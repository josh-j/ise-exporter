"""Small, release-tolerant normalisers for pxGrid records.

ISE 3.3 varies the case and nesting of endpoint attributes, while session
payloads differ slightly between the REST snapshot and pubsub topic.  Keep
those differences here so the transport and datasets do not each grow their
own almost-equivalent spelling tables.
"""
from __future__ import annotations


GONE_SESSION_STATES = frozenset({
    "DISCONNECT", "DISCONNECTED", "STOPPED", "TERMINATED",
})


def as_list(value, singular=""):
    """Normalize Cisco's scalar, list, and nested-singular collection shapes."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and singular:
        nested = value.get(singular)
        if nested is None:
            nested = value.get(singular[:1].upper() + singular[1:])
        if nested is not None:
            return as_list(nested)
    return [value]


def first(record, *keys):
    """Return the first non-empty value from a mapping."""
    if not isinstance(record, dict):
        return ""
    for key in keys:
        value = record.get(key)
        if value is not None and str(value).strip():
            return value
    return ""


def endpoint_attribute(record, *keys):
    """Read a pxGrid endpoint attribute from its flat or nested location."""
    value = first(record, *keys)
    if value:
        return value
    for container in ("customAttributes", "attributes", "otherAttributes"):
        value = first(record.get(container), *keys)
        if value:
            return value
    return ""


def normalize_mac(value):
    text = str(value or "").strip().upper().replace("-", ":").replace(".", "")
    if ":" not in text and len(text) == 12:
        text = ":".join(text[index:index + 2] for index in range(0, 12, 2))
    return text


def session_key(record):
    return str(first(
        record, "auditSessionId", "audit_session_id", "sessionId", "session_id",
        "callingStationId", "calling_station_id", "macAddress", "id",
    ) or "").strip()


def session_state(record):
    return str(first(record, "state", "status") or "").strip().upper()


def bool_label(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value or "").strip().lower()
    if text in {"true", "yes", "1", "registered", "compliant", "enabled"}:
        return "true"
    if text in {"false", "no", "0", "unregistered", "noncompliant", "disabled"}:
        return "false"
    return "unknown"


# The fields an operator reading Context Visibility is looking at, and no more.
# A pxGrid session record is large and mostly internal; serving it whole over the
# operator API would put the appliance's session table in a terminal and make
# every field an accidental contract. This is the same discipline session_detail
# keeps for the MnT cache: project what is read, resolve the spelling once.
def project_session(record):
    """One pxGrid session reduced to the fields the operator surface shows."""
    if not isinstance(record, dict):
        return None
    mac = normalize_mac(first(
        record, "macAddress", "mac_address", "callingStationId",
        "calling_station_id"))
    if not mac:
        # Without a MAC there is nothing to join an endpoint to, and an
        # unjoinable session on a Context Visibility row is noise.
        return None
    addresses = as_list(first(record, "ipAddresses", "ip_addresses"))
    address = str(addresses[0] if addresses else first(
        record, "framedIpAddress", "framed_ip_address", "ipAddress")).strip()
    profiles = as_list(first(
        record, "selectedAuthzProfiles", "selected_authz_profiles",
        "authorizationProfiles"))
    # nasIdentifier is what ISE 3.3 actually calls the device by. The other two
    # spellings are kept because other releases and the pubsub topic use them,
    # but reading them first left every NAD column empty against a live
    # appliance -- networkDeviceProfileName, the field that *is* always there,
    # is the device profile ("Cisco") and not its name.
    nad = first(record, "nasIdentifier", "nas_identifier", "nasName",
                "networkDeviceName", "network_device_name")
    # ISE names the serving node in `providers` and writes the string "None"
    # when there is nothing to name, which is not a node.
    providers = [str(name).strip() for name in as_list(first(record, "providers"))
                 if str(name).strip() and str(name).strip().lower() != "none"]
    return {
        "mac_address": mac,
        "ip_address": address,
        "user_name": str(first(record, "userName", "user_name") or ""),
        "nad": str(nad or ""),
        "nas_ip_address": str(first(
            record, "nasIpAddress", "nas_ip_address") or ""),
        "nas_port": str(first(record, "nasPortId", "nas_port_id") or ""),
        "endpoint_profile": str(first(
            record, "endpointProfile", "endpoint_profile") or ""),
        # Real fields, not the session state standing in for them.
        "auth_method": str(first(record, "authMethod", "auth_method") or ""),
        "auth_protocol": str(first(record, "authProtocol", "auth_protocol") or ""),
        "posture_status": str(first(
            record, "postureStatus", "posture_status") or ""),
        "authorization_profiles": ", ".join(
            str(profile).strip() for profile in profiles if str(profile).strip()),
        "security_group": str(first(
            record, "securityGroup", "security_group") or ""),
        "ise_node": ", ".join(providers) or str(first(
            record, "psnName", "psn_name", "iseNode", "ise_node", "server") or ""),
        "session_state": session_state(record),
        "audit_session_id": str(first(
            record, "auditSessionId", "audit_session_id") or ""),
        "last_update": str(first(
            record, "timestamp", "lastUpdateTime", "last_update_time") or ""),
    }
