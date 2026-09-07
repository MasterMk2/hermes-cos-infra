#!/usr/bin/env python3
"""Phase I.2 - MCP boundary adapter for the daily plan and the morning brief.

This module is the only bridge between the MCP surface (``src/events/event_mcp.py``)
and the deterministic planner / renderer in ``src/planning/``. It exists so that
``event_mcp.py`` - live production code serving the Phase I.1 tools - only gains
two thin handlers and two schemas.

Design rules (I2-CONTRACT.md sections 0 and 7; phase sections 31-35, 45-49, 64, 73):

* **Strictly read-only.** The connection to ``events.db`` is opened read-only
  (``immutable=1`` only as a last resort) with ``PRAGMA query_only=1``, and is
  always closed. Nothing here writes, mutates state, delivers, or schedules, and
  no delivery or state-writing module is imported anywhere in this file - a test
  greps this source to prove it.
* **Lazy imports.** ``planning.planner`` (T2) and ``planning.renderer`` (T3) are
  imported *inside* the call. This module - and therefore the whole MCP server -
  imports cleanly while ``src/planning/`` is incomplete or is not deployed next to
  ``event_mcp.py``. A missing module produces a clean ``{"error": "..._unavailable"}``
  result, never an ImportError at server start-up.
* **Bounded output.** Item dicts are projected through a fixed field whitelist,
  each category is clamped to ``MAX_ITEMS_PER_CATEGORY``, and the serialized plan
  is trimmed to ``MAX_PLAN_JSON_BYTES``. A future planner field can therefore never
  leak a raw source body into an LLM context window (phase section 73), and an
  oversized plan can never blow up the context.
* **Clean errors.** Malformed input produces an error *result*, never a traceback,
  and internal failures are reported as an exception *type* only - never a message,
  which could contain a filesystem path or source content.

``morning_brief_get`` renders from the **compacted** plan only: the canonical DB is
never dumped into the model context (phase section 49).
"""

import importlib
import json
import os
import re
import sqlite3
from datetime import datetime

try:  # Python 3.9+ stdlib; the Pi runs 3.11+
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - defensive, not reachable on the Pi
    ZoneInfo = None

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

DEFAULT_TIMEZONE = "Asia/Tokyo"          # phase section 32
DEFAULT_EVENTS_DB = "/opt/data/events/events.db"
DEFAULT_MODE = "auto"

#: ``mode`` values reachable from the MCP surface. ``llm`` is deliberately NOT
#: exposed: ``auto`` already tries the LLM renderer and falls back to the
#: deterministic template, so an MCP caller has no reason to force a hard failure.
VALID_MODES = ("auto", "template")

#: Plan categories, in the section 21 precedence order.
PLAN_CATEGORIES = ("scheduled", "must_do", "overdue", "next_up", "waiting_for", "backlog")

#: Order in which categories are trimmed when the serialized plan is too large.
#: Lowest decision value first; ``scheduled`` and ``must_do`` are trimmed last.
_TRIM_ORDER = ("backlog", "waiting_for", "next_up", "conflicts", "overdue", "must_do", "scheduled")

#: Defensive clamp. The configured caps (AMENDMENT 1 section F) are 3-7 per
#: category; this is the boundary's own guarantee, independent of config.
MAX_ITEMS_PER_CATEGORY = 10

#: Hard ceiling on the serialized ``daily_plan_get`` payload.
MAX_PLAN_JSON_BYTES = 32768

#: Hard ceiling on ``brief_text`` (the renderer's own limit is ~1200 chars).
MAX_BRIEF_CHARS = 4000

#: Item fields the boundary forwards, in this fixed order. Anything the planner
#: adds later is dropped until it is added here on purpose.
ITEM_FIELDS = (
    "commitment_id",
    "title",
    "responsibility",
    "work_state",
    "due_at",
    "due_precision",
    "scheduled_at",
    "scheduled_end_at",
    "all_day",
    "local_time",
    "counterparty",
    "priority_class",
    "reason_for_inclusion",
    "source_types",
    "overdue_days",
)

#: Phase section 34: every item must stay traceable. These keys are always
#: present in the projection, even when the planner omitted them.
REQUIRED_ITEM_FIELDS = (
    "commitment_id",
    "title",
    "responsibility",
    "work_state",
    "due_at",
    "scheduled_at",
    "reason_for_inclusion",
)

CONFLICT_FIELDS = ("kind", "commitment_ids", "window_start", "window_end", "detail")

#: Top-level plan keys forwarded verbatim. ``plan_hash`` is absent on purpose
#: (I2-CONTRACT.md section 7: dropped from the compact projection).
PLAN_HEADER_FIELDS = ("date", "timezone", "generated_at")
PLAN_TAIL_FIELDS = ("counts", "source_freshness")

_PLANNER_MODULES = ("planning.planner", "src.planning.planner")
_RENDERER_MODULES = ("planning.renderer", "src.planning.renderer")
_CONFIG_MODULES = ("planning.plan_config", "src.planning.plan_config")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TZ_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._+/-]{0,63}$")


