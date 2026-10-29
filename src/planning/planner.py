"""Deterministic daily planner — `build_daily_plan()`.

This module selects and ranks facts. It does not interpret them. There is no
score, no model, and no heuristic the production data does not support (phase
sections 22, 24, 26, 75): `planner.py` imports no LLM or provider code at all,
so a daily plan is still produced when every model in the fallback chain is
down. Everything below is stated as an explicit rule because the whole value of
this layer is that tomorrow's plan can be explained by reading it.

--------------------------------------------------------------------------
CATEGORY PRECEDENCE (phase section 21)
--------------------------------------------------------------------------
An item appears in **at most one** category. Highest first::

    scheduled > must_do > overdue > next_up > waiting_for > backlog

--------------------------------------------------------------------------
TIE-BREAKERS (phase section 27) — inside a category, in this exact order
--------------------------------------------------------------------------
    1. scheduled_at ascending  (nulls last)
    2. due_at       ascending  (nulls last)
    3. priority_class ascending  (P0 < P1 < ...)
    4. created_at   ascending  (nulls last)
    5. commitment_id ascending   <- final, guarantees a total order

Every comparison is made on *parsed, timezone-aware* datetimes. Lexicographic
comparison of the stored strings is wrong here: the same column carries
``+09:00`` on Discord/LINE/Gmail rows, ``+00:00`` on Calendar-derived rows and a
bare ``YYYY-MM-DD`` on all-day events, so string order mis-ranks a calendar row
against a Discord row by nine hours (I2-PROFILE.md section M).

**One category overrides the generic order — `overdue`** (AMENDMENT 1 F1)::

    overdue: due_at DESC (most recent deadline first)
             then a per-counterparty cap of 2 inside the shown set
             then commitment_id ASC as the final total-order key

Oldest-first was the obvious rule and it is the wrong one here: 10 of the 23
production overdue items are 8-14 days old and most share a single Discord
thread with three counterparties, so oldest-first would show the same five
stale lines every morning forever and phase section 12's "棚卸し可能" would
never happen. Most-recent-first plus counterparty diversity keeps the list
moving. `backlog` is rotated for the same reason (AMENDMENT 1 F2): three of
47 is a 6% sample, so the shown slice is rotated by an offset derived from the
plan date — same date always yields the same three items, which `plan_hash`
idempotency (phase section 58) requires, and the next date yields the next
three.

--------------------------------------------------------------------------
ELIGIBILITY — the noise filter (AMENDMENT 1 section C)
--------------------------------------------------------------------------
Universal exclusions first: `DONE`/`CANCELLED`/`DISMISSED` work states,
`INFORMATIONAL` responsibility, and `currentness = 'superseded'` rows never
appear anywhere.

Then the filter that decides whether this layer is useful at all. `SHARED` +
`ACTIONABLE` is 196 production rows, **194 of them Gmail newsletters and
campaigns**. Those are mail subjects, not tasks. So::

    scheduled — responsibility eligibility only:

        responsibility == SELF
      OR
        responsibility == SHARED
        AND its evidence source_types intersect {discord, line, calendar}
        AND (
              it carries a scheduled_at        # a fixed calendar slot
           OR (work_state == ACTIONABLE AND due_at IS NOT NULL)
        )

    must_do / overdue / next_up / backlog — the same, PLUS, for every
    responsibility including SELF:

        its evidence source_types intersect {discord, line, calendar}

    A SHARED row with non-Gmail evidence that is not action-eligible is
    counted in `counts.needs_review` and never rendered — see NEEDS REVIEW
    below.

    Any row the evidence test alone excluded is counted in
    `counts.filtered_noise` and never rendered.

This is a *filter*, not a judgement: it keys off `commitment_evidence
.source_type`, which is canonical. The split between the `scheduled` branch and
the deadline branch is the subject of the coordinator ruling recorded below
under SHARED ELIGIBILITY; read that before changing either line.

--------------------------------------------------------------------------
EVIDENCE, NOT RESPONSIBILITY, for the deadline categories (RULING 1)
--------------------------------------------------------------------------
AMENDMENT 1 section C keyed the Gmail exclusion on `responsibility == SHARED`.
Production carries `SELF` + `ACTIONABLE` rows whose only evidence is Gmail — a
weekly horoscope newsletter is one of them, carrying a real `due_at` — and the
AMENDMENT F1 ordering (`due_at DESC`) puts the newest of them at the *top* of
期限超過. A horoscope as line 1 of the owner's overdue list is the single most
likely reason to distrust the first brief, and it is also the shape AMENDMENT J
promises the renderer will never see.

So the evidence test is responsibility-agnostic for `must_do`, `overdue`,
`next_up` and `backlog`. `scheduled` keeps the rule above: a slot on the
owner's own calendar is on the owner's day (phase section 20), and no calendar
row is Gmail-only anyway.

What this costs, stated rather than hidden: a genuine Gmail-only deadline —
the profile's `formrun` due-soon row — is dropped from the brief too. A
Gmail-only deadline row cannot be told apart from a newsletter by any signal
phase section 24 permits, and every excluded row is still fully reachable on
the on-demand path through `commitment_list` / `commitment_search`. Nothing
leaves the system; it leaves the daily brief, which is what phase section 102
asks for. The exclusions are counted in `counts.filtered_noise`, so the
decision is auditable rather than silent — but `filtered_noise` lives in the
plan JSON only and is deliberately never rendered: a daily
「194件を除外しました」 line would itself be noise.

--------------------------------------------------------------------------
DEDUPLICATION (AMENDMENT 1 section E) — three rules, in order, and no others
--------------------------------------------------------------------------
1. **Shared evidence source_id.** Two commitments sharing a
   ``(source_type, source_id)`` evidence pair are the same canonical item.
   Exact; zero false positives; one production hit. Survivor: the
   `calendar_authority` row, else the higher `source_count`, else the
   lexicographically smaller `commitment_id`.
2. **Identical scheduling window.** The same raw ``(scheduled_at,
   scheduled_end_at)`` pair where both rows are `calendar_authority`. Exact;
   two production hits, both true positives. Survivor: the **longer title** (it
   carries the extra qualifier), then the more recent `updated_at`, then the
   smaller `commitment_id`.
3. **Calendar-authority precedence** (phase section 20). A commitment carrying
   both calendar and non-calendar evidence is calendar-authoritative: its
   `scheduled_at` wins and its `due_at` is ignored for category selection, so a
   stale Gmail/LINE time never reaches the brief. Fires on 0 production rows
   today and is covered by a synthetic fixture.

**Title similarity is never used.** A `SequenceMatcher >= 0.75` sweep produces
268 false-positive pairs from newsletter families, while both real duplicates
score *below* that threshold — strictly worse than useless. Dedup runs **before**
conflict detection: the only two window overlaps in the entire dataset are the
duplicate pairs, so skipping that order reports a phantom conflict.

--------------------------------------------------------------------------
BLOCKED (AMENDMENT 1 section D)
--------------------------------------------------------------------------
`BLOCKED` maps to `waiting_for` — semantically "I cannot act on this yet" is
exactly the waiting contract of phase sections 14/15 — and never to `must_do`
or `next_up`. The exception is `SHARED` + `BLOCKED`, below. It is never
silently dropped. Any work_state this module does not recognise is counted in
``counts.unknown_work_state`` and logged at WARNING: silent data loss is the
worst failure mode a planning layer has.

--------------------------------------------------------------------------
NEEDS REVIEW and AGE (owner semantics v2)
--------------------------------------------------------------------------
Owner semantics v2 in `commitments.reconcile` writes `SHARED` + `BLOCKED` for
a request whose assignee it could not determine: mostly work asked of somebody
else in a group chat. About 100 such rows a month arrive and nothing closes
them, so listing them under 返事待ち grew that section to 194 of 292 rows by
2026-10-30. They are not waiting for anybody's reply, so they are kept out of
every category and counted in ``counts.needs_review`` instead, and only for
`windows.needs_review_days` after the source message.

Genuine waiting rows (`WAITING`, other `BLOCKED`) arrive at about 50 a month
and are closed only by an explicit completion, so `waiting_for` grew too.
A waiting row older than `windows.waiting_max_age_days` stays in the brief
only while it still has a date today or later; otherwise it is counted in
``counts.aged_out``. The same rule retires an old needs-review row.

Age is measured from the earliest `origin` evidence `observed_at` (the source
message), falling back to `created_at`. `updated_at` is unusable for this:
the nightly backfill rewrites it on every row. Nothing here changes a stored
state; every row stays reachable through `commitment_list`.

--------------------------------------------------------------------------
TIMEZONE (phase sections 54, 83)
--------------------------------------------------------------------------
All day boundaries are computed in `timezone` (default Asia/Tokyo) with
`zoneinfo`, never a hard-coded +09:00 — writing the offset in would be a claim
about the future. Day start is 00:00:00 local; the day ends at the exclusive
00:00:00 of the next local day. Conventions for the 36 date-only values:

* a date-only `due_at` compares as **23:59:59.999999 local** of that day —
  which is what the reconciler already emulates for `week`/`daypart` rows, and
  it is why `local_time` is left NULL for them: the database holds no clock
  time, so the plan must not display one;
* a date-only `scheduled_at`/`scheduled_end_at` is an all-day span
  ``[start 00:00 local, end 00:00 local)``, **end-exclusive** (Google Calendar
  semantics: 09-12 → 09-13 is a one-day event), it sets ``all_day: true``, it
  belongs to `scheduled` for every local date it covers, and it is excluded
  from overlap detection — an all-day marker is not an occupied window.

--------------------------------------------------------------------------
PLAN DATE vs WALL CLOCK (phase sections 32, 92, 93)
--------------------------------------------------------------------------
`date` is a first-class input — `daily_plan_get` advertises it and the section
92 audit runs on three separate dates — so every window this module opens is
anchored to the **plan day**, never to `now`:

* ``next_up_limit = day_end_exclusive + next_up_hours``. `classify` tests
  ``day_end_exclusive <= due <= next_up_limit``; taking one end from the plan
  date and the other from the wall clock made the window empty for any future
  plan date, and an affected item then fell out of *every* category with no
  count and no warning — the silent data loss this module exists to prevent.
* ``waiting_followup`` idleness is measured to ``day_end_exclusive``. A brief
  is a document about a whole day, and measuring to `now` made the same date
  produce two different plans — and two different ``plan_hash`` values —
  depending on the hour the run happened.
* ``as_of`` is `now` clamped into the plan day
  (``min(max(now, day_start), day_end_exclusive)``). It is exactly `now` for a
  same-day plan, the start of the day for a future one and the end of the day
  for a historical replay, and it is what the `P0` imminence test is measured
  against — the one thing that genuinely means "right now". Judging a plan for
  2026-09-12 by Tuesday's clock is not a smaller error than judging it by no
  clock at all.

`now` itself is kept for `generated_at` only. The consequence is the property
two tests pin: for a fixed database, both the categories **and** the
``plan_hash`` of ``build_daily_plan(date=D)`` do not depend on when the call is
made. ``priority_class`` is the one clock-dependent field left, and
`compute_plan_hash` excludes it — see that function.

--------------------------------------------------------------------------
SHARED eligibility — settled by coordinator ruling, not a deviation
--------------------------------------------------------------------------
AMENDMENT 1 C, as first written, required ``work_state == ACTIONABLE AND
due_at IS NOT NULL`` for every action category *including* `scheduled`. Read
literally that dropped all 57 `SHARED` `SCHEDULED` calendar rows, which are
SCHEDULED with a NULL `due_at` — contradicting I2-PROFILE.md F, which sizes the
same filter as "2 rows + the 57 SHARED SCHEDULED calendar rows", and section N,
whose recommended audit day depends on a SHARED all-day calendar event reaching
`scheduled`.

The coordinator ruled on this and amended section C: the split rule above **is**
the contract. `scheduled` requires only SELF, or SHARED with non-Gmail
evidence; the ACTIONABLE/`due_at` requirement applies to the deadline-driven
categories alone. Two reasons beyond the arithmetic:

* phase section 20 makes Calendar the scheduling authority, and an event on the
  owner's own calendar is on the owner's day whatever responsibility the
  reconciler assigned it. Section 10 forbids guessing a time — and omitting the
  event entirely is a worse error than guessing one;
* it would remove the only multi-item day available to the section 92 audit.

Gmail-only evidence is still excluded from every category including
`scheduled`, and is counted in `counts.filtered_noise`. See
:data:`SHARED_SCHEDULED_ELIGIBLE`, which a test pins in both directions.

--------------------------------------------------------------------------
DEVIATIONS from I2-CONTRACT.md, all deliberate and reported
--------------------------------------------------------------------------
* **Item keys.** `due_precision` and `all_day` (AMENDMENT 1 I) are emitted next
  to the field they qualify rather than appended, so the Item still reads in
  the contract's order.
* **counts.unknown_work_state.** Additive, required by AMENDMENT 1 D.
* **Conflict detail phrases.** The contract's illustrative JSON pairs
  `overlapping_scheduled` with 「予定が連続しています」. Overlap and adjacency are
  different facts, so the closed set in `plan_models.CONFLICT_DETAILS` gives
  each kind its own phrase; none claims anything about travel (phase section 30).
  A conflict whose window end was **assumed** — the AMENDMENT 1 B3 fallback for
  a NULL `scheduled_end_at`, 0 production rows — carries the same `kind` but
  the `CONFLICT_DETAILS_ASSUMED_END` phrase, which says the end time is
  unknown. An unqualified 「予定が重なっています」 built on a guessed 60 minutes
  is a claim the data does not support: a 09:00 row assumed to run an hour
  "overlaps" a 09:30 meeting that a real 15-minute slot would not touch.
* **`due_within_72h`** stays the literal reason string even when
  `windows.next_up_hours` is configured to something other than 72 — the
  vocabulary is closed (phase section 35).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date as date_cls
from datetime import datetime
from datetime import time as time_cls
from datetime import timedelta
from datetime import timezone as timezone_cls
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from . import freshness as freshness_module
from . import plan_config
from .plan_models import (
    CONFLICT_DETAILS,
    REASON_DUE_TODAY,
    REASON_DUE_WITHIN_72H,
    REASON_NO_DATE_BACKLOG,
    REASON_SCHEDULED_TODAY,
    REASON_WAITING,
    REASON_WAITING_FOLLOWUP,
    WARNING_DATA_FRESHNESS,
    WARNING_UNKNOWN_WORK_STATE,
    Category,
    Conflict,
    ConflictKind,
    PlanItem,
    PriorityClass,
    overdue_reason,
)

LOG = logging.getLogger("hermes.planning.planner")

# `commitments.models.parse_iso` is the canonical parser (I2-CONTRACT.md
# section 3). It is imported the way `event_mcp.py` imports the same package,
# because the deployed layout puts `planning/` next to `event_mcp.py` where the
# package may be reachable under either name — and the planner must still
# import if it is reachable under neither.
try:  # pragma: no cover - import shape depends on deployment layout
    from commitments.models import parse_iso as _canonical_parse_iso
except ImportError:  # pragma: no cover
    try:
        from src.commitments.models import parse_iso as _canonical_parse_iso
    except ImportError:
        _canonical_parse_iso = None


def _fallback_parse_iso(ts_str: Optional[str]) -> Optional[datetime]:
    """Behavioural copy of `commitments.models.parse_iso` (naive -> UTC)."""
    if not ts_str:
        return None
    try:
        text = ts_str.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone_cls.utc)
        return parsed
    except Exception:
        return None


_parse_iso = _canonical_parse_iso or _fallback_parse_iso

DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: Evidence sources whose commitment titles are clean Japanese action phrases.
#: Gmail is deliberately absent: it is the noise engine (I2-PROFILE.md C.6).
NON_GMAIL_SOURCES = frozenset({"discord", "line", "calendar"})

TERMINAL_WORK_STATES = frozenset({"DONE", "CANCELLED", "DISMISSED"})
#: BLOCKED joins WAITING here — see the module docstring.
WAITING_WORK_STATES = frozenset({"WAITING", "BLOCKED"})
KNOWN_WORK_STATES = frozenset(
    {"ACTIONABLE", "WAITING", "SCHEDULED", "BLOCKED", "DONE", "CANCELLED", "DISMISSED"}
)
KNOWN_RESPONSIBILITIES = frozenset({"SELF", "COUNTERPARTY", "SHARED", "INFORMATIONAL"})

CALENDAR_AUTHORITY = "calendar_authority"

#: A SHARED row carrying a calendar slot is eligible for `scheduled` even
#: without an ACTIONABLE work_state or a due_at — the coordinator's ruling on
#: AMENDMENT 1 section C. Flipping this to False restores the literal first
#: draft of that section and drops the 57 SHARED SCHEDULED calendar rows; a
#: test pins the current behaviour so that cannot happen silently.
SHARED_SCHEDULED_ELIGIBLE = True

#: `due_precision` values that carry a real clock time. Everything else has a
#: synthetic one (I2-PROFILE.md B4) and must not be used to claim a deadline
#: falls inside a meeting.
PRECISE_DUE_PRECISIONS = frozenset({"minute", "exact_datetime"})

_COMMITMENT_PREDICATE = """
    work_state NOT IN ('DONE', 'CANCELLED', 'DISMISSED')
    AND (currentness IS NULL OR currentness <> 'superseded')
    AND responsibility <> 'INFORMATIONAL'
