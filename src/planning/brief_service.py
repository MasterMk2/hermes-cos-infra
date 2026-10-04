#!/usr/bin/env python3
"""Scheduled Morning Brief — the run that the systemd timer executes.

Phase I.2 sections 50, 53-60, 64-66, 99, 105-107.
Interface: I2-CONTRACT.md section 6, amended by I2-AMENDMENT sections A and G.

## The decision order is the specification

Every step below is ordered so that the expensive and the irreversible happen
last, and so that a second trigger on the same day costs one SQLite read:

  1. `morning_brief.enabled` false            -> skipped_disabled
  2. resolve the local date and the configured time in `timezone`
  3. late boot (sections 55, 56)               -> skipped_late_boot
  4. local idempotency pre-check (section 58)  -> suppressed_duplicate
  5. build the plan (section 60: a failure sends NOTHING and invents nothing)
  6. render (LLM, falling back to the deterministic template)
  7. deliver, bounded retry (section 59)
  8. write the health file (section 105) and return

## Who is authoritative for "exactly one brief per local date"

**The channel gateway is.** `proactive_deliveries` keys on
`(notification_id, channel)` as its PRIMARY KEY and repeats the constraint as
`UNIQUE INDEX idx_proactive_once`; `reserve_proactive()` catches the
`IntegrityError` inside the single writer's single transaction and the API
answers `200 {"status": "duplicate", "detail": "already_delivered"}`
(profile section H.4, I2-AMENDMENT G2). With
`notification_id = "morning-brief:YYYY-MM-DD"` that already satisfies phase
sections 57 and 58 in full.

Step 4 here is therefore a **cheap pre-check and a local record**, not a
second authority. When the two disagree the gateway wins: an
`already_delivered` answer is recorded locally as delivered and reported as
success. There is exactly one lock, and it is held by the component that
actually performs the send.

## Late boot, exactly (sections 55, 56)

    delta = now - (today's local date at `morning_brief.time`, in `timezone`)
    delta <= late_boot_grace_hours   -> send
    delta >  late_boot_grace_hours   -> skipped_late_boot

`delta` at exactly the grace boundary **sends** (section 56 reads "grace
window <= 3h: send"). A `now` earlier than the scheduled time also sends: a
`Persistent=true` timer that fires a few seconds early through clock skew is
not a late boot. `trigger="manual"` bypasses this check entirely — and only
this check; the idempotency check still applies, so a human running the CLI
after an already-delivered morning gets `suppressed_duplicate`, not a second
brief.

## Retry, exactly (section 59)

At most `MAX_DELIVERY_ATTEMPTS` (3) calls to the socket, with 2s then 8s
between them. Only the outcomes in `delivery.RETRYABLE` are retried; the two
409s (`delivery_in_progress`, `content_revision_mismatch`) stop immediately,
because a row under this notification id already exists and re-posting a brief
whose text differs by one character would otherwise loop forever
(I2-AMENDMENT G3). Termination is proved in
`tests-planning/test_brief_delivery.py` against an always-failing transport.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import brief_state, delivery

log = logging.getLogger("planning.brief_service")

# -- statuses (I2-CONTRACT.md section 6) ------------------------------------

ST_DELIVERED = "delivered"
ST_SUPPRESSED = "suppressed_duplicate"
ST_LATE_BOOT = "skipped_late_boot"
#: Additive to contract section 6. A run that fires *before* the configured
#: slot is a different fact from one that fires long after it, and it must not
#: consume `morning-brief:YYYY-MM-DD` — see `boot_window_decision`.
ST_EARLY_BOOT = "skipped_early"
ST_DISABLED = "skipped_disabled"
ST_FAILED = "failed"
ST_DRY_RUN = "dry_run"
ST_OUTCOME_UNKNOWN = "outcome_unknown"

ALL_STATUSES = frozenset({ST_DELIVERED, ST_SUPPRESSED, ST_LATE_BOOT,
                          ST_EARLY_BOOT, ST_DISABLED, ST_FAILED, ST_DRY_RUN,
                          ST_OUTCOME_UNKNOWN})

# -- failure reasons (phase section 106) -----------------------------------

FR_PLANNER = "planner_error"
FR_RENDERER = "renderer_error"
FR_DELIVERY = "delivery_error"
FR_STALE = "stale_source"
FR_DUPLICATE = "duplicate_suppressed"
FR_DELIVERY_UNKNOWN = "delivery_outcome_unknown"

ALL_FAILURE_REASONS = frozenset({FR_PLANNER, FR_RENDERER, FR_DELIVERY,
                                 FR_STALE, FR_DUPLICATE, FR_DELIVERY_UNKNOWN})

#: Section 59. Three calls to the socket, never more.
MAX_DELIVERY_ATTEMPTS = 3
#: Seconds between attempt 1->2 and 2->3.
RETRY_BACKOFF_SECONDS = (2.0, 8.0)

#: The only channel this service can name (sections 61, 99). Any other value
#: in the configuration refuses to deliver rather than honouring it.
PERMITTED_CHANNEL = delivery.CHANNEL_DISCORD

DEFAULT_TIMEZONE = "Asia/Tokyo"
#: I2-AMENDMENT A1: the owner's existing morning slot is 08:30 JST.
DEFAULT_TIME = "08:30"
DEFAULT_GRACE_HOURS = 3
#: How far *before* the slot a run may still be treated as on time. Five
#: minutes is generous for the clock skew a `Persistent=true` timer can show
#: and far too small to admit a 02:00 boot. See `boot_window_decision`.
DEFAULT_EARLY_TOLERANCE_SECONDS = 300

#: `boot_window_decision` reasons.
BOOT_LATE = "late"
BOOT_EARLY = "early"


# --- small config helpers -------------------------------------------------

def _get(config, path: str, default=None):
    """Read `a.b.c` out of a nested dict, tolerating a partial config.

    `brief_service` never loads configuration itself — `plan_config.py` (T2)
    owns that — so it must survive a caller handing it a subset.
    """
    node = config or {}
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def _parse_hhmm(value: str) -> tuple:
    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts):
        raise ValueError(f"morning_brief.time must be HH:MM, got {value!r}")
    hour, minute = int(parts[0]), int(parts[1])
    second = int(parts[2]) if len(parts) == 3 else 0
    if not (0 <= hour < 24 and 0 <= minute < 60 and 0 <= second < 60):
        raise ValueError(f"morning_brief.time out of range: {value!r}")
    return hour, minute, second


def scheduled_datetime(now: datetime, config: dict) -> datetime:
    """Today's configured brief time, as an aware datetime in `timezone`."""
    tz = ZoneInfo(_get(config, "timezone", DEFAULT_TIMEZONE))
    hour, minute, second = _parse_hhmm(_get(config, "morning_brief.time",
                                            DEFAULT_TIME))
    local = now.astimezone(tz)
    return local.replace(hour=hour, minute=minute, second=second,
                         microsecond=0)


