"""Resolve dated Discord commitments from attributable follow-up replies.

The automatic path accepts only an explicit completion statement in a direct
reply by the original author. An operator may confirm a less literal reply
after reviewing it; both paths record the reply as completion evidence.
Neither the passage of time nor a message in the same channel is proof.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

from .models import CommitmentEvidence, WorkState
from .store import get_commitment, save_commitment


ACTIVE_STATES = ("ACTIONABLE", "WAITING", "SCHEDULED", "BLOCKED")
COMPLETION_RE = re.compile(
    r"(?:^|[。！\n])[^。！？\n]{0,60}"
    r"(?:完了しました|完了です|終了しました|終わりました|終えました|済ませました|"
    r"提出しました|送付しました|確認しました|対応しました|"
    r"受診しました|受けてきました)(?:[。！\s]|$)"
)
UNCERTAIN_RE = re.compile(
    r"未完了|未提出|未確認|まだ|していません|できていません|"
    r"予定|はず|かも|[?？]"
)


def _when(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _message_id(source_id: str) -> str | None:
    parts = str(source_id).split(":")
    if len(parts) != 4 or parts[0] != "discord":
        return None
    return parts[-1] or None


def _origins(events: sqlite3.Connection, *, include_done=False) -> dict[str, set[str]]:
    """Map original Discord message ids to dated commitments."""
    states = ACTIVE_STATES + ((WorkState.DONE.value,) if include_done else ())
    placeholders = ",".join("?" for _ in states)
    rows = events.execute(
        "SELECT c.commitment_id, e.source_id FROM commitments c "
        "JOIN commitment_evidence e ON e.commitment_id=c.commitment_id "
        f"WHERE c.work_state IN ({placeholders}) "
        "AND c.currentness='current' AND (c.due_at IS NOT NULL OR c.scheduled_at IS NOT NULL) "
        "AND e.source_type='discord' AND e.evidence_role='origin'",
        states,
    )
    by_message: dict[str, set[str]] = defaultdict(set)
    for commitment_id, source_id in rows:
        message_id = _message_id(source_id)
        if message_id:
            by_message[message_id].add(commitment_id)
    return by_message


def _related(origin: sqlite3.Row, reply: sqlite3.Row) -> bool:
    origin_at = _when(origin["created_at"])
    reply_at = _when(reply["created_at"])
    return (
        reply["reply_to_message_id"] == origin["message_id"]
        and reply["author_id"] == origin["author_id"]
        and reply["channel_id"] == origin["channel_id"]
        and reply["guild_id"] == origin["guild_id"]
        and reply["deleted_at"] is None
        and origin_at is not None
        and reply_at is not None
        and reply_at > origin_at
    )


def _automatic(text: str) -> bool:
    cleaned = str(text or "").strip()
    return not UNCERTAIN_RE.search(cleaned) and bool(COMPLETION_RE.search(cleaned))


def candidates(events: sqlite3.Connection, discord: sqlite3.Connection) -> list[dict]:
    """Return strict automatic candidates without changing either database."""
    by_message = _origins(events)
    if not by_message:
        return []
    discord.row_factory = sqlite3.Row
    originals = {
        row["message_id"]: row for row in discord.execute(
            "SELECT message_id, author_id, channel_id, guild_id, created_at "
            "FROM messages WHERE message_id IS NOT NULL AND deleted_at IS NULL"
        ) if row["message_id"] in by_message
    }
    latest_reply: dict[str, sqlite3.Row] = {}
    for reply in discord.execute(
        "SELECT message_id, reply_to_message_id, author_id, channel_id, guild_id, "
        "created_at, deleted_at, content FROM messages "
        "WHERE reply_to_message_id IS NOT NULL ORDER BY created_ts"
    ):
        origin = originals.get(reply["reply_to_message_id"])
        if origin is None or not _related(origin, reply):
            continue
        latest_reply[origin["message_id"]] = reply
    found: dict[str, dict] = {}
    for origin_message_id, reply in latest_reply.items():
        # One source message can create several commitments. An unspecific
        # completion reply cannot prove which one was finished.
        if len(by_message[origin_message_id]) != 1 or not _automatic(reply["content"]):
            continue
        for commitment_id in by_message[origin_message_id]:
            found[commitment_id] = {
                "commitment_id": commitment_id,
                "origin_message_id": origin_message_id,
                "reply_message_id": reply["message_id"],
                "guild_id": reply["guild_id"],
                "channel_id": reply["channel_id"],
                "completed_at": reply["created_at"],
                "basis": "explicit_direct_reply",
            }
    return list(found.values())


def confirmed_reply(events: sqlite3.Connection, discord: sqlite3.Connection,
                    commitment_id: str, reply_message_id: str) -> dict | None:
    """Validate the provenance of an operator-reviewed completion reply."""
    discord.row_factory = sqlite3.Row
    origin_ids = {
        message_id for message_id, ids in _origins(events, include_done=True).items()
        if commitment_id in ids
    }
    if not origin_ids:
        return None
    reply = discord.execute(
        "SELECT message_id, reply_to_message_id, author_id, channel_id, guild_id, "
        "created_at, deleted_at FROM messages WHERE message_id=?",
        (reply_message_id,),
    ).fetchone()
    if reply is None or reply["reply_to_message_id"] not in origin_ids:
        return None
    origin = discord.execute(
        "SELECT message_id, author_id, channel_id, guild_id, created_at "
        "FROM messages WHERE message_id=? AND deleted_at IS NULL",
        (reply["reply_to_message_id"],),
    ).fetchone()
    if origin is None or not _related(origin, reply):
        return None
    return {
        "commitment_id": commitment_id,
        "origin_message_id": origin["message_id"],
        "reply_message_id": reply["message_id"],
        "guild_id": reply["guild_id"],
        "channel_id": reply["channel_id"],
        "completed_at": reply["created_at"],
        "basis": "operator_verified_direct_reply",
    }


def apply_completion(events: sqlite3.Connection, candidate: dict) -> bool:
    """Record DONE and its source pointer atomically; safe to retry."""
    events.execute("BEGIN IMMEDIATE")
    try:
        cmt = get_commitment(events, candidate["commitment_id"])
        if cmt is None or cmt.work_state not in ACTIVE_STATES:
            events.rollback()
            return False
        reply_source = (
            f"discord:{candidate['guild_id']}:{candidate['channel_id']}:"
            f"{candidate['reply_message_id']}"
        )
        cmt.work_state = WorkState.DONE.value
        cmt.resolved_at = candidate["completed_at"]
        cmt.evidence.append(CommitmentEvidence(
            source_type="discord",
            source_id=reply_source,
            observed_at=candidate["completed_at"],
            evidence_role="completion",
            confidence=1.0,
        ))
        save_commitment(
            events, cmt, actor="discord_completion",
            reason=f"{candidate['basis']}:{candidate['reply_message_id']}",
            auto_commit=False,
        )
        events.commit()
        return True
    except Exception:
        events.rollback()
        raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events-db", required=True)
    parser.add_argument("--discord-db", required=True)
    parser.add_argument("--apply", action="store_true", help="write verified completions")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("scan", help="find explicit direct-reply completions")
    confirm = sub.add_parser("confirm", help="use one operator-reviewed direct reply")
    confirm.add_argument("commitment_id")
    confirm.add_argument("reply_message_id")
    args = parser.parse_args(argv)

    mode = "rw" if args.apply else "ro"
    events = sqlite3.connect(f"file:{args.events_db}?mode={mode}", uri=True)
    discord = sqlite3.connect(f"file:{args.discord_db}?mode=ro", uri=True)
    try:
        if args.action == "scan":
            selected = candidates(events, discord)
        else:
            item = confirmed_reply(events, discord, args.commitment_id,
                                   args.reply_message_id)
            if item is None:
                parser.error("reply provenance did not match an active commitment")
            selected = [item]
        applied = sum(apply_completion(events, item) for item in selected) if args.apply else 0
        print(json.dumps({"candidates": selected, "applied": applied}, sort_keys=True))
        return 0
    finally:
        discord.close()
        events.close()


if __name__ == "__main__":
    raise SystemExit(main())
