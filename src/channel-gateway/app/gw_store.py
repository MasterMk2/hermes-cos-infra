#!/usr/bin/env python3
"""Unified Channel Gateway — durable queue operations.

Every state transition the gateway makes goes through here, so the properties
that matter are provable in one place:

* **A message is handled once.** `record_inbound` collapses on
  (platform, platform_message_id), so a LINE webhook redelivery or a Discord
  reconnect that replays the same message finds the row already there and
  does not queue a second run (sections 58, 59, 110).

* **Discord replies have write-ahead, per-chunk receipts.** A confirmed
  prefix is never replayed; an unknown chunk holds the whole reply. Unique
  reservations alone cannot make an external send and SQLite commit atomic.
  LINE's ordinary reply/push path still has separate unresolved boundaries.

* **A conversation is answered in order.** `claim_next` permits one active
  request per conversation. A held unknown reply releases that lock so future
  independent messages are not blocked forever; it never resumes its tail.

* **Process restarts preserve intent.** `recover_stale` requeues only safe
  work. A stale Discord SENDING becomes UNKNOWN, not permission to resend.

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

    Discord replies resume only a confirmed prefix plus a proven-unsent tail.
    SENDING and legacy ambiguous rows become UNKNOWN, never retry authority.
    All confirmed chunks settle the request without regenerating or sending.
    LINE batches follow the same hold rule, but never resume a partial batch
    or change a started reply into a push. A request without uncertain
    delivery is bounded by max_attempts.
    """
    # Select and recover under the same write lock as chunk claims. Otherwise
    # a live caller can refresh the lease between selection and invalidation.
    if not con.in_transaction:
        con.execute("BEGIN IMMEDIATE")
    else:
        # Store callers may already own a transaction. Acquire its write lock
        # before reading stale rows (also safe for an existing write txn).
        con.execute("UPDATE inbound_messages SET updated_at=updated_at WHERE 0")
    cutoff = _iso_minus(lease_seconds)
    rows = con.execute(
        "SELECT gateway_request_id, attempts, conversation_id, platform FROM inbound_messages"
        " WHERE state = 'PROCESSING' AND (claimed_at IS NULL OR claimed_at < ?)",
        (cutoff,),
    ).fetchall()
    requeued, failed, reply_unknown, reply_confirmed = 0, 0, 0, 0
    for r in rows:
        if r["platform"] in {"discord", "line"} and outbound_for_request(con, r["gateway_request_id"]):
            con.execute(
                "UPDATE outbound_messages SET state='UNKNOWN', updated_at=?,"
                " last_error='delivery_outcome_unknown'"
                " WHERE gateway_request_id=? AND state='SENDING'",
                (_now(), r["gateway_request_id"]))
            status_fn = _discord_reply_status if r["platform"] == "discord" else line_reply_status
            status, _ = status_fn(con, r["gateway_request_id"])
            if status == "already_delivered":
                set_state(con, r["gateway_request_id"], "RESPONDED")
                release_conversation(con, r["conversation_id"])
                reply_confirmed += 1
                continue
            if status != "ready":
                # Unknown is held even when the request exhausted its budget.
                reply_unknown += 1
                continue
        if int(r["attempts"] or 0) >= max_attempts:
            set_state(con, r["gateway_request_id"], "FAILED", error="max_attempts")
            failed += 1
        else:
            set_state(con, r["gateway_request_id"], "QUEUED", error="reclaimed")
            requeued += 1
        release_conversation(con, r["conversation_id"])
    return {"requeued": requeued, "failed": failed,
            "reply_unknown": reply_unknown, "reply_confirmed": reply_confirmed}


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


# ------------------------------------------ ordinary Discord reply ledger


def hold_reply(con, request_id: str, reason: str) -> None:
    row = get_inbound(con, request_id)
    set_state(con, request_id, "UNKNOWN", error=reason)
    if row is not None:
        con.execute("UPDATE conversation_state SET active_request_id=NULL, "
                    "active_since=NULL, updated_at=? WHERE conversation_id=? "
                    "AND active_request_id=?", (_now(), row["conversation_id"], request_id))


