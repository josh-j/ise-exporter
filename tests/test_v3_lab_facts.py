"""Expectations recorded against the live lab appliance.

These encode what ISE 3.3.0.430 Patch 11 on ``laba-ise-001.ise.lab`` actually
returns, as written up in ``docs/DATASETS_FACTS.md``. They exist so that drift
against the real appliance -- a renamed column, a changed envelope, a field that
stops being sent -- is caught rather than discovered as a silently empty metric.

They are **inert everywhere else**. The module skips unless ``ISE_LAB_TESTS`` is
set, and additionally skips when the appliance does not resolve or when the sops
secret cannot be decrypted, so an ordinary ``pytest`` run never touches a
network. No credential is embedded: both passwords are decrypted at runtime with
sops and are never logged, printed or asserted on.

Assertions are deliberately about *shape* -- field presence, envelope form,
column existence -- and not about volatile values such as the exact session or
row count, which change on their own.
"""
from __future__ import annotations

import os
import socket
import subprocess
import ssl
import warnings
import xml.etree.ElementTree as ET

import pytest
import requests
import urllib3

from ise_exporter3 import probe_data
from ise_exporter3.config import Config
from ise_exporter3.pxgrid import project_session
from ise_exporter3.transports import build_transports
from ise_exporter3.transports.dataconnect import SCHEMA_COLUMN_CONTRACTS


pytestmark = pytest.mark.skipif(
    not os.environ.get("ISE_LAB_TESTS"),
    reason="set ISE_LAB_TESTS=1 to probe the lab appliance")

HOST = os.environ.get("ISE_LAB_HOST", "laba-ise-001.ise.lab")
ERS_PORT = 9060
ORACLE_PORT = 2484
ORACLE_SERVICE = "cpm10"
ORACLE_USER = "dataconnect"
ADMIN_USER = os.environ.get("ISE_LAB_USER", "admin")
SECRETS = os.environ.get(
    "ISE_LAB_SECRETS", "/srv/nix-config/secrets/common.yaml")
AGE_KEY = os.environ.get(
    "SOPS_AGE_KEY_FILE", os.path.expanduser("~/.config/sops/age/keys.txt"))
TIMEOUT = 30

# One MnT detail fetch is one request against a production-shaped API; the
# module fetches at most one and reuses it, in the same small-page discipline
# the exporter itself keeps.
SMALL_PAGE = 5


def _secret(key):
    """Decrypt one sops value, or skip. The value is never logged."""
    try:
        result = subprocess.run(
            ["sops", "-d", "--extract", f'["{key}"]', SECRETS],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "SOPS_AGE_KEY_FILE": AGE_KEY})
    except (OSError, subprocess.SubprocessError):
        pytest.skip("sops is unavailable on this host")
    if result.returncode != 0 or not result.stdout.strip():
        pytest.skip(f"the sops secret {key} could not be decrypted here")
    return result.stdout.strip()


@pytest.fixture(scope="module")
def reachable():
    try:
        socket.getaddrinfo(HOST, 443)
    except socket.gaierror:
        pytest.skip(f"{HOST} does not resolve on this host")
    return HOST


@pytest.fixture(scope="module")
def admin(reachable):
    return (ADMIN_USER, _secret("lab_ise_ui_admin_pw"))


@pytest.fixture(scope="module")
def session(admin):
    """A TLS session against the lab. The lab CA is not in the trust store."""
    http = requests.Session()
    http.auth = requests.auth.HTTPBasicAuth(*admin)
    http.verify = False
    http.trust_env = False
    warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
    try:
        yield http
    finally:
        http.close()


def _get(session, url, accept="application/json"):
    try:
        response = session.get(
            url, headers={"Accept": accept}, timeout=TIMEOUT)
    except requests.RequestException as error:
        pytest.skip(f"the lab appliance is not answering: {type(error).__name__}")
    assert response.status_code == 200, url
    return response


def _mnt(session, path):
    return _get(session, f"https://{HOST}/admin/API/mnt{path}",
                accept="application/xml")


def _openapi(session, path):
    return _get(session, f"https://{HOST}/api/v1{path}").json()


def _ers(session, path):
    return _get(session, f"https://{HOST}:{ERS_PORT}/ers{path}").json()


# --- MnT XML ---------------------------------------------------------------

