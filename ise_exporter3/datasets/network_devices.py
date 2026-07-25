"""NAD inventory and Network Device Group classification.

ERS is the only authoritative source for configured NADs and their group
assignment -- Data Connect reports activity, not configuration. This is also
where `ops_owner` comes from, which every other dashboard groups by, so it is
effectively mandatory.

**Group membership is not in the list response.** `/config/networkdevice`
returns identity and a link; `NetworkDeviceGroupList` requires one detail request
per device. Some ISE builds do include it in the list, so that is used when
present, but the general case is a per-NAD fan-out -- 5,000 requests on a large
estate, which is why this converges rather than sampling.

Group membership is about as stable as ISE configuration gets: it changes when
someone edits a device, not continuously. So each NAD is fetched once and cached
for a long TTL, the per-cycle budget limits how fast a cold cache fills, and
coverage reaches the whole inventory. Getting this wrong is a documented v2
symptom -- "NAD Group Detail coverage stuck at 10%" on a ~4,000-NAD estate.

Group strings look like ``Location#All Locations#Germany#Ramstein AB``. The
category and the ``All X`` root are dropped; location keeps its remaining path
(sites nest), while ops owner and device type take the leaf.
"""
from collections import defaultdict

from prometheus_client import Gauge

from .. import detail_cache, nad_directory
from ..labels import label
from ..model import Cost, Dataset, Provider


CACHE = "ers_network_device"
# Group membership changes on configuration edits, so a long TTL is honest.
# A NAD moved between groups is picked up within this window.
CACHE_TTL_SECONDS = 7 * 86400
# Per-cycle warm-up ceiling. 500 paced ERS requests keep a cold start off the
# PAN for long stretches; 5,000 NADs converge in ten cycles.
WARMUP_FETCHES_PER_CYCLE = 500

total = Gauge("ise3_network_devices_total", "Configured network devices", ["provider"])
by_ops_owner = Gauge(
    "ise3_network_devices_by_ops_owner", "Network devices per ops owner",
    ["provider", "ops_owner"])
by_location = Gauge(
    "ise3_network_devices_by_location", "Network devices per location",
    ["provider", "location"])
by_device_type = Gauge(
    "ise3_network_devices_by_type", "Network devices per device type",
    ["provider", "device_type"])
assignment = Gauge(
    "ise3_network_device_assignment",
    "Group assignment for one network device, from normalized ISE NDGs",
    ["provider", "nad", "location", "ops_owner", "device_type"])
classified = Gauge(
    "ise3_network_devices_classified",
    "Devices whose group detail is cached; compare with the total to see how "
    "much of the inventory is classified", ["provider"])

_METRICS = (total, by_ops_owner, by_location, by_device_type, assignment, classified)

# A deployment with more NADs than this is outside what the exporter has been
# tested against; publishing a per-NAD series for each would be row storage.
MAX_DEVICES = 10_000


def device_addresses(record):
    """NAS IPs ISE will report for this device, for the session join."""
    addresses = record.get("NetworkDeviceIPList") if isinstance(record, dict) else None
    found = []
    for entry in addresses or ():
        if isinstance(entry, dict):
            value = entry.get("ipaddress") or entry.get("ipAddress")
            if value:
                found.append(str(value).strip())
    return found


def classify(groups):
    """Return (location, ops_owner, device_type) from an ISE group list."""
    # "Unknown" with a capital U for location so a NAD with no Location group
    # shares a series with location-less data rather than splitting into a
    # separate lowercase bucket.
    location, ops_owner, device_type = "Unknown", "unknown", "unknown"
    for group in groups or ():
        parts = str(group).split("#")
        if len(parts) <= 2:
            continue
        if parts[0] == "Location":
            location = "#".join(parts[2:])
        elif parts[0] == "Ops Owner":
            ops_owner = parts[-1]
        elif parts[0] == "Device Type":
            device_type = parts[-1]
    return label(location, "Unknown"), label(ops_owner), label(device_type)


def _groups_of(record):
    groups = record.get("NetworkDeviceGroupList")
    return groups if isinstance(groups, list) else None


