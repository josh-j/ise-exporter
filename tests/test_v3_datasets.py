"""The four REST datasets, driven through the real runner with a fake transport.

Each one is checked twice: that a well-formed Patch 11 payload produces the
expected series, and that a malformed one fails the whole dataset instead of
publishing a partial answer. Configuration state is what operators page on, so a
half-right deployment view is worse than a visible outage.
"""
from datetime import datetime, timedelta, timezone

import pytest
from prometheus_client import REGISTRY

from ise_exporter3.config import Config
from ise_exporter3.datasets import certificates, deployment, licensing, patches
from ise_exporter3.plan import PlannedDataset
from ise_exporter3.runtime import Runner
from ise_exporter3.transports import Transport


class FakeTransport(Transport):
    """Answers the OpenAPI calls a dataset makes, and records them.

    Subclasses Transport so prepare()/satisfies() stay in step with the real
    contract rather than drifting into a double that no longer resembles it.
    """

    target = "pan"

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def _answer(self, path):
        self.calls.append(path)
        if path not in self.responses:
            raise AssertionError(f"unexpected request for {path}")
        value = self.responses[path]
        if isinstance(value, Exception):
            raise value
        return value

    def get_openapi(self, path, *, api="openapi", unwrap=True, params=None):
        return self._answer(path)

    def get_openapi_all(self, path, *, api="openapi", params=None,
                        max_pages=100, max_rows=10_000):
        return self._answer(path)

    def get_ers(self, path, *, params=None, get_all=False, api="ers"):
        return self._answer(path)

    def get_ers_total(self, path, *, params=None, api="ers"):
        return self._answer(f"{path}?total")


def _config():
    return Config.from_document(
        {"targets": {"pan": {"host": "pan1", "user": "ro"}}},
        path="test.toml", environ={"ISE_PASS": "secret"})


def _run(dataset, responses):
    entry = PlannedDataset(
        name=dataset.name, description=dataset.description, enabled=True,
        interval=dataset.default_interval, dataset=dataset,
        provider=dataset.providers[0])
    transport = FakeTransport(responses)
    return Runner(_config()).run(entry, transport), transport


def _sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels)


# --- deployment -------------------------------------------------------------

NODES = [
    {"hostname": "pan1", "nodeStatus": "Connected",
     "roles": ["PrimaryAdmin"], "services": ["Session", "Profiler"]},
    {"hostname": "psn1", "nodeStatus": "Connected", "roles": [], "services": ["Session"]},
]
DEPLOYMENT_OK = {
    "/deployment/node": NODES,
    "/deployment/pan-ha": {"isEnabled": True},
}


def test_deployment_publishes_state_roles_services_and_ha():
    outcome, transport = _run(deployment.DATASET, DEPLOYMENT_OK)
    assert outcome.ok
    assert _sample("ise3_deployment_node_state", provider="openapi", node="pan1",
                   roles="PrimaryAdmin", services="Session,Profiler",
                   state="Connected") == 1
    # Every other state is published as 0 so a transition is visible rather than
    # a series that quietly disappears.
    assert _sample("ise3_deployment_node_state", provider="openapi", node="pan1",
                   roles="PrimaryAdmin", services="Session,Profiler",
                   state="Disconnected") == 0
    assert _sample("ise3_deployment_nodes", provider="openapi", role="PrimaryAdmin") == 1
    assert _sample("ise3_deployment_nodes", provider="openapi", role="PSN") == 1
    assert _sample("ise3_deployment_node_service_enabled", provider="openapi",
                   node="pan1", service="Profiler") == 1
    assert _sample("ise3_deployment_pan_ha_enabled", provider="openapi") == 1
    # The declared cost of two requests is what actually happens.
    assert len(transport.calls) == 2


