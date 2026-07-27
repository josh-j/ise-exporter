"""The scheduler holds no dataset knowledge -- it executes whatever the plan
resolved. These tests pin the cadence and backoff behaviour, and the honesty
rule that anything it cannot collect is named rather than left looking healthy."""
from prometheus_client import REGISTRY

from ise_exporter3.config import Config
from ise_exporter3.plan import MIN_WARMUP_INTERVAL_SECONDS, build_plan
from ise_exporter3.scheduler import (
    BACKOFF_SECONDS,
    FAST_RETRY_SECONDS,
    MAX_CONSECUTIVE_FAILURES,
    Scheduler,
)
from ise_exporter3.transports import Transport, TransportError
from tests.test_v3_datasets import (
    CERTIFICATES_OK,
    DEPLOYMENT_OK,
    LICENSE_OK,
    NAD_DETAILS,
    NADS,
    PATCHES_OK,
)


ALL_RESPONSES = {
    **DEPLOYMENT_OK, **PATCHES_OK, **LICENSE_OK, **CERTIFICATES_OK,
    **NAD_DETAILS,
    "/config/networkdevice": NADS,
    "/backup-restore/config/last-backup-status": {},
    "/config/internaluser": [],
    "/policy/device-admin/policy-set": [],
    "/policy/device-admin/command-sets": [],
    "/policy/device-admin/shell-profiles": [],
    "/config/endpoint": [],
}


class ScriptedTransport(Transport):
    target = "pan"

    def __init__(self, responses=None, failure=None):
        self.responses = dict(responses or ALL_RESPONSES)
        self.failure = failure
        self.calls = []

    def _answer(self, path):
        self.calls.append(path)
        if self.failure is not None:
            raise self.failure
        return self.responses[path]

    def get_openapi(self, path, **kwargs):
        return self._answer(path)

    def get_openapi_all(self, path, **kwargs):
        return self._answer(path)

    def get_ers(self, path, **kwargs):
        return self._answer(path)

    def close(self):
        pass


def _config(**overrides):
    document = {"targets": {"pan": {"host": "pan1", "user": "ro"}}}
    document.update(overrides)
    return Config.from_document(
        document, path="test.toml", environ={"ISE_PASS": "secret"})


class Clock:
    """A hand-cranked clock, so cadence assertions are exact instead of fuzzy."""

    def __init__(self, now=1000.0):
        self.now = float(now)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self.now


def _scheduler(transport=None, config=None, clock=None):
    config = config or _config()
    return Scheduler(config, build_plan(config),
                     {"pan": transport or ScriptedTransport()},
                     asynchronous=False, clock=clock or Clock())


def _sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels)


def test_only_datasets_with_a_built_provider_and_a_transport_are_runnable():
    scheduler = _scheduler()
    names = {state.name for state in scheduler.states}
    assert names == {"backup", "certificates", "deployment", "licensing",
                     "network_devices", "patches", "tacacs_config",
                     "tacacs_policy_rules"}


def test_a_declared_but_unbuilt_provider_is_named_not_left_looking_healthy():
    # Every shipped provider is built, so this uses a synthetic one: the
    # guarantee is that a declared-but-unbuilt source is reported by name rather
    # than crashing on a missing callable or reading as healthy.
    from ise_exporter3.model import Cost, Dataset, Provider
    from ise_exporter3.plan import PlannedDataset

    dataset = Dataset(
        name="unbuilt_probe", description="", default_interval=300,
        providers=(Provider(name="ers", cost=Cost(target="pan", requests=1)),))
    entry = PlannedDataset(
        name=dataset.name, description="", enabled=True, interval=300,
        dataset=dataset, provider=dataset.providers[0])

    class Plan:
        entries = (entry,)
        enabled = (entry,)
        unresolved = degraded = targets = overages = ()
        fits = True

    config = _config()
    scheduler = Scheduler(config, Plan(), {"pan": ScriptedTransport()},
                          asynchronous=False, clock=Clock())
    assert scheduler.states == ()
    assert _sample("ise3_dataset_up", dataset="unbuilt_probe", provider="ers") == 0
    assert _sample("ise3_dataset_last_failure_info", dataset="unbuilt_probe",
                   provider="ers", reason="not_implemented") == 1


def test_bootstrap_makes_everything_due_and_a_tick_collects_it():
    scheduler = _scheduler()
    scheduler.bootstrap()
    submitted = scheduler.tick()
    assert sorted(submitted) == ["backup", "certificates", "deployment",
                                 "licensing", "network_devices", "patches",
                                 "tacacs_config", "tacacs_policy_rules"]
    assert _sample("ise3_dataset_up", dataset="deployment", provider="openapi") == 1


def test_a_dataset_is_not_collected_again_before_it_is_due():
    clock = Clock()
    scheduler = _scheduler(clock=clock)
    scheduler.bootstrap()
    scheduler.tick()
    clock.advance(1)
    assert scheduler.tick() == []
    # deployment runs every five minutes; certificates every six hours.
    clock.advance(301)
    assert scheduler.tick() == ["deployment"]


