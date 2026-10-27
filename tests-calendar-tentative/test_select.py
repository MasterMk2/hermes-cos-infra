"""Which commitments become tentative entries, and why the others do not."""

from __future__ import annotations

import re
import unittest
from datetime import date, datetime, timedelta

from ct_fixtures import CALENDAR_ID, H1_PRIMARY_ID, JST, NOW, Workspace, make_cfg

import ct_select as sel


class SelectionCase(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.close)
        self.cfg = make_cfg()

    def select(self, cfg=None, *, index=True):
        return sel.select(self.ws.events, cfg=cfg or self.cfg, now=NOW,
                          index_con=self.ws.index if index else None)


class TimingTests(SelectionCase):
    def test_day_precision_deadline_is_an_all_day_entry_on_its_local_date(self):
        self.ws.add_commitment("c1", "協賛ロゴの入稿", due_at="2026-10-05T17:00:00+09:00",
                               due_precision="day")
        d = self.select().desired["c1"]
        self.assertEqual(d.kind, sel.KIND_DEADLINE)
        self.assertTrue(d.all_day)
        self.assertEqual(d.start, date(2026, 10, 5))
        self.assertEqual(d.end, date(2026, 10, 6))
        self.assertIsNone(d.deadline_time)
        body = sel.build_event_body(d, cfg=self.cfg)
        self.assertEqual(body["summary"], "【仮】〆 協賛ロゴの入稿")
        self.assertEqual(body["start"], {"date": "2026-10-05"})
        self.assertEqual(body["end"], {"date": "2026-10-06"})
        self.assertEqual(body["status"], "tentative")
        self.assertEqual(body["transparency"], "transparent")

    def test_a_known_clock_time_goes_into_the_deadline_title(self):
        self.ws.add_commitment("c1", "見積書の送付", due_at="2026-10-05T17:30:00+09:00",
                               due_precision="minute")
        d = self.select().desired["c1"]
        self.assertTrue(d.all_day)
        self.assertEqual(d.deadline_time, "17:30")
        self.assertEqual(sel.summary_for(d, "【仮】"), "【仮】〆17:30 見積書の送付")

    def test_an_appointment_with_a_time_is_a_timed_entry(self):
        self.ws.add_commitment("c1", "定例ミーティング", due_at="2026-10-03T18:00:00+09:00",
                               due_precision="exact_datetime", raw_type="appointment_candidate")
        d = self.select().desired["c1"]
        self.assertEqual(d.kind, sel.KIND_APPOINTMENT)
        self.assertFalse(d.all_day)
        body = sel.build_event_body(d, cfg=self.cfg)
        self.assertEqual(body["start"], {"dateTime": "2026-10-03T18:00:00+09:00",
                                         "timeZone": "Asia/Tokyo"})
        self.assertEqual(body["end"]["dateTime"], "2026-10-03T19:00:00+09:00")
        self.assertEqual(body["summary"], "【仮】定例ミーティング")

    def test_an_appointment_with_only_a_day_is_all_day_without_a_deadline_mark(self):
        self.ws.add_commitment("c1", "学科交流会", due_at="2026-10-03T00:00:00+09:00",
                               due_precision="date", raw_type="appointment_candidate")
        d = self.select().desired["c1"]
        self.assertEqual((d.kind, d.all_day), (sel.KIND_APPOINTMENT, True))
        self.assertEqual(sel.summary_for(d, "【仮】"), "【仮】学科交流会")

    def test_a_utc_timestamp_lands_on_the_local_date(self):
        # 16:00Z on the 4th is 01:00 on the 5th in Tokyo.
        self.ws.add_commitment("c1", "提出物", due_at="2026-10-04T16:00:00+00:00",
                               due_precision="minute")
        d = self.select().desired["c1"]
        self.assertEqual(d.start, date(2026, 10, 5))
        self.assertEqual(d.deadline_time, "01:00")

    def test_a_scheduled_slot_wins_over_a_due_date(self):
        self.ws.add_commitment("c1", "打ち合わせ", due_at="2026-10-09T17:00:00+09:00",
                               due_precision="day",
                               scheduled_at="2026-10-08T10:00:00+09:00",
                               scheduled_end_at="2026-10-08T10:30:00+09:00")
        d = self.select().desired["c1"]
        self.assertEqual(d.kind, sel.KIND_SCHEDULED)
        self.assertEqual(d.start, datetime(2026, 10, 8, 10, 0, tzinfo=JST))
        self.assertEqual(d.end, datetime(2026, 10, 8, 10, 30, tzinfo=JST))

    def test_time_is_never_guessed(self):
        for i, precision in enumerate(("week", "month", "daypart", "approximate", "unknown", None)):
            self.ws.add_commitment(f"c{i}", f"件名{i}", due_at="2026-10-05T17:00:00+09:00",
                                   due_precision=precision)
        self.ws.add_commitment("nodate", "日付なし")
        s = self.select()
        self.assertEqual(s.desired, {})
        for i in range(6):
            self.assertEqual(s.skipped[f"c{i}"], sel.R_IMPRECISE_TIME)
        self.assertEqual(s.skipped["nodate"], sel.R_NO_DATE)

    def test_past_and_beyond_horizon_are_skipped_but_today_is_kept(self):
        self.ws.add_commitment("past", "過去", due_at="2026-09-30T17:00:00+09:00", due_precision="day")
        self.ws.add_commitment("today", "今日締切", due_at="2026-10-01T08:00:00+09:00",
                               due_precision="day")
        self.ws.add_commitment("earlier", "今朝の予定", due_at="2026-10-01T08:00:00+09:00",
                               due_precision="minute", raw_type="appointment_candidate")
        far = (NOW + timedelta(days=61)).isoformat()
        self.ws.add_commitment("far", "遠い先", due_at=far, due_precision="day")
        s = self.select()
        self.assertEqual(set(s.desired), {"today"})
        self.assertEqual(s.skipped["past"], sel.R_PAST)
        self.assertEqual(s.skipped["earlier"], sel.R_PAST)
        self.assertEqual(s.skipped["far"], sel.R_BEYOND_HORIZON)