def _discord_reply_status(con, request_id: str) -> tuple:
    """Evaluate the ENTIRE reply. An unknown prefix blocks its unsent tail.

    The caller holds a write transaction. Historical PENDING/FAILED did not
    write SENDING before I/O: even attempts=0 may have reached Discord.
    """
    rows = outbound_for_request(con, request_id)
    if not rows:
        return "empty_answer", rows
    for r in rows:
        if r["state"] not in {"SENT", "NOT_SENT", "SENDING", "UNKNOWN"}:
            mark_outbound(con, r["outbound_id"], "UNKNOWN",
                          error="legacy_delivery_outcome_unknown")
    rows = outbound_for_request(con, request_id)
    if any(r["state"] == "UNKNOWN" for r in rows):
        hold_reply(con, request_id, "delivery_outcome_unknown")
        return "delivery_outcome_unknown", rows
    # Do not turn another live caller's claim into a retry or release its lock.
    if any(r["state"] == "SENDING" for r in rows):
        return "in_flight", rows
    tail = False
    for i, r in enumerate(rows):
        if r["chunk_index"] != i or r["chunk_total"] != len(rows):
            hold_reply(con, request_id, "reply_shape_mismatch")
            return "reply_shape_mismatch", rows
        if r["state"] == "NOT_SENT":
            if not r["body"]:
                hold_reply(con, request_id, "reply_body_expired")
                return "reply_body_expired", rows
            tail = True
        elif tail:  # a confirmed chunk after an unconfirmed one is not a prefix
            hold_reply(con, request_id, "reply_order_mismatch")
            return "reply_order_mismatch", rows
    return ("ready" if tail else "already_delivered"), rows


def prepare_discord_reply(con, *, request_id: str, conversation_id: str,
                          owner_id: str, chunks: list | None) -> tuple:
    """Freeze the whole reply once, or validate its original authorization.

    None means resume the stored answer without regenerating it. NOT_SENT is
    committed in the SAME transaction as a new reservation, so an old
    PENDING row never masquerades as proof that no adapter was called.
    """
    con.execute("BEGIN IMMEDIATE")
    inbound = get_inbound(con, request_id)
    if inbound is None:
        return "reply_scope_mismatch", []
    if inbound["state"] == "UNKNOWN":
        return "delivery_outcome_unknown", outbound_for_request(con, request_id)
    if inbound["state"] == "CANCELLED":
        return "reply_cancelled", []
    if (not owner_id or inbound["authz_subject"] != SUBJECT_OWNER
            or inbound["platform"] != "discord"
            or inbound["platform_user_id"] != owner_id
            or inbound["conversation_id"] != conversation_id
            or not conversation_id.startswith("discord:")):
        hold_reply(con, request_id, "reply_scope_mismatch")
        return "reply_scope_mismatch", []
    rows = outbound_for_request(con, request_id)
    if not rows:
        if not chunks:
            return "empty_answer", []
        for i, body in enumerate(chunks):
            oid, _ = reserve_outbound(
                con, request_id=request_id, platform="discord",
                conversation_id=conversation_id, target_user_id=owner_id,
                body=body, chunk_index=i, chunk_total=len(chunks), delivery_mode="dm")
            mark_outbound(con, oid, "NOT_SENT", mode="dm")
        rows = outbound_for_request(con, request_id)
    if any(r["platform"] != "discord" or r["conversation_id"] != conversation_id
           or r["target_user_id"] != owner_id or r["delivery_mode"] != "dm"
           for r in rows):
        hold_reply(con, request_id, "reply_scope_mismatch")
        return "reply_scope_mismatch", rows
    if chunks is not None and [r["body"] for r in rows] != chunks:
        hold_reply(con, request_id, "content_revision_mismatch")
        return "content_revision_mismatch", rows
    return _discord_reply_status(con, request_id)


def claim_discord_chunk(con, request_id: str) -> tuple:
    """Commit this claim before adapter I/O. Only the first unsent tail may run.

    BEGIN IMMEDIATE serializes separate connections, including two processes.
    A replay or concurrent caller never receives the same chunk to send.
    """
    con.execute("BEGIN IMMEDIATE")
    inbound = get_inbound(con, request_id)
    if inbound is None or inbound["state"] == "UNKNOWN":
        return "delivery_outcome_unknown", None
    if inbound["state"] == "CANCELLED":
        return "reply_cancelled", None
    status, rows = _discord_reply_status(con, request_id)
    if status != "ready":
        return status, None
    row = next(r for r in rows if r["state"] == "NOT_SENT")
    mark_outbound(con, row["outbound_id"], "SENDING", mode="dm")
    con.execute("UPDATE inbound_messages SET claimed_at=?, updated_at=? "
                "WHERE gateway_request_id=? AND state='PROCESSING'",
                (_now(), _now(), request_id))
    return "send", row


