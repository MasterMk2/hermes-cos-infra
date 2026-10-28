#!/usr/bin/env python3
"""Calendar Tentative Registration — entries the owner asks for in chat.

Hermes's chat agent cannot write a calendar and holds no Google credential.
What it can do is ask this service, through a unix socket that systemd hands
one connection at a time to a short-lived process of this service's own
account, for one 【仮】 entry on the primary calendar:

    {"op": "add", "title": "...", "date": "YYYY-MM-DD", "start": "HH:MM",
     "end": "HH:MM", "deadline": false, "note": "..."}
    {"op": "list"}

One JSON line in, one JSON line out. The request is text a language model
composed, so every field is checked here, and the only thing it can cause is
the kind of entry a run makes anyway: tentative, free on the owner's
free/busy, no attendees, no invitations, listed under this service's private
marker. It cannot change or delete any event. The owner confirms an entry by
taking 【仮】 off the title and rejects it by deleting it; a rejected entry
comes back only if the owner asks for it again.

The row key is `chat:` plus a hash of what was asked (kind, title, time), so
a request repeated by a retrying agent finds its own row instead of making a
second entry.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, time, timedelta, timezone

import ct_engine
import ct_select
import ct_store as store

MAX_REQUEST_BYTES = 4096
TITLE_LIMIT = 80
NOTE_LIMIT = 200
#: How far ahead a chat request may reach. Wider than the run's horizon:
#: the owner named the date.
MAX_DAYS_AHEAD = 366
LIST_LIMIT = 30

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_DEADLINE_MARK = re.compile(r"^〆(?:\d{1,2}:\d{2})?\s*")
ADD_FIELDS = frozenset({"op", "title", "date", "start", "end", "deadline", "note"})

#: What each state means to the owner, for `list` and for replies.
STATE_LABELS = {
    store.PENDING: "これから入れる",
    store.INSERTING: "確認中",
    store.UNKNOWN: "確認中",
    store.ACTIVE: "カレンダーにある（【仮】のまま）",
    store.CONFIRMED: "確定済み（【仮】を外した）",
    store.REJECTED: "削除された",
    store.USER_EDITED: "手で直された",
    store.WITHDRAWN: "取り下げた",
    store.FAILED: "入れられなかった",
}


class RequestError(ValueError):
    """A request this service will not carry out. `message` is for the owner."""

    def __init__(self, code: str, message: str):
        super().__init__(code)
        self.code = code
        self.message = message

    def reply(self) -> dict:
        return {"ok": False, "error": self.code, "message": self.message}


def parse_request(raw: bytes) -> dict:
    if not raw or len(raw) > MAX_REQUEST_BYTES:
        raise RequestError("bad_request", "依頼の形式が読めません。")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise RequestError("bad_request", "依頼の形式が読めません。") from None
    if not isinstance(data, dict) or data.get("op") not in ("add", "list"):
        raise RequestError("bad_request", "依頼の形式が読めません。")
    return data


def _text(data: dict, key: str, limit: int, *, required: bool) -> str:
    value = data.get(key)
    if value is None or value == "":
        if required:
            raise RequestError("bad_request", f"{key} がありません。")
        return ""
    if not isinstance(value, str):
        raise RequestError("bad_request", f"{key} は文字列で指定してください。")
    return ct_select.clean_text(value, limit)


def _clock(data: dict, key: str):
    value = data.get(key)
    if value is None or value == "":
        return None
    match = _HHMM.match(str(value)) if isinstance(value, str) else None
    if not match:
        raise RequestError("bad_time", f"{key} は 24 時間制の HH:MM で指定してください。")
    return time(int(match.group(1)), int(match.group(2)))


def chat_id_for(kind: str, title: str, start, end, deadline_time) -> str:
    raw = json.dumps([kind, ct_select.normalize_title(title), str(start), str(end),
                      deadline_time or ""], ensure_ascii=False)
    return store.CHAT_PREFIX + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def parse_add(data: dict, *, cfg, now: datetime):
    """(ct_select.Desired, note) for an `add` request, or RequestError."""
    unknown = set(data) - ADD_FIELDS
    if unknown:
        raise RequestError("bad_request", f"知らない項目があります: {', '.join(sorted(unknown))}")
    prefix = cfg.calendar["summary_prefix"].strip()
    title = _text(data, "title", TITLE_LIMIT, required=True)
    while title.startswith(prefix):
        title = title[len(prefix):].lstrip()
    if not ct_select.normalize_title(title):
        raise RequestError("bad_request", "予定の名前が空です。")
    note = _text(data, "note", NOTE_LIMIT, required=False)

    raw_date = data.get("date")
    if not isinstance(raw_date, str) or not _DATE.match(raw_date):
        raise RequestError("bad_date", "date は YYYY-MM-DD で指定してください。")
    try:
        day = date.fromisoformat(raw_date)
    except ValueError:
        raise RequestError("bad_date", "その日付はありません。") from None
    start_clock, end_clock = _clock(data, "start"), _clock(data, "end")
    deadline = data.get("deadline", False)
    if not isinstance(deadline, bool):
        raise RequestError("bad_request", "deadline は true か false で指定してください。")
    if end_clock is not None and (start_clock is None or deadline):
        raise RequestError("bad_time", "終わりの時刻は、始まりの時刻がある予定にだけ付けられます。")

    tz = cfg.tz
    now_local = now.astimezone(tz)
    if day < now_local.date():
        raise RequestError("past", "過去の日付には入れられません。")
    if day > now_local.date() + timedelta(days=MAX_DAYS_AHEAD):
        raise RequestError("too_far", "1 年より先の日付には入れられません。")

    deadline_time = None
    if deadline:
        kind, all_day = ct_select.KIND_DEADLINE, True
        start, end = day, day + timedelta(days=1)
        deadline_time = start_clock.strftime("%H:%M") if start_clock else None
    elif start_clock is None:
        kind, all_day = ct_select.KIND_APPOINTMENT, True
        start, end = day, day + timedelta(days=1)
    else:
        kind, all_day = ct_select.KIND_APPOINTMENT, False
        start = datetime.combine(day, start_clock, tzinfo=tz)
        if end_clock is None:
            end = start + timedelta(minutes=int(cfg.calendar["default_duration_minutes"]))
        else:
            end = datetime.combine(day, end_clock, tzinfo=tz)
            if end <= start:
                raise RequestError("bad_time", "終わりの時刻が始まりより前です。")
        if start < now_local:
            raise RequestError("past", "もう過ぎた時刻には入れられません。")

    desired = ct_select.Desired(
        commitment_id=chat_id_for(kind, title, start, end, deadline_time),
        kind=kind, title=title, all_day=all_day, start=start, end=end,
        deadline_time=deadline_time, source_types=(), counterparty=None, source_count=1,
    )
    return desired, note


def chat_description(desired, note: str, prefix: str) -> str:
    what = "締切" if desired.kind == ct_select.KIND_DEADLINE else "予定"
    lines = [f"Hermes がチャットで頼まれて仮登録した{what}です。"]
    if desired.kind == ct_select.KIND_DEADLINE:
        when = ct_select._md(desired.start)
        lines.append(f"期限: {when} {desired.deadline_time}" if desired.deadline_time
                     else f"期限: {when}")
    if note:
        lines.append(f"メモ: {note}")
    lines.append(f"確定するならタイトル先頭の{prefix.strip()}を消し、不要ならこの予定を削除してください。"
                 "削除した仮登録は、頼み直さない限り再登録しません。")
    lines.append(f"ref: {desired.commitment_id}")
    return "\n".join(lines)


def chat_body(desired, note: str, *, cfg) -> dict:
    body = ct_select.build_event_body(desired, cfg=cfg)
    body["description"] = chat_description(desired, note, cfg.calendar["summary_prefix"])
    return body


def _entry_key(body: dict, prefix: str, tz) -> tuple | None:
    """(normalized title, local date) of an entry body, for "already asked"."""
    summary = str(body.get("summary") or "").lstrip()
    if summary.startswith(prefix):
        summary = summary[len(prefix):]
    summary = _DEADLINE_MARK.sub("", summary)
    start = body.get("start") or {}
    if start.get("date"):
        day = date.fromisoformat(start["date"])
    else:
        kind, value = ct_select.parse_stored(start.get("dateTime"), tz)
        if kind != "dt":
            return None
        day = value.date()
    return ct_select.normalize_title(summary), day


def _reply(state: str, desired, message: str, **extra) -> dict:
    out = {"ok": True, "state": state, "entry": ct_select.line_for(desired),
           "message": message}
    out.update(extra)
    return out


def add(con, desired, note: str, *, cfg, now: datetime, client, index_con=None) -> dict:
    """Reserve one chat entry and, with a client, insert it now.

    `client` is None when another run holds the service lock: the row is
    then left PENDING and the run (or the next timer tick) inserts it.
    """
    cal = ct_engine.bind_calendar(con, cfg)
    prefix = cfg.calendar["summary_prefix"].strip()
    cid = desired.commitment_id
    body = chat_body(desired, note, cfg=cfg)
    revision = ct_select.content_revision(body)
    sort_key = "|".join(desired.sort_key)
    line = ct_select.line_for(desired)

    row = store.get(con, cid)
    if row is not None and row["state"] == store.ACTIVE:
        return _reply("exists", desired, "同じ【仮】の予定がもう入っています。",
                      link=row["html_link"] or None)
    if row is not None and row["state"] in (store.CONFIRMED, store.USER_EDITED):
        return _reply("owner_has_it", desired, "この予定はもうカレンダーにあり、本人の予定になっています。")
    if row is not None and row["state"] in (store.INSERTING, store.UNKNOWN):
        return _reply("checking", desired, "前に頼まれた分が入ったかどうか確認中です。数分後に一覧で確かめてください。")

    if row is None:
        key = _entry_key(body, prefix, cfg.tz)
        for other in store.all_proposals(con).values():
            if other["state"] not in (store.PENDING, *store.LIVE_STATES, store.CONFIRMED,
                                      store.USER_EDITED):
                continue
            if key is not None and _entry_key(json.loads(other["body_json"]), prefix, cfg.tz) == key:
                return _reply("exists", desired, "同じ日に同じ名前の予定がもう入っています。")
        if index_con is not None:
            by_title = ct_select._load_index(index_con, tz=cfg.tz, own_prefix=prefix)
            tolerance = timedelta(minutes=int(cfg.selection["match_tolerance_minutes"]))
            if by_title and ct_select._matches_existing(desired, by_title, tolerance):
                return _reply("already_on_calendar", desired,
                              "カレンダーに同じ予定がもうあります。")

    if row is None or row["state"] in (store.REJECTED, store.WITHDRAWN, store.FAILED):
        since = (now - timedelta(days=1)).astimezone(timezone.utc).isoformat()
        if store.chat_requests_since(con, since) >= int(cfg.limits["max_chat_requests_per_day"]):
            raise RequestError("daily_limit", "チャットからの仮登録は 1 日の上限に達しました。")
        live = sum(1 for r in store.all_proposals(con).values() if r["state"] in store.LIVE_STATES)
        if live >= int(cfg.limits["max_active"]):
            raise RequestError("too_many_active", "【仮】の予定が多すぎます。いくつか確定か削除をしてください。")

    if row is None:
        event_id = ct_select.event_id_for(cal, cid, 1)
        if store.is_retired(con, event_id):  # pragma: no cover - needs a hash collision
            raise RequestError("conflict", "この予定は入れられません。")
        store.reserve(con, commitment_id=cid, calendar_id=cal, event_id=event_id, generation=1,
                      kind=desired.kind, body=body, revision=revision, sort_key=sort_key,
                      line=line)
    elif row["state"] == store.PENDING:
        store.refresh_pending(con, cid, kind=desired.kind, body=body, revision=revision,
                              sort_key=sort_key, line=line)
    else:
        # Rejected or withdrawn before, asked for again: a fresh id, because
        # Google keeps the old one.
        store.repropose(con, cid, event_id_for=ct_select.event_id_for)
        store.set_intended(con, cid, kind=desired.kind, body=body, revision=revision,
                           sort_key=sort_key, line=line)
    con.commit()

    if client is None:
        return _reply("queued", desired, "受け付けました。15 分以内に【仮】でカレンダーに入ります。")
    report = ct_engine.RunReport(mode="chat")
    client.authenticate()
    state, _ = ct_engine.insert_one(con, client, store.get(con, cid), cfg=cfg, cal=cal,
                                    report=report)
    current = store.get(con, cid)
    if state == store.ACTIVE:
        # The owner hears about it in the chat reply; the digest need not repeat it.
        store.mark_notified(con, [cid])
        con.commit()
        return _reply("created", desired, "【仮】でカレンダーに入れました。",
                      link=current["html_link"] or None)
    if state == store.PENDING:
        return _reply("queued", desired, "今は Google に届きませんでした。15 分以内にもう一度入れます。")
    if state == store.FAILED:
        raise RequestError("refused", "Google がこの予定を受け付けませんでした。")
    if state in (store.UNKNOWN, None):
        return _reply("checking", desired, "入ったかどうか確認中です。数分後に一覧で確かめてください。")
    return _reply(state.lower(), desired, STATE_LABELS.get(state, state))


def list_entries(con, *, cfg, now: datetime) -> dict:
    """Entries whose time has not passed, soonest first, with their state."""
    out = []
    for row in sorted(store.all_proposals(con).values(), key=lambda r: r["sort_key"]):
        end = ct_engine.event_end(json.loads(row["body_json"]), cfg.tz)
        if end is not None and end <= now:
            continue
        out.append({"entry": row["line"], "state": row["state"],
                    "meaning": STATE_LABELS.get(row["state"], row["state"]),
                    "asked_in_chat": store.is_chat(row["commitment_id"])})
    return {"ok": True, "entries": out[:LIST_LIMIT], "more": max(0, len(out) - LIST_LIMIT)}
