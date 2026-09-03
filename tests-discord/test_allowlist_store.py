#!/usr/bin/env python3
"""D.1 unit tests — allowlists, dedupe, edit ordering, tombstones.

Spec sections 10-12 (double allowlist / default deny), 49 and 102 (dedupe),
37-38 and 105 (edit ordering and partial payloads), 39-42 (delete and bulk
delete tombstones), 44 (resumable backfill state).
"""

import tempfile
import unittest
from pathlib import Path

import dk_fixtures as fx
import dk_store
from dk_config import ConfigError, parse_config
from dk_normalize import normalize_message
from dk_schema import open_source_db, source_id_for


def cfg(**kw):
    return parse_config(fx.config_dict(**kw))


def rec_for(payload, channel_id=None, guild_id=fx.GUILD_A, **kw):
    cid = channel_id or payload.get("channel_id")
    return normalize_message(
        payload,
        guild_id=guild_id,
        guild_name="Guild A" if guild_id == fx.GUILD_A else "Guild B",
        channel=fx.CHANNEL_CTX[cid],
        **kw,
    )


class Allowlist(unittest.TestCase):
    def setUp(self):
        self.cfg = cfg()

    def test_allowed_guild_and_channel_is_ingested(self):
        self.assertTrue(self.cfg.guild_allowed(fx.GUILD_A))
        self.assertTrue(self.cfg.channel_allowed(fx.GUILD_A, fx.CHANNEL_A1))
        self.assertTrue(self.cfg.channel_allowed(fx.GUILD_B, fx.CHANNEL_B1))

    def test_wrong_guild_is_rejected(self):
        self.assertFalse(self.cfg.guild_allowed(fx.GUILD_OUTSIDE))
        # Even a channel id that IS allowlisted elsewhere is refused when it
        # is claimed by a guild outside the allowlist.
        self.assertFalse(self.cfg.channel_allowed(fx.GUILD_OUTSIDE, fx.CHANNEL_A1))

    def test_wrong_channel_is_rejected(self):
        self.assertFalse(self.cfg.channel_allowed(fx.GUILD_A, fx.CHANNEL_A_DENIED))
        # A channel of guild B is not reachable through guild A's scope.
        self.assertFalse(self.cfg.channel_allowed(fx.GUILD_A, fx.CHANNEL_B1))

    def test_thread_inherits_its_allowlisted_parent(self):
        self.assertTrue(
            self.cfg.channel_allowed(fx.GUILD_A, fx.THREAD_A, fx.CHANNEL_A2)
        )

    def test_thread_under_a_denied_parent_is_rejected(self):
        self.assertFalse(
            self.cfg.channel_allowed(fx.GUILD_A, fx.THREAD_A, fx.CHANNEL_A_DENIED)
        )

    def test_thread_inheritance_can_be_switched_off(self):
        data = fx.config_dict()
        data["guilds"][0]["include_threads"] = False
        c = parse_config(data)
        self.assertFalse(c.channel_allowed(fx.GUILD_A, fx.THREAD_A, fx.CHANNEL_A2))

    def test_disabled_guild_collects_nothing(self):
        c = cfg(guild_a_enabled=False)
        self.assertFalse(c.channel_allowed(fx.GUILD_A, fx.CHANNEL_A1))
        self.assertTrue(c.channel_allowed(fx.GUILD_B, fx.CHANNEL_B1))

    def test_empty_channel_list_collects_nothing(self):
        data = fx.config_dict()
        data["guilds"][0]["channels"] = []
        c = parse_config(data)
        self.assertFalse(c.guild_allowed(fx.GUILD_A))
        self.assertFalse(c.channel_allowed(fx.GUILD_A, fx.CHANNEL_A1))

    def test_shipped_placeholder_config_is_default_deny(self):
        c = parse_config(fx.empty_config_dict())
        self.assertEqual(c.active_guild_ids, ())
        self.assertEqual(c.allowed_channel_ids, ())
        self.assertFalse(c.ready_to_ingest)
        # The failure mode this guards against: an empty allowlist widening
        # into "collect everything the bot can see".
        self.assertFalse(c.channel_allowed(fx.GUILD_A, fx.CHANNEL_A1))
        self.assertFalse(c.channel_allowed("0", "0"))


