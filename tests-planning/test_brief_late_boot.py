"""Late boot — phase sections 55, 56, 90.

The rule, stated once so the tests can pin it exactly:

    delta = now - (today's local date at morning_brief.time, in `timezone`)
    delta <= late_boot_grace_hours   ->  send
    delta >  late_boot_grace_hours   ->  skipped_late_boot

The boundary is INCLUSIVE on the sending side: phase section 56 reads
"grace window <= 3h: send". Both sides of it are tested, one second apart,
because "12:00 boot after an 08:30 slot" is the scenario the owner will
actually hit and an off-by-one there is either a brief at midnight or no brief
at all.

`trigger="manual"` bypasses this check and only this check. A `now` before the
scheduled time also sends: a `Persistent=true` timer firing seconds early
through clock skew is not a late boot.

Run with:  PYTHONPATH=src python -m pytest tests-planning -q
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from planning import brief_service, brief_state  # noqa: E402
from test_brief_delivery import (  # noqa: E402
    FakeClient, base_config, make_planner, make_renderer, no_events,
)

JST = ZoneInfo("Asia/Tokyo")
SLOT = datetime(2026, 9, 8, 8, 30, tzinfo=JST)


@pytest.fixture
def state():
    con = brief_state.open_state(":memory:")
    try:
        yield brief_state.BriefState(con)
    finally:
        con.close()


def run_at(config, state, client, when, *, trigger="timer"):
    return brief_service.run_scheduled_brief(
        config, now=when, trigger=trigger, state=state, client=client,
        planner=make_planner(), renderer=make_renderer(),
        open_events=no_events, sleep=lambda _s: None)


# --- the pure decision, both sides of the boundary -----------------------

@pytest.mark.parametrize("when,expect_skip", [
    (SLOT - timedelta(minutes=1), False),          # early: clock skew, not late
    (SLOT, False),                                 # on time
    (SLOT + timedelta(hours=2, minutes=59), False),
    (SLOT + timedelta(hours=3) - timedelta(seconds=1), False),
    (SLOT + timedelta(hours=3), False),            # exactly the grace: SEND
    (SLOT + timedelta(hours=3, seconds=1), True),  # one second later: SKIP
    (SLOT + timedelta(hours=3, minutes=30), True),  # the 12:00 boot case
    (SLOT + timedelta(hours=14), True),
])
def test_late_boot_boundary(when, expect_skip, tmp_path):
    config = base_config(tmp_path)
    should_skip, delta = brief_service.late_boot_decision(when, config)
    assert should_skip is expect_skip
    assert delta == pytest.approx((when - SLOT).total_seconds())


def test_the_documented_twelve_o_clock_boot_skips(tmp_path, state):
    """Phase section 56's own example: the Pi boots at 12:00, the 08:30 run
    was missed, the grace is 3h — 12:00 is 3h30m late, so nothing is sent."""
    config = base_config(tmp_path)
    client = FakeClient()
    outcome = run_at(config, state, client,
                     datetime(2026, 9, 8, 12, 0, tzinfo=JST))
    assert outcome["status"] == brief_service.ST_LATE_BOOT
    assert client.calls == []
    assert "late_by_seconds=12600" in outcome["detail"]
    assert "grace_hours=3" in outcome["detail"]


def test_a_boot_inside_the_window_still_sends(tmp_path, state):
    config = base_config(tmp_path)
    client = FakeClient()
    outcome = run_at(config, state, client,
                     datetime(2026, 9, 8, 11, 29, tzinfo=JST))
    assert outcome["status"] == brief_service.ST_DELIVERED
    assert len(client.calls) == 1


def test_skip_is_recorded_and_visible_in_health(tmp_path, state):
    config = base_config(tmp_path)
    run_at(config, state, FakeClient(), SLOT + timedelta(hours=5))
    row = state.get("2026-09-08")
    assert row["delivery_status"] == brief_state.SKIPPED
    assert row["last_error"].startswith("late_boot:")
    health = brief_state.read_health(str(tmp_path))
    assert health["status"] == "skipped"
    assert health["last_failure_reason"] is None


def test_a_skipped_day_is_not_settled_so_a_manual_run_still_works(tmp_path,
                                                                 state):
    """A late-boot skip must not look like a delivery. The owner can still ask
    for the brief by hand later the same day."""
    config = base_config(tmp_path)
    client = FakeClient()
    run_at(config, state, client, SLOT + timedelta(hours=5))
    assert not state.is_settled("2026-09-08")

    manual = run_at(config, state, client, SLOT + timedelta(hours=6),
                    trigger="manual")
    assert manual["status"] == brief_service.ST_DELIVERED
    assert len(client.calls) == 1


def test_manual_trigger_bypasses_the_window_entirely(tmp_path, state):
    config = base_config(tmp_path)
    client = FakeClient()
    outcome = run_at(config, state, client,
                     datetime(2026, 9, 8, 23, 0, tzinfo=JST), trigger="manual")
    assert outcome["status"] == brief_service.ST_DELIVERED


# --- the window is configurable ------------------------------------------

@pytest.mark.parametrize("grace,offset_hours,expect_skip", [
    (0, 0, False),
    (0.5, 0.4, False),
    (0.5, 0.6, True),
    (1, 2, True),
    (6, 5, False),
    (6, 7, True),
])
def test_grace_window_is_configurable(grace, offset_hours, expect_skip,
                                      tmp_path):
    config = base_config(tmp_path, late_boot_grace_hours=grace)
    when = SLOT + timedelta(hours=offset_hours)
    should_skip, _ = brief_service.late_boot_decision(when, config)
    assert should_skip is expect_skip


# --- the slot itself is read from the single config source ---------------

@pytest.mark.parametrize("configured,expected_hour,expected_minute", [
    ("08:30", 8, 30),
    ("08:00", 8, 0),
    ("07:05", 7, 5),
    ("23:59", 23, 59),
])
def test_scheduled_datetime_comes_from_the_config(configured, expected_hour,
                                                  expected_minute, tmp_path):
    config = base_config(tmp_path, time=configured)
    slot = brief_service.scheduled_datetime(SLOT, config)
    assert (slot.hour, slot.minute, slot.second) == (expected_hour,
                                                     expected_minute, 0)
    assert slot.tzinfo is not None
    assert slot.utcoffset().total_seconds() == 9 * 3600


@pytest.mark.parametrize("bad", ["8:30 am", "0830", "", "25:00", "08:61",
                                 None, "eight"])
def test_a_malformed_time_fails_loudly_and_sends_nothing(bad, tmp_path, state):
    config = base_config(tmp_path, time=bad)
    client = FakeClient()
    outcome = run_at(config, state, client, SLOT)
    assert outcome["status"] == brief_service.ST_FAILED
    assert outcome["failure_reason"] == brief_service.FR_PLANNER
    assert outcome["detail"].startswith("config_error:")
    assert client.calls == []
    row = state.get("2026-09-08")
    assert row["delivery_status"] == brief_state.FAILED
    assert row["last_error"].startswith("config_error:")
    assert brief_state.read_health(str(tmp_path))["status"] == "fail"


def test_an_unusable_timezone_fails_visibly_rather_than_guessing(tmp_path,
                                                                 state):
    """Guessing between UTC and JST would silently move the owner's morning."""
    config = base_config(tmp_path)
    config["timezone"] = "Mars/Olympus_Mons"
    client = FakeClient()
    outcome = brief_service.run_scheduled_brief(
        config, now=SLOT, state=state, client=client,
        planner=make_planner(), renderer=make_renderer(),
        open_events=no_events, sleep=lambda _s: None)
    assert outcome["status"] == brief_service.ST_FAILED
    assert outcome["failure_reason"] == brief_service.FR_PLANNER
    assert "timezone" in outcome["detail"]
    assert outcome["local_date"] is None
    assert client.calls == []
    assert brief_state.read_health(str(tmp_path))["status"] == "fail"


