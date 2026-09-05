# Phase H.1 — Google Calendar Authority Layer

> 状態: **COMPLETED (2026-09-05)**。
> 実装・Pi 配備・read-only OAuth・実 Google Calendar E2E・既存経路の
> production regression を完了した。

## Architecture

```text
Google Calendar API
  ├─ events.list full + syncToken incremental
  └─ events.watch HTTPS hint
             ↓
hermes-calendar.service (hermescalendar, read-only OAuth scopes)
  ├─ canonical SQLite state
  ├─ bounded recurring-instance projection
  ├─ durable/coalesced sync queue
  └─ atomic read-only index publication
             ├─ Calendar MCP → Hermes
             └─ Attention Engine + Event reconciliation
```

The official scopes are deliberately narrowed to
`calendar.events.readonly` and `calendar.calendarlist.readonly`. Google
documents that `events.watch` accepts the former; no event mutation scope is
requested.

## Boundaries

- `hermescalendar` is a dedicated nologin system account with no privileged
  supplementary group.
- OAuth client ID, optional client secret, and refresh token are root-owned
  files under `/etc/hermes-secrets/google-calendar/`, passed only through
  systemd `LoadCredential`. Access tokens remain in process memory.
- The Calendar authority DB is host-only. Hermes and Attention consume a
  replacement-file read-only index; neither receives OAuth credentials.
- Calendar descriptions and all other Calendar text are untrusted data. MCP
  tool descriptions and the AGENTS rule fragment repeat this boundary.
- The sole proposed public route is `POST /google/calendar/watch`. It only
  validates Google channel headers, coalesces a durable sync request, and
  returns 204. It never reads an event body from the webhook. The route uses
  Caddy `log_skip`, so its opaque channel-authentication token is not retained
  in public-proxy access logs.

## Sync semantics

Initial/full state is staged in `resync_staging` and published in one SQLite
transaction only after every pagination page and `nextSyncToken` succeeds.
A 410 leaves the previous snapshot available until the replacement is ready;
later full-resync differences are compared so a moved/cancelled event remains
visible to Attention.

The canonical stream retains recurring masters and exceptions without a
time bound. A separate projection request expands only the past 30 days and
future 365 days, so infinite recurrences do not create an unbounded database.
That projection never uses a syncToken, because the Calendar API forbids
combining a syncToken with time range restrictions.

## Retrieval and Attention

`calendar_search`, `calendar_get`, and `calendar_status` are the only MCP
tools. They open the index with SQLite `mode=ro` and have neither a Google API
client nor a write route.

The Event Layer is not migrated. `event_source_links` resides in Calendar's
own DB. An explicit link is authoritative; an automatic link is allowed only
for one exact normalized title/time candidate. Multiple candidates remain
unmerged.

G.2 receives Calendar signals through the read-only index. Routine upcoming
events are suppressed so Google Calendar remains the routine reminder source;
an explicitly configured important keyword, a significant move, or a future
cancellation enters the normal G.2 dedupe, quiet-hours, burst-cap, and
Discord-first policy. Initial full sync creates no change log and therefore
no individual notification flood.

## Deployment gate

The installer intentionally stops before all irreversible or user-owned
actions. Before starting the service, the operator must:

1. Enable the Google Calendar API and create a **Desktop** OAuth client in
   the intended Google Cloud project.
2. Install the client ID/secret into root-only files on the Pi. Do not put
   them in YAML, `.env`, Git, or chat.
3. Use the loopback OAuth helper through SSH port forwarding. Its redirect is
   `http://127.0.0.1:8765/oauth2/callback`; it does not add a public OAuth
   callback route.
4. Review the discovery output and explicitly populate stable calendar IDs in
   `calendars.selected_ids`. No calendar is selected by default.
5. Inspect the existing Caddy vhost, add the supplied single route after
   substituting the Pi LAN address, validate the complete production config,
   and use the existing Caddy restart procedure.
6. Add the supplied MCP map entry to the existing Hermes `mcp_servers` map
   without changing other servers, add the AGENTS rule fragment, and restart
   the Hermes container only after config validation.

## Production completion record

The completion gate was verified on 2026-09-05 without adding a Calendar
write path or a public Hermes endpoint:

- Google Calendar: selected-only full and incremental sync, push, polling,
  recurrence exception, cancelled instance, and missed-push recovery.
- Calendar retrieval: the three read-only Calendar MCP tools returned the
  published snapshot; OAuth is unavailable to Hermes.
- Attention: initial import produced no individual flood; a deferred Calendar
  catch-up digest was delivered to Discord after quiet hours.
- G.1 regression: real owner Discord DM and LINE webhook each completed the
  inbound → Hermes → owner response path exactly once. The LINE reply used its
  permitted push fallback after the reply-token window.
- D.1 regression: real Discord index search and source-ID get preserved
  provenance and remained read-only.
- Security: Calendar credentials remain isolated; callback authentication is
  verified before queueing and its access log is skipped.
