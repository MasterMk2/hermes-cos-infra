#!/usr/bin/env python3
"""D.1 MCP tests — run INSIDE the Hermes container against the real `mcp`.

This is the read-only retrieval surface the assistant actually sees, so it is
exercised with the container's own interpreter and the container's own view
of the index path rather than with a stub.

Spec sections 59-67 (tool surface, provenance, read-only), 106 (search
behaviour including Japanese and short queries), 107 (provenance on every
row), 137 (acceptance criteria for the MCP layer).

    DISCORD_KNOWLEDGE_INDEX=/opt/data/tools/discord-knowledge/test-fixtures/index.db \
    /opt/hermes/.venv/bin/python3 -m unittest test_mcp_container -v
"""

import asyncio
import json
import os
import sqlite3
import unittest
from pathlib import Path

INDEX = Path(os.environ["DISCORD_KNOWLEDGE_INDEX"])
CATALOGUE = json.loads(
    (INDEX.parent / "fixture-catalogue.json").read_text(encoding="utf-8")
)

import discord_mcp as srv  # noqa: E402  (import after the env var is set)


def call(name, **args):
    """Drive the tool exactly the way the MCP dispatcher does."""

    class _Params:
        pass

    p = _Params()
    p.name = name
    p.arguments = args
    result = asyncio.run(srv._call_tool(None, p))
    return json.loads(result.content[0].text)


class ToolSurface(unittest.TestCase):
    def test_exactly_three_read_only_tools(self):
        listing = asyncio.run(srv._list_tools(None, None))
        names = sorted(t.name for t in listing.tools)
        self.assertEqual(names, ["discord_get", "discord_search", "discord_status"])

    def test_no_tool_can_act_on_discord(self):
        listing = asyncio.run(srv._list_tools(None, None))
        forbidden = (
            "send",
            "post",
            "delete",
            "edit",
            "react",
            "reaction",
            "ban",
            "kick",
            "role",
            "manage",
            "webhook",
        )
        for tool in listing.tools:
            for word in forbidden:
                self.assertNotIn(
                    word, tool.name.lower(), f"{tool.name} looks like a write tool"
                )

    def test_every_tool_warns_that_content_is_untrusted(self):
        listing = asyncio.run(srv._list_tools(None, None))
        for tool in listing.tools:
            self.assertIn("untrusted", tool.description.lower())

    def test_the_index_is_opened_read_only(self):
        with self.assertRaises(sqlite3.OperationalError) as ctx:
            with srv._connect() as con:
                con.execute("DELETE FROM messages")
        self.assertIn("readonly", str(ctx.exception).lower())

    def test_no_discord_library_is_imported(self):
        import sys

        self.assertNotIn("discord", sys.modules)


