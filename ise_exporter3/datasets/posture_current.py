"""Current posture of active endpoints.

Distinct from ``posture_history``: this is what is compliant right now, not what
was assessed over a reporting window. The two have separate metric families and
never substitute for one another.

- ``mnt`` reads the same cached per-session detail that answers
  ``session_authorization``, so it costs nothing extra and converges to the whole
  active set. The overall posture status is frequently ``NotApplicable`` even
  when posture ran, so the per-policy pass/fail parsed out of ``PostureReport``
  is the signal that actually works -- which is why this dataset parses the
  report rather than reading the summary field alone.
- ``pxgrid`` carries ``postureStatus`` on the session object for free with the
  session stream, but not the per-policy report -- a cheaper, coarser answer.
"""
from collections import defaultdict

from prometheus_client import Gauge

from .. import detail_cache, nad_directory
from ..labels import label
from ..model import Cost, Dataset, Provider
from ..pxgrid import first, normalize_mac as normalize_pxgrid_mac, session_key
from .session_authorization import CACHE, active_list, active_macs, normalize_mac


endpoints_by_status = Gauge(
    "ise3_posture_endpoints", "Distinct active endpoints by posture status",
    ["provider", "status", "ops_owner"])
policy_results = Gauge(
    "ise3_posture_policy_results",
    "Distinct endpoints per posture policy and result, parsed from PostureReport. "
    "The overall status is often NotApplicable even when posture ran, so this is "
    "the breakdown that carries the real signal",
    ["provider", "policy", "result"])
by_agent_version = Gauge(
    "ise3_posture_agent_version_endpoints",
    "Distinct endpoints per Secure Client agent version",
    ["provider", "agent_version"])
by_os = Gauge(
    "ise3_posture_endpoints_by_os", "Distinct endpoints per operating system",
    ["provider", "os"])
by_psn = Gauge(
    "ise3_posture_endpoints_by_psn",
    "Distinct active endpoints by serving PSN and posture status",
    ["provider", "psn", "status"])
field_coverage = Gauge(
    "ise3_session_detail_field_coverage",
    "Fraction of cached active-session details carrying each Secure Client "
    "and authentication field",
    ["provider", "field"])

_METRICS = (
    endpoints_by_status, policy_results, by_agent_version, by_os, by_psn,
    field_coverage,
)

# ISE spells the same posture verdict several ways across fields and releases.
_CANONICAL_STATUS = {
    "compliant": "Compliant",
    "noncompliant": "NonCompliant",
    "non_compliant": "NonCompliant",
    "pending": "Pending",
    "notapplicable": "NotApplicable",
    "na": "NotApplicable",
    "unknown": "Unknown",
    "error": "Error",
}

# The field-name variants ISE spells these with are resolved once, when the
# record is cached -- see session_detail.project. This dataset reads the
# resolved names, so a new spelling is handled in one place rather than in every
# reader of the cache.


def canonical_status(value):
    """Collapse ISE's spelling variants so one verdict is one series."""
    key = str(value or "").strip().lower().replace(" ", "")
    if not key:
        return "NotApplicable"
    return _CANONICAL_STATUS.get(key, str(value).strip())


def parse_posture_report(value):
    """Yield ``(policy, result)`` pairs from ISE's PostureReport field.

    The field is a semicolon-separated list of ``policy:result``, and ISE escapes
    a literal semicolon inside a policy name as ``\\;``. Splitting naively merges
    two policies into one nonsense label, so unescape before splitting.
    """
    text = str(value or "").strip()
    if not text:
        return
    placeholder = "\x00"
    for entry in text.replace("\\;", placeholder).split(";"):
        entry = entry.replace(placeholder, ";").strip()
        if not entry or ":" not in entry:
            continue
        policy, _, result = entry.rpartition(":")
        policy, result = policy.strip(), result.strip()
        if policy and result:
            yield policy, result


