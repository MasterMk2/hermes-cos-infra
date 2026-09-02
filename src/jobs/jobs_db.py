#!/usr/bin/env python3
"""Job layer persistence (SQLite) for the Hermes Coding-Worker Supervisor.

jobs.db is the canonical persistent state for jobs and agent runs.
Agent output is stored on the filesystem (logs/artifacts), never inlined.
"""

import json
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

JOBS_DIR = Path(__file__).resolve().parent
DB_PATH = JOBS_DIR / "jobs.db"
ARTIFACTS_DIR = JOBS_DIR / "artifacts"
LOGS_DIR = JOBS_DIR / "logs"
PROMPTS_DIR = JOBS_DIR / "prompts"

# Phase F.2 review-status values for jobs.review_status.
REVIEW_STATUSES = (
    "NOT_STARTED", "NOT_REQUIRED", "WAITING_REVIEW", "REVIEWING", "APPROVED",
    "CHANGES_REQUIRED", "REVIEW_INTERRUPTED", "REVIEW_FAILED",
)
# Review-row (immutable history) statuses.
REVIEW_ROW_STATUSES = (
    "reviewing", "approved", "changes_required", "interrupted",
    "failed", "cancelled",
)

STATUSES = [
    "NEW", "QUEUED", "DISPATCHING", "RUNNING", "AGENT_STOPPED", "VALIDATING",
    "CONTINUE_REQUIRED", "WAITING_REVIEW", "CHANGES_REQUESTED", "WAITING_HUMAN",
    "WAITING_WORKER", "ACCEPTED", "COMPLETED", "FAILED", "CANCELLED",
    # Phase F.1.1 lifecycle migration:
    #  - deterministic acceptance -> IMPLEMENTATION_ACCEPTED (+review WAITING_REVIEW)
    #  - COMPLETED is reserved for the Phase F.2 independent review
    #  - INTERRUPTED = remote worker unreachable; job preserved, never completed
    "IMPLEMENTATION_ACCEPTED", "INTERRUPTED",
    # F.1.1-Pi.2 hardening (M7): runner spawn failure must not leave QUEUED.
    # FAILED_TO_START is retryable (job_continue re-dispatches).
    "FAILED_TO_START",
]

# F.1.1-Pi.2 (M8): states where the implementation EXECUTION is over — even
# though review (Phase F.2) may still be pending. finished_at is stamped here.
TERMINAL_EXECUTION_STATES = {
    "COMPLETED", "IMPLEMENTATION_ACCEPTED", "FAILED", "CANCELLED",
}

# F.1.1-Pi.2 (M2): statuses a (re)dispatch may legally claim the job from.
DISPATCHABLE_STATUSES = (
    "NEW", "QUEUED", "CONTINUE_REQUIRED", "WAITING_HUMAN", "AGENT_STOPPED",
    "INTERRUPTED", "FAILED_TO_START",
)

