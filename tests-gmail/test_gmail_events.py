#!/usr/bin/env python3
from __future__ import annotations

import base64
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "gmail" / "app"))
sys.path.insert(0, str(ROOT / "src" / "gmail" / "events"))
sys.path.insert(0, str(ROOT / "src" / "calendar" / "app"))

import cal_normalize
import cal_reconcile
import cal_store
import gmail_event_extract as extractor
import gm_index
import gm_normalize
import gm_store
from cal_schema import open_db as open_calendar_db
from gm_schema import open_db as open_gmail_db


EVENTS_SCHEMA = """
CREATE TABLE events (
  event_id TEXT PRIMARY KEY, type TEXT NOT NULL, subtype TEXT,
  summary TEXT NOT NULL, actor TEXT NOT NULL, counterparty TEXT,
  status TEXT NOT NULL,
  actionability REAL, importance REAL, urgency REAL, confidence REAL,
  occurred_at TEXT, due_at TEXT, due_precision TEXT,
  created_at TEXT, updated_at TEXT, resolved_at TEXT,
  project_id TEXT, source_type TEXT, source_id TEXT, source_timestamp TEXT,
  source_conversation TEXT, source_speaker TEXT, evidence_count INTEGER,
  supersedes TEXT, superseded_by TEXT, classification_reason TEXT,
  needs_human_review INTEGER DEFAULT 0, attention REAL,
  last_surfaced_at TEXT, surface_count INTEGER DEFAULT 0,
  snoozed_until TEXT, classifier_version TEXT
);
CREATE TABLE processed_messages (
  source_id TEXT PRIMARY KEY, sent_at INTEGER, processed_at TEXT, result TEXT, content_hash TEXT
);
CREATE TABLE event_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, changed_at TEXT, field TEXT,
  old_value TEXT, new_value TEXT, actor TEXT
);
"""