def warm(ctx, cache, devices):
    """Fetch group detail for uncached devices, up to this cycle's budget."""
    outstanding = [device["id"] for device in devices
                   if cache.get(device["id"]) is None]
    for device_id in outstanding[:WARMUP_FETCHES_PER_CYCLE]:
        try:
            raw = ctx.transport.get_ers(
                f"/config/networkdevice/{device_id}", api="ers_device_detail")
        except Exception:       # noqa: BLE001 - one device must not fail the set
            cache.count("failed")
            continue
        record = raw.get("NetworkDevice") if isinstance(raw, dict) else None
        groups = _groups_of(record) if isinstance(record, dict) else None
        if groups is None:
            cache.count("empty")
            continue
        # Retain only the normalized classification and the addresses needed for
        # the session join -- never the device's authentication settings. A small
        # constant row regardless of how many groups ISE returns.
        cache.put(device_id, classify(groups) + (tuple(device_addresses(record)),))
        cache.count("fetched")
    return max(0, len(outstanding) - WARMUP_FETCHES_PER_CYCLE)


def fetch(ctx):
    devices = ctx.transport.get_ers(
        "/config/networkdevice", params={"size": 100}, get_all=True, api="ers_nads")
    if devices is None:
        ctx.fail("invalid_response", "the network device enumeration failed")
    if len(devices) > MAX_DEVICES:
        ctx.fail("response_too_large",
                 f"{len(devices)} network devices exceeds the {MAX_DEVICES} ceiling")

    identified, seen = [], set()
    for device in devices:
        if not isinstance(device, dict):
            ctx.fail("invalid_response", "a network device entry was not an object")
        device_id = str(device.get("id") or "").strip()
        name = label(device.get("name"), "unknown")
        if not device_id or device_id in seen:
            continue        # ERS can repeat a row across page boundaries
        seen.add(device_id)
        identified.append({"id": device_id, "name": name, "row": device})

    cache = detail_cache.shared(CACHE, ttl_seconds=CACHE_TTL_SECONDS)
    cache.retain({device["id"] for device in identified})

    # Some ISE builds return the group list inline. Take it for free when
    # offered rather than spending a detail request on it.
    for device in identified:
        groups = _groups_of(device["row"])
        if groups is not None and cache.get(device["id"]) is None:
            cache.put(device["id"],
                      classify(groups) + (tuple(device_addresses(device["row"])),))
            cache.count("inline")

    outstanding = warm(ctx, cache, identified)
    cache.publish(len(identified), deferred_count=outstanding)

    owners, locations, types = defaultdict(int), defaultdict(int), defaultdict(int)
    resolved, directory = 0, []
    for device in identified:
        classified_as = cache.get(device["id"])
        if classified_as is None:
            # Not yet classified. Counting it into a bucket would attribute it
            # to "unknown" and make an unclassified NAD indistinguishable from
            # one genuinely missing its groups.
            continue
        resolved += 1
        location, ops_owner, device_type, addresses = classified_as
        owners[ops_owner] += 1
        locations[location] += 1
        types[device_type] += 1
        ctx.set(assignment, 1, nad=device["name"], location=location,
                ops_owner=ops_owner, device_type=device_type)
        # Session data arrives keyed by NAS IP, and sometimes by device name.
        # Publish both so the join works either way.
        directory.append({
            "nad": device["name"], "location": location, "ops_owner": ops_owner,
            "keys": (*addresses, device["name"]),
        })

    # Replace rather than merge: a NAD removed from ERS must stop attributing.
    nad_directory.shared().replace(directory)

    ctx.set(total, len(identified))
    ctx.set(classified, resolved)
    for owner, count in owners.items():
        ctx.set(by_ops_owner, count, ops_owner=owner)
    for location, count in locations.items():
        ctx.set(by_location, count, location=location)
    for device_type, count in types.items():
        ctx.set(by_device_type, count, device_type=device_type)


DATASET = Dataset(
    name="network_devices",
    description="NAD inventory and Network Device Group classification",
    default_interval=21600,
    metrics=_METRICS,
    providers=(
        Provider(
            name="ers",
            # Enumeration pages at the 100-row ERS cap, then one detail request
            # per uncached device. Steady state is near zero: group membership
            # changes on a configuration edit, not on a cadence.
            cost=Cost(target="pan", scales_with="nads", requests_per_1k=10,
                      warmup_requests=WARMUP_FETCHES_PER_CYCLE,
                      churn_fraction=0.001),
            supplies=frozenset({"nad", "location", "ops_owner", "device_type"}),
            coverage="converging",
            fetch=fetch,
        ),
    ),
)
