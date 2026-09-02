#!/usr/bin/env bash
# Worker-side Independent Reviewer runner (Hermes CLI one-shot, Phase F.2).
# Bridge mounts this read-only at /opt/workerbin/run_hermes_reviewer.sh.
#
# Reviewer profile — INDEPENDENT from the implementation worker:
#   - HERMES_HOME is reviewer-local (/agent-home/reviewer-hermes): the
#     implementation agent-home is NOT mounted into this container at all.
#   - Provider/model are pinned below; there is NO fallback provider/model.
#     Reviewer identity is fixed: CommandCode / meta/muse-spark-1.2
#     (STANDARD variant). This is a deliberate, user-confirmed deviation from
#     the spec's meta/muse-spark-1.2-contributor, on data-policy grounds —
#     NOT a fallback: both ids are present in the live CommandCode /models
#     listing (measured), so the spec model is available. See the note at
#     REVIEWER_MODEL in jobs/review.py, which is the single source of truth.
#   - Non-interactive: -z one-shot; the read-only reviewer config pins
#     approvals.mode=off + deny rules. No --yolo flag.
#   - Credential: COMMANDCODE_API_KEY arrives via container env from the
#     bridge (runtime only); never persisted, never logged.
#   - Output contract: the reviewer writes /jobs/review-result.json.
set -u
export HERMES_HOME=/agent-home/reviewer-hermes
export HOME=/agent-home
export HERMES_INTERACTIVE=0

PROMPT_FILE=/jobs/prompt.txt
if [ ! -r "$PROMPT_FILE" ]; then
  echo "run_hermes_reviewer: prompt file missing: $PROMPT_FILE" >&2
  exit 2
fi

# Review cycle for the per-cycle usage file (bridge writes state.json first).
cycle=""
if [ -r /jobs/state.json ]; then
  cycle=$(python3 -c "import json;print(json.load(open('/jobs/state.json')).get('cycle') or '')" 2>/dev/null || true)
fi
if [ -z "$cycle" ]; then
  cycle=$(( $(ls /jobs/review-usage-*.json 2>/dev/null | wc -l) + 1 ))
fi
USAGE_FILE="/jobs/review-usage-${cycle}.json"

# reviewer isolation probe battery.
# Presence probes (no personal data mounts) plus an ACTIVE write-attempt
# battery: section 98 asks for a measured "the reviewer cannot mutate the
# implementation worktree", not an assumed one. Every write target below is
# outside this container's mounts, so each attempt must fail; the failure mode
# is recorded. The one write that IS expected to succeed goes into the
# ephemeral /workspace snapshot -- the host side then verifies it never
# propagated to the implementation worktree.
python3 - <<'PYEOF' > /jobs/reviewer-probes.json 2>/dev/null || true
import json, os
r = {}
r["absent_opt_data"] = not os.path.exists("/opt/data")
r["absent_srv_hermes"] = not os.path.exists("/srv/hermes")
r["absent_home_<PI_USER>"] = not os.path.exists("/home/<PI_USER>")
r["absent_impl_agent_home"] = not os.path.exists("/agent-home/hermes")
r["absent_docker_sock"] = not os.path.exists("/var/run/docker.sock")
r["write_tmp"] = os.access("/tmp", os.W_OK)

targets = {
    "impl_worktrees_root": "/home/<PI_USER>/coding-worker/worktrees/probe.txt",
    "impl_repos_root": "/home/<PI_USER>/coding-worker/repos/probe.txt",
    "impl_agent_home": "/agent-home/hermes/probe.txt",
    "personal_hermes_data": "/opt/data/probe.txt",
    "personal_srv_hermes": "/srv/hermes/probe.txt",
    "host_root": "/probe.txt",
}
attempts = {}
for name, path in targets.items():
    try:
        with open(path, "w") as fh:
            fh.write("reviewer-write-probe")
        attempts[name] = "WROTE"        # must never happen
    except OSError as exc:
        attempts[name] = type(exc).__name__
    except Exception as exc:            # noqa: BLE001
        attempts[name] = type(exc).__name__
r["write_attempts"] = attempts
r["write_attempts_all_failed"] = all(v != "WROTE" for v in attempts.values())

# expected-to-succeed, discarded-after-review write into the snapshot
try:
    with open("/workspace/.reviewer-write-probe", "w") as fh:
        fh.write("discarded after review (section 17/98)")
    r["snapshot_write"] = "WROTE"
except OSError as exc:
    r["snapshot_write"] = type(exc).__name__
print(json.dumps(r))
PYEOF

PROMPT=$(cat "$PROMPT_FILE")

cd /workspace
exec /opt/hermes/.venv/bin/hermes -z "$PROMPT" \
  --provider commandcode \
  --model meta/muse-spark-1.2 \
  --usage-file "$USAGE_FILE"
