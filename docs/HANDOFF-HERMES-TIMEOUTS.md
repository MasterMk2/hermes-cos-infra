# 引継ぎ: Hermes 応答タイムアウト（2026-10-27）

クラウドのセッションから Pi 上のローカルセッションへの引継ぎ。経緯の詳細は
[HERMES-TIMEOUTS.md](HERMES-TIMEOUTS.md)。ここには「何が分かっていて、何が残っているか」と、
次にやる順番だけを書く。オーナーへの返答は日本語で。

## 最初にやること

**既存の作業コピーには手を付けない。** Pi に hermes-cos-infra の作業コピーがある場合、そこには 9 月 10 日のローカル作業
（未コミットの変更や、GitHub に無い commit）が残っている可能性があり、本番の外にある唯一の控えかもしれない。
そこでの `switch` / `stash` / `reset` / `checkout -f` はしない。見るだけにする:

```sh
git -C <既存の作業コピー> status -sb; git -C <既存の作業コピー> branch -a
git -C <既存の作業コピー> stash list; git -C <既存の作業コピー> log --oneline -5
```

PR #9 の作業は、別のディレクトリに新しく clone して行う:

```sh
git clone https://github.com/MasterMk2/hermes-cos-infra.git ~/hermes-cos-infra-pr9
cd ~/hermes-cos-infra-pr9 && git switch claude/charming-thompson-3eaanv   # PR #9 のブランチ
```

その上で、このファイルと `docs/HERMES-TIMEOUTS.md` を読む。PR は
<https://github.com/MasterMk2/hermes-cos-infra/pull/9>（draft、レビューコメントなし）。CI はブランチの先頭の commit で改めて確かめる。

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
  - ただし、この DIFF のすべてが本番だけの変更ではない。gateway には、本番に配備されていない 2026-10-05 の
    GitHub 側の修正が 5 件ある（0532fe2・42eb2b4・b24e8f6・f4b9a0c・cbb9b0c、再起動時の送信安全化。
    REPLY-RESTART-SAFETY.md と LINE-RESTART-SAFETY.md）。
  - `gw_discord / gw_proactive / gw_schema / gw_store / gw_service` の差には、この 10-05 の分も含まれる。
    `gw_line / gw_line_client` も 10-05 に変わっているが、S1 の対象外なので本番の状態は未確認。
  - bridge の GitHub 側の最後の変更は 9-06（51bdb88）。
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

1. **ディスクが原因かどうか**: 162 s の計測コマンドを、続けてもう一度実行する。
   - 2 回目が大きく速くなれば（ページキャッシュが効く）、ディスクが原因だという強い根拠になる。
   - ただし、これだけでは確定しない。速さは MCP の起動、認証、ネットワークでも変わる。
     実行中に別の端末で `iostat -x 1`（入っていれば）と D 状態のプロセス（`ps -eo stat,comm | grep '^D'`）も記録し、
     待ち時間が I/O にあることを確かめる。
   - コマンドは、オーナーが 162 s を計測したときと同じもの（HERMES-TIMEOUTS.md には載っていない）:
   ```sh
   sudo -u hermesantigravity env HOME=/home/hermesantigravity bash -c 'cd ~ && time timeout 300 /srv/hermes/antigravity/bin/antigravity --print "[System Instruction] Use ONLY the read-only MCP tools. Do NOT call filesystem or shell tools. User: 今日のカレンダーの予定を一行で教えて" --model gemini-3.8-flash-medium --output-format json --disable-slash-commands </dev/null >/dev/null 2>&1; echo "exit=$?"'
   ```
   出力は捨てているので、回答の内容は表示されない。Antigravity を 1 回使う。
2. **9 月 10 日のローカル作業の在りか**: 本番の app ディレクトリ以外に、git の作業コピーが残っているか。
   ```sh
   sudo find / \( -path /proc -o -path /sys -o -path /var/lib/docker -o -path /srv/hermes/channel-gateway/app \) -prune -o -name gw_handoff.py -print 2>/dev/null
   ```
   見つかった作業コピーでは、`git cat-file -e cf7a647` と `git branch --list fix/owner-task-routing` も確かめる
   （どちらも [STATUS-RECONCILIATION.md](STATUS-RECONCILIATION.md) の記録にある名前）。
