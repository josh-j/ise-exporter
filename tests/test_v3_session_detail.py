"""What one cached MnT session record keeps, and what it must not lose.

The cache holds one record per active MAC -- 20,000 at the declared scale -- and
it used to hold the whole MnT document. These tests pin the projection that
replaced it: that it keeps everything both readers actually use, that it resolves
ISE's spelling variants once rather than on every read, and above all that it
preserves the one distinction a naive field copy would quietly destroy.
"""
import pytest

from ise_exporter3.session_detail import parse_attributes, project


FULL = {
    "calling_station_id": "00:11:22:33:44:55",
    "network_device_name": "sw-01",
    "nas_ip_address": "10.1.1.1",
    "location": "All Locations#Germany#Ramstein AB",
    "passed": "true",
    "authentication_method": "dot1x",
    "selected_azn_profiles": "PermitAccess,Quarantine",
    "other_attr_string":
        ":!:ISEPolicySetName=Wired Open Mode:!:"
        "AuthorizationPolicyMatchedRule=Basic_Authenticated_Access:!:"
        "StepLatency=1=5;2=12:!:TotalAuthenLatency=24:!:",
    "posture_status": "Compliant",
    "message_code": "5200",
    "response_time": "24",
    # Everything below is real MnT payload that nothing has ever read.
    "acct_session_id": "0A0101010000001F",
    "audit_session_id": "0A01010100000020",
    "nas_port_id": "GigabitEthernet1/0/12",
    "nas_port_type": "Ethernet",
    "framed_ip_address": "10.20.30.40",
    "service_type": "Framed",
    "identity_group": "Workstations",
    "server": "psn1",
    "acct_session_time": "3600",
}


def test_the_projection_keeps_what_its_readers_use():
    projected = project(FULL)
    assert projected["nad"] == "sw-01"
    assert projected["nas_ip"] == "10.1.1.1"
    assert projected["passed"] is True
    assert projected["method"] == "dot1x"
    assert projected["profiles"] == "PermitAccess,Quarantine"
    assert projected["policy_set"] == "Wired Open Mode"
    assert projected["authz_rule"] == "Basic_Authenticated_Access"
    assert projected["posture_status"] == "Compliant"
    assert projected["message_code"] == "5200"
    # Both latencies are attributes of other_attr_string; ISE has no element for
    # either, so reading them as top-level fields collected nothing.
    assert projected["step_latency"] == "1=5;2=12"
    assert projected["total_authentication_latency"] == "24"


def test_the_projection_drops_what_nothing_reads():
    projected = project(FULL)
    for field in ("acct_session_id", "audit_session_id", "nas_port_id",
                  "nas_port_type", "framed_ip_address", "service_type",
                  "identity_group", "server", "acct_session_time",
                  "other_attr_string"):
        assert field not in projected, f"{field} is retained but never read"
    # 20,000 of these are held at once, so the shape is the memory.
    assert len(projected) < len(FULL)


def test_posture_fields_stay_projected_for_estates_that_run_posture():
    # They are empty on a lab with no Secure Client and populated where posture
    # runs, so the projection keeps reading them. Dropping a lookup because one
    # estate never exercises it removes a working production metric -- the test
    # is whether ISE can emit the field, not whether it did here.
    projected = project(FULL)
    for field in ("posture_report", "agent_version", "operating_system"):
        assert field in projected
    populated = project({
        "posture_report": "AV_Installed:Passed",
        "PostureAgentVersion": "5.1.2.42",
        "os_type": "Windows 11",
    })
    assert populated["posture_report"] == "AV_Installed:Passed"
    assert populated["agent_version"] == "5.1.2.42"
    assert populated["operating_system"] == "Windows 11"


def test_execution_steps_is_not_offered_to_readers():
    # Genuinely dropped: no position in StepLatency maps to a message code, so
    # the codes are no longer projected for a mapping that cannot be made.
    assert "execution_steps" not in project(FULL)


def test_total_latency_falls_back_to_response_time():
    # response_time is a real element and carried exactly the TotalAuthenLatency
    # value on every sampled session; the attribute still wins where both exist.
    assert project({"response_time": "42"})["total_authentication_latency"] == "42"
    assert project({
        "response_time": "42",
        "other_attr_string": ":!:TotalAuthenLatency=25:!:",
    })["total_authentication_latency"] == "25"


