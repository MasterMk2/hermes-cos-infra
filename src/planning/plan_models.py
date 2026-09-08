"""Plan item / conflict dataclasses, enums and closed vocabularies.

Everything a consumer of `daily_plan_get` may legally see is declared here, so
that the renderer (T3) and the MCP boundary (T5) can validate against the same
constants the planner emits rather than against hand-copied string literals.

Three vocabularies are **closed** and a test pins each one:

* :class:`Category`   — the six display categories, in precedence order.
* :data:`REASONS`     — `reason_for_inclusion`, including the parameterised
  `overdue_<N>d` family (:func:`overdue_reason` / :func:`is_valid_reason`).
* :class:`ConflictKind` + :data:`CONFLICT_DETAILS` — the conflict phrases. The
  detail text is chosen by kind from a fixed table; no model text ever reaches
  it and it never asserts anything about travel time (phase section 30).

Item key order follows I2-CONTRACT.md section 3, with the two keys AMENDMENT 1
section I adds (`due_precision`, `all_day`) slotted next to the field they
qualify: `due_precision` after `due_at`, `all_day` after `scheduled_end_at`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class Category(str, Enum):
    """The six display categories of a daily plan."""

    SCHEDULED = "scheduled"
    MUST_DO = "must_do"
    OVERDUE = "overdue"
    NEXT_UP = "next_up"
    WAITING_FOR = "waiting_for"
    BACKLOG = "backlog"


#: Deduplication precedence (I2-CONTRACT.md section 3, "Deduplication").
#: An item appears in **at most one** category; the earliest match wins.
CATEGORY_PRECEDENCE: tuple = (
    Category.SCHEDULED,
    Category.MUST_DO,
    Category.OVERDUE,
    Category.NEXT_UP,
    Category.WAITING_FOR,
    Category.BACKLOG,
)


class PriorityClass(str, Enum):
    """Deterministic priority classes (phase section 25). No numeric score."""

    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"


# --------------------------------------------------------------------------
# reason_for_inclusion — closed vocabulary (phase section 35)
# --------------------------------------------------------------------------

REASON_SCHEDULED_TODAY = "scheduled_today"
REASON_DUE_TODAY = "due_today"
REASON_DUE_WITHIN_72H = "due_within_72h"
REASON_WAITING = "waiting"
REASON_WAITING_FOLLOWUP = "waiting_followup"
REASON_NO_DATE_BACKLOG = "no_date_backlog"
REASON_SHARED_NEEDS_REVIEW = "shared_needs_review"

#: The fixed members. `overdue_<N>d` is the one parameterised member and is
#: validated by :data:`OVERDUE_REASON_RE` rather than being enumerated.
REASONS: frozenset = frozenset(
    {
        REASON_SCHEDULED_TODAY,
        REASON_DUE_TODAY,
        REASON_DUE_WITHIN_72H,
        REASON_WAITING,
        REASON_WAITING_FOLLOWUP,
        REASON_NO_DATE_BACKLOG,
        REASON_SHARED_NEEDS_REVIEW,
    }
)

OVERDUE_REASON_RE = re.compile(r"^overdue_(0|[1-9][0-9]*)d$")


def overdue_reason(days: int) -> str:
    """`overdue_<N>d` for a whole number of days late.

    ``overdue_0d`` is accepted by the grammar but **the planner cannot produce
    it**, and that is deliberate rather than an oversight. `classify` routes to
    `OVERDUE` only when the deadline falls before the plan day's 00:00, so
    `_overdue_days` is 1 or more by construction. A deadline earlier *today*
    stays in `must_do`: phase section 11 makes "due today" the owner's task for
    today, and a 09:00 deadline read at 18:00 is still today's work — moving it
    to 期限超過 would make the brief worse, not more accurate.

    So `overdue_0d` and the renderer's 「本日期限超過」 branch are a defensive
    tail, not a state to expect. They are kept because they cost nothing and
    would be correct if that routing ever changed; the documentation says
    "unreachable today" instead of describing a state the planner cannot
    produce, and `test_planner_ordering` pins ``overdue_days >= 1``.
    """
    if days < 0:
        raise ValueError(f"overdue days must be >= 0, got {days}")
    return f"overdue_{int(days)}d"


def is_valid_reason(value: Optional[str]) -> bool:
    """True when `value` is a member of the closed vocabulary."""
    if not isinstance(value, str):
        return False
    return value in REASONS or bool(OVERDUE_REASON_RE.match(value))


# --------------------------------------------------------------------------
# Conflicts (phase sections 29, 30)
# --------------------------------------------------------------------------


class ConflictKind(str, Enum):
    OVERLAPPING_SCHEDULED = "overlapping_scheduled"
    DEADLINE_DURING_SCHEDULED = "deadline_during_scheduled"
    DENSE_WINDOW = "dense_window"


#: Fixed closed set of deterministic Japanese phrases, chosen by kind.
#: Phase section 30 caps the claim at "予定が連続しています" — none of these
#: infer travel time or difficulty; they only restate what the timestamps say.
CONFLICT_DETAILS: Dict[str, str] = {
    ConflictKind.OVERLAPPING_SCHEDULED.value: "予定が重なっています",
    ConflictKind.DEADLINE_DURING_SCHEDULED.value: "予定の時間帯に締切があります",
    ConflictKind.DENSE_WINDOW.value: "予定が連続しています",
}

#: The same three kinds, phrased for a window whose end time was **assumed**.
#: AMENDMENT 1 B3 sanctions filling a NULL `scheduled_end_at` with
#: `conflict_default_duration_minutes` (0 production rows have one), but a
#: conflict derived from a guessed duration is not the same fact as one derived
#: from two recorded end times: a 09:00 event assumed to run 60 minutes
#: "overlaps" a 09:30 meeting that a real 15-minute slot would not touch.
#: Phase section 30 forbids asserting more than the data supports, so the
#: assumption is disclosed in the phrase rather than hidden behind an
#: unqualified 「予定が重なっています」. The `kind` vocabulary stays closed at
#: three: this is the same kind of conflict, stated with the confidence the
#: data actually carries.
CONFLICT_DETAILS_ASSUMED_END: Dict[str, str] = {
    ConflictKind.OVERLAPPING_SCHEDULED.value:
        "終了時刻が記録されていないため、予定が重なる可能性があります",
    ConflictKind.DEADLINE_DURING_SCHEDULED.value:
        "終了時刻が記録されていないため、予定の時間帯に締切がある可能性があります",
    ConflictKind.DENSE_WINDOW.value:
        "終了時刻が記録されていないため、予定が連続する可能性があります",
}


# --------------------------------------------------------------------------
# Items
# --------------------------------------------------------------------------

#: Emission order of an Item's keys. `overdue_days` is present only for items
#: in the `overdue` category.
ITEM_KEY_ORDER: tuple = (
    "commitment_id",
    "title",
    "responsibility",
    "work_state",
    "due_at",
    "due_precision",
    "scheduled_at",
    "scheduled_end_at",
    "all_day",
    "counterparty",
    "local_time",
    "priority_class",
    "reason_for_inclusion",
    "source_types",
    "overdue_days",
)


@dataclass
class PlanItem:
    """One display line of a daily plan.

    Every string field is copied verbatim from the canonical DB. The planner
    never rewrites a title, never invents a time, and never normalises a
    timestamp in the output — `due_at` / `scheduled_at` / `scheduled_end_at`
    carry the stored representation, mixed offsets and all (I2-PROFILE.md
    section M). Only `local_time` is derived, and only for display.
    """

    commitment_id: str
    title: str
    responsibility: str
    work_state: str
    due_at: Optional[str] = None
    due_precision: Optional[str] = None
    scheduled_at: Optional[str] = None
    scheduled_end_at: Optional[str] = None
    all_day: bool = False
    counterparty: Optional[str] = None
    local_time: Optional[str] = None
    priority_class: str = PriorityClass.P4.value
    reason_for_inclusion: str = REASON_NO_DATE_BACKLOG
    source_types: List[str] = field(default_factory=list)
    overdue_days: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialise in :data:`ITEM_KEY_ORDER`, omitting `overdue_days` unless set."""
        out: Dict[str, Any] = {}
        for key in ITEM_KEY_ORDER:
            if key == "overdue_days":
                if self.overdue_days is not None:
                    out[key] = int(self.overdue_days)
                continue
            if key == "source_types":
                out[key] = list(self.source_types)
                continue
            out[key] = getattr(self, key)
        return out


