"""Unit tests for Antigravity Bridge Configuration."""

import os
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "antigravity-bridge", "app")))

from bridge_config import BridgeConfig, load_config


class TestBridgeConfig(unittest.TestCase):
    """Test configuration defaults and overrides."""

    def test_default_config(self):
        cfg = load_config()
        self.assertEqual(cfg.host, "0.0.0.0")
        self.assertEqual(cfg.port, 8765)
        self.assertEqual(cfg.default_model, "gemini-3.8-flash-medium")
        self.assertFalse(cfg.allow_tool_emulation)

    def test_env_overrides(self):
        with patch.dict(os.environ, {
            "ANTIGRAVITY_BRIDGE_HOST": "127.0.0.1",
            "ANTIGRAVITY_BRIDGE_PORT": "9999",
            "ANTIGRAVITY_DEFAULT_MODEL": "gemini-3.8-flash-high",
        }):
            cfg = load_config()
            self.assertEqual(cfg.host, "127.0.0.1")
            self.assertEqual(cfg.port, 9999)
            self.assertEqual(cfg.default_model, "gemini-3.8-flash-high")


if __name__ == "__main__":
    unittest.main()