def test_success_reschedules_from_completion_at_the_configured_cadence():
    clock = Clock()
    scheduler = _scheduler(clock=clock)
    scheduler.bootstrap()
    scheduler.tick()
    entry = next(s for s in scheduler.states if s.name == "deployment")
    assert scheduler.next_run["deployment"] == clock.now + entry.interval


def test_a_transient_failure_retries_soon_rather_than_at_the_full_cadence():
    # A six-hour dataset that fails once must not leave a blank panel for six
    # hours; that was a real v2 symptom.
    clock = Clock()
    scheduler = _scheduler(
        ScriptedTransport(failure=TransportError("timeout")), clock=clock)
    scheduler.bootstrap()
    scheduler.tick()
    entry = next(s for s in scheduler.states if s.name == "certificates")
    assert entry.interval == 21600
    assert scheduler.next_run["certificates"] == clock.now + FAST_RETRY_SECONDS


def test_an_account_level_failure_backs_off_instead_of_hammering():
    clock = Clock()
    scheduler = _scheduler(
        ScriptedTransport(failure=TransportError("authentication_failed")),
        clock=clock)
    scheduler.bootstrap()
    scheduler.tick()
    assert scheduler.next_run["deployment"] == clock.now + BACKOFF_SECONDS


def test_repeated_failure_stops_retrying_fast():
    clock = Clock()
    scheduler = _scheduler(
        ScriptedTransport(failure=TransportError("timeout")), clock=clock)
    scheduler.bootstrap()

    # Follow the schedule the exporter actually sets, rather than assuming it.
    delays = []
    for _ in range(MAX_CONSECUTIVE_FAILURES):
        submitted_at = clock.now
        scheduler.tick()
        delays.append(scheduler.next_run["deployment"] - submitted_at)
        clock.advance(delays[-1])

    assert scheduler.runner.consecutive_failures("deployment") == MAX_CONSECUTIVE_FAILURES
    # Fast retries while the failure might be transient, then one step to the
    # protected backoff on the attempt that crosses the threshold.
    assert delays[:-1] == [FAST_RETRY_SECONDS] * (MAX_CONSECUTIVE_FAILURES - 1)
    assert delays[-1] == BACKOFF_SECONDS


def test_a_snapshot_older_than_two_cadences_is_no_longer_fresh():
    clock = Clock()
    scheduler = _scheduler(clock=clock)
    scheduler.bootstrap()
    scheduler.tick()
    assert _sample("ise3_dataset_fresh", dataset="deployment", provider="openapi") == 1
    clock.advance(601)      # two five-minute cadences plus one second
    scheduler.refresh_freshness()
    assert _sample("ise3_dataset_fresh", dataset="deployment", provider="openapi") == 0


def test_a_failing_dataset_does_not_stop_the_others():
    class Selective(ScriptedTransport):
        def _answer(self, path):
            self.calls.append(path)
            if path == "/patch":
                raise TransportError("http_error", "boom")
            return self.responses[path]

    scheduler = _scheduler(Selective())
    scheduler.bootstrap(now=1000.0)
    scheduler.tick(now=1000.0)
    assert _sample("ise3_dataset_up", dataset="patches", provider="openapi") == 0
    assert _sample("ise3_dataset_up", dataset="deployment", provider="openapi") == 1


def test_the_scheduler_opens_one_lane_per_target_in_use():
    scheduler = _scheduler()
    assert set(scheduler.lanes.lanes) == {"pan"}


def test_the_exporter_keeps_almost_nothing_on_disk(tmp_path):
    # Prometheus is already a time-series database. An exporter that accumulates
    # history beside it builds a second, worse one -- v2's restart-persistent
    # snapshots had a documented 256 MiB ceiling. What belongs here is
    # cross-process safety state, measured in hundreds of bytes.
    config = Config.from_document(
        {"targets": {"pan": {"host": "pan1", "user": "ro"}},
         "exporter": {"state_db": str(tmp_path / "state.sqlite3")}},
        path="test.toml", environ={"ISE_PASS": "secret"})
    scheduler = Scheduler(config, build_plan(config), {"pan": ScriptedTransport()},
                          asynchronous=False, clock=Clock())
    scheduler.bootstrap()
    for _ in range(5):
        scheduler.tick()
        scheduler.clock.advance(21600)

    on_disk = scheduler.publish_state_size()
    assert on_disk < 64 * 1024, f"the exporter is storing {on_disk} bytes"
    assert REGISTRY.get_sample_value("ise3_exporter_state_bytes") == on_disk


def test_no_dataset_persists_a_metric_snapshot():
    # The v2 anti-pattern this replaces: serialising up to 20,000 samples per
    # reporting domain into SQLite so a restart need not re-query. Prometheus
    # already has those samples.
    import pathlib
    source = pathlib.Path(__file__).parents[1] / "ise_exporter3"
    offenders = [
        path.name for path in source.rglob("*.py")
        if "serialize_metric_snapshot" in path.read_text()
        or "replace_dataset_snapshot" in path.read_text()]
    assert not offenders, f"{offenders} persist metric snapshots"