def boot_window_decision(now: datetime, config: dict) -> tuple:
    """`(reason, delta_seconds)` — the two-sided window of section 56.

    ``reason`` is ``None`` (send), :data:`BOOT_LATE` or :data:`BOOT_EARLY`.

    The late half is section 56 as written: a boot more than
    ``late_boot_grace_hours`` past the slot has missed the morning, and sending
    then is worse than not sending. The boundary is inclusive on the sending
    side ("grace window <= 3h: send").

    **The early half is what protects AMENDMENT A3.** The systemd timer is
    ``Persistent=true``, so a boot after a missed elapse fires the unit
    immediately at whatever hour that is. With only an upper bound, a Pi that
    came back at 02:00 delivered *today's* brief to Discord at 02:00 —
    unmetered, with no quiet-hours logic anywhere on the proactive path — and
    it consumed ``morning-brief:YYYY-MM-DD``, so the real 08:30 elapse then
    answered ``suppressed_duplicate`` and the owner never got a brief at the
    configured hour. AMENDMENT A3 separates the brief from the attention engine
    by *time*; an unbounded early side dissolves that premise.

    So a run more than ``early_tolerance_seconds`` before the slot is skipped
    with its own status, and the still-pending scheduled elapse delivers
    normally. The tolerance stays large enough for real clock skew on a
    `Persistent=true` timer and small enough that no plausible boot hour fits
    inside it.
    """
    scheduled = scheduled_datetime(now, config)
    tz = scheduled.tzinfo
    grace_hours = float(_get(config, "morning_brief.late_boot_grace_hours",
                             DEFAULT_GRACE_HOURS))
    early_tolerance = abs(float(_get(config,
                                     "morning_brief.early_tolerance_seconds",
                                     DEFAULT_EARLY_TOLERANCE_SECONDS)))
    delta = (now.astimezone(tz) - scheduled).total_seconds()
    if delta > grace_hours * 3600.0:
        return BOOT_LATE, delta
    if delta < -early_tolerance:
        return BOOT_EARLY, delta
    return None, delta


