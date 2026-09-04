#!/usr/bin/env python3
"""G.1 unit tests — configuration refuses what it must.

The configuration file is reviewable and lives in the repository's shape, so
everything dangerous is kept out of it by parse-time refusal rather than by
convention: no credential, no wildcard bind, no route to the internet
(spec sections 16, 22, 24, 25, 105).
"""

import os
import tempfile
import unittest

import gw_fixtures as fx
from gw_config import (
    SECRET_MISSING,
    SECRET_NAMES,
    SECRET_UNREADABLE,
    Config,
    ConfigError,
    load_secret,
    parse_config,
    probe_secret,
    secret_status,
)


class TestListenHost(unittest.TestCase):
    def _host(self, value):
        return parse_config(fx.config_dict(line={"listen_host": value}))

    def test_loopback_allowed(self):
        for host in ("127.0.0.1", "::1", "localhost"):
            with self.subTest(host=host):
                self.assertEqual(self._host(host).line["listen_host"], host)

    def test_private_lan_address_allowed(self):
        """The reverse proxy for this deployment is on another machine.

        Measured: the TLS terminator is a container on a different host on
        the same LAN, so a loopback-only bind is unreachable from it. A named
        private address is the correct surface.
        """
        for host in ("192.168.1.10", "10.0.0.5", "172.16.4.4"):
            with self.subTest(host=host):
                self.assertEqual(self._host(host).line["listen_host"], host)

    def test_wildcard_refused(self):
        """0.0.0.0 binds interfaces that do not exist yet."""
        for host in ("0.0.0.0", "::"):
            with self.subTest(host=host):
                with self.assertRaises(ConfigError):
                    self._host(host)

    def test_publicly_routable_address_refused(self):
        for host in ("8.8.8.8", "1.1.1.1", "2001:4860:4860::8888"):
            with self.subTest(host=host):
                with self.assertRaises(ConfigError):
                    self._host(host)

    def test_non_routable_reserved_ranges_are_allowed(self):
        """The rule is 'not reachable from the internet', not 'RFC1918'.

        203.0.113.0/24 is the RFC 5737 documentation range: not private in
        the colloquial sense, but not globally routable either, so binding it
        is harmless and refusing it would be arbitrary.
        """
        for host in ("203.0.113.7", "169.254.1.1", "100.64.0.1"):
            with self.subTest(host=host):
                self.assertEqual(self._host(host).line["listen_host"], host)

    def test_nonsense_refused(self):
        for host in ("example.com", "", "not an address"):
            with self.subTest(host=host):
                with self.assertRaises(ConfigError):
                    self._host(host)

    def test_webhook_path_must_be_a_path(self):
        with self.assertRaises(ConfigError):
            parse_config(fx.config_dict(line={"webhook_path": "line/webhook"}))


class TestNoCredentialsInConfig(unittest.TestCase):
    def test_top_level_credential_keys_refused(self):
        for key in ("channel_secret", "access_token", "token", "api_key"):
            with self.subTest(key=key):
                with self.assertRaises(ConfigError):
                    parse_config(fx.config_dict(**{key: "s3cret"}))

    def test_inline_secret_value_refused(self):
        for field in ("value", "secret", "token"):
            with self.subTest(field=field):
                with self.assertRaises(ConfigError):
                    parse_config(fx.config_dict(
                        secrets={"line_channel_secret": {field: "s3cret"}}
                    ))

    def test_unknown_secret_names_refused(self):
        with self.assertRaises(ConfigError):
            parse_config(fx.config_dict(secrets={"aws_key": {}}))

    def test_all_four_credentials_have_refs(self):
        cfg = parse_config(fx.config_dict())
        self.assertEqual(set(cfg.secrets), set(SECRET_NAMES))
        for ref in cfg.secrets.values():
            self.assertTrue(ref.credential_name)
            self.assertTrue(ref.fallback_file.startswith("/"))


class TestHermesReachability(unittest.TestCase):
    def test_lan_and_loopback_allowed(self):
        for url in ("http://127.0.0.1:8642", "http://192.168.1.10:8642",
                    "http://10.1.2.3:8642", "http://172.16.0.9:8642"):
            with self.subTest(url=url):
                parse_config(fx.config_dict(hermes={"base_url": url}))

    def test_internet_refused(self):
        """The assistant's API is never reached across the internet."""
        for url in ("https://api.example.com", "http://8.8.8.8:8642"):
            with self.subTest(url=url):
                with self.assertRaises(ConfigError):
                    parse_config(fx.config_dict(hermes={"base_url": url}))

    def test_unsubstituted_placeholder_refused(self):
        """The sample ships a placeholder because the right value is per-host.

        A plausible default was measurably wrong on the target host — the API
        publishes its port on the LAN address only, so 127.0.0.1 refused every
        connection and every answer would have failed at runtime. Refusing the
        placeholder makes the operator supply the measured value.
        """
        with self.assertRaises(ConfigError) as ctx:
            parse_config(fx.config_dict(
                hermes={"base_url": "http://REPLACE_WITH_THIS_HOST_LAN_IP:8642"}
            ))
        self.assertIn("placeholder", str(ctx.exception))


class TestLimits(unittest.TestCase):
    def test_concurrency_is_bounded_for_this_host(self):
        """3.7 GiB of RAM shared with Hermes; unbounded fan-out is not an
        option even if the operator asks for it."""
        for value in (0, 9, 100):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError):
                    parse_config(fx.config_dict(limits={"max_active_requests": value}))

    def test_reasonable_values_accepted(self):
        cfg = parse_config(fx.config_dict(limits={"max_active_requests": 2}))
        self.assertEqual(cfg.limits["max_active_requests"], 2)


