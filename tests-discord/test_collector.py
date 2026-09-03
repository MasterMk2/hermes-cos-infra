#!/usr/bin/env python3
"""D.1 unit tests — gateway handlers, intents, writer and shutdown.

Spec sections 7/35 (minimal intents), 36 (create path), 39-42 (delete paths),
84/86/87 (deterministic writes, bounded queue, no LLM on the hot path).

These need discord.py, so run them with the collector's virtualenv:

    PYTHONPATH=../app:. ../venv/bin/python -m unittest test_collector
"""

import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import dk_fixtures as fx
import dk_store
from collector import Collector, build_intents
from dk_config import parse_config
from dk_schema import connect, init_source_db, source_id_for


class FakeType:
    def __init__(self, value):
        self.value = value


class FakeGuild:
    def __init__(self, guild_id, name):
        self.id = int(guild_id)
        self.name = name


class FakeChannel:
    def __init__(self, ctx, guild):
        self.id = int(ctx["channel_id"])
        self.name = ctx["channel_name"]
        self.type = FakeType(ctx["channel_type"])
        self.parent_id = int(ctx["parent_channel_id"]) if ctx["parent_channel_id"] else None
        self.parent = None
        self.guild = guild


class FakeAuthor:
    def __init__(self, payload):
        self.id = int(payload["id"])
        self.name = payload.get("username")
        self.global_name = payload.get("global_name")
        self.bot = bool(payload.get("bot"))


class FakeMessage:
    def __init__(self, payload, channel, guild):
        self.id = int(payload["id"])
        self.channel = channel
        self.guild = guild
        self.author = FakeAuthor(payload["author"])
        self.content = payload["content"]
        self.created_at = datetime.fromisoformat(payload["timestamp"])
        self.edited_at = (
            datetime.fromisoformat(payload["edited_timestamp"])
            if payload.get("edited_timestamp")
            else None
        )
        self.type = FakeType(payload.get("type", 0))
        self.webhook_id = payload.get("webhook_id")
        self.attachments = []
        self.embeds = []
        self.mentions = []
        self.reference = None


class RawDelete:
    def __init__(self, guild_id, channel_id, message_id):
        self.guild_id = int(guild_id)
        self.channel_id = int(channel_id)
        self.message_id = int(message_id)


class RawEdit:
    def __init__(self, guild_id, channel_id, data):
        self.guild_id = int(guild_id)
        self.channel_id = int(channel_id)
        self.data = data


class CollectorCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = init_source_db(
            connect(Path(self.tmp.name) / "messages.db", check_same_thread=False)
        )
        self.cfg = parse_config(fx.config_dict(owner=fx.OWNER_ID))
        self.client = Collector(self.cfg, self.con)
        self.guild_a = FakeGuild(fx.GUILD_A, "Guild A")
        self.guild_out = FakeGuild(fx.GUILD_OUTSIDE, "Somewhere Else")

    async def asyncTearDown(self):
        try:
            await self.client.close()
        except Exception:
            pass
        self.con.close()
        self.tmp.cleanup()

    def channel(self, channel_id, guild=None):
        return FakeChannel(fx.CHANNEL_CTX[channel_id], guild or self.guild_a)

    def message(self, payload, channel_id=None, guild=None):
        cid = channel_id or payload["channel_id"]
        return FakeMessage(payload, self.channel(cid, guild), guild or self.guild_a)

    def queued(self):
        return list(self.client.queue._queue)


class Intents(unittest.TestCase):
    def test_exactly_the_three_required_intents(self):
        i = build_intents()
        self.assertEqual(i.value, 1 | 512 | 32768)
        self.assertTrue(i.guilds and i.guild_messages and i.message_content)

    def test_everything_else_is_explicitly_off(self):
        i = build_intents()
        for name in (
            "members",
            "presences",
            "typing",
            "dm_messages",
            "dm_reactions",
            "guild_reactions",
            "voice_states",
            "auto_moderation_configuration",
            "auto_moderation_execution",
            "bans",
            "invites",
            "webhooks",
        ):
            self.assertFalse(getattr(i, name), f"intent {name} must be off")


class MessageRouting(CollectorCase):
    async def test_allowed_channel_is_queued(self):
        await self.client.on_message(self.message(fx.NORMAL))
        items = self.queued()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "create")
        self.assertEqual(items[0]["record"]["guild_id"], fx.GUILD_A)
        self.assertEqual(items[0]["record"]["content"], fx.NORMAL["content"])

    async def test_denied_channel_is_dropped_and_counted(self):
        payload = fx.message(channel_id=fx.CHANNEL_A_DENIED, content="secret ops")
        await self.client.on_message(self.message(payload))
        self.assertEqual(self.queued(), [])
        self.assertEqual(self.client._counters["dropped_out_of_scope"], 1)

    async def test_guild_outside_the_allowlist_is_dropped(self):
        payload = fx.message(content="not our server")
        await self.client.on_message(self.message(payload, guild=self.guild_out))
        self.assertEqual(self.queued(), [])
        self.assertEqual(self.client._counters["dropped_out_of_scope"], 1)

    async def test_direct_messages_are_ignored_entirely(self):
        msg = self.message(fx.NORMAL)
        msg.guild = None
        await self.client.on_message(msg)
        self.assertEqual(self.queued(), [])
        # Not even counted as out-of-scope: DMs never reach the collector
        # because the intent is not requested.
        self.assertEqual(self.client._counters["dropped_out_of_scope"], 0)

    async def test_thread_message_is_accepted_through_its_parent(self):
        await self.client.on_message(self.message(fx.THREAD_MESSAGE))
        items = self.queued()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["record"]["thread_id"], fx.THREAD_A)
        self.assertEqual(items[0]["record"]["parent_channel_id"], fx.CHANNEL_A2)


