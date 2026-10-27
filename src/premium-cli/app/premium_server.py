"""Loopback-only, authenticated endpoint for the native CLI reasoning workers."""
import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

from cli_reasoner import reason

MAX_REQUEST = 262144


def make_handler(token, runner=reason):
    if len(token) < 32:
        raise ValueError('A dedicated random token of at least 32 characters is required')
    slots = threading.BoundedSemaphore(1)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, code, data):
            payload = json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            if not hmac.compare_digest(self.headers.get('Authorization', '').encode(), ('Bearer ' + token).encode()):
                self.reply(401, {'status': 'unauthorized'})
            elif self.path == '/health':
                self.reply(200, {'status': 'ok', 'service': 'hermes-premium-cli', 'version': 1})
            else:
                self.reply(404, {'status': 'not_found'})

        def do_POST(self):
            if not hmac.compare_digest(self.headers.get('Authorization', '').encode(), ('Bearer ' + token).encode()):
                self.reply(401, {'status': 'unauthorized'})
                return
            if self.path != '/reason':
                self.reply(404, {'status': 'not_found'})
                return
            if self.headers.get('Transfer-Encoding'):
                self.reply(400, {'status': 'invalid_request'})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= MAX_REQUEST:
                    self.reply(413, {'status': 'invalid_size'})
                    return
                self.connection.settimeout(10)
                data = json.loads(self.rfile.read(length))
                messages = data['messages']
                budget = float(data.get('budget_seconds', 60))
                if not isinstance(messages, list) or not messages or not 1 <= budget <= 90:
                    raise ValueError()
                if not all(isinstance(m, dict) and m.get('role') in {'system', 'developer', 'user', 'assistant', 'tool'} for m in messages):
                    raise ValueError()
            except (ValueError, TypeError, KeyError, OSError):
                self.reply(400, {'status': 'invalid_request'})
                return
            if not slots.acquire(blocking=False):
                self.reply(503, {'status': 'busy'})
                return
            try:
                result = runner(messages, budget_seconds=budget)
                self.reply(200, result)
            except Exception:
                self.reply(503, {'status': 'unavailable'})
            finally:
                slots.release()

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=18766)
    parser.add_argument('--token-file', required=True)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('port must be between 1024 and 65535')
    token = Path(args.token_file).read_text(encoding='utf-8').strip()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(token))
    server.daemon_threads = True
    server.serve_forever()


if __name__ == '__main__':
    main()
