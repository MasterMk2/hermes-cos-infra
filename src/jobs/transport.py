#!/usr/bin/env python3
"""Transport adapter layer for the Coding Worker (Phase F.1.1).

The supervisor does not know HOW an agent is executed. Two adapters:

  LocalFixtureAdapter  — in-container execution for development/test fixture
                         repos ONLY (agents: simulated / fixture-local-hermes).
                         Never used for production jobs (enforced by callers).
  RemoteWorkerAdapter  — restricted SSH + workerctl protocol to an external,
                         privilege-separated worker machine (Claude Code or
                         Codex on the worker). Not connected until the user
                         provides worker host info (worker/README.md).

Both return the same normalized run-result dict so the supervisor loop,
continuation logic, stall detection and validation stay transport-agnostic.
"""

import json
import os
import subprocess
import time
from pathlib import Path

JOBS_DIR = Path(__file__).resolve().parent


class WorkerUnconfigured(Exception):
    pass


# Bridge socket (container path first, host path fallback for local tests)
BRIDGE_SOCK_CANDIDATES = [
    "/hermes-coding/bridge.sock",
    "/home/<PI_USER>/coding-worker/bridge-sock/bridge.sock",
]


def bridge_sock():
    for p in BRIDGE_SOCK_CANDIDATES:
        if os.path.exists(p):
            return p
    raise WorkerUnconfigured(
        "coding bridge socket not found (bridge not running or container "
        "mount missing; see coding-worker/README.md)"
    )


def bridge_request(payload, timeout=60):
    import socket as _s
    sock = bridge_sock()
    s = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(sock)
        s.sendall((json.dumps(payload) + "\n").encode())
        f = s.makefile("r")
        line = f.readline()
        if not line:
            raise WorkerUnconfigured("bridge closed connection")
        return json.loads(line)
    finally:
        s.close()


def remote_config():
    cfg = json.loads((JOBS_DIR / "worker_config.json").read_text(encoding="utf-8"))
    rcfg = cfg.get("transport", {}).get("remote") or {}
    path = JOBS_DIR / "remote_worker.json"
    if path.exists():
        rcfg = json.loads(path.read_text(encoding="utf-8"))
    if not rcfg or not rcfg.get("host"):
        raise WorkerUnconfigured(
            "no remote worker configured; production jobs require an external "
            "coding worker (see jobs/worker/README.md)"
        )
    return rcfg


def _ssh(rcfg, args, stdin=None, timeout=60):
    cmd = [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new",
        "-i", rcfg["ssh_key"], "-p", str(rcfg.get("port", 22)),
        f"{rcfg['username']}@{rcfg['host']}",
        # exactly one restricted entry point; the server parses arguments
        "workerctl", *args,
    ]
    return subprocess.run(
        cmd, input=stdin, capture_output=True, text=True, timeout=timeout,
    )


class LocalFixtureAdapter:
    """Development/test fixture execution inside the Hermes container.

    Only for repos flagged allow_simulated and jobs with kind='test'.
    """

    name = "local-fixture"

    def run(self, job, rcfg, repo_path, prompt, timeout_s):
        import agents_local
        agent = job.get("agent") or "simulated"
        res = agents_local.run_agent(agent, repo_path, prompt, timeout_s)
        return {
            "exit_code": res["exit_code"],
            "timed_out": res["timed_out"],
            "stdout_tail": res["stdout"][-4000:],
            "stderr_tail": res["stderr"][-4000:],
            "status": "OK",
            "worktree_id": None,
            "branch": None,
            "changed_files": None,   # collected by supervisor via local git
            "diff_stat": None,
            "test_summary": None,
        }