JSON_FIELDS = {
    "acceptance_criteria", "acceptance_results", "git_changed_files",
}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def connect():
    con = sqlite3.connect(f"file:{DB_PATH}?mode=rw", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def init_db():
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACTS_DIR.mkdir(exist_ok=True)
    LOGS_DIR.mkdir(exist_ok=True)
    PROMPTS_DIR.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS jobs (
          job_id TEXT PRIMARY KEY, project_id TEXT, task_id TEXT,
          parent_job_id TEXT,
          kind TEXT DEFAULT 'implementation',
          goal TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'NEW',
          priority TEXT DEFAULT 'normal',
          worker_id TEXT, agent TEXT,
          repository TEXT NOT NULL,
          branch TEXT,
          created_at TEXT, started_at TEXT, finished_at TEXT, updated_at TEXT,
          attempt INTEGER DEFAULT 0, max_attempts INTEGER DEFAULT 6,
          prompt_path TEXT,
          result_summary TEXT,
          exit_code INTEGER, timed_out INTEGER DEFAULT 0,
          acceptance_criteria TEXT DEFAULT '[]',
          acceptance_results TEXT DEFAULT '[]',
          tests_command TEXT, tests_status TEXT DEFAULT 'NOT_RUN',
          tests_summary TEXT,
          git_before_sha TEXT, git_after_sha TEXT,
          git_changed_files TEXT DEFAULT '[]',
          git_dirty_before INTEGER, git_dirty_after INTEGER,
          review_status TEXT DEFAULT 'NOT_STARTED',
          next_action TEXT, failure_reason TEXT,
          needs_human INTEGER DEFAULT 0,
          no_progress_strikes INTEGER DEFAULT 0,
          cancel_requested_at TEXT,
          implementation_finished_at TEXT
        );
        CREATE TABLE IF NOT EXISTS runs (
          run_id TEXT PRIMARY KEY, job_id TEXT NOT NULL,
          attempt INTEGER, agent TEXT,
          started_at TEXT, finished_at TEXT,
          exit_code INTEGER, timed_out INTEGER DEFAULT 0,
          prompt_path TEXT, prompt_hash TEXT,
          stdout_path TEXT, stderr_path TEXT,
          result_summary TEXT,
          git_before TEXT, git_after TEXT,
          continuation_reason TEXT, progress_signature TEXT,
          diff_hash TEXT
        );
        CREATE TABLE IF NOT EXISTS event_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts TEXT, job_id TEXT, run_id TEXT, level TEXT, message TEXT
        );
        CREATE TABLE IF NOT EXISTS reviews (
          review_id TEXT PRIMARY KEY,
          job_id TEXT NOT NULL,
          cycle INTEGER NOT NULL,
          status TEXT NOT NULL DEFAULT 'reviewing',
          reviewer_agent TEXT, reviewer_model TEXT,
          base_sha TEXT, target_sha TEXT, diff_hash TEXT, content_sha TEXT,
          started_at TEXT, finished_at TEXT,
          verdict TEXT, summary TEXT,
          findings_json TEXT, blocking_count INTEGER DEFAULT 0,
          fingerprints_json TEXT,
          usage_json TEXT, error_summary TEXT,
          content_progress TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
        CREATE INDEX IF NOT EXISTS idx_jobs_project ON jobs(project_id);
        CREATE INDEX IF NOT EXISTS idx_runs_job ON runs(job_id);
        CREATE INDEX IF NOT EXISTS idx_reviews_job ON reviews(job_id, cycle);
        """
    )
    _ensure_columns(con)
    _ensure_review_columns(con)
    con.commit()
    con.close()


def _ensure_columns(con):
    """Additive-only migration (F.1.1-Pi.2). Never drops or rewrites data.

    F.1.1-Pi.2 columns:
      jobs.cancel_requested_at        (H4: cancellation is persistent intent)
      jobs.implementation_finished_at (M8: implementation execution end)
      runs.diff_hash                  (M6: canonical diff hash for progress)
    Existing databases are upgraded in place; fresh databases already include
    the columns via CREATE TABLE."""
    jobs_cols = {r[1] for r in con.execute("PRAGMA table_info(jobs)").fetchall()}
    if "cancel_requested_at" not in jobs_cols:
        con.execute("ALTER TABLE jobs ADD COLUMN cancel_requested_at TEXT")
    if "implementation_finished_at" not in jobs_cols:
        con.execute("ALTER TABLE jobs ADD COLUMN implementation_finished_at TEXT")
    runs_cols = {r[1] for r in con.execute("PRAGMA table_info(runs)").fetchall()}
    if "diff_hash" not in runs_cols:
        con.execute("ALTER TABLE runs ADD COLUMN diff_hash TEXT")


def _ensure_review_columns(con):
    """F.2 additive review schema (section 28): the reviews table is created
    with IF NOT EXISTS; legacy databases created before F.2 get any missing
    review columns added in place. Running twice changes nothing (idempotent)."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(reviews)").fetchall()}
    if not cols:
        return  # table itself was just created with the full schema
    additions = {
        "attempt_no": "INTEGER DEFAULT 1",
        # F.2 (section 61/107): whether the canonical content binding actually
        # moved between the previous verdicted cycle and this one
        # ('first_cycle' | 'changed' | 'unchanged'). Review evidence only —
        # a cycle that converged on UNCHANGED content is reviewer-only
        # convergence and must never be counted as fix-loop evidence.
        "content_progress": "TEXT",
    }
    for name, decl in additions.items():
        if name not in cols:
            con.execute(f"ALTER TABLE reviews ADD COLUMN {name} {decl}")


