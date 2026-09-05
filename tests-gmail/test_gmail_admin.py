#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "gmail" / "app"))


def load(name: str):
    path = ROOT / "src" / "gmail" / "bin" / name
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


credentials = load("install-credentials.py")
pubsub = load("configure-pubsub.py")


class GmailAdminTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_installs_separate_validated_credential_files(self):
        oauth = self.root / "oauth.json"
        oauth.write_text(json.dumps({"installed": {"client_id": "test-client", "client_secret": "test-secret"}}), encoding="utf-8")
        service_account = self.root / "pubsub.json"
        service_account.write_text(json.dumps({"type": "service_account", "client_email": "pull@test.invalid", "private_key": "test-private"}), encoding="utf-8")
        destination = self.root / "secrets"
        credentials.install(oauth, service_account, destination=destination)
        self.assertEqual((destination / "client-id").read_text(encoding="utf-8").strip(), "test-client")
        self.assertEqual((destination / "client-secret").read_text(encoding="utf-8").strip(), "test-secret")
        self.assertEqual(json.loads((destination / "pubsub-service-account.json").read_text(encoding="utf-8"))["type"], "service_account")
        if os.name != "nt":
            self.assertEqual((destination / "client-id").stat().st_mode & 0o777, 0o400)

    def test_rejects_web_oauth_client_and_cross_project_resources(self):
        oauth = self.root / "oauth.json"
        oauth.write_text(json.dumps({"web": {"client_id": "test", "client_secret": "test"}}), encoding="utf-8")
        service_account = self.root / "pubsub.json"
        service_account.write_text(json.dumps({"type": "service_account", "client_email": "pull@test.invalid", "private_key": "test-private"}), encoding="utf-8")
        with self.assertRaises(credentials.CredentialInputError):
            credentials.install(oauth, service_account, destination=self.root / "secrets")
        config = self.root / "config.yaml"
        config.write_text("pubsub: {}\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            pubsub.configure(config, "projects/a/topics/t", "projects/b/subscriptions/s")

    def test_root_installer_avoids_recursive_venv_chown_and_limits_index_readers(self):
        installer = (ROOT / "src" / "gmail" / "install-root.sh").read_text(encoding="utf-8")
        attention_unit = (ROOT / "src" / "attention" / "systemd" / "attention-engine.service.in").read_text(encoding="utf-8")
        self.assertNotIn('chown -R root:root "$ROOT/venv"', installer)
        self.assertIn('install -d -m 0755 -o root -g root "$ROOT/venv"', installer)
        self.assertIn('install -d -m 2750 -o hermesgmail -g hermesreconcile "$ROOT/index"', installer)
        self.assertIn('chmod 0440 "$ROOT/index/index.db"', installer)
        self.assertIn("SupplementaryGroups=hermesreconcile", attention_unit)

    def test_line_refresh_timer_is_nonprivileged_and_uses_only_the_ssh_boundary(self):
        installer = (ROOT / "src" / "line-knowledge" / "install-root.sh").read_text(encoding="utf-8")
        unit = (ROOT / "src" / "line-knowledge" / "systemd" / "hermes-line-refresh.service.in").read_text(encoding="utf-8")
        timer = (ROOT / "src" / "line-knowledge" / "systemd" / "hermes-line-refresh.timer").read_text(encoding="utf-8")
        self.assertIn("User=@ENGINE_USER@", unit)
        self.assertIn("ProtectHome=tmpfs", unit)
        self.assertIn("BindReadOnlyPaths=@ENGINE_HOME@/.ssh", unit)
        self.assertIn("InaccessiblePaths=-/etc/hermes-secrets", unit)
        self.assertIn("OnCalendar=*-*-* 01:30:00", timer)
        self.assertIn("systemctl enable --now hermes-line-refresh.timer", installer)


if __name__ == "__main__":
    unittest.main()