class RemoteWorkerAdapter:
    """Restricted-SSH transport to the external coding worker.

    Protocol (one workerctl call per SSH connection, forced-command key):
      workerctl status / submit (stdin=package) / job_status <id> /
      job_result <id> / job_diff <id> [max_bytes] / job_cancel <id> /
      job_continue <id>
    """

    name = "remote-worker"

    def run(self, job, rcfg, repo_path, prompt, timeout_s):
        cfg = remote_config()
        package = {
            "job_id": job["job_id"],
            "repo_id": job["repository"],
            "kind": job.get("kind"),
            "prompt": prompt,
            "tests_command": job.get("tests_command"),
            "timeout_s": timeout_s,
        }
        p = _ssh(cfg, ["submit", "-"], stdin=json.dumps(package), timeout=120)
        if p.returncode != 0:
            return {"status": "INTERRUPTED", "exit_code": None, "timed_out": False,
                    "stdout_tail": "", "stderr_tail": (p.stderr or "")[-1000:],
                    "worktree_id": None, "branch": None, "changed_files": [],
                    "diff_stat": None, "test_summary": None}
        deadline = timeout_s + 300
        waited = 0
        interval = 15
        while waited < deadline:
            import time
            time.sleep(interval)
            waited += interval
            try:
                s = _ssh(cfg, ["job_status", job["job_id"]], timeout=30)
            except subprocess.TimeoutExpired:
                return {"status": "INTERRUPTED", "exit_code": None,
                        "timed_out": False, "stdout_tail": "",
                        "stderr_tail": "worker unreachable during poll",
                        "worktree_id": None, "branch": None,
                        "changed_files": [], "diff_stat": None,
                        "test_summary": None}
            if s.returncode != 0:
                continue
            info = json.loads(s.stdout or "{}")
            if info.get("state") in ("finished", "failed", "cancelled"):
                r = _ssh(cfg, ["job_result", job["job_id"]], timeout=60)
                try:
                    ev = json.loads(r.stdout or "{}")
                except ValueError:
                    ev = {}
                return {
                    "status": "OK",
                    "exit_code": ev.get("exit_status"),
                    "timed_out": bool(ev.get("timed_out")),
                    "stdout_tail": (ev.get("agent_reason") or "")[-4000:],
                    "stderr_tail": "",
                    "worktree_id": ev.get("worktree_id"),
                    "branch": ev.get("branch"),
                    "changed_files": ev.get("changed_files") or [],
                    "diff_stat": ev.get("diff_stat"),
                    "test_summary": ev.get("test_summary"),
                    "secret_scan": ev.get("secret_scan"),
                }
            if info.get("state") == "interrupted":
                return {"status": "INTERRUPTED", "exit_code": None,
                        "timed_out": False, "stdout_tail": "",
                        "stderr_tail": "worker reported interruption",
                        "worktree_id": info.get("worktree_id"), "branch": None,
                        "changed_files": [], "diff_stat": None,
                        "test_summary": None}
        return {"status": "INTERRUPTED", "exit_code": None, "timed_out": True,
                "stdout_tail": "", "stderr_tail": "remote poll deadline exceeded",
                "worktree_id": None, "branch": None, "changed_files": [],
                "diff_stat": None, "test_summary": None}


