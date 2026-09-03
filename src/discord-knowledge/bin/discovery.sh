#!/usr/bin/env bash
# List the guilds and channels the Knowledge Bot can see, so the operator can
# copy exact ids into the allowlist. No message content is fetched.
#
#   sudo bash /srv/hermes/discord-knowledge/bin/discovery.sh [--json] [--include-threads]
#
# Why this needs root, and why it is not `sudo -u discordknowledge ...`:
# the service account deliberately CANNOT read /etc/hermes-secrets. The token
# only ever reaches it through systemd's credential mechanism, which is what a
# transient systemd-run unit reproduces here — same delivery path as the real
# service, same unprivileged user, and the secret file stays unreadable to it.
set -euo pipefail

APP_ROOT="/srv/hermes/discord-knowledge"
TOKEN_FILE="/etc/hermes-secrets/discord-knowledge-token"

if [[ "$(id -u)" != "0" ]]; then
  echo "ERROR: needs root to hand the credential to the service user." >&2
  echo >&2
  echo "  sudo bash ${BASH_SOURCE[0]} $*" >&2
  exit 1
fi

[[ -x "${APP_ROOT}/venv/bin/python" ]] || {
  echo "ERROR: venv missing - run deploy.sh as the operator first." >&2; exit 1; }
[[ -f "$TOKEN_FILE" ]] || {
  echo "ERROR: no credential at ${TOKEN_FILE} - run install-root.sh first." >&2; exit 1; }

if grep -q REPLACE_THIS_LINE "$TOKEN_FILE" 2>/dev/null; then
  echo "ERROR: ${TOKEN_FILE} still holds the placeholder, not a real token." >&2
  exit 1
fi

exec systemd-run --pipe --wait --collect --quiet \
  --unit "discord-knowledge-discovery-$$" \
  -p User=discordknowledge \
  -p Group=discordknowledge \
  -p WorkingDirectory="$APP_ROOT" \
  -p LoadCredential="discord-knowledge-token:${TOKEN_FILE}" \
  -p NoNewPrivileges=true \
  -p PrivateTmp=true \
  -p ProtectSystem=strict \
  -p ProtectHome=true \
  -p CapabilityBoundingSet= \
  -p RestrictAddressFamilies="AF_INET AF_INET6 AF_UNIX" \
  "${APP_ROOT}/venv/bin/python" \
  "${APP_ROOT}/app/discovery.py" \
  --config "${APP_ROOT}/config/config.yaml" "$@"
