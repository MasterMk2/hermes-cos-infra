#!/usr/bin/env bash
# reconcile-line.sh — Downstream event extraction and commitment reconciliation for LINE.
# Triggered automatically after a successful LINE snapshot & index refresh.
# Flow:
#   1. Lock acquisition (/run/hermes/line-reconcile.lock or /srv/hermes/hermes-data/events/line-reconcile.lock)
#   2. Precondition check: verify LINE index and successful refresh status
#   3. Stage 1: Incremental event extraction (event_extract.py)
#   4. Stage 2: Commitment reconciliation (commitments.admin backfill)
#   5. Progress health tracking (/srv/hermes/hermes-data/events/line-reconcile-health.json)
# Exit codes: 0 ok | 1 runtime error | 10 locked | 11 precondition failed

set -euo pipefail

EVENTS_DIR=/srv/hermes/hermes-data/events
TOOLS_DIR=/srv/hermes/hermes-data/tools
LOG_DIR=/srv/hermes/hermes-data/logs
LOG="${LOG_DIR}/line-reconcile.log"
LOCK_DIR=/run/hermes
if [[ -d "$LOCK_DIR" && -w "$LOCK_DIR" ]]; then
  LOCK="${LOCK_DIR}/line-reconcile.lock"
else
  LOCK="${EVENTS_DIR}/line-reconcile.lock"
fi
HEALTH_FILE="${EVENTS_DIR}/line-reconcile-health.json"
REFRESH_LOG="${LOG_DIR}/line-refresh.log"
SNAPSHOT=/srv/hermes/knowledge/line/messages.jsonl
INDEX_DB=/srv/hermes/hermes-data/knowledge-index/line/index.db
EVENTS_DB="${EVENTS_DIR}/events.db"
STATE_FILE="${EVENTS_DIR}/state.json"

mkdir -p "$EVENTS_DIR" "$LOG_DIR"

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "reconcile-line: another run holds the lock" >&2
  exit 10
fi

log() { printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >> "$LOG"; }

START_TIME_MS=$(python3 -c "import time; print(int(time.time() * 1000))")
T_START="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
log "reconcile start"

# 1. Precondition checks
if [[ ! -f "$INDEX_DB" ]]; then
  log "precondition failed: $INDEX_DB missing"
  exit 11
fi

if [[ -f "$REFRESH_LOG" ]]; then
  LAST_REFRESH_LINE=$(tail -n 20 "$REFRESH_LOG" | grep "refresh " | tail -n 1 || true)
  if [[ "$LAST_REFRESH_LINE" == *"refresh FAILED"* ]]; then
    log "precondition failed: latest refresh status is FAILED"
    exit 11
  fi
fi

ERRORS=0
EE_ERRORS=0
CMT_ERRORS=0
T_EXTRACT=""
T_RECONCILE=""
EE_PROCESSED=0
EE_NEW=0
CMT_PROCESSED=0
CMT_ADDED=0
CMT_UPDATED=0

