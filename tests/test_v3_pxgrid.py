"""pxGrid v3 transport and dataset contract regressions."""
from __future__ import annotations

import threading
import time
from types import MethodType

from prometheus_client import REGISTRY

from ise_exporter3.config import Config
from ise_exporter3.datasets import active_sessions, endpoint_attributes, posture_current
from ise_exporter3.limits import DEFAULT_RESULT_BYTES, for_scale
from ise_exporter3.model import Scale
from ise_exporter3.plan import PlannedDataset, build_plan
from ise_exporter3.pxgrid import as_list
from ise_exporter3.runtime import Runner
from ise_exporter3.transports import Transport, TransportError
from ise_exporter3.transports.pxgrid import PxGridTransport
from ise_exporter3.transports.pxgrid import PUBSUB_SERVICE, SESSION_SERVICE


def _config(tmp_path=None):
    exporter = {}
    if tmp_path is not None:
        exporter["state_db"] = str(tmp_path / "state.sqlite3")
    return Config.from_document(
        {
            "exporter": exporter,
            "scale": Scale(
                nads=10, endpoints=10, sessions=10,
                accounts=10, policy_sets=10).__dict__,
            "targets": {
                "pxgrid": {
                    "host": "ise01.ise.lab",
                    "port": 8910,
                    "node_name": "ise-exporter",
                    "verify_tls": False,
                    "request_timeout": 7,
                },
            },
        },
        path="test.toml",
        environ={"ISE_PXGRID_PASSWORD": "secret"},
    )


def _state_transport():
    transport = object.__new__(PxGridTransport)
    transport.config = _config()
    transport._state_lock = threading.RLock()
    transport._sync_lock = threading.Lock()
    transport._sessions = {}
    transport._buffer = []
    transport._syncing = False
    transport._sequence = {}
    transport._session_snapshot_at = 0.0
    transport._endpoints = []
    transport._endpoint_snapshot_at = 0.0
    return transport


def test_config_accepts_a_bounded_pxgrid_request_timeout():
    target = _config().target("pxgrid")
    assert target.configured
    assert target.request_timeout == 7


def test_pxgrid_session_baseline_ceiling_scales_past_the_lab_default():
    lab = Scale(
        nads=10, endpoints=10, sessions=10, accounts=10, policy_sets=10)
    production = Scale(
        nads=5_000, endpoints=100_000, sessions=20_000,
        accounts=1_000, policy_sets=100)

    assert for_scale(lab).pxgrid_session_bytes == DEFAULT_RESULT_BYTES
    assert for_scale(production).pxgrid_session_bytes == 20_000 * 8 * 1024


def test_session_refresh_uses_the_pxgrid_specific_response_ceiling():
    transport = _state_transport()
    calls = []

    def query(self, *_args, **kwargs):
        calls.append(kwargs["max_bytes"])
        return {"sessions": []}

    transport._query = MethodType(query, transport)
    transport._refresh_sessions(force=True)

    assert calls == [transport.config.limits.pxgrid_session_bytes]


def test_plan_selects_built_pxgrid_collectors():
    plan = build_plan(_config())
    selected = {
        entry.name: entry for entry in plan.enabled
        if entry.provider is not None and entry.provider.name == "pxgrid"
    }
    assert selected["active_sessions"].provider.fetch is active_sessions.fetch_pxgrid
    assert selected["posture_current"].provider.fetch is posture_current.fetch_pxgrid
    assert selected["endpoint_attributes"].provider.fetch is endpoint_attributes.fetch_pxgrid
    # The two endpoint datasets read one transport cache and declare one cost pool.
    assert selected["endpoint_attributes"].shared_with == ("endpoint_inventory",)


def test_nested_cisco_collection_shapes_keep_every_record():
    assert as_list(
        {"session": [{"auditSessionId": "A"}, {"auditSessionId": "B"}]},
        "session",
    ) == [{"auditSessionId": "A"}, {"auditSessionId": "B"}]


