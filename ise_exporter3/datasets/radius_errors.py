"""RADIUS error aggregates by message code, NAD, method, and PSN.

Data Connect only. Kept separate from ``radius_reporting`` because the error view
is sparse: joining it into the authentication scan makes failure panels look
empty when the view simply has no rows for the window, which is a different
statement from "there were no failures".

**Complete, not top-K.** Grouping on (code x NAD x method x PSN) produces tens of
thousands of combinations, so any survivable row cap keeps an arbitrary fraction
of it -- and the tail is exactly where a failing switch hides. The marginals are
what a dashboard reads and their cardinalities add rather than multiply, so one
GROUPING SETS pass returns every one of them.

``MESSAGE_CODE`` is an Oracle NUMBER. Defaulting it with ``NVL(code, 'unknown')``
makes Oracle apply TO_NUMBER to the literal and the statement dies with
ORA-01722 the moment a NULL appears, so the cast goes the other way round.
"""
from prometheus_client import Gauge

from .. import reporting
from ..model import Cost, Dataset, Provider
from ..parsing import finite


errors_total = Gauge(
    "ise3_radius_errors_total", "RADIUS errors in the scan window", ["provider"])
by_message_code = Gauge(
    "ise3_radius_errors_by_message_code", "RADIUS errors per ISE message code",
    ["provider", "message_code"])
by_nad = Gauge(
    "ise3_radius_errors_by_nad", "RADIUS errors per NAD", ["provider", "nad"])
by_method = Gauge(
    "ise3_radius_errors_by_method", "RADIUS errors per authentication method",
    ["provider", "method"])
by_psn = Gauge(
    "ise3_radius_errors_by_psn", "RADIUS errors per PSN", ["provider", "psn"])

_METRICS = (errors_total, by_message_code, by_nad, by_method, by_psn)

VIEW = "radius_errors_view"

DIMENSIONS = (
    # TO_CHAR first: MESSAGE_CODE is a NUMBER, so defaulting it to a string
    # makes Oracle coerce the column and fail on the first NULL.
    ("message_code", "NVL(TO_CHAR(message_code), 'unknown')"),
    # RADIUS_ERRORS_VIEW says network_device_name; the TACACS views say
    # device_name. Verified against the live 3.3 P11 catalogue, not assumed.
    ("nad", "NVL(network_device_name, 'unknown')"),
    ("method", "NVL(authentication_method, 'unknown')"),
    ("psn", "NVL(ise_node, 'unknown')"),
)

GAUGES = {
    "message_code": (by_message_code, "message_code"),
    "nad": (by_nad, "nad"),
    "method": (by_method, "method"),
    "psn": (by_psn, "psn"),
}


def statements(hours, limits):
    recent = reporting.recent("timestamp", hours, limits)
    return {
        "totals": f"SELECT COUNT(*) AS events FROM {VIEW} WHERE {recent}",
        "marginals": reporting.marginals(VIEW, recent, DIMENSIONS, limits=limits),
    }


def fetch(ctx):
    hours = reporting.window_hours(ctx.dataset.default_interval, ctx.limits)
    results = ctx.transport.query_many(statements(hours, ctx.limits))

    for row in results.get("totals", []):
        ctx.set(errors_total, finite(row.get("events")))

    rows = results.get("marginals", [])
    reporting.publish_truncation(ctx, "marginals", rows)
    for dimension, entries in reporting.by_dimension(rows).items():
        target = GAUGES.get(dimension)
        if target is None:
            continue
        gauge, label_name = target
        for row in entries:
            (value,) = reporting.group(row, "value")
            ctx.set(gauge, finite(row.get("events")), **{label_name: value})


DATASET = Dataset(
    name="radius_errors",
    description="RADIUS error aggregates by code, NAD, method, PSN",
    default_interval=1800,
    metrics=_METRICS,
    providers=(
        Provider(
            name="dataconnect",
            cost=Cost(target="oracle", db_seconds=4.0),
            supplies=frozenset({"message_code", "nad", "method", "psn"}),
            requires=("view:RADIUS_ERRORS_VIEW",),
            fetch=fetch,
        ),
    ),
)
