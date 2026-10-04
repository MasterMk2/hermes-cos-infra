"""Delivery: the gateway response table and the bounded retry.

Phase section 59; I2-AMENDMENT section G3 — the row-by-row contract for what
the gateway answers and what the service is allowed to do about it.

The retry bound is the point of this file. `content_revision` is
`sha256(text)[:32]`, so a restart that recomputes a brief differing by one
character re-posts under the same `morning-brief:YYYY-MM-DD` id and receives
409 `content_revision_mismatch`. If that were retryable the service would loop
forever; `test_content_mismatch_stops_immediately` and
`test_always_failing_transport_terminates` are the two halves of the proof.

Run with:  PYTHONPATH=src python -m pytest tests-planning -q
"""

from __future__ import annotations

import hashlib
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from planning import brief_service, brief_state, delivery  # noqa: E402

JST = ZoneInfo("Asia/Tokyo")


# --- stubs ---------------------------------------------------------------
#
# `planner.py` (T2) and `renderer.py` (T3) are separate deliverables. These
# stubs honour their contract signatures exactly, which is what lets this
# module be tested independently of them.

def stub_plan(date="2026-09-08", plan_hash="h0", warnings=None):
    plan = {
        "date": date, "timezone": "Asia/Tokyo",
        "generated_at": f"{date}T08:30:00+09:00",
        "scheduled": [], "must_do": [], "overdue": [], "next_up": [],
        "waiting_for": [], "backlog": [], "conflicts": [],
        "counts": {}, "source_freshness": {}, "plan_hash": plan_hash,
    }
    if warnings:
        plan["warnings"] = list(warnings)
    return plan


def make_planner(plan=None, exc=None):
    def build_daily_plan(conn, *, date=None, timezone="Asia/Tokyo", now=None,
                         config=None, freshness=None):
        if exc is not None:
            raise exc
        return plan if plan is not None else stub_plan(date=date)
    return build_daily_plan


def make_renderer(text="今日の予定は 0 件です。", exc=None, renderer="template"):
    def render_brief(plan, *, mode="auto", config=None, client=None, now=None):
        if exc is not None:
            raise exc
        return {"text": text, "renderer": renderer, "model": None,
                "fallback_reason": None,
                "grounding": {"ok": True, "violations": []}}
    return render_brief


def no_events(config):
    class _Conn:
        def close(self):
            pass
    return _Conn()


