#!/usr/bin/env python3
"""Job Supervisor MCP server (stdio).

Exposes the Supervisor loop to Hermes. Write operations are explicitly
marked in tool descriptions. There is deliberately NO generic shell tool:
the only executed commands are allowlisted repository test commands.

Tools:
  job_create / job_get / job_list / job_status / job_dispatch /
  job_continue / job_validate / job_cancel / job_reconcile

Production Agent addendum (F.1.1-Pi): pi-sandbox-hermes-commandcode is the
production coding agent (worker Hermes CLI + CommandCode + GLM-5.3-Flash)
dispatched through the same PiSandboxWorkerAdapter; the supervisor core
stays provider-agnostic.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import jobs_db
import supervisor
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
import mcp.types as types

ID_SCHEMA = {
    "type": "object",
    "properties": {"job_id": {"type": "string"}},
    "required": ["job_id"],
}

# F.1.1-Pi.2: deterministic verification kinds the supervisor understands.
VERIFICATION_KINDS = {"file_exists", "function_exists", "test_command",
                      "git_scope", "artifact_contains"}


def _job_brief(job):
    brief = {
        "job_id": job["job_id"], "status": job["status"], "kind": job["kind"],
        "goal": job["goal"][:200], "repository": job["repository"],
        "project_id": job["project_id"], "agent": job["agent"],
        "attempt": job["attempt"], "max_attempts": job["max_attempts"],
        "branch": job["branch"], "tests_status": job["tests_status"],
        "acceptance_pass": sum(1 for a in job.get("acceptance_results", [])
                               if a.get("status") == "PASS"),
        "acceptance_total": len(job.get("acceptance_criteria", [])),
        "needs_human": bool(job["needs_human"]),
        "failure_reason": job["failure_reason"],
        "next_action": job["next_action"],
        # F.2 review surface (section 115)
        "review_status": job.get("review_status"),
    }
    brief.update(_review_surface(job["job_id"]))
    return brief


def _review_surface(job_id, summary_chars=200):
    """Section 115: the F.2 review fields every job-shaped response carries.

    Kept in one place so job_get and job_list can never drift apart on what
    they expose about a job's review state."""
    out = {}
    try:
        latest = jobs_db.latest_review(job_id)
        if latest:
            out["review_cycle"] = latest.get("cycle")
            out["review_model"] = latest.get("reviewer_model")
            out["latest_review_summary"] = (latest.get("summary") or "")[:summary_chars]
            out["blocking_findings_count"] = latest.get("blocking_count") or 0
            out["latest_review_verdict"] = latest.get("verdict")
            out["latest_review_content_progress"] = latest.get("content_progress")
        approved = jobs_db.latest_approved_review(job_id)
        if approved:
            out["approved_target_sha"] = (approved.get("target_sha") or "")[:12]
            out["approved_content_sha"] = (approved.get("content_sha") or "")[:16]
    except Exception:
        pass
    return out


def _review_brief(r, include_findings=False):
    b = {
        "review_id": r.get("review_id"), "job_id": r.get("job_id"),
        "cycle": r.get("cycle"), "attempt_no": r.get("attempt_no"),
        "status": r.get("status"), "reviewer_model": r.get("reviewer_model"),
        "target_sha": r.get("target_sha"), "diff_hash": r.get("diff_hash"),
        "content_sha": r.get("content_sha"),
        # section 61/107: 'changed' means the implementation actually moved
        # since the previous verdicted cycle; 'unchanged' means the reviewer
        # converged without any code change (not fix-loop evidence).
        "content_progress": r.get("content_progress"),
        "verdict": r.get("verdict"),
        "summary": (r.get("summary") or "")[:300],
        "blocking_count": r.get("blocking_count") or 0,
        "nonblocking_count": len(r.get("findings") or []) -
                             (r.get("blocking_count") or 0),
        "started_at": r.get("started_at"), "finished_at": r.get("finished_at"),
        "error_summary": (r.get("error_summary") or "")[:300],
    }
    if include_findings:
        b["findings"] = r.get("findings") or []
    return b


def _text(obj):
    return [types.TextContent(type="text", text=json.dumps(obj, ensure_ascii=False))]


