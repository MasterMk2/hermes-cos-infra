"""Ordering, priority classes and plan-size caps (phase sections 25, 27, 28, 81).

The display order is the whole product here: an ordering that is not a total
order produces a brief that reshuffles between two runs of the same facts, and
`plan_hash` idempotency (phase section 58) then breaks. So every rule is
pinned, including the two categories that deliberately override the generic
tie-break — `overdue` (AMENDMENT 1 F1) and `backlog` (AMENDMENT 1 F2).
"""

import os
import sys
import unittest
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import planning_fixtures as fx  # noqa: E402

from planning import plan_config  # noqa: E402


def ids(plan, category):
    return [item["commitment_id"] for item in plan[category]]


class ScheduledOrderingTest(unittest.TestCase):
    def setUp(self):
        self.conn = fx.open_db()
        fx.reset_ids()
        self.addCleanup(self.conn.close)

    def test_scheduled_is_chronological_across_mixed_offsets(self):
        """The killer case: a +00:00 calendar row against a +09:00 row. String
        order would put the calendar row nine hours out of place."""
        late = fx.add_commitment(
            self.conn, title="夕方の予定", work_state="SCHEDULED",
            scheduled_at="2026-09-08T09:00:00+00:00",      # 18:00 JST
            scheduled_end_at="2026-09-08T10:00:00+00:00",
            authority="calendar_authority", evidence="calendar",
        )
        early = fx.add_commitment(
            self.conn, title="朝の予定", work_state="SCHEDULED",
            scheduled_at="2026-09-08T11:00:00+09:00",      # 11:00 JST
            scheduled_end_at="2026-09-08T12:00:00+09:00",
            evidence="discord",
        )
        plan = fx.build_plan(self.conn)
        self.assertEqual(ids(plan, "scheduled"), [early, late])
        self.assertEqual(
            [i["local_time"] for i in plan["scheduled"]], ["11:00", "18:00"]
        )
        # The naive string order is the opposite, which is the bug being pinned.
        self.assertLess("2026-09-08T09:00:00+00:00", "2026-09-08T11:00:00+09:00")

    def test_all_day_sorts_before_timed_items(self):
        timed = fx.add_commitment(
            self.conn, title="午前の予定", work_state="SCHEDULED",
            scheduled_at="2026-09-08T00:00:00+00:00",
            scheduled_end_at="2026-09-08T01:00:00+00:00",
            authority="calendar_authority", evidence="calendar",
        )
        all_day = fx.add_commitment(
            self.conn, title="終日の予定", work_state="SCHEDULED",
            scheduled_at="2026-09-08", scheduled_end_at="2026-09-09",
            authority="calendar_authority", evidence="calendar",
        )
        plan = fx.build_plan(self.conn)
        self.assertEqual(ids(plan, "scheduled"), [all_day, timed])


class TieBreakTest(unittest.TestCase):
    """The five-level tie-breaker of phase section 27, one level at a time."""

    def setUp(self):
        self.conn = fx.open_db()
        fx.reset_ids()
        self.addCleanup(self.conn.close)

    def test_level_2_due_at_ascending(self):
        late = fx.add_commitment(
            self.conn, title="20時", due_at="2026-09-08T20:00:00+09:00",
            due_precision="minute", evidence="discord",
        )
        early = fx.add_commitment(
            self.conn, title="09時", due_at="2026-09-08T09:00:00+09:00",
            due_precision="minute", evidence="discord",
        )
        self.assertEqual(ids(fx.build_plan(self.conn), "must_do"), [early, late])

    def test_level_4_created_at_breaks_an_identical_deadline(self):
        newer = fx.add_commitment(
            self.conn, commitment_id="cmt_aaa", title="後から作成",
            due_at="2026-09-08T12:00:00+09:00", due_precision="minute",
            created_at="2026-09-05T00:00:00+00:00", evidence="discord",
        )
        older = fx.add_commitment(
            self.conn, commitment_id="cmt_bbb", title="先に作成",
            due_at="2026-09-08T12:00:00+09:00", due_precision="minute",
            created_at="2026-09-01T00:00:00+00:00", evidence="discord",
        )
        # `cmt_aaa` sorts first by id, so only created_at can produce this order.
        self.assertEqual(ids(fx.build_plan(self.conn), "must_do"), [older, newer])

    def test_level_5_commitment_id_is_the_final_total_order(self):
        second = fx.add_commitment(
            self.conn, commitment_id="cmt_zzz", title="同着B",
            due_at="2026-09-08T12:00:00+09:00", due_precision="minute",
            created_at="2026-09-01T00:00:00+00:00", evidence="discord",
        )
        first = fx.add_commitment(
            self.conn, commitment_id="cmt_aaa", title="同着A",
            due_at="2026-09-08T12:00:00+09:00", due_precision="minute",
            created_at="2026-09-01T00:00:00+00:00", evidence="discord",
        )
        self.assertEqual(ids(fx.build_plan(self.conn), "must_do"), [first, second])

    def test_nulls_sort_last(self):
        dated = fx.add_commitment(
            self.conn, commitment_id="cmt_zzz", title="日付あり",
            work_state="WAITING", responsibility="COUNTERPARTY",
            due_at="2026-09-08T12:00:00+09:00", evidence="line",
        )
        undated = fx.add_commitment(
            self.conn, commitment_id="cmt_aaa", title="日付なし",
            work_state="WAITING", responsibility="COUNTERPARTY", evidence="line",
        )
        # `cmt_aaa` would win on id alone; a NULL due_at must push it behind.
        self.assertEqual(ids(fx.build_plan(self.conn), "waiting_for"), [dated, undated])

    def test_order_is_stable_across_repeated_builds(self):
        fx.seed_realistic_day(self.conn)
        first = fx.build_plan(self.conn)
        for _ in range(5):
            self.assertEqual(fx.build_plan(self.conn), first)