def record_discord_receipt(con, outbound_id: str, *, state: str, error=None) -> bool:
    """Receipt + attempt are one transaction; a recovered hold is never reset."""
    con.execute("BEGIN IMMEDIATE")
    row = get_outbound(con, outbound_id)
    if row is None or row["state"] != "SENDING":
        return False
    if state not in {"SENT", "NOT_SENT", "UNKNOWN"}:
        raise ValueError("invalid Discord receipt")
    mark_outbound(con, outbound_id, state, error=error, mode="dm")
    if state == "SENT":
        con.execute("UPDATE outbound_messages SET last_error=NULL WHERE outbound_id=?",
                    (outbound_id,))
    record_attempt(con, outbound_id, mode="dm",
                   outcome="ok" if state == "SENT" else state.lower(),
                   error_category=error)
    if state == "UNKNOWN":
        hold_reply(con, row["gateway_request_id"], "delivery_outcome_unknown")
    return True


# ---------------------------------------------------- ordinary LINE batch


def line_reply_status(con, request_id: str) -> tuple:
    rows = outbound_for_request(con, request_id)
    if not rows:
        return "empty_answer", rows
    for row in rows:
        if row["state"] not in {"SENT", "NOT_SENT", "SENDING", "UNKNOWN"}:
            mark_outbound(con, row["outbound_id"], "UNKNOWN", error="legacy_delivery_outcome_unknown")
    rows = outbound_for_request(con, request_id)
    if any(row["state"] == "UNKNOWN" for row in rows):
        hold_reply(con, request_id, "delivery_outcome_unknown")
        return "delivery_outcome_unknown", rows
    if any(row["state"] == "SENDING" for row in rows):
        return "in_flight", rows
    valid = (1 <= len(rows) <= 5
             and all(row["chunk_index"] == i and row["chunk_total"] == len(rows)
                     for i, row in enumerate(rows))
             and len({row["delivery_mode"] for row in rows}) == 1
             and rows[0]["delivery_mode"] in {"reply", "push"})
    if not valid or len({row["state"] for row in rows}) != 1:
        hold_reply(con, request_id, "line_batch_mismatch")
        return "line_batch_mismatch", rows
    if all(row["state"] == "SENT" for row in rows):
        return "already_delivered", rows
    from gw_line import build_text_messages
    try:
        build_text_messages([row["body"] for row in rows])
    except ValueError:
        hold_reply(con, request_id, "line_batch_invalid_or_expired")
        return "line_batch_invalid_or_expired", rows
    return "ready", rows


def prepare_line_reply(con, *, request_id, conversation_id, owner_id, chunks, mode):
    """Freeze the exact batch and method; only fresh, proven-unsent work starts."""
    con.execute("BEGIN IMMEDIATE")
    inbound = get_inbound(con, request_id)
    if inbound is None or inbound["state"] == "UNKNOWN":
        return "delivery_outcome_unknown", []
    if inbound["state"] == "CANCELLED":
        return "reply_cancelled", []
    if (not owner_id or inbound["authz_subject"] != SUBJECT_OWNER
            or inbound["platform"] != "line" or inbound["platform_user_id"] != owner_id
            or inbound["conversation_id"] != conversation_id
            or conversation_id != "line:" + owner_id):
        hold_reply(con, request_id, "reply_scope_mismatch")
        return "reply_scope_mismatch", []
    rows = outbound_for_request(con, request_id)
    if not rows:
        if not chunks:
            return "empty_answer", []
        from gw_line import build_text_messages
        build_text_messages(chunks)
        if mode not in {"reply", "push"}:
            raise ValueError("invalid LINE method")
        for i, body in enumerate(chunks):
            oid, _ = reserve_outbound(con, request_id=request_id, platform="line",
                conversation_id=conversation_id, target_user_id=owner_id,
                body=body, chunk_index=i, chunk_total=len(chunks), delivery_mode=mode)
            mark_outbound(con, oid, "NOT_SENT", mode=mode)
        rows = outbound_for_request(con, request_id)
    if any(row["platform"] != "line" or row["target_user_id"] != owner_id
           or row["conversation_id"] != conversation_id for row in rows):
        hold_reply(con, request_id, "reply_scope_mismatch")
        return "reply_scope_mismatch", rows
    if chunks is not None and chunks != [row["body"] for row in rows]:
        hold_reply(con, request_id, "content_revision_mismatch")
        return "content_revision_mismatch", rows
    return line_reply_status(con, request_id)


