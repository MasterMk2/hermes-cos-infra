#!/usr/bin/env python3
"""Discord Knowledge — incremental indexer (source DB -> FTS5 search index).

One direction only:

  source DB   written by the collector. Opened read-write here, because
              draining the hand-off queue means deleting its rows.
  index DB    written by this file and nobody else, read (mode=ro) by the MCP
              server inside the Hermes container. Holds no deleted content.

Crash-safe ordering — the reason `index_queue` exists at all (spec section 55):

  1. read a batch of queue rows from the source, ordered by id;
  2. for each, re-read the CURRENT source row and apply it to the index, the
     whole batch inside ONE index transaction, then COMMIT the index;
  3. only then delete those queue rows and COMMIT the source.

A crash anywhere before step 3 replays the batch, so every apply is
idempotent: INSERT OR REPLACE into `messages`, and an fts row is always
deleted before it is re-inserted. The queue never loses an update; at worst it
re-does one.

The second job in this file exists because the MCP server can see the index DB
and nothing else — not the source DB, not systemd, not the collector. So every
run also publishes a status snapshot into the index `meta` table, which is how
the assistant can say "the collector is disconnected" or "the index is 400
messages behind" instead of quietly answering from a stale corpus. Counts, ids,
timestamps and configured names only: message content never reaches `meta`, and
never reaches the log either.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dk_config import Config, ConfigError, load_config
from dk_schema import open_index_db, open_source_db

DEFAULT_BATCH = 500

# `meta` is a status surface, not a log: bound every free-text value that is
# copied out of the source so a runaway error string cannot bloat the index.
MAX_META_TEXT = 500

# The columns the index copies verbatim from the source row. Everything else
# the source keeps — content_normalized, search_text, content_hash, the
# ingest_* bookkeeping — stays on the collector's side of the boundary.
# is_deleted and indexed_at are the indexer's own.
COPIED_COLUMNS = (
    "source_id",
    "message_id",
    "guild_id",
    "guild_name",
    "channel_id",
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
    "deleted_at",
    "content",
    "message_type",
    "reply_to_message_id",
    "attachments_json",
    "embeds_json",
    "revision",
)

# Read but never copied into an index column: these only feed the fts body.
BODY_COLUMNS = ("search_text", "content_normalized")

INDEX_COLUMNS = COPIED_COLUMNS + ("is_deleted", "indexed_at")

SELECT_SOURCE_MESSAGE = (
    "SELECT "
    + ", ".join(COPIED_COLUMNS + BODY_COLUMNS)
    + " FROM messages WHERE source_id = ?"
)

UPSERT_INDEX_MESSAGE = (
    "INSERT OR REPLACE INTO messages ("
    + ", ".join(INDEX_COLUMNS)
    + ") VALUES ("
    + ", ".join("?" * len(INDEX_COLUMNS))
    + ")"
)

# Used only when the source row is gone entirely: keep the metadata the index
# already carries and blank just what must not survive a delete.
TOMBSTONE_IN_PLACE = (
    "UPDATE messages SET is_deleted = 1, content = NULL, "
    "attachments_json = NULL, embeds_json = NULL, indexed_at = ? "
    "WHERE source_id = ?"
)

DELETE_FTS = "DELETE FROM fts WHERE source_id = ?"
INSERT_FTS = (
    "INSERT INTO fts (source_id, content, channel_name, author_name, guild_name) "
    "VALUES (?, ?, ?, ?, ?)"
)

# Copied verbatim out of the source `ingest_state` key/value table. An
# allowlist, not a dump: this is the only path from the collector's side into
# `meta`, and nothing resembling message content may be added to it.
INGEST_STATE_KEYS = (
    "collector_health",
    "gateway_connected",
    # When the collector last wrote its own state. Without it a dead
    # collector's last "CONNECTED" would be republished forever and read as
    # live; with it the MCP layer can age the health value out.
    "updated_at",
    "last_ready_at",
    "last_event_at",
    "last_successful_ingest_at",
    "last_error_summary",
    "session_resumed_count",
    "full_reconnect_count",
    "last_disconnect_reason",
    "rate_limit_hits",
    "messages_created",
    "messages_edited",
    "messages_deleted",
    "bulk_deletes",
    "backfill_fetched",
)

# `updated_at` is unqualified on the collector's side; in the index `meta`
# table it sits beside the indexer's own timestamps, so it is renamed to say
# whose clock it is.
INGEST_STATE_RENAMES = {"updated_at": "collector_state_updated_at"}


# ---- small helpers ----------------------------------------------------


def _utc_now_iso() -> str:
    """One shape everywhere: UTC ISO-8601, second resolution, trailing Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _jdump(value) -> str:
    """Compact JSON. ensure_ascii=False keeps Japanese guild and channel names
    legible to whoever reads `meta` with the sqlite3 CLI."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _clip(value, limit: int = MAX_META_TEXT) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _rollback(con: sqlite3.Connection) -> None:
    """A failing rollback must never mask the error that caused it."""
    try:
        con.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _close(con: sqlite3.Connection) -> None:
    try:
        con.close()
    except sqlite3.Error:
        pass


def _scalar(con: sqlite3.Connection, sql: str, params=(), default=0):
    row = con.execute(sql, params).fetchone()
    if row is None or row[0] is None:
        return default
    return row[0]


def _fts_body(row: sqlite3.Row) -> str:
    """What the trigram index actually tokenizes.

    `search_text` is the collector's render of the message: the body with
    mentions resolved to names, plus bounded embed text. Indexing that is what
    makes "@alice" and a bot's embed findable at all. Rows written before the
    column existed have it NULL, so fall back to content_normalized and then to
    the raw content.
    """
    for column in ("search_text", "content_normalized", "content"):
        value = row[column]
        if value is not None:
            return str(value)
    return ""


@dataclass
class RunStats:
    """Counters behind the summary line. `applied` counts queue rows drained,
    so it is always upserts + tombstones + skipped."""

    applied: int = 0
    upserts: int = 0
    tombstones: int = 0
    skipped: int = 0
    batches: int = 0


# ---- applying one queue entry -----------------------------------------


def apply_one(
    source: sqlite3.Connection,
    index: sqlite3.Connection,
    source_id: str,
    op: str,
    now: str,
) -> str:
    """Apply the current source state for one source_id; caller owns the
    transaction. Returns 'upsert', 'tombstone' or 'skip'.

    The queue carries no payload — it only names what changed — so the row is
    re-read here and the queued revision is deliberately never trusted. Two
    entries for the same id in one batch therefore converge on the same result
    as one entry.
    """
    src = source.execute(SELECT_SOURCE_MESSAGE, (source_id,)).fetchone()

    # This probe has to run BEFORE the upsert below, while it can still tell
    # "already indexed" from "new". It is what licenses skipping the fts DELETE
    # for a genuinely new row: an fts row is only ever written in the same
    # transaction as its `messages` row, and this file never deletes a
    # `messages` row, so "no message row" implies "no fts row".
    #
    # Without the skip, every insert of a backfill would scan the whole fts
    # content — its source_id column is UNINDEXED, so there is nothing else to
    # look it up by — and the scan grows with the corpus. Measured on the Pi at
    # a 5000-message index: 5000 fresh rows in 1.3 s with the skip, 11.5 ms per
    # row without it. Getting the order of these two statements wrong only ever
    # costs that scan; it cannot duplicate a row.
    known = (
        index.execute(
            "SELECT 1 FROM messages WHERE source_id = ?", (source_id,)
        ).fetchone()
        is not None
    )

    if src is None:
        # The source row is gone entirely (hard-deleted, or it never was in
        # scope). Preserve whatever metadata the index already holds rather
        # than replacing it with blanks; with nothing indexed there is nothing
        # to tombstone either.
        if not known:
            return "skip"
        index.execute(TOMBSTONE_IN_PLACE, (now, source_id))
        index.execute(DELETE_FTS, (source_id,))
        return "tombstone"

    # Only an explicit 'delete' forces a tombstone; any other op applies
    # whatever the source now says, and the source's own deleted_at wins.
    deleted = op == "delete" or src["deleted_at"] is not None

    # `content` is copied RAW — snippets and discord_get display it, and
    # search_text's rewritten mentions must not leak into what is shown back.
    values = {column: src[column] for column in COPIED_COLUMNS}
    if deleted:
        # Metadata survives so a reply chain still resolves and the assistant
        # can say "that message was deleted". The body never does.
        values["content"] = None
        values["attachments_json"] = None
        values["embeds_json"] = None

    index.execute(
        UPSERT_INDEX_MESSAGE,
        [values[column] for column in COPIED_COLUMNS] + [1 if deleted else 0, now],
    )
    if known:
        index.execute(DELETE_FTS, (source_id,))
    if deleted:
        return "tombstone"

    # Delete-then-insert is also what makes an edit behave: the previous body
    # is removed from the index, so the old text stops being findable the
    # moment the edit is applied.
    index.execute(
        INSERT_FTS,
        (
            source_id,
            _fts_body(src),
            src["channel_name"],
            src["author_name"],
            src["guild_name"],
        ),
    )
    return "upsert"


def drain(
    source: sqlite3.Connection,
    index: sqlite3.Connection,
    *,
    batch_size: int = DEFAULT_BATCH,
    max_batches: int | None = None,
    log: logging.Logger | None = None,
) -> RunStats:
    """Drain `index_queue` in id order, one index transaction per batch."""
    stats = RunStats()
    while max_batches is None or stats.batches < max_batches:
        # `revision` is not read on purpose: the queue names what changed, the
        # source row says what it now is.
        rows = source.execute(
            "SELECT id, source_id, op FROM index_queue ORDER BY id LIMIT ?",
            (batch_size,),
        ).fetchall()
        if not rows:
            break

        now = _utc_now_iso()
        index.execute("BEGIN IMMEDIATE")
        try:
            outcomes = [
                apply_one(source, index, str(row["source_id"]), str(row["op"] or ""), now)
                for row in rows
            ]
            index.execute("COMMIT")
        except BaseException:
            _rollback(index)
            raise

        # Only now. The index is durable, so forgetting these queue rows
        # forgets nothing. A crash one line earlier replays the whole batch,
        # which is exactly why every apply above is idempotent.
        source.execute("BEGIN IMMEDIATE")
        try:
            source.executemany(
                "DELETE FROM index_queue WHERE id = ?", [(row["id"],) for row in rows]
            )
            source.execute("COMMIT")
        except BaseException:
            _rollback(source)
            raise

        stats.batches += 1
        stats.applied += len(rows)
        stats.upserts += outcomes.count("upsert")
        stats.tombstones += outcomes.count("tombstone")
        stats.skipped += outcomes.count("skip")
        if log is not None:
            log.debug(
                "batch %d rows=%d upserts=%d tombstones=%d skipped=%d",
                stats.batches,
                len(rows),
                outcomes.count("upsert"),
                outcomes.count("tombstone"),
                outcomes.count("skip"),
            )
    return stats


# ---- status snapshot ---------------------------------------------------


def collect_status(
    source: sqlite3.Connection, index: sqlite3.Connection, cfg: Config
) -> dict:
    """Build the snapshot the MCP server reads instead of asking systemd.

    Every value is a count, an id, a timestamp or a name the operator put in
    the configuration. No message content, ever: `meta` is readable inside the
    container without going through the search layer's scoping.
    """
    indexed = _scalar(index, "SELECT COUNT(*) FROM messages")
    deleted = _scalar(index, "SELECT COUNT(*) FROM messages WHERE is_deleted = 1")

    # Ordering by created_ts rather than by the ISO string: created_ts is the
    # column the schema designates for range and ordering work.
    oldest_at = _scalar(
        index,
        "SELECT created_at FROM messages WHERE created_ts IS NOT NULL "
        "ORDER BY created_ts ASC LIMIT 1",
        default="",
    )
    newest_at = _scalar(
        index,
        "SELECT created_at FROM messages WHERE created_ts IS NOT NULL "
        "ORDER BY created_ts DESC LIMIT 1",
        default="",
    )

    # Counts every indexed row, tombstones included, so the per-guild numbers
    # sum back to indexed_message_count.
    per_guild = [
        {
            "guild_id": row["guild_id"],
            "guild_name": row["guild_name"] or "",
            "count": row["n"],
        }
        for row in index.execute(
            "SELECT guild_id, MAX(guild_name) AS guild_name, COUNT(*) AS n "
            "FROM messages GROUP BY guild_id ORDER BY guild_id"
        )
    ]

    source_count = _scalar(source, "SELECT COUNT(*) FROM messages")
    lag = _scalar(source, "SELECT COUNT(*) FROM index_queue")
    # Oldest by insertion order, which is the order the drain works in.
    lag_oldest = _scalar(
        source,
        "SELECT enqueued_at FROM index_queue ORDER BY id LIMIT 1",
        default="",
    )

    # Every configured guild, disabled ones included — the `enabled` flag only
    # tells the operator anything if the disabled entries are visible.
    allowed_guilds = [
        {
            "guild_id": scope.guild_id,
            "label": scope.label,
            "enabled": bool(scope.enabled),
            "channel_count": len(scope.channels),
        }
        for scope in cfg.guilds
    ]

    channels = {
        row["channel_id"]: row
        for row in source.execute(
            "SELECT channel_id, channel_name, channel_type, readable FROM channels"
        )
    }
    allowed_channels = []
    for scope in cfg.guilds:
        if not scope.active:
            continue
        # This walk is cfg.allowed_channel_ids in configuration order, kept as
        # a loop so each channel stays attached to the guild that allows it.
        for channel_id in scope.channels:
            known = channels.get(channel_id)
            allowed_channels.append(
                {
                    "channel_id": channel_id,
                    "guild_id": scope.guild_id,
                    "channel_name": (known["channel_name"] if known else "") or "",
                    "channel_type": (known["channel_type"] if known else "") or "",
                    # 1 ok / 0 permission denied / null not probed yet.
                    "readable": known["readable"] if known else None,
                }
            )

    backfill = [
        {
            "channel_id": row["channel_id"],
            "complete": bool(row["complete"]),
            "message_count": row["message_count"] or 0,
            "oldest_created_ts": row["oldest_created_ts"],
            "last_error": _clip(row["last_error"]),
        }
        for row in source.execute(
            "SELECT channel_id, complete, message_count, oldest_created_ts, "
            "last_error FROM backfill_state ORDER BY channel_id"
        )
    ]

    snapshot = {
        "last_indexed": _utc_now_iso(),
        "indexed_message_count": str(indexed),
        "source_message_count": str(source_count),
        "deleted_message_count": str(deleted),
        "index_lag": str(lag),
        "index_lag_oldest_enqueued_at": _clip(lag_oldest),
        "oldest_message_at": _clip(oldest_at),
        "newest_message_at": _clip(newest_at),
        "per_guild_counts_json": _jdump(per_guild),
        "allowed_guilds_json": _jdump(allowed_guilds),
        "allowed_channels_json": _jdump(allowed_channels),
        "backfill_state_json": _jdump(backfill),
    }

    # The collector's own health, forwarded as-is. A collector that has never
    # run leaves the table empty, which must read as "" rather than crash.
    state = {
        row["key"]: row["value"]
        for row in source.execute("SELECT key, value FROM ingest_state")
    }
    for key in INGEST_STATE_KEYS:
        snapshot[INGEST_STATE_RENAMES.get(key, key)] = _clip(state.get(key, ""))
    return snapshot


def publish_status(
    source: sqlite3.Connection, index: sqlite3.Connection, cfg: Config
) -> dict:
    """Write the snapshot into the index `meta` table and return it."""
    snapshot = collect_status(source, index, cfg)
    index.execute("BEGIN IMMEDIATE")
    try:
        index.executemany(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            sorted(snapshot.items()),
        )
        index.execute("COMMIT")
    except BaseException:
        _rollback(index)
        raise
    return snapshot


# ---- entry point -------------------------------------------------------


def _build_logger(log_dir, verbose: bool) -> logging.Logger:
    """Best-effort logging: counts, ids and timings, never a message body.

    A missing or read-only log directory is not a reason to stop indexing, so
    every failure here degrades to "no file log" and nothing else.
    """
    log = logging.getLogger("discord_knowledge.indexer")
    log.handlers.clear()
    log.propagate = False
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    if verbose:
        # stderr, so the summary line on stdout stays the only thing a caller
        # ever has to parse.
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(fmt)
        log.addHandler(stream)
    file_error = None
    try:
        directory = Path(log_dir)
        if not directory.is_dir():
            file_error = f"{directory} is not a directory"
        elif not os.access(directory, os.W_OK):
            file_error = f"{directory} is not writable by {os.getuid()}"
        else:
            handler = logging.FileHandler(directory / "indexer.log", encoding="utf-8")
            handler.setFormatter(fmt)
            log.addHandler(handler)
    except OSError as exc:
        file_error = f"{type(exc).__name__}: {exc}"
    log.addHandler(logging.NullHandler())
    if file_error:
        # Degrading to no file log is right; doing it silently is not. This
        # job runs every minute from cron with no terminal, so stderr is the
        # only channel that reaches anyone, and the file log is the only
        # record of why the index stopped moving.
        print(
            f"DISCORD_INDEX_WARN file logging disabled: {file_error}",
            file=sys.stderr,
        )
    return log


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"must be an integer >= 1; got {value!r}")
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be an integer >= 1; got {value!r}")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="indexer.py",
        description="Drain the collector's index_queue into the FTS5 search index.",
        epilog="exit 0 = success, 1 = runtime failure, 2 = bad arguments or configuration",
    )
    parser.add_argument("--config", default=None, help="path to config.yaml")
    parser.add_argument(
        "--source-db", default=None, help="override paths.source_db (tests)"
    )
    parser.add_argument(
        "--index-db", default=None, help="override paths.index_db (tests)"
    )
    parser.add_argument(
        "--batch",
        type=_positive_int,
        default=DEFAULT_BATCH,
        help=f"queue rows per index transaction (default {DEFAULT_BATCH})",
    )
    parser.add_argument(
        "--max-batches",
        type=_positive_int,
        default=None,
        help="stop after this many batches instead of draining fully",
    )
    parser.add_argument(
        "--status-only",
        action="store_true",
        help="republish the status snapshot without draining the queue",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="also log to stderr"
    )
    return parser


def main(argv=None) -> int:
    started = time.monotonic()
    args = build_parser().parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"DISCORD_INDEX_CONFIG {exc}", file=sys.stderr)
        return 2

    log = _build_logger(cfg.log_dir, args.verbose)
    source_path = Path(args.source_db or cfg.source_db)
    index_path = Path(args.index_db or cfg.index_db)

    if not source_path.is_file():
        # The collector owns that file. Creating it here would hand the
        # collector an empty database and hide the fact that it never ran.
        print(
            "DISCORD_INDEX_ERROR source database not found (the collector owns "
            f"it): {source_path}",
            file=sys.stderr,
        )
        log.error("source database not found")
        return 1
    try:
        index_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # let the sqlite open below report the real problem

    source = open_source_db(source_path)
    index = open_index_db(index_path)
    # The index carries message bodies. It is read by the assistant's
    # container, which runs as this same uid, so it never needs to be
    # world-readable — and it was created 0644 by default.
    for suffix in ("", "-wal", "-shm"):
        try:
            side = Path(str(index_path) + suffix)
            if side.exists() and (side.stat().st_mode & 0o007):
                side.chmod(0o640)
        except OSError:
            pass
    # Manual transaction control from here on: where COMMIT happens is the
    # whole crash-safety argument, so nothing implicit gets to guess. Both
    # open_*_db committed their schema work before returning.
    source.isolation_level = None
    index.isolation_level = None

    stats = RunStats()
    try:
        if not args.status_only:
            stats = drain(
                source,
                index,
                batch_size=args.batch,
                max_batches=args.max_batches,
                log=log,
            )
        # Published even when the queue was empty: to the container a stale
        # snapshot and a stopped indexer look identical.
        snapshot = publish_status(source, index, cfg)
    finally:
        _close(index)
        _close(source)

    line = (
        "DISCORD_INDEX "
        f"applied={stats.applied} "
        f"upserts={stats.upserts} "
        f"tombstones={stats.tombstones} "
        f"skipped={stats.skipped} "
        f"queue_remaining={snapshot['index_lag']} "
        f"indexed={snapshot['indexed_message_count']} "
        f"duration={time.monotonic() - started:.3f}s"
    )
    print(line)
    log.info(line)
    return 0


def cli(argv=None) -> int:
    """Nothing unexpected may escape as a traceback: a timer unit's log should
    carry one greppable line either way."""
    try:
        return main(argv)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"DISCORD_INDEX_FATAL {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(cli())
