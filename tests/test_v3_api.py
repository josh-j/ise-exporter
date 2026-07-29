"""The local operator API.

This is the surface that replaces v2's Python CLI, and the reason it lives in
the exporter process: an operator asking "which source is live" cannot bypass
the pacing gate or the authentication guard, because there is no second process
to bypass them from.
"""
import json
import urllib.error
import urllib.request

import pytest

from ise_exporter3.api import OperatorApi
from ise_exporter3.config import Config
from ise_exporter3.plan import build_plan
from ise_exporter3.scheduler import PROVIDER_FAILOVER_THRESHOLD, Scheduler
from ise_exporter3.server import HttpServer
from ise_exporter3.snapshots import LockedCollectorRegistry
from ise_exporter3.transports import TransportError
from tests.test_v3_scheduler import Clock, ScriptedTransport


def _config():
    return Config.from_document(
        {"targets": {"pan": {"host": "pan1", "user": "ro"}}},
        path="test.toml", environ={"ISE_PASS": "secret"})


def _api(transport=None, collect=True):
    config = _config()
    plan = build_plan(config)
    scheduler = Scheduler(config, plan, {"pan": transport or ScriptedTransport()},
                          asynchronous=False, clock=Clock())
    scheduler.bootstrap()
    if collect:
        scheduler.tick()
    return OperatorApi(config, plan, scheduler), scheduler


@pytest.fixture
def served():
    api, _scheduler = _api()
    server = HttpServer("127.0.0.1", 0, LockedCollectorRegistry(), routes=api.routes())
    server.start()
    try:
        yield f"http://127.0.0.1:{server.address[1]}"
    finally:
        server.stop(timeout=5)


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def test_health_answers_is_it_working_and_is_it_within_budget():
    api, _scheduler = _api()
    health = api.health()
    assert health["datasets_enabled"] > 0
    assert health["datasets_collecting"] == 8
    assert health["fits_budget"] in (True, False)
    assert isinstance(health["datasets_unresolved"], list)


def test_datasets_report_the_live_source_and_why_one_is_failing():
    class Selective(ScriptedTransport):
        def _answer(self, path):
            self.calls.append(path)
            if path == "/patch":
                raise TransportError("http_error", "ISE returned HTTP 503")
            return self.responses[path]

    api, _scheduler = _api(Selective())
    rows = {row["dataset"]: row for row in api.datasets()}

    assert rows["deployment"]["provider"] == "openapi"
    assert rows["deployment"]["consecutive_failures"] == 0
    assert rows["deployment"]["last_success_age_seconds"] is not None

    assert rows["patches"]["failure_reason"] == "http_error"
    assert "503" in rows["patches"]["failure_detail"]
    assert rows["patches"]["consecutive_failures"] == 1


def test_a_dataset_with_no_usable_source_is_reported_as_unscheduled():
    api, _scheduler = _api()
    rows = {row["dataset"]: row for row in api.datasets()}
    # No Oracle target is configured in this fixture.
    assert rows["radius_reporting"]["scheduled"] is False
    assert "radius_reporting" in api.health()["datasets_unresolved"] or (
        rows["radius_reporting"]["scheduled"] is False)


def test_providers_expose_what_each_source_can_actually_supply():
    # A fallback often answers a narrower question than the preferred source.
    # That difference is the thing an operator most needs to see.
    api, _scheduler = _api()
    rows = [row for row in api.providers()
            if row["dataset"] == "endpoint_inventory"]
    dataconnect = next(row for row in rows if row["provider"] == "dataconnect")
    ers = next(row for row in rows if row["provider"] == "ers")
    assert "identity_group" in dataconnect["supplies"]
    assert "identity_group" not in ers["supplies"]
    assert "ERS caps page size" in ers["notes"]


def test_targets_report_planned_load_against_the_declared_budget():
    api, _scheduler = _api()
    targets = {row["target"]: row for row in api.targets()}
    assert targets["pan"]["requests_per_hour"] > 0
    assert "budget_requests_per_hour" in targets["pan"]
    assert "over_budget" in targets["pan"]


