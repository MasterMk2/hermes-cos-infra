"""A run must read processed_messages once, however long the snapshot is.

Until 2026-10-29 the no-candidate bookkeeping re-read the whole table for
every snapshot message. The table is shared with Discord and Gmail, so on the
Pi this took about 4 minutes of a 10-minute budget and the run timed out
before it reached the commitments stage.
"""

import contextlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "line-knowledge"))

import event_extract  # noqa: E402

BASE_MS = 1_790_000_000_000


class _TracingSqlite:
    """The sqlite3 module, but every connection records its statements."""

    def __init__(self, statements):
        self.statements = statements

    def __getattr__(self, name):
        return getattr(sqlite3, name)

    def connect(self, *args, **kwargs):
        con = sqlite3.connect(*args, **kwargs)
        con.set_trace_callback(self.statements.append)
        return con


class NoCandidateBookkeepingTests(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        for name in ("SNAPSHOT", "INDEX_DB", "EVENTS_DIR", "LOG_DIR"):
            patcher = patch.object(event_extract, name, getattr(event_extract, name))
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_extract(self, message_count, done_count):
        """Run main() over messages that match no signal; return what it touched."""
        work = self.dir / f"run-{message_count}"
        work.mkdir()
        snapshot = work / "messages.jsonl"
        with snapshot.open("w", encoding="utf-8") as fh:
            for i in range(message_count):
                fh.write(json.dumps({"message_id": f"line-{i}", "chat_id": "chat",
                                     "sent_at": BASE_MS + i * 1000,
                                     "body": f"ありがとう {i}"}, ensure_ascii=False) + "\n")
        db = work / "events.db"
        con = sqlite3.connect(db)
        event_extract.init_db(con)
        done = [(f"line-{i}", BASE_MS + i * 1000, "2026-10-01T00:00:00+00:00", "no_event")
                for i in range(done_count)]
        others = [(f"gmail-{i}", BASE_MS, "2026-10-01T00:00:00+00:00", "created") for i in range(50)]
        con.executemany("INSERT INTO processed_messages VALUES (?,?,?,?)", done + others)
        con.commit()
        con.close()

        statements = []
        argv = ["event_extract.py", "--source", str(snapshot), "--db", str(db),
                "--index", str(work / "missing-index.db"), "--state", str(work / "state.json")]
        with patch.object(sys, "argv", argv), \
                patch.object(event_extract, "sqlite3", _TracingSqlite(statements)), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(event_extract.main(), 0)

        con = sqlite3.connect(db)
        rows = {r[0]: r[1:] for r in con.execute(
            "SELECT source_id, sent_at, processed_at, result FROM processed_messages")}
        con.close()
        state = json.loads((work / "state.json").read_text(encoding="utf-8"))
        reads = [s for s in statements
                 if s.lstrip().upper().startswith("SELECT") and "FROM processed_messages" in s]
        return rows, state, done + others, reads

    def test_new_messages_are_marked_and_old_rows_kept(self):
        rows, state, seeded, _ = self.run_extract(message_count=12, done_count=4)

        self.assertEqual(state["candidates"], 0)
        self.assertEqual(state["llm_calls"], 0)
        for source_id, sent_at, processed_at, result in seeded:
            self.assertEqual(rows[source_id], (sent_at, processed_at, result))
        for i in range(4, 12):
            sent_at, _, result = rows[f"line-{i}"]
            self.assertEqual((sent_at, result), (BASE_MS + i * 1000, "no_candidate"))
        self.assertEqual(len(rows), len(seeded) + 8)

    def test_reads_of_processed_messages_do_not_grow_with_the_snapshot(self):
        *_, small = self.run_extract(message_count=10, done_count=2)
        *_, large = self.run_extract(message_count=300, done_count=2)

        self.assertEqual(len(large), len(small))


if __name__ == "__main__":
    unittest.main()