def test_the_awkward_shapes_of_a_real_other_attr_string_parse():
    # Leading delimiter, keys with spaces, and values that contain their own
    # '=' and ';' -- StepLatency is the whole run of steps in one value.
    parsed = parse_attributes(
        ":!:Ops Owner=Ops Owner#All Ops Owners#AD Lab:!:StepLatency=1=0;2=3"
        ":!:AD-User-Resolved-DNs=CN=user3,OU=Lab:!:TotalAuthenLatency=25:!:")
    assert parsed["Ops Owner"] == "Ops Owner#All Ops Owners#AD Lab"
    assert parsed["StepLatency"] == "1=0;2=3"
    assert parsed["AD-User-Resolved-DNs"] == "CN=user3,OU=Lab"
    assert parsed["TotalAuthenLatency"] == "25"


def test_location_is_not_retained_per_session():
    # network_devices publishes ise3_network_device_assignment{nad,location,
    # ops_owner}, so a dashboard joins for it. Keeping a copy on every session
    # was 20,000 copies of a fact that already had one home -- and it was parsed
    # and then discarded unread at the only place that asked for it.
    assert "location" not in project(FULL)


def test_an_accounting_only_record_is_distinguishable_from_a_failed_one():
    # The distinction a naive {k: record.get(k)} loses. A stop record carries
    # no verdict at all, and counting it as an authorization would dilute every
    # ratio built from this cache -- but `passed` missing and `passed` false
    # both read as falsey.
    accounting = project({"calling_station_id": "x", "acct_status_type": "Stop"})
    failed = project({"calling_station_id": "x", "failed": "true"})

    assert accounting["has_verdict"] is False
    assert accounting["passed"] is False and accounting["failed"] is False
    assert failed["has_verdict"] is True
    assert failed["failed"] is True


@pytest.mark.parametrize("field,value,key", [
    ("posture_status", "Compliant", "posture_status"),
    ("PostureStatus", "Compliant", "posture_status"),
    ("posture_assessment_status", "Compliant", "posture_status"),
])
def test_ise_spelling_variants_are_resolved_once_at_the_boundary(field, value, key):
    # They used to be tried on every read of every record on every cycle. Now a
    # new spelling is handled in one place and costs one comparison per session.
    assert project({field: value})[key] == value


def test_the_first_populated_variant_wins():
    projected = project({"posture_status": "", "PostureStatus": "Compliant"})
    assert projected["posture_status"] == "Compliant"


def test_a_missing_field_is_an_empty_string_not_a_missing_key():
    # Readers index the projection directly, so every declared key must exist.
    projected = project({})
    assert projected["nad"] == "" and projected["policy_set"] == ""
    assert projected["has_verdict"] is False


def test_other_attr_string_is_parsed_once_and_not_retained():
    projected = project(FULL)
    assert "other_attr_string" not in projected
    assert projected["policy_set"] and projected["authz_rule"]


def test_attribute_parsing_handles_the_ise_pair_format():
    parsed = parse_attributes(":!:A=1:!:B=two:!:broken:!:C=3:!:")
    assert parsed == {"A": "1", "B": "two", "C": "3"}


def test_a_record_that_is_not_a_record_does_not_raise():
    # One malformed MnT response must not fail a 20,000-session collection.
    assert project(None)["nad"] == ""


# --- PostureReport, held to a document the appliance actually wrote ----------

# The grammar is verbatim from a report a posture-enabled appliance wrote;
# the names are invented, because real policy names are site-defined. What is
# being pinned is the shape: backslash-semicolon is literal in the payload --
# `\;` is the delimiter ISE writes, not an escape for a literal semicolon --
# policies are comma-separated, and the parenthesised tail holds requirements
# whose condition lists carry colons of their own.
REAL_POSTURE_REPORT = (
    r"Corp-Inventory-Agent\;Passed\;(Req-Inventory-Registered:Audit:Skipped:"
    r"Passed_Conditions[]:Failed_Conditions[Cond-Inventory-Version]:"
    r"Skipped_Conditions[Cond-Inventory-Name:Cond-Inventory-InstallDate]), "
    r"Corp-Firewall-On\;Passed\;(Req-Firewall-Enabled:Optional:Passed:"
    r"Passed_Conditions[Cond-Firewall-Private-Enabled:"
    r"Cond-Firewall-Domain-Enabled]:Failed_Conditions[]:"
    r"Skipped_Conditions[]), "
    r"Corp-Disk-Encryption\;Passed\;(Req-Disk-Encrypted:Optional:Passed:"
    r"Passed_Conditions[Cond-Disk-Volume-Encrypted]:"
    r"Failed_Conditions[]:Skipped_Conditions[]), "
    r"Corp-AV-Required\;Passed\;(Req-AV-Installed:Optional:Passed:"
    r"Passed_Conditions[Cond-AV-Product-Present]:Failed_Conditions[]:"
    r"Skipped_Conditions[]\;Req-AV-Scan-Recent:Audit:Failed:Passed_Conditions[]:"
    r"Failed_Conditions[Cond-AV-Scan-Age]:Skipped_Conditions[])"
)


