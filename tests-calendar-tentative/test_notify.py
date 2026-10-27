"""The owner digest: rendering, quiet hours, the gateway contract, retries."""

from __future__ import annotations

import json
import os
import socketserver
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler

from ct_fixtures import JST, NOW, FakeCalendar, Workspace, make_cfg

import ct_engine
import ct_notify as nt
import ct_select as sel
import ct_store as store

DUE = {"due_at": "2026-10-05T17:00:00+09:00", "due_precision": "day"}


class RenderTests(unittest.TestCase):
    def test_digest_lists_entries_and_says_how_to_decide(self):
        text = nt.render_digest(["10/5(月) 〆 協賛ロゴの入稿", "10/6(火) 終日 学科交流会"],
                                max_lines=8)
        self.assertEqual(text.splitlines()[0], "カレンダーに仮登録しました（2件）")
        self.assertIn("・10/5(月) 〆 協賛ロゴの入稿", text)
        self.assertTrue(text.endswith(nt.FOOTER))

    def test_long_digests_are_summarised_and_bounded(self):
        lines = [f"10/{d}(月) 〆 " + "あ" * 80 for d in range(1, 31)]
        text = nt.render_digest(lines, max_lines=20)
        self.assertLessEqual(len(text), nt.MAX_TEXT_CHARS)
        self.assertIn("（30件）", text)
        self.assertRegex(text, r"ほか\d+件")

    def test_the_same_entries_give_the_same_id(self):
        a = nt.notification_id_for(["c2", "c1"])
        self.assertEqual(a, nt.notification_id_for(["c1", "c2"]))
        self.assertNotEqual(a, nt.notification_id_for(["c1"]))
        # The gateway's id alphabet and length (gw_proactive.ID_ALLOWED, ID_MAX).
        self.assertRegex(a, r"^[A-Za-z0-9_.:-]{1,120}$")

    def test_quiet_hours_wrap_midnight(self):
        def at(h, m=0):
            return datetime(2026, 10, 1, h, m, tzinfo=JST)
        self.assertTrue(nt.in_quiet_hours(at(23, 0), "23:00", "08:00"))
        self.assertTrue(nt.in_quiet_hours(at(3), "23:00", "08:00"))
        self.assertFalse(nt.in_quiet_hours(at(8), "23:00", "08:00"))
        self.assertFalse(nt.in_quiet_hours(at(22, 59), "23:00", "08:00"))
        self.assertTrue(nt.in_quiet_hours(at(13), "12:00", "14:00"))
        self.assertFalse(nt.in_quiet_hours(at(13), "08:00", "08:00"))

    def test_gateway_answers_map_to_the_brief_contract(self):
        table = [
            (200, {"status": "delivered"}, nt.DELIVERED),
            (200, {"status": "duplicate", "detail": "already_delivered"}, nt.ALREADY_DELIVERED),
            (409, {"status": "in_flight"}, nt.IN_FLIGHT),
            (409, {"status": "outcome_unknown"}, nt.OUTCOME_UNKNOWN),
            (409, {"status": "rejected", "detail": "content_revision_mismatch"},
             nt.CONTENT_MISMATCH),
            (400, {"status": "rejected"}, nt.REJECTED),
            (503, {"status": "failed"}, nt.REJECTED),
            (502, {"status": "failed"}, nt.FAILED),
            (200, {"status": "surprising"}, nt.UNKNOWN),
        ]
        for status, body, expected in table:
            with self.subTest(status=status, body=body):
                self.assertEqual(nt.classify_response(status, body), expected)


class FakeNotifier:
    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [])
        self.sent = []

    def send(self, *, notification_id, text):
        self.sent.append((notification_id, text))
        return self.outcomes.pop(0) if self.outcomes else nt.DELIVERED


