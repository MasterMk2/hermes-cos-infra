"""The Gmail screen: mail from people in, newsletters and robots out."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from ct_fixtures import NOW, OWNER_ADDRESS, FakeCalendar, Workspace, make_cfg

import ct_engine
import ct_mail as cm
import ct_select as sel
import ct_store as store
from ct_config import ConfigError, load_config

DUE = {"due_at": "2026-10-05T17:00:00+09:00", "due_precision": "day"}
UNCATEGORIZED = ("INBOX", "UNREAD")


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

    def assertSkipped(self, cid, reason, *modes):
        for mode in modes or ("people", "people_and_notifications"):
            with self.subTest(mode=mode):
                self.assertEqual(self.select(mode).skipped.get(cid), reason)

    def assertBasis(self, cid, basis, mode="people"):
        self.assertEqual(self.select(mode).desired[cid].mail_basis, basis)


class WhatTheOwnerDidTests(ScreenCase):
    def test_mail_the_owner_sent_passes(self):
        cid = self.mail_commitment(is_sent=True, to="client@example.org")
        d = self.select().desired[cid]
        self.assertEqual(d.mail_basis, cm.BASIS_SENT)
        self.assertIn("根拠: あなたが送ったメール", sel.description_for(d))

    def test_a_reply_after_the_owner_wrote_passes_whatever_gmail_thinks(self):
        self.ws.add_mail("gmail:ask", is_sent=True, to="shop@example.org", thread_id="t-shop")
        cid = self.mail_commitment(sender="shop@example.org", thread_id="t-shop",
                                   labels=("INBOX", "CATEGORY_PROMOTIONS"))
        self.assertBasis(cid, cm.BASIS_THREAD)

    def test_replying_to_a_newsletter_does_not_admit_the_newsletter(self):
        # Many mail magazines say: reply to this mail to unsubscribe.
        cid = self.mail_commitment(sender="ABCストア <shop@abc.example>", thread_id="t-mag",
                                   labels=("INBOX", "CATEGORY_PROMOTIONS"))
        self.ws.add_mail("gmail:unsub", is_sent=True, to="shop@abc.example", thread_id="t-mag")
        self.assertSkipped(cid, cm.R_GMAIL_BULK)

    def test_replying_to_a_robot_does_not_make_it_a_correspondent(self):
        self.ws.add_mail("gmail:unsub", is_sent=True, to="magazine@abc.example")
        cid = self.mail_commitment(sender="magazine@abc.example",
                                   labels=("INBOX", "CATEGORY_UPDATES"))
        self.assertSkipped(cid, cm.R_GMAIL_NOTIFICATION, "people")

    def test_a_sender_the_owner_has_written_to_passes(self):
        self.ws.add_mail("gmail:out", is_sent=True, to="Office <office@example.org>",
                         cc="ベンダA <vendor-a@example.net>")
        a = self.mail_commitment(sender="Office <OFFICE@example.org>", labels=UNCATEGORIZED)
        b = self.mail_commitment(sender="vendor-a@example.net", labels=("INBOX", "CATEGORY_UPDATES"))
        s = self.select()
        self.assertEqual(s.desired[a].mail_basis, cm.BASIS_CORRESPONDENT)
        self.assertEqual(s.desired[b].mail_basis, cm.BASIS_CORRESPONDENT)

    def test_mail_to_oneself_does_not_make_oneself_a_correspondent(self):
        self.ws.add_mail("gmail:note", is_sent=True, to=OWNER_ADDRESS)
        cid = self.mail_commitment(sender=OWNER_ADDRESS, labels=("INBOX", "CATEGORY_UPDATES"))
        self.assertSkipped(cid, cm.R_GMAIL_NOTIFICATION, "people")

    def test_a_draft_is_never_evidence(self):
        self.ws.add_mail("gmail:out", is_sent=True, to="prof@example.ac.jp", thread_id="t-1")
        live = self.mail_commitment(sender=OWNER_ADDRESS, to="prof@example.ac.jp",
                                    thread_id="t-1", labels=("DRAFT",))
        gone = self.mail_commitment(sender=OWNER_ADDRESS, labels=("DRAFT",), deleted=True)
        self.assertSkipped(live, cm.R_GMAIL_DRAFT)
        self.assertSkipped(gone, cm.R_GMAIL_DRAFT)


class WhatGmailDecidedTests(ScreenCase):
    def test_promotions_and_social_are_bulk_even_from_a_known_shop(self):
        self.ws.add_mail("gmail:asked", is_sent=True, to="shop@example.org")
        ids = [self.mail_commitment(sender="shop@example.org", labels=("INBOX", label))
               for label in ("CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL")]
        for cid in ids:
            self.assertSkipped(cid, cm.R_GMAIL_BULK)

    def test_mailing_list_mail_from_a_stranger_stays_out(self):
        cid = self.mail_commitment(sender="member@example.ac.jp", to="seminar@lists.example.ac.jp",
                                   labels=("INBOX", "CATEGORY_FORUMS"))
        self.assertSkipped(cid, cm.R_GMAIL_LIST)

    def test_mailing_list_mail_from_someone_the_owner_writes_to_passes(self):
        self.ws.add_mail("gmail:out", is_sent=True, to="sato@example.ac.jp")
        cid = self.mail_commitment(sender="sato@example.ac.jp", to="seminar@lists.example.ac.jp",
                                   labels=("INBOX", "CATEGORY_FORUMS"))
        self.assertBasis(cid, cm.BASIS_CORRESPONDENT)

    def test_a_list_the_owner_has_posted_to_passes_even_with_a_rewritten_sender(self):
        self.ws.add_mail("gmail:post", is_sent=True, to="festival-2026@groups.example.com")
        rewritten = self.mail_commitment(
            sender="'山田太郎' via 医薬祭2026 <festival-2026@groups.example.com>",
            to="festival-2026@groups.example.com", labels=("INBOX", "CATEGORY_FORUMS"))
        member = self.mail_commitment(sender="hanako@example.ac.jp",
                                      to="festival-2026@groups.example.com",
                                      labels=("INBOX", "CATEGORY_FORUMS"))
        s = self.select()
        self.assertEqual(s.desired[rewritten].mail_basis, cm.BASIS_CORRESPONDENT)
        self.assertEqual(s.desired[member].mail_basis, cm.BASIS_CORRESPONDENT)

    def test_updates_wait_for_the_wider_mode(self):
        cid = self.mail_commitment(sender="reserve@clinic.example", labels=("INBOX", "CATEGORY_UPDATES"))
        self.assertSkipped(cid, cm.R_GMAIL_NOTIFICATION, "people")
        d = self.select("people_and_notifications").desired[cid]
        self.assertEqual(d.mail_basis, cm.BASIS_NOTIFICATION)
        self.assertIn("根拠: 自動通知のメール", sel.description_for(d))

    def test_primary_mail_from_a_person_passes(self):
        cid = self.mail_commitment(sender="山田太郎 <taro@example.org>",
                                   labels=("INBOX", "CATEGORY_PERSONAL", "IMPORTANT"))
        d = self.select().desired[cid]
        self.assertEqual(d.mail_basis, cm.BASIS_PERSONAL)
        self.assertIn("根拠: Gmail がメイン（個人）に分類したメール", sel.description_for(d))

    def test_without_gmail_categories_a_stranger_never_passes(self):
        # Google turns smart features (and with them categories) off by
        # default for accounts in Japan.
        person = self.mail_commitment(sender="taro@example.org", labels=UNCATEGORIZED)
        newsletter = self.mail_commitment(sender="shop-mail@ml.example-shop.jp",
                                          labels=UNCATEGORIZED)
        self.assertSkipped(person, cm.R_GMAIL_UNCATEGORIZED)
        self.assertSkipped(newsletter, cm.R_GMAIL_UNCATEGORIZED)

    def test_without_gmail_categories_what_the_owner_did_still_counts(self):
        self.ws.add_mail("gmail:out", is_sent=True, to="prof@example.ac.jp", labels=("SENT",))
        cid = self.mail_commitment(sender="prof@example.ac.jp", labels=UNCATEGORIZED)
        self.assertBasis(cid, cm.BASIS_CORRESPONDENT)

    def test_spam_trash_and_deleted_mail_never_pass(self):
        ids = [self.mail_commitment(labels=("SPAM",)), self.mail_commitment(labels=("TRASH",)),
               self.mail_commitment(deleted=True)]
        for cid in ids:
            self.assertSkipped(cid, cm.R_GMAIL_DISCARDED)

    def test_categorisation_is_reported(self):
        self.mail_commitment(labels=UNCATEGORIZED)
        self.ws.add_mail("gmail:x", labels=("INBOX", "CATEGORY_PERSONAL"))
        self.ws.add_mail("gmail:sent", is_sent=True, to="a@example.org")
        self.ws.add_mail("gmail:draft", labels=("DRAFT",))
        self.assertEqual(self.select().gmail_categorized, (2, 1))


class HeaderTests(ScreenCase):
    def test_blind_copy_blasts_wait_for_the_wider_mode(self):
        shapes = ("", "undisclosed-recipients:;", "Salon <salon@example.org>")
        ids = [self.mail_commitment(sender="Salon <salon@example.org>", to=to) for to in shapes]
        for cid in ids:
            self.assertSkipped(cid, cm.R_GMAIL_BROADCAST, "people")
            self.assertBasis(cid, cm.BASIS_BROADCAST, "people_and_notifications")

    def test_a_blind_copy_from_a_correspondent_passes(self):
        self.ws.add_mail("gmail:out", is_sent=True, to="club-lead@example.org")
        cid = self.mail_commitment(sender="club-lead@example.org", to="")
        self.assertBasis(cid, cm.BASIS_CORRESPONDENT)

    def test_automated_local_parts(self):
        for address in ("no-reply@accounts.example.com", "noreply@example.com",
                        "do-not-reply@example.com", "donotreply@example.com",
                        "notifications+abc123@example.com", "notification@example.com",
                        "newsletter@example.com", "news@example.com", "mailmag@example.com",
                        "mail-magazine@example.com", "marketing@example.com",
                        "MAILER-DAEMON@example.com", "alerts@example.com",
                        "store-news@example.com", "jp-newsletter@example.com",
                        "shop_news@example.com", "univcoop-news@example.ac.jp",
                        "merumaga@example.jp", "melmaga@example.jp", "shop.noreply@example.com",
                        "hello@news.example.com", "hello@mailer.example.com"):
            with self.subTest(address=address):
                self.assertTrue(cm.is_automated_sender(address))

    def test_people_and_ordinary_mail_hosts_are_not_automated(self):
        for address in ("taro@example.org", "office@mail.example.ac.jp", "info@example.org",
                        "newsroom-desk@example.org", "noriko@example.org", "notes@example.org",
                        "support@example.org", "magnus@example.org", "newsome@example.org", ""):
            with self.subTest(address=address):
                self.assertFalse(cm.is_automated_sender(address))

    def test_an_automated_sender_in_primary_is_still_a_notification(self):
        cid = self.mail_commitment(sender="ABCストア <store-news@abc.example>")
        self.assertSkipped(cid, cm.R_GMAIL_NOTIFICATION, "people")

    def test_display_names_that_strict_parsing_rejects_still_yield_the_address(self):
        self.assertEqual(cm.address_of("ABC Store [Official] <news@abc.example>"), "news@abc.example")
        self.assertEqual(cm.address_of("ABC Store, Shibuya <do-not-reply@abc.example>"),
                         "do-not-reply@abc.example")
        self.assertEqual(cm.address_of("tanaka@example.org <tanaka@example.org>"),
                         "tanaka@example.org")
        cid = self.mail_commitment(sender="ABC Store [Official] <news@abc.example>")
        self.assertSkipped(cid, cm.R_GMAIL_NOTIFICATION, "people")

    def test_one_malformed_recipient_does_not_erase_the_others(self):
        self.ws.add_mail("gmail:out", is_sent=True, to="田中 <tanaka@example.ac.jp>",
                         cc="A社 [採用] <saiyo@a.example.co.jp>")
        a = self.mail_commitment(sender="tanaka@example.ac.jp", labels=("INBOX", "CATEGORY_UPDATES"))
        b = self.mail_commitment(sender="saiyo@a.example.co.jp", labels=("INBOX", "CATEGORY_UPDATES"))
        s = self.select()
        self.assertEqual(s.desired[a].mail_basis, cm.BASIS_CORRESPONDENT)
        self.assertEqual(s.desired[b].mail_basis, cm.BASIS_CORRESPONDENT)

    def test_a_message_without_a_readable_sender_is_unscreened(self):
        # H.2's placeholder for a message it could not re-fetch has From ''.
        cid = self.mail_commitment(sender="", labels=())
        self.assertSkipped(cid, cm.R_GMAIL_UNSCREENED)

    def test_the_senders_address_never_reaches_the_calendar(self):
        self.ws.add_mail("gmail:p", sender="山田 <yamada@example.org>")
        self.ws.add_commitment("c", "見積書の確認", sources=[("gmail", "gmail:p")],
                               counterparty="山田 <yamada@example.org>", **DUE)
        text = sel.description_for(self.select().desired["c"])
        self.assertNotIn("@", text)
        self.assertNotIn("相手:", text)
        self.assertIsNone(sel.shown_counterparty("U" + "0123456789abcdef" * 2))  # privacy-ok
        self.assertEqual(sel.shown_counterparty("ベンダA"), "ベンダA")


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
        self.assertEqual(self.select().skipped[cid], cm.R_GMAIL_UNSCREENED)

    def test_chat_evidence_bypasses_the_screen(self):
        self.ws.add_mail("gmail:promo", labels=("INBOX", "CATEGORY_PROMOTIONS"))
        self.ws.add_commitment("mixed", "協賛ロゴの入稿",
                               sources=[("gmail", "gmail:promo"), ("discord", "d-1")], **DUE)
        s = self.select()
        self.assertIn("mixed", s.desired)
        self.assertIsNone(s.desired["mixed"].mail_basis)

    def test_bare_off_in_yaml_means_off_and_bare_yes_is_refused(self):
        path = os.path.join(self.ws.dir, "c.yaml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("selection:\n  gmail_only: off\n")
        self.assertEqual(load_config(path).selection["gmail_only"], "off")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("selection:\n  gmail_only: yes\n")
        with self.assertRaises(ConfigError):
            load_config(path)


class MultiMessageTests(ScreenCase):
    def test_any_passing_message_is_enough_and_the_strongest_basis_is_reported(self):
        self.ws.add_mail("gmail:out", is_sent=True, to="taro@example.org")
        cid = self.mail_commitment(mails=[
            {"labels": ("INBOX", "CATEGORY_PROMOTIONS")},
            {"sender": "taro@example.org"},
            {"sender": "hanako@example.org"},
        ])
        self.assertBasis(cid, cm.BASIS_CORRESPONDENT)

    def test_when_all_fail_the_nearest_miss_is_reported(self):
        cid = self.mail_commitment(mails=[
            {"labels": ("INBOX", "CATEGORY_PROMOTIONS")},
            {"sender": "no-reply@example.com"},
            {"labels": UNCATEGORIZED},
            None,
        ])
        self.assertEqual(self.select().skipped[cid], cm.R_GMAIL_NOTIFICATION)


class DraftWithdrawalTests(ScreenCase):
    def test_an_entry_built_on_a_draft_is_withdrawn(self):
        state = store.open_db(self.ws.state_path)
        self.addCleanup(state.close)
        google = FakeCalendar()
        cid = self.mail_commitment(sender=OWNER_ADDRESS, to="prof@example.ac.jp")

        def run(mode):
            cfg = make_cfg(selection={"gmail_only": mode})
            selection = sel.select(self.ws.events, cfg=cfg, now=NOW, index_con=self.ws.index,
                                   gmail_con=self.ws.gmail)
            return ct_engine.run(cfg=cfg, state_con=state, selection=selection, client=google,
                                 now=NOW)

        run("people")
        self.assertEqual(store.get(state, cid)["state"], store.ACTIVE)
        # It turns out the evidence was the owner's unsent draft.
        self.ws.relabel_mail(f"gmail:m{self.n}x0", ("DRAFT",))
        run("people")
        self.assertEqual(store.get(state, cid)["state"], store.WITHDRAWN)


class BoundaryTests(ScreenCase):
    def test_bodies_are_never_read(self):
        """The screen's SQL names its columns; none of them is content."""
        for sql in (cm._MESSAGE_SQL, cm._SENT_SQL, cm._CATEGORY_SQL):
            for column in ("body_text", "snippet", "commitment_text", "subject"):
                self.assertNotIn(column, sql)
            self.assertNotRegex(sql, r"(?i)select\s+\*")

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
