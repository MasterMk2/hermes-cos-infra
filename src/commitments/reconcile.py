"""Evidence reconciliation, calendar authority, and idempotent backfill engine."""

import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple

from .models import (
    Commitment,
    CommitmentEvidence,
    Responsibility,
    WorkState,
    EvidenceRole,
    Currentness,
    Authority,
    parse_iso,
)
from .store import (
    save_commitment,
    add_evidence,
    get_commitment,
)

# Automated mailer, newsletter, and promotional keywords
NOISE_KEYWORDS = [
    "ニュースレター", "newsletter", "メルマガ", "ウィークリーメール", "おすすめライブ配信",
    "セール", "クーポン", "ポイント", "配信", "新着", "新着記事", "新着商品",
    "セキュリティ通知", "security alert", "ログイン通知", "login notification",
    "ご登録", "会員登録", "仮登録", "登録完了", "振込手続完了", "領収書", "receipt",
    "bugzilla: confirm", "personal access token", "oauth application has been added",
    "今日の予定リスト", "予定されている予定はありません", "定期診断", "報告書",
    "アンケート", "満足度調査", "研修動画", "社労士コラム", "究極ドリル",
]


def is_automated_noise(source_type: str, summary: str, counterparty: Optional[str] = None) -> bool:
    """Detect if an event is an automated newsletter, notification, or transactional notice."""
    if source_type != "gmail":
        return False

    text = (summary or "").lower()
    for kw in NOISE_KEYWORDS:
        if kw.lower() in text:
            return True

    cp = (counterparty or "").lower()
    if any(bot in cp for bot in ["noreply", "no-reply", "notification", "mailmag", "info@", "sender@", "service-jp"]):
        # Automated sender
        return True

    return False


def classify_event_semantics(row: sqlite3.Row) -> Tuple[str, str, float]:
    """Classify (responsibility, work_state, confidence) from raw event attributes."""
    source_type = row["source_type"] or ""
    summary = row["summary"] or ""
    actor = row["actor"] or ""
    status = row["status"] or ""
    counterparty = row["counterparty"] or ""
    kind = row["type"] or ""
    review = bool(row["needs_human_review"]) if "needs_human_review" in row.keys() else False

    if status in {"resolved", "done", "closed"}:
        return (Responsibility.SHARED.value, WorkState.DONE.value, 0.90)
    if status in {"ignored", "cancelled"}:
        return (Responsibility.SHARED.value, WorkState.CANCELLED.value, 0.90)

    # 1. Check noise/informational first
    if is_automated_noise(source_type, summary, counterparty):
        return (Responsibility.INFORMATIONAL.value, WorkState.DISMISSED.value, 0.95)

    # 2. Check superseded
    if status == "superseded":
        return (Responsibility.SHARED.value, WorkState.CANCELLED.value, 0.90)

    # Actor identifies the speaker, not the assignee. Only promises made by
    # the owner establish ownership; a request made by the owner is waiting.
    if actor == "unknown" or review:
        return (Responsibility.SHARED.value, WorkState.BLOCKED.value, 0.40)
    if actor == "user" and kind == "request":
        return (Responsibility.COUNTERPARTY.value, WorkState.WAITING.value, 0.85)
    if actor == "user" and kind in {"commitment", "deadline", "follow_up"}:
        return (Responsibility.SELF.value, WorkState.ACTIONABLE.value, 0.85)
    if actor == "counterparty" and kind == "commitment":
        return (Responsibility.COUNTERPARTY.value, WorkState.WAITING.value, 0.85)

    # 5. Check waiting-for in Discord/LINE
    # If summary indicates waiting for external input or another person's commitment
    if any(w in summary for w in ["返信待ち", "確認待ち", "連絡待ち", "返信予定", "連絡する約束", "作ると約束", "対応すると約束"]):
        return (Responsibility.COUNTERPARTY.value, WorkState.WAITING.value, 0.80)

    # Received requests can address somebody else in a group. Neither an
    # imperative verb nor an open status proves that the owner is assigned.
    if kind in {"request", "deadline", "follow_up"} or status in {"open", "candidate"}:
        return (Responsibility.SHARED.value, WorkState.BLOCKED.value, 0.40)

    return (Responsibility.INFORMATIONAL.value, WorkState.DISMISSED.value, 0.50)


