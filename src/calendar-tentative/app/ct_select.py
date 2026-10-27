#!/usr/bin/env python3
"""Calendar Tentative Registration — which commitments become tentative events.

This module decides; it never calls Google. Its inputs are the canonical
commitments (`events.db`, read-only) and H.1's published Calendar snapshot
(`index.db`, read-only); its output is the set of events that *should* exist
on the tentative calendar, plus a reason for every commitment that should
not. The engine compares that against what it has already done.

The rules mirror the I.2 planner's eligibility (`planning/planner.py`) rather
than inventing new ones, because the planner's were measured against
production data and this layer feeds the same owner the same facts:

* Universal exclusions: `DONE`/`CANCELLED`/`DISMISSED`, `INFORMATIONAL`, and
  `currentness = 'superseded'`.
* The owner's work only: `SELF`, or `SHARED` when Discord/LINE/Calendar also
  carries it. `COUNTERPARTY` and `WAITING`/`BLOCKED` are someone else's next
  step, and a tentative slot on the owner's calendar would misstate that.
* Gmail-only evidence passes the mail screen in `ct_mail` first (I.1: 95%
  of Gmail-derived rows were newsletters and campaigns). By default only mail
  from people qualifies; `selection.gmail_only` widens or closes that.
* Anything Calendar already holds is not proposed again: `calendar_authority`
  rows, rows with Calendar evidence, and rows whose normalized title and time
  match an event in the published snapshot.
* This service's own entries are not "already on the calendar". They live on
  the primary calendar that H.1 reads, so they come back as snapshot rows and,
  through a Calendar backfill or merge, as Calendar evidence. Counting that
  echo would make every entry withdraw itself one H.1 sync after it was
  created. Every id this service makes starts with `hermesct`; snapshot rows,
  evidence and authority that rest only on such an id are ignored, and a
  commitment that is nothing but such an echo is never proposed.

Time is never guessed. A deadline needs a day (`day`/`date`) or a clock time
(`minute`/`exact_datetime`); `week`, `month`, `daypart`, `approximate` and
`unknown` are skipped with a reason. A deadline becomes an all-day entry on
its local date (with the clock time in the title when one is known); a
Discord/LINE `appointment_candidate` with a clock time becomes a timed entry.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import ct_mail

TERMINAL_WORK_STATES = frozenset({"DONE", "CANCELLED", "DISMISSED"})
NOT_OWNER_WORK_STATES = frozenset({"WAITING", "BLOCKED"})
NON_GMAIL_SOURCES = frozenset({"discord", "line", "calendar"})
CALENDAR_AUTHORITY = "calendar_authority"

#: `due_precision` values that carry a real clock time.
PRECISE_PRECISIONS = frozenset({"minute", "exact_datetime"})
#: Values that carry a real day but no meaningful time of day.
DAY_PRECISIONS = frozenset({"day", "date"})

#: Raw Event Layer types that describe something the owner attends rather
#: than something the owner must finish.
APPOINTMENT_TYPES = frozenset({"appointment_candidate"})

KIND_APPOINTMENT = "appointment"
KIND_DEADLINE = "deadline"
KIND_SCHEDULED = "scheduled"

# Skip reasons. A closed vocabulary: status output and tests key off these.
R_RESOLVED = "resolved"
R_SUPERSEDED = "superseded"
R_MISSING = "missing"
R_INFORMATIONAL = "informational"
R_NOT_OWNER_TASK = "not_owner_task"
R_SHARED_EXCLUDED = "shared_excluded"
R_SHARED_WITHOUT_CHAT = "shared_without_chat_evidence"
R_GMAIL_ONLY = "gmail_only"
R_GMAIL_BROADCAST = ct_mail.R_GMAIL_BROADCAST
R_GMAIL_NOTIFICATION = ct_mail.R_GMAIL_NOTIFICATION
R_GMAIL_UNCATEGORIZED = ct_mail.R_GMAIL_UNCATEGORIZED
R_GMAIL_LIST = ct_mail.R_GMAIL_LIST
R_GMAIL_BULK = ct_mail.R_GMAIL_BULK
R_GMAIL_UNSCREENED = ct_mail.R_GMAIL_UNSCREENED
R_GMAIL_DISCARDED = ct_mail.R_GMAIL_DISCARDED
R_GMAIL_DRAFT = ct_mail.R_GMAIL_DRAFT
R_ALREADY_ON_CALENDAR = "already_on_calendar"
R_MATCHES_EXISTING = "matches_existing_event"
R_LOW_CONFIDENCE = "low_confidence"
R_EMPTY_TITLE = "empty_title"
R_NO_DATE = "no_date"
R_IMPRECISE_TIME = "imprecise_time"
R_PAST = "past"
R_BEYOND_HORIZON = "beyond_horizon"
R_DUPLICATE_SOURCE = "duplicate_source"
R_OWN_ENTRY = "own_tentative_entry"

#: Every event id this service derives starts with this (event_id_for).
OWN_EVENT_PREFIX = "hermesct"

#: Reasons that mean "a tentative entry for this is now wrong", as opposed to
#: "it is no longer worth proposing" (past, beyond the horizon, reclassified).
#: Only these let the engine remove an entry it created. A draft is never
#: evidence of anything, so an entry built on one is wrong, not out of scope.
WITHDRAW_REASONS = frozenset({
    R_RESOLVED, R_SUPERSEDED, R_MISSING, R_ALREADY_ON_CALENDAR, R_MATCHES_EXISTING,
    R_GMAIL_DRAFT,
})

SOURCE_LABELS = {"discord": "Discord", "line": "LINE", "gmail": "Gmail",
                 "calendar": "カレンダー"}

WEEKDAYS_JA = "月火水木金土日"

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f​-‏ -‮⁦-⁩]")
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
#: LINE's internal user ids. Never shown, as in the planning renderer.
_OPAQUE_ID = re.compile(r"^[uU][0-9a-fA-F]{20,}$")
_LEADING_TAG = re.compile(r"^\s*(?:【[^】]{0,20}】|\[[^\]]{0,20}\]|［[^］]{0,20}］|〔[^〕]{0,20}〕)\s*")

TITLE_LIMIT = 120
COUNTERPARTY_LIMIT = 80


def clean_text(value, limit: int) -> str:
    """Single line, no control or bidi characters, bounded."""
    text = _CONTROL.sub("", str(value or ""))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def shown_counterparty(value) -> str | None:
    """The counterparty as the owner may see it on a calendar, or None.

    Gmail rows carry the raw From header (`名前 <address>`). A calendar entry
    the owner may move onto a shared calendar is no place for a third party's
    address, and guessing a display name out of a header is not done either,
    so anything with an address or an opaque platform id is left out.
    """
    text = clean_text(value, COUNTERPARTY_LIMIT)
    if not text or "@" in text or _OPAQUE_ID.match(text):
        return None
    return text


def is_own_event(value) -> bool:
    """An event id (or an H.1 `gcal:<calendar>:<event>` source id) this
    service created."""
    return str(value or "").rsplit(":", 1)[-1].startswith(OWN_EVENT_PREFIX)


def normalize_title(value) -> str:
    """Comparison key for "the same event": not a similarity measure.

    NFKC, casefold, leading bracket tags removed (【締切】, [仮], …) and all
    whitespace dropped. Two titles are equal or they are not; the planner's
    measurement that fuzzy matching produces hundreds of false pairs applies
    here as well.
    """
    text = unicodedata.normalize("NFKC", str(value or ""))
    previous = None
    while previous != text:
        previous = text
        text = _LEADING_TAG.sub("", text)
    return "".join(text.casefold().split())


def parse_stored(value, tz):
    """('date', date) | ('dt', aware datetime in tz) | (None, None)."""
    text = str(value or "").strip()
    if not text:
        return None, None
    if _DATE_ONLY.match(text):
        try:
            return "date", date.fromisoformat(text)
        except ValueError:
            return None, None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None, None
    if parsed.tzinfo is None:
        # Matches commitments.models.parse_iso: a naive value is UTC.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return "dt", parsed.astimezone(tz)


@dataclass(frozen=True)
class Desired:
    """One tentative event that should exist."""

    commitment_id: str
    kind: str
    title: str
    all_day: bool
    #: All-day: the local date. Timed: aware local start.
    start: object
    #: All-day: the exclusive end date. Timed: aware local end.
    end: object
    #: Deadline only: the local clock time, when the source carried one.
    deadline_time: str | None
    source_types: tuple
    counterparty: str | None
    source_count: int
    #: For a Gmail-only row: why the mail screen trusted it (ct_mail.BASIS_*).
    mail_basis: str | None = None

    @property
    def sort_key(self) -> tuple:
        if self.all_day:
            return (self.start.isoformat(), "", self.commitment_id)
        return (self.start.date().isoformat(), self.start.isoformat(), self.commitment_id)

    @property
    def local_date(self) -> date:
        return self.start if self.all_day else self.start.date()


@dataclass
class Selection:
    desired: dict = field(default_factory=dict)
    #: commitment_id -> reason, for every commitment that is not desired.
    skipped: dict = field(default_factory=dict)
    index_available: bool = False
    gmail_screen_available: bool = False
    #: (received, categorised) message counts; (n, 0) means Gmail's smart
    #: features are off and only what the owner did can admit mail.
    gmail_categorized: tuple | None = None

    def reason_for(self, commitment_id: str) -> str | None:
        """None when desired; R_MISSING when the commitment is gone."""
        if commitment_id in self.desired:
            return None
        return self.skipped.get(commitment_id, R_MISSING)

    def counts(self) -> dict:
        out: dict = {}
        for reason in self.skipped.values():
            out[reason] = out.get(reason, 0) + 1
        return dict(sorted(out.items()))


# ------------------------------------------------------------------ loading


def _table_columns(con, table: str) -> set:
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _load_commitments(con) -> list:
    cur = con.execute(
        """SELECT commitment_id, title, responsibility, work_state, counterparty,
                  due_at, due_precision, scheduled_at, scheduled_end_at, authority,
                  confidence, currentness, source_count
           FROM commitments"""
    )
    columns = [d[0] for d in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def _load_evidence(con) -> tuple:
    """(types, pairs, echoed): evidence without this service's own entries,
    and the commitments that carried any of those."""
    types: dict = {}
    pairs: dict = {}
    echoed: set = set()
    for cid, source_type, source_id in con.execute(
        "SELECT commitment_id, source_type, source_id FROM commitment_evidence"
    ):
        source_type, source_id = str(source_type or ""), str(source_id or "")
        if source_type == "calendar" and is_own_event(source_id):
            echoed.add(cid)
            continue
        types.setdefault(cid, set()).add(source_type)
        pairs.setdefault(cid, set()).add((source_type, source_id))
    return types, pairs, echoed


def _load_raw_types(con) -> dict:
    """commitment_id -> raw Event Layer types, joined through evidence.

    Commitments did not keep the extractor's `type`; the raw `events` row
    still has it. A missing table or column means "no type known", which
    makes every dated row a deadline — the conservative reading.
    """
    if not {"type", "source_type", "source_id"} <= _table_columns(con, "events"):
        return {}
    out: dict = {}
    for cid, etype in con.execute(
        """SELECT ce.commitment_id, e.type FROM commitment_evidence ce
           JOIN events e ON e.source_type = ce.source_type AND e.source_id = ce.source_id"""
    ):
        out.setdefault(cid, set()).add(str(etype or ""))
    return out


@dataclass(frozen=True)
class _IndexEvent:
    title_key: str
    all_day: bool
    start: object  # date for all-day, aware datetime otherwise


def _load_index(con, *, tz, own_prefix: str) -> dict:
    """title_key -> [_IndexEvent] for the snapshot's real events."""
    by_title: dict = {}
    cols = _table_columns(con, "calendar_events")
    if not {"event_id", "status", "summary", "start_at", "all_day"} <= cols:
        return by_title
    for row in con.execute(
        "SELECT event_id, status, summary, start_at, all_day FROM calendar_events"
    ):
        event_id, status, summary, start_at, all_day = row
        # Our own entry is never evidence that something is "already on a
        # calendar": that is how a tentative entry would withdraw itself. Its
        # title would match, too — normalize_title drops the 【仮】 tag. Once
        # the owner has taken the tag off it is a real event like any other.
        if is_own_event(event_id) and str(summary or "").lstrip().startswith(own_prefix):
            continue
        if str(status or "").lower() == "cancelled":
            continue
        kind, value = parse_stored(start_at, tz)
        if kind is None:
            continue
        key = normalize_title(summary)
        if not key:
            continue
        if kind == "date" or all_day:
            start = value if kind == "date" else value.date()
            by_title.setdefault(key, []).append(_IndexEvent(key, True, start))
        else:
            by_title.setdefault(key, []).append(_IndexEvent(key, False, value))
    return by_title


