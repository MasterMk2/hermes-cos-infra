#!/usr/bin/env python3
"""Morning Brief delivery — the gateway's proactive unix socket, and nothing else.

Phase I.2 sections 51, 52, 57-59, 61, 69, 99.

This module holds no Discord token, no LINE token, no recipient and no HTTP
client library. It says "tell the owner this, on Discord" over
`/srv/hermes/channel-gateway/run/proactive.sock` and the gateway decides what
that means. The recipient is not even expressible: the gateway rejects any
field outside its six-name allowlist with `400 unknown_field:<name>`
(`gw_proactive.py:105-107`), so a bug here cannot redirect a brief.

## LINE is unreachable from this file, structurally (sections 61, 99)

There is no LINE constant in this module and `send_brief()` has **no channel
parameter**. The literal `CHANNEL_DISCORD` is written into the request body at
one place. On the gateway side the channel is a plain `if/else` with no
`except` and no fallback (`gw_proactive.py:291-294`), and the recipient is
selected from the same validated variable (`:256-257`), so a `discord` request
cannot reach `_send_line` and cannot spend a LINE push.
`tests-planning/test_brief_line_negative.py` asserts both halves.

## Outcome mapping (I2-AMENDMENT G3 — the retry-termination contract)

| gateway answer                        | HTTP | outcome            | retry? |
|---------------------------------------|-----:|--------------------|--------|
| `status=delivered`                    |  200 | `delivered`        | done   |
| `status=duplicate detail=already_delivered` | 200 | `already_delivered` | done |
| `status=in_flight detail=delivery_in_progress` | 409 | `in_flight`  | **no** |
| `status=outcome_unknown`              |  409 | `outcome_unknown`  | **no** |
| `status=rejected detail=content_revision_mismatch` | 409 | `content_mismatch` | **no** |
| `status=rejected` (malformed request) |  400 | `rejected`         | **no** |
| owner not configured                  |  503 | `rejected`         | **no** |
| `status=failed`                       |  502 | `failed`           | yes    |
| socket absent / refused               |    - | `unreachable`      | yes    |
| answer never came back                |    - | `unknown`          | yes    |

Reservation/conflict 409s are not delivery receipts. `content_revision` is
`sha256(text)[:32]` (`gw_store.py:529-532`), so a restart that recomputes a
brief differing by a single character re-posts under the same
`morning-brief:YYYY-MM-DD` id and gets 409, not 200. Treating that as
retryable can repeatedly regenerate the same conflict. A reserved row proves
neither provider acceptance nor delivery: both in_flight and content_mismatch
stop automatic retries and keep planning health unknown until reconciliation.

`outcome_unknown` is a durable gateway hold and requires reconciliation. It
is not a duplicate-success receipt. `unknown` below is only transport loss.

`unknown` is kept distinct from `failed` on purpose: the request went out and
the answer was lost, so the message may well have been delivered. Retrying is
still safe — it retries the *same* notification id, which the gateway answers
from its own record — but calling it a failure and giving up would leave the
health file lying about a brief the owner actually received.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import socket

log = logging.getLogger("planning.delivery")

#: The only channel this module can name. There is deliberately no LINE
#: constant here (sections 61, 99) — see the module docstring.
CHANNEL_DISCORD = "discord"

#: The routine brief is never an alarm (section 44). INFO is also what the
#: attention engine's own digests use, so the two read alike in Discord.
SEVERITY_INFO = "INFO"

#: `notification_id` alphabet is `[A-Za-z0-9_.:-]`, max 120 chars
#: (`gw_proactive.py:75-78`). "morning-brief:2026-09-08" is 24 and clean.
ID_PREFIX = "morning-brief"

#: `gw_proactive.MAX_NOTIFICATION_CHARS`. Longer text is a 400, not a
#: truncation, so the caller must not exceed it.
MAX_TEXT_CHARS = 1200

DEFAULT_SOCKET_PATH = "/srv/hermes/channel-gateway/run/proactive.sock"
NOTIFY_PATH = "/internal/notify"

# -- outcomes ---------------------------------------------------------------

DELIVERED = "delivered"
ALREADY_DELIVERED = "already_delivered"
IN_FLIGHT = "in_flight"
CONTENT_MISMATCH = "content_mismatch"
REJECTED = "rejected"
FAILED = "failed"
UNREACHABLE = "unreachable"
UNKNOWN = "unknown"
OUTCOME_UNKNOWN = "outcome_unknown"

#: The owner has the brief.
SUCCESS = frozenset({DELIVERED, ALREADY_DELIVERED})
#: Stop immediately. This includes a durable unresolved receipt, which is
#: neither successful delivery nor a request another send can safely fix.
#: Section 59's bound depends on this
#: set being non-empty for exactly these cases.
TERMINAL = frozenset({DELIVERED, ALREADY_DELIVERED, IN_FLIGHT,
                      CONTENT_MISMATCH, REJECTED, OUTCOME_UNKNOWN})
#: Worth another attempt inside the bounded budget.
RETRYABLE = frozenset({FAILED, UNREACHABLE, UNKNOWN})


def notification_id_for(local_date: str) -> str:
    """`morning-brief:YYYY-MM-DD` — the idempotency key of section 58.

    The local (Asia/Tokyo) date, never the UTC date: at 08:30 JST the UTC date
    is still yesterday, and keying off it would let a single owner-day carry
    two distinct ids.
    """
    date = str(local_date).strip()
    if len(date) != 10 or date[4] != "-" or date[7] != "-":
        raise ValueError(f"local_date must be YYYY-MM-DD, got {local_date!r}")
    if not (date[:4] + date[5:7] + date[8:]).isdigit():
        raise ValueError(f"local_date must be YYYY-MM-DD, got {local_date!r}")
    return f"{ID_PREFIX}:{date}"


def content_revision(text: str) -> str:
    """The gateway's own revision key (`gw_store.py:529-532`).

    Reproduced here so a caller can tell "the same brief, re-sent" from "a
    different brief under the same id" *before* spending a socket round trip
    and reading a 409 back.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


