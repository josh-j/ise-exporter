"""Endpoint totals, profiles, and identity-group membership.

- ``dataconnect`` aggregates ``ENDPOINTS_DATA`` in the database and returns
  grouped rows. Cost is nearly flat in fleet size because the work happens
  server-side, which is why it is preferred at 100,000 endpoints.
- ``ers`` enumerates the endpoint list. Correct, but ERS caps page size at 100,
  so a 100,000-endpoint estate is ~1,000 PAN requests per collection. Present for
  deployments without Data Connect, not as a routine choice.
- ``pxgrid`` getEndpoints is one request, but the pxGrid endpoint directory only
  carries endpoints with live published profiling context -- it is not a mirror of
  the endpoint database, and it returned zero on both this lab and production.
"""
from prometheus_client import Gauge

from .. import reporting
from ..model import Cost, Dataset, Provider
from ..parsing import finite
from ..pxgrid import endpoint_attribute


endpoints_total = Gauge(
    "ise3_endpoints_total", "Endpoints in the ISE endpoint database", ["provider"])
by_profile = Gauge(
    "ise3_endpoints_by_profile", "Endpoints per endpoint policy",
    ["provider", "profile"])
by_identity_group = Gauge(
    "ise3_endpoints_by_identity_group", "Endpoints per identity group",
    ["provider", "identity_group"])
posture_applicable = Gauge(
    "ise3_endpoints_posture_applicable",
    "Endpoints subject to a posture policy. Being eligible for assessment is "
    "not the same as having been assessed, nor as being compliant",
    ["provider", "applicable"])
unprofiled = Gauge(
    "ise3_endpoints_unprofiled",
    "Endpoints with no useful endpoint policy", ["provider"])
field_coverage = Gauge(
    "ise3_endpoint_inventory_field_coverage",
    "Fraction of endpoint rows carrying each operational inventory field",
    ["provider", "field"])

_METRICS = (endpoints_total, by_profile, by_identity_group, posture_applicable,
            unprofiled, field_coverage)

VIEW = "endpoints_data"

# ENDPOINTS_DATA exposes IDENTITY_GROUP_ID, not IDENTITY_GROUP. Verified against
# the live 3.3 P11 catalogue.
DIMENSIONS = (
    ("profile", "NVL(endpoint_policy, 'Unknown')"),
    ("identity_group", "NVL(identity_group_id, 'none')"),
)


def statements(limits):
    """One aggregate pass; the endpoint snapshot is not an event history.

    No time bound here on purpose: ENDPOINTS_DATA is current-state, so a window
    would silently exclude endpoints that simply have not changed recently.
    """
    return {
        "totals": f"""
            SELECT COUNT(*) AS endpoints,
                   SUM(CASE WHEN NVL(posture_applicable, 0) = 1
                            THEN 1 ELSE 0 END) AS posture_yes,
                   SUM(CASE WHEN NVL(posture_applicable, 0) = 1
                            THEN 0 ELSE 1 END) AS posture_no,
                   SUM(CASE WHEN TRIM(endpoint_policy) IS NULL
                             OR LOWER(TRIM(endpoint_policy))
                                IN ('unknown', 'none', 'missing')
                            THEN 1 ELSE 0 END) AS unprofiled,
                   SUM(CASE WHEN TRIM(endpoint_policy) IS NOT NULL
                            THEN 1 ELSE 0 END) AS profile_present,
                   SUM(CASE WHEN TRIM(identity_group_id) IS NOT NULL
                            THEN 1 ELSE 0 END) AS identity_group_present,
                   SUM(CASE WHEN posture_applicable IS NOT NULL
                            THEN 1 ELSE 0 END) AS posture_applicable_present
            FROM {VIEW}
        """,
        # Marginals rather than two capped top-K statements: complete, one
        # scan, and it avoids ordering by an aggregate outside its own GROUP BY
        # (ORA-00937), which is what the capped form did.
        "marginals": reporting.marginals(VIEW, "1 = 1", DIMENSIONS,
                                         "COUNT(*) AS endpoints", limits=limits),
    }


def fetch(ctx):
    results = ctx.transport.query_many(statements(ctx.limits))

    for row in results.get("totals", []):
        ctx.set(endpoints_total, finite(row.get("endpoints")))
        ctx.set(unprofiled, finite(row.get("unprofiled")))
        ctx.set(posture_applicable, finite(row.get("posture_yes")), applicable="yes")
        ctx.set(posture_applicable, finite(row.get("posture_no")), applicable="no")
        total = finite(row.get("endpoints"))
        for field, column in (
            ("profile", "profile_present"),
            ("identity_group", "identity_group_present"),
            ("posture_applicable", "posture_applicable_present"),
        ):
            ctx.set(
                field_coverage,
                finite(row.get(column)) / total if total else 1.0,
                field=field,
            )

    rows = results.get("marginals", [])
    reporting.publish_truncation(ctx, "marginals", rows)
    gauges = {"profile": (by_profile, "profile"),
              "identity_group": (by_identity_group, "identity_group")}
    # IDENTITY_GROUP_ID is present in ENDPOINTS_DATA and NULL on every row of a
    # 3.3 P11 appliance; real group names live in ENDPOINT_IDENTITY_GROUPS,
    # which this statement does not join, so the breakdown is withheld rather
    # than published as one 'none' bucket holding the whole fleet.
    for dimension, entries in reporting.live_dimensions(ctx, rows).items():
        target = gauges.get(dimension)
        if target is None:
            continue
        gauge, label_name = target
        for row in entries:
            (value,) = reporting.group(row, "value")
            ctx.set(gauge, finite(row.get("endpoints")), **{label_name: value})


def fetch_pxgrid(ctx):
    endpoints = ctx.transport.get_endpoints(max_age=ctx.interval)
    profiles = {}
    profile_present = 0
    unprofiled_count = 0
    for endpoint in endpoints:
        profile = str(endpoint_attribute(
            endpoint, "endPointPolicy", "EndPointPolicy",
            "mfcInfoEndpointPolicy", "MFCInfoEndpointPolicy") or "").strip()
        if profile:
            profile_present += 1
        else:
            profile = "Unknown"
            unprofiled_count += 1
        profiles[profile] = profiles.get(profile, 0) + 1

    ctx.set(endpoints_total, len(endpoints))
    ctx.set(unprofiled, unprofiled_count)
    ctx.set(
        field_coverage,
        profile_present / len(endpoints) if endpoints else 1.0,
        field="profile")
    for profile, count in profiles.items():
        ctx.set(by_profile, count, profile=profile)


DATASET = Dataset(
    name="endpoint_inventory",
    description="Endpoint totals, profiles, identity groups",
    default_interval=21600,
    metrics=_METRICS,
    providers=(
        Provider(
            name="dataconnect",
            cost=Cost(target="oracle", db_seconds=6.0, scales_with="endpoints",
                      db_seconds_per_1k=0.02),
            supplies=frozenset({
                "total", "profile", "identity_group", "posture_applicable"}),
            requires=("view:ENDPOINTS_DATA",),
            notes="reads only attributes Cisco documents as updated in real "
                  "time (profile, identity group, posture applicability), so "
                  "its cadence is honest; most other ENDPOINTS_DATA columns "
                  "synchronize up to 12h behind and are not worth polling",
            fetch=fetch,
        ),
        Provider(
            name="ers",
            cost=Cost(target="pan", requests=2, scales_with="endpoints",
                      requests_per_1k=10),
            supplies=frozenset({"total", "profile"}),
            notes="ERS caps page size at 100, so this is ~1000 PAN requests at "
                  "100k endpoints; prefer Data Connect where it is available",
        ),
        Provider(
            name="pxgrid",
            # getEndpoints is paged at 1,000 rows. The transport cache is shared
            # with endpoint_attributes, so both datasets charge this pool once.
            cost=Cost(target="pxgrid", requests=1, scales_with="endpoints",
                      requests_per_1k=1, shares="pxgrid_endpoints"),
            supplies=frozenset({"total", "profile"}),
            coverage="bounded",
            requires=("capability:pxgrid_endpoints",),
            fetch=fetch_pxgrid,
            notes="the pxGrid endpoint directory is not a mirror of the endpoint "
                  "database: it carries only endpoints with live published "
                  "profiling context, so the total is not a fleet total",
        ),
    ),
)
