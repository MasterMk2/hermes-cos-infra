#!/usr/bin/env python3
"""Item 30: secret patterns must be identical across all three consumers
(supervisor / bridge / workerctl). Any drift fails this test."""

import unittest

import helpers  # noqa: F401  (sys.path setup)

import bridge  # noqa: E402
import supervisor  # noqa: E402
import security_patterns  # noqa: E402
import workerctl_server  # noqa: E402


def _describe(patterns):
    return [(pid, rx.pattern) for pid, rx in patterns]


class TestSecretPatternSingleSource(unittest.TestCase):
    def test_all_consumers_use_canonical_patterns(self):
        canon = _describe(security_patterns.SECRET_PATTERNS)
        self.assertEqual(_describe(supervisor.SECRET_PATTERNS), canon)
        self.assertEqual(_describe(bridge.SECRET_PATTERNS), canon)
        self.assertEqual(_describe(workerctl_server.SECRET_PATTERNS), canon)

    def test_canonical_set_includes_authorization_header(self):
        ids = {pid for pid, _ in security_patterns.SECRET_PATTERNS}
        self.assertIn("authorization_header", ids)

    def test_scan_text_reports_ids_never_values(self):
        hits = security_patterns.scan_text('token = "ghp_' + "A" * 36 + '"')
        self.assertIn("github_pat", hits)
        self.assertIn("credential_assignment", hits)
        # no matched VALUES ever come back
        for h in hits:
            self.assertIsInstance(h, str)
            self.assertNotIn("ghp_", h)


if __name__ == "__main__":
    unittest.main()
