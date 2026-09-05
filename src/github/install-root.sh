#!/usr/bin/env bash
# Hermes GitHub Read Authority -- idempotent installation.
#
# Creates dedicated paths, copies tools, and provisions root-managed
# credential placeholder. Does not start any service or mutate repositories.
set -euo pipefail

DEST=/srv/hermes/hermes-data/tools/github
SECRET_DIR=/etc/hermes-secrets/github
TOKEN_FILE="$SECRET_DIR/read-token"
SRC="$(cd "$(dirname "$0")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "must run as root: sudo bash $0" >&2
  exit 1
fi
findmnt -rn /srv/hermes >/dev/null || { echo "/srv/hermes is not mounted" >&2; exit 1; }

# Create secret directory
install -d -m 0700 -o root -g root "$SECRET_DIR"
if [ ! -e "$TOKEN_FILE" ]; then
  install -m 0400 -o root -g root /dev/null "$TOKEN_FILE"
fi

# Create target directory for Hermes MCP
install -d -m 0755 -o root -g root "$DEST"

# Backup existing code
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="/srv/hermes/backups/h3-github-$STAMP"
if [ -d "$DEST" ] && [ "$(ls -A "$DEST")" ]; then
  install -d -m 0700 -o root -g root "$BACKUP"
  cp -a "$DEST"/* "$BACKUP/" || true
fi

# Deploy application files
install -m 0644 -o root -g root "$SRC/app/gh_config.py" "$DEST/gh_config.py"
install -m 0644 -o root -g root "$SRC/app/gh_client.py" "$DEST/gh_client.py"
install -m 0644 -o root -g root "$SRC/mcp/github_mcp.py" "$DEST/github_mcp.py"

# Deploy configuration if not exists
if [ ! -e "$DEST/config.yaml" ]; then
  install -m 0644 -o root -g root "$SRC/config/config.sample.yaml" "$DEST/config.yaml"
fi

echo "GitHub Read Authority files installed to $DEST"
echo "Read-only token path: $TOKEN_FILE (0400 root:root)"
echo "Next: install read-only token into $TOKEN_FILE and add MCP snippet to Hermes config.yaml"