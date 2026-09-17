"""Bounded HTTP transport; sensitive request/answer text is never logged."""
import json
import socket
import threading
from time import monotonic
from .runtime import Runtime
from .config import DEFAULT
from socketserver import TCPServer
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from .protocol import empty_response

MAX_BODY = 2 * 1024 * 1024
SLOTS = threading.BoundedSemaphore(4)
RUNTIME = Runtime()

class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(0.75)

    def do_POST(self):
        deadline = monotonic() + DEFAULT.request_seconds
        response = empty_response()
        acquired = SLOTS.acquire(blocking=False)
        try:
            if not acquired:
                self._reply(response)
                return
            headers = self.headers.get_all('Content-Length', [])
            if len(headers) != 1 or self.headers.get('Transfer-Encoding'):
                raise ValueError('invalid framing')
            length = int(headers[0])
            if not 0 < length <= MAX_BODY:
                raise ValueError('body size')
            chunks = []
            remaining = length
            while remaining:
                budget = deadline - monotonic()
                if budget <= 0:
                    raise TimeoutError("request deadline")
                self.connection.settimeout(min(0.75, budget))
                chunk = self.rfile.read1(min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            body = b"".join(chunks)
            if len(body) != length:
                raise ValueError('incomplete body')
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError('object required')
            response = RUNTIME.decide(payload, deadline)
        except Exception:
            # Intentionally no traceback, payload, prompt or model output.
            response = empty_response()
        finally:
            if acquired:
                SLOTS.release()
        self._reply(response)

    def _reply(self, response):
        body=json.dumps(response,ensure_ascii=False,separators=(',',':')).encode()
        try:
            self.send_response(200)
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Connection','close')
            self.end_headers()
            self.wfile.write(body)
        except (OSError,socket.timeout):
            pass
        self.close_connection=True

    def log_message(self, format, *args):
        pass

class Server(ThreadingHTTPServer):
    daemon_threads=True
    allow_reuse_address=True
    def server_bind(self):
        # HTTPServer.server_bind calls getfqdn(), which may block on host DNS
        # before listen(). The protocol needs no reverse DNS lookup.
        TCPServer.server_bind(self)
        self.server_name = self.server_address[0]
        self.server_port = self.server_address[1]
    def handle_error(self, request, client_address):
        # Avoid default stderr traceback leakage.
        pass

def serve(port):
    Server(('0.0.0.0',port),Handler).serve_forever()
