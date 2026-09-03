#!/usr/bin/env python3
"""Discord Knowledge Collector — configuration loading and validation.

The configuration file carries NO secret. The bot token is delivered at
runtime only (systemd LoadCredential, or a root-owned fallback file); see
`load_token`.

Two independent boundaries guard ingestion (spec section 10):

  1. Discord's own permission boundary (the bot is only invited with
     View Channels / Read Message History, and only to the two target
     guilds).
  2. This allowlist. A guild the bot can see is not collected unless its
     exact id is listed AND enabled, and a channel is not collected unless
     its exact id is listed (threads may inherit their parent's allowance).

Default deny is structural: an empty or placeholder allowlist collects
nothing. It never degrades into "collect everything".
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

SNOWFLAKE_RE = re.compile(r"^[0-9]{15,25}$")

# Spec section 11 specifies exactly two target guilds. This deployment
# collects three, at the operator's explicit decision (2026-09-03): the two
# original guilds plus the operator's own small server. The limit is kept as a
# hard cap rather than removed, so the scope still cannot grow silently — it
# only grows when someone edits this line on purpose.
MAX_GUILDS = 3

DEFAULT_CONFIG_PATH = "/srv/hermes/discord-knowledge/config/config.yaml"

DEFAULT_LIMITS = {
    "content_max_chars": 8000,
    "embed_title_max_chars": 300,
    "embed_description_max_chars": 1000,
    "embed_field_max_chars": 300,
    "embed_field_max_count": 10,
    "embed_max_count": 5,
    "attachment_max_count": 10,
    "queue_warn_depth": 5000,
}

DEFAULT_BACKFILL = {
    "enabled": True,
    "days": 14,
    "max_messages_per_channel": 2000,
    "page_size": 100,
    "max_pages_per_run": 20,
}

DEFAULT_EVENTS = {
    "enabled": True,
    "window_days": 14,
    "bot_author_allowlist": [],
}


class ConfigError(ValueError):
    """Raised when the configuration cannot be used safely."""


@dataclass(frozen=True)
class GuildScope:
    guild_id: str
    label: str
    enabled: bool
    channels: tuple[str, ...]
    include_threads: bool

    @property
    def active(self) -> bool:
        """A guild only ingests when it is enabled AND names channels."""
        return self.enabled and bool(self.channels)


@dataclass(frozen=True)
class Config:
    path: str
    guilds: tuple[GuildScope, ...]
    source_db: str
    index_db: str
    state_dir: str
    log_dir: str
    credential_name: str
    fallback_secret_file: str
    backfill: dict
    limits: dict
    events: dict
    owner_discord_user_id: str | None
    raw: dict = field(default_factory=dict, repr=False)

    # ---- allowlist decisions (spec sections 10-12, 36) ----------------

    def guild(self, guild_id) -> GuildScope | None:
        gid = str(guild_id) if guild_id is not None else ""
        for g in self.guilds:
            if g.guild_id == gid:
                return g
        return None

    def guild_allowed(self, guild_id) -> bool:
        g = self.guild(guild_id)
        return bool(g and g.active)

    def channel_allowed(self, guild_id, channel_id, parent_channel_id=None) -> bool:
        """Ingest decision for one (guild, channel) pair.

        A thread is allowed when its parent channel is allowed and the guild
        opted into threads; the thread's own id may also be listed directly.
        """
        g = self.guild(guild_id)
        if g is None or not g.active:
            return False
        cid = str(channel_id) if channel_id is not None else ""
        if cid and cid in g.channels:
            return True
        if g.include_threads and parent_channel_id is not None:
            pid = str(parent_channel_id)
            if pid and pid in g.channels:
                return True
        return False

    @property
    def active_guild_ids(self) -> tuple[str, ...]:
        return tuple(g.guild_id for g in self.guilds if g.active)

    @property
    def allowed_channel_ids(self) -> tuple[str, ...]:
        out = []
        for g in self.guilds:
            if g.active:
                out.extend(g.channels)
        return tuple(out)

    @property
    def ready_to_ingest(self) -> bool:
        """False while the config still holds placeholders / empty scope.

        The collector must stay in a DEGRADED, non-ingesting state rather
        than fall back to collecting everything it can see.
        """
        return bool(self.active_guild_ids)


def _as_id(value, what) -> str:
    s = str(value).strip() if value is not None else ""
    if not SNOWFLAKE_RE.match(s):
        raise ConfigError(
            f"{what} must be a Discord snowflake id (digits only); got {s!r}"
        )
    return s


def _merge_defaults(section, defaults, what) -> dict:
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise ConfigError(f"{what} must be a mapping")
    unknown = set(section) - set(defaults)
    if unknown:
        raise ConfigError(f"{what} has unknown keys: {sorted(unknown)}")
    out = dict(defaults)
    out.update(section)
    return out


def _positive_int(section, key, what, minimum=1, maximum=10**9) -> None:
    v = section[key]
    if not isinstance(v, int) or isinstance(v, bool) or not (minimum <= v <= maximum):
        raise ConfigError(
            f"{what}.{key} must be an int in [{minimum}, {maximum}]; got {v!r}"
        )


def parse_config(data: dict, path: str = "<memory>") -> Config:
    if not isinstance(data, dict):
        raise ConfigError("configuration root must be a mapping")

    paths = data.get("paths") or {}
    if not isinstance(paths, dict):
        raise ConfigError("paths must be a mapping")

    secret = data.get("secret") or {}
    if not isinstance(secret, dict):
        raise ConfigError("secret must be a mapping")
    for forbidden in ("token", "bot_token", "value"):
        if forbidden in secret:
            raise ConfigError(
                "the configuration must never carry the bot token itself "
                f"(found secret.{forbidden}); use LoadCredential or the "
                "root-owned secret file"
            )

    raw_guilds = data.get("guilds") or []
    if not isinstance(raw_guilds, list):
        raise ConfigError("guilds must be a list")
    if len(raw_guilds) > MAX_GUILDS:
        raise ConfigError(
            f"at most {MAX_GUILDS} guilds may be configured; got {len(raw_guilds)}"
        )

    guilds: list[GuildScope] = []
    seen: set[str] = set()
    for i, g in enumerate(raw_guilds):
        if not isinstance(g, dict):
            raise ConfigError(f"guilds[{i}] must be a mapping")
        enabled = bool(g.get("enabled", False))
        gid_raw = g.get("guild_id")
        placeholder = isinstance(gid_raw, str) and not SNOWFLAKE_RE.match(gid_raw.strip())
        if placeholder and not enabled:
            # An un-filled placeholder entry is allowed only while disabled;
            # it exists so the operator can see where the id goes.
            continue
        gid = _as_id(gid_raw, f"guilds[{i}].guild_id")
        if gid in seen:
            raise ConfigError(f"duplicate guild_id {gid}")
        seen.add(gid)
        chan_raw = g.get("channels") or []
        if not isinstance(chan_raw, list):
            raise ConfigError(f"guilds[{i}].channels must be a list")
        channels = tuple(
            _as_id(c, f"guilds[{i}].channels[{j}]") for j, c in enumerate(chan_raw)
        )
        if len(set(channels)) != len(channels):
            raise ConfigError(f"guilds[{i}].channels contains duplicates")
        guilds.append(
            GuildScope(
                guild_id=gid,
                label=str(g.get("label") or gid),
                enabled=enabled,
                channels=channels,
                include_threads=bool(g.get("include_threads", True)),
            )
        )

    backfill = _merge_defaults(data.get("backfill"), DEFAULT_BACKFILL, "backfill")
    for k in ("days", "max_messages_per_channel", "page_size", "max_pages_per_run"):
        _positive_int(backfill, k, "backfill")
    if backfill["page_size"] > 100:
        raise ConfigError("backfill.page_size must be <= 100 (Discord API limit)")
    backfill["enabled"] = bool(backfill["enabled"])

    limits = _merge_defaults(data.get("limits"), DEFAULT_LIMITS, "limits")
    for k in limits:
        _positive_int(limits, k, "limits")

    events = _merge_defaults(data.get("events"), DEFAULT_EVENTS, "events")
    _positive_int(events, "window_days", "events")
    events["enabled"] = bool(events["enabled"])
    allow = events.get("bot_author_allowlist") or []
    if not isinstance(allow, list):
        raise ConfigError("events.bot_author_allowlist must be a list")
    events["bot_author_allowlist"] = [
        _as_id(a, f"events.bot_author_allowlist[{i}]") for i, a in enumerate(allow)
    ]

    owner = (data.get("owner") or {}).get("discord_user_id")
    owner_id = None
    if owner not in (None, "", "REPLACE_WITH_OWNER_DISCORD_USER_ID"):
        owner_id = _as_id(owner, "owner.discord_user_id")

    return Config(
        path=str(path),
        guilds=tuple(guilds),
        source_db=str(paths.get("source_db") or "/srv/hermes/knowledge/discord/messages.db"),
        index_db=str(
            paths.get("index_db")
            or "/srv/hermes/hermes-data/knowledge-index/discord/index.db"
        ),
        state_dir=str(paths.get("state_dir") or "/srv/hermes/discord-knowledge/state"),
        log_dir=str(paths.get("log_dir") or "/srv/hermes/discord-knowledge/logs"),
        credential_name=str(secret.get("credential_name") or "discord-knowledge-token"),
        fallback_secret_file=str(
            secret.get("fallback_file") or "/etc/hermes-secrets/discord-knowledge-token"
        ),
        backfill=backfill,
        limits=limits,
        events=events,
        owner_discord_user_id=owner_id,
        raw=data,
    )


def load_config(path: str | os.PathLike | None = None) -> Config:
    p = Path(path or os.environ.get("DISCORD_KNOWLEDGE_CONFIG") or DEFAULT_CONFIG_PATH)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {p}: {exc}") from exc
    data = yaml.safe_load(text) or {}
    return parse_config(data, path=str(p))


def load_token(cfg: Config) -> str:
    """Return the bot token, or raise.

    Order (spec sections 16/17):
      1. systemd LoadCredential — $CREDENTIALS_DIRECTORY/<credential_name>
      2. root-owned fallback file

    The token is never accepted from a command-line argument (argv is world
    readable through /proc) and never from the configuration file.
    """
    creds = os.environ.get("CREDENTIALS_DIRECTORY")
    tried = []
    if creds:
        cand = Path(creds) / cfg.credential_name
        tried.append(str(cand))
        if cand.is_file():
            tok = cand.read_text(encoding="utf-8").strip()
            if tok:
                return tok
    fb = Path(cfg.fallback_secret_file)
    tried.append(str(fb))
    if fb.is_file():
        tok = fb.read_text(encoding="utf-8").strip()
        if tok:
            return tok
    raise ConfigError(
        "discord bot token not available; looked at: " + ", ".join(tried)
    )
