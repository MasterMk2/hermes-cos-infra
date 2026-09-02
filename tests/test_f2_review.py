#!/usr/bin/env python3
"""Phase F.2 review-loop unit tests.

Covers: single review owner, approval flow (never auto-COMPLETED), blocking
finding -> fix flow, invalid JSON repair budget, conflicting verdict,
approval staleness, fake-DB approval guard, cancel during review, review
crash recovery, cycle exhaustion, hallucinated paths, non-blocking findings,
legacy completion guard, review package independence, fingerprints,
no-progress stall.
"""

import json
import threading
import unittest

import helpers  # noqa: F401  (sys.path setup)
from helpers import (PASSING_TEST_CMD, Sandbox, TEST_CRITERIA,
                     make_fixture_repo, spawn_runner_in_thread, wait_terminal)

import bridge            # noqa: E402
import jobs_db           # noqa: E402
import jobs_mcp          # noqa: E402
import review as rv      # noqa: E402
import supervisor        # noqa: E402
import transport         # noqa: E402

PROD_AGENT = "pi-sandbox-hermes-commandcode"

BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
CONTENT_SHA = "c" * 64
DIFF_SHA = "d" * 16

EVIDENCE = {
    "job_id": None, "repo_id": "r", "agent": PROD_AGENT,
    "transport": "pi-sandbox", "base_sha": BASE_SHA, "head_sha": HEAD_SHA,
    "diff_sha256": DIFF_SHA, "content_sha256": CONTENT_SHA,
    "changed_files": ["pkg/greeter.py"],
    "test_summary": {"status": "PASS", "summary": "ok",
                     "criteria_results": [
                         {"id": "ac1", "verification": "file_exists",
                          "status": "PASS", "detail": "ok"}]},
    "secret_scan": [],
    "usage": None,
}


