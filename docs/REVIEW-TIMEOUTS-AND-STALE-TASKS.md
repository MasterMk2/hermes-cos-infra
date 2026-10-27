# レビュー: コマンドの時間超過と、終わったタスクの取り残し

対象: `main` 相当の本ブランチ HEAD（`24c42c9`）。コードを読み、疑わしい箇所のうち 3 件は
隔離環境で再現スクリプトを走らせて実証した。実機 Pi・実 `jobs.db`・実 Docker・実ブリッジには
接続していない。行番号はこの時点のもの。

本書はレビュー結果であり、コードは変更していない。

## 要約

「コマンドが時間超過で止まる」「とっくに終わったタスクが残る」は、どちらも主にコーディング
監督系（`src/jobs/` の supervisor / transport、`src/bridge/bridge.py`、
`src/jobs/worker/workerctl_server.py`）に原因がある。根本は 4 つに集約できる。

1. **期限が階層ごとに独立に決まっていて、上位の期限が下位の最悪所要時間より短い箇所がある**
   （A2、D2、E1）。上位が先に諦め、下位はまだ走っている。
2. **停止（cancel / timeout）がプロセス木やコンテナまで届かない**（A3、C1、C2）。
   止めたつもりのエージェントが worktree を書き換え続ける。
3. **「終わったか」を 4 箇所（jobs.db、runner の pid、ブリッジの state.json、コンテナ）が
   別々に持ち、突き合わせる主体が手動 `job_reconcile` しかない**（A5〜A8、B3）。
   runner やブリッジが落ちると、その時点の状態が永久に残る。
4. **後始末の経路（`job_cleanup` / `review_cleanup`）はあるが呼ぶ主体がない**（B4）。
   worktree・ブランチ・snapshot・job ディレクトリが成功時も残る。

ブリッジ本体は単一スレッドで 1 接続ずつ処理しており（B1）、最大 10 分のテスト実行中は
他の呼び出しがすべてタイムアウトする。1 分ごとの watchdog はそれを「死んだ」と判定して
旧プロセスを止めずに新ブリッジを起動する（B2）。この組み合わせが、残骸プロセス・
`running` のまま固定された state・永久 busy（B3）を生む最短経路である。

## 症状との対応

| 症状 | 直接の原因 | 増幅する要因 |
|---|---|---|
| コマンドが時間超過で止まる | A2 ポーリング期限 < ブリッジ最悪値 / A4 `finished_timed_out` 未処理 / E1 systemd 10 分 < 抽出の最悪 19 分 / D1 Hermes run の時間超過を即失敗扱い | B1 ブリッジ単一スレッド / B5 毎秒の worktree 全走査 / A9 同じテストを 3 回実行 |
| 終わったタスクが残る | A3 cancel がワーカーに届かない / C1 タイムアウトが孫プロセスを殺さない / B3 ブリッジ起動時に再整合しない / B4 cleanup を誰も呼ばない / A5 reconcile 後に結果を回収せず新 attempt を起動 | A7 reconcile が手動のみ / A8 REVIEW_INTERRUPTED を再試行も通知もしない / A6 古い review 行と snapshot が残る |
| ブリッジが「busy」でジョブを受けない | B3 残った `running` state / A3 取り消し済みコンテナが走り続ける | B2 watchdog の二重起動 |

## A. ジョブ監督（`src/jobs/supervisor.py`、`src/jobs/transport.py`、`src/jobs/jobs_mcp.py`）

### A1. レビュー監視ループがブリッジ到達不能時にスリープ無しで回り続ける（High・再現済み）

`supervisor.py:613-631`。`review_status` の RPC が例外を投げると `state = "running"; continue`
で `time.sleep(15)`（631 行）を飛ばす。`bridge.sock` が無いと `bridge_request` は即例外なので、
期限（`agent_timeout_s + 600`、既定 30 分）まで CPU を占有し、毎周 `jobs_db.cancel_requested`
で SQLite を叩く。

再現（偽の時計で期限まで進めた）: `review_status` RPC 69,000 回、`sleep` 0 回、結果
`REVIEW_INTERRUPTED`。`PiSandboxWorkerAdapter` 側（`transport.py:254-267`）は `misses >= 8`
で諦める形になっており、ここだけ抜けている。

修正: 例外側でも 15 秒待ってから続ける。連続失敗回数で `REVIEW_INTERRUPTED` に落とす。