def late_boot_decision(now: datetime, config: dict) -> tuple:
    """`(should_skip, delta_seconds)` for section 56.

    Split out from `run_scheduled_brief` so the boundary can be tested on both
    sides without a database, a renderer or a socket. ``should_skip`` is true
    for either half of :func:`boot_window_decision`; callers that need to tell
    them apart use that function directly.
    """
    reason, delta = boot_window_decision(now, config)
    return reason is not None, delta


def local_date_for(now: datetime, config: dict) -> str:
    tz = ZoneInfo(_get(config, "timezone", DEFAULT_TIMEZONE))
    return now.astimezone(tz).strftime("%Y-%m-%d")


# --- default component resolution (lazy, so T2/T3 stay decoupled) ---------

def _default_open_events(config: dict) -> sqlite3.Connection:
    """A read-only connection to the canonical database (section 64).

    Two independent guards, because one of them is advisory:
      * `mode=ro` in the URI, so SQLite itself refuses a write; and
      * `PRAGMA query_only = ON`, which refuses UPDATE and CREATE TABLE alike.
    Verified on the Pi on 2026-09-08: `events.db` is `journal_mode = delete`,
    with no `-wal`/`-shm` companion, so a read-only open needs **no** write
    access to the directory holding it. That is why the service unit can mount
    `/srv/hermes/hermes-data/events` `ReadOnlyPaths=` — an OS-level guarantee
    of section 64 rather than a promise made in code.
    """
    path = _get(config, "paths.events_db",
                "/srv/hermes/hermes-data/events/events.db")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only = ON")
    return con


def _default_planner():
    from . import planner  # T2. Imported here so this module loads without it.
    return planner.build_daily_plan


def _default_renderer():
    from . import renderer  # T3.
    return renderer.render_brief


def _default_client(config: dict):
    return delivery.DeliveryClient(
        _get(config, "paths.gateway_proactive_socket",
             delivery.DEFAULT_SOCKET_PATH),
        timeout=float(_get(config, "morning_brief.socket_timeout_seconds", 30.0)),
    )


# --- the run --------------------------------------------------------------

