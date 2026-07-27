"""RADIUS accounting starts, stops, and completed-session duration.

This is separate from ``active_sessions``. Accounting records describe events in
a reporting window; active sessions describe current state. Treating one as the
other made the old dashboards look complete while answering a different
question.

Event type is carried as a pair of measures rather than multiplied into every
group. The NAD, PSN, and authorization-policy views are complete marginals, so a
quiet device cannot disappear behind a top-K bound. Optional columns are selected
against the discovered schema: older production views may lack PSN, device,
policy, or session duration, and losing one dimension must not take down the
event totals that remain valid.
"""
from prometheus_client import Gauge

from .. import reporting
from ..labels import label
from ..model import Cost, Dataset, Provider
from ..parsing import finite


events = Gauge(
    "ise3_radius_accounting_events",
    "RADIUS accounting records in the scan window by event type and dimension",
    ["provider", "dimension", "value", "event_type"],
)
session_duration = Gauge(
    "ise3_radius_accounting_session_duration_seconds",
    "Completed RADIUS session duration by dimension and statistic",
    ["provider", "dimension", "value", "statistic"],
)
duration_coverage = Gauge(
    "ise3_radius_accounting_duration_coverage",
    "Fraction of accounting rows carrying a positive session duration",
    ["provider", "dimension", "value"],
)
window_seconds = Gauge(
    "ise3_radius_accounting_window_seconds",
    "Reporting window represented by the current RADIUS accounting snapshot",
    ["provider"],
)

_METRICS = (events, session_duration, duration_coverage, window_seconds)
VIEW = "radius_accounting"


def _has(schema, column):
    columns = None if schema is None else schema.get(VIEW.upper())
    return columns is None or column.upper() in columns


def _shape(schema):
    status = (
        "UPPER(NVL(acct_status_type, 'UNKNOWN'))"
        if _has(schema, "acct_status_type")
        else "'UNKNOWN'"
    )
    duration = (
        "CASE WHEN acct_session_time > 0 THEN acct_session_time END"
        if _has(schema, "acct_session_time")
        else "CAST(NULL AS NUMBER)"
    )
    dimensions = []
    for name, column in (
        ("nad", "device_name"),
        ("psn", "ise_node"),
        ("policy", "authorization_policy"),
    ):
        if _has(schema, column):
            dimensions.append((name, f"NVL({column}, 'unknown')"))
    # A schema with none of the optional dimensions still has a meaningful
    # total. Keep one synthetic marginal so the completeness/truncation contract
    # and the dashboard shape remain identical.
    if not dimensions:
        dimensions.append(("source", "'all'"))

    measures = (
        f"SUM(CASE WHEN {status} LIKE '%START%' THEN 1 ELSE 0 END) AS starts, "
        f"SUM(CASE WHEN {status} LIKE '%STOP%' THEN 1 ELSE 0 END) AS stops, "
        f"SUM(CASE WHEN {status} NOT LIKE '%START%' "
        f"AND {status} NOT LIKE '%STOP%' THEN 1 ELSE 0 END) AS other, "
        f"AVG({duration}) AS mean_duration, "
        f"MAX({duration}) AS max_duration, "
        f"COUNT({duration}) AS duration_samples, COUNT(*) AS records"
    )
    return tuple(dimensions), measures


def statements(hours, limits, schema=None):
    recent = reporting.recent("timestamp", hours, limits)
    dimensions, measures = _shape(schema)
    return {
        "totals": f"""
            SELECT {measures}
            FROM {VIEW}
            WHERE {recent}
        """,
        "marginals": reporting.marginals(
            VIEW, recent, dimensions, measures, limits=limits
        ),
    }


def _publish(ctx, dimension, value, row):
    for event_type in ("starts", "stops", "other"):
        ctx.set(
            events,
            finite(row.get(event_type)),
            dimension=dimension,
            value=value,
            event_type=event_type,
        )
    samples = finite(row.get("duration_samples"))
    records = finite(row.get("records"))
    ctx.set(
        duration_coverage,
        samples / records if records else 0.0,
        dimension=dimension,
        value=value,
    )
    if samples:
        ctx.set(
            session_duration,
            finite(row.get("mean_duration")),
            dimension=dimension,
            value=value,
            statistic="mean",
        )
        ctx.set(
            session_duration,
            finite(row.get("max_duration")),
            dimension=dimension,
            value=value,
            statistic="max",
        )


def fetch(ctx):
    hours = reporting.window_hours(ctx.dataset.default_interval, ctx.limits)
    schema = getattr(ctx.transport, "schema", None)
    results = ctx.transport.query_many(statements(hours, ctx.limits, schema))
    ctx.set(window_seconds, hours * 3600)

    for row in results.get("totals", []):
        _publish(ctx, "total", "all", row)

    rows = results.get("marginals", [])
    reporting.publish_truncation(ctx, "marginals", rows)
    for dimension, entries in reporting.by_dimension(rows).items():
        for row in entries:
            (value,) = reporting.group(row, "value")
            _publish(ctx, dimension, label(value), row)


DATASET = Dataset(
    name="radius_accounting",
    description="RADIUS accounting events and completed-session duration",
    default_interval=1800,
    metrics=_METRICS,
    providers=(
        Provider(
            name="dataconnect",
            cost=Cost(target="oracle", db_seconds=6.0),
            supplies=frozenset(
                {"event_type", "nad", "psn", "policy", "session_duration"}
            ),
            requires=("view:RADIUS_ACCOUNTING",),
            fetch=fetch,
        ),
    ),
)
