#!/usr/bin/env bash
# Phase G.2 — turn on the gateway's owner-notification surface.
#
# One root step, so the operator is asked once rather than seven times. It:
#
#   1. backs up everything it is about to change, including gateway.db, which
#      lives in a 0750 directory that only root and the service user can read
#   2. creates run/ — the directory whose permissions ARE the access control
#      on the proactive socket
#   3. deploys the new application code
#   4. installs the updated unit (ReadWritePaths gains run/)
#   5. appends the `proactive:` block to config.yaml if it is not there
#   6. reloads, restarts, and then PROVES the socket is reachable from the
#      engine's account rather than assuming it
#
# Safe to re-run. Nothing here touches a credential.

set -euo pipefail

ROOT=/srv/hermes/channel-gateway
USER_NAME=channelgateway
ENGINE_GROUP="${ENGINE_GROUP:-}"
UNIT=/etc/systemd/system/channel-gateway.service
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="/srv/hermes/backups/g2-gateway-$STAMP"

if [ "$(id -u)" -ne 0 ]; then
  echo "must run as root: sudo bash $0" >&2
  exit 1
fi

say() { printf '  %-32s %s\n' "$1" "$2"; }

echo "== G.2: enable the proactive surface on the channel gateway =="

# --- 1. backup -------------------------------------------------------------
mkdir -p "$BACKUP"/{app,config,systemd,state}
cp -a "$ROOT"/app/*.py "$BACKUP/app/" 2>/dev/null || true
cp -a "$ROOT"/config/config.yaml "$BACKUP/config/" 2>/dev/null || true
cp -a "$UNIT" "$BACKUP/systemd/" 2>/dev/null || true
# A file copy of a live SQLite database can be torn. Use the backup API, which
# takes a consistent snapshot even while the gateway is writing.
if [ -f "$ROOT/state/gateway.db" ]; then
  "$ROOT/venv/bin/python" - "$ROOT/state/gateway.db" "$BACKUP/state/gateway.db" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
d = sqlite3.connect(dst)
s.backup(d)
d.close(); s.close()
PY
  say "gateway.db" "snapshotted"
fi
chmod -R go-rwx "$BACKUP/state" 2>/dev/null || true
say "backup" "$BACKUP"

# --- 2. the socket directory ----------------------------------------------
# 2770: owner is the gateway (it creates the socket), group is the engine's
# account (it connects), setgid so the socket inherits that group without the
# gateway process being a member. Nobody else can traverse it, which is the
# whole of the access control — the socket's own mode is belt and braces.
# Deliberately no default. A wrong group produces a socket the engine
# cannot reach and nothing that explains why, so the operator names it.
if [ -z "$ENGINE_GROUP" ]; then
  echo "set ENGINE_GROUP to the group the Attention Engine runs as, e.g." >&2
  echo "  sudo ENGINE_GROUP=<group> bash $0" >&2
  exit 1
fi
if ! getent group "$ENGINE_GROUP" >/dev/null; then
  echo "group '$ENGINE_GROUP' does not exist" >&2
  exit 1
fi
mkdir -p "$ROOT/run"
chown "$USER_NAME:$ENGINE_GROUP" "$ROOT/run"
chmod 2770 "$ROOT/run"
say "run/" "$(stat -c '%U:%G %a' "$ROOT/run")"

# --- 3. application code ---------------------------------------------------
bash "$SRC/bin/deploy.sh"

# --- 4. unit ---------------------------------------------------------------
if cmp -s "$SRC/systemd/channel-gateway.service" "$UNIT"; then
  say "unit" "unchanged"
else
  install -m 0644 -o root -g root "$SRC/systemd/channel-gateway.service" "$UNIT"
  say "unit" "UPDATED"
fi
systemctl daemon-reload

# --- 5. configuration ------------------------------------------------------
CONF="$ROOT/config/config.yaml"
if grep -qE '^proactive:' "$CONF"; then
  say "config proactive:" "already present (left untouched)"
else
  cat >>"$CONF" <<'YAML'

# Phase G.2 — the owner-notification surface used by the Attention Engine.
# There is no recipient field in this API: the LINE `to` and the Discord user
# id come from `owner:` above, exactly as they do on the reply path.
proactive:
  enabled: true
  socket_path: /srv/hermes/channel-gateway/run/proactive.sock
  quota_cache_seconds: 1800
  stuck_lease_seconds: 180
YAML
  say "config proactive:" "appended (enabled: true)"
fi

# --- 6. restart and prove it -----------------------------------------------
"$ROOT/venv/bin/python" "$ROOT/app/gw_service.py" --config "$CONF" --check \
  >/dev/null 2>&1 || true    # diagnostics only; the real gate is below

systemctl restart channel-gateway
sleep 4
systemctl is-active --quiet channel-gateway || {
  echo "gateway did not come back up:" >&2
  journalctl -u channel-gateway -n 30 --no-pager >&2
  exit 1
}

SOCK="$ROOT/run/proactive.sock"
for _ in 1 2 3 4 5 6 7 8 9 10; do
  [ -S "$SOCK" ] && break
  sleep 1
done
if [ ! -S "$SOCK" ]; then
  echo "the gateway restarted but no socket appeared at $SOCK" >&2
  journalctl -u channel-gateway -n 30 --no-pager >&2
  exit 1
fi
say "socket" "$(stat -c '%U:%G %a' "$SOCK")"

# The check that matters: not "does the file exist" but "can the account that
# will use it actually talk to it". A socket the engine cannot reach is the
# silent failure this phase exists to stop reporting as success.
ENGINE_USER="${ENGINE_USER:-$ENGINE_GROUP}"
if id -u "$ENGINE_USER" >/dev/null 2>&1; then
  if sudo -u "$ENGINE_USER" curl -sf --max-time 5 --unix-socket "$SOCK" \
       http://localhost/internal/health >/dev/null; then
    say "reachable as $ENGINE_USER" "yes"
  else
    echo "  the socket exists but $ENGINE_USER cannot reach it" >&2
    exit 1
  fi
fi

echo
echo "== done. The gateway can now be asked to notify the owner. =="
echo "   status:  sudo bash $ROOT/bin/gw-status.sh"
