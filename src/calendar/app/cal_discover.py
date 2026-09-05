#!/usr/bin/env python3
"""List Calendar metadata for the explicit allowlist decision.

This helper is deliberately root-only because it consumes the same
root-managed systemd credentials as the Calendar service.  It prints only
calendar ID, display name, primary flag, and access role; it never fetches
events and never prints OAuth credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from cal_google import GoogleCalendarClient, OAuthCredentials


def discover(credential_dir: str) -> int:
    if os.geteuid() != 0:
        print("calendar discovery must run as root", file=sys.stderr)
        return 2
    client = GoogleCalendarClient(OAuthCredentials.from_directory(credential_dir))
    page_token = None
    rows = []
    while True:
        page = client.list_calendars(**({"pageToken": page_token} if page_token else {}))
        rows.extend(
            {
                "id": item.get("id"),
                "summary": item.get("summaryOverride") or item.get("summary") or "(unnamed)",
                "primary": bool(item.get("primary")),
                "access_role": item.get("accessRole"),
            }
            for item in page.get("items") or []
        )
        page_token = page.get("nextPageToken")
        if not page_token:
            break
    print(json.dumps(rows, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credential-dir", default="/etc/hermes-secrets/google-calendar")
    args = parser.parse_args(argv)
    return discover(args.credential_dir)


if __name__ == "__main__":
    raise SystemExit(main())
