#!/usr/bin/env bash
# Operator-side status for the Discord Knowledge subsystem.
#
# Prints the systemd state, the collector's own health record, the storage
# counters and the index lag. It prints no message content and no secret; the
# token file is reported by permission bits only, never by contents.
set -uo pipefail

APP_ROOT="/srv/hermes/discord-knowledge"
SOURCE_DB="/srv/hermes/knowledge/discord/messages.db"
INDEX_DB="/srv/hermes/hermes-data/knowledge-index/discord/index.db"
TOKEN_FILE="/etc/hermes-secrets/discord-knowledge-token"

echo "== service =="
if systemctl list-unit-files discord-knowledge.service >/dev/null 2>&1; then
  systemctl --no-pager --lines=0 status discord-knowledge 2>&1 | head -5
else
  echo "  unit not installed (install-root.sh has not been run)"
fi

echo
echo "== credential =="
if [[ -e "$TOKEN_FILE" ]]; then
  echo "  $TOKEN_FILE  $(stat -c '%U:%G %a' "$TOKEN_FILE")  $(stat -c '%s' "$TOKEN_FILE") bytes"
  echo "  (contents never read or printed by this script)"
else
  echo "  absent: $TOKEN_FILE"
fi

echo
echo "== source database =="
if [[ -f "$SOURCE_DB" ]]; then
  python3 - "$SOURCE_DB" <<'PY'
import sqlite3, sys
con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
def one(sql, *a):
    try:
        return con.execute(sql, a).fetchone()[0]
    except sqlite3.Error:
        return "n/a"
print(f"  messages live    : {one('SELECT COUNT(*) FROM messages WHERE deleted_at IS NULL')}")
print(f"  messages deleted : {one('SELECT COUNT(*) FROM messages WHERE deleted_at IS NOT NULL')}")
print(f'  with content     : {one("SELECT COUNT(*) FROM messages WHERE deleted_at IS NULL AND content IS NOT NULL AND content <> \'\'")}')
print(f"  index queue      : {one('SELECT COUNT(*) FROM index_queue')}")
print(f"  oldest / newest  : {one('SELECT MIN(created_at) FROM messages')} / {one('SELECT MAX(created_at) FROM messages')}")
try:
    for r in con.execute("SELECT guild_id, MAX(guild_name), COUNT(*) FROM messages GROUP BY guild_id"):
        print(f"  guild {r[0]} {r[1] or ''}: {r[2]}")
except sqlite3.Error:
    pass
print("  --- collector state ---")
try:
    for r in con.execute("SELECT key, value FROM ingest_state ORDER BY key"):
        if r[0] in ("collector_health","gateway_connected","last_ready_at","last_event_at",
                    "last_successful_ingest_at","last_error_summary","queue_depth",
                    "session_resumed_count","full_reconnect_count","rate_limit_hits",
                    "messages_with_content","library","intents_value"):
            print(f"  {r[0]:<28} {str(r[1])[:90]}")
except sqlite3.Error:
    print("  (no ingest_state yet)")
PY
else
  echo "  absent: $SOURCE_DB (the collector has never run)"
fi

echo
echo "== search index =="
if [[ -f "$INDEX_DB" ]]; then
  python3 - "$INDEX_DB" <<'PY'
import sqlite3, sys
con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
def one(sql):
    try:
        return con.execute(sql).fetchone()[0]
    except sqlite3.Error:
        return "n/a"
print(f"  rows      : {one('SELECT COUNT(*) FROM messages')} ({one('SELECT COUNT(*) FROM messages WHERE is_deleted=0')} live)")
print(f"  fts rows  : {one('SELECT COUNT(*) FROM fts')}")
meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
for k in ("last_indexed", "index_lag", "collector_health", "gateway_connected"):
    print(f"  {k:<16}: {meta.get(k, '-')}")
PY
else
  echo "  absent: $INDEX_DB (nothing indexed yet)"
fi