class OverdueOrderingTest(unittest.TestCase):
    """AMENDMENT 1 F1 — the deliberate deviation from the generic tie-break."""

    def setUp(self):
        self.conn = fx.open_db()
        fx.reset_ids()
        self.addCleanup(self.conn.close)

    def test_most_recent_deadline_first_not_oldest_first(self):
        oldest = fx.add_commitment(
            self.conn, title="最も古い", due_at="2026-08-24T19:00:00+09:00",
            due_precision="minute", counterparty="担当A", evidence="discord",
        )
        newest = fx.add_commitment(
            self.conn, title="最も新しい", due_at="2026-09-07T09:00:00+09:00",
            due_precision="minute", counterparty="担当B", evidence="discord",
        )
        middle = fx.add_commitment(
            self.conn, title="中間", due_at="2026-09-01T09:00:00+09:00",
            due_precision="minute", counterparty="担当C", evidence="discord",
        )
        plan = fx.build_plan(self.conn)
        self.assertEqual(ids(plan, "overdue"), [newest, middle, oldest])

    def test_per_counterparty_cap_keeps_one_thread_from_owning_the_list(self):
        dominant = [
            fx.add_commitment(
                self.conn, title=f"同じ相手 {day}", counterparty="担当A",
                due_at=f"2026-09-0{day}T09:00:00+09:00", due_precision="minute",
                evidence="discord",
            )
            for day in (7, 6, 5, 4)
        ]
        other = fx.add_commitment(
            self.conn, title="別の相手", counterparty="担当B",
            due_at="2026-09-03T09:00:00+09:00", due_precision="minute",
            evidence="discord",
        )
        plan = fx.build_plan(self.conn)
        shown = ids(plan, "overdue")
        self.assertEqual(shown, [dominant[0], dominant[1], other])
        self.assertEqual(plan["counts"]["overdue"], {"shown": 3, "total": 5})
        self.assertNotIn(dominant[2], shown)

    def test_each_null_counterparty_row_is_its_own_bucket(self):
        """Coordinator ruling: a missing counterparty is not a thread.

        Bucketing every NULL row together capped the whole overdue list at
        `overdue_per_counterparty_cap` (2), whatever `limits.overdue` was and
        however many *distinct* items existed — the reviewer's six-row case,
        reproduced here. Production carries such rows (I2-PROFILE.md C.2 has
        one), and showing 2 of 5 available slots defeats phase section 12's
        棚卸し可能.
        """
        created = [
            fx.add_commitment(
                self.conn, title=f"相手なし {day}", counterparty=None,
                due_at=f"2026-09-0{day}T09:00:00+09:00", due_precision="minute",
                evidence="discord",
            )
            for day in (7, 6, 5, 4, 3, 2)
        ]
        plan = fx.build_plan(self.conn)
        self.assertEqual(plan["counts"]["overdue"], {"shown": 5, "total": 6})
        self.assertEqual(ids(plan, "overdue"), created[:5])

    def test_a_blank_counterparty_is_treated_like_a_missing_one(self):
        """An empty string reaches the planner from the reconciler as easily
        as a NULL; both mean "nobody named", not "the same nobody"."""
        created = [
            fx.add_commitment(
                self.conn, title=f"空欄 {day}", counterparty="   ",
                due_at=f"2026-09-0{day}T09:00:00+09:00", due_precision="minute",
                evidence="discord",
            )
            for day in (7, 6, 5)
        ]
        plan = fx.build_plan(self.conn)
        self.assertEqual(ids(plan, "overdue"), created)

    def test_the_cap_still_binds_a_named_counterparty_among_null_rows(self):
        """The fix must not disarm the cap it was protecting."""
        named = [
            fx.add_commitment(
                self.conn, title=f"同じ相手 {day}", counterparty="担当A",
                due_at=f"2026-09-0{day}T09:00:00+09:00", due_precision="minute",
                evidence="discord",
            )
            for day in (7, 6, 5)
        ]
        anonymous = [
            fx.add_commitment(
                self.conn, title=f"相手なし {day}", counterparty=None,
                due_at=f"2026-09-0{day}T08:00:00+09:00", due_precision="minute",
                evidence="discord",
            )
            for day in (4, 3)
        ]
        plan = fx.build_plan(self.conn)
        shown = ids(plan, "overdue")
        self.assertNotIn(named[2], shown)
        self.assertEqual(shown, [named[0], named[1]] + anonymous)
        self.assertEqual(plan["counts"]["overdue"], {"shown": 4, "total": 5})

    def test_identical_deadlines_break_on_commitment_id(self):
        later_id = fx.add_commitment(
            self.conn, commitment_id="cmt_zzz", title="同着B", counterparty="担当A",
            due_at="2026-09-05T09:00:00+09:00", due_precision="minute", evidence="discord",
        )
        earlier_id = fx.add_commitment(
            self.conn, commitment_id="cmt_aaa", title="同着A", counterparty="担当B",
            due_at="2026-09-05T09:00:00+09:00", due_precision="minute", evidence="discord",
        )
        self.assertEqual(ids(fx.build_plan(self.conn), "overdue"), [earlier_id, later_id])

    def test_overdue_days_and_priority_split_at_seven_days(self):
        old = fx.add_commitment(
            self.conn, title="8日超過", counterparty="担当A",
            due_at="2026-08-31T09:00:00+09:00", due_precision="minute", evidence="discord",
        )
        recent = fx.add_commitment(
            self.conn, title="1日超過", counterparty="担当B",
            due_at="2026-09-07T09:00:00+09:00", due_precision="minute", evidence="discord",
        )
        plan = fx.build_plan(self.conn)
        by_id = {i["commitment_id"]: i for i in plan["overdue"]}
        self.assertEqual(by_id[old]["overdue_days"], 8)
        self.assertEqual(by_id[old]["priority_class"], "P1")
        self.assertEqual(by_id[recent]["overdue_days"], 1)
        self.assertEqual(by_id[recent]["priority_class"], "P2")


