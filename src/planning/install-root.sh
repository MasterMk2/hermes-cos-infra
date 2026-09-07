#!/usr/bin/env bash
# Hermes Morning Brief — root installation. Idempotent, and reviewable before
# it is trusted.
#
# Phase I.2 sections 50, 53, 54, 65, 67, 68, 69; I2-AMENDMENT section G.
#
# WHAT THIS INSTALLS NO CREDENTIAL FOR
#   The brief reaches the owner only through the channel gateway's unix
#   socket, which holds both the Discord and the LINE token and fixes the
#   recipient from its own configuration. Nothing here writes a token, an
#   OAuth file, or a recipient id.
#
# THE IDENTITY IT CREATES
#   hermesplanner  --system, /usr/sbin/nologin, no home directory created,
#                  primary group hermesplanner, and exactly two supplementary
#                  groups:
#     hermesreconcile   READ events.db and the freshness snapshots (section 68)
#     hermesproactive   WRITE the gateway's proactive socket (I2-AMENDMENT G5)
#   It is not added to docker, sudo, adm, or the operator's own login group,
#   and this script refuses to continue if it finds it in any of them.
#
# WHY A NEW GROUP FOR THE SOCKET
#   /srv/hermes/channel-gateway/run is currently group-owned by the operator's
#   login group, which is in sudo, docker and adm. Adding a service identity to
#   that group would hand it group-read on the attention state database and the
#   Discord message store — a direct violation of section 69. Instead this
#   script creates a dedicated `hermesproactive` group, gives the run directory
#   to it (the directory is setgid 2770, so the socket inherits the group), and
#   puts both the attention engine's account and hermesplanner in it. Nothing
#   in the gateway's code or configuration changes.
#
# USAGE
#   sudo ATTENTION_USER=<the account attention-engine runs as> \
#        bash src/planning/install-root.sh [--dry-run]
#
#   --dry-run   print every change without making any of them
#   --uninstall remove the units, the identity and the group memberships,
#               restoring the run directory's original group
#
# A gateway restart is required for the socket to be recreated with the new
# group; this script performs it (and says so) unless --dry-run. The exact
# rollback is printed at the end and is also what --uninstall performs.

set -euo pipefail

# --- paths ----------------------------------------------------------------
APP_ROOT=/srv/hermes/planning                       # root-owned: code + config
STATE_DIR=/srv/hermes/hermes-data/planning          # the ONLY writable dir
GATEWAY_RUN=/srv/hermes/channel-gateway/run
GATEWAY_SOCK="$GATEWAY_RUN/proactive.sock"
EVENTS_DB=/srv/hermes/hermes-data/events/events.db

UNIT_DIR=/etc/systemd/system
SERVICE_UNIT="$UNIT_DIR/hermes-morning-brief.service"
TIMER_UNIT="$UNIT_DIR/hermes-morning-brief.timer"

PLANNER_USER=hermesplanner
PLANNER_GROUP=hermesplanner
RECONCILE_GROUP="${RECONCILE_GROUP:-hermesreconcile}"
PROACTIVE_GROUP="${PROACTIVE_GROUP:-hermesproactive}"

#: Groups the planner must never be in (section 69). Checked, not assumed.
FORBIDDEN_GROUPS="docker sudo adm root wheel staff"

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRY_RUN=0
UNINSTALL=0
RENDERER_NET="${PLANNER_RENDERER_NET:-0}"
CHANGES=0

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help) sed -n '2,45p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  echo "must run as root: sudo bash $0" >&2
  exit 1
fi

say()  { printf '  %-38s %s\n' "$1" "$2"; }
did()  { CHANGES=$((CHANGES + 1)); printf '  %-38s %s\n' "$1" "CHANGED: $2"; }
run()  {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '  would run: %s\n' "$*"
  else
    "$@"
  fi
}
die()  { echo "ERROR: $*" >&2; exit 1; }

# `install -m` cannot be trusted to narrow an existing path silently, so every
# permission change goes through this: it refuses to widen, and it says what it
# is doing when it narrows.
ensure_mode() {
  local path="$1" want="$2"
  [ -e "$path" ] || return 0
  local have; have="$(stat -c '%a' "$path")"
  [ "$have" = "$want" ] && return 0
  # Numeric compare: a larger octal value is a broader permission set.
  if [ "$((10#$have))" -lt "$((10#$want))" ]; then
    die "refusing to widen $path from 0$have to 0$want. Inspect it and decide by hand."
  fi
  run chmod "0$want" "$path"
  did "mode $path" "0$have -> 0$want"
}