# ----------------------------------------------------------------- deciding


def _matches_existing(desired: Desired, by_title: dict, tolerance: timedelta) -> bool:
    for existing in by_title.get(normalize_title(desired.title), ()):
        if desired.all_day or existing.all_day:
            other = existing.start if existing.all_day else existing.start.date()
            if other == desired.local_date:
                return True
        elif abs(existing.start - desired.start) <= tolerance:
            return True
    return False


def _timing(row: dict, raw_types: set, tz, default_duration: timedelta):
    """(kind, all_day, start, end, deadline_time) or a skip reason string."""
    sched_kind, sched = parse_stored(row.get("scheduled_at"), tz)
    if sched_kind is not None:
        end_kind, end = parse_stored(row.get("scheduled_end_at"), tz)
        if sched_kind == "date":
            end_date = end if end_kind == "date" and end > sched else sched + timedelta(days=1)
            return KIND_SCHEDULED, True, sched, end_date, None
        if end_kind != "dt" or end <= sched:
            end = sched + default_duration
        return KIND_SCHEDULED, False, sched, end, None

    due_kind, due = parse_stored(row.get("due_at"), tz)
    if due_kind is None:
        return R_NO_DATE
    precision = str(row.get("due_precision") or "").strip().lower()
    if due_kind == "date":
        precise = False
    elif precision in PRECISE_PRECISIONS:
        precise = True
    elif precision in DAY_PRECISIONS:
        precise = False
    else:
        return R_IMPRECISE_TIME

    day = due if due_kind == "date" else due.date()
    if raw_types & APPOINTMENT_TYPES:
        if precise:
            return KIND_APPOINTMENT, False, due, due + default_duration, None
        return KIND_APPOINTMENT, True, day, day + timedelta(days=1), None
    clock = due.strftime("%H:%M") if precise else None
    return KIND_DEADLINE, True, day, day + timedelta(days=1), clock


