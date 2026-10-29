"""Agenda projection engine for Personal Chief of Staff."""

import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any, Set, Tuple

from .models import (
    Commitment,
    Responsibility,
    WorkState,
    TemporalState,
    parse_iso,
)
from .store import list_commitments

JST = timezone(timedelta(hours=9))

#: The planner's defaults (planning.plan_config `windows`); see NEEDS REVIEW
#: and AGE in planning/planner.py. A waiting row, or an unassigned group
#: request (SHARED + BLOCKED), older than this with no date ahead is only
#: counted.
WAITING_MAX_AGE_DAYS = 30
NEEDS_REVIEW_DAYS = 14

#: Loaded one state at a time: the table also holds every finished row, so a
#: single capped query ordered by date cut off the undated active ones.
ACTIVE_WORK_STATES = (
    WorkState.ACTIONABLE.value,
    WorkState.WAITING.value,
    WorkState.SCHEDULED.value,
    WorkState.BLOCKED.value,
)
ACTIVE_LOAD_LIMIT = 100000


def _evidence_facts(conn: sqlite3.Connection) -> Tuple[Dict[str, datetime], Set[str]]:
    """Per commitment: the earliest `origin` time (when the message was
    written), and whether any evidence is not Gmail. The planner treats a
    SHARED row with Gmail evidence only as noise, not as a request."""
    origins: Dict[str, datetime] = {}
    non_gmail: Set[str] = set()
    rows = conn.execute(
        "SELECT commitment_id, source_type, evidence_role, observed_at FROM commitment_evidence"
    ).fetchall()
    for commitment_id, source_type, role, observed_at in rows:
        if source_type != "gmail":
            non_gmail.add(commitment_id)
        observed = parse_iso(observed_at) if role == "origin" else None
        if observed is not None and (commitment_id not in origins or observed < origins[commitment_id]):
            origins[commitment_id] = observed
    return origins, non_gmail


def _aged_out(cmt: Commitment, born: Optional[datetime], now: datetime, max_age_days: int) -> bool:
    """Older than `max_age_days` and with no date left today or later."""
    born = born or parse_iso(cmt.created_at)
    if born is None or now - born <= timedelta(days=max_age_days):
        return False
    ahead = parse_iso(cmt.due_at) or parse_iso(cmt.scheduled_at)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return ahead is None or ahead < today_start


def priority_score(cmt: Commitment, now: datetime) -> float:
    """Calculate deterministic priority score for ordering agenda items.

    Higher score = higher priority in the agenda.
    Factors:
    - Overdue / Proximity to deadline (up to +50 points)
    - Responsibility: SELF (+20) > SHARED (+10) > COUNTERPARTY (+5)
    - WorkState: ACTIONABLE (+15) > WAITING (+10) > SCHEDULED (+5)
    """
    score = 0.0

    # Responsibility weight
    if cmt.responsibility == Responsibility.SELF.value:
        score += 20.0
    elif cmt.responsibility == Responsibility.SHARED.value:
        score += 10.0
    elif cmt.responsibility == Responsibility.COUNTERPARTY.value:
        score += 5.0

    # Work state weight
    if cmt.work_state == WorkState.ACTIONABLE.value:
        score += 15.0
    elif cmt.work_state == WorkState.WAITING.value:
        score += 10.0
    elif cmt.work_state == WorkState.SCHEDULED.value:
        score += 5.0

    # Temporal proximity
    target_iso = cmt.due_at or cmt.scheduled_at
    dt = parse_iso(target_iso)
    if dt:
        diff_hours = (dt - now).total_seconds() / 3600.0
        if diff_hours < 0:
            # Overdue: higher score for recently overdue
            score += 50.0 + max(0.0, 20.0 - abs(diff_hours) / 24.0)
        elif diff_hours <= 24.0:
            # Due in next 24h
            score += 40.0 + (24.0 - diff_hours)
        elif diff_hours <= 72.0:
            # Due in 1-3 days
            score += 20.0 + (72.0 - diff_hours) / 5.0
        else:
            score += 5.0

    return score


