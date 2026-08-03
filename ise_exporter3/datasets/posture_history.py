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
by_psn = Gauge(
    "ise3_posture_assessments_by_psn",
    "Distinct endpoints assessed by serving PSN and posture status",
    ["provider", "psn", "status"])
failed_conditions = Gauge(
    "ise3_posture_failed_conditions",
    "Distinct endpoints failing a posture condition. Endpoints, not condition "
    "hits: an endpoint failing three conditions counts once in each",
    ["provider", "condition"])
eligible_total = Gauge(
    "ise3_posture_eligible_endpoints_total",
    "Endpoints currently eligible for posture assessment",
    ["provider"])
eligible_recent = Gauge(
    "ise3_posture_eligible_recently_assessed_total",
    "Eligible endpoints assessed inside the reporting window",
    ["provider"])
eligible_without_recent = Gauge(
    "ise3_posture_eligible_without_recent_assessment_total",
    "Eligible endpoints without an assessment inside the reporting window",
    ["provider"])

_METRICS = (
    assessments, by_policy, by_agent_version, by_os, by_psn,
    failed_conditions, eligible_total, eligible_recent, eligible_without_recent,
)

ENDPOINT_VIEW = "posture_assessment_by_endpoint"
CONDITION_VIEW = "posture_assessment_by_condition"
INVENTORY_VIEW = "endpoints_data"

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


def statements(hours, limits):
    endpoint_recent = reporting.recent("timestamp", hours, limits)
    condition_recent = reporting.recent("logged_at", hours, limits)
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
            FETCH FIRST {reporting.group_limit(limits)} ROWS ONLY
        """,
        "psn_status": f"""
            SELECT NVL(ise_node, 'unknown') AS psn,
                   NVL(posture_status, 'Unknown') AS status,
                   COUNT(DISTINCT endpoint_mac_address) AS endpoints,
                   COUNT(*) OVER () AS group_total
            FROM {ENDPOINT_VIEW}
            WHERE {endpoint_recent}
            GROUP BY NVL(ise_node, 'unknown'),
                     NVL(posture_status, 'Unknown')
            ORDER BY psn, status
            FETCH FIRST {reporting.group_limit(limits)} ROWS ONLY
        """,
        "endpoint_marginals": reporting.marginals(
            ENDPOINT_VIEW, endpoint_recent, ENDPOINT_DIMENSIONS, ENDPOINT_MEASURE,
            limits=limits),
        "condition_marginals": reporting.marginals(
            CONDITION_VIEW,
            f"{condition_recent} AND NVL(condition_status, 'Failed') = 'Failed'",
            CONDITION_DIMENSIONS, CONDITION_MEASURE, limits=limits),
        "eligibility": f"""
            WITH eligible AS (
                SELECT DISTINCT UPPER(REPLACE(REPLACE(mac_address, ':', ''), '-', ''))
                                AS endpoint_mac
                FROM {INVENTORY_VIEW}
                WHERE NVL(posture_applicable, 0) = 1
                  AND mac_address IS NOT NULL
            ),
            recently_assessed AS (
                SELECT DISTINCT UPPER(REPLACE(REPLACE(endpoint_mac_address, ':', ''), '-', ''))
                                AS endpoint_mac
                FROM {ENDPOINT_VIEW}
                WHERE {endpoint_recent}
                  AND endpoint_mac_address IS NOT NULL
            )
            SELECT COUNT(*) AS eligible,
                   SUM(CASE WHEN recently_assessed.endpoint_mac IS NOT NULL
                            THEN 1 ELSE 0 END) AS assessed,
                   SUM(CASE WHEN recently_assessed.endpoint_mac IS NULL
                            THEN 1 ELSE 0 END) AS without_recent
            FROM eligible
            LEFT JOIN recently_assessed
              ON recently_assessed.endpoint_mac = eligible.endpoint_mac
        """,
    }


def fetch(ctx):
    hours = reporting.scan_window(ctx)
    results = ctx.transport.query_many(statements(hours, ctx.limits))

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

    psn_pairs = results.get("psn_status", [])
    reporting.publish_truncation(ctx, "psn_status", psn_pairs)
    for row in psn_pairs:
        psn, status = reporting.group(row, "psn", "status")
        ctx.set(
            by_psn,
            finite(row.get("endpoints")),
            psn=psn,
            status=status,
        )

    conditions = results.get("condition_marginals", [])
    reporting.publish_truncation(ctx, "condition_marginals", conditions)
    for row in conditions:
        (value,) = reporting.group(row, "value")
        ctx.set(failed_conditions, finite(row.get("endpoints")), condition=value)

    for row in results.get("eligibility", []):
        ctx.set(eligible_total, finite(row.get("eligible")))
        ctx.set(eligible_recent, finite(row.get("assessed")))
        ctx.set(eligible_without_recent, finite(row.get("without_recent")))


DATASET = Dataset(
    name="posture_history",
    description="Historical posture assessments, conditions, Secure Client versions",
    # This feeds a rolling-window percentage. At the old six-hour cadence the
    # value was correct only at collection time but looked frozen for an entire
    # operator shift. Hourly still costs only 12 Oracle seconds per hour.
    default_interval=3600,
    windowed=True,
    metrics=_METRICS,
    providers=(
        Provider(
            name="dataconnect",
            cost=Cost(target="oracle", db_seconds=12.0),
            supplies=frozenset({
                "status", "policy", "condition", "os", "agent_version", "psn",
                "eligibility"}),
            requires=(
                "view:POSTURE_ASSESSMENT_BY_ENDPOINT",
                "view:ENDPOINTS_DATA",
            ),
            fetch=fetch,
        ),
    ),
)
