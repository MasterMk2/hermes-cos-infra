#!/usr/bin/env python3
"""Coding Handoff Broker — the operator-owned project registry (Phase J.1).

This file answers one question: *which projects may be started from a chat
channel, and with exactly what scope?*

The answer is a static, operator-reviewed document. Nothing in a conversation
can add to it, and the only thing a caller may choose is which of its entries
to use. That is the whole reason the handoff route is not "a model can start
coding jobs": a model — or text that merely travelled through a personal
source — can name a key, and naming a key that is not in this file does
nothing at all.

What an entry pins down, so that the chat side never decides it:

  repository / agent      which allowlisted repo, which production agent
  tests_command           must already be allowlisted for that repository
  allowed_paths           the write scope the supervisor enforces
  acceptance_criteria     deterministic, machine-checkable, written in advance
  required_toolchains     what the Worker image must actually provide
  spec_dir                where the versioned specifications live; a revision
                          from that directory is the ONLY text that becomes
                          the job goal

That last one carries the personal-context isolation guarantee. The Worker
receives one reviewed, hash-bound specification revision. It never receives
the conversation, the system prompt, the envelope, or any credential — not
because those are filtered out, but because they are never read in the first
place. Which revision is in play is the owner's decision, taken in the
channel; see `handoff_spec`.

`required_toolchains` exists because a specification can ask for more than the
Worker can do. The measured image provides `python3` and `git` and nothing
else — no node, no cargo. A project whose spec needs a toolchain the image
does not have must say so up front, so the route answers "provisioning
required" instead of dispatching work that cannot build.

Pure module: parsing and validation only, no I/O beyond a caller-supplied
mapping. `load_registry` is the one convenience that touches the filesystem.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

#: Verification kinds the supervisor can actually evaluate. Kept in step with
#: `jobs_mcp.VERIFICATION_KINDS`; duplicated rather than imported because the
#: broker must not depend on the MCP server package.
VERIFICATION_KINDS = frozenset({
    "file_exists", "function_exists", "test_command", "git_scope",
    "artifact_contains",
})

#: Production agents a chat-started job may use. Fixture agents are excluded on
#: purpose: a job started from Discord that quietly ran the simulator would
#: report progress and acceptance results that mean nothing.
PRODUCTION_AGENTS = frozenset({
    "pi-sandbox-claude-code", "pi-sandbox-codex", "pi-sandbox-opencode",
    "pi-sandbox-hermes-commandcode", "remote-claude-code", "remote-codex",
})

FIXTURE_AGENTS = frozenset({
    "simulated", "fixture-local-hermes", "pi-sandbox-fixture",
})

_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_CRITERION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

_TOOLCHAIN_RE = re.compile(r"^[a-z0-9][a-z0-9+._-]{0,31}$")

#: An absolute path. The deployment is POSIX, but the validator does not assume
#: the *host running it* is — the test suite for this module runs on both, and
#: a check that only ever passes on one platform is a check nobody runs.
#: What matters here is that a *relative* path is refused: `spec_dir` resolved
#: against a working directory would make the specification depend on how the
#: service happened to be started.
_ABSOLUTE_RE = re.compile(r"^(?:/|[A-Za-z]:[\\/])")

MAX_PROJECTS = 64
MAX_CRITERIA = 20
MAX_ALLOWED_PATHS = 32
MAX_TOOLCHAINS = 16


class RegistryError(ValueError):
    """The registry is malformed. The broker refuses to serve rather than
    guess — an ambiguous write scope is not something to be lenient about."""


@dataclass(frozen=True)
class Project:
    key: str
    repository: str
    agent: str
    spec_dir: str
    aliases: tuple = ()
    tests_command: str | None = None
    allowed_paths: tuple = ()
    acceptance_criteria: tuple = ()
    required_toolchains: tuple = ()
    max_attempts: int = 4
    enabled: bool = False
    #: Operator note explaining why this entry exists. Never sent to a channel.
    note: str = ""

    def public_scope(self) -> dict:
        """What may be shown to the owner in a chat proposal, and — because it
        is hashed into the scope digest — exactly what their approval covers.

        Host paths are deliberately absent: the owner approves a scope, not a
        filesystem layout, and `spec_dir` would tell a channel where the
        reviewed documents live for no benefit.
        """
        return {
            "project_key": self.key,
            "repository": self.repository,
            "agent": self.agent,
            "tests_command": self.tests_command,
            "allowed_paths": list(self.allowed_paths),
            "acceptance_criteria": [dict(c) for c in self.acceptance_criteria],
            "required_toolchains": list(self.required_toolchains),
            "max_attempts": self.max_attempts,
        }


@dataclass(frozen=True)
class Registry:
    projects: dict = field(default_factory=dict)

    def get(self, key) -> Project | None:
        return self.projects.get(str(key or "").strip().lower())

    def resolve(self, name) -> Project | None:
        """Match a key or an alias, case-insensitively. Nothing fuzzy: a name
        the operator did not write is not a project."""
        token = str(name or "").strip().lower()
        if not token:
            return None
        direct = self.projects.get(token)
        if direct is not None:
            return direct
        for project in self.projects.values():
            if any(token == str(a).strip().lower() for a in project.aliases):
                return project
        return None

    def enabled_refs(self) -> list:
        """The minimum the gateway needs in order to recognise a name."""
        return [{"key": p.key, "aliases": list(p.aliases)}
                for p in self.projects.values() if p.enabled]


def _criterion(raw, where: str) -> dict:
    if not isinstance(raw, dict):
        raise RegistryError(f"{where}: each acceptance criterion must be a mapping")
    cid = str(raw.get("id") or "")
    if not _CRITERION_ID_RE.match(cid):
        raise RegistryError(f"{where}: criterion id {cid!r} is not a plain identifier")
    description = str(raw.get("description") or "").strip()
    if not description:
        raise RegistryError(f"{where}: criterion {cid} needs a description")
    verification = raw.get("verification")
    if not isinstance(verification, dict):
        raise RegistryError(f"{where}: criterion {cid} needs verification{{kind,params}}")
    kind = str(verification.get("kind") or "")
    if kind not in VERIFICATION_KINDS:
        raise RegistryError(
            f"{where}: criterion {cid} has unknown verification kind {kind!r}; "
            f"allowed: {sorted(VERIFICATION_KINDS)}")
    params = verification.get("params") or {}
    if not isinstance(params, dict):
        raise RegistryError(f"{where}: criterion {cid} params must be a mapping")
    return {"id": cid, "description": description,
            "verification": {"kind": kind, "params": dict(params)}}


def _project(key: str, raw) -> Project:
    where = f"projects.{key}"
    if not isinstance(raw, dict):
        raise RegistryError(f"{where} must be a mapping")
    if not _KEY_RE.match(key):
        raise RegistryError(
            f"{where}: project key must be lower-case [a-z0-9._-], 2..64 chars")

    unknown = set(raw) - {
        "repository", "agent", "spec_dir", "aliases", "tests_command",
        "allowed_paths", "acceptance_criteria", "max_attempts", "enabled",
        "required_toolchains", "note",
    }
    if unknown:
        raise RegistryError(f"{where} has unknown keys: {sorted(unknown)}")

    repository = str(raw.get("repository") or "")
    if not _REPO_RE.match(repository):
        raise RegistryError(
            f"{where}: repository must be the allowlisted repository *id*, not a path")

    agent = str(raw.get("agent") or "")
    if agent in FIXTURE_AGENTS:
        raise RegistryError(
            f"{where}: {agent!r} is a fixture agent; a chat-started job must run "
            "a production agent or it reports results that mean nothing")
    if agent not in PRODUCTION_AGENTS:
        raise RegistryError(
            f"{where}: unknown agent {agent!r}; allowed: {sorted(PRODUCTION_AGENTS)}")

    spec_dir = str(raw.get("spec_dir") or "")
    if not _ABSOLUTE_RE.match(spec_dir):
        raise RegistryError(f"{where}: spec_dir must be an absolute path")

    toolchains = raw.get("required_toolchains") or []
    if not isinstance(toolchains, list) or len(toolchains) > MAX_TOOLCHAINS:
        raise RegistryError(
            f"{where}: required_toolchains must be a list of at most {MAX_TOOLCHAINS}")
    for tool in toolchains:
        if not _TOOLCHAIN_RE.match(str(tool or "")):
            raise RegistryError(f"{where}: toolchain {tool!r} is not a plain name")
    if not toolchains:
        raise RegistryError(
            f"{where}: required_toolchains must be stated explicitly — the "
            "Worker image provides python3 and git only, and a project that "
            "does not declare what it needs cannot be checked against that")

    aliases = raw.get("aliases") or []
    if not isinstance(aliases, list) or len(aliases) > 8:
        raise RegistryError(f"{where}: aliases must be a list of at most 8 names")
    for alias in aliases:
        if not _ALIAS_RE.match(str(alias or "")):
            raise RegistryError(f"{where}: alias {alias!r} is not a plain name")

    allowed_paths = raw.get("allowed_paths") or []
    if not isinstance(allowed_paths, list) or len(allowed_paths) > MAX_ALLOWED_PATHS:
        raise RegistryError(
            f"{where}: allowed_paths must be a list of at most {MAX_ALLOWED_PATHS}")
    for p in allowed_paths:
        text = str(p or "")
        if not text or _ABSOLUTE_RE.match(text) or ".." in Path(text).parts:
            raise RegistryError(
                f"{where}: allowed_paths entry {text!r} must be a relative "
                "in-repository prefix")

    criteria = raw.get("acceptance_criteria") or []
    if not isinstance(criteria, list) or not criteria:
        raise RegistryError(
            f"{where}: acceptance_criteria must be a non-empty list — a job with "
            "nothing to check can never be accepted or refused")
    if len(criteria) > MAX_CRITERIA:
        raise RegistryError(f"{where}: at most {MAX_CRITERIA} acceptance criteria")
    parsed = tuple(_criterion(c, where) for c in criteria)
    ids = [c["id"] for c in parsed]
    if len(set(ids)) != len(ids):
        raise RegistryError(f"{where}: duplicate acceptance criterion ids")

    tests_command = raw.get("tests_command")
    tests_command = str(tests_command) if tests_command else None
    if any(c["verification"]["kind"] == "test_command" for c in parsed) \
            and not tests_command:
        # The same rule the supervisor enforces at creation time. Catching it
        # in the registry means the operator hears about it while editing the
        # registry, not when the owner's authorization fails.
        raise RegistryError(
            f"{where}: a test_command criterion requires tests_command")

    try:
        max_attempts = int(raw.get("max_attempts", 4))
    except (TypeError, ValueError) as exc:
        raise RegistryError(f"{where}: max_attempts must be an integer") from exc
    if not (1 <= max_attempts <= 10):
        raise RegistryError(f"{where}: max_attempts must be 1..10")

    return Project(
        key=key,
        repository=repository,
        agent=agent,
        spec_dir=spec_dir,
        aliases=tuple(str(a) for a in aliases),
        tests_command=tests_command,
        allowed_paths=tuple(str(p) for p in allowed_paths),
        acceptance_criteria=parsed,
        required_toolchains=tuple(str(t) for t in toolchains),
        max_attempts=max_attempts,
        enabled=bool(raw.get("enabled", False)),
        note=str(raw.get("note") or ""),
    )


def parse_registry(data) -> Registry:
    if not isinstance(data, dict):
        raise RegistryError("registry root must be a mapping")
    unknown = set(data) - {"version", "projects"}
    if unknown:
        raise RegistryError(f"registry has unknown keys: {sorted(unknown)}")
    if int(data.get("version") or 0) != 1:
        raise RegistryError("registry version must be 1")
    raw_projects = data.get("projects") or {}
    if not isinstance(raw_projects, dict):
        raise RegistryError("projects must be a mapping of key -> entry")
    if len(raw_projects) > MAX_PROJECTS:
        raise RegistryError(f"at most {MAX_PROJECTS} projects")
    projects = {}
    seen_names = {}
    for key, raw in raw_projects.items():
        project = _project(str(key), raw)
        projects[project.key] = project
        for name in (project.key, *project.aliases):
            lowered = str(name).lower()
            if lowered in seen_names and seen_names[lowered] != project.key:
                raise RegistryError(
                    f"name {name!r} is claimed by both {seen_names[lowered]!r} "
                    f"and {project.key!r}; an ambiguous name must not select a "
                    "write scope")
            seen_names[lowered] = project.key
    return Registry(projects=projects)


def load_registry(path) -> Registry:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise RegistryError(f"cannot read registry {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise RegistryError(f"registry {path} is not valid JSON: {exc}") from exc
    return parse_registry(data)
