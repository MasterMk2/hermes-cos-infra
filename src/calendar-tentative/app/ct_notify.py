#!/usr/bin/env python3
"""Calendar Tentative Registration — the owner digest, via the gateway socket.

After a run adds tentative entries, the owner gets one Discord DM saying what
was added and how to accept or reject it. This is the "report back" half of
the feature; the calendar itself is where the decision happens.

The shape is the Morning Brief's (`planning/delivery.py`), deliberately:
the gateway's unix socket, Discord only, INFO severity, a deterministic
`notification_id`, and no recipient field — the gateway fixes the recipient
from its own configuration and rejects any field outside its allowlist. There
is no LINE constant in this module, so a digest can never spend a LINE push.

Retry contract (same table as the brief):

    delivered / already_delivered            done
    in_flight / outcome_unknown /
    content_mismatch / rejected              held; never re-sent automatically
    failed / unreachable / unknown           retried later, same id and text
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import socket
from datetime import datetime, time

log = logging.getLogger("calendar_tentative.notify")

CHANNEL_DISCORD = "discord"
SEVERITY_INFO = "INFO"
ID_PREFIX = "cal-tentative"
NOTIFY_PATH = "/internal/notify"
#: `gw_proactive.MAX_NOTIFICATION_CHARS`; longer is a 400, not a truncation.
MAX_TEXT_CHARS = 1200
MAX_ATTEMPTS = 5

DELIVERED = "delivered"
ALREADY_DELIVERED = "already_delivered"
IN_FLIGHT = "in_flight"
OUTCOME_UNKNOWN = "outcome_unknown"
CONTENT_MISMATCH = "content_mismatch"
REJECTED = "rejected"
FAILED = "failed"
UNREACHABLE = "unreachable"
UNKNOWN = "unknown"

SUCCESS = frozenset({DELIVERED, ALREADY_DELIVERED})
HOLD = frozenset({IN_FLIGHT, OUTCOME_UNKNOWN, CONTENT_MISMATCH, REJECTED})
RETRYABLE = frozenset({FAILED, UNREACHABLE, UNKNOWN})

HEADER = "カレンダーに仮登録しました（{n}件）"
FOOTER = "確定するなら自分のカレンダーへ移動、不要なら削除してください。"


def notification_id_for(commitment_ids) -> str:
    """Same set of entries, same id: a retry is recognised by the gateway."""
    digest = hashlib.sha256("\n".join(sorted(commitment_ids)).encode("utf-8")).hexdigest()
    return f"{ID_PREFIX}:{digest[:32]}"


def render_digest(lines, *, max_lines: int) -> str:
    lines = list(lines)
    shown = lines[:max_lines]
    out = [HEADER.format(n=len(lines))]
    out.extend(f"・{line}" for line in shown)
    if len(lines) > len(shown):
        out.append(f"ほか{len(lines) - len(shown)}件")
    out.append(FOOTER)
    text = "\n".join(out)
    while len(text) > MAX_TEXT_CHARS and shown:
        shown = shown[:-1]
        out = [HEADER.format(n=len(lines))]
        out.extend(f"・{line}" for line in shown)
        out.append(f"ほか{len(lines) - len(shown)}件")
        out.append(FOOTER)
        text = "\n".join(out)
    return text


def _hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def in_quiet_hours(now_local: datetime, start: str, end: str) -> bool:
    """[start, end) in local time; a window that crosses midnight wraps."""
    current = now_local.time().replace(second=0, microsecond=0, tzinfo=None)
    begin, finish = _hhmm(start), _hhmm(end)
    if begin == finish:
        return False
    if begin < finish:
        return begin <= current < finish
    return current >= begin or current < finish


def classify_response(status: int, parsed) -> str:
    if not isinstance(parsed, dict):
        parsed = {}
    reported = str(parsed.get("status") or "")
    detail = str(parsed.get("detail") or "")
    if status == 200 and reported == "delivered":
        return DELIVERED
    if status == 200 and reported == "duplicate" and detail == "already_delivered":
        return ALREADY_DELIVERED
    if status == 409 and reported == "in_flight":
        return IN_FLIGHT
    if status == 409 and reported == OUTCOME_UNKNOWN:
        return OUTCOME_UNKNOWN
    if status == 409 and detail == "content_revision_mismatch":
        return CONTENT_MISMATCH
    if status in (400, 409, 413, 503):
        return REJECTED
    if status == 502 or reported == "failed":
        return FAILED
    # Not understood is not success.
    return UNKNOWN if status == 200 else FAILED


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


class GatewayNotifier:
    def __init__(self, socket_path: str, *, timeout: float = 30.0):
        self.socket_path = str(socket_path)
        self.timeout = float(timeout)

    def __repr__(self):
        return f"GatewayNotifier({self.socket_path!r})"

    def send(self, *, notification_id: str, text: str) -> str:
        """Deliver one digest. Never raises; returns an outcome string."""
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
            return REJECTED
        body = {
            "notification_id": notification_id,
            "preferred_channel": CHANNEL_DISCORD,
            "severity": SEVERITY_INFO,
            "text": text,
            "reason": "calendar_tentative_digest",
            "dedupe_key": notification_id,
        }
        conn = _UnixHTTPConnection(self.socket_path, self.timeout)
        try:
            try:
                conn.connect()
            except OSError as exc:
                log.warning("gateway unreachable for %s: %s", notification_id,
                            type(exc).__name__)
                return UNREACHABLE
            try:
                conn.request("POST", NOTIFY_PATH,
                             body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                             headers={"Content-Type": "application/json"})
                resp = conn.getresponse()
                raw = resp.read()
            except (OSError, http.client.HTTPException) as exc:
                log.warning("gateway answer lost for %s: %s", notification_id,
                            type(exc).__name__)
                return UNKNOWN
            try:
                parsed = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError:
                parsed = {}
            return classify_response(resp.status, parsed)
        finally:
            try:
                conn.close()
            except Exception:
                pass
