#!/usr/bin/env bash
# Phase J.1 rollout — the single root invocation.
#
#   sudo src/coding-handoff/deploy/install.sh \
#        --bridge-config <PATH> \
#        --bridge-socket <PATH> \
#        [--jobs-dir /srv/hermes/hermes-data/jobs] \
#        [--enable-route] [--enable-dispatch] [--no-auto-rollback]
#
# `--bridge-config` and `--bridge-socket` are required and are not defaulted.
# The coding bridge's location differs per host, and installing a guessed path
# then asking the operator to correct it afterwards is how a deployment ends up
# half-configured. Both are validated before anything is written, and the
# resolved values are rendered into private installed files — nothing about
# this host's layout is recorded in the repository.
#
# The broker runs as the account that already owns the job database, resolved
# from that directory. A new service account would need an ACL on a 0700
# directory and on a 0660 socket the bridge recreates on every restart; that
# grant stops working silently the first time the bridge restarts.
#
# By default this installs an **inert** system: the gateway gains the code but
# no configuration section, so it behaves exactly as it does today, and the
# broker runs with dispatch disabled. Each is a separate flag, because
# installing the machinery and letting it start real coding jobs are two
# different decisions.
#
# Every replaced file is backed up under /var/backups/hermes-j1-<timestamp>/
# with its mode and ownership preserved. If health checks fail, the install
# rolls itself back before exiting.
#
# It sends nothing, prints no secret, installs no package and substitutes no
# provider: the agent, the model and the worker image all come from
# configuration that already exists on this host.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
DEPLOY_TOOL="$HERE/deploy_tool.py"
# shellcheck source=lib.sh
. "$HERE/lib.sh"

MANIFEST="$HERE/manifest.json"
UNIT_TEMPLATE="$ROOT/src/coding-handoff/systemd/hermes-coding-handoff.service.in"
UNIT_PATH="/etc/systemd/system/hermes-coding-handoff.service"
BROKER_ROOT="/opt/hermes-coding-handoff"
GATEWAY_DIR="/srv/hermes/channel-gateway"
GATEWAY_CONFIG="$GATEWAY_DIR/config/config.yaml"
SECRET_FILE="/etc/hermes-secrets/coding-handoff-token"
ENDPOINT="http://127.0.0.1:18771"

JOBS_DIR="/srv/hermes/hermes-data/jobs"
BRIDGE_CONFIG=""
BRIDGE_SOCKET=""
ENABLE_ROUTE=0
ENABLE_DISPATCH=0
AUTO_ROLLBACK=1

while [ $# -gt 0 ]; do
  case "$1" in
    --bridge-config)    BRIDGE_CONFIG="${2:-}"; shift 2 ;;
    --bridge-socket)    BRIDGE_SOCKET="${2:-}"; shift 2 ;;
    --jobs-dir)         JOBS_DIR="${2:-}"; shift 2 ;;
    --enable-route)     ENABLE_ROUTE=1; shift ;;
    --enable-dispatch)  ENABLE_DISPATCH=1; shift ;;
    --no-auto-rollback) AUTO_ROLLBACK=0; shift ;;
    -h|--help)          sed -n '2,33p' "$0"; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

need_root
need_cmd python3
need_cmd sha256sum
need_cmd systemctl
need_cmd install
need_cmd stat

[ -n "$BRIDGE_CONFIG" ] || die "--bridge-config is required (there is no correct default)"
[ -n "$BRIDGE_SOCKET" ] || die "--bridge-socket is required (there is no correct default)"
require_abs "$BRIDGE_CONFIG" "--bridge-config"
require_abs "$BRIDGE_SOCKET" "--bridge-socket"
require_abs "$JOBS_DIR" "--jobs-dir"
require_file "$BRIDGE_CONFIG" "--bridge-config"
require_socket "$BRIDGE_SOCKET" "--bridge-socket"
require_dir "$JOBS_DIR" "--jobs-dir"
require_file "$UNIT_TEMPLATE" "the unit template"
if [ "$ENABLE_ROUTE" = "1" ]; then
  require_file "$GATEWAY_CONFIG" "the gateway configuration"
fi