ensure_owner() {
  local path="$1" want="$2"
  [ -e "$path" ] || return 0
  local have; have="$(stat -c '%U:%G' "$path")"
  [ "$have" = "$want" ] && return 0
  run chown "$want" "$path"
  did "owner $path" "$have -> $want"
}

# ==========================================================================
#  UNINSTALL
# ==========================================================================
if [ "$UNINSTALL" -eq 1 ]; then
  echo "== Hermes Morning Brief: uninstall =="
  [ "$DRY_RUN" -eq 1 ] && echo "   (dry run: nothing will change)"
  run systemctl disable --now hermes-morning-brief.timer 2>/dev/null || true
  run systemctl stop hermes-morning-brief.service 2>/dev/null || true
  for unit in "$TIMER_UNIT" "$SERVICE_UNIT"; do
    [ -f "$unit" ] && { run rm -f "$unit"; did "removed" "$unit"; }
  done
  run systemctl daemon-reload

  ORIG_GROUP="${ORIGINAL_RUN_GROUP:-}"
  if [ -n "$ORIG_GROUP" ]; then
    run chgrp "$ORIG_GROUP" "$GATEWAY_RUN"
    did "run dir group" "restored to $ORIG_GROUP"
    run systemctl restart channel-gateway.service
    say "gateway" "restarted so the socket is recreated"
  else
    say "run dir group" "left as-is; pass ORIGINAL_RUN_GROUP=<group> to restore"
  fi

  if id -u "$PLANNER_USER" >/dev/null 2>&1; then
    run gpasswd -d "$PLANNER_USER" "$RECONCILE_GROUP" 2>/dev/null || true
    run gpasswd -d "$PLANNER_USER" "$PROACTIVE_GROUP" 2>/dev/null || true
    run userdel "$PLANNER_USER"
    did "identity" "$PLANNER_USER removed"
  fi
  say "state" "$STATE_DIR left in place (delete by hand if wanted)"
  echo
  echo "Removed. The gateway, the attention engine and events.db are untouched."
  exit 0
fi

# ==========================================================================
#  INSTALL
# ==========================================================================
echo "== Hermes Morning Brief: install =="
[ "$DRY_RUN" -eq 1 ] && echo "   *** DRY RUN — nothing below will be changed ***"
say "source" "$SRC"

ATTENTION_USER="${ATTENTION_USER:-}"
if [ -z "$ATTENTION_USER" ]; then
  cat >&2 <<'NEED'
ERROR: set ATTENTION_USER to the account attention-engine.service runs as.

  It is not committed to the repository (a unit file must not carry an
  operating-system account name), and it matters here because that account
  must join the new hermesproactive group or the attention engine loses its
  own access to the gateway socket the moment the group changes.

  Find it with:   systemctl show attention-engine -p User
  Then:           sudo ATTENTION_USER=<that account> bash "$0"
NEED
  exit 1
fi
id -u "$ATTENTION_USER" >/dev/null 2>&1 || die "account '$ATTENTION_USER' does not exist"
say "attention account" "$ATTENTION_USER"

# --- preconditions --------------------------------------------------------
[ -d "$GATEWAY_RUN" ] || die "$GATEWAY_RUN does not exist. Install the channel gateway first."
[ -f "$EVENTS_DB" ]   || die "$EVENTS_DB does not exist."
getent group "$RECONCILE_GROUP" >/dev/null || die "group '$RECONCILE_GROUP' does not exist"

ORIGINAL_RUN_GROUP="$(stat -c '%G' "$GATEWAY_RUN")"
ORIGINAL_RUN_MODE="$(stat -c '%a' "$GATEWAY_RUN")"
say "gateway run dir" "$GATEWAY_RUN ($ORIGINAL_RUN_GROUP, 0$ORIGINAL_RUN_MODE)"
if [ "$ORIGINAL_RUN_MODE" != "2770" ]; then
  say "WARNING" "run dir is 0$ORIGINAL_RUN_MODE, expected 2770 (setgid)"
  say "" "without setgid the socket will not inherit the new group"
fi

