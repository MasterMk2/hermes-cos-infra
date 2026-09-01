# Coding Worker Supervisor (Phase F.1)

HermesはSupervisor / Chief of Staff。Coding AgentはWorker。
**Agent process finished ≠ Task completed** — 完了はAcceptance Criteriaの
検証(validator)によってのみ確定する。

## Pipeline

```text
Hermes (Supervisor)
  ↓ job MCP (stdio): job_create / job_dispatch / job_get / job_list /
    job_status / job_continue / job_validate / job_cancel / job_reconcile
jobs.db (SQLite, canonical state)
  ↓ detached runner (supervisor_runner.py, crash-safe)
Agent Run (fixture agents: simulated / fixture-local-hermes、cwd=repository)
  ↓ stdout/stderr → logs/, git base→HEAD diff + working tree, canonical changed files
Validator (deterministic, agent自己申告を信用しない)
  ├─ all criteria PASS → IMPLEMENTATION_ACCEPTED (review_status=WAITING_REVIEW)
  └─ incomplete → CONTINUE_REQUIRED → continuation prompt → next run
       (attempt limit / no-progress で停止 → WAITING_HUMAN)
```

## F.1.1-Pi.2 Hardening (2026-09-01)

外部レビューで発見された高優先度バグの修正。動作は
`hermes-cos-infra/tests/` (83 tests) で回帰検証済み。

- **H1 test-not-run bypass 修正**: `test_command` criterion は
  「登録済み test command が実際に実行され、exit 0」のときのみ PASS。
  `NOT_RUN / UNKNOWN / MISSING / ERROR / TIMEOUT / BLOCKED` は常に UNMET。
  job_create 時にも相関を強制(test criterion があるのに tests_command
  無し → reject `test_command_required_for_test_criterion`)。
- **H2 committed changes 可視化**: job worktree には作成時に `base_sha`
  を記録し、changed files の canonical set は
  `git diff --name-only <base>...HEAD` **UNION** `git status --porcelain -z`
  (staged/unstaged/untracked 含む)。Agent が commit して working tree が
  clean になっても、scope validation / secret scan / evidence は空振りしない。
  porcelain は `-z`(NUL 区切り)でパースし、quoted path / rename に対応。
- **H4 cancellation 永続化**: `job_cancel` は terminal intent。
  `cancel_requested_at` を jobs.db に記録し、Supervisor は agent 返還後・
  validation 後・acceptance write 前に再確認、acceptance は CAS
  (`UPDATE ... WHERE status='VALIDATING'`)。bridge/workerctl の worker
  thread も finalize 時に cancelled を上書き禁止。
- **H5 job_validate guard**: pi-sandbox/remote job への手動ローカル
  validation は `validation_must_run_via_worker_adapter` で reject。
  reject 時は acceptance_results / status / evidence を一切変更しない。
- **M1**: per-job `max_attempts` を優先(global config は legacy NULL のみ
  fallback)。
- **M2**: dispatch は CAS claim (`already_claimed` で二重 runner 排除)。
- **M3**: Supervisor は偽 diff (changed-files の JSON) を scan しない。
  sandbox job の secret scan は bridge/workerctl が authoritative。
- **M6**: progress signature は base/head sha、canonical changed file 名、
  diff hash、tests status、unmet criteria id から生成(同名数でも diff が
  変われば progress 扱い)。内容は DB に保存せず hash のみ。
- **M7**: git state 不明は fail-closed(`WAITING_HUMAN`、dirty=false 扱い
  禁止)。runner spawn 失敗は `FAILED_TO_START`(job_continue で再試行可)。
- **M8**: `AGENT_STOPPED` 等の状態変更はすべて `transition()` 経由で
  event_log に監査跡を残す。`finished_at` は terminal execution states
  (IMPLEMENTATION_ACCEPTED / FAILED / CANCELLED / COMPLETED)で記録、
  `implementation_finished_at` は AGENT_STOPPED で記録。
- **Item 43**: `artifact_contains` は bounded read(超過時
  `artifact_too_large`)。
- **Item 45**: runner pid file は PID + process start time(JSON)。
  PID 再利用による誤 reconcile を防止。