def generate_canonical_id(source_type: str, source_id: str) -> str:
    """Generate a stable, deterministic commitment ID from the primary source."""
    seed = f"{source_type}:{source_id}".encode("utf-8")
    return f"cmt_{hashlib.sha256(seed).hexdigest()[:12]}"


def reconcile_event_row(row: sqlite3.Row) -> Commitment:
    """Map a raw event row to a canonical Commitment object."""
    event_id = row["event_id"]
    source_type = row["source_type"] or "event_layer"
    source_id = row["source_id"] or event_id
    summary = row["summary"] or ""
    due_at = row["due_at"]
    due_precision = row["due_precision"]
    counterparty = row["counterparty"]
    project_id = row["project_id"]
    created_at = row["created_at"]
    updated_at = row["updated_at"]
    resolved_at = row["resolved_at"]

    responsibility, work_state, confidence = classify_event_semantics(row)

    cid = generate_canonical_id(source_type, source_id)

    ev = CommitmentEvidence(
        source_type=source_type,
        source_id=source_id,
        observed_at=row["source_timestamp"] or row["created_at"] or datetime.now(timezone.utc).isoformat(),
        evidence_role=EvidenceRole.ORIGIN.value,
        confidence=float(row["confidence"] or 1.0),
        snippet=summary[:200],
    )

    currentness = Currentness.SUPERSEDED.value if row["status"] == "superseded" else Currentness.CURRENT.value

    return Commitment(
        commitment_id=cid,
        title=summary,
        summary=row["classification_reason"] or summary,
        responsibility=responsibility,
        work_state=work_state,
        counterparty=counterparty,
        project_id=project_id,
        due_at=due_at,
        due_precision=due_precision,
        authority=Authority.EVIDENCE.value,
        confidence=confidence,
        currentness=currentness,
        notes=f"Ingested from raw event {event_id}",
        created_at=created_at,
        updated_at=updated_at,
        resolved_at=resolved_at,
        source_count=1,
        evidence=[ev],
    )


#: Event ids and title tag of calendar tentative registration
#: (src/calendar-tentative, ct_select.OWN_EVENT_PREFIX / summary_prefix).
TENTATIVE_EVENT_PREFIX = "hermesct"
TENTATIVE_TITLE_TAG = "【仮】"


def _tentative_entry(cal_row) -> bool:
    """An unconfirmed (or withdrawn) entry of calendar tentative registration.

    It was made from a commitment, so backfilling it would list that work
    twice. Google may drop the title of a deleted event, so a cancelled
    entry counts regardless. One the owner confirmed (【仮】 removed) is an
    ordinary calendar event."""
    if not str(cal_row["event_id"] or "").startswith(TENTATIVE_EVENT_PREFIX):
        return False
    return (str(cal_row["status"] or "").lower() == "cancelled"
            or str(cal_row["summary"] or "").lstrip().startswith(TENTATIVE_TITLE_TAG))


def reconcile_calendar_event(cal_row: sqlite3.Row) -> Commitment:
    """Map an authoritative calendar event to a canonical Commitment."""
    cal_id = cal_row["source_id"] or cal_row["event_id"]
    summary = cal_row["summary"] or "Untitled Calendar Event"
    start_at = cal_row["start_at"]
    end_at = cal_row["end_at"]
    status = (cal_row["status"] or "confirmed").lower()
    cid = generate_canonical_id("calendar", cal_id)

    work_state = WorkState.CANCELLED.value if status == "cancelled" else WorkState.SCHEDULED.value

    # If title has 【締切】 or similar deadline indicator, also set due_at
    due_at = None
    if "【締切】" in summary or "締切" in summary or "期限" in summary:
        due_at = start_at

    # Classes, exams, meetings
    responsibility = Responsibility.SELF.value
    if any(w in summary for w in ["同窓会", "クラスアワー", "合宿", "テスト"]):
        responsibility = Responsibility.SHARED.value

    ev = CommitmentEvidence(
        source_type="calendar",
        source_id=cal_id,
        observed_at=cal_row["updated_at"] or datetime.now(timezone.utc).isoformat(),
        evidence_role=EvidenceRole.SCHEDULE.value,
        confidence=1.0,
        snippet=f"Calendar: {summary} ({start_at} - {end_at})",
    )

    return Commitment(
        commitment_id=cid,
        title=summary,
        summary=cal_row["description"] or summary,
        responsibility=responsibility,
        work_state=work_state,
        due_at=due_at,
        due_precision="exact_datetime" if start_at and "T" in start_at else "day",
        scheduled_at=start_at,
        scheduled_end_at=end_at,
        authority=Authority.CALENDAR_AUTHORITY.value,
        confidence=1.0,
        currentness=Currentness.CURRENT.value,
        notes="Authoritative calendar schedule",
        created_at=cal_row["updated_at"],
        updated_at=cal_row["updated_at"],
        source_count=1,
        evidence=[ev],
    )


