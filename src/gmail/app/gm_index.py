#!/usr/bin/env python3
"""Publish a no-WAL read-only Gmail retrieval snapshot.

``index.db`` is a symlink to one of two copies.  A change is written into the
copy readers are not using and the link is then swapped, so readers never wait
on a writer lock.  Most changes touch a few messages and are applied in place;
a full rebuild is written when that is not possible, and once a night to
compact the FTS segments the in-place updates leave behind.
"""

from __future__ import annotations

import os
import json
import hashlib
import logging
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import gm_store


log = logging.getLogger("gmail.index")

SLOTS = ("index-a.db", "index-b.db")
# FTS rowids equal gmail_messages rowids, so one message's FTS entry can be
# deleted without scanning the FTS table.
LAYOUT = "rowid-v1"
# Beyond this many changed messages a fresh build costs about the same.
MAX_INCREMENTAL_SOURCES = 500
# Local hour of the nightly compaction, clear of the 01:30 LINE refresh and
# the 08:30 brief.
COMPACTION_HOUR = 4

MESSAGE_COLUMNS = """source_id,message_id,thread_id,received_at,internal_date_ms,from_header,to_header,
   cc_header,subject,labels_json,snippet,body_text,commitment_text,content_state,mime_json,
   attachments_json,is_sent,deleted_at,ingested_at,revision,baseline,content_hash"""

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
    _fsync_directory(destination.parent)


def _fsync_directory(directory: Path) -> None:
    try:
        directory_fd = os.open(str(directory), os.O_RDONLY)
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


def _read_meta(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        try:
            return dict(con.execute("SELECT key,value FROM meta").fetchall())
        finally:
            con.close()
    except sqlite3.Error:
        return None


def _active_slot(destination: Path) -> str | None:
    try:
        target = os.readlink(destination)
    except OSError:
        return None
    return target if target in SLOTS else None


def _inactive_slot(destination: Path) -> Path:
    name = SLOTS[1] if _active_slot(destination) == SLOTS[0] else SLOTS[0]
    return destination.with_name(name)


def _point_link(destination: Path, slot: Path) -> None:
    """Swap the published name to the other copy in one rename."""
    link = destination.with_name(f".{destination.name}.link")
    link.unlink(missing_ok=True)
    os.symlink(slot.name, link)
    os.replace(link, destination)
    _fsync_directory(destination.parent)


def _message_values(row: sqlite3.Row) -> tuple:
    return tuple(row[:10]) + (int(_routine_excluded(row[9])),) + tuple(row[10:])


def _searchable(row: sqlite3.Row) -> tuple | None:
    """The FTS document for an authority row, or None when it is not searchable."""
    if row["deleted_at"] or _routine_excluded(row["labels_json"]) or row["content_state"] != "available":
        return None
    return (row["source_id"], row["subject"], row["from_header"], row["to_header"], row["body_text"])


def _insert_searchable(target: sqlite3.Connection, rowid: int, document: tuple | None) -> None:
    if document is not None:
        target.execute(
            "INSERT INTO gmail_fts(rowid,source_id,subject,from_header,to_header,body_text) VALUES(?,?,?,?,?,?)",
            (rowid,) + document,
        )


def _insert_message(target: sqlite3.Connection, row: sqlite3.Row) -> None:
    cursor = target.execute(
        "INSERT INTO gmail_messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", _message_values(row)
    )
    _insert_searchable(target, cursor.lastrowid, _searchable(row))


def _write_status(target: sqlite3.Connection, status: dict) -> None:
    now = gm_store.now_iso()
    target.execute("DELETE FROM gmail_status")
    target.execute(
        "INSERT INTO gmail_status VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            1, status["auth_state"], int(status["history_id_present"]), status["initialized_at"],
            status["last_full_sync_at"], status["last_incremental_sync_at"], status["last_api_success_at"],
            status["last_message_ingested_at"], status["last_error_code"], status["consecutive_failures"],
            status["body_unavailable_count"], status["history_reset_count"], status["message_count"], status["thread_count"],
            status["oldest_received_at"], status["newest_received_at"], status["watch_state"],
            status["watch_expiration_ms"], status["last_notification_at"],
            _pending_age((status["pending_sync"] or {}).get("requested_at"), datetime.fromisoformat(now)),
            now,
        ),
    )


def _publish_full(authority_con: sqlite3.Connection, destination: Path, signature: str, status: dict) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".gmail-index-", suffix=".db", dir=str(destination.parent))
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        target = sqlite3.connect(temporary)
        try:
            target.executescript(INDEX_SCHEMA)
            target.executemany(
                "INSERT INTO meta(key,value) VALUES(?,?)",
                [("content_signature", signature), ("layout", LAYOUT),
                 ("full_built_at", gm_store.now_iso()), ("incremental_updates", "0")],
            )
            for row in authority_con.execute(
                f"SELECT {MESSAGE_COLUMNS} FROM messages ORDER BY internal_date_ms,message_id"
            ):
                _insert_message(target, row)
            _write_status(target, status)
            target.commit()
            target.execute("PRAGMA journal_mode=DELETE")
        finally:
            target.close()
        if os.name == "nt":
            # Development hosts without unprivileged symlinks replace the file itself.
            _atomic_replace(temporary, destination)
            return
        slot = _inactive_slot(destination)
        # A journal left by an interrupted update belongs to the file being
        # replaced; rolled back into the new file it would corrupt it.
        slot.with_name(slot.name + "-journal").unlink(missing_ok=True)
        _atomic_replace(temporary, slot)
        os.chmod(slot, 0o440)
        _point_link(destination, slot)
    finally:
        if temporary.exists():
            temporary.unlink()