def _gmail_only_ids(rows, types_by_id, pairs_by_id) -> set:
    """Gmail evidence ids of the open rows the mail screen may have to judge."""
    ids: set = set()
    for row in rows:
        cid = str(row["commitment_id"])
        types = types_by_id.get(cid, set())
        if (types and not types & NON_GMAIL_SOURCES and row.get("responsibility") == "SELF"
                and row.get("work_state") not in TERMINAL_WORK_STATES):
            ids |= {sid for stype, sid in pairs_by_id.get(cid, ()) if stype == "gmail"}
    return ids


def select(events_con, *, cfg, now: datetime, index_con=None, gmail_con=None) -> Selection:
    """Decide the full desired set. Read-only on every connection."""
    tz = cfg.tz
    sel_cfg = cfg.selection
    default_duration = timedelta(minutes=int(cfg.calendar["default_duration_minutes"]))
    tolerance = timedelta(minutes=int(sel_cfg["match_tolerance_minutes"]))
    now_local = now.astimezone(tz)
    today = now_local.date()
    horizon_end = now_local + timedelta(days=int(sel_cfg["horizon_days"]))

    own_prefix = cfg.calendar["summary_prefix"].strip()
    by_title: dict = {}
    selection = Selection()
    if index_con is not None:
        by_title = _load_index(index_con, tz=tz, own_prefix=own_prefix)
        selection.index_available = True

    rows = _load_commitments(events_con)
    types_by_id, pairs_by_id, echoed = _load_evidence(events_con)
    raw_types = _load_raw_types(events_con)

    gmail_mode = sel_cfg["gmail_only"]
    mail_facts = None
    if gmail_mode in (ct_mail.MODE_PEOPLE, ct_mail.MODE_PEOPLE_AND_NOTIFICATIONS) \
            and gmail_con is not None and ct_mail.index_usable(gmail_con):
        mail_facts = ct_mail.load_facts(gmail_con, _gmail_only_ids(rows, types_by_id, pairs_by_id))
        selection.gmail_screen_available = True
        selection.gmail_categorized = (mail_facts.received_total, mail_facts.received_categorized)

    candidates: list = []
    for row in rows:
        cid = str(row["commitment_id"])
        source_types = types_by_id.get(cid, set())
        has_chat = bool(source_types & NON_GMAIL_SOURCES)
        gmail_only = bool(source_types) and not has_chat

        own_title = str(row.get("title") or "").lstrip().startswith(own_prefix)
        if cid in echoed and (not source_types or own_title):
            # A Calendar copy of one of our own entries — alone, or with
            # evidence merged into it (it still carries our title). Proposing
            # it would put 【仮】 on top of 【仮】.
            selection.skipped[cid] = R_OWN_ENTRY
            continue
        if row.get("work_state") in TERMINAL_WORK_STATES:
            selection.skipped[cid] = R_RESOLVED
            continue
        if str(row.get("currentness") or "") == "superseded":
            selection.skipped[cid] = R_SUPERSEDED
            continue
        responsibility = row.get("responsibility")
        if responsibility == "INFORMATIONAL":
            selection.skipped[cid] = R_INFORMATIONAL
            continue
        if responsibility not in ("SELF", "SHARED") or row.get("work_state") in NOT_OWNER_WORK_STATES:
            selection.skipped[cid] = R_NOT_OWNER_TASK
            continue
        if responsibility == "SHARED":
            if not sel_cfg["include_shared"]:
                selection.skipped[cid] = R_SHARED_EXCLUDED
                continue
            if not has_chat:
                selection.skipped[cid] = R_SHARED_WITHOUT_CHAT
                continue
        mail_basis = None
        if gmail_only and gmail_mode == ct_mail.MODE_OFF:
            selection.skipped[cid] = R_GMAIL_ONLY
            continue
        if gmail_only and gmail_mode != ct_mail.MODE_ALL:
            gmail_ids = [sid for stype, sid in pairs_by_id.get(cid, ()) if stype == "gmail"]
            passed, label = ct_mail.screen_commitment(gmail_ids, mail_facts, gmail_mode)
            if not passed:
                selection.skipped[cid] = label
                continue
            mail_basis = label
        # A merge with one of our own entries hands its calendar authority to
        # the merged row; that authority is our echo, not a real event.
        authority_is_echo = cid in echoed and "calendar" not in source_types
        if (row.get("authority") == CALENDAR_AUTHORITY and not authority_is_echo) \
                or "calendar" in source_types:
            selection.skipped[cid] = R_ALREADY_ON_CALENDAR
            continue
        try:
            confidence = float(row.get("confidence") if row.get("confidence") is not None else 1.0)
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < float(sel_cfg["min_confidence"]):
            selection.skipped[cid] = R_LOW_CONFIDENCE
            continue
        title = clean_text(row.get("title"), TITLE_LIMIT)
        if not normalize_title(title):
            selection.skipped[cid] = R_EMPTY_TITLE
            continue

        timing = _timing(row, raw_types.get(cid, set()), tz, default_duration)
        if isinstance(timing, str):
            selection.skipped[cid] = timing
            continue
        kind, all_day, start, end, clock = timing
        if all_day:
            if start < today:
                selection.skipped[cid] = R_PAST
                continue
            if start > horizon_end.date():
                selection.skipped[cid] = R_BEYOND_HORIZON
                continue
        else:
            if start < now_local:
                selection.skipped[cid] = R_PAST
                continue
            if start > horizon_end:
                selection.skipped[cid] = R_BEYOND_HORIZON
                continue

        desired = Desired(
            commitment_id=cid, kind=kind, title=title, all_day=all_day,
            start=start, end=end, deadline_time=clock,
            source_types=tuple(sorted(t for t in source_types if t)),
            counterparty=shown_counterparty(row.get("counterparty")),
            source_count=int(row.get("source_count") or 1),
            mail_basis=mail_basis,
        )
        if by_title and _matches_existing(desired, by_title, tolerance):
            selection.skipped[cid] = R_MATCHES_EXISTING
            continue
        candidates.append(desired)

    # Two commitments citing the same source message are one item (planner
    # dedup rule 1). Survivor: more evidence, then the smaller id.
    owner_of_pair: dict = {}
    for desired in sorted(candidates, key=lambda d: (-d.source_count, d.commitment_id)):
        pairs = pairs_by_id.get(desired.commitment_id, set())
        if any(pair in owner_of_pair for pair in pairs):
            selection.skipped[desired.commitment_id] = R_DUPLICATE_SOURCE
            continue
        for pair in pairs:
            owner_of_pair[pair] = desired.commitment_id
        selection.desired[desired.commitment_id] = desired
    return selection