# --------------------------------------------------------------------------
# Input validation - a malformed argument returns an error result, never raises
# --------------------------------------------------------------------------

def _validate_date(value):
    """Return ``(date_or_None, error_or_None)``. ``None``/empty means 'today'."""
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, {"error": "invalid_date", "detail": "date must be a string 'YYYY-MM-DD'"}
    text = value.strip()
    if not text:
        return None, None
    if len(text) > 32 or not _DATE_RE.match(text):
        return None, {"error": "invalid_date", "detail": "expected format 'YYYY-MM-DD'"}
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None, {"error": "invalid_date", "detail": "not a real calendar date"}
    return text, None


def _validate_timezone(value):
    """Return ``(tz_name, error_or_None)``. ``None``/empty means ``Asia/Tokyo``."""
    if value is None:
        return DEFAULT_TIMEZONE, None
    if not isinstance(value, str):
        return None, {"error": "invalid_timezone", "detail": "timezone must be an IANA name string"}
    text = value.strip()
    if not text:
        return DEFAULT_TIMEZONE, None
    if ".." in text or not _TZ_RE.match(text):
        return None, {"error": "invalid_timezone", "detail": "not a valid IANA timezone name"}
    if ZoneInfo is not None:
        try:
            ZoneInfo(text)
        except Exception:
            return None, {"error": "invalid_timezone", "detail": "unknown IANA timezone"}
    return text, None


def _validate_mode(value):
    """Return ``(mode, error_or_None)``. ``None``/empty means ``auto``."""
    if value is None:
        return DEFAULT_MODE, None
    if not isinstance(value, str):
        return None, {"error": "invalid_mode", "allowed": list(VALID_MODES)}
    text = value.strip().lower()
    if not text:
        return DEFAULT_MODE, None
    if text not in VALID_MODES:
        return None, {"error": "invalid_mode", "allowed": list(VALID_MODES)}
    return text, None


# --------------------------------------------------------------------------
# Read-only data access
# --------------------------------------------------------------------------

