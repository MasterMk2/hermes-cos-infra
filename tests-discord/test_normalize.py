#!/usr/bin/env python3
"""D.1 unit tests — payload normalization (spec sections 100, 107, 109)."""

import json
import unittest

import dk_fixtures as fx
from dk_normalize import (
    channel_type_name,
    edit_generation,
    message_type_name,
    normalize_message,
    search_blob,
    snowflake_ms,
    tombstone_record,
)
from dk_schema import parse_source_id, source_id_for


def norm(payload, channel_id=None, guild_id=fx.GUILD_A, guild_name="Guild A", **kw):
    cid = channel_id or payload.get("channel_id") or fx.CHANNEL_A1
    return normalize_message(
        payload,
        guild_id=guild_id,
        guild_name=guild_name,
        channel=fx.CHANNEL_CTX[cid],
        **kw,
    )


class NormalMessage(unittest.TestCase):
    def test_normal_guild_message(self):
        rec = norm(fx.NORMAL)
        self.assertEqual(
            rec["source_id"],
            source_id_for(fx.GUILD_A, fx.CHANNEL_A1, fx.NORMAL["id"]),
        )
        self.assertEqual(rec["guild_id"], fx.GUILD_A)
        self.assertEqual(rec["guild_name"], "Guild A")
        self.assertEqual(rec["channel_id"], fx.CHANNEL_A1)
        self.assertEqual(rec["channel_name"], "general")
        self.assertEqual(rec["channel_type"], "guild_text")
        self.assertEqual(rec["author_id"], fx.OWNER_ID)
        self.assertEqual(rec["author_name"], "owner")
        self.assertEqual(rec["author_global_name"], "Owner")
        self.assertEqual(rec["author_is_bot"], 0)
        self.assertIsNone(rec["webhook_id"])
        self.assertEqual(rec["content"], "明日の資料をまとめておきます")
        self.assertEqual(rec["message_type"], "default")
        self.assertIsNone(rec["deleted_at"])
        self.assertIsNone(rec["edited_at"])
        self.assertIsNone(rec["thread_id"])

    def test_source_id_round_trips(self):
        rec = norm(fx.NORMAL)
        parsed = parse_source_id(rec["source_id"])
        self.assertEqual(parsed, (fx.GUILD_A, fx.CHANNEL_A1, fx.NORMAL["id"]))

    def test_timestamps_are_utc_iso(self):
        rec = norm(fx.NORMAL)
        self.assertTrue(rec["created_at"].endswith("+00:00"))
        self.assertEqual(rec["created_ts"], snowflake_ms(fx.NORMAL["id"]))

    def test_missing_guild_id_is_refused(self):
        with self.assertRaises(ValueError):
            normalize_message(
                fx.NORMAL, guild_id=None, channel=fx.CHANNEL_CTX[fx.CHANNEL_A1]
            )


class OtherShapes(unittest.TestCase):
    def test_reply_keeps_its_reference(self):
        rec = norm(fx.REPLY)
        self.assertEqual(rec["reply_to_message_id"], fx.REPLY_TARGET["id"])
        self.assertEqual(rec["message_type"], "reply")

    def test_bot_message_is_flagged_but_kept(self):
        rec = norm(fx.BOT_MESSAGE)
        self.assertEqual(rec["author_is_bot"], 1)
        self.assertEqual(rec["content"], "本日の予定は 3 件です")

    def test_webhook_message_records_the_webhook_id(self):
        rec = norm(fx.WEBHOOK_MESSAGE)
        self.assertEqual(rec["webhook_id"], "415555555555555555")
        self.assertEqual(rec["author_is_bot"], 1)

    def test_thread_message_carries_thread_and_parent(self):
        rec = norm(fx.THREAD_MESSAGE, channel_id=fx.THREAD_A)
        self.assertEqual(rec["channel_id"], fx.THREAD_A)
        self.assertEqual(rec["thread_id"], fx.THREAD_A)
        self.assertEqual(rec["thread_name"], "release-thread")
        self.assertEqual(rec["parent_channel_id"], fx.CHANNEL_A2)
        self.assertEqual(rec["channel_type"], "public_thread")

    def test_edited_message_carries_edit_time(self):
        payload = fx.message(content="修正後", edited=fx.at(2))
        rec = norm(payload)
        self.assertTrue(rec["edited_at"].endswith("+00:00"))
        self.assertGreater(edit_generation(rec), "")

    def test_missing_optional_fields_do_not_raise(self):
        rec = norm(fx.MINIMAL)
        self.assertEqual(rec["content"], "")
        self.assertIsNone(rec["author_global_name"])
        self.assertEqual(rec["author_is_bot"], 0)
        self.assertIsNone(rec["attachments_json"])
        self.assertIsNone(rec["embeds_json"])
        self.assertEqual(rec["message_type"], "default")
        self.assertIsNotNone(rec["created_ts"])

    def test_unknown_type_codes_degrade_readably(self):
        self.assertEqual(message_type_name(9999), "type_9999")
        self.assertEqual(channel_type_name(9999), "type_9999")
        self.assertEqual(message_type_name(None), "default")


