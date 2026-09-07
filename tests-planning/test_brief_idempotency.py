"""One brief per local date — phase sections 57, 58, 88, 89.

Who is authoritative matters as much as the behaviour. The channel gateway is:
`proactive_deliveries` derives its PRIMARY KEY from `(notification_id, channel)`
and repeats the constraint as `UNIQUE INDEX idx_proactive_once`, inside the
only writer's single transaction (profile section H.4, I2-AMENDMENT G2). The
local `morning_brief_runs` table is a cheap pre-check and a record, not a
second lock — so these tests assert both that the pre-check saves the work
*and* that a gateway duplicate answer is honoured as success when the local
record is missing entirely.

Run with:  PYTHONPATH=src python -m pytest tests-planning -q
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from planning import brief_service, brief_state, delivery  # noqa: E402
from test_brief_delivery import (  # noqa: E402
    FakeClient, base_config, make_planner, make_renderer, no_events, stub_plan,
)

JST = ZoneInfo("Asia/Tokyo")


@pytest.fixture
def state():
    con = brief_state.open_state(":memory:")
    try:
        yield brief_state.BriefState(con)
    finally:
        con.close()


def run_at(config, state, client, when, *, trigger="timer", plan=None,
           planner=None, renderer=None):
    return brief_service.run_scheduled_brief(
        config, now=when, trigger=trigger, state=state, client=client,
        planner=planner or make_planner(plan),
        renderer=renderer or make_renderer(),
        open_events=no_events, sleep=lambda _s: None)


# --- phase section 88: the same day, twice -------------------------------

def test_same_day_double_trigger_delivers_exactly_once(tmp_path, state):
    config = base_config(tmp_path)
    client = FakeClient()
    morning = datetime(2026, 9, 8, 8, 30, tzinfo=JST)

    first = run_at(config, state, client, morning)
    second = run_at(config, state, client, morning + timedelta(minutes=5))

    assert first["status"] == brief_service.ST_DELIVERED
    assert second["status"] == brief_service.ST_SUPPRESSED
    assert second["failure_reason"] == brief_service.FR_DUPLICATE
    # The whole point: one request reached the gateway, not two.
    assert len(client.calls) == 1
    assert client.calls[0]["notification_id"] == "morning-brief:2026-09-08"


def test_the_pre_check_skips_the_work_not_just_the_send(tmp_path, state):
    """A second trigger must not rebuild the plan or call the renderer."""
    config = base_config(tmp_path)
    client = FakeClient()
    morning = datetime(2026, 9, 8, 8, 30, tzinfo=JST)
    run_at(config, state, client, morning)

    planner_calls, renderer_calls = [], []

    def counting_planner(conn, **kw):
        planner_calls.append(kw)
        return stub_plan()

    def counting_renderer(plan, **kw):
        renderer_calls.append(kw)
        return {"text": "x", "renderer": "template", "model": None,
                "fallback_reason": None, "grounding": {"ok": True,
                                                       "violations": []}}

    second = run_at(config, state, client, morning + timedelta(hours=1),
                    trigger="manual", planner=counting_planner,
                    renderer=counting_renderer)
    assert second["status"] == brief_service.ST_SUPPRESSED
    assert planner_calls == []
    assert renderer_calls == []


def test_manual_trigger_does_not_bypass_idempotency(tmp_path, state):
    """`--trigger manual` bypasses the late-boot skip and nothing else."""
    config = base_config(tmp_path)
    client = FakeClient()
    run_at(config, state, client, datetime(2026, 9, 8, 8, 30, tzinfo=JST))
    later = run_at(config, state, client,
                   datetime(2026, 9, 8, 21, 0, tzinfo=JST), trigger="manual")
    assert later["status"] == brief_service.ST_SUPPRESSED
    assert len(client.calls) == 1


# --- phase section 89: the next day --------------------------------------

def test_next_day_generates_a_new_brief(tmp_path, state):
    config = base_config(tmp_path)
    client = FakeClient()
    day1 = datetime(2026, 9, 8, 8, 30, tzinfo=JST)
    day2 = datetime(2026, 9, 9, 8, 30, tzinfo=JST)

    first = run_at(config, state, client, day1,
                   plan=stub_plan(date="2026-09-08", plan_hash="h-08"))
    second = run_at(config, state, client, day2,
                    plan=stub_plan(date="2026-09-09", plan_hash="h-09"))

    assert first["status"] == second["status"] == brief_service.ST_DELIVERED
    assert len(client.calls) == 2
    assert [c["notification_id"] for c in client.calls] == [
        "morning-brief:2026-09-08", "morning-brief:2026-09-09"]
    assert state.get("2026-09-08")["plan_hash"] == "h-08"
    assert state.get("2026-09-09")["plan_hash"] == "h-09"
    assert state.get("2026-09-08")["delivery_status"] == brief_state.DELIVERED


# --- the gateway is authoritative ----------------------------------------

def test_gateway_duplicate_answer_is_success_not_error(tmp_path, state):
    """No local record at all — e.g. planning.db was lost — and the gateway
    answers 200 `already_delivered`. That is the authority speaking."""
    config = base_config(tmp_path)
    client = FakeClient(delivery.Result(
        delivery.ALREADY_DELIVERED, detail="already_delivered",
        http_status=200, sent_at="2026-09-08T08:30:05+09:00"))

    outcome = run_at(config, state, client,
                     datetime(2026, 9, 8, 8, 30, tzinfo=JST))

    assert outcome["status"] == brief_service.ST_DELIVERED
    assert outcome["failure_reason"] is None
    assert outcome["attempts"] == 1          # not retried
    row = state.get("2026-09-08")
    assert row["delivery_status"] == brief_state.DELIVERED
    assert row["delivery_channel"] == "discord"
    health = brief_state.read_health(str(tmp_path))
    assert health["status"] == "ok"


def test_service_survives_a_missing_local_state_store(tmp_path):
    """Losing the pre-check must not lose the guarantee — the gateway keeps it."""
    config = base_config(tmp_path)
    # planning_dir points at a path that cannot be created: state degrades to
    # None and the run still completes.
    config["paths"]["planning_dir"] = str(tmp_path / "f" / "sub")
    (tmp_path / "f").write_text("not a directory", encoding="utf-8")
    client = FakeClient()
    outcome = brief_service.run_scheduled_brief(
        config, now=datetime(2026, 9, 8, 8, 30, tzinfo=JST),
        client=client, planner=make_planner(), renderer=make_renderer(),
        open_events=no_events, sleep=lambda _s: None)
    assert outcome["status"] == brief_service.ST_DELIVERED
    assert len(client.calls) == 1


# --- the key is the LOCAL date (phase section 58) ------------------------

@pytest.mark.parametrize("local,expected", [
    # 00:30 JST is still 15:30 of the PREVIOUS day in UTC. Keying off UTC
    # would give one owner-day two different notification ids.
    (datetime(2026, 9, 8, 0, 30, tzinfo=JST), "morning-brief:2026-09-08"),
    (datetime(2026, 9, 8, 8, 30, tzinfo=JST), "morning-brief:2026-09-08"),
    (datetime(2026, 9, 8, 23, 59, tzinfo=JST), "morning-brief:2026-09-08"),
    (datetime(2026, 9, 9, 0, 1, tzinfo=JST), "morning-brief:2026-09-09"),
])
def test_idempotency_key_uses_the_asia_tokyo_date(local, expected, tmp_path):
    config = base_config(tmp_path)
    assert brief_service.local_date_for(local, config) == expected.split(":", 1)[1]
    assert delivery.notification_id_for(
        brief_service.local_date_for(local, config)) == expected


def test_a_utc_clock_still_resolves_to_the_tokyo_date(tmp_path, state):
    """The service is handed `now` in UTC. It must still key off Tokyo."""
    config = base_config(tmp_path)
    client = FakeClient()
    utc_now = datetime(2026, 9, 8, 0, 30, tzinfo=ZoneInfo("UTC"))  # 09:30 JST
    outcome = run_at(config, state, client, utc_now)
    assert outcome["local_date"] == "2026-09-08"
    assert client.calls[0]["notification_id"] == "morning-brief:2026-09-08"


# --- the local record itself ---------------------------------------------

def test_state_row_shape_matches_the_contract(tmp_path, state):
    config = base_config(tmp_path)
    run_at(config, state, FakeClient(), datetime(2026, 9, 8, 8, 30, tzinfo=JST))
    row = state.get("2026-09-08")
    assert set(row.keys()) == {
        "local_date", "plan_hash", "generated_at", "delivered_at",
        "delivery_channel", "delivery_status", "attempts", "last_error",
    }
    assert row["delivered_at"] is not None
    assert row["attempts"] == 1


def test_regenerating_a_plan_keeps_the_attempt_budget(state):
    """A restart re-plans; it must not reset the bounded retry counter."""
    state.begin_run("2026-09-08", "h1")
    state.note_attempt("2026-09-08")
    state.note_attempt("2026-09-08")
    state.begin_run("2026-09-08", "h2")
    assert state.attempts("2026-09-08") == 2
    assert state.get("2026-09-08")["plan_hash"] == "h2"


def test_begin_run_never_downgrades_a_delivered_date(state):
    state.begin_run("2026-09-08", "h1")
    state.mark_delivered("2026-09-08", channel="discord")
    state.begin_run("2026-09-08", "h2")
    assert state.is_settled("2026-09-08")
    assert state.get("2026-09-08")["delivery_status"] == brief_state.DELIVERED


def test_prune_bounds_the_table(state):
    for day in range(1, 21):
        state.begin_run(f"2026-09-{day:02d}", "h")
    state.prune(keep_days=5)
    rows = state.con.execute(
        "SELECT local_date FROM morning_brief_runs ORDER BY local_date").fetchall()
    assert [r[0] for r in rows] == [f"2026-09-{d}" for d in
                                    ("16", "17", "18", "19", "20")]


def test_disabled_brief_sends_nothing(tmp_path, state):
    config = base_config(tmp_path, enabled=False)
    client = FakeClient()
    outcome = run_at(config, state, client,
                     datetime(2026, 9, 8, 8, 30, tzinfo=JST))
    assert outcome["status"] == brief_service.ST_DISABLED
    assert client.calls == []