def gmail_payload(message_id, *, subject="資料提出", text="明日までに提出お願いします", thread="t1"):
    return {
        "id": message_id, "threadId": thread, "historyId": "10",
        "internalDate": "1788595200000", "labelIds": ["INBOX"], "snippet": text,
        "payload": {
            "mimeType": "text/plain",
            "headers": [{"name": "Subject", "value": subject}, {"name": "From", "value": "sender@example.invalid"},
                        {"name": "Content-Type", "value": "text/plain; charset=UTF-8"}],
            "body": {"data": base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")},
        },
    }


class GmailEventTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.gmail_db = base / "gmail.db"
        self.index = base / "gmail" / "index.db"
        self.events_db = base / "events.db"
        self.reconcile = base / "gmail-event" / "calendar-reconcile.db"
        gmail = open_gmail_db(self.gmail_db)
        self.addCleanup(gmail.close)
        events = sqlite3.connect(self.events_db)
        events.executescript(EVENTS_SCHEMA)
        events.commit()
        events.close()
        self.gmail = gmail

    def _publish(self, *payloads, baseline=False):
        with self.gmail:
            gm_store.ensure_state(self.gmail)
            gm_store.set_auth_state(self.gmail, "AUTHORIZED")
            for payload in payloads:
                row = gm_normalize.normalize_message(payload, ingested_at="2026-09-05T00:00:00+00:00")
                gm_store.upsert_message(self.gmail, row, baseline=baseline)
            gm_index.publish_index(self.gmail, self.index)

    def test_commitment_provenance_thread_cancellation_and_initial_baseline_suppression(self):
        self._publish(gmail_payload("m1"))
        result = extractor.process(str(self.index), str(self.events_db), str(self.reconcile))
        self.assertEqual(result["created"], 1)
        events = sqlite3.connect(self.events_db)
        events.row_factory = sqlite3.Row
        row = events.execute("SELECT * FROM events").fetchone()
        self.assertEqual(row["source_type"], "gmail")
        self.assertEqual(row["source_id"], "gmail:m1")
        self.assertEqual(row["source_conversation"], "gmail:t1")
        self.assertEqual(row["status"], "open")
        events.close()

        self._publish(gmail_payload("m2", text="この依頼はキャンセルです", thread="t1"))
        result = extractor.process(str(self.index), str(self.events_db), str(self.reconcile))
        self.assertEqual(result["cancelled"], 1)
        events = sqlite3.connect(self.events_db)
        self.assertEqual(events.execute("SELECT status FROM events WHERE source_id='gmail:m1'").fetchone()[0], "cancelled")
        events.close()
        self.assertTrue(self.reconcile.exists())

        # A historical baseline can be extracted for later review, but remains
        # a candidate and therefore cannot create an immediate Attention alert.
        self._publish(gmail_payload("historical", thread="historical"), baseline=True)
        extractor.process(str(self.index), str(self.events_db), str(self.reconcile))
        events = sqlite3.connect(self.events_db)
        self.assertEqual(
            events.execute("SELECT status FROM events WHERE source_id='gmail:historical'").fetchone()[0],
            "candidate",
        )
        events.close()

    def test_calendar_explicit_link_wins_and_ambiguous_is_not_auto_merged(self):
        self._publish(gmail_payload("m1", subject="顧客会議", text="9/12 13:00でお願いします"))
        extractor.process(str(self.index), str(self.events_db), str(self.reconcile))
        events = sqlite3.connect(self.events_db)
        event_id = events.execute("SELECT event_id FROM events WHERE source_id='gmail:m1'").fetchone()[0]
        events.close()

        calendar_path = Path(self.temp.name) / "calendar.db"
        calendar = open_calendar_db(calendar_path)
        self.addCleanup(calendar.close)
        cal_store.upsert_calendar(calendar, {"id": "cal", "summary": "Primary", "primary": True, "timeZone": "Asia/Tokyo"}, selected=True)
        event = cal_normalize.normalize_event(
            {
                "id": "confirmed", "summary": "顧客会議",
                "start": {"dateTime": "2026-09-12T05:00:00Z"},
                "end": {"dateTime": "2026-09-12T06:00:00Z"},
                "status": "confirmed", "created": "2026-09-01T00:00:00Z",
                "updated": "2026-09-05T00:00:00Z", "etag": "e",
            },
            calendar_id="cal", calendar_name="Primary", calendar_timezone="Asia/Tokyo",
            ingested_at="2026-09-05T00:00:00+00:00",
        )
        cal_store.apply_event(calendar, event, baseline=True)
        calendar.commit()

        events = sqlite3.connect(self.events_db)
        events.execute(
            "INSERT INTO event_calendar_links(event_id,calendar_source_id,link_type,confidence,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (event_id, event["source_id"], "explicit", 1.0, "2026-09-05T00:00:00+00:00", "2026-09-05T00:00:00+00:00"),
        )
        events.commit(); events.close()
        # Republish the no-secret projection after the explicit operator link.
        extractor.process(str(self.index), str(self.events_db), str(self.reconcile))
        self.assertEqual(cal_reconcile.reconcile_calendar(calendar, events_db=str(self.reconcile), calendar_id="cal"), 1)
        resolved = cal_reconcile.authoritative_schedule(calendar, event_id)
        self.assertEqual(resolved["start_at"], "2026-09-12T05:00:00+00:00")

        # A second similarly titled Event has no explicit mapping and a
        # conflicting guessed time, so it must remain separate.
        events = sqlite3.connect(self.events_db)
        events.execute(
            "INSERT INTO events(event_id,type,summary,actor,status,due_at,source_type,source_id,source_conversation) VALUES(?,?,?,?,?,?,?,?,?)",
            ("ambiguous", "request", "顧客会議", "counterparty", "open",
             "2026-09-12T04:00:00+00:00", "gmail", "gmail:other", "gmail:other"),
        )
        events.commit(); events.close()
        extractor.process(str(self.index), str(self.events_db), str(self.reconcile))
        self.assertEqual(cal_reconcile.reconcile_calendar(calendar, events_db=str(self.reconcile), calendar_id="cal"), 0)


if __name__ == "__main__":
    unittest.main()
