#!/usr/bin/env python3
"""G.2 unit tests — the LINE quota reader (spec sections 6, 7, 8, 80, 120).

The whole point of these is that a *wrong* reading is worse than no reading.
"No target limit is set" and "the API refused us" arrive as different answers
and must stay different: one means the channel is uncapped, the other means
the engine has no idea and must behave as if the budget were nearly gone.

No credential appears here. The client is constructed with a string that is
not a token and never leaves the fake session.
"""

import asyncio
import unittest

from gw_quota import (
    AUTH_BACKOFF_SECONDS,
    ERR_AUTH,
    ERR_MALFORMED,
    ERR_PERMANENT,
    ERR_TRANSIENT,
    QUOTA_TYPE_LIMITED,
    QUOTA_TYPE_NONE,
    LineQuotaClient,
    QuotaSnapshot,
    classify_status,
    parse_consumption,
    parse_quota,
)

NOT_A_TOKEN = "fixture-value-not-a-credential"


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    async def text(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Answers the two quota URLs from a script, and counts the calls.

    The count is the assertion that matters for section 120: a client that
    keeps calling a URL that answered 401 is the retry storm, and the only
    way to see it is to count.
    """

    def __init__(self, responses):
        self.responses = responses    # url-substring -> list of (status, body)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        key = "consumption" if url.endswith("consumption") else "quota"
        queue = self.responses.get(key)
        if not queue:
            raise AssertionError(f"unscripted call to {url}")
        status, body = queue[0] if len(queue) == 1 else queue.pop(0)
        return FakeResponse(status, body)


def run(coro):
    return asyncio.run(coro)


class TestParsing(unittest.TestCase):
    def test_limited_quota_has_a_value(self):
        self.assertEqual(parse_quota({"type": "limited", "value": 200}),
                         (QUOTA_TYPE_LIMITED, 200))

    def test_none_quota_has_no_value_and_that_is_valid(self):
        # `type: none` means no target limit is set. It is not an error and it
        # is not UNKNOWN — mapping it either way would be wrong in opposite
        # directions (section 6).
        self.assertEqual(parse_quota({"type": "none"}), (QUOTA_TYPE_NONE, None))

    def test_limited_without_a_value_is_refused(self):
        with self.assertRaises(ValueError):
            parse_quota({"type": "limited"})

    def test_undocumented_type_is_refused_rather_than_guessed(self):
        with self.assertRaises(ValueError):
            parse_quota({"type": "unlimited", "value": 1})

    def test_boolean_is_not_an_integer(self):
        # True is an int in Python. A schema check that forgets this accepts
        # {"value": true} as a quota of 1.
        with self.assertRaises(ValueError):
            parse_quota({"type": "limited", "value": True})

    def test_consumption(self):
        self.assertEqual(parse_consumption({"totalUsage": 500}), 500)
        with self.assertRaises(ValueError):
            parse_consumption({})
        with self.assertRaises(ValueError):
            parse_consumption({"totalUsage": -1})

    def test_status_classification(self):
        self.assertEqual(classify_status(401), ERR_AUTH)
        self.assertEqual(classify_status(403), ERR_AUTH)
        self.assertEqual(classify_status(429), ERR_TRANSIENT)
        self.assertEqual(classify_status(503), ERR_TRANSIENT)
        self.assertEqual(classify_status(404), ERR_PERMANENT)


class TestSnapshot(unittest.TestCase):
    def test_remaining_is_none_when_the_read_failed(self):
        snap = QuotaSnapshot(ok=False, error_category=ERR_AUTH)
        self.assertIsNone(snap.remaining)
        self.assertFalse(snap.unlimited)

    def test_remaining_is_none_for_an_uncapped_account(self):
        snap = QuotaSnapshot(ok=True, quota_type=QUOTA_TYPE_NONE, used=12)
        self.assertIsNone(snap.remaining)
        self.assertTrue(snap.unlimited)

    def test_remaining_never_goes_negative(self):
        snap = QuotaSnapshot(ok=True, quota_type=QUOTA_TYPE_LIMITED,
                             limit=200, used=260)
        self.assertEqual(snap.remaining, 0)


class TestClient(unittest.TestCase):
    def make(self, responses, **kw):
        return LineQuotaClient(NOT_A_TOKEN, **kw), FakeSession(responses)

    def test_reads_both_endpoints_and_computes_remaining(self):
        client, session = self.make({
            "quota": [(200, '{"type":"limited","value":200}')],
            "consumption": [(200, '{"totalUsage":37}')],
        })
        snap = run(client.get(session))
        self.assertTrue(snap.ok)
        self.assertEqual(snap.limit, 200)
        self.assertEqual(snap.used, 37)
        self.assertEqual(snap.remaining, 163)
        self.assertEqual(len(session.calls), 2)

    def test_a_fresh_answer_is_served_from_cache(self):
        client, session = self.make({
            "quota": [(200, '{"type":"limited","value":200}')],
            "consumption": [(200, '{"totalUsage":1}')],
        }, cache_seconds=3600)
        run(client.get(session))
        run(client.get(session))
        # Two URLs, once. The second get() made no HTTP call at all.
        self.assertEqual(len(session.calls), 2)

    def test_force_bypasses_the_cache(self):
        client, session = self.make({
            "quota": [(200, '{"type":"limited","value":200}')],
            "consumption": [(200, '{"totalUsage":1}')],
        }, cache_seconds=3600)
        run(client.get(session))
        run(client.get(session, force=True))
        self.assertEqual(len(session.calls), 4)

    def test_auth_failure_does_not_become_a_retry_storm(self):
        client, session = self.make({
            "quota": [(401, "invalid token")],
            "consumption": [(200, '{"totalUsage":1}')],
        })
        first = run(client.get(session))
        self.assertFalse(first.ok)
        self.assertEqual(first.error_category, ERR_AUTH)
        calls_after_first = len(session.calls)
        for _ in range(5):
            again = run(client.get(session))
            self.assertFalse(again.ok)
            self.assertEqual(again.error_category, ERR_AUTH)
        # Five more attempts produced zero more HTTP calls (section 120).
        self.assertEqual(len(session.calls), calls_after_first)

    def test_a_failed_read_never_reports_stale_numbers_as_current(self):
        client, session = self.make({
            "quota": [(200, '{"type":"limited","value":200}'),
                      (503, "unavailable")],
            "consumption": [(200, '{"totalUsage":10}'),
                            (200, '{"totalUsage":10}')],
        }, cache_seconds=0)
        good = run(client.get(session))
        self.assertEqual(good.remaining, 190)
        bad = run(client.get(session))
        self.assertFalse(bad.ok)
        # The engine must see UNKNOWN, not "190 left" — spending against a
        # figure nobody has confirmed since is exactly section 8's failure.
        self.assertIsNone(bad.remaining)
        self.assertIsNone(bad.limit)

    def test_malformed_body_is_unknown_not_unlimited(self):
        client, session = self.make({
            "quota": [(200, "not json at all")],
            "consumption": [(200, '{"totalUsage":1}')],
        })
        snap = run(client.get(session))
        self.assertFalse(snap.ok)
        self.assertEqual(snap.error_category, ERR_MALFORMED)
        self.assertFalse(snap.unlimited)

    def test_uncapped_account_is_ok_and_unlimited(self):
        client, session = self.make({
            "quota": [(200, '{"type":"none"}')],
            "consumption": [(200, '{"totalUsage":4321}')],
        })
        snap = run(client.get(session))
        self.assertTrue(snap.ok)
        self.assertTrue(snap.unlimited)
        self.assertIsNone(snap.remaining)
        self.assertEqual(snap.used, 4321)

    def test_seed_restores_the_auth_backoff_across_a_restart(self):
        # A crash-restart loop must not turn a 401 into a call per start.
        client = LineQuotaClient(NOT_A_TOKEN)
        client.seed(
            QuotaSnapshot(ok=False, error_category=ERR_AUTH,
                          fetched_at="2026-09-04T00:00:00+00:00"),
            age_seconds=60,
        )
        session = FakeSession({"quota": [(200, '{"type":"none"}')],
                               "consumption": [(200, '{"totalUsage":0}')]})
        snap = run(client.get(session))
        self.assertFalse(snap.ok)
        self.assertEqual(session.calls, [])

    def test_seed_of_an_old_auth_failure_allows_a_fresh_attempt(self):
        client = LineQuotaClient(NOT_A_TOKEN)
        client.seed(
            QuotaSnapshot(ok=False, error_category=ERR_AUTH,
                          fetched_at="2026-09-04T00:00:00+00:00"),
            age_seconds=AUTH_BACKOFF_SECONDS + 60,
        )
        session = FakeSession({"quota": [(200, '{"type":"none"}')],
                               "consumption": [(200, '{"totalUsage":0}')]})
        snap = run(client.get(session))
        self.assertTrue(snap.ok)
        self.assertEqual(len(session.calls), 2)

    def test_local_accounting_between_refreshes(self):
        client, session = self.make({
            "quota": [(200, '{"type":"limited","value":200}')],
            "consumption": [(200, '{"totalUsage":10}')],
        }, cache_seconds=3600)
        snap = run(client.get(session))
        self.assertEqual(snap.remaining, 190)
        client.note_push_sent(1)
        snap = run(client.get(session))
        self.assertEqual(snap.remaining, 189)
        self.assertEqual(len(session.calls), 2)

    def test_note_push_is_ignored_when_the_budget_is_unknown(self):
        client = LineQuotaClient(NOT_A_TOKEN)
        client.note_push_sent(1)   # must not invent a snapshot
        session = FakeSession({"quota": [(401, "no")],
                               "consumption": [(200, "{}")]})
        snap = run(client.get(session))
        self.assertFalse(snap.ok)

    def test_the_client_never_renders_its_token(self):
        client = LineQuotaClient("super-secret-value")
        self.assertNotIn("super-secret-value", repr(client))
        self.assertNotIn("super-secret-value", str(client))
        self.assertNotIn("super-secret-value", f"{client}")


if __name__ == "__main__":
    unittest.main()
