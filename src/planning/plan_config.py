"""Configuration defaults and loading for the planning layer.

`DEFAULTS` is the single source of truth for every tunable in Phase I.2. The
values are not guesses: each one is either fixed by I2-CONTRACT.md section 2 or
by AMENDMENT 1, which replaced several of them with numbers measured against
the real production database (I2-PROFILE.md sections F and I).

Where AMENDMENT 1 and the contract disagree, AMENDMENT 1 wins:

* ``morning_brief.time`` is ``08:30``, not ``08:00`` — a user-configured morning
  slot already exists at 08:30 JST and phase section 53 says preserve it. It
  also lands *after* the attention engine's ~08:04 catch-up digest.
* ``limits`` come from AMENDMENT 1 section F: ``scheduled`` drops 8 → 5 (the
  calendar has never produced more than 2 on one day) and ``waiting_for`` rises
  5 → 7 (the entire production waiting set is 7, so no "+N more" is needed).
* ``overdue_per_counterparty_cap`` is new: without it the same five stale items
  from one Discord thread appear every morning forever (phase section 12).

`freshness_sources` is an additive block the contract does not enumerate; it
holds the read-only paths I2-PROFILE.md section G verified are readable without
root. Keeping them here rather than under ``paths`` means anything already
reading ``paths`` keeps working.

Phase I.2 integration (T6) reconciled three keys that were live in one module
but absent here, so that this really is the single source of truth the contract
claims: ``morning_brief.notify_on_failure`` and
``morning_brief.socket_timeout_seconds`` (read by ``brief_service``, shipped in
``config/planning.sample.yaml``) and ``renderer.llm.enabled`` (read by
``renderer.resolve_client``). All three keep the value their consumer already
defaulted to, so behaviour is unchanged; what changes is that
``load_config()`` on a host with no config file now returns them, and
``tests-planning/test_integration_pipeline.py`` pins the three sources —
``DEFAULTS``, the sample YAML, and the consuming modules — against each other.

Config file on the Pi: ``/srv/hermes/planning/config.yaml``
(env override: ``HERMES_PLANNING_CONFIG``).
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any, Dict, Optional

DEFAULT_CONFIG_PATH = "/srv/hermes/planning/config.yaml"
CONFIG_ENV_VAR = "HERMES_PLANNING_CONFIG"


DEFAULTS: Dict[str, Any] = {
    "timezone": "Asia/Tokyo",
    "morning_brief": {
        # AMENDMENT 1 A1: 08:30 JST, the owner's existing morning slot, and
        # after the attention engine's post-quiet-hours catch-up digest.
        "time": "08:30",
        "late_boot_grace_hours": 3,
        # The other side of the section 56 window. `Persistent=true` fires a
        # missed elapse at boot, whatever hour that is; without a lower bound a
        # 02:00 boot pushed the day's brief to Discord at 02:00 and consumed
        # `morning-brief:YYYY-MM-DD`, so the real 08:30 run then answered
        # `suppressed_duplicate`. Large enough for clock skew, far too small
        # for any plausible boot hour. `brief_service` reads this.
        "early_tolerance_seconds": 300,
        # Phase section 61 / G.2 policy: LINE is scarce escalation only.
        "channel": "discord",
        "enabled": True,
        # Phase section 60 / 126. Off by default: a brief that cannot be built
        # usually stays broken for days, and a daily "it broke" push is the
        # notification flood section 126 forbids. `brief_service` reads this.
        "notify_on_failure": False,
        # Seconds to wait on the gateway's unix socket. `brief_service` reads
        # this when it constructs the default `delivery.DeliveryClient`.
        "socket_timeout_seconds": 30,
    },
    "limits": {
        # AMENDMENT 1 section F, measured against production supply.
        "scheduled": 5,
        "must_do": 5,
        "overdue": 5,
        "next_up": 5,
        "waiting_for": 7,
        "backlog": 3,
    },
    # At most this many overdue items per counterparty inside the shown set,
    # so one stalled thread cannot own the whole overdue list.
    "overdue_per_counterparty_cap": 2,
    "windows": {
        "next_up_hours": 72,
        "imminent_hours": 3,
        "followup_days": 7,
        # Production has 0 rows with a NULL scheduled_end_at (I2-PROFILE.md
        # section E.2); this is a defensive fallback, not the primary path.
        "conflict_default_duration_minutes": 60,
        "dense_window_minutes": 90,
    },
    "freshness_thresholds_seconds": {
        "calendar": 86400,
        "gmail": 86400,
        # 48h matches the attention engine's health.sustained_seconds
        # .line_refresh threshold, so the two surfaces cannot disagree.
        "line": 172800,
        "discord": 172800,
        "commitments": 86400,
    },
    "paths": {
        "events_db": "/srv/hermes/hermes-data/events/events.db",
        "planning_dir": "/srv/hermes/hermes-data/planning",
        "gateway_proactive_socket": "/srv/hermes/channel-gateway/run/proactive.sock",
    },
    # Additive: the read-only freshness inputs verified in I2-PROFILE.md G.
    # An empty string disables a probe rather than inventing a source.
    "freshness_sources": {
        "calendar_index_db": "/srv/hermes/calendar/index/index.db",
        "gmail_status_json": "/srv/hermes/gmail/index/status.json",
        "gmail_index_db": "/srv/hermes/gmail/index/index.db",
        "line_reconcile_health_json": "/srv/hermes/hermes-data/events/line-reconcile-health.json",
        "line_index_db": "/srv/hermes/hermes-data/knowledge-index/line/index.db",
        "discord_collector_db": "/srv/hermes/knowledge/discord/messages.db",
    },
    "renderer": {
        "mode": "auto",
        "timeout_seconds": 30,
        # The gateway's MAX_NOTIFICATION_CHARS.
        "max_chars": 1200,
        # The scheduled brief ships TEMPLATE-ONLY (coordinator ruling on T3's
        # open question). `renderer.resolve_client` returns None while this is
        # false, so `mode: auto` degrades to the deterministic template without
        # a network socket or a token. Turning it on also requires widening
        # `RestrictAddressFamilies` in the unit — see the header of
        # `systemd/hermes-morning-brief.service.in` before doing so.
        "llm": {"enabled": False},
    },
}


class ConfigError(ValueError):
    """Raised for malformed configuration content."""


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge `overlay` into a copy of `base`."""
    out = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _parse(text: str, path: str) -> Dict[str, Any]:
    """Parse YAML (preferred) or JSON. An empty file means 'no overrides'."""
    if not text.strip():
        return {}
    data: Any
    try:
        import yaml  # PyYAML is already a dependency of every other service here.
    except ImportError:  # pragma: no cover - PyYAML is present on the Pi
        yaml = None
    if yaml is not None:
        try:
            data = yaml.safe_load(text)
        except Exception as exc:
            raise ConfigError(f"malformed planning config at {path}: {exc}") from exc
    else:
        try:
            data = json.loads(text)
        except Exception as exc:
            raise ConfigError(f"malformed planning config at {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(
            f"planning config at {path} must be a mapping, got {type(data).__name__}"
        )
    return data


