#!/usr/bin/env python3
"""One-time loopback OAuth helper for a Gmail Desktop OAuth client.

The callback binds only to localhost.  It writes a refresh token directly to
the root-managed credential file and never prints authorization, access, or
refresh tokens.  It does not create a public endpoint.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from gm_google import (
    CredentialUnavailable,
    GmailClient,
    OAuthCredentials,
    SCOPES,
    _read_secret,
    authorization_url,
    exchange_authorization_code,
)


def _scope_is_acceptable(tokens: dict) -> bool:
    """Reject a positively reported reduced grant; absent optional field is neutral."""
    returned = str(tokens.get("scope") or "").strip()
    return not returned or set(SCOPES).issubset(set(returned.split()))


def _write_root_secret(path: Path, value: str) -> None:
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=".refresh-", dir=str(directory))
    try:
        os.fchmod(fd, 0o400)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value.strip() + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o400)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _validate_readonly_grant(credentials: OAuthCredentials) -> None:
    """Prove the newly issued refresh grant can read message content.

    The check is deliberately in-memory: it stores, prints, and logs neither
    token material nor message metadata/content.  A list item is used only as
    an opaque handle for a single ``format=full`` authorization check.
    """
    client = GmailClient(credentials)
    profile = client.get_profile()
    if not str(profile.get("historyId") or ""):
        raise RuntimeError("profile_missing_history_id")
    listing = client.list_messages(maxResults=1, includeSpamTrash=False)
    items = listing.get("messages") if isinstance(listing, dict) else None
    if not isinstance(items, list):
        raise RuntimeError("invalid_message_list")
    if items:
        message_id = str((items[0] or {}).get("id") or "")
        if not message_id:
            raise RuntimeError("invalid_message_list")
        client.get_message(message_id, format="full")


def authorize(credential_dir: Path, host: str, port: int, timeout_seconds: int) -> int:
    if os.geteuid() != 0:
        print("OAuth helper must run as root so the refresh token remains root-managed", file=sys.stderr)
        return 2
    try:
        client_id = str(_read_secret(credential_dir, "client-id"))
        client_secret = _read_secret(credential_dir, "client-secret", required=False)
    except CredentialUnavailable as exc:
        print(f"OAuth setup blocked: {exc}", file=sys.stderr)
        return 2
    state = secrets.token_urlsafe(32)
    redirect_uri = f"http://{host}:{port}/oauth2/callback"
    received: dict[str, str] = {}
    done = threading.Event()

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_GET(self):
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            valid = parsed.path == "/oauth2/callback" and query.get("state", [""])[0] == state
            if valid and query.get("code"):
                received["code"] = query["code"][0]
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Authorization received. You may close this tab.")
            else:
                self.send_response(400)
                self.end_headers()
            done.set()

    server = HTTPServer((host, port), Callback)
    worker = threading.Thread(target=server.handle_request, daemon=True)
    worker.start()
    print("Open the following URL in a browser through the documented SSH port forward:")
    print(authorization_url(client_id, redirect_uri, state))
    if not done.wait(timeout_seconds):
        server.server_close()
        print("OAuth timed out; no token was written", file=sys.stderr)
        return 1
    server.server_close()
    code = received.get("code")
    if not code:
        print("OAuth callback was rejected; no token was written", file=sys.stderr)
        return 1
    try:
        tokens = exchange_authorization_code(
            client_id=client_id, client_secret=client_secret, redirect_uri=redirect_uri, code=code
        )
        refresh = str(tokens.get("refresh_token") or "")
        if not refresh or not _scope_is_acceptable(tokens):
            print("Google did not return a refresh token; no token was written", file=sys.stderr)
            return 1
        _validate_readonly_grant(OAuthCredentials(client_id, refresh, client_secret))
        _write_root_secret(credential_dir / "refresh-token", refresh)
    except Exception as exc:
        print(f"OAuth exchange failed: {type(exc).__name__}; no token was written", file=sys.stderr)
        return 1
    print("Gmail OAuth authorization completed; the refresh token is in root-managed credential storage.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credential-dir", default="/etc/hermes-secrets/google-gmail")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    args = parser.parse_args(argv)
    if args.host != "127.0.0.1" or not (1 <= args.port <= 65535):
        parser.error("OAuth callback may bind only to 127.0.0.1 with a valid port")
    return authorize(Path(args.credential_dir), args.host, args.port, args.timeout_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
