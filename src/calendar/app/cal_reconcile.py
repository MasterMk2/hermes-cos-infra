#!/usr/bin/env python3
"""Deterministic links between Calendar and chat-derived Event Layer rows."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime

import cal_store


def normalize_title(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").casefold()).strip()


def _parse(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def deterministic_candidates(events_db: str, calendar_row, *, tolerance_seconds: int = 300) -> list[str]:
    """Unique exact-title/time matches only; an ambiguous candidate never merges."""
    target_title = normalize_title(calendar_row["summary"])
    target_start = _parse(calendar_row["start_at"])
    if not target_title or target_start is None:
        return []
    con = sqlite3.connect(f"file:{events_db}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("SELECT event_id,summary,due_at,status FROM events WHERE status='open'").fetchall()
    finally:
        con.close()
    matched = []
    for row in rows:
        if normalize_title(row["summary"]) != target_title:
            continue
        due = _parse(row["due_at"])
        if due and abs((due - target_start).total_seconds()) <= tolerance_seconds:
            matched.append(str(row["event_id"]))
    return matched


def reconcile_calendar(con, *, events_db: str, calendar_id: str) -> int:
    """Link explicit records first, then only unique deterministic candidates.

    An H.2 reconciliation projection contains no Personal Hermes secrets and
    can therefore be safely read by the Calendar service. It may additionally
    carry an operator-confirmed explicit source mapping; title similarity alone
    never creates that mapping.
    """
    count = 0
    rows = con.execute(
        "SELECT * FROM events WHERE calendar_id=? AND status!='cancelled'", (calendar_id,)
    ).fetchall()
    for row in rows:
        linked = con.execute("SELECT 1 FROM event_source_links WHERE calendar_source_id=?", (row["source_id"],)).fetchone()
        if linked:
            continue
        # Explicit links are checked per exact Calendar source identifier.
        source = sqlite3.connect(f"file:{events_db}?mode=ro", uri=True, timeout=5)
        source.row_factory = sqlite3.Row
        try:
            table = source.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_calendar_links'"
            ).fetchone()
            explicit = []
            if table:
                explicit = source.execute(
                    """SELECT event_id,confidence FROM event_calendar_links
                       WHERE calendar_source_id=? AND link_type='explicit'""",
                    (row["source_id"],),
                ).fetchall()
        finally:
            source.close()
        if explicit:
            for item in explicit:
                cal_store.link_event(
                    con, row["source_id"], str(item["event_id"]),
                    link_type="explicit", confidence=float(item["confidence"]),
                )
                count += 1
            continue
        candidates = deterministic_candidates(events_db, row)
        if len(candidates) == 1:
            cal_store.link_event(con, row["source_id"], candidates[0], link_type="deterministic_candidate", confidence=1.0)
            count += 1
    return count


def authoritative_schedule(con, event_layer_event_id: str):
    """Return Calendar current schedule when explicitly/deterministically linked.

    Cancelled Calendar entries remain authoritative evidence of cancellation;
    callers must not turn them into active events.
    """
    return con.execute(
        """SELECT e.*,l.link_type,l.confidence FROM event_source_links l
           JOIN events e ON e.source_id=l.calendar_source_id
           WHERE l.event_layer_event_id=? ORDER BY
             CASE l.link_type WHEN 'explicit' THEN 0 ELSE 1 END LIMIT 1""",
        (event_layer_event_id,),
    ).fetchone()
