# Phase H.3.1 — Antigravity Provider Bridge Technical Report

## 1. Executive Summary

- **Phase**: H.3.1 (Antigravity Provider Bridge)
- **Status**: **HOLD** (Outcome B per Section 41: Model Works, Tool Calling Does Not)
- **Primary Model**: `z-ai/glm-5.3-flash` (via CommandCode / auto) — **RETAINED IN PRODUCTION**
- **Fallback Chain**:
  1. `laguna-s-2.1-free` (via opencode-free)
  2. `deepseek-v4-flash-free` (via opencode-free)
- **Antigravity Inference**: **PASS** (Gemini 3.8 Flash functional on Pi 4 with 1.6–2.5s latency)
- **Antigravity Quota**: **PASS** (Confirmed consumer quota pool, unexpected paid billing: **NO**)
- **Hermes Full Tool Integration**: **HOLD** (Antigravity CLI runtime owns an internal 57-tool agent loop and does not expose a raw OpenAI function-calling wire format; per Section 15, ad-hoc prompt parsers are prohibited)
- **Regression Suite**: **487 / 487 tests PASS** (474 baseline + 13 bridge unit tests)

---

## 2. Capability Probe (Pi 4 ARM64)

### 2.1 Hardware & OS Verification
- **Host**: Raspberry Pi 4 Model B (4GB RAM)
- **Hostname**: `<PI_HOSTNAME>` (`<PI_LAN_IP>`)
- **Kernel**: Linux 6.18.34+rpt-rpi-v8 `aarch64`
- **Distribution**: Debian GNU/Linux 13 (trixie)

### 2.2 Antigravity Binary
- **Source**: Official Google release manifest (`linux_arm64.json`)
- **Version**: `1.1.27`
- **Binary Type**: ELF 64-bit LSB pie executable, ARM aarch64, dynamically linked
- **Path**: `/home/<PI_USER>/antigravity-test-bin/antigravity`
- **Headless Mode**: Fully supported via `--print`, `--output-format json`, `--output-format stream-json`

### 2.3 Models Available
Verified via `antigravity models`:
- `gemini-3.8-flash-high`
- `gemini-3.8-flash-medium` (default evaluation target)
- `gemini-3.8-flash-low`
- `gemini-3.7-flash-medium`
- `gemini-3.6-flash-medium`
- `gemini-3.1-pro-high`

---

## 3. Authentication & Credential Storage

- **Auth Protocol**: OAuth 2.0 with PKCE (Proof Key for Code Exchange)
- **Scopes**:
  - `https://www.googleapis.com/auth/cloud-platform`
  - `https://www.googleapis.com/auth/userinfo.email`
  - `https://www.googleapis.com/auth/userinfo.profile`
  - `https://www.googleapis.com/auth/cclog`
  - `https://www.googleapis.com/auth/experimentsandconfigs`
  - `https://www.googleapis.com/auth/aicode`
  - `openid`
- **Redirect URI**: `https://antigravity.google/oauth-callback`
- **Credential Storage**: `/home/<PI_USER>/.gemini/antigravity-cli/antigravity-oauth-token`
- **File Permissions**: `0600` (`-rw-------`), owner `<PI_USER>`
- **Persistence**: Re-authenticated calls require no manual interaction; automated silent refresh supported by the binary.

---

## 4. Quota Pool & Billing Safety

### 4.1 Quota Pool Identification
Inspected log entries from `quota_manager.go` and `server_oauth.go`:
```text
server_oauth.go:192: applyAuthResult: email=user@example.com, authMethod=consumer, quotaProject=
quota_manager.go:45: doRefreshQuota: starting reload (force=true)
cache/default_project_id.txt: default-cli-project
```
- **Auth Method**: `consumer`
- **Quota Project**: Blank (None)
- **Actual Quota Source**: Google Antigravity consumer entitlement / Google One AI quota credits
- **Unexpected Paid Billing Risk**: **NO** (Without a GCP billing project or API key, commercial billing cannot be incurred)

---

## 5. Architectural Evaluation & Tool Calling Analysis

### 5.1 The Interface Mismatch
Personal Hermes architecture requires an inference provider that accepts standard OpenAI Chat Completions requests with a `tools` parameter and returns structured `tool_calls` in the assistant response:
```text
Hermes Agent -> tool_calls -> MCP Server (GitHub/Gmail/Calendar) -> tool result -> Hermes Agent
```

### 5.2 Antigravity CLI Behavior
1. **Internal Agent Loop**: Antigravity CLI embeds its own agent engine (`jetski`) with 57 built-in tools (`view_file`, `write_to_file`, `run_command`, etc.).
2. **Execution Interception**: When prompted with tasks that require tools, the CLI attempts to invoke its own internal tools within its own sandbox. In headless `--print` mode, tool calls trigger internal permission checks (`permission check failed for read_file... auto-denied`).
3. **Absence of Raw Function-Calling API**: Antigravity CLI does not expose an external function-calling API to emit unexecuted tool calls intended for an external orchestration authority.
4. **Prompt Parser Prohibition**: Section 15 of the specification strictly prohibits constructing an ad-hoc prompt parser to simulate tool calls.

### 5.3 Outcome Decision (Outcome B)
Per Section 16 and Section 41:
- Normal conversational completion: **PASS**
- Hermes tool calling: **FAIL / UNSUPPORTED**
- **Decision**: **STATUS: HOLD**. The system does NOT switch the primary frontend model to Antigravity. `z-ai/glm-5.3-flash` remains the Primary model with full MCP tool calling support.

---

## 6. Provider Bridge Implementation

A production-ready local bridge has been developed in `src/antigravity-bridge/`:
- **Server**: `src/antigravity-bridge/app/bridge_server.py`
- **Config**: `src/antigravity-bridge/app/bridge_config.py`
- **Features**:
  - OpenAI-compatible `/v1/models` and `/v1/chat/completions`
  - `/health` and `/status` monitoring endpoints
  - Robust subprocess management with configurable timeouts
  - Strict secret redaction across all logs
  - Eligible error classification (429 quota, 401 auth, 504 timeout, 503 overload)
  - Tool-call guardrail: Returns HTTP 501 on tool requests to trigger Hermes fallback to GLM-5.3-Flash.

---

## 7. Verification & Metrics

### 7.1 Measured Latency & Tokens
- **Conversational Latency**: 1.65s – 2.50s (Pi 4 native)
- **Memory Footprint**:
  - Bridge Server: ~18 MB RSS
  - Antigravity Subprocess: ~120 MB RSS during active turn
- **Unit & Integration Tests**:
  - `tests-antigravity`: 13 / 13 PASS
- **Full Regression Suite**:
  - `tests-github`: 25 / 25 PASS
  - `tests-gmail`: 31 / 31 PASS
  - `tests-calendar`: 15 / 15 PASS
  - `tests-attention`: 152 / 152 PASS
  - `tests-gateway`: 251 / 251 PASS
  - Total: **487 / 487 PASS**

---

## 8. Rollback & Stability

Because the Primary model was kept on `z-ai/glm-5.3-flash` per the Outcome B protocol, no breaking changes were introduced to the Hermes Production container or gateway. The baseline fallback chain remains 100% operational.
