#!/usr/bin/env python3
"""Coding Handoff Broker — readiness decisions and status projection.

Pure. Every input is a fact somebody else measured, so the decision itself can
be tested without a repository, a worker or a database.

The point of `preflight` is that it fails *specifically*. The failure this
phase exists to fix was a Front that said "I cannot do that here, open the
CLI" — an answer that is technically true and operationally useless. An owner
who is told which registration is missing can get it fixed; an owner who is
told "no" cannot.

It also fails *before* creating anything. A job created against a repository
with no commits would be dispatched into a `git worktree add` that cannot
succeed, and the owner would be looking at a job id for work that will never
run. Refusing up front and saying why is the honest outcome.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Machine-readable reasons a registered project still cannot run. The gateway
# renders these into Japanese; unknown codes fall through to their detail text,
# so a reason added here is never silently dropped from the owner's reply.
DISABLED = "project_disabled"
SPEC_MISSING = "spec_file_missing"
SUPERVISOR_ALLOWLIST = "repository_not_in_supervisor_allowlist"
BRIDGE_ALLOWLIST = "repository_not_in_bridge_allowlist"
TESTS_NOT_ALLOWLISTED = "tests_command_not_allowlisted"
PATHS_NOT_REGISTERED = "allowed_paths_not_registered"
REPO_PATH_MISSING = "repository_path_missing"
REPO_UNBORN = "repository_has_no_commits"
WORKER_OFFLINE = "worker_offline"
WORKER_BUSY = "worker_busy"
AGENT_UNAVAILABLE = "agent_not_configured_on_bridge"
TOOLCHAIN_MISSING = "worker_toolchain_missing"
SPEC_UNREADABLE = "spec_revision_unreadable"
#: The approved write scope is not the scope the worker would actually enforce.
EFFECTIVE_SCOPE_MISMATCH = "effective_write_scope_mismatch"

#: Refusals that happen at authorization rather than at preflight, because
#: they are about the approval itself rather than about readiness.
SCOPE_CHANGED = "scope_changed_since_proposal"
SPEC_CHANGED = "spec_changed_since_proposal"


@dataclass(frozen=True)
class Facts:
    """What the broker measured about one project, right now.

    `None` means "not measured" and is treated as not-ready, so a probe that
    failed can never read as a pass.
    """

    supervisor_repo: dict | None = None
    bridge_repo: dict | None = None
    bridge_agents: tuple = ()
    repo_path_exists: bool | None = None
    repo_has_head: bool | None = None
    #: The revision the owner would be approving, or None when the spec
    #: directory holds nothing readable.
    spec_revision: object | None = None
    worker_online: bool | None = None
    active_job: str | None = None
    #: Toolchains the Worker image was *measured* to provide. Declared by the
    #: operator in broker config rather than probed, because probing would mean
    #: the broker running containers.
    available_toolchains: tuple = ()
    #: The supervisor-side agent name → bridge agent name mapping, so an agent
    #: the supervisor accepts but the bridge has never heard of is caught here
    #: rather than as a rejected dispatch twenty seconds later.
    agent_map: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Preflight:
    ready: bool
    missing: tuple = ()

    def as_dict(self) -> dict:
        return {"ready": self.ready, "missing": [dict(m) for m in self.missing]}


def _miss(code: str, detail: str) -> dict:
    return {"code": code, "detail": str(detail)[:300]}


def preflight(project, facts: Facts) -> Preflight:
    """Everything that must be true before a chat turn may start this job."""
    missing = []

    if not project.enabled:
        missing.append(_miss(
            DISABLED,
            f"{project.key} is present in the registry but not enabled"))

    if facts.spec_revision is None:
        missing.append(_miss(
            SPEC_MISSING,
            f"{project.key} has no readable specification revision; the "
            "operator must add one before the owner can approve anything"))

    unavailable = [t for t in project.required_toolchains
                   if t not in (facts.available_toolchains or ())]
    if unavailable:
        # The Worker image was measured with python3 and git and nothing else.
        # A project asking for node or cargo is not "not ready yet" in a way
        # that waiting fixes — it needs provisioning, and saying so is the
        # difference between an honest blocker and a job that fails at build.
        missing.append(_miss(
            TOOLCHAIN_MISSING,
            f"the worker image does not provide {unavailable}; provisioning is "
            "required before this specification can be built or tested"))

    supervisor_repo = facts.supervisor_repo
    if not supervisor_repo:
        missing.append(_miss(
            SUPERVISOR_ALLOWLIST,
            f"{project.repository} is not in the supervisor repository allowlist"))
    else:
        allowed_tests = supervisor_repo.get("test_commands") or []
        if project.tests_command and project.tests_command not in allowed_tests:
            missing.append(_miss(
                TESTS_NOT_ALLOWLISTED,
                f"tests_command is not allowlisted for {project.repository}"))
        # Equality, not containment. The scope the owner approves has to be the
        # scope that will actually be enforced: `git_scope` is evaluated by the
        # bridge against *its* registered `allowed_paths`, so a registry that
        # permits more than the proposal showed would accept changes outside
        # what was approved — and, because the digest is taken over the
        # proposal, widening the registry afterwards would not even invalidate
        # the approval. Requiring the two to match makes any widening visible
        # as a readiness failure and a fresh proposal.
        registered_paths = supervisor_repo.get("allowed_paths") or []
        if sorted(project.allowed_paths) != sorted(registered_paths):
            missing.append(_miss(
                PATHS_NOT_REGISTERED,
                f"the approved write scope {sorted(project.allowed_paths)} is not "
                f"exactly what the supervisor has registered for "
                f"{project.repository} ({sorted(registered_paths)})"))

    if facts.bridge_repo is None:
        missing.append(_miss(
            BRIDGE_ALLOWLIST,
            f"{project.repository} is not in the coding bridge repository "
            "registry (the bridge keeps its own allowlist)"))
    else:
        # The bridge's copy is the one that actually decides `git_scope`.
        bridge_paths = facts.bridge_repo.get("allowed_paths")
        if bridge_paths is None:
            missing.append(_miss(
                EFFECTIVE_SCOPE_MISMATCH,
                f"the coding bridge registry has no allowed_paths for "
                f"{project.repository}; the scope that would be enforced is "
                "unknown"))
        elif sorted(project.allowed_paths) != sorted(bridge_paths):
            missing.append(_miss(
                EFFECTIVE_SCOPE_MISMATCH,
                f"the approved write scope {sorted(project.allowed_paths)} does "
                f"not match the scope the worker would enforce "
                f"({sorted(bridge_paths)})"))
        bridge_tests = facts.bridge_repo.get("test_commands")
        if project.tests_command and bridge_tests is not None \
                and project.tests_command not in bridge_tests:
            missing.append(_miss(
                TESTS_NOT_ALLOWLISTED,
                f"the test command is not registered with the coding bridge for "
                f"{project.repository}"))

    bridge_agent = (facts.agent_map or {}).get(project.agent)
    if facts.bridge_agents and (not bridge_agent
                                or bridge_agent not in facts.bridge_agents):
        missing.append(_miss(
            AGENT_UNAVAILABLE,
            f"agent {project.agent} has no command configured on the worker bridge"))

    if facts.repo_path_exists is not True:
        missing.append(_miss(
            REPO_PATH_MISSING,
            f"the worker-side checkout for {project.repository} is not present"))
    elif facts.repo_has_head is not True:
        # The live case for FastRawPhotoView: the remote exists and is empty,
        # so there is no commit to branch a worktree from.
        missing.append(_miss(
            REPO_UNBORN,
            f"{project.repository} has no commits yet; an initial commit is "
            "required before any job can be dispatched"))

    if facts.worker_online is not True:
        missing.append(_miss(WORKER_OFFLINE, "the coding worker bridge is not reachable"))
    elif facts.active_job:
        missing.append(_miss(
            WORKER_BUSY,
            "the worker already has an active job; it runs one at a time"))

    return Preflight(ready=not missing, missing=tuple(missing))


#: Fields of a supervisor job row that may be shown in a chat status reply.
#: An allowlist, not a redaction pass: a field added to the job table later
#: must be opted in deliberately rather than leak by default.
STATUS_FIELDS = (
    "job_id", "status", "repository", "agent", "attempt", "max_attempts",
    "tests_status", "review_status", "failure_reason", "next_action",
)


def status_view(job: dict, *, project_key: str | None = None) -> dict:
    """Project a supervisor job row down to what a chat reply may contain."""
    if not job:
        return {}
    out = {k: job.get(k) for k in STATUS_FIELDS}
    criteria = job.get("acceptance_criteria") or []
    results = job.get("acceptance_results") or []
    out["acceptance_total"] = len(criteria)
    out["acceptance_pass"] = sum(
        1 for r in results if isinstance(r, dict) and r.get("status") == "PASS")
    if project_key:
        out["project_key"] = project_key
    # `goal` is deliberately absent: it is the spec, and the channel already
    # knows what was approved. Echoing it back would put project text into the
    # chat transport for no benefit.
    return out


def dispatch_outcome(dispatch: dict, job: dict) -> dict:
    """Combine what `supervisor.dispatch_job` returned with the job row.

    The dispatch result alone is not enough to tell the owner anything true:
    it reports what the dispatcher *attempted*, while the job row is what
    actually persisted. When they disagree the row wins, because that is the
    state the next status question will read.
    """
    status = (job or {}).get("status") or (dispatch or {}).get("status") or ""
    detail = (dispatch or {}).get("note") or (dispatch or {}).get("error") or ""
    return {
        "job_id": (job or {}).get("job_id") or (dispatch or {}).get("job_id"),
        "status": status,
        "detail": detail,
        "failure_reason": (job or {}).get("failure_reason"),
    }
