"""Shared helpers for the Data Connect reporting datasets.

Every one of these datasets asks the same shape of question -- aggregate an event
view over a bounded window, group it, and take the top N -- so the parts that
must not vary live here: the window bound, the group ceiling, and the truncation
signal.

The truncation signal is the important one. A top-K breakdown that drops its tail
looks exactly like a complete one, which is how "NAD coverage capped at 1000 of
3693" went unnoticed in v2. Every bounded breakdown publishes what it returned
against what existed.

The group ceiling is no longer a constant in this module. It was one, at 5,500,
and a fleet of 5,000 NADs plus 1,000 TACACS accounts needs 6,000 -- so the number
that existed to make truncation rare instead made it certain, silently, at
exactly the size the exporter is built for. It comes from ``config.limits`` now,
derived from the declared scale, and every helper here takes it explicitly so no
statement can be built without saying which ceiling it was built against.
"""
from __future__ import annotations

from prometheus_client import Gauge

from . import snapshots, telemetry
from .labels import label
from .parsing import finite


# Shared across every dataset that publishes marginals, like the coverage
# families in telemetry, so it is written through ctx.set_shared rather than
# declared in one Dataset.metrics.
dimension_populated = Gauge(
    "ise3_breakdown_dimension_populated",
    "A breakdown dimension carried at least one real value in the scan window; "
    "zero means the column exists and is empty, so no series is published for it",
    ["dataset", "dimension"])


