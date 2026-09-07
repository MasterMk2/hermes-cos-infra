"""Phase I.2 integration: the four Wave-1 modules working as one system.

Every other module in `tests-planning/` proves one worker's deliverable against
*stubs* of the other three, because the four were built in parallel against
`I2-CONTRACT.md` rather than against each other. This file is the one that
wires the real modules together:

    isolated fixture DB
      -> planner.build_daily_plan       (T2, real)
      -> renderer.render_brief          (T3, real, template mode)
      -> brief_service.run_scheduled_brief with a fake transport  (T4, real)
      -> event_mcp dispatch             (T5, real)

What it pins that nothing else does:

* **Exactly one delivery**, carrying exactly the text the renderer produced —
  the seam between T3 and T4, where a text transformation would go unnoticed.
* **Phase section 64: canonical truth is never mutated.** Proved three ways —
  the connection the planner receives refuses writes, the database file is
  byte-identical afterwards, and the pipeline still completes when the file
  itself is read-only at the OS level.
* **The MCP tools return the contract shapes with the real planner behind
  them.** `test_mcp_planning.py` dispatches against a stub planner, so the
  compaction whitelist has never met a real plan there.
* **Cross-module config coherence.** `plan_config.DEFAULTS` is the contract's
  single source of truth (section 2); a key a consumer reads, or the shipped
  sample YAML sets, that is absent from `DEFAULTS` is a silent divergence.
* **Fixture shape drift.** `test_renderer_template.make_plan()` is hand-built
  while `planning_fixtures.make_plan_fixture()` runs the real planner. If the
  two shapes drift apart, the renderer suite is testing a plan production never
  produces.

Run with:  python -m pytest tests-planning -q
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "tests-planning"))

import planning_fixtures as pf  # noqa: E402
from planning import (  # noqa: E402
    brief_service,
    brief_state,
    delivery,
    mcp_planning,
    plan_config,
    planner,
    renderer,
)

JST = ZoneInfo("Asia/Tokyo")

#: 08:30 JST on the plan date — the configured `morning_brief.time`, so the
#: late-boot branch is not what is under test here.
NOW = datetime(2026, 9, 8, 8, 30, tzinfo=JST)
PLAN_DATE = "2026-09-08"

#: The four tables phase section 64 names as canonical truth. `events` exists
#: only in the production database; the isolated fixture carries the
#: commitments schema, so the snapshot covers whichever of the four are there.
CANONICAL_TABLES = ("commitments", "commitment_evidence", "commitment_history", "events")

#: Contract section 3, the exact top-level keys of a plan.
CONTRACT_PLAN_KEYS = {
    "date", "timezone", "generated_at", "scheduled", "must_do", "overdue",
    "next_up", "waiting_for", "backlog", "conflicts", "counts",
    "source_freshness", "plan_hash",
}

#: Contract section 3, the `Item` keys. `overdue_days` is present only in
#: `overdue`; `due_precision` and `all_day` are AMENDMENT 1's additions.
CONTRACT_ITEM_KEYS = {
    "commitment_id", "title", "responsibility", "work_state", "due_at",
    "scheduled_at", "scheduled_end_at", "counterparty", "local_time",
    "priority_class", "reason_for_inclusion", "source_types",
}

#: Contract section 3, `reason_for_inclusion` — a closed vocabulary. `overdue_Nd`
#: is a family, matched by pattern.
REASON_LITERALS = {
    "scheduled_today", "due_today", "due_within_72h", "waiting",
    "waiting_followup", "no_date_backlog", "shared_needs_review",
}
REASON_OVERDUE = re.compile(r"^overdue_\d+d$")

CONTRACT_CONFLICT_KINDS = {"overlapping_scheduled", "deadline_during_scheduled", "dense_window"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sha256_file(path) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def sidecar_files(db_path) -> list:
    """Any `-wal` / `-shm` / `-journal` sidecar SQLite left behind.

    A read-only pipeline must create none: their presence would mean a write
    transaction was opened against canonical truth.
    """
    directory = os.path.dirname(db_path)
    base = os.path.basename(db_path)
    return sorted(
        name for name in os.listdir(directory)
        if name.startswith(base + "-")
    )


def canonical_snapshot(db_path) -> dict:
    """Row count and a content hash for each canonical table that exists."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        present = {
            row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        snapshot = {}
        for table in CANONICAL_TABLES:
            if table not in present:
                continue
            rows = con.execute(
                f"SELECT * FROM {table} ORDER BY rowid"  # noqa: S608 — fixed names
            ).fetchall()
            blob = repr(rows).encode("utf-8")
            snapshot[table] = (len(rows), hashlib.sha256(blob).hexdigest())
        assert snapshot, "no canonical table found in the fixture"
        return snapshot
    finally:
        con.close()


