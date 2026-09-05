#!/usr/bin/env python3
"""Google Calendar authority store schema.

OAuth credentials never enter this database.  The only token held here is a
Calendar *syncToken*, which is a cursor, not an OAuth bearer or refresh token.
The current Calendar state is normalised; arbitrary raw API payloads are not
retained.
"""

from __future__ import annotations

import sqlite3


SCHEMA_VERSION = 1

EVENT_COLUMNS = """
  source_id TEXT PRIMARY KEY,
  calendar_id TEXT NOT NULL,
  calendar_name TEXT NOT NULL,
  event_id TEXT NOT NULL,
  ical_uid TEXT,
  status TEXT NOT NULL,
  event_type TEXT NOT NULL,
  summary TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  location TEXT NOT NULL DEFAULT '',
  start_at TEXT,
  end_at TEXT,
  all_day INTEGER NOT NULL DEFAULT 0,
  timezone TEXT,
  created_at TEXT,
  updated_at TEXT,
  recurring_event_id TEXT,
  original_start_at TEXT,
  recurrence_state TEXT NOT NULL DEFAULT 'one_off',
  organizer_display_name TEXT,
  organizer_is_self INTEGER,
  creator_display_name TEXT,
  creator_is_self INTEGER,
  attendee_count INTEGER NOT NULL DEFAULT 0,
  self_response_status TEXT,
  visibility TEXT,
  transparency TEXT,
  html_link TEXT,
  ingested_at TEXT NOT NULL,
  source_etag TEXT,
  meaningful_hash TEXT NOT NULL,
  deleted_at TEXT
"""

