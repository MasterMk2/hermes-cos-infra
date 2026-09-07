#!/usr/bin/env bash
# Daily LINE knowledge refresh (LLM-free). Cron --no-agent wrapper.
# Silent on success (empty stdout = no delivery); prints only on failure.
set -uo pipefail

# Pi migration: export MYLINE_SSH_HOST with your SSH alias, or rename the alias to line-source.
export MYLINE_SSH_HOST="${MYLINE_SSH_HOST:-line-source}"

# If running inside Hermes container (/opt/data) where host SSH is not mounted,
# verify that the host-level scheduled refresh (systemd hermes-line-refresh.timer)
# succeeded and that the index remains fresh.
if [[ ! -d /srv/hermes && -d /opt/data ]]; then
  DATA_DIR=/opt/data
  LOG="${DATA_DIR}/logs/line-refresh.log"
  IDX="${DATA_DIR}/knowledge-index/line/index.db"

  # Check that the index exists
  if [[ ! -f "$IDX" ]]; then
    echo "LINE refresh failed: index.db missing"
    exit 1
  fi

  # Check that index was modified within 48h (172800s)
  NOW=$(date +%s)
  MTIME=$(stat -c %Y "$IDX" 2>/dev/null || stat -f %m "$IDX" 2>/dev/null || echo 0)
  AGE=$(( NOW - MTIME ))
  if (( AGE > 172800 )); then
    echo "LINE refresh failed: index.db is stale (${AGE}s old) - see hermes-data/logs/line-refresh.log"
    exit 1
  fi

  # Check that log does not terminate in a failure
  if [[ -f "$LOG" ]]; then
    LAST_STATUS=$(tail -n 20 "$LOG" | grep -E "refresh (ok|FAILED)" | tail -n 1 || true)
    if [[ "$LAST_STATUS" == *"refresh FAILED"* ]]; then
      echo "LINE refresh failed - see hermes-data/logs/line-refresh.log"
      exit 1
    fi
  fi

  exit 0
fi

# If running on host (interactive or host scheduler):
HOST_REFRESH="/srv/hermes/hermes-data/tools/line-knowledge/refresh-line.sh"
if [[ -f "$HOST_REFRESH" ]]; then
  if bash "$HOST_REFRESH" >/dev/null 2>&1; then
    exit 0
  fi
fi

echo "LINE refresh failed - see hermes-data/logs/line-refresh.log"
exit 1
