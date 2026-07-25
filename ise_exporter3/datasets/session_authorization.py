"""What each active endpoint actually matched: policy set, rule, profile, method.

This is the ground-truth readout for open-mode versus closed-mode operation, and
it is the reason the MnT plane earns its cost. The per-session detail carries the
matched authorization rule and policy set inside `other_attr_string`, which is a
far stronger signal than inferring intent from profile names.

Coverage converges to the whole active set. The authorization decision is stable
for the life of a session, so each MAC is fetched once and cached: the per-cycle
budget limits how fast a cold cache fills, not how much of the fleet is ever
seen. On a 20,000-session estate at a 2,000-per-cycle budget the cache is warm in
about ten cycles, after which only newly-appeared MACs are fetched.

Every series is a count of **distinct MACs**, never one series per endpoint. A
device with several concurrent sessions counts once.

Dimensions are keyed on **ops owner**, not on NAD. Keying every dimension on the
NAD is a cross product: at 5,000 switches and ten dimension values each that is
50,000 series to answer questions that are actually asked per owner. The one
exception is the policy set, which is kept per NAD because "which switches are
still in open mode" is a per-switch question and has no useful coarser form.

Location is deliberately absent: `network_devices` already publishes
`ise3_network_device_assignment{nad,location,ops_owner}`, so a dashboard joins
for it rather than every session series carrying a copy.
"""
from collections import defaultdict

from prometheus_client import Gauge

from .. import detail_cache, nad_directory
from ..labels import label
from ..model import Cost, Dataset, Provider


CACHE = "mnt_session_detail"
# The active list is one request that two datasets need in the same tick. A TTL
# well under the cadence lets them share one fetch while still re-reading it
# every cycle -- without this, session_authorization and posture_current each
# fetch it and the shared cost pool understates MnT load by one request a cycle.
ACTIVE_LIST_CACHE = "mnt_active_list"
ACTIVE_LIST_TTL_SECONDS = 60
# One fan-out answers this dataset and posture_current, so they share a pool and
# the plan charges it once.
POOL = "mnt_session_detail"

status_endpoints = Gauge(
    "ise3_session_status_endpoints",
    "Distinct endpoints by RADIUS authentication status, per ops owner",
    ["provider", "ops_owner", "status"])
failure_reasons = Gauge(
    "ise3_session_failure_reason_endpoints",
    "Distinct endpoints per failure reason code, per ops owner",
    ["provider", "reason_code", "ops_owner"])
auth_methods = Gauge(
    "ise3_session_auth_method_endpoints",
    "Distinct endpoints per authentication method, per ops owner",
    ["provider", "method", "ops_owner"])
authz_profiles = Gauge(
    "ise3_session_authz_profile_endpoints",
    "Distinct endpoints per selected authorization profile, per ops owner",
    ["provider", "authz_profile", "ops_owner"])
authz_rules = Gauge(
    "ise3_session_authz_rule_endpoints",
    "Distinct endpoints per matched authorization rule, per ops owner. Parsed "
    "from other_attr_string.AuthorizationPolicyMatchedRule -- the ground-truth "
    "open-mode versus closed-mode signal where rule names follow convention",
    ["provider", "authz_rule", "ops_owner"])
policy_sets = Gauge(
    "ise3_session_policy_set_endpoints",
    "Distinct endpoints per matched policy set, per ops owner. Parsed from "
    "other_attr_string.ISEPolicySetName, which frequently names the mode outright",
    ["provider", "policy_set", "ops_owner"])
policy_set_by_nad = Gauge(
    "ise3_session_policy_set_endpoints_by_nad",
    "Distinct endpoints per matched policy set, per NAD. Kept per switch because "
    "'which switches are still in open mode' has no useful coarser form",
    ["provider", "policy_set", "nad"])

_METRICS = (status_endpoints, failure_reasons, auth_methods, authz_profiles,
            authz_rules, policy_sets, policy_set_by_nad)

# Per-cycle warm-up ceiling. Bounded so a cold start cannot monopolise the MnT
# lane; the cache converges over several cycles instead.
WARMUP_FETCHES_PER_CYCLE = 2000


def normalize_mac(mac):
    """ActiveList uses colons, some fields use dashes; the URL accepts colons."""
    return str(mac or "").strip().upper().replace("-", ":")


def normalize_location(value):
    """Strip the 'All Locations#' root so this joins with NAD inventory labels."""
    if not value:
        return "Unknown"
    parts = str(value).split("#")
    if parts and parts[0] == "All Locations" and len(parts) > 1:
        return "#".join(parts[1:])
    return value


def parse_attributes(value):
    """Parse ISE's ``:!:Key=Value:!:...`` other_attr_string format."""
    parsed = {}
    for part in str(value or "").split(":!:"):
        key, separator, item = part.partition("=")
        if separator and key.strip() and item.strip():
            parsed[key.strip()] = item.strip()
    return parsed


def active_list(ctx):
    """Read the active list, sharing one fetch across the datasets in this tick."""
    cache = detail_cache.shared(
        ACTIVE_LIST_CACHE, ttl_seconds=ACTIVE_LIST_TTL_SECONDS)
    cached = cache.get("current")
    if cached is not None:
        cache.count("cache_hit")
        return cached
    listing = ctx.transport.get_mnt_xml("/Session/ActiveList", api="mnt_active_list")
    cache.put("current", listing)
    cache.count("fetched")
    return listing


def active_macs(sessions):
    """Distinct MACs across the active list; one device may hold many sessions."""
    return {
        mac for mac in (
            normalize_mac(session.get("calling_station_id"))
            for session in sessions)
        if mac
    }


