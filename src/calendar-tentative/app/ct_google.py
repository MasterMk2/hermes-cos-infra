#!/usr/bin/env python3
"""Calendar Tentative Registration — Google OAuth 2.0 and Calendar v3, stdlib.

The single OAuth scope is `calendar.events.owned`: "See, create, change, and
delete events on Google calendars you own." It reaches events only — not
calendar settings, sharing, or calendars the owner merely subscribes to —
which is the narrowest scope that can write the owner's primary calendar.
Which events this service touches is decided by the engine: only ids it
derived itself (`hermesct…`), each write guarded by If-Match. H.1's
read-only token and this one are separate refresh tokens in separate
credential directories, so neither service can borrow the other's authority.

Access tokens stay in process memory. The refresh token, client id and the
optional client secret are read from systemd's per-unit credential directory
and never logged, stored in SQLite, or placed in the environment.

Errors carry the one fact the engine needs: whether the request could have
changed anything at Google.

    sent=False   the request never left (token refresh failed, DNS failed,
                 connection refused). Nothing changed; retrying is safe.
    status set   Google answered. For a mutation, an error answer means the
                 change was not made — this client performs no hidden
                 internal retry that could have succeeded first. 5xx is the
                 exception and is treated as unknown by the engine.
    status None  the request may have been sent and the answer was lost.
                 The engine holds the row and asks Google later.
"""

from __future__ import annotations

import json
import secrets
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
AUTHORIZE_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
API_ROOT = "https://www.googleapis.com/calendar/v3"
SCOPES = ("https://www.googleapis.com/auth/calendar.events.owned",)

MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class CredentialUnavailable(RuntimeError):
    pass


class ApiError(Exception):
    """A Calendar API failure, classified for the engine."""

    def __init__(self, status, code: str, *, sent: bool = True):
        super().__init__(f"{status}:{code}")
        self.status = status
        self.code = str(code or "")
        self.sent = bool(sent)

    @property
    def not_applied(self) -> bool:
        """True when this failure proves a mutation did not take effect."""
        if not self.sent:
            return True
        return self.status is not None and 400 <= int(self.status) < 500

    @property
    def unknown(self) -> bool:
        return not self.not_applied


def read_secret(directory, name: str, *, required: bool = True):
    try:
        value = (Path(directory) / name).read_text(encoding="utf-8").strip()
    except OSError as exc:
        if required:
            raise CredentialUnavailable(f"missing credential {name}") from exc
        return None
    if value.startswith("REPLACE_THIS_"):
        value = ""
    if required and not value:
        raise CredentialUnavailable(f"empty credential {name}")
    return value or None


@dataclass
class OAuthCredentials:
    client_id: str
    refresh_token: str
    client_secret: str | None = None

    def __repr__(self):
        return "OAuthCredentials(<redacted>)"

    @classmethod
    def from_directory(cls, directory) -> "OAuthCredentials":
        if not str(directory or "").strip():
            raise CredentialUnavailable("no credential directory (CREDENTIALS_DIRECTORY unset)")
        return cls(
            client_id=str(read_secret(directory, "client-id")),
            refresh_token=str(read_secret(directory, "refresh-token")),
            client_secret=read_secret(directory, "client-secret", required=False),
        )


def _never_sent(exc: BaseException) -> bool:
    """Connection-phase failures: no byte of the request reached Google."""
    reason = getattr(exc, "reason", exc)
    return isinstance(reason, (socket.gaierror, ConnectionRefusedError))


def _error_code(raw: bytes) -> str:
    try:
        value = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return "http_error"
    err = value.get("error") if isinstance(value, dict) else None
    if isinstance(err, dict):
        reasons = err.get("errors")
        if isinstance(reasons, list) and reasons and isinstance(reasons[0], dict):
            if reasons[0].get("reason"):
                return str(reasons[0]["reason"])[:80]
        return str(err.get("status") or "http_error")[:80]
    if isinstance(err, str):
        return err[:80]
    return "http_error"