@pytest.mark.parametrize("responses,reason", [
    ({"/deployment/node": [], "/deployment/pan-ha": {"isEnabled": True}},
     "invalid_response"),
    ({"/deployment/node": [{"hostname": "pan1", "nodeStatus": "Melted",
                            "roles": [], "services": []}],
      "/deployment/pan-ha": {"isEnabled": True}}, "invalid_response"),
    ({"/deployment/node": [{"hostname": "pan1", "nodeStatus": "Connected",
                            "roles": ["Emperor"], "services": []}],
      "/deployment/pan-ha": {"isEnabled": True}}, "invalid_response"),
    ({"/deployment/node": NODES, "/deployment/pan-ha": {"isEnabled": "yes"}},
     "invalid_response"),
])
def test_deployment_fails_closed_on_a_payload_it_does_not_recognise(responses, reason):
    outcome, _ = _run(deployment.DATASET, responses)
    assert not outcome.ok and outcome.reason == reason


def test_deployment_rejects_a_duplicate_hostname():
    outcome, _ = _run(deployment.DATASET, {
        "/deployment/node": [NODES[0], NODES[0]],
        "/deployment/pan-ha": {"isEnabled": False}})
    assert not outcome.ok


# --- patches ----------------------------------------------------------------

PATCHES_OK = {"/patch": {"iseVersion": "3.3.0.430",
                         "patchVersion": [{"patchNumber": 11}, {"patchNumber": 4}]}}


def test_patches_publishes_the_level_and_each_installed_patch():
    outcome, _ = _run(patches.DATASET, PATCHES_OK)
    assert outcome.ok
    assert _sample("ise3_patch_level", provider="openapi") == 11
    assert _sample("ise3_patch_installed", provider="openapi", patch_number="4") == 1
    assert _sample("ise3_version_supported", provider="openapi",
                   version="3.3.0.430") == 1


@pytest.mark.parametrize("payload", [
    {"iseVersion": "3.4.0.1", "patchVersion": [{"patchNumber": 11}]},
    {"iseVersion": "3.3.0.430", "patchVersion": [{"patchNumber": 9}]},
    {"iseVersion": "3.3.0.430", "patchVersion": [{"patchNumber": "eleven"}]},
    {"iseVersion": "3.3.0.430", "patchVersion": "not-a-list"},
])
def test_patches_fails_closed_outside_the_supported_contract(payload):
    # The exact-release check is the point: an untested release must not quietly
    # start exporting numbers.
    outcome, _ = _run(patches.DATASET, {"/patch": payload})
    assert not outcome.ok


# --- licensing --------------------------------------------------------------

LICENSE_OK = {"/license/system/tier-state": [
    {"name": "ESSENTIAL", "consumptionCounter": 120, "compliance": "COMPLIANT",
     "status": "ENABLED"},
    {"name": "ADVANTAGE", "consumptionCounter": 0, "compliance": "EVALUATION",
     "status": "DISABLED"},
]}


def test_licensing_publishes_consumption_enablement_and_compliance():
    outcome, _ = _run(licensing.DATASET, LICENSE_OK)
    assert outcome.ok
    assert _sample("ise3_license_consumption", provider="openapi", tier="ESSENTIAL") == 120
    assert _sample("ise3_license_enabled", provider="openapi", tier="ESSENTIAL") == 1
    assert _sample("ise3_license_compliant", provider="openapi", tier="ESSENTIAL") == 1
    # EVALUATION is a real state but is not compliance.
    assert _sample("ise3_license_compliant", provider="openapi", tier="ADVANTAGE") == 0


@pytest.mark.parametrize("tier", [
    {"name": "ESSENTIAL", "consumptionCounter": 1, "compliance": "SPLENDID",
     "status": "ENABLED"},
    {"name": "ESSENTIAL", "consumptionCounter": 1, "compliance": "COMPLIANT",
     "status": "PERHAPS"},
    {"name": "", "consumptionCounter": 1, "compliance": "COMPLIANT", "status": "ENABLED"},
    {"name": "ESSENTIAL", "compliance": "COMPLIANT", "status": "ENABLED"},
])
def test_licensing_fails_closed_on_an_unknown_vocabulary(tier):
    outcome, _ = _run(licensing.DATASET, {"/license/system/tier-state": [tier]})
    assert not outcome.ok


# --- certificates -----------------------------------------------------------

def _certificate(name, days, **overrides):
    expires = datetime.now(timezone.utc) + timedelta(days=days)
    row = {"friendlyName": name, "expirationDate": expires.isoformat(),
           "selfSigned": False, "keySize": 2048, "usedBy": "Admin"}
    row.update(overrides)
    return row


