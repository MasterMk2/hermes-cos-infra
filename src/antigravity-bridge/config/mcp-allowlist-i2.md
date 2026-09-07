# Phase I.2 — Antigravity MCP allowlist delta (two entries)

**Nothing on the Pi was edited by T5.** This document plus
`mcp-allowlist-i2.snippet.json` is the reviewable artifact; the coordinator applies it
as root in the serialized deployment step.

---

## 1. What is added

Exactly two entries, appended to `permissions.allow`:

```json
      "mcp(event/daily_plan_get)",
      "mcp(event/morning_brief_get)"
```

* Server: the **existing** `event` MCP server. No new `mcpServers` registration, no new
  venv, no new file permission.
* `permissions.deny` is **unchanged** (6 entries).
* Wildcards: none. A wildcard `mcp` allow entry must not appear anywhere
  (phase section 48). The forbidden token itself is deliberately never written out in
  this repository, so a security grep for it can only ever be a true positive.

**Count:** `25 → 27` against the documented baseline (I2-PROFILE.md section K.2).

> ⚠️ The 25-entry baseline was read from the operator account's readable copy, whose
> mtime predates Phase I.1. The live `hermesantigravity` list may already carry the five
> Phase I.1 entries (`agenda_get`, `commitment_*`). **Read the live file first** and
> enforce the real invariant: `after == before + 2`, and the two strings above are the
> only additions.

---

## 2. Files

| Role | Path |
|---|---|
| Allowlist (apply here) | `/home/hermesantigravity/.gemini/antigravity-cli/settings.json` |
| MCP server registration (no change needed) | `/home/hermesantigravity/.gemini/config/mcp_config.json` |
| Deployed server script — **Antigravity** (authoritative) | `/srv/hermes/antigravity/mcp/event/event_mcp.py` |
| Planning package — **Antigravity** (authoritative) | `/srv/hermes/antigravity/mcp/event/planning/` |
| Deployed server script — **Hermes container** (`event_layer`) | `/srv/hermes/hermes-data/tools/events/event_mcp.py` |
| Planning package — **Hermes container** | `/srv/hermes/hermes-data/tools/events/planning/` |

Both files under `/home/hermesantigravity` are unreadable to the operator account
(home is `drwx------`), so every step below needs `sudo`.

> **BOTH copies must be updated. Corrected during T6 integration.**
> T5 wrote this procedure before the ownership of the two `event_mcp.py` copies was
> settled and targeted only the `hermes-data/tools/events` path. The coordinator then
> verified with root (recorded as AMENDMENT 2 in `I2-AMENDMENTS.md`) that
> `hermesantigravity`'s `mcp_config.json` launches
> `/srv/hermes/antigravity/venv/bin/python3 /srv/hermes/antigravity/mcp/event/event_mcp.py`
> with `PYTHONPATH=/srv/hermes/antigravity/mcp/event` — so **that** copy is what the
> constrained agent runs, and `planning/` must sit next to it, mirroring how I.1 shipped
> `/srv/hermes/antigravity/mcp/event/commitments/`.
> The `hermes-data/tools/events` copy is *not* dead: section 5.2 below shows it is the
> live `event_layer` server for the Hermes agent container, which is what the Discord
> frontend reaches. Updating only one of the two leaves the other advertising 14 tools —
> precisely the silent half-landing section 5.1 warns about.

---

## 3. Deployment order (matters)

The allowlist entry must **not** land before the tool exists, or Antigravity will allow a
tool the server does not advertise.

