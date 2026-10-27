#!/usr/bin/env python3
"""Attention Engine — signals from the Event Layer.

The Event Layer (phase B.2) holds commitments, requests, deadlines and
follow-ups extracted from LINE and Discord. This module turns its rows into
canonical signals. It decides *what happened*; it does not decide whether to
tell anyone — that is `att_policy`.

Three rules here exist because of what the production data actually looks
like, read on 2026-09-04 before any of this was written:

* **93 events, 48 of them open, 13 already past their deadline.** D.1
  backfilled a fortnight of Discord history in one afternoon, so a naive
  "overdue" sweep announces thirteen things at once on the first scan, most
  of which passed days ago. Age is judged from `occurred_at` and `due_at`,
  never from `created_at` — which is when the extractor ran (section 25).

* **Not every deadline earns three reminders.** Section 23: the windows an
  event qualifies for depend on how actionable it is. "鈴木一郎が来週の役員会を
  欠席予定" (actionability 0.3) is a fact to have in a digest; it is not worth
  three separate reminders, and it is certainly not worth a metered push.

* **`actor` says who has to move.** 86 of 93 events are `counterparty`, and
  the field distinguishes "someone asked you" from "you promised someone".
  Both can be actionable; "someone else promised something" usually is not.
"""

from __future__ import annotations

import logging
import json

from att_signals import (
    SIG_DUE_SOON,
    SIG_NEW_COMMITMENT,
    SIG_OVERDUE,
    SIG_SNOOZE_EXPIRED,
    SIG_WAITING_FOR_USER,
    SOURCE_EVENT,
    make_signal,
)
from att_sources import SourceUnavailable, open_source, table_exists
from att_time import parse_iso

log = logging.getLogger("attention.events")

ACTIONABLE_TYPES = ("request", "commitment", "deadline", "follow_up")


def retire_non_owner_backlog(con):
    """Prevent old digest/deferred decisions from bypassing the new collector.

    Append an audit decision; keep signals, delivery history and source evidence.
    Calendar, health and coding signals use different source types.
    """
    from att_store import record_decision
    rows = con.execute(
        "SELECT s.* FROM signals s JOIN attention_decisions d ON s.signal_id=d.signal_id "
        "WHERE s.source_type='event' AND d.decision IN ('DEFER','DIGEST') "
        "AND d.decision_id=(SELECT MAX(decision_id) FROM attention_decisions WHERE signal_id=s.signal_id)"
    ).fetchall()
    count = 0
    for row in rows:
        try:
            meta = json.loads(row['metadata'] or '{}')
        except (ValueError, TypeError):
            meta = {}
        if (isinstance(meta, dict) and meta.get('actor') == 'user'
                and meta.get('event_type') in {'commitment', 'deadline', 'follow_up'}
                and not meta.get('needs_human_review')):
            continue
        record_decision(con, signal_id=row['signal_id'], decision='SUPPRESS',
                        reason='owner_not_confirmed', severity=row['severity'])
        count += 1
    return count


def window_label(hours: float) -> str:
    """A stable, readable name for a due window — part of the dedupe key.

    Must not change for a given number, ever: it is half the identity of the
    signal, so renaming '30m' to '0.5h' would make every pending reminder
    look new and fire a second time (section 16).
    """
    if hours >= 1:
        return f"{int(hours)}h" if float(hours).is_integer() else f"{hours}h"
    return f"{int(round(hours * 60))}m"


def windows_for(actionability: float, urgency: float, windows) -> list:
    """Which due windows this event qualifies for (section 23).

    Deterministic and deliberately coarse. The alternative — every event gets
    every window — turns one deadline into three notifications, and with 39
    dated events in the production table that is over a hundred messages for
    a fortnight's work.
    """
    ordered = sorted({float(w) for w in windows}, reverse=True)
    if not ordered:
        return []
    # Returned largest-first because the qualification below is "which of the
    # coarser warnings does this event earn". The caller re-sorts ascending to
    # pick the tightest window that currently applies.
    a = float(actionability or 0.0)
    u = float(urgency or 0.0)
    if a >= 0.7 and u >= 0.6:
        return ordered                      # act now, and remind me twice more
    if a >= 0.5:
        return ordered[:2]                  # one warning, one reminder
    return ordered[:1]                      # a single heads-up


