#!/usr/bin/env python3
"""Attention Engine — the policy. Deterministic, ordered, explainable.

This is the file the phase is about. Everything else collects facts or moves
bytes; this decides whether a person is interrupted.

Three properties are structural rather than aspirational:

**No model is consulted.** Every rule below is arithmetic and comparison
(sections 18, 19). "The assistant judged it important" is not a decision
record; a rule name and a policy version are (sections 77, 79). It also makes
"no signals means zero LLM calls" true by construction rather than by
inspection of a code path (sections 21, 93).

**The order is the policy.** Rules run cheapest-and-most-absolute first, and
the FIRST one that matches wins. That matters because several can apply at
once — a duplicate low-confidence event during quiet hours is all three — and
the reason recorded should be the one that would still have stopped it if the
others were removed.

**Every outcome is one of four**, and SUPPRESS is the common one. Ten things
found, seven suppressed, two folded into a digest, one delivered is the shape
of a good day (section 3), not a sign that the collectors are broken.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import att_budget as budget_mod
import att_store as store
from att_signals import SOURCE_CALENDAR, SOURCE_CODING, SOURCE_EVENT, SOURCE_HEALTH
from att_time import parse_iso

NOTIFY_NOW = "NOTIFY_NOW"
DEFER = "DEFER"
DIGEST = "DIGEST"
SUPPRESS = "SUPPRESS"

# The reason vocabulary (section 78). Every decision carries exactly one of
# these, and they are stable strings so that a month of decisions can be
# counted by reason without parsing prose.
R_OWNER_SUPPRESSED = "suppressed_by_owner"
R_SOURCE_DISABLED = "source_notifications_disabled"
R_BASELINE = "baseline_pre_existing"
R_STALE_SOURCE = "stale_historical_source"
R_EXPIRED = "signal_expired"
R_DUPLICATE = "suppressed_duplicate"
R_ENTITY_ALREADY_NOTIFIED = "entity_already_notified_this_scan"
R_LOW_CONFIDENCE = "low_confidence"
R_LOW_ACTIONABILITY = "low_actionability"
R_QUIET_HOURS = "quiet_hours"
R_QUIET_HOURS_CRITICAL = "quiet_hours_critical_override"
R_QUIET_HOURS_URGENT = "quiet_hours_urgent_time_sensitive"
R_BURST_LIMIT = "burst_limit_reached"
R_DOWNTIME_RECOVERY = "downtime_recovery_digest"
R_URGENT_DUE = "urgent_due_window"
R_DUE_WINDOW = "due_window"
R_OVERDUE = "overdue"
R_WAITING_HUMAN = "waiting_human"
R_CODING_COMPLETED = "coding_completed"
R_CODING_REVIEW_LIMIT = "coding_review_cycle_limit"
R_HEALTH_FAILURE = "health_sustained_failure"
R_HEALTH_RECOVERED = "health_recovered"
R_NEW_ITEM = "new_actionable_item"
R_ROUTINE = "routine"
R_CALENDAR_CHANGED = "calendar_significant_change"
R_CALENDAR_CANCELLED = "calendar_event_cancelled"
R_CALENDAR_ROUTINE = "calendar_routine_suppressed"

DISCORD = "discord"
LINE = "line"

#: A deadline is only "time sensitive" if acting on it tonight actually
#: matters. Section 56: a deadline next Tuesday does not get to wake anyone at
#: 02:00 just because the signal is labelled urgent.
TIME_SENSITIVE_HORIZON_SECONDS = 12 * 3600


@dataclass
class Decision:
    decision: str
    reason: str
    severity: str
    channels: list = field(default_factory=list)
    deliver_after: str | None = None
    detail: str = ""
    budget_state: str = ""

    @property
    def sends_now(self) -> bool:
        return self.decision == NOTIFY_NOW


@dataclass
class Context:
    """Per-scan state the policy needs but does not own."""

    initialized_at: str | None = None
    baseline_done: bool = True
    immediate_used: int = 0
    budget: object = None
    #: Set when this scan follows a long gap. Section 96: coming back after
    #: hours of downtime must not fire a hundred individual notifications for
    #: everything that happened while the engine was away.
    recovery_mode: bool = False
    #: Whether the pre-existing backlog is worth one summary. When false the
    #: baseline is silent instead.
    baseline_digest: bool = True
    #: Entities already interrupted for during this scan. One deadline can
    #: legitimately produce several signals at once — "this is new" and "this
    #: is due in twenty minutes" are both true — but they are one thing to
    #: act on, and two messages about it is two interruptions.
    notified_entities: set = field(default_factory=set)

    def take_immediate_slot(self, entity_key: str = "") -> None:
        self.immediate_used += 1
        if entity_key:
            self.notified_entities.add(entity_key)


def _source_enabled(source_type: str, cfg) -> bool:
    n = cfg.notifications
    return {
        SOURCE_EVENT: bool(n.get("event_notifications", True)),
        SOURCE_CODING: bool(n.get("coding_completion_notifications", True)),
        SOURCE_HEALTH: bool(n.get("health_notifications", True)),
        SOURCE_CALENDAR: bool(n.get("calendar_notifications", True)),
    }.get(source_type, True)


def time_sensitive(row, clock) -> bool:
    """Whether acting in the next few hours is what makes this matter.

    Health failures always qualify: a service that has been down long enough
    to be announced is not waiting for office hours. A deadline qualifies only
    if it is close (section 56).
    """
    if row["source_type"] == SOURCE_HEALTH:
        return True
    if row["signal_type"] in ("coding_waiting_human", "coding_review_cycle_limit"):
        return False
    due = parse_iso(row["due_at"])
    if due is None:
        return False
    return (due - clock.now()).total_seconds() <= TIME_SENSITIVE_HORIZON_SECONDS


def base_reason(row) -> str:
    """The reason a signal would be sent, before anything suppresses it."""
    return {
        "due_soon": R_DUE_WINDOW,
        "overdue": R_OVERDUE,
        "waiting_for_user": R_NEW_ITEM,
        "new_high_confidence_commitment": R_NEW_ITEM,
        "snooze_expired": R_NEW_ITEM,
        "coding_completed": R_CODING_COMPLETED,
        "coding_waiting_human": R_WAITING_HUMAN,
        "coding_review_cycle_limit": R_CODING_REVIEW_LIMIT,
        "coding_failed_to_start": R_WAITING_HUMAN,
        "health_degraded": R_HEALTH_FAILURE,
        "health_recovered": R_HEALTH_RECOVERED,
        "calendar_event_changed": R_CALENDAR_CHANGED,
        "calendar_event_cancelled": R_CALENDAR_CANCELLED,
    }.get(row["signal_type"], R_ROUTINE)


def cooldown_for(row, cfg) -> int:
    n = cfg.notifications
    if row["source_type"] == SOURCE_HEALTH:
        return int(n["health_cooldown_seconds"])
    return int(n["cooldown_seconds"])


def channels_for(severity: str, cfg, budget) -> tuple:
    """(channels, budget_reason). The channel policy of section 9.

    Severity does not select a channel by itself (section 53): an IMPORTANT
    notification goes to Discord whatever the LINE budget says, and an URGENT
    one goes to LINE only if the budget allows it. What comes back is the set
    of channels to actually deliver on — for URGENT and CRITICAL that is
    deliberately both, so a single failing platform cannot silence something
    that matters (sections 84, 85).
    """
    discord_on = bool(cfg.channels.get("discord_enabled", True))
    line_ok, line_reason = budget_mod.line_allowed(budget, severity, cfg)

    if severity == "CRITICAL":
        chans = ([LINE] if line_ok else []) + ([DISCORD] if discord_on else [])
    elif severity == "URGENT":
        chans = ([DISCORD] if discord_on else []) + ([LINE] if line_ok else [])
    else:
        chans = [DISCORD] if discord_on else []
        if not chans and line_ok:
            chans = [LINE]

    if not chans:
        # Both channels refused. Better to keep the notification PENDING and
        # say so than to drop it (section 83).
        return [], line_reason
    return chans, line_reason


def decide(con, row, cfg, clock, ctx: Context) -> Decision:
    """One signal in, one decision out. Pure apart from two DB lookups.

    The two reads — the owner's suppression list and the recent-delivery
    check — are what make "have I already said this?" a fact rather than a
    guess, so they belong here rather than in the caller.
    """
    severity = str(row["severity"])
    dedupe_key = str(row["dedupe_key"])
    now = clock.now()
    budget = ctx.budget

    def out(decision, reason, **kw):
        return Decision(decision=decision, reason=reason, severity=severity,
                        budget_state=getattr(budget, "state", ""), **kw)

    # 1. The owner said not this one. Nothing overrides that.
    if store.is_suppressed(con, dedupe_key, now=now.isoformat()):
        return out(SUPPRESS, R_OWNER_SUPPRESSED)

    # 2. A whole source switched off in configuration (section 105).
    if not _source_enabled(row["source_type"], cfg):
        return out(SUPPRESS, R_SOURCE_DISABLED)

    # 3. It predates the engine. Everything that was already true when this
    #    was installed is history — 48 open events, 13 of them already overdue
    #    at the moment of writing — and announcing it individually would be a
    #    flood on the first scan (sections 25, 95, 96).
    if not ctx.baseline_done and store.initialized_before(
            row["created_at"], ctx.initialized_at):
        # DIGEST rather than SUPPRESS: not one of these is worth an
        # interruption, but the fact that eleven deadlines have already passed
        # is worth one message. Measured on the first real scan: 25 signals,
        # 25 held back, one summary (sections 25, 96).
        return out(DIGEST if ctx.baseline_digest else SUPPRESS, R_BASELINE)

    # 4. Old news that arrived late. Distinct from the baseline rule: this one
    #    keeps applying forever, so a fortnight-old message ingested next
    #    month is still not news (section 25).
    created = parse_iso(row["created_at"])
    if row["source_type"] == SOURCE_EVENT and created is not None:
        max_age = float(cfg.events["max_source_age_days"]) * 86400
        age = (now - created).total_seconds()
        due = parse_iso(row["due_at"])
        # A deadline reminder is exempt: what matters there is the deadline
        # ahead, not when the request was made.
        forward_looking = due is not None and due >= now
        if age > max_age and not forward_looking and row["signal_type"] != "overdue":
            return out(SUPPRESS, R_STALE_SOURCE)

    # 5. Past its usefulness.
    expires = parse_iso(row["expires_at"])
    if expires is not None and expires <= now:
        return out(SUPPRESS, R_EXPIRED)

    # 6. Already said, recently. The deterministic duplicate rule of section
    #    18 — and the reason a 24-hour reminder and a 3-hour reminder are
    #    different dedupe keys rather than one key with a short cooldown.
    if store.recent_delivery(con, dedupe_key, cooldown_for(row, cfg),
                             now=now) is not None:
        return out(SUPPRESS, R_DUPLICATE)

    # 7. Something else about this same thing is already being sent in this
    #    scan. Still worth recording, still worth a digest line; not worth a
    #    second buzz about one deadline.
    if str(row["entity_key"]) in ctx.notified_entities:
        return out(DIGEST, R_ENTITY_ALREADY_NOTIFIED)

    # 8. The extractor was not sure. Section 24: a low-confidence candidate is
    #    never an immediate proactive notification.
    confidence = float(row["confidence"] or 0.0)
    if row["source_type"] == SOURCE_EVENT:
        if confidence < float(cfg.events["min_confidence_digest"]):
            return out(SUPPRESS, R_LOW_CONFIDENCE)
        if confidence < float(cfg.events["min_confidence_notify"]):
            return out(DIGEST, R_LOW_CONFIDENCE)

    # 9. Nothing for the owner to do about it. Worth having in a digest,
    #    not worth an interruption.
    actionability = float(row["actionability"] or 0.0)
    # Calendar itself owns normal reminders.  A Calendar event becomes an
    # interruption only for an explicitly configured important keyword, or
    # when it materially moved/cancelled.  This avoids a duplicate feed.
    if row["source_type"] == SOURCE_CALENDAR and row["signal_type"] == "calendar_upcoming_event":
        return out(SUPPRESS, R_CALENDAR_ROUTINE)
    if (severity == "INFO" and row["source_type"] == SOURCE_EVENT
            and actionability < float(cfg.events["min_actionability_notify"])):
        return out(DIGEST, R_LOW_ACTIONABILITY)

    # 10. Quiet hours (sections 54, 55, 56).
    local = clock.local()
    if cfg.quiet.contains(local):
        if severity == "CRITICAL":
            reason = R_QUIET_HOURS_CRITICAL
        elif severity == "URGENT" and time_sensitive(row, clock):
            reason = R_QUIET_HOURS_URGENT
        elif severity == "INFO":
            return out(DIGEST, R_QUIET_HOURS)
        else:
            # IMPORTANT, and URGENT that is not actually time sensitive.
            release = cfg.quiet.next_end(local)
            return out(DEFER, R_QUIET_HOURS,
                       deliver_after=release.astimezone(now.tzinfo).isoformat(),
                       detail=f"held until {release.strftime('%H:%M %Z')}")
        chans, budget_reason = channels_for(severity, cfg, budget)
        if not chans:
            return out(SUPPRESS, budget_reason)
        ctx.take_immediate_slot(str(row["entity_key"]))
        return out(NOTIFY_NOW, reason, channels=chans, detail=budget_reason)

    # 11. Coming back from downtime. Everything except a genuine emergency
    #     is folded into one recovery digest rather than delivered one by one
    #     (section 96).
    if ctx.recovery_mode and severity != "CRITICAL":
        return out(DIGEST, R_DOWNTIME_RECOVERY)

    # 12. The burst cap (section 97). Anything past it becomes digest material
    #     rather than being dropped — the information is kept, the interruption
    #     is not repeated.
    if ctx.immediate_used >= int(cfg.notifications["max_immediate_per_cycle"]):
        if severity == "CRITICAL":
            pass          # a genuine emergency is never held back by a cap
        else:
            return out(DIGEST, R_BURST_LIMIT)

    # 13. Send it.
    chans, budget_reason = channels_for(severity, cfg, budget)
    if not chans:
        # Every channel refused. For a routine notification that is a digest;
        # for anything higher it is worth saying that the budget stopped it.
        if severity in ("INFO", "IMPORTANT"):
            return out(DIGEST, budget_reason)
        return out(SUPPRESS, budget_reason)

    reason = base_reason(row)
    if severity == "URGENT" and row["signal_type"] == "due_soon":
        reason = R_URGENT_DUE
    ctx.take_immediate_slot(str(row["entity_key"]))
    return out(NOTIFY_NOW, reason, channels=chans, detail=budget_reason)
