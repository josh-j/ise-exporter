"""Converging Device Administration policy-rule inventory.

ISE exposes authentication and authorization rules behind separate per-policy
endpoints. Sampling a few policy sets forever would make the dashboard appear
complete while omitting rules, and reading every set every cycle would turn
configuration size into permanent PAN load.

Each policy set is therefore fetched once into a seven-day detail cache. A cold
cache fills ten policy sets per cycle, coverage and deferred work are exported,
and steady state pays only for policy sets that changed or aged out. The load
model declares the two requests per policy set explicitly.
"""
from prometheus_client import Gauge

from .. import detail_cache
from ..labels import label
from ..model import Cost, Dataset, Provider


CACHE = "openapi_tacacs_policy_rules"
CACHE_TTL_SECONDS = 7 * 86400
WARMUP_POLICY_SETS_PER_CYCLE = 10
REQUESTS_PER_POLICY_SET = 2
MAX_POLICY_SETS = 1000

rule_count = Gauge(
    "ise3_tacacs_policy_rule_count",
    "Configured Device Administration rules per policy set and rule type",
    ["provider", "policy_set", "rule_type"],
)
rules_total = Gauge(
    "ise3_tacacs_policy_rules_total",
    "Configured Device Administration rules by type across covered policy sets",
    ["provider", "rule_type"],
)

_METRICS = (rule_count, rules_total)


def _rule_list(ctx, policy_id, rule_type):
    return ctx.transport.get_openapi(
        f"/policy/device-admin/policy-set/{policy_id}/{rule_type}",
        api=f"pan_tacacs_{rule_type}_rules",
    )


def warm(ctx, cache, policies):
    outstanding = [
        policy["id"] for policy in policies
        if cache.get(policy["id"]) is None
    ]
    for policy_id in outstanding[:WARMUP_POLICY_SETS_PER_CYCLE]:
        try:
            authentication = _rule_list(ctx, policy_id, "authentication")
            authorization = _rule_list(ctx, policy_id, "authorization")
        except Exception:  # noqa: BLE001 - one policy must not fail the inventory
            cache.count("failed")
            continue
        if not isinstance(authentication, list) or not isinstance(authorization, list):
            cache.count("empty")
            continue
        cache.put(policy_id, {
            "authentication": len(authentication),
            "authorization": len(authorization),
        })
        cache.count("fetched")
    return len(cache.uncached(policy["id"] for policy in policies))


def fetch(ctx):
    rows = ctx.transport.get_openapi(
        "/policy/device-admin/policy-set",
        api="pan_policy_rule_sets",
    )
    if not isinstance(rows, list):
        ctx.fail("invalid_response", "the Device Admin policy-set inventory failed")
    if len(rows) > MAX_POLICY_SETS:
        ctx.fail(
            "response_too_large",
            f"{len(rows)} policy sets exceeds the {MAX_POLICY_SETS} ceiling",
        )

    policies = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            ctx.fail("invalid_response", "a Device Admin policy set was not an object")
        policy_id = str(row.get("id") or "").strip()
        if not policy_id or policy_id in seen:
            continue
        seen.add(policy_id)
        policies.append({
            "id": policy_id,
            "name": label(row.get("name"), "unknown"),
        })

    cache = detail_cache.shared(CACHE, ttl_seconds=CACHE_TTL_SECONDS)
    cache.retain(policy["id"] for policy in policies)
    outstanding = warm(ctx, cache, policies)
    cache.publish(len(policies), deferred_count=outstanding)
    ctx.defer(outstanding)

    totals = {"authentication": 0, "authorization": 0}
    for policy in policies:
        counts = cache.get(policy["id"])
        if counts is None:
            continue
        for rule_type in ("authentication", "authorization"):
            count = int(counts.get(rule_type) or 0)
            totals[rule_type] += count
            ctx.set(
                rule_count,
                count,
                policy_set=policy["name"],
                rule_type=rule_type,
            )
    for rule_type, count in totals.items():
        ctx.set(rules_total, count, rule_type=rule_type)


DATASET = Dataset(
    name="tacacs_policy_rules",
    description="Device Admin authentication and authorization rule inventory",
    default_interval=21600,
    metrics=_METRICS,
    providers=(
        Provider(
            name="openapi",
            cost=Cost(
                target="pan",
                requests=1,
                scales_with="policy_sets",
                warmup_requests=(
                    WARMUP_POLICY_SETS_PER_CYCLE * REQUESTS_PER_POLICY_SET
                ),
                churn_fraction=0.001,
                requests_per_entity=REQUESTS_PER_POLICY_SET,
            ),
            supplies=frozenset({"policy_set", "rule_count", "rule_type"}),
            coverage="converging",
            fetch=fetch,
        ),
    ),
)