def due_severity(window_hours: float, actionability: float) -> str:
    """Severity from how little time is left and whether the owner can act.

    Deliberately never CRITICAL. CRITICAL is for a system that has stopped
    working; a deadline the owner still has half an hour to meet is URGENT,
    and calling it CRITICAL would spend the emergency LINE reserve on a
    calendar entry (sections 52, 101).
    """
    actionable = float(actionability or 0.0) >= 0.5
    if window_hours <= 1:
        return "URGENT" if actionable else "IMPORTANT"
    if window_hours <= 6:
        return "IMPORTANT" if actionable else "INFO"
    return "INFO"


def _event_meta(row) -> dict:
    return {
        "event_type": row["type"],
        "actor": row["actor"],
        "status": row["status"],
        "project_id": row["project_id"],
        "source": row["source_type"],
        "needs_human_review": int(row["needs_human_review"] or 0),
        "surface_count": int(row["surface_count"] or 0),
    }


def collect(cfg, clock, *, initialized_at=None) -> list:
    """Every event signal the Event Layer justifies right now.

    Returns signals, not notifications. A signal here may well end up
    SUPPRESSED — most of them do, which is the point of the phase.
    """
    path = cfg.sources["events_db"]
    try:
        con = open_source(path)
    except SourceUnavailable as exc:
        log.warning("event source unavailable: %s", exc)
        return []
    try:
        if not table_exists(con, "events"):
            log.warning("event source has no events table: %s", path)
            return []
        rows = con.execute("SELECT * FROM events").fetchall()
        dismissed_event_ids = set()
        if table_exists(con, "commitments"):
            try:
                d_rows = con.execute(
                    "SELECT ce.source_id FROM commitment_evidence ce "
                    "JOIN commitments c ON ce.commitment_id = c.commitment_id "
                    "WHERE c.work_state IN ('DISMISSED', 'DONE', 'CANCELLED', 'BLOCKED', 'WAITING') "
                    "OR c.responsibility <> 'SELF'"
                ).fetchall()
                dismissed_event_ids = {str(r[0]) for r in d_rows}
            except Exception:
                pass
    except Exception:
        log.exception("event source query failed")
        return []
    finally:
        con.close()

    # A deterministic Calendar link is higher-authority evidence of the
    # current scheduled time/status.  Keep the original Event Layer row
    # untouched, but do not manufacture a second chat-derived reminder.
    try:
        from att_calendar import linked_event_layer_ids
        calendar_linked = linked_event_layer_ids(cfg)
    except Exception:
        calendar_linked = set()
    out = []
    for row in rows:
        if str(row["event_id"]) in calendar_linked:
            continue
        if str(row["event_id"]) in dismissed_event_ids or str(row["source_id"] or "") in dismissed_event_ids:
            continue
        try:
            out.extend(signals_for_event(row, cfg, clock))
        except Exception:
            # One malformed row must not cost the whole scan. The id is safe
            # to log; the summary is not logged at all.
            log.exception("event %s could not be normalised", row["event_id"])
    return out


