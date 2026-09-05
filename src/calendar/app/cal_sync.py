#!/usr/bin/env python3
"""Full/incremental Calendar synchronisation with safe 410 recovery."""

from __future__ import annotations

import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone

import cal_normalize
import cal_store


class CalendarApiError(RuntimeError):
    def __init__(self, status: int, code: str = "api_error"):
        super().__init__(f"Calendar API {status}: {code}")
        self.status = int(status)
        self.code = str(code)


def _calendar_details(con, calendar_id: str):
    row = cal_store.calendar_row(con, calendar_id)
    if row is None:
        raise ValueError(f"calendar {calendar_id!r} is not discovered/allowlisted")
    if not row["selected"]:
        raise ValueError(f"calendar {calendar_id!r} is not selected")
    return row


def _pages(client, calendar_id: str, base_params: dict):
    """Yield every page and the final nextSyncToken; never trust a partial token."""
    params = dict(base_params)
    final_token = None
    while True:
        page = client.list_events(calendar_id, **params)
        if not isinstance(page, dict):
            raise CalendarApiError(502, "invalid_page")
        yield page
        next_page = page.get("nextPageToken")
        if next_page:
            params["pageToken"] = next_page
            continue
        final_token = page.get("nextSyncToken")
        if not final_token:
            raise CalendarApiError(502, "missing_next_sync_token")
        return final_token


def full_sync(con, client, calendar_id: str, *, reset_410: bool = False,
              now=None) -> dict:
    """Build a full snapshot off to the side, then publish atomically."""
    started = time.monotonic()
    now = now or cal_store.now_iso()
    calendar = _calendar_details(con, calendar_id)
    generation = uuid.uuid4().hex
    fetched = 0
    try:
        iterator = _pages(client, calendar_id, {
            "showDeleted": True,
            "singleEvents": False,
            "maxResults": 250,
        })
        while True:
            try:
                page = next(iterator)
            except StopIteration as stop:
                token = stop.value
                break
            for event in page.get("items") or []:
                row = cal_normalize.normalize_event(
                    event, calendar_id=calendar_id, calendar_name=calendar["calendar_name"],
                    calendar_timezone=calendar["timezone"], ingested_at=now,
                )
                cal_store.stage_event(con, generation, row)
                fetched += 1
            con.commit()
    except Exception:
        cal_store.discard_staging(con, generation)
        con.commit()
        raise
    duration_ms = int((time.monotonic() - started) * 1000)
    with con:
        cal_store.publish_staging(
            con, calendar_id, generation, sync_token=str(token), finished_at=now,
            duration_ms=duration_ms, reset_410=reset_410,
        )
    return {"mode": "full", "fetched": fetched, "duration_ms": duration_ms, "reset_410": reset_410}


def incremental_sync(con, client, calendar_id: str, *, now=None) -> dict:
    """Apply all deltas, committing the new cursor only after the final page."""
    state = cal_store.sync_state(con, calendar_id)
    if state is None or not state["sync_token"]:
        return full_sync(con, client, calendar_id, now=now)
    started = time.monotonic()
    now = now or cal_store.now_iso()
    calendar = _calendar_details(con, calendar_id)
    changes = Counter()
    fetched = 0
    iterator = _pages(client, calendar_id, {
        "syncToken": state["sync_token"],
        "showDeleted": True,
        "singleEvents": False,
        "maxResults": 250,
    })
    while True:
        try:
            page = next(iterator)
        except StopIteration as stop:
            token = stop.value
            break
        for event in page.get("items") or []:
            row = cal_normalize.normalize_event(
                event, calendar_id=calendar_id, calendar_name=calendar["calendar_name"],
                calendar_timezone=calendar["timezone"], ingested_at=now,
            )
            changes[cal_store.apply_event(con, row, baseline=False)] += 1
            fetched += 1
        con.commit()
    duration_ms = int((time.monotonic() - started) * 1000)
    with con:
        cal_store.finish_incremental(
            con, calendar_id, sync_token=str(token), at=now, duration_ms=duration_ms,
            fetched=fetched, changes=changes,
        )
    return {"mode": "incremental", "fetched": fetched, "changes": dict(changes), "duration_ms": duration_ms}


def sync_calendar(con, client, calendar_id: str, *, now=None) -> dict:
    try:
        result = incremental_sync(con, client, calendar_id, now=now)
    except CalendarApiError as exc:
        if exc.status != 410:
            raise
        result = full_sync(con, client, calendar_id, reset_410=True, now=now)
    cal_store.set_auth_state(con, calendar_id, "AUTHORIZED")
    con.commit()
    return result


def refresh_projection(con, client, calendar_id: str, *, past_days: int = 30,
                       future_days: int = 365, now_dt=None, at=None) -> int:
    """Refresh a bounded expanded search view without contaminating syncToken state."""
    at = at or cal_store.now_iso()
    now_dt = now_dt or datetime.now(timezone.utc)
    calendar = _calendar_details(con, calendar_id)
    params = {
        "showDeleted": True,
        "singleEvents": True,
        "maxResults": 250,
        "timeMin": (now_dt - timedelta(days=int(past_days))).isoformat(),
        "timeMax": (now_dt + timedelta(days=int(future_days))).isoformat(),
    }
    rows = []
    page_token = None
    while True:
        request = dict(params)
        if page_token:
            request["pageToken"] = page_token
        page = client.list_events(calendar_id, **request)
        if not isinstance(page, dict):
            raise CalendarApiError(502, "invalid_projection_page")
        for event in page.get("items") or []:
            rows.append(cal_normalize.normalize_event(
                event, calendar_id=calendar_id, calendar_name=calendar["calendar_name"],
                calendar_timezone=calendar["timezone"], ingested_at=at,
            ))
        page_token = page.get("nextPageToken")
        if not page_token:
            break
    with con:
        con.execute("DELETE FROM event_instances WHERE calendar_id=?", (calendar_id,))
        for row in rows:
            cal_store._insert(con, "event_instances", cal_store.INSTANCE_FIELD_NAMES, row, replace=True)
        con.execute("UPDATE sync_state SET last_projection_at=? WHERE calendar_id=?", (at, calendar_id))
    return len(rows)