CERTIFICATES_OK = {
    "/deployment/node": [NODES[0]],
    "/certs/system-certificate/pan1": [_certificate("admin", 10),
                                       _certificate("portal", 200)],
    "/certs/trusted-certificate": [_certificate("root-ca", -5)],
}


def test_certificates_counts_expiry_buckets_cumulatively():
    outcome, _ = _run(certificates.DATASET, CERTIFICATES_OK)
    assert outcome.ok
    # A certificate expiring in ten days counts in 30, 60 and 90.
    assert _sample("ise3_certificates_expiring_soon", provider="openapi",
                   threshold_days="30") == 1
    assert _sample("ise3_certificates_expiring_soon", provider="openapi",
                   threshold_days="90") == 1
    assert _sample("ise3_certificates_expired", provider="openapi") == 1
    assert _sample("ise3_certificate_expiry_days", provider="openapi", node="pan1",
                   certificate="portal", store="system", usage="Admin") == 199


def test_certificates_fails_closed_on_an_unparseable_expiry():
    outcome, _ = _run(certificates.DATASET, {
        **CERTIFICATES_OK,
        "/certs/system-certificate/pan1": [_certificate("admin", 10,
                                                        expirationDate="soon")]})
    assert not outcome.ok and outcome.reason == "invalid_response"


def test_certificates_fetches_its_own_node_list_rather_than_a_shared_cache():
    # v2 shared a module-level node cache between datasets. Paying the request
    # keeps the cost declared instead of hidden behind another dataset's run.
    _, transport = _run(certificates.DATASET, CERTIFICATES_OK)
    assert "/deployment/node" in transport.calls


# --- backup -----------------------------------------------------------------

def _backup(payload):
    return {"/backup-restore/config/last-backup-status": payload}


def test_backup_publishes_age_and_status_for_a_completed_backup():
    from ise_exporter3.datasets import backup
    completed = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    outcome, _ = _run(backup.DATASET, _backup(
        {"status": "COMPLETED", "startDate": completed}))
    assert outcome.ok
    assert _sample("ise3_backup_configured", provider="openapi") == 1
    assert _sample("ise3_backup_age_hours", provider="openapi") == pytest.approx(5, abs=0.1)
    assert _sample("ise3_backup_last_status", provider="openapi",
                   status="COMPLETED") == 1
    assert _sample("ise3_backup_last_status", provider="openapi", status="ERROR") == 0


def test_a_deployment_that_never_ran_a_backup_is_reported_not_failed():
    # "No backup is configured" is exactly what an operator needs to see; an
    # error would hide it behind a failure reason.
    from ise_exporter3.datasets import backup
    outcome, _ = _run(backup.DATASET, _backup({}))
    assert outcome.ok
    assert _sample("ise3_backup_configured", provider="openapi") == 0


@pytest.mark.parametrize("payload", [
    {"status": "SPLENDID", "startDate": "2026-01-01T00:00:00Z"},
    {"status": "COMPLETED"},
    {"status": "COMPLETED", "startDate": "not-a-date"},
    {"status": "COMPLETED", "startDate": "2099-01-01T00:00:00Z"},
])
def test_backup_fails_closed_on_a_payload_it_cannot_trust(payload):
    from ise_exporter3.datasets import backup
    outcome, _ = _run(backup.DATASET, _backup(payload))
    assert not outcome.ok


# --- network devices --------------------------------------------------------

# The ERS *list* carries identity and a link -- not NetworkDeviceGroupList.
# Group membership needs one detail request per device, which is the whole
# reason this dataset converges rather than classifying everything at once.
NADS = [
    {"id": "d1", "name": "sw-01"},
    {"id": "d2", "name": "sw-02"},
    {"id": "d3", "name": "ap-01"},
]

NAD_DETAILS = {
    "/config/networkdevice/d1": {"NetworkDevice": {"NetworkDeviceGroupList": [
        "Location#All Locations#Germany#Ramstein AB",
        "Ops Owner#All Ops Owners#Network Team",
        "Device Type#All Device Types#Switch"]}},
    "/config/networkdevice/d2": {"NetworkDevice": {"NetworkDeviceGroupList": [
        "Location#All Locations#Germany#Ramstein AB",
        "Ops Owner#All Ops Owners#Network Team"]}},
    "/config/networkdevice/d3": {"NetworkDevice": {"NetworkDeviceGroupList": []}},
}

