#!/usr/bin/env python3
"""G.1 unit tests — LINE webhook authenticity and event handling.

The property under test throughout: **the signature is verified over the
bytes that arrived, before anything parses them** (spec sections 26, 27).
Everything else in the gateway trusts that check, so it is tested from more
directions than anything else here.
"""

import json
import unittest

import gw_fixtures as fx
from gw_canonical import SUBJECT_OWNER, SUBJECT_UNAUTHORIZED, canonical_from_line
from gw_line import (
    MAX_MESSAGES_PER_REQUEST,
    SIGNATURE_HEADER,
    build_text_messages,
    compute_signature,
    extract_events,
    header_lookup,
    is_redelivery,
    is_verification_probe,
    line_length,
    parse_webhook_body,
    verify_signature,
)


class TestSignature(unittest.TestCase):
    def test_valid_signature_accepted(self):
        body = fx.line_body([fx.line_text_event()])
        sig = compute_signature(body, fx.CHANNEL_SECRET)
        self.assertTrue(verify_signature(body, sig, fx.CHANNEL_SECRET))

    def test_one_flipped_byte_rejected(self):
        body = fx.line_body([fx.line_text_event()])
        sig = compute_signature(body, fx.CHANNEL_SECRET)
        tampered = bytearray(body)
        tampered[-2] ^= 0x01
        self.assertFalse(verify_signature(bytes(tampered), sig, fx.CHANNEL_SECRET))

    def test_wrong_secret_rejected(self):
        body = fx.line_body([fx.line_text_event()])
        sig = compute_signature(body, "a-different-secret")
        self.assertFalse(verify_signature(body, sig, fx.CHANNEL_SECRET))

    def test_missing_signature_returns_false_not_exception(self):
        body = fx.line_body([fx.line_text_event()])
        self.assertFalse(verify_signature(body, None, fx.CHANNEL_SECRET))
        self.assertFalse(verify_signature(body, "", fx.CHANNEL_SECRET))

    def test_empty_secret_rejects(self):
        """An unconfigured secret must authenticate nobody, not everybody."""
        body = fx.line_body([fx.line_text_event()])
        sig = compute_signature(body, fx.CHANNEL_SECRET)
        self.assertFalse(verify_signature(body, sig, ""))

    def test_reserialized_body_does_not_verify(self):
        """This is the whole reason `verify_signature` takes bytes.

        A caller that parsed first and re-serialized would produce different
        bytes — different key order, different spacing, different unicode
        escaping — and the signature would not match. Asserting it here means
        nobody can "fix" the API to accept a dict without a test failing.
        """
        body = fx.line_body([fx.line_text_event()])
        sig = compute_signature(body, fx.CHANNEL_SECRET)
        round_tripped = json.dumps(json.loads(body)).encode("utf-8")
        self.assertNotEqual(body, round_tripped)
        self.assertFalse(verify_signature(round_tripped, sig, fx.CHANNEL_SECRET))

    def test_bytes_required(self):
        with self.assertRaises(TypeError):
            compute_signature("a string body", fx.CHANNEL_SECRET)

    def test_signature_is_base64_sha256(self):
        """32 raw bytes base64-encoded is 44 characters ending in '='."""
        sig = compute_signature(b"{}", fx.CHANNEL_SECRET)
        self.assertEqual(len(sig), 44)


class TestHeaderLookup(unittest.TestCase):
    def test_case_insensitive(self):
        for name in ("x-line-signature", "X-Line-Signature", "X-LINE-SIGNATURE"):
            with self.subTest(name=name):
                self.assertEqual(
                    header_lookup({name: "value"}, SIGNATURE_HEADER), "value"
                )

    def test_absent_header_is_none(self):
        self.assertIsNone(header_lookup({"content-type": "x"}, SIGNATURE_HEADER))
        self.assertIsNone(header_lookup(None, SIGNATURE_HEADER))


