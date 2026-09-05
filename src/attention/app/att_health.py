#!/usr/bin/env python3
"""Attention Engine — infrastructure health, as episodes rather than events.

Section 39 is the whole design brief for this file, and it comes from a real
G.1 failure: the gateway answered HTTP 200 on every health check while its
worker died on every pass, and the Discord bot stayed connected while every
DM went unprocessed. Both were "up" by every check that existed. So nothing
here treats a running process as a healthy one.

What is checked instead is whether **work is moving**:

    gateway             queue depth against queue age, and whether Hermes
                        itself last ANSWERED — not merely whether something
                        was delivered, because an error message is a delivery
    discord collector   its own heartbeat, which advances on a timer rather
                        than on traffic, plus its gateway-connected flag
    coding supervisor   jobs that have sat in a working state too long
    line refresh        whether the daily job ran, and whether it succeeded
    hermes api          an end-to-end HTTP answer
    disk                free space on the volume everything writes to

And section 41 is enforced everywhere: an idle system is not a broken one.
"No messages today" is normal; "messages waiting and none processed" is not.
The heartbeat checks were chosen precisely because they advance when nothing
is happening, which is what makes them safe to alarm on.

Failures are episodes (section 36). A service that is down for an hour
extends one row across twelve scans; it does not produce twelve alerts.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass, field

import att_store as store
from att_signals import (
    SIG_HEALTH_DEGRADED,
    SIG_HEALTH_RECOVERED,
    SOURCE_HEALTH,
    make_signal,
)
from att_sources import SourceUnavailable, open_source, table_exists
from att_time import parse_iso

log = logging.getLogger("attention.health")

SVC_GATEWAY = "gateway"
SVC_COLLECTOR = "discord_collector"
SVC_SUPERVISOR = "coding_supervisor"
SVC_LINE_REFRESH = "line_refresh"
SVC_HERMES = "hermes_api"
SVC_DISK = "disk"
SVC_CALENDAR = "calendar"
SVC_GMAIL = "gmail"

#: How bad each failure is. Only two things reach CRITICAL: no space left,
#: and a notification path that cannot deliver — everything else is IMPORTANT,
#: because URGENT and CRITICAL are what draw on the metered channel and its
#: emergency reserve (sections 52, 101).
SEVERITY_BY_REASON = {
    "unreachable": "CRITICAL",
    "queue_stuck": "CRITICAL",
    "processing_stuck": "URGENT",
    "hermes_failing": "URGENT",
    "discord_disconnected": "URGENT",
    "disk_critical": "CRITICAL",
    "disk_low": "IMPORTANT",
    "heartbeat_stale": "URGENT",
    "gateway_disconnected": "IMPORTANT",
    "collector_degraded": "IMPORTANT",
    "collector_queue_stuck": "IMPORTANT",
    "state_unreadable": "IMPORTANT",
    "job_stalled": "IMPORTANT",
    "script_failed": "IMPORTANT",
    "not_running": "IMPORTANT",
    "auth_required": "IMPORTANT",
    "watch_expired": "IMPORTANT",
    "sync_stuck": "IMPORTANT",
    "gmail_auth_required": "IMPORTANT",
    "gmail_watch_expired": "IMPORTANT",
    "gmail_sync_stuck": "IMPORTANT",
}

JAPANESE_REASON = {
    "unreachable": "Gateway に接続できません",
    "queue_stuck": "受信キューが滞留し、処理が進んでいません",
    "processing_stuck": "処理中のまま止まっているリクエストがあります",
    "hermes_failing": "Hermes が応答を返せていません",
    "discord_disconnected": "Discord Interface Bot が切断されています",
    "disk_critical": "ディスク空き容量が危険水準です",
    "disk_low": "ディスク空き容量が少なくなっています",
    "heartbeat_stale": "Discord Collector の状態更新が止まっています",
    "gateway_disconnected": "Discord Collector が Discord から切断されています",
    "collector_degraded": "Discord Collector が degraded 状態です",
    "collector_queue_stuck": "Discord Collector のキューが滞留しています",
    "state_unreadable": "状態データベースが読めません",
    "job_stalled": "Coding job が進行しないまま停止しています",
    "script_failed": "LINE 知識の更新ジョブが失敗し続けています",
    "not_running": "定期ジョブが実行されていません",
    "auth_required": "Google Calendar の再認証が必要です",
    "watch_expired": "Google Calendar の push watch が期限切れです",
    "sync_stuck": "Google Calendar の同期キューが滞留しています",
    "gmail_auth_required": "Gmail の再認証が必要です",
    "gmail_watch_expired": "Gmail の push watch が期限切れです",
    "gmail_sync_stuck": "Gmail の同期キューが滞留しています",
}


@dataclass
class Probe:
    """One service's verdict for one scan."""

    service: str
    ok: bool
    reason: str = ""
    detail: str = ""
    #: Set when the probe could not reach a conclusion. An inconclusive probe
    #: neither opens an episode nor closes one — treating "I could not check"
    #: as "healthy" would silently close a real outage (section 140).
    inconclusive: bool = False
    facts: dict = field(default_factory=dict)


