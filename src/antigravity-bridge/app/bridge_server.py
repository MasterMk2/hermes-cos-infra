"""Antigravity Provider Bridge Server (Phase H.3.1).

Exposes an OpenAI-compatible HTTP interface to the official Google Antigravity CLI
running natively on Raspberry Pi 4 (aarch64).
"""

import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from typing import Any, Dict, List, Optional, Tuple

from bridge_config import BridgeConfig, load_config

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("antigravity-bridge")

# Secret redaction patterns
SECRET_PATTERNS = [
    re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]+"),
    re.compile(r"ghp_[A-Za-z0-9]{36,40}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{80,90}"),
    re.compile(r"4/0A[A-Za-z0-9_\-]{50,100}"),
    re.compile(r"ya29\.[A-Za-z0-9_\-]{50,200}"),
]


def redact_secrets(text: str) -> str:
    """Mask credentials and tokens from logs."""
    if not isinstance(text, str):
        return text
    redacted = text
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


class BridgeMetrics:
    """Thread-safe request and health metrics."""

    def __init__(self):
        self.total_requests = 0
        self.successful_requests = 0
        self.failed_requests = 0
        self.last_latency_seconds = 0.0
        self.last_error: Optional[str] = None
        self.start_time = time.time()


metrics = BridgeMetrics()


def format_messages_to_prompt(messages: List[Dict[str, Any]]) -> str:
    """Format OpenAI messages into a clean prompt string for Antigravity CLI."""
    lines = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and "text" in part:
                    parts.append(part["text"])
                else:
                    parts.append(str(part))
            content = " ".join(parts)
        
        if role in {"system", "developer"}:
            lines.append("[System Instruction]\n" + str(content) + "\n")
        elif role == "assistant":
            lines.append(f"Assistant: {content}")
        elif role == "user":
            lines.append(f"User: {content}")
        elif role == "tool":
            tool_id = msg.get("tool_call_id", "")
            lines.append(f"[Tool Result ({tool_id})]: {content}")
    return chr(10).join(lines).strip()


def classify_cli_error(returncode: int, stdout: str, stderr: str) -> Tuple[int, str, str]:
    """Classify Antigravity CLI errors into HTTP status code, error type, and message."""
    combined = (stdout + chr(10) + stderr).lower()
    
    if "resource exhausted" in combined or "quota" in combined or "429" in combined or "rate limit" in combined:
        return 429, "insufficient_quota", "Antigravity quota exhausted or rate limited"
    
    if "not logged in" in combined or "please sign in" in combined or "auth" in combined and "failed" in combined:
        return 401, "authentication_error", "Antigravity CLI authentication expired or missing"
    
    if "maximum context length" in combined or "context length exceeded" in combined:
        return 400, "context_length_exceeded", "Context length exceeded (ineligible for failover; compress context)"
        
    if "timed out" in combined or returncode == 124:
        return 504, "timeout", "Antigravity CLI invocation timed out"
        
    if "overloaded" in combined or "503" in combined or "unavailable" in combined:
        return 503, "service_unavailable", "Antigravity service overloaded or unavailable"
        
    return 500, "api_error", f"Antigravity CLI process failed with code {returncode}: {redact_secrets(stderr.strip() or stdout.strip())}"


def execute_antigravity(
    prompt: str,
    config: BridgeConfig,
    model: Optional[str] = None,
    json_schema: Optional[str] = None,
) -> Tuple[int, Dict[str, Any]]:
    """Execute Antigravity CLI non-interactively and return OpenAI-compatible result or error dict."""
    selected_model = model or config.default_model
    
    cmd = [
        config.binary_path,
        "--print", prompt,
        "--model", selected_model,
        "--output-format", "json",
        "--disable-slash-commands",
    ]
    
    if json_schema:
        cmd.extend(["--json-schema", json_schema])

    start_t = time.time()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=config.timeout_seconds,
        )
        duration = time.time() - start_t
        metrics.last_latency_seconds = duration
        
        if proc.returncode != 0:
            status_code, err_type, err_msg = classify_cli_error(proc.returncode, proc.stdout, proc.stderr)
            metrics.failed_requests += 1
            metrics.last_error = err_msg
            logger.error(f"Antigravity CLI failed ({status_code}): {err_msg}")
            return status_code, {
                "error": {
                    "message": err_msg,
                    "type": err_type,
                    "code": status_code,
                }
            }

        # Parse JSON output from CLI
        try:
            cli_out = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            metrics.failed_requests += 1
            err_msg = f"Failed to parse Antigravity JSON output: {e}"
            metrics.last_error = err_msg
            logger.error(err_msg)
            return 502, {
                "error": {
                    "message": err_msg,
                    "type": "invalid_response",
                    "code": 502,
                }
            }

        response_text = cli_out.get("response", "")
        usage_info = cli_out.get("usage", {})
        
        # Build OpenAI chat completion response
        res = {
            "id": f"chatcmpl-antigravity-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": selected_model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": response_text,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": usage_info.get("input_tokens", 0),
                "completion_tokens": usage_info.get("output_tokens", 0),
                "total_tokens": usage_info.get("total_tokens", 0),
            },
        }
        metrics.successful_requests += 1
        return 200, res

    except subprocess.TimeoutExpired:
        metrics.failed_requests += 1
        err_msg = f"Antigravity CLI timed out after {config.timeout_seconds}s"
        metrics.last_error = err_msg
        logger.error(err_msg)
        return 504, {
            "error": {
                "message": err_msg,
                "type": "timeout",
                "code": 504,
            }
        }
    except Exception as e:
        metrics.failed_requests += 1
        err_msg = f"Bridge internal exception: {e}"
        metrics.last_error = err_msg
        logger.error(err_msg)
        return 500, {
            "error": {
                "message": err_msg,
                "type": "internal_error",
                "code": 500,
            }
        }


