#!/usr/bin/env python3
"""Attention Engine — durable state schema.

One SQLite database. It exists so that the two questions that decide whether
the owner's phone buzzes can be answered after a crash:

    "have I already said this?"      -> signals, notifications, channel_budget
    "did the last thing I said arrive?" -> delivery_attempts

Everything here is the engine's own record. No credential is stored, and no
message from LINE or Discord is copied in wholesale — what lands in `summary`
is the Event Layer's own one-line description of a commitment or request,
bounded on the way in, and it is treated as data at every step (section 75).

  signals                a normalised thing that happened, keyed so the same
                         thing produces the same row every scan (section 16)
  attention_decisions    what the policy decided and WHY, with the policy
                         version, so an old decision can be explained later
                         (sections 76-79)
  notifications          one row per thing we chose to say, with its state
                         machine (section 45)
  delivery_attempts      one row per channel per try
  notification_episodes  health failures as episodes rather than as events,
                         so a service that is down for an hour produces one
                         alert and one recovery (sections 36-38)
  digests                which signals went into which digest
  channel_budget         the local LINE ledger, and the reservation that stops
                         two notifications spending the same last credit
                         (section 82)
  suppressions           "not this one, not for a while" — set by the owner
  scan_runs              the engine's own liveness (section 90)

Why a separate database rather than new tables in an existing one: the Event
Layer, the jobs supervisor and the gateway each own their schema, and a phase
that reaches into three other components' databases is a phase that cannot be
rolled back independently (section 144). Nothing here migrates anything.
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 1

#: Bumped whenever a decision rule changes, and written onto every decision
#: row. "Why did it notify me in September?" is answerable only if the rules
#: that were in force are identifiable (sections 77, 79).
POLICY_VERSION = "g2.2-owner"

DECISIONS = ("NOTIFY_NOW", "DEFER", "DIGEST", "SUPPRESS")

NOTIFICATION_STATES = (
    "PENDING",      # decided, not yet handed to the gateway
    "DELIVERING",   # handed over; outcome unknown (the dangerous one)
    "DELIVERED",    # at least one channel confirmed
    "FAILED",       # every channel refused, and retries are exhausted
    "SUPPRESSED",   # decided against before it was ever sent
    "DEFERRED",     # held for quiet hours or a burst cap
)

SEVERITIES = ("INFO", "IMPORTANT", "URGENT", "CRITICAL")

SOURCE_EVENT = "event"
SOURCE_CODING = "coding"
SOURCE_HEALTH = "health"

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS signals (
  signal_id     TEXT PRIMARY KEY,   -- derived, stable, NOT time-based
  source_type   TEXT NOT NULL,      -- 'event' | 'coding' | 'health'
  source_id     TEXT NOT NULL,
  signal_type   TEXT NOT NULL,
  created_at    TEXT NOT NULL,      -- when the underlying thing HAPPENED
  observed_at   TEXT NOT NULL,      -- when this engine first saw it
  last_seen_at  TEXT NOT NULL,
  subject       TEXT NOT NULL,
  summary       TEXT NOT NULL,
  severity      TEXT NOT NULL,
  urgency       REAL,
  confidence    REAL,
  actionability REAL,
  due_at        TEXT,
  expires_at    TEXT,
  dedupe_key    TEXT NOT NULL,
  entity_key    TEXT NOT NULL,      -- the thing itself, across signal types
  recommended_channel TEXT,
  metadata      TEXT NOT NULL DEFAULT '{}',
  state         TEXT NOT NULL DEFAULT 'NEW',   -- NEW | DECIDED
  seen_count    INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_signals_state ON signals(state, observed_at);
CREATE INDEX IF NOT EXISTS idx_signals_dedupe ON signals(dedupe_key);
CREATE INDEX IF NOT EXISTS idx_signals_entity ON signals(entity_key);

CREATE TABLE IF NOT EXISTS attention_decisions (
  decision_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  signal_id       TEXT NOT NULL,
  decided_at      TEXT NOT NULL,
  decision        TEXT NOT NULL,
  reason          TEXT NOT NULL,    -- machine-readable, from REASONS
  policy_version  TEXT NOT NULL,
  severity        TEXT NOT NULL,
  channels        TEXT NOT NULL DEFAULT '[]',
  budget_state    TEXT,
  detail          TEXT,
  notification_id TEXT,
  deliver_after   TEXT,             -- set when the decision is DEFER
  FOREIGN KEY (signal_id) REFERENCES signals(signal_id)
);
CREATE INDEX IF NOT EXISTS idx_decisions_signal
  ON attention_decisions(signal_id, decided_at);
CREATE INDEX IF NOT EXISTS idx_decisions_deferred
  ON attention_decisions(decision, deliver_after);

CREATE TABLE IF NOT EXISTS notifications (
  notification_id  TEXT PRIMARY KEY,
  kind             TEXT NOT NULL,   -- 'signal' | 'digest'
  dedupe_key       TEXT NOT NULL,
  severity         TEXT NOT NULL,
  subject          TEXT NOT NULL,
  body             TEXT NOT NULL,
  reason           TEXT NOT NULL,
  policy_version   TEXT NOT NULL,
  source_type      TEXT,
  source_id        TEXT,
  content_revision TEXT NOT NULL,
  state            TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  delivered_at     TEXT,
  delivered_channels TEXT NOT NULL DEFAULT '[]',
  last_error       TEXT,
  attempts         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_notifications_state
  ON notifications(state, created_at);
CREATE INDEX IF NOT EXISTS idx_notifications_dedupe
  ON notifications(dedupe_key, created_at);

CREATE TABLE IF NOT EXISTS delivery_attempts (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  notification_id TEXT NOT NULL,
  channel         TEXT NOT NULL,
  attempted_at    TEXT NOT NULL,
  outcome         TEXT NOT NULL,   -- delivered|duplicate|failed|rejected|unreachable
  detail          TEXT,
  quota_consumed  INTEGER NOT NULL DEFAULT 0,
  http_status     INTEGER,
  FOREIGN KEY (notification_id) REFERENCES notifications(notification_id)
);
CREATE INDEX IF NOT EXISTS idx_attempts_notification
  ON delivery_attempts(notification_id, attempted_at);

-- A failure is an EPISODE, not an event. Without this a service that is down
-- for an hour produces twelve identical alerts at a five-minute scan, which
-- is the alert storm section 35 forbids.
CREATE TABLE IF NOT EXISTS notification_episodes (
  episode_id           TEXT PRIMARY KEY,
  service              TEXT NOT NULL,
  reason               TEXT NOT NULL,
  state                TEXT NOT NULL,   -- 'DEGRADED' | 'RECOVERED'
  first_degraded_at    TEXT NOT NULL,
  last_degraded_at     TEXT NOT NULL,
  observations         INTEGER NOT NULL DEFAULT 1,
  notified_at          TEXT,
  recovered_at         TEXT,
  recovery_notified_at TEXT,
  detail               TEXT
);
CREATE INDEX IF NOT EXISTS idx_episodes_open
  ON notification_episodes(service, state);

CREATE TABLE IF NOT EXISTS digests (
  digest_id       TEXT PRIMARY KEY,
  kind            TEXT NOT NULL,      -- 'catchup' | 'recovery' | 'baseline'
  created_at      TEXT NOT NULL,
  item_count      INTEGER NOT NULL,
  notification_id TEXT,
  signal_ids      TEXT NOT NULL DEFAULT '[]'
);

-- The reservation, not just the receipt. Inserting the row and counting the
-- day's rows happen in one transaction, so two notifications decided in the
-- same scan cannot both believe they have the last credit (section 82).
CREATE TABLE IF NOT EXISTS channel_budget (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  channel         TEXT NOT NULL,
  notification_id TEXT NOT NULL,
  severity        TEXT NOT NULL,
  day             TEXT NOT NULL,   -- YYYY-MM-DD in the policy timezone
  month           TEXT NOT NULL,   -- YYYY-MM   ditto
  state           TEXT NOT NULL,   -- RESERVED | SPENT | RELEASED
  reserved_at     TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_budget_once
  ON channel_budget(channel, notification_id);
CREATE INDEX IF NOT EXISTS idx_budget_day
  ON channel_budget(channel, day, state);

CREATE TABLE IF NOT EXISTS suppressions (
  dedupe_key TEXT PRIMARY KEY,
  until      TEXT NOT NULL,
  created_at TEXT NOT NULL,
  origin     TEXT NOT NULL,
  note       TEXT
);

CREATE TABLE IF NOT EXISTS scan_runs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at    TEXT NOT NULL,
  finished_at   TEXT,
  ok            INTEGER,
  signals_seen  INTEGER NOT NULL DEFAULT 0,
  signals_new   INTEGER NOT NULL DEFAULT 0,
  notify_now    INTEGER NOT NULL DEFAULT 0,
  deferred      INTEGER NOT NULL DEFAULT 0,
  digested      INTEGER NOT NULL DEFAULT 0,
  suppressed    INTEGER NOT NULL DEFAULT 0,
  delivered     INTEGER NOT NULL DEFAULT 0,
  failed        INTEGER NOT NULL DEFAULT 0,
  llm_calls     INTEGER NOT NULL DEFAULT 0,
  duration_ms   INTEGER,
  error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_scan_runs_started ON scan_runs(started_at);
"""


