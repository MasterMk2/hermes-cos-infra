import io
import json
from pathlib import Path
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import URLError

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src/antigravity-bridge/app'))
sys.path.insert(0, str(ROOT / 'src/premium-cli/app'))
from bridge_config import BridgeConfig, load_config
from bridge_server import execute_routed
from cli_reasoner import command_for, parse_answer, reason
from premium_routing import should_try_premium, try_premium, validate_endpoint
from premium_server import make_handler


@pytest.mark.parametrize('text', ['要件定義をお願いします', '難問を考えて', 'root cause analysis', '基本設計を整理して'])
def test_complex_requests_use_premium(text):
    assert should_try_premium([{'role': 'user', 'content': text}], True)
    assert not should_try_premium([{'role': 'user', 'content': text}], False)


def test_history_and_tool_text_do_not_upgrade_routine_queries():
    messages = [{'role': 'user', 'content': '要件定義'}, {'role': 'tool', 'content': '難問'},
                {'role': 'user', 'content': '今日の予定は？'}]
    assert not should_try_premium(messages, True)


@pytest.mark.parametrize('endpoint', ['https://example.invalid', 'http://0.0.0.0:8000',
                                     'http://user:pass@127.0.0.1:8000', 'http://127.0.0.1:8000/private'])
def test_endpoint_cannot_forward_context_to_an_arbitrary_host(endpoint):
    with pytest.raises(ValueError):
        validate_endpoint(endpoint)


def test_cli_order_and_fallback():
    calls = []
    def runner(backend, messages, timeout):
        calls.append(backend)
        return (None, 'timeout') if backend == 'claude' else ({'status': 'answer', 'answer': 'ok'}, 'success')
    result = reason([], runner=runner)
    assert calls == ['claude', 'codex']
    assert result['model'] == 'gpt-6-astra'


def test_missing_live_context_returns_to_the_source_backend():
    calls = []
    def runner(backend, messages, timeout):
        calls.append(backend)
        return {'status': 'needs_context', 'answer': ''}, 'success'
    assert reason([], runner=runner)['status'] == 'needs_context'
    assert calls == ['claude']


def test_both_clis_unavailable_is_an_explicit_fallback_result():
    assert reason([], runner=lambda *_: (None, 'not_installed'))['status'] == 'unavailable'


def test_network_unavailable_uses_existing_antigravity_with_remaining_budget():
    cfg = BridgeConfig(premium_cli_enabled=True, premium_cli_token='x' * 40)
    with patch('premium_routing.try_premium', return_value=(None, 'unavailable')), \
         patch('bridge_server.execute_antigravity', return_value=(200, {'model': cfg.default_model})) as cheap:
        status, response = execute_routed([{'role': 'user', 'content': '要件定義'}], 'prompt', cfg, cfg.default_model)
    assert status == 200
    assert response['model'] == cfg.default_model
    assert 0 < cheap.call_args.kwargs['config'].timeout_seconds <= cfg.timeout_seconds


def test_success_does_not_also_call_the_cheaper_model():
    cfg = BridgeConfig(premium_cli_enabled=True)
    with patch('premium_routing.try_premium', return_value=({'model': 'gpt-6-astra'}, 'success')), \
         patch('bridge_server.execute_antigravity') as cheap:
        assert execute_routed([{'role': 'user', 'content': '要件定義'}], 'prompt', cfg, cfg.default_model)[1]['model'] == 'gpt-6-astra'
    cheap.assert_not_called()


def test_authenticated_http_boundary_and_model_identity():
    token = 't' * 40
    def runner(messages, budget_seconds):
        return {'status': 'answer', 'answer': '設計案', 'backend': 'codex', 'model': 'gpt-6-astra'}
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(token, runner))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = 'http://127.0.0.1:' + str(server.server_port)
        args = dict(endpoint=endpoint, timeout_seconds=2)
        response, status = try_premium([{'role': 'user', 'content': '要件定義'}], token=token, **args)
        assert status == 'success' and response['model'] == 'gpt-6-astra'
        assert response['choices'][0]['message']['content'] == '設計案'
        assert try_premium([], token='wrong', **args)[1] == 'authentication'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_config_is_opt_in_and_rejects_string_booleans(tmp_path):
    path = tmp_path / 'config.yaml'
    path.write_text('premium_cli:\n  enabled: "false"\n', encoding='utf-8')
    with pytest.raises(ValueError):
        load_config(str(path))


def test_codex_parser_requires_successful_completion():
    message = {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '{"status":"answer","answer":"ok"}'}}
    assert parse_answer('codex', json.dumps(message)) is None
    output = '\n'.join(map(json.dumps, [message, {'type': 'turn.completed', 'usage': {'input_tokens': 5, 'output_tokens': 2}}]))
    assert parse_answer('codex', output)['usage']['total_tokens'] == 7


def test_claude_failure_is_not_a_successful_answer():
    assert parse_answer('claude', '{"is_error":true,"result":"login required"}') is None


def test_cli_commands_disable_customizations_and_use_exact_requested_models(tmp_path):
    codex = command_for('codex', ['codex'], tmp_path)
    claude = command_for('claude', ['claude'], tmp_path)
    assert 'gpt-6-astra' in codex and '--ignore-user-config' in codex
    assert codex[codex.index('--sandbox') + 1] == 'read-only'
    assert 'model_reasoning_effort="medium"' in codex
    assert 'claude-fable-5' in claude and '--safe-mode' in claude
    assert claude[claude.index('--tools') + 1] == ''
    assert claude[claude.index('--effort') + 1] == 'high'
    assert '--no-session-persistence' in claude and '--ephemeral' in codex
