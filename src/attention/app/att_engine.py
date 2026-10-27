#!/usr/bin/env python3
"""Attention Engine — the service.

One process, one loop, one job: every `scan_interval_seconds`, look at the
Event Layer, the coding jobs and the health of the system; decide what (if
anything) is worth saying; and ask the gateway to say it.

The shape of a scan is deliberately boring:

    collect   ->  three read-only SQLite reads and a few probes
    decide    ->  deterministic policy, one row per decision, with a reason
    deliver   ->  the gateway's unix socket, owner-fixed, idempotent
    digest    ->  whatever was held back, folded into one message

A scan that finds nothing does exactly three database reads and stops. No
network call, no model, no notification, no row written except the scan record
(sections 21, 93). That is the normal case and it has to stay cheap: this runs
288 times a day on a Raspberry Pi whose database lives on a USB flash disk
(section 138).

Three things this file is careful about:

* **DELIVERED is set from the gateway's answer**, never from having sent the
  request. G.1's most expensive bug was a row that said delivered when
  nothing had been (section 88).
* **UNKNOWN is not FAILED.** If the gateway's answer is lost, the same channel
  is retried with the same notification id — which the gateway answers from
  its own record — rather than falling through to the other channel and
  delivering twice (section 86).
* **Nothing is dropped because a channel refused.** A notification whose every
  channel failed stays PENDING and is retried on the next scan; the delivery
  watchdog picks up anything that stalls (sections 83, 84, 89).
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import signal as signal_mod
import sys
import time
from pathlib import Path

import att_budget
import att_calendar
import att_coding
import att_events
import att_health
import att_policy as policy
import att_render as render
import att_store as store
from att_config import ConfigError, load_config
from att_gateway import GatewayClient
from att_gateway import UNKNOWN as GW_UNKNOWN
from att_schema import get_meta, open_db, set_meta
from att_time import Clock, parse_iso

EXIT_CONFIG = 78

log = logging.getLogger("attention")

#: A gap longer than this many scan intervals means the engine was away, and
#: the backlog is folded into one recovery digest rather than delivered item
#: by item (section 96).
DOWNTIME_INTERVALS = 4

#: A digest is allowed to fire more than once a day — deferred items can come
#: due at different times — but not without limit.
MAX_DIGESTS_PER_DAY = 3


def setup_logging(log_dir: str, level: str = "INFO") -> None:
    """stderr first, a rotating file when this account can write one.

    Same ordering as the gateway, for the same reason: an operator running
    `--check` from a different account must get diagnostics rather than a
    PermissionError from the logger.
    """
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s")
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    try:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            os.path.join(log_dir, "attention.log"),
            maxBytes=4 * 1024 * 1024, backupCount=4, encoding="utf-8",
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as exc:
        root.warning("file logging disabled (%s: %s) — stderr only",
                     log_dir, type(exc).__name__)


class Engine:
    def __init__(self, cfg, clock: Clock | None = None):
        self.cfg = cfg
        self.clock = clock or cfg.clock
        self.gateway = GatewayClient(
            cfg.gateway["socket_path"],
            timeout=float(cfg.gateway["timeout_seconds"]),
        )
        self._stop = False

    # -- plumbing -----------------------------------------------------

    def db(self):
        return open_db(self.cfg.paths["state_db"])

    def request_stop(self, *_args):
        self._stop = True

    # -- one scan -----------------------------------------------------

    def scan(self) -> dict:
        started = time.monotonic()
        con = self.db()
        counters = {k: 0 for k in ("signals_seen", "signals_new", "notify_now",
                                   "deferred", "digested", "suppressed",
                                   "delivered", "failed", "llm_calls")}
        scan_id = store.begin_scan(con)
        con.commit()
        error = None
        try:
            self._scan_body(con, counters)
        except Exception as exc:            # noqa: BLE001 - reported, not raised
            # Section 146: an error is not a stop. The scan is recorded as
            # failed, the loop continues, and the failure is visible in
            # `attention-status` rather than only in the journal.
            log.exception("scan failed")
            error = f"{type(exc).__name__}: {exc}"
        finally:
            store.finish_scan(
                con, scan_id, ok=(error is None), counters=counters,
                duration_ms=int((time.monotonic() - started) * 1000),
                error=error,
            )
            con.commit()
            con.close()
        return counters

    def _scan_body(self, con, counters) -> None:
        clock = self.clock
        counters['suppressed'] += att_events.retire_non_owner_backlog(con)
        initialized_at, baseline_done = store.first_run_state(con)
        recovery = self._downtime_recovery(con)

        # --- collect -------------------------------------------------
        signals = []
        signals += att_calendar.collect(self.cfg, clock,
                                        initialized_at=initialized_at)
        signals += att_events.collect(self.cfg, clock,
                                      initialized_at=initialized_at)
        signals += att_coding.collect(self.cfg, clock,
                                      initialized_at=initialized_at)
        probes = []
        if self.cfg.health.get("enabled", True):
            probes = att_health.probe_all(self.cfg, clock, self.gateway)
            signals += att_health.signals_from_probes(con, probes, self.cfg,
                                                      clock)
        con.commit()

        counters["signals_seen"] = len(signals)
        fresh = []
        for sig in signals:
            row, is_new = store.upsert_signal(con, sig)
            if is_new:
                counters["signals_new"] += 1
                fresh.append(row)
        con.commit()

        pending = store.undecided_signals(con)
        store.purge_suppressions(con, now=clock.now_iso())
        con.commit()

        if not pending and not self._has_work(con):
            # The cheap path, and the common one. Nothing was found, so
            # nothing is decided, no quota is read, no socket is opened.
            log.debug("scan: no signals")
            return

        # --- budget --------------------------------------------------
        # Read only when there is something to decide. A quota call on every
        # empty scan would be 288 requests a day to answer no questions.
        budget = self._line_budget(con)

        ctx = policy.Context(
            initialized_at=initialized_at, baseline_done=baseline_done,
            budget=budget, recovery_mode=recovery,
            baseline_digest=bool(self.cfg.first_run.get("baseline_digest",
                                                        True)),
        )

        # --- decide --------------------------------------------------
        # Most severe first, then soonest. Two rules depend on this order and
        # both would be wrong without it: the burst cap spends its three slots
        # on the three things that matter most rather than on whatever the
        # collectors happened to emit first, and the one-interruption-per-thing
        # rule keeps the URGENT signal about a deadline rather than the INFO
        # one about the same deadline.
        severity_rank = {"CRITICAL": 0, "URGENT": 1, "IMPORTANT": 2, "INFO": 3}
        pending = sorted(
            pending,
            key=lambda r: (severity_rank.get(str(r["severity"]), 9),
                           r["due_at"] or "9999", r["observed_at"]),
        )

        immediate = []
        for row in pending:
            decision = policy.decide(con, row, self.cfg, clock, ctx)
            store.record_decision(
                con, signal_id=row["signal_id"], decision=decision.decision,
                reason=decision.reason, severity=decision.severity,
                channels=decision.channels, budget_state=decision.budget_state,
                detail=decision.detail, deliver_after=decision.deliver_after,
            )
            store.mark_signal_decided(con, row["signal_id"])
            key = {"NOTIFY_NOW": "notify_now", "DEFER": "deferred",
                   "DIGEST": "digested", "SUPPRESS": "suppressed"}[
                       decision.decision]
            counters[key] += 1
            if decision.sends_now:
                immediate.append((row, decision))
            log.info("decided %s %s -> %s (%s) sev=%s ch=%s",
                     row["source_type"], row["signal_id"], decision.decision,
                     decision.reason, decision.severity,
                     ",".join(decision.channels) or "-")
        con.commit()

        # --- deliver -------------------------------------------------
        for row, decision in immediate:
            ok = self._notify_signal(con, row, decision, budget)
            counters["delivered" if ok else "failed"] += 1
            if ok and row["source_type"] == "health":
                meta = json.loads(row["metadata"] or "{}")
                eid = meta.get("episode_id")
                if eid:
                    if row["signal_type"] == "health_recovered":
                        store.mark_recovery_notified(con, eid)
                    else:
                        store.mark_episode_notified(con, eid)
            con.commit()

        # --- digests -------------------------------------------------
        self._maybe_digest(con, counters, recovery=recovery,
                           baseline_done=baseline_done)

        if not baseline_done:
            store.mark_baseline_done(con)
            con.commit()

        # --- retries -------------------------------------------------
        self._retry_stuck(con, budget, counters)
        con.commit()

    # -- helpers ------------------------------------------------------

    def _has_work(self, con) -> bool:
        """Whether anything other than new signals needs attention this scan."""
        return bool(
            store.stuck_notifications(con, lease_seconds=60)
            or store.deferred_due(con, now=self.clock.now_iso())
            or store.signals_awaiting_digest(con, limit=1)
        )

    def _downtime_recovery(self, con) -> bool:
        last = get_meta(con, "last_scan_finished_at")
        set_meta(con, "last_scan_finished_at", self.clock.now_iso())
        con.commit()
        if not last:
            return False
        previous = parse_iso(last)
        if previous is None:
            return False
        gap = (self.clock.now() - previous).total_seconds()
        threshold = float(self.cfg.schedule["scan_interval_seconds"]) * \
            DOWNTIME_INTERVALS
        if gap > threshold:
            log.warning("downtime of %ds detected; folding the backlog into a "
                        "recovery digest", int(gap))
            return True
        return False

    def _line_budget(self, con):
        payload = self.gateway.line_budget()
        day = self.clock.day_key()
        month = self.clock.month_key()
        return att_budget.evaluate(
            payload, self.cfg, self.clock,
            spent_today=store.spent_today(con, "line", day),
            spent_this_month=store.spent_this_month(con, "line", month),
        )

    # -- delivery -----------------------------------------------------

    def _notify_signal(self, con, row, decision, budget) -> bool:
        subject, body = render.render(row, self.clock)
        notification, is_new = store.create_notification(
            con, dedupe_key=row["dedupe_key"], severity=decision.severity,
            subject=subject, body=body, reason=decision.reason,
            source_type=row["source_type"], source_id=row["source_id"],
        )
        if not is_new and notification["state"] in ("DELIVERED", "DELIVERING"):
            # Somebody already did this — a previous scan, or this scan's
            # predecessor before a crash. Not an error, and not a second send.
            log.info("notification %s already %s", notification["notification_id"],
                     notification["state"])
            return notification["state"] == "DELIVERED"
        con.commit()
        return self.deliver(con, notification, decision.channels, budget)

    def deliver(self, con, notification, channels, budget) -> bool:
        """Attempt every channel in the decision. True if any succeeded.

        Both channels are attempted for URGENT and CRITICAL by design
        (section 9), so this is not a fallback chain — it is a delivery set,
        and the fallback property falls out of it: a failing LINE cannot
        silence a notification that also went to Discord (section 84).
        """
        nid = notification["notification_id"]
        store.mark_notification(con, nid, "DELIVERING", count_attempt=True)
        con.commit()

        delivered = []
        last_error = None
        certainly_unsent = True

        for channel in channels:
            if channel == "line":
                ok = self._reserve_line(con, notification, budget)
                if not ok:
                    store.record_attempt(con, nid, channel="line",
                                         outcome="rejected",
                                         detail="budget_reservation_refused")
                    con.commit()
                    continue

            result = self.gateway.notify(
                notification_id=nid, channel=channel,
                severity=notification["severity"], text=notification["body"],
                reason=notification["reason"],
                dedupe_key=notification["dedupe_key"],
            )
            if result.status == GW_UNKNOWN:
                # The answer was lost. Ask again on the SAME channel: the
                # gateway is idempotent on (notification_id, channel), and on
                # LINE the retry key is derived from those two, so a message
                # that did go out comes back as a duplicate rather than as a
                # second charge (sections 86, 87).
                log.warning("no answer for %s on %s; retrying the same channel",
                            nid, channel)
                result = self.gateway.notify(
                    notification_id=nid, channel=channel,
                    severity=notification["severity"],
                    text=notification["body"], reason=notification["reason"],
                    dedupe_key=notification["dedupe_key"],
                )

            store.record_attempt(
                con, nid, channel=channel, outcome=result.status,
                detail=result.detail, quota_consumed=result.quota_consumed,
                http_status=result.http_status,
            )
            if channel == "line":
                store.settle_budget(con, channel="line", notification_id=nid,
                                    spent=result.quota_consumed or result.ok)
            con.commit()

            if result.ok:
                delivered.append(channel)
                certainly_unsent = False
            else:
                last_error = f"{channel}:{result.status}:{result.detail}"
                if not result.certainly_unsent:
                    certainly_unsent = False

        if delivered:
            store.mark_notification(con, nid, "DELIVERED", channels=delivered)
            con.commit()
            log.info("delivered %s via %s sev=%s", nid, ",".join(delivered),
                     notification["severity"])
            return True

        attempts = int(notification["attempts"] or 0) + 1
        if attempts >= int(self.cfg.notifications["max_delivery_attempts"]):
            store.mark_notification(con, nid, "FAILED", error=last_error)
            log.error("giving up on %s after %d attempts: %s", nid, attempts,
                      last_error)
        else:
            # Section 83: never silently lost. It stays PENDING and the next
            # scan tries again — which is what makes "the gateway was down"
            # a delay rather than a dropped notification (section 134).
            store.mark_notification(con, nid, "PENDING", error=last_error)
            log.warning("delivery of %s failed (%s); will retry", nid,
                        last_error)
        con.commit()
        _ = certainly_unsent
        return False

    def _reserve_line(self, con, notification, budget) -> bool:
        """Take a LINE credit before spending one (section 82).

        `budget` may be None — the retry path and the digest path both call
        `deliver` without one. Reading it here rather than assuming a caller
        supplied it is what stops a retry of a LINE notification from
        bypassing the ledger entirely.
        """
        if budget is None:
            budget = self._line_budget(con)
        allowed, reason = att_budget.line_allowed(
            budget, notification["severity"], self.cfg)
        if not allowed:
            log.info("line refused for %s: %s",
                     notification["notification_id"], reason)
            return False
        ok, spent = store.reserve_channel_budget(
            con, channel="line",
            notification_id=notification["notification_id"],
            severity=notification["severity"],
            day=self.clock.day_key(), month=self.clock.month_key(),
            allowance=max(budget.daily_allowance,
                          1 if notification["severity"] == "CRITICAL" else 0),
        )
        con.commit()
        if not ok:
            log.info("line daily allowance exhausted (%d used)", spent)
        return ok

    # -- digests ------------------------------------------------------

    def _digest_kind(self, con, recovery: bool, baseline_done: bool) -> str:
        if not baseline_done:
            return "baseline"
        if recovery:
            return "recovery"
        if store.deferred_due(con, now=self.clock.now_iso()):
            return "catchup"
        return "scheduled"

    def _maybe_digest(self, con, counters, *, recovery: bool,
                      baseline_done: bool) -> None:
        n = self.cfg.notifications
        if self.cfg.quiet.contains(self.clock.local()):
            # Nothing accumulates into a message during quiet hours; that is
            # what quiet hours are (section 55).
            return

        kind = self._digest_kind(con, recovery, baseline_done)
        # A baseline or a recovery digest has to swallow the WHOLE backlog,
        # not one page of it: anything left behind would surface again in
        # tomorrow's scheduled digest, which is how "one summary" quietly
        # becomes a series.
        sweep = 500 if kind in ("baseline", "recovery") else             int(n["digest_max_items"]) * 3
        items = store.signals_awaiting_digest(con, limit=sweep)
        if not items:
            # Section 61: an empty digest is not sent, ever.
            return
        if kind == "scheduled" and not n.get("daily_digest_enabled", True):
            return
        if kind == "baseline" and not self.cfg.first_run.get("baseline_digest",
                                                             True):
            store.mark_baseline_done(con)
            con.commit()
            return

        day = self.clock.day_key()
        made = con.execute(
            "SELECT COUNT(*) FROM digests WHERE kind = ?"
            "   AND substr(created_at, 1, 10) = substr(?, 1, 10)",
            (kind, self.clock.now_iso()),
        ).fetchone()[0]
        if made >= MAX_DIGESTS_PER_DAY:
            return
        if kind == "scheduled" and made >= 1:
            # One scheduled digest a day. Deferred items get their own
            # 'catchup' at the end of quiet hours (section 57).
            return

        max_items = int(self.cfg.first_run["baseline_max_items"]
                        if kind == "baseline" else n["digest_max_items"])
        subject, body = render.render_digest(items, kind, self.clock,
                                             max_items=max_items)
        if not body:
            return

        dedupe_key = f"digest:{kind}:{day}:{made}"
        notification, is_new = store.create_notification(
            con, dedupe_key=dedupe_key, kind="digest",
            # A digest never escalates. Whatever is in it, it is a summary of
            # things that were individually judged not to warrant an
            # interruption, so it goes to Discord and never spends a push
            # (sections 62, 63).
            severity="INFO", subject=subject, body=body,
            reason=f"digest_{kind}", source_type="digest",
        )
        store.record_digest(
            con, digest_id=f"dg_{dedupe_key}", kind=kind,
            # Every signal this digest ACCOUNTED FOR, not only the ones whose
            # text fitted. The rest were counted in the "ほか N 件" line and
            # must not come back tomorrow as if they were new.
            signal_ids=[r["signal_id"] for r in items],
            notification_id=notification["notification_id"],
        )
        con.commit()
        if not is_new and notification["state"] in ("DELIVERED", "DELIVERING"):
            return
        if self.deliver(con, notification, ["discord"], None):
            counters["delivered"] += 1
        else:
            counters["failed"] += 1
        con.commit()

    # -- watchdog -----------------------------------------------------

    def _retry_stuck(self, con, budget, counters) -> None:
        """Section 89: nothing sits in DELIVERING forever."""
        lease = max(120, int(self.cfg.schedule["scan_interval_seconds"]))
        for row in store.stuck_notifications(con, lease_seconds=lease):
            attempts = int(row["attempts"] or 0)
            if attempts >= int(self.cfg.notifications["max_delivery_attempts"]):
                store.mark_notification(con, row["notification_id"], "FAILED",
                                        error="max_attempts")
                counters["failed"] += 1
                continue
            channels = self._channels_for_retry(con, row, budget)
            log.info("retrying stuck notification %s via %s",
                     row["notification_id"], ",".join(channels) or "-")
            if channels and self.deliver(con, row, channels, budget):
                counters["delivered"] += 1

    def _channels_for_retry(self, con, row, budget):
        if row["kind"] == "digest":
            return ["discord"]
        if budget is None:
            budget = self._line_budget(con)
        chans, _ = policy.channels_for(row["severity"], self.cfg, budget)
        return chans

    # -- the loop -----------------------------------------------------

    def run_forever(self) -> int:
        interval = int(self.cfg.schedule["scan_interval_seconds"])
        log.info("attention engine started (scan every %ds, policy %s, tz %s)",
                 interval, __import__("att_schema").POLICY_VERSION,
                 self.cfg.schedule["timezone"])
        while not self._stop:
            try:
                counters = self.scan()
                if any(counters[k] for k in ("notify_now", "delivered",
                                             "failed", "signals_new")):
                    log.info("scan: %s", counters)
            except Exception:
                log.exception("scan raised outside its own handler")
            # Sleep in short slices so SIGTERM is answered promptly rather
            # than after a whole interval.
            for _ in range(interval):
                if self._stop:
                    break
                time.sleep(1)
        log.info("attention engine stopped")
        return 0


# --------------------------------------------------------------- CLI


def run_check(cfg) -> int:
    """Say what a start would need, and whether it would work. Never sends."""
    ok = True
    log.info("config      : %s", cfg.path)
    log.info("timezone    : %s (now %s)", cfg.schedule["timezone"],
             cfg.clock.local().strftime("%Y-%m-%d %H:%M %Z"))
    log.info("quiet hours : %s-%s (currently %s)", cfg.quiet_hours["start"],
             cfg.quiet_hours["end"],
             "inside" if cfg.quiet.contains(cfg.clock.local()) else "outside")
    try:
        con = open_db(cfg.paths["state_db"])
        con.close()
        log.info("state db    : %s (openable)", cfg.paths["state_db"])
    except Exception as exc:
        log.error("state db    : %s NOT openable (%s)", cfg.paths["state_db"],
                  type(exc).__name__)
        ok = False

    for name in ("events_db", "jobs_db", "discord_collector_db"):
        path = cfg.sources.get(name) or ""
        if not path:
            log.info("source      : %-22s (not configured)", name)
            continue
        try:
            from att_sources import open_source
            con = open_source(path)
            tables = len(con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall())
            con.close()
            log.info("source      : %-22s readable (%d tables)", name, tables)
        except Exception as exc:
            log.error("source      : %-22s NOT readable (%s)", name,
                      type(exc).__name__)
            ok = False

    client = GatewayClient(cfg.gateway["socket_path"],
                           timeout=float(cfg.gateway["timeout_seconds"]))
    if client.health():
        log.info("gateway     : %s reachable", cfg.gateway["socket_path"])
        payload = client.line_budget()
        if payload is None:
            log.warning("line budget : unavailable — LINE would be UNKNOWN "
                        "and restricted to urgent/critical")
        else:
            budget = att_budget.evaluate(payload, cfg, cfg.clock)
            log.info("line budget : %s (%s)", budget.state, budget.detail)
            log.info("line daily  : %d/day allowance, reserve %d",
                     budget.daily_allowance, budget.reserve)
    else:
        log.error("gateway     : %s NOT reachable — nothing could be "
                  "delivered", cfg.gateway["socket_path"])
        ok = False

    log.info("channels    : default=%s line_escalation=%s routine_line=%s",
             cfg.channels["default_channel"],
             cfg.channels["line_escalation_enabled"],
             cfg.channels["routine_line_enabled"])
    log.info("llm         : enabled=%s", cfg.llm.get("enabled"))
    log.info("result      : %s", "would start" if ok else "would NOT start")
    return 0 if ok else EXIT_CONFIG


def run_selftest(cfg) -> int:
    """Report what this process CANNOT reach (section 111).

    Written as a positive test of a negative property, because the alternative
    is asserting in a report that a credential is unreachable without ever
    having tried. Each line is an actual open() and its actual errno.

    **It refuses to run as root**, and that guard exists because the first
    run of it lied. systemd blocks these paths by bind-mounting a mode-0000
    node over each one — and root bypasses permission checks, so running the
    test through `nsenter` as root reported the Hermes .env as READABLE when
    the service itself cannot open it. A self-test whose answer depends on
    who ran it is exactly the false pass section 140 refuses.

    To run it inside the service's own namespace:

        sudo nsenter -t $(systemctl show attention-engine -p MainPID --value) \\
          -m -- setpriv --reuid=<uid> --regid=<gid> --clear-groups -- \\
          /srv/hermes/attention/venv/bin/python \\
          /srv/hermes/attention/app/att_engine.py --selftest
    """
    if getattr(os, "geteuid", lambda: 1)() == 0:
        print("refusing to run as root: root bypasses the permission bits and "
              "the mode-0000 nodes systemd uses for InaccessiblePaths, so "
              "every answer would be wrong in the reassuring direction.",
              file=sys.stderr)
        return 2

    print(f"running as uid={os.getuid()} gid={os.getgid()} "
          f"groups={sorted(os.getgroups())}")
    targets = [
        ("hermes .env", "/srv/hermes/hermes-data/.env"),
        ("line channel secret", "/etc/hermes-secrets/line-channel-secret"),
        ("line access token", "/etc/hermes-secrets/line-channel-access-token"),
        ("discord interface token", "/etc/hermes-secrets/discord-interface-token"),
        ("discord knowledge token", "/etc/hermes-secrets/discord-knowledge-token"),
        ("gateway hermes key", "/etc/hermes-secrets/channel-gateway-hermes-key"),
        ("docker socket", "/run/docker.sock"),
        ("docker socket (alt)", "/var/run/docker.sock"),
        ("gateway state db", "/srv/hermes/channel-gateway/state/gateway.db"),
    ]
    print("== what this process can reach ==")
    leaked = 0
    for label, path in targets:
        try:
            with open(path, "rb") as fh:
                fh.read(1)
            print(f"  READABLE  {label:26s} {path}")
            leaked += 1
        except OSError as exc:
            print(f"  blocked   {label:26s} {path}  ({exc.__class__.__name__}:"
                  f" {exc.errno})")
    print()
    print("== what it must be able to reach ==")
    for label, path in (("events db", cfg.sources["events_db"]),
                        ("jobs db", cfg.sources["jobs_db"]),
                        ("gateway socket", cfg.gateway["socket_path"])):
        print(f"  {'ok      ' if os.path.exists(path) else 'MISSING '} "
              f"{label:26s} {path}")
    print()
    print(f"result: {leaked} credential path(s) readable from this process")
    return 0 if leaked == 0 else 1


def run_test_notification(cfg, channel: str) -> int:
    """Admin-only smoke test (section 104). Owner-fixed, bounded content."""
    engine = Engine(cfg)
    con = engine.db()
    try:
        stamp = engine.clock.local().strftime("%Y-%m-%d %H:%M")
        notification, _ = store.create_notification(
            con, dedupe_key=f"admin:test:{engine.clock.now_iso()}",
            severity="INFO", subject="Attention Engine テスト",
            body=("Attention Engine の疎通テストです。"
                  f"（{stamp} / policy {get_meta(con, 'schema_version')}）"),
            reason="admin_test_notification", source_type="admin",
        )
        con.commit()
        ok = engine.deliver(con, notification, [channel], None)
        con.commit()
        print(f"{'delivered' if ok else 'FAILED'} via {channel}: "
              f"{notification['notification_id']}")
        return 0 if ok else 1
    finally:
        con.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Hermes Attention Engine")
    ap.add_argument("--config", default=None)
    ap.add_argument("--check", action="store_true",
                    help="validate configuration and sources, then exit")
    ap.add_argument("--selftest", action="store_true",
                    help="report which credential paths this process can read")
    ap.add_argument("--once", action="store_true", help="run one scan and exit")
    ap.add_argument("--at", default=None,
                    help="pretend it is this ISO-8601 time (testing only; "
                         "never changes the host clock)")
    ap.add_argument("--test-notification", metavar="CHANNEL", default=None,
                    choices=["discord", "line"])
    ap.add_argument("--suppress", metavar="DEDUPE_KEY", default=None)
    ap.add_argument("--suppress-hours", type=float, default=24.0)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    setup_logging(cfg.paths["log_dir"], args.log_level)

    if args.check:
        return run_check(cfg)
    if args.selftest:
        return run_selftest(cfg)
    if args.test_notification:
        return run_test_notification(cfg, args.test_notification)

    if args.suppress:
        from datetime import timedelta

        con = open_db(cfg.paths["state_db"])
        until = cfg.clock.now() + timedelta(hours=args.suppress_hours)
        store.add_suppression(con, args.suppress, until.isoformat(),
                              origin="admin")
        con.commit()
        con.close()
        print(f"suppressed {args.suppress} until {until.isoformat()}")
        return 0

    clock = cfg.clock
    if args.at:
        from att_time import parse_iso as _parse

        when = _parse(args.at)
        if when is None:
            print(f"--at: cannot parse {args.at!r}", file=sys.stderr)
            return 2
        clock = clock.at(when)
        log.warning("running with a fixed clock: %s", clock.local())

    engine = Engine(cfg, clock=clock)
    if args.once:
        counters = engine.scan()
        print(json.dumps(counters, ensure_ascii=False))
        return 0

    for sig in (signal_mod.SIGTERM, signal_mod.SIGINT):
        signal_mod.signal(sig, engine.request_stop)
    return engine.run_forever()


if __name__ == "__main__":
    sys.exit(main())
