# Phase 7 — F.1.1-Pi.2 Supervisor Hardening Gate

## 目的

実Production Coding Agent接続前の安全ゲート。新機能追加ではなく、
外部レビューで発見された高優先度バグの修正により
`Agent stopped ≠ Task completed` 保証を実装レベルで堅牢化する。

## Critical Fixes (H1–H5)

| ID | 問題 | 修正 |
|---|---|---|
| H1 | `test_command` criterion が test 未実行 (`NOT_RUN`) のまま PASS し、Supervisor も `NOT_RUN` を許容して `IMPLEMENTATION_ACCEPTED` に到達可能 | criterion は「登録済み test command が実行され exit 0」のみ PASS。NOT_RUN/UNKNOWN/MISSING/ERROR/TIMEOUT/BLOCKED は常に UNMET。job_create 時にも tests_command 必須を強制 |
| H2 | changed_files が `git status --porcelain` 由来のみで、Agent が commit すると working tree が clean になり scope/secret 検査が空振り | worktree 作成時に `base_sha` を記録し、canonical changed set = `git diff --name-only <base>...HEAD` UNION `git status --porcelain -z`。staged/unstaged/untracked も正規化 |
| H3 | bridge の `proc.stdout.readline()` が blocking で、無出力 agent が watchdog (timeout/hang/heartbeat) を無効化 | reader thread + queue 化。監視ループは一定周期で必ず戻り、process alive / absolute timeout / last output / mtime / cancel を確認 |
| H4 | cancel が loop 先頭でしか確認されず `CANCELLED→VALIDATING→ACCEPTED` が可能。bridge/workerctl の生存 worker thread が後から `finished` で上書き可能 | `cancel_requested_at` を永続化。agent 返還後・validation 後・acceptance write 前に再確認。acceptance は CAS (`WHERE status='VALIDATING'`)。finalize は cancelled を上書き禁止 |
| H5 | `job_validate` が pi-sandbox 本番 job を Supervisor ローカル FS で評価し、誤 FAIL で acceptance_results を上書き | adapter-aware guard。pi-sandbox/remote job は `validation_must_run_via_worker_adapter` で reject。reject 時は状態を一切変更しない |

## State-machine / Security Fixes (M1–M8 + items)

- **M1** per-job `max_attempts` を優先(legacy NULL のみ global fallback)
- **M2** dispatch CAS claim(同時 dispatch は 1 勝者、runner 1 つのみ)
- **M3** Supervisor の偽 diff scan (`json.dumps(changed_files)`) を削除。
  bridge/workerctl の secret scan が authoritative
- **M4** workerctl `_run_job` を SSH 表面から排除(SSH_ORIGINAL_COMMAND
  存在時は internal command reject)
- **M5** Network egress threat model を README に明記(LLM provider と
  許可された依存解決のために outbound は必要、inbound port 無し)
- **M6** progress signature を base/head sha・canonical changed names・
  diff hash・unmet criteria ids から生成(同件数でも diff 変化は progress)
- **M7** git state 不明は fail-closed(WAITING_HUMAN)。spawn 失敗は
  FAILED_TO_START(event_log 記録、job_continue で再試行可)
- **M8** terminal execution states で `finished_at`、AGENT_STOPPED で
  `implementation_finished_at` を記録。状態変更は全て transition() 経由で
  event_log に監査跡。CAS 経路も同 coherence
- **Item 30** secret patterns を `src/security_patterns.py` に単一化
  (bridge の authorization_header を含む canonical set に統一)
- **Item 43** `artifact_contains` は bounded read(超過時 `artifact_too_large`)
- **Item 44** porcelain は `-z`(NUL 区切り)でパース(quoted path/rename 対応)
- **Item 45** runner pid file は PID + process start time(JSON)で PID 再利用対策
- **Item 46** timestamp は UTC ISO-8601 に統一(新規書き込みから、既存 data
  の破壊的 migration は無し。DB への column 追加は additive のみ)
- **Item 40/41/42** bridge.sock residual risk / Pi resource budget /
  dashboard TLS TODO を README に明記

## Regression (unittest — `src/tests/`, 83 tests)

```text
H1: test-not-run bypass / creation-time reject / executed pass/fail     PASS
H2: committed-change detection (supervisor/bridge/workerctl)            PASS
H3: silent-process watchdog (hang / absolute timeout / cancel / loud)   PASS
H4: cancel during RUNNING / at VALIDATING / bridge+workerctl CAS        PASS
H5: production validate guard (reject + no mutation) / fixture permitted PASS
M1: per-job attempts (2 vs 6, 6 vs 2, legacy NULL fallback)             PASS
M2: dispatch CAS (concurrent / sequential)                              PASS
M6: progress signature (content-based)                                  PASS
M7: git fail-closed / FAILED_TO_START + event log                       PASS
M8: lifecycle timestamps / state audit log                              PASS
Item 43: artifact_too_large (validator / bridge)                        PASS
M4: workerctl internal command not SSH-exposed                          PASS
pattern drift: 3 consumer equality                                      PASS
Integration: E2E accept+continuation / false completion / committed-
change E2E / cancellation / double dispatch / crash reconcile            PASS
py_compile: 全モジュール OK
```

(`/proc` 依存の PID-reuse start-time test は POSIX のみで実行、
Windows 開発環境では skip。実機 Pi で有効。)

## Deployment Notes

- `security_patterns.py` の配置: jobs → `/opt/data/`、bridge →
  `~/coding-worker/`、worker → workerctl と同じ bin/
- DB migration は additive のみ
  (`jobs.cancel_requested_at`, `jobs.implementation_finished_at`,
  `runs.diff_hash` — 初回 init/起動時に自動適用)
- 実機 compose (`src/docker-compose.hermes.yaml`) の変更は無し

## Remaining Risks

- 実 Docker / 実 SSH worker 上での E2E(bridge コンテナ起動含む)は
  この開発環境では実行不可。実機 Pi で fixture E2E の再実行を推奨
- egress のドメイン絞り込み(proxy / DNS allowlist)は未実施
- bridge の cron watchdog 方式は継続(専用 user + systemd 化は将来強化)
