# Native premium CLI route

Status: server migration activated; an authorized ordinary reboot completed on
2026-09-09 late evening JST, with recovery checks extending into 2026-09-10.
Post-reboot production Astra and normal Gemini inference both passed without
the PC worker/tunnel. DB integrity and ingestion checks passed. The Docker-network
boot-order fix is applied and verified. Both exact native models and the exact
unavailable-endpoint fallback probe passed after reboot. No manual channel
notification was sent.

## User intent and exact models

The user clarified that Codex Astra and Claude Fable mean the native Codex CLI
and Claude Code. They are not models provided by the existing Antigravity CLI.
The requested route is `claude-fable-5` (high) -> `gpt-6-astra` (medium) -> existing Antigravity
Gemini -> existing Hermes fallback providers. No Opus/Sonnet substitution.

Requirements and complex reasoning use the premium route; routine requests keep
the existing path. Missing live/private context returns to the current read-only
source backend. Failures, timeout, busy worker or unavailable computer fall back.

## Implementation

- `src/antigravity-bridge/app/premium_routing.py`: latest-user-message selection,
  bounded authenticated loopback request, response identity checks and fallback.
- `bridge_config.py` / `bridge_server.py`: opt-in private configuration and routed
  execution. Streaming reports the actual model used. Existing MCP guard stays.
- `src/premium-cli/app/cli_reasoner.py`: exact native models, isolated working
  directory, disabled customizations/tools, structured ephemeral responses and
  bounded Claude-to-Codex fallback.
- `premium_server.py`: authenticated loopback-only server with one active request.
- `supervisor.py`: local worker and SSH reverse-tunnel supervision.

Native CLI credentials now reside in the server's private service state. The
dedicated endpoint token and credentials are never stored in this repository.
See `src/premium-cli/README.md` for behavior and official documentation links.

## Measurements

- Codex CLI 0.153.4: ChatGPT login status confirmed; `gpt-6-astra` returned the
  expected synthetic response. A second older executable was present; the adapter
  resolves the installed PowerShell/npm launcher before the older native binary.
- Claude Code 2.1.266: `claude-fable-5` returned the expected synthetic response
  with safe mode, no tools and no session persistence.
- Real Pi -> authenticated reverse tunnel -> computer -> Astra request succeeded.
  Only a synthetic connectivity prompt was used; no private source body was used.
- 38 bridge/routing tests passed, including routine-query exclusion, correct
  fallback ordering, missing-context handling, authentication and response parsing.
- `git diff --check` passed.

## Server runtime and production activation

Both CLIs now execute on the always-on ARM64 Linux server. Node 22.23.2, Codex
0.153.4 and Claude Code 2.1.266 were installed in a dedicated system-volume
directory. `hermes-premium-cli.service` runs as a dedicated unprivileged account,
is enabled at boot and listens only on `127.0.0.1:18767`. Its unit is recorded in
`src/premium-cli/hermes-premium-cli.service`. CLI files are root-owned; credentials
are service-account-owned mode 0600 inside mode 0700 state directories.

The former PC supervisor, worker and SSH reverse tunnel have been stopped. Its
Startup shortcut was moved out of Startup into the private local runtime as
`Hermes Premium CLI.disabled.lnk`. Retained local files are an inactive rollback
copy, not an operational dependency. The original CLI logins were retained.

The reviewed Pi activation payload contains exactly three application files:
`bridge_config.py`, `bridge_server.py`, `premium_routing.py`. It adds the private
`premium_cli` configuration and token and restarts only the Antigravity bridge.
Its activation script and payload are in the temporary stage
`hermes-owner-filter.Ysn0G4` on the Pi. Activation completed successfully.

Production evidence:
- The existing HTTP bridge returned `gpt-6-astra` and the exact probe marker.
- Running the deployed router as its service account with a deliberately closed
  loopback endpoint returned HTTP 200 and `gemini-3.8-flash-medium`, with the exact
  fallback marker. This per-call override did not modify live configuration.
- A routine request bypassed premium selection and returned HTTP 200 from the
  same Gemini model with the exact routine marker.
- Antigravity and attention services are active/running, with NRestarts=0 and
  ExecMainStatus=0. All three scheduled Hermes timers remain scheduled; the
  Hermes container is running.
- Private rollback backup: `backups/premium-routing-20260909T135851Z`, relative
  to the runtime root, root-owned mode 0700. The dedicated token is mode 0640
  and restricted to root and the bridge service group.