def run_scheduled_brief(config: dict, *, now=None, trigger: str = "timer",
                        dry_run: bool = False,
                        planner=None, renderer=None, client=None,
                        state=None, open_events=None, sleep=None) -> dict:
    """Execute one scheduled morning brief. Returns the outcome; never raises.

    The keyword-only injection points beyond the contract's signature
    (`planner`, `renderer`, `client`, `state`, `open_events`, `sleep`) exist so
    that this module is testable against stubs while `planner.py` (T2) and
    `renderer.py` (T3) are being written in parallel, and so that the bounded
    retry can be proved to terminate without waiting ten real seconds. All of
    them default to the production component.
    """
    sleep = sleep or time.sleep
    planning_dir = _get(config, "paths.planning_dir",
                        "/srv/hermes/hermes-data/planning")
    tz_name = _get(config, "timezone", DEFAULT_TIMEZONE)
    try:
        tz = ZoneInfo(tz_name)
    except Exception as exc:
        # A configuration this broken cannot produce a correct local date, and
        # guessing one would silently move the owner's morning. Fail visibly.
        log.error("timezone %r is not usable: %s", tz_name, exc)
        _health(planning_dir, status="fail", failure_reason=FR_PLANNER)
        return {"status": ST_FAILED, "failure_reason": FR_PLANNER,
                "local_date": None, "trigger": trigger, "channel": None,
                "notification_id": None, "plan_hash": None, "renderer": None,
                "attempts": 0, "warnings": [], "timings_ms": {},
                "detail": f"config_error: timezone {tz_name!r}: {exc}"}
    now = now or datetime.now(tz)
    if now.tzinfo is None:
        # A naive clock is a programming error in the caller, not a runtime
        # condition: there is no correct guess between UTC and JST.
        raise ValueError("now must be timezone-aware")

    local_date = local_date_for(now, config)
    outcome = {
        "status": None,
        "failure_reason": None,
        "local_date": local_date,
        "trigger": trigger,
        "channel": None,
        "notification_id": None,
        "plan_hash": None,
        "renderer": None,
        "attempts": 0,
        "warnings": [],
        "timings_ms": {},
        "detail": None,
    }

    # 1. disabled -----------------------------------------------------
    if not _get(config, "morning_brief.enabled", True):
        outcome["status"] = ST_DISABLED
        _record(state, planning_dir, local_date, brief_state.SKIPPED,
                last_error="disabled")
        _health(planning_dir, status="disabled", failure_reason=None)
        return outcome

    # 2/3. boot window (both sides) -------------------------------------
    if trigger != "manual":
        try:
            skip_reason, delta = boot_window_decision(now, config)
        except ValueError as exc:
            # A malformed `morning_brief.time` maps to `planner_error`: phase
            # section 106's vocabulary is closed to five values and this is the
            # one that means "no plan was produced". `detail` carries
            # `config_error:` so the two are still told apart in the journal.
            log.error("morning_brief.time is unusable: %s", exc)
            outcome.update(status=ST_FAILED, failure_reason=FR_PLANNER,
                           detail=f"config_error: {exc}")
            _record(state, planning_dir, local_date, brief_state.FAILED,
                    last_error=f"config_error: {exc}"[:500])
            _health(planning_dir, status="fail", failure_reason=FR_PLANNER)
            return outcome
        if skip_reason == BOOT_LATE:
            grace = _get(config, "morning_brief.late_boot_grace_hours",
                         DEFAULT_GRACE_HOURS)
            log.info("late boot: %.0fs past the %s slot (grace %sh) — skipping",
                     delta, _get(config, "morning_brief.time", DEFAULT_TIME),
                     grace)
            outcome["status"] = ST_LATE_BOOT
            outcome["detail"] = f"late_by_seconds={int(delta)} grace_hours={grace}"
            _record(state, planning_dir, local_date, brief_state.SKIPPED,
                    last_error=f"late_boot:{int(delta)}s")
            _health(planning_dir, status="skipped", failure_reason=None)
            return outcome
        if skip_reason == BOOT_EARLY:
            tolerance = _get(config, "morning_brief.early_tolerance_seconds",
                             DEFAULT_EARLY_TOLERANCE_SECONDS)
            log.info("early boot: %.0fs before the %s slot (tolerance %ss) — "
                     "skipping; the scheduled elapse still delivers",
                     -delta, _get(config, "morning_brief.time", DEFAULT_TIME),
                     tolerance)
            outcome["status"] = ST_EARLY_BOOT
            outcome["detail"] = (f"early_by_seconds={int(-delta)} "
                                 f"tolerance_seconds={tolerance}")
            # Deliberately NOT recorded against `local_date`: the day's brief
            # has not been decided, and the 08:30 elapse must still be able to
            # produce and deliver it.
            _health(planning_dir, status="skipped", failure_reason=None)
            return outcome

    # 4. local idempotency pre-check ----------------------------------
    own_state = state is None
    st, con = _resolve_state(state, planning_dir)
    try:
        if st is not None and st.has_unknown_outcome(local_date):
            outcome.update(status=ST_OUTCOME_UNKNOWN,
                           failure_reason=FR_DELIVERY_UNKNOWN,
                           channel=PERMITTED_CHANNEL,
                           notification_id=delivery.notification_id_for(local_date))
            _health(planning_dir, status="unknown", failure_reason=FR_DELIVERY_UNKNOWN)
            return outcome
        if st is not None and st.is_settled(local_date):
            log.info("brief for %s already settled locally — not regenerating",
                     local_date)
            outcome.update(status=ST_SUPPRESSED, failure_reason=FR_DUPLICATE,
                           channel=PERMITTED_CHANNEL,
                           notification_id=delivery.notification_id_for(local_date))
            _health(planning_dir, status="ok", failure_reason=FR_DUPLICATE)
            return outcome

        # Channel policy, checked before anything is generated (sections 61,
        # 99). A configuration that asks for LINE does not get a LINE push and
        # does not get a silent Discord one either — it gets a refusal.
        channel = str(_get(config, "morning_brief.channel",
                           PERMITTED_CHANNEL) or "").strip().lower()
        if channel != PERMITTED_CHANNEL:
            log.error("morning_brief.channel=%r is not permitted; the routine "
                      "brief is Discord-only (sections 61, 99)", channel)
            outcome.update(status=ST_FAILED, failure_reason=FR_DELIVERY,
                           detail=f"channel_not_permitted:{channel}")
            _record(st, planning_dir, local_date, brief_state.FAILED,
                    last_error=f"channel_not_permitted:{channel}")
            _health(planning_dir, status="fail", failure_reason=FR_DELIVERY)
            return outcome
        outcome["channel"] = PERMITTED_CHANNEL

        # 5. build the plan -------------------------------------------
        build = planner or _default_planner
        t0 = time.monotonic()
        try:
            build_fn = build() if build is _default_planner else build
            events = (open_events or _default_open_events)(config)
            try:
                plan = build_fn(events, date=local_date, timezone=tz_name,
                                now=now, config=config)
            finally:
                try:
                    events.close()
                except Exception:
                    pass
        except Exception as exc:
            # Section 60: no fabricated fallback brief, ever. Record FAIL and
            # send nothing that could be mistaken for a brief.
            log.exception("plan generation failed for %s", local_date)
            outcome.update(status=ST_FAILED, failure_reason=FR_PLANNER,
                           detail=f"planner_error: {type(exc).__name__}: {exc}")
            _record(st, planning_dir, local_date, brief_state.FAILED,
                    last_error=outcome["detail"][:500])
            _health(planning_dir, status="fail", failure_reason=FR_PLANNER)
            _maybe_failure_notice(config, client, local_date, "plan", exc)
            return outcome
        outcome["timings_ms"]["plan"] = round((time.monotonic() - t0) * 1000, 1)

        if not isinstance(plan, dict):
            outcome.update(status=ST_FAILED, failure_reason=FR_PLANNER,
                           detail="planner_error: build_daily_plan returned "
                                  f"{type(plan).__name__}, expected dict")
            _record(st, planning_dir, local_date, brief_state.FAILED,
                    last_error=outcome["detail"][:500])
            _health(planning_dir, status="fail", failure_reason=FR_PLANNER)
            return outcome

        plan_hash = str(plan.get("plan_hash") or "")
        outcome["plan_hash"] = plan_hash
        warnings = list(plan.get("warnings") or [])
        outcome["warnings"] = warnings
        stale = "data_freshness_warning" in warnings
        if st is not None:
            st.begin_run(local_date, plan_hash,
                         generated_at=str(plan.get("generated_at") or ""))
        _health(planning_dir, last_plan_generated=now.isoformat(),
                last_plan_hash=plan_hash or None)

        # 6. render ----------------------------------------------------
        render = renderer or _default_renderer
        t0 = time.monotonic()
        try:
            render_fn = render() if render is _default_renderer else render
            rendered = render_fn(
                plan, mode=_get(config, "renderer.mode", "auto"),
                config=config, now=now,
            )
            text = str((rendered or {}).get("text") or "").strip()
        except Exception as exc:
            # `render_brief` is contracted never to raise (contract section 5),
            # and its template path needs no network. If it raised anyway the
            # renderer is unavailable rather than merely degraded, and section
            # 60's rule applies by the same logic: nothing is invented here.
            log.exception("render failed for %s", local_date)
            outcome.update(status=ST_FAILED, failure_reason=FR_RENDERER,
                           detail=f"renderer_error: {type(exc).__name__}: {exc}")
            _record(st, planning_dir, local_date, brief_state.FAILED,
                    last_error=outcome["detail"][:500])
            _health(planning_dir, status="fail", failure_reason=FR_RENDERER)
            _maybe_failure_notice(config, client, local_date, "render", exc)
            return outcome
        outcome["timings_ms"]["render"] = round((time.monotonic() - t0) * 1000, 1)
        outcome["renderer"] = (rendered or {}).get("renderer")

        if not text:
            outcome.update(status=ST_FAILED, failure_reason=FR_RENDERER,
                           detail="renderer_error: empty text")
            _record(st, planning_dir, local_date, brief_state.FAILED,
                    last_error=outcome["detail"])
            _health(planning_dir, status="fail", failure_reason=FR_RENDERER)
            return outcome

        notification_id = delivery.notification_id_for(local_date)
        outcome["notification_id"] = notification_id

        # 7. deliver ---------------------------------------------------
        if dry_run:
            outcome.update(status=ST_DRY_RUN,
                           failure_reason=FR_STALE if stale else None,
                           detail=f"chars={len(text)}")
            outcome["text_chars"] = len(text)
            outcome["text"] = text
            return outcome

        send = client if client is not None else _default_client(config)
        t0 = time.monotonic()
        result, attempts = _deliver_with_retry(send, notification_id, text,
                                               sleep=sleep, state=st,
                                               local_date=local_date)
        outcome["timings_ms"]["deliver"] = round((time.monotonic() - t0) * 1000, 1)
        outcome["attempts"] = attempts
        outcome["detail"] = result.detail

        _health(planning_dir, last_delivery_attempt=now.isoformat())
        if result.ok:
            if st is not None:
                st.mark_delivered(local_date, channel=PERMITTED_CHANNEL)
            outcome["status"] = ST_DELIVERED
            # A stale source does not stop the brief (contract section 4 /
            # phase section 37) but it must stay visible, so it is reported as
            # the reason the run was only degraded — not as a failure.
            outcome["failure_reason"] = FR_STALE if stale else None
            _health(planning_dir, last_delivery_success=now.isoformat(),
                    status="degraded" if stale else "ok",
                    failure_reason=FR_STALE if stale else None)
            log.info("morning brief delivered %s (%s, %d attempt(s), %d chars)",
                     notification_id, result.outcome, attempts, len(text))
        elif result.outcome == delivery.OUTCOME_UNKNOWN:
            if st is not None:
                st.mark_status(local_date, brief_state.UNKNOWN,
                               last_error=result.detail, channel=PERMITTED_CHANNEL)
            outcome.update(status=ST_OUTCOME_UNKNOWN,
                           failure_reason=FR_DELIVERY_UNKNOWN)
            _health(planning_dir, status="unknown", failure_reason=FR_DELIVERY_UNKNOWN)
            log.warning("morning brief %s outcome unknown — automatic retry held",
                        notification_id)
        elif result.outcome in (delivery.IN_FLIGHT, delivery.CONTENT_MISMATCH):
            # The gateway already holds a row for this date. Stop, and record
            # it as suppressed rather than failed — nothing is wrong.
            if st is not None:
                st.mark_status(local_date, brief_state.SUPPRESSED,
                               last_error=result.detail,
                               channel=PERMITTED_CHANNEL)
            outcome.update(status=ST_SUPPRESSED, failure_reason=FR_DUPLICATE)
            _health(planning_dir, status="ok", failure_reason=FR_DUPLICATE)
            log.info("morning brief %s already handled by the gateway (%s)",
                     notification_id, result.detail)
        else:
            if st is not None:
                st.mark_status(local_date, brief_state.FAILED,
                               last_error=f"{result.outcome}: {result.detail}"[:500],
                               channel=PERMITTED_CHANNEL)
            outcome.update(status=ST_FAILED, failure_reason=FR_DELIVERY)
            _health(planning_dir, status="fail", failure_reason=FR_DELIVERY)
            log.error("morning brief delivery failed %s after %d attempt(s): "
                      "%s %s", notification_id, attempts, result.outcome,
                      result.detail)
        return outcome
    finally:
        if own_state and con is not None:
            try:
                con.close()
            except Exception:
                pass


