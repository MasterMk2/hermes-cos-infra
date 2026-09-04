#!/usr/bin/env bash
# Attention Engine の現在状態。読み取りのみ。
# 通知本文も資格情報も表示しない（本文は attention.db 内にのみ存在する）。

set -uo pipefail
DEST=/srv/hermes/attention
PY="$DEST/venv/bin/python"
APP="$DEST/app/att_status.py"
CONF="$DEST/config/config.yaml"

[ -x "$PY" ]  || { echo "venv がありません: $PY" >&2; exit 1; }
[ -f "$APP" ] || { echo "未配置です: $APP  (deploy.sh を実行してください)" >&2; exit 1; }

echo "== unit =="
systemctl is-active attention-engine 2>/dev/null | sed 's/^/   active: /'
systemctl show attention-engine -p NRestarts -p Result -p ExecMainStartTimestamp \
  -p MemoryCurrent --no-pager 2>/dev/null | sed 's/^/   /'
echo

echo "== gateway socket =="
# 「ファイルがある」ではなく「実際に応答する」ことを見る。
SOCK="$(sed -n 's/^[[:space:]]*socket_path:[[:space:]]*//p' "$CONF" 2>/dev/null | head -1)"
SOCK="${SOCK:-/srv/hermes/channel-gateway/run/proactive.sock}"
if [ -S "$SOCK" ]; then
  CODE="$(curl -s -m 5 -o /dev/null -w '%{http_code}' --unix-socket "$SOCK" \
          http://localhost/internal/health 2>/dev/null)"
  echo "   $SOCK -> ${CODE:-接続失敗}"
else
  echo "   $SOCK がありません (gateway 側の proactive が無効か未起動)"
fi
echo

echo "== LINE 予算 (gateway 経由・token は表示されない) =="
if [ -S "$SOCK" ]; then
  curl -s -m 20 --unix-socket "$SOCK" http://localhost/internal/line_budget \
    2>/dev/null | "$PY" -c '
import json,sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("   取得できませんでした"); raise SystemExit
for k in ("ok","quota_type","limit","used","remaining","unlimited","stale",
          "error_category","fetched_at"):
    print("   %-16s %s" % (k, d.get(k)))
print("   %-16s %s" % ("ledger", d.get("ledger")))
'
else
  echo "   (socket なし)"
fi
echo

DB="$(sed -n 's/^[[:space:]]*state_db:[[:space:]]*//p' "$CONF" 2>/dev/null | head -1)"
exec "$PY" "$APP" --db "${DB:-$DEST/state/attention.db}" "$@"