class RawEvents(CollectorCase):
    async def test_edit_carrying_content_but_no_author_is_still_partial(self):
        """A MESSAGE_UPDATE that has `content` is not necessarily complete.

        Deciding on `content` alone would mark it full, and a full record
        carries author_id=None, which then overwrites a known author. The
        REST refetch is unavailable here (no gateway), so the payload must
        stay partial.
        """
        self.client._remember_channel(self.channel(fx.CHANNEL_A1))
        data = fx.partial_edit(fx.NORMAL["id"], "編集後の本文")
        self.assertIn("content", data)
        self.assertNotIn("author", data)
        await self.client.on_raw_message_edit(RawEdit(fx.GUILD_A, fx.CHANNEL_A1, data))
        items = self.queued()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "update")
        self.assertIs(items[0]["partial"], True)
        self.assertEqual(items[0]["record"]["content"], "編集後の本文")
        self.assertNotIn("author_id", items[0]["record"])

    async def test_an_edit_never_erases_the_stored_author(self):
        """End-to-end across the seam: handler decision -> writer -> row."""
        from dk_normalize import normalize_message

        dk_store.upsert_guild(self.con, fx.GUILD_A, "Guild A")
        dk_store.upsert_channel(self.con, fx.CHANNEL_A1, fx.GUILD_A, channel_name="general")
        dk_store.apply_message(
            self.con,
            normalize_message(
                fx.NORMAL,
                guild_id=fx.GUILD_A,
                guild_name="Guild A",
                channel=fx.CHANNEL_CTX[fx.CHANNEL_A1],
            ),
        )
        self.con.commit()
        sid = source_id_for(fx.GUILD_A, fx.CHANNEL_A1, fx.NORMAL["id"])
        self.assertEqual(dk_store.get_message(self.con, sid)["author_id"], fx.OWNER_ID)

        self.client._remember_channel(self.channel(fx.CHANNEL_A1))
        await self.client.on_raw_message_edit(
            RawEdit(
                fx.GUILD_A,
                fx.CHANNEL_A1,
                fx.partial_edit(fx.NORMAL["id"], "書き換えた"),
            )
        )
        items = [self.client.queue.get_nowait()]
        await asyncio.to_thread(self.client._apply_batch, items)

        row = dk_store.get_message(self.con, sid)
        self.assertEqual(row["content"], "書き換えた")
        self.assertEqual(row["author_id"], fx.OWNER_ID)
        self.assertEqual(row["author_name"], "owner")
        self.assertEqual(row["message_type"], "default")
        self.assertIsNotNone(row["edited_at"])

    async def test_edit_in_a_denied_channel_is_dropped(self):
        self.client._remember_channel(self.channel(fx.CHANNEL_A_DENIED))
        payload = RawEdit(
            fx.GUILD_A,
            fx.CHANNEL_A_DENIED,
            {"id": fx.NORMAL["id"], "channel_id": fx.CHANNEL_A_DENIED, "content": "x"},
        )
        await self.client.on_raw_message_edit(payload)
        self.assertEqual(self.queued(), [])

    async def test_delete_in_an_allowed_channel_is_queued(self):
        await self.client.on_raw_message_delete(
            RawDelete(fx.GUILD_A, fx.CHANNEL_A1, fx.NORMAL["id"])
        )
        items = self.queued()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "delete")

    async def test_delete_in_a_never_collected_channel_leaves_no_tombstone(self):
        """A channel outside the allowlist must not accumulate tombstones."""
        await self.client.on_raw_message_delete(
            RawDelete(fx.GUILD_A, fx.CHANNEL_A_DENIED, fx.NORMAL["id"])
        )
        self.assertEqual(self.queued(), [])

    async def test_delete_of_a_stored_message_is_honoured_even_if_scope_changed(self):
        payload = fx.message(channel_id=fx.CHANNEL_A_DENIED, content="was collected")
        sid = source_id_for(fx.GUILD_A, fx.CHANNEL_A_DENIED, payload["id"])
        dk_store.upsert_guild(self.con, fx.GUILD_A, "Guild A")
        dk_store.upsert_channel(self.con, fx.CHANNEL_A_DENIED, fx.GUILD_A)
        from dk_normalize import normalize_message

        dk_store.apply_message(
            self.con,
            normalize_message(
                payload,
                guild_id=fx.GUILD_A,
                channel=fx.CHANNEL_CTX[fx.CHANNEL_A_DENIED],
            ),
        )
        self.con.commit()
        await self.client.on_raw_message_delete(
            RawDelete(fx.GUILD_A, fx.CHANNEL_A_DENIED, payload["id"])
        )
        self.assertEqual(len(self.queued()), 1)
        self.assertEqual(dk_store.get_message(self.con, sid)["source_id"], sid)

    async def test_bulk_delete_is_one_queue_item(self):
        class RawBulk:
            guild_id = int(fx.GUILD_A)
            channel_id = int(fx.CHANNEL_A1)
            message_ids = {111111111111111111, 111111111111111112}

        await self.client.on_raw_bulk_message_delete(RawBulk())
        items = self.queued()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "bulk_delete")
        self.assertEqual(len(items[0]["message_ids"]), 2)