class TestReadiness(unittest.TestCase):
    def test_platform_without_owner_is_not_ready(self):
        cfg = parse_config(fx.config_dict(owner={"line_user_id": None}))
        self.assertFalse(cfg.line_ready)

    def test_disabled_platform_is_not_ready(self):
        cfg = parse_config(fx.config_dict(line={"enabled": False}))
        self.assertFalse(cfg.line_ready)

    def test_nothing_configured_means_no_channel(self):
        cfg = parse_config({"line": {"enabled": False}, "discord": {"enabled": False}})
        self.assertFalse(cfg.any_channel_ready)


class TestSecretLoading(unittest.TestCase):
    def test_credential_directory_wins(self):
        cfg = parse_config(fx.config_dict())
        with tempfile.TemporaryDirectory() as creds:
            path = os.path.join(creds, "line_channel_secret")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("from-credential\n")
            old = os.environ.get("CREDENTIALS_DIRECTORY")
            os.environ["CREDENTIALS_DIRECTORY"] = creds
            try:
                self.assertEqual(
                    load_secret(cfg, "line_channel_secret"), "from-credential"
                )
            finally:
                if old is None:
                    os.environ.pop("CREDENTIALS_DIRECTORY", None)
                else:
                    os.environ["CREDENTIALS_DIRECTORY"] = old

    def test_placeholder_file_is_not_a_secret(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sec")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("REPLACE_ME\n")
            cfg = parse_config(fx.config_dict(
                secrets={"line_channel_secret": {"fallback_file": path}}
            ))
            os.environ.pop("CREDENTIALS_DIRECTORY", None)
            self.assertIsNone(
                load_secret(cfg, "line_channel_secret", required=False)
            )

    def test_missing_required_secret_raises(self):
        cfg = parse_config(fx.config_dict(
            secrets={"line_channel_secret": {"fallback_file": "/nonexistent/x"}}
        ))
        os.environ.pop("CREDENTIALS_DIRECTORY", None)
        with self.assertRaises(ConfigError):
            load_secret(cfg, "line_channel_secret")

    def test_unknown_secret_name_raises(self):
        cfg = parse_config(fx.config_dict())
        with self.assertRaises(ConfigError):
            load_secret(cfg, "not_a_secret")


class TestSecretPermissionHandling(unittest.TestCase):
    """A credential directory closed to this user must not crash the process.

    `/etc/hermes-secrets` is 0700 root:root by design — the service user is
    supposed to be unable to read it, because systemd hands the service its
    own copies. Python 3.13's `Path.is_file()` propagates the resulting
    PermissionError rather than returning False, which turned the intended
    clean configuration error into a traceback out of `--check`. Observed on
    the target host, 2026-09-04.
    """

    @staticmethod
    def _with_eacces(fn):
        import pathlib

        real = pathlib.Path.is_file

        def boom(self, *a, **k):
            raise PermissionError(13, "Permission denied")

        pathlib.Path.is_file = boom
        try:
            return fn()
        finally:
            pathlib.Path.is_file = real

    def setUp(self):
        os.environ.pop("CREDENTIALS_DIRECTORY", None)

    def test_probe_returns_unreadable_instead_of_raising(self):
        from pathlib import Path

        status, value = self._with_eacces(
            lambda: probe_secret(Path("/etc/hermes-secrets/whatever"))
        )
        self.assertEqual(status, SECRET_UNREADABLE)
        self.assertIsNone(value)

    def test_load_secret_raises_config_error_not_permission_error(self):
        cfg = parse_config(fx.config_dict())
        with self.assertRaises(ConfigError):
            self._with_eacces(lambda: load_secret(cfg, "hermes_api_key"))

    def test_load_secret_optional_returns_none(self):
        cfg = parse_config(fx.config_dict())
        self.assertIsNone(
            self._with_eacces(
                lambda: load_secret(cfg, "hermes_api_key", required=False)
            )
        )

    def test_status_reports_unreadable(self):
        cfg = parse_config(fx.config_dict())
        status, _where = self._with_eacces(
            lambda: secret_status(cfg, "hermes_api_key")
        )
        self.assertEqual(status, SECRET_UNREADABLE)

    def test_absent_file_is_missing_not_unreadable(self):
        """The two must stay distinguishable: one is a fault, one is expected."""
        cfg = parse_config(fx.config_dict(
            secrets={"hermes_api_key": {"fallback_file": "/nonexistent/dir/key"}}
        ))
        status, _where = secret_status(cfg, "hermes_api_key")
        self.assertEqual(status, SECRET_MISSING)

    def test_readable_file_is_readable(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "key")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("a-value\n")
            cfg = parse_config(fx.config_dict(
                secrets={"hermes_api_key": {"fallback_file": path}}
            ))
            status, _where = secret_status(cfg, "hermes_api_key")
            self.assertEqual(status, "readable")
            self.assertEqual(load_secret(cfg, "hermes_api_key"), "a-value")


class TestConfigCarriesNoSecret(unittest.TestCase):
    def test_repr_of_config_has_no_raw_dump(self):
        """`raw` is repr=False so a logged Config cannot spill the file."""
        cfg = parse_config(fx.config_dict())
        self.assertNotIn("raw=", repr(cfg))
        self.assertIsInstance(cfg, Config)


if __name__ == "__main__":
    unittest.main(verbosity=2)
