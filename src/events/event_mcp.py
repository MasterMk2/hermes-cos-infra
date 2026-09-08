#!/usr/bin/env python3
"""Personal Event & Unified Commitment MCP server (read/write on events.db ONLY, stdio).

The Event server is the only component allowed to write events.db.
It never touches the LINE snapshot or the search index. State changes are
transitions only (no hard deletes) and are recorded in event_history.
All content boundaries: event summaries + provenance; no LINE full text
is stored here.

Phase I.1 adds:
- commitment_list
- commitment_get
- commitment_search
- commitment_status
- agenda_get

Phase I.2 adds (read-only, no writes, no delivery):
- daily_plan_get
- morning_brief_get
"""

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

# Ensure local tools can import commitments module
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(SCRIPT_DIR)
for p in [SCRIPT_DIR, PARENT_DIR, "/opt/data/tools", "/srv/hermes/hermes-data/tools"]:
    if p not in sys.path and os.path.isdir(p):
        sys.path.insert(0, p)

try:
    from commitments.store import (
        init_db as init_commitment_db,
        get_commitment,
        list_commitments,
        search_commitments,
        get_status as get_commitment_status,
    )
    from commitments.agenda import generate_agenda
except ImportError:
    # Fallback if package is adjacent
    try:
        from src.commitments.store import (
            init_db as init_commitment_db,
            get_commitment,
            list_commitments,
            search_commitments,
            get_status as get_commitment_status,
        )
        from src.commitments.agenda import generate_agenda
    except ImportError:
        init_commitment_db = None
        get_commitment = None
        list_commitments = None
        search_commitments = None
        get_commitment_status = None
        generate_agenda = None

# Phase I.2 planning boundary. The adapter itself is stdlib-only and imports the
# planner/renderer lazily, so this stays importable while src/planning is partial.
try:
    from planning.mcp_planning import (
        daily_plan_get as planning_daily_plan_get,
        morning_brief_get as planning_morning_brief_get,
    )
except ImportError:
    try:
        from src.planning.mcp_planning import (
            daily_plan_get as planning_daily_plan_get,
            morning_brief_get as planning_morning_brief_get,
        )
    except ImportError:
        planning_daily_plan_get = None
        planning_morning_brief_get = None

try:
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server
    import mcp.types as types
except ImportError:
    Server = None
    stdio_server = None
    types = None

DB = os.environ.get("EVENTS_DB", "/opt/data/events/events.db")
JST = timezone(timedelta(hours=9))

# Conservative surfacing thresholds (MVP, precision-first)
MIN_CONFIDENCE = 0.70
MIN_ATTENTION = 0.25
SURFACE_TYPES_EXCLUDED = {"social", "informational"}
DUP_SUPPRESS_HOURS = 24
HIGH_ATTENTION = 0.50  # above this, daily re-surfacing is allowed


def connect():
    is_ro = False
    try:
        con = sqlite3.connect(f"file:{DB}?mode=rw", uri=True, timeout=10)
    except sqlite3.OperationalError:
        try:
            con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
            is_ro = True
        except sqlite3.OperationalError:
            con = sqlite3.connect(f"file:{DB}?immutable=1", uri=True, timeout=10)
            is_ro = True
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=10000")
    if not is_ro and init_commitment_db is not None:
        try:
            init_commitment_db(con)
        except Exception as e:
            print(f"[event-layer] Warning: init_commitment_db failed: {e}", file=sys.stderr)
    return con


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def row_to_event(r):
    return {
        "event_id": r["event_id"],
        "type": r["type"],
        "subtype": r["subtype"],
        "summary": r["summary"],
        "actor": r["actor"],
        "status": r["status"],
        "due_at": r["due_at"],
        "due_precision": r["due_precision"],
        "attention": r["attention"],
        "confidence": r["confidence"],
        "importance": r["importance"],
        "urgency": r["urgency"],
        "actionability": r["actionability"],
        "needs_human_review": bool(r["needs_human_review"]),
        "project_id": r["project_id"],
        "supersedes": r["supersedes"],
        "superseded_by": r["superseded_by"],
        "source": {
            "type": r["source_type"],
            "source_id": r["source_id"],
            "timestamp": r["source_timestamp"],
            "conversation": r["source_conversation"],
            "speaker": r["source_speaker"],
        },
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
        "resolved_at": r["resolved_at"],
        "last_surfaced_at": r["last_surfaced_at"],
        "surface_count": r["surface_count"],
    }