class FakeBridge:
    """Programmable in-test bridge replacement for transport.bridge_request.

    mode controls the review_result payload:
      approve / changes_required / invalid_json / conflicting
    verify_sha / verify_content control the completion-gate binding check.
    cancel_job_id, when set, transitions that job to CANCELLED on the first
    review_status poll (deterministic cancel-during-review test).
    """

    def __init__(self, mode="approve", verify_sha=HEAD_SHA,
                 verify_content=None, tests_status="PASS",
                 review_error=None, snap_content=CONTENT_SHA, flip_after=1,
                 verify_secret=None, cancel_job_id=None):
        self.mode = mode
        self.verify_sha = verify_sha
        # verify_content=None tracks snap_content (fresh verification reads
        # the same worktree the review snapshot was built from)
        self.verify_content = verify_content
        self.tests_status = tests_status
        self.review_error = review_error
        self.snap_content = snap_content
        self.flip_after = flip_after
        self.verify_secret = verify_secret
        self.cancel_job_id = cancel_job_id
        self.calls = []
        self.status_polls = 0
        self.cancelled_reviews = []
        self.started_reviews = []

    def __call__(self, payload, timeout=60):
        self.calls.append(payload)
        action = payload.get("action")
        if action == "job_result":
            ev = dict(EVIDENCE)
            ev["job_id"] = payload.get("job_id")
            ev["content_sha256"] = self.snap_content
            return {"job_id": payload.get("job_id"), "state": "finished",
                    "evidence": ev}
        if action == "job_diff":
            return {"diff": "+def greet(name):\n+    return f\"Hello!\"\n",
                    "truncated": False, "untracked_files": ["pkg/greeter.py"]}
        if action == "review_start":
            if self.review_error:
                return {"error": self.review_error}
            self.started_reviews.append(payload.get("review_id"))
            return {"accepted": True, "review_id": payload.get("review_id"),
                    "state": "running", "snapshot_content_sha": self.snap_content}
        if action == "review_status":
            self.status_polls += 1
            if self.cancel_job_id and self.status_polls == 1:
                jobs_db.transition(self.cancel_job_id, "CANCELLED",
                                   failure_reason="cancel during review (test)")
            if self.status_polls >= self.flip_after:
                return {"review_id": payload.get("review_id"), "state": "finished"}
            return {"review_id": payload.get("review_id"), "state": "running"}
        if action == "review_result":
            return {"review_id": payload.get("review_id"), "state": "finished",
                    "review": self._review_payload()}
        if action == "review_cancel":
            self.cancelled_reviews.append(payload.get("review_id"))
            return {"review_id": payload.get("review_id"), "state": "cancelled"}
        if action == "review_cleanup":
            return {"review_id": payload.get("review_id"), "snapshot_removed": True}
        if action == "job_verify":
            return {
                "job_id": payload.get("job_id"), "head_sha": self.verify_sha,
                "base_sha": BASE_SHA, "changed_files": ["pkg/greeter.py"],
                "content_sha256": (self.verify_content if self.verify_content
                                   is not None else self.snap_content),
                "diff_sha256": DIFF_SHA,
                "secret_scan": list(self.verify_secret or []),
                "tests": {"status": self.tests_status, "summary": "ok"},
            }
        return {"error": f"unknown_action:{action}"}

    def _review_payload(self):
        if self.mode == "approve":
            data = {"verdict": "APPROVE",
                    "summary": "No blocking defects found.",
                    "findings": []}
        elif self.mode == "changes_required":
            data = {"verdict": "CHANGES_REQUIRED",
                    "summary": "Blocking correctness issue found.",
                    "findings": [{
                        "severity": "high", "category": "correctness",
                        "file": "pkg/greeter.py", "line_start": 1,
                        "line_end": 3,
                        "title": "greet ignores the name argument",
                        "explanation": "The function returns a constant; the "
                                       "registered tests only check file "
                                       "presence, not behaviour.",
                        "suggested_fix": "Return f\"Hello, {name}!\"",
                        "blocking": True, "pre_existing": False,
                        "confidence": 0.9,
                    }]}
        elif self.mode == "invalid_json":
            data = "this is not json at all {{{"
        elif self.mode == "conflicting":
            data = {"verdict": "APPROVE", "summary": "s",
                    "findings": [{"severity": "high", "category": "correctness",
                                  "title": "x", "explanation": "y",
                                  "blocking": True, "confidence": 0.9}]}
        else:
            data = None
        usage = {"attempts": [{"attempt_file": "review-usage-1.json",
                               "input_tokens": 100, "output_tokens": 50,
                               "reasoning_tokens": 10, "total_tokens": 160,
                               "api_calls": 2, "model": rv.REVIEWER_MODEL,
                               "provider": "commandcode", "completed": True}],
                 "input_tokens": 100, "output_tokens": 50,
                 "reasoning_tokens": 10, "total_tokens": 160, "api_calls": 2}
        return {"result_json": data, "usage": usage,
                "exit_status": 0, "timed_out": False}


def make_accepted_job(sb, repo, kind="implementation", agent=PROD_AGENT):
    """Create a job directly in the DB and move it to the accepted state
    (with deterministic validation results already recorded, as the real
    supervisor would have done before entering the review phase)."""
    jid = jobs_db.create_job(
        goal="implement greet", repository="r",
        acceptance_criteria=TEST_CRITERIA, agent=agent, kind=kind,
        tests_command=PASSING_TEST_CMD)
    jobs_db.transition(jid, "IMPLEMENTATION_ACCEPTED")
    jobs_db.update_job(
        jid, review_status="WAITING_REVIEW",
        acceptance_results=[
            {"id": ac["id"], "verification": ac["verification"]["kind"],
             "status": "PASS", "detail": "ok"} for ac in TEST_CRITERIA])
    return jid


class F2ReviewTestBase(unittest.TestCase):
    def setUp(self):
        self._patches = []

    def tearDown(self):
        for target, attr, value in reversed(self._patches):
            setattr(target, attr, value)

    def install_bridge(self, fake):
        self._patches.append((transport, "bridge_request",
                              transport.bridge_request))
        transport.bridge_request = fake

    def accept_job(self, sb, repo, kind="implementation", agent=PROD_AGENT):
        sb.write_config("r", repo, [PASSING_TEST_CMD])
        return make_accepted_job(sb, repo, kind=kind, agent=agent)