ACTIVE_LIST_FIELDS = {
    "user_name", "calling_station_id", "nas_ip_address", "acct_session_id",
    "audit_session_id", "server", "framed_ip_address", "framed_ipv6_address",
}
# Read off the ActiveList by designs that predate the live capture. None of
# these is on the document, which is why those designs collected nothing.
ACTIVE_LIST_ABSENT = {
    "network_device_name", "identity_group", "posture_status", "session_state",
    "location", "authentication_method",
}


@pytest.fixture(scope="module")
def active_list(session):
    root = ET.fromstring(_mnt(session, "/Session/ActiveList").content)
    rows = root.findall("activeSession")
    if not rows:
        pytest.skip("no active sessions on the appliance right now")
    return root, rows


def test_active_list_envelope_is_activelist_with_a_count(active_list):
    root, rows = active_list
    assert root.tag == "activeList"
    assert int(root.attrib["noOfActiveSession"]) == len(rows)


def test_active_list_carries_exactly_the_eight_known_fields(active_list):
    _, rows = active_list
    for row in rows:
        assert {child.tag for child in row} == ACTIVE_LIST_FIELDS


def test_active_list_omits_the_fields_designs_wrongly_read_from_it(active_list):
    _, rows = active_list
    for row in rows:
        assert ACTIVE_LIST_ABSENT.isdisjoint({child.tag for child in row})


def test_active_list_macs_are_colon_delimited_uppercase(active_list):
    _, rows = active_list
    for row in rows:
        mac = row.findtext("calling_station_id") or ""
        assert mac == mac.upper()
        assert mac.count(":") == 5


@pytest.fixture(scope="module")
def detail(session, active_list):
    """One per-MAC session detail document, fetched once for the module."""
    _, rows = active_list
    mac = rows[0].findtext("calling_station_id")
    root = ET.fromstring(_mnt(session, f"/Session/MACAddress/{mac}").content)
    return root, {child.tag: (child.text or "") for child in root}


# Present on all eight sampled sessions, and read (or worth reading) by the
# session_authorization / posture_current projections.
DETAIL_PRESENT = {
    "acs_server", "authentication_method", "authentication_protocol",
    "calling_station_id", "endpoint_policy", "execution_steps", "failed",
    "identity_store", "location", "message_code", "nas_ip_address",
    "network_device_name", "other_attr_string", "passed", "posture_status",
    "response_time", "selected_azn_profiles", "user_name",
}
# Every spelling session_detail.py tries for a fact ISE does not send at top
# level on 3.3 P11. A tag appearing here means a projection field went live.
DETAIL_ABSENT = {
    "failure_reason", "posture_report", "PostureReport",
    "posture_agent_version", "PostureAgentVersion", "agent_version",
    "operating_system", "os_type", "endpoint_operating_system",
    "step_latency", "StepLatency", "total_authen_latency", "TotalAuthenLatency",
    "total_authentication_latency", "TotalAuthenticationLatency",
    "identity_group", "session_state", "server",
}


def test_session_detail_envelope_and_present_fields(detail):
    root, fields = detail
    assert root.tag == "sessionParameters"
    assert DETAIL_PRESENT <= set(fields)


def test_session_detail_omits_every_projected_field_ise_does_not_send(detail):
    _, fields = detail
    assert DETAIL_ABSENT.isdisjoint(set(fields))


def test_session_detail_posture_status_is_present_but_empty(detail):
    _, fields = detail
    assert fields["posture_status"] == ""


def test_session_detail_verdict_is_a_lowercase_boolean(detail):
    _, fields = detail
    assert fields["passed"] in ("true", "false")
    assert fields["failed"] in ("true", "false")


def test_other_attr_string_shape_and_the_attributes_that_matter(detail):
    _, fields = detail
    parts = fields["other_attr_string"].split(":!:")
    # A leading delimiter, so the first part is empty.
    assert parts[0] == ""
    attributes = {}
    for part in parts[1:]:
        name, _, value = part.partition("=")
        attributes.setdefault(name, value)
    assert "AuthorizationPolicyMatchedRule" in attributes
    assert "ISEPolicySetName" in attributes
    # Both latency facts live here, not at top level.
    assert "TotalAuthenLatency" in attributes
    assert "StepLatency" in attributes


