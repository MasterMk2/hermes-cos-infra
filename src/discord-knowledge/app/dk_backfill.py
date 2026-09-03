#!/usr/bin/env python3
"""Discord Knowledge — bounded, resumable historical backfill.

The gateway only delivers messages sent while the collector is connected, so
history comes from the REST endpoint. Three properties matter:

* **Bounded.** A first run never walks the whole history of a busy guild. It
  stops at whichever comes first: the configured day window, the per-channel
  message cap, or the per-run page cap (spec sections 46, 47).

* **Resumable.** Progress is a cursor persisted per channel, so a crash or a
  restart continues from the oldest message already fetched instead of
  starting over (section 44).

* **Rate-limit respecting.** Paging goes through the Discord library's HTTP
  layer, which honours the bucket headers and the `Retry-After` value on a
  429. Nothing here sleeps on a guessed interval (sections 20, 45, 88).

Backfill never writes to the database directly: it hands batches to the
collector's single writer task, so there is exactly one writer and the
gateway path is never blocked behind a history page.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import discord

from dk_adapter import channel_context, payload_from_message
from dk_normalize import normalize_message


class BackfillResult(dict):
    """Plain dict; named for readability at call sites."""


async def _resolve_channel(client, channel_id):
    ch = client.get_channel(int(channel_id))
    if ch is not None:
        return ch
    return await client.fetch_channel(int(channel_id))


async def backfill_channel(
    client,
    cfg,
    guild_scope,
    channel_id,
    *,
    submit,
    state,
    log,
) -> BackfillResult:
    """Page one channel backwards until a bound is reached.

    `submit` is an awaitable taking a work item for the collector's writer.
    `state` is the persisted `backfill_state` row (or None on a first run).
    """
    bf = cfg.backfill
    result = BackfillResult(
        channel_id=str(channel_id),
        fetched=0,
        pages=0,
        complete=False,
        stopped_because=None,
        error=None,
    )
    if not bf.get("enabled"):
        result["stopped_because"] = "disabled"
        return result

    already = int((state or {}).get("message_count") or 0)
    if already >= int(bf["max_messages_per_channel"]):
        result["complete"] = bool((state or {}).get("complete"))
        result["stopped_because"] = "channel_cap_reached"
        return result

    cutoff = datetime.now(timezone.utc) - timedelta(days=int(bf["days"]))
    cursor = (state or {}).get("last_cursor")
    before = discord.Object(id=int(cursor)) if cursor else None

    async def _record(complete=None, cursor=None):
        """Persist why this channel stopped, then log it.

        Every return that carries an error goes through here — the two
        resolve failures, the container-channel branch, and both failures
        inside the paging loop. (The two returns that carry no error at all,
        backfill disabled and per-channel cap already reached, do not: the
        first records nothing anywhere by design and the second already has
        its state row.)

        An earlier version computed an error string and returned before the
        submit and the log, so a channel that was never paged looked
        identical to one that finished cleanly — the status surface showed a
        complete, error-free backfill while seventeen channels had silently
        never been touched.
        """
        await submit(
            {
                "kind": "backfill_state",
                "channel_id": str(channel_id),
                "guild_id": guild_scope.guild_id,
                # Keep the cursor on a mid-run failure so the next pass
                # resumes where this one stopped instead of starting over.
                "cursor": cursor,
                "complete": result["complete"] if complete is None else complete,
                "error": result["error"],
            }
        )
        log.info(
            "backfill channel=%s fetched=%d pages=%d complete=%s reason=%s",
            channel_id,
            result["fetched"],
            result["pages"],
            result["complete"] if complete is None else complete,
            result["stopped_because"] or result["error"],
        )
        return result

    try:
        channel = await _resolve_channel(client, channel_id)
    except discord.Forbidden as exc:
        result["error"] = f"forbidden: {exc.text or 'no access'}"[:300]
        result["stopped_because"] = "forbidden"
        await submit(
            {
                "kind": "channel_unreadable",
                "channel_id": str(channel_id),
                "guild_id": guild_scope.guild_id,
                "reason": result["error"],
            }
        )
        return await _record()
    except discord.HTTPException as exc:
        result["error"] = f"http {exc.status}"[:300]
        result["stopped_because"] = "resolve_failed"
        return await _record()

    if not hasattr(channel, "history"):
        # A forum or media channel holds no messages of its own; its content
        # lives in the threads under it, which backfill_all enumerates
        # separately. Record that fact rather than dropping it.
        result["error"] = "container channel: no message history of its own"
        result["stopped_because"] = "container_channel"
        result["complete"] = True
        return await _record()

    ctx = channel_context(channel)
    guild = getattr(channel, "guild", None)
    guild_name = getattr(guild, "name", None)

    remaining = int(bf["max_messages_per_channel"]) - already
    page_size = int(bf["page_size"])
    max_pages = int(bf["max_pages_per_run"])

    while result["pages"] < max_pages and remaining > 0:
        limit = min(page_size, remaining)
        try:
            batch = [m async for m in channel.history(limit=limit, before=before, oldest_first=False)]
        except discord.Forbidden as exc:
            # Visible channel, no Read Message History: a permission gap that
            # will not fix itself, so it must be recorded rather than left as
            # a channel that merely produced nothing.
            result["error"] = f"forbidden: {exc.text or 'no read history'}"[:300]
            result["stopped_because"] = "forbidden"
            await submit(
                {
                    "kind": "channel_unreadable",
                    "channel_id": ctx["channel_id"],
                    "guild_id": guild_scope.guild_id,
                    "reason": result["error"],
                }
            )
            return await _record(
                complete=False, cursor=str(before.id) if before is not None else None
            )
        except discord.HTTPException as exc:
            # The library has already waited out any 429 it could handle; a
            # surviving error is recorded and retried on the next run rather
            # than hammered here.
            result["error"] = f"http {exc.status}: {str(exc)[:120]}"
            result["stopped_because"] = "http_error"
            if exc.status == 429:
                await submit({"kind": "rate_limit"})
            return await _record(
                complete=False, cursor=str(before.id) if before is not None else None
            )

        result["pages"] += 1
        if not batch:
            result["complete"] = True
            result["stopped_because"] = "history_exhausted"
            break

        records = []
        too_old = False
        for msg in batch:
            created = getattr(msg, "created_at", None)
            if created is not None and created < cutoff:
                too_old = True
                continue
            payload = payload_from_message(msg)
            records.append(
                normalize_message(
                    payload,
                    guild_id=guild_scope.guild_id,
                    guild_name=guild_name,
                    channel=ctx,
                    limits=cfg.limits,
                    ingest_source="backfill",
                )
            )

        oldest = batch[-1]
        before = discord.Object(id=oldest.id)
        oldest_ts = int(oldest.created_at.timestamp() * 1000) if oldest.created_at else None

        await submit(
            {
                "kind": "backfill_batch",
                "records": records,
                "channel_id": ctx["channel_id"],
                "guild_id": guild_scope.guild_id,
                "channel_ctx": ctx,
                "guild_name": guild_name,
                "cursor": str(oldest.id),
                "oldest_created_ts": oldest_ts,
                "added": len(records),
                "complete": False,
            }
        )

        result["fetched"] += len(records)
        remaining -= len(batch)

        if too_old:
            result["complete"] = True
            result["stopped_because"] = "day_window_reached"
            break
        if len(batch) < limit:
            result["complete"] = True
            result["stopped_because"] = "history_exhausted"
            break

        # Yield between pages so gateway events keep being serviced.
        await asyncio.sleep(0)

    if result["stopped_because"] is None:
        result["stopped_because"] = (
            "page_cap_reached" if result["pages"] >= max_pages else "channel_cap_reached"
        )

    await submit(
        {
            "kind": "backfill_state",
            "channel_id": ctx["channel_id"],
            "guild_id": guild_scope.guild_id,
            "cursor": str(before.id) if before is not None else None,
            "complete": result["complete"],
            "error": result["error"],
        }
    )
    log.info(
        "backfill channel=%s fetched=%d pages=%d complete=%s reason=%s",
        ctx["channel_id"],
        result["fetched"],
        result["pages"],
        result["complete"],
        result["stopped_because"],
    )
    return result


# A forum's posts are threads, and a busy text channel can carry them too.
# Bounded like everything else in this module: at most this many threads per
# parent channel per pass.
MAX_THREADS_PER_CHANNEL = 50

# Threads multiply the per-channel cap: 50 threads x the per-channel message
# cap would be a 50x larger ceiling than the operator sized against measured
# volume. Cap the whole family instead.
MAX_THREAD_MESSAGES_PER_PARENT = 4000

# A catch-up pass is short by construction: it only has to cover the gap since
# the collector last saw the channel.
MAX_CATCHUP_PAGES = 5


async def catchup_channel(
    client, cfg, guild_scope, channel_id, after_message_id, *, submit, log
) -> BackfillResult:
    """Fetch anything posted while the collector was not listening.

    The gateway only delivers what arrives while connected, and a completed
    backfill never runs again — so without this, every message posted during a
    restart, a deploy, a crash or a network outage is permanently absent, and
    nothing anywhere records that a gap exists. Paging forward from the newest
    message already stored is the only way to close it.
    """
    result = BackfillResult(
        channel_id=str(channel_id), fetched=0, pages=0, complete=True,
        stopped_because="catchup", error=None,
    )
    try:
        channel = await _resolve_channel(client, channel_id)
    except (discord.Forbidden, discord.HTTPException):
        return result
    if not hasattr(channel, "history"):
        return result

    ctx = channel_context(channel)
    guild = getattr(channel, "guild", None)
    after = discord.Object(id=int(after_message_id))
    page_size = int(cfg.backfill["page_size"])

    while result["pages"] < MAX_CATCHUP_PAGES:
        try:
            batch = [
                m
                async for m in channel.history(
                    limit=page_size, after=after, oldest_first=True
                )
            ]
        except discord.Forbidden:
            return result
        except discord.HTTPException as exc:
            if exc.status == 429:
                await submit({"kind": "rate_limit"})
            return result
        result["pages"] += 1
        if not batch:
            break
        records = [
            normalize_message(
                payload_from_message(m),
                guild_id=guild_scope.guild_id,
                guild_name=getattr(guild, "name", None),
                channel=ctx,
                limits=cfg.limits,
                ingest_source="backfill",
            )
            for m in batch
        ]
        await submit(
            {
                "kind": "backfill_batch",
                "records": records,
                "channel_id": ctx["channel_id"],
                "guild_id": guild_scope.guild_id,
                "channel_ctx": ctx,
                "guild_name": getattr(guild, "name", None),
                "cursor": None,
                "oldest_created_ts": None,
                "added": len(records),
                "complete": True,
            }
        )
        result["fetched"] += len(records)
        after = discord.Object(id=batch[-1].id)
        if len(batch) < page_size:
            break
        await asyncio.sleep(0)

    if result["fetched"]:
        log.info(
            "catchup channel=%s recovered=%d pages=%d",
            channel_id, result["fetched"], result["pages"],
        )
    return result


async def _threads_under(client, channel_id, *, limit, log):
    """Threads whose messages belong to an allowlisted parent.

    Without this, allowlisting a forum collects nothing historical: the forum
    itself has no message history, and every post lives in a thread with its
    own channel id. Live posts still arrive over the gateway, which is why the
    gap was invisible until the backfill state was audited.
    """
    try:
        channel = await _resolve_channel(client, channel_id)
    except (discord.Forbidden, discord.HTTPException):
        return []
    found, seen = [], set()

    for t in list(getattr(channel, "threads", []) or []):
        if t.id not in seen:
            seen.add(t.id)
            found.append(t)

    archived = getattr(channel, "archived_threads", None)
    if callable(archived) and len(found) < limit:
        for public in (True, False):
            if len(found) >= limit:
                break
            try:
                async for t in archived(limit=limit - len(found), private=not public):
                    if t.id not in seen:
                        seen.add(t.id)
                        found.append(t)
            except discord.Forbidden:
                continue
            except discord.HTTPException as exc:
                log.warning(
                    "archived threads unavailable for channel=%s: http %s",
                    channel_id,
                    exc.status,
                )
                break
            except TypeError:
                # Older signatures do not accept `private`; one pass is enough.
                break
    return found[:limit]


async def backfill_all(client, cfg, *, submit, states, log, newest=None) -> list:
    """One bounded pass: catch up on what was missed, then continue history.

    `newest` maps channel_id -> newest message id already stored. Channels it
    names get a forward catch-up first, which is the only way to recover
    messages posted while the collector was down.
    """
    out = []
    newest = newest or {}

    async def _one(guild_scope, channel_id, state):
        try:
            return await backfill_channel(
                client, cfg, guild_scope, channel_id,
                submit=submit, state=state, log=log,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never let one channel abort the pass
            log.warning(
                "backfill failed channel=%s %s: %s",
                channel_id, type(exc).__name__, str(exc)[:200],
            )
            return BackfillResult(
                channel_id=str(channel_id), fetched=0, pages=0,
                complete=False, error=f"{type(exc).__name__}",
            )

    # A pass that recovered nothing and a pass that never ran look identical
    # unless the pass itself is counted, so count it.
    stats = {"probed": 0, "skipped": 0, "recovered": 0, "failed": 0}

    async def _catchup(guild_scope, channel_id):
        after = newest.get(str(channel_id))
        if not after:
            stats["skipped"] += 1
            return
        stats["probed"] += 1
        try:
            r = await catchup_channel(
                client, cfg, guild_scope, channel_id, after,
                submit=submit, log=log,
            )
            stats["recovered"] += int(r.get("fetched") or 0)
            out.append(r)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            stats["failed"] += 1
            log.warning(
                "catchup failed channel=%s %s", channel_id, type(exc).__name__
            )

    for guild_scope in cfg.guilds:
        if not guild_scope.active:
            continue
        for channel_id in guild_scope.channels:
            await _catchup(guild_scope, channel_id)
            state = states.get(str(channel_id))
            if not (state and state.get("complete")):
                out.append(await _one(guild_scope, channel_id, state))

            if not guild_scope.include_threads:
                continue
            try:
                threads = await _threads_under(
                    client, channel_id, limit=MAX_THREADS_PER_CHANNEL, log=log
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "thread enumeration failed channel=%s %s",
                    channel_id, type(exc).__name__,
                )
                continue
            if threads:
                log.info(
                    "backfill parent=%s threads=%d", channel_id, len(threads)
                )
            budget = MAX_THREAD_MESSAGES_PER_PARENT
            for thread in threads:
                # Register every thread we touch, before it has a chance to
                # yield nothing. A thread whose whole history predates the
                # window would otherwise leave a backfill_state row that
                # resolves to no name, no parent and no scope justification.
                await submit(
                    {
                        "kind": "channel_meta",
                        "guild_id": guild_scope.guild_id,
                        "guild_name": getattr(
                            getattr(thread, "guild", None), "name", None
                        ),
                        "channel_ctx": channel_context(thread),
                        "readable": True,
                    }
                )
                await _catchup(guild_scope, thread.id)
                tstate = states.get(str(thread.id))
                budget -= int((tstate or {}).get("message_count") or 0)
                if budget <= 0:
                    log.info(
                        "thread budget spent under parent=%s; %d threads deferred",
                        channel_id,
                        len(threads) - threads.index(thread),
                    )
                    break
                if tstate and tstate.get("complete"):
                    continue
                r = await _one(guild_scope, thread.id, tstate)
                out.append(r)
                budget -= int(r.get("fetched") or 0)

    log.info(
        "catchup pass: probed=%d recovered=%d skipped_no_prior_message=%d failed=%d",
        stats["probed"], stats["recovered"], stats["skipped"], stats["failed"],
    )
    return out
