#!/usr/bin/env python3
"""Strict non-secret configuration for the Calendar service."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


DEFAULT_CONFIG_PATH = "/srv/hermes/calendar/config/config.yaml"

DEFAULT_PATHS = {
    "authority_db": "/srv/hermes/calendar/state/calendar.db",
    "index_db": "/srv/hermes/calendar/index/index.db",
    "log_dir": "/srv/hermes/calendar/logs",
    "event_layer_db": "/srv/hermes/hermes-data/events/events.db",
}
DEFAULT_OAUTH = {
    # In production this resolves to systemd's unit-private credential tmpfs.
    # It is a directory path only; no credential value appears in YAML.
    "credential_dir": "",
    "scopes": [
        "https://www.googleapis.com/auth/calendar.events.readonly",
        "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    ],
}
DEFAULT_CALENDARS = {"selected_ids": []}
DEFAULT_SERVER = {
    "bind_host": "0.0.0.0",
    "port": 8791,
    "path": "/google/calendar/watch",
    "public_callback_url": "",
    "max_body_bytes": 1024,
}
DEFAULT_SYNC = {
    "poll_interval_seconds": 900,
    "worker_tick_seconds": 5,
    "projection_past_days": 30,
    "projection_future_days": 365,
    "projection_refresh_seconds": 21600,
    "max_retry_delay_seconds": 900,
    "watch_ttl_seconds": 604800,
    "watch_renewal_margin_seconds": 86400,
}

DEFAULTS = {
    "paths": DEFAULT_PATHS,
    "oauth": DEFAULT_OAUTH,
    "calendars": DEFAULT_CALENDARS,
    "server": DEFAULT_SERVER,
    "sync": DEFAULT_SYNC,
}


class ConfigError(ValueError):
    pass


def _merge(given, defaults, section):
    if given is None:
        given = {}
    if not isinstance(given, dict):
        raise ConfigError(f"{section} must be a mapping")
    unknown = set(given) - set(defaults)
    if unknown:
        raise ConfigError(f"{section} has unknown keys: {sorted(unknown)}")
    result = dict(defaults)
    result.update(given)
    return result


@dataclass(frozen=True)
class Config:
    path: str
    paths: dict
    oauth: dict
    calendars: dict
    server: dict
    sync: dict

    @property
    def credential_dir(self) -> str:
        return str(self.oauth["credential_dir"] or os.environ.get("CREDENTIALS_DIRECTORY") or "")


def parse_config(data: dict, *, path: str = "<memory>") -> Config:
    if not isinstance(data, dict):
        raise ConfigError("configuration root must be a mapping")
    # OAuth token values cannot accidentally be put in the config file.
    forbidden = {"token", "access_token", "refresh_token", "client_secret", "client_id", "password", "api_key"}
    if forbidden & set(data):
        raise ConfigError("credentials belong only in systemd credentials, never calendar config")
    unknown = set(data) - set(DEFAULTS)
    if unknown:
        raise ConfigError(f"unknown top-level sections: {sorted(unknown)}")
    merged = {key: _merge(data.get(key), value, key) for key, value in DEFAULTS.items()}
    ids = merged["calendars"]["selected_ids"]
    if not isinstance(ids, list) or any(not isinstance(x, str) or not x.strip() for x in ids):
        raise ConfigError("calendars.selected_ids must be a list of stable non-empty calendar IDs")
    if len(ids) != len(set(ids)):
        raise ConfigError("calendars.selected_ids contains duplicates")
    server = merged["server"]
    if server["path"] != "/google/calendar/watch":
        raise ConfigError("only server.path=/google/calendar/watch is permitted")
    if not (1 <= int(server["port"]) <= 65535):
        raise ConfigError("server.port must be 1..65535")
    callback = str(server["public_callback_url"] or "")
    if callback and not callback.startswith("https://"):
        raise ConfigError("server.public_callback_url must be HTTPS")
    sync = merged["sync"]
    if int(sync["poll_interval_seconds"]) < 300:
        raise ConfigError("sync.poll_interval_seconds must be at least 300")
    if not (30 <= int(sync["projection_future_days"]) <= 366):
        raise ConfigError("sync.projection_future_days must be 30..366")
    if not (0 <= int(sync["projection_past_days"]) <= 90):
        raise ConfigError("sync.projection_past_days must be 0..90")
    return Config(path=str(path), **merged)


def load_config(path: str | None = None) -> Config:
    file_path = Path(path or os.environ.get("HERMES_CALENDAR_CONFIG") or DEFAULT_CONFIG_PATH)
    try:
        source = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {file_path}: {type(exc).__name__}") from exc
    return parse_config(yaml.safe_load(source) or {}, path=str(file_path))