SCHEMA = f"""
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS calendars (
  calendar_id TEXT PRIMARY KEY,
  calendar_name TEXT NOT NULL,
  primary_calendar INTEGER NOT NULL DEFAULT 0,
  access_role TEXT,
  selected INTEGER NOT NULL DEFAULT 0,
  timezone TEXT,
  discovered_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events ({EVENT_COLUMNS});
CREATE INDEX IF NOT EXISTS idx_events_calendar_start
  ON events(calendar_id, start_at);
CREATE INDEX IF NOT EXISTS idx_events_status_start
  ON events(status, start_at);
CREATE INDEX IF NOT EXISTS idx_events_ical_uid ON events(ical_uid);

-- A full resync is first assembled here.  `events` is not touched until all
-- pages and the final nextSyncToken have arrived, then one SQLite transaction
-- publishes the replacement.  A failed 410 recovery therefore preserves the
-- previous usable snapshot.
CREATE TABLE IF NOT EXISTS resync_staging (
  generation TEXT NOT NULL,
  {EVENT_COLUMNS.replace('source_id TEXT PRIMARY KEY', 'source_id TEXT NOT NULL')},
  PRIMARY KEY(generation, source_id)
);

-- Expanded instances are bounded separately from the sync-token canonical
-- state.  A recurring rule is never expanded forever.
CREATE TABLE IF NOT EXISTS event_instances (
  source_id TEXT PRIMARY KEY,
  calendar_id TEXT NOT NULL,
  calendar_name TEXT NOT NULL,
  event_id TEXT NOT NULL,
  status TEXT NOT NULL,
  summary TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  location TEXT NOT NULL DEFAULT '',
  start_at TEXT,
  end_at TEXT,
  all_day INTEGER NOT NULL DEFAULT 0,
  timezone TEXT,
  recurring_event_id TEXT,
  original_start_at TEXT,
  recurrence_state TEXT NOT NULL,
  visibility TEXT,
  transparency TEXT,
  html_link TEXT,
  source_etag TEXT,
  meaningful_hash TEXT NOT NULL,
  updated_at TEXT,
  ingested_at TEXT NOT NULL,
  deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_instances_calendar_start
  ON event_instances(calendar_id, start_at);
CREATE INDEX IF NOT EXISTS idx_instances_status_start
  ON event_instances(status, start_at);

CREATE TABLE IF NOT EXISTS sync_state (
  calendar_id TEXT PRIMARY KEY REFERENCES calendars(calendar_id) ON DELETE CASCADE,
  sync_token TEXT,
  auth_state TEXT NOT NULL DEFAULT 'WAITING_HUMAN',
  initialized_at TEXT,
  last_full_sync_at TEXT,
  last_incremental_sync_at TEXT,
  last_api_success_at TEXT,
  last_projection_at TEXT,
  last_error_code TEXT,
  last_error_at TEXT,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  reset_410_count INTEGER NOT NULL DEFAULT 0,
  events_fetched INTEGER NOT NULL DEFAULT 0,
  events_changed INTEGER NOT NULL DEFAULT 0,
  events_cancelled INTEGER NOT NULL DEFAULT 0,
  sync_duration_ms INTEGER,
  active_generation TEXT
);

CREATE TABLE IF NOT EXISTS watch_channels (
  channel_id TEXT PRIMARY KEY,
  calendar_id TEXT NOT NULL REFERENCES calendars(calendar_id) ON DELETE CASCADE,
  resource_id TEXT NOT NULL,
  token_hash TEXT NOT NULL,
  expiration_ms INTEGER,
  created_at TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'ACTIVE',
  last_notification_at TEXT,
  last_message_number INTEGER,
  last_successful_sync_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_watch_calendar ON watch_channels(calendar_id, state);
CREATE INDEX IF NOT EXISTS idx_watch_resource ON watch_channels(resource_id, state);

CREATE TABLE IF NOT EXISTS sync_queue (
  calendar_id TEXT PRIMARY KEY REFERENCES calendars(calendar_id) ON DELETE CASCADE,
  requested_at TEXT NOT NULL,
  reason TEXT NOT NULL,
  pending_count INTEGER NOT NULL DEFAULT 1,
  attempts INTEGER NOT NULL DEFAULT 0,
  not_before TEXT,
  last_error_code TEXT,
  claimed_at TEXT
);

CREATE TABLE IF NOT EXISTS sync_locks (
  calendar_id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  locked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS change_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id TEXT NOT NULL,
  calendar_id TEXT NOT NULL,
  change_type TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  previous_hash TEXT,
  current_hash TEXT NOT NULL,
  baseline INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_change_log_source ON change_log(source_id, id);

-- Links are owned here so Calendar authority never mutates the pre-existing
-- Event Layer schema.  `explicit` is an operator/user-confirmed mapping;
-- automatic matching is allowed only for a unique deterministic candidate.
CREATE TABLE IF NOT EXISTS event_source_links (
  calendar_source_id TEXT NOT NULL REFERENCES events(source_id) ON DELETE CASCADE,
  event_layer_event_id TEXT NOT NULL,
  link_type TEXT NOT NULL,
  confidence REAL NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(calendar_source_id, event_layer_event_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_links_event_explicit
  ON event_source_links(event_layer_event_id, link_type)
  WHERE link_type = 'explicit';

CREATE TABLE IF NOT EXISTS metrics (
  key TEXT PRIMARY KEY,
  value INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
"""


def connect(path, *, readonly: bool = False, timeout: float = 30.0) -> sqlite3.Connection:
    if readonly:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=timeout)
    else:
        con = sqlite3.connect(str(path), timeout=timeout)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 30000")
    if not readonly:
        con.execute("PRAGMA synchronous = NORMAL")
        con.execute("PRAGMA foreign_keys = ON")
    return con


def open_db(path) -> sqlite3.Connection:
    con = connect(path)
    # Journal mode is a database-wide setting which may require an exclusive
    # lock.  Initialisation calls this once; steady-state workers use
    # `connect()` so a status reader cannot make every queue tick contend for
    # a WAL mode transition.
    con.execute("PRAGMA journal_mode = WAL")
    con.executescript(SCHEMA)
    # `open_db()` is also used by the five-second queue worker.  Updating this
    # value on every open would create a needless WAL write and brief writer
    # lock even when no Calendar data changed.  Schema migrations use explicit
    # migration code; this bootstrap marker only needs to be inserted once.
    con.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
    con.commit()
    return con


def integrity_ok(con: sqlite3.Connection) -> bool:
    row = con.execute("PRAGMA integrity_check").fetchone()
    return bool(row) and str(row[0]).lower() == "ok"
