# Hermes I.1-S Resume

## Objective

Execute Phase I.1-S Production Closeout for Unified Commitments & Agenda Layer (clean synthetic fixtures from production DB, enforce least-privilege filesystem boundary with HERMES_HOME_MODE=0710 and HERMES_GID=980, conduct real Discord and LINE frontend E2E retrieval queries via Antigravity primary, validate Attention Engine commitment noise suppression and new actionable signals, and resolve platform regression suite).

## Completed: COMPLETED (2026-09-06)

- A. Synthetic Production Data Cleanup:
  - 4 synthetic commitments (`cmt_e2e_discord_req_b`, `cmt_e2e_gmail_req_b`, `cmt_ambig_1`, `cmt_ambig_2`) and 4 synthetic evidence rows safely purged.
  - Mock evidence (`gmail:h2_e2e_mock_exam_confirm`) removed from real commitment `cmt_bf343048b742`; `source_count` restored to 1.
  - Transactional cleanup executed; database integrity verified (`PRAGMA integrity_check: ok`, `PRAGMA foreign_key_check: ok`).
  - Production DB count: commitments 737, evidence 737, history 737, events 656.
  - Backfill idempotency re-run verified: 0 added, 0 duplicates, stable 737 rows.
- B. Least-Privilege Filesystem Boundary:
  - Reviewed broad `0755` permission model; reconfigured `docker-compose.yaml` with `HERMES_GID=980` (`hermesreconcile`) and `HERMES_HOME_MODE=0710`.
  - Container-enforced startup permission sets `/srv/hermes/hermes-data` to `0710 <PI_USER>:hermesreconcile`.
  - Subdirectories tightened: `events/` (0750) with `events.db` (0640), `knowledge-index/` (0750) with `discord/index.db` (0640) and `line/index.db` (0640).
  - Sensitive files restricted: `config.yaml` (0600 <PI_USER>:<PI_USER>), `.env` (0600), `auth.json` (0600).
  - Multi-user isolation validated: `hermesantigravity` can traverse and read required indexes, but CANNOT read `config.yaml`, `.env`, `auth.json`, or `/var/run/docker.sock`.
  - Arbitrary local user (`nobody`) denied all access (`0/6 readable`).
  - Coding Worker isolated in separate container without volume mounts.
  - Verified persistence across container restart.
- C. Real Discord / LINE Frontend E2E:
  - Verified 3 real queries on Discord DM without manual `-t` flags (`今日やること何？`, `今、返事待ちって何がある？`, `期限過ぎてるものある？`).
  - Verified 1 real query on LINE webhook listener (`今日やること何？`).
  - Antigravity primary (`gemini-3.8-flash-medium`) utilized with 100% success; zero fallbacks triggered.
  - `agenda_get` / `event_*` MCP tools invoked cleanly to generate accurate Japanese summaries.
- D. Attention Integration / Flood Validation:
  - Verified `att_events.py` lines 42-66 suppressing events linked to `DISMISSED` commitments (360 routine noise events suppressed).
  - Verified historical baseline flood suppression: 0 proactive notifications generated on 737 commitments.
  - Added unit test suite in `tests-attention/test_commitments_attention.py` covering dismissed noise suppression, new SELF actionable signal generation, and overdue transitions (all passed).
- Full Regression Test Suite (510 / 510 PASS, 100%):
  - tests-antigravity: 19 / 19 PASS
  - tests-github: 26 / 26 PASS
  - tests-gmail: 31 / 31 PASS
  - tests-calendar: 15 / 15 PASS
  - tests-attention: 155 / 155 PASS
  - tests-commitments: 13 / 13 PASS
  - tests-gateway: 251 / 251 PASS

## Next phase

- Next: Phase I.2 — Daily Planning / Morning Brief

---

# Hermes I.1 Resume

## Objective

Deliver Unified Commitments & Agenda Layer: ingest dispersed promises, requests, tasks, and follow-ups across LINE, Discord, Gmail, Calendar, and Event Layer into a canonical current-state commitment store with immutable evidence links, state/history separation, explicit responsibility and temporal semantics, agenda projections, and read-only MCP exposure for Personal Hermes.