def test_response_time_matches_the_total_latency_attribute(detail):
    _, fields = detail
    attributes = dict(
        part.partition("=")[::2] for part in
        fields["other_attr_string"].split(":!:") if "=" in part)
    assert fields["response_time"] == attributes["TotalAuthenLatency"]


def test_step_latency_has_one_fewer_entry_than_execution_steps(detail):
    _, fields = detail
    attributes = dict(
        part.partition("=")[::2] for part in
        fields["other_attr_string"].split(":!:") if "=" in part)
    steps = [code for code in fields["execution_steps"].split(",") if code]
    latencies = [item for item in attributes["StepLatency"].split(";") if item]
    # The positional step->code mapping in session_authorization cannot be
    # right while these disagree.
    assert len(latencies) == len(steps) - 1


def test_a_mac_with_no_session_is_a_500_not_an_empty_document(session):
    response = session.get(
        f"https://{HOST}/admin/API/mnt/Session/MACAddress/00:00:00:00:00:01",
        headers={"Accept": "application/xml"}, timeout=TIMEOUT)
    assert response.status_code == 500
    assert b"mnt-rest-result" in response.content


# --- ERS -------------------------------------------------------------------

def test_ers_list_envelope_and_absolute_next_page(session):
    payload = _ers(session, f"/config/networkdevice?size={SMALL_PAGE}")
    result = payload["SearchResult"]
    assert isinstance(result["total"], int)
    assert result["resources"]
    href = result["nextPage"]["href"]
    assert href.startswith(f"https://{HOST}:{ERS_PORT}/ers/config/networkdevice")


def test_ers_network_device_rows_carry_no_inline_group_list(session):
    payload = _ers(session, f"/config/networkdevice?size={SMALL_PAGE}")
    for row in payload["SearchResult"]["resources"]:
        assert set(row) == {"id", "name", "description", "link"}


def test_network_device_detail_is_subnet_shaped(session):
    payload = _ers(session, "/config/networkdevice/name/campus-corp-wired")
    device = payload["NetworkDevice"]
    addresses = device["NetworkDeviceIPList"]
    assert addresses and all("ipaddress" in entry for entry in addresses)
    # The mask is real and is discarded by network_devices.device_addresses(),
    # which is why a session's NAS IP never equals the registered key.
    assert any(int(entry["mask"]) != 32 for entry in addresses)
    assert isinstance(device["NetworkDeviceGroupList"], list)


def test_internal_user_detail_has_no_password_info_object(session):
    listing = _ers(session, "/config/internaluser?size=1")
    resources = listing["SearchResult"]["resources"]
    if not resources:
        pytest.skip("no internal users on the appliance")
    user = _ers(
        session, f"/config/internaluser/{resources[0]['id']}")["InternalUser"]
    assert "passwordInfo" not in user
    # The hygiene signal is top level, and is a real JSON boolean.
    assert isinstance(user["passwordNeverExpires"], bool)
    assert isinstance(user["enabled"], bool)
    # Invented by the simulator; ISE does not send it.
    assert "identityGroups" not in user
    # No last-login field of any kind, so "never used" is unpublishable here.
    assert not [name for name in user if "login" in name.lower()]


# --- PAN OpenAPI -----------------------------------------------------------

@pytest.fixture(scope="module")
def nodes(session):
    payload = _openapi(session, "/deployment/node")
    assert payload["version"]
    return payload["response"]


def test_deployment_nodes_use_short_hostnames_with_a_separate_fqdn(nodes):
    for node in nodes:
        assert "." not in node["hostname"]
        assert node["fqdn"].startswith(node["hostname"] + ".")
        assert node["ipAddress"]
        assert isinstance(node["roles"], list)
        assert isinstance(node["services"], list)


def test_pan_ha_reports_a_real_boolean(session):
    payload = _openapi(session, "/deployment/pan-ha")["response"]
    assert isinstance(payload["isEnabled"], bool)


def test_patch_is_a_bare_object_listing_only_the_highest_patch(session):
    payload = _openapi(session, "/patch")
    assert "response" not in payload
    assert payload["iseVersion"]
    assert len(payload["patchVersion"]) == 1
    assert isinstance(payload["patchVersion"][0]["patchNumber"], int)


def test_license_tier_state_is_a_bare_list(session):
    payload = _openapi(session, "/license/system/tier-state")
    assert isinstance(payload, list) and payload
    for tier in payload:
        assert {"name", "status", "compliance", "consumptionCounter"} <= set(tier)
        assert isinstance(tier["consumptionCounter"], int)


