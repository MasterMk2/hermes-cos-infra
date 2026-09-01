#!/usr/bin/env python3
"""H1: a test_command criterion passes ONLY when the registered test was
executed and exited 0. NOT_RUN / UNKNOWN / MISSING / ERROR / TIMEOUT /
BLOCKED are always UNMET. Creation time enforces the correlation too."""

import unittest

import helpers  # noqa: F401  (sys.path setup)
from helpers import (PASSING_TEST_CMD, Sandbox, TEST_CRITERIA,
                     make_fixture_repo)

import jobs_db        # noqa: E402
import jobs_mcp       # noqa: E402
import supervisor     # noqa: E402
import validator      # noqa: E402
import bridge         # noqa: E402
import workerctl_server  # noqa: E402


def _criteria(kind="test_command"):
    return [{"id": "ac1", "description": "d", "verification":
             {"kind": kind, "params": {}}}]


class TestAcceptanceDecision(unittest.TestCase):
    """Test B: tests_status=NOT_RUN can never accept."""

    def _decision(self, tests_status, kind="test_command"):
        validation = [{"id": "ac1", "verification": kind, "status": "PASS"}]
        return supervisor.acceptance_decision(validation, tests_status)

    def test_not_run_never_accepts(self):
        self.assertFalse(self._decision("NOT_RUN"))

    def test_unknown_missing_error_timeout_blocked_never_accept(self):
        for status in ("UNKNOWN", "MISSING", "ERROR", "TIMEOUT", "BLOCKED",
                       "FAIL", "", None):
            self.assertFalse(self._decision(status), f"status={status!r}")

    def test_executed_pass_accepts(self):
        self.assertTrue(self._decision("PASS"))

    def test_jobs_without_test_criteria_do_not_require_tests(self):
        validation = [{"id": "a", "verification": "file_exists",
                       "status": "PASS"}]
        self.assertTrue(supervisor.acceptance_decision(validation, "NOT_RUN"))

    def test_empty_or_failing_validation_never_accepts(self):
        self.assertFalse(supervisor.acceptance_decision([], "PASS"))
        validation = [{"id": "a", "verification": "file_exists",
                       "status": "FAIL"}]
        self.assertFalse(supervisor.acceptance_decision(validation, "PASS"))


class TestCreationTimeValidation(unittest.TestCase):
    """Test A: test criterion without test command -> job_create rejected."""

    def test_create_rejected_without_tests_command(self):
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            sb.write_config("r", repo, [PASSING_TEST_CMD])
            res = jobs_mcp._call("job_create", {
                "goal": "g", "repository": "r", "kind": "test",
                "agent": "simulated",
                "acceptance_criteria": _criteria("test_command"),
            })
            self.assertEqual(res.get("error"),
                             "test_command_required_for_test_criterion")
            self.assertEqual(jobs_db.list_jobs(limit=100), [])

    def test_create_rejected_with_unknown_kind(self):
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            sb.write_config("r", repo, [PASSING_TEST_CMD])
            res = jobs_mcp._call("job_create", {
                "goal": "g", "repository": "r", "kind": "test",
                "agent": "simulated",
                "acceptance_criteria": _criteria("magic_kind"),
            })
            self.assertEqual(res.get("error"), "unknown_verification_kind")

    def test_create_ok_with_tests_command(self):
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            sb.write_config("r", repo, [PASSING_TEST_CMD])
            res = jobs_mcp._call("job_create", {
                "goal": "g", "repository": "r", "kind": "test",
                "agent": "simulated", "tests_command": PASSING_TEST_CMD,
                "acceptance_criteria": _criteria("test_command"),
            })
            self.assertIn("job_id", res)


class TestExecutedTests(unittest.TestCase):
    """Tests C & D: executed test with exit 0 -> PASS; nonzero -> FAIL."""

    def _evaluate(self, cmd, repo, allowlist=None):
        job = {"acceptance_criteria": _criteria("test_command"),
               "tests_command": cmd}
        rcfg = {"test_commands": [cmd] if allowlist is None else list(allowlist),
                "allowed_paths": ["pkg/", "tests/"]}
        return validator.evaluate(job, rcfg, str(repo), [])

    def test_executed_exit_zero_passes(self):
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            results, tstat, _ = self._evaluate(
                helpers.python_cmd('-c "pass"'), repo)
            self.assertEqual(tstat, "PASS")
            self.assertEqual(results[0]["status"], "PASS")

    def test_executed_nonzero_fails(self):
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            results, tstat, _ = self._evaluate(
                helpers.python_cmd('-c "raise SystemExit(3)"'), repo)
            self.assertEqual(tstat, "FAIL")
            self.assertEqual(results[0]["status"], "FAIL")

    def test_unregistered_command_is_blocked_not_pass(self):
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            registered = helpers.python_cmd('-c "pass"')
            mutated = registered + " --extra"  # not the registered string
            results, tstat, _ = self._evaluate(mutated, repo,
                                               allowlist=[registered])
            self.assertEqual(tstat, "BLOCKED")
            self.assertEqual(results[0]["status"], "FAIL")

    def test_no_command_registered_is_blocked_not_pass(self):
        job = {"acceptance_criteria": _criteria("test_command"),
               "tests_command": None}
        results, tstat, _ = validator.evaluate(
            job, {"test_commands": ["x"]}, "whatever", [])
        # BLOCKED is fail-closed: it can never satisfy a test_command criterion
        self.assertEqual(tstat, "BLOCKED")
        self.assertEqual(results[0]["status"], "FAIL")


class TestBridgeAndWorkerctlCriterionGate(unittest.TestCase):
    """H1 at the worker boundary: the criterion tracks the actual test run."""

    def _criterion(self):
        return [{"id": "ac_tests", "description": "d",
                 "verification": {"kind": "test_command", "params": {}}}]

    def test_bridge_criterion_fails_when_tests_not_run(self):
        res = bridge.evaluate_criteria(self._criterion(), {}, "wt",
                                       tests_result={"status": "NOT_RUN"})
        self.assertEqual(res[0]["status"], "FAIL")
        self.assertIn("NOT_RUN", res[0]["detail"])

    def test_bridge_criterion_fails_on_timeout(self):
        res = bridge.evaluate_criteria(self._criterion(), {}, "wt",
                                       tests_result={"status": "TIMEOUT"})
        self.assertEqual(res[0]["status"], "FAIL")

    def test_bridge_criterion_passes_only_on_pass(self):
        res = bridge.evaluate_criteria(self._criterion(), {}, "wt",
                                       tests_result={"status": "PASS"})
        self.assertEqual(res[0]["status"], "PASS")

    def test_bridge_criterion_fails_without_any_test_result(self):
        res = bridge.evaluate_criteria(self._criterion(), {}, "wt",
                                       tests_result=None)
        self.assertEqual(res[0]["status"], "FAIL")

    def test_workerctl_criterion_fails_when_tests_not_run(self):
        res = workerctl_server.evaluate_criteria(self._criterion(), {}, "wt",
                                                 tests_result={"status": "NOT_RUN"})
        self.assertEqual(res[0]["status"], "FAIL")

    def test_workerctl_criterion_passes_only_on_pass(self):
        res = workerctl_server.evaluate_criteria(self._criterion(), {}, "wt",
                                                 tests_result={"status": "PASS"})
        self.assertEqual(res[0]["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
