"""The REST transport's safety behaviours.

These are the properties that cost real debugging time in earlier versions: one
scheduled request must be exactly one wire attempt, ambient CA environment must
not override configured trust, a configured timeout must not be spendable twice,
and a partial enumeration must never be published as a complete one.
"""
import socket
import threading
import time

import pytest
import requests
from prometheus_client import REGISTRY

from ise_exporter3.config import Config
from ise_exporter3.transports import TransportError
from ise_exporter3.transports.rest import (
    MAX_HTTP_RESPONSE_BYTES,
    RestTransport,
    redact,
    split_timeout,
)
from ise_exporter3.transports import rest as rest_module


class FakeResponse:
    def __init__(self, *, status_code=200, payload=None, body=b"", headers=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload
        self._body = body if body else b"{}"
        self.closed = False

    def iter_content(self, chunk_size=1):
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start:start + chunk_size]

    @property
    def content(self):
        return getattr(self, "_content", self._body)

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON")
        return self._payload

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}
        self.trust_env = None

    def get(self, url, params=None, timeout=None, stream=None, allow_redirects=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        pass


@pytest.fixture
def transport(tmp_path):
    config = Config.from_document(
        {"targets": {"pan": {"host": "pan1.example.com", "user": "ro"}},
         "exporter": {"state_db": str(tmp_path / "state.sqlite3")}},
        path="test.toml", environ={"ISE_PASS": "secret"})
    return RestTransport(config, "pan")


def _measured(target="pan"):
    return REGISTRY.get_sample_value(
        "ise3_load_measured_requests_total", {"target": target}) or 0.0


# --- timeout and trust ------------------------------------------------------

def test_the_configured_timeout_is_split_across_connect_and_read():
    # A scalar Requests timeout allows the full budget once per phase, so a 30s
    # setting can take 60s on the wire.
    connect, read = split_timeout(30)
    assert connect + read == pytest.approx(30)
    assert connect <= 5


@pytest.mark.parametrize("value,total", [(1, 1), (7, 7), (900, 30), ("x", 30), (0, 1)])
def test_the_timeout_is_clamped_into_a_sane_band(value, total):
    assert sum(split_timeout(value)) == pytest.approx(total)


def test_ambient_ca_environment_cannot_override_configured_trust(transport):
    # Nix sets REQUESTS_CA_BUNDLE process-wide, which silently defeated an
    # explicit verify setting in an earlier version.
    assert transport.session.trust_env is False


def test_urllib3_retries_are_disabled_so_one_request_is_one_attempt(transport):
    adapter = transport.session.get_adapter("https://pan1.example.com")
    assert adapter.max_retries.total == 0


# --- measured load ----------------------------------------------------------

def test_every_wire_attempt_increments_the_measured_load_counter(transport):
    transport.session = FakeSession([
        FakeResponse(payload={"response": {"ok": True}}),
        FakeResponse(payload={"response": {"ok": True}}),
    ])
    before = _measured()
    transport.get_openapi("/deployment/node")
    transport.get_openapi("/deployment/pan-ha")
    assert _measured() == pytest.approx(before + 2)


def test_a_request_refused_by_the_auth_guard_is_not_counted_as_load(transport):
    transport._guard.failure(1, 3600, __import__("time").time())
    before = _measured()
    with pytest.raises(TransportError) as raised:
        transport.get_openapi("/deployment/node")
    assert raised.value.reason == "authentication_backoff"
    assert _measured() == pytest.approx(before)


# --- failure taxonomy -------------------------------------------------------

@pytest.mark.parametrize("response,reason", [
    (FakeResponse(status_code=401), "authentication_failed"),
    (FakeResponse(status_code=403), "authorization_failed"),
    (FakeResponse(status_code=500), "http_error"),
    (requests.exceptions.Timeout(), "timeout"),
    (requests.exceptions.SSLError("bad cert"), "tls_failed"),
    (requests.exceptions.ConnectionError("refused"), "connection_failed"),
])
def test_transport_failures_map_onto_bounded_reasons(transport, response, reason):
    transport.session = FakeSession([response])
    with pytest.raises(TransportError) as raised:
        transport.get_openapi("/deployment/node")
    assert raised.value.reason == reason


def test_an_oversized_response_is_refused_before_it_is_retained(transport):
    transport.session = FakeSession([FakeResponse(
        headers={"Content-Length": str(MAX_HTTP_RESPONSE_BYTES + 1)})])
    with pytest.raises(TransportError) as raised:
        transport.get_openapi("/deployment/node")
    assert raised.value.reason == "response_too_large"


def test_unparseable_json_is_an_invalid_response_not_a_crash(transport):
    transport.session = FakeSession([FakeResponse(payload=None)])
    with pytest.raises(TransportError) as raised:
        transport.get_openapi("/deployment/node")
    assert raised.value.reason == "invalid_response"


@pytest.mark.parametrize("text", [
    '{"password": "hunter2"}',
    "password=hunter2&user=admin",
    "<password>hunter2</password>",
    "Authorization: Bearer abc.def.ghi",
])
def test_error_snippets_are_redacted_before_they_reach_a_log(text):
    assert "hunter2" not in redact(text)
    assert "abc.def.ghi" not in redact(text)


@pytest.mark.parametrize("text,secret", [
    # The header this transport itself sends, echoed back by a WAF block page.
    ("Authorization: Basic aHVudGVyMg==", "aHVudGVyMg=="),
    ("<ns0:password>hunter2</ns0:password>", "hunter2"),
    ('{"token": hunter2}', "hunter2"),
    ('{"token": 123456789}', "123456789"),
    # A snippet is a truncated body: the terminator may simply not be there.
    ('{"password":"hunter2', "hunter2"),
    ("<password>hunter2", "hunter2"),
])
def test_redaction_covers_unquoted_namespaced_and_truncated_secrets(text, secret):
    assert secret not in redact(text)


def test_a_secret_straddling_the_snippet_boundary_is_redacted_before_it_is_cut():
    # Truncating before redacting would strand the closing quote outside the
    # window the patterns can see.
    filler = "a" * (rest_module.HTTP_ERROR_SNIPPET_BYTES - 20)
    body = ('{"note":"' + filler + '","password":"hunter2"}').encode("utf-8")
    assert "hunter2" not in rest_module.error_snippet(FakeResponse(body=body))


# --- read-phase classification and deadlines --------------------------------

def test_urllib3_read_retries_are_disabled_without_being_spent(transport):
    # read=0 is a count urllib3 spends, turning a read timeout into a
    # MaxRetryError that requests reports as an unreachable host.
    adapter = transport.session.get_adapter("https://pan1.example.com")
    assert adapter.max_retries.read is False


def test_a_read_timeout_is_reported_as_a_timeout_not_an_outage(transport):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    accepted = []
    thread = threading.Thread(
        target=lambda: accepted.append(listener.accept()), daemon=True)
    thread.start()
    transport.timeout = 1
    # The session's adapter, not a fake: this pins the urllib3 mapping itself.
    transport.session.mount(
        "http://", transport.session.get_adapter("https://pan1.example.com"))
    try:
        with pytest.raises(TransportError) as raised:
            transport._request(
                f"http://127.0.0.1:{listener.getsockname()[1]}/x", None, "probe")
    finally:
        thread.join(timeout=5)
        for connection, _address in accepted:
            connection.close()
        listener.close()
    assert raised.value.reason == "timeout"


def test_a_body_read_that_outlives_its_deadline_is_abandoned():
    # The per-socket read timeout restarts on every chunk, so only a wall-clock
    # deadline stops a drip-feeding peer holding the transport lock.
    response = FakeResponse(body=b"x" * 1024)
    with pytest.raises(requests.exceptions.Timeout):
        RestTransport._buffer(response, deadline=time.monotonic() - 1)
    assert response.closed


def test_a_body_read_stops_when_the_exporter_is_shutting_down():
    shutdown = threading.Event()
    shutdown.set()
    response = FakeResponse(body=b"x" * 1024)
    with pytest.raises(TransportError):
        RestTransport._buffer(response, shutdown=shutdown)
    assert response.closed


# --- MnT XML ----------------------------------------------------------------

def _mnt(transport, body):
    transport.session = FakeSession([FakeResponse(body=body)])
    return transport.get_mnt_xml("/Session/ActiveList")


def test_mnt_active_list_is_returned_as_bounded_plain_data(transport):
    body = (b'<sessionParameters noOfActiveSession="2">'
            b"<activeSession><user_name>a</user_name></activeSession>"
            b"<activeSession><user_name>b</user_name></activeSession>"
            b"</sessionParameters>")
    assert _mnt(transport, body) == {
        "total": 2, "sessions": [{"user_name": "a"}, {"user_name": "b"}]}


def test_a_single_record_mnt_response_keeps_its_fields(transport):
    body = b"<sessionParameters><user_name>a</user_name></sessionParameters>"
    assert _mnt(transport, body) == {"total": 0, "sessions": [{"user_name": "a"}]}


def test_a_doctype_hidden_behind_a_padded_prolog_is_still_refused(transport):
    # XML allows unbounded comments before the doctypedecl, so a prefix scan is
    # not a control: this payload expands an entity into hundreds of megabytes.
    body = (b'<?xml version="1.0"?><!-- ' + b"x" * 8192 + b" -->"
            b'<!DOCTYPE r [<!ENTITY a "boom">]>'
            b"<sessionParameters><user_name>&a;</user_name></sessionParameters>")
    with pytest.raises(TransportError) as raised:
        _mnt(transport, body)
    assert raised.value.reason == "invalid_response"


def test_the_mnt_element_ceiling_stops_the_parse_rather_than_describing_it(
        transport, monkeypatch):
    monkeypatch.setattr(rest_module, "MAX_XML_ELEMENTS", 8)
    body = (b"<sessionParameters>"
            + b"<activeSession><user_name>a</user_name></activeSession>" * 20
            + b"</sessionParameters>")
    with pytest.raises(TransportError) as raised:
        _mnt(transport, body)
    assert raised.value.reason == "response_too_large"


def test_a_deeply_nested_mnt_document_trips_a_ceiling(transport):
    # Nested elements were never counted at all, so depth was a free dimension.
    body = (b"<sessionParameters>" + b"<a>" * 200 + b"</a>" * 200
            + b"</sessionParameters>")
    with pytest.raises(TransportError) as raised:
        _mnt(transport, body)
    assert raised.value.reason == "response_too_large"


def test_a_production_scale_active_list_is_accepted(transport):
    # An ActiveList is one unpaged document sized by the fleet: at the 60k
    # sessions production runs, it is ~28 MiB. A ceiling below that rejects the
    # real thing, so the body is bounded by the HTTP cap and the parse by its
    # own record, element and depth ceilings rather than by a second byte limit.
    body = (b'<sessionParameters noOfActiveSession="60000">'
            + b"<activeSession><user_name>employee@example.com</user_name>"
              b"<calling_station_id>00:11:22:33:44:55</calling_station_id>"
              b"<nas_ip_address>10.10.10.10</nas_ip_address>"
              b"<acct_session_id>0A0A0A0A00000001</acct_session_id>"
              b"<framed_ip_address>10.20.30.40</framed_ip_address>"
              b"<nas_port_id>GigabitEthernet1/0/1</nas_port_id>"
              b"</activeSession>" * 60_000
            + b"</sessionParameters>")
    assert len(body) > 8 * 1024 * 1024
    result = _mnt(transport, body)
    assert result["total"] == 60_000
    assert len(result["sessions"]) == 60_000


# --- enumeration completeness ----------------------------------------------

def test_ers_enumeration_that_does_not_match_its_total_is_refused(transport):
    # A partial inventory published as complete is worse than an outage: it
    # looks authoritative and quietly under-counts.
    transport.session = FakeSession([FakeResponse(payload={
        "SearchResult": {"total": 5, "resources": [{"id": "1"}]}})])
    with pytest.raises(TransportError) as raised:
        transport.get_ers("/config/networkdevice", get_all=True)
    assert raised.value.reason == "invalid_response"


def test_ers_enumeration_follows_pagination_to_completion(transport):
    transport.session = FakeSession([
        FakeResponse(payload={"SearchResult": {
            "total": 2, "resources": [{"id": "1"}],
            "nextPage": {"href": "https://pan1/ers/config/networkdevice?page=2"}}}),
        FakeResponse(payload={"SearchResult": {"resources": [{"id": "2"}]}}),
    ])
    rows = transport.get_ers("/config/networkdevice", get_all=True)
    assert [row["id"] for row in rows] == ["1", "2"]


def test_pagination_survives_a_host_whose_name_begins_with_ers(transport):
    # "//" before such a host puts the first "/ers" inside the authority, so
    # splitting the whole URL stranded every paginated dataset on that node.
    transport.session = FakeSession([
        FakeResponse(payload={"SearchResult": {
            "total": 2, "resources": [{"id": "1"}],
            "nextPage": {"href": "https://ers-pan1.corp.example.com:9060"
                                 "/ers/config/networkdevice?size=100&page=2"}}}),
        FakeResponse(payload={"SearchResult": {"resources": [{"id": "2"}]}}),
    ])
    rows = transport.get_ers("/config/networkdevice", get_all=True)
    assert [row["id"] for row in rows] == ["1", "2"]


def test_a_next_page_pointing_at_another_resource_is_refused(transport):
    # The href host is ignored and the path is checked, so a rogue link cannot
    # redirect credentials or splice in unrelated rows.
    transport.session = FakeSession([FakeResponse(payload={"SearchResult": {
        "total": 2, "resources": [{"id": "1"}],
        "nextPage": {"href": "https://elsewhere/ers/config/endpoint?page=2"}}})])
    with pytest.raises(TransportError) as raised:
        transport.get_ers("/config/networkdevice", get_all=True)
    assert raised.value.reason == "invalid_response"


def test_an_empty_result_is_a_value_not_a_failure(transport):
    transport.session = FakeSession([FakeResponse(payload={
        "SearchResult": {"total": 0, "resources": []}})])
    assert transport.get_ers("/config/networkdevice", get_all=True) == []