"""

_COMMITMENT_SQL = f"""
SELECT commitment_id, title, responsibility, work_state, counterparty,
       due_at, due_precision, scheduled_at, scheduled_end_at,
       authority, currentness, created_at, updated_at, source_count
FROM commitments
WHERE {_COMMITMENT_PREDICATE}
"""

# One batched query for every candidate's evidence, not one per commitment
# (phase section 74). The join keeps the 573 Gmail evidence rows belonging to
# dismissed mail out of the result set.
_EVIDENCE_SQL = """
SELECT e.commitment_id, e.source_type, e.source_id, e.evidence_role, e.observed_at
FROM commitment_evidence e
JOIN commitments c ON c.commitment_id = e.commitment_id
WHERE c.work_state NOT IN ('DONE', 'CANCELLED', 'DISMISSED')
  AND (c.currentness IS NULL OR c.currentness <> 'superseded')
  AND c.responsibility <> 'INFORMATIONAL'
"""


# --------------------------------------------------------------------------
# Timestamp handling
# --------------------------------------------------------------------------


def is_date_only(value: Optional[str]) -> bool:
    """True for a bare ``YYYY-MM-DD`` — an all-day calendar value."""
    return bool(value) and bool(DATE_ONLY_RE.match(str(value).strip()))


def parse_stored(value: Optional[str], tzinfo) -> Tuple[Optional[str], Optional[datetime]]:
    """Parse a stored timestamp into ``(kind, aware datetime in tzinfo)``.

    `kind` is ``"date"`` for a bare ``YYYY-MM-DD`` (returned as 00:00 local,
    because such a value *is* a local calendar date and must not be read as
    midnight UTC), ``"dt"`` for anything with an offset, and ``None`` when the
    value is absent or unparsable.
    """
    if value is None:
        return (None, None)
    text = str(value).strip()
    if not text:
        return (None, None)
    if DATE_ONLY_RE.match(text):
        try:
            day = date_cls.fromisoformat(text)
        except ValueError:
            return (None, None)
        return ("date", datetime.combine(day, time_cls.min, tzinfo=tzinfo))
    parsed = _parse_iso(text)
    if parsed is None:
        return (None, None)
    return ("dt", parsed.astimezone(tzinfo))


# --------------------------------------------------------------------------
# Candidate rows
# --------------------------------------------------------------------------


@dataclass
class _Candidate:
    """One commitment plus everything derived from it, parsed once."""

    commitment_id: str
    title: str
    responsibility: str
    work_state: str
    counterparty: Optional[str]
    due_at: Optional[str]
    due_precision: Optional[str]
    scheduled_at: Optional[str]
    scheduled_end_at: Optional[str]
    authority: str
    created_at: Optional[str]
    updated_at: Optional[str]
    source_count: int
    source_types: List[str] = field(default_factory=list)
    source_pairs: List[Tuple[str, str]] = field(default_factory=list)

    due_dt: Optional[datetime] = None
    due_date_only: bool = False
    sched_start: Optional[datetime] = None
    sched_end: Optional[datetime] = None
    #: True when `sched_end` was filled from `conflict_default_duration_minutes`
    #: because the row carries no `scheduled_end_at` (AMENDMENT 1 B3: 0 of 81
    #: production rows). A conflict derived from a guessed duration says so.
    assumed_end: bool = False
    all_day: bool = False
    created_dt: Optional[datetime] = None
    updated_dt: Optional[datetime] = None
    #: Earliest `observed_at` among the `origin` evidence rows: when the
    #: source message was written. See NEEDS REVIEW and AGE.
    origin_dt: Optional[datetime] = None

    def covers(self, plan_date: date_cls) -> bool:
        """True when this row's scheduled span includes `plan_date`."""
        if self.sched_start is None:
            return False
        if not self.all_day:
            return self.sched_start.date() == plan_date
        end = self.sched_end or (self.sched_start + timedelta(days=1))
        end_date = end.date()
        if end_date <= self.sched_start.date():
            # Degenerate or malformed span: treat as a single day.
            end_date = self.sched_start.date() + timedelta(days=1)
        return self.sched_start.date() <= plan_date < end_date

    @property
    def calendar_authoritative(self) -> bool:
        """Calendar owns this row's time (phase section 20)."""
        if self.authority == CALENDAR_AUTHORITY:
            return True
        types = set(self.source_types)
        return "calendar" in types and len(types) > 1

    @property
    def gmail_only(self) -> bool:
        types = set(self.source_types)
        return bool(types) and not (types & NON_GMAIL_SOURCES)