# `--jobs-dir` selects the identity, the broker config and the unit, but the
# job-layer patch targets come from the manifest. Pointing them at different
# directories would patch one job layer and run against another, so a value
# that disagrees with the manifest is refused rather than half-applied.
if ! "$PY" "$DEPLOY_TOOL" manifest-patches "$MANIFEST" \
     | awk -F'\t' -v d="$JOBS_DIR/" '$2 ~ "^"d {n++} END {exit !(n==2)}'; then
  die "--jobs-dir $JOBS_DIR does not match the job-layer targets in the manifest"
fi

BRIDGE_SOCKET_DIR="$(dirname "$BRIDGE_SOCKET")"
JOBS_PARENT="$(dirname "$JOBS_DIR")"
SERVICE_USER="$(stat -c '%U' "$JOBS_DIR")"
SERVICE_GROUP="$(stat -c '%G' "$JOBS_DIR")"
[ "$SERVICE_USER" != "root" ] || die "the jobs directory is root-owned; the broker must not run as root"
[ "$SERVICE_USER" != "UNKNOWN" ] || die "cannot resolve the owner of $JOBS_DIR"

# Refuse while the worker is running something. `supervisor._spawn_runner`
# starts the runner with `start_new_session=True`, which changes its session
# but not its cgroup, so restarting this broker kills a supervisor child it
# owns while the bridge's container keeps going. The supervisor recovers such a
# job (reconcile moves it to AGENT_STOPPED and the bridge re-adopts a live
# container), but an avoidable interruption is still avoidable.
#
# This deliberately does not touch KillMode: loosening it would change how the
# whole unit is torn down, and that is not a decision to make as a side effect
# of an install.
WORKER_STATE="$("$PY" "$DEPLOY_TOOL" bridge-status "$BRIDGE_SOCKET" 2>/dev/null || echo unreachable)"
case "$WORKER_STATE" in
  busy:*)
    die "the worker is $WORKER_STATE. Wait for it to finish before installing." ;;
  idle) : ;;
  *) warn "cannot determine whether the worker is busy ($WORKER_STATE)" ;;
esac

STAMP="$(date -u +%Y%m%dT%H%M%S%NZ)"
BACKUP="/var/backups/hermes-j1-$STAMP"

say "Phase J.1 install"
say "  identity : $SERVICE_USER:$SERVICE_GROUP (resolved from $JOBS_DIR)"
say "  bridge   : $BRIDGE_SOCKET"
say "  backup   : $BACKUP"
say ""

bash "$HERE/preflight.sh" --bridge-config "$BRIDGE_CONFIG" \
     --bridge-socket "$BRIDGE_SOCKET" --jobs-dir "$JOBS_DIR" \
  || die "preflight failed; nothing was written"

abort() {
  trap - EXIT
  say ""
  say "${RED}$1${PLAIN}"
  if [ "$AUTO_ROLLBACK" = "1" ]; then
    say "rolling back automatically…"
    bash "$HERE/rollback.sh" "$BACKUP" || say "${RED}rollback reported problems${PLAIN}"
  else
    say "roll back with: sudo $HERE/rollback.sh $BACKUP"
  fi
  exit 1
}

mkdir "$BACKUP" || die "cannot create a fresh $BACKUP"
chmod 0700 "$BACKUP" || die "cannot protect the backup"
cp "$MANIFEST" "$BACKUP/manifest.json" || die "cannot back up the manifest"
printf '%s\n' "$STAMP" > "$BACKUP/.stamp" || die "cannot stamp the backup"

# Capture state before the first installed file or service changes. Disabled
# and inactive units use nonzero exit codes; unknown states are not accepted.
for unit in channel-gateway.service hermes-coding-handoff.service; do
  enabled="$(systemctl is-enabled "$unit" 2>/dev/null)" || true
  active="$(systemctl is-active "$unit" 2>/dev/null)" || true
  case "$enabled" in enabled|disabled|static|indirect|not-found) : ;; *) die "cannot determine enablement of $unit" ;; esac
  case "$active" in active|inactive|failed|unknown) : ;; *) die "cannot determine activity of $unit" ;; esac
  printf '%s\t%s\t%s\n' "$unit" "$enabled" "$active" >> "$BACKUP/.service-state" \
    || die "cannot save service state"
