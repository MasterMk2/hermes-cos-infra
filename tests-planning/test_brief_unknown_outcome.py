"""An unknown provider receipt survives a planning process restart."""

from planning import brief_service, brief_state, delivery
from test_brief_delivery import FakeClient, base_config, run


def test_gateway_unknown_is_not_content_mismatch_or_success():
    result = delivery.classify_response(
        409, {"status": "outcome_unknown", "detail": "delivery_outcome_unknown"})
    assert result.outcome == delivery.OUTCOME_UNKNOWN
    assert result.terminal and not result.retryable and not result.ok


def test_unknown_stops_retry_and_survives_reopen(tmp_path):
    path = str(tmp_path / "planning.db")
    con = brief_state.open_state(path)
    state = brief_state.BriefState(con)
    client = FakeClient(delivery.Result(delivery.OUTCOME_UNKNOWN,
                                       detail="delivery_outcome_unknown"))
    sleeps = []
    result = run(base_config(tmp_path), state, client, sleeps=sleeps)
    assert result["status"] == brief_service.ST_OUTCOME_UNKNOWN
    assert len(client.calls) == 1 and sleeps == []
    assert state.get("2026-09-08")["delivery_status"] == brief_state.UNKNOWN
    assert state.get("2026-09-08")["delivered_at"] is None
    assert not state.is_settled("2026-09-08")
    original_hash = state.get("2026-09-08")["plan_hash"]
    state.begin_run("2026-09-08", "different-plan-after-restart")
    state.mark_status("2026-09-08", brief_state.SKIPPED, last_error="disabled")
    assert state.get("2026-09-08")["delivery_status"] == brief_state.UNKNOWN
    assert state.get("2026-09-08")["plan_hash"] == original_hash
    assert state.get("2026-09-08")["last_error"] == "delivery_outcome_unknown"
    con.close()

    con = brief_state.open_state(path)
    state = brief_state.BriefState(con)
    def must_not_generate(*args, **kwargs):
        raise AssertionError("an unresolved send must not be regenerated")
    second = run(base_config(tmp_path), state, client, planner=must_not_generate)
    assert second["status"] == brief_service.ST_OUTCOME_UNKNOWN
    assert len(client.calls) == 1 and state.attempts("2026-09-08") == 1
    health = brief_state.read_health(str(tmp_path))
    assert health["status"] == "unknown"
    assert health["last_delivery_success"] is None
    assert health["last_failure_reason"] == "delivery_outcome_unknown"
    con.close()
