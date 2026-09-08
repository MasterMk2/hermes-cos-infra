"""Grounding post-validation — Phase I.2 sections 40, 86; contract section 5.

Every test here drives `render_brief` with a deliberately hostile stub client.
A model that invents a task, invents a deadline, drops an item, duplicates one,
re-ranks them, answers empty or answers at 5000 characters must all end the same
way: `renderer == "template"`, a populated `fallback_reason`, a populated
`grounding.violations`, and no exception.

The plan builders live in `test_renderer_template` so all four renderer suites
describe the same production shape.
"""

import os
import sys
import unittest

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from planning.renderer import render_brief, render_template, validate_grounding  # noqa: E402
from test_renderer_template import NOW, busy_day_plan, make_item, make_plan  # noqa: E402


def template_text(plan):
    return render_template(plan, now=NOW)[0]


def stub(text):
    """A client that answers with exactly `text`, whatever it was asked."""

    def client(messages, *, timeout=None):
        return text

    client.model = "gemini-3.8-flash-medium"
    return client


def hostile(mutate):
    """A client that takes the faithful rendering and corrupts it."""

    def client(messages, *, timeout=None):
        return mutate(client.plan_text)

    client.model = "gemini-3.8-flash-medium"
    return client


def run_hostile(plan, mutate):
    client = hostile(mutate)
    client.plan_text = template_text(plan)
    return render_brief(plan, mode="llm", client=client, now=NOW)


class TemplatePassesItsOwnGate(unittest.TestCase):
    """The gate has to admit a correct brief, or it proves nothing."""

    def test_the_template_output_is_grounded(self):
        plan = busy_day_plan()
        text, rendered, structural = render_template(plan, now=NOW)
        ok, violations = validate_grounding(
            text, plan, expected_first_line=text.split("\n")[0],
            rendered=rendered, structural=structural, now=NOW)
        self.assertTrue(ok, violations)
        self.assertEqual(violations, [])

    def test_a_model_echoing_the_template_is_accepted(self):
        plan = busy_day_plan()
        result = render_brief(plan, mode="llm", client=stub(template_text(plan)), now=NOW)
        self.assertEqual(result["renderer"], "llm")
        self.assertEqual(result["model"], "gemini-3.8-flash-medium")
        self.assertIsNone(result["fallback_reason"])
        self.assertEqual(result["grounding"], {"ok": True, "violations": []})

    def test_natural_rephrasing_within_the_rules_is_accepted(self):
        """Section 39: the model may group and phrase, keeping the facts."""
        plan = busy_day_plan()
        text = template_text(plan)
        # Leading bullet style changed, headings kept, items, facts and order
        # untouched. Only the line-leading 「・」 moves — a 「・」 inside a title
        # is part of the title (「週末の予定・都合確認への回答」).
        reworded = "\n".join("- " + line[1:] if line.startswith("・") else line
                             for line in text.split("\n"))
        result = render_brief(plan, mode="llm", client=stub(reworded), now=NOW)
        self.assertEqual(result["renderer"], "llm", result["grounding"])

    def test_a_bare_heading_without_brackets_is_accepted(self):
        plan = busy_day_plan()
        reworded = template_text(plan).replace("【", "").replace("】", "")
        result = render_brief(plan, mode="llm", client=stub(reworded), now=NOW)
        self.assertEqual(result["renderer"], "llm", result["grounding"])


