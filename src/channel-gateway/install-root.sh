#!/usr/bin/env bash
# Unified Channel Gateway — one-time root installation.
#
# Creates the service account, the directory tree, the virtualenv and the
# systemd unit. Installs NO credential: those are written separately, by the
# operator, so that no secret ever passes through this script, its arguments,
# or the shell history of the terminal that ran it.
#
# Safe to re-run. Every step checks before it acts and says what it did.

set -euo pipefail

ROOT=/srv/hermes/channel-gateway
USER_NAME=channelgateway
SECRETS_DIR=/etc/hermes-secrets
UNIT=/etc/systemd/system/channel-gateway.service
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "must run as root: sudo bash $0" >&2
  exit 1
fi

say() { printf '  %-34s %s\n' "$1" "$2"; }

echo "== Unified Channel Gateway: install =="

# --- service account -------------------------------------------------------
# No login shell, no home directory to leave files in, no supplementary
# groups. It exists only to own the queue database and the log directory.
if id -u "$USER_NAME" >/dev/null 2>&1; then
  say "user $USER_NAME" "already exists"
else
  useradd --system --no-create-home --shell /usr/sbin/nologin "$USER_NAME"
  say "user $USER_NAME" "created"
fi

# --- directories -----------------------------------------------------------
# app/ and venv/ stay root-owned: the service must not be able to rewrite its
# own code. Only state/ and logs/ are writable by the service user, and the
# unit's ReadWritePaths names exactly those two.
for d in "$ROOT" "$ROOT/app" "$ROOT/bin" "$ROOT/config"; do
  if [ -d "$d" ]; then say "$d" "exists"; else mkdir -p "$d"; say "$d" "created"; fi
  chown root:root "$d"; chmod 0755 "$d"
done
for d in "$ROOT/state" "$ROOT/logs"; do
  if [ -d "$d" ]; then say "$d" "exists"; else mkdir -p "$d"; say "$d" "created"; fi
  chown "$USER_NAME:$USER_NAME" "$d"; chmod 0750 "$d"
done

# --- secrets directory -----------------------------------------------------
# Created here so the operator's `install` command has somewhere to land. Note
# 0700 root:root — the service user cannot read this directory at all, which
# is the point: systemd reads the files as root and hands the service copies.
if [ -d "$SECRETS_DIR" ]; then
  say "$SECRETS_DIR" "exists"
else
  mkdir -p "$SECRETS_DIR"; say "$SECRETS_DIR" "created"
fi
chown root:root "$SECRETS_DIR"; chmod 0700 "$SECRETS_DIR"

# --- virtualenv ------------------------------------------------------------
# discord.py brings aiohttp, which is also what serves the webhook, so the
# dependency list is two names.
if [ -x "$ROOT/venv/bin/python" ]; then
  say "venv" "exists"
else
  python3 -m venv "$ROOT/venv"
  say "venv" "created"
fi
"$ROOT/venv/bin/pip" install --quiet --upgrade pip >/dev/null
"$ROOT/venv/bin/pip" install --quiet "discord.py>=2.4,<3" "PyYAML>=6,<7"
say "dependencies" "$("$ROOT/venv/bin/python" -c 'import discord,aiohttp,yaml;print(f"discord.py {discord.__version__}, aiohttp {aiohttp.__version__}, pyyaml {yaml.__version__}")')"
chown -R root:root "$ROOT/venv"

# --- config ----------------------------------------------------------------
if [ -f "$ROOT/config/config.yaml" ]; then
  say "config.yaml" "exists (left untouched)"
else
  install -m 0644 -o root -g root "$SRC/config/config.sample.yaml" \
    "$ROOT/config/config.yaml"
  say "config.yaml" "installed from sample — EDIT THE REPLACE_ VALUES"
fi

# --- systemd unit ----------------------------------------------------------
install -m 0644 -o root -g root "$SRC/systemd/channel-gateway.service" "$UNIT"
systemctl daemon-reload
say "unit" "installed and daemon-reloaded"

echo
echo "== next steps (nothing above touched a credential) =="
cat <<'NEXT'
  1. Edit /srv/hermes/channel-gateway/config/config.yaml
       - owner.line_user_id / owner.discord_user_id
       - line.listen_host  (this host's LAN address, not 127.0.0.1)

  2. Install the four credentials. Each command waits for the value on stdin:
     paste it, press Enter, then press Ctrl-D once. Ctrl-C would leave a
     truncated file.

       sudo install -m 0400 -o root -g root /dev/stdin /etc/hermes-secrets/line-channel-secret
       sudo install -m 0400 -o root -g root /dev/stdin /etc/hermes-secrets/line-channel-access-token
       sudo install -m 0400 -o root -g root /dev/stdin /etc/hermes-secrets/discord-interface-token
       sudo install -m 0400 -o root -g root /dev/stdin /etc/hermes-secrets/channel-gateway-hermes-key

  3. Deploy the application code:
       sudo bash <repo>/src/channel-gateway/bin/deploy.sh

  4. Check the configuration without starting anything:
       sudo -u channelgateway /srv/hermes/channel-gateway/venv/bin/python \
         /srv/hermes/channel-gateway/app/gw_service.py \
         --config /srv/hermes/channel-gateway/config/config.yaml --check

  5. Start it:
       sudo systemctl enable --now channel-gateway
       systemctl status channel-gateway
NEXT
