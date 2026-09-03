#!/usr/bin/env python3
"""G.1 unit tests — the webhook handler and the queue hand-off.

Two behaviours in `gw_service.py` were designed deliberately and are easy to
regress into their opposites, so they are pinned here rather than left to the
end-to-end run:

  * **A failed enqueue answers 500.** LINE does not redeliver a 200. If the
    row is not committed and the gateway claims success, that message is
    gone — the retry was the only remaining copy.
  * **An unauthorized sender gets 200 and silence.** No reply, no queue entry.
    Replying would confirm the endpoint is live and belongs to someone, and
    on LINE it would spend metered quota on a stranger.
"""

import contextlib
import io
import os
import sqlite3
import tempfile
import unittest

import gw_fixtures as fx
import gw_service
import gw_store as store
from gw_canonical import canonical_from_line
from gw_config import parse_config
from gw_line import SIGNATURE_HEADER, compute_signature
from gw_schema import open_db


class FakeRequest:
    """The three attributes `handle_line_webhook` actually touches."""

    def __init__(self, body: bytes, headers: dict, content_length=None):
        self._body = body
        self.headers = headers
        self.content_length = (
            len(body) if content_length is None else content_length
        )

    async def read(self):
        return self._body


class GatewayTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db_path)
        self.cfg = parse_config(
            fx.config_dict(paths={"state_db": self.db_path,
                                  "log_dir": tempfile.mkdtemp()})
        )
        self.secrets = {
            "hermes_api_key": "test-key",
            "line_channel_secret": fx.CHANNEL_SECRET,
            "line_channel_access_token": "test-token",
            "discord_interface_token": "test-discord-token",
        }
        self.gw = gw_service.Gateway(self.cfg, self.secrets)
        open_db(self.db_path).close()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except OSError:
                pass

    def signed(self, events, secret=None):
        body = fx.line_body(events)
        sig = compute_signature(body, secret or fx.CHANNEL_SECRET)
        return FakeRequest(body, {SIGNATURE_HEADER: sig})

    def queued_count(self):
        con = open_db(self.db_path)
        try:
            return con.execute(
                "SELECT COUNT(*) FROM inbound_messages WHERE state='QUEUED'"
            ).fetchone()[0]
        finally:
            con.close()


class TestSignatureGate(GatewayTestCase):
    async def test_valid_owner_event_is_accepted_and_queued(self):
        resp = await self.gw.handle_line_webhook(self.signed([fx.line_text_event()]))
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.queued_count(), 1)

    async def test_bad_signature_is_403_and_stores_nothing(self):
        req = self.signed([fx.line_text_event()], secret="wrong-secret")
        resp = await self.gw.handle_line_webhook(req)
        self.assertEqual(resp.status, 403)
        self.assertEqual(self.queued_count(), 0)

    async def test_missing_signature_header_is_403(self):
        body = fx.line_body([fx.line_text_event()])
        resp = await self.gw.handle_line_webhook(FakeRequest(body, {}))
        self.assertEqual(resp.status, 403)

    async def test_tampered_body_is_403(self):
        req = self.signed([fx.line_text_event()])
        req._body = req._body.replace(b"active", b"stand.")
        resp = await self.gw.handle_line_webhook(req)
        self.assertEqual(resp.status, 403)

    async def test_signature_header_case_does_not_matter(self):
        body = fx.line_body([fx.line_text_event()])
        sig = compute_signature(body, fx.CHANNEL_SECRET)
        resp = await self.gw.handle_line_webhook(
            FakeRequest(body, {"X-LINE-SIGNATURE": sig})
        )
        self.assertEqual(resp.status, 200)


class TestBodyLimits(GatewayTestCase):
    async def test_oversized_declared_length_is_413_without_reading(self):
        req = self.signed([fx.line_text_event()])
        req.content_length = self.cfg.line["max_body_bytes"] + 1
        resp = await self.gw.handle_line_webhook(req)
        self.assertEqual(resp.status, 413)

    async def test_oversized_actual_body_is_413(self):
        big = fx.line_text_event(text="あ" * 60000)
        req = self.signed([big])
        req.content_length = None
        req.content_length = 10          # lie about the size
        resp = await self.gw.handle_line_webhook(req)
        self.assertEqual(resp.status, 413)

    async def test_signed_but_unparseable_body_is_400(self):
        body = b"not json at all"
        sig = compute_signature(body, fx.CHANNEL_SECRET)
        resp = await self.gw.handle_line_webhook(
            FakeRequest(body, {SIGNATURE_HEADER: sig})
        )
        self.assertEqual(resp.status, 400)


