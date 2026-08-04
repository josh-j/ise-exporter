"""Current posture of active endpoints.

Distinct from ``posture_history``: this is what is compliant right now, not what
was assessed over a reporting window. The two have separate metric families and
never substitute for one another.

- ``mnt`` reads the same cached per-session detail that answers
  ``session_authorization``, so it costs nothing extra and converges to the whole
  active set. Where ISE emits ``posture_status`` empty the endpoint is published
  as ``Unavailable`` rather than as ``NotApplicable``, which is a real ISE
  verdict and would read as "posture ran and did not apply".
- ``pxgrid`` carries ``postureStatus`` on the session object for free with the
  session stream, but not the per-policy report -- a cheaper, coarser answer.

The posture fields are empty on an estate with no Secure Client and populated on
one that runs posture, so they are read either way and
``ise3_session_detail_field_coverage`` reports which world you are in. They were
briefly deleted here after a lab with no posture module showed them absent on
every session; that is the difference between a field ISE cannot answer and a
field this deployment never exercises, and only the first justifies dropping a
lookup.

Keeping them was right, but the reason recorded here was not. It argued that
Data Connect naming the same facts in ``POSTURE_ASSESSMENT_BY_ENDPOINT``
confirmed the MnT session document emits them as elements. It does not: column
names on one surface say nothing about tag names on another, and on a
posture-enabled deployment these arrive as CamelCase *attributes* of
``other_attr_string``. ``session_detail.project`` reads both sides now. The lab
was never going to settle it either way -- an estate that does not exercise a
field cannot tell you where the field lives.
"""
import re
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
requirement_results = Gauge(
    "ise3_posture_requirement_results",
    "Distinct endpoints per posture requirement, its mandate and its result, "
    "parsed from PostureReport. A policy rolls its requirements up, so an Audit "
    "requirement that failed leaves the policy Passed and is invisible one level "
    "up; mandate is what separates a failure that denies access from one that "
    "only records",
    ["provider", "requirement", "mandate", "result"])
applicable_endpoints = Gauge(
    "ise3_posture_applicable_endpoints",
    "Distinct active endpoints by whether posture applies to them, from the "
    "session's own PostureApplicable. The denominator a compliance share needs, "
    "measured on the same population as the numerator -- ise3_posture_eligible_"
    "endpoints_total answers the same question from the endpoint inventory, "
    "over every endpoint rather than the connected ones",
    ["provider", "applicable", "ops_owner"])
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
    endpoints_by_status, policy_results, requirement_results,
    applicable_endpoints, by_agent_version, by_os, by_psn, field_coverage,
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
    """Collapse ISE's spelling variants so one verdict is one series.

    An absent or empty status is ``Unavailable``: every verdict in the map is
    something ISE decided, and answering "NotApplicable" for a field the
    appliance did not populate reports a decision that was never made.
    """
    key = str(value or "").strip().lower().replace(" ", "")
    if not key:
        return "Unavailable"
    return _CANONICAL_STATUS.get(key, str(value).strip())


# PostureReport, as a posture-enabled appliance actually writes it:
#
#   POLICY\;RESULT\;(REQ:MANDATE:RESULT:Passed_Conditions[..]:Failed_Conditions[..]
#   :Skipped_Conditions[..]\;REQ:...), POLICY\;RESULT\;(...)
#
# Policies are comma-separated. Every separator inside a policy is written as an
# escaped semicolon -- ``\;`` is the delimiter itself, not an escape for a
# literal one -- and the parenthesised tail holds that policy's requirements,
# separated the same way. Unescaping first and then splitting on ``;``, which is
# what this module used to do, therefore finds exactly one entry: the whole
# string, whose last colon splits a 1.4 KB policy name off a fragment of the
# final condition list. ``label()`` then truncates and hashes that into a
# distinct series per endpoint, so the family grows with the fleet.
_POLICY = re.compile(r"([^,;()]+);([^;()]+);\(([^)]*)\)")


def _unescaped(value):
    """The report with ISE's ``\\;`` delimiters normalised to plain ``;``."""
    return str(value or "").strip().replace("\\;", ";")


def parse_posture_report(value):
    """Yield ``(policy, result)`` pairs from ISE's PostureReport field."""
    for match in _POLICY.finditer(_unescaped(value)):
        policy, result = match.group(1).strip(), match.group(2).strip()
        if policy and result:
            yield policy, result


