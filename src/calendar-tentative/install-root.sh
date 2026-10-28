#!/usr/bin/env bash
# Hermes calendar tentative registration — idempotent root installation.
#
# Creates the service identity, the root-only credential directory, the code
# and state tree, the virtualenv and the systemd units. It deliberately does
# NOT authorize Google, create the calendar, edit the configuration, or enable
# the timer: each of those is an owner decision, printed as a next step.
#
# THE IDENTITY
#   hermestentative  --system, /usr/sbin/nologin, no home, own primary group,
#                    and at most two supplementary groups:
#     hermesreconcile   READ events.db (commitments)            required
#     hermesproactive   CONNECT to the gateway's proactive socket  if present
#   Never docker, sudo, adm, root, wheel or staff; checked after the change.
#
# USAGE
#   sudo bash src/calendar-tentative/install-root.sh
#   sudo bash src/calendar-tentative/install-root.sh --uninstall
#     (removes units and identity; leaves state and credentials in place)

set -euo pipefail

ROOT=/srv/hermes/calendar-tentative
SECRET_DIR=/etc/hermes-secrets/google-calendar-tentative
H1_SECRET_DIR=/etc/hermes-secrets/google-calendar
EVENTS_DB=/srv/hermes/hermes-data/events/events.db
UNIT_DIR=/etc/systemd/system
SERVICE_UNIT="$UNIT_DIR/hermes-calendar-tentative.service"
TIMER_UNIT="$UNIT_DIR/hermes-calendar-tentative.timer"

SERVICE_USER=hermestentative
RECONCILE_GROUP="${RECONCILE_GROUP:-hermesreconcile}"
PROACTIVE_GROUP="${PROACTIVE_GROUP:-hermesproactive}"
FORBIDDEN_GROUPS="docker sudo adm root wheel staff"

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "must run as root: sudo bash $0" >&2
  exit 1
fi

if [ "${1:-}" = "--uninstall" ]; then
  systemctl disable --now hermes-calendar-tentative.timer 2>/dev/null || true
  systemctl stop hermes-calendar-tentative.service 2>/dev/null || true
  rm -f "$SERVICE_UNIT" "$TIMER_UNIT"
  systemctl daemon-reload
  if getent passwd "$SERVICE_USER" >/dev/null; then
    userdel "$SERVICE_USER"
  fi
  echo "Removed units and $SERVICE_USER. Left in place: $ROOT/state, $SECRET_DIR."
  echo "The tentative calendar itself remains in Google Calendar; delete it there if unwanted."
  exit 0
fi
if [ -n "${1:-}" ]; then
  echo "unknown argument: $1" >&2
  exit 2
fi

findmnt -rn /srv/hermes >/dev/null || { echo "/srv/hermes is not mounted" >&2; exit 1; }
[ -f "$EVENTS_DB" ] || { echo "$EVENTS_DB does not exist" >&2; exit 1; }
getent group "$RECONCILE_GROUP" >/dev/null || { echo "group $RECONCILE_GROUP does not exist" >&2; exit 1; }

# --- identity --------------------------------------------------------------
if ! getent group "$SERVICE_USER" >/dev/null; then
  groupadd --system "$SERVICE_USER"
fi
if ! getent passwd "$SERVICE_USER" >/dev/null; then
  useradd --system --gid "$SERVICE_USER" --home-dir "$ROOT" --no-create-home \
    --shell /usr/sbin/nologin --comment "Hermes calendar tentative registration" "$SERVICE_USER"
fi
usermod -aG "$RECONCILE_GROUP" "$SERVICE_USER"
GROUPS_FOR_UNIT="$RECONCILE_GROUP"
if getent group "$PROACTIVE_GROUP" >/dev/null; then
  usermod -aG "$PROACTIVE_GROUP" "$SERVICE_USER"
  GROUPS_FOR_UNIT="$RECONCILE_GROUP $PROACTIVE_GROUP"
