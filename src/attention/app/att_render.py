#!/usr/bin/env python3
"""Attention Engine — the words.

Deterministic templates, no model. Section 73 requires a template fallback for
every important notification whether or not a model is in the path, so the
templates have to exist and be right; once they are, putting a model in front
of them on a five-minute schedule buys nicer phrasing and costs a dependency,
a failure mode and a token bill (sections 19, 21). The configuration has an
`llm.enabled` switch that ships off, and nothing here calls anything.

The shape of a notification is section 68, in this order and no longer:

    what happened      one line, from the source's own summary
    why now            the deadline, the elapsed failure, the state change
    what to do         only when there is something specific

Section 71 matters here: a coding failure reason can carry a path or a command
line, and none of that belongs in a phone notification. `att_coding.safe_reason`
replaces such a reason with a pointer rather than trying to redact it in place.

Nothing in this file interprets the summary text. It is quoted, bounded and
placed; it is never parsed for instructions (section 75).
"""

from __future__ import annotations

import json

from att_time import humanize_delta, overdue_by, parse_iso

#: The gateway refuses anything longer. Kept a little under its limit so that
#: a digest's trailing line can always be appended.
MAX_BODY_CHARS = 1100

SEVERITY_MARK = {
    "INFO": "",
    "IMPORTANT": "",
    "URGENT": "【要対応】",
    "CRITICAL": "【重要】",
}


def _meta(row) -> dict:
    try:
        return json.loads(row["metadata"] or "{}")
    except (ValueError, TypeError):
        return {}