# 2. Stage 1: Event extraction
log "stage 1: event extraction start"
EVENT_EXTRACT="${TOOLS_DIR}/events/event_extract.py"
if [[ -f "$EVENT_EXTRACT" ]]; then
  # Record state mtime before run to detect new state
  PREV_STATE_MTIME=0
  if [[ -f "$STATE_FILE" ]]; then
    PREV_STATE_MTIME=$(stat -c %Y "$STATE_FILE" 2>/dev/null || echo 0)
  fi

  if python3 "$EVENT_EXTRACT" --max-candidates 100 --max-llm-calls 12 >> "$LOG" 2>&1; then
    T_EXTRACT="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    log "stage 1: event extraction ok"
    if [[ -f "$STATE_FILE" ]]; then
      NEW_STATE_MTIME=$(stat -c %Y "$STATE_FILE" 2>/dev/null || echo 0)
      if [[ "$NEW_STATE_MTIME" -ge "$PREV_STATE_MTIME" ]]; then
        EE_STATS=$(python3 -c "
import json
try:
    with open('$STATE_FILE') as f:
        d = json.load(f)
    print(f\"{d.get('classified', 0)}|{d.get('events_created', 0)}\")
except Exception:
    print('0|0')
")
        EE_PROCESSED=$(echo "$EE_STATS" | cut -d'|' -f1)
        EE_NEW=$(echo "$EE_STATS" | cut -d'|' -f2)
      fi
    fi
  else
    ERRORS=$((ERRORS + 1))
    EE_ERRORS=$((EE_ERRORS + 1))
    log "stage 1: event extraction failed"
  fi
else
  ERRORS=$((ERRORS + 1))
  EE_ERRORS=$((EE_ERRORS + 1))
  log "stage 1: $EVENT_EXTRACT missing"
fi

# 3. Stage 2: Commitment reconciliation
log "stage 2: commitment reconciliation start"
BACKFILL_OUT=$(mktemp)
if PYTHONPATH=/srv/hermes/hermes-data/tools:/srv/hermes/hermes-data python3 -m commitments.admin backfill > "$BACKFILL_OUT" 2>&1; then
  cat "$BACKFILL_OUT" >> "$LOG"
  T_RECONCILE="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  log "stage 2: commitment reconciliation ok"
  # Parse backfill output (e.g. processed_events, calendar_events_added)
  CMT_STATS=$(python3 -c "
import json
try:
    with open('$BACKFILL_OUT') as f:
        data = json.load(f)
    processed = data.get('processed_events', 0)
    cal_added = data.get('calendar_events_added', 0)
    print(f\"{processed}|{cal_added}|0\")
except Exception:
    print('0|0|0')
" 2>/dev/null || echo "0|0|0")
  CMT_PROCESSED=$(echo "$CMT_STATS" | cut -d'|' -f1)
  CMT_ADDED=$(echo "$CMT_STATS" | cut -d'|' -f2)
  CMT_UPDATED=$(echo "$CMT_STATS" | cut -d'|' -f3)
else
  cat "$BACKFILL_OUT" >> "$LOG"
  ERRORS=$((ERRORS + 1))
  CMT_ERRORS=$((CMT_ERRORS + 1))
  log "stage 2: commitment reconciliation failed"
fi
rm -f "$BACKFILL_OUT"

# 4. Progress health update
T_END="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
END_TIME_MS=$(python3 -c "import time; print(int(time.time() * 1000))")
DURATION_MS=$((END_TIME_MS - START_TIME_MS))

T_SNAP=""
T_IDX=""
if [[ -f "$SNAPSHOT" ]]; then
  T_SNAP="$(date -u -r "$SNAPSHOT" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || true)"
fi
if [[ -f "$INDEX_DB" ]]; then
  T_IDX="$(date -u -r "$INDEX_DB" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || true)"
fi

STATUS="OK"
if [[ $ERRORS -gt 0 ]]; then
  if [[ -n "$T_RECONCILE" || -n "$T_EXTRACT" ]]; then
    STATUS="PARTIAL"
  else
    STATUS="FAILED"
  fi
fi

cat <<EOF > "${HEALTH_FILE}.tmp"
{
  "last_started_at": "${T_START}",
  "last_finished_at": "${T_END}",
  "status": "${STATUS}",
  "snapshot_timestamp": "${T_SNAP}",
  "index_timestamp": "${T_IDX}",
  "event_extract_processed": ${EE_PROCESSED},
  "event_extract_new": ${EE_NEW},
  "event_extract_errors": ${EE_ERRORS},
  "commitment_processed": ${CMT_PROCESSED},
  "commitment_added": ${CMT_ADDED},
  "commitment_updated": ${CMT_UPDATED},
  "commitment_errors": ${CMT_ERRORS},
  "duration_ms": ${DURATION_MS},
  "last_event_extract": "${T_EXTRACT}",
  "last_commitment_reconcile": "${T_RECONCILE}",
  "errors": ${ERRORS},
  "updated_at": "${T_END}"
}
EOF
mv -f "${HEALTH_FILE}.tmp" "$HEALTH_FILE"
chmod 0644 "$HEALTH_FILE" 2>/dev/null || true

# Keep log bounded (< 2MB)
if [[ -f "$LOG" ]] && (( $(stat -c '%s' "$LOG" 2>/dev/null || wc -c < "$LOG") > 2000000 )); then
  tail -n 2000 "$LOG" > "${LOG}.tmp" && mv -f "${LOG}.tmp" "$LOG"
fi

if [[ $ERRORS -gt 0 ]]; then
  log "reconcile completed with errors ($STATUS)"
  exit 1
fi

log "reconcile ok"
exit 0
