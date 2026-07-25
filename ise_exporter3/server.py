"""The exporter's HTTP listener.

One plain ThreadingHTTPServer rather than prometheus_client's ``start_http_server``
helper: v2 had to reach into that helper's return tuple to shut it down, and M5
adds the operator API beside ``/metrics`` on the same server.
"""
from __future__ import annotations

import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest


logger = logging.getLogger(__name__)


def make_handler(registry, routes=None):
    """Build a handler serving /metrics plus any additional JSON routes."""
    extra = dict(routes or {})

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):       # noqa: N802 - BaseHTTPRequestHandler contract
            path = self.path.split("?", 1)[0]
            if path == "/metrics":
                self._respond(200, generate_latest(registry), CONTENT_TYPE_LATEST)
                return
            if path in ("/healthz", "/-/healthy"):
                self._respond(200, b"ok\n", "text/plain; charset=utf-8")
                return
            handler = extra.get(path)
            if handler is not None:
                status, body, content_type = handler()
                self._respond(status, body, content_type)
                return
            self._respond(404, b"not found\n", "text/plain; charset=utf-8")

        def log_message(self, fmt, *args):
            logger.debug(fmt, *args)

        def _respond(self, status, body, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return Handler


class HttpServer:
    """Start and stop the listener without leaking the socket or the thread."""

    def __init__(self, host, port, registry, routes=None):
        self.host = host
        self.port = port
        self._server = ThreadingHTTPServer(
            (host, port), make_handler(registry, routes))
        self._server.daemon_threads = True
        self._thread = None

    @property
    def address(self):
        return self._server.server_address

    def start(self):
        import threading

        self._thread = threading.Thread(
            target=self._server.serve_forever, name="ise3-http", daemon=True)
        self._thread.start()
        logger.info("metrics on http://%s:%d/metrics", self.host, self.address[1])

    def stop(self, timeout=5):
        # shutdown() waits on an event that only serve_forever() ever sets, so
        # calling it on a listener that never started blocks forever. run()'s
        # teardown reaches here whenever start() raised, so this must be safe.
        if self._thread is not None:
            try:
                self._server.shutdown()
            except Exception as error:  # noqa: BLE001 - teardown must not raise
                logger.warning("could not stop the listener: %s", error)
        try:
            self._server.server_close()
        except Exception as error:      # noqa: BLE001
            logger.warning("could not close the listener: %s", error)
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning("listener thread did not stop within %ss", timeout)