class BacklogRotationTest(unittest.TestCase):
    """AMENDMENT 1 F2 — three of many, rotated so the sample moves daily."""

    def setUp(self):
        self.conn = fx.open_db()
        fx.reset_ids()
        for index in range(10):
            fx.add_commitment(
                self.conn, commitment_id=f"cmt_backlog_{index:02d}",
                title=f"日付なしの作業 {index}", evidence="line",
            )
        self.addCleanup(self.conn.close)

    def test_same_date_yields_the_same_three_items(self):
        first = fx.build_plan(self.conn, date="2026-09-08")
        second = fx.build_plan(self.conn, date="2026-09-08")
        self.assertEqual(ids(first, "backlog"), ids(second, "backlog"))
        self.assertEqual(first["plan_hash"], second["plan_hash"])
        self.assertEqual(len(first["backlog"]), 3)
        self.assertEqual(first["counts"]["backlog"], {"shown": 3, "total": 10})

    def test_the_next_date_yields_the_next_three(self):
        day_one = ids(fx.build_plan(self.conn, date="2026-09-08"), "backlog")
        day_two = ids(
            fx.build_plan(
                self.conn, date="2026-09-09",
                now=fx.DEFAULT_NOW + timedelta(days=1),
            ),
            "backlog",
        )
        self.assertNotEqual(day_one, day_two)
        self.assertEqual(set(day_one) & set(day_two), set())

    def test_rotation_eventually_shows_every_item(self):
        seen = set()
        for offset in range(10):
            plan = fx.build_plan(
                self.conn,
                date=(fx.DEFAULT_NOW + timedelta(days=offset)).date().isoformat(),
                now=fx.DEFAULT_NOW + timedelta(days=offset),
            )
            seen.update(ids(plan, "backlog"))
        self.assertEqual(len(seen), 10)


