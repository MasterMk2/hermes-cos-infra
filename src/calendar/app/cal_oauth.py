#!/usr/bin/env python3
"""One-time loopback OAuth helper for a Google *Desktop* OAuth client.

It listens only on 127.0.0.1.  With an SSH local-port forward, the browser
redirect reaches this helper without creating a second public endpoint.  It
prints an authorization URL but never prints authorization, refresh, or access
tokens, and writes the refresh token directly into a root-only file.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from cal_google import (
    CredentialUnavailable,
    GoogleCalendarClient,
    OAuthCredentials,
    _read_secret,
    authorization_url,
    exchange_authorization_code,
    random_oauth_state,
)


def _write_root_secret(path: Path, value: str) -> None:
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    fd, tmp_name = tempfile.mkstemp(prefix=".refresh-", dir=str(directory))
    try:
        os.fchmod(fd, 0o400)
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            fp.write(value.strip() + "\n")
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp_name, path)
        os.chmod(path, 0o400)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def authorize(credential_dir: Path, host: str, port: int, timeout_seconds: int) -> int:
    if os.geteuid() != 0:
        print("OAuth helper must run as root so the refresh token can be written root-only", file=sys.stderr)
        return 2
    try:
        client_id = str(_read_secret(credential_dir, "client-id"))
        client_secret = _read_secret(credential_dir, "client-secret", required=False)
    except CredentialUnavailable as exc:
        print(f"OAuth setup blocked: {exc}", file=sys.stderr)
        return 2
    state = random_oauth_state()
    redirect_uri = f"http://{host}:{port}/oauth2/callback"
    received = {}
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
                self.wfile.write("Authorization received. You may close this tab.".encode("utf-8"))
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
        if not refresh:
            print("Google did not return a refresh token; no token was written", file=sys.stderr)
            return 1
        _write_root_secret(credential_dir / "refresh-token", refresh)
    except Exception as exc:
        print(f"OAuth exchange failed: {type(exc).__name__}; no token was written", file=sys.stderr)
        return 1
    print("OAuth authorization completed; refresh token stored in the root-managed credential file.")
    # Discovery output is the deliberate calendar-selection gate.  It contains
    # identifiers/labels only, not event bodies or any credential value.
    try:
        client = GoogleCalendarClient(OAuthCredentials(client_id, refresh, client_secret))
        page_token = None
        calendars = []
        while True:
            page = client.list_calendars(**({"pageToken": page_token} if page_token else {}))
            calendars.extend({
                "id": item.get("id"), "summary": item.get("summaryOverride") or item.get("summary"),
                "primary": bool(item.get("primary")), "access_role": item.get("accessRole"),
            } for item in page.get("items") or [])
            page_token = page.get("nextPageToken")
            if not page_token:
                break
        print("Calendar discovery (no calendar is selected automatically):")
        for item in calendars:
            print(f"- id={item['id']} primary={item['primary']} access_role={item['access_role']} summary={item['summary']}")
    except Exception as exc:
        print(f"Authorization succeeded but discovery deferred: {type(exc).__name__}", file=sys.stderr)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credential-dir", default="/etc/hermes-secrets/google-calendar")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    args = parser.parse_args(argv)
    if args.host != "127.0.0.1" or not (1 <= args.port <= 65535):
        parser.error("OAuth callback may bind only to 127.0.0.1 with a valid port")
    return authorize(Path(args.credential_dir), args.host, args.port, args.timeout_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
