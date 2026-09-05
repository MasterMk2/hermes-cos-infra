#!/usr/bin/env bash
# Hermes Calendar Authority Layer -- idempotent privileged installation.
# Creates the dedicated service identity and root-managed credential boundary,
# but does not authorize Google, select calendars, start the service, alter
# Hermes config, or expose any network route.
set -euo pipefail

ROOT=/srv/hermes/calendar
SECRET_DIR=/etc/hermes-secrets/google-calendar
UNIT=/etc/systemd/system/hermes-calendar.service
ROTATE=/etc/logrotate.d/hermes-calendar
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "must run as root: sudo bash $0" >&2
  exit 1
fi
findmnt -rn /srv/hermes >/dev/null || { echo "/srv/hermes is not mounted" >&2; exit 1; }

if ! getent group hermescalendar >/dev/null; then
  groupadd --system hermescalendar
fi
if ! getent passwd hermescalendar >/dev/null; then
  useradd --system --gid hermescalendar --home-dir "$ROOT" --no-create-home \
    --shell /usr/sbin/nologin --comment "Hermes Calendar Authority" hermescalendar
fi
for group in $(id -nG hermescalendar); do
  case "$group" in docker|sudo|adm|root|staff|wheel) echo "hermescalendar is in forbidden group $group" >&2; exit 1;; esac
done

install -d -m 0700 -o root -g root "$SECRET_DIR"
for credential in client-id client-secret refresh-token; do
  if [ ! -e "$SECRET_DIR/$credential" ]; then
    install -m 0400 -o root -g root /dev/null "$SECRET_DIR/$credential"
  fi
done

for directory in "$ROOT" "$ROOT/app" "$ROOT/bin" "$ROOT/config" "$ROOT/mcp" "$ROOT/systemd"; do
  install -d -m 0755 -o root -g root "$directory"
done
for directory in "$ROOT/state" "$ROOT/logs"; do
  install -d -m 0750 -o hermescalendar -g hermescalendar "$directory"
done
# Docker mounts the index directory read-only. The service atomically replaces
# index.db, so mounting the individual file would pin an obsolete inode.
install -d -m 0755 -o hermescalendar -g hermescalendar "$ROOT/index"
# The first service start atomically replaces this harmless empty placeholder
# with a valid read-only snapshot.
if [ ! -e "$ROOT/index/index.db" ]; then
  install -m 0444 -o hermescalendar -g hermescalendar /dev/null "$ROOT/index/index.db"
fi

if [ ! -f "$ROOT/config/config.yaml" ]; then
  install -m 0640 -o root -g hermescalendar "$SRC/config/config.sample.yaml" "$ROOT/config/config.yaml"
else
  install -m 0644 -o root -g root "$SRC/config/config.sample.yaml" "$ROOT/config/config.yaml.dist"
fi

if [ -x "$ROOT/venv/bin/python" ]; then
  :
else
  python3 -m venv "$ROOT/venv"
fi
"$ROOT/venv/bin/pip" install --quiet --upgrade pip
"$ROOT/venv/bin/pip" install --quiet -r "$SRC/requirements.txt"
chown -R root:root "$ROOT/venv"

bash "$SRC/bin/deploy.sh"
install -m 0644 -o root -g root "$SRC/systemd/hermes-calendar.service" "$UNIT"
install -m 0644 -o root -g root "$SRC/systemd/hermes-calendar.logrotate" "$ROTATE"
systemctl daemon-reload
systemctl enable hermes-calendar.service
systemd-analyze verify "$UNIT"
logrotate --debug "$ROTATE" >/dev/null 2>&1 || true

echo "Calendar infrastructure installed; service deliberately not started."
echo "Next: create Desktop OAuth client, install client-id/client-secret root-only, authorize, choose stable calendar IDs, configure callback, then start."
