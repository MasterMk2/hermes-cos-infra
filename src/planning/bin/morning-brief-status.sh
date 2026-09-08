#!/usr/bin/env bash
# Morning Brief の現在状態とリソース実測。読み取りのみ。
#
# ブリーフ本文も資格情報も表示しない。本文はどこにも保存していない
# （phase section 66 により plan_hash だけを記録する）。
#
# 使い方:
#   sudo bash /srv/hermes/planning/bin/morning-brief-status.sh
#   sudo bash /srv/hermes/planning/bin/morning-brief-status.sh --measure
#
# --measure は phase sections 103/104 の測定を行う。実際に配信はしない
# （`run --dry-run` を使うため socket を一切開かない）:
#   planner RSS / renderer RSS / overall duration / events.db query latency
#   host RAM / swap delta

set -uo pipefail

APP_ROOT=/srv/hermes/planning
PY="$APP_ROOT/venv/bin/python"
# Same single location the service unit's ExecStart names. See the comment on
# `CONFIG=` in install-root.sh: reading a different path here would report the
# defaults while the service ran on the installed file, or the reverse.
CONF="${HERMES_PLANNING_CONFIG:-$APP_ROOT/config.yaml}"
STATE_DIR=/srv/hermes/hermes-data/planning
SOCK=/srv/hermes/channel-gateway/run/proactive.sock
PLANNER_USER=hermesplanner

MEASURE=0
[ "${1:-}" = "--measure" ] && MEASURE=1

[ -x "$PY" ]   || { echo "venv がありません: $PY  (install-root.sh を実行してください)" >&2; exit 1; }
[ -f "$CONF" ] || { echo "設定がありません: $CONF" >&2; exit 1; }

echo "== timer =="
systemctl list-timers hermes-morning-brief.timer --no-pager 2>/dev/null | sed 's/^/   /'
echo "   next(計算値): $(systemd-analyze calendar "$(systemctl show hermes-morning-brief.timer -p TimersCalendar --value 2>/dev/null | sed 's/.*OnCalendar=//; s/ }.*//')" 2>/dev/null | sed -n '2p' | sed 's/^ *//')"
echo

echo "== service (最後の実行) =="
systemctl show hermes-morning-brief.service \
  -p Result -p ExecMainStatus -p ExecMainStartTimestamp -p ExecMainExitTimestamp \
  -p NRestarts --no-pager 2>/dev/null | sed 's/^/   /'
echo "   直近ログ:"
journalctl -u hermes-morning-brief --no-pager -n 12 -o short-iso 2>/dev/null | sed 's/^/     /'
echo

echo "== health (phase section 105) =="
"$PY" -c '
import json, sys
path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
except Exception as exc:
    print("   読めません: %s (%s)" % (path, exc)); raise SystemExit
for k in ("status", "last_failure_reason", "last_plan_generated",
          "last_delivery_attempt", "last_delivery_success",
          "last_plan_hash", "updated_at"):
    print("   %-22s %s" % (k, d.get(k)))
' "$STATE_DIR/morning-brief-health.json"
echo

echo "== 配信記録 (ローカル控え。正は gateway 側) =="
"$PY" -c '
import sqlite3, sys
try:
    con = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
    rows = con.execute(
        "SELECT local_date, delivery_status, attempts, delivered_at, last_error"
        " FROM morning_brief_runs ORDER BY local_date DESC LIMIT 7").fetchall()
except Exception as exc:
    print("   読めません (%s)" % exc); raise SystemExit
if not rows:
    print("   まだ記録がありません")
for r in rows:
    print("   %s  %-11s attempts=%s  delivered_at=%s%s"
          % (r[0], r[1], r[2], r[3], ("  err=" + r[4]) if r[4] else ""))
' "$STATE_DIR/planning.db"
echo

echo "== gateway socket (token は表示されない) =="
if [ -S "$SOCK" ]; then
  echo "   $SOCK  $(stat -c '%U:%G %a' "$SOCK")"
  CODE="$(curl -s -m 5 -o /dev/null -w '%{http_code}' --unix-socket "$SOCK" \
          http://localhost/internal/health 2>/dev/null)"
  echo "   /internal/health -> ${CODE:-接続失敗}"
