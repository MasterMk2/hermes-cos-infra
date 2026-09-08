"""Source freshness collection for the daily plan (phase sections 36, 37).

Answers one question per source: *when did this pipeline last succeed, and is
that long enough ago that the morning brief should say so?*

Design constraints that shaped this module:

* **It never raises.** A brief built on four fresh sources and one unreadable
  one is far more useful than no brief at all (phase section 37), so every
  probe is wrapped and an unreadable source becomes
  ``{"last_success_at": None, "age_seconds": None, "stale": True, "error": …}``.
* **Read-only, no root.** Every path here was verified readable by an
  unprivileged identity in I2-PROFILE.md section G. Nothing is opened for
  writing and the SQLite probes use ``mode=ro`` URIs.
* **No new source is invented.** An empty configured path disables its probe
  and reports ``not_configured`` rather than guessing a location.

`last_success_at` is reported **verbatim** from the source. The sources
disagree about format (``Z`` on the LINE health file, ``+00:00`` on the Gmail
status file), and rewriting them here would hide which surface a value came
from. Only `age_seconds` is derived.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from . import plan_config

#: The five sources phase section 36 names, in output order.
SOURCE_ORDER: tuple = ("calendar", "gmail", "line", "discord", "commitments")


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp; naive values are read as UTC.

    Mirrors ``commitments.models.parse_iso`` deliberately: the freshness
    surfaces are separate files and DBs from the canonical store, and this
    module must stay importable even where that package is not on the path.
    None of these sources emits a bare ``YYYY-MM-DD`` (I2-PROFILE.md M.1), so
    the all-day branch the planner needs is not needed here.
    """
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _entry(
    last_success_at: Optional[str],
    threshold_seconds: int,
    now: datetime,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    """Build one `source_freshness` entry."""
    parsed = _parse_ts(last_success_at)
    if parsed is None:
        return {
            "last_success_at": None,
            "age_seconds": None,
            "stale": True,
            "threshold_seconds": int(threshold_seconds),
            "error": error or ("unparsable_timestamp" if last_success_at else "no_timestamp"),
        }
    # A source timestamp ahead of `now` means clock skew, not negative age.
    age = max(0, int((now - parsed).total_seconds()))
    entry: Dict[str, Any] = {
        "last_success_at": last_success_at,
        "age_seconds": age,
        "stale": age > int(threshold_seconds),
        "threshold_seconds": int(threshold_seconds),
    }
    if error:
        entry["error"] = error
    return entry


def _read_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    return data


def _query_one(path: str, sql: str, params: tuple = ()) -> Optional[str]:
    """Run a single-value read-only query against a SQLite file."""
    if not path or not os.path.exists(path):
        raise FileNotFoundError(path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        row = conn.execute(sql, params).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    value = row[0]
    return value if value is None or isinstance(value, str) else str(value)


def _first(mapping: Dict[str, Any], *keys: str) -> Optional[str]:
    """First non-empty string value among `keys`."""
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _short_error(exc: BaseException) -> str:
    """A short, non-leaking reason string. Never includes a full path."""
    if isinstance(exc, FileNotFoundError):
        return "source_missing"
    if isinstance(exc, PermissionError):
        return "permission_denied"
    if isinstance(exc, sqlite3.Error):
        return f"sqlite_error: {type(exc).__name__}"
    if isinstance(exc, (json.JSONDecodeError, ValueError)):
        return "malformed_source"
    return f"unreadable: {type(exc).__name__}"


# --------------------------------------------------------------------------
# Probes. Each returns the raw `last_success_at` string, or raises.
# --------------------------------------------------------------------------


def _probe_calendar(sources: Dict[str, Any]) -> Optional[str]:
    path = sources.get("calendar_index_db") or ""
    if not path:
        raise FileNotFoundError(path)
    try:
        value = _query_one(path, "SELECT last_api_success_at FROM calendar_status LIMIT 1")
    except sqlite3.Error:
        value = None
    if value:
        return value
    return _query_one(path, "SELECT value FROM meta WHERE key = ?", ("published_at",))


def _probe_gmail(sources: Dict[str, Any]) -> Optional[str]:
    json_path = sources.get("gmail_status_json") or ""
    if json_path and os.path.exists(json_path):
        try:
            data = _read_json(json_path)
        except Exception:
            data = {}
        value = _first(data, "last_api_success_at", "last_incremental_sync_at", "published_at")
        if value:
            return value
    db_path = sources.get("gmail_index_db") or ""
    if not db_path:
        raise FileNotFoundError(json_path or db_path)
    return _query_one(
        db_path, "SELECT last_api_success_at FROM gmail_status WHERE singleton = 1"
    )


def _probe_line(sources: Dict[str, Any]) -> Optional[str]:
    health_path = sources.get("line_reconcile_health_json") or ""
    if health_path and os.path.exists(health_path):
        try:
            data = _read_json(health_path)
        except Exception:
            data = {}
        value = _first(data, "index_timestamp", "last_finished_at", "updated_at")
        if value:
            return value
    index_path = sources.get("line_index_db") or ""
    if not index_path:
        raise FileNotFoundError(health_path or index_path)
    return _query_one(index_path, "SELECT value FROM meta WHERE key = ?", ("last_indexed",))


def _probe_discord(sources: Dict[str, Any], events_db: str) -> Optional[str]:
    """Collector heartbeat first; the classifier's own Discord run second.

    The primary source is unreadable to `hermesplanner` in production, and by
    design: `/srv/hermes/knowledge/discord` is mode 2770 owned by the operator's
    login group, and I2-AMENDMENT G5 deliberately refuses to put a service
    identity in that group. The directory is not even traversable, so
    `os.path.exists` is False and the probe reported `source_missing` on every
    single run — which made `data_freshness_warning` permanent and put a
    「情報源の更新が止まっています: discord」 line in every brief forever. A
    warning that is always on is a warning the owner stops reading, and it is
    also wrong: the Discord pipeline is fresh.

    So this probe now has the fallback every other probe here already had.
    `events.db` `meta.discord_last_run` is written by the Discord classifier
    pass at the end of each run, and it is read through the connection the
    planner already holds — no new path, no new group, no new permission.
    It is a slightly weaker signal than the collector heartbeat (it says the
    Discord ingest ran, not that the gateway socket is currently connected),
    which is the right trade for section 37: it answers "is Discord-derived
    data fresh enough to plan from", which is the only question the brief asks.
    """
    path = sources.get("discord_collector_db") or ""
    primary_failure: Optional[BaseException] = None
    if path:
        try:
            # `updated_at` is the collector's liveness heartbeat (I2-PROFILE.md G.4).
            value = _query_one(
                path, "SELECT value FROM ingest_state WHERE key = ?", ("updated_at",))
            if value:
                return value
            value = _query_one(
                path, "SELECT value FROM ingest_state WHERE key = ?",
                ("last_successful_ingest_at",))
            if value:
                return value
        except (OSError, sqlite3.Error) as exc:
            primary_failure = exc
    if not events_db:
        # Nothing to fall back to, so report why the PRIMARY failed rather than
        # a bare `source_missing` about a path that was configured and present.
        # A swallowed `sqlite_error: DatabaseError` is the difference between
        # "the collector store is corrupt" and "nobody configured Discord".
        raise primary_failure or FileNotFoundError(path or events_db)
    return _query_one(events_db, "SELECT value FROM meta WHERE key = ?", ("discord_last_run",))


def _probe_commitments(sources: Dict[str, Any], events_db: str) -> Optional[str]:
    health_path = sources.get("line_reconcile_health_json") or ""
    if health_path and os.path.exists(health_path):
        try:
            data = _read_json(health_path)
        except Exception:
            data = {}
        value = _first(data, "last_commitment_reconcile", "last_finished_at")
        if value:
            return value
    if not events_db:
        raise FileNotFoundError(health_path or events_db)
    try:
        value = _query_one(events_db, "SELECT value FROM meta WHERE key = ?", ("last_run",))
    except sqlite3.Error:
        value = None
    if value:
        return value
    return _query_one(events_db, "SELECT MAX(updated_at) FROM commitments")


_PROBES: Dict[str, Any] = {
    "calendar": _probe_calendar,
    "gmail": _probe_gmail,
    "line": _probe_line,
}

#: Probes whose fallback reads `events.db`, which the planner already has open
#: read-only. They take `(sources, events_db)` instead of `(sources,)`.
_EVENTS_DB_PROBES: Dict[str, Any] = {
    "commitments": _probe_commitments,
    "discord": _probe_discord,
}


def collect(config: Dict[str, Any], *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Return the `source_freshness` block. Never raises.

    An unreadable source yields
    ``{"last_success_at": None, "age_seconds": None, "stale": True,
       "threshold_seconds": <n>, "error": "<short reason>"}``
    and the plan is still produced (phase section 37); the caller turns any
    ``stale`` entry into the ``data_freshness_warning`` warning.
    """
    cfg = plan_config.effective(config)
    sources = cfg.get("freshness_sources") or {}
    thresholds = cfg.get("freshness_thresholds_seconds") or {}
    events_db = (cfg.get("paths") or {}).get("events_db") or ""
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    out: Dict[str, Any] = {}
    for name in SOURCE_ORDER:
        threshold = int(thresholds.get(name, 86400))
        try:
            if name in _EVENTS_DB_PROBES:
                value = _EVENTS_DB_PROBES[name](sources, events_db)
            else:
                value = _PROBES[name](sources)
        except Exception as exc:  # never raises: phase section 37
            out[name] = _entry(None, threshold, now, error=_short_error(exc))
            continue
        out[name] = _entry(value, threshold, now)
    return out


def any_stale(source_freshness: Dict[str, Any]) -> bool:
    """True when at least one source is stale or unreadable."""
    return any(bool(entry.get("stale")) for entry in (source_freshness or {}).values())
