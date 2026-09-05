#!/usr/bin/env python3
"""Canonical Gmail evidence-store schema.

OAuth credentials, OAuth access tokens, and raw API responses do not belong
in this database.  The normalised message record is the authority for local
retrieval; a bounded, read-only projection is separately published for Hermes
and the event-ingestion bridge.
"""

from __future__ import annotations

import sqlite3


SCHEMA_VERSION = 2

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  source_id TEXT PRIMARY KEY,             -- gmail:<message_id>
  message_id TEXT NOT NULL UNIQUE,
  thread_id TEXT NOT NULL,
  history_id TEXT,
  internal_date_ms INTEGER,
  received_at TEXT,
  from_header TEXT NOT NULL DEFAULT '',
  to_header TEXT NOT NULL DEFAULT '',
  cc_header TEXT NOT NULL DEFAULT '',
  subject TEXT NOT NULL DEFAULT '',
  labels_json TEXT NOT NULL DEFAULT '[]',
  snippet TEXT NOT NULL DEFAULT '',
  body_text TEXT NOT NULL DEFAULT '',
  commitment_text TEXT NOT NULL DEFAULT '',
  content_state TEXT NOT NULL DEFAULT 'available',
  mime_json TEXT NOT NULL DEFAULT '{}',
  attachments_json TEXT NOT NULL DEFAULT '[]',
  is_sent INTEGER NOT NULL DEFAULT 0,
  deleted_at TEXT,
  ingested_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 0,
  baseline INTEGER NOT NULL DEFAULT 0,
  content_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_thread_date
  ON messages(thread_id, internal_date_ms);
CREATE INDEX IF NOT EXISTS idx_messages_live_date
  ON messages(deleted_at, internal_date_ms);
CREATE INDEX IF NOT EXISTS idx_messages_history ON messages(history_id);

-- A bounded recovery is assembled here.  It is published only after every
-- list/get page succeeds, so an expired history cursor never means "the
-- mailbox became empty".
CREATE TABLE IF NOT EXISTS resync_staging (
  generation TEXT NOT NULL,
  source_id TEXT NOT NULL,
  message_id TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  history_id TEXT,
  internal_date_ms INTEGER,
  received_at TEXT,
  from_header TEXT NOT NULL DEFAULT '',
  to_header TEXT NOT NULL DEFAULT '',
  cc_header TEXT NOT NULL DEFAULT '',
  subject TEXT NOT NULL DEFAULT '',
  labels_json TEXT NOT NULL DEFAULT '[]',
  snippet TEXT NOT NULL DEFAULT '',
  body_text TEXT NOT NULL DEFAULT '',
  commitment_text TEXT NOT NULL DEFAULT '',
  content_state TEXT NOT NULL DEFAULT 'available',
  mime_json TEXT NOT NULL DEFAULT '{}',
  attachments_json TEXT NOT NULL DEFAULT '[]',
  is_sent INTEGER NOT NULL DEFAULT 0,
  deleted_at TEXT,
  ingested_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 0,
  baseline INTEGER NOT NULL DEFAULT 0,
  content_hash TEXT NOT NULL,
  PRIMARY KEY(generation, source_id)
);

CREATE TABLE IF NOT EXISTS threads (
  thread_id TEXT PRIMARY KEY,
  latest_message_id TEXT,
  latest_internal_date_ms INTEGER,
  message_count INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);

-- There is one authorised mailbox per service instance.  Its history cursor
-- is a Gmail cursor, not an OAuth token.
CREATE TABLE IF NOT EXISTS sync_state (
  mailbox_key TEXT PRIMARY KEY,
  mailbox_email TEXT,
  history_id TEXT,
  auth_state TEXT NOT NULL DEFAULT 'WAITING_HUMAN',
  initialized_at TEXT,
  initial_range_start TEXT,
  last_full_sync_at TEXT,
  last_incremental_sync_at TEXT,
  last_api_success_at TEXT,
  last_message_ingested_at TEXT,
  last_error_code TEXT,
  last_error_at TEXT,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  history_reset_count INTEGER NOT NULL DEFAULT 0,
  messages_fetched INTEGER NOT NULL DEFAULT 0,
  messages_changed INTEGER NOT NULL DEFAULT 0,
  messages_deleted INTEGER NOT NULL DEFAULT 0,
  sync_duration_ms INTEGER,
  last_event_reconciliation_at TEXT
);

CREATE TABLE IF NOT EXISTS watch_state (
  mailbox_key TEXT PRIMARY KEY REFERENCES sync_state(mailbox_key) ON DELETE CASCADE,
  topic_name TEXT,
  expiration_ms INTEGER,
  state TEXT NOT NULL DEFAULT 'WAITING_HUMAN',
  created_at TEXT,
  renewed_at TEXT,
  last_notification_at TEXT,
  last_notification_history_id TEXT,
  last_successful_sync_at TEXT,
  last_error_code TEXT,
  last_error_at TEXT
);

-- Durable coalescing queue.  A Pub/Sub notification or polling result only
-- requests canonical history reconciliation; it never carries message truth.
CREATE TABLE IF NOT EXISTS sync_queue (
  mailbox_key TEXT PRIMARY KEY REFERENCES sync_state(mailbox_key) ON DELETE CASCADE,
  requested_at TEXT NOT NULL,
  reason TEXT NOT NULL,
  pending_count INTEGER NOT NULL DEFAULT 1,
  attempts INTEGER NOT NULL DEFAULT 0,
  not_before TEXT,
  claimed_at TEXT,
  last_error_code TEXT
);

CREATE TABLE IF NOT EXISTS sync_locks (
  mailbox_key TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  locked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notification_ledger (
  pubsub_message_id TEXT PRIMARY KEY,
  notification_history_id TEXT NOT NULL,
  received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS change_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id TEXT NOT NULL,
  change_type TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  previous_hash TEXT,
  current_hash TEXT,
  baseline INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_change_source ON change_log(source_id, id);

CREATE TABLE IF NOT EXISTS metrics (
  key TEXT PRIMARY KEY,
  value INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
"""

MIGRATIONS = {
    2: (
        "ALTER TABLE messages ADD COLUMN content_state TEXT NOT NULL DEFAULT 'available'",
        "ALTER TABLE resync_staging ADD COLUMN content_state TEXT NOT NULL DEFAULT 'available'",
    ),
}


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
    con.execute("PRAGMA journal_mode = WAL")
    con.executescript(SCHEMA)
    row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    current = int(row[0]) if row else SCHEMA_VERSION
    if current > SCHEMA_VERSION:
        raise RuntimeError("Gmail database schema is newer than this service")
    for version in range(current + 1, SCHEMA_VERSION + 1):
        for statement in MIGRATIONS.get(version, ()):
            con.execute(statement)
    con.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    con.commit()
    return con


def integrity_ok(con: sqlite3.Connection) -> bool:
    row = con.execute("PRAGMA integrity_check").fetchone()
    return bool(row) and str(row[0]).lower() == "ok"
