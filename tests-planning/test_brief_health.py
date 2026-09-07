"""Health and observability — phase sections 60, 105, 106, 107.

Section 106 requires the five failure reasons to be *distinguishable*, which is
only useful if each one is actually reachable. Every test here drives the run
into one of them and asserts the health file says so:

    planner_error        the plan could not be built — and NOTHING was sent
    renderer_error       the plan was fine, the text was not
    delivery_error       the socket refused, three times
    stale_source         a source is stale; the brief still went out, degraded
    duplicate_suppressed the date was already handled

Section 60 is the one with teeth: a planner failure must never produce a
fabricated brief. `test_planner_failure_sends_nothing_and_invents_nothing`
asserts the client was not called at all.

Run with:  PYTHONPATH=src python -m pytest tests-planning -q
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from planning import brief_service, brief_state, delivery  # noqa: E402
from test_brief_delivery import (  # noqa: E402
    FakeClient, base_config, make_planner, make_renderer, no_events, stub_plan,
)

JST = ZoneInfo("Asia/Tokyo")
MORNING = datetime(2026, 9, 8, 8, 30, tzinfo=JST)


@pytest.fixture
def state():
    con = brief_state.open_state(":memory:")
    try:
        yield brief_state.BriefState(con)
    finally:
        con.close()


def run_at(config, state, client, *, planner=None, renderer=None, now=MORNING):
    return brief_service.run_scheduled_brief(
        config, now=now, state=state, client=client,
        planner=planner or make_planner(), renderer=renderer or make_renderer(),
        open_events=no_events, sleep=lambda _s: None)


# --- the file itself (phase section 105) ---------------------------------

def test_health_file_has_exactly_the_contract_fields(tmp_path, state):
    run_at(base_config(tmp_path), state, FakeClient())
    raw = json.loads(
        (tmp_path / "morning-brief-health.json").read_text(encoding="utf-8"))
    assert set(raw) == {
        "last_plan_generated", "last_delivery_attempt", "last_delivery_success",
        "last_plan_hash", "status", "last_failure_reason", "updated_at",
    }
    assert set(raw) == set(brief_state.HEALTH_FIELDS)


def test_a_successful_run_fills_every_timestamp(tmp_path, state):
    outcome = run_at(base_config(tmp_path), state, FakeClient())
    health = brief_state.read_health(str(tmp_path))
    assert outcome["status"] == brief_service.ST_DELIVERED
    assert health["status"] == "ok"
    assert health["last_failure_reason"] is None
    assert health["last_plan_generated"]
    assert health["last_delivery_attempt"]
    assert health["last_delivery_success"]
    assert health["last_plan_hash"] == "h0"
    assert health["updated_at"]


def test_read_health_never_raises(tmp_path):
    assert brief_state.read_health(str(tmp_path / "nope")) == \
        brief_state.empty_health()
    (tmp_path / "morning-brief-health.json").write_text("{ broken",
                                                        encoding="utf-8")
    assert brief_state.read_health(str(tmp_path))["status"] == "unknown"
    (tmp_path / "morning-brief-health.json").write_text("[1,2]",
                                                        encoding="utf-8")
    assert brief_state.read_health(str(tmp_path))["status"] == "unknown"


def test_write_health_refuses_an_unknown_field(tmp_path):
    with pytest.raises(ValueError):
        brief_state.write_health(str(tmp_path), {"secret_token": "x"})


def test_last_success_survives_a_later_failure(tmp_path, state):
    """The one question the file exists to answer is 'when did this last work'.
    A merge, not a replace, is what keeps it answerable."""
    config = base_config(tmp_path)
    run_at(config, state, FakeClient())
    good = brief_state.read_health(str(tmp_path))["last_delivery_success"]
    assert good

    config2 = base_config(tmp_path)
    config2["paths"]["planning_dir"] = str(tmp_path)
    brief_service.run_scheduled_brief(
        config2, now=datetime(2026, 9, 9, 8, 30, tzinfo=JST), state=state,
        client=FakeClient(delivery.Result(delivery.FAILED, detail="boom",
                                          http_status=502)),
        planner=make_planner(stub_plan(date="2026-09-09")),
        renderer=make_renderer(), open_events=no_events,
        sleep=lambda _s: None)
    after = brief_state.read_health(str(tmp_path))
    assert after["status"] == "fail"
    assert after["last_failure_reason"] == brief_service.FR_DELIVERY
    assert after["last_delivery_success"] == good


# --- phase section 60: never fabricate -----------------------------------

def test_planner_failure_sends_nothing_and_invents_nothing(tmp_path, state):
    client = FakeClient()
    outcome = run_at(base_config(tmp_path), state, client,
                     planner=make_planner(exc=RuntimeError("events.db locked")))
    assert outcome["status"] == brief_service.ST_FAILED
    assert outcome["failure_reason"] == brief_service.FR_PLANNER
    assert "events.db locked" in outcome["detail"]
    # The load-bearing assertion: no request of any kind reached the gateway.
    assert client.calls == []
    health = brief_state.read_health(str(tmp_path))
    assert health["status"] == "fail"
    assert health["last_failure_reason"] == "planner_error"
    assert state.get("2026-09-08")["delivery_status"] == brief_state.FAILED


def test_a_planner_returning_the_wrong_type_is_a_planner_error(tmp_path, state):
    client = FakeClient()
    outcome = run_at(base_config(tmp_path), state, client,
                     planner=lambda conn, **kw: ["not", "a", "plan"])
    assert outcome["failure_reason"] == brief_service.FR_PLANNER
    assert client.calls == []


def test_the_optional_failure_notice_is_off_by_default(tmp_path, state):
    client = FakeClient()
    run_at(base_config(tmp_path), state, client,
           planner=make_planner(exc=RuntimeError("x")))
    assert client.calls == []


def test_the_failure_notice_when_enabled_is_a_notice_not_a_brief(tmp_path,
                                                                 state):
    config = base_config(tmp_path, notify_on_failure=True)
    client = FakeClient()
    run_at(config, state, client, planner=make_planner(exc=RuntimeError("x")))
    assert len(client.calls) == 1
    call = client.calls[0]
    # Its own id namespace: it can never be mistaken for, or collide with, the
    # brief's `morning-brief:YYYY-MM-DD`.
    assert call["notification_id"] == "morning-brief-failure:2026-09-08"
    assert call["notification_id"] != "morning-brief:2026-09-08"
    assert call["reason"] == "morning_brief_failure"
    assert "生成できませんでした" in call["text"]
    assert call["preferred_channel"] == "discord"


# --- renderer_error ------------------------------------------------------

def test_renderer_exception_is_its_own_failure_reason(tmp_path, state):
    client = FakeClient()
    outcome = run_at(base_config(tmp_path), state, client,
                     renderer=make_renderer(exc=RuntimeError("no template")))
    assert outcome["status"] == brief_service.ST_FAILED
    assert outcome["failure_reason"] == brief_service.FR_RENDERER
    assert client.calls == []
    assert brief_state.read_health(str(tmp_path))["last_failure_reason"] == \
        "renderer_error"


def test_empty_rendered_text_is_a_renderer_error_not_an_empty_brief(tmp_path,
                                                                    state):
    client = FakeClient()
    outcome = run_at(base_config(tmp_path), state, client,
                     renderer=make_renderer(text="   \n  "))
    assert outcome["failure_reason"] == brief_service.FR_RENDERER
    assert client.calls == []


def test_a_template_fallback_is_a_normal_success(tmp_path, state):
    """An LLM that is unavailable is not a failure: the template renderer
    produces a correct brief on its own (I2-CONTRACT.md section 5)."""
    outcome = run_at(base_config(tmp_path), state, FakeClient(),
                     renderer=make_renderer(renderer="template"))
    assert outcome["status"] == brief_service.ST_DELIVERED
    assert outcome["renderer"] == "template"
    assert brief_state.read_health(str(tmp_path))["status"] == "ok"


# --- stale_source (phase sections 37, 91, 106) ---------------------------

def test_a_stale_source_degrades_the_run_but_still_delivers(tmp_path, state):
    client = FakeClient()
    outcome = run_at(
        base_config(tmp_path), state, client,
        planner=make_planner(stub_plan(warnings=["data_freshness_warning"])))
    assert outcome["status"] == brief_service.ST_DELIVERED
    assert outcome["failure_reason"] == brief_service.FR_STALE
    assert outcome["warnings"] == ["data_freshness_warning"]
    assert len(client.calls) == 1
    health = brief_state.read_health(str(tmp_path))
    assert health["status"] == "degraded"
    assert health["last_failure_reason"] == "stale_source"
    assert health["last_delivery_success"]


# --- duplicate_suppressed ------------------------------------------------

def test_duplicate_suppression_is_reported_as_such(tmp_path, state):
    config = base_config(tmp_path)
    client = FakeClient()
    run_at(config, state, client)
    second = run_at(config, state, client)
    assert second["failure_reason"] == brief_service.FR_DUPLICATE
    health = brief_state.read_health(str(tmp_path))
    assert health["status"] == "ok"
    assert health["last_failure_reason"] == "duplicate_suppressed"


# --- delivery_error ------------------------------------------------------

def test_delivery_failure_is_reported_and_bounded(tmp_path, state):
    client = FakeClient(delivery.Result(delivery.FAILED, detail="send_failed",
                                        http_status=502))
    outcome = run_at(base_config(tmp_path), state, client)
    assert outcome["failure_reason"] == brief_service.FR_DELIVERY
    assert outcome["attempts"] == 3
    row = state.get("2026-09-08")
    assert row["delivery_status"] == brief_state.FAILED
    assert "send_failed" in row["last_error"]
    assert brief_state.read_health(str(tmp_path))["last_failure_reason"] == \
        "delivery_error"


# --- phase section 107: an ordinary error does not stop the process ------

def test_an_unwritable_health_file_does_not_break_the_run(tmp_path, state,
                                                          monkeypatch):
    def explode(*_a, **_k):
        raise OSError("read-only file system")
    monkeypatch.setattr(brief_state, "write_health", explode)
    outcome = run_at(base_config(tmp_path), state, FakeClient())
    assert outcome["status"] == brief_service.ST_DELIVERED


def test_health_file_is_written_atomically_and_narrow(tmp_path, state):
    run_at(base_config(tmp_path), state, FakeClient())
    leftovers = list(tmp_path.glob(".health-*"))
    assert leftovers == []
    assert (tmp_path / "morning-brief-health.json").exists()


def test_a_dry_run_does_not_paint_over_a_real_failure(tmp_path, state):
    """A manual preview refreshes the plan fields and nothing else. Reporting
    `ok` because someone ran `--dry-run` would hide a broken morning."""
    config = base_config(tmp_path)
    run_at(config, state, FakeClient(),
           planner=make_planner(exc=RuntimeError("boom")))
    assert brief_state.read_health(str(tmp_path))["status"] == "fail"

    outcome = brief_service.run_scheduled_brief(
        config, now=MORNING, state=state, client=FakeClient(), dry_run=True,
        planner=make_planner(stub_plan(plan_hash="fresh")),
        renderer=make_renderer(), open_events=no_events,
        sleep=lambda _s: None)
    health = brief_state.read_health(str(tmp_path))
    assert outcome["status"] == brief_service.ST_DRY_RUN
    assert health["last_plan_hash"] == "fresh"     # the plan fields moved
    assert health["status"] == "fail"              # the verdict did not
    assert health["last_failure_reason"] == "planner_error"


def test_a_fresh_plan_clears_a_stale_error_on_the_row(state):
    """`last_error` must describe the row it is attached to."""
    state.mark_status("2026-09-08", brief_state.FAILED,
                      last_error="planner_error: events.db locked")
    assert state.get("2026-09-08")["last_error"]
    state.begin_run("2026-09-08", "h-fresh")
    row = state.get("2026-09-08")
    assert row["last_error"] is None
    assert row["delivery_status"] == brief_state.PENDING
    assert row["plan_hash"] == "h-fresh"
