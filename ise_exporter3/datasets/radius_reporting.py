"""RADIUS authentication outcomes, methods, policies, and latency.

Data Connect only: no other interface aggregates historical authentication.

Exact totals come from Cisco's ``RADIUS_AUTHENTICATION_SUMMARY`` aggregate view.

**The breakdowns are complete, not top-K.** Passed and failed are carried as
*measures* rather than as a dimension, so outcome adds two columns instead of
doubling the group count; the remaining dimensions are reduced to marginals in
one GROUPING SETS pass. At the declared scale that is roughly 5,000 NADs plus a
few dozen PSNs, methods and policies -- one statement, every group. Capping this
by volume would have hidden exactly the quiet NAD an operator is looking for.

Latency is handled as untrusted input (see ``latency.py``): passed and failed are
timed on different code paths and never share a series, zero means not measured,
and CSCwm43211 means an inflated value can be plausible. The accounting-derived
figures are indicative; these authentication-side ones are preferred.
"""
from prometheus_client import Gauge

from .. import latency, reporting
from ..model import Cost, Dataset, Provider
from ..parsing import finite


authentications = Gauge(
    "ise3_radius_authentications", "Authentications in the scan window by outcome",
    ["provider", "status"])
by_method = Gauge(
    "ise3_radius_authentications_by_method", "Authentications per method",
    ["provider", "method", "status"])
by_nad = Gauge(
    "ise3_radius_authentications_by_nad", "Authentications per NAD",
    ["provider", "nad", "status"])
by_psn = Gauge(
    "ise3_radius_authentications_by_psn", "Authentications per PSN",
    ["provider", "psn", "status"])
by_policy = Gauge(
    "ise3_radius_authentications_by_policy", "Authentications per authorization policy",
    ["provider", "policy", "status"])
by_protocol = Gauge(
    "ise3_radius_authentications_by_protocol", "Authentications per protocol",
    ["provider", "protocol", "status"])
by_psn_detail = Gauge(
    "ise3_radius_authentications_by_psn_detail",
    "Authentications per PSN from the raw view, which carries dimensions the "
    "summary view does not", ["provider", "psn", "status"])
latency_seconds = Gauge(
    "ise3_radius_authentication_latency_seconds",
    "Mean authentication latency, over samples that could be trusted",
    ["provider", "status", "psn"])
latency_coverage = Gauge(
    "ise3_radius_authentication_latency_coverage",
    "Fraction of latency samples that were usable; low means ISE is not "
    "populating the field, not that latency improved",
    ["provider", "status", "psn"])
distinct_endpoints = Gauge(
    "ise3_radius_distinct_endpoints_total",
    "Distinct endpoints attempting RADIUS authentication in the scan window",
    ["provider"])
failures_by_nad_method = Gauge(
    "ise3_radius_failures_by_nad_method",
    "Failed RADIUS authentications retaining the NAD and authentication method "
    "together; this troubleshooting cross-product is bounded and publishes "
    "its coverage",
    ["provider", "nad", "method"])
failure_summary = Gauge(
    "ise3_radius_failure_summary",
    "Failed RADIUS authentications by one historical failure-context dimension",
    ["provider", "dimension", "value"])
window_seconds = Gauge(
    "ise3_radius_reporting_window_seconds",
    "Reporting window represented by the current RADIUS authentication snapshot",
    ["provider"])

_METRICS = (authentications, by_method, by_nad, by_psn, by_policy, by_protocol,
            by_psn_detail, latency_seconds, latency_coverage,
            distinct_endpoints, failures_by_nad_method, failure_summary,
            window_seconds)

SUMMARY_VIEW = "radius_authentication_summary"
DETAIL_VIEW = "radius_authentications"


# Outcome as a measure pair: it adds two columns rather than doubling every
# group, which is what keeps a 5,000-NAD breakdown inside one statement.
SUMMARY_MEASURES = ("SUM(passed_count) AS passed, SUM(failed_count) AS failed")
DETAIL_MEASURES = (
    "SUM(CASE WHEN NVL(failed, 0) = 0 THEN 1 ELSE 0 END) AS passed, "
    "SUM(CASE WHEN NVL(failed, 0) = 0 THEN 0 ELSE 1 END) AS failed, "
    "AVG(response_time) AS mean_response, "
    "SUM(CASE WHEN response_time > 0 THEN 1 ELSE 0 END) AS timed"
)