class FakeTransport:
    """A `delivery.DeliveryClient` stand-in that records instead of sending.

    Deliberately the *whole* surface `brief_service` uses, so the test breaks
    if that surface grows rather than silently passing a MagicMock.
    """

    def __init__(self, outcome=delivery.DELIVERED):
        self.outcome = outcome
        self.calls = []

    def send_brief(self, *, notification_id, text, reason="morning_brief"):
        self.calls.append(
            {"notification_id": notification_id, "text": text, "reason": reason}
        )
        return delivery.Result(
            self.outcome, http_status=200, delivery_id=f"d{len(self.calls)}"
        )

    def health(self):
        return True


class PipelineTestCase(unittest.TestCase):
    """An isolated fixture DB plus an isolated planning dir, per test."""

    seed = staticmethod(pf.seed_realistic_day)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="i2-integration-")
        self.addCleanup(self._cleanup)
        self.db_path = os.path.join(self.tmp, "fixture-events.db")
        conn = pf.open_db(self.db_path)
        try:
            self.ids = self.seed(conn)
            conn.commit()
        finally:
            conn.close()
        self.planning_dir = os.path.join(self.tmp, "planning")
        os.makedirs(self.planning_dir)
        self.config = self.make_config()

    def _cleanup(self):
        # A test may have made the fixture read-only; restore write before rmtree.
        for root, _dirs, files in os.walk(self.tmp):
            for name in files:
                try:
                    os.chmod(os.path.join(root, name), stat.S_IWRITE | stat.S_IREAD)
                except OSError:
                    pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_config(self) -> dict:
        config = plan_config.load_config(os.path.join(self.tmp, "absent.yaml"))
        config["paths"]["events_db"] = self.db_path
        config["paths"]["planning_dir"] = self.planning_dir
        # Template mode: the scheduled brief ships template-only, and a test
        # must never be able to reach a network no matter how it is configured.
        config["renderer"]["mode"] = "template"
        return config

    def readonly_conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only = ON")
        self.addCleanup(con.close)
        return con

    def build(self, conn=None, **kwargs):
        kwargs.setdefault("date", PLAN_DATE)
        kwargs.setdefault("timezone", "Asia/Tokyo")
        kwargs.setdefault("now", NOW)
        kwargs.setdefault("config", self.config)
        kwargs.setdefault("freshness", pf.freshness_block(NOW))
        return planner.build_daily_plan(conn or self.readonly_conn(), **kwargs)

    def pinned_planner(self):
        """The real `build_daily_plan` with the freshness probe pinned fresh.

        `freshness.collect()` reads the Pi's health files and index databases.
        On any other host every source reports stale, which is correct
        behaviour (section 37: degrade, still deliver) but makes the rendered
        text depend on the machine running the test. Injecting the block keeps
        the planner, the renderer and the service real while making the seam
        under test — does the delivered text equal the rendered text — a
        question about the code rather than about the filesystem.
        """
        block = pf.freshness_block(NOW)

        def build_daily_plan(conn, **kwargs):
            kwargs.setdefault("freshness", block)
            return planner.build_daily_plan(conn, **kwargs)

        return build_daily_plan


# ---------------------------------------------------------------------------
# 1. The whole pipeline, in process
# ---------------------------------------------------------------------------