def _derive(row: Dict[str, Any], tzinfo, default_duration: timedelta) -> _Candidate:
    cand = _Candidate(
        commitment_id=row["commitment_id"],
        title=row["title"],
        responsibility=row["responsibility"],
        work_state=row["work_state"],
        counterparty=row.get("counterparty"),
        due_at=row.get("due_at"),
        due_precision=row.get("due_precision"),
        scheduled_at=row.get("scheduled_at"),
        scheduled_end_at=row.get("scheduled_end_at"),
        authority=row.get("authority") or "evidence",
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
        source_count=int(row.get("source_count") or 1),
    )

    due_kind, due_dt = parse_stored(cand.due_at, tzinfo)
    if due_kind == "date" and due_dt is not None:
        # A date-only deadline is the end of that local day.
        cand.due_dt = due_dt.replace(hour=23, minute=59, second=59, microsecond=999999)
        cand.due_date_only = True
    else:
        cand.due_dt = due_dt

    sched_kind, sched_start = parse_stored(cand.scheduled_at, tzinfo)
    cand.sched_start = sched_start
    cand.all_day = sched_kind == "date"
    _, sched_end = parse_stored(cand.scheduled_end_at, tzinfo)
    if sched_start is not None and sched_end is None:
        # 0 of the 81 production scheduled rows lack an end; this is the
        # defensive path only (I2-PROFILE.md E.2). An all-day row's implied
        # one-day span is Google Calendar semantics, not a guess, so only the
        # timed case is flagged as assumed.
        cand.assumed_end = not cand.all_day
        sched_end = sched_start + (timedelta(days=1) if cand.all_day else default_duration)
    cand.sched_end = sched_end

    _, cand.created_dt = parse_stored(cand.created_at, tzinfo)
    _, cand.updated_dt = parse_stored(cand.updated_at, tzinfo)
    return cand


