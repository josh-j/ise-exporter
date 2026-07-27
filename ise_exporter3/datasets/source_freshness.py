"""Per-view recency probe across the Data Connect reporting views.

Answers "is this view receiving rows at all", which is the difference between a
legitimately empty panel and a broken feed -- the question every other reporting
dataset depends on and none of them can answer about itself.

One statement per view would mean a paced wait each, holding the serialized lane
for many minutes. One `UNION ALL` would mean a slow branch blowing the whole
dataset's timeout. So the probe runs as a small batch of unions: bounded, and a
slow view damages only its own branch.
"""
from prometheus_client import Gauge

from .. import reporting
from ..model import Cost, Dataset, Provider
from ..parsing import finite


has_recent_rows = Gauge(
    "ise3_source_has_recent_rows",
    "A reporting view produced rows inside the scan window",
    ["provider", "view"])
latest_row_age_seconds = Gauge(
    "ise3_source_latest_row_age_seconds",
    "Age of the newest row in a reporting view", ["provider", "view"])

_METRICS = (has_recent_rows, latest_row_age_seconds)

# Each view carries its own time column, so a probe cannot assume TIMESTAMP --
# ENDPOINTS_DATA is a current-state view with UPDATE_TIME, and the performance
# rollup uses LOGGED_TIME. Verified against the live 3.3 P11 catalogue.
VIEW_BATCHES = (
    (("radius_authentications", "timestamp"),
     ("radius_authentication_summary", "timestamp"),
     ("radius_accounting", "timestamp")),
    (("radius_errors_view", "timestamp"),
     ("posture_assessment_by_endpoint", "timestamp"),
     ("endpoints_data", "update_time")),
    (("key_performance_metrics", "logged_time"),
     ("system_summary", "timestamp"),
     ("profiled_endpoints_summary", "timestamp")),
)


def _branch(view, column, hours, limits):
    return f"""
        SELECT '{view}' AS view_name,
               COUNT(*) AS recent_rows,
               NVL(
                 (CAST(SYSTIMESTAMP AS DATE) - CAST(MAX({column}) AS DATE)) * 86400,
                 -1) AS age_seconds
        FROM {view}
        WHERE {reporting.recent(column, hours, limits)}
    """


def statements(hours, limits):
    return {
        f"batch_{index}": "\nUNION ALL\n".join(
            _branch(view, column, hours, limits) for view, column in batch)
        for index, batch in enumerate(VIEW_BATCHES)
    }


def fetch(ctx):
    hours = reporting.scan_window(ctx)
    results = ctx.transport.query_many(statements(hours, ctx.limits))
    for rows in results.values():
        for row in rows:
            (view,) = reporting.group(row, "view_name")
            recent = finite(row.get("recent_rows"))
            ctx.set(has_recent_rows, int(recent > 0), view=view)
            age = finite(row.get("age_seconds"), -1)
            if age >= 0:
                ctx.set(latest_row_age_seconds, age, view=view)


DATASET = Dataset(
    name="source_freshness",
    description="Per-view row recency across Data Connect reporting views",
    default_interval=21600,
    windowed=True,
    metrics=_METRICS,
    providers=(
        Provider(
            name="dataconnect",
            cost=Cost(target="oracle", db_seconds=4.0),
            supplies=frozenset({"view", "has_recent_rows", "latest_row_age"}),
            fetch=fetch,
        ),
    ),
)
