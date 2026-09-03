#!/usr/bin/env python3
"""Unified Channel Gateway — durable queue operations.

Every state transition the gateway makes goes through here, so the properties
that matter are provable in one place:

* **A message is handled once.** `record_inbound` collapses on
  (platform, platform_message_id), so a LINE webhook redelivery or a Discord
  reconnect that replays the same message finds the row already there and
  does not queue a second run (sections 58, 59, 110).

* **An answer is sent once.** `reserve_outbound` is a unique insert on
  (gateway_request_id, chunk_index). A retry after a timeout re-reserves the
  same row and learns it already exists, instead of sending a second copy
  (sections 60, 116).

* **A conversation is answered in order.** `claim_next` refuses to hand out
  work for a conversation that already has a request in flight, and picks the
  oldest waiting message within a conversation — so B cannot overtake A
  (sections 61, 119).

* **A crash loses nothing.** PROCESSING is a claim with a timestamp, not a
  commitment. `recover_stale` returns claims older than a lease back to
  QUEUED on startup and periodically (section 118).

Message text is stored because a queue that cannot replay is not durable.
`purge_expired` bounds how long it stays (section 87).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from gw_canonical import (
    SUBJECT_OWNER,
    CanonicalMessage,
    new_outbound_id,
    now_utc_iso,
)
from gw_schema import TERMINAL_INBOUND

# A PROCESSING row older than this is assumed to belong to a worker that died.
PROCESSING_LEASE_SECONDS = 300


def _now() -> str:
    return now_utc_iso()


def _iso_minus(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


# ------------------------------------------------------------- inbound


def record_inbound(con: sqlite3.Connection, message: CanonicalMessage) -> tuple:
    """Insert a newly received message. Returns (request_id, is_new).

    is_new is False when this exact platform message has been seen before,
    whatever state it reached. The caller must not enqueue work in that case:
    that is the whole of duplicate suppression.
    """
    now = _now()
    state = "AUTHENTICATED" if message.authz_subject == SUBJECT_OWNER else "RECEIVED"
    try:
        con.execute(
            "INSERT INTO inbound_messages ("
            "  gateway_request_id, platform, platform_message_id, platform_event_id,"
            "  platform_user_id, conversation_id, received_at, platform_timestamp,"
            "  text, reply_token, authz_subject, capability_profile, state,"
            "  attempts, updated_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)",
            (
                message.gateway_request_id,
                message.platform,
                message.platform_message_id,
                message.platform_event_id,
                message.platform_user_id,
                message.conversation_id,
                message.received_at,
                message.timestamp,
                message.text,
                message.reply_token,
                message.authz_subject,
                message.capability_profile,
                state,
                now,
            ),
        )
    except sqlite3.IntegrityError:
        return message.gateway_request_id, False
    return message.gateway_request_id, True


def get_inbound(con: sqlite3.Connection, request_id: str):
    return con.execute(
        "SELECT * FROM inbound_messages WHERE gateway_request_id = ?", (request_id,)
    ).fetchone()


def set_state(con: sqlite3.Connection, request_id: str, state: str, *,
              error=None, run_id=None) -> None:
    now = _now()
    fields = {"state": state, "updated_at": now}
    if error is not None:
        fields["last_error"] = str(error)[:200]
    if run_id is not None:
        fields["hermes_run_id"] = str(run_id)
    if state in TERMINAL_INBOUND:
        fields["completed_at"] = now
        fields["claimed_at"] = None
    cols = ", ".join(f"{k} = ?" for k in fields)
    con.execute(
        f"UPDATE inbound_messages SET {cols} WHERE gateway_request_id = ?",
        list(fields.values()) + [request_id],
    )


def enqueue(con: sqlite3.Connection, request_id: str) -> None:
    set_state(con, request_id, "QUEUED")


def claim_next(con: sqlite3.Connection, *, max_active: int) -> sqlite3.Row | None:
    """Take the next message that may be worked on, or None.

    Two bounds apply at once: at most `max_active` requests in flight across
    the whole gateway, and at most one per conversation. The second is what
    keeps a conversation's answers in order — a second message from the same
    chat waits rather than racing the first (sections 61, 62, 119).
    """
    active = con.execute(
        "SELECT COUNT(*) FROM inbound_messages WHERE state = 'PROCESSING'"
    ).fetchone()[0]
    if active >= max_active:
        return None

    row = con.execute(
        "SELECT * FROM inbound_messages"
        " WHERE state = 'QUEUED'"
        "   AND conversation_id NOT IN ("
        "     SELECT conversation_id FROM inbound_messages WHERE state = 'PROCESSING'"
        "   )"
        " ORDER BY received_at, rowid LIMIT 1"
    ).fetchone()
    if row is None:
        return None

    now = _now()
    con.execute(
        "UPDATE inbound_messages SET state='PROCESSING', claimed_at=?, "
        "attempts = attempts + 1, updated_at=? WHERE gateway_request_id=? "
        "AND state='QUEUED'",
        (now, now, row["gateway_request_id"]),
    )
    if con.total_changes == 0:
        return None
    con.execute(
        "INSERT INTO conversation_state (conversation_id, platform, active_request_id,"
        " active_since, last_message_at, message_count, updated_at)"
        " VALUES (?,?,?,?,?,1,?)"
        " ON CONFLICT(conversation_id) DO UPDATE SET"
        "   active_request_id = excluded.active_request_id,"
        "   active_since = excluded.active_since,"
        "   last_message_at = excluded.last_message_at,"
        "   message_count = conversation_state.message_count + 1,"
        "   updated_at = excluded.updated_at",
        (row["conversation_id"], row["platform"], row["gateway_request_id"],
         now, row["received_at"], now),
    )
    return get_inbound(con, row["gateway_request_id"])


def release_conversation(con: sqlite3.Connection, conversation_id: str) -> None:
    con.execute(
        "UPDATE conversation_state SET active_request_id = NULL, active_since = NULL,"
        " updated_at = ? WHERE conversation_id = ?",
        (_now(), conversation_id),
    )


def recover_stale(con: sqlite3.Connection, *, lease_seconds: int = PROCESSING_LEASE_SECONDS,
                  max_attempts: int = 3) -> dict:
    """Return abandoned claims to the queue (section 118).

    A worker that died mid-request left the row PROCESSING. Returning it to
    QUEUED is safe because the expensive half — the Hermes call — is
    idempotent on the request id, and the send half is guarded by the unique
    outbound reservation. A row that has burned its attempts becomes FAILED
    rather than looping.
    """
    cutoff = _iso_minus(lease_seconds)
    rows = con.execute(
        "SELECT gateway_request_id, attempts, conversation_id FROM inbound_messages"
        " WHERE state = 'PROCESSING' AND (claimed_at IS NULL OR claimed_at < ?)",
        (cutoff,),
    ).fetchall()
    requeued, failed = 0, 0
    for r in rows:
        if int(r["attempts"] or 0) >= max_attempts:
            set_state(con, r["gateway_request_id"], "FAILED", error="max_attempts")
            failed += 1
        else:
            set_state(con, r["gateway_request_id"], "QUEUED", error="reclaimed")
            requeued += 1
        release_conversation(con, r["conversation_id"])
    return {"requeued": requeued, "failed": failed}


# ------------------------------------------------------------ outbound


def reserve_outbound(con: sqlite3.Connection, *, request_id: str, platform: str,
                     conversation_id: str, target_user_id: str, body: str,
                     chunk_index: int, chunk_total: int, delivery_mode: str) -> tuple:
    """Reserve the right to send one chunk. Returns (outbound_id, is_new).

    is_new False means this chunk was already reserved — by this worker
    before a crash, or by a retry. The caller must not send again unless the
    existing row is still PENDING.
    """
    oid = new_outbound_id(request_id, chunk_index)
    now = _now()
    try:
        con.execute(
            "INSERT INTO outbound_messages ("
            " outbound_id, gateway_request_id, platform, conversation_id,"
            " target_user_id, body, chunk_index, chunk_total, delivery_mode,"
            " state, attempts, created_at, updated_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,'PENDING',0,?,?)",
            (oid, request_id, platform, conversation_id, target_user_id, body,
             chunk_index, chunk_total, delivery_mode, now, now),
        )
    except sqlite3.IntegrityError:
        return oid, False
    return oid, True


def get_outbound(con: sqlite3.Connection, outbound_id: str):
    return con.execute(
        "SELECT * FROM outbound_messages WHERE outbound_id = ?", (outbound_id,)
    ).fetchone()


def outbound_for_request(con: sqlite3.Connection, request_id: str) -> list:
    return con.execute(
        "SELECT * FROM outbound_messages WHERE gateway_request_id = ?"
        " ORDER BY chunk_index",
        (request_id,),
    ).fetchall()


def mark_outbound(con: sqlite3.Connection, outbound_id: str, state: str, *,
                  error=None, mode=None) -> None:
    now = _now()
    fields = {"state": state, "updated_at": now}
    if state == "SENT":
        fields["sent_at"] = now
    if error is not None:
        fields["last_error"] = str(error)[:200]
    if mode is not None:
        fields["delivery_mode"] = mode
    cols = ", ".join(f"{k} = ?" for k in fields)
    con.execute(
        f"UPDATE outbound_messages SET {cols} WHERE outbound_id = ?",
        list(fields.values()) + [outbound_id],
    )


def record_attempt(con: sqlite3.Connection, outbound_id: str, *, mode: str,
                   outcome: str, status_code=None, error_category=None) -> None:
    con.execute(
        "UPDATE outbound_messages SET attempts = attempts + 1, updated_at = ?"
        " WHERE outbound_id = ?",
        (_now(), outbound_id),
    )
    con.execute(
        "INSERT INTO delivery_attempts (outbound_id, attempted_at, mode, outcome,"
        " status_code, error_category) VALUES (?,?,?,?,?,?)",
        (outbound_id, _now(), mode, outcome, status_code,
         str(error_category)[:100] if error_category else None),
    )


def already_delivered(con: sqlite3.Connection, request_id: str) -> bool:
    """True when every reserved chunk for this request has been sent."""
    rows = outbound_for_request(con, request_id)
    return bool(rows) and all(r["state"] == "SENT" for r in rows)


# ------------------------------------------------------- conversation


def session_id_for(con: sqlite3.Connection, conversation_id: str, platform: str) -> str:
    """The Hermes session this conversation uses.

    One per channel conversation, never shared between platforms: a thread
    the owner left half-finished on LINE should not surface in a Discord DM
    (sections 129, 130).
    """
    row = con.execute(
        "SELECT hermes_session_id FROM conversation_state WHERE conversation_id = ?",
        (conversation_id,),
    ).fetchone()
    if row and row["hermes_session_id"]:
        return row["hermes_session_id"]
    session_id = f"gw-{conversation_id.replace(':', '-')}"
    now = _now()
    con.execute(
        "INSERT INTO conversation_state (conversation_id, platform, hermes_session_id,"
        " message_count, updated_at) VALUES (?,?,?,0,?)"
        " ON CONFLICT(conversation_id) DO UPDATE SET"
        "   hermes_session_id = COALESCE(conversation_state.hermes_session_id,"
        "                                excluded.hermes_session_id),"
        "   updated_at = excluded.updated_at",
        (conversation_id, platform, session_id, now),
    )
    row = con.execute(
        "SELECT hermes_session_id FROM conversation_state WHERE conversation_id = ?",
        (conversation_id,),
    ).fetchone()
    return row["hermes_session_id"]


# ------------------------------------------------------------- audit


def audit(con: sqlite3.Connection, event: str, *, request_id=None, platform=None,
          authenticated=None, capability_profile=None, detail=None) -> None:
    """Record what happened. Categories and ids only — never a message body.

    `detail` is bounded and is expected to hold a category, a status code or a
    count. Anything that might carry user text belongs in the inbound row,
    which retention expires; the audit trail outlives it (section 86).
    """
    con.execute(
        "INSERT INTO audit_events (at, gateway_request_id, platform, event,"
        " authenticated, capability_profile, detail) VALUES (?,?,?,?,?,?,?)",
        (_now(), request_id, platform, event,
         None if authenticated is None else int(bool(authenticated)),
         capability_profile, (str(detail)[:200] if detail is not None else None)),
    )


def bump(con: sqlite3.Connection, key: str, delta: int = 1) -> None:
    con.execute(
        "INSERT INTO metrics (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET"
        "   value = CAST(CAST(metrics.value AS INTEGER) + ? AS TEXT)",
        (key, str(delta), delta),
    )


def metrics(con: sqlite3.Connection) -> dict:
    return {r[0]: r[1] for r in con.execute("SELECT key, value FROM metrics")}


# --------------------------------------------------------- retention


def purge_expired(con: sqlite3.Connection, *, retention_days: int) -> dict:
    """Drop message bodies past their retention window (section 87).

    The row survives so duplicate suppression keeps working — a webhook
    redelivered a month later must still be recognised — but the text, the
    reply token and the outbound bodies are cleared. Personal memory lives in
    Hermes; the gateway is a pipe with a short memory of its own.
    """
    cutoff = _iso_minus(retention_days * 86400)
    inbound = con.execute(
        "UPDATE inbound_messages SET text = NULL, reply_token = NULL, updated_at = ?"
        " WHERE received_at < ? AND text IS NOT NULL",
        (_now(), cutoff),
    ).rowcount
    outbound = con.execute(
        "UPDATE outbound_messages SET body = '' , updated_at = ?"
        " WHERE created_at < ? AND body <> ''",
        (_now(), cutoff),
    ).rowcount
    attempts = con.execute(
        "DELETE FROM delivery_attempts WHERE attempted_at < ?", (cutoff,)
    ).rowcount
    audits = con.execute(
        "DELETE FROM audit_events WHERE at < ?",
        (_iso_minus(max(retention_days, 90) * 86400),),
    ).rowcount
    return {
        "inbound_cleared": inbound,
        "outbound_cleared": outbound,
        "attempts_deleted": attempts,
        "audits_deleted": audits,
    }


def queue_depth(con: sqlite3.Connection) -> dict:
    rows = dict(
        con.execute("SELECT state, COUNT(*) FROM inbound_messages GROUP BY state")
    )
    rows["outbound_pending"] = con.execute(
        "SELECT COUNT(*) FROM outbound_messages WHERE state IN ('PENDING','SENDING')"
    ).fetchone()[0]
    return rows


def status_snapshot(con: sqlite3.Connection) -> dict:
    depth = queue_depth(con)
    return {
        "queue": depth,
        "backlogged": depth.get("QUEUED", 0) + depth.get("outbound_pending", 0) > 50,
        "metrics": metrics(con),
        "conversations": con.execute(
            "SELECT COUNT(*) FROM conversation_state"
        ).fetchone()[0],
        "generated_at": _now(),
    }


def json_dump(value) -> str:
    return json.dumps(value, ensure_ascii=False)
