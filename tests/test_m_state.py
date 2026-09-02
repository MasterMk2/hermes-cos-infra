#!/usr/bin/env python3
"""M-series state machine hardening tests:
  M1 per-job max_attempts, M2 dispatch CAS, M6 progress signature,
  M7 git fail-closed + spawn failure, M8 lifecycle timestamps."""

import threading
import time
import unittest

import helpers  # noqa: F401  (sys.path setup)
from helpers import (PASSING_TEST_CMD, Sandbox, TEST_CRITERIA,
                     make_fixture_repo, wait_terminal)

import bridge      # noqa: E402
import jobs_db     # noqa: E402
import jobs_mcp    # noqa: E402
import supervisor  # noqa: E402
import validator   # noqa: E402


def _create_job(sb, repo, behavior="no_progress", max_attempts=6):
    res = jobs_mcp._call("job_create", {
        "goal": f"g\n#SIM-BEHAVIOR: {behavior}", "repository": "r",
        "kind": "test", "agent": "simulated",
        "tests_command": PASSING_TEST_CMD, "max_attempts": max_attempts,
        "acceptance_criteria": TEST_CRITERIA,
    })
    return res["job_id"]


class TestM1PerJobMaxAttempts(unittest.TestCase):
    def _run_to_limit(self, sb, repo, job_max, global_max):
        from helpers import spawn_runner_in_thread
        sb.write_config("r", repo, [PASSING_TEST_CMD], max_attempts=global_max,
                        no_progress_strikes=50)
        job_id = _create_job(sb, repo, "no_progress", max_attempts=job_max)
        fake_spawn, _ = spawn_runner_in_thread()
        orig_spawn = supervisor._spawn_runner
        supervisor._spawn_runner = fake_spawn
        try:
            jobs_mcp._call("job_dispatch", {"job_id": job_id})
            final = wait_terminal(job_id, timeout=180)
        finally:
            supervisor._spawn_runner = orig_spawn
        return final, jobs_db.runs_for_job(job_id)

    def test_job_max_attempts_2_beats_global_6(self):
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            final, runs = self._run_to_limit(sb, repo, job_max=2,
                                             global_max=6)
            self.assertEqual(final["status"], "WAITING_HUMAN")
            self.assertIn("attempt limit 2", final["failure_reason"])
            self.assertEqual(len(runs), 2)

    def test_job_max_attempts_6_beats_global_2(self):
        # M1 spec: the job's own value WINS even when it is larger than the
        # global default (global is only a fallback, never a cap).
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            final, runs = self._run_to_limit(sb, repo, job_max=6,
                                             global_max=2)
            self.assertEqual(final["status"], "WAITING_HUMAN")
            self.assertIn("attempt limit 6", final["failure_reason"])
            self.assertEqual(len(runs), 6)

    def test_legacy_job_without_attempts_falls_back_to_global(self):
        # M1 spec: a legacy fixture row with max_attempts=NULL uses the
        # global config default.
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            from helpers import spawn_runner_in_thread
            sb.write_config("r", repo, [PASSING_TEST_CMD], max_attempts=2,
                            no_progress_strikes=50)
            jid = jobs_db.create_job(
                goal="g\n#SIM-BEHAVIOR: no_progress", repository="r",
                acceptance_criteria=TEST_CRITERIA, agent="simulated",
                kind="test", tests_command=PASSING_TEST_CMD,
                max_attempts=None)
            self.assertIsNone(jobs_db.get_job(jid)["max_attempts"])
            fake_spawn, _ = spawn_runner_in_thread()
            orig_spawn = supervisor._spawn_runner
            supervisor._spawn_runner = fake_spawn
            try:
                jobs_mcp._call("job_dispatch", {"job_id": jid})
                final = wait_terminal(jid, timeout=180)
            finally:
                supervisor._spawn_runner = orig_spawn
            self.assertEqual(final["status"], "WAITING_HUMAN")
            self.assertIn("attempt limit 2", final["failure_reason"])
            self.assertEqual(len(jobs_db.runs_for_job(jid)), 2)


