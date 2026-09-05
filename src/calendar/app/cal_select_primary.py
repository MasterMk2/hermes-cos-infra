#!/usr/bin/env python3
"""Set the explicit Calendar allowlist to the one Google primary calendar.

This is an operator action, never an automatic service-start behaviour.  It
exists so the user can explicitly choose "Primary only" without copying an
email-shaped calendar ID into a terminal transcript or chat.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from cal_google import GoogleCalendarClient, OAuthCredentials


def _primary_id(credential_dir: str) -> str:
    client = GoogleCalendarClient(OAuthCredentials.from_directory(credential_dir))
    token = None
    found: list[str] = []
    while True:
        page = client.list_calendars(**({"pageToken": token} if token else {}))
        found.extend(str(item["id"]) for item in page.get("items") or [] if item.get("primary") and item.get("id"))
        token = page.get("nextPageToken")
        if not token:
            break
    if len(found) != 1:
        raise RuntimeError(f"expected exactly one primary calendar, found {len(found)}")
    return found[0]


def select_primary(config_path: Path, credential_dir: str) -> int:
    if os.geteuid() != 0:
        print("calendar selection must run as root", file=sys.stderr)
        return 2
    calendar_id = _primary_id(credential_dir)
    original = config_path.read_text(encoding="utf-8")
    replacement = "  selected_ids:\n    - " + json.dumps(calendar_id) + "\n"
    updated, count = re.subn(
        r"(?m)^  selected_ids:\s*\[\]\s*$",
        replacement.rstrip("\n"),
        original,
        count=1,
    )
    if count != 1:
        print("selection refused: config must retain the default empty selected_ids list", file=sys.stderr)
        return 2
    fd, temporary = tempfile.mkstemp(prefix=".calendar-config-", dir=str(config_path.parent))
    try:
        os.fchmod(fd, 0o640)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        os.chown(temporary, 0, os.stat(config_path).st_gid)
        os.replace(temporary, config_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print("Calendar allowlist set to the one primary calendar; no ID is printed.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/srv/hermes/calendar/config/config.yaml")
    parser.add_argument("--credential-dir", default="/etc/hermes-secrets/google-calendar")
    args = parser.parse_args(argv)
    return select_primary(Path(args.config), args.credential_dir)


if __name__ == "__main__":
    raise SystemExit(main())
