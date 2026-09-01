#!/usr/bin/env python3
"""Supervisor state machine for the Coding Worker loop (Phase F.1.1-Pi.2).

Core principle (unchanged): an agent run finishing NEVER completes a job.
State lives in jobs.db so any crashed supervisor can be reconciled.

Phase F.1.1-Pi.2 hardening (behaviour-preserving unless noted):
  - H1: a test_command criterion passes ONLY when the registered test was
    executed and exited 0. NOT_RUN/UNKNOWN/MISSING/ERROR/TIMEOUT/BLOCKED
    can never satisfy acceptance.
  - H2: canonical changed-file set = committed diff vs base_sha UNION
    working-tree changes (staged/untracked included). Commits no longer
    make changes invisible to scope validation / secret scan.
  - H4: cancellation is persistent terminal intent; re-checked after the
    agent returns, after validation, and enforced via CAS on acceptance.
  - M1: per-job max_attempts is honoured (global config is only a fallback).
  - M2: dispatch uses a compare-and-set claim (no double runner).
  - M3: supervisor never scans a fake "diff" (json of changed-file names);
    bridge/workerctl secret scans are authoritative for sandbox jobs.
  - M6: progress signature hashes base/head sha, canonical changed names,
    diff hash, tests status and unmet criteria ids (not just file counts).
  - M7: git state unknown is fail-closed (dispatch refused, WAITING_HUMAN);
    spawn failure -> FAILED_TO_START (retryable), never stuck QUEUED.
  - M8: AGENT_STOPPED goes through transition(); timestamps coherent.

Repository allowlist remains human-managed: the supervisor refuses
repository names that are not allowlisted and never registers new paths.
It never pushes, merges, or creates PRs.
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# canonical secret-pattern module lives at the src/ level (see security_patterns.py)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jobs_db
import transport
from jobs_db import log
from security_patterns import SECRET_PATTERNS, scan_text, is_env_file
from transport import WorkerUnconfigured

CONFIG_PATH = Path(__file__).resolve().parent / "worker_config.json"


def load_config():
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def repo_cfg(name):
    cfg = load_config()
    rc = cfg.get("repositories", {}).get(name)
    if not rc:
        raise ValueError(f"repository not allowlisted: {name}")
    return rc


# ── deterministic secret scanning (pattern ids only, never values) ──────
# F.1.1-Pi.2 (Item 30): patterns live in security_patterns.py (single source).
# F.1.1-Pi.2 (M3): the supervisor NEVER scans a fake "diff" — for sandbox
# jobs the bridge/workerctl scan of the real worktree diff is authoritative
# (merged via result["secret_scan"]); locally we only scan canonical
# changed-file contents read from the fixture worktree.

def _changed_file_texts(repo_path, changed_files, max_bytes=200_000):
    texts = []
    for f in changed_files:
        if is_env_file(f):
            texts.append((".env_added:" + f, ""))
            continue
        p = Path(repo_path) / f
        if p.is_file() and p.exists():
            try:
                texts.append((f, p.read_text(encoding="utf-8", errors="replace")[:max_bytes]))
            except OSError:
                continue
    return texts


def secret_scan(repo_path, changed_files):
    """Returns list of pattern ids found (empty = clean). Never reports values."""
    hits = set()
    for f in changed_files or []:
        if is_env_file(f):
            hits.add("env_file_added")
    if repo_path and Path(repo_path).exists():
        for name, text in _changed_file_texts(repo_path, changed_files or []):
            if name.startswith(".env_added"):
                hits.add("env_file_added")
                continue
            hits |= scan_text(text)
    return sorted(hits)


def _git(repo_path, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo_path), capture_output=True, text=True, timeout=60,
    )


class GitStateUnknown(RuntimeError):
    """F.1.1-Pi.2 (M7): git state could not be determined — fail closed."""


def git_state(repo_path):
    """(branch, sha, dirty). Raises GitStateUnknown on any failure — the
    supervisor must never treat 'unknown' as 'clean' (M7 fail-closed)."""
    rb = _git(repo_path, "rev-parse", "--abbrev-ref", "HEAD")
    rs = _git(repo_path, "rev-parse", "HEAD")
    rst = _git(repo_path, "status", "--porcelain")
    sha = rs.stdout.strip()
    if rb.returncode != 0 or rs.returncode != 0 or rst.returncode != 0 or not sha:
        detail = ((rb.stderr or "") + (rs.stderr or "") + (rst.stderr or "")).strip()
        raise GitStateUnknown(
            f"git state unknown for {repo_path}: "
            f"{(detail or 'no error output')[:200]}")
    return rb.stdout.strip(), sha, bool(rst.stdout.strip())


def parse_porcelain_z(data):
    """Parse `git status --porcelain -z` output (Item 44: NUL-separated, no
    quoted-path / ' -> ' string parsing). Rename/copy entries carry the
    DESTINATION path in the first field; we keep the destination only."""
    files = set()
    fields = (data or "").split("\0")
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, path = entry[:2], entry[3:]
        if not path:
            continue
        if xy and xy[0] in "RC" and i < len(fields):
            i += 1  # skip the original-path field
        files.add(path)
    return files


def canonical_diff(repo_path, base_sha, max_bytes=2_000_000):
    """Bounded canonical diff bytes vs base (for hashing only, M6)."""
    if not base_sha:
        return b""
    r = _git(repo_path, "diff", f"{base_sha}...HEAD")
    if r.returncode != 0:
        raise GitStateUnknown(
            f"git diff vs base failed: {(r.stderr or '')[:200]}")
    return r.stdout.encode("utf-8", "replace")[:max_bytes]


def changed_files_since(repo_path, before_sha):
    """H2 canonical changed-file set:
      git diff --name-only <base>...HEAD  UNION  git status --porcelain
    so committed, staged, unstaged and untracked changes are all visible,
    even when the working tree is clean after an agent commit.
    Raises GitStateUnknown on git failure (fail-closed, M7)."""
    files = set()
    if before_sha:
        r = _git(repo_path, "diff", "--name-only", "-z", f"{before_sha}...HEAD")
        if r.returncode != 0:
            raise GitStateUnknown(
                f"git diff vs base failed: {(r.stderr or '')[:200]}")
        files |= {f for f in r.stdout.split("\0") if f}
    rst = _git(repo_path, "status", "--porcelain", "-z")
    if rst.returncode != 0:
        raise GitStateUnknown(
            f"git status failed: {(rst.stderr or '')[:200]}")
    files |= parse_porcelain_z(rst.stdout)
    return sorted(files)


def worker_offline():
    return bool(load_config().get("worker", {}).get("offline"))


def build_prompt(job, rcfg, prev_run=None, validation=None, behavior=None):
    """Initial or continuation prompt package (least-context, no personal data)."""
    lines = []
    if prev_run is None:
        lines.append(f"# Coding Job {job['job_id']} (attempt {job['attempt'] + 1})")
        lines.append(f"\nGoal:\n{job['goal']}")
        lines.append(f"\nRepository: {job['repository']} (work only inside this directory).")
        lines.append("\nAcceptance criteria (all must hold before the job can complete):")
        for ac in job["acceptance_criteria"]:
            lines.append(f"- [{ac['id']}] {ac['description']}")
        lines.append(
            "\nConstraints:\n"
            "- Work only inside this repository directory; never modify paths outside it.\n"
            "- Never run git push, or any remote/merge/deploy operation.\n"
            "- Verify your work by running the test command before you finish.\n"
            "- Ending your run does not complete the job; acceptance is evaluated separately."
        )
    else:
        passed = [a for a in (validation or []) if a.get("status") == "PASS"]
        failed = [a for a in (validation or []) if a.get("status") == "FAIL"]
        lines.append(
            f"# Coding Job {job['job_id']} — CONTINUATION (attempt {job['attempt'] + 1})"
        )
        lines.append("\nPrevious run accomplished:")
        for a in passed:
            lines.append(f"- [{a['id']}] {a['description']} — verified")
        if prev_run.get("result_summary"):
            lines.append(f"- Run summary: {prev_run['result_summary'][:400]}")
        lines.append("\nStill missing (failed acceptance criteria):")
        for a in failed:
            lines.append(f"- [{a['id']}] {a['description']} ({a.get('detail', '')})")
        if not failed:
            lines.append("- (validation did not pass; see details)")
        if job.get("tests_summary"):
            lines.append("\nTest output (tail):")
            lines.append("```text")
            lines.append(job["tests_summary"][-800:])
            lines.append("```")
        cf = job.get("git_changed_files") or []
        if cf:
            lines.append(f"\nRelevant diff (changed files): {', '.join(cf[:20])}")
        lines.append(
            "\nContinue from the current repository state.\n"
            "Do not redo completed work unnecessarily.\n"
            "Work only inside this repository; never run git push or remote operations."
        )
    if behavior:
        lines.append(f"\n#SIM-BEHAVIOR: {behavior}")
    return "\n".join(lines) + "\n"


def acceptance_decision(validation, tests_status):
    """H1 acceptance gate — the single place that decides acceptance.

    All deterministic criteria must PASS. Additionally, when any criterion
    has kind `test_command`, the registered test must have been EXECUTED and
    exited 0 (tests_status == PASS). NOT_RUN / UNKNOWN / MISSING / ERROR /
    TIMEOUT / BLOCKED are always UNMET (no silent bypass)."""
    if not validation or not all(a.get("status") == "PASS" for a in validation):
        return False
    kinds = {a.get("verification") for a in validation}
    if "test_command" in kinds and tests_status != "PASS":
        return False
    return True


def test_criterion_required(job):
    """True when the job's acceptance criteria include kind=test_command."""
    return any(
        (a.get("verification") or {}).get("kind") == "test_command"
        for a in (job.get("acceptance_criteria") or [])
    )


