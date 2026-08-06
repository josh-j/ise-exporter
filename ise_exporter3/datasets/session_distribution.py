"""Active session count per PSN.

A separate dataset because the planner picks exactly one provider per dataset,
and the preferred source for ``active_sessions`` cannot answer this question:
a pxGrid session record has no owning-PSN field -- ``providers`` is literally
``["None"]`` on a live 3.3 appliance, and no scalar spelling exists either
(docs/DATASETS_FACTS.md 6.3). Housed inside ``active_sessions``, the per-PSN
breakdown either went dark or read every session as ``psn="unknown"`` whenever
pxGrid was the healthy preference, and no PSN-capable source ever ran.

- ``mnt`` is the authoritative cheap answer: the ActiveList is a session index
  that names each session's PSN in ``server``, so one read counts the whole
  fleet exactly.
- ``dataconnect`` reconstructs the same answer from accounting starts minus
  stops -- the same bounded dedup scan ``active_sessions`` runs, grouped by
  PSN alone -- and is the last resort for the same reason it is one there: the
  scan covers the largest table in the MnT database.

pxGrid is deliberately not offered here.
"""
from collections import defaultdict

from prometheus_client import Gauge

from .. import reporting
from ..labels import label
from ..model import Cost, Dataset, Provider
from ..parsing import finite
from .active_sessions import VIEW, has_column, session_scan

by_psn = Gauge(
    "ise3_active_sessions_by_psn", "Active sessions per PSN", ["provider", "psn"])

_METRICS = (by_psn,)

# The fallback statement scans the same accounting window active_sessions does,
# so it carries the same widened per-statement budget; see
# reporting.statement_timeout_option for why the budget is a declared option
# rather than a constant.
OPTIONS = (reporting.statement_timeout_option(45),)


def _publish(ctx, counts, empty):
    # A healthy empty answer has no PSN rows from which to form a labelled
    # series.  Without an explicit zero the PSN panels render the same blank
    # state as a failed/unavailable collector.  Keep the zero inside the data
    # family so delta() also has a stable baseline on quiet deployments.
    if empty:
        ctx.set(by_psn, 0, psn="No active sessions")
    for psn, count in counts.items():
        ctx.set(by_psn, count, psn=psn)


def fetch_mnt(ctx):
    listing = ctx.transport.get_mnt_xml("/Session/ActiveList", api="mnt_active_list")
    sessions = listing.get("sessions") or []
    counts = defaultdict(int)
    for session in sessions:
        # `server` is the PSN column of the session index. A row without one is
        # still a session, so it is counted rather than dropped.
        counts[label(session.get("server"), "unknown")] += 1
    _publish(ctx, counts, empty=not sessions)


def statement(hours, limits, schema=None):
    """Live sessions per PSN from one bounded accounting scan.

    The same reconstruction rule as active_sessions.statement -- the dedup pass
    keeps each session's latest record in the window, and a session whose
    latest record is not a stop is live -- with the cheap aggregation: one
    GROUP BY over the handful of PSNs, no GROUPING SETS and no NAD marginal.
    """
    recent = reporting.recent("timestamp", hours, limits)
    status, _mac, partition, order = session_scan(schema)
    psn = ("NVL(ise_node, 'unknown')" if has_column(schema, "ise_node")
           else "'unknown'")
    return f"""
        SELECT psn, COUNT(*) AS sessions, COUNT(*) OVER () AS group_total
        FROM (
            SELECT {psn} AS psn, {status} AS status,
                   ROW_NUMBER() OVER (
                       PARTITION BY {partition}
                       ORDER BY {order}) AS latest
            FROM {VIEW}
            WHERE {recent}
        )
        WHERE latest = 1 AND status NOT LIKE '%STOP%'
        GROUP BY psn
        ORDER BY sessions DESC, psn
        FETCH FIRST {reporting.group_limit(limits)} ROWS ONLY
    """


def fetch_dataconnect(ctx):
    hours = reporting.scan_window(ctx)
    schema = getattr(ctx.transport, "schema", None)
    results = ctx.transport.query_many(
        {"psn": statement(hours, ctx.limits, schema)},
        timeout=ctx.option("statement_timeout"))
    rows = results.get("psn", [])
    reporting.publish_truncation(ctx, "psn", rows)

    counts = defaultdict(float)
    for row in rows:
        counts[label(row.get("psn"), "unknown")] += finite(row.get("sessions"))
    _publish(ctx, counts, empty=not counts)


DATASET = Dataset(
    name="session_distribution",
    description="Active session count per PSN",
    default_interval=300,
    metrics=_METRICS,
    options=OPTIONS,
    providers=(
        Provider(
            name="mnt",
            # One ActiveList read; the index names each session's PSN directly.
            cost=Cost(target="mnt", requests=1),
            supplies=frozenset({"psn"}),
            fetch=fetch_mnt,
        ),
        Provider(
            name="dataconnect",
            # The same bounded RADIUS_ACCOUNTING dedup scan active_sessions
            # declares at 5.0s. The aggregation here is cheaper, but the scan
            # dominates, so a smaller figure would be an invented saving.
            cost=Cost(target="oracle", db_seconds=5.0),
            supplies=frozenset({"psn"}),
            requires=("view:RADIUS_ACCOUNTING",),
            fetch=fetch_dataconnect,
            notes="reconstructed from accounting; completeness depends on NAD "
                  "start/stop records",
        ),
    ),
)