def _list_open(args):
    include_all = bool(args.get("include_all"))
    limit = max(1, min(int(args.get("limit") or 20), 50))
    con = connect()
    try:
        if include_all:
            rows = con.execute(
                "SELECT * FROM events WHERE status IN ('open','confirmed') "
                "ORDER BY attention DESC, due_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = con.execute(
                """
                SELECT * FROM events
                WHERE status='open'
                  AND confidence >= ?
                  AND attention >= ?
                  AND type NOT IN ('social','informational')
                  AND (snoozed_until IS NULL OR snoozed_until < ?)
                  AND (last_surfaced_at IS NULL
                       OR last_surfaced_at < ?
                       OR attention >= ?)
                ORDER BY attention DESC, due_at ASC LIMIT ?
                """,
                (
                    MIN_CONFIDENCE,
                    MIN_ATTENTION,
                    datetime.now(timezone.utc).isoformat(),
                    (datetime.now(timezone.utc) - timedelta(hours=DUP_SUPPRESS_HOURS)).isoformat(),
                    HIGH_ATTENTION,
                    limit,
                ),
            ).fetchall()
    finally:
        con.close()
    events = [row_to_event(r) for r in rows]
    if not include_all and rows:
        try:
            con = connect()
            try:
                for r in rows:
                    con.execute(
                        "UPDATE events SET last_surfaced_at=?, surface_count=surface_count+1 "
                        "WHERE event_id=?",
                        (now_iso(), r["event_id"]),
                    )
                con.commit()
            finally:
                con.close()
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            pass
    return {
        "count": len(events),
        "events": events,
        "note": "Event summaries are derived from untrusted LINE history; "
        "verify provenance with line_get when needed.",
    }


def _get(args):
    eid = str(args.get("event_id") or "").strip()
    con = connect()
    try:
        r = con.execute("SELECT * FROM events WHERE event_id=?", (eid,)).fetchone()
    finally:
        con.close()
    if r is None:
        return {"error": "not_found", "event_id": eid}
    ev = row_to_event(r)
    ev["classification_reason"] = r["classification_reason"]
    return {"event": ev}


def _search(args):
    """Search events (open and closed) by optional filters. Summary-level only."""
    query = str(args.get("query") or "").strip()
    etype = str(args.get("type") or "").strip()
    status = str(args.get("status") or "").strip()
    actor = str(args.get("actor") or "").strip()
    project_id = str(args.get("project_id") or "").strip()
    after = str(args.get("after") or "").strip()
    before = str(args.get("before") or "").strip()
    limit = max(1, min(int(args.get("limit") or 20), 100))
    where, params = [], []
    if query:
        where.append("(summary LIKE ? OR classification_reason LIKE ?)")
        params.extend([f"%{query}%", f"%{query}%"])
    if etype:
        where.append("type=?"); params.append(etype)
    if status:
        where.append("status=?"); params.append(status)
    if actor:
        where.append("actor=?"); params.append(actor)
    if project_id:
        where.append("project_id=?"); params.append(project_id)
    if after:
        where.append("COALESCE(due_at, source_timestamp) >= ?"); params.append(after)
    if before:
        where.append("COALESCE(due_at, source_timestamp) <= ?"); params.append(before)
    sql = "SELECT * FROM events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY COALESCE(due_at, source_timestamp) DESC LIMIT ?"
    params.append(limit)
    con = connect()
    try:
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()
    events = [
        {
            "event_id": r["event_id"],
            "type": r["type"],
            "status": r["status"],
            "summary": r["summary"],
            "actor": r["actor"],
            "due_at": r["due_at"],
            "due_precision": r["due_precision"],
            "confidence": r["confidence"],
            "project_id": r["project_id"],
            "source": {"type": r["source_type"], "source_id": r["source_id"],
                       "timestamp": r["source_timestamp"]},
        }
        for r in rows
    ]
    return {
        "count": len(events),
        "events": events,
        "note": "Summaries derive from untrusted LINE history; "
        "use event_get + line_get for provenance.",
    }


