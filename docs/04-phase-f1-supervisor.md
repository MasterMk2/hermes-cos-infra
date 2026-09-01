# Phase 4 — F.1 Coding Agent Worker & Supervision Loop

## 目的

「Agentが1回応答して停止」を「Task完了」としないSupervisor Loopの実装。

## Architecture

```text
Hermes (Supervisor)
  ↓ jobs-supervisor MCP (stdio, 9 tools, generic shell tool無し)
jobs.db (SQLite: jobs / runs / event_log)
  ↓ detached runner (crash-safe)
Agent Run → stdout/stderr, git before/after, changed files
Validator (deterministic, agent自己申告を信用しない)
  ├─ 全criteria PASS → ACCEPTED
  └─ 未達 → CONTINUE_REQUIRED → continuation prompt → 次run
       (attempt limit / no-progress で停止 → WAITING_HUMAN)
```

## 実装

- `jobs_db.py`: job / agent run / event_log 層。agent outputはfile保存(DB巨大化回避)
- `supervisor.py`: state machine / continuation prompt生成 /
  progress signature (git sha + tests + criteria pass + files) /
  same-failure early stop
- `validator.py`: verification kinds = file_exists / function_exists /
  test_command (repo allowlist必須) / git_scope / artifact_contains
- `jobs_mcp.py`: job_create/get/list/status/dispatch/continue/validate/cancel/reconcile
  - repository はallowlist名のみ受付(新規path登録不可)
  - 任意shell commandを渡せるtoolは意図的に存在しない
- git safety: dirty repo → WAITING_HUMAN / job専用branch `hermes/job-<id>` /
  push・merge不実装

## E2E Tests (fixture repo, 全PASS)

| Test | 内容 | 結果 |
|---|---|---|
| T1 continuation | run1部分完了→CONTINUE_REQUIRED→run2→全criteria PASS→COMPLETED | PASS |
| T2 real agent | 実CLIで実装→scope遵守(1file)→tests PASS→完了 | PASS |
| T3 false completion | agentが「Done」と申告してもtest失敗→COMPLETED不遷移→WAITING_HUMAN | PASS |
| T4 no-progress | 同一signature 3連続で自動停止(無限継続なし) | PASS |
| T5 disconnect | worker offline→job QUEUED保存 | PASS |
| T6 recovery | RUNNING+runner死→reconcile→AGENT_STOPPED→job_continue復帰 | PASS |

## Notes

- 選定agent: 当時はHermes同梱CLI(非対話mode) — 後続F.1.1でfixture専用に降格
- 複数の実装bugを発見・修正: runs schema列不足 / argv順 / MCP spawn時の
  env sanitization対策(HERMES_HOME導出)
