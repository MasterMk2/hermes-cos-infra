# Hermes H.3.2 Resume

## Objective

Establish Gemini 3.8 Flash via Google Antigravity consumer quota as the Primary model for Personal Hermes frontend by deploying it as a constrained delegated read-only agent backend with native MCP tools, hard security boundaries, SSE streaming, and a resilient fallback chain.

## Completed: COMPLETED (2026-09-06)

- Constrained Delegated Read-Only Agent Backend:
  - Antigravity CLI v1.1.27 operational as a delegated read-only agent backend.
  - Native MCP tools mounted: GitHub, Calendar, Gmail, Discord knowledge, LINE knowledge, Event Layer.
- Hard Security Boundaries Enforced:
  - `permissions.deny: ["command(*)", "file(*)", "browser(*)", "terminal(*)"]` in Antigravity settings.json.
  - Shell execution (`run_command`), file mutation, and browser operations strictly blocked at core policy layer.
- Server-Sent Events (SSE) Streaming:
  - Bridge server enhanced with OpenAI-compatible SSE streaming (`stream: True`), eliminating stream drops.
- Personal Hermes E2E Verification (/v1/runs):
  - Primary Model query: Correctly retrieved latest commit (`e16288afc06056509f5c2d82b00a516090a09339`) from `MasterMk2/hermes-cos-infra`.
  - Negative Security Test: Explicitly refused commit creation requests, enforcing read-only boundary.
  - Failover Test: When Bridge was stopped, Hermes automatically transitioned to Fallback #1 (`z-ai/glm-5.3-flash` via CommandCode), executed GitHub MCP, and returned answer.
  - Recovery Test: When Bridge was restarted, Hermes automatically restored Primary (`gemini-3.8-flash-medium`) and answered greetings normally.
- Resilience Fallback Chain Active in Production:
  - Primary: `gemini-3.8-flash-medium` via Antigravity Bridge (`http://172.18.0.1:8765/v1`)
  - Fallback #1: `z-ai/glm-5.3-flash` via `commandcode`
  - Fallback #2: `laguna-s-2.1-free` via `opencode-free`
  - Fallback #3: `deepseek-v4-flash-free` via `opencode-free`
- Regression Test Suites (484 tests ALL PASS on Pi):
  - tests-antigravity: 16 / 16 passed
  - tests-github + model_resilience: 26 / 26 passed
  - tests-gmail: 31 / 31 passed
  - tests-calendar: 15 / 15 passed
  - tests-attention: 145 / 145 passed
  - tests-gateway: 251 / 251 passed

## Next phase

- Next: Phase I.1 (or next scheduled capability)

---

# Hermes H.3.1 Resume

## Objective

Validate Google Antigravity entitlement/quota for Gemini 3.8 Flash on Raspberry Pi 4 (aarch64) as a potential Primary model for Personal Hermes, implement local provider bridge if viable, and verify tool calling and model resilience.

## Completed: HOLD (Outcome B per Section 41) (2026-09-06)

- Official Antigravity CLI binary `1.1.27` verified and functional on Raspberry Pi 4 (Debian 13 aarch64 native ELF).
- Google OAuth 2.0 PKCE authentication successfully completed; credentials persisted in `~/.gemini/antigravity-cli/antigravity-oauth-token` (mode 0600).
- Quota source confirmed: Consumer Antigravity entitlement / Google One AI credits (`authMethod=consumer`, `quotaProject=` blank).
- Unexpected paid billing risk: NONE (no GCP billing project attached).
- Conversational inference PASSED: Gemini 3.8 Flash responsive via `--print` and `--output-format json` (latency 1.65s–2.50s).
- Tool calling compatibility: Antigravity CLI owns its own 57 internal agent tools and does not expose a raw OpenAI function-calling wire format. Ad-hoc prompt parsing is strictly prohibited per Section 15.
- Decision: Per Section 16 & 41 (Outcome B), primary model replacement is placed on HOLD. Existing Production Primary (`z-ai/glm-5.3-flash` via CommandCode) and fallback chain (`laguna-s-2.1-free` -> `deepseek-v4-flash-free`) remain fully ACTIVE and INTACT.
- Provider bridge developed and verified in `src/antigravity-bridge/` with unit test suite in `tests-antigravity/` (13/13 PASS).
- All regression test suites passing on Pi (487 tests total: 474 baseline + 13 bridge tests).

## Next phase

- Next: Review findings before proceeding to Phase I.1

---

# Hermes H.3 Resume

## Objective

Deliver frontend research capability (strictly read-only GitHub MCP access for Personal Hermes) and provider/model resilience with automated fallback chains without weakening existing security boundaries.

## Completed: COMPLETED (2026-09-06)

- Personal Hermes frontend GitHub read capability operational via stdio JSON-RPC MCP server (`/opt/data/tools/github/github_mcp.py`).
- Read-only methods exposed: `github_repo_status`, `github_list_commits`, `github_get_commit`, `github_get_file`, `github_list_prs`, `github_get_pr`, `github_list_issues`, `github_get_issue`.
- All mutation endpoints strictly absent (no write logic, no branch/commit/push/PR modification).
- Allowlist validation enforced (`MasterMk2/hermes-cos-infra`, `MasterMk2/hermes-pi-setup`).
- Negative write test PASSED: Personal Hermes rejected write/commit requests on `hermes-cos-infra` and recognized read-only execution constraints.
- Positive E2E query PASSED: Personal Hermes queried latest commit (`e16288a`) and summarized details accurately.
- Provider fallback chain operational:
  - Primary: `z-ai/glm-5.3-flash` (via auto/CommandCode)
  - Fallback #1: `laguna-s-2.1-free` (via opencode-free)
  - Fallback #2: `deepseek-v4-flash-free` (via opencode-free)
- Error classification verified: Rate limits (429), quota (402), server errors (500, 502, 503), timeouts failover properly; context overflow (400) triggers compression instead.
- Antigravity direct integration documented as unsupported / not exposed as external HTTP API.
- All test suites passing on Pi (474 tests total):
  - tests-github: 25 / 25
  - tests-gmail: 31 / 31
  - tests-calendar: 15 / 15
  - tests-attention: 152 / 152
  - tests-gateway: 251 / 251

## Next phase

- Next: Phase I.1 (or next scheduled capability)
