"""Shared, isolated test fixtures for the Phase I.2 planning suites.

Every planning test builds its own database here. Nothing in this module can
reach the canonical store: :func:`open_db` refuses any path that looks like a
production location, and the default is an in-memory database that has no path
at all (phase section 78 — never add a synthetic commitment to production).

The schema is the **real** one. It is imported from
``commitments.store.SCHEMA_SQL`` rather than copied, so a future migration to
the canonical store cannot leave these fixtures quietly testing a shape that no
longer exists.

Everything here is deterministic: fixed ids, a fixed clock, a fixed plan date.
Ranking, `plan_hash` and the backlog rotation are all order-sensitive, so a
fixture that produced a different order on a different day would make those
tests meaningless.

All names, titles and counterparties are invented. Nothing in this file comes
from real personal data.

Public helpers (the other Phase I.2 workers import these):

* :func:`open_db` / :func:`new_db`   — an isolated DB carrying the real schema
* :func:`add_commitment`            — one commitment plus its evidence
* :func:`add_evidence`              — extra evidence for an existing row
* :func:`freshness_block`           — a `source_freshness` block, fresh or stale
* :func:`build_plan`                — `build_daily_plan` with the fixed clock
* :func:`make_plan_fixture`         — a small realistic plan dict (contract §3)
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from commitments.store import SCHEMA_SQL  # noqa: E402  (path bootstrap above)
from planning import plan_config  # noqa: E402
from planning.planner import build_daily_plan  # noqa: E402

JST = ZoneInfo("Asia/Tokyo")

#: The plan date every fixture uses unless told otherwise, and the clock that
#: goes with it: 08:30 JST is the configured morning-brief time.
DEFAULT_PLAN_DATE = "2026-09-08"
DEFAULT_NOW = datetime(2026, 9, 8, 8, 30, tzinfo=JST)

FRESHNESS_SOURCES: Tuple[str, ...] = ("calendar", "gmail", "line", "discord", "commitments")

#: Any path containing one of these is a production location. Opening one from
#: a test would violate phase section 78, so it is refused outright rather than
#: guarded by a convention someone can forget.
_FORBIDDEN_PATH_MARKERS = ("/srv/hermes", "\\srv\\hermes", "hermes-data", "/opt/data")


class ProductionAccessError(RuntimeError):
    """Raised when a fixture is pointed at a production database."""


def _assert_isolated(path: str) -> None:
    normalised = str(path).replace("\\", "/").lower()
    for marker in _FORBIDDEN_PATH_MARKERS:
        if marker.replace("\\", "/").lower() in normalised:
            raise ProductionAccessError(
                f"refusing to open a production-looking database path: {path!r}"
            )


_WORK_STATE_CHECK_RE = re.compile(r"CHECK\(work_state IN \([^)]*\)\)")


def open_db(
    path: str = ":memory:", *, allow_unknown_work_state: bool = False
) -> sqlite3.Connection:
    """Open an isolated SQLite database carrying the real commitments schema.

    `allow_unknown_work_state` drops only the ``CHECK(work_state IN (...))``
    constraint, so a test can insert a state this planner predates. That is not
    a hypothetical: `work_state` is a `CHECK` column with two legal values that
    have zero production rows today, and the planner has to stay loud rather
    than silently dropping a row when a future reconcile starts writing an
    eighth one (AMENDMENT 1 section D).
    """
    if path != ":memory:":
        _assert_isolated(path)
    schema = SCHEMA_SQL
    if allow_unknown_work_state:
        schema = _WORK_STATE_CHECK_RE.sub("", schema)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(schema)
    # `events.db` also carries a `meta` table; the freshness probe reads it as
    # one of its commitment-reconciliation fallbacks.
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);"
    )
    conn.commit()
    return conn


#: Backwards-friendly alias.
new_db = open_db


_ID_COUNTER = {"n": 0}


def next_commitment_id() -> str:
    """Deterministic, sortable synthetic ids in the canonical `cmt_` shape."""
    _ID_COUNTER["n"] += 1
    return f"cmt_{_ID_COUNTER['n']:012d}"


def reset_ids(start: int = 0) -> None:
    """Reset the id counter so a test's ids are reproducible."""
    _ID_COUNTER["n"] = start


