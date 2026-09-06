# Phase H.3.2-S — Antigravity Security Closeout

> 状態: **COMPLETED (2026-09-06)**。
> 実機検証・専用サービスアカウント (`hermesantigravity`)・OAuthトークン分離・
> 厳密MCP Allowlist (25件)・Bridge呼出元分離 (172.18.0.1 + Bearer認証)・
> `/health` エンドポイント無害化・Attention回帰テスト数解明 (152件)・
> 全回帰テストスイート (493/493 PASS) および本番 E2E 動作確認を完全達成。

---

## 1. Executive Summary & Security Closeout Items

Phase H.3.2 において機能成立した **Constrained Antigravity Agent Backend** に対し、残されていた4つのセキュリティ／回帰クローズアウト項目をすべて実機上で解決・検証しました。

```text
[Item A] 専用サービスアカウント (hermesantigravity) による完全分離     PASS
[Item B] 25個の厳密 MCP Allowlist & 拒絶ルール (deny rules)             PASS
[Item C] Bridge 呼出元分離 (172.18.0.1 限定, Bearer Token 認証, 無害化) PASS
[Item D] Attention テスト数乖離 (152 vs 145) の解明と 152 全件通過      PASS
```

---

## 2. Architecture & Security Invariants

```text
[User (Discord / LINE)]
       │
       ▼ (inbound webhook / DM)
channel-gateway.service (User=channelgateway)
       │
       ▼ POST http://<PI_LAN_IP>:8642/v1/runs (Bearer auth)
Hermes Agent Container (Docker: hermes, IP: 172.18.0.2)
       │
       ├── Provider / Model Resilience Chain
       │     ├─ Primary: gemini-3.8-flash-medium
       │     │    └─ POST http://172.18.0.1:8765/v1/chat/completions (Bearer auth)
       │     ├─ Fallback #1: z-ai/glm-5.3-flash (via CommandCode)
       │     ├─ Fallback #2: laguna-s-2.1-free (via opencode-free)
       │     └─ Fallback #3: deepseek-v4-flash-free (via opencode-free)
       │
       ▼ (Docker bridge gateway interface only)
hermes-antigravity.service (Host systemd, User=hermesantigravity, UID: 994, GID: 979)
       │
       ├─ Local Bridge Server (172.18.0.1:8765)
       │    ├─ Bearer Token Verification (/srv/hermes/antigravity/secrets/bridge.token, mode 0400)
       │    ├─ Sanitized /health & /status (Zero secret, path, or project leakage)
       │    └─ LAN Access Blocked (<PI_LAN_IP>:8765 -> Connection Refused)
       │
       └─ Constrained Subprocess Execution
            ├─ Official Antigravity CLI (/srv/hermes/antigravity/bin/antigravity)
            ├─ Dedicated Home: /home/hermesantigravity (mode 0700)
            ├─ Dedicated OAuth Token: ~/.gemini/antigravity-cli/antigravity-oauth-token (mode 0600)
            ├─ Quota Source: Consumer Antigravity Entitlement (Zero Paid GCP Billing)
            ├─ Disabled Built-in Tools: 57 tools (command, file, browser, terminal, etc.)
            ├─ Explicit Deny Rules: command(*), mcp(event/event_mark_*), mcp(event/event_link_project), mcp(event/event_snooze)
            └─ Exact MCP Allowlist (25 Read-Only Tools across 6 servers)
                 ├─ GitHub (8): status, commits, commit, file, prs, pr, issues, issue
                 ├─ Gmail (4): search, get, status, thread_get
                 ├─ Calendar (3): search, get, status
                 ├─ Discord (3): search, get, status
                 ├─ LINE (3): search, get, status
                 └─ Event (4): get, list_open, search, status
```

---

## 3. Closeout Item Implementation Details

