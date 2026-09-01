#!/usr/bin/env python3
"""Host Coding Bridge (Phase F.1.1-Pi).

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
"""

import json
import os
import re
import socket
import subprocess
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "bridge" / "config.json"
SOCK = ROOT / "bridge-sock" / "bridge.sock"
LOG = ROOT / "logs" / "bridge.log"

SECRET_PATTERNS = [
    ("private_key_header", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_pat", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("openai_like_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("authorization_header", re.compile(r"(?i)\bauthorization\b\s*[:=]\s*\S+")),
    ("credential_assignment", re.compile(
        r"(?i)\b(api[_-]?key|secret|passwd|password|token)\b\s*[=:]\s*"
        r"[\"']?[A-Za-z0-9_\-+/]{20,}")),
]

JOB_FIELDS = {"job_id", "repo_id", "agent", "prompt", "tests_command",
              "acceptance_criteria", "timeout_s"}


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


def changed_files(worktree):
    files = set()
    st = docker_run_git(worktree, "status", "--porcelain")
    for line in (st.stdout or "").splitlines():
        if len(line) > 3:
            files.add(line[3:].strip())
    return sorted(files)


def docker_run_git(worktree, *args):
    # run git ON THE HOST against the worktree (bridge owns it)
    return subprocess.run(["git", "-C", str(worktree), *args],
                          capture_output=True, text=True, timeout=120)


def secret_scan(worktree, files, max_bytes=200_000):
    hits = set()
    for f in files:
        if f.lower().endswith(".env") or Path(f).name == ".env":
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
                for pid, rx in SECRET_PATTERNS:
                    if rx.search(text):
                        hits.add(pid)
        except OSError:
            continue
    return sorted(hits)


def evaluate_criteria(criteria, repo_cfg, worktree):
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
                cf = changed_files(worktree)
                allowed = repo_cfg.get("allowed_paths") or []
                extra = [f for f in cf if not any(f.startswith(a) for a in allowed)]
                ok, detail = not extra, (f"changed={len(cf)} extra={extra}" if extra
                                         else f"changed={len(cf)} all in scope")
            elif kind == "artifact_contains":
                p = _wt_path(worktree, params.get("path"))
                ok = p.is_file() and params.get("text", "") in \
                    p.read_text(errors="replace")
                detail = f"contains={ok}"
            elif kind == "test_command":
                ok, detail = True, "evaluated via allowlisted test run"
        except (ValueError, SyntaxError, OSError) as exc:
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
    last_activity = time.time()
    last_mtime = worktree_max_mtime(worktree)
    output_tail = ""
    exit_status, timed_out, oom = None, False, False
    while True:
        try:
            out = proc.stdout.readline() if proc.poll() is None else ""
        except Exception:
            out = ""
        if out:
            output_tail = (output_tail + out)[-4000:]
            last_activity = time.time()
        alive = proc.poll() is None
        mtime = worktree_max_mtime(worktree)
        if mtime > last_mtime:
            last_mtime = mtime
            last_activity = time.time()
        elapsed = time.time() - started
        if not alive:
            exit_status = proc.returncode
            break
        if elapsed > timeout_s:
            timed_out = True
            subprocess.run(["docker", "stop", "-t", "10", name],
                           capture_output=True, timeout=60)
            proc.wait(timeout=60)
            exit_status = proc.returncode
            break
        if elapsed > 300 and time.time() - last_activity > c.get("hang_watchdog_s", 900):
            timed_out = True
            log(f"{job_id}: hang watchdog fired (no progress {int(time.time()-last_activity)}s)")
            subprocess.run(["docker", "stop", "-t", "10", name],
                           capture_output=True, timeout=60)
            proc.wait(timeout=60)
            exit_status = proc.returncode
            break
        if int(elapsed) % 30 == 0:
            time.sleep(1)
        else:
            time.sleep(2)
    insp = docker("inspect", name, timeout=30)
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


def _job_worker(job_id, package):
    c = cfg()
    repo_cfg = c["repositories"][package["repo_id"]]
    worktree = ROOT / "worktrees" / job_id
    try:
        st = read_state(job_id) or {}
        st.update({"state": "running", "started_at": now()})
        write_state(job_id, st)

        agent_res = run_agent_container(job_id, package["repo_id"],
                                        package["agent"],
                                        ROOT / "jobs" / job_id / "prompt.txt",
                                        int(package.get("timeout_s") or
                                            c.get("agent_timeout_s", 1200)))
        files = changed_files(worktree)
        scans = secret_scan(worktree, files)
        tests = run_test_container(job_id, repo_cfg, package["tests_command"]) \
            if package.get("tests_command") else {"status": "NOT_RUN", "summary": None}
        criteria_results = evaluate_criteria(package.get("acceptance_criteria") or [],
                                             repo_cfg, worktree)
        diff_stat = (docker_run_git(worktree, "diff", "--stat", "HEAD").stdout or "")[:2000]
        head = docker_run_git(worktree, "rev-parse", "HEAD")
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
            **agent_res,
            "changed_files": files,
            "diff_stat": diff_stat,
            "test_summary": {"status": tests["status"], "summary": tests["summary"],
                             "criteria_results": criteria_results},
            "secret_scan": scans,
            "isolation": probes,
            "finished_at": now(),
        }
        st["evidence"] = evidence
        if scans:
            st["state"] = "finished"
            st["note"] = "secret scan hits present"
        elif agent_res.get("oom"):
            st["state"] = "failed"
            st["error"] = "worker container OOM killed"
        else:
            st["state"] = "finished"
        write_state(job_id, st)
        log(f"{job_id}: job finished state={st['state']} exit={agent_res['exit_status']}")
    except Exception as exc:  # durability: record, never leave state running
        st = read_state(job_id) or {}
        st["state"] = "failed"
        st["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        write_state(job_id, st)
        log(f"{job_id}: EXCEPTION {st['error']}")


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
    if not worktree.exists():
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

    jd = job_dir(job_id)
    (jd / "prompt.txt").write_text(prompt)
    (jd / "prompt.txt").chmod(0o600)
    write_state(job_id, {"job_id": job_id, "state": "running",
                         "repo_id": repo_id, "agent": agent,
                         "attempt": (read_state(job_id) or {}).get("attempt", 0) + 1,
                         "created_at": now()})
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
    st["state"] = "cancelled"
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
