"""The Gmail screen: mail from people in, newsletters and robots out."""

from __future__ import annotations

import unittest
from unittest import mock

from ct_fixtures import NOW, OWNER_ADDRESS, Workspace, make_cfg

import ct_mail as cm
import ct_select as sel

DUE = {"due_at": "2026-10-05T17:00:00+09:00", "due_precision": "day"}


class ScreenCase(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.close)
        self.n = 0

    def mail_commitment(self, title="書類の提出", *, mails=None, **mail):
        """A Gmail-only SELF commitment backed by one (or more) indexed mails."""
        self.n += 1
        cid = f"c{self.n}"
        mails = mails or [mail]
        sources = []
        for i, spec in enumerate(mails):
            source_id = f"gmail:m{self.n}x{i}"
            if spec is not None:
                self.ws.add_mail(source_id, **spec)
            sources.append(("gmail", source_id))
        self.ws.add_commitment(cid, title, sources=sources, **DUE)
        return cid

    def select(self, mode="people", *, gmail=True):
        return sel.select(self.ws.events, cfg=make_cfg(selection={"gmail_only": mode}), now=NOW,
                          index_con=self.ws.index, gmail_con=self.ws.gmail if gmail else None)


class WhatTheOwnerDidTests(ScreenCase):
    def test_mail_the_owner_sent_passes(self):
        cid = self.mail_commitment(is_sent=True, to="client@example.org")
        d = self.select().desired[cid]
        self.assertEqual(d.mail_basis, cm.BASIS_SENT)
        self.assertIn("根拠: あなたが送ったメール", sel.description_for(d))

    def test_a_thread_the_owner_wrote_in_passes_even_if_gmail_calls_it_promotions(self):
        self.ws.add_mail("gmail:reply", is_sent=True, to="shop@example.org", thread_id="t-shop")
        cid = self.mail_commitment(sender="shop@example.org", thread_id="t-shop",
                                   labels=("INBOX", "CATEGORY_PROMOTIONS"))
        self.assertEqual(self.select().desired[cid].mail_basis, cm.BASIS_THREAD)

    def test_a_sender_the_owner_has_written_to_passes(self):
        self.ws.add_mail("gmail:out", is_sent=True, to="Office <office@example.org>",
                         cc="ベンダA <vendor-a@example.net>")
        a = self.mail_commitment(sender="Office <OFFICE@example.org>")
        b = self.mail_commitment(sender="vendor-a@example.net", labels=("INBOX", "CATEGORY_UPDATES"))
        s = self.select()
        self.assertEqual(s.desired[a].mail_basis, cm.BASIS_CORRESPONDENT)
        # Updates from a correspondent is still correspondence.
        self.assertEqual(s.desired[b].mail_basis, cm.BASIS_CORRESPONDENT)

    def test_mail_to_oneself_does_not_make_oneself_a_correspondent(self):
        # Notices are often addressed "from" the account they notify.
        self.ws.add_mail("gmail:note", is_sent=True, to=OWNER_ADDRESS)
        cid = self.mail_commitment(sender=OWNER_ADDRESS, labels=("INBOX", "CATEGORY_UPDATES"))
        self.assertEqual(self.select().skipped[cid], cm.R_GMAIL_NOTIFICATION)


class WhatGmailDecidedTests(ScreenCase):
    def test_promotions_social_and_forums_are_bulk(self):
        ids = [self.mail_commitment(labels=("INBOX", label)) for label in
               ("CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS")]
        s = self.select("people_and_notifications")
        for cid in ids:
            self.assertEqual(s.skipped[cid], cm.R_GMAIL_BULK)

    def test_a_known_shop_in_promotions_is_still_bulk(self):
        self.ws.add_mail("gmail:asked", is_sent=True, to="shop@example.org")
        cid = self.mail_commitment(sender="shop@example.org", labels=("INBOX", "CATEGORY_PROMOTIONS"))
        self.assertEqual(self.select().skipped[cid], cm.R_GMAIL_BULK)

    def test_updates_wait_for_the_wider_mode(self):
        cid = self.mail_commitment(sender="reserve@clinic.example", labels=("INBOX", "CATEGORY_UPDATES"))
        self.assertEqual(self.select().skipped[cid], cm.R_GMAIL_NOTIFICATION)
        d = self.select("people_and_notifications").desired[cid]
        self.assertEqual(d.mail_basis, cm.BASIS_NOTIFICATION)
        self.assertIn("根拠: 自動通知のメール", sel.description_for(d))

    def test_spam_trash_and_deleted_mail_never_pass(self):
        ids = [self.mail_commitment(labels=("SPAM",)), self.mail_commitment(labels=("TRASH",)),
               self.mail_commitment(deleted=True)]
        s = self.select("people_and_notifications")
        for cid in ids:
            self.assertEqual(s.skipped[cid], cm.R_GMAIL_DISCARDED)

    def test_personal_mail_from_a_human_passes(self):
        cid = self.mail_commitment(sender="山田太郎 <taro@example.org>",
                                   labels=("INBOX", "CATEGORY_PERSONAL", "IMPORTANT"))
        self.assertEqual(self.select().desired[cid].mail_basis, cm.BASIS_PERSONAL)

    def test_uncategorised_mail_from_a_human_passes(self):
        cid = self.mail_commitment(sender="taro@example.org", labels=("INBOX",))
        self.assertEqual(self.select().desired[cid].mail_basis, cm.BASIS_PERSONAL)