# --- backup ---------------------------------------------------------------
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="/srv/hermes/backups/i2-morning-brief-install-$STAMP"
if [ "$DRY_RUN" -eq 0 ]; then
  mkdir -p "$BACKUP"
  for f in "$SERVICE_UNIT" "$TIMER_UNIT" "$APP_ROOT/config/config.yaml"; do
    [ -f "$f" ] && cp -a "$f" "$BACKUP/" || true
  done
  printf 'ORIGINAL_RUN_GROUP=%s\nORIGINAL_RUN_MODE=%s\n' \
    "$ORIGINAL_RUN_GROUP" "$ORIGINAL_RUN_MODE" >"$BACKUP/rollback.env"
fi
say "backup" "$BACKUP"

# --- 1. the proactive group (I2-AMENDMENT G5) -----------------------------
echo
echo "-- 1. gateway socket group --"
if getent group "$PROACTIVE_GROUP" >/dev/null; then
  say "group $PROACTIVE_GROUP" "exists (gid $(getent group "$PROACTIVE_GROUP" | cut -d: -f3))"
else
  run groupadd --system "$PROACTIVE_GROUP"
  did "group $PROACTIVE_GROUP" "created (--system)"
fi

for account in "$ATTENTION_USER" "$PLANNER_USER"; do
  if [ "$account" = "$PLANNER_USER" ] && ! id -u "$PLANNER_USER" >/dev/null 2>&1; then
    continue   # created in step 2; joined there
  fi
  if id -nG "$account" 2>/dev/null | tr ' ' '\n' | grep -qx "$PROACTIVE_GROUP"; then
    say "$account in $PROACTIVE_GROUP" "already"
  else
    run usermod -aG "$PROACTIVE_GROUP" "$account"
    did "$account" "added to $PROACTIVE_GROUP"
  fi
done

if [ "$ORIGINAL_RUN_GROUP" = "$PROACTIVE_GROUP" ]; then
  say "run dir group" "already $PROACTIVE_GROUP"
else
  run chgrp "$PROACTIVE_GROUP" "$GATEWAY_RUN"
  did "run dir group" "$ORIGINAL_RUN_GROUP -> $PROACTIVE_GROUP"
  say "rollback" "chgrp $ORIGINAL_RUN_GROUP $GATEWAY_RUN && systemctl restart channel-gateway"
  GATEWAY_RESTART_NEEDED=1
fi
# The directory permissions are the whole access mechanism (the gateway chmods
# the socket to 0660 at gw_proactive.py:407). Keep 2770 so the socket inherits
# the group; never widen it.
ensure_mode "$GATEWAY_RUN" 2770

# --- 2. the planner identity (sections 67, 69) ----------------------------
echo
echo "-- 2. service identity --"
if id -u "$PLANNER_USER" >/dev/null 2>&1; then
  say "user $PLANNER_USER" "exists (uid $(id -u "$PLANNER_USER"))"
else
  run useradd --system --no-create-home --home-dir "$APP_ROOT" \
      --shell /usr/sbin/nologin --user-group \
      --comment "Hermes Morning Brief planner" "$PLANNER_USER"
  did "user $PLANNER_USER" "created (system, nologin, no home)"
fi

for grp in "$RECONCILE_GROUP" "$PROACTIVE_GROUP"; do
  if id -nG "$PLANNER_USER" 2>/dev/null | tr ' ' '\n' | grep -qx "$grp"; then
    say "$PLANNER_USER in $grp" "already"
  else
    run usermod -aG "$grp" "$PLANNER_USER"
    did "$PLANNER_USER" "added to $grp"
  fi
done

# Section 69, checked rather than asserted. This runs after the additions, so
# it catches a pre-existing membership as well as anything this script did.
if [ "$DRY_RUN" -eq 0 ] && id -u "$PLANNER_USER" >/dev/null 2>&1; then
  ACTUAL="$(id -nG "$PLANNER_USER" | tr ' ' '\n' | sort | tr '\n' ' ')"
  say "$PLANNER_USER groups" "$ACTUAL"
  for bad in $FORBIDDEN_GROUPS; do
    if id -nG "$PLANNER_USER" | tr ' ' '\n' | grep -qx "$bad"; then
      die "$PLANNER_USER is in '$bad'. Phase section 69 forbids it. Remove it with: gpasswd -d $PLANNER_USER $bad"
    fi
  done
  EXPECTED="$PLANNER_GROUP $PROACTIVE_GROUP $RECONCILE_GROUP "
  EXPECTED="$(printf '%s\n' $EXPECTED | sort | tr '\n' ' ')"
  if [ "$ACTUAL" != "$EXPECTED" ]; then
    say "NOTE" "group set differs from the expected minimum: $EXPECTED"
    say "" "review it; every extra group is reachable credentials"
  fi
