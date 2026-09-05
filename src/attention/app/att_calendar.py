#!/usr/bin/env python3
"""Calendar authority signals from the published read-only snapshot."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from att_signals import (
    SIG_CALENDAR_CANCELLED,
    SIG_CALENDAR_CHANGED,
    SIG_CALENDAR_DUE_SOON,
    SIG_CALENDAR_UPCOMING,
    SOURCE_CALENDAR,
    make_signal,
)
from att_time import parse_iso


log = logging.getLogger("attention.calendar")
JST = timezone(timedelta(hours=9))


def _connect(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _start(value, all_day):
    if not value:
        return None
    if all_day:
        try:
            return datetime.fromisoformat(str(value)).replace(tzinfo=JST)
        except ValueError:
            return None
    return parse_iso(value)


def _important(row, cfg) -> bool:
    text = str(row["summary"] or "").casefold()
    return any(item.casefold() in text for item in cfg.calendar["important_keywords"] if item.strip())


def _meta(row, *, important=False):
    return {
        "calendar_id": row["calendar_id"],
        "calendar_name": row["calendar_name"],
        "location": row["location"],
        "all_day": bool(row["all_day"]),
        "timezone": row["timezone"],
        "recurrence_state": row["recurrence_state"],
        "important": important,
        "untrusted_calendar_text": True,
    }


def collect(cfg, clock, *, initialized_at=None) -> list:
    if not cfg.calendar.get("enabled", True):
        return []
    path = cfg.sources.get("calendar_index_db") or ""
    if not path:
        return []
    try:
        con = _connect(path)
    except sqlite3.Error:
        return []
    try:
        rows = con.execute("SELECT * FROM calendar_events").fetchall()
        changes = con.execute("SELECT * FROM calendar_changes ORDER BY id").fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    by_id = {row["source_id"]: row for row in rows}
    now = clock.now()
    out = []
    horizon = now + timedelta(hours=int(cfg.calendar["upcoming_hours"]))
    for row in rows:
        if row["status"] == "cancelled":
            continue
        start = _start(row["start_at"], row["all_day"])
        if start is None or not (now <= start <= horizon):
            continue
        important = _important(row, cfg)
        signal_type = SIG_CALENDAR_DUE_SOON if important else SIG_CALENDAR_UPCOMING
        out.append(make_signal(
            source_type=SOURCE_CALENDAR, source_id=row["source_id"], signal_type=signal_type,
            created_at=(row["updated_at"] or row["start_at"] or now.isoformat()),
            subject=row["summary"], summary=row["summary"],
            key_parts=("calendar", row["source_id"], signal_type, row["meaningful_hash"]),
            entity_key=f"calendar:{row['source_id']}",
            severity="IMPORTANT" if important else "INFO", urgency=0.7 if important else 0.1,
            confidence=1.0, actionability=0.8 if important else 0.0,
            due_at=row["start_at"], metadata=_meta(row, important=important),
        ))
    for change in changes:
        row = by_id.get(change["source_id"])
        if row is None or change["baseline"]:
            continue
        if change["change_type"] == "cancelled":
            start = _start(row["start_at"], row["all_day"])
            # Cancellation of an already-past event is history, not an alert.
            if start is None or start <= now:
                continue
            signal_type, severity = SIG_CALENDAR_CANCELLED, "IMPORTANT"
        elif change["change_type"] == "updated":
            signal_type, severity = SIG_CALENDAR_CHANGED, "IMPORTANT"
        else:
            continue
        out.append(make_signal(
            source_type=SOURCE_CALENDAR, source_id=row["source_id"], signal_type=signal_type,
            created_at=change["occurred_at"], subject=row["summary"], summary=row["summary"],
            key_parts=("calendar", row["source_id"], signal_type, change["current_hash"]),
            entity_key=f"calendar:{row['source_id']}", severity=severity, urgency=0.7,
            confidence=1.0, actionability=0.8, due_at=row["start_at"],
            metadata=_meta(row, important=_important(row, cfg)),
        ))
    return out


def linked_event_layer_ids(cfg) -> set[str]:
    """Event Layer rows linked to a current Calendar schedule.

    This is deliberately only a link lookup.  It never treats a fuzzy title
    similarity as authority, and it does not mutate either database.
    """
    if not cfg.calendar.get("enabled", True):
        return set()
    path = cfg.sources.get("calendar_index_db") or ""
    try:
        con = _connect(path)
    except sqlite3.Error:
        return set()
    try:
        rows = con.execute(
            "SELECT l.event_layer_event_id FROM event_links l JOIN calendar_events e "
            "ON e.source_id=l.calendar_source_id WHERE e.status!='cancelled'"
        ).fetchall()
        return {str(row[0]) for row in rows}
    except sqlite3.Error:
        return set()
    finally:
        con.close()