class InventedContentTests(unittest.TestCase):
    """Section 40: no new task, no new deadline."""

    def test_an_invented_task_falls_back(self):
        result = run_hostile(
            busy_day_plan(),
            lambda t: t.replace("【最優先】", "【最優先】\n・確定申告の書類を税理士に送る"))
        self.assertEqual(result["renderer"], "template")
        self.assertEqual(result["fallback_reason"], "grounding_failed")
        self.assertTrue(any(v.startswith("unknown_item_line")
                            for v in result["grounding"]["violations"]),
                        result["grounding"]["violations"])

    def test_an_invented_clock_time_falls_back(self):
        result = run_hostile(busy_day_plan(),
                             lambda t: t.replace("・22:00 夜間の見学", "・14:00 夜間の見学"))
        self.assertEqual(result["renderer"], "template")
        self.assertEqual(result["fallback_reason"], "grounding_failed")
        self.assertIn("time_not_in_plan:14:00", result["grounding"]["violations"])

    def test_an_invented_deadline_phrase_falls_back(self):
        result = run_hostile(
            busy_day_plan(),
            lambda t: t.replace("・定例会の開催", "・定例会の開催（明日締切 9月13日）"))
        self.assertEqual(result["renderer"], "template")
        self.assertEqual(result["fallback_reason"], "grounding_failed")
        self.assertIn("date_not_in_plan:9月13日", result["grounding"]["violations"])

    def test_an_invented_iso_date_falls_back(self):
        result = run_hostile(busy_day_plan(),
                             lambda t: t + "\n・次回レビューは 2027-03-01 です")
        self.assertEqual(result["renderer"], "template")
        self.assertTrue(any(v.startswith("date_not_in_plan")
                            for v in result["grounding"]["violations"]))

    def test_an_invented_slash_date_falls_back(self):
        result = run_hostile(busy_day_plan(),
                             lambda t: t.replace("・定例会の開催", "・定例会の開催 12/25"))
        self.assertEqual(result["renderer"], "template")
        self.assertIn("date_not_in_plan:12/25", result["grounding"]["violations"])

    def test_a_synthetic_2359_from_a_coarse_row_is_rejected(self):
        """Amendment B4: `local_time` exists but is fake precision."""
        plan = make_plan(must_do=[make_item(title="高橋宛:予算修正は今週中(学務の要望)",
                                            due_at="2026-09-12T23:59:00+09:00",
                                            due_precision="week", local_time="23:59")])
        self.assertNotIn("23:59", template_text(plan))
        result = run_hostile(plan, lambda t: t.replace("1. 高橋宛", "1. 23:59 高橋宛"))
        self.assertEqual(result["renderer"], "template")
        self.assertIn("time_not_in_plan:23:59", result["grounding"]["violations"])


class AppendedToARealLineTests(unittest.TestCase):
    """Section 40, on the shape that actually defeats a title-match gate.

    Every other hostile case in this file injects on a NEW line, where
    `unknown_item_line` catches it. The real attack is an append: a line that
    already contains a real title is *classified as legitimate* the moment the
    title's 12-character key is found in it, and everything else on it used to
    be unexamined. All four of section 40's prohibitions went through that way,
    with `ok=True` and `violations=[]`.
    """

    def one_line_plan(self):
        return make_plan(
            date="2026-09-12",
            scheduled=[make_item(
                title="研究室のゼミ", work_state="SCHEDULED",
                scheduled_at="2026-09-12T01:00:00+00:00",
                scheduled_end_at="2026-09-12T02:00:00+00:00", local_time="10:00",
                priority_class="P1", reason_for_inclusion="scheduled_today",
                source_types=["calendar"])],
            must_do=[make_item(
                title="講座Aの申込書を提出", due_at="2026-09-12T17:00:00+09:00",
                due_precision="minute", local_time="17:00", priority_class="P1")],
        )

    def assert_rejected(self, mutate, expected_prefix):
        plan = self.one_line_plan()
        result = run_hostile(plan, mutate)
        violations = result["grounding"]["violations"]
        self.assertEqual(result["renderer"], "template", violations)
        self.assertEqual(result["fallback_reason"], "grounding_failed")
        self.assertTrue(any(v.startswith(expected_prefix) for v in violations),
                        f"expected {expected_prefix!r} in {violations!r}")
        return violations

    def test_an_invented_task_appended_to_a_real_line_falls_back(self):
        self.assert_rejected(
            lambda t: t.replace("・10:00 研究室のゼミ",
                                "・10:00 研究室のゼミ、および学費を今日中に振り込むこと"),
            "unknown_line_residue")

    def test_an_invented_kanji_clock_time_appended_falls_back(self):
        """「19時」 is a clock time. The scanner only knowing `HH:MM` is half of
        why this case passed; the line already matching a title is the rest."""
        violations = self.assert_rejected(
            lambda t: t.replace("・10:00 研究室のゼミ", "・10:00 研究室のゼミ 19時開始"),
            "time_not_in_plan")
        self.assertIn("time_not_in_plan:19時", violations)

    def test_an_invented_relative_deadline_appended_falls_back(self):
        """No digit, no `M月D日`, no `M/D` — and still a deadline claim."""
        self.assert_rejected(
            lambda t: t.replace("1. 17:00 講座Aの申込書を提出",
                                "1. 17:00 講座Aの申込書を提出（あさってまでに必着）"),
            "date_not_in_plan")

    def test_a_completion_claim_appended_falls_back(self):
        violations = self.assert_rejected(
            lambda t: t.replace("1. 17:00 講座Aの申込書を提出",
                                "1. 17:00 講座Aの申込書を提出（完了済み）"),
            "completion_claimed")
        self.assertIn("completion_claimed:完了", violations)

    def test_an_appended_comment_on_an_overdue_line_falls_back(self):
        """The overdue line already carries a decoration, so the residue check
        has to subtract the right one and still see what was added."""
        plan = busy_day_plan()
        result = run_hostile(
            plan,
            lambda t: t.replace("（12日超過・鈴木一郎）", "（12日超過・鈴木一郎・要至急対応）"))
        self.assertEqual(result["renderer"], "template", result["grounding"])
        self.assertTrue(any(v.startswith("unknown_line_residue")
                            for v in result["grounding"]["violations"]),
                        result["grounding"]["violations"])

    def test_a_faithful_line_is_still_accepted(self):
        """The gate has to admit the renderer's own line, or it proves nothing."""
        plan = self.one_line_plan()
        result = render_brief(plan, mode="llm", client=stub(template_text(plan)),
                              now=NOW)
        self.assertEqual(result["renderer"], "llm", result["grounding"])


