from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG_PATH = os.environ.get("GITHUB_CONFIG") or "/opt/data/tools/github/config.yaml"
DEFAULT_TOKEN_PATH = os.environ.get("GITHUB_TOKEN_PATH") or "/opt/data/tools/github/read-token"
DEFAULT_ALLOWED_REPOSITORIES = [
    "mastermk2/hermes-cos-infra",
    "mastermk2/hermes-pi-setup",
]


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class GitHubConfig:
    allowed_repositories: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOWED_REPOSITORIES))
    token_path: str = DEFAULT_TOKEN_PATH
    api_base_url: str = "https://api.github.com"
    timeout_seconds: float = 15.0
    file_content_max_bytes: int = 64_000
    default_limit: int = 10
    max_limit: int = 50

    def is_repo_allowed(self, repo: str) -> bool:
        norm = repo.strip().lower()
        return norm in [r.lower() for r in self.allowed_repositories]

    def read_token(self) -> str | None:
        token = os.environ.get("GITHUB_READ_TOKEN")
        if token and token.strip():
            return token.strip()
        env_token_path = os.environ.get("GITHUB_TOKEN_PATH", "").strip()
        candidates = []
        if env_token_path:
            candidates.append(Path(env_token_path))
        candidates.extend([
            Path(self.token_path),
            Path(self.token_path.replace("/opt/data", "/srv/hermes/hermes-data")),
            Path(self.token_path.replace("/srv/hermes/hermes-data", "/opt/data")),
        ])
        for p in candidates:
            try:
                if p.is_file():
                    t = p.read_text(encoding="utf-8").strip()
                    if t:
                        return t
            except OSError:
                continue
        return None


def parse_config(raw: dict[str, Any] | None) -> GitHubConfig:
    data = raw or {}

    # Refuse credentials directly embedded in config
    for key in ("token", "access_token", "api_key", "secret", "private_key"):
        if key in data:
            raise ConfigError(f"Credentials must not be embedded in configuration: found {key!r}")

    repos = data.get("allowed_repositories")
    if repos is not None:
        if not isinstance(repos, list) or not all(isinstance(r, str) and "/" in r for r in repos):
            raise ConfigError("allowed_repositories must be a list of 'owner/repo' strings")
        allowed = [r.strip().lower() for r in repos if r.strip()]
    else:
        allowed = list(DEFAULT_ALLOWED_REPOSITORIES)

    token_path = str(data.get("token_path") or DEFAULT_TOKEN_PATH).strip()
    api_base_url = str(data.get("api_base_url") or "https://api.github.com").strip().rstrip("/")
    timeout_seconds = float(data.get("timeout_seconds") or 15.0)
    file_content_max_bytes = int(data.get("file_content_max_bytes") or 64_000)
    default_limit = int(data.get("default_limit") or 10)
    max_limit = int(data.get("max_limit") or 50)

    return GitHubConfig(
        allowed_repositories=allowed,
        token_path=token_path,
        api_base_url=api_base_url,
        timeout_seconds=timeout_seconds,
        file_content_max_bytes=file_content_max_bytes,
        default_limit=default_limit,
        max_limit=max_limit,
    )


def load_config(path: str | None = None) -> GitHubConfig:
    target = Path(path or os.environ.get("GITHUB_CONFIG") or DEFAULT_CONFIG_PATH)
    if not target.is_file():
        return parse_config({})
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise ConfigError(f"Cannot read config {target}: {exc}") from exc
    return parse_config(raw)