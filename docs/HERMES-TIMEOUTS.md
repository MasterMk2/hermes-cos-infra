# Hermes の応答タイムアウト（2026-10-27）

Discord DM に「応答に時間がかかりすぎたため中断しました」が続けて返り、attention から
「gateway: Hermes が応答を返せていません」→「復旧」→再び同じ警告、と通知された件の記録。
コードで直した部分と、Pi の実データが無いと決められない部分を分けて書く。
このPR作業ではPiへの配備・再起動・実機DBの変更はしていない。

## Gateway 側で「遅い」が「答えが消える」に変わっていた理由

基準: `597c897`（当日稼働していたと想定するコード。配備物との一致は診断スクリプトの S1 で確認する）。

1. **待ちが打ち切られると、その run は捨てられていた。** 240 s を超えると時間切れの定型文を返し、
   Hermes がその後に答えを出しても受け取る経路がなかった。再起動時も run id を読まず、新しい run を始めていた。
2. **worker が 1 つで、全会話を直列に処理していた。** 遅い 1 件の後ろで、他の DM もずっと待たされた。
3. **claim の競合判定が効いていなかった。** `con.total_changes == 0` は `open_db` が meta を書いた時点で 1 になるため、
   「取り損ねた」分岐が成立しなかった。
4. **attention の「復旧」は Hermes の成功ではなかった。** `hermes_failing` は最後の失敗が 3600 s の窓から
   外れた時点で回復扱いになっていた。14:20 前後の「復旧」はこれで、成功の記録は無かった。

## 変更内容

| 場所 | 変更 |
|---|---|
| `gw_store` / `gw_schema` | `claim_next` を `BEGIN IMMEDIATE` + `rowcount` で原子的に。`note_run_started` が `hermes_run_id` と `run_started_at`（追加列）を最初の poll より前に保存。`touch_claim` で claim を更新 |
| `gw_service` | 会話ごとに 1 件、全体で `limits.max_active_requests` 件まで並列。保存済み run id があれば再開し、新しい run は始めない。期限は `run_started_at` から測るので再起動をまたいでも延びない。heartbeat で claim を更新、status 取得の短い失敗は `unavailable_grace_seconds` まで待つ、Hermes が run を忘れた場合は同じ Idempotency-Key で 1 回だけ再開始。期限の判定は status を読んだ後なので、期限を過ぎて再開した待ちも、完了済みの答えなら届ける。停止時は PROCESSING と run id を残し、次の起動が lease を待たずにすぐ戻して同じ run を待つ |
| `gw_discord` | 受信時の DB 処理を event loop の外へ（専用の 1 本の thread で受信順に記録するので、続けて送った 2 通の順番は入れ替わらず、停止時に待っていた DM も書き終えてから終わる）。待機中は typing 表示を 8 s ごとに更新 |
| `gw_config` | `absolute_timeout_seconds` 既定 480（上限 840: attention の queue_stuck 900 s 未満）、`poll_request_timeout_seconds`、`heartbeat_seconds`、`unavailable_grace_seconds` を追加 |
| `gw_format` | 時間切れの文言を「少し時間をおいて、もう一度聞いてください。」に（遅さの原因は質問の長さとは限らず、分けて聞き直す案内は根拠が無かったため） |
| `att_health` | 失敗が窓から外れても、その後に成功が無ければ `hermes_failing` の episode だけ開いたまま保持。回復通知は Hermes が実際に答えたときだけ。gateway の他の episode（接続断など）は通常どおり閉じて、次の障害もまた通知される。その回復通知は「gateway は復旧しました」ではなく「〜は解消しました（Hermes が応答を返せていません は継続中）」 |
| Antigravity bridge | 呼び出しごとに内容を含まない 1 行（model / status / outcome / 所要秒 / prompt bytes）。timeout 時は stderr の固定語彙フラグだけ記録。CLI を別 session で起動し、timeout でプロセスグループごと kill。128 KiB 超の prompt は `prompt_too_large` として報告（status は 500 のまま）。`/health` の last_error に実行ファイルのパスを出さない |

返信の at-most-once（[REPLY-RESTART-SAFETY.md](REPLY-RESTART-SAFETY.md)、[LINE-RESTART-SAFETY.md](LINE-RESTART-SAFETY.md)）は変えていない。
遅れて届いた答えも、固定済みの 1 通として同じ ledger を通る。

## Pi での診断結果（2026-10-27 23:31 JST 実行）

3 件の DM（13:11 / 13:12 / 14:49）とも、Hermes の run は**答えを出せずに失敗**していた
（`GET /v1/runs/<id>` が `status=failed`、所要 474 s / 625 s / 497 s）。待ち時間を延ばすだけでは答えは届かない。

