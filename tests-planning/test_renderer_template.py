"""Template fallback correctness — Phase I.2 sections 41-44, 76, 100-102.

The template is the product, not a degraded mode: it is what the 08:30 job
delivers whenever a provider is down, so every property the brief must have is
asserted here against the deterministic path alone.

This module also holds the plan builders the other three renderer suites
import. `tests-planning/planning_fixtures.py` (T2) owns the *database* fixtures;
the renderer consumes only the plan JSON of contract section 3, so it needs
plan dicts, not rows. If `planning_fixtures.make_plan_fixture()` later lands,
`make_plan` here is the single place to redirect.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from planning.renderer import (  # noqa: E402
    MAX_TITLE_CHARS,
    count_sentence,
    render_brief,
    render_template,
)

JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 9, 12, 8, 30, tzinfo=JST)


def make_item(**overrides):
    """One Item of contract section 3 plus the amendment I additions."""
    item = {
        "commitment_id": "cmt_" + format(abs(hash(overrides.get("title", ""))) % 0xFFFFFFFFFFFF, "012x"),
        "title": "無題",
        "responsibility": "SELF",
        "work_state": "ACTIONABLE",
        "due_at": None,
        "scheduled_at": None,
        "scheduled_end_at": None,
        "counterparty": None,
        "local_time": None,
        "priority_class": "P2",
        "reason_for_inclusion": "due_today",
        "source_types": ["discord"],
        "due_precision": None,
        "all_day": False,
    }
    item.update(overrides)
    return item


def make_plan(*, date="2026-09-12", timezone_name="Asia/Tokyo", conflicts=None,
              warnings=None, source_freshness=None, totals=None, **categories):
    """A plan dict of contract section 3.

    `totals` overrides `counts.<cat>.total` so the "+N more" arithmetic of
    section 28 can be exercised without inventing 47 backlog items.
    """
    plan = {
        "date": date,
        "timezone": timezone_name,
        "generated_at": f"{date}T08:30:00+09:00",
        "scheduled": list(categories.get("scheduled") or []),
        "must_do": list(categories.get("must_do") or []),
        "overdue": list(categories.get("overdue") or []),
        "next_up": list(categories.get("next_up") or []),
        "waiting_for": list(categories.get("waiting_for") or []),
        "backlog": list(categories.get("backlog") or []),
        "conflicts": list(conflicts or []),
        "plan_hash": "0" * 64,
    }
    totals = totals or {}
    plan["counts"] = {
        name: {"shown": len(plan[name]), "total": int(totals.get(name, len(plan[name])))}
        for name in ("scheduled", "must_do", "overdue", "next_up", "waiting_for", "backlog")
    }
    plan["counts"]["conflicts"] = {"shown": len(plan["conflicts"]), "total": len(plan["conflicts"])}
    plan["counts"]["filtered_noise"] = {"shown": 0, "total": int(totals.get("filtered_noise", 0))}
    if source_freshness is not None:
        plan["source_freshness"] = source_freshness
    if warnings:
        plan["warnings"] = list(warnings)
    return plan


def busy_day_plan():
    """The 2026-09-12 shape recommended by I2-PROFILE section N.

    Two scheduled rows (one all-day, one timed), a real same-day deadline, a
    deep overdue tail, a waiting set and a backlog sample — the only production
    date that exercises every section at once.
    """
    return make_plan(
        date="2026-09-12",
        scheduled=[
            make_item(title="地域の交流会", responsibility="SHARED", work_state="SCHEDULED",
                      scheduled_at="2026-09-12", scheduled_end_at="2026-09-13", all_day=True,
                      priority_class="P1", reason_for_inclusion="scheduled_today",
                      source_types=["calendar"]),
            make_item(title="夜間の見学", work_state="SCHEDULED",
                      scheduled_at="2026-09-12T13:00:00+00:00",
                      scheduled_end_at="2026-09-12T14:00:00+00:00", local_time="22:00",
                      priority_class="P1", reason_for_inclusion="scheduled_today",
                      source_types=["calendar"]),
        ],
        must_do=[
            make_item(title="週末の予定・都合確認への回答", due_at="2026-09-12T18:00:00+09:00",
                      due_precision="daypart", local_time="18:00", priority_class="P1",
                      source_types=["line"]),
            make_item(title="棚卸し表の提出", due_at="2026-09-12T09:30:00+09:00",
                      due_precision="exact_datetime", local_time="09:30", priority_class="P1"),
        ],
        overdue=[
            make_item(title="8/31まで:配布文の確認とメール送信、連絡先の割り振り",
                      due_at="2026-08-31T00:00:00+09:00", due_precision="date",
                      counterparty="鈴木一郎", local_time="00:00", overdue_days=12,
                      priority_class="P1", reason_for_inclusion="overdue_12d"),
            make_item(title="ベンダB見積書の再提出は今週中か確認",
                      due_at="2026-08-28T23:59:00+09:00", due_precision="daypart",
                      counterparty="鈴木一郎", local_time="23:59", overdue_days=15,
                      priority_class="P1", reason_for_inclusion="overdue_15d"),
            make_item(title="高橋さんに9/1(火)18時の来訪を伝える約束",
                      due_at="2026-08-24T19:00:00+09:00", due_precision="exact_datetime",
                      counterparty="山田太郎(やまだたろう)", local_time="19:00", overdue_days=19,
                      priority_class="P1", reason_for_inclusion="overdue_19d"),
        ],
        next_up=[
            make_item(title="掲示物設置（9/17 13:00〜）の確認と素材提供",
                      due_at="2026-09-14T13:00:00+09:00", due_precision="minute",
                      local_time="13:00", reason_for_inclusion="due_within_72h",
                      source_types=["line"]),
        ],
        waiting_for=[
            make_item(title="案は了承、実施日を改めて連絡する約束", responsibility="COUNTERPARTY",
                      work_state="WAITING", counterparty="鈴木一郎", priority_class="P3",
                      reason_for_inclusion="waiting"),
            make_item(title="来週以降の日程調整・都合確認", responsibility="COUNTERPARTY",
                      work_state="WAITING", due_at="2026-09-03T00:00:00+09:00",
                      due_precision="date", local_time="00:00", priority_class="P3",
                      reason_for_inclusion="waiting_followup", source_types=["line"]),
        ],
        backlog=[
            make_item(title="定例会の開催", priority_class="P4",
                      reason_for_inclusion="no_date_backlog"),
            make_item(title="掲示物の確認・承認", counterparty="鈴木一郎(すずきいちろう)",
                      priority_class="P4", reason_for_inclusion="no_date_backlog",
                      source_types=["line"]),
        ],
        conflicts=[{
            "kind": "deadline_during_scheduled",
            "commitment_ids": ["cmt_a", "cmt_b"],
            "window_start": "2026-09-12T00:00:00+09:00",
            "window_end": "2026-09-13T00:00:00+09:00",
            "detail": "終日の予定と同じ日に期限があります",
        }],
        totals={"overdue": 23, "waiting_for": 7, "backlog": 47, "filtered_noise": 194},
        warnings=["data_freshness_warning"],
        source_freshness={
            "calendar": {"last_success_at": "2026-09-12T08:00:00+09:00", "age_seconds": 1800,
                         "stale": False, "threshold_seconds": 86400},
            "gmail": {"last_success_at": "2026-09-10T08:00:00+09:00", "age_seconds": 174600,
                      "stale": True, "threshold_seconds": 86400},
        },
    )


class TemplateStructureTests(unittest.TestCase):
    """Section 41: the six headings, empty sections omitted."""

    def test_all_sections_present_on_a_busy_day(self):
        text = render_brief(busy_day_plan(), mode="template", now=NOW)["text"]
        for heading in ("【今日の予定】", "【最優先】", "【期限超過】",
                        "【次に見るもの】", "【返事待ち】", "【注意点】"):
            self.assertIn(heading, text)

    def test_headings_appear_in_section_41_order(self):
        text = render_brief(busy_day_plan(), mode="template", now=NOW)["text"]
        order = [text.index(h) for h in ("【今日の予定】", "【最優先】", "【期限超過】",
                                         "【次に見るもの】", "【返事待ち】", "【注意点】")]
        self.assertEqual(order, sorted(order))

    def test_empty_sections_are_omitted(self):
        plan = make_plan(must_do=[make_item(title="棚卸し表の提出",
                                            due_at="2026-09-12T09:30:00+09:00",
                                            due_precision="exact_datetime", local_time="09:30")])
        text = render_brief(plan, mode="template", now=NOW)["text"]
        self.assertIn("【最優先】", text)
        for heading in ("【今日の予定】", "【期限超過】", "【次に見るもの】",
                        "【返事待ち】", "【注意点】"):
            self.assertNotIn(heading, text)

    def test_completely_empty_plan_still_renders_a_brief(self):
        result = render_brief(make_plan(), mode="template", now=NOW)
        self.assertEqual(result["renderer"], "template")
        self.assertTrue(result["text"].strip())
        self.assertIn("ありません", result["text"])

    def test_backlog_is_rendered_under_next_up_heading(self):
        plan = make_plan(backlog=[make_item(title="定例会の開催",
                                            reason_for_inclusion="no_date_backlog")])
        text = render_brief(plan, mode="template", now=NOW)["text"]
        self.assertIn("【次に見るもの】", text)
        self.assertIn("定例会の開催", text)
        self.assertEqual(text.count("【次に見るもの】"), 1)

    def test_output_is_deterministic(self):
        plan = busy_day_plan()
        first = render_brief(plan, mode="template", now=NOW)["text"]
        second = render_brief(plan, mode="template", now=NOW)["text"]
        self.assertEqual(first, second)

    def test_result_shape_matches_contract_section_5(self):
        result = render_brief(busy_day_plan(), mode="template", now=NOW)
        self.assertEqual(sorted(result), ["fallback_reason", "grounding", "model",
                                          "renderer", "text"])
        self.assertEqual(result["renderer"], "template")
        self.assertIsNone(result["model"])
        self.assertIsNone(result["fallback_reason"])
        self.assertEqual(result["grounding"], {"ok": True, "violations": []})


class FirstLineTests(unittest.TestCase):
    """Section 42: the opening line is a deterministic count sentence."""

    def test_counts_come_from_the_plan_counts_block(self):
        plan = make_plan(
            scheduled=[make_item(title="A", scheduled_at="2026-09-12T00:00:00+00:00",
                                 local_time="09:00")] * 3,
            must_do=[make_item(title="B")] * 2,
            overdue=[make_item(title="C", overdue_days=1)],
        )
        self.assertEqual(count_sentence(plan),
                         "今日は予定3件、今日中のタスク2件、期限超過1件です。")

    def test_first_line_of_the_brief_is_that_sentence(self):
        plan = busy_day_plan()
        text = render_brief(plan, mode="template", now=NOW)["text"]
        self.assertEqual(text.split("\n")[0], count_sentence(plan, now=NOW))

    def test_totals_not_shown_counts_drive_the_sentence(self):
        plan = busy_day_plan()
        # 3 overdue rows are listed but 23 exist; the sentence reports reality.
        self.assertIn("期限超過23件", count_sentence(plan, now=NOW))

    def test_zero_everything_is_stated_plainly(self):
        self.assertEqual(count_sentence(make_plan()),
                         "今日は予定も今日中のタスクも期限超過もありません。")

    def test_secondary_counts_are_a_second_clause(self):
        plan = make_plan(waiting_for=[make_item(title="W", work_state="WAITING")],
                         totals={"waiting_for": 7})
        self.assertIn("ほかに返事待ち7件があります。", count_sentence(plan))

    def test_a_non_today_plan_date_says_the_date_instead_of_today(self):
        plan = busy_day_plan()
        later = datetime(2026, 9, 14, 8, 30, tzinfo=JST)
        self.assertTrue(count_sentence(plan, now=later).startswith("9月12日は"))

    def test_without_a_clock_the_sentence_never_guesses(self):
        self.assertTrue(count_sentence(busy_day_plan()).startswith("今日は"))


class MoreLineTests(unittest.TestCase):
    """Section 28: the truncated tail is `counts.total - shown`."""

    def test_more_line_uses_total_minus_shown(self):
        text = render_brief(busy_day_plan(), mode="template", now=NOW)["text"]
        self.assertIn("・ほか20件", text)   # overdue: 23 - 3

    def test_next_up_and_backlog_share_one_more_line(self):
        text = render_brief(busy_day_plan(), mode="template", now=NOW)["text"]
        # next_up hides 0, backlog hides 45; the merged section states 45 once.
        self.assertIn("・ほか45件", text)
        self.assertEqual(text.count("ほか45件"), 1)

    def test_no_more_line_when_nothing_is_hidden(self):
        plan = make_plan(waiting_for=[make_item(title="返事待ちの件",
                                                work_state="WAITING")])
        text = render_brief(plan, mode="template", now=NOW)["text"]
        self.assertFalse([line for line in text.split("\n") if line.startswith("・ほか")])


class TimeAndDateFidelityTests(unittest.TestCase):
    """Section 10 and amendments B2/B4/J: never invent or upgrade precision."""

    def test_all_day_renders_as_zenjitsu_never_a_clock(self):
        text = render_brief(busy_day_plan(), mode="template", now=NOW)["text"]
        self.assertIn("・終日 地域の交流会", text)

    def test_calendar_scheduled_time_is_shown_verbatim(self):
        text = render_brief(busy_day_plan(), mode="template", now=NOW)["text"]
        self.assertIn("・22:00 夜間の見学", text)

    def test_coarse_due_precision_never_renders_a_clock_time(self):
        plan = make_plan(must_do=[make_item(title="高橋宛:予算修正は今週中(学務の要望)",
                                            due_at="2026-09-12T23:59:00+09:00",
                                            due_precision="week", local_time="23:59")])
        text = render_brief(plan, mode="template", now=NOW)["text"]
        self.assertIn("高橋宛:予算修正は今週中(学務の要望)", text)
        self.assertNotIn("23:59", text)

    def test_exact_datetime_due_precision_does_render_a_clock_time(self):
        plan = make_plan(must_do=[make_item(title="棚卸し表の提出",
                                            due_at="2026-09-12T09:30:00+09:00",
                                            due_precision="exact_datetime",
                                            local_time="09:30")])
        self.assertIn("09:30", render_brief(plan, mode="template", now=NOW)["text"])

    def test_a_future_item_carries_its_date(self):
        text = render_brief(busy_day_plan(), mode="template", now=NOW)["text"]
        self.assertIn("9月14日", text)

    def test_a_today_item_does_not_repeat_todays_date(self):
        text = render_brief(busy_day_plan(), mode="template", now=NOW)["text"]
        self.assertNotIn("9月12日", text.split("\n", 1)[1])

    def test_utc_stored_scheduled_time_is_not_reformatted_from_the_iso(self):
        """Section 10: the renderer trusts `local_time`, it does not convert.

        `scheduled_at` is 13:00 UTC; only the planner's `local_time` (22:00 JST)
        may appear. A renderer that formatted the ISO string itself would print
        13:00 here.
        """
        text = render_brief(busy_day_plan(), mode="template", now=NOW)["text"]
        self.assertIn("22:00", text)
        self.assertNotIn("13:00 夜間の見学", text)

    def test_overdue_days_are_stated_not_recomputed(self):
        text = render_brief(busy_day_plan(), mode="template", now=NOW)["text"]
        self.assertIn("（12日超過・鈴木一郎）", text)
        self.assertIn("（19日超過・山田太郎(やまだたろう)）", text)

    def test_same_day_overdue_reads_as_today(self):
        plan = make_plan(overdue=[make_item(title="投票の依頼（本日締切）", overdue_days=0,
                                            due_at="2026-09-12T00:10:00+09:00",
                                            due_precision="date", local_time="00:10")])
        self.assertIn("（本日期限超過）", render_brief(plan, mode="template", now=NOW)["text"])


class CounterpartyTests(unittest.TestCase):
    """Amendment J: a mail sender is not a person waiting on you."""

    def test_gmail_style_counterparty_is_not_rendered(self):
        plan = make_plan(waiting_for=[make_item(
            title="ShipXplorer Sharing Station Offline",
            work_state="WAITING",
            counterparty='"ShipXplorer Support" <support@shipxplorer.example>')])
        text = render_brief(plan, mode="template", now=NOW)["text"]
        self.assertNotIn("@", text)
        self.assertNotIn("ShipXplorer Support", text)

    def test_opaque_line_user_id_is_not_rendered_as_a_name(self):
        plan = make_plan(waiting_for=[make_item(
            title="参加メンバー依頼と名簿送付要否の確認", work_state="WAITING",
            counterparty="u0000000000000000000000000000")])
        self.assertNotIn("ud0364457", render_brief(plan, mode="template", now=NOW)["text"])

    def test_a_real_name_is_rendered(self):
        plan = make_plan(waiting_for=[make_item(title="案は了承、実施日を改めて連絡する約束",
                                                work_state="WAITING",
                                                counterparty="鈴木一郎")])
        self.assertIn("（鈴木一郎）", render_brief(plan, mode="template", now=NOW)["text"])


class ToneTests(unittest.TestCase):
    """Sections 43 and 44: information density, no filler, no escalation."""

    FILLER = ("頑張りましょう", "頑張って", "素敵な一日", "良い一日", "応援", "ファイト",
              "お疲れ様", "ですね！")
    ALARM = ("大変です", "緊急です", "危険", "急いで", "至急", "!!", "！！", "ヤバ")

    def test_no_motivational_filler(self):
        text = render_brief(busy_day_plan(), mode="template", now=NOW)["text"]
        for phrase in self.FILLER:
            self.assertNotIn(phrase, text)

    def test_no_alarm_escalation_however_large_the_overdue_count(self):
        plan = busy_day_plan()
        plan["counts"]["overdue"]["total"] = 999
        text = render_brief(plan, mode="template", now=NOW)["text"]
        for phrase in self.ALARM:
            self.assertNotIn(phrase, text)
        self.assertIn("期限超過999件", text)

    def test_zero_overdue_and_huge_overdue_use_the_same_sentence_shape(self):
        calm = count_sentence(make_plan(overdue=[make_item(title="X", overdue_days=1)]))
        loud = count_sentence(make_plan(overdue=[make_item(title="X", overdue_days=1)],
                                        totals={"overdue": 500}))
        self.assertTrue(calm.endswith("です。"))
        self.assertTrue(loud.endswith("です。"))


class NotesTests(unittest.TestCase):
    """Sections 30 and 37: conflicts and freshness, verbatim from the plan."""

    def test_conflict_detail_is_copied_verbatim(self):
        text = render_brief(busy_day_plan(), mode="template", now=NOW)["text"]
        self.assertIn("・終日の予定と同じ日に期限があります", text)

    def test_stale_sources_are_named_without_escalation(self):
        text = render_brief(busy_day_plan(), mode="template", now=NOW)["text"]
        self.assertIn("・情報源の更新が止まっています: gmail", text)

    def test_notes_section_is_omitted_when_there_is_nothing_to_note(self):
        plan = make_plan(must_do=[make_item(title="定例会の開催")])
        self.assertNotIn("【注意点】", render_brief(plan, mode="template", now=NOW)["text"])


class LengthTests(unittest.TestCase):
    """Sections 28 and 102: bounded output, no backlog dump."""

    def test_busy_day_fits_the_gateway_limit(self):
        text = render_brief(busy_day_plan(), mode="template", now=NOW)["text"]
        self.assertLessEqual(len(text), 1200)

    def test_an_oversized_plan_is_trimmed_into_the_limit(self):
        plan = make_plan(
            scheduled=[make_item(title=f"予定{i}", scheduled_at="2026-09-12T00:00:00+00:00",
                                 local_time="09:00", work_state="SCHEDULED") for i in range(5)],
            must_do=[make_item(title=f"今日中のタスク{i}" + "あ" * 50) for i in range(5)],
            overdue=[make_item(title=f"期限超過{i}" + "い" * 50, overdue_days=i)
                     for i in range(5)],
            next_up=[make_item(title=f"次{i}" + "う" * 50) for i in range(5)],
            waiting_for=[make_item(title=f"待{i}" + "え" * 50, work_state="WAITING")
                         for i in range(7)],
            backlog=[make_item(title=f"棚{i}" + "お" * 50) for i in range(3)],
        )
        text = render_brief(plan, mode="template", now=NOW)["text"]
        self.assertLessEqual(len(text), 1200)
        # The most important section survives the trim intact.
        self.assertIn("予定0", text)
        self.assertIn("【最優先】", text)

    def test_trimming_preserves_the_counts_as_a_more_line(self):
        plan = make_plan(backlog=[make_item(title="棚" + "お" * 60) for _ in range(3)],
                         totals={"backlog": 47})
        text = render_brief(plan, mode="template", now=NOW, config={"renderer": {"max_chars": 120}})["text"]
        self.assertLessEqual(len(text), 120)
        self.assertIn("日付未定47件", text)

    def test_a_long_title_is_truncated_not_summarised(self):
        long_title = "あ" * 200
        plan = make_plan(must_do=[make_item(title=long_title)])
        text = render_brief(plan, mode="template", now=NOW)["text"]
        self.assertIn("あ" * (MAX_TITLE_CHARS - 1) + "…", text)


class NoAddedFaultsTests(unittest.TestCase):
    """Section 100: the renderer cannot fix a bad plan, but must not add a fault."""

    def test_a_waiting_item_is_never_framed_as_the_users_own_task(self):
        text = render_brief(busy_day_plan(), mode="template", now=NOW)["text"]
        waiting_block = text.split("【返事待ち】", 1)[1]
        self.assertIn("案は了承、実施日を改めて連絡する約束", waiting_block)
        must_do_block = text.split("【最優先】", 1)[1].split("【", 1)[0]
        self.assertNotIn("形は了承", must_do_block)

    def test_only_the_plans_own_titles_appear(self):
        plan = busy_day_plan()
        text = render_brief(plan, mode="template", now=NOW)["text"]
        titles = {i["title"] for cat in ("scheduled", "must_do", "overdue", "next_up",
                                         "waiting_for", "backlog") for i in plan[cat]}
        for line in text.split("\n"):
            stripped = line.strip()
            if not stripped.startswith("・") and not stripped[:2].rstrip(".").isdigit():
                continue
            if stripped.startswith("・ほか"):
                continue
            if stripped in ("・終日の予定と同じ日に期限があります",
                            "・情報源の更新が止まっています: gmail"):
                continue
            self.assertTrue(any(t[:12] in stripped for t in titles),
                            f"line matches no plan title: {stripped}")

    def test_no_item_is_rendered_twice(self):
        text = render_brief(busy_day_plan(), mode="template", now=NOW)["text"]
        self.assertEqual(text.count("棚卸し表の提出"), 1)
        self.assertEqual(text.count("地域の交流会"), 1)

    def test_the_renderer_reads_no_state_and_touches_no_db(self):
        """The template path is pure: same input, same output, no side effect."""
        plan = busy_day_plan()
        before = repr(plan)
        render_brief(plan, mode="template", now=NOW)
        self.assertEqual(before, repr(plan))


class MalformedPlanTests(unittest.TestCase):
    """A renderer that raises at 08:30 delivers nothing (sections 60, 76)."""

    def test_missing_categories_do_not_raise(self):
        self.assertTrue(render_brief({"date": "2026-09-12"}, mode="template")["text"])

    def test_missing_counts_fall_back_to_list_lengths(self):
        plan = {"date": "2026-09-12", "timezone": "Asia/Tokyo",
                "must_do": [make_item(title="定例会の開催")]}
        self.assertIn("今日中のタスク1件", render_brief(plan, mode="template")["text"])

    def test_unparseable_timestamps_do_not_raise(self):
        plan = make_plan(must_do=[make_item(title="壊れた日付", due_at="not-a-date",
                                            due_precision="minute", local_time="09:00")])
        self.assertIn("壊れた日付", render_brief(plan, mode="template", now=NOW)["text"])

    def test_unknown_timezone_falls_back_to_tokyo(self):
        plan = make_plan(timezone_name="Mars/Olympus",
                         must_do=[make_item(title="定例会の開催")])
        self.assertIn("定例会の開催", render_brief(plan, mode="template", now=NOW)["text"])

    def test_render_template_returns_positions_and_structural_lines(self):
        text, rendered, structural = render_template(busy_day_plan(), now=NOW)
        self.assertEqual(len(rendered), 12)
        self.assertEqual(rendered, sorted(rendered))
        self.assertIn("【今日の予定】", structural)
        self.assertEqual(structural[0], text.split("\n")[0])


if __name__ == "__main__":
    unittest.main()