done

# Every failure after this point, including a helper's exit and an unchecked
# shell command, must take the same rollback path. Explicit abort disarms it.
trap 'rc=$?; if [ "$rc" -ne 0 ]; then abort "install failed (exit $rc)"; fi' EXIT
set -e

# ------------------------------------------------------------ directories
# The broker owns its state. Its code, config and specifications are root-owned
# and readable by the service group — the service must not be able to rewrite
# its own code or widen its own registry.
install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0750 "$BROKER_ROOT/state"
install -d -o root -g "$SERVICE_GROUP" -m 0750 "$BROKER_ROOT/app"
install -d -o root -g "$SERVICE_GROUP" -m 0750 "$BROKER_ROOT/config"
install -d -o root -g "$SERVICE_GROUP" -m 0750 "$BROKER_ROOT/specs"

# ------------------------------------------------------------------ files
say "installing files"
read_lines INSTALL_ROWS manifest_files "$MANIFEST" all \
  || abort "cannot enumerate the manifest"
while IFS=$'\t' read -r source target mode payload baseline; do
  [ -n "$source" ] || continue
  install_file "$ROOT/$source" "$target" "$mode" "$BACKUP" || abort "could not install $target"
  ok "$target"
done <<< "$INSTALL_ROWS"

chown -R root:"$SERVICE_GROUP" "$BROKER_ROOT/app" "$BROKER_ROOT/config" "$BROKER_ROOT/specs"
chmod -R go-w "$BROKER_ROOT/app" "$BROKER_ROOT/config" "$BROKER_ROOT/specs"

