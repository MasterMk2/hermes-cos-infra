import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from commitments.reconcile import classify_event_semantics


@pytest.mark.parametrize('source', ['line', 'discord', 'gmail'])
@pytest.mark.parametrize('actor,kind,expected', [
    ('user', 'commitment', ('SELF', 'ACTIONABLE')),
    ('user', 'request', ('COUNTERPARTY', 'WAITING')),
    ('counterparty', 'commitment', ('COUNTERPARTY', 'WAITING')),
    ('counterparty', 'request', ('SHARED', 'BLOCKED')),
    ('unknown', 'commitment', ('SHARED', 'BLOCKED')),
    ('counterparty', 'deadline', ('SHARED', 'BLOCKED')),
])
def test_speaker_is_not_automatically_assignee(source, actor, kind, expected):
    row = dict(source_type=source, actor=actor, type=kind, summary='資料を作成・提出する依頼',
               status='open', counterparty='', needs_human_review=0)
    assert classify_event_semantics(row)[:2] == expected


def test_owner_uncertainty_is_not_actionable():
    row = dict(source_type='discord', actor='user', type='commitment', summary='提出します',
               status='open', counterparty='', needs_human_review=1)
    assert classify_event_semantics(row)[:2] == ('SHARED', 'BLOCKED')
