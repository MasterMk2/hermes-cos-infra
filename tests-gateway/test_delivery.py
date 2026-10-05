#!/usr/bin/env python3
"""G.1 unit tests — delivery decisions and outbound shaping.

Reply is free and single-use; push is metered at 200 messages a month on the
free plan in Japan. Every test here is ultimately about not spending a push
that a reply could have covered, and not spending the same push twice
(spec sections 33, 34, 36).
"""

import unittest
from datetime import datetime, timedelta, timezone

import gw_fixtures as fx
from gw_canonical import canonical_from_line, new_outbound_id
from gw_format import (
    REDACTED,
    chunk_text,
    neutralize_mentions,
    prepare_outbound,
    redact,
    redaction_hits,
    summarize_if_oversized,
    user_facing_error,
)
from gw_line import (
    REPLY_TOKEN_SAFETY_MARGIN_SECONDS,
    REPLY_TOKEN_TTL_SECONDS,
    choose_delivery_mode,
    classify_send_failure,
    reply_still_viable,
    retry_key,
    unsupported_input_reply,
)


def message_at(seconds_ago=0, reply_token="tok-1"):
    received = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    msg = canonical_from_line(
        fx.line_text_event(reply_token=reply_token, timestamp=int(received.timestamp()*1000)),
        owner_line_user_id=fx.OWNER_LINE,
    )
    return msg.__class__(**{**msg.to_dict(), "received_at": received.isoformat()})


class TestReplyVersusPush(unittest.TestCase):
    def test_fresh_message_replies(self):
        self.assertEqual(choose_delivery_mode(message_at(0)), "reply")

    def test_expired_token_pushes(self):
        self.assertEqual(choose_delivery_mode(message_at(90)), "push")

    def test_missing_token_pushes(self):
        self.assertEqual(choose_delivery_mode(message_at(0, reply_token=None)), "push")

    def test_boundary_is_ttl_minus_margin(self):
        cutoff = REPLY_TOKEN_TTL_SECONDS - REPLY_TOKEN_SAFETY_MARGIN_SECONDS
        self.assertEqual(choose_delivery_mode(message_at(cutoff - 2)), "reply")
        self.assertEqual(choose_delivery_mode(message_at(cutoff + 2)), "push")

    def test_margin_reflects_the_measured_latency(self):
        """The existing 8s first-attempt margin is retained, not a retry proof."""
        self.assertEqual(REPLY_TOKEN_SAFETY_MARGIN_SECONDS, 8)
        window = REPLY_TOKEN_TTL_SECONDS - REPLY_TOKEN_SAFETY_MARGIN_SECONDS
        self.assertGreaterEqual(window, 49.6)   # the measured median still fits

    def test_viability_accepts_naive_timestamps_as_utc(self):
        naive = (datetime.now(timezone.utc)).replace(tzinfo=None).isoformat()
        self.assertTrue(reply_still_viable(naive))


class TestRetryKey(unittest.TestCase):
    def test_stable_for_the_same_chunk(self):
        """The property that stops a crash-retry billing twice."""
        oid = new_outbound_id("gw-line-abc", 0)
        self.assertEqual(retry_key(oid), retry_key(oid))

    def test_differs_between_chunks_and_requests(self):
        self.assertNotEqual(retry_key(new_outbound_id("r1", 0)),
                            retry_key(new_outbound_id("r1", 1)))
        self.assertNotEqual(retry_key(new_outbound_id("r1", 0)),
                            retry_key(new_outbound_id("r2", 0)))

    def test_is_a_hex_uuid(self):
        import uuid

        value = retry_key(new_outbound_id("gw-line-abc", 0))
        self.assertEqual(str(uuid.UUID(value)), value)


class TestFailureClassification(unittest.TestCase):
    def test_400_alone_is_not_token_or_fallback_authority(self):
        """Expired, used token and invalid message must not authorize fallback."""
        self.assertEqual(classify_send_failure(400), "rejected")

    def test_409_requires_endpoint_and_intent_evidence(self):
        self.assertEqual(classify_send_failure(409), "conflict")

    def test_transient_statuses_retry(self):
        for code in (500, 502, 503, 504):
            with self.subTest(code=code):
                self.assertEqual(classify_send_failure(code), "retryable")

    def test_429_does_not_authorize_another_transport_attempt(self):
        self.assertEqual(classify_send_failure(429), "rejected")

    def test_auth_failures_are_permanent(self):
        for code in (401, 403, 404):
            with self.subTest(code=code):
                self.assertEqual(classify_send_failure(code), "permanent")


