"""One cached MnT session record, reduced to what is actually read.

``session_authorization`` and ``posture_current`` share one cache of per-session
detail, keyed by MAC and held for the life of the session. At the declared scale
that is 20,000 records, and it was the largest single thing this process
retained -- because it retained the *whole* MnT record: authentication server
internals, accounting counters, NAS port detail, timestamps, and a dozen fields
neither dataset has ever looked at.

``network_devices`` has always kept a normalised projection of its ERS records
instead of the records themselves, for exactly this reason. This is that
discipline applied to the bigger cache, and it does three things:

- **retains less.** Fourteen resolved fields instead of the whole document.
- **resolves ISE's spelling variants once.** The same posture verdict arrives as
  ``posture_status``, ``PostureStatus`` or ``posture_assessment_status``
  depending on release and field; the readers used to try each name on every
  read, which meant the variant handling ran 20,000 times a cycle forever rather
  than once per session.
- **parses ``other_attr_string`` once.** It is the field the authorization rule,
  the policy set and both authentication latencies are buried in, it was parsed
  on every read of every record on every cycle, and the raw string no longer has
  to be kept at all.

A field is projected where the appliance answers it, and "the appliance did not
answer it here" is not the same claim as "this API cannot answer it". Neither
latency is an element on ISE 3.3, under any spelling, on any session -- those
are read out of ``other_attr_string`` instead.

Posture turned out to be the same case, not a different one. This docstring used
to say the posture fields were merely unexercised by a lab with no Secure
Client, and that they would populate as elements wherever posture ran. They do
not: on a deployment that runs posture they arrive as CamelCase attributes of
``other_attr_string`` -- ``PostureReport``, ``PostureAgentVersion``,
``PostureApplicable`` -- exactly like the latencies. Reading only the elements
collected nothing there, and the empty top-level ``posture_status`` element made
the absence look already explained. Every posture lookup now tries the elements
and then the attributes, which is what ``_either`` is for.

The lesson generalises: an estate that does not exercise a field cannot tell you
where that field lives, so "absent here" is never evidence about the shape.

The projection is deliberately flat and fully resolved: a reader gets a value or
an empty string, never a choice of field names. Adding a field here is the
supported way to read something new -- reaching past it to the raw record is not
possible, because the raw record is gone.
"""
from __future__ import annotations


# ISE spells the same fact several ways across releases and API surfaces. Each
# tuple is tried in order and the first non-empty value wins.
_NAD_FIELDS = ("network_device_name",)
_NAS_IP_FIELDS = ("nas_ip_address",)
_FAILURE_FIELDS = ("failure_reason",)
_METHOD_FIELDS = ("authentication_method",)
_PROFILE_FIELDS = ("selected_azn_profiles",)
_POSTURE_STATUS_FIELDS = (
    "posture_status", "PostureStatus", "posture_assessment_status")
# Empty on an estate with no Secure Client, populated where posture runs. Data
# Connect names the same three facts POSTURE_REPORT, POSTURE_AGENT_VERSION and
# ENDPOINT_OPERATING_SYSTEM, which is what confirms they are ISE fields rather
# than spellings this exporter invented.
_POSTURE_REPORT_FIELDS = ("posture_report", "PostureReport")
_AGENT_FIELDS = ("posture_agent_version", "PostureAgentVersion", "agent_version")
_OS_FIELDS = ("operating_system", "os_type", "endpoint_operating_system")
_MESSAGE_CODE_FIELDS = ("message_code",)
# Neither latency has a top-level element on ISE 3.3; response_time is the only
# one, and it carries the same milliseconds as the TotalAuthenLatency attribute.
_TOTAL_LATENCY_FIELDS = ("response_time",)