1. **Deploy the code first — to both consumers.**

   a. Antigravity (the constrained agent; `root:root`, matching the existing
      `/srv/hermes/antigravity/mcp/event/commitments/`):
   ```bash
   sudo install -o root -g root -m 0644 \
       event_mcp.py /srv/hermes/antigravity/mcp/event/event_mcp.py
   sudo install -d -o root -g root -m 0755 \
       /srv/hermes/antigravity/mcp/event/planning
   sudo install -o root -g root -m 0644 \
       mcp_planning.py planner.py renderer.py plan_models.py plan_config.py freshness.py \
       /srv/hermes/antigravity/mcp/event/planning/
   ```

   b. The Hermes agent container's `event_layer` server (section 5.2):
   ```bash
   sudo install -o <PI_USER> -g <PI_USER> -m 0644 \
       event_mcp.py /srv/hermes/hermes-data/tools/events/event_mcp.py
   sudo install -d -o <PI_USER> -g <PI_USER> -m 0755 \
       /srv/hermes/hermes-data/tools/events/planning
   sudo install -o <PI_USER> -g <PI_USER> -m 0644 \
       mcp_planning.py planner.py renderer.py plan_models.py plan_config.py freshness.py \
       /srv/hermes/hermes-data/tools/events/planning/
   ```

   Six modules, and **only** those six: `mcp_planning.py` imports the deterministic core
   plus the renderer and nothing else. `delivery.py`, `brief_state.py`,
   `brief_service.py` and `cli.py` must **not** be deployed here — they are the
   scheduled-brief half, they belong to `hermesplanner` under `/srv/hermes/planning/app/`,
   and shipping them into the MCP tree would put a delivery client inside the agent's
   import path for no reason. `tests-planning/test_integration_pipeline.py` and
   `test_mcp_planning.py` both pin that `mcp_planning` never reaches them.

   Each server is launched with its own directory on the path — Antigravity via an
   explicit `PYTHONPATH=/srv/hermes/antigravity/mcp/event`, the container via
   `event_mcp.py`'s own `sys.path` bootstrap (section 5.2) — so in both trees `planning/`
   sitting next to `event_mcp.py` is importable as `planning.*`.
   **This is the import path T5 relies on** — `planning.mcp_planning`, with
   `src.planning.mcp_planning` only as a repo-local fallback. `planning/__init__.py` is
   optional (namespace package), but shipping one is harmless.

   `src/planning/install-root.sh` does **not** do any of this: it installs the
   scheduled-brief app under `/srv/hermes/planning/` and deliberately never writes into
   the Antigravity tree. The two halves of Phase I.2 have separate deployment steps.

2. **Verify the server advertises 16 tools** before touching the allowlist:
   ```bash
   sudo -u <PI_USER> env \
     PYTHONPATH=/srv/hermes/hermes-data/tools/events \
     EVENTS_DB=/srv/hermes/hermes-data/events/events.db \
     /home/<PI_USER>/antigravity-probe-venv/bin/python3 - <<'PY'
   import asyncio, event_mcp
   tools = asyncio.run(event_mcp._list_tools()).tools
   print(len(tools)); print([t.name for t in tools])
   PY
   ```
   Expect `16` and both new names present.

3. **Back up, then apply the allowlist delta as root.**
   ```bash
   sudo cp -a /home/hermesantigravity/.gemini/antigravity-cli/settings.json \
              /home/hermesantigravity/.gemini/antigravity-cli/settings.json.pre-i2.bak
   sudo -u hermesantigravity python3 - <<'PY'
   import json, pathlib
   p = pathlib.Path("/home/hermesantigravity/.gemini/antigravity-cli/settings.json")
   cfg = json.loads(p.read_text(encoding="utf-8"))
   allow = cfg["permissions"]["allow"]
   before = len(allow)
   for entry in ("mcp(event/daily_plan_get)", "mcp(event/morning_brief_get)"):
       if entry not in allow:
           allow.append(entry)
   assert not any("*" in e for e in allow), "wildcard allow entry present - abort"
   assert len(allow) == before + 2, (before, len(allow))
   p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
   print("allow:", before, "->", len(allow), "deny:", len(cfg["permissions"]["deny"]))
   PY
   ```
   Preserve the file's existing mode (`0600`) and owner (`hermesantigravity`) — the
   heredoc above runs *as* that user, so both are preserved automatically.

4. **Restart and verify.**
   ```bash
   sudo systemctl restart hermes-antigravity.service
   sudo -u hermesantigravity grep -c 'mcp(' \
       /home/hermesantigravity/.gemini/antigravity-cli/settings.json
   # must print nothing: no allow entry may contain a wildcard
   sudo -u hermesantigravity python3 -c "import json,sys;\
d=json.load(open('/home/hermesantigravity/.gemini/antigravity-cli/settings.json'));\
print([e for e in d['permissions']['allow'] if '*' in e])"
   ```

