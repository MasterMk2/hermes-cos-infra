#!/usr/bin/env bash
# Restore the non-privileged LINE knowledge refresh scheduler and downstream reconciliation.
# Installs:
#   1. hermes-classifier-broker.service (native inference broker via Antigravity bridge)
#   2. hermes-line-reconcile.service (dedicated non-privileged hermeslinereconcile identity)
#   3. hermes-line-refresh.service + timer (refresh scheduler)
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
TOOL=/srv/hermes/hermes-data/tools/line-knowledge/refresh-line.sh
RECONCILE_TOOL=/srv/hermes/hermes-data/tools/line-knowledge/reconcile-line.sh
BROKER_TOOL=/srv/hermes/hermes-data/tools/events/event_broker.py

UNIT=/etc/systemd/system/hermes-line-refresh.service
RECONCILE_UNIT=/etc/systemd/system/hermes-line-reconcile.service
BROKER_UNIT=/etc/systemd/system/hermes-classifier-broker.service
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

ENGINE_GROUP="$(id -gn "$ENGINE_USER")"
ENGINE_HOME="$(getent passwd "$ENGINE_USER" | cut -d: -f6)"
[ -d "$ENGINE_HOME/.ssh" ] || { echo "required SSH configuration directory is missing" >&2; exit 1; }

# 1. Ensure dedicated unprivileged reconcile identity exists (Section 14)
RECONCILE_USER=hermeslinereconcile
RECONCILE_GROUP=hermesreconcile
if ! id -u "$RECONCILE_USER" >/dev/null 2>&1; then
  useradd -r -s /usr/sbin/nologin -g "$RECONCILE_GROUP" -d /srv/hermes/hermes-data/events "$RECONCILE_USER"
  echo "Created dedicated system user: $RECONCILE_USER (group: $RECONCILE_GROUP)"
fi

# 2. Permissions & Group Ownership (POSIX permissions + optional ACL)
mkdir -p /srv/hermes/hermes-data/events /srv/hermes/hermes-data/logs

# Standard POSIX group permissions (hermesreconcile group GID 980):
chgrp -R "$RECONCILE_GROUP" /srv/hermes/hermes-data/events
chmod 0770 /srv/hermes/hermes-data/events
chmod g+s /srv/hermes/hermes-data/events
find /srv/hermes/hermes-data/events -maxdepth 1 -type f -exec chmod 0660 {} +

chgrp -R "$RECONCILE_GROUP" /srv/hermes/hermes-data/logs
chmod 0770 /srv/hermes/hermes-data/logs
chmod g+s /srv/hermes/hermes-data/logs
if [ -f /srv/hermes/hermes-data/logs/line-reconcile.log ]; then
  chmod 0660 /srv/hermes/hermes-data/logs/line-reconcile.log
  chgrp "$RECONCILE_GROUP" /srv/hermes/hermes-data/logs/line-reconcile.log
fi

if [ -d /srv/hermes/knowledge/line ]; then
  chgrp -R "$RECONCILE_GROUP" /srv/hermes/knowledge/line
  chmod 0750 /srv/hermes/knowledge/line
  chmod g+s /srv/hermes/knowledge/line
  if [ -f /srv/hermes/knowledge/line/messages.jsonl ]; then
    chmod 0640 /srv/hermes/knowledge/line/messages.jsonl
  fi
fi

if [ -d /srv/hermes/hermes-data/knowledge-index/line ]; then
  chgrp -R "$RECONCILE_GROUP" /srv/hermes/hermes-data/knowledge-index/line
  chmod 0750 /srv/hermes/hermes-data/knowledge-index/line
  chmod g+s /srv/hermes/hermes-data/knowledge-index/line
  if [ -f /srv/hermes/hermes-data/knowledge-index/line/index.db ]; then
    chmod 0640 /srv/hermes/hermes-data/knowledge-index/line/index.db
  fi
fi

# Optional: apply POSIX ACLs if setfacl is available or installable
if ! command -v setfacl >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq && apt-get install -y -qq acl || true
  fi
fi