def test_last_backup_status_reports_null_fields_rather_than_omitting_them(session):
    payload = _openapi(
        session, "/backup-restore/config/last-backup-status")["response"]
    assert "status" in payload and "startDate" in payload


def test_system_certificates_page_by_short_hostname(session, nodes):
    hostname = nodes[0]["hostname"]
    payload = _openapi(
        session, f"/certs/system-certificate/{hostname}?size={SMALL_PAGE}")
    assert payload["version"]
    # OpenAPI keeps the key and nulls it; ERS omits it. Both reduce to None.
    assert "nextPage" in payload
    row = payload["response"][0]
    assert {"friendlyName", "expirationDate", "usedBy", "keySize",
            "selfSigned"} <= set(row)
    assert isinstance(row["keySize"], int)
    assert isinstance(row["selfSigned"], bool)


def test_trusted_certificates_are_shaped_differently_from_system_ones(session):
    payload = _openapi(session, f"/certs/trusted-certificate?size={SMALL_PAGE}")
    row = payload["response"][0]
    assert "trustedFor" in row
    # The trusted store has neither, so the selfSigned check is a no-op there
    # and keySize is a string that int() survives by chance.
    assert "selfSigned" not in row
    assert "usedBy" not in row
    assert isinstance(row["keySize"], str)


def test_device_admin_policy_sets_use_the_response_envelope(session):
    payload = _openapi(session, "/policy/device-admin/policy-set")
    assert payload["version"]
    assert payload["response"]


def test_device_admin_rules_nest_their_identity_under_rule(session):
    sets = _openapi(session, "/policy/device-admin/policy-set")["response"]
    rules = _openapi(
        session,
        f"/policy/device-admin/policy-set/{sets[0]['id']}/authentication",
    )["response"]
    assert rules and "rule" in rules[0]
    assert {"id", "name", "rank"} <= set(rules[0]["rule"])


def test_command_sets_and_shell_profiles_are_bare_lists_that_overlap(session):
    command_sets = _openapi(session, "/policy/device-admin/command-sets")
    profiles = _openapi(session, "/policy/device-admin/shell-profiles")
    assert isinstance(command_sets, list)
    assert isinstance(profiles, list)
    # ISE mirrors command sets into the shell-profile list, so counting len()
    # of the profiles over-reports by the size of the overlap. This is why
    # tacacs_config counts neither collection from here.
    assert {entry["id"] for entry in command_sets} & {
        entry["id"] for entry in profiles}


def test_ers_owns_the_device_admin_result_objects_the_openapi_conflates(session):
    """ERS is where the two collections are counted, and why.

    ``ciscoisesdk``'s 3.3 generation puts the Device Admin profile list at
    ``/policy/device-admin/profiles``; ``/shell-profiles`` is the 3.1-era path,
    reinstated in 3.5. Whichever of the two this release routes, the OpenAPI
    surface returns a bare list with no total and -- as the test above records
    -- mixes command sets into the profiles. The ERS collections stay disjoint
    and carry an exact ``SearchResult.total``, so that is what the dataset
    reads. This pins the relationship so a release that changes it is caught.
    """
    command_sets = _ers(session, "/config/tacacscommandsets?size=100")
    profiles = _ers(session, "/config/tacacsprofile?size=100")
    ers_command_sets = command_sets["SearchResult"]
    ers_profiles = profiles["SearchResult"]
    assert isinstance(ers_command_sets["total"], int)
    assert isinstance(ers_profiles["total"], int)

    # The collections ERS reports are disjoint: no id is in both.
    command_set_ids = {row["id"] for row in ers_command_sets["resources"]}
    profile_ids = {row["id"] for row in ers_profiles["resources"]}
    assert not command_set_ids & profile_ids

    # And the OpenAPI shell-profile list is the union, which is exactly what
    # made counting it require a guess.
    openapi_profiles = {
        entry["id"] for entry in
        _openapi(session, "/policy/device-admin/shell-profiles")}
    assert openapi_profiles == profile_ids | (openapi_profiles & command_set_ids)


