#!/usr/bin/env python3
"""One-time authorization for tentative registration, and its calendar.

Run as root on the Pi, through an SSH local-port forward, exactly like H.1's
`cal_oauth.py` — but asking Google for one scope only:

    https://www.googleapis.com/auth/calendar.app.created

which lets this service create its own secondary calendars and manage
events on those, and nothing else. The consent screen says so.

    sudo /srv/hermes/calendar-tentative/venv/bin/python \\
        /srv/hermes/calendar-tentative/app/ct_oauth.py --create-calendar "Hermes 仮登録"

It listens only on 127.0.0.1, prints the authorization URL (never a token),
writes the refresh token to a root-only file, and — with --create-calendar —
creates the secondary calendar and prints its id for `calendar.calendar_id`.
The same Desktop OAuth client as H.1 may be reused; the refresh token is
separate, so revoking one never affects the other.
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

from ct_google import (
    ApiError,
    CredentialUnavailable,
    OAuthCredentials,
    TentativeCalendarClient,
    authorization_url,
    exchange_authorization_code,
    random_oauth_state,
    read_secret,
)

DEFAULT_CREDENTIAL_DIR = "/etc/hermes-secrets/google-calendar-tentative"
DEFAULT_PORT = 8766
CALENDAR_DESCRIPTION = (
    "Hermes が会話・メールから見つけた予定と締切の仮登録です。"
    "確定するものは自分のカレンダーへ移動し、不要なものは削除してください。"
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


def _authorize(credential_dir: Path, host: str, port: int, timeout_seconds: int) -> int:
    try:
        client_id = str(read_secret(credential_dir, "client-id"))
        client_secret = read_secret(credential_dir, "client-secret", required=False)
    except CredentialUnavailable as exc:
        print(f"OAuth setup blocked: {exc}", file=sys.stderr)
        return 2
    state = random_oauth_state()
    redirect_uri = f"http://{host}:{port}/oauth2/callback"
    received: dict = {}
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
    print("Open the following URL in a browser through the SSH port forward "
          f"(ssh -L {port}:127.0.0.1:{port} <pi>):")
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
        tokens = exchange_authorization_code(client_id=client_id, client_secret=client_secret,
                                             redirect_uri=redirect_uri, code=code)
    except ApiError as exc:
        print(f"OAuth exchange failed: {exc.status}:{exc.code}; no token was written",
              file=sys.stderr)
        return 1
    granted = set(str(tokens.get("scope") or "").split())
    if granted and granted != {"https://www.googleapis.com/auth/calendar.app.created"}:
        # A broader grant than this service needs is refused, not stored.
        print(f"Google granted unexpected scopes {sorted(granted)}; no token was written",
              file=sys.stderr)
        return 1
    refresh = str(tokens.get("refresh_token") or "")
    if not refresh:
        print("Google did not return a refresh token; no token was written", file=sys.stderr)
        return 1
    _write_root_secret(credential_dir / "refresh-token", refresh)
    print("Authorization completed; refresh token stored in the root-only credential file.")
    return 0


def _create_calendar(credential_dir: Path, name: str, time_zone: str) -> int:
    try:
        client = TentativeCalendarClient(OAuthCredentials.from_directory(credential_dir))
        created = client.create_calendar(summary=name, time_zone=time_zone,
                                         description=CALENDAR_DESCRIPTION)
    except (CredentialUnavailable, ApiError) as exc:
        print(f"Calendar creation failed: {exc}", file=sys.stderr)
        return 1
    calendar_id = str(created.get("id") or "")
    if not calendar_id:
        print("Google did not return a calendar id", file=sys.stderr)
        return 1
    print("Tentative calendar created. Put this in calendar.calendar_id:")
    print(f"  {calendar_id}")
    print("Do NOT add it to H.1's calendars.selected_ids.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credential-dir", default=DEFAULT_CREDENTIAL_DIR)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--skip-authorize", action="store_true",
                        help="reuse the stored refresh token (only create the calendar)")
    parser.add_argument("--create-calendar", metavar="NAME", default=None)
    parser.add_argument("--time-zone", default="Asia/Tokyo")
    args = parser.parse_args(argv)
    if args.host != "127.0.0.1" or not (1 <= args.port <= 65535):
        parser.error("OAuth callback may bind only to 127.0.0.1 with a valid port")
    if os.geteuid() != 0:
        print("must run as root so the refresh token is written root-only", file=sys.stderr)
        return 2
    credential_dir = Path(args.credential_dir)
    if not args.skip_authorize:
        rc = _authorize(credential_dir, args.host, args.port, args.timeout_seconds)
        if rc:
            return rc
    if args.create_calendar:
        return _create_calendar(credential_dir, args.create_calendar.strip()[:100] or "Hermes 仮登録",
                                args.time_zone)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