3. **timeout を上げて本当に効くか**:
   - 本番の bridge / gateway が設定値に上限を設けていないか:
     `grep -n timeout_seconds /srv/hermes/antigravity/app/*.py`、`grep -n absolute_timeout /srv/hermes/channel-gateway/app/gw_config.py`
   - Hermes 側に、bridge 向け provider の HTTP timeout が無いか。240 s 以下なら、bridge を 240 s にしても先に Hermes が切る。
     `/srv/hermes/hermes-data/config.yaml` の `model:` 周りと `custom` provider の設定を確かめる。

## 次の作業（この順で）

### 1. 設定だけでできる応急処置（コードの配備は不要）
- **bridge の `timeout_seconds` を 120 → 240 にする**（`/srv/hermes/antigravity/config/bridge.yaml`）。
  1. 未確認 3 で、上限が無く Hermes 側が先に切らないことを確かめる。
  2. オーナーの了承を取る。
  3. Hermes の run と分類 broker の処理が走っていないことを確かめる。bridge を再起動すると、実行中の CLI 呼び出しがすべて切れる。
  4. `sudo systemctl restart hermes-antigravity` を実行し、`curl -s http://172.18.0.1:8765/health` で確認する。
  - 根拠: ツール付き呼び出しが最短でも 162 s かかるので、120 s では一度も成功しない。
  - 代償: 本当に固まった呼び出しは 240 s × 3 回まで待たされる。
  - 分類 broker（45 s）と planning（既定 30 s）は自分の側で先に切るので、待ち時間は変わらない。
    ただし bridge 側の CLI は、切断後も timeout_seconds まで動き続ける（broker が詰まると、CLI プロセスが最長 240 s 残る）。
- 今の Gateway は 240 s で諦める。Hermes の準備（約 40 s）とツール呼び出し（約 162 s）で約 200 s なので、ぎりぎり間に合う。
  - 597c897 のコードでは worker が 1 本なので、`absolute_timeout_seconds` を上げると他の DM がその分長く待たされる
    （本番の gw_service.py は DIFF なので未確認）。
  - 上げる場合は、未確認 3 で上限を確かめ、オーナーの了承を取る。
  - 再起動の前に、設定を検証する:
    `sudo -u channelgateway /srv/hermes/channel-gateway/venv/bin/python /srv/hermes/channel-gateway/app/gw_service.py --config /srv/hermes/channel-gateway/config/config.yaml --check`
  - 処理中の依頼が無いことを `sudo bash /srv/hermes/channel-gateway/bin/gw-status.sh`（読み取りのみ）で確かめてから、
    channel-gateway を再起動する。
  - 597c897 のコードでは、再起動時に処理中だった依頼は、lease 切れと housekeeping（各 300 s）を待つので、
    約 5〜10 分後に新しく処理し直しになる。

### 2. fallback を直す（オーナーの操作が要る）
- `commandcode` の API キーを作り直して、Hermes の `.env`（`/srv/hermes/hermes-data/.env`）を更新する。
  `.env` はオーナー自身に編集してもらい、セッションでは表示も読み取りもしない。キーの値はチャットにも repo にも書かない。
- `opencode-free` は終了したモデルを、いま使えるものに差し替える（無料モデルは期間限定で入れ替わる。
  OpenCode の Console か <https://opencode.ai/docs/zen> で確認する）。`fallback_providers` が `fallback_model` より優先される。
- どちらも Hermes コンテナ（名前は `hermes`）の再起動が要る。再起動はオーナーの了承を取ってから。

