#!/usr/bin/env python3
"""Unified Channel Gateway — LINE adapter.

The pure half of the LINE side: signature verification, event validation and
the reply-versus-push decision. No network, no database, no clock except what
is passed in, so all of it is testable before a single credential exists.

The one rule this module exists to enforce:

    **the signature is verified over the bytes that arrived, before anything
    parses them.**

`verify_signature` therefore takes `bytes`, not a dict and not a str. A caller
that has already run `json.loads` cannot use this function without
re-serializing — and a re-serialized body does not verify, which is exactly
the property the tests assert.

Every constant below was read from developers.line.biz on 2026-09-03 rather
than assumed; the ones that bite are marked with why.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import uuid
from datetime import datetime, timedelta, timezone

# Documented lowercase, but the reference states field-name casing "may change
# without notice" and shows it already changed once. Match case-insensitively.
SIGNATURE_HEADER = "x-line-signature"

# "Reply tokens must be used within one minute after receiving the webhook."
# The same page warns the limit "is subject to change without notice… don't
# rely on the time limit for implementation", so this is a scheduling hint for
# choosing reply-vs-push, never a correctness assumption: a reply that loses
# the race fails with 400 and the caller falls back.
REPLY_TOKEN_TTL_SECONDS = 60

# A redelivered webhook's token is usable for a minute from the REDELIVERY,
# but never more than 20 minutes after the original event.
REPLY_TOKEN_ABSOLUTE_CEILING_SECONDS = 20 * 60

# How much of the token's life must remain for a reply to be worth trying.
#
# Measured on this host, 2026-09-03, five representative prompts through the
# live Hermes API: 9.3s / 23.4s / 49.6s / 62.5s / 88.3s (median 49.6s). The
# margin decides where the free/metered line falls, and the answers cluster
# right on it, so it is set from that measurement rather than by feel.
#
# 8 seconds, not the 15 first written here: a reply that loses the race fails
# with 400 and the caller immediately pushes instead, so an optimistic attempt
# costs one wasted request and a few hundred milliseconds, while a pessimistic
# one costs a push credit out of a monthly 200. When the downside is that
# asymmetric, try the free path.
REPLY_TOKEN_SAFETY_MARGIN_SECONDS = 8

# "Max character limit: 5000" — but counted in UTF-16 code units, so an emoji
# outside the BMP costs two. `line_length` below does that counting.
MAX_TEXT_UNITS = 5000
# Both reply and push accept "Max: 5" message objects per request.
MAX_MESSAGES_PER_REQUEST = 5

REPLY_ENDPOINT = "https://api.line.me/v2/bot/message/reply"
PUSH_ENDPOINT = "https://api.line.me/v2/bot/message/push"

# Push (not reply) supports an idempotency key: a hex UUID, remembered for 24
# hours, answering 409 when the same key is replayed. Reply has no equivalent
# — its single-use token is the equivalent.
RETRY_KEY_HEADER = "X-Line-Retry-Key"
RETRY_KEY_TTL_SECONDS = 24 * 3600

# The receiver must answer within about two seconds, and sustained failure can
# get the webhook switched off at LINE's end. Everything after the signature
# check is therefore a queue insert and nothing else.
WEBHOOK_RESPONSE_BUDGET_SECONDS = 2.0

# Reply messages do not count against the monthly quota; push messages do, and
# the free plan in Japan allows 200 a month. So push is a fallback for when a
# reply token is unusable, never a default.
PUSH_IS_METERED = True


class SignatureError(Exception):
    """Raised when a webhook cannot be attributed to the configured channel."""


def line_length(text) -> int:
    """Length as LINE counts it: UTF-16 code units.

    `len()` in Python counts code points, so a single 🍎 measures 1 there and
    2 at LINE. Chunking on the Python figure would let an emoji-heavy answer
    exceed the real limit and fail the send.
    """
    return len(str(text or "").encode("utf-16-le")) // 2


def compute_signature(raw_body: bytes, channel_secret: str) -> str:
    """base64( HMAC-SHA256( channel_secret, raw_body ) )."""
    if not isinstance(raw_body, (bytes, bytearray)):
        raise TypeError(
            "raw_body must be bytes: the signature covers the bytes that "
            "arrived, and any parse/re-serialize round trip invalidates it"
        )
    digest = hmac.new(
        channel_secret.encode("utf-8"), bytes(raw_body), hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def verify_signature(raw_body: bytes, signature, channel_secret: str) -> bool:
    """Constant-time comparison of the header against the computed value.

    Returns False rather than raising for a missing or malformed header: a
    caller that forgets to check the return value should get "rejected", not
    an exception it might swallow.
    """
    if not signature or not channel_secret:
        return False
    expected = compute_signature(raw_body, channel_secret)
    return hmac.compare_digest(expected, str(signature))


def header_lookup(headers, name: str):
    """Case-insensitive header read.

    LINE's reference warns that request header casing may change without
    notice and documents a case in which it already did.
    """
    target = name.lower()
    for k, v in (headers or {}).items():
        if str(k).lower() == target:
            return v
    return None


def parse_webhook_body(raw_body: bytes) -> dict:
    """Parse AFTER verification. Kept separate so the ordering is visible."""
    import json

    return json.loads(raw_body.decode("utf-8"))


def extract_events(payload: dict) -> list:
    """The events array, or an empty list.

    LINE's console verification posts `{"destination": ..., "events": []}` and
    expects success; an empty list is a normal, valid payload.
    """
    if not isinstance(payload, dict):
        return []
    events = payload.get("events")
    return events if isinstance(events, list) else []


def is_verification_probe(payload: dict) -> bool:
    """True for the console's connection check: valid signature, no events."""
    return isinstance(payload, dict) and extract_events(payload) == []


