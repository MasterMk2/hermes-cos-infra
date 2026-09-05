#!/usr/bin/env python3
"""G.2 unit tests — delivery, crash recovery and whole scans.

Spec sections 121 (a crash must not duplicate a delivery), 122 (a stable
retry key), 124 (downtime produces a digest, not a flood), 93 (a scan that
finds nothing costs nothing) and 134 (a gateway that is away delays a
notification rather than losing it).

These drive `Engine.scan()` against real temporary SQLite files shaped like
the Event Layer's and the supervisor's tables, so the collectors, the policy,
the store and the digest logic are exercised together. Only the gateway is a
fake — it is the boundary past which a real credential would be needed, and
past which the gateway's own socket tests and the production run take over.
"""

import json
import os
import sqlite3
import tempfile
import unittest

import att_engine
import att_gateway
import att_store as store
import att_fixtures as fx
from att_gateway import Result
from att_schema import set_meta

EVENTS_SCHEMA = """
CREATE TABLE events (
  event_id TEXT PRIMARY KEY, type TEXT NOT NULL, subtype TEXT,
  summary TEXT NOT NULL, actor TEXT NOT NULL, counterparty TEXT,
  status TEXT NOT NULL,
  actionability REAL, importance REAL, urgency REAL, confidence REAL,
  occurred_at TEXT, due_at TEXT, due_precision TEXT,
  created_at TEXT, updated_at TEXT, resolved_at TEXT,
  project_id TEXT, source_type TEXT, source_id TEXT, source_timestamp TEXT,
  source_conversation TEXT, source_speaker TEXT, evidence_count INTEGER,
  supersedes TEXT, superseded_by TEXT, classification_reason TEXT,
  needs_human_review INTEGER DEFAULT 0, attention REAL,
  last_surfaced_at TEXT, surface_count INTEGER DEFAULT 0,
  snoozed_until TEXT, classifier_version INTEGER
);
"""

JOBS_SCHEMA = """
CREATE TABLE jobs (
  job_id TEXT PRIMARY KEY, project_id TEXT, task_id TEXT, parent_job_id TEXT,
  kind TEXT, goal TEXT NOT NULL, status TEXT NOT NULL, priority TEXT,
  worker_id TEXT, agent TEXT, repository TEXT NOT NULL, branch TEXT,
  created_at TEXT, started_at TEXT, finished_at TEXT, updated_at TEXT,
  attempt INTEGER DEFAULT 0, max_attempts INTEGER DEFAULT 6, prompt_path TEXT,
  result_summary TEXT, exit_code INTEGER, timed_out INTEGER,
  acceptance_criteria TEXT, acceptance_results TEXT, tests_command TEXT,
  tests_status TEXT, tests_summary TEXT, git_before_sha TEXT,
  git_after_sha TEXT, git_changed_files TEXT, git_dirty_before INTEGER,
  git_dirty_after INTEGER, review_status TEXT, next_action TEXT,
  failure_reason TEXT, needs_human INTEGER DEFAULT 0,
  no_progress_strikes INTEGER DEFAULT 0, cancel_requested_at TEXT,
  implementation_finished_at TEXT
);
"""


