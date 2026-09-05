#!/usr/bin/env bash
# Hermes Gmail Intelligence -- idempotent privileged installation.
#
# This creates isolated identities, paths, root-managed credential placeholders
# and units. It never authorizes Gmail, creates Cloud resources, starts a
# service, changes Hermes configuration, or creates a public endpoint.
set -euo pipefail

ROOT=/srv/hermes/gmail
EVENT_ROOT=/srv/hermes/gmail-event
SECRET_DIR=/etc/hermes-secrets/google-gmail
UNIT=/etc/systemd/system/hermes-gmail.service
EVENT_UNIT=/etc/systemd/system/hermes-gmail-event-ingest.service
EVENT_TIMER=/etc/systemd/system/hermes-gmail-event-ingest.timer
ROTATE=/etc/logrotate.d/hermes-gmail
SRC="$(cd "$(dirname "$0")" && pwd)"
CAL_SRC="$(cd "$SRC/../calendar" && pwd)"
ATT_SRC="$(cd "$SRC/../attention" && pwd)"
ENGINE_USER="$(printenv ENGINE_USER || true)"

if [ "$(id -u)" -ne 0 ]; then
  echo "must run as root: sudo ENGINE_USER=<event-layer-owner> bash $0" >&2
  exit 1
fi
findmnt -rn /srv/hermes >/dev/null || { echo "/srv/hermes is not mounted" >&2; exit 1; }
if [ -z "$ENGINE_USER" ] || ! id -u "$ENGINE_USER" >/dev/null 2>&1; then
  echo "ENGINE_USER must name the account that owns the existing events.db" >&2
  exit 1
fi

if ! getent group hermesgmail >/dev/null; then
  groupadd --system hermesgmail
fi
if ! getent passwd hermesgmail >/dev/null; then
  useradd --system --gid hermesgmail --home-dir "$ROOT" --no-create-home \
    --shell /usr/sbin/nologin --comment "Hermes Gmail Intelligence" hermesgmail
fi
if ! getent group hermesreconcile >/dev/null; then
  groupadd --system hermesreconcile
fi
# The Event bridge uses this group directly and Attention uses it only to
# consume the published Gmail evidence snapshot.  Neither receives OAuth or
# canonical-state access.
if ! id -nG "$ENGINE_USER" | tr ' ' '\n' | grep -Fx hermesreconcile >/dev/null; then
  usermod -aG hermesreconcile "$ENGINE_USER"
fi
for group in $(id -nG hermesgmail); do
  case "$group" in docker|sudo|adm|root|staff|wheel) echo "hermesgmail is in forbidden group $group" >&2; exit 1;; esac
done

# Back up only code/config/unit files that this installer may replace. Secret
# directories and databases are intentionally excluded.
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="/srv/hermes/backups/h2-gmail-$STAMP"
install -d -m 0700 -o root -g root "$BACKUP"
for item in /etc/systemd/system/hermes-calendar.service /etc/systemd/system/attention-engine.service /srv/hermes/calendar/config/config.yaml; do
  [ -f "$item" ] && cp -a "$item" "$BACKUP/" || true
done
install -d -m 0700 -o root -g root "$BACKUP/calendar-app"
for item in /srv/hermes/calendar/app/cal_config.py /srv/hermes/calendar/app/cal_service.py /srv/hermes/calendar/app/cal_reconcile.py; do
  [ -f "$item" ] && cp -a "$item" "$BACKUP/calendar-app/" || true
done
install -d -m 0700 -o root -g root "$BACKUP/attention-app"
for item in /srv/hermes/attention/app/att_config.py /srv/hermes/attention/app/att_health.py; do
  [ -f "$item" ] && cp -a "$item" "$BACKUP/attention-app/" || true
done

install -d -m 0700 -o root -g root "$SECRET_DIR"
for credential in client-id client-secret refresh-token pubsub-service-account.json; do
  [ -e "$SECRET_DIR/$credential" ] || install -m 0400 -o root -g root /dev/null "$SECRET_DIR/$credential"
done

for directory in "$ROOT" "$ROOT/app" "$ROOT/bin" "$ROOT/config" "$ROOT/mcp" "$ROOT/systemd"; do
  install -d -m 0755 -o root -g root "$directory"
done
for directory in "$ROOT/state" "$ROOT/logs"; do
  install -d -m 0750 -o hermesgmail -g hermesgmail "$directory"
