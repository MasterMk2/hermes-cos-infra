#!/usr/bin/env python3
"""Host Coding Bridge (Phase F.1.1-Pi.2).

A small, strictly-typed RPC bridge on a Unix socket. The Hermes container
 talks to the worker container ONLY through this bridge; it never touches
 Docker directly and can never pass images, mounts, paths or commands.

Protocol: one JSON request line -> one JSON response line per connection.

Actions (complete surface):
  worker_status            bridge/docker/image/active job/resources
  job_start                prepare worktree + run agent + tests (detached)
  job_status <job_id>      state only
  job_result <job_id>      evidence JSON (bounded)
  job_cancel <job_id>      stop container, mark cancelled
  job_cleanup <job_id>     remove terminal job's worktree

Security model:
  - agent command and container args come from THIS config only
  - repo_id must exist in the registry; paths never accepted
  - tests_command must exactly match the repository registry
  - container: cap-drop ALL, no-new-privileges, read-only rootfs,
    tmpfs /tmp, memory/cpu/pids limits, fixed uid, no published ports
  - one job at a time

Production Agent addendum (F.1.1-Pi Hermes+CommandCode):
  - agents registry may run the worker Hermes CLI (hermes-commandcode-*)
  - per-agent read-only mounts come from agent_mounts (this config only)
  - per-agent credential env comes from agent_pass_env + secrets_source:
    the bridge alone reads the host secret file and passes requested
    variable names into the container env at run time. Secret values are
    never logged, never stored in job dirs, and never included in evidence.

F.1.1-Pi.2 hardening:
  - H1: test_command criteria PASS only when the registered test ran (exit 0)
  - H2: worktree base_sha is recorded at job_start; the canonical changed
    set = committed diff vs base UNION porcelain changes (staged/unstaged/
    untracked) so agent commits can never hide changes from scans
  - H3: agent stdout is drained by a reader thread; the supervision loop
    wakes every poll interval (watchdog works for silent agents)
  - H4: cancellation is terminal — finalize never overwrites 'cancelled'
  - M7: git state unknown at job_start is fail-closed (job refused)

Phase F.2 (independent review):
  - review_start/status/result/cancel/cleanup: typed reviewer actions only;
    the reviewer agent+model come from THIS config, never from requests
  - review snapshot: detached worktree at the exact target SHA plus the
    implementation's uncommitted state; the implementation worktree itself
    is never mounted into the reviewer container
  - combined concurrency 1: an active review blocks job_start and vice versa
  - content_sha256: per-file bounded content binding for exact-revision
    approval (covers uncommitted trees)
  - job_verify: fresh canonical re-verification used by the completion gate
"""

import hashlib
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # canonical security_patterns module (src/ level)
from security_patterns import SECRET_PATTERNS, scan_text, is_env_file

CONFIG_PATH = ROOT / "bridge" / "config.json"
SOCK = ROOT / "bridge-sock" / "bridge.sock"
LOG = ROOT / "logs" / "bridge.log"

JOB_FIELDS = {"job_id", "repo_id", "agent", "prompt", "tests_command",
              "acceptance_criteria", "timeout_s"}
REVIEW_FIELDS = {"job_id", "review_id", "cycle", "repo_id", "target_sha",
                 "base_sha", "prompt", "timeout_s"}

# Item 43: bounded reads for artifact-based criteria.
ARTIFACT_MAX_BYTES = 1_000_000
# Item 43/M6: bounded diff body for hashing.
DIFF_MAX_BYTES = 2_000_000
# F.2: bounded per-file content for the exact-revision binding hash.
CONTENT_HASH_FILE_MAX = 200_000
# F.2: bounded reviewer output file read.
REVIEW_RESULT_MAX_BYTES = 1_000_000
# F.2: bounded total for untracked-file copies into a review snapshot.
SNAPSHOT_COPY_MAX_BYTES = 4_000_000


def cfg():
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def log(msg):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as fh:
        fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}\n")


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def job_dir(job_id):
    if not re.fullmatch(r"job_[0-9a-f]{10}", job_id or ""):
        raise ValueError("bad job_id")
    d = ROOT / "jobs" / job_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_path(job_id):
    return job_dir(job_id) / "state.json"


def read_state(job_id):
    p = state_path(job_id)
    return json.loads(p.read_text()) if p.exists() else None


def write_state(job_id, st):
    state_path(job_id).write_text(json.dumps(st, ensure_ascii=False, indent=1))
    (state_path(job_id)).chmod(0o600)


def any_active_job():
    for p in (ROOT / "jobs").glob("*/state.json"):
        try:
            if json.loads(p.read_text()).get("state") == "running":
                return p.parent.name
        except Exception:
            continue
    return None


def any_active_review():
    """F.2: an in-flight reviewer run also occupies the single coding slot."""
    for p in (ROOT / "jobs").glob("*/review-*/state.json"):
        try:
            if json.loads(p.read_text()).get("state") == "running":
                return p.parent.name + "/" + p.parent.name
        except Exception:
            continue
    return None


def content_hash(worktree, files, per_file_max=CONTENT_HASH_FILE_MAX):
    """F.2: exact-revision content binding (section 50/51).

    sha256 over the sorted (path, bounded file content hash) pairs of the
    canonical changed set. Detects ANY content change — including uncommitted
    working-tree edits that a committed-diff hash cannot see."""
    parts = []
    for f in files or []:
        try:
            p = _wt_path(worktree, f)
        except ValueError:
            parts.append([f, "path_escape"])
            continue
        if p.is_file() and not p.is_symlink():
            try:
                h = hashlib.sha256(p.read_bytes()[:per_file_max]).hexdigest()
            except OSError:
                h = "unreadable"
            parts.append([f, h])
        else:
            parts.append([f, "missing"])
    raw = json.dumps(parts, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def docker(*args, timeout=120, check=False):
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=check)


def _wt_path(worktree, rel):
    if not rel or Path(rel).is_absolute() or ".." in Path(rel).parts:
        raise ValueError(f"unsafe path: {rel!r}")
    p = (Path(worktree) / rel).resolve()
    if not str(p).startswith(str(Path(worktree).resolve())):
        raise ValueError(f"path escapes worktree: {rel!r}")
    return p


def parse_porcelain_z(data):
    """Item 44: NUL-separated porcelain parsing (no quoted-path / ' -> '
    string splitting). Rename entries: first field is the destination path."""
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
            i += 1  # skip original-path field
        files.add(path)
    return files