class FakeClient:
    """Records every request and answers from a scripted list of Results."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def send_brief(self, *, notification_id, text, reason="morning_brief"):
        # Mirror the real client's body so the LINE-negative test can inspect
        # exactly what would go on the wire.
        self.calls.append({
            "notification_id": notification_id,
            "preferred_channel": delivery.CHANNEL_DISCORD,
            "severity": delivery.SEVERITY_INFO,
            "text": text,
            "reason": reason,
            "dedupe_key": notification_id,
        })
        if not self.results:
            return delivery.Result(delivery.DELIVERED, detail="ok",
                                   http_status=200)
        if len(self.results) == 1:
            return self.results[0]
        return self.results.pop(0)


@pytest.fixture
def state():
    con = brief_state.open_state(":memory:")
    try:
        yield brief_state.BriefState(con)
    finally:
        con.close()


def base_config(tmp_path, **over):
    config = {
        "timezone": "Asia/Tokyo",
        "morning_brief": {"time": "08:30", "late_boot_grace_hours": 3,
                          "channel": "discord", "enabled": True},
        "paths": {"planning_dir": str(tmp_path),
                  "events_db": str(tmp_path / "events.db"),
                  "gateway_proactive_socket": str(tmp_path / "s.sock")},
        "renderer": {"mode": "template", "max_chars": 1200},
    }
    config["morning_brief"].update(over)
    return config


def run(config, state, client, *, now=None, **kw):
    sleeps = kw.pop("sleeps", [])
    return brief_service.run_scheduled_brief(
        config, now=now or datetime(2026, 9, 8, 8, 30, tzinfo=JST),
        state=state, client=client,
        planner=kw.pop("planner", make_planner()),
        renderer=kw.pop("renderer", make_renderer()),
        open_events=no_events, sleep=sleeps.append, **kw)


# --- I2-AMENDMENT G3, row by row -----------------------------------------

@pytest.mark.parametrize("status,body,expected,retryable", [
    (200, {"status": "delivered", "detail": "ok"},
     delivery.DELIVERED, False),
    (200, {"status": "duplicate", "detail": "already_delivered",
           "sent_at": "2026-09-08T08:30:05+09:00"},
     delivery.ALREADY_DELIVERED, False),
    (409, {"status": "in_flight", "detail": "delivery_in_progress"},
     delivery.IN_FLIGHT, False),
    (409, {"status": "rejected", "detail": "content_revision_mismatch"},
     delivery.CONTENT_MISMATCH, False),
    (400, {"status": "rejected", "detail": "text_too_long"},
     delivery.REJECTED, False),
    (413, {"status": "rejected", "detail": "body_too_large"},
     delivery.REJECTED, False),
    (503, {"status": "failed", "detail": "discord_owner_not_configured"},
     delivery.REJECTED, False),
    (502, {"status": "failed", "detail": "discord_send_failed"},
     delivery.FAILED, True),
])
def test_classify_response_matches_the_amendment(status, body, expected,
                                                 retryable):
    result = delivery.classify_response(status, body)
    assert result.outcome == expected
    assert result.retryable is retryable
    assert result.terminal is not retryable


def test_success_set_is_exactly_delivered_and_already_delivered():
    assert delivery.SUCCESS == {delivery.DELIVERED, delivery.ALREADY_DELIVERED}
    # An `already_delivered` answer is success, not an error to be reported.
    assert delivery.classify_response(
        200, {"status": "duplicate", "detail": "already_delivered"}).ok


def test_unrecognised_body_is_never_reported_as_success():
    assert not delivery.classify_response(200, {"status": "banana"}).ok
    assert not delivery.classify_response(200, "not a dict").ok
    assert not delivery.classify_response(599, {}).ok


def test_notification_id_is_the_local_date_and_gateway_legal():
    nid = delivery.notification_id_for("2026-09-08")
    assert nid == "morning-brief:2026-09-08"
    assert len(nid) <= 120
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
                  "0123456789_.:-")
    assert set(nid) <= allowed
    for bad in ("2026-9-8", "20260908", "", "not-a-date", "2026-09-0x"):
        with pytest.raises(ValueError):
            delivery.notification_id_for(bad)


def test_content_revision_matches_the_gateway_derivation():
    text = "今日の予定は 2 件です。"
    assert delivery.content_revision(text) == \
        hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def test_over_long_text_is_refused_without_opening_a_socket():
    # A socket path that cannot exist: reaching the transport would raise.
    client = delivery.DeliveryClient("/nonexistent/never/proactive.sock")
    result = client.send_brief(notification_id="morning-brief:2026-09-08",
                               text="x" * (delivery.MAX_TEXT_CHARS + 1))
    assert result.outcome == delivery.REJECTED
    assert result.detail == "text_too_long"
    assert client.send_brief(notification_id="morning-brief:2026-09-08",
                             text="   ").detail == "text_empty"


# --- the bound (phase section 59) ----------------------------------------

def test_always_failing_transport_terminates(tmp_path, state):
    """The proof that the retry is bounded: an endpoint that never succeeds."""
    client = FakeClient(delivery.Result(delivery.FAILED,
                                        detail="discord_send_failed",
                                        http_status=502))
    sleeps = []
    outcome = run(base_config(tmp_path), state, client, sleeps=sleeps)

    assert len(client.calls) == brief_service.MAX_DELIVERY_ATTEMPTS == 3
    assert outcome["attempts"] == 3
    assert outcome["status"] == brief_service.ST_FAILED
    assert outcome["failure_reason"] == brief_service.FR_DELIVERY
    # Two waits for three attempts, and no third wait after the last failure.
    assert sleeps == [2.0, 8.0]
    assert state.get("2026-09-08")["delivery_status"] == brief_state.FAILED


def test_unreachable_socket_is_retried_then_bounded(tmp_path, state):
    client = FakeClient(delivery.Result(delivery.UNREACHABLE,
                                        detail="FileNotFoundError"))
    outcome = run(base_config(tmp_path), state, client)
    assert len(client.calls) == 3
    assert outcome["status"] == brief_service.ST_FAILED


def test_lost_answer_is_retried_but_still_bounded(tmp_path, state):
    # The request went out and the answer did not come back. Retrying the same
    # notification id is safe — the gateway answers from its own record — but
    # it must still stop.
    client = FakeClient(delivery.Result(delivery.UNKNOWN, detail="timeout"))
    outcome = run(base_config(tmp_path), state, client)
    assert len(client.calls) == 3
    assert outcome["status"] == brief_service.ST_FAILED


def test_transient_failure_then_success_stops_at_two(tmp_path, state):
    client = FakeClient(
        delivery.Result(delivery.FAILED, detail="discord_send_failed",
                        http_status=502),
        delivery.Result(delivery.DELIVERED, detail="ok", http_status=200),
    )
    sleeps = []
    outcome = run(base_config(tmp_path), state, client, sleeps=sleeps)
    assert len(client.calls) == 2
    assert outcome["attempts"] == 2
    assert outcome["status"] == brief_service.ST_DELIVERED
    assert sleeps == [2.0]


def test_content_mismatch_stops_immediately(tmp_path, state):
    """The infinite-loop case from I2-AMENDMENT G3, pinned."""
    client = FakeClient(delivery.Result(delivery.CONTENT_MISMATCH,
                                        detail="content_revision_mismatch",
                                        http_status=409))
    outcome = run(base_config(tmp_path), state, client)
    assert len(client.calls) == 1
    assert outcome["status"] == brief_service.ST_SUPPRESSED
    assert outcome["failure_reason"] == brief_service.FR_DUPLICATE
    assert state.get("2026-09-08")["delivery_status"] == brief_state.SUPPRESSED


def test_in_flight_stops_immediately(tmp_path, state):
    client = FakeClient(delivery.Result(delivery.IN_FLIGHT,
                                        detail="delivery_in_progress",
                                        http_status=409))
    outcome = run(base_config(tmp_path), state, client)
    assert len(client.calls) == 1
    assert outcome["status"] == brief_service.ST_SUPPRESSED


def test_rejected_request_is_not_retried(tmp_path, state):
    client = FakeClient(delivery.Result(delivery.REJECTED,
                                        detail="text_too_long",
                                        http_status=400))
    outcome = run(base_config(tmp_path), state, client)
    assert len(client.calls) == 1
    assert outcome["status"] == brief_service.ST_FAILED
    assert outcome["failure_reason"] == brief_service.FR_DELIVERY


def test_dry_run_opens_no_socket_and_records_no_delivery(tmp_path, state):
    client = FakeClient()
    outcome = run(base_config(tmp_path), state, client, dry_run=True)
    assert client.calls == []
    assert outcome["status"] == brief_service.ST_DRY_RUN
    assert outcome["text_chars"] > 0
    assert state.get("2026-09-08")["delivery_status"] == brief_state.PENDING


def test_status_and_failure_reason_vocabularies_are_closed(tmp_path, state):
    outcome = run(base_config(tmp_path), state, FakeClient())
    assert outcome["status"] in brief_service.ALL_STATUSES
    assert (outcome["failure_reason"] is None
            or outcome["failure_reason"] in brief_service.ALL_FAILURE_REASONS)
    assert brief_service.ALL_FAILURE_REASONS == {
        "planner_error", "renderer_error", "delivery_error", "stale_source",
        "duplicate_suppressed", "delivery_outcome_unknown",
    }
