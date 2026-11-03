"""The durable wait on Hermes: late answers, restarts, heartbeats, concurrency.

2026-10-27: two owner DMs needing calendar and mail lookups ran past the old
240 s limit. Each got "応答に時間がかかりすぎたため中断しました" while its
Hermes run carried on and its finished answer was thrown away. These tests pin
the replacement: the run id is stored before the first poll, the deadline is
the run's own, a late answer is the one reply, and the at-most-once reply
ledger is untouched.

Everything is synthetic: a fake Hermes, a fake Discord bot and a fake clock
that the fake Hermes advances, so an eight-minute wait runs in milliseconds.
"""

import asyncio
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

import gw_fixtures as fx
import gw_store as store
from gw_canonical import CanonicalMessage
from gw_config import ConfigError, parse_config
from gw_format import USER_FACING_TIMEOUT, USER_FACING_UNAVAILABLE
from gw_hermes import HermesRejected, HermesUnavailable
from gw_schema import open_db
from gw_service import Gateway

CONV = "discord:" + fx.DM_CHANNEL_ID


class Clock:
    def __init__(self):
        self.now = datetime.now(timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class FakeHermes:
    """Scripted Hermes. Each get_run consumes one step and advances the clock."""

    def __init__(self, clock, steps, *, step_seconds=30, run_ids=("run-1", "run-2")):
        self.clock = clock
        self.steps = list(steps)
        self.step_seconds = step_seconds
        self.run_ids = list(run_ids)
        self.started = []
        self.polled = []
        self.on_poll = None

    def start_run(self, envelope, text, *, session_id, request_id):
        self.started.append(request_id)
        return self.run_ids[min(len(self.started) - 1, len(self.run_ids) - 1)], False

    def get_run(self, run_id, *, timeout=None):
        self.polled.append(run_id)
        if self.on_poll:
            self.on_poll(len(self.polled))
        self.clock.advance(self.step_seconds)
        step = self.steps.pop(0) if self.steps else {"status": "running"}
        if isinstance(step, BaseException):
            raise step
        return step


class Bot:
    def __init__(self):
        self.calls = []
        self.typing = []
        self.typing_error = None

    def prepare(self, text):
        return [text] if text else []

    async def send_chunks(self, channel, chunks):
        self.calls.append((channel, list(chunks)))
        return 1, None

    async def typing_once(self, channel_id):
        self.typing.append(channel_id)
        if self.typing_error:
            raise self.typing_error
        return True


def make_gateway(path, *, hermes_steps=(), step_seconds=30, clock=None, **hermes_cfg):
    cfg = parse_config(fx.config_dict(
        paths={"state_db": str(path)},
        hermes={"base_url": "http://127.0.0.1:8642", "poll_interval_seconds": 0.01,
                **hermes_cfg}))
    gw = Gateway(cfg, {"hermes_api_key": "synthetic-test-key"})
    gw.clock = clock or Clock()
    gw.hermes = FakeHermes(gw.clock, hermes_steps, step_seconds=step_seconds)
    gw.discord_bot = Bot()
    return gw


def seed(gw, request_id="req-1", conversation_id=CONV, platform="discord", claim=True):
    msg = CanonicalMessage(
        message_id=request_id, platform=platform, platform_message_id=request_id,
        platform_user_id=fx.OWNER_DISCORD if platform == "discord" else fx.OWNER_LINE,
        conversation_id=conversation_id, timestamp="2026-10-27T04:11:00+00:00",
        text="明日の撮影ってどういうスケジュール感だっけ", authz_subject="owner",
        capability_profile="personal-chat", gateway_request_id=request_id,
        received_at=datetime.now(timezone.utc).isoformat())
    con = gw.db()
    try:
        store.record_inbound(con, msg)
        store.enqueue(con, request_id)
        con.commit()
        row = store.claim_next(con, max_active=8) if claim else None
        con.commit()
        return row
    finally:
        con.close()


def inbound(gw, request_id="req-1"):
    con = gw.db()
    try:
        return store.get_inbound(con, request_id)
    finally:
        con.close()


def replies(gw, request_id="req-1"):
    con = gw.db()
    try:
        return [r["body"] for r in store.outbound_for_request(con, request_id)]
    finally:
        con.close()


def metric(gw, key):
    con = gw.db()
    try:
        row = con.execute("SELECT value FROM metrics WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None
    finally:
        con.close()


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------ late answers


def test_an_answer_after_four_minutes_is_delivered_not_thrown_away(tmp_path):
    # Nine polls of 30 s: the answer lands at 270 s, past the old 240 s limit.
    steps = [{"status": "running"}] * 8 + [{"status": "completed", "output": "明日は10時集合です"}]
    gw = make_gateway(tmp_path / "gw.db", hermes_steps=steps)
    row = seed(gw)
    run(gw.process(row))
    assert replies(gw) == ["明日は10時集合です"]
    assert gw.discord_bot.calls == [(fx.DM_CHANNEL_ID, ["明日は10時集合です"])]
    current = inbound(gw)
    assert current["state"] == "RESPONDED"
    assert current["hermes_run_id"] == "run-1"
    assert metric(gw, "last_hermes_success_at") is not None
    assert gw.hermes.started == ["req-1"]


def test_the_run_is_recorded_before_the_first_status_read(tmp_path):
    gw = make_gateway(tmp_path / "gw.db", hermes_steps=[{"status": "completed", "output": "ok"}])
    row = seed(gw)
    seen = {}

    def check(n):
        current = inbound(gw)
        seen["run"] = current["hermes_run_id"]
        seen["started"] = current["run_started_at"]

    gw.hermes.on_poll = check
    run(gw.process(row))
    assert seen["run"] == "run-1" and seen["started"]


def test_the_deadline_ends_the_wait_with_one_timeout_reply(tmp_path):
    gw = make_gateway(tmp_path / "gw.db", hermes_steps=[{"status": "running"}] * 40)
    row = seed(gw)
    run(gw.process(row))
    assert replies(gw) == [USER_FACING_TIMEOUT]
    assert "分けて" not in USER_FACING_TIMEOUT
    # 30 s per read: the wait used the whole 480 s allowance and no more
    # (one extra read is clock skew between the test clock and the row).
    assert 480 <= len(gw.hermes.polled) * 30 <= 510
    assert metric(gw, "last_hermes_failure_at") is not None


def test_a_terminal_failure_and_an_empty_answer_are_internal_errors(tmp_path):
    for i, step in enumerate(({"status": "failed"}, {"status": "completed", "output": "  "},
                              {"status": "waiting_for_approval"})):
        gw = make_gateway(tmp_path / f"gw{i}.db", hermes_steps=[step])
        run(gw.process(seed(gw)))
        assert replies(gw) == ["処理中に内部エラーが発生しました。もう一度お試しください。"]


# ----------------------------------------------------------------- restarts


def stop_mid_wait(db, polls=3):
    """A gateway that stops while waiting: the row stays PROCESSING, freshly heartbeated."""
    gw = make_gateway(db, hermes_steps=[{"status": "running"}] * polls)
    row = seed(gw)
    gw.hermes.on_poll = lambda n: gw.stop.set() if n == polls else None
    run(gw.process(row))
    assert replies(gw) == []
    assert inbound(gw)["state"] == "PROCESSING"
    return gw


def claim(gw, max_active=2):
    con = gw.db()
    try:
        row = store.claim_next(con, max_active=max_active)
        con.commit()
        return row
    finally:
        con.close()


def test_a_restart_resumes_the_same_run_without_asking_again(tmp_path):
    db = tmp_path / "gw.db"
    stop_mid_wait(db)
    restarted = make_gateway(db, hermes_steps=[{"status": "completed", "output": "再開後の答え"}])
    # The claim is seconds old, well inside the 300 s lease: only the startup
    # recovery may hand it back, and it must do so at once.
    assert run(restarted.recover_previous_process())["requeued"] == 1
    run(restarted.process(claim(restarted)))
    assert restarted.hermes.started == []
    assert restarted.hermes.polled == ["run-1"]
    assert replies(restarted) == ["再開後の答え"]


class DecliningCodingRoute:
    """The J.1 coding route, declining every turn as it does for a plain question."""

    def __init__(self):
        self.offered = []

    async def handle(self, message):
        self.offered.append(message.gateway_request_id)
        return None


def test_a_resumed_run_is_not_offered_to_the_coding_route_again(tmp_path):
    # Merging PR #9 with the recovered J.1 route: the route declined this turn
    # before its run started, so a restart waits on that run instead of
    # asking the route a second time.
    db = tmp_path / "gw.db"
    stop_mid_wait(db)
    restarted = make_gateway(db, hermes_steps=[{"status": "completed", "output": "再開後の答え"}])
    restarted.coding = DecliningCodingRoute()
    run(restarted.recover_previous_process())
    run(restarted.process(claim(restarted)))
    assert restarted.coding.offered == []
    assert restarted.hermes.started == []
    assert replies(restarted) == ["再開後の答え"]


def test_a_new_turn_is_offered_to_the_coding_route_first(tmp_path):
    gw = make_gateway(tmp_path / "gw.db",
                      hermes_steps=[{"status": "completed", "output": "答え"}])
    gw.coding = DecliningCodingRoute()
    run(gw.process(seed(gw)))
    assert gw.coding.offered == ["req-1"]
    assert gw.hermes.started == ["req-1"]
    assert replies(gw) == ["答え"]


def test_a_run_that_finished_during_a_long_restart_is_still_delivered(tmp_path):
    # Review of this change: a wait resumed past its deadline answered with
    # the timeout text without reading the run once, though Hermes had the
    # answer ready.
    db = tmp_path / "gw.db"
    gw = stop_mid_wait(db)
    con = gw.db()
    late = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    con.execute("UPDATE inbound_messages SET run_started_at = ?", (late,))
    con.commit()
    con.close()
    restarted = make_gateway(db, hermes_steps=[{"status": "completed", "output": "停止中に出た答え"}])
    run(restarted.recover_previous_process())
    run(restarted.process(claim(restarted)))
    assert restarted.hermes.polled == ["run-1"]
    assert replies(restarted) == ["停止中に出た答え"]
    assert metric(restarted, "last_hermes_success_at") is not None
    assert metric(restarted, "last_hermes_failure_at") is None


def test_a_run_still_going_after_its_deadline_is_read_once_then_timed_out(tmp_path):
    db = tmp_path / "gw.db"
    gw = stop_mid_wait(db)
    con = gw.db()
    late = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    con.execute("UPDATE inbound_messages SET run_started_at = ?", (late,))
    con.commit()
    con.close()
    restarted = make_gateway(db, hermes_steps=[{"status": "running"}] * 5)
    run(restarted.recover_previous_process())
    run(restarted.process(claim(restarted)))
    assert restarted.hermes.polled == ["run-1"]
    assert replies(restarted) == [USER_FACING_TIMEOUT]


def test_restarts_during_one_wait_do_not_use_up_its_attempts(tmp_path):
    # Second review: every startup requeue cost an attempt, so three restarts
    # during one long wait failed the request with no reply at all.
    db = tmp_path / "gw.db"
    gw = stop_mid_wait(db)
    run(gw.note_clean_shutdown())
    for _ in range(4):
        restarted = make_gateway(db)
        run(restarted.recover_previous_process())
        assert inbound(restarted)["state"] == "QUEUED"
        assert claim(restarted)["gateway_request_id"] == "req-1"
        run(restarted.note_clean_shutdown())   # systemctl restart: SIGTERM, clean stop
    assert inbound(gw)["attempts"] <= 2
    final = make_gateway(db, hermes_steps=[{"status": "completed", "output": "最後に届く答え"}])
    run(final.recover_previous_process())
    run(final.process(claim(final)))
    assert final.hermes.started == []
    assert replies(final) == ["最後に届く答え"]


def test_a_request_that_keeps_killing_the_process_still_ends(tmp_path):
    # Third review: refunding every resumed row let a request that crashes the
    # process (an OOM kill while handling its answer) loop forever, blocking
    # its conversation until systemd gave up on the whole gateway. A crash
    # writes no clean-shutdown marker, so each one counts.
    db = tmp_path / "gw.db"
    gw = stop_mid_wait(db)                      # run stored, attempts 1, then killed
    for _ in range(2):
        restarted = make_gateway(db)
        run(restarted.recover_previous_process())
        claim(restarted)                        # resumed ... and killed again
    run(make_gateway(db).recover_previous_process())
    current = inbound(gw)
    assert (current["state"], current["last_error"]) == ("FAILED", "max_attempts")


def test_the_clean_shutdown_marker_is_used_once(tmp_path):
    db = tmp_path / "gw.db"
    gw = stop_mid_wait(db)
    run(gw.note_clean_shutdown())
    restarted = make_gateway(db)
    run(restarted.recover_previous_process())
    assert metric(restarted, "clean_shutdown_at") is None
    assert inbound(restarted)["attempts"] == 0


def test_a_request_that_never_reached_hermes_still_counts_its_attempts(tmp_path):
    # The poison-message bound stays for rows with no run: claimed, then the
    # process died before Hermes was asked.
    db = tmp_path / "gw.db"
    gw = make_gateway(db)
    seed(gw)                                    # attempts 1, no run id
    for _ in range(2):
        restarted = make_gateway(db)
        run(restarted.recover_previous_process())
        claim(restarted)
    run(make_gateway(db).recover_previous_process())
    current = inbound(gw)
    assert (current["state"], current["last_error"]) == ("FAILED", "max_attempts")


def test_the_worker_claims_nothing_until_startup_has_finished(tmp_path):
    gw = make_gateway(tmp_path / "gw.db")
    seed(gw, claim=False)

    async def scenario():
        gw.started = asyncio.Event()
        worker = asyncio.create_task(gw.worker())
        await asyncio.sleep(0.2)
        assert inbound(gw)["state"] == "QUEUED"     # a failing start takes nothing
        gw.stop.set()
        gw.wake.set()
        await asyncio.wait_for(worker, 10)

    run(scenario())
    assert inbound(gw)["state"] == "QUEUED"


def test_a_forgotten_run_past_its_deadline_is_not_started_again(tmp_path):
    db = tmp_path / "gw.db"
    gw = stop_mid_wait(db)
    con = gw.db()
    late = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    con.execute("UPDATE inbound_messages SET run_started_at = ?", (late,))
    con.commit()
    con.close()
    restarted = make_gateway(db, hermes_steps=[HermesRejected("run_not_found")])
    run(restarted.recover_previous_process())
    run(restarted.process(claim(restarted)))
    assert restarted.hermes.started == []
    assert replies(restarted) == [USER_FACING_TIMEOUT]


def test_requests_left_by_a_stopped_process_do_not_hold_every_slot(tmp_path):
    db = tmp_path / "gw.db"
    gw = make_gateway(db)
    for rid, conv, platform in (("dm-1", CONV, "discord"),
                                ("line-1", "line:" + fx.OWNER_LINE, "line")):
        seed(gw, rid, conversation_id=conv, platform=platform, claim=False)
    for rid in ("dm-1", "line-1"):
        row = claim(gw)
        con = gw.db()
        store.note_run_started(con, row["gateway_request_id"], "run-" + rid)
        con.commit()
        con.close()
    seed(gw, "dm-other", conversation_id="discord:other", claim=False)
    assert claim(gw) is None            # both slots held by the dead process
    restarted = make_gateway(db)
    assert run(restarted.recover_previous_process())["requeued"] == 2
    picked = {claim(restarted)["gateway_request_id"], claim(restarted)["gateway_request_id"]}
    assert picked == {"dm-1", "line-1"}  # oldest first, each resuming its run
    assert inbound(restarted, "dm-1")["hermes_run_id"] == "run-dm-1"


def test_the_deadline_is_the_runs_own_across_a_restart(tmp_path):
    db = tmp_path / "gw.db"
    gw = make_gateway(db, hermes_steps=[{"status": "running"}] * 30)
    row = seed(gw)
    con = gw.db()
    store.note_run_started(con, "req-1", "run-1")
    started = (datetime.now(timezone.utc) - timedelta(seconds=470)).isoformat()
    con.execute("UPDATE inbound_messages SET run_started_at = ?", (started,))
    con.commit()
    con.close()
    row = inbound(gw)
    run(gw.process(row))
    assert gw.hermes.started == []
    # 10 s of allowance left: one 30 s read and the deadline is passed.
    assert len(gw.hermes.polled) == 1
    assert replies(gw) == [USER_FACING_TIMEOUT]


def test_a_delivered_reply_is_never_regenerated(tmp_path):
    gw = make_gateway(tmp_path / "gw.db", hermes_steps=[{"status": "completed", "output": "a"}])
    row = seed(gw)
    run(gw.process(row))
    gw.hermes.steps = [{"status": "completed", "output": "b"}]
    run(gw.process(inbound(gw)))
    assert gw.hermes.started == ["req-1"]
    assert replies(gw) == ["a"]


# --------------------------------------------------------------- heartbeat


def test_the_heartbeat_keeps_a_live_wait_from_being_reclaimed(tmp_path):
    gw = make_gateway(tmp_path / "gw.db", step_seconds=61,
                      hermes_steps=[{"status": "running"}] * 6 + [{"status": "completed", "output": "ok"}])
    row = seed(gw)
    claims = []
    gw.hermes.on_poll = lambda n: claims.append(inbound(gw)["claimed_at"])
    run(gw.process(row))
    assert len(set(claims)) >= 5  # renewed as the wait went on
    assert replies(gw) == ["ok"]


def test_a_lost_claim_stops_the_wait_without_a_reply(tmp_path):
    gw = make_gateway(tmp_path / "gw.db", step_seconds=61,
                      hermes_steps=[{"status": "running"}] * 10 + [{"status": "completed", "output": "late"}])
    row = seed(gw)

    def take_over(n):
        if n == 2:
            con = gw.db()
            con.execute("UPDATE inbound_messages SET state = 'QUEUED'")
            con.commit()
            con.close()

    gw.hermes.on_poll = take_over
    run(gw.process(row))
    assert replies(gw) == []
    assert gw.discord_bot.calls == []


# ------------------------------------------------------------ flaky status


def test_a_short_status_outage_is_waited_through(tmp_path):
    steps = [HermesUnavailable("URLError"), HermesUnavailable("URLError"),
             {"status": "completed", "output": "復旧後の答え"}]
    gw = make_gateway(tmp_path / "gw.db", hermes_steps=steps)
    run(gw.process(seed(gw)))
    assert replies(gw) == ["復旧後の答え"]


def test_a_long_status_outage_ends_with_the_unavailable_text_once(tmp_path):
    gw = make_gateway(tmp_path / "gw.db", hermes_steps=[HermesUnavailable("URLError")] * 20)
    run(gw.process(seed(gw)))
    assert replies(gw) == [USER_FACING_UNAVAILABLE]
    # 90 s of grace at 30 s per read: the first failure plus three more.
    assert len(gw.hermes.polled) <= 5


def test_a_forgotten_run_is_restarted_once_under_the_same_key(tmp_path):
    steps = [HermesRejected("run_not_found"), {"status": "completed", "output": "二度目"}]
    gw = make_gateway(tmp_path / "gw.db", hermes_steps=steps)
    run(gw.process(seed(gw)))
    assert gw.hermes.started == ["req-1", "req-1"]
    assert gw.hermes.polled == ["run-1", "run-2"]
    assert replies(gw) == ["二度目"]
    assert inbound(gw)["hermes_run_id"] == "run-2"


def test_a_forgotten_run_hermes_cannot_restart_gets_the_unavailable_text(tmp_path):
    class Warming(FakeHermes):
        def start_run(self, envelope, text, *, session_id, request_id):
            if self.started:
                self.started.append(request_id)
                raise HermesUnavailable("http_503")
            return super().start_run(envelope, text, session_id=session_id,
                                     request_id=request_id)

    gw = make_gateway(tmp_path / "gw.db")
    gw.hermes = Warming(gw.clock, [HermesRejected("run_not_found")])
    run(gw.process(seed(gw)))
    assert replies(gw) == [USER_FACING_UNAVAILABLE]


def test_a_failed_heartbeat_write_does_not_end_the_wait(tmp_path, monkeypatch):
    gw = make_gateway(tmp_path / "gw.db", step_seconds=61,
                      hermes_steps=[{"status": "running"}] * 3 + [{"status": "completed", "output": "ok"}])
    real, calls = store.touch_claim, []

    def flaky(con, request_id):
        calls.append(request_id)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(con, request_id)

    monkeypatch.setattr(store, "touch_claim", flaky)
    run(gw.process(seed(gw)))
    assert len(calls) >= 2
    assert replies(gw) == ["ok"]


def test_a_run_forgotten_twice_is_a_failure(tmp_path):
    steps = [HermesRejected("run_not_found"), HermesRejected("run_not_found")]
    gw = make_gateway(tmp_path / "gw.db", hermes_steps=steps)
    run(gw.process(seed(gw)))
    assert len(gw.hermes.started) == 2
    assert replies(gw) == ["処理中に内部エラーが発生しました。もう一度お試しください。"]


# ------------------------------------------------------------------- typing


def test_discord_shows_typing_while_waiting(tmp_path):
    steps = [{"status": "running"}] * 5 + [{"status": "completed", "output": "ok"}]
    gw = make_gateway(tmp_path / "gw.db", hermes_steps=steps)
    run(gw.process(seed(gw)))
    # One pulse per 30 s read (the pulse interval is 8 s).
    assert gw.discord_bot.typing == [fx.DM_CHANNEL_ID] * 6


def test_a_failing_typing_indicator_changes_nothing(tmp_path):
    steps = [{"status": "running"}, {"status": "completed", "output": "ok"}]
    gw = make_gateway(tmp_path / "gw.db", hermes_steps=steps)
    gw.discord_bot.typing_error = RuntimeError("synthetic")
    run(gw.process(seed(gw)))
    assert replies(gw) == ["ok"]


def test_typing_can_be_switched_off(tmp_path):
    cfg = parse_config(fx.config_dict(paths={"state_db": str(tmp_path / "gw.db")},
                                      discord={"enabled": True, "typing_indicator": False},
                                      hermes={"poll_interval_seconds": 0.01}))
    gw = Gateway(cfg, {"hermes_api_key": "synthetic-test-key"})
    gw.clock = Clock()
    gw.hermes = FakeHermes(gw.clock, [{"status": "completed", "output": "ok"}])
    gw.discord_bot = Bot()
    run(gw.process(seed(gw)))
    assert gw.discord_bot.typing == []


# -------------------------------------------------------------- concurrency


def test_claim_next_is_atomic_across_connections(tmp_path):
    gw = make_gateway(tmp_path / "gw.db")
    seed(gw, claim=False)
    barrier = threading.Barrier(2)
    results = []

    def claim():
        con = open_db(gw.cfg.state_db)
        try:
            barrier.wait()
            row = store.claim_next(con, max_active=8)
            con.commit()
            results.append(row["gateway_request_id"] if row else None)
        finally:
            con.close()

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results, key=str) == [None, "req-1"]
    assert inbound(gw)["attempts"] == 1


def test_a_lost_race_is_reported_as_no_claim(tmp_path):
    gw = make_gateway(tmp_path / "gw.db")
    seed(gw, claim=False)
    con = gw.db()
    try:
        # Opening the database writes the meta row, which is why the old
        # `total_changes == 0` guard could never fire.
        assert con.total_changes >= 1
        original = con.execute

        class Raced:
            def __init__(self, inner):
                self.inner = inner

            def __getattr__(self, name):
                return getattr(self.inner, name)

            def execute(self, sql, *args):
                if sql.startswith("UPDATE inbound_messages SET state='PROCESSING'"):
                    original("UPDATE inbound_messages SET state = 'CANCELLED'")
                return original(sql, *args)

        assert store.claim_next(Raced(con), max_active=8) is None
    finally:
        con.close()


def test_two_conversations_wait_on_hermes_at_the_same_time(tmp_path):
    gw = make_gateway(tmp_path / "gw.db")
    seed(gw, "dm-1", claim=False)
    seed(gw, "line-1", conversation_id="line:" + fx.OWNER_LINE, platform="line", claim=False)
    seed(gw, "dm-2", claim=False)  # same conversation as dm-1: must wait for it
    in_flight, peak, order = set(), [0], []
    release = threading.Event()

    class Slow(FakeHermes):
        def start_run(self, envelope, text, *, session_id, request_id):
            return f"run-{request_id}", False

        def get_run(self, run_id, *, timeout=None):
            request_id = run_id[4:]
            in_flight.add(request_id)
            peak[0] = max(peak[0], len(in_flight))
            if request_id != "dm-2":
                release.wait(timeout=0.5)
            in_flight.discard(request_id)
            order.append(request_id)
            return {"status": "completed", "output": f"answer {request_id}"}

    gw.hermes = Slow(gw.clock, [])
    gw.line = None  # LINE delivery is not under test; it settles as not configured

    async def scenario():
        worker = asyncio.create_task(gw.worker())
        for _ in range(300):
            await asyncio.sleep(0.01)
            if len(order) >= 3:
                break
        release.set()
        gw.stop.set()
        gw.wake.set()
        await asyncio.wait_for(worker, 10)

    run(scenario())
    assert peak[0] == 2               # the DM and LINE waited concurrently
    assert order.index("dm-2") > order.index("dm-1")   # one conversation stays in order
    assert sorted(order) == ["dm-1", "dm-2", "line-1"]


def peak_concurrency(tmp_path, limit):
    cfg = parse_config(fx.config_dict(paths={"state_db": str(tmp_path / "gw.db")},
                                      limits={"max_active_requests": limit},
                                      hermes={"poll_interval_seconds": 0.01}))
    gw = Gateway(cfg, {"hermes_api_key": "synthetic-test-key"})
    gw.clock = Clock()
    gw.discord_bot = Bot()
    seed(gw, "dm-1", claim=False)
    seed(gw, "line-1", conversation_id="line:" + fx.OWNER_LINE, platform="line", claim=False)
    in_flight, peak, done = set(), [0], []
    lock = threading.Lock()

    class Counting(FakeHermes):
        def start_run(self, envelope, text, *, session_id, request_id):
            return f"run-{request_id}", False

        def get_run(self, run_id, *, timeout=None):
            with lock:
                in_flight.add(run_id)
                peak[0] = max(peak[0], len(in_flight))
            # Held long enough that two concurrent requests always overlap.
            threading.Event().wait(0.3)
            with lock:
                in_flight.discard(run_id)
                done.append(run_id)
            return {"status": "completed", "output": "x"}

    gw.hermes = Counting(gw.clock, [])
    gw.line = None

    async def scenario():
        worker = asyncio.create_task(gw.worker())
        for _ in range(300):
            await asyncio.sleep(0.01)
            if len(done) >= 2:
                break
        gw.stop.set()
        gw.wake.set()
        await asyncio.wait_for(worker, 10)

    run(scenario())
    assert len(done) == 2
    return peak[0]


def test_max_active_one_restores_strictly_serial_work(tmp_path):
    assert peak_concurrency(tmp_path, 1) == 1


def test_max_active_two_really_overlaps(tmp_path):
    # The control that shows the test above can fail.
    assert peak_concurrency(tmp_path, 2) == 2


def test_shutdown_leaves_a_waiting_request_recoverable(tmp_path):
    gw = make_gateway(tmp_path / "gw.db", hermes_steps=[{"status": "running"}] * 1000,
                      step_seconds=0.001)
    seed(gw, claim=False)

    async def scenario():
        worker = asyncio.create_task(gw.worker())
        for _ in range(200):
            await asyncio.sleep(0.01)
            if gw.hermes.polled:
                break
        gw.stop.set()
        gw.wake.set()
        await asyncio.wait_for(worker, 10)

    run(scenario())
    current = inbound(gw)
    assert current["state"] == "PROCESSING"
    assert current["hermes_run_id"] == "run-1"
    assert replies(gw) == []


# ------------------------------------------------------------------- config


def test_wait_settings_are_bounded():
    good = parse_config(fx.config_dict())
    assert good.hermes["absolute_timeout_seconds"] == 480
    for hermes in ({"absolute_timeout_seconds": 900}, {"absolute_timeout_seconds": 10},
                   {"heartbeat_seconds": 200}, {"poll_request_timeout_seconds": 0},
                   {"poll_interval_seconds": 0}, {"unavailable_grace_seconds": 600,
                                                  "absolute_timeout_seconds": 300},
                   {"heartbeat_seconds": True}, {"unknown_key": 1}):
        with pytest.raises(ConfigError):
            parse_config(fx.config_dict(hermes={"base_url": "http://127.0.0.1:8642", **hermes}))
