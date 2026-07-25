"""Shared helpers for the Data Connect reporting datasets.

Every one of these datasets asks the same shape of question -- aggregate an event
view over a bounded window, group it, and take the top N -- so the parts that
must not vary live here: the window bound, the group ceiling, and the truncation
signal.

The truncation signal is the important one. A top-K breakdown that drops its tail
looks exactly like a complete one, which is how "NAD coverage capped at 1000 of
3693" went unnoticed in v2. Every bounded breakdown publishes what it returned
against what existed.
"""
from __future__ import annotations

from . import telemetry
from .labels import label
from .parsing import finite


# Safety valve, not a design target. Statements below are shaped so that a
# deployment at the declared scale returns every group; this cap exists so a
# fleet far past that scale degrades loudly (truncated = 1) instead of tripping
# the transport's hard row ceiling and taking the dataset down.
MAX_GROUPS = 5500

# Absolute ceiling on how far back any event statement may scan. The exporter
# samples a bounded window; it never aggregates the whole interval between runs.
MAX_WINDOW_HOURS = 6


def window_hours(interval_seconds, ceiling=MAX_WINDOW_HOURS):
    """Match the scan to the cadence without exceeding the hard ceiling."""
    try:
        hours = max(1, int(interval_seconds) // 3600)
    except (TypeError, ValueError):
        hours = 1
    return max(1, min(ceiling, hours))


def recent(column, hours):
    """An index-friendly lower bound built from a validated integer."""
    hours = max(1, min(MAX_WINDOW_HOURS, int(hours)))
    return (f"{column} >= CAST(SYSTIMESTAMP - "
            f"NUMTODSINTERVAL({hours}, 'HOUR') AS TIMESTAMP)")


def marginals(source, where, dimensions, measures="COUNT(*) AS events",
              limit=MAX_GROUPS):
    """Every one-dimensional breakdown of a view, from a single scan.

    Grouping on a cross product is what makes a breakdown unaffordable: errors
    by (code x NAD x method x PSN) has tens of thousands of combinations, so any
    survivable row cap keeps a small and arbitrary fraction of it. The marginals
    are what a dashboard actually reads, and their cardinalities add rather than
    multiply -- roughly 5,000 NADs plus a few hundred codes plus a handful of
    methods and PSNs, which fits one statement.

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
    limit = max(1, min(MAX_GROUPS, int(limit)))
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
        FETCH FIRST {limit} ROWS ONLY
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


def top_groups(selected, source, where, group_by, order_by, limit=MAX_GROUPS):
    """A bounded top-K aggregate that also reports the full group count.

    The window functions are what make truncation detectable: COUNT(*) OVER ()
    is the number of groups that existed, computed before the row limit applies.
    """
    limit = max(1, min(MAX_GROUPS, int(limit)))
    return f"""
        SELECT grouped.*, COUNT(*) OVER () AS group_total
        FROM (
            SELECT {selected}
            FROM {source}
            WHERE {where}
            GROUP BY {group_by}
        ) grouped
        ORDER BY {order_by} DESC
        FETCH FIRST {limit} ROWS ONLY
    """


def publish_truncation(ctx, breakdown, rows, limit=MAX_GROUPS):
    """Export returned-versus-existing for one bounded breakdown."""
    returned = len(rows)
    total = returned
    if rows:
        total = max(returned, int(finite(rows[0].get("group_total"), returned)))
    dataset = ctx.dataset.name
    telemetry.topk_groups_returned.labels(
        dataset=dataset, breakdown=breakdown).set(returned)
    telemetry.topk_groups_total.labels(
        dataset=dataset, breakdown=breakdown).set(total)
    telemetry.topk_truncated.labels(
        dataset=dataset, breakdown=breakdown).set(int(total > returned))
    return total > returned


def group(row, *names, default="unknown"):
    """Bounded label values for one grouped row."""
    return tuple(label(row.get(name), default) for name in names)
