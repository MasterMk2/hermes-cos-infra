#!/usr/bin/env python3
"""Attention Engine — how another component's database is opened.

One function, because the rule it encodes is easy to get wrong and expensive
to get wrong quietly.

**A WAL database cannot be read through a read-only mount.** Measured on this
host, 2026-09-04: opening `file:...?mode=ro` against a WAL database whose
`-shm` sidecar does not exist fails with `attempt to write a readonly
database`, because SQLite has to create the shared-memory index before it can
read anything. A read-only bind mount, which was the first design for
isolating this engine from the Event Layer's data, therefore cannot work at
all — not "works with a caveat", but fails on every scan.

So the connection is opened read-write at the filesystem level and made
read-only at the SQLite level:

    PRAGMA query_only = ON

Also measured, on the live events database: a subsequent UPDATE and a
subsequent CREATE TABLE both fail with `attempt to write a readonly
database`, while SELECT works. That is a real guarantee rather than a
convention — the engine cannot modify the Event Layer, the jobs supervisor or
the Discord collector's index even by accident.
"""

from __future__ import annotations

import sqlite3


class SourceUnavailable(Exception):
    """The source could not be opened. A scan continues without it.

    Deliberately not fatal: the Event Layer being briefly locked must not stop
    the coding and health collectors from running, and a missing database on a
    host where that component is not installed is a normal state, not a fault.
    """


def open_source(path, *, timeout: float = 10.0) -> sqlite3.Connection:
    """A query-only connection to another component's SQLite database."""
    try:
        con = sqlite3.connect(str(path), timeout=timeout)
    except sqlite3.Error as exc:
        raise SourceUnavailable(f"{path}: {type(exc).__name__}") from exc
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout = 10000")
        # Order matters: query_only must be set before anything else touches
        # the database, and it is what makes every statement after this point
        # incapable of changing someone else's data.
        con.execute("PRAGMA query_only = ON")
    except sqlite3.Error as exc:
        con.close()
        raise SourceUnavailable(f"{path}: {type(exc).__name__}") from exc
    return con


def table_exists(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def columns(con: sqlite3.Connection, table: str) -> set:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
