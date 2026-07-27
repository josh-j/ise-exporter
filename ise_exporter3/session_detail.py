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

- **retains less.** Thirteen resolved fields instead of the whole document.
- **resolves ISE's spelling variants once.** The same posture verdict arrives as
  ``posture_status``, ``PostureStatus`` or ``posture_assessment_status``
  depending on release and field; the readers used to try each name on every
  read, which meant the variant handling ran 20,000 times a cycle forever rather
  than once per session.
- **parses ``other_attr_string`` once.** It is the field the authorization rule
  and policy set are buried in, it was parsed on every read of every record on
  every cycle, and the raw string no longer has to be kept at all.

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
_POSTURE_REPORT_FIELDS = ("posture_report", "PostureReport")
_AGENT_FIELDS = ("posture_agent_version", "PostureAgentVersion", "agent_version")
_OS_FIELDS = ("operating_system", "os_type", "endpoint_operating_system")
_EXECUTION_STEP_FIELDS = ("execution_steps", "ExecutionSteps", "Steps")
_STEP_LATENCY_FIELDS = ("step_latency", "StepLatency")
_TOTAL_LATENCY_FIELDS = (
    "total_authen_latency",
    "TotalAuthenLatency",
    "total_authentication_latency",
    "TotalAuthenticationLatency",
)

# Parsed out of other_attr_string rather than read from a column of their own.
POLICY_SET_ATTRIBUTE = "ISEPolicySetName"
AUTHZ_RULE_ATTRIBUTE = "AuthorizationPolicyMatchedRule"


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
        "method": first(record, _METHOD_FIELDS),
        "profiles": first(record, _PROFILE_FIELDS),
        "authz_rule": attributes.get(AUTHZ_RULE_ATTRIBUTE, ""),
        "policy_set": attributes.get(POLICY_SET_ATTRIBUTE, ""),
        "posture_status": first(record, _POSTURE_STATUS_FIELDS),
        "posture_report": first(record, _POSTURE_REPORT_FIELDS),
        "agent_version": first(record, _AGENT_FIELDS),
        "operating_system": first(record, _OS_FIELDS),
        "execution_steps": first(record, _EXECUTION_STEP_FIELDS),
        "step_latency": first(record, _STEP_LATENCY_FIELDS),
        "total_authentication_latency": first(record, _TOTAL_LATENCY_FIELDS),
    }