def test_a_degraded_dataset_is_visible_through_the_api():
    from tests.test_v3_failover import _setup, _run_until_next

    scheduler, sources, clock = _setup()
    sources["pxgrid"].healthy = False
    for _ in range(PROVIDER_FAILOVER_THRESHOLD):
        _run_until_next(scheduler, clock)

    api = OperatorApi(scheduler.config, scheduler.plan, scheduler)
    assert api.health()["datasets_degraded"] == ["sessions"]
    row = next(row for row in api.datasets() if row["dataset"] == "sessions")
    assert row["degraded"] is True
    assert row["provider"] == "mnt"


def test_every_route_is_reachable_and_returns_json(served):
    for path in ("/api/v1", "/api/v1/health", "/api/v1/datasets",
                 "/api/v1/providers", "/api/v1/targets", "/api/v1/plan",
                 "/api/v1/dataconnect/status"):
        payload = _get(f"{served}{path}")
        assert payload is not None


def test_the_index_names_every_route_it_serves(served):
    index = _get(f"{served}/api/v1")
    for path in index["routes"]:
        assert path.startswith("/api/v1")
    assert "/api/v1/dataconnect/query" in index["routes"]


def test_the_plain_text_plan_is_the_same_report_the_command_prints(served):
    with urllib.request.urlopen(f"{served}/api/v1/plan.txt", timeout=5) as response:
        body = response.read().decode("utf-8")
    assert "DATASET" in body and "TARGET" in body
    assert "budget" in body.lower()


def test_metrics_are_still_served_beside_the_api(served):
    with urllib.request.urlopen(f"{served}/metrics", timeout=5) as response:
        assert "ise3_dataset_up" in response.read().decode("utf-8")


def test_an_unknown_route_is_a_404_not_a_stack_trace(served):
    with pytest.raises(urllib.error.HTTPError) as raised:
        _get(f"{served}/api/v1/nonsense")
    assert raised.value.code == 404


def test_the_api_serves_state_and_never_reaches_ise():
    # Every route outside the dataconnect namespace must answer from what the
    # exporter already computed. If one of them queried ISE, an operator
    # refreshing a page would be load. The dataconnect routes do reach ISE by
    # design, and refuse without an explorer -- which is the case here.
    transport = ScriptedTransport()
    api, _scheduler = _api(transport)
    before = len(transport.calls)
    for route in api.routes().values():
        route({})
    assert len(transport.calls) == before


# --- the pxGrid session directory -------------------------------------------

class _StubPxGrid:
    """A transport holding a snapshot, which is all this route ever reads."""

    def __init__(self, sessions=(), error=None, age=42.0):
        self._sessions = list(sessions)
        self._error = error
        self._age = age
        self.refreshes = []

    def snapshot_age(self):
        return self._age

    def get_sessions(self, *, max_age=300, refresh=True):
        self.refreshes.append(refresh)
        if self._error is not None:
            raise self._error
        return [dict(row) for row in self._sessions]


def _pxgrid_api(pxgrid):
    config = _config()
    plan = build_plan(config)
    return OperatorApi(config, plan, None, None, pxgrid, clock=lambda: 1_042.0)


def _body(response):
    status, payload, _content_type = response
    return status, json.loads(payload.decode("utf-8"))


# The field names and shapes ISE 3.3 Patch 11 actually returns from
# getSessions, captured off the lab rather than assumed. The first version of
# this projection read nasName for the device and left every NAD column empty
# against a real appliance.
SESSION = {
    "timestamp": "2026-07-29T16:33:54.527Z",
    "state": "STARTED",
    "userName": "ise-exporter-test-user",
    "callingStationId": "02:1E:5E:00:00:01",
    "calledStationId": "00:11:22:33:44:55",
    "ipAddresses": ["10.200.40.211"],
    "macAddress": "02:1E:5E:00:00:01",
    "nasIpAddress": "10.200.30.1",
    "nasPortId": "GigabitEthernet1/0/1",
    "nasIdentifier": "ise-exporter-live-test",
    "nasPortType": "Ethernet",
    "endpointProfile": "Unknown",
    # ISE writes the string "None" when it has no provider to name.
    "providers": ["None"],
    "serviceType": "Framed",
    "networkDeviceProfileName": "Cisco",
    "selectedAuthzProfiles": ["Lab_Employee_Full"],
    "authMethod": "dot1x",
    "authProtocol": "PAP_ASCII",
}


