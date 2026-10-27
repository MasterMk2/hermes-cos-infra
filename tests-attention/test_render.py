#!/usr/bin/env python3
"""G.2 unit tests — the notification text (spec sections 68-73).

A notification has to answer three things and stop: what happened, why now,
and what to do about it. These tests exist because the first digest that went
to a real phone was one 200-character paragraph — eight bullet points folded
into a single line by a `_bound` helper that flattened newlines while capping
length. Nothing in the suite noticed, because nothing looked at the shape of
the output.

They also pin the two properties that are about safety rather than taste: a
notification never carries a path or a credential (section 71), and it never
carries an empty body (section 73's deterministic fallback).
"""

import unittest

import att_render as render
import att_fixtures as fx


CLOCK = fx.clock_at("2026-09-04T12:00")


def sig(con, **over):
    return fx.put_signal(con, clock=CLOCK, created_at=CLOCK.now_iso(), **over)


class TestEventText(unittest.TestCase):
    def test_a_deadline_says_what_and_how_long(self):
        con = fx.db()
        row = sig(con, signal_type="due_soon", severity="URGENT",
                  summary="資料の提出", due_at=fx.ahead(CLOCK, hours=14))
        subject, body = render.render(row, CLOCK)
        self.assertIn("資料の提出", body)
        self.assertIn("残り", body)
        self.assertIn("約14時間", body)
        self.assertTrue(subject)

    def test_an_overdue_item_says_by_how_much(self):
        con = fx.db()
        row = sig(con, signal_type="overdue", severity="IMPORTANT",
                  summary="見積書の再提出", due_at=fx.ago(CLOCK, days=3))
        _subject, body = render.render(row, CLOCK)
        self.assertIn("3日超過", body)

    def test_urgent_is_marked_and_info_is_not(self):
        con = fx.db()
        urgent = sig(con, source_id="a", signal_type="due_soon",
                     severity="URGENT", due_at=fx.ahead(CLOCK, minutes=20),
                     key_parts=("event", "a", "due-soon", "30m"))
        info = sig(con, source_id="b", signal_type="due_soon", severity="INFO",
                   due_at=fx.ahead(CLOCK, hours=20),
                   key_parts=("event", "b", "due-soon", "24h"))
        self.assertTrue(render.render(urgent, CLOCK)[1].startswith("【要対応】"))
        self.assertFalse(render.render(info, CLOCK)[1].startswith("【"))


class TestCodingText(unittest.TestCase):
    def test_a_completion_names_the_job_and_the_gates(self):
        con = fx.db()
        row = sig(con, source_type="coding", source_id="job_abc",
                  signal_type="coding_completed", severity="INFO",
                  summary="fixture を実装する")
        _subject, body = render.render(row, CLOCK)
        self.assertIn("job_abc", body)
        self.assertIn("Muse review", body)

    def test_waiting_human_carries_a_bounded_reason(self):
        con = fx.db()
        row = sig(con, source_type="coding", source_id="job_xyz",
                  signal_type="coding_waiting_human", severity="IMPORTANT",
                  summary="関数を実装する",
                  metadata={"reason": "reviewer could not verify the change"})
        _subject, body = render.render(row, CLOCK)
        self.assertIn("job_xyz", body)
        self.assertIn("理由", body)

    def test_no_notification_ever_carries_a_path_or_a_token(self):
        # Section 71. The reason is sanitised at the collector; this asserts
        # the property at the last point before it leaves the process.
        import att_coding

        con = fx.db()
        for hostile in ("failed at /srv/hermes/hermes-data/.env",
                        "Authorization: Bearer abcdef0123456789",
                        "ssh key /home/someone/.ssh/id_ed25519 rejected"):
            row = sig(con, source_type="coding", source_id="job_h",
                      signal_type="coding_waiting_human", severity="IMPORTANT",
                      summary="x",
                      metadata={"reason": att_coding.safe_reason(hostile)},
                      key_parts=("job", "job_h", hostile[:8]))
            _subject, body = render.render(row, CLOCK)
            with self.subTest(reason=hostile[:24]):
                self.assertNotIn("/srv/", body)
                self.assertNotIn("/home/", body)
                self.assertNotIn("Bearer", body)


