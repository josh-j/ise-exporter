"""Per-view recency probe across the Data Connect reporting views.

Answers "is this view receiving rows at all", which is the difference between a
legitimately empty panel and a broken feed -- the question every other reporting
dataset depends on and none of them can answer about itself.

One statement per view would mean a paced wait each, holding the serialized lane
for many minutes. One `UNION ALL` would mean a slow branch blowing the whole
dataset's timeout. So the probe runs as a tolerant batch of unions: a statement
that dies damages only the three views it carries, the others still publish,
and `ise3_source_probed` names the views whose statement did not answer -- a
diagnostic dataset that goes completely dark because one view is slow is
silent exactly when it is needed.

Every fact is read off one `MAX(column)` aggregate per view -- the same shape
nad_health uses for its source age, and for the same reason: the optimiser
answers it from the timestamp index instead of scanning the view. The probe
used to also take `COUNT(*)` and a windowed count, which forced a full scan of
each view; at production row counts three of those in one statement cannot fit
the fixed statement budget, and the probe timed out on exactly the deployments
it exists to reassure.

Three states have to stay distinguishable, because the operator's next action
differs for each: a view that is missing (no series), a view that exists and is
empty (has_rows 0, no age), and a view that holds rows but has stopped receiving
them (has_rows 1, age far above the window). Emptiness and recency both derive
from the newest row: a view is empty when MAX() is NULL, and it is recent when
its newest row is younger than the scan window.
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
has_rows = Gauge(
    "ise3_source_has_rows",
    "A reporting view holds at least one row. Zero with no age is an empty "
    "view; one with a large age is a stale feed; neither is a missing view",
    ["provider", "view"])
probed = Gauge(
    "ise3_source_probed",
    "The freshness probe answered for this view inside its statement budget. "
    "Zero means the statement carrying this view failed -- missing, erroring, "
    "or too slow to aggregate -- and the view's other freshness series are "
    "absent because they are unknown, not because the view is",
    ["provider", "view"])

_METRICS = (has_recent_rows, latest_row_age_seconds, has_rows, probed)

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


def _branch(view, column):
    # The age is deliberately NOT taken under a window predicate. Computing the
    # newest row inside the same window that decides whether the view is recent
    # means a stale view reports no age at all -- exactly the view an operator
    # is asking about. The explicit CASE keeps emptiness separate from the -1
    # sentinel, which a future-dated newest row could otherwise collide with.
    return f"""
        SELECT '{view}' AS view_name,
               CASE WHEN MAX({column}) IS NULL THEN 0 ELSE 1 END AS has_rows,
               NVL(
                 (CAST(SYSTIMESTAMP AS DATE) - CAST(MAX({column}) AS DATE)) * 86400,
                 -1) AS age_seconds
        FROM {view}
    """


def statements():
    # The marker routes these statements to the freshness_probe telemetry
    # label, so their cost is not attributed to the first view a batch names.
    return {
        f"batch_{index}": "/* ise_exporter:freshness */\n" + "\nUNION ALL\n".join(
            _branch(view, column) for view, column in batch)
        for index, batch in enumerate(VIEW_BATCHES)
    }


def fetch(ctx):
    window = reporting.scan_window(ctx) * 3600
    results, errors = ctx.transport.query_many(statements(), tolerant=True)
    if not results:
        # Nothing answered, so there is no partial verdict to publish; the
        # first classified error names why for the failure detail.
        raise next(iter(errors.values()))
    for index, batch in enumerate(VIEW_BATCHES):
        answered = f"batch_{index}" in results
        for view, _column in batch:
            ctx.set(probed, int(answered), view=view)
    for rows in results.values():
        for row in rows:
            (view,) = reporting.group(row, "view_name")
            populated = finite(row.get("has_rows")) > 0
            age = finite(row.get("age_seconds"), -1)
            ctx.set(has_rows, int(populated), view=view)
            # A future-dated newest row (negative age) still counts as recent:
            # the view is receiving rows, which is the question asked here.
            ctx.set(has_recent_rows, int(populated and age <= window), view=view)
            if populated:
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
            # Three batched statements of three MAX() aggregates each, every
            # one answerable from the view's timestamp index.
            cost=Cost(target="oracle", db_seconds=1.5),
            supplies=frozenset({
                "view", "has_recent_rows", "latest_row_age", "has_rows",
                "probed"}),
            fetch=fetch,
        ),
    ),
)