def test_session_startup_does_not_discover_the_unused_endpoint_service():
    transport = object.__new__(PxGridTransport)
    transport._discovered = False
    transport._services = {}
    transport._capabilities = set()
    calls = []

    def lookup(self, service):
        calls.append(service)
        if service == SESSION_SERVICE:
            return [{"properties": {"sessionTopic": "/topic/session"}}]
        return [{"properties": {"wsUrl": "wss://ise/pxgrid/pubsub"}}]

    transport._lookup = MethodType(lookup, transport)
    transport._discover()

    assert calls == [SESSION_SERVICE, PUBSUB_SERVICE]
    assert "capability:pxgrid_session_topic" in transport._capabilities


def test_rest_failover_promotes_the_provider_that_answered():
    transport = object.__new__(PxGridTransport)
    transport.settings = _config().target("pxgrid")
    transport._rest_services = {
        SESSION_SERVICE: (
            ("bad-peer", "https://bad.example/rest", "bad-secret"),
            ("good-peer", "https://good.example/rest", "good-secret"),
        ),
    }
    calls = []

    def resolve(self, service, *, refresh=False):
        assert service == SESSION_SERVICE
        assert not refresh
        return self._rest_services[service]

    def post(self, url, _body, **_kwargs):
        calls.append(url)
        if "bad.example" in url:
            raise TransportError("connection_failed", "unreachable")
        return {"sessions": []}

    transport._resolve_rest = MethodType(resolve, transport)
    transport._post = MethodType(post, transport)

    assert transport._query(
        SESSION_SERVICE, "getSessions", {}, api="pxgrid_get_sessions",
    ) == {"sessions": []}
    assert transport._query(
        SESSION_SERVICE, "getSessions", {}, api="pxgrid_get_sessions",
    ) == {"sessions": []}
    assert calls == [
        "https://bad.example/rest/getSessions",
        "https://good.example/rest/getSessions",
        "https://good.example/rest/getSessions",
    ]
    assert transport._rest_services[SESSION_SERVICE][0][0] == "good-peer"


def test_snapshot_drain_keeps_a_newer_disconnect():
    transport = _state_transport()

    def query(self, *_args, **_kwargs):
        self._apply_session({
            "auditSessionId": "A2",
            "state": "DISCONNECTED",
        })
        return {"sessions": [
            {"auditSessionId": "A1", "state": "STARTED"},
            {"auditSessionId": "A2", "state": "STARTED"},
        ]}

    transport._query = MethodType(query, transport)
    transport._refresh_sessions(force=True)

    assert set(transport._sessions) == {"A1"}
    assert transport._buffer == []
    assert not transport._syncing


def test_failed_refresh_preserves_prior_state_and_applies_buffered_events():
    transport = _state_transport()
    transport._sessions = {
        "A1": {"auditSessionId": "A1", "state": "STARTED"},
    }

    def query(self, *_args, **_kwargs):
        self._apply_session({
            "auditSessionId": "A2",
            "state": "STARTED",
        })
        raise TimeoutError("snapshot timed out")

    transport._query = MethodType(query, transport)
    try:
        transport._refresh_sessions(force=True)
    except TimeoutError:
        pass

    assert set(transport._sessions) == {"A1", "A2"}
    assert not transport._syncing


def test_endpoint_paging_is_bounded_and_shared_cache_avoids_a_second_read(monkeypatch):
    import ise_exporter3.transports.pxgrid as pxgrid_transport

    monkeypatch.setattr(pxgrid_transport, "ENDPOINT_PAGE_SIZE", 2)
    transport = _state_transport()
    calls = []

    def query(self, _service, _endpoint, body, **_kwargs):
        calls.append(body["startIndex"])
        if body["startIndex"] == 0:
            return {"endpoints": {
                "endpoint": [{"id": "1"}, {"id": "2"}],
            }}
        return {"endpoints": [{"id": "3"}]}

    transport._query = MethodType(query, transport)
    first = transport.get_endpoints(max_age=3600)
    second = transport.get_endpoints(max_age=3600)

    assert [row["id"] for row in first] == ["1", "2", "3"]
    assert second == first
    assert calls == [0, 2]


