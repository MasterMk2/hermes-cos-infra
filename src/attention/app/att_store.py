#!/usr/bin/env python3
"""Attention Engine — durable operations.

Every state change goes through here, so the properties that decide whether
the owner is bothered twice are provable in one file:

* **A signal seen twice is one row.** `upsert_signal` collapses on the derived
  id, so a deadline rediscovered on all 288 scans of a day is one signal with
  a `seen_count`, not 288 candidates (section 16).

* **A notification is created once per dedupe key per cooldown.**
  `recent_delivery` is the check that turns "we already said this" into a
  deterministic SUPPRESS rather than a judgement call (sections 18, 64).

* **A LINE credit is reserved before it is spent.** `reserve_channel_budget`
  inserts and counts inside one transaction, so two notifications decided in
  the same scan cannot both believe they hold the last credit (section 82).

* **DELIVERED means an adapter said so.** Nothing here sets that state from
  optimism; `mark_notification` is called with the gateway's answer, and the
  gateway sets its own row from the platform's answer (section 88).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from att_schema import POLICY_VERSION, get_meta, set_meta
from att_time import iso, parse_iso


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _minus(seconds: float, *, now=None) -> str:
    base = now or datetime.now(timezone.utc)
    return iso(base - timedelta(seconds=seconds))


def content_revision(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:32]


def new_notification_id(dedupe_key: str, *, kind: str = "signal") -> str:
    """Derived from the dedupe key, so a retry after a crash is a retry.

    The gateway keys its own delivery row on (notification_id, channel), so a
    notification id that changed between attempts would look like new work and
    could cost a second metered push (sections 46, 87, 121).
    """
    digest = hashlib.sha256(f"{kind}:{dedupe_key}".encode("utf-8")).hexdigest()
    return f"ntf_{digest[:24]}"


# ----------------------------------------------------------- signals


def upsert_signal(con: sqlite3.Connection, signal) -> tuple:
    """Insert or refresh one signal. Returns (row, is_new).

    A signal already present is touched, not replaced: `observed_at` is when
    we FIRST saw it, and overwriting it would make every backfilled event
    look like it arrived just now — which is precisely the mistake section 25
    is about.
    """
    row = signal.to_row()
    now = now_iso()
    try:
        con.execute(
            "INSERT INTO signals ("
            " signal_id, source_type, source_id, signal_type, created_at,"
            " observed_at, last_seen_at, subject, summary, severity, urgency,"
            " confidence, actionability, due_at, expires_at, dedupe_key,"
            " entity_key, recommended_channel, metadata, state, seen_count"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'NEW',1)",
            (row["signal_id"], row["source_type"], row["source_id"],
             row["signal_type"], row["created_at"], now, now, row["subject"],
             row["summary"], row["severity"], row["urgency"], row["confidence"],
             row["actionability"], row["due_at"], row["expires_at"],
             row["dedupe_key"], row["entity_key"], row["recommended_channel"],
             row["metadata"]),
        )
    except sqlite3.IntegrityError:
        existing = get_signal(con, row["signal_id"])
        if not _signal_changed(existing, row) and _seen_recently(existing, now):
            # Nothing about this signal has changed and we touched it not long
            # ago, so there is nothing to write. Section 138: the state
            # database lives on a USB flash disk, and this runs 288 times a
            # day — rewriting every open signal's row on every pass would be
            # thousands of page writes to record that nothing happened.
            # `last_seen_at` and `seen_count` become coarse, which is what
            # they are for.
            return existing, False
        con.execute(
            "UPDATE signals SET last_seen_at = ?, seen_count = seen_count + 1,"
            " summary = ?, severity = ?, urgency = ?, confidence = ?,"
            " actionability = ?, due_at = ?, metadata = ?"
            " WHERE signal_id = ?",
            (now, row["summary"], row["severity"], row["urgency"],
             row["confidence"], row["actionability"], row["due_at"],
             row["metadata"], row["signal_id"]),
        )
        return get_signal(con, row["signal_id"]), False
    return get_signal(con, row["signal_id"]), True


#: How long a signal may go untouched before its row is refreshed even though
#: nothing about it changed. Long, because the only thing the refresh buys is
#: a more precise "last seen".
TOUCH_INTERVAL_SECONDS = 3600

_MATERIAL_FIELDS = ("summary", "severity", "urgency", "confidence",
                    "actionability", "due_at", "metadata")


def _signal_changed(existing, row) -> bool:
    if existing is None:
        return True
    return any(existing[f] != row[f] for f in _MATERIAL_FIELDS)


def _seen_recently(existing, now: str) -> bool:
    seen = parse_iso(existing["last_seen_at"]) if existing else None
    if seen is None:
        return False
    current = parse_iso(now)
    if current is None:
        return False
    return (current - seen).total_seconds() < TOUCH_INTERVAL_SECONDS


def get_signal(con: sqlite3.Connection, signal_id: str):
    return con.execute(
        "SELECT * FROM signals WHERE signal_id = ?", (signal_id,)
    ).fetchone()


def mark_signal_decided(con: sqlite3.Connection, signal_id: str) -> None:
    con.execute(
        "UPDATE signals SET state = 'DECIDED' WHERE signal_id = ?", (signal_id,)
    )


def undecided_signals(con: sqlite3.Connection, limit: int = 200) -> list:
    return con.execute(
        "SELECT * FROM signals WHERE state = 'NEW'"
        " ORDER BY observed_at, rowid LIMIT ?", (limit,)
    ).fetchall()


# --------------------------------------------------------- decisions


def record_decision(con: sqlite3.Connection, *, signal_id: str, decision: str,
                    reason: str, severity: str, channels=None,
                    budget_state=None, detail=None, notification_id=None,
                    deliver_after=None) -> int:
    """Write the decision AND why, with the policy version (sections 76-79).

    "The assistant thought it was important" is not a record. A machine
    readable reason plus the version of the rules that produced it is what
    makes a decision explainable a month later, and what makes a change in
    behaviour attributable to a change in policy rather than to mood.
    """
    cur = con.execute(
        "INSERT INTO attention_decisions ("
        " signal_id, decided_at, decision, reason, policy_version, severity,"
        " channels, budget_state, detail, notification_id, deliver_after"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (signal_id, now_iso(), decision, reason, POLICY_VERSION, severity,
         json.dumps(list(channels or []), ensure_ascii=False), budget_state,
         (str(detail)[:300] if detail else None), notification_id,
         deliver_after),
    )
    return cur.lastrowid


def deferred_due(con: sqlite3.Connection, *, now=None) -> list:
    """Deferred decisions whose hold has expired."""
    return con.execute(
        "SELECT d.*, s.* FROM attention_decisions d"
        " JOIN signals s ON s.signal_id = d.signal_id"
        " WHERE d.decision = 'DEFER' AND d.deliver_after IS NOT NULL"
        "   AND d.deliver_after <= ?"
        "   AND NOT EXISTS (SELECT 1 FROM attention_decisions d2"
        "                   WHERE d2.signal_id = d.signal_id"
        "                     AND d2.decision_id > d.decision_id)"
        " ORDER BY d.deliver_after",
        (now or now_iso(),),
    ).fetchall()


# ------------------------------------------------------ notifications


def recent_delivery(con: sqlite3.Connection, dedupe_key: str,
                    cooldown_seconds: int, *, now=None):
    """The most recent notification for this key inside its cooldown, or None.

    Counts anything that reached DELIVERING or beyond. A notification that was
    SUPPRESSED or DEFERRED did not reach the owner and must not silence the
    next attempt — that would turn a deferral into a permanent loss.
    """
    cutoff = _minus(cooldown_seconds, now=now)
    return con.execute(
        "SELECT * FROM notifications WHERE dedupe_key = ?"
        "   AND state IN ('DELIVERING','DELIVERED')"
        "   AND created_at >= ?"
        " ORDER BY created_at DESC LIMIT 1",
        (dedupe_key, cutoff),
    ).fetchone()


def any_delivery(con: sqlite3.Connection, dedupe_key: str):
    return con.execute(
        "SELECT * FROM notifications WHERE dedupe_key = ?"
        "   AND state IN ('DELIVERING','DELIVERED')"
        " ORDER BY created_at DESC LIMIT 1",
        (dedupe_key,),
    ).fetchone()


def create_notification(con: sqlite3.Connection, *, dedupe_key: str,
                        severity: str, subject: str, body: str, reason: str,
                        kind: str = "signal", source_type=None,
                        source_id=None) -> tuple:
    """Reserve one notification. Returns (row, is_new).

    The id is derived from the dedupe key, so a crash between deciding and
    delivering finds the row it already made instead of minting a second one.
    """
    nid = new_notification_id(dedupe_key, kind=kind)
    now = now_iso()
    rev = content_revision(body)
    try:
        con.execute(
            "INSERT INTO notifications ("
            " notification_id, kind, dedupe_key, severity, subject, body,"
            " reason, policy_version, source_type, source_id,"
            " content_revision, state, created_at, updated_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,'PENDING',?,?)",
            (nid, kind, dedupe_key, severity, subject, body, reason,
             POLICY_VERSION, source_type, source_id, rev, now, now),
        )
    except sqlite3.IntegrityError:
        return get_notification(con, nid), False
    return get_notification(con, nid), True


def get_notification(con: sqlite3.Connection, notification_id: str):
    return con.execute(
        "SELECT * FROM notifications WHERE notification_id = ?",
        (notification_id,),
    ).fetchone()


def mark_notification(con: sqlite3.Connection, notification_id: str,
                      state: str, *, error=None, channels=None,
                      count_attempt: bool = False) -> None:
    now = now_iso()
    fields = {"state": state, "updated_at": now}
    if state == "DELIVERED":
        fields["delivered_at"] = now
        fields["last_error"] = None
    if error is not None:
        fields["last_error"] = str(error)[:300]
    if channels is not None:
        fields["delivered_channels"] = json.dumps(list(channels),
                                                  ensure_ascii=False)
    cols = ", ".join(f"{k} = ?" for k in fields)
    extra = ", attempts = attempts + 1" if count_attempt else ""
    con.execute(
        f"UPDATE notifications SET {cols}{extra} WHERE notification_id = ?",
        list(fields.values()) + [notification_id],
    )


def record_attempt(con: sqlite3.Connection, notification_id: str, *,
                   channel: str, outcome: str, detail=None,
                   quota_consumed: bool = False, http_status=None) -> None:
    con.execute(
        "INSERT INTO delivery_attempts (notification_id, channel, attempted_at,"
        " outcome, detail, quota_consumed, http_status) VALUES (?,?,?,?,?,?,?)",
        (notification_id, channel, now_iso(), outcome,
         (str(detail)[:200] if detail else None),
         int(bool(quota_consumed)), http_status),
    )


def stuck_notifications(con: sqlite3.Connection, *, lease_seconds: int = 600,
                        now=None) -> list:
    """DELIVERING rows that stopped moving (section 89).

    A row here means the engine handed the notification to the gateway and
    never learned the outcome — a crash, or a gateway that went away
    mid-request. Retrying is safe because the gateway is idempotent on
    (notification_id, channel): a delivery that did complete comes back as
    `duplicate` rather than as a second message.
    """
    return con.execute(
        "SELECT * FROM notifications WHERE state IN ('PENDING','DELIVERING')"
        "   AND updated_at < ? ORDER BY created_at LIMIT 50",
        (_minus(lease_seconds, now=now),),
    ).fetchall()


# ------------------------------------------------------ health episodes


def episode_id(service: str, reason: str, started_at: str) -> str:
    digest = hashlib.sha256(
        f"{service}:{reason}:{started_at}".encode("utf-8")
    ).hexdigest()
    return f"ep_{digest[:20]}"


def open_episode(con: sqlite3.Connection, service: str, reason: str):
    """The currently degraded episode for this service+reason, or None."""
    return con.execute(
        "SELECT * FROM notification_episodes WHERE service = ? AND reason = ?"
        "   AND state = 'DEGRADED' ORDER BY first_degraded_at DESC LIMIT 1",
        (service, reason),
    ).fetchone()


def open_episodes(con: sqlite3.Connection, service: str = None) -> list:
    if service:
        return con.execute(
            "SELECT * FROM notification_episodes"
            " WHERE service = ? AND state = 'DEGRADED'", (service,)
        ).fetchall()
    return con.execute(
        "SELECT * FROM notification_episodes WHERE state = 'DEGRADED'"
    ).fetchall()


def observe_degraded(con: sqlite3.Connection, service: str, reason: str,
                     detail=None, *, now=None) -> tuple:
    """Open or extend an episode. Returns (row, is_new_episode).

    An episode, not an event. Twelve scans of a service that is down for an
    hour extend one row; they do not create twelve (sections 35, 36).
    """
    ts = now or now_iso()
    existing = open_episode(con, service, reason)
    if existing is not None:
        con.execute(
            "UPDATE notification_episodes SET last_degraded_at = ?,"
            " observations = observations + 1, detail = ?"
            " WHERE episode_id = ?",
            (ts, (str(detail)[:300] if detail else existing["detail"]),
             existing["episode_id"]),
        )
        return get_episode(con, existing["episode_id"]), False
    eid = episode_id(service, reason, ts)
    con.execute(
        "INSERT INTO notification_episodes ("
        " episode_id, service, reason, state, first_degraded_at,"
        " last_degraded_at, observations, detail"
        ") VALUES (?,?,?,'DEGRADED',?,?,1,?)",
        (eid, service, reason, ts, ts,
         (str(detail)[:300] if detail else None)),
    )
    return get_episode(con, eid), True


def get_episode(con: sqlite3.Connection, eid: str):
    return con.execute(
        "SELECT * FROM notification_episodes WHERE episode_id = ?", (eid,)
    ).fetchone()


def mark_episode_notified(con: sqlite3.Connection, eid: str, *, now=None) -> None:
    con.execute(
        "UPDATE notification_episodes SET notified_at = ? WHERE episode_id = ?",
        (now or now_iso(), eid),
    )


def close_episodes(con: sqlite3.Connection, service: str,
                   keep_reasons=(), *, now=None) -> list:
    """Mark every open episode for this service recovered. Returns the rows.

    The caller only announces recovery for episodes that were announced when
    they broke: a transient blip that never reached the owner must not
    produce an "it's fixed!" message about something they never heard of
    (section 38).
    """
    ts = now or now_iso()
    rows = [r for r in open_episodes(con, service)
            if r["reason"] not in set(keep_reasons)]
    for r in rows:
        con.execute(
            "UPDATE notification_episodes SET state = 'RECOVERED',"
            " recovered_at = ? WHERE episode_id = ?", (ts, r["episode_id"])
        )
    return rows


def mark_recovery_notified(con: sqlite3.Connection, eid: str, *, now=None) -> None:
    con.execute(
        "UPDATE notification_episodes SET recovery_notified_at = ?"
        " WHERE episode_id = ?", (now or now_iso(), eid)
    )


# ---------------------------------------------------------- budget


class BudgetFull(Exception):
    """The reservation was refused. Never a reason to send anyway."""


def reserve_channel_budget(con: sqlite3.Connection, *, channel: str,
                           notification_id: str, severity: str, day: str,
                           month: str, allowance: int) -> tuple:
    """Take one unit of the day's allowance. Returns (ok, spent_today).

    Insert-then-count inside the caller's transaction. Counting first and
    inserting after leaves a window in which two notifications both see the
    same "one left" — which on a metered channel is a real charge, not a
    rounding error (section 82).

    A row that already exists for this (channel, notification) is the retry
    case and is allowed through without consuming a second unit.
    """
    now = now_iso()
    existing = con.execute(
        "SELECT * FROM channel_budget WHERE channel = ? AND notification_id = ?",
        (channel, notification_id),
    ).fetchone()
    if existing is not None:
        if existing["state"] == "RELEASED":
            con.execute(
                "UPDATE channel_budget SET state = 'RESERVED', updated_at = ?"
                " WHERE id = ?", (now, existing["id"])
            )
        return True, spent_today(con, channel, day)

    con.execute(
        "INSERT INTO channel_budget (channel, notification_id, severity, day,"
        " month, state, reserved_at, updated_at)"
        " VALUES (?,?,?,?,?,'RESERVED',?,?)",
        (channel, notification_id, severity, day, month, now, now),
    )
    used = spent_today(con, channel, day)
    if used > int(allowance):
        # Roll our own row back rather than the transaction: the caller may
        # have written a decision row it wants to keep, and the refusal is a
        # normal outcome rather than an error.
        con.execute(
            "DELETE FROM channel_budget WHERE channel = ? AND notification_id = ?",
            (channel, notification_id),
        )
        return False, used - 1
    return True, used


def spent_today(con: sqlite3.Connection, channel: str, day: str) -> int:
    return con.execute(
        "SELECT COUNT(*) FROM channel_budget WHERE channel = ? AND day = ?"
        "   AND state IN ('RESERVED','SPENT')", (channel, day)
    ).fetchone()[0]


def spent_this_month(con: sqlite3.Connection, channel: str, month: str) -> int:
    return con.execute(
        "SELECT COUNT(*) FROM channel_budget WHERE channel = ? AND month = ?"
        "   AND state IN ('RESERVED','SPENT')", (channel, month)
    ).fetchone()[0]


def settle_budget(con: sqlite3.Connection, *, channel: str,
                  notification_id: str, spent: bool) -> None:
    """Turn a reservation into a charge, or give it back."""
    con.execute(
        "UPDATE channel_budget SET state = ?, updated_at = ?"
        " WHERE channel = ? AND notification_id = ?",
        ("SPENT" if spent else "RELEASED", now_iso(), channel, notification_id),
    )


# ------------------------------------------------------ suppressions


def add_suppression(con: sqlite3.Connection, dedupe_key: str, until: str, *,
                    origin: str = "owner", note=None) -> None:
    con.execute(
        "INSERT INTO suppressions (dedupe_key, until, created_at, origin, note)"
        " VALUES (?,?,?,?,?)"
        " ON CONFLICT(dedupe_key) DO UPDATE SET until = excluded.until,"
        "   created_at = excluded.created_at, origin = excluded.origin,"
        "   note = excluded.note",
        (dedupe_key, until, now_iso(), origin,
         (str(note)[:200] if note else None)),
    )


def is_suppressed(con: sqlite3.Connection, dedupe_key: str, *, now=None) -> bool:
    row = con.execute(
        "SELECT until FROM suppressions WHERE dedupe_key = ?", (dedupe_key,)
    ).fetchone()
    return bool(row) and str(row["until"]) > (now or now_iso())


def purge_suppressions(con: sqlite3.Connection, *, now=None) -> int:
    return con.execute(
        "DELETE FROM suppressions WHERE until < ?", (now or now_iso(),)
    ).rowcount


# ---------------------------------------------------------- digests


def record_digest(con: sqlite3.Connection, *, digest_id: str, kind: str,
                  signal_ids, notification_id=None) -> None:
    con.execute(
        "INSERT OR REPLACE INTO digests (digest_id, kind, created_at,"
        " item_count, notification_id, signal_ids) VALUES (?,?,?,?,?,?)",
        (digest_id, kind, now_iso(), len(list(signal_ids)), notification_id,
         json.dumps(list(signal_ids), ensure_ascii=False)),
    )


def signals_awaiting_digest(con: sqlite3.Connection, limit: int = 50) -> list:
    """Signals whose last decision was DIGEST and that are not in one yet."""
    return con.execute(
        "SELECT s.* FROM signals s"
        " JOIN attention_decisions d ON d.signal_id = s.signal_id"
        " WHERE d.decision IN ('DIGEST','DEFER')"
        "   AND d.decision_id = (SELECT MAX(decision_id) FROM attention_decisions"
        "                        WHERE signal_id = s.signal_id)"
        "   AND NOT EXISTS (SELECT 1 FROM digests g"
        "                   WHERE g.signal_ids LIKE '%' || s.signal_id || '%')"
        " ORDER BY CASE s.severity WHEN 'CRITICAL' THEN 0 WHEN 'URGENT' THEN 1"
        "                          WHEN 'IMPORTANT' THEN 2 ELSE 3 END,"
        "          s.due_at IS NULL, s.due_at, s.observed_at"
        " LIMIT ?",
        (limit,),
    ).fetchall()


# ------------------------------------------------------- scan runs


def begin_scan(con: sqlite3.Connection) -> int:
    cur = con.execute(
        "INSERT INTO scan_runs (started_at) VALUES (?)", (now_iso(),)
    )
    return cur.lastrowid


def finish_scan(con: sqlite3.Connection, scan_id: int, *, ok: bool,
                counters: dict, duration_ms: int, error=None) -> None:
    con.execute(
        "UPDATE scan_runs SET finished_at = ?, ok = ?, signals_seen = ?,"
        " signals_new = ?, notify_now = ?, deferred = ?, digested = ?,"
        " suppressed = ?, delivered = ?, failed = ?, llm_calls = ?,"
        " duration_ms = ?, error = ? WHERE id = ?",
        (now_iso(), int(bool(ok)), counters.get("signals_seen", 0),
         counters.get("signals_new", 0), counters.get("notify_now", 0),
         counters.get("deferred", 0), counters.get("digested", 0),
         counters.get("suppressed", 0), counters.get("delivered", 0),
         counters.get("failed", 0), counters.get("llm_calls", 0),
         duration_ms, (str(error)[:400] if error else None), scan_id),
    )


def engine_health(con: sqlite3.Connection) -> dict:
    """The engine's own liveness (section 90).

    Reported, never self-alerted on: an attention engine that notices it is
    broken and tries to tell you about it through itself is the alert storm of
    section 91. This is for the status command and for whatever watches from
    outside.
    """
    def one(sql, params=()):
        row = con.execute(sql, params).fetchone()
        return row[0] if row else None

    return {
        "last_scan_at": one("SELECT MAX(started_at) FROM scan_runs"),
        "last_successful_scan_at": one(
            "SELECT MAX(started_at) FROM scan_runs WHERE ok = 1"),
        "last_error": one(
            "SELECT error FROM scan_runs WHERE error IS NOT NULL"
            " ORDER BY id DESC LIMIT 1"),
        "scans_total": one("SELECT COUNT(*) FROM scan_runs") or 0,
        "signal_backlog": one(
            "SELECT COUNT(*) FROM signals WHERE state = 'NEW'") or 0,
        "pending_notifications": one(
            "SELECT COUNT(*) FROM notifications"
            " WHERE state IN ('PENDING','DELIVERING')") or 0,
        "oldest_pending_at": one(
            "SELECT MIN(created_at) FROM notifications"
            " WHERE state IN ('PENDING','DELIVERING')"),
        "last_delivery_success_at": one(
            "SELECT MAX(delivered_at) FROM notifications"
            " WHERE state = 'DELIVERED'"),
        "llm_calls_total": one("SELECT COALESCE(SUM(llm_calls),0) FROM scan_runs") or 0,
    }


# ----------------------------------------------------- install state


def first_run_state(con: sqlite3.Connection) -> tuple:
    """(initialized_at, baseline_done). The watermark from section 25.

    Everything that already existed when the engine was installed is history.
    Without this the first scan announces every open deadline in the Event
    Layer, most of which passed days ago.
    """
    return (get_meta(con, "initialized_at"),
            get_meta(con, "baseline_done") == "1")


def mark_baseline_done(con: sqlite3.Connection) -> None:
    set_meta(con, "baseline_done", "1")


def initialized_before(row_created_at, initialized_at) -> bool:
    """Whether a thing predates the engine. Both may be None; be careful.

    A missing `initialized_at` means the meta row was lost, and the safe
    reading is "treat nothing as pre-existing" — that risks one extra
    notification, where the opposite risks silencing everything forever.
    """
    if not initialized_at:
        return False
    a, b = parse_iso(row_created_at), parse_iso(initialized_at)
    if a is None or b is None:
        return False
    return a < b
