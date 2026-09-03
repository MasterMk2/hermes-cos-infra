#!/usr/bin/env bash
# ============================================================================
# Discord Knowledge Collector - privileged install.
#
#   sudo bash install-root.sh [--owner-user NAME] [--dry-run] [--uninstall]
#
# Idempotent: safe to re-run. It creates the service account, the secret
# directory, the data tree and the systemd unit, and then STOPS - it never
# starts the service, because a fresh install still has a placeholder token
# and an empty guild allowlist.
#
# It never prints, reads or overwrites a token value.
# It never touches /srv/hermes/hermes-data.
# ============================================================================
set -euo pipefail

SERVICE_USER="discordknowledge"
SERVICE_GROUP="discordknowledge"
# The operator account that owns the code and config. Defaults to whoever
# invoked sudo, so no account name is hard-coded into this file; override with
# --owner-user when installing on behalf of a different account.
OWNER_USER="${SUDO_USER:-}"
DRY_RUN=0
UNINSTALL=0

UNIT_NAME="discord-knowledge.service"
UNIT_DEST="/etc/systemd/system/${UNIT_NAME}"
SECRET_DIR="/etc/hermes-secrets"
TOKEN_FILE="${SECRET_DIR}/discord-knowledge-token"
MOUNT_ROOT="/srv/hermes"
APP_ROOT="${MOUNT_ROOT}/discord-knowledge"
KNOWLEDGE_PARENT="${MOUNT_ROOT}/knowledge"
DATA_DIR="${KNOWLEDGE_PARENT}/discord"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_SRC="${SCRIPT_DIR}/systemd/${UNIT_NAME}"

CHANGES=()

# ---------------------------------------------------------------- helpers --

die()    { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
say()    { printf '%s\n' "$*"; }
head1()  { printf '\n== %s ==\n' "$*"; }
record() { CHANGES+=("$*"); }

# Every mutation goes through run(). --dry-run prints and does not execute;
# read-only checks stay live so the printed plan reflects the real system.
run() {
  if (( DRY_RUN )); then
    printf '  [dry-run] %s\n' "$*"
  else
    printf '  + %s\n' "$*"
    "$@"
  fi
}

usage() {
  cat <<'USAGE'
Usage: sudo bash install-root.sh [OPTIONS]

  --owner-user NAME   Operator account that owns the code and config
                      (default: the account that invoked sudo). Must already
                      exist, together with its like-named primary group.
  --dry-run           Print every action without performing it.
  --uninstall         Stop, disable and remove the systemd unit only.
                      Keeps the token, the databases, the data directories
                      and the service account.
  -h, --help          This text.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --owner-user)   OWNER_USER="${2:-}"; [[ -n "$OWNER_USER" ]] || die "--owner-user needs a value"; shift 2 ;;
    --owner-user=*) OWNER_USER="${1#*=}"; [[ -n "$OWNER_USER" ]] || die "--owner-user needs a value"; shift ;;
    --dry-run)      DRY_RUN=1; shift ;;
    --uninstall)    UNINSTALL=1; shift ;;
    -h|--help)      usage; exit 0 ;;
    *)              usage >&2; die "unknown argument: $1" ;;
  esac
done

if [[ "$(id -u)" -ne 0 ]]; then
  say "ERROR: this installer changes /etc and /srv and must run as root." >&2
  say "" >&2
  say "  sudo bash install-root.sh" >&2
  say "" >&2
  say "(--dry-run also requires root: it inspects root-only paths.)" >&2
  exit 1
fi

