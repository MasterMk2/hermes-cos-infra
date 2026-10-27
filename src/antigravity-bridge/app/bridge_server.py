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
    lines = [
        "[System Instruction]\n"
        "You are a delegated read-only assistant operating under a strict security sandbox.\n"
        "Do NOT call filesystem or shell tools (e.g. list_dir, view_file, run_command).\n"
        "Use ONLY the permitted read-only MCP tools (for calendar, event, discord, line, gmail, github).\n"
    ]
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


def execute_routed(messages, prompt, config, model):
    """Complex reasoning tries native CLIs; ordinary and unavailable paths keep MCP."""
    from dataclasses import replace
    from premium_routing import should_try_premium, try_premium
    if should_try_premium(messages, config.premium_cli_enabled):
        started = time.monotonic()
        # Preserve at least 15 seconds of the existing request budget for the
        # normal source/tool backend if the premium route cannot answer.
        budget = min(config.premium_cli_timeout_seconds, max(0, config.timeout_seconds - 15))
        data, status = try_premium(messages, endpoint=config.premium_cli_endpoint,
                                   token=config.premium_cli_token, timeout_seconds=budget)
        logger.info('premium_cli status=%s', status)
        if data is not None:
            metrics.successful_requests += 1
            metrics.last_latency_seconds = time.monotonic() - started
            return 200, data
        config = replace(config, timeout_seconds=max(1, config.timeout_seconds - (time.monotonic() - started)))
    return execute_antigravity(prompt=prompt, config=config, model=model)


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
        denied = cli_out.get("denied_actions", [])
        if not response_text and denied:
            denied_names = ", ".join(d.get("display_name", d.get("action", "unknown")) for d in denied)
            response_text = f"I am a strictly read-only assistant. I do not have permission to perform mutation actions ({denied_names})."

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

    def _is_authorized(self) -> bool:
        """Verify Bearer token against configured internal secret."""
        if not getattr(self.config, "auth_token", None):
            return True
        auth_header = self.headers.get("Authorization", "").strip()
        if not auth_header.startswith("Bearer "):
            return False
        token = auth_header[7:].strip()
        import hmac
        return hmac.compare_digest(token, self.config.auth_token)

    def _send_json(self, status_code: int, data: Dict[str, Any]):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_sse_stream(self, response_text: str, model: str, usage_info: Dict[str, Any]):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        req_id = f"chatcmpl-antigravity-{uuid.uuid4().hex[:12]}"
        created = int(time.time())

        chunk_size = 64
        for i in range(0, max(1, len(response_text)), chunk_size):
            chunk = response_text[i:i + chunk_size] if response_text else ""
            delta: Dict[str, Any] = {"content": chunk}
            if i == 0:
                delta["role"] = "assistant"
            data = {
                "id": req_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": delta,
                        "finish_reason": None,
                    }
                ],
            }
            line = f"data: {json.dumps(data)}\n\n"
            self.wfile.write(line.encode("utf-8"))

        stop_data = {
            "id": req_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": usage_info.get("input_tokens", 0),
                "completion_tokens": usage_info.get("output_tokens", 0),
                "total_tokens": usage_info.get("total_tokens", 0),
            },
        }
        stop_line = f"data: {json.dumps(stop_data)}\n\n"
        self.wfile.write(stop_line.encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True

    def do_GET(self):
        if self.path in {"/health", "/status"}:
            uptime = time.time() - metrics.start_time
            # Check if oauth token file exists
            token_path = os.path.expanduser("~/.gemini/antigravity-cli/antigravity-oauth-token")
            is_auth = os.path.isfile(token_path)
            
            # Sanitized status payload (Section 18: zero secret/path leakage)
            data = {
                "status": "healthy",
                "provider": "antigravity",
                "configured_model": self.config.default_model,
                "authenticated": is_auth,
                "auth_method": "consumer",
                "tool_calling_support": (
                    "delegated_mcp_agent" if self.config.allow_delegated_mcp else "unsupported_in_headless_agent_loop"
                ),
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
            if not self._is_authorized():
                self._send_json(401, {"error": {"message": "Unauthorized: valid Bearer token required", "type": "authentication_error", "code": 401}})
                return
            models_data = [
                {"id": m, "object": "model", "created": int(metrics.start_time), "owned_by": "google-antigravity"}
                for m in self.config.supported_models
            ]
            self._send_json(200, {"object": "list", "data": models_data})
            return

        self._send_json(404, {"error": {"message": "Not Found", "code": 404}})

    def do_POST(self):
        if self.path in {"/v1/chat/completions", "/chat/completions"}:
            if not self._is_authorized():
                metrics.failed_requests += 1
                self._send_json(401, {"error": {"message": "Unauthorized: valid Bearer token required", "type": "authentication_error", "code": 401}})
                return
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

            # Tool Calling Verification (Section 14, 15, 16 & Phase H.3.2)
            # Antigravity CLI does not expose an external function calling API.
            # In Phase H.3.2, constrained delegated MCP agent mode allows Antigravity
            # to fulfill read-only tool requirements via its own mounted MCP servers.
            if tools:
                if not self.config.allow_delegated_mcp:
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
                else:
                    logger.info(f"Delegating tool request (tools: {len(tools)}) to constrained Antigravity read-only agent backend.")

            stream = bool(payload.get("stream", False))

            prompt = format_messages_to_prompt(messages)
            if not prompt:
                self._send_json(400, {"error": {"message": "No prompt content found in messages", "code": 400}})
                return

            status_code, response_data = execute_routed(
                messages=messages,
                prompt=prompt,
                config=self.config,
                model=model,
            )
            if stream and status_code == 200:
                choices = response_data.get("choices", [])
                content = choices[0].get("message", {}).get("content", "") if choices else ""
                usage = response_data.get("usage", {})
                self._send_sse_stream(content, response_data.get('model', model), usage)
                return

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
