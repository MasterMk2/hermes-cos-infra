#!/usr/bin/env python3
"""Discord Knowledge — source-database write layer.

Every write the collector performs goes through here, so the ordering and
tombstone rules live in exactly one place:

* **Upsert, never duplicate.** `source_id` is the primary key, so the same
  message arriving from the gateway and again from backfill collapses into
  one row (spec section 49).

* **Newer wins, and only newer.** A payload is applied only when its edit
  generation is not older than the stored one, so a slow backfill page
  cannot overwrite an edit the gateway already delivered (sections 38, 105).

* **Deleted stays deleted.** Once `deleted_at` is set, no later payload
  resurrects the body (sections 39, 40).

* **Index hand-off is transactional.** The `index_queue` row is written in
  the same transaction as the message, so a crash can leave the index behind
  but never leaves a change unrecorded (section 55).

No content is ever logged from this module.
"""

from __future__ import annotations

import json
import sqlite3

from dk_normalize import now_utc_iso, search_blob

# Columns a message payload may set. Identity columns are excluded: they are
# part of the primary key or come from the collector's channel registry.
MUTABLE_COLUMNS = (
    "guild_name",
    "channel_name",
    "channel_type",
    "thread_id",
    "thread_name",
    "parent_channel_id",
    "author_id",
    "author_name",
    "author_global_name",
    "author_is_bot",
    "webhook_id",
    "created_at",
    "created_ts",
    "edited_at",
    "content",
    "content_normalized",
    "search_text",
    "content_hash",
    "message_type",
    "reply_to_message_id",
    "attachments_json",
    "embeds_json",
    "ingest_source",
)

INSERT_COLUMNS = ("source_id", "message_id", "guild_id", "channel_id") + MUTABLE_COLUMNS

# Columns supplied by the collector's own registry rather than by the payload.
# A None here means "the collector does not know", never "Discord cleared it",
# so a None must never erase a value already stored.
REGISTRY_COLUMNS = frozenset(
    {
        "guild_name",
        "channel_name",
        "channel_type",
        "thread_id",
        "thread_name",
        "parent_channel_id",
        "created_at",
        "created_ts",
    }
)


# Discord never clears the author of an existing message, so a None here can
# only mean "this payload did not carry it". Backstop for the case where a
# payload is mistakenly treated as complete.
NEVER_CLEARED_COLUMNS = frozenset({"author_id", "author_name"})


def _row_to_dict(row) -> dict:
    return {k: row[k] for k in row.keys()}


# ---------------------------------------------------------------- registry


def upsert_guild(con: sqlite3.Connection, guild_id, guild_name=None) -> None:
    now = now_utc_iso()
    con.execute(
        "INSERT INTO guilds (guild_id, guild_name, first_seen_at, updated_at) "
        "VALUES (?,?,?,?) "
        "ON CONFLICT(guild_id) DO UPDATE SET "
        "  guild_name = COALESCE(excluded.guild_name, guilds.guild_name), "
        "  updated_at = excluded.updated_at",
        (str(guild_id), guild_name, now, now),
    )


def upsert_channel(
    con: sqlite3.Connection,
    channel_id,
    guild_id,
    *,
    channel_name=None,
    channel_type=None,
    parent_channel_id=None,
    is_thread=False,
    readable=None,
    last_error=None,
) -> None:
    now = now_utc_iso()
    con.execute(
        "INSERT INTO channels (channel_id, guild_id, channel_name, channel_type, "
        "  parent_channel_id, is_thread, readable, last_error, first_seen_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(channel_id) DO UPDATE SET "
        "  guild_id = excluded.guild_id, "
        "  channel_name = COALESCE(excluded.channel_name, channels.channel_name), "
        "  channel_type = COALESCE(excluded.channel_type, channels.channel_type), "
        "  parent_channel_id = COALESCE(excluded.parent_channel_id, channels.parent_channel_id), "
        "  is_thread = excluded.is_thread, "
        "  readable = COALESCE(excluded.readable, channels.readable), "
        "  last_error = excluded.last_error, "
        "  updated_at = excluded.updated_at",
        (
            str(channel_id),
            str(guild_id),
            channel_name,
            channel_type,
            str(parent_channel_id) if parent_channel_id else None,
            1 if is_thread else 0,
            None if readable is None else (1 if readable else 0),
            last_error,
            now,
            now,
        ),
    )