class TestReviewDispatch(F2ReviewTestBase):
    def test_approve_flow_records_review_not_completed(self):
        """Section 86: a valid APPROVE review records APPROVED — the job is
        NOT completed directly (the final gate must run separately)."""
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            jid = self.accept_job(sb, repo)
            self.install_bridge(FakeBridge("approve"))
            out = supervisor.run_review_phase(jid, supervisor.load_config())
            self.assertEqual(out, "APPROVED_PRESENT")
            job = jobs_db.get_job(jid)
            self.assertEqual(job["review_status"], "APPROVED")
            self.assertEqual(job["status"], "IMPLEMENTATION_ACCEPTED")
            rec = jobs_db.latest_review(jid)
            self.assertEqual(rec["status"], "approved")
            self.assertEqual(rec["verdict"], "APPROVE")
            self.assertEqual(rec["target_sha"], HEAD_SHA)
            self.assertEqual(rec["content_sha"], CONTENT_SHA)

    def test_double_review_dispatch_prevented(self):
        """Section 85: exactly one review owner; a second runner whose claim
        loses exits (REVIEW_BUSY) and never double-runs a review."""
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            jid = self.accept_job(sb, repo)
            # simulate the winning runner: claim + a LIVE pidfile (alive is
            # simulated via _pid_alive so the test stays platform-independent)
            self.assertTrue(jobs_db.cas_review_status(
                jid, "REVIEWING", ("WAITING_REVIEW",)))
            pidfile = jobs_db.LOGS_DIR / f"runner-{jid}.pid"
            pidfile.write_text(json.dumps({"pid": 555555}))
            orig_alive = supervisor._pid_alive
            supervisor._pid_alive = lambda pid, ticks=None: pid == 555555
            try:
                self.install_bridge(FakeBridge("approve"))
                out = supervisor.run_review_phase(jid, supervisor.load_config())
                self.assertEqual(out, "REVIEW_BUSY")
                self.assertEqual(jobs_db.latest_review(jid), None)
            finally:
                supervisor._pid_alive = orig_alive

    def test_mcp_job_review_dispatch_and_single_claim(self):
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            jid = self.accept_job(sb, repo)
            spawned = []
            orig = supervisor._spawn_runner
            supervisor._spawn_runner = lambda j: spawned.append(j) or 4242
            try:
                res = jobs_mcp._call("job_review", {"job_id": jid})
                self.assertEqual(res.get("review_status"), "REVIEWING")
                self.assertEqual(spawned, [jid])
                res2 = jobs_mcp._call("job_review", {"job_id": jid})
                self.assertEqual(res2.get("error"), "already_claimed")
                res3 = jobs_mcp._call("job_review", {"job_id": jid})
                self.assertEqual(res3.get("error"), "already_claimed")
            finally:
                supervisor._spawn_runner = orig

    def test_test_kind_jobs_skip_review(self):
        """Section 111: test-kind fixture jobs skip review by policy."""
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            jid = self.accept_job(sb, repo, kind="test", agent="simulated")
            self.install_bridge(FakeBridge("approve"))
            out = supervisor.run_review_phase(jid, supervisor.load_config())
            self.assertEqual(out, "NOT_REQUIRED")
            self.assertEqual(jobs_db.latest_review(jid), None)


