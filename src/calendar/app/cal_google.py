#!/usr/bin/env python3
"""Official Google OAuth 2.0 and Calendar v3 transport, using stdlib HTTP.

Access tokens remain process-memory values.  The refresh token, client id and
optional client secret are read only from systemd's per-unit credential
directory and are never put in environment variables, logs, or SQLite.
"""

from __future__ import annotations

import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from cal_sync import CalendarApiError


TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
AUTHORIZE_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
API_ROOT = "https://www.googleapis.com/calendar/v3"
SCOPES = (
    "https://www.googleapis.com/auth/calendar.events.readonly",
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
)


class CredentialUnavailable(RuntimeError):
    pass


def _read_secret(directory: str | Path, name: str, *, required: bool = True) -> str | None:
    try:
        value = (Path(directory) / name).read_text(encoding="utf-8").strip()
    except OSError as exc:
        if required:
            raise CredentialUnavailable(f"missing credential {name}") from exc
        return None
    if required and (not value or value.startswith("REPLACE_THIS_")):
        raise CredentialUnavailable(f"empty credential {name}")
    if value and value.startswith("REPLACE_THIS_"):
        return None
    return value or None


@dataclass
class OAuthCredentials:
    client_id: str
    refresh_token: str
    client_secret: str | None = None

    @classmethod
    def from_directory(cls, directory: str | Path) -> "OAuthCredentials":
        return cls(
            client_id=str(_read_secret(directory, "client-id")),
            refresh_token=str(_read_secret(directory, "refresh-token")),
            client_secret=_read_secret(directory, "client-secret", required=False),
        )


def _json_request(url: str, *, data: dict | None = None, headers: dict | None = None,
                  timeout: int = 20) -> dict:
    payload = None
    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    if data is not None:
        payload = urllib.parse.urlencode(data).encode("utf-8")
        request_headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=payload, headers=request_headers, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(2 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read(8192).decode("utf-8", "replace"))
            code = str(body.get("error") or body.get("error_description") or "http_error")
        except Exception:
            code = "http_error"
        raise CalendarApiError(exc.code, code) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CalendarApiError(503, type(exc).__name__) from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CalendarApiError(502, "invalid_json") from exc
    if not isinstance(value, dict):
        raise CalendarApiError(502, "invalid_json_shape")
    return value


class GoogleCalendarClient:
    def __init__(self, credentials: OAuthCredentials, *, timeout: int = 20):
        self.credentials = credentials
        self.timeout = int(timeout)
        self._access_token = None
        self._expires_at = 0.0

    def _refresh(self) -> None:
        data = {
            "grant_type": "refresh_token",
            "client_id": self.credentials.client_id,
            "refresh_token": self.credentials.refresh_token,
        }
        if self.credentials.client_secret:
            data["client_secret"] = self.credentials.client_secret
        result = _json_request(TOKEN_ENDPOINT, data=data, timeout=self.timeout)
        token = result.get("access_token")
        if not token:
            raise CalendarApiError(401, str(result.get("error") or "token_refresh_failed"))
        self._access_token = str(token)
        self._expires_at = time.monotonic() + max(30, int(result.get("expires_in") or 300) - 30)

    def _bearer(self) -> str:
        if not self._access_token or time.monotonic() >= self._expires_at:
            self._refresh()
        return str(self._access_token)

    def _api(self, method: str, path: str, *, params: dict | None = None,
             payload: dict | None = None, retried: bool = False) -> dict:
        query = urllib.parse.urlencode({k: str(v).lower() if isinstance(v, bool) else str(v)
                                        for k, v in (params or {}).items() if v is not None})
        url = f"{API_ROOT}{path}" + (f"?{query}" if query else "")
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            url, data=body, method=method,
            headers={"Authorization": f"Bearer {self._bearer()}", "Accept": "application/json",
                     **({"Content-Type": "application/json"} if body is not None else {})},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read(2 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            try:
                value = json.loads(exc.read(8192).decode("utf-8", "replace"))
                err = value.get("error") or {}
                code = str(err.get("status") or err.get("message") or "http_error") if isinstance(err, dict) else str(err)
            except Exception:
                code = "http_error"
            if exc.code == 401 and not retried:
                self._access_token = None
                return self._api(method, path, params=params, payload=payload, retried=True)
            raise CalendarApiError(exc.code, code) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CalendarApiError(503, type(exc).__name__) from exc
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise CalendarApiError(502, "invalid_json") from exc
        return data if isinstance(data, dict) else {}

    def list_calendars(self, **params) -> dict:
        return self._api("GET", "/users/me/calendarList", params=params)

    def list_events(self, calendar_id: str, **params) -> dict:
        return self._api("GET", f"/calendars/{urllib.parse.quote(calendar_id, safe='')}/events", params=params)

    def watch_events(self, calendar_id: str, *, channel_id: str, channel_token: str,
                     callback_url: str, ttl_seconds: int) -> dict:
        return self._api("POST", f"/calendars/{urllib.parse.quote(calendar_id, safe='')}/events/watch", payload={
            "id": channel_id, "type": "web_hook", "address": callback_url,
            "token": channel_token, "params": {"ttl": str(ttl_seconds)},
        })

    def stop_channel(self, *, channel_id: str, resource_id: str) -> None:
        self._api("POST", "/channels/stop", payload={"id": channel_id, "resourceId": resource_id})


def authorization_url(client_id: str, redirect_uri: str, state: str) -> str:
    return AUTHORIZE_ENDPOINT + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent", "state": state,
    })


def exchange_authorization_code(*, client_id: str, client_secret: str | None,
                                redirect_uri: str, code: str) -> dict:
    data = {"grant_type": "authorization_code", "client_id": client_id,
            "redirect_uri": redirect_uri, "code": code}
    if client_secret:
        data["client_secret"] = client_secret
    return _json_request(TOKEN_ENDPOINT, data=data)


def random_oauth_state() -> str:
    return secrets.token_urlsafe(32)
