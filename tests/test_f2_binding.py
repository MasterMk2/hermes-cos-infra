#!/usr/bin/env python3
"""Phase F.2 addendum tests — revision binding + fix-loop progress evidence.

These cover the gaps found when auditing the first real F.2 fix-loop run
(job_4f1b65121d), where review cycle 1 said CHANGES_REQUIRED and cycle 2 said
APPROVE while the canonical content binding was byte-identical:

  section 53   the approved diff hash must equal the current diff hash
  section 61   a re-review after a fix must bind a moved revision -- and when
               it does not, that fact must be recorded (section 107 still
               allows the implementation to rebut a false-positive finding, so
               this is evidence, not a hard block)
  section 104  a cycle that converged WITHOUT a code change is not fix-loop
               evidence and must be distinguishable in the review history
  section 110  no job kind has a review-less path to COMPLETED
  section 28   the review schema migration is additive and idempotent
"""

import unittest

import helpers  # noqa: F401  (sys.path setup)
from helpers import Sandbox, make_fixture_repo

import jobs_db           # noqa: E402
import review as rv      # noqa: E402
import supervisor        # noqa: E402

from test_f2_review import (BASE_SHA, CONTENT_SHA, DIFF_SHA, F2ReviewTestBase,
                            FakeBridge, HEAD_SHA, PROD_AGENT)

OTHER_CONTENT_SHA = "e" * 64
OTHER_DIFF_SHA = "f" * 16


class FlippingBridge(FakeBridge):
    """FakeBridge whose verdict and content binding can change between review
    cycles, so a two-cycle fix loop can be driven deterministically."""

    def __init__(self, modes, contents, **kw):
        super().__init__(modes[0], **kw)
        self._modes = list(modes)
        self._contents = list(contents)
        self._cycle = 0
        self.snap_content = self._contents[0]

    def next_cycle(self):
        self._cycle += 1
        self.mode = self._modes[min(self._cycle, len(self._modes) - 1)]
        self.snap_content = self._contents[min(self._cycle,
                                               len(self._contents) - 1)]
        self.status_polls = 0


class TestDiffHashBinding(F2ReviewTestBase):
    def test_completion_rejects_stale_diff_hash(self):
        """Section 53: approval binds the exact diff hash; a changed diff hash
        invalidates the approval even when SHA and content still match."""
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            jid = self.accept_job(sb, repo)
            fb = FakeBridge("approve")
            self.install_bridge(fb)
            self.assertEqual(supervisor.run_review_phase(
                jid, supervisor.load_config()), "APPROVED_PRESENT")
            rec = jobs_db.latest_approved_review(jid)
            self.assertEqual(rec["diff_hash"], DIFF_SHA)

            # the committed diff moved while SHA + content binding did not
            jobs_db.update_review(rec["review_id"], diff_hash=OTHER_DIFF_SHA)
            ok, why, _ = supervisor.completion_decision(jid)
            self.assertFalse(ok)
            self.assertEqual(why, "stale_diff_hash")

    def test_stale_diff_hash_reopens_review_instead_of_completing(self):
        """Section 51: a stale binding sends the job back to a fresh review,
        never to COMPLETED."""
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            jid = self.accept_job(sb, repo)
            self.install_bridge(FakeBridge("approve"))
            supervisor.run_review_phase(jid, supervisor.load_config())
            rec = jobs_db.latest_approved_review(jid)
            jobs_db.update_review(rec["review_id"], diff_hash=OTHER_DIFF_SHA)

            out = supervisor.finalize_completion(jid)
            self.assertEqual(out, "REVIEW_AGAIN")
            job = jobs_db.get_job(jid)
            self.assertEqual(job["status"], "IMPLEMENTATION_ACCEPTED")
            self.assertEqual(job["review_status"], "WAITING_REVIEW")

    def test_legacy_review_row_without_diff_hash_still_completes(self):
        """A review recorded before the diff-hash binding existed must not be
        retro-invalidated (additive change, no behaviour regression)."""
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            jid = self.accept_job(sb, repo)
            self.install_bridge(FakeBridge("approve"))
            supervisor.run_review_phase(jid, supervisor.load_config())
            rec = jobs_db.latest_approved_review(jid)
            jobs_db.update_review(rec["review_id"], diff_hash=None)
            ok, why, _ = supervisor.completion_decision(jid)
            self.assertTrue(ok, why)


