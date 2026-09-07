# Phase H.3.2 — Constrained Antigravity Agent Backend

> 状態: **COMPLETED (2026-09-06)**。
> 実装・Pi 実機配備・ネイティブ MCP 連携 (GitHub/Calendar/Gmail/Discord/LINE/Events)・
> ハードセキュリティ境界 (Shell/File/Browser 完全拒絶)・SSE ストリーミング対応・
> Personal Hermes E2E 検証・書き込み拒絶ネガティブテスト・モデルフォールバック連鎖実測 (GLM-5.3-Flash)・
> 全回帰テストスイート (484 tests ALL PASS) をすべて実測完了。

---

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
       │     ├─ Primary: gemini-3.8-flash-medium
       │     │     via Antigravity Bridge (http://172.18.0.1:8765/v1)
       │     │     └── Constrained Delegated Read-Only Agent Backend
       │     │           ├── Native MCP Read Servers (GitHub, Calendar, Gmail, Discord, LINE, Events)
       │     │           └── Security Policy (DENY: command, file, browser, terminal)
       │     │
       │     ├─ Fallback #1: z-ai/glm-5.3-flash (via commandcode)
       │     ├─ Fallback #2: laguna-s-2.1-free (via opencode-free, keyless)
       │     └─ Fallback #3: deepseek-v4-flash-free (via opencode-free, keyless)
       │
       └── Frontend MCP Toolset (platform_toolsets.api_server)
             ├─ mcp__jobs_readonly (jobs status/list/reviews)
             ├─ mcp__calendar (read-only calendar events)
             ├─ mcp__gmail (read-only gmail messages/threads)
             └─ mcp__github (read-only GitHub repository intelligence)
```

---

## 2. Key Technical Breakthroughs & Implementation

### A. Constrained Delegated Read-Only Agent Backend
- Phase H.3.1 では Antigravity CLI が独自の agent/tool loop を持ち、外部へ生の OpenAI 互換 tool_calls を返さないため HOLD となっていた。
- Phase H.3.2 では、Antigravity CLI 自身を「**読み取り専用に厳格制約された移譲型エージェント (Constrained Delegated Agent)**」として位置付け。
- Antigravity CLI に 6 つのネイティブ MCP サーバー（GitHub, Calendar, Gmail, Discord Knowledge, LINE Knowledge, Event Layer）を直接マウントし、Hermes からツール要求が渡された際に Antigravity 内部で読み取りツールを自律実行させて回答を合成する構成を実現。

### B. ハードセキュリティ境界 (Hard Security Invariants)
- `/home/<PI_USER>/.gemini/antigravity-cli/settings.json` において権限を厳格定義:
  - `permissions.allow`: `["mcp(*)"]`
  - `permissions.deny`: `["command(*)", "file(*)", "browser(*)", "terminal(*)"]`
- シェル実行 (`run_command`)、ファイル変更 (`write_to_file`, `edit_file`)、ブラウザ自動操作は Antigravity のコアポリシーレベルで即座に拒絶 (`Permission denied ... Matches user-configured deny rule`)。
- 実機ネガティブテストにおいて、GitHub コミット作成要求に対し Personal Hermes が「現在の api_server セッションは読み取り専用制約下にある」ことを正確に認識し、書き込みを完全に拒絶することを確認。

### C. Server-Sent Events (SSE) ストリーミングの実装
- Hermes Agent ループが送信する `stream: True` に対し、Bridge が従来の単一 JSON ではなく標準 OpenAI 準拠の `text/event-stream` (SSE) チャンクおよび `data: [DONE]\n\n` を返すよう `bridge_server.py` を拡張。
- これにより `EmptyStreamError` やリトライループを完全に解消し、スムーズな単一ターン完了を実現。

---

## 3. Provider / Model Resilience Chain

ユーザー要求に基づき、以下の耐障害性フォールバック連鎖を本番確立:

| 優先度 | モデル名 | プロバイダ | 役割 / 接続先 |
| :--- | :--- | :--- | :--- |
| **Primary** | `gemini-3.8-flash-medium` | `custom` | Antigravity Provider Bridge (`http://172.18.0.1:8765/v1`) |
| **Fallback #1** | `z-ai/glm-5.3-flash` | `commandcode` | CommandCode API (`https://api.commandcode.ai/provider/v1`) |
| **Fallback #2** | `laguna-s-2.1-free` | `opencode-free` | OpenCode Zen (`https://opencode.ai/zen/v1/`) |
| **Fallback #3** | `deepseek-v4-flash-free` | `opencode-free` | OpenCode Zen (`https://opencode.ai/zen/v1/`) |

- **フェイルオーバー実機実証**:
  Bridge プロセス停止時、Hermes は即座に障害を検出し、Fallback #1 (`z-ai/glm-5.3-flash` via `commandcode`) へ自動遷移して GitHub MCP 調査を完遂 (`e16288afc06056509f5c2d82b00a516090a09339` を回答)。
- **プライマリ自動復旧実機実証**:
  Bridge プロセス復帰後、Hermes は Primary (`gemini-3.8-flash-medium`) へ自動復帰し、正常に応答。

---

## 4. 実機検証サマリ (Pi: <PI_HOSTNAME>)

```text
Personal Hermes E2E API (/v1/runs):
  1. Primary Model 調査 (Gemini 3.8 Flash via Antigravity)
     - hermes-cos-infra 最新コミット調査: PASS
     - ハッシュ e16288afc06056509f5c2d82b00a516090a09339 を正確に取得・回答
  2. 書き込み拒絶ネガティブテスト (Security Gate)
     - コミット作成要求: PASS
     - 読み取り専用制約を自己認識し、明示的拒絶回答を出力
  3. モデルフォールバック実機遷移テスト
     - Bridge 停止時: PASS
     - Fallback #1 (z-ai/glm-5.3-flash via commandcode) へ自動遷移し成功
  4. Primary 復旧テスト
     - Bridge 起動時: PASS
     - Primary (Gemini 3.8 Flash) へ自動復帰し正常応答

回帰テストスイート全件通過 (Pi 実機):
  - tests-antigravity :  16 /  16 passed
  - tests-github      :  26 /  26 passed (model_resilience 含む)
  - tests-gmail       :  31 /  31 passed
  - tests-calendar    :  15 /  15 passed
  - tests-attention   : 145 / 145 passed
  - tests-gateway     : 251 / 251 passed
  合計                : 484 / 484 passed (ALL PASS)
```