class TestVerificationProbe(GatewayTestCase):
    async def test_empty_event_list_is_200(self):
        """The console's Verify button. An error here makes a healthy channel
        look broken in LINE's UI."""
        resp = await self.gw.handle_line_webhook(self.signed([]))
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.queued_count(), 0)


class TestAuthorization(GatewayTestCase):
    async def test_stranger_gets_200_and_no_queue_entry(self):
        req = self.signed([fx.line_text_event(user_id=fx.STRANGER_LINE)])
        resp = await self.gw.handle_line_webhook(req)
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.queued_count(), 0)

    async def test_stranger_is_recorded_for_audit(self):
        await self.gw.handle_line_webhook(
            self.signed([fx.line_text_event(user_id=fx.STRANGER_LINE)])
        )
        con = open_db(self.db_path)
        try:
            events = [r["event"] for r in con.execute(
                "SELECT event FROM audit_events"
            )]
        finally:
            con.close()
        self.assertIn("inbound_rejected", events)

    async def test_mixed_batch_queues_only_the_owner(self):
        req = self.signed([
            fx.line_text_event(message_id="m1", user_id=fx.STRANGER_LINE),
            fx.line_text_event(message_id="m2", user_id=fx.OWNER_LINE),
            fx.line_text_event(message_id="m3", user_id=fx.STRANGER_LINE),
        ])
        resp = await self.gw.handle_line_webhook(req)
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.queued_count(), 1)


class TestDuplicateSuppression(GatewayTestCase):
    async def test_redelivery_does_not_queue_twice(self):
        await self.gw.handle_line_webhook(self.signed([fx.line_text_event()]))
        await self.gw.handle_line_webhook(
            self.signed([fx.line_text_event(redelivery=True)])
        )
        con = open_db(self.db_path)
        try:
            total = con.execute(
                "SELECT COUNT(*) FROM inbound_messages"
            ).fetchone()[0]
        finally:
            con.close()
        self.assertEqual(total, 1)


class TestEnqueueFailureAsksForRedelivery(GatewayTestCase):
    async def test_commit_failure_answers_500_not_200(self):
        """The single most costly thing to get backwards.

        LINE never redelivers a 200. If the row did not commit and the
        gateway says OK, the message is destroyed — its only other copy was
        the retry the gateway just declined.
        """
        class Exploding(sqlite3.Connection):
            def commit(self):
                raise sqlite3.OperationalError("disk I/O error")

        real_open = gw_service.open_db

        def broken(path):
            con = sqlite3.connect(path, factory=Exploding)
            con.row_factory = sqlite3.Row
            return con

        self.gw.db = lambda: broken(self.db_path)
        try:
            resp = await self.gw.handle_line_webhook(
                self.signed([fx.line_text_event()])
            )
        finally:
            self.gw.db = lambda: real_open(self.db_path)

        self.assertEqual(resp.status, 500)
        self.assertEqual(self.queued_count(), 0)

    async def test_after_recovery_the_redelivery_is_accepted(self):
        """Following on: LINE retries, the database is healthy, it queues."""
        resp = await self.gw.handle_line_webhook(
            self.signed([fx.line_text_event(redelivery=True)])
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.queued_count(), 1)


class TestRowRoundTrip(GatewayTestCase):
    async def test_worker_can_rebuild_the_message_from_the_row(self):
        """After a restart the object is gone and the row is all there is."""
        await self.gw.handle_line_webhook(
            self.signed([fx.line_text_event(text="明日の予定は？")])
        )
        con = open_db(self.db_path)
        try:
            row = store.claim_next(con, max_active=2)
            con.commit()
        finally:
            con.close()
        self.assertIsNotNone(row)
        msg = gw_service.message_from_row(row)
        self.assertEqual(msg.text, "明日の予定は？")
        self.assertTrue(msg.authenticated)
        self.assertEqual(msg.platform, "line")
        self.assertEqual(msg.reply_token, "tok-0001")


