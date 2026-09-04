#!/usr/bin/env python3
"""Attention Engine — signals from the coding system (phase F.2).

The governing rule is section 33, and it is worth stating before any code:

    the system is recovering on its own  ->  do not interrupt
    the system needs a human            ->  interrupt

That single line decides everything here. The F.2 supervisor drives a fix
loop: GLM implements, a deterministic gate validates, Muse reviews, and a
CHANGES_REQUIRED verdict sends it round again. Every one of those transitions
is visible in `jobs.db`, and notifying on any of them would report the
machine's internal monologue as news (sections 29, 32). The supervisor moves
a job to WAITING_HUMAN precisely when it has run out of ways to continue —
that is the event worth a message.

Statuses seen in production on 2026-09-04: AGENT_STOPPED, CANCELLED,
COMPLETED, IMPLEMENTATION_ACCEPTED, WAITING_HUMAN. Only the last two of those
five are ever notified, and IMPLEMENTATION_ACCEPTED is not one of them — it
means the deterministic gate passed and the review has not run yet, which is
the middle of the loop, not the end.
"""

from __future__ import annotations

import logging

from att_signals import (
    SIG_JOB_COMPLETED,
    SIG_JOB_FAILED_TO_START,
    SIG_JOB_REVIEW_LIMIT,
    SIG_JOB_WAITING_HUMAN,
    SOURCE_CODING,
    make_signal,
)
from att_sources import SourceUnavailable, open_source, table_exists
from att_time import parse_iso

log = logging.getLogger("attention.coding")

STATUS_COMPLETED = "COMPLETED"
STATUS_WAITING_HUMAN = "WAITING_HUMAN"

#: The supervisor writes this exact prefix into `failure_reason` when the
#: review budget runs out (supervisor.py: "review_cycle_limit_reached ...").
#: Matching on it lets the notification say WHY the loop stopped instead of
#: "a job needs you", which is the difference between an actionable message
#: and an alert (section 28).
REVIEW_LIMIT_MARKER = "review_cycle_limit_reached"

#: Reasons that mean the job never got off the ground and no retry will fix
#: it. A retryable spawn failure is the supervisor's business, not the
#: owner's (section 33).
UNRECOVERABLE_MARKERS = ("unrecoverable", "failed_to_start_permanent",
                         "no_worker", "worker_unreachable_permanent")

#: Never let a bounded internal string reach the owner unbounded. Reasons can
#: contain a path or a command line; `safe_reason` is what stops that
#: (section 71).
MAX_REASON_CHARS = 160

_SENSITIVE_FRAGMENTS = ("/srv/", "/home/", "/etc/", "/opt/", "/root/",
                        "Authorization", "Bearer ", "token", "api_key",
                        "password", "secret")


def safe_reason(text) -> str:
    """A bounded, path-free rendering of a supervisor failure reason.

    Section 71 is explicit that a WAITING_HUMAN notification must not dump
    secrets or paths. Rather than trying to redact a filesystem path in
    place — which invites an incomplete pattern — a reason that contains one
    is replaced wholesale with a category, and the detail stays in the jobs
    database where the owner can look it up.
    """
    s = " ".join(str(text or "").split())
    if not s:
        return ""
    lowered = s.lower()
    if any(f.lower() in lowered for f in _SENSITIVE_FRAGMENTS):
        return "詳細は job の failure_reason を参照してください"
    if len(s) > MAX_REASON_CHARS:
        s = s[: MAX_REASON_CHARS - 1].rstrip() + "…"
    return s


def collect(cfg, clock, *, initialized_at=None) -> list:
    path = cfg.sources["jobs_db"]
    try:
        con = open_source(path)
    except SourceUnavailable as exc:
        log.warning("coding source unavailable: %s", exc)
        return []
    try:
        if not table_exists(con, "jobs"):
            return []
        rows = con.execute(
            "SELECT job_id, status, review_status, needs_human, attempt,"
            " goal, repository, failure_reason, next_action, created_at,"
            " updated_at, finished_at, result_summary"
            " FROM jobs ORDER BY updated_at DESC LIMIT 200"
        ).fetchall()
    except Exception:
        log.exception("coding source query failed")
        return []
    finally:
        con.close()

    now = clock.now()
    max_age = float(cfg.coding["max_age_hours"]) * 3600
    out = []
    for row in rows:
        updated = parse_iso(row["updated_at"]) or parse_iso(row["created_at"])
        if updated is None:
            continue
        # A job that has not moved in two days is settled, whatever its
        # status. Without this bound the first scan announces every job the
        # supervisor ever parked (section 95 pairs with section 96 here).
        if (now - updated).total_seconds() > max_age:
            continue
        sig = signal_for_job(row, cfg, clock)
        if sig is not None:
            out.append(sig)
    return out


def signal_for_job(row, cfg, clock):
    """One signal, or None when the job is in a state nobody needs to know."""
    status = str(row["status"] or "").upper()
    if status not in {s.upper() for s in cfg.coding["notify_statuses"]}:
        # Everything else is the loop running: RUNNING, VALIDATING,
        # WAITING_REVIEW, REVIEWING, IMPLEMENTATION_ACCEPTED, AGENT_STOPPED.
        # A CHANGES_REQUIRED review that the supervisor is about to act on
        # lives here too, which is section 32 (fix-loop noise).
        return None

    when = (parse_iso(row["finished_at"]) or parse_iso(row["updated_at"])
            or clock.now())
    goal = " ".join(str(row["goal"] or "").split())
    reason = safe_reason(row["failure_reason"])
    # The revision is what makes a SECOND stop, after the owner unblocked the
    # job and it stopped again, a new signal rather than a suppressed
    # duplicate (section 16's `job:<id>:waiting-human:<revision>`).
    revision = str(row["attempt"] or 0)

    def build(signal_type, key_parts, severity, **meta):
        return make_signal(
            source_type=SOURCE_CODING, source_id=row["job_id"],
            signal_type=signal_type, created_at=when.isoformat(),
            subject=f"coding job {row['job_id']}",
            summary=goal or f"coding job {row['job_id']}",
            key_parts=key_parts, entity_key=f"job:{row['job_id']}",
            severity=severity, confidence=1.0,
            metadata=dict(
                status=status, review_status=row["review_status"],
                attempt=int(row["attempt"] or 0),
                repository=str(row["repository"] or "")[:80],
                reason=reason, **meta,
            ),
        )

    if status == STATUS_COMPLETED:
        # Section 30: one Discord message, no LINE. A finished job is good
        # news, and good news is never urgent.
        return build(
            SIG_JOB_COMPLETED,
            ("job", row["job_id"], "completed", revision),
            "INFO", result=safe_reason(row["result_summary"]),
        )

    if status == STATUS_WAITING_HUMAN:
        failure = str(row["failure_reason"] or "")
        if REVIEW_LIMIT_MARKER in failure:
            return build(
                SIG_JOB_REVIEW_LIMIT,
                ("job", row["job_id"], "review-cycle-limit", revision),
                "IMPORTANT", limit_reached=True,
            )
        if any(m in failure.lower() for m in UNRECOVERABLE_MARKERS):
            return build(
                SIG_JOB_FAILED_TO_START,
                ("job", row["job_id"], "failed-to-start", revision),
                "IMPORTANT", unrecoverable=True,
            )
        # Section 31: the automation cannot continue, so the owner must.
        return build(
            SIG_JOB_WAITING_HUMAN,
            ("job", row["job_id"], "waiting-human", revision),
            "IMPORTANT", needs_human=int(row["needs_human"] or 0),
        )

    return None
