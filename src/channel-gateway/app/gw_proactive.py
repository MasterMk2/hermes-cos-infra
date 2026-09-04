#!/usr/bin/env python3
"""Unified Channel Gateway — the proactive (owner-notification) surface.

G.1 gave the gateway one job: answer the owner when the owner speaks. G.2 adds
the opposite direction — the Attention Engine decides something is worth
saying and asks the gateway to say it. The whole of the security design is in
what this API *cannot* express (spec sections 48-51):

* **There is no recipient.** `send_owner_notification` takes a channel, not an
  address. The `to` field of the LINE push and the Discord user id come from
  the gateway's own configuration, exactly as they do on the reply path. An
  unknown key in the request body is a rejection rather than an ignored
  field, so a future `recipient_id` cannot be quietly accepted by a caller
  that got ahead of the server.

* **There is no raw send.** The only verbs are "deliver this notification",
  "how much LINE budget is left" and "how is the pipe doing". Nothing here
  reaches an arbitrary endpoint, and nothing here accepts a message object,
  a payload template or a URL.

* **The engine never sees a token.** It asks for the LINE budget and gets
  numbers. The channel access token stays in this process, in `LineQuotaClient`
  and `LineClient`, both of which refuse to render it.

* **The transport is a unix socket, not a port.** The two platforms cannot
  reach it; neither can anything on the LAN. Access is controlled by the
  permissions on the directory that holds it, which is the same mechanism
  D.1 used to let one account read another service's state directory.
  Nothing is mounted into the Hermes container, so the assistant itself —
  the component most exposed to hostile text — has no route here at all.

Idempotency is the other half. `proactive_deliveries` is unique on
(notification_id, channel), so a crash between the send and the state write
cannot produce a second charged LINE push: the retry computes the same
delivery id, finds the row, and the LINE retry key derived from that id makes
LINE answer 409 for a message it already accepted (sections 46, 86, 87).
"""

from __future__ import annotations

import json
import logging
import os
import stat

from aiohttp import web

import gw_store as store
from gw_format import prepare_outbound
from gw_line import build_text_messages
from gw_schema import PROACTIVE_CHANNELS, SEVERITIES, open_db

log = logging.getLogger("gateway.proactive")

CHANNEL_DISCORD = "discord"
CHANNEL_LINE = "line"

#: The engine composes short notifications by design (section 68). This is a
#: ceiling that stops a malformed one from becoming a five-chunk LINE push,
#: not a target.
MAX_NOTIFICATION_CHARS = 1200

MAX_REQUEST_BYTES = 16384

#: Exactly the fields `send_owner_notification` accepts. Anything else is a
#: 400, because the interesting extra field is the one that names a recipient.
ALLOWED_FIELDS = frozenset({
    "notification_id", "preferred_channel", "severity", "text", "reason",
    "dedupe_key",
})
REQUIRED_FIELDS = ("notification_id", "preferred_channel", "severity", "text")

#: Ids are ours to honour, not to parse. Bound the shape anyway so a
#: pathological value cannot become a 4 KB primary key or a log injection.
ID_MAX = 120
ID_ALLOWED = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-"
)