def test_endpoint_paging_stops_when_the_server_ignores_start_index(monkeypatch):
    import ise_exporter3.transports.pxgrid as pxgrid_transport

    monkeypatch.setattr(pxgrid_transport, "ENDPOINT_PAGE_SIZE", 2)
    transport = _state_transport()
    calls = []

    def query(self, _service, _endpoint, body, **_kwargs):
        calls.append(body["startIndex"])
        return {"endpoints": [{"id": "1"}, {"id": "2"}]}

    transport._query = MethodType(query, transport)
    try:
        transport.get_endpoints(max_age=3600)
        raise AssertionError("a repeated page must not be paged forever")
    except TransportError as error:
        assert error.reason == "invalid_response"

    assert calls == [0, 2]


class _PostTransport(PxGridTransport):
    def __init__(self, response=None, error=None):
        self.config = _config()
        self.settings = self.config.target("pxgrid")
        self.timeout = self.settings.request_timeout
        self._request_lock = threading.RLock()
        self._closed = threading.Event()
        self._shutdown = None
        self._guard = _AlwaysOpenGuard()
        self.limiter = _NullLimiter()
        self.session = _FakeSession(response, error)


class _AlwaysOpenGuard:
    def blocked(self, _now):
        return False

    def success(self):
        pass


class _NullLimiter:
    def acquire(self, _api):
        pass


class _FakeSession:
    def __init__(self, response, error):
        self._response = response
        self._error = error

    def post(self, *_args, **_kwargs):
        if self._error is not None:
            raise self._error
        return self._response


class _FakeResponse:
    def __init__(self, status, *, chunks=(), raises=None):
        self.status_code = status
        self.headers = {}
        self._chunks = chunks
        self._raises = raises
        self.closed = False

    def iter_content(self, chunk_size=None):
        for chunk in self._chunks:
            yield chunk
        if self._raises is not None:
            raise self._raises

    def close(self):
        self.closed = True


def _error_count(api, kind):
    value = REGISTRY.get_sample_value(
        "ise3_api_errors_total",
        {"target": "pxgrid", "api": api, "error_type": kind, "http_code": "0"})
    return value or 0.0


def test_a_broken_error_body_still_classifies_the_http_failure():
    response = _FakeResponse(502, raises=RuntimeError("stream reset"))
    transport = _PostTransport(response)

    try:
        transport._post(
            "https://ise01.ise.lab/rest/getEndpoints", {},
            api="pxgrid_get_endpoints")
        raise AssertionError("HTTP 502 must raise")
    except TransportError as error:
        assert error.reason == "http_error"

    assert response.closed
    assert REGISTRY.get_sample_value(
        "ise3_api_requests_total",
        {"target": "pxgrid", "api": "pxgrid_get_endpoints", "status": "http_502"})


def test_a_body_that_dies_in_transit_is_a_connection_failure():
    import requests

    before = _error_count("pxgrid_get_sessions", "connection_error")
    transport = _PostTransport(_FakeResponse(
        200, raises=requests.exceptions.ChunkedEncodingError("truncated")))

    try:
        transport._post(
            "https://ise01.ise.lab/rest/getSessions", {},
            api="pxgrid_get_sessions")
        raise AssertionError("a truncated body must raise")
    except TransportError as error:
        assert error.reason == "connection_failed"

    assert _error_count("pxgrid_get_sessions", "connection_error") == before + 1


def test_an_unclassified_request_failure_becomes_unexpected_error():
    before = _error_count("pxgrid_serviceloookup", "unknown")
    transport = _PostTransport(error=RuntimeError("adapter exploded"))

    try:
        transport._post(
            "https://ise01.ise.lab/rest/getSessions", {},
            api="pxgrid_serviceloookup")
        raise AssertionError("an unclassified failure must raise")
    except TransportError as error:
        assert error.reason == "unexpected_error"

    assert _error_count("pxgrid_serviceloookup", "unknown") == before + 1