class EngineCase(unittest.TestCase):
    """A whole engine over temporary source databases and a fake gateway."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.events_db = os.path.join(self.tmp.name, "events.db")
        self.jobs_db = os.path.join(self.tmp.name, "jobs.db")
        self.state_db = os.path.join(self.tmp.name, "attention.db")
        for path, schema in ((self.events_db, EVENTS_SCHEMA),
                             (self.jobs_db, JOBS_SCHEMA)):
            con = sqlite3.connect(path)
            con.executescript(schema)
            con.commit()
            con.close()
        self.clock = fx.clock_at("2026-09-04T12:00")

    def tearDown(self):
        self.tmp.cleanup()

    def config(self, **over):
        sections = {
            "paths": {"state_db": self.state_db,
                      "log_dir": os.path.join(self.tmp.name, "logs")},
            "sources": {"events_db": self.events_db, "jobs_db": self.jobs_db,
                        "discord_collector_db": "", "hermes_health_url": "",
                        "line_refresh_output_dir": "",
                        "calendar_index_db": "", "gmail_index_db": "",
                        "disk_path": self.tmp.name},
            "health": {"enabled": False},
        }
        for name, values in over.items():
            sections.setdefault(name, {}).update(values)
        return fx.config(**sections)

    def engine(self, cfg=None, gateway=None, clock=None):
        eng = att_engine.Engine(cfg or self.config(),
                                clock=clock or self.clock)
        eng.gateway = gateway or fx.FakeGateway(budget=fx.QUOTA_HEALTHY,
                                                liveness=fx.HEALTHY_LIVENESS)
        return eng

    # -- source helpers ------------------------------------------------

    def add_event(self, **over):
        row = {
            "event_id": "evt_1", "type": "request", "summary": "資料を提出する",
            "actor": "counterparty", "counterparty": "だれか", "status": "open",
            "actionability": 0.9, "importance": 0.8, "urgency": 0.9,
            "confidence": 0.9,
            "occurred_at": fx.ago(self.clock, hours=1),
            "due_at": fx.ahead(self.clock, minutes=25),
            "created_at": fx.ago(self.clock, hours=1),
            "source_type": "discord", "source_id": "m1",
            "needs_human_review": 0, "surface_count": 0,
            "evidence_count": 1, "classifier_version": 1,
        }
        row.update(over)
        con = sqlite3.connect(self.events_db)
        cols = ",".join(row)
        marks = ",".join("?" * len(row))
        con.execute(f"INSERT INTO events ({cols}) VALUES ({marks})",
                    list(row.values()))
        con.commit()
        con.close()
        return row

    def add_job(self, **over):
        row = {
            "job_id": "job_1", "goal": "fixture を実装する",
            "status": "COMPLETED", "repository": "fixture",
            "review_status": "APPROVED", "needs_human": 0, "attempt": 1,
            "created_at": fx.ago(self.clock, hours=2),
            "updated_at": fx.ago(self.clock, minutes=5),
            "finished_at": fx.ago(self.clock, minutes=5),
            "result_summary": "ok",
        }
        row.update(over)
        con = sqlite3.connect(self.jobs_db)
        cols = ",".join(row)
        marks = ",".join("?" * len(row))
        con.execute(f"INSERT INTO jobs ({cols}) VALUES ({marks})",
                    list(row.values()))
        con.commit()
        con.close()
        return row

    def state(self):
        con = sqlite3.connect(self.state_db)
        con.row_factory = sqlite3.Row
        return con

    def past_first_run(self):
        """Mark the install watermark done.

        Every test that is not ABOUT the first run has to do this: a freshly
        created database treats everything that already exists as history, by
        design (sections 25, 95).
        """
        from att_schema import open_db

        con = open_db(self.state_db)
        store.mark_baseline_done(con)
        con.commit()
        con.close()


# ------------------------------------------------------------- scans


class TestScan(EngineCase):
    def test_a_scan_with_nothing_to_do_costs_nothing(self):
        # Section 93: DB reads only, no LLM, no notifications — and here,
        # not even a quota call, because there is nothing to decide.
        gw = fx.FakeGateway(budget=fx.QUOTA_HEALTHY,
                            liveness=fx.HEALTHY_LIVENESS)
        counters = self.engine(gateway=gw).scan()
        self.assertEqual(counters["signals_seen"], 0)
        self.assertEqual(counters["notify_now"], 0)
        self.assertEqual(counters["llm_calls"], 0)
        self.assertEqual(gw.calls, [])

    def test_a_completed_job_produces_exactly_one_discord_message(self):
        self.add_job(status="COMPLETED")
        gw = fx.FakeGateway(budget=fx.QUOTA_HEALTHY,
                            liveness=fx.HEALTHY_LIVENESS)
        self.past_first_run()
        eng = self.engine(gateway=gw)
        eng.scan()
        self.assertEqual(len(gw.calls), 1)
        self.assertEqual(gw.calls[0]["channel"], "discord")
        self.assertIn("job_1", gw.calls[0]["text"])

    def test_the_second_scan_does_not_repeat_the_first(self):
        # The whole of section 129, offline: the same fixture rescanned must
        # produce no second delivery.
        self.add_job(status="COMPLETED")
        gw = fx.FakeGateway(budget=fx.QUOTA_HEALTHY,
                            liveness=fx.HEALTHY_LIVENESS)
        self.past_first_run()
        eng = self.engine(gateway=gw)
        eng.scan()
        first = len(gw.calls)
        self.assertEqual(first, 1)
        eng.scan()
        eng.scan()
        self.assertEqual(len(gw.calls), first,
                         "a rescan produced another delivery")

    def test_the_first_scan_holds_history_back_and_digests_it(self):
        # Sections 25, 95, 96. Ten pre-existing overdue events must produce
        # one digest, not ten notifications.
        for i in range(10):
            self.add_event(event_id=f"evt_{i}", status="open",
                           occurred_at=fx.ago(self.clock, days=5),
                           created_at=fx.ago(self.clock, days=5),
                           due_at=fx.ago(self.clock, days=2))
        gw = fx.FakeGateway(budget=fx.QUOTA_HEALTHY,
                            liveness=fx.HEALTHY_LIVENESS)
        eng = self.engine(gateway=gw)
        counters = eng.scan()
        self.assertEqual(counters["notify_now"], 0)
        self.assertGreaterEqual(counters["digested"], 10)
        self.assertEqual(len(gw.calls), 1)
        self.assertEqual(gw.calls[0]["channel"], "discord")
        self.assertEqual(gw.calls[0]["severity"], "INFO")
        self.assertIn("初回同期", gw.calls[0]["text"])
        # The digest accounted for all of them, so a second scan must add
        # nothing: they are not new just because they are still outstanding.
        eng.scan()
        self.assertEqual(len(gw.calls), 1)

    def test_an_urgent_deadline_reaches_both_channels(self):
        self.add_event(due_at=fx.ahead(self.clock, minutes=25),
                       occurred_at=fx.ago(self.clock, minutes=30),
                       created_at=fx.ago(self.clock, minutes=30))
        gw = fx.FakeGateway(budget=fx.QUOTA_HEALTHY,
                            liveness=fx.HEALTHY_LIVENESS)
        self.past_first_run()
        eng = self.engine(gateway=gw)
        eng.scan()
        channels = [c["channel"] for c in gw.calls]
        self.assertIn("discord", channels)
        self.assertIn("line", channels)

    def test_no_llm_call_is_ever_made(self):
        # Section 21 as a property of the build: there is no code path to a
        # model, so the counter cannot be anything but zero.
        self.add_event()
        self.add_job()
        eng = self.engine()
        eng.scan()
        con = self.state()
        total = con.execute(
            "SELECT COALESCE(SUM(llm_calls), 0) FROM scan_runs").fetchone()[0]
        con.close()
        self.assertEqual(total, 0)


# ---------------------------------------------------------- delivery


class TestDelivery(EngineCase):
    def notification(self, eng, con, *, severity="IMPORTANT",
                     dedupe_key="test:1"):
        row, _ = store.create_notification(
            con, dedupe_key=dedupe_key, severity=severity, subject="s",
            body="本文", reason="test")
        con.commit()
        return row

    def test_a_successful_delivery_is_recorded_as_delivered(self):
        gw = fx.FakeGateway()
        eng = self.engine(gateway=gw)
        con = eng.db()
        n = self.notification(eng, con)
        self.assertTrue(eng.deliver(con, n, ["discord"], None))
        row = store.get_notification(con, n["notification_id"])
        self.assertEqual(row["state"], "DELIVERED")
        self.assertEqual(json.loads(row["delivered_channels"]), ["discord"])
        con.close()

    def test_a_failed_delivery_is_never_recorded_as_delivered(self):
        # Section 88, the G.1 lesson.
        gw = fx.FakeGateway(answers=[Result(att_gateway.FAILED,
                                            detail="http:500", channel="discord")])
        eng = self.engine(gateway=gw)
        con = eng.db()
        n = self.notification(eng, con)
        self.assertFalse(eng.deliver(con, n, ["discord"], None))
        row = store.get_notification(con, n["notification_id"])
        self.assertNotEqual(row["state"], "DELIVERED")
        self.assertIsNone(row["delivered_at"])
        con.close()

    def test_an_unreachable_gateway_leaves_the_notification_pending(self):
        # Section 134: the gateway being away is a delay, not a loss.
        gw = fx.FakeGateway(answers=[Result(att_gateway.UNREACHABLE,
                                            detail="FileNotFoundError",
                                            channel="discord")])
        eng = self.engine(gateway=gw)
        con = eng.db()
        n = self.notification(eng, con)
        eng.deliver(con, n, ["discord"], None)
        row = store.get_notification(con, n["notification_id"])
        self.assertEqual(row["state"], "PENDING")
        self.assertEqual(row["attempts"], 1)
        con.close()

    def test_a_lost_answer_retries_the_same_channel_not_the_other(self):
        # Section 86. UNKNOWN means the message may have gone out; falling
        # through to the other channel would deliver it twice.
        gw = fx.FakeGateway(answers=[
            Result(att_gateway.UNKNOWN, detail="timeout", channel="line"),
            Result(att_gateway.DUPLICATE, detail="already_delivered",
                   channel="line"),
        ])
        eng = self.engine(gateway=gw)
        con = eng.db()
        n = self.notification(eng, con, severity="CRITICAL")
        eng.deliver(con, n, ["line"], None)
        self.assertEqual([c["channel"] for c in gw.calls], ["line", "line"])
        self.assertEqual(
            store.get_notification(con, n["notification_id"])["state"],
            "DELIVERED")
        con.close()

    def test_a_failing_line_does_not_silence_a_critical_notification(self):
        # Section 84: never dropped because one channel failed.
        gw = fx.FakeGateway(answers=[
            Result(att_gateway.FAILED, detail="http:500", channel="line"),
            Result(att_gateway.DELIVERED, channel="discord"),
        ])
        eng = self.engine(gateway=gw)
        con = eng.db()
        n = self.notification(eng, con, severity="CRITICAL")
        self.assertTrue(eng.deliver(con, n, ["line", "discord"], None))
        row = store.get_notification(con, n["notification_id"])
        self.assertEqual(json.loads(row["delivered_channels"]), ["discord"])
        con.close()

    def test_the_notification_id_is_derived_from_the_dedupe_key(self):
        # Section 122's stability, one level up from the gateway's retry key:
        # the same logical notification computes the same id after a restart,
        # which is what lets the gateway recognise the retry.
        a = store.new_notification_id("event:evt_1:due-soon:3h")
        b = store.new_notification_id("event:evt_1:due-soon:3h")
        self.assertEqual(a, b)
        self.assertNotEqual(
            a, store.new_notification_id("event:evt_1:due-soon:24h"))

    def test_a_crash_before_the_state_write_does_not_resend(self):
        # Section 121. The first process delivered and died; the second finds
        # the row DELIVERING, retries, and the gateway answers `duplicate`.
        gw = fx.FakeGateway(answers=[Result(att_gateway.DUPLICATE,
                                            detail="already_delivered",
                                            channel="discord")])
        eng = self.engine(gateway=gw)
        con = eng.db()
        n = self.notification(eng, con)
        store.mark_notification(con, n["notification_id"], "DELIVERING",
                                count_attempt=True)
        con.execute("UPDATE notifications SET updated_at = ?"
                    " WHERE notification_id = ?",
                    ("2020-01-01T00:00:00+00:00", n["notification_id"]))
        con.commit()
        eng._retry_stuck(con, None, {"delivered": 0, "failed": 0})
        con.commit()
        row = store.get_notification(con, n["notification_id"])
        self.assertEqual(row["state"], "DELIVERED")
        # One retry, and the gateway called it a duplicate rather than sending.
        self.assertEqual(len(gw.calls), 1)
        con.close()

    def test_delivery_gives_up_after_the_configured_attempts(self):
        cfg = self.config(notifications={"max_delivery_attempts": 2})
        gw = fx.FakeGateway()
        gw.default = Result(att_gateway.FAILED, detail="http:500",
                            channel="discord")
        eng = self.engine(cfg=cfg, gateway=gw)
        con = eng.db()
        n = self.notification(eng, con)
        eng.deliver(con, n, ["discord"], None)
        n2 = store.get_notification(con, n["notification_id"])
        eng.deliver(con, n2, ["discord"], None)
        row = store.get_notification(con, n["notification_id"])
        self.assertEqual(row["state"], "FAILED")
        con.close()

    def test_a_line_delivery_reserves_a_credit_and_settles_it(self):
        cfg = self.config()
        gw = fx.FakeGateway(budget=fx.QUOTA_HEALTHY)
        eng = self.engine(cfg=cfg, gateway=gw)
        con = eng.db()
        import att_budget

        budget = att_budget.evaluate(fx.QUOTA_HEALTHY, cfg, self.clock)
        n = self.notification(eng, con, severity="CRITICAL")
        eng.deliver(con, n, ["line"], budget)
        con.commit()
        self.assertEqual(store.spent_today(con, "line",
                                           self.clock.day_key()), 1)
        row = con.execute(
            "SELECT state FROM channel_budget WHERE notification_id = ?",
            (n["notification_id"],)).fetchone()
        self.assertEqual(row["state"], "SPENT")
        con.close()

    def test_a_refused_line_reservation_does_not_call_the_gateway(self):
        cfg = self.config()
        gw = fx.FakeGateway(budget=fx.QUOTA_EXHAUSTED)
        eng = self.engine(cfg=cfg, gateway=gw)
        con = eng.db()
        import att_budget

        budget = att_budget.evaluate(fx.QUOTA_EXHAUSTED, cfg, self.clock)
        n = self.notification(eng, con, severity="URGENT")
        eng.deliver(con, n, ["line"], budget)
        self.assertEqual(gw.calls, [])
        attempts = con.execute(
            "SELECT outcome FROM delivery_attempts WHERE notification_id = ?",
            (n["notification_id"],)).fetchall()
        self.assertEqual([a["outcome"] for a in attempts], ["rejected"])
        con.close()


# ------------------------------------------------------------ digests


class TestDigests(EngineCase):
    def test_an_empty_digest_is_never_sent(self):
        # Section 61.
        gw = fx.FakeGateway()
        eng = self.engine(gateway=gw)
        con = eng.db()
        eng._maybe_digest(con, {"delivered": 0, "failed": 0},
                          recovery=False, baseline_done=True)
        self.assertEqual(gw.calls, [])
        con.close()

    def test_downtime_produces_one_digest_rather_than_a_flood(self):
        # Section 124. Thirty missed deadlines, one message.
        for i in range(30):
            self.add_event(event_id=f"evt_{i}",
                           occurred_at=fx.ago(self.clock, minutes=30),
                           created_at=fx.ago(self.clock, minutes=30),
                           due_at=fx.ahead(self.clock, hours=2))
        gw = fx.FakeGateway(budget=fx.QUOTA_HEALTHY,
                            liveness=fx.HEALTHY_LIVENESS)
        self.past_first_run()
        eng = self.engine(gateway=gw)
        con = eng.db()
        from att_schema import set_meta

        # The engine was last here eight hours ago.
        set_meta(con, "last_scan_finished_at", fx.ago(self.clock, hours=8))
        con.commit()
        con.close()
        counters = eng.scan()
        self.assertEqual(counters["notify_now"], 0)
        self.assertGreaterEqual(counters["digested"], 30)
        self.assertEqual(len(gw.calls), 1)
        self.assertEqual(gw.calls[0]["channel"], "discord")
        self.assertEqual(gw.calls[0]["severity"], "INFO")

    def test_a_digest_never_spends_a_push(self):
        # Section 62/63: routine digests go to Discord, full stop.
        for i in range(12):
            self.add_event(event_id=f"evt_{i}", confidence=0.55,
                           occurred_at=fx.ago(self.clock, minutes=30),
                           created_at=fx.ago(self.clock, minutes=30),
                           due_at=fx.ahead(self.clock, hours=2))
        gw = fx.FakeGateway(budget=fx.QUOTA_HEALTHY,
                            liveness=fx.HEALTHY_LIVENESS)
        self.past_first_run()
        eng = self.engine(gateway=gw)
        eng.scan()
        self.assertTrue(all(c["channel"] == "discord" for c in gw.calls))

    def test_nothing_is_digested_during_quiet_hours(self):
        gw = fx.FakeGateway(budget=fx.QUOTA_HEALTHY,
                            liveness=fx.HEALTHY_LIVENESS)
        eng = self.engine(gateway=gw,
                          clock=fx.clock_at("2026-09-04T23:30"))
        con = eng.db()
        eng._maybe_digest(con, {"delivered": 0, "failed": 0},
                          recovery=True, baseline_done=True)
        self.assertEqual(gw.calls, [])
        con.close()


class TestOvernightRelease(EngineCase):
    """Section 57 — what was held back at night has to come out in the morning.

    DEFER is the one canonical decision with a second half: the decision is
    only correct if something later releases it. That release had never run
    anywhere — not here, and not in production, because the first attempt at
    it jumped the clock eight hours and tripped the downtime rule instead,
    which folds the backlog into a `recovery` digest and hides whether
    `catchup` works at all.

    The distinction these tests pin down: a deferred signal that the morning
    scan sees again is decided fresh, on its merits; only one that is no
    longer being emitted is released by the catch-up. Both halves matter —
    the first is why the morning is not a duplicate of the night, the second
    is why nothing is silently dropped.
    """

    #: Far enough after quiet hours end that the morning scan still sees a
    #: live deadline, close enough that the night scan judges it worth
    #: raising at all.
    MORNING_DEADLINE = "2026-09-05T09:00"

    def _defer_overnight(self, gw):
        """One scan inside quiet hours, leaving a DEFER behind."""
        self.add_event(event_id="evt_overnight",
                       due_at=fx.clock_at(self.MORNING_DEADLINE).now_iso(),
                       occurred_at=fx.ago(self.clock, hours=1),
                       created_at=fx.ago(self.clock, hours=1))
        self.past_first_run()
        eng = self.engine(gateway=gw, clock=fx.clock_at("2026-09-04T23:30"))
        eng.scan()
        con = self.state()
        deferred = [r["signal_id"] for r in con.execute(
            "SELECT signal_id FROM attention_decisions WHERE decision='DEFER'")]
        con.close()
        self.assertTrue(deferred, "the fixture did not produce a deferral")
        self.assertEqual(gw.calls, [], "quiet hours were not respected")
        return deferred

    def _morning(self, gw, at="2026-09-05T08:05", last_scan="2026-09-05T07:55"):
        """A scan after quiet hours end, with the night's scans accounted for.

        `last_scan_finished_at` is set explicitly: without it the nine-hour
        jump reads as an outage and the recovery rule pre-empts the catch-up.
        A real engine writes that key every five minutes all night.
        """
        from att_schema import open_db

        clock = fx.clock_at(at)
        con = open_db(self.state_db)
        set_meta(con, "last_scan_finished_at",
                 fx.clock_at(last_scan).now_iso())
        con.commit()
        con.close()
        eng = self.engine(gateway=gw, clock=clock)
        eng.scan()
        return eng

    def _digests(self):
        con = self.state()
        rows = [dict(r) for r in con.execute(
            "SELECT kind, signal_ids, notification_id FROM digests")]
        con.close()
        return rows

    def test_a_signal_that_stopped_recurring_is_released_by_the_catchup(self):
        # The event is resolved overnight, so the morning collector does not
        # emit it again. Without a catch-up it would sit deferred for ever.
        gw = fx.FakeGateway(budget=fx.QUOTA_HEALTHY,
                            liveness=fx.HEALTHY_LIVENESS)
        deferred = self._defer_overnight(gw)

        con = sqlite3.connect(self.events_db)
        con.execute("UPDATE events SET status = 'resolved'")
        con.commit()
        con.close()

        self._morning(gw)

        kinds = [d["kind"] for d in self._digests()]
        self.assertEqual(kinds, ["catchup"], f"digest kinds were {kinds}")
        carried = self._digests()[0]["signal_ids"]
        for sig in deferred:
            self.assertIn(sig, carried)
        self.assertEqual(len(gw.calls), 1)
        self.assertEqual(gw.calls[0]["channel"], "discord",
                         "a digest must never spend a push")
        self.assertEqual(gw.calls[0]["severity"], "INFO")

    def test_the_catchup_is_sent_once(self):
        gw = fx.FakeGateway(budget=fx.QUOTA_HEALTHY,
                            liveness=fx.HEALTHY_LIVENESS)
        self._defer_overnight(gw)
        con = sqlite3.connect(self.events_db)
        con.execute("UPDATE events SET status = 'resolved'")
        con.commit()
        con.close()

        self._morning(gw)
        first = len(gw.calls)
        self._morning(gw, at="2026-09-05T08:10", last_scan="2026-09-05T08:05")
        self.assertEqual(len(gw.calls), first,
                         "the catch-up digest was sent twice")
        self.assertEqual([d["kind"] for d in self._digests()], ["catchup"])

    def test_a_signal_still_present_in_the_morning_is_decided_fresh(self):
        # The deadline is still open at 08:05, so the morning scan sees the
        # same event and judges it on its merits rather than summarising it.
        # This is the reason the catch-up is usually empty, and it is
        # deliberate: an item that still matters deserves the notification it
        # would have got at any other hour, not a bullet in a list.
        gw = fx.FakeGateway(budget=fx.QUOTA_HEALTHY,
                            liveness=fx.HEALTHY_LIVENESS)
        self._defer_overnight(gw)
        self._morning(gw)

        self.assertTrue(gw.calls, "the morning produced nothing at all")
        con = self.state()
        latest = con.execute(
            "SELECT decision FROM attention_decisions"
            " ORDER BY decision_id DESC LIMIT 1").fetchone()[0]
        con.close()
        self.assertNotEqual(latest, "DEFER",
                            "the item was still being held after quiet hours")


if __name__ == "__main__":
    unittest.main()
