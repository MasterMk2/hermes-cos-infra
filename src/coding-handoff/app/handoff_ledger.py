#!/usr/bin/env python3
"""Coding Handoff Broker — the authorization ledger (Phase J.1).

One table, one job: make "the owner authorized this" produce **at most one**
coding job, for good, across retries, concurrent turns and a process that dies
mid-write.

The chat transports guarantee the opposite of exactly-once. A Discord webhook
can be redelivered, the gateway retries a request whose reply it never saw, and
a crash between "job created" and "authorization recorded" leaves the broker
certain of nothing. Since the worker runs one job at a time, a duplicate does
not fail loudly — it queues, and the owner watches the same work run twice.

Three mechanisms, and each one closes a hole the others cannot:

  the primary key      one row per authorization id, so two concurrent callers
                       cannot both be the writer.

  the pre-recorded id  the job id is **derived** from the authorization and
                       written to the ledger *before* any job exists. A writer
                       that dies after creating the job leaves the id behind,
                       so recovery finds the job that was already made instead
                       of making another. Creation is create-if-absent against
                       `jobs.job_id`, which is a primary key in the canonical
                       database — so even a torn recovery converges on one job.

  the claim epoch      retaking a stale claim increments it. The original,
                       slow writer can still be alive; its late `settle` is
                       refused because it no longer holds the current epoch.
                       Without this, a writer that was merely slow would
                       overwrite the recovery's result.

Crash recovery is therefore not "retry the request". It is "find out what the
previous attempt actually did, and finish it".
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timezone

#: How long a half-finished claim is left alone before recovery takes it over.
#: Long enough that a slow `dispatch_job` is never mistaken for a dead writer,
#: short enough that a crash does not need an operator.
STALE_CLAIM_SECONDS = 300

CLAIMING = "claiming"
CREATED = "created"
REFUSED = "refused"

#: What `claim` concluded.
WON = "won"                  # this caller is the writer; go and finish
DONE = "done"                # already completed; return the existing job
IN_PROGRESS = "in_progress"  # another writer holds a fresh claim
CONFLICT = "conflict"        # same id, different bound request

JOB_ID_RE = re.compile(r"^job_[0-9a-f]{10}$")

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS authorizations (
  authorization_id TEXT PRIMARY KEY,
  conversation_id  TEXT NOT NULL,
  project_key      TEXT NOT NULL,
  -- What the owner approved. Stored so a replay can be told apart from a
  -- different request arriving under a reused id.
  scope_digest     TEXT NOT NULL,
  -- Derived, and written before the job exists. This is the correlation that
  -- survives a crash.
  job_id           TEXT NOT NULL,
  state            TEXT NOT NULL,
  claim_epoch      INTEGER NOT NULL DEFAULT 1,
  detail           TEXT,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_auth_project
  ON authorizations(project_key, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_auth_job
  ON authorizations(job_id);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def deterministic_job_id(authorization_id: str, project_key: str,
                         scope_digest: str) -> str:
    """The job id this authorization will always produce.

    Derived rather than random so that recovery does not depend on having
    recorded anything: the same authorization, for the same project and the
    same approved scope, names the same job every time. The shape matches what
    the supervisor and the coding bridge both validate (`job_` + 10 hex).
    """
    material = f"{authorization_id}\x00{project_key}\x00{scope_digest}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"job_{digest[:10]}"


def connect(path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 30000")
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA synchronous = FULL")
    return con


def init_db(con: sqlite3.Connection) -> sqlite3.Connection:
    con.executescript(SCHEMA)
    con.commit()
    return con


def open_db(path) -> sqlite3.Connection:
    return init_db(connect(path))


def _age_seconds(row, now: datetime) -> float:
    try:
        updated = datetime.fromisoformat(str(row["updated_at"]))
    except (TypeError, ValueError):
        return float("inf")
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return (now - updated).total_seconds()


def claim(con: sqlite3.Connection, authorization_id: str, *,
          conversation_id: str, project_key: str, scope_digest: str,
          job_id: str, now: datetime | None = None) -> tuple:
    """Decide this caller's role. Returns `(outcome, row, epoch)`.

    One `BEGIN IMMEDIATE` transaction, so two callers arriving together cannot
    both come back as WON.
    """
    now = now or datetime.now(timezone.utc)
    stamp = now.isoformat()
    con.execute("BEGIN IMMEDIATE")
    try:
        found = con.execute(
            "SELECT * FROM authorizations WHERE authorization_id=?",
            (authorization_id,),
        ).fetchone()
        if found is None:
            con.execute(
                "INSERT INTO authorizations (authorization_id, conversation_id,"
                " project_key, scope_digest, job_id, state, claim_epoch,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (authorization_id, conversation_id, project_key, scope_digest,
                 job_id, CLAIMING, 1, stamp, stamp),
            )
            con.commit()
            return WON, None, 1

        row = dict(found)
        # A reused id carrying a different request is not a replay. Answering
        # it with the first request's job would tell the owner their new
        # approval was accepted when something else entirely was started.
        if (row["conversation_id"], row["project_key"], row["scope_digest"]) \
                != (conversation_id, project_key, scope_digest):
            con.commit()
            return CONFLICT, row, int(row["claim_epoch"])

        if row["state"] == CREATED:
            con.commit()
            return DONE, row, int(row["claim_epoch"])

        if row["state"] == CLAIMING \
                and _age_seconds(found, now) <= STALE_CLAIM_SECONDS:
            con.commit()
            return IN_PROGRESS, row, int(row["claim_epoch"])

        # Either a stale claim (the writer probably died) or a previous refusal
        # the caller is entitled to retry. Both become a new epoch, which
        # fences the old writer out of settling.
        epoch = int(row["claim_epoch"]) + 1
        con.execute(
            "UPDATE authorizations SET state=?, claim_epoch=?, updated_at=?,"
            " detail=? WHERE authorization_id=?",
            (CLAIMING, epoch, stamp,
             "retaken" if row["state"] == CLAIMING else "retried after refusal",
             authorization_id),
        )
        con.commit()
        return WON, row, epoch
    except Exception:
        con.rollback()
        raise


def settle(con: sqlite3.Connection, authorization_id: str, *, epoch: int,
           state: str, detail: str = "") -> bool:
    """Record the outcome, only if this caller still holds the claim.

    Returns False when the epoch has moved on — meaning a recovery took over
    while this writer was working. A False here is not an error to retry; it
    means somebody else is now responsible for the answer.
    """
    cur = con.execute(
        "UPDATE authorizations SET state=?, detail=?, updated_at=?"
        " WHERE authorization_id=? AND claim_epoch=?",
        (state, str(detail or "")[:300], now_iso(), authorization_id, int(epoch)),
    )
    con.commit()
    return cur.rowcount == 1


def get(con: sqlite3.Connection, authorization_id: str) -> dict | None:
    row = con.execute(
        "SELECT * FROM authorizations WHERE authorization_id=?",
        (authorization_id,),
    ).fetchone()
    return dict(row) if row else None


def latest_for_project(con: sqlite3.Connection, project_key: str) -> dict | None:
    """The most recent job this project produced, so "how is it going?" can be
    answered without the owner having to quote a job id back.

    Includes rows still in `claiming`: their job id is already durable, and a
    job that is being created is a truer answer than "nothing here".
    """
    row = con.execute(
        "SELECT * FROM authorizations WHERE project_key=?"
        " ORDER BY created_at DESC LIMIT 1",
        (str(project_key or ""),),
    ).fetchone()
    return dict(row) if row else None


def job_is_ours(con: sqlite3.Connection, job_id: str) -> dict | None:
    """Only jobs this broker created may be read back through the chat route.

    The supervisor holds jobs from earlier phases and any number an operator
    started at the CLI. None of them were authorized on a channel, so none of
    them are answerable on one.
    """
    row = con.execute(
        "SELECT * FROM authorizations WHERE job_id=? LIMIT 1",
        (str(job_id or ""),),
    ).fetchone()
    return dict(row) if row else None