class TestEventExtraction(unittest.TestCase):
    def test_verification_probe_is_valid_not_an_error(self):
        payload = parse_webhook_body(fx.line_verification_body())
        self.assertEqual(extract_events(payload), [])
        self.assertTrue(is_verification_probe(payload))

    def test_events_extracted(self):
        payload = parse_webhook_body(
            fx.line_body([fx.line_text_event(), fx.line_text_event(message_id="m2")])
        )
        self.assertEqual(len(extract_events(payload)), 2)

    def test_malformed_payload_yields_no_events(self):
        for junk in ({}, {"events": "not a list"}, [], None, "text"):
            with self.subTest(junk=junk):
                self.assertEqual(extract_events(junk), [])

    def test_redelivery_read_from_body_not_header(self):
        """`deliveryContext.isRedelivery` is a body field.

        An earlier draft looked for an `x-line-*` header, which does not
        exist — the check would never have fired.
        """
        self.assertFalse(is_redelivery(fx.line_text_event()))
        self.assertTrue(is_redelivery(fx.line_text_event(redelivery=True)))
        self.assertFalse(is_redelivery({}))


class TestNormalization(unittest.TestCase):
    def test_owner_message_is_authenticated(self):
        msg = canonical_from_line(fx.line_text_event(),
                                  owner_line_user_id=fx.OWNER_LINE)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.authz_subject, SUBJECT_OWNER)
        self.assertTrue(msg.authenticated)
        self.assertEqual(msg.text, "こんにちは")
        self.assertEqual(msg.conversation_id, f"line:{fx.OWNER_LINE}")

    def test_stranger_is_not_authenticated(self):
        msg = canonical_from_line(
            fx.line_text_event(user_id=fx.STRANGER_LINE),
            owner_line_user_id=fx.OWNER_LINE,
        )
        self.assertEqual(msg.authz_subject, SUBJECT_UNAUTHORIZED)
        self.assertFalse(msg.authenticated)

    def test_unset_owner_authenticates_nobody(self):
        """A half-configured gateway must reject, not admit."""
        msg = canonical_from_line(fx.line_text_event(), owner_line_user_id=None)
        self.assertFalse(msg.authenticated)

    def test_non_message_events_are_dropped(self):
        for etype in ("follow", "unfollow", "join", "postback", "beacon"):
            with self.subTest(etype=etype):
                event = fx.line_text_event()
                event["type"] = etype
                self.assertIsNone(
                    canonical_from_line(event, owner_line_user_id=fx.OWNER_LINE)
                )

    def test_non_text_message_normalizes_with_empty_text(self):
        """Answered with a bounded 'text only' reply, not silently ignored."""
        for mtype in ("image", "video", "audio", "file", "location", "sticker"):
            with self.subTest(mtype=mtype):
                msg = canonical_from_line(
                    fx.line_text_event(message_type=mtype),
                    owner_line_user_id=fx.OWNER_LINE,
                )
                self.assertIsNotNone(msg)
                self.assertEqual(msg.text, "")
                self.assertEqual(msg.reply_context, mtype)

    def test_request_id_is_stable_for_the_same_message(self):
        a = canonical_from_line(fx.line_text_event(),
                                owner_line_user_id=fx.OWNER_LINE)
        b = canonical_from_line(fx.line_text_event(),
                                owner_line_user_id=fx.OWNER_LINE)
        self.assertEqual(a.gateway_request_id, b.gateway_request_id)

    def test_request_id_differs_between_messages(self):
        a = canonical_from_line(fx.line_text_event(message_id="m1"),
                                owner_line_user_id=fx.OWNER_LINE)
        b = canonical_from_line(fx.line_text_event(message_id="m2"),
                                owner_line_user_id=fx.OWNER_LINE)
        self.assertNotEqual(a.gateway_request_id, b.gateway_request_id)


class TestTextMeasurement(unittest.TestCase):
    def test_utf16_code_units_not_code_points(self):
        """LINE counts UTF-16 units; an emoji outside the BMP costs two."""
        self.assertEqual(line_length("abc"), 3)
        self.assertEqual(line_length("あいう"), 3)
        self.assertEqual(len("🍎"), 1)
        self.assertEqual(line_length("🍎"), 2)
        self.assertEqual(line_length("🍎🍎🍎"), 6)

    def test_empty_and_none(self):
        self.assertEqual(line_length(""), 0)
        self.assertEqual(line_length(None), 0)

    def test_build_text_messages_caps_at_five(self):
        msgs = build_text_messages([f"chunk{i}" for i in range(9)])
        self.assertEqual(len(msgs), MAX_MESSAGES_PER_REQUEST)
        self.assertTrue(all(m["type"] == "text" for m in msgs))


if __name__ == "__main__":
    unittest.main(verbosity=2)
