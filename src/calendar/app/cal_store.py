#!/usr/bin/env python3
"""Durable operations for the Calendar authority database."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone


UTC = timezone.utc
EVENT_FIELD_NAMES = (
    "source_id", "calendar_id", "calendar_name", "event_id", "ical_uid", "status",
    "event_type", "summary", "description", "location", "start_at", "end_at",
    "all_day", "timezone", "created_at", "updated_at", "recurring_event_id",
    "original_start_at", "recurrence_state", "organizer_display_name",
    "organizer_is_self", "creator_display_name", "creator_is_self", "attendee_count",
    "self_response_status", "visibility", "transparency", "html_link", "ingested_at",
    "source_etag", "meaningful_hash", "deleted_at",
)
INSTANCE_FIELD_NAMES = (
    "source_id", "calendar_id", "calendar_name", "event_id", "status", "summary",
    "description", "location", "start_at", "end_at", "all_day", "timezone",
    "recurring_event_id", "original_start_at", "recurrence_state", "visibility",
    "transparency", "html_link", "source_etag", "meaningful_hash", "updated_at",
    "ingested_at", "deleted_at",
)


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _insert(con, table: str, fields: tuple[str, ...], row: dict, *, replace: bool = False):
    names = ", ".join(fields)
    slots = ", ".join("?" for _ in fields)
    mode = "INSERT OR REPLACE" if replace else "INSERT"
    con.execute(f"{mode} INTO {table} ({names}) VALUES ({slots})", [row.get(k) for k in fields])


def upsert_calendar(con, item: dict, *, selected: bool, at: str | None = None) -> None:
    at = at or now_iso()
    calendar_id = str(item.get("id") or "")
    if not calendar_id:
        raise ValueError("Calendar list item has no id")
    con.execute(
        """INSERT INTO calendars(calendar_id, calendar_name, primary_calendar, access_role,
           selected, timezone, discovered_at, updated_at) VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(calendar_id) DO UPDATE SET calendar_name=excluded.calendar_name,
             primary_calendar=excluded.primary_calendar, access_role=excluded.access_role,
             selected=excluded.selected, timezone=excluded.timezone, updated_at=excluded.updated_at""",
        (calendar_id, str(item.get("summaryOverride") or item.get("summary") or ""),
         int(bool(item.get("primary"))), item.get("accessRole"), int(bool(selected)),
         item.get("timeZone"), at, at),
    )
    con.execute(
        "INSERT OR IGNORE INTO sync_state(calendar_id) VALUES(?)", (calendar_id,)
    )


def sync_state(con, calendar_id: str):
    return con.execute("SELECT * FROM sync_state WHERE calendar_id=?", (calendar_id,)).fetchone()


def calendar_row(con, calendar_id: str):
    return con.execute("SELECT * FROM calendars WHERE calendar_id=?", (calendar_id,)).fetchone()


def selected_calendars(con):
    return con.execute("SELECT * FROM calendars WHERE selected=1 ORDER BY calendar_name, calendar_id").fetchall()


def _is_older(existing, incoming: dict) -> bool:
    old = existing["updated_at"]
    new = incoming.get("updated_at")
    return bool(old and new and str(new) < str(old))


def _classify(existing, row: dict) -> str:
    if existing is None:
        return "cancelled" if row["status"] == "cancelled" else "created"
    if _is_older(existing, row):
        return "stale"
    if existing["meaningful_hash"] == row["meaningful_hash"]:
        return "cosmetic"
    if row["status"] == "cancelled" and existing["status"] != "cancelled":
        return "cancelled"
    return "updated"


def _write_current(con, row: dict) -> None:
    _insert(con, "events", EVENT_FIELD_NAMES, row, replace=True)


def apply_event(con, row: dict, *, baseline: bool = False) -> str:
    """Apply one canonical event and append only meaningful change evidence."""
    existing = con.execute("SELECT * FROM events WHERE source_id=?", (row["source_id"],)).fetchone()
    change = _classify(existing, row)
    if change == "stale":
        return change
    _write_current(con, row)
    if not baseline and change in ("created", "updated", "cancelled"):
        con.execute(
            "INSERT INTO change_log(source_id, calendar_id, change_type, occurred_at, previous_hash, current_hash, baseline) "
            "VALUES(?,?,?,?,?,?,0)",
            (row["source_id"], row["calendar_id"], change, row["ingested_at"],
             existing["meaningful_hash"] if existing else None, row["meaningful_hash"]),
        )
    return change


def stage_event(con, generation: str, row: dict) -> None:
    fields = ("generation",) + EVENT_FIELD_NAMES
    values = {"generation": generation, **row}
    _insert(con, "resync_staging", fields, values, replace=True)


def discard_staging(con, generation: str) -> None:
    con.execute("DELETE FROM resync_staging WHERE generation=?", (generation,))


def publish_staging(con, calendar_id: str, generation: str, *, sync_token: str,
                    finished_at: str, duration_ms: int, reset_410: bool) -> int:
    """Atomically replace only one calendar after every full-sync page succeeded."""
    count = con.execute(
        "SELECT COUNT(*) FROM resync_staging WHERE generation=?", (generation,)
    ).fetchone()[0]
    state = sync_state(con, calendar_id)
    # Initial import is a baseline. A later 410 recovery compares the fully
    # staged snapshot before publishing it, so a move/cancellation observed
    # only by the resync still becomes an Attention-relevant change record.
    changes = {"created": 0, "updated": 0, "cancelled": 0}
    if state is not None and state["initialized_at"]:
        old = {row["source_id"]: row for row in con.execute(
            "SELECT source_id,status,meaningful_hash FROM events WHERE calendar_id=?", (calendar_id,)
        )}
        staged = con.execute(
            "SELECT source_id,status,meaningful_hash FROM resync_staging WHERE generation=?", (generation,)
        ).fetchall()
        for row in staged:
            previous = old.get(row["source_id"])
            if previous is None:
                kind = "cancelled" if row["status"] == "cancelled" else "created"
            elif previous["meaningful_hash"] == row["meaningful_hash"]:
                continue
            elif row["status"] == "cancelled" and previous["status"] != "cancelled":
                kind = "cancelled"
            else:
                kind = "updated"
            changes[kind] += 1
            con.execute(
                "INSERT INTO change_log(source_id,calendar_id,change_type,occurred_at,previous_hash,current_hash,baseline) VALUES(?,?,?,?,?,?,0)",
                (row["source_id"], calendar_id, kind, finished_at,
                 previous["meaningful_hash"] if previous else None, row["meaningful_hash"]),
            )
    con.execute("DELETE FROM events WHERE calendar_id=?", (calendar_id,))
    fields = ", ".join(EVENT_FIELD_NAMES)
    con.execute(
        f"INSERT INTO events ({fields}) SELECT {fields} FROM resync_staging WHERE generation=?",
        (generation,),
    )
    con.execute("DELETE FROM resync_staging WHERE generation=?", (generation,))
    con.execute(
        """UPDATE sync_state SET sync_token=?, initialized_at=COALESCE(initialized_at, ?),
           last_full_sync_at=?, last_api_success_at=?, last_error_code=NULL,
           last_error_at=NULL, consecutive_failures=0, events_fetched=?,
           events_changed=?, events_cancelled=?, sync_duration_ms=?,
           active_generation=?, reset_410_count=reset_410_count+? WHERE calendar_id=?""",
        (sync_token, finished_at, finished_at, finished_at, count,
         changes["created"] + changes["updated"], changes["cancelled"], duration_ms,
         generation, int(reset_410), calendar_id),
    )
    return int(count)


def finish_incremental(con, calendar_id: str, *, sync_token: str, at: str,
                       duration_ms: int, fetched: int, changes: dict) -> None:
    con.execute(
        """UPDATE sync_state SET sync_token=?, last_incremental_sync_at=?,
           last_api_success_at=?, last_error_code=NULL, last_error_at=NULL,
           consecutive_failures=0, events_fetched=?, events_changed=?,
           events_cancelled=?, sync_duration_ms=? WHERE calendar_id=?""",
        (sync_token, at, at, fetched, int(changes.get("created", 0)) + int(changes.get("updated", 0)),
         int(changes.get("cancelled", 0)), duration_ms, calendar_id),
    )


def record_sync_failure(con, calendar_id: str, code: str, at: str | None = None) -> None:
    at = at or now_iso()
    con.execute(
        "UPDATE sync_state SET last_error_code=?, last_error_at=?, consecutive_failures=consecutive_failures+1 WHERE calendar_id=?",
        (str(code)[:120], at, calendar_id),
    )


def set_auth_state(con, calendar_id: str, state: str) -> None:
    con.execute("UPDATE sync_state SET auth_state=? WHERE calendar_id=?", (state, calendar_id))


def enqueue_sync(con, calendar_id: str, *, reason: str, at: str | None = None) -> bool:
    at = at or now_iso()
    existing = con.execute("SELECT 1 FROM sync_queue WHERE calendar_id=?", (calendar_id,)).fetchone()
    con.execute(
        """INSERT INTO sync_queue(calendar_id, requested_at, reason) VALUES(?,?,?)
           ON CONFLICT(calendar_id) DO UPDATE SET pending_count=sync_queue.pending_count+1,
             reason=excluded.reason, requested_at=MIN(sync_queue.requested_at, excluded.requested_at)""",
        (calendar_id, at, reason),
    )
    return existing is None


def claim_queue(con, *, at: str | None = None):
    at = at or now_iso()
    row = con.execute(
        "SELECT * FROM sync_queue WHERE claimed_at IS NULL AND (not_before IS NULL OR not_before<=?) "
        "ORDER BY requested_at LIMIT 1", (at,),
    ).fetchone()
    if row is None:
        return None
    con.execute("UPDATE sync_queue SET claimed_at=? WHERE calendar_id=? AND claimed_at IS NULL", (at, row["calendar_id"]))
    return con.execute("SELECT * FROM sync_queue WHERE calendar_id=?", (row["calendar_id"],)).fetchone()


def recover_abandoned_claims(con) -> int:
    """Release work claimed by a stopped predecessor of the singleton service."""
    rows = con.execute("SELECT calendar_id FROM sync_queue WHERE claimed_at IS NOT NULL").fetchall()
    for row in rows:
        con.execute("UPDATE sync_queue SET claimed_at=NULL WHERE calendar_id=?", (row["calendar_id"],))
        con.execute("DELETE FROM sync_locks WHERE calendar_id=?", (row["calendar_id"],))
    return len(rows)


def complete_queue(con, calendar_id: str) -> None:
    con.execute("DELETE FROM sync_queue WHERE calendar_id=?", (calendar_id,))


def retry_queue(con, calendar_id: str, *, code: str, delay_seconds: int, at: str | None = None) -> None:
    at = at or now_iso()
    not_before = (datetime.fromisoformat(at) + timedelta(seconds=delay_seconds)).isoformat()
    con.execute(
        "UPDATE sync_queue SET attempts=attempts+1, last_error_code=?, not_before=?, claimed_at=NULL WHERE calendar_id=?",
        (str(code)[:120], not_before, calendar_id),
    )


def acquire_lock(con, calendar_id: str, owner_id: str, *, at: str | None = None,
                 stale_seconds: int = 1800) -> bool:
    at = at or now_iso()
    old = con.execute("SELECT * FROM sync_locks WHERE calendar_id=?", (calendar_id,)).fetchone()
    if old:
        try:
            stale = datetime.fromisoformat(str(old["locked_at"])) < datetime.fromisoformat(at) - timedelta(seconds=stale_seconds)
        except ValueError:
            stale = True
        if not stale and old["owner_id"] != owner_id:
            return False
    con.execute(
        "INSERT INTO sync_locks(calendar_id, owner_id, locked_at) VALUES(?,?,?) "
        "ON CONFLICT(calendar_id) DO UPDATE SET owner_id=excluded.owner_id, locked_at=excluded.locked_at",
        (calendar_id, owner_id, at),
    )
    return True


def release_lock(con, calendar_id: str, owner_id: str) -> None:
    con.execute("DELETE FROM sync_locks WHERE calendar_id=? AND owner_id=?", (calendar_id, owner_id))


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_watch_token() -> str:
    return secrets.token_urlsafe(32)


def register_watch(con, *, calendar_id: str, channel_id: str, resource_id: str,
                   channel_token: str, expiration_ms: int | None, at: str | None = None) -> None:
    at = at or now_iso()
    con.execute("UPDATE watch_channels SET state='SUPERSEDED' WHERE calendar_id=? AND state='ACTIVE'", (calendar_id,))
    con.execute(
        "INSERT INTO watch_channels(channel_id, calendar_id, resource_id, token_hash, expiration_ms, created_at) VALUES(?,?,?,?,?,?)",
        (channel_id, calendar_id, resource_id, token_hash(channel_token), expiration_ms, at),
    )


def verify_watch_headers(con, headers: dict, *, at: str | None = None):
    """Return a verified calendar id, else None.  No guessed channel queues work."""
    def header(name):
        value = headers.get(name) or headers.get(name.lower())
        if value:
            return str(value)
        # Go/Caddy and other HTTP stacks canonicalize names as e.g.
        # X-Goog-Channel-Id, while Google documents X-Goog-Channel-ID.
        # Header names are case-insensitive; do not reject valid pushes
        # because of presentation casing.
        target = name.casefold()
        for key, candidate in headers.items():
            if str(key).casefold() == target:
                return str(candidate or "")
        return ""
    channel_id = header("X-Goog-Channel-ID")
    resource_id = header("X-Goog-Resource-ID")
    token = header("X-Goog-Channel-Token")
    state = header("X-Goog-Resource-State")
    message = header("X-Goog-Message-Number")
    if not channel_id or not resource_id or not token or state not in {"sync", "exists", "not_exists"}:
        return None
    try:
        message_n = int(message)
    except ValueError:
        return None
    row = con.execute(
        "SELECT * FROM watch_channels WHERE channel_id=? AND resource_id=? AND state='ACTIVE'",
        (channel_id, resource_id),
    ).fetchone()
    if row is None or not secrets.compare_digest(row["token_hash"], token_hash(token)):
        return None
    con.execute(
        "UPDATE watch_channels SET last_notification_at=?, last_message_number=? WHERE channel_id=?",
        (at or now_iso(), message_n, channel_id),
    )
    return str(row["calendar_id"])


def watch_due_for_renewal(con, *, now_ms: int, margin_ms: int):
    return con.execute(
        "SELECT * FROM watch_channels WHERE state='ACTIVE' AND (expiration_ms IS NULL OR expiration_ms<=?) ORDER BY calendar_id",
        (now_ms + margin_ms,),
    ).fetchall()


def link_event(con, calendar_source_id: str, event_layer_event_id: str, *, link_type: str = "explicit",
               confidence: float = 1.0, at: str | None = None) -> None:
    if link_type not in {"explicit", "deterministic_candidate"}:
        raise ValueError("unsupported link type")
    at = at or now_iso()
    con.execute(
        """INSERT INTO event_source_links(calendar_source_id, event_layer_event_id, link_type, confidence, created_at, updated_at)
           VALUES(?,?,?,?,?,?) ON CONFLICT(calendar_source_id,event_layer_event_id) DO UPDATE SET
           link_type=excluded.link_type, confidence=excluded.confidence, updated_at=excluded.updated_at""",
        (calendar_source_id, event_layer_event_id, link_type, float(confidence), at, at),
    )


def increment_metric(con, key: str, *, amount: int = 1, at: str | None = None) -> None:
    con.execute(
        "INSERT INTO metrics(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=value+excluded.value,updated_at=excluded.updated_at",
        (key, int(amount), at or now_iso()),
    )
