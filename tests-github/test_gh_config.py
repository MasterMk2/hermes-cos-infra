from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "src" / "github" / "app"))

from gh_config import ConfigError, GitHubConfig, load_config, parse_config


class TestGitHubConfig(unittest.TestCase):
    def test_default_config(self):
        cfg = parse_config({})
        self.assertTrue(cfg.is_repo_allowed("MasterMk2/hermes-cos-infra"))
        self.assertTrue(cfg.is_repo_allowed("mastermk2/hermes-cos-infra"))
        self.assertFalse(cfg.is_repo_allowed("other/repo"))

    def test_explicit_allowlist(self):
        cfg = parse_config({"allowed_repositories": ["Org/Repo1", "Org/Repo2"]})
        self.assertTrue(cfg.is_repo_allowed("org/repo1"))
        self.assertTrue(cfg.is_repo_allowed("ORG/REPO2"))
        self.assertFalse(cfg.is_repo_allowed("mastermk2/hermes-cos-infra"))

    def test_rejects_credentials_in_config(self):
        with self.assertRaises(ConfigError):
            parse_config({"token": "ghp_secret"})
        with self.assertRaises(ConfigError):
            parse_config({"access_token": "ghp_secret"})
        with self.assertRaises(ConfigError):
            parse_config({"api_key": "ghp_secret"})

    def test_rejects_malformed_repositories(self):
        with self.assertRaises(ConfigError):
            parse_config({"allowed_repositories": "not-a-list"})
        with self.assertRaises(ConfigError):
            parse_config({"allowed_repositories": ["no-slash"]})

    def test_read_token_from_file_and_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "token"
            token_file.write_text("test-token-file\n", encoding="utf-8")
            cfg = parse_config({"token_path": str(token_file)})
            
            # Reads from file
            self.assertEqual(cfg.read_token(), "test-token-file")

            # Environment variable overrides
            os.environ["GITHUB_READ_TOKEN"] = "env-override-token"
            try:
                self.assertEqual(cfg.read_token(), "env-override-token")
            finally:
                del os.environ["GITHUB_READ_TOKEN"]

    def test_read_token_fallback_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            host_dir = Path(tmp) / "srv" / "hermes" / "hermes-data" / "tools" / "github"
            host_dir.mkdir(parents=True)
            token_file = host_dir / "read-token"
            token_file.write_text("my-host-token", encoding="utf-8")

            # Config configured with container path /opt/data
            cfg = parse_config({"token_path": "/opt/data/tools/github/read-token"})
            # Override host candidate path test via GITHUB_TOKEN_PATH
            os.environ["GITHUB_TOKEN_PATH"] = str(token_file)
            try:
                self.assertEqual(cfg.read_token(), "my-host-token")
            finally:
                del os.environ["GITHUB_TOKEN_PATH"]


if __name__ == "__main__":
    unittest.main()