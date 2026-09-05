#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "gmail" / "app"))
sys.path.insert(0, str(ROOT / "src" / "gmail" / "app"))

import gm_oauth


class _ValidatedClient:
    def __init__(self, credentials):
        self.credentials = credentials
        self.calls = []

    def get_profile(self):
        self.calls.append("profile")
        return {"historyId": "opaque"}

    def list_messages(self, **kwargs):
        self.calls.append(("list", kwargs))
        return {"messages": [{"id": "opaque"}]}

    def get_message(self, message_id, **kwargs):
        self.calls.append(("get", message_id, kwargs))
        return {"id": message_id}


class GmailOAuthTest(unittest.TestCase):
    def test_accepts_expected_or_omitted_optional_scope_response(self):
        self.assertTrue(gm_oauth._scope_is_acceptable({"scope": "https://www.googleapis.com/auth/gmail.readonly"}))
        self.assertTrue(gm_oauth._scope_is_acceptable({}))

    def test_rejects_a_reported_reduced_scope(self):
        self.assertFalse(gm_oauth._scope_is_acceptable({"scope": "https://www.googleapis.com/auth/gmail.metadata"}))

    def test_new_grant_is_validated_without_retaining_message_data(self):
        original = gm_oauth.GmailClient
        client = None
        try:
            def factory(credentials):
                nonlocal client
                client = _ValidatedClient(credentials)
                return client
            gm_oauth.GmailClient = factory
            gm_oauth._validate_readonly_grant(gm_oauth.OAuthCredentials("client", "refresh"))
        finally:
            gm_oauth.GmailClient = original
        self.assertEqual(client.calls, [
            "profile",
            ("list", {"maxResults": 1, "includeSpamTrash": False}),
            ("get", "opaque", {"format": "full"}),
        ])


if __name__ == "__main__":
    unittest.main()
