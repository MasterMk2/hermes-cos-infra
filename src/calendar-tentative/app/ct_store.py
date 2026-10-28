#!/usr/bin/env python3
"""Calendar Tentative Registration — durable state.

One SQLite database, owned by this service alone, holding what must survive
a crash: which commitment has which tentative event, and how sure we are
that Google has it.

    proposals        one row per commitment ever proposed
    retired_ids      event ids that must never be inserted again
    notifications    the owner digest currently being delivered, if any
    runs             one row per run, counts only — never a title

Proposal states. The interesting ones are the two in the middle: a write-
ahead claim and the hold it becomes when the answer is lost.

    PENDING      reserved. Nothing was sent, or Google proved nothing exists.
                 The body may still be refreshed from the latest commitment.
    INSERTING    the insert call is about to be / was made. A crash here is
                 read as UNKNOWN on the next run, never as PENDING.
    UNKNOWN      the insert may have happened. Resolved only by asking Google
                 for the exact event id; never by inserting again blindly.
    ACTIVE       the tentative event exists and is still ours.
    CONFIRMED    the owner took the 【仮】 off the title.               final
    REJECTED     the owner deleted it. Never proposed again.              final
    USER_EDITED  the owner changed it. We never touch it again.           final
    WITHDRAWN    we removed it: the commitment resolved or Calendar got
                 the real event.                                          final
    FAILED       Google refused the insert `max_attempts` times.          final

"final" means the engine takes no further action on its own. `repropose`
(an explicit operator command) is the only way back, and it advances the
generation so the retired event id is never reused.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

SCHEMA_VERSION = 1

PENDING = "PENDING"
INSERTING = "INSERTING"
UNKNOWN = "UNKNOWN"
ACTIVE = "ACTIVE"
CONFIRMED = "CONFIRMED"
REJECTED = "REJECTED"
USER_EDITED = "USER_EDITED"
WITHDRAWN = "WITHDRAWN"
FAILED = "FAILED"

STATES = (PENDING, INSERTING, UNKNOWN, ACTIVE, CONFIRMED, REJECTED,
          USER_EDITED, WITHDRAWN, FAILED)
FINAL_STATES = frozenset({CONFIRMED, REJECTED, USER_EDITED, WITHDRAWN, FAILED})
#: States that occupy a slot on the tentative calendar (or may).
LIVE_STATES = frozenset({INSERTING, UNKNOWN, ACTIVE})

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS proposals (
  commitment_id         TEXT PRIMARY KEY,
  generation            INTEGER NOT NULL DEFAULT 1,
  calendar_id           TEXT NOT NULL,
  event_id              TEXT NOT NULL UNIQUE,
  state                 TEXT NOT NULL,
  kind                  TEXT NOT NULL,
  body_json             TEXT NOT NULL,   -- the meaningful fields we intend
  content_revision      TEXT NOT NULL,   -- revision of body_json
  applied_revision      TEXT,            -- what Google holds, as far as we know
  applied_body_json     TEXT,            -- ...and its meaningful fields
  etag                  TEXT,            -- Google's, after our last write
  html_link             TEXT,
  sort_key              TEXT NOT NULL,   -- local start, for ordering only
  line                  TEXT NOT NULL,   -- one notification line
  attempts              INTEGER NOT NULL DEFAULT 0,
  last_error            TEXT,            -- category only, never a body
  withdraw_reason       TEXT,
  withdraw_requested_at TEXT,
  notified_at           TEXT,
  created_at            TEXT NOT NULL,
  updated_at            TEXT NOT NULL,
  sent_at               TEXT,
  resolved_at           TEXT
);
CREATE INDEX IF NOT EXISTS idx_proposals_state ON proposals(state);

-- Google keeps a deleted event's id. Inserting it again is a 409 at best and
-- a resurrected event at worst, so a retired id is remembered forever.
CREATE TABLE IF NOT EXISTS retired_ids (
  event_id      TEXT PRIMARY KEY,
  commitment_id TEXT NOT NULL,
  retired_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
  notification_id TEXT PRIMARY KEY,
  body            TEXT NOT NULL,
  commitment_ids  TEXT NOT NULL,         -- JSON list
  state           TEXT NOT NULL,         -- PENDING | DONE | HELD
  attempts        INTEGER NOT NULL DEFAULT 0,
  last_outcome    TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at   TEXT NOT NULL,
  finished_at  TEXT,
  mode         TEXT NOT NULL,
  counts_json  TEXT,                     -- outcome counters only
  skipped_json TEXT,                     -- skip reason -> count
  error        TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 30000")
    con.execute("PRAGMA journal_mode = DELETE")
    con.execute("PRAGMA synchronous = FULL")
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db(con: sqlite3.Connection) -> sqlite3.Connection:
    con.executescript(SCHEMA)
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),))
    con.commit()
    return con


def open_db(path) -> sqlite3.Connection:
    return init_db(connect(path))


def get_meta(con, key: str):
    row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(con, key: str, value: str) -> None:
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, str(value)))


# ---------------------------------------------------------------- proposals


def all_proposals(con) -> dict:
    return {r["commitment_id"]: r for r in con.execute("SELECT * FROM proposals")}


def get(con, commitment_id: str):
    return con.execute("SELECT * FROM proposals WHERE commitment_id = ?",
                       (commitment_id,)).fetchone()


def is_retired(con, event_id: str) -> bool:
    return con.execute("SELECT 1 FROM retired_ids WHERE event_id = ?",
                       (event_id,)).fetchone() is not None


def reserve(con, *, commitment_id: str, calendar_id: str, event_id: str, generation: int,
            kind: str, body: dict, revision: str, sort_key: str, line: str) -> None:
    now = now_iso()
    con.execute(
        """INSERT INTO proposals (commitment_id, generation, calendar_id, event_id, state,
             kind, body_json, content_revision, sort_key, line, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'PENDING', ?, ?, ?, ?, ?, ?, ?)""",
        (commitment_id, generation, calendar_id, event_id, kind,
         json.dumps(body, ensure_ascii=False, sort_keys=True), revision, sort_key, line,
         now, now),
    )


def refresh_pending(con, commitment_id: str, *, kind: str, body: dict, revision: str,
                    sort_key: str, line: str) -> bool:
    """Update the intended body of a row nothing has been sent for."""
    cur = con.execute(
        """UPDATE proposals SET kind = ?, body_json = ?, content_revision = ?, sort_key = ?,
             line = ?, updated_at = ?
           WHERE commitment_id = ? AND state = 'PENDING' AND content_revision <> ?""",
        (kind, json.dumps(body, ensure_ascii=False, sort_keys=True), revision, sort_key, line,
         now_iso(), commitment_id, revision),
    )
    return cur.rowcount == 1


def set_intended(con, commitment_id: str, *, kind: str, body: dict, revision: str,
                 sort_key: str, line: str) -> None:
    """Record the body an update is about to send (before the PATCH)."""
    con.execute(
        """UPDATE proposals SET kind = ?, body_json = ?, content_revision = ?, sort_key = ?,
             line = ?, updated_at = ? WHERE commitment_id = ?""",
        (kind, json.dumps(body, ensure_ascii=False, sort_keys=True), revision, sort_key, line,
         now_iso(), commitment_id),
    )


def drop_pending(con, commitment_id: str) -> bool:
    """Forget a reservation that never reached Google."""
    cur = con.execute("DELETE FROM proposals WHERE commitment_id = ? AND state = 'PENDING'",
                      (commitment_id,))
    return cur.rowcount == 1


def transition(con, commitment_id: str, state: str, *, expect=None, error=None,
               count_attempt: bool = False, etag=None, html_link=None,
               applied_revision=None, applied_body=None, sent: bool = False) -> bool:
    """Move one row to `state`. With `expect`, only from that state (CAS)."""
    if state not in STATES:
        raise ValueError(f"unknown state {state}")
    now = now_iso()
    sets = ["state = ?", "updated_at = ?", "last_error = ?"]
    args: list = [state, now, (str(error)[:120] if error else None)]
    if count_attempt:
        sets.append("attempts = attempts + 1")
    if etag is not None:
        sets.append("etag = ?")
        args.append(etag)
    if html_link is not None:
        sets.append("html_link = ?")
        args.append(html_link)
    if applied_revision is not None:
        sets.append("applied_revision = ?")
        args.append(applied_revision)
    if applied_body is not None:
        sets.append("applied_body_json = ?")
        args.append(json.dumps(applied_body, ensure_ascii=False, sort_keys=True))
    if sent:
        sets.append("sent_at = COALESCE(sent_at, ?)")
        args.append(now)
    if state in FINAL_STATES:
        sets.append("resolved_at = ?")
        args.append(now)
    sql = f"UPDATE proposals SET {', '.join(sets)} WHERE commitment_id = ?"
    args.append(commitment_id)
    if expect is not None:
        sql += " AND state = ?"
        args.append(expect)
    cur = con.execute(sql, args)
    if cur.rowcount == 1 and state in FINAL_STATES:
        row = get(con, commitment_id)
        con.execute(
            "INSERT OR IGNORE INTO retired_ids(event_id, commitment_id, retired_at) VALUES (?, ?, ?)",
            (row["event_id"], commitment_id, now),
        )
    return cur.rowcount == 1


def adopt_remote(con, commitment_id: str, *, etag: str, applied_revision=None,
                 applied_body=None) -> None:
    sets = "etag = ?, updated_at = ?, last_error = NULL"
    args: list = [etag, now_iso()]
    if applied_revision is not None:
        sets += ", applied_revision = ?"
        args.append(applied_revision)
    if applied_body is not None:
        sets += ", applied_body_json = ?"
        args.append(json.dumps(applied_body, ensure_ascii=False, sort_keys=True))
    con.execute(f"UPDATE proposals SET {sets} WHERE commitment_id = ?", (*args, commitment_id))


def note_error(con, commitment_id: str, error: str) -> None:
    con.execute("UPDATE proposals SET last_error = ?, updated_at = ? WHERE commitment_id = ?",
                (str(error)[:120], now_iso(), commitment_id))


def request_withdrawal(con, commitment_id: str, reason: str) -> None:
    con.execute(
        """UPDATE proposals SET withdraw_reason = ?, withdraw_requested_at = COALESCE(
             withdraw_requested_at, ?), updated_at = ? WHERE commitment_id = ?""",
        (reason, now_iso(), now_iso(), commitment_id),
    )


def clear_withdrawal(con, commitment_id: str) -> None:
    con.execute(
        """UPDATE proposals SET withdraw_reason = NULL, withdraw_requested_at = NULL,
             updated_at = ? WHERE commitment_id = ?""",
        (now_iso(), commitment_id),
    )


def repropose(con, commitment_id: str, *, event_id_for) -> str | None:
    """Operator action: give a final row a fresh generation and event id."""
    row = get(con, commitment_id)
    if row is None or row["state"] not in FINAL_STATES:
        return None
    generation = int(row["generation"]) + 1
    event_id = event_id_for(row["calendar_id"], commitment_id, generation)
    while is_retired(con, event_id):  # pragma: no cover - needs a hash collision
        generation += 1
        event_id = event_id_for(row["calendar_id"], commitment_id, generation)
    con.execute(
        """UPDATE proposals SET generation = ?, event_id = ?, state = 'PENDING', attempts = 0,
             last_error = NULL, etag = NULL, html_link = NULL, applied_revision = NULL,
             applied_body_json = NULL, withdraw_reason = NULL, withdraw_requested_at = NULL, notified_at = NULL,
             sent_at = NULL, resolved_at = NULL, updated_at = ?
           WHERE commitment_id = ?""",
        (generation, event_id, now_iso(), commitment_id),
    )
    return event_id


def mark_notified(con, commitment_ids, *, at: str | None = None) -> None:
    stamp = at or now_iso()
    con.executemany(
        "UPDATE proposals SET notified_at = COALESCE(notified_at, ?) WHERE commitment_id = ?",
        [(stamp, cid) for cid in commitment_ids],
    )


# ------------------------------------------------------------ notifications


def open_notification(con):
    return con.execute(
        "SELECT * FROM notifications WHERE state = 'PENDING' ORDER BY created_at LIMIT 1"
    ).fetchone()


def add_notification(con, *, notification_id: str, body: str, commitment_ids) -> None:
    now = now_iso()
    con.execute(
        """INSERT OR IGNORE INTO notifications(notification_id, body, commitment_ids, state,
             created_at, updated_at) VALUES (?, ?, ?, 'PENDING', ?, ?)""",
        (notification_id, body, json.dumps(sorted(commitment_ids)), now, now),
    )


def settle_notification(con, notification_id: str, *, state: str, outcome: str) -> None:
    con.execute(
        """UPDATE notifications SET state = ?, last_outcome = ?, attempts = attempts + 1,
             updated_at = ? WHERE notification_id = ?""",
        (state, outcome[:80], now_iso(), notification_id),
    )


def note_notification_attempt(con, notification_id: str, outcome: str) -> None:
    con.execute(
        """UPDATE notifications SET last_outcome = ?, attempts = attempts + 1, updated_at = ?
           WHERE notification_id = ?""",
        (outcome[:80], now_iso(), notification_id),
    )


# ------------------------------------------------------------------- runs


def start_run(con, mode: str) -> int:
    cur = con.execute("INSERT INTO runs(started_at, mode) VALUES (?, ?)", (now_iso(), mode))
    con.commit()
    return int(cur.lastrowid)


def finish_run(con, run_id: int, *, counts: dict, skipped: dict, error=None) -> None:
    con.execute(
        "UPDATE runs SET finished_at = ?, counts_json = ?, skipped_json = ?, error = ? WHERE id = ?",
        (now_iso(), json.dumps(counts, sort_keys=True), json.dumps(skipped, sort_keys=True),
         (str(error)[:200] if error else None), run_id),
    )
    con.commit()


def status_snapshot(con) -> dict:
    """Counts and timestamps only: no title, no body, no event content."""
    by_state = {r["state"]: r["n"] for r in con.execute(
        "SELECT state, COUNT(*) AS n FROM proposals GROUP BY state")}
    last = con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    pending_note = con.execute(
        "SELECT notification_id, attempts, last_outcome FROM notifications WHERE state = 'PENDING'"
    ).fetchone()
    return {
        "schema_version": get_meta(con, "schema_version"),
        "calendar_id": get_meta(con, "calendar_id"),
        "proposals": {state: by_state.get(state, 0) for state in STATES},
        "last_run": None if last is None else {
            "started_at": last["started_at"], "finished_at": last["finished_at"],
            "mode": last["mode"], "error": last["error"],
            "counts": json.loads(last["counts_json"] or "{}"),
            "skipped": json.loads(last["skipped_json"] or "{}"),
        },
        "notification_pending": None if pending_note is None else dict(pending_note),
    }
