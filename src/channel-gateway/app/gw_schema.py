#!/usr/bin/env python3
"""Unified Channel Gateway — durable state schema.

One SQLite database, at /srv/hermes/channel-gateway/state/gateway.db, holding
everything that must survive a restart:

  inbound_messages    what a channel sent us, and how far it got
  outbound_messages   what we owe a channel, and whether it was delivered
  delivery_attempts   one row per send attempt, so a retry can prove it is a
                      retry rather than a second answer
  conversation_state  the per-conversation lock that keeps one request in
                      flight at a time, and the session id we reuse
  audit_events        who asked, whether they were the owner, what happened

Phase G.2 adds two more, both about messages nobody asked for:

  proactive_deliveries  one row per (notification, channel) the Attention
                        Engine asked us to deliver. The uniqueness is the
                        whole of duplicate suppression across a crash.
  line_quota_cache      the last answer from LINE's quota endpoints, so a
                        restart neither loses the numbers nor forgets that
                        the token was refused an hour ago.

The gateway is not an agent. Nothing here holds personal knowledge, memory or
reasoning state — those stay inside Hermes. What is stored is the minimum
needed to answer "did we already handle this?" and "did the reply go out?".

Message text IS stored, because a queued message that cannot be replayed
after a crash is a message lost. It is bounded and expired: see
`purge_expired`, and the retention setting in the configuration.
"""

from __future__ import annotations

import sqlite3

#: 2 adds the two G.2 tables. The bump is informational: every statement in
#: SCHEMA is CREATE ... IF NOT EXISTS, so applying version 2 to a version 1
#: database adds the new tables and touches nothing that exists (section 144).
SCHEMA_VERSION = 2

# Inbound lifecycle (spec section 57). Terminal states are RESPONDED, FAILED
# and CANCELLED; UNKNOWN is a non-retrying hold, not success or failure.
INBOUND_STATES = (
    "RECEIVED",       # accepted from the platform, signature already verified
    "AUTHENTICATED",  # sender matched the owner allowlist
    "QUEUED",         # durable, waiting for a worker
    "PROCESSING",     # handed to Hermes; a crash here is retried, not lost
    "RESPONDED",      # a reply was delivered
    "FAILED",         # gave up after bounded retries
    "CANCELLED",      # superseded or dropped deliberately
    "UNKNOWN",        # unresolved reply; held, never automatically retried
)

TERMINAL_INBOUND = frozenset({"RESPONDED", "FAILED", "CANCELLED", "UNKNOWN"})

OUTBOUND_STATES = ("PENDING", "SENDING", "SENT", "FAILED", "ABANDONED", "NOT_SENT", "UNKNOWN")
# Reply NOT_SENT is a new, proven-unsent reservation or local preflight failure.
# Old PENDING/FAILED rows predate write-ahead sending and cannot prove that.

# Proactive lifecycle (G.2 spec section 45). SENDING is the dangerous one: it
# means an adapter call was started and its outcome is not yet known, which is
# exactly the state a crash leaves behind.
# UNKNOWN is a durable hold, not a failure or permission to retry. A process
# can die after the provider accepted a message but before SENT was recorded.
PROACTIVE_STATES = ("PENDING", "SENDING", "SENT", "FAILED", "UNKNOWN", "NOT_SENT")
# NOT_SENT carries a definite negative receipt and may be retried. Historical
# FAILED rows did not distinguish a rejection from a partial/ambiguous send;
# claim() conservatively holds those as UNKNOWN instead of guessing.

PROACTIVE_CHANNELS = ("discord", "line")

