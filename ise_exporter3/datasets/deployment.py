"""Deployment nodes, personas, services, and PAN HA.

OpenAPI is the only source: no other interface reports persona assignment or HA
state. Two bounded reads per cycle, independent of fleet size.

``hostname`` is the SHORT node name, which is also the token the per-node
certificate paths take; fqdn and ipAddress arrive beside it and are deliberately
not labelled on, since one identity per node is what dashboards join on.

The response is validated fail-closed against the Patch 11 contract. A node with
an unrecognised state, role or service fails the whole dataset rather than being
published as a plausible partial deployment -- this is the metric operators page
on, so a half-right answer is worse than a visible outage.
"""
from collections import defaultdict

from prometheus_client import Gauge

from ..compatibility import (
    DEPLOYMENT_NODE_ROLES,
    DEPLOYMENT_NODE_SERVICES,
    DEPLOYMENT_NODE_STATES,
    validated_nodes,
)
from ..labels import label
from ..model import Cost, Dataset, Provider


node_state = Gauge(
    "ise3_deployment_node_state",
    "Deployment node in the named state (1 for the current state)",
    ["provider", "node", "roles", "services", "state"])
node_count = Gauge(
    "ise3_deployment_nodes", "Deployment nodes by role", ["provider", "role"])
node_service_enabled = Gauge(
    "ise3_deployment_node_service_enabled",
    "Service currently assigned to a deployment node", ["provider", "node", "service"])
pan_ha_enabled = Gauge(
    "ise3_deployment_pan_ha_enabled", "PAN high availability is enabled", ["provider"])

_METRICS = (node_state, node_count, node_service_enabled, pan_ha_enabled)


def _validated_rows(ctx, nodes):
    rows, seen, roles_seen = [], set(), defaultdict(int)
    for node in nodes:
        hostname = node.get("hostname")
        state = node.get("nodeStatus")
        roles = node.get("roles")
        services = node.get("services")
        if hostname in seen:
            ctx.fail("invalid_response", f"duplicate deployment node {hostname!r}")
        if state not in DEPLOYMENT_NODE_STATES:
            ctx.fail("invalid_response",
                     f"node {hostname!r} reported unknown status {state!r}")
        if not isinstance(roles, list) or not isinstance(services, list):
            ctx.fail("invalid_response",
                     f"node {hostname!r} reported malformed roles or services")
        if (any(role not in DEPLOYMENT_NODE_ROLES for role in roles)
                or any(service not in DEPLOYMENT_NODE_SERVICES for service in services)
                or len(roles) != len(set(roles))
                or len(services) != len(set(services))):
            ctx.fail("invalid_response",
                     f"node {hostname!r} reported unknown roles or services")
        seen.add(hostname)
        for role in roles or ["PSN"]:
            roles_seen[label(role)] += 1
        rows.append({
            "node": label(hostname),
            "roles": label(",".join(roles) if roles else "PSN"),
            "services": label(",".join(services) if services else "none"),
            "service_list": [label(service) for service in services],
            "state": state,
        })
    return rows, roles_seen


def fetch(ctx):
    nodes = validated_nodes(
        ctx.transport.get_openapi("/deployment/node", api="pan_nodes"))
    if nodes is None:
        ctx.fail("invalid_response", "the deployment node list failed validation")
    if not nodes:
        ctx.fail("invalid_response", "ISE reported no deployment nodes")

    rows, roles_seen = _validated_rows(ctx, nodes)

    pan_ha = ctx.transport.get_openapi("/deployment/pan-ha", api="pan_ha")
    if not isinstance(pan_ha, dict) or not isinstance(pan_ha.get("isEnabled"), bool):
        ctx.fail("invalid_response", "PAN HA status was missing or malformed")

    for row in rows:
        # Publish every possible state so a transition is a visible 1 -> 0 rather
        # than a series that silently disappears.
        for state in DEPLOYMENT_NODE_STATES:
            ctx.set(node_state, int(state == row["state"]),
                    node=row["node"], roles=row["roles"],
                    services=row["services"], state=state)
        for service in row["service_list"]:
            ctx.set(node_service_enabled, 1, node=row["node"], service=service)
    for role, count in roles_seen.items():
        ctx.set(node_count, count, role=role)
    ctx.set(pan_ha_enabled, int(pan_ha["isEnabled"]))


DATASET = Dataset(
    name="deployment",
    description="Deployment nodes, personas, services, PAN HA",
    default_interval=300,
    metrics=_METRICS,
    providers=(
        Provider(
            name="openapi",
            # GET /deployment/node + GET /deployment/pan-ha. Fixed cost; a
            # deployment is at most a handful of nodes and the list is one page.
            cost=Cost(target="pan", requests=2),
            supplies=frozenset({"node", "persona", "service", "ha"}),
            fetch=fetch,
        ),
    ),
)
