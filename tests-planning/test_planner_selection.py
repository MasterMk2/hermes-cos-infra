"""Selection rules of the deterministic daily planner (phase sections 79, 84, 85).

Covers, in order: the nine selection requirements of section 79, the WAITING
separation of section 84, the no-date backlog of section 85, the BLOCKED
mapping and the unknown-work_state counter of AMENDMENT 1 section D, and the
Gmail noise filter of AMENDMENT 1 section C — plus the schema-level invariants
the renderer and the MCP boundary build against: the exact Item keys, the
closed `reason_for_inclusion` vocabulary, and the one-category-per-item rule.
"""

import os
import sys
import unittest
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import planning_fixtures as fx  # noqa: E402

from planning.plan_models import (  # noqa: E402
    CATEGORY_PRECEDENCE,
    ITEM_KEY_ORDER,
    PLAN_REQUIRED_KEYS,
    REASON_DUE_TODAY,
    REASON_DUE_WITHIN_72H,
    REASON_NO_DATE_BACKLOG,
    REASON_SCHEDULED_TODAY,
    REASON_SHARED_NEEDS_REVIEW,
    REASON_WAITING,
    REASON_WAITING_FOLLOWUP,
    WARNING_UNKNOWN_WORK_STATE,
    is_valid_reason,
)

CATEGORY_NAMES = [c.value for c in CATEGORY_PRECEDENCE]


def ids(plan, category):
    return [item["commitment_id"] for item in plan[category]]


def all_ids(plan):
    out = []
    for category in CATEGORY_NAMES:
        out.extend(ids(plan, category))
    return out


def find(plan, commitment_id):
    """Return the (category, item) a commitment landed in, or (None, None)."""
    for category in CATEGORY_NAMES:
        for item in plan[category]:
            if item["commitment_id"] == commitment_id:
                return (category, item)
    return (None, None)