def progress_signature(job, run):
    """M6: no-progress signature over content, not just counts.

    Inputs: base sha, HEAD sha, canonical changed-file NAMES, diff hash,
    tests status, passing criteria ids and unmet criteria ids. Two runs that
    touch the same number of files with different content produce different
    signatures. Only hashes are stored — never file contents."""
    job = job or {}
    run = run or {}
    acceptance = job.get("acceptance_results") or []
    raw = json.dumps({
        "base": run.get("git_before"),
        "head": run.get("git_after"),
        "files": sorted(job.get("git_changed_files") or []),
        "diff": run.get("diff_hash"),
        "tests": job.get("tests_status"),
        "pass": sorted(a["id"] for a in acceptance if a.get("status") == "PASS"),
        "unmet": sorted(a["id"] for a in acceptance if a.get("status") != "PASS"),
    }, sort_keys=True)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _runner_start_ticks(pid):
    """Linux process start time (field 22 of /proc/<pid>/stat), or None when
    unavailable. Used so a recycled PID is never mistaken for our runner."""
    if os.name != "posix":
        return None
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read().decode()
        return data[data.rindex(")") + 2:].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def _spawn_runner(job_id):
    runner_log = jobs_db.LOGS_DIR / f"runner-{job_id}.log"
    pid_file = jobs_db.LOGS_DIR / f"runner-{job_id}.pid"
    with open(runner_log, "ab") as fh:
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), job_id],
            stdout=fh, stderr=fh, start_new_session=True,
        )
    # Item 45: persist PID + process start time (JSON). A stale pid file
    # whose PID was reused by an unrelated process must not look alive.
    pid_file.write_text(json.dumps({
        "pid": proc.pid, "start_ticks": _runner_start_ticks(proc.pid),
    }))
    return proc.pid