### A2. ポーリング期限切れを「完了」と誤認する（High）

`transport.py:252-300`。待ち期限は `timeout_s + 600`。一方ブリッジ側は
エージェント `timeout_s` ＋ 停止待ち（最大約 90 秒、`bridge.py:485-497`）＋ 登録テスト最大 600 秒
（`bridge.py:642`）＋ git 操作。期限内に終わらないとループを抜けて `job_result` を取るが、
`state` が `running` でも `status: "OK"` として返す（270-300 行）。

結果: 空の evidence で `AGENT_STOPPED` → `VALIDATING` → 検証項目ゼロで不合格 →
`CONTINUE_REQUIRED` → 次の attempt の `job_start` はブリッジが `readopted` を返して同じ
コンテナを監視し直す（`bridge.py:1164-1168`）。本来の attempt 1 の結果が attempt 2 として
記録され、attempt を 1 つ消費し、書いたプロンプトは使われない。

修正: ループ後に `state == "running"` なら `INTERRUPTED`（`timed_out=True`）を返し、
ブリッジへ `job_cancel` を送る。待ち期限は `agent_timeout_s + test_timeout_s + 余裕` に揃える。

### A3. `job_cancel` がワーカーに届かない（High）

`jobs_mcp.py:403-411` は `jobs.db` を `CANCELLED` にするだけ。supervisor が使うブリッジ action
に `job_cancel` は無い（`review_cancel` のみ）。`adapter.run`（`supervisor.py:1239`）の間は
cancel を見ず、ポーリング（`transport.py:254-269`）も見ない。ブリッジの `_cancelled()`
（`bridge.py:591-594`）はブリッジ側 `state.json` の `cancel_requested_at` しか見ない。

結果: 取り消したジョブのコンテナが `agent_timeout_s`（20 分）＋テストまで走り続け、その間
`any_active_job()` が busy を返すので、次のジョブは `worker rejected the job` で
`WAITING_HUMAN` になる（`supervisor.py:1257-1267`）。H4 は「cancel 後に受理しない」を保証するが
「cancel で止まる」は保証していない。

修正: `job_cancel` でブリッジ `job_cancel`（remote は `workerctl job_cancel`）を送る。
ポーリング中に `cancel_requested` を見たら同じく送って即 return する。

### A4. RemoteWorkerAdapter が `finished_timed_out` を終端として扱わない（High）

`transport.py:165` の終端集合は `finished / failed / cancelled`。workerctl はエージェントが
タイムアウトすると `finished_timed_out` を書く（`workerctl_server.py:402-404`）。supervisor は
終わっているジョブを `timeout_s + 300` までポーリングし続け、最後に `INTERRUPTED`
（`needs_human=1`）で止まる。evidence は回収されない。これは症状そのものである。

修正: 終端集合に `finished_timed_out` を加え、evidence の `timed_out` を読む。
`resumable`（`workerctl_server.py:510`）の扱いも決める。

### A5. reconcile の「回収して継続」が実際には新 attempt を起動する（Medium）

`supervisor.py:1062-1067` は runner 死亡・ブリッジ側 finished を `AGENT_STOPPED` にして
`job_continue to collect and continue` と案内する。しかし `run_job_loop`（1102 行以降）に既存結果を
回収する経路は無く、必ず `adapter.run` → `job_start` → 新 attempt。ブリッジ側の finished evidence は
捨てられ、前回の変更が残った worktree で新しいエージェントが走る（remote は
`worktree dirty from previous attempt` で failed、`workerctl_server.py:337-341`）。

修正: `run_job_loop` 冒頭に「ブリッジ側が finished/failed で、その attempt の run が未回収なら
`job_result` を回収して検証へ進む」collect パスを入れる。

### A6. レビュー runner 死亡時の復旧が古い review 行と snapshot を取り残す（Medium）

`supervisor.py:1008-1029` は `review_status` を `REVIEW_INTERRUPTED` にするだけで、`reviews` 行は
`reviewing` のまま。再試行（`run_review_phase:523-537`）は `prev.status == "interrupted"` に
ならないので cycle+1 の新規 review を作る。ブリッジがまだ旧レビューを実行中なら busy →
120 秒待って `REVIEW_INTERRUPTED`。旧 `review_id` の verdict は回収されず（docstring の
「its verdict is still collected by the retry」は成立しない）、旧 snapshot worktree も
`review_cleanup` されない。

