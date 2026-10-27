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
    # The whole wait for one answer, measured from the run's recorded start
    # and surviving restarts. 480 = four bridge timeouts (120 s): a primary
    # timeout, one retry and the measured fallback maximum, with headroom.
    # At most 840 so a message queued behind it stays under the attention
    # engine's 900 s queue_stuck threshold.
    "absolute_timeout_seconds": 480,
    "poll_interval_seconds": 2,
    # One status GET may take this long before it counts as a failed read.
    "poll_request_timeout_seconds": 10,
    # A waiting worker renews its claim this often, so recover_stale (lease
    # 300 s) never reclaims a wait that is still alive.
    "heartbeat_seconds": 60,
    # Status reads failing back to back for this long end the wait with the
    # "unavailable" text; a shorter blip is waited through.
    "unavailable_grace_seconds": 90,
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

# G.2. Off by default: a gateway that gains the ability to message the owner
# unprompted should do so because someone configured it, not because it was
# upgraded.
DEFAULT_PROACTIVE = {
    "enabled": False,
    "socket_path": "/srv/hermes/channel-gateway/run/proactive.sock",
    # Section 80 asks for 15-60 minutes. The engine also refreshes right after
    # it spends a push, so the TTL only bounds how stale the figure gets from
    # sends made by something else.
    "quota_cache_seconds": 1800,
    # Section 89's watchdog: a delivery left SENDING for longer than this is
    # assumed to belong to a process that died, and is retried.
    "stuck_lease_seconds": 180,
}

# J.1. Off by default, and off means the coding route does not exist: the
# gateway keeps sending every turn to Hermes exactly as it did before. Turning
# it on is three decisions, not one — enable it, point it at a broker, and give
# the broker a credential — because it is the only path in this service that
# can cause a write outside the gateway.
DEFAULT_CODING_HANDOFF = {
    "enabled": False,
    "endpoint": "http://127.0.0.1:18771",
    "timeout_seconds": 20,
    # How long a shown-but-unanswered proposal stays answerable.
    "proposal_ttl_seconds": 1800,
    # Registry lookups are cheap but not free, and the answer changes only when
    # an operator edits the registry.
    "projects_cache_seconds": 120,
}

SECRET_NAMES = (
    "line_channel_secret",
    "line_channel_access_token",
    "discord_interface_token",
    "hermes_api_key",
    "coding_handoff_token",
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
    proactive: dict
    coding_handoff: dict
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

    @property
    def proactive_ready(self) -> bool:
        """Whether the owner-notification socket should be opened at all.

        Requires a channel: a proactive API with nowhere to deliver is a
        socket that accepts work it can only fail, which is worse than not
        existing (G.2 sections 49, 83).
        """
        return bool(self.proactive.get("enabled")) and self.any_channel_ready

    @property
    def coding_handoff_ready(self) -> bool:
        """Whether the coding route may engage at all.

        A channel is required for the same reason the proactive socket needs
        one: a route that can start a coding job but cannot tell the owner its
        job id is worse than no route.
        """
        return bool(self.coding_handoff.get("enabled")) and self.any_channel_ready


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


def validate_handoff_endpoint(value) -> str:
    """Loopback only, explicit port, no credentials, no path.

    Stricter than `line.listen_host`, which has to be reachable from a proxy on
    the LAN. This one is the opposite direction — the gateway calling out — and
    the only correct destination is a service on this host. A typo that sent a
    job-creation request to a LAN address would be a serious failure, so the
    rule is written to make that impossible rather than unlikely.
    """
    from urllib.parse import urlparse

    parsed = urlparse(str(value or ""))
    if parsed.scheme != "http":
        raise ValueError("must be an http:// loopback origin")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("must point at loopback (127.0.0.1, localhost or ::1)")
    if parsed.username or parsed.password:
        raise ValueError("must not embed credentials")
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("must be a bare origin with no path, query or fragment")
    if not parsed.port:
        raise ValueError("requires an explicit port")
    return str(value).rstrip("/")


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
    proactive = _merge(data.get("proactive"), DEFAULT_PROACTIVE, "proactive")
    coding_handoff = _merge(data.get("coding_handoff"), DEFAULT_CODING_HANDOFF,
                            "coding_handoff")

    if coding_handoff["enabled"]:
        # Validated only when the route is on, so an existing deployment that
        # has never heard of this section keeps starting.
        try:
            coding_handoff["endpoint"] = validate_handoff_endpoint(
                coding_handoff["endpoint"])
        except ValueError as exc:
            raise ConfigError(f"coding_handoff.endpoint: {exc}") from exc
        if not (5 <= int(coding_handoff["timeout_seconds"]) <= 120):
            raise ConfigError("coding_handoff.timeout_seconds must be 5..120")
        if not (60 <= int(coding_handoff["proposal_ttl_seconds"]) <= 86400):
            raise ConfigError(
                "coding_handoff.proposal_ttl_seconds must be 60..86400")
        if not (0 <= int(coding_handoff["projects_cache_seconds"]) <= 3600):
            raise ConfigError(
                "coding_handoff.projects_cache_seconds must be 0..3600")

    sock = str(proactive["socket_path"])
    if not sock.startswith("/"):
        raise ConfigError("proactive.socket_path must be an absolute path")
    if sock.startswith(("/srv/hermes/channel-gateway/app",
                        "/srv/hermes/channel-gateway/venv")):
        # Those two directories are root-owned and read-only to the service
        # precisely so it cannot rewrite its own code; a socket there would
        # either fail to bind or require loosening that.
        raise ConfigError(
            "proactive.socket_path must not live under app/ or venv/"
        )
    if not (60 <= int(proactive["quota_cache_seconds"]) <= 21600):
        raise ConfigError(
            "proactive.quota_cache_seconds must be 60..21600 (section 80 "
            "suggests 15-60 minutes)"
        )

    for key, low, high in (("absolute_timeout_seconds", 30, 840),
                           ("poll_request_timeout_seconds", 1, 60),
                           ("heartbeat_seconds", 1, 100),
                           ("unavailable_grace_seconds", 1, 840)):
        value = hermes[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
            raise ConfigError(f"hermes.{key} must be {low}..{high}")
    poll = hermes["poll_interval_seconds"]
    if isinstance(poll, bool) or not isinstance(poll, (int, float)) or not 0.01 <= poll <= 30:
        raise ConfigError("hermes.poll_interval_seconds must be 0.01..30")
    if hermes["unavailable_grace_seconds"] > hermes["absolute_timeout_seconds"]:
        raise ConfigError("hermes.unavailable_grace_seconds must not exceed absolute_timeout_seconds")
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
        proactive=proactive,
        coding_handoff=coding_handoff,
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
