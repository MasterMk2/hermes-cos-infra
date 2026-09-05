#!/usr/bin/env python3
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "gmail" / "app"))
import gm_integrate_hermes as integrate


class HermesIntegrationTest(unittest.TestCase):
    def test_bounded_idempotent_edits_preserve_existing_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = base / "config.yaml"
            compose = base / "docker-compose.yaml"
            agents = base / "AGENTS.md"
            config.write_text("mcp_servers:\n  calendar:\n    command: cal\nother:\n  value: 1\n", encoding="utf-8")
            compose.write_text("services:\n  hermes:\n    volumes:\n      - /srv/hermes/hermes-data:/opt/data\n", encoding="utf-8")
            agents.write_text("# Rules\n\nKeep existing instruction.\n", encoding="utf-8")
            changed = integrate.apply(config, compose, agents)
            self.assertTrue(all(changed.values()))
            self.assertIn("  gmail:\n", config.read_text(encoding="utf-8"))
            self.assertIn("/srv/hermes/gmail/index:/opt/gmail:ro", compose.read_text(encoding="utf-8"))
            text = agents.read_text(encoding="utf-8")
            self.assertIn("Gmail evidence authority (H.2)", text)
            self.assertIn("untrusted", text)
            again = integrate.apply(config, compose, agents)
            self.assertFalse(any(again.values()))


if __name__ == "__main__":
    unittest.main()
