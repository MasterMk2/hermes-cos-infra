#!/usr/bin/env python3
"""Configure only the non-secret Gmail Pub/Sub resource names."""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path

import yaml


RESOURCE = re.compile(r"^projects/(?P<project>[^/]+)/(?P<kind>topics|subscriptions)/(?P<name>[^/]+)$")


def _resource(value: str, kind: str) -> tuple[str, str]:
    match = RESOURCE.fullmatch(value.strip())
    if not match or match.group("kind") != kind:
        raise ValueError(f"invalid Pub/Sub {kind} resource name")
    return match.group("project"), match.group("name")


def _atomic_write(path: Path, value: str) -> None:
    original = path.stat()
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.h2-", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, original.st_mode & 0o777)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.chown(temporary, original.st_uid, original.st_gid)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def configure(path: Path, topic: str, subscription: str) -> None:
    topic_project, _ = _resource(topic, "topics")
    subscription_project, _ = _resource(subscription, "subscriptions")
    if topic_project != subscription_project:
        raise ValueError("topic and subscription must be in the same project")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("Gmail configuration must be a mapping")
    pubsub = raw.setdefault("pubsub", {})
    if not isinstance(pubsub, dict):
        raise ValueError("Gmail pubsub configuration must be a mapping")
    pubsub["topic_name"] = topic
    pubsub["subscription_name"] = subscription
    _atomic_write(path, yaml.safe_dump(raw, allow_unicode=True, sort_keys=False))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--topic", required=True)
    parser.add_argument("--subscription", required=True)
    parser.add_argument("--config", default="/srv/hermes/gmail/config/config.yaml")
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        print("Pub/Sub configuration must run as root", file=sys.stderr)
        return 2
    try:
        configure(Path(args.config), args.topic, args.subscription)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"Pub/Sub configuration refused: {type(exc).__name__}", file=sys.stderr)
        return 2
    print("Gmail Pub/Sub resource names configured; no credential value was read or displayed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