done
# The index is an intentionally published, read-only evidence snapshot.  It
# is readable by Hermes' read-only container mount and the dedicated Event /
# Attention reader group, but not by unrelated local users.  setgid preserves
# the reader group on atomic replacement files created by hermesgmail.
install -d -m 2750 -o hermesgmail -g hermesreconcile "$ROOT/index"
if [ -e "$ROOT/index/index.db" ]; then
  chown hermesgmail:hermesreconcile "$ROOT/index/index.db"
  chmod 0440 "$ROOT/index/index.db"
else
  install -m 0440 -o hermesgmail -g hermesreconcile /dev/null "$ROOT/index/index.db"
fi
if [ -e "$ROOT/index/status.json" ]; then
  chown hermesgmail:hermesreconcile "$ROOT/index/status.json"
  chmod 0440 "$ROOT/index/status.json"
fi

for directory in "$EVENT_ROOT" "$EVENT_ROOT/app" "$EVENT_ROOT/state"; do
  install -d -m 0755 -o root -g root "$directory"
done
# A tiny cross-service snapshot is group-readable by Calendar. It contains
# only event identity/title/due/status/link fields, never Gmail body/header or
# any credential. The bridge runs with this primary group and can publish it.
install -d -m 0770 -o root -g hermesreconcile "$EVENT_ROOT/reconcile"

if [ ! -f "$ROOT/config/config.yaml" ]; then
  install -m 0640 -o root -g hermesgmail "$SRC/config/config.sample.yaml" "$ROOT/config/config.yaml"
else
  install -m 0644 -o root -g root "$SRC/config/config.sample.yaml" "$ROOT/config/config.yaml.dist"
fi

[ -x "$ROOT/venv/bin/python" ] || python3 -m venv "$ROOT/venv"
"$ROOT/venv/bin/pip" install --quiet --upgrade pip
"$ROOT/venv/bin/pip" install --quiet -r "$SRC/requirements.txt"
# pip above runs as root, so a fresh venv is already root-owned.  Touch only
# the directory itself on repeat deploys; recursively chowning its dependency
# tree causes prolonged USB I/O and can stall the Gmail service.
install -d -m 0755 -o root -g root "$ROOT/venv"

bash "$SRC/bin/deploy.sh"
# H.2 changes Calendar only to consume the minimal event reconciliation
# projection. Reuse the established H.1 deploy script; it neither reads OAuth
# credentials nor restarts the Calendar service.
[ -d /srv/hermes/calendar/app ] || { echo "Calendar H.1 deployment missing" >&2; exit 1; }
bash "$CAL_SRC/bin/deploy.sh"
[ -d /srv/hermes/attention/app ] || { echo "Attention G.2 deployment missing" >&2; exit 1; }
bash "$ATT_SRC/bin/deploy.sh"
install -m 0644 -o root -g root "$SRC/systemd/hermes-gmail.service" "$UNIT"
install -m 0644 -o root -g root "$CAL_SRC/systemd/hermes-calendar.service" /etc/systemd/system/hermes-calendar.service
ENGINE_GROUP="$(id -gn "$ENGINE_USER")"
ATT_TMP="$(mktemp)"
sed -e "s|@ENGINE_USER@|$ENGINE_USER|g" -e "s|@ENGINE_GROUP@|$ENGINE_GROUP|g" \
  "$ATT_SRC/systemd/attention-engine.service.in" >"$ATT_TMP"
install -m 0644 -o root -g root "$ATT_TMP" /etc/systemd/system/attention-engine.service
rm -f "$ATT_TMP"
install -m 0644 -o root -g root "$SRC/systemd/hermes-gmail-event-ingest.timer" "$EVENT_TIMER"
TMP="$(mktemp)"
sed -e "s|@ENGINE_USER@|$ENGINE_USER|g" "$SRC/systemd/hermes-gmail-event-ingest.service.in" >"$TMP"
install -m 0644 -o root -g root "$TMP" "$EVENT_UNIT"
rm -f "$TMP"
install -m 0644 -o root -g root "$SRC/systemd/hermes-gmail.logrotate" "$ROTATE"
systemctl daemon-reload
systemctl enable hermes-gmail.service hermes-gmail-event-ingest.timer
systemd-analyze verify "$UNIT" "$EVENT_UNIT" "$EVENT_TIMER"
logrotate --debug "$ROTATE" >/dev/null 2>&1 || true

echo "Gmail infrastructure installed; no OAuth, Pub/Sub, service, timer, or public endpoint was started."
echo "Backup created at $BACKUP (credentials and databases excluded)."
echo "Next: install separate Gmail OAuth credentials and Pub/Sub service-account credential root-only, configure topic/subscription, authorize read-only Gmail, then start."
