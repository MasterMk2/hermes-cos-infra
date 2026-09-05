#!/usr/bin/env python3
"""Normalisation for Google Calendar Event resources.

All source text is retained as bounded data.  In particular, `description`
is deliberately not parsed for instructions and is never emitted in logs.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u200b-\u200f\u2028-\u202e\u2066-\u2069]")


def clean_text(value, limit: int) -> str:
    text = _CONTROL.sub("", str(value or ""))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def canonical_time(value) -> str | None:
    if not value:
        return None
    raw = str(value)
    # RFC3339 Z is equivalent to +00:00 but datetime.fromisoformat only
    # gained Z support recently; normalising it also makes fingerprints stable.
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return raw
    if dt.tzinfo is None:
        return raw
    return dt.astimezone(timezone.utc).isoformat()


def _when(part: dict | None) -> tuple[str | None, bool, str | None]:
    part = part or {}
    if part.get("date"):
        return str(part["date"]), True, str(part.get("timeZone") or "") or None
    return canonical_time(part.get("dateTime")), False, str(part.get("timeZone") or "") or None


def _person(value: dict | None) -> tuple[str | None, int | None]:
    value = value or {}
    display = clean_text(value.get("displayName"), 160) or None
    return display, (1 if value.get("self") is True else 0 if "self" in value else None)


def recurrence_state(event: dict) -> str:
    if event.get("recurringEventId"):
        return "cancelled_instance" if event.get("status") == "cancelled" else "instance"
    if event.get("recurrence"):
        return "master"
    return "one_off"


def meaningful_fingerprint(row: dict) -> str:
    payload = {
        "calendar_id": row["calendar_id"],
        "calendar_name": row["calendar_name"],
        "status": row["status"],
        "summary": row["summary"].casefold(),
        "location": row["location"].casefold(),
        "start_at": row["start_at"],
        "end_at": row["end_at"],
        "all_day": row["all_day"],
        "recurrence_state": row["recurrence_state"],
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_event(event: dict, *, calendar_id: str, calendar_name: str,
                    calendar_timezone: str | None, ingested_at: str) -> dict:
    event_id = str(event.get("id") or "")
    if not event_id:
        raise ValueError("Calendar event has no id")
    start_at, start_all_day, start_tz = _when(event.get("start"))
    end_at, end_all_day, end_tz = _when(event.get("end"))
    original_start_at, _original_all_day, _original_tz = _when(event.get("originalStartTime"))
    organizer_name, organizer_self = _person(event.get("organizer"))
    creator_name, creator_self = _person(event.get("creator"))
    attendees = event.get("attendees") if isinstance(event.get("attendees"), list) else []
    self_response = None
    for attendee in attendees:
        if isinstance(attendee, dict) and attendee.get("self"):
            self_response = clean_text(attendee.get("responseStatus"), 64) or None
            break
    row = {
        "source_id": f"gcal:{calendar_id}:{event_id}",
        "calendar_id": calendar_id,
        "calendar_name": clean_text(calendar_name, 300),
        "event_id": event_id,
        "ical_uid": clean_text(event.get("iCalUID"), 512) or None,
        "status": clean_text(event.get("status") or "confirmed", 64).lower(),
        "event_type": clean_text(event.get("eventType") or "default", 64),
        "summary": clean_text(event.get("summary"), 500),
        "description": clean_text(event.get("description"), 8192),
        "location": clean_text(event.get("location"), 1024),
        "start_at": start_at,
        "end_at": end_at,
        "all_day": int(start_all_day or end_all_day),
        "timezone": start_tz or end_tz or calendar_timezone or None,
        "created_at": canonical_time(event.get("created")),
        "updated_at": canonical_time(event.get("updated")),
        "recurring_event_id": clean_text(event.get("recurringEventId"), 512) or None,
        "original_start_at": original_start_at,
        "recurrence_state": recurrence_state(event),
        "organizer_display_name": organizer_name,
        "organizer_is_self": organizer_self,
        "creator_display_name": creator_name,
        "creator_is_self": creator_self,
        "attendee_count": len(attendees),
        "self_response_status": self_response,
        "visibility": clean_text(event.get("visibility") or "default", 64),
        "transparency": clean_text(event.get("transparency") or "opaque", 64),
        "html_link": clean_text(event.get("htmlLink"), 2048) or None,
        "ingested_at": ingested_at,
        "source_etag": clean_text(event.get("etag"), 512) or None,
        "deleted_at": ingested_at if event.get("status") == "cancelled" else None,
    }
    row["meaningful_hash"] = meaningful_fingerprint(row)
    return row
