"""Per-NAD authentication activity and dead-switch detection.

Data Connect supplies the activity; ``network_devices`` supplies the
authoritative NAD list to correlate against.

**This dataset pages to completion rather than taking a top-K.** A single top-K
page silently answers "the 1,000 busiest" to a question that was "which switches
went silent" -- and the switches you care about are precisely the ones at the
bottom. That is the documented v2 symptom: *NAD coverage capped at 1000 of 3693*.
A page is one ``limits.group_ceiling``, which is derived to clear the declared
NAD count, and a second page is fetched only when the first page's exact group
total shows there is more -- so the common case stays one statement and an estate
larger than the one declared still gets counted rather than cut off.

Silence is the signal, so a NAD in inventory with no activity is published with
zero and a never-seen marker rather than omitted. Silence is only meaningful
against a feed that is current, so the age of the activity view itself is
published beside it: a whole estate reported silent while
``ise3_nad_activity_source_age_seconds`` exceeds the window is a stale source,
which is a different fault with a different owner.

**This dataset cannot answer without the inventory**, and says so rather than
guessing. Silence is measured against the set of configured NADs, so an empty
directory does not mean "nothing is dead" -- it means the question cannot be
asked yet. A collection that finds no directory fails with `dependency_pending`,
which keeps the previous snapshot and retries within a minute. That ordering is
real rather than theoretical: on a cold start this dataset runs before
`network_devices` has published anything, and it used to report two series and
zero silent switches against an estate of 504.
"""
from prometheus_client import Gauge

from .. import nad_directory, reporting
from ..labels import label
from ..model import Cost, Dataset, Provider
from ..parsing import finite


authentications = Gauge(
    "ise3_nad_authentications", "Authentications attributed to a NAD in the window",
    ["provider", "nad", "status"])
last_seen_age_seconds = Gauge(
    "ise3_nad_last_authentication_age_seconds",
    "Seconds since a NAD last authenticated anything in the scan window",
    ["provider", "nad"])
# Deliberately not published per NAD: silence is exactly
# `ise3_nad_authentications == 0`, which Prometheus computes for free. Emitting
# it as well would duplicate one series per switch -- 5,000 of them at the
# declared scale -- to say something the data already says.
silent_total = Gauge(
    "ise3_nad_silent_total", "Configured NADs with no activity in the window",
    ["provider"])
covered = Gauge(
    "ise3_nad_activity_covered", "NADs the activity scan reached", ["provider"])
source_age_seconds = Gauge(
    "ise3_nad_activity_source_age_seconds",
    "Age of the newest row in the activity view, measured over the whole view "
    "rather than the scan window. A whole estate reported silent while this is "
    "older than the window is a stale feed, not a dead estate; -1 means the "
    "view has never held a row", ["provider"])

_METRICS = (authentications, last_seen_age_seconds, silent_total, covered,
            source_age_seconds)

VIEW = "radius_authentication_summary"


def _page(hours, offset, limits):
    # A page is one group ceiling, which is derived to clear the declared NAD
    # count and sits below the statement row ceiling by construction -- so a
    # page cannot trip the transport, and the second page is only ever paid for
    # on an estate larger than the one declared.
    recent = reporting.recent("timestamp", hours, limits)
    return f"""
        SELECT grouped.*, COUNT(*) OVER () AS group_total
        FROM (
            SELECT NVL(device_name, 'unknown') AS nad,
                   SUM(passed_count) AS passed,
                   SUM(failed_count) AS failed,
                   (CAST(SYSTIMESTAMP AS DATE)
                    - CAST(MAX(timestamp) AS DATE)) * 86400 AS age_seconds
            FROM {VIEW}
            WHERE {recent}
            GROUP BY NVL(device_name, 'unknown')
        ) grouped
        ORDER BY nad
        OFFSET {int(offset)} ROWS FETCH NEXT {reporting.group_limit(limits)} ROWS ONLY
    """


