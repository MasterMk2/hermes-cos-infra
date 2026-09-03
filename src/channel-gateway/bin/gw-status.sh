#!/usr/bin/env bash
# What the gateway is doing right now — for a human, at a glance.
#
# Reads only. Prints no message body and no credential: queue depths, states,
# counters and the last few audit categories. If you want to know what someone
# actually said, that is deliberately not here.

set -uo pipefail

DEST=/srv/hermes/channel-gateway
DB="$DEST/state/gateway.db"

echo "== unit =="
systemctl is-active channel-gateway 2>/dev/null | sed 's/^/  active: /'
systemctl show channel-gateway -p NRestarts -p ExecMainStartTimestamp \
  -p Result --no-pager 2>/dev/null | sed 's/^/  /'

echo
echo "== listener =="
# The reverse proxy reaches this port from another host on the LAN, so a
# loopback-only bind here means the webhook is unreachable even though the
# service looks healthy. Show what it actually bound.
ss -ltn 2>/dev/null | awk 'NR==1 || /:8790/' | sed 's/^/  /'

echo
echo "== queue =="
if [ ! -f "$DB" ]; then
  echo "  no database yet at $DB"
  exit 0
fi
sqlite3 -readonly "$DB" <<'SQL' 2>/dev/null | sed 's/^/  /'
.mode column
.headers on
SELECT state, COUNT(*) AS n FROM inbound_messages GROUP BY state ORDER BY n DESC;
SQL

echo
echo "== outbound =="
sqlite3 -readonly "$DB" <<'SQL' 2>/dev/null | sed 's/^/  /'
.mode column
.headers on
SELECT platform, delivery_mode, state, COUNT(*) AS n
  FROM outbound_messages GROUP BY platform, delivery_mode, state ORDER BY n DESC;
SQL

echo
echo "== LINE push usage (metered: 200/month on the free plan) =="
sqlite3 -readonly "$DB" <<'SQL' 2>/dev/null | sed 's/^/  /'
.mode column
.headers on
SELECT strftime('%Y-%m', sent_at) AS month, COUNT(*) AS pushes
  FROM outbound_messages
 WHERE platform = 'line' AND delivery_mode = 'push' AND state = 'SENT'
 GROUP BY month ORDER BY month DESC LIMIT 3;
SQL

echo
echo "== recent audit categories (no message content) =="
sqlite3 -readonly "$DB" <<'SQL' 2>/dev/null | sed 's/^/  /'
.mode column
.headers on
SELECT at, event, platform, COALESCE(detail,'') AS detail
  FROM audit_events ORDER BY id DESC LIMIT 12;
SQL

echo
echo "== last errors =="
sqlite3 -readonly "$DB" <<'SQL' 2>/dev/null | sed 's/^/  /'
.mode column
.headers on
SELECT updated_at, platform, state, COALESCE(last_error,'') AS last_error
  FROM inbound_messages WHERE last_error IS NOT NULL
 ORDER BY updated_at DESC LIMIT 5;
SQL
