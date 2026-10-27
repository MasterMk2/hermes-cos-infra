#!/usr/bin/env python3
"""Calendar Tentative Registration — one run: decide, reconcile, apply, report.

    1. reserve     desired commitments get a PENDING row and a fixed event id
    2. resolve     INSERTING/UNKNOWN rows are settled by GETting that exact id
    3. reconcile   one list of the tentative calendar shows what the owner did:
                   deleted (REJECTED), moved to a real calendar (CONFIRMED),
                   edited (USER_EDITED), or left alone
    4. maintain    untouched entries follow their commitment: PATCH on change,
                   DELETE when it resolved or the real event now exists
    5. insert      new entries, soonest first, within per-run and total caps
    6. notify      one owner digest for entries not yet reported

Every Google mutation is preceded by a committed state change, so a crash at
any point leaves a row that says what might have happened. Nothing is ever
inserted twice: the event id is derived from (calendar, commitment,
generation), a lost insert is resolved by reading that id back, and a retired
id is never reused.

The owner always wins. PATCH and DELETE carry `If-Match: <etag>`; if the owner
touched the entry, Google answers 412 and the row becomes USER_EDITED. A
deleted entry is never re-created. Withdrawal only removes an entry this
service created, on the calendar this service created, and only one whose
time has not passed.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone

import ct_notify
import ct_select
import ct_store as store
from ct_google import ApiError

log = logging.getLogger("calendar_tentative.engine")

PRIVATE_MARKER = "hermes_ct"


class CalendarChanged(RuntimeError):
    """The configured calendar is not the one this state database belongs to."""


@dataclass
class RunReport:
    mode: str
    counts: dict = field(default_factory=dict)
    skipped: dict = field(default_factory=dict)
    plan: dict = field(default_factory=dict)
    notification: str | None = None
    index_available: bool = False
    gmail_screen_available: bool = False
    gmail_categorized: tuple | None = None

    def bump(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n

    def as_dict(self) -> dict:
        return {"mode": self.mode, "counts": dict(sorted(self.counts.items())),
                "skipped": self.skipped, "plan": self.plan,
                "notification": self.notification,
                "index_available": self.index_available,
                "gmail_screen_available": self.gmail_screen_available,
                "gmail_categorized": (None if self.gmail_categorized is None else
                                      {"received": self.gmail_categorized[0],
                                       "categorized": self.gmail_categorized[1]})}


# ------------------------------------------------------------------ helpers


def _norm_when(part):
    if not isinstance(part, dict):
        return None
    if part.get("date"):
        return ("date", str(part["date"]))
    kind, value = ct_select.parse_stored(part.get("dateTime"), timezone.utc)
    return ("dt", value) if kind == "dt" else None


def remote_matches(remote: dict, body: dict | None) -> bool:
    """The owner-visible fields Google holds equal `body`'s.

    Colour, reminders and similar presentation changes are not compared: an
    owner recolouring an entry has not edited what it says.
    """
    if not body:
        return False
    return (
        str(remote.get("summary") or "") == body.get("summary", "")
        and str(remote.get("description") or "").rstrip() == body.get("description", "").rstrip()
        and _norm_when(remote.get("start")) == _norm_when(body.get("start"))
        and _norm_when(remote.get("end")) == _norm_when(body.get("end"))
    )


def _body(row, column: str = "body_json"):
    raw = row[column]
    return json.loads(raw) if raw else None


def event_end(body: dict, tz) -> datetime | None:
    end = body.get("end") or {}
    if end.get("date"):
        return datetime.combine(date.fromisoformat(end["date"]), time.min, tzinfo=tz)
    kind, value = ct_select.parse_stored(end.get("dateTime"), tz)
    return value if kind == "dt" else None


def insert_resource(body: dict, *, event_id: str, commitment_id: str) -> dict:
    resource = dict(body)
    resource["id"] = event_id
    resource["extendedProperties"] = {
        "private": {PRIVATE_MARKER: "1", "hermes_commitment": commitment_id},
    }
    return resource


@dataclass(frozen=True)
class _Item:
    desired: ct_select.Desired
    body: dict
    revision: str
    sort_key: str
    line: str


def _items(selection, cfg) -> dict:
    out = {}
    for cid, desired in selection.desired.items():
        body = ct_select.build_event_body(desired, cfg=cfg)
        out[cid] = _Item(desired, body, ct_select.content_revision(body),
                         "|".join(desired.sort_key), ct_select.line_for(desired))
    return out


# --------------------------------------------------------------------- plan


def plan(state_con, selection, *, cfg, now: datetime) -> RunReport:
    """What a run would do, from local state only. No Google call."""
    report = RunReport(mode="plan", skipped=selection.counts(),
                       index_available=selection.index_available,
                       gmail_screen_available=selection.gmail_screen_available,
                       gmail_categorized=selection.gmail_categorized)
    items = _items(selection, cfg)
    rows = store.all_proposals(state_con)
    limits, beh = cfg.limits, cfg.behaviour

    creates = sorted(
        (item for cid, item in items.items()
         if cid not in rows or rows[cid]["state"] == store.PENDING),
        key=lambda item: item.sort_key)
    live = sum(1 for r in rows.values() if r["state"] in store.LIVE_STATES)
    room = max(0, min(int(limits["max_creates_per_run"]), int(limits["max_active"]) - live))
    report.plan["create"] = [item.line for item in creates[:room]]
    report.plan["create_deferred"] = max(0, len(creates) - room)

    updates, withdrawals = [], []
    for cid, row in sorted(rows.items(), key=lambda kv: kv[1]["sort_key"]):
        if row["state"] != store.ACTIVE:
            continue
        reason = selection.reason_for(cid)
        if reason is None and beh["update_on_change"] and items[cid].revision != row["applied_revision"]:
            updates.append(items[cid].line)
        elif reason in ct_select.WITHDRAW_REASONS and beh["withdraw_on_resolution"]:
            end = event_end(_body(row), cfg.tz)
            if end is not None and end > now:
                withdrawals.append(f"{row['line']} ({reason})")
    report.plan["update"] = updates[: int(limits["max_updates_per_run"])]
    report.plan["withdraw"] = withdrawals[: int(limits["max_withdrawals_per_run"])]
    report.plan["held_unknown"] = sum(
        1 for r in rows.values() if r["state"] in (store.INSERTING, store.UNKNOWN))
    return report


# ---------------------------------------------------------------------- run


def run(*, cfg, state_con, selection, client, now: datetime, notifier=None) -> RunReport:
    """Apply one run against Google. `selection` comes from ct_select.select."""
    cal = cfg.calendar_id
    if not cal:
        raise CalendarChanged("calendar.calendar_id is not configured")
    bound = store.get_meta(state_con, "calendar_id")
    if bound and bound != cal:
        raise CalendarChanged(
            "this state database belongs to another tentative calendar; move it "
            "aside before pointing the service at a new calendar")
    if not bound:
        store.set_meta(state_con, "calendar_id", cal)
        state_con.commit()

    report = RunReport(mode="apply", skipped=selection.counts(),
                       index_available=selection.index_available,
                       gmail_screen_available=selection.gmail_screen_available,
                       gmail_categorized=selection.gmail_categorized)
    items = _items(selection, cfg)

    client.authenticate()
    _reserve(state_con, items, selection, cal=cal, report=report)
    _resolve_unknown(state_con, client, cal=cal, report=report)
    snapshot_ok = _reconcile(state_con, client, selection, cal=cal, report=report)
    if snapshot_ok:
        _maintain(state_con, client, items, selection, cfg=cfg, cal=cal, now=now, report=report)
    _insert(state_con, client, items, cfg=cfg, cal=cal, report=report)
    if notifier is not None and cfg.notify["enabled"]:
        report.notification = _notify(state_con, notifier, cfg=cfg, now=now)
    return report


def _reserve(con, items: dict, selection, *, cal: str, report: RunReport) -> None:
    rows = store.all_proposals(con)
    for cid, item in items.items():
        row = rows.get(cid)
        if row is None:
            generation = 1
            event_id = ct_select.event_id_for(cal, cid, generation)
            while store.is_retired(con, event_id):  # pragma: no cover - collision only
                generation += 1
                event_id = ct_select.event_id_for(cal, cid, generation)
            store.reserve(con, commitment_id=cid, calendar_id=cal, event_id=event_id,
                          generation=generation, kind=item.desired.kind, body=item.body,
                          revision=item.revision, sort_key=item.sort_key, line=item.line)
            report.bump("reserved")
        elif row["state"] == store.PENDING:
            store.refresh_pending(con, cid, kind=item.desired.kind, body=item.body,
                                  revision=item.revision, sort_key=item.sort_key, line=item.line)
    for cid, row in rows.items():
        if row["state"] == store.PENDING and cid not in items:
            # Nothing reached Google for this row (or Google proved it), so
            # forgetting it costs nothing and frees nothing that matters.
            store.drop_pending(con, cid)
            report.bump("dropped_pending")
    con.commit()


def _settle_from_remote(con, row, remote, *, report: RunReport) -> None:
    """Settle an INSERTING/UNKNOWN row from a GET of its exact event id."""
    cid = row["commitment_id"]
    if remote is None:
        # Google has no event with this id: the insert never happened.
        store.transition(con, cid, store.PENDING, error="absent_after_unknown")
        report.bump("resolved_absent")
    elif str(remote.get("status") or "") == "cancelled":
        store.transition(con, cid, store.REJECTED, error="deleted_before_resolution")
        report.bump("rejected")
    elif remote_matches(remote, _body(row)):
        store.transition(con, cid, store.ACTIVE, etag=remote.get("etag") or "",
                         html_link=remote.get("htmlLink") or "",
                         applied_revision=row["content_revision"], applied_body=_body(row),
                         sent=True)
        report.bump("resolved_created")
    else:
        store.transition(con, cid, store.USER_EDITED, etag=remote.get("etag") or "", sent=True)
        report.bump("user_edited")
    con.commit()


def _resolve_unknown(con, client, *, cal: str, report: RunReport) -> None:
    for cid, row in store.all_proposals(con).items():
        if row["state"] not in (store.INSERTING, store.UNKNOWN):
            continue
        if row["state"] == store.INSERTING:
            # Only a crash leaves INSERTING behind: runs are serialized by
            # the service lock. It is exactly as uncertain as UNKNOWN.
            store.transition(con, cid, store.UNKNOWN, expect=store.INSERTING,
                             error="interrupted_insert")
            con.commit()
            row = store.get(con, cid)
        try:
            remote = client.get_event(cal, row["event_id"])
        except ApiError as exc:
            store.note_error(con, cid, f"resolve:{exc.status}:{exc.code}")
            con.commit()
            report.bump("held_unknown")
            continue
        _settle_from_remote(con, row, remote, report=report)


def _reconcile(con, client, selection, *, cal: str, report: RunReport) -> bool:
    """Learn what the owner did to ACTIVE entries. False if Google is unreadable."""
    active = [r for r in store.all_proposals(con).values() if r["state"] == store.ACTIVE]
    if not active:
        return True
    try:
        snapshot = {str(item.get("id")): item for item in client.list_events(cal)}
    except ApiError as exc:
        log.warning("tentative calendar unreadable: %s:%s", exc.status, exc.code)
        report.bump("list_failed")
        return False
    for row in active:
        cid = row["commitment_id"]
        remote = snapshot.get(row["event_id"])
        if remote is None or str(remote.get("status") or "") == "cancelled":
            if row["withdraw_requested_at"]:
                store.transition(con, cid, store.WITHDRAWN, error=row["withdraw_reason"])
                report.bump("withdrawn")
            elif row["event_id"] in selection.event_ids_elsewhere:
                store.transition(con, cid, store.CONFIRMED)
                report.bump("confirmed")
            else:
                store.transition(con, cid, store.REJECTED)
                report.bump("rejected")
        elif (remote.get("etag") or "") != (row["etag"] or ""):
            if remote_matches(remote, _body(row)):
                # Our own update whose answer was lost, or a change that did
                # not touch what the entry says.
                store.adopt_remote(con, cid, etag=remote.get("etag") or "",
                                   applied_revision=row["content_revision"],
                                   applied_body=_body(row))
            elif remote_matches(remote, _body(row, "applied_body_json")):
                store.adopt_remote(con, cid, etag=remote.get("etag") or "")
            else:
                store.transition(con, cid, store.USER_EDITED, etag=remote.get("etag") or "")
                report.bump("user_edited")
        con.commit()
    return True


def _maintain(con, client, items: dict, selection, *, cfg, cal: str, now: datetime,
              report: RunReport) -> None:
    beh, limits = cfg.behaviour, cfg.limits
    updates = withdrawals = 0
    active = sorted((r for r in store.all_proposals(con).values() if r["state"] == store.ACTIVE),
                    key=lambda r: r["sort_key"])
    for row in active:
        cid = row["commitment_id"]
        reason = selection.reason_for(cid)
        if reason is None:
            item = items[cid]
            if row["withdraw_requested_at"]:
                # Wanted again (a reopened commitment) after a delete that did
                # not go through. A later disappearance is the owner's doing.
                store.clear_withdrawal(con, cid)
                con.commit()
            if not beh["update_on_change"] or item.revision == row["applied_revision"]:
                continue
            if updates >= int(limits["max_updates_per_run"]):
                report.bump("update_deferred")
                continue
            updates += 1
            store.set_intended(con, cid, kind=item.desired.kind, body=item.body,
                               revision=item.revision, sort_key=item.sort_key, line=item.line)
            con.commit()
            _patch(con, client, store.get(con, cid), item, cal=cal, report=report)
        elif reason in ct_select.WITHDRAW_REASONS and beh["withdraw_on_resolution"]:
            end = event_end(_body(row), cfg.tz)
            if end is None or end <= now:
                continue  # the past stays as it was
            if withdrawals >= int(limits["max_withdrawals_per_run"]):
                report.bump("withdraw_deferred")
                continue
            withdrawals += 1
            store.request_withdrawal(con, cid, reason)
            con.commit()
            _delete(con, client, store.get(con, cid), cal=cal, report=report)


def _patch(con, client, row, item: _Item, *, cal: str, report: RunReport) -> None:
    cid = row["commitment_id"]
    try:
        remote = client.patch_event(cal, row["event_id"], item.body, etag=row["etag"] or "")
    except ApiError as exc:
        if exc.status == 412:
            # Someone else changed it since our last write. If what Google has
            # is exactly what we meant to write, the someone was us.
            try:
                remote = client.get_event(cal, row["event_id"])
            except ApiError:
                remote = None
            if remote is not None and remote_matches(remote, item.body):
                store.adopt_remote(con, cid, etag=remote.get("etag") or "",
                                   applied_revision=item.revision, applied_body=item.body)
                report.bump("updated")
            else:
                store.transition(con, cid, store.USER_EDITED, error="precondition_failed")
                report.bump("user_edited")
        else:
            # 404/410 and lost answers are settled by the next reconcile.
            store.note_error(con, cid, f"patch:{exc.status}:{exc.code}")
            report.bump("update_failed")
        con.commit()
        return
    store.adopt_remote(con, cid, etag=remote.get("etag") or "",
                       applied_revision=item.revision, applied_body=item.body)
    con.commit()
    report.bump("updated")


def _delete(con, client, row, *, cal: str, report: RunReport) -> None:
    cid = row["commitment_id"]
    try:
        client.delete_event(cal, row["event_id"], etag=row["etag"] or "")
    except ApiError as exc:
        if exc.status in (404, 410):
            store.transition(con, cid, store.WITHDRAWN, error=row["withdraw_reason"])
            report.bump("withdrawn")
        elif exc.status == 412:
            # The owner changed it after the commitment resolved. Their entry
            # now, not ours to remove.
            store.transition(con, cid, store.USER_EDITED, error="precondition_failed")
            report.bump("user_edited")
        else:
            store.note_error(con, cid, f"delete:{exc.status}:{exc.code}")
            report.bump("withdraw_failed")
        con.commit()
        return
    store.transition(con, cid, store.WITHDRAWN, error=row["withdraw_reason"])
    con.commit()
    report.bump("withdrawn")


def _insert(con, client, items: dict, *, cfg, cal: str, report: RunReport) -> None:
    limits = cfg.limits
    rows = store.all_proposals(con)
    live = sum(1 for r in rows.values() if r["state"] in store.LIVE_STATES)
    pending = sorted((r for cid, r in rows.items()
                      if r["state"] == store.PENDING and cid in items),
                     key=lambda r: r["sort_key"])
    created = 0
    for index, row in enumerate(pending):
        if created >= int(limits["max_creates_per_run"]) or live >= int(limits["max_active"]):
            report.bump("create_deferred", len(pending) - index)
            break
        cid = row["commitment_id"]
        if store.is_retired(con, row["event_id"]):  # pragma: no cover - defensive
            continue
        if not store.transition(con, cid, store.INSERTING, expect=store.PENDING,
                                count_attempt=True):
            continue
        con.commit()
        body = _body(row)
        resource = insert_resource(body, event_id=row["event_id"], commitment_id=cid)
        try:
            remote = client.insert_event(cal, resource)
        except ApiError as exc:
            if exc.status == 409:
                # The id exists: an earlier insert of ours whose answer was
                # lost. Read it back rather than guess.
                try:
                    existing = client.get_event(cal, row["event_id"])
                except ApiError:
                    store.transition(con, cid, store.UNKNOWN, error="duplicate_id_unverified")
                    report.bump("held_unknown")
                else:
                    _settle_from_remote(con, store.get(con, cid), existing, report=report)
            elif exc.not_applied:
                # Only a refusal Google itself gave counts toward FAILED; an
                # outage on our side (DNS, refused connection) never does.
                attempts = int(store.get(con, cid)["attempts"])
                final = exc.sent and attempts >= int(limits["max_attempts"])
                store.transition(con, cid, store.FAILED if final else store.PENDING,
                                 error=f"insert:{exc.status}:{exc.code}")
                report.bump("create_failed")
            else:
                store.transition(con, cid, store.UNKNOWN, error=f"insert:{exc.status}:{exc.code}")
                report.bump("held_unknown")
            con.commit()
            live += 1 if store.get(con, cid)["state"] in store.LIVE_STATES else 0
            continue
        store.transition(con, cid, store.ACTIVE, etag=remote.get("etag") or "",
                         html_link=remote.get("htmlLink") or "",
                         applied_revision=row["content_revision"], applied_body=body, sent=True)
        con.commit()
        created += 1
        live += 1
        report.bump("created")


def _notify(con, notifier, *, cfg, now: datetime) -> str | None:
    note_cfg = cfg.notify
    if ct_notify.in_quiet_hours(now.astimezone(cfg.tz), note_cfg["quiet_hours_start"],
                                note_cfg["quiet_hours_end"]):
        return "quiet_hours"
    pending = store.open_notification(con)
    if pending is None:
        rows = sorted((r for r in store.all_proposals(con).values()
                       if r["state"] == store.ACTIVE and r["sent_at"] and not r["notified_at"]),
                      key=lambda r: r["sort_key"])
        if not rows:
            return None
        ids = [r["commitment_id"] for r in rows]
        store.add_notification(
            con, notification_id=ct_notify.notification_id_for(ids),
            body=ct_notify.render_digest([r["line"] for r in rows],
                                         max_lines=int(note_cfg["max_lines"])),
            commitment_ids=ids)
        con.commit()
        pending = store.open_notification(con)
    outcome = notifier.send(notification_id=pending["notification_id"], text=pending["body"])
    ids = json.loads(pending["commitment_ids"])
    if outcome in ct_notify.SUCCESS:
        store.settle_notification(con, pending["notification_id"], state="DONE", outcome=outcome)
        store.mark_notified(con, ids)
    elif outcome in ct_notify.HOLD or int(pending["attempts"]) + 1 >= ct_notify.MAX_ATTEMPTS:
        # Possibly delivered, or refused for good: either way, sending a
        # different digest that repeats these entries would be the duplicate.
        store.settle_notification(con, pending["notification_id"], state="HELD", outcome=outcome)
        store.mark_notified(con, ids)
    else:
        store.note_notification_attempt(con, pending["notification_id"], outcome)
    con.commit()
    return outcome
