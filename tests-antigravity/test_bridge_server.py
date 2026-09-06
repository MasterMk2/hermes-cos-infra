"""Unit tests for Antigravity Provider Bridge (Phase H.3.1 Section 22)."""

import json
import subprocess
import unittest
from unittest.mock import MagicMock, patch

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "antigravity-bridge", "app")))

from bridge_config import BridgeConfig
from bridge_server import (
    classify_cli_error,
    execute_antigravity,
    format_messages_to_prompt,
    redact_secrets,
)


class TestBridgeFormattingAndRedaction(unittest.TestCase):
    """Test prompt formatting and secret redaction."""

    def test_credential_redaction(self):
        text = "Bearer ya29.a0AfH6SMD123 ghp_1234567890abcdef1234567890abcdef1234 4/0ATsMZqDcS__PUcVnI0BiokVUR6ty5EtVzNugmiWBRjxI7VJKl7L04g2xce4dDmzjV3cWOw"
        redacted = redact_secrets(text)
        self.assertNotIn("ghp_", redacted)
        self.assertNotIn("ya29.", redacted)
        self.assertNotIn("4/0ATsMZqDcS", redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_message_formatting(self):
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello world"},
            {"role": "assistant", "content": "Greetings!"},
            {"role": "tool", "tool_call_id": "call_1", "content": "commit: abc1234"},
        ]
        prompt = format_messages_to_prompt(messages)
        self.assertIn("You are a helpful assistant.", prompt)
        self.assertIn("User: Hello world", prompt)
        self.assertIn("Assistant: Greetings!", prompt)
        self.assertIn("[Tool Result (call_1)]: commit: abc1234", prompt)


class TestErrorClassification(unittest.TestCase):
    """Test CLI error classification into HTTP codes and failover categories."""

    def test_quota_429_classification(self):
        code, err_type, msg = classify_cli_error(1, "", "Error: resource exhausted (quota exceeded)")
        self.assertEqual(code, 429)
        self.assertEqual(err_type, "insufficient_quota")

        code, err_type, msg = classify_cli_error(1, "", "rate limit 429 too many requests")
        self.assertEqual(code, 429)

    def test_auth_expiration_401_classification(self):
        code, err_type, msg = classify_cli_error(1, "", "Error: You are not logged into Antigravity.")
        self.assertEqual(code, 401)
        self.assertEqual(err_type, "authentication_error")

    def test_context_overflow_400_classification(self):
        code, err_type, msg = classify_cli_error(1, "", "maximum context length is 8192 tokens")
        self.assertEqual(code, 400)
        self.assertEqual(err_type, "context_length_exceeded")

    def test_timeout_504_classification(self):
        code, err_type, msg = classify_cli_error(124, "", "Command timed out")
        self.assertEqual(code, 504)
        self.assertEqual(err_type, "timeout")

    def test_server_overloaded_503_classification(self):
        code, err_type, msg = classify_cli_error(1, "", "Service unavailable / overloaded")
        self.assertEqual(code, 503)
        self.assertEqual(err_type, "service_unavailable")

    def test_generic_crash_500_classification(self):
        code, err_type, msg = classify_cli_error(2, "", "Segmentation fault")
        self.assertEqual(code, 500)
        self.assertEqual(err_type, "api_error")


class TestAntigravityExecution(unittest.TestCase):
    """Test CLI subprocess execution handling."""

    def setUp(self):
        self.cfg = BridgeConfig(
            binary_path="/usr/bin/antigravity",
            default_model="gemini-3.8-flash-medium",
            timeout_seconds=5,
        )

    @patch("subprocess.run")
    def test_successful_execution(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = json.dumps({
            "status": "SUCCESS",
            "response": "Hello from Gemini 3.8 Flash!",
            "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}
        })
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc

        code, res = execute_antigravity("Hello", self.cfg)
        self.assertEqual(code, 200)
        self.assertEqual(res["model"], "gemini-3.8-flash-medium")
        self.assertEqual(res["choices"][0]["message"]["content"], "Hello from Gemini 3.8 Flash!")
        self.assertEqual(res["usage"]["total_tokens"], 120)

    @patch("subprocess.run")
    def test_malformed_json_handling(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "NOT JSON TEXT"
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc

        code, res = execute_antigravity("Hello", self.cfg)
        self.assertEqual(code, 502)
        self.assertIn("error", res)
        self.assertEqual(res["error"]["type"], "invalid_response")

    @patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="antigravity", timeout=5))
    def test_timeout_handling(self, mock_run):
        code, res = execute_antigravity("Hello", self.cfg)
        self.assertEqual(code, 504)
        self.assertEqual(res["error"]["type"], "timeout")


if __name__ == "__main__":
    unittest.main()
