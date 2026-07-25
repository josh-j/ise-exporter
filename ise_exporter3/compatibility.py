"""The supported ISE contract and the shapes its responses must satisfy.

Ported from v2. The exact-release check is deliberate: a newer patch, ISE 3.4 or
an older 3.3 patch must be evaluated explicitly before the contract changes. The
value bounds below exist so a malformed or hostile response cannot become
unbounded Prometheus state.
"""
from __future__ import annotations


SUPPORTED_ISE_VERSION = "3.3.0.430"
SUPPORTED_PATCH_LEVEL = 11

MAX_DEPLOYMENT_NODES = 100
MAX_LICENSE_TIERS = 32
MAX_CERTIFICATES_PER_STORE = 1000
MAX_CERTIFICATE_ROWS = 5000

DEPLOYMENT_NODE_STATES = (
    "Connected", "Disconnected", "InProgress", "NotApplicable", "NotInSync",
    "NotUpgraded", "RegistrationFailed", "ReplicationStopped",
)
DEPLOYMENT_NODE_ROLES = frozenset({
    "PrimaryAdmin", "PrimaryDedicatedMonitoring", "PrimaryMonitoring",
    "SecondaryAdmin", "SecondaryDedicatedMonitoring", "SecondaryMonitoring",
    "Standalone",
})
DEPLOYMENT_NODE_SERVICES = frozenset({
    "DeviceAdmin", "PassiveIdentity", "Profiler", "SXP", "Session", "TC-NAC",
    "pxGrid", "pxGridCloud",
})


class CompatibilityError(RuntimeError):
    """The connected deployment does not satisfy the supported contract."""


def valid_hostname(value):
    """Accept DNS-safe ISE node names usable as metric labels and URL paths."""
    if not isinstance(value, str) or not value or len(value) > 253:
        return False
    allowed = frozenset(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-")
    return all(
        1 <= len(part) <= 63
        and part[0] != "-" and part[-1] != "-"
        and not (set(part) - allowed)
        for part in value.split("."))


def validated_nodes(value):
    """Return the deployment node list only when its identity fields are sane."""
    if not isinstance(value, list) or len(value) > MAX_DEPLOYMENT_NODES:
        return None
    if not all(isinstance(node, dict) for node in value):
        return None
    hostnames = [node.get("hostname") for node in value]
    if any(not valid_hostname(hostname) for hostname in hostnames):
        return None
    if len({hostname.casefold() for hostname in hostnames}) != len(hostnames):
        return None
    return value
