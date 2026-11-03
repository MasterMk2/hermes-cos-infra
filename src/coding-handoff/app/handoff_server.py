#!/usr/bin/env python3
"""Coding Handoff Broker — the typed HTTP surface (Phase J.1).

Loopback only, bearer token, four actions, no free-form argument anywhere. The
shape follows the premium-CLI service that is already deployed on this host,
because an operator who has reviewed that one has already reviewed this one's
threat model: a small stdlib server, bound to 127.0.0.1, whose entire input
vocabulary fits on a screen.

    POST /handoff/projects    which projects the operator has enabled
    POST /handoff/preflight   scope + specification revision + readiness
    POST /handoff/authorize   create and dispatch exactly one job
    POST /handoff/status      read one job this broker created

What the caller may choose is a project key, a scope digest and an
authorization id. It may not choose a repository, a path, a command, an agent,
a model, a prompt or a spec: every one of those comes from the operator's
registry, and that is the property that makes it safe for the other end of
this socket to be a chat channel.

Namespace note (measured, not assumed)
--------------------------------------
The supervisor's repository registry holds *container-local* paths
(`/opt/data/...`) while the coding bridge's registry holds *host* paths. This
broker runs on the host, so it reads the bridge registry for anything about
files on disk, and treats the supervisor registry purely as an allowlist and a
source of the approved test commands and write scope. It never rewrites either.

Dispatching from the host is safe for the same measured reason:
`supervisor.run_job_loop` sets `repo_path = None` for every adapter that is not
`local-fixture`, so a `pi-sandbox` job never resolves a registry path at all —
all of its git work happens in the bridge's own worktree.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

import handoff_core as core
import handoff_ledger as ledger
import handoff_registry as registry
import handoff_spec as spec

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("coding-handoff")

#: Requests are tiny. A body larger than this is not a mistake worth parsing.
MAX_BODY_BYTES = 16384

_AUTH_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_JOB_ID_RE = re.compile(r"^job_[0-9a-f]{10}$")

DEFAULT_CONFIG_PATH = "/opt/hermes-coding-handoff/config/handoff.json"

CONFIG_KEYS = {
    "listen_host", "listen_port", "token_file", "registry_file", "jobs_dir",
    "bridge_config", "bridge_socket", "ledger_db", "available_toolchains",
    "dispatch_enabled",
}


class ConfigError(ValueError):
    pass


def load_config(path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ConfigError("broker config root must be a mapping")
    unknown = set(data) - CONFIG_KEYS
    if unknown:
        raise ConfigError(f"broker config has unknown keys: {sorted(unknown)}")
    cfg = {
        "listen_host": str(data.get("listen_host") or "127.0.0.1"),
        "listen_port": int(data.get("listen_port") or 18771),
        "token_file": str(data.get("token_file") or ""),
        "registry_file": str(data.get("registry_file") or ""),
        "jobs_dir": str(data.get("jobs_dir") or ""),
        "bridge_config": str(data.get("bridge_config") or ""),
        "bridge_socket": str(data.get("bridge_socket") or ""),
        "ledger_db": str(data.get("ledger_db") or ""),
        "available_toolchains": tuple(
            str(t) for t in (data.get("available_toolchains") or ())),
        # Off by default: an operator installing the broker and an operator
        # allowing it to start real jobs are two decisions, and they should be
        # able to make the first one and look around before making the second.
        "dispatch_enabled": bool(data.get("dispatch_enabled", False)),
    }
    if cfg["listen_host"] not in ("127.0.0.1", "localhost", "::1"):
        raise ConfigError(
            "listen_host must be loopback; the broker is reached from this host "
            "only, and binding anything else publishes a job-creation API")
    for required in ("token_file", "registry_file", "jobs_dir", "bridge_config",
                     "ledger_db"):
        if not cfg[required]:
            raise ConfigError(f"broker config needs {required}")
    if not cfg["available_toolchains"]:
        raise ConfigError(
            "available_toolchains must list what the worker image was measured "
            "to provide; an empty list would let every project look buildable")
    return cfg


def load_token(path) -> str:
    token = Path(path).read_text(encoding="utf-8").strip()
    if len(token) < 32:
        raise ConfigError("broker token must be at least 32 characters")
    return token


# ------------------------------------------------------------------ facts


def bridge_request(sock_path: str, payload: dict, timeout: float = 20.0) -> dict:
    """One JSON line in, one JSON line out — the bridge's own protocol."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(sock_path)
        s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        with s.makefile("r") as fh:
            line = fh.readline()
        return json.loads(line) if line.strip() else {}
    finally:
        s.close()