class TestVerdictHandling(F2ReviewTestBase):
    def test_blocking_finding_routes_to_fix_flow(self):
        """Section 87: CHANGES_REQUIRED + blocking finding -> fix flow."""
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            jid = self.accept_job(sb, repo)
            self.install_bridge(FakeBridge("changes_required"))
            out = supervisor.run_review_phase(jid, supervisor.load_config())
            self.assertEqual(out, "FIX_REQUIRED")
            job = jobs_db.get_job(jid)
            self.assertEqual(job["review_status"], "CHANGES_REQUIRED")
            self.assertEqual(job["status"], "CONTINUE_REQUIRED")
            rec = jobs_db.latest_review(jid)
            self.assertEqual(rec["status"], "changes_required")
            self.assertEqual(rec["blocking_count"], 1)
            self.assertTrue(rec["fingerprints"])
            # fix prompt carries the structured finding, not a transcript
            fix = jobs_db.latest_review(jid, statuses=("changes_required",))
            prompt = supervisor.build_prompt(job, sb.write_config(
                "r", repo, [PASSING_TEST_CMD]), prev_run=None,
                review_fix=fix)
            self.assertIn("CHANGES_REQUIRED", prompt)
            self.assertIn("greet ignores the name argument", prompt)
            self.assertIn("Return f\"Hello, {name}!\"", prompt)
            self.assertIn("preserving all original acceptance criteria", prompt)

    def test_invalid_json_gets_one_repair_then_fails(self):
        """Section 40/88: invalid output -> strict repair retry; budget
        exhaustion -> REVIEW_FAILED + WAITING_HUMAN."""
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            jid = self.accept_job(sb, repo)
            self.install_bridge(FakeBridge("invalid_json"))
            out = supervisor.run_review_phase(jid, supervisor.load_config())
            self.assertEqual(out, "WAITING_HUMAN")
            job = jobs_db.get_job(jid)
            self.assertEqual(job["review_status"], "REVIEW_FAILED")
            self.assertEqual(job["status"], "WAITING_HUMAN")
            rows = jobs_db.reviews_for_job(jid)
            self.assertEqual(len(rows), 2)  # attempt 1 + repair attempt
            self.assertEqual(rows[0]["status"], "failed")
            self.assertEqual(rows[1]["status"], "failed")

    def test_conflicting_verdict_is_invalid(self):
        """Section 36/89: APPROVE + blocking=true is never accepted."""
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            jid = self.accept_job(sb, repo)
            self.install_bridge(FakeBridge("conflicting"))
            out = supervisor.run_review_phase(jid, supervisor.load_config())
            self.assertEqual(out, "WAITING_HUMAN")
            self.assertEqual(jobs_db.get_job(jid)["review_status"],
                             "REVIEW_FAILED")

    def test_nonblocking_findings_allowed_with_approve(self):
        """Section 96: APPROVE with non-blocking findings is valid and the
        findings stay in the immutable history."""
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            jid = self.accept_job(sb, repo)
            fb = FakeBridge("approve")
            orig_payload = fb._review_payload

            def payload_with_notes():
                d = orig_payload()
                d["result_json"]["findings"] = [{
                    "severity": "low", "category": "maintainability",
                    "file": "pkg/greeter.py", "title": "docstring style",
                    "explanation": "minor", "blocking": False,
                    "confidence": 0.5}]
                d["result_json"]["summary"] = "approved with notes"
                return d
            fb._review_payload = payload_with_notes
            self.install_bridge(fb)
            out = supervisor.run_review_phase(jid, supervisor.load_config())
            self.assertEqual(out, "APPROVED_PRESENT")
            rec = jobs_db.latest_review(jid)
            self.assertEqual(rec["status"], "approved")
            self.assertEqual(rec["blocking_count"], 0)
            self.assertEqual(len(rec["findings"]), 1)
            self.assertFalse(rec["findings"][0]["blocking"])

    def test_review_no_progress_stall(self):
        """Section 64/108: same blocking fingerprints + no content progress
        across cycles -> WAITING_HUMAN (no infinite fix<->review loop)."""
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            jid = self.accept_job(sb, repo)
            fb = FakeBridge("changes_required")
            self.install_bridge(fb)
            out1 = supervisor.run_review_phase(jid, supervisor.load_config())
            self.assertEqual(out1, "FIX_REQUIRED")
            # the fix attempt produced NO content change and did not resolve
            # the findings: restore the accepted state with the SAME binding
            jobs_db.update_job(jid, status="IMPLEMENTATION_ACCEPTED",
                               review_status="WAITING_REVIEW")
            out2 = supervisor.run_review_phase(jid, supervisor.load_config())
            self.assertEqual(out2, "WAITING_HUMAN")
            job = jobs_db.get_job(jid)
            self.assertEqual(job["status"], "WAITING_HUMAN")
            self.assertIn("review_no_progress_stalled", job["failure_reason"])