def _age(value, now) -> float | None:
    ts = parse_iso(value)
    return None if ts is None else (now - ts).total_seconds()


# ------------------------------------------------------------- gateway


def probe_gateway(cfg, clock, gateway) -> Probe:
    live = gateway.liveness()
    if live is None:
        return Probe(SVC_GATEWAY, False, "unreachable",
                     "proactive socket did not answer")
    now = clock.now()
    stuck_after = float(cfg.health["queue_stuck_seconds"])

    facts = {
        "queue_depth": live.get("queue_depth"),
        "processing_depth": live.get("processing_depth"),
        "responded": live.get("responded_count"),
    }

    # Section 42: depth alone is not stuck, and age alone is not stuck. Both
    # together, with nothing having been processed since, is stuck.
    q_age = _age(live.get("oldest_queued_at"), now)
    if int(live.get("queue_depth") or 0) > 0 and q_age and q_age > stuck_after:
        return Probe(SVC_GATEWAY, False, "queue_stuck",
                     f"{live.get('queue_depth')} queued, oldest {int(q_age)}s",
                     facts=facts)

    p_age = _age(live.get("oldest_processing_at"), now)
    if p_age and p_age > stuck_after:
        return Probe(SVC_GATEWAY, False, "processing_stuck",
                     f"in flight for {int(p_age)}s", facts=facts)

    if live.get("discord_enabled") and not live.get("discord_connected"):
        return Probe(SVC_GATEWAY, False, "discord_disconnected",
                     "interface bot is not connected", facts=facts)

    # Hermes answering, as distinct from the gateway delivering. A gateway
    # that replies "いま応答できません" to everything looks perfect by every
    # queue metric; only these two timestamps tell them apart.
    ok_at = parse_iso(live.get("last_hermes_success_at"))
    fail_at = parse_iso(live.get("last_hermes_failure_at"))
    if fail_at is not None and (ok_at is None or fail_at > ok_at):
        # Failures only happen when a message arrives, so this cannot fire on
        # an idle system — there is nothing to fail.
        since = (now - fail_at).total_seconds()
        if since < float(cfg.sustained_seconds(SVC_GATEWAY)) * 4:
            return Probe(SVC_GATEWAY, False, "hermes_failing",
                         f"last success {live.get('last_hermes_success_at')}",
                         facts=facts)

    return Probe(SVC_GATEWAY, True, facts=facts)


# --------------------------------------------------- discord collector