def claim_line_batch(con, request_id):
    con.execute("BEGIN IMMEDIATE")
    inbound = get_inbound(con, request_id)
    if inbound is None or inbound["state"] == "UNKNOWN":
        return "delivery_outcome_unknown", None
    if inbound["state"] == "CANCELLED":
        return "reply_cancelled", None
    status, rows = line_reply_status(con, request_id)
    if status != "ready":
        return status, None
    first_attempt = con.execute("SELECT MIN(attempted_at) FROM delivery_attempts "
        "WHERE outbound_id=? AND outcome='started'", (rows[0]["outbound_id"],)).fetchone()[0]
    started = first_attempt or _now()
    for row in rows:
        mark_outbound(con, row["outbound_id"], "SENDING")
    # The first transport-attempt boundary is durable, not regenerated on retry.
    record_attempt(con, rows[0]["outbound_id"], mode=rows[0]["delivery_mode"], outcome="started")
    con.execute("UPDATE inbound_messages SET claimed_at=?, updated_at=? "
                "WHERE gateway_request_id=? AND state='PROCESSING'", (_now(), _now(), request_id))
    return "send", {"rows": rows, "reply_token": inbound["reply_token"], "started_at": started}


def record_line_receipt(con, request_id, *, ok, status_code, category, not_sent=False):
    con.execute("BEGIN IMMEDIATE")
    rows = outbound_for_request(con, request_id)
    if not rows or any(row["state"] != "SENDING" for row in rows):
        return False
    # Only the supported endpoint's positive receipt can certify acceptance.
    ok = bool(ok) and (status_code == 200 or (status_code == 409
        and rows[0]["delivery_mode"] == "push" and category == "already_sent"))
    # Only explicit local preflight outcomes, never a provider's final status.
    not_sent = bool(not_sent) and category in {"invalid_batch", "no_reply_token", "no_owner"}
    state = "SENT" if ok else "NOT_SENT" if not_sent else "UNKNOWN"
    for row in rows:
        mark_outbound(con, row["outbound_id"], state,
                      error=None if ok else category if not_sent else "delivery_outcome_unknown")
    con.execute("INSERT INTO delivery_attempts (outbound_id, attempted_at, mode, outcome,"
                "status_code, error_category) VALUES (?,?,?,?,?,?)",
                (rows[0]["outbound_id"], _now(), rows[0]["delivery_mode"],
                 "ok" if ok else "not_sent" if not_sent else "unknown", status_code, category))
    if not ok and not not_sent:
        hold_reply(con, request_id, "delivery_outcome_unknown")
    return state


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


# =========================================================== G.2 ==========
#
# Everything below serves the Attention Engine. None of it is reachable from
# a channel: the routes that call it listen on a unix socket the platforms
# cannot see, and none of it takes a recipient.


def stamp(con: sqlite3.Connection, key: str, when: str | None = None) -> None:
    """Record 'this happened, most recently, at'.

    Distinct from `bump`: a counter says how often, a stamp says how long ago,
    and the difference is the whole of section 41. A gateway with zero
    messages today is idle; a gateway whose last SUCCESSFUL Hermes answer was
    three hours ago while messages kept arriving is broken. Only the second
    is worth waking someone for, and telling them apart needs both figures.
    """
    con.execute(
        "INSERT INTO metrics (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, when or _now()),
    )