class TestFindingScopeIsEnforcedInProduction(F2ReviewTestBase):
    """Section 37: a finding path must be repo-relative AND inside the
    repository's allowed paths.

    The validator has always had the scope half, but it is gated behind
    `if repo_dirs` — so it did nothing until the production call site
    actually passed the registry's allowed_paths. These tests pin the wiring,
    not just the validator, which is what the earlier version was missing."""

    def _bridge_with_finding_path(self, path):
        fb = FakeBridge("changes_required")
        orig = fb._review_payload

        def payload():
            d = orig()
            d["result_json"]["findings"][0]["file"] = path
            return d
        fb._review_payload = payload
        return fb

    def test_in_scope_path_is_accepted(self):
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            jid = self.accept_job(sb, repo)   # allowed_paths: pkg/ tests/ README.md
            self.install_bridge(self._bridge_with_finding_path('pkg/greeter.py'))
            out = supervisor.run_review_phase(jid, supervisor.load_config())
            self.assertEqual(out, 'FIX_REQUIRED')
            self.assertEqual(jobs_db.latest_review(jid)['status'],
                             'changes_required')

    def test_out_of_scope_path_is_rejected_on_the_production_path(self):
        """A path that is repo-relative and traversal-free, but outside the
        repository's allowed_paths, must still be refused."""
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            jid = self.accept_job(sb, repo)
            self.install_bridge(self._bridge_with_finding_path('secrets/prod.env'))
            out = supervisor.run_review_phase(jid, supervisor.load_config())
            self.assertEqual(out, 'WAITING_HUMAN')
            job = jobs_db.get_job(jid)
            self.assertEqual(job['review_status'], 'REVIEW_FAILED')
            rows = jobs_db.reviews_for_job(jid)
            self.assertTrue(rows)
            self.assertIn('outside allowed repo scope',
                          rows[-1]['error_summary'] or '')

    def test_validator_still_rejects_traversal_without_any_scope_list(self):
        """The path checks that do not depend on repo_dirs keep working when
        no scope list is supplied at all."""
        for bad in ('../../etc/passwd', '/etc/shadow', 'a\\b.py'):
            res, err = rv.validate_review_output({
                'verdict': 'CHANGES_REQUIRED', 'summary': 's',
                'findings': [{'severity': 'high', 'category': 'correctness',
                              'file': bad, 'title': 't', 'explanation': 'e',
                              'blocking': True, 'confidence': 0.9}]},
                repo_scope_dirs=None)
            self.assertIsNone(res, bad)
            self.assertIn('path', err, bad)


