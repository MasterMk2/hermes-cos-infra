#!/usr/bin/env python3
"""Install separate Gmail credentials into the root-managed H.2 store.

The input files are read once, validated, and copied with restrictive modes.
No credential value is printed, logged, written into the repository, or added
to the Gmail SQLite databases.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path


class CredentialInputError(ValueError):
    pass


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CredentialInputError("credential JSON is unreadable") from exc
    if not isinstance(value, dict):
        raise CredentialInputError("credential JSON must be an object")
    return value


def _write_secret(directory: Path, name: str, value: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{name}-", dir=str(directory))
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o400)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value.rstrip("\n") + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / name)
        os.chmod(directory / name, 0o400)
    finally:
        if temporary.exists():
            temporary.unlink()


def install(oauth_json: Path, pubsub_json: Path, *, destination: Path) -> None:
    oauth = _load_json(oauth_json)
    # The local loopback helper is exclusively for an installed/desktop client.
    installed = oauth.get("installed")
    if not isinstance(installed, dict):
        raise CredentialInputError("OAuth JSON must describe a Desktop client")
    client_id = str(installed.get("client_id") or "").strip()
    client_secret = str(installed.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        raise CredentialInputError("OAuth client JSON is incomplete")
    pubsub = _load_json(pubsub_json)
    if pubsub.get("type") != "service_account" or not str(pubsub.get("client_email") or "") or not str(pubsub.get("private_key") or ""):
        raise CredentialInputError("Pub/Sub JSON must be a service-account key")
    _write_secret(destination, "client-id", client_id)
    _write_secret(destination, "client-secret", client_secret)
    _write_secret(destination, "pubsub-service-account.json", json.dumps(pubsub, separators=(",", ":")))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--oauth-client-json", required=True)
    parser.add_argument("--pubsub-service-account-json", required=True)
    parser.add_argument("--destination", default="/etc/hermes-secrets/google-gmail")
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        print("credential installation must run as root", file=sys.stderr)
        return 2
    try:
        install(Path(args.oauth_client_json), Path(args.pubsub_service_account_json), destination=Path(args.destination))
    except CredentialInputError as exc:
        print(f"credential installation refused: {exc}", file=sys.stderr)
        return 2
    print("Gmail credentials installed in root-managed storage.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