def test_which_device_admin_profile_route_this_release_serves(session):
    """Record whether 3.3 P11 answers ``/profiles``, ``/shell-profiles`` or both.

    Nothing depends on the answer any more -- that is the point of reading ERS
    instead -- but the SDK disagrees with itself across generations here, so
    the appliance's actual answer is worth having written down.
    """
    served = {}
    for path in ("/policy/device-admin/profiles",
                 "/policy/device-admin/shell-profiles"):
        try:
            response = session.get(
                f"https://{HOST}/api/v1{path}",
                headers={"Accept": "application/json"}, timeout=TIMEOUT)
        except requests.RequestException as error:
            pytest.skip(f"the lab appliance is not answering: {type(error).__name__}")
        served[path] = response.status_code
    # At least one of them has to answer, or the OpenAPI surface has moved
    # again and this note is out of date.
    assert 200 in served.values(), served


# --- Data Connect ----------------------------------------------------------

@pytest.fixture(scope="module")
def oracle(reachable):
    oracledb = pytest.importorskip("oracledb")
    password = _secret("lab_ise_dataconnect_pw")
    try:
        connection = oracledb.connect(
            user=ORACLE_USER, password=password, host=HOST, port=ORACLE_PORT,
            service_name=ORACLE_SERVICE, protocol="tcps",
            ssl_context=ssl._create_unverified_context(),
            ssl_server_dn_match=False, tcp_connect_timeout=TIMEOUT)
    except Exception as error:      # noqa: BLE001 - any failure means "skip"
        pytest.skip(f"Data Connect is not answering: {type(error).__name__}")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture(scope="module")
def catalog(oracle):
    """The whole visible column catalogue, read once (one dictionary read)."""
    cursor = oracle.cursor()
    cursor.execute(
        "SELECT table_name, column_name, data_type FROM user_tab_columns")
    columns = {}
    types = {}
    for table, column, data_type in cursor:
        columns.setdefault(table, set()).add(column)
        types[(table, column)] = data_type
    return columns, types


REQUIRED_VIEWS = (
    "RADIUS_AUTHENTICATION_SUMMARY", "RADIUS_AUTHENTICATIONS",
    "RADIUS_ACCOUNTING", "RADIUS_ERRORS_VIEW", "KEY_PERFORMANCE_METRICS",
    "SYSTEM_SUMMARY", "AAA_DIAGNOSTICS_VIEW", "SYSTEM_DIAGNOSTICS_VIEW",
    "ENDPOINTS_DATA", "POSTURE_ASSESSMENT_BY_ENDPOINT",
    "POSTURE_ASSESSMENT_BY_CONDITION", "PROFILED_ENDPOINTS_SUMMARY",
    "TACACS_AUTHENTICATION_LAST_TWO_DAYS",
    "TACACS_AUTHORIZATION_LAST_TWO_DAYS", "TACACS_ACCOUNTING_LAST_TWO_DAYS",
)


def test_every_view_the_datasets_read_exists(oracle):
    cursor = oracle.cursor()
    cursor.execute("SELECT view_name FROM user_views")
    views = {row[0] for row in cursor}
    assert set(REQUIRED_VIEWS) <= views


def test_every_contracted_column_exists(catalog):
    columns, _ = catalog
    missing = []
    for view, contract in SCHEMA_COLUMN_CONTRACTS.items():
        have = columns.get(view, set())
        for column in tuple(contract["required"]) + tuple(contract["optional"]):
            if column not in have:
                missing.append(f"{view}.{column}")
    assert not missing


def test_radius_errors_hard_coded_columns_exist_with_the_right_types(catalog):
    columns, types = catalog
    assert {"MESSAGE_CODE", "NETWORK_DEVICE_NAME", "AUTHENTICATION_METHOD",
            "ISE_NODE", "TIMESTAMP"} <= columns["RADIUS_ERRORS_VIEW"]
    # NUMBER, so the TO_CHAR-first defence is load bearing.
    assert types[("RADIUS_ERRORS_VIEW", "MESSAGE_CODE")] == "NUMBER"
    # VARCHAR2 here, so the unconditional TO_CHAR has to tolerate both.
    assert types[("AAA_DIAGNOSTICS_VIEW", "MESSAGE_CODE")] == "VARCHAR2"


def test_key_performance_metrics_times_on_logged_time_only(catalog):
    columns, _ = catalog
    assert "LOGGED_TIME" in columns["KEY_PERFORMANCE_METRICS"]
    # A statement naming TIMESTAMP here is an ORA-00904, not a lost dimension.
    assert "TIMESTAMP" not in columns["KEY_PERFORMANCE_METRICS"]