class TestContentProgressEvidence(F2ReviewTestBase):
    def test_first_cycle_is_marked_first_cycle(self):
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            jid = self.accept_job(sb, repo)
            self.install_bridge(FakeBridge("approve"))
            supervisor.run_review_phase(jid, supervisor.load_config())
            self.assertEqual(jobs_db.latest_review(jid)["content_progress"],
                             "first_cycle")

    def test_real_fix_loop_records_changed(self):
        """Section 104/130: CHANGES_REQUIRED -> the implementation actually
        moves -> fresh APPROVE is marked 'changed' (real fix-loop evidence)."""
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            jid = self.accept_job(sb, repo)
            fb = FlippingBridge(["changes_required", "approve"],
                                [CONTENT_SHA, OTHER_CONTENT_SHA])
            self.install_bridge(fb)
            self.assertEqual(supervisor.run_review_phase(
                jid, supervisor.load_config()), "FIX_REQUIRED")

            # the fix ran and changed the code: same SHA topology, new content
            fb.next_cycle()
            jobs_db.update_job(jid, status="IMPLEMENTATION_ACCEPTED",
                               review_status="WAITING_REVIEW")
            self.assertEqual(supervisor.run_review_phase(
                jid, supervisor.load_config()), "APPROVED_PRESENT")

            rows = jobs_db.reviews_for_job(jid)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["content_progress"], "first_cycle")
            self.assertEqual(rows[1]["content_progress"], "changed")
            self.assertEqual(rows[1]["verdict"], "APPROVE")

    def test_approve_without_any_code_change_is_marked_unchanged(self):
        """Section 61 vs 107: the reviewer may converge on an unchanged
        revision (rebutted false positive), but the review record must say so
        -- it is NOT section 104 fix-loop evidence."""
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            jid = self.accept_job(sb, repo)
            fb = FlippingBridge(["changes_required", "approve"],
                                [CONTENT_SHA, CONTENT_SHA])
            self.install_bridge(fb)
            self.assertEqual(supervisor.run_review_phase(
                jid, supervisor.load_config()), "FIX_REQUIRED")

            fb.next_cycle()
            jobs_db.update_job(jid, status="IMPLEMENTATION_ACCEPTED",
                               review_status="WAITING_REVIEW")
            self.assertEqual(supervisor.run_review_phase(
                jid, supervisor.load_config()), "APPROVED_PRESENT")

            rows = jobs_db.reviews_for_job(jid)
            self.assertEqual(rows[1]["content_progress"], "unchanged")
            # section 113/114: the fact is auditable in the event log too
            con = jobs_db.connect()
            try:
                msgs = [r[0] for r in con.execute(
                    "SELECT message FROM event_log WHERE job_id=?", (jid,))]
            finally:
                con.close()
            self.assertTrue(any("UNCHANGED content" in m for m in msgs), msgs)

    def test_unchanged_convergence_still_completes(self):
        """The marker is evidence, not a block: section 107's rebuttal path
        must stay automated."""
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            jid = self.accept_job(sb, repo)
            fb = FlippingBridge(["changes_required", "approve"],
                                [CONTENT_SHA, CONTENT_SHA])
            self.install_bridge(fb)
            supervisor.run_review_phase(jid, supervisor.load_config())
            fb.next_cycle()
            jobs_db.update_job(jid, status="IMPLEMENTATION_ACCEPTED",
                               review_status="WAITING_REVIEW")
            supervisor.run_review_phase(jid, supervisor.load_config())
            self.assertEqual(supervisor.finalize_completion(jid), "COMPLETED")


class TestReviewlessCompletionGuardAllKinds(F2ReviewTestBase):
    def test_test_kind_job_skips_review_but_can_never_complete(self):
        """Section 68/110/111: kind=test skips review by policy -- and because
        completion_decision() is the only writer of COMPLETED, skipping review
        does NOT buy a completion. The job parks at IMPLEMENTATION_ACCEPTED."""
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            jid = self.accept_job(sb, repo, kind="test",
                                  agent="pi-sandbox-fixture")
            self.install_bridge(FakeBridge("approve"))
            self.assertEqual(supervisor.run_review_phase(
                jid, supervisor.load_config()), "NOT_REQUIRED")
            ok, why, _ = supervisor.completion_decision(jid)
            self.assertFalse(ok)
            self.assertEqual(why, "no_approval")
            self.assertEqual(jobs_db.get_job(jid)["status"],
                             "IMPLEMENTATION_ACCEPTED")

    def test_production_agent_with_test_kind_also_cannot_complete(self):
        """The kind=test escape hatch is not a review bypass even when the job
        runs on a real production worker agent."""
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            jid = self.accept_job(sb, repo, kind="test", agent=PROD_AGENT)
            self.install_bridge(FakeBridge("approve"))
            self.assertEqual(supervisor.run_review_phase(
                jid, supervisor.load_config()), "NOT_REQUIRED")
            ok, why, _ = supervisor.completion_decision(jid)
            self.assertFalse(ok)
            self.assertEqual(why, "no_approval")

    def test_only_completion_decision_writes_completed(self):
        """Section 55: there is exactly one COMPLETED writer in the supervisor
        source -- the CAS inside finalize_completion()."""
        import inspect
        src = inspect.getsource(supervisor)
        writers = [ln.strip() for ln in src.splitlines()
                   if '"COMPLETED"' in ln and
                   ("cas_status(" in ln or "transition(" in ln)]
        self.assertEqual(len(writers), 1, writers)
        self.assertIn("cas_status", writers[0])