### 3. 本番のコードを GitHub に回収する（PR #9 を配備する前に必須）
- **PR #9 をそのまま配備しない。** 配備の手段はコンポーネントごとに違う:
  - gateway: `src/channel-gateway/bin/deploy.sh`。app/*.py で本番を上書きするので、coding handoff の連携が消える。config.yaml は書き換えない。
  - attention: `src/attention/bin/deploy.sh`（反映には attention-engine の再起動が要る）。
  - bridge: deploy.sh が無い。`src/antigravity-bridge/install-root.sh` は bridge_server.py / bridge_config.py に加えて
    systemd unit も上書きし、daemon-reload する。premium routing の連携が消えるので、**本番では実行しない**。
    回収と merge を済ませたファイルを手でコピーする。
- PR #9 を配備すると、本番に入っていない 2026-10-05 の gateway 修正（UNKNOWN / NOT_SENT 状態の追加を含む）も一緒に初めて入る。
  旧版に戻すときは、PROCESSING 行だけでなく UNKNOWN の行の扱いにも注意する（REPLY-RESTART-SAFETY.md の「戻し方」）。
- 手順の案:
  1. 本番の `/srv/hermes/channel-gateway/app`、`/srv/hermes/antigravity/app`、関係する systemd unit と drop-in、
     `hermes-premium-cli` / `hermes-coding-handoff` のコードと unit を一式控える（資格情報のファイルは除く）。
     ローカルの作業コピーが見つかれば、そちらを優先する。
  2. **本番の元になった GitHub の commit を特定する。** 本番ファイルの sha256 を `git log` 上の各版と比べる
     （候補は 9-08 の main `640d9db` と、記録にあるローカル main `cf7a647`）。
  3. その commit から別ブランチを作り、控えた本番ファイルを置いて「本番状態の回収」として commit する。
     差分が 9 月 10 日のローカル作業だけになるようにする。**597c897 の上に本番ファイルを直接置かない**
     （置くと 10-05 の修正を黙って巻き戻す。PR #9 が触れない hunk では衝突も起きない）。
  4. この repo は public なので、push 前に必ず検査する:
     - 回収したファイルは `git add` してから `scripts/check_offline.py` を流す。privacy guard は `git ls-files` に載ったファイルしか見ない。
     - denylist があれば `HERMES_PRIVACY_DENYLIST` で指定する。ただし denylist の照合対象は planning 系のパスだけ。
     - `git diff --cached` を全部目で読み、LAN の IP、ユーザー ID、氏名、メール、トークン、`/home` のパスが無いことを確かめる。
     - push はオーナーが差分を確認してからにする。public repo では、ブランチを push した時点で公開される。
  5. そのブランチを PR #9 のブランチへ merge し、10-05 の修正、9 月 10 日のローカル作業、PR #9 の三つを手で突き合わせる。
     `gw_service.py` / `gw_store.py` / `gw_discord.py` / `bridge_server.py` は三方で変わっている。
  6. テスト（下記）を通してから配備する。配備時は `config.yaml` の `absolute_timeout_seconds` を 480 に上げる
     （deploy.sh は config.yaml を書き換えない）。

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

`requirements-test.txt` は Python 3.12 以降向けに固定されている（[OFFLINE-TESTS.md](OFFLINE-TESTS.md)）。
先に `python3 --version` を確かめる。

```sh
python3 -m venv /tmp/hermes-test-venv
/tmp/hermes-test-venv/bin/python -m pip install --require-hashes -r requirements-test.txt
/tmp/hermes-test-venv/bin/python scripts/check_offline.py        # gateway / attention / planning / calendar-tentative
/tmp/hermes-test-venv/bin/python -m pytest -q tests-antigravity   # CI 対象外。別に実行する
```

引継ぎ時点のブランチでの結果: gateway 382、attention 164、planning 607（3 skipped）、calendar-tentative 132、antigravity 25。
すべて pass（クラウドで Python 3.12 / 3.13 の両方で確認）。

診断スクリプトを再実行するときは、PR #9 の merge 前ならこのブランチの checkout から実行する
（`main` にはまだ無い）。

## 守ること

- 本番への配備・再起動は、オーナーの了承を取ってから一つずつやる。
- 診断のレポート（`/tmp/hermes-diag-*.txt`）、API キー、ID の類は repo に入れない（repo は public）。
- Hermes の provider 設定や bridge の timeout は、根拠（計測）を示してから変える。今回の 120 → 240 には 162 s の計測という根拠がある。
- クラウド側のセッションは、この引継ぎをもって PR #9 の監視を止めた。以後の CI やレビューへの対応はローカルセッションで行う。
