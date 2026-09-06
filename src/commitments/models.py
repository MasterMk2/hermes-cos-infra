"""Data models, enums, and temporal state derivation for Unified Commitments."""

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Optional, List, Dict, Any


class Responsibility(str, Enum):
    SELF = "SELF"                  # User is directly responsible for performing the task
    COUNTERPARTY = "COUNTERPARTY"  # External party is responsible (user waiting on them)
    SHARED = "SHARED"              # Group/team shared responsibility
    INFORMATIONAL = "INFORMATIONAL"# Automated newsletter, notification, receipt, non-actionable


class WorkState(str, Enum):
    ACTIONABLE = "ACTIONABLE"      # Pending task requiring action
    WAITING = "WAITING"            # Waiting for reply/review/dependency
    SCHEDULED = "SCHEDULED"        # Explicit time slot reserved on calendar
    BLOCKED = "BLOCKED"            # Blocked by prerequisite
    DONE = "DONE"                  # Completed
    CANCELLED = "CANCELLED"        # Cancelled/abandoned
    DISMISSED = "DISMISSED"        # Filtered noise/mailer


class TemporalState(str, Enum):
    NO_DATE = "NO_DATE"
    FUTURE = "FUTURE"
    DUE_SOON = "DUE_SOON"
    OVERDUE = "OVERDUE"


class EvidenceRole(str, Enum):
    ORIGIN = "origin"
    SCHEDULE = "schedule"
    CONFIRMATION = "confirmation"
    AMENDMENT = "amendment"
    COMPLETION = "completion"
    CANCELLATION = "cancellation"
    CONTEXT = "context"


class Currentness(str, Enum):
    CURRENT = "current"
    SUPERSEDED = "superseded"
    UNCERTAIN = "uncertain"


class Authority(str, Enum):
    USER_EXPLICIT = "user_explicit"
    CALENDAR_AUTHORITY = "calendar_authority"
    EVIDENCE = "evidence"


def parse_iso(ts_str: Optional[str]) -> Optional[datetime]:
    if not ts_str:
        return None
    try:
        # Handle trailing Z or offsets
        s = ts_str.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            # Default to UTC if naive
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def compute_temporal_state(
    due_at: Optional[str],
    scheduled_at: Optional[str] = None,
    work_state: Optional[str] = None,
    now: Optional[datetime] = None,
    soon_threshold_hours: float = 48.0,
) -> str:
    """Dynamically derive temporal state without mutating DB state.

    Rules:
    1. If work_state in ('DONE', 'CANCELLED', 'DISMISSED'), temporal state is irrelevant (return NO_DATE/FUTURE).
    2. Overdue applies only if due_at is in the past AND work_state is active (ACTIONABLE, WAITING, SCHEDULED).
    3. If due_at or scheduled_at is within `soon_threshold_hours` from now, it is DUE_SOON.
    4. If due_at or scheduled_at is further in the future, it is FUTURE.
    5. If neither is set, it is NO_DATE.
    """
    if work_state in (WorkState.DONE.value, WorkState.CANCELLED.value, WorkState.DISMISSED.value):
        return TemporalState.NO_DATE.value

    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    dt_due = parse_iso(due_at)
    dt_sched = parse_iso(scheduled_at)

    if dt_due is None and dt_sched is None:
        return TemporalState.NO_DATE.value

    # Check overdue on deadline first
    if dt_due is not None and dt_due < now:
        return TemporalState.OVERDUE.value

    # Check due_soon on whichever date is earlier
    target_dt = None
    if dt_due and dt_sched:
        target_dt = min(dt_due, dt_sched)
    else:
        target_dt = dt_due or dt_sched

    if target_dt is not None:
        diff = (target_dt - now).total_seconds()
        if diff < 0:
            # Scheduled time in the past without explicit due date
            return TemporalState.OVERDUE.value
        elif diff <= soon_threshold_hours * 3600.0:
            return TemporalState.DUE_SOON.value
        else:
            return TemporalState.FUTURE.value

    return TemporalState.NO_DATE.value


@dataclass
class CommitmentEvidence:
    source_type: str
    source_id: str
    observed_at: str
    evidence_role: str = EvidenceRole.ORIGIN.value
    confidence: float = 1.0
    snippet: Optional[str] = None
    id: Optional[int] = None
    commitment_id: Optional[str] = None
    created_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "commitment_id": self.commitment_id,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "observed_at": self.observed_at,
            "evidence_role": self.evidence_role,
            "confidence": self.confidence,
            "snippet": self.snippet,
            "created_at": self.created_at,
        }


@dataclass
class Commitment:
    commitment_id: str
    title: str
    responsibility: str
    work_state: str
    summary: Optional[str] = None
    counterparty: Optional[str] = None
    project_id: Optional[str] = None
    due_at: Optional[str] = None
    due_precision: Optional[str] = None
    scheduled_at: Optional[str] = None
    scheduled_end_at: Optional[str] = None
    authority: str = Authority.EVIDENCE.value
    confidence: float = 1.0
    currentness: str = Currentness.CURRENT.value
    notes: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    resolved_at: Optional[str] = None
    source_count: int = 1
    evidence: List[CommitmentEvidence] = field(default_factory=list)

    def get_temporal_state(self, now: Optional[datetime] = None) -> str:
        return compute_temporal_state(
            due_at=self.due_at,
            scheduled_at=self.scheduled_at,
            work_state=self.work_state,
            now=now,
        )

    def to_dict(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        return {
            "commitment_id": self.commitment_id,
            "title": self.title,
            "summary": self.summary,
            "responsibility": self.responsibility,
            "work_state": self.work_state,
            "temporal_state": self.get_temporal_state(now),
            "counterparty": self.counterparty,
            "project_id": self.project_id,
            "due_at": self.due_at,
            "due_precision": self.due_precision,
            "scheduled_at": self.scheduled_at,
            "scheduled_end_at": self.scheduled_end_at,
            "authority": self.authority,
            "confidence": self.confidence,
            "currentness": self.currentness,
            "notes": self.notes,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "resolved_at": self.resolved_at,
            "source_count": self.source_count,
            "evidence": [e.to_dict() for e in self.evidence],
        }

    def to_summary_dict(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        sources = list(dict.fromkeys(e.source_type for e in self.evidence)) if self.evidence else []
        return {
            "commitment_id": self.commitment_id,
            "title": self.title,
            "responsibility": self.responsibility,
            "work_state": self.work_state,
            "temporal_state": self.get_temporal_state(now),
            "due_at": self.due_at,
            "scheduled_at": self.scheduled_at,
            "counterparty": self.counterparty,
            "project_id": self.project_id,
            "source_types": sources,
        }