def canonical_changed_files(worktree, base_sha):
    """H2: canonical changed-file set.
      committed diff (base...HEAD) UNION working-tree changes
    (staged + unstaged + untracked). Commits can no longer hide changes.
    Raises on git failure — the caller must fail closed, not assume clean."""
    files = set()
    if base_sha:
        r = docker_run_git(worktree, "diff", "--name-only", "-z",
                           f"{base_sha}...HEAD")
        if r.returncode != 0:
            raise RuntimeError(f"git diff vs base failed: {(r.stderr or '')[:200]}")
        files |= {f for f in (r.stdout or "").split("\0") if f}
    st = docker_run_git(worktree, "status", "--porcelain", "-z")
    if st.returncode != 0:
        raise RuntimeError(f"git status failed: {(st.stderr or '')[:200]}")
    files |= parse_porcelain_z(st.stdout or "")
    return sorted(files)


def worktree_base_sha(repo_path):
    """M7 fail-closed: resolve the base sha at worktree creation, or raise."""
    r = subprocess.run(["git", "-C", str(repo_path), "rev-parse", "HEAD"],
                       capture_output=True, text=True, timeout=120)
    sha = (r.stdout or "").strip()
    if r.returncode != 0 or not sha:
        raise RuntimeError(
            f"git state unknown for {repo_path}: {(r.stderr or 'no output')[:200]}")
    return sha


def bounded_diff_hash(worktree, base_sha, max_bytes=DIFF_MAX_BYTES):
    """M6: sha256 over the bounded canonical diff body (hash only, no bulk)."""
    if not base_sha:
        return None
    r = docker_run_git(worktree, "diff", f"{base_sha}...HEAD")
    body = (r.stdout or "").encode("utf-8", "replace")[:max_bytes]
    return hashlib.sha256(body).hexdigest()[:16]


def docker_run_git(worktree, *args):
    # run git ON THE HOST against the worktree (bridge owns it)
    return subprocess.run(["git", "-C", str(worktree), *args],
                          capture_output=True, text=True, timeout=120)


def secret_scan(worktree, files, max_bytes=200_000):
    hits = set()
    for f in files:
        if is_env_file(f):
            hits.add("env_file_added")
            continue
        try:
            p = _wt_path(worktree, f)
        except ValueError:
            hits.add("worktree_escape_path")
            continue
        try:
            if p.is_file() and not p.is_symlink():
                text = p.read_text(errors="replace")[:max_bytes]
                hits |= scan_text(text)
        except OSError:
            continue
    return sorted(hits)


def evaluate_criteria(criteria, repo_cfg, worktree, tests_result=None,
                      changed_files=None, artifact_max_bytes=ARTIFACT_MAX_BYTES):
    """Deterministic criteria evaluation.

    H1: kind=test_command passes ONLY when `tests_result` shows the
    registered test was executed and exited 0 (status == PASS). When no test
    command was registered/executed the criterion is FAIL (never a silent
    'evaluated via allowlisted test run' bypass).
    Item 43: artifact_contains reads are bounded (artifact_too_large)."""
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
                if p.is_file():
                    import ast
                    tree = ast.parse(p.read_text(encoding="utf-8"))
                    names = {n.name for n in ast.walk(tree)
                             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                               ast.ClassDef))}
                    ok = params.get("name") in names
                    detail = f"{'found' if ok else 'missing'}: {params.get('name')}"
                else:
                    detail = f"file missing: {params.get('file')}"
            elif kind == "git_scope":
                # H2: always evaluate scope against the canonical changed set
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
                    # Item 43: never unbounded-read huge artifacts
                    ok, detail = False, f"artifact_too_large (> {artifact_max_bytes} bytes)"
                else:
                    ok = params.get("text", "") in \
                        p.read_text(errors="replace")[:artifact_max_bytes]
                    detail = f"contains={ok}"
            elif kind == "test_command":
                # H1: executed registered test with exit 0 — or FAIL.
                tstat = (tests_result or {}).get("status") or "NOT_RUN"
                ok = tstat == "PASS"
                detail = f"tests={tstat}"
        except (ValueError, SyntaxError, OSError, RuntimeError) as exc:
            ok, detail = False, f"error: {exc}"
        results.append({"id": ac.get("id"), "description": ac.get("description"),
                        "verification": kind, "status": "PASS" if ok else "FAIL",
                        "detail": str(detail)[:200]})
    return results


def container_limits(c):
    return [
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--memory", c["limits"]["memory"],
        "--memory-swap", c["limits"]["memory"],
        "--cpus", c["limits"]["cpus"],
        "--pids-limit", str(c["limits"]["pids"]),
        "--read-only",
        "--tmpfs", "/tmp:rw,size=256m,exec,nosuid",
        "--user", f"{c['container_uid']}:{c['container_gid']}",
        "-e", "HOME=/agent-home",
        "-e", "DEBIAN_FRONTEND=noninteractive",
    ]


def _read_secret_values(keys):
    """Read ONLY the requested credential names from the bridge-owned secret
    source (F.1.1-Pi addendum). Values are passed straight to the worker
    container env; they are never logged, never written to job dirs, and
    never included in evidence. Missing names are simply absent."""
    c = cfg()
    src = c.get("secrets_source")
    wanted = set(keys or [])
    if not src or not wanted:
        return {}
    try:
        text = Path(src).read_text(encoding="utf-8")
    except OSError:
        log(f"secrets_source unreadable: {src} (presence only, no value)")
        return {}
    vals = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if k in wanted:
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            if v:
                vals[k] = v
    return vals


def usage_summary(job_id):
    """Aggregate per-attempt --usage-file reports (numeric fields only)."""
    return usage_summary_dir(ROOT / "jobs" / job_id, "usage-attempt-*.json")


