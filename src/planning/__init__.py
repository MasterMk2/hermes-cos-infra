"""Hermes Phase I.2 — Daily Planning package.

The planning layer is a *read-only projection* over the canonical commitments
store. It never writes `events.db` (phase section 64) and the deterministic core
(`plan_config`, `plan_models`, `freshness`, `planner`) imports no LLM or provider
code at all (phase section 75): a daily plan must still be produced when every
model in the fallback chain is down.

Module map (I2-CONTRACT.md section 1), completed at Phase I.2 integration:

Deterministic core — no LLM, no network, no provider import (phase section 75):

* ``plan_config``   — defaults + config loading, shared by every module here
* ``plan_models``   — item/conflict dataclasses, enums, closed vocabularies
* ``freshness``     — per-source freshness collection (phase sections 36, 37)
* ``planner``       — ``build_daily_plan()``, the deterministic selector/ranker

Rendering and the read-only tool boundary:

* ``renderer``      — ``render_brief()``, the deterministic Japanese template,
  the grounding gate, and an optional LLM path that ships **off**
* ``mcp_planning``  — the ``daily_plan_get`` / ``morning_brief_get`` adapter
  behind ``src/events/event_mcp.py`` (phase sections 47-49)

The scheduled morning brief (phase sections 50-61). These are *not* part of the
MCP deployment: only the six modules above ship next to ``event_mcp.py``, so a
delivery client never enters the agent's import path.

* ``brief_state``   — the separate planning SQLite, never ``events.db``
* ``delivery``      — the gateway proactive client (Discord only, no token here)
* ``brief_service`` — the scheduled run: late boot, idempotency, bounded retry
* ``cli``           — the entrypoint systemd runs and the manual dry-run
"""

__all__ = [
    "plan_config",
    "plan_models",
    "freshness",
    "planner",
    "renderer",
    "mcp_planning",
    "brief_state",
    "delivery",
    "brief_service",
    "cli",
]