def _load_candidates(conn, tzinfo, default_duration: timedelta) -> List[_Candidate]:
    """Two SQL statements total: one for commitments, one for all evidence."""
    cur = conn.cursor()
    cur.execute(_COMMITMENT_SQL)
    columns = [d[0] for d in cur.description]
    rows = [dict(zip(columns, values)) for values in cur.fetchall()]

    types_by_id: Dict[str, List[str]] = {}
    pairs_by_id: Dict[str, List[Tuple[str, str]]] = {}
    origin_by_id: Dict[str, datetime] = {}
    cur.execute(_EVIDENCE_SQL)
    for commitment_id, source_type, source_id, role, observed_at in cur.fetchall():
        types_by_id.setdefault(commitment_id, []).append(source_type)
        pairs_by_id.setdefault(commitment_id, []).append((source_type, source_id))
        if role == "origin":
            # Parsed, not compared as text: the column mixes +09:00 and +00:00.
            _, observed = parse_stored(observed_at, tzinfo)
            if observed is not None and (
                commitment_id not in origin_by_id or observed < origin_by_id[commitment_id]
            ):
                origin_by_id[commitment_id] = observed

    candidates: List[_Candidate] = []
    for row in rows:
        cand = _derive(row, tzinfo, default_duration)
        cand.source_types = sorted(set(types_by_id.get(cand.commitment_id, [])))
        cand.source_pairs = sorted(set(pairs_by_id.get(cand.commitment_id, [])))
        cand.origin_dt = origin_by_id.get(cand.commitment_id)
        candidates.append(cand)
    return candidates


# --------------------------------------------------------------------------
# Eligibility (AMENDMENT 1 section C)
# --------------------------------------------------------------------------


def _has_non_gmail_evidence(cand: _Candidate) -> bool:
    return bool(set(cand.source_types) & NON_GMAIL_SOURCES)


def _action_eligible(cand: _Candidate) -> bool:
    """Responsibility eligibility. The `scheduled` gate, unchanged."""
    if cand.responsibility == "SELF":
        return True
    if cand.responsibility != "SHARED":
        return False
    if not _has_non_gmail_evidence(cand):
        return False
    if SHARED_SCHEDULED_ELIGIBLE and cand.scheduled_at:
        return True
    return cand.work_state == "ACTIONABLE" and cand.due_at is not None


def _deadline_eligible(cand: _Candidate) -> bool:
    """The `must_do` / `overdue` / `next_up` / `backlog` gate.

    Responsibility-agnostic evidence test (coordinator RULING 1 on the
    AMENDMENT 1 section C filter): a deadline row reaches a deadline-driven
    category only when something other than Gmail says it is a task. See the
    EVIDENCE, NOT RESPONSIBILITY block in the module docstring.
    """
    return _action_eligible(cand) and _has_non_gmail_evidence(cand)


def _shared_needs_review(cand: _Candidate) -> bool:
    """SHARED, non-Gmail evidence, but no unambiguous self next action."""
    return (
        cand.responsibility == "SHARED"
        and _has_non_gmail_evidence(cand)
        and not _action_eligible(cand)
    )


def _is_filtered_noise(cand: _Candidate) -> bool:
    """SHARED with Gmail-only evidence: excluded everywhere, only counted.

    This is the pre-classification half of the section C filter, and it is the
    only half that removes a row before deduplication. The RULING 1 half —
    Gmail-only evidence on a `SELF` row — is applied inside `classify`, because
    such a row is still allowed into `scheduled` and `waiting_for`; it is
    counted in `counts.filtered_noise` by `build_daily_plan` when it lands in
    no category at all.
    """
    return cand.responsibility == "SHARED" and cand.gmail_only