def _bound(text: str, limit: int = MAX_BODY_CHARS) -> str:
    """Cap the length. Line breaks are STRUCTURE and are kept.

    An earlier version folded newlines into spaces, and the first real digest
    arrived as one 200-character paragraph — eight bullet points run together
    on a phone screen. Both platforms accept newlines, and the per-item
    summaries are already collapsed to a single line each on the way in
    (`att_signals.clean_text`), so nothing here needs to flatten anything.
    """
    text = str(text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _lines(*parts) -> str:
    return "\n".join(p for p in parts if p)


# ------------------------------------------------------------- events


def _due_phrase(row, clock) -> str:
    due = parse_iso(row["due_at"])
    if due is None:
        return ""
    local = clock.local_of(due)
    remaining = (due - clock.now()).total_seconds()
    when = local.strftime("%m/%d %H:%M")
    if remaining >= 0:
        return f"期限は {when}、残り{humanize_delta(remaining)}です。"
    return f"期限は {when} で、{overdue_by(remaining)}超過しています。"


def render_event(row, clock) -> tuple:
    meta = _meta(row)
    summary = row["summary"]
    kind = row["signal_type"]

    if kind == "due_soon":
        subject = "期限が近づいています"
        body = _lines(f"「{summary}」が未完了です。", _due_phrase(row, clock))
    elif kind == "overdue":
        subject = "期限を過ぎています"
        body = _lines(f"「{summary}」が未完了のままです。",
                      _due_phrase(row, clock))
    elif kind == "snooze_expired":
        subject = "スヌーズが明けました"
        body = _lines(f"「{summary}」のスヌーズ期間が終わりました。",
                      _due_phrase(row, clock))
    elif kind == "waiting_for_user":
        subject = "対応が必要な依頼があります"
        who = meta.get("counterparty") or ""
        body = _lines(f"「{summary}」への対応が求められています。"
                      + (f"（{who}）" if who else ""),
                      _due_phrase(row, clock))
    else:
        subject = "新しい予定・約束"
        body = _lines(f"「{summary}」を記録しました。", _due_phrase(row, clock))
    return subject, body


# ----------------------------------------------------------- calendar


def render_calendar(row, clock) -> tuple:
    meta = _meta(row)
    summary = row["summary"]
    when = _due_phrase(row, clock).replace("期限は", "予定は") if row["due_at"] else ""
    location = meta.get("location") or ""
    kind = row["signal_type"]
    if kind == "calendar_event_cancelled":
        return ("予定が取り消されました", _lines(f"「{summary}」は Calendar 上で取り消されました。", when))
    if kind == "calendar_event_changed":
        return ("予定が変更されました", _lines(f"「{summary}」の Calendar 上の予定が変更されました。", when, f"場所: {location}" if location else ""))
    if kind == "calendar_important_due_soon":
        return ("重要な予定が近づいています", _lines(f"「{summary}」が近づいています。", when, f"場所: {location}" if location else ""))
    return ("予定があります", _lines(f"「{summary}」", when))


# ------------------------------------------------------------- coding


def render_coding(row, clock) -> tuple:
    meta = _meta(row)
    job_id = row["source_id"]
    goal = row["summary"]
    kind = row["signal_type"]
    reason = meta.get("reason") or ""

    if kind == "coding_completed":
        subject = "Coding job が完了しました"
        body = _lines(
            f"Coding job {job_id} が完了しました。",
            f"目的: {goal}" if goal else "",
            "GLM 実装 → deterministic validation → Muse review → "
            "exact-revision gate を通過しています。",
        )
    elif kind == "coding_waiting_human":
        subject = "Coding job が判断待ちです"
        body = _lines(
            f"Coding job {job_id} が人間の判断待ちです。",
            f"目的: {goal}" if goal else "",
            f"理由: {reason}" if reason else "",
        )
    elif kind == "coding_review_cycle_limit":
        subject = "Coding job のレビュー回数が上限に達しました"
        body = _lines(
            f"Coding job {job_id} がレビュー上限に達し、自動修正を停止しました。",
            f"目的: {goal}" if goal else "",
            "続行するには予算を引き上げて job_continue するか、内容を確認してください。",
        )
    else:
        subject = "Coding job が起動できませんでした"
        body = _lines(
            f"Coding job {job_id} が開始できず、再試行では解決しません。",
            f"理由: {reason}" if reason else "",
        )
    return subject, body


# ------------------------------------------------------------- health


def _health_evidence(meta) -> str:
    """The one concrete number that makes the alert actionable, in Japanese.

    The probe's `detail` is written for a log line and stays English; putting
    it in a notification produced "約6分継続しています。（the last 3 runs
    reported a failure）", which is both languages at once and buries the
    figure that matters. Facts the probes publish are rendered here instead,
    and `detail` is the fallback for the ones that have none.
    """
    if meta.get("consecutive_failures"):
        return f"直近 {meta['consecutive_failures']} 回の実行が失敗しています。"
    if meta.get("queue_depth"):
        return f"未処理が {meta['queue_depth']} 件たまっています。"
    if meta.get("free_percent") is not None:
        return f"空き容量は {meta['free_percent']}% です。"
    if meta.get("age_hours"):
        return f"最後の実行から {meta['age_hours']} 時間経過しています。"
    if meta.get("stalled"):
        return f"{meta['stalled']} 件のジョブが進行していません。"
    return ""


def render_health(row, clock) -> tuple:
    meta = _meta(row)
    service = row["source_id"]
    if row["signal_type"] == "health_recovered":
        return ("復旧しました",
                _lines(f"{service} は復旧しました。",
                       f"（{meta.get('reason', '')} の状態が解消）"
                       if meta.get("reason") else ""))
    degraded = int(meta.get("degraded_seconds") or 0)
    evidence = _health_evidence(meta)
    return ("システム障害", _lines(
        f"{service}: {row['summary']}",
        evidence,
        f"検知から{humanize_delta(degraded)}経過しています。" if degraded else "",
    ))


RENDERERS = {
    "event": render_event,
    "calendar": render_calendar,
    "coding": render_coding,
    "health": render_health,
}


def render(row, clock) -> tuple:
    """(subject, body) for one signal. Never raises, never empty."""
    fn = RENDERERS.get(row["source_type"])
    try:
        subject, body = fn(row, clock) if fn else (None, None)
    except Exception:
        subject, body = None, None
    if not body:
        # The last-resort template of section 73: an important signal must
        # produce SOMETHING rather than nothing if rendering goes wrong.
        subject = "確認が必要な項目があります"
        body = str(row["summary"] or row["signal_type"])
    mark = SEVERITY_MARK.get(str(row["severity"]), "")
    return subject, _bound(f"{mark}{body}" if mark else body)


# ------------------------------------------------------------ digests


DIGEST_TITLE = {
    "catchup": "夜間に保留した項目",
    "baseline": "現在たまっている項目（初回同期）",
    "recovery": "停止中にたまった項目",
    "scheduled": "まとめてお知らせ",
}

#: Order within a digest (section 60). Urgency first, then how soon, then
#: whether a person is blocked, then finished work, then everything else.
DIGEST_ORDER = {
    "health_degraded": 0,
    "overdue": 1,
    "due_soon": 2,
    "waiting_for_user": 3,
    "coding_waiting_human": 3,
    "coding_review_cycle_limit": 3,
    "coding_failed_to_start": 3,
    "snooze_expired": 4,
    "coding_completed": 5,
    "new_high_confidence_commitment": 6,
    "health_recovered": 7,
    "calendar_event_cancelled": 1,
    "calendar_event_changed": 2,
    "calendar_important_due_soon": 2,
    "calendar_upcoming_event": 8,
}


def digest_sort_key(row):
    from att_signals import SEVERITY_ORDER

    return (
        DIGEST_ORDER.get(row["signal_type"], 9),
        -SEVERITY_ORDER.get(str(row["severity"]), 0),
        row["due_at"] or "9999",
    )


def digest_line(row, clock) -> str:
    """One item, one short line. A digest of paragraphs is not a digest."""
    summary = str(row["summary"] or "")[:70]
    kind = row["signal_type"]
    due = parse_iso(row["due_at"])
    if kind in ("due_soon", "overdue") and due is not None:
        remaining = (due - clock.now()).total_seconds()
        when = (f"残り{humanize_delta(remaining)}" if remaining >= 0
                else f"{overdue_by(remaining)}超過")
        return f"・{summary}（{when}）"
    if kind == "coding_completed":
        return f"・完了: coding job {row['source_id']}"
    if kind in ("coding_waiting_human", "coding_review_cycle_limit",
                "coding_failed_to_start"):
        return f"・判断待ち: coding job {row['source_id']}"
    if kind == "health_degraded":
        return f"・障害: {row['source_id']} — {summary}"
    if kind == "health_recovered":
        return f"・復旧: {row['source_id']}"
    return f"・{summary}"


def render_digest(rows, kind: str, clock, *, max_items: int = 8) -> tuple:
    """(subject, body) for a digest, or (None, None) when there is nothing.

    An empty digest is not sent (section 61). "特に何もありません" every
    morning is how a channel becomes background noise.
    """
    rows = list(rows)
    if not rows:
        return None, None
    ordered = sorted(rows, key=digest_sort_key)
    shown = ordered[:max_items]
    lines = [digest_line(r, clock) for r in shown]
    hidden = len(ordered) - len(shown)
    if hidden > 0:
        lines.append(f"・ほか {hidden} 件")
    title = DIGEST_TITLE.get(kind, DIGEST_TITLE["scheduled"])
    body = _lines(f"{title}（{len(ordered)} 件）", *lines)
    return title, _bound(body)
