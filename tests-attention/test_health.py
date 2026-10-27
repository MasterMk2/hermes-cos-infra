#!/usr/bin/env python3
"""G.2 unit tests — health detection (spec section 118).

Four properties, and the fourth is the one that made this phase necessary:

    one transient failure                -> nothing
    a sustained failure                  -> one notification
    the same failure, still there        -> still one
    recovery, after a notified failure   -> one more

plus the G.1 lesson (section 39): a process that is alive is not a service
that is working, and an idle service is not a broken one (section 41).
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import att_health as health
import att_store as store
import att_fixtures as fx


def probe(service, ok, reason="", detail=""):
    return health.Probe(service=service, ok=ok, reason=reason, detail=detail)


def run(con, probes, cfg=None, clock=None):
    cfg = cfg or fx.config()
    clock = clock or fx.clock_at("2026-09-04T12:00")
    out = health.signals_from_probes(con, probes, cfg, clock)
    con.commit()
    return out


class TestEpisodes(unittest.TestCase):
    def setUp(self):
        self.cfg = fx.config(health={"sustained_seconds": {"gateway": 900}})
        self.con = fx.db()

    def test_one_transient_failure_says_nothing(self):
        # Section 35. A single reconnect, timeout or 5xx is not an incident.
        out = run(self.con, [probe("gateway", False, "unreachable")],
                  self.cfg, fx.clock_at("2026-09-04T12:00"))
        self.assertEqual(out, [])
        self.assertEqual(len(store.open_episodes(self.con, "gateway")), 1)

    def test_a_sustained_failure_notifies_once(self):
        cfg, con = self.cfg, self.con
        t0 = fx.clock_at("2026-09-04T12:00")
        run(con, [probe("gateway", False, "unreachable")], cfg, t0)
        # Still broken five minutes later: not yet 15 minutes.
        self.assertEqual(
            run(con, [probe("gateway", False, "unreachable")], cfg,
                fx.clock_at("2026-09-04T12:05")), [])
        # Twenty minutes in, it has lasted.
        out = run(con, [probe("gateway", False, "unreachable")], cfg,
                  fx.clock_at("2026-09-04T12:20"))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].signal_type, "health_degraded")
        self.assertEqual(out[0].severity, "CRITICAL")

    def test_still_broken_does_not_notify_again(self):
        cfg, con = self.cfg, self.con
        run(con, [probe("gateway", False, "unreachable")], cfg,
            fx.clock_at("2026-09-04T12:00"))
        out = run(con, [probe("gateway", False, "unreachable")], cfg,
                  fx.clock_at("2026-09-04T12:20"))
        episode_id = out[0].metadata["episode_id"]
        store.mark_episode_notified(con, episode_id)
        con.commit()
        for at in ("12:25", "12:40", "13:30", "18:00"):
            self.assertEqual(
                run(con, [probe("gateway", False, "unreachable")], cfg,
                    fx.clock_at(f"2026-09-04T{at}")), [],
                f"a second alert was produced at {at}")

    def test_recovery_after_a_notified_failure_notifies_once(self):
        cfg, con = self.cfg, self.con
        run(con, [probe("gateway", False, "unreachable")], cfg,
            fx.clock_at("2026-09-04T12:00"))
        out = run(con, [probe("gateway", False, "unreachable")], cfg,
                  fx.clock_at("2026-09-04T12:20"))
        store.mark_episode_notified(con, out[0].metadata["episode_id"])
        con.commit()
        recovered = run(con, [probe("gateway", True)], cfg,
                        fx.clock_at("2026-09-04T12:40"))
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].signal_type, "health_recovered")
        store.mark_recovery_notified(con, recovered[0].metadata["episode_id"])
        con.commit()
        again = run(con, [probe("gateway", True)], cfg,
                    fx.clock_at("2026-09-04T12:45"))
        self.assertEqual(again, [])

    def test_hermes_failing_recovers_only_when_hermes_answers_again(self):
        cfg, con = self.cfg, self.con
        failing = probe("gateway", False, "hermes_failing")
        run(con, [failing], cfg, fx.clock_at("2026-10-27T04:19"))
        out = run(con, [failing], cfg, fx.clock_at("2026-10-27T04:35"))
        store.mark_episode_notified(con, out[0].metadata["episode_id"])
        con.commit()
        # An hour on, nothing has succeeded: the episode is held, so no 「復旧」.
        quiet = health.Probe("gateway", True, hold_reasons=("hermes_failing",))
        self.assertEqual(run(con, [quiet], cfg, fx.clock_at("2026-10-27T05:20")), [])
        self.assertEqual(len(store.open_episodes(con, "gateway")), 1)
        # The next real answer closes it, with one truthful recovery.
        recovered = run(con, [probe("gateway", True)], cfg,
                        fx.clock_at("2026-10-27T06:00"))
        self.assertEqual([s.signal_type for s in recovered], ["health_recovered"])

    def test_a_held_hermes_failure_does_not_keep_an_outage_open(self):
        # Review of the 2026-10-27 fix: holding the whole gateway verdict kept
        # every gateway episode open, so a later outage was folded into the
        # old, already announced one and nobody was told.
        cfg, con = self.cfg, self.con
        failing = probe("gateway", False, "hermes_failing")
        run(con, [failing], cfg, fx.clock_at("2026-10-27T04:19"))
        out = run(con, [failing], cfg, fx.clock_at("2026-10-27T04:40"))
        store.mark_episode_notified(con, out[0].metadata["episode_id"])
        down = probe("gateway", False, "unreachable")
        run(con, [down], cfg, fx.clock_at("2026-10-27T05:00"))
        out = run(con, [down], cfg, fx.clock_at("2026-10-27T05:20"))
        store.mark_episode_notified(con, out[0].metadata["episode_id"])
        con.commit()
        held = health.Probe("gateway", True, hold_reasons=("hermes_failing",))
        back = run(con, [held], cfg, fx.clock_at("2026-10-27T05:45"))
        self.assertEqual([(s.signal_type, s.metadata["reason"]) for s in back],
                         [("health_recovered", "unreachable")])
        self.assertEqual([e["reason"] for e in store.open_episodes(con, "gateway")],
                         ["hermes_failing"])
        # The next outage is a new episode and is announced once it lasts.
        run(con, [down], cfg, fx.clock_at("2026-10-28T02:00"))
        again = run(con, [down], cfg, fx.clock_at("2026-10-28T02:20"))
        self.assertEqual([s.signal_type for s in again], ["health_degraded"])

    def test_recovery_from_a_failure_nobody_heard_about_is_silent(self):
        # Section 38. Announcing the end of a problem that was never announced
        # is how a quiet system becomes a chatty one.
        cfg, con = self.cfg, self.con
        run(con, [probe("gateway", False, "unreachable")], cfg,
            fx.clock_at("2026-09-04T12:00"))
        out = run(con, [probe("gateway", True)], cfg,
                  fx.clock_at("2026-09-04T12:05"))
        self.assertEqual(out, [])

    def test_an_inconclusive_probe_neither_opens_nor_closes(self):
        # "I could not check" is not "it is fine". Treating it as healthy
        # would silently close a real outage (section 140).
        cfg, con = self.cfg, self.con
        run(con, [probe("gateway", False, "unreachable")], cfg,
            fx.clock_at("2026-09-04T12:00"))
        run(con, [health.Probe("gateway", True, inconclusive=True)], cfg,
            fx.clock_at("2026-09-04T12:05"))
        self.assertEqual(len(store.open_episodes(con, "gateway")), 1)

    def test_a_different_reason_is_a_different_episode(self):
        cfg, con = self.cfg, self.con
        run(con, [probe("gateway", False, "unreachable")], cfg,
            fx.clock_at("2026-09-04T12:00"))
        run(con, [probe("gateway", False, "queue_stuck")], cfg,
            fx.clock_at("2026-09-04T12:01"))
        self.assertEqual(len(store.open_episodes(con, "gateway")), 2)


class TestGatewayProbe(unittest.TestCase):
    """Sections 39-42 — work moving, not processes alive."""

    def setUp(self):
        self.cfg = fx.config()
        self.clock = fx.clock_at("2026-09-04T12:00")

    def gw(self, **over):
        payload = dict(fx.HEALTHY_LIVENESS)
        payload.update(over)
        return fx.FakeGateway(liveness=payload)

    def test_an_idle_gateway_is_healthy(self):
        # Section 41. Nothing has arrived all day; every timestamp is null.
        p = health.probe_gateway(self.cfg, self.clock, self.gw())
        self.assertTrue(p.ok, p.reason)

    def test_an_unreachable_gateway_is_degraded(self):
        p = health.probe_gateway(self.cfg, self.clock,
                                 fx.FakeGateway(liveness=None))
        self.assertFalse(p.ok)
        self.assertEqual(p.reason, "unreachable")

    def test_a_queue_with_depth_but_no_age_is_not_stuck(self):
        # Just arrived. Depth alone is normal operation (section 42).
        p = health.probe_gateway(self.cfg, self.clock, self.gw(
            queue_depth=3, oldest_queued_at=fx.ago(self.clock, seconds=20)))
        self.assertTrue(p.ok, p.reason)

    def test_a_queue_that_is_deep_and_old_is_stuck(self):
        p = health.probe_gateway(self.cfg, self.clock, self.gw(
            queue_depth=3, oldest_queued_at=fx.ago(self.clock, hours=2)))
        self.assertFalse(p.ok)
        self.assertEqual(p.reason, "queue_stuck")

    def test_a_request_stuck_in_flight_is_detected(self):
        p = health.probe_gateway(self.cfg, self.clock, self.gw(
            processing_depth=1,
            oldest_processing_at=fx.ago(self.clock, hours=1)))
        self.assertFalse(p.ok)
        self.assertEqual(p.reason, "processing_stuck")

    def test_a_disconnected_interface_bot_is_detected(self):
        p = health.probe_gateway(self.cfg, self.clock,
                                 self.gw(discord_connected=False))
        self.assertFalse(p.ok)
        self.assertEqual(p.reason, "discord_disconnected")

    def test_delivering_error_messages_is_not_health(self):
        # THE G.1 failure, as a test (section 39). Every queue metric is
        # perfect; the gateway is answering every message with an error.
        p = health.probe_gateway(self.cfg, self.clock, self.gw(
            queue_depth=0,
            last_inbound_processed_at=fx.ago(self.clock, minutes=1),
            last_outbound_success_at=fx.ago(self.clock, minutes=1),
            last_hermes_success_at=fx.ago(self.clock, hours=6),
            last_hermes_failure_at=fx.ago(self.clock, minutes=1),
            hermes_failure_count=12, responded_count=12))
        self.assertFalse(p.ok)
        self.assertEqual(p.reason, "hermes_failing")

    def test_an_aged_failure_without_a_success_is_not_a_recovery(self):
        # 2026-10-27: an hour after the last failure the probe fell through to
        # "ok" and announced 「gateway は復旧しました」 while Hermes still had
        # not answered anything.
        p = health.probe_gateway(self.cfg, self.clock, self.gw(
            last_hermes_failure_at=fx.ago(self.clock, hours=2),
            last_hermes_success_at=fx.ago(self.clock, hours=6)))
        self.assertTrue(p.ok)
        self.assertFalse(p.inconclusive)
        self.assertEqual(p.hold_reasons, ("hermes_failing",))

    def test_a_failure_with_no_success_ever_recorded_is_not_a_recovery(self):
        p = health.probe_gateway(self.cfg, self.clock, self.gw(
            last_hermes_failure_at=fx.ago(self.clock, hours=2)))
        self.assertEqual(p.hold_reasons, ("hermes_failing",))

    def test_a_success_after_the_last_failure_is_healthy_again(self):
        p = health.probe_gateway(self.cfg, self.clock, self.gw(
            last_hermes_failure_at=fx.ago(self.clock, hours=2),
            last_hermes_success_at=fx.ago(self.clock, minutes=1)))
        self.assertTrue(p.ok, p.reason)
        self.assertEqual(p.hold_reasons, ())


class TestCollectorProbe(unittest.TestCase):
    def make_db(self, tmpdir, state):
        import os
        import sqlite3

        path = os.path.join(tmpdir, "messages.db")
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE ingest_state (key TEXT PRIMARY KEY, value TEXT)")
        con.executemany("INSERT INTO ingest_state VALUES (?,?)",
                        [(k, str(v)) for k, v in state.items()])
        con.commit()
        con.close()
        return path

    def probe(self, state, clock=None):
        import tempfile

        clock = clock or fx.clock_at("2026-09-04T12:00")
        with tempfile.TemporaryDirectory() as tmp:
            path = self.make_db(tmp, state)
            cfg = fx.config(sources={"discord_collector_db": path})
            return health.probe_collector(cfg, clock)

    def test_a_live_collector_with_no_traffic_is_healthy(self):
        # Section 41 again, and the reason the heartbeat rather than the last
        # message is what is checked: measured on 2026-09-04, `updated_at`
        # advanced within 25 seconds while no message had arrived for hours.
        clock = fx.clock_at("2026-09-04T12:00")
        p = self.probe({"updated_at": fx.ago(clock, seconds=30),
                        "gateway_connected": "1",
                        "collector_health": "CONNECTED",
                        "queue_depth": "0",
                        "last_successful_ingest_at": fx.ago(clock, hours=18)},
                       clock)
        self.assertTrue(p.ok, p.reason)

    def test_a_stale_heartbeat_is_degraded(self):
        clock = fx.clock_at("2026-09-04T12:00")
        p = self.probe({"updated_at": fx.ago(clock, hours=3),
                        "gateway_connected": "1",
                        "collector_health": "CONNECTED"}, clock)
        self.assertFalse(p.ok)
        self.assertEqual(p.reason, "heartbeat_stale")

    def test_a_disconnected_collector_is_degraded(self):
        clock = fx.clock_at("2026-09-04T12:00")
        p = self.probe({"updated_at": fx.ago(clock, seconds=10),
                        "gateway_connected": "0",
                        "collector_health": "DEGRADED"}, clock)
        self.assertFalse(p.ok)
        self.assertEqual(p.reason, "gateway_disconnected")

    def test_a_missing_database_is_inconclusive_not_broken(self):
        cfg = fx.config(sources={"discord_collector_db": "/nonexistent/x.db"})
        p = health.probe_collector(cfg, fx.clock_at("2026-09-04T12:00"))
        self.assertTrue(p.inconclusive)


class TestDiskProbe(unittest.TestCase):
    def test_a_normal_volume_is_healthy(self):
        cfg = fx.config(sources={"disk_path": "/"})
        p = health.probe_disk(cfg, fx.clock_at("2026-09-04T12:00"))
        self.assertIn("free_percent", p.facts)

    def test_thresholds_escalate(self):
        cfg = fx.config(sources={"disk_path": "/"},
                        health={"disk_warn_percent": 100,
                                "disk_critical_percent": 100})
        # A capacity policy test must not depend on how full the CI runner's
        # root filesystem happens to be (some fixtures report 100% free).
        with patch.object(health.shutil, "disk_usage",
                          return_value=SimpleNamespace(total=1000, used=900, free=100)):
            p = health.probe_disk(cfg, fx.clock_at("2026-09-04T12:00"))
        self.assertFalse(p.ok)
        self.assertEqual(p.reason, "disk_critical")
        self.assertEqual(health.SEVERITY_BY_REASON["disk_critical"], "CRITICAL")


class TestLineRefreshProbe(unittest.TestCase):
    def test_no_directory_means_no_probe(self):
        # Section 14: a source that is not wired up is not invented.
        cfg = fx.config(sources={"line_refresh_output_dir": ""})
        p = health.probe_line_refresh(cfg, fx.clock_at("2026-09-04T12:00"))
        self.assertTrue(p.inconclusive)

    def write_runs(self, tmp, statuses):
        """One report file per run, oldest first, with distinct mtimes."""
        import os
        import time

        for i, status in enumerate(statuses):
            path = os.path.join(tmp, f"run_{i}.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(f"# Cron Job\n\n**Status:** {status}\n")
            os.utime(path, (time.time() + i, time.time() + i))
        return tmp

    def test_one_failed_run_is_not_yet_an_incident(self):
        # Section 35. A daily job that failed once may well succeed tomorrow;
        # it is recorded as inconclusive so it neither alarms nor closes an
        # episode that is already open.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            self.write_runs(tmp, ["completed", "completed", "script failed"])
            cfg = fx.config(sources={"line_refresh_output_dir": tmp})
            p = health.probe_line_refresh(cfg, fx.clock_at("2026-09-04T12:00"))
            self.assertTrue(p.ok)
            self.assertTrue(p.inconclusive)

    def test_two_consecutive_failures_are_an_incident(self):
        # The condition found on this host: three consecutive failed runs of
        # line_refresh_daily.sh, with the LINE index unchanged since 2026-09-01.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            self.write_runs(tmp, ["completed", "script failed", "script failed"])
            cfg = fx.config(sources={"line_refresh_output_dir": tmp})
            p = health.probe_line_refresh(cfg, fx.clock_at("2026-09-04T12:00"))
            self.assertFalse(p.ok)
            self.assertEqual(p.reason, "script_failed")
            self.assertEqual(p.facts["consecutive_failures"], 2)

    def test_a_failed_daily_job_is_reported_within_the_hour(self):
        # The per-reason threshold. Waiting the two days that "the job has not
        # run at all" deserves would be the wrong instrument for a job that
        # has told us it failed.
        cfg = fx.config()
        self.assertEqual(cfg.sustained_seconds("line_refresh"), 172800)
        self.assertEqual(
            cfg.sustained_seconds("line_refresh", "script_failed"), 3600)

    def test_a_recent_success_is_healthy(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "run.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("**Status:** completed\nrefresh ok\n")
            cfg = fx.config(sources={"line_refresh_output_dir": tmp})
            p = health.probe_line_refresh(cfg, fx.clock_at("2026-09-04T12:00"))
            self.assertTrue(p.ok, p.reason)


class TestGmailProbe(unittest.TestCase):
    def make_index(self, tmpdir, *, auth="AUTHORIZED", expiry=None, pending=None):
        import os
        import sqlite3

        path = os.path.join(tmpdir, f"gmail-{auth}-{expiry}-{pending}.db")
        con = sqlite3.connect(path)
        con.execute(
            """CREATE TABLE gmail_status (
              singleton INTEGER PRIMARY KEY, auth_state TEXT, history_id_present INTEGER,
              initialized_at TEXT,last_full_sync_at TEXT,last_incremental_sync_at TEXT,
              last_api_success_at TEXT,last_message_ingested_at TEXT,last_error_code TEXT,
              consecutive_failures INTEGER,history_reset_count INTEGER,message_count INTEGER,
              thread_count INTEGER,oldest_received_at TEXT,newest_received_at TEXT,
              watch_state TEXT,watch_expiration_ms INTEGER,last_notification_at TEXT,
              pending_sync_age_seconds INTEGER,published_at TEXT
            )"""
        )
        con.execute(
            "INSERT INTO gmail_status VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (1, auth, 1, None, None, None, None, None, None, 0, 0, 0, 0, None, None,
             "ACTIVE", expiry, None, pending, "2026-09-04T00:00:00+00:00"),
        )
        con.commit(); con.close()
        return path

    def test_authorization_and_progress_failures_are_measurable(self):
        import tempfile

        clock = fx.clock_at("2026-09-04T12:00")
        with tempfile.TemporaryDirectory() as tmp:
            path = self.make_index(tmp, auth="AUTH_REQUIRED")
            p = health.probe_gmail(fx.config(sources={"gmail_index_db": path}), clock)
            self.assertFalse(p.ok)
            self.assertEqual(p.reason, "gmail_auth_required")
            path = self.make_index(tmp, pending=3601)
            p = health.probe_gmail(fx.config(sources={"gmail_index_db": path}), clock)
            self.assertFalse(p.ok)
            self.assertEqual(p.reason, "gmail_sync_stuck")

    def test_pending_setup_is_inconclusive_not_an_alert(self):
        p = health.probe_gmail(
            fx.config(sources={"gmail_index_db": "/not-configured/gmail.db"}),
            fx.clock_at("2026-09-04T12:00"),
        )
        self.assertTrue(p.ok)
        self.assertTrue(p.inconclusive)


if __name__ == "__main__":
    unittest.main()