def probe_collector(cfg, clock) -> Probe:
    path = cfg.sources.get("discord_collector_db") or ""
    if not path or not os.path.exists(path):
        return Probe(SVC_COLLECTOR, True, inconclusive=True,
                     detail="collector database not configured")
    try:
        con = open_source(path)
    except SourceUnavailable as exc:
        return Probe(SVC_COLLECTOR, False, "state_unreadable", str(exc))
    try:
        if not table_exists(con, "ingest_state"):
            return Probe(SVC_COLLECTOR, True, inconclusive=True,
                         detail="no ingest_state table")
        state = {r["key"]: r["value"]
                 for r in con.execute("SELECT key, value FROM ingest_state")}
    except Exception as exc:
        return Probe(SVC_COLLECTOR, False, "state_unreadable",
                     type(exc).__name__)
    finally:
        con.close()

    now = clock.now()
    facts = {k: state.get(k) for k in
             ("collector_health", "gateway_connected", "queue_depth",
              "last_event_at", "last_successful_ingest_at")}

    # The heartbeat. Measured on 2026-09-04: `updated_at` advanced within 25
    # seconds while no message had arrived for hours, so it is a liveness
    # signal and not a traffic signal — which is exactly what section 41
    # requires of anything allowed to raise an alarm.
    beat = _age(state.get("updated_at"), now)
    if beat is None:
        return Probe(SVC_COLLECTOR, True, inconclusive=True,
                     detail="collector publishes no heartbeat")
    if beat > float(cfg.health["collector_heartbeat_seconds"]):
        return Probe(SVC_COLLECTOR, False, "heartbeat_stale",
                     f"status last written {int(beat)}s ago", facts=facts)

    if str(state.get("gateway_connected") or "") != "1":
        return Probe(SVC_COLLECTOR, False, "gateway_disconnected",
                     "collector reports it is not connected", facts=facts)

    health = str(state.get("collector_health") or "").upper()
    if health and health not in ("CONNECTED", "READY", "OK"):
        return Probe(SVC_COLLECTOR, False, "collector_degraded",
                     f"collector_health={health}", facts=facts)

    depth = int(state.get("queue_depth") or 0)
    ingest_age = _age(state.get("last_successful_ingest_at"), now)
    if depth > 0 and ingest_age and ingest_age > float(
            cfg.health["queue_stuck_seconds"]):
        return Probe(SVC_COLLECTOR, False, "collector_queue_stuck",
                     f"{depth} queued, none ingested for {int(ingest_age)}s",
                     facts=facts)

    return Probe(SVC_COLLECTOR, True, facts=facts)


# --------------------------------------------------- coding supervisor


#: States in which something should be ACTIVELY running. A job that sits in
#: one of these for hours means the loop stalled without parking the job —
#: the "no work is actually progressing" case section 140 refuses to pass.
#:
#: `IMPLEMENTATION_ACCEPTED` and `WAITING_REVIEW` are deliberately absent, and
#: this was measured rather than assumed: the F.2 supervisor is not a daemon
#: on this host — it advances a job when Hermes invokes it — so a job that has
#: passed the deterministic gate and is waiting for a review that nobody has
#: asked for yet is idle, not stalled. Including them flagged seven leftover
#: F.2 jobs as a fault on the very first real scan, which is section 41's
#: mistake in reverse: treating an idle system as a broken one.
WORKING_STATES = ("RUNNING", "VALIDATING", "REVIEWING", "DISPATCHED",
                  "QUEUED")


def probe_supervisor(cfg, clock) -> Probe:
    path = cfg.sources.get("jobs_db") or ""
    if not path or not os.path.exists(path):
        return Probe(SVC_SUPERVISOR, True, inconclusive=True,
                     detail="jobs database not configured")
    try:
        con = open_source(path)
    except SourceUnavailable as exc:
        return Probe(SVC_SUPERVISOR, False, "state_unreadable", str(exc))
    try:
        if not table_exists(con, "jobs"):
            return Probe(SVC_SUPERVISOR, True, inconclusive=True)
        placeholders = ",".join("?" * len(WORKING_STATES))
        rows = con.execute(
            f"SELECT job_id, status, updated_at FROM jobs"
            f" WHERE status IN ({placeholders})", WORKING_STATES
        ).fetchall()
    except Exception as exc:
        return Probe(SVC_SUPERVISOR, False, "state_unreadable",
                     type(exc).__name__)
    finally:
        con.close()

    now = clock.now()
    limit = float(cfg.sustained_seconds(SVC_SUPERVISOR)) * 4
    stalled = [r for r in rows
               if (_age(r["updated_at"], now) or 0) > limit]
    if stalled:
        # No job at all is the normal state and is emphatically healthy.
        return Probe(SVC_SUPERVISOR, False, "job_stalled",
                     f"{len(stalled)} job(s) not advancing",
                     facts={"stalled": len(stalled),
                            "job_id": stalled[0]["job_id"]})
    return Probe(SVC_SUPERVISOR, True, facts={"working": len(rows)})


