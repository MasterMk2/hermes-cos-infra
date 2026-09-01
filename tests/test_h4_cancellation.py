#!/usr/bin/env python3
"""H4: cancellation is terminal intent.

- cancel during RUNNING -> final CANCELLED (never VALIDATING/ACCEPTED)
- cancel landing at the AGENT_STOPPED -> VALIDATING boundary -> CANCELLED
- worker finalization (bridge + workerctl) can never overwrite 'cancelled'
"""

import threading
import unittest

import helpers  # noqa: F401  (sys.path setup)
from helpers import (PASSING_TEST_CMD, Sandbox, TEST_CRITERIA,
                     make_fixture_repo, spawn_runner_in_thread, wait_status,
                     wait_terminal)

import bridge            # noqa: E402
import jobs_db           # noqa: E402
import jobs_mcp          # noqa: E402
import supervisor        # noqa: E402
import workerctl_server  # noqa: E402


def _create_job(sb, repo, behavior):
    res = jobs_mcp._call("job_create", {
        "goal": f"g\n#SIM-BEHAVIOR: {behavior}", "repository": "r",
        "kind": "test", "agent": "simulated",
        "tests_command": PASSING_TEST_CMD,
        "acceptance_criteria": TEST_CRITERIA,
    })
    return res["job_id"]


class TestCancelDuringRun(unittest.TestCase):
    def test_cancel_while_agent_running_final_cancelled(self):
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            sb.write_config("r", repo, [PASSING_TEST_CMD])
            fake_spawn, _ = spawn_runner_in_thread()
            orig_spawn = supervisor._spawn_runner
            supervisor._spawn_runner = fake_spawn
            try:
                job_id = _create_job(sb, repo, "sleep:4")
                res = jobs_mcp._call("job_dispatch", {"job_id": job_id})
                self.assertEqual(res["status"], "DISPATCHING")
                wait_status(job_id, "RUNNING")
                # user/agent cancels mid-run
                cres = jobs_mcp._call("job_cancel", {"job_id": job_id})
                self.assertEqual(cres["status"], "CANCELLED")
                final = wait_terminal(job_id, timeout=60)
                self.assertEqual(final["status"], "CANCELLED")
                self.assertIsNotNone(final.get("cancel_requested_at"))
                self.assertIsNotNone(final.get("finished_at"))
                # validation never ran for the cancelled job
                self.assertEqual(final["acceptance_results"], [])
            finally:
                supervisor._spawn_runner = orig_spawn

    def test_cancel_landing_at_validating_is_never_accepted(self):
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            sb.write_config("r", repo, [PASSING_TEST_CMD])
            fake_spawn, _ = spawn_runner_in_thread()
            orig_spawn = supervisor._spawn_runner
            supervisor._spawn_runner = fake_spawn
            orig_transition = jobs_db.transition

            def cancel_at_validating(job_id, new_status, failure_reason=None):
                if new_status == "VALIDATING":
                    return orig_transition(job_id, "CANCELLED",
                                           failure_reason="cancel injected at VALIDATING")
                return orig_transition(job_id, new_status, failure_reason)

            jobs_db.transition = cancel_at_validating
            try:
                job_id = _create_job(sb, repo, "full")
                jobs_mcp._call("job_dispatch", {"job_id": job_id})
                final = wait_terminal(job_id, timeout=60)
                self.assertEqual(final["status"], "CANCELLED")
                self.assertNotEqual(final["status"], "IMPLEMENTATION_ACCEPTED")
            finally:
                jobs_db.transition = orig_transition
                supervisor._spawn_runner = orig_spawn

    def test_supervisor_cas_cannot_accept_after_cancel(self):
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            sb.write_config("r", repo, [PASSING_TEST_CMD])
            fake_spawn, _ = spawn_runner_in_thread()
            orig_spawn = supervisor._spawn_runner
            supervisor._spawn_runner = fake_spawn
            try:
                job_id = _create_job(sb, repo, "full")
                jobs_db.transition(job_id, "CANCELLED",
                                   failure_reason="cancel before dispatch")
                res = jobs_mcp._call("job_dispatch", {"job_id": job_id})
                self.assertEqual(res.get("error"), "terminal")
            finally:
                supervisor._spawn_runner = orig_spawn


