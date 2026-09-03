#!/usr/bin/env bash
# Deploy the gateway application code from the repository to /srv.
#
# Reports what it did for every file — new, updated, or unchanged — and never
# copies silently. D.1's deploy script overwrote production from a stale
# staging copy and reverted a fix with no output at all; the fix was to make
# the script say what changed, so a no-op deploy is visibly a no-op and an
# unexpected "updated" is visible before the restart.
#
# Does not restart the service. Restarting is a separate, deliberate step.

set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_SRC="$(cd "$SRC/.." && pwd)"
DEST=/srv/hermes/channel-gateway

if [ "$(id -u)" -ne 0 ]; then
  echo "must run as root: sudo bash $0" >&2
  exit 1
fi
[ -d "$DEST" ] || { echo "$DEST missing — run install-root.sh first" >&2; exit 1; }

new=0; updated=0; unchanged=0

copy() {
  local from="$1" to="$2" label="$3"
  if [ ! -f "$from" ]; then
    echo "  MISSING   $label  ($from)" >&2
    return 1
  fi
  if [ ! -f "$to" ]; then
    install -m 0644 -o root -g root "$from" "$to"
    echo "  new       $label"
    new=$((new + 1))
  elif cmp -s "$from" "$to"; then
    echo "  unchanged $label"
    unchanged=$((unchanged + 1))
  else
    install -m 0644 -o root -g root "$from" "$to"
    echo "  UPDATED   $label"
    updated=$((updated + 1))
  fi
}

echo "== deploy: $SRC -> $DEST =="

for f in "$SRC"/app/*.py; do
  copy "$f" "$DEST/app/$(basename "$f")" "app/$(basename "$f")"
done

# The canonical secret pattern list. gw_format.py raises at import if this is
# absent rather than falling back to an empty list — redaction is a security
# control and must fail closed, so this file is not optional.
copy "$REPO_SRC/security_patterns.py" "$DEST/app/security_patterns.py" \
     "app/security_patterns.py"

for f in "$SRC"/bin/*.sh; do
  b="$(basename "$f")"
  [ "$b" = "deploy.sh" ] && continue
  copy "$f" "$DEST/bin/$b" "bin/$b"
  chmod 0755 "$DEST/bin/$b"
done

echo
echo "  new=$new updated=$updated unchanged=$unchanged"

# A deploy that changed nothing is a normal outcome and says so; one that
# changed something tells the operator a restart is now pending.
if [ $((new + updated)) -gt 0 ]; then
  echo
  echo "  code changed — restart when ready:"
  echo "    sudo systemctl restart channel-gateway"
else
  echo "  nothing to restart."
fi
