#!/usr/bin/env bash
# Phase J.1 rollout — read-only verification.
#
#   preflight.sh --bridge-config PATH --bridge-socket PATH
#                [--jobs-dir PATH] [--gateway-app PATH] [--secret-dir PATH]
#
# Changes nothing. Run it as the operator before asking for sudo, and again as
# root immediately before installing — install.sh calls it itself and refuses
# to continue if it fails.
#
# What it proves:
#   * the package is intact (every payload file hashes to the manifest)
#   * the host is what the package was prepared against (every target holds its
#     recorded baseline, and the drifted Antigravity files are untouched)
#   * the bridge config and socket the operator named actually exist and are
#     the right kind of thing
#   * the identity that will run the broker can reach everything it needs
#
# Exit 0 means an install would be applying known content to known content.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
DEPLOY_TOOL="$HERE/deploy_tool.py"
# shellcheck source=lib.sh
. "$HERE/lib.sh"

MANIFEST="$HERE/manifest.json"
JOBS_DIR="/srv/hermes/hermes-data/jobs"
# Defaults are this host's layout; every one of them can be named explicitly,
# for the same reason the bridge paths must be. A check that only ever looks at
# a hard-coded path cannot be exercised anywhere but production.
GATEWAY_APP="/srv/hermes/channel-gateway/app"
SECRET_DIR="/etc/hermes-secrets"
BRIDGE_CONFIG=""
BRIDGE_SOCKET=""

while [ $# -gt 0 ]; do
  case "$1" in
    --bridge-config) BRIDGE_CONFIG="${2:-}"; shift 2 ;;
    --bridge-socket) BRIDGE_SOCKET="${2:-}"; shift 2 ;;
    --jobs-dir)      JOBS_DIR="${2:-}"; shift 2 ;;
    --gateway-app)   GATEWAY_APP="${2:-}"; shift 2 ;;
    --secret-dir)    SECRET_DIR="${2:-}"; shift 2 ;;
    -h|--help)       sed -n '2,20p' "$0"; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

[ -f "$MANIFEST" ] || die "manifest not found: $MANIFEST"
[ -f "$DEPLOY_TOOL" ] || die "deploy_tool.py not found next to this script"

need_cmd python3
need_cmd sha256sum
need_cmd stat

say "Phase J.1 preflight"
say ""
say "manifest"
verify_manifest "$MANIFEST"

say ""
say "package integrity"
verify_payload "$MANIFEST" "$ROOT"

say ""
say "host baselines (fail closed)"
verify_baselines "$MANIFEST"

say ""
say "job layer (patched in place, checked before any write)"
verify_job_patches "$MANIFEST"

say ""
say "deployed files this change must not touch"
verify_drift "$MANIFEST"

say ""
say "paths"
require_abs "$JOBS_DIR" "--jobs-dir"
if [ -d "$JOBS_DIR" ]; then ok "jobs directory: $JOBS_DIR"; else bad "missing: $JOBS_DIR"; fi
if [ -d "$GATEWAY_APP" ]; then ok "gateway app: $GATEWAY_APP"; else bad "missing: $GATEWAY_APP"; fi
if [ -d "$SECRET_DIR" ]; then ok "secret store present"; else bad "missing: $SECRET_DIR"; fi

# The bridge is named, never guessed. An earlier draft defaulted these to a
# path that does not exist on this host.
if [ -z "$BRIDGE_CONFIG" ] || [ -z "$BRIDGE_SOCKET" ]; then
  bad "--bridge-config and --bridge-socket are required (no default is correct here)"
