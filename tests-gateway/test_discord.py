#!/usr/bin/env python3
"""G.1 unit tests — the Discord Interface adapter.

This file exists because its absence cost a production failure. `_handle`
had no test at all, and it contained

    con = store.open_db(self.db_path)      # open_db lives in gw_schema

which raised AttributeError on every single DM. `on_message` caught it, as
designed, so the bot stayed connected and the operator saw a healthy service
that answered nothing. A single test that ran one message through would have
caught it before it ever reached the host.

discord.py is not installed everywhere, so these skip when it is missing —
but they DO run on the deployment host, which is where the adapter runs.
"""

import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

import gw_fixtures as fx
import gw_store as store
from gw_schema import open_db

try:
    import discord

    import gw_discord
    HAVE_DISCORD = True
except ImportError:  # pragma: no cover - depends on the environment
    HAVE_DISCORD = False


def fake_message(*, content="やあ", author_id=fx.OWNER_DISCORD, is_bot=False,
                 dm=True, message_id=1545253544654012469):
    """A stand-in for discord.Message carrying only what the adapter reads.

    `spec=` makes isinstance() work, which is what the DM check uses.
    """
    channel_cls = discord.DMChannel if dm else discord.TextChannel
    channel = mock.MagicMock(spec=channel_cls)
    channel.id = int(fx.DM_CHANNEL_ID)

    author = mock.MagicMock()
    author.id = int(author_id)
    author.bot = is_bot

    msg = mock.MagicMock()
    msg.id = message_id
    msg.author = author
    msg.channel = channel
    msg.content = content
    msg.created_at = datetime.now(timezone.utc)
    return msg


@unittest.skipUnless(HAVE_DISCORD, "discord.py not installed")
class DiscordAdapterTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from gw_config import parse_config

        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db_path)
        open_db(self.db_path).close()
        self.cfg = parse_config(fx.config_dict(
            paths={"state_db": self.db_path, "log_dir": tempfile.mkdtemp()}
        ))
        import asyncio

        self.bot = gw_discord.InterfaceBot(self.cfg, self.db_path,
                                           asyncio.Event())

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except OSError:
                pass

    def counts(self):
        con = open_db(self.db_path)
        try:
            return {r[0]: r[1] for r in con.execute(
                "SELECT state, COUNT(*) FROM inbound_messages GROUP BY state"
            )}
        finally:
            con.close()

    def audits(self):
        con = open_db(self.db_path)
        try:
            return [r[0] for r in con.execute("SELECT event FROM audit_events")]
        finally:
            con.close()


class TestIntents(unittest.TestCase):
    @unittest.skipUnless(HAVE_DISCORD, "discord.py not installed")
    def test_direct_messages_only(self):
        """4096 = DIRECT_MESSAGES. No GUILDS, no MESSAGE_CONTENT.

        Verified empirically against the live API on 2026-09-04: a DM sent to
        a bot holding exactly these intents arrived with `content` populated,
        which is what makes requesting the privileged intent unnecessary.
        """
        self.assertEqual(gw_discord.build_intents().value, 4096)
        self.assertEqual(gw_discord.REQUIRED_INTENTS_VALUE, 4096)

    @unittest.skipUnless(HAVE_DISCORD, "discord.py not installed")
    def test_guild_intents_are_absent(self):
        intents = gw_discord.build_intents()
        self.assertFalse(intents.guilds)
        self.assertFalse(intents.guild_messages)
        self.assertFalse(intents.message_content)
        self.assertTrue(intents.dm_messages)


