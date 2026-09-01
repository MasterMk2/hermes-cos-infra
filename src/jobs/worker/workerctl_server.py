#!/usr/bin/env python3
"""workerctl — Coding Worker control plane (Phase F.1.1-Pi.2).

Runs ON the worker machine as the dedicated low-privilege `hermes-worker`
account. Hermes reaches it ONLY over restricted SSH (forced command), never
through a generic shell.

Invocation:
  workerctl request            # SSH forced-command entry (SSH_ORIGINAL_COMMAND)
  workerctl <subcommand> ...   # local/test usage

Subcommands (the complete protocol surface — nothing else is executed):
  status                      worker online / agent / registered repo ids
  submit -                    package on stdin (JSON) -> detached job
  job_status <job_id>         state only (running/finished/failed/interrupted)
  job_result <job_id>         evidence JSON (bounded, no bulk source)
  job_diff  <job_id> [bytes]  bounded unified diff (binary -> metadata only)
  job_cancel <job_id>         cooperative cancel
  job_continue <job_id>       mark a finished job resumable (Hermes re-submits)
  selftest                    isolation/escape self-checks (no agent call)

Security model:
  - worker root: ~/hermes-worker/ (repos, worktrees, jobs, logs, bin, config)
  - repositories only via allowlist in config/worker.json (repo_id -> path);
    Hermes can never pass a path/cwd
  - per-job git worktree + dedicated branch; registered checkouts untouched
  - agent runs NON-INTERACTIVELY without approval-bypass flags; the command
    template is human-configured in config/worker.json
  - deterministic secret scan of the diff before evidence is returned
  - changed-file validation: any path escaping the worktree (../, absolute,
    symlinked out) is rejected and flagged

F.1.1-Pi.2 hardening:
  - M4: internal commands (_run_job) are rejected on the SSH surface —
    SSH_ORIGINAL_COMMAND present => internal subcommands forbidden
  - H1: test_command criteria PASS only when the allowlisted test ran (exit 0)
  - H2: worktree base_sha recorded; canonical changed set = committed diff
    vs base UNION porcelain (staged/unstaged/untracked)
  - H4: finalization never overwrites 'cancelled' (terminal intent)
  - Item 46: state timestamps are UTC ISO-8601
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# canonical secret-pattern module must sit next to workerctl (deployment:
# worker/README.md step 2) or on PYTHONPATH — pattern drift is forbidden.
from security_patterns import SECRET_PATTERNS, scan_text, is_env_file

WORKER_ROOT = Path(
    os.environ.get("HERMES_WORKER_ROOT", Path.home() / "hermes-worker")
)
CONFIG_PATH = WORKER_ROOT / "config" / "worker.json"
JOBS_DIR = WORKER_ROOT / "jobs"
LOGS_DIR = WORKER_ROOT / "logs"
WORKTREES_DIR = WORKER_ROOT / "worktrees"

# M4: subcommands that only the local detached runner may invoke.
INTERNAL_COMMANDS = {"_run_job"}

# Item 43: bounded artifact reads for criteria evaluation.
ARTIFACT_MAX_BYTES = 1_000_000
DIFF_MAX_BYTES = 2_000_000


def load_config():
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not WORKER_ROOT.exists():
        raise SystemExit("worker root missing; run setup (worker/README.md)")
    return cfg


def out(obj):
    print(json.dumps(obj, ensure_ascii=False))


def _git(worktree, *args):
    return subprocess.run(
        ["git", *args], cwd=str(worktree), capture_output=True, text=True, timeout=120,
    )


def _safe_worktree_path(job_id, rel):
    wt = (WORKTREES_DIR / job_id).resolve()
    if not rel or Path(rel).is_absolute() or ".." in Path(rel).parts:
        raise ValueError(f"unsafe path from agent: {rel!r}")
    p = (wt / rel).resolve()
    if not str(p).startswith(str(wt)):
        raise ValueError(f"path escapes worktree: {rel!r}")
    return p


def now_iso():
    """Item 46: UTC ISO-8601 everywhere (no epoch floats in new writes)."""
    return datetime.now(timezone.utc).isoformat()


def parse_porcelain_z(data):
    """Item 44: NUL-separated porcelain parsing. Rename entries: first field
    is the destination path (kept), second is the original (skipped)."""
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
            i += 1
        files.add(path)
    return files


def canonical_changed_files(worktree, base_sha):
    """H2 canonical changed set: committed diff (base...HEAD) UNION
    working-tree changes (staged/unstaged/untracked). Raises on git failure
    (fail-closed — never treat unknown git state as 'no changes')."""
    files = set()
    if base_sha:
        r = _git(worktree, "diff", "--name-only", "-z", f"{base_sha}...HEAD")
        if r.returncode != 0:
            raise RuntimeError(f"git diff vs base failed: {(r.stderr or '')[:200]}")
        files |= {f for f in (r.stdout or "").split("\0") if f}
    st = _git(worktree, "status", "--porcelain", "-z")
    if st.returncode != 0:
        raise RuntimeError(f"git status failed: {(st.stderr or '')[:200]}")
    files |= parse_porcelain_z(st.stdout or "")
    return sorted(files)


def validate_changed_within_worktree(job_id, files):
    """Reject any change that escapes the worktree (../, absolute, symlink)."""
    rejected = []
    for f in files:
        try:
            _safe_worktree_path(job_id, f)
        except ValueError as exc:
            rejected.append(str(exc))
    return rejected


def secret_scan(worktree, files, max_bytes=200_000):
    hits = set()
    for f in files:
        if is_env_file(f):
            hits.add("env_file_added")
            continue
        p = (Path(worktree) / f)
        try:
            if p.resolve().is_file() and not p.is_symlink():
                text = p.read_text(encoding="utf-8", errors="replace")[:max_bytes]
                hits |= scan_text(text)
        except OSError:
            continue
    return sorted(hits)


def evaluate_criteria(criteria, repo_cfg, worktree, tests_result=None,
                      changed_files=None, artifact_max_bytes=ARTIFACT_MAX_BYTES):
    """Deterministic checks (same kinds as the supervisor validator).

    H1: kind=test_command passes ONLY when `tests_result` shows the
    allowlisted test was executed and exited 0. Item 43: artifact_contains
    reads are bounded (artifact_too_large). git_scope uses the canonical
    changed set (H2)."""
    results = []
    for ac in criteria:
        kind = ac.get("verification", {}).get("kind")
        params = ac.get("verification", {}).get("params", {})
        ok, detail = False, f"unknown kind {kind}"
        try:
            if kind == "file_exists":
                p = _wt_path(worktree, params.get("path"))
                ok, detail = p.is_file(), f"exists={p.is_file()}"
            elif kind == "function_exists":
                p = _wt_path(worktree, params.get("file"))
                import ast
                if p.is_file():
                    tree = ast.parse(p.read_text(encoding="utf-8"))
                    names = {n.name for n in ast.walk(tree)
                             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
                    ok, detail = params.get("name") in names, \
                        f"{'found' if params.get('name') in names else 'missing'}: {params.get('name')}"
                else:
                    detail = f"file missing: {params.get('file')}"
            elif kind == "git_scope":
                cf = changed_files if changed_files is not None \
                    else canonical_changed_files(worktree, None)
                allowed = repo_cfg.get("allowed_paths") or []
                extra = [f for f in cf if not any(f.startswith(a) for a in allowed)]
                ok, detail = not extra, (f"changed={len(cf)} extra={extra}" if extra
                                         else f"changed={len(cf)} all in scope")
            elif kind == "artifact_contains":
                p = _wt_path(worktree, params.get("path"))
                if not p.is_file():
                    ok, detail = False, "file missing"
                elif p.stat().st_size > artifact_max_bytes:
                    ok, detail = False, f"artifact_too_large (> {artifact_max_bytes} bytes)"
                else:
                    ok = params.get("text", "") in \
                        p.read_text(encoding="utf-8", errors="replace")[:artifact_max_bytes]
                    detail = f"contains={ok}"
            elif kind == "test_command":
                # H1: executed allowlisted test with exit 0 — or FAIL.
                tstat = (tests_result or {}).get("status") or "NOT_RUN"
                ok, detail = (tstat == "PASS"), f"tests={tstat}"
        except (ValueError, SyntaxError, OSError, RuntimeError) as exc:
            ok, detail = False, f"error: {exc}"
        results.append({"id": ac.get("id"), "description": ac.get("description"),
                        "verification": kind, "status": "PASS" if ok else "FAIL",
                        "detail": detail})
    return results


def _wt_path(worktree, rel):
    if not rel or Path(rel).is_absolute() or ".." in Path(rel).parts:
        raise ValueError(f"unsafe path: {rel!r}")
    p = (Path(worktree) / rel).resolve()
    if not str(p).startswith(str(Path(worktree).resolve())):
        raise ValueError(f"path escapes worktree: {rel!r}")
    return p


def worktree_job_id(worktree):
    return Path(worktree).name


def run_tests(worktree, command, allowlist, timeout_s):
    if not command or command not in (allowlist or []):
        return {"status": "BLOCKED", "summary": "test command not in repository allowlist"}
    try:
        p = subprocess.run(command, shell=True, cwd=str(worktree),
                           capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return {"status": "TIMEOUT", "summary": f"tests exceeded {timeout_s}s"}
    out = (p.stdout or "") + (p.stderr or "")
    lines = [l for l in out.splitlines() if l.strip()]
    return {"status": "PASS" if p.returncode == 0 else "FAIL",
            "summary": "\n".join(lines[-12:])[:1500], "exit_code": p.returncode}


def job_state_path(job_id):
    return JOBS_DIR / f"{job_id}.json"


def read_state(job_id):
    p = job_state_path(job_id)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def write_state(job_id, state):
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    job_state_path(job_id).write_text(
        json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def run_job_detached(job_id):
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    log = (LOGS_DIR / f"job-{job_id}.log").open("ab")
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "_run_job", job_id],
        stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
    )
    (LOGS_DIR / f"job-{job_id}.pid").write_text(str(proc.pid))


def _is_cancelled_state(st):
    """H4: cancellation is terminal intent."""
    return bool(st) and (st.get("state") == "cancelled"
                         or bool(st.get("cancel_requested_at")))


def _commit_terminal_state(job_id, state, new_state, error=None):
    """H4 CAS-style final write: re-reads persisted state; a cancel that
    landed while the runner was finishing is NEVER overwritten."""
    cur = read_state(job_id) or {}
    if _is_cancelled_state(cur) or _is_cancelled_state(state):
        state["state"] = "cancelled"
        state.setdefault("cancel_requested_at",
                         cur.get("cancel_requested_at") or now_iso())
    else:
        state["state"] = new_state
    if error:
        state["error"] = error
    state["finished_at"] = now_iso()
    write_state(job_id, state)
    return state["state"]


def _run_job(job_id, package):
    cfg = load_config()
    repo_cfg = cfg["repositories"][package["repo_id"]]
    repo_path = Path(repo_cfg["path"]).resolve()
    worktree = WORKTREES_DIR / job_id
    state = read_state(job_id) or {"job_id": job_id, "state": "running",
                                   "created_at": now_iso()}
    state["state"] = "running"
    write_state(job_id, state)
    try:
        # H2: base_sha is resolved once, at worktree creation, and stored in
        # job state — later HEAD moves never change the comparison base.
        base_sha = state.get("base_sha")
        if not worktree.exists():
            # M7 fail-closed: refuse to run when git state is unknown
            r = subprocess.run(["git", "-C", str(repo_path), "rev-parse", "HEAD"],
                               capture_output=True, text=True, timeout=120)
            base_sha = (r.stdout or "").strip()
            if r.returncode != 0 or not base_sha:
                state["state"] = "failed"
                state["error"] = ("git state unknown; worktree not created "
                                  f"({(r.stderr or 'no output')[:200]})")
                write_state(job_id, state)
                return
            branch = f"hermes/job-{job_id}"
            subprocess.run(
                ["git", "-C", str(repo_path), "worktree", "add", "-b", branch,
                 str(worktree)],
                capture_output=True, text=True, timeout=120, check=True)
            state["base_sha"] = base_sha
            write_state(job_id, state)
        if base_sha is None:
            base_sha = state.get("base_sha")
        # dirty worktree from a previous attempt: refuse (no auto reset)
        if canonical_changed_files(worktree, base_sha) and state.get("evidence"):
            state["state"] = "failed"
            state["error"] = "worktree dirty from previous attempt"
            write_state(job_id, state)
            return

        prompt_file = JOBS_DIR / f"{job_id}-prompt.txt"
        prompt_file.write_text(package["prompt"], encoding="utf-8")
        agent_cfg = cfg.get("agent", {})
        template = agent_cfg["command_template"]
        timeout_s = int(package.get("timeout_s") or 1200)
        cmd = template.format(prompt_file=prompt_file, worktree=worktree)
        try:
            proc = subprocess.run(cmd, shell=True, cwd=str(worktree),
                                  capture_output=True, text=True, timeout=timeout_s)
            exit_status, timed_out = proc.returncode, False
            agent_out = (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired as exc:
            exit_status, timed_out = None, True
            o = exc.stdout or b""
            agent_out = (o.decode("utf-8", "replace") if isinstance(o, bytes) else o)

        # H2: canonical set — commits by the agent stay visible
        files = canonical_changed_files(worktree, base_sha)
        rejected = validate_changed_within_worktree(job_id, files)
        scans = secret_scan(worktree, files)
        tests = run_tests(worktree, package.get("tests_command"),
                          repo_cfg.get("test_commands"), 600)
        # H1: criteria evaluated against the actual allowlisted test run
        criteria_results = evaluate_criteria(
            package.get("acceptance_criteria") or [], repo_cfg, worktree,
            tests_result=tests, changed_files=files)
        diff_stat = _git(worktree, "diff", "--stat", "HEAD", "--", ".").stdout[:2000]
        head = _git(worktree, "rev-parse", "HEAD")
        d = _git(worktree, "diff", f"{base_sha}...HEAD")
        diff_hash = hashlib.sha256(
            (d.stdout or "").encode("utf-8", "replace")[:DIFF_MAX_BYTES]
        ).hexdigest()[:16] if base_sha else None
        evidence = {
            "job_id": job_id,
            "repo_id": package["repo_id"],
            "branch": f"hermes/job-{job_id}",
            "worktree_id": job_id,
            "worktree_path": str(worktree),
            "base_sha": base_sha,
            "head_sha": head.stdout.strip() if head.returncode == 0 else None,
            "exit_status": exit_status,
            "timed_out": timed_out,
            "agent_reason": "\n".join(
                [l for l in agent_out.splitlines() if l.strip()][-10:])[:2000],
            "changed_files": files,
            "changed_files_rejected": rejected,
            "diff_stat": diff_stat,
            "diff_sha256": diff_hash,
            "test_summary": {"status": tests["status"], "summary": tests["summary"],
                             "criteria_results": criteria_results},
            "secret_scan": scans,
            "worktree_escape_rejected": bool(rejected),
            "finished_at": now_iso(),
        }
        state["evidence"] = evidence
        if rejected:
            _commit_terminal_state(job_id, state, "failed",
                                   error="worktree escape detected")
        else:
            # H4: 'finished_timed_out' only when not cancelled
            _commit_terminal_state(job_id, state,
                                   "finished" if not timed_out else "finished_timed_out")
    except Exception as exc:  # noqa: BLE001 — worker durability: record, never crash silently
        _commit_terminal_state(job_id, state, "failed",
                               error=f"{type(exc).__name__}: {exc}")


def cmd_status():
    cfg = load_config()
    agent = cfg.get("agent", {}).get("name")
    active = 0
    if JOBS_DIR.exists():
        for p in JOBS_DIR.glob("*.json"):
            try:
                if json.loads(p.read_text()).get("state") == "running":
                    active += 1
            except Exception:
                pass
    out({"state": "online", "agent": agent,
         "repositories": sorted(cfg.get("repositories", {}).keys()),
         "active_jobs": active})


def cmd_submit(package):
    cfg = load_config()
    job_id = package.get("job_id")
    repo_id = package.get("repo_id")
    if not job_id or repo_id not in cfg.get("repositories", {}):
        out({"error": "bad_package_or_unknown_repo_id"})
        return
    st = read_state(job_id)
    if st and _is_cancelled_state(st):
        # H4: a cancelled job is not silently re-accepted
        out({"accepted": False, "job_id": job_id, "state": "cancelled"})
        return
    if st and st.get("state") == "running":
        out({"accepted": False, "job_id": job_id, "state": "already_running"})
        return
    attempt = (st or {}).get("attempt", 0) + 1
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    (JOBS_DIR / f"{job_id}-package.json").write_text(
        json.dumps(package, ensure_ascii=False), encoding="utf-8")
    write_state(job_id, {"job_id": job_id, "state": "queued",
                         "attempt": attempt, "created_at": now_iso()})
    run_job_detached(job_id)
    out({"accepted": True, "job_id": job_id, "state": "running", "attempt": attempt})


def cmd_job_status(job_id):
    st = read_state(job_id)
    out({"job_id": job_id, "state": st.get("state") if st else "unknown",
         "worktree_id": st.get("evidence", {}).get("worktree_id") if st else None})


def cmd_job_result(job_id):
    st = read_state(job_id)
    if not st:
        out({"error": "unknown_job"})
        return
    ev = st.get("evidence")
    if ev:
        ev = {**ev, "diff_stat": (ev.get("diff_stat") or "")[:2000]}
    out({"job_id": job_id, "state": st.get("state"), "error": st.get("error"),
         "evidence": ev})


def cmd_job_diff(job_id, max_bytes=100_000):
    st = read_state(job_id)
    if not st or not st.get("evidence"):
        out({"error": "unknown_job"})
        return
    worktree = st["evidence"].get("worktree_path")
    p = subprocess.run(["git", "diff", "HEAD", "--", "."], cwd=worktree,
                       capture_output=True, text=True, timeout=60)
    diff = p.stdout
    binary = "\x00" in diff[:8000]
    out({"job_id": job_id, "binary": binary,
         "truncated": len(diff.encode()) > max_bytes,
         "diff": "" if binary else diff[:max_bytes]})


def cmd_job_cancel(job_id):
    st = read_state(job_id)
    if not st:
        out({"error": "unknown_job"})
        return
    if st.get("state") == "running":
        pidf = LOGS_DIR / f"job-{job_id}.pid"
        if pidf.exists():
            try:
                os.kill(int(pidf.read_text().strip()), 15)
            except (ValueError, ProcessLookupError, PermissionError):
                pass
    # H4: terminal intent, stamped persistently — the detached runner's
    # finalize step can no longer write finished/failed over this.
    st["state"] = "cancelled"
    if not st.get("cancel_requested_at"):
        st["cancel_requested_at"] = now_iso()
    write_state(job_id, st)
    out({"job_id": job_id, "state": "cancelled"})


def cmd_job_continue(job_id):
    st = read_state(job_id)
    if not st:
        out({"error": "unknown_job"})
        return
    st["state"] = "resumable"
    write_state(job_id, st)
    out({"job_id": job_id, "state": "resumable"})


def cmd_selftest():
    """Isolation self-checks runnable on the worker without an agent call."""
    results = {}
    cfg = load_config()
    # 1. personal Hermes data must NOT exist on the worker
    hermes_markers = ["SOUL.md", "USER.md", "MEMORY.md", "events.db",
                      "knowledge-index", ".env"]
    present = [m for m in hermes_markers if (WORKER_ROOT / m).exists()]
    results["personal_data_absent"] = (not present, f"found={present}" if present else "clean")
    # 2. path traversal / symlink escape logic
    wt = WORKTREES_DIR / "selftest"
    wt.mkdir(parents=True, exist_ok=True)
    for bad in ["../escape.txt", "/tmp/escape.txt", "a/../../escape.txt"]:
        try:
            _wt_path(wt, bad)
            results[f"reject:{bad}"] = (False, "NOT rejected")
        except ValueError:
            results[f"reject:{bad}"] = (True, "rejected")
    symlink = wt / "link-out"
    if symlink.exists() or symlink.is_symlink():
        symlink.unlink()
    try:
        symlink.symlink_to("/tmp")
        try:
            _wt_path(wt, "link-out/escape.txt")
            results["reject:symlink"] = (False, "NOT rejected")
        except ValueError:
            results["reject:symlink"] = (True, "rejected")
    finally:
        if symlink.is_symlink():
            symlink.unlink()
    results["symlink_cleanup"] = (True, "removed")
    ok = all(v[0] for v in results.values())
    out({"selftest": "PASS" if ok else "FAIL", "checks": results})


def main():
    argv = sys.argv[1:]
    via_ssh = False
    # SSH forced-command mode: the client sends "workerctl submit ..." while the
    # authorized_keys runs ".../workerctl request" — recover the real arguments.
    if argv and argv[0] == "request":
        via_ssh = True
        original = os.environ.get("SSH_ORIGINAL_COMMAND", "")
        parts = original.split()
        if parts and parts[0] == "workerctl":
            parts = parts[1:]
        argv = parts
    elif os.environ.get("SSH_ORIGINAL_COMMAND"):
        # invoked over the SSH surface by any other path — same restrictions
        via_ssh = True

    if not argv:
        out({"error": "no_subcommand"})
        return
    cmd, rest = argv[0], argv[1:]
    # M4: internal commands must never be reachable from the SSH surface.
    if via_ssh and cmd in INTERNAL_COMMANDS:
        out({"error": "internal_command_forbidden_over_ssh", "command": cmd})
        return
    if cmd == "status":
        cmd_status()
    elif cmd == "submit" and rest and rest[0] == "-":
        cmd_submit(json.loads(sys.stdin.read()))
    elif cmd == "job_status" and rest:
        cmd_job_status(rest[0])
    elif cmd == "job_result" and rest:
        cmd_job_result(rest[0])
    elif cmd == "job_diff" and rest:
        cmd_job_diff(rest[0], int(rest[1]) if len(rest) > 1 else 100_000)
    elif cmd == "job_cancel" and rest:
        cmd_job_cancel(rest[0])
    elif cmd == "job_continue" and rest:
        cmd_job_continue(rest[0])
    elif cmd == "selftest":
        cmd_selftest()
    elif cmd == "_run_job" and rest:
        package = json.loads((JOBS_DIR / f"{rest[0]}-package.json").read_text())
        _run_job(rest[0], package)
    else:
        out({"error": f"unknown_or_forbidden:{cmd}"})


if __name__ == "__main__":
    main()
