#!/usr/bin/env python3
"""Minimal Cloud Pub/Sub pull client.

The service account credential is a distinct root-managed systemd credential.
The Gmail OAuth refresh token is never used for Pub/Sub subscription access.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from pathlib import Path


class PubSubUnavailable(RuntimeError):
    pass


class PubSubError(RuntimeError):
    def __init__(self, status: int, code: str):
        super().__init__(f"Pub/Sub {int(status)}: {str(code)}")
        self.status = int(status)
        self.code = str(code)


class PubSubPullClient:
    """REST pull/ack client with a subscription-only service identity."""

    def __init__(self, service_account_file: str | Path, subscription: str, *, timeout: int = 20):
        self.service_account_file = str(service_account_file)
        self.subscription = str(subscription)
        self.timeout = int(timeout)
        self._credentials = None

    def _token(self) -> str:
        try:
            from google.oauth2 import service_account
            from google.auth.transport.requests import Request
        except ImportError as exc:
            raise PubSubUnavailable("google_auth_unavailable") from exc
        if self._credentials is None:
            try:
                self._credentials = service_account.Credentials.from_service_account_file(
                    self.service_account_file,
                    scopes=["https://www.googleapis.com/auth/pubsub"],
                )
            except Exception as exc:
                raise PubSubUnavailable("pubsub_credential_unavailable") from exc
        try:
            if not self._credentials.valid or self._credentials.expired:
                self._credentials.refresh(Request())
            token = str(self._credentials.token or "")
        except Exception as exc:
            raise PubSubUnavailable("pubsub_token_refresh_failed") from exc
        if not token:
            raise PubSubUnavailable("pubsub_token_missing")
        return token

    def _post(self, method: str, payload: dict) -> dict:
        url = f"https://pubsub.googleapis.com/v1/{self.subscription}:{method}"
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            url, data=data, method="POST",
            headers={
                "Authorization": f"Bearer {self._token()}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(2 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            raise PubSubError(exc.code, "http_error") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PubSubError(503, type(exc).__name__) from exc
        try:
            value = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, ValueError) as exc:
            raise PubSubError(502, "invalid_json") from exc
        return value if isinstance(value, dict) else {}

    def pull(self, *, max_messages: int = 10) -> list[dict]:
        response = self._post("pull", {"maxMessages": max(1, min(50, int(max_messages)))})
        return [item for item in response.get("receivedMessages") or [] if isinstance(item, dict)]

    def acknowledge(self, ack_ids: list[str]) -> None:
        if ack_ids:
            self._post("acknowledge", {"ackIds": [str(item) for item in ack_ids]})


def parse_notification(received: dict) -> tuple[str, str, str] | None:
    """Return (Pub/Sub id, mailbox, history id), never the email body."""
    message = received.get("message") or {}
    message_id = str(message.get("messageId") or "")
    encoded = message.get("data")
    if not message_id or not encoded:
        return None
    try:
        raw = base64.b64decode(str(encoded) + "=" * (-len(str(encoded)) % 4), validate=False)
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, TypeError):
        return None
    email = str(value.get("emailAddress") or "")
    history_id = str(value.get("historyId") or "")
    if not email or not history_id.isdigit():
        return None
    return message_id, email, history_id