# ------------------------------------------------------ line refresh


def probe_line_refresh(cfg, clock) -> Probe:
    directory = cfg.sources.get("line_refresh_output_dir") or ""
    if not directory or not os.path.isdir(directory):
        # Section 14: a source that is not wired up is not invented. No
        # directory means no probe, not a failing probe.
        return Probe(SVC_LINE_REFRESH, True, inconclusive=True,
                     detail="refresh output directory not configured")
    try:
        entries = [os.path.join(directory, n) for n in os.listdir(directory)
                   if n.endswith(".md")]
    except OSError as exc:
        return Probe(SVC_LINE_REFRESH, False, "state_unreadable",
                     type(exc).__name__)
    if not entries:
        return Probe(SVC_LINE_REFRESH, True, inconclusive=True,
                     detail="no runs recorded yet")

    recent = sorted(entries, key=os.path.getmtime, reverse=True)[:3]
    age = clock.now().timestamp() - os.path.getmtime(recent[0])
    if age > float(cfg.sustained_seconds(SVC_LINE_REFRESH)):
        return Probe(SVC_LINE_REFRESH, False, "not_running",
                     f"last run {int(age / 3600)}h ago",
                     facts={"age_hours": int(age / 3600)})

    def failed(path) -> bool:
        try:
            text = open(path, encoding="utf-8", errors="replace").read(4000)
        except OSError:
            return False
        lowered = text.lower()
        return "failed" in lowered or "**status:** error" in lowered

    verdicts = [failed(p) for p in recent]
    # Section 35 applied to a scheduled job: one bad run is a transient, two
    # in a row is a pattern. Waiting for the episode threshold instead would
    # be the wrong instrument — a job that runs once a day would need two days
    # of episode time to say what its own output already says.
    if len(verdicts) >= 2 and verdicts[0] and verdicts[1]:
        consecutive = 0
        for v in verdicts:
            if not v:
                break
            consecutive += 1
        return Probe(SVC_LINE_REFRESH, False, "script_failed",
                     f"the last {consecutive} runs reported a failure",
                     facts={"age_hours": int(age / 3600),
                            "consecutive_failures": consecutive})
    if verdicts and verdicts[0]:
        # One failure. Recorded as inconclusive rather than healthy, so it
        # neither raises an alarm nor closes an episode that is already open.
        return Probe(SVC_LINE_REFRESH, True, inconclusive=True,
                     detail="one failed run; waiting to see if it repeats")
    return Probe(SVC_LINE_REFRESH, True, facts={"age_hours": int(age / 3600)})


# --------------------------------------------------------- calendar sync