# ------------------------------------------------------------- event body


def event_id_for(calendar_id: str, commitment_id: str, generation: int) -> str:
    """Deterministic Google event id: base32hex alphabet, 5..1024 chars.

    Hex digits are a subset of base32hex, and so is the `hermesct` prefix.
    The same (calendar, commitment, generation) always yields the same id,
    which is what turns a retried insert after a lost response into a 409
    instead of a second event. The generation only advances when a retired
    entry is deliberately re-proposed, because Google keeps deleted ids.
    """
    digest = hashlib.sha256(
        f"{calendar_id}\n{commitment_id}\n{int(generation)}".encode("utf-8")
    ).hexdigest()
    return "hermesct" + digest[:40]


def _md(day: date) -> str:
    return f"{day.month}/{day.day}({WEEKDAYS_JA[day.weekday()]})"


def summary_for(desired: Desired, prefix: str) -> str:
    if desired.kind == KIND_DEADLINE:
        core = f"〆{desired.deadline_time} {desired.title}" if desired.deadline_time \
            else f"〆 {desired.title}"
    else:
        core = desired.title
    return clean_text(f"{prefix}{core}", 200)


def description_for(desired: Desired, prefix: str) -> str:
    sources = "・".join(SOURCE_LABELS.get(t, t) for t in desired.source_types) or "記録"
    what = "締切" if desired.kind == KIND_DEADLINE else "予定"
    lines = [f"Hermes が {sources} の記録から仮登録した{what}です。"]
    if desired.kind == KIND_DEADLINE:
        when = _md(desired.start)
        lines.append(f"期限: {when} {desired.deadline_time}" if desired.deadline_time
                     else f"期限: {when}")
    if desired.counterparty:
        lines.append(f"相手: {desired.counterparty}")
    if desired.mail_basis in ct_mail.BASIS_LABELS:
        lines.append(f"根拠: {ct_mail.BASIS_LABELS[desired.mail_basis]}")
    lines.append(f"確定するならタイトル先頭の{prefix.strip()}を消し、不要ならこの予定を削除してください。"
                 "削除した仮登録は再登録しません。")
    lines.append(f"ref: {desired.commitment_id}")
    return "\n".join(lines)


