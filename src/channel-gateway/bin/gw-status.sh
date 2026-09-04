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
# アドレスもポートも設定から読む。以前は 8790 を決め打ちし、失敗しても
# 「応答なし」としか言わなかった — 何が起きたのか分からない出力だった。
# HTTP コードは常に表示する。
CONF="$DEST/config/config.yaml"
HOST="$(sed -n 's/^[[:space:]]*listen_host:[[:space:]]*//p' "$CONF" 2>/dev/null | head -1)"
PORT="$(sed -n 's/^[[:space:]]*listen_port:[[:space:]]*//p' "$CONF" 2>/dev/null | head -1)"
URL="http://${HOST:-127.0.0.1}:${PORT:-8790}/healthz"
BODY="$(mktemp)"
CODE="$(curl -s -m 5 -o "$BODY" -w '%{http_code}' "$URL" 2>/dev/null)"
if [ "${CODE:-000}" = "200" ]; then
  echo "   $URL -> 200"
  sed 's/^/   /' "$BODY"; echo
else
  echo "   $URL -> ${CODE:-接続失敗}"
  echo "   (サービスが起動しているか、listen_host が実際の bind 先か確認)"
fi
rm -f "$BODY"
echo

# DB は 0750 サービスユーザー所有。root か channelgateway で実行する必要がある。
# 読めない場合、gw_status.py が理由と対処を印字する (黙って空にはしない)。
if [ "$(id -u)" -eq 0 ]; then
  sudo -u channelgateway "$PY" "$APP" "$@"
else
  "$PY" "$APP" "$@"
fi
