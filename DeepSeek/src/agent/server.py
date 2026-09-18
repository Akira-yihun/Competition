"""HTTP transport.

Baseline defects this replaces (audit findings A15-A17):

* it emitted only ``roleCommandMap`` -- a strict judge reads the missing
  ``prompt`` / ``executeCmd`` keys as a malformed envelope and discards the whole
  turn (a 异常, five of which end the match);
* ``Content-Length`` was parsed outside the ``try``, so a bad header escaped
  ``do_POST`` and produced **no response at all** -- a guaranteed 5-second
  timeout 异常;
* every request logged the full response to stdout, where a full pipe blocks the
  process and turns into a whole-match timeout.

So: parse defensively, answer everything, log to a file-ish sink instead of the
response path, and always emit a complete envelope.
"""
from __future__ import annotations

import json
import logging
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .brain import make_handler
from .config import DEFAULT, Config
from .protocol import empty_envelope

LOGGER = logging.getLogger(__name__)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CoreGeek"
    sys_version = ""

    #: populated by :func:`serve`
    decide: Any = staticmethod(lambda payload: empty_envelope())
    cfg: Config = DEFAULT
    max_body: int = DEFAULT.max_body_bytes

    # -- helpers -----------------------------------------------------------
    def _read_body(self) -> Any:
        """Read and decode the request body; never raises."""
        try:
            length = self.headers.get("Content-Length")
            size = int(length) if length not in (None, "") else 0
        except (TypeError, ValueError):
            size = 0
        if size < 0 or size > self.max_body:
            return None
        if size == 0:
            return {}
        try:
            raw = self.rfile.read(size)
        except (OSError, ValueError):
            return None
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except (ValueError, UnicodeError):
            return None

    def _respond(self, payload: dict) -> None:
        try:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError):
            body = b'{"roleCommandMap":{},"prompt":"","executeCmd":""}'
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass                        # the judge hung up; nothing to do

    # -- request handling --------------------------------------------------
    def _handle(self) -> None:
        payload = self._read_body()
        if payload is None:
            self._respond(empty_envelope())
            return
        try:
            response = self.decide(payload)
        except Exception:               # strategy bugs must not lose the turn
            LOGGER.exception("decision failed")
            response = empty_envelope()
        if not _envelope_shaped(response):
            response = empty_envelope()
        _log_turn(payload, response)
        self._respond(response)

    def do_POST(self) -> None:          # noqa: N802
        self._handle()

    def do_GET(self) -> None:           # noqa: N802
        self._respond(empty_envelope())

    def do_PUT(self) -> None:           # noqa: N802
        self._handle()

    def log_message(self, format: str, *args: Any) -> None:
        LOGGER.debug(format, *args)


def _envelope_shaped(response: Any) -> bool:
    """The three top-level keys must exist with the right JSON types."""
    return (isinstance(response, dict)
            and isinstance(response.get("roleCommandMap"), dict)
            and isinstance(response.get("prompt"), str)
            and isinstance(response.get("executeCmd"), str))


def _log_turn(payload: Any, response: dict) -> None:
    """Log a *summary*, never the raw response body.

    The baseline printed the complete response every round; on a shared log
    console that leaks task answers to the opponent, and a filled stdout pipe
    blocks the process (which the judge sees as a timeout).
    """
    if not LOGGER.isEnabledFor(logging.INFO):
        return
    try:
        round_no = payload.get("roundNo") if isinstance(payload, dict) else None
        commands = response.get("roleCommandMap") or {}
        actions = ",".join(
            f"{rid}:{cmd.get('action')}" for rid, cmd in sorted(commands.items())
        )
        LOGGER.info("round=%s actions=[%s] prompt=%s", round_no, actions,
                    "yes" if response.get("prompt") else "no")
    except Exception:
        LOGGER.debug("turn log failed")


def serve(port: int, cfg: Config = DEFAULT) -> None:
    """Serve until killed.  Binds 0.0.0.0 as the interface document requires."""
    Handler.cfg = cfg
    Handler.max_body = cfg.max_body_bytes
    Handler.decide = staticmethod(make_handler(cfg))

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