修正: reconcile で該当 review 行を `interrupted` にする。再試行時は同じ `review_id` の
ブリッジ状態を `review_status` / `review_result` で先に確認し、終わっていれば回収する。

### A7. reconcile は手動呼び出しのみ（Medium）

呼び出しは `jobs_mcp.py:413-415` だけ。MCP 起動時にも周期にも走らない。runner が死んだ
`RUNNING / DISPATCHING / VALIDATING / REVIEWING` は誰かが `job_reconcile` を呼ぶまで残り、
`dispatch_job`（`supervisor.py:918-919`）は `already_active` で拒否する。`CONTINUE_REQUIRED` で
runner が死んだ場合は reconcile の対象ですらない（1030-1032 行）。Attention の通知対象は
`COMPLETED / WAITING_HUMAN` のみ（`att_config.py:141`）なので、いずれも静かに止まる。

修正: `jobs_mcp.main()` 起動時と `job_list` / `job_status` 呼び出し時（安価）に reconcile を走らせる。
加えて Attention に「非終端 ＋ runner 不在 ＋ `updated_at` が N 分以上古い」を stall として
検出させる。

### A8. `REVIEW_INTERRUPTED` は自動再試行も通知もされない（Medium）

`_mark_review_interrupted`（431-439 行）と `_interrupt_review_dispatch`（776-781 行）は
`needs_human=0` で `job_review to resume` と書くが、再試行するのは人間だけ。ステータスは
`IMPLEMENTATION_ACCEPTED` のままなので Attention は通知しない。実装は受理済みなのに誰にも
知らされず止まる。`MAX_INTERRUPTED_ATTEMPTS` の上限は既にあるので自動再試行は安全に足せる。

### A9. 同じ登録テストが 1 attempt で 3 回走る（Low）

`_job_worker`（`bridge.py:1035`）、review 開始前の `job_verify`（`supervisor.py:540`）、
completion gate の `job_verify`（814 行）。各 600 秒上限。fresh verification の意図は理解できるが、
Pi では A2 の期限と B1 の閉塞時間を直接押し上げる。

### A10. ログ・プロンプト・pid ファイル・`event_log` の保持ポリシーが無い（Low）

`jobs_db.py:14-18`、`_spawn_runner`（`supervisor.py:882-895`）。`runner-<job>.pid` は runner 終了後も
残る（Item 45 の start_ticks 照合で誤判定は防げている）。

## B. ホスト Coding Bridge（`src/bridge/bridge.py`、`src/bin/watch-bridge.sh`）

### B1. accept ループが単一スレッドで、長い action が全呼び出しを塞ぐ（High・再現済み）

`main()`（1360-1395 行）は accept → handle → 応答を直列に行う。`job_verify`（1285-1326 行）は
`run_test_container`（629-650 行、最大 600 秒）を同期実行し、`review_start` は snapshot 作成、
`job_cleanup` は `git worktree remove` を行う。その間、supervisor の `job_status`（30 秒）も
watchdog の `worker_status`（5 秒）もタイムアウトする。

再現: 4 秒の `job_verify` を流している最中の `worker_status` は 3.5 秒待たされてから返った。
600 秒なら 5 秒の probe は確実に失敗する。

修正: 接続ごとにスレッドを立て、状態を変える action（`job_start` / `review_start` / `job_verify`
の busy 判定とクレーム、cancel、cleanup）を 1 本のロックで直列化する。読み取り action
（`worker_status` / `job_status` / `review_status` / `job_result` / `review_result` / `job_diff`）は
ロック外で返す。

### B2. watchdog が「遅い」ブリッジを「死んだ」と判定し、旧プロセスを止めずに二重起動する（High）

`watch-bridge.sh:10-33`。`alive()` は 5 秒期限。失敗すると `rm -f $SOCK` して `nohup` で新プロセスを
起動するが、旧プロセスは終了させない。B1 により `job_verify` のたびに起こり得る。旧プロセスは
ソケットを失ったまま worker thread を抱え続け（残骸）、新旧が同じ `jobs/<id>/state.json` に書く。

修正: probe の期限を action の最悪値以上にするか、別の軽い生存確認（pid ファイル ＋ `/proc`）に
変える。再起動前に旧 pid を終了する。`bridge.py` 側で pid ファイル／flock による単一起動を保証する。

### B3. 起動時の再整合が無く、残った `running` 状態で永久に busy（High）