# Canonical severities (section 52). The gateway does not interpret these — it
# records them and lets the engine's policy decide the channel — but it
# refuses an unknown one so a typo cannot become a new severity by accident.
SEVERITIES = ("INFO", "IMPORTANT", "URGENT", "CRITICAL")

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS inbound_messages (
  gateway_request_id  TEXT PRIMARY KEY,   -- our correlation id, ours alone
  platform            TEXT NOT NULL,      -- 'line' | 'discord'
  platform_message_id TEXT NOT NULL,      -- the platform's own id
  platform_event_id   TEXT,               -- LINE webhook event id, when given
  platform_user_id    TEXT NOT NULL,
  conversation_id     TEXT NOT NULL,
  received_at         TEXT NOT NULL,
  platform_timestamp  TEXT,
  text                TEXT,
  reply_token         TEXT,               -- LINE only; one use, short lived
  reply_token_expires_at TEXT,
  authz_subject       TEXT NOT NULL,      -- 'owner' | 'unauthorized'
  capability_profile  TEXT NOT NULL,
  state               TEXT NOT NULL,
  attempts            INTEGER NOT NULL DEFAULT 0,
  last_error          TEXT,               -- category only, never a payload
  hermes_run_id       TEXT,
  claimed_at          TEXT,               -- set while PROCESSING
  completed_at        TEXT,
  updated_at          TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_inbound_dedupe
  ON inbound_messages(platform, platform_message_id);
CREATE INDEX IF NOT EXISTS idx_inbound_state ON inbound_messages(state, received_at);
CREATE INDEX IF NOT EXISTS idx_inbound_conv ON inbound_messages(conversation_id, received_at);

CREATE TABLE IF NOT EXISTS outbound_messages (
  outbound_id        TEXT PRIMARY KEY,
  gateway_request_id TEXT NOT NULL,
  platform           TEXT NOT NULL,
  conversation_id    TEXT NOT NULL,
  target_user_id     TEXT NOT NULL,       -- always the owner; never taken from a payload
  body               TEXT NOT NULL,
  chunk_index        INTEGER NOT NULL DEFAULT 0,
  chunk_total        INTEGER NOT NULL DEFAULT 1,
  delivery_mode      TEXT,                -- 'reply' | 'push' | 'dm'
  state              TEXT NOT NULL,
  attempts           INTEGER NOT NULL DEFAULT 0,
  last_error         TEXT,
  created_at         TEXT NOT NULL,
  sent_at            TEXT,
  updated_at         TEXT NOT NULL,
  FOREIGN KEY (gateway_request_id) REFERENCES inbound_messages(gateway_request_id)
);
-- One logical answer per request per chunk: the uniqueness that stops a retry
-- from becoming a second reply (spec section 60).
CREATE UNIQUE INDEX IF NOT EXISTS idx_outbound_once
  ON outbound_messages(gateway_request_id, chunk_index);
CREATE INDEX IF NOT EXISTS idx_outbound_state ON outbound_messages(state, created_at);

CREATE TABLE IF NOT EXISTS delivery_attempts (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  outbound_id        TEXT NOT NULL,
  attempted_at       TEXT NOT NULL,
  mode               TEXT,
  outcome            TEXT NOT NULL,       -- 'sent' | 'retryable' | 'permanent'
  status_code        INTEGER,
  error_category     TEXT,                -- never a body, never a token
  FOREIGN KEY (outbound_id) REFERENCES outbound_messages(outbound_id)
);
CREATE INDEX IF NOT EXISTS idx_attempts_outbound ON delivery_attempts(outbound_id);

CREATE TABLE IF NOT EXISTS conversation_state (
  conversation_id   TEXT PRIMARY KEY,
  platform          TEXT NOT NULL,
  hermes_session_id TEXT,                 -- per channel, never shared (section 129)
  active_request_id TEXT,                 -- the one in flight; NULL when idle
  active_since      TEXT,
  last_message_at   TEXT,
  message_count     INTEGER NOT NULL DEFAULT 0,
  updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  at                 TEXT NOT NULL,
  gateway_request_id TEXT,
  platform           TEXT,
  event              TEXT NOT NULL,
  authenticated      INTEGER,
  capability_profile TEXT,
  detail             TEXT,                -- bounded, category-level, no body
  FOREIGN KEY (gateway_request_id) REFERENCES inbound_messages(gateway_request_id)
);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_events(at);
CREATE INDEX IF NOT EXISTS idx_audit_request ON audit_events(gateway_request_id);

CREATE TABLE IF NOT EXISTS metrics (key TEXT PRIMARY KEY, value TEXT);

-- ---------------------------------------------------------------- G.2 ----

-- One row per (notification, channel). Deliberately NOT part of
-- outbound_messages: that table has a foreign key to inbound_messages, and a
-- proactive notification has no inbound message — reusing it would mean
-- forging one, which would corrupt every count that reads the inbound queue.
CREATE TABLE IF NOT EXISTS proactive_deliveries (
  delivery_id      TEXT PRIMARY KEY,   -- derived from (notification_id, channel)
  notification_id  TEXT NOT NULL,      -- the engine's id; ours to honour, not mint
  channel          TEXT NOT NULL,      -- 'discord' | 'line'
  severity         TEXT NOT NULL,
  dedupe_key       TEXT,               -- the engine's, for the audit trail only
  reason           TEXT,               -- machine-readable decision reason
  content_revision TEXT NOT NULL,      -- sha256 of the exact text delivered
  body             TEXT NOT NULL,
  target_user_id   TEXT NOT NULL,      -- always the configured owner
  state            TEXT NOT NULL,
  delivery_mode    TEXT,               -- 'push' (line) | 'dm' (discord)
  attempts         INTEGER NOT NULL DEFAULT 0,
  quota_consumed   INTEGER NOT NULL DEFAULT 0,
  last_error       TEXT,
  created_at       TEXT NOT NULL,
  sent_at          TEXT,
  updated_at       TEXT NOT NULL
);
-- Redundant with the primary key, on purpose: the id is derived from these
-- two columns, and if that derivation is ever changed or broken this index
-- still refuses the second charge for the same notification.
CREATE UNIQUE INDEX IF NOT EXISTS idx_proactive_once
  ON proactive_deliveries(notification_id, channel);
CREATE INDEX IF NOT EXISTS idx_proactive_state
  ON proactive_deliveries(state, created_at);
CREATE INDEX IF NOT EXISTS idx_proactive_sent
  ON proactive_deliveries(channel, state, sent_at);

-- Exactly one row. Survives a restart so the engine does not pay a quota call
-- to rediscover what was read four minutes ago, and so an authentication
-- failure is still remembered after a crash-restart (section 120).
CREATE TABLE IF NOT EXISTS line_quota_cache (
  id             INTEGER PRIMARY KEY CHECK (id = 1),
  ok             INTEGER NOT NULL,
  quota_type     TEXT,
  limit_value    INTEGER,
  used           INTEGER,
  error_category TEXT,
  fetched_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);
"""

ADDITIVE = {
    "inbound_messages": {
        "platform_event_id": "TEXT",
        "hermes_run_id": "TEXT",
        "reply_token_expires_at": "TEXT",
        # When the Hermes run for this request was first started. The wait's
        # hard deadline is measured from here, so a restart that resumes the
        # same run does not hand it a fresh allowance.
        "run_started_at": "TEXT",
    },
    "outbound_messages": {
        "delivery_mode": "TEXT",
        "chunk_total": "INTEGER NOT NULL DEFAULT 1",
    },
}


def _apply_additive(con: sqlite3.Connection, table: str, columns: dict) -> list:
    have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    if not have:
        return []
    added = []
    for name, decl in columns.items():
        if name not in have:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            added.append(name)
    return added


def connect(path, *, readonly: bool = False, timeout: float = 30.0,
            check_same_thread: bool = True) -> sqlite3.Connection:
    if readonly:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=timeout,
                              check_same_thread=check_same_thread)
    else:
        con = sqlite3.connect(str(path), timeout=timeout,
                              check_same_thread=check_same_thread)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 30000")
    if not readonly:
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("PRAGMA synchronous = NORMAL")
        con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db(con: sqlite3.Connection) -> sqlite3.Connection:
    con.executescript(SCHEMA)
    for table, cols in ADDITIVE.items():
        _apply_additive(con, table, cols)
    con.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    con.commit()
    return con


def open_db(path) -> sqlite3.Connection:
    return init_db(connect(path))


def integrity_ok(con: sqlite3.Connection) -> bool:
    row = con.execute("PRAGMA integrity_check").fetchone()
    return bool(row) and str(row[0]).lower() == "ok"
