#!/usr/bin/env python3
"""Bounded full and incremental Gmail synchronisation."""

from __future__ import annotations

import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone

import gm_normalize
import gm_store
from gm_google import GmailApiError


class MessageContentUnavailable(RuntimeError):
    """A single Gmail message cannot be read despite a verified mailbox grant."""


def _message_pages(client, *, query: str):
    token = None
    while True:
        page = client.list_messages(
            q=query, includeSpamTrash=False, maxResults=500,
            **({"pageToken": token} if token else {}),
        )
        if not isinstance(page, dict):
            raise GmailApiError(502, "invalid_message_page")
        yield page
        token = page.get("nextPageToken")
        if not token:
            return


def _history_pages(client, history_id: str):
    token = None
    final_history_id = None
    while True:
        page = client.list_history(
            history_id, maxResults=500,
            **({"pageToken": token} if token else {}),
        )
        if not isinstance(page, dict):
            raise GmailApiError(502, "invalid_history_page")
        yield page
        if page.get("historyId"):
            final_history_id = str(page["historyId"])
        token = page.get("nextPageToken")
        if not token:
            if not final_history_id:
                raise GmailApiError(502, "missing_history_id")
            return final_history_id


def _fetch_and_normalize(client, message_id: str, *, at: str) -> dict:
    try:
        message = client.get_message(message_id)
    except GmailApiError as exc:
        if exc.status == 403 and str(exc.code).startswith("get_message:"):
            # This probes metadata only: it neither stores nor logs headers,
            # subject, body, attachment information, or the message ID.  A
            # A metadata-only result can be either a globally narrow grant or
            # a message-specific content restriction.  The caller decides
            # safely: no full-body success in the batch remains AUTH_REQUIRED;
            # an otherwise verified mailbox retains this opaque record only.
            try:
                client.get_message(message_id, format="metadata")
            except GmailApiError:
                raise MessageContentUnavailable() from exc
            else:
                raise MessageContentUnavailable() from exc
        raise
    if not isinstance(message, dict):
        raise GmailApiError(502, "invalid_message")
    return gm_normalize.normalize_message(message, ingested_at=at)


def full_sync(con, client, *, initial_days: int, reset_history: bool = False,
              now: datetime | None = None) -> dict:
    """Reconcile a deliberately bounded message window through a staging table."""
    started = time.monotonic()
    now = now or datetime.now(timezone.utc)
    range_start = now - timedelta(days=int(initial_days))
    query = f"after:{range_start.date().strftime('%Y/%m/%d')} -in:spam -in:trash"
    profile_before = client.get_profile()
    start_history_id = str(profile_before.get("historyId") or "")
    if not start_history_id:
        raise GmailApiError(502, "profile_missing_history_id")
    generation = uuid.uuid4().hex
    fetched = 0
    readable = 0
    unavailable: list[tuple[str, str]] = []
    try:
        for page in _message_pages(client, query=query):
            for item in page.get("messages") or []:
                message_id = str(item.get("id") or "")
                if not message_id:
                    continue
                try:
                    row = _fetch_and_normalize(client, message_id, at=gm_store.now_iso())
                except MessageContentUnavailable:
                    unavailable.append((message_id, str(item.get("threadId") or message_id)))
                    continue
                gm_store.stage_message(con, generation, row)
                fetched += 1
                readable += 1
            # Staging survives a process failure but has no published effect.
            con.commit()
    except Exception:
        gm_store.discard_staging(con, generation)
        con.commit()
        raise
    if unavailable and not readable:
        gm_store.discard_staging(con, generation)
        con.commit()
        raise GmailApiError(403, "message_body_read_unverified")
    for message_id, thread_id in unavailable:
        gm_store.stage_message(
            con, generation,
            gm_normalize.unavailable_message(message_id, thread_id, ingested_at=gm_store.now_iso()),
        )
    fetched += len(unavailable)
    duration_ms = int((time.monotonic() - started) * 1000)
    with con:
        changes = gm_store.publish_staging(
            con, generation, range_start_ms=int(range_start.timestamp() * 1000),
            # Start from the pre-list profile so any mail that arrived during
            # baseline fetch is replayed by history.list rather than missed.
            history_id=start_history_id,
            reset_history=reset_history,
        )
        gm_store.enqueue_sync(con, "initial_delta" if not reset_history else "history_recovery")
        gm_store.increment_metric(con, "full_syncs")
        gm_store.increment_metric(con, "messages_fetched", fetched)
        if unavailable:
            gm_store.increment_metric(con, "messages_body_unavailable", len(unavailable))
    return {
        "mode": "full",
        "fetched": fetched,
        "changes": changes,
        "duration_ms": duration_ms,
        "range_start": range_start.isoformat(),
        "reset_history": bool(reset_history),
        "body_unavailable": len(unavailable),
    }