def test_the_session_directory_is_projected_not_served_whole():
    # A pxGrid session record is large and mostly internal. Serving it whole
    # would put the appliance's session table in a terminal and make every
    # internal field an accidental contract.
    status, payload = _body(_pxgrid_api(_StubPxGrid([SESSION])).pxgrid_sessions({}))
    assert status == 200
    row = payload["sessions"][0]
    assert row == {
        "mac_address": "02:1E:5E:00:00:01",
        "ip_address": "10.200.40.211",
        "user_name": "ise-exporter-test-user",
        # nasIdentifier, not nasName: the bug a live session found.
        "nad": "ise-exporter-live-test",
        "nas_ip_address": "10.200.30.1",
        "nas_port": "GigabitEthernet1/0/1",
        "endpoint_profile": "Unknown",
        "auth_method": "dot1x",
        "auth_protocol": "PAP_ASCII",
        # Absent from an ISE 3.3 session that ran no posture and got no SGT.
        # Empty because the appliance said nothing, not because it was missed.
        "posture_status": "",
        "authorization_profiles": "Lab_Employee_Full",
        "security_group": "",
        "ise_node": "",
        "session_state": "STARTED",
        "audit_session_id": "",
        "last_update": "2026-07-29T16:33:54.527Z",
    }


def test_the_device_name_comes_from_the_field_ise_actually_uses():
    # networkDeviceProfileName is always present and is the device *profile*
    # ("Cisco"), not its name. Reading it, or nasName, is how the NAD column
    # ends up empty on every row of a live deployment.
    from ise_exporter3.pxgrid import project_session
    assert project_session(SESSION)["nad"] == "ise-exporter-live-test"


def test_a_provider_of_none_is_not_a_node_name():
    from ise_exporter3.pxgrid import project_session
    assert project_session(SESSION)["ise_node"] == ""
    named = dict(SESSION, providers=["laba-ise-001"])
    assert project_session(named)["ise_node"] == "laba-ise-001"


def test_the_route_never_forces_a_refresh_an_operator_did_not_pay_for():
    # get_sessions reconciles when its baseline ages out, and that reconcile is
    # a full getSessions against the appliance. A page reload must not be able
    # to schedule one, so this asks for whatever is held.
    pxgrid = _StubPxGrid([SESSION])
    _pxgrid_api(pxgrid).pxgrid_sessions({})
    assert pxgrid.refreshes == [False]


def test_the_snapshot_age_is_reported_rather_than_hidden():
    # These are sessions as of a moment, and a caller joining them onto
    # endpoint rows should know which moment.
    _status, payload = _body(_pxgrid_api(_StubPxGrid([SESSION])).pxgrid_sessions({}))
    assert payload["snapshot_age_seconds"] == 42.0


def test_an_age_measured_from_the_epoch_is_not_an_age():
    # Before any baseline the honest answer is "unknown", not fifty-six years.
    _status, payload = _body(
        _pxgrid_api(_StubPxGrid([SESSION], age=None)).pxgrid_sessions({}))
    assert payload["snapshot_age_seconds"] is None


def test_a_session_without_a_mac_is_dropped_rather_than_served_unjoinable():
    pxgrid = _StubPxGrid([SESSION, {"userName": "nobody", "state": "STARTED"}])
    _status, payload = _body(_pxgrid_api(pxgrid).pxgrid_sessions({}))
    assert payload["row_count"] == 1


@pytest.mark.parametrize("pattern", [
    "02:1E:5E:00:00:01", "021e.5e00.0001", "021E5E000001", "02:1E:5E:*",
    # The same wildcards the cmdlets hand to a Data Connect LIKE. A mid-pattern
    # star used to be stripped and the trailing one read as a prefix, so this
    # matched every session on the appliance instead of the one asked for.
    "*5E000001", "*5E00*", "02:1E:5E:00:00:0?"])
