"""Entries the owner asks for in chat: the request, the run, the MCP tool."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import socket
import sys
import tempfile
import threading
import types
import unittest
from datetime import timedelta
from pathlib import Path

from ct_fixtures import CALENDAR_ID, NOW, FakeCalendar, Workspace, make_cfg

import ct_engine
import ct_request as req
import ct_select as sel
import ct_service
import ct_store as store
from ct_google import ApiError

ROOT = Path(__file__).resolve().parents[1]


def ask(**fields):
    return {"op": "add", **fields}


class RequestCase(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.close)
        self.cfg = make_cfg(paths={"state_db": self.ws.state_path,
                                   "events_db": self.ws.events_path,
                                   "calendar_index_db": self.ws.index_path,
                                   "gmail_index_db": ""})
        self.google = FakeCalendar()
        self.state = store.open_db(self.ws.state_path)
        self.addCleanup(self.state.close)

    def add(self, *, client="google", index=True, **fields):
        desired, note = req.parse_add(ask(**fields), cfg=self.cfg, now=NOW)
        return req.add(self.state, desired, note, cfg=self.cfg, now=NOW,
                       client=self.google if client == "google" else client,
                       index_con=self.ws.index if index else None)

    def run_once(self, now=NOW):
        selection = sel.select(self.ws.events, cfg=self.cfg, now=now, index_con=self.ws.index)
        return ct_engine.run(cfg=self.cfg, state_con=self.state, selection=selection,
                             client=self.google, now=now)

    def only_row(self):
        rows = list(store.all_proposals(self.state).values())
        self.assertEqual(len(rows), 1)
        return rows[0]


class ParseTests(RequestCase):
    def parse(self, **fields):
        return req.parse_add(ask(**fields), cfg=self.cfg, now=NOW)

    def refused(self, code, **fields):
        with self.assertRaises(req.RequestError) as caught:
            self.parse(**fields)
        self.assertEqual(caught.exception.code, code)

    def test_a_timed_request_defaults_to_the_configured_length(self):
        d, note = self.parse(title="歯医者", date="2026-10-03", start="10:00", note="駅前")
        self.assertEqual(d.kind, sel.KIND_APPOINTMENT)
        self.assertFalse(d.all_day)
        self.assertEqual(d.start.isoformat(), "2026-10-03T10:00:00+09:00")
        self.assertEqual(d.end.isoformat(), "2026-10-03T11:00:00+09:00")
        self.assertEqual(note, "駅前")
        self.assertTrue(d.commitment_id.startswith(store.CHAT_PREFIX))

    def test_all_day_and_deadline_shapes(self):
        d, _ = self.parse(title="文化祭", date="2026-10-10")
        self.assertTrue(d.all_day)
        self.assertEqual(d.kind, sel.KIND_APPOINTMENT)
        d, _ = self.parse(title="レポート提出", date="2026-10-10", start="17:00", deadline=True)
        self.assertEqual(d.kind, sel.KIND_DEADLINE)
        self.assertEqual(d.deadline_time, "17:00")
        self.assertEqual(sel.summary_for(d, "【仮】"), "【仮】〆17:00 レポート提出")

    def test_a_given_prefix_is_not_doubled(self):
        d, _ = self.parse(title="【仮】 打ち合わせ", date="2026-10-03")
        self.assertEqual(d.title, "打ち合わせ")

    def test_bad_input_is_refused_with_a_reason(self):
        self.refused("bad_date", title="x", date="10/3")
        self.refused("bad_date", title="x", date="2026-02-30")
        self.refused("past", title="x", date="2026-09-30")
        self.refused("past", title="x", date="2026-10-01", start="08:00")
        self.refused("too_far", title="x", date="2027-12-01")
        self.refused("bad_time", title="x", date="2026-10-03", start="25:00")
        self.refused("bad_time", title="x", date="2026-10-03", end="11:00")
        self.refused("bad_time", title="x", date="2026-10-03", start="11:00", end="10:00")
        self.refused("bad_time", title="x", date="2026-10-03", start="11:00", end="12:00",
                     deadline=True)
        self.refused("bad_request", title="  ", date="2026-10-03")
        self.refused("bad_request", title=["x"], date="2026-10-03")
        self.refused("bad_request", title="x", date="2026-10-03", attendees=["a@example.com"])
        self.refused("bad_request", title="x", date="2026-10-03", deadline="yes")

    def test_parse_request_reads_one_json_object(self):
        self.assertEqual(req.parse_request(b'{"op": "list"}\n'), {"op": "list"})
        for raw in (b"", b"not json", b"[]", b'{"op": "delete"}',
                    b'{"op": "add", "title": "' + b"x" * 5000 + b'"}'):
            with self.assertRaises(req.RequestError):
                req.parse_request(raw)


class AddTests(RequestCase):
    def test_it_is_created_at_once_as_a_tentative_free_entry(self):
        reply = self.add(title="歯医者", date="2026-10-03", start="10:00", note="駅前")
        self.assertEqual(reply["state"], "created")
        self.assertTrue(reply["ok"])
        row = self.only_row()
        self.assertEqual(row["state"], store.ACTIVE)
        self.assertIsNotNone(row["notified_at"])
        event = self.google.events[row["event_id"]]
        self.assertTrue(row["event_id"].startswith(sel.OWN_EVENT_PREFIX))
        self.assertEqual(event["summary"], "【仮】歯医者")
        self.assertEqual(event["status"], "tentative")
        self.assertEqual(event["transparency"], "transparent")
        self.assertNotIn("attendees", event)
        self.assertEqual(event["extendedProperties"]["private"]["hermes_ct"], "1")
        self.assertIn("チャットで頼まれて", event["description"])
        self.assertIn("メモ: 駅前", event["description"])

    def test_the_same_request_twice_makes_one_entry(self):
        self.add(title="歯医者", date="2026-10-03", start="10:00")
        reply = self.add(title="歯医者", date="2026-10-03", start="10:00")
        self.assertEqual(reply["state"], "exists")
        self.assertEqual(self.google.verbs().count("insert"), 1)

    def test_what_is_already_there_is_not_added_again(self):
        self.ws.add_index_event("real-1", "ゼミ発表", "2026-10-05T00:00:00+00:00",
                                end_at="2026-10-05T01:00:00+00:00")
        reply = self.add(title="ゼミ発表", date="2026-10-05", start="09:00")
        self.assertEqual(reply["state"], "already_on_calendar")
        self.ws.add_commitment("c1", "協賛ロゴの入稿", due_at="2026-10-06T17:00:00+09:00",
                               due_precision="day")
        self.run_once()
        reply = self.add(title="協賛ロゴの入稿", date="2026-10-06", deadline=True)
        self.assertEqual(reply["state"], "exists")
        self.assertEqual(self.google.verbs().count("insert"), 1)

    def test_without_the_lock_it_waits_for_the_run_which_keeps_it(self):
        reply = self.add(client=None, title="歯医者", date="2026-10-03", start="10:00")
        self.assertEqual(reply["state"], "queued")
        self.assertEqual(self.google.calls, [])
        report = self.run_once()
        self.assertEqual(report.counts.get("created"), 1)
        self.assertNotIn("dropped_pending", report.counts)
        self.assertEqual(self.only_row()["state"], store.ACTIVE)

    def test_a_run_never_moves_or_withdraws_a_chat_entry(self):
        self.add(title="歯医者", date="2026-10-03", start="10:00")
        self.google.calls.clear()
        report = self.run_once()
        self.assertNotIn("patch", self.google.verbs())
        self.assertNotIn("delete", self.google.verbs())
        self.assertNotIn("withdrawn", report.counts)
        self.assertEqual(self.only_row()["state"], store.ACTIVE)
        plan = ct_engine.plan(self.state, sel.select(self.ws.events, cfg=self.cfg, now=NOW),
                              cfg=self.cfg, now=NOW)
        self.assertEqual(plan.plan["withdraw"], [])

    def test_the_owner_wins_and_asking_again_brings_it_back(self):
        self.add(title="歯医者", date="2026-10-03", start="10:00")
        first = self.only_row()["event_id"]
        self.google.owner_delete(first)
        self.run_once()
        self.assertEqual(self.only_row()["state"], store.REJECTED)
        reply = self.add(title="歯医者", date="2026-10-03", start="10:00")
        self.assertEqual(reply["state"], "created")
        row = self.only_row()
        self.assertNotEqual(row["event_id"], first)
        self.assertEqual(row["generation"], 2)

    def test_an_owner_confirmed_entry_is_left_alone(self):
        self.add(title="歯医者", date="2026-10-03", start="10:00")
        self.google.owner_edit(self.only_row()["event_id"], summary="歯医者")
        self.run_once()
        self.assertEqual(self.only_row()["state"], store.CONFIRMED)
        self.google.calls.clear()
        reply = self.add(title="歯医者", date="2026-10-03", start="10:00")
        self.assertEqual(reply["state"], "owner_has_it")
        self.assertEqual(self.google.calls, [])

    def test_a_daily_cap_bounds_what_chat_can_add(self):
        self.cfg = make_cfg(paths={"state_db": self.ws.state_path,
                                   "events_db": self.ws.events_path},
                            limits={"max_chat_requests_per_day": 2})
        self.add(title="一", date="2026-10-03")
        self.add(title="二", date="2026-10-03")
        with self.assertRaises(req.RequestError) as caught:
            self.add(title="三", date="2026-10-03")
        self.assertEqual(caught.exception.code, "daily_limit")
        self.assertEqual(len(self.google.live()), 2)

    def test_google_unreachable_leaves_it_for_the_timer(self):
        self.google.fail["insert"] = [("raise", ApiError(None, "ConnectionRefusedError",
                                                         sent=False))]
        reply = self.add(title="歯医者", date="2026-10-03", start="10:00")
        self.assertEqual(reply["state"], "queued")
        self.assertEqual(self.only_row()["state"], store.PENDING)
        self.run_once()
        self.assertEqual(self.only_row()["state"], store.ACTIVE)

    def test_list_shows_upcoming_entries_and_which_were_asked_for(self):
        self.ws.add_commitment("c1", "協賛ロゴの入稿", due_at="2026-10-06T17:00:00+09:00",
                               due_precision="day")
        self.run_once()
        self.add(title="歯医者", date="2026-10-03", start="10:00")
        out = req.list_entries(self.state, cfg=self.cfg, now=NOW)
        self.assertEqual([e["asked_in_chat"] for e in out["entries"]], [True, False])
        self.assertEqual(out["entries"][0]["state"], store.ACTIVE)
        later = req.list_entries(self.state, cfg=self.cfg, now=NOW + timedelta(days=4))
        self.assertEqual([e["asked_in_chat"] for e in later["entries"]], [False])


class ServiceTests(RequestCase):
    def call(self, line: bytes, cfg=None) -> dict:
        out = io.BytesIO()
        code = ct_service.cmd_request(cfg or self.cfg, NOW, stdin=io.BytesIO(line), stdout=out,
                                      client_factory=lambda _cfg: self.google)
        self.assertEqual(code, ct_service.EXIT_OK)
        return json.loads(out.getvalue().decode("utf-8"))

    def test_one_json_line_in_one_json_line_out(self):
        reply = self.call(json.dumps(ask(title="歯医者", date="2026-10-03",
                                         start="10:00")).encode() + b"\n")
        self.assertEqual(reply["state"], "created")
        listed = self.call(b'{"op": "list"}\n')
        self.assertEqual(len(listed["entries"]), 1)
        bad = self.call(b"hello\n")
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error"], "bad_request")

    def test_dry_run_and_switched_off_write_nothing(self):
        dry = make_cfg(paths={"state_db": self.ws.state_path, "events_db": self.ws.events_path},
                       behaviour={"mode": "dry_run"})
        reply = self.call(json.dumps(ask(title="歯医者", date="2026-10-03")).encode(), dry)
        self.assertEqual(reply["state"], "dry_run")
        off = make_cfg(paths={"state_db": self.ws.state_path, "events_db": self.ws.events_path},
                       limits={"max_chat_requests_per_day": 0})
        reply = self.call(json.dumps(ask(title="歯医者", date="2026-10-03")).encode(), off)
        self.assertEqual(reply["error"], "disabled")
        self.assertEqual(store.all_proposals(self.state), {})
        self.assertEqual(self.google.calls, [])


def load_mcp(socket_path):
    """The MCP server with only its transport stubbed (as tests-calendar does)."""
    class Server:
        def __init__(self, *_args, **_kwargs): pass
    class Plain:
        def __init__(self, **kwargs): self.__dict__.update(kwargs)
    mcp = types.ModuleType("mcp")
    mcp_types = types.ModuleType("mcp.types")
    for name in ("Tool", "ListToolsResult", "TextContent", "CallToolResult"):
        setattr(mcp_types, name, type(name, (Plain,), {}))
    lowlevel = types.ModuleType("mcp.server.lowlevel")
    lowlevel.Server = Server
    stdio = types.ModuleType("mcp.server.stdio")
    stdio.stdio_server = None
    saved = {k: sys.modules.get(k) for k in ("mcp", "mcp.types", "mcp.server",
                                             "mcp.server.lowlevel", "mcp.server.stdio")}
    sys.modules.update({"mcp": mcp, "mcp.types": mcp_types, "mcp.server": types.ModuleType("mcp.server"),
                        "mcp.server.lowlevel": lowlevel, "mcp.server.stdio": stdio})
    os.environ["HERMES_CT_REQUEST_SOCKET"] = str(socket_path)
    try:
        spec = importlib.util.spec_from_file_location(
            "ct_request_mcp_test", ROOT / "src" / "calendar-tentative" / "mcp" / "ct_request_mcp.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        os.environ.pop("HERMES_CT_REQUEST_SOCKET", None)
        for key, value in saved.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value


@unittest.skipUnless(hasattr(socket, "AF_UNIX"), "needs unix sockets")
class McpOverSocketTests(RequestCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "request.sock")
        self.mcp = load_mcp(self.path)

    def serve(self, connections=1):
        """What systemd does with Accept=yes: one cmd_request per connection."""
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(self.path)
        listener.listen(1)
        self.addCleanup(listener.close)

        def loop():
            for _ in range(connections):
                conn, _ = listener.accept()
                with conn, conn.makefile("rb") as rfile, conn.makefile("wb") as wfile:
                    ct_service.cmd_request(self.cfg, NOW, stdin=rfile, stdout=wfile,
                                           client_factory=lambda _cfg: self.google)
        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
        return thread

    def test_the_tool_reaches_the_service_and_relays_its_reply(self):
        thread = self.serve(connections=2)
        reply = self.mcp.ask({"op": "add", "title": "歯医者", "date": "2026-10-03",
                              "start": "10:00"})
        self.assertEqual(reply["state"], "created")
        listed = self.mcp.ask({"op": "list"})
        thread.join(5)
        self.assertEqual(listed["entries"][0]["asked_in_chat"], True)
        self.assertEqual(len(self.google.live()), 1)

    def test_no_service_means_a_plain_answer_not_a_crash(self):
        reply = self.mcp.ask({"op": "list"})
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "tentative_service_unavailable")

    def test_the_tools_offer_no_way_to_change_or_delete(self):
        import asyncio
        tools = asyncio.run(self.mcp._list_tools()).tools
        self.assertEqual([t.name for t in tools],
                         ["calendar_tentative_add", "calendar_tentative_list"])
        add = tools[0].inputSchema
        self.assertFalse(add["additionalProperties"])
        self.assertEqual(set(add["properties"]),
                         {"title", "date", "start", "end", "deadline", "note"})


if __name__ == "__main__":
    unittest.main()
