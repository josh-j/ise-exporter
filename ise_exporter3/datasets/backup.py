"""Configuration and operational backup status.

OpenAPI only.

An ISE that has never been backed up returns 200 with every one of the response's
fields as explicit JSON null -- status and startDate included -- rather than
omitting them. That is a real answer, not a failure: the dataset publishes
``configured = 0`` rather than erroring, because "no backup is configured" is
exactly what an operator needs to see and an error would hide it behind a failure
reason.

In that state the timestamp, age and status series are deliberately not
published at all. An absent series says "never answered"; a zero would say "last
backup at the epoch, zero hours old", which is a different and false claim.
"""
from datetime import datetime, timezone

from prometheus_client import Gauge

from ..model import Cost, Dataset, Provider
from ..parsing import parse_ise_date


configured = Gauge(
    "ise3_backup_configured", "A backup schedule exists on this deployment",
    ["provider"])
last_success_timestamp = Gauge(
    "ise3_backup_last_success_timestamp", "Unix time of the last successful backup",
    ["provider"])
age_hours = Gauge(
    "ise3_backup_age_hours", "Hours since the last successful backup", ["provider"])
last_status = Gauge(
    "ise3_backup_last_status", "Status of the most recent backup attempt",
    ["provider", "status"])

_METRICS = (configured, last_success_timestamp, age_hours, last_status)

_STATUSES = ("COMPLETED", "ERROR", "IN_PROGRESS")
_MAX_FUTURE_SKEW_SECONDS = 300


def fetch(ctx):
    payload = ctx.transport.get_openapi(
        "/backup-restore/config/last-backup-status", api="pan_backup")
    if not isinstance(payload, dict):
        ctx.fail("invalid_response", "backup status was not an object")

    status = payload.get("status")
    started = payload.get("startDate")
    if not status and not started:
        ctx.set(configured, 0)
        return

    if not isinstance(status, str) or status not in _STATUSES:
        ctx.fail("invalid_response", f"backup reported unknown status {status!r}")

    ctx.set(configured, 1)
    for known in _STATUSES:
        ctx.set(last_status, int(known == status), status=known)

    if status != "COMPLETED":
        return

    if not started:
        ctx.fail("invalid_response", "a completed backup returned no startDate")
    finished = parse_ise_date(started)
    if finished is None:
        ctx.fail("invalid_response", "a completed backup returned an invalid startDate")
    now = datetime.now(timezone.utc).timestamp()
    if finished.timestamp() > now + _MAX_FUTURE_SKEW_SECONDS:
        ctx.fail("invalid_response", "a completed backup returned a future startDate")

    ctx.set(last_success_timestamp, finished.timestamp())
    ctx.set(age_hours, max(0.0, (now - finished.timestamp()) / 3600))


DATASET = Dataset(
    name="backup",
    description="Configuration and operational backup status",
    default_interval=21600,
    metrics=_METRICS,
    providers=(
        Provider(
            name="openapi",
            cost=Cost(target="pan", requests=1),
            supplies=frozenset({"last_backup", "status"}),
            fetch=fetch,
        ),
    ),
)