def test_the_mac_filter_matches_however_the_caller_spells_it(pattern):
    # ISE writes one spelling, the operator pasted another. Comparing the
    # normalised forms is what makes a MAC copied off one screen work here.
    pxgrid = _StubPxGrid([SESSION, dict(SESSION, macAddress="FF:EE:DD:00:00:01")])
    _status, payload = _body(
        _pxgrid_api(pxgrid).pxgrid_sessions({"mac": pattern}))
    assert payload["row_count"] == 1
    assert payload["sessions"][0]["mac_address"] == "02:1E:5E:00:00:01"


def test_a_pattern_that_matches_nothing_matches_nothing():
    # The failure that matters is the other direction: a filter that quietly
    # stops filtering serves every session on the appliance to a caller who
    # asked for one.
    pxgrid = _StubPxGrid([SESSION, dict(SESSION, macAddress="FF:EE:DD:00:00:01")])
    for pattern in ("*9999*", "AA:BB:CC:DD:EE:FF", "nonsense"):
        _status, payload = _body(
            _pxgrid_api(pxgrid).pxgrid_sessions({"mac": pattern}))
        assert payload["row_count"] == 0, pattern


def test_the_row_ceiling_is_stated_rather_than_silently_applied():
    sessions = [dict(SESSION, macAddress=f"02:1E:5E:00:00:{index:02X}")
                for index in range(1, 6)]
    _status, payload = _body(
        _pxgrid_api(_StubPxGrid(sessions)).pxgrid_sessions({"first": "2"}))
    assert payload["row_count"] == 2
    assert payload["matched"] == 5
    assert payload["truncated"] is True


def test_no_pxgrid_target_is_a_refusal_that_names_itself():
    status, payload = _body(_pxgrid_api(None).pxgrid_sessions({}))
    assert status == 409
    assert payload["error"] == "pxgrid_unconfigured"


def test_a_disconnected_stream_is_a_refusal_not_a_traceback():
    # The caller can act on "pxGrid is not connected"; they cannot act on a 500.
    pxgrid = _StubPxGrid(error=TransportError(
        "connection_failed", "pxGrid session stream is not connected"))
    status, payload = _body(_pxgrid_api(pxgrid).pxgrid_sessions({}))
    assert status == 503
    assert payload["error"] == "connection_failed"


def test_the_route_is_listed_in_the_index():
    api, _scheduler = _api()
    routes = api.routes()
    assert "/api/v1/pxgrid/sessions" in routes
    _status, payload = _body(routes["/api/v1"]({}))
    assert "/api/v1/pxgrid/sessions" in payload["routes"]


# --- MnT session detail ------------------------------------------------------

class _StubMnt:
    """The MnT transport's surface, which is one method."""

    def __init__(self, record=None, error=None):
        self._record = record
        self._error = error
        self.paths = []

    def get_mnt_xml(self, path, *, api="mnt_xml"):
        self.paths.append(path)
        if self._error is not None:
            raise self._error
        return {"total": 0, "sessions": [dict(self._record)] if self._record else []}


# Field names taken from a live ISE 3.3 session record.
MNT_RECORD = {
    "user_name": "ise-exporter-test-user",
    "calling_station_id": "02:1E:5E:00:00:01",
    "nas_ip_address": "10.200.30.1",
    "acct_input_octets": "84213",
    "acct_output_octets": "12904",
    "audit_session_id": "0B0B0B0B00000001",
    "selected_azn_profiles": "Lab_Employee_Full",
    "identity_group": "Employees",
    "execution_steps": "11001,11017,11507",
}


def _mnt_api(mnt):
    config = _config()
    return OperatorApi(config, build_plan(config), None, None, None, mnt)


def test_the_session_detail_is_served_whole_because_that_is_the_point():
    # 39 of MnT's 47 fields have no counterpart anywhere else, so projecting
    # this down to a curated list would discard the reason to call it.
    mnt = _StubMnt(MNT_RECORD)
    status, payload = _body(_mnt_api(mnt).mnt_session({"mac": "02:1E:5E:00:00:01"}))
    assert status == 200
    assert payload["found"] is True
    assert payload["session"]["acct_input_octets"] == "84213"
    assert payload["session"]["execution_steps"] == "11001,11017,11507"
    assert len(payload["session"]) == len(MNT_RECORD)


