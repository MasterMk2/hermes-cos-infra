#!/usr/bin/env bash
# Deploy Calendar code and the read-only MCP script. Run as root after the
# root installer created the service-owned state tree. No secret is read,
# copied, or printed; no service/container is restarted here.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST=/srv/hermes/calendar
MCP_DEST=/srv/hermes/hermes-data/tools/calendar

if [ "$(id -u)" -ne 0 ]; then
  echo "must run as root: sudo bash $0" >&2
  exit 1
fi
[ -d "$DEST" ] || { echo "$DEST missing -- run install-root.sh first" >&2; exit 1; }

new=0; updated=0; unchanged=0
copy() {
  local from="$1" to="$2" mode="$3" label="$4"
  if [ ! -e "$to" ]; then
    install -m "$mode" -o root -g root "$from" "$to"
    echo "  new       $label"; new=$((new + 1))
  elif cmp -s "$from" "$to"; then
    unchanged=$((unchanged + 1))
  else
    install -m "$mode" -o root -g root "$from" "$to"
    echo "  updated   $label"; updated=$((updated + 1))
  fi
}

for file in "$SRC"/app/*.py; do
  copy "$file" "$DEST/app/$(basename "$file")" 0644 "app/$(basename "$file")"
done
install -d -m 0755 -o root -g root "$MCP_DEST"
copy "$SRC/mcp/calendar_mcp.py" "$MCP_DEST/calendar_mcp.py" 0644 "mcp/calendar_mcp.py"
echo "calendar deploy: new=$new updated=$updated unchanged=$unchanged"
echo "No service or Docker restart was performed."