def probe_calendar(cfg, clock) -> Probe:
    """Health from the published Calendar status snapshot, never its OAuth DB."""
    path = cfg.sources.get("calendar_index_db") or ""
    if not path or not os.path.isfile(path):
        return Probe(SVC_CALENDAR, True, inconclusive=True,
                     detail="calendar index not published yet")
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT * FROM calendar_status").fetchall()
        con.close()
    except sqlite3.Error as exc:
        return Probe(SVC_CALENDAR, False, "state_unreadable", type(exc).__name__)
    if not rows:
        return Probe(SVC_CALENDAR, True, inconclusive=True,
                     detail="Calendar authorization/selection is pending")
    now_ms = int(clock.now().timestamp() * 1000)
    for row in rows:
        facts = {"calendar_id": row["calendar_id"], "pending_sync_age_seconds": row["pending_sync_age_seconds"]}
        if str(row["auth_state"]) == "AUTH_REQUIRED":
            return Probe(SVC_CALENDAR, False, "auth_required", "authorization is required", facts=facts)
        expiry = row["watch_expiration_ms"]
        if expiry is not None and int(expiry) < now_ms:
            return Probe(SVC_CALENDAR, False, "watch_expired", "watch expiration passed", facts=facts)
        pending = row["pending_sync_age_seconds"]
        if pending is not None and int(pending) > int(cfg.health["queue_stuck_seconds"]):
            return Probe(SVC_CALENDAR, False, "sync_stuck", "sync queue is not advancing", facts=facts)
    return Probe(SVC_CALENDAR, True, facts={"selected_calendars": len(rows)})


# ------------------------------------------------------------ Gmail sync


def probe_gmail(cfg, clock) -> Probe:
    """Health from Gmail's published snapshot, never Gmail OAuth/state."""
    path = cfg.sources.get("gmail_index_db") or ""
    if not path or not os.path.isfile(path):
        return Probe(SVC_GMAIL, True, inconclusive=True,
                     detail="gmail index not published yet")
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM gmail_status WHERE singleton=1").fetchone()
        con.close()
    except sqlite3.Error as exc:
        return Probe(SVC_GMAIL, False, "state_unreadable", type(exc).__name__)
    if row is None:
        return Probe(SVC_GMAIL, True, inconclusive=True,
                     detail="Gmail authorization is pending")
    facts = {
        "pending_sync_age_seconds": row["pending_sync_age_seconds"],
        "watch_state": row["watch_state"],
    }
    if str(row["auth_state"]) == "AUTH_REQUIRED":
        return Probe(SVC_GMAIL, False, "gmail_auth_required",
                     "authorization is required", facts=facts)
    # No recorded expiration means Cloud Pub/Sub setup has not completed; it
    # is a human gate, not an outage. Once Gmail provided an expiration, an
    # elapsed value is a measurable service degradation.
    expiry = row["watch_expiration_ms"]
    if expiry is not None and int(expiry) < int(clock.now().timestamp() * 1000):
        return Probe(SVC_GMAIL, False, "gmail_watch_expired",
                     "watch expiration passed", facts=facts)
    pending = row["pending_sync_age_seconds"]
    if pending is not None and int(pending) > int(cfg.health["queue_stuck_seconds"]):
        return Probe(SVC_GMAIL, False, "gmail_sync_stuck",
                     "sync queue is not advancing", facts=facts)
    return Probe(SVC_GMAIL, True, facts=facts)


# ------------------------------------------------------- hermes / disk


def probe_hermes(cfg, clock) -> Probe:
    url = cfg.sources.get("hermes_health_url") or ""
    if not url:
        return Probe(SVC_HERMES, True, inconclusive=True,
                     detail="health url not configured")
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            code = resp.status
            resp.read(200)
    except urllib.error.HTTPError as exc:
        code = exc.code
    except Exception as exc:
        return Probe(SVC_HERMES, False, "unreachable", type(exc).__name__)
    if code != 200:
        return Probe(SVC_HERMES, False, "unreachable", f"HTTP {code}")
    return Probe(SVC_HERMES, True, facts={"http": code})


def probe_disk(cfg, clock) -> Probe:
    path = cfg.sources.get("disk_path") or "/"
    try:
        usage = shutil.disk_usage(path)
    except OSError as exc:
        return Probe(SVC_DISK, False, "state_unreadable", type(exc).__name__)
    free_pct = 100.0 * usage.free / max(1, usage.total)
    facts = {"free_percent": round(free_pct, 1),
             "free_gb": round(usage.free / 1e9, 1)}
    if free_pct < float(cfg.health["disk_critical_percent"]):
        return Probe(SVC_DISK, False, "disk_critical",
                     f"{free_pct:.1f}% free on {path}", facts=facts)
    if free_pct < float(cfg.health["disk_warn_percent"]):
        return Probe(SVC_DISK, False, "disk_low",
                     f"{free_pct:.1f}% free on {path}", facts=facts)
    return Probe(SVC_DISK, True, facts=facts)