def _transition(args, new_status, extra_sql="", extra_params=(), actor="system"):
    eid = str(args.get("event_id") or "").strip()
    con = connect()
    try:
        r = con.execute("SELECT status FROM events WHERE event_id=?", (eid,)).fetchone()
        if r is None:
            return {"error": "not_found", "event_id": eid}
        old = r["status"]
        now = now_iso()
        resolved_at = now if new_status == "resolved" else None
        con.execute(
            f"UPDATE events SET status=?, updated_at=?, resolved_at=COALESCE(?,resolved_at)"
            f"{extra_sql} WHERE event_id=?",
            (new_status, now, resolved_at) + extra_params + (eid,),
        )
        con.execute(
            "INSERT INTO event_history (event_id, changed_at, field, old_value, new_value, actor) "
            "VALUES (?,?,?,?,?,?)",
            (eid, now, "status", old, new_status, actor),
        )
        con.commit()
    finally:
        con.close()
    return {"event_id": eid, "status": new_status, "previous": old}


def _resolved(args):
    return _transition(args, "resolved", actor=str(args.get("by") or "hermes"))


def _ignored(args):
    return _transition(args, "ignored", actor=str(args.get("by") or "hermes"))


def _cancelled(args):
    return _transition(args, "cancelled", actor=str(args.get("by") or "hermes"))


PROJECTS_DIR = "/opt/data/workspace/projects"


def _link_project(args):
    eid = str(args.get("event_id") or "").strip()
    project_id = str(args.get("project_id") or "").strip()
    if not project_id or not os.path.isdir(os.path.join(PROJECTS_DIR, project_id)):
        return {"error": "unknown_project", "project_id": project_id}
    con = connect()
    try:
        r = con.execute("SELECT project_id FROM events WHERE event_id=?", (eid,)).fetchone()
        if r is None:
            return {"error": "not_found", "event_id": eid}
        old = r["project_id"]
        now = now_iso()
        con.execute(
            "UPDATE events SET project_id=?, updated_at=? WHERE event_id=?",
            (project_id, now, eid),
        )
        con.execute(
            "INSERT INTO event_history (event_id, changed_at, field, old_value, new_value, actor) "
            "VALUES (?,?,?,?,?,?)",
            (eid, now, "project_id", old, project_id, str(args.get("by") or "hermes")),
        )
        con.commit()
    finally:
        con.close()
    return {"event_id": eid, "project_id": project_id, "previous": old}


def _snooze(args):
    days = args.get("days")
    try:
        days = max(0.25, min(float(days), 30.0))
    except (TypeError, ValueError):
        return {"error": "days_required_number"}
    until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    return _transition(
        args, "open", extra_sql=", snoozed_until=?", extra_params=(until,),
        actor=str(args.get("by") or "hermes"),
    ) | {"snoozed_until": until}


