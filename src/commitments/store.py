"""SQLite store and audit repository for Unified Commitments."""

import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple

from .models import (
    Commitment,
    CommitmentEvidence,
    Responsibility,
    WorkState,
    TemporalState,
    EvidenceRole,
    Currentness,
    Authority,
    compute_temporal_state,
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS commitments (
    commitment_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    summary TEXT,
    responsibility TEXT NOT NULL CHECK(responsibility IN ('SELF', 'COUNTERPARTY', 'SHARED', 'INFORMATIONAL')),
    work_state TEXT NOT NULL CHECK(work_state IN ('ACTIONABLE', 'WAITING', 'SCHEDULED', 'BLOCKED', 'DONE', 'CANCELLED', 'DISMISSED')),
    counterparty TEXT,
    project_id TEXT,
    due_at TEXT,
    due_precision TEXT,
    scheduled_at TEXT,
    scheduled_end_at TEXT,
    authority TEXT NOT NULL DEFAULT 'evidence',
    confidence REAL NOT NULL DEFAULT 1.0,
    currentness TEXT NOT NULL DEFAULT 'current' CHECK(currentness IN ('current', 'superseded', 'uncertain')),
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT,
    source_count INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS commitment_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    commitment_id TEXT NOT NULL REFERENCES commitments(commitment_id) ON DELETE CASCADE,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    evidence_role TEXT NOT NULL CHECK(evidence_role IN ('origin', 'schedule', 'confirmation', 'amendment', 'completion', 'cancellation', 'context')),
    confidence REAL NOT NULL DEFAULT 1.0,
    snippet TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(commitment_id, source_type, source_id)
);

CREATE TABLE IF NOT EXISTS commitment_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    commitment_id TEXT NOT NULL REFERENCES commitments(commitment_id) ON DELETE CASCADE,
    changed_at TEXT NOT NULL,
    field TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    actor TEXT NOT NULL DEFAULT 'system',
    reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_commitments_work_state ON commitments(work_state);
CREATE INDEX IF NOT EXISTS idx_commitments_responsibility ON commitments(responsibility);
CREATE INDEX IF NOT EXISTS idx_commitments_due_at ON commitments(due_at);
CREATE INDEX IF NOT EXISTS idx_commitments_scheduled_at ON commitments(scheduled_at);
CREATE INDEX IF NOT EXISTS idx_commitment_evidence_source ON commitment_evidence(source_type, source_id);
"""


def init_db(conn: sqlite3.Connection) -> None:
    """Initialize commitment tables and indexes idempotently."""
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def save_commitment(
    conn: sqlite3.Connection,
    cmt: Commitment,
    actor: str = "system",
    reason: str = "create",
    auto_commit: bool = True,
) -> Commitment:
    """Insert or update a Commitment with field-level change history and evidence."""
    now_iso = datetime.now(timezone.utc).isoformat()
    cur = conn.cursor()

    cur.execute("SELECT * FROM commitments WHERE commitment_id = ?", (cmt.commitment_id,))
    existing = cur.fetchone()

    if existing is None:
        cur.execute(
            """
            INSERT INTO commitments (
                commitment_id, title, summary, responsibility, work_state,
                counterparty, project_id, due_at, due_precision,
                scheduled_at, scheduled_end_at, authority, confidence,
                currentness, notes, created_at, updated_at, resolved_at, source_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cmt.commitment_id,
                cmt.title,
                cmt.summary,
                cmt.responsibility,
                cmt.work_state,
                cmt.counterparty,
                cmt.project_id,
                cmt.due_at,
                cmt.due_precision,
                cmt.scheduled_at,
                cmt.scheduled_end_at,
                cmt.authority,
                cmt.confidence,
                cmt.currentness,
                cmt.notes,
                cmt.created_at or now_iso,
                cmt.updated_at or now_iso,
                cmt.resolved_at,
                cmt.source_count,
            ),
        )
        cur.execute(
            """
            INSERT INTO commitment_history (
                commitment_id, changed_at, field, old_value, new_value, actor, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (cmt.commitment_id, now_iso, "status", None, cmt.work_state, actor, reason),
        )
    else:
        # Track changed fields
        tracked = [
            "title", "summary", "responsibility", "work_state", "counterparty",
            "project_id", "due_at", "scheduled_at", "authority", "currentness"
        ]
        col_names = [col[0] for col in cur.description]
        row_dict = dict(zip(col_names, existing))

        # A source replay cannot undo a completion established by later
        # evidence. Explicit admin reopen still uses save_commitment normally.
        if (reason in {"initial_backfill", "calendar_backfill"}
                and row_dict.get("work_state") == WorkState.DONE.value):
            cmt.work_state = WorkState.DONE.value
            cmt.resolved_at = row_dict.get("resolved_at")

        for f in tracked:
            old_v = str(row_dict.get(f) or "")
            new_v = str(getattr(cmt, f) or "")
            if old_v != new_v:
                cur.execute(
                    """
                    INSERT INTO commitment_history (
                        commitment_id, changed_at, field, old_value, new_value, actor, reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (cmt.commitment_id, now_iso, f, old_v, new_v, actor, reason),
                )

        # If existing is already superseded by a merge, preserve superseded state during backfill
        if row_dict.get("currentness") == "superseded" and cmt.currentness != "superseded":
            cmt.currentness = "superseded"
            cmt.work_state = row_dict.get("work_state") or "CANCELLED"
            cmt.notes = row_dict.get("notes")

        # Preserve higher source_count from existing/evidence table
        cur.execute("SELECT COUNT(*) FROM commitment_evidence WHERE commitment_id = ?", (cmt.commitment_id,))
        ev_count = cur.fetchone()[0]
        cmt.source_count = max(cmt.source_count, ev_count, row_dict.get("source_count") or 1)

        cur.execute(
            """
            UPDATE commitments SET
                title = ?, summary = ?, responsibility = ?, work_state = ?,
                counterparty = ?, project_id = ?, due_at = ?, due_precision = ?,
                scheduled_at = ?, scheduled_end_at = ?, authority = ?,
                confidence = ?, currentness = ?, notes = ?, updated_at = ?,
                resolved_at = ?, source_count = ?
            WHERE commitment_id = ?
            """,
            (
                cmt.title,
                cmt.summary,
                cmt.responsibility,
                cmt.work_state,
                cmt.counterparty,
                cmt.project_id,
                cmt.due_at,
                cmt.due_precision,
                cmt.scheduled_at,
                cmt.scheduled_end_at,
                cmt.authority,
                cmt.confidence,
                cmt.currentness,
                cmt.notes,
                now_iso,
                cmt.resolved_at,
                cmt.source_count,
                cmt.commitment_id,
            ),
        )

    # Save evidence if any
    for ev in cmt.evidence:
        add_evidence(conn, cmt.commitment_id, ev, auto_commit=auto_commit)

    if auto_commit:
        conn.commit()
    return cmt


def add_evidence(
    conn: sqlite3.Connection,
    commitment_id: str,
    ev: CommitmentEvidence,
    auto_commit: bool = True,
) -> bool:
    """Attach evidence to commitment idempotently. Returns True if newly inserted."""
    now_iso = datetime.now(timezone.utc).isoformat()
    ev_created = ev.created_at or now_iso
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO commitment_evidence (
                commitment_id, source_type, source_id, observed_at,
                evidence_role, confidence, snippet, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                commitment_id,
                ev.source_type,
                ev.source_id,
                ev.observed_at,
                ev.evidence_role,
                ev.confidence,
                ev.snippet,
                ev_created,
            ),
        )
        cur.execute(
            """
            UPDATE commitments SET
                source_count = (SELECT COUNT(*) FROM commitment_evidence WHERE commitment_id = ?),
                updated_at = ?
            WHERE commitment_id = ?
            """,
            (commitment_id, now_iso, commitment_id),
        )
        if auto_commit:
            conn.commit()
        return True
    except sqlite3.IntegrityError:
        # Already linked
        return False


