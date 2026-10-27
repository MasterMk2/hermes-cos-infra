#!/usr/bin/env python3
"""Calendar Tentative Registration — command line and timer entry point.

    ct_service.py run         one run (the timer calls this). Honours
                              behaviour.mode; prints counts only.
    ct_service.py plan        what a run would do, from local state, with
                              titles. No credential, no Google call, no write.
    ct_service.py status      counts and the last run. No titles.
    ct_service.py check       configuration, credentials (presence only),
                              database access. Exit 0 or 78.
    ct_service.py repropose <commitment_id>
                              let a rejected/withdrawn/failed entry be
                              proposed again, under a new event id.

Exit 78 (EX_CONFIG) is "configured to do nothing" and pairs with
RestartPreventExitStatus=78, as in the other Hermes units.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import ct_engine
import ct_mail
import ct_select
import ct_store as store
from ct_config import ConfigError, load_config
from ct_google import ApiError, CredentialUnavailable, OAuthCredentials, TentativeCalendarClient
from ct_notify import GatewayNotifier

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONFIG = 78

log = logging.getLogger("calendar_tentative")


def open_readonly(path) -> sqlite3.Connection:
    """`mode=ro` plus `query_only`: SQLite itself refuses any write."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 15000")
    con.execute("PRAGMA query_only = ON")
    return con


def open_index(cfg):
    path = cfg.paths["calendar_index_db"]
    if not path:
        return None
    try:
        con = open_readonly(path)
        con.execute("SELECT 1 FROM calendar_events LIMIT 1")
        return con
    except sqlite3.Error as exc:
        # Optional input: without it the service only loses the duplicate
        # check against real events and the "moved = confirmed" signal.
        log.warning("calendar index unavailable (%s); continuing without it",
                    type(exc).__name__)
        return None


@contextlib.contextmanager
def run_lock(state_db: str):
    """One run at a time: a manual run cannot race the timer's."""
    directory = Path(state_db).parent
    directory.mkdir(parents=True, exist_ok=True)
    handle = open(directory / ".run.lock", "a+")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        handle.close()


def open_gmail_index(cfg):
    path = cfg.paths["gmail_index_db"]
    if not path or cfg.selection["gmail_only"] in ("off", "all"):
        return None
    try:
        con = open_readonly(path)
        if ct_mail.index_usable(con):
            return con
        con.close()
        log.warning("gmail index has an unexpected shape; Gmail-only rows are held back")
    except sqlite3.Error as exc:
        # Fail closed: without the screen, mail-only rows are not proposed.
        log.warning("gmail index unavailable (%s); Gmail-only rows are held back",
                    type(exc).__name__)
    return None


def _select(cfg, now):
    events = open_readonly(cfg.paths["events_db"])
    index = open_index(cfg)
    gmail = open_gmail_index(cfg)
    try:
        return ct_select.select(events, cfg=cfg, now=now, index_con=index, gmail_con=gmail)
    finally:
        events.close()
        for con in (index, gmail):
            if con is not None:
                con.close()


