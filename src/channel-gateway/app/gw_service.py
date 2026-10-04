#!/usr/bin/env python3
"""Unified Channel Gateway — the service.

One process holds three things on one asyncio loop:

    listener   an aiohttp route that accepts LINE webhooks, verifies them,
               writes one durable row, and answers. Nothing else.
    interface  a discord.py client for owner DMs (optional).
    worker     the only component that talks to Hermes or sends anything.

The split is the design (spec sections 12, 29, 31). LINE gives the receiver
about two seconds and switches a failing webhook off, so the handler must not
wait on an assistant that takes half a minute to think. Handing off through a
committed database row rather than an in-memory queue is what makes a restart
mid-conversation preserve its intent. The worker resumes safe work and holds
unconfirmed sends rather than silently replaying them.

Three decisions in here are easy to get backwards, so they are stated:

* **A failed enqueue answers 500, not 200.** LINE does not redeliver a 200.
  If the row is not committed, the only remaining copy of that message is
  LINE's retry, and throwing it away by claiming success loses the message
  permanently.
* **An unauthorized sender gets 200 and silence.** Answering would confirm
  the endpoint is live and belongs to someone, and on LINE it would spend
  metered quota on a stranger.
* **The push target is the configured owner, never a payload field.**

Exit 78 (EX_CONFIG) means "configured to do nothing" and is paired with
RestartPreventExitStatus=78 in the unit, so a half-provisioned gateway stops
instead of restart-looping.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
import signal
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from aiohttp import web

import gw_store as store
from gw_canonical import (
    PLATFORM_DISCORD,
    PLATFORM_LINE,
    CanonicalMessage,
    build_envelope,
)
from gw_config import (
    SECRET_MISSING,
    SECRET_UNREADABLE,
    ConfigError,
    load_config,
    load_secret,
    secret_status,
)
from gw_format import prepare_outbound, user_facing_error
from gw_hermes import HermesClient, HermesRejected, HermesUnavailable
from gw_line import (
    SIGNATURE_HEADER,
    build_text_messages,
    choose_delivery_mode,
    extract_events,
    header_lookup,
    is_redelivery,
    parse_webhook_body,
    unsupported_input_reply,
    verify_signature,
)
from gw_line_client import LineClient
from gw_proactive import ProactiveServer
from gw_quota import QuotaSnapshot, LineQuotaClient
from gw_schema import open_db

EXIT_CONFIG = 78

log = logging.getLogger("gateway")

# How long the worker sleeps when the queue is empty and nothing woke it.
# Short enough that a missed wake-up costs seconds, not minutes.
IDLE_POLL_SECONDS = 5

# Housekeeping cadence: stale-lease recovery and retention purge.
MAINTENANCE_INTERVAL_SECONDS = 300


# ----------------------------------------------------------------- logging


def setup_logging(log_dir: str, level: str = "INFO") -> None:
    """stderr always; a rotating file when this account can write one.

    Formatter carries no message body by construction — the call sites pass
    ids and categories. Anything that could hold user text belongs in the
    database, which is 0600 and on the same disk as the rest of the data.

    stderr is attached FIRST and the file is optional, because the log
    directory is 0750 owned by the service user: an operator running
    `--check` from their own account cannot write it. An earlier version
    opened the file before anything else and died with a PermissionError, so
    a diagnostic tool was defeated by the very permissions it exists to
    diagnose. Losing the file copy costs nothing here — under systemd the
    journal has every line anyway.
    """
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s %(message)s"
    )
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    try:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            os.path.join(log_dir, "gateway.log"),
            maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8",
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as exc:
        root.warning("file logging disabled (%s: %s) — stderr only",
                     log_dir, type(exc).__name__)

    # discord.py logs the token nowhere, but it is chatty at INFO.
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


# ------------------------------------------------------------ row -> message


def message_from_row(row) -> CanonicalMessage:
    """Rebuild the canonical message the worker needs from its stored row.

    The worker deliberately reads the row rather than keeping the object the
    listener built: after a restart the object is gone and the row is all
    there is, so making that the only path means the restart case is the
    tested case.
    """
    return CanonicalMessage(
        message_id=row["platform_message_id"],
        platform=row["platform"],
        platform_message_id=row["platform_message_id"],
        platform_user_id=row["platform_user_id"],
        conversation_id=row["conversation_id"],
        timestamp=row["platform_timestamp"] or row["received_at"],
        text=row["text"] or "",
        authz_subject=row["authz_subject"],
        capability_profile=row["capability_profile"],
        gateway_request_id=row["gateway_request_id"],
        platform_event_id=row["platform_event_id"],
        reply_token=row["reply_token"],
        received_at=row["received_at"],
    )


def discord_definitely_not_sent(error) -> bool:
    """Only the adapter's pre-send channel lookup can prove no message send.

    discord.py may internally retry 5xx/resets before returning a final 4xx.
    A final HTTP status alone cannot rule out an earlier accepted attempt.
    """
    return str(error or "").startswith("channel_unavailable:")


# --------------------------------------------------------------- the gateway


class Gateway:
    def __init__(self, cfg, secrets: dict):
        self.cfg = cfg
        self.secrets = secrets
        self.wake = asyncio.Event()
        self.stop = asyncio.Event()
        self.hermes = HermesClient(
            cfg.hermes["base_url"],
            secrets["hermes_api_key"],
            model=cfg.hermes["model"],
            poll_interval=float(cfg.hermes["poll_interval_seconds"]),
            absolute_timeout=float(cfg.hermes["absolute_timeout_seconds"]),
        )
        self.line = (
            LineClient(secrets["line_channel_access_token"])
            if cfg.line_ready and secrets.get("line_channel_access_token")
            else None
        )
        # G.2: the quota reader shares the LINE credential with the sender
        # and, like it, never leaves this process. The Attention Engine asks
        # the gateway for numbers; it is never given a token (section 48).
        self.line_quota = (
            LineQuotaClient(
                secrets["line_channel_access_token"],
                cache_seconds=int(cfg.proactive["quota_cache_seconds"]),
            )
            if cfg.line_ready and secrets.get("line_channel_access_token")
            else None
        )
        self.proactive = ProactiveServer(self) if cfg.proactive_ready else None
        self.discord_bot = None       # set in run() when enabled
        self.session: aiohttp.ClientSession | None = None

    def db(self):
        return open_db(self.cfg.state_db)

    def _in_thread(self, fn, *args, **kwargs):
        """Run one store operation on its own connection, in a worker thread.

        A sqlite3 connection belongs to the thread that created it. Opening
        one on the event loop and then handing it to `asyncio.to_thread`
        raises

            ProgrammingError: SQLite objects created in a thread can only be
            used in that same thread

        which is exactly what happened in production on the first worker
        tick: the webhook kept accepting messages (its connection is opened
        inside the thread that uses it) while every one of them sat in the
        queue unprocessed, because the worker died on each pass.

        Opening inside the thread keeps a connection and its only user
        together, which is a stronger guarantee than `check_same_thread=
        False` would give — that flag silences the check without making the
        sharing safe.

        The values returned survive the close: `sqlite3.Row` holds its data,
        not a cursor.
        """
        def _run():
            con = self.db()
            try:
                result = fn(con, *args, **kwargs)
                con.commit()
                return result
            finally:
                con.close()

        return asyncio.to_thread(_run)

    # -- inbound: LINE ------------------------------------------------

    async def handle_line_webhook(self, request: web.Request) -> web.Response:
        max_body = int(self.cfg.line["max_body_bytes"])

        # Refuse an oversized body from the declared length, before reading
        # it. This runs BEFORE signature verification only because it never
        # accepts anything — it is a cheap ceiling, not an authorization
        # decision, and no unverified body reaches the parser either way.
        if request.content_length is not None and request.content_length > max_body:
            return web.Response(status=413, text="too large")

        raw = await request.read()
        if len(raw) > max_body:
            return web.Response(status=413, text="too large")

        signature = header_lookup(request.headers, SIGNATURE_HEADER)
        secret = self.secrets.get("line_channel_secret") or ""
        if not verify_signature(raw, signature, secret):
            # Not attributable to the configured channel. Nothing is parsed,
            # nothing is stored, and the answer says only "no".
            log.warning("line webhook rejected: bad signature (%d bytes)", len(raw))
            return web.Response(status=403, text="bad signature")

        try:
            payload = parse_webhook_body(raw)
        except Exception:
            log.warning("line webhook: signature valid but body is not JSON")
            return web.Response(status=400, text="bad body")

        events = extract_events(payload)
        if not events:
            # The console's "Verify" button posts a signed, empty payload and
            # expects 200. Treating it as an error makes the channel look
            # broken in the LINE console while being perfectly healthy.
            log.info("line webhook: verification probe accepted")
            return web.Response(status=200, text="OK")

        try:
            queued = await asyncio.to_thread(self._store_line_events, events)
        except Exception:
            # The row is not committed, so LINE's redelivery is the only
            # remaining copy. 500 asks for it; 200 would discard the message.
            log.exception("line webhook: enqueue failed, asking for redelivery")
            return web.Response(status=500, text="retry")

        if queued:
            self.wake.set()
        return web.Response(status=200, text="OK")

    def _store_line_events(self, events: list) -> int:
        """Synchronous, transactional. Returns how many became work."""
        from gw_canonical import canonical_from_line

        queued = 0
        con = self.db()
        try:
            for event in events:
                canonical = canonical_from_line(
                    event, owner_line_user_id=self.cfg.owner_line_user_id
                )
                if canonical is None:
                    continue
                request_id, is_new = store.record_inbound(con, canonical)
                if not canonical.authenticated:
                    store.audit(con, "inbound_rejected", request_id=request_id,
                                platform=PLATFORM_LINE, authenticated=False,
                                detail="not_owner")
                    continue
                if not is_new:
                    store.audit(con, "inbound_duplicate", request_id=request_id,
                                platform=PLATFORM_LINE,
                                detail="redelivery" if is_redelivery(event) else "dup")
                    continue
                store.enqueue(con, request_id)
                store.audit(con, "inbound_queued", request_id=request_id,
                            platform=PLATFORM_LINE, authenticated=True,
                            capability_profile=canonical.capability_profile)
                queued += 1
            con.commit()
        finally:
            con.close()
        return queued

    async def handle_health(self, request: web.Request) -> web.Response:
        """Local health only. Never routed publicly (sections 16, 22)."""
        snap = await self._in_thread(store.status_snapshot)
        return web.json_response({"status": "ok", "component": "channel-gateway",
                                  **snap})

    # -- the worker ---------------------------------------------------

    async def worker(self):
        max_active = int(self.cfg.limits["max_active_requests"])
        while not self.stop.is_set():
            row = None
            try:
                row = await self._in_thread(store.claim_next,
                                            max_active=max_active)
            except Exception:
                log.exception("worker: claim failed")

            if row is None:
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), IDLE_POLL_SECONDS)
                except asyncio.TimeoutError:
                    pass
                continue

            try:
                await self.process(row)
            except Exception:
                log.exception("worker: processing failed request_id=%s",
                              row["gateway_request_id"])
                await asyncio.to_thread(self._fail, row, "internal")

    async def process(self, row):
        request_id = row["gateway_request_id"]
        message = message_from_row(row)
        log.info("processing %s platform=%s conversation=%s",
                 request_id, message.platform, message.conversation_id)

        # Resume the immutable Discord reply before asking Hermes again. The
        # original chunks/target are authoritative across a process restart.
        if message.platform == PLATFORM_DISCORD:
            saved = await self._in_thread(store.outbound_for_request, request_id)
            if saved or row["state"] == "UNKNOWN":
                await self.deliver(row, message, None)
                return

        # A non-text message is answered briefly rather than ignored, and its
        # media is never fetched: nothing here calls LINE's content endpoint
        # or downloads a Discord attachment (spec section 39).
        if not message.text.strip():
            await self.deliver(row, message, unsupported_input_reply())
            return

        session_id = await self._in_thread(
            store.session_id_for, message.conversation_id, message.platform
        )

        envelope = build_envelope(message)
        try:
            result = await self.hermes.ask_async(
                envelope, message.text,
                session_id=session_id, request_id=request_id,
            )
        except HermesUnavailable as exc:
            log.warning("hermes unavailable for %s: %s", request_id, exc)
            await self._note_hermes(False)
            await self.deliver(row, message, user_facing_error("unavailable"))
            return
        except HermesRejected as exc:
            log.error("hermes rejected %s: %s", request_id, exc)
            await self._note_hermes(False)
            await self.deliver(row, message, user_facing_error("internal"))
            return

        if result.status == "timeout":
            await self._note_hermes(False)
            await self.deliver(row, message, user_facing_error("timeout"),
                               run_id=result.run_id)
            return
        if not result.ok or not result.text.strip():
            await self._note_hermes(False)
            await self.deliver(row, message, user_facing_error("internal"),
                               run_id=result.run_id)
            return

        await self._note_hermes(True)
        await self.deliver(row, message, result.text, run_id=result.run_id)

    async def _note_hermes(self, ok: bool):
        """Record whether the assistant itself answered (G.2 sections 39, 40).

        A RESPONDED row proves only that something was delivered, and
        "いま応答できません" is something. G.1's worst failure was a gateway
        that stayed connected, kept its queue empty and answered every single
        message with an error — indistinguishable from health by every metric
        that existed at the time. This is the metric that distinguishes them.
        """
        def _write(con):
            if ok:
                store.stamp(con, "last_hermes_success_at")
            else:
                store.stamp(con, "last_hermes_failure_at")
                store.bump(con, "hermes_failed")
        try:
            await self._in_thread(_write)
        except Exception:
            log.exception("could not record the hermes outcome")

    # -- outbound -----------------------------------------------------

    async def deliver(self, row, message: CanonicalMessage, text: str | None,
                      run_id=None):
        if message.platform == PLATFORM_LINE:
            ok, detail, mode = await self.deliver_line(row, message, text)
        else:
            ok, detail, mode = await self.deliver_discord(row, message, text)

        # Whether the answer actually reached the owner is the single most
        # important fact this service produces, and it was missing from the
        # journal entirely: the first real message showed only "processing"
        # and nothing about its fate. Log the outcome, the mode (reply is
        # free, push is metered) and the size — never the text.
        log.info("delivered %s platform=%s mode=%s ok=%s detail=%s chars=%d",
                 row["gateway_request_id"], message.platform, mode or "-",
                 ok, detail, len(text or ""))
        await asyncio.to_thread(self._settle, row, ok, detail, run_id, mode)

    def _settle(self, row, ok: bool, detail, run_id, mode=""):
        con = self.db()
        try:
            if row["platform"] == PLATFORM_DISCORD:
                con.execute("BEGIN IMMEDIATE")
                # A concurrent live sender keeps its claim; a held reply must
                # not become FAILED through a generic exception/settle path.
                if detail in {"in_flight", "reply_cancelled"}:
                    return
                current = store.get_inbound(con, row["gateway_request_id"])
                if current is not None and current["state"] == "UNKNOWN":
                    return
                reply = store.outbound_for_request(con, row["gateway_request_id"])
                if reply and all(r["state"] == "SENT" for r in reply):
                    ok, detail = True, "already_delivered"
                if any(r["state"] == "SENDING" for r in reply):
                    # This may be a NEW caller's claim after our known negative
                    # receipt. Settlement owns no send token, so it must not
                    # invalidate that claim. If our own receipt write failed,
                    # lease recovery will hold it once truly stale instead.
                    return
                if any(r["state"] == "UNKNOWN" for r in reply):
                    store._hold_discord_reply(con, row["gateway_request_id"],
                                              "delivery_outcome_unknown")
                    con.commit()
                    return
            store.set_state(con, row["gateway_request_id"],
                            "RESPONDED" if ok else "FAILED",
                            error=None if ok else detail, run_id=run_id)
            store.release_conversation(con, row["conversation_id"])
            store.audit(con, "responded" if ok else "response_failed",
                        request_id=row["gateway_request_id"],
                        platform=row["platform"],
                        detail=f"{mode}:{detail}"[:100] if mode
                        else str(detail or "")[:100])
            store.bump(con, "responded" if ok else "failed")
            # A metered push is worth its own counter: the free plan allows
            # 200 a month and `gw_status` reads this to show the month's use.
            if ok and mode == "push":
                store.bump(con, "line_push")
            con.commit()
        finally:
            con.close()

    def _fail(self, row, detail):
        self._settle(row, False, detail, None)

    async def deliver_line(self, row, message: CanonicalMessage, text: str):
        if self.line is None:
            return False, "line_not_configured", ""

        chunks = prepare_outbound(
            text,
            limit=int(self.cfg.line["text_chunk_chars"]),
            max_chunks=int(self.cfg.line["max_messages_per_request"]),
            neutralize=False,   # LINE has no @everyone to neutralise
            soft_limit=int(self.cfg.limits["response_soft_limit_chars"]),
            hard_limit=int(self.cfg.limits["response_hard_limit_chars"]),
        )
        if not chunks:
            return False, "empty_answer", ""

        # One row per chunk for the durable record, but ONE API call for all
        # of them: LINE accepts up to five message objects per request, and a
        # push is metered, so five separate pushes would cost five times what
        # one does.
        con = self.db()
        try:
            reserved = []
            for i, chunk in enumerate(chunks):
                oid, _is_new = store.reserve_outbound(
                    con, request_id=row["gateway_request_id"],
                    platform=PLATFORM_LINE,
                    conversation_id=row["conversation_id"],
                    target_user_id=self.cfg.owner_line_user_id,
                    body=chunk, chunk_index=i, chunk_total=len(chunks),
                    delivery_mode="pending",
                )
                reserved.append(oid)
            already = store.already_delivered(con, row["gateway_request_id"])
            con.commit()
        finally:
            con.close()

        if already:
            # A previous attempt got all the way through; sending again would
            # duplicate the answer and spend quota.
            log.info("line: already delivered %s", row["gateway_request_id"])
            return True, "already_delivered", "none"

        messages = build_text_messages(chunks)
        mode = choose_delivery_mode(message)
        result = None

        if mode == "reply":
            result = await self.line.reply(self.session, message.reply_token,
                                           messages)
            if not result.ok and result.category == "token":
                # Expired or already used — indistinguishable, and both are
                # answered the same way: spend a push rather than lose the
                # answer (sections 33, 34).
                log.info("line: reply token unusable for %s, pushing",
                         row["gateway_request_id"])
                await self._record_attempt(reserved[0], "reply", result)
                result = None

        if result is None:
            result = await self.line.push(
                self.session, self.cfg.owner_line_user_id, messages,
                outbound_id=reserved[0],
            )

        await self._record_attempt(reserved[0], result.mode, result)
        await asyncio.to_thread(self._mark_chunks, reserved, result)
        return result.ok, result.category, result.mode

    def _mark_chunks(self, outbound_ids, result):
        con = self.db()
        try:
            for oid in outbound_ids:
                store.mark_outbound(
                    con, oid, "SENT" if result.ok else "FAILED",
                    error=None if result.ok else result.category,
                    mode=result.mode,
                )
            con.commit()
        finally:
            con.close()

    async def _record_attempt(self, outbound_id, mode, result):
        def _write():
            con = self.db()
            try:
                store.record_attempt(
                    con, outbound_id, mode=mode,
                    outcome="ok" if result.ok else "error",
                    status_code=result.status, error_category=result.category,
                )
                con.commit()
            finally:
                con.close()
        await asyncio.to_thread(_write)

    async def deliver_discord(self, row, message: CanonicalMessage, text: str | None):
        if self.discord_bot is None:
            return False, "discord_not_configured", ""
        chunks = self.discord_bot.prepare(text) if text is not None else None
        status, _ = await self._in_thread(
            store.prepare_discord_reply,
            request_id=row["gateway_request_id"],
            conversation_id=row["conversation_id"],
            owner_id=self.cfg.owner_discord_user_id, chunks=chunks)
        if status != "ready":
            return status == "already_delivered", status, "dm"

        while True:
            status, chunk = await self._in_thread(
                store.claim_discord_chunk, row["gateway_request_id"])
            if status != "send":
                return status == "already_delivered", status, "dm"
            # The committed SENDING row precedes the only send call. One
            # chunk at a time makes each confirmed prefix durable immediately.
            channel_id = chunk["conversation_id"].split(":", 1)[1]
            try:
                sent, error = await self.discord_bot.send_chunks(channel_id, [chunk["body"]])
            except Exception as exc:
                sent, error = 0, type(exc).__name__
            # CancelledError intentionally leaves SENDING for stale recovery.
            if sent == 1 and error is None:
                state = "SENT"
            elif sent == 0 and discord_definitely_not_sent(error):
                state = "NOT_SENT"
            else:
                state = "UNKNOWN"
            recorded = await self._in_thread(
                store.record_discord_receipt, chunk["outbound_id"],
                state=state, error=error)
            if not recorded or state == "UNKNOWN":
                return False, "delivery_outcome_unknown", "dm"
            if state == "NOT_SENT":
                return False, error, "dm"

    # -- housekeeping -------------------------------------------------

    @staticmethod
    def _housekeep(con, *, retention_days: int, stuck_lease: int = 180) -> tuple:
        """Every periodic chore on one connection, inside one thread."""
        recovered = store.recover_stale(con)
        recovered.update(
            store.recover_stuck_proactive(con, lease_seconds=stuck_lease)
        )
        return recovered, store.purge_expired(con, retention_days=retention_days)

    async def maintenance(self):
        while not self.stop.is_set():
            try:
                recovered, purged = await self._in_thread(
                    self._housekeep,
                    retention_days=int(self.cfg.retention["days"]),
                    stuck_lease=int(self.cfg.proactive["stuck_lease_seconds"]),
                )
                # Both helpers return a dict of counters, and a dict of zeros
                # is truthy — so test the values, not the dict, or the log
                # claims a recovery every five minutes forever.
                if any((recovered or {}).values()):
                    log.warning("recovered stale request(s): %s", recovered)
                if any((purged or {}).values()):
                    log.info("retention purge: %s", purged)
            except Exception:
                log.exception("maintenance failed")
            try:
                await asyncio.wait_for(self.stop.wait(),
                                       MAINTENANCE_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass

    # -- lifecycle ----------------------------------------------------

    def _seed_quota_cache(self):
        """Adopt the last quota answer that survived the restart (section 80).

        Two things are restored, and the second matters more: the numbers, so
        a restart does not cost an API call, and the memory of a 401, so a
        crash-restart loop does not become a retry storm against a credential
        that will refuse it again (section 120).
        """
        if self.line_quota is None:
            return
        con = self.db()
        try:
            row = store.load_quota_cache(con)
        finally:
            con.close()
        if row is None:
            return
        try:
            fetched = datetime.fromisoformat(row["fetched_at"])
            if fetched.tzinfo is None:
                fetched = fetched.replace(tzinfo=timezone.utc)
            age = max(0.0, (datetime.now(timezone.utc) - fetched).total_seconds())
        except (TypeError, ValueError):
            return
        self.line_quota.seed(
            QuotaSnapshot(
                ok=bool(row["ok"]), quota_type=row["quota_type"],
                limit=row["limit_value"], used=row["used"],
                error_category=row["error_category"],
                fetched_at=row["fetched_at"],
            ),
            age_seconds=age,
        )
        log.info("line quota cache restored (age=%ds ok=%s)", int(age),
                 bool(row["ok"]))

    async def run(self):
        self.session = aiohttp.ClientSession()
        self._seed_quota_cache()
        tasks = [asyncio.create_task(self.worker(), name="worker"),
                 asyncio.create_task(self.maintenance(), name="maintenance")]
        runner = None

        if self.cfg.line_ready:
            app = web.Application(
                client_max_size=int(self.cfg.line["max_body_bytes"])
            )
            app.router.add_post(self.cfg.line["webhook_path"],
                                self.handle_line_webhook)
            app.router.add_get("/healthz", self.handle_health)
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            site = web.TCPSite(runner, self.cfg.line["listen_host"],
                               int(self.cfg.line["listen_port"]))
            await site.start()
            log.info("line webhook listening on %s:%s%s",
                     self.cfg.line["listen_host"], self.cfg.line["listen_port"],
                     self.cfg.line["webhook_path"])

        if self.cfg.discord_ready and self.secrets.get("discord_interface_token"):
            from gw_discord import InterfaceBot

            self.discord_bot = InterfaceBot(self.cfg, self.cfg.state_db, self.wake)
            tasks.append(asyncio.create_task(
                self.discord_bot.start(self.secrets["discord_interface_token"]),
                name="discord",
            ))
            log.info("discord interface bot starting (intents=%s)",
                     self.discord_bot.intents.value)

        if self.proactive is not None:
            # Started last: the socket is the signal that the gateway is ready
            # to accept proactive work, and it should not appear before the
            # worker and the channels that will carry it.
            await self.proactive.start(self.cfg.proactive["socket_path"])

        await self.stop.wait()
        log.info("shutting down")

        if self.proactive is not None:
            await self.proactive.stop()
        if self.discord_bot is not None:
            await self.discord_bot.close()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if runner is not None:
            await runner.cleanup()
        await self.session.close()


# ------------------------------------------------------------------- main


def load_all_secrets(cfg) -> dict:
    """Every credential the enabled channels need, and nothing more."""
    secrets = {"hermes_api_key": load_secret(cfg, "hermes_api_key")}
    if cfg.line_ready:
        secrets["line_channel_secret"] = load_secret(cfg, "line_channel_secret")
        secrets["line_channel_access_token"] = load_secret(
            cfg, "line_channel_access_token"
        )
    if cfg.discord_ready:
        secrets["discord_interface_token"] = load_secret(
            cfg, "discord_interface_token"
        )
    return secrets


def probe_hermes(cfg) -> tuple:
    """(reachable, detail) for the configured Hermes base_url.

    An unauthenticated GET of /health, which is enough to prove the ADDRESS
    is right — the failure this exists to catch. It needs no credential, so
    it can run before any secret is provisioned.
    """
    url = cfg.hermes["base_url"].rstrip("/") + "/health"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            body = resp.read(300).decode("utf-8", "replace").strip()
        return True, f"{url} -> {resp.status} {body[:140]}"
    except urllib.error.HTTPError as exc:
        # Answering at all means the address is right, whatever the status.
        return True, f"{url} -> HTTP {exc.code}"
    except Exception as exc:
        return False, f"{url} -> {type(exc).__name__}: {exc}"


def run_check(cfg) -> int:
    """Report everything a start would need, then say whether it would start.

    The diagnostics run BEFORE the readiness gate on purpose. An earlier
    version returned 78 for "no owner id" and stopped there, without ever
    testing whether the Hermes address worked — and on this host it did not:
    the API publishes its port on the LAN address only, so the sample's
    127.0.0.1 refused every connection. That would have surfaced as every
    answer failing at runtime, long after the credentials were set up.
    """
    ok = True
    as_root = getattr(os, "geteuid", lambda: 1)() == 0
    log.info("running as  : uid=%s%s",
             getattr(os, "geteuid", lambda: "?")(), " (root)" if as_root else "")

    # Everything the service owns is 0750 to the service user, so a check run
    # from another account legitimately cannot touch it. Report that; do not
    # die of it. The whole point of this command is to explain a deployment,
    # and it is worthless if the deployment's own permissions kill it.
    try:
        open_db(cfg.state_db).close()
        log.info("state db    : %s (openable)", cfg.state_db)
    except Exception as exc:
        log.info("state db    : %s (not openable from this account: %s)",
                 cfg.state_db, type(exc).__name__)
        if as_root:
            log.error("root cannot open the state database — check ownership "
                      "of %s", os.path.dirname(cfg.state_db))
            ok = False

    log.info("listen      : %s:%s%s", cfg.line["listen_host"],
             cfg.line["listen_port"], cfg.line["webhook_path"])

    reachable, detail = probe_hermes(cfg)
    log.info("hermes      : %s", detail)
    if not reachable:
        log.error("hermes is NOT reachable at the configured base_url — every "
                  "answer would fail. Check the address against what the API "
                  "actually listens on (ss -ltn | grep 8642).")
        ok = False

    log.info("channels    : line_ready=%s discord_ready=%s",
             cfg.line_ready, cfg.discord_ready)

    # G.2. Reported even when disabled, because "the engine cannot reach the
    # gateway" is otherwise a silent failure on the engine's side only.
    sock = cfg.proactive["socket_path"]
    sock_dir = os.path.dirname(sock)
    if cfg.proactive_ready:
        if os.path.isdir(sock_dir):
            try:
                st = os.stat(sock_dir)
                import grp
                import pwd
                log.info("proactive   : enabled, socket dir %s (%s:%s %o)",
                         sock_dir, pwd.getpwuid(st.st_uid).pw_name,
                         grp.getgrgid(st.st_gid).gr_name,
                         st.st_mode & 0o7777)
            except (KeyError, OSError):
                log.info("proactive   : enabled, socket dir %s exists", sock_dir)
        else:
            log.error("proactive is enabled but %s does not exist; create it "
                      "as root (owner: the gateway user, group: the engine's "
                      "account) before starting", sock_dir)
            ok = False
    else:
        log.info("proactive   : disabled (proactive.enabled=%s)",
                 cfg.proactive.get("enabled"))
    if not cfg.any_channel_ready:
        log.error("no channel is ready: set owner ids and enable a platform "
                  "in %s", cfg.path)
        ok = False

    # Presence only — never a value, never a length. `/etc/hermes-secrets` is
    # 0700 root:root by design, so a non-root check genuinely CANNOT tell a
    # missing file from an unreadable one: every name in a closed directory
    # answers EACCES. Say that plainly instead of guessing, and say which
    # account would be able to tell.
    needed = ["hermes_api_key"]
    if cfg.line.get("enabled"):
        needed += ["line_channel_secret", "line_channel_access_token"]
    if cfg.discord.get("enabled"):
        needed += ["discord_interface_token"]

    unverified = 0
    for name in needed:
        status, where = secret_status(cfg, name)
        log.info("credential  : %-26s %-22s %s", name, status, where)
        if status == SECRET_MISSING:
            log.error("credential %s is missing; install it before starting", name)
            ok = False
        elif status == SECRET_UNREADABLE:
            if as_root:
                # Root cannot read it either: that is a real permissions fault,
                # not the expected privilege boundary.
                log.error("credential %s exists but is unreadable even as root",
                          name)
                ok = False
            else:
                unverified += 1

    if unverified:
        log.warning(
            "%d credential(s) could not be checked from this account — "
            "/etc/hermes-secrets is root-only by design, and systemd hands the "
            "service its own copies at start. Re-run this check as root to "
            "verify they are installed.", unverified,
        )

    if ok and unverified:
        log.info("result      : would start, but %d credential(s) unverified "
                 "from here", unverified)
    else:
        log.info("result      : %s", "would start" if ok else "would NOT start")
    return 0 if ok else EXIT_CONFIG


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Unified Channel Gateway")
    ap.add_argument("--config", default=None)
    ap.add_argument("--check", action="store_true",
                    help="validate configuration and credentials, then exit")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    setup_logging(cfg.log_dir, args.log_level)

    if args.check:
        return run_check(cfg)

    if not cfg.any_channel_ready:
        log.error(
            "no channel is ready: set owner ids and enable a platform in %s",
            cfg.path,
        )
        return EXIT_CONFIG

    try:
        secrets = load_all_secrets(cfg)
    except ConfigError as exc:
        log.error("credential unavailable: %s", exc)
        return EXIT_CONFIG

    con = open_db(cfg.state_db)
    con.close()

    gw = Gateway(cfg, secrets)

    async def _run():
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, gw.stop.set)
            except NotImplementedError:  # pragma: no cover - non-POSIX
                pass
        await gw.run()

    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
