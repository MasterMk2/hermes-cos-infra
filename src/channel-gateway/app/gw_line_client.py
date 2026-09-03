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

import aiohttp

from gw_line import (
    MAX_MESSAGES_PER_REQUEST,
    PUSH_ENDPOINT,
    REPLY_ENDPOINT,
    RETRY_KEY_HEADER,
    classify_send_failure,
    retry_key,
)

# LINE's own guidance is to keep webhook processing off the send path; these
# timeouts are for the send, which happens on the worker, not the webhook.
SEND_TIMEOUT_SECONDS = 20

# Retries here are only for the classes `classify_send_failure` calls
# retryable — 5xx and 429. A 400 is the reply token and is answered by
# switching to push, never by sending the same request again.
MAX_TRANSPORT_RETRIES = 2
RETRY_BACKOFF_SECONDS = (1.0, 3.0)


class LineSendResult:
    __slots__ = ("ok", "status", "category", "detail", "mode")

    def __init__(self, ok, status, category, detail="", mode=""):
        self.ok = ok
        self.status = status
        self.category = category
        self.detail = detail
        self.mode = mode

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

    async def _post(self, session, url, body, headers) -> LineSendResult:
        """One HTTP attempt, with retries for transport-class failures only."""
        last = LineSendResult(False, 0, "retryable", "no attempt made")
        for attempt in range(MAX_TRANSPORT_RETRIES + 1):
            try:
                async with session.post(
                    url, data=json.dumps(body).encode("utf-8"),
                    headers=headers, timeout=self._timeout,
                ) as resp:
                    text = (await resp.text())[:300]
                    if resp.status == 200:
                        return LineSendResult(True, 200, "ok")
                    category = classify_send_failure(resp.status)
                    # 409 means this exact retry key was already accepted:
                    # the message is delivered, and reporting failure would
                    # make the worker send it a third time.
                    if category == "already_sent":
                        return LineSendResult(True, resp.status, "already_sent")
                    last = LineSendResult(False, resp.status, category, text)
                    if category != "retryable":
                        return last
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last = LineSendResult(False, 0, "retryable", type(exc).__name__)
            if attempt < MAX_TRANSPORT_RETRIES:
                await asyncio.sleep(RETRY_BACKOFF_SECONDS[attempt])
        return last

    async def reply(self, session, reply_token: str, messages: list) -> LineSendResult:
        """Answer on the inbound event's token. Free, and single use.

        No retry key exists for reply: the token itself is the idempotency
        mechanism, since LINE accepts it exactly once.
        """
        if not reply_token:
            return LineSendResult(False, 0, "token", "no reply token", mode="reply")
        result = await self._post(
            session, REPLY_ENDPOINT,
            {"replyToken": reply_token,
             "messages": messages[:MAX_MESSAGES_PER_REQUEST]},
            self._headers(),
        )
        result.mode = "reply"
        return result

    async def push(self, session, owner_user_id: str, messages: list, *,
                   outbound_id: str) -> LineSendResult:
        """Send to the configured owner. Metered — 200/month on the free plan.

        `owner_user_id` is the value the gateway was configured with. It is a
        parameter rather than a field only so this class stays free of config
        loading; the caller passes `cfg.owner_line_user_id` and nothing else
        in the process has a different value to pass.

        The retry key is derived from `outbound_id`, so a retry of the same
        chunk is recognised by LINE as a duplicate and answered 409 instead
        of delivering — and charging for — a second copy.
        """
        if not owner_user_id:
            return LineSendResult(False, 0, "permanent", "no owner id", mode="push")
        result = await self._post(
            session, PUSH_ENDPOINT,
            {"to": owner_user_id,
             "messages": messages[:MAX_MESSAGES_PER_REQUEST]},
            self._headers({RETRY_KEY_HEADER: retry_key(outbound_id)}),
        )
        result.mode = "push"
        return result
