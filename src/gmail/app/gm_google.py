#!/usr/bin/env python3
"""Official OAuth 2.0 transport for the read-only Gmail API.

Refresh and access tokens remain private process values.  Credential files are
read only from the systemd credential directory and are deliberately excluded
from configuration, logs, SQLite, and MCP.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path


TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
API_ROOT = "https://gmail.googleapis.com/gmail/v1"
SCOPES = ("https://www.googleapis.com/auth/gmail.readonly",)


class CredentialUnavailable(RuntimeError):
    pass


class GmailApiError(RuntimeError):
    def __init__(self, status: int, code: str = "api_error"):
        super().__init__(f"Gmail API {int(status)}: {str(code)}")
        self.status = int(status)
        self.code = str(code)


def _read_secret(directory: str | Path, name: str, *, required: bool = True) -> str | None:
    try:
        value = (Path(directory) / name).read_text(encoding="utf-8").strip()
    except OSError as exc:
        if required:
            raise CredentialUnavailable(f"missing credential {name}") from exc
        return None
    if required and (not value or value.startswith("REPLACE_THIS_")):
        raise CredentialUnavailable(f"empty credential {name}")
    if value.startswith("REPLACE_THIS_"):
        return None
    return value or None


@dataclass(frozen=True)
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


def _form_request(url: str, data: dict, *, timeout: int) -> dict:
    payload = urllib.parse.urlencode(data).encode("utf-8")
    request = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
    )
    return _request_json(request, timeout=timeout)


def _safe_google_error_code(status: int, message: str) -> str:
    """Classify only known operational errors; never persist provider prose."""
    lowered = str(message or "").lower()
    if "metadata" in lowered and "format" in lowered:
        return "metadata_scope_full_format_denied"
    if "insufficient authentication scopes" in lowered:
        return "insufficient_oauth_scope"
    if "access not configured" in lowered or "api has not been used" in lowered:
        return "api_not_enabled"
    return "http_error"


def _request_json(request: urllib.request.Request, *, timeout: int, limit: int = 8 * 1024 * 1024) -> dict:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read(8192).decode("utf-8", "replace"))
            error = body.get("error") or {}
            if isinstance(error, dict):
                code = _safe_google_error_code(exc.code, str(error.get("message") or ""))
                # Preserve a structured provider status only when it does not
                # replace a more specific non-sensitive classification.
                if code == "http_error" and error.get("status"):
                    code = str(error["status"])
            else:
                code = "http_error"
        except Exception:
            code = "http_error"
        raise GmailApiError(exc.code, code) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GmailApiError(503, type(exc).__name__) from exc
    if len(raw) > limit:
        raise GmailApiError(502, "response_too_large")
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, ValueError) as exc:
        raise GmailApiError(502, "invalid_json") from exc
    if not isinstance(data, dict):
        raise GmailApiError(502, "invalid_json_shape")
    return data


class GmailClient:
    def __init__(self, credentials: OAuthCredentials, *, timeout: int = 25):
        self.credentials = credentials
        self.timeout = int(timeout)
        self._access_token: str | None = None
        self._expires_at = 0.0

    def _refresh(self) -> None:
        data = {
            "grant_type": "refresh_token",
            "client_id": self.credentials.client_id,
            "refresh_token": self.credentials.refresh_token,
        }
        if self.credentials.client_secret:
            data["client_secret"] = self.credentials.client_secret
        result = _form_request(TOKEN_ENDPOINT, data, timeout=self.timeout)
        token = str(result.get("access_token") or "")
        if not token:
            raise GmailApiError(401, str(result.get("error") or "token_refresh_failed"))
        self._access_token = token
        self._expires_at = time.monotonic() + max(30, int(result.get("expires_in") or 300) - 30)

    def _bearer(self) -> str:
        if not self._access_token or time.monotonic() >= self._expires_at:
            self._refresh()
        return str(self._access_token)

    def _api(self, method: str, path: str, *, operation: str, params: dict | None = None,
             payload: dict | None = None, retried: bool = False) -> dict:
        query = urllib.parse.urlencode(
            [(key, str(item)) for key, value in (params or {}).items()
             if value is not None for item in (value if isinstance(value, list) else [value])],
            doseq=True,
        )
        url = f"{API_ROOT}{path}" + (f"?{query}" if query else "")
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            url, data=body, method=method,
            headers={
                "Authorization": f"Bearer {self._bearer()}",
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if body is not None else {}),
            },
        )
        try:
            return _request_json(request, timeout=self.timeout)
        except GmailApiError as exc:
            if exc.status == 401 and not retried:
                self._access_token = None
                return self._api(method, path, operation=operation, params=params, payload=payload, retried=True)
            # Keep progress observable without logging a message ID, URL,
            # header, body, or any OAuth material.
            raise GmailApiError(exc.status, f"{operation}:{exc.code}") from exc

    def get_profile(self) -> dict:
        return self._api("GET", "/users/me/profile", operation="get_profile")

    def list_messages(self, **params) -> dict:
        return self._api("GET", "/users/me/messages", operation="list_messages", params=params)

    def get_message(self, message_id: str, *, format: str = "full") -> dict:
        if format not in {"full", "metadata", "minimal"}:
            raise ValueError("unsupported Gmail message format")
        return self._api(
            "GET", f"/users/me/messages/{urllib.parse.quote(str(message_id), safe='')}",
            operation="get_message", params={"format": format},
        )

    def list_history(self, start_history_id: str, **params) -> dict:
        return self._api(
            "GET", "/users/me/history",
            operation="list_history", params={"startHistoryId": str(start_history_id), **params},
        )

    def watch(self, topic_name: str) -> dict:
        return self._api("POST", "/users/me/watch", operation="watch", payload={"topicName": str(topic_name)})


def authorization_url(client_id: str, redirect_uri: str, state: str) -> str:
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    })


def exchange_authorization_code(*, client_id: str, client_secret: str | None,
                                redirect_uri: str, code: str) -> dict:
    data = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code": code,
    }
    if client_secret:
        data["client_secret"] = client_secret
    return _form_request(TOKEN_ENDPOINT, data, timeout=25)
