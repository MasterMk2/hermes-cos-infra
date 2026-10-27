#!/usr/bin/env python3
"""Unified Channel Gateway — the Discord Interface adapter.

This is the *second* Discord bot in this system and it must never be confused
with the first (spec sections 3, 41):

    Knowledge Bot (phase D.1)   ears only. Reads guild channels, writes to a
                                search index, never speaks.
    Interface Bot (this file)   mouth and ears for one person, in DMs only.
                                Never reads a guild channel.

They are separate Discord Applications with separate tokens, and the
separation is enforced here by construction rather than by discipline:

* the gateway connects with `Intents.none() + dm_messages`, numeric value
  **4096**. GUILDS and GUILD_MESSAGES are absent, so guild message events are
  not delivered to this connection at all — there is no guild content for a
  bug to leak, because none arrives.
* `MESSAGE_CONTENT` (32768) is deliberately NOT requested. Discord documents
  four cases in which content is delivered without it, and "Content in DMs
  with the app" is one of them, which is the entire surface this bot uses.
  Verified against docs.discord.com/developers/events/gateway on 2026-09-03.
* every send goes out with `AllowedMentions.none()` set client-wide, so no
  answer this gateway relays can ping @everyone, a role, or a user.

The adapter does not answer questions. It normalizes, authenticates against
the single configured owner id, and puts a row in the queue; the worker does
the rest. That is the whole of section 12's "the gateway is not an agent".
"""

from __future__ import annotations

import asyncio
import logging

import discord

import gw_store as store
from gw_canonical import PLATFORM_DISCORD, canonical_from_discord
from gw_format import chunk_text, neutralize_mentions, redact
# open_db lives in gw_schema, not gw_store. Calling store.open_db raised
# AttributeError on every single DM — caught by on_message's handler, logged,
# and otherwise invisible: the bot stayed connected and answered nothing.
from gw_schema import open_db

log = logging.getLogger("gateway.discord")

# Discord's own hard limit on a message body. The library does not chunk for
# you and raises HTTPException(50035) if you exceed it, so the gateway chunks
# below the line rather than discovering it.
DISCORD_MAX_MESSAGE_CHARS = 2000

# The intents this bot is allowed to hold. Asserted at startup: if a later
# edit widens them, the service refuses to start instead of quietly gaining
# the ability to read guild messages.
REQUIRED_INTENTS_VALUE = 4096


def build_intents() -> discord.Intents:
    """DIRECT_MESSAGES and nothing else."""
    intents = discord.Intents.none()
    intents.dm_messages = True
    return intents