class ConfigValidation(unittest.TestCase):
    def test_the_guild_cap_is_enforced(self):
        """The cap is a hard ceiling, not a suggestion: the scope cannot grow
        by accident, only by someone editing MAX_GUILDS on purpose."""
        from dk_config import MAX_GUILDS

        data = fx.config_dict()
        extra = ["911111111111111111", "922222222222222222", "933333333333333333"]
        while len(data["guilds"]) < MAX_GUILDS:
            data["guilds"].append(
                {"guild_id": extra.pop(), "enabled": True, "channels": [fx.CHANNEL_A1]}
            )
        parse_config(data)  # at the cap: accepted

        data["guilds"].append(
            {"guild_id": extra.pop(), "enabled": True, "channels": [fx.CHANNEL_A1]}
        )
        with self.assertRaises(ConfigError):
            parse_config(data)  # one over: refused

    def test_guild_name_instead_of_id_is_refused(self):
        data = fx.config_dict()
        data["guilds"][0]["guild_id"] = "My Server"
        with self.assertRaises(ConfigError):
            parse_config(data)

    def test_token_in_config_is_refused(self):
        data = fx.config_dict()
        data["secret"] = {"token": "not-a-real-token"}
        with self.assertRaises(ConfigError):
            parse_config(data)

    def test_owner_id_is_optional_and_validated(self):
        self.assertIsNone(parse_config(fx.config_dict()).owner_discord_user_id)
        self.assertEqual(
            parse_config(fx.config_dict(owner=fx.OWNER_ID)).owner_discord_user_id,
            fx.OWNER_ID,
        )
        with self.assertRaises(ConfigError):
            parse_config(fx.config_dict(owner="me"))


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = open_source_db(Path(self.tmp.name) / "messages.db")
        dk_store.upsert_guild(self.con, fx.GUILD_A, "Guild A")
        dk_store.upsert_channel(
            self.con, fx.CHANNEL_A1, fx.GUILD_A, channel_name="general"
        )

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def rows(self):
        return self.con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    def get(self, source_id):
        return dk_store.get_message(self.con, source_id)

    def queue(self):
        return [
            (r["source_id"], r["op"])
            for r in self.con.execute("SELECT * FROM index_queue ORDER BY id")
        ]


class Dedupe(StoreCase):
    def test_same_message_twice_yields_one_row(self):
        rec = rec_for(fx.NORMAL)
        self.assertEqual(dk_store.apply_message(self.con, rec), "inserted")
        self.assertEqual(dk_store.apply_message(self.con, rec), "unchanged")
        self.assertEqual(self.rows(), 1)

    def test_gateway_then_backfill_collapses_into_one_row(self):
        rec = rec_for(fx.NORMAL)
        dk_store.apply_message(self.con, rec, ingest_source="gateway")
        again = rec_for(fx.NORMAL, ingest_source="backfill")
        dk_store.apply_message(self.con, again, ingest_source="backfill")
        self.assertEqual(self.rows(), 1)

    def test_only_one_index_queue_entry_for_an_unchanged_repeat(self):
        rec = rec_for(fx.NORMAL)
        dk_store.apply_message(self.con, rec)
        dk_store.apply_message(self.con, rec)
        self.assertEqual(len(self.queue()), 1)


class EditOrdering(StoreCase):
    def setUp(self):
        super().setUp()
        self.payload = fx.message(content="A")
        self.sid = source_id_for(fx.GUILD_A, fx.CHANNEL_A1, self.payload["id"])
        dk_store.apply_message(self.con, rec_for(self.payload))

    def test_edit_replaces_the_body(self):
        edited = fx.message(
            message_id=self.payload["id"], content="B", edited=fx.at(2)
        )
        self.assertEqual(dk_store.apply_message(self.con, rec_for(edited)), "updated")
        row = self.get(self.sid)
        self.assertEqual(row["content"], "B")
        self.assertEqual(row["revision"], 1)

    def test_partial_edit_does_not_null_out_absent_fields(self):
        partial = normalize_message(
            fx.partial_edit(self.payload["id"], "B"),
            guild_id=fx.GUILD_A,
            channel=fx.CHANNEL_CTX[fx.CHANNEL_A1],
            partial=True,
        )
        dk_store.apply_message(self.con, partial, partial=True)
        row = self.get(self.sid)
        self.assertEqual(row["content"], "B")
        self.assertEqual(row["author_id"], fx.OWNER_ID)
        self.assertEqual(row["author_name"], "owner")
        self.assertEqual(row["message_type"], "default")

    def test_older_backfill_payload_cannot_overwrite_a_newer_edit(self):
        """Spec section 105: the gateway edit must survive a late page."""
        edited = fx.message(
            message_id=self.payload["id"], content="B", edited=fx.at(2)
        )
        dk_store.apply_message(self.con, rec_for(edited))
        stale = rec_for(
            fx.message(message_id=self.payload["id"], content="A"),
            ingest_source="backfill",
        )
        self.assertEqual(
            dk_store.apply_message(self.con, stale, ingest_source="backfill"),
            "skipped_stale",
        )
        self.assertEqual(self.get(self.sid)["content"], "B")

    def test_a_newer_edit_from_backfill_is_applied(self):
        newer = rec_for(
            fx.message(message_id=self.payload["id"], content="C", edited=fx.at(3)),
            ingest_source="backfill",
        )
        self.assertEqual(
            dk_store.apply_message(self.con, newer, ingest_source="backfill"), "updated"
        )
        self.assertEqual(self.get(self.sid)["content"], "C")

    def test_registry_fields_are_never_blanked_by_an_unknowing_payload(self):
        blind = rec_for(fx.message(message_id=self.payload["id"], content="D"))
        blind["channel_name"] = None
        blind["guild_name"] = None
        dk_store.apply_message(self.con, blind)
        row = self.get(self.sid)
        self.assertEqual(row["channel_name"], "general")
        self.assertEqual(row["guild_name"], "Guild A")


