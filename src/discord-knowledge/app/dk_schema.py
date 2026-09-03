#!/usr/bin/env python3
"""Discord Knowledge — canonical SQLite schemas (source DB and search index).

Two databases, deliberately separate:

  source DB   /srv/hermes/knowledge/discord/messages.db
              Written by the collector only. Canonical normalized record,
              tombstones, backfill cursors, and the durable index queue.
              NOT mounted into the Hermes container.

  index DB    /srv/hermes/hermes-data/knowledge-index/discord/index.db
              Written by the indexer only, read (mode=ro) by the MCP server
              inside the Hermes container. Holds no deleted content.

Both schemas are created with CREATE ... IF NOT EXISTS and migrated
additively, so re-running is safe and never destroys data.
"""

from __future__ import annotations

import sqlite3

SOURCE_SCHEMA_VERSION = 1
INDEX_SCHEMA_VERSION = 1

# Spec section 28: stable, provenance-carrying source id.
SOURCE_ID_PREFIX = "discord"


def source_id_for(guild_id, channel_id, message_id) -> str:
    """discord:<guild_id>:<channel_id>:<message_id> — never change this shape."""
    return f"{SOURCE_ID_PREFIX}:{guild_id}:{channel_id}:{message_id}"


def parse_source_id(source_id: str):
    """Return (guild_id, channel_id, message_id) or None if malformed."""
    parts = str(source_id or "").split(":")
    if len(parts) != 4 or parts[0] != SOURCE_ID_PREFIX:
        return None
    guild, channel, message = parts[1], parts[2], parts[3]
    if not (guild.isdigit() and channel.isdigit() and message.isdigit()):
        return None
    return guild, channel, message


SOURCE_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS guilds (
  guild_id      TEXT PRIMARY KEY,
  guild_name    TEXT,
  first_seen_at TEXT,
  updated_at    TEXT
);

CREATE TABLE IF NOT EXISTS channels (
  channel_id        TEXT PRIMARY KEY,
  guild_id          TEXT NOT NULL,
  channel_name      TEXT,
  channel_type      TEXT,
  parent_channel_id TEXT,
  is_thread         INTEGER NOT NULL DEFAULT 0,
  readable          INTEGER,          -- 1 ok / 0 permission denied / NULL unknown
  last_error        TEXT,
  first_seen_at     TEXT,
  updated_at        TEXT,
  FOREIGN KEY (guild_id) REFERENCES guilds(guild_id)
);
CREATE INDEX IF NOT EXISTS idx_channels_guild ON channels(guild_id);

CREATE TABLE IF NOT EXISTS messages (
  source_id           TEXT PRIMARY KEY,   -- discord:<guild>:<channel>:<message>
  message_id          TEXT NOT NULL,
  guild_id            TEXT NOT NULL,
  guild_name          TEXT,
  channel_id          TEXT NOT NULL,
  channel_name        TEXT,
  channel_type        TEXT,
  thread_id           TEXT,
  thread_name         TEXT,
  parent_channel_id   TEXT,
  author_id           TEXT,
  author_name         TEXT,
  author_global_name  TEXT,
  author_is_bot       INTEGER NOT NULL DEFAULT 0,
  webhook_id          TEXT,
  created_at          TEXT,               -- UTC ISO-8601
  created_ts          INTEGER,            -- epoch milliseconds (ordering/range)
  edited_at           TEXT,               -- UTC ISO-8601 or NULL
  deleted_at          TEXT,               -- UTC ISO-8601 or NULL (tombstone)
  content             TEXT,               -- raw text; NULL once deleted
  content_normalized  TEXT,               -- mentions/emoji rendered readably
  search_text         TEXT,               -- what the index tokenizes (body + bounded embed text)
  content_hash        TEXT,               -- sha256 of raw content (edit detection)
  message_type        TEXT,
  reply_to_message_id TEXT,
  attachments_json    TEXT,               -- bounded metadata only, no binaries
  embeds_json         TEXT,               -- bounded text metadata only
  revision            INTEGER NOT NULL DEFAULT 0,
  ingest_source       TEXT,               -- 'gateway' | 'backfill'
  ingested_at         TEXT,
  updated_at          TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel_id, created_ts);
CREATE INDEX IF NOT EXISTS idx_messages_guild   ON messages(guild_id, created_ts);
CREATE INDEX IF NOT EXISTS idx_messages_msgid   ON messages(message_id);
CREATE INDEX IF NOT EXISTS idx_messages_live    ON messages(deleted_at, created_ts);

CREATE TABLE IF NOT EXISTS backfill_state (
  channel_id        TEXT PRIMARY KEY,
  guild_id          TEXT,
  last_cursor       TEXT,     -- oldest message id fetched so far (paging back)
  oldest_message_id TEXT,
  oldest_created_ts INTEGER,
  newest_message_id TEXT,
  newest_created_ts INTEGER,
  complete          INTEGER NOT NULL DEFAULT 0,
  message_count     INTEGER NOT NULL DEFAULT 0,
  last_error        TEXT,
  started_at        TEXT,
  updated_at        TEXT
);

CREATE TABLE IF NOT EXISTS ingest_state (key TEXT PRIMARY KEY, value TEXT);