def _apply_changes(authority_con: sqlite3.Connection, target: sqlite3.Connection, signature: str) -> int | None:
    """Bring a copy forward from its own signature; None when that is not possible."""
    meta = dict(target.execute("SELECT key,value FROM meta").fetchall())
    if meta.get("layout") != LAYOUT:
        return None
    try:
        last_id, count = (int(part) for part in str(meta.get("content_signature") or "").split(":"))
    except ValueError:
        return None
    current_id = int(signature.split(":")[0])
    # The change log only grows.  Anything else means this copy's signature
    # no longer describes a prefix of it.
    seen = authority_con.execute("SELECT COUNT(*) FROM change_log WHERE id<=?", (last_id,)).fetchone()[0]
    if last_id > current_id or seen != count:
        return None
    sources = [row[0] for row in authority_con.execute(
        "SELECT DISTINCT source_id FROM change_log WHERE id>? AND id<=?", (last_id, current_id)
    )]
    if len(sources) > MAX_INCREMENTAL_SOURCES:
        return None
    target.row_factory = sqlite3.Row
    columns = [column["name"] for column in target.execute("PRAGMA table_info(gmail_messages)")]
    update = f"UPDATE gmail_messages SET {','.join(f'{name}=?' for name in columns)} WHERE rowid=?"
    for source_id in sources:
        old = target.execute(
            "SELECT rowid AS id,source_id,subject,from_header,to_header,body_text,deleted_at,labels_json,"
            "content_state FROM gmail_messages WHERE source_id=?", (source_id,)
        ).fetchone()
        row = authority_con.execute(
            f"SELECT {MESSAGE_COLUMNS} FROM messages WHERE source_id=?", (source_id,)
        ).fetchone()
        if old is None:
            if row is not None:
                _insert_message(target, row)
            continue
        before = _searchable(old)
        after = _searchable(row) if row is not None else None
        # Read state and labels change far more often than text.  Rewriting
        # an unchanged FTS document would churn its trigram segments for nothing.
        if before != after and before is not None:
            target.execute("DELETE FROM gmail_fts WHERE rowid=?", (old["id"],))
        if row is None:
            target.execute("DELETE FROM gmail_messages WHERE rowid=?", (old["id"],))
            continue
        target.execute(update, _message_values(row) + (old["id"],))
        if before != after:
            _insert_searchable(target, old["id"], after)
    target.execute("UPDATE meta SET value=? WHERE key='content_signature'", (signature,))
    target.execute("UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='incremental_updates'")
    return len(sources)


def _publish_incremental(authority_con: sqlite3.Connection, destination: Path, signature: str,
                         status: dict) -> int | None:
    """Update the copy readers are not using, then swap to it.

    Returns the number of changed messages, or None when only a full build
    can bring that copy up to date.
    """
    if _active_slot(destination) is None:
        return None
    slot = _inactive_slot(destination)
    if not slot.exists():
        return None
    os.chmod(slot, 0o640)
    target = None
    try:
        # The first read of a read-write connection rolls back a journal left
        # by an interrupted update.
        target = sqlite3.connect(slot, timeout=30, isolation_level=None)
        target.execute("BEGIN IMMEDIATE")
        changed = _apply_changes(authority_con, target, signature)
        if changed is None:
            return None
        _write_status(target, status)
        target.execute("COMMIT")
    finally:
        if target is not None:
            if target.in_transaction:
                target.execute("ROLLBACK")
            target.close()
        os.chmod(slot, 0o440)
    _point_link(destination, slot)
    return changed


def _compaction_due(destination: Path, now: datetime) -> bool:
    """Rebuild the copy readers are not using once a night if updates fragmented it."""
    if os.name == "nt" or now.hour != COMPACTION_HOUR or _active_slot(destination) is None:
        return False
    meta = _read_meta(_inactive_slot(destination))
    if meta is None:
        return False
    try:
        updates = int(meta.get("incremental_updates") or 0)
        age = now - datetime.fromisoformat(str(meta["full_built_at"]))
    except (KeyError, ValueError, TypeError):
        return True
    return updates > 0 and age > timedelta(hours=12)


def publish_index(authority_con: sqlite3.Connection, destination: str | Path, *,
                  now: datetime | None = None) -> bool:
    """Publish the index; readers never see a partial index or wait on a writer."""
    destination = Path(destination)
    status = gm_store.status_dict(authority_con)
    status_path = destination.with_name("status.json")
    signature = _content_signature(authority_con)
    published = _published_signature(destination)
    compact = _compaction_due(destination, now or datetime.now().astimezone())
    if not compact and (signature == published or (published is None and _legacy_snapshot_matches(authority_con, destination))):
        _atomic_json(status_path, status)
        return False
    started = time.monotonic()
    changed = None
    if not compact and os.name != "nt":
        try:
            changed = _publish_incremental(authority_con, destination, signature, status)
        except (sqlite3.Error, OSError) as exc:
            # The full build below never touches the copy readers are using.
            log.warning("incremental index update failed: %s; rebuilding", type(exc).__name__)
    if changed is None:
        _publish_full(authority_con, destination, signature, status)
    _atomic_json(status_path, status)
    log.info(
        "index published mode=%s changed=%s seconds=%.1f",
        "full" if changed is None else "incremental", "-" if changed is None else changed,
        time.monotonic() - started,
    )
    return True