def usage_summary_dir(directory, pattern):
    """Aggregate --usage-file reports from a directory (numeric fields only)."""
    agg = {"attempts": [], "input_tokens": 0, "output_tokens": 0,
           "reasoning_tokens": 0, "cache_read_tokens": 0,
           "cache_write_tokens": 0, "total_tokens": 0, "api_calls": 0,
           "estimated_cost_usd": 0.0}
    # Section 76: never invent a cost. The harness writes estimated_cost_usd=0.0
    # together with cost_status="unknown" when the provider returned no cost at
    # all; aggregating that as 0.0 would record "this run was free", which is a
    # fabricated billing figure. Only costs the provider actually reported are
    # summed; if none were reported the aggregate cost is None/"unavailable"
    # and only the token counts are kept (they are enough to compute a cost
    # later, outside the jobs DB).
    cost_known = False
    found = False
    for f in sorted(Path(directory).glob(pattern)):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        found = True
        entry = {"attempt_file": f.name}
        for k in ("input_tokens", "output_tokens", "reasoning_tokens",
                  "cache_read_tokens", "cache_write_tokens", "total_tokens",
                  "api_calls", "estimated_cost_usd", "cost_status",
                  "cost_source", "model", "provider", "completed", "failed"):
            entry[k] = d.get(k)
        agg["attempts"].append(entry)
        for k in ("input_tokens", "output_tokens", "reasoning_tokens",
                  "cache_read_tokens", "cache_write_tokens", "total_tokens",
                  "api_calls"):
            v = d.get(k) or 0
            if isinstance(v, (int, float)):
                agg[k] += int(v)
        cost = d.get("estimated_cost_usd")
        status = (d.get("cost_status") or "").lower()
        if isinstance(cost, (int, float)) and status not in ("unknown", "unavailable"):
            cost_known = True
            agg["estimated_cost_usd"] += float(cost)
    if not found:
        return None
    if cost_known:
        agg["estimated_cost_usd"] = round(agg["estimated_cost_usd"], 6)
        agg["cost_status"] = "provider_reported"
    else:
        agg["estimated_cost_usd"] = None
        agg["cost_status"] = "unavailable"
    return agg


def worktree_max_mtime(worktree):
    latest = 0.0
    for p in list(Path(worktree).rglob("*"))[:2000]:
        try:
            latest = max(latest, p.lstat().st_mtime)
        except OSError:
            continue
    return latest


def _drain_output(stream, out_q):
    """H3: reader thread body — pushes stdout lines into a queue so the
    supervision loop NEVER blocks on a silent agent's readline()."""
    try:
        for line in iter(stream.readline, ""):
            out_q.put(line)
    except (ValueError, OSError):
        pass
    finally:
        out_q.put(None)  # EOF sentinel


def supervise_process(proc, *, mtime_fn=lambda: 0.0, timeout_s=1200,
                      hang_watchdog_s=900, grace_s=300, stop_fn,
                      cancelled_fn=None, poll_s=1.0):
    """H3: non-blocking supervision loop (transport-agnostic core).

    Guarantees:
      - the loop wakes at least every `poll_s` seconds regardless of whether
        the process produces output (silent agents included)
      - each cycle re-checks: process alive, absolute timeout, output age,
        worktree activity (mtime), cancellation
      - on timeout/hang/cancel it invokes stop_fn() exactly once and waits
      - returns (exit_status, timed_out, output_tail)

    Reader thread + queue keeps readline() off the supervision path, so a
    process that stops writing can no longer disable the watchdog."""
    out_q = queue.Queue()
    reader = threading.Thread(target=_drain_output, args=(proc.stdout, out_q),
                              daemon=True)
    reader.start()
    started = time.time()
    last_activity = time.time()
    last_mtime = mtime_fn()
    output_tail = ""
    timed_out = False

    def kill_and_wait():
        try:
            stop_fn()
        except Exception as exc:
            log(f"supervise stop_fn error: {type(exc).__name__}: {exc}")
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=30)
            except Exception:
                pass

    while True:
        try:
            item = out_q.get(timeout=poll_s)
        except queue.Empty:
            item = None
        if item is not None:
            if item is None:
                # EOF: process stdout closed — drain the rest and finish
                while True:
                    try:
                        extra = out_q.get_nowait()
                    except queue.Empty:
                        break
                    if extra is None:
                        break
                    output_tail = (output_tail + extra)[-4000:]
                break
            output_tail = (output_tail + item)[-4000:]
            last_activity = time.time()
        alive = proc.poll() is None
        if not alive and out_q.empty():
            break
        # worktree activity counts as progress even without stdout
        try:
            mtime = mtime_fn()
            if mtime > last_mtime:
                last_mtime = mtime
                last_activity = time.time()
        except OSError:
            pass
        elapsed = time.time() - started
        # H4: a cancel recorded while running must stop the container now,
        # not only after the agent happens to exit.
        if cancelled_fn is not None and cancelled_fn():
            timed_out = True
            log("supervise: cancel requested; stopping process")
            kill_and_wait()
            break
        if elapsed > timeout_s:
            timed_out = True
            log(f"supervise: absolute timeout after {int(elapsed)}s")
            kill_and_wait()
            break
        if elapsed > grace_s and time.time() - last_activity > hang_watchdog_s:
            timed_out = True
            log(f"supervise: hang watchdog fired "
                f"(no progress {int(time.time() - last_activity)}s)")
            kill_and_wait()
            break
    # final drain of anything left in the queue
    while True:
        try:
            extra = out_q.get_nowait()
        except queue.Empty:
            break
        if extra is None:
            continue
        output_tail = (output_tail + extra)[-4000:]
    return proc.poll() if proc.poll() is not None else proc.returncode, \
        timed_out, output_tail


