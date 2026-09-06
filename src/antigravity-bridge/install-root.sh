#!/usr/bin/env bash
# Hermes Antigravity Provider Bridge -- Idempotent Privileged Installation (Phase H.3.2-S)
set -euo pipefail

DEST=/srv/hermes/antigravity
UNIT=/etc/systemd/system/hermes-antigravity.service
SRC="$(cd "$(dirname "$0")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "must run as root: sudo bash $0" >&2
  exit 1
fi
findmnt -rn /srv/hermes >/dev/null || { echo "/srv/hermes is not mounted" >&2; exit 1; }

# 1. Identity Provisioning
if ! getent group hermesantigravity >/dev/null; then
  groupadd --system hermesantigravity
fi
if ! getent passwd hermesantigravity >/dev/null; then
  useradd --system --gid hermesantigravity --create-home --home-dir /home/hermesantigravity --shell /usr/sbin/nologin hermesantigravity
fi

chmod 0700 /home/hermesantigravity
chown -R hermesantigravity:hermesantigravity /home/hermesantigravity

# Verify identity isolation
for group in $(id -nG hermesantigravity); do
  case "$group" in docker|sudo|adm|root|staff|wheel) echo "hermesantigravity is in forbidden group $group" >&2; exit 1;; esac
done

# 2. Directory Provisioning
install -d -m 0755 -o root -g root "$DEST"
install -d -m 0755 -o root -g root "$DEST/bin"
install -d -m 0755 -o root -g root "$DEST/app"
install -d -m 0750 -o root -g hermesantigravity "$DEST/config"
install -d -m 0750 -o hermesantigravity -g hermesantigravity "$DEST/state"
install -d -m 0750 -o hermesantigravity -g hermesantigravity "$DEST/logs"
install -d -m 0700 -o hermesantigravity -g hermesantigravity "$DEST/secrets"

# 3. Binary Placement
if [ -f "/home/<PI_USER>/antigravity-test-bin/antigravity" ]; then
  install -m 0755 -o root -g root "/home/<PI_USER>/antigravity-test-bin/antigravity" "$DEST/bin/antigravity"
fi

# 4. Secret Bearer Token Provisioning
if [ ! -f "$DEST/secrets/bridge.token" ]; then
  TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  install -m 0400 -o hermesantigravity -g hermesantigravity <(echo -n "$TOKEN") "$DEST/secrets/bridge.token"
fi

# 5. Application Deployment
install -m 0644 -o root -g root "$SRC/app/bridge_config.py" "$DEST/app/bridge_config.py"
install -m 0644 -o root -g root "$SRC/app/bridge_server.py" "$DEST/app/bridge_server.py"

# 6. Configuration Deployment
if [ ! -f "$DEST/config/bridge.yaml" ]; then
  install -m 0640 -o root -g hermesantigravity "$SRC/config/bridge.sample.yaml" "$DEST/config/bridge.yaml"
fi

# 7. Python Virtual Environment
if [ ! -x "$DEST/venv/bin/python" ]; then
  python3 -m venv "$DEST/venv"
  "$DEST/venv/bin/pip" install --quiet --upgrade pip
  "$DEST/venv/bin/pip" install --quiet pyyaml
fi

# 8. Systemd Service Deployment
install -m 0644 -o root -g root "$SRC/systemd/hermes-antigravity.service" "$UNIT"
systemctl daemon-reload
systemd-analyze verify "$UNIT"

echo "Antigravity bridge infrastructure installed successfully."