def _source_age():
    """How old the activity feed itself is, unbounded by the scan window.

    Silence measured inside a window says nothing about whether the window has
    anything to measure. On a feed four days behind the appliance clock, every
    configured NAD reports silent at any legal window and the verdict is
    indistinguishable from a genuinely dead estate. MAX() alone so the optimiser
    can answer it from the timestamp index instead of scanning the view.
    """
    return f"""
        SELECT NVL(
                 (CAST(SYSTIMESTAMP AS DATE) - CAST(MAX(timestamp) AS DATE)) * 86400,
                 -1) AS age_seconds
        FROM {VIEW}
    """


def fetch(ctx):
    # Checked BEFORE any Oracle work, not after. This dataset cannot answer
    # without the inventory, and a scan whose result is then discarded is the
    # exact failure the batch-ceiling defect had: paying for the query and
    # aborting anyway. Retrying on a one-minute timer made that a recurring
    # cost rather than a one-off, which is how it was caught -- in production,
    # burning duty cycle every sixty seconds through a cold start.
    directory = nad_directory.shared()
    if not len(directory):
        # Silence is measured against the set of configured NADs, so an empty
        # directory does not mean "nothing is dead" -- it means the question
        # cannot be asked yet. Failing keeps the previous snapshot and names
        # the reason; `dependency_pending` is deliberately not a slow-retry
        # reason, so the first inventory collection unblocks this within a
        # minute.
        ctx.fail("dependency_pending",
                 "the NAD directory is empty; network_devices has not published "
                 "an inventory yet, so silence cannot be distinguished from an "
                 "unconfigured switch")

    hours = reporting.scan_window(ctx)

    for row in ctx.transport.query(_source_age()):
        ctx.set(source_age_seconds, finite(row.get("age_seconds"), -1))

    rows = ctx.transport.query(_page(hours, 0, ctx.limits))
    total_groups = int(finite(rows[0].get("group_total"), len(rows))) if rows else 0
    # Only pay for a second page when the first proved there is more. Ordering by
    # name rather than volume keeps the pages disjoint and stable.
    if total_groups > len(rows):
        rows = rows + ctx.transport.query(_page(hours, len(rows), ctx.limits))

    reporting.publish_truncation(ctx, "by_nad", rows)

    active = {}
    for row in rows:
        (nad,) = reporting.group(row, "nad")
        passed, failed = finite(row.get("passed")), finite(row.get("failed"))
        active[nad] = passed + failed
        ctx.set(authentications, passed, nad=nad, status="passed")
        ctx.set(authentications, failed, nad=nad, status="failed")
        age = finite(row.get("age_seconds"), -1)
        if age >= 0:
            ctx.set(last_seen_age_seconds, age, nad=nad)

    # A NAD that authenticated nothing produces no row at all, so silence has to
    # come from the inventory side. This is the dead-switch signal.
    quiet = 0
    for nad, _location, _owner in directory.classifications():
        nad = label(nad)
        if nad in active:
            continue
        quiet += 1
        # Published as an explicit zero rather than omitted: a NAD that
        # authenticated nothing produces no row at all, and an absent series is
        # indistinguishable from a switch nobody configured.
        ctx.set(authentications, 0, nad=nad, status="passed")
        ctx.set(authentications, 0, nad=nad, status="failed")

    ctx.set(silent_total, quiet)
    ctx.set(covered, len(active))


DATASET = Dataset(
    name="nad_health",
    description="Per-NAD authentication activity and dead-switch detection",
    default_interval=21600,
    windowed=True,
    metrics=_METRICS,
    providers=(
        Provider(
            name="dataconnect",
            # One paged scan, plus a conditional second page only when the exact
            # group total shows the first did not cover the fleet, plus one
            # index-answerable MAX for the age of the feed itself.
            cost=Cost(target="oracle", db_seconds=8.5, scales_with="nads",
                      db_seconds_per_1k=0.5),
            supplies=frozenset({"nad", "last_seen", "activity", "silent"}),
            requires=("view:RADIUS_AUTHENTICATION_SUMMARY",),
            fetch=fetch,
        ),
    ),
)
