#!/usr/bin/env python3
"""Discord Knowledge — payload normalization (pure functions, no I/O).

Everything here turns a raw Discord message object (the JSON shape the
gateway delivers and the REST history endpoint returns) into the canonical
record stored in the source database. It performs no network access, opens
no database and reads no secret, so it is fully unit-testable against
recorded payload fixtures.

Two properties matter most:

* **Raw content is preserved verbatim.** `content` is exactly what Discord
  sent. A separate `content_normalized` field carries the mention-resolved
  rendering; the two are never conflated (spec section 30).

* **Partial payloads never null out known data.** `MESSAGE_UPDATE` may carry
  only the fields that changed. `normalize_message(..., partial=True)`
  returns *only* the keys the payload actually contained, so the caller can
  apply a narrow UPDATE instead of overwriting with NULLs (section 37).

All content from Discord is untrusted external data. Nothing in this module
interprets it as an instruction; it is measured, bounded and stored.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

from dk_schema import source_id_for

# Discord epoch: snowflake ids embed their own creation time, which is the
# only timestamp available on a raw delete payload.
DISCORD_EPOCH_MS = 1420070400000

MESSAGE_TYPES = {
    0: "default",
    1: "recipient_add",
    2: "recipient_remove",
    3: "call",
    4: "channel_name_change",
    5: "channel_icon_change",
    6: "channel_pinned_message",
    7: "user_join",
    8: "guild_boost",
    9: "guild_boost_tier_1",
    10: "guild_boost_tier_2",
    11: "guild_boost_tier_3",
    12: "channel_follow_add",
    14: "guild_discovery_disqualified",
    15: "guild_discovery_requalified",
    16: "guild_discovery_grace_period_initial_warning",
    17: "guild_discovery_grace_period_final_warning",
    18: "thread_created",
    19: "reply",
    20: "chat_input_command",
    21: "thread_starter_message",
    22: "guild_invite_reminder",
    23: "context_menu_command",
    24: "auto_moderation_action",
    25: "role_subscription_purchase",
    26: "interaction_premium_upsell",
    27: "stage_start",
    28: "stage_end",
    29: "stage_speaker",
    31: "stage_topic",
    32: "guild_application_premium_subscription",
    36: "guild_incident_alert_mode_enabled",
    37: "guild_incident_alert_mode_disabled",
    38: "guild_incident_report_raid",
    39: "guild_incident_report_false_alarm",
    46: "poll_result",
}

CHANNEL_TYPES = {
    0: "guild_text",
    1: "dm",
    2: "guild_voice",
    3: "group_dm",
    4: "guild_category",
    5: "guild_announcement",
    10: "announcement_thread",
    11: "public_thread",
    12: "private_thread",
    13: "guild_stage_voice",
    14: "guild_directory",
    15: "guild_forum",
    16: "guild_media",
}

THREAD_CHANNEL_TYPES = frozenset({"announcement_thread", "public_thread", "private_thread"})

DEFAULT_LIMITS = {
    "content_max_chars": 8000,
    "embed_title_max_chars": 300,
    "embed_description_max_chars": 1000,
    "embed_field_max_chars": 300,
    "embed_field_max_count": 10,
    "embed_max_count": 5,
    "attachment_max_count": 10,
}

_USER_MENTION_RE = re.compile(r"<@!?(\d{15,25})>")
_CHANNEL_MENTION_RE = re.compile(r"<#(\d{15,25})>")
_EMOJI_RE = re.compile(r"<a?:([A-Za-z0-9_]{2,32}):\d{15,25}>")


def message_type_name(value) -> str:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return "default"
    return MESSAGE_TYPES.get(n, f"type_{n}")


def channel_type_name(value) -> str:
    if isinstance(value, str) and value:
        return value
    try:
        n = int(value)
    except (TypeError, ValueError):
        return "unknown"
    return CHANNEL_TYPES.get(n, f"type_{n}")


def is_thread_type(channel_type) -> bool:
    return channel_type_name(channel_type) in THREAD_CHANNEL_TYPES


def snowflake_ms(snowflake) -> int | None:
    """Creation time in epoch milliseconds, decoded from the id itself."""
    try:
        n = int(str(snowflake))
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return (n >> 22) + DISCORD_EPOCH_MS


def iso_utc(value) -> str | None:
    """Normalize a Discord timestamp to UTC ISO-8601 (section 29)."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def ms_from_iso(value) -> int | None:
    iso = iso_utc(value)
    if iso is None:
        return None
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def bound_text(value, cap: int) -> str | None:
    """Cap a string at `cap` characters, marking the truncation visibly."""
    if value is None:
        return None
    s = str(value)
    if cap <= 0:
        return ""
    if len(s) <= cap:
        return s
    return s[: cap - 1] + "…"