else
  echo "   $SOCK がありません (channel-gateway が停止中か proactive 無効)"
fi
echo

echo "== 権限境界 (phase section 69) =="
if id -u "$PLANNER_USER" >/dev/null 2>&1; then
  echo "   groups: $(id -nG "$PLANNER_USER")"
  for bad in docker sudo adm; do
    if id -nG "$PLANNER_USER" | tr ' ' '\n' | grep -qx "$bad"; then
      echo "   !! $bad に所属しています — section 69 違反"
    fi
  done
  for p in /srv/hermes/attention/state /srv/hermes/gmail/state \
           /srv/hermes/calendar/state /srv/hermes/channel-gateway/state \
           /run/docker.sock; do
    [ -e "$p" ] || continue
    if sudo -u "$PLANNER_USER" test -r "$p" 2>/dev/null; then
      echo "   !! 読めてしまいます: $p"
    else
      echo "   拒否 (正常): $p"
    fi
  done
else
  echo "   $PLANNER_USER が存在しません"
fi

[ "$MEASURE" -eq 0 ] && exit 0

echo
echo "== リソース実測 (phase sections 103, 104) =="
echo "   配信はしません (run --dry-run: socket を開きません)"

read_mem() { awk -v k="$1" '$1 == k":" {print $2}' /proc/meminfo; }
MEM_TOTAL="$(read_mem MemTotal)"
MEM_AVAIL_BEFORE="$(read_mem MemAvailable)"
SWAP_TOTAL="$(read_mem SwapTotal)"
SWAP_FREE_BEFORE="$(read_mem SwapFree)"

START_NS="$(date +%s%N)"
OUT="$(sudo -u "$PLANNER_USER" env PYTHONPATH="$APP_ROOT/app" \
        "$PY" -m planning.cli --config "$CONF" run --trigger manual \
        --dry-run --measure --json 2>/tmp/morning-brief-measure.err)"
RC=$?
END_NS="$(date +%s%N)"

MEM_AVAIL_AFTER="$(read_mem MemAvailable)"
SWAP_FREE_AFTER="$(read_mem SwapFree)"

echo "   exit code            $RC"
printf '   wall clock           %s ms\n' "$(( (END_NS - START_NS) / 1000000 ))"
if [ -n "$OUT" ]; then
  printf '%s' "$OUT" | "$PY" -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("   JSON を解釈できませんでした"); raise SystemExit
t = d.get("timings_ms") or {}
r = d.get("resources") or {}
print("   plan 生成             %s ms" % t.get("plan"))
print("   renderer              %s ms" % t.get("render"))
print("   合計 (プロセス内)     %s ms" % t.get("total"))
print("   events.db query       %s ms" % r.get("events_db_query_ms"))
print("   RSS 開始              %s kB" % r.get("rss_before_kb"))
print("   RSS ピーク            %s kB" % r.get("rss_peak_kb"))
print("   status                %s" % d.get("status"))
print("   本文 (未送信)         %s 文字" % d.get("text_chars"))
'
else
  echo "   出力がありません。エラー: $(head -5 /tmp/morning-brief-measure.err)"
fi
echo "   host RAM total        $((MEM_TOTAL / 1024)) MB"
echo "   host RAM available    $((MEM_AVAIL_BEFORE / 1024)) -> $((MEM_AVAIL_AFTER / 1024)) MB"
echo "   swap total            $((SWAP_TOTAL / 1024)) MB"
echo "   swap free 差分        $(( (SWAP_FREE_BEFORE - SWAP_FREE_AFTER) )) kB (正の値 = swap 増加)"
echo "   MemoryMax (unit)      $(systemctl show hermes-morning-brief.service -p MemoryMax --value 2>/dev/null)"
echo "   MemoryPeak (直近実行)  $(systemctl show hermes-morning-brief.service -p MemoryPeak --value 2>/dev/null)"
echo
echo "   注: renderer RSS は planner と同一プロセスのため、RSS ピークと"
echo "       plan 直後の RSS の差として観測する (--measure の brief 版:"
echo "       planning.cli brief --measure --json が rss_after_plan_kb を出す)。"