else
  echo "NOTE: group $PROACTIVE_GROUP not found (installed by the Morning Brief)."
  echo "      The owner digest cannot reach the gateway; set notify.enabled: false"
  echo "      or install src/planning first and re-run this script."
fi
for group in $(id -nG "$SERVICE_USER"); do
  for bad in $FORBIDDEN_GROUPS; do
    if [ "$group" = "$bad" ]; then
      echo "$SERVICE_USER is in forbidden group $group; remove it: gpasswd -d $SERVICE_USER $group" >&2
      exit 1
    fi
  done
done

# --- credentials (presence only; never written with a value here) ---------
install -d -m 0700 -o root -g root "$SECRET_DIR"
for credential in client-id client-secret refresh-token; do
  if [ ! -e "$SECRET_DIR/$credential" ]; then
    install -m 0400 -o root -g root /dev/null "$SECRET_DIR/$credential"
  fi
done

# --- code, config, state ---------------------------------------------------
for directory in "$ROOT" "$ROOT/app" "$ROOT/config"; do
  install -d -m 0755 -o root -g root "$directory"
done
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$ROOT/state"
for file in "$SRC"/app/*.py; do
  install -m 0644 -o root -g root "$file" "$ROOT/app/$(basename "$file")"
done
if [ ! -f "$ROOT/config/config.yaml" ]; then
  install -m 0640 -o root -g "$SERVICE_USER" "$SRC/config/config.sample.yaml" "$ROOT/config/config.yaml"
else
  install -m 0644 -o root -g root "$SRC/config/config.sample.yaml" "$ROOT/config/config.yaml.dist"
fi

if [ ! -x "$ROOT/venv/bin/python" ]; then
  python3 -m venv "$ROOT/venv"
fi
"$ROOT/venv/bin/pip" install --quiet --upgrade pip
"$ROOT/venv/bin/pip" install --quiet -r "$SRC/requirements.txt"
chown -R root:root "$ROOT/venv"

# --- units -----------------------------------------------------------------
sed "s|@SUPPLEMENTARY_GROUPS@|$GROUPS_FOR_UNIT|" \
  "$SRC/systemd/hermes-calendar-tentative.service.in" > "$SERVICE_UNIT.tmp"
install -m 0644 -o root -g root "$SERVICE_UNIT.tmp" "$SERVICE_UNIT"
rm -f "$SERVICE_UNIT.tmp"
install -m 0644 -o root -g root "$SRC/systemd/hermes-calendar-tentative.timer" "$TIMER_UNIT"
systemctl daemon-reload
systemd-analyze verify "$SERVICE_UNIT" "$TIMER_UNIT" || true

if sudo -u "$SERVICE_USER" test -r "$EVENTS_DB"; then
  echo "events.db         readable by $SERVICE_USER"
else
  echo "WARNING: $SERVICE_USER cannot read $EVENTS_DB yet (group changes apply to new processes)"
fi

cat <<NEXT

Installed. Nothing is authorized, created or scheduled yet. Next, as root:

 1. Reuse H.1's Desktop OAuth client (or create another):
      install -m 0400 -o root -g root $H1_SECRET_DIR/client-id     $SECRET_DIR/client-id
      install -m 0400 -o root -g root $H1_SECRET_DIR/client-secret $SECRET_DIR/client-secret
 2. Authorize (scope: calendar.events.owned only) through an SSH forward:
      ssh -L 8766:127.0.0.1:8766 <pi>
      $ROOT/venv/bin/python $ROOT/app/ct_oauth.py
 3. Entries go onto the primary calendar (calendar.calendar_id: primary) with
    a 【仮】 title prefix. Consider behaviour.mode: dry_run for the first runs.
 4. Check and preview — no Google call, nothing written:
      sudo -u $SERVICE_USER $ROOT/venv/bin/python $ROOT/app/ct_service.py --config $ROOT/config/config.yaml check
      sudo -u $SERVICE_USER $ROOT/venv/bin/python $ROOT/app/ct_service.py --config $ROOT/config/config.yaml plan
 5. Start:  systemctl enable --now hermes-calendar-tentative.timer
NEXT