async def _list_tools(ctx=None, params=None):
    return types.ListToolsResult(tools=[
        types.Tool(
            name="job_create",
            description=(
                "WRITE: create a new supervised coding job. Requires: goal, "
                "repository (must already be allowlisted in the worker config; "
                "new repositories need human approval), acceptance_criteria "
                "(non-empty list with ids), optional tests_command (must be "
                "allowlisted for the repository), optional project_id."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "goal": {"type": "string"},
                    "repository": {"type": "string"},
                    "acceptance_criteria": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "description": {"type": "string"},
                                "verification": {
                                    "type": "object",
                                    "properties": {
                                        "kind": {"type": "string",
                                                 "enum": ["file_exists", "function_exists",
                                                          "test_command", "git_scope",
                                                          "artifact_contains"]},
                                        "params": {"type": "object"},
                                    },
                                },
                            },
                            "required": ["id", "description", "verification"],
                        },
                    },
                    "tests_command": {"type": "string"},
                    "project_id": {"type": "string"},
                    "task_id": {"type": "string"},
                    "kind": {"type": "string"},
                    "max_attempts": {"type": "integer", "minimum": 1, "maximum": 10},
                    "agent": {"type": "string"},
                },
                "required": ["goal", "repository", "acceptance_criteria"],
            },
        ),
        types.Tool(name="job_get",
                   description="Read one job with run history (no file contents).",
                   inputSchema=ID_SCHEMA),
        types.Tool(
            name="job_list",
            description="List jobs (optional status/project_id filter), newest first.",
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "project_id": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
            },
        ),
        types.Tool(
            name="job_status",
            description=("Supervisor observability: job counts by status, worker "
                         "online/offline, last run. No job content."),
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="job_dispatch",
            description=("WRITE: start (or resume) the supervision loop for a job "
                         "in NEW/QUEUED/CONTINUE_REQUIRED/WAITING_HUMAN-approve state. "
                         "Runs detached; poll job_get for progress."),
            inputSchema=ID_SCHEMA,
        ),
        types.Tool(
            name="job_continue",
            description=("WRITE: approve continuation for a job stopped in "
                         "CONTINUE_REQUIRED/WAITING_HUMAN/AGENT_STOPPED and dispatch "
                         "the next agent run. Honours attempt limits."),
            inputSchema=ID_SCHEMA,
        ),
        types.Tool(
            name="job_validate",
            description="Re-run deterministic acceptance validation against the current repository state.",
            inputSchema=ID_SCHEMA,
        ),
        types.Tool(
            name="job_cancel",
            description=("WRITE: cancel a non-terminal job. State transition only; "
                         "the runner observes cancellation cooperatively."),
            inputSchema=ID_SCHEMA,
        ),
        types.Tool(
            name="job_reconcile",
            description=("Crash recovery: reconcile RUNNING/DISPATCHING jobs whose "
                         "runner is no longer alive into AGENT_STOPPED (recoverable); "
                         "review-aware (REVIEWING with a dead review runner becomes "
                         "REVIEW_INTERRUPTED, retryable)."),
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="job_review",
            description=("WRITE (F.2): dispatch the independent reviewer for a "
                         "production job in WAITING_REVIEW/REVIEW_INTERRUPTED. "
                         "The reviewer model is fixed by policy (no arbitrary "
                         "model selection). Automatic after IMPLEMENTATION_ACCEPTED."),
            inputSchema=ID_SCHEMA,
        ),
        types.Tool(
            name="job_get_review",
            description=("Read the latest review (or a specific review_id) with "
                         "structured findings for a job."),
            inputSchema={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "review_id": {"type": "string"},
                },
                "required": ["job_id"],
            },
        ),
        types.Tool(
            name="job_list_reviews",
            description="List the immutable review history of a job (no bulk text).",
            inputSchema={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "required": ["job_id"],
            },
        ),
    ])