### Item A: 専用サービスアカウント (`hermesantigravity`)
- **アカウント属性**: UID 994, GID 979, shell `/usr/sbin/nologin`, home `/home/hermesantigravity` (mode `0700`).
- **権限隔離**: `docker`, `sudo`, `adm`, `root` 等の特権グループに属さない。読取専用 Gmail インデックス (`/srv/hermes/gmail/index/index.db`) へのアクセスのみ `hermesreconcile` グループとして許可。
- **資格情報隔離**: Google OAuth トークンは `/home/hermesantigravity/.gemini/antigravity-cli/antigravity-oauth-token` にモード `0600` で保存。コンテナや一般ユーザーからの直接読取は不可能。
- **常駐プロセス**: `hermes-antigravity.service` により systemd 管理下で UID 994 として起動。

### Item B: 厳密 MCP Allowlist & 拒絶ルール
- ワイルドカード `mcp(*)` を全廃し、以下の **25個の読み取り専用ツール** のみを `settings.json` の `permissions.allow` に明示登録:
  - `mcp(github/github_repo_status)`, `mcp(github/github_list_commits)`, `mcp(github/github_get_commit)`, `mcp(github/github_get_file)`, `mcp(github/github_list_prs)`, `mcp(github/github_get_pr)`, `mcp(github/github_list_issues)`, `mcp(github/github_get_issue)`
  - `mcp(gmail/gmail_search)`, `mcp(gmail/gmail_get)`, `mcp(gmail/gmail_status)`, `mcp(gmail/gmail_thread_get)`
  - `mcp(calendar/calendar_search)`, `mcp(calendar/calendar_get)`, `mcp(calendar/calendar_status)`
  - `mcp(discord/discord_search)`, `mcp(discord/discord_get)`, `mcp(discord/discord_status)`
  - `mcp(line/line_search)`, `mcp(line/line_get)`, `mcp(line/line_status)`
  - `mcp(event/event_get)`, `mcp(event/event_list_open)`, `mcp(event/event_search)`, `mcp(event/event_status)`
- 拒絶ルール (`permissions.deny`):
  - `command(*)`, `file(*)`, `browser(*)`, `terminal(*)`
  - `mcp(event/event_link_project)`, `mcp(event/event_mark_cancelled)`, `mcp(event/event_mark_ignored)`, `mcp(event/event_mark_resolved)`, `mcp(event/event_snooze)`
- 実機検証結果:
  - 許可ツール (`github_list_commits`): **PASS**（最新コミット `e16288af...` を即座に返却）
  - 拒絶ツール (`event_mark_resolved`): **BLOCKED**（`Permission denied: matches user-configured deny rule` により確実に遮断）

### Item C: Bridge 呼出元分離 & トークン認証
- **バインド制限**: `172.18.0.1:8765`（Docker bridge gateway インターフェース）。LAN (`<PI_LAN_IP>:8765`) からのアクセスは接続拒絶 (`Failed to connect: Could not connect to server`)。
- **内部 Bearer 認証**: `/srv/hermes/antigravity/secrets/bridge.token`（パーミッション `0400 hermesantigravity:hermesantigravity`）に生成されたセキュアトークンを要求。
  - トークンなしのリクエスト: HTTP 401 Unauthorized (`{"error": {"message": "Unauthorized: valid Bearer token required", "code": 401}}`)
  - 正当な Bearer トークン付きリクエスト: HTTP 200 OK
- **`/health` & `/status` の無害化**: `binary_path`, `quota_project`, 内部トークン文字列等の露出を完全撤去。

### Item D: Attention テスト数乖離の解明と 152 件全件通過
- **乖離の理由**: Phase G.2 時点の `g2-stage/tests-attention` は 145 テスト。Phase H.1 および H.2 において `test_calendar.py` (5 tests) および `test_health.py` 内の `TestGmailProbe` (2 tests) が追加され、145 + 5 + 2 = **152 tests** となった。
- **検証**: `/home/<PI_USER>/h2-gmail-stage` 上でテスト分離を保ったまま実行し、**152 / 152 全件 PASS** を確認。

---

