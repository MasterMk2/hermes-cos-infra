#!/usr/bin/env python3
"""Attention Engine — configuration.

Carries no credential and cannot: the engine has no token to configure. The
LINE channel access token stays in the gateway (section 48), and the only
address the engine knows is a unix socket path.

Unknown keys are an error rather than a warning, for the same reason they are
in the gateway: a misspelled `line_escalation_enable` that is silently ignored
leaves an operator believing they turned something off. Every default here is
the conservative one — section 106's "safe initial defaults" — so a
half-configured engine is quiet rather than noisy.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from att_time import Clock, QuietHours

DEFAULT_CONFIG_PATH = "/srv/hermes/attention/config/config.yaml"


class ConfigError(ValueError):
    """Raised when the engine must not start."""


DEFAULT_PATHS = {
    "state_db": "/srv/hermes/attention/state/attention.db",
    "log_dir": "/srv/hermes/attention/logs",
}

DEFAULT_SOURCES = {
    "events_db": "/srv/hermes/hermes-data/events/events.db",
    "jobs_db": "/srv/hermes/hermes-data/jobs/jobs.db",
    "discord_collector_db": "/srv/hermes/knowledge/discord/messages.db",
    # The Hermes cron job that refreshes the LINE knowledge index writes one
    # markdown file per run here. Empty disables that probe rather than
    # inventing a source (section 14).
    "line_refresh_output_dir": "",
    "hermes_health_url": "",
    "coding_bridge_socket": "",
    "disk_path": "/srv/hermes",
    # Calendar is read from its published no-WAL index.  Attention does not
    # receive the Calendar service's canonical DB or OAuth credentials.
    "calendar_index_db": "/srv/hermes/calendar/index/index.db",
}

DEFAULT_GATEWAY = {
    "socket_path": "/srv/hermes/channel-gateway/run/proactive.sock",
    "timeout_seconds": 30,
    # A LINE push through the gateway can take a few seconds; a Discord DM is
    # usually under one. This is the ceiling before the engine treats the
    # outcome as UNKNOWN — which is deliberately NOT the same as failed
    # (section 86).
}

DEFAULT_SCHEDULE = {
    "scan_interval_seconds": 300,
    "timezone": "Asia/Tokyo",
    # Where the deferred backlog is released. Set to the end of quiet hours by
    # default so it lands before the existing chief-morning-review rather than
    # competing with it (section 58).
    "catchup_minute_of_hour": 0,
}

DEFAULT_QUIET_HOURS = {
    "enabled": True,
    "start": "23:00",
    "end": "08:00",
}

DEFAULT_CHANNELS = {
    # Discord is the default proactive channel: delivery there is not metered,
    # and the interface bot is already isolated to owner DMs (section 11).
    "default_channel": "discord",
    "line_escalation_enabled": True,
    # LINE is an escalation channel, never a feed (section 10). Turning this
    # on would let INFO and IMPORTANT spend metered pushes.
    "routine_line_enabled": False,
    "discord_enabled": True,
}

DEFAULT_NOTIFICATIONS = {
    "coding_completion_notifications": True,
    "health_notifications": True,
    "event_notifications": True,
    "calendar_notifications": True,
    "daily_digest_enabled": True,
    # "特に何もありません" every morning is a notification that trains the
    # reader to ignore the channel (section 61).
    "empty_digest_enabled": False,
    # Section 97: a bounded burst. Everything past it becomes a digest rather
    # than being dropped.
    "max_immediate_per_cycle": 3,
    "digest_max_items": 8,
    # Section 64. One dedupe key may not produce a second notification inside
    # this window; a genuinely new reminder uses a different key (a different
    # due window), not a shorter cooldown.
    "cooldown_seconds": 21600,
    "health_cooldown_seconds": 21600,
    "max_delivery_attempts": 3,
}

DEFAULT_EVENTS = {
    # Hours before the deadline at which a reminder becomes due. Not every
    # event uses every window — section 23: the window is chosen by how
    # actionable and how urgent the event is, so a low-stakes commitment gets
    # one reminder rather than three.
    "due_windows_hours": [24, 3, 0.5],
    "min_confidence_notify": 0.7,
    "min_actionability_notify": 0.5,
    "min_confidence_digest": 0.5,
    # An event first observed more than this long after it happened is
    # history that was backfilled, not news (section 25).
    "max_source_age_days": 3,
    "overdue_grace_minutes": 30,
    # Overdue is announced once, not once per scan for the rest of time.
    "overdue_max_age_days": 7,
    "statuses_open": ["open"],
    "statuses_ignored": ["resolved", "ignored", "cancelled", "superseded",
                         "done", "closed"],
}

DEFAULT_CALENDAR = {
    "enabled": True,
    # Routine calendar reminders stay with Google Calendar. Hermes only raises
    # an approaching event when the owner deliberately configures a keyword.
    "upcoming_hours": 24,
    "important_keywords": [],
}

DEFAULT_CODING = {
    "notify_statuses": ["COMPLETED", "WAITING_HUMAN"],
    # Section 29: everything else is an internal transition. Listing the ones
    # that are explicitly NOT notifications makes the intent reviewable.
    "internal_statuses": ["NEW", "RUNNING", "AGENT_STOPPED", "VALIDATING",
                          "WAITING_REVIEW", "REVIEWING",
                          "IMPLEMENTATION_ACCEPTED", "CANCELLED"],
    "max_age_hours": 48,
}

DEFAULT_HEALTH = {
    "enabled": True,
    # Section 37: how long a service must stay degraded before it is worth
    # telling anyone. One reconnect is not an incident (section 35).
    "sustained_seconds": {
        "gateway": 900,
        "discord_collector": 1800,
        "line_refresh": 172800,
        # The job reports its own outcome; two consecutive failures is already
        # the evidence, so this only bounds how quickly it is acted on.
        "line_refresh:script_failed": 3600,
        "coding_supervisor": 1800,
        "hermes_api": 900,
        "disk": 3600,
        "calendar": 1800,
        "calendar:auth_required": 1800,
        "calendar:watch_expired": 3600,
        "calendar:sync_stuck": 1800,
    },
    "queue_stuck_seconds": 900,
    "collector_heartbeat_seconds": 600,
    "disk_warn_percent": 10,
    "disk_critical_percent": 5,
    "recovery_notifications": True,
}

DEFAULT_LINE_BUDGET = {
    # Section 7. Fractions of the monthly target, not absolute counts: the
    # target is read from LINE and must never be hardcoded (section 6).
    "healthy_above": 0.5,
    "conserve_above": 0.2,
    # Section 101. This slice of the month's allowance is not available to
    # routine traffic at all; only URGENT and CRITICAL may draw on it.
    "emergency_reserve_fraction": 0.25,
    # Section 100. Spend at most this share of "remaining budget divided by
    # remaining days" on any one day, so an unusual day cannot eat the week.
    "daily_spend_factor": 0.7,
    "min_daily_allowance": 1,
    # Section 8. With no quota reading at all, LINE is for emergencies only —
    # and even then, bounded.
    "unknown_max_per_day": 1,
    "max_per_day_hard_cap": 6,
}

DEFAULT_FIRST_RUN = {
    # Everything that already existed at install is history. One digest that
    # says what is outstanding is useful; thirteen separate pushes about
    # deadlines that passed last week are not (sections 25, 96).
    "baseline_digest": True,
    "baseline_max_items": 8,
}

DEFAULT_LLM = {
    # Off. Section 73 requires a deterministic fallback for every important
    # notification anyway, so the templates have to exist and be correct; once
    # they do, an LLM in the path buys wording and costs a dependency, a
    # failure mode and a token bill on a five-minute schedule (sections 19,
    # 21). Shipping it off makes "no signals means zero LLM calls" a property
    # of the build rather than a claim about a code path.
    "enabled": False,
}

SECTION_DEFAULTS = {
    "paths": DEFAULT_PATHS,
    "sources": DEFAULT_SOURCES,
    "gateway": DEFAULT_GATEWAY,
    "schedule": DEFAULT_SCHEDULE,
    "quiet_hours": DEFAULT_QUIET_HOURS,
    "channels": DEFAULT_CHANNELS,
    "notifications": DEFAULT_NOTIFICATIONS,
    "events": DEFAULT_EVENTS,
    "calendar": DEFAULT_CALENDAR,
    "coding": DEFAULT_CODING,
    "health": DEFAULT_HEALTH,
    "line_budget": DEFAULT_LINE_BUDGET,
    "first_run": DEFAULT_FIRST_RUN,
    "llm": DEFAULT_LLM,
}


def _merge(section, defaults, what) -> dict:
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise ConfigError(f"{what} must be a mapping")
    unknown = set(section) - set(defaults)
    if unknown:
        raise ConfigError(f"{what} has unknown keys: {sorted(unknown)}")
    out = dict(defaults)
    for key, value in section.items():
        if isinstance(value, dict) and isinstance(defaults.get(key), dict):
            merged = dict(defaults[key])
            merged.update(value)
            out[key] = merged
        else:
            out[key] = value
    return out


@dataclass(frozen=True)
class Config:
    path: str
    paths: dict
    sources: dict
    gateway: dict
    schedule: dict
    quiet_hours: dict
    channels: dict
    notifications: dict
    events: dict
    calendar: dict
    coding: dict
    health: dict
    line_budget: dict
    first_run: dict
    llm: dict
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def clock(self) -> Clock:
        return Clock(tz_name=str(self.schedule["timezone"]))

    @property
    def quiet(self) -> QuietHours:
        return QuietHours.parse(
            self.quiet_hours["start"], self.quiet_hours["end"],
            self.quiet_hours.get("enabled", True),
        )

    @property
    def line_escalation(self) -> bool:
        return bool(self.channels.get("line_escalation_enabled"))

    def sustained_seconds(self, service: str, reason: str = "") -> int:
        """How long a failure must last before it is worth reporting.

        Looked up as "service:reason" first, then "service". The override
        exists because one threshold per service is the wrong shape: the LINE
        refresh job going quiet is only meaningful after two days, while the
        same job reporting its own failure twice running is meaningful within
        the hour.
        """
        table = self.health.get("sustained_seconds") or {}
        if reason and f"{service}:{reason}" in table:
            return int(table[f"{service}:{reason}"])
        return int(table.get(service, 1800))


def parse_config(data: dict, path: str = "<memory>") -> Config:
    if not isinstance(data, dict):
        raise ConfigError("configuration root must be a mapping")

    # The engine has no credential, so a key that looks like one is either a
    # misunderstanding of the design or an attempt to give it authority it
    # must not have (sections 48, 111). Refuse to start either way.
    for forbidden in ("token", "access_token", "channel_secret", "api_key",
                      "secrets", "line_channel_access_token",
                      "discord_token", "owner"):
        if forbidden in data:
            raise ConfigError(
                f"the attention engine must hold no credential and no "
                f"recipient (found {forbidden!r}); the gateway owns both"
            )

    unknown = set(data) - set(SECTION_DEFAULTS)
    if unknown:
        raise ConfigError(f"unknown top-level sections: {sorted(unknown)}")

    merged = {name: _merge(data.get(name), defaults, name)
              for name, defaults in SECTION_DEFAULTS.items()}

    sched = merged["schedule"]
    if not (60 <= int(sched["scan_interval_seconds"]) <= 3600):
        raise ConfigError(
            "schedule.scan_interval_seconds must be 60..3600; a scan every "
            "minute is the write amplification section 138 warns about, and "
            "an hourly scan cannot honour a 30-minute due window"
        )
    # Fails now, with a clear message, rather than at the first quiet-hours
    # evaluation six hours after deployment.
    QuietHours.parse(merged["quiet_hours"]["start"],
                     merged["quiet_hours"]["end"])
    Clock(tz_name=str(sched["timezone"])).local()

    ch = merged["channels"]
    if ch["default_channel"] not in ("discord", "line"):
        raise ConfigError("channels.default_channel must be discord or line")
    if ch["default_channel"] == "line":
        raise ConfigError(
            "channels.default_channel must not be line: LINE is metered and "
            "is an escalation channel, never the routine feed (section 10)"
        )

    lb = merged["line_budget"]
    if not (0.0 <= float(lb["emergency_reserve_fraction"]) < 1.0):
        raise ConfigError("line_budget.emergency_reserve_fraction must be 0..1")
    if not (0.0 < float(lb["daily_spend_factor"]) <= 1.0):
        raise ConfigError("line_budget.daily_spend_factor must be >0 and <=1")
    if float(lb["conserve_above"]) >= float(lb["healthy_above"]):
        raise ConfigError(
            "line_budget.conserve_above must be below healthy_above"
        )

    ev = merged["events"]
    windows = ev["due_windows_hours"]
    if not isinstance(windows, list) or not windows:
        raise ConfigError("events.due_windows_hours must be a non-empty list")
    try:
        windows = sorted({float(w) for w in windows}, reverse=True)
    except (TypeError, ValueError) as exc:
        raise ConfigError("events.due_windows_hours must be numbers") from exc
    if any(w <= 0 for w in windows):
        raise ConfigError("events.due_windows_hours must be positive")
    ev["due_windows_hours"] = windows

    cal = merged["calendar"]
    if not (1 <= int(cal["upcoming_hours"]) <= 168):
        raise ConfigError("calendar.upcoming_hours must be 1..168")
    if not isinstance(cal["important_keywords"], list) or any(
            not isinstance(item, str) for item in cal["important_keywords"]):
        raise ConfigError("calendar.important_keywords must be a list of strings")

    n = merged["notifications"]
    if not (1 <= int(n["max_immediate_per_cycle"]) <= 10):
        raise ConfigError(
            "notifications.max_immediate_per_cycle must be 1..10 (section 97)"
        )

    gw = merged["gateway"]
    if not str(gw["socket_path"]).startswith("/"):
        raise ConfigError("gateway.socket_path must be an absolute path")

    return Config(path=str(path), raw=data, **merged)


def load_config(path=None) -> Config:
    p = Path(path or os.environ.get("ATTENTION_CONFIG") or DEFAULT_CONFIG_PATH)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {p}: {exc}") from exc
    return parse_config(yaml.safe_load(text) or {}, path=str(p))