# ------------------------------------------------------------ job patches
#
# The job layer is transformed in place rather than copied. The deployed files
# are logically identical to the committed ones but carry CRLF and, in
# transport.py, this host's own resolved bridge path. Copying over that would
# reinstate a path that does not exist here and break the container consumers
# that rely on the fallback.
say ""
say "job layer (patched in place, not replaced)"
read_lines PATCH_ROWS tool manifest-patches "$MANIFEST"   || abort "cannot enumerate the job-layer patches"
PATCHED_ANY=0
while IFS=$'\t' read -r kind target live note; do
  [ -n "$kind" ] || continue
  # Preflight already classified every target as fresh or already-applied and
  # refused otherwise, so anything unexpected here is fatal rather than a
  # warning to step over.
  [ -f "$target" ] || abort "job layer target vanished since preflight: $target"
  mkdir -p "$BACKUP/$(dirname "${target#/}")"
  backup_file "$target" "$BACKUP/${target#/}" || abort "cannot back up $target"
  tmp="$(mktemp "$(dirname "$target")/.j1-XXXXXX")" || abort "cannot stage $target"
  if ! tool patch-jobs "$kind" "$target" "$tmp" "$live" >/dev/null; then
    rm -f "$tmp"
    abort "$kind refused $target"
  fi
  if [ -s "$tmp" ]; then
    chmod --reference="$target" "$tmp"
    chown --reference="$target" "$tmp" 2>/dev/null || true
    mv -f "$tmp" "$target" || { rm -f "$tmp"; abort "cannot write $target"; }
    ok "$kind applied: $target"
  else
    # `patch-jobs` writes nothing when the patch is already in place, which is
    # what a second run of the documented two-stage rollout looks like.
    rm -f "$tmp"
    ok "$kind already applied: $target"
  fi
  PATCHED_ANY=$((PATCHED_ANY + 1))
done <<< "$PATCH_ROWS"

# Dispatch needs the deterministic job id the patch adds; without it a
# redelivered authorization could produce a second job.
if [ "$ENABLE_DISPATCH" = "1" ] && [ "$PATCHED_ANY" -lt 2 ]; then
  abort "--enable-dispatch requires both job-layer patches; $PATCHED_ANY applied"
fi

# ----------------------------------------------------------------- secret
say ""
say "credential"
if [ ! -f "$SECRET_FILE" ]; then
  install -d -m 0700 -o root -g root /etc/hermes-secrets
  umask 077
  # Written straight into a 0400 root file. Never printed, never placed in an
  # environment variable, never passed on a command line.
  "$PY" -c 'import secrets,sys; sys.stdout.write(secrets.token_hex(32))' \
    > "$SECRET_FILE" || abort "cannot write the broker credential"
  chmod 0400 "$SECRET_FILE"
  chown root:root "$SECRET_FILE"
  ok "generated $SECRET_FILE (not displayed)"
else
  ok "credential already present (left untouched)"
fi

BROKER_TOKEN="$BROKER_ROOT/state/broker.token"
install -m 0400 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$SECRET_FILE" "$BROKER_TOKEN" \
  || abort "cannot install the broker credential"
ok "broker credential installed (not displayed)"

# ----------------------------------------------------------------- config
BROKER_CONFIG="$BROKER_ROOT/config/handoff.json"
REGISTRY="$BROKER_ROOT/config/projects.json"

if [ -f "$BROKER_CONFIG" ]; then
  backup_file "$BROKER_CONFIG" "$BACKUP/handoff.json" || abort "cannot back up broker configuration"
fi
say ""
say "broker configuration (live resolved paths)"
tool render-config "$BROKER_CONFIG" \
  "token_file=$BROKER_TOKEN" \
  "registry_file=$REGISTRY" \
  "jobs_dir=$JOBS_DIR" \
  "bridge_config=$BRIDGE_CONFIG" \
  "bridge_socket=$BRIDGE_SOCKET" \
  "ledger_db=$BROKER_ROOT/state/handoff-ledger.db" \
  "dispatch_enabled=$ENABLE_DISPATCH" >/dev/null || abort "cannot write the broker configuration"
chmod 0640 "$BROKER_CONFIG"
chown root:"$SERVICE_GROUP" "$BROKER_CONFIG"
ok "wrote $BROKER_CONFIG"

if [ ! -f "$REGISTRY" ]; then
  cp "$BROKER_ROOT/config/projects.sample.json" "$REGISTRY"
  chmod 0640 "$REGISTRY"
  chown root:"$SERVICE_GROUP" "$REGISTRY"
  ok "seeded $REGISTRY from the sample"
else
  ok "project registry already present (left untouched)"
fi

tool check-registry "$BROKER_ROOT/app" "$REGISTRY" \
  || abort "the project registry is invalid; the broker was not started"
ok "project registry parses"

# ------------------------------------------------------------------- unit
say ""
say "unit"
if [ -f "$UNIT_PATH" ]; then
  mkdir -p "$BACKUP/$(dirname "${UNIT_PATH#/}")"
  backup_file "$UNIT_PATH" "$BACKUP/${UNIT_PATH#/}" || abort "cannot back up the unit"
else
  mkdir -p "$BACKUP/.absent/$(dirname "${UNIT_PATH#/}")"
  : > "$BACKUP/.absent/${UNIT_PATH#/}"
fi
# Repository read access. Resolved from the bridge registry rather than
# assumed: on this host the checkouts live under the operator's home, which
# ProtectHome=tmpfs hides, and a broker that cannot stat a checkout reports
# every project as missing.
read_lines REPO_ROOTS tool bridge-repo-binds "$BRIDGE_CONFIG"   || abort "cannot resolve the repository paths to expose"
REPO_BINDS=""
while IFS= read -r repo_root; do
  [ -n "$repo_root" ] || continue
  REPO_BINDS="${REPO_BINDS}BindReadOnlyPaths=${repo_root}"$'\n'
  ok "repository bind: $repo_root"
done <<< "$REPO_ROOTS"
[ -n "$REPO_BINDS" ] || abort "the bridge registry lists no repository paths"

tool render-unit "$UNIT_TEMPLATE" "$UNIT_PATH" \
  "REPO_BINDS=${REPO_BINDS%$'\n'}" \
  "JOBS_PARENT=$JOBS_PARENT" \
  "SERVICE_USER=$SERVICE_USER" \
  "SERVICE_GROUP=$SERVICE_GROUP" \
  "STATE_DIR=$BROKER_ROOT/state" \
  "APP_DIR=$BROKER_ROOT/app" \
  "CONFIG_DIR=$BROKER_ROOT/config" \
  "CONFIG_FILE=$BROKER_CONFIG" \
  "SPEC_DIR=$BROKER_ROOT/specs" \
  "JOBS_DIR=$JOBS_DIR" \
  "BRIDGE_SOCKET=$BRIDGE_SOCKET" \
  "BRIDGE_SOCKET_DIR=$BRIDGE_SOCKET_DIR" \
  "BRIDGE_CONFIG=$BRIDGE_CONFIG" \
  "GATEWAY_DIR=$GATEWAY_DIR" >/dev/null || abort "cannot render the unit"
chmod 0644 "$UNIT_PATH"
ok "rendered $UNIT_PATH"

# ------------------------------------------------------- identity access
say ""
say "identity access (before starting anything)"
check_access_as "$SERVICE_USER" "$JOBS_DIR" -w "write the job layer"
check_access_as "$SERVICE_USER" "$BRIDGE_SOCKET" -w "connect the bridge socket"
check_access_as "$SERVICE_USER" "$BRIDGE_CONFIG" -r "read the bridge registry"
check_access_as "$SERVICE_USER" "$REGISTRY" -r "read the project registry"
check_access_as "$SERVICE_USER" "$BROKER_ROOT/specs" -r "read the specifications"
check_access_as "$SERVICE_USER" "$BROKER_ROOT/state" -w "write its ledger"
# supervisor.py imports security_patterns from the directory above jobs/.
if [ -f "$JOBS_PARENT/security_patterns.py" ]; then
  check_access_as "$SERVICE_USER" "$JOBS_PARENT/security_patterns.py" -r \
    "read security_patterns.py (imported by the supervisor)"
else
  bad "security_patterns.py not found beside $JOBS_DIR; dispatch would fail at import"
fi
while IFS= read -r repo_root; do
  [ -n "$repo_root" ] || continue
  check_access_as "$SERVICE_USER" "$repo_root" -r "read repositories under"
done <<< "$REPO_ROOTS"
[ "$FAILURES" -eq 0 ] || abort "the service identity cannot reach what it needs"

# ---------------------------------------------------------------- gateway
if [ "$ENABLE_ROUTE" = "1" ]; then
  say ""
  say "enabling the gateway route"
  require_file "$GATEWAY_CONFIG" "the gateway configuration"
  backup_file "$GATEWAY_CONFIG" "$BACKUP/config.yaml" || abort "cannot back up gateway configuration"
  tool enable-gateway-route "$GATEWAY_CONFIG" "$ENDPOINT" \
    || abort "cannot update the gateway configuration"
  ok "gateway configuration updated"
fi

# ------------------------------------------------------------------ start
say ""
say "starting"
systemctl daemon-reload || abort "systemctl daemon-reload failed"
systemctl enable --now hermes-coding-handoff.service \
  || abort "the broker did not start (journalctl -u hermes-coding-handoff)"
# `enable --now` starts a stopped service and does nothing to a running one, so
# a second run that rewrote the config and the unit would leave the old process
# serving the old settings. Restart unconditionally: the broker holds no
# in-memory state that a restart loses — the ledger is on disk.
systemctl restart hermes-coding-handoff.service \
  || abort "the broker did not restart (journalctl -u hermes-coding-handoff)"
ok "hermes-coding-handoff.service is running the rendered configuration"

if [ "$ENABLE_ROUTE" = "1" ]; then
  systemctl restart channel-gateway.service || abort "the gateway did not restart"
  ok "channel-gateway.service restarted"
else
  warn "gateway not restarted: the route was not enabled (--enable-route)"
fi

# ----------------------------------------------------------------- health
say ""
# The health check is told what was asked for, so it can say whether the
# running process actually reflects it rather than merely being alive.
bash "$HERE/health.sh" --expect-dispatch "$ENABLE_DISPATCH" \
  --expect-route "$ENABLE_ROUTE" || abort "health checks failed"

say ""
say "${GREEN}install complete${PLAIN}"
say "backup:   $BACKUP"
say "rollback: sudo $HERE/rollback.sh $BACKUP"
[ "$ENABLE_ROUTE" = "1" ] || say "note: the gateway route is installed but not enabled"
[ "$ENABLE_DISPATCH" = "1" ] || say "note: the broker will refuse to dispatch until dispatch is enabled"
trap - EXIT
