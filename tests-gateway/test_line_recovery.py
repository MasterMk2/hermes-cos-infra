"""Ordinary LINE boundary regressions. All HTTP is a synthetic in-memory fake."""
import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
import gw_fixtures as fx
import gw_store as store
from gw_canonical import canonical_from_line
from gw_config import parse_config
from gw_line import PUSH_ENDPOINT
from gw_line import REPLY_ENDPOINT, line_length
from gw_line_client import LineClient
from gw_service import Gateway


class AcceptedThenTimeout(asyncio.TimeoutError):
    """The fake provider has accepted the body, but the caller loses the receipt."""


class Response:
    def __init__(self, step):
        self.step = step
        if isinstance(step, int):
            step = (step, {}, {})
        if isinstance(step, tuple):
            self.status, self.body, self.headers = step
    async def __aenter__(self):
        if isinstance(self.step, BaseException):
            raise self.step
        return self
    async def __aexit__(self, *args):
        return False
    async def text(self):
        return json.dumps(self.body)


class Session:
    def __init__(self, steps):
        self.steps = list(steps); self.calls = []; self.accepted = []
    def post(self, url, *, data, headers, timeout):
        self.calls.append({'url': url, 'body': json.loads(data), 'headers': headers})
        assert self.steps, 'unexpected duplicate external call'
        step = self.steps.pop(0)
        if isinstance(step, AcceptedThenTimeout) or step == 200:
            self.accepted.append(json.loads(data))
        return Response(step)


def gateway(tmp_path, steps=(), age=0, config=None):
    cfg = parse_config(fx.config_dict(paths={'state_db': str(tmp_path/'gw.db')}, **(config or {})))
    gw = Gateway(cfg, {'hermes_api_key': 'synthetic-test-key'})
    gw.line = LineClient('synthetic-line-token')
    gw.session = Session(steps)
    msg = canonical_from_line(fx.line_text_event(), owner_line_user_id=fx.OWNER_LINE)
    msg = replace(msg, received_at=(datetime.now(timezone.utc)-timedelta(seconds=age)).isoformat(), timestamp=(datetime.now(timezone.utc)-timedelta(seconds=age)).isoformat())
    con = gw.db()
    try:
        rid, new = store.record_inbound(con, msg)
        if new:
            store.enqueue(con, rid); store.claim_next(con, max_active=1)
        con.commit(); row = store.get_inbound(con, rid)
    finally:
        con.close()
    return gw, row, msg


def run(coro):
    async def fast_sleep(_): pass
    with patch('gw_line_client.asyncio.sleep', fast_sleep):
        return asyncio.run(coro)


def outbound(gw, row):
    con = gw.db()
    try: return store.outbound_for_request(con, row['gateway_request_id'])
    finally: con.close()


def recover(gw, row):
    con=gw.db()
    try:
        con.execute("UPDATE inbound_messages SET claimed_at='2000-01-01', attempts=1")
        store.recover_stale(con); con.commit()
        return store.get_inbound(con, row['gateway_request_id'])
    finally: con.close()


def test_reply_acceptance_lost_then_used_token_never_falls_back_to_push(tmp_path):
    # First synthetic reply was accepted, but its response was lost. Old code
    # then saw used-token 400 and sent the same answer again via push.
    gw,row,msg=gateway(tmp_path,[AcceptedThenTimeout(),(400,{'message':'Invalid reply token'},{}),200])
    ok,detail,mode=run(gw.deliver_line(row,msg,'synthetic answer'))
    assert not ok and detail == 'delivery_outcome_unknown'
    assert [c['url'] for c in gw.session.calls] == [REPLY_ENDPOINT]
    assert len(gw.session.accepted) == 1
    assert outbound(gw,row)[0]['state'] == 'UNKNOWN'