def run_agent_container(job_id, repo_id, agent, prompt_file, timeout_s):
    c = cfg()
    name = f"hermes-cw-{job_id}"
    worktree = ROOT / "worktrees" / job_id
    jobdir = ROOT / "jobs" / job_id
    cmd = [
        "docker", "run", "--rm", "--name", name,
        *container_limits(c),
        "-v", f"{worktree}:/workspace",
        "-v", f"{jobdir}:/jobs",
        "-v", f"{ROOT / 'bin'}:/opt/workerbin:ro",
        "-v", f"{ROOT / 'agent-home'}:/agent-home",
        "-w", "/workspace",
    ]
    # per-agent, bridge-config-owned mounts (e.g. read-only worker Hermes
    # config) and credential env (F.1.1-Pi addendum: values pass through
    # argv once to the docker daemon; they are never logged or persisted
    # by the bridge)
    for m in (c.get("agent_mounts") or {}).get(agent) or []:
        cmd.extend(["-v", m])
    secrets = _read_secret_values((c.get("agent_pass_env") or {}).get(agent))
    for k, v in secrets.items():
        cmd.extend(["-e", f"{k}={v}"])
    cmd.extend(["-e", f"HERMES_JOB_ID={job_id}"])
    cmd.append(c["image"])
    cmd.extend(c["agents"][agent].format(prompt_file=prompt_file).split())
    started = time.time()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)

    def _cancelled():
        st = read_state(job_id)
        return bool(st) and (st.get("state") == "cancelled"
                             or bool(st.get("cancel_requested_at")))

    def _stop():
        subprocess.run(["docker", "stop", "-t", "10", name],
                       capture_output=True, timeout=60)

    exit_status, timed_out, output_tail = supervise_process(
        proc,
        mtime_fn=lambda: worktree_max_mtime(worktree),
        timeout_s=timeout_s,
        hang_watchdog_s=c.get("hang_watchdog_s", 900),
        grace_s=300,
        stop_fn=_stop,
        cancelled_fn=_cancelled,
        poll_s=1.0,
    )
    insp = docker("inspect", name, timeout=30)
    oom = False
    if insp.returncode == 0:
        try:
            st = json.loads(insp.stdout)[0]["State"]
            oom = bool(st.get("OOMKilled"))
            if st.get("ExitCode") is not None:
                exit_status = st["ExitCode"]
        except Exception:
            pass
    docker("rm", "-f", name, timeout=60)
    return {
        "exit_status": exit_status, "timed_out": timed_out, "oom": oom,
        "agent_reason": "\n".join([l for l in output_tail.splitlines()
                                   if l.strip()][-10:])[:2000],
        "duration_s": round(time.time() - started, 1),
    }


