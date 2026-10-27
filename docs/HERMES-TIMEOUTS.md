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

## Hermes 自体が遅かった理由（未確定）

コードからは決められない。可能性の高い順に:

- **H1** Antigravity の CLI が 120 s で打ち切られ、retry / fallback が残り時間に収まらない
- **H2** 会話ごとに Hermes session が 1 つのまま伸び続け、prompt が肥大（128 KiB 超だと CLI を起動できない）
- **H3** 有料ルーティング（Fable / Astra）の経路。リポジトリに無いコード
- **H4/H5** USB ディスクの I/O 停止、メモリ・CPU・電圧
- **H6** 配備物がリポジトリと違う

これらの証拠を集めるのが `src/channel-gateway/bin/hermes-timeout-diag.sh`。

## 診断スクリプトの使い方

```
sudo bash src/channel-gateway/bin/hermes-timeout-diag.sh 2026-10-27 12:50-15:40
```

- 引数は日付と JST の時間帯（省略時は今日の 12:50-15:40）
- `SKIP_RUN_API=1` で Hermes の `GET /v1/runs/<id>` を省略（この手順だけ Gateway の Hermes キーを root で読む。表示はしない）
- `HIST_DAYS=21` で傾向表の日数、`OUTBASE=/tmp` で出力先
- 何も再起動しない。`/srv` と `/etc` に書かない。docker exec しない。本文・prompt・回答・資格情報を出さない。
  ID・メール・トークンはマスクし、報告は 0600 で出力先に置く。共有前に一度目を通すこと
- checkout から実行すると、配備物とその checkout の差分（S1）も出る

各セクションの見出しに、どの仮説を確かめるものかを書いてある。読み方は末尾の S16。

## 配備するときに

- `gw_schema` は追加列だけで、既存 DB は起動時に `run_started_at` が足される
- **`config.yaml` の `hermes.absolute_timeout_seconds` は手で 480 に上げる。** 旧 sample は 240 を明示していて、
  deploy.sh は config.yaml を書き換えない。240 のままだと、遅れた回答を待つ時間は以前と同じ
- 起動時に、前のプロセスが PROCESSING のまま残した行をすぐ queue に戻す（同じ DB を使う Gateway は 1 つだけ、という前提。
  2 つ同時に動かさないこと）。送信中だった返信は lease 切れのときと同じく UNKNOWN で保留し、再送しない。
  run を保存済みの行は再起動で試行回数（max_attempts）を減らさない。起動が全部終わるまで worker は何も取らない
- 配備は `src/channel-gateway/bin/deploy.sh`（コピーして差分を表示するだけ。再起動は別手順）
- 旧版へ戻すと、PROCESSING のまま残った行を旧コードが新しい run で処理し直すことがある。
  戻すなら受信を止め、PROCESSING 行が無いことを確かめてから
- bridge の per-call ログは `journalctl -u hermes-antigravity` に出る。診断スクリプトが日ごとに集計する

## 証拠なしに変えないもの

bridge の `timeout_seconds`（120）、Hermes 側の provider retry / fallback / timeout / 圧縮設定、
session の切り替え、有料ルーティング、image digest。いずれも分類 broker や planning にも効くので、
S7/S9 の段階別の所要時間を見てから決める。