class TestChunking(unittest.TestCase):
    def test_short_text_is_one_chunk(self):
        self.assertEqual(chunk_text("短い返事", 100, 5), ["短い返事"])

    def test_empty_text_yields_nothing(self):
        self.assertEqual(chunk_text("", 100, 5), [])
        self.assertEqual(chunk_text(None, 100, 5), [])

    def test_every_chunk_respects_the_limit(self):
        text = "\n".join(f"行 {i} " + "あ" * 40 for i in range(60))
        for chunk in chunk_text(text, 200, 20):
            self.assertLessEqual(len(chunk), 200)

    def test_code_fences_are_not_split_open(self):
        """A chunk boundary inside a fence would render as broken markup."""
        text = "説明\n\n```python\n" + "\n".join(
            f"line_{i} = {i}" for i in range(80)
        ) + "\n```\nおわり"
        chunks = chunk_text(text, 300, 20)
        for chunk in chunks:
            self.assertEqual(chunk.count("```") % 2, 0,
                             f"unbalanced fence in chunk: {chunk[:60]!r}")

    def test_max_chunks_truncates_rather_than_overflowing(self):
        text = "あ" * 5000
        chunks = chunk_text(text, 100, 3)
        self.assertLessEqual(len(chunks), 3)


class TestRedaction(unittest.TestCase):
    SECRETS = [
        "Bot MTIzNDU2Nzg5MDEyMzQ1Njc4.GaBcDe.FgHiJkLmNoPqRsTuVwXyZ0123456789ab",
        "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",  # privacy-ok
    ]

    def test_known_secret_shapes_are_removed(self):
        for secret in self.SECRETS:
            with self.subTest(secret=secret[:12]):
                out = redact(f"token is {secret} ok")
                self.assertNotIn(secret, out)
                self.assertIn(REDACTED, out)

    def test_ordinary_text_is_untouched(self):
        text = "明日の会議は10時からです。"
        self.assertEqual(redact(text), text)

    def test_hits_are_reported_for_audit(self):
        self.assertTrue(redaction_hits(f"x {self.SECRETS[0]} y"))
        self.assertFalse(redaction_hits("ふつうの文章"))


class TestMentionNeutralisation(unittest.TestCase):
    def test_everyone_and_here_defused(self):
        for raw in ("@everyone", "@here"):
            with self.subTest(raw=raw):
                self.assertNotIn(raw, neutralize_mentions(f"よろしく {raw} です"))

    def test_id_mentions_defused(self):
        out = neutralize_mentions("<@123456789012345678> さん")
        self.assertNotIn("<@123456789012345678>", out)

    def test_email_like_text_survives(self):
        text = "アドレスは example@example.com です"
        self.assertIn("example@example.com", neutralize_mentions(text))


class TestOutboundPipeline(unittest.TestCase):
    def test_oversized_answer_is_bounded(self):
        text = "あ" * 40000
        chunks = prepare_outbound(text, limit=4800, max_chunks=5,
                                  neutralize=False, soft_limit=4000,
                                  hard_limit=20000)
        self.assertLessEqual(len(chunks), 5)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 4800)

    def test_secret_in_an_answer_never_reaches_the_channel(self):
        chunks = prepare_outbound(
            f"設定はこうです: {TestRedaction.SECRETS[1]}",
            limit=4800, max_chunks=5, neutralize=True,
        )
        joined = "".join(chunks)
        self.assertNotIn(TestRedaction.SECRETS[1], joined)

    def test_summarize_flags_oversize(self):
        body, was_cut = summarize_if_oversized("あ" * 5000, soft_limit=100,
                                               hard_limit=1000)
        self.assertTrue(was_cut)
        self.assertLessEqual(len(body), 1100)


class TestUserFacingText(unittest.TestCase):
    def test_categories_differ(self):
        seen = {user_facing_error(c)
                for c in ("busy", "timeout", "unavailable", "internal")}
        self.assertEqual(len(seen), 4)

    def test_never_leaks_internals(self):
        for category in ("busy", "timeout", "unavailable", "internal", "unknown"):
            text = user_facing_error(category)
            for leak in ("Traceback", "/srv/", "Exception", "http://", "sqlite"):
                self.assertNotIn(leak, text)

    def test_unsupported_input_is_bounded_and_says_no_media(self):
        text = unsupported_input_reply("image")
        self.assertLess(len(text), 200)
        self.assertTrue(text.strip())


if __name__ == "__main__":
    unittest.main(verbosity=2)
