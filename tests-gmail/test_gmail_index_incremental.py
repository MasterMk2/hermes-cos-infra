#!/usr/bin/env python3
"""The two-copy index: in-place updates must match a fresh build and never touch the copy readers use."""
from __future__ import annotations

import base64
import hashlib
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "gmail" / "app"))

import gm_index
import gm_normalize
import gm_store
from gm_schema import open_db


JST = timezone(timedelta(hours=9))
NOON = datetime(2026, 10, 29, 12, 0, tzinfo=JST)
WORDS = ["見積書", "請求書", "打ち合わせ", "締め切り", "議事録", "納品物", "確認依頼", "日程調整"]


def payload(message_id: str, *, history_id: int, body: str, labels: list[str], thread_id: str) -> dict:
    return {
        "id": message_id, "threadId": thread_id, "historyId": str(history_id),
        "internalDate": str(1788508800000 + int(hashlib.sha256(message_id.encode()).hexdigest()[:6], 16)),
        "labelIds": labels, "snippet": body[:40],
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": f"件名 {body[:12]}"},
                {"name": "From", "value": "sender@example.invalid"},
                {"name": "To", "value": "owner@example.invalid"},
                {"name": "Content-Type", "value": "text/plain; charset=UTF-8"},
            ],
            "body": {"data": base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")},
        },
    }


def snapshot(path: Path) -> dict:
    """Everything a reader can observe, minus the publish time."""
    with tempfile.TemporaryDirectory() as scratch:
        # FTS5's integrity check needs a writable file; check a copy.
        copy = Path(scratch) / "copy.db"
        shutil.copyfile(path, copy)
        con = sqlite3.connect(copy)
        try:
            con.execute("INSERT INTO gmail_fts(gmail_fts) VALUES('integrity-check')")
            names = [row[1] for row in con.execute("PRAGMA table_info(gmail_status)")]
            status = dict(zip(names, con.execute("SELECT * FROM gmail_status").fetchone()))
            del status["published_at"], status["pending_sync_age_seconds"]
            return {
                "messages": {row[0]: row for row in con.execute("SELECT * FROM gmail_messages ORDER BY source_id")},
                "fts": sorted(row[0] for row in con.execute("SELECT source_id FROM gmail_fts")),
                # Incremental deletes rely on FTS rowids naming the same message.
                "misplaced_fts_rows": con.execute(
                    "SELECT COUNT(*) FROM gmail_fts f LEFT JOIN gmail_messages m ON m.rowid=f.rowid "
                    "WHERE m.source_id IS NOT f.source_id").fetchone()[0],
                "match": {word: sorted(row[0] for row in con.execute(
                    "SELECT source_id FROM gmail_fts WHERE gmail_fts MATCH ?", (f'"{word}"',)))
                    for word in WORDS},
                "status": status,
                "signature": con.execute("SELECT value FROM meta WHERE key='content_signature'").fetchone()[0],
            }
        finally:
            con.close()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@unittest.skipIf(os.name == "nt", "the two-copy index needs POSIX symlinks")
class IncrementalIndexTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.con = open_db(self.root / "gmail.db")
        self.addCleanup(self.con.close)
        with self.con:
            gm_store.ensure_state(self.con)
        self.index = self.root / "index" / "index.db"
        self.history = 100
        self.rng = random.Random(20261029)
        self.live: dict[str, dict] = {}

    def upsert(self, message_id: str, *, body: str | None = None, labels: list[str] | None = None):
        self.history += 1
        state = self.live.setdefault(message_id, {
            "body": " ".join(self.rng.sample(WORDS, 3)), "labels": ["INBOX"],
            "thread": f"thread-{self.rng.randrange(5)}",
        })
        state["body"] = body or state["body"]
        state["labels"] = labels or state["labels"]
        row = gm_normalize.normalize_message(
            payload(message_id, history_id=self.history, body=state["body"], labels=state["labels"],
                    thread_id=state["thread"]),
            ingested_at=gm_store.now_iso(),
        )
        with self.con:
            gm_store.upsert_message(self.con, row)

    def publish(self, now: datetime = NOON) -> bool:
        with self.con:
            return gm_index.publish_index(self.con, self.index, now=now)

    def fresh(self) -> dict:
        reference = Path(tempfile.mkdtemp(dir=self.temp.name)) / "index.db"
        with self.con:
            gm_index.publish_index(self.con, reference, now=NOON)
        return snapshot(reference)

    def assert_matches_fresh_build(self):
        self.assertEqual(snapshot(self.index), self.fresh())

    def seed(self, count: int = 6):
        for number in range(count):
            self.upsert(f"m{number}")
        self.publish()
        # The second copy does not exist yet, so the next change builds it.
        self.upsert("m0", body="議事録 請求書 日程調整")
        self.publish()

    def test_random_changes_match_a_fresh_build_after_every_publish(self):
        self.seed()
        modes = []
        for step in range(60):
            before = gm_index._content_signature(self.con)
            for _ in range(self.rng.randint(1, 3)):
                ids = sorted(self.live)
                action = self.rng.choice(["create", "body", "labels", "spam", "trash", "restore",
                                          "tombstone", "unavailable"])
                target = self.rng.choice(ids)
                if action == "create":
                    self.upsert(f"m{len(ids) + step * 10}")
                elif action == "body":
                    self.upsert(target, body=" ".join(self.rng.sample(WORDS, 3)))
                elif action == "labels":
                    self.upsert(target, labels=self.rng.choice([["INBOX"], ["INBOX", "UNREAD"], ["STARRED"]]))
                elif action in ("spam", "trash"):
                    self.upsert(target, labels=["SPAM"] if action == "spam" else ["TRASH"])
                elif action == "restore":
                    self.upsert(target, labels=["INBOX"])
                elif action == "tombstone":
                    with self.con:
                        gm_store.tombstone_message(self.con, target)
                else:
                    with self.con:
                        gm_store.upsert_message(self.con, gm_normalize.unavailable_message(
                            target, self.live[target]["thread"], ingested_at=gm_store.now_iso()))
            if gm_index._content_signature(self.con) == before:
                # e.g. a tombstone for a message that is already deleted
                self.assertFalse(self.publish())
                continue
            with self.assertLogs("gmail.index", level="INFO") as logs:
                self.assertTrue(self.publish())
            modes.append("incremental" if "mode=incremental" in logs.output[-1] else "full")
            self.assert_matches_fresh_build()
        self.assertGreater(len(modes), 40)
        self.assertEqual(set(modes), {"incremental"})

    def test_update_writes_only_the_copy_readers_are_not_using(self):
        self.seed()
        active = self.index.resolve()
        before = sha256(active)
        self.upsert("m1", body="納品物 締め切り 確認依頼")
        self.assertTrue(self.publish())
        self.assertNotEqual(self.index.resolve(), active)
        self.assertEqual(sha256(active), before)
        self.assertEqual(os.readlink(self.index), self.index.resolve().name)
        self.assertEqual(oct(self.index.resolve().stat().st_mode & 0o777), "0o440")
        self.assert_matches_fresh_build()

    def test_unchanged_content_only_rewrites_status(self):
        self.seed()
        link = os.readlink(self.index)
        self.assertFalse(self.publish())
        self.assertEqual(os.readlink(self.index), link)

    def test_interrupted_update_is_rolled_back_before_the_next_one(self):
        self.seed()
        inactive = gm_index._inactive_slot(self.index)
        os.chmod(inactive, 0o640)
        # Spill uncommitted pages into the file, then die without committing:
        # what a power cut in the middle of an update leaves behind.
        script = textwrap.dedent(f"""
            import os, sqlite3
            con = sqlite3.connect({str(inactive)!r}, isolation_level=None)
            con.execute("PRAGMA cache_size=10")
            con.execute("BEGIN IMMEDIATE")
            for number in range(300):
                con.execute("INSERT INTO meta(key,value) VALUES(?,?)", (f"junk{{number}}", "x" * 4000))
            con.execute("UPDATE gmail_messages SET body_text='broken'")
            os._exit(0)
        """)
        subprocess.run([sys.executable, "-c", script], check=True)
        journal = inactive.with_name(inactive.name + "-journal")
        self.assertGreater(journal.stat().st_size, 0)
        os.chmod(inactive, 0o440)
        self.upsert("m2", body="打ち合わせ 見積書 議事録")
        with self.assertLogs("gmail.index", level="INFO") as logs:
            self.assertTrue(self.publish())
        self.assertIn("mode=incremental", logs.output[-1])
        self.assertFalse(journal.exists())
        self.assert_matches_fresh_build()

    def test_full_build_removes_the_journal_of_the_copy_it_replaces(self):
        self.seed()
        inactive = gm_index._inactive_slot(self.index)
        journal = inactive.with_name(inactive.name + "-journal")
        journal.write_bytes(b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7" + bytes(504))
        self.upsert("m3", body="確認依頼 納品物 請求書")
        with mock.patch.object(gm_index, "_publish_incremental", return_value=None):
            self.assertTrue(self.publish())
        self.assertEqual(self.index.resolve(), inactive)
        self.assertFalse(journal.exists())
        self.assert_matches_fresh_build()

    def test_rewritten_change_log_forces_a_full_build(self):
        self.seed()
        with self.con:
            self.con.execute("DELETE FROM change_log WHERE id=(SELECT MIN(id) FROM change_log)")
        self.upsert("m3", body="確認依頼 納品物 請求書")
        with self.assertLogs("gmail.index", level="INFO") as logs:
            self.assertTrue(self.publish())
        self.assertIn("mode=full", logs.output[-1])
        self.assert_matches_fresh_build()

    def test_too_many_changes_rebuild_instead(self):
        self.seed()
        self.upsert("m1", body="議事録 議事録 議事録")
        self.upsert("m2", body="請求書 請求書 請求書")
        with mock.patch.object(gm_index, "MAX_INCREMENTAL_SOURCES", 1), \
                self.assertLogs("gmail.index", level="INFO") as logs:
            self.assertTrue(self.publish())
        self.assertIn("mode=full", logs.output[-1])
        self.assert_matches_fresh_build()

    def test_a_failed_update_falls_back_to_a_full_build(self):
        self.seed()
        self.upsert("m4", body="日程調整 締め切り 見積書")
        with mock.patch.object(gm_index, "_apply_changes", side_effect=sqlite3.OperationalError("locked")), \
                self.assertLogs("gmail.index", level="INFO") as logs:
            self.assertTrue(self.publish())
        self.assertIn("OperationalError", logs.output[0])
        self.assertIn("mode=full", logs.output[-1])
        self.assertEqual(oct(gm_index._inactive_slot(self.index).stat().st_mode & 0o777), "0o440")
        self.assert_matches_fresh_build()

    def test_regular_file_index_is_replaced_by_the_link(self):
        reference = Path(tempfile.mkdtemp(dir=self.temp.name)) / "index.db"
        self.upsert("m0")
        with self.con:
            gm_index.publish_index(self.con, reference, now=NOON)
        self.index.parent.mkdir()
        shutil.copyfile(reference.resolve(), self.index)
        self.upsert("m1")
        self.assertTrue(self.publish())
        self.assertTrue(self.index.is_symlink())
        self.assert_matches_fresh_build()

    def test_nightly_compaction_rebuilds_a_fragmented_copy_once(self):
        self.seed()
        self.upsert("m1", body="議事録 見積書 請求書")
        self.publish()
        self.upsert("m2", body="締め切り 確認依頼 納品物")
        self.publish()
        inactive = gm_index._inactive_slot(self.index)
        os.chmod(inactive, 0o640)
        con = sqlite3.connect(inactive)
        with con:
            con.execute("UPDATE meta SET value='2026-10-28T00:00:00+00:00' WHERE key='full_built_at'")
        con.close()
        os.chmod(inactive, 0o440)
        self.assertFalse(self.publish(now=NOON.replace(hour=5)))
        with self.assertLogs("gmail.index", level="INFO") as logs:
            self.assertTrue(self.publish(now=NOON.replace(hour=4, minute=10)))
        self.assertIn("mode=full", logs.output[-1])
        self.assertEqual(self.index.resolve(), inactive)
        self.assertEqual(gm_index._read_meta(inactive)["incremental_updates"], "0")
        self.assertFalse(self.publish(now=NOON.replace(hour=4, minute=25)))
        self.assert_matches_fresh_build()


if __name__ == "__main__":
    unittest.main()