def generate_agenda(
    conn: sqlite3.Connection,
    now: Optional[datetime] = None,
    window_days: int = 7,
    view: Optional[str] = None,
    limit_per_view: Optional[int] = None,
    waiting_max_age_days: int = WAITING_MAX_AGE_DAYS,
    needs_review_days: int = NEEDS_REVIEW_DAYS,
) -> Dict[str, Any]:
    """Generate Chief of Staff agenda projection across Today, Soon, Overdue, Waiting, and Scheduled."""
    eff_limit = limit_per_view if limit_per_view is not None else (10 if view else 2)
    if now is None:
        now = datetime.now(JST)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=JST)

    # End of today in JST
    today_end = datetime(now.year, now.month, now.day, 23, 59, 59, tzinfo=now.tzinfo)
    soon_end = now + timedelta(days=window_days)

    all_active: List[Commitment] = []
    for state in ACTIVE_WORK_STATES:
        all_active.extend(list_commitments(conn, work_state=state, limit=ACTIVE_LOAD_LIMIT, now=now))
    origin_times, non_gmail = _evidence_facts(conn)
    needs_review_count = 0
    aged_out_count = 0

    today_items: List[Dict[str, Any]] = []
    soon_items: List[Dict[str, Any]] = []
    overdue_items: List[Dict[str, Any]] = []
    waiting_items: List[Dict[str, Any]] = []
    scheduled_items: List[Dict[str, Any]] = []

    for cmt in all_active:
        if cmt.work_state in (WorkState.DONE.value, WorkState.CANCELLED.value, WorkState.DISMISSED.value):
            continue
        if cmt.currentness == "superseded":
            continue

        c_dict = {
            "commitment_id": cmt.commitment_id,
            "title": cmt.title,
            "responsibility": cmt.responsibility,
            "work_state": cmt.work_state,
            "priority_score": round(priority_score(cmt, now), 1),
        }
        if cmt.due_at:
            c_dict["due_at"] = cmt.due_at
        if cmt.scheduled_at:
            c_dict["scheduled_at"] = cmt.scheduled_at
        if cmt.counterparty:
            c_dict["counterparty"] = cmt.counterparty

        # 1. Waiting-for. An unassigned group request (SHARED + BLOCKED) is
        # not a reply anybody waits for; it is only counted, as in the planner.
        needs_review = (cmt.responsibility == Responsibility.SHARED.value
                        and cmt.work_state == WorkState.BLOCKED.value)
        waiting = not needs_review and (
            cmt.work_state in (WorkState.WAITING.value, WorkState.BLOCKED.value)
            or cmt.responsibility == Responsibility.COUNTERPARTY.value
        )
        if needs_review or waiting:
            max_age = needs_review_days if needs_review else waiting_max_age_days
            if _aged_out(cmt, origin_times.get(cmt.commitment_id), now, max_age):
                aged_out_count += 1
                continue
        if waiting:
            waiting_items.append(c_dict)
            continue
        if needs_review and cmt.commitment_id in non_gmail:
            needs_review_count += 1

        # 2. Scheduled
        if cmt.scheduled_at:
            scheduled_items.append(c_dict)

        # A group task or unknown assignee is not the owner's to-do list.
        if cmt.responsibility != Responsibility.SELF.value:
            continue

        # 3. Temporal classifications
        dt_due = parse_iso(cmt.due_at)
        dt_sched = parse_iso(cmt.scheduled_at)
        dt_primary = dt_due or dt_sched

        if dt_due and dt_due < now and cmt.work_state in (WorkState.ACTIONABLE.value, WorkState.WAITING.value):
            overdue_items.append(c_dict)
        elif dt_primary:
            if dt_primary <= today_end or (dt_primary - now).total_seconds() <= 24 * 3600:
                today_items.append(c_dict)
            elif dt_primary <= soon_end:
                soon_items.append(c_dict)
        elif cmt.work_state == WorkState.ACTIONABLE.value and cmt.responsibility == Responsibility.SELF.value:
            # Actionable without explicit date belongs on today's backlog
            today_items.append(c_dict)

    # Sort each section by priority score descending
    today_items.sort(key=lambda x: x["priority_score"], reverse=True)
    soon_items.sort(key=lambda x: x["priority_score"], reverse=True)
    overdue_items.sort(key=lambda x: x["priority_score"], reverse=True)
    waiting_items.sort(key=lambda x: x["priority_score"], reverse=True)
    scheduled_items.sort(key=lambda x: x.get("scheduled_at") or "")

    summary = {
        "today_count": len(today_items),
        "soon_count": len(soon_items),
        "overdue_count": len(overdue_items),
        "waiting_count": len(waiting_items),
        "scheduled_count": len(scheduled_items),
        "needs_review_count": needs_review_count,
        "aged_out_count": aged_out_count,
    }

    view_norm = (view or "").strip().lower()
    if view_norm in ("waiting", "waiting_for"):
        return {
            "as_of": now.isoformat(),
            "summary": summary,
            "waiting_for": waiting_items[:eff_limit],
        }
    if view_norm in ("today", "soon", "overdue", "scheduled"):
        items_map = {
            "today": today_items,
            "soon": soon_items,
            "overdue": overdue_items,
            "scheduled": scheduled_items,
        }
        return {
            "as_of": now.isoformat(),
            "summary": summary,
            view_norm: items_map[view_norm][:eff_limit],
        }

    return {
        "as_of": now.isoformat(),
        "summary": summary,
        "today": today_items[:eff_limit],
        "soon": soon_items[:eff_limit],
        "overdue": overdue_items[:eff_limit],
        "waiting_for": waiting_items[:eff_limit],
        "scheduled": scheduled_items[:eff_limit],
    }
