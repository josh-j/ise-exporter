"""Series cardinality at the declared production scale.

A cap that bites at the size the exporter is built for is not a safety limit, it
is a bug with a constant in front of it. These tests measure what each dataset
would publish at 5,000 NADs / 100,000 endpoints / 20,000 sessions and hold the
line on what a series may be keyed on.

The rule is about the *key*, not the number: NAD-keyed is legitimate -- dead
switch detection needs one series per switch -- and endpoint-keyed is not, because
that is row storage wearing a metric's clothes.
"""
import pytest

from ise_exporter3 import datasets as registry
from ise_exporter3 import limits as limits_module
from ise_exporter3.model import Scale

NADS, ENDPOINTS, SESSIONS = 5_000, 100_000, 20_000

# The ceilings a production-profile configuration resolves to. Read from the
# derivation rather than restated here, so a test cannot pass against numbers
# the exporter stopped using.
PRODUCTION = limits_module.for_scale(
    Scale(nads=NADS, endpoints=ENDPOINTS, sessions=SESSIONS, accounts=1_000))
MAX_SNAPSHOT_SAMPLES = PRODUCTION.snapshot_samples
SOFT_SAMPLE_WARNING = PRODUCTION.snapshot_sample_warning

# Labels whose value space is the fleet. A series keyed on one of these grows
# with the estate, so each is judged against what it is allowed to grow to.
FLEET_LABELS = {
    "nad": NADS,
    "endpoint": ENDPOINTS,
    "mac": ENDPOINTS,
    "mac_address": ENDPOINTS,
    "calling_station_id": ENDPOINTS,
    "session": SESSIONS,
    "username": 1_000,       # internal Device Admin accounts, a human-sized set
}

# What a single dataset may key on. Endpoint identity is the line: at 100,000
# endpoints any per-endpoint series is storage, not monitoring.
FORBIDDEN_KEYS = {"endpoint", "mac", "mac_address", "calling_station_id", "session"}


def _families():
    for dataset in registry.all_datasets():
        for family in dataset.metrics:
            yield dataset, family


def test_no_metric_is_keyed_on_an_endpoint_identity():
    # The invariant the sample ceiling is really protecting. A MAC-keyed series
    # is 100,000 series that Prometheus must store forever and no dashboard can
    # usefully render.
    offenders = [
        f"{dataset.name}/{family._name}: {sorted(set(family._labelnames) & FORBIDDEN_KEYS)}"
        for dataset, family in _families()
        if set(family._labelnames) & FORBIDDEN_KEYS]
    assert not offenders, f"endpoint-keyed metrics: {offenders}"


def test_no_metric_crosses_two_fleet_dimensions():
    # nad x endpoint, or nad x username, multiplies two large spaces together.
    # One fleet dimension per series is the affordable shape.
    offenders = []
    for dataset, family in _families():
        fleet = [name for name in family._labelnames if name in FLEET_LABELS]
        if len(fleet) > 1:
            offenders.append(f"{dataset.name}/{family._name}: {fleet}")
    assert not offenders, f"metrics crossing two fleet dimensions: {offenders}"


def test_the_hard_ceiling_accommodates_the_declared_scale():
    # nad_health legitimately publishes a status pair plus an age per switch.
    # A ceiling below that would fail at exactly the size this is built for.
    nad_health_series = NADS * 2 + NADS + 2
    assert nad_health_series < MAX_SNAPSHOT_SAMPLES
    assert SOFT_SAMPLE_WARNING < MAX_SNAPSHOT_SAMPLES


def test_nad_health_stays_inside_the_ceiling_at_five_thousand_switches():
    """Measured, not estimated: multiplying out the label space assumes labels
    are independent, and most are not -- a NAD's location and owner are
    determined by the NAD, so they add nothing to the count."""
    from prometheus_client import REGISTRY

    from ise_exporter3 import nad_directory
    from ise_exporter3.config import Config
    from ise_exporter3.datasets import nad_health
    from ise_exporter3.plan import PlannedDataset
    from ise_exporter3.runtime import Runner
    from ise_exporter3.transports import Transport

    class Oracle(Transport):
        target = "oracle"

        def query(self, sql, parameters=None, *, adaptive=True):
            return [{"nad": f"sw-{index:05d}", "passed": 10, "failed": 1,
                     "age_seconds": 60, "group_total": NADS}
                    for index in range(NADS)]

    nad_directory.shared().replace([
        {"nad": f"sw-{index:05d}", "location": "site", "ops_owner": "team",
         "keys": (f"10.0.{index // 256}.{index % 256}",)}
        for index in range(NADS)])

    config = Config.from_document(
        {"targets": {"pan": {"host": "p", "user": "u"},
                     "oracle": {"host": "m", "user": "dataconnect",
                                "service": "cpm10"}}},
        path="test.toml",
        environ={"ISE_PASS": "x", "ISE_DATACONNECT_PASSWORD": "y"})
    entry = PlannedDataset(
        name="nad_health", description="", enabled=True, interval=21600,
        dataset=nad_health.DATASET, provider=nad_health.DATASET.providers[0])

    outcome = Runner(config).run(entry, Oracle())
    assert outcome.ok, outcome.detail
    published = REGISTRY.get_sample_value(
        "ise3_dataset_series", {"dataset": "nad_health"})
    assert published <= MAX_SNAPSHOT_SAMPLES
    # A status pair plus an age per switch, and nothing per switch that
    # Prometheus could have derived.
    assert published == pytest.approx(NADS * 3, rel=0.01)
    nad_directory.shared().replace(())