class TestBridgeFinalStateCAS(unittest.TestCase):
    """Worker thread finishing after a cancel must preserve 'cancelled'."""

    def _bridge_job_dir(self, job_id):
        d = bridge.ROOT / "jobs" / job_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_bridge_finalize_after_cancel_preserves_cancelled(self):
        saved_root = bridge.ROOT
        with Sandbox() as sb:
            bridge.ROOT = sb.dir
            try:
                job_id = "job_" + "a" * 10
                self._bridge_job_dir(job_id)
                bridge.write_state(job_id, {
                    "job_id": job_id, "state": "cancelled",
                    "cancel_requested_at": bridge.now(),
                })
                # worker thread finishing 'successfully' afterwards
                st = {"job_id": job_id, "state": "running",
                      "evidence": {"changed_files": []}}
                final = bridge._commit_terminal_state(job_id, st, "finished")
                self.assertEqual(final, "cancelled")
                self.assertEqual(bridge.read_state(job_id)["state"], "cancelled")
                self.assertIsNotNone(
                    bridge.read_state(job_id).get("cancel_requested_at"))
            finally:
                bridge.ROOT = saved_root

    def test_bridge_finalize_without_cancel_writes_finished(self):
        saved_root = bridge.ROOT
        with Sandbox() as sb:
            bridge.ROOT = sb.dir
            try:
                job_id = "job_" + "b" * 10
                self._bridge_job_dir(job_id)
                bridge.write_state(job_id, {"job_id": job_id, "state": "running"})
                st = {"job_id": job_id, "state": "running"}
                final = bridge._commit_terminal_state(job_id, st, "finished")
                self.assertEqual(final, "finished")
            finally:
                bridge.ROOT = saved_root


class TestWorkerctlFinalStateCAS(unittest.TestCase):
    def test_workerctl_finalize_after_cancel_preserves_cancelled(self):
        with Sandbox() as sb:
            saved = (workerctl_server.JOBS_DIR,)
            workerctl_server.JOBS_DIR = sb.dir
            try:
                job_id = "job_c_test"
                workerctl_server.write_state(job_id, {
                    "job_id": job_id, "state": "cancelled",
                    "cancel_requested_at": workerctl_server.now_iso(),
                })
                state = {"job_id": job_id, "state": "running",
                         "evidence": {"changed_files": []}}
                final = workerctl_server._commit_terminal_state(
                    job_id, state, "finished")
                self.assertEqual(final, "cancelled")
                self.assertEqual(
                    workerctl_server.read_state(job_id)["state"], "cancelled")
            finally:
                workerctl_server.JOBS_DIR = saved[0]

    def test_workerctl_submit_refuses_cancelled_job(self):
        with Sandbox() as sb:
            saved = (workerctl_server.JOBS_DIR,)
            workerctl_server.JOBS_DIR = sb.dir
            try:
                job_id = "job_d_test"
                workerctl_server.write_state(job_id, {
                    "job_id": job_id, "state": "cancelled",
                    "cancel_requested_at": workerctl_server.now_iso(),
                })
                orig_load = workerctl_server.load_config
                workerctl_server.load_config = lambda: {
                    "repositories": {"r": {"path": "x"}}}

                class _FakeIO:
                    def __init__(self):
                        self.data = None

                    def write(self, s):
                        self.data = s

                    def flush(self):
                        pass

                import io
                import contextlib
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    workerctl_server.cmd_submit({
                        "job_id": job_id, "repo_id": "r", "prompt": "p"})
                out = json_loads(buf.getvalue())
                self.assertEqual(out.get("accepted"), False)
                self.assertEqual(out.get("state"), "cancelled")
            finally:
                workerctl_server.JOBS_DIR = saved[0]
                workerctl_server.load_config = orig_load


def json_loads(s):
    import json
    return json.loads(s)


if __name__ == "__main__":
    unittest.main()
