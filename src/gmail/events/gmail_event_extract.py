#!/usr/bin/env python3
"""Deterministic Gmail commitment ingestion into the existing Event Layer.

This bridge has no Gmail OAuth credential and reads only the published Gmail
index.  It writes the existing events.db and a minimal reconciliation snapshot
for Calendar; it never sends mail, modifies labels, opens URLs, or executes
attachments.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


SOURCE_TYPE = "gmail"
JST = timezone(timedelta(hours=9))
EVENT_TYPES = {"request", "commitment", "deadline", "other"}
TERMINAL = {"resolved", "ignored", "cancelled", "superseded", "done", "closed"}
PROMISE_RE = re.compile(r"(?:します|いたします|提出します|送ります|連絡します|確認します|対応します|返信します|i[' ]?ll|i will)", re.IGNORECASE)
REQUEST_RE = re.compile(r"(?:お願いします|ください|ご対応|ご確認|ご提出|ご返信|please|could you|can you)", re.IGNORECASE)
CANCEL_RE = re.compile(r"(?:キャンセル|中止|取り消し|延期|reschedule|cancelled?|canceled?)", re.IGNORECASE)
DATE_RE = re.compile(r"(?:(?P<month>\d{1,2})[月/](?P<day>\d{1,2})日?|(?P<today>今日|明日|あした|来週))")
TIME_RE = re.compile(r"(?P<hour>[01]?\d|2[0-3])(?:[:時](?P<minute>[0-5]\d)?)")

RECONCILE_SCHEMA = """
CREATE TABLE events (
  event_id TEXT PRIMARY KEY,
  summary TEXT NOT NULL,
  due_at TEXT,
  status TEXT NOT NULL,
  source_type TEXT NOT NULL
);
CREATE TABLE event_calendar_links (
  event_id TEXT NOT NULL,
  calendar_source_id TEXT NOT NULL,
  link_type TEXT NOT NULL,
  confidence REAL NOT NULL,
  PRIMARY KEY(event_id,calendar_source_id)
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_received(value: str | None) -> datetime:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def _due(text: str, received_at: str | None) -> tuple[str | None, str]:
    """Extract only explicit, bounded dates; a mention alone is not certainty."""
    match = DATE_RE.search(text)
    if not match:
        return None, "unknown"
    base = _parse_received(received_at).astimezone(JST)
    if match.group("today"):
        word = match.group("today")
        if word in ("明日", "あした"):
            day = base.date() + timedelta(days=1)
        elif word == "来週":
            day = base.date() + timedelta(days=7)
        else:
            day = base.date()
    else:
        month, day_num = int(match.group("month")), int(match.group("day"))
        year = base.year
        try:
            day = datetime(year, month, day_num).date()
        except ValueError:
            return None, "unknown"
        if day < base.date() - timedelta(days=1):
            day = datetime(year + 1, month, day_num).date()
    after = text[match.end():match.end() + 24]
    time_match = TIME_RE.search(after)
    if time_match:
        hour = int(time_match.group("hour"))
        minute = int(time_match.group("minute") or 0)
        return datetime(day.year, day.month, day.day, hour, minute, tzinfo=JST).isoformat(), "minute"
    return datetime(day.year, day.month, day.day, 17, 0, tzinfo=JST).isoformat(), "day"


def candidate_for(row) -> dict | None:
    """Return a conservative commitment candidate, never an instruction."""
    if row["deleted_at"]:
        return None
    labels = set(json.loads(row["labels_json"] or "[]"))
    if {"SPAM", "TRASH"} & labels:
        return None
    text = str(row["commitment_text"] or "")
    if not text.strip():
        return None
    if CANCEL_RE.search(text):
        return {
            "kind": "cancellation", "confidence": 0.9, "actionability": 0.0,
            "due_at": None, "due_precision": "unknown", "reason": "explicit_cancellation",
        }
    sent = bool(row["is_sent"])
    due_at, precision = _due(text, row["received_at"])
    if sent and PROMISE_RE.search(text):
        return {
            "kind": "commitment", "confidence": 0.9 if due_at else 0.76,
            "actionability": 0.85, "due_at": due_at, "due_precision": precision,
            "reason": "sent_explicit_promise",
        }
    if not sent and REQUEST_RE.search(text):
        return {
            "kind": "request", "confidence": 0.84 if due_at else 0.72,
            "actionability": 0.78, "due_at": due_at, "due_precision": precision,
            "reason": "received_explicit_request",
        }
    return None


def event_id_for(source_id: str, kind: str) -> str:
    return "gmev_" + hashlib.sha256(f"{source_id}:{kind}".encode("utf-8")).hexdigest()[:24]


def _history(con, event_id: str, field: str, old: str, new: str) -> None:
    con.execute(
        "INSERT INTO event_history(event_id,changed_at,field,old_value,new_value,actor) VALUES(?,?,?,?,?,?)",
        (event_id, now_iso(), field, old, new, "system"),
    )


def _ensure_link_schema(con) -> None:
    con.execute(
        """CREATE TABLE IF NOT EXISTS event_calendar_links (
           event_id TEXT NOT NULL,
           calendar_source_id TEXT NOT NULL,
           link_type TEXT NOT NULL,
           confidence REAL NOT NULL DEFAULT 1.0,
           created_at TEXT NOT NULL,
           updated_at TEXT NOT NULL,
           PRIMARY KEY(event_id,calendar_source_id)
        )"""
    )


def _mark_processed(con, row, result: str) -> None:
    con.execute(
        "INSERT OR REPLACE INTO processed_messages(source_id,sent_at,processed_at,result,content_hash)"
        " VALUES(?,?,?,?,?)",
        (row["source_id"], int(row["internal_date_ms"] or 0), now_iso(), result, row["content_hash"]),
    )


def _already_processed(con, row) -> bool:
    item = con.execute(
        "SELECT content_hash FROM processed_messages WHERE source_id=?", (row["source_id"],)
    ).fetchone()
    return bool(item and str(item["content_hash"] or "") == str(row["content_hash"] or ""))


def _cancel_single_thread_event(con, row) -> bool:
    rows = con.execute(
        "SELECT event_id,status FROM events WHERE source_type=? AND source_conversation=? AND status='open'",
        (SOURCE_TYPE, f"gmail:{row['thread_id']}"),
    ).fetchall()
    if len(rows) != 1:
        return False
    event_id = rows[0]["event_id"]
    con.execute("UPDATE events SET status='cancelled',updated_at=? WHERE event_id=?", (now_iso(), event_id))
    _history(con, event_id, "status", "open", "cancelled")
    return True


def _supersede_prior(con, row, event_id: str, kind: str) -> None:
    rows = con.execute(
        """SELECT event_id FROM events WHERE source_type=? AND source_conversation=? AND type=?
           AND status='open' AND event_id<>?""",
        (SOURCE_TYPE, f"gmail:{row['thread_id']}", kind, event_id),
    ).fetchall()
    if len(rows) == 1:
        old = rows[0]["event_id"]
        con.execute(
            "UPDATE events SET status='superseded',superseded_by=?,updated_at=? WHERE event_id=?",
            (event_id, now_iso(), old),
        )
        _history(con, old, "status", "open", "superseded")


def write_candidate(con, row, candidate: dict) -> str:
    if candidate["kind"] == "cancellation":
        return "cancelled_thread" if _cancel_single_thread_event(con, row) else "cancellation_evidence"
    kind = candidate["kind"]
    if kind not in EVENT_TYPES:
        return "no_candidate"
    event_id = event_id_for(row["source_id"], kind)
    existing = con.execute("SELECT status FROM events WHERE event_id=?", (event_id,)).fetchone()
    if existing and str(existing["status"]) in TERMINAL:
        _history(con, event_id, "email_reevaluated", str(existing["status"]), str(existing["status"]))
        return "reeval_blocked"
    subject = " ".join(str(row["subject"] or "").split())[:200] or "(no subject)"
    # Initial bounded sync is historical baseline. It is useful evidence, but
    # it must not create an immediate Attention flood merely because indexing
    # happened today.
    status = "open" if float(candidate["confidence"]) >= 0.70 and not row["baseline"] else "candidate"
    values = {
        "summary": subject, "actionability": float(candidate["actionability"]),
        "importance": 0.6, "urgency": 0.65 if candidate["due_at"] else 0.35,
        "confidence": float(candidate["confidence"]), "due_at": candidate["due_at"],
        "due_precision": candidate["due_precision"], "reason": candidate["reason"],
    }
    if existing:
        con.execute(
            """UPDATE events SET summary=?,status=?,actionability=?,importance=?,urgency=?,confidence=?,
               due_at=?,due_precision=?,updated_at=?,classification_reason=?,needs_human_review=0,
               attention=? WHERE event_id=?""",
            (values["summary"], status, values["actionability"], values["importance"], values["urgency"],
             values["confidence"], values["due_at"], values["due_precision"], now_iso(),
             values["reason"], values["importance"] * values["urgency"], event_id),
        )
        _history(con, event_id, "email_evidence_update", "1", "0")
        return "updated"
    con.execute(
        """INSERT INTO events(event_id,type,subtype,summary,actor,counterparty,status,actionability,
           importance,urgency,confidence,occurred_at,due_at,due_precision,created_at,updated_at,
           project_id,source_type,source_id,source_timestamp,source_conversation,source_speaker,
           evidence_count,supersedes,classification_reason,needs_human_review,attention,classifier_version)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            event_id, kind, "gmail", values["summary"], "mine" if row["is_sent"] else "counterparty",
            row["from_header"][:200] if not row["is_sent"] else None, status,
            values["actionability"], values["importance"], values["urgency"], values["confidence"],
            row["received_at"], values["due_at"], values["due_precision"], now_iso(), now_iso(), None,
            SOURCE_TYPE, row["source_id"], row["received_at"], f"gmail:{row['thread_id']}",
            row["from_header"][:200], 1, None, values["reason"], 0,
            values["importance"] * values["urgency"], "gmail-deterministic-v1",
        ),
    )
    _supersede_prior(con, row, event_id, kind)
    return "created"


def _atomic_publish(events_con, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".calendar-reconcile-", suffix=".db", dir=str(destination.parent))
    os.close(fd)
    temporary = Path(temp_name)
    try:
        index = sqlite3.connect(temporary)
        try:
            index.executescript(RECONCILE_SCHEMA)
            rows = events_con.execute(
                "SELECT event_id,summary,due_at,status,source_type FROM events"
            ).fetchall()
            index.executemany("INSERT INTO events VALUES(?,?,?,?,?)", [tuple(row) for row in rows])
            try:
                links = events_con.execute(
                    "SELECT event_id,calendar_source_id,link_type,confidence FROM event_calendar_links"
                ).fetchall()
            except sqlite3.Error:
                links = []
            index.executemany("INSERT INTO event_calendar_links VALUES(?,?,?,?)", [tuple(row) for row in links])
            index.commit()
            index.execute("PRAGMA journal_mode=DELETE")
            index.execute("VACUUM")
            index.commit()
        finally:
            index.close()
        if os.name == "nt" and destination.exists():
            os.chmod(destination, 0o644)
        if os.name != "nt":
            os.chown(temporary, -1, os.getgid())
            os.chmod(temporary, 0o640)
        os.replace(temporary, destination)
        if os.name != "nt":
            os.chmod(destination, 0o640)
    finally:
        if temporary.exists():
            temporary.unlink()


def process(source_db: str, events_db: str, reconciliation_index: str) -> dict:
    source = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True, timeout=10)
    source.row_factory = sqlite3.Row
    events = sqlite3.connect(events_db, timeout=30)
    events.row_factory = sqlite3.Row
    events.execute("PRAGMA busy_timeout=30000")
    try:
        _ensure_link_schema(events)
        rows = source.execute(
            "SELECT * FROM gmail_messages WHERE deleted_at IS NULL AND content_state='available' "
            "ORDER BY internal_date_ms,message_id"
        ).fetchall()
        counts = {"scanned": len(rows), "created": 0, "updated": 0, "cancelled": 0, "no_candidate": 0}
        with events:
            for row in rows:
                if _already_processed(events, row):
                    continue
                candidate = candidate_for(row)
                if candidate is None:
                    _mark_processed(events, row, "no_candidate")
                    counts["no_candidate"] += 1
                    continue
                result = write_candidate(events, row, candidate)
                _mark_processed(events, row, result)
                if result in counts:
                    counts[result] += 1
                elif result == "cancelled_thread":
                    counts["cancelled"] += 1
        _atomic_publish(events, Path(reconciliation_index))
        return counts
    finally:
        source.close()
        events.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Gmail evidence -> existing Event Layer")
    parser.add_argument("--source-db", default="/srv/hermes/gmail/index/index.db")
    parser.add_argument("--db", default="/srv/hermes/hermes-data/events/events.db")
    parser.add_argument("--reconciliation-index", default="/srv/hermes/gmail-event/reconcile/calendar-reconcile.db")
    args = parser.parse_args(argv)
    try:
        result = process(args.source_db, args.db, args.reconciliation_index)
    except sqlite3.Error as exc:
        print(f"gmail event ingestion failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print("gmail event ingestion " + " ".join(f"{key}={value}" for key, value in sorted(result.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