`main()` は状態ディレクトリを見ない。ジョブ実行中にブリッジが落ちる／再起動される（B2 で起きやすい）
と worker thread が消え、`state.json` は `running` のまま。`any_active_job()`（129-136 行）が
その job_id を返し続け、`job_start`（1141-1143 行）・`review_start`（817-819 行）・`job_verify`
（1300 行）がすべて busy になる。supervisor 側は BUSY → `WAITING_HUMAN`、review は
`REVIEW_INTERRUPTED`、completion gate は `verification_failed busy` → `WAITING_HUMAN`。
コンテナ自体も `docker run --rm` のままエージェントが自分で終わるまで走る。

修正: 起動時に `running` な `state.json` を列挙し、`docker inspect hermes-cw-<id>` で実在を確認。
無ければ `failed`（`error=bridge_restarted`）に、有れば監視スレッドを再接続するか `docker stop`
して `interrupted` にする。review（`hermes-rv-<id>`）も同様。

### B4. 後始末の経路が一度も呼ばれない（Medium）

`job_cleanup`（1225-1238 行）を supervisor は呼ばない。`worktrees/<job>`、`jobs/<job>`
（prompt.txt / state.json / usage）、ブランチ `hermes/job-<id>` は成功時も残る。`review_cleanup` は
verdict が出た場合のみ（`supervisor.py:688-692`）で、interrupted / failed / cancelled /
invalid-output のレビューでは `review-snapshots/<job>/cycle-N` の worktree が残る。
`a_job_cleanup` 自体もブランチ・`jobs/<id>`・snapshot は消さない。README の「深刻な失敗時の
worktree は保持（手動 cleanup）」は成功時に消す前提の記述だが、成功時も消していない。

修正: `COMPLETED / CANCELLED / FAILED` 到達時に supervisor から `job_cleanup` を呼ぶ
（保持したいなら retention 日数）。review の全終端で `review_cleanup`。cleanup でブランチ削除と
`git worktree prune`。ブリッジ起動時に孤児 snapshot を掃除。

### B5. worktree 全走査を毎秒実行（Medium）

`supervise_process`（460-558 行）は `poll_s=1.0` ごとに `mtime_fn` を呼び、`worktree_max_mtime`
（438-445 行）は `list(Path.rglob("*"))[:2000]` と全列挙してから切る。`node_modules` 等を含む
worktree では Pi の IO/CPU を食い、B1 と合わせてブリッジ応答を遅らせる。

修正: 走査は 15〜30 秒に 1 回、`os.scandir` で上限打ち切り、`.git` / `node_modules` を除外。

### B6. 表示上の不整合（Low）

`any_active_review()`（144 行）の戻り値が同じ名前を `<name>/<name>` と二度連結している。
`handle()` の `needs_body` は未使用。

## C. ワーカー側（`src/jobs/worker/workerctl_server.py`、エージェント実行）

### C1. タイムアウト／キャンセルがエージェントの子プロセスを殺さない（High・再現済み）

`_run_job`（349-357 行）は `subprocess.run(shell=True, timeout=)`。Python は直接の子だけを
SIGKILL し、エージェントが起動した MCP サーバ・ツール・テストの子プロセスは残る。
再現: `sleep 300 | grep ...` を 1 秒でタイムアウトさせた後も `sleep 300` が生存。
`cmd_job_cancel`（484-502 行）は runner の pid に SIGTERM を送るだけで、エージェント（孫）には
届かない。残った孫は worktree を書き換え続けるので、その後の changed_files・secret scan・テストが
「動いている対象」に対して走る。`run_tests`（241-252 行）も同じ。

修正: `Popen(start_new_session=True)` ＋ タイムアウト／キャンセル時に `os.killpg(pgid, SIGTERM)`
→ 猶予 → SIGKILL。`run_job_detached` は既に `start_new_session=True` なので cancel 側を killpg に
変えるだけで runner 配下全体が止まる。同系統: `agents_local.py:52`（fixture 専用）、
`event_extract.py:326`（`docker compose exec` の fallback。クライアントを殺してもコンテナ内の
`hermes -z` は残る）。ブリッジ側は `docker stop` ＋ `docker rm -f` で閉じており問題ない。

### C2. RemoteWorkerAdapter は期限超過でリモートを止めない（Medium）

`transport.py:193-196` は `INTERRUPTED` を返すだけで `workerctl job_cancel` を送らない。
ワーカー上でジョブが走り続ける。

