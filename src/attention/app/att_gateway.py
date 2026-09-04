#!/usr/bin/env python3
"""Attention Engine — the client for the gateway's proactive socket.

This is the engine's only way to reach a person. There is no LINE client
here, no Discord client, no token and no recipient: the engine says "notify
the owner, on this channel, with these words", and the gateway decides what
that means in terms of an API call (sections 47, 48, 51).

Standard library only, on purpose. `http.client` over an `AF_UNIX` socket is
about twenty lines and pulls in nothing; adding aiohttp or requests to this
process would be a dependency, a virtualenv to keep current and a supply
chain, for a client that makes three kinds of request.

The distinction this module exists to preserve is between:

    UNREACHABLE   the connection never opened. Nothing was sent. It is safe
                  to try the other channel immediately.
    UNKNOWN       the request went out and the answer did not come back. The
                  message may well have been delivered.

Section 86 turns on that difference. On UNKNOWN the engine retries the SAME
channel with the same notification id — which the gateway answers from its
own record, and which on LINE carries the same retry key, so a message that
did go out is recognised rather than charged again. Falling straight through
to the other channel on UNKNOWN is how one notification becomes two.
"""

from __future__ import annotations

import http.client
import json
import logging
import socket

log = logging.getLogger("attention.gateway")

DELIVERED = "delivered"
DUPLICATE = "duplicate"
FAILED = "failed"
REJECTED = "rejected"
IN_FLIGHT = "in_flight"
UNREACHABLE = "unreachable"
UNKNOWN = "unknown"

#: Outcomes after which the owner has the message.
SUCCESS = frozenset({DELIVERED, DUPLICATE})
#: Outcomes after which the message definitely did NOT go out, so another
#: channel may be tried at once.
DEFINITELY_NOT_SENT = frozenset({UNREACHABLE, REJECTED})


class Result:
    __slots__ = ("status", "detail", "channel", "mode", "quota_consumed",
                 "http_status", "delivery_id")

    def __init__(self, status, *, detail="", channel="", mode="",
                 quota_consumed=False, http_status=None, delivery_id=None):
        self.status = status
        self.detail = detail
        self.channel = channel
        self.mode = mode
        self.quota_consumed = bool(quota_consumed)
        self.http_status = http_status
        self.delivery_id = delivery_id

    @property
    def ok(self) -> bool:
        return self.status in SUCCESS

    @property
    def certainly_unsent(self) -> bool:
        return self.status in DEFINITELY_NOT_SENT

    def __repr__(self):
        return (f"Result({self.status!r}, channel={self.channel!r}, "
                f"detail={self.detail!r}, quota={self.quota_consumed})")


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


class GatewayUnreachable(Exception):
    """The socket could not be opened. Nothing was sent."""


class GatewayClient:
    def __init__(self, socket_path: str, *, timeout: float = 30.0):
        self.socket_path = str(socket_path)
        self.timeout = float(timeout)

    def __repr__(self):
        return f"GatewayClient({self.socket_path!r})"

    # -- transport -----------------------------------------------------

    def _request(self, method: str, path: str, body=None) -> tuple:
        """(http_status, parsed_body). Raises GatewayUnreachable or TimeoutError.

        Connection errors and read errors are deliberately NOT collapsed into
        one exception type: the caller's behaviour differs, and a client that
        reports "it failed" for both makes the safe choice impossible.
        """
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

    # -- the three verbs ----------------------------------------------

    def notify(self, *, notification_id: str, channel: str, severity: str,
               text: str, reason: str = "", dedupe_key: str = "") -> Result:
        """Ask the gateway to tell the owner. Never raises.

        Note what is absent from the body: any way to name a recipient. That
        is not an omission the caller could correct — the gateway rejects an
        unknown field outright (sections 50, 126).
        """
        body = {
            "notification_id": notification_id,
            "preferred_channel": channel,
            "severity": severity,
            "text": text,
        }
        if reason:
            body["reason"] = reason[:200]
        if dedupe_key:
            body["dedupe_key"] = dedupe_key[:200]

        try:
            status, parsed = self._request("POST", "/internal/notify", body)
        except GatewayUnreachable as exc:
            log.warning("gateway unreachable for %s: %s", notification_id, exc)
            return Result(UNREACHABLE, detail=str(exc), channel=channel)
        except (socket.timeout, TimeoutError, http.client.HTTPException,
                OSError) as exc:
            # The request went out. Whether it was acted on is unknown, and
            # saying "failed" here is how a delivered message gets sent twice.
            log.warning("gateway answer lost for %s: %s", notification_id,
                        type(exc).__name__)
            return Result(UNKNOWN, detail=type(exc).__name__, channel=channel)

        reported = str(parsed.get("status") or "")
        detail = str(parsed.get("detail") or "")
        result_status = {
            "delivered": DELIVERED, "duplicate": DUPLICATE,
            "failed": FAILED, "rejected": REJECTED, "in_flight": IN_FLIGHT,
        }.get(reported)
        if result_status is None:
            # An answer we do not understand is not a success. Treating an
            # unrecognised body as delivered is exactly the silent failure
            # section 88 forbids.
            result_status = UNKNOWN if status == 200 else FAILED
            detail = detail or f"unexpected_response:{status}"
        return Result(
            result_status, detail=detail, channel=channel,
            mode=str(parsed.get("mode") or ""),
            quota_consumed=bool(parsed.get("quota_consumed")),
            http_status=status, delivery_id=parsed.get("delivery_id"),
        )

    def line_budget(self, *, force: bool = False) -> dict | None:
        """LINE's own numbers, via the gateway. None when it cannot be read.

        None is not zero and it is not "unlimited" — the caller must map it to
        UNKNOWN and become conservative (section 8).
        """
        path = "/internal/line_budget" + ("?force=1" if force else "")
        try:
            status, parsed = self._request("GET", path)
        except (GatewayUnreachable, socket.timeout, TimeoutError, OSError,
                http.client.HTTPException) as exc:
            log.warning("line budget unavailable: %s", type(exc).__name__)
            return None
        if status != 200 or not isinstance(parsed, dict):
            return None
        return parsed

    def liveness(self) -> dict | None:
        try:
            status, parsed = self._request("GET", "/internal/liveness")
        except (GatewayUnreachable, socket.timeout, TimeoutError, OSError,
                http.client.HTTPException):
            return None
        return parsed if status == 200 and isinstance(parsed, dict) else None

    def health(self) -> bool:
        try:
            status, _ = self._request("GET", "/internal/health")
        except Exception:
            return False
        return status == 200