def git_has_head(repo_path: str, timeout: float = 30.0):
    """(exists, has_head). `has_head` is False for an unborn repository — the
    live state of the project this phase was opened for."""
    path = Path(repo_path)
    if not path.is_dir():
        return False, False
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--verify", "HEAD"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return True, False
    return True, proc.returncode == 0 and bool((proc.stdout or "").strip())


@dataclass(frozen=True)
class Snapshot:
    """One project, as it was at a single instant.

    Taken once and carried through validation and creation, so that the scope
    the owner approved and the scope that executes cannot be two different
    readings of a registry an operator is editing.
    """

    project: object
    revision: object
    scope: dict
    digest: str
    preflight: object


class Broker:
    """Everything the HTTP layer needs, with the I/O in one place so the
    request handler stays a thin translation of JSON to method calls."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.jobs_dir = Path(cfg["jobs_dir"])

    # -- registries ---------------------------------------------------

    def registry(self):
        return registry.load_registry(self.cfg["registry_file"])

    def supervisor_repos(self) -> dict:
        """Membership, test commands and write scope — never the path.

        Read as data rather than through `supervisor.repo_cfg` so that this
        host-side process cannot accidentally act on a container-local path.
        """
        raw = json.loads(
            (self.jobs_dir / "worker_config.json").read_text(encoding="utf-8"))
        return raw.get("repositories") or {}

    def bridge_registry(self) -> tuple:
        raw = json.loads(Path(self.cfg["bridge_config"]).read_text(encoding="utf-8"))
        return raw.get("repositories") or {}, tuple(raw.get("agents") or ())

    def agent_map(self) -> dict:
        """The supervisor's own logical-agent → bridge-agent mapping, read from
        the transport module so this never drifts from what dispatch will do."""
        sys.path.insert(0, str(self.jobs_dir))
        import transport  # noqa: PLC0415 — deliberately late and path-scoped
        return dict(transport.PiSandboxWorkerAdapter.AGENT_MAP)

    def worker_state(self) -> tuple:
        sock_path = self.cfg["bridge_socket"]
        if not sock_path or not Path(sock_path).exists():
            return False, None
        try:
            resp = bridge_request(sock_path, {"version": 1,
                                              "action": "worker_status"})
        except (OSError, ValueError):
            return False, None
        online = bool(resp.get("bridge") == "alive"
                      and resp.get("docker_reachable"))
        return online, resp.get("active_job") or resp.get("active_review")

    # -- decisions ----------------------------------------------------

    def gather(self, project) -> core.Facts:
        bridge_repos, bridge_agents = self.bridge_registry()
        bridge_repo = bridge_repos.get(project.repository)
        exists = has_head = None
        if bridge_repo:
            exists, has_head = git_has_head(bridge_repo.get("path") or "")
        online, active = self.worker_state()
        try:
            revision = spec.latest_revision(project.spec_dir)
        except spec.SpecError as exc:
            log.warning("spec unreadable for %s: %s", project.key, exc)
            revision = None
        return core.Facts(
            supervisor_repo=self.supervisor_repos().get(project.repository),
            bridge_repo=bridge_repo,
            bridge_agents=bridge_agents,
            repo_path_exists=exists,
            repo_has_head=has_head,
            spec_revision=revision,
            worker_online=online,
            active_job=active,
            available_toolchains=self.cfg["available_toolchains"],
            agent_map=self.agent_map(),
        )

    def snapshot(self, project_key: str):
        """One immutable view of a project, taken once.

        The registry is read exactly once here, and the resulting `Snapshot` is
        what every later step uses. Reading it twice — once to validate and
        once to execute — is a real race: an operator editing the registry in
        between would have the *second* read's agent, test command and write
        scope executed under an approval taken against the first.
        """
        project = self.registry().resolve(project_key)
        if project is None:
            return None
        facts = self.gather(project)
        scope = project.public_scope()
        revision = facts.spec_revision
        digest = spec.scope_digest(scope, revision) if revision else ""
        result = core.preflight(project, facts)
        return Snapshot(project=project, revision=revision, scope=scope,
                        digest=digest, preflight=result)

    def preflight(self, project_key: str) -> dict:
        snap = self.snapshot(project_key)
        if snap is None:
            return {"error": "project_not_registered", "project_key": project_key}
        return self._render_preflight(snap)

    def _render_preflight(self, snap) -> dict:
        payload = {"project_key": snap.project.key, "scope": snap.scope,
                   **snap.preflight.as_dict()}
        if snap.revision is not None:
            payload["spec"] = snap.revision.as_dict()
            payload["scope_digest"] = snap.digest
        if not self.cfg["dispatch_enabled"]:
            payload["ready"] = False
            payload.setdefault("missing", []).append(
                {"code": "dispatch_disabled",
                 "detail": "the broker is installed but dispatch is not enabled"})
        return payload

    def authorize(self, *, authorization_id: str, conversation_id: str,
                  project_key: str, scope_digest: str) -> dict:
        """Create and dispatch at most one job for this authorization, ever.

        The ledger is consulted **before** any readiness check. A duplicate of
        a request that already succeeded must answer with the job that exists,
        and readiness is a property of starting new work — an approval that
        already produced a running job cannot become "not ready" because the
        worker got busy or the operator edited a spec afterwards. Reporting
        `not_ready` there would tell the owner nothing was created while their
        job was running.
        """
        con = ledger.open_db(self.cfg["ledger_db"])
        try:
            # A completed authorization is answered from the ledger alone. An
            # earlier version resolved the registry first, so a project renamed
            # or removed after a successful dispatch made the replay report
            # `project_not_registered` for a job that exists and is running.
            recorded = ledger.get(con, authorization_id)
            if recorded and recorded["state"] == ledger.CREATED \
                    and recorded["scope_digest"] == scope_digest \
                    and recorded["conversation_id"] == conversation_id:
                return self._replay(recorded)

            registry_project = self.registry().resolve(project_key)
            if registry_project is None:
                return {"error": "project_not_registered",
                        "project_key": project_key}
            canonical_key = registry_project.key
            job_id = ledger.deterministic_job_id(authorization_id, canonical_key,
                                                 scope_digest)
            outcome, row, epoch = ledger.claim(
                con, authorization_id, conversation_id=conversation_id,
                project_key=canonical_key, scope_digest=scope_digest,
                job_id=job_id)

            if outcome == ledger.CONFLICT:
                return {"error": "authorization_conflict",
                        "project_key": canonical_key}
            if outcome == ledger.DONE:
                return self._replay(row)
            if outcome == ledger.IN_PROGRESS:
                return {"error": "authorization_in_progress",
                        "project_key": canonical_key,
                        "job_id": (row or {}).get("job_id")}

            recorded_id = (row or {}).get("job_id") or job_id
            existing = self._get_job(recorded_id)
            if existing:
                # A previous attempt died after creating the job. Finish the
                # correlation rather than creating a second one, and do not
                # re-apply transient readiness gates to work that already runs.
                result = self._dispatch_approved(recorded_id, existing,
                                                 canonical_key, scope_digest)
                ledger.settle(con, authorization_id, epoch=epoch,
                              state=ledger.CREATED,
                              detail="recovered after interrupted claim")
                result.update({"project_key": canonical_key, "replayed": True})
                return result

            snap = self.snapshot(canonical_key)
            rendered = self._render_preflight(snap) if snap else {}
            if snap is None or not rendered.get("ready"):
                ledger.settle(con, authorization_id, epoch=epoch,
                              state=ledger.REFUSED, detail="not ready")
                return {"error": "not_ready", "project_key": canonical_key,
                        "missing": rendered.get("missing") or []}
            if scope_digest != snap.digest:
                ledger.settle(con, authorization_id, epoch=epoch,
                              state=ledger.REFUSED, detail="scope changed")
                return {"error": core.SCOPE_CHANGED,
                        "project_key": canonical_key,
                        "spec": rendered.get("spec"), "scope": snap.scope,
                        "scope_digest": snap.digest}

            try:
                result = self._create_and_dispatch(snap, recorded_id)
            except Exception as exc:
                ledger.settle(con, authorization_id, epoch=epoch,
                              state=ledger.REFUSED,
                              detail=f"{type(exc).__name__}: {exc}")
                raise
            if result.get("error"):
                ledger.settle(con, authorization_id, epoch=epoch,
                              state=ledger.REFUSED,
                              detail=str(result.get("detail") or result["error"]))
                result["project_key"] = canonical_key
                return result
            ledger.settle(con, authorization_id, epoch=epoch,
                          state=ledger.CREATED,
                          detail=result.get("detail") or "")
            result["project_key"] = canonical_key
            return result
        finally:
            con.close()

    def _replay(self, row: dict) -> dict:
        """Answer a duplicate with what actually exists now."""
        job = self._get_job((row or {}).get("job_id")) or {}
        if job:
            result = self._dispatch_approved(row["job_id"], job,
                                             row["project_key"], row["scope_digest"])
            result.update(replayed=True, project_key=row["project_key"])
            return result
        return {"replayed": True,
                "project_key": (row or {}).get("project_key"),
                "job_id": (row or {}).get("job_id"),
                "status": job.get("status") or "",
                "failure_reason": job.get("failure_reason"),
                "detail": (row or {}).get("detail") or ""}

    def _jobs(self):
        sys.path.insert(0, str(self.jobs_dir))
        import jobs_db  # noqa: PLC0415
        import supervisor  # noqa: PLC0415
        return jobs_db, supervisor

    def _get_job(self, job_id):
        if not job_id:
            return None
        jobs_db, _ = self._jobs()
        jobs_db.init_db()
        return jobs_db.get_job(job_id)

    def _dispatch_if_needed(self, job_id: str, job: dict) -> dict:
        jobs_db, supervisor = self._jobs()
        dispatch = supervisor.dispatch_job(job_id)
        fresh = jobs_db.get_job(job_id) or job
        return core.dispatch_outcome(dispatch, fresh)

    def _dispatch_approved(self, job_id: str, job: dict,
                           project_key: str, approved_digest: str) -> dict:
        # Readback of work already running must remain possible even when the
        # registration changes. Starting an interrupted job is a new side
        # effect and must satisfy the original approval and both allowlists.
        view = core.status_view(job, project_key=project_key)
        view["job_id"] = job_id
        if job.get("status") not in ("NEW", "FAILED_TO_START"):
            return view
        snap = self.snapshot(project_key)
        ready = self._render_preflight(snap) if snap else {}
        if not ready.get("ready") or snap.digest != approved_digest:
            view.update(error="recovery_blocked",
                        detail="starting this job requires the original approved scope and a ready worker",
                        missing=ready.get("missing", []))
            return view
        return self._dispatch_if_needed(job_id, job)

    def _create_and_dispatch(self, snap, job_id: str) -> dict:
        """Create the job under its derived id, then dispatch it.

        The registry and the specification are re-verified against the snapshot
        immediately before the first durable side effect, so the window between
        approval and creation cannot be used to substitute a different agent,
        test command, write scope or specification.
        """
        import sqlite3 as _sqlite3

        jobs_db, _ = self._jobs()
        project = snap.project
        fresh = self.registry().resolve(project.key)
        if fresh is None or fresh.public_scope() != snap.scope:
            return {"error": core.SCOPE_CHANGED,
                    "detail": "the registry changed while it was being authorized"}
        # The bridge's registry is what actually enforces the write scope, so
        # it is re-read here too: widening it between the proposal and this
        # moment would otherwise let the old approval cover the new scope.
        try:
            bridge_repos, _ = self.bridge_registry()
        except (OSError, ValueError) as exc:
            return {"error": core.SCOPE_CHANGED,
                    "detail": f"the worker registry became unreadable: {exc}"}
        bridge_now = bridge_repos.get(project.repository) or {}
        if sorted(bridge_now.get("allowed_paths") or []) != \
                sorted(project.allowed_paths):
            return {"error": core.SCOPE_CHANGED,
                    "detail": "the worker's write scope changed while it was "
                              "being authorized"}
        revision = spec.get_revision(project.spec_dir, snap.revision.revision)
        if revision is None or revision.sha256 != snap.revision.sha256:
            return {"error": core.SPEC_CHANGED,
                    "detail": "the specification changed while it was being "
                              "authorized"}
        if spec.scope_digest(fresh.public_scope(), revision) != snap.digest:
            return {"error": core.SCOPE_CHANGED,
                    "detail": "the approved scope no longer matches"}

        # `read_spec_text` re-hashes the file it reads, so the bytes that
        # become the goal are provably the bytes the owner approved.
        goal = build_goal(project, revision, self.cfg["available_toolchains"])
        jobs_db.init_db()
        try:
            jobs_db.create_job(
                goal=goal,
                repository=project.repository,
                acceptance_criteria=[dict(c) for c in project.acceptance_criteria],
                agent=project.agent,
                project_id=project.key,
                kind="implementation",
                max_attempts=project.max_attempts,
                tests_command=project.tests_command,
                job_id=job_id,
            )
        except _sqlite3.IntegrityError:
            # Another writer created it under the same derived id. That is the
            # exactly-once guarantee working, not a failure.
            log.info("job %s already existed; adopting it", job_id)
        job = jobs_db.get_job(job_id) or {}
        result = self._dispatch_approved(job_id, job, project.key, snap.digest)
        log.info("authorized job=%s project=%s revision=%s status=%s",
                 job_id, project.key, revision.revision, result.get("status"))
        return result

    def status(self, *, job_id: str = "", project_key: str = "",
               authorization_id: str = "") -> dict:
        """Read one job back.

        `authorization_id` addresses **one approval**, and when it is given the
        answer is resolved from the ledger alone — no falling back to the
        project's most recent job. That fallback is the wrong answer to "what
        happened to the instruction I just gave": a project with earlier jobs
        would have an unrelated one reported as the outcome of this approval.

        Three outcomes are distinguishable on purpose: no record at all (the
        request never arrived, so nothing was created), a record with a job
        (here it is), and a record whose job has not appeared yet (uncertain).
        """
        con = ledger.open_db(self.cfg["ledger_db"])
        try:
            if authorization_id:
                record = ledger.get(con, authorization_id)
                if record is None:
                    return {"error": "authorization_unknown",
                            "authorization_id": authorization_id}
            elif job_id:
                record = ledger.job_is_ours(con, job_id)
            else:
                project = self.registry().resolve(project_key)
                if project is None:
                    return {"error": "project_not_registered"}
                record = ledger.latest_for_project(con, project.key)
        finally:
            con.close()
        if not record or not record.get("job_id"):
            # Jobs this broker did not create are not answerable here. The
            # supervisor holds jobs from earlier phases and from the operator's
            # own CLI use; none of them were authorized on a channel.
            return {"error": "job_not_from_handoff"}
        job = self._get_job(record["job_id"])
        if not job:
            # The ledger records the derived id before the job exists, so this
            # is a real state: authorized, not yet created. Reported as
            # uncertain rather than as a job, and never as running.
            return {"job_id": record["job_id"],
                    "project_key": record.get("project_key"),
                    "authorization_id": record.get("authorization_id"),
                    "status": "",
                    "detail": record.get("detail") or "authorization recorded"}
        view = core.status_view(job, project_key=record.get("project_key"))
        view["authorization_id"] = record.get("authorization_id")
        return view


def build_goal(project, revision, toolchains=()) -> str:
    """The job prompt: one reviewed specification revision, and nothing else.

    No conversation, no channel metadata, no envelope, no owner identity, no
    credential. The header exists so the Worker can see which revision it is
    building and what it may touch — both of which are already in the registry
    and the spec, and neither of which came from a chat message.
    """
    text = spec.read_spec_text(revision)
    scope = ", ".join(project.allowed_paths) or "(registered write scope)"
    # Stated from the operator's measurement of the image, not hard-coded: a
    # provisioned image that the readiness gate accepted would otherwise be
    # described to the worker as the one that could not build anything.
    available = ", ".join(toolchains) or "(not stated)"
    header = (
        f"# {project.key} — specification {revision.label}\n"
        f"\n"
        f"Repository: {project.repository}\n"
        f"Writable paths: {scope}\n"
        f"Test command: {project.tests_command or '(none registered)'}\n"
        f"Available toolchains: {available} (no network access)\n"
        f"\n"
        f"Implement the specification below. Stay inside the writable paths.\n"
        f"---\n"
    )
    return header + text


# ------------------------------------------------------------------ HTTP


class Handler(BaseHTTPRequestHandler):
    broker: Broker = None
    token: str = ""

    server_version = "hermes-coding-handoff/1.0"
    sys_version = ""

    def _send(self, code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "").strip()
        if not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(header[7:].strip(), self.token)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None
        if length <= 0 or length > MAX_BODY_BYTES:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def do_GET(self):
        if self.path in ("/health", "/status"):
            # Deliberately says nothing about projects, jobs or paths: an
            # unauthenticated endpoint should confirm the process is alive and
            # not describe what it can do.
            self._send(200, {"status": "ok", "component": "coding-handoff",
                             "dispatch_enabled": bool(
                                 self.broker.cfg["dispatch_enabled"])})
            return
        self._send(404, {"error": "not_found"})

    def do_POST(self):
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return
        body = self._body()
        if body is None or not isinstance(body, dict):
            self._send(400, {"error": "bad_request"})
            return
        try:
            self._send(200, self._dispatch(self.path, body))
        except FileNotFoundError as exc:
            log.error("missing file: %s", exc)
            self._send(503, {"error": "broker_misconfigured"})
        except registry.RegistryError as exc:
            log.error("registry refused: %s", exc)
            self._send(503, {"error": "registry_invalid", "detail": str(exc)[:200]})
        except Exception as exc:  # never leak a traceback to the caller
            log.exception("request failed")
            self._send(500, {"error": "internal",
                             "detail": type(exc).__name__})

    def _dispatch(self, path: str, body: dict) -> dict:
        if path == "/handoff/projects":
            return {"projects": self.broker.registry().enabled_refs()}
        if path == "/handoff/preflight":
            return self.broker.preflight(str(body.get("project_key") or ""))
        if path == "/handoff/authorize":
            authorization_id = str(body.get("authorization_id") or "")
            digest = str(body.get("scope_digest") or "")
            if not _AUTH_ID_RE.match(authorization_id):
                return {"error": "bad_authorization_id"}
            if not _DIGEST_RE.match(digest):
                return {"error": "bad_scope_digest"}
            return self.broker.authorize(
                authorization_id=authorization_id,
                conversation_id=str(body.get("conversation_id") or "")[:200],
                project_key=str(body.get("project_key") or ""),
                scope_digest=digest,
            )
        if path == "/handoff/status":
            job_id = str(body.get("job_id") or "")
            if job_id and not _JOB_ID_RE.match(job_id):
                return {"error": "bad_job_id"}
            authorization_id = str(body.get("authorization_id") or "")
            if authorization_id and not _AUTH_ID_RE.match(authorization_id):
                return {"error": "bad_authorization_id"}
            return self.broker.status(
                job_id=job_id,
                project_key=str(body.get("project_key") or ""),
                authorization_id=authorization_id)
        return {"error": "unknown_action"}

    def log_message(self, fmt, *args):
        # Request lines only, never bodies: a body carries a project key and an
        # authorization id, and neither belongs in a shared journal.
        log.info("%s", fmt % args)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def run(config_path: str = None):
    cfg = load_config(config_path or os.environ.get("HANDOFF_CONFIG")
                      or DEFAULT_CONFIG_PATH)
    Handler.broker = Broker(cfg)
    Handler.token = load_token(cfg["token_file"])
    # Fail closed at start-up rather than on the owner's first authorization.
    Handler.broker.registry()
    httpd = ThreadedHTTPServer((cfg["listen_host"], cfg["listen_port"]), Handler)
    log.info("coding handoff broker on %s:%s (dispatch_enabled=%s)",
             cfg["listen_host"], cfg["listen_port"], cfg["dispatch_enabled"])
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("stopping")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else None)
