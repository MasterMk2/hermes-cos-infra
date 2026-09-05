#!/usr/bin/env python3
"""Transactional Gmail evidence-store operations."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone


MAILBOX_KEY = "me"
MESSAGE_COLUMNS = (
    "source_id", "message_id", "thread_id", "history_id", "internal_date_ms",
    "received_at", "from_header", "to_header", "cc_header", "subject",
    "labels_json", "snippet", "body_text", "commitment_text", "content_state", "mime_json",
    "attachments_json", "is_sent", "deleted_at", "ingested_at", "updated_at",
    "revision", "baseline", "content_hash",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _insert(con: sqlite3.Connection, table: str, row: dict, *, replace: bool = False) -> None:
    cols = list(MESSAGE_COLUMNS)
    placeholders = ",".join("?" for _ in cols)
    verb = "INSERT OR REPLACE" if replace else "INSERT"
    con.execute(
        f"{verb} INTO {table} ({','.join(cols)}) VALUES({placeholders})",
        [row.get(key) for key in cols],
    )


def ensure_state(con: sqlite3.Connection) -> None:
    con.execute(
        "INSERT OR IGNORE INTO sync_state(mailbox_key) VALUES(?)", (MAILBOX_KEY,)
    )
    con.execute(
        "INSERT OR IGNORE INTO watch_state(mailbox_key) VALUES(?)", (MAILBOX_KEY,)
    )


def state(con: sqlite3.Connection):
    ensure_state(con)
    return con.execute("SELECT * FROM sync_state WHERE mailbox_key=?", (MAILBOX_KEY,)).fetchone()


def watch(con: sqlite3.Connection):
    ensure_state(con)
    return con.execute("SELECT * FROM watch_state WHERE mailbox_key=?", (MAILBOX_KEY,)).fetchone()


def set_auth_state(con: sqlite3.Connection, value: str) -> None:
    ensure_state(con)
    con.execute(
        "UPDATE sync_state SET auth_state=?, last_error_code=NULL, last_error_at=NULL,"
        " consecutive_failures=0 WHERE mailbox_key=?",
        (value, MAILBOX_KEY),
    )


def record_failure(con: sqlite3.Connection, code: str, *, auth_required: bool = False) -> None:
    ensure_state(con)
    at = now_iso()
    con.execute(
        "UPDATE sync_state SET auth_state=?,last_error_code=?,last_error_at=?,"
        "consecutive_failures=consecutive_failures+1 WHERE mailbox_key=?",
        ("AUTH_REQUIRED" if auth_required else "DEGRADED", str(code)[:120], at, MAILBOX_KEY),
    )


def record_api_success(con: sqlite3.Connection, *, mailbox_email: str | None = None) -> None:
    ensure_state(con)
    con.execute(
        "UPDATE sync_state SET last_api_success_at=?, mailbox_email=COALESCE(?,mailbox_email)"
        " WHERE mailbox_key=?",
        (now_iso(), mailbox_email, MAILBOX_KEY),
    )


def enqueue_sync(con: sqlite3.Connection, reason: str, *, at: str | None = None,
                 force: bool = False) -> bool:
    ensure_state(con)
    at = at or now_iso()
    existing = con.execute(
        "SELECT pending_count FROM sync_queue WHERE mailbox_key=?", (MAILBOX_KEY,)
    ).fetchone()
    if existing is None:
        con.execute(
            "INSERT INTO sync_queue(mailbox_key,requested_at,reason) VALUES(?,?,?)",
            (MAILBOX_KEY, at, str(reason)[:80]),
        )
        return True
    if force:
        con.execute(
            "UPDATE sync_queue SET pending_count=pending_count+1,requested_at=?,reason=?,"
            "not_before=NULL,last_error_code=NULL WHERE mailbox_key=?",
            (at, str(reason)[:80], MAILBOX_KEY),
        )
    else:
        con.execute(
            "UPDATE sync_queue SET pending_count=pending_count+1,requested_at=?,reason=?"
            " WHERE mailbox_key=?",
            (at, str(reason)[:80], MAILBOX_KEY),
        )
    return False


def claim_queue(con: sqlite3.Connection, *, at: str | None = None):
    at = at or now_iso()
    row = con.execute(
        "SELECT * FROM sync_queue WHERE claimed_at IS NULL AND "
        "(not_before IS NULL OR not_before<=?) ORDER BY requested_at LIMIT 1",
        (at,),
    ).fetchone()
    if row is None:
        return None
    con.execute("UPDATE sync_queue SET claimed_at=? WHERE mailbox_key=?", (at, row["mailbox_key"]))
    return row


def complete_queue(con: sqlite3.Connection) -> None:
    con.execute("DELETE FROM sync_queue WHERE mailbox_key=?", (MAILBOX_KEY,))


def retry_queue(con: sqlite3.Connection, code: str, delay_seconds: int) -> None:
    retry_at = (datetime.now(timezone.utc) + timedelta(seconds=max(1, int(delay_seconds)))).isoformat()
    con.execute(
        "UPDATE sync_queue SET claimed_at=NULL,attempts=attempts+1,not_before=?,last_error_code=?"
        " WHERE mailbox_key=?",
        (retry_at, str(code)[:120], MAILBOX_KEY),
    )


def recover_claim(con: sqlite3.Connection) -> int:
    row = con.execute(
        "UPDATE sync_queue SET claimed_at=NULL WHERE mailbox_key=? AND claimed_at IS NOT NULL",
        (MAILBOX_KEY,),
    )
    return int(row.rowcount or 0)


def recover_orphan_locks(con: sqlite3.Connection) -> int:
    """Release locks left only by a process that has already exited.

    This is called exclusively during the systemd unit's serialized startup,
    after systemd has stopped any earlier instance of that same unit.  It is
    intentionally not used by ordinary workers, so a live sync lock remains
    exclusive during push/poll coalescing.
    """
    row = con.execute("DELETE FROM sync_locks WHERE mailbox_key=?", (MAILBOX_KEY,))
    return int(row.rowcount or 0)


def recover_orphan_staging(con: sqlite3.Connection) -> int:
    """Discard only unpublished full-sync work left by a terminated worker.

    The canonical ``messages`` table and its published retrieval snapshot are
    never touched here.  This startup-only cleanup bounds disk use after an
    interrupted initial sync while preserving the old published generation.
    """
    row = con.execute("DELETE FROM resync_staging")
    return int(row.rowcount or 0)


def acquire_lock(con: sqlite3.Connection, owner_id: str) -> bool:
    try:
        con.execute(
            "INSERT INTO sync_locks(mailbox_key,owner_id,locked_at) VALUES(?,?,?)",
            (MAILBOX_KEY, owner_id, now_iso()),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def release_lock(con: sqlite3.Connection, owner_id: str) -> None:
    con.execute(
        "DELETE FROM sync_locks WHERE mailbox_key=? AND owner_id=?",
        (MAILBOX_KEY, owner_id),
    )


def _refresh_thread(con: sqlite3.Connection, thread_id: str) -> None:
    row = con.execute(
        "SELECT message_id,internal_date_ms FROM messages WHERE thread_id=? AND deleted_at IS NULL"
        " ORDER BY internal_date_ms DESC,message_id DESC LIMIT 1",
        (thread_id,),
    ).fetchone()
    count = con.execute(
        "SELECT COUNT(*) FROM messages WHERE thread_id=? AND deleted_at IS NULL", (thread_id,)
    ).fetchone()[0]
    con.execute(
        """INSERT INTO threads(thread_id,latest_message_id,latest_internal_date_ms,message_count,updated_at)
           VALUES(?,?,?,?,?)
           ON CONFLICT(thread_id) DO UPDATE SET latest_message_id=excluded.latest_message_id,
             latest_internal_date_ms=excluded.latest_internal_date_ms,message_count=excluded.message_count,
             updated_at=excluded.updated_at""",
        (thread_id, row["message_id"] if row else None, row["internal_date_ms"] if row else None, count, now_iso()),
    )


def _change(con: sqlite3.Connection, row: dict, kind: str, previous_hash: str | None,
            *, baseline: bool) -> None:
    con.execute(
        "INSERT INTO change_log(source_id,change_type,occurred_at,previous_hash,current_hash,baseline)"
        " VALUES(?,?,?,?,?,?)",
        (row["source_id"], kind, row["ingested_at"], previous_hash, row["content_hash"], int(baseline)),
    )


def upsert_message(con: sqlite3.Connection, row: dict, *, baseline: bool = False) -> str:
    existing = con.execute("SELECT * FROM messages WHERE source_id=?", (row["source_id"],)).fetchone()
    if existing is None:
        row = dict(row)
        row["revision"] = 1
        row["baseline"] = int(bool(baseline))
        _insert(con, "messages", row)
        _refresh_thread(con, row["thread_id"])
        _change(con, row, "created", None, baseline=baseline)
        return "created"
    meaningful_changed = str(existing["content_hash"]) != str(row["content_hash"]) or bool(existing["deleted_at"])
    if not meaningful_changed and str(existing["history_id"] or "") == str(row["history_id"] or ""):
        return "unchanged"
    row = dict(row)
    row["revision"] = int(existing["revision"] or 0) + 1
    row["baseline"] = int(bool(baseline))
    _insert(con, "messages", row, replace=True)
    _refresh_thread(con, row["thread_id"])
    _change(con, row, "updated" if meaningful_changed else "metadata", existing["content_hash"], baseline=baseline)
    return "updated" if meaningful_changed else "metadata"


def tombstone_message(con: sqlite3.Connection, message_id: str, *, at: str | None = None,
                      baseline: bool = False) -> bool:
    row = con.execute("SELECT * FROM messages WHERE message_id=?", (message_id,)).fetchone()
    if row is None or row["deleted_at"]:
        return False
    at = at or now_iso()
    con.execute(
        "UPDATE messages SET deleted_at=?,body_text='',commitment_text='',snippet='',"
        " attachments_json='[]',updated_at=?,revision=revision+1 WHERE message_id=?",
        (at, at, message_id),
    )
    changed = dict(row)
    changed.update({"ingested_at": at, "content_hash": "", "source_id": row["source_id"]})
    _change(con, changed, "deleted", row["content_hash"], baseline=baseline)
    _refresh_thread(con, row["thread_id"])
    return True


def message_thread_id(con: sqlite3.Connection, message_id: str) -> str | None:
    row = con.execute("SELECT thread_id FROM messages WHERE message_id=?", (message_id,)).fetchone()
    return str(row[0]) if row and row[0] else None


def has_available_content(con: sqlite3.Connection) -> bool:
    return bool(con.execute(
        "SELECT 1 FROM messages WHERE deleted_at IS NULL AND content_state='available' LIMIT 1"
    ).fetchone())


def stage_message(con: sqlite3.Connection, generation: str, row: dict) -> None:
    cols = ("generation",) + MESSAGE_COLUMNS
    con.execute(
        f"INSERT OR REPLACE INTO resync_staging ({','.join(cols)}) "
        f"VALUES({','.join('?' for _ in cols)})",
        [generation] + [row.get(key) for key in MESSAGE_COLUMNS],
    )


def _unstage(row: sqlite3.Row, generation: str) -> dict:
    del generation
    return dict(row)


def discard_staging(con: sqlite3.Connection, generation: str) -> None:
    con.execute("DELETE FROM resync_staging WHERE generation=?", (generation,))


def publish_staging(con: sqlite3.Connection, generation: str, *, range_start_ms: int,
                    history_id: str, reset_history: bool) -> dict:
    rows = con.execute(
        "SELECT * FROM resync_staging WHERE generation=? ORDER BY internal_date_ms,message_id",
        (generation,),
    ).fetchall()
    counts = {"created": 0, "updated": 0, "metadata": 0, "deleted": 0}
    staged_ids = []
    for raw in rows:
        row = _unstage(raw, generation)
        staged_ids.append(row["message_id"])
        kind = upsert_message(con, row, baseline=True)
        if kind in counts:
            counts[kind] += 1
    # Only the bounded window is reconciled.  Older local evidence is never
    # discarded merely because Gmail history has expired.
    existing = con.execute(
        "SELECT message_id FROM messages WHERE internal_date_ms>=? AND deleted_at IS NULL",
        (int(range_start_ms),),
    ).fetchall()
    staged_set = set(staged_ids)
    for item in existing:
        if item["message_id"] not in staged_set and tombstone_message(con, item["message_id"], baseline=True):
            counts["deleted"] += 1
    now = now_iso()
    ensure_state(con)
    con.execute(
        """UPDATE sync_state SET history_id=?,initialized_at=COALESCE(initialized_at,?),
           last_full_sync_at=?,last_incremental_sync_at=NULL,last_api_success_at=?,
           last_error_code=NULL,last_error_at=NULL,consecutive_failures=0,
           history_reset_count=history_reset_count+?,messages_fetched=?,
           messages_changed=?,messages_deleted=?,sync_duration_ms=NULL,
           last_message_ingested_at=CASE WHEN ? > 0 THEN ? ELSE last_message_ingested_at END
           WHERE mailbox_key=?""",
        (history_id, now, now, now, int(reset_history), len(rows),
         counts["created"] + counts["updated"], counts["deleted"], len(rows), now, MAILBOX_KEY),
    )
    discard_staging(con, generation)
    return counts


def finish_incremental(con: sqlite3.Connection, *, history_id: str, fetched: int,
                       changed: int, deleted: int, duration_ms: int) -> None:
    now = now_iso()
    ensure_state(con)
    con.execute(
        """UPDATE sync_state SET history_id=?,auth_state='AUTHORIZED',
           last_incremental_sync_at=?,last_api_success_at=?,last_error_code=NULL,
           last_error_at=NULL,consecutive_failures=0,messages_fetched=?,
           messages_changed=?,messages_deleted=?,sync_duration_ms=?,
           last_message_ingested_at=CASE WHEN ? > 0 THEN ? ELSE last_message_ingested_at END
           WHERE mailbox_key=?""",
        (history_id, now, now, fetched, changed, deleted, duration_ms, fetched, now, MAILBOX_KEY),
    )
    con.execute(
        "UPDATE watch_state SET last_successful_sync_at=? WHERE mailbox_key=?",
        (now, MAILBOX_KEY),
    )


def set_watch(con: sqlite3.Connection, *, topic_name: str, expiration_ms: int | None,
              state: str = "ACTIVE") -> None:
    now = now_iso()
    ensure_state(con)
    con.execute(
        """UPDATE watch_state SET topic_name=?,expiration_ms=?,state=?,created_at=COALESCE(created_at,?),
           renewed_at=?,last_error_code=NULL,last_error_at=NULL WHERE mailbox_key=?""",
        (topic_name, expiration_ms, state, now, now, MAILBOX_KEY),
    )


def record_watch_failure(con: sqlite3.Connection, code: str) -> None:
    ensure_state(con)
    con.execute(
        "UPDATE watch_state SET state='DEGRADED',last_error_code=?,last_error_at=? WHERE mailbox_key=?",
        (str(code)[:120], now_iso(), MAILBOX_KEY),
    )


def accept_notification(con: sqlite3.Connection, *, message_id: str, history_id: str) -> bool:
    try:
        con.execute(
            "INSERT INTO notification_ledger(pubsub_message_id,notification_history_id,received_at)"
            " VALUES(?,?,?)",
            (message_id, history_id, now_iso()),
        )
    except sqlite3.IntegrityError:
        return False
    con.execute(
        "UPDATE watch_state SET last_notification_at=?,last_notification_history_id=? WHERE mailbox_key=?",
        (now_iso(), history_id, MAILBOX_KEY),
    )
    return True


def increment_metric(con: sqlite3.Connection, key: str, amount: int = 1) -> None:
    con.execute(
        """INSERT INTO metrics(key,value,updated_at) VALUES(?,?,?)
           ON CONFLICT(key) DO UPDATE SET value=value+excluded.value,updated_at=excluded.updated_at""",
        (str(key), int(amount), now_iso()),
    )


def status_dict(con: sqlite3.Connection) -> dict:
    current = state(con)
    current_watch = watch(con)
    count = con.execute("SELECT COUNT(*) FROM messages WHERE deleted_at IS NULL").fetchone()[0]
    unavailable_count = con.execute(
        "SELECT COUNT(*) FROM messages WHERE deleted_at IS NULL AND content_state!='available'"
    ).fetchone()[0]
    thread_count = con.execute("SELECT COUNT(*) FROM threads WHERE message_count>0").fetchone()[0]
    oldest_newest = con.execute(
        "SELECT MIN(received_at),MAX(received_at) FROM messages WHERE deleted_at IS NULL"
    ).fetchone()
    pending = con.execute(
        "SELECT requested_at,pending_count,claimed_at FROM sync_queue WHERE mailbox_key=?", (MAILBOX_KEY,)
    ).fetchone()
    return {
        "auth_state": current["auth_state"],
        "history_id_present": bool(current["history_id"]),
        "initialized_at": current["initialized_at"],
        "last_full_sync_at": current["last_full_sync_at"],
        "last_incremental_sync_at": current["last_incremental_sync_at"],
        "last_api_success_at": current["last_api_success_at"],
        "last_message_ingested_at": current["last_message_ingested_at"],
        "last_error_code": current["last_error_code"],
        "consecutive_failures": int(current["consecutive_failures"] or 0),
        "history_reset_count": int(current["history_reset_count"] or 0),
        "message_count": int(count),
        "body_unavailable_count": int(unavailable_count),
        "thread_count": int(thread_count),
        "oldest_received_at": oldest_newest[0],
        "newest_received_at": oldest_newest[1],
        "watch_state": current_watch["state"],
        "watch_expiration_ms": current_watch["expiration_ms"],
        "last_notification_at": current_watch["last_notification_at"],
        "pending_sync": dict(pending) if pending else None,
    }