def test_the_policy_breakdown_is_one_series_per_policy_not_per_endpoint():
    r"""The old parser unescaped `\;` and split on `;`, finding one entry.

    `\;` is the delimiter, so unescaping first destroys the only structure
    there is: the whole 1.4 KB report became one `policy` label, split at its
    last colon. Every endpoint's report differs, so `label()` truncated and
    hashed each into its own series and the family grew with the fleet.
    """
    from ise_exporter3.datasets.posture_current import parse_posture_report

    pairs = list(parse_posture_report(REAL_POSTURE_REPORT))
    assert pairs == [
        ("Corp-Inventory-Agent", "Passed"),
        ("Corp-Firewall-On", "Passed"),
        ("Corp-Disk-Encryption", "Passed"),
        ("Corp-AV-Required", "Passed"),
    ]
    assert all(len(policy) < 40 for policy, _result in pairs)


def test_a_failed_audit_requirement_survives_a_policy_that_reads_passed():
    """Corp-AV-Required rolls up to Passed while Req-AV-Scan-Recent failed.

    An Audit requirement does not change the verdict, so this failure exists
    only at the requirement level -- and mandate is what separates it from one
    that would actually deny access.
    """
    from ise_exporter3.datasets.posture_current import parse_posture_requirements

    requirements = list(parse_posture_requirements(REAL_POSTURE_REPORT))
    assert ("Req-AV-Scan-Recent", "Audit", "Failed") in requirements
    assert ("Req-AV-Installed", "Optional", "Passed") in requirements
    # The conditions blob carries colons of its own; only the first three fields
    # are the requirement, so it must not leak into the result label.
    assert all(":" not in field
               for entry in requirements for field in entry)
    assert not [entry for entry in requirements if entry[1] == "Mandatory"]


def test_an_empty_or_absent_report_yields_nothing_rather_than_a_bad_label():
    from ise_exporter3.datasets.posture_current import (
        parse_posture_report, parse_posture_requirements)

    for value in ("", None, "   ", "not a report"):
        assert list(parse_posture_report(value)) == []
        assert list(parse_posture_requirements(value)) == []


def test_posture_is_read_from_other_attr_string_where_the_appliance_puts_it():
    """The bug this projection shipped with, and the reason it went unseen.

    On a deployment that runs posture the report, the agent version and the
    eligibility flag are CamelCase *attributes* of other_attr_string -- like
    both latencies -- not elements. Reading only the elements collected nothing
    there, while the lab's empty top-level `posture_status` element made the
    absence look like "this estate has no Secure Client".
    """
    projected = project({
        "calling_station_id": "00:11:22:33:44:55",
        "posture_status": "",
        "other_attr_string": (
            ":!:PostureStatus=Compliant"
            ":!:PostureApplicable=Yes"
            ":!:PostureAssessmentStatus=NotApplicable"
            ":!:PostureAgentVersion=Posture Agent for Windows 5.1.17.3394"
            r":!:PostureReport=Corp-Firewall-On\;Passed\;"
            r"(Req-Firewall-Enabled:Mandatory:Passed:Passed_Conditions[]:"
            r"Failed_Conditions[]:Skipped_Conditions[])"
            ":!:"),
    })
    assert projected["posture_status"] == "Compliant"
    assert projected["posture_applicable"] == "Yes"
    assert projected["agent_version"] == "Posture Agent for Windows 5.1.17.3394"
    assert projected["posture_report"].startswith("Corp-Firewall-On")


def test_the_element_wins_over_the_attribute_where_both_are_populated():
    projected = project({
        "posture_status": "NonCompliant",
        "other_attr_string": ":!:PostureStatus=Compliant:!:",
    })
    assert projected["posture_status"] == "NonCompliant"


def test_the_assessment_trigger_state_never_displaces_the_verdict():
    # PostureAssessmentStatus is the last-assessment trigger state and reads
    # NotApplicable on sessions whose verdict is Compliant. Taking the first
    # Posture*Status found would report a verdict ISE never reached.
    projected = project({
        "other_attr_string": (
            ":!:PostureAssessmentStatus=NotApplicable"
            ":!:PostureStatus=Compliant:!:"),
    })
    assert projected["posture_status"] == "Compliant"
