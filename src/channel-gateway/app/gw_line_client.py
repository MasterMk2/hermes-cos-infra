#!/usr/bin/env python3
"""Unified Channel Gateway — the LINE send side.

`gw_line.py` decides *what* and *whether*; this file is the only place that
talks to api.line.me. Splitting them is what lets every decision — reply vs
push, chunking, the retry key — be tested without a credential.

Two properties this module is responsible for, both of which cost real money
or real trust if they slip:

* **The push target is never read from a payload.** `push()` takes the owner
  id the caller was configured with. There is no code path in which a `to`
  field arriving in a webhook reaches an outbound request (spec sections 6,
  35): a forged event could otherwise make the gateway message a stranger.

* **The access token never leaves this module.** It is held on the instance,
  written only into an Authorization header, and `__repr__` is overridden so
  that a stray log line or an exception rendering the client cannot print it.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import aiohttp

from gw_line import (
    MAX_MESSAGES_PER_REQUEST,
    PUSH_ENDPOINT,
    REPLY_ENDPOINT,
    RETRY_KEY_HEADER,
    classify_send_failure,
    retry_key,
    line_length,
    MAX_TEXT_UNITS,
)

# LINE's own guidance is to keep webhook processing off the send path; these
# timeouts are for the send, which happens on the worker, not the webhook.
SEND_TIMEOUT_SECONDS = 20

# Only same-key push 5xx/transport failures retry within this bounded call.
# Reply never retries or falls back: a used token may mean acceptance happened.
MAX_TRANSPORT_RETRIES = 2
RETRY_BACKOFF_SECONDS = (1.0, 3.0)


def valid_text_batch(messages) -> bool:
    return (isinstance(messages, list) and 1 <= len(messages) <= MAX_MESSAGES_PER_REQUEST
            and all(isinstance(m, dict) and m.get("type") == "text"
                    and isinstance(m.get("text"), str) and m["text"]
                    and line_length(m["text"]) <= MAX_TEXT_UNITS for m in messages))


def push_retry_window_open(started_at: str, *, now=None, reserve_seconds=0) -> bool:
    """Never extend the provider's 24h window or trust a backwards clock."""
    try:
        start = datetime.fromisoformat(started_at)
        if start.tzinfo is None:
            return False
        elapsed = ((now or datetime.now(timezone.utc)) - start).total_seconds()
        return 0 <= elapsed < 24 * 60 * 60 - reserve_seconds
    except (TypeError, ValueError):
        return False


class LineSendResult:
    __slots__ = ("ok", "status", "category", "detail", "mode", "not_sent")

    def __init__(self, ok, status, category, detail="", mode="", *, not_sent=False):
        self.ok = ok
        self.status = status
        self.category = category
        self.detail = detail
        self.mode = mode
        self.not_sent = not_sent

    def __repr__(self):  # no token, no body — safe to log
        return (f"LineSendResult(ok={self.ok}, status={self.status}, "
                f"category={self.category!r}, mode={self.mode!r})")


class LineClient:
    """Sends text messages as the configured LINE Official Account."""

    def __init__(self, channel_access_token: str, *,
                 timeout: int = SEND_TIMEOUT_SECONDS):
        self._token = channel_access_token
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    def __repr__(self):
        # The token is the whole of this object's state. Anything that prints
        # a client — a log line, a traceback, a debugger — must not print it.
        return "LineClient(token=<redacted>)"

    __str__ = __repr__

    def _headers(self, extra=None) -> dict:
        h = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        h.update(extra or {})
        return h

    async def _post(self, session, url, body, headers, *, started_at=None) -> LineSendResult:
        """One reply attempt, or bounded same-key push transport retries.

        A final failure does not summarize earlier attempts. The caller holds
        all failed/ambiguous sends; this function never chooses a fallback.
        """
        push = url == PUSH_ENDPOINT and bool(headers.get(RETRY_KEY_HEADER))
        attempts = MAX_TRANSPORT_RETRIES + 1 if push else 1
        if started_at is None:
            started_at = datetime.now(timezone.utc).isoformat()
        last = LineSendResult(False, 0, "unknown", "no attempt made")
        for attempt in range(attempts):
            if push and not push_retry_window_open(started_at, reserve_seconds=self._timeout.total or SEND_TIMEOUT_SECONDS):
                return LineSendResult(False, 0, "retry_window_expired")
            try:
                async with session.post(
                    url, data=json.dumps(body).encode("utf-8"),
                    headers=headers, timeout=self._timeout,
                ) as resp:
                    # Consume the response without propagating body/token text.
                    await resp.text()
                    if resp.status == 200:
                        return LineSendResult(True, 200, "ok")
                    accepted_id = next((str(v) for k, v in resp.headers.items()
                                        if k.lower() == "x-line-accepted-request-id"), "").strip()
                    if (resp.status == 409 and push and accepted_id
                            and push_retry_window_open(started_at)):
                        # Provider acceptance of this frozen push, NOT arrival
                        # at the user's device and never a reply-token receipt.
                        return LineSendResult(True, 409, "already_sent")
                    category = classify_send_failure(resp.status)
                    last = LineSendResult(False, resp.status, category)
                    # Official retry guidance: 4xx (including 429) do not
                    # authorize another attempt; only push 5xx/transport do.
                    if not (push and resp.status >= 500):
                        return last
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last = LineSendResult(False, 0, "unknown", type(exc).__name__)
            if attempt < attempts - 1:
                await asyncio.sleep(RETRY_BACKOFF_SECONDS[attempt])
        return last

    async def reply(self, session, reply_token: str, messages: list) -> LineSendResult:
        """Answer on the inbound event's token. Free, and single use.

        Reply does not support retry keys. The token can only be used once,
        but a lost response is not evidence that it has not been used.
        """
        if not valid_text_batch(messages):
            return LineSendResult(False, 0, "invalid_batch", mode="reply", not_sent=True)
        if not reply_token:
            return LineSendResult(False, 0, "no_reply_token", mode="reply", not_sent=True)
        result = await self._post(
            session, REPLY_ENDPOINT,
            {"replyToken": reply_token,
             "messages": messages},
            self._headers(),
        )
        result.mode = "reply"
        return result

    async def push(self, session, owner_user_id: str, messages: list, *,
                   outbound_id: str, started_at=None) -> LineSendResult:
        """Send to the configured owner. Metered — 200/month on the free plan.

        `owner_user_id` is the value the gateway was configured with. It is a
        parameter rather than a field only so this class stays free of config
        loading; the caller passes `cfg.owner_line_user_id` and nothing else
        in the process has a different value to pass.

        The retry key is derived from `outbound_id`, so a retry of the same
        chunk is recognised by LINE as a duplicate and answered 409 instead
        of delivering — and charging for — a second copy.
        """
        if not valid_text_batch(messages):
            return LineSendResult(False, 0, "invalid_batch", mode="push", not_sent=True)
        if not owner_user_id:
            return LineSendResult(False, 0, "no_owner", mode="push", not_sent=True)
        result = await self._post(
            session, PUSH_ENDPOINT,
            {"to": owner_user_id,
             "messages": messages},
            self._headers({RETRY_KEY_HEADER: retry_key(outbound_id)}),
            started_at=started_at,
        )
        result.mode = "push"
        return result