# Directory in the managed list: must exist with exactly this ownership and
# mode. Only what actually differs is touched, so a re-run reports "nothing
# changed" and a half-finished earlier attempt self-heals.
ensure_dir() {
  local path="$1" owner="$2" group="$3" mode="$4"
  local want_mode cur_owner cur_mode
  want_mode="$(printf '%o' "$((8#${mode}))")"   # 0755 -> 755, 2770 -> 2770

  if [[ ! -d "$path" ]]; then
    run mkdir -p "$path"
    # mkdir honours umask, so mode is always set explicitly afterwards.
    run chown "${owner}:${group}" "$path"
    run chmod "$mode" "$path"
    record "created ${path} (${owner}:${group} ${want_mode})"
    return
  fi

  cur_owner="$(stat -c '%U:%G' "$path")"
  cur_mode="$(stat -c '%a' "$path")"
  if [[ "$cur_owner" == "${owner}:${group}" && "$cur_mode" == "$want_mode" ]]; then
    say "  = ok: ${path} (${cur_owner} ${cur_mode})"
    return
  fi

  say "  ! ${path} is ${cur_owner} ${cur_mode}, want ${owner}:${group} ${want_mode}"
  if [[ "$cur_owner" != "${owner}:${group}" ]]; then
    run chown "${owner}:${group}" "$path"
    record "chown ${path}: ${cur_owner} -> ${owner}:${group}"
  fi
  if [[ "$cur_mode" != "$want_mode" ]]; then
    run chmod "$mode" "$path"
    record "chmod ${path}: ${cur_mode} -> ${want_mode}"
  fi
}

# Parent directory OUTSIDE the managed list: create it if missing, but never
# re-own or re-mode one that already exists.
ensure_parent() {
  local path="$1" owner="$2" group="$3" mode="$4"
  if [[ -d "$path" ]]; then
    say "  = exists, left untouched: ${path} ($(stat -c '%U:%G %a' "$path"))"
    return
  fi
  run mkdir -p "$path"
  run chown "${owner}:${group}" "$path"
  run chmod "$mode" "$path"
  record "created parent directory ${path} (${owner}:${group} $(printf '%o' "$((8#${mode}))"))"
}

# ------------------------------------------------------------- uninstall ---

if (( UNINSTALL )); then
  head1 "Uninstall (unit only)"
  if systemctl is-active --quiet "$UNIT_NAME"; then
    run systemctl stop "$UNIT_NAME"
    record "stopped ${UNIT_NAME}"
  else
    say "  = not running: ${UNIT_NAME}"
  fi
  if systemctl is-enabled --quiet "$UNIT_NAME" 2>/dev/null; then
    run systemctl disable "$UNIT_NAME"
    record "disabled ${UNIT_NAME}"
  else
    say "  = not enabled: ${UNIT_NAME}"
  fi
  if [[ -e "$UNIT_DEST" ]]; then
    run rm -f "$UNIT_DEST"
    record "removed ${UNIT_DEST}"
  else
    say "  = absent: ${UNIT_DEST}"
  fi
  run systemctl daemon-reload
  run systemctl reset-failed "$UNIT_NAME" || true

  head1 "Deliberately KEPT (this script never deletes data)"
  cat <<KEPT
  ${TOKEN_FILE}
  ${SECRET_DIR}
  ${APP_ROOT}                (code, config, venv, state, logs)
  ${DATA_DIR}                (messages.db and its WAL)
  ${MOUNT_ROOT}/hermes-data  (never touched by this script at all)
  user/group ${SERVICE_USER} (deleting it would orphan the ownership above)

  To remove those, do it by hand, deliberately.
KEPT
  head1 "Changes made"
  if [[ ${#CHANGES[@]} -eq 0 ]]; then
    say "  (none - already uninstalled)"
  else
    printf '  - %s\n' "${CHANGES[@]}"
  fi
  exit 0
fi

# ------------------------------------------------------------ preflight ----

head1 "Preflight"
(( DRY_RUN )) && say "  MODE: dry-run - nothing will be changed."

[[ -n "$OWNER_USER" ]] \
  || die "cannot determine the operator account (SUDO_USER is unset); pass --owner-user NAME"
[[ "$OWNER_USER" != "root" ]] \
  || die "the operator account must not be root; pass --owner-user NAME"
getent passwd "$OWNER_USER" >/dev/null \
  || die "owner user '${OWNER_USER}' does not exist (use --owner-user NAME)"
getent group "$OWNER_USER" >/dev/null \
  || die "group '${OWNER_USER}' does not exist; the operator's primary group is expected to match the username"
say "  owner user/group: ${OWNER_USER}:${OWNER_USER}"

# The data tree lives on the USB disk. Installing while it is unmounted would
# populate the SD card underneath the mount point, and the service would later
# read an empty database. Refuse instead.
findmnt -rn "$MOUNT_ROOT" >/dev/null \
  || die "${MOUNT_ROOT} is not a mount point right now - mount the USB disk first, then re-run"
say "  ${MOUNT_ROOT} is mounted: $(findmnt -rno SOURCE,FSTYPE "$MOUNT_ROOT")"

[[ -f "$UNIT_SRC" ]] || die "unit file not found next to this script: ${UNIT_SRC}"
say "  unit source: ${UNIT_SRC}"

# ---------------------------------------------------------- service user ---

head1 "Service account"
if getent group "$SERVICE_GROUP" >/dev/null; then
  say "  = group exists: ${SERVICE_GROUP}"
else
  run groupadd --system "$SERVICE_GROUP"
  record "created system group ${SERVICE_GROUP}"
fi

# No login shell, no home of its own (APP_ROOT is the working directory only),
# and NO supplementary groups. In particular NOT docker, NOT sudo, NOT adm:
# membership in docker is equivalent to root on this host, which would hand the
# collector the assistant's own credentials and every other container's data.
if getent passwd "$SERVICE_USER" >/dev/null; then
  say "  = user exists: ${SERVICE_USER}"
else
  run useradd \
    --system \
    --gid "$SERVICE_GROUP" \
    --home-dir "$APP_ROOT" \
    --no-create-home \
    --shell /usr/sbin/nologin \
    --comment "Discord Knowledge Collector" \
    "$SERVICE_USER"
  record "created system user ${SERVICE_USER} (nologin, no home, no supplementary groups)"
fi

# Assertion, not decoration: if an earlier hand-edit ever added the collector
# to a privileged group, stop here rather than install on top of it.
# Deliberately a plain loop, not `id -nG | grep -q`: under `set -o pipefail` a
# SIGPIPE on the writer can mask grep's match and make this check fail OPEN.
if getent passwd "$SERVICE_USER" >/dev/null; then
  SERVICE_GROUPS="$(id -nG "$SERVICE_USER")"
  for g in $SERVICE_GROUPS; do
    case "$g" in
      docker|sudo|adm|root|staff|wheel)
        die "${SERVICE_USER} is in the privileged group '${g}'. Remove it (gpasswd -d ${SERVICE_USER} ${g}) and re-run; docker membership in particular is equivalent to root on this host."
        ;;
    esac
  done
  say "  groups: ${SERVICE_GROUPS}"
fi

# --------------------------------------------------------------- secrets ---

head1 "Secret"
ensure_dir "$SECRET_DIR" root root 0700

if [[ -e "$TOKEN_FILE" ]]; then
  say "  = token file already present: ${TOKEN_FILE}"
  say "    (refusing to overwrite; contents are neither read nor printed)"
  say "    current: $(stat -c '%U:%G %a' "$TOKEN_FILE")"
else
  # Created at 0400 root:root in one step - never a world-readable window.
  run install -m 0400 -o root -g root /dev/stdin "$TOKEN_FILE" <<'PLACEHOLDER'
REPLACE_THIS_LINE_WITH_THE_DISCORD_BOT_TOKEN
PLACEHOLDER
  record "created ${TOKEN_FILE} (root:root 0400, placeholder)"
  say "  created placeholder token file (0400 root:root)"
fi

# ---------------------------------------------------------------- layout ---

head1 "Directory layout"
# Not in the managed list - created only if missing, otherwise left alone.
ensure_parent "$KNOWLEDGE_PARENT" "$OWNER_USER" "$OWNER_USER" 0755

# Operator-writable, service-read-only: the collector must not be able to
# modify its own code or configuration.
ensure_dir "$APP_ROOT"          "$OWNER_USER" "$OWNER_USER" 0755
ensure_dir "${APP_ROOT}/app"    "$OWNER_USER" "$OWNER_USER" 0755
ensure_dir "${APP_ROOT}/config" "$OWNER_USER" "$OWNER_USER" 0755
ensure_dir "${APP_ROOT}/bin"    "$OWNER_USER" "$OWNER_USER" 0755

# Service-writable. setgid (2770) so anything either side creates keeps the
# shared group; the group owner is the operator because the operator's primary
# group is their own username.
ensure_dir "${APP_ROOT}/state"  "$SERVICE_USER" "$OWNER_USER" 2770
ensure_dir "${APP_ROOT}/logs"   "$SERVICE_USER" "$OWNER_USER" 2770
ensure_dir "$DATA_DIR"          "$SERVICE_USER" "$OWNER_USER" 2770

# Files inside the service-writable trees may predate the service account -
# the operator can run the collector or the indexer by hand before the unit
# exists, and those files land owned by the operator. setgid fixes the group
# for NEW files only, so correct the existing ones too. These three trees hold
# nothing but this subsystem's own data, so a recursive fix is in scope.
if ! getent passwd "$SERVICE_USER" >/dev/null; then
  # --dry-run reaches here before the account exists. `find ! -user <unknown>`
  # errors out, and reporting "already correct" from that would be a lie in
  # exactly the output the operator reads to decide whether to run for real.
  say "  (ownership check skipped: ${SERVICE_USER} does not exist yet)"
fi
for tree in "${APP_ROOT}/state" "${APP_ROOT}/logs" "$DATA_DIR"; do
  if getent passwd "$SERVICE_USER" >/dev/null \
     && [[ -d "$tree" ]] \
     && [[ -n "$(find "$tree" -mindepth 1 -print -quit 2>/dev/null)" ]]; then
    drift="$(find "$tree" -mindepth 1 \( ! -user "$SERVICE_USER" -o ! -group "$OWNER_USER" \) -print -quit 2>/dev/null || true)"
    if [[ -n "$drift" ]]; then
      run chown -R "${SERVICE_USER}:${OWNER_USER}" "$tree"
      run find "$tree" -type f -exec chmod 0660 {} +
      run find "$tree" -type d -exec chmod 2770 {} +
      record "corrected ownership of pre-existing files under ${tree}"
      say "  ~ corrected ownership under ${tree}"
    else
      say "  = ownership already correct under ${tree}"
    fi
  fi
done

say "  (${MOUNT_ROOT}/hermes-data is not referenced by this script.)"

# ------------------------------------------------------------------ unit ---

head1 "systemd unit"
if [[ -f "$UNIT_DEST" ]] && cmp -s "$UNIT_SRC" "$UNIT_DEST"; then
  say "  = already current: ${UNIT_DEST}"
else
  run install -m 0644 -o root -g root "$UNIT_SRC" "$UNIT_DEST"
  record "installed ${UNIT_DEST} (root:root 0644)"
fi

run systemctl daemon-reload

# daemon-reload first, so is-enabled can see a unit installed a moment ago.
if systemctl is-enabled --quiet "$UNIT_NAME" 2>/dev/null; then
  say "  = already enabled: ${UNIT_NAME}"
else
  run systemctl enable "$UNIT_NAME"
  record "enabled ${UNIT_NAME} (starts on boot)"
fi

head1 "systemd-analyze verify"
VERIFY_TARGET="$UNIT_DEST"
[[ -f "$VERIFY_TARGET" ]] || VERIFY_TARGET="$UNIT_SRC"
say "  verifying ${VERIFY_TARGET}"
if systemd-analyze verify "$VERIFY_TARGET"; then
  say "  RESULT: clean (no warnings)"
else
  say "  RESULT: systemd-analyze reported the messages above - read them."
  say "  Warnings about a missing ExecStart binary or venv are expected until"
  say "  the venv is created in the next steps."
fi

# NOT started on purpose.
head1 "Not started"
cat <<'NOTSTARTED'
  The service is enabled but NOT started, and starting it now would fail or,
  worse, sit in a restart loop:

    - the token file still holds a placeholder, so Discord login fails; and
    - config.yaml still has placeholder guild ids with enabled: false, so the
      collector has nothing it is allowed to read (default deny).

  Enabled-but-not-started is the expected end state of this installer.

  After your first real start attempt:

    systemctl status discord-knowledge

  showing "status=78/n/a" (EX_CONFIG) is NOT a crash. It means the collector
  found a configuration fault a restart cannot fix - invalid token, the
  MESSAGE_CONTENT privileged intent still off in the Discord Developer Portal,
  or an empty guild allowlist - and stopped deliberately instead of hammering
  Discord's IDENTIFY endpoint. Fix the cause, then:

    sudo systemctl reset-failed discord-knowledge
    sudo systemctl start discord-knowledge
NOTSTARTED

# --------------------------------------------------------------- summary ---

head1 "Created or changed"
if [[ ${#CHANGES[@]} -eq 0 ]]; then
  say "  (nothing - the system already matched the desired state)"
else
  printf '  - %s\n' "${CHANGES[@]}"
fi

head1 "Next steps (operator)"
cat <<NEXT
  1. Install the real bot token. Never echo it, never leave it in shell
     history, never paste it into config.yaml.

       # editor route (restrictive umask so no temp file leaks):
       ( umask 077; sudo -e ${TOKEN_FILE} )
       sudo chmod 0400 ${TOKEN_FILE}
       sudo chown root:root ${TOKEN_FILE}

       # paste route (nothing hits the shell history or argv):
       sudo install -m 0400 -o root -g root /dev/stdin ${TOKEN_FILE}
       <paste the token, press Enter, then Ctrl-D>

       Verify without revealing it:
       sudo stat -c '%U:%G %a %s bytes' ${TOKEN_FILE}

  2. Copy the application as ${OWNER_USER} (NOT as root):

       install -m 0644 app/*.py         ${APP_ROOT}/app/
       install -m 0644 config/config.yaml ${APP_ROOT}/config/
       install -m 0644 requirements.txt ${APP_ROOT}/

  3. Build the venv as ${OWNER_USER} (the service only reads it):

       python3 -m venv ${APP_ROOT}/venv
       ${APP_ROOT}/venv/bin/pip install -r ${APP_ROOT}/requirements.txt

  4. Fill in the allowlist in ${APP_ROOT}/config/config.yaml:
     replace the placeholder guild ids, list the channel ids as quoted
     strings, then set enabled: true. Nothing is collected until all three
     are done.

  5. Start and watch:

       sudo systemctl start ${UNIT_NAME}
       journalctl -u ${UNIT_NAME} -f

  6. Re-running this script is safe at any time. To remove just the unit:

       sudo bash install-root.sh --uninstall
NEXT
