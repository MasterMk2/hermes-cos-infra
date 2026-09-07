"""Deduplication and Calendar authority (phase sections 20, 21, 80).

Production carries **no** calendar-versus-commitment duplication: zero
commitments hold both calendar and non-calendar evidence, and the two ingest
paths never touch. So rules 1 and 2 below are pinned against the shapes the
profiling actually found (one shared-`source_id` collision, two identical
calendar windows), and rule 3 — the Calendar-authority collapse phase section
21 warns about — is driven entirely by synthetic fixtures, because there is
nothing in production to drive it with.

The negative test matters as much as the positive ones. A `SequenceMatcher`
sweep at r >= 0.75 produces 268 false-positive pairs from newsletter families
while both real duplicates score *below* that threshold, so title similarity is
strictly worse than useless here and must never creep back in.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import planning_fixtures as fx  # noqa: E402


def ids(plan, category):
    return [item["commitment_id"] for item in plan[category]]


def all_ids(plan):
    out = []
    for category in ("scheduled", "must_do", "overdue", "next_up", "waiting_for", "backlog"):
        out.extend(ids(plan, category))
    return out


class SharedSourceIdRuleTest(unittest.TestCase):
    """Rule 1 — two commitments sharing a `(source_type, source_id)` pair."""

    def setUp(self):
        self.conn = fx.open_db()
        fx.reset_ids()
        self.addCleanup(self.conn.close)

    def test_shared_evidence_collapses_to_one_display_item(self):
        shared = ("discord", "discord:1234:5678:9012")
        fx.add_commitment(
            self.conn, commitment_id="cmt_aaa", title="同じ依頼（片方）",
            due_at="2026-09-08T18:00:00+09:00", due_precision="minute",
            evidence=[shared],
        )
        fx.add_commitment(
            self.conn, commitment_id="cmt_bbb", title="同じ依頼（もう片方）",
            due_at="2026-09-08T18:00:00+09:00", due_precision="minute",
            evidence=[shared, ("line", "line:999")],
        )
        plan = fx.build_plan(self.conn)
        self.assertEqual(len(all_ids(plan)), 1)
        self.assertEqual(plan["counts"]["must_do"], {"shown": 1, "total": 1})

    def test_survivor_prefers_calendar_authority(self):
        shared = ("calendar", "calendar:event-1")
        fx.add_commitment(
            self.conn, commitment_id="cmt_zzz", title="カレンダー側",
            work_state="SCHEDULED", scheduled_at="2026-09-08T00:00:00+00:00",
            scheduled_end_at="2026-09-08T01:00:00+00:00",
            authority="calendar_authority", evidence=[shared],
        )
        fx.add_commitment(
            self.conn, commitment_id="cmt_aaa", title="証跡側",
            work_state="SCHEDULED", scheduled_at="2026-09-08T00:00:00+00:00",
            scheduled_end_at="2026-09-08T01:00:00+00:00",
            authority="evidence", evidence=[shared],
        )
        self.assertEqual(ids(fx.build_plan(self.conn), "scheduled"), ["cmt_zzz"])

    def test_survivor_falls_back_to_the_higher_source_count(self):
        shared = ("discord", "discord:same")
        fx.add_commitment(
            self.conn, commitment_id="cmt_zzz", title="根拠が多い方",
            due_at="2026-09-08T18:00:00+09:00", source_count=2,
            evidence=[shared, ("line", "line:extra")],
        )
        fx.add_commitment(
            self.conn, commitment_id="cmt_aaa", title="根拠が少ない方",
            due_at="2026-09-08T18:00:00+09:00", source_count=1, evidence=[shared],
        )
        self.assertEqual(ids(fx.build_plan(self.conn), "must_do"), ["cmt_zzz"])

    def test_survivor_falls_back_to_the_smaller_commitment_id(self):
        shared = ("discord", "discord:same")
        for cid in ("cmt_zzz", "cmt_aaa"):
            fx.add_commitment(
                self.conn, commitment_id=cid, title=f"{cid} の表現",
                due_at="2026-09-08T18:00:00+09:00", source_count=1, evidence=[shared],
            )
        self.assertEqual(ids(fx.build_plan(self.conn), "must_do"), ["cmt_aaa"])

    def test_source_types_are_merged_onto_the_survivor(self):
        shared = ("discord", "discord:same")
        fx.add_commitment(
            self.conn, commitment_id="cmt_aaa", title="Discord のみ",
            due_at="2026-09-08T18:00:00+09:00", source_count=1, evidence=[shared],
        )
        fx.add_commitment(
            self.conn, commitment_id="cmt_bbb", title="Discord と LINE",
            due_at="2026-09-08T18:00:00+09:00", source_count=1,
            evidence=[shared, ("line", "line:other")],
        )
        plan = fx.build_plan(self.conn)
        self.assertEqual(plan["must_do"][0]["source_types"], ["discord", "line"])

    def test_different_source_ids_are_not_collapsed(self):
        fx.add_commitment(
            self.conn, commitment_id="cmt_aaa", title="別件A",
            due_at="2026-09-08T18:00:00+09:00", evidence=[("discord", "discord:1")],
        )
        fx.add_commitment(
            self.conn, commitment_id="cmt_bbb", title="別件B",
            due_at="2026-09-08T18:00:00+09:00", evidence=[("discord", "discord:2")],
        )
        self.assertEqual(len(fx.build_plan(self.conn)["must_do"]), 2)


class IdenticalWindowRuleTest(unittest.TestCase):
    """Rule 2 — the same calendar window described by two calendar events."""

    def setUp(self):
        self.conn = fx.open_db()
        fx.reset_ids()
        self.addCleanup(self.conn.close)

    def _pair(self, **overrides):
        window = {
            "work_state": "SCHEDULED",
            "responsibility": "SHARED",
            "scheduled_at": "2026-09-08T00:00:00+00:00",
            "scheduled_end_at": "2026-09-08T02:00:00+00:00",
            "authority": "calendar_authority",
            "confidence": 1.0,
        }
        window.update(overrides)
        return window

    def test_identical_window_collapses_keeping_the_longer_title(self):
        fx.add_commitment(
            self.conn, commitment_id="cmt_aaa", title="第2回 クラスアワー",
            evidence=[("calendar", "calendar:a")], **self._pair(),
        )
        fx.add_commitment(
            self.conn, commitment_id="cmt_bbb", title="【第2回】クラスアワー（資料配布）",
            evidence=[("calendar", "calendar:b")], **self._pair(),
        )
        plan = fx.build_plan(self.conn)
        self.assertEqual(ids(plan, "scheduled"), ["cmt_bbb"])
        self.assertEqual(plan["scheduled"][0]["title"], "【第2回】クラスアワー（資料配布）")

    def test_equal_titles_break_on_the_more_recent_updated_at(self):
        fx.add_commitment(
            self.conn, commitment_id="cmt_aaa", title="同じ長さの題名",
            updated_at="2026-09-01T00:00:00+00:00",
            evidence=[("calendar", "calendar:a")], **self._pair(),
        )
        fx.add_commitment(
            self.conn, commitment_id="cmt_bbb", title="同じ長さの題名",
            updated_at="2026-09-07T00:00:00+00:00",
            evidence=[("calendar", "calendar:b")], **self._pair(),
        )
        self.assertEqual(ids(fx.build_plan(self.conn), "scheduled"), ["cmt_bbb"])

    def test_a_different_window_is_not_collapsed(self):
        fx.add_commitment(
            self.conn, commitment_id="cmt_aaa", title="午前の回",
            evidence=[("calendar", "calendar:a")], **self._pair(),
        )
        fx.add_commitment(
            self.conn, commitment_id="cmt_bbb", title="午後の回",
            evidence=[("calendar", "calendar:b")],
            **self._pair(
                scheduled_at="2026-09-08T05:00:00+00:00",
                scheduled_end_at="2026-09-08T07:00:00+00:00",
            ),
        )
        self.assertEqual(len(fx.build_plan(self.conn)["scheduled"]), 2)

    def test_identical_window_without_calendar_authority_is_not_collapsed(self):
        """Two people genuinely booked into the same slot is a conflict, not a
        duplicate. Rule 2 fires only when both rows are calendar-authoritative."""
        fx.add_commitment(
            self.conn, commitment_id="cmt_aaa", title="証跡由来A",
            evidence="discord", **self._pair(authority="evidence"),
        )
        fx.add_commitment(
            self.conn, commitment_id="cmt_bbb", title="証跡由来B",
            evidence="discord", **self._pair(authority="evidence"),
        )
        self.assertEqual(len(fx.build_plan(self.conn)["scheduled"]), 2)

    def test_dedup_runs_before_conflict_detection(self):
        """Without this order the two real duplicate pairs in production are
        the only 'overlaps' in the whole dataset — a phantom conflict."""
        fx.add_commitment(
            self.conn, commitment_id="cmt_aaa", title="重複した予定",
            evidence=[("calendar", "calendar:a")], **self._pair(),
        )
        fx.add_commitment(
            self.conn, commitment_id="cmt_bbb", title="重複した予定（詳細つき）",
            evidence=[("calendar", "calendar:b")], **self._pair(),
        )
        plan = fx.build_plan(self.conn)
        self.assertEqual(len(plan["scheduled"]), 1)
        self.assertEqual(plan["conflicts"], [])
        self.assertEqual(plan["counts"]["conflicts"], {"shown": 0, "total": 0})


class CalendarAuthorityRuleTest(unittest.TestCase):
    """Rule 3 — phase section 20. Fires on 0 production rows; synthetic only."""

    def setUp(self):
        self.conn = fx.open_db()
        fx.reset_ids()
        self.addCleanup(self.conn.close)

    def test_calendar_evidence_wins_over_a_stale_non_calendar_deadline(self):
        cid = fx.add_commitment(
            self.conn, title="打ち合わせ", work_state="SCHEDULED",
            due_at="2026-09-01T10:00:00+09:00",           # stale LINE/Gmail time
            due_precision="minute",
            scheduled_at="2026-09-08T05:00:00+00:00",     # 14:00 JST, calendar
            scheduled_end_at="2026-09-08T06:00:00+00:00",
            authority="calendar_authority",
            evidence=[("calendar", "calendar:x"), ("line", "line:y")],
        )
        plan = fx.build_plan(self.conn)
        self.assertEqual(ids(plan, "scheduled"), [cid])
        self.assertEqual(ids(plan, "overdue"), [])
        self.assertEqual(plan["scheduled"][0]["local_time"], "14:00")
        self.assertEqual(plan["scheduled"][0]["source_types"], ["calendar", "line"])

    def test_mixed_evidence_alone_is_enough_to_confer_authority(self):
        """Even with `authority='evidence'`, calendar plus non-calendar
        evidence means the calendar owns the time."""
        cid = fx.add_commitment(
            self.conn, title="打ち合わせ", work_state="ACTIONABLE",
            due_at="2026-08-20T10:00:00+09:00", due_precision="minute",
            scheduled_at="2026-09-08T05:00:00+00:00",
            scheduled_end_at="2026-09-08T06:00:00+00:00",
            authority="evidence",
            evidence=[("calendar", "calendar:x"), ("discord", "discord:y")],
        )
        plan = fx.build_plan(self.conn)
        self.assertEqual(ids(plan, "scheduled"), [cid])
        self.assertEqual(ids(plan, "overdue"), [])

    def test_a_stale_deadline_still_counts_without_calendar_evidence(self):
        cid = fx.add_commitment(
            self.conn, title="期限切れの作業", due_at="2026-09-01T10:00:00+09:00",
            due_precision="minute", evidence="discord",
        )
        self.assertEqual(ids(fx.build_plan(self.conn), "overdue"), [cid])


class NoTitleSimilarityTest(unittest.TestCase):
    def setUp(self):
        self.conn = fx.open_db()
        fx.reset_ids()
        self.addCleanup(self.conn.close)

    def test_two_newsletters_differing_only_by_a_date_are_not_collapsed(self):
        for index, day in enumerate(("06", "09")):
            fx.add_commitment(
                self.conn, commitment_id=f"cmt_news_{index}",
                title=f"新着記事のお知らせ [2026/08/{day}]",
                due_at="2026-09-08T18:00:00+09:00", due_precision="minute",
                evidence=[("discord", f"discord:news-{day}")],
            )
        plan = fx.build_plan(self.conn)
        self.assertEqual(len(plan["must_do"]), 2)
        self.assertEqual(plan["counts"]["must_do"]["total"], 2)

    def test_byte_identical_titles_are_still_two_items(self):
        """Recurring calendar instances share a title and are legitimately
        distinct; only an identical *window* makes them the same item."""
        for index, hour in enumerate(("00", "05")):
            fx.add_commitment(
                self.conn, commitment_id=f"cmt_recurring_{index}",
                title="定例テスト", work_state="SCHEDULED",
                scheduled_at=f"2026-09-08T{hour}:00:00+00:00",
                scheduled_end_at=f"2026-09-08T{int(hour) + 1:02d}:00:00+00:00",
                authority="calendar_authority", evidence=[("calendar", f"cal:{hour}")],
            )
        self.assertEqual(len(fx.build_plan(self.conn)["scheduled"]), 2)

    def test_planner_source_never_mentions_a_similarity_matcher(self):
        import planning.planner as planner_module

        source = open(planner_module.__file__, encoding="utf-8").read()
        for forbidden in ("SequenceMatcher", "difflib", "fuzz", "levenshtein"):
            self.assertNotIn(f"import {forbidden}", source)
            self.assertNotIn(f"{forbidden}(", source)


if __name__ == "__main__":
    unittest.main()