def _row_to_job(r):
    d = dict(r)
    for f in JSON_FIELDS:
        try:
            d[f] = json.loads(d.get(f) or "[]")
        except (TypeError, ValueError):
            d[f] = []
    return d


def new_id(prefix):
    return f"{prefix}_{secrets.token_hex(5)}"


def create_job(goal, repository, acceptance_criteria, agent=None, project_id=None,
               task_id=None, parent_job_id=None, kind="implementation",
               priority="normal", max_attempts=6, tests_command=None):
    con = connect()
    try:
        jid = new_id("job")
        ts = now_iso()
        con.execute(
            "INSERT INTO jobs (job_id, project_id, task_id, parent_job_id, kind, goal,"
            " status, priority, agent, repository, created_at, updated_at,"
            " max_attempts, acceptance_criteria, tests_command)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (jid, project_id, task_id, parent_job_id, kind, goal, "NEW",
             priority, agent, repository, ts, ts,
             max_attempts, json.dumps(acceptance_criteria, ensure_ascii=False),
             tests_command),
        )
        con.commit()
        log(jid, None, "info", f"job created status=NEW repository={repository}")
        return jid
    finally:
        con.close()


def get_job(job_id):
    con = connect()
    try:
        r = con.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return _row_to_job(r) if r else None
    finally:
        con.close()


def list_jobs(status=None, project_id=None, limit=50):
    q, p = "SELECT * FROM jobs", []
    if status or project_id:
        w = []
        if status:
            w.append("status=?"); p.append(status)
        if project_id:
            w.append("project_id=?"); p.append(project_id)
        q += " WHERE " + " AND ".join(w)
    q += " ORDER BY created_at DESC LIMIT ?"
    p.append(max(1, min(int(limit), 100)))
    con = connect()
    try:
        return [_row_to_job(r) for r in con.execute(q, p).fetchall()]
    finally:
        con.close()


def update_job(job_id, **fields):
    if not fields:
        return
    for f in list(fields):
        if f in JSON_FIELDS:
            fields[f] = json.dumps(fields[f], ensure_ascii=False)
    fields["updated_at"] = now_iso()
    cols = ", ".join(f"{k}=?" for k in fields)
    con = connect()
    try:
        con.execute(f"UPDATE jobs SET {cols} WHERE job_id=?",
                    tuple(fields.values()) + (job_id,))
        con.commit()
    finally:
        con.close()


def transition(job_id, new_status, failure_reason=None):
    if new_status not in STATUSES:
        raise ValueError(f"unknown status {new_status}")
    fields = {"status": new_status}
    if failure_reason is not None:
        fields["failure_reason"] = failure_reason
    # F.1.1-Pi.2 (M8): coherent lifecycle timestamps.
    # finished_at = the execution phase ended (review may still be pending);
    # implementation_finished_at = the agent run itself ended (AGENT_STOPPED).
    # H4: cancellation stamps cancel_requested_at once, persistently.
    if new_status == "CANCELLED":
        prev = get_job(job_id) or {}
        fields.setdefault("finished_at", now_iso())
        if not prev.get("cancel_requested_at"):
            fields["cancel_requested_at"] = now_iso()
    elif new_status in TERMINAL_EXECUTION_STATES:
        fields["finished_at"] = now_iso()
    if new_status == "AGENT_STOPPED":
        fields["implementation_finished_at"] = now_iso()
    if new_status == "RUNNING":
        # a new attempt started: execution-phase stamps no longer apply
        fields["finished_at"] = None
        fields["implementation_finished_at"] = None
    update_job(job_id, **fields)
    log(job_id, None, "state", f"status -> {new_status}")


