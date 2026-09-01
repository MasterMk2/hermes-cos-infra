#!/usr/bin/env python3
"""Detached job runner entry point.

Spawned by job_dispatch (start_new_session) so it survives the MCP process.
Crash-safe: all state lives in jobs.db; job_reconcile recovers stale states.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import jobs_db
from supervisor import run_job_loop

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: supervisor_runner.py <job_id>")
        raise SystemExit(2)
    jobs_db.init_db()
    raise SystemExit(run_job_loop(sys.argv[1]))