NETWORK_DEVICES_OK = {"/config/networkdevice": NADS, **NAD_DETAILS}


@pytest.fixture(autouse=True)
def _fresh_device_cache():
    from ise_exporter3 import detail_cache, nad_directory
    from ise_exporter3.datasets import network_devices, tacacs_config
    for cache in (network_devices.CACHE, tacacs_config.CACHE):
        detail_cache._CACHES.pop(cache, None)
    nad_directory.shared().replace(())
    yield
    for cache in (network_devices.CACHE, tacacs_config.CACHE):
        detail_cache._CACHES.pop(cache, None)
    nad_directory.shared().replace(())


def test_network_devices_normalizes_group_strings_into_labels():
    from ise_exporter3.datasets import network_devices
    outcome, _ = _run(network_devices.DATASET, NETWORK_DEVICES_OK)
    assert outcome.ok
    assert _sample("ise3_network_devices_total", provider="ers") == 3
    # Location keeps its remaining path because sites nest; the others take the leaf.
    assert _sample("ise3_network_device_assignment", provider="ers", nad="sw-01",
                   location="Germany#Ramstein AB", ops_owner="Network Team",
                   device_type="Switch") == 1
    assert _sample("ise3_network_devices_by_ops_owner", provider="ers",
                   ops_owner="Network Team") == 2
    # A NAD with no groups still counts, under the shared unknown buckets.
    assert _sample("ise3_network_devices_by_location", provider="ers",
                   location="Unknown") == 1
    assert _sample("ise3_network_devices_by_type", provider="ers",
                   device_type="unknown") == 2


def test_group_membership_comes_from_the_detail_request_not_the_list():
    # The bug this guards: reading NetworkDeviceGroupList off the list response
    # classifies every NAD as unknown, because the list does not carry it.
    from ise_exporter3.datasets import network_devices
    _outcome, transport = _run(network_devices.DATASET, NETWORK_DEVICES_OK)
    assert "/config/networkdevice/d1" in transport.calls
    assert _sample("ise3_network_devices_classified", provider="ers") == 3


def test_an_inline_group_list_is_used_without_spending_a_detail_request():
    # Some ISE builds do return groups in the list. Take it for free.
    from ise_exporter3.datasets import network_devices
    inline = [{"id": "d1", "name": "sw-01", "NetworkDeviceGroupList": [
        "Ops Owner#All Ops Owners#Network Team"]}]
    _outcome, transport = _run(
        network_devices.DATASET, {"/config/networkdevice": inline})
    assert "/config/networkdevice/d1" not in transport.calls
    assert _sample("ise3_network_devices_by_ops_owner", provider="ers",
                   ops_owner="Network Team") == 1


def test_classification_converges_instead_of_sampling(monkeypatch):
    # A per-cycle budget must fill the cache, not cap coverage. This is the
    # documented v2 symptom: "NAD group detail coverage stuck at 10%".
    from ise_exporter3.datasets import network_devices
    monkeypatch.setattr(network_devices, "WARMUP_FETCHES_PER_CYCLE", 1)

    classified = []
    for _ in range(3):
        outcome, _ = _run(network_devices.DATASET, NETWORK_DEVICES_OK)
        assert outcome.ok
        classified.append(_sample("ise3_network_devices_classified", provider="ers"))
    assert classified == [1, 2, 3]


def test_an_unclassified_device_is_not_counted_as_unknown(monkeypatch):
    # Otherwise a NAD that simply has not been fetched yet is indistinguishable
    # from one genuinely missing its groups, and the ratio silently lies.
    from ise_exporter3.datasets import network_devices
    monkeypatch.setattr(network_devices, "WARMUP_FETCHES_PER_CYCLE", 0)
    outcome, _ = _run(network_devices.DATASET, NETWORK_DEVICES_OK)
    assert outcome.ok
    assert _sample("ise3_network_devices_total", provider="ers") == 3
    assert _sample("ise3_network_devices_classified", provider="ers") == 0
    assert _sample("ise3_network_devices_by_ops_owner", provider="ers",
                   ops_owner="unknown") is None