# --- warm-up pacing ---------------------------------------------------------

def _warm_scheduler(clock=None):
    """A scheduler whose network_devices cache is genuinely cold.

    ScriptedTransport answers the enumeration with NADS and every detail path
    from NAD_DETAILS, so the cache fills over several passes exactly as it does
    against an appliance.
    """
    from ise_exporter3 import detail_cache
    from ise_exporter3.datasets import network_devices

    detail_cache._CACHES.pop(network_devices.CACHE, None)
    return _scheduler(clock=clock or Clock())


def _state(scheduler, name):
    return next(state for state in scheduler.states if state.name == name)


def test_a_filling_cache_is_revisited_at_the_rate_the_budget_affords(monkeypatch):
    # The defect: network_devices warmed 500 devices per run at a six-hour
    # cadence, so 5,000 NADs took sixty hours while the PAN budget sat at 1%
    # utilisation. Nothing was wrong with the budget -- the cadence ignored it.
    #
    # One device per pass against the three-NAD fixture reproduces the shape at
    # a size a test can hold.
    from ise_exporter3.datasets import network_devices

    monkeypatch.setattr(network_devices, "WARMUP_FETCHES_PER_CYCLE", 1)
    clock = Clock()
    scheduler = _warm_scheduler(clock)
    state = _state(scheduler, "network_devices")

    outcome = scheduler._collect(state)
    assert outcome.ok
    assert outcome.deferred == 2, "two of three devices left for the next pass"

    scheduled = scheduler.next_run[state.name] - clock.now
    assert scheduled < state.interval, (
        "a cache with work outstanding must not wait out the full cadence")
    assert scheduled >= MIN_WARMUP_INTERVAL_SECONDS


def test_the_warmup_cadence_is_the_one_the_plan_printed(monkeypatch):
    # A plan that says one thing while the scheduler does another is worse than
    # either number alone, so both come from the same derivation.
    from ise_exporter3.datasets import network_devices
    from ise_exporter3.plan import warmup_interval

    monkeypatch.setattr(network_devices, "WARMUP_FETCHES_PER_CYCLE", 1)
    clock = Clock()
    scheduler = _warm_scheduler(clock)
    state = _state(scheduler, "network_devices")
    scheduler._collect(state)

    expected = warmup_interval(scheduler.plan, state.entry)
    assert scheduler.next_run[state.name] - clock.now == expected


def test_a_warm_cache_returns_to_the_cadence_the_data_deserves(monkeypatch):
    # The pacing is for filling, not forever: a warm cache re-reading itself
    # every twenty minutes would spend the budget it was given to save.
    from ise_exporter3.datasets import network_devices

    monkeypatch.setattr(network_devices, "WARMUP_FETCHES_PER_CYCLE", 1)
    clock = Clock()
    scheduler = _warm_scheduler(clock)
    state = _state(scheduler, "network_devices")

    for _ in range(3):        # one device per pass, three in the fixture
        scheduler._collect(state)
    outcome = scheduler._collect(state)

    assert outcome.deferred == 0
    assert scheduler.next_run[state.name] - clock.now == state.interval


def test_outstanding_work_is_carried_out_of_the_collection(monkeypatch):
    # The signal already existed as a gauge nobody acted on. Carrying it on the
    # Outcome is what lets the scheduler do anything about it.
    from ise_exporter3.datasets import network_devices
    from ise_exporter3.runtime import Outcome

    assert "deferred" in Outcome.__dataclass_fields__
    monkeypatch.setattr(network_devices, "WARMUP_FETCHES_PER_CYCLE", 1)
    scheduler = _warm_scheduler()
    outcome = scheduler._collect(_state(scheduler, "network_devices"))
    assert outcome.deferred == 2
    assert _sample("ise3_detail_fetches_deferred",
                   cache=network_devices.CACHE) == 2


def test_the_target_is_told_whether_a_cache_on_it_is_still_filling(monkeypatch):
    # Only a declared warm-up burst does anything with this, but the target has
    # to be told either way or the burst could never begin, and "nobody looked"
    # must not read the same as "nothing is filling".
    from ise_exporter3.datasets import network_devices

    class WarmAware(ScriptedTransport):
        def __init__(self):
            super().__init__()
            self.warming = None

        def set_warming(self, warming):
            self.warming = warming

    monkeypatch.setattr(network_devices, "WARMUP_FETCHES_PER_CYCLE", 1)
    transport = WarmAware()
    scheduler = _scheduler(transport=transport)
    scheduler.bootstrap()
    assert transport.warming is False
    assert _sample("ise3_budget_warming", target="pan") == 0

    from ise_exporter3 import detail_cache

    detail_cache._CACHES.pop(network_devices.CACHE, None)
    scheduler._collect(_state(scheduler, "network_devices"))
    assert transport.warming is True
    assert _sample("ise3_budget_warming", target="pan") == 1