class TestOwnerDmIsQueued(DiscordAdapterTestCase):
    async def test_owner_dm_reaches_the_queue(self):
        """The test the missing of which cost a production failure."""
        await self.bot._handle(fake_message(content="テストです"))
        self.assertEqual(self.counts().get("QUEUED"), 1)
        self.assertIn("inbound_queued", self.audits())

    async def test_on_message_does_not_swallow_a_working_path(self):
        """on_message catches everything; make sure it still does the work."""
        await self.bot.on_message(fake_message(content="ping"))
        self.assertEqual(self.counts().get("QUEUED"), 1)

    async def test_the_database_work_runs_off_the_event_loop(self):
        # On the loop it would stall discord.py's gateway heartbeat whenever
        # the USB disk is slow.
        import threading
        loop_thread = threading.get_ident()
        seen = []
        original = gw_discord.store.record_inbound

        def spy(con, canonical):
            seen.append(threading.get_ident())
            return original(con, canonical)

        gw_discord.store.record_inbound = spy
        try:
            await self.bot._handle(fake_message(content="テストです"))
        finally:
            gw_discord.store.record_inbound = original
        self.assertEqual(len(seen), 1)
        self.assertNotEqual(seen[0], loop_thread)
        self.assertEqual(self.counts().get("QUEUED"), 1)

    async def test_two_quick_dms_are_recorded_in_the_order_they_arrived(self):
        # With the database work in a thread, nothing ordered two DMs' commits:
        # the second could be queued, claimed and answered before the first.
        import asyncio
        import threading
        import time
        events, original = [], self.bot._record

        def slow_first(canonical):
            mid = canonical.platform_message_id
            events.append(("start", mid))
            if not events[:-1]:
                time.sleep(0.2)   # the first DM's write is slow (a USB stall)
            try:
                return original(canonical)
            finally:
                events.append(("end", mid))

        self.bot._record = slow_first
        first = asyncio.create_task(self.bot._handle(fake_message(content="一通目", message_id=1)))
        second = asyncio.create_task(self.bot._handle(fake_message(content="二通目", message_id=2)))
        await asyncio.gather(first, second)
        self.assertEqual(events, [("start", "1"), ("end", "1"), ("start", "2"), ("end", "2")])
        con = open_db(self.db_path)
        try:
            row = store.claim_next(con, max_active=2)
            con.commit()
        finally:
            con.close()
        self.assertEqual(row["text"], "一通目")

    async def test_a_dm_queued_at_shutdown_is_still_recorded(self):
        # Second review: DMs parked behind an intake lock were cancelled at
        # shutdown before they were written, and Discord does not redeliver.
        import asyncio
        import time
        original = self.bot._record

        def slow(canonical):
            time.sleep(0.2)
            return original(canonical)

        self.bot._record = slow
        tasks = [asyncio.create_task(self.bot._handle(
            fake_message(content=f"{i}通目", message_id=10 + i))) for i in range(3)]
        await asyncio.sleep(0.05)
        for t in tasks:
            t.cancel()      # what asyncio.run does to leftover tasks on exit
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.to_thread(self.bot._intake.shutdown, wait=True)
        self.assertEqual(self.counts().get("QUEUED"), 3)

    async def test_typing_pulse_is_best_effort(self):
        class Channel:
            def __init__(self, error=None):
                self.error, self.calls = error, 0

            async def typing(self):
                self.calls += 1
                if self.error:
                    raise self.error

        ok, broken = Channel(), Channel(RuntimeError("synthetic"))
        self.bot.get_channel = lambda cid: ok if cid == 1 else broken
        self.assertTrue(await self.bot.typing_once("1"))
        self.assertFalse(await self.bot.typing_once("2"))
        self.assertEqual((ok.calls, broken.calls), (1, 1))

    async def test_the_text_survives(self):
        await self.bot._handle(fake_message(content="明日の予定は？"))
        con = open_db(self.db_path)
        try:
            text = con.execute(
                "SELECT text FROM inbound_messages"
            ).fetchone()[0]
        finally:
            con.close()
        self.assertEqual(text, "明日の予定は？")

    async def test_duplicate_message_is_not_queued_twice(self):
        msg = fake_message()
        await self.bot._handle(msg)
        await self.bot._handle(msg)
        con = open_db(self.db_path)
        try:
            total = con.execute(
                "SELECT COUNT(*) FROM inbound_messages"
            ).fetchone()[0]
        finally:
            con.close()
        self.assertEqual(total, 1)
        self.assertIn("inbound_duplicate", self.audits())


class TestWhatMustNeverBeActedOn(DiscordAdapterTestCase):
    async def test_guild_message_is_ignored(self):
        await self.bot._handle(fake_message(dm=False))
        self.assertEqual(self.counts(), {})

    async def test_bot_author_is_ignored(self):
        await self.bot._handle(fake_message(is_bot=True))
        self.assertEqual(self.counts(), {})

    async def test_stranger_is_recorded_but_not_queued(self):
        await self.bot._handle(fake_message(author_id=fx.STRANGER_DISCORD))
        counts = self.counts()
        self.assertIsNone(counts.get("QUEUED"))
        self.assertEqual(counts.get("RECEIVED"), 1)
        self.assertIn("inbound_rejected", self.audits())

    async def test_empty_content_is_reported_not_queued(self):
        """Empty DM content would mean the intent assumption is wrong."""
        with self.assertLogs("gateway.discord", level="WARNING") as caught:
            await self.bot._handle(fake_message(content="   "))
        self.assertEqual(self.counts(), {})
        self.assertTrue(
            any("MESSAGE_CONTENT" in line for line in caught.output),
            "the warning must name the likely cause",
        )


class TestOutboundShaping(DiscordAdapterTestCase):
    async def test_long_answer_is_chunked_under_the_limit(self):
        chunks = self.bot.prepare("あ" * 12000)
        self.assertTrue(chunks)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), gw_discord.DISCORD_MAX_MESSAGE_CHARS)

    async def test_mentions_are_neutralised(self):
        chunks = self.bot.prepare("@everyone 集合")
        self.assertNotIn("@everyone", "".join(chunks))

    async def test_secrets_are_redacted(self):
        leak = "sk-ant-api03-" + "A" * 40
        chunks = self.bot.prepare(f"key={leak}")
        self.assertNotIn(leak, "".join(chunks))

    async def test_empty_answer_produces_no_chunks(self):
        self.assertEqual(self.bot.prepare(""), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
