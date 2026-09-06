"""Configuration for Antigravity Provider Bridge (Phase H.3.1)."""

from dataclasses import dataclass, field
import os
from typing import Any, Dict, Optional
import yaml


@dataclass
class BridgeConfig:
    """Settings for the local Antigravity provider bridge."""

    host: str = "172.18.0.1"
    port: int = 8765
    binary_path: str = "/srv/hermes/antigravity/bin/antigravity"
    default_model: str = "gemini-3.8-flash-medium"
    timeout_seconds: int = 120
    allow_tool_emulation: bool = False  # Section 15: strictly no ad-hoc prompt parsers
    allow_delegated_mcp: bool = True    # Phase H.3.2: constrained delegated read-only agent
    auth_token: Optional[str] = None
    auth_token_file: Optional[str] = "/srv/hermes/antigravity/secrets/bridge.token"
    supported_models: list = field(
        default_factory=lambda: [
            "gemini-3.8-flash-medium",
            "gemini-3.8-flash-high",
            "gemini-3.8-flash-low",
            "gemini-3.7-flash-medium",
            "gemini-3.6-flash-medium",
            "gemini-3.1-pro-high",
        ]
    )


def load_config(config_path: Optional[str] = None) -> BridgeConfig:
    """Load configuration from YAML file or environment variables."""
    cfg = BridgeConfig()

    # Fallback to local test binary path if production path doesn't exist
    if not os.path.exists(cfg.binary_path):
        legacy_path = os.path.expanduser("~/antigravity-test-bin/antigravity")
        if os.path.exists(legacy_path):
            cfg.binary_path = legacy_path

    path = config_path or os.environ.get("ANTIGRAVITY_BRIDGE_CONFIG")
    if path and os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
            if "host" in data:
                cfg.host = str(data["host"])
            if "port" in data:
                cfg.port = int(data["port"])
            if "binary_path" in data:
                cfg.binary_path = str(data["binary_path"])
            if "default_model" in data:
                cfg.default_model = str(data["default_model"])
            if "timeout_seconds" in data:
                cfg.timeout_seconds = int(data["timeout_seconds"])
            if "allow_tool_emulation" in data:
                cfg.allow_tool_emulation = bool(data["allow_tool_emulation"])
            if "allow_delegated_mcp" in data:
                cfg.allow_delegated_mcp = bool(data["allow_delegated_mcp"])
            if "auth_token" in data and data["auth_token"]:
                cfg.auth_token = str(data["auth_token"]).strip()
            if "auth_token_file" in data:
                cfg.auth_token_file = str(data["auth_token_file"])
            if "supported_models" in data and isinstance(data["supported_models"], list):
                cfg.supported_models = data["supported_models"]

    # Load token from file if not explicitly configured
    if not cfg.auth_token and cfg.auth_token_file and os.path.isfile(cfg.auth_token_file):
        try:
            with open(cfg.auth_token_file, "r", encoding="utf-8") as f:
                token_val = f.read().strip()
                if token_val:
                    cfg.auth_token = token_val
        except OSError:
            pass

    # Environment overrides
    if "ANTIGRAVITY_BRIDGE_HOST" in os.environ:
        cfg.host = os.environ["ANTIGRAVITY_BRIDGE_HOST"]
    if "ANTIGRAVITY_BRIDGE_PORT" in os.environ:
        try:
            cfg.port = int(os.environ["ANTIGRAVITY_BRIDGE_PORT"])
        except ValueError:
            pass
    if "ANTIGRAVITY_BIN" in os.environ:
        cfg.binary_path = os.environ["ANTIGRAVITY_BIN"]
    if "ANTIGRAVITY_DEFAULT_MODEL" in os.environ:
        cfg.default_model = os.environ["ANTIGRAVITY_DEFAULT_MODEL"]
    if "ANTIGRAVITY_ALLOW_DELEGATED_MCP" in os.environ:
        cfg.allow_delegated_mcp = os.environ["ANTIGRAVITY_ALLOW_DELEGATED_MCP"].lower() in {"1", "true", "yes"}
    if "ANTIGRAVITY_BRIDGE_AUTH_TOKEN" in os.environ:
        cfg.auth_token = os.environ["ANTIGRAVITY_BRIDGE_AUTH_TOKEN"].strip()
    elif "ANTIGRAVITY_BRIDGE_TOKEN" in os.environ:
        cfg.auth_token = os.environ["ANTIGRAVITY_BRIDGE_TOKEN"].strip()

    return cfg