1. **主経路（Antigravity）のツール付き呼び出しが毎回 120 s で打ち切られた。** 10-27 は
   `Delegating tool request (tools: 6)` が 10 回、そのすべてが `CLI timed out after 120s`（10-25 は 6 回中 3 回）。
   ツールなしの分類 broker は同じ bridge で 24〜42 s で通っている。Hermes はこれを 3 回 retry するので、
   fallback に移るまでに約 6 分かかる。
2. **fallback が全部壊れていた。** `commandcode`（z-ai/glm-5.3-flash）は `401 Invalid 'Authorization' header or token`、
   `opencode-free` の `laguna-s-2.1-free` と `deepseek-v4-flash-free` は `400 Model is unavailable`。
   どれも 1 秒以内に失敗し、run はそこで終わる。
3. **Gateway は 240 s で諦め、2 通目は前の run の後ろで待たされた。** Hermes 側でも
   「Another Hermes process is using this session」で、捨てられた 1 通目の run が終わるまで 2 通目は始まらなかった。
4. **USB メモリ（/srv/hermes）が非常に遅い。** gateway.db の 4 KiB 直接読み出しが 0.65〜1.07 s、
   jbd2 などが D 状態、attention の scan は日ごと p90 8〜33 s・最大 252 s、14:53 に Discord の heartbeat が 30 s 止まった。
   ツール（MCP）はこのディスク上の DB を読むので、120 s 超えの一因と考えられる（直接の因果は未確認）。
5. **「復旧」は誤報だった。** 2 回とも、最後の成功（11:35:52）のまま 3600 s の窓が切れて回復扱いになっていた
   （この PR で修正済み）。
6. **本番のコードは GitHub と違う。** gateway の大半と bridge が 597c897 と不一致で、
   `gw_coding.py`・`gw_handoff.py`・`gw_handoff_client.py`・`premium_routing.py` は本番にしか無い
   （[STATUS-RECONCILIATION.md](STATUS-RECONCILIATION.md) の 9 月 10 日のローカル作業と思われる）。
   会話の Hermes session は 109 メッセージ・約 3 万 token。
7. **会話の文脈は縮まない設定だった。** Hermes の `session_reset.mode = none`、`compression.threshold = 0.5`
   （Gemini の大きな窓の半分なので、3 万 token では圧縮されない）。失敗時の request dump は 1 通 約 156 KB で、
   10-25 の 152 KB から増えている。本番には `hermes-premium-cli` / `hermes-coding-handoff` など GitHub に無い service もある。

## Hermes 自体が遅かった理由（診断前の仮説）

診断前の見立て。上の結果では H1（と、壊れた fallback）・H3・H4・H6・H7 が裏付けられた:

- **H1** Antigravity の CLI が 120 s で打ち切られ、retry / fallback が残り時間に収まらない
- **H2** 会話ごとに Hermes session が 1 つのまま伸び続け、prompt が肥大（128 KiB 超だと CLI を起動できない）
- **H3** 有料ルーティング（Fable / Astra）の経路。リポジトリに無いコード
- **H4/H5** USB ディスクの I/O 停止、メモリ・CPU・電圧
- **H6** 配備物がリポジトリと違う

これらの証拠を集めるのが `src/channel-gateway/bin/hermes-timeout-diag.sh`。

## 診断スクリプトの使い方

hermes-cos-infra の checkout の最上位で:

```
sudo bash src/channel-gateway/bin/hermes-timeout-diag.sh 2026-10-27 12:50-15:40
```

Pi に checkout が無い（またはこのファイルを含まない）ときは、スクリプトだけ取得して実行する。
checkout を書き換えずに済む:

```
# PR #9 の merge 前は main を claude/charming-thompson-3eaanv に読み替える
curl -fsSL -o /tmp/hermes-timeout-diag.sh \
  https://raw.githubusercontent.com/MasterMk2/hermes-cos-infra/main/src/channel-gateway/bin/hermes-timeout-diag.sh
sudo bash /tmp/hermes-timeout-diag.sh 2026-10-27 12:50-15:40
```

- 引数は日付と JST の時間帯（省略時は今日の 12:50-15:40）
- `SKIP_RUN_API=1` で Hermes の `GET /v1/runs/<id>` を省略（この手順だけ Gateway の Hermes キーを root で読む。表示はしない）
- `HIST_DAYS=21` で傾向表の日数、`OUTBASE=/tmp` で出力先
- 何も再起動しない。`/srv` と `/etc` に書かない。docker exec しない。本文・prompt・回答・資格情報を出さない。
  ID・メール・トークンはマスクし、報告は 0600 で出力先に置く。共有前に一度目を通すこと