**Rollback:** restore `settings.json.pre-i2.bak` and restart the service. The two tools
then remain in the server but are not allowed, which is the pre-I.2 security posture.

---

## 4. Do the deny rules need a companion entry?

**No — explicitly, and by design.**

1. The allowlist is an **exact string list**, not a glob. `mcp(event/daily_plan_get)`
   grants that one tool and nothing adjacent to it.
2. Neither new name shares a prefix or a stem with any denied name
   (`event_link_project`, `event_mark_cancelled`, `event_mark_ignored`,
   `event_mark_resolved`, `event_snooze`). They are not even `event_`-prefixed. There is
   no string by which the model or the CLI could confuse a plan read with a mutation.
3. Both tools are read-only **in code**: they delegate to `planning.mcp_planning`, which
   opens `events.db` with `mode=ro` plus `PRAGMA query_only=1`, never calls a write path,
   never imports `planning.delivery` / `planning.brief_state` / `planning.brief_service`,
   and returns a compacted projection. `tests-planning/test_mcp_planning.py` pins this.
4. Adding a deny entry for a tool name that does not exist would be dead configuration
   and would falsely imply a mutating planning tool exists.

**Standing rule for later phases:** if a mutating planning tool is ever added (a plan
dismiss, snooze, or re-plan write), its name goes into `permissions.deny` in the *same*
change that adds it to the server.

---

## 5. Verified deployment facts (T5, read-only inspection of the production host)

Measured, not assumed. These extend I2-PROFILE.md section K.4.

1. **`event_mcp.py` exists twice on the host**, byte-identical today (same md5):

   | Path | Owner / mode | Referenced by |
   |---|---|---|
   | `/srv/hermes/hermes-data/tools/events/event_mcp.py` | `<PI_USER>:<PI_USER>` `0644` | the live `event` registration and the Hermes container's `event_layer` server |
   | `/srv/hermes/antigravity/mcp/event/event_mcp.py` | `root:root` `0644` | only `setup_mcp_and_user.py` (the original H.3.2 deployment script); **no current registration points at it** |

   The operator's `mcp_config.json` launches the first path. The `hermesantigravity`
   copy of `mcp_config.json` is root-only and was not read — **the coordinator must
   confirm with `sudo -u hermesantigravity cat` which of the two paths it launches**,
   and update that one (or both). Leaving a stale 14-tool copy behind is the one way
   this deployment can silently half-land.

2. **The Hermes agent container is a second consumer**, and it matters for the Discord
   frontend gate. `/srv/hermes/hermes-data/config.yaml` registers:
   ```yaml
   event_layer:
     command: /opt/hermes/.venv/bin/python3
     args:
     - /opt/data/tools/events/event_mcp.py
   ```
   `/opt/data` is the container's mount of `/srv/hermes/hermes-data`, so it is the same
   file. Note there is **no `PYTHONPATH` and no `EVENTS_DB`** in this registration: the
   container relies on `event_mcp.py`'s own `sys.path` bootstrap (it inserts its own
   directory and its parent) and on the `/opt/data/events/events.db` default. This is
   exactly why `planning/` must sit **next to `event_mcp.py`**, as `commitments/`
   already does — that one location satisfies the Antigravity `PYTHONPATH` *and* the
   container's bootstrap, with no config change in either place.

3. **The MCP schema cache does not need invalidating.**
   `/srv/hermes/hermes-data/cache/mcp_schema_cache.json` caches the advertised tool list
   per server; its `event_layer` entry currently lists exactly the 14 Phase I.1 tools.
   Its `ttl_ms` is `0`, and `tools/mcp_schema_cache.py:84-88` treats an entry as expired
   when `(now - written_at) * 1000 >= ttl_ms` — always true at `ttl_ms == 0`. The entry
   is therefore re-discovered on every read and will pick up 16 tools by itself. No
   manual cache clear is required; `clear_cache_entry("event_layer")` exists if the
   coordinator wants to force it anyway.
