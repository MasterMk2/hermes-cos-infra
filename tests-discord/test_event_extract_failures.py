"""A Discord message that keeps failing must earn strikes, not stop every tick.

Until 2026-10-29 one failed classification was taken for an outage: the tick
stopped without a strike, and the same message came first again 15 minutes
later. On the Pi some messages failed this way up to six times.
"""

import contextlib
import io
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "src" / "discord-knowledge" / "app"
LINE_DIR = ROOT / "src" / "line-knowledge"
for _p in (Path(__file__).resolve().parent, APP_DIR, LINE_DIR,
           ROOT / "src" / "discord-knowledge" / "events"):
    sys.path.insert(0, str(_p))
os.environ.setdefault("DISCORD_KNOWLEDGE_APP_DIR", str(APP_DIR))
os.environ.setdefault("HERMES_EVENTS_TOOL_DIR", str(LINE_DIR))

import yaml  # noqa: E402

import discord_event_extract as dee  # noqa: E402
import dk_fixtures as fx  # noqa: E402
import dk_store  # noqa: E402
from dk_normalize import normalize_message  # noqa: E402
from dk_schema import open_source_db  # noqa: E402

SLOW = RuntimeError("classifier failed after 1 attempt(s): timed out")
DOWN = dee.event_extract.ClassifierUnavailable("classifier failed fast twice: refused")


class FailedClassificationTickTests(unittest.TestCase):
    """main() over four owner messages with a signal and one without."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.root / "messages.db"
        self.config = self.root / "config.yaml"
        data = fx.config_dict(owner=fx.OWNER_ID)
        data["paths"] = {"source_db": str(self.source), "index_db": str(self.root / "index.db"),
                         "state_dir": str(self.root), "log_dir": str(self.root)}
        self.config.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

        con = open_source_db(self.source)
        dk_store.upsert_guild(con, fx.GUILD_A, "Guild A")
        ctx = fx.CHANNEL_CTX[fx.CHANNEL_A1]
        dk_store.upsert_channel(con, fx.CHANNEL_A1, fx.GUILD_A, channel_name=ctx["channel_name"],
                                channel_type=ctx["channel_type"],
                                parent_channel_id=ctx["parent_channel_id"],
                                is_thread=ctx["is_thread"], readable=True)
        bodies = [f"明日までに送ります {i}" for i in range(4)] + ["了解です"]
        for hour, body in enumerate(bodies, start=8):
            payload = fx.message(content=body, created=fx.at(20, hour))
            dk_store.apply_message(con, normalize_message(
                payload, guild_id=fx.GUILD_A, guild_name="Guild A", channel=ctx))
        con.commit()
        con.close()

        self.db = self.root / "events" / "events.db"
        for name in ("CLASSIFIER_SYSTEM", "classify_one"):
            patcher = patch.object(dee.event_extract, name, getattr(dee.event_extract, name))
            patcher.start()
            self.addCleanup(patcher.stop)

    def tick(self, errors=None):
        """Run one tick; errors maps a message number to the exception it raises."""
        errors = errors or {}
        calls = []

        def classify(cand):
            number = int(cand["message"].rsplit("送ります", 1)[1].split()[0])
            calls.append(number)
            if number in errors:
                raise errors[number]
            return {"is_event": False}

        dee.event_extract.classify_one = classify
        argv = ["discord_event_extract.py", "--config", str(self.config),
                "--source-db", str(self.source), "--db", str(self.db)]
        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(dee.main(), 0)

        con = sqlite3.connect(self.db)
        results = sorted(r[0] for r in con.execute("SELECT result FROM processed_messages"))
        con.close()
        return calls, results

    def test_a_slow_failure_gets_a_strike_and_the_tick_continues(self):
        calls, results = self.tick({1: SLOW})

        self.assertEqual(calls, [0, 1, 2, 3])
        self.assertEqual(results, ["failed:1", "no_candidate", "no_event", "no_event", "no_event"])

    def test_a_message_that_keeps_failing_is_parked_after_three_ticks(self):
        for _ in range(3):
            self.tick({0: SLOW})
        calls, results = self.tick({0: SLOW})

        self.assertEqual(calls, [])
        self.assertEqual(results, ["abandoned:3", "no_candidate", "no_event", "no_event", "no_event"])

    def test_an_unavailable_classifier_stops_the_tick_without_strikes(self):
        calls, results = self.tick({0: DOWN, 1: DOWN})

        self.assertEqual(calls, [0])
        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