def run_test_container(job_id, repo_cfg, tests_command):
    c = cfg()
    name = f"hermes-cw-{job_id}-test"
    worktree = ROOT / "worktrees" / job_id
    cmd = [
        "docker", "run", "--rm", "--name", name,
        *container_limits(c),
        "-v", f"{worktree}:/workspace",
        "-w", "/workspace",
        c["image"],
        "bash", "-lc", f"cd /workspace && {tests_command}",
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        out = (p.stdout or "") + (p.stderr or "")
        lines = [l for l in out.splitlines() if l.strip()]
        return {"status": "PASS" if p.returncode == 0 else "FAIL",
                "summary": "\n".join(lines[-12:])[:1500]}
    except subprocess.TimeoutExpired:
        return {"status": "TIMEOUT", "summary": f"tests exceeded 600s"}
    finally:
        docker("rm", "-f", name, timeout=60)


def _is_cancelled_state(st):
    """H4: cancellation is terminal intent — once recorded, finalization can
    never promote the job to finished/failed."""
    return bool(st) and (st.get("state") == "cancelled"
                         or bool(st.get("cancel_requested_at")))


def _commit_terminal_state(job_id, st, new_state, note=None, error=None):
    """H4 CAS-style final write: re-reads the persisted state and refuses to
    overwrite 'cancelled' (a cancel may have landed while the worker thread
    was finishing). Returns the state actually persisted."""
    cur = read_state(job_id) or {}
    if _is_cancelled_state(cur) or _is_cancelled_state(st):
        st["state"] = "cancelled"
        st.setdefault("cancel_requested_at", cur.get("cancel_requested_at") or now())
        if note:
            st["note"] = note
        if error:
            st["error"] = error
    else:
        st["state"] = new_state
        if note:
            st["note"] = note
        if error:
            st["error"] = error
    write_state(job_id, st)
    return st["state"]


# ─────────────────────────────────────────────────────────────────────────
# Phase F.2: independent reviewer (typed actions only — section 123)
# ─────────────────────────────────────────────────────────────────────────

def review_dir(job_id, review_id):
    d = ROOT / "jobs" / job_id / f"review-{review_id}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def review_state_path(job_id, review_id):
    return review_dir(job_id, review_id) / "state.json"


def read_review_state(job_id, review_id):
    p = review_state_path(job_id, review_id)
    return json.loads(p.read_text()) if p.exists() else None


def write_review_state(job_id, review_id, st):
    p = review_state_path(job_id, review_id)
    p.write_text(json.dumps(st, ensure_ascii=False, indent=1))
    p.chmod(0o600)


def _is_review_cancelled(st):
    return bool(st) and (st.get("state") == "cancelled"
                         or bool(st.get("cancel_requested_at")))


def _commit_review_terminal_state(job_id, review_id, st, new_state,
                                  note=None, error=None):
    """F.2/H4: the reviewer finalize must never resurrect a cancelled
    review (cancel during review is terminal intent — section 71)."""
    cur = read_review_state(job_id, review_id) or {}
    if _is_review_cancelled(cur) or _is_review_cancelled(st):
        st["state"] = "cancelled"
        st.setdefault("cancel_requested_at",
                      cur.get("cancel_requested_at") or now())
        if note:
            st["note"] = note
        if error:
            st["error"] = error
    else:
        st["state"] = new_state
        if note:
            st["note"] = note
        if error:
            st["error"] = error
    write_review_state(job_id, review_id, st)
    return st["state"]


def snapshot_path(job_id, cycle):
    return ROOT / "review-snapshots" / job_id / f"cycle-{int(cycle or 1)}"


def build_review_snapshot(job_id, repo_id, target_sha, cycle):
    """Detached read-snapshot of the exact target revision (section 15/16).

    The snapshot = detached worktree at target_sha + the implementation's
    uncommitted tracked diff applied + untracked new files copied (bounded).
    The implementation worktree is never touched or mounted (section 14/98)."""
    repo_path = c_registry_path(repo_id)
    if not repo_path:
        return None, f"unknown repo_id {repo_id!r}"
    impl = ROOT / "worktrees" / job_id
    snap = snapshot_path(job_id, cycle)
    if snap.exists():
        return snap, None
    snap.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["git", "-C", str(repo_path), "worktree", "add", "--detach",
         str(snap), str(target_sha)],
        capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return None, f"snapshot worktree failed: {(r.stderr or '')[-300:]}"
    if impl.exists():
        d = docker_run_git(str(impl), "diff", "HEAD")
        patch = d.stdout or ""
        if patch:
            ap = subprocess.run(
                ["git", "-C", str(snap), "apply", "--whitespace=nowarn", "-"],
                input=patch, capture_output=True, text=True, timeout=120)
            if ap.returncode != 0:
                return None, f"snapshot apply failed: {(ap.stderr or '')[-300:]}"
        ls = docker_run_git(str(impl), "ls-files", "--others",
                            "--exclude-standard")
        total = 0
        for rel in (ls.stdout or "").splitlines():
            rel = rel.strip()
            if not rel:
                continue
            try:
                src = _wt_path(str(impl), rel)
                dst = _wt_path(str(snap), rel)
            except ValueError:
                return None, f"unsafe untracked path: {rel!r}"
            if src.is_file() and not src.is_symlink():
                data = src.read_bytes()[:CONTENT_HASH_FILE_MAX]
                total += len(data)
                if total > SNAPSHOT_COPY_MAX_BYTES:
                    return None, "snapshot untracked copy budget exceeded"
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(data)
    return snap, None


def a_review_start(req):
    c = cfg()
    extra = set(req.keys()) - REVIEW_FIELDS - {"version", "action"}
    if extra:
        return {"error": "unexpected_fields", "fields": sorted(extra)}
    job_id = req.get("job_id") or ""
    review_id = req.get("review_id") or ""
    repo_id = req.get("repo_id") or ""
    target_sha = req.get("target_sha") or ""
    base_sha = req.get("base_sha")
    prompt = req.get("prompt") or ""
    cycle = req.get("cycle") or 1
    timeout_s = int(req.get("timeout_s") or c.get("agent_timeout_s", 1200))
    if not re.fullmatch(r"job_[0-9a-f]{10}", job_id):
        return {"error": "bad_job_id"}
    if not re.fullmatch(r"rev_[0-9a-f]{10}", review_id):
        return {"error": "bad_review_id"}
    if repo_id not in c["repositories"]:
        return {"error": "unknown_repo_id", "allowed": sorted(c["repositories"])}
    if not re.fullmatch(r"[0-9a-f]{40}", target_sha):
        return {"error": "bad_target_sha"}
    if len(prompt) > 100_000:
        return {"error": "prompt_too_large"}
    # F.2 (section 19/124): the reviewer agent comes from THIS config only.
    reviewer_agent = c.get("reviewer_agent") or ""
    if not reviewer_agent or reviewer_agent not in c["agents"]:
        return {"error": "reviewer_agent_unconfigured"}
    active = any_active_job() or any_active_review()
    if active:
        return {"error": "busy", "active": active}
    st = read_review_state(job_id, review_id)
    if st and st.get("state") == "running":
        return {"error": "already_running", "review_id": review_id}

    snap, err = build_review_snapshot(job_id, repo_id, target_sha, cycle)
    if err:
        log(f"{review_id}: snapshot refused: {err}"[:250])
        return {"error": "snapshot_failed", "detail": err}
    try:
        files = canonical_changed_files(str(snap), base_sha)
    except RuntimeError as exc:
        return {"error": "git_state_unknown", "detail": str(exc)[:200]}
    snap_content = content_hash(str(snap), files)

    rd = review_dir(job_id, review_id)
    (rd / "prompt.txt").write_text(prompt)
    (rd / "prompt.txt").chmod(0o600)
    write_review_state(job_id, review_id, {
        "review_id": review_id, "job_id": job_id, "cycle": int(cycle),
        "state": "running", "repo_id": repo_id, "agent": reviewer_agent,
        "target_sha": target_sha, "base_sha": base_sha,
        "snapshot": str(snap), "snapshot_content_sha": snap_content,
        "created_at": now(),
    })
    threading.Thread(target=_review_worker,
                     args=(job_id, review_id, req), daemon=True).start()
    log(f"{review_id}: review started job={job_id} cycle={cycle} "
        f"target={target_sha[:12]} snapshot_content={snap_content[:12]}")
    return {"accepted": True, "review_id": review_id, "state": "running",
            "snapshot_content_sha": snap_content}


def _review_worker(job_id, review_id, req):
    c = cfg()
    reviewer_agent = c.get("reviewer_agent")
    rd = review_dir(job_id, review_id)
    st = read_review_state(job_id, review_id) or {}
    snap = Path(st.get("snapshot") or "")
    name = f"hermes-rv-{review_id}"
    cmd = [
        "docker", "run", "--rm", "--name", name,
        *container_limits(c),
        "-v", f"{snap}:/workspace",
        "-v", f"{rd}:/jobs",
        "-v", f"{ROOT / 'bin'}:/opt/workerbin:ro",
        # F.2 (section 11/48): ONLY the reviewer home is mounted — the
        # implementation agent-home is never visible to the reviewer.
        "-v", f"{ROOT / 'reviewer-home'}:/agent-home",
        "-w", "/workspace",
    ]
    for m in (c.get("agent_mounts") or {}).get(reviewer_agent) or []:
        cmd.extend(["-v", m])
    secrets = _read_secret_values((c.get("agent_pass_env") or {}).get(reviewer_agent))
    for k, v in secrets.items():
        cmd.extend(["-e", f"{k}={v}"])
    cmd.extend(["-e", f"HERMES_REVIEW_ID={review_id}"])
    cmd.append(c["image"])
    cmd.extend(c["agents"][reviewer_agent]
               .format(prompt_file="/jobs/prompt.txt").split())
    started = time.time()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)

    def _cancelled():
        s = read_review_state(job_id, review_id)
        return _is_review_cancelled(s)

    def _stop():
        subprocess.run(["docker", "stop", "-t", "10", name],
                       capture_output=True, timeout=60)

    exit_status, timed_out, output_tail = supervise_process(
        proc,
        mtime_fn=lambda: worktree_max_mtime(str(snap)) if snap.exists() else 0.0,
        timeout_s=int(req.get("timeout_s") or c.get("agent_timeout_s", 1200)),
        hang_watchdog_s=c.get("hang_watchdog_s", 900),
        grace_s=300,
        stop_fn=_stop,
        cancelled_fn=_cancelled,
        poll_s=1.0,
    )
    insp = docker("inspect", name, timeout=30)
    oom = False
    if insp.returncode == 0:
        try:
            s0 = json.loads(insp.stdout)[0]["State"]
            oom = bool(s0.get("OOMKilled"))
            if s0.get("ExitCode") is not None:
                exit_status = s0["ExitCode"]
        except Exception:
            pass
    docker("rm", "-f", name, timeout=60)

    result_text = None
    rf = rd / "review-result.json"
    if rf.exists():
        try:
            result_text = rf.read_text(errors="replace")[:REVIEW_RESULT_MAX_BYTES]
        except OSError:
            result_text = None
    probes = None
    pf = rd / "reviewer-probes.json"
    if pf.exists():
        try:
            probes = json.loads(pf.read_text())
        except ValueError:
            probes = None
    evidence = {
        "review_id": review_id, "job_id": job_id,
        "reviewer_agent": reviewer_agent,
        "exit_status": exit_status, "timed_out": timed_out, "oom": oom,
        "agent_reason": "\n".join([l for l in output_tail.splitlines()
                                   if l.strip()][-10:])[:2000],
        "duration_s": round(time.time() - started, 1),
        "result_json": result_text,
        "usage": usage_summary_dir(rd, "review-usage-*.json"),
        "isolation": probes,
        "finished_at": now(),
    }
    st = read_review_state(job_id, review_id) or {}
    st["evidence"] = evidence
    note = "reviewer produced no result file" if result_text is None else None
    if evidence.get("oom"):
        _commit_review_terminal_state(job_id, review_id, st, "failed",
                                      note=note, error="reviewer container OOM killed")
    else:
        _commit_review_terminal_state(job_id, review_id, st, "finished", note=note)
    log(f"{review_id}: review terminal state={st['state']} "
        f"exit={exit_status} result_file={result_text is not None}")


