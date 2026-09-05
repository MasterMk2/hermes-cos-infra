#!/usr/bin/env python3
"""Publish a read-only Calendar search snapshot.

The index is a SQLite replacement file, never a bind-mounted live WAL
database.  Hermes and Attention can open it `mode=ro` without being given
write access to the Calendar authority database or any OAuth credential.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path


INDEX_SCHEMA = """
PRAGMA journal_mode=DELETE;
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE calendar_events (
  source_id TEXT PRIMARY KEY, calendar_id TEXT NOT NULL, calendar_name TEXT NOT NULL,
  event_id TEXT NOT NULL, status TEXT NOT NULL, summary TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '', location TEXT NOT NULL DEFAULT '',
  start_at TEXT, end_at TEXT, all_day INTEGER NOT NULL, timezone TEXT,
  recurring_event_id TEXT, original_start_at TEXT, recurrence_state TEXT NOT NULL,
  visibility TEXT, transparency TEXT, html_link TEXT, updated_at TEXT,
  meaningful_hash TEXT NOT NULL, deleted_at TEXT
);
CREATE INDEX idx_calendar_events_range ON calendar_events(status, start_at);
CREATE INDEX idx_calendar_events_calendar ON calendar_events(calendar_id, start_at);
CREATE TABLE event_links (
  calendar_source_id TEXT NOT NULL, event_layer_event_id TEXT NOT NULL,
  link_type TEXT NOT NULL, confidence REAL NOT NULL,
  PRIMARY KEY(calendar_source_id, event_layer_event_id)
);
CREATE INDEX idx_event_links_event ON event_links(event_layer_event_id);
CREATE TABLE calendar_changes (
  id INTEGER PRIMARY KEY, source_id TEXT NOT NULL, calendar_id TEXT NOT NULL,
  change_type TEXT NOT NULL, occurred_at TEXT NOT NULL, current_hash TEXT NOT NULL,
  baseline INTEGER NOT NULL
);
CREATE INDEX idx_calendar_changes_id ON calendar_changes(id);
CREATE TABLE calendar_status (
  calendar_id TEXT PRIMARY KEY, calendar_name TEXT NOT NULL, primary_calendar INTEGER NOT NULL,
  selected INTEGER NOT NULL, auth_state TEXT NOT NULL, initialized_at TEXT,
  last_full_sync_at TEXT, last_incremental_sync_at TEXT, last_api_success_at TEXT,
  last_projection_at TEXT, last_error_code TEXT, last_error_at TEXT,
  consecutive_failures INTEGER NOT NULL, reset_410_count INTEGER NOT NULL,
  events_fetched INTEGER NOT NULL, events_changed INTEGER NOT NULL,
  events_cancelled INTEGER NOT NULL, sync_duration_ms INTEGER,
  watch_state TEXT, watch_expiration_ms INTEGER, last_notification_at TEXT,
  event_count INTEGER NOT NULL, oldest_start TEXT, newest_start TEXT, pending_sync_age_seconds INTEGER
);
"""


def _fsync_path(path: Path) -> None:
    # Windows rejects fsync on a read-only handle.  The temporary snapshot is
    # still 0600 at this point, so open it read/write before chmod/replace.
    fd = os.open(str(path), os.O_RDWR)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish_index(authority_con, destination: str | Path) -> None:
    """Atomically replace `destination` with a no-WAL, read-only snapshot."""
    dest = Path(destination)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".index-", suffix=".db", dir=str(dest.parent))
    os.close(fd)
    temp = Path(temp_name)
    try:
        out = sqlite3.connect(str(temp))
        out.row_factory = sqlite3.Row
        try:
            out.executescript(INDEX_SCHEMA)
            for row in authority_con.execute("SELECT * FROM event_instances"):
                out.execute(
                    "INSERT INTO calendar_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (row["source_id"], row["calendar_id"], row["calendar_name"], row["event_id"],
                     row["status"], row["summary"], row["description"], row["location"],
                     row["start_at"], row["end_at"], row["all_day"], row["timezone"],
                     row["recurring_event_id"], row["original_start_at"], row["recurrence_state"],
                     row["visibility"], row["transparency"], row["html_link"], row["updated_at"],
                     row["meaningful_hash"], row["deleted_at"]),
                )
            for row in authority_con.execute("SELECT calendar_source_id,event_layer_event_id,link_type,confidence FROM event_source_links"):
                out.execute("INSERT INTO event_links VALUES(?,?,?,?)", tuple(row))
            for row in authority_con.execute(
                "SELECT id,source_id,calendar_id,change_type,occurred_at,current_hash,baseline FROM change_log ORDER BY id DESC LIMIT 2000"
            ):
                out.execute("INSERT INTO calendar_changes VALUES(?,?,?,?,?,?,?)", tuple(row))
            status_rows = authority_con.execute(
                """SELECT c.calendar_id,c.calendar_name,c.primary_calendar,c.selected,s.auth_state,
                   s.initialized_at,s.last_full_sync_at,s.last_incremental_sync_at,s.last_api_success_at,
                   s.last_projection_at,s.last_error_code,s.last_error_at,s.consecutive_failures,
                   s.reset_410_count,s.events_fetched,s.events_changed,s.events_cancelled,s.sync_duration_ms,
                   w.state AS watch_state,w.expiration_ms,w.last_notification_at,
                   (SELECT COUNT(*) FROM event_instances i WHERE i.calendar_id=c.calendar_id) AS event_count,
                   (SELECT MIN(start_at) FROM event_instances i WHERE i.calendar_id=c.calendar_id AND i.status!='cancelled') AS oldest_start,
                   (SELECT MAX(start_at) FROM event_instances i WHERE i.calendar_id=c.calendar_id AND i.status!='cancelled') AS newest_start,
                   (SELECT CAST(strftime('%s','now')-strftime('%s',requested_at) AS INTEGER) FROM sync_queue q WHERE q.calendar_id=c.calendar_id) AS pending_sync_age_seconds
                   FROM calendars c JOIN sync_state s ON s.calendar_id=c.calendar_id
                   LEFT JOIN watch_channels w ON w.calendar_id=c.calendar_id AND w.state='ACTIVE'
                   WHERE c.selected=1"""
            ).fetchall()
            for row in status_rows:
                out.execute("INSERT INTO calendar_status VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", tuple(row))
            out.execute("INSERT INTO meta VALUES('index_format','1')")
            out.execute("INSERT INTO meta VALUES('published_at',datetime('now'))")
            out.commit()
        finally:
            out.close()
        _fsync_path(temp)
        os.chmod(temp, 0o444)
        # NTFS does not allow replacing a read-only destination.  The old
        # snapshot is made service-writable only for this atomic replacement;
        # the newly published file is immediately 0444 again before readers
        # can observe it.
        if dest.exists():
            os.chmod(dest, 0o644)
        os.replace(temp, dest)
        # fsync the directory so a power cut does not resurrect a half publish.
        if hasattr(os, "O_DIRECTORY"):
            dir_fd = os.open(str(dest.parent), os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass
