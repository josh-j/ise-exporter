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

That exception was still 20,000 series on its own -- past the soft warning and
41 % of the hard ceiling for one metric family, before any other dataset has
published anything. So it is bounded, and like every other bound in this
exporter the bound is an operator decision declared in
``datasets.session_authorization.options`` rather than a constant here:

    policy_set_by_nad  publish the per-switch view at all
    top_nads           switches published per policy set, most endpoints first

Every session is still counted and still rolled up per ops owner by
``ise3_session_policy_set_endpoints``; ``top_nads`` chooses only how many
switches get their own series under each policy set. The breakdown exports
``ise3_topk_groups_returned`` against ``ise3_topk_groups_total``, so a panel
showing 200 switches out of 5,000 says so, and ``top_nads = 0`` publishes every
switch and warns at start with the series count that implies.

Location is deliberately absent: `network_devices` already publishes
`ise3_network_device_assignment{nad,location,ops_owner}`, so a dashboard joins
for it rather than every session series carrying a copy.
"""
import math
import re
import time
from collections import defaultdict

from prometheus_client import Gauge

from .. import detail_cache, nad_directory, reporting
from ..labels import label
from ..model import Cost, Dataset, Option, Provider
from ..pxgrid import normalize_mac as _canonical_mac
from ..session_detail import project
from ..transports import TransportError


CACHE = "mnt_session_detail"
# The active list is one request that two datasets need in the same tick, so the
# pool owner refreshes it and the pool readers reuse what the owner left. It was
# a short wall-clock TTL, which cannot work here: the owner holds the serialized
# mnt lane for WARMUP_LANE_FRACTION of a cadence, so any TTL shorter than the
# cadence has always expired by the time the reader runs and both datasets fetch
# it -- the shared cost pool then understates MnT load by one full ActiveList
# read a cycle and the two aggregate over snapshots minutes apart. The staleness
# bound is one cadence: reuse this cycle's listing, never the previous owner's.
ACTIVE_LIST_CACHE = "mnt_active_list"
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
failed_authz_profiles = Gauge(
    "ise3_session_failed_authz_profile_endpoints",
    "Distinct failed endpoints per selected authorization profile and ops owner",
    ["provider", "authz_profile", "ops_owner"])
failed_authz_rules = Gauge(
    "ise3_session_failed_authz_rule_endpoints",
    "Distinct failed endpoints per matched authorization rule and ops owner",
    ["provider", "authz_rule", "ops_owner"])
failed_policy_sets = Gauge(
    "ise3_session_failed_policy_set_endpoints",
    "Distinct failed endpoints per matched policy set and ops owner",
    ["provider", "policy_set", "ops_owner"])
authentication_latency = Gauge(
    "ise3_session_authentication_latency_seconds",
    "Current MnT authentication latency across usable session-detail samples",
    ["provider", "statistic"])
authentication_latency_samples = Gauge(
    "ise3_session_authentication_latency_samples",
    "Current MnT session details carrying usable total authentication latency",
    ["provider"])
step_latency = Gauge(
    "ise3_session_authentication_step_latency_seconds",
    "Current MnT authentication-step latency across usable session-detail "
    "samples. step is the position within other_attr_string.StepLatency, not an "
    "ISE message code: ISE reports one more execution step than it reports step "
    "latencies, so no position can be mapped to a code",
    ["provider", "step", "statistic"])
step_latency_samples = Gauge(
    "ise3_session_authentication_step_latency_samples",
    "Current MnT session details carrying usable latency for each step position",
    ["provider", "step"])

_METRICS = (status_endpoints, failure_reasons, auth_methods, authz_profiles,
            authz_rules, policy_sets, policy_set_by_nad, failed_authz_profiles,
            failed_authz_rules, failed_policy_sets, authentication_latency,
            authentication_latency_samples, step_latency, step_latency_samples)

# Per-cycle warm-up ceiling. Bounded so a cold start cannot monopolise the MnT
# lane; the cache converges over several cycles instead.
WARMUP_FETCHES_PER_CYCLE = 2000
# Share of one cadence a warm-up pass may hold the lane for. Half leaves the
# other datasets on this target the rest of the cycle, which is what keeps
# dataset_fresh honest for them while this cache fills.
WARMUP_LANE_FRACTION = 0.5

# Only used to size a startup warning: how many policy sets a deployment
# typically runs, so "one series per NAD" can be stated as a number rather than
# as a shape. The real count comes from the appliance and is not knowable here.
TYPICAL_POLICY_SETS = 4
# Step positions published, and the highest position accepted. ISE 3.3 reports
# 27 of them on a dot1x session; the bound is only there so a malformed
# population cannot manufacture an unbounded label domain.
MAX_STEP_POSITIONS = 256


def _top_nads_danger(value, scale):
    if value:
        return ""
    return (
        "datasets.session_authorization.options.top_nads = 0 publishes one "
        "series per (policy set, NAD) pair: about "
        f"{scale.nads * TYPICAL_POLICY_SETS:,} series at the declared "
        f"{scale.nads:,} NADs and a typical {TYPICAL_POLICY_SETS} policy sets, "
        "which on its own is past the soft series warning. Nothing is truncated "
        "and nothing is hidden -- this is only worth knowing before it lands")


def _policy_set_by_nad_danger(value, scale):
    if value:
        return ""
    return (
        "datasets.session_authorization.options.policy_set_by_nad = false drops "
        "the per-switch view of which policy set each NAD matched, which is the "
        "open-mode versus closed-mode signal; the per-ops-owner rollup remains, "
        "but no panel can name the switch")


def _detail_refresh_danger(value, scale):
    if value < 24:
        return (
            "datasets.session_authorization.options.detail_refresh_hours = "
            f"{value} re-reads the detail of every session still active after "
            f"{value}h. Sessions outlive that on most wired estates, so this "
            "buys freshness with MnT requests the plan does not model")
    return ""


def _detail_grace_danger(value, scale):
    if value > 240:
        return (
            "datasets.session_authorization.options.detail_grace_minutes = "
            f"{value} holds the decision of a departed endpoint for {value / 60:.0f}h. "
            "An endpoint that returns inside that window keeps its old "
            "authorization until detail_refresh_hours elapses")
    return ""


OPTIONS = (
    Option(
        name="detail_grace_minutes",
        default=15,
        minimum=0,
        maximum=1440,
        # The fetch is per arrival, and a roam, a sleep/wake or a session that
        # straddles a cycle boundary all read as arrivals. Holding a departed
        # endpoint briefly makes its return free, which is the cheapest request
        # reduction available here because the decision is unchanged.
        description="minutes an endpoint that left the active list keeps its "
                    "cached detail, so a reconnect inside the window costs no "
                    "request; 0 drops it immediately",
        danger=_detail_grace_danger,
    ),
    Option(
        name="skip_nas_ip_prefixes",
        default="",
        # Keyed on the NAS address because that is what the active list gives
        # us. ISE 3.3 returns a session index -- user, MAC, NAS IP, framed IP,
        # session ids and PSN -- and nothing richer, so identity group or NAD
        # name could only be learned by making the request this avoids.
        description="comma-separated NAS address prefixes whose endpoints are "
                    "never detail-fetched, e.g. \"10.20.\"; they still count in "
                    "active_sessions but leave every authorization breakdown, "
                    "so name only segments whose policy detail you do not "
                    "analyse",
    ),
    Option(
        name="detail_refresh_hours",
        default=24,
        minimum=1,
        maximum=720,
        # The fan-out is one request per MAC and the whole cost of this dataset.
        # Entries for MACs that left the active set are dropped every cycle, so
        # this bound is not about memory: it is only how long a *still active*
        # session keeps its cached decision before being read again.
        description="hours a still-active session keeps its cached "
                    "authorization detail before it is re-read; longer costs "
                    "fewer MnT requests, but a mid-session change (CoA, posture "
                    "remediation, ANC quarantine) stays stale for that long",
        danger=_detail_refresh_danger,
    ),
    Option(
        name="policy_set_by_nad",
        default=True,
        description="publish the per-switch policy set breakdown, the "
                    "open-mode versus closed-mode signal",
        danger=_policy_set_by_nad_danger,
    ),
    Option(
        name="top_nads",
        default=200,
        minimum=0,
        maximum=100_000,
        description="switches published per policy set, most endpoints first; "
                    "0 publishes every switch",
        danger=_top_nads_danger,
    ),
)


def normalize_mac(mac):
    """ActiveList uses colons, some fields use dashes or Cisco's dotted
    three-group form; the URL accepts colons.

    One canonicaliser with the pxGrid provider, so the same endpoint has the
    same identity whichever source published it and the detail cache is not
    keyed on a form the detail URL does not accept.
    """
    return _canonical_mac(mac)


# The shape normalize_mac produces, and the only shape the detail URL is ever
# given: hex pairs and colons need no quoting, so a canonical MAC is safe in a
# URL path by construction. calling_station_id is whatever the NAS sent -- a
# username, an IP, free text -- and MnT answers a detail request for one of
# those with HTTP 500 ("Server encountered error"), so anything else must never
# cost a request. Same check the operator API applies before its detail route.
CANONICAL_MAC = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")


def invalid_macs(sessions):
    """Distinct calling_station_ids that are not MACs at all.

    Kept apart from active_macs so the fetch can count them: an id that can
    never be cached would otherwise be re-requested every cycle for as long as
    the session lives, and every attempt answered with a 500.
    """
    found = set()
    for session in sessions:
        mac = normalize_mac(session.get("calling_station_id"))
        if mac and not CANONICAL_MAC.match(mac):
            found.add(mac)
    return found


def active_list(ctx, *, refresh=False):
    """Read the active list, sharing one fetch across the datasets in this tick.

    ``refresh`` is the pool owner (the cache filler); everything else is a pool
    reader and consumes whatever the owner left this cycle, fetching only when
    there is nothing -- the case where the owner is disabled, which the plan
    charges separately.
    """
    cache = detail_cache.shared(ACTIVE_LIST_CACHE, ttl_seconds=max(ctx.interval, 1))
    # shared() ignores ttl_seconds for a cache that already exists, and the
    # bound is the owner's configured cadence, which is only known here.
    cache.ttl = max(ctx.interval, 1)
    if not refresh:
        cached = cache.get("current")
        if cached is not None:
            cache.count("cache_hit")
            return cached
    listing = ctx.transport.get_mnt_xml("/Session/ActiveList", api="mnt_active_list")
    cache.put("current", listing)
    cache.count("fetched")
    return listing


def active_macs(sessions):
    """Distinct MACs across the active list; one device may hold many sessions.

    Canonical shapes only: a junk calling_station_id is not an endpoint this
    dataset can ever detail-fetch, so admitting it here would put it in every
    coverage denominator and, worse, into a detail URL.
    """
    return {
        mac for mac in (
            normalize_mac(session.get("calling_station_id"))
            for session in sessions)
        if mac and CANONICAL_MAC.match(mac)
    }


def _skipped_prefixes(ctx):
    return tuple(
        prefix.strip()
        for prefix in str(ctx.option("skip_nas_ip_prefixes") or "").split(",")
        if prefix.strip()
    )


def detail_macs(sessions, skip_prefixes):
    """MACs whose detail is worth a request, decided from the active list.

    An endpoint holding several sessions is in scope unless every one of them
    is on a skipped segment: the cheaper reading would drop a device the moment
    one of its sessions looked uninteresting.
    """
    if not skip_prefixes:
        return active_macs(sessions)
    wanted = set()
    for session in sessions:
        mac = normalize_mac(session.get("calling_station_id"))
        if not mac or not CANONICAL_MAC.match(mac):
            continue
        nas_ip = str(session.get("nas_ip_address") or "").strip()
        if not nas_ip or not nas_ip.startswith(skip_prefixes):
            wanted.add(mac)
    return wanted




# MnT answers a MAC with no current session with HTTP 500 and cpm-code 34110,
# so a session that ended between the active list and its detail fetch is an
# ordinary event rather than a failure. The transport carries both, which is
# what lets this tell that apart from the appliance being in trouble.
MNT_NO_SESSION_CODE = "34110"

# Failures that indict the target or the credentials rather than one record.
# Continuing would fire the same doomed request at every remaining MAC --
# thousands of attempts against an unreachable node, or enough 401s to lock
# the account -- and report the cycle as a success. These fail the collection
# with their own reason; everything per-record is skipped and counted.
FATAL_REASONS = frozenset({
    "authentication_failed", "authorization_failed", "authentication_backoff",
    "connection_failed", "tls_failed", "state_unavailable",
})


def _session_gone(error):
    """Whether this failure is ISE saying the session is no longer there.

    Decided on the cpm-code alone: MnT uses HTTP 500 both for a MAC with no
    current session (34110) and for genuine trouble ("Server encountered
    error", no code), so the status cannot tell churn from a broken record.
    """
    return getattr(error, "code", "") == MNT_NO_SESSION_CODE


def warm(ctx, cache, macs):
    """Fetch detail for uncached MACs, up to this cycle's budget.

    Returns how many were left for the next cycle. Nothing already cached is
    re-fetched: the fact does not change while the session lives.

    Bounded in wall-clock as well as in count. The count stopped being a bound
    on lane time once the request budget became something the transport enforces
    by blocking: 2,000 detail requests through the limiter hold the serialized
    mnt lane for minutes to hours, and every other mnt dataset -- including
    posture_current, which only reads this cache -- waits behind it and reads as
    stale. What this pass does not reach is deferred, which the scheduler
    already paces the next visit from.
    """
    outstanding = cache.uncached(macs)
    batch = outstanding[:WARMUP_FETCHES_PER_CYCLE]
    deadline = time.monotonic() + max(1.0, ctx.interval * WARMUP_LANE_FRACTION)
    fetched = 0
    for mac in batch:
        if time.monotonic() >= deadline:
            break
        fetched += 1
        try:
            # ``mac`` is canonical by construction -- active_macs admits nothing
            # else -- so the path needs no quoting and can reach no other
            # MnT resource.
            record = ctx.transport.get_mnt_xml(
                f"/Session/MACAddress/{mac}", api="mnt_session_detail")
        except TransportError as error:
            if error.reason in FATAL_REASONS:
                raise
            # A MAC that left between the ActiveList read and its detail fetch
            # is the normal churn case, and ISE answers it with HTTP 500 and
            # cpm-code 34110 rather than an empty document -- so it arrives
            # here, not at the empty branch below. Counted apart from a genuine
            # per-record failure (a 500 with no code) because it is expected
            # traffic; both are skipped and the cycle carries on.
            cache.count("gone" if _session_gone(error) else "failed")
            continue
        except Exception:       # noqa: BLE001 - one MAC must not fail the dataset
            cache.count("failed")
            continue
        rows = record.get("sessions") or []
        if not rows:
            cache.count("empty")
            continue
        # The projection, not the record: an MnT session document carries
        # dozens of fields and 20,000 of them held whole was the largest thing
        # this process retained. See session_detail.project.
        cache.put(mac, project(rows[0]))
        cache.count("fetched")
    return len(outstanding) - fetched


def _milliseconds(value):
    text = str(value or "").strip()
    if text.lower().endswith("ms"):
        text = text[:-2].strip()
    try:
        milliseconds = float(text)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(milliseconds) or not 0 <= milliseconds <= 86_400_000:
        return None
    return milliseconds / 1000.0


def _step_samples(raw_latency):
    """``1=0;2=0;4=2;...`` from other_attr_string.StepLatency, by position.

    The position is the label. It used to be resolved to the ISE message code at
    the same offset in execution_steps, which cannot be right: ISE 3.3 reports 28
    execution steps and 27 step latencies for the same session, and nothing in
    the document says which end of the list the missing entry belongs to. A
    neighbouring message code on every series is worse than an honest position.
    """
    samples = []
    for item in str(raw_latency or "").split(";"):
        position, separator, raw_ms = item.partition("=")
        if not separator:
            continue
        try:
            index = int(position.strip())
        except ValueError:
            continue
        if not 1 <= index <= MAX_STEP_POSITIONS:
            continue
        seconds = _milliseconds(raw_ms)
        if seconds is not None:
            samples.append((str(index), seconds))
    return samples


def aggregate(cache, macs, directory):
    """Build distinct-MAC sets per dimension from whatever is cached.

    Every bucket is keyed on (value, ops_owner) -- a marginal, not a cross
    product with the NAD. ``policy_set_nad`` is the one deliberate exception.
    """
    buckets = {name: defaultdict(set) for name in (
        "status", "reason", "method", "profile", "rule", "policy_set",
        "policy_set_nad", "failed_profile", "failed_rule", "failed_policy_set")}
    covered = 0
    total_latencies = []
    step_latencies = defaultdict(list)

    for mac in macs:
        detail = cache.get(mac)
        if not detail:
            continue
        covered += 1
        # Accounting-only records carry no verdict and cannot say anything about
        # authorization; counting them would dilute every ratio below.
        if not detail["has_verdict"]:
            continue

        nad = label(detail["nad"] or detail["nas_ip"], "unknown")
        # ops_owner lives only in ERS groups, so it comes from the inventory
        # directory. A NAD that is not in inventory still counts, as "unknown" --
        # a session from an unconfigured NAD is itself worth seeing.
        owner = label(directory.ops_owner(detail["nas_ip"], detail["nad"]))

        if detail["passed"]:
            buckets["status"][("passed", owner)].add(mac)
        if detail["failed"]:
            buckets["status"][("failed", owner)].add(mac)
            # ISE 3.3 emits no failure_reason element; message_code is the
            # result code the document does carry. Whether a failing session
            # adds failure_reason could not be observed, so the leading-code
            # split stays and message_code is the fallback.
            reason = detail["failure_reason"].split(" ", 1)[0] or detail["message_code"]
            if reason:
                buckets["reason"][(label(reason), owner)].add(mac)

        if detail["method"]:
            buckets["method"][(label(detail["method"]), owner)].add(mac)

        # selected_azn_profiles can be comma-separated.
        for profile in detail["profiles"].split(","):
            if profile.strip():
                normalized = label(profile.strip())
                buckets["profile"][(normalized, owner)].add(mac)
                if detail["failed"]:
                    buckets["failed_profile"][(normalized, owner)].add(mac)

        # Already parsed out of other_attr_string when the record was cached,
        # rather than on every read of every session on every cycle.
        rule, policy_set = detail["authz_rule"], detail["policy_set"]
        if rule:
            buckets["rule"][(label(rule), owner)].add(mac)
            if detail["failed"]:
                buckets["failed_rule"][(label(rule), owner)].add(mac)
        if policy_set:
            buckets["policy_set"][(label(policy_set), owner)].add(mac)
            buckets["policy_set_nad"][(label(policy_set), nad)].add(mac)
            if detail["failed"]:
                buckets["failed_policy_set"][(label(policy_set), owner)].add(mac)

        total_latency = _milliseconds(detail["total_authentication_latency"])
        if total_latency is not None:
            total_latencies.append(total_latency)
        for step, seconds in _step_samples(detail["step_latency"]):
            step_latencies[step].append(seconds)

    # Step positions are a label. Keep the most-observed ones and use numeric
    # ordering as a stable tie-breaker.
    step_latencies = dict(sorted(
        step_latencies.items(),
        key=lambda item: (-len(item[1]), int(item[0])),
    )[:MAX_STEP_POSITIONS])
    return buckets, covered, total_latencies, step_latencies


def rank_nads(bucket, keep):
    """Order each policy set's switches by endpoint count and keep the top K.

    Ranked within a policy set rather than across all of them, because the
    question is "which switches are on this policy set", and a global top-K
    would let one busy policy set crowd every other one out of the answer.
    Ties break on the switch name so the published set is stable between
    collections -- a top-K that reshuffles makes every panel look like something
    changed. ``keep`` of 0 keeps all of them.
    """
    by_policy = {}
    for (policy_set, nad), members in bucket.items():
        by_policy.setdefault(policy_set, []).append((nad, members))
    kept = []
    for policy_set in sorted(by_policy):
        rows = sorted(by_policy[policy_set], key=lambda row: (-len(row[1]), row[0]))
        for nad, members in (rows if not keep else rows[:keep]):
            kept.append((policy_set, nad, members))
    return kept


def fetch(ctx):
    cache = detail_cache.shared(CACHE)
    # retain() below already drops departed MACs, so this bound is not what
    # keeps the cache finite -- it is only how long a still-active session may
    # hold a decision that a CoA could have changed underneath it. shared()
    # ignores ttl_seconds for a cache that already exists, so set it here where
    # the operator's choice is known.
    cache.ttl = ctx.option("detail_refresh_hours") * 3600
    # The pool owner: this dataset's read is the one the plan charges, and its
    # snapshot is what the readers in this cycle aggregate against.
    listing = active_list(ctx, refresh=True)
    sessions = listing.get("sessions") or []
    # Visible, not silent: one tick per distinct junk id per cycle, in the same
    # counter the failed fetches use, so a NAS spraying garbage into
    # calling_station_id shows up without ever costing a request.
    for _ in invalid_macs(sessions):
        cache.count("invalid")
    macs = detail_macs(sessions, _skipped_prefixes(ctx))

    # Departed MACs are held briefly rather than dropped, so a reconnect inside
    # the window is free. Coverage is still measured against the active set.
    cache.retain(macs, ctx.option("detail_grace_minutes") * 60)
    if not macs:
        cache.publish(0)
        return

    outstanding = warm(ctx, cache, macs)
    directory = nad_directory.shared()
    buckets, covered, total_latencies, step_latencies = aggregate(
        cache, macs, directory)
    cache.publish(len(macs), deferred_count=outstanding)
    # The scheduler revisits a filling cache at the rate the budget affords
    # rather than at this dataset's cadence, so it needs the count too.
    ctx.defer(outstanding)

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
    for (profile, owner), members in buckets["failed_profile"].items():
        ctx.set(
            failed_authz_profiles,
            len(members),
            authz_profile=profile,
            ops_owner=owner,
        )
    for (rule, owner), members in buckets["failed_rule"].items():
        ctx.set(
            failed_authz_rules,
            len(members),
            authz_rule=rule,
            ops_owner=owner,
        )
    for (name, owner), members in buckets["failed_policy_set"].items():
        ctx.set(
            failed_policy_sets,
            len(members),
            policy_set=name,
            ops_owner=owner,
        )

    ctx.set(authentication_latency_samples, len(total_latencies))
    if total_latencies:
        for statistic, value in (
            ("mean", sum(total_latencies) / len(total_latencies)),
            ("max", max(total_latencies)),
        ):
            ctx.set(authentication_latency, value, statistic=statistic)
    for step, samples in step_latencies.items():
        ctx.set(step_latency_samples, len(samples), step=step)
        for statistic, value in (
            ("mean", sum(samples) / len(samples)),
            ("max", max(samples)),
        ):
            ctx.set(step_latency, value, step=step, statistic=statistic)

    # The one bounded breakdown in this dataset. The rollup above is computed
    # from every session regardless, so bounding this one narrows what is
    # published and never what is measured.
    pairs = buckets["policy_set_nad"]
    if ctx.option("policy_set_by_nad"):
        kept = rank_nads(pairs, ctx.option("top_nads"))
        reporting.publish_coverage(ctx, "policy_set_nad", len(kept), len(pairs))
        for name, nad, members in kept:
            ctx.set(policy_set_by_nad, len(members), policy_set=name, nad=nad)
    else:
        # Zero of however many exist, rather than no signal at all: an absent
        # coverage series and a complete one look identical on a dashboard.
        reporting.publish_coverage(ctx, "policy_set_nad", 0, len(pairs))


DATASET = Dataset(
    name="session_authorization",
    description="Policy set, authorization rule, profile and method per endpoint",
    default_interval=300,
    metrics=_METRICS,
    options=OPTIONS,
    providers=(
        Provider(
            name="mnt",
            # Converging: one fetch per MAC, cached for the session's life. The
            # warm-up ceiling bounds a cold start; steady state is churn. At a
            # 5-minute cadence, 1% of sessions turning over per cycle implies a
            # mean session of roughly eight hours.
            cost=Cost(target="mnt", requests=1, scales_with="sessions",
                      warmup_requests=WARMUP_FETCHES_PER_CYCLE,
                      churn_fraction=0.01, churn_interval=300, shares=POOL),
            supplies=frozenset({
                "policy_set", "authz_rule", "authz_profile", "method",
                "failure_reason", "failure_context", "status", "nad",
                "ops_owner", "authentication_latency"}),
            coverage="converging",
            fetch=fetch,
        ),
    ),
)