- checkout から実行すると、配備物とその checkout の差分（S1）も出る

各セクションの見出しに、どの仮説を確かめるものかを書いてある。読み方は末尾の S16。

## 配備するときに

- **このブランチをそのまま本番へ `deploy.sh` しない。** 本番の gateway と bridge には GitHub に無いローカル変更
  （coding handoff、premium routing）が入っていて、上書きするとそれらが消える。先に Pi 上のローカル作業を
  GitHub に回収し、その上にこの修正を載せ直してから配備する（2026-10-28 に回収・統合済み）
- `gw_schema` は追加列だけで、既存 DB は起動時に `run_started_at` が足される
- **`config.yaml` の `hermes.absolute_timeout_seconds` は手で 480 に上げる。** 旧 sample は 240 を明示していて、
  deploy.sh は config.yaml を書き換えない。240 のままだと、遅れた回答を待つ時間は以前と同じ
- 起動時に、前のプロセスが PROCESSING のまま残した行をすぐ queue に戻す（同じ DB を使う Gateway は 1 つだけ、という前提。
  2 つ同時に動かさないこと）。送信中だった返信は lease 切れのときと同じく UNKNOWN で保留し、再送しない。
  正常停止（SIGTERM）の後なら、run を保存済みの行は再起動で試行回数（max_attempts）を減らさない。
  クラッシュや OOM kill の後は減らすので、毎回プロセスを落とす依頼も 3 回で FAILED になり、会話が詰まり続けない。
  起動が全部終わるまで worker は何も取らない
- 配備は `src/channel-gateway/bin/deploy.sh`（コピーして差分を表示するだけ。再起動は別手順）
- 旧版へ戻すと、PROCESSING のまま残った行を旧コードが新しい run で処理し直すことがある。
  戻すなら受信を止め、PROCESSING 行が無いことを確かめてから
- bridge の per-call ログは `journalctl -u hermes-antigravity` に出る。診断スクリプトが日ごとに集計する

## 次に直すもの（診断結果から）

1. fallback を使える状態に戻す: `commandcode` の API キーの更新、`opencode-free` の終了したモデルの差し替え
2. ~~主経路の 504（timeout）を 3 回 retry しない設定にできるか、Hermes の設定を確認する~~
   → 2026-10-28 に `agent.api_max_retries: 1` にした（下の「2026-10-28 の配備」）
3. /srv/hermes を USB メモリから SSD（UAS 対応の USB-SATA など）へ移すことを検討する
4. ~~上の「配備するときに」の回収を済ませてから、この PR の修正を配備する~~ → 2026-10-28 に回収・統合・配備した

## 証拠なしに変えないもの

bridge の `timeout_seconds`（120）、Hermes 側の provider retry / fallback / timeout / 圧縮設定、
session の切り替え、有料ルーティング、image digest。いずれも分類 broker や planning にも効く。
変えるときは診断スクリプトの S7/S9 で段階別の所要時間を確かめてから。
bridge の `timeout_seconds` については、2026-10-27 に根拠が得られた: 1 行の質問でもツール付き呼び出しに 162 s かかり、
120 s では一度も成功しない。120 → 240 の手順は [HANDOFF-HERMES-TIMEOUTS.md](HANDOFF-HERMES-TIMEOUTS.md) にある。
変えたら、ここに変更日と値を書き足す。
- 2026-10-28 01:47 JST: 120 → 240
- 2026-10-28 05:39 JST: 240 → 600（PR #9 の配備と同時。Hermes の `agent.api_max_retries: 1` と組み合わせる）

## 2026-10-28 の確認と変更（Pi）

- **bridge の `timeout_seconds` を 120 → 240 に変更した**（2026-10-28 01:47 JST、オーナー了承済み）。
  - 元の設定は `bridge.yaml.bak-20261028` として同じディレクトリに控えてある。戻すときはこの控えを戻して `hermes-antigravity` を再起動する。
  - 再起動後の `/health` は healthy。120 s を超える呼び出しが実際に成功するかは、まだ観測していない。
- 変更前の確認:
  - bridge・gateway とも timeout の値に上限を設けていない。
  - 10-27 は premium 経路が一度も動いておらず、120 s をすべて Antigravity が使っていた。
  - Hermes（0.20.6）は provider の timeout を設定しておらず、既定の 1800 s。config の 180 / 120 / 300 は terminal / browser / code_execution のもの。
  - 10-27 の agent.log では、主経路は 1 run あたり 3 回（attempt 1/3〜3/3）。どれも bridge の 120 s で 504 になり、Hermes が先に切った跡は無い。
  - 240 s では、固まったときの最悪値は 3 × 240 s ≈ 12 分。`agent.api_max_retries` は 0.20.6 にもある。