def test_one_unreachable_device_does_not_fail_the_inventory():
    from ise_exporter3.datasets import network_devices
    responses = dict(NETWORK_DEVICES_OK)
    responses["/config/networkdevice/d2"] = RuntimeError("ERS said no")
    outcome, _ = _run(network_devices.DATASET, responses)
    assert outcome.ok
    assert _sample("ise3_network_devices_classified", provider="ers") == 2


# --- TACACS configuration --------------------------------------------------

TACACS_CONFIG_OK = {
    "/config/internaluser": [
        {"id": "u1", "name": "admin"},
        {"id": "u2", "name": "automation"},
    ],
    "/config/internaluser/u1": {
        "InternalUser": {
            "enabled": "true",
            "passwordInfo": {"passwordNeverExpires": "true"},
        }
    },
    "/config/internaluser/u2": {
        "InternalUser": {
            "enabled": "false",
            "passwordInfo": {"passwordNeverExpires": "false"},
        }
    },
    "/policy/device-admin/policy-set": [
        {"id": "ps1", "name": "Device Admin"},
        {"id": "ps2", "name": "Break Glass"},
    ],
    "/config/tacacscommandsets?total": 1,
    "/config/tacacsprofile?total": 2,
}


def test_tacacs_config_publishes_each_device_admin_object_inventory():
    from ise_exporter3.datasets import tacacs_config

    outcome, transport = _run(tacacs_config.DATASET, TACACS_CONFIG_OK)
    assert outcome.ok
    assert _sample(
        "ise3_tacacs_policy_objects",
        provider="ers",
        object_type="policy_sets",
    ) == 2
    assert _sample(
        "ise3_tacacs_policy_objects",
        provider="ers",
        object_type="command_sets",
    ) == 1
    assert _sample(
        "ise3_tacacs_policy_objects",
        provider="ers",
        object_type="shell_profiles",
    ) == 2
    # Read off ERS, which keeps command sets and shell profiles in separate
    # collections, rather than off the Device Admin OpenAPI, whose profile list
    # on 3.3 P11 has the command sets mixed into it.
    assert "/config/tacacscommandsets?total" in transport.calls
    assert "/config/tacacsprofile?total" in transport.calls
    assert not [call for call in transport.calls if "shell-profiles" in call]


def test_active_sessions_mnt_attributes_on_the_nas_address_alone():
    # The MnT active list is a session index and carries no network device
    # element, so the NAS address is the only join key it offers. Reading a
    # device name that is never there left the unmatched label reading as a
    # device name and hid that the NAS address was doing all the work.
    from types import SimpleNamespace

    from ise_exporter3 import nad_directory
    from ise_exporter3.datasets import active_sessions

    nad_directory.shared().replace([{
        "nad": "campus-corp-wired", "location": "Ramstein",
        "ops_owner": "Network Team", "keys": ("10.200.40.12",)}])
    sessions = [
        {"calling_station_id": "00:11:22:33:44:55", "server": "laba-ise-001",
         "nas_ip_address": "10.200.40.12", "network_device_name": "ignored"},
        {"calling_station_id": "00:11:22:33:44:56", "server": "laba-ise-001",
         "nas_ip_address": "10.200.40.99", "network_device_name": "ignored"},
    ]

    published = []

    ctx = SimpleNamespace(
        interval=300,
        transport=SimpleNamespace(get_mnt_xml=lambda path, *, api="": {
            "total": len(sessions), "sessions": sessions}),
        set=lambda family, value, /, **labels: published.append(
            (family._name, value, labels)))
    active_sessions.fetch_mnt(ctx)

    by_nad = {tuple(sorted(labels.items())): value
              for name, value, labels in published
              if name == "ise3_active_sessions_by_nad"}
    assert by_nad[(("location", "Ramstein"), ("nad", "campus-corp-wired"))] == 1
    # Unattributable, and published as the address it actually came from.
    assert by_nad[(("location", "Unknown"), ("nad", "10.200.40.99"))] == 1


