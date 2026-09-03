#!/usr/bin/env bash
# Classify new Discord messages into the Event Layer, a bounded batch at a time.
#
# Runs as the OPERATOR account from cron, NOT as the collector's service user:
# classification shells into the assistant's container, and giving the
# collector that reach would hand it the assistant's own credentials.
#
# Why this exists at all: the extractor's candidate window is the last N days.
# Without a schedule, every day that passes permanently disqualifies another
# day of messages from ever becoming an event — silently, because a message
# that ages out is simply never a candidate. Running it by hand is not a plan.
#
# Bounded on purpose: at most --max-llm-calls model calls per tick, so a large
# backlog is worked off over several ticks instead of in one unbounded burst.
# Zero candidates means zero calls.
set -euo pipefail

APP_ROOT="/srv/hermes/discord-knowledge"
EVENTS_DIR="/srv/hermes/hermes-data/tools/events"
LOCK="${APP_ROOT}/state/event-tick.lock"
LOG="${APP_ROOT}/logs/event-tick.log"
MAX_CALLS="${DK_EVENT_MAX_CALLS:-12}"
MAX_CANDIDATES="${DK_EVENT_MAX_CANDIDATES:-100}"

fail() { echo "event-tick: $*" >&2; exit 1; }

[[ -f "${EVENTS_DIR}/discord_event_extract.py" ]] || fail "extractor missing"
[[ -f "${APP_ROOT}/config/config.yaml" ]] || fail "config missing"

mkdir -p "$(dirname "$LOCK")" "$(dirname "$LOG")" 2>/dev/null || true
[[ -w "$(dirname "$LOCK")" ]] || fail "state dir not writable"

exec 9>"$LOCK" || fail "cannot open lock ${LOCK}"
# A tick can run for many minutes (one model call per candidate), so an
# overlapping start is expected and skipping is correct.
flock -n 9 || exit 0

{
  date -u +'%Y-%m-%dT%H:%M:%SZ'
  cd "$EVENTS_DIR"
  DISCORD_KNOWLEDGE_APP_DIR="${APP_ROOT}/app" \
    python3 "${EVENTS_DIR}/discord_event_extract.py" \
      --config "${APP_ROOT}/config/config.yaml" \
      --max-candidates "$MAX_CANDIDATES" \
      --max-llm-calls "$MAX_CALLS" 2>&1 || echo "extractor exit=$?"
} >>"$LOG" 2>&1

# Keep the tick log bounded: it is one summary line per run, but it runs
# forever on a USB flash volume.
if [[ -f "$LOG" ]] && (( $(stat -c '%s' "$LOG") > 2000000 )); then
  tail -n 2000 "$LOG" >"${LOG}.tmp" && mv "${LOG}.tmp" "$LOG"
fi
