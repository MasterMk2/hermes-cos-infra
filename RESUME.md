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
