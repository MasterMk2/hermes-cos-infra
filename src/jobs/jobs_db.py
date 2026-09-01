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

STATUSES = [
    "NEW", "QUEUED", "DISPATCHING", "RUNNING", "AGENT_STOPPED", "VALIDATING",
    "CONTINUE_REQUIRED", "WAITING_REVIEW", "CHANGES_REQUESTED", "WAITING_HUMAN",
    "WAITING_WORKER", "ACCEPTED", "COMPLETED", "FAILED", "CANCELLED",
    # Phase F.1.1 lifecycle migration:
    #  - deterministic acceptance -> IMPLEMENTATION_ACCEPTED (+review WAITING_REVIEW)
    #  - COMPLETED is reserved for the Phase F.2 independent review
    #  - INTERRUPTED = remote worker unreachable; job preserved, never completed
    "IMPLEMENTATION_ACCEPTED", "INTERRUPTED",
]

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
          no_progress_strikes INTEGER DEFAULT 0
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
          continuation_reason TEXT, progress_signature TEXT
        );
        CREATE TABLE IF NOT EXISTS event_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts TEXT, job_id TEXT, run_id TEXT, level TEXT, message TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
        CREATE INDEX IF NOT EXISTS idx_jobs_project ON jobs(project_id);
        CREATE INDEX IF NOT EXISTS idx_runs_job ON runs(job_id);
        """
    )
    con.commit()
    con.close()


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
    if new_status == "COMPLETED":
        fields["finished_at"] = now_iso()
    update_job(job_id, **fields)
    log(job_id, None, "state", f"status -> {new_status}")


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


if __name__ == "__main__":
    init_db()
    print(f"initialized {DB_PATH}")