class Deletion(StoreCase):
    def setUp(self):
        super().setUp()
        self.payload = fx.message(content="消える予定の本文")
        self.sid = source_id_for(fx.GUILD_A, fx.CHANNEL_A1, self.payload["id"])
        dk_store.apply_message(self.con, rec_for(self.payload))

    def test_delete_leaves_a_tombstone_without_a_body(self):
        self.assertEqual(
            dk_store.apply_delete(
                self.con, fx.GUILD_A, fx.CHANNEL_A1, self.payload["id"]
            ),
            "tombstoned",
        )
        row = self.get(self.sid)
        self.assertIsNotNone(row["deleted_at"])
        self.assertIsNone(row["content"])
        self.assertIsNone(row["content_normalized"])
        self.assertIsNone(row["search_text"])
        self.assertIsNone(row["attachments_json"])
        # Identity and timing survive so provenance still resolves.
        self.assertEqual(row["message_id"], self.payload["id"])
        self.assertEqual(row["guild_id"], fx.GUILD_A)
        self.assertIsNotNone(row["created_at"])
        self.assertEqual(self.queue()[-1], (self.sid, "delete"))

    def test_delete_also_drops_the_content_hash(self):
        """The hash is a fingerprint of the body Discord just removed. Keeping
        it would let someone confirm a guess at what the message said, and it
        has no remaining purpose: a tombstone is never re-evaluated."""
        row_before = self.get(self.sid)
        self.assertTrue(row_before["content_hash"])
        dk_store.apply_delete(self.con, fx.GUILD_A, fx.CHANNEL_A1, self.payload["id"])
        self.assertIsNone(self.get(self.sid)["content_hash"])

    def test_a_later_payload_cannot_resurrect_a_deleted_body(self):
        dk_store.apply_delete(self.con, fx.GUILD_A, fx.CHANNEL_A1, self.payload["id"])
        again = rec_for(fx.message(message_id=self.payload["id"], content="復活"))
        self.assertEqual(
            dk_store.apply_message(self.con, again), "skipped_deleted"
        )
        self.assertIsNone(self.get(self.sid)["content"])

    def test_delete_of_an_unseen_message_still_tombstones(self):
        unseen = fx.message(content="never ingested")
        sid = source_id_for(fx.GUILD_A, fx.CHANNEL_A1, unseen["id"])
        self.assertEqual(
            dk_store.apply_delete(self.con, fx.GUILD_A, fx.CHANNEL_A1, unseen["id"]),
            "tombstoned_unknown",
        )
        row = self.get(sid)
        self.assertIsNone(row["content"])
        self.assertIsNotNone(row["deleted_at"])

    def test_bulk_delete_follows_the_same_policy(self):
        payloads = [fx.message(content=f"bulk {i}") for i in range(4)]
        for p in payloads:
            dk_store.apply_message(self.con, rec_for(p))
        counts = dk_store.apply_bulk_delete(
            self.con, fx.GUILD_A, fx.CHANNEL_A1, [p["id"] for p in payloads]
        )
        self.assertEqual(counts["tombstoned"], 4)
        for p in payloads:
            row = self.get(source_id_for(fx.GUILD_A, fx.CHANNEL_A1, p["id"]))
            self.assertIsNone(row["content"])
            self.assertIsNotNone(row["deleted_at"])

    def test_double_delete_is_idempotent(self):
        dk_store.apply_delete(self.con, fx.GUILD_A, fx.CHANNEL_A1, self.payload["id"])
        self.assertEqual(
            dk_store.apply_delete(
                self.con, fx.GUILD_A, fx.CHANNEL_A1, self.payload["id"]
            ),
            "already_deleted",
        )