def _status():
    con = connect()
    try:
        total = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        by_status = dict(
            con.execute("SELECT status, COUNT(*) FROM events GROUP BY status").fetchall()
        )
        by_type = dict(
            con.execute("SELECT type, COUNT(*) FROM events GROUP BY type").fetchall()
        )
        open_n = con.execute(
            "SELECT COUNT(*) FROM events WHERE status='open'"
        ).fetchone()[0]
        due_soon = con.execute(
            "SELECT COUNT(*) FROM events WHERE status='open' AND due_at IS NOT NULL "
            "AND due_at BETWEEN ? AND ?",
            (
                datetime.now(JST).isoformat(),
                (datetime.now(JST) + timedelta(days=2)).isoformat(),
            ),
        ).fetchone()[0]
        overdue = con.execute(
            "SELECT COUNT(*) FROM events WHERE status='open' AND due_at IS NOT NULL "
            "AND due_at < ?",
            (datetime.now(JST).isoformat(),),
        ).fetchone()[0]
        processed = con.execute("SELECT COUNT(*) FROM processed_messages").fetchone()[0]
        last_processed = con.execute(
            "SELECT MAX(processed_at) FROM processed_messages"
        ).fetchone()[0]
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    finally:
        con.close()
    return {
        "db_path": DB,
        "total_events": total,
        "by_status": by_status,
        "by_type": by_type,
        "open_events": open_n,
        "candidate_events": by_status.get("candidate", 0),
        "resolved_events": by_status.get("resolved", 0),
        "ignored_events": by_status.get("ignored", 0),
        "cancelled_events": by_status.get("cancelled", 0),
        "due_soon_48h": due_soon,
        "overdue": overdue,
        "messages_processed": processed,
        "last_processed_at": last_processed,
        "last_run": meta.get("last_run"),
        "last_run_metrics": {
            "candidates": meta.get("candidates"),
            "classified": meta.get("classified"),
            "llm_calls": meta.get("llm_calls"),
        },
        "classifier": meta.get("classifier_version"),
        "content_in_status": False,
        "write_scope": "events.db only",
    }


# ==============================================================================
# Phase I.1 — Unified Commitments & Agenda Layer Handlers
# ==============================================================================

def _commitment_list(args):
    if list_commitments is None:
        return {"error": "commitments_module_unavailable"}
    con = connect()
    try:
        resp = args.get("responsibility")
        state = args.get("work_state")
        temp = args.get("temporal_state")
        proj = args.get("project_id")
        limit = max(1, min(int(args.get("limit") or 20), 50))
        offset = max(0, int(args.get("offset") or 0))

        cmts = list_commitments(
            con,
            responsibility=resp,
            work_state=state,
            temporal_state=temp,
            project_id=proj,
            limit=limit,
            offset=offset,
        )
        return {
            "count": len(cmts),
            "commitments": [c.to_summary_dict() for c in cmts],
        }
    finally:
        con.close()


def _commitment_get(args):
    if get_commitment is None:
        return {"error": "commitments_module_unavailable"}
    cid = str(args.get("commitment_id") or "").strip()
    if not cid:
        return {"error": "commitment_id_required"}
    con = connect()
    try:
        cmt = get_commitment(con, cid)
        if cmt is None:
            return {"error": "not_found", "commitment_id": cid}
        return {"commitment": cmt.to_dict()}
    finally:
        con.close()


def _commitment_search(args):
    if search_commitments is None:
        return {"error": "commitments_module_unavailable"}
    query = str(args.get("query") or "").strip()
    limit = max(1, min(int(args.get("limit") or 20), 50))
    con = connect()
    try:
        cmts = search_commitments(con, query=query, limit=limit)
        return {
            "count": len(cmts),
            "commitments": [c.to_summary_dict() for c in cmts],
        }
    finally:
        con.close()


def _commitment_status():
    if get_commitment_status is None:
        return {"error": "commitments_module_unavailable"}
    con = connect()
    try:
        return get_commitment_status(con)
    finally:
        con.close()