def _connect_readonly(db_path):
    """Open ``events.db`` read-only. Never requests write mode, never migrates."""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    except sqlite3.OperationalError:
        con = sqlite3.connect(f"file:{db_path}?immutable=1", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=10000")
    try:
        con.execute("PRAGMA query_only=1")
    except sqlite3.Error:  # pragma: no cover - very old sqlite only
        pass
    return con


def _resolve(module_names, attr):
    """Lazily resolve ``attr`` from the first importable module. ``None`` if absent."""
    for name in module_names:
        try:
            module = importlib.import_module(name)
        except Exception:
            continue
        fn = getattr(module, attr, None)
        if fn is not None:
            return fn
    return None


def _load_planning_config(load_config=None):
    """Best-effort planning config. Never fatal: ``None`` means 'planner defaults'."""
    fn = load_config or _resolve(_CONFIG_MODULES, "load_config")
    if fn is None:
        return None
    try:
        cfg = fn()
    except Exception:
        return None
    return cfg if isinstance(cfg, dict) else None


def _resolve_db_path(db_path=None):
    return db_path or os.environ.get("EVENTS_DB") or DEFAULT_EVENTS_DB


def _build_plan(date_value, tz_value, *, db_path=None, build_plan=None, load_config=None):
    """Return ``(plan, error)``. Read-only; the connection is always closed."""
    fn = build_plan or _resolve(_PLANNER_MODULES, "build_daily_plan")
    if fn is None:
        return None, {
            "error": "planning_module_unavailable",
            "detail": "planning.planner.build_daily_plan is not importable",
        }
    config = _load_planning_config(load_config)
    path = _resolve_db_path(db_path)
    if not os.path.exists(path):
        # `immutable=1` would happily open a phantom database and fail later with
        # a confusing "no such table". Fail here, cleanly and once.
        return None, {"error": "events_db_unavailable", "detail": "database file not found"}
    try:
        con = _connect_readonly(path)
    except sqlite3.Error as exc:
        return None, {"error": "events_db_unavailable", "detail": type(exc).__name__}
    try:
        plan = fn(con, date=date_value, timezone=tz_value, config=config)
    except Exception as exc:
        return None, {"error": "planner_error", "detail": type(exc).__name__}
    finally:
        try:
            con.close()
        except sqlite3.Error:  # pragma: no cover
            pass
    if not isinstance(plan, dict):
        return None, {"error": "planner_error", "detail": "planner_returned_non_dict"}
    return plan, None


# --------------------------------------------------------------------------
# Compaction and size bounding
# --------------------------------------------------------------------------

def _project_item(item):
    """Whitelist an Item dict, guaranteeing the phase section 34 traceable keys."""
    out = {}
    for key in ITEM_FIELDS:
        if key in item:
            out[key] = item[key]
        elif key in REQUIRED_ITEM_FIELDS:
            out[key] = None
    return out


def _project_conflict(conflict):
    return {key: conflict[key] for key in CONFLICT_FIELDS if key in conflict}


def _json_bytes(payload):
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _bound_plan_size(plan):
    """Trim the lowest-value categories until the payload fits ``MAX_PLAN_JSON_BYTES``."""
    if _json_bytes(plan) <= MAX_PLAN_JSON_BYTES:
        return plan
    trimmed = []
    for category in _TRIM_ORDER:
        items = plan.get(category)
        if not items:
            continue
        while items and _json_bytes(plan) > MAX_PLAN_JSON_BYTES:
            items.pop()
            if category not in trimmed:
                trimmed.append(category)
        if _json_bytes(plan) <= MAX_PLAN_JSON_BYTES:
            break
    if trimmed:
        plan["truncated"] = True
        plan["truncated_categories"] = trimmed
    return plan


def compact_plan(plan):
    """Project a full plan (I2-CONTRACT.md section 3) into the LLM-facing shape.

    Drops ``plan_hash``; keeps ``counts`` and ``source_freshness``; whitelists and
    clamps every list. The result is what both ``daily_plan_get`` returns and what
    ``morning_brief_get`` hands to the renderer.
    """
    out = {}
    for key in PLAN_HEADER_FIELDS:
        if key in plan:
            out[key] = plan[key]
    for category in PLAN_CATEGORIES:
        items = plan.get(category) or []
        if not isinstance(items, list):
            items = []
        out[category] = [
            _project_item(item)
            for item in items[:MAX_ITEMS_PER_CATEGORY]
            if isinstance(item, dict)
        ]
    conflicts = plan.get("conflicts") or []
    if not isinstance(conflicts, list):
        conflicts = []
    out["conflicts"] = [
        _project_conflict(c) for c in conflicts[:MAX_ITEMS_PER_CATEGORY] if isinstance(c, dict)
    ]
    for key in PLAN_TAIL_FIELDS:
        if key in plan:
            out[key] = plan[key]
    warnings = plan.get("warnings")
    if warnings:
        out["warnings"] = list(warnings)
    return _bound_plan_size(out)


# --------------------------------------------------------------------------
# Public tool handlers
# --------------------------------------------------------------------------

def daily_plan_get(args=None, *, db_path=None, build_plan=None, load_config=None):
    """``daily_plan_get`` - read-only compact daily plan (phase sections 31-35).

    Injectable ``build_plan`` / ``load_config`` exist for tests only; production
    resolves them lazily from ``planning.planner`` / ``planning.plan_config``.
    """
    args = args or {}
    date_value, err = _validate_date(args.get("date"))
    if err:
        return err
    tz_value, err = _validate_timezone(args.get("timezone"))
    if err:
        return err

    plan, err = _build_plan(
        date_value, tz_value, db_path=db_path, build_plan=build_plan, load_config=load_config
    )
    if err:
        return err
    return compact_plan(plan)


def morning_brief_get(
    args=None, *, db_path=None, build_plan=None, render=None, load_config=None
):
    """``morning_brief_get`` - the compact plan rendered to Japanese prose.

    Internally: ``daily_plan_get`` -> the fixed renderer (phase section 49). The
    renderer only ever sees the compacted plan, never the canonical DB.
    """
    args = args or {}
    date_value, err = _validate_date(args.get("date"))
    if err:
        return err
    tz_value, err = _validate_timezone(args.get("timezone"))
    if err:
        return err
    mode, err = _validate_mode(args.get("mode"))
    if err:
        return err

    render_fn = render or _resolve(_RENDERER_MODULES, "render_brief")
    if render_fn is None:
        return {
            "error": "renderer_module_unavailable",
            "detail": "planning.renderer.render_brief is not importable",
        }

    plan, err = _build_plan(
        date_value, tz_value, db_path=db_path, build_plan=build_plan, load_config=load_config
    )
    if err:
        return err
    compact = compact_plan(plan)

    config = _load_planning_config(load_config)
    try:
        rendered = render_fn(compact, mode=mode, config=config)
    except Exception as exc:
        return {"error": "renderer_error", "detail": type(exc).__name__}
    if not isinstance(rendered, dict):
        return {"error": "renderer_error", "detail": "renderer_returned_non_dict"}

    text = rendered.get("text")
    if not isinstance(text, str) or not text.strip():
        return {"error": "renderer_error", "detail": "renderer_returned_empty_text"}
    if len(text) > MAX_BRIEF_CHARS:
        text = text[:MAX_BRIEF_CHARS]

    renderer_name = rendered.get("renderer")
    if not isinstance(renderer_name, str) or not renderer_name:
        renderer_name = "unknown"

    counts = compact.get("counts")
    return {
        "date": compact.get("date") or date_value,
        "timezone": compact.get("timezone") or tz_value,
        "brief_text": text,
        "renderer": renderer_name,
        "counts": counts if isinstance(counts, dict) else {},
    }
