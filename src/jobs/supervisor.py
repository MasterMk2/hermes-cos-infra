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
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# canonical secret-pattern module lives at the src/ level (see security_patterns.py)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jobs_db
import review as rv
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


def build_prompt(job, rcfg, prev_run=None, validation=None, behavior=None,
                 review_fix=None):
    """Initial or continuation prompt package (least-context, no personal data).

    F.2 (section 57/58): fix attempts receive the reviewer's STRUCTURED
    blocking findings only — never the reviewer conversation transcript."""
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
    if review_fix is not None and (review_fix.get("findings") or []):
        blocking = [f for f in review_fix["findings"] if f.get("blocking")]
        if blocking:
            lines.append(
                f"\n# Independent review (cycle {review_fix.get('cycle')}) "
                f"returned CHANGES_REQUIRED"
            )
            lines.append(
                "Blocking findings (structured only — the reviewer "
                "conversation is not shared):"
            )
            for f in blocking:
                loc = f.get("file") or "(repository-wide)"
                span = ""
                if f.get("line_start"):
                    span = f":{f.get('line_start')}" + (
                        f"-{f.get('line_end')}" if f.get("line_end") else "")
                lines.append(
                    f"- [{f.get('severity')}/{f.get('category')}] {loc}{span} "
                    f"— {f.get('title')}")
                if f.get("explanation"):
                    lines.append(f"  explanation: {f['explanation']}")
                if f.get("suggested_fix"):
                    lines.append(f"  suggested_fix: {f['suggested_fix']}")
            lines.append(
                "\nAddress the blocking review findings while preserving all "
                "original acceptance criteria.\n"
                "Do not blindly follow a finding if repository evidence proves "
                "it incorrect; if a finding appears incorrect, preserve "
                "technical evidence explaining why.\n"
                "Do not push or merge.\n"
                "The Supervisor will rerun deterministic validation and an "
                "independent review after your changes."
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


# ─────────────────────────────────────────────────────────────────────────
# Phase F.2: independent review + exact-revision completion gate
# ─────────────────────────────────────────────────────────────────────────

# Reviewer identity is FIXED (section 6/19): the supervisor never selects a
# reviewer model and never falls back to another one.
REVIEWER_AGENT = rv.REVIEWER_LOGICAL_ID
REVIEWER_MODEL = rv.REVIEWER_MODEL

# Bounded in-cycle retries (section 40/93): one repair for invalid JSON,
# one re-attempt for interrupted/hung reviews.
MAX_INVALID_ATTEMPTS = 2
MAX_INTERRUPTED_ATTEMPTS = 2


def _bridge(payload, timeout=60):
    """Bridge RPC with the container path first (transport helper)."""
    return transport.bridge_request(payload, timeout=timeout)


def fetch_production_evidence(job_id):
    """Fresh implementation evidence for the review phase (job_result)."""
    try:
        resp = _bridge({"version": 1, "action": "job_result", "job_id": job_id},
                       timeout=60)
    except (OSError, ValueError, WorkerUnconfigured) as exc:
        log(job_id, None, "review", f"bridge unreachable for evidence: {exc}")
        return None
    if resp.get("error"):
        log(job_id, None, "review", f"evidence fetch failed: {resp['error']}")
        return None
    return resp.get("evidence") or {}


def fetch_fresh_verification(job_id, timeout_s=900):
    """F.2: fresh exact-revision verification via the worker adapter
    (bridge job_verify: current HEAD + content binding + secret scan +
    a real re-run of the registered tests). Used as the authoritative
    binding source for review cycles and the completion gate — finalize-time
    evidence can be stale for legacy jobs accepted before F.2."""
    try:
        ver = transport.bridge_request(
            {"version": 1, "action": "job_verify", "job_id": job_id},
            timeout=timeout_s)
    except (OSError, ValueError, WorkerUnconfigured) as exc:
        log(job_id, None, "review", f"bridge unreachable for verify: {exc}")
        return None
    if ver.get("error"):
        log(job_id, None, "review", f"job_verify failed: {ver['error']}")
        return None
    return ver


def merge_review_evidence(ev, ver):
    """Package evidence (finalize snapshot) + fresh binding (job_verify)."""
    merged = dict(ev or {})
    if ver:
        merged["head_sha"] = ver.get("head_sha") or merged.get("head_sha")
        merged["base_sha"] = ver.get("base_sha") or merged.get("base_sha")
        merged["content_sha256"] = ver.get("content_sha256") or \
            merged.get("content_sha256")
        merged["diff_sha256"] = ver.get("diff_sha256") or merged.get("diff_sha256")
        merged["changed_files"] = ver.get("changed_files") or \
            merged.get("changed_files") or []
        merged["secret_scan"] = ver.get("secret_scan") or []
        old_ts = merged.get("test_summary") or {}
        merged["test_summary"] = {
            "status": (ver.get("tests") or {}).get("status") or
                      old_ts.get("status") or "NOT_RUN",
            "summary": (ver.get("tests") or {}).get("summary") or
                       old_ts.get("summary"),
            "criteria_results": old_ts.get("criteria_results") or [],
        }
    return merged


def fetch_review_diff(job_id, max_bytes=rv.MAX_PACKAGE_DIFF_CHARS):
    """Bounded canonical diff text for the review package (section 47)."""
    try:
        resp = _bridge({"version": 1, "action": "job_diff", "job_id": job_id,
                        "max_bytes": max_bytes}, timeout=120)
    except (OSError, ValueError, WorkerUnconfigured) as exc:
        log(job_id, None, "review", f"bridge unreachable for diff: {exc}")
        return None
    if resp.get("error"):
        return None
    return resp.get("diff") or ""


def _review_usage_log(job_id, cycle, usage):
    """Section 74/75/119: per-role, per-cycle review usage in event_log."""
    if not isinstance(usage, dict):
        return
    attempts = usage.get("attempts") or []
    if not attempts:
        return
    total = usage.get("total_tokens")
    jobs_db.log(job_id, None, "usage",
                "review usage cycle={c} model={m} in={i} out={o} reasoning={r} "
                "cache_read={cr} total={t} api_calls={call} cost_usd={u} "
                "attempts={n}".format(
                    c=cycle, m=rv.REVIEWER_MODEL,
                    i=usage.get("input_tokens"), o=usage.get("output_tokens"),
                    r=usage.get("reasoning_tokens"),
                    cr=usage.get("cache_read_tokens"), t=total,
                    call=usage.get("api_calls"),
                    # section 76: never write an invented cost
                    u=(usage.get("estimated_cost_usd")
                       if usage.get("cost_status") == "provider_reported"
                       else "unavailable"),
                    n=len(attempts)))


def _mark_review_interrupted(job_id, rid):
    jobs_db.update_review(rid, status="interrupted",
                          error_summary="reviewer interrupted (timeout/hang/bridge)")
    jobs_db.update_job(job_id, review_status="REVIEW_INTERRUPTED",
                       next_action="job_review to resume the interrupted review",
                       needs_human=0)
    jobs_db.log(job_id, None, "review",
                f"review interrupted review_id={rid} (bounded retry available "
                f"via job_review)")


def _review_fail(job_id, rid, why):
    jobs_db.update_review(rid, status="failed", error_summary=why[:300])
    jobs_db.update_job(job_id, review_status="REVIEW_FAILED",
                       needs_human=1,
                       next_action="inspect review failure; rerun with job_review "
                                   "only after fixing the cause")
    jobs_db.transition(job_id, "WAITING_HUMAN",
                       failure_reason=f"review failed: {why}"[:300])
    jobs_db.log(job_id, None, "review", f"review failed review_id={rid}: {why}"[:200])


def _review_cancelled(job_id, rid):
    jobs_db.update_review(rid, status="cancelled",
                          error_summary="job cancelled during review")
    jobs_db.log(job_id, None, "review", f"review cancelled review_id={rid}")


def run_review_phase(job_id, cfg):
    """F.2 review phase. Returns one of:
      COMPLETED        final gate passed and completion CAS won
      FIX_REQUIRED     CHANGES_REQUIRED verdict recorded; fix attempt needed
      WAITING_HUMAN    review failed / cycle budget exhausted / stalled
      REVIEW_AGAIN     approval present but stale/unbound → fresh review
      REVIEW_INTERRUPTED  reviewer interrupted (bounded retry via job_review)
      NOT_REQUIRED     test-kind fixture job (review policy: optional)
      CANCELLED        cancel requested during review

    Approval binding + completion conditions live in completion_decision()
    — the single canonical gate (section 55)."""
    job = jobs_db.get_job(job_id)
    if not job:
        return "NOT_FOUND"
    if jobs_db.cancel_requested(job_id):
        return "CANCELLED"
    # Section 111: review is mandatory for production (implementation) jobs
    # and skipped for test-kind fixture jobs. The production policy is fixed
    # in code — no MCP/config path can disable it (section 68).
    if job.get("kind") != "implementation":
        return "NOT_REQUIRED"

    limits = cfg.get("limits", {})
    max_cycles = max(1, int(limits.get("max_review_cycles", 3)))
    review_timeout = int(limits.get("agent_timeout_s", 1200))
    rcfg = repo_cfg(job["repository"])

    # single-owner claim (section 85: exactly one review runner). The CAS is
    # the ONLY claim point — a losing runner exits (REVIEW_BUSY); there is no
    # silent adoption by a stranger.
    if not jobs_db.cas_review_status(
            job_id, "REVIEWING", ("WAITING_REVIEW", "REVIEW_INTERRUPTED")):
        job = jobs_db.get_job(job_id)
        rs = (job.get("review_status") or "")
        if rs == "APPROVED":
            return "APPROVED_PRESENT"
        if rs == "REVIEWING":
            pid, ticks = read_runner_pid_file(job_id)
            if pid == os.getpid():
                pass  # our own pre-claim (job_review claimed before spawning us)
            elif pid is not None and _pid_alive(pid, ticks):
                return "REVIEW_BUSY"
            else:
                # the previous owner died between claim and review start;
                # take over only while the claim is still unowned
                if not jobs_db.cas_review_status(job_id, "REVIEWING",
                                                 ("REVIEWING",)):
                    return "REVIEW_BUSY"
        else:
            return "REVIEW_BUSY"

    used = jobs_db.reviews_for_job(job_id)
    verdicted = [r for r in used
                 if r["status"] in ("approved", "changes_required", "failed")]
    if len(verdicted) >= max_cycles:
        jobs_db.transition(job_id, "WAITING_HUMAN",
                           failure_reason=f"review_cycle_limit_reached "
                                          f"({len(verdicted)} cycles)")
        jobs_db.update_job(job_id, needs_human=1,
                           next_action="human decision on review findings")
        jobs_db.log(job_id, None, "review",
                    f"review cycle limit reached ({max_cycles})")
        return "WAITING_HUMAN"
    prev = jobs_db.latest_review(job_id)
    if prev and prev.get("status") == "interrupted":
        cycle, attempt_no = prev["cycle"], (prev.get("attempt_no") or 1) + 1
        if attempt_no > MAX_INTERRUPTED_ATTEMPTS + 1:
            jobs_db.transition(job_id, "WAITING_HUMAN",
                               failure_reason="review retries exhausted after "
                                              "repeated interruptions")
            jobs_db.update_job(job_id, needs_human=1,
                               next_action="inspect reviewer infra; job_review "
                                           "to retry once resolved")
            return "WAITING_HUMAN"
    elif prev:
        cycle, attempt_no = prev["cycle"] + 1, 1
    else:
        cycle, attempt_no = 1, 1

    ev = fetch_production_evidence(job_id)
    ver = fetch_fresh_verification(job_id)
    if (ev is None or not ev.get("head_sha")) or ver is None:
        # bridge hiccup right now — bounded retry inside this runner
        for _ in range(3):
            time.sleep(20)
            if ev is None or not ev.get("head_sha"):
                ev = fetch_production_evidence(job_id)
            if ver is None:
                ver = fetch_fresh_verification(job_id)
            if ev and ev.get("head_sha") and ver:
                break
        if ev is None or not ev.get("head_sha") or ver is None:
            _interrupt_review_dispatch(job_id)
            return "REVIEW_INTERRUPTED"
    # fresh binding wins: the review always binds the CURRENT exact revision
    ev = merge_review_evidence(ev, ver)

    while True:  # bounded attempt loop for the current cycle
        if jobs_db.cancel_requested(job_id):
            return "CANCELLED"
        rid = jobs_db.create_review(
            job_id, cycle, REVIEWER_AGENT, REVIEWER_MODEL,
            base_sha=ev.get("base_sha"), target_sha=ev.get("head_sha"),
            diff_hash=ev.get("diff_sha256"), content_sha=ev.get("content_sha256"),
            attempt_no=attempt_no)
        ev_pkg = dict(ev)
        diff = fetch_review_diff(job_id)
        if diff is not None:
            ev_pkg["review_diff"] = diff
        package = rv.build_review_package(job, ev_pkg, {"cycle": cycle})
        if attempt_no > 1:
            package += ("\n\n# REPAIR NOTE\nYour previous output could not be "
                        "validated. Write ONLY the valid JSON object to "
                        "/jobs/review-result.json (no markdown fences, no "
                        "commentary, no extra keys).\n")
        resp = _bridge({"version": 1, "action": "review_start",
                        "job_id": job_id, "review_id": rid, "cycle": cycle,
                        "repo_id": job["repository"],
                        "target_sha": ev.get("head_sha"),
                        "base_sha": ev.get("base_sha"),
                        "prompt": package[:100_000],
                        "timeout_s": review_timeout}, timeout=180)
        if resp.get("error"):
            if resp.get("error") == "busy":
                # combined concurrency=1: wait bounded, then interrupted
                waited = 0
                ok = None
                while waited < 120:
                    time.sleep(20); waited += 20
                    try:
                        ok = _bridge({"version": 1, "action": "review_start",
                                      "job_id": job_id, "review_id": rid,
                                      "cycle": cycle, "repo_id": job["repository"],
                                      "target_sha": ev.get("head_sha"),
                                      "base_sha": ev.get("base_sha"),
                                      "prompt": package[:100_000],
                                      "timeout_s": review_timeout}, timeout=180)
                    except (OSError, ValueError, WorkerUnconfigured):
                        ok = None
                    if ok and not ok.get("error"):
                        break
                if not (ok and not ok.get("error")):
                    _mark_review_interrupted(job_id, rid)
                    return "REVIEW_INTERRUPTED"
                resp = ok
            else:
                _review_fail(job_id, rid,
                             f"bridge rejected review_start: {resp.get('error')}")
                return "WAITING_HUMAN"

        # ── poll the reviewer run (H3-style bounded poll) ────────────────
        deadline = time.time() + review_timeout + 600
        state = "running"
        while time.time() < deadline:
            if jobs_db.cancel_requested(job_id):
                try:
                    _bridge({"version": 1, "action": "review_cancel",
                             "job_id": job_id, "review_id": rid}, timeout=60)
                except (OSError, ValueError, WorkerUnconfigured):
                    pass
                _review_cancelled(job_id, rid)
                return "CANCELLED"
            try:
                st = _bridge({"version": 1, "action": "review_status",
                              "job_id": job_id, "review_id": rid}, timeout=30)
                state = st.get("state")
            except (OSError, ValueError, WorkerUnconfigured):
                state = "running"
                continue
            if state not in ("running", None):
                break
            time.sleep(15)
        if state == "running":
            try:
                _bridge({"version": 1, "action": "review_cancel",
                         "job_id": job_id, "review_id": rid}, timeout=60)
            except (OSError, ValueError, WorkerUnconfigured):
                pass
            _mark_review_interrupted(job_id, rid)
            return "REVIEW_INTERRUPTED"

        # ── collect + validate the structured verdict ────────────────────
        try:
            res = _bridge({"version": 1, "action": "review_result",
                           "job_id": job_id, "review_id": rid}, timeout=60)
        except (OSError, ValueError, WorkerUnconfigured) as exc:
            _mark_review_interrupted(job_id, rid)
            return "REVIEW_INTERRUPTED"
        payload = res.get("review") or {}
        raw = payload.get("result_json")
        data = raw if isinstance(raw, dict) else rv.parse_review_text(raw)
        normalized, err = rv.validate_review_output(data, repo_scope_dirs=None)
        usage = payload.get("usage")
        _review_usage_log(job_id, cycle, usage)
        if err:
            # record the invalid attempt in the immutable history, then
            # either repair-retry once or fail safe (section 40)
            jobs_db.update_review(rid, status="failed",
                                  error_summary=f"invalid reviewer output: {err}"[:300])
            jobs_db.log(job_id, None, "review",
                        f"review output invalid review_id={rid} "
                        f"attempt={attempt_no}: {err}"[:200])
            if attempt_no >= MAX_INVALID_ATTEMPTS:
                _review_fail(job_id, rid, f"invalid reviewer output: {err}")
                return "WAITING_HUMAN"
            attempt_no += 1
            continue  # one strict repair retry (section 40)
        if payload.get("snapshot_hash_mismatch"):
            _review_fail(job_id, rid,
                         "review snapshot does not match the approved evidence "
                         "binding (integrity check failed)")
            return "WAITING_HUMAN"

        findings = normalized["findings"]
        fps = rv.fingerprint_set(findings)
        verdict = normalized["verdict"]
        content_progress = _content_progress(job_id, rid, ev.get("content_sha256"))
        jobs_db.update_review(
            rid, status=("approved" if verdict == "APPROVE" else "changes_required"),
            verdict=verdict, summary=normalized["summary"],
            findings=findings, blocking_count=normalized["blocking_count"],
            fingerprints=fps, usage=usage, content_progress=content_progress,
            finished_at=jobs_db.now_iso())
        try:
            _bridge({"version": 1, "action": "review_cleanup",
                     "job_id": job_id, "review_id": rid}, timeout=60)
        except (OSError, ValueError, WorkerUnconfigured):
            pass  # snapshot cleanup is best-effort

        if verdict == "APPROVE":
            jobs_db.update_job(job_id, review_status="APPROVED", needs_human=0)
            jobs_db.log(job_id, None, "review",
                        f"review approved cycle={cycle} review_id={rid} "
                        f"model={REVIEWER_MODEL} target_sha="
                        f"{(ev.get('head_sha') or '')[:12]} "
                        f"blocking=0 nonblocking={normalized['nonblocking_count']} "
                        f"content_progress={content_progress}")
            if content_progress == "unchanged":
                # section 61 vs 107: an APPROVE bound to the SAME content as the
                # previous verdicted cycle means the implementation did not
                # change — the reviewer simply changed its mind. That is a legal
                # convergence path (section 107 allows the implementation to
                # rebut a false-positive finding), but it is NOT evidence of a
                # real fix loop and must never be reported as one.
                jobs_db.log(job_id, None, "review",
                            f"review approved on UNCHANGED content binding "
                            f"(cycle={cycle}): reviewer-only convergence, not "
                            f"fix-loop evidence")
            return "APPROVED_PRESENT"

        # CHANGES_REQUIRED
        jobs_db.update_job(job_id, review_status="CHANGES_REQUIRED")
        jobs_db.log(job_id, None, "review",
                    f"review changes_required cycle={cycle} review_id={rid} "
                    f"blocking={normalized['blocking_count']} "
                    f"nonblocking={normalized['nonblocking_count']}")

        # section 64/108: repeated identical blocking fingerprints with no
        # content or acceptance progress must stop the loop
        prev_cr = jobs_db.latest_review(job_id, statuses=("changes_required",))
        prev_rows = [r for r in jobs_db.reviews_for_job(job_id)
                     if r["status"] == "changes_required" and r["review_id"] != rid]
        if prev_rows:
            prev_fps = set(prev_rows[-1].get("fingerprints") or [])
            same_fps = prev_fps and prev_fps.issubset(set(fps))
            no_content_progress = (
                prev_rows[-1].get("content_sha") and
                prev_rows[-1]["content_sha"] == ev.get("content_sha256"))
            no_acceptance_progress = (prev_rows[-1].get("blocking_count", 0) ==
                                      normalized["blocking_count"])
            if same_fps and no_content_progress and no_acceptance_progress:
                jobs_db.transition(job_id, "WAITING_HUMAN",
                                   failure_reason="review_no_progress_stalled "
                                                  "(same blocking findings, no "
                                                  "code or acceptance progress)")
                jobs_db.update_job(job_id, needs_human=1,
                                   next_action="human arbitration of repeated "
                                               "review findings")
                return "WAITING_HUMAN"

        jobs_db.transition(job_id, "CONTINUE_REQUIRED")
        jobs_db.update_job(job_id, needs_human=0,
                           next_action=f"fix blocking review findings (cycle {cycle})")
        jobs_db.log(job_id, None, "review",
                    f"fix_dispatched cycle={cycle} blocking="
                    f"{normalized['blocking_count']}")
        return "FIX_REQUIRED"


def _content_progress(job_id, review_id, content_sha):
    """Section 61/107: did the implementation actually move since the previous
    verdicted review cycle?

    Returns 'first_cycle' (no earlier verdict), 'changed' (the canonical
    content binding differs from the previous verdicted cycle) or 'unchanged'
    (byte-identical canonical changed set). Recorded as review evidence — it is
    deliberately NOT enforced, because section 107 allows the implementation to
    rebut a false-positive finding and let a fresh review decide. Its purpose is
    that a cycle which converged WITHOUT a code change can never be mistaken for
    section 104 real-fix-loop evidence."""
    prior = [r for r in jobs_db.reviews_for_job(job_id)
             if r["review_id"] != review_id
             and r["status"] in ("approved", "changes_required")]
    if not prior:
        return "first_cycle"
    prev_sha = prior[-1].get("content_sha")
    if not prev_sha or not content_sha:
        return "unknown"
    return "unchanged" if prev_sha == content_sha else "changed"


def _interrupt_review_dispatch(job_id):
    jobs_db.update_job(job_id, review_status="REVIEW_INTERRUPTED",
                       next_action="job_review to retry the review",
                       needs_human=0)
    jobs_db.log(job_id, None, "review",
                "review dispatch interrupted (bridge unreachable); job preserved")


def completion_decision(job_id):
    """Section 55: THE single canonical final completion gate.

    COMPLETED requires ALL of:
      cancel not requested
      job.status == IMPLEMENTATION_ACCEPTED
      review_status == APPROVED + a valid bound review record (verdict APPROVE)
      approved target SHA == current target SHA   (exact revision, section 50)
      approved content binding == current content binding (section 51/90)
      approved diff hash == current diff hash      (section 53)
      acceptance decision PASS + fresh registered tests PASS (final validation)
      canonical secret scan PASS
    The write itself is a CAS from IMPLEMENTATION_ACCEPTED (section 54)."""
    job = jobs_db.get_job(job_id)
    if not job:
        return (False, "not_found", "")
    if jobs_db.cancel_requested(job_id):
        return (False, "cancelled", "")
    if job["status"] != "IMPLEMENTATION_ACCEPTED":
        return (False, "not_accepted", str(job["status"]))
    if (job.get("review_status") or "") != "APPROVED":
        return (False, "no_approval", str(job.get("review_status")))
    rec = jobs_db.latest_approved_review(job_id)
    if not rec:
        return (False, "no_review_record", "")
    if rec.get("verdict") != "APPROVE" or rec.get("status") != "approved":
        return (False, "invalid_review_record", str(rec.get("verdict")))
    if not rec.get("target_sha") or not rec.get("content_sha"):
        return (False, "unbound_review_record", "")
    try:
        ver = transport.bridge_request(
            {"version": 1, "action": "job_verify", "job_id": job_id}, timeout=900)
    except (OSError, ValueError, WorkerUnconfigured) as exc:
        return (False, "verification_unavailable", str(exc)[:200])
    if ver.get("error"):
        return (False, "verification_failed", str(ver.get("error"))[:200])
    if (ver.get("head_sha") or "") != rec["target_sha"]:
        return (False, "stale_target_sha", "")
    if (ver.get("content_sha256") or "") != rec["content_sha"]:
        return (False, "stale_content_binding", "")
    # Section 53: the approved diff hash must equal the current diff hash.
    # NOTE: for the Pi worker topology the agent does not commit, so the
    # committed diff (base...HEAD) is empty and this hash is the same constant
    # on every row. content_sha256 above is the STRICTLY STRONGER binding and is
    # the operative one; this check exists so the literal section-53 condition
    # holds and so the gate stays correct for a worker that DOES commit.
    if rec.get("diff_hash") and (ver.get("diff_sha256") or "") != rec["diff_hash"]:
        return (False, "stale_diff_hash", "")
    if ver.get("secret_scan"):
        return (False, "secret_scan_hits", ",".join(ver["secret_scan"]))
    tstat = ((ver.get("tests") or {}).get("status")) or "NOT_RUN"
    if not acceptance_decision(job.get("acceptance_results") or [], tstat):
        return (False, "final_validation_unmet", f"tests={tstat}")
    return (True, "ok", "")


def finalize_completion(job_id):
    """Run the canonical gate and, only on full PASS, CAS the job to
    COMPLETED. A stale/incomplete approval is invalidated back to
    WAITING_REVIEW so a fresh review cycle runs (section 51)."""
    jobs_db.log(job_id, None, "review", "final_validation_started")
    ok, why, detail = completion_decision(job_id)
    if not ok:
        if why in ("stale_target_sha", "stale_content_binding", "stale_diff_hash"):
            jobs_db.update_job(job_id, review_status="WAITING_REVIEW",
                               next_action="approval stale: target changed "
                                           "after approval; fresh review required",
                               needs_human=0)
            jobs_db.log(job_id, None, "review",
                        f"approval invalidated ({why}); fresh review required")
            return "REVIEW_AGAIN"
        if why == "cancelled":
            return "CANCELLED"
        jobs_db.transition(job_id, "WAITING_HUMAN",
                           failure_reason=f"final completion gate: {why} {detail}"[:300])
        jobs_db.update_job(job_id, needs_human=1,
                           next_action="resolve final gate failure")
        return "WAITING_HUMAN"
    if jobs_db.cas_status(job_id, "COMPLETED", ("IMPLEMENTATION_ACCEPTED",)):
        jobs_db.log(job_id, None, "review", "completed (final gate passed, "
                    "exact revision approved, CAS commit)")
        return "COMPLETED"
    return "COMPLETION_RACE_LOST"


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
    except OSError:
        # e.g. invalid-parameter noise on non-POSIX hosts; treat as dead
        # (fail-safe: reconcile only marks recoverable states)
        return False
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
    """Crash recovery for both transports (never marks jobs COMPLETED).

    F.2 (section 69/70): review-aware — an IMPLEMENTATION_ACCEPTED job whose
    review runner died while REVIEWING becomes REVIEW_INTERRUPTED (job
    preserved, bounded retry via job_review); the reviewer container on the
    bridge keeps finishing on its own and its verdict is still collected by
    the retry."""
    out = []
    for job in jobs_db.list_jobs(limit=100):
        # review-aware reconcile (checked first; the job status itself stays
        # IMPLEMENTATION_ACCEPTED while a review is in flight)
        if job["status"] == "IMPLEMENTATION_ACCEPTED" and \
                (job.get("review_status") or "") == "REVIEWING":
            pid, start_ticks = read_runner_pid_file(job["job_id"])
            alive = False
            if pid is not None:
                try:
                    alive = _pid_alive(pid, start_ticks)
                except (ProcessLookupError, PermissionError):
                    alive = False
            if alive:
                out.append({"job_id": job["job_id"], "was": "REVIEWING(review)",
                            "now": "REVIEWING (live review runner)"})
                continue
            jobs_db.update_job(job["job_id"], review_status="REVIEW_INTERRUPTED",
                               next_action="job_review to resume the interrupted review",
                               needs_human=0)
            log(job["job_id"], None, "review",
                "review interrupted by supervisor restart (runner dead); "
                "job preserved")
            out.append({"job_id": job["job_id"], "was": "REVIEWING(review)",
                        "now": "REVIEW_INTERRUPTED (runner dead; job preserved)"})
            continue
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
    """The detached job driver: implementation attempts + F.2 review +
    exact-revision completion, until terminal or stopped."""
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

    prev_signature = None
    strikes = job.get("no_progress_strikes", 0)
    continuation_reason = None
    validation = None

    while True:
        job = jobs_db.get_job(job_id)
        if job["status"] in ("CANCELLED", "WAITING_HUMAN", "COMPLETED",
                             "INTERRUPTED"):
            break
        if job["status"] == "IMPLEMENTATION_ACCEPTED":
            # F.2: independent review + exact-revision completion gate.
            # (The legacy COMPLETED-without-review path no longer exists;
            # section 110 guard lives in completion_decision().)
            outcome = run_review_phase(job_id, cfg)
            log(job_id, None, "review", f"review phase outcome: {outcome}")
            if outcome == "COMPLETED":
                break
            if outcome in ("APPROVED_PRESENT", "REVIEW_AGAIN"):
                # approval recorded (or re-established): run the final gate
                gout = finalize_completion(job_id)
                if gout == "COMPLETED":
                    break
                if gout in ("REVIEW_AGAIN", "COMPLETION_RACE_LOST"):
                    continue  # bounded: cycle budget inside run_review_phase
                break
            if outcome == "FIX_REQUIRED":
                pass  # fall through to a fix attempt below
            else:
                break  # WAITING_HUMAN / NOT_REQUIRED / REVIEW_INTERRUPTED /
                       # REVIEW_BUSY / CANCELLED / NOT_FOUND

        # ── implementation attempt setup (idempotent; also valid when the
        # runner was spawned straight into a fix attempt after CHANGES_REQUIRED)
        try:
            adapter = transport.get_adapter(job)
            rcfg = repo_cfg(job["repository"])
        except WorkerUnconfigured as exc:
            jobs_db.transition(job_id, "WAITING_HUMAN", failure_reason=str(exc))
            jobs_db.update_job(job_id, needs_human=1)
            break
        except ValueError as exc:
            jobs_db.transition(job_id, "FAILED", failure_reason=str(exc))
            break

        repo_path = Path(rcfg["path"]) \
            if (adapter.name == "local-fixture" and Path(rcfg["path"]).exists()) else None
        agent = job.get("agent") or cfg.get("default_agent") or "simulated"
        if adapter.name == "local-fixture" and not rcfg.get("allow_simulated"):
            jobs_db.transition(job_id, "FAILED",
                               failure_reason="fixture agent not allowed for this repository")
            break

        if repo_path is not None and not job["git_before_sha"]:
            # first dispatch of this job only: record the comparison base,
            # gate on a dirty tree, and set up the job branch (M7 fail-closed)
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
        # the comparison base for this attempt (stored at first dispatch;
        # None for sandbox/remote transports — the worker records its own)
        before_sha = jobs_db.get_job(job_id)["git_before_sha"]
        jobs_db.transition(job_id, "DISPATCHING")

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
        # F.2 (section 57/59): after CHANGES_REQUIRED the fix attempt gets the
        # structured blocking findings — never the reviewer conversation.
        review_fix = None
        if (job.get("review_status") or "") == "CHANGES_REQUIRED":
            review_fix = jobs_db.latest_review(job_id,
                                               statuses=("changes_required",))
        prompt = build_prompt(job, rcfg, prev_run=prev_run, validation=validation,
                              behavior=behavior, review_fix=review_fix)
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

        # usage metrics (F.1.1-Pi addendum): structured JSON lives in the
        # bridge evidence + per-attempt usage files; a machine-parseable
        # summary lands in event_log for SQL aggregation. No secrets here.
        usage = result.get("usage")
        if isinstance(usage, dict) and usage.get("attempts"):
            jobs_db.log(job_id, run_id, "usage",
                        "usage in={i} out={o} reasoning={r} total={t} "
                        "api_calls={c} cost_usd={u} attempts={n}".format(
                            i=usage.get("input_tokens"), o=usage.get("output_tokens"),
                            r=usage.get("reasoning_tokens"), t=usage.get("total_tokens"),
                            c=usage.get("api_calls"),
                            # section 76: never write an invented cost
                            u=(usage.get("estimated_cost_usd")
                               if usage.get("cost_status") == "provider_reported"
                               else "unavailable"),
                            n=len(usage.get("attempts") or [])))

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
            # F.2: review is now QUEUED (mandatory for production jobs); the
            # review phase runs in the next loop iteration of this runner.
            jobs_db.update_job(job_id, review_status="WAITING_REVIEW",
                               next_action="independent review (Phase F.2)",
                               needs_human=0, no_progress_strikes=0,
                               result_summary=summary)
            jobs_db.log(job_id, run_id, "review",
                        "review_queued (IMPLEMENTATION_ACCEPTED; "
                        "review_status=WAITING_REVIEW)")
            log(job_id, run_id, "info",
                "deterministic acceptance satisfied -> IMPLEMENTATION_ACCEPTED "
                "(review queued)")
            continue

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
