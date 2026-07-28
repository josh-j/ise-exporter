"""The simulator must not answer a question the appliance refuses.

Every assertion here is a payload shape captured from ISE 3.3.0.430 Patch 11 and
written up in `docs/DATASETS_FACTS.md`. They exist because a field the simulator
invents is a design that passes in simulation and collects nothing in
production -- which is exactly how the MnT ActiveList, the per-MAC detail, the
patch list and four Data Connect dimensions all shipped wrong.
"""
import xml.etree.ElementTree as ET

import pytest

from ise_exporter3.session_detail import parse_attributes, project
from tools.fake_ise3 import (
    LAB_EMPTY_VIEWS,
    Estate,
    VirtualClock,
    synthesize,
)
from tools.simulate_scale3 import declared_views


# The 43 elements /Session/MACAddress/<mac> really returns.
REAL_DETAIL_ELEMENTS = frozenset("""
    acct_acs_timestamp acct_acsview_timestamp acct_authentic acct_id
    acct_input_octets acct_input_packets acct_output_octets acct_output_packets
    acct_session_id acct_status_type acs_server audit_session_id
    auth_acs_timestamp auth_acsview_timestamp auth_id authentication_method
    authentication_protocol calling_station_id cpmsession_id
    destination_ip_address device_ip_address device_type endpoint_policy
    execution_steps failed framed_ip_address identity_store location
    message_code nas_ip_address nas_port_id network_device_name
    orig_calling_station_id other_attr_string passed posture_status response
    response_time selected_azn_profiles service_type started stopped user_name
""".split())

# Elements the simulator used to emit and ISE does not have under any spelling.
INVENTED_DETAIL_ELEMENTS = (
    "server", "session_state", "identity_group", "failure_reason",
    "posture_report", "posture_agent_version", "operating_system",
    "step_latency", "total_authentication_latency",
)


@pytest.fixture
def estate():
    return Estate(nads=20, endpoints=200, sessions=10, accounts=5,
                  clock=VirtualClock())


def _detail(estate, index=0):
    mac = estate.session_fields(index)["mac"]
    status, body = estate.session_detail_response(mac)
    assert status == 200
    return ET.fromstring(body)


def test_active_list_root_and_eighth_child(estate):
    document = ET.fromstring(estate.active_list_xml())
    assert document.tag == "activeList"
    assert document.get("noOfActiveSession") == "10"
    children = [child.tag for child in list(document)[0]]
    assert children == [
        "user_name", "calling_station_id", "nas_ip_address", "acct_session_id",
        "audit_session_id", "server", "framed_ip_address",
        "framed_ipv6_address",
    ]
    # Eight children, seven usable: the IPv6 one is empty, so a transport that
    # drops empty text is left with seven keys.
    assert list(document)[0].find("framed_ipv6_address").text in (None, "")


def test_active_list_names_no_network_device(estate):
    text = estate.active_list_xml().decode("utf-8")
    for absent in ("network_device_name", "identity_group", "posture_status",
                   "session_state", "location"):
        assert f"<{absent}>" not in text


def test_session_detail_is_the_real_element_set(estate):
    tags = [child.tag for child in _detail(estate)]
    assert len(tags) == 43
    assert set(tags) == REAL_DETAIL_ELEMENTS


def test_session_detail_invents_nothing(estate):
    tags = {child.tag for child in _detail(estate)}
    assert tags.isdisjoint(INVENTED_DETAIL_ELEMENTS)


def test_posture_status_is_present_and_empty(estate):
    element = _detail(estate).find("posture_status")
    assert element is not None
    assert element.text in (None, "")


def test_execution_steps_outnumber_step_latencies(estate):
    document = _detail(estate)
    codes = document.find("execution_steps").text.split(",")
    latencies = parse_attributes(
        document.find("other_attr_string").text)["StepLatency"].split(";")
    # 28 against 27, and which end is dropped is not observable -- which is why
    # a step latency cannot be labelled with a message code.
    assert len(codes) == 28
    assert len(latencies) == 27


def test_other_attr_string_shape(estate):
    raw = _detail(estate).find("other_attr_string").text
    assert raw.startswith(":!:")
    assert raw.split(":!:")[0] == ""
    attributes = parse_attributes(raw)
    assert len(attributes) == 42
    # Keys with spaces, and values carrying '=' and ';'.
    assert attributes["Ops Owner"].startswith("Ops Owner#All Ops Owners#")
    assert attributes["Network Device Profile"] == "Cisco"
    assert attributes["AD-User-Resolved-DNs"].count("=") >= 3
    assert ";" in attributes["StepLatency"]
    # Both latencies live here and nowhere else.
    assert attributes["TotalAuthenLatency"].isdigit()
    assert "ClientLatency" in attributes


def test_projection_reads_the_latencies_the_document_carries(estate):
    document = _detail(estate)
    record = {child.tag: child.text for child in document if child.text}
    resolved = project(record)
    assert resolved["total_authentication_latency"] == record["response_time"]
    assert resolved["step_latency"].startswith("1=")
    assert resolved["policy_set"] and resolved["authz_rule"]


def test_departed_mac_is_a_500_not_an_empty_document(estate):
    status, body = estate.session_detail_response("00:00:00:00:00:01")
    assert status == 500
    document = ET.fromstring(body)
    assert document.tag == "mnt-rest-result"
    assert document.find("cpm-code").text == "34110"
    assert b"sessionParameters" not in body


