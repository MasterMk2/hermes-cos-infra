#!/usr/bin/env python3
"""G.2 test fixtures.

Nothing here is a real identifier. The engine holds no credential and no
recipient, so there is nothing to invent: what the tests need is a
configuration, an empty database and a way to put a signal in front of the
policy.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import att_store as store
from att_config import SECTION_DEFAULTS, parse_config
from att_schema import init_db
from att_signals import make_signal
from att_time import Clock

TZ = "Asia/Tokyo"


def config(**sections):
    """A config built from the shipped defaults, with sections overridden."""
    data = {}
    for name, overrides in sections.items():
        if name not in SECTION_DEFAULTS:
            raise AssertionError(f"no such config section: {name}")
        data[name] = dict(overrides)
    return parse_config(data, path="<test>")


def db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    return init_db(con)


def clock_at(text: str) -> Clock:
    """A clock frozen at a local time in the policy timezone.

    Written as local time because every quiet-hours boundary in the spec is
    stated in local time, and converting them by hand in each test is how a
    nine-hour offset gets into an assertion (section 123).
    """
    naive = datetime.fromisoformat(text)
    from zoneinfo import ZoneInfo

    local = naive.replace(tzinfo=ZoneInfo(TZ))
    return Clock(tz_name=TZ, fixed=local.astimezone(timezone.utc))


def utc(dt_local: str) -> str:
    return clock_at(dt_local).now().isoformat()


def put_signal(con, *, source_type="event", source_id="evt_1",
               signal_type="due_soon", severity="IMPORTANT",
               created_at=None, due_at=None, summary="テスト項目",
               confidence=0.9, actionability=0.8, urgency=0.6,
               key_parts=None, metadata=None, clock=None):
    """Insert one signal and return its row, as the policy will see it."""
    clock = clock or Clock(tz_name=TZ)
    sig = make_signal(
        source_type=source_type, source_id=source_id, signal_type=signal_type,
        created_at=created_at or clock.now_iso(),
        subject=summary, summary=summary,
        key_parts=key_parts or (source_type, source_id, signal_type),
        severity=severity, confidence=confidence,
        actionability=actionability, urgency=urgency, due_at=due_at,
        metadata=metadata or {},
    )
    row, _ = store.upsert_signal(con, sig)
    con.commit()
    return row


def mark_delivered(con, dedupe_key, *, severity="IMPORTANT", body="x",
                   when=None):
    """Pretend a notification for this key already reached the owner."""
    row, _ = store.create_notification(
        con, dedupe_key=dedupe_key, severity=severity, subject="s", body=body,
        reason="test",
    )
    store.mark_notification(con, row["notification_id"], "DELIVERED",
                            channels=["discord"])
    if when is not None:
        con.execute(
            "UPDATE notifications SET created_at = ?, delivered_at = ?"
            " WHERE notification_id = ?",
            (when, when, row["notification_id"]),
        )
    con.commit()
    return store.get_notification(con, row["notification_id"])


def ago(clock, **kw) -> str:
    return (clock.now() - timedelta(**kw)).isoformat()


def ahead(clock, **kw) -> str:
    return (clock.now() + timedelta(**kw)).isoformat()


class FakeGateway:
    """Records notify() calls; answers from a script.

    The engine's only route to a person, so a fake here is the boundary of
    what the unit tests cover: everything below it is exercised for real in
    the gateway's own socket tests and in the production end-to-end run.
    """

    def __init__(self, *, budget=None, liveness=None, answers=None):
        self.calls = []
        self.budget_payload = budget
        self.liveness_payload = liveness
        self.answers = list(answers or [])
        self.default = None

    def notify(self, **kw):
        from att_gateway import DELIVERED, Result

        self.calls.append(kw)
        if self.answers:
            return self.answers.pop(0)
        return self.default or Result(DELIVERED, channel=kw["channel"],
                                      quota_consumed=(kw["channel"] == "line"))

    def line_budget(self, *, force=False):
        return self.budget_payload

    def liveness(self):
        return self.liveness_payload

    def health(self):
        return self.liveness_payload is not None


QUOTA_HEALTHY = {"ok": True, "quota_type": "limited", "limit": 200,
                 "used": 10, "remaining": 190, "unlimited": False,
                 "fetched_at": "2026-09-04T00:00:00+00:00", "stale": False}

QUOTA_CONSERVE = {"ok": True, "quota_type": "limited", "limit": 200,
                  "used": 140, "remaining": 60, "unlimited": False,
                  "stale": False}

QUOTA_CRITICAL = {"ok": True, "quota_type": "limited", "limit": 200,
                  "used": 185, "remaining": 15, "unlimited": False,
                  "stale": False}

QUOTA_EXHAUSTED = {"ok": True, "quota_type": "limited", "limit": 200,
                   "used": 200, "remaining": 0, "unlimited": False,
                   "stale": False}

QUOTA_UNLIMITED = {"ok": True, "quota_type": "none", "limit": None,
                   "used": 4321, "remaining": None, "unlimited": True,
                   "stale": False}

QUOTA_UNKNOWN = {"ok": False, "error_category": "auth", "quota_type": None,
                 "limit": None, "used": None, "remaining": None,
                 "unlimited": False, "stale": True}


HEALTHY_LIVENESS = {
    "now": "2026-09-04T00:00:00+00:00",
    "last_inbound_received_at": None,
    "last_inbound_processed_at": None,
    "last_hermes_success_at": None,
    "last_hermes_failure_at": None,
    "last_outbound_success_at": None,
    "last_proactive_success_at": None,
    "oldest_queued_at": None,
    "oldest_processing_at": None,
    "queue_depth": 0, "processing_depth": 0, "outbound_pending": 0,
    "proactive_pending": 0, "processing_failure_count": 0,
    "hermes_failure_count": 0, "delivery_failure_count": 0,
    "responded_count": 0, "discord_connected": True,
    "line_enabled": True, "discord_enabled": True,
}
