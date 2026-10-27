#!/usr/bin/env python3
"""Verification of Attention Engine integration with Unified Commitments."""

import os
import sqlite3
import tempfile
import unittest

import att_events
import att_fixtures as fx
from att_signals import SIG_DUE_SOON, SIG_OVERDUE, SIG_NEW_COMMITMENT


class TestCommitmentAttentionIntegration(unittest.TestCase):
    def test_old_deferred_noise_cannot_return_in_digest(self):
        import att_store
        con = fx.db()
        try:
            for source_id, actor in [('owner', 'user'), ('other', 'counterparty')]:
                row = fx.put_signal(con, source_id=source_id,
                                    metadata={'actor': actor, 'event_type': 'commitment'})
                att_store.record_decision(con, signal_id=row['signal_id'], decision='DEFER',
                                          reason='quiet_hours', severity='IMPORTANT',
                                          deliver_after=fx.ago(self.clock, minutes=1))
            con.commit()
            self.assertEqual(att_events.retire_non_owner_backlog(con), 1)
            self.assertEqual(att_events.retire_non_owner_backlog(con), 0)
            items = att_store.signals_awaiting_digest(con)
            self.assertEqual([r['source_id'] for r in items], ['owner'])
            self.assertEqual(con.execute('SELECT count(*) FROM signals').fetchone()[0], 2)
        finally:
            con.close()

    def test_other_peoples_tasks_and_unknown_assignees_never_notify(self):
        from test_policy import _event_row
        for actor, kind, review in [('counterparty', 'commitment', 0),
                                    ('counterparty', 'request', 0),
                                    ('unknown', 'deadline', 0),
                                    ('user', 'request', 0),
                                    ('user', 'commitment', 1)]:
            with self.subTest(actor=actor, kind=kind, review=review):
                row = _event_row(actor=actor, type=kind, needs_human_review=review,
                                 due_at=fx.ahead(self.clock, minutes=25),
                                 occurred_at=fx.ago(self.clock, minutes=10),
                                 snoozed_until=fx.ago(self.clock, minutes=1))
                self.assertEqual(att_events.signals_for_event(row, self.cfg, self.clock), [])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "events.db")
        self.clock = fx.clock_at("2026-09-06T12:00:00+09:00")

        con = sqlite3.connect(self.db_path)
        con.executescript("""
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
            snoozed_until TEXT, classifier_version INTEGER
        );
        CREATE TABLE commitments (
            commitment_id TEXT PRIMARY KEY,
            canonical_summary TEXT NOT NULL,
            responsibility TEXT NOT NULL,
            work_state TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE commitment_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            commitment_id TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_id TEXT NOT NULL
        );
        """)
        con.commit()
        con.close()

        self.cfg = fx.config(
            sources={"events_db": self.db_path, "calendar_index_db": ""},
            events={
                "statuses_open": ["open"],
                "statuses_ignored": ["resolved", "cancelled"],
                "overdue_grace_minutes": 15,
                "overdue_max_age_days": 7,
                "max_source_age_days": 3,
                "min_confidence_digest": 0.5,
                "min_confidence_notify": 0.7,
                "min_actionability_notify": 0.7,
                "due_windows_hours": [1, 3, 24, 72],
            }
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_dismissed_noise_suppressed(self):
        """Events linked to DISMISSED commitments must generate zero signals."""
        con = sqlite3.connect(self.db_path)
        con.execute("""
            INSERT INTO events (event_id, type, summary, actor, status, actionability, confidence, occurred_at, created_at)
            VALUES ('evt_noise_1', 'request', 'Newsletter discount', 'counterparty', 'open', 0.9, 0.9,
                    '2026-09-06T11:00:00+09:00', '2026-09-06T11:00:00+09:00')
        """)
        con.execute("""
            INSERT INTO commitments (commitment_id, canonical_summary, responsibility, work_state, created_at)
            VALUES ('cmt_noise_1', 'Newsletter discount', 'INFORMATIONAL', 'DISMISSED', '2026-09-06T11:00:00+09:00')
        """)
        con.execute("""
            INSERT INTO commitment_evidence (commitment_id, source_type, source_id)
            VALUES ('cmt_noise_1', 'discord', 'evt_noise_1')
        """)
        con.commit()
        con.close()

        signals = att_events.collect(self.cfg, self.clock)
        self.assertEqual(len(signals), 0, "DISMISSED commitment event was not suppressed!")

    def test_new_self_actionable_signal_generated(self):
        """A new SELF + ACTIONABLE event within the recent window generates a signal."""
        con = sqlite3.connect(self.db_path)
        con.execute("""
            INSERT INTO events (event_id, type, summary, actor, status, actionability, urgency, confidence,
                                occurred_at, created_at, due_at)
            VALUES ('evt_self_1', 'commitment', '今日中に資料を送付する', 'user', 'open', 0.9, 0.8, 0.9,
                    '2026-09-06T11:30:00+09:00', '2026-09-06T11:30:00+09:00', '2026-09-06T13:00:00+09:00')
        """)
        con.execute("""
            INSERT INTO commitments (commitment_id, canonical_summary, responsibility, work_state, created_at)
            VALUES ('cmt_self_1', '今日中に資料を送付する', 'SELF', 'ACTIONABLE', '2026-09-06T11:30:00+09:00')
        """)
        con.execute("""
            INSERT INTO commitment_evidence (commitment_id, source_type, source_id)
            VALUES ('cmt_self_1', 'discord', 'evt_self_1')
        """)
        con.commit()
        con.close()

        signals = att_events.collect(self.cfg, self.clock)
        self.assertGreater(len(signals), 0, "SELF commitment should produce attention signal")
        signal_types = [s.signal_type for s in signals]
        # Clock is 12:00, due is 13:00 (1 hour remaining -> due_soon 1h)
        self.assertIn(SIG_DUE_SOON, signal_types)

    def test_overdue_transition_validated(self):
        """When due_at has passed by grace period, an OVERDUE signal is generated."""
        con = sqlite3.connect(self.db_path)
        con.execute("""
            INSERT INTO events (event_id, type, summary, actor, status, actionability, urgency, confidence,
                                occurred_at, created_at, due_at)
            VALUES ('evt_overdue_1', 'commitment', '昨日の提出物', 'user', 'open', 0.9, 0.8, 0.9,
                    '2026-09-05T10:00:00+09:00', '2026-09-05T10:00:00+09:00', '2026-09-05T18:00:00+09:00')
        """)
        con.commit()
        con.close()

        signals = att_events.collect(self.cfg, self.clock)
        signal_types = [s.signal_type for s in signals]
        self.assertIn(SIG_OVERDUE, signal_types)


if __name__ == '__main__':
    unittest.main()