SUMMARY_DIMENSIONS = (
    ("nad", "NVL(device_name, 'unknown')"),
    ("psn", "NVL(ise_node, 'unknown')"),
)
FAILURE_DIMENSION_COLUMNS = (
    ("authorization_profile", "authorization_profiles"),
    ("location", "location"),
    ("identity_store", "identity_store"),
    ("identity_group", "identity_group"),
    ("device_type", "device_type"),
    ("security_group", "security_group"),
)
DETAIL_DIMENSIONS = (
    ("method", "NVL(authentication_method, 'unknown')"),
    ("protocol", "NVL(authentication_protocol, 'unknown')"),
    ("policy", "NVL(authorization_rule, 'none')"),
    ("psn", "NVL(ise_node, 'unknown')"),
)


def _has(schema, view, column):
    columns = None if schema is None else schema.get(view.upper())
    return columns is None or column.upper() in columns


def _summary_dimensions(schema):
    dimensions = list(SUMMARY_DIMENSIONS)
    if _has(schema, SUMMARY_VIEW, "failure_reason"):
        dimensions.append((
            "failure_class",
            """CASE
                WHEN TRIM(failure_reason) IS NULL THEN 'unspecified'
                WHEN LOWER(failure_reason) LIKE '%password%'
                  OR LOWER(failure_reason) LIKE '%credential%'
                    THEN 'credentials'
                WHEN LOWER(failure_reason) LIKE '%certificate%'
                  OR LOWER(failure_reason) LIKE '%tls%'
                    THEN 'certificate_or_tls'
                WHEN LOWER(failure_reason) LIKE '%identity%'
                  OR LOWER(failure_reason) LIKE '%user not found%'
                    THEN 'identity'
                WHEN LOWER(failure_reason) LIKE '%timeout%'
                  OR LOWER(failure_reason) LIKE '%no response%'
                    THEN 'timeout'
                WHEN LOWER(failure_reason) LIKE '%policy%'
                  OR LOWER(failure_reason) LIKE '%reject%'
                  OR LOWER(failure_reason) LIKE '%denied%'
                    THEN 'policy_denied'
                ELSE 'other'
            END""",
        ))
    dimensions.extend(
        (name, f"NVL({column}, 'unknown')")
        for name, column in FAILURE_DIMENSION_COLUMNS
        if _has(schema, SUMMARY_VIEW, column)
    )
    return tuple(dimensions)


def statements(hours, limits, schema=None):
    """Exact totals, plus every marginal from two scans.

    The totals come from the aggregate view and stay exact independently of the
    breakdowns, which is the distinction that matters: a total and a breakdown
    answer different questions and must not be derived from one another.
    """
    summary_recent = reporting.recent("timestamp", hours, limits)
    detail_recent = reporting.recent("timestamp", hours, limits)
    result = {
        "totals": f"""
            SELECT SUM(passed_count) AS passed, SUM(failed_count) AS failed
            FROM {SUMMARY_VIEW}
            WHERE {summary_recent}
        """,
        "summary_marginals": reporting.marginals(
            SUMMARY_VIEW, summary_recent, _summary_dimensions(schema),
            SUMMARY_MEASURES,
            limits=limits),
        # Method, protocol, policy and latency are dimensions the summary view
        # does not expose, so they come from the bounded raw view.
        "detail_marginals": reporting.marginals(
            DETAIL_VIEW, detail_recent, DETAIL_DIMENSIONS, DETAIL_MEASURES,
            limits=limits),
        # This pair cannot be reconstructed from marginals: the operator's
        # question is specifically which method failed at which NAD. It is
        # therefore the one deliberately bounded RADIUS cross-product, with
        # COUNT(*) OVER() making that bound explicit.
        "failure_context": reporting.top_groups(
            "NVL(device_name, 'unknown') AS nad, "
            "NVL(authentication_method, 'unknown') AS method, "
            "COUNT(*) AS events",
            DETAIL_VIEW,
            f"{detail_recent} AND NVL(failed, 0) <> 0",
            "NVL(device_name, 'unknown'), "
            "NVL(authentication_method, 'unknown')",
            "events",
            limits=limits,
        ),
    }
    if _has(schema, SUMMARY_VIEW, "calling_station_id"):
        result["detail_totals"] = f"""
            SELECT COUNT(DISTINCT calling_station_id) AS endpoints
            FROM {SUMMARY_VIEW}
            WHERE {summary_recent}
        """
    return result


