#!/usr/bin/env bash
# Drain the Discord index queue and republish the status snapshot.
#
# Runs as the OPERATOR account from cron. It is the only writer of the search
# index, which is why the collector does not need write access to
# /srv/hermes/hermes-data at all.
#
# Safe to run concurrently with the collector: the source database is opened
# with a 30s busy timeout and the queue rows are deleted only after the index
# transaction has committed, so a collision retries rather than losing work.
# Overlapping *indexer* runs are prevented by flock.
set -euo pipefail

APP_ROOT="/srv/hermes/discord-knowledge"
LOCK="${APP_ROOT}/state/indexer.lock"
LOG="${APP_ROOT}/logs/index-tick.log"

# cron mails stderr; a bare `exit 0` here would mean the index silently stops
# advancing while every minute reports success. Only the "another tick is
# still running" case is a legitimate quiet skip.
fail() { echo "index-tick: $*" >&2; exit 1; }

[[ -x "${APP_ROOT}/venv/bin/python" ]] || fail "venv python missing at ${APP_ROOT}/venv/bin/python"
[[ -f "${APP_ROOT}/config/config.yaml" ]] || fail "config missing at ${APP_ROOT}/config/config.yaml"

mkdir -p "$(dirname "$LOCK")" "$(dirname "$LOG")" 2>/dev/null || true
[[ -w "$(dirname "$LOCK")" ]] || fail "state dir not writable: $(dirname "$LOCK")"
[[ -d "$(dirname "$LOG")" && -w "$(dirname "$LOG")" ]] || fail "log dir not writable: $(dirname "$LOG")"

exec 9>"$LOCK" || fail "cannot open lock file ${LOCK}"
flock -n 9 || exit 0   # a previous tick is still running; a real quiet skip

{
  date -u +'%Y-%m-%dT%H:%M:%SZ'
  "${APP_ROOT}/venv/bin/python" "${APP_ROOT}/app/indexer.py" \
    --config "${APP_ROOT}/config/config.yaml" 2>&1 || echo "indexer exit=$?"
} >>"$LOG" 2>&1

# Keep the tick log bounded; it is one line per minute of counters.
if [[ -f "$LOG" ]] && (( $(stat -c '%s' "$LOG") > 2000000 )); then
  tail -n 2000 "$LOG" >"${LOG}.tmp" && mv "${LOG}.tmp" "$LOG"
fi