def liveness(con: sqlite3.Connection) -> dict:
    """The end-to-end facts section 40 asks for.

    Most are computed from the tables rather than kept as counters, so they
    cannot drift out of step with reality — the exception is
    `last_hermes_success_at`, which has no row of its own to be read off.
    A RESPONDED inbound row proves only that SOMETHING was delivered, and
    "いま応答できません" is something: the G.1 failure this exists to catch was
    a gateway that answered every message with an error and looked, by every
    queue metric, perfectly healthy.
    """
    def one(sql, params=()):
        row = con.execute(sql, params).fetchone()
        return row[0] if row else None

    m = metrics(con)
    oldest_queued = one(
        "SELECT MIN(received_at) FROM inbound_messages WHERE state = 'QUEUED'"
    )
    oldest_processing = one(
        "SELECT MIN(claimed_at) FROM inbound_messages WHERE state = 'PROCESSING'"
    )
    return {
        "now": _now(),
        "last_inbound_received_at": one(
            "SELECT MAX(received_at) FROM inbound_messages"
        ),
        "last_inbound_processed_at": one(
            "SELECT MAX(completed_at) FROM inbound_messages"
            " WHERE state = 'RESPONDED'"
        ),
        "last_hermes_success_at": m.get("last_hermes_success_at"),
        "last_hermes_failure_at": m.get("last_hermes_failure_at"),
        "last_outbound_success_at": one(
            "SELECT MAX(sent_at) FROM outbound_messages WHERE state = 'SENT'"
        ),
        "last_proactive_success_at": one(
            "SELECT MAX(sent_at) FROM proactive_deliveries WHERE state = 'SENT'"
        ),
        "oldest_queued_at": oldest_queued,
        "oldest_processing_at": oldest_processing,
        "queue_depth": one(
            "SELECT COUNT(*) FROM inbound_messages WHERE state = 'QUEUED'"
        ) or 0,
        "processing_depth": one(
            "SELECT COUNT(*) FROM inbound_messages WHERE state = 'PROCESSING'"
        ) or 0,
        "outbound_pending": one(
            "SELECT COUNT(*) FROM outbound_messages"
            " WHERE state IN ('PENDING','SENDING','NOT_SENT')"
        ) or 0,
        "reply_unknown": one(
            "SELECT COUNT(*) FROM inbound_messages WHERE state = 'UNKNOWN'"
        ) or 0,
        "proactive_pending": one(
            "SELECT COUNT(*) FROM proactive_deliveries"
            " WHERE state IN ('PENDING','SENDING')"
        ) or 0,
        "proactive_unknown": one(
            "SELECT COUNT(*) FROM proactive_deliveries WHERE state = 'UNKNOWN'"
        ) or 0,
        "processing_failure_count": int(m.get("failed") or 0),
        "hermes_failure_count": int(m.get("hermes_failed") or 0),
        "delivery_failure_count": one(
            "SELECT COUNT(*) FROM delivery_attempts WHERE outcome NOT IN ('ok','started')"
        ) or 0,
        "responded_count": int(m.get("responded") or 0),
    }


# ------------------------------------------------------ proactive send


def proactive_delivery_id(notification_id: str, channel: str) -> str:
    """Stable, derived, and the reason a crash cannot charge twice.

    Same construction as `new_outbound_id`: the identity of a delivery is a
    function of what it is, not of when it was attempted, so a retry after a
    crash computes the identifier the earlier attempt already inserted.
    """
    import hashlib

    digest = hashlib.sha256(
        f"{notification_id}:{channel}".encode("utf-8")
    ).hexdigest()
    return f"pn_{digest[:24]}"


def content_revision(text: str) -> str:
    import hashlib

    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:32]


def reserve_proactive(con: sqlite3.Connection, *, notification_id: str,
                      channel: str, severity: str, dedupe_key, reason,
                      body: str, target_user_id: str) -> tuple:
    """Claim the right to deliver one notification on one channel.

    Returns (row, is_new). `is_new` False means this exact (notification,
    channel) already exists — either delivered, in flight, or failed. The
    caller decides what that means; what it must never do is send anyway.
    """
    did = proactive_delivery_id(notification_id, channel)
    rev = content_revision(body)
    now = _now()
    try:
        con.execute(
            "INSERT INTO proactive_deliveries ("
            " delivery_id, notification_id, channel, severity, dedupe_key,"
            " reason, content_revision, body, target_user_id, state, attempts,"
            " quota_consumed, created_at, updated_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,'PENDING',0,0,?,?)",
            (did, notification_id, channel, severity,
             (str(dedupe_key)[:200] if dedupe_key else None),
             (str(reason)[:200] if reason else None),
             rev, body, target_user_id, now, now),
        )
    except sqlite3.IntegrityError:
        return get_proactive(con, did), False
    return get_proactive(con, did), True


