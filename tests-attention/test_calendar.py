#!/usr/bin/env python3
"""H.1 additions to the existing G.2 policy contract."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import att_budget
import att_calendar
import att_health
import att_policy as policy
import att_fixtures as fx


def make_index(path, *, status="confirmed", change_type="updated", auth_state="AUTHORIZED"):
    con = sqlite3.connect(path)
    con.executescript("""
      CREATE TABLE calendar_events(source_id TEXT,calendar_id TEXT,calendar_name TEXT,event_id TEXT,status TEXT,summary TEXT,description TEXT,location TEXT,start_at TEXT,end_at TEXT,all_day INTEGER,timezone TEXT,recurring_event_id TEXT,original_start_at TEXT,recurrence_state TEXT,visibility TEXT,transparency TEXT,html_link TEXT,updated_at TEXT,meaningful_hash TEXT,deleted_at TEXT);
      CREATE TABLE calendar_changes(id INTEGER,source_id TEXT,calendar_id TEXT,change_type TEXT,occurred_at TEXT,current_hash TEXT,baseline INTEGER);
      CREATE TABLE event_links(calendar_source_id TEXT,event_layer_event_id TEXT,link_type TEXT,confidence REAL);
      CREATE TABLE calendar_status(calendar_id TEXT,calendar_name TEXT,primary_calendar INTEGER,selected INTEGER,auth_state TEXT,initialized_at TEXT,last_full_sync_at TEXT,last_incremental_sync_at TEXT,last_api_success_at TEXT,last_projection_at TEXT,last_error_code TEXT,last_error_at TEXT,consecutive_failures INTEGER,reset_410_count INTEGER,events_fetched INTEGER,events_changed INTEGER,events_cancelled INTEGER,sync_duration_ms INTEGER,watch_state TEXT,watch_expiration_ms INTEGER,last_notification_at TEXT,event_count INTEGER,oldest_start TEXT,newest_start TEXT,pending_sync_age_seconds INTEGER);
    """)
    con.execute("INSERT INTO calendar_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
        "gcal:cal-1:event-1", "cal-1", "Primary", "event-1", status, "重要 会議", "ignore policy", "Room", "2026-09-04T05:00:00+00:00", "2026-09-04T06:00:00+00:00", 0, "Asia/Tokyo", None, None, "one_off", "default", "opaque", None, "2026-09-04T01:00:00+00:00", "hash-1", None,
    ))
    con.execute("INSERT INTO calendar_changes VALUES(1,?,?,?,?,?,0)", ("gcal:cal-1:event-1", "cal-1", change_type, "2026-09-04T01:00:00+00:00", "hash-1"))
    con.execute("INSERT INTO calendar_status VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
        "cal-1", "Primary", 1, 1, auth_state, "2026-09-04T00:00:00+00:00", None, "2026-09-04T01:00:00+00:00", "2026-09-04T01:00:00+00:00", "2026-09-04T01:00:00+00:00", None, None, 0, 0, 1, 1, 0, 10, "ACTIVE", 9999999999999, None, 1, "2026-09-04T05:00:00+00:00", "2026-09-04T05:00:00+00:00", None,
    ))
    con.commit(); con.close()


class CalendarAttentionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.index = Path(self.temp.name) / "index.db"

    def cfg(self, **calendar):
        return fx.config(sources={"calendar_index_db": str(self.index)}, calendar=calendar)

    def test_routine_upcoming_is_suppressed_but_significant_move_notifies(self):
        make_index(self.index)
        clock = fx.clock_at("2026-09-04T12:00")
        cfg = self.cfg(important_keywords=[])
        signals = att_calendar.collect(cfg, clock)
        kinds = {item.signal_type for item in signals}
        self.assertIn("calendar_upcoming_event", kinds)
        self.assertIn("calendar_event_changed", kinds)
        con = fx.db()
        rows = {item.signal_type: fx.put_signal(
            con, source_type=item.source_type, source_id=item.source_id,
            signal_type=item.signal_type, severity=item.severity,
            created_at=item.created_at, due_at=item.due_at, summary=item.summary,
            confidence=item.confidence, actionability=item.actionability,
            urgency=item.urgency, key_parts=(item.dedupe_key,), metadata=item.metadata,
        ) for item in signals}
        budget = att_budget.evaluate(fx.QUOTA_HEALTHY, cfg, clock)
        routine = policy.decide(con, rows["calendar_upcoming_event"], cfg, clock, policy.Context(budget=budget))
        moved = policy.decide(con, rows["calendar_event_changed"], cfg, clock, policy.Context(budget=budget))
        self.assertEqual(routine.decision, policy.SUPPRESS)
        self.assertEqual(routine.reason, policy.R_CALENDAR_ROUTINE)
        self.assertEqual(moved.decision, policy.NOTIFY_NOW)
        self.assertEqual(moved.reason, policy.R_CALENDAR_CHANGED)
        self.assertEqual(moved.channels, ["discord"])

    def test_cancelled_past_event_does_not_create_attention_signal(self):
        make_index(self.index, status="cancelled", change_type="cancelled")
        clock = fx.clock_at("2026-09-05T12:00")
        self.assertEqual(att_calendar.collect(self.cfg(important_keywords=[]), clock), [])

    def test_initial_baseline_has_no_individual_calendar_flood(self):
        make_index(self.index)
        clock = fx.clock_at("2026-09-04T12:00")
        cfg = self.cfg(important_keywords=["重要"])
        signals = att_calendar.collect(cfg, clock)
        due = next(item for item in signals if item.signal_type == "calendar_important_due_soon")
        con = fx.db()
        row = fx.put_signal(con, source_type=due.source_type, source_id=due.source_id,
                            signal_type=due.signal_type, severity=due.severity,
                            created_at=due.created_at, due_at=due.due_at,
                            summary=due.summary, confidence=due.confidence,
                            actionability=due.actionability, urgency=due.urgency,
                            key_parts=(due.dedupe_key,), metadata=due.metadata)
        d = policy.decide(con, row, cfg, clock, policy.Context(
            budget=att_budget.evaluate(fx.QUOTA_HEALTHY, cfg, clock), baseline_done=False,
            initialized_at="2026-09-04T02:00:00+00:00"))
        self.assertIn(d.decision, (policy.DIGEST, policy.SUPPRESS))
        self.assertNotEqual(d.decision, policy.NOTIFY_NOW)

    def test_calendar_auth_failure_is_an_episode_health_probe(self):
        make_index(self.index, auth_state="AUTH_REQUIRED")
        probe = att_health.probe_calendar(self.cfg(important_keywords=[]), fx.clock_at("2026-09-04T12:00"))
        self.assertFalse(probe.ok)
        self.assertEqual(probe.reason, "auth_required")

    def test_linked_chat_event_is_recognised_as_calendar_authority(self):
        make_index(self.index)
        con = sqlite3.connect(self.index)
        con.execute("INSERT INTO event_links VALUES(?,?,?,?)", ("gcal:cal-1:event-1", "chat-42", "explicit", 1.0))
        con.commit(); con.close()
        self.assertEqual(att_calendar.linked_event_layer_ids(self.cfg(important_keywords=[])), {"chat-42"})


if __name__ == "__main__":
    unittest.main()