def parse_posture_requirements(value):
    """Yield ``(requirement, mandate, result)`` from ISE's PostureReport field.

    A policy rolls its requirements up, so a policy reading ``Passed`` can still
    contain a failed requirement -- an Audit requirement that failed does not
    change the verdict, and is invisible at the policy level. That is the case
    worth alerting on, and it only exists at this depth.
    """
    for match in _POLICY.finditer(_unescaped(value)):
        for entry in match.group(3).split(";"):
            fields = entry.strip().split(":", 3)
            if len(fields) < 3:
                continue
            requirement, mandate, result = (field.strip() for field in fields[:3])
            if requirement and mandate and result:
                yield requirement, mandate, result


def fetch_pxgrid(ctx):
    sessions = ctx.transport.get_sessions(max_age=ctx.interval)
    directory = nad_directory.shared()
    statuses = defaultdict(set)
    systems = defaultdict(set)
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
            # nasIdentifier is what the session object actually calls the device
            # by; the other spellings are not fields it carries. Reading only
            # those left every session attributable by NAS IP alone -- the same
            # empty-column failure project_session was fixed for.
            first(session, "nasIdentifier", "nas_identifier", "nasName",
                  "networkDeviceName", "network_device_name")))
        status = canonical_status(first(
            session, "postureStatus", "posture_status"))
        statuses[(status, owner)].add(identity)
        system = first(session, "endpointOperatingSystem",
                       "endpoint_operating_system")
        if system:
            systems[label(system)].add(identity)
        for field_name, keys in (
            ("posture_status", ("postureStatus", "posture_status")),
            ("mdm_registered", ("mdmRegistered", "mdm_registered")),
            ("mdm_compliant", ("mdmCompliant", "mdm_compliant")),
            ("operating_system", ("endpointOperatingSystem",
                                  "endpoint_operating_system")),
        ):
            fields[field_name] += int(bool(first(session, *keys)))

    for (status, owner), members in statuses.items():
        ctx.set(
            endpoints_by_status, len(members),
            status=status, ops_owner=owner)
    # The endpoint OS *is* a session-object field, so it is reported rather than
    # declared missing. The other two are genuinely MnT-only detail, and the
    # dashboard should say that explicitly rather than rendering two panels as
    # though their metric names or queries were broken.
    for system, members in systems.items():
        ctx.set(by_os, len(members), os=system)
    if not systems:
        ctx.set(by_os, 0, os="Not reported by active sessions")
    ctx.set(by_agent_version, 0, agent_version="Unavailable from pxGrid")
    ctx.set(
        policy_results, 0,
        policy="Unavailable from pxGrid", result="Failed")
    ctx.set(requirement_results, 0, requirement="Unavailable from pxGrid",
            mandate="Mandatory", result="Failed")
    ctx.set(applicable_endpoints, 0, applicable="Unavailable from pxGrid",
            ops_owner="unknown")
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
        # Empty is a successful current-state answer, not a missing answer.
        # Publish labelled zero states for the categorical dashboard families;
        # otherwise all four panels are indistinguishable from a broken MnT
        # read even though dataset_up and dataset_fresh are both true.
        ctx.set(by_agent_version, 0, agent_version="No active Secure Client sessions")
        ctx.set(by_os, 0, os="No active endpoints")
        ctx.set(
            policy_results, 0,
            policy="No active posture policies", result="Failed")
        ctx.set(requirement_results, 0,
                requirement="No active posture requirements",
                mandate="Mandatory", result="Failed")
        ctx.set(applicable_endpoints, 0,
                applicable="No active endpoints", ops_owner="unknown")
        return

    serving_psns = defaultdict(set)
    for session in sessions:
        mac = normalize_mac(session.get("calling_station_id"))
        if mac:
            serving_psns[mac].add(label(session.get("server"), "unknown"))

    directory = nad_directory.shared()
    statuses, psns = defaultdict(set), defaultdict(set)
    policies, requirements, agents, systems, applicable = (
        defaultdict(set), defaultdict(set), defaultdict(set),
        defaultdict(set), defaultdict(set))
    fields = defaultdict(int)
    covered = 0

    # Read-only over the cache session_authorization fills. This dataset issues
    # no detail requests of its own, which is why the two share a cost pool.
    for mac in macs:
        detail = cache.get(mac)
        if not detail:
            continue
        covered += 1
        # Coverage is the honest signal for the posture fields: an estate with
        # no Secure Client reports 0.0 and reads as "nothing runs posture here",
        # which is true, while a deployment that runs it reports what it has.
        for field in (
            "posture_status",
            "posture_report",
            "posture_applicable",
            "agent_version",
            "operating_system",
            "step_latency",
            "total_authentication_latency",
        ):
            fields[field] += int(bool(detail[field]))
        owner = label(directory.ops_owner(detail["nas_ip"], detail["nad"]))
        status = canonical_status(detail["posture_status"])
        statuses[(status, owner)].add(mac)
        if detail["posture_applicable"]:
            applicable[(label(detail["posture_applicable"]), owner)].add(mac)
        for psn in serving_psns.get(mac, ("unknown",)):
            psns[(psn, status)].add(mac)

        for policy, result in parse_posture_report(detail["posture_report"]):
            policies[(label(policy), label(result))].add(mac)
        for requirement, mandate, result in parse_posture_requirements(
                detail["posture_report"]):
            requirements[
                (label(requirement), label(mandate), label(result))].add(mac)
        if detail["agent_version"]:
            agents[label(detail["agent_version"])].add(mac)
        if detail["operating_system"]:
            systems[label(detail["operating_system"])].add(mac)

    if not covered:
        # Publishing here would clear every posture family and republish nothing
        # under dataset_up=1: a compliance panel would read "no non-compliant
        # endpoints" from a dataset that is not collecting. nad_health refuses
        # for the same reason, and the previous snapshot survives instead.
        ctx.fail("dependency_pending",
                 "no cached session detail for any of the %d active MACs; "
                 "session_authorization fills the mnt_session_detail cache and "
                 "has not populated it yet (or is disabled), so posture cannot "
                 "be distinguished from an empty estate" % len(macs))

    for (status, owner), members in statuses.items():
        ctx.set(endpoints_by_status, len(members), status=status, ops_owner=owner)
    for (psn, status), members in psns.items():
        ctx.set(by_psn, len(members), psn=psn, status=status)
    for (policy, result), members in policies.items():
        ctx.set(policy_results, len(members), policy=policy, result=result)
    if not policies:
        ctx.set(
            policy_results, 0,
            policy="No reported posture policies", result="Failed")
    for (requirement, mandate, result), members in requirements.items():
        ctx.set(requirement_results, len(members),
                requirement=requirement, mandate=mandate, result=result)
    if not requirements:
        ctx.set(requirement_results, 0,
                requirement="No reported posture requirements",
                mandate="Mandatory", result="Failed")
    for (answer, owner), members in applicable.items():
        ctx.set(applicable_endpoints, len(members),
                applicable=answer, ops_owner=owner)
    if not applicable:
        ctx.set(applicable_endpoints, 0,
                applicable="Not reported by active sessions",
                ops_owner="unknown")
    for agent, members in agents.items():
        ctx.set(by_agent_version, len(members), agent_version=agent)
    if not agents:
        ctx.set(by_agent_version, 0, agent_version="Not reported by active sessions")
    for system, members in systems.items():
        ctx.set(by_os, len(members), os=system)
    if not systems:
        ctx.set(by_os, 0, os="Not reported by active sessions")
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
            # the posture status and the serving PSN come out of detail already
            # fetched, so this is pooled and charged once. Coverage converges to
            # the whole active set rather than sampling it.
            cost=Cost(target="mnt", requests=1, scales_with="sessions",
                      warmup_requests=2000, churn_fraction=0.01,
                      churn_interval=300,
                      shares="mnt_session_detail", pool_reader=True),
            supplies=frozenset({
                "status", "policy_result", "os", "agent_version", "psn"}),
            coverage="converging",
            fetch=fetch_mnt,
        ),
        Provider(
            name="pxgrid",
            # get_sessions re-baselines the whole session snapshot once it is
            # older than this dataset's interval, which is the same unpaged
            # getSessions active_sessions declares. One shared snapshot, so one
            # pooled charge at whichever member's cadence is shorter.
            cost=Cost(target="pxgrid", requests=1, streaming=True,
                      shares="pxgrid_sessions"),
            supplies=frozenset({"status", "mdm", "os"}),
            requires=("capability:pxgrid_session_topic",),
            fetch=fetch_pxgrid,
            notes="session postureStatus and endpointOperatingSystem; no "
                  "per-policy PostureReport breakdown and no agent version",
        ),
    ),
)
