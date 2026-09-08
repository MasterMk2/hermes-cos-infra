"""Timezone and date-boundary correctness (phase sections 54, 83).

This is a named completion gate, and it is the failure mode that would be least
visible in review: every timestamp in the database already carries an offset,
so nothing crashes when the arithmetic is done in the wrong zone — the plan
just quietly puts an item on the wrong day.

The database makes that easy to get wrong. The same column carries `+09:00` on
Discord/LINE/Gmail rows, `+00:00` on Calendar-derived rows, and a bare
`YYYY-MM-DD` on all-day events, so there are three parsing paths and each one
has its own boundary.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import planning_fixtures as fx  # noqa: E402

from planning.planner import parse_stored  # noqa: E402

JST = ZoneInfo("Asia/Tokyo")


def ids(plan, category):
    return [item["commitment_id"] for item in plan[category]]


class UtcDateConfusionTest(unittest.TestCase):
    """The specific confusion phase section 83 names: a UTC date is not a JST date."""

    def setUp(self):
        self.conn = fx.open_db()
        fx.reset_ids()
        self.addCleanup(self.conn.close)

    def test_1530z_belongs_to_the_next_jst_day(self):
        """2026-09-08T15:30:00Z is 2026-09-09 00:30 JST. A planner that reads
        the UTC date would put it on 09-08."""
        cid = fx.add_commitment(
            self.conn, title="UTC 15:30 の締切",
            due_at="2026-09-08T15:30:00+00:00", due_precision="minute",
            evidence="discord",
        )
        kind, parsed = parse_stored("2026-09-08T15:30:00+00:00", JST)
        self.assertEqual(kind, "dt")
        self.assertEqual(parsed.date().isoformat(), "2026-09-09")
        self.assertEqual(parsed.strftime("%H:%M"), "00:30")

        on_the_eighth = fx.build_plan(self.conn, date="2026-09-08")
        self.assertEqual(ids(on_the_eighth, "must_do"), [])
        self.assertEqual(ids(on_the_eighth, "next_up"), [cid])

        on_the_ninth = fx.build_plan(
            self.conn, date="2026-09-09", now=fx.DEFAULT_NOW + timedelta(days=1)
        )
        self.assertEqual(ids(on_the_ninth, "must_do"), [cid])
        self.assertEqual(on_the_ninth["must_do"][0]["local_time"], "00:30")

    def test_the_literal_z_suffix_parses_identically(self):
        cid = fx.add_commitment(
            self.conn, title="Z 表記の締切", due_at="2026-09-08T15:30:00Z",
            due_precision="minute", evidence="discord",
        )
        plan = fx.build_plan(self.conn, date="2026-09-09", now=fx.DEFAULT_NOW + timedelta(days=1))
        self.assertEqual(ids(plan, "must_do"), [cid])

    def test_a_calendar_row_lands_on_its_jst_day(self):
        cid = fx.add_commitment(
            self.conn, title="UTC 深夜の予定", work_state="SCHEDULED",
            scheduled_at="2026-09-08T15:30:00+00:00",
            scheduled_end_at="2026-09-08T16:30:00+00:00",
            authority="calendar_authority", evidence="calendar",
        )
        self.assertEqual(ids(fx.build_plan(self.conn, date="2026-09-08"), "scheduled"), [])
        ninth = fx.build_plan(
            self.conn, date="2026-09-09", now=fx.DEFAULT_NOW + timedelta(days=1)
        )
        self.assertEqual(ids(ninth, "scheduled"), [cid])
        self.assertEqual(ninth["scheduled"][0]["local_time"], "00:30")


class DayBoundaryTest(unittest.TestCase):
    """23:59 JST and 00:01 JST, on both sides."""

    def setUp(self):
        self.conn = fx.open_db()
        fx.reset_ids()
        self.addCleanup(self.conn.close)

    def _due(self, cid, value, precision="minute"):
        return fx.add_commitment(
            self.conn, commitment_id=cid, title=f"{cid} の締切",
            due_at=value, due_precision=precision, evidence="discord",
        )

    def test_2359_today_is_due_today(self):
        cid = self._due("cmt_2359", "2026-09-08T23:59:59+09:00")
        plan = fx.build_plan(self.conn)
        self.assertEqual(ids(plan, "must_do"), [cid])
        self.assertEqual(plan["must_do"][0]["local_time"], "23:59")

    def test_0000_today_is_due_today_not_overdue(self):
        cid = self._due("cmt_0000", "2026-09-08T00:00:00+09:00")
        plan = fx.build_plan(self.conn)
        self.assertEqual(ids(plan, "must_do"), [cid])
        self.assertEqual(ids(plan, "overdue"), [])

    def test_2359_yesterday_is_overdue_by_one_day(self):
        cid = self._due("cmt_prev", "2026-09-07T23:59:59+09:00")
        plan = fx.build_plan(self.conn)
        self.assertEqual(ids(plan, "overdue"), [cid])
        self.assertEqual(plan["overdue"][0]["overdue_days"], 1)

    def test_0001_tomorrow_is_not_due_today(self):
        cid = self._due("cmt_next", "2026-09-09T00:01:00+09:00")
        plan = fx.build_plan(self.conn)
        self.assertEqual(ids(plan, "must_do"), [])
        self.assertEqual(ids(plan, "next_up"), [cid])

    def test_the_plan_date_at_2359_jst_is_still_that_day(self):
        self._due("cmt_today", "2026-09-08T20:00:00+09:00")
        plan = fx.build_plan(
            self.conn, date=None, now=datetime(2026, 9, 8, 23, 59, 59, tzinfo=JST)
        )
        self.assertEqual(plan["date"], "2026-09-08")
        self.assertEqual(ids(plan, "must_do"), ["cmt_today"])

    def test_the_plan_date_at_0001_jst_is_the_new_day(self):
        self._due("cmt_today", "2026-09-08T20:00:00+09:00")
        plan = fx.build_plan(
            self.conn, date=None, now=datetime(2026, 9, 9, 0, 1, 0, tzinfo=JST)
        )
        self.assertEqual(plan["date"], "2026-09-09")
        self.assertEqual(ids(plan, "must_do"), [])
        self.assertEqual(ids(plan, "overdue"), ["cmt_today"])

    def test_a_naive_now_is_read_as_utc(self):
        """Defensive: nothing in the DB is naive, but an injected clock may be."""
        self._due("cmt_today", "2026-09-08T20:00:00+09:00")
        plan = fx.build_plan(
            self.conn, date=None, now=datetime(2026, 9, 8, 15, 30, 0)  # 09-09 00:30 JST
        )
        self.assertEqual(plan["date"], "2026-09-09")

    def test_a_utc_now_is_converted_before_the_day_is_chosen(self):
        self._due("cmt_today", "2026-09-08T20:00:00+09:00")
        plan = fx.build_plan(
            self.conn, date=None,
            now=datetime(2026, 9, 7, 23, 30, 0, tzinfo=timezone.utc),  # 09-08 08:30 JST
        )
        self.assertEqual(plan["date"], "2026-09-08")
        self.assertEqual(ids(plan, "must_do"), ["cmt_today"])


class DateOnlyValueTest(unittest.TestCase):
    """The 36 bare `YYYY-MM-DD` values: JST local dates, end-exclusive."""

    def setUp(self):
        self.conn = fx.open_db()
        fx.reset_ids()
        self.addCleanup(self.conn.close)

    def test_a_date_only_value_is_a_local_date_not_midnight_utc(self):
        kind, parsed = parse_stored("2026-09-12", JST)
        self.assertEqual(kind, "date")
        self.assertEqual(parsed.isoformat(), "2026-09-12T00:00:00+09:00")
        self.assertEqual(parsed.utcoffset(), timedelta(hours=9))

    def test_a_one_day_all_day_event_covers_only_its_own_day(self):
        fx.add_commitment(
            self.conn, commitment_id="cmt_allday", title="終日の予定",
            work_state="SCHEDULED", scheduled_at="2026-09-08",
            scheduled_end_at="2026-09-09", authority="calendar_authority",
            evidence="calendar",
        )
        eighth = fx.build_plan(self.conn, date="2026-09-08")
        ninth = fx.build_plan(
            self.conn, date="2026-09-09", now=fx.DEFAULT_NOW + timedelta(days=1)
        )
        self.assertEqual(ids(eighth, "scheduled"), ["cmt_allday"])
        self.assertEqual(ids(ninth, "scheduled"), [], "the end date is exclusive")

    def test_a_multi_day_all_day_event_covers_every_day_it_spans(self):
        fx.add_commitment(
            self.conn, commitment_id="cmt_camp", title="合宿", work_state="SCHEDULED",
            scheduled_at="2026-09-08", scheduled_end_at="2026-09-11",
            authority="calendar_authority", evidence="calendar",
        )
        covered = []
        for offset in range(4):
            day = (fx.DEFAULT_NOW + timedelta(days=offset)).date().isoformat()
            plan = fx.build_plan(
                self.conn, date=day, now=fx.DEFAULT_NOW + timedelta(days=offset)
            )
            if ids(plan, "scheduled"):
                covered.append(day)
        self.assertEqual(covered, ["2026-09-08", "2026-09-09", "2026-09-10"])

    def test_an_all_day_item_never_shows_a_clock_time(self):
        fx.add_commitment(
            self.conn, title="終日の予定", work_state="SCHEDULED",
            scheduled_at="2026-09-08", scheduled_end_at="2026-09-09",
            authority="calendar_authority", evidence="calendar",
        )
        item = fx.build_plan(self.conn)["scheduled"][0]
        self.assertTrue(item["all_day"])
        self.assertIsNone(item["local_time"])

    def test_a_coarse_due_precision_publishes_no_clock_time(self):
        """AMENDMENT 1 B4 applied at the source, not only in the renderer.

        `week` / `daypart` / `approximate` rows carry a *synthetic* 23:59:00.
        The renderer always refused to display it; `daily_plan_get` published
        it as `local_time` to an MCP consumer that has no renderer in front of
        it, which is a deadline the database does not contain.
        """
        for precision in ("week", "daypart", "approximate", "day", "month",
                          "unknown", None):
            with self.subTest(due_precision=precision):
                conn = fx.open_db()
                self.addCleanup(conn.close)
                fx.reset_ids()
                fx.add_commitment(
                    conn, title="今週中に学務へ書類を出す",
                    due_at="2026-09-08T23:59:00+09:00", due_precision=precision,
                    evidence="discord",
                )
                item = fx.build_plan(conn)["must_do"][0]
                self.assertIsNone(item["local_time"])
                # Canonical values are untouched: only the derived field goes.
                self.assertEqual(item["due_at"], "2026-09-08T23:59:00+09:00")
                self.assertEqual(item["due_precision"], precision)

    def test_a_date_only_deadline_is_the_end_of_that_local_day(self):
        cid = fx.add_commitment(
            self.conn, title="日付だけの締切", due_at="2026-09-08",
            due_precision="date", evidence="discord",
        )
        plan = fx.build_plan(self.conn)
        self.assertEqual(ids(plan, "must_do"), [cid])
        # No clock time exists in the data, so none is displayed.
        self.assertIsNone(plan["must_do"][0]["local_time"])
        self.assertEqual(plan["must_do"][0]["due_at"], "2026-09-08")

    def test_a_date_only_deadline_yesterday_is_overdue_by_one_day(self):
        fx.add_commitment(
            self.conn, commitment_id="cmt_yesterday", title="昨日締切",
            due_at="2026-09-07", due_precision="date", evidence="discord",
        )
        plan = fx.build_plan(self.conn)
        self.assertEqual(ids(plan, "overdue"), ["cmt_yesterday"])
        self.assertEqual(plan["overdue"][0]["overdue_days"], 1)


class ExplicitTimezoneTest(unittest.TestCase):
    """The zone is a parameter, not a hard-coded +09:00 (phase section 54)."""

    def setUp(self):
        self.conn = fx.open_db()
        fx.reset_ids()
        self.addCleanup(self.conn.close)

    def test_the_same_row_changes_day_with_the_requested_zone(self):
        cid = fx.add_commitment(
            self.conn, title="UTC 15:30 の締切", due_at="2026-09-08T15:30:00+00:00",
            due_precision="minute", evidence="discord",
        )
        in_utc = fx.build_plan(
            self.conn, date="2026-09-08", timezone="UTC",
            now=datetime(2026, 9, 8, 8, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(in_utc["timezone"], "UTC")
        self.assertEqual(ids(in_utc, "must_do"), [cid])
        self.assertEqual(in_utc["must_do"][0]["local_time"], "15:30")

        in_jst = fx.build_plan(self.conn, date="2026-09-08")
        self.assertEqual(ids(in_jst, "must_do"), [])

    def test_generated_at_carries_the_plan_zone(self):
        plan = fx.build_plan(self.conn)
        self.assertTrue(plan["generated_at"].endswith("+09:00"))
        self.assertEqual(plan["timezone"], "Asia/Tokyo")

    def test_stored_timestamps_are_never_rewritten_in_the_output(self):
        raw = "2026-09-08T18:00:00+09:00"
        fx.add_commitment(
            self.conn, title="そのまま返る締切", due_at=raw, due_precision="minute",
            evidence="discord",
        )
        self.assertEqual(fx.build_plan(self.conn)["must_do"][0]["due_at"], raw)

    def test_planner_does_not_hard_code_a_nine_hour_offset(self):
        import planning.planner as planner_module

        source = open(planner_module.__file__, encoding="utf-8").read()
        self.assertNotIn("timedelta(hours=9)", source)
        self.assertIn("ZoneInfo", source)


if __name__ == "__main__":
    unittest.main()