class TestCycleBudgetAndCrash(F2ReviewTestBase):
    def test_cycle_exhaustion_waits_for_human(self):
        """Section 63/94: max_review_cycles blocking verdicts -> WAITING_HUMAN
        with review_cycle_limit_reached."""
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            sb.write_config("r", repo, [PASSING_TEST_CMD], max_attempts=6)
            jid = make_accepted_job(sb, repo)
            fb = FakeBridge("changes_required")
            self.install_bridge(fb)
            for i in range(3):
                out = supervisor.run_review_phase(jid, supervisor.load_config())
                self.assertEqual(out, "FIX_REQUIRED")
                # simulate a fix attempt that keeps producing new content
                jobs_db.update_job(jid, status="IMPLEMENTATION_ACCEPTED",
                                   review_status="WAITING_REVIEW")
                fb.snap_content = ("c" + str(i + 2)) * 32
            out4 = supervisor.run_review_phase(jid, supervisor.load_config())
            self.assertEqual(out4, "WAITING_HUMAN")
            job = jobs_db.get_job(jid)
            self.assertIn("review_cycle_limit_reached", job["failure_reason"])

    def test_review_crash_becomes_interrupted(self):
        """Section 93: a dead review runner while REVIEWING -> REVIEW_INTERRUPTED
        (job preserved, retryable via job_review)."""
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            jid = self.accept_job(sb, repo)
            jobs_db.update_job(jid, review_status="REVIEWING")
            pidfile = jobs_db.LOGS_DIR / f"runner-{jid}.pid"
            pidfile.write_text(json.dumps({"pid": 999999, "start_ticks": "1"}))
            out = supervisor.reconcile()
            self.assertTrue(any(r["job_id"] == jid and
                                "REVIEW_INTERRUPTED" in r["now"] for r in out))
            job = jobs_db.get_job(jid)
            self.assertEqual(job["review_status"], "REVIEW_INTERRUPTED")
            self.assertIn("job_review", job["next_action"])
            self.assertEqual(job["status"], "IMPLEMENTATION_ACCEPTED")

    def test_cancel_during_review_is_terminal(self):
        """Section 71/92: cancel during REVIEWING -> CANCELLED; the reviewer
        container is stopped and nothing resurrects the job."""
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            jid = self.accept_job(sb, repo)
            fb = FakeBridge("approve", flip_after=3, cancel_job_id=jid)
            self.install_bridge(fb)
            out = supervisor.run_review_phase(jid, supervisor.load_config())
            self.assertEqual(out, "CANCELLED")
            job = jobs_db.get_job(jid)
            self.assertEqual(job["status"], "CANCELLED")
            self.assertTrue(fb.cancelled_reviews, "reviewer not stopped")
            rec = jobs_db.latest_review(jid)
            self.assertEqual(rec["status"], "cancelled")