def _print(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def _counts_only(report: ct_engine.RunReport) -> dict:
    data = report.as_dict()
    data["plan"] = {k: (len(v) if isinstance(v, list) else v) for k, v in data["plan"].items()}
    return data


def cmd_plan(cfg, now, *, with_titles: bool) -> int:
    selection = _select(cfg, now)
    path = cfg.paths["state_db"]
    if os.path.exists(path):
        state = open_readonly(path)
    else:
        state = store.init_db(sqlite3.connect(":memory:"))
        state.row_factory = sqlite3.Row
    try:
        report = ct_engine.plan(state, selection, cfg=cfg, now=now)
    finally:
        state.close()
    _print(report.as_dict() if with_titles else _counts_only(report))
    return EXIT_OK


def cmd_run(cfg, now, *, force_dry_run: bool) -> int:
    if force_dry_run or cfg.dry_run:
        return cmd_plan(cfg, now, with_titles=False)
    if not cfg.calendar_id:
        log.error("calendar.calendar_id is empty: create the tentative calendar with "
                  "ct_oauth.py --create-calendar and put its id in %s", cfg.path)
        return EXIT_CONFIG
    try:
        client = TentativeCalendarClient(OAuthCredentials.from_directory(cfg.credential_dir))
    except CredentialUnavailable as exc:
        log.error("credential unavailable: %s", exc)
        return EXIT_CONFIG
    notifier = (GatewayNotifier(cfg.notify["socket_path"],
                                timeout=float(cfg.notify["timeout_seconds"]))
                if cfg.notify["enabled"] else None)

    with run_lock(cfg.paths["state_db"]) as locked:
        if not locked:
            log.info("another run holds the lock; nothing to do")
            return EXIT_OK
        state = store.open_db(cfg.paths["state_db"])
        run_id = store.start_run(state, "apply")
        report = None
        try:
            selection = _select(cfg, now)
            report = ct_engine.run(cfg=cfg, state_con=state, selection=selection,
                                   client=client, now=now, notifier=notifier)
        except ct_engine.CalendarChanged as exc:
            store.finish_run(state, run_id, counts={}, skipped={}, error="calendar_changed")
            log.error("%s", exc)
            return EXIT_CONFIG
        except ApiError as exc:
            # Only token refresh can surface here; per-entry failures are
            # recorded on their rows and never abort a run.
            store.finish_run(state, run_id, counts={}, skipped={},
                             error=f"api:{exc.status}:{exc.code}")
            log.error("google api: %s:%s", exc.status, exc.code)
            return EXIT_CONFIG if exc.status in (400, 401, 403) and not exc.sent else EXIT_FAILURE
        except Exception as exc:
            store.finish_run(state, run_id, counts={}, skipped={}, error=type(exc).__name__)
            raise
        else:
            store.finish_run(state, run_id, counts=report.counts, skipped=report.skipped)
        finally:
            state.close()
    _print(_counts_only(report))
    return EXIT_OK


def cmd_status(cfg) -> int:
    path = cfg.paths["state_db"]
    if not os.path.exists(path):
        _print({"state_db": "absent", "calendar_id_configured": bool(cfg.calendar_id)})
        return EXIT_OK
    con = open_readonly(path)
    try:
        data = store.status_snapshot(con)
    finally:
        con.close()
    data["calendar_id_configured"] = bool(cfg.calendar_id)
    data["mode"] = cfg.behaviour["mode"]
    _print(data)
    return EXIT_OK


def cmd_check(cfg) -> int:
    ok = True
    log.info("config      : %s (mode=%s)", cfg.path, cfg.behaviour["mode"])
    if cfg.calendar_id:
        log.info("calendar    : configured")
    else:
        log.error("calendar    : calendar.calendar_id is empty (run ct_oauth.py --create-calendar)")
        ok = False

    directory = cfg.credential_dir
    for name, required in (("client-id", True), ("refresh-token", True), ("client-secret", False)):
        present = bool(directory) and (Path(directory) / name).is_file()
        log.info("credential  : %-14s %s", name, "present" if present else "absent")
        if required and not present:
            # Under systemd this is $CREDENTIALS_DIRECTORY. Run by hand, the
            # root-only source directory is legitimately unreadable.
            log.warning("credential %s not visible from this process "
                        "(expected when not run by systemd)", name)

    try:
        con = open_readonly(cfg.paths["events_db"])
        n = con.execute("SELECT COUNT(*) FROM commitments").fetchone()[0]
        con.close()
        log.info("events db   : readable (%d commitments)", n)
    except sqlite3.Error as exc:
        log.error("events db   : not readable (%s)", type(exc).__name__)
        ok = False

    index = open_index(cfg)
    if index is None:
        log.warning("calendar idx: unavailable (duplicate check and confirm detection off)")
    else:
        mine = 0
        if cfg.calendar_id:
            mine = index.execute("SELECT COUNT(*) FROM calendar_events WHERE calendar_id = ?",
                                 (cfg.calendar_id,)).fetchone()[0]
        index.close()
        log.info("calendar idx: readable")
        if mine:
            # Selecting the tentative calendar in H.1 would turn every
            # tentative entry into a calendar_authority commitment.
            log.error("calendar idx: the tentative calendar is selected in H.1 "
                      "(calendars.selected_ids); remove it there")
            ok = False

    mode = cfg.selection["gmail_only"]
    if mode in ("off", "all"):
        log.info("gmail screen: not used (gmail_only=%s)", mode)
    else:
        gmail = open_gmail_index(cfg)
        if gmail is None:
            log.warning("gmail screen: index unavailable; Gmail-only rows will be held back")
        else:
            try:
                total, categorized = gmail.execute(ct_mail._CATEGORY_SQL).fetchone()
            finally:
                gmail.close()
            log.info("gmail screen: readable (gmail_only=%s, %d received, %d with a Gmail "
                     "category)", mode, total or 0, categorized or 0)
            if total and not categorized:
                # Google turns smart features off by default in Japan. Then no
                # mail has a category and only mail tied to something the
                # owner did (sent, replied, wrote to the sender) can pass.
                log.warning("gmail screen: no received mail carries a Gmail category; Gmail "
                            "smart features look off, so mail from strangers will not pass")

    state_dir = Path(cfg.paths["state_db"]).parent
    writable = state_dir.is_dir() and os.access(state_dir, os.W_OK)
    log.info("state dir   : %s (%s)", state_dir, "writable" if writable else "not writable here")

    if cfg.notify["enabled"]:
        sock = cfg.notify["socket_path"]
        log.info("notify      : enabled, socket %s", "present" if os.path.exists(sock) else "absent")
    else:
        log.info("notify      : disabled")
    log.info("result      : %s", "ready" if ok else "NOT ready")
    return EXIT_OK if ok else EXIT_CONFIG


def cmd_repropose(cfg, commitment_id: str) -> int:
    with run_lock(cfg.paths["state_db"]) as locked:
        if not locked:
            log.error("a run is in progress; try again in a minute")
            return EXIT_FAILURE
        con = store.open_db(cfg.paths["state_db"])
        try:
            event_id = store.repropose(con, commitment_id, event_id_for=ct_select.event_id_for)
            con.commit()
        finally:
            con.close()
    if event_id is None:
        log.error("no final proposal for %s (only REJECTED/WITHDRAWN/FAILED/... can be "
                  "re-proposed)", commitment_id)
        return EXIT_FAILURE
    log.info("re-proposed %s; the next run inserts it if it is still eligible", commitment_id)
    return EXIT_OK


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Hermes calendar tentative registration")
    parser.add_argument("--config", default=None)
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="command")
    run_p = sub.add_parser("run")
    run_p.add_argument("--dry-run", action="store_true")
    sub.add_parser("plan")
    sub.add_parser("status")
    sub.add_parser("check")
    rep = sub.add_parser("repropose")
    rep.add_argument("commitment_id")
    args = parser.parse_args(argv)

    logging.basicConfig(level=getattr(logging, str(args.log_level).upper(), logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
                        stream=sys.stderr)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    now = datetime.now(timezone.utc)
    command = args.command or "run"
    if command == "plan":
        return cmd_plan(cfg, now, with_titles=True)
    if command == "status":
        return cmd_status(cfg)
    if command == "check":
        return cmd_check(cfg)
    if command == "repropose":
        return cmd_repropose(cfg, args.commitment_id)
    return cmd_run(cfg, now, force_dry_run=getattr(args, "dry_run", False))


if __name__ == "__main__":
    sys.exit(main())
