#!/usr/bin/env bash
# ゲートウェイの現在状態。読み取りのみ。本文も資格情報も表示しない。
#
# 実体は app/gw_status.py。sqlite3 コマンドは対象ホストに入っていないため
# 使わない — 以前のバージョンはそれに依存していて、何も言わずに空の表を
# 出していた。venv の Python はサービス本体が使うので必ず存在する。

set -uo pipefail
DEST=/srv/hermes/channel-gateway
PY="$DEST/venv/bin/python"
APP="$DEST/app/gw_status.py"

[ -x "$PY" ]  || { echo "venv がありません: $PY" >&2; exit 1; }
[ -f "$APP" ] || { echo "未配置です: $APP  (deploy.sh を実行してください)" >&2; exit 1; }

echo "== unit =="
systemctl is-active channel-gateway 2>/dev/null | sed 's/^/   active: /'
systemctl show channel-gateway -p NRestarts -p Result -p ExecMainStartTimestamp \
  --no-pager 2>/dev/null | sed 's/^/   /'
echo

echo "== listener =="
# リバースプロキシは別ホストから届くので、ループバックだけに bind していると
# サービスが健全に見えても webhook は届かない。実際の bind 先を出す。
ss -ltn 2>/dev/null | awk 'NR==1 || /:8790/' | sed 's/^/   /'
echo

echo "== healthz =="
curl -s -m 5 "http://$(awk '/listen_host:/{print $2}' "$DEST/config/config.yaml" 2>/dev/null):8790/healthz" 2>/dev/null \
  | sed 's/^/   /' || echo "   応答なし"
echo
echo

# DB は 0750 サービスユーザー所有。root か channelgateway で実行する必要がある。
# 読めない場合、gw_status.py が理由と対処を印字する (黙って空にはしない)。
if [ "$(id -u)" -eq 0 ]; then
  sudo -u channelgateway "$PY" "$APP" "$@"
else
  "$PY" "$APP" "$@"
fi
