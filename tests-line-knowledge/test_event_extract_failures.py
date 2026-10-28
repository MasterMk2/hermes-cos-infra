"""One slow or failing message must not stop a run, and must not run twice.

Until 2026-10-29 the extractor gave the broker 45 s and then asked again,
while the bridge was still running the first call (one call in five took
longer). With one call per wave, a single message that failed twice was taken
for an outage: the run stopped without a strike, and the same message came
first again on the next run.
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
SLOW = RuntimeError("classifier failed after 1 attempt(s): timed out")
DOWN = event_extract.ClassifierUnavailable("classifier failed fast twice: refused")


class FakeClock:
    """Stands in for the time module; only advance() and sleep() move it."""

    def __init__(self):
        self.now = 1_000.0

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def advance(self, seconds):
        self.now += seconds


class ClassifyOneRetryTests(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        socket_path = Path(tmp.name) / "classifier.sock"
        socket_path.touch()
        self.clock = FakeClock()
        for name, value in (("CLASSIFIER_SOCKET", socket_path), ("time", self.clock)):
            patcher = patch.object(event_extract, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def classify_with_broker(self, seconds_per_call, answers):
        calls = []

        def broker(cand):
            calls.append(cand["source_id"])
            self.clock.advance(seconds_per_call)
            answer = answers[len(calls) - 1]
            if isinstance(answer, Exception):
                raise answer
            return answer

        with patch.object(event_extract, "call_broker", broker):
            try:
                return event_extract.classify_one({"source_id": "m1"}), calls
            except RuntimeError as exc:
                return exc, calls

    def test_a_fast_failure_is_retried_once(self):
        result, calls = self.classify_with_broker(
            1, [ConnectionRefusedError("broker restarting"), {"is_event": False}])

        self.assertEqual(result, {"is_event": False})
        self.assertEqual(len(calls), 2)

    def test_two_fast_failures_mean_the_classifier_is_unavailable(self):
        result, calls = self.classify_with_broker(
            1, [ConnectionRefusedError("refused"), ConnectionRefusedError("refused")])

        self.assertIsInstance(result, event_extract.ClassifierUnavailable)
        self.assertEqual(len(calls), 2)

    def test_a_slow_failure_is_not_retried_and_belongs_to_the_message(self):
        result, calls = self.classify_with_broker(
            event_extract.FAST_FAILURE_SECONDS, [TimeoutError("timed out"), {"is_event": False}])

        self.assertIsInstance(result, RuntimeError)
        self.assertNotIsInstance(result, event_extract.ClassifierUnavailable)
        self.assertEqual(len(calls), 1)

    def test_the_extractor_outwaits_the_broker(self):
        import event_broker

        self.assertGreater(event_extract.BROKER_WAIT_SECONDS, event_broker.TIMEOUT_SECONDS)


class FailedClassificationRunTests(unittest.TestCase):
    """main() with four candidates c0..c3 and one message without a signal."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        for name in ("SNAPSHOT", "INDEX_DB", "EVENTS_DIR", "LOG_DIR"):
            patcher = patch.object(event_extract, name, getattr(event_extract, name))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.clock = FakeClock()

    def run_extract(self, errors=None, seed=(), seconds_per_call=20, extra_args=()):
        """errors maps a candidate id to the exception its classification raises."""
        errors = errors or {}
        messages = [("seen", "ありがとう")] + [(f"c{i}", f"明日までに送ります {i}") for i in range(4)]
        messages.append(("plain", "了解です"))
        snapshot = self.dir / "messages.jsonl"
        with snapshot.open("w", encoding="utf-8") as fh:
            for i, (message_id, body) in enumerate(messages):
                fh.write(json.dumps({"message_id": message_id, "chat_id": "chat",
                                     "sent_at": BASE_MS + i * 1000, "body": body},
                                    ensure_ascii=False) + "\n")
        db = self.dir / "events.db"
        con = sqlite3.connect(db)
        event_extract.init_db(con)
        con.executemany("INSERT INTO processed_messages VALUES (?,?,?,?)",
                        [("seen", BASE_MS, "2026-10-01T00:00:00+00:00", "no_event"), *seed])
        con.commit()
        con.close()

        calls = []

        def classify(cand):
            calls.append(cand["source_id"])
            self.clock.advance(seconds_per_call)
            if cand["source_id"] in errors:
                raise errors[cand["source_id"]]
            return {"is_event": False}

        argv = ["event_extract.py", "--source", str(snapshot), "--db", str(db),
                "--index", str(self.dir / "missing-index.db"),
                "--state", str(self.dir / "state.json"), *extra_args]
        with patch.object(sys, "argv", argv), \
                patch.object(event_extract, "classify_one", classify), \
                patch.object(event_extract, "time", self.clock), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(event_extract.main(), 0)

        con = sqlite3.connect(db)
        results = dict(con.execute("SELECT source_id, result FROM processed_messages"))
        con.close()
        return calls, results

    def test_a_slow_failure_gets_a_strike_and_the_run_continues(self):
        calls, results = self.run_extract(errors={"c1": SLOW})

        self.assertEqual(calls, ["c0", "c1", "c2", "c3"])
        self.assertEqual(results["c1"], "failed:1")
        self.assertEqual([results[c] for c in ("c0", "c2", "c3")], ["no_event"] * 3)
        self.assertEqual(results["plain"], "no_candidate")

    def test_a_failure_in_the_last_call_gets_its_strike_too(self):
        _, results = self.run_extract(errors={"c3": SLOW})

        self.assertEqual(results["c3"], "failed:1")

    def test_the_third_strike_parks_the_message(self):
        _, results = self.run_extract(
            errors={"c1": SLOW}, seed=[("c1", BASE_MS + 2000, "2026-10-28T00:00:00+00:00", "failed:2")])

        self.assertEqual(results["c1"], "abandoned:3")

    def test_slow_failures_in_a_row_stop_the_run(self):
        calls, results = self.run_extract(errors={c: SLOW for c in ("c0", "c1", "c2", "c3")})

        self.assertEqual(calls, ["c0", "c1"])
        self.assertEqual((results["c0"], results["c1"]), ("failed:1", "failed:1"))
        self.assertEqual(set(results), {"seen", "c0", "c1"})

    def test_an_unavailable_classifier_stops_the_run_without_strikes(self):
        calls, results = self.run_extract(errors={"c0": DOWN, "c1": DOWN})

        self.assertEqual(calls, ["c0"])
        self.assertEqual(set(results), {"seen"})

    def test_the_time_budget_defers_the_rest_but_keeps_the_bookkeeping(self):
        calls, results = self.run_extract(seconds_per_call=30, extra_args=["--max-seconds", "50"])

        self.assertEqual(calls, ["c0", "c1"])
        self.assertNotIn("c2", results)
        self.assertEqual(results["plain"], "no_candidate")


if __name__ == "__main__":
    unittest.main()
