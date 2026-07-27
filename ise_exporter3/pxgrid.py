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