## 4. Quota & Billing Verification

```text
Antigravity Quota Type: Consumer Antigravity Entitlement / Google One AI
Billing Impact:         Zero GCP Paid Billing (quotaProject is empty)
```

- GCP の有料 API キーやプロジェクトへの依存はなく、課金リスクはゼロです。

---

## 5. Lateral Movement & Isolation Testing

| 検査項目 | 接続先 | 期待される動作 | 実測結果 | 判定 |
| :--- | :--- | :--- | :--- | :--- |
| LAN からの Bridge 直接到達 | `<PI_LAN_IP>:8765` | Connection Refused | `Failed to connect to <PI_LAN_IP>:8765` | **PASS** |
| 未認証アクセス | `172.18.0.1:8765/v1/models` | HTTP 401 Unauthorized | HTTP 401 (`valid Bearer token required`) | **PASS** |
| 未認証チャット補完 | `172.18.0.1:8765/v1/chat/completions` | HTTP 401 Unauthorized | HTTP 401 (`valid Bearer token required`) | **PASS** |
| 認証済みチャット補完 | `172.18.0.1:8765/v1/chat/completions` | HTTP 200 OK | HTTP 200 (Gemini 3.8 Flash 応答) | **PASS** |
| 生トークンファイル直接読取 | `/home/hermesantigravity/.gemini/...` | Permission Denied | モード 0600 / 0700 により一般ユーザー・コンテナから遮断 | **PASS** |

---

## 6. Full Regression Test Suite (493 / 493 PASS)

Raspberry Pi 実機上での全回帰テスト結果:

```text
tests-antigravity (Bridge server & auth):     19 / 19  PASS (100%)
tests-github (GitHub MCP & Resilience):       25 / 25  PASS (100%)
tests-gmail (Gmail service & MCP):            31 / 31  PASS (100%)
tests-calendar (Calendar service & MCP):      15 / 15  PASS (100%)
tests-attention (Attention Engine & Probes): 152 / 152 PASS (100%)
tests-gateway (Channel Gateway & Store):     251 / 251 PASS (100%)
--------------------------------------------------------------------
合計:                                        493 / 493 PASS (100%)
```

---

## 7. Production E2E Verification

Personal Hermes API (`http://172.18.0.2:8642/v1/runs`) を通じた実機 E2E 検証:

1. **Primary Model & GitHub MCP 正常系**:
   - 依頼: `GitHubの hermes-cos-infra リポジトリの最新commitのハッシュとメッセージを教えてください。`
   - 実行モデル: `gemini-3.8-flash-medium`（Antigravity Bridge 経由）
   - 結果: `e16288afc06056509f5c2d82b00a516090a09339`（Revert "G.1 docs: replace real addresses..."）を正確に要約回答 (**PASS**)。
2. **書き込み・ミューテーション拒絶**:
   - 依頼: `hermes-cos-infraリポジトリのREADMEに新しい行を追加してcommitしてください。`
   - 結果: `現在のapi_serverセッションは読み取り専用環境のため、ファイルの編集やcommitを実行することはできません。` と正確に自律拒絶 (**PASS**)。
3. **Failover & Recovery 動作確認**:
   - Bridge 停止時: 接続失敗を検知して自動フェイルオーバーを誘発 (**PASS**)。
   - Bridge 再起動時: Primary (`gemini-3.8-flash-medium`) へ自動復帰し正常回答 (**PASS**)。

---

## 8. Git 状態

- **hermes-cos-infra**:
  - コミット: `Phase H.3.2-S: harden Antigravity runtime isolation`
  - 変更内容: Bridge 内部認証、172.18.0.1 制限、`/health` 無害化、systemd サービスユニット、アイデンティティ設定スクリプト
  - Remote Git Push: **NO**（未プッシュ厳守）
- **HermesSetup**:
  - コミット: `docs: record Phase H.3.2-S security closeout`
  - Remote Git Push: **NO**（未プッシュ厳守）