# Parsed out of other_attr_string rather than read from a column of their own.
POLICY_SET_ATTRIBUTE = "ISEPolicySetName"
AUTHZ_RULE_ATTRIBUTE = "AuthorizationPolicyMatchedRule"
# Both latencies are attributes of other_attr_string, not elements of the
# session document: ISE 3.3 emits no step_latency/StepLatency or
# total_authen_latency/TotalAuthenLatency tag under any spelling.
STEP_LATENCY_ATTRIBUTE = "StepLatency"
TOTAL_LATENCY_ATTRIBUTE = "TotalAuthenLatency"
# Posture is the same case as the latencies, and for the same reason it was
# missed: on a deployment that runs posture the report, the agent version and
# the eligibility flag arrive as CamelCase *attributes* of other_attr_string,
# not as elements. The lab has an empty top-level ``posture_status`` element and
# nothing else, which reads as "this estate does not run posture" -- so the
# absence looked explained and the attribute side was never tried.
_POSTURE_APPLICABLE_FIELDS = ("PostureApplicable", "posture_applicable")


def first(record, names):
    """The first of ``names`` this record answers with something."""
    for name in names:
        value = record.get(name)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def parse_attributes(value):
    """Parse ISE's ``:!:Key=Value:!:...`` other_attr_string format."""
    parsed = {}
    for part in str(value or "").split(":!:"):
        key, separator, item = part.partition("=")
        if separator and key.strip() and item.strip():
            parsed[key.strip()] = item.strip()
    return parsed


def _either(record, attributes, names):
    """The first answer from the elements, else from the parsed attributes.

    ISE puts the same fact in either place depending on the field and the
    release, and a reader cannot tell which without looking in both. Elements
    win where both are populated, because that is the document's own column.
    """
    return first(record, names) or first(attributes, names)


def _truthy(value):
    return str(value or "").strip().lower() == "true"


def project(record):
    """Reduce one raw MnT session record to the fields its readers use.

    ``has_verdict`` is kept separate from ``passed``/``failed`` on purpose: an
    accounting-only record carries neither, and counting it as an authorization
    would dilute every ratio built from this cache. "Absent" and "false" are
    different answers and the projection has to preserve that distinction, which
    is the one thing a naive ``{k: record.get(k)}`` would quietly lose.
    """
    record = record or {}
    attributes = parse_attributes(record.get("other_attr_string"))
    return {
        "nad": first(record, _NAD_FIELDS),
        "nas_ip": first(record, _NAS_IP_FIELDS),
        # Location is deliberately absent: network_devices publishes
        # ise3_network_device_assignment{nad,location,ops_owner}, so a dashboard
        # joins for it rather than this cache holding 20,000 copies.
        "has_verdict": "passed" in record or "failed" in record,
        "passed": _truthy(record.get("passed")),
        "failed": _truthy(record.get("failed")),
        "failure_reason": first(record, _FAILURE_FIELDS),
        # The ISE result code, and the only reason code a real session document
        # carries: failure_reason is not an element of it on ISE 3.3.
        "message_code": first(record, _MESSAGE_CODE_FIELDS),
        "method": first(record, _METHOD_FIELDS),
        "profiles": first(record, _PROFILE_FIELDS),
        "authz_rule": attributes.get(AUTHZ_RULE_ATTRIBUTE, ""),
        "policy_set": attributes.get(POLICY_SET_ATTRIBUTE, ""),
        "posture_status": _either(record, attributes, _POSTURE_STATUS_FIELDS),
        "posture_report": _either(record, attributes, _POSTURE_REPORT_FIELDS),
        "posture_applicable": _either(
            record, attributes, _POSTURE_APPLICABLE_FIELDS),
        "agent_version": _either(record, attributes, _AGENT_FIELDS),
        "operating_system": _either(record, attributes, _OS_FIELDS),
        "step_latency": attributes.get(STEP_LATENCY_ATTRIBUTE, ""),
        "total_authentication_latency": (
            attributes.get(TOTAL_LATENCY_ATTRIBUTE, "")
            or first(record, _TOTAL_LATENCY_FIELDS)),
    }
