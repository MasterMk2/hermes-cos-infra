#!/usr/bin/env python3
"""D.1 unit tests — source DB -> incremental index pipeline.

Spec sections 55-58 (durable queue, idempotency, edit and delete indexing),
39/42 (deleted content is not searchable), 103-105 and 107.

The indexer is exercised as a subprocess, exactly the way the scheduled job
runs it, so the CLI contract is covered too.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import dk_fixtures as fx
import dk_store
import yaml
from dk_normalize import normalize_message
from dk_schema import connect, open_source_db, source_id_for

APP_DIR = Path(__file__).resolve().parent.parent / "app"
INDEXER = APP_DIR / "indexer.py"


def rec_for(payload, guild_id=fx.GUILD_A, guild_name="Guild A", **kw):
    cid = payload.get("channel_id")
    return normalize_message(
        payload,
        guild_id=guild_id,
        guild_name=guild_name,
        channel=fx.CHANNEL_CTX[cid],
        **kw,
    )


class PipelineCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "messages.db"
        self.index = self.root / "index.db"
        self.cfg_path = self.root / "config.yaml"

        data = fx.config_dict()
        data["paths"] = {
            "source_db": str(self.source),
            "index_db": str(self.index),
            "state_dir": str(self.root),
            "log_dir": str(self.root),
        }
        self.cfg_path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )

        self.con = open_source_db(self.source)
        dk_store.upsert_guild(self.con, fx.GUILD_A, "Guild A")
        dk_store.upsert_guild(self.con, fx.GUILD_B, "Guild B")
        for cid, ctx in fx.CHANNEL_CTX.items():
            gid = fx.GUILD_B if cid == fx.CHANNEL_B1 else fx.GUILD_A
            dk_store.upsert_channel(
                self.con,
                cid,
                gid,
                channel_name=ctx["channel_name"],
                channel_type=ctx["channel_type"],
                parent_channel_id=ctx["parent_channel_id"],
                is_thread=ctx["is_thread"],
                readable=True,
            )

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    # -- helpers -------------------------------------------------------

    def store(self, payload, guild_id=fx.GUILD_A, guild_name="Guild A", **kw):
        rec = rec_for(payload, guild_id=guild_id, guild_name=guild_name, **kw)
        dk_store.apply_message(self.con, rec)
        self.con.commit()
        return rec

    def run_indexer(self, *extra):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(APP_DIR)
        proc = subprocess.run(
            [
                sys.executable,
                str(INDEXER),
                "--config",
                str(self.cfg_path),
                "--source-db",
                str(self.source),
                "--index-db",
                str(self.index),
                *extra,
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        self.assertEqual(
            proc.returncode, 0, f"indexer failed: {proc.stdout}\n{proc.stderr}"
        )
        self.assertIn("DISCORD_INDEX", proc.stdout)
        return proc.stdout.strip()

    def idx(self):
        return connect(self.index, readonly=True)

    def fts_hits(self, term):
        phrase = '"' + term.replace('"', '""') + '"'
        with self.idx() as con:
            return [
                r["source_id"]
                for r in con.execute("SELECT source_id FROM fts WHERE fts MATCH ?", (phrase,))
            ]

    def like_hits(self, term):
        with self.idx() as con:
            return [
                r["source_id"]
                for r in con.execute(
                    "SELECT source_id FROM messages "
                    "WHERE is_deleted = 0 AND content LIKE ?",
                    (f"%{term}%",),
                )
            ]

    def row(self, source_id):
        with self.idx() as con:
            return con.execute(
                "SELECT * FROM messages WHERE source_id = ?", (source_id,)
            ).fetchone()

    def counts(self):
        with self.idx() as con:
            return {
                "messages": con.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                "live": con.execute(
                    "SELECT COUNT(*) FROM messages WHERE is_deleted = 0"
                ).fetchone()[0],
                "fts": con.execute("SELECT COUNT(*) FROM fts").fetchone()[0],
            }

    def meta(self):
        with self.idx() as con:
            return dict(con.execute("SELECT key, value FROM meta").fetchall())


class BasicIndexing(PipelineCase):
    def test_messages_reach_the_index_with_full_provenance(self):
        rec = self.store(fx.NORMAL)
        self.run_indexer()
        row = self.row(rec["source_id"])
        self.assertIsNotNone(row)
        for field in (
            "source_id",
            "message_id",
            "guild_id",
            "guild_name",
            "channel_id",
            "channel_name",
            "author_id",
            "author_name",
            "created_at",
            "created_ts",
        ):
            self.assertIsNotNone(row[field], f"missing provenance field {field}")
        self.assertEqual(row["is_deleted"], 0)
        self.assertEqual(row["content"], fx.NORMAL["content"])

    def test_queue_is_drained_and_replayable(self):
        self.store(fx.NORMAL)
        self.assertEqual(dk_store.queue_depth(self.con), 1)
        self.run_indexer()
        self.assertEqual(dk_store.queue_depth(self.con), 0)

        # Simulate a crash between the index commit and the queue delete:
        # the entry is replayed and must not duplicate anything.
        before = self.counts()
        dk_store.enqueue_index(
            self.con, source_id_for(fx.GUILD_A, fx.CHANNEL_A1, fx.NORMAL["id"]), "upsert"
        )
        self.con.commit()
        self.run_indexer()
        self.assertEqual(self.counts(), before)

    def test_running_twice_changes_nothing(self):
        for payload in (fx.NORMAL, fx.REPLY, fx.BOT_MESSAGE):
            self.store(payload)
        self.store(fx.THREAD_MESSAGE)
        self.run_indexer()
        first = self.counts()
        self.run_indexer()
        self.assertEqual(self.counts(), first)
        self.assertEqual(first["fts"], first["live"])

    def test_thread_message_keeps_its_own_channel_and_parent(self):
        rec = self.store(fx.THREAD_MESSAGE)
        self.run_indexer()
        row = self.row(rec["source_id"])
        self.assertEqual(row["channel_id"], fx.THREAD_A)
        self.assertEqual(row["thread_id"], fx.THREAD_A)
        self.assertEqual(row["parent_channel_id"], fx.CHANNEL_A2)
        self.assertEqual(row["thread_name"], "release-thread")

    def test_two_guilds_share_one_index_without_mixing_provenance(self):
        a = self.store(fx.message(content="A側の話題です"))
        b = self.store(
            fx.message(channel_id=fx.CHANNEL_B1, content="B側の話題です"),
            guild_id=fx.GUILD_B,
            guild_name="Guild B",
        )
        self.run_indexer()
        self.assertEqual(self.row(a["source_id"])["guild_id"], fx.GUILD_A)
        self.assertEqual(self.row(a["source_id"])["guild_name"], "Guild A")
        self.assertEqual(self.row(b["source_id"])["guild_id"], fx.GUILD_B)
        self.assertEqual(self.row(b["source_id"])["guild_name"], "Guild B")
        with self.idx() as con:
            per_guild = dict(
                con.execute(
                    "SELECT guild_id, COUNT(*) FROM messages GROUP BY guild_id"
                ).fetchall()
            )
        self.assertEqual(per_guild, {fx.GUILD_A: 1, fx.GUILD_B: 1})


class EditIndexing(PipelineCase):
    def test_old_text_stops_being_searchable_after_an_edit(self):
        payload = fx.message(content="旧テキストABC")
        rec = self.store(payload)
        self.run_indexer()
        self.assertEqual(self.fts_hits("旧テキストABC"), [rec["source_id"]])

        edited = fx.message(
            message_id=payload["id"], content="新テキストXYZ", edited=fx.at(2)
        )
        self.store(edited)
        self.run_indexer()

        self.assertEqual(self.fts_hits("旧テキストABC"), [])
        self.assertEqual(self.like_hits("旧テキストABC"), [])
        self.assertEqual(self.fts_hits("新テキストXYZ"), [rec["source_id"]])
        self.assertEqual(self.counts()["fts"], 1)

    def test_edit_keeps_exactly_one_fts_row(self):
        payload = fx.message(content="one")
        self.store(payload)
        self.run_indexer()
        for i, body in enumerate(("two", "three", "four"), start=2):
            self.store(
                fx.message(message_id=payload["id"], content=body, edited=fx.at(i))
            )
            self.run_indexer()
        self.assertEqual(self.counts()["fts"], 1)
        self.assertEqual(self.counts()["messages"], 1)

    def test_embed_text_and_mentions_are_searchable(self):
        rec = self.store(fx.BOT_MESSAGE)
        self.store(
            fx.MENTION_MESSAGE, channel_names={fx.CHANNEL_A2: "projects"}
        )
        self.run_indexer()
        # Bounded embed text reaches the index, so a bot notice stays findable.
        self.assertIn(rec["source_id"], self.fts_hits("会議室"))
        # The raw body is what is displayed, not the normalized rendering.
        self.assertEqual(self.row(rec["source_id"])["content"], fx.BOT_MESSAGE["content"])


class DeleteIndexing(PipelineCase):
    def test_deleted_content_leaves_the_index_but_the_tombstone_stays(self):
        payload = fx.message(content="消えるべき本文DELETEME")
        rec = self.store(payload)
        self.run_indexer()
        self.assertEqual(self.fts_hits("消えるべき本文DELETEME"), [rec["source_id"]])

        dk_store.apply_delete(self.con, fx.GUILD_A, fx.CHANNEL_A1, payload["id"])
        self.con.commit()
        self.run_indexer()

        self.assertEqual(self.fts_hits("消えるべき本文DELETEME"), [])
        self.assertEqual(self.like_hits("消えるべき本文DELETEME"), [])
        row = self.row(rec["source_id"])
        self.assertEqual(row["is_deleted"], 1)
        self.assertIsNone(row["content"])
        self.assertIsNotNone(row["deleted_at"])
        # Provenance survives so a link back to Discord still resolves.
        self.assertEqual(row["message_id"], payload["id"])
        self.assertEqual(row["channel_id"], fx.CHANNEL_A1)
        self.assertIsNotNone(row["created_at"])
        self.assertEqual(self.counts()["fts"], 0)

    def test_bulk_delete_removes_every_body_from_the_index(self):
        payloads = [fx.message(content=f"BULKBODY{i}") for i in range(5)]
        for p in payloads:
            self.store(p)
        self.run_indexer()
        self.assertEqual(self.counts()["fts"], 5)

        dk_store.apply_bulk_delete(
            self.con, fx.GUILD_A, fx.CHANNEL_A1, [p["id"] for p in payloads]
        )
        self.con.commit()
        self.run_indexer()

        self.assertEqual(self.counts()["fts"], 0)
        self.assertEqual(self.counts()["messages"], 5)
        for p in payloads:
            self.assertEqual(self.fts_hits(f"BULKBODY{p['content'][-1]}"), [])
            row = self.row(source_id_for(fx.GUILD_A, fx.CHANNEL_A1, p["id"]))
            self.assertEqual(row["is_deleted"], 1)
            self.assertIsNone(row["content"])

    def test_thread_columns_survive_on_a_tombstone(self):
        """A deleted thread message must still resolve back to its thread and
        parent channel, or the provenance link is broken exactly where a
        reader most needs it."""
        rec = self.store(fx.THREAD_MESSAGE)
        self.run_indexer()
        dk_store.apply_delete(
            self.con, fx.GUILD_A, fx.THREAD_A, fx.THREAD_MESSAGE["id"]
        )
        self.con.commit()
        self.run_indexer()
        row = self.row(rec["source_id"])
        self.assertEqual(row["is_deleted"], 1)
        self.assertIsNone(row["content"])
        self.assertEqual(row["thread_id"], fx.THREAD_A)
        self.assertEqual(row["thread_name"], "release-thread")
        self.assertEqual(row["parent_channel_id"], fx.CHANNEL_A2)
        self.assertEqual(row["channel_type"], "public_thread")

    def test_delete_of_a_message_never_indexed_is_harmless(self):
        unseen = fx.message(content="never indexed")
        dk_store.apply_delete(self.con, fx.GUILD_A, fx.CHANNEL_A1, unseen["id"])
        self.con.commit()
        self.run_indexer()
        row = self.row(source_id_for(fx.GUILD_A, fx.CHANNEL_A1, unseen["id"]))
        if row is not None:
            self.assertEqual(row["is_deleted"], 1)
            self.assertIsNone(row["content"])
        self.assertEqual(self.counts()["fts"], 0)


class SearchBehaviour(PipelineCase):
    def setUp(self):
        super().setUp()
        self.jp = self.store(fx.message(content="来週の打ち合わせは水曜日にしましょう"))
        self.short = self.store(fx.message(content="OK です"))
        self.other = self.store(
            fx.message(author=fx.user(fx.OTHER_ID, "alice", "Alice"), content="了解です")
        )
        self.b = self.store(
            fx.message(channel_id=fx.CHANNEL_B1, content="Bサーバーの連絡事項"),
            guild_id=fx.GUILD_B,
            guild_name="Guild B",
        )
        self.run_indexer()

    def test_japanese_substring_search_works_through_fts(self):
        """Three or more characters is where the trigram tokenizer earns its
        keep: no word segmenter, and 打ち合わせ matches mid-sentence."""
        self.assertIn(self.jp["source_id"], self.fts_hits("打ち合わせ"))
        self.assertIn(self.jp["source_id"], self.fts_hits("来週の"))

    def test_short_queries_need_the_like_fallback(self):
        """FTS5 trigram cannot match fewer than three characters, which is
        why the MCP layer falls back to LIKE. Proven here, not assumed.

        This bites harder in Japanese than in English: 水曜 / 会議 / 予定 are
        ordinary two-character search terms that FTS silently returns nothing
        for. The fallback is not an edge case, it is the common path.
        """
        for term in ("O", "OK", "水", "水曜"):
            self.assertEqual(self.fts_hits(term), [], f"{term!r} should miss in FTS")
        for term, expected in (
            ("O", self.short),
            ("OK", self.short),
            ("水曜", self.jp),
        ):
            self.assertIn(expected["source_id"], self.like_hits(term))

    def test_filters_have_the_columns_they_need(self):
        with self.idx() as con:
            by_guild = con.execute(
                "SELECT COUNT(*) FROM messages WHERE guild_id = ? AND is_deleted = 0",
                (fx.GUILD_B,),
            ).fetchone()[0]
            by_channel = con.execute(
                "SELECT COUNT(*) FROM messages WHERE channel_id = ?", (fx.CHANNEL_B1,)
            ).fetchone()[0]
            by_author = con.execute(
                "SELECT COUNT(*) FROM messages WHERE author_id = ?", (fx.OTHER_ID,)
            ).fetchone()[0]
            by_date = con.execute(
                "SELECT COUNT(*) FROM messages WHERE created_ts >= ?", (0,)
            ).fetchone()[0]
        self.assertEqual(by_guild, 1)
        self.assertEqual(by_channel, 1)
        self.assertEqual(by_author, 1)
        self.assertEqual(by_date, 4)

    def test_every_indexed_row_carries_provenance(self):
        with self.idx() as con:
            rows = con.execute("SELECT * FROM messages").fetchall()
        self.assertEqual(len(rows), 4)
        for r in rows:
            self.assertTrue(r["source_id"].startswith("discord:"))
            self.assertTrue(r["guild_id"])
            self.assertTrue(r["channel_id"])
            self.assertTrue(r["author_id"])
            self.assertTrue(r["created_at"])


class StatusSnapshot(PipelineCase):
    def test_snapshot_is_published_and_content_free(self):
        self.store(fx.NORMAL)
        dk_store.set_ingest_states(
            self.con,
            {
                "collector_health": "CONNECTED",
                "gateway_connected": "1",
                "last_ready_at": "2026-09-02T00:00:00+00:00",
            },
        )
        self.con.commit()
        self.run_indexer()
        meta = self.meta()
        for key in (
            "last_indexed",
            "indexed_message_count",
            "source_message_count",
            "index_lag",
            "collector_health",
            "gateway_connected",
            "allowed_guilds_json",
            "allowed_channels_json",
            "per_guild_counts_json",
            "backfill_state_json",
        ):
            self.assertIn(key, meta, f"status key {key} missing")
        self.assertEqual(meta["collector_health"], "CONNECTED")
        self.assertEqual(meta["index_lag"], "0")
        blob = json.dumps(meta, ensure_ascii=False)
        self.assertNotIn(fx.NORMAL["content"], blob)

    def test_status_only_run_does_not_drain(self):
        self.store(fx.NORMAL)
        self.run_indexer("--status-only")
        self.assertEqual(dk_store.queue_depth(self.con), 1)
        self.assertEqual(self.meta().get("index_lag"), "1")


class UntrustedPayloads(PipelineCase):
    def test_instruction_shaped_bodies_are_stored_and_searched_as_data(self):
        stored = [self.store(p) for p in fx.injection_messages()]
        self.run_indexer()
        self.assertEqual(self.counts()["live"], len(stored))
        for rec in stored:
            row = self.row(rec["source_id"])
            self.assertEqual(row["is_deleted"], 0)
            self.assertEqual(row["content"], rec["content"])
        # A term from an injection body is retrievable exactly like any other
        # text: it is evidence, and nothing about it changed the pipeline.
        hits = self.fts_hits("developer mode")
        self.assertEqual(len(hits), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