class InterfaceBot(discord.Client):
    """Receives owner DMs, queues them, and delivers answers back."""

    def __init__(self, cfg, db_path: str, wake: asyncio.Event):
        intents = build_intents()
        if intents.value != REQUIRED_INTENTS_VALUE:
            # Not a warning. A widened intent set is a change in what this
            # process is capable of reading, and it must not happen silently.
            raise RuntimeError(
                f"interface bot intents are {intents.value}, expected "
                f"{REQUIRED_INTENTS_VALUE} (DIRECT_MESSAGES only)"
            )
        super().__init__(
            intents=intents,
            # Client-wide, so it applies to every send including ones added
            # later that forget to pass it.
            allowed_mentions=discord.AllowedMentions.none(),
            max_messages=None,      # no message cache; nothing to leak
            chunk_guilds_at_startup=False,
        )
        self.cfg = cfg
        self.db_path = db_path
        self.wake = wake
        self._chunk_chars = min(
            int(cfg.discord.get("text_chunk_chars") or 1900),
            DISCORD_MAX_MESSAGE_CHARS,
        )
        self._max_chunks = int(cfg.discord.get("max_chunks") or 4)
        # Resolved lazily by `owner_dm_channel`, cleared whenever opening it
        # fails so a transient error cannot pin a broken handle forever.
        self._owner_dm = None
        # Orders intake commits. Each DM is its own task; with the SQLite work
        # in a thread, two DMs sent back to back could commit (and be claimed)
        # in either order. asyncio.Lock wakes waiters first in, first out, and
        # nothing awaits before it in _handle, so commits follow dispatch.
        self._record_lock = asyncio.Lock()

    # -- lifecycle -----------------------------------------------------

    async def on_ready(self):
        log.info(
            "interface bot ready as %s (id=%s) intents=%s guilds_visible=%d",
            self.user, getattr(self.user, "id", "?"),
            self.intents.value, len(self.guilds),
        )

    # -- inbound -------------------------------------------------------

    async def on_message(self, message: discord.Message):
        """Queue one owner DM. Everything else is dropped, quietly."""
        try:
            await self._handle(message)
        except Exception:
            # A raise here would be swallowed by discord.py's task handling
            # and the message would vanish with no trace. Log it against the
            # id so it can be found, and keep the connection alive.
            log.exception("interface on_message failed id=%s",
                          getattr(message, "id", "?"))

    async def _handle(self, message: discord.Message):
        is_dm = isinstance(message.channel, discord.DMChannel)
        is_bot = bool(getattr(message.author, "bot", False))

        # `canonical_from_discord` returns None for a bot author or a
        # non-DM channel, so this is belt and braces — but the belt is in a
        # different file and this is the one place a guild message could
        # arrive if the intents were ever widened.
        if not is_dm or is_bot:
            return

        canonical = canonical_from_discord(
            message_id=message.id,
            author_id=message.author.id,
            channel_id=message.channel.id,
            content=message.content,
            created_at=message.created_at,
            is_bot=is_bot,
            is_dm=is_dm,
            owner_discord_user_id=self.cfg.owner_discord_user_id,
        )
        if canonical is None:
            return

        # Empty content on a DM means the intent assumption above is wrong on
        # this account. Say so loudly once per message rather than treating a
        # blank prompt as a real question.
        if not canonical.text.strip():
            log.warning(
                "empty DM content id=%s — if this persists, MESSAGE_CONTENT "
                "may be required on this application", message.id
            )
            return

        # SQLite work runs in a thread. On the event loop it shares the thread
        # with discord.py's gateway heartbeat, and a slow USB write once held
        # that heartbeat long enough to risk a shard reconnect.
        async with self._record_lock:
            outcome = await asyncio.to_thread(self._record, canonical)
        if outcome == "not_owner":
            log.info("dropped non-owner DM from %s", message.author.id)
        elif outcome == "queued":
            self.wake.set()

    def _record(self, canonical) -> str:
        """Durably record one DM. Returns 'queued', 'duplicate' or 'not_owner'."""
        con = open_db(self.db_path)
        try:
            request_id, is_new = store.record_inbound(con, canonical)
            if not canonical.authenticated:
                # Not the owner. Recorded for the audit trail, never answered:
                # a reply would confirm to a stranger that this bot is live
                # and belongs to someone.
                store.audit(con, "inbound_rejected", request_id=request_id,
                            platform=PLATFORM_DISCORD, authenticated=False,
                            detail="not_owner")
                con.commit()
                return "not_owner"
            if not is_new:
                store.audit(con, "inbound_duplicate", request_id=request_id,
                            platform=PLATFORM_DISCORD, detail="dedupe")
                con.commit()
                return "duplicate"
            store.enqueue(con, request_id)
            store.audit(con, "inbound_queued", request_id=request_id,
                        platform=PLATFORM_DISCORD, authenticated=True,
                        capability_profile=canonical.capability_profile)
            con.commit()
            return "queued"
        finally:
            con.close()

    # -- outbound ------------------------------------------------------

    def prepare(self, text: str) -> list:
        """Chunk an answer to Discord's limit, with mentions neutralised."""
        body = redact(neutralize_mentions(text or ""))
        return chunk_text(body, self._chunk_chars, self._max_chunks)

    async def send_chunks(self, channel_id: int, chunks: list) -> tuple:
        """Deliver to a DM channel. Returns (sent_count, error_or_None)."""
        try:
            channel = self.get_channel(int(channel_id)) or \
                await self.fetch_channel(int(channel_id))
        except discord.HTTPException as exc:
            return 0, f"channel_unavailable:{exc.code}"
        except Exception as exc:  # lookup failed before any channel.send
            return 0, f"channel_unavailable:{type(exc).__name__}"
        return await self._send_to(channel, chunks)

    async def typing_once(self, channel_id) -> bool:
        """Show "typing…" in a DM for a few seconds. Best effort, no state.

        One POST to Discord's typing endpoint (`await channel.typing()` in
        discord.py 2.x). It creates no message, so repeating it after a
        restart is harmless, and Discord clears it when the reply arrives.
        Any failure is swallowed: the indicator must never affect the reply.
        """
        try:
            channel = self.get_channel(int(channel_id)) or \
                await self.fetch_channel(int(channel_id))
            await channel.typing()
            return True
        except Exception as exc:
            log.debug("typing indicator skipped: %s", type(exc).__name__)
            return False

    async def owner_dm_channel(self):
        """The DM channel with the configured owner, opened if it is not open.

        The reply path never needs this — an inbound DM already tells the
        gateway which channel to answer in. A proactive notification has no
        inbound message, so the channel has to be resolved from the owner id
        (G.2 section 50): the id comes from configuration, never from a
        request body.

        `get_user` almost always misses here: the bot runs with intents 4096
        and no member cache, so there is nothing to hit. The first send costs
        one `fetch_user` and one `create_dm`; discord.py then keeps the DM
        channel on the user object, and this instance keeps it too, so
        subsequent notifications cost neither.
        """
        if self._owner_dm is not None:
            return self._owner_dm
        uid = int(self.cfg.owner_discord_user_id)
        user = self.get_user(uid) or await self.fetch_user(uid)
        channel = user.dm_channel or await user.create_dm()
        self._owner_dm = channel
        return channel

    async def send_owner_dm(self, chunks: list) -> tuple:
        """Deliver a proactive notification. Returns (sent_count, error).

        This adapter does not submit a provider-side idempotency key. The
        caller must hold partial/ambiguous sends as UNKNOWN instead of
        replaying them. Its durable SENDING row also survives a crash after
        channel.send but before the receipt is recorded. This is an at-most
        one automatic attempt on uncertain outcomes, not a delivery guarantee.
        """
        if not self.cfg.owner_discord_user_id:
            return 0, "owner_not_configured"
        try:
            channel = await self.owner_dm_channel()
        except discord.HTTPException as exc:
            self._owner_dm = None
            return 0, f"owner_dm_unavailable:{exc.status}:{exc.code}"
        except Exception as exc:  # pragma: no cover - transport
            self._owner_dm = None
            return 0, f"owner_dm_unavailable:{type(exc).__name__}"
        return await self._send_to(channel, chunks)

    async def _send_to(self, channel, chunks: list) -> tuple:
        """Send chunks in order. Returns (sent_count, error_or_None).

        Stops at the first failure. The caller owns the durable receipt and
        must distinguish a negative response from an ambiguous/partial send.
        Ordinary replies call this with one write-ahead claimed chunk.
        """
        sent = 0
        for chunk in chunks:
            try:
                await channel.send(
                    chunk, allowed_mentions=discord.AllowedMentions.none()
                )
                sent += 1
            except discord.HTTPException as exc:
                # Match on `exc.code`, the Discord error number, not on the
                # exception class: Forbidden/NotFound/HTTPException all carry
                # the actionable detail in `code`.
                return sent, f"http:{exc.status}:{exc.code}"
            except Exception as exc:  # pragma: no cover - transport
                return sent, type(exc).__name__
        return sent, None