def _history_message_ids(page: dict) -> tuple[dict[str, str], set[str]]:
    changed: dict[str, str] = {}
    deleted: set[str] = set()
    def add_changed(message: dict) -> None:
        message_id = str((message or {}).get("id") or "")
        if message_id:
            changed[message_id] = str((message or {}).get("threadId") or changed.get(message_id) or "")
    for entry in page.get("history") or []:
        if not isinstance(entry, dict):
            continue
        for group_name in ("messagesAdded", "labelsAdded", "labelsRemoved"):
            for item in entry.get(group_name) or []:
                message = item.get("message") or {}
                add_changed(message)
        # Gmail can expose a message list in history records in addition to
        # operation-specific arrays.  It is only a candidate list; fetching
        # the current message below remains canonical.
        for message in entry.get("messages") or []:
            add_changed(message)
        for item in entry.get("messagesDeleted") or []:
            message = item.get("message") or {}
            message_id = str(message.get("id") or "")
            if message_id:
                deleted.add(message_id)
    return changed, deleted


def incremental_sync(con, client) -> dict:
    state = gm_store.state(con)
    history_id = str(state["history_id"] or "")
    if not history_id:
        raise GmailApiError(410, "history_missing")
    started = time.monotonic()
    changed_ids: dict[str, str] = {}
    deleted_ids: set[str] = set()
    iterator = _history_pages(client, history_id)
    while True:
        try:
            page = next(iterator)
        except StopIteration as stop:
            next_history_id = str(stop.value or "")
            break
        changed, deleted = _history_message_ids(page)
        changed_ids.update(changed)
        deleted_ids.update(deleted)
    at = gm_store.now_iso()
    changes = Counter()
    fetched = 0
    # The cursor is published last.  A failure rolls back the whole delta;
    # the same history records can be safely replayed after restart.
    con.execute("SAVEPOINT gmail_incremental")
    try:
        for message_id in sorted(changed_ids):
            if message_id in deleted_ids:
                continue
            try:
                row = _fetch_and_normalize(client, message_id, at=at)
            except GmailApiError as exc:
                if exc.status == 404:
                    if gm_store.tombstone_message(con, message_id, at=at):
                        changes["deleted"] += 1
                    continue
                raise
            except MessageContentUnavailable:
                if not gm_store.has_available_content(con):
                    raise GmailApiError(403, "message_body_read_unverified")
                row = gm_normalize.unavailable_message(
                    message_id,
                    changed_ids.get(message_id) or gm_store.message_thread_id(con, message_id) or message_id,
                    ingested_at=at,
                )
                gm_store.increment_metric(con, "messages_body_unavailable")
            changes[gm_store.upsert_message(con, row, baseline=False)] += 1
            fetched += 1
        for message_id in sorted(deleted_ids):
            if gm_store.tombstone_message(con, message_id, at=at):
                changes["deleted"] += 1
        duration_ms = int((time.monotonic() - started) * 1000)
        gm_store.finish_incremental(
            con, history_id=next_history_id, fetched=fetched,
            changed=int(changes["created"] + changes["updated"]),
            deleted=int(changes["deleted"]), duration_ms=duration_ms,
        )
        gm_store.increment_metric(con, "incremental_syncs")
        gm_store.increment_metric(con, "messages_fetched", fetched)
        gm_store.increment_metric(con, "messages_changed", int(changes["created"] + changes["updated"]))
        gm_store.increment_metric(con, "messages_deleted", int(changes["deleted"]))
    except Exception:
        con.execute("ROLLBACK TO gmail_incremental")
        con.execute("RELEASE gmail_incremental")
        raise
    else:
        con.execute("RELEASE gmail_incremental")
    return {
        "mode": "incremental",
        "fetched": fetched,
        "changes": dict(changes),
        "duration_ms": duration_ms,
    }


def sync_mailbox(con, client, *, initial_days: int) -> dict:
    try:
        result = incremental_sync(con, client)
    except GmailApiError as exc:
        if exc.status not in (404, 410):
            raise
        result = full_sync(con, client, initial_days=initial_days, reset_history=True)
    gm_store.set_auth_state(con, "AUTHORIZED")
    return result
