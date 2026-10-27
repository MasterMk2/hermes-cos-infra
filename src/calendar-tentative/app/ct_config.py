#!/usr/bin/env python3
"""Calendar Tentative Registration — strict, secret-free configuration.

Every key has a conservative default and every unknown key is an error, so a
typo cannot silently become "the default the operator did not mean". Nothing
here may hold a credential: the OAuth client and refresh token reach the
process only through systemd's per-unit credential directory.

Entries go onto the owner's primary calendar, marked by a title prefix
(`【仮】`) rather than by living on a calendar of their own. `calendar.calendar_id`
accepts exactly `primary`: the OAuth scope (`calendar.events.owned`) would
also reach the owner's other calendars, and this check keeps every write on
the one calendar the owner looks at, the one the engine reconciles.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

DEFAULT_CONFIG_PATH = "/srv/hermes/calendar-tentative/config/config.yaml"
CONFIG_ENV_VAR = "HERMES_CALENDAR_TENTATIVE_CONFIG"

MODE_APPLY = "apply"
MODE_DRY_RUN = "dry_run"

#: The only write target: Google's alias for the owner's own main calendar.
PRIMARY_CALENDAR = "primary"

DEFAULT_PATHS = {
    "state_db": "/srv/hermes/calendar-tentative/state/tentative.db",
    # Canonical commitments. Opened read-only; this service never writes it.
    "events_db": "/srv/hermes/hermes-data/events/events.db",
    # H.1's published read-only snapshot. Used to avoid proposing what is
    # already on a real calendar; this service's own entries in it are
    # ignored. Optional: an empty value disables the check.
    "calendar_index_db": "/srv/hermes/calendar/index/index.db",
    # H.2's published read-only snapshot. Only headers, labels, thread ids
    # and the sent flag are read, to tell mail from people apart from
    # newsletters. Without it, Gmail-only rows are not proposed (fail closed).
    "gmail_index_db": "/srv/hermes/gmail/index/index.db",
}

#: Which commitments whose only evidence is Gmail may be proposed (ct_mail).
#:   off                       none
#:   people                    mail the owner sent, replies in threads the
#:                             owner wrote in, mail from people (or lists) the
#:                             owner has written to, and mail Gmail filed under
#:                             Primary. Never promotions/social, never
#:                             automated senders or blind-copy blasts.
#:   people_and_notifications  also automated notices (Gmail "Updates",
#:                             no-reply senders) and blind-copy blasts.
#:   all                       no screening at all.
GMAIL_ONLY_MODES = ("off", "people", "people_and_notifications", "all")
DEFAULT_OAUTH = {
    # Empty means systemd's CREDENTIALS_DIRECTORY. A directory path only.
    "credential_dir": "",
}
DEFAULT_CALENDAR = {
    "calendar_id": PRIMARY_CALENDAR,
    "time_zone": "Asia/Tokyo",
    # Marks an entry as unconfirmed. Removing it from the title is how the
    # owner confirms one (ct_engine), so it must not be empty.
    "summary_prefix": "【仮】",
    "default_duration_minutes": 60,
    # Google event colour "1".."11", or empty for the calendar's own colour.
    "color_id": "",
}
DEFAULT_SELECTION = {
    "horizon_days": 60,
    "min_confidence": 0.7,
    "include_shared": True,
    # I.1 measured 95% of Gmail-derived rows as newsletters and campaigns, so
    # Gmail-only rows pass a mail screen first (GMAIL_ONLY_MODES). A row that
    # Discord, LINE or Calendar also mentions is unaffected by this setting.
    "gmail_only": "people",
    "match_tolerance_minutes": 5,
}
DEFAULT_LIMITS = {
    "max_creates_per_run": 10,
    "max_updates_per_run": 10,
    "max_withdrawals_per_run": 10,
    "max_active": 100,
    "max_attempts": 5,
}
DEFAULT_BEHAVIOUR = {
    "mode": MODE_APPLY,
    "update_on_change": True,
    "withdraw_on_resolution": True,
}
DEFAULT_NOTIFY = {
    "enabled": False,
    "socket_path": "/srv/hermes/channel-gateway/run/proactive.sock",
    "quiet_hours_start": "23:00",
    "quiet_hours_end": "08:00",
    "max_lines": 8,
    "timeout_seconds": 30,
}

DEFAULTS = {
    "paths": DEFAULT_PATHS,
    "oauth": DEFAULT_OAUTH,
    "calendar": DEFAULT_CALENDAR,
    "selection": DEFAULT_SELECTION,
    "limits": DEFAULT_LIMITS,
    "behaviour": DEFAULT_BEHAVIOUR,
    "notify": DEFAULT_NOTIFY,
}

#: A key that looks like a credential is refused wherever it appears.
FORBIDDEN_KEYS = frozenset({
    "token", "access_token", "refresh_token", "client_secret", "client_id",
    "password", "api_key", "secret",
})

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ConfigError(ValueError):
    pass


def _merge(given, defaults: dict, section: str) -> dict:
    if given is None:
        given = {}
    if not isinstance(given, dict):
        raise ConfigError(f"{section} must be a mapping")
    if FORBIDDEN_KEYS & set(given):
        raise ConfigError(f"{section}: credentials belong only in systemd credentials")
    unknown = set(given) - set(defaults)
    if unknown:
        raise ConfigError(f"{section} has unknown keys: {sorted(unknown)}")
    merged = dict(defaults)
    merged.update(given)
    return merged


def _int(value, name: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} must be an integer")
    if not lo <= value <= hi:
        raise ConfigError(f"{name} must be {lo}..{hi}")
    return value


def _bool(value, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{name} must be true or false")
    return value


def validate_calendar_id(value: str) -> str:
    """Exactly `primary`; nothing else."""
    cal = str(value or "").strip()
    if cal != PRIMARY_CALENDAR:
        raise ConfigError(
            "calendar.calendar_id must be \"primary\": entries go onto the owner's "
            "main calendar with a title prefix, never onto another calendar"
        )
    return cal


@dataclass(frozen=True)
class Config:
    path: str
    paths: dict
    oauth: dict
    calendar: dict
    selection: dict
    limits: dict
    behaviour: dict
    notify: dict

    @property
    def calendar_id(self) -> str:
        return self.calendar["calendar_id"]

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.calendar["time_zone"])

    @property
    def dry_run(self) -> bool:
        return self.behaviour["mode"] == MODE_DRY_RUN

    @property
    def credential_dir(self) -> str:
        return str(self.oauth["credential_dir"]
                   or os.environ.get("CREDENTIALS_DIRECTORY") or "")


def parse_config(data, *, path: str = "<memory>") -> Config:
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError("configuration root must be a mapping")
    if FORBIDDEN_KEYS & set(data):
        raise ConfigError("credentials belong only in systemd credentials, never this config")
    unknown = set(data) - set(DEFAULTS)
    if unknown:
        raise ConfigError(f"unknown top-level sections: {sorted(unknown)}")
    merged = {key: _merge(data.get(key), value, key) for key, value in DEFAULTS.items()}

    for key in ("state_db", "events_db"):
        if not str(merged["paths"][key] or "").strip():
            raise ConfigError(f"paths.{key} must be set")
    for key in ("calendar_index_db", "gmail_index_db"):
        merged["paths"][key] = str(merged["paths"][key] or "")

    cal = merged["calendar"]
    cal["calendar_id"] = validate_calendar_id(cal["calendar_id"])
    try:
        ZoneInfo(str(cal["time_zone"]))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError("calendar.time_zone is not a known IANA zone") from exc
    prefix = cal["summary_prefix"]
    if not isinstance(prefix, str) or not prefix.strip() or len(prefix) > 20 or "\n" in prefix:
        raise ConfigError("calendar.summary_prefix must be a short, non-empty single-line string")
    _int(cal["default_duration_minutes"], "calendar.default_duration_minutes", 5, 1440)
    color = str(cal["color_id"] or "")
    if color and color not in {str(n) for n in range(1, 12)}:
        raise ConfigError("calendar.color_id must be empty or 1..11")
    cal["color_id"] = color

    sel = merged["selection"]
    _int(sel["horizon_days"], "selection.horizon_days", 1, 366)
    conf = sel["min_confidence"]
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not 0.0 <= conf <= 1.0:
        raise ConfigError("selection.min_confidence must be 0.0..1.0")
    _bool(sel["include_shared"], "selection.include_shared")
    # YAML 1.1 reads a bare `off` (or `no`/`false`) as False. That can only
    # mean the narrowest mode; True could mean either wider one, so it is
    # refused rather than guessed.
    if sel["gmail_only"] is False:
        sel["gmail_only"] = "off"
    if sel["gmail_only"] is True:
        raise ConfigError('selection.gmail_only: write the mode as a quoted string, e.g. "people"')
    if sel["gmail_only"] not in GMAIL_ONLY_MODES:
        raise ConfigError(f"selection.gmail_only must be one of {', '.join(GMAIL_ONLY_MODES)}")
    _int(sel["match_tolerance_minutes"], "selection.match_tolerance_minutes", 0, 1440)

    lim = merged["limits"]
    for key in ("max_creates_per_run", "max_updates_per_run", "max_withdrawals_per_run"):
        _int(lim[key], f"limits.{key}", 0, 500)
    _int(lim["max_active"], "limits.max_active", 0, 2000)
    _int(lim["max_attempts"], "limits.max_attempts", 1, 50)

    beh = merged["behaviour"]
    if beh["mode"] not in (MODE_APPLY, MODE_DRY_RUN):
        raise ConfigError("behaviour.mode must be apply or dry_run")
    _bool(beh["update_on_change"], "behaviour.update_on_change")
    _bool(beh["withdraw_on_resolution"], "behaviour.withdraw_on_resolution")

    note = merged["notify"]
    _bool(note["enabled"], "notify.enabled")
    for key in ("quiet_hours_start", "quiet_hours_end"):
        if not isinstance(note[key], str) or not _HHMM.match(note[key]):
            raise ConfigError(f"notify.{key} must be HH:MM")
    _int(note["max_lines"], "notify.max_lines", 1, 20)
    _int(note["timeout_seconds"], "notify.timeout_seconds", 1, 300)
    if note["enabled"] and not str(note["socket_path"] or "").strip():
        raise ConfigError("notify.socket_path must be set when notify is enabled")

    return Config(path=str(path), **merged)


def load_config(path: str | None = None) -> Config:
    file_path = Path(path or os.environ.get(CONFIG_ENV_VAR) or DEFAULT_CONFIG_PATH)
    try:
        source = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {file_path}: {type(exc).__name__}") from exc
    try:
        data = yaml.safe_load(source)
    except yaml.YAMLError as exc:
        raise ConfigError(f"config {file_path} is not valid YAML") from exc
    return parse_config(data, path=str(file_path))
