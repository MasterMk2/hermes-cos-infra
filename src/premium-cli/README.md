# Premium CLI reasoning route

For requirements and complex reasoning, the Antigravity bridge can first ask
Claude Code (`claude-fable-5`, high), then Codex (`gpt-6-astra`, medium), then retain its existing
Antigravity/Gemini route and the Hermes provider fallbacks. Routine requests do
not enter the CLI route. Only the latest user message selects the route; old
conversation content and tool results cannot upgrade an unrelated routine query.

The worker uses the native authenticated CLIs, not an Antigravity model alias.
The noninteractive interfaces are documented in the official
[Codex documentation](https://developers.openai.com/codex/noninteractive) and
[Claude Code documentation](https://code.claude.com/docs/en/headless).

## Boundaries

- The worker accepts only authenticated requests on `127.0.0.1`. A dedicated
  random token must be supplied via a private file and never committed.
- For a worker on another computer, an SSH reverse tunnel exposes the endpoint
  only on the Pi's loopback address. No public HTTP listener or firewall change
  is required. Native subscription credentials stay with their original CLI.
- Each request runs in a temporary empty working directory. Codex uses a read-only
  sandbox, ignores user config, disables shell/apps/browser/MCP customizations and
  uses an ephemeral session. Claude Code uses safe mode, no tools, strict empty
  MCP config, and no session persistence. Both use the exact requested models.
- Only the supplied conversation is used. Missing private/live facts produce
  `needs_context` and return control to the existing read-only MCP backend.
- Missing CLIs, execution errors, timeouts, invalid responses, an unavailable PC
  and a busy worker fall back. The worker accepts one active request at a time.
- A CLI refusal or a source-context request is not retried against the second
  premium model. Ordinary source/tool processing remains the existing backend.
- No prompt or CLI stderr is logged by these adapters. Output usage, when the
  CLI reports it, is forwarded; missing usage is not invented.

## Configuration

The existing bridge defaults to disabled. After the endpoint has passed its
authenticated health and synthetic reasoning probes, its private runtime YAML
can add:

```yaml
premium_cli:
  enabled: true
  endpoint: http://127.0.0.1:18766
  timeout_seconds: 60
  token_file: <PRIVATE_TOKEN_FILE>
```

The premium attempt is bounded within the existing bridge request timeout,
leaving at least 15 seconds for the original backend. The worker divides the
remaining premium budget between Claude Code first and Codex second. These are explicit
implementation settings, not claims about provider response-time guarantees.

Run `premium_server.py --token-file <PRIVATE_TOKEN_FILE>` directly on a CLI host,
or use `supervisor.py --config <PRIVATE_RUNTIME_CONFIG>` to keep both the endpoint
and reverse tunnel running. The private config contains `port` and `ssh_target`.
Keep the worker's startup lifecycle explicit: an installed CLI alone is not a
running endpoint, and a stopped worker means the Pi uses its normal fallback.

## Verified in this task

The production worker now runs on the always-on ARM64 Linux server using
`hermes-premium-cli.service`, listening on loopback port 18767. The bridge points
to that server-local endpoint. Both native models were verified there, and the
production Astra probe passed with the former PC worker and tunnel stopped.
The PC Startup shortcut is disabled. Actual server reboot recovery was observed:
the worker started automatically, production Astra inference passed, both native
models passed, and unavailable-endpoint fallback returned Gemini. The bridge's
Docker startup ordering was subsequently corrected and its unit verified; a
further reboot with that final ordering has not been performed.

- Both exact models returned the expected synthetic response through their native
  CLIs; no private source content was used for connectivity testing.
- The Pi reached this computer through the authenticated reverse tunnel and
  received the expected response from Codex Astra.
- Bridge, route selection, HTTP authentication, model identity, CLI fallback,
  missing-context handling and command isolation tests pass.
- Production bridge activation passed: the existing HTTP endpoint returned Astra.
  Deployed-router checks returned the normal Gemini backend for both an
  unavailable premium endpoint and a routine request. See the production record
  in `docs/22-premium-cli-routing.md` for scope and remaining observation gates.