class PriorityClassTest(unittest.TestCase):
    def setUp(self):
        self.conn = fx.open_db()
        fx.reset_ids()
        self.addCleanup(self.conn.close)

    def test_p0_only_for_an_imminent_fixed_schedule(self):
        imminent = fx.add_commitment(
            self.conn, title="1時間後の予定", work_state="SCHEDULED",
            scheduled_at="2026-09-08T09:30:00+09:00",
            scheduled_end_at="2026-09-08T10:30:00+09:00",
            authority="calendar_authority", evidence="calendar",
        )
        distant = fx.add_commitment(
            self.conn, title="夜の予定", work_state="SCHEDULED",
            scheduled_at="2026-09-08T21:00:00+09:00",
            scheduled_end_at="2026-09-08T22:00:00+09:00",
            authority="calendar_authority", evidence="calendar",
        )
        plan = fx.build_plan(self.conn)
        classes = {i["commitment_id"]: i["priority_class"] for i in plan["scheduled"]}
        self.assertEqual(classes[imminent], "P0")
        self.assertEqual(classes[distant], "P1")

    def test_an_all_day_marker_is_never_imminent(self):
        cid = fx.add_commitment(
            self.conn, title="終日の予定", work_state="SCHEDULED",
            scheduled_at="2026-09-08", scheduled_end_at="2026-09-09",
            authority="calendar_authority", evidence="calendar",
        )
        plan = fx.build_plan(self.conn)
        self.assertEqual(plan["scheduled"][0]["commitment_id"], cid)
        self.assertEqual(plan["scheduled"][0]["priority_class"], "P1")

    def test_must_do_next_up_waiting_and_backlog_classes(self):
        fx.add_commitment(
            self.conn, title="今日", due_at="2026-09-08T18:00:00+09:00",
            due_precision="minute", evidence="discord",
        )
        fx.add_commitment(
            self.conn, title="明日", due_at="2026-09-10T09:00:00+09:00",
            due_precision="minute", evidence="discord",
        )
        fx.add_commitment(
            self.conn, title="放置された確認", responsibility="COUNTERPARTY",
            work_state="WAITING", updated_at="2026-08-01T00:00:00+09:00",
            evidence="line",
        )
        fx.add_commitment(
            self.conn, title="最近の確認", responsibility="COUNTERPARTY",
            work_state="WAITING", updated_at="2026-09-07T00:00:00+09:00",
            evidence="line",
        )
        fx.add_commitment(self.conn, title="日付なし", evidence="line")
        plan = fx.build_plan(self.conn)
        self.assertEqual(plan["must_do"][0]["priority_class"], "P1")
        self.assertEqual(plan["next_up"][0]["priority_class"], "P2")
        classes = {i["reason_for_inclusion"]: i["priority_class"] for i in plan["waiting_for"]}
        self.assertEqual(classes["waiting_followup"], "P3")
        self.assertEqual(classes["waiting"], "P4")
        self.assertEqual(plan["backlog"][0]["priority_class"], "P4")

    def test_there_is_no_numeric_score_anywhere_in_the_plan(self):
        """Phase section 26: explainable classes, not a 0-100 score."""
        fx.seed_realistic_day(self.conn)
        plan = fx.build_plan(self.conn)
        for category in ("scheduled", "must_do", "overdue", "next_up", "waiting_for", "backlog"):
            for item in plan[category]:
                self.assertNotIn("priority_score", item)
                self.assertNotIn("score", item)
                self.assertRegex(item["priority_class"], r"^P[0-4]$")


class CapTest(unittest.TestCase):
    def setUp(self):
        self.conn = fx.open_db()
        fx.reset_ids()
        self.addCleanup(self.conn.close)

    def test_defaults_match_the_amendment(self):
        self.assertEqual(
            plan_config.DEFAULTS["limits"],
            {"scheduled": 5, "must_do": 5, "overdue": 5, "next_up": 5,
             "waiting_for": 7, "backlog": 3},
        )
        self.assertEqual(plan_config.DEFAULTS["morning_brief"]["time"], "08:30")
        self.assertEqual(plan_config.DEFAULTS["overdue_per_counterparty_cap"], 2)

    def test_limits_are_configurable_and_totals_survive(self):
        for hour in range(9, 15):
            fx.add_commitment(
                self.conn, title=f"締切 {hour}",
                due_at=f"2026-09-08T{hour:02d}:00:00+09:00", due_precision="minute",
                evidence="discord",
            )
        plan = fx.build_plan(self.conn, config={"limits": {"must_do": 2}})
        self.assertEqual(len(plan["must_do"]), 2)
        self.assertEqual(plan["counts"]["must_do"], {"shown": 2, "total": 6})

    def test_next_up_window_is_configurable(self):
        cid = fx.add_commitment(
            self.conn, title="来週の締切", due_at="2026-09-13T09:00:00+09:00",
            due_precision="minute", evidence="discord",
        )
        self.assertEqual(ids(fx.build_plan(self.conn), "next_up"), [])
        widened = fx.build_plan(self.conn, config={"windows": {"next_up_hours": 168}})
        self.assertEqual(ids(widened, "next_up"), [cid])
        # The vocabulary stays closed even when the window is not 72 hours.
        self.assertEqual(widened["next_up"][0]["reason_for_inclusion"], "due_within_72h")


if __name__ == "__main__":
    unittest.main()
