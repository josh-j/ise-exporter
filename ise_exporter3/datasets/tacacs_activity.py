"""TACACS authentication, authorization, accounting, and command activity.

Data Connect only, and deliberately separate from ``tacacs_config``: the two
describe the same feature but one is configuration and the other is activity, and
they must never substitute for one another.

Complete rather than top-K *in the scan*: each view is reduced to its marginals
in one pass, so every account and every device is counted rather than the busiest
thousand. Pass and fail are **measures**, not a dimension, so status cannot
multiply the group count.

Publication is where this dataset differs from its siblings, and the reason is
arithmetic. Both of its dimensions are large -- roughly one group per Device
Administration account plus one per device doing Device Admin -- so a fleet of
5,000 NADs and 1,000 accounts is 6,000 groups per statement, and a series per
device across three statements is about 25,000 series for this dataset alone.
That is the second-largest metric family in the exporter, answering a question
nobody asks per switch: a Device Admin dashboard wants "who logged in to what,
and what failed", not one time series per access switch.

So the device dimension is bounded, and *how* it is bounded is an operator
decision declared in ``datasets.tacacs_activity.options`` -- never a constant in
this file:

    device_rollup   publish device activity per ops owner (about a dozen series)
    top_devices     how many individual devices to keep, worst failures first

Both are published from the same complete scan, and the per-device breakdown
always exports ``ise3_topk_groups_returned`` against ``ise3_topk_groups_total``
with ``ise3_topk_truncated``, so a panel showing 200 devices out of 5,000 says
so. Setting ``top_devices = 0`` publishes every device and warns at start with
the series count it implies.

The ops-owner rollup reads ``nad_directory``, which ``network_devices`` fills
from ERS groups over its first several cycles. Until it converges the rollup
attributes most devices to ``unknown`` -- correct, and visible in
``ise3_nad_directory_entries``, but worth knowing when reading a cold start.

Cisco's TACACS views retain two days. An ``EPOCH_TIME`` lower bound is applied
before grouping, otherwise the view's retention rather than the configured window
becomes the scan size -- a 48-hour scan every six hours by accident.
"""
from prometheus_client import Gauge

from .. import nad_directory, reporting
from ..labels import label
from ..model import Cost, Dataset, Option, Provider
from ..parsing import finite


authentications = Gauge(
    "ise3_tacacs_authentications", "TACACS authentications by outcome",
    ["provider", "dimension", "value", "status"])
authorizations = Gauge(
    "ise3_tacacs_authorizations", "TACACS authorizations by outcome",
    ["provider", "dimension", "value", "status"])
commands = Gauge(
    "ise3_tacacs_commands", "TACACS accounting records",
    ["provider", "dimension", "value"])
active_accounts = Gauge(
    "ise3_tacacs_active_accounts", "Distinct accounts with TACACS activity",
    ["provider"])

_METRICS = (authentications, authorizations, commands, active_accounts)

AUTHENTICATION_VIEW = "tacacs_authentication_last_two_days"
AUTHORIZATION_VIEW = "tacacs_authorization_last_two_days"
ACCOUNTING_VIEW = "tacacs_accounting_last_two_days"

# The TACACS views say device_name, while RADIUS_ERRORS_VIEW says
# network_device_name. Verified against the live 3.3 P11 catalogue rather than
# assumed to be consistent -- they are not.
IDENTITY_DIMENSIONS = (
    ("username", "NVL(username, 'unknown')"),
    ("device", "NVL(device_name, 'unknown')"),
)
COMMAND_DIMENSIONS = IDENTITY_DIMENSIONS + (
    # The accounting view calls it COMMAND. The first word is the family; the
    # full command line is operator input and must never become a label.
    ("command_family", "NVL(REGEXP_SUBSTR(command, '^[^ ]+'), 'unknown')"),
)

# Status as a measure pair, so it adds to the row count instead of multiplying it.
OUTCOME_MEASURES = (
    "SUM(CASE WHEN UPPER(NVL(status, '')) LIKE 'PASS%' THEN 1 ELSE 0 END) AS passed, "
    "SUM(CASE WHEN UPPER(NVL(status, '')) LIKE 'PASS%' THEN 0 ELSE 1 END) AS failed"
)

# Series per device across the three statements: passed and failed for both
# authentications and authorizations, plus one accounting series.
SERIES_PER_DEVICE = 5


def _top_devices_danger(value, scale):
    if value:
        return ""
    return (
        "datasets.tacacs_activity.options.top_devices = 0 publishes one series "
        f"per device: about {scale.nads * SERIES_PER_DEVICE:,} series at the "
        f"declared {scale.nads:,} NADs, which is the largest single metric "
        "family in the exporter. Nothing is truncated and nothing is hidden -- "
        "this is only worth knowing before it lands in your snapshot")


def _device_rollup_danger(value, scale):
    if value:
        return ""
    return (
        "datasets.tacacs_activity.options.device_rollup = false drops the "
        "per-ops-owner view of Device Admin activity; with a top_devices bound "
        "in force, devices outside it are then counted in no breakdown at all")


