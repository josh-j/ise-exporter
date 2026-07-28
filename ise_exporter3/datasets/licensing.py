"""License tiers, consumption, and compliance state.

OpenAPI only. The compliance and status vocabularies come from ISE 3.3's
TierStateSettings enum and are matched exactly: guessing at an unknown value is
how a non-compliant deployment ends up reported as healthy.

``compliant`` stays a strict boolean -- EVALUATION is a licensed state, not a
compliant one, and a deployment running every tier on evaluation should read as
0. But 0 alone cannot distinguish an evaluation licence from a genuine
entitlement breach, and an unlicensed lab is the common case, so the reported
state is published alongside it rather than collapsed into the boolean.
"""
import math

from prometheus_client import Gauge

from ..compatibility import MAX_LICENSE_TIERS
from ..labels import label
from ..model import Cost, Dataset, Provider


consumption = Gauge(
    "ise3_license_consumption", "Licenses consumed in a tier", ["provider", "tier"])
enabled = Gauge(
    "ise3_license_enabled", "Tier is enabled", ["provider", "tier"])
compliant = Gauge(
    "ise3_license_compliant", "Tier is in compliance", ["provider", "tier"])
compliance_state = Gauge(
    "ise3_license_compliance_state",
    "Compliance state ISE reports for a tier (1 for the current state)",
    ["provider", "tier", "state"])

_METRICS = (consumption, enabled, compliant, compliance_state)

_COMPLIANT = frozenset({"COMPLIANT", "FULL_COMPLIANCE", "RESERVED_IN_COMPLIANCE"})
_COMPLIANCE_STATES = _COMPLIANT | frozenset({
    "EVALUATION", "EVALUATION_EXPIRED", "NONCOMPLIANT", "RELEASED_ENTITLEMENT"})
_STATUS_STATES = frozenset({"DISABLED", "ENABLED"})


def fetch(ctx):
    # tier-state returns a bare list, with no `response` envelope.
    payload = ctx.transport.get_openapi(
        "/license/system/tier-state", api="pan_license", unwrap=False)
    if not isinstance(payload, (dict, list)) or not payload:
        ctx.fail("invalid_response", "license tier-state was empty or malformed")

    tiers = payload if isinstance(payload, list) else [payload]
    if len(tiers) > MAX_LICENSE_TIERS:
        ctx.fail("invalid_response",
                 f"license tier-state exceeded the {MAX_LICENSE_TIERS}-tier ceiling")

    seen = set()
    for tier in tiers:
        if not isinstance(tier, dict):
            ctx.fail("invalid_response", "license tier-state contained a non-object")
        name = str(tier.get("name") or "").strip()
        if not name or name in seen:
            ctx.fail("invalid_response", "license tier-state had an invalid tier name")
        seen.add(name)
        if "consumptionCounter" not in tier:
            ctx.fail("invalid_response", f"license tier {name} omitted consumption")
        try:
            used = float(tier.get("consumptionCounter") or 0)
        except (TypeError, ValueError):
            ctx.fail("invalid_response", f"license tier {name} had invalid consumption")
        if not math.isfinite(used) or used < 0:
            ctx.fail("invalid_response", f"license tier {name} had invalid consumption")
        compliance = str(tier.get("compliance") or "").strip().upper()
        status = str(tier.get("status") or "").strip().upper()
        if compliance not in _COMPLIANCE_STATES:
            ctx.fail("invalid_response", f"license tier {name} had unknown compliance")
        if status not in _STATUS_STATES:
            ctx.fail("invalid_response", f"license tier {name} had unknown status")

        tier_label = label(name)
        ctx.set(consumption, used, tier=tier_label)
        ctx.set(enabled, int(status == "ENABLED"), tier=tier_label)
        ctx.set(compliant, int(compliance in _COMPLIANT), tier=tier_label)
        # Every state, so a transition is a visible 1 -> 0 rather than a series
        # that disappears, and EVALUATION is distinguishable from NONCOMPLIANT.
        for state in sorted(_COMPLIANCE_STATES):
            ctx.set(compliance_state, int(state == compliance),
                    tier=tier_label, state=state)


DATASET = Dataset(
    name="licensing",
    description="License tiers, consumption, compliance",
    default_interval=21600,
    metrics=_METRICS,
    providers=(
        Provider(
            name="openapi",
            cost=Cost(target="pan", requests=1),
            supplies=frozenset({"tier", "consumption", "compliance",
                                "compliance_state"}),
            fetch=fetch,
        ),
    ),
)