-- Durable hand-off between "source updated" and "index updated" (section 55).
-- The indexer applies entries idempotently and deletes them only after the
-- index transaction has committed, so a crash replays instead of losing.
CREATE TABLE IF NOT EXISTS index_queue (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id   TEXT NOT NULL,
  op          TEXT NOT NULL,           -- 'upsert' | 'delete'
  revision    INTEGER,
  enqueued_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_queue_source ON index_queue(source_id);

CREATE TABLE IF NOT EXISTS metrics (key TEXT PRIMARY KEY, value TEXT);
"""

INDEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS messages (
  source_id           TEXT PRIMARY KEY,
  message_id          TEXT,
  guild_id            TEXT,
  guild_name          TEXT,
  channel_id          TEXT,
  channel_name        TEXT,
  channel_type        TEXT,
  thread_id           TEXT,
  thread_name         TEXT,
  parent_channel_id   TEXT,
  author_id           TEXT,
  author_name         TEXT,
  author_global_name  TEXT,
  author_is_bot       INTEGER,
  webhook_id          TEXT,
  created_at          TEXT,
  created_ts          INTEGER,
  edited_at           TEXT,
  deleted_at          TEXT,
  is_deleted          INTEGER NOT NULL DEFAULT 0,
  content             TEXT,             -- always NULL when is_deleted = 1
  message_type        TEXT,
  reply_to_message_id TEXT,
  attachments_json    TEXT,
  embeds_json         TEXT,
  revision            INTEGER,
  indexed_at          TEXT
);
CREATE INDEX IF NOT EXISTS idx_idx_channel ON messages(channel_id, created_ts);
CREATE INDEX IF NOT EXISTS idx_idx_guild   ON messages(guild_id, created_ts);
CREATE INDEX IF NOT EXISTS idx_idx_author  ON messages(author_id);
CREATE INDEX IF NOT EXISTS idx_idx_live    ON messages(is_deleted, created_ts);

-- trigram tokenizer: substring matching that works for Japanese without a
-- word segmenter. Queries shorter than 3 characters fall back to LIKE in the
-- MCP layer (the same lesson the LINE index taught).
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
  source_id UNINDEXED,
  content,
  channel_name,
  author_name,
  guild_name,
  tokenize='trigram'
);
"""


def _apply_additive_columns(con: sqlite3.Connection, table: str, columns: dict) -> list:
    """Add any missing column. Never drops, renames or rewrites existing data."""
    have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    if not have:
        return []
    added = []
    for name, decl in columns.items():
        if name not in have:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            added.append(name)
    return added


SOURCE_ADDITIVE = {
    "messages": {
        "content_normalized": "TEXT",
        "search_text": "TEXT",
        "content_hash": "TEXT",
        "revision": "INTEGER NOT NULL DEFAULT 0",
        "ingest_source": "TEXT",
    },
    "channels": {
        "readable": "INTEGER",
        "last_error": "TEXT",
    },
    "backfill_state": {
        "newest_created_ts": "INTEGER",
        "started_at": "TEXT",
    },
}

INDEX_ADDITIVE = {
    "messages": {
        "is_deleted": "INTEGER NOT NULL DEFAULT 0",
        "revision": "INTEGER",
        "thread_name": "TEXT",
    },
}


def connect(
    path,
    *,
    readonly: bool = False,
    timeout: float = 30.0,
    check_same_thread: bool = True,
) -> sqlite3.Connection:
    """`check_same_thread=False` is used by the collector, whose single
    writer task hands DB work to a worker thread so disk I/O never blocks the
    gateway heartbeat. Exactly one thread touches the connection at a time."""
    if readonly:
        con = sqlite3.connect(
            f"file:{path}?mode=ro",
            uri=True,
            timeout=timeout,
            check_same_thread=check_same_thread,
        )
    else:
        con = sqlite3.connect(
            str(path), timeout=timeout, check_same_thread=check_same_thread
        )
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 30000")
    if not readonly:
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("PRAGMA synchronous = NORMAL")
        con.execute("PRAGMA foreign_keys = ON")
    return con


def init_source_db(con: sqlite3.Connection) -> sqlite3.Connection:
    con.executescript(SOURCE_SCHEMA)
    for table, cols in SOURCE_ADDITIVE.items():
        _apply_additive_columns(con, table, cols)
    con.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
        (str(SOURCE_SCHEMA_VERSION),),
    )
    con.commit()
    return con


def init_index_db(con: sqlite3.Connection) -> sqlite3.Connection:
    con.executescript(INDEX_SCHEMA)
    for table, cols in INDEX_ADDITIVE.items():
        _apply_additive_columns(con, table, cols)
    con.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('index_schema_version', ?)",
        (str(INDEX_SCHEMA_VERSION),),
    )
    con.commit()
    return con


def open_source_db(path) -> sqlite3.Connection:
    return init_source_db(connect(path))


def open_index_db(path) -> sqlite3.Connection:
    return init_index_db(connect(path))


def integrity_ok(con: sqlite3.Connection) -> bool:
    row = con.execute("PRAGMA integrity_check").fetchone()
    return bool(row) and str(row[0]).lower() == "ok"