def is_redelivery(event: dict) -> bool:
    """Whether LINE says this is a redelivered event.

    It is a BODY field, `deliveryContext.isRedelivery` — not a header. The
    reference lists exactly four `x-line-*` headers and none of them carries
    this, so a header-based check would never fire. Deduplication does not
    depend on this either way; it keys on the message id, which is stable
    across redelivery. This is for the audit trail.
    """
    ctx = (event or {}).get("deliveryContext")
    return bool(isinstance(ctx, dict) and ctx.get("isRedelivery"))


# Namespace for deriving retry keys. Any fixed UUID works; this one is
# arbitrary and constant so the derivation is reproducible across restarts.
_RETRY_KEY_NAMESPACE = uuid.UUID("6f9d3a1c-1b47-4f9a-9a2e-7c3b5d8e40f1")


def retry_key(outbound_id: str) -> str:
    """A hex UUID for X-Line-Retry-Key, derived from the chunk it sends.

    Deliberately NOT random. LINE deduplicates on this key for 24 hours and
    answers 409 for a replay — but only if the replay carries the SAME key. A
    worker that crashed between the HTTP send and the state write retries the
    identical chunk; with a fresh uuid4 that retry would look like new work,
    the owner would receive the answer twice, and it would cost two push
    credits out of a monthly 200. Deriving from `outbound_id` — itself
    (request_id, chunk_index) — makes the retry indistinguishable from the
    original, which is exactly what the 409 is for.

    Same stability property as `idempotency_key` on the Hermes side.
    """
    return str(uuid.uuid5(_RETRY_KEY_NAMESPACE, str(outbound_id)))


def reply_token_deadline(received_at: str, ttl_seconds: int = REPLY_TOKEN_TTL_SECONDS):
    base = datetime.fromisoformat(received_at)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return base + timedelta(seconds=ttl_seconds)


def reply_still_viable(received_at: str, *, now=None,
                       margin: int = REPLY_TOKEN_SAFETY_MARGIN_SECONDS) -> bool:
    """Whether a reply token is worth spending.

    A reply that arrives after the token expires is rejected with 400 and the
    answer is lost unless a push follows — and push is metered, so the
    decision is made with a margin, before the send, rather than discovered.
    """
    now = now or datetime.now(timezone.utc)
    return now < (reply_token_deadline(received_at) - timedelta(seconds=margin))


def choose_delivery_mode(message, *, now=None) -> str:
    """'reply' while the token is comfortably alive, otherwise 'push'.

    Reply is free and push is not, so this deliberately prefers reply and
    treats push as the exception. The push target is always the configured
    owner and is never read from a payload; that is enforced at the sender.
    """
    if not getattr(message, "reply_token", None):
        return "push"
    return "reply" if reply_still_viable(message.received_at, now=now) else "push"


def build_text_messages(chunks) -> list:
    """LINE message objects for a list of already-chunked strings."""
    return [{"type": "text", "text": c} for c in chunks][:MAX_MESSAGES_PER_REQUEST]


# Seven inbound message types exist; only text is in scope for G.1. The others
# get a bounded answer rather than silence, and their media is never fetched —
# binary content lives on a different domain and is explicitly out of scope.
TEXT_TYPE = "text"
KNOWN_MESSAGE_TYPES = frozenset(
    {"text", "image", "video", "audio", "file", "location", "sticker"}
)

UNSUPPORTED_INPUT_REPLY = (
    "いまはテキストメッセージのみ対応しています。"
    "画像や音声などは内容を読み取りません。"
)


def unsupported_input_reply(message_type: str = "") -> str:
    """Bounded answer for a non-text message. The media is never downloaded."""
    return UNSUPPORTED_INPUT_REPLY


def classify_send_failure(status_code: int) -> str:
    """'retryable' | 'permanent' | 'token' for a send response.

    400 on a reply is almost always the token: expired and already-used are
    indistinguishable, both returning `{"message": "Invalid reply token"}`.
    The caller's answer to that is a push, not a retry of the same reply.
    """
    if status_code in (500, 502, 503, 504):
        return "retryable"
    if status_code == 429:
        return "retryable"
    if status_code == 409:
        return "already_sent"
    if status_code == 400:
        return "token"
    return "permanent"
