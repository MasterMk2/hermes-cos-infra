#!/usr/bin/env python3
"""Unified Channel Gateway — configuration and secret loading.

The configuration carries no secret. Four credentials are needed at runtime
and every one of them arrives the same way: systemd's LoadCredential first,
a root-owned file second (spec sections 24, 25, 105).

The owner identifiers are not secrets, but they are the authority boundary
(sections 5, 6, 7), so they get the same validation a credential would: an
exact immutable platform id, or the platform is disabled. A display name is
never accepted — there is no code path that reads one.
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = "/srv/hermes/channel-gateway/config/config.yaml"

# LINE user ids are 'U' followed by 32 hex characters. Discord ids are
# snowflakes. Both are immutable; both are checked exactly.
LINE_USER_ID_RE = re.compile(r"^U[0-9a-f]{32}$")
DISCORD_USER_ID_RE = re.compile(r"^[0-9]{15,25}$")

PLACEHOLDERS = {
    "",
    None,
    "REPLACE_WITH_OWNER_LINE_USER_ID",
    "REPLACE_WITH_OWNER_DISCORD_USER_ID",
}

DEFAULT_LINE = {
    "enabled": False,
    "listen_host": "127.0.0.1",
    "listen_port": 8790,
    "webhook_path": "/line/webhook",
    "max_body_bytes": 65536,
    "reply_token_ttl_seconds": 60,
    "text_chunk_chars": 4800,
    "max_messages_per_request": 5,
}

DEFAULT_DISCORD = {
    "enabled": False,
    "text_chunk_chars": 1900,
    "max_chunks": 4,
    "typing_indicator": True,
}

DEFAULT_HERMES = {
    "base_url": "http://127.0.0.1:8642",
    "model": "hermes-agent",
    "soft_timeout_seconds": 45,
    "absolute_timeout_seconds": 240,
    "poll_interval_seconds": 2,
    "session_prefix": "gw",
}

DEFAULT_LIMITS = {
    "max_active_requests": 2,
    "max_attempts": 3,
    "outbound_max_attempts": 3,
    "queue_warn_depth": 50,
    "response_hard_limit_chars": 20000,
    "response_soft_limit_chars": 4000,
}

DEFAULT_RETENTION = {"days": 14}

SECRET_NAMES = (
    "line_channel_secret",
    "line_channel_access_token",
    "discord_interface_token",
    "hermes_api_key",
)


class ConfigError(ValueError):
    """Raised when the gateway cannot be started safely."""


@dataclass(frozen=True)
class SecretRef:
    credential_name: str
    fallback_file: str


@dataclass(frozen=True)
class Config:
    path: str
    owner_line_user_id: str | None
    owner_discord_user_id: str | None
    state_db: str
    log_dir: str
    line: dict
    discord: dict
    hermes: dict
    limits: dict
    retention: dict
    secrets: dict
    raw: dict = field(default_factory=dict, repr=False)

    # ---- authority boundary (sections 5, 6, 48, 111, 112) -------------

    def is_owner(self, platform: str, platform_user_id) -> bool:
        """The only authentication in the gateway.

        Exact match against a configured immutable id. An unset owner id
        means the platform authenticates nobody — deliberately, so that a
        half-configured gateway rejects rather than admits.
        """
        uid = str(platform_user_id or "")
        if not uid:
            return False
        if platform == "line":
            return bool(self.owner_line_user_id) and uid == self.owner_line_user_id
        if platform == "discord":
            return bool(self.owner_discord_user_id) and uid == self.owner_discord_user_id
        return False

    @property
    def line_ready(self) -> bool:
        return bool(self.line.get("enabled")) and bool(self.owner_line_user_id)

    @property
    def discord_ready(self) -> bool:
        return bool(self.discord.get("enabled")) and bool(self.owner_discord_user_id)

    @property
    def any_channel_ready(self) -> bool:
        return self.line_ready or self.discord_ready


def _merge(section, defaults, what) -> dict:
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


def _owner_id(value, pattern, what) -> str | None:
    if value in PLACEHOLDERS:
        return None
    s = str(value).strip()
    if not pattern.match(s):
        raise ConfigError(
            f"{what} must be an exact platform user id matching {pattern.pattern}; "
            f"a display name or nickname is never accepted"
        )
    return s


def _validate_listen_host(value) -> str:
    """Loopback or one explicit private address — never a wildcard.

    This started as "loopback only", on the assumption that the reverse proxy
    would share the host. It does not: the TLS terminator for this deployment
    is a Caddy container on a different machine on the LAN, and a loopback
    bind makes the webhook unreachable from it. Measured, not assumed —
    `wget` from inside that container reaches this host's other services.

    So the rule is written as the property that actually matters rather than
    as the address that happened to satisfy it:

      * a wildcard (`0.0.0.0`, `::`) is refused. It binds every interface,
        including ones added later, which is how a service becomes public by
        accident rather than by decision.
      * a globally routable address is refused. The webhook is reached
        through the proxy; nothing on the internet may open a socket here.

    The operator therefore has to name one private address, and that address
    is visible in the config where it can be reviewed (sections 16, 22).
    """
    host = str(value or "").strip()
    if host in ("localhost",):
        return host
    try:
        addr = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ConfigError(
            f"line.listen_host must be an IP address or 'localhost', got {host!r}"
        ) from exc
    if addr.is_unspecified:
        raise ConfigError(
            "line.listen_host must not be a wildcard (0.0.0.0 / ::): bind one "
            "explicit address so the reachable surface is a decision, not a default"
        )
    # `is_global` states the property directly — "routable from the internet"
    # — rather than approximating it with a list of private ranges. Loopback,
    # RFC1918, link-local, CGNAT and the documentation ranges are all
    # non-global and all fine to bind; a publicly routable address is not.
    if addr.is_global:
        raise ConfigError(
            "line.listen_host must not be a publicly routable address: the "
            "gateway is reached only through the reverse proxy, never directly "
            "from the internet"
        )
    return host


def parse_config(data: dict, path: str = "<memory>") -> Config:
    if not isinstance(data, dict):
        raise ConfigError("configuration root must be a mapping")

    for forbidden in ("channel_secret", "access_token", "token", "api_key"):
        if forbidden in data:
            raise ConfigError(
                f"the configuration must never hold a credential (found {forbidden}); "
                "secrets arrive through systemd credentials or a root-owned file"
            )

    owner = data.get("owner") or {}
    if not isinstance(owner, dict):
        raise ConfigError("owner must be a mapping")
    line_owner = _owner_id(owner.get("line_user_id"), LINE_USER_ID_RE, "owner.line_user_id")
    discord_owner = _owner_id(
        owner.get("discord_user_id"), DISCORD_USER_ID_RE, "owner.discord_user_id"
    )

    paths = data.get("paths") or {}
    line = _merge(data.get("line"), DEFAULT_LINE, "line")
    discord = _merge(data.get("discord"), DEFAULT_DISCORD, "discord")
    hermes = _merge(data.get("hermes"), DEFAULT_HERMES, "hermes")
    limits = _merge(data.get("limits"), DEFAULT_LIMITS, "limits")
    retention = _merge(data.get("retention"), DEFAULT_RETENTION, "retention")

    if not (1 <= int(limits["max_active_requests"]) <= 8):
        raise ConfigError("limits.max_active_requests must be 1..8 on this host")
    line["listen_host"] = _validate_listen_host(line["listen_host"])
    if not str(line["webhook_path"]).startswith("/"):
        raise ConfigError("line.webhook_path must start with /")

    base = str(hermes["base_url"]).rstrip("/")
    if "REPLACE_" in base:
        # Substitution is mandatory rather than optional: the sample ships a
        # placeholder because the correct value differs per host, and a
        # plausible-looking default (127.0.0.1) was measurably wrong on this
        # one — the API publishes its port on the LAN address only, so every
        # request failed. Refusing here means that shows up now.
        raise ConfigError(
            "hermes.base_url still holds a placeholder; set it to the address "
            "the Hermes API actually listens on (check with: ss -ltn | grep 8642)"
        )
    if not base.startswith(("http://127.0.0.1", "http://localhost", "http://192.168.",
                            "http://10.", "http://172.", "https://127.0.0.1",
                            "https://localhost")):
        raise ConfigError(
            "hermes.base_url must stay on the local host or LAN; the assistant's "
            "API is never reached across the internet"
        )
    hermes["base_url"] = base

    raw_secrets = data.get("secrets") or {}
    if not isinstance(raw_secrets, dict):
        raise ConfigError("secrets must be a mapping")
    unknown = set(raw_secrets) - set(SECRET_NAMES)
    if unknown:
        raise ConfigError(f"secrets has unknown keys: {sorted(unknown)}")
    secrets = {}
    for name in SECRET_NAMES:
        entry = raw_secrets.get(name) or {}
        if not isinstance(entry, dict):
            raise ConfigError(f"secrets.{name} must be a mapping")
        for forbidden in ("value", "secret", "token"):
            if forbidden in entry:
                raise ConfigError(
                    f"secrets.{name}.{forbidden} would put a credential in the "
                    "configuration file; use credential_name / fallback_file"
                )
        secrets[name] = SecretRef(
            credential_name=str(entry.get("credential_name") or name),
            fallback_file=str(
                entry.get("fallback_file") or f"/etc/hermes-secrets/{name}"
            ),
        )

    return Config(
        path=str(path),
        owner_line_user_id=line_owner,
        owner_discord_user_id=discord_owner,
        state_db=str(paths.get("state_db")
                     or "/srv/hermes/channel-gateway/state/gateway.db"),
        log_dir=str(paths.get("log_dir") or "/srv/hermes/channel-gateway/logs"),
        line=line,
        discord=discord,
        hermes=hermes,
        limits=limits,
        retention=retention,
        secrets=secrets,
        raw=data,
    )


def load_config(path=None) -> Config:
    p = Path(path or os.environ.get("CHANNEL_GATEWAY_CONFIG") or DEFAULT_CONFIG_PATH)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {p}: {exc}") from exc
    return parse_config(yaml.safe_load(text) or {}, path=str(p))


#: What `probe_secret` can conclude about one credential file.
SECRET_READABLE = "readable"
SECRET_UNREADABLE = "present-but-unreadable"
SECRET_MISSING = "missing"


def probe_secret(path: Path) -> tuple:
    """(status, value_or_None) for one candidate file.

    Every filesystem call here is wrapped, because the interesting case is
    the one that used to crash: `/etc/hermes-secrets` is 0700 root:root by
    design, so a non-root reader gets EACCES from `stat` rather than a
    negative answer — and Python 3.13's `Path.is_file()` propagates that
    instead of returning False. The result was a traceback out of `--check`
    where a clean configuration error was intended.

    "present but unreadable" is kept distinct from "missing" on purpose. Under
    systemd the service never reads these paths at all — it reads the copies
    systemd places in $CREDENTIALS_DIRECTORY — so an operator running a check
    by hand SHOULD find them unreadable, and that is not a fault.
    """
    try:
        if path.is_file():
            try:
                return SECRET_READABLE, path.read_text(encoding="utf-8").strip()
            except OSError:
                return SECRET_UNREADABLE, None
        return SECRET_MISSING, None
    except PermissionError:
        # Cannot even stat it: the directory above is closed to this user.
        # Whether the file exists is unknowable from here.
        return SECRET_UNREADABLE, None
    except OSError:
        return SECRET_MISSING, None


def secret_status(cfg: Config, name: str) -> tuple:
    """(status, where) for a credential, without returning its value."""
    ref = cfg.secrets[name]
    creds = os.environ.get("CREDENTIALS_DIRECTORY")
    if creds:
        status, _ = probe_secret(Path(creds) / ref.credential_name)
        if status == SECRET_READABLE:
            return status, "systemd credential"
    status, _ = probe_secret(Path(ref.fallback_file))
    return status, ref.fallback_file


def load_secret(cfg: Config, name: str, *, required: bool = True) -> str | None:
    """systemd credential first, root-owned file second.

    Deliberately absent: any way to pass a credential on the command line or
    in the environment of a shell the operator types into. /proc/<pid>/cmdline
    is world readable and shell history outlives the terminal.
    """
    if name not in cfg.secrets:
        raise ConfigError(f"unknown secret {name}")
    ref = cfg.secrets[name]
    tried = []

    creds = os.environ.get("CREDENTIALS_DIRECTORY")
    if creds:
        cand = Path(creds) / ref.credential_name
        status, value = probe_secret(cand)
        tried.append(f"{cand} ({status})")
        if status == SECRET_READABLE and value:
            return value

    fb = Path(ref.fallback_file)
    status, value = probe_secret(fb)
    tried.append(f"{fb} ({status})")
    if status == SECRET_READABLE and value and not value.startswith("REPLACE_"):
        return value

    if required:
        raise ConfigError(
            f"secret {name} not available; looked at: {', '.join(tried)}"
        )
    return None
