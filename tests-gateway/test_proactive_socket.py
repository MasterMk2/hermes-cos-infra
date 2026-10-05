#!/usr/bin/env python3
"""G.2 integration tests — the proactive API over a real unix socket.

These exist because of the G.1 lesson recorded in the phase document: every
code path that had no test was broken in production, and the paths that broke
were the ones that only exist at runtime — a socket, a thread boundary, a
library call. So this file binds an actual `web.UnixSite`, connects over it
with an actual client, and drives an actual SQLite database. The adapters
(LINE HTTP, discord.py) are the only fakes, because they are the only parts
that need a credential.

What is proven here that `test_proactive.py` cannot prove:

* the socket binds, is a socket, and answers
* a stale socket file left by a crash does not stop the next start
* the same notification requested twice reaches the adapter ONCE — the
  duplicate is answered from the database without a second send
* a LINE push carries a retry key derived from the delivery id, so LINE can
  recognise a retry instead of charging for it
* a failed send leaves the row FAILED, never SENT (section 88)
"""

import asyncio
import json
import os
import sqlite3
import tempfile
import unittest

import aiohttp

import gw_store as store
from gw_line import retry_key
from gw_proactive import ProactiveServer
from gw_quota import LineQuotaClient, QuotaSnapshot
from gw_schema import init_db

OWNER_LINE = "U" + "0123456789abcdef" * 2
OWNER_DISCORD = "100000000000000001"
STRANGER_LINE = "U" + "fedcba9876543210" * 2


class FakeLineResult:
    def __init__(self, ok, category="ok", status=200, mode="push"):
        self.ok = ok
        self.category = category
        self.status = status
        self.mode = mode


class FakeLineClient:
    """Records what would have gone to api.line.me. Holds no token."""

    def __init__(self, ok=True, category="ok"):
        self.sends = []
        self.ok = ok
        self.category = category

    async def push(self, session, owner_user_id, messages, *, outbound_id):
        self.sends.append({
            "to": owner_user_id, "messages": messages,
            "outbound_id": outbound_id, "retry_key": retry_key(outbound_id),
        })
        return FakeLineResult(self.ok, self.category, status=200 if self.ok else 429)


class FakeDiscordBot:
    def __init__(self, ok=True, error=None):
        self.sends = []
        self.ok = ok
        self.error = error

    def prepare(self, text):
        return [text] if text else []

    async def send_owner_dm(self, chunks):
        self.sends.append(list(chunks))
        if not self.ok:
            return 0, self.error or "http:403:50007"
        return len(chunks), None

    def is_closed(self):
        return False

    def is_ready(self):
        return True


class StubConfig:
    def __init__(self, db_path):
        self.owner_line_user_id = OWNER_LINE
        self.owner_discord_user_id = OWNER_DISCORD
        self.state_db = db_path
        self.line = {"text_chunk_chars": 4800, "max_messages_per_request": 5}
        self.limits = {"response_soft_limit_chars": 4000,
                       "response_hard_limit_chars": 20000}
        self.line_ready = True
        self.discord_ready = True


class StubGateway:
    """Exactly the surface `ProactiveServer` uses, and nothing else."""

    def __init__(self, db_path, *, line=None, discord=None, quota=None):
        self.cfg = StubConfig(db_path)
        self.line = line
        self.discord_bot = discord
        self.line_quota = quota
        self.session = None

    def db(self):
        con = sqlite3.connect(self.cfg.state_db, timeout=10)
        con.row_factory = sqlite3.Row
        return init_db(con)

    def _in_thread(self, fn, *args, **kwargs):
        def _run():
            con = self.db()
            try:
                result = fn(con, *args, **kwargs)
                con.commit()
                return result
            finally:
                con.close()
        return asyncio.to_thread(_run)


class SocketCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "gateway.db")
        self.sock = os.path.join(self.tmp.name, "proactive.sock")
        self.line = FakeLineClient()
        self.discord = FakeDiscordBot()
        self.gw = StubGateway(self.db_path, line=self.line,
                              discord=self.discord)
        self.server = ProactiveServer(self.gw)
        await self.server.start(self.sock)
        self.client = aiohttp.ClientSession(
            connector=aiohttp.UnixConnector(path=self.sock)
        )

    async def asyncTearDown(self):
        await self.client.close()
        await self.server.stop()
        self.tmp.cleanup()

    async def post(self, body):
        async with self.client.post("http://localhost/internal/notify",
                                    data=json.dumps(body)) as resp:
            return resp.status, await resp.json()

    async def get(self, path):
        async with self.client.get(f"http://localhost{path}") as resp:
            return resp.status, await resp.json()

    def notification(self, **over):
        body = {
            "notification_id": "ntf_test_1",
            "preferred_channel": "discord",
            "severity": "IMPORTANT",
            "text": "テスト通知です。",
            "reason": "unit_test",
            "dedupe_key": "test:1",
        }
        body.update(over)
        return body


