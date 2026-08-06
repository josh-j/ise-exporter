"""End to end: a real listener, a real scrape, real dataset series."""
import gzip
import re
import struct
import urllib.error
import urllib.request

import pytest

from ise_exporter3.config import Config
from ise_exporter3.plan import build_plan
from ise_exporter3.scheduler import Scheduler
from http.server import ThreadingHTTPServer

from ise_exporter3.server import HttpServer, accepts_gzip, make_handler
from ise_exporter3.snapshots import LockedCollectorRegistry
from tests.test_v3_scheduler import ScriptedTransport


@pytest.fixture
def served():
    server = HttpServer("127.0.0.1", 0, LockedCollectorRegistry())
    server.start()
    try:
        yield server, f"http://127.0.0.1:{server.address[1]}"
    finally:
        server.stop(timeout=5)


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, response.read().decode("utf-8")


def _sample(body, name, selector=""):
    """A real sample line: name, optional labels, then a value.

    A family that exports only its # HELP and # TYPE header satisfies a bare
    substring check while rendering as no-data on every dashboard -- which is
    exactly how the measured-load counters shipped invisible.
    """
    labels = re.escape(selector) if selector else r"(?:\{[^}]*\})?"
    return re.search(rf"^{re.escape(name)}{labels} \S+$", body, re.MULTILINE)


def test_metrics_are_served_after_a_collection(served):
    server, base = served
    config = Config.from_document(
        {"targets": {"pan": {"host": "pan1", "user": "ro"}}},
        path="test.toml", environ={"ISE_PASS": "secret"})
    scheduler = Scheduler(config, build_plan(config), {"pan": ScriptedTransport()},
                          asynchronous=False)
    scheduler.bootstrap()
    scheduler.tick()

    status, body = _get(f"{base}/metrics")
    assert status == 200
    # Data, health, provider selection and the load model all in one scrape.
    assert 'ise3_deployment_pan_ha_enabled{provider="openapi"} 1.0' in body
    assert 'ise3_dataset_up{dataset="deployment",provider="openapi"} 1.0' in body
    assert 'ise3_dataset_provider_active{dataset="deployment",provider="openapi"} 1.0' in body
    assert _sample(body, "ise3_load_planned_requests_per_hour")
    # A sample, not just the family header: the measured counter has no events
    # yet, so this line only exists because publishing the plan seeded it, and
    # it must survive the snapshot commit that produced the scrape above.
    assert _sample(body, "ise3_load_measured_requests_total",
                   '{target="pan"}')
    # Same fact for the detail-fetch counter: network_devices resolved to ERS
    # on the configured pan target, so its outcomes are seeded even though no
    # fetch has happened, and the snapshot commits above must not clear them.
    assert _sample(body, "ise3_detail_fetches_total",
                   '{cache="ers_network_device",result="fetched"}')


def test_a_route_handler_is_given_the_parsed_query_string():
    # The operator API's dataconnect namespace takes repeatable parameters, so
    # the listener parses once and every handler is handed the same dict. A
    # handler that parsed self.path itself would be a second parser to disagree
    # with this one.
    seen = []

    def handler(query):
        seen.append(query)
        return 200, b"{}", "application/json"

    server = HttpServer(
        "127.0.0.1", 0, LockedCollectorRegistry(), routes={"/probe": handler})
    server.start()
    try:
        base = f"http://127.0.0.1:{server.address[1]}"
        _get(f"{base}/probe?view=radius_accounting&eq=A:1&eq=B:2")
        _get(f"{base}/probe")
    finally:
        server.stop(timeout=5)

    assert seen[0] == {"view": ["radius_accounting"], "eq": ["A:1", "B:2"]}
    # No query string is an empty dict, never None: a handler must not have to
    # tell "absent" from "empty".
    assert seen[1] == {}