def test_accepted_reply_crash_cannot_become_push_after_restart(tmp_path):
    gw,row,msg=gateway(tmp_path,[asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError): run(gw.deliver_line(row,msg,'first answer'))
    assert outbound(gw,row)[0]['state'] == 'SENDING'
    restarted,_,_=gateway(tmp_path,[200],age=90)
    current=recover(restarted,row)
    assert current['state'] == 'UNKNOWN'
    run(restarted.deliver_line(current,replace(msg,received_at='2000-01-01T00:00:00+00:00'),'different answer'))
    assert restarted.session.calls == []


def test_push_crash_after_acceptance_beyond_24h_never_replays(tmp_path):
    gw,row,msg=gateway(tmp_path,[asyncio.CancelledError()],age=90)
    with pytest.raises(asyncio.CancelledError): run(gw.deliver_line(row,msg,'first answer'))
    con=gw.db()
    con.execute("UPDATE outbound_messages SET created_at='2000-01-01',updated_at='2000-01-01'")
    con.commit();con.close()
    restarted,_,_=gateway(tmp_path,[200],age=90000)
    current=recover(restarted,row)
    run(restarted.deliver_line(current,msg,'first answer'))
    assert restarted.session.calls == [] and current['state'] == 'UNKNOWN'


def test_changed_body_cannot_be_acknowledged_from_original_push_key(tmp_path):
    gw,row,msg=gateway(tmp_path,[asyncio.CancelledError()],age=90)
    with pytest.raises(asyncio.CancelledError): run(gw.deliver_line(row,msg,'original body'))
    restarted,_,_=gateway(tmp_path,[(409,{'message':'The retry key is already accepted'},{'x-line-accepted-request-id':'synthetic-receipt'})],age=90)
    ok,_,_=run(restarted.deliver_line(row,msg,'different body'))
    assert not ok and restarted.session.calls == []
    assert outbound(restarted,row)[0]['body'] == 'original body'


@pytest.mark.parametrize('status,body',[(400,{'message':'invalid message object'}),(409,{'message':'unexpected conflict'})])
def test_reply_400_or_409_is_not_push_permission_or_success(tmp_path,status,body):
    gw,row,msg=gateway(tmp_path,[(status,body,{}),200])
    ok,_,_=run(gw.deliver_line(row,msg,'synthetic answer'))
    assert not ok and len(gw.session.calls)==1
    assert outbound(gw,row)[0]['state']=='UNKNOWN'


@pytest.mark.parametrize('age,token,expected',[(0,'synthetic-token',REPLY_ENDPOINT),(90,'synthetic-token',PUSH_ENDPOINT),(0,None,PUSH_ENDPOINT)])
def test_fresh_unsent_intent_selects_reply_or_push_once(tmp_path,age,token,expected):
    gw,row,msg=gateway(tmp_path,[200],age=age)
    msg=replace(msg,reply_token=token)
    with gw.db() as con:
        con.execute('UPDATE inbound_messages SET reply_token=?',(token,))
    con.close()
    assert run(gw.deliver_line(row,msg,'answer'))[0]
    assert [c['url'] for c in gw.session.calls]==[expected]
    assert outbound(gw,row)[0]['delivery_mode']==('reply' if expected==REPLY_ENDPOINT else 'push')
    assert outbound(gw,row)[0]['state']=='SENT'
    assert run(gw.deliver_line(row,msg,'answer'))[0]
    assert len(gw.session.calls)==1


@pytest.mark.parametrize('headers,accepted',[({'x-line-accepted-request-id':'synthetic-receipt'},True),({},False)])
def test_push_409_requires_acceptance_evidence(tmp_path,headers,accepted):
    gw,row,msg=gateway(tmp_path,[(409,{'message':'The retry key is already accepted'},headers)],age=90)
    assert run(gw.deliver_line(row,msg,'answer'))[0] is accepted
    assert outbound(gw,row)[0]['state']==('SENT' if accepted else 'UNKNOWN')


def test_push_internal_retry_keeps_exact_body_target_and_key(tmp_path):
    gw,row,msg=gateway(tmp_path,[asyncio.TimeoutError(),(409,{}, {'x-line-accepted-request-id':'synthetic-receipt'})],age=90)
    assert run(gw.deliver_line(row,msg,'answer'))[0]
    assert len(gw.session.calls)==2
    assert gw.session.calls[0] == gw.session.calls[1]
    assert all(c['url']==PUSH_ENDPOINT for c in gw.session.calls)


@pytest.mark.parametrize('age',[86400,90000,-60])
def test_expired_or_backwards_push_window_makes_no_http_call(tmp_path,age):
    gw,_,_=gateway(tmp_path,[])
    started=(datetime.now(timezone.utc)-timedelta(seconds=age)).isoformat()
    result=run(gw.line.push(gw.session,fx.OWNER_LINE,[{'type':'text','text':'answer'}],outbound_id='synthetic-id',started_at=started))
    assert not result.ok and result.category=='retry_window_expired'
    assert gw.session.calls==[]


def test_retry_window_closes_between_internal_attempts(tmp_path):
    gw,_,_=gateway(tmp_path,[asyncio.TimeoutError()])
    with patch('gw_line_client.push_retry_window_open',side_effect=[True,False]):
        result=run(gw.line.push(gw.session,fx.OWNER_LINE,[{'type':'text','text':'answer'}],outbound_id='synthetic-id'))
    assert not result.ok and len(gw.session.calls)==1


@pytest.mark.parametrize('legacy',['PENDING','FAILED','ABANDONED'])
def test_legacy_line_rows_are_not_replay_authority(tmp_path,legacy):
    gw,row,msg=gateway(tmp_path,[],age=90)
    with gw.db() as con:
        oid,_=store.reserve_outbound(con,request_id=row['gateway_request_id'],platform='line',conversation_id=row['conversation_id'],target_user_id=fx.OWNER_LINE,body='answer',chunk_index=0,chunk_total=1,delivery_mode='pending')
        store.mark_outbound(con,oid,legacy)
    con.close()
    assert not run(gw.deliver_line(row,msg,'answer'))[0]
    assert gw.session.calls==[] and outbound(gw,row)[0]['state']=='UNKNOWN'


@pytest.mark.parametrize('change',['body','count','owner','conversation'])
def test_frozen_batch_scope_cannot_change_before_first_send(tmp_path,change):
    gw,row,msg=gateway(tmp_path,[],age=90)
    with patch.object(store,'claim_line_batch',side_effect=asyncio.CancelledError()),pytest.raises(asyncio.CancelledError):
        run(gw.deliver_line(row,msg,'answer'))
    if change=='owner':gw.cfg=replace(gw.cfg,owner_line_user_id=fx.STRANGER_LINE)
    if change=='conversation':row=dict(row);row['conversation_id']='line:'+fx.STRANGER_LINE
    text='changed' if change=='body' else 'あ'*9601 if change=='count' else 'answer'
    assert not run(gw.deliver_line(row,msg,text))[0]
    assert gw.session.calls==[] and outbound(gw,row)[0]['body']=='answer'


def test_frozen_reply_method_never_switches_to_push_after_wait(tmp_path):
    gw,row,msg=gateway(tmp_path,[(400,{'message':'Invalid reply token'},{}),200])
    with patch.object(store,'claim_line_batch',side_effect=asyncio.CancelledError()),pytest.raises(asyncio.CancelledError):
        run(gw.deliver_line(row,msg,'answer'))
    old=replace(msg,received_at='2000-01-01T00:00:00+00:00')
    assert not run(gw.deliver_line(row,old,None))[0]
    assert [c['url'] for c in gw.session.calls]==[REPLY_ENDPOINT]


def test_receipt_write_failure_stays_in_flight_then_unknown(tmp_path):
    gw,row,msg=gateway(tmp_path,[200])
    with patch.object(store,'record_line_receipt',side_effect=OSError('synthetic')),pytest.raises(OSError):
        run(gw.deliver_line(row,msg,'answer'))
    gw._fail(row,'internal')
    assert outbound(gw,row)[0]['state']=='SENDING'
    assert recover(gw,row)['state']=='UNKNOWN'
    assert not run(gw.deliver_line(row,msg,'answer'))[0]
    assert len(gw.session.calls)==1


def test_batch_claim_is_atomic_across_connections(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    gw,row,msg=gateway(tmp_path,[],age=90)
    with patch.object(store,'claim_line_batch',side_effect=asyncio.CancelledError()),pytest.raises(asyncio.CancelledError):
        run(gw.deliver_line(row,msg,'あ'*10000))
    def claim(_):
        con=gw.db()
        try:
            result=store.claim_line_batch(con,row['gateway_request_id']);con.commit();return result[0]
        finally:con.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim,range(2)))==['in_flight','send']
    assert all(r['state']=='SENDING' for r in outbound(gw,row))