### C3. workerctl の pid ファイルは PID のみ（Low）

277 行。supervisor 側（Item 45）の start_ticks 照合と不揃いで、PID 再利用時に誤って SIGTERM を
送り得る。

## D. Channel Gateway（Hermes 呼び出し）

### D1. タイムアウトした run の答えは回収されない（Medium）

`gw_hermes.py:181-199` の docstring は「run id を記録しているので後で回収できる」と言うが、
`gw_service.py:411-414` は timeout を即エラー返信して `RESPONDED` で確定し、`hermes_run_id` は
書くだけ（`gw_store.py:109`）で読む箇所が無い。Hermes 側では run が完走し、結果は捨てられる。
利用者には「時間超過」だけが届く。

修正: timeout 時は inbound を保留状態で持ち、housekeeping で `get_run` を再確認して完成していれば
配信、一定時間で諦める。少なくとも docstring を実態に合わせる。

### D2. 処理リースと Hermes 待ち時間の余裕が薄い（Medium）

`PROCESSING_LEASE_SECONDS = 300`（`gw_store.py:43`）に対し、`start_run`（connect 30 秒）＋
poll 240 秒 ＋ 最後の `get_run` 30 秒 ＝ 300 秒。配信（`claim_discord_chunk` まで `claimed_at` の
更新なし）の前に `recover_stale`（182-237 行、5 分ごと）がリースを切り得る。切られると `QUEUED`
に戻り、同じ idempotency key で Hermes は replay するが、待ちをもう一度やり直す。

修正: Hermes ポーリング中に `claimed_at` を延長する（ハートビート）か、lease を
`absolute_timeout + 配信最悪値 + 余裕` にし、設定検証で `lease > absolute_timeout + connect_timeout × 2`
を強制する。

### D3. `soft_timeout_seconds` は読まれていない（Low）

`gw_config.py:59` に既定値があるだけで参照が無い。

## E. systemd / 定期ジョブ

### E1. `hermes-line-reconcile.service` の `TimeoutStartSec=10min` は最悪所要時間より短い（Medium）

`reconcile-line.sh` は `--max-llm-calls 12`。`classify_one`（`event_extract.py:300-333`）は broker
45 秒 × 2 試行 ＋ 3 秒で 1 候補最大 93 秒、12 候補で約 19 分。Antigravity 側が遅い／504 を返す
ときにちょうど伸びる。systemd が 10 分で SIGTERM → `set -e` のシェルは health JSON を書かずに
死に、stage 2（commitments backfill）が走らない。`hermes-line-refresh.service`（5 分）も SSH
スナップショット ＋ index 再構築 ＋（書込可なら）同じ抽出を含む。

修正: `TimeoutStartSec` を最悪値以上にするか、抽出側に 1 回あたりの合計予算を持たせて自分で
打ち切る。`reconcile-line.sh` に `trap ... TERM` を入れて health を書いてから終了する。
`hermes-gmail-event-ingest.service` は oneshot で期限未指定（oneshot の既定は無期限）なので
殺されることは無いが、逆に無限に待ち得る点は把握しておく。

### E2. ブリッジだけ systemd 化されていない（Low、README 既知）

systemd 化（`Restart=on-failure`、`KillMode=mixed`）すれば B2 の二重起動と残骸プロセスは
仕組みとして防げる。

## 推奨する着手順

1. **局所修正で症状を直接止める**: A1、A4、A2（running ≠ finished）、A3（cancel 伝播）、C1（killpg）。
   `tests/` に回帰を足す。いずれも数十行。
2. **ブリッジ**: B3（起動時再整合）→ B1（接続スレッド化 ＋ ロック）→ B2（watchdog）→ B5。
3. **回収と後始末**: A5、A6、A7、A8、B4。
4. **周辺**: D1、D2、E1。

## 検証状況

- `tests/` 129 件 OK（Python 3.13.17、本環境）。
- 再現スクリプト 3 本（A1、B1、C1）を本セッションの scratchpad で実行して確認した。
  いずれも標準ライブラリのみで、Docker や実ブリッジは使っていない。
- 未検証: 実機 Pi での発生頻度、実 `jobs.db` の残留ジョブ数、実ブリッジの `jobs/` 配下の
  `running` 残骸の有無。着手前に実機で `jobs/*/state.json` と `job_list` を突き合わせると、
  B3／A7 の実害が数字で出る。