def get_commitment(conn: sqlite3.Connection, commitment_id: str) -> Optional[Commitment]:
    """Retrieve full commitment by ID with evidence records."""
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM commitments WHERE commitment_id = ?", (commitment_id,))
    row = cur.fetchone()
    if row is None:
        return None

    cmt = Commitment(
        commitment_id=row["commitment_id"],
        title=row["title"],
        summary=row["summary"],
        responsibility=row["responsibility"],
        work_state=row["work_state"],
        counterparty=row["counterparty"],
        project_id=row["project_id"],
        due_at=row["due_at"],
        due_precision=row["due_precision"],
        scheduled_at=row["scheduled_at"],
        scheduled_end_at=row["scheduled_end_at"],
        authority=row["authority"],
        confidence=row["confidence"],
        currentness=row["currentness"],
        notes=row["notes"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        resolved_at=row["resolved_at"],
        source_count=row["source_count"],
    )

    cur.execute(
        "SELECT * FROM commitment_evidence WHERE commitment_id = ? ORDER BY observed_at ASC",
        (commitment_id,),
    )
    for er in cur.fetchall():
        cmt.evidence.append(
            CommitmentEvidence(
                id=er["id"],
                commitment_id=er["commitment_id"],
                source_type=er["source_type"],
                source_id=er["source_id"],
                observed_at=er["observed_at"],
                evidence_role=er["evidence_role"],
                confidence=er["confidence"],
                snippet=er["snippet"],
                created_at=er["created_at"],
            )
        )
    return cmt


def list_commitments(
    conn: sqlite3.Connection,
    responsibility: Optional[str] = None,
    work_state: Optional[str] = None,
    temporal_state: Optional[str] = None,
    project_id: Optional[str] = None,
    currentness: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    now: Optional[datetime] = None,
) -> List[Commitment]:
    """List commitments with flexible multi-dimensional filtering."""
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    where_clauses = []
    params: List[Any] = []

    if responsibility:
        where_clauses.append("responsibility = ?")
        params.append(responsibility)
    if work_state:
        where_clauses.append("work_state = ?")
        params.append(work_state)
    if project_id:
        where_clauses.append("project_id = ?")
        params.append(project_id)
    if currentness:
        where_clauses.append("currentness = ?")
        params.append(currentness)

    sql = "SELECT * FROM commitments"
    if where_clauses:
        sql += " WHERE " + " AND ".join(where_clauses)
    sql += " ORDER BY COALESCE(due_at, scheduled_at, '9999-12-31') ASC, updated_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    cur.execute(sql, params)
    rows = cur.fetchall()

    results: List[Commitment] = []
    for row in rows:
        cmt = Commitment(
            commitment_id=row["commitment_id"],
            title=row["title"],
            summary=row["summary"],
            responsibility=row["responsibility"],
            work_state=row["work_state"],
            counterparty=row["counterparty"],
            project_id=row["project_id"],
            due_at=row["due_at"],
            due_precision=row["due_precision"],
            scheduled_at=row["scheduled_at"],
            scheduled_end_at=row["scheduled_end_at"],
            authority=row["authority"],
            confidence=row["confidence"],
            currentness=row["currentness"],
            notes=row["notes"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            resolved_at=row["resolved_at"],
            source_count=row["source_count"],
        )
        # Filter by temporal state dynamically if requested
        if temporal_state and cmt.get_temporal_state(now) != temporal_state:
            continue
        results.append(cmt)

    return results


def search_commitments(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 20,
    now: Optional[datetime] = None,
) -> List[Commitment]:
    """Search commitments across title, summary, counterparty, and notes."""
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    like_pat = f"%{query}%"
    cur.execute(
        """
        SELECT * FROM commitments
        WHERE title LIKE ? OR summary LIKE ? OR counterparty LIKE ? OR notes LIKE ?
        ORDER BY updated_at DESC LIMIT ?
        """,
        (like_pat, like_pat, like_pat, like_pat, limit),
    )
    rows = cur.fetchall()
    results = []
    for r in rows:
        results.append(
            Commitment(
                commitment_id=r["commitment_id"],
                title=r["title"],
                summary=r["summary"],
                responsibility=r["responsibility"],
                work_state=r["work_state"],
                counterparty=r["counterparty"],
                project_id=r["project_id"],
                due_at=r["due_at"],
                due_precision=r["due_precision"],
                scheduled_at=r["scheduled_at"],
                scheduled_end_at=r["scheduled_end_at"],
                authority=r["authority"],
                confidence=r["confidence"],
                currentness=r["currentness"],
                notes=r["notes"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
                resolved_at=r["resolved_at"],
                source_count=r["source_count"],
            )
        )
    return results


def get_status(conn: sqlite3.Connection, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Generate high-level commitment metrics and stats."""
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM commitments")
    total = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM commitment_evidence")
    total_ev = cur.fetchone()[0]

    cur.execute("SELECT responsibility, COUNT(*) FROM commitments GROUP BY responsibility")
    by_resp = dict(cur.fetchall())

    cur.execute("SELECT work_state, COUNT(*) FROM commitments GROUP BY work_state")
    by_state = dict(cur.fetchall())

    cur.execute("SELECT source_type, COUNT(*) FROM commitment_evidence GROUP BY source_type")
    by_source = dict(cur.fetchall())

    # Dynamically calculate temporal states for non-dismissed, non-done commitments
    active_cmts = list_commitments(conn, limit=1000, now=now)
    temporal_counts: Dict[str, int] = {}
    for cmt in active_cmts:
        if cmt.work_state not in (WorkState.DONE.value, WorkState.CANCELLED.value, WorkState.DISMISSED.value):
            st = cmt.get_temporal_state(now)
            temporal_counts[st] = temporal_counts.get(st, 0) + 1

    return {
        "total_commitments": total,
        "total_evidence": total_ev,
        "by_responsibility": by_resp,
        "by_work_state": by_state,
        "by_source": by_source,
        "active_temporal_counts": temporal_counts,
    }
