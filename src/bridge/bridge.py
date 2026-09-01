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

F.1.1-Pi.2 hardening:
  - H1: test_command criteria PASS only when the registered test ran (exit 0)
  - H2: worktree base_sha is recorded at job_start; the canonical changed
    set = committed diff vs base UNION porcelain changes (staged/unstaged/
    untracked) so agent commits can never hide changes from scans
  - H3: agent stdout is drained by a reader thread; the supervision loop
    wakes every poll interval (watchdog works for silent agents)
  - H4: cancellation is terminal — finalize never overwrites 'cancelled'
  - M7: git state unknown at job_start is fail-closed (job refused)
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

# Item 43: bounded reads for artifact-based criteria.
ARTIFACT_MAX_BYTES = 1_000_000
# Item 43/M6: bounded diff body for hashing.
DIFF_MAX_BYTES = 2_000_000


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
        c["image"],
    ] + c["agents"][agent].format(prompt_file=prompt_file).split()
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
            "test_summary": {"status": tests["status"], "summary": tests["summary"],
                             "criteria_results": criteria_results},
            "secret_scan": scans,
            "isolation": probes,
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
    active = any_active_job()
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


ACTIONS = {
    "worker_status": (a_worker_status, False),
    "job_start": (a_job_start, True),
    "job_status": (a_job_status, True),
    "job_result": (a_job_result, True),
    "job_cancel": (a_job_cancel, True),
    "job_cleanup": (a_job_cleanup, True),
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
