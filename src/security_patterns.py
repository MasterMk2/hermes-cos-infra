#!/usr/bin/env python3
"""Canonical deterministic secret-scan patterns (single source of truth).

Phase F.1.1-Pi.2 hardening (Item 30): supervisor.py, bridge/bridge.py and
worker/workerctl_server.py previously carried three drifting copies of the
same pattern list (the bridge additionally matched authorization headers the
other two silently ignored). This module is the only definition.

Deployment note: this file must be copied next to each consumer:
  - jobs/  -> /opt/data/ (imported via parent dir on sys.path)
  - bridge -> <worker-root>/ (imported via parent dir on sys.path)
  - worker -> the directory holding the `workerctl` entry point

Rules:
  - patterns match STRUCTURE only; never store or log matched values
  - scans report pattern ids only (no content leaves the scanning boundary)
  - any change here must keep the three consumers' visible behaviour
    identical (enforced by tests/test_security_patterns.py)
"""

import re

SECRET_PATTERNS = [
    ("private_key_header", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_pat", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("openai_like_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("authorization_header", re.compile(r"(?i)\bauthorization\b\s*[:=]\s*\S+")),
    ("credential_assignment", re.compile(
        r"(?i)\b(api[_-]?key|secret|passwd|password|token)\b\s*[=:]\s*"
        r"[\"']?[A-Za-z0-9_\-+/]{20,}")),
]


def scan_text(text, max_bytes=200_000):
    """Return the set of pattern ids matching `text` (ids only, never values)."""
    hits = set()
    if not text:
        return hits
    for pid, rx in SECRET_PATTERNS:
        if rx.search(text[:max_bytes]):
            hits.add(pid)
    return hits


def is_env_file(name):
    """True for any .env-style file (top-level or nested, any case)."""
    n = (name or "").lower()
    return n.endswith(".env") or n == ".env"