class TestJobGetReviewSurface(F2ReviewTestBase):
    """Section 115/116: review state must be readable from job_get and
    job_list, so the Chief of Staff can tell WAITING_REVIEW from
    CHANGES_REQUIRED from COMPLETED without touching the DB."""

    FIELDS = ("review_status", "review_cycle", "review_model",
              "latest_review_summary", "blocking_findings_count",
              "approved_target_sha")

    def test_job_get_exposes_the_review_surface(self):
        import jobs_mcp
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            jid = self.accept_job(sb, repo)
            self.install_bridge(FakeBridge("approve"))
            supervisor.run_review_phase(jid, supervisor.load_config())

            out = jobs_mcp._call("job_get", {"job_id": jid})
            for f in self.FIELDS:
                self.assertIn(f, out, f)
            self.assertEqual(out["review_status"], "APPROVED")
            self.assertEqual(out["review_cycle"], 1)
            self.assertEqual(out["review_model"], supervisor.REVIEWER_MODEL)
            self.assertEqual(out["blocking_findings_count"], 0)
            self.assertEqual(out["approved_target_sha"], HEAD_SHA[:12])
            self.assertEqual(out["latest_review_verdict"], "APPROVE")

    def test_job_list_exposes_the_same_surface(self):
        import jobs_mcp
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            jid = self.accept_job(sb, repo)
            self.install_bridge(FakeBridge("approve"))
            supervisor.run_review_phase(jid, supervisor.load_config())

            rows = jobs_mcp._call("job_list", {})["jobs"]
            row = [r for r in rows if r["job_id"] == jid][0]
            for f in self.FIELDS:
                self.assertIn(f, row, f)

    def test_changes_required_is_visible_before_any_approval(self):
        import jobs_mcp
        with Sandbox() as sb:
            repo = make_fixture_repo(sb.dir)
            jid = self.accept_job(sb, repo)
            self.install_bridge(FakeBridge("changes_required"))
            supervisor.run_review_phase(jid, supervisor.load_config())

            out = jobs_mcp._call("job_get", {"job_id": jid})
            self.assertEqual(out["review_status"], "CHANGES_REQUIRED")
            self.assertEqual(out["latest_review_verdict"], "CHANGES_REQUIRED")
            self.assertGreaterEqual(out["blocking_findings_count"], 1)
            # nothing is approved yet, so no approval binding is advertised
            self.assertNotIn("approved_target_sha", out)