class _DatasetTransport(Transport):
    target = "pxgrid"

    def __init__(self):
        self.sessions = [
            {
                "auditSessionId": "A1",
                "state": "STARTED",
                "callingStationId": "00-11-22-33-44-55",
                "nasIpAddress": "10.0.0.1",
                "postureStatus": "Compliant",
                "mdmRegistered": True,
            },
            {
                "auditSessionId": "A2",
                "state": "STARTED",
                "callingStationId": "00-11-22-33-44-66",
                "nasIpAddress": "10.0.0.1",
                "postureStatus": "NonCompliant",
            },
        ]

    def get_sessions(self, *, max_age):
        assert max_age == 300
        return self.sessions


def _run(dataset):
    provider = dataset.provider("pxgrid")
    entry = PlannedDataset(
        name=dataset.name,
        description=dataset.description,
        enabled=True,
        interval=dataset.default_interval,
        dataset=dataset,
        provider=provider,
    )
    return Runner(_config()).run(entry, _DatasetTransport())


def test_session_and_posture_datasets_publish_from_one_reconciled_state():
    assert _run(active_sessions.DATASET).ok
    assert _run(posture_current.DATASET).ok
    assert REGISTRY.get_sample_value(
        "ise3_active_sessions_total", {"provider": "pxgrid"}) == 2
    assert REGISTRY.get_sample_value(
        "ise3_active_session_endpoints", {"provider": "pxgrid"}) == 2
    assert REGISTRY.get_sample_value(
        "ise3_posture_endpoints",
        {"provider": "pxgrid", "status": "Compliant", "ops_owner": "unknown"},
    ) == 1


def test_sequence_gap_forces_a_reconciled_resnapshot():
    transport = _state_transport()
    assert not transport._sequence_gap({"sequence": 10})
    assert not transport._sequence_gap({"sequence": 11})
    assert transport._sequence_gap({"sequence": 13})
    # A sequence reset means the same thing: the non-replayable interval is
    # uncertain and must be replaced from getSessions.
    assert transport._sequence_gap({"sequence": 0})


def test_a_reconnect_re_anchors_the_session_sequence():
    transport = _state_transport()
    assert not transport._sequence_gap({"sequence": 10})
    transport._sequence.clear()
    assert not transport._sequence_gap({"sequence": 4001})
    assert transport._sequence_gap({"sequence": 4003})


def test_a_non_container_frame_is_not_a_sequence_gap():
    transport = _state_transport()
    for payload in (None, 0, True, "sequence"):
        assert not transport._sequence_gap(payload)


def test_rest_failover_reissues_a_rotated_access_secret():
    transport = object.__new__(PxGridTransport)
    transport.settings = _config().target("pxgrid")
    transport._services = {}
    transport._rest_services = {}
    transport._peer_secrets = {}
    transport._activated = True
    secrets = []

    def control(self, operation, body=None):
        if operation == "ServiceLookup":
            return {"services": [{
                "nodeName": "peer-1",
                "properties": {"restBaseUrl": "https://ise01.ise.lab/rest"},
            }]}
        secrets.append(body["peerNodeName"])
        return {"secret": f"secret-{len(secrets)}"}

    def post(self, _url, _body, **_kwargs):
        raise TransportError("authentication_failed")

    transport._control = MethodType(control, transport)
    transport._post = MethodType(post, transport)

    try:
        transport._query(
            SESSION_SERVICE, "getSessions", {}, api="pxgrid_get_sessions")
    except TransportError as error:
        assert error.reason == "authentication_failed"

    assert secrets == ["peer-1", "peer-1"]


def _demand_transport():
    """Enough of a transport to exercise prepare/satisfies without a network."""
    transport = object.__new__(PxGridTransport)
    transport._prepare_lock = threading.Lock()
    transport._stream_lock = threading.Lock()
    transport._supervisor = None
    transport._awaiting_stream = False
    transport._discovered = True
    transport._services = {}
    transport._rest_services = {}
    transport._capabilities = {"capability:pxgrid_session_topic",
                               "capability:pxgrid_endpoints"}
    transport._connected = threading.Event()
    transport._connected.set()
    transport.timeout = 1
    transport._stopping = MethodType(lambda self: False, transport)
    transport._validate_files = MethodType(lambda self: None, transport)
    transport._activate = MethodType(lambda self: None, transport)
    transport._start_supervisor = MethodType(
        lambda self: setattr(self, "_supervisor", "started"), transport)
    return transport


