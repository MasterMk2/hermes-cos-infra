"""Restart/ambiguous-outcome regression tests, with no sockets or live sends.

Exercise the real handler and an on-disk SQLite DB; only the channel adapters
and HTTP request reader are fakes. Closing/reopening the DB matters here.
"""

import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace

import gw_store as store
from gw_proactive import ProactiveServer
from test_proactive_socket import FakeDiscordBot, FakeLineClient, StubGateway


class SimulatedCrash(RuntimeError):
    pass


class RequestBody:
    def __init__(self, data):
        self.data = json.dumps(data).encode("utf-8")

    async def read(self, count):
        return self.data[:count]


class TestDurableOutcome(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "gateway.db")
        self.discord = FakeDiscordBot()
        self.line = FakeLineClient()
        self.reopen()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def reopen(self):
        self.gw = StubGateway(self.db_path, line=self.line, discord=self.discord)
        self.server = ProactiveServer(self.gw)

    def payload(self, channel="discord", **changes):
        body = {"notification_id": "ntf_restart_fixture", "preferred_channel": channel,
                "severity": "IMPORTANT", "text": "合成テストの通知"}
        body.update(changes)
        return body

    async def post(self, body):
        response = await self.server.handle_notify(
            SimpleNamespace(content=RequestBody(body)))
        return response.status, json.loads(response.body)

    def row(self):
        con = self.gw.db()
        try:
            return dict(con.execute("SELECT * FROM proactive_deliveries").fetchone())
        finally:
            con.close()

    async def test_accepted_then_crashed_is_held_after_reopen_on_both_channels(self):
        for channel in ("discord", "line"):
            with self.subTest(channel=channel):
                self.db_path = os.path.join(self.tmp.name, channel + ".db")
                self.reopen()
                original = self.gw._in_thread

                async def crash_before_success_record(fn, *args, **kwargs):
                    if fn is store.mark_proactive and args[1] == "SENT":
                        raise SimulatedCrash("accepted, but no durable receipt")
                    return await original(fn, *args, **kwargs)

                self.gw._in_thread = crash_before_success_record
                with self.assertRaises(SimulatedCrash):
                    await self.post(self.payload(channel))
                self.assertEqual(self.row()["state"], "SENDING")
                self.reopen()
                con = self.gw.db()
                try:
                    con.execute("UPDATE proactive_deliveries SET updated_at = ?",
                                ("2020-01-01T00:00:00+00:00",))
                    store.recover_stuck_proactive(con, lease_seconds=60)
                    con.commit()
                finally:
                    con.close()
                self.reopen()
                for _ in range(3):
                    status, answer = await self.post(self.payload(channel))
                    self.assertEqual((status, answer["status"]), (409, "outcome_unknown"))
                self.assertEqual(self.row()["state"], "UNKNOWN")
                self.assertEqual(self.row()["attempts"], 1)
                self.assertIsNone(self.row()["sent_at"])
                sends = self.discord.sends if channel == "discord" else self.line.sends
                self.assertEqual(len(sends), 1)

    async def test_discord_timeout_after_acceptance_is_not_retried(self):
        async def accepted_without_answer(chunks):
            self.discord.sends.append(list(chunks))
            raise TimeoutError("synthetic lost receipt")
        self.discord.send_owner_dm = accepted_without_answer
        status, answer = await self.post(self.payload())
        self.assertEqual((status, answer["status"]), (409, "outcome_unknown"))
        self.reopen()
        await self.post(self.payload())
        self.assertEqual(len(self.discord.sends), 1)
        self.assertEqual(self.row()["state"], "UNKNOWN")

    async def test_partial_discord_delivery_is_not_replayed(self):
        self.discord.prepare = lambda text: ["part one", "part two"]
        async def partial(chunks):
            self.discord.sends.append([chunks[0]])
            return 1, "http:403:50007"
        self.discord.send_owner_dm = partial
        _, answer = await self.post(self.payload())
        self.assertEqual(answer["status"], "outcome_unknown")
        self.reopen()
        await self.post(self.payload())
        self.assertEqual(len(self.discord.sends), 1)

    async def test_known_rejection_without_a_send_can_be_retried(self):
        self.discord.ok = False
        status, answer = await self.post(self.payload())
        self.assertEqual((status, answer["status"]), (502, "failed"))
        self.assertEqual(self.row()["state"], "NOT_SENT")
        self.discord.ok = True
        status, answer = await self.post(self.payload())
        self.assertEqual((status, answer["status"]), (200, "delivered"))

    async def test_recovery_does_not_turn_unknown_into_success_or_pending(self):
        original = self.gw._in_thread
        async def stop_after_claim(fn, *args, **kwargs):
            result = await original(fn, *args, **kwargs)
            if fn.__name__ == "claim":
                raise SimulatedCrash("before adapter entry")
            return result
        self.gw._in_thread = stop_after_claim
        with self.assertRaises(SimulatedCrash):
            await self.post(self.payload())
        self.reopen()
        con = self.gw.db()
        try:
            con.execute("UPDATE proactive_deliveries SET updated_at = ?",
                        ("2020-01-01T00:00:00+00:00",))
            store.recover_stuck_proactive(con, lease_seconds=60)
            store.recover_stuck_proactive(con, lease_seconds=60)
            con.commit()
            status = store.liveness(con)
        finally:
            con.close()
        self.assertEqual(self.row()["state"], "UNKNOWN")
        self.assertEqual(status["proactive_unknown"], 1)
        self.assertIsNone(status["last_proactive_success_at"])
        self.assertEqual(self.discord.sends, [])

    async def test_cancellation_keeps_a_durable_claim_for_recovery(self):
        async def cancelled(chunks):
            raise asyncio.CancelledError()
        self.discord.send_owner_dm = cancelled
        with self.assertRaises(asyncio.CancelledError):
            await self.post(self.payload())
        self.assertEqual(self.row()["state"], "SENDING")

    async def test_retry_cannot_change_the_recorded_recipient(self):
        self.line.ok = False  # a definite 429 rejection, so retry is otherwise safe
        await self.post(self.payload("line"))
        original_target = self.row()["target_user_id"]
        self.gw.cfg.owner_line_user_id = "U" + "fedcba9876543210" * 2
        self.line.ok = True
        status, answer = await self.post(self.payload("line"))
        self.assertEqual((status, answer["detail"]), (409, "delivery_target_mismatch"))
        self.assertEqual(len(self.line.sends), 1)
        self.assertEqual(self.row()["target_user_id"], original_target)

    async def test_unknown_cannot_gain_authorization_from_a_retry_payload(self):
        async def lost(chunks):
            self.discord.sends.append(list(chunks))
            raise TimeoutError("synthetic")
        self.discord.send_owner_dm = lost
        await self.post(self.payload())
        self.reopen()
        for override in ({"approved": True}, {"force": True}, {"recipient_id": "stranger"}):
            status, answer = await self.post(self.payload(**override))
            self.assertEqual(status, 400)
            self.assertEqual(answer["status"], "rejected")
        status, answer = await self.post(self.payload(text="different fixture body"))
        self.assertEqual((status, answer["status"]), (409, "outcome_unknown"))
        self.assertEqual(len(self.discord.sends), 1)
        self.assertEqual(self.row()["state"], "UNKNOWN")

    async def test_legacy_ambiguous_rows_do_not_regain_replay_permission(self):
        for legacy_state in ("FAILED", "PENDING"):
            with self.subTest(state=legacy_state):
                self.db_path = os.path.join(self.tmp.name, legacy_state + ".db")
                self.reopen()
                con = self.gw.db()
                try:
                    row, _ = store.reserve_proactive(
                        con, notification_id="ntf_restart_fixture", channel="discord",
                        severity="IMPORTANT", dedupe_key=None, reason=None,
                        body="合成テストの通知", target_user_id=self.gw.cfg.owner_discord_user_id)
                    con.execute("UPDATE proactive_deliveries SET state = ?, attempts = 1",
                                (legacy_state,))
                    con.commit()
                finally:
                    con.close()
                self.reopen()
                status, answer = await self.post(self.payload())
                self.assertEqual((status, answer["status"]), (409, "outcome_unknown"))
                self.assertEqual(self.row()["state"], "UNKNOWN")
                self.assertEqual(self.discord.sends, [])


if __name__ == "__main__":
    unittest.main()