def connect(path, *, readonly: bool = False, timeout: float = 30.0) -> sqlite3.Connection:
    if readonly:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=timeout)
    else:
        con = sqlite3.connect(str(path), timeout=timeout)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 30000")
    if not readonly:
        con.execute("PRAGMA journal_mode = WAL")
        # NORMAL rather than FULL: this database sits on a USB flash disk that
        # the whole system shares, and the engine writes on a five-minute
        # cadence. A power cut can cost the last scan's rows; it cannot
        # corrupt the file, and re-deriving one scan is free because every
        # signal id is derived rather than generated (section 138).
        con.execute("PRAGMA synchronous = NORMAL")
        con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db(con: sqlite3.Connection) -> sqlite3.Connection:
    con.executescript(SCHEMA)
    con.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    # The install watermark. Everything that already existed when the engine
    # was first switched on is history, not news: at the moment of writing
    # this the Event Layer held 48 open events, 13 of them already past their
    # deadline, every one of which a naive "overdue" rule would have announced
    # on the first scan (sections 25, 95, 96).
    con.execute(
        "INSERT OR IGNORE INTO meta (key, value)"
        " VALUES ('initialized_at', datetime('now'))"
    )
    con.commit()
    return con


def open_db(path) -> sqlite3.Connection:
    return init_db(connect(path))


def get_meta(con: sqlite3.Connection, key: str, default=None):
    row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(con: sqlite3.Connection, key: str, value) -> None:
    con.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def integrity_ok(con: sqlite3.Connection) -> bool:
    row = con.execute("PRAGMA integrity_check").fetchone()
    return bool(row) and str(row[0]).lower() == "ok"
