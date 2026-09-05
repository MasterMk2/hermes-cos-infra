#!/usr/bin/env python3
"""Publish a no-WAL read-only Gmail retrieval snapshot."""

from __future__ import annotations

import os
import json
import hashlib
import sqlite3
import tempfile
from pathlib import Path

import gm_store


INDEX_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE gmail_messages (
  source_id TEXT PRIMARY KEY,
  message_id TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  received_at TEXT,
  internal_date_ms INTEGER,
  from_header TEXT NOT NULL,
  to_header TEXT NOT NULL,
  cc_header TEXT NOT NULL,
  subject TEXT NOT NULL,
  labels_json TEXT NOT NULL,
  routine_excluded INTEGER NOT NULL,
  snippet TEXT NOT NULL,
  body_text TEXT NOT NULL,
  commitment_text TEXT NOT NULL,
  content_state TEXT NOT NULL,
  mime_json TEXT NOT NULL,
  attachments_json TEXT NOT NULL,
  is_sent INTEGER NOT NULL,
  deleted_at TEXT,
  indexed_at TEXT NOT NULL,
  revision INTEGER NOT NULL,
  baseline INTEGER NOT NULL,
  content_hash TEXT NOT NULL
);
CREATE INDEX idx_gmail_messages_thread_date ON gmail_messages(thread_id,internal_date_ms);
CREATE INDEX idx_gmail_messages_live_date ON gmail_messages(deleted_at,internal_date_ms);

CREATE VIRTUAL TABLE gmail_fts USING fts5(
  source_id UNINDEXED,
  subject,
  from_header,
  to_header,
  body_text,
  tokenize='trigram'
);

CREATE TABLE gmail_status (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  auth_state TEXT NOT NULL,
  history_id_present INTEGER NOT NULL,
  initialized_at TEXT,
  last_full_sync_at TEXT,
  last_incremental_sync_at TEXT,
  last_api_success_at TEXT,
  last_message_ingested_at TEXT,
  last_error_code TEXT,
  consecutive_failures INTEGER NOT NULL,
  body_unavailable_count INTEGER NOT NULL,
  history_reset_count INTEGER NOT NULL,
  message_count INTEGER NOT NULL,
  thread_count INTEGER NOT NULL,
  oldest_received_at TEXT,
  newest_received_at TEXT,
  watch_state TEXT NOT NULL,
  watch_expiration_ms INTEGER,
  last_notification_at TEXT,
  pending_sync_age_seconds INTEGER,
  published_at TEXT NOT NULL
);
"""


def _atomic_replace(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(source), os.O_RDWR)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    if os.name == "nt" and destination.exists():
        # Windows prevents replacement of a read-only file.  The Pi target is
        # Linux and retains the immutable-to-readers 0440 mode after publish.
        os.chmod(destination, 0o644)
    os.replace(source, destination)
    try:
        directory_fd = os.open(str(destination.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        # Directory fsync is not available on every development platform.
        # The production deployment is Linux; replacement itself remains
        # atomic everywhere supported by os.replace.
        pass


def _atomic_json(destination: Path, value: dict) -> None:
    """Publish non-content health state without rebuilding the FTS snapshot."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".gmail-status-", suffix=".json", dir=str(destination.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        _atomic_replace(temporary, destination)
        if os.name != "nt":
            os.chmod(destination, 0o440)
    finally:
        if temporary.exists():
            temporary.unlink()


def _pending_age(value: str | None, now) -> int | None:
    if not value:
        return None
    try:
        from datetime import datetime
        return max(0, int((now - datetime.fromisoformat(str(value))).total_seconds()))
    except (TypeError, ValueError):
        return None


def _routine_excluded(labels_json: str) -> bool:
    """Keep SPAM/TRASH evidence canonical, but out of routine retrieval."""
    try:
        import json
        labels = {str(item) for item in json.loads(labels_json or "[]")}
    except (TypeError, ValueError):
        return False
    return bool({"SPAM", "TRASH"} & labels)


def _content_signature(authority_con: sqlite3.Connection) -> str:
    row = authority_con.execute(
        "SELECT COALESCE(MAX(id),0),COUNT(*) FROM change_log"
    ).fetchone()
    return f"{int(row[0] or 0)}:{int(row[1] or 0)}"