def test_endpoints_data_keys_and_time_columns(catalog):
    columns, types = catalog
    endpoints = columns["ENDPOINTS_DATA"]
    assert {"MAC_ADDRESS", "ENDPOINT_POLICY", "IDENTITY_GROUP_ID",
            "POSTURE_APPLICABLE", "UPDATE_TIME"} <= endpoints
    assert "IDENTITY_GROUP" not in endpoints
    assert "TIMESTAMP" not in endpoints
    assert "ISE_NODE" not in endpoints
    assert types[("ENDPOINTS_DATA", "POSTURE_APPLICABLE")] == "NUMBER"
    # Selecting this raw fails under the thin driver; it must be CAST.
    assert types[("ENDPOINTS_DATA", "UPDATE_TIME")].startswith("TIMESTAMP")


def test_the_two_posture_views_disagree_about_their_own_column_names(catalog):
    columns, _ = catalog
    endpoint = columns["POSTURE_ASSESSMENT_BY_ENDPOINT"]
    condition = columns["POSTURE_ASSESSMENT_BY_CONDITION"]
    assert {"TIMESTAMP", "ENDPOINT_MAC_ADDRESS", "ENDPOINT_OPERATING_SYSTEM",
            "POSTURE_AGENT_VERSION", "POSTURE_STATUS", "POSTURE_POLICY_MATCHED",
            "ISE_NODE"} <= endpoint
    assert {"LOGGED_AT", "ENDPOINT_ID", "ENDPOINT_OS", "CONDITION_NAME",
            "CONDITION_STATUS"} <= condition
    assert "TIMESTAMP" not in condition
    assert "ENDPOINT_OPERATING_SYSTEM" not in condition


def test_profiled_endpoints_summary_has_no_node_column(catalog):
    columns, _ = catalog
    profiled = columns["PROFILED_ENDPOINTS_SUMMARY"]
    assert {"TIMESTAMP", "SOURCE", "ENDPOINT_ACTION_NAME",
            "ENDPOINT_PROFILE"} <= profiled
    assert "ISE_NODE" not in profiled


def test_tacacs_two_day_views_time_on_epoch_time(catalog):
    columns, types = catalog
    for view in ("TACACS_AUTHENTICATION_LAST_TWO_DAYS",
                 "TACACS_AUTHORIZATION_LAST_TWO_DAYS",
                 "TACACS_ACCOUNTING_LAST_TWO_DAYS"):
        assert "EPOCH_TIME" in columns[view]
        assert types[(view, "EPOCH_TIME")] == "NUMBER"
        assert "TIMESTAMP" not in columns[view]
    authorization = columns["TACACS_AUTHORIZATION_LAST_TWO_DAYS"]
    assert {"SHELL_PROFILE", "MATCHED_COMMAND_SET",
            "AUTHORIZATION_POLICY"} <= authorization
    # Singular here, plural on the other two.
    assert "DEVICE_GROUP" in authorization


def test_radius_summary_lacks_the_detail_only_columns(catalog):
    columns, types = catalog
    summary = columns["RADIUS_AUTHENTICATION_SUMMARY"]
    assert {"PASSED_COUNT", "FAILED_COUNT", "TOTAL_RESPONSE_TIME",
            "SECURITY_GROUP", "FAILURE_REASON"} <= summary
    # Absent here, which is why radius_reporting needs the detail view too.
    assert {"AUTHENTICATION_METHOD", "AUTHENTICATION_PROTOCOL",
            "AUTHORIZATION_RULE", "RESPONSE_TIME"}.isdisjoint(summary)
    # PASSED is text while FAILED is numeric, so NVL(failed,0)=0 is the only
    # arithmetic that works on the detail view.
    assert types[("RADIUS_AUTHENTICATIONS", "PASSED")] == "VARCHAR2"
    assert types[("RADIUS_AUTHENTICATIONS", "FAILED")] == "NUMBER"


def test_accounting_status_type_uses_mixed_case_start_and_stop(oracle):
    cursor = oracle.cursor()
    cursor.execute(
        "SELECT DISTINCT acct_status_type FROM radius_accounting "
        "FETCH FIRST 10 ROWS ONLY")
    values = {row[0] for row in cursor if row[0]}
    if not values:
        pytest.skip("no accounting rows on the appliance")
    # The dataset upper-cases before matching, which is what makes this safe.
    assert all("START" in value.upper() or "STOP" in value.upper()
               for value in values)


