#!/usr/bin/env bash
# refresh-line.sh — single manual entry point for LINE knowledge refresh.
# Flow: SSH snapshot -> snapshot validation -> index build -> index validation.
# Any failure stops the flow and keeps the previous good index.
# Exit codes: 0 ok | 3 ssh/token | 4 remote | 5 snapshot invalid | 6 index invalid | 10 locked
set -euo pipefail

# SSH alias from ~/.ssh/config (override with MYLINE_SSH_HOST).
# Pi migration: set MYLINE_SSH_HOST to your pre-existing SSH alias before running.
export MYLINE_SSH_HOST="${MYLINE_SSH_HOST:-line-source}"

TOOL_DIR=/srv/hermes/hermes-data/tools/line-knowledge
LOG=/srv/hermes/hermes-data/logs/line-refresh.log
LOCK=/tmp/line-refresh.lock
SRC=/srv/hermes/knowledge/line/messages.jsonl
IDX=/srv/hermes/hermes-data/knowledge-index/line/index.db

mkdir -p "$(dirname "$LOG")"
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "refresh-line: another run holds the lock" >&2
  exit 10
fi

log() { printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >> "$LOG"; }

log "refresh start"
trap 'log "refresh FAILED" >&2' ERR

python3 "$TOOL_DIR/refresh_from_myline.py" 2>&1 | tee -a "$LOG"
python3 "$TOOL_DIR/validate_snapshot.py" "$SRC" 2>&1 | tee -a "$LOG"
python3 "$TOOL_DIR/build_index.py" 2>&1 | tee -a "$LOG"
python3 "$TOOL_DIR/validate_index.py" "$IDX" 2>&1 | tee -a "$LOG"

# Event extraction (Stage 1 deterministic + Stage 2 LLM, candidates only).
# When running under systemd sandbox (ProtectSystem=strict), /srv/hermes/hermes-data/events
# is read-only and docker.sock is inaccessible by design; event extraction runs
# when executed with write permissions (e.g. operator/manual or dedicated trigger).
EVENT_EXTRACT=/srv/hermes/hermes-data/tools/events/event_extract.py
if [[ -f "$EVENT_EXTRACT" && -w /srv/hermes/hermes-data/events ]]; then
  if ! python3 "$EVENT_EXTRACT" >> "$LOG" 2>&1; then
    log "event extraction failed (non-fatal)"
  fi
else
  log "events dir read-only or not writable (event extraction skipped in sandbox)"
fi

END=$(date +%s)
log "refresh ok"
printf 'refresh-line: OK (log: %s)\n' "$LOG"
exit 0