class EligibilityTests(SelectionCase):
    DUE = {"due_at": "2026-10-05T17:00:00+09:00", "due_precision": "day"}

    def test_only_the_owners_own_work_is_proposed(self):
        self.ws.add_commitment("self", "自分の作業", **self.DUE)
        self.ws.add_commitment("cp", "相手の作業", responsibility="COUNTERPARTY", **self.DUE)
        self.ws.add_commitment("wait", "返事待ち", work_state="WAITING", **self.DUE)
        self.ws.add_commitment("blk", "保留中", work_state="BLOCKED", **self.DUE)
        self.ws.add_commitment("info", "お知らせ", responsibility="INFORMATIONAL", **self.DUE)
        s = self.select()
        self.assertEqual(set(s.desired), {"self"})
        self.assertEqual(s.skipped["cp"], sel.R_NOT_OWNER_TASK)
        self.assertEqual(s.skipped["wait"], sel.R_NOT_OWNER_TASK)
        self.assertEqual(s.skipped["blk"], sel.R_NOT_OWNER_TASK)
        self.assertEqual(s.skipped["info"], sel.R_INFORMATIONAL)

    def test_resolved_and_superseded_are_never_proposed(self):
        for state in ("DONE", "CANCELLED", "DISMISSED"):
            self.ws.add_commitment(state, state, work_state=state, **self.DUE)
        self.ws.add_commitment("sup", "置き換え済み", currentness="superseded", **self.DUE)
        s = self.select()
        self.assertEqual(s.desired, {})
        self.assertEqual({s.skipped[k] for k in ("DONE", "CANCELLED", "DISMISSED")},
                         {sel.R_RESOLVED})
        self.assertEqual(s.skipped["sup"], sel.R_SUPERSEDED)

    def test_gmail_only_rows_pass_the_mail_screen_first(self):
        self.ws.add_commitment("mail", "メール由来", sources=[("gmail", "m-1")], **self.DUE)
        self.ws.add_commitment("both", "両方", sources=[("gmail", "m-2"), ("line", "l-2")],
                               **self.DUE)
        # No Gmail index given: mail-only rows are held back, mixed ones are not.
        s = self.select()
        self.assertEqual(set(s.desired), {"both"})
        self.assertEqual(s.skipped["mail"], sel.R_GMAIL_UNSCREENED)
        s = self.select(make_cfg(selection={"gmail_only": "off"}))
        self.assertEqual(s.skipped["mail"], sel.R_GMAIL_ONLY)
        s = self.select(make_cfg(selection={"gmail_only": "all"}))
        self.assertEqual(set(s.desired), {"mail", "both"})

    def test_shared_needs_chat_evidence_even_when_gmail_is_allowed(self):
        self.ws.add_commitment("sh", "サークル合宿の準備", responsibility="SHARED", **self.DUE)
        self.ws.add_commitment("shm", "キャンペーン", responsibility="SHARED",
                               sources=[("gmail", "m-9")], **self.DUE)
        s = self.select(make_cfg(selection={"gmail_only": "all"}))
        self.assertIn("sh", s.desired)
        self.assertEqual(s.skipped["shm"], sel.R_SHARED_WITHOUT_CHAT)
        s = self.select(make_cfg(selection={"include_shared": False}))
        self.assertEqual(s.skipped["sh"], sel.R_SHARED_EXCLUDED)

    def test_what_calendar_already_holds_is_not_proposed_again(self):
        self.ws.add_commitment("auth", "授業", authority="calendar_authority",
                               scheduled_at="2026-10-05T09:00:00+00:00",
                               sources=[("calendar", "gcal:x:1")])
        self.ws.add_commitment("ev", "合宿", sources=[("discord", "d-1"), ("calendar", "gcal:x:2")],
                               **self.DUE)
        s = self.select()
        self.assertEqual(s.desired, {})
        self.assertEqual(s.skipped["auth"], sel.R_ALREADY_ON_CALENDAR)
        self.assertEqual(s.skipped["ev"], sel.R_ALREADY_ON_CALENDAR)

    def test_low_confidence_is_skipped(self):
        self.ws.add_commitment("low", "推測", confidence=0.6, **self.DUE)
        self.assertEqual(self.select().skipped["low"], sel.R_LOW_CONFIDENCE)