def known_channels(con: sqlite3.Connection) -> list:
    """Every channel the collector has ever registered.

    Used to rebuild the in-memory parent map at startup. Without it a thread's
    parent is unknown until a fresh message happens to arrive in that thread,
    and until then an edit or a bulk delete inside the thread fails the
    allowlist check and is dropped — silently, and for exactly the historical
    content backfill just went to the trouble of collecting.
    """
    return [
        {
            "channel_id": r["channel_id"],
            "guild_id": r["guild_id"],
            "channel_name": r["channel_name"],
            "channel_type": r["channel_type"],
            "parent_channel_id": r["parent_channel_id"],
            "is_thread": bool(r["is_thread"]),
        }
        for r in con.execute("SELECT * FROM channels")
    ]


def newest_message_ids(con: sqlite3.Connection) -> dict:
    """channel_id -> the newest message id stored for it.

    The starting point for a catch-up pass: anything posted while the
    collector was down is newer than this and the gateway will never resend
    it, so only an explicit forward fetch can recover it.
    """
    return {
        r["channel_id"]: r["message_id"]
        for r in con.execute(
            "SELECT channel_id, message_id FROM messages "
            "WHERE created_ts = (SELECT MAX(created_ts) FROM messages m2 "
            "                    WHERE m2.channel_id = messages.channel_id) "
            "GROUP BY channel_id"
        )
    }


def mark_channel_unreadable(con: sqlite3.Connection, channel_id, guild_id, reason) -> None:
    """A channel the bot cannot read is recorded per channel; the guild-wide
    collector keeps running (spec section 94)."""
    upsert_channel(
        con,
        channel_id,
        guild_id,
        readable=False,
        last_error=str(reason)[:300],
    )


# ------------------------------------------------------------------ queue


def enqueue_index(con: sqlite3.Connection, source_id: str, op: str, revision=None) -> None:
    con.execute(
        "INSERT INTO index_queue (source_id, op, revision, enqueued_at) VALUES (?,?,?,?)",
        (source_id, op, revision, now_utc_iso()),
    )


def queue_depth(con: sqlite3.Connection) -> int:
    return int(con.execute("SELECT COUNT(*) FROM index_queue").fetchone()[0])


def queue_oldest_enqueued_at(con: sqlite3.Connection):
    row = con.execute(
        "SELECT enqueued_at FROM index_queue ORDER BY id LIMIT 1"
    ).fetchone()
    return row[0] if row else None


# --------------------------------------------------------------- messages


def get_message(con: sqlite3.Connection, source_id: str):
    return con.execute(
        "SELECT * FROM messages WHERE source_id = ?", (source_id,)
    ).fetchone()


def _generation(value) -> str:
    return value or ""


def apply_message(
    con: sqlite3.Connection,
    rec: dict,
    *,
    partial: bool = False,
    ingest_source: str = "gateway",
) -> str:
    """Insert or update one canonical record.

    Returns one of: 'inserted' | 'updated' | 'unchanged' | 'skipped_stale' |
    'skipped_deleted'. The caller uses the return value for metrics only —
    every branch leaves the database consistent.
    """
    source_id = rec["source_id"]
    existing = get_message(con, source_id)
    now = now_utc_iso()

    if existing is None:
        if partial:
            # A partial update for a message we never stored: keep what the
            # payload carried rather than inventing fields. The collector
            # will have tried a REST fetch first; this is the last resort.
            partial = False
        row = {c: rec.get(c) for c in INSERT_COLUMNS}
        row["source_id"] = source_id
        row["ingest_source"] = rec.get("ingest_source") or ingest_source
        if row.get("search_text") is None:
            row["search_text"] = search_blob(rec)
        cols = list(INSERT_COLUMNS) + ["deleted_at", "revision", "ingested_at", "updated_at"]
        values = [row.get(c) for c in INSERT_COLUMNS] + [rec.get("deleted_at"), 0, now, now]
        con.execute(
            f"INSERT INTO messages ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' for _ in cols)})",
            values,
        )
        enqueue_index(con, source_id, "delete" if rec.get("deleted_at") else "upsert", 0)
        return "inserted"

    if existing["deleted_at"]:
        # Tombstones are terminal for content. Nothing re-opens them.
        return "skipped_deleted"

    incoming_gen = _generation(rec.get("edited_at") if ("edited_at" in rec) else existing["edited_at"])
    stored_gen = _generation(existing["edited_at"])
    if incoming_gen < stored_gen:
        return "skipped_stale"

    def _keeps(col):
        if col not in rec:
            return False
        if rec[col] is not None or existing[col] is None:
            return True
        # rec[col] is None while something is stored: only a full payload may
        # clear a payload-derived field, and registry / never-cleared fields
        # never may.
        if col in REGISTRY_COLUMNS or col in NEVER_CLEARED_COLUMNS:
            return False
        return not partial

    updates = {c: rec[c] for c in MUTABLE_COLUMNS if _keeps(c)}
    if partial:
        updates.pop("ingest_source", None)
    else:
        updates["ingest_source"] = rec.get("ingest_source") or ingest_source

    if "content" in updates or "content_normalized" in updates or "embeds_json" in updates:
        merged = _row_to_dict(existing)
        merged.update(updates)
        updates["search_text"] = search_blob(merged)

    changed = {k: v for k, v in updates.items() if existing[k] != v}
    if not changed:
        return "unchanged"

    revision = int(existing["revision"] or 0) + 1
    changed["revision"] = revision
    changed["updated_at"] = now
    cols = ", ".join(f"{c} = ?" for c in changed)
    con.execute(
        f"UPDATE messages SET {cols} WHERE source_id = ?",
        list(changed.values()) + [source_id],
    )
    enqueue_index(con, source_id, "upsert", revision)
    return "updated"