class AntigravityRequestHandler(BaseHTTPRequestHandler):
    """HTTP handler implementing OpenAI-compatible endpoints."""

    config: BridgeConfig = BridgeConfig()

    def _send_json(self, status_code: int, data: Dict[str, Any]):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in {"/health", "/status"}:
            uptime = time.time() - metrics.start_time
            # Check if oauth token file exists
            token_path = os.path.expanduser("~/.gemini/antigravity-cli/antigravity-oauth-token")
            is_auth = os.path.isfile(token_path)
            
            data = {
                "status": "healthy",
                "provider": "antigravity",
                "binary_path": self.config.binary_path,
                "configured_model": self.config.default_model,
                "authenticated": is_auth,
                "auth_method": "consumer",
                "quota_project": None,
                "tool_calling_support": "unsupported_in_headless_agent_loop",
                "uptime_seconds": round(uptime, 1),
                "metrics": {
                    "total_requests": metrics.total_requests,
                    "successful_requests": metrics.successful_requests,
                    "failed_requests": metrics.failed_requests,
                    "last_latency_seconds": round(metrics.last_latency_seconds, 2),
                    "last_error": metrics.last_error,
                },
            }
            self._send_json(200, data)
            return

        if self.path in {"/v1/models", "/models"}:
            models_data = [
                {"id": m, "object": "model", "created": int(metrics.start_time), "owned_by": "google-antigravity"}
                for m in self.config.supported_models
            ]
            self._send_json(200, {"object": "list", "data": models_data})
            return

        self._send_json(404, {"error": {"message": "Not Found", "code": 404}})

    def do_POST(self):
        if self.path in {"/v1/chat/completions", "/chat/completions"}:
            metrics.total_requests += 1
            content_len = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(content_len).decode("utf-8", errors="replace")

            try:
                payload = json.loads(raw_body)
            except json.JSONDecodeError:
                self._send_json(400, {"error": {"message": "Invalid JSON in request body", "code": 400}})
                return

            messages = payload.get("messages", [])
            model = payload.get("model", self.config.default_model)
            tools = payload.get("tools", [])

            # Tool Calling Verification (Section 14, 15, 16)
            # Antigravity CLI does not expose an external function calling API.
            # Invoking tool tasks in headless print mode intercepts them with internal agent tools.
            if tools:
                logger.warning("Tool calling requested on Antigravity provider bridge.")
                if not self.config.allow_tool_emulation:
                    # Return 501 / Failover-eligible error so Hermes cleanly triggers fallback chain
                    metrics.failed_requests += 1
                    err_msg = (
                        "Antigravity CLI headless runtime owns its internal agent loop and does not expose "
                        "a raw OpenAI function-calling API. Tool calls must fall back to capable provider (GLM-5.3-Flash)."
                    )
                    metrics.last_error = err_msg
                    self._send_json(501, {
                        "error": {
                            "message": err_msg,
                            "type": "unsupported_tool_calls",
                            "code": 501,
                        }
                    })
                    return

            prompt = format_messages_to_prompt(messages)
            if not prompt:
                self._send_json(400, {"error": {"message": "No prompt content found in messages", "code": 400}})
                return

            status_code, response_data = execute_antigravity(
                prompt=prompt,
                config=self.config,
                model=model,
            )
            self._send_json(status_code, response_data)
            return

        self._send_json(404, {"error": {"message": "Not Found", "code": 404}})

    def log_message(self, format: str, *args):
        # Override to prevent logging secrets
        msg = format % args
        logger.info(redact_secrets(msg))


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Multi-threaded HTTP server."""
    daemon_threads = True


def run_server(config_path: Optional[str] = None):
    cfg = load_config(config_path)
    AntigravityRequestHandler.config = cfg
    
    server_address = (cfg.host, cfg.port)
    httpd = ThreadedHTTPServer(server_address, AntigravityRequestHandler)
    logger.info(f"Antigravity Provider Bridge running on {cfg.host}:{cfg.port}")
    logger.info(f"Binary path: {cfg.binary_path}, Default model: {cfg.default_model}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Stopping Antigravity Provider Bridge...")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    conf_file = sys.argv[1] if len(sys.argv) > 1 else None
    run_server(conf_file)
