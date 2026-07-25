"""Historical posture assessments, conditions, and Secure Client versions.

Data Connect only, and distinct from ``posture_current``: this is what was
assessed over a reporting window, not what is compliant right now.

Complete, not top-K. Two shapes are used deliberately:

- **policy x status** is kept as a real two-dimensional group, because both sides
  are small (tens of policies, six statuses) and the pair is the question -- "how
  many endpoints failed which policy" cannot be reconstructed from marginals.
- everything else is marginal, so agent version, OS and condition each cover
  their whole range instead of competing for one row budget.

A cross product is affordable exactly when both sides are bounded. That is the
test, not a blanket ban.

Note for dashboards, because it has misled before: endpoints posture on connect
or periodically, and offline endpoints never enter the window at all. A standing
population of unassessed endpoints is normal and there is **no 100% target**.
"""
from prometheus_client import Gauge

from .. import reporting
from ..model import Cost, Dataset, Provider
from ..parsing import finite


assessments = Gauge(
    "ise3_posture_assessments", "Endpoints assessed in the window by status",
    ["provider", "status"])
by_policy = Gauge(
    "ise3_posture_assessments_by_policy",
    "Distinct endpoints assessed against a posture policy, by outcome. Counts "
    "endpoints once each: summing across conditions would double-count",
    ["provider", "policy", "status"])
by_agent_version = Gauge(
    "ise3_posture_assessments_by_agent_version",
    "Distinct endpoints assessed, per Secure Client agent version",
    ["provider", "agent_version"])
by_os = Gauge(
    "ise3_posture_assessments_by_os", "Distinct endpoints assessed, per OS",
    ["provider", "os"])
failed_conditions = Gauge(
    "ise3_posture_failed_conditions",
    "Distinct endpoints failing a posture condition. Endpoints, not condition "
    "hits: an endpoint failing three conditions counts once in each",
    ["provider", "condition"])

_METRICS = (assessments, by_policy, by_agent_version, by_os, failed_conditions)

ENDPOINT_VIEW = "posture_assessment_by_endpoint"
CONDITION_VIEW = "posture_assessment_by_condition"

# Column names verified against the live 3.3 P11 catalogue. The two posture
# views disagree with each other: the endpoint view keys on
# ENDPOINT_MAC_ADDRESS and times on TIMESTAMP, the condition view keys on
# ENDPOINT_ID and times on LOGGED_AT.
ENDPOINT_DIMENSIONS = (
    ("agent_version", "NVL(posture_agent_version, 'unknown')"),
    ("os", "NVL(endpoint_operating_system, 'unknown')"),
    ("status", "NVL(posture_status, 'Unknown')"),
)
CONDITION_DIMENSIONS = (
    ("condition", "NVL(condition_name, 'unknown')"),
)
ENDPOINT_MEASURE = "COUNT(DISTINCT endpoint_mac_address) AS endpoints"
CONDITION_MEASURE = "COUNT(DISTINCT endpoint_id) AS endpoints"


def statements(hours):
    endpoint_recent = reporting.recent("timestamp", hours)
    condition_recent = reporting.recent("logged_at", hours)
    return {
        # Both sides bounded (tens of policies, six statuses), and the pair is
        # the question, so this one stays a real two-dimensional group.
        "policy_status": f"""
            SELECT NVL(posture_policy_matched, 'none') AS policy,
                   NVL(posture_status, 'Unknown') AS status,
                   COUNT(DISTINCT endpoint_mac_address) AS endpoints,
                   COUNT(*) OVER () AS group_total
            FROM {ENDPOINT_VIEW}
            WHERE {endpoint_recent}
            GROUP BY NVL(posture_policy_matched, 'none'),
                     NVL(posture_status, 'Unknown')
            ORDER BY policy, status
            FETCH FIRST {reporting.MAX_GROUPS} ROWS ONLY
        """,
        "endpoint_marginals": reporting.marginals(
            ENDPOINT_VIEW, endpoint_recent, ENDPOINT_DIMENSIONS, ENDPOINT_MEASURE),
        "condition_marginals": reporting.marginals(
            CONDITION_VIEW,
            f"{condition_recent} AND NVL(condition_status, 'Failed') = 'Failed'",
            CONDITION_DIMENSIONS, CONDITION_MEASURE),
    }


def fetch(ctx):
    hours = reporting.window_hours(ctx.dataset.default_interval)
    results = ctx.transport.query_many(statements(hours))

    pairs = results.get("policy_status", [])
    reporting.publish_truncation(ctx, "policy_status", pairs)
    for row in pairs:
        policy, status = reporting.group(row, "policy", "status")
        ctx.set(by_policy, finite(row.get("endpoints")),
                policy=policy, status=status)

    rows = results.get("endpoint_marginals", [])
    reporting.publish_truncation(ctx, "endpoint_marginals", rows)
    marginal_gauges = {
        "agent_version": (by_agent_version, "agent_version"),
        "os": (by_os, "os"),
        "status": (assessments, "status"),
    }
    for dimension, entries in reporting.by_dimension(rows).items():
        target = marginal_gauges.get(dimension)
        if target is None:
            continue
        gauge, label_name = target
        for row in entries:
            (value,) = reporting.group(row, "value")
            ctx.set(gauge, finite(row.get("endpoints")), **{label_name: value})

    conditions = results.get("condition_marginals", [])
    reporting.publish_truncation(ctx, "condition_marginals", conditions)
    for row in conditions:
        (value,) = reporting.group(row, "value")
        ctx.set(failed_conditions, finite(row.get("endpoints")), condition=value)


DATASET = Dataset(
    name="posture_history",
    description="Historical posture assessments, conditions, Secure Client versions",
    default_interval=21600,
    metrics=_METRICS,
    providers=(
        Provider(
            name="dataconnect",
            cost=Cost(target="oracle", db_seconds=10.0, max_rows=6000),
            supplies=frozenset({
                "status", "policy", "condition", "os", "agent_version"}),
            requires=("view:POSTURE_ASSESSMENT_BY_ENDPOINT",),
            fetch=fetch,
        ),
    ),
)