def content_sha256(value) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def normalize_attachments(items, limits) -> list:
    """Metadata only. Binaries are never downloaded and signed/temporary
    CDN URLs are never persisted (spec section 31)."""
    out = []
    if not isinstance(items, list):
        return out
    cap = int(limits.get("attachment_max_count", 10))
    for a in items[:cap]:
        if not isinstance(a, dict):
            continue
        size = a.get("size")
        out.append(
            {
                "attachment_id": str(a.get("id")) if a.get("id") is not None else None,
                "filename": bound_text(a.get("filename"), 300),
                "content_type": bound_text(a.get("content_type"), 120),
                "size": int(size) if isinstance(size, int) else None,
            }
        )
    return out


def normalize_embeds(items, limits) -> list:
    """Bounded text metadata only — no images, no urls, no full embed JSON
    (spec section 32). Informational bot embeds keep their readable text so
    they stay searchable; everything else is dropped."""
    out = []
    if not isinstance(items, list):
        return out
    cap = int(limits.get("embed_max_count", 5))
    t_cap = int(limits.get("embed_title_max_chars", 300))
    d_cap = int(limits.get("embed_description_max_chars", 1000))
    f_cap = int(limits.get("embed_field_max_chars", 300))
    f_count = int(limits.get("embed_field_max_count", 10))
    for e in items[:cap]:
        if not isinstance(e, dict):
            continue
        fields = []
        raw_fields = e.get("fields")
        if isinstance(raw_fields, list):
            for f in raw_fields[:f_count]:
                if not isinstance(f, dict):
                    continue
                fields.append(
                    {
                        "name": bound_text(f.get("name"), f_cap),
                        "value": bound_text(f.get("value"), f_cap),
                    }
                )
        author = e.get("author") if isinstance(e.get("author"), dict) else {}
        footer = e.get("footer") if isinstance(e.get("footer"), dict) else {}
        entry = {
            "type": bound_text(e.get("type"), 40),
            "title": bound_text(e.get("title"), t_cap),
            "description": bound_text(e.get("description"), d_cap),
            "author_name": bound_text(author.get("name"), t_cap),
            "footer_text": bound_text(footer.get("text"), t_cap),
            "fields": fields,
        }
        if any(v for k, v in entry.items() if k != "type"):
            out.append(entry)
    return out


def embed_search_text(embeds) -> str:
    """Flatten bounded embed text so bot-generated notices remain findable."""
    parts = []
    for e in embeds or []:
        for key in ("title", "description", "author_name", "footer_text"):
            if e.get(key):
                parts.append(str(e[key]))
        for f in e.get("fields") or []:
            if f.get("name"):
                parts.append(str(f["name"]))
            if f.get("value"):
                parts.append(str(f["value"]))
    return "\n".join(parts)


def normalize_content(content, data=None, channel_names=None) -> str | None:
    """Human-readable rendering of mentions and custom emoji.

    Stored beside — never instead of — the raw content. User names come from
    the payload's own `mentions` array, so this needs no member cache and no
    GUILD_MEMBERS intent.
    """
    if content is None:
        return None
    text = str(content)
    users = {}
    for u in (data or {}).get("mentions") or []:
        if isinstance(u, dict) and u.get("id"):
            users[str(u["id"])] = str(u.get("global_name") or u.get("username") or u["id"])
    names = dict(channel_names or {})

    text = _USER_MENTION_RE.sub(lambda m: "@" + users.get(m.group(1), m.group(1)), text)
    text = _CHANNEL_MENTION_RE.sub(lambda m: "#" + names.get(m.group(1), m.group(1)), text)
    text = _EMOJI_RE.sub(lambda m: ":" + m.group(1) + ":", text)
    return text


def _author_fields(author) -> dict:
    if not isinstance(author, dict):
        return {
            "author_id": None,
            "author_name": None,
            "author_global_name": None,
            "author_is_bot": 0,
        }
    return {
        "author_id": str(author["id"]) if author.get("id") is not None else None,
        "author_name": bound_text(author.get("username"), 200),
        "author_global_name": bound_text(author.get("global_name"), 200),
        "author_is_bot": 1 if author.get("bot") else 0,
    }