def _excluded_by_evidence(cand: _Candidate) -> bool:
    """True when only the Gmail-evidence rule kept this row out of every category.

    Distinguishes 「a newsletter the filter caught」 from 「a COUNTERPARTY row
    that was never the owner's task」, so `counts.filtered_noise` stays a
    statement about the noise filter rather than a bucket for everything the
    planner declined.
    """
    return cand.gmail_only and _action_eligible(cand)


# --------------------------------------------------------------------------
# Deduplication (AMENDMENT 1 section E)
# --------------------------------------------------------------------------


class _Groups:
    """Minimal union-find that remembers which rule linked each group."""

    def __init__(self, ids: Iterable[str]) -> None:
        self._parent = {i: i for i in ids}
        self._rules: Dict[str, set] = {i: set() for i in self._parent}

    def find(self, item: str) -> str:
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, left: str, right: str, rule: str) -> None:
        a, b = self.find(left), self.find(right)
        rules = self._rules[a] | self._rules[b] | {rule}
        if a != b:
            self._parent[b] = a
            self._rules.pop(b, None)
        self._rules[a] = rules

    def groups(self) -> Dict[str, Tuple[List[str], set]]:
        out: Dict[str, Tuple[List[str], set]] = {}
        for item in self._parent:
            root = self.find(item)
            members, rules = out.setdefault(root, ([], self._rules.get(root, set())))
            members.append(item)
        return out


def _survivor_shared_source(members: List[_Candidate]) -> _Candidate:
    """Contract rule: calendar_authority, else higher source_count, else id."""
    return sorted(
        members,
        key=lambda c: (
            0 if c.authority == CALENDAR_AUTHORITY else 1,
            -c.source_count,
            c.commitment_id,
        ),
    )[0]


def _survivor_identical_window(members: List[_Candidate]) -> _Candidate:
    """AMENDMENT 1 E rule 2: longer title, then newer updated_at, then id."""
    return sorted(
        members,
        key=lambda c: (
            -len(c.title or ""),
            -(c.updated_dt.timestamp() if c.updated_dt else float("-inf")),
            c.commitment_id,
        ),
    )[0]


def deduplicate(candidates: Sequence[_Candidate]) -> Tuple[List[_Candidate], int]:
    """Collapse rows representing the same canonical item.

    Returns ``(survivors, collapsed_count)``. Title similarity is never used.
    """
    if len(candidates) < 2:
        return (list(candidates), 0)

    by_id = {c.commitment_id: c for c in candidates}
    groups = _Groups(by_id)

    # Rule 1 — a shared (source_type, source_id) evidence pair.
    by_pair: Dict[Tuple[str, str], List[str]] = {}
    for cand in candidates:
        for pair in cand.source_pairs:
            by_pair.setdefault(pair, []).append(cand.commitment_id)
    for pair, ids in by_pair.items():
        unique = sorted(set(ids))
        for other in unique[1:]:
            groups.union(unique[0], other, "shared_source_id")

    # Rule 2 — byte-identical scheduling window, both calendar-authoritative.
    by_window: Dict[Tuple[Optional[str], Optional[str]], List[str]] = {}
    for cand in candidates:
        if cand.authority == CALENDAR_AUTHORITY and cand.scheduled_at:
            key = (cand.scheduled_at, cand.scheduled_end_at)
            by_window.setdefault(key, []).append(cand.commitment_id)
    for key, ids in by_window.items():
        unique = sorted(set(ids))
        for other in unique[1:]:
            groups.union(unique[0], other, "identical_window")

    survivors: List[_Candidate] = []
    collapsed = 0
    for _root, (member_ids, rules) in groups.groups().items():
        members = [by_id[i] for i in member_ids]
        if len(members) == 1:
            survivors.append(members[0])
            continue
        collapsed += len(members) - 1
        if "shared_source_id" in rules:
            keeper = _survivor_shared_source(members)
        else:
            keeper = _survivor_identical_window(members)
        merged = sorted({t for member in members for t in member.source_types})
        keeper.source_types = merged
        LOG.info(
            "planner: collapsed %d duplicate commitment(s) into %s via %s",
            len(members) - 1,
            keeper.commitment_id,
            ",".join(sorted(rules)),
        )
        survivors.append(keeper)

    survivors.sort(key=lambda c: c.commitment_id)
    return (survivors, collapsed)


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


@dataclass
class _Context:
    plan_date: date_cls
    day_start: datetime
    day_end_exclusive: datetime
    now: datetime
    #: `now` clamped into the plan day — see the PLAN DATE vs WALL CLOCK
    #: section of the module docstring. Equal to `now` for a same-day plan.
    as_of: datetime
    next_up_limit: datetime
    imminent_limit: datetime
    followup_delta: timedelta
    waiting_max_age: timedelta
    needs_review_max_age: timedelta


#: `counts` keys for rows that `classify` keeps out of every category on
#: purpose. See NEEDS REVIEW and AGE in the module docstring.
HELD_NEEDS_REVIEW = "needs_review"
HELD_AGED_OUT = "aged_out"


def _effective_due(cand: _Candidate) -> Optional[datetime]:
    """Calendar authority: a calendar-owned slot suppresses a stale due_at."""
    if cand.calendar_authoritative and cand.sched_start is not None:
        return None
    return cand.due_dt


def _overdue_days(cand: _Candidate, ctx: _Context) -> int:
    due = _effective_due(cand)
    if due is None:
        return 0
    return max(0, (ctx.plan_date - due.date()).days)


def _aged_out(cand: _Candidate, ctx: _Context, max_age: timedelta) -> bool:
    """Older than `max_age` and with no date left today or later."""
    born = cand.origin_dt or cand.created_dt
    if born is None or (ctx.day_end_exclusive - born) <= max_age:
        return False
    ahead = _effective_due(cand) or cand.sched_start
    return ahead is None or ahead < ctx.day_start


def held_back(cand: _Candidate, ctx: _Context) -> Optional[str]:
    """The `counts` key for a row that stays out of the brief, else None.

    Only meaningful for a row that reached the waiting stage of `classify`;
    `classify` calls it there, and `build_daily_plan` calls it again to count
    a row `classify` left uncategorised.
    """
    if _shared_needs_review(cand):
        if _aged_out(cand, ctx, ctx.needs_review_max_age):
            return HELD_AGED_OUT
        return HELD_NEEDS_REVIEW
    if cand.work_state in WAITING_WORK_STATES and _aged_out(cand, ctx, ctx.waiting_max_age):
        return HELD_AGED_OUT
    return None