class TestHealthText(unittest.TestCase):
    def test_the_evidence_is_japanese_and_concrete(self):
        # The first real health notification read "約6分継続しています。（the
        # last 3 runs reported a failure）" — two languages, and the number
        # that mattered was in the parenthetical.
        con = fx.db()
        row = sig(con, source_type="health", source_id="line_refresh",
                  signal_type="health_degraded", severity="IMPORTANT",
                  summary="LINE 知識の更新ジョブが失敗し続けています",
                  metadata={"consecutive_failures": 3, "degraded_seconds": 4000,
                            "detail": "the last 3 runs reported a failure"})
        _subject, body = render.render(row, CLOCK)
        self.assertIn("直近 3 回", body)
        self.assertNotIn("reported a failure", body)

    def test_recovery_is_short(self):
        con = fx.db()
        row = sig(con, source_type="health", source_id="gateway",
                  signal_type="health_recovered", severity="INFO",
                  summary="gateway は復旧しました",
                  metadata={"reason": "unreachable"})
        _subject, body = render.render(row, CLOCK)
        self.assertIn("復旧", body)
        self.assertLess(len(body), 120)

    def test_a_partial_recovery_does_not_claim_the_service_recovered(self):
        con = fx.db()
        row = sig(con, source_type="health", source_id="gateway",
                  signal_type="health_recovered", severity="INFO",
                  summary="Discord Interface Bot が切断されています は解消しました"
                          "（Hermes が応答を返せていません は継続中）",
                  metadata={"reason": "discord_disconnected",
                            "still_open": ["hermes_failing"]})
        subject, body = render.render(row, CLOCK)
        self.assertNotIn("gateway は復旧しました", body)
        self.assertIn("継続中", body)
        line = render.digest_line(row, CLOCK)
        self.assertTrue(line.startswith("・一部復旧: gateway"), line)


class TestShape(unittest.TestCase):
    def test_a_multi_line_body_keeps_its_line_breaks(self):
        con = fx.db()
        row = sig(con, signal_type="due_soon", severity="IMPORTANT",
                  summary="ある作業", due_at=fx.ahead(CLOCK, hours=5))
        _subject, body = render.render(row, CLOCK)
        self.assertIn("\n", body,
                      "the notification was flattened into one line")

    def test_a_digest_is_one_line_per_item(self):
        con = fx.db()
        rows = [sig(con, source_id=f"e{i}", signal_type="due_soon",
                    severity="IMPORTANT", summary=f"項目{i}",
                    due_at=fx.ahead(CLOCK, hours=2 + i),
                    key_parts=("event", f"e{i}", "due-soon", "3h"))
                for i in range(5)]
        _subject, body = render.render_digest(rows, "catchup", CLOCK)
        lines = body.split("\n")
        self.assertEqual(len(lines), 6)          # title + five items
        self.assertTrue(all(l.startswith("・") for l in lines[1:]))

    def test_a_digest_says_how_many_it_did_not_show(self):
        con = fx.db()
        rows = [sig(con, source_id=f"e{i}", signal_type="due_soon",
                    severity="IMPORTANT", summary=f"項目{i}",
                    due_at=fx.ahead(CLOCK, hours=2),
                    key_parts=("event", f"e{i}", "due-soon", "3h"))
                for i in range(12)]
        _subject, body = render.render_digest(rows, "baseline", CLOCK,
                                              max_items=8)
        self.assertIn("ほか 4 件", body)
        self.assertIn("（12 件）", body)

    def test_an_empty_digest_renders_nothing(self):
        # Section 61, at the renderer as well as at the engine.
        self.assertEqual(render.render_digest([], "catchup", CLOCK),
                         (None, None))

    def test_the_most_urgent_item_leads_the_digest(self):
        con = fx.db()
        rows = [
            sig(con, source_id="c", signal_type="coding_completed",
                source_type="coding", severity="INFO", summary="完了",
                key_parts=("job", "c", "completed")),
            sig(con, source_id="h", source_type="health",
                signal_type="health_degraded", severity="CRITICAL",
                summary="障害", key_parts=("health", "h", "degraded")),
            sig(con, source_id="e", signal_type="overdue",
                severity="IMPORTANT", summary="期限超過",
                due_at=fx.ago(CLOCK, days=1),
                key_parts=("event", "e", "overdue")),
        ]
        _subject, body = render.render_digest(rows, "catchup", CLOCK)
        lines = body.split("\n")[1:]
        self.assertIn("障害", lines[0])
        self.assertIn("期限超過", lines[1])

    def test_every_body_is_bounded(self):
        con = fx.db()
        row = sig(con, signal_type="due_soon", severity="INFO",
                  summary="あ" * 5000, due_at=fx.ahead(CLOCK, hours=2))
        _subject, body = render.render(row, CLOCK)
        self.assertLessEqual(len(body), render.MAX_BODY_CHARS)

    def test_a_body_is_never_empty(self):
        # Section 73's deterministic fallback: an unknown signal type must
        # still produce something rather than nothing.
        con = fx.db()
        row = sig(con, signal_type="something_new_and_unhandled",
                  severity="IMPORTANT", summary="未知の種別")
        subject, body = render.render(row, CLOCK)
        self.assertTrue(subject)
        self.assertTrue(body.strip())


if __name__ == "__main__":
    unittest.main()
