"""Endpoint profiling events by source and action.

Current endpoint inventory answers what profile an endpoint has now. This
dataset answers how profiling is changing: which sources are creating,
reclassifying, or otherwise updating endpoint profiles in the reporting window.
Both dimensions are small bounded vocabularies, so their pair is retained
instead of split into marginals that could not reconstruct the source/action
relationship.
"""
from prometheus_client import Gauge

from .. import reporting
from ..labels import label
from ..model import Cost, Dataset, Provider
from ..parsing import finite


events = Gauge(
    "ise3_endpoint_profile_events",
    "Endpoint profiling events by source and action",
    ["provider", "source", "action"],
)
events_total = Gauge(
    "ise3_endpoint_profile_events_total",
    "Endpoint profiling events in the reporting window",
    ["provider"],
)

_METRICS = (events, events_total)
VIEW = "profiled_endpoints_summary"


def _column(schema, column):
    columns = None if schema is None else schema.get(VIEW.upper())
    if columns is None or column.upper() in columns:
        return f"NVL({column}, 'unknown')"
    return "'unknown'"


def statements(hours, limits, schema=None):
    recent = reporting.recent("timestamp", hours, limits)
    source = _column(schema, "source")
    action = _column(schema, "endpoint_action_name")
    return {
        "totals": f"SELECT COUNT(*) AS events FROM {VIEW} WHERE {recent}",
        "source_action": f"""
            SELECT {source} AS source, {action} AS action,
                   COUNT(*) AS events, COUNT(*) OVER () AS group_total
            FROM {VIEW}
            WHERE {recent}
            GROUP BY {source}, {action}
            ORDER BY events DESC, source, action
            FETCH FIRST {reporting.group_limit(limits)} ROWS ONLY
        """,
    }


def fetch(ctx):
    hours = reporting.scan_window(ctx)
    schema = getattr(ctx.transport, "schema", None)
    results = ctx.transport.query_many(statements(hours, ctx.limits, schema))

    for row in results.get("totals", []):
        ctx.set(events_total, finite(row.get("events")))

    rows = results.get("source_action", [])
    reporting.publish_truncation(ctx, "source_action", rows)
    for row in rows:
        ctx.set(
            events,
            finite(row.get("events")),
            source=label(row.get("source")),
            action=label(row.get("action")),
        )


DATASET = Dataset(
    name="profile_events",
    description="Endpoint profiling events by source and action",
    default_interval=21600,
    windowed=True,
    metrics=_METRICS,
    providers=(
        Provider(
            name="dataconnect",
            cost=Cost(target="oracle", db_seconds=2.0),
            supplies=frozenset({"source", "action"}),
            requires=("view:PROFILED_ENDPOINTS_SUMMARY",),
            fetch=fetch,
        ),
    ),
)
