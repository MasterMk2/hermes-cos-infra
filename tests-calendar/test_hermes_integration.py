from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import cal_integrate_hermes as integration


class HermesIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.config = root / "config.yaml"
        self.compose = root / "docker-compose.yaml"
        self.agents = root / "AGENTS.md"
        self.config.write_text("model: {}\nmcp_servers:\n  existing:\n    command: existing\nterminal: {}\n", encoding="utf-8")
        self.compose.write_text("services:\n  hermes:\n    volumes:\n      - /srv/hermes/hermes-data:/opt/data\n", encoding="utf-8")
        self.agents.write_text("# rules\n", encoding="utf-8")

    def test_bounded_idempotent_edits_preserve_existing_content(self):
        before_modes = [os.stat(path).st_mode & 0o777 for path in (self.config, self.compose, self.agents)]
        changed = integration.apply(self.config, self.compose, self.agents)
        self.assertEqual(list(changed.values()), [True, True, True])
        self.assertIn("existing:\n    command: existing", self.config.read_text(encoding="utf-8"))
        self.assertIn("calendar:\n    command: /opt/hermes/.venv/bin/python3", self.config.read_text(encoding="utf-8"))
        self.assertIn("/srv/hermes/calendar/index:/opt/calendar:ro", self.compose.read_text(encoding="utf-8"))
        self.assertIn(integration.AGENTS_MARKER, self.agents.read_text(encoding="utf-8"))
        self.assertEqual(list(integration.apply(self.config, self.compose, self.agents).values()), [False, False, False])
        self.assertEqual(before_modes, [os.stat(path).st_mode & 0o777 for path in (self.config, self.compose, self.agents)])
