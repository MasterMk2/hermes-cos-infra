#!/usr/bin/env python3
"""Coding Handoff Broker — versioned specifications (Phase J.1).

A project's specification is a numbered file in the project's spec directory:

    0001-core-thumbnail-engine.md
    0002-metadata-index.md

The numbering is the version. The operator adds a revision by writing a new
file; nothing else changes, and no service restarts. That is what makes
"the requirements we agreed in Discord" able to become something the Worker
can be told to build: the agreed text is written up as the next revision, and
the owner then approves *that revision, by number and by hash* in the channel.

Why a hash, and not just a number
---------------------------------
The proposal the owner reads and the goal the Worker receives must be the same
text. Between those two moments an operator can edit a file, and a route that
only recorded "revision 2" would happily dispatch whatever revision 2 says
now. So the proposal binds `sha256` of the exact bytes, and the authorization
carries that hash back. If it no longer matches, the authorization is refused
and the owner is shown the new revision — a re-review, not a silent swap.

Deliberately not here (Phase J.1): authoring a revision *from* chat text. The
owner approves specifications through this route; they do not write them
through it. A route that let a chat turn create the text that becomes a
Worker goal would put conversation content — and anything that had been
retrieved into that conversation — inside the job prompt, which is exactly the
isolation this design exists to keep.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

#: `0001-some-name.md`. Four digits so the ordering is lexicographic as well
#: as numeric, and a restricted name so a revision file can never be a path.
REVISION_RE = re.compile(r"^(\d{4})-([a-z0-9][a-z0-9-]{0,63})\.md$")

MAX_SPEC_BYTES = 100_000
MAX_REVISIONS = 999

#: How much of a specification may be quoted back into a chat proposal. Enough
#: for the owner to recognise what they agreed to, far short of shipping the
#: document through the channel.
SUMMARY_CHARS = 400


class SpecError(ValueError):
    """The specification directory cannot be read unambiguously."""


@dataclass(frozen=True)
class SpecRevision:
    revision: int
    name: str
    path: str
    sha256: str
    size_bytes: int
    title: str
    summary: str

    @property
    def label(self) -> str:
        """What the owner sees: `rev.0001 (a1b2c3d4)`. Short enough to read in
        a chat message, specific enough to identify exact bytes."""
        return f"rev.{self.revision:04d} ({self.sha256[:8]})"

    def as_dict(self) -> dict:
        return {
            "revision": self.revision,
            "name": self.name,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "title": self.title,
            "summary": self.summary,
            "label": self.label,
        }


def _title_and_summary(text: str) -> tuple:
    lines = [ln.rstrip() for ln in text.splitlines()]
    title = ""
    for line in lines:
        if line.startswith("# "):
            title = line[2:].strip()
            break
    body = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        body.append(stripped)
        if sum(len(b) for b in body) >= SUMMARY_CHARS:
            break
    summary = " ".join(body)[:SUMMARY_CHARS]
    return title, summary


def _read_revision(path: Path) -> SpecRevision:
    match = REVISION_RE.match(path.name)
    if not match:
        raise SpecError(f"not a revision file name: {path.name}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SpecError(f"cannot read {path.name}: {exc}") from exc
    if not raw.strip():
        raise SpecError(f"specification {path.name} is empty")
    if len(raw) > MAX_SPEC_BYTES:
        raise SpecError(
            f"specification {path.name} is larger than {MAX_SPEC_BYTES} bytes")
    text = raw.decode("utf-8", errors="replace")
    title, summary = _title_and_summary(text)
    return SpecRevision(
        revision=int(match.group(1)),
        name=path.name,
        path=str(path),
        sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
        title=title or match.group(2).replace("-", " "),
        summary=summary,
    )


def list_revisions(spec_dir) -> list:
    """Every well-formed revision, oldest first. Malformed names are ignored
    rather than fatal: an operator's scratch note in the directory must not
    take the route offline."""
    directory = Path(spec_dir)
    if not directory.is_dir():
        return []
    found = []
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.is_symlink():
            continue
        if not REVISION_RE.match(path.name):
            continue
        found.append(_read_revision(path))
        if len(found) >= MAX_REVISIONS:
            break
    return sorted(found, key=lambda r: r.revision)


def latest_revision(spec_dir) -> SpecRevision | None:
    revisions = list_revisions(spec_dir)
    return revisions[-1] if revisions else None


def get_revision(spec_dir, revision: int) -> SpecRevision | None:
    for candidate in list_revisions(spec_dir):
        if candidate.revision == int(revision):
            return candidate
    return None


def read_spec_text(revision: SpecRevision) -> str:
    """The exact bytes that were hashed, re-read and re-verified.

    Re-hashing here is not paranoia about disks; it closes the window between
    the preflight that produced the hash and the dispatch that uses the text.
    """
    path = Path(revision.path)
    try:
        raw = path.read_bytes()[:MAX_SPEC_BYTES]
    except OSError as exc:
        raise SpecError(f"cannot read {revision.name}: {exc}") from exc
    if hashlib.sha256(raw).hexdigest() != revision.sha256:
        raise SpecError(
            f"{revision.name} changed while it was being authorized; "
            "the approved revision no longer matches the file")
    return raw.decode("utf-8", errors="replace").strip()


def scope_digest(public_scope: dict, revision: SpecRevision) -> str:
    """One hash over everything the owner is approving.

    Not just the specification: the repository, the agent, the test command,
    the write scope, the acceptance criteria and the attempt limit are all part
    of what "yes" means. If an operator widens `allowed_paths` between the
    proposal and the authorization, that yes no longer applies to what would
    run, and this digest is what notices.
    """
    payload = {
        "scope": public_scope,
        "spec": {"revision": revision.revision, "sha256": revision.sha256},
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
