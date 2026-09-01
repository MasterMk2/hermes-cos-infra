#!/usr/bin/env python3
"""Shared test helpers for the F.1.1-Pi.2 supervisor hardening tests.

Everything runs in-process against per-test sandboxes (temp dirs), so no
test touches the real worker_config.json / jobs.db or any production path.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

INFRA_ROOT = Path(__file__).resolve().parent.parent
SRC = INFRA_ROOT / "src"
for p in (str(SRC), str(SRC / "jobs"), str(SRC / "bridge"),
          str(SRC / "jobs" / "worker")):
    if p not in sys.path:
        sys.path.insert(0, p)


def install_mcp_stub():
    """jobs_mcp.py imports the `mcp` package. Unit tests only exercise the
    tool-call logic, so a minimal in-memory stub keeps them stdlib-only."""
    if "mcp" in sys.modules:
        return
    mcp = types.ModuleType("mcp")
    server = types.ModuleType("mcp.server")
    lowlevel = types.ModuleType("mcp.server.lowlevel")
    stdio = types.ModuleType("mcp.server.stdio")
    mcp_types = types.ModuleType("mcp.types")
    lowlevel_server = types.ModuleType("mcp.server.lowlevel.Server")

    class Server:
        def __init__(self, *args, **kwargs):
            pass

        def create_initialization_options(self):
            return {}

        async def run(self, *args, **kwargs):
            raise NotImplementedError("stub server cannot run")

    class _Node:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    def stdio_server(*args, **kwargs):
        raise NotImplementedError("stub stdio cannot run")

    lowlevel_server.Server = Server
    mcp_types.TextContent = _Node
    mcp_types.Tool = _Node
    mcp_types.ListToolsResult = _Node
    mcp_types.CallToolResult = _Node
    stdio.stdio_server = stdio_server
    lowlevel.Server = lowlevel_server.Server
    server.lowlevel = lowlevel
    mcp.server = server
    mcp.types = mcp_types
    for name, mod in [("mcp", mcp), ("mcp.server", server),
                      ("mcp.server.lowlevel", lowlevel),
                      ("mcp.server.lowlevel.Server", lowlevel_server),
                      ("mcp.server.stdio", stdio), ("mcp.types", mcp_types)]:
        sys.modules[name] = mod


install_mcp_stub()

import jobs_db        # noqa: E402
import supervisor      # noqa: E402


RUNNER_THREADS = []


class Sandbox:
    """Redirects all module-level persistence paths into a temp dir."""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="f11pi2-"))
        self._saved = []
        RUNNER_THREADS.clear()

    def __enter__(self):
        for mod, attrs in (
            (jobs_db, ["JOBS_DIR", "DB_PATH", "ARTIFACTS_DIR", "LOGS_DIR",
                       "PROMPTS_DIR"]),
            (supervisor, ["CONFIG_PATH"]),
        ):
            for a in attrs:
                self._saved.append((mod, a, getattr(mod, a)))
        jobs_db.JOBS_DIR = self.dir
        jobs_db.DB_PATH = self.dir / "jobs.db"
        jobs_db.ARTIFACTS_DIR = self.dir / "artifacts"
        jobs_db.LOGS_DIR = self.dir / "logs"
        jobs_db.PROMPTS_DIR = self.dir / "prompts"
        supervisor.CONFIG_PATH = self.dir / "worker_config.json"
        jobs_db.init_db()
        return self

    def __exit__(self, *exc):
        # let in-process runner threads finish BEFORE restoring paths —
        # otherwise they race the teardown (observed as stray exceptions)
        for t in RUNNER_THREADS:
            t.join(180)
        for mod, a, val in reversed(self._saved):
            setattr(mod, a, val)
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)
        return False

    # -- config ------------------------------------------------------------
    def write_config(self, repo_name, repo_path, test_commands,
                     max_attempts=6, no_progress_strikes=50,
                     agent_timeout_s=90, test_timeout_s=180,
                     allow_simulated=True, allowed_paths=("pkg/", "tests/",
                                                          "README.md")):
        cfg = {
            "worker": {"kind": "test-sandbox", "enabled": True,
                       "offline": False},
            "default_agent": None,
            "limits": {
                "max_attempts": max_attempts,
                "agent_timeout_s": agent_timeout_s,
                "test_timeout_s": test_timeout_s,
                "no_progress_strikes": no_progress_strikes,
            },
            "transport": {"kind": "local-fixture", "remote": None},
            "repositories": {
                repo_name: {
                    "path": str(repo_path),
                    "test_commands": list(test_commands),
                    "allowed_paths": list(allowed_paths),
                    "allow_simulated": allow_simulated,
                },
            },
        }
        supervisor.CONFIG_PATH.write_text(json.dumps(cfg), encoding="utf-8")
        return cfg


def run_git(repo, *args, check=True):
    env = dict(os.environ)
    env.setdefault("GIT_AUTHOR_NAME", "tester")
    env.setdefault("GIT_AUTHOR_EMAIL", "tester@example.invalid")
    env.setdefault("GIT_COMMITTER_NAME", "tester")
    env.setdefault("GIT_COMMITTER_EMAIL", "tester@example.invalid")
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                       text=True, env=env)
    if check and r.returncode != 0:
        raise AssertionError(f"git {args} failed: {r.stderr}")
    return r


TEST_GREETER = '''import unittest
from pkg.greeter import greet, farewell


class TestGreeter(unittest.TestCase):
    def test_greet(self):
        self.assertEqual(greet("x"), "Hello, x!")

    def test_farewell(self):
        self.assertEqual(farewell("x"), "Goodbye, x!")
'''


def make_fixture_repo(root):
    """Fixture repo: pkg/ + tests/ committed; pkg/greeter.py intentionally
    absent (the sim agent creates it during the job)."""
    repo = root / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    (repo / "README.md").write_text("fixture repo\n", encoding="utf-8")
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "tests" / "test_greeter.py").write_text(TEST_GREETER,
                                                    encoding="utf-8")
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-q", "-m", "initial")
    return repo


def python_cmd(args):
    """Allowlist-safe test command string for this interpreter."""
    return f'"{sys.executable}" {args}'


PASSING_TEST_CMD = python_cmd("-m unittest discover -s tests -v")
FAILING_TEST_CMD = python_cmd('-c "raise SystemExit(1)"')

TEST_CRITERIA = [
    {"id": "ac_file", "description": "greeter module exists",
     "verification": {"kind": "file_exists",
                      "params": {"path": "pkg/greeter.py"}}},
    {"id": "ac_scope", "description": "changes stay in scope",
     "verification": {"kind": "git_scope", "params": {}}},
    {"id": "ac_tests", "description": "unit tests pass",
     "verification": {"kind": "test_command", "params": {}}},
]


def spawn_runner_in_thread():
    """Replace supervisor._spawn_runner with an in-process runner thread so
    tests drive the real run_job_loop without detached subprocesses."""
    started = []

    def fake_spawn(job_id):
        t = threading.Thread(target=supervisor.run_job_loop, args=(job_id,),
                             daemon=True)
        t.start()
        RUNNER_THREADS.append(t)
        started.append(job_id)
        return os.getpid()

    return fake_spawn, started


TERMINAL_STATUSES = {"IMPLEMENTATION_ACCEPTED", "WAITING_HUMAN", "CANCELLED",
                      "FAILED", "FAILED_TO_START", "COMPLETED"}


def wait_for(job_id, statuses, timeout=90.0, poll=0.1):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        job = jobs_db.get_job(job_id)
        if job is None:
            time.sleep(poll)
            continue
        last = job["status"]
        if last in statuses:
            return job
        time.sleep(poll)
    raise TimeoutError(f"job {job_id} stuck at {last!r}")


def wait_terminal(job_id, timeout=90.0):
    return wait_for(job_id, TERMINAL_STATUSES, timeout=timeout)


def wait_status(job_id, status, timeout=30.0):
    return wait_for(job_id, {status}, timeout=timeout)
