# 引継ぎ: Hermes 応答タイムアウト（2026-10-27）

クラウドのセッションから Pi 上のローカルセッションへの引継ぎ。経緯の詳細は
[HERMES-TIMEOUTS.md](HERMES-TIMEOUTS.md)。ここには「何が分かっていて、何が残っているか」と、
次にやる順番だけを書く。オーナーへの返答は日本語で。

## 最初にやること

```sh
cd <hermes-cos-infra の checkout>
git fetch origin claude/charming-thompson-3eaanv
git switch claude/charming-thompson-3eaanv   # PR #9 のブランチ
```

その上で、このファイルと `docs/HERMES-TIMEOUTS.md` を読む。PR は
<https://github.com/MasterMk2/hermes-cos-infra/pull/9>（draft、CI 緑、マージ可能、レビューコメントなし）。

## 症状

2026-10-27 13:11 / 13:12 / 14:49（JST）の Discord DM に、時間切れの定型文が返った。attention は
「gateway: Hermes が応答を返せていません」を送り、約 1 時間後に「復旧」と通知したが、Hermes は答えていなかった。

## 確定した事実（Pi での診断、2026-10-27 23:31 JST）

1. **3 件とも Hermes の run 自体が失敗していた**（`GET /v1/runs/<id>` が `status=failed`、474 / 625 / 497 s）。
   Gateway が 240 s で諦めたので時間切れに見えただけで、待っても答えは無かった。
2. **主経路**: Hermes → Antigravity bridge（`gemini-3.8-flash-medium`）のツール付き呼び出しが毎回 120 s で
   打ち切られた（10-27 は 10 回中 10 回）。Hermes はそれを 3 回 retry してから fallback に移るので、そこまでに約 6.4 分かかる。
3. **fallback が全部壊れている**: `commandcode / z-ai/glm-5.3-flash` は `401 Invalid 'Authorization' header or token`。
   `opencode-free` の `laguna-s-2.1-free` と `deepseek-v4-flash-free` は `400 Model is unavailable`。
4. **ツール付き呼び出しは、1 行の質問でも 162 s かかる**。オーナーが手で計測した（`real 2m41s`、
   `user+sys` は約 34 s、`exit=0`）。ツールなしの呼び出し（分類 broker）は 24〜42 s。
   つまり、会話の長さ（約 3 万 token）が主因ではなく、ツール（MCP）の実行が遅い。
5. **`/srv/hermes` は USB メモリ（ELECOM MF-PKU3、usb-storage、write cache 無効）で、非常に遅い**。
   4 KiB の直接読み出しに 0.65〜1.07 s かかり、jbd2 などが D 状態だった。attention の scan は日ごとの p90 が 8〜33 s。
   gateway の Discord heartbeat も 14:53 に 30 s 止まった。MCP サーバー本体（`/srv/hermes/antigravity/venv`、
   `/srv/hermes/antigravity/mcp/...`）も、読む DB（calendar.db、events.db など）も、すべてこのディスクにある。
   ディスクが原因の可能性は高いが、**まだ確定していない**（下の「未確認」1）。
6. **「復旧」は誤報だった**: 最後の成功（11:35:52）のまま、3600 s の判定窓が切れただけで回復扱いになっていた。PR #9 で修正済み。
7. **2 通目が待たされた理由**: 2 通目は Gateway 側では 1 通目の後ろに並び、Hermes 側でも
   「Another Hermes process is using this session」で、捨てられた 1 通目の run が終わるまで待たされていた。

### 本番の構成（診断で見えたもの）

- **本番のコードは GitHub と違う**。597c897 と比べると、gateway の `gw_canonical / gw_config / gw_discord / gw_hermes /
  gw_proactive / gw_schema / gw_service / gw_store` と bridge の `bridge_server / bridge_config` が DIFF だった。
  mtime は 9 月 4〜10 日。
- 本番にしか無いファイル: `channel-gateway/app/gw_coding.py`、`gw_handoff.py`、`gw_handoff_client.py`、
  `antigravity/app/premium_routing.py`。本番にしか無い service: `hermes-premium-cli`、`hermes-coding-handoff`。
  drop-in: `channel-gateway.service.d/10-coding-handoff.conf`、`hermes-antigravity.service.d/20-docker-order.conf`。
  [STATUS-RECONCILIATION.md](STATUS-RECONCILIATION.md) にある 9 月 10 日のローカル作業（`fix/owner-task-routing` など）と思われる。