fi

# --- 3. directories -------------------------------------------------------
echo
echo "-- 3. directories --"
# Code and configuration stay root-owned: the service must not be able to
# rewrite its own code or loosen its own configuration.
for d in "$APP_ROOT" "$APP_ROOT/app" "$APP_ROOT/bin" "$APP_ROOT/config"; do
  if [ -d "$d" ]; then
    say "dir $d" "exists"
  else
    run mkdir -p "$d"
    did "dir $d" "created"
  fi
  ensure_owner "$d" "root:root"
  ensure_mode "$d" 755
done

# The one writable directory (sections 65, 68).
if [ -d "$STATE_DIR" ]; then
  say "dir $STATE_DIR" "exists"
else
  run mkdir -p "$STATE_DIR"
  did "dir $STATE_DIR" "created"
fi
ensure_owner "$STATE_DIR" "$PLANNER_USER:$PLANNER_GROUP"
ensure_mode "$STATE_DIR" 750

# --- 4. read access, verified rather than assumed -------------------------
echo
echo "-- 4. read access --"
if [ "$DRY_RUN" -eq 0 ]; then
  if sudo -u "$PLANNER_USER" test -r "$EVENTS_DB"; then
    say "events.db" "readable by $PLANNER_USER"
  else
    say "WARNING" "$PLANNER_USER cannot read $EVENTS_DB yet"
    say "" "group changes apply to NEW processes; systemd will start a new one"
  fi
  # Prove the negative too: section 69's whole point.
  for denied in /srv/hermes/attention/state /srv/hermes/gmail/state \
                /srv/hermes/calendar/state /srv/hermes/channel-gateway/state; do
    [ -e "$denied" ] || continue
    if sudo -u "$PLANNER_USER" test -r "$denied" 2>/dev/null; then
      say "WARNING" "$PLANNER_USER can read $denied — investigate"
    else
      say "denied (good)" "$denied"
    fi
  done
else
  say "read checks" "skipped in dry run"
fi

# --- 5. configuration -----------------------------------------------------
echo
echo "-- 5. configuration --"
CONFIG="$APP_ROOT/config/config.yaml"
if [ -f "$CONFIG" ]; then
  say "config.yaml" "exists (left untouched — it holds the brief time)"
else
  run install -m 0644 -o root -g root \
      "$SRC/config/planning.sample.yaml" "$CONFIG"
  did "config.yaml" "installed from planning.sample.yaml"
fi

# The single source of truth for the schedule (section 53). Read it back out
# of the INSTALLED file, so the timer can never disagree with the service.
# Quote stripping is done in the shell, not in awk: mawk (Debian's default)
# does not accept awk's \x hex escapes, so an apostrophe inside the awk program
# is not worth the quoting it would cost.
read_time() {
  [ -f "$1" ] || { echo ""; return; }
  awk '
    /^morning_brief:/ { inblock = 1; next }
    /^[^[:space:]#]/  { inblock = 0 }
    inblock && $1 == "time:" { print $2; exit }
  ' "$1" | tr -d "\"'" | tr -d '[:space:]'
}
MORNING_TIME="$(read_time "$CONFIG")"
if [ -z "$MORNING_TIME" ]; then
  if [ "$DRY_RUN" -eq 1 ]; then
    MORNING_TIME="$(read_time "$SRC/config/planning.sample.yaml")"
    say "morning time" "$MORNING_TIME (from the sample; config not installed yet)"
  else
    die "could not read morning_brief.time from $CONFIG"
  fi
fi
case "$MORNING_TIME" in
  [0-2][0-9]:[0-5][0-9]) : ;;
  *) die "morning_brief.time must be HH:MM, found '$MORNING_TIME' in $CONFIG" ;;
esac
say "morning time" "$MORNING_TIME Asia/Tokyo (from $CONFIG)"