- After server migration, both exact native models returned the expected marker
  on the server. The hardened worker and the existing production HTTP endpoint
  returned Astra. With the PC worker/tunnel stopped and the old forwarded port
  confirmed absent, the production endpoint again returned Astra successfully.
- Server migration rollback configuration:
  `backups/server-premium-20260909T141407Z`, relative to the runtime root.
- The first migration attempt rolled back because the bridge was not listening
  two seconds after restart. Readiness polling replaced that assumption; the
  subsequent activation and verification passed.
- Installation exposed high I/O wait on the existing USB-backed runtime volume.
  CLI binaries and package cache were moved to the separate system volume before
  installation completed. This migration does not establish that the underlying
  storage latency is fixed; the existing bridge and notification data still use
  their existing volume.
- Actual reboot recovery was subsequently observed; see the recovery section.
  The next morning brief's delivered content remains unobserved.

## Storage incident and recovery

The pre-reboot unavailable-premium probe selected fallback correctly but Antigravity
timed out after approximately 120 seconds (HTTP 504). The routine-query part of
that probe was not run because the fallback assertion failed. Kernel diagnostics
showed blocked I/O allocation (`blk_mq_get_tag`), an ext4 journal waiting for
updates and a worker blocked for over 241 seconds. Gmail ingestion and Calendar
also had tasks in uninterruptible I/O wait. Active systemd states are not proof
that those operations are healthy. No hardware fault or specific kernel defect
has been established. No disk reset, sysfs tuning or forced reboot was attempted.

Current and pre-migration bridge configurations plus the new unit were copied
and fsynced into root-only `/var/lib/hermes-premium-recovery` on the separate
system volume. This is a configuration recovery checkpoint, not a current DB
backup. Original private runtime backups remain available. The abandoned partial
CLI install on the USB volume is retained until storage recovery; do not start it.

An ordinary `systemctl reboot` was explicitly approved and executed. The boot ID
changed, SSH returned, and systemd reached running with no failed units.
Post-reboot measurements:
- Both event and attention DBs passed read-only integrity and foreign-key checks.
  There were 747 events and all 823 evidence rows were retained.
- Gmail and Calendar incremental synchronization completed. Scheduled Gmail
  event ingestion completed successfully; all three timers remain scheduled.
- From the existing Hermes container and its configured provider credentials,
  a synthetic requirements request returned Astra and a routine request returned
  Gemini, with the exact markers. Observed durations were 12.50 and 23.34 seconds;
  these are individual probes, not performance guarantees.
- No uninterruptible I/O task was observed in the checked post-boot snapshot;
  sampled I/O wait was 1-2 percent after initial startup activity. The original
  stall's root cause and long-term recurrence risk remain unresolved.
- Premium worker and attention engine started automatically. The bridge retried
  six times with `Cannot assign requested address` before the Docker network
  became ready, then stayed running. A drop-in declaring `Wants=docker.service`
  and `After=docker.service` is recorded in
  `src/antigravity-bridge/hermes-antigravity-docker-order.conf`.

Final verification on 2026-09-10:
- The drop-in was installed, systemd configuration reloaded, unit verification
  passed, and the effective `After` dependency includes `docker.service`.
  The running bridge did not require a restart to install next-boot ordering.
- Under the native service account, Astra and Fable each returned the exact
  synthetic marker successfully.
- Under the bridge service account, the deployed router with a deliberately
  unavailable premium endpoint returned HTTP 200, the existing Gemini model,
  and the exact marker. The routine-query check also passed. Live endpoint
  configuration was not changed for this failure simulation.
- Premium worker, bridge and attention engine are active/running; all three
  timers are active. The bridge's restart counter remains at the six retries
  from initial boot; premium and attention counters are zero. No new error-level
  kernel message or uninterruptible I/O task was present in the final snapshot.
- The old PC forwarding port is absent; the server-local worker port is listening.

The subsequent Fable-first preference is deployed and verified on 2026-09-10 JST:
`claude-fable-5` with high effort is first, followed by `gpt-6-astra` with medium
effort. The existing production endpoint returned Fable with the exact marker.
A simulated first-backend failure exercised real Astra inference successfully;
the native CLI command arguments confirmed both effort settings. The adapter's
deployed SHA-256 matches the tested payload, and all three related services are
active. Private adapter backup:
`/var/lib/hermes-premium-recovery/fable-first-20260909T165717Z`.
No further activation is required. Actual boot with the new ordering,
long-term storage stability and the next morning brief's delivered content remain
observation limits. Do not reboot again solely to claim acceptance of the ordering
change without a maintenance decision.
