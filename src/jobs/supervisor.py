#!/usr/bin/env python3
"""Supervisor state machine for the Coding Worker loop (Phase F.1.1).

Core principle (unchanged): an agent run finishing NEVER completes a job.
State lives in jobs.db so any crashed supervisor can be reconciled.

Phase F.1.1 changes (supervision logic preserved):
  - execution goes through transport adapters (local-fixture for test repos
    only; remote restricted-SSH worker for production)
  - lifecycle correction: deterministic acceptance now yields
    IMPLEMENTATION_ACCEPTED (+ review_status=WAITING_REVIEW) instead of
    COMPLETED. COMPLETED is reserved for the Phase F.2 independent review.
  - deterministic secret scanning of diffs before acceptance
  - remote-worker reconciliation: unreachable worker -> INTERRUPTED (job is
    never treated as completed by loss of connection)

Repository allowlist remains human-managed: the supervisor refuses
repository names that are not allowlisted and never registers new paths.
It never pushes, merges, or creates PRs.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import jobs_db
import transport
from jobs_db import log
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
SECRET_PATTERNS = [
    ("private_key_header", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_pat", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("openai_like_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("credential_assignment", re.compile(
        r"(?i)\b(api[_-]?key|secret|passwd|password|token)\b\s*[=:]\s*"
        r"[\"']?[A-Za-z0-9_\-+/]{20,}")),
]


def _changed_file_texts(repo_path, before_sha, changed_files, max_bytes=200_000):
    texts = []
    for f in changed_files:
        if f.lower().endswith(".env") or f == ".env":
            texts.append((".env_added:" + f, ""))
            continue
        p = Path(repo_path) / f
        if p.is_file() and p.exists():
            try:
                texts.append((f, p.read_text(encoding="utf-8", errors="replace")[:max_bytes]))
            except OSError:
                continue
    return texts


def secret_scan(repo_path, before_sha, changed_files, diff_text=None):
    """Returns list of pattern ids found (empty = clean). Never reports values."""
    hits = set()
    for f in changed_files or []:
        if f.lower().endswith(".env") or Path(f).name == ".env":
            hits.add("env_file_added")
    scanned = []
    if repo_path and Path(repo_path).exists():
        scanned = _changed_file_texts(repo_path, before_sha, changed_files)
    elif diff_text:
        scanned = [("diff", diff_text[:200_000])]
    for name, text in scanned:
        if name.startswith(".env_added"):
            hits.add("env_file_added")
            continue
        for pid, rx in SECRET_PATTERNS:
            if rx.search(text):
                hits.add(pid)
    return sorted(hits)


def _git(repo_path, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo_path), capture_output=True, text=True, timeout=60,
    )


def git_state(repo_path):
    branch = _git(repo_path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    sha = _git(repo_path, "rev-parse", "HEAD").stdout.strip()
    dirty = bool(_git(repo_path, "status", "--porcelain").stdout.strip())
    return branch, sha, dirty


def changed_files_since(repo_path, before_sha):
    if not before_sha:
        return []
    out = _git(repo_path, "diff", "--name-only", before_sha).stdout
    staged = _git(repo_path, "status", "--porcelain").stdout
    files = set(l.strip() for l in out.splitlines() if l.strip())
    for line in staged.splitlines():
        if len(line) > 3:
            files.add(line[3:].strip())
    return sorted(f for f in files if f)


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


def progress_signature(job, run):
    raw = json.dumps({
        "git": run.get("git_after"),
        "tests": job.get("tests_status"),
        "pass": sorted(a["id"] for a in job.get("acceptance_results", [])
                       if a.get("status") == "PASS"),
        "files": len(job.get("git_changed_files") or []),
    }, sort_keys=True)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _spawn_runner(job_id):
    runner_log = jobs_db.LOGS_DIR / f"runner-{job_id}.log"
    pid_file = jobs_db.LOGS_DIR / f"runner-{job_id}.pid"
    with open(runner_log, "ab") as fh:
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), job_id],
            stdout=fh, stderr=fh, start_new_session=True,
        )
    pid_file.write_text(str(proc.pid))
    return proc.pid


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
    jobs_db.transition(job_id, "QUEUED")
    pid = _spawn_runner(job_id)
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


def _pid_alive(pid):
    """True only if the pid exists AND is not a zombie (reaped-but-unwaited
    runners must count as dead)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read().decode()
        state = data[data.rindex(")") + 2:].split()[0]
        return state != "Z"
    except (OSError, ValueError, IndexError):
        return True


def reconcile():
    """Crash recovery for both transports (never marks jobs COMPLETED)."""
    out = []
    for job in jobs_db.list_jobs(limit=100):
        if job["status"] not in ("RUNNING", "DISPATCHING", "VALIDATING",
                                 "AGENT_STOPPED"):
            continue
        pid_file = jobs_db.LOGS_DIR / f"runner-{job['job_id']}.pid"
        alive = False
        if pid_file.exists():
            try:
                alive = _pid_alive(int(pid_file.read_text().strip()))
            except (ValueError, ProcessLookupError, PermissionError):
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
    max_attempts = limits.get("max_attempts", 6)
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
        branch, sha, dirty = git_state(repo_path)
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
        if adapter.name == "local-fixture" and repo_path is not None:
            branch2, head_sha, dirty2 = git_state(repo_path)
            changed = changed_files_since(repo_path, before_sha)
        else:
            head_sha = result.get("head_sha")
        summary = (result.get("stdout_tail") or "").strip().splitlines() or [""]
        summary = " | ".join(s.strip()[:120] for s in summary if s.strip())[:400]

        jobs_db.update_run(
            run_id, finished_at=jobs_db.now_iso(), exit_code=result.get("exit_code"),
            timed_out=int(result.get("timed_out") or 0), stdout_path=str(out_path),
            stderr_path=str(err_path), result_summary=summary,
            git_before=before_sha, git_after=head_sha,
        )
        # AGENT_STOPPED: the run ended. This is NOT completion.
        jobs_db.update_job(
            job_id, status="AGENT_STOPPED", result_summary=summary,
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

        # deterministic secret scan of the diff (before any acceptance)
        diff_text = None
        if adapter.name != "local-fixture":
            diff_text = json.dumps(changed or [])
        scan_hits = set(secret_scan(repo_path if repo_path else None,
                                    before_sha if repo_path else None,
                                    changed or [], diff_text=diff_text))
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

        all_pass = bool(validation) and all(a["status"] == "PASS" for a in validation) \
            and tests_status in ("PASS", "NOT_RUN")

        if all_pass:
            # deterministic implementation acceptance — NOT completion.
            jobs_db.transition(job_id, "IMPLEMENTATION_ACCEPTED")
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
