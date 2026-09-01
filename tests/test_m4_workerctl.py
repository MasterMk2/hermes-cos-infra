#!/usr/bin/env python3
"""M4: workerctl internal commands must not be reachable over the SSH
surface. Item 45: runner pid files carry PID + start time."""

import contextlib
import io
import json
import os
import unittest

import helpers  # noqa: F401  (sys.path setup)
from helpers import Sandbox

import jobs_db           # noqa: E402
import supervisor        # noqa: E402
import workerctl_server  # noqa: E402


class TestM4InternalCommandNotSshExposed(unittest.TestCase):
    def _run_main(self, argv, env_extra=None):
        import sys as _sys
        old_argv = _sys.argv
        old_env = {}
        _sys.argv = ["workerctl"] + list(argv)
        for k, v in (env_extra or {}).items():
            old_env[k] = os.environ.get(k)
            os.environ[k] = v
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                workerctl_server.main()
            return buf.getvalue()
        finally:
            _sys.argv = old_argv
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_ssh_request_rejects_internal_command(self):
        out = self._run_main(["request"], {"SSH_ORIGINAL_COMMAND":
                                           "workerctl _run_job job_1234567890"})
        data = json.loads(out or "{}")
        self.assertEqual(data.get("error"),
                         "internal_command_forbidden_over_ssh")

    def test_ssh_direct_env_rejects_internal_command(self):
        out = self._run_main(["_run_job", "job_1234567890"],
                             {"SSH_ORIGINAL_COMMAND":
                              "workerctl _run_job job_1234567890"})
        data = json.loads(out or "{}")
        self.assertEqual(data.get("error"),
                         "internal_command_forbidden_over_ssh")

    def test_ssh_request_still_allows_public_surface(self):
        with Sandbox() as sb:
            saved = workerctl_server.WORKER_ROOT
            workerctl_server.WORKER_ROOT = sb.dir
            try:
                out = self._run_main(
                    ["request"],
                    {"SSH_ORIGINAL_COMMAND":
                     "workerctl job_status job_1234567890"})
                data = json.loads(out or "{}")
                # public commands pass the guard (unknown job is fine here)
                self.assertNotIn("internal_command_forbidden", data)
                self.assertEqual(data.get("job_id"), "job_1234567890")
                self.assertEqual(data.get("state"), "unknown")
            finally:
                workerctl_server.WORKER_ROOT = saved

    def test_local_invocation_permits_internal_command(self):
        """Internal subprocess (no SSH_ORIGINAL_COMMAND) may run _run_job."""
        with Sandbox() as sb:
            saved = workerctl_server.WORKER_ROOT
            workerctl_server.WORKER_ROOT = sb.dir
            try:
                # no config file in the sandbox -> the internal path is
                # actually reached (FileNotFoundError from load_config proves
                # the guard did NOT reject the local invocation)
                with self.assertRaises(FileNotFoundError):
                    self._run_main(["_run_job", "job_1234567890"])
            finally:
                workerctl_server.WORKER_ROOT = saved

    def test_ssh_rejects_all_internal_commands(self):
        for cmd in workerctl_server.INTERNAL_COMMANDS:
            out = self._run_main(
                ["request"],
                {"SSH_ORIGINAL_COMMAND": f"workerctl {cmd} job_1234567890"})
            data = json.loads(out or "{}")
            self.assertEqual(data.get("error"),
                             "internal_command_forbidden_over_ssh")


class TestPidFileStartTicks(unittest.TestCase):
    def test_pid_file_json_roundtrip(self):
        with Sandbox():
            job_id = "job_1234567890"
            pid_file = jobs_db.LOGS_DIR / f"runner-{job_id}.pid"
            pid_file.parent.mkdir(parents=True, exist_ok=True)
            pid_file.write_text(json.dumps({"pid": 424242,
                                            "start_ticks": "12345"}))
            pid, ticks = supervisor.read_runner_pid_file(job_id)
            self.assertEqual((pid, ticks), (424242, "12345"))

    def test_legacy_plain_pid_file_still_reads(self):
        with Sandbox():
            job_id = "job_1234567890"
            pid_file = jobs_db.LOGS_DIR / f"runner-{job_id}.pid"
            pid_file.parent.mkdir(parents=True, exist_ok=True)
            pid_file.write_text("424242")
            pid, ticks = supervisor.read_runner_pid_file(job_id)
            self.assertEqual((pid, ticks), (424242, None))

    def test_missing_pid_file_reads_none(self):
        with Sandbox():
            pid, ticks = supervisor.read_runner_pid_file("job_1234567890")
            self.assertIsNone(pid)
            self.assertIsNone(ticks)

    @unittest.skipUnless(os.name == "posix",
                         "/proc start-time comparison is POSIX-only")
    def test_reused_pid_with_different_start_time_is_dead(self):
        mine = os.getpid()
        real_ticks = supervisor._runner_start_ticks(mine)
        self.assertTrue(supervisor._pid_alive(mine, real_ticks))
        self.assertFalse(supervisor._pid_alive(mine, "999999999"),
                         "mismatched start time => PID was reused => dead")


if __name__ == "__main__":
    unittest.main()