def warm(ctx, cache, macs):
    """Fetch detail for uncached MACs, up to this cycle's budget.

    Returns how many were left for the next cycle. Nothing already cached is
    re-fetched: the fact does not change while the session lives.
    """
    outstanding = cache.uncached(macs)
    batch = outstanding[:WARMUP_FETCHES_PER_CYCLE]
    for mac in batch:
        try:
            record = ctx.transport.get_mnt_xml(
                f"/Session/MACAddress/{mac}", api="mnt_session_detail")
        except Exception:       # noqa: BLE001 - one MAC must not fail the dataset
            cache.count("failed")
            continue
        rows = record.get("sessions") or []
        if not rows:
            cache.count("empty")
            continue
        cache.put(mac, rows[0])
        cache.count("fetched")
    return len(outstanding) - len(batch)


def _nad_key(detail):
    return (
        label(detail.get("network_device_name") or detail.get("nas_ip_address"),
              "unknown"),
        label(normalize_location(detail.get("location")), "Unknown"),
    )


def aggregate(cache, macs, directory):
    """Build distinct-MAC sets per dimension from whatever is cached.

    Every bucket is keyed on (value, ops_owner) -- a marginal, not a cross
    product with the NAD. ``policy_set_nad`` is the one deliberate exception.
    """
    buckets = {name: defaultdict(set) for name in (
        "status", "reason", "method", "profile", "rule", "policy_set",
        "policy_set_nad")}
    covered = 0

    for mac in macs:
        detail = cache.get(mac)
        if not detail:
            continue
        covered += 1
        # Accounting-only records carry no verdict and cannot say anything about
        # authorization; counting them would dilute every ratio below.
        if "passed" not in detail and "failed" not in detail:
            continue

        nad, _location = _nad_key(detail)
        # ops_owner lives only in ERS groups, so it comes from the inventory
        # directory. A NAD that is not in inventory still counts, as "unknown" --
        # a session from an unconfigured NAD is itself worth seeing.
        owner = label(directory.ops_owner(
            detail.get("nas_ip_address"), detail.get("network_device_name")))

        if str(detail.get("passed", "")).lower() == "true":
            buckets["status"][("passed", owner)].add(mac)
        if str(detail.get("failed", "")).lower() == "true":
            buckets["status"][("failed", owner)].add(mac)
            reason = str(detail.get("failure_reason", "")).split(" ", 1)[0]
            if reason:
                buckets["reason"][(label(reason), owner)].add(mac)

        method = detail.get("authentication_method")
        if method:
            buckets["method"][(label(method), owner)].add(mac)

        # selected_azn_profiles can be comma-separated.
        for profile in str(detail.get("selected_azn_profiles", "")).split(","):
            if profile.strip():
                buckets["profile"][(label(profile.strip()), owner)].add(mac)

        attributes = parse_attributes(detail.get("other_attr_string"))
        rule = attributes.get("AuthorizationPolicyMatchedRule")
        policy_set = attributes.get("ISEPolicySetName")
        if rule:
            buckets["rule"][(label(rule), owner)].add(mac)
        if policy_set:
            buckets["policy_set"][(label(policy_set), owner)].add(mac)
            buckets["policy_set_nad"][(label(policy_set), nad)].add(mac)

    return buckets, covered


def fetch(ctx):
    cache = detail_cache.shared(CACHE)
    listing = active_list(ctx)
    macs = active_macs(listing.get("sessions") or [])

    # Drop departed MACs first, so coverage is measured against the current set.
    cache.retain(macs)
    if not macs:
        cache.publish(0)
        return

    outstanding = warm(ctx, cache, macs)
    directory = nad_directory.shared()
    buckets, covered = aggregate(cache, macs, directory)
    cache.publish(len(macs), deferred_count=outstanding)

    for (status, owner), members in buckets["status"].items():
        ctx.set(status_endpoints, len(members), status=status, ops_owner=owner)
    for (reason, owner), members in buckets["reason"].items():
        ctx.set(failure_reasons, len(members), reason_code=reason, ops_owner=owner)
    for (method, owner), members in buckets["method"].items():
        ctx.set(auth_methods, len(members), method=method, ops_owner=owner)
    for (profile, owner), members in buckets["profile"].items():
        ctx.set(authz_profiles, len(members), authz_profile=profile, ops_owner=owner)
    for (rule, owner), members in buckets["rule"].items():
        ctx.set(authz_rules, len(members), authz_rule=rule, ops_owner=owner)
    for (name, owner), members in buckets["policy_set"].items():
        ctx.set(policy_sets, len(members), policy_set=name, ops_owner=owner)
    for (name, nad), members in buckets["policy_set_nad"].items():
        ctx.set(policy_set_by_nad, len(members), policy_set=name, nad=nad)


DATASET = Dataset(
    name="session_authorization",
    description="Policy set, authorization rule, profile and method per endpoint",
    default_interval=300,
    metrics=_METRICS,
    providers=(
        Provider(
            name="mnt",
            # Converging: one fetch per MAC, cached for the session's life. The
            # warm-up ceiling bounds a cold start; steady state is churn. At a
            # 5-minute cadence, 1% of sessions turning over per cycle implies a
            # mean session of roughly eight hours.
            cost=Cost(target="mnt", requests=1, scales_with="sessions",
                      warmup_requests=WARMUP_FETCHES_PER_CYCLE,
                      churn_fraction=0.01, shares=POOL),
            supplies=frozenset({
                "policy_set", "authz_rule", "authz_profile", "method",
                "failure_reason", "status", "nad", "ops_owner"}),
            coverage="converging",
            fetch=fetch,
        ),
    ),
)
