#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "gmail" / "app"))

import gm_index
import gm_normalize
import gm_store
from gm_schema import open_db


def load_server(index_path):
    class Server:
        def __init__(self, *_args, **_kwargs): pass
        def create_initialization_options(self): return None
        async def run(self, *_args): return None
    class Tool:
        def __init__(self, **kwargs): self.__dict__.update(kwargs)
    class ListToolsResult:
        def __init__(self, **kwargs): self.__dict__.update(kwargs)
    class TextContent:
        def __init__(self, **kwargs): self.__dict__.update(kwargs)
    class CallToolResult:
        def __init__(self, **kwargs): self.__dict__.update(kwargs)
    mcp = types.ModuleType("mcp")
    mcp_types = types.ModuleType("mcp.types")
    for cls in (Tool, ListToolsResult, TextContent, CallToolResult):
        setattr(mcp_types, cls.__name__, cls)
    lowlevel = types.ModuleType("mcp.server.lowlevel"); lowlevel.Server = Server
    stdio = types.ModuleType("mcp.server.stdio")
    async def stdio_server():
        yield None, None
    stdio.stdio_server = stdio_server
    sys.modules.update({
        "mcp": mcp, "mcp.types": mcp_types, "mcp.server": types.ModuleType("mcp.server"),
        "mcp.server.lowlevel": lowlevel, "mcp.server.stdio": stdio,
    })
    os.environ["HERMES_GMAIL_INDEX"] = str(index_path)
    spec = importlib.util.spec_from_file_location("gmail_mcp_test", ROOT / "src" / "gmail" / "mcp" / "gmail_mcp.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def payload(message_id, *, thread="thread-1", subject="明日の会議"):
    return {
        "id": message_id, "threadId": thread, "historyId": "10",
        "internalDate": "1788595200000", "labelIds": ["INBOX"], "snippet": "確認お願いします",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": "sender@example.invalid"},
                {"name": "Content-Type", "value": "text/plain; charset=UTF-8"},
            ],
            "body": {"data": "5piO5pel44Gu5Lya6K2w44Gr44Gk44GE44Gm"},
        },
    }


class GmailMcpTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        authority = Path(self.temp.name) / "gmail.db"
        con = open_db(authority)
        self.addCleanup(con.close)
        row = gm_normalize.normalize_message(payload("m1"), ingested_at="2026-09-05T00:00:00+00:00")
        spam = payload("m-spam", subject="確認お願いします")
        spam["labelIds"] = ["SPAM"]
        spam_row = gm_normalize.normalize_message(spam, ingested_at="2026-09-05T00:00:00+00:00")
        unavailable = gm_normalize.unavailable_message("m-unavailable", "thread-unavailable",
                                                        ingested_at="2026-09-05T00:00:00+00:00")
        with con:
            gm_store.ensure_state(con)
            gm_store.record_api_success(con, mailbox_email="owner@example.invalid")
            gm_store.upsert_message(con, row, baseline=True)
            gm_store.upsert_message(con, spam_row, baseline=True)
            gm_store.upsert_message(con, unavailable, baseline=True)
            gm_store.set_auth_state(con, "AUTHORIZED")
            gm_store.set_watch(con, topic_name="projects/p/topics/t", expiration_ms=9999999999999)
            gm_index.publish_index(con, Path(self.temp.name) / "index" / "index.db")
        self.srv = load_server(Path(self.temp.name) / "index" / "index.db")

    def test_tools_are_read_only_and_text_is_untrusted(self):
        import asyncio
        names = sorted(tool.name for tool in asyncio.run(self.srv._list_tools()).tools)
        self.assertEqual(names, ["gmail_get", "gmail_search", "gmail_status", "gmail_thread_get"])
        self.assertTrue(all("untrusted" in tool.description.lower() for tool in asyncio.run(self.srv._list_tools()).tools[:3]))
        forbidden = ("send", "reply", "forward", "draft", "delete", "label", "archive", "write")
        self.assertTrue(all(not any(word in tool.name.lower() for word in forbidden) for tool in asyncio.run(self.srv._list_tools()).tools))

    def test_search_get_thread_and_status_keep_provenance(self):
        found = self.srv.gmail_search({"query": "会議", "after": "2026-09-05", "before": "2026-09-05"})
        self.assertEqual(found["results"][0]["source_id"], "gmail:m1")
        got = self.srv.gmail_get({"source_id": "gmail:m1"})
        self.assertEqual(got["message"]["thread_id"], "thread-1")
        self.assertIn("untrusted", got["note"].lower())
        thread = self.srv.gmail_thread_get({"thread_id": "thread-1"})
        self.assertEqual(len(thread["messages"]), 1)
        status = self.srv.gmail_status({})
        self.assertTrue(status["index_ready"])
        self.assertEqual(status["status"]["auth_state"], "AUTHORIZED")
        self.assertNotIn("refresh_token", repr(status).lower())

    def test_spam_and_trash_are_not_routine_retrieval_results(self):
        results = self.srv.gmail_search({"limit": 10})["results"]
        self.assertEqual([item["source_id"] for item in results], ["gmail:m1"])
        thread = self.srv.gmail_thread_get({"thread_id": "thread-1"})["messages"]
        self.assertEqual([item["source_id"] for item in thread], ["gmail:m1"])

    def test_body_unavailable_record_is_not_searchable_or_actionable(self):
        results = self.srv.gmail_search({"limit": 10})["results"]
        self.assertNotIn("gmail:m-unavailable", [item["source_id"] for item in results])
        direct = self.srv.gmail_get({"source_id": "gmail:m-unavailable"})["message"]
        self.assertEqual(direct["content_state"], "body_unavailable")
        self.assertEqual(direct["body"], "")
        self.assertEqual(self.srv.gmail_status({})["status"]["body_unavailable_count"], 1)


if __name__ == "__main__":
    unittest.main()