def test_an_empty_active_list_publishes_a_psn_zero_state():
    from types import SimpleNamespace

    from ise_exporter3.datasets import active_sessions

    published = []
    ctx = SimpleNamespace(
        interval=300,
        transport=SimpleNamespace(get_mnt_xml=lambda path, *, api="": {
            "total": 0, "sessions": []}),
        set=lambda family, value, /, **labels: published.append(
            (family._name, value, labels)))

    active_sessions.fetch_mnt(ctx)

    assert ("ise3_active_sessions_by_psn", 0,
            {"psn": "No active sessions"}) in published


def test_active_sessions_pxgrid_counts_sessions_per_serving_node():
    # The production regression: pxGrid is the preferred provider, and a commit
    # clears every family the dataset declares, so a pxgrid fetch that never
    # wrote by_psn left the per-PSN panels permanently empty wherever pxGrid
    # was healthy. The session record does name its serving node -- the
    # `providers` list, with "None" standing in for nothing, and scalar
    # spellings on other releases -- so the breakdown must come from here too.
    from types import SimpleNamespace

    from ise_exporter3 import nad_directory
    from ise_exporter3.datasets import active_sessions

    nad_directory.shared().replace([])
    sessions = [
        {"macAddress": "00:11:22:33:44:55", "nasIpAddress": "10.200.40.12",
         "providers": ["laba-ise-001"]},
        {"macAddress": "00:11:22:33:44:56", "nasIpAddress": "10.200.40.12",
         "providers": ["None"], "psnName": "laba-ise-002"},
        {"macAddress": "00:11:22:33:44:57", "nasIpAddress": "10.200.40.99",
         "providers": ["laba-ise-001"]},
    ]

    published = []
    ctx = SimpleNamespace(
        interval=300,
        transport=SimpleNamespace(get_sessions=lambda *, max_age: sessions),
        set=lambda family, value, /, **labels: published.append(
            (family._name, value, labels)))
    active_sessions.fetch_pxgrid(ctx)

    by_psn = {labels["psn"]: value for name, value, labels in published
              if name == "ise3_active_sessions_by_psn"}
    assert by_psn == {"laba-ise-001": 2, "laba-ise-002": 1}


def test_an_empty_pxgrid_baseline_publishes_the_same_psn_zero_state():
    # Empty-vs-failed must read the same whichever provider is active: a quiet
    # deployment on pxGrid gets the explicit zero, not the blank of a broken
    # collector.
    from types import SimpleNamespace

    from ise_exporter3.datasets import active_sessions

    published = []
    ctx = SimpleNamespace(
        interval=300,
        transport=SimpleNamespace(get_sessions=lambda *, max_age: []),
        set=lambda family, value, /, **labels: published.append(
            (family._name, value, labels)))

    active_sessions.fetch_pxgrid(ctx)

    assert ("ise3_active_sessions_by_psn", 0,
            {"psn": "No active sessions"}) in published


def _dataconnect_limits():
    from ise_exporter3 import limits as limits_module
    from ise_exporter3.model import Scale

    return limits_module.for_scale(
        Scale(nads=5_000, endpoints=100_000, sessions=20_000, accounts=1_000))


def _dataconnect_ctx(transport):
    from types import SimpleNamespace

    published, shared = [], []
    return SimpleNamespace(
        interval=300,
        dataset=SimpleNamespace(name="active_sessions", default_interval=300),
        limits=_dataconnect_limits(),
        transport=transport,
        option=lambda name: {"statement_timeout": 45}[name],
        set=lambda family, value, /, **labels: published.append(
            (family._name, value, labels)),
        set_shared=lambda family, value, /, **labels: shared.append(
            (family._name, value, labels)),
        published=published,
        shared=shared)


def test_active_sessions_dataconnect_dedupes_and_bounds_the_accounting_scan():
    # The reconstruction rule in one statement: latest record per session id
    # and calling station over the stale window, live means not a stop, and
    # every breakdown comes from that single pass under the group ceiling.
    from ise_exporter3.datasets import active_sessions

    sql = active_sessions.statement(1, _dataconnect_limits())
    assert "ROW_NUMBER() OVER" in sql
    assert "COALESCE(acct_session_id, session_id), calling_station_id" in sql
    assert "NOT LIKE '%STOP%'" in sql
    assert "GROUPING SETS ((psn), (nad), ())" in sql
    assert "NUMTODSINTERVAL(1, 'HOUR')" in sql
    assert "FETCH FIRST" in sql and "COUNT(*) OVER ()" in sql


