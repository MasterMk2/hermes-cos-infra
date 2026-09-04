#!/usr/bin/env python3
"""Unified Channel Gateway — the LINE message quota reader.

Two endpoints, read-only, and the only reason they live in the gateway rather
than in the Attention Engine is that they need the channel access token — and
the engine must never hold one (spec section 48). The engine asks the gateway
"how much LINE budget is there?"; the gateway answers with facts and keeps the
credential.

Verified against LINE's own OpenAPI description (line/line-openapi,
messaging-api.yml, read 2026-09-04) rather than from memory:

    GET /v2/bot/message/quota
        -> {"type": "none" | "limited", "value": <int>}
        `value` is present only when type is "limited". "Gets the target limit
        for sending messages in the current month. The total number of the
        free messages and the additional messages is returned."

    GET /v2/bot/message/quota/consumption
        -> {"totalUsage": <int>}
        "The number of sent messages in the current month."

Three semantics that decide policy and are easy to get backwards:

* **`type: "none"` is NOT "unknown".** It means no target limit is set on the
  account — sending is not capped. Mapping it to UNKNOWN would throttle a
  channel that has no cap at all. UNKNOWN is reserved for "the API did not
  answer": a 401, a 5xx, a timeout (sections 6, 8).

* **`totalUsage` is account-wide for the calendar month**, and it already
  includes the gateway's own conversational pushes. It is the billing
  authority; the Attention Engine's local ledger is a policy aid that closes
  the gap between one send and the next quota refresh, not a figure that must
  reconcile exactly (section 81).

* **Only chargeable sends are counted, per recipient.** Reply messages are
  free and are not in `totalUsage`; push, multicast, broadcast and narrowcast
  are counted, and the count is by number of people addressed — the number of
  message objects in one request does not matter (developers.line.biz,
  Messaging API pricing, read 2026-09-04). So one push carrying five chunks
  costs one, which is what `deliver_line` already relies on.

An authentication failure is remembered and backed off hard. A revoked token
answers 401 identically on every call, and a five-minute refresh loop against
it is exactly the kind of retry storm section 120 forbids.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone

import aiohttp

QUOTA_ENDPOINT = "https://api.line.me/v2/bot/message/quota"
CONSUMPTION_ENDPOINT = "https://api.line.me/v2/bot/message/quota/consumption"

FETCH_TIMEOUT_SECONDS = 10

#: How long a successful read stays fresh. Section 80 asks for 15-60 minutes;
#: the engine also refreshes immediately after it spends a push, so a long TTL
#: costs accuracy only for sends made by something other than the engine.
DEFAULT_CACHE_SECONDS = 1800

#: After a 401/403 the credential is wrong, not busy. Nothing about waiting
#: five minutes makes a revoked token valid, so wait an hour and say so.
AUTH_BACKOFF_SECONDS = 3600

#: After a 5xx/timeout, back off briefly and try again on the next scan.
TRANSIENT_BACKOFF_SECONDS = 300

QUOTA_TYPE_LIMITED = "limited"
QUOTA_TYPE_NONE = "none"

ERR_AUTH = "auth"
ERR_TRANSIENT = "transient"
ERR_PERMANENT = "permanent"
ERR_MALFORMED = "malformed"


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class QuotaSnapshot:
    """What the gateway knows about LINE's monthly message allowance.

    `ok` False means the numbers are unusable and the caller must treat the
    budget as UNKNOWN — never as unlimited (section 8).
    """

    ok: bool
    quota_type: str | None = None      # 'limited' | 'none' | None
    limit: int | None = None           # present only when quota_type=='limited'
    used: int | None = None            # totalUsage
    fetched_at: str | None = None
    error_category: str | None = None  # ERR_* when ok is False
    stale: bool = False                # served from cache past its TTL

    @property
    def remaining(self) -> int | None:
        """Messages left this month, or None when that is not a number.

        None for an unlimited account (`type: none`) and None for a failed
        read — the caller must distinguish those two, which is why the flag
        is `quota_type`, not a sentinel value here.
        """
        if not self.ok or self.limit is None or self.used is None:
            return None
        return max(0, int(self.limit) - int(self.used))

    @property
    def unlimited(self) -> bool:
        return bool(self.ok and self.quota_type == QUOTA_TYPE_NONE)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "quota_type": self.quota_type,
            "limit": self.limit,
            "used": self.used,
            "remaining": self.remaining,
            "unlimited": self.unlimited,
            "fetched_at": self.fetched_at,
            "error_category": self.error_category,
            "stale": self.stale,
        }


def classify_status(status: int) -> str:
    if status in (401, 403):
        return ERR_AUTH
    if status == 429 or 500 <= status < 600:
        return ERR_TRANSIENT
    return ERR_PERMANENT


def parse_quota(body) -> tuple:
    """(quota_type, limit) from a /quota body, or raise ValueError."""
    if not isinstance(body, dict):
        raise ValueError("quota body is not an object")
    qtype = body.get("type")
    if qtype not in (QUOTA_TYPE_LIMITED, QUOTA_TYPE_NONE):
        # A value LINE has not documented. Refusing it is safer than guessing:
        # an unrecognised type becomes UNKNOWN, and UNKNOWN is conservative.
        raise ValueError(f"unknown quota type {qtype!r}")
    if qtype == QUOTA_TYPE_NONE:
        return qtype, None
    value = body.get("value")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("quota value missing or not a non-negative integer")
    return qtype, value


def parse_consumption(body) -> int:
    """totalUsage from a /quota/consumption body, or raise ValueError."""
    if not isinstance(body, dict):
        raise ValueError("consumption body is not an object")
    used = body.get("totalUsage")
    if not isinstance(used, int) or isinstance(used, bool) or used < 0:
        raise ValueError("totalUsage missing or not a non-negative integer")
    return used


class LineQuotaClient:
    """Reads the two quota endpoints, caches the answer, backs off on 401.

    Holds the channel access token and, like `LineClient`, refuses to render
    it: `__repr__` is overridden so a traceback or a debug log cannot print
    the one piece of state this object has.
    """

    def __init__(self, channel_access_token: str, *,
                 cache_seconds: int = DEFAULT_CACHE_SECONDS,
                 auth_backoff_seconds: int = AUTH_BACKOFF_SECONDS,
                 transient_backoff_seconds: int = TRANSIENT_BACKOFF_SECONDS):
        self._token = channel_access_token
        self._cache_seconds = int(cache_seconds)
        self._auth_backoff = int(auth_backoff_seconds)
        self._transient_backoff = int(transient_backoff_seconds)
        self._snapshot: QuotaSnapshot | None = None
        self._fetched_monotonic: float | None = None
        self._blocked_until_monotonic: float = 0.0
        self._lock = asyncio.Lock()

    def __repr__(self):
        return "LineQuotaClient(token=<redacted>)"

    __str__ = __repr__

    # -- cache bookkeeping -------------------------------------------

    def _fresh(self, clock) -> bool:
        return (
            self._snapshot is not None
            and self._snapshot.ok
            and self._fetched_monotonic is not None
            and (clock - self._fetched_monotonic) < self._cache_seconds
        )

    def seed(self, snapshot: QuotaSnapshot, age_seconds: float) -> None:
        """Adopt a snapshot loaded from the durable cache after a restart.

        A restart must not cost a quota call, and more importantly must not
        cost the *knowledge* of a recent 401 — otherwise every restart starts
        a fresh hammering of an endpoint that will refuse it again.
        """
        if snapshot is None:
            return
        clock = self._clock()
        self._snapshot = snapshot
        self._fetched_monotonic = clock - max(0.0, float(age_seconds))
        if not snapshot.ok and snapshot.error_category == ERR_AUTH:
            remaining = self._auth_backoff - max(0.0, float(age_seconds))
            if remaining > 0:
                self._blocked_until_monotonic = clock + remaining

    @staticmethod
    def _clock() -> float:
        # time.monotonic(), not the loop clock: `seed` runs at startup before
        # any loop exists, and a clock that changes meaning between callers is
        # a backoff that silently stops working.
        return time.monotonic()

    # -- the read ----------------------------------------------------

    async def _get_json(self, session, url) -> tuple:
        """(body, error_category). Exactly one HTTP attempt, no retry here.

        Retrying is the caller's business and is spaced by the cache, not by a
        tight loop: these endpoints are informational, and a failed read
        degrades the budget to UNKNOWN, which is already the safe state.
        """
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            async with session.get(
                url, headers=headers,
                timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT_SECONDS),
            ) as resp:
                text = await resp.text()
                if resp.status != 200:
                    return None, classify_status(resp.status)
                try:
                    return json.loads(text), None
                except ValueError:
                    return None, ERR_MALFORMED
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None, ERR_TRANSIENT

    async def get(self, session, *, force: bool = False) -> QuotaSnapshot:
        """The current snapshot, from cache when it is fresh enough.

        Never raises. A failure becomes an `ok=False` snapshot whose
        `error_category` says which kind, because the caller's job is to pick
        a conservative policy, not to handle an exception.
        """
        async with self._lock:
            clock = self._clock()
            if not force and self._fresh(clock):
                return self._snapshot

            if clock < self._blocked_until_monotonic:
                # Still inside a backoff. Report the remembered failure rather
                # than making the call that is known to fail.
                base = self._snapshot or QuotaSnapshot(
                    ok=False, error_category=ERR_AUTH
                )
                return replace(base, ok=False, stale=True,
                               error_category=base.error_category or ERR_AUTH)

            quota_body, err = await self._get_json(session, QUOTA_ENDPOINT)
            if err is not None:
                return self._record_failure(err, clock)
            used_body, err = await self._get_json(session, CONSUMPTION_ENDPOINT)
            if err is not None:
                return self._record_failure(err, clock)

            try:
                qtype, limit = parse_quota(quota_body)
                used = parse_consumption(used_body)
            except ValueError:
                return self._record_failure(ERR_MALFORMED, clock)

            snap = QuotaSnapshot(
                ok=True, quota_type=qtype, limit=limit, used=used,
                fetched_at=_now().isoformat(),
            )
            self._snapshot = snap
            self._fetched_monotonic = clock
            self._blocked_until_monotonic = 0.0
            return snap

    def _record_failure(self, category: str, clock: float) -> QuotaSnapshot:
        if category == ERR_AUTH:
            self._blocked_until_monotonic = clock + self._auth_backoff
        else:
            self._blocked_until_monotonic = clock + self._transient_backoff
        snap = QuotaSnapshot(ok=False, error_category=category,
                             fetched_at=_now().isoformat())
        # Keep the last GOOD numbers out of the failure snapshot on purpose:
        # a stale limit presented as current would let the engine spend
        # against a budget nobody has confirmed since.
        self._snapshot = snap
        self._fetched_monotonic = clock
        return snap

    def note_push_sent(self, count: int = 1) -> None:
        """Account for a push we just made, without another API call.

        The API is the billing authority (section 81) but it is read at most
        every `cache_seconds`; between reads the only way the gateway knows it
        spent something is that it spent it. Adjusting the cached `used`
        upward keeps the next few policy decisions from over-spending on a
        figure that is known to be behind.
        """
        snap = self._snapshot
        if snap is None or not snap.ok or snap.used is None:
            return
        self._snapshot = replace(snap, used=int(snap.used) + int(count))