def test_session_authorization_scales_with_owners_not_with_switches():
    """The cross-product bug this dataset used to have: keying every dimension
    on the NAD made its series count grow with the estate rather than with the
    number of questions anyone asks."""
    from prometheus_client import REGISTRY

    from ise_exporter3 import detail_cache, nad_directory
    from ise_exporter3.config import Config
    from ise_exporter3.datasets import session_authorization as authz
    from ise_exporter3.plan import PlannedDataset
    from ise_exporter3.runtime import Runner
    from ise_exporter3.transports import Transport

    switches, owners, macs = 500, 20, 2_000

    class MnT(Transport):
        target = "mnt"

        def get_mnt_xml(self, path, *, api="mnt_xml"):
            if path.endswith("/ActiveList"):
                return {"total": macs, "sessions": [
                    {"calling_station_id": f"00:11:22:{i // 65536:02X}:"
                                           f"{(i // 256) % 256:02X}:{i % 256:02X}"}
                    for i in range(macs)]}
            index = int(path[-2:], 16)
            switch = index % switches
            return {"total": 0, "sessions": [{
                "calling_station_id": path.rsplit("/", 1)[-1],
                "network_device_name": f"sw-{switch:05d}",
                "nas_ip_address": f"10.0.{switch // 256}.{switch % 256}",
                "passed": "true",
                "authentication_method": "dot1x",
                "selected_azn_profiles": "PermitAccess",
                "other_attr_string":
                    ":!:ISEPolicySetName=Wired:!:"
                    "AuthorizationPolicyMatchedRule=Basic:!:",
            }]}

    for cache in (authz.CACHE, authz.ACTIVE_LIST_CACHE):
        detail_cache._CACHES.pop(cache, None)
    nad_directory.shared().replace([
        {"nad": f"sw-{index:05d}", "location": "site",
         "ops_owner": f"team-{index % owners}",
         "keys": (f"10.0.{index // 256}.{index % 256}",)}
        for index in range(switches)])

    config = Config.from_document(
        {"targets": {"pan": {"host": "p", "user": "u"}, "mnt": {"host": "m"}}},
        path="test.toml", environ={"ISE_PASS": "x"})
    entry = PlannedDataset(
        name=authz.DATASET.name, description="", enabled=True, interval=300,
        dataset=authz.DATASET, provider=authz.DATASET.providers[0])

    assert Runner(config).run(entry, MnT()).ok
    published = REGISTRY.get_sample_value(
        "ise3_dataset_series", {"dataset": "session_authorization"})

    # Owner-keyed dimensions contribute a handful each; only the deliberate
    # per-switch policy-set series scales with the estate. If this ever grows
    # with `switches x dimensions` again, the cross product is back.
    assert published < switches * 3, (
        f"{published} series for {switches} switches looks like a cross product")
    for cache in (authz.CACHE, authz.ACTIVE_LIST_CACHE):
        detail_cache._CACHES.pop(cache, None)
    nad_directory.shared().replace(())


def test_the_runner_reports_how_wide_each_dataset_is():
    # Growth has to be visible before the ceiling arrives, otherwise the first
    # sign of trouble is a dataset that stops publishing.
    from prometheus_client import REGISTRY

    from ise_exporter3.config import Config
    from ise_exporter3.plan import PlannedDataset
    from ise_exporter3.runtime import Runner
    from ise_exporter3.datasets import deployment
    from tests.test_v3_datasets import DEPLOYMENT_OK, FakeTransport

    config = Config.from_document(
        {"targets": {"pan": {"host": "pan1", "user": "ro"}}},
        path="test.toml", environ={"ISE_PASS": "secret"})
    entry = PlannedDataset(
        name="deployment", description="", enabled=True, interval=300,
        dataset=deployment.DATASET, provider=deployment.DATASET.providers[0])
    assert Runner(config).run(entry, FakeTransport(DEPLOYMENT_OK)).ok
    assert REGISTRY.get_sample_value(
        "ise3_dataset_series", {"dataset": "deployment"}) > 0


# --- convergence arithmetic -------------------------------------------------

def _cache_ttl(dataset_name):
    module = __import__(f"ise_exporter3.datasets.{dataset_name}", fromlist=["x"])
    return getattr(module, "CACHE_TTL_SECONDS", 86_400)


