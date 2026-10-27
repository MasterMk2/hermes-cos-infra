#!/usr/bin/env python3
"""Unified Channel Gateway — client for the Coding Handoff Broker (J.1).

A deliberately small client for a deliberately small server. It validates that
the endpoint is a loopback origin before it will send anything — the same check
the deployed premium-CLI route applies, and for the same reason: a
configuration typo must not turn a job-creation call into a request to some
other host.

Failures are values, not exceptions. Every call returns `(payload, status)`
where `status` is a fixed vocabulary, and the caller decides what the owner is
told. A broker that is down must produce "I could not reach the worker
intake", never a half-rendered success.
"""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

OK = "ok"
UNAVAILABLE = "unavailable"
AUTHENTICATION = "authentication"
INVALID_RESPONSE = "invalid_response"
NOT_CONFIGURED = "not_configured"

#: Replies are small structures. Anything larger is not a reply we understand.
MAX_RESPONSE_BYTES = 262_144


def validate_endpoint(endpoint: str) -> str:
    parsed = urlparse(str(endpoint or ""))
    if (parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username or parsed.password
            or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}):
        raise ValueError("coding_handoff endpoint must be an HTTP loopback origin")
    if not parsed.port:
        raise ValueError("coding_handoff endpoint requires an explicit port")
    return str(endpoint).rstrip("/")


class HandoffClient:
    def __init__(self, endpoint: str, token: str, *, timeout_seconds: float = 20.0,
                 opener=urlopen):
        self.endpoint = endpoint
        self.token = token
        self.timeout_seconds = float(timeout_seconds)
        self._opener = opener

    def _call(self, path: str, body: dict) -> tuple:
        try:
            origin = validate_endpoint(self.endpoint)
        except ValueError:
            return None, NOT_CONFIGURED
        if not self.token or self.timeout_seconds <= 0:
            return None, NOT_CONFIGURED
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = Request(origin + path, data=data, method="POST", headers={
            "Authorization": "Bearer " + self.token,
            "Content-Type": "application/json",
        })
        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            return None, AUTHENTICATION if exc.code in (401, 403) else UNAVAILABLE
        except (URLError, TimeoutError, OSError):
            return None, UNAVAILABLE
        if len(raw) > MAX_RESPONSE_BYTES:
            return None, INVALID_RESPONSE
        try:
            payload = json.loads(raw)
        except ValueError:
            return None, INVALID_RESPONSE
        if not isinstance(payload, dict):
            return None, INVALID_RESPONSE
        return payload, OK

    def projects(self) -> tuple:
        return self._call("/handoff/projects", {})

    def preflight(self, project_key: str) -> tuple:
        return self._call("/handoff/preflight", {"project_key": project_key})

    def authorize(self, *, authorization_id: str, conversation_id: str,
                  project_key: str, scope_digest: str) -> tuple:
        return self._call("/handoff/authorize", {
            "authorization_id": authorization_id,
            "conversation_id": conversation_id,
            "project_key": project_key,
            "scope_digest": scope_digest,
        })

    def status(self, *, job_id: str = "", project_key: str = "",
               authorization_id: str = "") -> tuple:
        """`authorization_id` asks about one specific approval.

        Without it the broker answers with the project's most recent job,
        which is the wrong answer to "what happened to the instruction I
        just gave" when that project has older jobs.
        """
        return self._call("/handoff/status",
                          {"job_id": job_id, "project_key": project_key,
                           "authorization_id": authorization_id})