# --- 6. timezone pinning, verified on THIS host (section 54) --------------
echo
echo "-- 6. timezone pinning --"
CAL_SPEC="*-*-* ${MORNING_TIME}:00 Asia/Tokyo"
if systemd-analyze calendar "$CAL_SPEC" >/dev/null 2>&1; then
  say "OnCalendar suffix" "supported by systemd $(systemctl --version | head -1 | awk '{print $2}')"
  systemd-analyze calendar "$CAL_SPEC" | sed 's/^/    /'
else
  die "this systemd does not accept an OnCalendar timezone suffix.
     Calendar-specification timezones need systemd >= 252 (this host reports
     $(systemctl --version | head -1)). Fall back by hand: use
     'OnCalendar=*-*-* ${MORNING_TIME}:00' with the host timezone confirmed as
     Asia/Tokyo (timedatectl), and note the assumption in RESUME.md."
fi

# --- 7. application + venv ------------------------------------------------
echo
echo "-- 7. application --"
if [ -x "$APP_ROOT/venv/bin/python" ]; then
  say "venv" "exists"
else
  run python3 -m venv "$APP_ROOT/venv"
  did "venv" "created"
fi
# PyYAML and nothing else, matching the attention engine: the gateway client
# here is http.client over AF_UNIX, which is standard library.
if [ "$DRY_RUN" -eq 0 ]; then
  "$APP_ROOT/venv/bin/pip" install --quiet --upgrade pip >/dev/null
  "$APP_ROOT/venv/bin/pip" install --quiet "PyYAML>=6,<7"
  say "dependencies" "$("$APP_ROOT/venv/bin/python" -c 'import yaml;print("pyyaml "+yaml.__version__)')"
  chown -R root:root "$APP_ROOT/venv"
fi