def classify(cand: _Candidate, ctx: _Context) -> Tuple[Optional[Category], Optional[str]]:
    """Return the single ``(category, reason_for_inclusion)`` for this row."""
    if _is_filtered_noise(cand):
        return (None, None)

    eligible = _action_eligible(cand)
    # RULING 1: the deadline-driven categories additionally require non-Gmail
    # evidence, whatever the responsibility.
    deadline_eligible = _deadline_eligible(cand)
    actionable = cand.work_state == "ACTIONABLE"
    due = _effective_due(cand)

    if eligible and cand.covers(ctx.plan_date):
        return (Category.SCHEDULED, REASON_SCHEDULED_TODAY)

    if deadline_eligible and actionable and due is not None:
        if ctx.day_start <= due < ctx.day_end_exclusive:
            return (Category.MUST_DO, REASON_DUE_TODAY)
        if due < ctx.day_start:
            return (Category.OVERDUE, overdue_reason(_overdue_days(cand, ctx)))
        if ctx.day_end_exclusive <= due <= ctx.next_up_limit:
            return (Category.NEXT_UP, REASON_DUE_WITHIN_72H)

    # Before the waiting branch: SHARED + BLOCKED is a WAITING_WORK_STATE, but
    # an undetermined assignee is not a reply anybody is waiting for.
    if held_back(cand, ctx) is not None:
        return (None, None)

    if cand.work_state in WAITING_WORK_STATES:
        idle_from = cand.updated_dt or cand.created_dt
        # Measured to the end of the plan day, not to the wall clock: a brief
        # is a document about a whole day, and anchoring it to `now` made the
        # same date produce two different plans (and two different
        # `plan_hash`es) depending on the hour the run happened.
        if idle_from is not None and (ctx.day_end_exclusive - idle_from) > ctx.followup_delta:
            return (Category.WAITING_FOR, REASON_WAITING_FOLLOWUP)
        return (Category.WAITING_FOR, REASON_WAITING)

    if deadline_eligible and actionable and cand.due_at is None and cand.scheduled_at is None:
        return (Category.BACKLOG, REASON_NO_DATE_BACKLOG)

    return (None, None)


def priority_class(cand: _Candidate, category: Category, reason: str, ctx: _Context) -> str:
    """P0-P4 (phase section 25). Informational metadata, not the sort order.

    The phase document's table leaves two shapes undefined; both are completed
    here deterministically: a scheduled item that is not imminent is P1 (it is
    a fixed commitment for today, the same tier as a deadline today), and a
    plain `waiting` row is P4 alongside the backlog, which keeps
    `waiting_followup` — the one waiting shape phase section 15 wants surfaced
    — strictly above it at P3.
    """
    if category is Category.SCHEDULED:
        if not cand.all_day and cand.sched_start is not None:
            ends_after_now = cand.sched_end is None or cand.sched_end > ctx.as_of
            if cand.sched_start <= ctx.imminent_limit and ends_after_now:
                return PriorityClass.P0.value
        return PriorityClass.P1.value
    if category is Category.MUST_DO:
        return PriorityClass.P1.value
    if category is Category.OVERDUE:
        return PriorityClass.P1.value if _overdue_days(cand, ctx) >= 7 else PriorityClass.P2.value
    if category is Category.NEXT_UP:
        return PriorityClass.P2.value
    if category is Category.WAITING_FOR:
        return (
            PriorityClass.P3.value
            if reason == REASON_WAITING_FOLLOWUP
            else PriorityClass.P4.value
        )
    return PriorityClass.P4.value


# --------------------------------------------------------------------------
# Ordering
# --------------------------------------------------------------------------


def _nullable_key(value: Optional[datetime]) -> Tuple[int, float]:
    """Sort key placing NULLs last, comparing parsed instants not strings."""
    if value is None:
        return (1, 0.0)
    return (0, value.timestamp())


def generic_sort_key(cand: _Candidate, priority: str) -> tuple:
    """The five-level tie-breaker of phase section 27, in order."""
    return (
        _nullable_key(cand.sched_start),
        _nullable_key(cand.due_dt),
        priority,
        _nullable_key(cand.created_dt),
        cand.commitment_id,
    )


def _counterparty_bucket(cand: _Candidate) -> str:
    """The diversity bucket a row belongs to for the overdue cap.

    A named counterparty is its own bucket, shared with every other row naming
    the same person. A NULL or blank one is bucketed by `commitment_id`, so the
    cap only ever suppresses genuine repeats from one named thread. The ``\\x00``
    prefix cannot collide with a real name.
    """
    name = (cand.counterparty or "").strip()
    return name if name else "\x00" + cand.commitment_id


def _order_overdue(
    entries: List[Tuple[_Candidate, str]], limit: int, per_counterparty_cap: int
) -> List[Tuple[_Candidate, str]]:
    """AMENDMENT 1 F1: newest deadline first, max 2 per counterparty, id last.

    The cap is strict: if it leaves a slot unfilled, the slot stays unfilled.
    Back-filling would reinstate exactly the "same stale thread every morning"
    behaviour the cap exists to prevent, and `counts.overdue.total` already
    tells the reader how much was held back.

    A **missing** counterparty is not a thread to diversify away from, so each
    NULL/empty row is its own bucket (coordinator ruling). Sharing one bucket
    made the shown list end at 2 however many distinct overdue items existed —
    a shape production does carry, and one that defeats section 12's 棚卸し.
    """
    ordered = sorted(
        entries,
        key=lambda pair: (
            -(pair[0].due_dt.timestamp() if pair[0].due_dt else float("-inf")),
            pair[0].commitment_id,
        ),
    )
    shown: List[Tuple[_Candidate, str]] = []
    per_counterparty: Dict[str, int] = {}
    for cand, priority in ordered:
        if len(shown) >= limit:
            break
        key = _counterparty_bucket(cand)
        if per_counterparty.get(key, 0) >= per_counterparty_cap:
            continue
        per_counterparty[key] = per_counterparty.get(key, 0) + 1
        shown.append((cand, priority))
    return shown


def _order_backlog(
    entries: List[Tuple[_Candidate, str]], limit: int, plan_date: date_cls
) -> List[Tuple[_Candidate, str]]:
    """AMENDMENT 1 F2: deterministic daily rotation over the sorted candidates."""
    ordered = sorted(entries, key=lambda pair: generic_sort_key(pair[0], pair[1]))
    if not ordered:
        return []
    offset = (plan_date.toordinal() * 3) % len(ordered)
    rotated = ordered[offset:] + ordered[:offset]
    return rotated[:limit]


# --------------------------------------------------------------------------
# Conflicts (phase sections 29, 30)
# --------------------------------------------------------------------------