def _normalise_evidence(
    evidence: Optional[Any], commitment_id: str
) -> List[Tuple[str, str]]:
    """Accept ``"discord"``, ``["discord", "line"]`` or ``[(type, id), ...]``."""
    if evidence is None:
        return []
    if isinstance(evidence, str):
        evidence = [evidence]
    pairs: List[Tuple[str, str]] = []
    for index, item in enumerate(evidence):
        if isinstance(item, (tuple, list)) and len(item) == 2:
            pairs.append((str(item[0]), str(item[1])))
        else:
            pairs.append((str(item), f"{item}:{commitment_id}:{index}"))
    return pairs


def add_commitment(
    conn: sqlite3.Connection,
    *,
    commitment_id: Optional[str] = None,
    title: str = "テスト用の作業",
    responsibility: str = "SELF",
    work_state: str = "ACTIONABLE",
    summary: Optional[str] = None,
    counterparty: Optional[str] = None,
    project_id: Optional[str] = None,
    due_at: Optional[str] = None,
    due_precision: Optional[str] = None,
    scheduled_at: Optional[str] = None,
    scheduled_end_at: Optional[str] = None,
    authority: str = "evidence",
    confidence: float = 0.85,
    currentness: str = "current",
    notes: Optional[str] = None,
    created_at: str = "2026-09-01T04:01:35.611079+00:00",
    updated_at: str = "2026-09-07T07:48:49.560488+00:00",
    resolved_at: Optional[str] = None,
    source_count: Optional[int] = None,
    evidence: Optional[Any] = None,
    commit: bool = True,
) -> str:
    """Insert one commitment and its evidence. Returns the commitment id.

    `evidence` accepts a bare source type (``"discord"``), a list of types, or
    explicit ``(source_type, source_id)`` pairs — the last form is what the
    shared-`source_id` deduplication rule needs.
    """
    commitment_id = commitment_id or next_commitment_id()
    pairs = _normalise_evidence(evidence, commitment_id)
    if source_count is None:
        source_count = max(1, len({t for t, _ in pairs}))

    conn.execute(
        """
        INSERT INTO commitments (
            commitment_id, title, summary, responsibility, work_state,
            counterparty, project_id, due_at, due_precision,
            scheduled_at, scheduled_end_at, authority, confidence,
            currentness, notes, created_at, updated_at, resolved_at, source_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            commitment_id, title, summary, responsibility, work_state,
            counterparty, project_id, due_at, due_precision,
            scheduled_at, scheduled_end_at, authority, confidence,
            currentness, notes, created_at, updated_at, resolved_at, source_count,
        ),
    )
    for source_type, source_id in pairs:
        add_evidence(
            conn,
            commitment_id,
            source_type,
            source_id,
            observed_at=created_at,
            commit=False,
        )
    if commit:
        conn.commit()
    return commitment_id


def add_evidence(
    conn: sqlite3.Connection,
    commitment_id: str,
    source_type: str,
    source_id: Optional[str] = None,
    *,
    evidence_role: str = "origin",
    observed_at: str = "2026-09-01T04:01:35.611079+00:00",
    confidence: float = 0.9,
    snippet: Optional[str] = None,
    commit: bool = True,
) -> None:
    """Attach one evidence row. Duplicate `(type, id)` pairs are ignored."""
    source_id = source_id or f"{source_type}:{commitment_id}"
    conn.execute(
        """
        INSERT OR IGNORE INTO commitment_evidence (
            commitment_id, source_type, source_id, observed_at,
            evidence_role, confidence, snippet, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            commitment_id, source_type, source_id, observed_at,
            evidence_role, confidence, snippet, observed_at,
        ),
    )
    if commit:
        conn.commit()


def freshness_block(
    now: Optional[datetime] = None,
    stale_sources: Sequence[str] = (),
    *,
    unreadable_sources: Sequence[str] = (),
) -> Dict[str, Any]:
    """Build an injectable `source_freshness` block.

    Sources named in `stale_sources` come back older than their threshold;
    those in `unreadable_sources` come back as the "never raises" shape the
    contract requires of :func:`planning.freshness.collect`.
    """
    now = now or DEFAULT_NOW
    thresholds = plan_config.DEFAULTS["freshness_thresholds_seconds"]
    out: Dict[str, Any] = {}
    for name in FRESHNESS_SOURCES:
        threshold = int(thresholds[name])
        if name in unreadable_sources:
            out[name] = {
                "last_success_at": None,
                "age_seconds": None,
                "stale": True,
                "threshold_seconds": threshold,
                "error": "source_missing",
            }
            continue
        age = threshold + 3600 if name in stale_sources else 600
        out[name] = {
            "last_success_at": (now - timedelta(seconds=age)).isoformat(),
            "age_seconds": age,
            "stale": age > threshold,
            "threshold_seconds": threshold,
        }
    return out


