"""Currently active RADIUS sessions.

Three sources with genuinely different semantics, which is why the active
provider is a label on the data and not only on health:

- ``pxgrid`` is the live session directory. Nearest to real time, cheapest
  steady-state cost (a change feed, not a scan), but it carries no owning-PSN
  field, so per-PSN breakdown is unavailable from this source alone.
- ``mnt`` is the current session store. One ActiveList read gives exact counts by
  PSN, and by NAD and ops owner as far as the inventory can resolve the NAS
  address -- the list names no network device, so an unresolved address is
  published as itself. The expensive per-endpoint detail is a different question
  and belongs to ``session_authorization``, which caches it.
- ``dataconnect`` reconstructs sessions from accounting starts minus stops. It
  has the freshness and completeness of whatever the NADs actually sent, and it
  is by far the most expensive of the three -- a 60-minute scan with a dedup
  pass over the largest table in the MnT database.

Prefer pxGrid, fall back to MnT, and treat Data Connect as the last resort. A
dashboard reading this dataset must gate on the active provider: switching from
pxGrid to Data Connect changes what "active" means.
"""
from collections import defaultdict

from prometheus_client import Gauge

from .. import nad_directory
from ..labels import label
from ..model import Cost, Dataset, Provider
from ..pxgrid import first, normalize_mac


total = Gauge("ise3_active_sessions_total", "Active RADIUS sessions", ["provider"])
by_psn = Gauge(
    "ise3_active_sessions_by_psn", "Active sessions per PSN", ["provider", "psn"])
by_nad = Gauge(
    "ise3_active_sessions_by_nad", "Active sessions per NAD",
    ["provider", "nad", "location"])
by_ops_owner = Gauge(
    "ise3_active_sessions_by_ops_owner", "Active sessions per ops owner",
    ["provider", "ops_owner"])
unique_endpoints = Gauge(
    "ise3_active_session_endpoints", "Distinct endpoints holding a session",
    ["provider"])

_METRICS = (total, by_psn, by_nad, by_ops_owner, unique_endpoints)


def fetch_pxgrid(ctx):
    sessions = ctx.transport.get_sessions(max_age=ctx.interval)
    directory = nad_directory.shared()
    nads, owners, endpoints = defaultdict(int), defaultdict(int), set()
    matched = unmatched = 0

    for session in sessions:
        nas_ip = first(session, "nasIpAddress", "nas_ip_address")
        device = first(
            session, "nasName", "networkDeviceName", "network_device_name")
        classification = directory.lookup(nas_ip, device)
        if classification:
            matched += 1
            nad, location, owner = classification
        else:
            unmatched += 1
            nad = label(device or nas_ip, "unknown")
            location, owner = "Unknown", "unknown"
        nads[(nad, location)] += 1
        owners[owner] += 1
        mac = normalize_mac(first(
            session, "macAddress", "callingStationId", "calling_station_id"))
        if mac:
            endpoints.add(mac)

    nad_directory.record_attribution(matched, unmatched)
    ctx.set(total, len(sessions))
    ctx.set(unique_endpoints, len(endpoints))
    for (nad, location), count in nads.items():
        ctx.set(by_nad, count, nad=nad, location=location)
    for owner, count in owners.items():
        ctx.set(by_ops_owner, count, ops_owner=owner)


def fetch_mnt(ctx):
    listing = ctx.transport.get_mnt_xml("/Session/ActiveList", api="mnt_active_list")
    sessions = listing.get("sessions") or []
    directory = nad_directory.shared()

    psns, nads, owners = defaultdict(int), defaultdict(int), defaultdict(int)
    endpoints = set()
    matched = unmatched = 0

    for session in sessions:
        # The NAS address is the only join key the ActiveList offers: it is a
        # session index -- user, MAC, NAS IP, framed IP, session ids and PSN --
        # and carries no network device name to fall back on.
        nas_ip = session.get("nas_ip_address")
        classification = directory.lookup(nas_ip)
        if classification:
            matched += 1
            nad, location, owner = classification
        else:
            # Counted, but not attributable. A session from a NAD that is not in
            # ERS inventory is a real finding, not a row to drop.
            unmatched += 1
            nad, location, owner = label(nas_ip, "unknown"), "Unknown", "unknown"

        psns[label(session.get("server"), "unknown")] += 1
        nads[(nad, location)] += 1
        owners[owner] += 1
        mac = normalize_mac(session.get("calling_station_id"))
        if mac:
            endpoints.add(mac)

    nad_directory.record_attribution(matched, unmatched)

    # ISE reports the count on the envelope; trust it over the row count, which
    # a truncated document would understate.
    ctx.set(total, listing.get("total") or len(sessions))
    ctx.set(unique_endpoints, len(endpoints))
    for psn, count in psns.items():
        ctx.set(by_psn, count, psn=psn)
    for (nad, location), count in nads.items():
        ctx.set(by_nad, count, nad=nad, location=location)
    for owner, count in owners.items():
        ctx.set(by_ops_owner, count, ops_owner=owner)


DATASET = Dataset(
    name="active_sessions",
    description="Currently active RADIUS sessions",
    default_interval=300,
    metrics=_METRICS,
    providers=(
        Provider(
            name="pxgrid",
            # Persistent STOMP/WSS subscription plus a periodic getSessions
            # re-poll for the bulk baseline the change feed does not replay. The
            # snapshot belongs to the transport, so posture_current's pxgrid
            # provider drives the same re-poll: pooled, and charged once at the
            # shorter of the two cadences.
            cost=Cost(target="pxgrid", requests=1, streaming=True,
                      shares="pxgrid_sessions"),
            supplies=frozenset({"session", "endpoint", "posture_status", "mdm"}),
            requires=("capability:pxgrid_session_topic",),
            fetch=fetch_pxgrid,
            notes="no owning-PSN field in the session object; PSN breakdown needs another source",
        ),
        Provider(
            name="mnt",
            # One ActiveList read. Session counts by NAD, PSN and ops owner are
            # exact and cheap; the expensive per-MAC detail belongs to
            # session_authorization and posture_current, which cache it.
            cost=Cost(target="mnt", requests=1),
            supplies=frozenset({"session", "endpoint", "psn", "nad", "ops_owner"}),
            fetch=fetch_mnt,
        ),
        Provider(
            name="dataconnect",
            # One materialised RADIUS_ACCOUNTING scan over the stale window with
            # a ROW_NUMBER dedup. The costliest recurring statement in v2.
            cost=Cost(target="oracle", db_seconds=5.0),
            supplies=frozenset({"session", "psn", "nad"}),
            requires=("view:RADIUS_ACCOUNTING",),
            notes="reconstructed from accounting; completeness depends on NAD start/stop records",
        ),
    ),
)