class SelectionTest(unittest.TestCase):
    def setUp(self):
        self.conn = fx.open_db()
        fx.reset_ids()
        self.addCleanup(self.conn.close)

    def plan(self, **kwargs):
        return fx.build_plan(self.conn, **kwargs)

    # -- section 79 ------------------------------------------------------

    def test_scheduled_today_selected(self):
        cid = fx.add_commitment(
            self.conn,
            title="定例ミーティング",
            work_state="SCHEDULED",
            scheduled_at="2026-09-08T00:00:00+00:00",
            scheduled_end_at="2026-09-08T01:00:00+00:00",
            authority="calendar_authority",
            evidence="calendar",
        )
        plan = self.plan()
        self.assertEqual(ids(plan, "scheduled"), [cid])
        self.assertEqual(plan["scheduled"][0]["reason_for_inclusion"], REASON_SCHEDULED_TODAY)
        # 09:00 JST, from the +00:00 calendar value — not 00:00 UTC.
        self.assertEqual(plan["scheduled"][0]["local_time"], "09:00")

    def test_self_due_today_selected(self):
        cid = fx.add_commitment(
            self.conn, due_at="2026-09-08T18:00:00+09:00", due_precision="minute",
            evidence="discord",
        )
        plan = self.plan()
        self.assertEqual(ids(plan, "must_do"), [cid])
        self.assertEqual(plan["must_do"][0]["reason_for_inclusion"], REASON_DUE_TODAY)

    def test_self_overdue_selected_with_day_count(self):
        cid = fx.add_commitment(
            self.conn, due_at="2026-09-01T09:00:00+09:00", due_precision="minute",
            evidence="discord",
        )
        plan = self.plan()
        self.assertEqual(ids(plan, "overdue"), [cid])
        item = plan["overdue"][0]
        self.assertEqual(item["overdue_days"], 7)
        self.assertEqual(item["reason_for_inclusion"], "overdue_7d")

    def test_due_soon_selected_and_not_duplicated(self):
        soon = fx.add_commitment(
            self.conn, title="近日の締切", due_at="2026-09-10T09:00:00+09:00",
            due_precision="minute", evidence="discord",
        )
        today = fx.add_commitment(
            self.conn, title="今日の締切", due_at="2026-09-08T09:00:00+09:00",
            due_precision="minute", evidence="discord",
        )
        plan = self.plan()
        self.assertEqual(ids(plan, "next_up"), [soon])
        self.assertEqual(ids(plan, "must_do"), [today])
        self.assertNotIn(soon, ids(plan, "must_do") + ids(plan, "overdue"))
        self.assertEqual(plan["next_up"][0]["reason_for_inclusion"], REASON_DUE_WITHIN_72H)

    def test_beyond_the_next_up_window_is_not_selected(self):
        fx.add_commitment(
            self.conn, due_at="2026-09-30T09:00:00+09:00", due_precision="minute",
            evidence="discord",
        )
        plan = self.plan()
        self.assertEqual(all_ids(plan), [])

    def test_waiting_is_separated_from_own_actions(self):
        cid = fx.add_commitment(
            self.conn, title="返答待ち", responsibility="COUNTERPARTY",
            work_state="WAITING", counterparty="担当C",
            due_at="2026-09-08T12:00:00+09:00", due_precision="minute",
            evidence="line",
        )
        plan = self.plan()
        self.assertEqual(ids(plan, "waiting_for"), [cid])
        self.assertEqual(ids(plan, "must_do"), [])
        self.assertEqual(ids(plan, "next_up"), [])
        self.assertEqual(ids(plan, "overdue"), [])

    def test_counterparty_actionable_is_not_my_task(self):
        fx.add_commitment(
            self.conn, responsibility="COUNTERPARTY", work_state="ACTIONABLE",
            due_at="2026-09-08T12:00:00+09:00", evidence="discord",
        )
        self.assertEqual(all_ids(self.plan()), [])

    def test_done_cancelled_dismissed_excluded(self):
        for state in ("DONE", "CANCELLED", "DISMISSED"):
            fx.add_commitment(
                self.conn, title=f"{state}の項目", work_state=state,
                due_at="2026-09-08T12:00:00+09:00", evidence="discord",
            )
        self.assertEqual(all_ids(self.plan()), [])

    def test_informational_and_superseded_excluded(self):
        fx.add_commitment(
            self.conn, responsibility="INFORMATIONAL", work_state="DISMISSED",
            due_at="2026-09-08T12:00:00+09:00", evidence="gmail",
        )
        fx.add_commitment(
            self.conn, currentness="superseded",
            due_at="2026-09-08T12:00:00+09:00", evidence="discord",
        )
        self.assertEqual(all_ids(self.plan()), [])

    # -- section 84 / 85 -------------------------------------------------

    def test_waiting_followup_after_the_idle_threshold(self):
        stale = fx.add_commitment(
            self.conn, title="長く止まっている確認", responsibility="COUNTERPARTY",
            work_state="WAITING", updated_at="2026-08-20T00:00:00+09:00",
            evidence="line",
        )
        recent = fx.add_commitment(
            self.conn, title="昨日依頼した確認", responsibility="COUNTERPARTY",
            work_state="WAITING", updated_at="2026-09-07T00:00:00+09:00",
            evidence="line",
        )
        plan = self.plan()
        reasons = {i["commitment_id"]: i["reason_for_inclusion"] for i in plan["waiting_for"]}
        self.assertEqual(reasons[stale], REASON_WAITING_FOLLOWUP)
        self.assertEqual(reasons[recent], REASON_WAITING)

    def test_no_date_backlog_never_occupies_top_priority(self):
        backlog = fx.add_commitment(self.conn, title="日付なしの作業", evidence="line")
        fx.add_commitment(
            self.conn, title="今日の締切", due_at="2026-09-08T18:00:00+09:00",
            due_precision="minute", evidence="discord",
        )
        plan = self.plan()
        self.assertEqual(ids(plan, "backlog"), [backlog])
        item = plan["backlog"][0]
        self.assertEqual(item["reason_for_inclusion"], REASON_NO_DATE_BACKLOG)
        self.assertEqual(item["priority_class"], "P4")
        # `backlog` is last in the contract's category precedence, so it can
        # never displace a dated item.
        self.assertEqual(CATEGORY_NAMES[-1], "backlog")

    # -- AMENDMENT 1 section D: BLOCKED and unknown states ---------------

    def test_blocked_maps_to_waiting_for_and_never_to_a_task(self):
        cid = fx.add_commitment(
            self.conn, title="前提待ちの作業", work_state="BLOCKED",
            due_at="2026-09-08T12:00:00+09:00", due_precision="minute",
            evidence="discord",
        )
        plan = self.plan()
        self.assertEqual(ids(plan, "waiting_for"), [cid])
        for category in ("must_do", "next_up", "overdue", "backlog", "scheduled"):
            self.assertNotIn(cid, ids(plan, category))

    def test_blocked_overdue_still_waits_rather_than_becoming_overdue(self):
        cid = fx.add_commitment(
            self.conn, work_state="BLOCKED", due_at="2026-08-01T12:00:00+09:00",
            evidence="discord",
        )
        plan = self.plan()
        self.assertEqual(ids(plan, "waiting_for"), [cid])
        self.assertEqual(ids(plan, "overdue"), [])

    def test_unknown_work_state_is_counted_and_warned_not_dropped_silently(self):
        conn = fx.open_db(allow_unknown_work_state=True)
        self.addCleanup(conn.close)
        fx.add_commitment(
            conn, work_state="DEFERRED_PENDING_REVIEW",
            due_at="2026-09-08T12:00:00+09:00", evidence="discord",
        )
        with self.assertLogs("hermes.planning.planner", level="WARNING") as captured:
            plan = fx.build_plan(conn)
        self.assertEqual(plan["counts"]["unknown_work_state"]["total"], 1)
        self.assertIn(WARNING_UNKNOWN_WORK_STATE, plan["warnings"])
        self.assertTrue(any("DEFERRED_PENDING_REVIEW" in line for line in captured.output))
        self.assertEqual(all_ids(plan), [])

    # -- AMENDMENT 1 section C: the noise filter -------------------------

    def test_shared_gmail_only_is_filtered_and_only_counted(self):
        cid = fx.add_commitment(
            self.conn, title="【本日限定】キャンペーンのお知らせ",
            responsibility="SHARED", due_at="2026-09-08T17:00:00+09:00",
            due_precision="day", confidence=0.7, evidence="gmail",
        )
        plan = self.plan()
        self.assertEqual(all_ids(plan), [])
        self.assertEqual(plan["counts"]["filtered_noise"], {"shown": 0, "total": 1})
        self.assertNotIn(cid, str(plan[c] for c in CATEGORY_NAMES))

    def test_shared_with_non_gmail_evidence_and_a_deadline_is_actionable(self):
        cid = fx.add_commitment(
            self.conn, title="共同作業の締切", responsibility="SHARED",
            due_at="2026-09-08T18:00:00+09:00", due_precision="minute",
            evidence="discord",
        )
        plan = self.plan()
        self.assertEqual(ids(plan, "must_do"), [cid])
        self.assertEqual(plan["counts"]["filtered_noise"]["total"], 0)

    def test_shared_without_a_deadline_needs_review_rather_than_a_task(self):
        cid = fx.add_commitment(
            self.conn, title="担当が曖昧な依頼", responsibility="SHARED",
            evidence="discord",
        )
        plan = self.plan()
        self.assertEqual(ids(plan, "waiting_for"), [cid])
        self.assertEqual(
            plan["waiting_for"][0]["reason_for_inclusion"], REASON_SHARED_NEEDS_REVIEW
        )
        self.assertEqual(ids(plan, "backlog"), [])

    def test_shared_calendar_event_still_reaches_scheduled(self):
        """Coordinator ruling on AMENDMENT 1 C: a SHARED calendar slot is a
        real fixture of the day even though it is SCHEDULED with a NULL due_at.
        Phase section 20 makes Calendar the scheduling authority, and omitting
        an event outright is worse than any question about its ownership."""
        cid = fx.add_commitment(
            self.conn, title="全社研修", responsibility="SHARED",
            work_state="SCHEDULED", scheduled_at="2026-09-08",
            scheduled_end_at="2026-09-09", authority="calendar_authority",
            confidence=1.0, evidence="calendar",
        )
        plan = self.plan()
        self.assertEqual(ids(plan, "scheduled"), [cid])
        self.assertTrue(plan["scheduled"][0]["all_day"])
        self.assertIsNone(plan["scheduled"][0]["local_time"])

    def test_shared_scheduled_with_no_due_at_is_not_dropped(self):
        """The literal first draft of AMENDMENT 1 C required ACTIONABLE and a
        non-NULL due_at for every action category, which would have dropped all
        57 production SHARED SCHEDULED calendar rows. This pins the ruling so a
        refactor cannot silently reinstate it."""
        cid = fx.add_commitment(
            self.conn, title="時刻ありの共同予定", responsibility="SHARED",
            work_state="SCHEDULED", due_at=None,
            scheduled_at="2026-09-08T05:00:00+00:00",
            scheduled_end_at="2026-09-08T06:00:00+00:00",
            authority="calendar_authority", confidence=1.0, evidence="calendar",
        )
        plan = self.plan()
        self.assertEqual(ids(plan, "scheduled"), [cid])
        item = plan["scheduled"][0]
        self.assertIsNone(item["due_at"])
        self.assertEqual(item["work_state"], "SCHEDULED")
        self.assertEqual(item["responsibility"], "SHARED")
        self.assertEqual(item["local_time"], "14:00")
        self.assertEqual(plan["counts"]["filtered_noise"]["total"], 0)

    def test_shared_gmail_only_scheduled_row_is_still_filtered(self):
        fx.add_commitment(
            self.conn, title="メール由来の予定らしきもの", responsibility="SHARED",
            work_state="SCHEDULED", scheduled_at="2026-09-08T00:00:00+00:00",
            scheduled_end_at="2026-09-08T01:00:00+00:00", evidence="gmail",
        )
        plan = self.plan()
        self.assertEqual(all_ids(plan), [])
        self.assertEqual(plan["counts"]["filtered_noise"]["total"], 1)

    # -- schema invariants ------------------------------------------------

    def test_every_item_appears_in_exactly_one_category(self):
        fx.seed_realistic_day(self.conn)
        plan = self.plan()
        seen = all_ids(plan)
        self.assertEqual(len(seen), len(set(seen)))

    def test_item_keys_are_exactly_the_contract_shape(self):
        fx.seed_realistic_day(self.conn)
        plan = self.plan()
        expected = [k for k in ITEM_KEY_ORDER if k != "overdue_days"]
        for category in CATEGORY_NAMES:
            for item in plan[category]:
                keys = list(item.keys())
                if category == "overdue":
                    self.assertEqual(keys, list(ITEM_KEY_ORDER))
                else:
                    self.assertEqual(keys, expected)

    def test_reason_vocabulary_is_closed(self):
        fx.seed_realistic_day(self.conn)
        plan = self.plan()
        found = set()
        for category in CATEGORY_NAMES:
            for item in plan[category]:
                self.assertTrue(
                    is_valid_reason(item["reason_for_inclusion"]),
                    f"reason outside the closed vocabulary: {item['reason_for_inclusion']}",
                )
                found.add(item["reason_for_inclusion"])
        self.assertFalse(found - {
            REASON_SCHEDULED_TODAY, REASON_DUE_TODAY, REASON_DUE_WITHIN_72H,
            REASON_WAITING, REASON_WAITING_FOLLOWUP, REASON_NO_DATE_BACKLOG,
            REASON_SHARED_NEEDS_REVIEW,
        } - {r for r in found if r.startswith("overdue_")})

    def test_reason_validator_rejects_anything_else(self):
        for bad in ("due_tomorrow", "overdue", "overdue_d", "overdue_-1d", "", None, "URGENT"):
            self.assertFalse(is_valid_reason(bad), bad)
        for good in ("overdue_0d", "overdue_1d", "overdue_42d", REASON_DUE_TODAY):
            self.assertTrue(is_valid_reason(good), good)

    def test_plan_top_level_keys_and_absent_warnings(self):
        fx.add_commitment(self.conn, evidence="discord")
        plan = self.plan()
        for key in PLAN_REQUIRED_KEYS:
            self.assertIn(key, plan)
        self.assertNotIn("warnings", plan, "warnings must be omitted entirely when empty")
        self.assertEqual(plan["date"], fx.DEFAULT_PLAN_DATE)
        self.assertEqual(plan["timezone"], "Asia/Tokyo")

    def test_counts_total_is_before_the_cap(self):
        for hour in range(9, 9 + 8):
            fx.add_commitment(
                self.conn, title=f"締切 {hour}時",
                due_at=f"2026-09-08T{hour:02d}:00:00+09:00", due_precision="minute",
                evidence="discord",
            )
        plan = self.plan()
        self.assertEqual(plan["counts"]["must_do"]["total"], 8)
        self.assertEqual(plan["counts"]["must_do"]["shown"], 5)
        self.assertEqual(len(plan["must_do"]), 5)

    def test_titles_are_never_rewritten(self):
        raw = "  【重要】 資料をレビューする  "
        cid = fx.add_commitment(
            self.conn, title=raw, due_at="2026-09-08T18:00:00+09:00", evidence="discord",
        )
        plan = self.plan()
        _category, item = find(plan, cid)
        self.assertEqual(item["title"], raw)

    def test_plan_hash_ignores_generated_at_and_freshness(self):
        fx.seed_realistic_day(self.conn)
        base = self.plan()
        later = self.plan(now=fx.DEFAULT_NOW.replace(hour=9, minute=15))
        aged = self.plan(freshness=fx.freshness_block(fx.DEFAULT_NOW - timedelta(hours=2)))
        self.assertEqual(base["plan_hash"], later["plan_hash"])
        self.assertNotEqual(base["source_freshness"], aged["source_freshness"])
        self.assertEqual(base["plan_hash"], aged["plan_hash"])
        self.assertNotEqual(base["generated_at"], later["generated_at"])

    def test_plan_hash_follows_the_warning_because_the_brief_text_changes(self):
        """`warnings` stays inside the hashed body: the contract names exactly
        three exclusions, and a freshness warning is content the reader sees."""
        fx.seed_realistic_day(self.conn)
        base = self.plan()
        warned = self.plan(freshness=fx.freshness_block(stale_sources=("line",)))
        self.assertNotIn("warnings", base)
        self.assertEqual(warned["warnings"], ["data_freshness_warning"])
        self.assertNotEqual(base["plan_hash"], warned["plan_hash"])

    def test_plan_hash_changes_when_a_fact_changes(self):
        fx.seed_realistic_day(self.conn)
        before = self.plan()
        fx.add_commitment(
            self.conn, title="追加された締切", due_at="2026-09-08T20:00:00+09:00",
            due_precision="minute", evidence="discord",
        )
        self.assertNotEqual(before["plan_hash"], self.plan()["plan_hash"])

    def test_planner_writes_nothing(self):
        fx.seed_realistic_day(self.conn)
        before = self.conn.execute("SELECT COUNT(*) FROM commitments").fetchone()[0]
        self.plan()
        after = self.conn.execute("SELECT COUNT(*) FROM commitments").fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM commitment_history").fetchone()[0], 0
        )

    def test_empty_database_produces_a_well_defined_empty_plan(self):
        plan = self.plan()
        for category in CATEGORY_NAMES:
            self.assertEqual(plan[category], [])
            self.assertEqual(plan["counts"][category], {"shown": 0, "total": 0})
        self.assertEqual(plan["conflicts"], [])
        self.assertNotIn("warnings", plan)

    def test_fixtures_refuse_a_production_path(self):
        with self.assertRaises(fx.ProductionAccessError):
            fx.open_db("/srv/hermes/hermes-data/events/events.db")