def _token_request(data: dict, timeout: int) -> dict:
    payload = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_ENDPOINT, data=payload, method="POST",
        headers={"Accept": "application/json",
                 "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(65536)
    except urllib.error.HTTPError as exc:
        raise ApiError(exc.code, _error_code(exc.read(8192)), sent=False) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ApiError(None, type(exc).__name__, sent=False) from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise ApiError(502, "invalid_json", sent=False) from exc
    if not isinstance(value, dict):
        raise ApiError(502, "invalid_json_shape", sent=False)
    return value


class TentativeCalendarClient:
    """The five verbs this service needs, and no others."""

    def __init__(self, credentials: OAuthCredentials, *, timeout: int = 20):
        self.credentials = credentials
        self.timeout = int(timeout)
        self._access_token = None
        self._expires_at = 0.0

    def __repr__(self):
        return "TentativeCalendarClient(<credentials redacted>)"

    # -- auth ----------------------------------------------------------

    def _refresh(self) -> None:
        data = {
            "grant_type": "refresh_token",
            "client_id": self.credentials.client_id,
            "refresh_token": self.credentials.refresh_token,
        }
        if self.credentials.client_secret:
            data["client_secret"] = self.credentials.client_secret
        result = _token_request(data, self.timeout)
        token = result.get("access_token")
        if not token:
            raise ApiError(401, str(result.get("error") or "token_refresh_failed"), sent=False)
        self._access_token = str(token)
        self._expires_at = time.monotonic() + max(30, int(result.get("expires_in") or 300) - 30)

    def _bearer(self) -> str:
        if not self._access_token or time.monotonic() >= self._expires_at:
            self._refresh()
        return str(self._access_token)

    def authenticate(self) -> None:
        """Obtain an access token now, so an OAuth outage stops a run before
        it touches any entry rather than being charged to one."""
        self._bearer()

    # -- transport -----------------------------------------------------

    def _api(self, method: str, path: str, *, params=None, payload=None,
             headers=None, retried: bool = False) -> dict:
        bearer = self._bearer()  # raises sent=False: nothing has gone out yet
        query = urllib.parse.urlencode({
            k: (str(v).lower() if isinstance(v, bool) else str(v))
            for k, v in (params or {}).items() if v is not None
        })
        url = f"{API_ROOT}{path}" + (f"?{query}" if query else "")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req_headers = {"Authorization": f"Bearer {bearer}", "Accept": "application/json"}
        if body is not None:
            req_headers["Content-Type"] = "application/json; charset=utf-8"
        req_headers.update(headers or {})
        req = urllib.request.Request(url, data=body, method=method, headers=req_headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read(MAX_RESPONSE_BYTES)
        except urllib.error.HTTPError as exc:
            code = _error_code(exc.read(8192))
            if exc.code == 401 and not retried:
                # An expired access token. Google rejected the request, so
                # nothing was applied; refresh once and try again.
                self._access_token = None
                return self._api(method, path, params=params, payload=payload,
                                 headers=headers, retried=True)
            raise ApiError(exc.code, code) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ApiError(None, type(exc).__name__, sent=not _never_sent(exc)) from exc
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            # The request was answered with 2xx; for a mutation that means it
            # was applied even though we cannot read the result.
            raise ApiError(None, "invalid_json") from exc
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _cal(calendar_id: str) -> str:
        return urllib.parse.quote(calendar_id, safe="")

    # -- verbs -----------------------------------------------------------

    def insert_event(self, calendar_id: str, body: dict) -> dict:
        return self._api("POST", f"/calendars/{self._cal(calendar_id)}/events",
                         params={"sendUpdates": "none"}, payload=body)

    def get_event(self, calendar_id: str, event_id: str):
        """The event resource, or None when Google has no such event."""
        try:
            return self._api(
                "GET", f"/calendars/{self._cal(calendar_id)}/events/{self._cal(event_id)}")
        except ApiError as exc:
            if exc.status in (404, 410):
                return None
            raise

    def patch_event(self, calendar_id: str, event_id: str, body: dict, *, etag: str) -> dict:
        # If-Match makes the owner's own edit win: Google answers 412 instead
        # of overwriting a change we did not make.
        return self._api(
            "PATCH", f"/calendars/{self._cal(calendar_id)}/events/{self._cal(event_id)}",
            params={"sendUpdates": "none"}, payload=body, headers={"If-Match": etag})

    def delete_event(self, calendar_id: str, event_id: str, *, etag: str) -> None:
        self._api(
            "DELETE", f"/calendars/{self._cal(calendar_id)}/events/{self._cal(event_id)}",
            params={"sendUpdates": "none"}, headers={"If-Match": etag})

    def list_events(self, calendar_id: str, *, private_property: str) -> list:
        """The events carrying `private_property` (`key=value`), deleted ones included.

        The target is the owner's primary calendar, years of it; only the
        entries this service marked are fetched. Google may return a deleted
        entry without its properties, so it is simply absent here — which the
        engine already reads as "the owner deleted it".
        """
        items: list = []
        page_token = None
        for _ in range(50):
            page = self._api("GET", f"/calendars/{self._cal(calendar_id)}/events", params={
                "showDeleted": True, "maxResults": 2500, "pageToken": page_token,
                "privateExtendedProperty": private_property,
            })
            items.extend(i for i in page.get("items") or [] if isinstance(i, dict))
            page_token = page.get("nextPageToken")
            if not page_token:
                return items
        raise ApiError(None, "too_many_pages", sent=False)


def authorization_url(client_id: str, redirect_uri: str, state: str) -> str:
    return AUTHORIZE_ENDPOINT + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent",
        "state": state,
    })


def exchange_authorization_code(*, client_id: str, client_secret, redirect_uri: str,
                                code: str, timeout: int = 20) -> dict:
    data = {"grant_type": "authorization_code", "client_id": client_id,
            "redirect_uri": redirect_uri, "code": code}
    if client_secret:
        data["client_secret"] = client_secret
    return _token_request(data, timeout)


def random_oauth_state() -> str:
    return secrets.token_urlsafe(32)