@pytest.mark.parametrize('text',['あ'*25000,'😀'*15000,'あ'*4800,'😀'*2501])
def test_five_message_utf16_boundary_and_truncation_match_durable_batch(tmp_path,text):
    gw,row,msg=gateway(tmp_path,[200],age=90,config={'line':{'max_messages_per_request':9,'text_chunk_chars':5000},'limits':{'response_hard_limit_chars':99999}})
    assert run(gw.deliver_line(row,msg,text))[0]
    saved=outbound(gw,row)
    messages=gw.session.calls[0]['body']['messages']
    assert 1<=len(saved)==len(messages)<=5
    assert [r['body'] for r in saved]==[m['text'] for m in messages]
    assert all(line_length(m['text'])<=5000 for m in messages)
    if line_length(text)>25000:assert '省略' in messages[-1]['text']


@pytest.mark.parametrize('count',[0,6])
def test_transport_never_silently_slices_invalid_batch(tmp_path,count):
    gw,_,_=gateway(tmp_path,[])
    result=run(gw.line.push(gw.session,fx.OWNER_LINE,[{'type':'text','text':'a'}]*count,outbound_id='synthetic-id'))
    assert not result.ok and result.not_sent and result.category=='invalid_batch'
    assert gw.session.calls==[]


def test_definite_local_preflight_failure_is_not_unknown_or_push(tmp_path):
    gw,row,msg=gateway(tmp_path,[])
    # Reserve a reply, then simulate locally unavailable token before I/O.
    with patch.object(store,'claim_line_batch',side_effect=asyncio.CancelledError()),pytest.raises(asyncio.CancelledError):
        run(gw.deliver_line(row,msg,'answer'))
    with gw.db() as con:con.execute('UPDATE inbound_messages SET reply_token=NULL')
    con.close()
    assert run(gw.deliver_line(row,msg,None))[1]=='no_reply_token'
    assert outbound(gw,row)[0]['state']=='NOT_SENT'
    assert gw.session.calls==[]