class TestM2DoubleDispatch(unittest.TestCase):
    def test_concurrent_dispatch_single_winner_single_runner(self):
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            sb.write_config("r", repo, [PASSING_TEST_CMD])
            job_id = _create_job(sb, repo, "no_progress")

            spawned = []
            release = threading.Event()

            def slow_spawn(job_id_):
                release.wait(5)
                spawned.append(job_id_)
                return 0

            orig_spawn = supervisor._spawn_runner
            supervisor._spawn_runner = slow_spawn
            try:
                results = []

                def dispatch():
                    results.append(
                        supervisor.dispatch_job(job_id))

                t1 = threading.Thread(target=dispatch)
                t2 = threading.Thread(target=dispatch)
                t1.start()
                t2.start()
                t1.join(10)
                t2.join(10)
                release.set()

                ok = [r for r in results
                      if r.get("status") == "DISPATCHING" and "error" not in r]
                rejected = [r for r in results
                            if r.get("error") in ("already_claimed",
                                                  "already_active")]
                self.assertEqual(len(results), 2)
                self.assertEqual(len(ok), 1, f"exactly one winner: {results}")
                self.assertEqual(len(rejected), 1, f"one loser: {results}")
                self.assertEqual(len(spawned), 1, "exactly one runner")
            finally:
                supervisor._spawn_runner = orig_spawn

    def test_sequential_second_dispatch_rejected(self):
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            sb.write_config("r", repo, [PASSING_TEST_CMD])
            job_id = _create_job(sb, repo, "no_progress")
            spawned = []

            def slow_spawn(jid):
                spawned.append(jid)
                time.sleep(0.5)  # hold DISPATCHING long enough
                return 0

            orig_spawn = supervisor._spawn_runner
            supervisor._spawn_runner = slow_spawn
            try:
                r1 = supervisor.dispatch_job(job_id)
                r2 = supervisor.dispatch_job(job_id)
                self.assertEqual(r1["status"], "DISPATCHING")
                self.assertIn(r2.get("error"),
                              ("already_claimed", "already_active"))
            finally:
                supervisor._spawn_runner = orig_spawn


class TestM6ProgressSignature(unittest.TestCase):
    def test_same_file_count_different_content_changes_signature(self):
        job1 = {"git_changed_files": ["a.py"], "tests_status": "FAIL",
                "acceptance_results": [{"id": "x", "status": "FAIL"}]}
        run1 = {"git_before": "base", "git_after": "1111",
                "diff_hash": "d1"}
        job2 = {"git_changed_files": ["b.py"], "tests_status": "FAIL",
                "acceptance_results": [{"id": "x", "status": "FAIL"}]}
        run2 = {"git_before": "base", "git_after": "2222",
                "diff_hash": "d2"}
        s1 = supervisor.progress_signature(job1, run1)
        s2 = supervisor.progress_signature(job2, run2)
        self.assertNotEqual(s1, s2,
                            "same file COUNT but different diff must differ")

    def test_identical_state_gives_identical_signature(self):
        job = {"git_changed_files": ["a.py"], "tests_status": "FAIL",
               "acceptance_results": [{"id": "x", "status": "FAIL"}]}
        run = {"git_before": "base", "git_after": "1111", "diff_hash": "d1"}
        self.assertEqual(supervisor.progress_signature(job, run),
                         supervisor.progress_signature(job, run))

    def test_signature_covers_unmet_criteria(self):
        job1 = {"git_changed_files": [], "tests_status": "FAIL",
                "acceptance_results": [{"id": "x", "status": "FAIL"}]}
        job2 = {"git_changed_files": [], "tests_status": "FAIL",
                "acceptance_results": [{"id": "x", "status": "PASS"}]}
        run = {"git_before": "b", "git_after": "h", "diff_hash": None}
        self.assertNotEqual(supervisor.progress_signature(job1, run),
                            supervisor.progress_signature(job2, run))

    def test_signature_is_hash_only(self):
        job = {"git_changed_files": ["a" * 500], "tests_status": "FAIL",
               "acceptance_results": []}
        run = {"git_before": "b", "git_after": "h", "diff_hash": None}
        sig = supervisor.progress_signature(job, run)
        self.assertLessEqual(len(sig), 16)


class TestM7GitFailClosed(unittest.TestCase):
    def test_git_state_raises_on_non_repo(self):
        import tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp(prefix="nongit-"))
        with self.assertRaises(supervisor.GitStateUnknown):
            supervisor.git_state(str(d))

    def test_run_job_loop_refuses_on_unknown_git_state(self):
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            sb.write_config("r", repo, [PASSING_TEST_CMD])
            job_id = _create_job(sb, repo)
            orig_git_state = supervisor.git_state

            def broken_state(repo_path):
                raise supervisor.GitStateUnknown(
                    "git state unknown: simulated git failure")

            supervisor.git_state = broken_state
            try:
                rc = supervisor.run_job_loop(job_id)
                self.assertEqual(rc, 1)
                job = jobs_db.get_job(job_id)
                self.assertEqual(job["status"], "WAITING_HUMAN")
                self.assertEqual(job["needs_human"], 1)
                self.assertIn("git state unknown", job["failure_reason"])
            finally:
                supervisor.git_state = orig_git_state

    def test_spawn_failure_becomes_failed_to_start(self):
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            sb.write_config("r", repo, [PASSING_TEST_CMD])
            job_id = _create_job(sb, repo)

            def broken_spawn(jid):
                raise RuntimeError("no pids available")

            orig_spawn = supervisor._spawn_runner
            supervisor._spawn_runner = broken_spawn
            try:
                res = supervisor.dispatch_job(job_id)
                self.assertEqual(res["status"], "FAILED_TO_START")
                job = jobs_db.get_job(job_id)
                self.assertEqual(job["status"], "FAILED_TO_START")
                self.assertIn("runner spawn failed", job["failure_reason"])
            finally:
                supervisor._spawn_runner = orig_spawn
            # retryability: job_continue accepts FAILED_TO_START
            from helpers import spawn_runner_in_thread
            fake_spawn, _ = spawn_runner_in_thread()
            supervisor._spawn_runner = fake_spawn
            try:
                res2 = jobs_mcp._call("job_continue", {"job_id": job_id})
                self.assertNotIn("error", res2)
                self.assertEqual(res2.get("status"), "DISPATCHING")
            finally:
                supervisor._spawn_runner = orig_spawn

    def test_events_logged_for_spawn_failure(self):
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            sb.write_config("r", repo, [PASSING_TEST_CMD])
            job_id = _create_job(sb, repo)

            def broken_spawn(jid):
                raise OSError("spawn boom")

            orig_spawn = supervisor._spawn_runner
            supervisor._spawn_runner = broken_spawn
            try:
                supervisor.dispatch_job(job_id)
            finally:
                supervisor._spawn_runner = orig_spawn
            con = jobs_db.connect()
            try:
                rows = con.execute(
                    "SELECT message FROM event_log WHERE job_id=? AND level='error'",
                    (job_id,)).fetchall()
            finally:
                con.close()
            self.assertTrue(any("spawn" in r["message"] for r in rows))


