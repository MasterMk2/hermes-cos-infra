#!/usr/bin/env bash
# Measure per-channel message volume so the backfill bounds come from the real
# server rather than from a guess (spec sections 46/47).
#
#   sudo bash /srv/hermes/discord-knowledge/bin/volume-probe.sh [--json] [--limit N] [--guild ID]
#
# Reads recent messages through the same REST endpoint the backfill uses, but
# prints ONLY counts, ratios and timestamps — never a message body — and
# writes nothing to the databases.
#
# Runs as the service user with the token delivered through systemd's
# credential mechanism, exactly like discovery.sh and the real service.
set -euo pipefail

APP_ROOT="/srv/hermes/discord-knowledge"
TOKEN_FILE="/etc/hermes-secrets/discord-knowledge-token"

if [[ "$(id -u)" != "0" ]]; then
  echo "ERROR: needs root to hand the credential to the service user." >&2
  echo "  sudo bash ${BASH_SOURCE[0]} $*" >&2
  exit 1
fi

[[ -x "${APP_ROOT}/venv/bin/python" ]] || {
  echo "ERROR: venv missing - run deploy.sh as the operator first." >&2; exit 1; }
[[ -f "$TOKEN_FILE" ]] || {
  echo "ERROR: no credential at ${TOKEN_FILE}." >&2; exit 1; }

exec systemd-run --pipe --wait --collect --quiet \
  --unit "discord-knowledge-volume-$$" \
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
  "${APP_ROOT}/app/volume_probe.py" \
  --config "${APP_ROOT}/config/config.yaml" "$@"
