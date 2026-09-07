#!/usr/bin/env python3
"""Morning Brief runtime state — a separate SQLite database, never events.db.

Phase I.2 sections 64, 65, 66.

## What this store is, and what it deliberately is NOT

It is **not** the authority on "has today's brief already been sent". The
channel gateway is (I2-AMENDMENT G2, profile section H.4): `proactive_deliveries`
derives its PRIMARY KEY from `(notification_id, channel)` and carries a
redundant `UNIQUE INDEX idx_proactive_once` on the same pair, and
`reserve_proactive()` catches the `IntegrityError` and answers
`200 {"detail": "already_delivered"}`. That mechanism already satisfies phase
sections 57 and 58 completely, on the only writer in the system, inside a
single transaction.

Building a second competing idempotency mechanism here would give two
authorities that can disagree — and the one that would lose is the one that
cannot see the actual delivery. So this table is:

  * a **cheap local pre-check** so the ordinary second timer trigger costs one
    SQLite read instead of a plan build, a render and a socket round trip; and
  * the **local record** phase section 66 asks for (date, plan_hash,
    generated_at, delivered_at, delivery_channel, delivery_status).

When the two disagree, the gateway wins: a gateway `already_delivered` answer
is recorded here as `delivered`, never reported as an error.

## What is never stored

No raw source message bodies, and not the brief text itself (section 66) —
only `plan_hash`, which is the sha256 the planner computes over the plan body.
A leaked planning.db must not become a second copy of the owner's mail.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone

#: Terminal and non-terminal values of `morning_brief_runs.delivery_status`.
PENDING = "pending"
DELIVERED = "delivered"
SUPPRESSED = "suppressed"
SKIPPED = "skipped"
FAILED = "failed"

#: The states after which the owner has today's brief and nothing more may be
#: sent for that local date.
SETTLED = frozenset({DELIVERED, SUPPRESSED})

STATE_DB_NAME = "planning.db"
HEALTH_FILE_NAME = "morning-brief-health.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS morning_brief_runs (
  local_date       TEXT PRIMARY KEY,
  plan_hash        TEXT NOT NULL,
  generated_at     TEXT NOT NULL,
  delivered_at     TEXT,
  delivery_channel TEXT,
  delivery_status  TEXT NOT NULL,
  attempts         INTEGER NOT NULL DEFAULT 0,
  last_error       TEXT
);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def state_db_path(planning_dir: str) -> str:
    return os.path.join(planning_dir, STATE_DB_NAME)


def health_path(planning_dir: str) -> str:
    return os.path.join(planning_dir, HEALTH_FILE_NAME)


def open_state(path: str) -> sqlite3.Connection:
    """Open (creating if needed) the planning state database.

    The caller owns the connection and must close it. `:memory:` is accepted
    so tests never touch a file.
    """
    if path != ":memory:":
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
    con = sqlite3.connect(path, timeout=15.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    # Rollback journal, not WAL: a WAL database needs write access to the
    # directory holding it in order to be *read*, and this file lives in a
    # directory the service unit is otherwise happy to keep narrow.
    con.execute("PRAGMA journal_mode = DELETE")
    con.executescript(SCHEMA)
    con.commit()
    return con


class BriefState:
    """Thin, explicit accessor. Every method commits before returning."""

    def __init__(self, con: sqlite3.Connection):
        self.con = con

    # -- reads ---------------------------------------------------------

    def get(self, local_date: str):
        cur = self.con.execute(
            "SELECT * FROM morning_brief_runs WHERE local_date = ?",
            (local_date,),
        )
        return cur.fetchone()

    def is_settled(self, local_date: str) -> bool:
        """True when today's brief is already delivered or deliberately
        suppressed. This is the cheap pre-check, not the authority."""
        row = self.get(local_date)
        return bool(row) and row["delivery_status"] in SETTLED

    def attempts(self, local_date: str) -> int:
        row = self.get(local_date)
        return int(row["attempts"]) if row else 0

    # -- writes --------------------------------------------------------

    def begin_run(self, local_date: str, plan_hash: str,
                  generated_at: str | None = None) -> None:
        """Record that a plan exists for this date, without claiming delivery.

        Re-running the same date overwrites `plan_hash`/`generated_at` and
        leaves `attempts` alone: a restart that recomputes the plan is a
        normal event, and losing the attempt counter would un-bound the retry
        budget that section 59 requires.

        `last_error` IS cleared, and only here: a plan that has just been built
        successfully makes yesterday's "planner_error: …" a lie about the row
        it is attached to. A delivery error is re-recorded by the delivery path
        moments later if it recurs.
        """
        generated_at = generated_at or _utcnow()
        self.con.execute(
            """
            INSERT INTO morning_brief_runs
                (local_date, plan_hash, generated_at, delivery_status, attempts)
            VALUES (?, ?, ?, ?, 0)
            ON CONFLICT(local_date) DO UPDATE SET
                plan_hash = excluded.plan_hash,
                generated_at = excluded.generated_at,
                last_error = NULL,
                delivery_status = CASE
                    WHEN morning_brief_runs.delivery_status IN ('delivered', 'suppressed')
                    THEN morning_brief_runs.delivery_status
                    ELSE excluded.delivery_status END
            """,
            (local_date, plan_hash, generated_at, PENDING),
        )
        self.con.commit()

    def note_attempt(self, local_date: str) -> int:
        """Increment and return the attempt counter for this date."""
        self.con.execute(
            "UPDATE morning_brief_runs SET attempts = attempts + 1 "
            "WHERE local_date = ?",
            (local_date,),
        )
        self.con.commit()
        return self.attempts(local_date)

    def mark_delivered(self, local_date: str, *, channel: str,
                       delivered_at: str | None = None) -> None:
        self.con.execute(
            "UPDATE morning_brief_runs SET delivery_status = ?, "
            "delivered_at = ?, delivery_channel = ?, last_error = NULL "
            "WHERE local_date = ?",
            (DELIVERED, delivered_at or _utcnow(), channel, local_date),
        )
        self.con.commit()

    def mark_status(self, local_date: str, status: str, *,
                    last_error: str | None = None,
                    plan_hash: str = "", channel: str | None = None) -> None:
        """Record a non-delivered outcome, creating the row when the run never
        got as far as building a plan (a late-boot skip, a disabled brief)."""
        row = self.get(local_date)
        if row is None:
            self.con.execute(
                "INSERT INTO morning_brief_runs (local_date, plan_hash, "
                "generated_at, delivery_status, attempts, last_error, "
                "delivery_channel) VALUES (?, ?, ?, ?, 0, ?, ?)",
                (local_date, plan_hash, _utcnow(), status, last_error, channel),
            )
        else:
            self.con.execute(
                "UPDATE morning_brief_runs SET delivery_status = ?, "
                "last_error = ?, delivery_channel = COALESCE(?, delivery_channel) "
                "WHERE local_date = ?",
                (status, last_error, channel, local_date),
            )
        self.con.commit()

    def prune(self, keep_days: int = 400) -> int:
        """Bound the table. It gains one row per day; 400 rows is ~30 KB."""
        cur = self.con.execute(
            "DELETE FROM morning_brief_runs WHERE local_date NOT IN "
            "(SELECT local_date FROM morning_brief_runs "
            " ORDER BY local_date DESC LIMIT ?)",
            (int(keep_days),),
        )
        self.con.commit()
        return cur.rowcount


# --- health file (phase section 105) ------------------------------------
#
# `status` describes the last outcome that could have delivered:
# ok | degraded | fail | skipped | disabled | unknown. A `plan`, `brief` or
# `--dry-run` invocation refreshes `last_plan_generated` and `last_plan_hash`
# but deliberately leaves `status` and `last_failure_reason` alone — a manual
# preview must not paint over a real failure as healthy, and the next
# scheduled run is what clears it.

#: Exactly the keys I2-CONTRACT.md section 6 fixes, plus `updated_at`.
HEALTH_FIELDS = (
    "last_plan_generated", "last_delivery_attempt", "last_delivery_success",
    "last_plan_hash", "status", "last_failure_reason", "updated_at",
)


def empty_health() -> dict:
    return {
        "last_plan_generated": None,
        "last_delivery_attempt": None,
        "last_delivery_success": None,
        "last_plan_hash": None,
        "status": "unknown",
        "last_failure_reason": None,
        "updated_at": None,
    }


def read_health(planning_dir: str) -> dict:
    """Never raises. A missing or corrupt file reads as `empty_health()`."""
    path = health_path(planning_dir)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return empty_health()
    if not isinstance(data, dict):
        return empty_health()
    out = empty_health()
    out.update({k: data.get(k) for k in HEALTH_FIELDS if k in data})
    return out


def write_health(planning_dir: str, updates: dict) -> dict:
    """Merge `updates` into the health file and write it atomically.

    Merge rather than replace: `last_delivery_success` must survive a day on
    which delivery failed, otherwise "when did this last work?" — the one
    question the file exists to answer — becomes unanswerable.
    """
    current = read_health(planning_dir)
    for key, value in updates.items():
        if key not in HEALTH_FIELDS:
            raise ValueError(f"unknown health field: {key}")
        current[key] = value
    current["updated_at"] = _utcnow()

    os.makedirs(planning_dir, exist_ok=True)
    path = health_path(planning_dir)
    fd, tmp = tempfile.mkstemp(dir=planning_dir, prefix=".health-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(current, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o640)
        except OSError:
            pass
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return current