class Result:
    __slots__ = ("outcome", "detail", "http_status", "delivery_id",
                 "quota_consumed", "sent_at")

    def __init__(self, outcome, *, detail="", http_status=None,
                 delivery_id=None, quota_consumed=False, sent_at=None):
        self.outcome = outcome
        self.detail = detail
        self.http_status = http_status
        self.delivery_id = delivery_id
        self.quota_consumed = bool(quota_consumed)
        self.sent_at = sent_at

    @property
    def channel(self) -> str:
        # Not stored, and not read from the response: there is exactly one
        # channel this module can produce.
        return CHANNEL_DISCORD

    @property
    def ok(self) -> bool:
        return self.outcome in SUCCESS

    @property
    def terminal(self) -> bool:
        return self.outcome in TERMINAL

    @property
    def retryable(self) -> bool:
        return self.outcome in RETRYABLE

    def __repr__(self):
        return (f"Result({self.outcome!r}, detail={self.detail!r}, "
                f"http={self.http_status})")


class GatewayUnreachable(Exception):
    """The socket could not be opened. Nothing was sent."""


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


class DeliveryClient:
    """`http.client` over `AF_UNIX`, standard library only.

    The same shape as `src/attention/app/att_gateway.GatewayClient`, minus
    everything the brief does not need: no LINE budget query, no channel
    argument, no second channel.
    """

    def __init__(self, socket_path: str = DEFAULT_SOCKET_PATH, *,
                 timeout: float = 30.0):
        self.socket_path = str(socket_path)
        self.timeout = float(timeout)

    def __repr__(self):
        return f"DeliveryClient({self.socket_path!r})"

    # -- transport -----------------------------------------------------

    def _request(self, method: str, path: str, body=None) -> tuple:
        conn = _UnixHTTPConnection(self.socket_path, self.timeout)
        try:
            try:
                conn.connect()
            except (FileNotFoundError, ConnectionRefusedError, PermissionError,
                    OSError) as exc:
                raise GatewayUnreachable(type(exc).__name__) from exc
            payload = None
            headers = {}
            if body is not None:
                payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            try:
                parsed = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError:
                parsed = {}
            return resp.status, parsed
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # -- the one verb --------------------------------------------------

    def send_brief(self, *, notification_id: str, text: str,
                   reason: str = "morning_brief") -> Result:
        """Deliver the routine morning brief. Never raises.

        There is no `channel` parameter and no `severity` parameter. Both are
        fixed by policy — Discord (sections 61, 99) and INFO (section 44) —
        and a parameter is a way for a caller to get them wrong.
        """
        if not isinstance(text, str) or not text.strip():
            return Result(REJECTED, detail="text_empty")
        if len(text) > MAX_TEXT_CHARS:
            # The gateway would answer 400 `text_too_long`; refusing here
            # keeps the reason legible and costs no round trip. Truncating
            # would silently deliver half a brief.
            return Result(REJECTED, detail="text_too_long")

        body = {
            "notification_id": notification_id,
            "preferred_channel": CHANNEL_DISCORD,
            "severity": SEVERITY_INFO,
            "text": text,
            "reason": reason[:200],
            "dedupe_key": notification_id[:200],
        }
        return self._notify(body)

    def _notify(self, body: dict) -> Result:
        try:
            status, parsed = self._request("POST", NOTIFY_PATH, body)
        except GatewayUnreachable as exc:
            log.warning("gateway unreachable for %s: %s",
                        body.get("notification_id"), exc)
            return Result(UNREACHABLE, detail=str(exc))
        except (socket.timeout, TimeoutError, http.client.HTTPException,
                OSError) as exc:
            log.warning("gateway answer lost for %s: %s",
                        body.get("notification_id"), type(exc).__name__)
            return Result(UNKNOWN, detail=type(exc).__name__)
        return classify_response(status, parsed)

    def health(self) -> bool:
        try:
            status, _ = self._request("GET", "/internal/health")
        except Exception:
            return False
        return status == 200


