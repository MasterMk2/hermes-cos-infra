#!/usr/bin/env python3
"""Strict non-secret configuration for the Gmail ingestion service."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


DEFAULT_CONFIG_PATH = "/srv/hermes/gmail/config/config.yaml"

DEFAULTS = {
    "paths": {
        "authority_db": "/srv/hermes/gmail/state/gmail.db",
        "index_db": "/srv/hermes/gmail/index/index.db",
        "log_dir": "/srv/hermes/gmail/logs",
    },
    "oauth": {
        "credential_dir": "",
        "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
    },
    "mailbox": {
        "initial_days": 30,
    },
    "pubsub": {
        "topic_name": "",
        "subscription_name": "",
        "credential_filename": "pubsub-service-account.json",
        "pull_batch_size": 10,
    },
    "sync": {
        "poll_interval_seconds": 900,
        "worker_tick_seconds": 5,
        "max_retry_delay_seconds": 900,
        "auth_retry_delay_seconds": 21600,
        "watch_renewal_margin_seconds": 129600,
    },
}


class ConfigError(ValueError):
    pass


def _merge(given, defaults, name):
    if given is None:
        given = {}
    if not isinstance(given, dict):
        raise ConfigError(f"{name} must be a mapping")
    unknown = set(given) - set(defaults)
    if unknown:
        raise ConfigError(f"{name} has unknown keys: {sorted(unknown)}")
    out = dict(defaults)
    out.update(given)
    return out


@dataclass(frozen=True)
class Config:
    path: str
    paths: dict
    oauth: dict
    mailbox: dict
    pubsub: dict
    sync: dict

    @property
    def credential_dir(self) -> str:
        return str(self.oauth["credential_dir"] or os.environ.get("CREDENTIALS_DIRECTORY") or "")

    @property
    def pubsub_credential_path(self) -> str:
        directory = self.credential_dir
        return str(Path(directory) / str(self.pubsub["credential_filename"])) if directory else ""


def parse_config(data: dict, *, path: str = "<memory>") -> Config:
    if not isinstance(data, dict):
        raise ConfigError("configuration root must be a mapping")
    forbidden = {
        "token", "access_token", "refresh_token", "client_secret", "client_id",
        "password", "api_key", "service_account", "private_key",
    }
    if forbidden & set(data):
        raise ConfigError("credentials belong only in root-managed systemd credentials")
    unknown = set(data) - set(DEFAULTS)
    if unknown:
        raise ConfigError(f"unknown top-level sections: {sorted(unknown)}")
    merged = {key: _merge(data.get(key), value, key) for key, value in DEFAULTS.items()}
    if merged["oauth"]["scopes"] != DEFAULTS["oauth"]["scopes"]:
        raise ConfigError("only Gmail read-only scope is permitted")
    if not (7 <= int(merged["mailbox"]["initial_days"]) <= 90):
        raise ConfigError("mailbox.initial_days must be 7..90")
    pubsub = merged["pubsub"]
    topic = str(pubsub["topic_name"] or "")
    subscription = str(pubsub["subscription_name"] or "")
    if bool(topic) != bool(subscription):
        raise ConfigError("pubsub.topic_name and subscription_name must be configured together")
    if topic and not topic.startswith("projects/"):
        raise ConfigError("pubsub.topic_name must be a fully-qualified projects/... topic")
    if subscription and not subscription.startswith("projects/"):
        raise ConfigError("pubsub.subscription_name must be a fully-qualified projects/... subscription")
    if not (1 <= int(pubsub["pull_batch_size"]) <= 50):
        raise ConfigError("pubsub.pull_batch_size must be 1..50")
    sync = merged["sync"]
    if int(sync["poll_interval_seconds"]) < 300:
        raise ConfigError("sync.poll_interval_seconds must be at least 300")
    if not (1 <= int(sync["worker_tick_seconds"]) <= 60):
        raise ConfigError("sync.worker_tick_seconds must be 1..60")
    if int(sync["watch_renewal_margin_seconds"]) < 3600:
        raise ConfigError("sync.watch_renewal_margin_seconds must be at least one hour")
    return Config(path=str(path), **merged)


def load_config(path: str | None = None) -> Config:
    file_path = Path(path or os.environ.get("HERMES_GMAIL_CONFIG") or DEFAULT_CONFIG_PATH)
    try:
        raw = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {file_path}: {type(exc).__name__}") from exc
    return parse_config(yaml.safe_load(raw) or {}, path=str(file_path))
