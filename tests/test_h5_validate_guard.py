#!/usr/bin/env python3
"""H5: job_validate is adapter-aware. Local deterministic validation is only
meaningful for local-fixture jobs; for pi-sandbox/remote jobs the worktree
lives on the worker, so a local re-validation would fabricate evidence.
Reject WITHOUT mutating acceptance_results / status / tests fields."""

import unittest

import helpers  # noqa: F401  (sys.path setup)
from helpers import PASSING_TEST_CMD, Sandbox, TEST_CRITERIA, make_fixture_repo

import jobs_db   # noqa: E402
import jobs_mcp  # noqa: E402


class TestProductionValidateGuard(unittest.TestCase):
    def _make_sandbox_job(self, agent, kind="implementation"):
        """Create the job directly in the DB (no dispatch needed)."""
        return jobs_db.create_job(
            goal="production job", repository="r",
            acceptance_criteria=TEST_CRITERIA, agent=agent, kind=kind,
            tests_command=PASSING_TEST_CMD)

    def test_pi_sandbox_job_validate_rejected_without_mutation(self):
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            sb.write_config("r", repo, [PASSING_TEST_CMD])
            jid = self._make_sandbox_job("pi-sandbox-fixture")
            before = jobs_db.get_job(jid)
            res = jobs_mcp._call("job_validate", {"job_id": jid})
            self.assertEqual(res.get("error"),
                             "validation_must_run_via_worker_adapter")
            self.assertEqual(res.get("adapter"), "pi-sandbox")
            after = jobs_db.get_job(jid)
            # NO state mutation on reject
            self.assertEqual(after["acceptance_results"],
                             before["acceptance_results"])
            self.assertEqual(after["tests_status"], before["tests_status"])
            self.assertEqual(after["status"], before["status"])
            self.assertEqual(after["tests_summary"], before["tests_summary"])
            self.assertEqual(after["failure_reason"], before["failure_reason"])

    def test_pi_sandbox_claude_code_job_rejected_too(self):
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            sb.write_config("r", repo, [PASSING_TEST_CMD])
            jid = self._make_sandbox_job("pi-sandbox-claude-code")
            res = jobs_mcp._call("job_validate", {"job_id": jid})
            self.assertEqual(res.get("error"),
                             "validation_must_run_via_worker_adapter")

    def test_remote_job_rejected_when_unconfigured(self):
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            sb.write_config("r", repo, [PASSING_TEST_CMD])
            jid = self._make_sandbox_job("remote-codex")
            before = jobs_db.get_job(jid)
            res = jobs_mcp._call("job_validate", {"job_id": jid})
            self.assertIn("error", res)
            after = jobs_db.get_job(jid)
            self.assertEqual(after["acceptance_results"],
                             before["acceptance_results"])
            self.assertEqual(after["status"], before["status"])

    def test_fixture_job_validate_permitted(self):
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            sb.write_config("r", repo, [PASSING_TEST_CMD])
            jid = jobs_db.create_job(
                goal="fixture job", repository="r",
                acceptance_criteria=TEST_CRITERIA, agent="simulated",
                kind="test", tests_command=PASSING_TEST_CMD)
            jobs_db.update_job(jid, git_changed_files=["pkg/greeter.py"])
            res = jobs_mcp._call("job_validate", {"job_id": jid})
            self.assertNotIn("error", res)
            self.assertIn("acceptance_results", res)
            after = jobs_db.get_job(jid)
            self.assertEqual(len(after["acceptance_results"]), 3)


if __name__ == "__main__":
    unittest.main()
