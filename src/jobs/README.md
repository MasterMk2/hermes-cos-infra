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
Agent Run (hermes-cli: hermes -z --ignore-rules --yolo, cwd=repository)
  ↓ stdout/stderr → logs/, git before/after, changed files
Validator (deterministic, agent自己申告を信用しない)
  ├─ all criteria PASS → ACCEPTED → COMPLETED
  └─ incomplete → CONTINUE_REQUIRED → continuation prompt → next run
       (attempt limit / no-progress で停止 → WAITING_HUMAN)
```

## Layout

| path | role |
|---|---|
| jobs.db | Job / Agent Run / event log (canonical) |
| prompts/ | 各attemptのprompt package |
| logs/ | runner / stdout / stderr |
| artifacts/ | 将来の成果物 |
| supervisor.py | state machine + continuation logic |
| validator.py | deterministic acceptance evaluation |
| agents_local.py | agent execution (hermes-cli / simulated fixture) |
| test_fixtures/sim_agent.py | deterministic test agent (fixture repoのみ) |
| worker_config.json | worker設定 + repository allowlist(人間管理) |
| worker/ | remote worker transport seam (SSH + workerctl、未接続) |

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
- worker agentは`--ignore-rules`で最小context(SOUL/MEMORY/AGENTSを注入しない)、
  Personal Memory / LINE / Event DBはpromptへ含めない
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
