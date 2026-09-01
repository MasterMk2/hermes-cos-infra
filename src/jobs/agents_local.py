#!/usr/bin/env python3
"""Agent execution layer — DEVELOPMENT/TEST FIXTURE ONLY (Phase F.1.1).

The agents here run inside the Hermes container next to persistent personal
data. Runtime audits (2026-09-01) proved that:
  - `--ignore-rules` does NOT measurably remove personal context injection
    (prompt-size composition identical with and without the flag)
  - the container agent can read /opt/data/.env and all personal data
Therefore this module is the LOCAL FIXTURE adapter backend only, used for
supervisor regression tests on allowlisted fixture repos (job kind='test').
Production implementation agents run on the external worker via the
restricted-SSH transport (transport.RemoteWorkerAdapter), never here.

agent kinds (fixture only):
  simulated              — deterministic test fixture (scripts repo edits)
  fixture-local-hermes   — real hermes -z in-container; regression use only
                           (formerly 'hermes-cli'; renamed to make its
                           fixture status explicit)
"""

import os
import subprocess
import sys
from pathlib import Path

HERMES_BIN = "/opt/hermes/.venv/bin/hermes"
SIM_AGENT = str(Path(__file__).resolve().parent / "test_fixtures" / "sim_agent.py")
# Hermes data home (contains config.yaml / .env). Derived from this module's
# location (jobs/ lives under the Hermes data dir) so the agent finds the
# provider credentials even when the parent env was sanitized (e.g. MCP spawn).
_HERMES_DATA_HOME = str(Path(__file__).resolve().parent.parent)


def _agent_env():
    env = dict(os.environ)
    env.setdefault("HERMES_HOME", _HERMES_DATA_HOME)
    env.setdefault("HOME", _HERMES_DATA_HOME)
    return env


def run_agent(agent, cwd, prompt, timeout_s):
    if agent in ("fixture-local-hermes", "hermes-cli"):
        argv = [HERMES_BIN, "--ignore-rules", "--yolo", "-z", prompt]
    elif agent == "simulated":
        # sys.executable (not the POSIX-only "python3") so the fixture runs
        # identically on the Pi and on any dev machine running the tests
        argv = [sys.executable, SIM_AGENT, prompt]
    else:
        return {"exit_code": None, "timed_out": False,
                "stdout": "", "stderr": f"unknown agent kind: {agent}"}
    try:
        proc = subprocess.run(
            argv, cwd=str(cwd), capture_output=True, text=True, timeout=timeout_s,
            env=_agent_env(),
        )
        return {
            "exit_code": proc.returncode,
            "timed_out": False,
            "stdout": proc.stdout or "",
            "stderr": (proc.stderr or "")[-4000:],
        }
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"")
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        err = (exc.stderr or b"")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        return {
            "exit_code": None,
            "timed_out": True,
            "stdout": out[-4000:],
            "stderr": err[-4000:] or f"agent timed out after {timeout_s}s",
        }