class EvidenceInTitlesIsNotAClaim(unittest.TestCase):
    """A verbatim title carrying a date is quoted evidence, not a renderer claim."""

    def test_a_title_containing_a_slash_date_does_not_trip_the_gate(self):
        plan = make_plan(overdue=[make_item(
            title="高橋さんに9/1(火)18時の来訪を伝える約束",
            due_at="2026-08-24T19:00:00+09:00", due_precision="exact_datetime",
            local_time="19:00", overdue_days=19, counterparty="山田太郎(やまだたろう)")])
        result = render_brief(plan, mode="llm", client=stub(template_text(plan)), now=NOW)
        self.assertEqual(result["renderer"], "llm", result["grounding"])

    def test_a_title_containing_a_clock_time_does_not_trip_the_gate(self):
        plan = make_plan(next_up=[make_item(
            title="掲示物設置（9/17 13:00〜）の確認と素材提供",
            due_at="2026-09-14T13:00:00+09:00", due_precision="minute",
            local_time="13:00")])
        result = render_brief(plan, mode="llm", client=stub(template_text(plan)), now=NOW)
        self.assertEqual(result["renderer"], "llm", result["grounding"])


class OrderAndCoverageTests(unittest.TestCase):
    """Section 40: no re-ranking, no silent loss, no duplication."""

    def test_a_reordered_plan_falls_back(self):
        def reorder(text):
            lines = text.split("\n")
            block = [i for i, line in enumerate(lines) if line.startswith("・8/31まで")][0]
            last = [i for i, line in enumerate(lines) if line.startswith("・平野さんに")][0]
            lines[block], lines[last] = lines[last], lines[block]
            return "\n".join(lines)

        result = run_hostile(busy_day_plan(), reorder)
        self.assertEqual(result["renderer"], "template")
        self.assertEqual(result["fallback_reason"], "grounding_failed")
        self.assertIn("order_changed", result["grounding"]["violations"])

    def test_whole_sections_swapped_falls_back(self):
        def swap(text):
            head, rest = text.split("【今日の予定】", 1)
            body, tail = rest.split("【期限超過】", 1)
            return head + "【期限超過】" + tail.split("【次に見るもの】")[0] + \
                "【今日の予定】" + body + "【次に見るもの】" + tail.split("【次に見るもの】", 1)[1]

        result = run_hostile(busy_day_plan(), swap)
        self.assertEqual(result["renderer"], "template")
        self.assertIn("order_changed", result["grounding"]["violations"])

    def test_a_dropped_item_falls_back(self):
        result = run_hostile(
            busy_day_plan(),
            lambda t: "\n".join(line for line in t.split("\n")
                                if "棚卸し表の提出" not in line))
        self.assertEqual(result["renderer"], "template")
        self.assertTrue(any(v.startswith("missing_item")
                            for v in result["grounding"]["violations"]))

    def test_a_duplicated_item_falls_back(self):
        result = run_hostile(
            busy_day_plan(),
            lambda t: t.replace("・22:00 夜間の見学", "・22:00 夜間の見学\n・22:00 夜間の見学"))
        self.assertEqual(result["renderer"], "template")
        self.assertIn("duplicate_item", result["grounding"]["violations"])

    def test_dropping_the_overdue_age_falls_back(self):
        """Section 12: an overdue line without its age cannot be reviewed."""
        result = run_hostile(
            busy_day_plan(),
            lambda t: t.replace("（12日超過・鈴木一郎）", ""))
        self.assertEqual(result["renderer"], "template")
        self.assertTrue(any(v.startswith("overdue_age_dropped")
                            for v in result["grounding"]["violations"]),
                        result["grounding"]["violations"])

    def test_altering_the_overdue_age_falls_back(self):
        result = run_hostile(busy_day_plan(),
                             lambda t: t.replace("12日超過", "2日超過"))
        self.assertEqual(result["renderer"], "template")
        self.assertTrue(any(v.startswith("overdue_age_dropped")
                            for v in result["grounding"]["violations"]))

    def test_a_longer_age_does_not_satisfy_a_shorter_one(self):
        """「17日超過」 must not pass for an item that is 7 days late."""
        plan = make_plan(overdue=[make_item(title="定例会の出欠・予定をメンバーに確認",
                                            due_at="2026-09-05T00:00:00+09:00",
                                            due_precision="date", local_time="00:00",
                                            overdue_days=7)])
        result = run_hostile(plan, lambda t: t.replace("（7日超過）", "（17日超過）"))
        self.assertEqual(result["renderer"], "template")
        self.assertTrue(any(v.startswith("overdue_age_dropped")
                            for v in result["grounding"]["violations"]))

    def test_the_age_may_be_reworded_around(self):
        result = run_hostile(
            busy_day_plan(),
            lambda t: t.replace("（12日超過・鈴木一郎）", " — 12日超過（鈴木一郎）"))
        self.assertEqual(result["renderer"], "llm", result["grounding"])

    def test_a_restated_completion_falls_back(self):
        """Section 40: completion inference is prohibited."""
        result = run_hostile(
            busy_day_plan(),
            lambda t: t + "\n・棚卸し表の提出 は完了済みです")
        self.assertEqual(result["renderer"], "template")
        self.assertIn("duplicate_item", result["grounding"]["violations"])


