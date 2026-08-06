"""Currently active RADIUS sessions.

Three sources with genuinely different semantics, which is why the active
provider is a label on the data and not only on health:

- ``pxgrid`` is the live session directory. Nearest to real time, cheapest
  steady-state cost (a change feed, not a scan). The session record names its
  serving node in ``providers`` (scalar spellings such as ``psnName`` on other
  releases), so the per-PSN breakdown is available here too.
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

from .. import nad_directory, reporting
from ..labels import label
from ..model import Cost, Dataset, Provider
from ..parsing import finite
from ..pxgrid import as_list, first, normalize_mac


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

VIEW = "radius_accounting"

# One materialised accounting scan with a dedup pass over the largest table in
# the MnT database -- the costliest recurring statement in v2, and it exceeds
# the transport's default attempt budget in production (DPY-4024); see
# reporting.statement_timeout_option for why the widened budget is a declared
# option rather than a constant.
OPTIONS = (reporting.statement_timeout_option(45),)


def fetch_pxgrid(ctx):
    sessions = ctx.transport.get_sessions(max_age=ctx.interval)
    directory = nad_directory.shared()
    psns, nads, owners = defaultdict(int), defaultdict(int), defaultdict(int)
    endpoints = set()
    matched = unmatched = 0

    for session in sessions:
        nas_ip = first(session, "nasIpAddress", "nas_ip_address")
        # nasIdentifier first: it is the field the session object actually
        # carries, and without it every session here fell back to the NAS IP and
        # counted as unmatched whenever the directory needed the name.
        device = first(
            session, "nasIdentifier", "nas_identifier", "nasName",
            "networkDeviceName", "network_device_name")
        classification = directory.lookup(nas_ip, device)
        if classification:
            matched += 1
            nad, location, owner = classification
        else:
            unmatched += 1
            nad = label(device or nas_ip, "unknown")
            location, owner = "Unknown", "unknown"
        # ISE names the serving node in `providers` and writes the string
        # "None" when there is nothing to name -- the same reading
        # pxgrid.project_session settled for the operator surface. The commit
        # clears every family in this dataset, so a fetch that never wrote
        # by_psn emptied the per-PSN panels wherever pxGrid was the healthy
        # preference.
        serving = [
            str(name).strip() for name in as_list(first(session, "providers"))
            if str(name).strip() and str(name).strip().lower() != "none"]
        psn = ", ".join(serving) or first(
            session, "psnName", "psn_name", "iseNode", "ise_node", "server")
        psns[label(psn, "unknown")] += 1
        nads[(nad, location)] += 1
        owners[owner] += 1
        mac = normalize_mac(first(
            session, "macAddress", "callingStationId", "calling_station_id"))
        if mac:
            endpoints.add(mac)

    nad_directory.record_attribution(matched, unmatched)
    ctx.set(total, len(sessions))
    ctx.set(unique_endpoints, len(endpoints))
    # The same empty-vs-failed distinction fetch_mnt keeps: a healthy empty
    # baseline must not render like an unavailable collector.
    if not sessions:
        ctx.set(by_psn, 0, psn="No active sessions")
    for psn, count in psns.items():
        ctx.set(by_psn, count, psn=psn)
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
    # A healthy empty ActiveList has no PSN rows from which to form a labelled
    # series.  Without an explicit zero both PSN panels render the same blank
    # state as a failed/unavailable collector.  Keep the zero inside the data
    # family so delta() also has a stable baseline on quiet deployments.
    if not sessions:
        ctx.set(by_psn, 0, psn="No active sessions")
    for psn, count in psns.items():
        ctx.set(by_psn, count, psn=psn)
    for (nad, location), count in nads.items():
        ctx.set(by_nad, count, nad=nad, location=location)
    for owner, count in owners.items():
        ctx.set(by_ops_owner, count, ops_owner=owner)


def _has(schema, column):
    columns = None if schema is None else schema.get(VIEW.upper())
    return columns is None or column.upper() in columns


def _shape(schema):
    """The column expressions this appliance's RADIUS_ACCOUNTING can support.

    Optional columns follow radius_accounting's discipline: a missing dimension
    degrades to ``unknown`` rather than taking down the counts that remain
    valid. NAS_IP_ADDRESS and TIMESTAMP are in every catalogue of this view.
    """
    status = (
        "UPPER(NVL(acct_status_type, 'UNKNOWN'))"
        if _has(schema, "acct_status_type")
        else "'UNKNOWN'"
    )
    psn = "NVL(ise_node, 'unknown')" if _has(schema, "ise_node") else "'unknown'"
    nad = (
        "NVL(device_name, nas_ip_address)"
        if _has(schema, "device_name")
        else "nas_ip_address"
    )
    mac = "calling_station_id" if _has(schema, "calling_station_id") else "NULL"
    # ACCT_SESSION_ID is the id the NAD repeats on the stop that closes its
    # start (RFC 2866); SESSION_ID is ISE's own. Either one, qualified by the
    # calling station, names a session well enough to pick its latest record.
    keys = [column for column in ("acct_session_id", "session_id")
            if _has(schema, column)]
    session = f"COALESCE({', '.join(keys)})" if len(keys) > 1 else (
        keys[0] if keys else "nas_ip_address")
    partition = f"{session}, {mac}" if mac != "NULL" else session
    # ID breaks the tie a same-timestamp start/stop pair leaves behind.
    order = "timestamp DESC" + (", id DESC" if _has(schema, "id") else "")
    return psn, nad, status, mac, partition, order


def statement(hours, limits, schema=None):
    """Live sessions reconstructed from one bounded accounting scan.

    The dedup pass keeps each session's latest record in the window; a session
    whose latest record is not a stop is live. GROUPING SETS then yields the
    per-PSN and per-NAD marginals and the grand total (with its exact distinct
    endpoint count) from that single pass, so the largest table in the MnT
    database is scanned once, not once per breakdown.
    """
    recent = reporting.recent("timestamp", hours, limits)
    psn, nad, status, mac, partition, order = _shape(schema)
    # Total first, then the handful of PSNs, then NADs busiest-first: the row
    # ceiling must truncate the one large dimension's tail, never the total or
    # the per-PSN rows behind it. publish_truncation reports whatever is cut.
    return f"""
        SELECT dimension, value, sessions, endpoints, nas_ip, group_total
        FROM (
            SELECT CASE WHEN GROUPING(psn) = 0 THEN 'psn'
                        WHEN GROUPING(nad) = 0 THEN 'nad'
                        ELSE 'total' END AS dimension,
                   NVL(COALESCE(psn, nad), 'all') AS value,
                   COUNT(*) AS sessions,
                   COUNT(DISTINCT mac) AS endpoints,
                   MAX(nas_ip) AS nas_ip,
                   COUNT(*) OVER () AS group_total
            FROM (
                SELECT {psn} AS psn, {nad} AS nad,
                       nas_ip_address AS nas_ip, {mac} AS mac,
                       {status} AS status,
                       ROW_NUMBER() OVER (
                           PARTITION BY {partition}
                           ORDER BY {order}) AS latest
                FROM {VIEW}
                WHERE {recent}
            )
            WHERE latest = 1 AND status NOT LIKE '%STOP%'
            GROUP BY GROUPING SETS ((psn), (nad), ())
        )
        ORDER BY CASE dimension WHEN 'total' THEN 0 WHEN 'psn' THEN 1 ELSE 2 END,
                 sessions DESC, value
        FETCH FIRST {reporting.group_limit(limits)} ROWS ONLY
    """


def fetch_dataconnect(ctx):
    hours = reporting.scan_window(ctx)
    schema = getattr(ctx.transport, "schema", None)
    results = ctx.transport.query_many(
        {"sessions": statement(hours, ctx.limits, schema)},
        timeout=ctx.option("statement_timeout"))
    rows = results.get("sessions", [])
    reporting.publish_truncation(ctx, "sessions", rows)

    directory = nad_directory.shared()
    psns, nads = defaultdict(int), defaultdict(int)
    matched = unmatched = 0
    sessions = 0.0
    endpoints = 0.0

    for row in rows:
        dimension = str(row.get("dimension") or "")
        count = finite(row.get("sessions"))
        if dimension == "total":
            sessions = count
            endpoints = finite(row.get("endpoints"))
        elif dimension == "psn":
            psns[label(row.get("value"), "unknown")] += count
        elif dimension == "nad":
            # The same attribution the other providers do, with the same
            # honesty when it fails: the group's NAS address (any one of the
            # device's addresses seen in the window) or its reported name
            # resolves through ERS inventory, and an unresolved device is
            # published as itself rather than dropped.
            classification = directory.lookup(row.get("nas_ip"), row.get("value"))
            if classification:
                matched += 1
                nad, location, _owner = classification
            else:
                unmatched += 1
                nad, location = label(row.get("value"), "unknown"), "Unknown"
            nads[(nad, location)] += count

    nad_directory.record_attribution(matched, unmatched)
    ctx.set(total, sessions)
    # COUNT(DISTINCT NULL) is a well-formed zero; a schema without the calling
    # station has no endpoint answer, and zero endpoints under live sessions
    # would be an invented one.
    if _has(schema, "calling_station_id"):
        ctx.set(unique_endpoints, endpoints)
    # The same empty-vs-failed distinction the other providers keep: a healthy
    # empty window (or its lone zero total row) must not render like an
    # unavailable collector.
    if not psns:
        ctx.set(by_psn, 0, psn="No active sessions")
    for psn, count in psns.items():
        ctx.set(by_psn, count, psn=psn)
    for (nad, location), count in nads.items():
        ctx.set(by_nad, count, nad=nad, location=location)


DATASET = Dataset(
    name="active_sessions",
    description="Currently active RADIUS sessions",
    default_interval=300,
    metrics=_METRICS,
    options=OPTIONS,
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
            supplies=frozenset({"session", "endpoint", "psn", "nad", "ops_owner"}),
            requires=("capability:pxgrid_session_topic",),
            fetch=fetch_pxgrid,
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
            supplies=frozenset({"session", "endpoint", "psn", "nad"}),
            requires=("view:RADIUS_ACCOUNTING",),
            fetch=fetch_dataconnect,
            notes="reconstructed from accounting; completeness depends on NAD start/stop records",
        ),
    ),
)