def cas_status(job_id, new_status, allowed_from, **fields):
    """F.1.1-Pi.2 (M2/H4): compare-and-set state transition.

    Updates the job only when its current status is in `allowed_from`.
    Returns True when this caller won the transition, False otherwise —
    callers must treat False as "someone else owns the state now"
    (e.g. concurrent dispatch, or the job was CANCELLED meanwhile)."""
    if new_status not in STATUSES:
        raise ValueError(f"unknown status {new_status}")
    # M8: CAS paths get the same lifecycle timestamp coherence as
    # transition() — cancellation stamps are terminal and permanent.
    if new_status == "CANCELLED":
        fields.setdefault("cancel_requested_at", now_iso())
    if new_status in TERMINAL_EXECUTION_STATES:
        fields.setdefault("finished_at", now_iso())
    if new_status == "AGENT_STOPPED":
        fields.setdefault("implementation_finished_at", now_iso())
    if new_status == "RUNNING":
        fields.setdefault("finished_at", None)
        fields.setdefault("implementation_finished_at", None)
    placeholders = ",".join("?" for _ in allowed_from)
    if fields:
        for f in list(fields):
            if f in JSON_FIELDS:
                fields[f] = json.dumps(fields[f], ensure_ascii=False)
        fields["updated_at"] = now_iso()
        assignments = ", ".join(f"{k}=?" for k in fields)
        sql = (f"UPDATE jobs SET status=?, {assignments} WHERE job_id=? "
               f"AND status IN ({placeholders})")
        params = (new_status, *fields.values(), job_id, *allowed_from)
    else:
        sql = (f"UPDATE jobs SET status=?, updated_at=? WHERE job_id=? "
               f"AND status IN ({placeholders})")
        params = (new_status, now_iso(), job_id, *allowed_from)
    con = connect()
    try:
        cur = con.execute(sql, params)
        con.commit()
        return cur.rowcount == 1
    finally:
        con.close()


def cancel_requested(job_id):
    """H4: True once the job entered CANCELLED (persistent terminal intent)."""
    job = get_job(job_id)
    return bool(job) and (
        job["status"] == "CANCELLED" or bool(job.get("cancel_requested_at"))
    )


def create_run(job_id, attempt, agent, prompt_path, prompt_hash):
    con = connect()
    try:
        rid = new_id("run")
        con.execute(
            "INSERT INTO runs (run_id, job_id, attempt, agent, started_at,"
            " prompt_path, prompt_hash) VALUES (?,?,?,?,?,?,?)",
            (rid, job_id, attempt, agent, now_iso(), str(prompt_path), prompt_hash),
        )
        con.commit()
        log(job_id, rid, "info", f"run created attempt={attempt} agent={agent}")
        return rid
    finally:
        con.close()