def test_a_fragment_or_query_string_never_changes_which_route_answers():
    # Route lookup keys on the path alone. ?x=1 selecting a 404 would make
    # every parameterised route unreachable.
    server = HttpServer(
        "127.0.0.1", 0, LockedCollectorRegistry(),
        routes={"/probe": lambda query: (200, b"ok", "text/plain")})
    server.start()
    try:
        base = f"http://127.0.0.1:{server.address[1]}"
        assert _get(f"{base}/probe?a=1&a=2")[0] == 200
        assert _get(f"{base}/metrics?x=y")[0] == 200
        assert _get(f"{base}/healthz?x=y")[0] == 200
    finally:
        server.stop(timeout=5)


def test_health_and_unknown_paths(served):
    _server, base = served
    assert _get(f"{base}/healthz")[0] == 200
    with pytest.raises(urllib.error.HTTPError) as raised:
        _get(f"{base}/nope")
    assert raised.value.code == 404


def test_the_listener_stops_cleanly_and_frees_its_port():
    server = HttpServer("127.0.0.1", 0, LockedCollectorRegistry())
    server.start()
    port = server.address[1]
    server.stop(timeout=5)
    # Binding the same port again proves the socket was really released.
    again = HttpServer("127.0.0.1", port, LockedCollectorRegistry())
    again.start()
    again.stop(timeout=5)


def test_stopping_a_listener_that_never_started_returns_instead_of_hanging():
    # run()'s teardown calls stop() unconditionally, so this path is reached
    # whenever start() raised. shutdown() waits on an event serve_forever()
    # sets, which would otherwise block forever here.
    HttpServer("127.0.0.1", 0, LockedCollectorRegistry()).stop(timeout=5)


# --- scrape size ------------------------------------------------------------

def _get_raw(url, headers=None):
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, response.headers, response.read()


def test_a_client_that_offers_gzip_gets_a_compressed_scrape(served):
    server, base = served
    config = Config.from_document(
        {"targets": {"pan": {"host": "pan1", "user": "ro"}}},
        path="test.toml", environ={"ISE_PASS": "secret"})
    scheduler = Scheduler(config, build_plan(config), {"pan": ScriptedTransport()},
                          asynchronous=False)
    scheduler.bootstrap()
    scheduler.tick()

    _status, plain_headers, plain = _get_raw(f"{base}/metrics")
    _status, headers, body = _get_raw(
        f"{base}/metrics", {"Accept-Encoding": "gzip"})

    assert plain_headers.get("Content-Encoding") is None
    assert headers.get("Content-Encoding") == "gzip"
    # Prometheus text compresses roughly tenfold; anything under half is a sign
    # the body is not what we think it is. The two scrapes are not compared byte
    # for byte because counters move between them -- exact fidelity is asserted
    # against a fixed body below.
    assert len(body) < len(plain) / 2
    assert gzip.decompress(body).startswith(b"# HELP")
    assert int(headers.get("Content-Length")) == len(body)


def test_compression_does_not_change_the_body_it_delivers():
    # Fidelity, against a body that does not move underneath the assertion.
    payload = (b"# HELP ise3_probe a fixed body\n"
               b"# TYPE ise3_probe gauge\n") + b"ise3_probe 1.0\n" * 500
    server = HttpServer(
        "127.0.0.1", 0, LockedCollectorRegistry(),
        routes={"/probe": lambda query: (200, payload, "text/plain; charset=utf-8")})
    server.start()
    try:
        base = f"http://127.0.0.1:{server.address[1]}"
        _status, headers, body = _get_raw(
            f"{base}/probe", {"Accept-Encoding": "gzip"})
        assert headers.get("Content-Encoding") == "gzip"
        assert gzip.decompress(body) == payload
        assert len(body) < len(payload) / 10

        _status, plain_headers, plain = _get_raw(f"{base}/probe")
        assert plain_headers.get("Content-Encoding") is None
        assert plain == payload
    finally:
        server.stop(timeout=5)


def test_the_response_says_it_was_negotiated_even_when_it_was_not_compressed():
    # A cache that stores the plain body and later serves it to a gzip client,
    # or the reverse, is the failure Vary exists to prevent. It has to be sent
    # on both answers, not only the compressed one.
    server = HttpServer("127.0.0.1", 0, LockedCollectorRegistry())
    server.start()
    try:
        base = f"http://127.0.0.1:{server.address[1]}"
        _status, headers, _body = _get_raw(f"{base}/metrics")
        assert headers.get("Vary") == "Accept-Encoding"
    finally:
        server.stop(timeout=5)