def test_push_transport_budget_keeps_one_key_and_then_holds(tmp_path):
    gw,row,msg=gateway(tmp_path,[500,500,500],age=90)
    assert not run(gw.deliver_line(row,msg,'answer'))[0]
    assert len(gw.session.calls)==3
    assert all(c==gw.session.calls[0] for c in gw.session.calls)
    assert outbound(gw,row)[0]['state']=='UNKNOWN'
    assert not run(gw.deliver_line(row,msg,'answer'))[0]
    assert len(gw.session.calls)==3


def test_push_429_is_not_retried(tmp_path):
    gw,row,msg=gateway(tmp_path,[429],age=90)
    assert not run(gw.deliver_line(row,msg,'answer'))[0]
    assert len(gw.session.calls)==1 and outbound(gw,row)[0]['state']=='UNKNOWN'


def test_reply_409_with_acceptance_header_is_still_not_push_receipt(tmp_path):
    gw,row,msg=gateway(tmp_path,[(409,{}, {'x-line-accepted-request-id':'synthetic-receipt'})])
    assert not run(gw.deliver_line(row,msg,'answer'))[0]
    assert len(gw.session.calls)==1


def test_old_unsent_batch_never_resets_first_attempt_window(tmp_path):
    gw,row,msg=gateway(tmp_path,[],age=90)
    with patch.object(store,'claim_line_batch',side_effect=asyncio.CancelledError()),pytest.raises(asyncio.CancelledError):
        run(gw.deliver_line(row,msg,'answer'))
    with gw.db() as con:
        assert store.claim_line_batch(con,row['gateway_request_id'])[0]=='send'
    con.close()
    with gw.db() as con:
        assert store.record_line_receipt(con,row['gateway_request_id'],ok=False,status_code=0,category='no_owner',not_sent=True)=='NOT_SENT'
        con.execute("UPDATE delivery_attempts SET attempted_at='2000-01-01T00:00:00+00:00' WHERE outcome='started'")
    con.close()
    assert not run(gw.deliver_line(row,msg,None))[0]
    assert gw.session.calls==[] and outbound(gw,row)[0]['state']=='UNKNOWN'


