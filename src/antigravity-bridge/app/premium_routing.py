"""Opt-in, bounded CLI-first reasoning route. No prompt content is logged."""
from __future__ import annotations

import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

COMPLEX = re.compile(
    r"要件定義|要件整理|仕様策定|仕様設計|基本設計|詳細設計|設計して|設計を|設計案|"
    r"難問|難しい問題|根本原因|原因分析|トレードオフ|論証|証明して|証明を|"
    r"\brequirements?\b|\barchitecture\b|\broot cause\b|\btrade[ -]?offs?\b|\bprove\b",
    re.IGNORECASE,
)


def latest_user_text(messages):
    for message in reversed(messages):
        if message.get('role') != 'user':
            continue
        content = message.get('content', '')
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return '\n'.join(p.get('text', '') for p in content
                             if isinstance(p, dict) and p.get('type') == 'text')
        return ''
    return ''


def should_try_premium(messages, enabled=False):
    return bool(enabled and COMPLEX.search(latest_user_text(messages)))


def validate_endpoint(endpoint):
    p = urlparse(endpoint)
    if (p.scheme != 'http' or p.hostname not in {'127.0.0.1', 'localhost', '::1'}
            or p.username or p.password or p.query or p.fragment
            or p.path not in {'', '/'}):
        raise ValueError('premium_cli endpoint must be an HTTP loopback origin')
    if not p.port:
        raise ValueError('premium_cli endpoint requires an explicit port')
    return endpoint.rstrip('/')


def try_premium(messages, *, endpoint, token, timeout_seconds, opener=urlopen):
    """Return (completion or None, fixed status). The default route owns fallback."""
    started = time.monotonic()
    try:
        origin = validate_endpoint(endpoint)
        if not token or timeout_seconds <= 0:
            return None, 'not_configured'
        body = json.dumps({'messages': messages, 'budget_seconds': timeout_seconds}, ensure_ascii=False).encode('utf-8')
        if len(body) > 262144:
            return None, 'context_too_large'
        req = Request(origin + '/reason', data=body, method='POST', headers={
            'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
        })
        with opener(req, timeout=timeout_seconds) as response:
            raw = response.read(1048577)
        if len(raw) > 1048576:
            return None, 'invalid_response'
        data = json.loads(raw)
        if data.get('status') != 'answer':
            return None, 'needs_context' if data.get('status') == 'needs_context' else 'unavailable'
        if data.get('model') not in {'gpt-6-astra', 'claude-fable-5'}:
            return None, 'invalid_response'
        if {'codex': 'gpt-6-astra', 'claude': 'claude-fable-5'}.get(data.get('backend')) != data['model']:
            return None, 'invalid_response'
        answer = data.get('answer')
        if not isinstance(answer, str) or not answer.strip():
            return None, 'invalid_response'
        return {
            'id': 'chatcmpl-premium-' + str(time.time_ns()), 'object': 'chat.completion',
            'created': int(time.time()), 'model': data['model'],
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': answer}, 'finish_reason': 'stop'}],
            'usage': data.get('usage') if isinstance(data.get('usage'), dict) else {},
            'hermes_route': {'backend': data['backend'], 'latency_seconds': round(time.monotonic() - started, 3)},
        }, 'success'
    except HTTPError as exc:
        return None, 'authentication' if exc.code in (401, 403) else 'unavailable'
    except (URLError, TimeoutError, OSError):
        return None, 'unavailable'
    except (ValueError, TypeError, KeyError, AttributeError):
        return None, 'invalid_response'
