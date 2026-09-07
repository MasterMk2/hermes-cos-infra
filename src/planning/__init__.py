"""Hermes Phase I.2 — Daily Planning package.

The planning layer is a *read-only projection* over the canonical commitments
store. It never writes `events.db` (phase section 64) and the deterministic core
(`plan_config`, `plan_models`, `freshness`, `planner`) imports no LLM or provider
code at all (phase section 75): a daily plan must still be produced when every
model in the fallback chain is down.

Module map (I2-CONTRACT.md section 1):

* ``plan_config``  — defaults + config loading, shared by every module here
* ``plan_models``  — item/conflict dataclasses, enums, closed vocabularies
* ``freshness``    — per-source freshness collection (phase sections 36, 37)
* ``planner``      — ``build_daily_plan()``, the deterministic selector/ranker
* ``renderer``     — natural-language rendering (owned by another worker)
"""

__all__ = ["plan_config", "plan_models", "freshness", "planner"]