def test_the_outcome_dict_always_carries_the_same_keys(tmp_path, state):
    """The CLI and the status script read these by name; a branch that omits
    one turns a failure report into a KeyError."""
    expected = {"status", "failure_reason", "local_date", "trigger", "channel",
                "notification_id", "plan_hash", "renderer", "attempts",
                "warnings", "timings_ms", "detail"}
    cases = [
        (base_config(tmp_path), SLOT, "timer"),                      # delivered
        (base_config(tmp_path, enabled=False), SLOT, "timer"),       # disabled
        (base_config(tmp_path), SLOT + timedelta(hours=9), "timer"),  # late
        (base_config(tmp_path, time="nope"), SLOT, "timer"),         # bad time
    ]
    for config, when, trigger in cases:
        con = brief_state.open_state(":memory:")
        try:
            outcome = brief_service.run_scheduled_brief(
                config, now=when, trigger=trigger,
                state=brief_state.BriefState(con), client=FakeClient(),
                planner=make_planner(), renderer=make_renderer(),
                open_events=no_events, sleep=lambda _s: None)
            assert expected <= set(outcome), \
                f"missing {expected - set(outcome)} for {outcome['status']}"
            assert outcome["status"] in brief_service.ALL_STATUSES
        finally:
            con.close()

    bad_tz = base_config(tmp_path)
    bad_tz["timezone"] = "Nowhere/At_All"
    outcome = brief_service.run_scheduled_brief(
        bad_tz, now=SLOT, state=state, client=FakeClient(),
        planner=make_planner(), renderer=make_renderer(),
        open_events=no_events, sleep=lambda _s: None)
    assert expected <= set(outcome)


def test_the_slot_is_computed_in_tokyo_not_in_the_hosts_clock(tmp_path):
    """`now` in UTC must still be compared against 08:30 Asia/Tokyo."""
    config = base_config(tmp_path)
    utc = ZoneInfo("UTC")
    # 2026-09-07 23:30 UTC == 2026-09-08 08:30 JST: exactly on time.
    should_skip, delta = brief_service.late_boot_decision(
        datetime(2026, 9, 7, 23, 30, tzinfo=utc), config)
    assert should_skip is False
    assert delta == pytest.approx(0.0)
    # 2026-09-08 03:31 UTC == 12:31 JST: 4h01m late.
    should_skip, delta = brief_service.late_boot_decision(
        datetime(2026, 9, 8, 3, 31, tzinfo=utc), config)
    assert should_skip is True
    assert delta == pytest.approx(4 * 3600 + 60)


def test_a_naive_now_is_refused_rather_than_guessed(tmp_path, state):
    with pytest.raises(ValueError):
        brief_service.run_scheduled_brief(
            base_config(tmp_path), now=datetime(2026, 9, 8, 8, 30),
            state=state, client=FakeClient())