def test_recovered_hold_cannot_be_cleared_by_late_receipt(tmp_path):
    gw,row,msg=gateway(tmp_path,[asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):run(gw.deliver_line(row,msg,'answer'))
    recover(gw,row)
    with gw.db() as con:
        assert not store.record_line_receipt(con,row['gateway_request_id'],ok=True,status_code=200,category='ok')
    con.close()
    assert outbound(gw,row)[0]['state']=='UNKNOWN'


def test_confirmed_batch_survives_crash_before_inbound_settlement(tmp_path):
    gw,row,msg=gateway(tmp_path,[200])
    assert run(gw.deliver_line(row,msg,'answer'))[0]
    restarted,_,_=gateway(tmp_path,[])
    current=recover(restarted,row)
    assert current['state']=='RESPONDED'
    assert run(restarted.deliver_line(current,msg,None))[0]
    assert restarted.session.calls==[]


def test_resuming_unsent_batch_does_not_regenerate_answer(tmp_path):
    gw,row,msg=gateway(tmp_path,[],age=90)
    with patch.object(store,'claim_line_batch',side_effect=asyncio.CancelledError()),pytest.raises(asyncio.CancelledError):
        run(gw.deliver_line(row,msg,'original answer'))
    restarted,_,_=gateway(tmp_path,[200],age=90)
    current=recover(restarted,row)
    async def no_regeneration(*a,**kw): raise AssertionError('immutable batch must be reused')
    restarted.hermes.ask_async=no_regeneration
    def no_new_run(*a,**kw): raise AssertionError('immutable batch must be reused')
    restarted.hermes.start_run=no_new_run
    restarted.hermes.get_run=no_new_run
    run(restarted.process(current))
    assert restarted.session.calls[0]['body']['messages']==[{'type':'text','text':'original answer'}]


def test_cancelled_line_request_does_not_gain_retry_permission(tmp_path):
    gw,row,msg=gateway(tmp_path,[])
    with gw.db() as con:store.set_state(con,row['gateway_request_id'],'CANCELLED')
    con.close()
    run(gw.deliver(row,msg,'answer'))
    assert gw.session.calls==[]


def test_redelivery_age_limit_is_not_a_new_reply_token(tmp_path):
    gw,row,msg=gateway(tmp_path,[200])
    old=replace(msg,timestamp=(datetime.now(timezone.utc)-timedelta(minutes=21)).isoformat())
    assert run(gw.deliver_line(row,old,'answer'))[0]
    assert gw.session.calls[0]['url']==PUSH_ENDPOINT


@pytest.mark.parametrize('age',[0,90])
def test_real_process_exit_after_line_acceptance_never_replays(tmp_path,age):
    import os
    from pathlib import Path
    import subprocess
    import sys
    receipt=tmp_path/'synthetic-receipt.txt'
    child=r'''
import asyncio, os, sys
from pathlib import Path
import offline_guard
offline_guard.pytest_configure(None)
from test_line_recovery import gateway
class ExitResponse:
    async def __aenter__(self):
        with open(sys.argv[2], 'w') as out:
            out.write('synthetic provider acceptance');out.flush();os.fsync(out.fileno())
        assert not offline_guard._attempts
        os._exit(73)
    async def __aexit__(self,*a):return False
class ExitSession:
    def post(self,*a,**kw):return ExitResponse()
gw,row,msg=gateway(Path(sys.argv[1]),age=int(sys.argv[3]))
gw.session=ExitSession()
asyncio.run(gw.deliver_line(row,msg,'answer'))
'''
    env=os.environ.copy()
    root=Path(__file__).resolve().parents[1]
    env['PYTHONPATH']=os.pathsep.join(str(root/p) for p in ('scripts','src/channel-gateway/app','tests-gateway'))
    result=subprocess.run([sys.executable,'-c',child,str(tmp_path),str(receipt),str(age)],env=env,capture_output=True,timeout=20)
    assert result.returncode==73,result.stderr.decode()
    assert receipt.read_text()=='synthetic provider acceptance'
    restarted,row,msg=gateway(tmp_path,[],age=90000)
    current=recover(restarted,row)
    run(restarted.process(current))
    assert current['state']=='UNKNOWN' and restarted.session.calls==[]


@pytest.mark.parametrize('soft,hard',[(4000,'20000'),('4000',20000),('4000','20000')])
def test_quoted_yaml_response_limits_preserve_numeric_behavior(tmp_path,soft,hard):
    # parse_config intentionally preserves quoted YAML scalars; both limits
    # used to be normalized by the LINE call site before formatting.
    gw,row,msg=gateway(tmp_path,[200],age=90,config={'limits':{'response_soft_limit_chars':soft,'response_hard_limit_chars':hard}})
    assert run(gw.deliver_line(row,msg,'あ'*21000))[0]
    saved=outbound(gw,row)
    assert 1<=len(saved)<=5 and all(line_length(r['body'])<=5000 for r in saved)
    assert [r['body'] for r in saved]==[m['text'] for m in gw.session.calls[0]['body']['messages']]
    assert '結果が長いため' in saved[-1]['body']
