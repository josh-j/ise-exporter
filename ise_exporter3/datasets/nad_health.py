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
zero and a never-seen marker rather than omitted.

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

_METRICS = (authentications, last_seen_age_seconds, silent_total, covered)

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


def fetch(ctx):
    hours = reporting.window_hours(ctx.dataset.default_interval, ctx.limits)

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
    directory = nad_directory.shared()
    if not len(directory):
        # Without the inventory there is no set to be silent *against*, and the
        # loop below would fall through to `silent_total = 0` -- "no dead
        # switches" -- which is the confident wrong answer this exporter exists
        # to avoid. Found on the lab: at a cold start this dataset runs before
        # network_devices has filled the directory, and published two series
        # against 504 configured NADs.
        #
        # Failing keeps the previous snapshot and names the reason.
        # `dependency_pending` is deliberately not a slow-retry reason, so the
        # scheduler comes back in a minute rather than in fifteen, and the very
        # first inventory collection unblocks it.
        ctx.fail("dependency_pending",
                 "the NAD directory is empty; network_devices has not published "
                 "an inventory yet, so silence cannot be distinguished from an "
                 "unconfigured switch")

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
    metrics=_METRICS,
    providers=(
        Provider(
            name="dataconnect",
            # One paged scan, plus a conditional second page only when the exact
            # group total shows the first did not cover the fleet.
            cost=Cost(target="oracle", db_seconds=8.0, scales_with="nads",
                      db_seconds_per_1k=0.5),
            supplies=frozenset({"nad", "last_seen", "activity", "silent"}),
            requires=("view:RADIUS_AUTHENTICATION_SUMMARY",),
            fetch=fetch,
        ),
    ),
)