class TestCompletionGate(F2ReviewTestBase):
    def test_completion_requires_bound_approval(self):
        """Section 53/91: a bare review_status=APPROVED without a matching
        bound review record can never complete."""
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            jid = self.accept_job(sb, repo)
            jobs_db.update_job(jid, review_status="APPROVED")  # fake DB write
            self.install_bridge(FakeBridge("approve"))
            ok, why, _ = supervisor.completion_decision(jid)
            self.assertFalse(ok)
            self.assertEqual(why, "no_review_record")

    def test_completion_requires_review_at_all(self):
        """Section 110: the review-less completion path is impossible."""
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            jid = self.accept_job(sb, repo)  # WAITING_REVIEW, no review
            self.install_bridge(FakeBridge("approve"))
            ok, why, _ = supervisor.completion_decision(jid)
            self.assertFalse(ok)
            self.assertEqual(why, "no_approval")
            # no writer anywhere may move it to COMPLETED
            self.assertEqual(jobs_db.get_job(jid)["status"],
                             "IMPLEMENTATION_ACCEPTED")

    def test_approval_stale_after_target_change(self):
        """Section 51/90: approval for SHA A is invalid for a tree at B."""
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            jid = self.accept_job(sb, repo)
            fb = FakeBridge("approve")
            self.install_bridge(fb)
            out = supervisor.run_review_phase(jid, supervisor.load_config())
            self.assertEqual(out, "APPROVED_PRESENT")
            # the implementation tree changes after the approval
            fb.verify_sha = "e" * 40
            out2 = supervisor.finalize_completion(jid)
            self.assertEqual(out2, "REVIEW_AGAIN")
            job = jobs_db.get_job(jid)
            self.assertEqual(job["review_status"], "WAITING_REVIEW")
            self.assertEqual(job["status"], "IMPLEMENTATION_ACCEPTED")

    def test_approval_stale_after_content_change(self):
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            jid = self.accept_job(sb, repo)
            fb = FakeBridge("approve")
            self.install_bridge(fb)
            supervisor.run_review_phase(jid, supervisor.load_config())
            fb.verify_content = "f" * 64
            out2 = supervisor.finalize_completion(jid)
            self.assertEqual(out2, "REVIEW_AGAIN")

    def test_final_gate_checks_tests_secret_and_cancel(self):
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            jid = self.accept_job(sb, repo)
            fb = FakeBridge("approve", tests_status="FAIL")
            self.install_bridge(fb)
            out = supervisor.run_review_phase(jid, supervisor.load_config())
            self.assertEqual(out, "APPROVED_PRESENT")
            gout = supervisor.finalize_completion(jid)
            self.assertEqual(gout, "WAITING_HUMAN")
            self.assertIn("final_validation_unmet",
                          jobs_db.get_job(jid)["failure_reason"])

            # secret scan hit at gate time -> no completion
            jid2 = self.accept_job(sb, repo)
            fb2 = FakeBridge("approve", verify_secret=["credential_assignment"])
            self.install_bridge(fb2)
            supervisor.run_review_phase(jid2, supervisor.load_config())
            g2 = supervisor.finalize_completion(jid2)
            self.assertEqual(g2, "WAITING_HUMAN")
            self.assertIn("secret_scan_hits",
                          jobs_db.get_job(jid2)["failure_reason"])

            # cancel during the gate -> no completion
            jid3 = self.accept_job(sb, repo)
            fb3 = FakeBridge("approve")
            self.install_bridge(fb3)
            supervisor.run_review_phase(jid3, supervisor.load_config())
            jobs_db.transition(jid3, "CANCELLED")
            g3 = supervisor.finalize_completion(jid3)
            self.assertEqual(g3, "CANCELLED")

    def test_full_approval_to_completed(self):
        """Section 102/109: bound approval + matching verification + tests +
        secret scan + no cancel -> COMPLETED via CAS."""
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            jid = self.accept_job(sb, repo)
            fb = FakeBridge("approve")
            self.install_bridge(fb)
            out = supervisor.run_review_phase(jid, supervisor.load_config())
            self.assertEqual(out, "APPROVED_PRESENT")
            gout = supervisor.finalize_completion(jid)
            self.assertEqual(gout, "COMPLETED")
            job = jobs_db.get_job(jid)
            self.assertEqual(job["status"], "COMPLETED")
            self.assertEqual(job["review_status"], "APPROVED")