class FirstLineTests(unittest.TestCase):
    """Section 42: the deterministic opening line must survive verbatim."""

    def test_a_rewritten_first_line_falls_back(self):
        result = run_hostile(
            busy_day_plan(),
            lambda t: "おはようございます！今日も一日頑張りましょう。\n" + t)
        self.assertEqual(result["renderer"], "template")
        self.assertIn("first_line_changed", result["grounding"]["violations"])

    def test_altered_counts_in_the_first_line_fall_back(self):
        result = run_hostile(busy_day_plan(),
                             lambda t: t.replace("期限超過23件", "期限超過3件"))
        self.assertEqual(result["renderer"], "template")
        self.assertIn("first_line_changed", result["grounding"]["violations"])


class LengthAndEmptinessTests(unittest.TestCase):
    """Contract section 5 checks 4 and 5, with their own fallback reasons."""

    def test_a_5000_character_answer_falls_back(self):
        plan = busy_day_plan()
        result = render_brief(plan, mode="llm", client=stub("あ" * 5000), now=NOW)
        self.assertEqual(result["renderer"], "template")
        self.assertIn("too_long", result["grounding"]["violations"])
        self.assertIsNotNone(result["fallback_reason"])

    def test_a_purely_too_long_answer_reports_too_long(self):
        """Length alone maps to `too_long`, not to the generic reason."""
        plan = busy_day_plan()
        # Every added line is a permitted structural heading, so the ONLY thing
        # wrong with this answer is that it is 1200+ characters long.
        padded = template_text(plan) + "\n【注意点】" * 400
        result = render_brief(plan, mode="llm", client=stub(padded), now=NOW)
        self.assertEqual(result["renderer"], "template")
        self.assertEqual(result["grounding"]["violations"], ["too_long"])
        self.assertEqual(result["fallback_reason"], "too_long")

    def test_an_empty_answer_falls_back(self):
        result = render_brief(busy_day_plan(), mode="llm", client=stub(""), now=NOW)
        self.assertEqual(result["renderer"], "template")
        self.assertEqual(result["fallback_reason"], "empty")
        self.assertEqual(result["grounding"]["violations"], ["empty"])

    def test_a_whitespace_only_answer_falls_back(self):
        result = render_brief(busy_day_plan(), mode="llm", client=stub("  \n \t "), now=NOW)
        self.assertEqual(result["fallback_reason"], "empty")

    def test_a_none_answer_falls_back(self):
        result = render_brief(busy_day_plan(), mode="llm", client=stub(None), now=NOW)
        self.assertEqual(result["renderer"], "template")
        self.assertEqual(result["fallback_reason"], "empty")


