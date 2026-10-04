"""Ordinary Discord replies: durable boundaries, synthetic adapters only."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import patch

import pytest
import gw_fixtures as fx
import gw_store as store
from gw_canonical import CanonicalMessage
from gw_config import parse_config
from gw_schema import open_db
from gw_service import Gateway, message_from_row


class Bot:
    def __init__(self, results=()):
        self.results = list(results)
        self.calls = []
    def prepare(self, text):
        return text.split('|') if text else []
    async def send_chunks(self, channel, chunks):
        self.calls.append((channel, list(chunks)))
        result = self.results.pop(0) if self.results else (1, None)
        if isinstance(result, BaseException):
            raise result
        return result


def make_gateway(path):
    cfg = parse_config(fx.config_dict(paths={'state_db': str(path)}))
    gw = Gateway(cfg, {'hermes_api_key': 'synthetic-test-key'})
    gw.discord_bot = Bot()
    return gw


def seed(gw):
    msg = CanonicalMessage(
        message_id='synthetic-message', platform='discord',
        platform_message_id='synthetic-message', platform_user_id=fx.OWNER_DISCORD,
        conversation_id='discord:' + fx.DM_CHANNEL_ID, timestamp='2026-10-04T00:00:00+00:00',
        text='synthetic question', authz_subject='owner', capability_profile='owner_full',
        gateway_request_id='synthetic-request', received_at='2026-10-04T00:00:00+00:00')
    with gw.db() as con:
        rid, _ = store.record_inbound(con, msg)
        store.enqueue(con, rid)
        row = store.claim_next(con, max_active=1)
    con.close()
    return row, msg


def rows(gw):
    con = gw.db()
    try:
        return store.outbound_for_request(con, 'synthetic-request')
    finally:
        con.close()


def recover(gw):
    con = gw.db()
    try:
        con.execute("UPDATE inbound_messages SET claimed_at='2000-01-01', attempts=1")
        result = store.recover_stale(con)
        con.commit()
        return result, store.get_inbound(con, 'synthetic-request')
    finally:
        con.close()


def run(coro):
    return asyncio.run(coro)


def test_receipts_are_committed_between_chunks(tmp_path):
    gw = make_gateway(tmp_path/'gw.db'); row, msg = seed(gw)
    class Inspect(Bot):
        async def send_chunks(self, channel, chunks):
            index = len(self.calls)
            assert [r['state'] for r in rows(gw)] == ['SENT'] * index + ['SENDING'] + ['NOT_SENT'] * (2-index)
            return await super().send_chunks(channel, chunks)
    gw.discord_bot = Inspect()
    assert run(gw.deliver_discord(row, msg, 'a|b|c'))[0]
    assert [r['state'] for r in rows(gw)] == ['SENT'] * 3
    assert [c[1] for c in gw.discord_bot.calls] == [['a'], ['b'], ['c']]


@pytest.mark.parametrize('stop_at', [0, 1, 2])
def test_crash_after_accept_before_receipt_holds_whole_reply(tmp_path, stop_at):
    gw = make_gateway(tmp_path/'gw.db'); row, msg = seed(gw)
    gw.discord_bot = Bot([(1, None)] * stop_at + [asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        run(gw.deliver_discord(row, msg, 'a|b|c'))
    assert [r['state'] for r in rows(gw)] == ['SENT'] * stop_at + ['SENDING'] + ['NOT_SENT'] * (2-stop_at)
    # New Gateway and new SQLite connections model a process restart.
    restarted = make_gateway(tmp_path/'gw.db')
    _, current = recover(restarted)
    assert current['state'] == 'UNKNOWN'
    assert run(restarted.deliver_discord(current, msg, 'a|b|c'))[1] == 'delivery_outcome_unknown'
    assert restarted.discord_bot.calls == []
    assert [r['state'] for r in rows(restarted)] == ['SENT'] * stop_at + ['UNKNOWN'] + ['NOT_SENT'] * (2-stop_at)


def test_confirmed_prefix_only_resumes_frozen_unsent_tail(tmp_path):
    gw = make_gateway(tmp_path/'gw.db'); row, msg = seed(gw)
    # Interrupt between the first durable receipt and the next claim.
    original = store.claim_discord_chunk
    def stop(con, *args, **kw):
        if any(r['state'] == 'SENT' for r in store.outbound_for_request(con, row['gateway_request_id'])):
            raise asyncio.CancelledError()
        return original(con, *args, **kw)
    with patch.object(store, 'claim_discord_chunk', side_effect=stop), pytest.raises(asyncio.CancelledError):
        run(gw.deliver_discord(row, msg, 'a|b|c'))
    assert [r['state'] for r in rows(gw)] == ['SENT', 'NOT_SENT', 'NOT_SENT']
    restarted = make_gateway(tmp_path/'gw.db'); _, current = recover(restarted)
    assert current['state'] == 'QUEUED'
    async def no_regeneration(*a, **kw):
        raise AssertionError('the frozen answer must be resumed without another Hermes call')
    restarted.hermes.ask_async = no_regeneration
    run(restarted.process(current))
    assert [c[1] for c in restarted.discord_bot.calls] == [['b'], ['c']]
    with restarted.db() as con:
        assert store.get_inbound(con, row['gateway_request_id'])['state'] == 'RESPONDED'
    con.close()


@pytest.mark.parametrize('error,expected', [('http:429:0', 'UNKNOWN'), ('http:403:50007', 'UNKNOWN'), ('channel_unavailable:10003', 'NOT_SENT'), ('TimeoutError', 'UNKNOWN'), ('http:500:0', 'UNKNOWN')])
def test_definite_rejection_is_distinct_from_unknown(tmp_path, error, expected):
    gw = make_gateway(tmp_path/'gw.db'); row, msg = seed(gw)
    gw.discord_bot = Bot([(1, None), (0, error)])
    run(gw.deliver_discord(row, msg, 'a|b|c'))
    assert [r['state'] for r in rows(gw)] == ['SENT', expected, 'NOT_SENT']
    restarted = make_gateway(tmp_path/'gw.db')
    run(restarted.deliver_discord(row, msg, 'a|b|c'))
    assert [c[1] for c in restarted.discord_bot.calls] == ([['b'], ['c']] if expected == 'NOT_SENT' else [])


def test_all_confirmed_crash_before_inbound_settle_does_not_resend(tmp_path):
    gw = make_gateway(tmp_path/'gw.db'); row, msg = seed(gw)
    assert run(gw.deliver_discord(row, msg, 'a|b'))[0]
    restarted = make_gateway(tmp_path/'gw.db'); _, current = recover(restarted)
    assert current['state'] == 'RESPONDED'
    assert run(restarted.deliver_discord(current, msg, 'a|b'))[0]
    assert restarted.discord_bot.calls == []


@pytest.mark.parametrize('legacy', ['PENDING', 'FAILED', 'ABANDONED'])
def test_legacy_nonconfirmed_rows_do_not_become_retry_authority(tmp_path, legacy):
    gw = make_gateway(tmp_path/'gw.db'); row, msg = seed(gw)
    with gw.db() as con:
        oid, _ = store.reserve_outbound(con, request_id=row['gateway_request_id'], platform='discord', conversation_id=msg.conversation_id, target_user_id=fx.OWNER_DISCORD, body='a', chunk_index=0, chunk_total=1, delivery_mode='dm')
        store.mark_outbound(con, oid, legacy)
    con.close()
    assert run(gw.deliver_discord(row, msg, 'a'))[1] == 'delivery_outcome_unknown'
    assert gw.discord_bot.calls == [] and rows(gw)[0]['state'] == 'UNKNOWN'


@pytest.mark.parametrize('change', ['body', 'chunk_count', 'owner', 'conversation'])
def test_retry_cannot_change_immutable_reply_scope(tmp_path, change):
    gw = make_gateway(tmp_path/'gw.db'); row, msg = seed(gw)
    gw.discord_bot = Bot([(0, 'channel_unavailable:10003')])
    run(gw.deliver_discord(row, msg, 'a|b'))
    text = 'changed|b' if change == 'body' else 'a' if change == 'chunk_count' else 'a|b'
    if change == 'owner':
        gw.cfg = replace(gw.cfg, owner_discord_user_id=fx.STRANGER_DISCORD)
    if change == 'conversation':
        row = dict(row); row['conversation_id'] = 'discord:999'
    before = [dict(r) for r in rows(gw)]
    gw.discord_bot = Bot()
    assert not run(gw.deliver_discord(row, msg, text))[0]
    assert gw.discord_bot.calls == []
    assert [(r['body'], r['target_user_id'], r['conversation_id']) for r in rows(gw)] == [(r['body'], r['target_user_id'], r['conversation_id']) for r in before]


def test_same_chunk_claim_is_atomic_across_connections(tmp_path):
    gw = make_gateway(tmp_path/'gw.db'); row, msg = seed(gw)
    gw.discord_bot = Bot([(0, 'channel_unavailable:10003')])
    run(gw.deliver_discord(row, msg, 'a|b'))
    def claim(_):
        con = gw.db()
        try:
            result = store.claim_discord_chunk(con, row['gateway_request_id'])
            con.commit()
            return result[0]
        finally:
            con.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim, range(2))) == ['in_flight', 'send']


def test_real_process_exit_after_adapter_acceptance_has_no_replay(tmp_path):
    """A separate process exits before recording a receipt; no transport exists."""
    import os
    from pathlib import Path
    import subprocess
    import sys
    db = tmp_path/'gw.db'
    receipt = tmp_path/'synthetic-acceptance.txt'
    child = r'''
import asyncio, os, sys
import offline_guard
offline_guard.pytest_configure(None)
from test_reply_recovery import make_gateway, seed, Bot
gw = make_gateway(sys.argv[1]); row, msg = seed(gw)
class ExitAfterAccept(Bot):
    async def send_chunks(self, channel, chunks):
        # The file is a synthetic provider receipt outside the Gateway DB.
        with open(sys.argv[2], 'w') as out:
            out.write(chunks[0]); out.flush(); os.fsync(out.fileno())
        assert not offline_guard._attempts
        os._exit(73)
gw.discord_bot = ExitAfterAccept()
asyncio.run(gw.deliver_discord(row, msg, 'a|b'))
'''
    env = os.environ.copy()
    # Only local Python + synthetic fixtures. No credentials/endpoints passed.
    env['PYTHONPATH'] = os.pathsep.join(str(Path(__file__).resolve().parents[1]/p)
        for p in ('scripts', 'src/channel-gateway/app', 'tests-gateway'))
    result = subprocess.run([sys.executable, '-c', child, str(db), str(receipt)],
                            env=env, timeout=20, check=False, capture_output=True)
    assert result.returncode == 73, result.stderr.decode()
    assert receipt.read_text() == 'a'
    restarted = make_gateway(db)
    _, row = recover(restarted)
    assert row['state'] == 'UNKNOWN'
    run(restarted.process(row))
    assert restarted.discord_bot.calls == []
    assert [r['state'] for r in rows(restarted)] == ['UNKNOWN', 'NOT_SENT']


def test_exception_after_send_cannot_be_settled_as_known_failure(tmp_path):
    gw = make_gateway(tmp_path/'gw.db'); row, msg = seed(gw)
    with patch.object(store, 'record_discord_receipt', side_effect=OSError('synthetic disk failure')):
        with pytest.raises(OSError):
            run(gw.deliver_discord(row, msg, 'a|b'))
    gw._fail(row, 'internal')
    with gw.db() as con:
        assert store.get_inbound(con, row['gateway_request_id'])['state'] == 'UNKNOWN'
    con.close()
    # A direct caller with a stale PROCESSING snapshot also cannot resend.
    gw.discord_bot = Bot()
    assert not run(gw.deliver_discord(row, msg, 'a|b'))[0]
    assert gw.discord_bot.calls == []


def test_in_flight_response_preserves_live_sender_claim(tmp_path):
    gw = make_gateway(tmp_path/'gw.db'); row, msg = seed(gw)
    class Concurrent(Bot):
        async def send_chunks(self, channel, chunks):
            another = make_gateway(tmp_path/'gw.db')
            await another.deliver(row, msg, 'a|b')
            assert another.discord_bot.calls == []
            with another.db() as con:
                current = store.get_inbound(con, row['gateway_request_id'])
                assert current['state'] == 'PROCESSING'
                assert con.execute('SELECT active_request_id FROM conversation_state').fetchone()[0] == row['gateway_request_id']
            con.close()
            return await super().send_chunks(channel, chunks)
    gw.discord_bot = Concurrent()
    run(gw.deliver(row, msg, 'a|b'))
    assert len(gw.discord_bot.calls) == 2


def test_legacy_sent_prefix_and_pending_tail_is_held(tmp_path):
    gw = make_gateway(tmp_path/'gw.db'); row, msg = seed(gw)
    gw.discord_bot = Bot([(1, None), (0, 'channel_unavailable:10003')])
    run(gw.deliver_discord(row, msg, 'a|b|c'))
    with gw.db() as con:
        con.execute("UPDATE outbound_messages SET state='PENDING' WHERE state='NOT_SENT'")
    con.close()
    gw.discord_bot = Bot()
    _, current = recover(gw)
    assert current['state'] == 'UNKNOWN'
    run(gw.process(current))
    assert gw.discord_bot.calls == []


def test_out_of_order_receipts_are_held_not_completed(tmp_path):
    gw = make_gateway(tmp_path/'gw.db'); row, msg = seed(gw)
    gw.discord_bot = Bot([(0, 'channel_unavailable:10003')])
    run(gw.deliver_discord(row, msg, 'a|b'))
    with gw.db() as con:
        con.execute("UPDATE outbound_messages SET state='SENT' WHERE chunk_index=1")
    con.close()
    gw.discord_bot = Bot()
    assert run(gw.deliver_discord(row, msg, 'a|b'))[1] == 'reply_order_mismatch'
    assert gw.discord_bot.calls == []


def test_expired_unsent_body_is_held_not_regenerated(tmp_path):
    gw = make_gateway(tmp_path/'gw.db'); row, msg = seed(gw)
    gw.discord_bot = Bot([(0, 'channel_unavailable:10003')])
    run(gw.deliver_discord(row, msg, 'a|b'))
    with gw.db() as con:
        con.execute("UPDATE outbound_messages SET body='' ")
    con.close()
    gw.discord_bot = Bot()
    assert run(gw.deliver_discord(row, msg, None))[1] == 'reply_body_expired'
    assert gw.discord_bot.calls == []


def test_cancelled_request_cannot_gain_permission_by_retry(tmp_path):
    gw = make_gateway(tmp_path/'gw.db'); row, msg = seed(gw)
    with gw.db() as con:
        store.set_state(con, row['gateway_request_id'], 'CANCELLED')
    con.close()
    run(gw.deliver(row, msg, 'a|b'))
    assert gw.discord_bot.calls == []
    with gw.db() as con:
        assert store.get_inbound(con, row['gateway_request_id'])['state'] == 'CANCELLED'
    con.close()


def test_late_receipt_cannot_silently_clear_recovered_hold(tmp_path):
    gw = make_gateway(tmp_path/'gw.db'); row, msg = seed(gw)
    gw.discord_bot = Bot([asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        run(gw.deliver_discord(row, msg, 'a|b'))
    _, current = recover(gw)
    assert current['state'] == 'UNKNOWN'
    with gw.db() as con:
        assert not store.record_discord_receipt(con, rows(gw)[0]['outbound_id'], state='SENT')
    con.close()
    assert rows(gw)[0]['state'] == 'UNKNOWN'


def test_claim_does_not_skip_an_unknown_middle_chunk(tmp_path):
    gw = make_gateway(tmp_path/'gw.db'); row, msg = seed(gw)
    gw.discord_bot = Bot([(1, None), (0, 'channel_unavailable:10003')])
    run(gw.deliver_discord(row, msg, 'a|b|c'))
    with gw.db() as con:
        con.execute("UPDATE outbound_messages SET state='UNKNOWN' WHERE chunk_index=1")
    con.close()
    with gw.db() as con:
        status, claimed = store.claim_discord_chunk(con, row['gateway_request_id'])
    con.close()
    assert status == 'delivery_outcome_unknown' and claimed is None
    assert rows(gw)[2]['state'] == 'NOT_SENT'


def test_interruption_before_claim_resumes_all_frozen_chunks(tmp_path):
    gw = make_gateway(tmp_path/'gw.db'); row, msg = seed(gw)
    with patch.object(store, 'claim_discord_chunk', side_effect=asyncio.CancelledError()), pytest.raises(asyncio.CancelledError):
        run(gw.deliver_discord(row, msg, 'a|b'))
    assert [r['state'] for r in rows(gw)] == ['NOT_SENT', 'NOT_SENT']
    assert gw.discord_bot.calls == []
    restarted = make_gateway(tmp_path/'gw.db'); _, current = recover(restarted)
    run(restarted.process(current))
    assert [c[1] for c in restarted.discord_bot.calls] == [['a'], ['b']]


def test_real_adapter_lookup_failure_never_reaches_channel_send():
    from gw_discord import InterfaceBot
    class LookupFails:
        def get_channel(self, channel_id):
            return None
        async def fetch_channel(self, channel_id):
            raise TimeoutError('synthetic lookup failed before message send')
        async def _send_to(self, channel, chunks):
            raise AssertionError('lookup failure must not attempt message delivery')
    assert run(InterfaceBot.send_chunks(LookupFails(), fx.DM_CHANNEL_ID, ['a'])) == (0, 'channel_unavailable:TimeoutError')