class AutomatedSenderTests(ScreenCase):
    def test_automated_local_parts(self):
        for address in ("no-reply@accounts.example.com", "noreply@example.com",
                        "do-not-reply@example.com", "donotreply@example.com",
                        "notifications+abc123@example.com", "notification@example.com",
                        "newsletter@example.com", "news@example.com", "mailmag@example.com",
                        "mail-magazine@example.com", "marketing@example.com",
                        "MAILER-DAEMON@example.com", "alerts@example.com"):
            with self.subTest(address=address):
                self.assertTrue(cm.is_automated_sender(address))

    def test_automated_sending_hosts(self):
        self.assertTrue(cm.is_automated_sender("hello@news.example.com"))
        self.assertTrue(cm.is_automated_sender("hello@mailer.example.com"))

    def test_people_and_ordinary_mail_hosts_are_not_automated(self):
        for address in ("taro@example.org", "office@mail.example.ac.jp", "info@example.org",
                        "newsroom-desk@example.org", "noriko@example.org", "notes@example.org",
                        "support@example.org", ""):
            with self.subTest(address=address):
                self.assertFalse(cm.is_automated_sender(address))

    def test_an_automated_sender_without_a_category_is_a_notification(self):
        cid = self.mail_commitment(sender="Shop <no-reply@shop.example>", labels=("INBOX",))
        self.assertEqual(self.select().skipped[cid], cm.R_GMAIL_NOTIFICATION)


class ModeTests(ScreenCase):
    def test_off_and_all(self):
        bulk = self.mail_commitment(labels=("INBOX", "CATEGORY_PROMOTIONS"))
        human = self.mail_commitment(sender="taro@example.org")
        off = self.select("off")
        self.assertEqual(off.skipped[bulk], sel.R_GMAIL_ONLY)
        self.assertEqual(off.skipped[human], sel.R_GMAIL_ONLY)
        everything = self.select("all")
        self.assertIn(bulk, everything.desired)
        self.assertIsNone(everything.desired[bulk].mail_basis)

    def test_no_index_means_nothing_mail_only_is_proposed(self):
        cid = self.mail_commitment(is_sent=True, to="client@example.org")
        s = self.select(gmail=False)
        self.assertEqual(s.skipped[cid], cm.R_GMAIL_UNSCREENED)
        self.assertFalse(s.gmail_screen_available)
        self.assertIn(cid, self.select("all", gmail=False).desired)

    def test_a_message_missing_from_the_index_is_unscreened(self):
        cid = self.mail_commitment(mails=[None])
        self.assertEqual(self.select().skipped[cid], cm.R_GMAIL_UNSCREENED)

    def test_an_index_of_the_wrong_shape_is_not_trusted(self):
        cid = self.mail_commitment(is_sent=True, to="client@example.org")
        self.ws.gmail.execute("DROP TABLE gmail_messages")
        self.ws.gmail.execute("CREATE TABLE gmail_messages (source_id TEXT)")
        s = self.select()
        self.assertEqual(s.skipped[cid], cm.R_GMAIL_UNSCREENED)

    def test_chat_evidence_bypasses_the_screen(self):
        self.ws.add_mail("gmail:promo", labels=("INBOX", "CATEGORY_PROMOTIONS"))
        self.ws.add_commitment("mixed", "協賛ロゴの入稿",
                               sources=[("gmail", "gmail:promo"), ("discord", "d-1")], **DUE)
        s = self.select()
        self.assertIn("mixed", s.desired)
        self.assertIsNone(s.desired["mixed"].mail_basis)


class MultiMessageTests(ScreenCase):
    def test_any_passing_message_is_enough_and_the_strongest_basis_is_reported(self):
        self.ws.add_mail("gmail:out", is_sent=True, to="taro@example.org")
        cid = self.mail_commitment(mails=[
            {"labels": ("INBOX", "CATEGORY_PROMOTIONS")},
            {"sender": "taro@example.org"},
            {"sender": "hanako@example.org"},
        ])
        self.assertEqual(self.select().desired[cid].mail_basis, cm.BASIS_CORRESPONDENT)

    def test_when_all_fail_the_nearest_miss_is_reported(self):
        cid = self.mail_commitment(mails=[
            {"labels": ("INBOX", "CATEGORY_PROMOTIONS")},
            {"sender": "no-reply@example.com"},
            None,
        ])
        self.assertEqual(self.select().skipped[cid], cm.R_GMAIL_NOTIFICATION)


class BoundaryTests(ScreenCase):
    def test_bodies_are_never_read(self):
        """The screen's SQL names its columns; none of them is content."""
        for sql in (cm._MESSAGE_SQL, cm._SENT_SQL):
            for column in ("body_text", "snippet", "commitment_text", "subject", "*"):
                self.assertNotIn(column, sql)

    def test_only_open_self_rows_are_looked_up(self):
        self.mail_commitment(sender="taro@example.org")
        self.ws.add_mail("gmail:done", sender="x@example.org")
        self.ws.add_commitment("done", "済み", work_state="DONE",
                               sources=[("gmail", "gmail:done")], **DUE)
        self.ws.add_commitment("cp", "相手", responsibility="COUNTERPARTY",
                               sources=[("gmail", "gmail:cp")], **DUE)
        with mock.patch.object(cm, "load_facts", wraps=cm.load_facts) as spy:
            self.select()
        (_, looked_up), _ = spy.call_args
        self.assertEqual(set(looked_up), {"gmail:m1x0"})


if __name__ == "__main__":
    unittest.main()