class TestConnectionsStayInTheirThread(GatewayTestCase):
    """A sqlite3 connection belongs to the thread that opened it.

    The first production start failed on exactly this. The worker opened its
    connection on the event loop and handed it to `asyncio.to_thread`:

        sqlite3.ProgrammingError: SQLite objects created in a thread can only
        be used in that same thread

    Nothing here caught it, because every earlier test called the store
    synchronously on one thread. The result in production was the worst
    possible shape of failure — the webhook kept accepting messages and
    answering 200, while every one of them sat in the queue forever, because
    the worker died on each pass. /healthz returned 500 from the same cause.

    These tests exercise the threaded path, so the next such mistake fails
    here instead of in the journal.
    """

    async def test_claim_from_a_thread_does_not_raise(self):
        row = await self.gw._in_thread(store.claim_next, max_active=2)
        self.assertIsNone(row)            # empty queue, but no exception

    async def test_queued_message_is_claimable_from_a_thread(self):
        await self.gw.handle_line_webhook(self.signed([fx.line_text_event()]))
        row = await self.gw._in_thread(store.claim_next, max_active=2)
        self.assertIsNotNone(row)
        self.assertEqual(row["platform_message_id"], "msg-0001")

    async def test_row_survives_the_connection_close(self):
        """`_in_thread` closes the connection before returning."""
        await self.gw.handle_line_webhook(self.signed([fx.line_text_event()]))
        row = await self.gw._in_thread(store.claim_next, max_active=2)
        msg = gw_service.message_from_row(row)     # reads every column
        self.assertEqual(msg.text, "こんにちは")

    async def test_health_endpoint_is_200_not_500(self):
        """The production symptom: /healthz answered 500 from this bug."""
        resp = await self.gw.handle_health(None)
        self.assertEqual(resp.status, 200)

    async def test_session_lookup_from_a_thread(self):
        sid = await self.gw._in_thread(
            store.session_id_for, "line:U0", "line"
        )
        self.assertTrue(sid)

    async def test_housekeeping_from_a_thread(self):
        recovered, purged = await self.gw._in_thread(
            self.gw._housekeep, retention_days=14
        )
        self.assertEqual(recovered, {"requeued": 0, "failed": 0})
        self.assertIn("inbound_cleared", purged)


class TestHermesReachabilityProbe(unittest.TestCase):
    """`--check` must fail on an unreachable Hermes, not just on missing ids.

    The bug this covers was real: the sample said 127.0.0.1 while the API
    published its port on the LAN address only, so nothing could connect. The
    old `--check` returned 78 for "no owner id" and never got as far as
    noticing, which would have left the address wrong until the first message
    silently failed.
    """

    def _cfg(self, base_url, **kw):
        import tempfile as tf
        return parse_config(fx.config_dict(
            hermes={"base_url": base_url},
            paths={"state_db": os.path.join(tf.mkdtemp(), "g.db"),
                   "log_dir": tf.mkdtemp()},
            **kw,
        ))

    def test_closed_port_reports_unreachable(self):
        # Port 1 on loopback: nothing listens, so this refuses immediately.
        ok, detail = gw_service.probe_hermes(self._cfg("http://127.0.0.1:1"))
        self.assertFalse(ok)
        self.assertIn("127.0.0.1:1/health", detail)

    def test_check_fails_when_hermes_unreachable(self):
        rc = gw_service.run_check(self._cfg("http://127.0.0.1:1"))
        self.assertEqual(rc, 78)

    def test_check_still_fails_without_owner_ids(self):
        cfg = parse_config(fx.config_dict(
            owner={"line_user_id": None, "discord_user_id": None},
            hermes={"base_url": "http://127.0.0.1:1"},
        ))
        self.assertEqual(gw_service.run_check(cfg), 78)

    def test_check_survives_an_unreadable_credential_directory(self):
        """The regression this covers produced a traceback, not a verdict.

        Running `--check` as the service user is the documented way to test a
        deployment, and that user cannot read /etc/hermes-secrets by design.
        """
        import pathlib

        real = pathlib.Path.is_file

        def boom(self, *a, **k):
            if "hermes-secrets" in str(self):
                raise PermissionError(13, "Permission denied")
            return real(self)

        cfg = self._cfg("http://127.0.0.1:1")
        pathlib.Path.is_file = boom
        try:
            rc = gw_service.run_check(cfg)      # must not raise
        finally:
            pathlib.Path.is_file = real
        self.assertEqual(rc, 78)               # unreachable hermes still fails

    def test_check_survives_an_unopenable_state_db(self):
        """state/ is 0750 to the service user; another account cannot open it.

        That is the design, not a fault, and the diagnostic must survive it.
        """
        import tempfile as tf

        cfg = parse_config(fx.config_dict(
            # A directory is not a database: sqlite refuses to open it, which
            # stands in for the permission denial seen on the host.
            paths={"state_db": tf.mkdtemp(), "log_dir": tf.mkdtemp()},
            hermes={"base_url": "http://127.0.0.1:1"},
        ))
        rc = gw_service.run_check(cfg)          # must not raise
        self.assertEqual(rc, 78)

    def test_probe_treats_an_http_error_as_reachable(self):
        """A 404 still proves the address is right; only a connection
        failure means the address is wrong."""
        import urllib.error

        real = gw_service.urllib.request.urlopen

        def fake(*a, **k):
            raise urllib.error.HTTPError("u", 404, "nf", None, None)

        gw_service.urllib.request.urlopen = fake
        try:
            ok, detail = gw_service.probe_hermes(self._cfg("http://127.0.0.1:8642"))
        finally:
            gw_service.urllib.request.urlopen = real
        self.assertTrue(ok)
        self.assertIn("404", detail)