class TestUsageCostHonesty(unittest.TestCase):
    """Section 76: no invented cost ever reaches the jobs DB."""

    def _write(self, d, name, payload):
        import json as _json
        (d / name).write_text(_json.dumps(payload))

    def test_unknown_cost_aggregates_to_unavailable(self):
        import tempfile
        from pathlib import Path as _P
        import bridge
        with tempfile.TemporaryDirectory() as td:
            d = _P(td)
            # exactly what the Hermes CLI writes when CommandCode returns no cost
            self._write(d, "usage-attempt-1.json", {
                "estimated_cost_usd": 0.0, "cost_status": "unknown",
                "cost_source": "none", "input_tokens": 100,
                "output_tokens": 50, "total_tokens": 150, "api_calls": 2,
                "model": "z-ai/glm-5.3-flash", "provider": "commandcode"})
            agg = bridge.usage_summary_dir(d, "usage-attempt-*.json")
        self.assertEqual(agg["cost_status"], "unavailable")
        self.assertIsNone(agg["estimated_cost_usd"])
        # tokens are still kept so a cost can be computed later, outside the DB
        self.assertEqual(agg["total_tokens"], 150)
        self.assertEqual(agg["api_calls"], 2)

    def test_provider_reported_cost_is_kept(self):
        import tempfile
        from pathlib import Path as _P
        import bridge
        with tempfile.TemporaryDirectory() as td:
            d = _P(td)
            self._write(d, "usage-attempt-1.json", {
                "estimated_cost_usd": 0.0125, "cost_status": "exact",
                "cost_source": "provider", "input_tokens": 100,
                "total_tokens": 150, "api_calls": 2})
            agg = bridge.usage_summary_dir(d, "usage-attempt-*.json")
        self.assertEqual(agg["cost_status"], "provider_reported")
        self.assertAlmostEqual(agg["estimated_cost_usd"], 0.0125)

    def test_attempt_entries_carry_model_and_cost_status(self):
        """Section 74/100: per-attempt model identity is preserved, so the
        implementation model and the reviewer model can be told apart."""
        import tempfile
        from pathlib import Path as _P
        import bridge
        with tempfile.TemporaryDirectory() as td:
            d = _P(td)
            self._write(d, "review-usage-1.json", {
                "estimated_cost_usd": 0.0, "cost_status": "unknown",
                "input_tokens": 10, "total_tokens": 20, "api_calls": 1,
                "model": "meta/muse-spark-1.2", "provider": "commandcode"})
            agg = bridge.usage_summary_dir(d, "review-usage-*.json")
        entry = agg["attempts"][0]
        self.assertEqual(entry["model"], "meta/muse-spark-1.2")
        self.assertEqual(entry["provider"], "commandcode")
        self.assertEqual(entry["cost_status"], "unknown")


class TestReviewMigrationIdempotent(unittest.TestCase):
    def test_init_db_twice_is_safe_and_additive(self):
        """Section 28: running the migration twice changes nothing and never
        drops rows."""
        with Sandbox() as sb:  # noqa: F841  (Sandbox points jobs_db at a temp DB)
            jobs_db.init_db()
            jid = jobs_db.create_job(goal="g", repository="r",
                                     acceptance_criteria=[], agent=PROD_AGENT,
                                     kind="implementation")
            rid = jobs_db.create_review(jid, 1, "agent", "model",
                                        base_sha=BASE_SHA, target_sha=HEAD_SHA,
                                        diff_hash=DIFF_SHA,
                                        content_sha=CONTENT_SHA)
            jobs_db.update_review(rid, content_progress="changed")

            jobs_db.init_db()  # second run must be a no-op
            jobs_db.init_db()  # and a third

            self.assertIsNotNone(jobs_db.get_job(jid))
            r = jobs_db.get_review(rid)
            self.assertEqual(r["content_sha"], CONTENT_SHA)
            self.assertEqual(r["diff_hash"], DIFF_SHA)
            self.assertEqual(r["content_progress"], "changed")

    def test_reviews_table_has_the_documented_columns(self):
        """Section 27: the immutable review history keeps every documented
        field."""
        with Sandbox():
            jobs_db.init_db()
            con = jobs_db.connect()
            try:
                cols = {r[1] for r in
                        con.execute("PRAGMA table_info(reviews)").fetchall()}
            finally:
                con.close()
            for name in ("review_id", "job_id", "cycle", "status",
                         "reviewer_agent", "reviewer_model", "base_sha",
                         "target_sha", "diff_hash", "content_sha",
                         "started_at", "finished_at", "verdict", "summary",
                         "findings_json", "blocking_count",
                         "fingerprints_json", "usage_json", "error_summary",
                         "attempt_no", "content_progress"):
                self.assertIn(name, cols)


if __name__ == "__main__":
    unittest.main()
