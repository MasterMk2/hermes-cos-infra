# Phase G.2 — Proactive Chief of Staff / Attention Engine

G.1 gave Hermes a mouth it could answer with. G.2 gives it the judgement to
speak first — and, far more often, the judgement not to.

---

## 1. What it is

```text
events.db ──┐
jobs.db ────┤   attention-engine.service
health ─────┘        │
                     │  collect → decide → deliver → digest
                     │
                     ▼  unix socket (owner-fixed, no recipient field)
              channel-gateway.service          ← the only holder of tokens
                     ├─ Discord DM   (default, unmetered)
                     └─ LINE push    (escalation, metered, budgeted)
```

A scan every five minutes. On a scan that finds nothing: three read-only
SQLite reads, one row written, no network call, no model. That is the normal
case and it is what makes running 288 times a day acceptable on a Pi whose
disk is a USB stick.

## 2. The decisions that shaped it

### 2.1 A read-only reader was impossible, and that was measured

The first design gave the engine its own unprivileged account and a read-only
bind mount onto the Event Layer's directory. It cannot work:

```text
WAL database, sidecars absent, directory read-only
    -> sqlite3.OperationalError: attempt to write a readonly database
WAL database, directory writable, db file mode 0444
    -> reads fine; SQLite creates the -shm index next to it
```

SQLite must create the shared-memory index before it can read a WAL database,
and that is a write into the directory. So the isolation had to move
somewhere else:

* the engine runs as the account that owns the data, and its unit blocks the
  credentials with `InaccessiblePaths=`;
* every connection to somebody else's database sets `PRAGMA query_only = ON`,
  which was measured on the live events database to refuse both `UPDATE` and
  `CREATE TABLE` while `SELECT` works.

### 2.2 A self-test that lies is worse than no self-test

`att_engine.py --selftest` opens every credential path and prints the errno.
Its first run reported `/srv/hermes/hermes-data/.env` as **READABLE** — and
that was the test's fault, not the unit's: it had been run through `nsenter`
as root, and root bypasses both the permission bits and the mode-0000 nodes
systemd bind-mounts for `InaccessiblePaths`. The command now refuses to run
as root. Measured correctly, inside the service's own mount namespace and as
the service user:

```text
running as uid=1000 gid=1000 groups=[4, 20, 24, 27, 29, ... 985 ...]
  blocked   hermes .env                /srv/hermes/hermes-data/.env  (PermissionError: 13)
  blocked   line channel secret        /etc/hermes-secrets/...        (PermissionError: 13)
  blocked   line access token          /etc/hermes-secrets/...        (PermissionError: 13)
  blocked   discord interface token    /etc/hermes-secrets/...        (PermissionError: 13)
  blocked   discord knowledge token    /etc/hermes-secrets/...        (PermissionError: 13)
  blocked   gateway hermes key         /etc/hermes-secrets/...        (PermissionError: 13)
  blocked   docker socket              /run/docker.sock               (PermissionError: 13)
  blocked   gateway state db           /srv/hermes/channel-gateway/state/gateway.db (PermissionError: 13)
result: 0 credential path(s) readable from this process
```

Note the group list. `SupplementaryGroups=` does **not** remove groups the
account holds in the system database — systemd documents this and the
measurement confirms it, so the service still carries `docker` (985) and
`sudo` (27). Both are inert here and that is also measured rather than
assumed: the docker socket answers `EACCES` because an inaccessible node is
mounted over it, and `NoNewPrivs=1` with an empty capability bounding set
makes the `sudo` membership unusable. A dedicated account would be cleaner;
§2.1 is why there is not one.

### 2.3 The install-time flood, avoided by watermark rather than by luck

At the moment of writing the Event Layer held 93 events, 48 open, **13 of
them already past their deadline**, because D.1 had backfilled a fortnight of
Discord history in one afternoon. The first real scan produced 25 signals.

Every one of them predates the engine's `initialized_at` watermark, so none
is announced individually; they become one Discord message:

```text
{"signals_seen": 25, "signals_new": 25, "notify_now": 0,
 "digested": 25, "suppressed": 0, "delivered": 1, "llm_calls": 0}
```

Age is judged from `occurred_at` and `due_at` — never from `created_at`,
which is when the extractor ran.

### 2.4 Two bugs the tests found before production did

**The due-window loop picked the largest matching window.** With windows
`[24h, 3h, 30m]` and a deadline twenty-five minutes away it returned the 24h
reminder, whose severity is INFO. The consequence was not cosmetic: no
deadline could ever have reached URGENT, so the LINE escalation path was
unreachable by construction. Fixed by scanning windows ascending and taking
the tightest one that applies.

**`_reserve_line` crashed when `deliver` was called without a budget** — the
retry path and the digest path both do. A LINE notification that failed once
would have raised `AttributeError` on every subsequent retry.

### 2.5 A false positive found by running it for real

The first health sweep against production reported `coding_supervisor /
job_stalled`: seven jobs sitting in `IMPLEMENTATION_ACCEPTED` for two days.
They are not stalled. The F.2 supervisor is not a daemon on this host — it
advances a job when Hermes invokes it — so a job waiting for a review nobody
has asked for is idle. `IMPLEMENTATION_ACCEPTED` and `WAITING_REVIEW` were
removed from the "should be actively running" set. This is section 41's rule
in reverse: an idle system must not be reported as a broken one.

### 2.6 One genuine failure, found on the first sweep