def _when(value, all_day: bool, tz_name: str) -> dict:
    if all_day:
        return {"date": value.isoformat()}
    return {"dateTime": value.replace(microsecond=0).isoformat(), "timeZone": tz_name}


def build_event_body(desired: Desired, *, cfg) -> dict:
    """The meaningful part of the event resource. Ids and markers are added
    by the engine; this is what `content_revision` covers."""
    tz_name = cfg.calendar["time_zone"]
    body = {
        "summary": summary_for(desired, cfg.calendar["summary_prefix"]),
        "description": description_for(desired, cfg.calendar["summary_prefix"]),
        "start": _when(desired.start, desired.all_day, tz_name),
        "end": _when(desired.end, desired.all_day, tz_name),
        # Shown as tentative, and never blocks free/busy: nobody has agreed to
        # this slot yet, least of all the owner.
        "status": "tentative",
        "transparency": "transparent",
    }
    if cfg.calendar["color_id"]:
        body["colorId"] = cfg.calendar["color_id"]
    return body


def content_revision(body: dict) -> str:
    raw = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def line_for(desired: Desired) -> str:
    """One notification line: date, time when there is one, title."""
    if desired.kind == KIND_DEADLINE:
        clock = f"〆{desired.deadline_time}" if desired.deadline_time else "〆"
        return clean_text(f"{_md(desired.start)} {clock} {desired.title}", 90)
    if desired.all_day:
        return clean_text(f"{_md(desired.start)} 終日 {desired.title}", 90)
    return clean_text(f"{_md(desired.start.date())} {desired.start:%H:%M} {desired.title}", 90)