def window_hours(interval_seconds, limits):
    """Match the scan to the cadence without exceeding the declared ceiling."""
    try:
        hours = max(1, int(interval_seconds) // 3600)
    except (TypeError, ValueError):
        hours = 1
    return max(1, min(limits.window_hours, hours))


def scan_window(ctx):
    """Hours of history one collection must cover at its effective cadence.

    Widened by the configured interval, never narrowed by it: lengthening a
    cadence without lengthening the window publishes a fraction of the events as
    if it were all of them, while shortening one must not shrink a window whose
    length is the question being asked -- 70 minutes of quiet is not a dead
    switch just because nad_health was set to run hourly.
    """
    interval = max(int(getattr(ctx, "interval", 0) or 0),
                   int(ctx.dataset.default_interval))
    return window_hours(interval, ctx.limits)


def recent(column, hours, limits):
    """An index-friendly lower bound built from a validated integer."""
    hours = max(1, min(limits.window_hours, int(hours)))
    return (f"{column} >= CAST(SYSTIMESTAMP - "
            f"NUMTODSINTERVAL({hours}, 'HOUR') AS TIMESTAMP)")


def recent_epoch(column, hours, limits):
    """The same bound for a view that times on a NUMBER of epoch seconds.

    Cisco's TACACS two-day views carry ``EPOCH_TIME`` rather than a timestamp,
    so the comparison is arithmetic. It lives beside ``recent`` because the two
    are one decision -- how far back any statement may scan -- and a second copy
    somewhere else is how a window ceiling stops applying to half the views.
    """
    hours = max(1, min(limits.window_hours, int(hours)))
    return (f"{column} >= (CAST(SYSTIMESTAMP AS DATE) - DATE '1970-01-01') "
            f"* 86400 - {hours * 3600}")


def group_limit(limits, limit=0):
    """Resolve a statement's row limit against the declared group ceiling.

    A dataset may ask for fewer -- a paged scan does -- but never for more: the
    ceiling is what the transport's row ceiling was sized against, and the two
    are derived together precisely so they cannot drift apart.
    """
    ceiling = limits.group_ceiling
    if not limit:
        return ceiling
    return max(1, min(ceiling, int(limit)))


def marginals(source, where, dimensions, measures="COUNT(*) AS events", *,
              limits, limit=0):
    """Every one-dimensional breakdown of a view, from a single scan.

    Grouping on a cross product is what makes a breakdown unaffordable: errors
    by (code x NAD x method x PSN) has tens of thousands of combinations, so any
    survivable row cap keeps a small and arbitrary fraction of it. The marginals
    are what a dashboard actually reads, and their cardinalities add rather than
    multiply -- roughly 5,000 NADs plus a few hundred codes plus a handful of
    methods and PSNs, which fits one statement. That sum is exactly what
    ``limits.group_ceiling`` is derived from.

    ``GROUPING SETS`` computes them all in one pass, so this is also cheaper than
    the capped cross product it replaces. Each row names its own dimension, and
    exactly one dimension column is non-NULL per row, which is what COALESCE
    picks out.

    v2 learned the same thing from the other end: its identity-summary panel
    showed four dimensions re-partitioning one total and reading as if they were
    additive. They are marginals -- comparable within a dimension, never across.
    """
    dimensions = tuple(dimensions)
    cases = " ".join(
        f"WHEN GROUPING({expression}) = 0 THEN '{name}'"
        for name, expression in dimensions)
    # COALESCE needs at least two arguments (ORA-00938), and a single-dimension
    # probe is a legitimate shape -- posture conditions have exactly one.
    if len(dimensions) == 1:
        coalesced = dimensions[0][1]
    else:
        coalesced = "COALESCE(" + ", ".join(
            expression for _name, expression in dimensions) + ")"
    sets = ", ".join(f"({expression})" for _name, expression in dimensions)
    return f"""
        SELECT dimension, value, {_measure_names(measures)}, group_total
        FROM (
            SELECT CASE {cases} ELSE 'total' END AS dimension,
                   NVL({coalesced}, 'unknown') AS value,
                   {measures},
                   COUNT(*) OVER () AS group_total
            FROM {source}
            WHERE {where}
            GROUP BY GROUPING SETS ({sets})
        )
        ORDER BY dimension, value
        FETCH FIRST {group_limit(limits, limit)} ROWS ONLY
    """


def _measure_names(measures):
    """The aliases a measure clause defines, for the outer projection."""
    names = []
    for part in str(measures).split(","):
        chunk = part.strip().rsplit(" AS ", 1)
        if len(chunk) == 2:
            names.append(chunk[1].strip())
    return ", ".join(names) or "events"


def by_dimension(rows):
    """Group marginal rows by the dimension each one belongs to."""
    grouped = {}
    for row in rows:
        grouped.setdefault(str(row.get("dimension") or "unknown"), []).append(row)
    return grouped


# The placeholders every marginal substitutes for NULL. A dimension whose rows
# carry only these carries no values at all.
PLACEHOLDER_VALUES = frozenset({"", "unknown", "none"})


def live_dimensions(ctx, rows):
    """Marginal rows by dimension, minus the dimensions that hold no values.

    A column can exist, pass every schema presence check, and still be NULL on
    every row -- ISE 3.3 P11 ships four of them in these views. The marginal then
    collapses to a single ``unknown`` group carrying the dataset total, which
    reads as a breakdown and is not one. Withholding those series and publishing
    the absence instead is the difference between "no endpoints in this identity
    group" and "this appliance does not populate identity groups".

    A dimension that returned no rows at all is left unpublished: an empty window
    is no evidence either way, and calling it dead would be its own invention.
    """
    live = {}
    for dimension, entries in by_dimension(rows).items():
        populated = any(
            str(row.get("value") or "").strip().lower() not in PLACEHOLDER_VALUES
            for row in entries)
        ctx.set_shared(dimension_populated, int(populated),
                       dataset=ctx.dataset.name, dimension=dimension)
        if populated:
            live[dimension] = entries
    return live


def top_groups(selected, source, where, group_by, order_by, *, limits, limit=0):
    """A bounded top-K aggregate that also reports the full group count.

    The window functions are what make truncation detectable: COUNT(*) OVER ()
    is the number of groups that existed, computed before the row limit applies.
    """
    return f"""
        SELECT grouped.*, COUNT(*) OVER () AS group_total
        FROM (
            SELECT {selected}
            FROM {source}
            WHERE {where}
            GROUP BY {group_by}
        ) grouped
        ORDER BY {order_by} DESC
        FETCH FIRST {group_limit(limits, limit)} ROWS ONLY
    """


def publish_coverage(ctx, breakdown, returned, total):
    """Export returned-versus-existing for one bounded breakdown.

    The one call every bound goes through, whether the tail was dropped by the
    database (a FETCH FIRST that filled) or by us (a declared top-K published
    from a complete result). Both are the same fact to whoever reads the panel:
    this breakdown is not all of them, and here is how many it is not.
    """
    returned, total = int(returned), max(int(total), int(returned))
    dataset = ctx.dataset.name
    # Through the publication, not straight to the registry: coverage describes
    # the rows this attempt is about to publish, so a discarded attempt must not
    # leave it describing a snapshot that was rolled back.
    ctx.set_shared(telemetry.topk_groups_returned, returned,
                   dataset=dataset, breakdown=breakdown)
    ctx.set_shared(telemetry.topk_groups_total, total,
                   dataset=dataset, breakdown=breakdown)
    ctx.set_shared(telemetry.topk_truncated, int(total > returned),
                   dataset=dataset, breakdown=breakdown)
    return total > returned


def forget_coverage(dataset_name):
    """Drop one dataset's coverage series when it changes source.

    The families are keyed on (dataset, breakdown) with no provider label, and a
    fallback provider need not publish coverage at all -- endpoint_inventory's
    ers and pxgrid providers do not. Left alone, the departed source's numbers
    stay beside a breakdown of a different size and read as current.
    """
    with snapshots.snapshot_lock:
        for family in (telemetry.topk_groups_returned, telemetry.topk_groups_total,
                       telemetry.topk_truncated):
            names = tuple(getattr(family, "_labelnames", ()))
            if "dataset" not in names:
                continue
            index = names.index("dataset")
            for key in [labels for labels in getattr(family, "_metrics", {})
                        if labels[index] == dataset_name]:
                family._metrics.pop(key, None)


def publish_truncation(ctx, breakdown, rows):
    """Coverage for a statement that carries its own ``group_total``."""
    returned = len(rows)
    total = returned
    if rows:
        total = max(returned, int(finite(rows[0].get("group_total"), returned)))
    return publish_coverage(ctx, breakdown, returned, total)


def group(row, *names, default="unknown"):
    """Bounded label values for one grouped row."""
    return tuple(label(row.get(name), default) for name in names)