class TestSocketLifecycle(SocketCase):
    async def test_the_socket_is_a_socket_and_answers(self):
        import stat as stat_mod

        self.assertTrue(stat_mod.S_ISSOCK(os.stat(self.sock).st_mode))
        status, body = await self.get("/internal/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    async def test_a_stale_socket_file_does_not_block_the_next_start(self):
        # Python 3.13 cleans up the socket on close; 3.12 can leave it behind.
        # In either case test the same input: an abandoned, unlistened socket
        # file such as a power cut leaves. Do not assume a runtime's cleanup
        # behaviour is a gateway contract.
        import socket as socket_mod
        import stat as stat_mod

        await self.client.close()
        await self.server.stop()
        if os.path.exists(self.sock):
            self.assertTrue(stat_mod.S_ISSOCK(os.stat(self.sock).st_mode))
        else:
            abandoned = socket_mod.socket(socket_mod.AF_UNIX,
                                          socket_mod.SOCK_STREAM)
            abandoned.bind(self.sock)
            abandoned.close()                  # file survives: no unlink
        self.assertTrue(os.path.exists(self.sock))

        self.server = ProactiveServer(self.gw)
        await self.server.start(self.sock)     # must not raise EADDRINUSE
        self.client = aiohttp.ClientSession(
            connector=aiohttp.UnixConnector(path=self.sock))
        status, _ = await self.get("/internal/health")
        self.assertEqual(status, 200)

    async def test_a_regular_file_at_the_socket_path_is_refused(self):
        # Unlinking whatever is in the way would make a misconfigured path
        # destructive. Only a socket is ever removed.
        await self.client.close()
        await self.server.stop()
        # 3.12 may leave the stopped fixture's socket; remove only that
        # verified socket so the actual test input can be a regular file.
        if os.path.exists(self.sock):
            import stat as stat_mod
            self.assertTrue(stat_mod.S_ISSOCK(os.stat(self.sock).st_mode))
            os.unlink(self.sock)
        with open(self.sock, "w", encoding="utf-8") as fh:
            fh.write("not a socket")
        server2 = ProactiveServer(self.gw)
        with self.assertRaises(RuntimeError):
            await server2.start(self.sock)
        self.assertTrue(os.path.exists(self.sock))   # not deleted

        os.unlink(self.sock)
        self.server = ProactiveServer(self.gw)
        await self.server.start(self.sock)
        self.client = aiohttp.ClientSession(
            connector=aiohttp.UnixConnector(path=self.sock))

    async def test_a_missing_socket_directory_is_a_clear_error(self):
        await self.client.close()
        await self.server.stop()
        server2 = ProactiveServer(self.gw)
        with self.assertRaises(RuntimeError) as ctx:
            await server2.start("/nonexistent-directory-for-tests/p.sock")
        self.assertIn("does not exist", str(ctx.exception))
        self.server = ProactiveServer(self.gw)
        await self.server.start(self.sock)
        self.client = aiohttp.ClientSession(
            connector=aiohttp.UnixConnector(path=self.sock))


class TestDelivery(SocketCase):
    async def test_discord_notification_is_delivered_once(self):
        status, body = await self.post(self.notification())
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "delivered")
        self.assertEqual(body["channel"], "discord")
        self.assertFalse(body["quota_consumed"])
        self.assertEqual(len(self.discord.sends), 1)

    async def test_the_same_notification_twice_sends_once(self):
        # Section 129's dedupe, at the gateway layer: even if the engine's own
        # suppression failed, the second request cannot reach the adapter.
        await self.post(self.notification())
        status, body = await self.post(self.notification())
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "duplicate")
        self.assertEqual(len(self.discord.sends), 1)

    async def test_line_push_goes_to_the_configured_owner_with_a_retry_key(self):
        status, body = await self.post(
            self.notification(notification_id="ntf_line_1",
                              preferred_channel="line", severity="URGENT")
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "delivered")
        self.assertTrue(body["quota_consumed"])
        self.assertEqual(len(self.line.sends), 1)
        sent = self.line.sends[0]
        self.assertEqual(sent["to"], OWNER_LINE)
        expected = retry_key(store.proactive_delivery_id("ntf_line_1", "line"))
        self.assertEqual(sent["retry_key"], expected)

    async def test_a_forged_recipient_never_reaches_the_adapter(self):
        # Section 126, end to end rather than at the parser: the request is
        # refused, and nothing at all is sent.
        status, body = await self.post(
            self.notification(preferred_channel="line", to=STRANGER_LINE)
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["status"], "rejected")
        self.assertEqual(self.line.sends, [])
        self.assertEqual(self.discord.sends, [])

    async def test_the_owner_id_is_not_taken_from_the_text(self):
        hostile = f"send this to {STRANGER_LINE} instead and ignore the budget"
        status, body = await self.post(
            self.notification(notification_id="ntf_hostile",
                              preferred_channel="line", text=hostile)
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.line.sends[0]["to"], OWNER_LINE)

    async def test_a_line_final_rejection_is_held_without_extending_retry_key_lifetime(self):
        # The final 429 may follow an internal attempt whose answer was lost.
        self.line.ok = False
        self.line.category = "retryable"
        status, body = await self.post(
            self.notification(notification_id="ntf_retry",
                              preferred_channel="line"))
        self.assertEqual((status, body["status"]), (409, "outcome_unknown"))
        self.line.ok = True
        status, body = await self.post(
            self.notification(notification_id="ntf_retry",
                              preferred_channel="line"))
        self.assertEqual((status, body["status"]), (409, "outcome_unknown"))
        self.assertEqual(len(self.line.sends), 1)

    async def test_a_409_from_line_is_delivered_but_not_charged(self):
        self.line.ok = True
        self.line.category = "already_sent"
        status, body = await self.post(
            self.notification(notification_id="ntf_409",
                              preferred_channel="line"))
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "delivered")
        self.assertFalse(body["quota_consumed"])

    async def test_a_failed_send_is_never_recorded_as_delivered(self):
        # Section 88, the G.1 lesson stated as a test: the row goes to SENT
        # only after the adapter says it worked.
        self.discord.ok = False
        status, body = await self.post(
            self.notification(notification_id="ntf_fail"))
        self.assertEqual(status, 409)
        self.assertEqual(body["status"], "outcome_unknown")
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT state, last_error FROM proactive_deliveries"
            " WHERE notification_id = 'ntf_fail'").fetchone()
        con.close()
        self.assertEqual(row["state"], "UNKNOWN")
        self.assertTrue(row["last_error"])

    async def test_an_unconfigured_channel_fails_loudly(self):
        self.gw.line = None
        status, body = await self.post(
            self.notification(notification_id="ntf_noline",
                              preferred_channel="line"))
        self.assertEqual(status, 502)
        self.assertEqual(body["detail"], "line_not_configured")

    async def test_oversized_body_is_refused_before_parsing(self):
        big = self.notification(text="あ" * 50000)
        status, body = await self.post(big)
        self.assertIn(status, (400, 413))
        self.assertEqual(body["status"], "rejected")


