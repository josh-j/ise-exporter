"""Per-view recency probe across the Data Connect reporting views.

Answers "is this view receiving rows at all", which is the difference between a
legitimately empty panel and a broken feed -- the question every other reporting
dataset depends on and none of them can answer about itself.

One statement per view would mean a paced wait each, holding the serialized lane
for many minutes. One `UNION ALL` would mean a slow branch blowing the whole
dataset's timeout. So the probe runs as a small batch of unions: bounded, and a
slow view damages only its own branch.

Three states have to stay distinguishable, because the operator's next action
differs for each: a view that is missing (no series), a view that exists and is
empty (rows 0, no age), and a view that holds rows but has stopped receiving
them (rows > 0, age far above the window). That is why the age is aggregated
over the whole view and the window survives only as a counted predicate.
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
    "Age of the newest row in a reporting view, measured over the whole view "
    "rather than the scan window, so a view staler than the window still has "
    "an age", ["provider", "view"])
rows_total = Gauge(
    "ise3_source_rows_total",
    "Rows a reporting view holds at all. Zero with no age is an empty view; "
    "non-zero with a large age is a stale feed; neither is a missing view",
    ["provider", "view"])

_METRICS = (has_recent_rows, latest_row_age_seconds, rows_total)

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
    # The age is deliberately NOT taken under the window predicate. Computing
    # the newest row inside the same window that decides whether the view is
    # recent means a stale view reports no age at all -- exactly the view an
    # operator is asking about -- and makes stale, empty and missing look
    # identical. The window survives as a counted predicate instead, so one
    # aggregate pass still answers both questions.
    return f"""
        SELECT '{view}' AS view_name,
               COUNT(*) AS total_rows,
               NVL(SUM(CASE WHEN {reporting.recent(column, hours, limits)}
                            THEN 1 ELSE 0 END), 0) AS recent_rows,
               NVL(
                 (CAST(SYSTIMESTAMP AS DATE) - CAST(MAX({column}) AS DATE)) * 86400,
                 -1) AS age_seconds
        FROM {view}
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
            ctx.set(rows_total, finite(row.get("total_rows")), view=view)
            age = finite(row.get("age_seconds"), -1)
            # -1 is the empty-view sentinel: MAX() over no rows is NULL, and
            # there is no newest row to be old.
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
            # Each branch aggregates its whole view rather than a windowed
            # slice, which is what makes a stale view report an age at all. It
            # costs a full scan per view on a large deployment, so the budget is
            # declared for that and not for the cheaper windowed form.
            cost=Cost(target="oracle", db_seconds=9.0),
            supplies=frozenset({
                "view", "has_recent_rows", "latest_row_age", "rows_total"}),
            fetch=fetch,
        ),
    ),
)
