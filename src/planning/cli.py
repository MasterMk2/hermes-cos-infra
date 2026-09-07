#!/usr/bin/env python3
"""Morning Brief command line — the systemd entry point and the manual view.

    python3 -m planning.cli plan   [--date YYYY-MM-DD] [--tz Asia/Tokyo] [--json]
    python3 -m planning.cli brief  [--date ...] [--mode auto|llm|template] [--json]
    python3 -m planning.cli run    [--trigger timer|manual] [--dry-run] [--json]
    python3 -m planning.cli health [--json]

`run` is what `hermes-morning-brief.service` executes. **`plan`, `brief` and
`run --dry-run` never deliver** — they open no socket at all, which is the
property that makes it safe to inspect the morning's output by hand.

Exit codes, chosen so systemd can tell the three apart:

    0   delivered, suppressed_duplicate, skipped_late_boot, skipped_disabled,
        dry_run — the run did the right thing
    1   failed — a real failure, visible in `systemctl status`
    78  EX_CONFIG — the configuration or an owned module is missing or
        malformed. A restart cannot fix either, which is why the unit carries
        `RestartPreventExitStatus=78`, matching `attention-engine.service`.

`--measure` adds a `resources` block (phase sections 103, 104): peak RSS after
each stage, per-stage wall clock, and the latency of one probe query against
`events.db`. `bin/morning-brief-status.sh` wraps it with the host figures
(RAM, swap delta) that a process cannot see about itself.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

EX_OK = 0
EX_FAIL = 1
EX_CONFIG = 78

#: Env override named in I2-CONTRACT.md section 2.
CONFIG_ENV = "HERMES_PLANNING_CONFIG"
DEFAULT_CONFIG_PATH = "/srv/hermes/planning/config.yaml"

_OK_STATUSES = {"delivered", "suppressed_duplicate", "skipped_late_boot",
                "skipped_disabled", "dry_run"}


class ConfigError(Exception):
    """Anything that should exit 78 rather than be retried."""


# --- lazy component resolution -------------------------------------------
#
# `plan_config.py` (T2) and `renderer.py` (T3) are separate deliverables. They
# are imported at the point of use so that `cli.py health` — the one command an
# operator runs when something is broken — keeps working even if one of them is
# missing, and so that a missing module produces a sentence rather than a
# traceback at import time.

def _load_config(path: str | None):
    try:
        from . import plan_config
    except ImportError as exc:
        raise ConfigError(
            f"planning.plan_config is not installed ({exc}). Deploy the whole "
            "planning package, not just the service files."
        ) from exc
    resolved = path or os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG_PATH
    try:
        return plan_config.load_config(resolved), resolved
    except Exception as exc:
        raise ConfigError(f"{resolved}: {exc}") from exc


def _planner():
    try:
        from . import planner
    except ImportError as exc:
        raise ConfigError(f"planning.planner is not installed ({exc})") from exc
    return planner.build_daily_plan


def _renderer():
    try:
        from . import renderer
    except ImportError as exc:
        raise ConfigError(f"planning.renderer is not installed ({exc})") from exc
    return renderer.render_brief


# --- resource measurement (phase section 103) ----------------------------

def _rss_kb() -> int | None:
    """Peak RSS of this process in KiB, or None where it cannot be read.

    `ru_maxrss` is a high-water mark, so sampling it after each stage gives the
    peak *up to* that stage; the difference between two samples is the extra
    the later stage demanded. On Linux it is already KiB. There is no reading
    on Windows, where these commands are only ever run against stubs.
    """
    try:
        import resource
    except ImportError:
        return None
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


def _events_db_latency_ms(config) -> float | None:
    """One trivial indexed read, timed. Never fails the command."""
    from . import brief_service
    try:
        con = brief_service._default_open_events(config)
    except Exception:
        return None
    try:
        t0 = time.monotonic()
        con.execute("SELECT COUNT(*) FROM commitments").fetchone()
        return round((time.monotonic() - t0) * 1000, 2)
    except Exception:
        return None
    finally:
        try:
            con.close()
        except Exception:
            pass


# --- commands -------------------------------------------------------------

def _now(args, config) -> datetime:
    tz = ZoneInfo(args.tz or (config or {}).get("timezone") or "Asia/Tokyo")
    return datetime.now(tz)


def cmd_plan(args) -> int:
    config, _ = _load_config(args.config)
    build = _planner()
    from . import brief_service
    now = _now(args, config)
    date = args.date or brief_service.local_date_for(now, config)
    tz_name = args.tz or config.get("timezone", "Asia/Tokyo")

    rss_before = _rss_kb()
    con = brief_service._default_open_events(config)
    t0 = time.monotonic()
    try:
        plan = build(con, date=date, timezone=tz_name, now=now, config=config)
    finally:
        con.close()
    elapsed = round((time.monotonic() - t0) * 1000, 1)

    if args.measure:
        plan = dict(plan)
        plan["resources"] = {
            "plan_ms": elapsed,
            "rss_before_kb": rss_before,
            "rss_peak_kb": _rss_kb(),
            "events_db_query_ms": _events_db_latency_ms(config),
        }
    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_plan_summary(plan, elapsed)
    return EX_OK


def _print_plan_summary(plan, elapsed_ms) -> None:
    print(f"date        {plan.get('date')}  ({plan.get('timezone')})")
    print(f"generated   {plan.get('generated_at')}  in {elapsed_ms} ms")
    print(f"plan_hash   {plan.get('plan_hash')}")
    counts = plan.get("counts") or {}
    for name in ("scheduled", "must_do", "overdue", "next_up", "waiting_for",
                 "backlog", "conflicts", "filtered_noise"):
        entry = counts.get(name)
        if isinstance(entry, dict):
            print(f"  {name:<14} {entry.get('shown')} / {entry.get('total')}")
    warnings = plan.get("warnings")
    if warnings:
        print(f"warnings    {', '.join(warnings)}")


def cmd_brief(args) -> int:
    config, _ = _load_config(args.config)
    build = _planner()
    render = _renderer()
    from . import brief_service
    now = _now(args, config)
    date = args.date or brief_service.local_date_for(now, config)
    tz_name = args.tz or config.get("timezone", "Asia/Tokyo")

    con = brief_service._default_open_events(config)
    t0 = time.monotonic()
    try:
        plan = build(con, date=date, timezone=tz_name, now=now, config=config)
    finally:
        con.close()
    plan_ms = round((time.monotonic() - t0) * 1000, 1)
    rss_after_plan = _rss_kb()

    t0 = time.monotonic()
    rendered = render(plan, mode=args.mode, config=config, now=now)
    render_ms = round((time.monotonic() - t0) * 1000, 1)

    if args.json:
        payload = dict(rendered)
        payload["plan_hash"] = plan.get("plan_hash")
        payload["date"] = plan.get("date")
        if args.measure:
            payload["resources"] = {
                "plan_ms": plan_ms, "render_ms": render_ms,
                "rss_after_plan_kb": rss_after_plan,
                "rss_peak_kb": _rss_kb(),
                "events_db_query_ms": _events_db_latency_ms(config),
            }
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(rendered.get("text", ""))
        print()
        print(f"-- renderer={rendered.get('renderer')} "
              f"model={rendered.get('model')} "
              f"fallback_reason={rendered.get('fallback_reason')} "
              f"chars={len(rendered.get('text') or '')} "
              f"plan={plan_ms}ms render={render_ms}ms")
        if args.measure:
            print(f"-- rss_after_plan={rss_after_plan}kB "
                  f"rss_peak={_rss_kb()}kB "
                  f"events_db_query={_events_db_latency_ms(config)}ms")
    return EX_OK


def cmd_run(args) -> int:
    config, _ = _load_config(args.config)
    from . import brief_service
    rss_before = _rss_kb()
    t0 = time.monotonic()
    outcome = brief_service.run_scheduled_brief(
        config, trigger=args.trigger, dry_run=args.dry_run,
    )
    outcome["timings_ms"]["total"] = round((time.monotonic() - t0) * 1000, 1)
    if args.measure:
        outcome["resources"] = {
            "rss_before_kb": rss_before,
            "rss_peak_kb": _rss_kb(),
            "events_db_query_ms": _events_db_latency_ms(config),
        }

    if args.json:
        print(json.dumps(outcome, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        text = outcome.pop("text", None)
        print(f"status          {outcome['status']}")
        print(f"failure_reason  {outcome['failure_reason']}")
        print(f"local_date      {outcome['local_date']}  trigger={outcome['trigger']}")
        print(f"channel         {outcome['channel']}")
        print(f"notification_id {outcome['notification_id']}")
        print(f"plan_hash       {outcome['plan_hash']}")
        print(f"renderer        {outcome['renderer']}  attempts={outcome['attempts']}")
        if outcome.get("warnings"):
            print(f"warnings        {', '.join(outcome['warnings'])}")
        if outcome.get("detail"):
            print(f"detail          {outcome['detail']}")
        print(f"timings_ms      {outcome['timings_ms']}")
        if text and args.dry_run:
            print("\n--- brief (NOT delivered) ---")
            print(text)
    return EX_OK if outcome["status"] in _OK_STATUSES else EX_FAIL


def cmd_health(args) -> int:
    config, path = _load_config(args.config)
    from . import brief_state
    planning_dir = ((config.get("paths") or {}).get("planning_dir")
                    or "/srv/hermes/hermes-data/planning")
    health = brief_state.read_health(planning_dir)
    if args.json:
        print(json.dumps(health, ensure_ascii=False, indent=2, sort_keys=True))
        return EX_OK
    print(f"config          {path}")
    print(f"planning_dir    {planning_dir}")
    for key in brief_state.HEALTH_FIELDS:
        print(f"  {key:<24} {health.get(key)}")
    db = brief_state.state_db_path(planning_dir)
    if os.path.exists(db):
        try:
            con = brief_state.open_state(db)
            try:
                rows = con.execute(
                    "SELECT local_date, delivery_status, attempts, "
                    "delivered_at, last_error FROM morning_brief_runs "
                    "ORDER BY local_date DESC LIMIT 7"
                ).fetchall()
            finally:
                con.close()
            print("\nrecent runs (local record; the gateway is authoritative)")
            for row in rows:
                print(f"  {row['local_date']}  {row['delivery_status']:<11} "
                      f"attempts={row['attempts']}  "
                      f"delivered_at={row['delivered_at']}"
                      + (f"  err={row['last_error']}" if row["last_error"] else ""))
        except Exception as exc:
            print(f"\n  (state database unreadable: {exc})")
    else:
        print(f"\n  (no state database yet at {db})")
    return EX_OK


# --- argument parsing -----------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="planning.cli",
        description="Hermes Morning Brief — plan, render, deliver, inspect.",
    )
    parser.add_argument("--config", help=f"default: ${CONFIG_ENV} or "
                                         f"{DEFAULT_CONFIG_PATH}")
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("--json", action="store_true")
        sp.add_argument("--measure", action="store_true",
                        help="add a resources block (phase section 103)")
        return sp

    p = common(sub.add_parser("plan", help="build the plan; never delivers"))
    p.add_argument("--date")
    p.add_argument("--tz")
    p.set_defaults(func=cmd_plan)

    p = common(sub.add_parser("brief", help="plan + render; never delivers"))
    p.add_argument("--date")
    p.add_argument("--tz")
    p.add_argument("--mode", default="auto", choices=("auto", "llm", "template"))
    p.set_defaults(func=cmd_brief)

    p = common(sub.add_parser("run", help="the scheduled run (systemd)"))
    p.add_argument("--trigger", default="timer", choices=("timer", "manual"))
    p.add_argument("--dry-run", action="store_true",
                   help="generate and print; open no socket")
    p.add_argument("--tz")
    p.set_defaults(func=cmd_run)

    p = common(sub.add_parser("health", help="read the health file and record"))
    p.set_defaults(func=cmd_health)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    for name in ("date", "tz", "mode", "measure"):
        if not hasattr(args, name):
            setattr(args, name, None)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EX_CONFIG
    except KeyboardInterrupt:
        return EX_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