def _converging():
    for dataset in registry.all_datasets():
        for provider in dataset.providers:
            if provider.cost.converges:
                yield dataset, provider


def test_every_cache_finishes_warming_before_its_entries_expire():
    """A cache whose TTL expires before the sweep comes round never converges.

    Coverage plateaus at (warm rate x TTL) / fleet and stays there, which looks
    exactly like a sampling cap -- the failure this whole pattern exists to
    remove. Both offenders found this way were arithmetic, not design:
    endpoint_attributes needed 300 hours against a 24-hour TTL, and tacacs_config
    declared the wrong scale dimension so it believed it needed 50 cycles rather
    than 10.
    """
    from ise_exporter3.config import PROFILES
    from ise_exporter3.model import Scale

    scale = Scale(**PROFILES["production"]["scale"])
    broken = []
    for dataset, provider in _converging():
        warm_seconds = provider.cost.cycles_to_warm(scale) * dataset.default_interval
        ttl = _cache_ttl(dataset.name)
        if warm_seconds >= ttl:
            broken.append(
                f"{dataset.name}/{provider.name}: {warm_seconds / 3600:.0f}h to warm "
                f"but entries expire after {ttl / 3600:.0f}h")
    assert not broken, f"caches that can never converge: {broken}"


def test_every_converging_cache_warms_in_an_operationally_useful_time():
    # Converging eventually is not the same as converging usefully. A week to
    # first full coverage would mean an operator never sees a complete answer
    # inside an incident.
    from ise_exporter3.config import PROFILES
    from ise_exporter3.model import Scale

    scale = Scale(**PROFILES["production"]["scale"])
    slow = []
    for dataset, provider in _converging():
        hours = provider.cost.cycles_to_warm(scale) * dataset.default_interval / 3600
        if hours > 72:
            slow.append(f"{dataset.name}/{provider.name}: {hours:.0f}h")
    assert not slow, f"caches slower than three days to full coverage: {slow}"


def test_a_dataset_declares_the_dimension_it_actually_scales_with():
    # tacacs_config scaled its account sweep against the NAD count, so the model
    # believed a 1,000-account estate needed 5,000 fetches.
    tacacs = registry.get("tacacs_config").providers[0]
    assert tacacs.cost.scales_with == "accounts"
    devices = registry.get("network_devices").providers[0]
    assert devices.cost.scales_with == "nads"


def test_the_active_list_is_read_once_per_tick_not_once_per_dataset():
    """session_authorization and posture_current both need the active list.

    Two fetches a cycle is not just waste: they share a cost pool, so the
    declared MnT load would have been short by one request every five minutes.

    The reader runs minutes after the owner, not microseconds: the owner holds
    the serialized mnt lane for half a cadence while it warms, so a sharing rule
    based on a short wall-clock TTL is always expired by the time it matters.
    """
    from ise_exporter3 import detail_cache
    from ise_exporter3.datasets import session_authorization as authz

    class Transport:
        def __init__(self):
            self.reads = 0

        def get_mnt_xml(self, path, *, api="mnt_xml"):
            self.reads += 1
            return {"total": 0, "sessions": []}

    class Ctx:
        interval = 300

    detail_cache._CACHES.pop(authz.ACTIVE_LIST_CACHE, None)
    ctx = Ctx()
    ctx.transport = Transport()
    authz.active_list(ctx, refresh=True)        # session_authorization, the owner
    cache = detail_cache.shared(authz.ACTIVE_LIST_CACHE)
    # 150s of warm-up later, well past the 60s window that used to gate this.
    entry = cache._entries["current"]
    entry["at"] -= 150
    authz.active_list(ctx)                      # posture_current, same cycle
    assert ctx.transport.reads == 1
    detail_cache._CACHES.pop(authz.ACTIVE_LIST_CACHE, None)


def test_a_reader_with_no_owner_still_reads_the_active_list_once():
    """posture_current alone pays for its own enumeration, which the plan charges.

    With session_authorization disabled nothing refreshes the listing, and the
    reader must not aggregate over an empty cache -- the plan's no-fillers branch
    charges exactly this one request per cycle.
    """
    from ise_exporter3 import detail_cache
    from ise_exporter3.datasets import session_authorization as authz

    class Transport:
        def __init__(self):
            self.reads = 0

        def get_mnt_xml(self, path, *, api="mnt_xml"):
            self.reads += 1
            return {"total": 0, "sessions": []}

    class Ctx:
        interval = 300

    detail_cache._CACHES.pop(authz.ACTIVE_LIST_CACHE, None)
    ctx = Ctx()
    ctx.transport = Transport()
    authz.active_list(ctx)
    assert ctx.transport.reads == 1
    detail_cache._CACHES.pop(authz.ACTIVE_LIST_CACHE, None)
