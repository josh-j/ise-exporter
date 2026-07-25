"""TACACS authentication, authorization, accounting, and command activity.

Data Connect only, and deliberately separate from ``tacacs_config``: the two
describe the same feature but one is configuration and the other is activity, and
they must never substitute for one another.

Complete rather than top-K: each view is reduced to its marginals in one pass, so
every account and every device appears rather than the busiest thousand. Pass and
fail are **measures**, not a dimension, so status cannot multiply the group count.

Cisco's TACACS views retain two days. An ``EPOCH_TIME`` lower bound is applied
before grouping, otherwise the view's retention rather than the configured window
becomes the scan size -- a 48-hour scan every six hours by accident.
"""
from prometheus_client import Gauge

from .. import reporting
from ..model import Cost, Dataset, Provider
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


def _epoch_bound(hours):
    """These views expose a numeric epoch rather than a timestamp column."""
    hours = max(1, min(reporting.MAX_WINDOW_HOURS, int(hours)))
    return (f"epoch_time >= (CAST(SYSTIMESTAMP AS DATE) - DATE '1970-01-01') "
            f"* 86400 - {hours * 3600}")


def statements(hours):
    bound = _epoch_bound(hours)
    return {
        "authentications": reporting.marginals(
            AUTHENTICATION_VIEW, bound, IDENTITY_DIMENSIONS, OUTCOME_MEASURES),
        "authorizations": reporting.marginals(
            AUTHORIZATION_VIEW, bound, IDENTITY_DIMENSIONS, OUTCOME_MEASURES),
        "commands": reporting.marginals(
            ACCOUNTING_VIEW, bound, COMMAND_DIMENSIONS),
    }


def fetch(ctx):
    hours = reporting.window_hours(ctx.dataset.default_interval)
    results = ctx.transport.query_many(statements(hours))
    accounts = set()

    for key, gauge in (("authentications", authentications),
                       ("authorizations", authorizations)):
        rows = results.get(key, [])
        reporting.publish_truncation(ctx, key, rows)
        for row in rows:
            dimension, value = reporting.group(row, "dimension", "value")
            if dimension == "username":
                accounts.add(value)
            ctx.set(gauge, finite(row.get("passed")),
                    dimension=dimension, value=value, status="passed")
            ctx.set(gauge, finite(row.get("failed")),
                    dimension=dimension, value=value, status="failed")

    rows = results.get("commands", [])
    reporting.publish_truncation(ctx, "commands", rows)
    for row in rows:
        dimension, value = reporting.group(row, "dimension", "value")
        if dimension == "username":
            accounts.add(value)
        ctx.set(commands, finite(row.get("events")),
                dimension=dimension, value=value)

    ctx.set(active_accounts, len(accounts))


DATASET = Dataset(
    name="tacacs_activity",
    description="TACACS authentication, authorization, accounting, commands",
    default_interval=21600,
    metrics=_METRICS,
    providers=(
        Provider(
            name="dataconnect",
            cost=Cost(target="oracle", db_seconds=6.0, max_rows=6000),
            supplies=frozenset({
                "username", "device", "policy", "command_family", "status"}),
            requires=("view:TACACS_AUTHENTICATION", "view:TACACS_ACCOUNTING"),
            fetch=fetch,
        ),
    ),
)