def resolve_config_path(path: Optional[str] = None) -> str:
    """Explicit argument wins, then the env override, then the Pi default."""
    if path:
        return path
    env_path = os.environ.get(CONFIG_ENV_VAR)
    if env_path:
        return env_path
    return DEFAULT_CONFIG_PATH


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """Deep-merge the YAML/JSON at `path` over :data:`DEFAULTS`.

    A missing file is not an error — the defaults are the shipped configuration
    and a planner that refuses to start because an optional override file is
    absent is a worse failure than one that runs with its documented defaults.
    Malformed *content*, on the other hand, raises :class:`ConfigError` (a
    ``ValueError``): a half-parsed config silently ignoring an operator's
    change is exactly the failure the gateway and attention configs avoid.
    """
    resolved = resolve_config_path(path)
    try:
        with open(resolved, "r", encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return copy.deepcopy(DEFAULTS)
    except IsADirectoryError as exc:
        raise ConfigError(f"planning config path is a directory: {resolved}") from exc
    except OSError:
        # Unreadable (permissions, transient IO): fall back to defaults rather
        # than taking the morning brief down over an optional file.
        return copy.deepcopy(DEFAULTS)
    return _deep_merge(DEFAULTS, _parse(text, resolved))


def effective(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return `config` merged over the defaults; `None` yields the defaults.

    Every module here calls this instead of trusting a caller-supplied dict to
    be complete, so a partial config from a test or an MCP call cannot raise a
    KeyError deep inside the planner.
    """
    if not config:
        return copy.deepcopy(DEFAULTS)
    return _deep_merge(DEFAULTS, config)
