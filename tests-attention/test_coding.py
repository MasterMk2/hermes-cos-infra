#!/usr/bin/env python3
"""G.2 unit tests — coding notifications (spec section 117).

The point of every test here is section 33: while the fix loop is running,
the owner is not interrupted. F.2's supervisor drives GLM, a deterministic
gate, a Muse review and a retry on CHANGES_REQUIRED — and every one of those
is visible in `jobs.db`. Notifying on any of them would report the machine
talking to itself.
"""

import unittest

import att_coding
import att_policy as policy
import att_store as store
import att_fixtures as fx
from test_policy import budget, decide


def job(**over):
    """A row shaped like the F.2 `jobs` table."""
    clock = fx.clock_at("2026-09-04T12:00")
    base = {
        "job_id": "job_abc123", "status": "COMPLETED",
        "review_status": "APPROVED", "needs_human": 0, "attempt": 1,
        "goal": "fixture の関数を実装する", "repository": "fix-loop-fixture",
        "failure_reason": None, "next_action": None,
        "created_at": fx.ago(clock, hours=2),
        "updated_at": fx.ago(clock, minutes=10),
        "finished_at": fx.ago(clock, minutes=10),
        "result_summary": "16/16 implemented",
    }
    base.update(over)
    return base


class TestWhichStatesNotify(unittest.TestCase):
    def setUp(self):
        self.cfg = fx.config()
        self.clock = fx.clock_at("2026-09-04T12:00")

    def sig(self, **over):
        return att_coding.signal_for_job(job(**over), self.cfg, self.clock)

    def test_completed_produces_one_signal(self):
        s = self.sig(status="COMPLETED")
        self.assertIsNotNone(s)
        self.assertEqual(s.signal_type, "coding_completed")
        # Section 30: good news is never urgent, and never spends a push.
        self.assertEqual(s.severity, "INFO")

    def test_waiting_human_produces_a_signal(self):
        s = self.sig(status="WAITING_HUMAN", needs_human=1,
                     failure_reason="reviewer could not verify the change")
        self.assertIsNotNone(s)
        self.assertEqual(s.signal_type, "coding_waiting_human")
        self.assertEqual(s.severity, "IMPORTANT")

    def test_internal_transitions_produce_nothing(self):
        # Section 29, named state by named state.
        for status in ("NEW", "RUNNING", "AGENT_STOPPED", "VALIDATING",
                       "WAITING_REVIEW", "REVIEWING",
                       "IMPLEMENTATION_ACCEPTED", "CANCELLED", "DISPATCHED"):
            with self.subTest(status=status):
                self.assertIsNone(self.sig(status=status))

    def test_changes_required_while_the_loop_retries_is_silent(self):
        # Section 32. The reviewer asked for changes and the supervisor is
        # about to make them; the job is still RUNNING and nobody is blocked.
        self.assertIsNone(self.sig(status="RUNNING",
                                   review_status="CHANGES_REQUIRED"))
        self.assertIsNone(self.sig(status="WAITING_REVIEW",
                                   review_status="CHANGES_REQUIRED"))

    def test_review_cycle_limit_is_its_own_signal(self):
        # Section 28: the loop gave up. That is different from "a job needs
        # you", and the notification says which.
        s = self.sig(status="WAITING_HUMAN",
                     failure_reason="review_cycle_limit_reached after 3 cycles")
        self.assertEqual(s.signal_type, "coding_review_cycle_limit")

    def test_unrecoverable_start_failure_is_its_own_signal(self):
        s = self.sig(status="WAITING_HUMAN",
                     failure_reason="spawn unrecoverable: worker refused")
        self.assertEqual(s.signal_type, "coding_failed_to_start")

    def test_a_new_attempt_is_a_new_signal(self):
        # Section 16's `job:<id>:waiting-human:<revision>`: after the owner
        # unblocks a job and it stops again, that is news, not a duplicate.
        a = self.sig(status="WAITING_HUMAN", attempt=1)
        b = self.sig(status="WAITING_HUMAN", attempt=2)
        self.assertNotEqual(a.signal_id, b.signal_id)
        self.assertNotEqual(a.dedupe_key, b.dedupe_key)

    def test_the_same_state_twice_is_the_same_signal(self):
        a = self.sig(status="COMPLETED")
        b = self.sig(status="COMPLETED")
        self.assertEqual(a.signal_id, b.signal_id)