def test_a_timestamp_with_time_zone_column_needs_a_cast(oracle):
    """source_freshness only survives ENDPOINTS_DATA because it CASTs."""
    cursor = oracle.cursor()
    cursor.execute(
        "SELECT MAX(CAST(update_time AS DATE)) FROM endpoints_data")
    assert cursor.fetchone() is not None


def test_probe_data_is_ise_framed_and_the_column_truncates_it(oracle):
    """PROBE_DATA's framing, and the fact that the column cannot hold it.

    The framing is undocumented, so this is the check that it has not moved:
    a varint pair count, then 0x11-tagged varint-length UTF-8 records that
    alternate name and value. Read off this appliance and implemented from it.

    The second half matters more than the first. ENDPOINTS_DATA projects this
    column as utl_raw.cast_to_varchar2(dbms_lob.substr(EDF_KRYOBUFFER, 2000)):
    the profiling buffer is a LOB holding the whole attribute set, and the view
    exposes its first 2000 bytes. The header still declares the full count,
    which is the only reason a reader can tell a complete attribute set from a
    cut-off one.
    """
    cursor = oracle.cursor()

    def handler(inner, metadata):
        if metadata.name == "PROBE_DATA":
            return inner.var(metadata.type_code, size=32767,
                             arraysize=inner.arraysize, bypass_decode=True)
        return None

    cursor.outputtypehandler = handler
    cursor.execute(
        "SELECT probe_data FROM endpoints_data "
        "WHERE probe_data IS NOT NULL FETCH FIRST 5 ROWS ONLY")
    samples = [row[0] for row in cursor if row[0]]
    if not samples:
        pytest.skip("no endpoint on this appliance carries probe data")

    # bypass_decode is what makes this readable at all: decoded as text the
    # non-UTF-8 bytes become U+FFFD and the framing is gone with them.
    assert all(isinstance(sample, bytes) for sample in samples)

    decoded = [probe_data.decode(sample) for sample in samples]
    assert all(field["encoding"] == "ise-tlv" for field in decoded)
    assert all(field["attributes"] for field in decoded)
    # Names ISE puts on every endpoint, whatever else is profiled.
    assert any("OUI" in field["attributes"] for field in decoded)

    # A complete field agrees with its own header; a truncated one says so
    # rather than passing off a prefix as the whole attribute set.
    for field in decoded:
        if field["truncated"]:
            assert field["count"] < field["declared"]
            assert "not reachable through Data Connect" in field["note"]
        else:
            assert field["count"] == field["declared"]


def test_the_session_projection_reads_fields_this_appliance_really_sends(tmp_path):
    """Every projected session field resolves against a live record.

    The projection was written from the field names that seemed likely, and
    against a real session two of them were wrong: the device name lives in
    nasIdentifier (networkDeviceProfileName is the device *profile*), and the
    method is authMethod rather than the session state standing in for it. A
    wrong name does not fail -- it returns an empty column on every row, which
    is the kind of quiet wrong this suite exists to catch.

    Needs at least one active session. The lab usually has none, so this skips
    rather than failing; tools/lab_sessions3.py makes some.
    """
    pytest.importorskip("websocket")
    transport = _pxgrid_transport(tmp_path)
    if transport is None:
        pytest.skip("no pxGrid target is configured for this lab")
    try:
        data = transport._query(
            "com.cisco.ise.session", "getSessions", {},
            api="pxgrid_get_sessions",
            max_bytes=transport.config.limits.pxgrid_session_bytes)
    except Exception as error:                   # noqa: BLE001 - skip, not fail
        pytest.skip(f"pxGrid is not answering: {type(error).__name__}")
    finally:
        transport.close()
    records = (data.get("sessions") if isinstance(data, dict) else data) or []
    if not records:
        pytest.skip("no active sessions on the appliance right now")

    projected = [project_session(record) for record in records]
    projected = [row for row in projected if row]
    assert projected, "every session was dropped for want of a MAC"

    # The fields ISE fills on any authenticated session. Anything here coming
    # back empty means a field name has moved.
    for row in projected:
        for field in ("mac_address", "user_name", "nad", "nas_ip_address",
                      "session_state", "last_update", "auth_method"):
            assert row[field], f"{field} is empty; ISE renamed the field it reads"


