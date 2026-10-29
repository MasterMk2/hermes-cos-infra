"""Preview or apply owner semantics to automatic, single-source active rows.

No messages are sent. Output contains aggregate counts only. Existing manual
decisions, terminal states, calendar authority and merged evidence are retained.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from .reconcile import classify_event_semantics, generate_canonical_id


def reclassify(conn, *, apply=False):
    conn.row_factory = sqlite3.Row
    counts = Counter()
    for row in conn.execute('SELECT * FROM events').fetchall():
        cid = generate_canonical_id(row['source_type'] or 'event_layer', row['source_id'] or row['event_id'])
        cmt = conn.execute('SELECT * FROM commitments WHERE commitment_id=?', (cid,)).fetchone()
        if cmt is None:
            counts['missing'] += 1
            continue
        evidence_count = conn.execute('SELECT count(*) FROM commitment_evidence WHERE commitment_id=?', (cid,)).fetchone()[0]
        protected = (cmt['work_state'] not in {'ACTIONABLE', 'WAITING', 'BLOCKED'}
                     or cmt['currentness'] != 'current' or cmt['authority'] != 'evidence'
                     or cmt['source_count'] != 1 or evidence_count != 1)
        manual = conn.execute(
            "SELECT 1 FROM commitment_history WHERE commitment_id=? "
            "AND NOT (COALESCE(actor, '') IN ('backfill', 'system', 'admin_cli', 'owner_filter') "
            "AND COALESCE(reason, '') IN ('initial_backfill', 'owner_semantics_v2')) LIMIT 1", (cid,)
        ).fetchone()
        if protected or manual:
            counts['protected'] += 1
            continue
        responsibility, state, confidence = classify_event_semantics(row)
        if (cmt['responsibility'], cmt['work_state']) == (responsibility, state):
            counts['unchanged'] += 1
            continue
        counts['changed'] += 1
        counts[f"to_{responsibility}_{state}"] += 1
        if not apply:
            continue
        now = datetime.now(timezone.utc).isoformat()
        for field, value in [('responsibility', responsibility), ('work_state', state)]:
            if cmt[field] != value:
                conn.execute(
                    'INSERT INTO commitment_history '
                    '(commitment_id, changed_at, field, old_value, new_value, actor, reason) '
                    "VALUES (?, ?, ?, ?, ?, 'owner_filter', 'owner_semantics_v2')",
                    (cid, now, field, cmt[field], value),
                )
        conn.execute('UPDATE commitments SET responsibility=?, work_state=?, confidence=?, updated_at=? WHERE commitment_id=?',
                     (responsibility, state, confidence, now, cid))
    return dict(counts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    uri = Path(args.db).resolve().as_uri() + ('?mode=rw' if args.apply else '?mode=ro')
    with sqlite3.connect(uri, uri=True) as conn:
        conn.execute('PRAGMA foreign_keys=ON')
        if args.apply:
            conn.execute('BEGIN IMMEDIATE')
        result = reclassify(conn, apply=args.apply)
        if args.apply and (conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok'
                           or conn.execute('PRAGMA foreign_key_check').fetchone()):
            raise RuntimeError('Database integrity validation failed')
    conn.close()
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
