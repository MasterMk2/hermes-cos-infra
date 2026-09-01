#!/usr/bin/env bash
# watchdog: keep the coding bridge alive (Restart=on-failure equivalent).
# cron: @reboot + every minute. flock prevents duplicate starts.
set -u
BASE=/home/<PI_USER>/coding-worker
SOCK=$BASE/bridge-sock/bridge.sock
exec 9>"/tmp/hermes-bridge-watch.lock" 2>/dev/null || exit 0
flock -n 9 || exit 0

alive() {
  python3 - "$SOCK" <<'EOF'
import socket, sys, json
try:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect(sys.argv[1])
    s.sendall((json.dumps({"version": 1, "action": "worker_status"}) + "\n").encode())
    f = s.makefile("r")
    line = f.readline()
    resp = json.loads(line) if line else {}
    sys.exit(0 if resp.get("bridge") == "alive" else 1)
except Exception:
    sys.exit(1)
EOF
}

if ! alive; then
  # remove stale socket, restart bridge
  rm -f "$SOCK"; mkdir -p "$BASE/bridge-sock"
  cd "$BASE/bridge" || exit 0
  nohup python3 "$BASE/bridge/bridge.py" >> "$BASE/logs/bridge.log" 2>&1 &
  echo "$(date -u +%FT%TZ) watchdog restarted bridge (pid $!)" >> "$BASE/logs/bridge.log"
fi