class ProactiveRejected(Exception):
    """The request is malformed. Never retried, never delivered."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _clean_id(value, what: str) -> str:
    s = str(value or "").strip()
    if not s or len(s) > ID_MAX or not set(s) <= ID_ALLOWED:
        raise ProactiveRejected(f"{what}_invalid")
    return s


def parse_notify_request(body) -> dict:
    """Validate a notify request into exactly the fields the sender may set.

    Pure, so the refusals are testable without a socket — including the one
    that matters most, which is that a body carrying `recipient_id` or
    `to` is refused rather than having the extra field dropped (section 126).
    """
    if not isinstance(body, dict):
        raise ProactiveRejected("body_not_object")
    unknown = sorted(set(body) - ALLOWED_FIELDS)
    if unknown:
        raise ProactiveRejected(f"unknown_field:{unknown[0]}")
    for name in REQUIRED_FIELDS:
        if name not in body:
            raise ProactiveRejected(f"missing:{name}")

    channel = str(body["preferred_channel"] or "").strip().lower()
    if channel not in PROACTIVE_CHANNELS:
        raise ProactiveRejected("channel_invalid")

    severity = str(body["severity"] or "").strip().upper()
    if severity not in SEVERITIES:
        raise ProactiveRejected("severity_invalid")

    text = body["text"]
    if not isinstance(text, str) or not text.strip():
        raise ProactiveRejected("text_empty")
    if len(text) > MAX_NOTIFICATION_CHARS:
        raise ProactiveRejected("text_too_long")

    reason = body.get("reason")
    dedupe_key = body.get("dedupe_key")
    for value, what in ((reason, "reason"), (dedupe_key, "dedupe_key")):
        if value is not None and (not isinstance(value, str) or len(value) > 200):
            raise ProactiveRejected(f"{what}_invalid")

    return {
        "notification_id": _clean_id(body["notification_id"], "notification_id"),
        "channel": channel,
        "severity": severity,
        "text": text,
        "reason": reason,
        "dedupe_key": dedupe_key,
    }


# --------------------------------------------------------------- claim

CLAIMED = "claimed"
ALREADY_SENT = "already_sent"
IN_FLIGHT = "in_flight"
CONTENT_MISMATCH = "content_mismatch"


def claim(con, *, notification_id: str, channel: str, severity: str,
          dedupe_key, reason, body: str, target_user_id: str) -> tuple:
    """Reserve-and-claim in one transaction. Returns (row, outcome).

    Splitting reserve from claim would leave a window in which two requests
    for the same notification both see PENDING and both send. One statement
    sequence on one connection closes it, and SQLite's write lock does the
    rest — the gateway is one process, so this is the only writer.
    """
    row, is_new = store.reserve_proactive(
        con, notification_id=notification_id, channel=channel,
        severity=severity, dedupe_key=dedupe_key, reason=reason,
        body=body, target_user_id=target_user_id,
    )
    if not is_new:
        state = row["state"]
        if state == "SENT":
            return row, ALREADY_SENT
        if state == "SENDING":
            return row, IN_FLIGHT
        if row["content_revision"] != store.content_revision(body):
            # Same notification id, different words. That is a second
            # notification wearing the first one's name, and honouring it
            # would let a caller re-send under an id already spent. Refuse
            # rather than guess which text was meant (section 46).
            return row, CONTENT_MISMATCH
    store.mark_proactive(con, row["delivery_id"], "SENDING", count_attempt=True)
    return store.get_proactive(con, row["delivery_id"]), CLAIMED


# ------------------------------------------------------------- server


class ProactiveServer:
    """The unix-socket API. Owns no credential and no policy — just routes."""

    def __init__(self, gateway):
        self.gw = gateway
        self._runner = None
        self._site = None

    # -- delivery ------------------------------------------------------

    async def _send_discord(self, row) -> tuple:
        """(ok, mode, detail). The owner's DM channel, opened if needed."""
        bot = self.gw.discord_bot
        if bot is None:
            return False, "", "discord_not_configured"
        chunks = bot.prepare(row["body"])
        if not chunks:
            return False, "", "empty_after_preparation"
        sent, error = await bot.send_owner_dm(chunks)
        if error is None and sent == len(chunks):
            return True, "dm", "ok"
        return False, "dm", error or "partial"

    async def _send_line(self, row) -> tuple:
        """(ok, mode, detail). Always a push: nobody replied to us.

        The retry key is derived from `delivery_id`, which is derived from
        (notification_id, channel) — so a retry of this exact notification is
        the same key, and LINE deduplicates it for 24 hours instead of
        charging a second message (sections 87, 122).
        """
        client = self.gw.line
        if client is None or not self.gw.cfg.owner_line_user_id:
            return False, "", "line_not_configured"
        chunks = prepare_outbound(
            row["body"],
            limit=int(self.gw.cfg.line["text_chunk_chars"]),
            max_chunks=int(self.gw.cfg.line["max_messages_per_request"]),
            neutralize=False,
            soft_limit=int(self.gw.cfg.limits["response_soft_limit_chars"]),
            hard_limit=int(self.gw.cfg.limits["response_hard_limit_chars"]),
        )
        if not chunks:
            return False, "", "empty_after_preparation"
        result = await client.push(
            self.gw.session, self.gw.cfg.owner_line_user_id,
            build_text_messages(chunks), outbound_id=row["delivery_id"],
        )
        return result.ok, "push", result.category

    # -- routes --------------------------------------------------------

    async def handle_notify(self, request: web.Request) -> web.Response:
        raw = await request.content.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            return web.json_response(
                {"status": "rejected", "detail": "body_too_large"}, status=413
            )
        try:
            req = parse_notify_request(json.loads(raw.decode("utf-8")))
        except ProactiveRejected as exc:
            log.warning("proactive notify rejected: %s", exc.reason)
            return web.json_response(
                {"status": "rejected", "detail": exc.reason}, status=400
            )
        except Exception:
            return web.json_response(
                {"status": "rejected", "detail": "body_not_json"}, status=400
            )

        channel = req["channel"]
        # The recipient. Read from configuration, never from the request —
        # there is no field in the request that could carry one.
        target = (self.gw.cfg.owner_line_user_id if channel == CHANNEL_LINE
                  else self.gw.cfg.owner_discord_user_id)
        if not target:
            return web.json_response(
                {"status": "failed", "channel": channel,
                 "detail": f"{channel}_owner_not_configured"}, status=503
            )

        row, outcome = await self.gw._in_thread(
            claim, notification_id=req["notification_id"], channel=channel,
            severity=req["severity"], dedupe_key=req["dedupe_key"],
            reason=req["reason"], body=req["text"], target_user_id=str(target),
        )

        if outcome == ALREADY_SENT:
            log.info("proactive duplicate suppressed %s channel=%s",
                     req["notification_id"], channel)
            return web.json_response({
                "status": "duplicate", "delivery_id": row["delivery_id"],
                "channel": channel, "mode": row["delivery_mode"],
                "detail": "already_delivered",
                "quota_consumed": bool(row["quota_consumed"]),
                "sent_at": row["sent_at"],
            })
        if outcome == IN_FLIGHT:
            return web.json_response({
                "status": "in_flight", "delivery_id": row["delivery_id"],
                "channel": channel, "detail": "delivery_in_progress",
            }, status=409)
        if outcome == CONTENT_MISMATCH:
            return web.json_response({
                "status": "rejected", "delivery_id": row["delivery_id"],
                "channel": channel, "detail": "content_revision_mismatch",
            }, status=409)

        if channel == CHANNEL_LINE:
            ok, mode, detail = await self._send_line(row)
        else:
            ok, mode, detail = await self._send_discord(row)

        # A LINE push is charged whether or not we are pleased with it; an
        # `already_sent` (409 on the retry key) means the charge happened on
        # the earlier attempt, so it is delivered but not charged again.
        quota_consumed = bool(
            channel == CHANNEL_LINE and ok and detail != "already_sent"
        )
        await self.gw._in_thread(
            store.mark_proactive, row["delivery_id"],
            "SENT" if ok else "FAILED",
            error=None if ok else detail, mode=mode,
            quota_consumed=quota_consumed,
        )
        if quota_consumed and self.gw.line_quota is not None:
            # Keep the cached figure honest between refreshes (section 80).
            self.gw.line_quota.note_push_sent(1)

        log.info("proactive %s %s channel=%s mode=%s ok=%s detail=%s chars=%d",
                 "delivered" if ok else "failed", req["notification_id"],
                 channel, mode or "-", ok, detail, len(req["text"]))

        return web.json_response({
            "status": "delivered" if ok else "failed",
            "delivery_id": row["delivery_id"], "channel": channel,
            "mode": mode, "detail": detail, "quota_consumed": quota_consumed,
        }, status=200 if ok else 502)

    async def handle_line_budget(self, request: web.Request) -> web.Response:
        """Facts about the monthly allowance. No token, ever."""
        if self.gw.line_quota is None:
            return web.json_response({
                "ok": False, "error_category": "line_not_configured",
                "quota_type": None, "limit": None, "used": None,
                "remaining": None, "unlimited": False, "fetched_at": None,
                "stale": False, "ledger": await self.gw._in_thread(
                    store.proactive_summary),
            })
        force = request.query.get("force") in ("1", "true", "yes")
        snap = await self.gw.line_quota.get(self.gw.session, force=force)
        await self.gw._in_thread(store.save_quota_cache, snap)
        payload = snap.as_dict()
        payload["ledger"] = await self.gw._in_thread(store.proactive_summary)
        return web.json_response(payload)

    async def handle_liveness(self, request: web.Request) -> web.Response:
        """Whether work is actually moving, not whether a process is alive.

        Section 39 is the reason this exists as its own route rather than a
        200 from /healthz: in G.1 the gateway kept answering 200 while its
        worker died on every pass, and a health check that could not tell the
        difference reported success throughout.
        """
        data = await self.gw._in_thread(store.liveness)
        data["discord_connected"] = bool(
            self.gw.discord_bot is not None
            and not self.gw.discord_bot.is_closed()
            and self.gw.discord_bot.is_ready()
        )
        data["line_enabled"] = bool(self.gw.cfg.line_ready)
        data["discord_enabled"] = bool(self.gw.cfg.discord_ready)
        return web.json_response(data)

    async def handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "component": "proactive"})

    # -- lifecycle -----------------------------------------------------

    def build_app(self) -> web.Application:
        app = web.Application(client_max_size=MAX_REQUEST_BYTES)
        app.router.add_post("/internal/notify", self.handle_notify)
        app.router.add_get("/internal/line_budget", self.handle_line_budget)
        app.router.add_get("/internal/liveness", self.handle_liveness)
        app.router.add_get("/internal/health", self.handle_health)
        return app

    async def start(self, socket_path: str) -> None:
        """Bind the unix socket, replacing a stale one left by a crash.

        An unclean shutdown leaves the socket file behind and `bind` then
        fails with EADDRINUSE — which would take the whole gateway down on
        the next start over a file nothing is listening on. Unlink it first,
        but only after confirming it IS a socket: refusing to delete a
        regular file here is what stops a misconfigured path from destroying
        something else.
        """
        path = str(socket_path)
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            raise RuntimeError(
                f"proactive socket directory does not exist: {parent} "
                "(create it as root, owned by the gateway user with the "
                "engine's group, before enabling the proactive API)"
            )
        try:
            st = os.stat(path)
        except FileNotFoundError:
            st = None
        if st is not None:
            if not stat.S_ISSOCK(st.st_mode):
                raise RuntimeError(
                    f"proactive socket path exists and is not a socket: {path}"
                )
            os.unlink(path)

        self._runner = web.AppRunner(self.build_app(), access_log=None)
        await self._runner.setup()
        self._site = web.UnixSite(self._runner, path)
        await self._site.start()
        try:
            # The directory permissions are the real gate; this only makes
            # sure the socket itself is not world-writable if the umask is
            # ever loosened.
            os.chmod(path, 0o660)
        except OSError:
            log.warning("could not chmod the proactive socket; relying on the "
                        "directory permissions")
        log.info("proactive api listening on %s (mode=%o)", path,
                 stat.S_IMODE(os.stat(path).st_mode))

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