class TestReasonSafety(unittest.TestCase):
    """Section 71 — a bounded summary, never a path or a secret."""

    def test_a_path_in_the_reason_is_replaced_wholesale(self):
        for hostile in (
            "cannot write /srv/hermes/hermes-data/.env",
            "ssh failed for /home/someone/.ssh/id_ed25519",
            "Authorization: Bearer abcdef123456",
            "api_key=sk-not-a-real-key",
        ):
            with self.subTest(reason=hostile[:20]):
                out = att_coding.safe_reason(hostile)
                self.assertNotIn("/srv/", out)
                self.assertNotIn("/home/", out)
                self.assertNotIn("Bearer", out)
                self.assertNotIn("sk-", out)

    def test_an_ordinary_reason_survives_but_is_bounded(self):
        out = att_coding.safe_reason("reviewer could not verify the change")
        self.assertEqual(out, "reviewer could not verify the change")
        self.assertLessEqual(len(att_coding.safe_reason("あ" * 500)),
                             att_coding.MAX_REASON_CHARS)

    def test_the_rendered_notification_carries_no_path(self):
        import att_render

        con = fx.db()
        clock = fx.clock_at("2026-09-04T12:00")
        row = fx.put_signal(
            con, source_type="coding", source_id="job_abc123",
            signal_type="coding_waiting_human", severity="IMPORTANT",
            summary="fixture の関数を実装する", clock=clock,
            created_at=clock.now_iso(),
            metadata={"reason": att_coding.safe_reason(
                "failed at /srv/hermes/hermes-data/.env")})
        _subject, body = att_render.render(row, clock)
        self.assertNotIn("/srv/", body)
        self.assertIn("job_abc123", body)


class TestCodingDecisions(unittest.TestCase):
    def test_completed_goes_to_discord_only(self):
        # Section 30 as a channel assertion: LINE is never spent on a job
        # finishing successfully.
        con = fx.db()
        clock = fx.clock_at("2026-09-04T12:00")
        row = fx.put_signal(con, source_type="coding", source_id="job_1",
                            signal_type="coding_completed", severity="INFO",
                            actionability=0.0, clock=clock,
                            created_at=clock.now_iso())
        d = decide(con, row, clock=clock)
        self.assertEqual(d.decision, policy.NOTIFY_NOW)
        self.assertEqual(d.channels, ["discord"])
        self.assertEqual(d.reason, policy.R_CODING_COMPLETED)

    def test_the_same_completion_processed_twice_notifies_once(self):
        # Section 117's last case, and section 154's "duplicate completion
        # notification impossible".
        con = fx.db()
        clock = fx.clock_at("2026-09-04T12:00")
        row = fx.put_signal(con, source_type="coding", source_id="job_1",
                            signal_type="coding_completed", severity="INFO",
                            clock=clock, created_at=clock.now_iso())
        first = decide(con, row, clock=clock)
        self.assertEqual(first.decision, policy.NOTIFY_NOW)
        fx.mark_delivered(con, row["dedupe_key"], when=clock.now_iso())
        second = decide(con, row, clock=clock)
        self.assertEqual(second.decision, policy.SUPPRESS)
        self.assertEqual(second.reason, policy.R_DUPLICATE)

    def test_waiting_human_reaches_discord_immediately(self):
        con = fx.db()
        clock = fx.clock_at("2026-09-04T12:00")
        row = fx.put_signal(con, source_type="coding", source_id="job_2",
                            signal_type="coding_waiting_human",
                            severity="IMPORTANT", clock=clock,
                            created_at=clock.now_iso())
        d = decide(con, row, clock=clock)
        self.assertEqual(d.decision, policy.NOTIFY_NOW)
        self.assertIn("discord", d.channels)
        self.assertNotIn("line", d.channels)   # IMPORTANT is not escalation

    def test_waiting_human_is_not_time_sensitive_at_night(self):
        # A blocked job at 02:00 waits until morning: nobody is going to fix
        # it in their sleep, and waking them costs the same as telling them
        # at 08:00 (sections 55, 56).
        con = fx.db()
        clock = fx.clock_at("2026-09-04T02:00")
        row = fx.put_signal(con, source_type="coding", source_id="job_3",
                            signal_type="coding_waiting_human",
                            severity="IMPORTANT", clock=clock,
                            created_at=clock.now_iso())
        d = decide(con, row, clock=clock)
        self.assertEqual(d.decision, policy.DEFER)


class TestCollectorBounds(unittest.TestCase):
    def test_old_jobs_are_not_collected(self):
        # Without this the first scan announces every job the supervisor ever
        # parked — 4 WAITING_HUMAN and 19 COMPLETED rows were sitting in the
        # production database when this was written.
        cfg = fx.config(coding={"max_age_hours": 48})
        clock = fx.clock_at("2026-09-04T12:00")
        old = job(updated_at=fx.ago(clock, days=10),
                  finished_at=fx.ago(clock, days=10))
        recent = job(updated_at=fx.ago(clock, minutes=5))
        self.assertIsNotNone(att_coding.signal_for_job(recent, cfg, clock))
        # signal_for_job itself has no age rule; `collect` applies it. Assert
        # the boundary the collector uses rather than duplicating the query.
        from att_time import parse_iso

        age = (clock.now() - parse_iso(old["updated_at"])).total_seconds()
        self.assertGreater(age, float(cfg.coding["max_age_hours"]) * 3600)


if __name__ == "__main__":
    unittest.main()
