"""Known ISE defects that affect what this exporter publishes.

Some ISE metrics are wrong in ways no validation can detect. A range check
catches a negative duration or an absurd one; it cannot catch a latency that is
inflated but plausible, because nothing about the value says so.

Where that is true, the honest move is not to silently republish the number and
not to silently drop it, but to say so next to it. Each defect below is exported
as `ise3_ise_known_defect_info`, so a dashboard can annotate the affected panel
and an operator finds out from the metric rather than from a support case.

Adding one is deliberately cheap. If a defect is later confirmed fixed in the
supported release, delete the entry -- do not leave a stale warning attached to a
number that is now correct.
"""
from __future__ import annotations

from dataclasses import dataclass

from prometheus_client import Gauge


defect_info = Gauge(
    "ise3_ise_known_defect_info",
    "A known ISE defect affecting an exported metric family",
    ["defect", "metric", "impact", "confirmed_on_supported_release"])


@dataclass(frozen=True)
class Defect:
    """One ISE defect and what it does to a metric this exporter publishes."""

    identifier: str
    headline: str
    metrics: tuple
    impact: str
    # Whether the defect is confirmed present on the release this exporter
    # supports. "unconfirmed" is an honest answer and the common one: Cisco's
    # bug tool needs an account, so affected/fixed release lists are frequently
    # unavailable. An unconfirmed defect is still worth surfacing -- it tells
    # an operator which number to distrust first.
    confirmed_on_supported_release: str = "unconfirmed"
    detail: str = ""


DEFECTS = (
    Defect(
        identifier="CSCwm43211",
        headline="ISE reporting wrong latency for RequestLatency and "
                 "TotalAuthenLatency, RADIUS accounting",
        metrics=(
            "ise3_radius_accounting_latency_seconds",
            "ise3_radius_total_authentication_latency_seconds",
        ),
        impact="inflated",
        detail=(
            "ISE reports high requestLatency and TotalLatency in prrt-server.log "
            "and localStore.log while a packet capture shows no corresponding "
            "delay. The reported value is plausible, so no range check can "
            "detect it: a latency panel fed from these fields can show seconds "
            "of delay that did not happen. Prefer authentication-side latency "
            "and treat the accounting-derived figures as indicative only."
        ),
    ),
)


def metrics_affected_by_known_defects():
    """Metric names any listed defect touches."""
    return {name for defect in DEFECTS for name in defect.metrics}


def publish():
    """Export every known defect so dashboards and operators can see it."""
    for defect in DEFECTS:
        for metric in defect.metrics:
            defect_info.labels(
                defect=defect.identifier,
                metric=metric,
                impact=defect.impact,
                confirmed_on_supported_release=defect.confirmed_on_supported_release,
            ).set(1)
    return len(DEFECTS)