def read_runner_pid_file(job_id):
    """Returns (pid, start_ticks) — tolerant of legacy plain-pid pidfiles."""
    pid_file = jobs_db.LOGS_DIR / f"runner-{job_id}.pid"
    if not pid_file.exists():
        return None, None
    try:
        raw = pid_file.read_text().strip()
        try:
            data = json.loads(raw)
            return int(data["pid"]), data.get("start_ticks")
        except (ValueError, TypeError, KeyError):
            return int(raw), None
    except (OSError, ValueError):
        return None, None


def dispatch_job(job_id):
    job = jobs_db.get_job(job_id)
    if not job:
        return {"error": "not_found", "job_id": job_id}
    if job["status"] in ("RUNNING", "DISPATCHING", "VALIDATING"):
        return {"error": "already_active", "job_id": job_id, "status": job["status"]}
    if job["status"] in ("COMPLETED", "CANCELLED", "IMPLEMENTATION_ACCEPTED"):
        return {"error": "terminal", "job_id": job_id, "status": job["status"]}
    if worker_offline():
        jobs_db.transition(job_id, "QUEUED")
        log(job_id, None, "info", "dispatch requested while worker offline; kept QUEUED")
        return {"job_id": job_id, "status": "QUEUED", "note": "worker offline; job preserved"}
    try:
        transport.get_adapter(job)
    except WorkerUnconfigured as exc:
        jobs_db.transition(job_id, "WAITING_HUMAN", failure_reason=str(exc))
        jobs_db.update_job(job_id, needs_human=1,
                           next_action="provide external worker / fix agent kind")
        return {"job_id": job_id, "status": "WAITING_HUMAN",
                "note": str(exc)[:200]}
    # F.1.1-Pi.2 (M2): CAS claim — exactly one dispatcher may own the job.
    # Two concurrent dispatch_job calls race here; only one CAS succeeds.
    if not jobs_db.cas_status(job_id, "DISPATCHING",
                              jobs_db.DISPATCHABLE_STATUSES):
        return {"error": "already_claimed", "job_id": job_id,
                "status": jobs_db.get_job(job_id)["status"]}
    try:
        pid = _spawn_runner(job_id)
    except Exception as exc:  # M7: never leave the job stuck QUEUED/DISPATCHING
        jobs_db.transition(job_id, "FAILED_TO_START",
                           failure_reason=f"runner spawn failed: {exc}"[:300])
        log(job_id, None, "error", f"runner spawn failed: {exc}"[:300])
        return {"job_id": job_id, "status": "FAILED_TO_START",
                "note": str(exc)[:200]}
    log(job_id, None, "info", f"dispatched detached runner pid={pid}")
    return {"job_id": job_id, "status": "DISPATCHING", "runner_pid": pid}