def classify_response(status: int, parsed) -> Result:
    """Map one gateway answer onto the outcome table in the module docstring.

    Pure, so every row of I2-AMENDMENT G3 is testable without a socket — which
    matters, because `AF_UNIX` does not exist on the workstation where these
    tests are written.
    """
    if not isinstance(parsed, dict):
        parsed = {}
    reported = str(parsed.get("status") or "")
    detail = str(parsed.get("detail") or "")
    common = {
        "detail": detail,
        "http_status": status,
        "delivery_id": parsed.get("delivery_id"),
        "quota_consumed": bool(parsed.get("quota_consumed")),
        "sent_at": parsed.get("sent_at"),
    }

    if status == 200 and reported == "delivered":
        return Result(DELIVERED, **common)
    if status == 200 and reported == "duplicate" and detail == "already_delivered":
        # `detail == "already_delivered"`. Success, and explicitly not a
        # retry: the gateway logged `proactive duplicate suppressed`.
        return Result(ALREADY_DELIVERED, **common)
    if status == 409 and reported == "in_flight":
        return Result(IN_FLIGHT, **common)
    if status == 409 and reported == OUTCOME_UNKNOWN:
        # The gateway has a durable hold: unlike a lost socket answer, this
        # cannot be resolved by another send attempt. Do not call it a
        # content mismatch or infer that the owner received the brief.
        return Result(OUTCOME_UNKNOWN, **common)
    if status == 409 and detail == "content_revision_mismatch":
        return Result(CONTENT_MISMATCH,
                      **{**common, "detail": detail or "content_revision_mismatch"})
    if status == 409:
        return Result(REJECTED, **{**common, "detail": detail or "conflict"})
    if status == 400:
        return Result(REJECTED, **{**common, "detail": detail or "rejected"})
    if status == 503:
        # `<channel>_owner_not_configured`. A repetition cannot configure it.
        return Result(REJECTED, **{**common, "detail": detail or "owner_not_configured"})
    if status == 502 or reported == "failed":
        return Result(FAILED, **{**common, "detail": detail or "send_failed"})
    if status == 413:
        return Result(REJECTED, **{**common, "detail": detail or "body_too_large"})

    # An answer this module does not understand is not a success. Saying
    # "delivered" for an unrecognised body is exactly the silent failure the
    # health file exists to prevent.
    return Result(UNKNOWN if status == 200 else FAILED,
                  **{**common, "detail": detail or f"unexpected_response:{status}"})
