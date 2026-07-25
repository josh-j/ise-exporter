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
from .session_authorization import CACHE, active_list, active_macs


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

_METRICS = (endpoints_by_status, policy_results, by_agent_version, by_os)

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


def fetch_mnt(ctx):
    cache = detail_cache.shared(CACHE)
    # Shared with session_authorization: one active-list read per tick, not two.
    listing = active_list(ctx)
    macs = active_macs(listing.get("sessions") or [])
    if not macs:
        return

    directory = nad_directory.shared()
    statuses, policies = defaultdict(set), defaultdict(set)
    agents, systems = defaultdict(set), defaultdict(set)

    # Read-only over the cache session_authorization fills. This dataset issues
    # no detail requests of its own, which is why the two share a cost pool.
    for mac in macs:
        detail = cache.get(mac)
        if not detail:
            continue
        owner = label(directory.ops_owner(detail["nas_ip"], detail["nad"]))
        statuses[(canonical_status(detail["posture_status"]), owner)].add(mac)

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
            notes="session postureStatus only; no per-policy PostureReport breakdown",
        ),
    ),
)