def apply_delete(
    con: sqlite3.Connection, guild_id, channel_id, message_id, *, deleted_at=None
) -> str:
    """Record a deletion as a tombstone.

    The row keeps its identity and timing so provenance survives, but the
    body is dropped here and the indexer removes it from the search index.
    A delete for a message we never ingested still leaves a tombstone, so a
    later out-of-order backfill page cannot resurrect it.
    """
    from dk_normalize import tombstone_record

    stamp = deleted_at or now_utc_iso()
    rec = tombstone_record(guild_id, channel_id, message_id, deleted_at=stamp)
    source_id = rec["source_id"]
    existing = get_message(con, source_id)
    if existing is None:
        con.execute(
            "INSERT INTO messages (source_id, message_id, guild_id, channel_id, "
            "  created_at, created_ts, deleted_at, revision, ingest_source, "
            "  ingested_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                source_id,
                rec["message_id"],
                rec["guild_id"],
                rec["channel_id"],
                rec["created_at"],
                rec["created_ts"],
                stamp,
                0,
                "gateway",
                stamp,
                stamp,
            ),
        )
        enqueue_index(con, source_id, "delete", 0)
        return "tombstoned_unknown"

    if existing["deleted_at"]:
        return "already_deleted"

    revision = int(existing["revision"] or 0) + 1
    # content_hash goes too: it is a sha256 of the body Discord just removed,
    # and keeping it leaves a fingerprint of deleted content that could confirm
    # a guess at what was said. Its only purpose is edit detection on a live
    # message, and a tombstone is never re-evaluated.
    con.execute(
        "UPDATE messages SET deleted_at = ?, content = NULL, content_normalized = NULL, "
        "  search_text = NULL, content_hash = NULL, attachments_json = NULL, "
        "  embeds_json = NULL, revision = ?, updated_at = ? WHERE source_id = ?",
        (stamp, revision, stamp, source_id),
    )
    enqueue_index(con, source_id, "delete", revision)
    return "tombstoned"


def apply_bulk_delete(con: sqlite3.Connection, guild_id, channel_id, message_ids) -> dict:
    """MESSAGE_DELETE_BULK follows exactly the same tombstone policy."""
    counts = {"tombstoned": 0, "tombstoned_unknown": 0, "already_deleted": 0}
    stamp = now_utc_iso()
    for mid in message_ids or []:
        result = apply_delete(con, guild_id, channel_id, mid, deleted_at=stamp)
        counts[result] = counts.get(result, 0) + 1
    return counts


# ------------------------------------------------------- state and metrics


def set_ingest_state(con: sqlite3.Connection, key: str, value) -> None:
    con.execute(
        "INSERT OR REPLACE INTO ingest_state (key, value) VALUES (?,?)",
        (key, "" if value is None else str(value)),
    )