if [ "$DRY_RUN" -eq 0 ]; then
  install -d -m 0755 -o root -g root "$APP_ROOT/app/planning"
  for f in "$SRC"/*.py; do
    [ -e "$f" ] || continue
    install -m 0644 -o root -g root "$f" "$APP_ROOT/app/planning/$(basename "$f")"
  done
  # PEP 420 namespace packages work without it, but an explicit __init__.py is
  # what makes `python -m planning.cli` behave identically in the venv and in
  # the repository.
  [ -f "$APP_ROOT/app/planning/__init__.py" ] || \
    install -m 0644 -o root -g root /dev/null "$APP_ROOT/app/planning/__init__.py"
  say "app" "$APP_ROOT/app/planning ($(ls -1 "$APP_ROOT/app/planning"/*.py | wc -l) modules)"
  if [ -d "$SRC/bin" ]; then
    for f in "$SRC"/bin/*.sh; do
      [ -e "$f" ] || continue
      install -m 0755 -o root -g root "$f" "$APP_ROOT/bin/$(basename "$f")"
    done
    say "bin" "$APP_ROOT/bin"
  fi
else
  say "app" "would copy $(ls -1 "$SRC"/*.py 2>/dev/null | wc -l) modules to $APP_ROOT/app/planning"
fi

# --- 8. units -------------------------------------------------------------
echo
echo "-- 8. systemd units --"
if [ "$RENDERER_NET" = "1" ]; then
  ADDRESS_FAMILIES="AF_UNIX AF_INET AF_INET6"
  say "RestrictAddressFamilies" "$ADDRESS_FAMILIES  (PLANNER_RENDERER_NET=1)"
  say "" "the renderer was declared to need TCP; the template path needs none"
else
  ADDRESS_FAMILIES="AF_UNIX"
  say "RestrictAddressFamilies" "$ADDRESS_FAMILIES  (no network; template fallback)"
fi

render_unit() {
  local template="$1" target="$2" tmp
  tmp="$(mktemp)"
  sed -e "s|@PLANNER_USER@|$PLANNER_USER|g" \
      -e "s|@PLANNER_GROUP@|$PLANNER_GROUP|g" \
      -e "s|@RECONCILE_GROUP@|$RECONCILE_GROUP|g" \
      -e "s|@PROACTIVE_GROUP@|$PROACTIVE_GROUP|g" \
      -e "s|@RENDERER_ADDRESS_FAMILIES@|$ADDRESS_FAMILIES|g" \
      -e "s|@MORNING_TIME@|$MORNING_TIME|g" \
      "$template" >"$tmp"
  if cmp -s "$tmp" "$target" 2>/dev/null; then
    say "$(basename "$target")" "unchanged"
  elif [ "$DRY_RUN" -eq 1 ]; then
    say "$(basename "$target")" "would be written"
    diff -u "$target" "$tmp" 2>/dev/null | sed 's/^/    /' | head -40 || true
  else
    install -m 0644 -o root -g root "$tmp" "$target"
    did "$(basename "$target")" "installed"
  fi
  rm -f "$tmp"
}
render_unit "$SRC/systemd/hermes-morning-brief.service.in" "$SERVICE_UNIT"
render_unit "$SRC/systemd/hermes-morning-brief.timer.in"   "$TIMER_UNIT"
run systemctl daemon-reload

if [ "$DRY_RUN" -eq 0 ]; then
  VERIFY_OUT="$(systemd-analyze verify "$SERVICE_UNIT" "$TIMER_UNIT" 2>&1 || true)"
  if [ -n "$VERIFY_OUT" ]; then
    say "systemd-analyze verify" "reported:"
    printf '%s\n' "$VERIFY_OUT" | sed 's/^/    /'
  else
    say "systemd-analyze verify" "clean"
  fi
fi

# --- 9. gateway restart ---------------------------------------------------
echo
echo "-- 9. gateway socket --"
if [ "${GATEWAY_RESTART_NEEDED:-0}" = "1" ]; then
  say "restart required" "the socket must be recreated to inherit $PROACTIVE_GROUP"
  run systemctl restart channel-gateway.service
  run systemctl restart attention-engine.service
  if [ "$DRY_RUN" -eq 0 ]; then
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      [ -S "$GATEWAY_SOCK" ] && break
      sleep 1
    done
    if [ -S "$GATEWAY_SOCK" ]; then
      SOCK_GROUP="$(stat -c '%G' "$GATEWAY_SOCK")"
      SOCK_MODE="$(stat -c '%a' "$GATEWAY_SOCK")"
      say "socket" "$GATEWAY_SOCK ($SOCK_GROUP, 0$SOCK_MODE)"
      [ "$SOCK_GROUP" = "$PROACTIVE_GROUP" ] || \
        say "WARNING" "socket group is $SOCK_GROUP, expected $PROACTIVE_GROUP"
    else
      say "WARNING" "socket did not reappear within 10s — check channel-gateway"
    fi
  fi
else
  say "restart" "not required (group unchanged)"
fi

# --- summary --------------------------------------------------------------
echo
if [ "$DRY_RUN" -eq 1 ]; then
  echo "== dry run complete: $CHANGES change(s) would have been made =="
  exit 0
fi
echo "== install complete: $CHANGES change(s) made =="
cat <<NEXT

  What ${PLANNER_USER} was granted, in full:
    primary group      ${PLANNER_GROUP}
    ${RECONCILE_GROUP}$(printf '%*s' $((18 - ${#RECONCILE_GROUP})) '')READ  ${EVENTS_DB}
                       READ  /srv/hermes/gmail/index (published snapshot)
                       READ  /srv/hermes/hermes-data/knowledge-index
    ${PROACTIVE_GROUP}$(printf '%*s' $((18 - ${#PROACTIVE_GROUP})) '')WRITE ${GATEWAY_SOCK} (connect only)
    unit ReadWritePaths   ${STATE_DIR}
                          ${GATEWAY_RUN}
    unit ReadOnlyPaths    /srv/hermes/hermes-data/events  (section 64)
    NOT granted        docker, sudo, adm, the operator's login group,
                       any OAuth or channel token, attention state,
                       gateway state or gateway config

  Verify before enabling anything (sends nothing):
    sudo -u ${PLANNER_USER} ${APP_ROOT}/venv/bin/python -m planning.cli \\
      --config ${CONFIG} run --dry-run --measure
    (PYTHONPATH=${APP_ROOT}/app)

  Then enable the timer:
    sudo systemctl enable --now hermes-morning-brief.timer
    systemctl list-timers hermes-morning-brief.timer
    sudo bash ${APP_ROOT}/bin/morning-brief-status.sh

  ROLLBACK (exact):
    sudo ORIGINAL_RUN_GROUP=${ORIGINAL_RUN_GROUP} bash $0 --uninstall
  which removes both units, deletes ${PLANNER_USER}, and restores
  ${GATEWAY_RUN} to group ${ORIGINAL_RUN_GROUP} with a gateway restart.
  Saved for you at: ${BACKUP}/rollback.env

NEXT