def _agenda_get(args):
    if generate_agenda is None:
        return {"error": "agenda_module_unavailable"}
    days = max(1, min(int(args.get("window_days") or 7), 30))
    view = args.get("view")
    if not view and args.get("date"):
        view = "today"
    limit = int(args.get("limit")) if args.get("limit") is not None else None
    con = connect()
    try:
        return generate_agenda(con, window_days=days, view=view, limit_per_view=limit)
    finally:
        con.close()


# ==============================================================================
# Phase I.2 — Daily Plan & Morning Brief Handlers (READ-ONLY)
# ==============================================================================
# Both handlers delegate to planning.mcp_planning, which opens its own read-only
# connection (mode=ro + PRAGMA query_only) on EVENTS_DB. Nothing here writes,
# mutates state, or delivers a notification.

def _daily_plan_get(args):
    if planning_daily_plan_get is None:
        return {"error": "planning_module_unavailable"}
    return planning_daily_plan_get(args, db_path=DB)


def _morning_brief_get(args):
    if planning_morning_brief_get is None:
        return {"error": "planning_module_unavailable"}
    return planning_morning_brief_get(args, db_path=DB)


SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "include_all": {
            "type": "boolean",
            "description": "true = every open event (no surfacing filter); false = attention-surfaced only",
        },
        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
    },
}
ID_SCHEMA = {
    "type": "object",
    "properties": {
        "event_id": {"type": "string"},
        "by": {"type": "string", "description": "who requested the change (e.g. user)"},
    },
    "required": ["event_id"],
}
SEARCH_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "substring match on summary / classification reason"},
        "type": {"type": "string"},
        "status": {"type": "string", "description": "open|candidate|resolved|ignored|cancelled|superseded"},
        "actor": {"type": "string", "description": "user|counterparty"},
        "project_id": {"type": "string"},
        "after": {"type": "string", "description": "ISO; filter by due_at (else source_timestamp)"},
        "before": {"type": "string", "description": "ISO; filter by due_at (else source_timestamp)"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
    },
}
LINK_SCHEMA = {
    "type": "object",
    "properties": {
        "event_id": {"type": "string"},
        "project_id": {"type": "string"},
        "by": {"type": "string"},
    },
    "required": ["event_id", "project_id"],
}
SNOOZE_SCHEMA = {
    "type": "object",
    "properties": {
        "event_id": {"type": "string"},
        "days": {"type": "number", "minimum": 0.25, "maximum": 30, "default": 3},
    },
    "required": ["event_id"],
}
EMPTY_SCHEMA = {"type": "object", "properties": {}}

COMMITMENT_LIST_SCHEMA = {
    "type": "object",
    "properties": {
        "responsibility": {
            "type": "string",
            "enum": ["SELF", "COUNTERPARTY", "SHARED", "INFORMATIONAL"],
            "description": "Filter by responsibility (SELF, COUNTERPARTY, SHARED, INFORMATIONAL)",
        },
        "work_state": {
            "type": "string",
            "enum": ["ACTIONABLE", "WAITING", "SCHEDULED", "BLOCKED", "DONE", "CANCELLED", "DISMISSED"],
            "description": "Filter by work state",
        },
        "temporal_state": {
            "type": "string",
            "enum": ["OVERDUE", "DUE_SOON", "FUTURE", "NO_DATE"],
            "description": "Filter by dynamic temporal state",
        },
        "project_id": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
        "offset": {"type": "integer", "minimum": 0, "default": 0},
    },
}

COMMITMENT_GET_SCHEMA = {
    "type": "object",
    "properties": {
        "commitment_id": {"type": "string", "description": "Canonical commitment ID (e.g. cmt_...)"},
    },
    "required": ["commitment_id"],
}

COMMITMENT_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Search term across title, summary, counterparty, notes"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
    },
    "required": ["query"],
}