class ToneGateTests(unittest.TestCase):
    """Sections 43 and 44 are enforced, not merely requested."""

    def test_appended_motivation_falls_back(self):
        result = run_hostile(busy_day_plan(), lambda t: t + "\n今日も頑張りましょう！")
        self.assertEqual(result["renderer"], "template")
        self.assertNotIn("頑張りましょう", result["text"])

    def test_appended_alarm_falls_back(self):
        result = run_hostile(busy_day_plan(),
                             lambda t: t + "\n期限超過が23件もあります。大変です！")
        self.assertEqual(result["renderer"], "template")
        self.assertNotIn("大変です", result["text"])


class NeverRaisesTests(unittest.TestCase):
    """`render_brief` must never raise for a bad model response."""

    def test_a_client_that_raises_falls_back(self):
        def boom(messages, *, timeout=None):
            raise RuntimeError("bridge returned 502")

        result = render_brief(busy_day_plan(), mode="llm", client=boom, now=NOW)
        self.assertEqual(result["renderer"], "template")
        self.assertEqual(result["fallback_reason"], "llm_unavailable")

    def test_a_client_that_times_out_reports_timeout(self):
        def slow(messages, *, timeout=None):
            raise TimeoutError("the read operation timed out")

        result = render_brief(busy_day_plan(), mode="llm", client=slow, now=NOW)
        self.assertEqual(result["fallback_reason"], "timeout")

    def test_a_client_returning_a_non_string_does_not_raise(self):
        def weird(messages, *, timeout=None):
            return {"unexpected": "shape"}

        result = render_brief(busy_day_plan(), mode="llm", client=weird, now=NOW)
        self.assertEqual(result["renderer"], "template")
        self.assertIsNotNone(result["fallback_reason"])

    def test_the_fallback_text_is_always_the_full_template(self):
        plan = busy_day_plan()
        expected = template_text(plan)
        for mutate in (lambda t: "", lambda t: "あ" * 5000,
                       lambda t: t + "\n・でっちあげたタスク"):
            result = run_hostile(plan, mutate)
            self.assertEqual(result["text"], expected)
            self.assertEqual(result["renderer"], "template")

    def test_every_hostile_case_reports_a_reason_and_violations(self):
        plan = busy_day_plan()
        cases = {
            "invented task": lambda t: t + "\n・でっちあげたタスク",
            "invented time": lambda t: t.replace("22:00", "14:00"),
            "invented deadline": lambda t: t.replace("・定例会の開催",
                                                     "・定例会の開催（明日締切 9月13日）"),
            "too long": lambda t: "あ" * 5000,
            "empty": lambda t: "",
            "reordered": lambda t: "\n".join(reversed(t.split("\n"))),
            # Appended to an EXISTING item's line rather than added as a new
            # one — the family that used to pass the gate entirely.
            "appended task": lambda t: t.replace(
                "・定例会の開催", "・定例会の開催、あわせて学費の振込も済ませること"),
            "appended completion": lambda t: t.replace(
                "・定例会の開催", "・定例会の開催（対応済）"),
            "appended kanji time": lambda t: t.replace(
                "・定例会の開催", "・定例会の開催 19時開始"),
            "appended relative deadline": lambda t: t.replace(
                "・定例会の開催", "・定例会の開催（来週までに）"),
        }
        for name, mutate in cases.items():
            with self.subTest(case=name):
                result = run_hostile(plan, mutate)
                self.assertEqual(result["renderer"], "template")
                self.assertIsNotNone(result["fallback_reason"])
                self.assertFalse(result["grounding"]["ok"])
                self.assertTrue(result["grounding"]["violations"])


class ValidateGroundingDirectTests(unittest.TestCase):
    """`validate_grounding` is usable on its own, with no template handed in."""

    def test_it_derives_the_template_when_not_given_one(self):
        plan = busy_day_plan()
        ok, violations = validate_grounding(template_text(plan), plan, now=NOW)
        self.assertTrue(ok, violations)

    def test_violations_are_deduplicated_and_ordered(self):
        plan = busy_day_plan()
        bad = template_text(plan) + "\n・14:00 でっちあげ\n・14:00 でっちあげ2"
        ok, violations = validate_grounding(bad, plan, now=NOW)
        self.assertFalse(ok)
        self.assertEqual(len(violations), len(set(violations)))


if __name__ == "__main__":
    unittest.main()
