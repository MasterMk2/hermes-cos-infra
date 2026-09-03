#!/usr/bin/env python3
"""Discord Knowledge Collector — read-only gateway ingestion service.

Ears, not mouth. This process connects to the Discord gateway with a
dedicated Knowledge Bot credential and writes normalized messages into the
source database. It has no tool that sends, edits, deletes or reacts, it
never touches the assistant's own data or credentials, and it opens no
listening socket (the gateway is an outbound WebSocket).

Shape of the hot path (spec sections 84, 86, 87):

    gateway event -> allowlist check -> normalize -> bounded queue -> return

A single writer task drains that queue and applies batches to SQLite in a
worker thread, so neither disk I/O nor a slow batch can stall the gateway
heartbeat. No language model is ever called from this process; event
classification is a separate, scheduled batch job.

Exit codes:
    0   clean shutdown
    1   runtime failure
    2   bad arguments
    78  configuration or credential problem a restart cannot fix
        (bad token, MESSAGE_CONTENT not enabled, empty allowlist).
        The unit file sets RestartPreventExitStatus=78 so this stops the
        service rather than looping IDENTIFY against Discord.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

import discord

import dk_store
from dk_adapter import channel_context, payload_from_message
from dk_backfill import backfill_all
from dk_config import ConfigError, load_config, load_token
from dk_normalize import normalize_message, now_utc_iso
from dk_schema import connect, init_source_db, parse_source_id

EX_CONFIG = 78

QUEUE_MAXSIZE = 5000
WRITE_BATCH = 200
STATUS_INTERVAL_S = 30
FETCH_CONCURRENCY = 4
# How long a SIGTERM waits for the writer to finish already-queued work.
# systemd's default TimeoutStopSec is 90s, so this stays well inside it.
SHUTDOWN_DRAIN_S = 20

HEALTH_STARTING = "STARTING"
HEALTH_CONNECTED = "CONNECTED"
HEALTH_DEGRADED = "DEGRADED"
HEALTH_BACKFILLING = "BACKFILLING"
HEALTH_DISCONNECTED = "DISCONNECTED"
HEALTH_AUTH_FAILED = "AUTH_FAILED"
HEALTH_STOPPED = "STOPPED"

log = logging.getLogger("discord-knowledge")


def build_intents() -> discord.Intents:
    """The minimum that read-only message knowledge requires.

    GUILDS + GUILD_MESSAGES + MESSAGE_CONTENT and nothing else. Members,
    presences, typing, DMs, reactions, moderation and voice are all left off
    explicitly rather than by default (spec sections 7, 35).
    """
    intents = discord.Intents.none()
    intents.guilds = True
    intents.guild_messages = True
    intents.message_content = True
    # Explicit negation: these must never be requested.
    intents.members = False
    intents.presences = False
    intents.typing = False
    intents.dm_messages = False
    intents.guild_reactions = False
    intents.dm_reactions = False
    intents.voice_states = False
    intents.auto_moderation_configuration = False
    intents.auto_moderation_execution = False
    return intents


def setup_logging(log_dir, verbose=False) -> None:
    """Logs carry counts, ids and timings. Never a token, never a body."""
    handlers = [logging.StreamHandler(sys.stdout)]
    file_error = None
    try:
        d = Path(log_dir)
        d.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(d / "collector.log", encoding="utf-8"))
    except OSError as exc:
        # Degrading to stdout only is correct — a log we cannot open must not
        # stop ingestion. Doing it SILENTLY is not: the file log was the only
        # collector history that survives a reboot, and a stale-owner
        # collector.log left it disabled for hours without a word.
        file_error = f"{type(exc).__name__}: {exc}"
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
        force=True,
    )
    # The library's own debug logging can echo payloads; keep it at WARNING.
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)
    logging.getLogger("discord.client").setLevel(logging.WARNING)
    if file_error:
        log.warning(
            "file logging disabled, stdout only: cannot open %s/collector.log "
            "(%s). Collector history will not survive a reboot until this is "
            "fixed — check that the file is owned by the service user.",
            log_dir,
            file_error,
        )


class Collector(discord.Client):
    def __init__(self, cfg, con, **kwargs):
        super().__init__(
            intents=build_intents(),
            max_messages=None,  # raw events are used; no message cache needed
            chunk_guilds_at_startup=False,
            member_cache_flags=discord.MemberCacheFlags.none(),
            **kwargs,
        )
        self.cfg = cfg
        self.con = con
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self._writer_task = None
        self._status_task = None
        self._backfill_task = None
        self._fetch_sem = asyncio.Semaphore(FETCH_CONCURRENCY)
        self._parents: dict[str, str | None] = {}
        self._guild_names: dict[str, str] = {}
        self._channel_ctx: dict[str, dict] = {}
        self._health = HEALTH_STARTING
        self._counters = {
            "messages_created": 0,
            "messages_edited": 0,
            "messages_deleted": 0,
            "bulk_deletes": 0,
            "backfill_fetched": 0,
            "rate_limit_hits": 0,
            "session_resumed_count": 0,
            "full_reconnect_count": 0,
            "queue_high_water": 0,
            "dropped_out_of_scope": 0,
        }
        self._last = {
            "last_ready_at": "",
            "last_event_at": "",
            "last_successful_ingest_at": "",
            "last_error_summary": "",
            "last_disconnect_reason": "",
        }

    # ------------------------------------------------------------ plumbing

    async def submit(self, item: dict) -> None:
        """Hand work to the writer. Blocks when the queue is full, which
        applies backpressure instead of growing without bound or dropping
        data silently (spec section 87)."""
        await self.queue.put(item)
        depth = self.queue.qsize()
        if depth > self._counters["queue_high_water"]:
            self._counters["queue_high_water"] = depth
        if depth >= int(self.cfg.limits.get("queue_warn_depth", 5000)) // 2:
            log.warning("write queue depth=%d", depth)

    def _set_health(self, state, reason=None) -> None:
        self._health = state
        if reason:
            self._last["last_error_summary"] = str(reason)[:300]
        log.info("health=%s%s", state, f" reason={reason}" if reason else "")

    def _allowed(self, guild_id, channel_id) -> bool:
        parent = self._parents.get(str(channel_id))
        return self.cfg.channel_allowed(guild_id, channel_id, parent)

    def _ctx_for(self, channel_id, guild_id):
        ctx = self._channel_ctx.get(str(channel_id))
        if ctx:
            return ctx
        return {
            "channel_id": str(channel_id),
            "channel_name": None,
            "channel_type": None,
            "parent_channel_id": self._parents.get(str(channel_id)),
            "thread_name": None,
        }

    def _remember_channel(self, channel, guild_id=None) -> dict:
        ctx = channel_context(channel)
        self._channel_ctx[ctx["channel_id"]] = ctx
        self._parents[ctx["channel_id"]] = ctx["parent_channel_id"]
        gid = str(guild_id or getattr(getattr(channel, "guild", None), "id", "") or "")
        if gid:
            self._guild_names.setdefault(
                gid, getattr(getattr(channel, "guild", None), "name", None) or ""
            )
        return ctx

    # ------------------------------------------------------- writer thread

    def _apply_batch(self, items) -> dict:
        """Runs in a worker thread. One transaction per batch."""
        counts = {"created": 0, "edited": 0, "deleted": 0, "bulk": 0, "backfill": 0}
        con = self.con
        try:
            for item in items:
                kind = item.get("kind")
                if kind in ("create", "update"):
                    rec = item["record"]
                    dk_store.upsert_guild(con, rec["guild_id"], rec.get("guild_name"))
                    ctx = item.get("channel_ctx") or {}
                    dk_store.upsert_channel(
                        con,
                        rec["channel_id"],
                        rec["guild_id"],
                        channel_name=ctx.get("channel_name"),
                        channel_type=rec.get("channel_type"),
                        parent_channel_id=ctx.get("parent_channel_id"),
                        is_thread=bool(ctx.get("is_thread")),
                        readable=True,
                    )
                    action = dk_store.apply_message(
                        con,
                        rec,
                        partial=bool(item.get("partial")),
                        ingest_source=item.get("ingest_source", "gateway"),
                    )
                    if kind == "create" and action == "inserted":
                        counts["created"] += 1
                    elif action in ("updated", "inserted"):
                        counts["edited"] += 1
                elif kind == "delete":
                    dk_store.apply_delete(
                        con, item["guild_id"], item["channel_id"], item["message_id"]
                    )
                    counts["deleted"] += 1
                elif kind == "bulk_delete":
                    res = dk_store.apply_bulk_delete(
                        con, item["guild_id"], item["channel_id"], item["message_ids"]
                    )
                    counts["deleted"] += sum(res.values())
                    counts["bulk"] += 1
                elif kind == "backfill_batch":
                    ctx = item.get("channel_ctx") or {}
                    # Register the channel even when the page was empty: a
                    # thread whose whole history predates the window would
                    # otherwise leave a backfill_state row that nothing can
                    # resolve to a name, a parent, or a scope justification.
                    dk_store.upsert_guild(con, item["guild_id"], item.get("guild_name"))
                    if ctx.get("channel_id"):
                        dk_store.upsert_channel(
                            con,
                            item["channel_id"],
                            item["guild_id"],
                            channel_name=ctx.get("channel_name"),
                            channel_type=ctx.get("channel_type"),
                            parent_channel_id=ctx.get("parent_channel_id"),
                            is_thread=bool(ctx.get("is_thread")),
                            readable=True,
                        )
                    added = 0
                    for rec in item["records"]:
                        if dk_store.apply_message(
                            con, rec, ingest_source="backfill"
                        ) == "inserted":
                            added += 1
                    dk_store.save_backfill_state(
                        con,
                        item["channel_id"],
                        item["guild_id"],
                        last_cursor=item.get("cursor"),
                        oldest_created_ts=item.get("oldest_created_ts"),
                        added=added,
                    )
                    counts["backfill"] += added
                elif kind == "backfill_state":
                    dk_store.save_backfill_state(
                        con,
                        item["channel_id"],
                        item["guild_id"],
                        last_cursor=item.get("cursor"),
                        complete=item.get("complete"),
                        last_error=item.get("error"),
                    )
                elif kind == "channel_unreadable":
                    dk_store.mark_channel_unreadable(
                        con, item["channel_id"], item["guild_id"], item["reason"]
                    )
                elif kind == "channel_meta":
                    dk_store.upsert_guild(con, item["guild_id"], item.get("guild_name"))
                    ctx = item["channel_ctx"]
                    dk_store.upsert_channel(
                        con,
                        ctx["channel_id"],
                        item["guild_id"],
                        channel_name=ctx.get("channel_name"),
                        channel_type=ctx.get("channel_type"),
                        parent_channel_id=ctx.get("parent_channel_id"),
                        is_thread=bool(ctx.get("is_thread")),
                        readable=item.get("readable"),
                        last_error=item.get("reason"),
                    )
                elif kind == "rate_limit":
                    dk_store.bump_metric(con, "rate_limit_hits")
            con.commit()
        except Exception:
            con.rollback()
            raise
        return counts

    async def _writer_loop(self) -> None:
        while True:
            first = await self.queue.get()
            items = [first]
            while len(items) < WRITE_BATCH:
                try:
                    items.append(self.queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                counts = await asyncio.to_thread(self._apply_batch, items)
            except Exception as exc:
                self._last["last_error_summary"] = f"write failed: {type(exc).__name__}"
                log.error("write batch failed: %s: %s", type(exc).__name__, exc)
            else:
                self._counters["messages_created"] += counts["created"]
                self._counters["messages_edited"] += counts["edited"]
                self._counters["messages_deleted"] += counts["deleted"]
                self._counters["bulk_deletes"] += counts["bulk"]
                self._counters["backfill_fetched"] += counts["backfill"]
                if any(counts.values()):
                    self._last["last_successful_ingest_at"] = now_utc_iso()
            finally:
                for _ in items:
                    self.queue.task_done()

    # ------------------------------------------------------ status snapshot

    def _write_status(self) -> None:
        """Runs in a worker thread. Content-free, published for the indexer
        to copy into the search index's meta table."""
        state = {
            "collector_health": self._health,
            "gateway_connected": "1" if (self.is_ready() and not self.is_closed()) else "0",
            "queue_depth": self.queue.qsize(),
            "queue_high_water": self._counters["queue_high_water"],
            "dropped_out_of_scope": self._counters["dropped_out_of_scope"],
            "intents_value": build_intents().value,
            "library": f"discord.py {discord.__version__}",
            "updated_at": now_utc_iso(),
        }
        state.update({k: v for k, v in self._counters.items()})
        state.update(self._last)
        try:
            dk_store.set_ingest_states(self.con, state)
            counts = dk_store.counts_snapshot(self.con)
            dk_store.set_ingest_states(
                self.con,
                {
                    "source_message_count": counts["live"],
                    "deleted_message_count": counts["deleted"],
                    "messages_with_content": counts["with_content"],
                    "oldest_message_at": counts["oldest_message_at"] or "",
                    "newest_message_at": counts["newest_message_at"] or "",
                },
            )
            self.con.commit()
        except Exception as exc:
            log.warning("status publish failed: %s", type(exc).__name__)

    async def _status_loop(self) -> None:
        while True:
            await asyncio.to_thread(self._write_status)
            await asyncio.sleep(STATUS_INTERVAL_S)

    # ------------------------------------------------------ gateway events

    async def setup_hook(self) -> None:
        self._writer_task = asyncio.create_task(self._writer_loop(), name="dk-writer")
        self._status_task = asyncio.create_task(self._status_loop(), name="dk-status")

    def _seed_registry(self) -> int:
        """Rebuild the parent map from what is already stored.

        `_parents` and `_channel_ctx` are in-memory, so a restart forgets every
        thread the collector knows about. An edit or a bulk delete arriving for
        one of those threads then resolves parent_channel_id=None, fails the
        allowlist check and is dropped — the thread is not itself allowlisted,
        only its parent is. Seeding from the channels table closes that window
        at startup instead of waiting for a fresh message in each thread.
        """
        try:
            rows = dk_store.known_channels(self.con)
        except Exception as exc:
            log.warning("could not seed channel registry: %s", type(exc).__name__)
            return 0
        for r in rows:
            cid = r["channel_id"]
            self._parents[cid] = r["parent_channel_id"]
            self._channel_ctx.setdefault(
                cid,
                {
                    "channel_id": cid,
                    "channel_name": r["channel_name"],
                    "channel_type": r["channel_type"],
                    "parent_channel_id": r["parent_channel_id"],
                    "thread_name": r["channel_name"] if r["is_thread"] else None,
                    "is_thread": r["is_thread"],
                },
            )
        return len(rows)

    async def on_ready(self) -> None:
        self._counters["full_reconnect_count"] += 1
        self._last["last_ready_at"] = now_utc_iso()
        seeded = await asyncio.to_thread(self._seed_registry)
        if seeded:
            log.info("channel registry seeded from store: %d channels", seeded)
        joined = {str(g.id): g.name for g in self.guilds}
        self._guild_names.update(joined)

        unexpected = sorted(set(joined) - set(g.guild_id for g in self.cfg.guilds))
        if unexpected:
            # Being in an extra guild is not fatal — the allowlist already
            # refuses it — but it must be visible.
            log.warning("bot is a member of %d non-allowlisted guild(s)", len(unexpected))

        for scope in self.cfg.guilds:
            if not scope.active:
                continue
            if scope.guild_id not in joined:
                log.warning("allowlisted guild %s not joined by the bot", scope.guild_id)
                self._last["last_error_summary"] = (
                    f"allowlisted guild {scope.guild_id} not joined"
                )
                continue
            for channel_id in scope.channels:
                await self._register_channel(scope.guild_id, channel_id)

        self._set_health(HEALTH_CONNECTED)
        log.info(
            "READY guilds=%d allowed_guilds=%d allowed_channels=%d intents=%d",
            len(self.guilds),
            len(self.cfg.active_guild_ids),
            len(self.cfg.allowed_channel_ids),
            build_intents().value,
        )
        if self._backfill_task is None or self._backfill_task.done():
            self._backfill_task = asyncio.create_task(
                self._run_backfill(), name="dk-backfill"
            )

    async def _register_channel(self, guild_id, channel_id) -> None:
        try:
            channel = self.get_channel(int(channel_id)) or await self.fetch_channel(
                int(channel_id)
            )
        except discord.Forbidden as exc:
            log.warning("channel %s not readable: forbidden", channel_id)
            await self.submit(
                {
                    "kind": "channel_unreadable",
                    "channel_id": str(channel_id),
                    "guild_id": str(guild_id),
                    "reason": f"forbidden: {exc.text or 'no access'}",
                }
            )
            return
        except discord.HTTPException as exc:
            log.warning("channel %s lookup failed: http %s", channel_id, exc.status)
            return
        ctx = self._remember_channel(channel, guild_id)
        await self.submit(
            {
                "kind": "channel_meta",
                "guild_id": str(guild_id),
                "guild_name": self._guild_names.get(str(guild_id)),
                "channel_ctx": ctx,
                "readable": True,
            }
        )

    async def _run_backfill(self) -> None:
        if not self.cfg.backfill.get("enabled"):
            return
        prev = self._health
        self._set_health(HEALTH_BACKFILLING)
        try:
            states = await asyncio.to_thread(self._load_backfill_states)
            newest = await asyncio.to_thread(self._load_newest_ids)
            await backfill_all(
                self, self.cfg, submit=self.submit, states=states, log=log,
                newest=newest,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("backfill pass failed: %s: %s", type(exc).__name__, exc)
            self._last["last_error_summary"] = f"backfill: {type(exc).__name__}"
        finally:
            self._set_health(HEALTH_CONNECTED if self.is_ready() else prev)

    def _load_backfill_states(self) -> dict:
        return {s["channel_id"]: s for s in dk_store.backfill_summary(self.con)}

    def _load_newest_ids(self) -> dict:
        """Newest stored message per channel — the starting point for the
        catch-up pass that recovers whatever was posted while we were down."""
        try:
            return dk_store.newest_message_ids(self.con)
        except Exception as exc:
            log.warning("could not read newest message ids: %s", type(exc).__name__)
            return {}

    async def on_resumed(self) -> None:
        self._counters["session_resumed_count"] += 1
        self._set_health(HEALTH_CONNECTED)

    async def on_disconnect(self) -> None:
        # A disconnect is normal; the library resumes on its own.
        self._last["last_disconnect_reason"] = "gateway disconnect"
        if self._health == HEALTH_CONNECTED:
            self._health = HEALTH_DISCONNECTED

    async def on_message(self, message) -> None:
        guild = getattr(message, "guild", None)
        if guild is None:
            return  # DMs are out of scope and the intent is not requested
        channel = message.channel
        ctx = self._remember_channel(channel, guild.id)
        self._last["last_event_at"] = now_utc_iso()
        if not self.cfg.channel_allowed(guild.id, ctx["channel_id"], ctx["parent_channel_id"]):
            self._counters["dropped_out_of_scope"] += 1
            return
        record = normalize_message(
            payload_from_message(message),
            guild_id=str(guild.id),
            guild_name=guild.name,
            channel=ctx,
            limits=self.cfg.limits,
            ingest_source="gateway",
            channel_names={c["channel_id"]: c["channel_name"] for c in self._channel_ctx.values()},
        )
        await self.submit({"kind": "create", "record": record, "channel_ctx": ctx})

    async def on_raw_message_edit(self, payload) -> None:
        """Raw, not cached: edits to messages older than this process still
        arrive. The payload may be partial, so missing fields are fetched
        rather than written as NULL (spec section 37)."""
        data = dict(payload.data or {})
        channel_id = str(payload.channel_id)
        guild_id = str(getattr(payload, "guild_id", None) or data.get("guild_id") or "")
        if not guild_id:
            return
        self._last["last_event_at"] = now_utc_iso()
        ctx = self._ctx_for(channel_id, guild_id)
        if not self.cfg.channel_allowed(guild_id, channel_id, ctx.get("parent_channel_id")):
            self._counters["dropped_out_of_scope"] += 1
            return

        # A payload counts as complete only when it carries the fields a full
        # message object always has. Keying this on `content` alone would let
        # an update that happens to include content — but not `author` — be
        # treated as full, and a full record writes author_id=None over a
        # known author. Ask Discord for the whole message instead.
        partial = not all(k in data for k in ("author", "type", "content"))
        if partial:
            fetched = await self._fetch_message(channel_id, data.get("id"), guild_id)
            if fetched is not None:
                data, ctx, partial = fetched[0], fetched[1], False

        try:
            record = normalize_message(
                data,
                guild_id=guild_id,
                guild_name=self._guild_names.get(guild_id),
                channel=ctx,
                limits=self.cfg.limits,
                ingest_source="gateway",
                partial=partial,
                channel_names={c["channel_id"]: c["channel_name"] for c in self._channel_ctx.values()},
            )
        except ValueError:
            return
        await self.submit(
            {"kind": "update", "record": record, "channel_ctx": ctx, "partial": partial}
        )

    async def _fetch_message(self, channel_id, message_id, guild_id):
        """Best-effort refetch of a message a partial payload did not fully
        describe. Returning None is always safe: the caller then applies the
        partial payload, which cannot erase what it did not carry."""
        if not message_id:
            return None
        async with self._fetch_sem:
            try:
                channel = self.get_channel(int(channel_id)) or await self.fetch_channel(
                    int(channel_id)
                )
                msg = await channel.fetch_message(int(message_id))
            except discord.NotFound:
                return None
            except discord.Forbidden as exc:
                await self.submit(
                    {
                        "kind": "channel_unreadable",
                        "channel_id": str(channel_id),
                        "guild_id": str(guild_id),
                        "reason": f"forbidden: {exc.text or 'no access'}",
                    }
                )
                return None
            except discord.HTTPException as exc:
                if exc.status == 429:
                    self._counters["rate_limit_hits"] += 1
                return None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A refetch that fails for any other reason (mid-reconnect,
                # HTTP layer not up yet) must not cost us the edit itself.
                log.debug(
                    "refetch unavailable for channel=%s: %s",
                    channel_id,
                    type(exc).__name__,
                )
                return None
        return payload_from_message(msg), self._remember_channel(msg.channel, guild_id)

    async def on_raw_message_delete(self, payload) -> None:
        guild_id = str(getattr(payload, "guild_id", None) or "")
        channel_id = str(payload.channel_id)
        if not guild_id:
            return
        self._last["last_event_at"] = now_utc_iso()
        if not await self._delete_in_scope(guild_id, channel_id, payload.message_id):
            return
        await self.submit(
            {
                "kind": "delete",
                "guild_id": guild_id,
                "channel_id": channel_id,
                "message_id": str(payload.message_id),
            }
        )

    async def on_raw_bulk_message_delete(self, payload) -> None:
        guild_id = str(getattr(payload, "guild_id", None) or "")
        channel_id = str(payload.channel_id)
        if not guild_id:
            return
        self._last["last_event_at"] = now_utc_iso()
        ctx = self._ctx_for(channel_id, guild_id)
        if not self.cfg.channel_allowed(guild_id, channel_id, ctx.get("parent_channel_id")):
            return
        await self.submit(
            {
                "kind": "bulk_delete",
                "guild_id": guild_id,
                "channel_id": channel_id,
                "message_ids": [str(m) for m in payload.message_ids],
            }
        )

    async def _delete_in_scope(self, guild_id, channel_id, message_id) -> bool:
        """A delete is acted on when the channel is allowlisted, or when the
        message is already stored. A channel we never collected must not gain
        tombstones."""
        ctx = self._ctx_for(channel_id, guild_id)
        if self.cfg.channel_allowed(guild_id, channel_id, ctx.get("parent_channel_id")):
            return True
        from dk_schema import source_id_for

        sid = source_id_for(guild_id, channel_id, message_id)
        row = await asyncio.to_thread(dk_store.get_message, self.con, sid)
        return row is not None

    async def on_thread_create(self, thread) -> None:
        guild = getattr(thread, "guild", None)
        if guild is None:
            return
        ctx = self._remember_channel(thread, guild.id)
        if not self.cfg.channel_allowed(guild.id, ctx["channel_id"], ctx["parent_channel_id"]):
            return
        await self.submit(
            {
                "kind": "channel_meta",
                "guild_id": str(guild.id),
                "guild_name": guild.name,
                "channel_ctx": ctx,
                "readable": True,
            }
        )

    async def on_error(self, event_method, *args, **kwargs) -> None:
        exc = sys.exc_info()[1]
        self._last["last_error_summary"] = f"{event_method}: {type(exc).__name__}"
        log.error("handler error in %s: %s", event_method, type(exc).__name__)

    async def shutdown(self) -> None:
        self._set_health(HEALTH_STOPPED)
        # Stop producing first, then let the writer finish what is already
        # queued. Cancelling the writer with items still in flight would drop
        # messages that Discord will never resend.
        if self._backfill_task is not None:
            self._backfill_task.cancel()
        if self._writer_task is not None and not self._writer_task.done():
            try:
                await asyncio.wait_for(self.queue.join(), timeout=SHUTDOWN_DRAIN_S)
            except (TimeoutError, asyncio.TimeoutError):
                log.warning(
                    "shutdown: %d write items still queued after %ds",
                    self.queue.qsize(),
                    SHUTDOWN_DRAIN_S,
                )
            except asyncio.CancelledError:
                raise
        for task in (self._status_task, self._writer_task):
            if task is not None:
                task.cancel()
        try:
            await asyncio.to_thread(self._write_status)
        except Exception:
            pass
        if not self.is_closed():
            await self.close()


async def run(cfg, token) -> int:
    con = init_source_db(connect(cfg.source_db, check_same_thread=False))
    client = Collector(cfg, con)

    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stopping.set)
        except NotImplementedError:
            pass

    async def _watch_stop():
        await stopping.wait()
        log.info("shutdown signal received")
        await client.shutdown()

    stopper = asyncio.create_task(_watch_stop())
    rc = 0
    try:
        await client.start(token)
    except discord.LoginFailure as exc:
        client._set_health(HEALTH_AUTH_FAILED, "invalid bot token (login rejected)")
        log.error(
            "authentication failed: the bot token was rejected. Not retrying; "
            "replace the credential and restart. (%s)",
            type(exc).__name__,
        )
        rc = EX_CONFIG
    except discord.PrivilegedIntentsRequired:
        client._set_health(
            HEALTH_DEGRADED,
            "MESSAGE_CONTENT privileged intent is not enabled for this application",
        )
        log.error(
            "gateway refused the connection: the MESSAGE_CONTENT privileged "
            "intent is not enabled for this application. Enable it in the "
            "Discord Developer Portal (Bot -> Privileged Gateway Intents). "
            "No alternative access method will be attempted."
        )
        rc = EX_CONFIG
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        client._set_health(HEALTH_DEGRADED, f"{type(exc).__name__}")
        log.exception("collector stopped with an unexpected error")
        rc = 1
    finally:
        stopper.cancel()
        try:
            await client.shutdown()
        except Exception:
            pass
        try:
            await asyncio.to_thread(client._write_status)
            con.close()
        except Exception:
            pass
    return rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Discord Knowledge Collector (read-only ingestion)"
    )
    ap.add_argument("--config", default=None)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument(
        "--check",
        action="store_true",
        help="validate configuration and exit without connecting",
    )
    args = ap.parse_args(argv)
    # Deliberately absent: any option that would accept the bot token. argv is
    # world-readable through /proc, so the credential only ever arrives via
    # systemd LoadCredential or the root-owned secret file.

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"DISCORD_COLLECTOR config error: {exc}", file=sys.stderr)
        return EX_CONFIG

    setup_logging(cfg.log_dir, args.verbose)

    if not cfg.ready_to_ingest:
        # Default deny: an empty or placeholder allowlist collects nothing.
        # It must never widen into "collect everything the bot can see".
        try:
            con = init_source_db(connect(cfg.source_db))
            dk_store.set_ingest_states(
                con,
                {
                    "collector_health": HEALTH_DEGRADED,
                    "gateway_connected": "0",
                    "last_error_summary": (
                        "no active guild/channel allowlist: nothing is collected. "
                        "Fill in guilds[].guild_id, set enabled: true and list "
                        "channels[] in the collector configuration."
                    ),
                    "updated_at": now_utc_iso(),
                },
            )
            con.commit()
            con.close()
        except Exception:
            pass
        log.error(
            "no active guild/channel allowlist — refusing to connect. "
            "Default deny is intentional: fill in the allowlist first."
        )
        return EX_CONFIG

    if args.check:
        print(
            "DISCORD_COLLECTOR config ok guilds=%d channels=%d source_db=%s"
            % (len(cfg.active_guild_ids), len(cfg.allowed_channel_ids), cfg.source_db)
        )
        return 0

    try:
        token = load_token(cfg)
    except ConfigError as exc:
        log.error("%s", exc)
        return EX_CONFIG

    try:
        return asyncio.run(run(cfg, token))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # pragma: no cover - last resort
        print(
            f"DISCORD_COLLECTOR_FATAL {type(exc).__name__}: {exc}", file=sys.stderr
        )
        raise SystemExit(1)