- Gateway の `config.yaml`: `hermes.absolute_timeout_seconds = 240`、`limits.max_active_requests = 2`、discord / line / proactive は有効
- `bridge.yaml`: `timeout_seconds = 120`、`allow_delegated_mcp = True`
- Hermes（`/srv/hermes/hermes-data/config.yaml`、version 0.20.6）:
  - `session_reset.mode = none`、`compression.threshold = 0.5` なので、会話は縮まない。失敗時の request dump は約 156 KB。
  - `fallback_providers` は上の 3 つ（`fallback_model` も同じ内容）。
  - Hermes の fallback のドキュメントには、主経路の retry 回数を変える設定キーが書かれていない。
- 診断スクリプト（S3）の per-day 集計の KeyError は修正済み（10ce947）。

## 未確認（次に確かめる）

1. **ディスクが原因かどうか**: 162 s の計測コマンドを、続けてもう一度実行する。2 回目が大きく速くなれば
   （ページキャッシュが効くので）ディスクが原因と確定する。コマンドは HERMES-TIMEOUTS.md と同じもの:
   ```sh
   sudo -u hermesantigravity env HOME=/home/hermesantigravity bash -c 'cd ~ && time timeout 300 /srv/hermes/antigravity/bin/antigravity --print "[System Instruction] Use ONLY the read-only MCP tools. Do NOT call filesystem or shell tools. User: 今日のカレンダーの予定を一行で教えて" --model gemini-3.8-flash-medium --output-format json --disable-slash-commands </dev/null >/dev/null 2>&1; echo "exit=$?"'
   ```
   出力は捨てているので、回答の内容は表示されない。Antigravity を 1 回使う。
2. **9 月 10 日のローカル作業の在りか**: `/srv` の外に git の作業コピーが残っているか。
   `sudo find / -name gw_handoff.py -not -path '/srv/*' -not -path '/proc/*' 2>/dev/null` の結果はまだ確認できていない。
3. **本番の bridge / gateway が設定値に上限を設けていないか**:
   `grep -n timeout_seconds /srv/hermes/antigravity/app/*.py`、`grep -n absolute_timeout /srv/hermes/channel-gateway/app/gw_config.py`

## 次の作業（この順で）

### 1. 設定だけでできる応急処置（コードの配備は不要）
- **bridge の `timeout_seconds` を 120 → 240 にする**（`/srv/hermes/antigravity/config/bridge.yaml`）。
  未確認 3 で上限が無いことを確かめてから変え、`sudo systemctl restart hermes-antigravity` を実行し、
  `curl -s http://172.18.0.1:8765/health` で確認する。
  - 根拠: ツール付き呼び出しが最短でも 162 s かかるので、120 s では一度も成功しない。
  - 代償: 本当に固まった呼び出しは 240 s × 3 回まで待たされる。分類 broker は自前の 45 s で切るので影響しない。
- 今の Gateway は 240 s で諦める。Hermes の準備（約 40 s）とツール呼び出し（約 162 s）で約 200 s なので、ぎりぎり間に合う。
  - 本番の旧コードは worker が 1 本なので、`absolute_timeout_seconds` を上げると他の DM がその分長く待たされる。
  - 上げる場合は、未確認 3 で上限を確かめ、処理中の依頼が無いときに channel-gateway を再起動する
    （旧コードでは、再起動時に処理中だった依頼は lease 切れ（約 5 分）を待ってから新しく処理し直しになる）。

### 2. fallback を直す（オーナーの操作が要る）
- `commandcode` の API キーを作り直して、Hermes の `.env` を更新する。キーの値はチャットにも repo にも書かない。
- `opencode-free` は終了したモデルを、いま使えるものに差し替える（無料モデルは期間限定で入れ替わる。
  OpenCode の Console か <https://opencode.ai/docs/zen> で確認する）。`fallback_providers` が `fallback_model` より優先される。
- どちらも Hermes コンテナの再起動が要る。

### 3. 本番のコードを GitHub に回収する（PR #9 を配備する前に必須）
- **PR #9 をそのまま `deploy.sh` しない。** deploy.sh は `src/channel-gateway/app/*.py` で本番を上書きするので、
  coding handoff や premium routing の連携が消える。