- 162 s を計測したコマンドを、続けて 2 回実行した:
  - 1 回目: 301 s で打ち切り（timeout 300）。2 回目: 218 s で成功。user+sys は 2 回とも約 36 s。
  - /srv/hermes のディスクは使用率 90% 以上のサンプルが約 25%、D 状態（jbd2、kworker、python、usb-storage）が約 3〜4 割のサンプルで出た。
  - ディスクが原因という見方と矛盾しないが、確定はしていない。
  - 218 s と Hermes の準備（約 40 s）を足すと、本番の gateway の 240 s を超える。bridge の変更だけでは、DM の時間切れは減らない見込み。
- 本番のコードの照合（sha256）:
  - gateway: 640d9db に 9 月 10 日の coding handoff（Phase J.1）を足したもの。10-05 の gateway 修正は入っていない。
  - bridge・attention（engine / events / schema）・premium-cli: 9 月 10 日の owner-task-routing の作業。GitHub には未回収。
  - したがって、PR #9 の attention を配備すると、gateway・bridge と同じく本番だけの変更が巻き戻る。回収の対象に attention も含める。

## 2026-10-28 の配備（PR #9、a1ba3eb）

オーナーの了承を段ごとに取り、次の順に入れた: 状態確認 → 退避 → Hermes → bridge → gateway → attention。

| 対象 | 変更 | 時刻（JST） |
|---|---|---|
| Hermes | `agent.api_max_retries: 1` を追加し、コンテナを再起動（health 200 まで 153 s） | 05:35 頃 |
| bridge | `bridge_server.py` を a1ba3eb に、`timeout_seconds` を 240 → 600。再起動 | 05:39 |
| gateway | `deploy.sh`（更新 10、新規 1）、`absolute_timeout_seconds` を 240 → 840、`--check` の後に起動 | 05:45 |
| attention | `deploy.sh`（更新 6）、attention-engine を再起動 | 05:45 |

値の根拠:
- gateway 840: 検証範囲の上限。attention の queue_stuck（900 s）より短い
- bridge 600: 計測した 218 s / 300 s 超が収まり、840 より短い。呼び出しが固まっても gateway の期限より前に失敗として返る。
  premium CLI（最大 60 s）を使う場合は、その時間も 600 s に含まれる（`bridge_server.py`）
- Hermes `api_max_retries: 1`: 0.20.6 ではこの値は試行回数で、0 は 1 に丸められる（1 = 再試行なし）。
  600 s で打ち切られた呼び出しをやり直しても 840 s に収まらない。代わりに、一時的な失敗も再試行されない
- 1 回の呼び出しを打ち切るのは bridge の値。Hermes の上限は 1800 s（`HERMES_API_TIMEOUT` 未設定）で、
  90 s の stale 検出は接続先がローカル扱いのため無効

配備前の確認:
- 本番と a1ba3eb の差は、gateway app 10 と診断スクリプト（新規）、attention 6、bridge は `bridge_server.py` だけ。
  回収した内容と本番が一致していることを、もう一度確かめた
- 本番の 3 つの venv（Python 3.13.5）で、新しいコードの py_compile と起動モジュールの import が通った
- gateway の DB に処理中・送信中の行が無いことを確かめてから、Hermes・bridge・gateway を再起動した
- 退避: コードと設定（gateway / attention / bridge）、Hermes の config.yaml、gateway の DB（`state/gateway.db.bak-pr9`）

配備後の観察:
- gateway は USB の I/O 待ちで、起動から受け付け開始まで約 4 分かかった。その間に届いた DM は取りこぼした可能性がある
- 実際の DM 1 件: bridge の per-call ログ `status=200 outcome=ok duration=233.5s`、Hermes の run は 246 s で completed、
  回答は DM に届いた（`ok=True`。`detail=already_delivered` は全 chunk 送信済みの意味）。旧 gateway の 240 s なら捨てられていた
- 1 行の質問でも約 4 分かかる状態は変わっていない。ツールを多く使う依頼は 600 / 840 s に届きうる。fallback は壊れたまま

戻すとき:
- 各段を退避から戻して、そのサービスを再起動する。gateway は受信を止め、PROCESSING 行が無いことを確かめてから戻す。
  DB は戻さなくてよい（追加列は旧コードが無視する）
- 失敗した proactive の行は、同じ通知 id で再要求されると、新しいコードでは再送せず UNKNOWN で保留する