class Search(unittest.TestCase):
    def test_japanese_phrase_uses_fts(self):
        out = call("discord_search", query="打ち合わせ")
        self.assertEqual(out["method"], "fts_trigram")
        self.assertGreaterEqual(out["total_returned"], 1)
        self.assertIn(
            CATALOGUE["plain"], [r["source_id"] for r in out["results"]]
        )

    def test_one_character_query_falls_back(self):
        out = call("discord_search", query="資")
        self.assertEqual(out["method"], "like_fallback")
        self.assertGreaterEqual(out["total_returned"], 1)

    def test_two_character_japanese_query_falls_back_and_still_finds(self):
        """水曜 is an ordinary search term and FTS5 trigram cannot match it."""
        out = call("discord_search", query="水曜")
        self.assertEqual(out["method"], "like_fallback")
        self.assertIn(CATALOGUE["plain"], [r["source_id"] for r in out["results"]])

    def test_guild_filter(self):
        out = call("discord_search", query="連絡", guild_id=CATALOGUE["guild_b"].split(":")[1])
        self.assertGreaterEqual(out["total_returned"], 1)
        for r in out["results"]:
            self.assertEqual(r["guild_id"], CATALOGUE["guild_b"].split(":")[1])

    def test_channel_filter(self):
        thread_channel = CATALOGUE["thread"].split(":")[2]
        out = call("discord_search", query="スレッド", channel_id=thread_channel)
        self.assertGreaterEqual(out["total_returned"], 1)
        for r in out["results"]:
            self.assertEqual(r["channel_id"], thread_channel)

    def test_author_filter(self):
        author_id = call("discord_get", source_id=CATALOGUE["other_author"])["target"][
            "author_id"
        ]
        out = call("discord_search", query="準備", author_id=author_id)
        self.assertGreaterEqual(out["total_returned"], 1)
        for r in out["results"]:
            self.assertEqual(r["author_id"], author_id)

    def test_date_filters(self):
        wide = call("discord_search", query="です", after="2026-01-01", before="2026-12-31")
        self.assertGreaterEqual(wide["total_returned"], 1)
        narrow = call("discord_search", query="です", after="2030-01-01")
        self.assertEqual(narrow["total_returned"], 0)

    def test_bad_id_filters_are_rejected_not_interpolated(self):
        for arg, err in (
            ("guild_id", "invalid_guild_id"),
            ("channel_id", "invalid_channel_id"),
            ("author_id", "invalid_author_id"),
        ):
            out = call("discord_search", query="です", **{arg: "1 OR 1=1"})
            self.assertEqual(out.get("error"), err)

    def test_limit_is_clamped(self):
        out = call("discord_search", query="です", limit=9999)
        self.assertLessEqual(out["total_returned"], 50)

    def test_deleted_content_is_never_returned(self):
        out = call("discord_search", query="GONEBODY")
        self.assertEqual(out["total_returned"], 0)
        for r in call("discord_search", query="です", limit=50)["results"]:
            self.assertFalse(r["is_deleted"])

    def test_edited_message_returns_only_the_current_body(self):
        old = call("discord_search", query="OLDBODY")
        new = call("discord_search", query="NEWBODY")
        self.assertEqual(old["total_returned"], 0)
        self.assertEqual(new["total_returned"], 1)

    def test_every_result_carries_provenance(self):
        out = call("discord_search", query="です", limit=50)
        self.assertGreaterEqual(out["total_returned"], 1)
        for r in out["results"]:
            for field in (
                "source_id",
                "guild_id",
                "guild_name",
                "channel_id",
                "channel_name",
                "author_id",
                "author_name",
                "timestamp",
                "message_id",
            ):
                self.assertIsNotNone(r.get(field), f"{field} missing from result")
            self.assertTrue(r["source_id"].startswith("discord:"))
            self.assertTrue(r["timestamp"].endswith("+00:00"))
            self.assertIn("timestamp_jst", r)

    def test_results_are_labelled_untrusted(self):
        out = call("discord_search", query="です")
        self.assertIn("untrusted", out["note"].lower())

    def test_empty_query_is_refused(self):
        self.assertEqual(call("discord_search", query="  ").get("error"), "query_required")


class Get(unittest.TestCase):
    """`discord_get` mirrors the LINE server's shape: the record is under
    `target`, with bounded `context_before` / `context_after` beside it."""

    def test_full_record_with_provenance(self):
        out = call("discord_get", source_id=CATALOGUE["plain"])
        target = out["target"]
        self.assertEqual(target["source_id"], CATALOGUE["plain"])
        self.assertIn("打ち合わせ", target["content"])
        self.assertTrue(target["guild_name"])
        self.assertTrue(target["channel_name"])
        self.assertTrue(target["author_name"])
        self.assertTrue(target["timestamp"].endswith("+00:00"))
        self.assertIn("timestamp_jst", target)
        self.assertIn("untrusted", out["note"].lower())

    def test_reply_reference_is_preserved(self):
        out = call("discord_get", source_id=CATALOGUE["reply"])
        expected = CATALOGUE["reply_target"].split(":")[3]
        self.assertEqual(out["target"]["reply_to_message_id"], expected)

    def test_attachment_metadata_only(self):
        out = call("discord_get", source_id=CATALOGUE["attachment"])
        atts = out["target"]["attachments"]
        self.assertEqual(len(atts), 1)
        self.assertEqual(atts[0]["filename"], "spec.pdf")
        blob = json.dumps(out, ensure_ascii=False)
        self.assertNotIn("cdn.example.invalid", blob)

    def test_embed_text_is_available(self):
        out = call("discord_get", source_id=CATALOGUE["bot"])
        target = out["target"]
        self.assertTrue(target["embeds"])
        self.assertEqual(target["embeds"][0]["title"], "本日の予定")
        self.assertTrue(target["author_is_bot"])

    def test_tombstone_returns_metadata_without_a_body(self):
        out = call("discord_get", source_id=CATALOGUE["deleted"])
        target = out["target"]
        self.assertIsNone(target["content"])
        self.assertEqual(target["content_state"], "deleted")
        self.assertIn("deleted", target["message"].lower())
        self.assertTrue(target["is_deleted"])
        self.assertIsNotNone(target["deleted_at"])
        # Provenance still resolves, which is the point of keeping the row.
        self.assertTrue(target["source_id"].startswith("discord:"))
        self.assertIsNotNone(target["timestamp"])
        self.assertNotIn("GONEBODY", json.dumps(out, ensure_ascii=False))

    def test_context_is_bounded_and_stays_in_the_channel(self):
        out = call("discord_get", source_id=CATALOGUE["plain"], context_before=3, context_after=3)
        channel = CATALOGUE["plain"].split(":")[2]
        for row in out["context_before"] + out["context_after"]:
            self.assertEqual(row["channel_id"], channel)
        self.assertLessEqual(len(out["context_before"]), 3)
        self.assertLessEqual(len(out["context_after"]), 3)

    def test_thread_context_does_not_leak_into_the_parent_channel(self):
        out = call("discord_get", source_id=CATALOGUE["thread"], context_before=5, context_after=5)
        thread_channel = CATALOGUE["thread"].split(":")[2]
        for row in out["context_before"] + out["context_after"]:
            self.assertEqual(row["channel_id"], thread_channel)

    def test_unknown_id_is_not_found(self):
        out = call("discord_get", source_id="discord:1:2:3")
        self.assertEqual(out.get("error"), "not_found")