class QueueAndCounts(StoreCase):
    def test_every_change_enqueues_exactly_one_index_entry(self):
        payload = fx.message(content="A")
        sid = source_id_for(fx.GUILD_A, fx.CHANNEL_A1, payload["id"])
        dk_store.apply_message(self.con, rec_for(payload))
        dk_store.apply_message(
            self.con,
            rec_for(fx.message(message_id=payload["id"], content="B", edited=fx.at(2))),
        )
        dk_store.apply_delete(self.con, fx.GUILD_A, fx.CHANNEL_A1, payload["id"])
        self.assertEqual(
            self.queue(), [(sid, "upsert"), (sid, "upsert"), (sid, "delete")]
        )
        self.assertEqual(dk_store.queue_depth(self.con), 3)
        self.assertIsNotNone(dk_store.queue_oldest_enqueued_at(self.con))

    def test_counts_snapshot_reports_empty_content_separately(self):
        """Section 8: without MESSAGE_CONTENT every body arrives empty, which
        must be visible rather than counted as a successful ingest."""
        dk_store.apply_message(self.con, rec_for(fx.message(content="ある")))
        dk_store.apply_message(self.con, rec_for(fx.message(content="")))
        counts = dk_store.counts_snapshot(self.con)
        self.assertEqual(counts["live"], 2)
        self.assertEqual(counts["with_content"], 1)

    def test_per_guild_counts_do_not_mix_guilds(self):
        dk_store.upsert_guild(self.con, fx.GUILD_B, "Guild B")
        dk_store.upsert_channel(
            self.con, fx.CHANNEL_B1, fx.GUILD_B, channel_name="b-general"
        )
        dk_store.apply_message(self.con, rec_for(fx.message(content="a-side")))
        dk_store.apply_message(
            self.con,
            rec_for(
                fx.message(channel_id=fx.CHANNEL_B1, content="b-side"),
                guild_id=fx.GUILD_B,
            ),
        )
        per_guild = {g["guild_id"]: g["count"] for g in dk_store.counts_snapshot(self.con)["per_guild"]}
        self.assertEqual(per_guild, {fx.GUILD_A: 1, fx.GUILD_B: 1})


class BackfillState(StoreCase):
    def test_cursor_survives_and_only_moves_backwards(self):
        dk_store.save_backfill_state(
            self.con,
            fx.CHANNEL_A1,
            fx.GUILD_A,
            last_cursor="900",
            oldest_created_ts=5000,
            added=100,
        )
        dk_store.save_backfill_state(
            self.con,
            fx.CHANNEL_A1,
            fx.GUILD_A,
            last_cursor="800",
            oldest_created_ts=3000,
            added=100,
        )
        row = dk_store.get_backfill_state(self.con, fx.CHANNEL_A1)
        self.assertEqual(row["last_cursor"], "800")
        self.assertEqual(row["oldest_created_ts"], 3000)
        self.assertEqual(row["message_count"], 200)
        self.assertEqual(row["complete"], 0)

    def test_completion_and_error_are_recorded(self):
        dk_store.save_backfill_state(
            self.con, fx.CHANNEL_A1, fx.GUILD_A, complete=True, last_error=None
        )
        summary = dk_store.backfill_summary(self.con)
        self.assertEqual(len(summary), 1)
        self.assertTrue(summary[0]["complete"])

    def test_channel_permission_failure_is_per_channel(self):
        dk_store.mark_channel_unreadable(
            self.con, fx.CHANNEL_A_DENIED, fx.GUILD_A, "forbidden: no access"
        )
        rows = {
            r["channel_id"]: r
            for r in self.con.execute("SELECT * FROM channels")
        }
        self.assertEqual(rows[fx.CHANNEL_A_DENIED]["readable"], 0)
        self.assertIn("forbidden", rows[fx.CHANNEL_A_DENIED]["last_error"])
        # The allowed channel is untouched: one bad channel does not take the
        # guild's collector down with it.
        self.assertIsNone(rows[fx.CHANNEL_A1]["last_error"])


class MigrationIdempotency(unittest.TestCase):
    def test_reopening_an_existing_database_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "messages.db"
            con = open_source_db(path)
            dk_store.upsert_guild(con, fx.GUILD_A, "Guild A")
            dk_store.upsert_channel(con, fx.CHANNEL_A1, fx.GUILD_A)
            dk_store.apply_message(con, rec_for(fx.NORMAL))
            con.commit()
            before = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            con.close()

            con = open_source_db(path)
            con = open_source_db(path)
            after = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            self.assertEqual(before, after)
            self.assertEqual(
                con.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )
            con.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