@pytest.mark.parametrize("spelling", [
    "02:1E:5E:00:00:01", "021e.5e00.0001", "02-1E-5E-00-00-01", "021E5E000001"])
def test_any_spelling_of_a_mac_reaches_the_same_resource(spelling):
    mnt = _StubMnt(MNT_RECORD)
    _mnt_api(mnt).mnt_session({"mac": spelling})
    assert mnt.paths == ["/Session/MACAddress/02:1E:5E:00:00:01"]


@pytest.mark.parametrize("hostile", [
    "../../Version", "02:1E:5E:00:00:01/../../FailureReasons",
    "*", "", "..%2f..%2fVersion", "02:1E:5E:00:00:01 OR 1=1",
])
def test_only_a_mac_may_reach_the_url_path(hostile):
    # This value is interpolated into an MnT URL. The check is on the
    # normalised form, so it cannot be evaded by spelling the MAC differently,
    # and anything that is not a MAC never reaches the transport at all.
    mnt = _StubMnt(MNT_RECORD)
    status, payload = _body(_mnt_api(mnt).mnt_session({"mac": hostile}))
    assert status == 400
    assert payload["error"] == "invalid_request"
    assert mnt.paths == []


def test_a_mac_with_no_session_is_an_answer_not_a_failure():
    # MnT reports it as HTTP 500 with cpm-code 34110. Passing that on as an
    # error would make "not connected" look like an outage on every quiet
    # endpoint, which is most of them.
    mnt = _StubMnt(error=TransportError(
        "http_error", "ISE returned HTTP 500", status=500, code="34110"))
    status, payload = _body(_mnt_api(mnt).mnt_session({"mac": "02:1E:5E:00:00:01"}))
    assert status == 200
    assert payload["found"] is False
    assert payload["session"] is None


def test_a_real_mnt_failure_is_still_a_failure():
    mnt = _StubMnt(error=TransportError(
        "http_error", "ISE returned HTTP 500", status=500, code="12345"))
    status, payload = _body(_mnt_api(mnt).mnt_session({"mac": "02:1E:5E:00:00:01"}))
    assert status == 502


def test_no_mnt_target_is_a_refusal_that_names_itself():
    status, payload = _body(_mnt_api(None).mnt_session({"mac": "02:1E:5E:00:00:01"}))
    assert status == 409
    assert payload["error"] == "mnt_unconfigured"


def test_an_oversized_field_is_truncated_rather_than_served():
    config = _config()
    huge = dict(MNT_RECORD, other_attr_string="x" * (config.limits.field_bytes + 50))
    _status, payload = _body(_mnt_api(_StubMnt(huge)).mnt_session(
        {"mac": "02:1E:5E:00:00:01"}))
    assert payload["session"]["other_attr_string"].endswith("...[truncated]")


def test_a_record_the_field_ceiling_cut_short_says_so():
    # The one thing a caller cannot recover from a short record is that it was
    # short. Same reasoning the probe column's note is built on: only the
    # answer knows whether it is partial, so the answer has to say.
    from ise_exporter3 import api as api_module

    _status, payload = _body(_mnt_api(_StubMnt(MNT_RECORD)).mnt_session(
        {"mac": "02:1E:5E:00:00:01"}))
    assert payload["fields_omitted"] == 0

    wide = {f"field_{index:03d}": "x" for index in range(
        api_module.MNT_MAX_FIELDS + 12)}
    _status, payload = _body(_mnt_api(_StubMnt(wide)).mnt_session(
        {"mac": "02:1E:5E:00:00:01"}))
    assert len(payload["session"]) == api_module.MNT_MAX_FIELDS
    assert payload["fields_omitted"] == 12


def test_the_mnt_route_is_listed_in_the_index():
    api, _scheduler = _api()
    assert "/api/v1/mnt/session" in api.routes()