def _worker_job_alive(job):
    """Remote reconcile: is the worker reachable and the job still running there?"""
    try:
        cfg = transport.remote_config()
    except WorkerUnconfigured:
        return None
    try:
        p = transport._ssh(cfg, ["job_status", job["job_id"]], timeout=30)
        if p.returncode != 0:
            return None
        return json.loads(p.stdout or "{}")
    except Exception:
        return None


def _pid_alive(pid, start_ticks=None):
    """True only if the pid exists AND is not a zombie (reaped-but-unwaited
    runners must count as dead). When `start_ticks` is known, a recycled PID
    running unrelated code also counts as dead (Item 45)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if os.name == "posix":
        try:
            with open(f"/proc/{pid}/stat", "rb") as fh:
                data = fh.read().decode()
            fields = data[data.rindex(")") + 2:].split()
            state = fields[0]
            if state == "Z":
                return False
            if start_ticks is not None and fields[19] != str(start_ticks):
                return False  # PID was reused by a different process
        except (OSError, ValueError, IndexError):
            return True
    return True


def reconcile():
    """Crash recovery for both transports (never marks jobs COMPLETED)."""
    out = []
    for job in jobs_db.list_jobs(limit=100):
        if job["status"] not in ("RUNNING", "DISPATCHING", "VALIDATING",
                                 "AGENT_STOPPED"):
            continue
        pid, start_ticks = read_runner_pid_file(job["job_id"])
        alive = False
        if pid is not None:
            try:
                alive = _pid_alive(pid, start_ticks)
            except (ProcessLookupError, PermissionError):
                alive = False
        if alive:
            out.append({"job_id": job["job_id"], "was": job["status"],
                        "now": job["status"] + " (live runner)"})
            continue
        is_remote = (job.get("agent") or "").startswith("remote-")
        is_pi = (job.get("agent") or "").startswith("pi-sandbox-")
        if is_pi and job["status"] == "RUNNING":
            # worker container may still be alive on the bridge; never lose it
            try:
                binfo = transport.bridge_request(
                    {"version": 1, "action": "job_status", "job_id": job["job_id"]},
                    timeout=30)
            except Exception:
                binfo = None
            if binfo and binfo.get("state") == "running":
                jobs_db.transition(job["job_id"], "INTERRUPTED",
                                   failure_reason="supervisor runner died; worker container still running")
                jobs_db.update_job(job["job_id"], needs_human=1,
                                   next_action="job_continue to re-adopt the running container")
                out.append({"job_id": job["job_id"], "was": "RUNNING",
                            "now": "INTERRUPTED (container alive on bridge, runner dead)"})
                continue
            if binfo and binfo.get("state") in ("finished", "failed"):
                jobs_db.transition(job["job_id"], "AGENT_STOPPED",
                                   failure_reason="runner died; worker container finished")
                jobs_db.update_job(job["job_id"], next_action="job_continue to collect and continue")
                out.append({"job_id": job["job_id"], "was": "RUNNING", "now": "AGENT_STOPPED"})
                continue
            jobs_db.transition(job["job_id"], "INTERRUPTED",
                               failure_reason="supervisor restart; bridge unreachable")
            jobs_db.update_job(job["job_id"], needs_human=1,
                               next_action="job_continue after bridge recovers")
            out.append({"job_id": job["job_id"], "was": job["status"],
                        "now": "INTERRUPTED (bridge unreachable)"})
            continue
        if is_remote:
            info = _worker_job_alive(job)
            if info is None:
                jobs_db.transition(job["job_id"], "INTERRUPTED",
                                   failure_reason="worker unreachable during reconciliation")
                jobs_db.update_job(job["job_id"], needs_human=1,
                                   next_action="job_continue after worker recovers")
                out.append({"job_id": job["job_id"], "was": job["status"],
                            "now": "INTERRUPTED (worker unreachable)"})
                continue
            if info.get("state") == "running":
                out.append({"job_id": job["job_id"], "was": job["status"],
                            "now": "RUNNING on worker (monitoring resumed)"})
                jobs_db.transition(job["job_id"], "RUNNING")
                continue
        if job["status"] in ("RUNNING", "DISPATCHING", "VALIDATING"):
            jobs_db.transition(job["job_id"], "AGENT_STOPPED",
                               failure_reason="supervisor restart: runner not alive")
            jobs_db.update_job(job["job_id"], next_action="job_continue to resume")
            out.append({"job_id": job["job_id"], "was": job["status"],
                        "now": "AGENT_STOPPED"})
        else:
            out.append({"job_id": job["job_id"], "was": job["status"],
                        "now": job["status"] + " (recoverable)"})
    return out


def run_job_loop(job_id):
    """The detached job driver: dispatch agent runs until accepted or stopped."""
    cfg = load_config()
    limits = cfg.get("limits", {})
    global_max_attempts = limits.get("max_attempts", 6)
    agent_timeout = limits.get("agent_timeout_s", 1200)
    test_timeout = limits.get("test_timeout_s", 600)
    no_progress_limit = limits.get("no_progress_strikes", 2)

    jobs_db.init_db()
    job = jobs_db.get_job(job_id)
    if not job:
        print(f"job not found: {job_id}")
        return 1

    try:
        adapter = transport.get_adapter(job)
        rcfg = repo_cfg(job["repository"])
    except WorkerUnconfigured as exc:
        jobs_db.transition(job_id, "WAITING_HUMAN", failure_reason=str(exc))
        jobs_db.update_job(job_id, needs_human=1)
        return 1
    except ValueError as exc:
        jobs_db.transition(job_id, "FAILED", failure_reason=str(exc))
        return 1

    repo_path = Path(rcfg["path"]) \
        if (adapter.name == "local-fixture" and Path(rcfg["path"]).exists()) else None
    agent = job.get("agent") or cfg.get("default_agent") or "simulated"
    if adapter.name == "local-fixture" and not rcfg.get("allow_simulated"):
        jobs_db.transition(job_id, "FAILED",
                           failure_reason="fixture agent not allowed for this repository")
        return 1

    before_sha = None
    if repo_path is not None:
        # M7 fail-closed: unknown git state must never be read as clean.
        try:
            branch, sha, dirty = git_state(repo_path)
        except GitStateUnknown as exc:
            jobs_db.transition(job_id, "WAITING_HUMAN", failure_reason=str(exc)[:300])
            jobs_db.update_job(job_id, needs_human=1,
                               next_action="resolve git state, then job_continue")
            log(job_id, None, "error", f"git state unknown; dispatch refused: {exc}"[:300])
            return 1
        jobs_db.update_job(job_id, git_before_sha=sha, git_dirty_before=int(dirty))
        if dirty:
            jobs_db.transition(job_id, "WAITING_HUMAN",
                               failure_reason="repository working tree is dirty before dispatch; human decision required")
            return 1
        default_branches = {"main", "master"}
        if job.get("branch"):
            target = job["branch"]
            if _git(repo_path, "rev-parse", "--verify", target).returncode != 0:
                _git(repo_path, "checkout", "-b", target)
            else:
                _git(repo_path, "checkout", target)
        elif branch in default_branches:
            target = f"hermes/{job_id}"
            _git(repo_path, "checkout", "-b", target)
            jobs_db.update_job(job_id, branch=target)
        else:
            jobs_db.update_job(job_id, branch=branch)
        before_sha = jobs_db.get_job(job_id)["git_before_sha"] or sha
    jobs_db.transition(job_id, "DISPATCHING")

    prev_signature = None
    strikes = job.get("no_progress_strikes", 0)
    continuation_reason = None
    validation = None

    while True:
        job = jobs_db.get_job(job_id)
        if job["status"] in ("CANCELLED", "WAITING_HUMAN", "COMPLETED",
                             "IMPLEMENTATION_ACCEPTED", "INTERRUPTED"):
            break
        # M1: per-job max_attempts wins; the global config is only the
        # fallback for legacy fixtures whose job row lacks the value.
        max_attempts = job.get("max_attempts") or global_max_attempts
        if job["attempt"] >= max_attempts:
            jobs_db.transition(job_id, "WAITING_HUMAN",
                               failure_reason=f"attempt limit {max_attempts} reached")
            break

        attempt = job["attempt"] + 1
        prev_run = jobs_db.latest_run(job_id)
        behavior = None
        if agent in ("simulated", "fixture-local-hermes", "pi-sandbox-fixture"):
            behavior = _sim_behavior_for(job_id)
        prompt = build_prompt(job, rcfg, prev_run=prev_run, validation=validation,
                              behavior=behavior)
        prompt_path = jobs_db.PROMPTS_DIR / f"{job_id}-attempt{attempt}.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        prompt_hash = hashlib.sha1(prompt.encode()).hexdigest()[:16]

        jobs_db.update_job(job_id, attempt=attempt, prompt_path=str(prompt_path),
                           agent=agent, tests_status="NOT_RUN", tests_summary=None)
        run_id = jobs_db.create_run(job_id, attempt, agent, prompt_path, prompt_hash)
        jobs_db.update_run(run_id, continuation_reason=continuation_reason)
        jobs_db.transition(job_id, "RUNNING")
        log(job_id, run_id, "info",
            f"agent start attempt={attempt} transport={adapter.name}")

        try:
            result = adapter.run(job, rcfg, repo_path, prompt, agent_timeout)
        except WorkerUnconfigured as exc:
            jobs_db.transition(job_id, "WAITING_HUMAN", failure_reason=str(exc))
            jobs_db.update_job(job_id, needs_human=1,
                               next_action="configure external worker")
            log(job_id, run_id, "warn", "worker unconfigured during run")
            break

        # H4: cancellation is terminal intent. Re-read it as the FIRST thing
        # after the agent returns — never let a cancelled job advance to
        # VALIDATING / IMPLEMENTATION_ACCEPTED just because the loop is here.
        if jobs_db.cancel_requested(job_id):
            jobs_db.update_run(run_id, finished_at=jobs_db.now_iso(),
                               result_summary="cancelled during agent run")
            log(job_id, run_id, "info",
                "cancel detected after agent stop; validation skipped")
            break

        if result.get("status") in ("REJECTED", "BUSY"):
            jobs_db.update_run(run_id, finished_at=jobs_db.now_iso(),
                               result_summary="rejected by worker bridge")
            jobs_db.transition(job_id, "WAITING_HUMAN",
                               failure_reason=(result.get("stderr_tail")
                                               or "worker rejected the job")[:300])
            jobs_db.update_job(job_id, needs_human=1,
                               next_action="inspect bridge rejection reason")
            log(job_id, run_id, "warn",
                f"worker rejected job: {(result.get('stderr_tail') or '')[:150]}")
            break


        if result.get("status") == "INTERRUPTED":
            jobs_db.update_run(run_id, finished_at=jobs_db.now_iso(),
                               timed_out=int(result.get("timed_out") or 0),
                               result_summary="worker interrupted")
            jobs_db.transition(job_id, "INTERRUPTED",
                               failure_reason=result.get("stderr_tail") or "worker interrupted")
            jobs_db.update_job(job_id, needs_human=1,
                               next_action="job_continue after worker recovers")
            log(job_id, run_id, "warn", "worker interrupted; job preserved")
            break

        out_path = jobs_db.LOGS_DIR / f"{run_id}-stdout.log"
        err_path = jobs_db.LOGS_DIR / f"{run_id}-stderr.log"
        out_path.write_text(result.get("stdout_tail") or "", encoding="utf-8")
        err_path.write_text(result.get("stderr_tail") or "", encoding="utf-8")

        changed = result.get("changed_files")
        head_sha = None
        dirty2 = None
        diff_hash = result.get("diff_hash")
        if adapter.name == "local-fixture" and repo_path is not None:
            try:
                # H2: canonical set (committed + staged + unstaged + untracked)
                changed = changed_files_since(repo_path, before_sha)
                _, head_sha, dirty2 = git_state(repo_path)
                diff_hash = hashlib.sha1(
                    canonical_diff(repo_path, before_sha)).hexdigest()[:16]
            except GitStateUnknown as exc:
                jobs_db.update_run(run_id, finished_at=jobs_db.now_iso())
                jobs_db.transition(job_id, "WAITING_HUMAN",
                                   failure_reason=str(exc)[:300])
                jobs_db.update_job(job_id, needs_human=1,
                                   next_action="resolve git state, then job_continue")
                log(job_id, run_id, "error",
                    f"git state unknown after agent run: {exc}"[:300])
                break
        else:
            head_sha = result.get("head_sha")
        summary = (result.get("stdout_tail") or "").strip().splitlines() or [""]
        summary = " | ".join(s.strip()[:120] for s in summary if s.strip())[:400]

        jobs_db.update_run(
            run_id, finished_at=jobs_db.now_iso(), exit_code=result.get("exit_code"),
            timed_out=int(result.get("timed_out") or 0), stdout_path=str(out_path),
            stderr_path=str(err_path), result_summary=summary,
            git_before=before_sha, git_after=head_sha, diff_hash=diff_hash,
        )
        # AGENT_STOPPED: the run ended. This is NOT completion. (M8: audited
        # state change goes through transition(), never a raw status update.)
        jobs_db.transition(job_id, "AGENT_STOPPED")
        jobs_db.update_job(
            job_id, result_summary=summary,
            exit_code=result.get("exit_code"), timed_out=int(result.get("timed_out") or 0),
            git_after_sha=head_sha,
            git_dirty_after=dirty2 if dirty2 is not None else 0,
            git_changed_files=changed or [],
        )
        log(job_id, run_id, "info",
            f"agent stopped exit={result.get('exit_code')} changed={len(changed or [])}")

        # validation (deterministic, independent from agent self-report)
        jobs_db.transition(job_id, "VALIDATING")
        ev_tests = result.get("test_summary")
        if isinstance(ev_tests, dict) and ev_tests.get("criteria_results") is not None:
            # remote worker executed the allowlisted tests + deterministic checks
            tests_status = ev_tests.get("status") or "NOT_RUN"
            tests_summary = ev_tests.get("summary")
            validation = ev_tests["criteria_results"]
        elif repo_path is not None:
            validation, tests_status, tests_summary = _criteria_from_local(
                job, rcfg, repo_path, changed, test_timeout)
        else:
            validation, tests_status, tests_summary = [], "NOT_RUN", None

        # H4: cancelled while validating? Re-check before writing anything.
        if jobs_db.cancel_requested(job_id):
            jobs_db.update_job(job_id, acceptance_results=validation,
                               tests_status=tests_status, tests_summary=tests_summary)
            log(job_id, run_id, "info", "cancel detected during validation; skip")
            break

        # deterministic secret scan (before any acceptance).
        # M3: no synthetic diff scanning. For sandbox jobs the bridge /
        # workerctl already scanned the real worktree diff (authoritative);
        # its pattern ids arrive via result["secret_scan"]. The supervisor
        # only reads canonical changed-file contents for local fixture jobs.
        scan_hits = set(secret_scan(repo_path, changed or []))
        scan_hits.update(result.get("secret_scan") or [])
        if scan_hits:
            jobs_db.update_job(job_id, acceptance_results=validation,
                               tests_status=tests_status, tests_summary=tests_summary)
            jobs_db.transition(job_id, "WAITING_HUMAN",
                               failure_reason="secret-like content detected in diff (patterns: "
                                              + ",".join(sorted(scan_hits)) + ")")
            jobs_db.update_job(job_id, needs_human=1,
                               next_action="human review of flagged diff")
            log(job_id, run_id, "warn", f"secret scan hit: {','.join(sorted(scan_hits))}")
            break

        jobs_db.update_job(job_id, acceptance_results=validation,
                           tests_status=tests_status, tests_summary=tests_summary)

        # H1: test_command criteria require an executed, exiting-0 registered
        # test. NOT_RUN (or any non-PASS tests_status) can never accept.
        all_pass = acceptance_decision(validation, tests_status)

        if all_pass:
            # H4: final guard — claim acceptance with CAS from VALIDATING so a
            # concurrent job_cancel (which sets CANCELLED) can never be
            # overwritten by IMPLEMENTATION_ACCEPTED.
            if not jobs_db.cas_status(job_id, "IMPLEMENTATION_ACCEPTED",
                                      ("VALIDATING",)):
                log(job_id, run_id, "info",
                    "acceptance lost the CAS race (cancel/other owner); not accepted")
                break
            # deterministic implementation acceptance — NOT completion.
            jobs_db.update_job(job_id, review_status="WAITING_REVIEW",
                               next_action="independent review (Phase F.2)",
                               needs_human=0, no_progress_strikes=0,
                               result_summary=summary)
            log(job_id, run_id, "info",
                "deterministic acceptance satisfied -> IMPLEMENTATION_ACCEPTED (review pending)")
            break

        # not complete: continuation decision
        sig = progress_signature(jobs_db.get_job(job_id), jobs_db.get_run(run_id))
        if sig == prev_signature:
            strikes += 1
        else:
            strikes = 0
        prev_signature = sig
        jobs_db.update_job(job_id, no_progress_strikes=strikes)
        jobs_db.update_run(run_id, progress_signature=sig)

        if strikes >= no_progress_limit:
            jobs_db.transition(job_id, "WAITING_HUMAN",
                               failure_reason=f"no progress across {strikes + 1} consecutive runs")
            log(job_id, run_id, "warn", f"no-progress stop (strikes={strikes})")
            break

        continuation_reason = "acceptance criteria not yet satisfied"
        jobs_db.transition(job_id, "CONTINUE_REQUIRED")
        log(job_id, run_id, "info", f"CONTINUE_REQUIRED (attempt {attempt} incomplete)")

    return 0


def _criteria_from_local(job, rcfg, repo_path, changed, test_timeout):
    import validator as vmod
    return vmod.evaluate(job, rcfg, repo_path, changed, test_timeout)


def _sim_behavior_for(job_id):
    """Test-harness hook: simulated behaviour is declared by a #SIM-BEHAVIOR
    line in the job goal (placed only by the test harness on fixture repos)."""
    job = jobs_db.get_job(job_id)
    goal = job.get("goal", "") if job else ""
    for line in goal.splitlines():
        if line.startswith("#SIM-BEHAVIOR:"):
            return line.split(":", 1)[1].strip()
    return None


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: supervisor.py <job_id>")
        raise SystemExit(2)
    raise SystemExit(run_job_loop(sys.argv[1]))
