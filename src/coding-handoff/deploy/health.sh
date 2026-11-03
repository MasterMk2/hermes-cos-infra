#!/usr/bin/env bash
# Phase J.1 rollout — post-install health.
#
# Read-only, and it never sends anything to a channel. The point is to answer
# "did this deployment break the Front?" before anybody finds out by messaging
# it, so the checks that matter most are the ones about the things this change
# did NOT intend to alter.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_TOOL="$HERE/deploy_tool.py"
# shellcheck source=lib.sh
. "$HERE/lib.sh"

MANIFEST="$HERE/manifest.json"
EXPECT_DISPATCH=""
EXPECT_ROUTE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --expect-dispatch) EXPECT_DISPATCH="${2:-}"; shift 2 ;;
    --expect-route)    EXPECT_ROUTE="${2:-}"; shift 2 ;;
    *) shift ;;
  esac
done

say "Phase J.1 health"
say ""

say "services"
for unit in channel-gateway.service hermes-coding-handoff.service; do
  if systemctl is-active --quiet "$unit"; then
    ok "active: $unit"
  else
    bad "not active: $unit (journalctl -u $unit)"
  fi
done

# The two services this change must not disturb. A premium reasoning route or
# an Antigravity bridge that stopped is this deployment's fault until proven
# otherwise, so it is checked here rather than discovered later.
for unit in hermes-antigravity.service hermes-premium-cli.service; do
  if systemctl list-unit-files "$unit" >/dev/null 2>&1 \
     && systemctl cat "$unit" >/dev/null 2>&1; then
    if systemctl is-active --quiet "$unit"; then
      ok "still active: $unit"
    else
      bad "not active: $unit — this change must not have stopped it"
    fi
  fi
done

say ""
say "broker"
if command -v curl >/dev/null 2>&1; then
  # Unauthenticated health only. No token is read, and nothing is posted.
  if curl -fsS --max-time 5 http://127.0.0.1:18771/health >/dev/null 2>&1; then
    ok "broker answers on loopback"
    # What the *running* process believes, not what the file on disk says. A
    # config rewritten without a restart would otherwise pass silently.
    if [ -n "$EXPECT_DISPATCH" ]; then
      live_dispatch="$(curl -fsS --max-time 5 http://127.0.0.1:18771/health         | "$PY" "$DEPLOY_TOOL" read-health-flag dispatch_enabled 2>/dev/null)"
      case "$EXPECT_DISPATCH:$live_dispatch" in
        1:true|0:false) ok "running broker reports dispatch_enabled=$live_dispatch" ;;
        *) bad "running broker reports dispatch_enabled=$live_dispatch, expected $EXPECT_DISPATCH" ;;
      esac
    fi
  else
    bad "broker did not answer on 127.0.0.1:18771"
  fi
else
  warn "curl unavailable; broker HTTP check skipped"
fi

if ss -ltn 2>/dev/null | grep -q '127\.0\.0\.1:18771'; then
  ok "broker is bound to loopback only"
elif ss -ltn 2>/dev/null | grep -q ':18771'; then
  bad "port 18771 is bound to something other than loopback"
fi

say ""
say "installed content"
verify_payload "$MANIFEST" "$(cd "$HERE/../../.." && pwd)"
read_lines HEALTH_ROWS manifest_files "$MANIFEST" all \
  || die "cannot enumerate the installed manifest"
while IFS=$'\t' read -r source target mode payload baseline; do
  [ -n "$source" ] || continue
  if [ ! -f "$target" ]; then
    bad "missing after install: $target"
  elif [ "$(sha256_of "$target")" = "$payload" ]; then
    ok "installed: $target"
  else
    bad "content does not match the package: $target"
  fi
done <<< "$HEALTH_ROWS"

say ""
say "deployed files this change must not touch"
verify_drift "$MANIFEST"

say ""
if [ "${EXPECT_ROUTE:-0}" = "1" ]; then
  say "gateway route"
  if grep -q "coding_handoff:" /srv/hermes/channel-gateway/config/config.yaml 2>/dev/null      && "$PY" -c 'import sys,yaml; d=yaml.safe_load(open("/srv/hermes/channel-gateway/config/config.yaml",encoding="utf-8")) or {}; sys.exit(0 if (d.get("coding_handoff") or {}).get("enabled") else 1)' 2>/dev/null; then
    ok "coding_handoff is enabled in the gateway configuration"
  else
    bad "the gateway configuration does not have coding_handoff enabled"
  fi
  say ""
fi

say "gateway import check"
# Proves the new modules load under the gateway's own interpreter, which is
# where a syntax error would otherwise surface as a crash loop.
GW_PY="/srv/hermes/channel-gateway/venv/bin/python"
if [ -x "$GW_PY" ]; then
  if PYTHONPATH=/srv/hermes/channel-gateway/app PYTHONDONTWRITEBYTECODE=1 \
     "$GW_PY" -c 'import gw_coding, gw_handoff, gw_handoff_client, gw_config, gw_service' \
     >/dev/null 2>&1; then
    ok "gateway modules import cleanly"
  else
    bad "gateway modules do not import — the service will crash-loop"
  fi
else
  warn "gateway venv not found; import check skipped"
fi

say ""
if [ "$FAILURES" -eq 0 ]; then
  say "${GREEN}health passed${PLAIN}"
  exit 0
fi
say "${RED}health failed: $FAILURES problem(s)${PLAIN}"
exit 1