AGENDA_GET_SCHEMA = {
    "type": "object",
    "properties": {
        "view": {
            "type": "string",
            "enum": ["today", "soon", "overdue", "waiting_for", "scheduled", "all"],
            "description": "Filter agenda projection to a specific view (e.g. 'today', 'waiting_for', 'overdue', 'soon', 'scheduled'). Defaults to compact overview.",
        },
        "date": {
            "type": "string",
            "description": "Optional ISO date (e.g. '2026-09-06'). When provided, projects agenda for that specific date.",
        },
        "window_days": {"type": "integer", "minimum": 1, "maximum": 30, "default": 7, "description": "Upcoming window in days"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 25, "description": "Max items to return for the view"},
    },
}

DAILY_PLAN_GET_SCHEMA = {
    "type": "object",
    "properties": {
        "date": {
            "type": "string",
            "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
            "description": "Optional local date 'YYYY-MM-DD'. Defaults to today in `timezone`.",
        },
        "timezone": {
            "type": "string",
            "default": "Asia/Tokyo",
            "description": "IANA timezone used for the local-day boundaries. Defaults to Asia/Tokyo.",
        },
    },
}

MORNING_BRIEF_GET_SCHEMA = {
    "type": "object",
    "properties": {
        "date": {
            "type": "string",
            "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
            "description": "Optional local date 'YYYY-MM-DD'. Defaults to today in `timezone`.",
        },
        "timezone": {
            "type": "string",
            "default": "Asia/Tokyo",
            "description": "IANA timezone used for the local-day boundaries. Defaults to Asia/Tokyo.",
        },
        "mode": {
            "type": "string",
            "enum": ["auto", "template"],
            "default": "auto",
            "description": "'auto' = natural-language render with deterministic fallback; 'template' = deterministic only.",
        },
    },
}


async def _list_tools(ctx=None, params=None):
    return types.ListToolsResult(
        tools=[
            types.Tool(
                name="event_list_open",
                description=(
                    "List open personal events (commitments, deadlines, requests) that "
                    "warrant attention, attention-scored and conservatively filtered."
                ),
                inputSchema=SEARCH_SCHEMA,
            ),
            types.Tool(
                name="event_get",
                description="Get one event with full provenance by event_id.",
                inputSchema=ID_SCHEMA,
            ),
            types.Tool(
                name="event_search",
                description=(
                    "Search events, open and closed, by keyword and filters "
                    "(type, status, actor, project_id, date range)."
                ),
                inputSchema=SEARCH_INPUT_SCHEMA,
            ),
            types.Tool(
                name="event_mark_resolved",
                description="Mark an event resolved (e.g. the user said it is done).",
                inputSchema=ID_SCHEMA,
            ),
            types.Tool(
                name="event_mark_ignored",
                description="Mark an event as ignored (false positive / not worth tracking).",
                inputSchema=ID_SCHEMA,
            ),
            types.Tool(
                name="event_mark_cancelled",
                description="Mark an event cancelled: the planned item itself was called off.",
                inputSchema=ID_SCHEMA,
            ),
            types.Tool(
                name="event_link_project",
                description="Link an existing event to an existing project_id (validated).",
                inputSchema=LINK_SCHEMA,
            ),
            types.Tool(
                name="event_snooze",
                description="Snooze an event for N days (default 3); hidden from Morning Review until then.",
                inputSchema=SNOOZE_SCHEMA,
            ),
            types.Tool(
                name="event_status",
                description="Event layer statistics (counts only, no content).",
                inputSchema=EMPTY_SCHEMA,
            ),
            # --- Phase I.1 Tools ---
            types.Tool(
                name="commitment_list",
                description="List canonical commitments with multi-dimensional filtering (responsibility, work_state, temporal_state).",
                inputSchema=COMMITMENT_LIST_SCHEMA,
            ),
            types.Tool(
                name="commitment_get",
                description="Get a single canonical commitment with all linked multi-source evidence.",
                inputSchema=COMMITMENT_GET_SCHEMA,
            ),
            types.Tool(
                name="commitment_search",
                description="Search commitments across title, summary, and counterparty.",
                inputSchema=COMMITMENT_SEARCH_SCHEMA,
            ),
            types.Tool(
                name="commitment_status",
                description="Return high-level summary counts and statistics of commitments and evidence.",
                inputSchema=EMPTY_SCHEMA,
            ),
            types.Tool(
                name="agenda_get",
                description="Get Chief of Staff prioritized agenda projection: today, soon, overdue, waiting_for, and scheduled.",
                inputSchema=AGENDA_GET_SCHEMA,
            ),
            # --- Phase I.2 Tools (read-only) ---
            types.Tool(
                name="daily_plan_get",
                description=(
                    "PRIMARY tool for \"what should I do today\". Returns one day's deterministic plan: "
                    "what is scheduled today, what is due today, what is overdue, what is coming up next, "
                    "what is waiting on someone else, plus schedule conflicts, per-category counts and "
                    "source freshness. Use it for 今日何すればいい？ / 今日の予定まとめて / 何からやればいい？ / "
                    "今日の優先順位は？ / what should I do today / today's plan / what should I start with / "
                    "today's priorities. Prefer this over agenda_get whenever the question is about a single day. "
                    "Read-only projection over canonical commitments and calendar authority. "
                    "`local_time` is null whenever the stored deadline carries no real clock time "
                    "(any `due_precision` other than minute/exact_datetime, and every all-day row): "
                    "state the date alone in that case and never quote a time the plan did not give you."
                ),
                inputSchema=DAILY_PLAN_GET_SCHEMA,
            ),
            types.Tool(
                name="morning_brief_get",
                description=(
                    "The same day plan as daily_plan_get, already rendered as a short Japanese brief "
                    "(今日の予定 / 最優先 / 期限超過 / 次に見るもの / 返事待ち / 注意点). Use it when the user wants "
                    "the day summarised as prose to read or forward (朝のブリーフ / 今日のまとめ / morning brief); "
                    "use daily_plan_get instead when you need to reason over the individual items. Read-only; "
                    "renders from the compact plan only, never from raw source messages."
                ),
                inputSchema=MORNING_BRIEF_GET_SCHEMA,
            ),
        ]
    )


