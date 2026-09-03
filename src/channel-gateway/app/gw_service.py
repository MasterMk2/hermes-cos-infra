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
mid-conversation lose nothing: the row survives, the worker picks it up.

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
from gw_config import ConfigError, load_config, load_secret
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
    """Rotating file plus stderr for the journal.

    Formatter carries no message body by construction — the call sites pass
    ids and categories. Anything that could hold user text belongs in the
    database, which is 0600 and on the same disk as the rest of the data.
    """
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s %(message)s"
    )
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    fh = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, "gateway.log"),
        maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root.addHandler(sh)
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
        self.discord_bot = None       # set in run() when enabled
        self.session: aiohttp.ClientSession | None = None

    def db(self):
        return open_db(self.cfg.state_db)

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
        con = self.db()
        try:
            snap = await asyncio.to_thread(store.status_snapshot, con)
        finally:
            con.close()
        return web.json_response({"status": "ok", "component": "channel-gateway",
                                  **snap})

    # -- the worker ---------------------------------------------------

    async def worker(self):
        max_active = int(self.cfg.limits["max_active_requests"])
        while not self.stop.is_set():
            row = None
            try:
                con = self.db()
                try:
                    row = await asyncio.to_thread(
                        store.claim_next, con, max_active=max_active
                    )
                    con.commit()
                finally:
                    con.close()
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

        # A non-text message is answered briefly rather than ignored, and its
        # media is never fetched: nothing here calls LINE's content endpoint
        # or downloads a Discord attachment (spec section 39).
        if not message.text.strip():
            await self.deliver(row, message, unsupported_input_reply())
            return

        con = self.db()
        try:
            session_id = await asyncio.to_thread(
                store.session_id_for, con, message.conversation_id, message.platform
            )
            con.commit()
        finally:
            con.close()

        envelope = build_envelope(message)
        try:
            result = await self.hermes.ask_async(
                envelope, message.text,
                session_id=session_id, request_id=request_id,
            )
        except HermesUnavailable as exc:
            log.warning("hermes unavailable for %s: %s", request_id, exc)
            await self.deliver(row, message, user_facing_error("unavailable"))
            return
        except HermesRejected as exc:
            log.error("hermes rejected %s: %s", request_id, exc)
            await self.deliver(row, message, user_facing_error("internal"))
            return

        if result.status == "timeout":
            await self.deliver(row, message, user_facing_error("timeout"),
                               run_id=result.run_id)
            return
        if not result.ok or not result.text.strip():
            await self.deliver(row, message, user_facing_error("internal"),
                               run_id=result.run_id)
            return

        await self.deliver(row, message, result.text, run_id=result.run_id)

    # -- outbound -----------------------------------------------------

    async def deliver(self, row, message: CanonicalMessage, text: str,
                      run_id=None):
        if message.platform == PLATFORM_LINE:
            ok, detail = await self.deliver_line(row, message, text)
        else:
            ok, detail = await self.deliver_discord(row, message, text)
        await asyncio.to_thread(self._settle, row, ok, detail, run_id)

    def _settle(self, row, ok: bool, detail, run_id):
        con = self.db()
        try:
            store.set_state(con, row["gateway_request_id"],
                            "RESPONDED" if ok else "FAILED",
                            error=None if ok else detail, run_id=run_id)
            store.release_conversation(con, row["conversation_id"])
            store.audit(con, "responded" if ok else "response_failed",
                        request_id=row["gateway_request_id"],
                        platform=row["platform"], detail=str(detail or "")[:100])
            store.bump(con, "responded" if ok else "failed")
            con.commit()
        finally:
            con.close()

    def _fail(self, row, detail):
        self._settle(row, False, detail, None)

    async def deliver_line(self, row, message: CanonicalMessage, text: str):
        if self.line is None:
            return False, "line_not_configured"

        chunks = prepare_outbound(
            text,
            limit=int(self.cfg.line["text_chunk_chars"]),
            max_chunks=int(self.cfg.line["max_messages_per_request"]),
            neutralize=False,   # LINE has no @everyone to neutralise
            soft_limit=int(self.cfg.limits["response_soft_limit_chars"]),
            hard_limit=int(self.cfg.limits["response_hard_limit_chars"]),
        )
        if not chunks:
            return False, "empty_answer"

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
            return True, "already_delivered"

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
        return result.ok, result.category

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

    async def deliver_discord(self, row, message: CanonicalMessage, text: str):
        if self.discord_bot is None:
            return False, "discord_not_configured"

        chunks = self.discord_bot.prepare(text)
        if not chunks:
            return False, "empty_answer"

        channel_id = str(row["conversation_id"]).split(":", 1)[1]
        con = self.db()
        try:
            reserved = []
            for i, chunk in enumerate(chunks):
                oid, _ = store.reserve_outbound(
                    con, request_id=row["gateway_request_id"],
                    platform=PLATFORM_DISCORD,
                    conversation_id=row["conversation_id"],
                    target_user_id=self.cfg.owner_discord_user_id,
                    body=chunk, chunk_index=i, chunk_total=len(chunks),
                    delivery_mode="dm",
                )
                reserved.append(oid)
            already = store.already_delivered(con, row["gateway_request_id"])
            con.commit()
        finally:
            con.close()

        if already:
            return True, "already_delivered"

        sent, error = await self.discord_bot.send_chunks(channel_id, chunks)

        def _write():
            con = self.db()
            try:
                for i, oid in enumerate(reserved):
                    ok = i < sent
                    store.mark_outbound(con, oid, "SENT" if ok else "FAILED",
                                        error=None if ok else error, mode="dm")
                    store.record_attempt(con, oid, mode="dm",
                                         outcome="ok" if ok else "error",
                                         error_category=error)
                con.commit()
            finally:
                con.close()
        await asyncio.to_thread(_write)
        return (error is None and sent == len(chunks)), error or "ok"

    # -- housekeeping -------------------------------------------------

    async def maintenance(self):
        while not self.stop.is_set():
            try:
                con = self.db()
                try:
                    recovered = await asyncio.to_thread(store.recover_stale, con)
                    purged = await asyncio.to_thread(
                        store.purge_expired, con,
                        retention_days=int(self.cfg.retention["days"]),
                    )
                    con.commit()
                    # Both helpers return a dict of counters, and a dict of
                    # zeros is truthy — so test the values, not the dict, or
                    # the log claims a recovery every five minutes forever.
                    if any((recovered or {}).values()):
                        log.warning("recovered stale request(s): %s", recovered)
                    if any((purged or {}).values()):
                        log.info("retention purge: %s", purged)
                finally:
                    con.close()
            except Exception:
                log.exception("maintenance failed")
            try:
                await asyncio.wait_for(self.stop.wait(),
                                       MAINTENANCE_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass

    # -- lifecycle ----------------------------------------------------

    async def run(self):
        self.session = aiohttp.ClientSession()
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

        await self.stop.wait()
        log.info("shutting down")

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

    open_db(cfg.state_db).close()
    log.info("state db    : %s", cfg.state_db)
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
    if not cfg.any_channel_ready:
        log.error("no channel is ready: set owner ids and enable a platform "
                  "in %s", cfg.path)
        ok = False

    try:
        secrets = load_all_secrets(cfg)
        # Names only. Printing a length would still say something about a
        # credential, and printing a value would be the credential.
        log.info("credentials : %s", sorted(secrets))
    except ConfigError as exc:
        log.error("credential unavailable: %s", exc)
        ok = False

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