class PiSandboxWorkerAdapter:
    """Production default (F.1.1-Pi): disposable worker container on the same
    Pi, managed by the host Coding Bridge over a typed Unix-socket RPC.

    Hermes can only pass: job_id, repo_id, agent name, prompt, tests_command,
    acceptance_criteria, timeout. Images, mounts, paths and commands are
    resolved by the bridge from its own config."""

    name = "pi-sandbox"

    AGENT_MAP = {
        "pi-sandbox-fixture": "fixture",
        "pi-sandbox-claude-code": "claude-code",
        "pi-sandbox-codex": "codex",
        "pi-sandbox-opencode": "opencode",
    }

    def run(self, job, rcfg, repo_path, prompt, timeout_s):
        agent = self.AGENT_MAP.get(job.get("agent") or "")
        if not agent:
            return {"status": "REJECTED", "exit_code": None, "timed_out": False,
                    "stdout_tail": "", "stderr_tail": f"unmapped agent {job.get('agent')!r}",
                    "changed_files": []}
        req = {
            "version": 1, "action": "job_start",
            "job_id": job["job_id"], "repo_id": job["repository"],
            "agent": agent, "prompt": prompt[:100_000],
            "tests_command": job.get("tests_command"),
            "acceptance_criteria": job.get("acceptance_criteria") or [],
            "timeout_s": int(timeout_s),
        }
        try:
            resp = bridge_request(req, timeout=120)
        except WorkerUnconfigured as exc:
            raise
        except (OSError, ValueError) as exc:
            return {"status": "INTERRUPTED", "exit_code": None, "timed_out": False,
                    "stdout_tail": "", "stderr_tail": f"bridge unreachable: {exc}",
                    "changed_files": []}
        if resp.get("error"):
            return {"status": "REJECTED", "exit_code": None, "timed_out": False,
                    "stdout_tail": "", "stderr_tail": f"bridge: {resp.get('error')} "
                                                     f"{json.dumps(resp.get('fields') or resp.get('allowed') or resp.get('detail') or '')[:150]}",
                    "changed_files": []}
        if not resp.get("accepted"):
            return {"status": "BUSY" if resp.get("error") == "busy" else "REJECTED",
                    "exit_code": None, "timed_out": False, "stdout_tail": "",
                    "stderr_tail": f"bridge: {resp}", "changed_files": []}

        deadline = time.time() + timeout_s + 600
        misses = 0
        while time.time() < deadline:
            time.sleep(15)
            try:
                st = bridge_request({"version": 1, "action": "job_status",
                                     "job_id": job["job_id"]}, timeout=30)
                misses = 0
            except (OSError, ValueError, WorkerUnconfigured):
                misses += 1
                if misses >= 8:
                    return {"status": "INTERRUPTED", "exit_code": None,
                            "timed_out": False, "stdout_tail": "",
                            "stderr_tail": "bridge unreachable during poll",
                            "changed_files": []}
                continue
            if st.get("state") not in ("running", None):
                break
        try:
            res = bridge_request({"version": 1, "action": "job_result",
                                  "job_id": job["job_id"]}, timeout=60)
        except (OSError, ValueError, WorkerUnconfigured):
            return {"status": "INTERRUPTED", "exit_code": None, "timed_out": False,
                    "stdout_tail": "", "stderr_tail": "bridge unreachable at result time",
                    "changed_files": []}
        ev = res.get("evidence") or {}
        tail = ev.get("agent_reason") or ""
        if ev.get("oom"):
            tail = (tail + "\n[worker container OOM killed]").strip()
        return {
            "status": "OK",
            "exit_code": ev.get("exit_status"),
            "timed_out": bool(ev.get("timed_out")),
            "oom": bool(ev.get("oom")),
            "stdout_tail": tail[-4000:],
            "stderr_tail": res.get("error") or res.get("note") or "",
            "worktree_id": ev.get("worktree_id"),
            "branch": ev.get("branch"),
            "head_sha": ev.get("head_sha"),
            "changed_files": ev.get("changed_files") or [],
            "test_summary": ev.get("test_summary"),
            "secret_scan": ev.get("secret_scan") or [],
            "isolation": ev.get("isolation"),
        }


def get_adapter(job):
    agent = job.get("agent") or ""
    kind = job.get("kind") or "implementation"
    if agent in ("simulated", "fixture-local-hermes"):
        if kind != "test":
            raise WorkerUnconfigured(
                "local fixture execution is development/test-only (F.1.1); "
                "production jobs must use a sandbox worker agent"
            )
        return LocalFixtureAdapter()
    if agent.startswith("pi-sandbox-"):
        return PiSandboxWorkerAdapter()
    if agent.startswith("remote-"):
        remote_config()  # fail fast at dispatch time if the worker is unconfigured
        return RemoteWorkerAdapter()
    raise WorkerUnconfigured(f"no transport adapter for agent kind: {agent!r}")


def worker_online():
    """Observability helper (counts-only, no content). Bridge-first."""
    try:
        resp = bridge_request({"version": 1, "action": "worker_status"}, timeout=20)
        return bool(resp.get("bridge") == "alive" and resp.get("docker_reachable"))
    except Exception:
        return False
