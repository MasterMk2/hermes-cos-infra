#!/usr/bin/env python3
"""Gmail read-only ingestion: Pub/Sub hint/poll -> history -> local snapshot."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import gm_index
import gm_store
import gm_sync
from gm_config import ConfigError, load_config
from gm_google import CredentialUnavailable, GmailApiError, GmailClient, OAuthCredentials
from gm_pubsub import PubSubError, PubSubPullClient, PubSubUnavailable, parse_notification
from gm_schema import connect, open_db


EXIT_CONFIG = 78
AUTH_FAILURE_STATUSES = {401, 403}


def setup_logging(log_dir: str) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    root.addHandler(stream)
    try:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            Path(log_dir) / "gmail.log", maxBytes=4 * 1024 * 1024, backupCount=4, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError as exc:
        root.warning("gmail file log unavailable: %s", type(exc).__name__)


class GmailService:
    def __init__(self, cfg, *, client_factory=None, pubsub_factory=None):
        self.cfg = cfg
        self.client_factory = client_factory or self._default_client
        self.pubsub_factory = pubsub_factory or self._default_pubsub
        self.owner_id = f"gmail-{uuid.uuid4().hex}"
        self.log = logging.getLogger("gmail.service")
        self._client = None
        self._pubsub = None
        self._last_poll = 0.0
        self.stop_requested = False
        con = open_db(self.cfg.paths["authority_db"])
        con.close()

    def db(self):
        return connect(self.cfg.paths["authority_db"])

    def _default_client(self):
        return GmailClient(OAuthCredentials.from_directory(self.cfg.credential_dir))

    def client(self):
        if self._client is None:
            self._client = self.client_factory()
        return self._client

    def _default_pubsub(self):
        if not self.cfg.pubsub["subscription_name"]:
            return None
        if not self.cfg.pubsub_credential_path or not os.path.isfile(self.cfg.pubsub_credential_path):
            raise PubSubUnavailable("pubsub_credential_unavailable")
        return PubSubPullClient(self.cfg.pubsub_credential_path, self.cfg.pubsub["subscription_name"])

    def pubsub(self):
        if self._pubsub is None:
            self._pubsub = self.pubsub_factory()
        return self._pubsub

    def _publish_index(self, con) -> None:
        gm_index.publish_index(con, self.cfg.paths["index_db"])

    def _profile(self) -> dict:
        profile = self.client().get_profile()
        if not isinstance(profile, dict) or not str(profile.get("historyId") or ""):
            raise GmailApiError(502, "profile_missing_history_id")
        con = self.db()
        try:
            with con:
                gm_store.record_api_success(con, mailbox_email=str(profile.get("emailAddress") or "") or None)
                self._publish_index(con)
        finally:
            con.close()
        return profile

    def drain_pubsub(self) -> int:
        """Durably enqueue valid notifications, then acknowledge them."""
        try:
            client = self.pubsub()
        except PubSubUnavailable as exc:
            self.log.warning("Pub/Sub pull unavailable: %s", str(exc))
            return 0
        if client is None:
            return 0
        received = client.pull(max_messages=int(self.cfg.pubsub["pull_batch_size"]))
        if not received:
            return 0
        con = self.db()
        ack_ids: list[str] = []
        accepted = 0
        try:
            with con:
                state = gm_store.state(con)
                mailbox = str(state["mailbox_email"] or "")
                for item in received:
                    ack = str(item.get("ackId") or "")
                    parsed = parse_notification(item)
                    if not parsed or not mailbox or parsed[1] != mailbox:
                        gm_store.increment_metric(con, "pubsub_rejected")
                        if ack:
                            ack_ids.append(ack)
                        continue
                    notification_id, _mailbox, history_id = parsed
                    if gm_store.accept_notification(con, message_id=notification_id, history_id=history_id):
                        inserted = gm_store.enqueue_sync(con, "pubsub")
                        gm_store.increment_metric(con, "pubsub_received")
                        if not inserted:
                            gm_store.increment_metric(con, "pubsub_coalesced")
                        accepted += 1
                    else:
                        gm_store.increment_metric(con, "pubsub_duplicate")
                    if ack:
                        ack_ids.append(ack)
                self._publish_index(con)
            # Ack only after the durable queue transaction commits.  A crash
            # before this point is safe: Pub/Sub redelivers and the ledger
            # makes it idempotent.
            client.acknowledge(ack_ids)
        finally:
            con.close()
        return accepted

    def enqueue(self, reason: str, *, force: bool = False) -> int:
        con = self.db()
        try:
            with con:
                inserted = gm_store.enqueue_sync(con, reason, force=force)
                self._publish_index(con)
                return int(inserted)
        finally:
            con.close()

    def renew_watch(self) -> bool:
        topic = str(self.cfg.pubsub["topic_name"] or "")
        if not topic:
            return False
        con = self.db()
        try:
            current = gm_store.watch(con)
            now_ms = int(time.time() * 1000)
            expiration = int(current["expiration_ms"] or 0)
            margin_ms = int(self.cfg.sync["watch_renewal_margin_seconds"]) * 1000
            if current["state"] == "ACTIVE" and expiration > now_ms + margin_ms:
                return False
            response = self.client().watch(topic)
            try:
                new_expiration = int(response.get("expiration")) if response.get("expiration") else None
            except (TypeError, ValueError):
                new_expiration = None
            if new_expiration is None:
                raise GmailApiError(502, "watch_missing_expiration")
            with con:
                gm_store.set_watch(con, topic_name=topic, expiration_ms=new_expiration)
                gm_store.increment_metric(con, "watch_renewals")
                self._publish_index(con)
            return True
        except GmailApiError as exc:
            with con:
                gm_store.record_watch_failure(con, exc.code)
                self._publish_index(con)
            raise
        finally:
            con.close()

    def process_one(self) -> bool:
        con = self.db()
        try:
            with con:
                queued = gm_store.claim_queue(con)
            if queued is None:
                return False
            with con:
                if not gm_store.acquire_lock(con, self.owner_id):
                    gm_store.retry_queue(con, "sync_locked", 15)
                    return True
            try:
                result = gm_sync.sync_mailbox(
                    con, self.client(), initial_days=int(self.cfg.mailbox["initial_days"])
                )
                with con:
                    gm_store.complete_queue(con)
                    gm_store.increment_metric(con, "sync_success")
                    self._publish_index(con)
                self.log.info(
                    "sync completed mode=%s fetched=%s", result["mode"], result.get("fetched", 0)
                )
            except (CredentialUnavailable, GmailApiError) as exc:
                status = int(getattr(exc, "status", 401))
                code = str(getattr(exc, "code", type(exc).__name__))
                with con:
                    gm_store.record_failure(con, code, auth_required=status in AUTH_FAILURE_STATUSES)
                    delay = int(self.cfg.sync["auth_retry_delay_seconds"] if status in AUTH_FAILURE_STATUSES
                                else min(int(self.cfg.sync["max_retry_delay_seconds"]),
                                         30 * (2 ** min(int(queued["attempts"] or 0) + 1, 5))))
                    gm_store.retry_queue(con, code, delay)
                    self._publish_index(con)
                self.log.warning("sync deferred code=%s", code)
            finally:
                with con:
                    gm_store.release_lock(con, self.owner_id)
            return True
        finally:
            con.close()

    def startup(self) -> None:
        con = self.db()
        prior_auth_state = ""
        try:
            with con:
                recovered = gm_store.recover_claim(con)
                recovered_locks = gm_store.recover_orphan_locks(con)
                recovered_staging = gm_store.recover_orphan_staging(con)
                prior_auth_state = str(gm_store.state(con)["auth_state"] or "")
                if recovered:
                    gm_store.increment_metric(con, "queue_claim_recoveries", recovered)
                if recovered_locks:
                    gm_store.increment_metric(con, "orphan_lock_recoveries", recovered_locks)
                if recovered_staging:
                    gm_store.increment_metric(con, "orphan_staging_rows_discarded", recovered_staging)
                self._publish_index(con)
        finally:
            con.close()
        try:
            self._profile()
            # A deliberate service restart after repairing credentials or
            # IAM must not wait out an old six-hour auth backoff.  Normal
            # polling keeps the bounded backoff unchanged.
            self.enqueue("startup", force=prior_auth_state == "AUTH_REQUIRED")
            self.renew_watch()
        except CredentialUnavailable:
            self.log.warning("OAuth credentials are not installed; Gmail is waiting for human authorization")
        except GmailApiError as exc:
            self.log.warning("Gmail startup deferred code=%s", exc.code)

    def run_cycle(self) -> None:
        # A durable startup/recovery request must not wait behind a pull
        # request.  Pull is a hint transport, whereas the sync queue contains
        # canonical work that survived a prior crash.
        while self.process_one():
            pass
        try:
            self.drain_pubsub()
        except (PubSubError, PubSubUnavailable) as exc:
            self.log.warning("Pub/Sub deferred code=%s", getattr(exc, "code", str(exc)))
        now = time.monotonic()
        if now - self._last_poll >= int(self.cfg.sync["poll_interval_seconds"]):
            self.enqueue("poll")
            self._last_poll = now
            try:
                self.renew_watch()
            except (CredentialUnavailable, GmailApiError) as exc:
                self.log.warning("watch renewal deferred code=%s", getattr(exc, "code", type(exc).__name__))
        # Process a just-enqueued Pub/Sub or polling hint immediately rather
        # than delaying it until the following worker tick.
        while self.process_one():
            pass

    def run_forever(self) -> None:
        self.startup()
        while not self.stop_requested:
            self.run_cycle()
            time.sleep(int(self.cfg.sync["worker_tick_seconds"]))


def selftest() -> int:
    if os.geteuid() == 0:
        print("gmail selftest must run as the service user, not root", file=sys.stderr)
        return 2
    protected = (
        "/srv/hermes/hermes-data/.env",
        "/etc/hermes-secrets/google-calendar/refresh-token",
        "/etc/hermes-secrets/line-channel-access-token",
        "/etc/hermes-secrets/discord-interface-token",
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
        print("gmail isolation failed: protected paths reachable", file=sys.stderr)
        return 1
    print("gmail isolation: protected paths inaccessible")
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
        print(f"gmail config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    if args.check:
        print("gmail configuration valid; no credentials or network checked")
        return 0
    setup_logging(cfg.paths["log_dir"])
    service = GmailService(cfg)
    if args.once:
        service.startup()
        try:
            service.drain_pubsub()
        except (PubSubError, PubSubUnavailable):
            pass
        while service.process_one():
            pass
        return 0
    try:
        service.run_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