`line_refresh_daily.sh` had failed on 9/1, 9/2 and 9/3, and the LINE
knowledge index had not been rebuilt since 2026-09-01. This is a pre-existing
fault unrelated to G.2, and the engine found it within a minute of first
running. It is left unfixed deliberately — outside this phase's scope — and
is recorded in RESUME.md.

The probe was tightened as a result: one failed run of a daily job is
inconclusive, two consecutive failures are an incident. Waiting for the
episode threshold that "the job has not run at all" deserves (two days) would
be the wrong instrument for a job that has told us it failed.

## 3. The policy

Ordered, deterministic, first match wins. No model is consulted — which makes
"a scan with no signals costs zero LLM calls" a property of the build rather
than a claim about a code path.

| # | Rule | Outcome |
|---|------|---------|
| 1 | the owner silenced this key | SUPPRESS |
| 2 | the source is switched off | SUPPRESS |
| 3 | it predates the install | DIGEST (or SUPPRESS) |
| 4 | old news, no future deadline | SUPPRESS |
| 5 | expired | SUPPRESS |
| 6 | already delivered inside the cooldown | SUPPRESS |
| 7 | this thing is already being announced | DIGEST |
| 8 | the extractor was unsure | DIGEST / SUPPRESS |
| 9 | nothing to do about it | DIGEST |
| 10 | quiet hours | DEFER / DIGEST / send |
| 11 | returning from downtime | DIGEST |
| 12 | burst cap reached | DIGEST |
| 13 | — | NOTIFY_NOW |

Signals are decided most-severe-first, so the burst cap spends its three
slots on the three things that matter rather than on whatever the collectors
emitted first.

## 4. The LINE budget

Never a hardcoded number. Read through the gateway from LINE's own
endpoints, whose semantics were verified against `line/line-openapi`:

```text
GET /v2/bot/message/quota              -> {"type":"none"|"limited","value":N}
GET /v2/bot/message/quota/consumption  -> {"totalUsage":N}
```

`type: "none"` means **no target limit is set** — that is HEALTHY, not
UNKNOWN. UNKNOWN is reserved for a read that failed, and it restricts LINE to
urgent and critical, capped at one a day.

Measured on this account: `limited / 200 / used 2 / remaining 198`, which
produced a reserve of 50 and a daily allowance of 3 with 27 days left in the
month:

```text
routine_pool = remaining - ceil(limit * 0.25)      = 198 - 50 = 148
daily        = floor(148 / 27 * 0.7)               = 3
```

## 5. What was proven in production

| Check | Result |
|---|---|
| Discord proactive DM | 25 signals → one baseline digest, delivered |
| LINE proactive push | one URGENT fixture → **LINE `totalUsage` 2 → 3** |
| dedupe (re-scan) | 6 signals seen, 0 new, 0 delivered |
| gateway idempotency | same id twice → `delivered`, then `duplicate` |
| forged recipient | `{"status":"rejected","detail":"unknown_field:recipient_id"}` |
| gateway unavailable | notification stayed `PENDING`, never `DELIVERED` |
| gateway recovery | retried on the next scan, delivered, attempts=2 |
| quiet hours (injected clock) | 22:59 → 3 sent; 23:30 → 0 sent, 4 deferred |
| health episode | one alert after the threshold; rescan → none |
| coding | COMPLETED and WAITING_HUMAN sent; RUNNING and WAITING_REVIEW silent |
| LLM calls | 0 |

## 6. Files

```text
src/attention/
  app/att_schema.py     tables, policy version, install watermark
  app/att_time.py       tz-aware clock, quiet hours, billing month
  app/att_signals.py    canonical signal, stable derived ids
  app/att_config.py     strict configuration, conservative defaults
  app/att_sources.py    query_only connections to other components
  app/att_events.py     Event Layer  -> signals
  app/att_coding.py     jobs.db      -> signals
  app/att_health.py     probes       -> episodes -> signals
  app/att_budget.py     LINE budget state and daily allowance
  app/att_policy.py     the decision
  app/att_render.py     deterministic Japanese templates
  app/att_gateway.py    stdlib HTTP over the unix socket
  app/att_engine.py     the loop, delivery, digests, CLI
  app/att_store.py      durable operations
  app/att_status.py     read-only status, no bodies, no credentials

src/channel-gateway/
  app/gw_proactive.py   the owner-notification socket API
  app/gw_quota.py       LINE quota reader with an auth backoff
```

## 7. Known limitations

* **Discord has no idempotency key.** A crash in the window between
  `channel.send()` returning and the state row being written can duplicate a
  DM. LINE is protected by a retry key derived from the delivery id; Discord
  is protected only by the `proactive_deliveries` row. One duplicate message
  on the unmetered channel is the accepted residual.
* **Supplementary groups are not dropped** (§2.2). Inert, and measured to be
  inert, but present.
* **No MCP surface.** `attention_status` / `attention_recent` /
  `attention_suppress` were deferred: they appear in no acceptance criterion,
  and the observability requirement is met by `att-status.sh` on the host.
  Suppression is available as `att_engine.py --suppress <dedupe_key>`.
* **No LLM wording.** The configuration has `llm.enabled: false` and there is
  no code path to a model. Deterministic templates only.
* **The catch-up digest and `chief-morning-review` have not yet run on the
  same morning.** They are designed not to overlap — the catch-up carries
  only what was deferred overnight, the review is the day's brief — but that
  has not been observed in production.
* **`line_refresh_daily.sh` is still failing** (§2.6). Detected, reported,
  not fixed.