def update_run(run_id, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    con = connect()
    try:
        con.execute(f"UPDATE runs SET {cols} WHERE run_id=?",
                    tuple(fields.values()) + (run_id,))
        con.commit()
    finally:
        con.close()


def get_run(run_id):
    con = connect()
    try:
        r = con.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(r) if r else None
    finally:
        con.close()


def latest_run(job_id):
    con = connect()
    try:
        r = con.execute(
            "SELECT * FROM runs WHERE job_id=? ORDER BY started_at DESC, rowid DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        return dict(r) if r else None
    finally:
        con.close()


def runs_for_job(job_id):
    con = connect()
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM runs WHERE job_id=? ORDER BY started_at, rowid", (job_id,))]
    finally:
        con.close()


def log(job_id, run_id, level, message):
    con = connect()
    try:
        con.execute(
            "INSERT INTO event_log (ts, job_id, run_id, level, message) VALUES (?,?,?,?,?)",
            (now_iso(), job_id, run_id, level, message[:500]),
        )
        con.commit()
    finally:
        con.close()


def counts_by_status():
    con = connect()
    try:
        return dict(con.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())
    finally:
        con.close()


# ── Phase F.2: immutable review history (section 27) ────────────────────

def create_review(job_id, cycle, reviewer_agent, reviewer_model,
                  base_sha=None, target_sha=None, diff_hash=None,
                  content_sha=None, attempt_no=1):
    con = connect()
    try:
        rid = new_id("rev")
        con.execute(
            "INSERT INTO reviews (review_id, job_id, cycle, attempt_no, status,"
            " reviewer_agent, reviewer_model, base_sha, target_sha, diff_hash,"
            " content_sha, started_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, job_id, cycle, attempt_no, "reviewing", reviewer_agent,
             reviewer_model, base_sha, target_sha, diff_hash, content_sha,
             now_iso()),
        )
        con.commit()
        log(job_id, None, "review",
            f"review started review_id={rid} cycle={cycle} attempt={attempt_no} "
            f"model={reviewer_model} target_sha={(target_sha or '')[:12]}")
        return rid
    finally:
        con.close()


def update_review(review_id, **fields):
    if not fields:
        return
    # JSON-typed columns arrive as python objects under the plain names.
    if "findings" in fields:
        fields["findings_json"] = json.dumps(fields.pop("findings"),
                                             ensure_ascii=False)
    if "fingerprints" in fields:
        fields["fingerprints_json"] = json.dumps(fields.pop("fingerprints"),
                                                 ensure_ascii=False)
    if "usage" in fields:
        fields["usage_json"] = json.dumps(fields.pop("usage"),
                                          ensure_ascii=False)
    cols = ", ".join(f"{k}=?" for k in fields)
    con = connect()
    try:
        con.execute(f"UPDATE reviews SET {cols} WHERE review_id=?",
                    tuple(fields.values()) + (review_id,))
        con.commit()
    finally:
        con.close()


def get_review(review_id):
    con = connect()
    try:
        r = con.execute("SELECT * FROM reviews WHERE review_id=?",
                        (review_id,)).fetchone()
        return _review_row(r) if r else None
    finally:
        con.close()


def _review_row(r):
    d = dict(r)
    for f in ("findings_json", "fingerprints_json", "usage_json"):
        raw = d.pop(f, None)
        key = f[:-5]
        try:
            d[key] = json.loads(raw) if raw else ({} if f == "usage_json" else [])
        except (TypeError, ValueError):
            d[key] = {} if f == "usage_json" else []
    return d


def reviews_for_job(job_id, limit=20):
    con = connect()
    try:
        return [_review_row(r) for r in con.execute(
            "SELECT * FROM reviews WHERE job_id=? ORDER BY cycle, started_at, rowid "
            "LIMIT ?", (job_id, max(1, min(int(limit), 100))))]
    finally:
        con.close()


def latest_review(job_id, statuses=None):
    con = connect()
    try:
        q = "SELECT * FROM reviews WHERE job_id=?"
        p = [job_id]
        if statuses:
            q += " AND status IN (%s)" % ",".join("?" for _ in statuses)
            p.extend(statuses)
        q += " ORDER BY cycle DESC, started_at DESC, rowid DESC LIMIT 1"
        r = con.execute(q, p).fetchone()
        return _review_row(r) if r else None
    finally:
        con.close()


def latest_approved_review(job_id):
    return latest_review(job_id, statuses=("approved",))


def review_verdict_counts():
    con = connect()
    try:
        rows = con.execute(
            "SELECT status, COUNT(*) FROM reviews GROUP BY status").fetchall()
        return {r[0]: r[1] for r in rows}
    finally:
        con.close()


def cas_review_status(job_id, new_review_status, allowed_from):
    """F.2: compare-and-set on jobs.review_status (single review owner)."""
    placeholders = ",".join("?" for _ in allowed_from)
    con = connect()
    try:
        cur = con.execute(
            f"UPDATE jobs SET review_status=?, updated_at=? WHERE job_id=? "
            f"AND review_status IN ({placeholders})",
            (new_review_status, now_iso(), job_id, *allowed_from))
        con.commit()
        return cur.rowcount == 1
    finally:
        con.close()


if __name__ == "__main__":
    init_db()
    print(f"initialized {DB_PATH}")