if command -v setfacl >/dev/null 2>&1; then
  setfacl -m u:"$RECONCILE_USER":rwx /srv/hermes/hermes-data/events 2>/dev/null || true
  setfacl -d -m u:"$RECONCILE_USER":rw- /srv/hermes/hermes-data/events 2>/dev/null || true
  find /srv/hermes/hermes-data/events -maxdepth 1 -type f -exec setfacl -m u:"$RECONCILE_USER":rw- {} + 2>/dev/null || true
  setfacl -m u:"$RECONCILE_USER":rwx /srv/hermes/hermes-data/logs 2>/dev/null || true
  setfacl -d -m u:"$RECONCILE_USER":rw- /srv/hermes/hermes-data/logs 2>/dev/null || true
  [ -f /srv/hermes/hermes-data/logs/line-reconcile.log ] && setfacl -m u:"$RECONCILE_USER":rw- /srv/hermes/hermes-data/logs/line-reconcile.log 2>/dev/null || true
  if [ -d /srv/hermes/knowledge/line ]; then
    setfacl -m u:"$RECONCILE_USER":r-x /srv/hermes/knowledge/line 2>/dev/null || true
    setfacl -d -m u:"$RECONCILE_USER":r-- /srv/hermes/knowledge/line 2>/dev/null || true
    [ -f /srv/hermes/knowledge/line/messages.jsonl ] && setfacl -m u:"$RECONCILE_USER":r-- /srv/hermes/knowledge/line/messages.jsonl 2>/dev/null || true
  fi
  if [ -d /srv/hermes/hermes-data/knowledge-index/line ]; then
    setfacl -m u:"$RECONCILE_USER":r-x /srv/hermes/hermes-data/knowledge-index/line 2>/dev/null || true
    setfacl -d -m u:"$RECONCILE_USER":r-- /srv/hermes/hermes-data/knowledge-index/line 2>/dev/null || true
    [ -f /srv/hermes/hermes-data/knowledge-index/line/index.db ] && setfacl -m u:"$RECONCILE_USER":r-- /srv/hermes/hermes-data/knowledge-index/line/index.db 2>/dev/null || true
  fi
fi

# 3. Deploy scripts
mkdir -p /srv/hermes/hermes-data/tools/line-knowledge /srv/hermes/hermes-data/tools/events
install -m 0755 -o "$ENGINE_USER" -g "$ENGINE_GROUP" "$SRC/refresh-line.sh" "$TOOL"
install -m 0755 -o "$ENGINE_USER" -g "$ENGINE_GROUP" "$SRC/reconcile-line.sh" "$RECONCILE_TOOL"
install -m 0755 -o "$ENGINE_USER" -g "$ENGINE_GROUP" "$SRC/event_broker.py" "$BROKER_TOOL"
install -m 0755 -o "$ENGINE_USER" -g "$ENGINE_GROUP" "$SRC/event_extract.py" "/srv/hermes/hermes-data/tools/events/event_extract.py"

# 4. Backup existing systemd units
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="/srv/hermes/backups/h2-line-scheduler-$STAMP"
install -d -m 0700 -o root -g root "$BACKUP"
for item in "$UNIT" "$RECONCILE_UNIT" "$BROKER_UNIT" "$TIMER"; do
  [ -f "$item" ] && cp -a "$item" "$BACKUP/" || true
done

# 5. Install systemd units
TMP="$(mktemp)"
sed -e "s|@ENGINE_USER@|$ENGINE_USER|g" \
    -e "s|@ENGINE_GROUP@|$ENGINE_GROUP|g" \
    -e "s|@ENGINE_HOME@|$ENGINE_HOME|g" \
    "$SRC/systemd/hermes-line-refresh.service.in" >"$TMP"
install -m 0644 -o root -g root "$TMP" "$UNIT"

install -m 0644 -o root -g root "$SRC/systemd/hermes-classifier-broker.service.in" "$BROKER_UNIT"
install -m 0644 -o root -g root "$SRC/systemd/hermes-line-reconcile.service.in" "$RECONCILE_UNIT"
rm -f "$TMP"

install -m 0644 -o root -g root "$SRC/systemd/hermes-line-refresh.timer" "$TIMER"

# 6. Reload and start services
systemctl daemon-reload
systemctl enable --now hermes-classifier-broker.service
systemctl enable --now hermes-line-refresh.timer
systemd-analyze verify "$UNIT" "$RECONCILE_UNIT" "$BROKER_UNIT" "$TIMER"

echo "Phase I.1-S2-FR: Broker and Reconcile services deployed and verified successfully."
echo "Backup created at $BACKUP (credentials and message data excluded)."
