#!/usr/bin/env python3
"""Discord Knowledge — one-shot discovery listing (setup aid only).

Prints the guilds the Knowledge Bot is a member of and the channels it can
see, so the operator can copy exact ids into the allowlist. It exists purely
because writing an allowlist by hand needs ids, and ids are awkward to read
off the Discord client.

What it deliberately does NOT do (spec section 13):
  * it never fetches or prints message content;
  * it never writes to the source database or the search index;
  * it never prints the token, the credential path contents, or any secret;
  * it does not enable ingestion — the allowlist stays whatever it was.

Readability is probed with the permission bits Discord reports for the bot,
not by reading a message, so running this changes nothing on the server.

Exit codes: 0 ok | 1 runtime failure | 2 bad args | 78 credential/config.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import discord

from dk_adapter import discovery_row
from dk_config import ConfigError, load_config, load_token

EX_CONFIG = 78

# Channel types that can hold readable message history. Voice, category,
# directory and stage channels are listed but never proposed for ingestion.
HISTORY_TYPES = {0, 5, 10, 11, 12, 15, 16}


def _readable(channel, me) -> bool | None:
    perms = getattr(channel, "permissions_for", None)
    if perms is None or me is None:
        return None
    try:
        p = perms(me)
    except Exception:
        return None
    return bool(getattr(p, "view_channel", False) and getattr(p, "read_message_history", False))


def invite_url(application_id) -> str:
    """The minimal-permission invite link (spec section 9).

    The bitfield is computed from the library's own constants rather than
    hard-coded, so it cannot drift from what the permission actually is.
    """
    perms = discord.Permissions.none()
    perms.view_channel = True
    perms.read_message_history = True
    return (
        "https://discord.com/oauth2/authorize"
        f"?client_id={application_id}&scope=bot&permissions={perms.value}"
    )


class DiscoveryClient(discord.Client):
    def __init__(self, include_threads: bool, production_intents: bool = False):
        intents = discord.Intents.none()
        intents.guilds = True
        if production_intents:
            # Ask for exactly what the collector will ask for. If
            # MESSAGE_CONTENT has not been enabled in the Developer Portal,
            # the gateway refuses the connection and we learn it here rather
            # than after ingesting a guild's worth of empty message bodies.
            intents.guild_messages = True
            intents.message_content = True
        super().__init__(
            intents=intents,
            max_messages=None,
            chunk_guilds_at_startup=False,
            member_cache_flags=discord.MemberCacheFlags.none(),
        )
        self.include_threads = include_threads
        self.production_intents = production_intents
        self.rows: list = []
        self.guild_summary: list = []
        self.identity: dict = {}
        self.failure = None

    async def on_ready(self) -> None:
        # Reaching on_ready is itself the proof that the credential works:
        # a bad token raises LoginFailure before we get here.
        self.identity = {
            "bot_user": str(getattr(self.user, "name", "") or ""),
            "bot_user_id": str(getattr(self.user, "id", "") or ""),
            "application_id": str(getattr(self, "application_id", "") or ""),
            "guild_count": len(self.guilds),
            "intents_value": self.intents.value,
            "message_content_intent": "approved" if self.production_intents else "not tested",
        }
        try:
            for guild in self.guilds:
                me = guild.me
                channels = list(getattr(guild, "channels", []) or [])
                if self.include_threads:
                    channels += list(getattr(guild, "threads", []) or [])
                visible = 0
                for channel in channels:
                    ctype = getattr(getattr(channel, "type", None), "value", None)
                    row = discovery_row(guild, channel, _readable(channel, me))
                    row["can_hold_history"] = ctype in HISTORY_TYPES
                    if row["readable"] and row["can_hold_history"]:
                        visible += 1
                    self.rows.append(row)
                self.guild_summary.append(
                    {
                        "guild_id": str(guild.id),
                        "guild_name": guild.name,
                        # Useful when identifying the operator's own Discord
                        # user id for SELF/OTHER event classification, but it
                        # is a candidate to confirm, never an assumption.
                        "guild_owner_id": str(getattr(guild, "owner_id", "") or ""),
                        "channels_listed": len(channels),
                        "readable_history_channels": visible,
                    }
                )
        except Exception as exc:  # keep the shape of the failure, not its detail
            self.failure = f"{type(exc).__name__}: {str(exc)[:200]}"
        finally:
            await self.close()


NO_GUILD_HINT = """
The bot authenticated successfully but is not a member of any guild yet.

