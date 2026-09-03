#!/usr/bin/env python3
"""Discord Knowledge — read-only volume measurement (setup aid only).

Answers the question the backfill bounds actually depend on: how much traffic
does each channel really carry, and how much of it is a person talking?
Spec sections 46/47 require those bounds to come from the real server volume
rather than from a guessed number.

What it does NOT do:
  * it never prints, logs or stores any message body — only counts, ratios
    and timestamps leave this process;
  * it never writes to the source database or the search index;
  * it never writes to Discord.

It reads recent messages through the REST history endpoint, which is the same
call the backfill makes, so the rate-limit behaviour observed here is the
behaviour the real backfill will see.

Exit codes: 0 ok | 1 runtime failure | 2 bad args | 78 credential/config.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone

import discord

from dk_config import ConfigError, load_config, load_token

EX_CONFIG = 78

HISTORY_TYPES = {0, 5, 10, 11, 12, 15, 16}
PROBE_CONCURRENCY = 4


def _readable(channel, me) -> bool:
    perms = getattr(channel, "permissions_for", None)
    if perms is None or me is None:
        return False
    try:
        p = perms(me)
    except Exception:
        return False
    return bool(p.view_channel and p.read_message_history)


class VolumeProbe(discord.Client):
    def __init__(self, limit: int, days: int, guild_filter=None):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.message_content = True
        super().__init__(
            intents=intents,
            max_messages=None,
            chunk_guilds_at_startup=False,
            member_cache_flags=discord.MemberCacheFlags.none(),
        )
        self.limit = limit
        self.days = days
        self.guild_filter = set(guild_filter or [])
        self.rows: list = []
        self.errors: list = []
        self.rate_limit_hits = 0

    async def _probe(self, guild, channel, sem):
        async with sem:
            cutoff = datetime.now(timezone.utc) - timedelta(days=self.days)
            row = {
                "guild_id": str(guild.id),
                "guild_name": guild.name,
                "channel_id": str(channel.id),
                "channel_name": getattr(channel, "name", None),
                "channel_type": getattr(getattr(channel, "type", None), "value", None),
                "fetched": 0,
                "in_window": 0,
                "human": 0,
                "bot": 0,
                "webhook": 0,
                "with_content": 0,
                "empty_content": 0,
                "with_attachment": 0,
                "with_embed": 0,
                "newest_at": None,
                "oldest_at": None,
                "span_days": None,
                "msgs_per_day": None,
                "error": None,
            }
            try:
                msgs = [m async for m in channel.history(limit=self.limit)]
            except discord.Forbidden:
                row["error"] = "forbidden"
                self.rows.append(row)
                return
            except discord.HTTPException as exc:
                if exc.status == 429:
                    self.rate_limit_hits += 1
                row["error"] = f"http {exc.status}"
                self.rows.append(row)
                return
            except Exception as exc:
                row["error"] = type(exc).__name__
                self.rows.append(row)
                return

            row["fetched"] = len(msgs)
            for m in msgs:
                created = m.created_at
                if created and created >= cutoff:
                    row["in_window"] += 1
                if getattr(m, "webhook_id", None):
                    row["webhook"] += 1
                elif getattr(m.author, "bot", False):
                    row["bot"] += 1
                else:
                    row["human"] += 1
                # Length only. The body itself never leaves this loop.
                if (m.content or "").strip():
                    row["with_content"] += 1
                else:
                    row["empty_content"] += 1
                if m.attachments:
                    row["with_attachment"] += 1
                if m.embeds:
                    row["with_embed"] += 1

            if msgs:
                newest = msgs[0].created_at
                oldest = msgs[-1].created_at
                row["newest_at"] = newest.isoformat() if newest else None
                row["oldest_at"] = oldest.isoformat() if oldest else None
                if newest and oldest:
                    span = (newest - oldest).total_seconds() / 86400.0
                    row["span_days"] = round(span, 2)
                    # Only meaningful when the page actually filled: a short
                    # page means the channel simply has fewer messages.
                    if span > 0 and len(msgs) >= self.limit:
                        row["msgs_per_day"] = round(len(msgs) / span, 2)
                    elif span > 0:
                        row["msgs_per_day"] = round(len(msgs) / max(span, 1.0), 2)
            self.rows.append(row)

    async def on_ready(self) -> None:
        try:
            sem = asyncio.Semaphore(PROBE_CONCURRENCY)
            tasks = []
            for guild in self.guilds:
                if self.guild_filter and str(guild.id) not in self.guild_filter:
                    continue
                me = guild.me
                for channel in list(getattr(guild, "channels", []) or []):
                    ctype = getattr(getattr(channel, "type", None), "value", None)
                    if ctype not in HISTORY_TYPES:
                        continue
                    if not _readable(channel, me):
                        continue
                    if not hasattr(channel, "history"):
                        continue
                    tasks.append(self._probe(guild, channel, sem))
            await asyncio.gather(*tasks)
        except Exception as exc:
            self.errors.append(f"{type(exc).__name__}: {str(exc)[:200]}")
        finally:
            await self.close()


def _render(rows, days, limit) -> str:
    out = []
    by_guild = {}
    for r in rows:
        by_guild.setdefault((r["guild_id"], r["guild_name"]), []).append(r)

    for (gid, gname), items in sorted(by_guild.items(), key=lambda kv: kv[0][1] or ""):
        live = [r for r in items if not r["error"]]
        total_window = sum(r["in_window"] for r in live)
        total_human = sum(r["human"] for r in live)
        total_bot = sum(r["bot"] + r["webhook"] for r in live)
        total_fetched = sum(r["fetched"] for r in live)
        with_content = sum(r["with_content"] for r in live)
        out.append(f"=== {gname}   guild_id {gid}")
        out.append(
            f"    probed {len(live)} channels, {total_fetched} messages "
            f"(most recent {limit} per channel)"
        )
        out.append(
            f"    last {days}d: {total_window} messages | "
            f"human {total_human} / bot+webhook {total_bot} | "
            f"non-empty body {with_content}/{total_fetched}"
        )
        out.append("")
        out.append(
            "    msgs/day  win  human  bot  body   channel"
        )
        for r in sorted(
            live, key=lambda x: (-(x["msgs_per_day"] or 0), -(x["in_window"] or 0))
        ):
            rate = f"{r['msgs_per_day']:>8.1f}" if r["msgs_per_day"] else "       -"
            out.append(
                f"    {rate}  {r['in_window']:>3}  {r['human']:>5}  "
                f"{r['bot'] + r['webhook']:>3}  {r['with_content']:>4}   "
                f"{r['channel_id']}  {r['channel_name']}"
            )
        bad = [r for r in items if r["error"]]
        if bad:
            out.append(f"    -- errors ({len(bad)}): "
                       + ", ".join(f"{r['channel_name']}:{r['error']}" for r in bad[:10]))
        out.append("")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Measure per-channel message volume (no content is shown or stored)"
    )
    ap.add_argument("--config", default=None)
    ap.add_argument("--limit", type=int, default=100, help="messages fetched per channel")
    ap.add_argument("--days", type=int, default=14, help="window for the in-window count")
    ap.add_argument("--guild", action="append", help="restrict to this guild id (repeatable)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if not (1 <= args.limit <= 100):
        print("--limit must be 1..100 (Discord page size)", file=sys.stderr)
        return 2

    try:
        cfg = load_config(args.config)
        token = load_token(cfg)
    except ConfigError as exc:
        print(f"VOLUME_PROBE config error: {exc}", file=sys.stderr)
        return EX_CONFIG

    client = VolumeProbe(args.limit, args.days, args.guild)
    try:
        asyncio.run(client.start(token))
    except discord.LoginFailure:
        print("VOLUME_PROBE authentication failed.", file=sys.stderr)
        return EX_CONFIG
    except discord.PrivilegedIntentsRequired:
        print(
            "VOLUME_PROBE gateway refused: MESSAGE CONTENT intent not enabled.",
            file=sys.stderr,
        )
        return EX_CONFIG
    except Exception as exc:
        print(f"VOLUME_PROBE failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    for err in client.errors:
        print(f"VOLUME_PROBE partial failure: {err}", file=sys.stderr)

    if args.json:
        print(json.dumps({"rows": client.rows, "rate_limit_hits": client.rate_limit_hits},
                         ensure_ascii=False, indent=1))
    else:
        print(_render(client.rows, args.days, args.limit))
        print(f"rate limit responses observed: {client.rate_limit_hits}")
        print("No message content was printed or stored by this command.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