class EngineNotifyTests(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.close)
        self.cfg = make_cfg(notify={"enabled": True})
        self.google = FakeCalendar()
        self.state = store.open_db(self.ws.state_path)
        self.addCleanup(self.state.close)

    def run_once(self, notifier, *, now=NOW, cfg=None):
        cfg = cfg or self.cfg
        selection = sel.select(self.ws.events, cfg=cfg, now=now, index_con=self.ws.index)
        return ct_engine.run(cfg=cfg, state_con=self.state, selection=selection,
                             client=self.google, now=now, notifier=notifier)

    def test_one_digest_per_batch_of_new_entries(self):
        self.ws.add_commitment("c1", "協賛ロゴの入稿", **DUE)
        self.ws.add_commitment("c2", "見積書の送付", **DUE)
        notifier = FakeNotifier()
        report = self.run_once(notifier)
        self.assertEqual(report.notification, nt.DELIVERED)
        self.assertEqual(len(notifier.sent), 1)
        self.assertIn("（2件）", notifier.sent[0][1])
        self.run_once(notifier)
        self.assertEqual(len(notifier.sent), 1)
        self.ws.add_commitment("c3", "発表練習", **DUE)
        self.run_once(notifier)
        self.assertEqual(len(notifier.sent), 2)
        self.assertIn("（1件）", notifier.sent[1][1])

    def test_a_failed_digest_is_retried_verbatim(self):
        self.ws.add_commitment("c1", "協賛ロゴの入稿", **DUE)
        notifier = FakeNotifier([nt.UNREACHABLE, nt.UNKNOWN, nt.DELIVERED])
        self.run_once(notifier)
        self.ws.add_commitment("c2", "見積書の送付", **DUE)
        self.run_once(notifier)
        self.run_once(notifier)
        first = notifier.sent[0]
        self.assertEqual(notifier.sent[1], first)
        self.assertEqual(notifier.sent[2], first)
        # c2 was not folded into the retried digest; it gets its own next.
        self.run_once(notifier)
        self.assertEqual(len(notifier.sent), 4)
        self.assertIn("見積書の送付", notifier.sent[3][1])

    def test_an_uncertain_digest_is_held_never_repeated(self):
        self.ws.add_commitment("c1", "協賛ロゴの入稿", **DUE)
        notifier = FakeNotifier([nt.OUTCOME_UNKNOWN])
        self.run_once(notifier)
        self.run_once(notifier)
        self.assertEqual(len(notifier.sent), 1)
        self.assertIsNotNone(store.get(self.state, "c1")["notified_at"])

    def test_retries_are_bounded(self):
        self.ws.add_commitment("c1", "協賛ロゴの入稿", **DUE)
        notifier = FakeNotifier([nt.FAILED] * 10)
        for _ in range(8):
            self.run_once(notifier)
        self.assertEqual(len(notifier.sent), nt.MAX_ATTEMPTS)

    def test_quiet_hours_hold_the_digest_until_morning(self):
        night = datetime(2026, 10, 1, 23, 30, tzinfo=JST)
        self.ws.add_commitment("c1", "協賛ロゴの入稿", **DUE)
        notifier = FakeNotifier()
        report = self.run_once(notifier, now=night)
        self.assertEqual(report.notification, "quiet_hours")
        self.assertEqual(notifier.sent, [])
        self.run_once(notifier, now=night + timedelta(hours=9))
        self.assertEqual(len(notifier.sent), 1)

    def test_disabled_means_no_digest(self):
        self.ws.add_commitment("c1", "協賛ロゴの入稿", **DUE)
        notifier = FakeNotifier()
        self.run_once(notifier, cfg=make_cfg())
        self.assertEqual(notifier.sent, [])

    def test_a_pending_digest_is_retried_as_built(self):
        self.ws.add_commitment("c1", "協賛ロゴの入稿", **DUE)
        notifier = FakeNotifier([nt.UNREACHABLE])
        self.run_once(notifier)
        self.google.owner_delete(store.get(self.state, "c1")["event_id"])
        # The pending digest still names c1 — it is retried as it was built.
        self.run_once(notifier)
        self.assertEqual(notifier.sent[1], notifier.sent[0])


class _Recorder(BaseHTTPRequestHandler):
    received: list = []
    reply = (200, {"status": "delivered"})

    def log_message(self, *_args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        type(self).received.append((self.path, json.loads(self.rfile.read(length))))
        status, body = type(self).reply
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class _UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def get_request(self):
        request, _ = super().get_request()
        return request, ("local", 0)


class SocketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "proactive.sock")
        _Recorder.received = []
        try:
            self.server = _UnixHTTPServer(self.path, _Recorder)
        except (PermissionError, OSError) as exc:  # pragma: no cover - sandboxed hosts
            self.skipTest(f"AF_UNIX unavailable here: {exc}")
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_the_request_names_a_channel_never_a_recipient(self):
        outcome = nt.GatewayNotifier(self.path, timeout=5).send(
            notification_id="cal-tentative:abc", text="カレンダーに仮登録しました（1件）")
        self.assertEqual(outcome, nt.DELIVERED)
        path, body = _Recorder.received[0]
        self.assertEqual(path, "/internal/notify")
        self.assertEqual(set(body), {"notification_id", "preferred_channel", "severity",
                                     "text", "reason", "dedupe_key"})
        self.assertEqual(body["preferred_channel"], "discord")
        self.assertEqual(body["severity"], "INFO")

    def test_a_missing_socket_is_unreachable(self):
        outcome = nt.GatewayNotifier(os.path.join(self.tmp.name, "absent.sock"), timeout=1).send(
            notification_id="cal-tentative:abc", text="x")
        self.assertEqual(outcome, nt.UNREACHABLE)


if __name__ == "__main__":
    unittest.main()
