#!/usr/bin/env python3
"""Calendar Sync Service: verified webhook hint -> durable sync -> index.

The webhook intentionally never interprets a request body as an event.  It
validates the Google channel headers, persists one coalesced queue entry, and
returns immediately; Calendar API + syncToken remain the source of truth.
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import socketserver
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cal_index
import cal_reconcile
import cal_store
import cal_sync
from cal_config import ConfigError, load_config
from cal_google import CredentialUnavailable, GoogleCalendarClient, OAuthCredentials
from cal_schema import connect, open_db


EXIT_CONFIG = 78
AUTH_FAILURE_STATUSES = {401, 403}


def setup_logging(log_dir: str) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(fmt)
    root.addHandler(stderr)
    try:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            Path(log_dir) / "calendar.log", maxBytes=4 * 1024 * 1024, backupCount=4, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError as exc:
        root.warning("calendar file log unavailable: %s", type(exc).__name__)


class CalendarService:
    def __init__(self, cfg, *, client_factory=None):
        self.cfg = cfg
        self.client_factory = client_factory or self._default_client
        self.owner_id = f"calendar-{uuid.uuid4().hex}"
        self.stop_requested = False
        self.log = logging.getLogger("calendar.service")
        self._client = None
        self._last_poll = 0.0
        # Initialise WAL/schema exactly once.  Queue-worker connections below
        # must not repeatedly change database-wide journal mode.
        con = open_db(self.cfg.paths["authority_db"])
        con.close()

    def db(self):
        return connect(self.cfg.paths["authority_db"])

    def _default_client(self):
        credentials = OAuthCredentials.from_directory(self.cfg.credential_dir)
        return GoogleCalendarClient(credentials)

    def client(self):
        if self._client is None:
            self._client = self.client_factory()
        return self._client

    def discover(self) -> int:
        """Refresh CalendarList metadata while retaining default-deny selection."""
        client = self.client()
        selected_ids = set(self.cfg.calendars["selected_ids"])
        token = None
        items = []
        while True:
            page = client.list_calendars(**({"pageToken": token} if token else {}))
            items.extend(page.get("items") or [])
            token = page.get("nextPageToken")
            if not token:
                break
        con = self.db()
        try:
            with con:
                for item in items:
                    cal_store.upsert_calendar(con, item, selected=str(item.get("id")) in selected_ids)
            return len(items)
        finally:
            con.close()

    def _publish_index(self, con) -> None:
        cal_index.publish_index(con, self.cfg.paths["index_db"])

    def enqueue_selected(self, reason: str) -> int:
        con = self.db()
        try:
            with con:
                count = 0
                for row in cal_store.selected_calendars(con):
                    count += int(cal_store.enqueue_sync(con, row["calendar_id"], reason=reason))
                self._publish_index(con)
            return count
        finally:
            con.close()

    def _projection_due(self, state) -> bool:
        raw = state["last_projection_at"]
        if not raw:
            return True
        try:
            stamp = datetime.fromisoformat(str(raw))
            return (datetime.now(timezone.utc) - stamp).total_seconds() >= int(self.cfg.sync["projection_refresh_seconds"])
        except ValueError:
            return True

    def process_one(self) -> bool:
        con = self.db()
        try:
            with con:
                queued = cal_store.claim_queue(con)
            if queued is None:
                return False
            calendar_id = str(queued["calendar_id"])
            with con:
                if not cal_store.acquire_lock(con, calendar_id, self.owner_id):
                    cal_store.retry_queue(con, calendar_id, code="sync_locked", delay_seconds=15)
                    return True
            try:
                result = cal_sync.sync_calendar(con, self.client(), calendar_id)
                state = cal_store.sync_state(con, calendar_id)
                if result["mode"] == "full" or result.get("fetched", 0) or self._projection_due(state):
                    cal_sync.refresh_projection(
                        con, self.client(), calendar_id,
                        past_days=int(self.cfg.sync["projection_past_days"]),
                        future_days=int(self.cfg.sync["projection_future_days"]),
                    )
                projected = str(self.cfg.paths.get("reconciliation_index_db") or "")
                events_db = projected if projected and os.path.isfile(projected) else str(self.cfg.paths.get("event_layer_db") or "")
                if events_db and os.path.isfile(events_db):
                    cal_reconcile.reconcile_calendar(con, events_db=events_db, calendar_id=calendar_id)
                with con:
                    cal_store.complete_queue(con, calendar_id)
                    cal_store.increment_metric(con, "sync_success")
                    self._publish_index(con)
                self.log.info("sync completed calendar=%s mode=%s fetched=%s", calendar_id, result["mode"], result.get("fetched", 0))
            except (CredentialUnavailable, cal_sync.CalendarApiError) as exc:
                status = getattr(exc, "status", 401)
                code = getattr(exc, "code", type(exc).__name__)
                with con:
                    cal_store.record_sync_failure(con, calendar_id, str(code))
                    if int(status) in AUTH_FAILURE_STATUSES:
                        cal_store.set_auth_state(con, calendar_id, "AUTH_REQUIRED")
                        cal_store.retry_queue(con, calendar_id, code=str(code), delay_seconds=21600)
                    else:
                        attempts = int(queued["attempts"] or 0) + 1
                        delay = min(int(self.cfg.sync["max_retry_delay_seconds"]), 30 * (2 ** min(attempts, 5)))
                        cal_store.retry_queue(con, calendar_id, code=str(code), delay_seconds=delay)
                    self._publish_index(con)
                # Do not include API response text or calendar content in logs.
                self.log.warning("sync deferred calendar=%s code=%s", calendar_id, code)
            finally:
                with con:
                    cal_store.release_lock(con, calendar_id, self.owner_id)
            return True
        finally:
            con.close()

    def renew_watches(self) -> int:
        callback = str(self.cfg.server["public_callback_url"] or "")
        if not callback:
            return 0
        now_ms = int(time.time() * 1000)
        con = self.db()
        try:
            due = cal_store.watch_due_for_renewal(
                con, now_ms=now_ms, margin_ms=int(self.cfg.sync["watch_renewal_margin_seconds"]) * 1000
            )
            # No watch exists yet: selected calendars also need first creation.
            active = {str(row["calendar_id"]) for row in con.execute("SELECT calendar_id FROM watch_channels WHERE state='ACTIVE'")}
            selected = [str(row["calendar_id"]) for row in cal_store.selected_calendars(con)]
            ids = list(dict.fromkeys([str(row["calendar_id"]) for row in due] + [x for x in selected if x not in active]))
            renewed = 0
            for calendar_id in ids:
                channel_id = uuid.uuid4().hex
                channel_token = cal_store.new_watch_token()
                response = self.client().watch_events(
                    calendar_id, channel_id=channel_id, channel_token=channel_token,
                    callback_url=callback, ttl_seconds=int(self.cfg.sync["watch_ttl_seconds"]),
                )
                resource_id = str(response.get("resourceId") or "")
                if not resource_id:
                    raise cal_sync.CalendarApiError(502, "watch_missing_resource_id")
                try:
                    expiration_ms = int(response.get("expiration")) if response.get("expiration") else None
                except (TypeError, ValueError):
                    expiration_ms = None
                with con:
                    cal_store.register_watch(
                        con, calendar_id=calendar_id, channel_id=channel_id, resource_id=resource_id,
                        channel_token=channel_token, expiration_ms=expiration_ms,
                    )
                    cal_store.increment_metric(con, "watch_renewals")
                renewed += 1
            if renewed:
                with con:
                    self._publish_index(con)
            return renewed
        finally:
            con.close()

    def handle_webhook(self, headers: dict) -> bool:
        con = self.db()
        try:
            with con:
                calendar_id = cal_store.verify_watch_headers(con, headers)
                if not calendar_id:
                    cal_store.increment_metric(con, "push_rejected")
                    return False
                inserted = cal_store.enqueue_sync(con, calendar_id, reason="push")
                cal_store.increment_metric(con, "push_received")
                if not inserted:
                    cal_store.increment_metric(con, "push_coalesced")
            return True
        finally:
            con.close()

    def startup(self) -> None:
        """Discover and queue only explicit allowlisted calendars, or stay human-gated."""
        # Publish a valid empty snapshot before authorization so the MCP and
        # Attention report "not configured" rather than an invalid SQLite
        # file. It contains no OAuth credential and no event content.
        con = self.db()
        try:
            with con:
                recovered = cal_store.recover_abandoned_claims(con)
                for _ in range(recovered):
                    cal_store.increment_metric(con, "queue_claim_recoveries")
                self._publish_index(con)
        finally:
            con.close()
        if recovered:
            self.log.warning("recovered %s durable sync claim(s) from a stopped predecessor", recovered)
        try:
            self.discover()
            self.enqueue_selected("startup")
            self.renew_watches()
        except CredentialUnavailable:
            self.log.warning("OAuth credentials are not installed; Calendar service is waiting for human authorization")
        except cal_sync.CalendarApiError as exc:
            self.log.warning("Calendar startup deferred: %s", getattr(exc, "code", type(exc).__name__))

    def run_forever(self) -> None:
        self.startup()
        while not self.stop_requested:
            now = time.monotonic()
            if now - self._last_poll >= int(self.cfg.sync["poll_interval_seconds"]):
                self.enqueue_selected("poll")
                self._last_poll = now
                try:
                    self.renew_watches()
                except (CredentialUnavailable, cal_sync.CalendarApiError) as exc:
                    self.log.warning("watch renewal deferred: %s", getattr(exc, "code", type(exc).__name__))
            while self.process_one():
                pass
            time.sleep(int(self.cfg.sync["worker_tick_seconds"]))


def make_handler(service: CalendarService):
    class Handler(BaseHTTPRequestHandler):
        server_version = "HermesCalendarWebhook/1"

        def log_message(self, *_args):
            # Headers and request path must not land in an access log.
            return

        def do_POST(self):
            if self.path != service.cfg.server["path"]:
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_error(400)
                return
            if length < 0 or length > int(service.cfg.server["max_body_bytes"]):
                self.send_error(413)
                return
            if length:
                self.rfile.read(length)  # Google pushes are hints; body is ignored.
            if service.handle_webhook(dict(self.headers.items())):
                self.send_response(204)
            else:
                self.send_error(403)
            self.end_headers()

        def do_GET(self):
            self.send_error(405)

    return Handler


def selftest() -> int:
    if os.geteuid() == 0:
        print("calendar selftest must run as the service user, not root", file=sys.stderr)
        return 2
    protected = (
        "/srv/hermes/hermes-data/.env",
        "/etc/hermes-secrets/line-channel-access-token",
        "/etc/hermes-secrets/discord-interface-token",
        "/etc/hermes-secrets/commandcode-key",
        "/etc/hermes-secrets/myline-ssh-key",
        "/srv/hermes/coding-worker",
        "/run/docker.sock",
    )
    readable = []
    for path in protected:
        try:
            with open(path, "rb"):
                readable.append(path)
        except OSError:
            pass
    if readable:
        print("calendar isolation failed: protected paths reachable", file=sys.stderr)
        return 1
    print("calendar isolation: protected paths inaccessible")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=None)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)
    if args.selftest:
        return selftest()
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"calendar config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    if args.check:
        print("calendar configuration valid; no credentials or network checked")
        return 0
    setup_logging(cfg.paths["log_dir"])
    service = CalendarService(cfg)
    if args.once:
        service.startup()
        while service.process_one():
            pass
        return 0
    httpd = ThreadingHTTPServer((cfg.server["bind_host"], int(cfg.server["port"])), make_handler(service))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        service.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