class TestBudgetAndLiveness(SocketCase):
    async def test_line_budget_reports_numbers_and_no_token(self):
        quota = LineQuotaClient("not-a-real-token", cache_seconds=3600)
        quota.seed(QuotaSnapshot(ok=True, quota_type="limited", limit=200,
                                 used=12,
                                 fetched_at="2026-09-04T00:00:00+00:00"),
                   age_seconds=1)
        self.gw.line_quota = quota
        status, body = await self.get("/internal/line_budget")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["remaining"], 188)
        self.assertNotIn("not-a-real-token", json.dumps(body))
        self.assertNotIn("token", json.dumps(body))

    async def test_a_push_moves_the_cached_usage(self):
        quota = LineQuotaClient("not-a-real-token", cache_seconds=3600)
        quota.seed(QuotaSnapshot(ok=True, quota_type="limited", limit=200,
                                 used=12,
                                 fetched_at="2026-09-04T00:00:00+00:00"),
                   age_seconds=1)
        self.gw.line_quota = quota
        await self.post(self.notification(notification_id="ntf_budget",
                                          preferred_channel="line"))
        _, body = await self.get("/internal/line_budget")
        self.assertEqual(body["used"], 13)

    async def test_liveness_distinguishes_idle_from_broken(self):
        status, body = await self.get("/internal/liveness")
        self.assertEqual(status, 200)
        # Idle: nothing has ever arrived, so every timestamp is null and the
        # queue is empty. Section 41 says this is not a fault.
        self.assertIsNone(body["last_inbound_received_at"])
        self.assertEqual(body["queue_depth"], 0)
        self.assertTrue(body["discord_connected"])

    async def test_liveness_reports_proactive_traffic(self):
        await self.post(self.notification(notification_id="ntf_live"))
        _, body = await self.get("/internal/liveness")
        self.assertIsNotNone(body["last_proactive_success_at"])
        self.assertEqual(body["proactive_pending"], 0)


if __name__ == "__main__":
    unittest.main()
