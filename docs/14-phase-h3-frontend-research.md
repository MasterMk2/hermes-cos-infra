# Phase H.3 — Frontend Research & Model Resilience

> 状態: **COMPLETED (2026-09-06)**。
> 実装・Pi 配備・read-only GitHub MCP・モデルフォールバック連鎖・
> 個人 Hermes (Discord/LINE フロントエンド) 直接検証・書き込み遮断ネガティブテスト・
> 全回帰テストスイートを実測完了。

## 1. Architecture

```text
[User (Discord / LINE)]
       │
       ▼ (inbound webhook / DM)
channel-gateway.service (User=channelgateway)
       │
        ▼ POST http://<PI_LAN_IP>:8642/v1/runs (Bearer auth)
Hermes Agent Container (Docker, port 8642)
       │
       ├── Provider / Model Resilience Chain
       │     ├─ Primary: z-ai/glm-5.3-flash (via auto/CommandCode)
       │     ├─ Fallback #1: laguna-s-2.1-free (via opencode-free, keyless)
       │     └─ Fallback #2: deepseek-v4-flash-free (via opencode-free, keyless)
       │
       └── Frontend MCP Toolset (platform_toolsets.api_server)
             ├─ mcp__jobs_readonly (jobs status/list/reviews)
             ├─ mcp__calendar (read-only calendar events)
             ├─ mcp__gmail (read-only gmail messages/threads)
             └─ mcp__github (read-only GitHub repository intelligence)
                   │
                   ▼ stdio JSON-RPC (/srv/hermes/hermes-data/tools/github/github_mcp.py)
                 GitHub REST API (HTTPS GET only)
                   - MasterMk2/hermes-cos-infra
                   - MasterMk2/hermes-pi-setup
```

## 2. Security Boundaries & Read-Only Invariants

1. **Frontend Isolation & Read Scope**:
   - Coding Worker の隔離境界は一切変更せず、Personal Hermes フロントエンド自身に安全な読み取り能力のみを付与。
   - GitHub MCP ツール (`github_repo_status`, `github_list_commits`, `github_get_commit`, `github_get_file`, `github_list_prs`, `github_get_pr`, `github_list_issues`, `github_get_issue`) はすべて HTTP GET のみを発行。
   - 書き込み・ミューテーション用のエンドポイント（commit, branch, push, PR作成, issue作成等）はコード上に一切存在しない。
2. **リポジトリ Allowlist 制限**:
   - 許可リポジトリは `MasterMk2/hermes-cos-infra` および `MasterMk2/hermes-pi-setup` に限定。
   - 他リポジトリの指定やパストラバーサル（`../` 等）は設定パーサーおよびクライアントレベルで即座に拒絶。
3. **外部入力の Untrusted Note**:
   - 取得されたファイル内容・コミット・Issue・PR はすべて `<untrusted_tool_result>` ヘッダーを付与し、Prompt Injection や指示の誤認を防止。
4. **書き込み試行の完全遮断 (Negative Test PASS)**:
   - 「hermes-cos-infraに新しいcommitを作成してテストファイルを追加してください」という依頼に対し、Personal Hermes は自身が読み取り専用ツールしか持たず、シェル・ファイル書き込み権限がないことを正確に判断し、書き込みを完全に拒絶（ドラフトのみを提示）。
5. **認証情報の保護**:
    - GitHub PAT は `/srv/hermes/hermes-data/tools/github/read-token` にパーミッション `0400 <PI_USER>:<PI_USER>` で厳格に保管。
   - クラシック PAT の使用が選択されたが、MCP クライアントおよび Hermes ツール定義側の完全 read-only 実装により、権限逸脱を二重に防止。

## 3. Model Resilience & Provider Fallback

1. **フォールバック連鎖**:
   - Primary: `z-ai/glm-5.3-flash` (CommandCode)
   - Fallback #1: `laguna-s-2.1-free` (opencode-free)
   - Fallback #2: `deepseek-v4-flash-free` (opencode-free)
2. **対象エラー分類**:
   - レート制限 (429)、クォータ/課金枯渇 (402)、サーバー過負荷・停止 (500, 502, 503)、タイムアウトをフォールバック対象として正確に分類。
   - コンテキスト長超過 (Context Overflow, 400) はモデル切り替えではなくコンテキスト圧縮 (compression) へ適切に誘導。
3. **Antigravity 直接連携**:
   - Antigravity は CLI バイナリであり、デーモン向けの外部 HTTP API / API キーが非公開のため、現状は直接外部連携不可 (UNSUPPORTED / NOT EXPOSED) として明記。

## 4. 実機検証サマリ (Pi: <PI_HOSTNAME>)

```text
GitHub Read MCP:
  github_repo_status          PASS
  github_list_commits         PASS
  github_get_commit           PASS
  github_get_file             PASS
  github_list_prs             PASS
  github_get_pr               PASS
  github_list_issues          PASS
  github_get_issue            PASS
  Allowlist 検証              PASS
  パーミッション (0400)       PASS

Personal Hermes E2E:
  最新 commit 調査依頼        PASS (e16288a を 39.4s で正確に要約回答)
  コミット作成・書込依頼      PASS (読み取り専用制約を認識し正しく拒絶)

モデルフォールバック:
  fallback list 表示          PASS (Primary + 2 Fallbacks 構成確認)
  エラー分類ユニットテスト    PASS
  連鎖遷移ユニットテスト      PASS

テストスイート全件通過:
  tests-github                25 / 25 passed
  tests-gmail                 31 / 31 passed
  tests-calendar              15 / 15 passed
  tests-attention             152 / 152 passed
  tests-gateway               251 / 251 passed
```