def detect_conflicts(
    scheduled: Sequence[_Candidate],
    must_do: Sequence[_Candidate],
    *,
    dense_window: timedelta,
) -> List[Conflict]:
    """Detect conflicts over the deduplicated, uncapped candidate sets.

    All-day rows are excluded from every window test: an all-day marker is not
    an occupied time window, and claiming otherwise asserts more than the data
    supports (AMENDMENT 1 B2, phase section 30).
    """
    timed = [c for c in scheduled if not c.all_day and c.sched_start and c.sched_end]
    timed.sort(key=lambda c: (c.sched_start, c.commitment_id))

    conflicts: List[Conflict] = []
    overlapping_pairs = set()

    for i, left in enumerate(timed):
        for right in timed[i + 1 :]:
            if left.sched_start < right.sched_end and right.sched_start < left.sched_end:
                overlapping_pairs.add(frozenset({left.commitment_id, right.commitment_id}))
                conflicts.append(
                    Conflict(
                        kind=ConflictKind.OVERLAPPING_SCHEDULED.value,
                        commitment_ids=[left.commitment_id, right.commitment_id],
                        window_start=min(left.sched_start, right.sched_start).isoformat(),
                        window_end=max(left.sched_end, right.sched_end).isoformat(),
                        assumed_end=left.assumed_end or right.assumed_end,
                    )
                )

    # Consecutive pairs with a small or zero gap: 「予定が連続しています」.
    for left, right in zip(timed, timed[1:]):
        if frozenset({left.commitment_id, right.commitment_id}) in overlapping_pairs:
            continue
        gap = right.sched_start - left.sched_end
        if timedelta(0) <= gap <= dense_window:
            conflicts.append(
                Conflict(
                    kind=ConflictKind.DENSE_WINDOW.value,
                    commitment_ids=[left.commitment_id, right.commitment_id],
                    window_start=left.sched_start.isoformat(),
                    window_end=right.sched_end.isoformat(),
                    assumed_end=left.assumed_end or right.assumed_end,
                )
            )

    # A deadline landing inside an occupied window — only when the deadline
    # carries a real clock time. `week`/`daypart`/`approximate` rows carry a
    # synthetic 23:59, and reporting a conflict from one would be fake
    # precision (I2-PROFILE.md B4).
    for task in must_do:
        if task.due_dt is None or task.due_date_only:
            continue
        if (task.due_precision or "") not in PRECISE_DUE_PRECISIONS:
            continue
        for slot in timed:
            if slot.commitment_id == task.commitment_id:
                continue
            if slot.sched_start <= task.due_dt < slot.sched_end:
                conflicts.append(
                    Conflict(
                        kind=ConflictKind.DEADLINE_DURING_SCHEDULED.value,
                        commitment_ids=[slot.commitment_id, task.commitment_id],
                        window_start=slot.sched_start.isoformat(),
                        window_end=slot.sched_end.isoformat(),
                        assumed_end=slot.assumed_end,
                    )
                )

    conflicts.sort(
        key=lambda c: (c.window_start or "", c.kind, tuple(sorted(c.commitment_ids)))
    )
    return conflicts


# --------------------------------------------------------------------------
# Item construction
# --------------------------------------------------------------------------


def _local_time(cand: _Candidate) -> Optional[str]:
    """HH:MM in the plan timezone, or None when the DB holds no clock time.

    Calendar authority (phase section 20): when a row carries both a
    `scheduled_at` and a `due_at`, the scheduled time is displayed. An all-day
    row and a date-only deadline both yield None — they render as 終日 or as a
    date, never as an invented clock time.

    A **coarse `due_precision` yields None too.** AMENDMENT 1 B4 records that
    `week` / `daypart` / `approximate` rows carry a *synthetic* ``23:59:00``,
    and calls rendering it "a fake-precision defect". That rule used to live in
    the renderer only, so `daily_plan_get` still handed an MCP consumer a
    ``local_time`` of ``"23:59"`` for a 「今週中」 task — a deadline the database
    does not contain, on the one path with no renderer in front of it. It is
    applied here instead, at the source, for every consumer. The raw `due_at`
    stays verbatim (it is canonical) and `due_precision` stays beside it, so a
    consumer that wants to decide for itself still can.
    """
    if cand.scheduled_at:
        if cand.all_day or cand.sched_start is None:
            return None
        return cand.sched_start.strftime("%H:%M")
    if cand.due_dt is None or cand.due_date_only:
        return None
    if (cand.due_precision or "") not in PRECISE_DUE_PRECISIONS:
        return None
    return cand.due_dt.strftime("%H:%M")


def _to_item(cand: _Candidate, category: Category, reason: str, priority: str, ctx: _Context) -> PlanItem:
    return PlanItem(
        commitment_id=cand.commitment_id,
        title=cand.title,
        responsibility=cand.responsibility,
        work_state=cand.work_state,
        due_at=cand.due_at,
        due_precision=cand.due_precision,
        scheduled_at=cand.scheduled_at,
        scheduled_end_at=cand.scheduled_end_at,
        all_day=cand.all_day,
        counterparty=cand.counterparty,
        local_time=_local_time(cand),
        priority_class=priority,
        reason_for_inclusion=reason,
        source_types=list(cand.source_types),
        overdue_days=_overdue_days(cand, ctx) if category is Category.OVERDUE else None,
    )


#: Removed from the hashed body in addition to the three top-level exclusions.
#: `priority_class` is derived from the wall clock through `imminent_limit` —
#: a 14:00 calendar item is `P1` at 08:30 and `P0` at 11:30 — so leaving it in
#: meant the *stated purpose* of this hash, "the same facts produce the same
#: hash on a retry", was not achieved for any day carrying a timed event. The
#: contract documents `priority_class` as informational metadata rather than
#: display order (the order is the section 27 tie-breaker), so removing it
#: takes nothing the reader sees out of the hash.
_HASH_EXCLUDED_ITEM_FIELDS = ("priority_class",)


def _hash_body(plan: Dict[str, Any]) -> Dict[str, Any]:
    """The plan with everything that changes per run removed."""
    body = {}
    for key, value in plan.items():
        if key in ("generated_at", "plan_hash", "source_freshness"):
            continue
        if isinstance(value, list) and all(isinstance(v, dict) for v in value):
            body[key] = [
                {k: v for k, v in item.items() if k not in _HASH_EXCLUDED_ITEM_FIELDS}
                for item in value
            ]
        else:
            body[key] = value
    return body