def _call(name, args):
    if name == "job_create":
        try:
            rcfg = supervisor.repo_cfg(args.get("repository") or "")
        except ValueError as exc:
            return {"error": "repository_not_allowlisted", "detail": str(exc)}
        acs = args.get("acceptance_criteria") or []
        if not acs:
            return {"error": "acceptance_criteria_required"}
        # F.1.1-Pi.2 (H1 creation-time): criteria structure is validated up
        # front so a malformed criterion can never silently fail-open later.
        for ac in acs:
            if not ac.get("id") or not isinstance(ac.get("verification"), dict):
                return {"error": "malformed_criterion",
                        "detail": "each criterion needs id + verification{kind,params}"}
            if ac["verification"].get("kind") not in VERIFICATION_KINDS:
                return {"error": "unknown_verification_kind",
                        "allowed": sorted(VERIFICATION_KINDS)}
        cmd = args.get("tests_command")
        if cmd and cmd not in (rcfg.get("test_commands") or []):
            return {"error": "test_command_not_allowlisted",
                    "allowed": rcfg.get("test_commands")}
        # F.1.1-Pi.2 (H1 creation-time correlation): a test_command criterion
        # is only satisfiable when the job registers an (allowlisted) test
        # command. Reject creation instead of guaranteeing a UNMET criterion.
        if any((ac.get("verification") or {}).get("kind") == "test_command"
               for ac in acs) and not cmd:
            return {"error": "test_command_required_for_test_criterion",
                    "detail": "criterion kind=test_command requires job-level "
                              "tests_command (must be allowlisted for the repository)"}
        agent = args.get("agent")
        allowed_agents = ("simulated", "fixture-local-hermes",
                          "pi-sandbox-fixture", "pi-sandbox-claude-code",
                          "pi-sandbox-codex", "pi-sandbox-opencode",
                          "pi-sandbox-hermes-commandcode",
                          "remote-claude-code", "remote-codex")
        if agent and agent not in allowed_agents:
            return {"error": "unknown_agent", "allowed": list(allowed_agents)}
        kind = args.get("kind", "implementation")
        if agent in (None, "simulated", "fixture-local-hermes"):
            if kind != "test":
                return {"error": "local_fixture_is_test_only",
                        "detail": "F.1.1-Pi: production implementation jobs require a "
                                  "sandbox/external worker agent (pi-sandbox-* / remote-*)"}
            if not rcfg.get("allow_simulated"):
                return {"error": "fixture_agent_not_allowed_for_repository"}
        # F.1.1-Pi addendum (section 79): the sandbox fixture agent is a test
        # transport only — never a production implementation worker.
        if agent == "pi-sandbox-fixture" and kind != "test":
            return {"error": "fixture_agent_is_test_only",
                    "detail": "fixture agents run test/transport jobs only; "
                              "production implementation requires a production "
                              "worker agent (e.g. pi-sandbox-hermes-commandcode)"}
        if kind == "implementation" and agent and not (
                agent.startswith("pi-sandbox-") or agent.startswith("remote-")):
            return {"error": "production_requires_sandbox_or_remote_worker"}
        if agent and agent.startswith("remote-") and kind == "test":
            # allow remote agents for tests too (transport E2E)
            pass
        jid = jobs_db.create_job(
            goal=args["goal"], repository=args["repository"],
            acceptance_criteria=acs, agent=agent,
            project_id=args.get("project_id"), task_id=args.get("task_id"),
            kind=args.get("kind", "implementation"),
            max_attempts=args.get("max_attempts", 6),
            tests_command=cmd,
        )
        return {"job_id": jid, "status": "NEW"}

    if name == "job_get":
        job = jobs_db.get_job(args.get("job_id", ""))
        if not job:
            return {"error": "not_found"}
        job["runs"] = jobs_db.runs_for_job(job["job_id"])
        # Section 115: the review surface travels with the job record itself,
        # not only with the job_list brief.
        job.update(_review_surface(job["job_id"], summary_chars=400))
        return job

    if name == "job_list":
        return {"jobs": [_job_brief(j) for j in jobs_db.list_jobs(
            status=args.get("status"), project_id=args.get("project_id"),
            limit=args.get("limit", 50))]}

    if name == "job_status":
        cfg = supervisor.load_config()
        counts = jobs_db.counts_by_status()
        import transport as tmod
        offline = bool(cfg.get("worker", {}).get("offline"))
        online = (not offline) and tmod.worker_online()
        return {
            "jobs_total": sum(counts.values()),
            "by_status": counts,
            "queued": counts.get("QUEUED", 0),
            "running": counts.get("RUNNING", 0),
            "waiting_human": counts.get("WAITING_HUMAN", 0),
            "continue_required": counts.get("CONTINUE_REQUIRED", 0),
            "implementation_accepted": counts.get("IMPLEMENTATION_ACCEPTED", 0),
            "completed": counts.get("COMPLETED", 0),
            "failed": counts.get("FAILED", 0),
            "worker_online": online,
            "worker_transport": "pi-sandbox bridge + local fixture",
            "default_agent": cfg.get("default_agent"),
        }

    if name == "job_dispatch":
        return supervisor.dispatch_job(args.get("job_id", ""))

    if name == "job_continue":
        job = jobs_db.get_job(args.get("job_id", ""))
        if not job:
            return {"error": "not_found"}
        # F.1.1-Pi.2 (M7): FAILED_TO_START is retryable.
        if job["status"] not in ("CONTINUE_REQUIRED", "WAITING_HUMAN", "AGENT_STOPPED",
                                 "QUEUED", "NEW", "INTERRUPTED", "FAILED_TO_START"):
            return {"error": "not_resumable", "status": job["status"]}
        if job["attempt"] >= job["max_attempts"]:
            return {"error": "attempt_limit_reached", "attempt": job["attempt"]}
        jobs_db.update_job(job["job_id"], needs_human=0, failure_reason=None,
                           no_progress_strikes=0)
        return supervisor.dispatch_job(job["job_id"])

    if name == "job_validate":
        job = jobs_db.get_job(args.get("job_id", ""))
        if not job:
            return {"error": "not_found"}
        # F.1.1-Pi.2 (H5): job_validate is adapter-aware. Local deterministic
        # validation reads the LOCAL filesystem — meaningful only for
        # local-fixture jobs. For pi-sandbox/remote jobs the worktree lives on
        # the worker side; a local re-validation would fabricate false FAILs
        # and overwrite worker-produced acceptance evidence. Reject WITHOUT
        # mutating any state.
        import transport as tmod
        try:
            adapter = tmod.get_adapter(job)
        except Exception as exc:  # unconfigured remote etc. — still no mutation
            return {"error": "validation_unavailable",
                    "detail": f"no adapter to validate through: {exc}"[:200]}
        if adapter.name != "local-fixture":
            return {"error": "validation_must_run_via_worker_adapter",
                    "adapter": adapter.name,
                    "detail": "this job runs on a worker adapter; acceptance "
                              "evidence comes from the worker run (job_result "
                              "evidence), not from local filesystem validation"}
        rcfg = supervisor.repo_cfg(job["repository"])
        import validator as vmod
        results, tstat, tsum = vmod.evaluate(
            job, rcfg, rcfg["path"], job.get("git_changed_files") or [])
        jobs_db.update_job(job["job_id"], acceptance_results=results,
                           tests_status=tstat, tests_summary=tsum)
        return {"job_id": job["job_id"], "acceptance_results": results,
                "tests_status": tstat}

    if name == "job_cancel":
        job = jobs_db.get_job(args.get("job_id", ""))
        if not job:
            return {"error": "not_found"}
        if job["status"] in ("COMPLETED", "CANCELLED"):
            return {"error": "terminal", "status": job["status"]}
        jobs_db.transition(job["job_id"], "CANCELLED",
                           failure_reason=args.get("reason") or "cancelled by user/agent")
        return {"job_id": job["job_id"], "status": "CANCELLED"}

    if name == "job_reconcile":
        return {"reconciled": supervisor.reconcile()}

    # ── Phase F.2 review tools (typed only; no arbitrary model/prompt) ────
    if name == "job_review":
        job = jobs_db.get_job(args.get("job_id", ""))
        if not job:
            return {"error": "not_found"}
        if jobs_db.cancel_requested(job["job_id"]):
            return {"error": "cancelled", "job_id": job["job_id"]}
        if job["status"] not in ("IMPLEMENTATION_ACCEPTED",):
            return {"error": "not_reviewable", "status": job["status"]}
        if job.get("kind") != "implementation":
            return {"error": "review_policy_test_jobs_skip_review"}
        rs = (job.get("review_status") or "")
        if rs == "REVIEWING":
            # section 85: exactly one review owner
            return {"error": "already_claimed", "review_status": rs}
        if rs not in ("WAITING_REVIEW", "REVIEW_INTERRUPTED"):
            return {"error": "review_not_dispatchable", "review_status": rs}
        pid, start_ticks = supervisor.read_runner_pid_file(job["job_id"])
        if pid is not None:
            try:
                if supervisor._pid_alive(pid, start_ticks):
                    return {"error": "runner_alive",
                            "detail": "a supervisor runner is already attached"}
            except OSError:
                pass
        # single-owner CAS claim before spawning the runner
        if not jobs_db.cas_review_status(job["job_id"], "REVIEWING",
                                         ("WAITING_REVIEW", "REVIEW_INTERRUPTED")):
            return {"error": "already_claimed",
                    "review_status": jobs_db.get_job(job["job_id"])["review_status"]}
        # spawn the runner; the runner adopts its own pre-claim via pid match
        pid = supervisor._spawn_runner(job["job_id"])
        jobs_db.log(job["job_id"], None, "review",
                    f"review dispatch (job_review) runner pid={pid}")
        return {"job_id": job["job_id"], "review_status": "REVIEWING",
                "runner_pid": pid}

    if name == "job_get_review":
        job_id = args.get("job_id", "")
        if not jobs_db.get_job(job_id):
            return {"error": "not_found"}
        if args.get("review_id"):
            r = jobs_db.get_review(args["review_id"])
            if not r or r.get("job_id") != job_id:
                return {"error": "review_not_found"}
            return _review_brief(r, include_findings=True)
        r = jobs_db.latest_review(job_id)
        if not r:
            return {"error": "no_reviews"}
        return _review_brief(r, include_findings=True)

    if name == "job_list_reviews":
        job_id = args.get("job_id", "")
        if not jobs_db.get_job(job_id):
            return {"error": "not_found"}
        rows = jobs_db.reviews_for_job(job_id, limit=args.get("limit", 20))
        return {"reviews": [_review_brief(r) for r in rows]}

    return {"error": f"unknown_tool:{name}"}


async def _call_tool(ctx=None, params=None):
    try:
        out = _call(params.name, params.arguments or {})
    except Exception as exc:
        out = {"error": f"internal:{type(exc).__name__}", "detail": str(exc)[:200]}
    return types.CallToolResult(content=_text(out))


server = Server("jobs-supervisor", on_list_tools=_list_tools, on_call_tool=_call_tool)


async def main():
    jobs_db.init_db()
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
