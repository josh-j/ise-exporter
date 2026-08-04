"""The exporter's HTTP listener.

One plain ThreadingHTTPServer rather than prometheus_client's ``start_http_server``
helper: v2 had to reach into that helper's return tuple to shut it down, and M5
adds the operator API beside ``/metrics`` on the same server.

Two things about the ``/metrics`` path are worth stating, because both are about
what a scrape costs rather than what it says.

**It is compressed.** At the declared scale a scrape is roughly 60,000 series and
6.4 MiB of text, Prometheus always offers gzip, and this format compresses about
tenfold -- so the default was sending ten times the bytes for no reason. Content
negotiation is real negotiation: a client that does not offer gzip gets exactly
the same body it always did.

**The compression happens outside the snapshot lock.** ``generate_latest`` holds
``snapshot_lock`` for the whole of its collection, which blocks publication for
that window. Compressing inside it would add the compression time to the block.
So the payload is generated first and compressed after, which is why the two are
separate statements below rather than one nested call -- a detail that looks like
style and is not.
"""
from __future__ import annotations

import gzip
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest


logger = logging.getLogger(__name__)

# Below this, the gzip header and the CPU cost are not worth the bytes saved.
MIN_COMPRESS_BYTES = 1024
# Level 6 is zlib's default and the knee of the curve for this text: level 9
# spends noticeably more CPU inside a scrape for around a percent more ratio,
# and a scrape's job is to be cheap.
COMPRESS_LEVEL = 6


def accepts_gzip(header):
    """True when the client actually offered gzip.

    Parsed rather than substring-matched: ``gzip;q=0`` is a client saying it does
    *not* want gzip, and answering it with a compressed body would be wrong.
    """
    for part in str(header or "").split(","):
        token, _, parameters = part.strip().partition(";")
        if token.strip().lower() not in ("gzip", "*"):
            continue
        quality = "1"
        for parameter in parameters.split(";"):
            name, _, value = parameter.partition("=")
            if name.strip().lower() == "q":
                quality = value.strip() or "0"
        try:
            if float(quality) > 0:
                return True
        except ValueError:
            return True         # a malformed q is not a refusal
    return False


def make_handler(registry, routes=None):
    """Build a handler serving /metrics plus any additional JSON routes.

    Every route handler takes the parsed query string, whether it reads it or
    not. The alternative -- handing the raw path to the handlers that take
    parameters -- would put a second query-string parser in the operator API,
    and the two would eventually disagree about what ``?view=a&view=b`` means.
    """
    extra = dict(routes or {})

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):       # noqa: N802 - BaseHTTPRequestHandler contract
            request = urlsplit(self.path)
            path = request.path
            if path == "/metrics":
                # Generated under snapshot_lock; compressed in _respond, after
                # the lock is released.
                payload = generate_latest(registry)
                self._respond(200, payload, CONTENT_TYPE_LATEST)
                return
            if path in ("/healthz", "/-/healthy"):
                self._respond(200, b"ok\n", "text/plain; charset=utf-8")
                return
            handler = extra.get(path)
            if handler is not None:
                status, body, content_type = handler(parse_qs(request.query))
                self._respond(status, body, content_type)
                return
            self._respond(404, b"not found\n", "text/plain; charset=utf-8")

        def log_message(self, fmt, *args):
            logger.debug(fmt, *args)

        def handle_one_request(self):
            # A scraper that hangs up is not an exporter fault, and at the
            # declared scale it is a routine one: the payload above is seconds
            # of work, so any client whose timeout is shorter than that closes
            # while this thread is still writing. Left to the base class, that
            # arrives as a bare traceback on stderr -- unformatted, outside the
            # logger, and indistinguishable at a glance from the Data Connect
            # socket teardown the transport reports for a genuinely expired
            # Oracle session. One is noise about the reader; the other is a
            # failed collection, and an operator must not have to read a peer
            # address to tell them apart.
            try:
                super().handle_one_request()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                self.close_connection = True
                logger.debug(
                    "scrape client %s closed the connection before the response "
                    "was written", self.client_address[0])


        def _respond(self, status, body, content_type):
            encoding = ""
            if (len(body) >= MIN_COMPRESS_BYTES
                    and accepts_gzip(self.headers.get("Accept-Encoding"))):
                compressed = gzip.compress(body, compresslevel=COMPRESS_LEVEL)
                # Only when it actually helps: an already-compressed or
                # incompressible body must not be sent larger than it arrived.
                if len(compressed) < len(body):
                    body, encoding = compressed, "gzip"
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            if encoding:
                self.send_header("Content-Encoding", encoding)
            # Vary regardless: a cache must not serve a gzip body to a client
            # that did not ask for one, and this response is negotiated whether
            # or not this particular client offered.
            self.send_header("Vary", "Accept-Encoding")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return Handler


class LoggingHTTPServer(ThreadingHTTPServer):
    """Report a failed request through the logger, never onto stderr.

    ``socketserver`` prints an unhandled request exception as a bare traceback
    on stderr. Under systemd that lands in the journal outside the exporter's
    own log format and level, so it is invisible to a log level raised to quiet
    the exporter and unmissable in a journal being read for anything else.
    """

    def handle_error(self, request, client_address):
        logger.exception("unhandled error serving a request from %s",
                         client_address[0] if client_address else "an unknown peer")


class HttpServer:
    """Start and stop the listener without leaking the socket or the thread."""

    def __init__(self, host, port, registry, routes=None):
        self.host = host
        self.port = port
        self._server = LoggingHTTPServer(
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