class AuditDayTest(unittest.TestCase):
    """2026-09-12, the day I2-PROFILE.md section N picks for the section 92
    read-only production audit. It is the only forward-looking date with more
    than one scheduled item, so it is the only one that exercises multi-item
    `scheduled` ordering at all — and it only has two because a SHARED calendar
    event counts. These numbers are the ones reported back to the coordinator.
    """

    def setUp(self):
        self.conn = fx.open_db()
        self.ids = fx.seed_audit_day(self.conn)
        self.plan = fx.build_plan(self.conn, date=fx.AUDIT_DATE, now=fx.AUDIT_NOW)
        self.addCleanup(self.conn.close)

    def test_the_audit_day_shows_two_scheduled_items(self):
        self.assertEqual(self.plan["counts"]["scheduled"], {"shown": 2, "total": 2})
        self.assertEqual(
            ids(self.plan, "scheduled"),
            [self.ids["all_day_shared"], self.ids["timed_self"]],
        )

    def test_both_shapes_render_correctly(self):
        all_day, timed = self.plan["scheduled"]
        self.assertTrue(all_day["all_day"])
        self.assertIsNone(all_day["local_time"])
        self.assertEqual(all_day["responsibility"], "SHARED")
        self.assertFalse(timed["all_day"])
        self.assertEqual(timed["local_time"], "22:00")
        self.assertEqual(timed["responsibility"], "SELF")

    def test_the_daypart_deadline_is_a_must_do_with_no_faked_conflict(self):
        self.assertEqual(ids(self.plan, "must_do"), [self.ids["daypart_deadline"]])
        self.assertEqual(self.plan["must_do"][0]["due_precision"], "daypart")
        # An all-day marker is not an occupied window, and a `daypart` clock
        # time is synthetic — neither may produce a conflict claim.
        self.assertEqual(self.plan["conflicts"], [])
        self.assertEqual(self.plan["counts"]["conflicts"], {"shown": 0, "total": 0})

    def test_the_audit_day_is_stable(self):
        again = fx.build_plan(self.conn, date=fx.AUDIT_DATE, now=fx.AUDIT_NOW)
        self.assertEqual(again["plan_hash"], self.plan["plan_hash"])


class LlmIndependenceTest(unittest.TestCase):
    """Phase section 75: the planner must work with no model available."""

    def test_planner_module_imports_no_provider_code(self):
        import planning.planner as planner_module

        source = open(planner_module.__file__, encoding="utf-8").read()
        for forbidden in ("openai", "anthropic", "gemini", "antigravity", "llm_client", "requests"):
            self.assertNotIn(
                f"import {forbidden}", source, f"planner.py must not import {forbidden}"
            )
        loaded = set(sys.modules)
        for forbidden in ("openai", "anthropic", "google.generativeai"):
            self.assertNotIn(forbidden, loaded)


if __name__ == "__main__":
    unittest.main()