class EndToEndPipelineTests(PipelineTestCase):
    """Fixture DB -> planner -> renderer -> fake transport, all real modules."""

    def run_pipeline(self, transport=None, **kwargs):
        transport = transport or FakeTransport()
        outcome = brief_service.run_scheduled_brief(
            self.config, now=NOW, trigger="timer", client=transport, **kwargs
        )
        return outcome, transport

    def test_pipeline_delivers_exactly_once(self):
        outcome, transport = self.run_pipeline()
        self.assertEqual(outcome["status"], "delivered", outcome)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(
            transport.calls[0]["notification_id"], f"morning-brief:{PLAN_DATE}"
        )

    def test_delivered_text_is_exactly_what_the_renderer_produced(self):
        """The T3/T4 seam: nothing between the renderer and the socket edits text."""
        expected = renderer.render_brief(
            self.build(), mode="template", config=self.config, now=NOW
        )["text"]
        _outcome, transport = self.run_pipeline(planner=self.pinned_planner())
        self.assertEqual(transport.calls[0]["text"], expected)

    def test_delivered_text_is_a_real_japanese_brief_not_a_placeholder(self):
        _outcome, transport = self.run_pipeline()
        text = transport.calls[0]["text"]
        self.assertTrue(text.strip())
        self.assertLessEqual(len(text), self.config["renderer"]["max_chars"])
        # Section 41's headings, for the categories this fixture populates.
        for heading in ("【今日の予定】", "【最優先】", "【期限超過】", "【返事待ち】"):
            self.assertIn(heading, text)
        # Section 42: the opening line is the deterministic count sentence.
        self.assertRegex(text.split("\n", 1)[0], r"件")

    def test_titles_in_the_brief_come_from_the_database_verbatim(self):
        """No rewriting between the DB row and the delivered text (section 34)."""
        plan = self.build()
        _outcome, transport = self.run_pipeline()
        text = transport.calls[0]["text"]
        shown = [
            item["title"]
            for category in ("scheduled", "must_do")
            for item in plan[category]
        ]
        self.assertTrue(shown, "fixture should populate scheduled/must_do")
        for title in shown:
            self.assertIn(title, text)

    def test_a_second_run_the_same_day_delivers_nothing_more(self):
        """Idempotency holds across the *real* modules, not only the stubs."""
        transport = FakeTransport()
        first, _ = self.run_pipeline(transport=transport)
        second, _ = self.run_pipeline(transport=transport)
        self.assertEqual(first["status"], "delivered")
        self.assertEqual(second["status"], "suppressed_duplicate")
        self.assertEqual(len(transport.calls), 1)

    def test_dry_run_reaches_a_brief_and_delivers_nothing(self):
        outcome, transport = self.run_pipeline(dry_run=True)
        self.assertEqual(outcome["status"], "dry_run")
        self.assertEqual(transport.calls, [])
        self.assertTrue(outcome["text"].strip())

    def test_state_and_health_land_in_the_planning_dir_only(self):
        self.run_pipeline()
        self.assertTrue(os.path.exists(os.path.join(self.planning_dir, "planning.db")))
        health_path = os.path.join(self.planning_dir, "morning-brief-health.json")
        with open(health_path, encoding="utf-8") as handle:
            health = json.load(handle)
        for key in ("last_plan_generated", "last_delivery_attempt",
                    "last_delivery_success", "last_plan_hash", "status",
                    "last_failure_reason", "updated_at"):
            self.assertIn(key, health)
        # `degraded` is the correct status on any host without the Pi's health
        # files: section 37 says a stale source degrades the run and still
        # delivers. The fresh-source case is pinned below.
        self.assertIn(health["status"], ("ok", "degraded"))
        # Nothing was written beside the fixture database.
        self.assertEqual(
            sorted(os.listdir(self.tmp)), ["fixture-events.db", "planning"]
        )

    def test_health_reports_ok_when_every_source_is_fresh(self):
        self.run_pipeline(planner=self.pinned_planner())
        health_path = os.path.join(self.planning_dir, "morning-brief-health.json")
        with open(health_path, encoding="utf-8") as handle:
            health = json.load(handle)
        self.assertEqual(health["status"], "ok")
        self.assertIsNone(health["last_failure_reason"])

    def test_a_stale_source_degrades_the_run_but_still_delivers(self):
        """Section 37: freshness is a warning, never a stop condition."""
        stale = pf.freshness_block(NOW, stale_sources=("line", "gmail"))

        def build_daily_plan(conn, **kwargs):
            kwargs["freshness"] = stale
            return planner.build_daily_plan(conn, **kwargs)

        outcome, transport = self.run_pipeline(planner=build_daily_plan)
        self.assertEqual(outcome["status"], "delivered")
        self.assertEqual(len(transport.calls), 1)
        self.assertIn("data_freshness_warning", outcome["warnings"])
        self.assertEqual(outcome["failure_reason"], "stale_source")

    def test_the_planning_state_db_is_not_the_events_db(self):
        """Section 65: planning state is a separate SQLite file."""
        self.run_pipeline()
        state_db = os.path.join(self.planning_dir, "planning.db")
        con = sqlite3.connect(state_db)
        try:
            tables = {
                row[0] for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            con.close()
        self.assertIn("morning_brief_runs", tables)
        self.assertFalse(tables & set(CANONICAL_TABLES))

    def test_no_raw_source_body_is_stored_in_planning_state(self):
        """Section 66: the hash is stored, never the brief or a message body."""
        self.run_pipeline()
        with open(os.path.join(self.planning_dir, "planning.db"), "rb") as handle:
            blob = handle.read()
        plan = self.build()
        for category in ("scheduled", "must_do", "overdue"):
            for item in plan[category]:
                self.assertNotIn(item["title"].encode("utf-8"), blob)


# ---------------------------------------------------------------------------
# 2. Phase section 64 — canonical truth is never written
# ---------------------------------------------------------------------------

class CanonicalDatabaseIsReadOnlyTests(PipelineTestCase):
    """Three independent proofs, because one of them could be vacuous."""

    def test_the_connection_the_planner_receives_refuses_writes(self):
        con = self.readonly_conn()
        with self.assertRaises(sqlite3.OperationalError):
            con.execute("UPDATE commitments SET title = 'x'")
        with self.assertRaises(sqlite3.OperationalError):
            con.execute("DELETE FROM commitments")
        with self.assertRaises(sqlite3.OperationalError):
            con.execute("CREATE TABLE scratch (a TEXT)")
        # ...and the planner still builds a full plan over it.
        plan = self.build(con)
        self.assertEqual(plan["date"], PLAN_DATE)
        self.assertTrue(plan["counts"]["scheduled"]["total"])

    def test_the_database_file_is_byte_identical_after_a_full_run(self):
        before = sha256_file(self.db_path)
        snapshot_before = canonical_snapshot(self.db_path)
        outcome, transport = (
            brief_service.run_scheduled_brief(
                self.config, now=NOW, trigger="timer", client=FakeTransport()
            ),
            None,
        )
        self.assertEqual(outcome["status"], "delivered", outcome)
        self.assertEqual(sha256_file(self.db_path), before)
        self.assertEqual(canonical_snapshot(self.db_path), snapshot_before)
        self.assertEqual(sidecar_files(self.db_path), [],
                         "a -wal/-journal sidecar means a write transaction was opened")
        del transport

    def test_the_pipeline_runs_against_an_os_level_readonly_file(self):
        """The strongest form: even the OS would refuse a write."""
        os.chmod(self.db_path, stat.S_IREAD)
        try:
            transport = FakeTransport()
            outcome = brief_service.run_scheduled_brief(
                self.config, now=NOW, trigger="timer", client=transport
            )
            self.assertEqual(outcome["status"], "delivered", outcome)
            self.assertEqual(len(transport.calls), 1)
            self.assertTrue(transport.calls[0]["text"].strip())
        finally:
            os.chmod(self.db_path, stat.S_IWRITE | stat.S_IREAD)

    def test_the_mcp_read_path_also_leaves_the_database_untouched(self):
        before = sha256_file(self.db_path)
        out = mcp_planning.daily_plan_get({"date": PLAN_DATE}, db_path=self.db_path)
        self.assertNotIn("error", out)
        brief = mcp_planning.morning_brief_get(
            {"date": PLAN_DATE, "mode": "template"}, db_path=self.db_path
        )
        self.assertNotIn("error", brief)
        self.assertEqual(sha256_file(self.db_path), before)
        self.assertEqual(sidecar_files(self.db_path), [])

    def test_no_planning_module_issues_a_write_statement_against_events_db(self):
        """A tracing proof: every statement the planner runs is a read."""
        executed = []
        con = self.readonly_conn()
        con.set_trace_callback(executed.append)
        self.build(con)
        con.set_trace_callback(None)
        self.assertTrue(executed, "the planner should have queried something")
        for statement in executed:
            head = statement.strip().split(None, 1)[0].upper()
            self.assertIn(
                head, {"SELECT", "PRAGMA", "WITH", "BEGIN", "COMMIT", "ROLLBACK"},
                f"non-read statement against canonical truth: {statement!r}",
            )


# ---------------------------------------------------------------------------
# 3. The MCP boundary, dispatched, with the real planner and renderer behind it
# ---------------------------------------------------------------------------

class McpDispatchContractShapeTests(PipelineTestCase):
    """`test_mcp_planning.py` dispatches against a stub planner; this does not."""

    def call(self, name, arguments=None):
        import test_mcp_planning as t5  # the stub `mcp` modules live there

        class _Params:
            def __init__(self, name, arguments):
                self.name = name
                self.arguments = arguments

        with t5.load_event_mcp(self.db_path) as event_mcp:
            result = asyncio.run(event_mcp._call_tool(None, _Params(name, arguments)))
        return json.loads(result.content[0].text)

    def test_daily_plan_get_returns_the_contract_shape(self):
        out = self.call("daily_plan_get", {"date": PLAN_DATE})
        self.assertNotIn("error", out)
        # Section 47: compacted for an LLM context — `plan_hash` dropped,
        # `counts` and `source_freshness` kept.
        self.assertNotIn("plan_hash", out)
        self.assertIn("counts", out)
        self.assertIn("source_freshness", out)
        self.assertEqual(out["date"], PLAN_DATE)
        self.assertEqual(out["timezone"], "Asia/Tokyo")
        for category in ("scheduled", "must_do", "overdue", "next_up",
                         "waiting_for", "backlog", "conflicts"):
            self.assertIsInstance(out[category], list, category)
            self.assertIn(category, out["counts"])
            self.assertEqual(
                out["counts"][category]["shown"], len(out[category]), category
            )
            self.assertGreaterEqual(
                out["counts"][category]["total"], out["counts"][category]["shown"]
            )

    def test_dispatched_items_carry_the_contract_item_keys(self):
        out = self.call("daily_plan_get", {"date": PLAN_DATE})
        seen = 0
        for category in ("scheduled", "must_do", "overdue", "next_up",
                         "waiting_for", "backlog"):
            for item in out[category]:
                seen += 1
                self.assertLessEqual(CONTRACT_ITEM_KEYS, set(item), category)
                reason = item["reason_for_inclusion"]
                self.assertTrue(
                    reason in REASON_LITERALS or REASON_OVERDUE.match(reason),
                    f"{reason!r} is outside the closed vocabulary of section 35",
                )
                if category == "overdue":
                    self.assertIsInstance(item["overdue_days"], int)
        self.assertGreater(seen, 0, "the fixture should produce displayable items")

    def test_dispatched_conflicts_use_the_closed_kind_vocabulary(self):
        out = self.call("daily_plan_get", {"date": PLAN_DATE})
        self.assertTrue(out["conflicts"], "the fixture seeds an overlap")
        for conflict in out["conflicts"]:
            self.assertIn(conflict["kind"], CONTRACT_CONFLICT_KINDS)
            self.assertEqual(sorted(conflict["commitment_ids"]),
                             conflict["commitment_ids"])
            self.assertTrue(str(conflict["detail"]).strip())

    def test_morning_brief_get_returns_the_contract_shape(self):
        out = self.call("morning_brief_get", {"date": PLAN_DATE, "mode": "template"})
        self.assertNotIn("error", out)
        self.assertEqual(sorted(out),
                         ["brief_text", "counts", "date", "renderer", "timezone"])
        self.assertEqual(out["renderer"], "template")
        self.assertTrue(out["brief_text"].strip())
        self.assertEqual(out["date"], PLAN_DATE)

    def test_dispatch_does_not_transform_what_the_adapter_returned(self):
        """`event_mcp` is a router: the direct call and the dispatched call agree."""
        args = {"date": PLAN_DATE, "mode": "template"}
        direct = mcp_planning.morning_brief_get(dict(args), db_path=self.db_path)
        dispatched = self.call("morning_brief_get", dict(args))
        self.assertNotIn("error", direct)
        self.assertEqual(dispatched, direct)

    def test_the_dispatched_brief_is_rendered_from_this_database(self):
        """The real planner and the real renderer, not a stub, are behind it."""
        plan = self.build()
        out = self.call("morning_brief_get", {"date": PLAN_DATE, "mode": "template"})
        titles = [item["title"] for item in plan["scheduled"] + plan["must_do"]]
        self.assertTrue(titles)
        for title in titles:
            self.assertIn(title, out["brief_text"])
        # And it is the deterministic template renderer that produced it.
        self.assertEqual(out["renderer"], "template")
        self.assertEqual(
            out["brief_text"].split("\n", 1)[0],
            renderer.render_brief(
                mcp_planning.compact_plan(plan), mode="template"
            )["text"].split("\n", 1)[0],
        )

    def test_both_tools_reject_a_bad_date_without_a_traceback(self):
        for name in ("daily_plan_get", "morning_brief_get"):
            out = self.call(name, {"date": "2026-13-45"})
            self.assertEqual(out.get("error"), "invalid_date", name)

    def test_a_phase_i1_tool_still_dispatches_after_the_merge(self):
        out = self.call("commitment_status", {})
        self.assertNotIn("unknown_tool", json.dumps(out))


# ---------------------------------------------------------------------------
# 4. Cross-module coherence: config keys, and the two coordinator rulings
# ---------------------------------------------------------------------------

def flatten_keys(mapping, prefix=""):
    keys = set()
    for key, value in mapping.items():
        path = f"{prefix}{key}"
        keys.add(path)
        if isinstance(value, dict):
            keys |= flatten_keys(value, path + ".")
    return keys


class CrossModuleConfigCoherenceTests(unittest.TestCase):
    """Contract section 2: `plan_config` is what *everyone* reads.

    A key a consumer reads but `DEFAULTS` does not define still "works" —
    the consumer falls back to a literal of its own — which is exactly why the
    divergence survives review. It means `load_config()` on a host with no
    config file returns a config the sample YAML documents but the code has
    never seen, and the two defaults can drift apart silently.
    """

    src = REPO_ROOT / "src" / "planning"

    def test_every_dotted_config_key_a_module_reads_exists_in_defaults(self):
        defaults = flatten_keys(plan_config.DEFAULTS)
        pattern = re.compile(r'_get\(\s*config\s*,\s*"([a-z_]+(?:\.[a-z_]+)*)"')
        missing = {}
        for path in sorted(self.src.glob("*.py")):
            for key in pattern.findall(path.read_text(encoding="utf-8")):
                if key not in defaults:
                    missing.setdefault(key, []).append(path.name)
        self.assertEqual(missing, {})

    def test_the_sample_yaml_and_defaults_describe_the_same_keys(self):
        sample = self.load_sample()
        defaults = flatten_keys(plan_config.DEFAULTS)
        keys = flatten_keys(sample)
        self.assertEqual(sorted(keys - defaults), [],
                         "the sample sets a key DEFAULTS does not define")
        # `renderer.llm` carries commented-out optional sub-keys; only the
        # branch the sample actually sets has to be mirrored.
        self.assertEqual(
            sorted(k for k in defaults - keys if not k.startswith("renderer.llm.")),
            [], "DEFAULTS defines a key the shipped sample never mentions")

    def test_the_sample_yaml_ships_the_llm_renderer_disabled(self):
        """Coordinator ruling on T3: the scheduled brief is template-only.

        T4 finalised this file before the ruling landed, so it is checked here
        rather than assumed.
        """
        sample = self.load_sample()
        self.assertIs(sample["renderer"]["llm"]["enabled"], False)
        self.assertIs(plan_config.DEFAULTS["renderer"]["llm"]["enabled"], False)

    def test_an_llm_client_is_not_resolvable_from_the_shipped_defaults(self):
        """The ruling, enforced at the code seam rather than in the YAML."""
        self.assertIsNone(renderer.resolve_client(plan_config.DEFAULTS["renderer"]))
        self.assertIsNone(renderer.resolve_client(self.load_sample()["renderer"]))
        result = renderer.render_brief(
            pf.make_plan_fixture(), mode="auto", config=plan_config.DEFAULTS
        )
        self.assertEqual(result["renderer"], "template")
        self.assertEqual(result["fallback_reason"], "llm_unavailable")

    def test_the_service_unit_grants_no_network_by_default(self):
        """`RestrictAddressFamilies=AF_UNIX` unless the operator opts in."""
        installer = (self.src / "install-root.sh").read_text(encoding="utf-8")
        self.assertIn('ADDRESS_FAMILIES="AF_UNIX"', installer)
        unit = (self.src / "systemd" / "hermes-morning-brief.service.in").read_text(
            encoding="utf-8"
        )
        self.assertIn("RestrictAddressFamilies=@RENDERER_ADDRESS_FAMILIES@", unit)
        # The template must not hard-code an inet family anywhere active.
        active = [
            line for line in unit.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertNotIn("AF_INET", "\n".join(active))

    def test_the_timer_time_comes_from_the_config_and_nowhere_else(self):
        """Section 53: one source of truth for the morning time."""
        timer = (self.src / "systemd" / "hermes-morning-brief.timer.in").read_text(
            encoding="utf-8"
        )
        self.assertIn("@MORNING_TIME@", timer)
        self.assertNotRegex(
            "\n".join(
                line for line in timer.splitlines()
                if line.startswith("OnCalendar")
            ),
            r"\d{2}:\d{2}",
        )
        self.assertEqual(self.load_sample()["morning_brief"]["time"],
                         plan_config.DEFAULTS["morning_brief"]["time"])

    def load_sample(self) -> dict:
        path = self.src / "config" / "planning.sample.yaml"
        text = path.read_text(encoding="utf-8")
        try:
            import yaml
        except ImportError:  # pragma: no cover - PyYAML is present on both hosts
            self.skipTest("PyYAML is unavailable")
        loaded = yaml.safe_load(text)
        self.assertIsInstance(loaded, dict)
        return loaded


# ---------------------------------------------------------------------------
# 5. Fixture shape drift between the two plan builders
# ---------------------------------------------------------------------------

class FixtureShapeDoesNotDriftTests(unittest.TestCase):
    """`test_renderer_template.make_plan()` is hand-built; the planner is not.

    Two builders for the same structure is a real hazard: the hand-built one
    exists so the renderer can be driven with plans the planner would never
    emit (a hostile model answer, a 47-item backlog), and it must keep that
    freedom — but its *shape* has to stay the shape production produces, or the
    renderer suite is green against a plan that cannot occur.
    """

    def setUp(self):
        import test_renderer_template as trt

        self.trt = trt
        self.real = pf.make_plan_fixture()
        self.hand = trt.make_plan(must_do=[trt.make_item()])

    def test_the_hand_built_plan_has_no_key_the_planner_never_emits(self):
        self.assertEqual(sorted(set(self.hand) - set(self.real)), [])

    def test_the_hand_built_plan_omits_only_documented_optional_keys(self):
        missing = set(self.real) - set(self.hand)
        # `source_freshness` is deliberately optional in the hand builder: the
        # renderer must tolerate a plan without it, and `busy_day_plan()`
        # covers the present case. `warnings` is omitted when empty (section 3).
        self.assertLessEqual(missing, {"source_freshness", "warnings"})

    def test_the_hand_built_item_matches_the_planner_item_shape(self):
        real_item = self.real["must_do"][0]
        hand_item = self.hand["must_do"][0]
        self.assertEqual(sorted(hand_item), sorted(real_item))

    def test_the_hand_built_counts_match_the_planner_counts_shape(self):
        self.assertEqual(sorted(self.hand["counts"]), sorted(self.real["counts"]))

    def test_both_builders_satisfy_the_contract_top_level_keys(self):
        self.assertLessEqual(CONTRACT_PLAN_KEYS, set(self.real))

    def test_the_renderer_accepts_both_builders(self):
        for name, plan in (("planner", self.real), ("hand", self.hand)):
            result = renderer.render_brief(plan, mode="template", now=NOW)
            self.assertEqual(result["renderer"], "template", name)
            self.assertTrue(result["text"].strip(), name)


# ---------------------------------------------------------------------------
# 6. Performance, against a production-sized fixture (phase section 74)
# ---------------------------------------------------------------------------

class ProductionSizedPerformanceTests(unittest.TestCase):
    """Section 74: the deterministic build is preferred under 500 ms.

    `test_planner_performance.py` (T2) already bounds the planner alone. This
    bounds the *pipeline* — plan plus render — because that is what the timer
    actually runs, and records the renderer's own cost separately.
    """

    budget_ms = 500.0

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="i2-perf-")
        cls.db_path = os.path.join(cls.tmp, "perf-events.db")
        conn = pf.open_db(cls.db_path)
        try:
            cls.rows = pf.seed_production_sized(conn)
            conn.commit()
        finally:
            conn.close()
        cls.conn = sqlite3.connect(f"file:{cls.db_path}?mode=ro", uri=True)
        cls.conn.row_factory = sqlite3.Row
        cls.conn.execute("PRAGMA query_only = ON")

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def build(self):
        return planner.build_daily_plan(
            self.conn, date=PLAN_DATE, timezone="Asia/Tokyo", now=NOW,
            config=plan_config.DEFAULTS, freshness=pf.freshness_block(NOW),
        )

    def median_ms(self, fn, runs=15):
        import statistics
        import time

        samples = []
        for _ in range(runs):
            start = time.perf_counter()
            fn()
            samples.append((time.perf_counter() - start) * 1000.0)
        return statistics.median(samples)

    def test_the_fixture_really_is_production_sized(self):
        self.assertGreaterEqual(self.rows, 700)

    def test_plan_and_render_together_stay_inside_the_section_74_budget(self):
        plan = self.build()
        elapsed = self.median_ms(
            lambda: renderer.render_brief(self.build(), mode="template", now=NOW)
        )
        self.assertLess(elapsed, self.budget_ms,
                        f"plan+render median {elapsed:.1f} ms over {self.rows} rows")
        self.assertTrue(plan["counts"]["overdue"]["total"] >= 0)

    def test_the_template_renderer_is_a_rounding_error_next_to_the_planner(self):
        plan = self.build()
        render_ms = self.median_ms(
            lambda: renderer.render_brief(plan, mode="template", now=NOW)
        )
        self.assertLess(render_ms, 50.0, f"template render median {render_ms:.2f} ms")


# ---------------------------------------------------------------------------
# 7. Phase section 75 — the deterministic core needs no LLM at all
# ---------------------------------------------------------------------------

class DeterministicCoreIsLlmFreeTests(unittest.TestCase):
    """A daily plan must survive every model in the fallback chain being down."""

    core = ("plan_config.py", "plan_models.py", "freshness.py", "planner.py")
    forbidden = re.compile(
        r"\b(?:openai|anthropic|gemini|antigravity|urllib\.request|http\.client|requests)\b",
        re.IGNORECASE,
    )

    def test_no_core_module_references_a_provider_or_a_network_client(self):
        base = REPO_ROOT / "src" / "planning"
        for name in self.core:
            text = (base / name).read_text(encoding="utf-8")
            code = "\n".join(
                line for line in text.splitlines()
                if not line.lstrip().startswith("#")
            )
            hit = self.forbidden.search(code)
            self.assertIsNone(hit, f"{name} references {hit.group(0) if hit else ''}")

    def test_the_planner_builds_with_every_renderer_module_unimportable(self):
        """Sever `planning.renderer` and the plan is still produced."""
        saved = sys.modules.pop("planning.renderer", None)
        sys.modules["planning.renderer"] = None  # poisons `import planning.renderer`
        try:
            conn = pf.open_db()
            try:
                pf.seed_realistic_day(conn)
                plan = pf.build_plan(conn, date=PLAN_DATE, now=NOW)
            finally:
                conn.close()
            self.assertEqual(plan["date"], PLAN_DATE)
            self.assertIn("plan_hash", plan)
        finally:
            if saved is not None:
                sys.modules["planning.renderer"] = saved
            else:
                sys.modules.pop("planning.renderer", None)


# ---------------------------------------------------------------------------
# 8. The MCP deployment set — what actually has to ship next to event_mcp.py
# ---------------------------------------------------------------------------

class McpDeploymentSetTests(unittest.TestCase):
    """`src/antigravity-bridge/config/mcp-allowlist-i2.md` names six modules.

    That list is a root `install` command, so getting it wrong half-lands the
    deployment: too few modules and `daily_plan_get` answers
    `planning_module_unavailable` forever, too many and a Discord delivery
    client lands inside the constrained agent's import path. This test is what
    keeps the document honest, by staging exactly those six in a directory of
    their own and driving both tools through them.
    """

    #: The modules the deployment document installs, and nothing else.
    DEPLOYED = (
        "__init__.py", "mcp_planning.py", "planner.py", "renderer.py",
        "plan_models.py", "plan_config.py", "freshness.py",
    )
    #: The scheduled-brief half, which must NOT reach the agent.
    WITHHELD = ("planning.delivery", "planning.brief_state",
                "planning.brief_service", "planning.cli")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="i2-deploy-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        staged = os.path.join(self.tmp, "planning")
        os.makedirs(staged)
        for name in self.DEPLOYED:
            shutil.copy2(REPO_ROOT / "src" / "planning" / name, staged)
        self.db_path = os.path.join(self.tmp, "fixture-events.db")
        conn = pf.open_db(self.db_path)
        try:
            pf.seed_realistic_day(conn)
            conn.commit()
        finally:
            conn.close()

    def staged_import(self):
        """Import `planning.mcp_planning` resolving ONLY through the staging dir."""
        saved_path = list(sys.path)
        saved_modules = {
            name: module for name, module in sys.modules.items()
            if name == "planning" or name.startswith("planning.")
        }
        for name in saved_modules:
            del sys.modules[name]
        sys.path = [self.tmp] + [
            entry for entry in sys.path
            if os.path.abspath(entry) != os.path.abspath(REPO_ROOT / "src")
        ]

        def restore():
            sys.path[:] = saved_path
            for name in [
                n for n in sys.modules
                if n == "planning" or n.startswith("planning.")
            ]:
                del sys.modules[name]
            sys.modules.update(saved_modules)

        self.addCleanup(restore)
        import importlib

        module = importlib.import_module("planning.mcp_planning")
        self.assertTrue(
            os.path.abspath(module.__file__).startswith(os.path.abspath(self.tmp)),
            f"resolved the repo copy, not the staged one: {module.__file__}",
        )
        return module

    def test_the_documented_module_set_is_sufficient(self):
        staged = self.staged_import()
        plan = staged.daily_plan_get({"date": PLAN_DATE}, db_path=self.db_path)
        self.assertNotIn("error", plan)
        self.assertIn("counts", plan)
        brief = staged.morning_brief_get(
            {"date": PLAN_DATE, "mode": "template"}, db_path=self.db_path
        )
        self.assertNotIn("error", brief)
        self.assertTrue(brief["brief_text"].strip())

    def test_the_documented_module_set_is_minimal(self):
        """No delivery client, no state store, no service reaches the agent."""
        staged = self.staged_import()
        staged.morning_brief_get(
            {"date": PLAN_DATE, "mode": "template"}, db_path=self.db_path
        )
        for name in self.WITHHELD:
            self.assertNotIn(name, sys.modules)
        loaded = sorted(
            name for name in sys.modules
            if name.startswith("planning.")
        )
        self.assertEqual(
            loaded,
            ["planning.freshness", "planning.mcp_planning", "planning.plan_config",
             "planning.plan_models", "planning.planner", "planning.renderer"],
        )

    def deployment_doc(self) -> str:
        path = (REPO_ROOT / "src" / "antigravity-bridge" / "config"
                / "mcp-allowlist-i2.md")
        if not path.exists():
            # A staged partial copy of the repo (how these suites are run on
            # the Pi) legitimately has no `src/antigravity-bridge/`.
            self.skipTest("the deployment document is not in this checkout")
        return path.read_text(encoding="utf-8")

    def test_the_deployment_document_lists_exactly_these_modules(self):
        doc = self.deployment_doc()
        for name in self.DEPLOYED:
            if name == "__init__.py":
                continue  # the document calls it optional, and it is
            self.assertIn(name, doc, f"{name} is not named in the install command")
        for module in ("brief_service.py", "brief_state.py", "delivery.py"):
            # Named only in the prose that forbids them, never in an install line.
            for line in doc.splitlines():
                if module in line and line.lstrip().startswith("sudo install"):
                    self.fail(f"{module} appears in an install command: {line!r}")

    def test_the_document_targets_both_live_event_mcp_copies(self):
        """AMENDMENT 2: Antigravity and the Hermes container are both consumers."""
        doc = self.deployment_doc()
        self.assertIn("/srv/hermes/antigravity/mcp/event/planning", doc)
        self.assertIn("/srv/hermes/hermes-data/tools/events/planning", doc)


# ---------------------------------------------------------------------------
# 9. Phase section 113 — the classifier broker is untouched
# ---------------------------------------------------------------------------

class FrozenModulesAreUntouchedTests(unittest.TestCase):
    """Phase section 3 freezes I.1; section 113 names the broker specifically."""

    frozen = (
        "src/commitments", "src/attention", "src/channel-gateway",
        "src/calendar", "src/gmail", "src/line-knowledge",
    )

    def test_phase_i2_changed_no_frozen_module(self):
        import subprocess

        try:
            changed = subprocess.run(
                ["git", "diff", "--name-only", "a9e849d..HEAD"],
                cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60,
            )
        except (OSError, subprocess.SubprocessError):  # pragma: no cover
            self.skipTest("git is unavailable")
        if changed.returncode != 0:  # pragma: no cover - shallow clone, worktree
            self.skipTest("the pre-I.2 commit is not reachable here")
        offenders = [
            name for name in changed.stdout.split()
            if any(name.startswith(prefix + "/") for prefix in self.frozen)
        ]
        self.assertEqual(offenders, [])

    def test_the_classifier_broker_still_imports(self):
        broker = REPO_ROOT / "src" / "line-knowledge" / "event_broker.py"
        if not broker.exists():  # pragma: no cover
            self.skipTest("the broker is not present in this checkout")
        import importlib.util

        spec = importlib.util.spec_from_file_location("_i2_broker_probe", broker)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(hasattr(module, "__doc__"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