def signals_for_event(row, cfg, clock) -> list:
    # Proactive task reminders require a confirmed owner action. Received
    # requests can target another group member; the speaker is not assignee.
    if (str(row["actor"]) != "user"
            or str(row["type"]) not in {"commitment", "deadline", "follow_up"}
            or bool(row["needs_human_review"])):
        return []
    ev = cfg.events
    now = clock.now()
    status = str(row["status"] or "").lower()

    # Sections 26 and 27, before anything else is computed. A resolved event
    # is not a quiet event, it is not an event at all, and a snoozed one has
    # been explicitly deferred by the owner.
    if status in {s.lower() for s in ev["statuses_ignored"]}:
        return []

    snoozed_until = parse_iso(row["snoozed_until"])
    if snoozed_until and snoozed_until > now:
        return []

    occurred = parse_iso(row["occurred_at"]) or parse_iso(row["created_at"])
    due = parse_iso(row["due_at"])
    confidence = float(row["confidence"] or 0.0)
    actionability = float(row["actionability"] or 0.0)
    urgency = float(row["urgency"] or 0.0)
    summary = row["summary"] or ""
    subject = summary
    meta = _event_meta(row)
    signals = []

    def build(signal_type, key_parts, severity, **kw):
        return make_signal(
            source_type=SOURCE_EVENT, source_id=row["event_id"],
            signal_type=signal_type,
            # The event's own time, not the extractor's. This is the field a
            # backfill would otherwise corrupt.
            created_at=(occurred or now).isoformat(),
            subject=subject, summary=summary,
            key_parts=key_parts, entity_key=f"event:{row['event_id']}",
            severity=severity, urgency=urgency, confidence=confidence,
            actionability=actionability,
            due_at=(due.isoformat() if due else None),
            metadata=dict(meta, **kw),
        )

    # -- snooze expiry (section 27) ----------------------------------
    if snoozed_until and snoozed_until <= now and status in ev["statuses_open"]:
        signals.append(build(
            SIG_SNOOZE_EXPIRED,
            ("event", row["event_id"], "snooze-expired",
             snoozed_until.strftime("%Y%m%dT%H%M")),
            "IMPORTANT" if actionability >= 0.5 else "INFO",
            snoozed_until=snoozed_until.isoformat(),
        ))

    if status not in {s.lower() for s in ev["statuses_open"]}:
        # `candidate` and anything else unrecognised: not open, not ignored.
        # A low-confidence candidate is digest material at best (section 24),
        # and it gets there through the new-commitment path below only if it
        # is recent — never through a deadline reminder.
        return signals

    # -- deadlines ----------------------------------------------------
    if due is not None:
        remaining = (due - now).total_seconds()
        grace = float(ev["overdue_grace_minutes"]) * 60
        if remaining > 0:
            # Ascending: the TIGHTEST window that still applies is the one
            # that describes the situation. Twenty-five minutes before a
            # deadline is a 30-minute reminder, not a 24-hour one — and the
            # difference is INFO versus URGENT, which is the difference
            # between a digest line and a metered push.
            for hours in sorted(windows_for(actionability, urgency,
                                            ev["due_windows_hours"])):
                if remaining <= hours * 3600:
                    label = window_label(hours)
                    signals.append(build(
                        SIG_DUE_SOON,
                        ("event", row["event_id"], "due-soon", label),
                        due_severity(hours, actionability),
                        window=label, remaining_seconds=int(remaining),
                    ))
                    # Only the tightest window that currently applies. At 23
                    # hours out that is the 24h reminder; the 3h one becomes a
                    # separate signal, with its own key, when 3 hours remain
                    # (sections 64, 65).
                    break
        elif -remaining > grace:
            overdue_seconds = -remaining
            max_age = float(ev["overdue_max_age_days"]) * 86400
            # Past this, it is not "overdue", it is history. Something a week
            # late is not news at 08:05 every morning for the rest of the year.
            if overdue_seconds <= max_age:
                signals.append(build(
                    SIG_OVERDUE,
                    ("event", row["event_id"], "overdue"),
                    "IMPORTANT" if actionability >= ev["min_actionability_notify"]
                    else "INFO",
                    overdue_seconds=int(overdue_seconds),
                ))

    # -- something new worth knowing about ----------------------------
    # Bounded by when it HAPPENED. An event extracted today from a Discord
    # message posted twelve days ago is not new (section 25).
    if occurred is not None:
        age_days = (now - occurred).total_seconds() / 86400.0
        if (age_days <= float(ev["max_source_age_days"])
                and str(row["type"]) in ACTIONABLE_TYPES
                and confidence >= float(ev["min_confidence_digest"])):
            waiting = (str(row["actor"]) == "counterparty"
                       and str(row["type"]) == "request"
                       and actionability >= float(ev["min_actionability_notify"]))
            signals.append(build(
                SIG_WAITING_FOR_USER if waiting else SIG_NEW_COMMITMENT,
                ("event", row["event_id"], "new"),
                "IMPORTANT" if (actionability >= float(ev["min_actionability_notify"]) and confidence >=
                                float(ev["min_confidence_notify"])) else "INFO",
                waiting_for_user=bool(waiting), age_days=round(age_days, 2),
            ))

    return signals