def merge_commitments(
    conn: sqlite3.Connection,
    target_id: str,
    source_id: str,
    actor: str = "system",
    reason: str = "cross_source_merge",
) -> Optional[Commitment]:
    """Merge source commitment into target commitment with complete audit trail."""
    target = get_commitment(conn, target_id)
    source = get_commitment(conn, source_id)
    if target is None or source is None or target_id == source_id:
        return None

    # Transfer evidence
    for ev in source.evidence:
        ev_copy = CommitmentEvidence(
            source_type=ev.source_type,
            source_id=ev.source_id,
            observed_at=ev.observed_at,
            evidence_role=ev.evidence_role,
            confidence=ev.confidence,
            snippet=ev.snippet,
        )
        add_evidence(conn, target_id, ev_copy, auto_commit=False)

    # Authority and date resolution:
    # If source has calendar authority, target gains calendar authority and scheduled times
    if source.authority == Authority.CALENDAR_AUTHORITY.value:
        target.authority = Authority.CALENDAR_AUTHORITY.value
        target.scheduled_at = source.scheduled_at
        target.scheduled_end_at = source.scheduled_end_at
        if source.work_state == WorkState.CANCELLED.value:
            target.work_state = WorkState.CANCELLED.value

    # Due date: if target has no due_at and source has one, inherit it
    if not target.due_at and source.due_at:
        target.due_at = source.due_at
        target.due_precision = source.due_precision

    # Mark source as superseded/cancelled
    source.work_state = WorkState.CANCELLED.value
    source.currentness = Currentness.SUPERSEDED.value
    source.notes = (source.notes or "") + f" | Merged into {target_id}"
    save_commitment(conn, source, actor=actor, reason=f"merged_into:{target_id}", auto_commit=False)

    # Save target updates
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM commitment_evidence WHERE commitment_id = ?", (target_id,))
    target.source_count = cur.fetchone()[0]
    save_commitment(conn, target, actor=actor, reason=f"{reason}:{source_id}", auto_commit=False)
    conn.commit()
    return get_commitment(conn, target_id)


def run_backfill(
    events_conn: sqlite3.Connection,
    calendar_conn: Optional[sqlite3.Connection] = None,
    actor: str = "backfill",
) -> Dict[str, Any]:
    """Run idempotent baseline backfill from existing events.db and calendar_events."""
    events_conn.row_factory = sqlite3.Row
    cur = events_conn.cursor()

    cur.execute("SELECT * FROM events")
    event_rows = cur.fetchall()

    processed_events = 0
    dismissed_count = 0
    actionable_count = 0
    waiting_count = 0

    for row in event_rows:
        cmt = reconcile_event_row(row)
        save_commitment(events_conn, cmt, actor=actor, reason="initial_backfill", auto_commit=False)
        processed_events += 1
        if cmt.work_state == WorkState.DISMISSED.value:
            dismissed_count += 1
        elif cmt.work_state == WorkState.ACTIONABLE.value:
            actionable_count += 1
        elif cmt.work_state == WorkState.WAITING.value:
            waiting_count += 1

    # Backfill calendar events if provided
    cal_count = 0
    if calendar_conn:
        calendar_conn.row_factory = sqlite3.Row
        cal_cur = calendar_conn.cursor()
        cal_cur.execute("SELECT * FROM calendar_events")
        for cal_row in cal_cur.fetchall():
            if _tentative_entry(cal_row):
                continue
            cal_cmt = reconcile_calendar_event(cal_row)
            save_commitment(events_conn, cal_cmt, actor=actor, reason="calendar_backfill", auto_commit=False)
            cal_count += 1

    events_conn.commit()

    return {
        "status": "completed",
        "processed_events": processed_events,
        "calendar_events_added": cal_count,
        "dismissed_noise": dismissed_count,
        "actionable_commitments": actionable_count,
        "waiting_commitments": waiting_count,
    }