class ExistingEventTests(SelectionCase):
    def test_same_title_on_the_same_day_is_already_on_the_calendar(self):
        self.ws.add_commitment("c1", "アルバム 個人ページ入稿", due_at="2026-10-05T17:00:00+09:00",
                               due_precision="day")
        self.ws.add_index_event("e1", "【締切】アルバム　個人ページ入稿", "2026-10-05", all_day=True)
        s = self.select()
        self.assertEqual(s.skipped["c1"], sel.R_MATCHES_EXISTING)

    def test_a_timed_match_needs_the_same_time_within_tolerance(self):
        self.ws.add_commitment("near", "面談", due_at="2026-10-03T15:00:00+09:00",
                               due_precision="minute", raw_type="appointment_candidate")
        self.ws.add_commitment("off", "相談", due_at="2026-10-03T15:00:00+09:00",
                               due_precision="minute", raw_type="appointment_candidate")
        self.ws.add_index_event("e1", "面談", "2026-10-03T06:03:00+00:00")  # 15:03 JST
        self.ws.add_index_event("e2", "相談", "2026-10-03T07:00:00+00:00")  # 16:00 JST
        s = self.select()
        self.assertEqual(s.skipped["near"], sel.R_MATCHES_EXISTING)
        self.assertIn("off", s.desired)

    def test_similar_titles_are_not_the_same_event(self):
        self.ws.add_commitment("c1", "資料作成", due_at="2026-10-05T17:00:00+09:00",
                               due_precision="day")
        self.ws.add_index_event("e1", "資料作成の打ち合わせ", "2026-10-05", all_day=True)
        self.assertIn("c1", self.select().desired)

    def test_cancelled_events_and_our_own_entries_do_not_count(self):
        at = "2026-10-05T18:00:00+09:00"
        self.ws.add_commitment("c1", "定例ミーティング", due_at=at, due_precision="minute",
                               raw_type="appointment_candidate")
        self.ws.add_index_event("e1", "定例ミーティング", at, status="cancelled")
        # Our own entry, as H.1 sees it on the primary calendar. Its title
        # matches once the 【仮】 tag is dropped; its id says whose it is.
        own = sel.event_id_for(CALENDAR_ID, "c1", 1)
        self.ws.add_index_event(own, "【仮】定例ミーティング", at, calendar_id=H1_PRIMARY_ID)
        self.assertIn("c1", self.select().desired)

    def test_a_real_event_on_the_primary_calendar_still_counts(self):
        self.ws.add_commitment("c1", "発表練習", due_at="2026-10-05T17:00:00+09:00",
                               due_precision="day")
        self.ws.add_index_event("abc123", "発表練習", "2026-10-05", all_day=True,
                                calendar_id=H1_PRIMARY_ID)
        self.assertEqual(self.select().skipped["c1"], sel.R_MATCHES_EXISTING)

    def test_without_an_index_the_check_is_simply_off(self):
        self.ws.add_commitment("c1", "発表練習", due_at="2026-10-05T17:00:00+09:00",
                               due_precision="day")
        self.ws.add_index_event("e1", "発表練習", "2026-10-05", all_day=True)
        s = self.select(index=False)
        self.assertIn("c1", s.desired)
        self.assertFalse(s.index_available)