- 手順の案:
  1. 本番の `/srv/hermes/channel-gateway/app`、`/srv/hermes/antigravity/app`、関係する systemd unit と drop-in、
     `hermes-premium-cli` / `hermes-coding-handoff` のコードと unit を一式控える（資格情報のファイルは除く）。
     ローカルの作業コピーが見つかればそちらを優先する。
  2. 597c897 から別ブランチを作り、控えた本番ファイルを対応する `src/` に置いて「本番状態の回収」として commit する。
     この repo は public なので、commit 前に秘密情報・個人情報を検査する
     （`tests-planning/test_privacy_guard.py` は `scripts/check_offline.py` に含まれる）。
  3. その上に PR #9 の変更を載せる（merge して衝突を解消する）。`gw_service.py` / `gw_store.py` / `gw_discord.py` /
     `bridge_server.py` は本番側でも変わっているので、手で突き合わせる。
  4. テスト（下記）を通してから配備する。配備時は `config.yaml` の `absolute_timeout_seconds` を 480 に上げる。
     deploy.sh は config.yaml を書き換えない。

### 4. 根本対策
- `/srv/hermes` を SSD（UAS 対応の USB-SATA など）へ移す。未確認 1 でディスクが原因と確定したら、これがいちばん効く。
  移行は停止を伴うので、手順と時間はオーナーと決める。
- 会話の文脈が伸び続ける件（session が 1 つのまま）は二次的。Hermes の `session_reset` が今の版で効くかは、
  ドキュメント間で食い違いがあり確認できていない。必要なら Gateway 側で session を切り替える方法を後で検討する。

## PR #9 の中身

| commit | 内容 |
|---|---|
| ad854d5 | 遅れた答えを捨てない（run id と開始時刻を保存し、再起動をまたいで同じ run を待つ）。会話をまたいで並列に処理。claim の原子化。attention の誤「復旧」修正 |
| 15a3578 | Pi 用の読み取り専用診断 `src/channel-gateway/bin/hermes-timeout-diag.sh` |
| 79ce9e8 | bridge: 呼び出しごとの内容なしログ、timeout 時にプロセスグループを kill、E2BIG を `prompt_too_large` として報告 |
| 1e135ec / cb763e3 / b49a56b | 3 回の敵対的レビューで見つかった指摘の修正。主なもの: 期限の判定を status を読んだ後にする、起動時に前のプロセスの行をすぐ戻す、正常停止後のみ試行回数を返す、Discord の受信順を保つ、hermes_failing の保持、部分復旧の文言 |
| 1b83c0e ほか | ドキュメント（HERMES-TIMEOUTS.md）、診断スクリプトの修正 |

ポイント:
- 期限（既定 480 s、上限 840 s）は `run_started_at` から測る。判定は status を読んだ後にするので、
  期限を過ぎてから再開した待ちでも、完了済みの答えは届く。
- 起動時は `recover_previous_process()`（lease 0 の `recover_stale`）で前のプロセスの PROCESSING 行を戻す。
  - 正常停止のときは `clean_shutdown_at` マーカーを書き、次の起動でそれを消費する。マーカーがあれば、run を保存済みの行に試行回数を返す。
  - クラッシュの後は返さない（毎回プロセスを落とす依頼を max_attempts で止めるため）。
  - 起動が終わるまで worker は何も取らない。
- Gateway は同じ DB に対して 1 つだけ動かす前提。

## テスト

```sh
python3 -m venv /tmp/hermes-test-venv
/tmp/hermes-test-venv/bin/python -m pip install --require-hashes -r requirements-test.txt
/tmp/hermes-test-venv/bin/python scripts/check_offline.py        # gateway / attention / planning / calendar-tentative
/tmp/hermes-test-venv/bin/python -m pytest -q tests-antigravity   # CI 対象外。別に実行する
```

ブランチの先頭での結果: gateway 382、attention 164、planning 607（3 skipped）、calendar-tentative 132、antigravity 25。
すべて pass。CI（Python 3.12 / 3.13）も緑。

## 守ること

- 本番への配備・再起動は、オーナーの了承を取ってから一つずつやる。
- 診断のレポート（`/tmp/hermes-diag-*.txt`）、API キー、ID の類は repo に入れない（repo は public）。
- Hermes の provider 設定や bridge の timeout は、根拠（計測）を示してから変える。今回の 120 → 240 には 162 s の計測という根拠がある。
- クラウド側のセッションは、この引継ぎをもって PR #9 の監視を止めた。以後の CI やレビューへの対応はローカルセッションで行う。
