#!/usr/bin/env python3
"""Discord Knowledge — library object -> raw payload adapters.

The normalizer's input is the raw Discord message object (the JSON the
gateway sends and the REST history endpoint returns). Raw gateway events
already hand us that shape; the library's rich `Message` objects do not, so
this thin layer converts them back.

Keeping the conversion here means `dk_normalize` has exactly one input shape
to reason about, and the risky part — field mapping — is covered by the same
fixtures the raw path uses. Nothing here imports the Discord library: the
functions are duck-typed, so a stub object exercises them in tests.
"""

from __future__ import annotations


def _enum_value(value, default=None):
    """Discord library enums expose `.value`; raw payloads give plain ints."""
    if value is None:
        return default
    inner = getattr(value, "value", value)
    try:
        return int(inner)
    except (TypeError, ValueError):
        return default


def _iso(dt):
    return dt.isoformat() if dt is not None else None


def _user_dict(user):
    if user is None:
        return None
    return {
        "id": str(getattr(user, "id", "") or ""),
        "username": getattr(user, "name", None),
        "global_name": getattr(user, "global_name", None),
        "bot": bool(getattr(user, "bot", False)),
    }


def payload_from_message(msg) -> dict:
    """`discord.Message` -> raw message payload dict."""
    ref = getattr(msg, "reference", None)
    reference = None
    if ref is not None and getattr(ref, "message_id", None):
        reference = {
            "message_id": str(ref.message_id),
            "channel_id": str(getattr(ref, "channel_id", "") or ""),
        }

    attachments = []
    for a in getattr(msg, "attachments", None) or []:
        attachments.append(
            {
                "id": str(getattr(a, "id", "") or ""),
                "filename": getattr(a, "filename", None),
                "content_type": getattr(a, "content_type", None),
                "size": getattr(a, "size", None),
            }
        )

    embeds = []
    for e in getattr(msg, "embeds", None) or []:
        to_dict = getattr(e, "to_dict", None)
        embeds.append(to_dict() if callable(to_dict) else {})

    return {
        "id": str(msg.id),
        "channel_id": str(getattr(getattr(msg, "channel", None), "id", "") or ""),
        "author": _user_dict(getattr(msg, "author", None)),
        "content": getattr(msg, "content", None),
        "timestamp": _iso(getattr(msg, "created_at", None)),
        "edited_timestamp": _iso(getattr(msg, "edited_at", None)),
        "type": _enum_value(getattr(msg, "type", None), 0),
        "webhook_id": (
            str(msg.webhook_id) if getattr(msg, "webhook_id", None) else None
        ),
        "attachments": attachments,
        "embeds": embeds,
        "mentions": [_user_dict(u) for u in (getattr(msg, "mentions", None) or [])],
        "message_reference": reference,
    }


def channel_context(channel) -> dict:
    """`discord.abc.GuildChannel` / `discord.Thread` -> collector registry entry.

    A thread is a channel in its own right: it has its own id, and its parent
    id is what the allowlist is usually written against.
    """
    parent = getattr(channel, "parent", None)
    parent_id = getattr(channel, "parent_id", None) or getattr(parent, "id", None)
    ctype = _enum_value(getattr(channel, "type", None))
    name = getattr(channel, "name", None)
    is_thread = ctype in (10, 11, 12)
    return {
        "channel_id": str(getattr(channel, "id", "") or ""),
        "channel_name": name,
        "channel_type": ctype,
        "parent_channel_id": str(parent_id) if parent_id else None,
        "thread_name": name if is_thread else None,
        "is_thread": is_thread,
    }


def guild_context(guild) -> dict:
    return {
        "guild_id": str(getattr(guild, "id", "") or ""),
        "guild_name": getattr(guild, "name", None),
    }


def discovery_row(guild, channel, readable) -> dict:
    """One line of the discovery listing (spec section 13).

    Identity and shape only — never message content, never a secret.
    """
    ctx = channel_context(channel)
    return {
        "guild_id": str(getattr(guild, "id", "") or ""),
        "guild_name": getattr(guild, "name", None),
        "channel_id": ctx["channel_id"],
        "channel_name": ctx["channel_name"],
        "channel_type": ctx["channel_type"],
        "parent_channel_id": ctx["parent_channel_id"],
        "is_thread": ctx["is_thread"],
        "readable": readable,
    }