def compute_plan_hash(plan: Dict[str, Any]) -> str:
    """sha256 over the plan body, excluding the fields that change per run.

    `generated_at`, `plan_hash` and `source_freshness` are removed at the top
    level, and `priority_class` is removed from every item, so that the same
    facts produce the same hash on a retry — which is what phase sections 58
    and 66 need in order to suppress a duplicate morning brief.

    `warnings` is deliberately **kept** in the body: the contract names exactly
    three top-level exclusions, and a warning is content the reader sees in the
    brief. It follows that a retry which happens to cross a staleness threshold
    produces a different hash. That is correct rather than unfortunate — the
    brief text really did change — and the delivery layer's own
    ``(notification_id, channel)`` uniqueness, not this hash, is what makes
    one-brief-per-day authoritative.
    """
    encoded = json.dumps(_hash_body(plan), sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def build_daily_plan(
    conn,
    *,
    date: Optional[str] = None,
    timezone: str = "Asia/Tokyo",
    now: Optional[datetime] = None,
    config: Optional[Dict[str, Any]] = None,
    freshness: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the deterministic daily plan (I2-CONTRACT.md section 3).

    `conn` is a `sqlite3.Connection` opened **read-only** on `events.db`; this
    function issues exactly two SELECT statements and writes nothing. `now` is
    injectable so the day-boundary behaviour can be tested without moving the
    host clock, and `freshness` is injectable so a plan can be built without
    touching the five health surfaces.
    """
    cfg = plan_config.effective(config)
    tz_name = timezone or cfg.get("timezone") or "Asia/Tokyo"
    tzinfo = ZoneInfo(tz_name)

    if now is None:
        now_local = datetime.now(tzinfo)
    elif now.tzinfo is None:
        now_local = now.replace(tzinfo=timezone_cls.utc).astimezone(tzinfo)
    else:
        now_local = now.astimezone(tzinfo)

    plan_date = date_cls.fromisoformat(date) if date else now_local.date()
    day_start = datetime.combine(plan_date, time_cls.min, tzinfo=tzinfo)
    day_end_exclusive = day_start + timedelta(days=1)

    windows = cfg["windows"]
    limits = cfg["limits"]
    # `now` clamped into the plan day. For the 08:30 scheduled run this *is*
    # `now`; for any other plan date it is the nearest instant on that day, so
    # "how things stand" is judged as of the day being planned rather than as
    # of the wall clock. See the module docstring.
    as_of = min(max(now_local, day_start), day_end_exclusive)
    ctx = _Context(
        plan_date=plan_date,
        day_start=day_start,
        day_end_exclusive=day_end_exclusive,
        now=now_local,
        as_of=as_of,
        # Anchored to the plan day, never to the wall clock: `classify` tests
        # `day_end_exclusive <= due <= next_up_limit`, and the two must come
        # from the same origin or the window is empty for a future plan date.
        next_up_limit=day_end_exclusive + timedelta(hours=float(windows["next_up_hours"])),
        imminent_limit=as_of + timedelta(hours=float(windows["imminent_hours"])),
        followup_delta=timedelta(days=float(windows["followup_days"])),
        waiting_max_age=timedelta(days=float(windows["waiting_max_age_days"])),
        needs_review_max_age=timedelta(days=float(windows["needs_review_days"])),
    )

    default_duration = timedelta(minutes=float(windows["conflict_default_duration_minutes"]))
    candidates = _load_candidates(conn, tzinfo, default_duration)

    unknown_states: Dict[str, int] = {}
    kept: List[_Candidate] = []
    for cand in candidates:
        if cand.work_state not in KNOWN_WORK_STATES:
            unknown_states[cand.work_state] = unknown_states.get(cand.work_state, 0) + 1
            LOG.warning(
                "planner: unrecognised work_state %r on %s — counted, not dropped silently",
                cand.work_state,
                cand.commitment_id,
            )
            continue
        if cand.responsibility not in KNOWN_RESPONSIBILITIES:
            LOG.warning(
                "planner: unrecognised responsibility %r on %s",
                cand.responsibility,
                cand.commitment_id,
            )
        kept.append(cand)

    filtered_noise = sum(1 for cand in kept if _is_filtered_noise(cand))

    # Dedup runs before classification and therefore before conflict
    # detection: the only window overlaps in the whole dataset are the
    # duplicate pairs, so the other order reports a phantom conflict.
    survivors, collapsed = deduplicate([c for c in kept if not _is_filtered_noise(c)])

    buckets: Dict[Category, List[Tuple[_Candidate, str]]] = {c: [] for c in Category}
    not_categorised = 0
    held: Dict[str, int] = {HELD_NEEDS_REVIEW: 0, HELD_AGED_OUT: 0}
    for cand in survivors:
        category, reason = classify(cand, ctx)
        if category is None or reason is None:
            held_key = held_back(cand, ctx)
            # RULING 1's other half: a row the evidence rule — and only the
            # evidence rule — kept out of every category is noise, and is
            # counted rather than dropped in silence.
            if held_key is not None:
                held[held_key] += 1
            elif _excluded_by_evidence(cand):
                filtered_noise += 1
            else:
                not_categorised += 1
            continue
        buckets[category].append((cand, reason))

    per_counterparty_cap = int(cfg.get("overdue_per_counterparty_cap", 2))
    shown: Dict[Category, List[Tuple[_Candidate, str]]] = {}
    for category, entries in buckets.items():
        limit = int(limits[category.value])
        if category is Category.OVERDUE:
            shown[category] = _order_overdue(entries, limit, per_counterparty_cap)
        elif category is Category.BACKLOG:
            shown[category] = _order_backlog(entries, limit, plan_date)
        else:
            ordered = sorted(
                entries,
                key=lambda pair: generic_sort_key(
                    pair[0], priority_class(pair[0], category, pair[1], ctx)
                ),
            )
            shown[category] = ordered[:limit]

    conflicts = detect_conflicts(
        [cand for cand, _ in buckets[Category.SCHEDULED]],
        [cand for cand, _ in buckets[Category.MUST_DO]],
        dense_window=timedelta(minutes=float(windows["dense_window_minutes"])),
    )

    plan: Dict[str, Any] = {
        "date": plan_date.isoformat(),
        "timezone": tz_name,
        "generated_at": now_local.isoformat(),
    }
    for category in Category:
        plan[category.value] = [
            _to_item(cand, category, reason, priority_class(cand, category, reason, ctx), ctx).to_dict()
            for cand, reason in shown[category]
        ]
    plan["conflicts"] = [c.to_dict() for c in conflicts]
    plan["counts"] = {
        category.value: {
            "shown": len(shown[category]),
            "total": len(buckets[category]),
        }
        for category in Category
    }
    plan["counts"]["conflicts"] = {"shown": len(conflicts), "total": len(conflicts)}
    plan["counts"]["filtered_noise"] = {"shown": 0, "total": filtered_noise}
    plan["counts"]["unknown_work_state"] = {"shown": 0, "total": sum(unknown_states.values())}
    # Every eligible row is now accounted for somewhere. Before these two, a
    # collapsed duplicate was only logged at INFO and a row that fell out for a
    # reason other than the evidence filter — a COUNTERPARTY task, a row with
    # no evidence at all — vanished with no counter, which is the same class of
    # silent loss the AMENDMENT 1 D counter exists to prevent. A test asserts
    # the arithmetic closes.
    plan["counts"]["deduplicated"] = {"shown": 0, "total": collapsed}
    plan["counts"]["not_categorised"] = {"shown": 0, "total": not_categorised}
    for key, total in held.items():
        plan["counts"][key] = {"shown": 0, "total": total}

    if freshness is None:
        freshness = freshness_module.collect(cfg, now=now_local)
    plan["source_freshness"] = freshness

    warnings: List[str] = []
    if freshness_module.any_stale(freshness):
        warnings.append(WARNING_DATA_FRESHNESS)
    if unknown_states:
        warnings.append(WARNING_UNKNOWN_WORK_STATE)
    if warnings:
        plan["warnings"] = warnings

    plan["plan_hash"] = compute_plan_hash(plan)
    return plan


__all__ = [
    "build_daily_plan",
    "compute_plan_hash",
    "classify",
    "deduplicate",
    "detect_conflicts",
    "generic_sort_key",
    "parse_stored",
    "priority_class",
    "CONFLICT_DETAILS",
]