else
  require_abs "$BRIDGE_CONFIG" "--bridge-config"
  require_abs "$BRIDGE_SOCKET" "--bridge-socket"
  if [ -f "$BRIDGE_CONFIG" ]; then
    if "$PY" -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); sys.exit(0 if d.get("repositories") and d.get("agents") else 1)' "$BRIDGE_CONFIG"; then
      ok "bridge config parses and has repositories + agents: $BRIDGE_CONFIG"
    else
      bad "bridge config has no repositories/agents: $BRIDGE_CONFIG"
    fi
  else
    bad "bridge config not found: $BRIDGE_CONFIG"
  fi
  if [ -S "$BRIDGE_SOCKET" ]; then
    ok "bridge socket present: $BRIDGE_SOCKET"
    # A restart of the broker kills a supervisor runner it spawned — the
    # runner gets its own session but stays in the same cgroup. Installing
    # while work is in flight is therefore avoidable interference.
    worker_state="$(tool bridge-status "$BRIDGE_SOCKET" 2>/dev/null || echo unreachable)"
    case "$worker_state" in
      idle) ok "worker is idle" ;;
      busy:*) bad "worker is $worker_state; installing now would interrupt it" ;;
      *) warn "cannot determine whether the worker is busy ($worker_state)" ;;
    esac
  else
    bad "bridge socket not found (is the bridge running?): $BRIDGE_SOCKET"
  fi
  if [ -f "$BRIDGE_CONFIG" ]; then
    # Readiness is observed by stat-ing these and running `git rev-parse` in
    # them, so the unit has to bind them back in under ProtectHome=tmpfs.
    binds=0
    if read_lines repo_roots tool bridge-repo-binds "$BRIDGE_CONFIG"; then
      while IFS= read -r repo_root; do
        [ -n "$repo_root" ] || continue
        binds=$((binds + 1))
        if [ -d "$repo_root" ]; then
          ok "repository tree readable: $repo_root"
        else
          bad "repository tree missing: $repo_root"
        fi
      done <<< "$repo_roots"
    fi
    [ "$binds" -gt 0 ] || bad "the bridge registry lists no repository paths"
  fi
fi

# supervisor.py inserts jobs/'s parent on sys.path and imports
# security_patterns from there; without it every dispatch fails at import.
if [ -f "$(dirname "$JOBS_DIR")/security_patterns.py" ]; then
  ok "security_patterns.py present beside the job layer"
else
  bad "security_patterns.py not found beside $JOBS_DIR"
fi

say ""
say "service identity"
# Derived, not invented: the broker runs as the account that already owns the
# job database, because that is the account the bridge socket is group-owned
# by and the only one that can reach a 0700 jobs directory without an ACL that
# the bridge would erase on its next restart.
if [ -d "$JOBS_DIR" ]; then
  SERVICE_USER="$(stat -c '%U' "$JOBS_DIR")"
  SERVICE_GROUP="$(stat -c '%G' "$JOBS_DIR")"
  if [ "$SERVICE_USER" = "root" ]; then
    bad "the jobs directory is root-owned; the broker must not run as root"
  else
    ok "will run as the existing jobs owner (resolved from $JOBS_DIR)"
    if [ "$(id -u)" = "0" ]; then
      check_access_as "$SERVICE_USER" "$JOBS_DIR" -w "write the job layer"
      # Container-created SQLite files and runtime directories may be owned
      # by root even when jobs/ belongs to the broker account. Directory
      # access alone does not prove that an existing database is writable.
      for name in jobs.db jobs.db-wal jobs.db-shm logs artifacts prompts; do
        if [ -e "$JOBS_DIR/$name" ]; then
          check_access_as "$SERVICE_USER" "$JOBS_DIR/$name" -r "read job runtime state"
          check_access_as "$SERVICE_USER" "$JOBS_DIR/$name" -w "write job runtime state"
          if [ -d "$JOBS_DIR/$name" ]; then
            check_access_as "$SERVICE_USER" "$JOBS_DIR/$name" -x "traverse job runtime directory"
          fi
        fi
      done
      check_access_as "$SERVICE_USER" "$(dirname "$JOBS_DIR")/security_patterns.py" -r         "read security_patterns.py"
      [ -n "$BRIDGE_SOCKET" ] && [ -S "$BRIDGE_SOCKET" ] && \
        check_access_as "$SERVICE_USER" "$BRIDGE_SOCKET" -w "connect the bridge socket"
      [ -n "$BRIDGE_CONFIG" ] && [ -f "$BRIDGE_CONFIG" ] && \
        check_access_as "$SERVICE_USER" "$BRIDGE_CONFIG" -r "read the bridge registry"
    else
      warn "run as root to verify the identity's access"
    fi
  fi
fi

say ""
say "environment"
if command -v systemctl >/dev/null 2>&1; then
  if systemctl cat channel-gateway.service >/dev/null 2>&1; then
    ok "unit present: channel-gateway.service"
  else
    bad "unit not found: channel-gateway.service"
  fi
else
  warn "systemctl unavailable; unit checks skipped"
fi

if "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  ok "python3 is new enough for the broker"
else
  bad "python3 is older than 3.9"
fi

say ""
if [ "$FAILURES" -eq 0 ]; then
  say "${GREEN}preflight passed${PLAIN} — the host matches this package"
  exit 0
fi
say "${RED}preflight failed: $FAILURES problem(s)${PLAIN} — do not install"
exit 1