Nothing is wrong with the token — the invite step has not been completed.
In the Discord Developer Portal:

  OAuth2 -> URL Generator
    scopes      : bot
    permissions : View Channels, Read Message History   (nothing else)

Open the generated URL once per target guild and authorise it there. You need
Manage Server on the guild to accept the invite. Then re-run this command.
"""


def _render_text(guilds, rows, identity=None) -> str:
    out = []
    if identity:
        out += [
            "BOT",
            "---",
            "  authenticated : yes (gateway READY reached)",
            f"  bot user      : {identity.get('bot_user') or '?'}"
            f"  (id {identity.get('bot_user_id') or '?'})",
            f"  application id: {identity.get('application_id') or '?'}",
            f"  guilds joined : {identity.get('guild_count', 0)}",
            f"  intents asked : {identity.get('intents_value')}",
            f"  MESSAGE_CONTENT: {identity.get('message_content_intent')}",
            "",
        ]
        if identity.get("application_id"):
            out += [
                "INVITE URL (minimal permissions: View Channels + Read Message History)",
                "----------",
                "  " + invite_url(identity["application_id"]),
                "",
            ]
    out += ["GUILDS", "------"]
    if not guilds:
        out.append("  (none)")
    for g in guilds:
        out.append(
            f"  {g['guild_id']}  {g['guild_name']}  "
            f"(channels={g['channels_listed']}, readable_history={g['readable_history_channels']})"
        )
    out.append("")
    if not guilds:
        out.append(NO_GUILD_HINT.strip())
        return "\n".join(out)
    out.append("CHANNELS  (readable/history-capable first)")
    out.append("-----------------------------------------")
    ordered = sorted(
        rows,
        key=lambda r: (
            r["guild_id"],
            not (r["readable"] and r["can_hold_history"]),
            r["is_thread"],
            str(r["channel_name"] or ""),
        ),
    )
    for r in ordered:
        flag = "ok  " if (r["readable"] and r["can_hold_history"]) else "--  "
        kind = f"type={r['channel_type']}" + (" thread" if r["is_thread"] else "")
        parent = f" parent={r['parent_channel_id']}" if r["parent_channel_id"] else ""
        out.append(
            f"  {flag}{r['channel_id']}  {str(r['channel_name'] or '')[:40]:<40} "
            f"{kind}{parent}  guild={r['guild_id']}"
        )
    out.append("")
    out.append(
        "Copy the ids of the channels you want collected into the collector "
        "config under the matching guild. Nothing is collected until you do."
    )
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="List guilds and channels the Knowledge Bot can see (no content)"
    )
    ap.add_argument("--config", default=None)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument(
        "--include-threads", action="store_true", help="also list active threads"
    )
    ap.add_argument(
        "--production-intents",
        action="store_true",
        help="connect with the collector's intents to verify MESSAGE_CONTENT is enabled",
    )
    args = ap.parse_args(argv)

    try:
        cfg = load_config(args.config)
        token = load_token(cfg)
    except ConfigError as exc:
        print(f"DISCOVERY config error: {exc}", file=sys.stderr)
        return EX_CONFIG

    client = DiscoveryClient(args.include_threads, args.production_intents)
    try:
        asyncio.run(client.start(token))
    except discord.LoginFailure:
        print(
            "DISCOVERY authentication failed: the bot token was rejected.",
            file=sys.stderr,
        )
        return EX_CONFIG
    except discord.PrivilegedIntentsRequired:
        print(
            "DISCOVERY gateway refused the connection: the MESSAGE CONTENT "
            "privileged intent is not enabled for this application.\n"
            "Enable it in the Discord Developer Portal under\n"
            "  Bot -> Privileged Gateway Intents -> MESSAGE CONTENT INTENT\n"
            "Without it every message body arrives empty, which would look "
            "like a successful ingest of blank messages.",
            file=sys.stderr,
        )
        return EX_CONFIG
    except Exception as exc:
        print(f"DISCOVERY failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if client.failure:
        print(f"DISCOVERY partial failure: {client.failure}", file=sys.stderr)

    if args.json:
        print(
            json.dumps(
                {
                    "bot": client.identity,
                    "guilds": client.guild_summary,
                    "channels": client.rows,
                },
                ensure_ascii=False,
                indent=1,
            )
        )
    else:
        print(_render_text(client.guild_summary, client.rows, client.identity))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