@dataclass
class Conflict:
    """A deterministically detected scheduling conflict (phase section 29)."""

    kind: str
    commitment_ids: List[str]
    window_start: Optional[str] = None
    window_end: Optional[str] = None
    #: True when at least one of the windows involved had its end time filled
    #: in from `conflict_default_duration_minutes` rather than read from the
    #: database. It only changes the phrase — see CONFLICT_DETAILS_ASSUMED_END.
    assumed_end: bool = False

    @property
    def detail(self) -> str:
        if self.assumed_end:
            return CONFLICT_DETAILS_ASSUMED_END[self.kind]
        return CONFLICT_DETAILS[self.kind]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "commitment_ids": sorted(self.commitment_ids),
            "window_start": self.window_start,
            "window_end": self.window_end,
            "detail": self.detail,
        }


#: The top-level keys a plan always carries. `warnings` is omitted entirely
#: when empty (I2-CONTRACT.md section 3).
PLAN_REQUIRED_KEYS: tuple = (
    "date",
    "timezone",
    "generated_at",
    "scheduled",
    "must_do",
    "overdue",
    "next_up",
    "waiting_for",
    "backlog",
    "conflicts",
    "counts",
    "source_freshness",
    "plan_hash",
)

#: Keys of the `counts` block. The six categories plus `conflicts` come from
#: the contract; `filtered_noise` from AMENDMENT 1 section I; and
#: `unknown_work_state` implements AMENDMENT 1 section D's requirement that an
#: unrecognised `work_state` is loud rather than silently dropped.
COUNTS_KEYS: tuple = (
    "scheduled",
    "must_do",
    "overdue",
    "next_up",
    "waiting_for",
    "backlog",
    "conflicts",
    "filtered_noise",
    "unknown_work_state",
    # Additive. Every eligible row lands in exactly one of the buckets above or
    # in one of these two, so `sum(totals)` is the eligible row count and no
    # row can leave the planner uncounted.
    "deduplicated",
    "not_categorised",
)

WARNING_DATA_FRESHNESS = "data_freshness_warning"
WARNING_UNKNOWN_WORK_STATE = "unknown_work_state"

#: The closed set of warning identifiers the planner can put in `warnings`.
#: `renderer.WARNING_PHRASE` must carry a Japanese phrase for every one of
#: them; a test asserts it, because the renderer's fallback branch prints the
#: identifier itself and an English token in the 08:30 brief is not the section
#: 41 shape.
WARNINGS: frozenset = frozenset({WARNING_DATA_FRESHNESS, WARNING_UNKNOWN_WORK_STATE})