def build_plan(
    conn: sqlite3.Connection,
    *,
    date: Optional[str] = DEFAULT_PLAN_DATE,
    now: Optional[datetime] = None,
    timezone: str = "Asia/Tokyo",
    config: Optional[Dict[str, Any]] = None,
    freshness: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """`build_daily_plan` with the fixed clock and an injected freshness block.

    Freshness is injected by default so that no test ever depends on a health
    file existing on the machine running it.
    """
    now = now or DEFAULT_NOW
    if freshness is None:
        freshness = freshness_block(now)
    return build_daily_plan(
        conn,
        date=date,
        timezone=timezone,
        now=now,
        config=config,
        freshness=freshness,
    )


# --------------------------------------------------------------------------
# A small realistic plan, for the renderer and brief workers
# --------------------------------------------------------------------------


def seed_realistic_day(conn: sqlite3.Connection) -> Dict[str, str]:
    """Populate `conn` with a day that exercises every display category.

    Shapes covered: a timed calendar meeting, a second timed meeting that
    overlaps it, an all-day SHARED calendar event, a precise deadline today, a
    week-precision overdue item, a due-soon item, a WAITING counterparty row, a
    no-date backlog row, and one Gmail-only SHARED row that must be filtered
    out and only counted.
    """
    reset_ids()
    ids: Dict[str, str] = {}

    ids["meeting"] = add_commitment(
        conn,
        title="定例ミーティング",
        responsibility="SELF",
        work_state="SCHEDULED",
        scheduled_at="2026-09-08T00:00:00+00:00",     # 09:00 JST
        scheduled_end_at="2026-09-08T01:00:00+00:00",  # 10:00 JST
        authority="calendar_authority",
        confidence=1.0,
        evidence=[("calendar", "calendar:event-meeting")],
    )
    ids["handover"] = add_commitment(
        conn,
        title="資料の受け渡し",
        responsibility="SELF",
        work_state="SCHEDULED",
        scheduled_at="2026-09-08T00:30:00+00:00",      # 09:30 JST — overlaps
        scheduled_end_at="2026-09-08T01:30:00+00:00",  # 10:30 JST
        authority="calendar_authority",
        confidence=1.0,
        evidence=[("calendar", "calendar:event-handover")],
    )
    ids["all_day"] = add_commitment(
        conn,
        title="全社研修",
        responsibility="SHARED",
        work_state="SCHEDULED",
        scheduled_at="2026-09-08",
        scheduled_end_at="2026-09-09",
        authority="calendar_authority",
        confidence=1.0,
        evidence=[("calendar", "calendar:event-training")],
    )
    ids["must_do"] = add_commitment(
        conn,
        title="会議資料の最終確認",
        responsibility="SELF",
        work_state="ACTIONABLE",
        due_at="2026-09-08T18:00:00+09:00",
        due_precision="minute",
        counterparty="担当A",
        evidence="discord",
    )
    ids["overdue"] = add_commitment(
        conn,
        title="見積書の再提出を依頼",
        responsibility="SELF",
        work_state="ACTIONABLE",
        due_at="2026-08-30T23:59:00+09:00",
        due_precision="week",
        counterparty="担当B",
        evidence="discord",
    )
    ids["next_up"] = add_commitment(
        conn,
        title="健康診断の予約",
        responsibility="SELF",
        work_state="ACTIONABLE",
        due_at="2026-09-10T00:00:00+09:00",
        due_precision="date",
        evidence="discord",
    )
    ids["waiting"] = add_commitment(
        conn,
        title="ロゴ掲載可否の回答",
        responsibility="COUNTERPARTY",
        work_state="WAITING",
        counterparty="担当C",
        updated_at="2026-09-07T07:48:49.560488+00:00",
        evidence="line",
    )
    ids["backlog"] = add_commitment(
        conn,
        title="備品の在庫を確認する",
        responsibility="SELF",
        work_state="ACTIONABLE",
        evidence="line",
    )
    ids["noise"] = add_commitment(
        conn,
        title="【本日限定】キャンペーンのお知らせ",
        responsibility="SHARED",
        work_state="ACTIONABLE",
        due_at="2026-09-08T17:00:00+09:00",
        due_precision="day",
        counterparty='"配信事務局" <news@example.invalid>',
        confidence=0.7,
        evidence="gmail",
    )
    return ids


#: The date I2-PROFILE.md section N recommends for the section 92 read-only
#: production audit: the only day in the next 14 with more than one scheduled
#: item, and equal to the busiest day in the whole dataset.
AUDIT_DATE = "2026-09-12"
AUDIT_NOW = datetime(2026, 9, 12, 8, 30, tzinfo=JST)


def seed_audit_day(conn: sqlite3.Connection) -> Dict[str, str]:
    """Mirror the shape of production's 2026-09-12 (I2-PROFILE.md section N).

    Same three rows, same timestamps, same responsibilities and precisions —
    an all-day SHARED calendar event, a timed SELF calendar event at 22:00-23:00
    JST, and a `daypart`-precision LINE deadline at 18:00 JST. Titles and ids
    are synthetic; only the shape is reproduced, because the real titles are
    the owner's own calendar entries.

    This is the fixture the reported "2026-09-12 shows N scheduled items"
    figure is measured against.
    """
    reset_ids()
    return {
        "all_day_shared": add_commitment(
            conn,
            title="終日の共同予定",
            responsibility="SHARED",
            work_state="SCHEDULED",
            scheduled_at="2026-09-12",
            scheduled_end_at="2026-09-13",
            authority="calendar_authority",
            confidence=1.0,
            evidence=[("calendar", "calendar:audit-all-day")],
        ),
        "timed_self": add_commitment(
            conn,
            title="夜の予定",
            responsibility="SELF",
            work_state="SCHEDULED",
            scheduled_at="2026-09-12T13:00:00+00:00",      # 22:00 JST
            scheduled_end_at="2026-09-12T14:00:00+00:00",  # 23:00 JST
            authority="calendar_authority",
            confidence=1.0,
            evidence=[("calendar", "calendar:audit-timed")],
        ),
        "daypart_deadline": add_commitment(
            conn,
            title="土曜夜の都合確認への回答",
            responsibility="SELF",
            work_state="ACTIONABLE",
            due_at="2026-09-12T18:00:00+09:00",
            due_precision="daypart",
            confidence=0.7,
            evidence=[("line", "line:audit-deadline")],
        ),
    }


def make_plan_fixture(
    *,
    date: str = DEFAULT_PLAN_DATE,
    now: Optional[datetime] = None,
    stale_sources: Sequence[str] = (),
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return a small realistic plan dict matching I2-CONTRACT.md section 3.

    Built by running the real planner over :func:`seed_realistic_day`, so the
    fixture cannot drift away from the schema the planner actually emits. The
    database is created, used and discarded inside this call.

    Pass `stale_sources` to get a plan carrying `data_freshness_warning`.
    """
    now = now or DEFAULT_NOW
    conn = open_db()
    try:
        seed_realistic_day(conn)
        return build_plan(
            conn,
            date=date,
            now=now,
            config=config,
            freshness=freshness_block(now, stale_sources=stale_sources),
        )
    finally:
        conn.close()


def make_stale_plan_fixture(**kwargs: Any) -> Dict[str, Any]:
    """A plan whose LINE source is stale (carries `data_freshness_warning`)."""
    kwargs.setdefault("stale_sources", ("line",))
    return make_plan_fixture(**kwargs)


#: Production's own mix, as measured (I2-PROFILE.md A.1/B.1): 786 commitments
#: whose shape is dominated by Gmail noise. A performance fixture built from a
#: uniform population would be an easier database than the real one, so this
#: reproduces the distribution rather than the row count alone.
PRODUCTION_MIX: Tuple[Tuple[str, str, str, int], ...] = (
    # (responsibility, work_state, evidence source_type, rows)
    ("INFORMATIONAL", "DISMISSED", "gmail", 365),
    ("SHARED", "ACTIONABLE", "gmail", 194),
    ("SHARED", "ACTIONABLE", "discord", 2),
    ("SHARED", "CANCELLED", "gmail", 61),
    ("SHARED", "SCHEDULED", "calendar", 57),
    ("SELF", "ACTIONABLE", "discord", 52),
    ("SELF", "ACTIONABLE", "gmail", 14),
    ("SELF", "ACTIONABLE", "line", 11),
    ("SELF", "SCHEDULED", "calendar", 22),
    ("COUNTERPARTY", "WAITING", "line", 7),
    ("SELF", "CANCELLED", "discord", 1),
)


def seed_production_sized(
    conn: sqlite3.Connection,
    *,
    plan_date: str = DEFAULT_PLAN_DATE,
    mix: Sequence[Tuple[str, str, str, int]] = PRODUCTION_MIX,
) -> int:
    """Fill `conn` with a production-sized, production-shaped commitment set.

    Roughly 786 commitments and 787 evidence rows, spread across a +/- 30 day
    window around `plan_date` so that every category has real work to do.
    Returns the number of commitments inserted. Written with `executemany` in
    one transaction: the point of the fixture is to time the planner, not the
    fixture.
    """
    anchor = datetime.fromisoformat(plan_date).replace(tzinfo=JST)
    commitments: List[tuple] = []
    evidence: List[tuple] = []
    index = 0

    for responsibility, work_state, source_type, count in mix:
        for offset in range(count):
            index += 1
            cid = f"cmt_perf{index:09d}"
            # Deterministic spread: -30 .. +30 days, with a wrapped hour.
            day_shift = (index * 7) % 61 - 30
            hour = 9 + (index % 12)
            when = anchor + timedelta(days=day_shift, hours=hour - 9)
            calendar_row = source_type == "calendar"
            due_at = None if calendar_row else when.isoformat()
            scheduled_at = when.isoformat() if calendar_row else None
            scheduled_end_at = (
                (when + timedelta(hours=1)).isoformat() if calendar_row else None
            )
            commitments.append(
                (
                    cid,
                    f"性能試験の項目 {index}",
                    None,
                    responsibility,
                    work_state,
                    f"担当{index % 17}" if source_type != "gmail" else None,
                    None,
                    due_at,
                    "minute" if due_at else None,
                    scheduled_at,
                    scheduled_end_at,
                    "calendar_authority" if calendar_row else "evidence",
                    1.0 if calendar_row else 0.85,
                    "current",
                    None,
                    "2026-09-01T04:01:35.611079+00:00",
                    "2026-09-07T07:48:49.560488+00:00",
                    None,
                    1,
                )
            )
            evidence.append(
                (
                    cid,
                    source_type,
                    f"{source_type}:{index}",
                    "2026-09-01T04:01:35.611079+00:00",
                    "schedule" if calendar_row else "origin",
                    0.9,
                    None,
                    "2026-09-01T04:01:35.611079+00:00",
                )
            )

    # One row with two evidence sources, matching the single production case.
    if evidence:
        evidence.append(
            (
                commitments[-1][0],
                "line",
                "line:multi-source",
                "2026-09-01T04:01:35.611079+00:00",
                "origin",
                0.85,
                None,
                "2026-09-01T04:01:35.611079+00:00",
            )
        )

    conn.executemany(
        """
        INSERT INTO commitments (
            commitment_id, title, summary, responsibility, work_state,
            counterparty, project_id, due_at, due_precision,
            scheduled_at, scheduled_end_at, authority, confidence,
            currentness, notes, created_at, updated_at, resolved_at, source_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        commitments,
    )
    conn.executemany(
        """
        INSERT INTO commitment_evidence (
            commitment_id, source_type, source_id, observed_at,
            evidence_role, confidence, snippet, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        evidence,
    )
    conn.commit()
    return len(commitments)


def count_sql_statements(conn: sqlite3.Connection) -> List[str]:
    """Install a trace callback and return the mutable list it appends to."""
    statements: List[str] = []
    conn.set_trace_callback(statements.append)
    return statements


__all__ = [
    "DEFAULT_NOW",
    "DEFAULT_PLAN_DATE",
    "FRESHNESS_SOURCES",
    "JST",
    "AUDIT_DATE",
    "AUDIT_NOW",
    "PRODUCTION_MIX",
    "seed_audit_day",
    "seed_production_sized",
    "ProductionAccessError",
    "add_commitment",
    "add_evidence",
    "build_plan",
    "count_sql_statements",
    "freshness_block",
    "make_plan_fixture",
    "make_stale_plan_fixture",
    "new_db",
    "next_commitment_id",
    "open_db",
    "reset_ids",
    "seed_realistic_day",
]