def test_nads_are_subnet_defined_and_sessions_arrive_from_inside(estate):
    subnet = next(device for device in estate.nads if device["mask"] == 24)
    assert subnet["ip"].endswith(".0")
    assert subnet["nas_ip"] != subnet["ip"]
    # The configured address is the network address, so a session's NAS IP can
    # only be joined to it by containment.
    assert subnet["nas_ip"].rsplit(".", 1)[0] == subnet["ip"].rsplit(".", 1)[0]


def test_rooted_nads_carry_two_segment_groups(estate):
    rooted = next(device for device in estate.nads if device["rooted"])
    groups = estate.device_groups(rooted)
    assert "Location#All Locations" in groups
    assert "Device Type#All Device Types" in groups
    # Ops owner is the one that still resolves on a rooted NAD.
    assert any(group.count("#") == 2 and group.startswith("Ops Owner")
               for group in groups)


def test_nodes_are_short_hostnames_with_fqdn_beside_them(estate):
    node = estate.nodes[0]
    assert "." not in node["hostname"]
    assert node["fqdn"].startswith(node["hostname"] + ".")
    assert node["ipAddress"]
    assert all("." not in psn for psn in estate.psns)


def test_empty_view_aggregate_returns_one_row_of_zeros_and_nulls(estate):
    sql = ("SELECT 'radius_errors_view' AS view_name, COUNT(*) AS total_rows, "
           "NVL(MAX(timestamp), -1) AS age_seconds FROM RADIUS_ERRORS_VIEW")
    columns, rows = synthesize(sql, estate, empty_views=LAB_EMPTY_VIEWS)
    assert columns == ["view_name", "total_rows", "age_seconds"]
    assert rows == [("radius_errors_view", 0, None)]
    # The same statement against a populated view is not empty.
    assert synthesize(sql, estate)[1][0][1] > 0


def test_empty_view_group_by_returns_no_rows(estate):
    sql = ("SELECT NVL(ise_node, 'unknown') AS psn, COUNT(*) AS events "
           "FROM RADIUS_ERRORS_VIEW GROUP BY ise_node")
    assert synthesize(sql, estate, empty_views=LAB_EMPTY_VIEWS)[1] == []


def test_empty_view_marginals_return_no_rows(estate):
    sql = ("SELECT CASE WHEN GROUPING(ise_node) = 0 THEN 'psn' END AS dimension, "
           "COUNT(*) AS events FROM RADIUS_ERRORS_VIEW "
           "GROUP BY GROUPING SETS ((ise_node))")
    assert synthesize(sql, estate, empty_views=LAB_EMPTY_VIEWS)[1] == []


def test_union_decides_each_branch_on_its_own_view(estate):
    sql = ("SELECT 'a' AS view_name, COUNT(*) AS total_rows FROM SYSTEM_SUMMARY "
           "UNION ALL "
           "SELECT 'b' AS view_name, COUNT(*) AS total_rows FROM RADIUS_ERRORS_VIEW")
    _columns, rows = synthesize(sql, estate, empty_views=LAB_EMPTY_VIEWS)
    assert [row[1] for row in rows] == [rows[0][1], 0]
    assert rows[0][1] > 0


def test_always_null_dimensions_yield_one_placeholder_series(estate):
    sql = ("SELECT CASE WHEN GROUPING(identity_group_id) = 0 THEN 'identity_group' "
           "END AS dimension, COUNT(*) AS endpoints FROM ENDPOINTS_DATA "
           "GROUP BY GROUPING SETS ((identity_group_id))")
    _columns, rows = synthesize(sql, estate)
    assert [row[0] for row in rows] == ["identity_group"]

    # The same dimension name is live on another view and must stay live.
    live = ("SELECT CASE WHEN GROUPING(authorization_rule) = 0 THEN 'policy' END "
            "AS dimension, COUNT(*) AS events FROM RADIUS_AUTHENTICATIONS "
            "GROUP BY GROUPING SETS ((authorization_rule))")
    assert len(synthesize(live, estate)[1]) > 1


def test_declared_catalogue_invents_no_time_or_node_column():
    views = declared_views()
    assert "TIMESTAMP" not in views["KEY_PERFORMANCE_METRICS"]
    assert "LOGGED_TIME" in views["KEY_PERFORMANCE_METRICS"]
    assert {"TIMESTAMP", "ISE_NODE"}.isdisjoint(views["ENDPOINTS_DATA"])
    assert "UPDATE_TIME" in views["ENDPOINTS_DATA"]
    assert "TIMESTAMP" not in views["POSTURE_ASSESSMENT_BY_CONDITION"]
    assert "LOGGED_AT" in views["POSTURE_ASSESSMENT_BY_CONDITION"]
    assert "ISE_NODE" not in views["PROFILED_ENDPOINTS_SUMMARY"]
    for view in ("TACACS_AUTHENTICATION_LAST_TWO_DAYS",
                 "TACACS_ACCOUNTING_LAST_TWO_DAYS",
                 "TACACS_AUTHORIZATION_LAST_TWO_DAYS"):
        assert "TIMESTAMP" not in views[view]
        assert "EPOCH_TIME" in views[view]


def test_declared_catalogue_covers_the_hard_coded_error_columns():
    # radius_errors has no degradation path, so the catalogue has to carry the
    # four columns it names or the simulation says nothing about them.
    columns = declared_views()["RADIUS_ERRORS_VIEW"]
    assert {"MESSAGE_CODE", "NETWORK_DEVICE_NAME", "AUTHENTICATION_METHOD",
            "ISE_NODE"} <= columns