class IdentityTests(SelectionCase):
    def test_two_commitments_citing_one_message_become_one_entry(self):
        due = {"due_at": "2026-10-05T17:00:00+09:00", "due_precision": "day"}
        self.ws.add_commitment("a", "請求書の確認", sources=[("discord", "same")], **due)
        self.ws.add_commitment("b", "請求書を確認する", sources=[("discord", "same"), ("line", "l")],
                               **due)
        s = self.select()
        self.assertEqual(set(s.desired), {"b"})
        self.assertEqual(s.skipped["a"], sel.R_DUPLICATE_SOURCE)

    def test_event_ids_are_deterministic_and_valid_for_google(self):
        one = sel.event_id_for(CALENDAR_ID, "cmt_1", 1)
        self.assertEqual(one, sel.event_id_for(CALENDAR_ID, "cmt_1", 1))
        self.assertNotEqual(one, sel.event_id_for(CALENDAR_ID, "cmt_1", 2))
        self.assertNotEqual(one, sel.event_id_for("other@group.calendar.google.com", "cmt_1", 1))
        self.assertTrue(one.startswith(sel.OWN_EVENT_PREFIX))
        self.assertTrue(sel.is_own_event(one))
        self.assertTrue(sel.is_own_event(f"gcal:{H1_PRIMARY_ID}:{one}"))
        self.assertFalse(sel.is_own_event(f"gcal:{H1_PRIMARY_ID}:abc123"))
        # Google: base32hex alphabet (0-9, a-v), length 5..1024.
        self.assertRegex(one, r"^[0-9a-v]{5,1024}$")

    def test_titles_are_single_line_and_bounded(self):
        self.ws.add_commitment("c1", "一行目\n‮二行目\t" + "あ" * 300,
                               due_at="2026-10-05T17:00:00+09:00", due_precision="day")
        d = self.select().desired["c1"]
        self.assertNotIn("\n", d.title)
        self.assertNotIn("‮", d.title)
        self.assertLessEqual(len(d.title), sel.TITLE_LIMIT)

    def test_description_says_where_it_came_from_and_how_to_decide(self):
        self.ws.add_commitment("c1", "見積書の送付", due_at="2026-10-05T17:30:00+09:00",
                               due_precision="minute", counterparty="ベンダA",
                               sources=[("discord", "d"), ("gmail", "g")])
        text = sel.description_for(self.select().desired["c1"], "【仮】")
        self.assertIn("Discord・Gmail", text)
        self.assertIn("期限: 10/5(月) 17:30", text)
        self.assertIn("相手: ベンダA", text)
        self.assertIn("タイトル先頭の【仮】を消し", text)
        self.assertIn("ref: c1", text)

    def test_normalize_title_strips_tags_and_space_only(self):
        self.assertEqual(sel.normalize_title("【締切】 [仮] Ａｂｃ　ｄ"), "abcd")
        self.assertNotEqual(sel.normalize_title("資料作成"), sel.normalize_title("資料"))

    def test_revision_changes_with_meaning_not_with_key_order(self):
        body = {"summary": "a", "start": {"date": "2026-10-05"}}
        same = {"start": {"date": "2026-10-05"}, "summary": "a"}
        self.assertEqual(sel.content_revision(body), sel.content_revision(same))
        self.assertNotEqual(sel.content_revision(body),
                            sel.content_revision({**body, "summary": "b"}))
        self.assertTrue(re.fullmatch(r"[0-9a-f]{32}", sel.content_revision(body)))


if __name__ == "__main__":
    unittest.main()
