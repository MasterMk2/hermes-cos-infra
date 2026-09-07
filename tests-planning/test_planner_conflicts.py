"""Conflict detection (phase sections 29, 30, 82).

Production supplies no genuine overlapping window and no NULL
`scheduled_end_at` — the only two literal window overlaps in the entire dataset
are the duplicate pairs that deduplication removes first — so every case here
is synthetic by necessity, and that is recorded rather than hidden.

Phase section 30 caps what a conflict may claim. These tests pin that the
detail text comes from a fixed table keyed by kind, that nothing infers travel
time or difficulty, and that a deadline whose clock time is *synthetic* never
produces a "deadline during a meeting" claim.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import planning_fixtures as fx  # noqa: E402

from planning.plan_models import CONFLICT_DETAILS, ConflictKind  # noqa: E402


def kinds(plan):
    return [c["kind"] for c in plan["conflicts"]]


class OverlapTest(unittest.TestCase):
    def setUp(self):
        self.conn = fx.open_db()
        fx.reset_ids()
        self.addCleanup(self.conn.close)

    def _slot(self, cid, start, end, **overrides):
        payload = dict(
            commitment_id=cid, title=f"{cid} の予定", work_state="SCHEDULED",
            scheduled_at=start, scheduled_end_at=end,
            authority="calendar_authority", confidence=1.0,
            evidence=[("calendar", f"calendar:{cid}")],
        )
        payload.update(overrides)
        return fx.add_commitment(self.conn, **payload)

    def test_overlapping_scheduled_events_are_detected(self):
        self._slot("cmt_a", "2026-09-08T00:00:00+00:00", "2026-09-08T01:30:00+00:00")
        self._slot("cmt_b", "2026-09-08T01:00:00+00:00", "2026-09-08T02:00:00+00:00")
        plan = fx.build_plan(self.conn)
        self.assertEqual(kinds(plan), [ConflictKind.OVERLAPPING_SCHEDULED.value])
        conflict = plan["conflicts"][0]
        self.assertEqual(conflict["commitment_ids"], ["cmt_a", "cmt_b"])
        self.assertEqual(conflict["window_start"], "2026-09-08T09:00:00+09:00")
        self.assertEqual(conflict["window_end"], "2026-09-08T11:00:00+09:00")
        self.assertEqual(plan["counts"]["conflicts"], {"shown": 1, "total": 1})

    def test_back_to_back_events_are_dense_not_overlapping(self):
        self._slot("cmt_a", "2026-09-08T00:00:00+00:00", "2026-09-08T01:00:00+00:00")
        self._slot("cmt_b", "2026-09-08T01:00:00+00:00", "2026-09-08T02:00:00+00:00")
        plan = fx.build_plan(self.conn)
        self.assertEqual(kinds(plan), [ConflictKind.DENSE_WINDOW.value])
        self.assertEqual(plan["conflicts"][0]["detail"], "予定が連続しています")

    def test_a_wide_gap_is_not_a_conflict(self):
        self._slot("cmt_a", "2026-09-08T00:00:00+00:00", "2026-09-08T01:00:00+00:00")
        self._slot("cmt_b", "2026-09-08T06:00:00+00:00", "2026-09-08T07:00:00+00:00")
        plan = fx.build_plan(self.conn)
        self.assertEqual(plan["conflicts"], [])

    def test_the_dense_window_is_configurable(self):
        # 09:00-10:00 JST then 12:00-13:00 JST: a two-hour gap.
        self._slot("cmt_a", "2026-09-08T00:00:00+00:00", "2026-09-08T01:00:00+00:00")
        self._slot("cmt_b", "2026-09-08T03:00:00+00:00", "2026-09-08T04:00:00+00:00")
        self.assertEqual(fx.build_plan(self.conn)["conflicts"], [])
        widened = fx.build_plan(self.conn, config={"windows": {"dense_window_minutes": 120}})
        self.assertEqual(kinds(widened), [ConflictKind.DENSE_WINDOW.value])

    def test_an_all_day_marker_is_not_an_occupied_window(self):
        """AMENDMENT 1 B2: the only production case of two items on one day is
        an all-day event beside a timed one. Calling that a conflict asserts
        more than the data supports."""
        self._slot("cmt_all", "2026-09-08", "2026-09-09")
        self._slot("cmt_timed", "2026-09-08T13:00:00+00:00", "2026-09-08T14:00:00+00:00")
        plan = fx.build_plan(self.conn)
        self.assertEqual(len(plan["scheduled"]), 2)
        self.assertEqual(plan["conflicts"], [])

    def test_two_all_day_events_do_not_conflict_with_each_other(self):
        self._slot("cmt_a", "2026-09-08", "2026-09-09")
        self._slot("cmt_b", "2026-09-07", "2026-09-10")
        plan = fx.build_plan(self.conn)
        self.assertEqual(len(plan["scheduled"]), 2)
        self.assertEqual(plan["conflicts"], [])

    def test_null_end_falls_back_to_the_configured_duration(self):
        """0 of the 81 production scheduled rows lack an end; this is the
        defensive path only."""
        self._slot("cmt_a", "2026-09-08T00:00:00+00:00", None)
        self._slot("cmt_b", "2026-09-08T00:30:00+00:00", "2026-09-08T01:30:00+00:00")
        plan = fx.build_plan(self.conn)
        self.assertEqual(kinds(plan), [ConflictKind.OVERLAPPING_SCHEDULED.value])
        narrowed = fx.build_plan(
            self.conn, config={"windows": {"conflict_default_duration_minutes": 15,
                                           "dense_window_minutes": 5}},
        )
        self.assertEqual(narrowed["conflicts"], [])

    def test_conflicts_are_found_even_when_the_plan_caps_the_list(self):
        self._slot("cmt_a", "2026-09-08T00:00:00+00:00", "2026-09-08T01:30:00+00:00")
        self._slot("cmt_b", "2026-09-08T01:00:00+00:00", "2026-09-08T02:00:00+00:00")
        plan = fx.build_plan(self.conn, config={"limits": {"scheduled": 1}})
        self.assertEqual(len(plan["scheduled"]), 1)
        self.assertEqual(plan["counts"]["scheduled"], {"shown": 1, "total": 2})
        self.assertEqual(kinds(plan), [ConflictKind.OVERLAPPING_SCHEDULED.value])

    def test_conflict_order_is_deterministic(self):
        self._slot("cmt_a", "2026-09-08T00:00:00+00:00", "2026-09-08T01:30:00+00:00")
        self._slot("cmt_b", "2026-09-08T01:00:00+00:00", "2026-09-08T02:00:00+00:00")
        self._slot("cmt_c", "2026-09-08T06:00:00+00:00", "2026-09-08T07:30:00+00:00")
        self._slot("cmt_d", "2026-09-08T07:00:00+00:00", "2026-09-08T08:00:00+00:00")
        first = fx.build_plan(self.conn)["conflicts"]
        for _ in range(3):
            self.assertEqual(fx.build_plan(self.conn)["conflicts"], first)
        self.assertEqual(
            [c["commitment_ids"] for c in first],
            [["cmt_a", "cmt_b"], ["cmt_c", "cmt_d"]],
        )


class DeadlineDuringScheduleTest(unittest.TestCase):
    def setUp(self):
        self.conn = fx.open_db()
        fx.reset_ids()
        fx.add_commitment(
            self.conn, commitment_id="cmt_slot", title="長い会議",
            work_state="SCHEDULED",
            scheduled_at="2026-09-08T04:00:00+00:00",     # 13:00-16:00 JST
            scheduled_end_at="2026-09-08T07:00:00+00:00",
            authority="calendar_authority", confidence=1.0,
            evidence=[("calendar", "calendar:slot")],
        )
        self.addCleanup(self.conn.close)

    def test_a_precise_deadline_inside_a_meeting_is_a_conflict(self):
        fx.add_commitment(
            self.conn, commitment_id="cmt_task", title="14時締切の提出",
            due_at="2026-09-08T14:00:00+09:00", due_precision="exact_datetime",
            evidence="discord",
        )
        plan = fx.build_plan(self.conn)
        self.assertEqual(kinds(plan), [ConflictKind.DEADLINE_DURING_SCHEDULED.value])
        conflict = plan["conflicts"][0]
        self.assertEqual(conflict["commitment_ids"], ["cmt_slot", "cmt_task"])
        self.assertEqual(conflict["detail"], "予定の時間帯に締切があります")

    def test_a_coarse_deadline_never_produces_a_fake_precision_conflict(self):
        """`week` / `daypart` rows carry a synthetic clock time. Reporting a
        conflict from one would claim precision the data does not have."""
        fx.add_commitment(
            self.conn, commitment_id="cmt_task", title="今週中の作業",
            due_at="2026-09-08T14:00:00+09:00", due_precision="week",
            evidence="discord",
        )
        plan = fx.build_plan(self.conn)
        self.assertEqual(len(plan["must_do"]), 1)
        self.assertEqual(plan["conflicts"], [])

    def test_a_date_only_deadline_produces_no_conflict(self):
        fx.add_commitment(
            self.conn, commitment_id="cmt_task", title="日付だけの締切",
            due_at="2026-09-08", due_precision="date", evidence="discord",
        )
        plan = fx.build_plan(self.conn)
        self.assertEqual(len(plan["must_do"]), 1)
        self.assertIsNone(plan["must_do"][0]["local_time"])
        self.assertEqual(plan["conflicts"], [])

    def test_a_deadline_outside_the_window_is_not_a_conflict(self):
        fx.add_commitment(
            self.conn, commitment_id="cmt_task", title="夜の締切",
            due_at="2026-09-08T20:00:00+09:00", due_precision="minute",
            evidence="discord",
        )
        self.assertEqual(fx.build_plan(self.conn)["conflicts"], [])


class ConflictVocabularyTest(unittest.TestCase):
    def test_detail_is_a_fixed_closed_set_keyed_by_kind(self):
        self.assertEqual(
            set(CONFLICT_DETAILS), {k.value for k in ConflictKind}
        )
        for kind, detail in CONFLICT_DETAILS.items():
            self.assertTrue(detail.strip(), kind)

    def test_no_detail_claims_anything_about_travel(self):
        """Phase section 30: with no travel-time data, say only that the
        schedule is tight — never that getting there is hard."""
        for detail in CONFLICT_DETAILS.values():
            for forbidden in ("移動", "間に合", "厳しい", "難しい", "急い"):
                self.assertNotIn(forbidden, detail)

    def test_every_emitted_conflict_uses_the_table(self):
        conn = fx.open_db()
        self.addCleanup(conn.close)
        fx.reset_ids()
        fx.seed_realistic_day(conn)
        plan = fx.build_plan(conn)
        self.assertTrue(plan["conflicts"])
        for conflict in plan["conflicts"]:
            self.assertEqual(conflict["detail"], CONFLICT_DETAILS[conflict["kind"]])
            self.assertEqual(conflict["commitment_ids"], sorted(conflict["commitment_ids"]))
            self.assertEqual(
                sorted(conflict), ["commitment_ids", "detail", "kind", "window_end", "window_start"]
            )


if __name__ == "__main__":
    unittest.main()