def get_ingest_state(con: sqlite3.Connection, key: str, default=None):
    row = con.execute("SELECT value FROM ingest_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_ingest_states(con: sqlite3.Connection, mapping: dict) -> None:
    for k, v in mapping.items():
        set_ingest_state(con, k, v)


def bump_metric(con: sqlite3.Connection, key: str, delta: int = 1) -> None:
    con.execute(
        "INSERT INTO metrics (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(metrics.value AS INTEGER) + ? AS TEXT)",
        (key, str(delta), delta),
    )


def metrics_snapshot(con: sqlite3.Connection) -> dict:
    return {r[0]: r[1] for r in con.execute("SELECT key, value FROM metrics")}


def counts_snapshot(con: sqlite3.Connection) -> dict:
    """Content-free counters used by status reporting and the smoke tests.

    `with_content` exists to catch the MESSAGE_CONTENT intent failure mode:
    without that privileged intent Discord delivers every message with an
    empty body, which must be reported as broken rather than as success
    (spec section 8).
    """
    live = con.execute(
        "SELECT COUNT(*) FROM messages WHERE deleted_at IS NULL"
    ).fetchone()[0]
    deleted = con.execute(
        "SELECT COUNT(*) FROM messages WHERE deleted_at IS NOT NULL"
    ).fetchone()[0]
    with_content = con.execute(
        "SELECT COUNT(*) FROM messages WHERE deleted_at IS NULL "
        "AND content IS NOT NULL AND content <> ''"
    ).fetchone()[0]
    oldest, newest = con.execute(
        "SELECT MIN(created_at), MAX(created_at) FROM messages WHERE deleted_at IS NULL"
    ).fetchone()
    per_guild = [
        {"guild_id": r[0], "guild_name": r[1], "count": r[2]}
        for r in con.execute(
            "SELECT guild_id, MAX(guild_name), COUNT(*) FROM messages "
            "WHERE deleted_at IS NULL GROUP BY guild_id ORDER BY guild_id"
        )
    ]
    per_channel = [
        {"channel_id": r[0], "channel_name": r[1], "count": r[2]}
        for r in con.execute(
            "SELECT channel_id, MAX(channel_name), COUNT(*) FROM messages "
            "WHERE deleted_at IS NULL GROUP BY channel_id ORDER BY COUNT(*) DESC"
        )
    ]
    return {
        "live": live,
        "deleted": deleted,
        "with_content": with_content,
        "oldest_message_at": oldest,
        "newest_message_at": newest,
        "per_guild": per_guild,
        "per_channel": per_channel,
        "queue_depth": queue_depth(con),
    }


# ------------------------------------------------------------- backfill


def get_backfill_state(con: sqlite3.Connection, channel_id):
    return con.execute(
        "SELECT * FROM backfill_state WHERE channel_id = ?", (str(channel_id),)
    ).fetchone()


def save_backfill_state(
    con: sqlite3.Connection,
    channel_id,
    guild_id,
    *,
    last_cursor=None,
    oldest_message_id=None,
    oldest_created_ts=None,
    newest_message_id=None,
    newest_created_ts=None,
    complete=None,
    added=0,
    last_error=None,
) -> None:
    """Persist resumable progress for one channel (spec section 44).

    Cursors move strictly backwards through history, so a restart continues
    from the oldest message already fetched instead of starting over.
    """
    now = now_utc_iso()
    existing = get_backfill_state(con, channel_id)
    if existing is None:
        con.execute(
            "INSERT INTO backfill_state (channel_id, guild_id, last_cursor, "
            "  oldest_message_id, oldest_created_ts, newest_message_id, "
            "  newest_created_ts, complete, message_count, last_error, "
            "  started_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(channel_id),
                str(guild_id),
                last_cursor,
                oldest_message_id,
                oldest_created_ts,
                newest_message_id,
                newest_created_ts,
                1 if complete else 0,
                int(added or 0),
                last_error,
                now,
                now,
            ),
        )
        return

    fields = {"updated_at": now, "guild_id": str(guild_id)}
    if last_cursor is not None:
        fields["last_cursor"] = last_cursor
    if oldest_message_id is not None:
        fields["oldest_message_id"] = oldest_message_id
    if oldest_created_ts is not None:
        prev = existing["oldest_created_ts"]
        fields["oldest_created_ts"] = (
            oldest_created_ts if prev is None else min(int(prev), int(oldest_created_ts))
        )
    if newest_message_id is not None:
        fields["newest_message_id"] = newest_message_id
    if newest_created_ts is not None:
        prev = existing["newest_created_ts"]
        fields["newest_created_ts"] = (
            newest_created_ts if prev is None else max(int(prev), int(newest_created_ts))
        )
    if complete is not None:
        fields["complete"] = 1 if complete else 0
    if added:
        fields["message_count"] = int(existing["message_count"] or 0) + int(added)
    fields["last_error"] = last_error

    cols = ", ".join(f"{c} = ?" for c in fields)
    con.execute(
        f"UPDATE backfill_state SET {cols} WHERE channel_id = ?",
        list(fields.values()) + [str(channel_id)],
    )


def backfill_summary(con: sqlite3.Connection) -> list:
    return [
        {
            "channel_id": r["channel_id"],
            "guild_id": r["guild_id"],
            "complete": bool(r["complete"]),
            "message_count": r["message_count"],
            "oldest_created_ts": r["oldest_created_ts"],
            "last_error": r["last_error"],
            "updated_at": r["updated_at"],
        }
        for r in con.execute("SELECT * FROM backfill_state ORDER BY channel_id")
    ]


def json_dump(value) -> str:
    return json.dumps(value, ensure_ascii=False)