class WriterAndShutdown(CollectorCase):
    async def test_batch_is_applied_in_one_transaction(self):
        await self.client.on_message(self.message(fx.NORMAL))
        await self.client.on_message(self.message(fx.REPLY))
        items = [self.client.queue.get_nowait() for _ in range(2)]
        counts = await asyncio.to_thread(self.client._apply_batch, items)
        self.assertEqual(counts["created"], 2)
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2
        )
        # Every applied message leaves exactly one index-queue entry behind.
        self.assertEqual(dk_store.queue_depth(self.con), 2)

    async def test_shutdown_drains_the_queue_before_stopping(self):
        await self.client.setup_hook()
        for i in range(25):
            await self.client.on_message(
                self.message(fx.message(content=f"drain {i}"))
            )
        self.assertGreater(self.client.queue.qsize(), 0)
        await self.client.shutdown()
        self.assertEqual(self.client.queue.qsize(), 0)
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 25
        )

    async def test_status_snapshot_is_written_without_content(self):
        await self.client.on_message(self.message(fx.NORMAL))
        items = [self.client.queue.get_nowait()]
        await asyncio.to_thread(self.client._apply_batch, items)
        await asyncio.to_thread(self.client._write_status)
        state = {
            r["key"]: r["value"]
            for r in self.con.execute("SELECT key, value FROM ingest_state")
        }
        self.assertEqual(state["source_message_count"], "1")
        self.assertEqual(state["messages_with_content"], "1")
        self.assertEqual(state["intents_value"], str(build_intents().value))
        self.assertNotIn(fx.NORMAL["content"], "".join(state.values()))


class BackfillReporting(unittest.IsolatedAsyncioTestCase):
    """A channel that yields nothing must say why.

    Forum channels have no message history of their own, and the first version
    returned from that branch before recording anything — so seventeen
    allowlisted channels were never backfilled while the status surface showed
    a complete, error-free backfill. Silence is the bug being tested here.
    """

    class _Container:
        """A forum-like channel: no `history` attribute."""

        def __init__(self, channel_id):
            self.id = int(channel_id)
            self.name = "projects-forum"
            self.type = FakeType(15)
            self.parent_id = None
            self.guild = FakeGuild(fx.GUILD_A, "Guild A")

    class _Client:
        def __init__(self, channel):
            self._channel = channel

        def get_channel(self, cid):
            return self._channel if int(cid) == self._channel.id else None

    async def test_a_container_channel_records_state_and_logs(self):
        import logging

        import dk_backfill

        cfg = parse_config(fx.config_dict())
        scope = cfg.guild(fx.GUILD_A)
        submitted = []

        async def submit(item):
            submitted.append(item)

        records = []

        class _Log:
            def info(self, msg, *a):
                records.append(msg % a if a else msg)

            def warning(self, msg, *a):
                records.append(msg % a if a else msg)

        channel = self._Container(fx.CHANNEL_A2)
        result = await dk_backfill.backfill_channel(
            self._Client(channel),
            cfg,
            scope,
            fx.CHANNEL_A2,
            submit=submit,
            state=None,
            log=_Log(),
        )

        self.assertEqual(result["fetched"], 0)
        self.assertTrue(result["complete"])
        self.assertIn("no message history", result["error"])
        # The two things the old code dropped on the floor:
        states = [i for i in submitted if i["kind"] == "backfill_state"]
        self.assertEqual(len(states), 1, "no backfill_state was recorded")
        self.assertEqual(states[0]["channel_id"], fx.CHANNEL_A2)
        self.assertIn("no message history", states[0]["error"])
        self.assertTrue(records, "nothing was logged")
        self.assertIn(str(fx.CHANNEL_A2), records[0])

    async def test_thread_enumeration_is_bounded(self):
        import dk_backfill

        self.assertLessEqual(dk_backfill.MAX_THREADS_PER_CHANNEL, 100)
        self.assertGreater(dk_backfill.MAX_THREADS_PER_CHANNEL, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