class TestStatusToolExplainsItself(unittest.TestCase):
    """A status tool that cannot read must say so, not print empty tables.

    The shell version shelled out to `sqlite3`, which is not installed on the
    target host. Every section came back blank and nothing said why — the
    operator's view of a running gateway was silently, plausibly wrong.
    """

    def test_missing_database_is_reported_and_nonzero(self):
        import gw_status

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = gw_status.main(["--db", "/nonexistent/dir/gateway.db"])
        self.assertEqual(rc, 1)
        self.assertIn("ありません", buf.getvalue())

    def test_connect_explains_who_can_read(self):
        import gw_status

        con, err = gw_status.connect("/nonexistent/dir/gateway.db")
        self.assertIsNone(con)
        self.assertTrue(err)

    def test_readable_database_reports_zero_and_succeeds(self):
        import tempfile as tf

        import gw_status
        from gw_schema import open_db

        path = os.path.join(tf.mkdtemp(), "g.db")
        open_db(path).close()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = gw_status.main(["--db", path])
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("受信キュー", out)
        # An empty gateway says so explicitly rather than printing nothing.
        self.assertIn("(なし)", out)

    def test_status_never_prints_message_text(self):
        """The tool reports states and counts, never what anyone said."""
        import tempfile as tf

        import gw_status
        from gw_schema import open_db

        path = os.path.join(tf.mkdtemp(), "g.db")
        con = open_db(path)
        secret_text = "SENTINEL-PRIVATE-MESSAGE-9f3a"
        msg = canonical_from_line(
            fx.line_text_event(text=secret_text),
            owner_line_user_id=fx.OWNER_LINE,
        )
        rid, _ = store.record_inbound(con, msg)
        store.enqueue(con, rid)
        con.commit()
        con.close()

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            gw_status.main(["--db", path])
        self.assertNotIn(secret_text, buf.getvalue())


class TestLoggingDegradesInsteadOfDying(unittest.TestCase):
    """A diagnostic must not be defeated by the permissions it diagnoses.

    logs/ is 0750 owned by the service user. An operator running `--check`
    from their own account cannot write there, and an earlier version opened
    the log file before anything else and exited with a PermissionError —
    observed on the target host, 2026-09-04.
    """

    def setUp(self):
        import logging as _logging

        self._root = _logging.getLogger()
        self._saved = list(self._root.handlers)
        self._level = self._root.level

    def tearDown(self):
        import logging as _logging

        root = _logging.getLogger()
        for h in list(root.handlers):
            if h not in self._saved:
                try:
                    h.close()
                except Exception:
                    pass
                root.removeHandler(h)
        root.setLevel(self._level)

    def test_unwritable_log_dir_still_logs_to_stderr(self):
        import logging as _logging
        import tempfile as tf

        # A regular file where a directory is expected: mkdir raises OSError,
        # which is the same class of failure as EACCES on the real host.
        fd, path = tempfile.mkstemp()
        os.close(fd)
        try:
            gw_service.setup_logging(path)          # must not raise
        finally:
            os.unlink(path)

        root = _logging.getLogger()
        stream_handlers = [h for h in root.handlers
                           if isinstance(h, _logging.StreamHandler)
                           and not isinstance(h, _logging.FileHandler)]
        self.assertTrue(stream_handlers, "stderr logging must survive")
        file_handlers = [h for h in root.handlers
                         if isinstance(h, _logging.FileHandler)]
        self.assertFalse(file_handlers, "no file handler should have opened")

    def test_writable_log_dir_gets_a_file_handler(self):
        import logging as _logging
        import tempfile as tf

        gw_service.setup_logging(tf.mkdtemp())
        root = _logging.getLogger()
        self.assertTrue([h for h in root.handlers
                         if isinstance(h, _logging.FileHandler)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
