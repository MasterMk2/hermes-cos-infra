"""Configuration refusals and the shape of every request sent to Google."""

from __future__ import annotations

import io
import json
import socket
import unittest
import urllib.error
import urllib.parse
from unittest import mock

from ct_fixtures import CALENDAR_ID

import ct_google as gg
from ct_config import ConfigError, parse_config


class ConfigTests(unittest.TestCase):
    def test_defaults_are_conservative(self):
        cfg = parse_config({})
        self.assertEqual(cfg.calendar_id, "")
        self.assertEqual(cfg.selection["gmail_only"], "people")
        self.assertFalse(cfg.notify["enabled"])
        self.assertEqual(cfg.limits["max_creates_per_run"], 10)

    def test_only_a_secondary_calendar_is_a_write_target(self):
        for bad in ("primary", "PRIMARY", "owner@example.com",
                    "ja.japanese#holiday@group.v.calendar.google.com"):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                parse_config({"calendar": {"calendar_id": bad}})
        cfg = parse_config({"calendar": {"calendar_id": CALENDAR_ID}})
        self.assertEqual(cfg.calendar_id, CALENDAR_ID)

    def test_credentials_and_unknown_keys_are_refused(self):
        for data in ({"refresh_token": "x"}, {"oauth": {"client_secret": "x"}},
                     {"selection": {"horizon": 3}}, {"extra": {}}):
            with self.subTest(data=data), self.assertRaises(ConfigError):
                parse_config(data)

    def test_values_are_range_checked(self):
        for data in ({"selection": {"horizon_days": 0}},
                     {"selection": {"min_confidence": 1.5}},
                     {"selection": {"gmail_only": "yes"}},
                     {"selection": {"include_gmail_only": True}},
                     {"behaviour": {"mode": "fire"}},
                     {"notify": {"quiet_hours_start": "25:00"}},
                     {"calendar": {"color_id": "12"}},
                     {"calendar": {"time_zone": "Mars/Base"}},
                     {"limits": {"max_attempts": 0}}):
            with self.subTest(data=data), self.assertRaises(ConfigError):
                parse_config(data)


class _Response(io.BytesIO):
    def __init__(self, body: dict | None, status=200):
        super().__init__(json.dumps(body).encode() if body is not None else b"")
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code, body=None):
    return urllib.error.HTTPError("https://x", code, "err", {},
                                  io.BytesIO(json.dumps(body or {}).encode()))


class GoogleClientTests(unittest.TestCase):
    def setUp(self):
        self.client = gg.TentativeCalendarClient(gg.OAuthCredentials("cid", "rt", "cs"))
        self.requests = []
        self.responses = []
        patcher = mock.patch.object(gg.urllib.request, "urlopen", self._urlopen)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _urlopen(self, req, timeout=None):
        self.requests.append(req)
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def token(self):
        return _Response({"access_token": "at", "expires_in": 3600})

    def test_the_only_scope_requested_is_app_created(self):
        self.assertEqual(gg.SCOPES, ("https://www.googleapis.com/auth/calendar.app.created",))
        url = gg.authorization_url("cid", "http://127.0.0.1:8766/oauth2/callback", "st")
        scope = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["scope"]
        self.assertEqual(scope, ["https://www.googleapis.com/auth/calendar.app.created"])

    def test_insert_sends_no_invitations(self):
        self.responses = [self.token(), _Response({"id": "e1", "etag": '"1"'})]
        self.client.insert_event(CALENDAR_ID, {"id": "e1"})
        req = self.requests[1]
        self.assertEqual(req.get_method(), "POST")
        self.assertIn("sendUpdates=none", req.full_url)
        self.assertIn(urllib.parse.quote(CALENDAR_ID, safe=""), req.full_url)

    def test_patch_and_delete_are_conditional_on_our_etag(self):
        self.responses = [self.token(), _Response({"etag": '"2"'}), _Response(None)]
        self.client.patch_event(CALENDAR_ID, "e1", {"summary": "s"}, etag='"1"')
        self.client.delete_event(CALENDAR_ID, "e1", etag='"2"')
        self.assertEqual(self.requests[1].get_header("If-match"), '"1"')
        self.assertEqual(self.requests[1].get_method(), "PATCH")
        self.assertEqual(self.requests[2].get_header("If-match"), '"2"')
        self.assertEqual(self.requests[2].get_method(), "DELETE")

    def test_a_missing_event_reads_as_none(self):
        self.responses = [self.token(), _http_error(404, {"error": {"status": "NOT_FOUND"}})]
        self.assertIsNone(self.client.get_event(CALENDAR_ID, "e1"))

    def test_an_expired_token_is_refreshed_once(self):
        self.responses = [self.token(), _http_error(401), self.token(), _Response({"items": []})]
        self.assertEqual(self.client.list_events(CALENDAR_ID), [])
        self.assertEqual(len(self.requests), 4)

    def test_failures_say_whether_anything_could_have_changed(self):
        self.responses = [self.token(), _http_error(400, {"error": {"errors": [{"reason": "invalid"}]}})]
        with self.assertRaises(gg.ApiError) as ctx:
            self.client.insert_event(CALENDAR_ID, {"id": "e1"})
        self.assertEqual((ctx.exception.status, ctx.exception.code), (400, "invalid"))
        self.assertTrue(ctx.exception.not_applied)

        self.responses = [_http_error(503)]
        with self.assertRaises(gg.ApiError) as ctx:
            self.client.insert_event(CALENDAR_ID, {"id": "e1"})
        self.assertTrue(ctx.exception.unknown)

        self.responses = [TimeoutError()]
        with self.assertRaises(gg.ApiError) as ctx:
            self.client.insert_event(CALENDAR_ID, {"id": "e1"})
        self.assertTrue(ctx.exception.unknown)

        self.responses = [urllib.error.URLError(ConnectionRefusedError())]
        with self.assertRaises(gg.ApiError) as ctx:
            self.client.insert_event(CALENDAR_ID, {"id": "e1"})
        self.assertFalse(ctx.exception.sent)
        self.assertTrue(ctx.exception.not_applied)

        self.responses = [urllib.error.URLError(socket.gaierror())]
        with self.assertRaises(gg.ApiError) as ctx:
            self.client.insert_event(CALENDAR_ID, {"id": "e1"})
        self.assertFalse(ctx.exception.sent)

    def test_a_token_failure_is_never_sent(self):
        client = gg.TentativeCalendarClient(gg.OAuthCredentials("cid", "rt"))
        self.responses = [_http_error(400, {"error": "invalid_grant"})]
        with self.assertRaises(gg.ApiError) as ctx:
            client.authenticate()
        self.assertFalse(ctx.exception.sent)
        self.assertEqual(ctx.exception.code, "invalid_grant")
        self.assertEqual(len(self.requests), 1)

    def test_credentials_never_render(self):
        creds = gg.OAuthCredentials("cid", "very-secret-refresh", "cs")
        self.assertNotIn("very-secret", repr(creds))
        self.assertNotIn("very-secret", repr(gg.TentativeCalendarClient(creds)))


if __name__ == "__main__":
    unittest.main()