def test_a_baseline_still_in_flight_is_pending_not_a_connection_failure():
    # The first production getSessions baseline can outlive the bounded wait in
    # _await_stream. Until the stream has failed at something, the honest
    # answer is "not yet": reporting it as connection_failed counted a strike
    # per cycle until the scheduler stepped active_sessions off a source that
    # was only warming -- the exact escalation PENDING_REASONS exists to stop.
    transport = _demand_transport()
    transport._connected.clear()
    transport._stream_error = None
    ready, reason, detail = transport.satisfies(
        ("capability:pxgrid_session_topic",))
    assert not ready and reason == "baseline_pending"
    assert "baseline" in detail

    # A stream that actually failed keeps its real classification.
    transport._stream_error = TransportError("tls_failed", "handshake refused")
    ready, reason, detail = transport.satisfies(
        ("capability:pxgrid_session_topic",))
    assert not ready and reason == "tls_failed"
    assert detail == "handshake refused"


def test_baseline_pending_is_bounded_and_the_scheduler_treats_it_as_not_yet():
    from ise_exporter3.scheduler import PENDING_REASONS, SLOW_RETRY_REASONS
    from ise_exporter3.transports import FAILURE_EXPLANATIONS, FAILURE_REASONS

    assert "baseline_pending" in FAILURE_REASONS
    assert FAILURE_EXPLANATIONS["baseline_pending"]
    assert "baseline_pending" in PENDING_REASONS
    assert "baseline_pending" not in SLOW_RETRY_REASONS


def test_the_session_stream_is_started_by_demand_not_by_advertisement():
    # An endpoint-only deployment plans streams=0 for pxgrid. Subscribing anyway
    # holds the whole session map, re-baselines getSessions on every reconnect
    # against the token bucket the operator sized from that plan, and publishes
    # a second session count next to the one MnT supplies.
    transport = _demand_transport()
    transport.prepare()
    assert transport._supervisor is None

    assert transport.satisfies(("capability:pxgrid_endpoints",))[0]
    assert transport._supervisor is None

    assert transport.satisfies(("capability:pxgrid_session_topic",))[0]
    assert transport._supervisor == "started"


def test_asking_for_the_held_snapshot_really_does_not_reconcile():
    # What /api/v1/pxgrid/sessions depends on. The route serves memory this
    # process already holds and must never be the reason a full getSessions
    # runs -- an operator refreshing a page would otherwise schedule one. Its
    # own test can only prove which argument it passes; this proves the
    # argument means what it needs it to, against the transport that has to
    # honour it.
    transport = _state_transport()
    transport._connected = threading.Event()
    transport._connected.set()
    transport._sessions = {"a": {"macAddress": "02:1E:5E:00:00:01"}}
    reconciles = []
    transport._refresh_sessions = MethodType(
        lambda self, *, force=False: reconciles.append(force), transport)

    # A baseline older than any max_age a caller could name.
    transport._session_snapshot_at = 1.0
    assert len(transport.get_sessions(refresh=False)) == 1
    assert reconciles == []

    # And the same transport does reconcile without it, so the assertion above
    # is not passing because this stub never refreshes at all.
    transport.get_sessions()
    assert reconciles == [True]


def test_an_age_measured_from_the_epoch_is_reported_as_no_age_at_all():
    # Before any baseline, _session_snapshot_at is 0.0. Subtracting that from
    # the wall clock is a fifty-six-year-old snapshot, which is not an answer.
    transport = _state_transport()
    assert transport.snapshot_age() is None
    transport._session_snapshot_at = time.time() - 30
    assert 29 <= transport.snapshot_age() <= 31