class TestReviewSchemaAndPackage(unittest.TestCase):
    def test_schema_rejects_bad_paths(self):
        """Section 37/95: hallucinated/traversal paths are rejected."""
        for bad in ("../../etc/passwd", "/etc/shadow", "a\\b.py", "~/.ssh/id"):
            res, err = rv.validate_review_output({
                "verdict": "CHANGES_REQUIRED", "summary": "s",
                "findings": [{"severity": "high", "category": "correctness",
                              "file": bad, "title": "t", "explanation": "e",
                              "blocking": True, "confidence": 0.9}]})
            self.assertIsNone(res, bad)
            self.assertIn("path", err, bad)

    def test_schema_bounds(self):
        """Section 38/39: finding count + text bounds are enforced."""
        many = [{"severity": "low", "category": "maintainability",
                 "title": f"f{i}", "explanation": "e", "blocking": False}
                for i in range(31)]
        res, err = rv.validate_review_output(
            {"verdict": "CHANGES_REQUIRED", "summary": "s", "findings": many})
        self.assertIsNone(res)
        self.assertIn("too many findings", err)
        res, err = rv.validate_review_output({
            "verdict": "APPROVE", "summary": "x" * 3000, "findings": []})
        self.assertIsNone(res)
        self.assertIn("summary too long", err)

    def test_fingerprint_stability(self):
        """Section 56: identical findings fingerprint identically across
        cycles; different ones do not."""
        f1 = {"category": "correctness", "file": "pkg/a.py",
              "line_start": 1, "line_end": 2,
              "title": "  Bug   here  ", "explanation": "x" * 400}
        f2 = dict(f1, title="bug   here", explanation="x" * 400 + " more")
        f3 = dict(f1, file="pkg/b.py")
        self.assertEqual(rv.finding_fingerprint(f1),
                         rv.finding_fingerprint(f2))
        self.assertNotEqual(rv.finding_fingerprint(f1),
                            rv.finding_fingerprint(f3))

    def test_review_package_independence(self):
        """Section 97: the reviewer package contains task/criteria/diff/
        evidence and NEVER the implementation transcript/final message."""
        job = {"job_id": "job_" + "1" * 10, "repository": "r",
               "goal": "implement greet correctly",
               "acceptance_criteria": TEST_CRITERIA}
        ev = dict(EVIDENCE)
        ev["agent_reason"] = "IMPLEMENTATION_AGENT_FINAL_MESSAGE_MARKER"
        pkg = rv.build_review_package(job, ev, {"cycle": 1})
        self.assertIn("implement greet correctly", pkg)
        self.assertIn("[ac1]", pkg)
        self.assertIn("pkg/greeter.py", pkg)
        self.assertIn("registered tests status: PASS", pkg)
        self.assertIn("content binding hash", pkg)
        self.assertNotIn("IMPLEMENTATION_AGENT_FINAL_MESSAGE_MARKER", pkg)
        self.assertNotIn("conversation", pkg.split("Output contract")[0])


class TestBridgeReviewHelpers(unittest.TestCase):
    def test_content_hash_detects_content_change(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            wt = Path(d) / "wt"
            (wt / "pkg").mkdir(parents=True)
            (wt / "pkg" / "a.py").write_text("x = 1\n")
            h1 = bridge.content_hash(str(wt), ["pkg/a.py"])
            (wt / "pkg" / "a.py").write_text("x = 2\n")
            h2 = bridge.content_hash(str(wt), ["pkg/a.py"])
            h3 = bridge.content_hash(str(wt), ["pkg/a.py"])
            self.assertNotEqual(h1, h2)
            self.assertEqual(h2, h3)

    def test_review_finalize_never_overwrites_cancelled(self):
        """Section 71: a reviewer finishing after a cancel stays cancelled."""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            saved_root = bridge.ROOT
            bridge.ROOT = Path(d)
            try:
                jid = "job_" + "a" * 10
                rid = "rev_" + "b" * 10
                bridge.write_review_state(jid, rid, {
                    "review_id": rid, "job_id": jid, "cycle": 1,
                    "state": "cancelled", "cancel_requested_at": bridge.now()})
                st = {"review_id": rid, "job_id": jid, "state": "running"}
                final = bridge._commit_review_terminal_state(
                    jid, rid, st, "finished")
                self.assertEqual(final, "cancelled")
            finally:
                bridge.ROOT = saved_root

    def test_review_start_rejects_unexpected_fields(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            saved_root = bridge.ROOT
            bridge.ROOT = Path(d)
            (Path(d) / "bridge").mkdir()
            (Path(d) / "bridge" / "config.json").write_text(json.dumps({
                "version": 1, "image": "x",
                "agents": {"rev": "cmd"},
                "reviewer_agent": "rev",
                "repositories": {"r": {"path": str(Path(d) / "repo"),
                                       "test_commands": [],
                                       "allowed_paths": ["pkg/"]}}}))
            try:
                res = bridge.a_review_start({
                    "version": 1, "action": "review_start",
                    "job_id": "job_" + "a" * 10, "review_id": "rev_" + "b" * 10,
                    "cycle": 1, "repo_id": "r", "target_sha": "a" * 40,
                    "prompt": "p", "model": "arbitrary-model"})
                self.assertEqual(res.get("error"), "unexpected_fields")
                self.assertIn("model", res.get("fields", []))
            finally:
                bridge.ROOT = saved_root


if __name__ == "__main__":
    unittest.main()