def a_review_status(req):
    st = read_review_state(req.get("job_id", ""), req.get("review_id", ""))
    if not st:
        return {"error": "unknown_review"}
    return {"review_id": st.get("review_id"), "job_id": st.get("job_id"),
            "cycle": st.get("cycle"), "state": st.get("state"),
            "error": st.get("error")}


def a_review_result(req):
    st = read_review_state(req.get("job_id", ""), req.get("review_id", ""))
    if not st:
        return {"error": "unknown_review"}
    ev = st.get("evidence") or {}
    if isinstance(ev.get("result_json"), str) and \
            len(ev.get("result_json") or "") > REVIEW_RESULT_MAX_BYTES:
        ev = dict(ev)
        ev["result_json"] = ev["result_json"][:REVIEW_RESULT_MAX_BYTES]
    return {"review_id": st.get("review_id"), "job_id": st.get("job_id"),
            "state": st.get("state"), "error": st.get("error"),
            "note": st.get("note"), "review": ev}


def a_review_cancel(req):
    job_id = req.get("job_id", "")
    review_id = req.get("review_id", "")
    st = read_review_state(job_id, review_id)
    if not st:
        return {"error": "unknown_review"}
    if st.get("state") == "running":
        docker("stop", "-t", "10", f"hermes-rv-{review_id}", timeout=60)
    st["state"] = "cancelled"
    if not st.get("cancel_requested_at"):
        st["cancel_requested_at"] = now()
    write_review_state(job_id, review_id, st)
    return {"review_id": review_id, "state": "cancelled"}


def a_review_cleanup(req):
    job_id = req.get("job_id", "")
    review_id = req.get("review_id", "")
    st = read_review_state(job_id, review_id)
    if not st:
        return {"error": "unknown_review"}
    if st.get("state") == "running":
        return {"error": "review_still_running"}
    snap = Path(st.get("snapshot") or "")
    removed = False
    if snap.exists():
        repo_path = c_registry_path(st.get("repo_id"))
        if repo_path:
            subprocess.run(["git", "-C", str(repo_path), "worktree", "remove",
                            "--force", str(snap)],
                           capture_output=True, text=True, timeout=120)
        if snap.exists():
            import shutil
            shutil.rmtree(snap, ignore_errors=True)
        removed = not snap.exists()
    return {"review_id": review_id, "snapshot_removed": removed}


def _job_worker(job_id, package):
    c = cfg()
    repo_cfg = c["repositories"][package["repo_id"]]
    worktree = ROOT / "worktrees" / job_id
    try:
        st = read_state(job_id) or {}
        # H4: never resurrect a cancelled job into running
        if _is_cancelled_state(st):
            log(f"{job_id}: cancel already requested; not starting worker")
            return
        st.update({"state": "running", "started_at": now()})
        write_state(job_id, st)

        agent_res = run_agent_container(job_id, package["repo_id"],
                                        package["agent"],
                                        ROOT / "jobs" / job_id / "prompt.txt",
                                        int(package.get("timeout_s") or
                                            c.get("agent_timeout_s", 1200)))
        # H2: base_sha is recorded at worktree creation and never changes;
        # the canonical changed set is committed-diff-vs-base UNION porcelain.
        base_sha = st.get("base_sha")
        files = canonical_changed_files(worktree, base_sha)
        scans = secret_scan(worktree, files)
        tests = run_test_container(job_id, repo_cfg, package["tests_command"]) \
            if package.get("tests_command") else {"status": "NOT_RUN", "summary": None}
        # H1: test_command criteria are evaluated against the actual test run
        criteria_results = evaluate_criteria(
            package.get("acceptance_criteria") or [], repo_cfg, worktree,
            tests_result=tests, changed_files=files)
        diff_stat = (docker_run_git(worktree, "diff", "--stat", "HEAD").stdout or "")[:2000]
        head = docker_run_git(worktree, "rev-parse", "HEAD")
        diff_hash = bounded_diff_hash(worktree, base_sha)
        probes = None
        probes_file = ROOT / "jobs" / job_id / "probes.json"
        if probes_file.exists():
            try:
                probes = json.loads(probes_file.read_text())
            except ValueError:
                probes = None
        evidence = {
            "job_id": job_id, "repo_id": package["repo_id"],
            "agent": package["agent"], "transport": "pi-sandbox",
            "branch": f"hermes/job-{job_id}", "worktree_id": job_id,
            "worktree_path": str(worktree), "head_sha":
                head.stdout.strip() if head.returncode == 0 else None,
            "base_sha": base_sha,
            **agent_res,
            "changed_files": files,
            "diff_stat": diff_stat,
            "diff_sha256": diff_hash,
            "content_sha256": content_hash(str(worktree), files),
            "test_summary": {"status": tests["status"], "summary": tests["summary"],
                             "criteria_results": criteria_results},
            "secret_scan": scans,
            "isolation": probes,
            "usage": usage_summary(job_id),
            "finished_at": now(),
        }
        st["evidence"] = evidence
        # note: scans / OOM are recorded as evidence, but the terminal state
        # decision itself must respect cancellation (H4).
        note = "secret scan hits present" if scans else None
        if agent_res.get("oom") and not scans:
            _commit_terminal_state(job_id, st, "failed",
                                   note=note, error="worker container OOM killed")
        else:
            _commit_terminal_state(job_id, st, "finished", note=note)
        log(f"{job_id}: job terminal state={st['state']} exit={agent_res['exit_status']}")
    except Exception as exc:  # durability: record, never leave state running
        st = read_state(job_id) or {}
        st = st if isinstance(st, dict) else {}
        if "evidence" not in st:
            st["evidence"] = st.get("evidence")
        _commit_terminal_state(job_id, st, "failed",
                               error=f"{type(exc).__name__}: {str(exc)[:300]}")
        log(f"{job_id}: EXCEPTION {st.get('error')}"[:300])