class TestM8LifecycleTimestamps(unittest.TestCase):
    def _job(self):
        return jobs_db.create_job(goal="g", repository="r",
                                  acceptance_criteria=[{"id": "a",
                                                        "verification":
                                                        {"kind": "git_scope",
                                                         "params": {}}}])

    def test_agent_stopped_stamps_implementation_finished(self):
        with Sandbox():
            jid = self._job()
            jobs_db.transition(jid, "RUNNING")
            self.assertIsNone(jobs_db.get_job(jid)["finished_at"])
            jobs_db.transition(jid, "AGENT_STOPPED")
            job = jobs_db.get_job(jid)
            self.assertIsNotNone(job["implementation_finished_at"])
            self.assertIsNone(job["finished_at"])

    def test_acceptance_stamps_finished_at(self):
        with Sandbox():
            jid = self._job()
            jobs_db.transition(jid, "AGENT_STOPPED")
            jobs_db.transition(jid, "IMPLEMENTATION_ACCEPTED")
            job = jobs_db.get_job(jid)
            self.assertIsNotNone(job["finished_at"])
            self.assertIsNotNone(job["implementation_finished_at"])

    def test_cancel_stamps_cancel_requested_and_finished(self):
        with Sandbox():
            jid = self._job()
            jobs_db.transition(jid, "RUNNING")
            jobs_db.transition(jid, "CANCELLED", failure_reason="user")
            job = jobs_db.get_job(jid)
            self.assertIsNotNone(job["cancel_requested_at"])
            self.assertIsNotNone(job["finished_at"])
            self.assertEqual(job["status"], "CANCELLED")

    def test_state_changes_are_audited_in_event_log(self):
        with Sandbox():
            jid = self._job()
            jobs_db.transition(jid, "RUNNING")
            jobs_db.transition(jid, "AGENT_STOPPED")
            jobs_db.transition(jid, "FAILED", failure_reason="x")
            con = jobs_db.connect()
            try:
                rows = [r["message"] for r in con.execute(
                    "SELECT message FROM event_log WHERE job_id=? AND level='state'",
                    (jid,)).fetchall()]
            finally:
                con.close()
            self.assertIn("status -> RUNNING", rows)
            self.assertIn("status -> AGENT_STOPPED", rows)
            self.assertIn("status -> FAILED", rows)

    def test_iso8601_utc_timestamps(self):
        with Sandbox():
            jid = self._job()
            jobs_db.transition(jid, "AGENT_STOPPED")
            ts = jobs_db.get_job(jid)["implementation_finished_at"]
            from datetime import datetime
            parsed = datetime.fromisoformat(ts)
            self.assertIsNotNone(parsed.utcoffset())


class TestArtifactSizeLimit(unittest.TestCase):
    def test_validator_flags_artifact_too_large(self):
        import tempfile
        from pathlib import Path
        repo = Path(tempfile.mkdtemp(prefix="artifact-"))
        big = repo / "big.txt"
        big.write_text("x" * (validator.ARTIFACT_MAX_BYTES + 10),
                       encoding="utf-8")
        ok, detail = validator._check_artifact_contains(
            str(repo), {"path": "big.txt", "text": "needle"})
        self.assertFalse(ok)
        self.assertIn("artifact_too_large", detail)

    def test_bridge_flags_artifact_too_large(self):
        import tempfile
        from pathlib import Path
        repo = Path(tempfile.mkdtemp(prefix="artifact-"))
        big = repo / "big.txt"
        big.write_text("x" * (bridge.ARTIFACT_MAX_BYTES + 10),
                       encoding="utf-8")
        res = bridge.evaluate_criteria(
            [{"id": "a", "description": "d",
              "verification": {"kind": "artifact_contains",
                               "params": {"path": "big.txt",
                                          "text": "needle"}}}],
            {}, str(repo), changed_files=[])
        self.assertEqual(res[0]["status"], "FAIL")
        self.assertIn("artifact_too_large", res[0]["detail"])


if __name__ == "__main__":
    unittest.main()
