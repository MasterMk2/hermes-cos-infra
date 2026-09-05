#!/usr/bin/env bash
# Deploy Gmail code without reading credentials or restarting services.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST=/srv/hermes/gmail
EVENT_DEST=/srv/hermes/gmail-event/app
MCP_DEST=/srv/hermes/hermes-data/tools/gmail

if [ "$(id -u)" -ne 0 ]; then
  echo "must run as root: sudo bash $0" >&2
  exit 1
fi
[ -d "$DEST/app" ] || { echo "$DEST missing -- run install-root.sh first" >&2; exit 1; }

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
install -d -m 0755 -o root -g root "$EVENT_DEST" "$MCP_DEST"
copy "$SRC/events/gmail_event_extract.py" "$EVENT_DEST/gmail_event_extract.py" 0644 "events/gmail_event_extract.py"
copy "$SRC/mcp/gmail_mcp.py" "$MCP_DEST/gmail_mcp.py" 0644 "mcp/gmail_mcp.py"
echo "gmail deploy: new=$new updated=$updated unchanged=$unchanged"
echo "No service, timer, container, or OAuth operation was started."
