import sqlite3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from commitments.models import Commitment, CommitmentEvidence
from commitments.store import init_db, save_commitment
from commitments.reconcile import generate_canonical_id
from commitments.owner_filter import reclassify


def test_preview_apply_idempotency_and_manual_terminal_merge_protection():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    init_db(conn)
    conn.execute('CREATE TABLE events (event_id TEXT, source_type TEXT, source_id TEXT, '
                 'summary TEXT, actor TEXT, type TEXT, status TEXT, counterparty TEXT, needs_human_review INTEGER)')
    for name in ('automatic', 'manual', 'manual_null_reason', 'done', 'merged', 'superseded'):
        conn.execute('INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                     (name, 'discord', name, '資料を提出する', 'counterparty', 'commitment', 'open', '', 0))
        cmt = Commitment(
            commitment_id=generate_canonical_id('discord', name), title='資料を提出する',
            responsibility='SELF', work_state='DONE' if name == 'done' else 'ACTIONABLE',
            currentness='superseded' if name == 'superseded' else 'current',
            source_count=2 if name == 'merged' else 1,
            evidence=[CommitmentEvidence(source_type='discord', source_id=name, observed_at='2026-09-09T00:00:00Z')],
        )
        if name == 'merged':
            cmt.evidence.append(CommitmentEvidence(source_type='line', source_id='second', observed_at='2026-09-09T00:00:00Z'))
        save_commitment(conn, cmt, actor='user' if name.startswith('manual') else 'backfill',
                        reason=None if name == 'manual_null_reason' else 'initial_backfill')
    before = conn.total_changes
    preview = reclassify(conn)
    assert preview['changed'] == 1
    assert preview['protected'] == 5
    assert conn.total_changes == before
    with conn:
        assert reclassify(conn, apply=True) == preview
    assert reclassify(conn).get('changed', 0) == 0
    assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    assert conn.execute('SELECT count(*) FROM commitment_evidence').fetchone()[0] == 7
    conn.close()