def get_proactive(con: sqlite3.Connection, delivery_id: str):
    return con.execute(
        "SELECT * FROM proactive_deliveries WHERE delivery_id = ?",
        (delivery_id,),
    ).fetchone()


def mark_proactive(con: sqlite3.Connection, delivery_id: str, state: str, *,
                   error=None, mode=None, quota_consumed=None,
                   count_attempt: bool = False) -> None:
    now = _now()
    fields = {"state": state, "updated_at": now}
    if state == "SENT":
        fields["sent_at"] = now
        fields["last_error"] = None
    if error is not None:
        fields["last_error"] = str(error)[:200]
    if mode is not None:
        fields["delivery_mode"] = mode
    if quota_consumed is not None:
        fields["quota_consumed"] = int(bool(quota_consumed))
    cols = ", ".join(f"{k} = ?" for k in fields)
    extra = ", attempts = attempts + 1" if count_attempt else ""
    con.execute(
        f"UPDATE proactive_deliveries SET {cols}{extra} WHERE delivery_id = ?",
        list(fields.values()) + [delivery_id],
    )


def recover_stuck_proactive(con: sqlite3.Connection, *,
                            lease_seconds: int = 180,
                            max_attempts: int = 3) -> dict:
    """Hold abandoned sends without guessing whether the provider accepted.

    A lease expiring is not evidence of non-delivery. Both channels become
    UNKNOWN and a subsequent claim is refused. Even a provider retry key
    cannot be assumed valid after an arbitrarily long outage. Keep the old
    max_attempts argument/result counters for callers, but never turn an
    uncertain send into a retryable PENDING/FAILED row based on an attempt
    count. A still-running adapter may later record its actual result.
    """
    cutoff = _iso_minus(lease_seconds)
    cur = con.execute(
        "UPDATE proactive_deliveries SET state = 'UNKNOWN', updated_at = ?,"
        " last_error = 'delivery_outcome_unknown'"
        " WHERE state = 'SENDING' AND (updated_at IS NULL OR updated_at < ?)",
        (_now(), cutoff),
    )
    return {"proactive_requeued": 0, "proactive_failed": 0,
            "proactive_unknown": cur.rowcount}


def proactive_summary(con: sqlite3.Connection) -> dict:
    rows = con.execute(
        "SELECT channel, state, COUNT(*) AS n FROM proactive_deliveries"
        " GROUP BY channel, state"
    ).fetchall()
    out = {f"{r['channel']}:{r['state']}": r["n"] for r in rows}
    out["line_month_sent"] = con.execute(
        "SELECT COUNT(*) FROM proactive_deliveries WHERE channel='line'"
        " AND state='SENT' AND strftime('%Y-%m', sent_at) = strftime('%Y-%m','now')"
    ).fetchone()[0]
    return out


# ----------------------------------------------------- LINE quota cache


def save_quota_cache(con: sqlite3.Connection, snap) -> None:
    con.execute(
        "INSERT INTO line_quota_cache"
        " (id, ok, quota_type, limit_value, used, error_category, fetched_at,"
        "  updated_at) VALUES (1,?,?,?,?,?,?,?)"
        " ON CONFLICT(id) DO UPDATE SET ok=excluded.ok,"
        "  quota_type=excluded.quota_type, limit_value=excluded.limit_value,"
        "  used=excluded.used, error_category=excluded.error_category,"
        "  fetched_at=excluded.fetched_at, updated_at=excluded.updated_at",
        (int(bool(snap.ok)), snap.quota_type, snap.limit, snap.used,
         snap.error_category, snap.fetched_at or _now(), _now()),
    )


def load_quota_cache(con: sqlite3.Connection):
    return con.execute(
        "SELECT * FROM line_quota_cache WHERE id = 1"
    ).fetchone()