async def _call_tool(ctx=None, params=None):
    name = params.name
    arguments = params.arguments or {}
    t0 = time.monotonic()
    try:
        if name == "event_list_open":
            out = _list_open(arguments)
        elif name == "event_get":
            out = _get(arguments)
        elif name == "event_search":
            out = _search(arguments)
        elif name == "event_mark_resolved":
            out = _resolved(arguments)
        elif name == "event_mark_ignored":
            out = _ignored(arguments)
        elif name == "event_mark_cancelled":
            out = _cancelled(arguments)
        elif name == "event_link_project":
            out = _link_project(arguments)
        elif name == "event_snooze":
            out = _snooze(arguments)
        elif name == "event_status":
            out = _status()
        elif name == "commitment_list":
            out = _commitment_list(arguments)
        elif name == "commitment_get":
            out = _commitment_get(arguments)
        elif name == "commitment_search":
            out = _commitment_search(arguments)
        elif name == "commitment_status":
            out = _commitment_status()
        elif name == "agenda_get":
            out = _agenda_get(arguments)
        elif name == "daily_plan_get":
            out = _daily_plan_get(arguments)
        elif name == "morning_brief_get":
            out = _morning_brief_get(arguments)
        else:
            out = {"error": f"unknown_tool:{name}"}
    except Exception as exc:
        out = {"error": f"internal:{type(exc).__name__}"}
    ms = int((time.monotonic() - t0) * 1000)
    print(f"[event-layer] tool={name} ms={ms}", file=sys.stderr)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(out, ensure_ascii=False))]
    )


server = Server(
    "event-layer",
    on_list_tools=_list_tools,
    on_call_tool=_call_tool,
) if Server is not None else None


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