SUMMARY_GAUGES = {"nad": (by_nad, "nad"), "psn": (by_psn, "psn")}
DETAIL_GAUGES = {
    "method": (by_method, "method"),
    "protocol": (by_protocol, "protocol"),
    "policy": (by_policy, "policy"),
    "psn": (by_psn_detail, "psn"),
}


def _publish_outcomes(ctx, rows, gauges):
    for dimension, entries in reporting.by_dimension(rows).items():
        target = gauges.get(dimension)
        if target is None:
            continue
        gauge, label_name = target
        for row in entries:
            (value,) = reporting.group(row, "value")
            ctx.set(gauge, finite(row.get("passed")),
                    status="passed", **{label_name: value})
            ctx.set(gauge, finite(row.get("failed")),
                    status="failed", **{label_name: value})


def fetch(ctx):
    hours = reporting.window_hours(ctx.dataset.default_interval, ctx.limits)
    schema = getattr(ctx.transport, "schema", None)
    results = ctx.transport.query_many(statements(hours, ctx.limits, schema))
    ctx.set(window_seconds, hours * 3600)

    for row in results.get("totals", []):
        ctx.set(authentications, finite(row.get("passed")), status="passed")
        ctx.set(authentications, finite(row.get("failed")), status="failed")

    summary = results.get("summary_marginals", [])
    reporting.publish_truncation(ctx, "summary_marginals", summary)
    _publish_outcomes(ctx, summary, SUMMARY_GAUGES)
    for dimension, entries in reporting.by_dimension(summary).items():
        if dimension in SUMMARY_GAUGES:
            continue
        for row in entries:
            (value,) = reporting.group(row, "value")
            ctx.set(
                failure_summary,
                finite(row.get("failed")),
                dimension=dimension,
                value=value,
            )

    detail = results.get("detail_marginals", [])
    reporting.publish_truncation(ctx, "detail_marginals", detail)
    _publish_outcomes(ctx, detail, DETAIL_GAUGES)

    # Latency only from the PSN marginal: a mean over "all methods" mixes
    # populations that are not comparable, and a mean of means across groups of
    # different sizes is wrong regardless.
    for row in reporting.by_dimension(detail).get("psn", []):
        (psn,) = reporting.group(row, "value")
        timed = int(finite(row.get("timed")))
        usable = latency.observe(ctx, row.get("mean_response"), result="all")
        accumulator = latency.LatencyAccumulator()
        accumulator.seen = int(finite(row.get("passed")) + finite(row.get("failed")))
        if usable is not None and timed:
            accumulator.usable = timed
            accumulator.total = usable * timed
        if accumulator.mean is not None:
            ctx.set(latency_seconds, accumulator.mean, status="all", psn=psn)
        ctx.set(latency_coverage, accumulator.coverage or 0.0,
                status="all", psn=psn)

    for row in results.get("detail_totals", []):
        ctx.set(distinct_endpoints, finite(row.get("endpoints")))

    failure_context = results.get("failure_context", [])
    reporting.publish_truncation(ctx, "failure_context", failure_context)
    for row in failure_context:
        nad, method = reporting.group(row, "nad", "method")
        ctx.set(
            failures_by_nad_method,
            finite(row.get("events")),
            nad=nad,
            method=method,
        )


DATASET = Dataset(
    name="radius_reporting",
    description="RADIUS authentication outcomes, methods, policies, latency",
    default_interval=1800,
    metrics=_METRICS,
    providers=(
        Provider(
            name="dataconnect",
            # Five statements in one atomic batch under a single lease: exact
            # totals and distinct endpoints, two GROUPING SETS scans covering
            # every marginal, and one explicitly bounded failure correlation.
            cost=Cost(target="oracle", db_seconds=19.0),
            supplies=frozenset({
                "status", "method", "protocol", "policy", "nad", "psn",
                "latency", "distinct_endpoints", "failure_context"}),
            requires=(
                "view:RADIUS_AUTHENTICATION_SUMMARY",
                "view:RADIUS_AUTHENTICATIONS",
            ),
            fetch=fetch,
        ),
    ),
)
