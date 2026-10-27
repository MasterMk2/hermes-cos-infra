#!/usr/bin/env bash
# Phase J.1 rollout — undo.
#
#   sudo src/coding-handoff/deploy/rollback.sh /var/backups/hermes-j1-<stamp>
#
# Restores every file install.sh replaced, from the backup it made, with the
# mode and ownership it captured. Files that install.sh created are removed —
# but only when they are still exactly what it installed, because deleting an
# edit somebody made afterwards is not a rollback.
#
# install.sh calls this itself when health checks fail, so a failed install
# does not leave a partly-changed host.
#
# What it deliberately does NOT remove: the ledger database and the generated
# credential. The ledger is the record of which authorizations produced which
# jobs, and deleting it would make a reinstall create duplicates of jobs that
# already exist. Remove them by hand, after checking, if that is really wanted.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_TOOL="$HERE/deploy_tool.py"
# shellcheck source=lib.sh
. "$HERE/lib.sh"

BACKUP="${1:-}"
[ -n "$BACKUP" ] || die "usage: rollback.sh <backup-directory>"
[ -d "$BACKUP" ] || die "no such backup: $BACKUP"
[ -f "$BACKUP/manifest.json" ] || die "$BACKUP does not look like a J.1 backup"

need_root
need_cmd python3
need_cmd systemctl

MANIFEST="$BACKUP/manifest.json"
#: What was enabled and active before the install, recorded by install.sh.
STATE_FILE="$BACKUP/.service-state"
UNIT_PATH="/etc/systemd/system/hermes-coding-handoff.service"
GATEWAY_CONFIG="/srv/hermes/channel-gateway/config/config.yaml"
BROKER_CONFIG="/opt/hermes-coding-handoff/config/handoff.json"

say "Phase J.1 rollback from $BACKUP"
say ""

say "stopping the broker"
if systemctl list-units --all --type=service 2>/dev/null | grep -q hermes-coding-handoff; then
  systemctl disable --now hermes-coding-handoff.service 2>/dev/null \
    && ok "stopped hermes-coding-handoff.service" \
    || warn "hermes-coding-handoff.service did not stop cleanly"
else
  ok "hermes-coding-handoff.service was not installed"
fi

# restore_one <path> — put back what was there, or remove what we added.
restore_one() {
  local target="$1" payload="${2:-}" saved="$BACKUP/${1#/}" absent="$BACKUP/.absent/${1#/}"
  if [ -f "$saved" ]; then
    cp -p "$saved" "$target" || { bad "cannot restore $target"; return; }
    ok "restored: $target"
    RESTORED=$((RESTORED + 1))
    return
  fi
  if [ -f "$absent" ] && [ -e "$target" ]; then
    if [ -n "$payload" ] && [ "$(sha256_of "$target")" != "$payload" ]; then
      warn "left in place (edited since install): $target"
      return
    fi
    rm -f "$target" && ok "removed: $target"
    REMOVED=$((REMOVED + 1))
  fi
}

RESTORED=0
REMOVED=0

say ""
say "restoring files"
read_lines ROLLBACK_ROWS manifest_files "$MANIFEST" all \
  || die "cannot enumerate the backup manifest; nothing was touched"
while IFS=$'\t' read -r source target mode payload baseline; do
  [ -n "$source" ] || continue
  restore_one "$target" "$payload"
done <<< "$ROLLBACK_ROWS"

# The unit and the two configuration files are rendered rather than copied, so
# they are not in the manifest and are handled by name.
restore_one "$UNIT_PATH"

# The job layer was patched in place; its originals are in the backup under
# their own paths, so restoring them puts back the deployed content exactly —
# CRLF, resolved fallback path and all.
say ""
say "restoring the job layer"
read_lines ROLLBACK_PATCHES tool manifest-patches "$MANIFEST" \
  || die "cannot enumerate the job-layer patches"
while IFS=$'\t' read -r kind target live note; do
  [ -n "$target" ] || continue
  restore_one "$target"
done <<< "$ROLLBACK_PATCHES"

if [ -f "$BACKUP/config.yaml" ]; then
  cp -p "$BACKUP/config.yaml" "$GATEWAY_CONFIG" \
    && { ok "restored: $GATEWAY_CONFIG"; RESTORED=$((RESTORED + 1)); } \
    || bad "cannot restore $GATEWAY_CONFIG"
fi
if [ -f "$BACKUP/handoff.json" ]; then
  cp -p "$BACKUP/handoff.json" "$BROKER_CONFIG" \
    && { ok "restored: $BROKER_CONFIG"; RESTORED=$((RESTORED + 1)); } \
    || bad "cannot restore $BROKER_CONFIG"
fi

say ""
say "units"
systemctl daemon-reload || warn "systemctl daemon-reload failed"

# Restoring files without restoring service state leaves a host that was
# running before the install stopped afterwards. `install.sh` records what was
# enabled and active before it touched anything; this puts that back rather
# than guessing from the current state, which the install itself changed.
restore_service() {
  local unit="$1" was_enabled="" was_active=""
  if [ -f "$STATE_FILE" ]; then
    was_enabled="$(awk -F'\t' -v u="$unit" '$1==u{print $2}' "$STATE_FILE")"
    was_active="$(awk -F'\t' -v u="$unit" '$1==u{print $3}' "$STATE_FILE")"
  fi
  if [ -z "$was_enabled" ] && [ -z "$was_active" ]; then
    warn "no recorded pre-install state for $unit; leaving it alone"
    return
  fi
  case "$was_enabled" in
    enabled)  systemctl enable "$unit" >/dev/null 2>&1 || warn "cannot re-enable $unit" ;;
    disabled) systemctl disable "$unit" >/dev/null 2>&1 || warn "cannot disable $unit" ;;
  esac
  if [ "$was_active" = "active" ]; then
    systemctl restart "$unit" \
      && ok "$unit restored to active" \
      || bad "$unit was active before the install and did not restart"
  else
    systemctl stop "$unit" >/dev/null 2>&1
    ok "$unit restored to ${was_active:-inactive}"
  fi
}

restore_service channel-gateway.service
if [ -f "$STATE_FILE" ] && grep -q "^hermes-coding-handoff.service" "$STATE_FILE"; then
  restore_service hermes-coding-handoff.service
fi

say ""
say "restored $RESTORED file(s), removed $REMOVED file(s)"
say "kept on purpose: the handoff ledger and the generated credential"
say ""
if [ "$FAILURES" -eq 0 ]; then
  say "${GREEN}rollback complete${PLAIN}"
  exit 0
fi
say "${RED}rollback finished with $FAILURES problem(s)${PLAIN}"
exit 1