# ── actions ─────────────────────────────────────────────────────────────
def a_worker_status(_req):
    c = cfg()
    docker_ok = docker("info", "--format", "{{.ServerVersion}}", timeout=30).returncode == 0
    img = docker("image", "inspect", c["image"], "--format", "{{.Id}}", timeout=30)
    active = any_active_job()
    mem = ""
    try:
        memopen = open("/proc/meminfo")
        for line in memopen:
            if line.startswith("MemAvailable"):
                mem = line.split()[1] + " kB available"
                break
    except OSError:
        pass
    return {
        "bridge": "alive", "docker_reachable": docker_ok,
        "worker_image": c["image"],
        "worker_image_present": img.returncode == 0,
        "worker_image_id": img.stdout.strip() if img.returncode == 0 else None,
        "active_job": active,
        "active_review": any_active_review(),
        "reviewer_agent": c.get("reviewer_agent"),
        "resources": mem,
    }


def a_job_start(req):
    c = cfg()
    extra = set(req.keys()) - JOB_FIELDS - {"version", "action"}
    if extra:
        return {"error": "unexpected_fields", "fields": sorted(extra)}
    job_id = req.get("job_id") or ""
    repo_id = req.get("repo_id") or ""
    agent = req.get("agent") or ""
    prompt = req.get("prompt") or ""
    tests_command = req.get("tests_command")
    criteria = req.get("acceptance_criteria") or []
    if not re.fullmatch(r"job_[0-9a-f]{10}", job_id):
        return {"error": "bad_job_id"}
    if repo_id not in c["repositories"]:
        return {"error": "unknown_repo_id", "allowed": sorted(c["repositories"])}
    if agent not in c["agents"]:
        return {"error": "unknown_agent", "allowed": sorted(c["agents"])}
    if len(prompt) > 100_000:
        return {"error": "prompt_too_large"}
    if tests_command and tests_command not in \
            c["repositories"][repo_id].get("test_commands", []):
        return {"error": "test_command_not_in_registry"}
    if len(criteria) > 20:
        return {"error": "too_many_criteria"}
    active = any_active_job() or any_active_review()
    if active and active != job_id:
        return {"error": "busy", "active_job": active}

    repo_path = Path(c["repositories"][repo_id]["path"])
    worktree = ROOT / "worktrees" / job_id
    base_sha = None
    if not worktree.exists():
        # H2/M7: the comparison base is resolved ONCE at worktree creation.
        # Unknown git state is fail-closed (no job, no assumptions).
        try:
            base_sha = worktree_base_sha(repo_path)
        except RuntimeError as exc:
            log(f"{job_id}: refused job_start: {exc}"[:250])
            return {"error": "git_state_unknown", "detail": str(exc)[:200]}
        branch = f"hermes/job-{job_id}"
        r = subprocess.run(
            ["git", "-C", str(repo_path), "worktree", "add", "-b", branch,
             str(worktree)],
            capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return {"error": "worktree_failed", "detail": (r.stderr or "")[-300:]}
    else:
        st = read_state(job_id) or {}
        if st.get("state") == "running":
            # re-adopt: supervisor runner died; keep watching the live container
            return {"accepted": True, "job_id": job_id, "state": "running",
                    "readopted": True, "attempt": st.get("attempt", 1)}
        # H4: a cancelled job is not silently re-adopted as running
        if _is_cancelled_state(st):
            return {"error": "job_cancelled", "job_id": job_id}
        base_sha = st.get("base_sha")

    jd = job_dir(job_id)
    (jd / "prompt.txt").write_text(prompt)
    (jd / "prompt.txt").chmod(0o600)
    st = read_state(job_id) or {}
    write_state(job_id, {
        "job_id": job_id, "state": "running",
        "repo_id": repo_id, "agent": agent,
        "base_sha": base_sha or st.get("base_sha"),
        "tests_command": tests_command or st.get("tests_command"),
        "attempt": st.get("attempt", 0) + 1,
        "created_at": st.get("created_at") or now(),
    })
    threading.Thread(target=_job_worker, args=(job_id, req), daemon=True).start()
    log(f"{job_id}: job started repo={repo_id} agent={agent}")
    return {"accepted": True, "job_id": job_id, "state": "running",
            "worktree": str(worktree)}


def a_job_status(req):
    st = read_state(req.get("job_id", ""))
    if not st:
        return {"error": "unknown_job"}
    return {"job_id": st.get("job_id"), "state": st.get("state"),
            "error": st.get("error"), "attempt": st.get("attempt")}


def a_job_result(req):
    st = read_state(req.get("job_id", ""))
    if not st:
        return {"error": "unknown_job"}
    ev = st.get("evidence") or {}
    return {"job_id": st.get("job_id"), "state": st.get("state"),
            "error": st.get("error"), "note": st.get("note"), "evidence": ev}


def a_job_cancel(req):
    job_id = req.get("job_id", "")
    st = read_state(job_id)
    if not st:
        return {"error": "unknown_job"}
    if st.get("state") == "running":
        docker("stop", "-t", "10", f"hermes-cw-{job_id}", timeout=60)
    # H4: cancellation is terminal intent — stamp it persistently. The
    # worker thread's finalize step can no longer write finished/failed.
    st["state"] = "cancelled"
    if not st.get("cancel_requested_at"):
        st["cancel_requested_at"] = now()
    write_state(job_id, st)
    return {"job_id": job_id, "state": "cancelled"}


def a_job_cleanup(req):
    job_id = req.get("job_id", "")
    st = read_state(job_id)
    if not st:
        return {"error": "unknown_job"}
    if st.get("state") == "running":
        return {"error": "job_still_running"}
    worktree = ROOT / "worktrees" / job_id
    repo_path = c_registry_path(st.get("repo_id"))
    if worktree.exists() and repo_path:
        subprocess.run(["git", "-C", str(repo_path), "worktree", "remove",
                        "--force", str(worktree)],
                       capture_output=True, text=True, timeout=120)
    return {"job_id": job_id, "cleaned": not worktree.exists()}


def c_registry_path(repo_id):
    c = cfg()
    r = c.get("repositories", {}).get(repo_id or "")
    return Path(r["path"]) if r else None


def a_job_diff(req):
    """F.2 (section 44/47): bounded canonical diff text for the review
    package (committed diff vs base UNION the uncommitted working tree).
    Read-only; never mutates state."""
    c = cfg()
    job_id = req.get("job_id") or ""
    try:
        max_bytes = min(int(req.get("max_bytes") or 60_000), 200_000)
    except (TypeError, ValueError):
        max_bytes = 60_000
    if not re.fullmatch(r"job_[0-9a-f]{10}", job_id):
        return {"error": "bad_job_id"}
    st = read_state(job_id)
    if not st:
        return {"error": "unknown_job"}
    worktree = ROOT / "worktrees" / job_id
    if not worktree.exists():
        return {"error": "no_worktree"}
    base = st.get("base_sha")
    parts = []
    if base:
        r1 = docker_run_git(str(worktree), "diff", f"{base}...HEAD")
        if r1.returncode != 0:
            return {"error": "git_state_unknown", "detail": (r1.stderr or "")[:200]}
        parts.append(r1.stdout or "")
    r2 = docker_run_git(str(worktree), "diff", "HEAD")
    if r2.returncode != 0:
        return {"error": "git_state_unknown", "detail": (r2.stderr or "")[:200]}
    parts.append(r2.stdout or "")
    ls = docker_run_git(str(worktree), "ls-files", "--others",
                        "--exclude-standard")
    untracked = [l.strip() for l in (ls.stdout or "").splitlines() if l.strip()]
    text = "\n".join(p for p in parts if p)
    truncated = len(text) > max_bytes
    return {"diff": text[:max_bytes], "truncated": truncated,
            "untracked_files": untracked[:50]}


def a_job_verify(req):
    """F.2 (section 52): fresh canonical re-verification for the completion
    gate — current HEAD, content binding, secret scan and a real re-run of
    the registered tests. Read-only for the job record; the test container is
    ephemeral."""
    c = cfg()
    job_id = req.get("job_id") or ""
    if not re.fullmatch(r"job_[0-9a-f]{10}", job_id):
        return {"error": "bad_job_id"}
    st = read_state(job_id)
    if not st:
        return {"error": "unknown_job"}
    worktree = ROOT / "worktrees" / job_id
    if not worktree.exists():
        return {"error": "no_worktree"}
    if any_active_job() or any_active_review():
        return {"error": "busy"}
    repo_cfg = c["repositories"].get(st.get("repo_id") or "")
    if not repo_cfg:
        return {"error": "unknown_repo_id"}
    base_sha = st.get("base_sha")
    try:
        files = canonical_changed_files(str(worktree), base_sha)
    except RuntimeError as exc:
        return {"error": "git_state_unknown", "detail": str(exc)[:200]}
    scans = secret_scan(str(worktree), files)
    head = docker_run_git(str(worktree), "rev-parse", "HEAD")
    tests_command = st.get("tests_command")
    if tests_command and tests_command not in repo_cfg.get("test_commands", []):
        return {"error": "test_command_not_in_registry"}
    tests = run_test_container(job_id, repo_cfg, tests_command) \
        if tests_command else {"status": "NOT_RUN", "summary": None}
    return {
        "job_id": job_id,
        "head_sha": head.stdout.strip() if head.returncode == 0 else None,
        "base_sha": base_sha,
        "changed_files": files,
        "content_sha256": content_hash(str(worktree), files),
        "diff_sha256": bounded_diff_hash(worktree, base_sha),
        "secret_scan": scans,
        "tests": {"status": tests["status"], "summary": tests["summary"]},
    }


ACTIONS = {
    "worker_status": (a_worker_status, False),
    "job_start": (a_job_start, True),
    "job_status": (a_job_status, True),
    "job_result": (a_job_result, True),
    "job_cancel": (a_job_cancel, True),
    "job_cleanup": (a_job_cleanup, True),
    # F.2 typed review surface (section 123) — no generic shell/docker
    "job_diff": (a_job_diff, True),
    "job_verify": (a_job_verify, True),
    "review_start": (a_review_start, True),
    "review_status": (a_review_status, True),
    "review_result": (a_review_result, True),
    "review_cancel": (a_review_cancel, True),
    "review_cleanup": (a_review_cleanup, True),
}


def handle(req):
    if not isinstance(req, dict) or req.get("version") != 1:
        return {"error": "bad_request_version"}
    action = req.get("action")
    entry = ACTIONS.get(action)
    if not entry:
        return {"error": "unknown_action", "allowed": sorted(ACTIONS)}
    fn, needs_body = entry
    if not needs_body:
        return fn(req)
    return fn(req)


def main():
    c = cfg()
    if SOCK.exists():
        SOCK.unlink()
    old = umask = os.umask(0o117)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(SOCK))
    os.umask(old)
    os.chmod(SOCK, 0o660)
    srv.listen(16)
    log("bridge listening on " + str(SOCK))
    while True:
        conn, _ = srv.accept()
        try:
            conn.settimeout(30)
            f = conn.makefile("rw")
            line = f.readline()
            if not line.strip():
                f.close()
                continue
            try:
                req = json.loads(line)
                resp = handle(req)
            except Exception as exc:
                resp = {"error": f"internal:{type(exc).__name__}",
                        "detail": str(exc)[:200]}
            f.write(json.dumps(resp, ensure_ascii=False) + "\n")
            f.flush()
            f.close()
        except Exception as exc:
            log(f"connection error: {type(exc).__name__}: {exc}")
        finally:
            try:
                conn.close()
            except OSError:
                pass


if __name__ == "__main__":
    main()