def probe_all(cfg, clock, gateway) -> list:
    probes = []
    for fn, args in (
        (probe_gateway, (cfg, clock, gateway)),
        (probe_collector, (cfg, clock)),
        (probe_supervisor, (cfg, clock)),
        (probe_line_refresh, (cfg, clock)),
        (probe_calendar, (cfg, clock)),
        (probe_gmail, (cfg, clock)),
        (probe_hermes, (cfg, clock)),
        (probe_disk, (cfg, clock)),
    ):
        try:
            probes.append(fn(*args))
        except Exception:
            log.exception("probe %s raised", getattr(fn, "__name__", fn))
    return probes


# ------------------------------------------- episodes -> signals


def signals_from_probes(con, probes, cfg, clock) -> list:
    """Turn this scan's verdicts into signals, via the episode ledger.

    A degraded probe produces a signal only once its episode has lasted
    longer than the service's sustained threshold, and only once per episode
    (sections 35, 36, 37). A recovery produces one only if the failure was
    announced — nobody wants "it's fixed" about a problem they never heard of
    (section 38).
    """
    if not cfg.health.get("enabled", True):
        return []
    now = clock.now()
    now_s = now.isoformat()
    signals = []

    for probe in probes:
        if probe.inconclusive:
            continue

        if not probe.ok:
            episode, _is_new = store.observe_degraded(
                con, probe.service, probe.reason, probe.detail, now=now_s
            )
            if episode["notified_at"]:
                continue           # already announced; the episode continues
            started = parse_iso(episode["first_degraded_at"]) or now
            elapsed = (now - started).total_seconds()
            if elapsed < float(cfg.sustained_seconds(probe.service,
                                                     probe.reason)):
                # Section 35: one reconnect, one timeout, one 5xx is not an
                # incident. Nothing is sent until it has lasted.
                continue
            severity = SEVERITY_BY_REASON.get(probe.reason, "IMPORTANT")
            signals.append(make_signal(
                source_type=SOURCE_HEALTH, source_id=probe.service,
                signal_type=SIG_HEALTH_DEGRADED,
                created_at=episode["first_degraded_at"],
                subject=probe.service,
                summary=JAPANESE_REASON.get(probe.reason, probe.reason),
                key_parts=("health", probe.service, probe.reason,
                           episode["episode_id"]),
                entity_key=f"health:{probe.service}",
                severity=severity, confidence=1.0, actionability=0.9,
                metadata={
                    "episode_id": episode["episode_id"],
                    "reason": probe.reason,
                    "detail": probe.detail,
                    "degraded_seconds": int(elapsed),
                    "observations": episode["observations"],
                    **{k: v for k, v in (probe.facts or {}).items()},
                },
            ))
            continue

        # Healthy. Close whatever was open, and announce only what was told.
        for closed in store.close_episodes(con, probe.service, now=now_s):
            if not closed["notified_at"] or closed["recovery_notified_at"]:
                continue
            if not cfg.health.get("recovery_notifications", True):
                continue
            signals.append(make_signal(
                source_type=SOURCE_HEALTH, source_id=probe.service,
                signal_type=SIG_HEALTH_RECOVERED,
                created_at=now_s,
                subject=probe.service,
                summary=f"{probe.service} は復旧しました",
                key_parts=("health", probe.service, "recovered",
                           closed["episode_id"]),
                entity_key=f"health:{probe.service}",
                severity="INFO", confidence=1.0, actionability=0.1,
                metadata={"episode_id": closed["episode_id"],
                          "reason": closed["reason"],
                          "was_degraded_since": closed["first_degraded_at"]},
            ))
    return signals
