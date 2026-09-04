#!/usr/bin/env bash
# Deploy the attention engine's code from the repository to /srv.
#
# Reports every file as new, updated or unchanged, and never copies silently:
# D.1's deploy script once reverted a fix from a stale staging copy with no
# output at all. Does not restart the service — that stays a deliberate step.

set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST=/srv/hermes/attention

if [ "$(id -u)" -ne 0 ]; then
  echo "must run as root: sudo bash $0" >&2
  exit 1
fi
[ -d "$DEST" ] || { echo "$DEST missing — run install-root.sh first" >&2; exit 1; }

new=0; updated=0; unchanged=0

copy() {
  local from="$1" to="$2" label="$3" mode="${4:-0644}"
  if [ ! -f "$from" ]; then
    echo "  MISSING   $label  ($from)" >&2
    return 1
  fi
  if [ ! -f "$to" ]; then
    install -m "$mode" -o root -g root "$from" "$to"
    echo "  new       $label"; new=$((new + 1))
  elif cmp -s "$from" "$to"; then
    echo "  unchanged $label"; unchanged=$((unchanged + 1))
  else
    install -m "$mode" -o root -g root "$from" "$to"
    echo "  UPDATED   $label"; updated=$((updated + 1))
  fi
}

echo "== deploy: $SRC -> $DEST =="

for f in "$SRC"/app/*.py; do
  copy "$f" "$DEST/app/$(basename "$f")" "app/$(basename "$f")"
done
for f in "$SRC"/bin/*.sh; do
  b="$(basename "$f")"
  [ "$b" = "deploy.sh" ] && continue
  copy "$f" "$DEST/bin/$b" "bin/$b" 0755
done

echo
echo "  new=$new updated=$updated unchanged=$unchanged"
if [ $((new + updated)) -gt 0 ]; then
  echo
  echo "  code changed — restart when ready:"
  echo "    sudo systemctl restart attention-engine"
else
  echo "  nothing to restart."
fi