def _pxgrid_transport(state_directory):
    """The lab's configured pxGrid transport, or None.

    REST only: prepare() activates and discovers, and the STOMP supervisor is
    started elsewhere, so this opens no second subscription against a client
    name the running exporter may already hold.

    The auth guard is deliberately shared state, and a test run is not the
    exporter, so it is pointed at a scratch directory. A probe must not be able
    to move the lockout counter the running service depends on.
    """
    import tomllib

    path = os.environ.get("ISE_LAB_EXPORTER_CONFIG")
    if not path or not os.path.isfile(path):
        return None
    os.environ.setdefault("ISE_PXGRID_PASSWORD", _secret("lab_ise_pxgrid_pw"))
    os.environ.setdefault("ISE_PASS", _secret("lab_ise_ui_admin_pw"))
    os.environ.setdefault(
        "ISE_DATACONNECT_PASSWORD", _secret("lab_ise_dataconnect_pw"))
    with open(path, "rb") as handle:
        document = tomllib.load(handle)
    document.setdefault("exporter", {})["state_db"] = str(
        state_directory / "state.sqlite3")
    try:
        config = Config.from_document(document, path=path, environ=os.environ)
        transport = build_transports(config, kinds={"pxgrid"}).get("pxgrid")
    except Exception:                            # noqa: BLE001 - skip, not fail
        return None
    if transport is None:
        return None
    transport.prepare()
    return transport


def test_mnt_session_detail_is_the_richest_per_endpoint_source(session):
    """DATASETS_FACTS §6.5: 42 fields, and the two shapes that read as breakage.

    MnT carries the accounting counters and correlation ids nothing else does,
    so a shrinking field set here is a real capability loss. The two error
    shapes are asserted because both look like faults and are not: a MAC with
    no session is HTTP 500 with cpm-code 34110, and the record count must be an
    integer rather than the word ``all``.

    Needs an active session; ``tools/lab_sessions3.py start`` makes some.
    """
    root = ET.fromstring(_mnt(session, "/Session/ActiveList").content)
    macs = [child.text for node in root.iter() if node.tag == "activeSession"
            for child in node if child.tag == "calling_station_id" and child.text]
    if not macs:
        pytest.skip("no active sessions on the appliance right now")

    detail = ET.fromstring(_mnt(session, f"/Session/MACAddress/{macs[0]}").content)
    fields = {child.tag: child.text for node in detail.iter() for child in node
              if len(child) == 0 and child.text and child.text.strip()}
    fields.update({child.tag: child.text for child in detail
                   if len(child) == 0 and child.text and child.text.strip()})
    assert len(fields) >= 20, "MnT session detail lost most of its fields"
    # The four groups §6.5 says nothing else carries.
    for name in ("acct_session_id", "audit_session_id", "selected_azn_profiles",
                 "identity_store"):
        assert name in fields, f"MnT stopped sending {name}"

    # A MAC with no session: an ordinary state, reported as a server error.
    absent = session.get(
        f"https://{HOST}/admin/API/mnt/Session/MACAddress/AA:BB:CC:DD:EE:FF",
        headers={"Accept": "application/xml"}, timeout=TIMEOUT)
    assert absent.status_code == 500
    assert "<cpm-code>34110</cpm-code>" in absent.text

    # The record count is an integer. 'all' is a 400, which is easy to read as
    # "the API is broken" rather than "that argument is wrong".
    good = session.get(
        f"https://{HOST}/admin/API/mnt/AuthStatus/MACAddress/{macs[0]}/86400/100/All",
        headers={"Accept": "application/xml"}, timeout=TIMEOUT)
    assert good.status_code == 200
    bad = session.get(
        f"https://{HOST}/admin/API/mnt/AuthStatus/MACAddress/{macs[0]}/86400/all/all",
        headers={"Accept": "application/xml"}, timeout=TIMEOUT)
    assert bad.status_code == 400

    # Documented as absent on 3.3 so nobody designs a dataset around it.
    missing = session.get(
        f"https://{HOST}/admin/API/mnt/AccountStatus/MACAddress/{macs[0]}/86400",
        headers={"Accept": "application/xml"}, timeout=TIMEOUT)
    assert missing.status_code == 404
