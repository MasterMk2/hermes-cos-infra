"""Source freshness and the stale-source warning (phase sections 36, 37, 91).

Two properties matter here and they pull in opposite directions:

* the plan must **tell** the reader when it is built on stale data, and
* a stale or unreadable source must **not** take the morning brief down.

So `collect` never raises — every probe is exercised below against a missing
file, a malformed file and a database without the expected table — and the
planner turns any stale entry into a warning while still producing the plan.

Every path used here is a temporary directory. No production health surface is
read by any test in this file.
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import planning_fixtures as fx  # noqa: E402

from planning import freshness, plan_config  # noqa: E402
from planning.plan_models import WARNING_DATA_FRESHNESS  # noqa: E402
from planning.planner import build_daily_plan  # noqa: E402

ENTRY_KEYS = {"last_success_at", "age_seconds", "stale", "threshold_seconds"}


def iso(dt):
    return dt.isoformat()


def run_sql(path, *statements):
    """Run statements against `path` and close the handle afterwards.

    `sqlite3.connect` used as a context manager commits but does not close, and
    a still-open handle makes the temporary directory undeletable on Windows.
    """
    conn = sqlite3.connect(path)
    try:
        for statement in statements:
            if isinstance(statement, tuple):
                conn.execute(statement[0], statement[1])
            else:
                conn.execute(statement)
        conn.commit()
    finally:
        conn.close()


class SourceDirectory:
    """A throwaway directory holding one copy of each health surface."""

    def __init__(self, root, timestamp):
        self.root = root
        self.timestamp = timestamp
        self.calendar_db = os.path.join(root, "calendar-index.db")
        self.gmail_json = os.path.join(root, "gmail-status.json")
        self.gmail_db = os.path.join(root, "gmail-index.db")
        self.line_json = os.path.join(root, "line-reconcile-health.json")
        self.line_db = os.path.join(root, "line-index.db")
        self.discord_db = os.path.join(root, "discord-messages.db")
        self.events_db = os.path.join(root, "events.db")

    def write_all(self):
        stamp = iso(self.timestamp)
        run_sql(
            self.calendar_db,
            "CREATE TABLE calendar_status (last_api_success_at TEXT)",
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)",
            ("INSERT INTO calendar_status VALUES (?)", (stamp,)),
        )
        with open(self.gmail_json, "w", encoding="utf-8") as handle:
            json.dump({"auth_state": "AUTHORIZED", "last_api_success_at": stamp}, handle)
        with open(self.line_json, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "status": "OK",
                    "index_timestamp": stamp,
                    "last_finished_at": stamp,
                    "last_commitment_reconcile": stamp,
                },
                handle,
            )
        run_sql(
            self.discord_db,
            "CREATE TABLE ingest_state (key TEXT PRIMARY KEY, value TEXT)",
            ("INSERT INTO ingest_state VALUES ('updated_at', ?)", (stamp,)),
        )
        run_sql(
            self.events_db,
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)",
            ("INSERT INTO meta VALUES ('last_run', ?)", (stamp,)),
            # The Discord classifier pass writes this at the end of every run.
            # It is `_probe_discord`'s fallback, and in production it is the
            # only Discord signal `hermesplanner` can read at all.
            ("INSERT INTO meta VALUES ('discord_last_run', ?)", (stamp,)),
        )
        return self

    def config(self, **overrides):
        sources = {
            "calendar_index_db": self.calendar_db,
            "gmail_status_json": self.gmail_json,
            "gmail_index_db": self.gmail_db,
            "line_reconcile_health_json": self.line_json,
            "line_index_db": self.line_db,
            "discord_collector_db": self.discord_db,
        }
        sources.update(overrides)
        return {"paths": {"events_db": self.events_db}, "freshness_sources": sources}


class CollectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = datetime(2026, 9, 8, 8, 30, tzinfo=timezone.utc)

    def sources(self, age=timedelta(minutes=10)):
        return SourceDirectory(self.tmp.name, self.now - age).write_all()

    def test_all_five_sources_are_reported_fresh(self):
        result = freshness.collect(self.sources().config(), now=self.now)
        self.assertEqual(list(result), list(freshness.SOURCE_ORDER))
        for name, entry in result.items():
            self.assertTrue(ENTRY_KEYS.issubset(entry), name)
            self.assertFalse(entry["stale"], name)
            self.assertEqual(entry["age_seconds"], 600, name)
            self.assertNotIn("error", entry, name)
        self.assertFalse(freshness.any_stale(result))

    def test_an_old_source_is_stale_per_its_own_threshold(self):
        """25 hours old: past the 24h calendar/gmail/commitments bar, still
        inside the 48h LINE/Discord bar the attention engine already uses."""
        result = freshness.collect(
            self.sources(age=timedelta(hours=25)).config(), now=self.now
        )
        self.assertTrue(result["calendar"]["stale"])
        self.assertTrue(result["gmail"]["stale"])
        self.assertTrue(result["commitments"]["stale"])
        self.assertFalse(result["line"]["stale"])
        self.assertFalse(result["discord"]["stale"])
        self.assertTrue(freshness.any_stale(result))

    def test_thresholds_come_from_config(self):
        config = self.sources(age=timedelta(hours=2)).config()
        config["freshness_thresholds_seconds"] = {"calendar": 60}
        result = freshness.collect(config, now=self.now)
        self.assertEqual(result["calendar"]["threshold_seconds"], 60)
        self.assertTrue(result["calendar"]["stale"])

    def test_a_missing_source_is_stale_with_a_short_reason(self):
        config = self.sources().config()
        os.remove(config["freshness_sources"]["calendar_index_db"])
        result = freshness.collect(config, now=self.now)
        self.assertEqual(
            result["calendar"],
            {
                "last_success_at": None,
                "age_seconds": None,
                "stale": True,
                "threshold_seconds": 86400,
                "error": "source_missing",
            },
        )
        # Every other source is unaffected.
        self.assertFalse(result["discord"]["stale"])

    def test_an_unconfigured_source_is_disabled_not_invented(self):
        """With no events.db either, an unconfigured source stays disabled."""
        config = self.sources().config(discord_collector_db="")
        config["paths"]["events_db"] = ""
        result = freshness.collect(config, now=self.now)
        self.assertTrue(result["discord"]["stale"])
        self.assertEqual(result["discord"]["error"], "source_missing")

    def test_an_unreadable_collector_db_falls_back_to_events_db(self):
        """The production case, and the reason the fallback exists.

        `/srv/hermes/knowledge/discord` is mode 2770 owned by the operator's
        login group and `hermesplanner` is deliberately not in it
        (I2-AMENDMENT G5), so the directory is not even traversable:
        `os.path.exists` is False and the primary probe cannot run. Before the
        fallback this made `data_freshness_warning` permanent and printed
        「情報源の更新が止まっています: discord」 in every single brief, while
        the Discord pipeline was in fact minutes fresh.
        """
        config = self.sources().config(
            discord_collector_db=os.path.join(self.tmp.name, "no-such-dir", "messages.db")
        )
        result = freshness.collect(config, now=self.now)
        self.assertFalse(result["discord"]["stale"])
        self.assertEqual(result["discord"]["age_seconds"], 600)
        self.assertNotIn("error", result["discord"])
        self.assertFalse(freshness.any_stale(result))

    def test_discord_is_stale_when_both_the_collector_and_events_db_are_gone(self):
        config = self.sources().config(
            discord_collector_db=os.path.join(self.tmp.name, "no-such-dir", "messages.db")
        )
        config["paths"]["events_db"] = ""
        result = freshness.collect(config, now=self.now)
        self.assertTrue(result["discord"]["stale"])
        self.assertEqual(result["discord"]["error"], "source_missing")

    def test_the_collector_heartbeat_still_wins_when_it_is_readable(self):
        """The fallback must not shadow the better signal where it exists."""
        fixture = self.sources()
        run_sql(
            fixture.discord_db,
            ("UPDATE ingest_state SET value = ? WHERE key = 'updated_at'",
             (iso(self.now - timedelta(hours=40)),)),
        )
        result = freshness.collect(fixture.config(), now=self.now)
        self.assertEqual(result["discord"]["age_seconds"], 40 * 3600)
        self.assertFalse(result["discord"]["stale"])  # inside the 48h Discord bar

    def test_a_malformed_json_source_never_raises(self):
        config = self.sources().config()
        with open(config["freshness_sources"]["gmail_status_json"], "w", encoding="utf-8") as h:
            h.write("{ this is not json")
        result = freshness.collect(config, now=self.now)
        self.assertTrue(result["gmail"]["stale"])
        self.assertIn("error", result["gmail"])

    def test_a_database_without_the_expected_table_never_raises(self):
        """A collector db missing `ingest_state` falls through, it does not raise.

        With no events.db behind it there is nothing left to fall back to, so
        the sqlite failure is what surfaces — which is what makes the reason
        diagnosable rather than a bare `source_missing`.
        """
        config = self.sources().config()
        os.remove(config["freshness_sources"]["discord_collector_db"])
        run_sql(
            config["freshness_sources"]["discord_collector_db"],
            "CREATE TABLE unrelated (x TEXT)",
        )
        config["paths"]["events_db"] = ""
        result = freshness.collect(config, now=self.now)
        self.assertTrue(result["discord"]["stale"])
        self.assertTrue(result["discord"]["error"].startswith("sqlite_error"))

    def test_a_database_without_the_expected_table_falls_back_when_it_can(self):
        config = self.sources().config()
        os.remove(config["freshness_sources"]["discord_collector_db"])
        run_sql(
            config["freshness_sources"]["discord_collector_db"],
            "CREATE TABLE unrelated (x TEXT)",
        )
        result = freshness.collect(config, now=self.now)
        self.assertFalse(result["discord"]["stale"])
        self.assertEqual(result["discord"]["age_seconds"], 600)

    def test_an_unparsable_timestamp_is_stale_rather_than_fatal(self):
        config = self.sources().config()
        run_sql(
            config["freshness_sources"]["discord_collector_db"],
            "UPDATE ingest_state SET value = 'never' WHERE key = 'updated_at'",
        )
        result = freshness.collect(config, now=self.now)
        self.assertEqual(result["discord"]["error"], "unparsable_timestamp")
        self.assertTrue(result["discord"]["stale"])

    def test_collect_survives_an_entirely_empty_config(self):
        result = freshness.collect({"freshness_sources": {k: "" for k in (
            "calendar_index_db", "gmail_status_json", "gmail_index_db",
            "line_reconcile_health_json", "line_index_db", "discord_collector_db",
        )}, "paths": {"events_db": ""}}, now=self.now)
        self.assertEqual(len(result), 5)
        self.assertTrue(all(entry["stale"] for entry in result.values()))

    def test_the_gmail_index_db_is_the_documented_fallback(self):
        source = SourceDirectory(self.tmp.name, self.now - timedelta(minutes=10)).write_all()
        os.remove(source.gmail_json)
        run_sql(
            source.gmail_db,
            "CREATE TABLE gmail_status (singleton INTEGER, last_api_success_at TEXT)",
            ("INSERT INTO gmail_status VALUES (1, ?)", (iso(source.timestamp),)),
        )
        result = freshness.collect(source.config(), now=self.now)
        self.assertFalse(result["gmail"]["stale"])

    def test_a_future_timestamp_is_zero_age_not_negative(self):
        source = SourceDirectory(self.tmp.name, self.now + timedelta(hours=5)).write_all()
        result = freshness.collect(source.config(), now=self.now)
        self.assertEqual(result["calendar"]["age_seconds"], 0)
        self.assertFalse(result["calendar"]["stale"])

    def test_an_error_reason_never_leaks_a_full_path(self):
        config = self.sources().config()
        os.remove(config["freshness_sources"]["calendar_index_db"])
        entry = freshness.collect(config, now=self.now)["calendar"]
        self.assertNotIn(self.tmp.name, entry["error"])
        self.assertNotIn("/", entry["error"])


class StaleWarningTest(unittest.TestCase):
    """Phase sections 37 and 91: warn, do not fail."""

    def setUp(self):
        self.conn = fx.open_db()
        fx.reset_ids()
        fx.seed_realistic_day(self.conn)
        self.addCleanup(self.conn.close)

    def test_a_stale_source_warns_but_the_plan_is_still_produced(self):
        plan = fx.build_plan(self.conn, freshness=fx.freshness_block(stale_sources=("line",)))
        self.assertEqual(plan["warnings"], [WARNING_DATA_FRESHNESS])
        self.assertTrue(plan["must_do"])
        self.assertTrue(plan["scheduled"])
        self.assertTrue(plan["source_freshness"]["line"]["stale"])

    def test_an_unreadable_source_also_warns(self):
        plan = fx.build_plan(
            self.conn, freshness=fx.freshness_block(unreadable_sources=("calendar",))
        )
        self.assertEqual(plan["warnings"], [WARNING_DATA_FRESHNESS])
        self.assertEqual(plan["source_freshness"]["calendar"]["error"], "source_missing")

    def test_no_warning_key_at_all_when_every_source_is_fresh(self):
        plan = fx.build_plan(self.conn)
        self.assertNotIn("warnings", plan)

    def test_the_freshness_block_is_carried_through_verbatim(self):
        block = fx.freshness_block(stale_sources=("discord", "gmail"))
        plan = fx.build_plan(self.conn, freshness=block)
        self.assertEqual(plan["source_freshness"], block)

    def test_freshness_is_collected_when_not_injected(self):
        """With no health surfaces on this machine, collection still returns a
        complete block and the plan is still built."""
        plan = build_daily_plan(
            self.conn,
            date=fx.DEFAULT_PLAN_DATE,
            now=fx.DEFAULT_NOW,
            config={
                "paths": {"events_db": ""},
                "freshness_sources": {k: "" for k in plan_config.DEFAULTS["freshness_sources"]},
            },
        )
        self.assertEqual(set(plan["source_freshness"]), set(freshness.SOURCE_ORDER))
        self.assertEqual(plan["warnings"], [WARNING_DATA_FRESHNESS])
        self.assertTrue(plan["must_do"])


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def path(self, name, content):
        full = os.path.join(self.tmp.name, name)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(content)
        return full

    def test_a_missing_file_yields_the_defaults(self):
        loaded = plan_config.load_config(os.path.join(self.tmp.name, "absent.yaml"))
        self.assertEqual(loaded, plan_config.DEFAULTS)

    def test_overrides_are_deep_merged(self):
        path = self.path("config.yaml", "limits:\n  backlog: 9\nmorning_brief:\n  time: '07:15'\n")
        loaded = plan_config.load_config(path)
        self.assertEqual(loaded["limits"]["backlog"], 9)
        self.assertEqual(loaded["limits"]["must_do"], plan_config.DEFAULTS["limits"]["must_do"])
        self.assertEqual(loaded["morning_brief"]["time"], "07:15")
        self.assertTrue(loaded["morning_brief"]["enabled"])

    def test_malformed_content_raises_value_error(self):
        path = self.path("bad.yaml", "just a bare string, not a mapping\n")
        with self.assertRaises(ValueError):
            plan_config.load_config(path)
        self.assertTrue(issubclass(plan_config.ConfigError, ValueError))

    def test_an_empty_file_means_no_overrides(self):
        self.assertEqual(plan_config.load_config(self.path("empty.yaml", "")), plan_config.DEFAULTS)

    def test_the_env_override_is_honoured(self):
        path = self.path("env.yaml", "limits:\n  backlog: 4\n")
        previous = os.environ.get(plan_config.CONFIG_ENV_VAR)
        os.environ[plan_config.CONFIG_ENV_VAR] = path
        try:
            self.assertEqual(plan_config.load_config()["limits"]["backlog"], 4)
            self.assertEqual(plan_config.resolve_config_path(), path)
        finally:
            if previous is None:
                os.environ.pop(plan_config.CONFIG_ENV_VAR, None)
            else:
                os.environ[plan_config.CONFIG_ENV_VAR] = previous

    def test_effective_never_returns_a_partial_config(self):
        merged = plan_config.effective({"limits": {"backlog": 1}})
        self.assertEqual(merged["limits"]["backlog"], 1)
        self.assertEqual(set(merged), set(plan_config.DEFAULTS))
        self.assertEqual(plan_config.effective(None), plan_config.DEFAULTS)

    def test_defaults_are_not_mutated_by_a_caller(self):
        merged = plan_config.effective({"limits": {"backlog": 99}})
        merged["limits"]["backlog"] = 1234
        self.assertEqual(plan_config.DEFAULTS["limits"]["backlog"], 3)

    def test_the_routine_channel_is_never_line(self):
        """Phase sections 61 and 99, asserted at the source of the value."""
        self.assertEqual(plan_config.DEFAULTS["morning_brief"]["channel"], "discord")


if __name__ == "__main__":
    unittest.main()
