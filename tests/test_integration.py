#!/usr/bin/env python3
"""F.1.1-Pi.2 integration tests (fixture E2E).

Covers: submit -> agent -> validation -> continuation -> accept,
false-completion refusal, committed-change visibility E2E, cancellation
integration, double dispatch, crash/reconcile."""

import json
import unittest

import helpers  # noqa: F401  (sys.path setup)
from helpers import (PASSING_TEST_CMD, Sandbox, TEST_CRITERIA,
                     make_fixture_repo, spawn_runner_in_thread, wait_terminal)

import jobs_db     # noqa: E402
import jobs_mcp    # noqa: E402
import supervisor  # noqa: E402


def _create_job(sb, repo, behavior, max_attempts=6, no_progress_strikes=50):
    sb.write_config("r", repo, [PASSING_TEST_CMD],
                    max_attempts=max_attempts,
                    no_progress_strikes=no_progress_strikes)
    res = jobs_mcp._call("job_create", {
        "goal": f"g\n#SIM-BEHAVIOR: {behavior}", "repository": "r",
        "kind": "test", "agent": "simulated",
        "tests_command": PASSING_TEST_CMD,
        "acceptance_criteria": TEST_CRITERIA,
    })
    return res["job_id"]


def _dispatch_with_thread_runner(job_id):
    fake_spawn, _ = spawn_runner_in_thread()
    orig = supervisor._spawn_runner
    supervisor._spawn_runner = fake_spawn
    try:
        res = jobs_mcp._call("job_dispatch", {"job_id": job_id})
    finally:
        supervisor._spawn_runner = orig
    return res


class TestFixtureE2E(unittest.TestCase):
    """52: submit -> agent -> validation -> continuation -> accept."""

    def test_full_accept_after_continuation(self):
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            job_id = _create_job(sb, repo, "partial_then_done")
            res = _dispatch_with_thread_runner(job_id)
            self.assertEqual(res["status"], "DISPATCHING")
            final = wait_terminal(job_id, timeout=180)
            self.assertEqual(final["status"], "IMPLEMENTATION_ACCEPTED",
                             final["failure_reason"])
            # re-read once the runner thread has settled (Sandbox joins it)
            final = jobs_db.get_job(job_id)
            self.assertEqual(final["review_status"], "WAITING_REVIEW")
            self.assertNotEqual(final["status"], "COMPLETED")
            runs = jobs_db.runs_for_job(job_id)
            self.assertEqual(len(runs), 2, "continuation ran a second attempt")
            self.assertEqual(final["tests_status"], "PASS")
            statuses = {a["id"]: a["status"]
                        for a in final["acceptance_results"]}
            self.assertEqual(statuses,
                             {"ac_file": "PASS", "ac_scope": "PASS",
                              "ac_tests": "PASS"})
            # M8 coherence: acceptance stamps finished_at
            self.assertIsNotNone(final["finished_at"])

    def test_false_completion_never_accepted(self):
        """53: a confident self-report with failing tests can never reach
        IMPLEMENTATION_ACCEPTED — the single most important negative gate."""
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            job_id = _create_job(sb, repo, "false_completion",
                                 no_progress_strikes=2)
            _dispatch_with_thread_runner(job_id)
            final = wait_terminal(job_id, timeout=180)
            self.assertNotEqual(final["status"], "IMPLEMENTATION_ACCEPTED")
            self.assertNotEqual(final["status"], "COMPLETED")
            self.assertEqual(final["status"], "WAITING_HUMAN")
            self.assertIn("no progress", final["failure_reason"])
            runs = jobs_db.runs_for_job(job_id)
            self.assertGreaterEqual(len(runs), 3)

    def test_committed_change_e2e_secret_scan_stops_acceptance(self):
        """54: agent commits its change (clean tree) -> changed file still
        detected, secret scan sees it, job halts for human review."""
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            job_id = _create_job(sb, repo, "commit_secret")
            _dispatch_with_thread_runner(job_id)
            final = wait_terminal(job_id, timeout=120)
            self.assertNotEqual(final["status"], "IMPLEMENTATION_ACCEPTED")
            self.assertEqual(final["status"], "WAITING_HUMAN")
            self.assertIn("secret-like content", final["failure_reason"])
            self.assertIn("credential_assignment", final["failure_reason"])
            # the committed file is in the canonical changed set
            self.assertIn("secrets/leaked.txt", final["git_changed_files"])

    def test_cancellation_integration(self):
        """55: job_cancel mid-run -> worker term -> CANCELLED; no validation,
        no acceptance afterwards."""
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            job_id = _create_job(sb, repo, "sleep:4")
            res = _dispatch_with_thread_runner(job_id)
            self.assertEqual(res["status"], "DISPATCHING")

            from helpers import wait_status
            wait_status(job_id, "RUNNING")
            jobs_mcp._call("job_cancel", {"job_id": job_id})
            final = wait_terminal(job_id, timeout=60)
            self.assertEqual(final["status"], "CANCELLED")
            self.assertEqual(final["acceptance_results"], [])

            # a cancelled job cannot be resumed or re-dispatched
            cont = jobs_mcp._call("job_continue", {"job_id": job_id})
            self.assertEqual(cont.get("error"), "not_resumable")
            disp = jobs_mcp._call("job_dispatch", {"job_id": job_id})
            self.assertEqual(disp.get("error"), "terminal")

    def test_double_dispatch_integration_single_runner(self):
        """56: two dispatches for the same job -> one runner only."""
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            job_id = _create_job(sb, repo, "no_progress", max_attempts=1)
            r1 = _dispatch_with_thread_runner(job_id)
            self.assertEqual(r1["status"], "DISPATCHING")
            # second dispatch while DISPATCHING/RUNNING must be refused
            r2 = jobs_mcp._call("job_dispatch", {"job_id": job_id})
            self.assertIn(r2.get("error"),
                          ("already_claimed", "already_active"))

    def test_crash_reconcile_never_completes(self):
        """Runner death is recoverable (AGENT_STOPPED), never completion."""
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            job_id = _create_job(sb, repo, "no_progress")
            # simulate a crashed supervisor: RUNNING with no live runner
            jobs_db.transition(job_id, "RUNNING")
            out = jobs_mcp._call("job_reconcile", {})
            self.assertTrue(any(
                e["job_id"] == job_id and e["now"].startswith("AGENT_STOPPED")
                for e in out["reconciled"]))
            job = jobs_db.get_job(job_id)
            self.assertEqual(job["status"], "AGENT_STOPPED")
            self.assertNotIn(job["status"], ("COMPLETED",
                                             "IMPLEMENTATION_ACCEPTED"))
            # and it is resumable
            res = _dispatch_with_thread_runner(job_id)
            self.assertEqual(res["status"], "DISPATCHING")
            wait_terminal(job_id, timeout=120)


if __name__ == "__main__":
    unittest.main()
