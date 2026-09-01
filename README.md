# Hermes Personal Chief of Staff — Infrastructure Work Log

個人AIアシスタント「Hermes」(Personal Chief of Staff) の運用基盤に対する
一連の保守・強化・隔離フェーズの作業記録とコードスナップショット。

単一のRaspberry Pi 4 (arm64, Docker) 上で稼働するHermesに対し、
Security Boundaryを維持したまま以下を段階的に構築した。

```text
Hermes Container (Personal AI: SOUL / MEMORY / LINE Knowledge / Events)
        │  isolated (no docker.sock, no host fs)
        ▼
Coding Supervisor (jobs.db + typed MCP)
        │  typed Unix RPC (action allowlist)
        ▼
Host Coding Bridge
        ▼
Disposable Worker Container (cap-drop ALL, read-only rootfs)
        ▼
Coding Agent → isolated git worktree → deterministic tests
        ▼
evidence → IMPLEMENTATION_ACCEPTED → WAITING_REVIEW
```

## 原則

- **Agent process finished ≠ Task completed** — 完了はAcceptance Criteriaの
  deterministic validationによってのみ確定する
- **test未実行 → acceptance不可**(H1: test_command criterion は実行済み
  exit 0 の test のみで PASS。NOT_RUN は決して PASS にならない)
- **commit済み変更も検査対象**(H2: canonical changed set = base_sha からの
  committed diff UNION working tree。commit で scope/secret 検査が空振り
  しない)
- **cancel は terminal intent**(H4: cancel 後に validation / acceptance へ
  進まない。cancel_requested_at を永続化し finalize は上書きしない)
- Hermes Personal Data (SOUL/MEMORY/LINE/Events) はCoding Agentから到達不可。
  **隔離は container / filesystem boundary による**
  (`--ignore-rules` は個人context注入を止めない — 実測で確認済み。
  旧前提「--ignore-rules gives personal-context isolation」は訂正)
- repository allowlist / test command allowlist / secret scan / no push / no merge

## Deployment Source of Truth

```text
実機 Pi に deploy されているのは src/docker-compose.hermes.yaml
  (image digest pin 済み、bridge-sock mount 含む)
docker-compose.yaml / docker-compose-stageB.yaml は template / snapshot
  (:latest は digest pin 前の初期取得用の意味のみ)
```

## Phase Index

| # | Document | 概要 |
|---|---|---|
| 1 | [docs/01-maintenance.md](docs/01-maintenance.md) | hostname整合 / image digest pin / config v39移行確認 / regression |
| 2 | [docs/02-pre-b2-safety-gate.md](docs/02-pre-b2-safety-gate.md) | context scanner誤検知修復 / canonical rule配置 / Event Layer監査 |
| 3 | [docs/03-phase-b2-event-layer.md](docs/03-phase-b2-event-layer.md) | Event Layer本番投入(14日backfill) / MCP拡張 / 品質review |
| 4 | [docs/04-phase-f1-supervisor.md](docs/04-phase-f1-supervisor.md) | Coding Supervisor (jobs.db / continuation loop / false completion拒否) |
| 5 | [docs/05-phase-f11-isolation-gate.md](docs/05-phase-f11-isolation-gate.md) | ignore-rules実測 / transport adapter / lifecycle移行 / secret scan |
| 6 | [docs/06-phase-f11pi-same-host-worker.md](docs/06-phase-f11pi-same-host-worker.md) | 同一Pi上の分離Worker container / typed bridge / 隔離実証 |
| 7 | [docs/07-phase-f11pi2-hardening.md](docs/07-phase-f11pi2-hardening.md) | Supervisor Hardening Gate (H1-H5 / M1-M8 / 回帰83tests) |
| - | [docs/lessons-learned.md](docs/lessons-learned.md) | 実測で見つかった落とし穴と修正 |

## Code Snapshot (`src/`)

実機で稼働しているコードの匿名化スナップショット。
パス中の `<PI_USER>` `<LAN_IP>` `<PI_HOSTNAME>` は環境ごとのプレースホルダ。

```text
src/
├── jobs/                       Coding Supervisor (container内, stdio MCP)
│   ├── jobs_db.py              job/run/event_log SQLite層 (cancel CAS / 監査)
│   ├── supervisor.py           state machine + continuation + canonical changed set
│   ├── validator.py            deterministic acceptance validator
│   ├── transport.py            adapter群 (local fixture / pi-sandbox / remote)
│   ├── agents_local.py         fixture agent実行(test専用)
│   ├── jobs_mcp.py             typed MCP server (9 tools, generic shell無し)
│   ├── supervisor_runner.py    detached runner entry
│   ├── test_fixtures/          deterministic test agent
│   └── worker/                 remote worker protocol (workerctl)
├── bridge/                     Host Coding Bridge (host側)
│   ├── bridge.py               typed Unix-socket RPC, 非blocking watchdog
│   ├── config.json             repo registry / limits / agent commands
│   └── Dockerfile              worker image (arm64)
├── security_patterns.py        canonical secret patterns (単一ソース)
├── bin/ + worker/              fixture agent, watchdog, worker側設定
├── tests/                      hardening 回帰テスト (unittest, 83 tests)
└── docker-compose.hermes.yaml  実機 deploy 対象 (digest pin)
```

## Status

- Supervisor / Bridge / 分離Worker / 全isolation test: 稼働・検証済み
- F.1.1-Pi.2 Supervisor Hardening Gate: 完了 (H1-H5 / M1-M8、回帰テスト全PASS)
  - 実Production Coding Agent (Hermes CLI + CommandCode) の接続は
    この Hardening Gate 完了後に行うこと
- Next: 実Coding Agent接続 → Phase F.2 — Independent Coding Review & Fix Loop
