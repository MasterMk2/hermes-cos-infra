"""Phase I.2 (T5) — MCP boundary tests for daily_plan_get / morning_brief_get.

Covers the T5 slice of I2-CONTRACT.md section 8:

* the tool listing contains exactly the two new names on top of the 14 existing ones,
* both schemas validate and every input is optional,
* both tools are strictly read-only (mode=ro connection, DB byte-identical after a call),
* the compacted projection drops `plan_hash`, keeps `counts` / `source_freshness`, and
  keeps every phase-section-34 traceable item field,
* output size is bounded,
* malformed input yields a clean error result, never a traceback,
* the renderer only ever sees the compacted plan (phase sections 49, 73),
* the Antigravity allowlist delta is exactly two entries and contains no wildcard.

The planner (T2) and the renderer (T3) are stubbed: this suite must pass while
`src/planning/` is still incomplete.
"""

import asyncio
import hashlib
import importlib
import importlib.util
import json
import os
import re
import sqlite3
import sys
import tempfile
import types as pytypes
import unittest
from contextlib import contextmanager

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(REPO_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from planning import mcp_planning  # noqa: E402

EVENT_MCP_PATH = os.path.join(SRC_DIR, "events", "event_mcp.py")

ALLOWLIST_SNIPPET = os.path.join(
    SRC_DIR, "antigravity-bridge", "config", "mcp-allowlist-i2.snippet.json"
)
ALLOWLIST_DOC = os.path.join(SRC_DIR, "antigravity-bridge", "config", "mcp-allowlist-i2.md")

PHASE_I1_TOOLS = [
    "event_list_open",
    "event_get",
    "event_search",
    "event_mark_resolved",
    "event_mark_ignored",
    "event_mark_cancelled",
    "event_link_project",
    "event_snooze",
    "event_status",
    "commitment_list",
    "commitment_get",
    "commitment_search",
    "commitment_status",
    "agenda_get",
]
PHASE_I2_TOOLS = ["daily_plan_get", "morning_brief_get"]


# ---------------------------------------------------------------------------
# Minimal `mcp` stubs so event_mcp.py can be exercised on a workstation that has
# no `mcp` package installed. Installed and removed inside a context manager so
# nothing leaks into the other suites.
# ---------------------------------------------------------------------------

class _StubTool:
    def __init__(self, name=None, description=None, inputSchema=None):  # noqa: N803
        self.name = name
        self.description = description
        self.inputSchema = inputSchema


class _StubListToolsResult:
    def __init__(self, tools=None):
        self.tools = list(tools or [])


class _StubTextContent:
    def __init__(self, type=None, text=None):  # noqa: A002
        self.type = type
        self.text = text


class _StubCallToolResult:
    def __init__(self, content=None):
        self.content = list(content or [])


class _StubServer:
    def __init__(self, name, on_list_tools=None, on_call_tool=None):
        self.name = name
        self.on_list_tools = on_list_tools
        self.on_call_tool = on_call_tool


def _build_mcp_stub_modules():
    mcp_mod = pytypes.ModuleType("mcp")
    server_mod = pytypes.ModuleType("mcp.server")
    lowlevel_mod = pytypes.ModuleType("mcp.server.lowlevel")
    stdio_mod = pytypes.ModuleType("mcp.server.stdio")
    types_mod = pytypes.ModuleType("mcp.types")

    lowlevel_mod.Server = _StubServer
    stdio_mod.stdio_server = lambda *a, **k: None
    types_mod.Tool = _StubTool
    types_mod.ListToolsResult = _StubListToolsResult
    types_mod.TextContent = _StubTextContent
    types_mod.CallToolResult = _StubCallToolResult

    server_mod.lowlevel = lowlevel_mod
    server_mod.stdio = stdio_mod
    mcp_mod.server = server_mod
    mcp_mod.types = types_mod
    return {
        "mcp": mcp_mod,
        "mcp.server": server_mod,
        "mcp.server.lowlevel": lowlevel_mod,
        "mcp.server.stdio": stdio_mod,
        "mcp.types": types_mod,
    }


@contextmanager
def _patched_modules(overrides):
    """Temporarily install modules in sys.modules, restoring the previous state."""
    saved = {}
    for name, module in overrides.items():
        saved[name] = sys.modules.get(name, KeyError)
        sys.modules[name] = module
    try:
        yield
    finally:
        for name, previous in saved.items():
            if previous is KeyError:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


@contextmanager
def load_event_mcp(db_path):
    """Load event_mcp.py under a private module name, with stubbed `mcp`.

    Loading under a private name means `events.event_mcp` (used by
    tests-commitments) is never imported, replaced, or cached by this suite.
    """
    overrides = {}
    if "mcp" not in sys.modules:
        overrides = _build_mcp_stub_modules()
    saved_db = os.environ.get("EVENTS_DB", KeyError)
    os.environ["EVENTS_DB"] = db_path
    try:
        with _patched_modules(overrides):
            spec = importlib.util.spec_from_file_location(
                "_event_mcp_under_test", EVENT_MCP_PATH
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.DB = db_path
            yield module
    finally:
        if saved_db is KeyError:
            os.environ.pop("EVENTS_DB", None)
        else:
            os.environ["EVENTS_DB"] = saved_db


@contextmanager
def stub_planning_modules(build_plan=None, render_brief=None, load_config=None):
    """Inject stub `planning.planner` / `planning.renderer` / `planning.plan_config`.

    mcp_planning resolves these lazily via importlib, so a sys.modules entry is
    exactly what production would find once T2/T3 land their real modules.
    """
    overrides = {}
    if build_plan is not None:
        planner = pytypes.ModuleType("planning.planner")
        planner.build_daily_plan = build_plan
        overrides["planning.planner"] = planner
    if render_brief is not None:
        renderer = pytypes.ModuleType("planning.renderer")
        renderer.render_brief = render_brief
        overrides["planning.renderer"] = renderer
    if load_config is not None:
        cfg = pytypes.ModuleType("planning.plan_config")
        cfg.load_config = load_config
        overrides["planning.plan_config"] = cfg
    with _patched_modules(overrides):
        yield


@contextmanager
def hidden_planning_modules(*names):
    """Make the named planning submodules un-importable (T2/T3 not landed yet)."""
    blocker = object()
    saved = {name: sys.modules.get(name, KeyError) for name in names}
    real_import = importlib.import_module

    def fake_import(name, package=None):
        if name in names:
            raise ModuleNotFoundError(f"No module named {name!r}")
        return real_import(name, package)

    for name in names:
        sys.modules.pop(name, None)
    mcp_planning.importlib.import_module = fake_import
    try:
        yield blocker
    finally:
        mcp_planning.importlib.import_module = real_import
        for name, previous in saved.items():
            if previous is KeyError:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


def make_events_db(path):
    """A tiny real sqlite file; the stub planner never queries it."""
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS probe (id INTEGER PRIMARY KEY, v TEXT)")
        con.execute("INSERT INTO probe (v) VALUES ('baseline')")
        con.commit()
    finally:
        con.close()
    return path


def sample_item(commitment_id="cmt_1", **overrides):
    item = {
        "commitment_id": commitment_id,
        "title": "見積書を送る",
        "responsibility": "SELF",
        "work_state": "ACTIONABLE",
        "due_at": "2026-09-08T09:00:00+00:00",
        "due_precision": "minute",
        "scheduled_at": None,
        "scheduled_end_at": None,
        "all_day": False,
        "local_time": "18:00",
        "counterparty": None,
        "priority_class": "P1",
        "reason_for_inclusion": "due_today",
        "source_types": ["discord"],
    }
    item.update(overrides)
    return item


def sample_plan(**overrides):
    plan = {
        "date": "2026-09-08",
        "timezone": "Asia/Tokyo",
        "generated_at": "2026-09-08T08:30:00+09:00",
        "scheduled": [],
        "must_do": [sample_item()],
        "overdue": [],
        "next_up": [],
        "waiting_for": [],
        "backlog": [],
        "conflicts": [],
        "counts": {
            "scheduled": {"shown": 0, "total": 0},
            "must_do": {"shown": 1, "total": 1},
            "overdue": {"shown": 0, "total": 0},
            "next_up": {"shown": 0, "total": 0},
            "waiting_for": {"shown": 0, "total": 0},
            "backlog": {"shown": 0, "total": 0},
            "conflicts": {"shown": 0, "total": 0},
            "filtered_noise": {"shown": 0, "total": 194},
        },
        "source_freshness": {
            "calendar": {
                "last_success_at": "2026-09-08T08:00:00+09:00",
                "age_seconds": 1800,
                "stale": False,
                "threshold_seconds": 86400,
            }
        },
        "plan_hash": "0" * 64,
    }
    plan.update(overrides)
    return plan


def planner_stub(plan=None, calls=None):
    def _build(conn, *, date=None, timezone=None, now=None, config=None, freshness=None):
        if calls is not None:
            calls.append(
                {"conn": conn, "date": date, "timezone": timezone, "config": config}
            )
        return sample_plan() if plan is None else plan

    return _build


def renderer_stub(result=None, calls=None):
    def _render(plan, *, mode="auto", config=None, client=None, now=None):
        if calls is not None:
            calls.append({"plan": plan, "mode": mode, "config": config})
        if result is not None:
            return result
        return {
            "text": "本日の予定 1件。\n最優先\n- 見積書を送る",
            "renderer": "template",
            "model": None,
            "fallback_reason": None,
            "grounding": {"ok": True, "violations": []},
        }

    return _render


class _Params:
    def __init__(self, name, arguments=None):
        self.name = name
        self.arguments = arguments


class TempDbTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = make_events_db(os.path.join(self.tmp.name, "events.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def db_fingerprint(self):
        with open(self.db_path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()


# ---------------------------------------------------------------------------
# Tool surface — the exact 16-tool listing and the two schemas
# ---------------------------------------------------------------------------

class TestToolSurface(TempDbTestCase):
    def tool_list(self):
        with load_event_mcp(self.db_path) as event_mcp:
            return asyncio.run(event_mcp._list_tools()).tools

    def test_list_tools_returns_exactly_16_tools(self):
        tools = self.tool_list()
        self.assertEqual(len(tools), 16)
        self.assertEqual([t.name for t in tools], PHASE_I1_TOOLS + PHASE_I2_TOOLS)

    def test_all_14_phase_i1_tools_survive(self):
        names = [t.name for t in self.tool_list()]
        for name in PHASE_I1_TOOLS:
            self.assertIn(name, names)

    def test_tool_names_are_unique(self):
        names = [t.name for t in self.tool_list()]
        self.assertEqual(len(names), len(set(names)))

    def test_new_tool_schemas_validate(self):
        tools = {t.name: t for t in self.tool_list()}
        for name in PHASE_I2_TOOLS:
            schema = tools[name].inputSchema
            self.assertEqual(schema["type"], "object")
            self.assertIn("date", schema["properties"])
            self.assertIn("timezone", schema["properties"])
            # Every input is optional: phase section 32 defaults must apply.
            self.assertNotIn("required", schema)
            self.assertEqual(schema["properties"]["timezone"]["default"], "Asia/Tokyo")
            pattern = schema["properties"]["date"]["pattern"]
            self.assertIsNotNone(re.match(pattern, "2026-09-08"))
            self.assertIsNone(re.match(pattern, "8 Sep 2026"))
            json.dumps(schema)  # serializable over stdio

    def test_morning_brief_mode_enum_is_read_only_safe(self):
        tools = {t.name: t for t in self.tool_list()}
        mode = tools["morning_brief_get"].inputSchema["properties"]["mode"]
        self.assertEqual(mode["enum"], ["auto", "template"])
        self.assertEqual(mode["default"], "auto")

    def test_descriptions_carry_the_owner_trigger_phrases(self):
        """Phase section 124: the model must pick daily_plan_get with no manual -t."""
        tools = {t.name: t for t in self.tool_list()}
        plan_desc = tools["daily_plan_get"].description
        for phrase in ("今日何すればいい？", "今日の予定まとめて", "何からやればいい？",
                       "今日の優先順位は？", "what should I do today"):
            self.assertIn(phrase, plan_desc)
        # It must also disambiguate itself from the existing agenda_get tool.
        self.assertIn("agenda_get", plan_desc)
        brief_desc = tools["morning_brief_get"].description
        self.assertIn("daily_plan_get", brief_desc)
        for desc in (plan_desc, brief_desc):
            self.assertIn("ead-only", desc)


# ---------------------------------------------------------------------------
# Dispatch through _call_tool
# ---------------------------------------------------------------------------

class TestDispatch(TempDbTestCase):
    def call(self, event_mcp, name, arguments=None):
        result = asyncio.run(event_mcp._call_tool(None, _Params(name, arguments)))
        return json.loads(result.content[0].text)

    def test_daily_plan_get_dispatches(self):
        with load_event_mcp(self.db_path) as event_mcp:
            with stub_planning_modules(build_plan=planner_stub()):
                out = self.call(event_mcp, "daily_plan_get", {})
        self.assertEqual(out["date"], "2026-09-08")
        self.assertIn("counts", out)
        self.assertNotIn("plan_hash", out)

    def test_morning_brief_get_dispatches(self):
        with load_event_mcp(self.db_path) as event_mcp:
            with stub_planning_modules(
                build_plan=planner_stub(), render_brief=renderer_stub()
            ):
                out = self.call(event_mcp, "morning_brief_get", {"mode": "template"})
        self.assertEqual(sorted(out), ["brief_text", "counts", "date", "renderer", "timezone"])

    def test_unknown_tool_still_reports_unknown(self):
        with load_event_mcp(self.db_path) as event_mcp:
            out = self.call(event_mcp, "daily_plan_delete", {})
        self.assertEqual(out["error"], "unknown_tool:daily_plan_delete")

    def test_phase_i1_tool_still_dispatches(self):
        """The change is additive: an existing tool must still route."""
        with load_event_mcp(self.db_path) as event_mcp:
            out = self.call(event_mcp, "commitment_status", {})
        self.assertIsInstance(out, dict)
        self.assertNotIn("unknown_tool", json.dumps(out))

    def test_handlers_guard_a_missing_planning_package(self):
        with load_event_mcp(self.db_path) as event_mcp:
            event_mcp.planning_daily_plan_get = None
            event_mcp.planning_morning_brief_get = None
            self.assertEqual(
                self.call(event_mcp, "daily_plan_get", {})["error"],
                "planning_module_unavailable",
            )
            self.assertEqual(
                self.call(event_mcp, "morning_brief_get", {})["error"],
                "planning_module_unavailable",
            )

    def test_bad_date_produces_an_error_result_not_a_traceback(self):
        with load_event_mcp(self.db_path) as event_mcp:
            with stub_planning_modules(build_plan=planner_stub()):
                out = self.call(event_mcp, "daily_plan_get", {"date": "2026-13-45"})
        self.assertEqual(out["error"], "invalid_date")
        self.assertNotIn("internal:", json.dumps(out))


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

class TestInputValidation(TempDbTestCase):
    def plan(self, args, **kw):
        return mcp_planning.daily_plan_get(
            args, db_path=self.db_path, build_plan=planner_stub(), **kw
        )

    def test_defaults_are_today_and_asia_tokyo(self):
        calls = []
        mcp_planning.daily_plan_get(
            {}, db_path=self.db_path, build_plan=planner_stub(calls=calls)
        )
        self.assertIsNone(calls[0]["date"])       # None -> planner resolves "today"
        self.assertEqual(calls[0]["timezone"], "Asia/Tokyo")

    def test_none_args_is_accepted(self):
        out = mcp_planning.daily_plan_get(None, db_path=self.db_path, build_plan=planner_stub())
        self.assertEqual(out["date"], "2026-09-08")

    def test_valid_date_is_forwarded(self):
        calls = []
        mcp_planning.daily_plan_get(
            {"date": " 2026-09-08 "},
            db_path=self.db_path,
            build_plan=planner_stub(calls=calls),
        )
        self.assertEqual(calls[0]["date"], "2026-09-08")

    def test_malformed_dates_rejected_cleanly(self):
        for bad in ("2026-9-8", "08/09/2026", "yesterday", "2026-02-30", "2026-13-01",
                    "2026-09-08T00:00:00", "x" * 64):
            with self.subTest(bad=bad):
                out = self.plan({"date": bad})
                self.assertEqual(out["error"], "invalid_date", bad)

    def test_non_string_date_rejected(self):
        for bad in (20260908, [], {}, True):
            with self.subTest(bad=bad):
                self.assertEqual(self.plan({"date": bad})["error"], "invalid_date")

    def test_empty_date_means_today(self):
        calls = []
        mcp_planning.daily_plan_get(
            {"date": "  "}, db_path=self.db_path, build_plan=planner_stub(calls=calls)
        )
        self.assertIsNone(calls[0]["date"])

    def test_invalid_timezone_rejected_cleanly(self):
        for bad in ("Mars/Olympus", "../../etc/passwd", "Asia/Tokyo; DROP TABLE", "/etc/localtime", 5):
            with self.subTest(bad=bad):
                self.assertEqual(self.plan({"timezone": bad})["error"], "invalid_timezone")

    def test_alternative_timezone_is_forwarded(self):
        calls = []
        mcp_planning.daily_plan_get(
            {"timezone": "UTC"}, db_path=self.db_path, build_plan=planner_stub(calls=calls)
        )
        self.assertEqual(calls[0]["timezone"], "UTC")

    def test_invalid_mode_rejected_cleanly(self):
        out = mcp_planning.morning_brief_get(
            {"mode": "llm"},
            db_path=self.db_path,
            build_plan=planner_stub(),
            render=renderer_stub(),
        )
        self.assertEqual(out["error"], "invalid_mode")
        self.assertEqual(out["allowed"], ["auto", "template"])

    def test_mode_is_forwarded_to_the_renderer(self):
        calls = []
        mcp_planning.morning_brief_get(
            {"mode": "TEMPLATE "},
            db_path=self.db_path,
            build_plan=planner_stub(),
            render=renderer_stub(calls=calls),
        )
        self.assertEqual(calls[0]["mode"], "template")

    def test_default_mode_is_auto(self):
        calls = []
        mcp_planning.morning_brief_get(
            {}, db_path=self.db_path, build_plan=planner_stub(), render=renderer_stub(calls=calls)
        )
        self.assertEqual(calls[0]["mode"], "auto")


# ---------------------------------------------------------------------------
# Compaction and output bounds
# ---------------------------------------------------------------------------

class TestCompaction(TempDbTestCase):
    def test_plan_hash_is_dropped_counts_and_freshness_kept(self):
        out = mcp_planning.daily_plan_get(
            {}, db_path=self.db_path, build_plan=planner_stub()
        )
        self.assertNotIn("plan_hash", out)
        self.assertIn("counts", out)
        self.assertIn("source_freshness", out)
        self.assertIn("generated_at", out)

    def test_every_category_key_is_present(self):
        out = mcp_planning.daily_plan_get({}, db_path=self.db_path, build_plan=planner_stub())
        for category in mcp_planning.PLAN_CATEGORIES + ("conflicts",):
            self.assertIn(category, out)

    def test_items_keep_the_section_34_traceable_fields(self):
        out = mcp_planning.daily_plan_get({}, db_path=self.db_path, build_plan=planner_stub())
        item = out["must_do"][0]
        for field in ("commitment_id", "title", "responsibility", "work_state",
                      "due_at", "scheduled_at", "reason_for_inclusion"):
            self.assertIn(field, item)
        self.assertEqual(item["commitment_id"], "cmt_1")
        self.assertEqual(item["title"], "見積書を送る")

    def test_traceable_fields_are_present_even_when_the_planner_omits_them(self):
        bare = sample_plan(must_do=[{"commitment_id": "cmt_x", "title": "t"}])
        out = mcp_planning.daily_plan_get(
            {}, db_path=self.db_path, build_plan=planner_stub(plan=bare)
        )
        item = out["must_do"][0]
        for field in mcp_planning.REQUIRED_ITEM_FIELDS:
            self.assertIn(field, item)
        self.assertIsNone(item["reason_for_inclusion"])

    def test_amendment_fields_survive_compaction(self):
        """due_precision / all_day / local_time drive the renderer (AMENDMENT B4, B2)."""
        out = mcp_planning.daily_plan_get({}, db_path=self.db_path, build_plan=planner_stub())
        item = out["must_do"][0]
        for field in ("due_precision", "all_day", "local_time", "priority_class",
                      "source_types", "counterparty", "scheduled_end_at"):
            self.assertIn(field, item)

    def test_unknown_item_fields_are_dropped(self):
        """Data minimisation (phase section 73): a raw body must never reach the model."""
        noisy = sample_plan(
            must_do=[sample_item(raw_body="ここに生のGmail本文", internal_score=0.93)]
        )
        out = mcp_planning.daily_plan_get(
            {}, db_path=self.db_path, build_plan=planner_stub(plan=noisy)
        )
        item = out["must_do"][0]
        self.assertNotIn("raw_body", item)
        self.assertNotIn("internal_score", item)
        self.assertNotIn("生のGmail本文", json.dumps(out, ensure_ascii=False))

    def test_unknown_top_level_plan_keys_are_dropped(self):
        noisy = sample_plan(debug_sql="SELECT * FROM commitments", raw_rows=[{"a": 1}])
        out = mcp_planning.daily_plan_get(
            {}, db_path=self.db_path, build_plan=planner_stub(plan=noisy)
        )
        self.assertNotIn("debug_sql", out)
        self.assertNotIn("raw_rows", out)

    def test_warnings_are_kept_when_present_and_omitted_when_empty(self):
        with_warn = sample_plan(warnings=["data_freshness_warning"])
        out = mcp_planning.daily_plan_get(
            {}, db_path=self.db_path, build_plan=planner_stub(plan=with_warn)
        )
        self.assertEqual(out["warnings"], ["data_freshness_warning"])
        out2 = mcp_planning.daily_plan_get(
            {}, db_path=self.db_path, build_plan=planner_stub()
        )
        self.assertNotIn("warnings", out2)

    def test_conflicts_are_projected(self):
        plan = sample_plan(conflicts=[{
            "kind": "overlapping_scheduled",
            "commitment_ids": ["cmt_a", "cmt_b"],
            "window_start": "2026-09-08T09:00:00+09:00",
            "window_end": "2026-09-08T10:30:00+09:00",
            "detail": "予定が連続しています",
            "debug": "drop me",
        }])
        out = mcp_planning.daily_plan_get(
            {}, db_path=self.db_path, build_plan=planner_stub(plan=plan)
        )
        self.assertEqual(sorted(out["conflicts"][0]), sorted(mcp_planning.CONFLICT_FIELDS))

    def test_categories_are_clamped(self):
        plan = sample_plan(
            backlog=[sample_item(f"cmt_b{i}") for i in range(40)],
            must_do=[sample_item(f"cmt_m{i}") for i in range(40)],
        )
        out = mcp_planning.daily_plan_get(
            {}, db_path=self.db_path, build_plan=planner_stub(plan=plan)
        )
        for category in mcp_planning.PLAN_CATEGORIES:
            self.assertLessEqual(len(out[category]), mcp_planning.MAX_ITEMS_PER_CATEGORY)

    def test_output_size_is_bounded(self):
        big = sample_plan(
            must_do=[sample_item(f"cmt_m{i}", title="長" * 4000) for i in range(10)],
            backlog=[sample_item(f"cmt_b{i}", title="長" * 4000) for i in range(10)],
            waiting_for=[sample_item(f"cmt_w{i}", title="長" * 4000) for i in range(10)],
        )
        out = mcp_planning.daily_plan_get(
            {}, db_path=self.db_path, build_plan=planner_stub(plan=big)
        )
        size = len(json.dumps(out, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        self.assertLessEqual(size, mcp_planning.MAX_PLAN_JSON_BYTES)
        self.assertTrue(out["truncated"])
        self.assertIn("backlog", out["truncated_categories"])

    def test_a_normal_plan_is_not_marked_truncated(self):
        out = mcp_planning.daily_plan_get({}, db_path=self.db_path, build_plan=planner_stub())
        self.assertNotIn("truncated", out)

    def test_titles_are_never_rewritten(self):
        plan = sample_plan(must_do=[sample_item(title="  空白と「記号」を含む題名  ")])
        out = mcp_planning.daily_plan_get(
            {}, db_path=self.db_path, build_plan=planner_stub(plan=plan)
        )
        self.assertEqual(out["must_do"][0]["title"], "  空白と「記号」を含む題名  ")


# ---------------------------------------------------------------------------
# Read-only guarantees
# ---------------------------------------------------------------------------

class TestReadOnly(TempDbTestCase):
    def test_connection_is_read_only(self):
        con = mcp_planning._connect_readonly(self.db_path)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                con.execute("INSERT INTO probe (v) VALUES ('mutated')")
            with self.assertRaises(sqlite3.OperationalError):
                con.execute("DROP TABLE probe")
        finally:
            con.close()

    def test_read_still_works_on_the_read_only_connection(self):
        con = mcp_planning._connect_readonly(self.db_path)
        try:
            self.assertEqual(con.execute("SELECT v FROM probe").fetchone()[0], "baseline")
        finally:
            con.close()

    def test_daily_plan_get_leaves_the_db_byte_identical(self):
        before = self.db_fingerprint()
        mcp_planning.daily_plan_get({}, db_path=self.db_path, build_plan=planner_stub())
        self.assertEqual(self.db_fingerprint(), before)

    def test_morning_brief_get_leaves_the_db_byte_identical(self):
        before = self.db_fingerprint()
        mcp_planning.morning_brief_get(
            {}, db_path=self.db_path, build_plan=planner_stub(), render=renderer_stub()
        )
        self.assertEqual(self.db_fingerprint(), before)

    def test_no_sidecar_files_are_created(self):
        before = sorted(os.listdir(self.tmp.name))
        mcp_planning.daily_plan_get({}, db_path=self.db_path, build_plan=planner_stub())
        self.assertEqual(sorted(os.listdir(self.tmp.name)), before)

    def test_the_connection_is_closed_even_when_the_planner_raises(self):
        holder = {}

        def grabby(conn, **kwargs):
            holder["conn"] = conn
            raise RuntimeError("boom")

        out = mcp_planning.daily_plan_get({}, db_path=self.db_path, build_plan=grabby)
        self.assertEqual(out["error"], "planner_error")
        with self.assertRaises(sqlite3.ProgrammingError):
            holder["conn"].execute("SELECT 1")

    def test_module_never_references_a_write_or_delivery_path(self):
        source = open(mcp_planning.__file__, encoding="utf-8").read()
        for forbidden in ("mode=rw", "planning.delivery", "planning.brief_state",
                          "planning.brief_service", "send_owner_notification",
                          "proactive.sock", "INSERT ", "UPDATE ", "DELETE "):
            self.assertNotIn(forbidden, source, forbidden)


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------

class TestFailureHandling(TempDbTestCase):
    def test_planner_not_yet_landed(self):
        with hidden_planning_modules("planning.planner", "src.planning.planner"):
            out = mcp_planning.daily_plan_get({}, db_path=self.db_path)
        self.assertEqual(out["error"], "planning_module_unavailable")

    def test_renderer_not_yet_landed(self):
        with hidden_planning_modules("planning.renderer", "src.planning.renderer"):
            out = mcp_planning.morning_brief_get(
                {}, db_path=self.db_path, build_plan=planner_stub()
            )
        self.assertEqual(out["error"], "renderer_module_unavailable")

    def test_planner_exception_leaks_only_the_type(self):
        def exploding(conn, **kwargs):
            raise ValueError("/srv/hermes/hermes-data/events/events.db is corrupt")

        out = mcp_planning.daily_plan_get({}, db_path=self.db_path, build_plan=exploding)
        self.assertEqual(out, {"error": "planner_error", "detail": "ValueError"})

    def test_renderer_exception_leaks_only_the_type(self):
        def exploding(plan, **kwargs):
            raise TimeoutError("upstream 10.0.0.5 timed out")

        out = mcp_planning.morning_brief_get(
            {}, db_path=self.db_path, build_plan=planner_stub(), render=exploding
        )
        self.assertEqual(out, {"error": "renderer_error", "detail": "TimeoutError"})

    def test_non_dict_planner_result(self):
        out = mcp_planning.daily_plan_get(
            {}, db_path=self.db_path, build_plan=lambda conn, **kw: ["not", "a", "plan"]
        )
        self.assertEqual(out["error"], "planner_error")

    def test_missing_db_is_reported_cleanly(self):
        out = mcp_planning.daily_plan_get(
            {}, db_path=os.path.join(self.tmp.name, "absent.db"), build_plan=planner_stub()
        )
        self.assertEqual(out["error"], "events_db_unavailable")

    def test_broken_config_loader_is_not_fatal(self):
        def broken():
            raise OSError("config unreadable")

        out = mcp_planning.daily_plan_get(
            {}, db_path=self.db_path, build_plan=planner_stub(), load_config=broken
        )
        self.assertEqual(out["date"], "2026-09-08")

    def test_config_is_forwarded_to_the_planner(self):
        calls = []
        mcp_planning.daily_plan_get(
            {},
            db_path=self.db_path,
            build_plan=planner_stub(calls=calls),
            load_config=lambda: {"timezone": "Asia/Tokyo"},
        )
        self.assertEqual(calls[0]["config"], {"timezone": "Asia/Tokyo"})


# ---------------------------------------------------------------------------
# morning_brief_get
# ---------------------------------------------------------------------------

class TestMorningBrief(TempDbTestCase):
    def brief(self, args=None, **kw):
        kw.setdefault("build_plan", planner_stub())
        kw.setdefault("render", renderer_stub())
        return mcp_planning.morning_brief_get(args or {}, db_path=self.db_path, **kw)

    def test_output_keys_are_exactly_the_contracted_five(self):
        out = self.brief()
        self.assertEqual(
            sorted(out), ["brief_text", "counts", "date", "renderer", "timezone"]
        )
        self.assertEqual(out["date"], "2026-09-08")
        self.assertEqual(out["timezone"], "Asia/Tokyo")
        self.assertEqual(out["renderer"], "template")
        self.assertEqual(out["counts"]["must_do"], {"shown": 1, "total": 1})

    def test_renderer_receives_the_compacted_plan_only(self):
        """Phase sections 49 and 73: never dump the canonical DB into the model."""
        calls = []
        noisy = sample_plan(must_do=[sample_item(raw_body="生のメール本文")])
        self.brief(build_plan=planner_stub(plan=noisy), render=renderer_stub(calls=calls))
        seen = calls[0]["plan"]
        self.assertNotIn("plan_hash", seen)
        self.assertNotIn("raw_body", seen["must_do"][0])
        self.assertNotIn("生のメール本文", json.dumps(seen, ensure_ascii=False))
        self.assertIn("counts", seen)

    def test_brief_text_is_bounded(self):
        long_text = {"text": "あ" * 50000, "renderer": "llm"}
        out = self.brief(render=renderer_stub(result=long_text))
        self.assertLessEqual(len(out["brief_text"]), mcp_planning.MAX_BRIEF_CHARS)

    def test_empty_render_is_an_error_not_an_empty_brief(self):
        """Phase section 60: never fabricate or emit a hollow brief."""
        for bad in ({"text": "", "renderer": "llm"},
                    {"text": "   ", "renderer": "llm"},
                    {"renderer": "llm"},
                    {"text": 42, "renderer": "llm"}):
            with self.subTest(bad=bad):
                out = self.brief(render=renderer_stub(result=bad))
                self.assertEqual(out["error"], "renderer_error")

    def test_non_dict_render_result(self):
        out = self.brief(render=lambda plan, **kwargs: "just a string")
        self.assertEqual(out["error"], "renderer_error")

    def test_renderer_name_is_reported_verbatim(self):
        out = self.brief(render=renderer_stub(result={"text": "本文", "renderer": "llm"}))
        self.assertEqual(out["renderer"], "llm")

    def test_missing_renderer_name_is_labelled_unknown(self):
        out = self.brief(render=renderer_stub(result={"text": "本文"}))
        self.assertEqual(out["renderer"], "unknown")

    def test_bad_input_short_circuits_before_the_planner_runs(self):
        calls = []
        out = self.brief({"date": "not-a-date"}, build_plan=planner_stub(calls=calls))
        self.assertEqual(out["error"], "invalid_date")
        self.assertEqual(calls, [])


# ---------------------------------------------------------------------------
# Antigravity allowlist delta
# ---------------------------------------------------------------------------

class TestAllowlistDelta(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(ALLOWLIST_SNIPPET, encoding="utf-8") as fh:
            cls.snippet = json.load(fh)
        with open(ALLOWLIST_DOC, encoding="utf-8") as fh:
            cls.doc = fh.read()

    def test_exactly_two_entries_are_added(self):
        self.assertEqual(
            self.snippet["entries_to_add"],
            ["mcp(event/daily_plan_get)", "mcp(event/morning_brief_get)"],
        )

    def test_count_goes_from_25_to_27(self):
        self.assertEqual(self.snippet["expected_allow_count_before"], 25)
        self.assertEqual(self.snippet["expected_allow_count_after"], 27)
        self.assertEqual(len(self.snippet["expected_allow_after"]), 27)

    def test_resulting_allowlist_is_the_baseline_plus_the_two(self):
        after = self.snippet["expected_allow_after"]
        self.assertEqual(after[-2:], self.snippet["entries_to_add"])
        self.assertEqual(len(set(after)), 27)

    def test_no_wildcard_in_any_allow_entry(self):
        """Phase section 48 completion gate."""
        for entry in self.snippet["expected_allow_after"] + self.snippet["entries_to_add"]:
            self.assertNotIn("*", entry)

    def test_deny_wildcard_is_only_the_preexisting_command_rule(self):
        starred = [e for e in self.snippet["expected_deny_after"] if "*" in e]
        self.assertEqual(starred, ["command(*)"])

    def test_the_forbidden_token_is_never_written_out_in_our_own_files(self):
        """A repo-wide grep for the forbidden token must not hit our own artifacts."""
        token = "mcp(" + "*)"
        for path in (ALLOWLIST_SNIPPET, ALLOWLIST_DOC,
                     os.path.join(SRC_DIR, "planning", "mcp_planning.py"),
                     os.path.join(SRC_DIR, "events", "event_mcp.py")):
            with self.subTest(path=os.path.basename(path)):
                self.assertNotIn(token, open(path, encoding="utf-8").read())

    def test_every_entry_uses_the_exact_verbatim_syntax(self):
        pattern = re.compile(r"^mcp\([a-z_]+/[a-z0-9_]+\)$")
        for entry in self.snippet["expected_allow_after"]:
            self.assertRegex(entry, pattern)

    def test_deny_list_is_preserved_and_unchanged(self):
        self.assertTrue(self.snippet["deny_unchanged"])
        for denied in ("mcp(event/event_link_project)", "mcp(event/event_mark_cancelled)",
                       "mcp(event/event_mark_ignored)", "mcp(event/event_mark_resolved)",
                       "mcp(event/event_snooze)", "command(*)"):
            self.assertIn(denied, self.snippet["expected_deny_after"])
        self.assertEqual(len(self.snippet["expected_deny_after"]), 6)

    def test_no_companion_deny_entry_is_claimed_necessary_and_it_is_justified(self):
        self.assertFalse(self.snippet["deny_companion_entry_required"])
        self.assertGreater(len(self.snippet["deny_companion_rationale"]), 200)

    def test_new_tools_do_not_collide_with_any_denied_name(self):
        denied_tools = {
            e.split("/", 1)[1].rstrip(")")
            for e in self.snippet["expected_deny_after"] if e.startswith("mcp(")
        }
        for tool in PHASE_I2_TOOLS:
            self.assertNotIn(tool, denied_tools)
            for denied in denied_tools:
                self.assertFalse(tool.startswith(denied) or denied.startswith(tool))

    def test_doc_names_the_target_file_and_the_rollback(self):
        self.assertIn(".gemini/antigravity-cli/settings.json", self.doc)
        self.assertIn("Rollback", self.doc)
        self.assertIn("mcp(event/daily_plan_get)", self.doc)
        self.assertIn("mcp(event/morning_brief_get)", self.doc)


# ---------------------------------------------------------------------------
# Privacy (I2-CONTRACT.md section 0)
# ---------------------------------------------------------------------------

class TestPrivacy(unittest.TestCase):
    OWNED_FILES = (
        os.path.join(SRC_DIR, "planning", "mcp_planning.py"),
        os.path.join(SRC_DIR, "events", "event_mcp.py"),
        ALLOWLIST_SNIPPET,
        ALLOWLIST_DOC,
        os.path.abspath(__file__),
    )

    def test_no_windows_paths_or_home_directories(self):
        # Assembled from fragments so this test file does not match itself.
        home = "/" + "home" + "/"
        bad = re.compile(
            r"[A-Za-z]:\\\\?Users|/Users/[a-z]|" + re.escape(home) + r"(?!<PI_USER>|hermes)"
        )
        for path in self.OWNED_FILES:
            with self.subTest(path=os.path.basename(path)):
                text = open(path, encoding="utf-8").read()
                self.assertIsNone(bad.search(text), path)

    def test_no_ip_literals(self):
        ipv4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
        for path in self.OWNED_FILES:
            if path.endswith("test_mcp_planning.py"):
                continue  # this suite deliberately feeds a fake IP to a stub exception
            with self.subTest(path=os.path.basename(path)):
                text = open(path, encoding="utf-8").read()
                self.assertIsNone(ipv4.search(text), path)


if __name__ == "__main__":
    unittest.main()