def _deliver_with_retry(client, notification_id: str, text: str, *,
                        sleep, state=None, local_date: str = "") -> tuple:
    """At most `MAX_DELIVERY_ATTEMPTS` socket calls. Returns `(Result, n)`.

    Terminates unconditionally: the loop runs a fixed number of times and every
    non-retryable outcome returns early. There is no path on which the number
    of socket calls exceeds three (section 59).
    """
    result = None
    for attempt in range(1, MAX_DELIVERY_ATTEMPTS + 1):
        if state is not None and local_date:
            state.note_attempt(local_date)
        result = client.send_brief(notification_id=notification_id, text=text)
        if result.terminal:
            return result, attempt
        if attempt < MAX_DELIVERY_ATTEMPTS:
            backoff = RETRY_BACKOFF_SECONDS[min(attempt - 1,
                                                len(RETRY_BACKOFF_SECONDS) - 1)]
            log.warning("delivery attempt %d/%d failed (%s: %s); retrying in %ss",
                        attempt, MAX_DELIVERY_ATTEMPTS, result.outcome,
                        result.detail, backoff)
            sleep(backoff)
    return result, MAX_DELIVERY_ATTEMPTS


def _maybe_failure_notice(config, client, local_date: str, stage: str,
                          exc: BaseException) -> None:
    """Phase section 60's optional short failure notice — Discord only, opt-in.

    It is off by default. A brief that cannot be generated is usually a source
    or configuration problem that persists for days, and a daily "it broke"
    push is exactly the notification flood section 126 forbids; the health file
    and the journal are the primary channel. When enabled, the notice carries
    its own `morning-brief-failure:` id — distinct from the brief's — so it can
    never be mistaken for a brief and gets one-per-day idempotency from the
    same gateway constraint.

    Called for a planner or renderer failure only, never for a delivery
    failure: announcing "delivery is broken" down the channel that just refused
    the brief cannot work, and the second attempt would double the noise.
    """
    if not _get(config, "morning_brief.notify_on_failure", False):
        return
    try:
        send = client if client is not None else _default_client(config)
        text = (f"【自動通知】{local_date} のモーニングブリーフを生成できませんでした"
                f"（段階: {stage}）。本文は送信していません。"
                f"詳細は morning-brief-health.json と journalctl -u "
                f"hermes-morning-brief を確認してください。")
        res = send.send_brief(
            notification_id=f"morning-brief-failure:{local_date}",
            text=text, reason="morning_brief_failure",
        )
        log.info("failure notice for %s: %s", local_date, res.outcome)
    except Exception:
        # A failure notice that fails must not change the outcome of the run.
        log.exception("failure notice could not be sent")