@pytest.mark.parametrize("header,expected", [
    ("gzip", True),
    ("gzip, deflate", True),
    ("gzip;q=0.5", True),
    ("*", True),
    ("deflate", False),
    ("", False),
    (None, False),
    # A client saying it does not want gzip, which a substring match reads as
    # a client asking for it.
    ("gzip;q=0", False),
    ("deflate, gzip;q=0", False),
])
def test_gzip_is_only_used_when_the_client_actually_offered_it(header, expected):
    assert accepts_gzip(header) is expected


def test_a_tiny_body_is_not_compressed_for_nothing(served):
    server, base = served
    _status, headers, body = _get_raw(
        f"{base}/healthz", {"Accept-Encoding": "gzip"})
    assert headers.get("Content-Encoding") is None
    assert body == b"ok\n"


def test_compression_happens_outside_the_snapshot_lock(served):
    """The lock is held for generation and must not also cover compression.

    Generation already blocks publication for its duration; adding compression
    to that window would make every scrape a longer stall for the collectors.
    This asserts the shape rather than the timing: the payload is produced by
    the locked registry, and _respond -- which compresses -- is called after
    that call has returned and released the lock.
    """
    import inspect

    from ise_exporter3 import server as server_module

    source = inspect.getsource(server_module.make_handler)
    generate_at = source.index("generate_latest(registry)")
    respond_at = source.index("self._respond(200, payload", generate_at)
    assert generate_at < respond_at
    # And the compression itself is in _respond, not beside generate_latest.
    assert "gzip.compress" not in source[generate_at:respond_at]
    assert "gzip.compress" in source


def test_a_scraper_that_hangs_up_is_not_reported_as_an_exporter_error(caplog):
    """EPIPE from the reader is noise about the reader, not a failure here.

    At the declared scale a scrape is seconds of work, so any client whose
    timeout is shorter closes while this thread is still writing. Left to the
    base class that surfaces as a bare stderr traceback carrying the same
    "[Errno 32] Broken pipe" text the Data Connect transport reports for a
    genuinely expired Oracle session -- and an operator hunting a failed
    collection must not have to check a peer address to tell them apart.
    """
    import logging

    handler_class = make_handler(LockedCollectorRegistry())

    class HungUp:
        def readline(self, *_args):
            raise BrokenPipeError(32, "Broken pipe")

    stub = handler_class.__new__(handler_class)
    stub.rfile = HungUp()
    stub.client_address = ("172.16.4.9", 51234)
    stub.close_connection = False

    with caplog.at_level(logging.DEBUG, logger="ise_exporter3.server"):
        stub.handle_one_request()

    # Swallowed, the connection retired, and reported below the operator's
    # normal level rather than as an error.
    assert stub.close_connection is True
    assert any(record.levelno == logging.DEBUG
               and "closed the connection" in record.getMessage()
               for record in caplog.records)
    assert not [record for record in caplog.records
                if record.levelno >= logging.WARNING]


def test_the_listener_routes_a_request_failure_through_the_logger():
    """Anything that is not a hang-up still must not reach stderr raw."""
    from ise_exporter3.server import LoggingHTTPServer

    assert LoggingHTTPServer.handle_error is not ThreadingHTTPServer.handle_error


def test_the_listener_survives_a_client_that_disconnects_early(served):
    """And the end-to-end shape: an aborted scrape does not stop the next one."""
    import socket

    server, base = served
    raw = socket.create_connection(("127.0.0.1", server.address[1]), timeout=5)
    raw.sendall(b"GET /metrics HTTP/1.1\r\nHost: localhost\r\n\r\n")
    raw.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                   struct.pack("ii", 1, 0))     # RST rather than a clean FIN
    raw.close()

    status, body = _get(f"{base}/metrics")
    assert status == 200
    assert "ise3_" in body