## Completed: COMPLETED (2026-09-06)

- Production Event Profiling completed (656 raw events, 81 calendar events analyzed; noise and duplication patterns mapped).
- Canonical Commitment Schema & Store implemented in `/srv/hermes/hermes-data/events/events.db`:
  - `commitments`, `commitment_evidence`, `commitment_history` tables with strict integrity and foreign keys.
- Immutable evidence linkage established linking events to commitments.
- Reconcile engine implemented with responsibility (`SELF`, `COUNTERPARTY`, `MUTUAL`, `INFORMATIONAL`), work state (`ACTIONABLE`, `SCHEDULED`, `WAITING`, `COMPLETED`, `DISMISSED`, `ABANDONED`), and temporal state semantics.
- Agenda projection engine (`agenda_get`) providing `today`, `upcoming`, `waiting`, `overdue`, `undated` projections.
- Antigravity read-only MCP retrieval: `event_mcp.py` exposing `agenda_get`, `commitment_get`, `commitment_search`, `commitment_history`.
- Backfill pipeline completed populating initial baseline commitments.

## Next phase

- Next: Phase I.1-S Production Closeout

---

# Hermes H.3.2-S Resume

## Objective

Harden the Antigravity runtime isolation and complete security closeout (dedicated service identity `hermesantigravity`, 25 exact MCP tool allowlist, bridge caller isolation and internal bearer token authentication, `/health` endpoint sanitization, and regression test resolution).

## Completed: COMPLETED (2026-09-06)

- Dedicated Service Account `hermesantigravity` (UID 994, GID 979, shell `/usr/sbin/nologin`, home `/home/hermesantigravity` 0700):
  - Completely isolated from privileged groups (`docker`, `sudo`, `adm`, `root`).
  - Read access to published Gmail index via `hermesreconcile` group.
  - Dedicated Google OAuth token in `/home/hermesantigravity/.gemini/antigravity-cli/antigravity-oauth-token` (0600).
  - Managed under host systemd service `hermes-antigravity.service`.
- Exact 25 MCP Read-Only Tools Allowlist:
  - Wildcard `mcp(*)` eliminated; replaced by exact 25 entries across GitHub (8), Gmail (4), Calendar (3), Discord (3), LINE (3), Event (4).
  - Explicit deny rules for execution (`command(*)`, `file(*)`, `browser(*)`, `terminal(*)`) and mutations (`mcp(event/event_mark_*)`, etc.).
  - Verified allowed tool execution and denial interception.
- Bridge Caller Isolation & Authentication:
  - Bound exclusively to `172.18.0.1:8765` (Docker bridge gateway interface); LAN connections refused.
  - Internal Bearer token authentication enforced on completions and models endpoints.
  - Sanitized `/health` and `/status` endpoints with zero secret, path, or project leakage.
- Regression Test Count Discrepancy Resolved (Item D):
  - Explained 145 (G.2 baseline) vs 152 (H.1/H.2 with calendar and gmail probes).
  - All 152 attention tests running and passing on Pi with test isolation intact.
- Full Regression Test Suite (493 / 493 PASS, 100%):
  - tests-antigravity: 19 / 19 PASS
  - tests-github: 25 / 25 PASS
  - tests-gmail: 31 / 31 PASS
  - tests-calendar: 15 / 15 PASS
  - tests-attention: 152 / 152 PASS
  - tests-gateway: 251 / 251 PASS
- Production E2E Verification (/v1/runs):
  - Primary Gemini 3.8 Flash query with GitHub MCP: PASS (commit `e16288af...` summarized).
  - Mutation rejection: PASS (autonomous read-only invariant refusal).
  - Failover to CommandCode fallback and automatic recovery verified.
- Quota Invariant: Consumer Antigravity quota verified (`authMethod=consumer`, `quotaProject=` blank). Zero paid GCP billing.

## Next phase

- Next: Phase I.1 (or next scheduled capability)

---

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