def normalize_message(
    data: dict,
    *,
    guild_id,
    guild_name=None,
    channel=None,
    limits=None,
    ingest_source: str = "gateway",
    partial: bool = False,
    channel_names=None,
) -> dict:
    """Raw Discord message object -> canonical source-DB record.

    `channel` describes where the message lives and comes from the collector's
    channel registry, not from the payload:
        {"channel_id", "channel_name", "channel_type",
         "parent_channel_id", "thread_name"}

    With `partial=True` only the keys the payload actually carried are
    returned (plus the identity keys), so a partial MESSAGE_UPDATE can be
    applied without erasing fields Discord simply did not resend.
    """
    limits = {**DEFAULT_LIMITS, **(limits or {})}
    channel = dict(channel or {})

    message_id = str(data.get("id")) if data.get("id") is not None else None
    channel_id = str(
        channel.get("channel_id") or data.get("channel_id") or ""
    ) or None
    gid = str(guild_id) if guild_id is not None else None
    if not (message_id and channel_id and gid):
        raise ValueError("message_id, channel_id and guild_id are all required")

    ctype = channel_type_name(channel.get("channel_type"))
    thread = is_thread_type(ctype)

    rec = {
        "source_id": source_id_for(gid, channel_id, message_id),
        "message_id": message_id,
        "guild_id": gid,
        "channel_id": channel_id,
    }

    # Identity/context fields always come from the collector's registry, so
    # they are safe to set even for a partial payload.
    rec["guild_name"] = bound_text(guild_name, 200)
    rec["channel_name"] = bound_text(channel.get("channel_name"), 200)
    rec["channel_type"] = ctype
    rec["thread_id"] = channel_id if thread else None
    rec["thread_name"] = bound_text(channel.get("thread_name") or (channel.get("channel_name") if thread else None), 200)
    rec["parent_channel_id"] = (
        str(channel["parent_channel_id"]) if channel.get("parent_channel_id") else None
    )

    has = data.__contains__

    if has("author") or not partial:
        rec.update(_author_fields(data.get("author")))
    if has("webhook_id") or not partial:
        rec["webhook_id"] = (
            str(data["webhook_id"]) if data.get("webhook_id") is not None else None
        )

    # created_at is immutable; a raw payload that omits it still yields the
    # correct value from the snowflake.
    created_iso = iso_utc(data.get("timestamp")) if has("timestamp") else None
    created_ms = ms_from_iso(data.get("timestamp")) if has("timestamp") else None
    if created_ms is None:
        created_ms = snowflake_ms(message_id)
        if created_iso is None and created_ms is not None:
            created_iso = datetime.fromtimestamp(
                created_ms / 1000.0, tz=timezone.utc
            ).isoformat()
    rec["created_at"] = created_iso
    rec["created_ts"] = created_ms

    if has("edited_timestamp") or not partial:
        rec["edited_at"] = iso_utc(data.get("edited_timestamp"))

    if has("content") or not partial:
        raw = data.get("content")
        raw = bound_text(raw, limits["content_max_chars"]) if raw is not None else None
        rec["content"] = raw
        rec["content_normalized"] = normalize_content(raw, data, channel_names)
        rec["content_hash"] = content_sha256(raw)

    if has("type") or not partial:
        rec["message_type"] = message_type_name(data.get("type"))

    if has("message_reference") or has("referenced_message") or not partial:
        ref = data.get("message_reference") or {}
        ref_id = ref.get("message_id") if isinstance(ref, dict) else None
        if ref_id is None and isinstance(data.get("referenced_message"), dict):
            ref_id = data["referenced_message"].get("id")
        rec["reply_to_message_id"] = str(ref_id) if ref_id is not None else None

    if has("attachments") or not partial:
        atts = normalize_attachments(data.get("attachments"), limits)
        rec["attachments_json"] = json.dumps(atts, ensure_ascii=False) if atts else None

    if has("embeds") or not partial:
        embeds = normalize_embeds(data.get("embeds"), limits)
        rec["embeds_json"] = json.dumps(embeds, ensure_ascii=False) if embeds else None

    if not partial:
        rec["deleted_at"] = None
        rec["ingest_source"] = ingest_source

    return rec


def tombstone_record(guild_id, channel_id, message_id, *, deleted_at=None) -> dict:
    """Minimal record for a delete event about a message we may never have
    seen. Body is never fabricated; only identity and timing survive."""
    gid, cid, mid = str(guild_id), str(channel_id), str(message_id)
    created_ms = snowflake_ms(mid)
    created_iso = (
        datetime.fromtimestamp(created_ms / 1000.0, tz=timezone.utc).isoformat()
        if created_ms is not None
        else None
    )
    return {
        "source_id": source_id_for(gid, cid, mid),
        "message_id": mid,
        "guild_id": gid,
        "channel_id": cid,
        "created_at": created_iso,
        "created_ts": created_ms,
        "deleted_at": deleted_at or now_utc_iso(),
    }


def edit_generation(record: dict) -> str:
    """Ordering key for 'which version of this message is newer'.

    Discord does not expose a revision counter, but `edited_timestamp` is
    monotonic per message and absent on the original. Comparing it as a
    string is correct because both values are normalized UTC ISO-8601.
    An out-of-order backfill page therefore cannot overwrite a newer edit
    already delivered by the gateway (spec section 38).
    """
    return record.get("edited_at") or ""


def search_blob(record: dict) -> str:
    """Text a search index should see for this record: normalized content
    plus bounded embed text."""
    parts = []
    body = record.get("content_normalized") or record.get("content")
    if body:
        parts.append(str(body))
    raw_embeds = record.get("embeds_json")
    if raw_embeds:
        try:
            parts.append(embed_search_text(json.loads(raw_embeds)))
        except (ValueError, TypeError):
            pass
    return "\n".join(p for p in parts if p)
