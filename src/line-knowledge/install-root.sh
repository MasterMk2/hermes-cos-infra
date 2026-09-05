#!/usr/bin/env bash
# Restore the non-privileged LINE knowledge refresh scheduler.  This installer
# never reads an SSH key, a LINE token, or message data.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
TOOL=/srv/hermes/hermes-data/tools/line-knowledge/refresh-line.sh
UNIT=/etc/systemd/system/hermes-line-refresh.service
TIMER=/etc/systemd/system/hermes-line-refresh.timer
ENGINE_USER="$(printenv ENGINE_USER || true)"

if [ "$(id -u)" -ne 0 ]; then
  echo "must run as root: sudo ENGINE_USER=<knowledge-owner> bash $0" >&2
  exit 1
fi
if [ -z "$ENGINE_USER" ] || ! id -u "$ENGINE_USER" >/dev/null 2>&1; then
  echo "ENGINE_USER must name the account that owns the existing LINE knowledge tools" >&2
  exit 1
fi
[ -x "$TOOL" ] || { echo "existing LINE refresh tool is missing" >&2; exit 1; }

ENGINE_GROUP="$(id -gn "$ENGINE_USER")"
ENGINE_HOME="$(getent passwd "$ENGINE_USER" | cut -d: -f6)"
[ -d "$ENGINE_HOME/.ssh" ] || { echo "required SSH configuration directory is missing" >&2; exit 1; }

STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="/srv/hermes/backups/h2-line-scheduler-$STAMP"
install -d -m 0700 -o root -g root "$BACKUP"
for item in "$UNIT" "$TIMER"; do
  [ -f "$item" ] && cp -a "$item" "$BACKUP/" || true
done

TMP="$(mktemp)"
sed -e "s|@ENGINE_USER@|$ENGINE_USER|g" \
    -e "s|@ENGINE_GROUP@|$ENGINE_GROUP|g" \
    -e "s|@ENGINE_HOME@|$ENGINE_HOME|g" \
    "$SRC/systemd/hermes-line-refresh.service.in" >"$TMP"
install -m 0644 -o root -g root "$TMP" "$UNIT"
rm -f "$TMP"
install -m 0644 -o root -g root "$SRC/systemd/hermes-line-refresh.timer" "$TIMER"
systemctl daemon-reload
systemctl enable --now hermes-line-refresh.timer
systemd-analyze verify "$UNIT" "$TIMER"

echo "LINE Knowledge refresh timer enabled; it runs as the existing knowledge owner."
echo "Backup created at $BACKUP (credentials and message data excluded)."