class DeletedAndTombstone(unittest.TestCase):
    def test_tombstone_has_identity_and_no_body(self):
        rec = tombstone_record(fx.GUILD_A, fx.CHANNEL_A1, fx.NORMAL["id"])
        self.assertNotIn("content", rec)
        self.assertEqual(rec["message_id"], fx.NORMAL["id"])
        self.assertEqual(rec["guild_id"], fx.GUILD_A)
        self.assertEqual(rec["channel_id"], fx.CHANNEL_A1)
        self.assertIsNotNone(rec["deleted_at"])
        # created_at survives because the snowflake carries it, even though a
        # raw delete payload contains no timestamp at all.
        self.assertIsNotNone(rec["created_at"])


class BoundedMetadata(unittest.TestCase):
    def test_attachment_metadata_only_no_signed_urls(self):
        rec = norm(fx.WITH_ATTACHMENT)
        atts = json.loads(rec["attachments_json"])
        self.assertEqual(len(atts), 1)
        self.assertEqual(
            set(atts[0]), {"attachment_id", "filename", "content_type", "size"}
        )
        blob = rec["attachments_json"]
        self.assertNotIn("cdn.example.invalid", blob)
        self.assertNotIn("signed", blob)

    def test_embed_keeps_bounded_text_and_drops_urls(self):
        rec = norm(fx.BOT_MESSAGE)
        embeds = json.loads(rec["embeds_json"])
        self.assertEqual(len(embeds), 1)
        self.assertEqual(embeds[0]["title"], "本日の予定")
        self.assertIn("定例", embeds[0]["description"])
        self.assertEqual(embeds[0]["fields"][0]["name"], "場所")
        self.assertNotIn("should-not-be-stored", rec["embeds_json"])
        self.assertNotIn("image", embeds[0])

    def test_content_is_capped(self):
        payload = fx.message(content="あ" * 5000)
        rec = norm(payload, limits={"content_max_chars": 100})
        self.assertEqual(len(rec["content"]), 100)

    def test_embed_text_is_searchable_through_search_blob(self):
        rec = norm(fx.BOT_MESSAGE)
        blob = search_blob(rec)
        self.assertIn("本日の予定は 3 件です", blob)
        self.assertIn("会議室 A", blob)


class RawContentPreserved(unittest.TestCase):
    def test_raw_and_normalized_are_both_kept(self):
        names = {fx.CHANNEL_A2: "projects"}
        rec = norm(fx.MENTION_MESSAGE, channel_names=names)
        self.assertIn(f"<@{fx.OTHER_ID}>", rec["content"])
        # User names come from the payload's own `mentions` array, so no
        # member cache and no GUILD_MEMBERS intent is needed.
        self.assertIn("@Alice", rec["content_normalized"])
        self.assertIn("#projects", rec["content_normalized"])
        self.assertIn(":tada:", rec["content_normalized"])
        self.assertNotEqual(rec["content"], rec["content_normalized"])

    def test_unknown_channel_mention_keeps_the_id_rather_than_guessing(self):
        rec = norm(fx.MENTION_MESSAGE)
        self.assertIn(f"#{fx.CHANNEL_A2}", rec["content_normalized"])
        self.assertIn("@Alice", rec["content_normalized"])

    def test_content_hash_changes_with_content(self):
        a = norm(fx.message(content="A"))
        b = norm(fx.message(content="B"))
        self.assertNotEqual(a["content_hash"], b["content_hash"])


class PartialPayloads(unittest.TestCase):
    def test_partial_returns_only_present_fields(self):
        rec = normalize_message(
            fx.partial_edit(fx.NORMAL["id"], "書き換え後"),
            guild_id=fx.GUILD_A,
            guild_name="Guild A",
            channel=fx.CHANNEL_CTX[fx.CHANNEL_A1],
            partial=True,
        )
        self.assertEqual(rec["content"], "書き換え後")
        self.assertIn("edited_at", rec)
        # Absent from the payload, therefore absent from the record: the
        # caller must not be able to write NULL over a known author.
        for absent in ("author_id", "author_name", "message_type", "webhook_id"):
            self.assertNotIn(absent, rec)

    def test_partial_without_content_does_not_invent_one(self):
        rec = normalize_message(
            fx.partial_embed_only(fx.NORMAL["id"]),
            guild_id=fx.GUILD_A,
            channel=fx.CHANNEL_CTX[fx.CHANNEL_A1],
            partial=True,
        )
        self.assertNotIn("content", rec)
        self.assertIn("embeds_json", rec)


class UntrustedContent(unittest.TestCase):
    """Instruction-shaped text is stored as data and nothing more."""

    def test_injection_bodies_normalize_like_any_other_text(self):
        for payload in fx.injection_messages():
            rec = norm(payload)
            self.assertEqual(rec["content"], payload["content"])
            self.assertEqual(rec["message_type"], "default")
            self.assertIsNone(rec["deleted_at"])
            # No field anywhere in the record is derived from interpreting
            # the text: the record's shape is identical to a normal message.
            self.assertEqual(
                set(norm(fx.NORMAL)).symmetric_difference(set(rec)), set()
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
