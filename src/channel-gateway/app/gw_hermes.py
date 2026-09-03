#!/usr/bin/env python3
"""Unified Channel Gateway — the call into Hermes.

The gateway builds every field of every request. Nothing here is derived from
a channel payload except the user's text, which travels in `input` and only
in `input` (spec sections 68, 70).

Measured contract, not assumed — each of these was observed against the live
API before this file was written:

  POST /v1/runs
    body    {"input", "instructions", "session_id", "model"}
    header  Idempotency-Key: 1-255 visible ASCII
    -> 202 {"run_id", "status", "replayed"}
       same key + same payload  -> replayed: true, same run_id
       same key + other payload -> 409 idempotency_key_conflict
  GET /v1/runs/{run_id}
    -> {"status", "output", "usage", "session_id", ...}
       status in started|running|completed|failed|cancelled|interrupted
                 |waiting_for_approval
    `output` is the answer text.

`instructions` is what makes the envelope unforgeable: it is a separate
request field from `input`, so there is no string a user can type that lands
in it. It is sent on EVERY turn — a system prompt stored once at session
creation is written but never fed into a turn, so relying on that would leave
the run with no envelope at all.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request

from gw_canonical import TrustedEnvelope, render_envelope_system_message

# Statuses that mean "stop polling".
TERMINAL_RUN_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "interrupted"}
)

# A run parked on an approval prompt will never be answered over HTTP: there
# is no caller to approve it. Treat it as a failure rather than waiting out
# the timeout. The capability profile is what prevents it arising.
STUCK_RUN_STATUSES = frozenset({"waiting_for_approval"})


class HermesUnavailable(Exception):
    """Transport-level failure: retry later, keep the message queued."""


class HermesRejected(Exception):
    """The API refused the request itself; retrying it unchanged will not help."""


class HermesResult:
    __slots__ = ("status", "text", "run_id", "usage", "replayed")

    def __init__(self, status, text, run_id, usage=None, replayed=False):
        self.status = status
        self.text = text or ""
        self.run_id = run_id
        self.usage = usage or {}
        self.replayed = replayed

    @property
    def ok(self) -> bool:
        return self.status == "completed"


def idempotency_key(gateway_request_id: str) -> str:
    """1-255 visible ASCII, and stable for a given inbound message.

    Stability is the point: a worker that crashed after starting a run and a
    worker retrying after a timeout produce the same key, so Hermes replays
    the first run instead of answering twice.
    """
    key = "".join(ch for ch in str(gateway_request_id) if 33 <= ord(ch) <= 126)
    return key[:255] or "gw_unkeyed"


def build_run_body(envelope: TrustedEnvelope, user_text: str, *,
                   session_id: str, model: str) -> dict:
    """The complete request. Every value is the gateway's own.

    Note the shape of the seam: `instructions` is rendered from the envelope
    dataclass, `input` is the raw user text, and there is no third field into
    which anything else could be merged.
    """
    return {
        "instructions": render_envelope_system_message(envelope),
        "input": str(user_text or ""),
        "session_id": session_id,
        "model": model,
    }


class HermesClient:
    """Minimal client. Blocking calls are run in a thread by the caller."""

    def __init__(self, base_url: str, api_key: str, *, model: str = "hermes-agent",
                 poll_interval: float = 2.0, absolute_timeout: float = 240.0,
                 connect_timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.model = model
        self.poll_interval = poll_interval
        self.absolute_timeout = absolute_timeout
        self.connect_timeout = connect_timeout

    # -- transport -----------------------------------------------------

    def _request(self, method: str, path: str, body=None, headers=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self._api_key}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=self.connect_timeout) as r:
                raw = r.read().decode("utf-8") or "{}"
                return r.status, json.loads(raw)
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8") or "{}")
            except Exception:
                payload = {}
            return exc.code, payload
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise HermesUnavailable(type(exc).__name__) from exc

    def health(self) -> dict:
        status, body = self._request("GET", "/health")
        if status != 200:
            raise HermesUnavailable(f"health {status}")
        return body

    # -- runs ----------------------------------------------------------

    def start_run(self, envelope: TrustedEnvelope, user_text: str, *,
                  session_id: str, request_id: str) -> tuple:
        """Returns (run_id, replayed). Raises on a refusal that a retry
        cannot fix."""
        body = build_run_body(envelope, user_text, session_id=session_id,
                              model=self.model)
        status, payload = self._request(
            "POST", "/v1/runs", body,
            {"Idempotency-Key": idempotency_key(request_id)},
        )
        if status in (200, 202):
            run_id = payload.get("run_id")
            if not run_id:
                raise HermesRejected("no run_id in response")
            return run_id, bool(payload.get("replayed"))
        if status == 409:
            # The same key was used with a different payload. That means the
            # gateway changed a request it had already submitted — a bug, not
            # a transient fault, so surface it rather than retry into it.
            raise HermesRejected("idempotency_key_conflict")
        if status in (401, 403):
            raise HermesRejected(f"auth {status}")
        if status >= 500:
            raise HermesUnavailable(f"http {status}")
        raise HermesRejected(f"http {status}")

    def get_run(self, run_id: str) -> dict:
        status, payload = self._request("GET", f"/v1/runs/{run_id}")
        if status == 200:
            return payload
        if status == 404:
            raise HermesRejected("run_not_found")
        if status >= 500:
            raise HermesUnavailable(f"http {status}")
        raise HermesRejected(f"http {status}")

    def poll(self, run_id: str, *, deadline: float | None = None) -> HermesResult:
        """Block until the run settles or the absolute deadline passes.

        A timeout leaves the run alone: it may still be running, and the
        answer can be collected on a later attempt because the run id is
        recorded against the request.
        """
        deadline = deadline or (time.monotonic() + self.absolute_timeout)
        last = {}
        while time.monotonic() < deadline:
            last = self.get_run(run_id)
            status = str(last.get("status") or "")
            if status in TERMINAL_RUN_STATUSES:
                return HermesResult(status, last.get("output"), run_id,
                                    last.get("usage"))
            if status in STUCK_RUN_STATUSES:
                return HermesResult("failed", "", run_id, last.get("usage"))
            time.sleep(self.poll_interval)
        return HermesResult("timeout", "", run_id, last.get("usage"))

    def ask(self, envelope: TrustedEnvelope, user_text: str, *, session_id: str,
            request_id: str) -> HermesResult:
        run_id, replayed = self.start_run(
            envelope, user_text, session_id=session_id, request_id=request_id
        )
        result = self.poll(run_id)
        result.replayed = replayed
        return result

    # -- async convenience ---------------------------------------------

    async def ask_async(self, envelope: TrustedEnvelope, user_text: str, *,
                        session_id: str, request_id: str) -> HermesResult:
        return await asyncio.to_thread(
            self.ask, envelope, user_text,
            session_id=session_id, request_id=request_id,
        )