def _published_signature(destination: Path) -> str | None:
    if not destination.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{destination}?mode=ro", uri=True, timeout=2)
        try:
            row = con.execute("SELECT value FROM meta WHERE key='content_signature'").fetchone()
            return str(row[0]) if row else None
        finally:
            con.close()
    except sqlite3.Error:
        return None


def _snapshot_signature(con: sqlite3.Connection, table: str) -> str:
    """Content-only identity used to adopt an index created before H.2.1."""
    digest = hashlib.sha256()
    for row in con.execute(
        f"SELECT source_id,content_hash,deleted_at,revision FROM {table} ORDER BY source_id"
    ):
        for value in row:
            digest.update(str(value or "").encode("utf-8"))
            digest.update(b"\0")
    return digest.hexdigest()


def _legacy_snapshot_matches(authority_con: sqlite3.Connection, destination: Path) -> bool:
    """Avoid one unnecessary heavy rebuild when adopting a pre-signature index."""
    if not destination.exists():
        return False
    try:
        index_con = sqlite3.connect(f"file:{destination}?mode=ro", uri=True, timeout=2)
        try:
            return _snapshot_signature(authority_con, "messages") == _snapshot_signature(index_con, "gmail_messages")
        finally:
            index_con.close()
    except sqlite3.Error:
        return False


def publish_index(authority_con: sqlite3.Connection, destination: str | Path) -> bool:
    """Write a complete replacement file; readers never see a partial index."""
    destination = Path(destination)
    status = gm_store.status_dict(authority_con)
    status_path = destination.with_name("status.json")
    signature = _content_signature(authority_con)
    published = _published_signature(destination)
    if signature == published or (published is None and _legacy_snapshot_matches(authority_con, destination)):
        _atomic_json(status_path, status)
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".gmail-index-", suffix=".db", dir=str(destination.parent))
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        target = sqlite3.connect(temporary)
        try:
            target.executescript(INDEX_SCHEMA)
            target.execute("INSERT INTO meta(key,value) VALUES('content_signature',?)", (signature,))
            rows = authority_con.execute(
                """SELECT source_id,message_id,thread_id,received_at,internal_date_ms,from_header,to_header,
                   cc_header,subject,labels_json,snippet,body_text,commitment_text,content_state,mime_json,
                   attachments_json,is_sent,deleted_at,ingested_at,revision,baseline,content_hash
                   FROM messages ORDER BY internal_date_ms,message_id"""
            ).fetchall()
            now = gm_store.now_iso()
            for row in rows:
                target.execute(
                    "INSERT INTO gmail_messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    tuple(row[:10]) + (int(_routine_excluded(row[9])),) + tuple(row[10:]),
                )
                if (not row["deleted_at"] and not _routine_excluded(row["labels_json"])
                        and row["content_state"] == "available"):
                    target.execute(
                        "INSERT INTO gmail_fts VALUES(?,?,?,?,?)",
                        (row["source_id"], row["subject"], row["from_header"], row["to_header"], row["body_text"]),
                    )
            target.execute(
                "INSERT INTO gmail_status VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    1, status["auth_state"], int(status["history_id_present"]), status["initialized_at"],
                    status["last_full_sync_at"], status["last_incremental_sync_at"], status["last_api_success_at"],
                    status["last_message_ingested_at"], status["last_error_code"], status["consecutive_failures"],
                    status["body_unavailable_count"], status["history_reset_count"], status["message_count"], status["thread_count"],
                    status["oldest_received_at"], status["newest_received_at"], status["watch_state"],
                    status["watch_expiration_ms"], status["last_notification_at"],
                    _pending_age((status["pending_sync"] or {}).get("requested_at"), __import__("datetime").datetime.fromisoformat(now)),
                    now,
                ),
            )
            target.commit()
            target.execute("PRAGMA journal_mode=DELETE")
        finally:
            target.close()
        _atomic_replace(temporary, destination)
        if os.name != "nt":
            os.chmod(destination, 0o440)
        _atomic_json(status_path, status)
        return True
    finally:
        if temporary.exists():
            temporary.unlink()
