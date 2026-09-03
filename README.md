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
        │
        ▼
Independent Reviewer Container (別model / 別HERMES_HOME / 別session)
        │  read snapshot of target_sha のみ。実装worktreeもtranscriptも渡さない
        ▼
structured findings → APPROVED  or  CHANGES_REQUIRED → 実装fix → 再review
        ▼
completion_decision() — exact target SHA + content binding + diff hash
        + 登録test再実行 + secret scan + cancel guard + CAS
        ▼
COMPLETED
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
- **implementation accepted ≠ completed / reviewer said approved ≠ completed**
  (F.2: `COMPLETED` は `completion_decision()` 一箇所だけが書き、
  独立レビューの承認が **その exact revision** に bind されていること、
  最新の deterministic 検証が通ること、cancel されていないことを全て要求する。
  承認後に worktree が動けば承認は無効化され再レビューになる)
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
| 8 | [docs/08-phase-f2-independent-review.md](docs/08-phase-f2-independent-review.md) | Independent Reviewer (Muse) / fix loop / exact-revision completion gate |
| - | [docs/F2-FINAL-REPORT.md](docs/F2-FINAL-REPORT.md) | F.2 実測レポート (real fix loop / 隔離実測 / usage / 回帰) |
| 9 | [docs/09-phase-d1-discord-knowledge.md](docs/09-phase-d1-discord-knowledge.md) | Discord read-only Knowledge (collector / 増分index / discord_* MCP / Event統合) |
| - | [docs/D1-STATUS-REPORT.md](docs/D1-STATUS-REPORT.md) | D.1 実測レポート (実Discord E2E / 敵対的監査 / 除外判断の根拠) |
| - | [docs/lessons-learned.md](docs/lessons-learned.md) | 実測で見つかった落とし穴と修正 |

## Code Snapshot (`src/`)

実機で稼働しているコードの匿名化スナップショット。
パス中の `<PI_USER>` `<LAN_IP>` `<PI_HOSTNAME>` は環境ごとのプレースホルダ。

```text
src/
├── jobs/                       Coding Supervisor (container内, stdio MCP)
│   ├── jobs_db.py              job/run/event_log/reviews SQLite層 (cancel CAS / 監査)
│   ├── supervisor.py           state machine + continuation + review phase
│   │                           + completion_decision() (唯一のCOMPLETED writer)
│   ├── review.py               Reviewer persona / 出力schema validator /
│   │                           finding fingerprint / review package builder
│   ├── validator.py            deterministic acceptance validator
│   ├── transport.py            adapter群 (local fixture / pi-sandbox / remote)
│   ├── agents_local.py         fixture agent実行(test専用)
│   ├── jobs_mcp.py             typed MCP server (12 tools, generic shell無し)
│   ├── supervisor_runner.py    detached runner entry
│   ├── test_fixtures/          deterministic test agent
│   └── worker/                 remote worker protocol (workerctl)
├── bridge/                     Host Coding Bridge (host側)
│   ├── bridge.py               typed Unix-socket RPC, 非blocking watchdog,
│   │                           review snapshot / reviewer container / job_verify
│   ├── config.json             repo registry / limits / agent commands
│   └── Dockerfile              worker image (arm64)
├── reviewer-hermes/            Reviewer専用Hermes config (ro mount 元)
├── security_patterns.py        canonical secret patterns (単一ソース)
├── bin/ + worker/              fixture agent, reviewer runner, watchdog, worker設定
├── discord-knowledge/          Phase D.1 — Discord read-only Knowledge
│   ├── app/                    collector / normalize / store / backfill / indexer
│   ├── mcp/discord_mcp.py      discord_search / discord_get / discord_status
│   ├── events/                 discord_event_extract.py (既存Event Layerへ接続)
│   ├── systemd/ + install-root.sh + bin/   専用user・LoadCredential・配備
│   └── config/config.yaml      allowlist (default deny)。tokenは絶対に置かない
└── docker-compose.hermes.yaml  実機 deploy 対象 (digest pin)

tests/                          F.2 回帰テスト (repo root、src/ の外。unittest 129 tests)
tests-discord/                  D.1 テスト (host 94 + Hermes container 32 = 126)
docs/                           フェーズ文書 + 実測レポート
```

## Status

- Supervisor / Bridge / 分離Worker / 全isolation test: 稼働・検証済み
- F.1.1-Pi.2 Supervisor Hardening Gate: 完了 (H1-H5 / M1-M8、回帰テスト全PASS)
- F.1.1-Pi / Pi.2 / Pi.3: 完了 (実Production Coding Agent 接続済み、
  Hermes CLI + CommandCode + GLM-5.3-Flash)
- F.2 Independent Coding Review & Fix Loop: Reviewer (Muse Spark 1.2) /
  review schema / fix loop / exact-revision completion gate を実装・実機検証。
  production coding job は **独立レビュー承認 + exact revision 一致 + 最終
  deterministic 検証** を通らない限り `COMPLETED` にならない
- 回帰テスト: 129 tests OK
  (F.1.1 hardening 83 + F.2 review 25 + F.2 binding/scope/cost/surface 21)
- D.1 Discord Knowledge Ingestion: **本番稼働中**。3 Guild / 116 channel から
  1,685件を取り込み、`discord_search`・`discord_get`・`discord_status` で
  Hermesから検索可能。Event Layerへ `source_type=discord` で合流し、
  actor判定は66/66一致。Bot TokenはHermesに渡さず、送信toolが存在しない。
  テスト126 + 実データ不変条件23/23 PASS。配備後に6レンズの敵対的監査を行い、
  forum履歴が無言で欠落する欠陥など6件を修正
  (詳細: [docs/D1-STATUS-REPORT.md](docs/D1-STATUS-REPORT.md))
- Real Fix Loop 実測済み (Pi実機・両roleとも実APIコール):
  GLM実装 → deterministic acceptance → **Muse が CHANGES_REQUIRED (blocking 4)**
  → GLM が実際にコードを修正 (content binding が移動) → fresh Muse review
  → APPROVE → exact-revision gate → COMPLETED
  (詳細: [docs/F2-FINAL-REPORT.md](docs/F2-FINAL-REPORT.md))
