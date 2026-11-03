"""The classifier broker must send the token the bridge checks.

On 2026-10-28 the broker read Hermes's config.yaml instead, and stopped
starting once the hermes-data binds hid that file. These tests pin the
broker to the bridge's own token sources and their order.
"""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "line-knowledge"))
sys.path.insert(0, str(ROOT / "src" / "antigravity-bridge" / "app"))

import bridge_config  # noqa: E402
import event_broker  # noqa: E402


class LoadBearerTokenTests(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.yaml_path = self.dir / "bridge.yaml"
        self.default_file = self.dir / "default.token"
        for name, value in (("BRIDGE_CONFIG_PATH", self.yaml_path),
                            ("BRIDGE_TOKEN_FILE", str(self.default_file))):
            patcher = patch.object(event_broker, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for key in ("HERMES_BRIDGE_TOKEN", "ANTIGRAVITY_BRIDGE_AUTH_TOKEN", "ANTIGRAVITY_BRIDGE_TOKEN"):
            os.environ.pop(key, None)

    def write_yaml(self, text):
        self.yaml_path.write_text(text, encoding="utf-8")

    def write_token(self, path, value):
        Path(path).write_text(value + "\n", encoding="utf-8")

    def test_environment_comes_first(self):
        os.environ["HERMES_BRIDGE_TOKEN"] = "from-env"
        self.write_yaml("auth_token: from-yaml\n")
        self.assertEqual(event_broker.load_bearer_token(), "from-env")

    def test_auth_token_in_bridge_yaml(self):
        self.write_yaml("auth_token: ' from-yaml '\n")
        self.write_token(self.default_file, "from-file")
        self.assertEqual(event_broker.load_bearer_token(), "from-yaml")

    def test_auth_token_file_named_in_bridge_yaml(self):
        named = self.dir / "named.token"
        self.write_token(named, "from-named-file")
        self.write_token(self.default_file, "from-default-file")
        self.write_yaml(f"auth_token_file: {named}\n")
        self.assertEqual(event_broker.load_bearer_token(), "from-named-file")

    def test_default_token_file_when_bridge_yaml_names_none(self):
        self.write_yaml("port: 8765\n")
        self.write_token(self.default_file, "from-default-file")
        self.assertEqual(event_broker.load_bearer_token(), "from-default-file")

    def test_default_token_file_when_bridge_yaml_is_absent(self):
        self.write_token(self.default_file, "from-default-file")
        self.assertEqual(event_broker.load_bearer_token(), "from-default-file")

    def test_blank_auth_token_falls_through_to_the_file(self):
        self.write_yaml("auth_token: '   '\n")
        self.write_token(self.default_file, "from-default-file")
        self.assertEqual(event_broker.load_bearer_token(), "from-default-file")

    def test_unreadable_bridge_yaml_falls_back_to_the_default_file(self):
        # The host case: bridge.yaml is 0640 for a group the broker lacks.
        self.write_yaml("auth_token_file: /elsewhere/bridge.token\n")
        self.write_token(self.default_file, "from-default-file")
        real_open = open

        def denying_open(file, *args, **kwargs):
            if str(file) == str(self.yaml_path):
                raise PermissionError(13, "Permission denied", str(file))
            return real_open(file, *args, **kwargs)

        with patch("builtins.open", denying_open):
            self.assertEqual(event_broker.load_bearer_token(), "from-default-file")

    def test_untraversable_config_dir_falls_back_to_the_default_file(self):
        # Python 3.13's Path.is_file raises on EACCES; 3.14 returns False.
        self.write_token(self.default_file, "from-default-file")
        with patch.object(type(self.yaml_path), "is_file",
                          side_effect=PermissionError(13, "Permission denied")):
            self.assertEqual(event_broker.load_bearer_token(), "from-default-file")

    def test_missing_token_file_is_an_error(self):
        self.write_yaml("port: 8765\n")
        with self.assertRaisesRegex(RuntimeError, "bridge token file"):
            event_broker.load_bearer_token()

    def test_empty_token_file_is_an_error(self):
        self.default_file.write_text("\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "empty"):
            event_broker.load_bearer_token()

    def test_unreadable_bridge_yaml_is_an_error(self):
        self.write_yaml("auth_token: [unclosed\n")
        self.write_token(self.default_file, "from-default-file")
        with self.assertRaisesRegex(RuntimeError, "Could not read"):
            event_broker.load_bearer_token()

    def test_hermes_config_yaml_is_not_a_source(self):
        # Only the bridge's sources are read, whatever else exists on the host.
        self.write_yaml("port: 8765\n")
        opened = []
        real_open = open

        def recording_open(file, *args, **kwargs):
            opened.append(str(file))
            return real_open(file, *args, **kwargs)

        self.write_token(self.default_file, "from-default-file")
        with patch("builtins.open", recording_open):
            event_broker.load_bearer_token()
        self.assertEqual(opened, [str(self.yaml_path), str(self.default_file)])

    def test_agrees_with_the_bridge(self):
        named = self.dir / "named.token"
        self.write_token(named, "from-named-file")
        self.write_token(self.default_file, "from-default-file")
        cases = {
            "auth_token in yaml": "auth_token: from-yaml\n",
            "auth_token_file in yaml": f"auth_token_file: {named}\n",
            "blank auth_token": f"auth_token: ''\nauth_token_file: {named}\n",
        }
        for label, text in cases.items():
            with self.subTest(label):
                self.write_yaml(text)
                bridge = bridge_config.load_config(str(self.yaml_path))
                self.assertEqual(event_broker.load_bearer_token(), bridge.auth_token)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