def fetch_pxgrid(ctx):
    sessions = ctx.transport.get_sessions(max_age=ctx.interval)
    directory = nad_directory.shared()
    statuses = defaultdict(set)
    fields = defaultdict(int)
    identified = set()

    for session in sessions:
        identity = normalize_pxgrid_mac(first(
            session, "macAddress", "callingStationId", "calling_station_id"))
        identity = identity or session_key(session)
        if not identity:
            continue
        identified.add(identity)
        owner = label(directory.ops_owner(
            first(session, "nasIpAddress", "nas_ip_address"),
            first(session, "nasName", "networkDeviceName", "network_device_name")))
        status = canonical_status(first(
            session, "postureStatus", "posture_status"))
        statuses[(status, owner)].add(identity)
        for field_name, keys in (
            ("posture_status", ("postureStatus", "posture_status")),
            ("mdm_registered", ("mdmRegistered", "mdm_registered")),
            ("mdm_compliant", ("mdmCompliant", "mdm_compliant")),
        ):
            fields[field_name] += int(bool(first(session, *keys)))

    for (status, owner), members in statuses.items():
        ctx.set(
            endpoints_by_status, len(members),
            status=status, ops_owner=owner)
    if identified:
        for field_name, populated in fields.items():
            ctx.set(
                field_coverage, populated / len(identified),
                field=field_name)


def fetch_mnt(ctx):
    cache = detail_cache.shared(CACHE)
    # Shared with session_authorization: one active-list read per tick, not two.
    listing = active_list(ctx)
    sessions = listing.get("sessions") or []
    macs = active_macs(sessions)
    if not macs:
        return

    serving_psns = defaultdict(set)
    for session in sessions:
        mac = normalize_mac(session.get("calling_station_id"))
        if mac:
            serving_psns[mac].add(label(session.get("server"), "unknown"))

    directory = nad_directory.shared()
    statuses, policies = defaultdict(set), defaultdict(set)
    agents, systems, psns = defaultdict(set), defaultdict(set), defaultdict(set)
    fields = defaultdict(int)
    covered = 0

    # Read-only over the cache session_authorization fills. This dataset issues
    # no detail requests of its own, which is why the two share a cost pool.
    for mac in macs:
        detail = cache.get(mac)
        if not detail:
            continue
        covered += 1
        for field in (
            "posture_status",
            "posture_report",
            "agent_version",
            "operating_system",
            "step_latency",
            "total_authentication_latency",
        ):
            fields[field] += int(bool(detail[field]))
        owner = label(directory.ops_owner(detail["nas_ip"], detail["nad"]))
        status = canonical_status(detail["posture_status"])
        statuses[(status, owner)].add(mac)
        for psn in serving_psns.get(mac, ("unknown",)):
            psns[(psn, status)].add(mac)

        for policy, result in parse_posture_report(detail["posture_report"]):
            policies[(label(policy), label(result))].add(mac)

        if detail["agent_version"]:
            agents[label(detail["agent_version"])].add(mac)
        if detail["operating_system"]:
            systems[label(detail["operating_system"])].add(mac)

    for (status, owner), members in statuses.items():
        ctx.set(endpoints_by_status, len(members), status=status, ops_owner=owner)
    for (policy, result), members in policies.items():
        ctx.set(policy_results, len(members), policy=policy, result=result)
    for agent, members in agents.items():
        ctx.set(by_agent_version, len(members), agent_version=agent)
    for system, members in systems.items():
        ctx.set(by_os, len(members), os=system)
    for (psn, status), members in psns.items():
        ctx.set(by_psn, len(members), psn=psn, status=status)
    if covered:
        for field, populated in fields.items():
            ctx.set(field_coverage, populated / covered, field=field)


DATASET = Dataset(
    name="posture_current",
    description="Current posture and Secure Client state of active endpoints",
    default_interval=300,
    metrics=_METRICS,
    providers=(
        Provider(
            name="mnt",
            # The same cached per-MAC fan-out that answers session_authorization:
            # posture, OS, agent version and PSN come out of detail already
            # fetched, so this is pooled and charged once. Coverage converges to
            # the whole active set rather than sampling it.
            cost=Cost(target="mnt", requests=1, scales_with="sessions",
                      warmup_requests=2000, churn_fraction=0.01,
                      shares="mnt_session_detail"),
            supplies=frozenset({"status", "policy_result", "os", "agent_version", "psn"}),
            coverage="converging",
            fetch=fetch_mnt,
        ),
        Provider(
            name="pxgrid",
            cost=Cost(target="pxgrid", requests=0, streaming=True),
            supplies=frozenset({"status", "mdm"}),
            requires=("capability:pxgrid_session_topic",),
            fetch=fetch_pxgrid,
            notes="session postureStatus only; no per-policy PostureReport breakdown",
        ),
    ),
)
