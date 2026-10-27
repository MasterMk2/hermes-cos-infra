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
        # privacy-ok: hand-written dummy credentials whose only purpose is to
        # prove `redact_secrets` scrubs them. The two assertions below fail if
        # anyone replaces them with something the redactor does not catch.
        text = "Bearer ya29.a0AfH6SMD123 ghp_1234567890abcdef1234567890abcdef1234 4/0ATsMZqDcS__PUcVnI0BiokVUR6ty5EtVzNugmiWBRjxI7VJKl7L04g2xce4dDmzjV3cWOw"  # privacy-ok
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

    @patch("bridge_server.run_cli")
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

    @patch("bridge_server.run_cli")
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

    @patch("bridge_server.run_cli")
    def test_denied_actions_handling(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = json.dumps({
            "status": "SUCCESS",
            "response": "",
            "denied_actions": [{"action": "command", "display_name": "RunCommand"}],
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
        })
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc

        code, res = execute_antigravity("Run a shell command", self.cfg)
        self.assertEqual(code, 200)
        self.assertIn("strictly read-only assistant", res["choices"][0]["message"]["content"])
        self.assertIn("RunCommand", res["choices"][0]["message"]["content"])


class TestCliObservability(unittest.TestCase):
    """What the bridge leaves behind for diagnosing a slow Hermes turn."""

    def setUp(self):
        self.cfg = BridgeConfig(binary_path="/sensitive/host/path/antigravity",
                                default_model="gemini-3.8-flash-medium", timeout_seconds=5)

    @patch("bridge_server.run_cli")
    def test_every_call_logs_one_content_free_line(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = json.dumps({"response": "ok", "usage": {}})
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc
        prompt = "User: \u79d8\u5bc6\u306e\u76f8\u8ac7"
        with self.assertLogs("antigravity-bridge", level="INFO") as caught:
            code, _ = execute_antigravity(prompt, self.cfg)
        self.assertEqual(code, 200)
        calls = [line for line in caught.output if "cli call" in line]
        self.assertEqual(len(calls), 1)
        self.assertIn("status=200 outcome=ok", calls[0])
        self.assertIn(f"prompt_bytes={len(prompt.encode('utf-8'))}", calls[0])
        self.assertNotIn(prompt, "\n".join(caught.output))

    @patch("bridge_server.run_cli")
    def test_a_timeout_logs_stderr_flags_not_stderr(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(
            ["antigravity"], 5, stderr="429 rate limit, retrying: User: private words")
        with self.assertLogs("antigravity-bridge", level="INFO") as caught:
            code, res = execute_antigravity("User: private words", self.cfg)
        self.assertEqual(code, 504)
        self.assertEqual(res["error"]["type"], "timeout")
        text = "\n".join(caught.output)
        self.assertIn("timed out after 5s (stderr flags=rate_limited,retrying", text)
        self.assertIn("outcome=timeout", text)
        self.assertNotIn("private words", text)

    def test_an_oversized_prompt_is_reported_as_such_without_the_binary_path(self):
        cfg = BridgeConfig(binary_path="/bin/true", timeout_seconds=5)
        prompt = "x" * 200000          # one argv string over MAX_ARG_STRLEN
        code, res = execute_antigravity(prompt, cfg)
        self.assertEqual(code, 500)    # unchanged, so Hermes still falls back
        self.assertEqual(res["error"]["type"], "prompt_too_large")
        self.assertIn("200", res["error"]["message"])
        self.assertNotIn("/bin/true", res["error"]["message"])

    @patch("bridge_server.run_cli")
    def test_other_exec_failures_do_not_leak_the_binary_path(self, mock_run):
        mock_run.side_effect = FileNotFoundError(2, "No such file or directory",
                                                 "/sensitive/host/path/antigravity")
        code, res = execute_antigravity("hi", self.cfg)
        self.assertEqual(code, 500)
        self.assertNotIn("/sensitive", res["error"]["message"])

    @unittest.skipUnless(os.path.exists("/bin/sh"), "needs /bin/sh")
    def test_a_timeout_mid_character_is_still_a_timeout(self):
        from bridge_server import run_cli

        script = "printf 'progress \\343\\201' >&2; sleep 30"
        with patch("bridge_server.run_cli",
                   lambda cmd, timeout: run_cli(["/bin/sh", "-c", script], 1)):
            code, res = execute_antigravity("hi", self.cfg)
        self.assertEqual(code, 504)
        self.assertEqual(res["error"]["type"], "timeout")

    @unittest.skipUnless(os.path.exists("/bin/sh") and os.path.isdir("/proc"), "needs /bin/sh and /proc")
    def test_a_timeout_kills_what_the_cli_started(self):
        import tempfile
        import time
        from bridge_server import run_cli

        with tempfile.TemporaryDirectory() as tmp:
            pidfile = os.path.join(tmp, "pid")
            script = f"sleep 60 & echo $! > {pidfile}; wait"
            started = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                run_cli(["/bin/sh", "-c", script], 1)
            self.assertLess(time.monotonic() - started, 30)
            with open(pidfile) as fh:
                child = int(fh.read().strip())

        def alive(pid):
            try:
                with open(f"/proc/{pid}/stat") as fh:
                    return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
            except FileNotFoundError:
                return False

        deadline = time.monotonic() + 5
        while alive(child) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(alive(child), "the CLI's own child outlived the timeout")


class TestRequestHandlerDelegation(unittest.TestCase):
    """Test HTTP handler tool delegation and fallback logic."""

    def test_handler_rejects_tools_when_delegation_disabled(self):
        from bridge_server import AntigravityRequestHandler
        from io import BytesIO

        cfg = BridgeConfig(allow_delegated_mcp=False, allow_tool_emulation=False)
        AntigravityRequestHandler.config = cfg

        body = json.dumps({
            "messages": [{"role": "user", "content": "Check commit"}],
            "tools": [{"type": "function", "function": {"name": "github_list_commits"}}]
        }).encode("utf-8")

        mock_rfile = BytesIO(body)
        mock_wfile = BytesIO()

        # Instantiate mock handler
        handler = MagicMock()
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = mock_rfile
        handler.wfile = mock_wfile
        handler.path = "/v1/chat/completions"
        handler.config = cfg

        # Call do_POST logic
        AntigravityRequestHandler.do_POST(handler)
        handler._send_json.assert_called_once()
        args = handler._send_json.call_args[0]
        status_code = args[0]
        data = args[1]
        self.assertEqual(status_code, 501)
        self.assertEqual(data["error"]["type"], "unsupported_tool_calls")

    @patch("bridge_server.execute_antigravity")
    def test_handler_streams_sse_when_stream_requested(self, mock_exec):
        from bridge_server import AntigravityRequestHandler
        from io import BytesIO

        mock_exec.return_value = (
            200,
            {
                "choices": [{"message": {"content": "Streamed answer", "role": "assistant"}}],
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            },
        )

        cfg = BridgeConfig(allow_delegated_mcp=True)
        AntigravityRequestHandler.config = cfg

        body = json.dumps({
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": True,
        }).encode("utf-8")

        handler = MagicMock()
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = BytesIO(body)
        handler.path = "/v1/chat/completions"
        handler.config = cfg

        AntigravityRequestHandler.do_POST(handler)
        handler._send_sse_stream.assert_called_once_with(
            "Streamed answer",
            cfg.default_model,
            {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )

    def test_send_sse_stream_format(self):
        from bridge_server import AntigravityRequestHandler
        from io import BytesIO

        handler = MagicMock()
        mock_wfile = BytesIO()
        handler.wfile = mock_wfile

        # Directly invoke unbound method with handler mock
        AntigravityRequestHandler._send_sse_stream(
            handler,
            response_text="Hello world!",
            model="gemini-3.8-flash-medium",
            usage_info={"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
        )

        output = mock_wfile.getvalue().decode("utf-8")
        self.assertIn("data: ", output)
        self.assertIn("Hello world!", output)
        self.assertIn('"finish_reason": "stop"', output)
        self.assertTrue(output.endswith("data: [DONE]\n\n"))

    def test_auth_token_enforcement(self):
        from bridge_server import AntigravityRequestHandler
        from io import BytesIO

        cfg = BridgeConfig(auth_token="expected-bridge-token-xyz")
        AntigravityRequestHandler.config = cfg

        # Case 1: Missing Authorization header
        handler = MagicMock()
        handler.headers = {}
        handler.config = cfg
        self.assertFalse(AntigravityRequestHandler._is_authorized(handler))

        # Case 2: Wrong Bearer token
        handler.headers = {"Authorization": "Bearer wrong-token"}
        self.assertFalse(AntigravityRequestHandler._is_authorized(handler))

        # Case 3: Correct Bearer token
        handler.headers = {"Authorization": "Bearer expected-bridge-token-xyz"}
        self.assertTrue(AntigravityRequestHandler._is_authorized(handler))

    def test_unauthorized_post_returns_401(self):
        from bridge_server import AntigravityRequestHandler
        from io import BytesIO

        cfg = BridgeConfig(auth_token="expected-bridge-token-xyz")
        AntigravityRequestHandler.config = cfg

        body = json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode("utf-8")
        handler = MagicMock()
        handler.headers = {"Content-Length": str(len(body)), "Authorization": "Bearer bad-token"}
        handler.rfile = BytesIO(body)
        handler.path = "/v1/chat/completions"
        handler.config = cfg
        handler._is_authorized = lambda: AntigravityRequestHandler._is_authorized(handler)

        AntigravityRequestHandler.do_POST(handler)
        handler._send_json.assert_called_once()
        args = handler._send_json.call_args[0]
        status_code = args[0]
        data = args[1]
        self.assertEqual(status_code, 401)
        self.assertEqual(data["error"]["code"], 401)

    def test_health_sanitized_no_binary_or_secrets(self):
        from bridge_server import AntigravityRequestHandler

        cfg = BridgeConfig(
            binary_path="/sensitive/host/path/antigravity",
            auth_token="secret-bridge-token",
        )
        AntigravityRequestHandler.config = cfg

        handler = MagicMock()
        handler.headers = {}
        handler.path = "/health"
        handler.config = cfg

        AntigravityRequestHandler.do_GET(handler)
        handler._send_json.assert_called_once()
        args = handler._send_json.call_args[0]
        status_code = args[0]
        data = args[1]
        self.assertEqual(status_code, 200)
        self.assertNotIn("binary_path", data)
        self.assertNotIn("quota_project", data)
        self.assertNotIn("secret-bridge-token", json.dumps(data))
        self.assertEqual(data["status"], "healthy")


if __name__ == "__main__":
    unittest.main()

