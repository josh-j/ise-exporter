"""pxGrid v3 transport and dataset contract regressions."""
from __future__ import annotations

import threading
from types import MethodType

from prometheus_client import REGISTRY

from ise_exporter3.config import Config
from ise_exporter3.datasets import active_sessions, endpoint_attributes, posture_current
from ise_exporter3.model import Scale
from ise_exporter3.plan import PlannedDataset, build_plan
from ise_exporter3.pxgrid import as_list
from ise_exporter3.runtime import Runner
from ise_exporter3.transports import Transport
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