OPTIONS = (
    Option(
        name="device_rollup",
        default=True,
        description="publish TACACS device activity per ops owner, joined "
                    "through the NAD directory that network_devices fills",
        danger=_device_rollup_danger,
    ),
    Option(
        name="top_devices",
        default=200,
        minimum=0,
        maximum=100_000,
        description="individual devices to publish, worst failures first; "
                    "0 publishes every device",
        danger=_top_devices_danger,
    ),
)


def _epoch_bound(hours, limits):
    """These views expose a numeric epoch rather than a timestamp column."""
    hours = max(1, min(limits.window_hours, int(hours)))
    return (f"epoch_time >= (CAST(SYSTIMESTAMP AS DATE) - DATE '1970-01-01') "
            f"* 86400 - {hours * 3600}")


def statements(hours, limits):
    bound = _epoch_bound(hours, limits)
    return {
        "authentications": reporting.marginals(
            AUTHENTICATION_VIEW, bound, IDENTITY_DIMENSIONS, OUTCOME_MEASURES,
            limits=limits),
        "authorizations": reporting.marginals(
            AUTHORIZATION_VIEW, bound, IDENTITY_DIMENSIONS, OUTCOME_MEASURES,
            limits=limits),
        "commands": reporting.marginals(
            ACCOUNTING_VIEW, bound, COMMAND_DIMENSIONS, limits=limits),
    }


def _outcome_of(row):
    """(passed, failed) for a row from either measure shape.

    The accounting view has no verdict, so its events count as neither -- which
    is what makes "worst failures first" fall back to "busiest first" there
    instead of ranking every command row equally at zero.
    """
    if "passed" in row or "failed" in row:
        return finite(row.get("passed")), finite(row.get("failed"))
    return finite(row.get("events")), 0.0


def rank_devices(rows, keep):
    """Order device rows worst-first and keep the declared number of them.

    Failures first, then total volume, then the device name so the set is stable
    between collections -- a top-K that reshuffles on ties makes every panel look
    like something changed. ``keep`` of 0 means keep all of them.
    """
    ordered = sorted(
        rows,
        key=lambda row: (
            -_outcome_of(row)[1],
            -sum(_outcome_of(row)),
            str(row.get("value") or "")))
    return ordered if not keep else ordered[:keep]


def rollup_by_owner(rows, directory):
    """Sum device rows onto the ops owner each device belongs to."""
    totals = {}
    for row in rows:
        owner = label(directory.ops_owner(row.get("value")))
        passed, failed = _outcome_of(row)
        carried = totals.setdefault(owner, [0.0, 0.0])
        carried[0] += passed
        carried[1] += failed
    return totals


def _publish(ctx, gauge, dimension, value, passed, failed):
    if gauge is commands:
        ctx.set(gauge, passed, dimension=dimension, value=value)
        return
    ctx.set(gauge, passed, dimension=dimension, value=value, status="passed")
    ctx.set(gauge, failed, dimension=dimension, value=value, status="failed")


def _publish_devices(ctx, gauge, breakdown, rows, directory, rollup, keep):
    """The one bounded breakdown in this dataset, and everything it must say."""
    if rollup:
        for owner, (passed, failed) in rollup_by_owner(rows, directory).items():
            _publish(ctx, gauge, "ops_owner", owner, passed, failed)

    kept = rank_devices(rows, keep)
    reporting.publish_coverage(ctx, f"{breakdown}.device", len(kept), len(rows))
    for row in kept:
        (value,) = reporting.group(row, "value")
        passed, failed = _outcome_of(row)
        _publish(ctx, gauge, "device", value, passed, failed)


def fetch(ctx):
    hours = reporting.window_hours(ctx.dataset.default_interval, ctx.limits)
    results = ctx.transport.query_many(statements(hours, ctx.limits))
    rollup = ctx.option("device_rollup")
    keep = ctx.option("top_devices")
    directory = nad_directory.shared()
    accounts = set()

    for key, gauge in (("authentications", authentications),
                       ("authorizations", authorizations),
                       ("commands", commands)):
        rows = results.get(key, [])
        # Whether the *scan* truncated -- a separate fact from the device bound
        # below, and the one that says the group ceiling is too small for this
        # estate rather than that we chose to publish fewer series.
        reporting.publish_truncation(ctx, key, rows)

        grouped = reporting.by_dimension(rows)
        _publish_devices(ctx, gauge, key, grouped.get("device", []), directory,
                         rollup, keep)

        for dimension, entries in grouped.items():
            if dimension == "device":
                continue
            for row in entries:
                (value,) = reporting.group(row, "value")
                if dimension == "username":
                    accounts.add(value)
                passed, failed = _outcome_of(row)
                _publish(ctx, gauge, dimension, value, passed, failed)

    ctx.set(active_accounts, len(accounts))


DATASET = Dataset(
    name="tacacs_activity",
    description="TACACS authentication, authorization, accounting, commands",
    default_interval=21600,
    metrics=_METRICS,
    options=OPTIONS,
    providers=(
        Provider(
            name="dataconnect",
            cost=Cost(target="oracle", db_seconds=6.0),
            supplies=frozenset({
                "username", "device", "ops_owner", "command_family", "status"}),
            requires=("view:TACACS_AUTHENTICATION", "view:TACACS_ACCOUNTING"),
            fetch=fetch,
        ),
    ),
)