def test_active_sessions_dataconnect_reconstructs_the_three_breakdowns():
    from ise_exporter3 import nad_directory
    from ise_exporter3.datasets import active_sessions

    nad_directory.shared().replace([{
        "nad": "campus-corp-wired", "location": "Ramstein",
        "ops_owner": "Network Team", "keys": ("10.200.40.12",)}])

    rows = [
        {"dimension": "total", "value": "all", "sessions": 3, "endpoints": 2,
         "nas_ip": None, "group_total": 5},
        {"dimension": "psn", "value": "laba-ise-001", "sessions": 2,
         "endpoints": 2, "nas_ip": None, "group_total": 5},
        {"dimension": "psn", "value": "laba-ise-002", "sessions": 1,
         "endpoints": 1, "nas_ip": None, "group_total": 5},
        {"dimension": "nad", "value": "sw-campus-1", "sessions": 2,
         "endpoints": 2, "nas_ip": "10.200.40.12", "group_total": 5},
        {"dimension": "nad", "value": "10.200.40.99", "sessions": 1,
         "endpoints": 1, "nas_ip": "10.200.40.99", "group_total": 5},
    ]

    class Transport:
        schema = None

        def query_many(self, statements, *, timeout=None):
            assert set(statements) == {"sessions"}
            # The costliest recurring statement in v2: the dataset must pass
            # its declared statement budget through to the transport.
            assert timeout == 45
            return {"sessions": rows}

    ctx = _dataconnect_ctx(Transport())
    active_sessions.fetch_dataconnect(ctx)

    assert ("ise3_active_sessions_total", 3.0, {}) in ctx.published
    assert ("ise3_active_session_endpoints", 2.0, {}) in ctx.published
    by_psn = {labels["psn"]: value for name, value, labels in ctx.published
              if name == "ise3_active_sessions_by_psn"}
    assert by_psn == {"laba-ise-001": 2.0, "laba-ise-002": 1.0}
    by_nad = {tuple(sorted(labels.items())): value
              for name, value, labels in ctx.published
              if name == "ise3_active_sessions_by_nad"}
    # Attributed through the NAS address the group carries, like the other
    # providers; unresolved devices are published as themselves.
    assert by_nad[(("location", "Ramstein"), ("nad", "campus-corp-wired"))] == 2.0
    assert by_nad[(("location", "Unknown"), ("nad", "10.200.40.99"))] == 1.0


def test_an_empty_accounting_window_publishes_the_same_psn_zero_state():
    # A healthy empty window comes back as the lone zero total row the empty
    # grouping set produces (or as no rows at all); either way the zero
    # sentinel keeps it distinct from a failed collector.
    from ise_exporter3.datasets import active_sessions

    class Transport:
        schema = None

        def query_many(self, statements, *, timeout=None):
            return {"sessions": [{
                "dimension": "total", "value": "all", "sessions": 0,
                "endpoints": 0, "nas_ip": None, "group_total": 1}]}

    ctx = _dataconnect_ctx(Transport())
    active_sessions.fetch_dataconnect(ctx)

    assert ("ise3_active_sessions_total", 0.0, {}) in ctx.published
    assert ("ise3_active_sessions_by_psn", 0,
            {"psn": "No active sessions"}) in ctx.published


def test_an_empty_active_list_publishes_current_posture_zero_states():
    from types import SimpleNamespace

    from ise_exporter3.datasets import posture_current

    published = []
    ctx = SimpleNamespace(
        interval=300,
        transport=SimpleNamespace(get_mnt_xml=lambda path, *, api="": {
            "total": 0, "sessions": []}),
        set=lambda family, value, /, **labels: published.append(
            (family._name, value, labels)))

    posture_current.fetch_mnt(ctx)

    assert ("ise3_posture_agent_version_endpoints", 0,
            {"agent_version": "No active Secure Client sessions"}) in published
    assert ("ise3_posture_endpoints_by_os", 0,
            {"os": "No active endpoints"}) in published
    assert ("ise3_posture_policy_results", 0,
            {"policy": "No active posture policies", "result": "Failed"}) in published