- **Item 46**: timestamp は UTC ISO-8601 に統一(新規書き込みから)。

Secret patterns は `src/security_patterns.py` に単一化(supervisor /
bridge / workerctl 間の drift は `tests/test_security_patterns.py` で検出)。

## Layout

| path | role |
|---|---|
| jobs.db | Job / Agent Run / event log (canonical) |
| prompts/ | 各attemptのprompt package |
| logs/ | runner / stdout / stderr |
| artifacts/ | 将来の成果物 |
| supervisor.py | state machine + continuation logic |
| validator.py | deterministic acceptance evaluation |
| agents_local.py | agent execution (fixture agents / simulated) |
| test_fixtures/sim_agent.py | deterministic test agent (fixture repoのみ) |
| worker_config.json | worker設定 + repository allowlist(人間管理) |
| worker/ | remote worker transport seam (SSH + workerctl) |
| ../tests/ | hardening 回帰テスト (unittest) |

## Lifecycle

Phase F.1 (旧):

```text
... → VALIDATING → ACCEPTED → COMPLETED
```

Phase F.1.1 (現行・semantic migration):

```text
... → VALIDATING → IMPLEMENTATION_ACCEPTED (review_status=WAITING_REVIEW)
                                      → Phase F.2 Independent Review → COMPLETED
INTERRUPTED: remote worker到達不能時の安全状態(job保存、COMPLETED扱いしない)
```

- 既存fixture jobsの `COMPLETED` はF.1 test artifactとして保存(変換しない)
- 新規jobは上記新lifecycleで動作。DB migration不要(statusは文字列、
  ACCEPTEDは後方互換のため残存、新規使用はしない)
- COMPLETEDへの到達はF.2のIndependent Review後のみ

## F.1.1 Runtime Audit Results (2026-09-01)

- `hermes -z --ignore-rules`: **無効** — prompt-size compositionが同一
  (memory 1,721B / user profile 798B / context 1,458B 不変)。
  → container内hermes-as-coderは個人contextを注入する。前提訂正。
- filesystem reachability: container内agent identityから
  `/opt/data/.env` (mode 600, 同owner) / memories / events.db / 
  knowledge-index / jobs 設定 すべて **readable**。
  → filesystem isolation = insufficient。production実行は外部workerのみ。

## Transport Adapters

```text
LocalFixtureAdapter  — 開発/test fixture専用(agent: simulated / fixture-local-hermes,
                       kind='test'かつallow_simulated repoのみ)。本番禁止(guard実装)
RemoteWorkerAdapter  — restricted SSH + workerctl(外部worker、Claude Code/Codex)。
                       worker未設定の本番job → WAITING_HUMAN(job保存)
```

worker側セットアップ: `worker/README.md`(ユーザー実施)。
SSH制限検証: `worker/validate_ssh.sh`。

## Security Model

- repositoryはallowlist名でのみ指定可能。新規path登録は人間承認(本README +
  worker_config.jsonの人間による編集)
- validatorが実行するのはallowlist済みtest commandのみ。generic shell toolは存在しない
- dirty repositoryはdispatch前にWAITING_HUMAN(既存変更を勝手にcommit/resetしない)
- Job毎に専用local branch `hermes/job-<id>`。main/masterへの直接commitなし
- git push / PR / merge / deployは本Phaseでは実装・実行しない
- **個人context隔離は container / filesystem boundary による**
  (Runtime Audit のとおり `--ignore-rules` は個人context注入を止めない。
  旧前提「--ignore-rules gives personal-context isolation」は訂正済み。
  Production jobの実行境界は PiSandboxWorkerAdapter / bridge コンテナ)
- worker agent の prompt に Personal Memory / LINE / Event DB は含めない
- prompt/logへsecretを出力しない

## Remote Worker (transport seam, 未接続)

Worker machine情報(IP/OS/username/repo path)は人間が提供するまで未接続。
接続手順は worker/README.md を参照。専用SSH鍵(no-port-forwarding等)+
workerctl wrapperのみを許可する設計。

## Manual operations

```bash
docker exec hermes python3 /opt/data/jobs/jobs_db.py            # init
docker exec hermes python3 /opt/data/jobs/supervisor.py <job_id>  # 手動drive
```
