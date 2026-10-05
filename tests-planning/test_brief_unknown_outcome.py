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


import pytest


@pytest.mark.parametrize('outcome,detail', [
    (delivery.IN_FLIGHT, 'delivery_in_progress'),
    (delivery.CONTENT_MISMATCH, 'content_revision_mismatch'),
])
def test_reservation_or_conflict_never_confirms_delivery(tmp_path, outcome, detail):
    path = str(tmp_path / 'planning.db')
    con = brief_state.open_state(path); state = brief_state.BriefState(con)
    client = FakeClient(delivery.Result(outcome, detail=detail, http_status=409))
    sleeps = []
    result = run(base_config(tmp_path), state, client, sleeps=sleeps)
    assert result['status'] == brief_service.ST_OUTCOME_UNKNOWN
    assert result['detail'] == detail
    assert len(client.calls) == 1 and sleeps == []
    row = state.get('2026-09-08')
    assert row['delivered_at'] is None and row['last_error'] == detail
    original_hash = row['plan_hash']
    con.close()
    con = brief_state.open_state(path); state = brief_state.BriefState(con)
    def no_regeneration(*a, **kw):
        raise AssertionError('a pending gateway reservation is not retry authority')
    second = run(base_config(tmp_path), state, client, planner=no_regeneration)
    assert second['status'] == brief_service.ST_OUTCOME_UNKNOWN
    assert len(client.calls) == 1
    assert state.get('2026-09-08')['plan_hash'] == original_hash
    assert state.get('2026-09-08')['last_error'] == detail
    health = brief_state.read_health(str(tmp_path))
    assert health['status'] == 'unknown' and health['last_delivery_success'] is None
    con.close()


def test_legacy_suppressed_row_is_not_a_delivery_receipt(tmp_path):
    con = brief_state.open_state(str(tmp_path/'planning.db'))
    state = brief_state.BriefState(con)
    state.begin_run('2026-09-08', 'original-hash')
    state.mark_status('2026-09-08', brief_state.SUPPRESSED,
                      last_error='delivery_in_progress', channel='discord')
    assert not state.is_settled('2026-09-08')
    state.begin_run('2026-09-08', 'must-not-replace-original')
    client = FakeClient()
    result = run(base_config(tmp_path), state, client)
    assert result['status'] == brief_service.ST_OUTCOME_UNKNOWN and client.calls == []
    row = state.get('2026-09-08')
    assert row['delivery_status'] == brief_state.UNKNOWN
    assert row['plan_hash'] == 'original-hash' and row['delivered_at'] is None
    assert row['last_error'] == 'delivery_in_progress'
    assert brief_state.read_health(str(tmp_path))['status'] == 'unknown'
    con.close()


@pytest.mark.parametrize('confirmed', [delivery.DELIVERED, delivery.ALREADY_DELIVERED])
def test_only_confirmed_receipt_marks_brief_healthy(tmp_path, confirmed):
    con = brief_state.open_state(str(tmp_path/'planning.db'))
    state = brief_state.BriefState(con)
    client = FakeClient(delivery.Result(confirmed, detail='already_delivered' if confirmed == delivery.ALREADY_DELIVERED else 'ok', http_status=200))
    assert run(base_config(tmp_path), state, client)['status'] == brief_service.ST_DELIVERED
    assert state.get('2026-09-08')['delivered_at'] is not None
    health = brief_state.read_health(str(tmp_path))
    assert health['status'] == 'ok' and health['last_delivery_success'] is not None
    con.close()


def test_unspecified_duplicate_response_does_not_claim_delivery():
    result = delivery.classify_response(200, {'status': 'duplicate', 'detail': 'reserved'})
    assert not result.ok
