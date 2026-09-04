#!/usr/bin/env python3
"""Attention Engine — what LINE may be spent on today.

LINE push messages are metered and reply messages are not (verified against
developers.line.biz on 2026-09-04: push, multicast, broadcast and narrowcast
count toward the plan's quota; reply does not, and the count is by recipient
rather than by message object). A proactive notification is always a push, so
every LINE notification this engine sends costs one of a finite monthly
allowance.

**The monthly figure is never written down here.** It is read from LINE's own
`/v2/bot/message/quota` through the gateway (section 6). On this account that
read returned `{"type": "limited", "value": 200}` with `totalUsage` 2 on
2026-09-04, but that is an observation in a report, not a constant in code:
the plan can change, and a hardcoded 200 would keep spending confidently
against a number that had become wrong.

Two ideas do the work:

**An emergency reserve** (section 101). A slice of the month's allowance is
not available to routine traffic at all. Without it, a busy fortnight spends
the month's budget and the genuine emergency in week four finds nothing left.

**A daily allowance derived from what is actually left** (sections 99, 100).
Not "200 / 30". Remaining budget minus the reserve, divided by the days
remaining in the month, times a factor below 1 so that an unusual day cannot
consume the following week. Arithmetic on the calendar month in the owner's
timezone, because a month boundary computed in UTC is nine hours out here and
that is a whole day of allowance at the wrong moment.

And the state that matters most is the one that is easy to get wrong:
**UNKNOWN is not HEALTHY.** A quota read that failed means the engine has no
idea how much is left, and section 8 says that must not become permission to
send freely.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from att_time import days_remaining_in_month

HEALTHY = "HEALTHY"
CONSERVE = "CONSERVE"
CRITICAL = "CRITICAL"
EXHAUSTED = "EXHAUSTED"
UNKNOWN = "UNKNOWN"

# Why a LINE delivery was refused. These strings land in the decision record
# and in the status output, so they are part of the interface (section 78).
R_DISABLED = "line_escalation_disabled"
R_ROUTINE = "line_routine_disabled"
R_EXHAUSTED = "line_budget_exhausted"
R_UNKNOWN = "line_budget_unknown"
R_CRITICAL_ONLY = "line_budget_critical_only"
R_CONSERVE = "line_budget_conserve"
R_DAILY_SPENT = "line_daily_budget_spent"
R_RESERVE_ONLY = "line_emergency_reserve_only"
R_OK = "line_budget_ok"


@dataclass(frozen=True)
class LineBudget:
    """Everything the policy needs to decide about the metered channel."""

    state: str
    ok: bool = False
    quota_type: str | None = None
    limit: int | None = None
    used: int | None = None
    remaining: int | None = None
    unlimited: bool = False
    reserve: int = 0
    routine_pool: int = 0
    daily_allowance: int = 0
    spent_today: int = 0
    spent_this_month: int = 0
    detail: str = ""

    @property
    def usable_today(self) -> int:
        return max(0, self.daily_allowance - self.spent_today)

    def as_dict(self) -> dict:
        return {
            "state": self.state, "ok": self.ok, "quota_type": self.quota_type,
            "limit": self.limit, "used": self.used,
            "remaining": self.remaining, "unlimited": self.unlimited,
            "reserve": self.reserve, "routine_pool": self.routine_pool,
            "daily_allowance": self.daily_allowance,
            "spent_today": self.spent_today,
            "spent_this_month": self.spent_this_month,
            "usable_today": self.usable_today, "detail": self.detail,
        }


def classify(remaining, limit, cfg_budget) -> str:
    if not limit:
        return UNKNOWN
    if remaining is None:
        return UNKNOWN
    if remaining <= 0:
        return EXHAUSTED
    ratio = float(remaining) / float(limit)
    if ratio > float(cfg_budget["healthy_above"]):
        return HEALTHY
    if ratio > float(cfg_budget["conserve_above"]):
        return CONSERVE
    return CRITICAL


def evaluate(payload, cfg, clock, *, spent_today: int = 0,
             spent_this_month: int = 0) -> LineBudget:
    """Turn the gateway's quota answer into a budget the policy can use.

    `payload` is whatever `/internal/line_budget` returned, or None. Every
    path through this function that cannot produce real numbers produces
    UNKNOWN with a tiny allowance — never a large one, and never unlimited.
    """
    lb = cfg.line_budget
    hard_cap = int(lb["max_per_day_hard_cap"])

    if not payload or not payload.get("ok"):
        detail = "quota unavailable"
        if isinstance(payload, dict) and payload.get("error_category"):
            detail = f"quota unavailable ({payload['error_category']})"
        return LineBudget(
            state=UNKNOWN, ok=False,
            daily_allowance=min(int(lb["unknown_max_per_day"]), hard_cap),
            spent_today=spent_today, spent_this_month=spent_this_month,
            detail=detail,
        )

    quota_type = payload.get("quota_type")
    limit = payload.get("limit")
    used = payload.get("used")
    remaining = payload.get("remaining")
    unlimited = bool(payload.get("unlimited"))

    if unlimited:
        # `type: "none"` — no target limit is set on the account. That is a
        # real, healthy answer and not an unknown one (section 6). The daily
        # hard cap still applies: an uncapped plan is not a reason to make
        # LINE the routine feed (section 10).
        return LineBudget(
            state=HEALTHY, ok=True, quota_type=quota_type, used=used,
            unlimited=True, daily_allowance=hard_cap,
            spent_today=spent_today, spent_this_month=spent_this_month,
            detail="no monthly target limit set on this account",
        )

    state = classify(remaining, limit, lb)
    reserve = int(math.ceil(float(limit or 0)
                            * float(lb["emergency_reserve_fraction"])))
    routine_pool = max(0, int(remaining or 0) - reserve)

    days_left = days_remaining_in_month(clock.local())
    raw = (routine_pool / days_left) * float(lb["daily_spend_factor"])
    allowance = int(math.floor(raw))
    if routine_pool > 0:
        allowance = max(int(lb["min_daily_allowance"]), allowance)
    allowance = min(allowance, hard_cap)
    if state == EXHAUSTED:
        allowance = 0

    return LineBudget(
        state=state, ok=True, quota_type=quota_type, limit=limit, used=used,
        remaining=remaining, unlimited=False, reserve=reserve,
        routine_pool=routine_pool, daily_allowance=allowance,
        spent_today=spent_today, spent_this_month=spent_this_month,
        detail=(f"{remaining}/{limit} left, reserve {reserve}, "
                f"{days_left}d remaining this month"),
    )


def line_allowed(budget: LineBudget, severity: str, cfg) -> tuple:
    """(allowed, reason). The whole of the metered-channel policy.

    Ordered from the cheapest, most absolute refusal to the most nuanced, so
    that the reason recorded on the decision is the most informative true one
    (section 78).
    """
    channels = cfg.channels
    routine = severity in ("INFO", "IMPORTANT")
    emergency = severity == "CRITICAL"

    if not channels.get("line_escalation_enabled"):
        return False, R_DISABLED
    if routine and not channels.get("routine_line_enabled"):
        # Section 10: LINE is an escalation channel. Routine traffic goes to
        # Discord however healthy the budget looks.
        return False, R_ROUTINE
    if budget.state == EXHAUSTED:
        return False, R_EXHAUSTED
    if budget.state == UNKNOWN:
        # Section 8: with no reading at all, urgent and critical only, and
        # even those are capped for the day.
        if routine:
            return False, R_UNKNOWN
        if budget.spent_today >= budget.daily_allowance:
            return False, R_DAILY_SPENT
        return True, R_UNKNOWN
    if budget.state == CRITICAL and not emergency:
        # Under a fifth of the month left: only a genuine emergency.
        return False, R_CRITICAL_ONLY
    if budget.state == CONSERVE and routine:
        return False, R_CONSERVE

    if not emergency and not budget.unlimited and budget.routine_pool <= 0:
        # What is left is the emergency reserve, and this is not an emergency.
        return False, R_RESERVE_ONLY

    if budget.spent_today >= budget.daily_allowance:
        # CRITICAL may exceed the day's share — that is what the reserve is
        # for — but only while LINE can physically still deliver.
        if emergency and (budget.unlimited or (budget.remaining or 0) > 0):
            return True, "line_emergency_override"
        return False, R_DAILY_SPENT

    return True, R_OK