# --- state / health plumbing ----------------------------------------------

def _resolve_state(state, planning_dir: str) -> tuple:
    """`(BriefState|None, owned_connection|None)`.

    A state store that cannot be opened is logged and degraded to `None`
    rather than failing the run: the gateway, not this file, is authoritative
    for one-brief-per-day, so losing the local record loses the pre-check and
    the bookkeeping, not the guarantee.
    """
    if state is not None:
        return state, None
    try:
        con = brief_state.open_state(brief_state.state_db_path(planning_dir))
    except Exception:
        log.exception("planning state unavailable at %s — continuing without "
                      "the local pre-check (the gateway remains authoritative)",
                      planning_dir)
        return None, None
    return brief_state.BriefState(con), con


def _record(state, planning_dir: str, local_date: str, status: str,
            *, last_error=None) -> None:
    st, con = _resolve_state(state, planning_dir)
    if st is None:
        return
    try:
        st.mark_status(local_date, status, last_error=last_error)
    except Exception:
        log.exception("could not record %s for %s", status, local_date)
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


def _health(planning_dir: str, *, status=None, failure_reason=None, **fields):
    """Write the health file, never letting it break the run (section 107)."""
    updates = dict(fields)
    if status is not None:
        updates["status"] = status
        updates["last_failure_reason"] = failure_reason
    try:
        brief_state.write_health(planning_dir, updates)
    except Exception:
        log.exception("could not write the health file in %s", planning_dir)