class Status(unittest.TestCase):
    def test_status_reports_readiness_and_scope(self):
        out = call("discord_status")
        self.assertTrue(out["index_ready"])
        self.assertGreaterEqual(out["indexed_message_count"], 1)
        self.assertIn("read-only", out["tool_scope"])
        self.assertTrue(out["write_capability"].startswith("none"))
        self.assertFalse(out["content_in_status"])
        self.assertIn("collector_health", out)
        self.assertIn("gateway_connected", out)
        self.assertIn("index_lag", out)
        self.assertIsNotNone(out.get("allowed_guilds"))
        self.assertIsNotNone(out.get("allowed_channels"))

    def test_collector_health_is_reported_with_its_age(self):
        """The age is wall-clock relative to when the fixture was built, so
        assert the invariant rather than a particular verdict — a fixture that
        happens to be older than the threshold must not fail the suite."""
        out = call("discord_status")
        self.assertTrue(out["collector_state_updated_at"])
        age = out["collector_state_age_seconds"]
        self.assertIsInstance(age, int)
        self.assertGreaterEqual(age, 0)
        self.assertIsInstance(out["collector_state_stale"], bool)
        self.assertEqual(
            out["collector_state_stale"], age > srv.COLLECTOR_STATE_STALE_AFTER_S
        )

    def test_a_dead_collector_is_not_reported_as_live(self):
        """The health value is the collector's own last self-report. If the
        collector stopped, the indexer keeps republishing it verbatim, so the
        age is what distinguishes 'connected' from 'last seen connected'."""
        fresh = {"collector_state_updated_at": "2026-09-02T12:00:00+00:00"}
        stale = {"collector_state_updated_at": "2020-01-01T00:00:00+00:00"}
        self.assertIs(srv._state_is_stale(stale), True)
        self.assertGreater(srv._state_age_seconds(stale), 60 * 60 * 24)
        # Unknown must never read as healthy.
        self.assertIsNone(srv._state_is_stale({}))
        self.assertIsNone(srv._state_age_seconds({}))
        self.assertIsNone(srv._state_is_stale({"collector_state_updated_at": "nonsense"}))
        self.assertIsInstance(srv._state_age_seconds(fresh), int)

    def test_status_contains_no_message_content_and_no_secret(self):
        blob = json.dumps(call("discord_status"), ensure_ascii=False)
        for leak in ("打ち合わせ", "GONEBODY", "NEWBODY", "token", "Authorization"):
            self.assertNotIn(leak, blob)


class UntrustedContent(unittest.TestCase):
    """Instruction-shaped Discord text is returned as evidence, labelled."""

    def test_injection_text_is_retrievable_as_ordinary_data(self):
        out = call("discord_search", query="developer mode")
        self.assertEqual(out["total_returned"], 1)
        hit = out["results"][0]
        self.assertIn("untrusted", out["note"].lower())
        full = call("discord_get", source_id=hit["source_id"])
        self.assertIn("developer mode", full["target"]["content"])
        self.assertIn("untrusted", full["note"].lower())
        # Nothing about the response changed shape because of the text.
        normal = call("discord_get", source_id=CATALOGUE["plain"])
        self.assertEqual(set(full), set(normal))
        self.assertEqual(set(full["target"]), set(normal["target"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
