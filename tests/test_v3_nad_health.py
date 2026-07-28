"""Dead-switch detection, and the answer it must refuse to give.

`nad_health` measures silence: a NAD that authenticated nothing produces no row
at all, so "which switches went quiet" can only be answered against the set of
switches that exist -- and that set comes from `network_devices`, through the
shared NAD directory.

Which means an empty directory is not "nothing is dead". It is "the question
cannot be asked yet", and those two must not look the same. Found on a live lab:
at a cold start this dataset runs before the inventory has published anything,
and reported zero silent switches against an estate of 504.
"""
import pytest
from prometheus_client import REGISTRY

from ise_exporter3 import nad_directory
from ise_exporter3.config import Config
from ise_exporter3.datasets import nad_health
from ise_exporter3.plan import PlannedDataset
from ise_exporter3.runtime import Runner
from ise_exporter3.scheduler import SLOW_RETRY_REASONS
from ise_exporter3.transports import FAILURE_EXPLANATIONS, FAILURE_REASONS, Transport


class Oracle(Transport):
    """Answers the paged activity scan, and counts what it was asked for."""

    target = "oracle"

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.queries = 0

    def query(self, sql, parameters=None, *, adaptive=True):
        self.queries += 1
        for row in self.rows:
            row.setdefault("group_total", len(self.rows))
        return self.rows

    def prepare(self):
        return None

    def satisfies(self, requirements):
        return True, "", ""


def _config():
    return Config.from_document(
        {"targets": {
            "pan": {"host": "pan1", "user": "ro"},
            "oracle": {"host": "mnt1", "user": "dataconnect", "service": "cpm10"}}},
        path="test.toml",
        environ={"ISE_PASS": "x", "ISE_DATACONNECT_PASSWORD": "y"})


def _entry():
    return PlannedDataset(
        name=nad_health.DATASET.name, description="", enabled=True, interval=21600,
        dataset=nad_health.DATASET, provider=nad_health.DATASET.providers[0])


def _run(transport):
    return Runner(_config()).run(_entry(), transport)


def _inventory(*names):
    nad_directory.shared().replace([
        {"nad": name, "location": "site", "ops_owner": "net-ops",
         "keys": (name,)} for name in names])


@pytest.fixture(autouse=True)
def _clean_directory():
    nad_directory.shared().replace(())
    yield
    nad_directory.shared().replace(())


def _sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels)


# --- the refusal ------------------------------------------------------------

def test_an_empty_inventory_is_refused_rather_than_reported_as_zero_silent():
    outcome = _run(Oracle([{"nad": "sw-01", "passed": 5, "failed": 0}]))
    assert not outcome.ok
    assert outcome.reason == "dependency_pending"
    assert "network_devices" in outcome.detail


def test_the_refusal_names_a_reason_the_vocabulary_knows():
    # Free text never reaches a label, so a new failure mode has to join the
    # closed set rather than arrive as unexpected_error.
    assert "dependency_pending" in FAILURE_REASONS
    assert FAILURE_EXPLANATIONS["dependency_pending"]


def test_the_refusal_retries_in_a_minute_not_in_fifteen():
    # This is a cold-start ordering problem that the very next network_devices
    # collection resolves. Backing off like an authentication failure would
    # leave dead-switch detection blind for a quarter of an hour after every
    # restart.
    assert "dependency_pending" not in SLOW_RETRY_REASONS


def test_the_last_good_answer_survives_a_refusal_rather_than_dropping_to_zero():
    """The specific lie this prevents: silent_total falling to 0.

    Order-independent on purpose. `ise3_nad_silent_total` is a process-global
    gauge, so asserting it is *absent* would only hold in a fresh interpreter
    and would pass or fail depending on which tests ran first. What actually
    matters is the transition: a real answer, then a refusal, and the real
    answer still standing -- which is what `Publication.discard` guarantees and
    what a fresh zero would have destroyed.
    """
    _inventory("sw-01", "sw-02", "sw-03")
    assert _run(Oracle([{"nad": "sw-01", "passed": 5, "failed": 1}])).ok
    assert _sample("ise3_nad_silent_total", provider="dataconnect") == 2

    # The inventory goes away -- a restart, or network_devices failing.
    nad_directory.shared().replace(())
    outcome = _run(Oracle([{"nad": "sw-01", "passed": 5, "failed": 1}]))

    assert not outcome.ok and outcome.reason == "dependency_pending"
    assert _sample("ise3_nad_silent_total", provider="dataconnect") == 2, \
        "a refusal must not overwrite the last real answer with zero"
    assert _sample("ise3_dataset_up", dataset="nad_health",
                   provider="dataconnect") == 0


# --- the normal path still works -------------------------------------------

def test_a_populated_inventory_reports_silence_against_it():
    _inventory("sw-01", "sw-02", "sw-03")
    outcome = _run(Oracle([{"nad": "sw-01", "passed": 5, "failed": 1}]))
    assert outcome.ok, f"{outcome.reason}: {outcome.detail}"
    # Two of three configured switches authenticated nothing.
    assert _sample("ise3_nad_silent_total", provider="dataconnect") == 2
    assert _sample("ise3_nad_activity_covered", provider="dataconnect") == 1
    # A silent switch is an explicit zero, not an absent series.
    assert _sample("ise3_nad_authentications", provider="dataconnect",
                   nad="sw-02", status="passed") == 0


def test_an_inventory_with_no_activity_at_all_is_a_valid_answer():
    # Every switch silent is a real and alarming answer, and distinct from the
    # refusal above: the question was asked and this is what came back.
    _inventory("sw-01", "sw-02")
    outcome = _run(Oracle([]))
    assert outcome.ok
    assert _sample("ise3_nad_silent_total", provider="dataconnect") == 2


def test_the_refusal_costs_the_appliance_nothing():
    """The dependency is checked before any Oracle work, not after.

    Caught in production rather than by a test: the first version ran the paged
    activity scan, then found the directory empty and discarded the result. On a
    one-minute retry through a cold start that is a Data Connect query a minute,
    charged to the duty cycle, for an answer thrown away every time -- the same
    "pays for the scan and aborts anyway" shape as the batch-ceiling defect, in
    a new place and on a timer.
    """
    transport = Oracle([{"nad": "sw-01", "passed": 5, "failed": 0}])
    outcome = _run(transport)

    assert not outcome.ok and outcome.reason == "dependency_pending"
    assert transport.queries == 0, (
        "a refusal must not first spend a Data Connect statement")


def test_the_scan_still_runs_once_the_dependency_is_satisfied():
    _inventory("sw-01", "sw-02")
    transport = Oracle([{"nad": "sw-01", "passed": 5, "failed": 0}])
    assert _run(transport).ok
    assert transport.queries >= 1


# --- a pending dependency must not escalate ---------------------------------

def test_waiting_longer_does_not_make_the_wait_longer():
    """Caught in production, and it defeated the whole point of the refusal.

    nad_health refused five times at one-minute intervals while the NAD
    inventory warmed, which tripped MAX_CONSECUTIVE_FAILURES and pushed the
    retry out to max(interval, BACKOFF) -- six hours. The dataset then sat blind
    for exactly the window the refusal was written to close, with an honest
    label on it instead of a wrong number.

    A pending dependency is not a failure to back off from: the thing it waits
    for is already on its way, so escalating makes the wait longer the longer it
    has been waiting.
    """
    from ise_exporter3 import scheduler as scheduler_module
    from ise_exporter3.plan import build_plan
    from ise_exporter3.runtime import Outcome

    config = _config()
    sched = scheduler_module.Scheduler(
        config, build_plan(config), {"oracle": Oracle()}, asynchronous=False)
    state = next(s for s in sched.states if s.name == "nad_health")

    pending = Outcome("nad_health", "dataconnect", False,
                      reason="dependency_pending")
    # Well past the consecutive-failure threshold.
    for _ in range(scheduler_module.MAX_CONSECUTIVE_FAILURES + 3):
        sched.runner.failures["nad_health"] = {
            "reason": "dependency_pending", "detail": "", "count": 99}
        assert sched._retry_delay(state, pending) == \
            scheduler_module.FAST_RETRY_SECONDS

    # An ordinary failure still escalates -- this narrows the exemption rather
    # than removing the backoff.
    broken = Outcome("nad_health", "dataconnect", False, reason="invalid_response")
    assert sched._retry_delay(state, broken) >= scheduler_module.BACKOFF_SECONDS


def test_a_pending_reason_is_one_the_vocabulary_knows():
    from ise_exporter3.scheduler import PENDING_REASONS, SLOW_RETRY_REASONS

    assert PENDING_REASONS <= FAILURE_REASONS
    # "not yet" and "stop hammering" are opposite instructions.
    assert not (PENDING_REASONS & SLOW_RETRY_REASONS)


# --- the session join, against how NADs are really addressed ----------------

def test_a_subnet_configured_nad_attributes_the_sessions_inside_it():
    """Found on the lab: campus-corp-wired is 10.200.40.0/24 and every one of its
    sessions arrives from 10.200.40.11-.14. Keying on the network address alone
    meant no session ever matched, and a whole segment went unattributed."""
    from ise_exporter3.datasets import network_devices

    keys = network_devices.device_addresses(
        {"NetworkDeviceIPList": [{"ipaddress": "10.200.40.0", "mask": 24}]})
    assert keys == ["10.200.40.0/24"]

    directory = nad_directory.shared()
    directory.replace([{"nad": "campus-corp-wired", "location": "Unknown",
                        "ops_owner": "AD Lab", "keys": (*keys, "campus-corp-wired")}])
    assert directory.ops_owner("10.200.40.12") == "AD Lab"
    assert directory.ops_owner("campus-corp-wired") == "AD Lab"
    # Still a bounded claim: an address outside the prefix is not this NAD's.
    assert directory.ops_owner("10.201.40.12") == "unknown"


def test_a_host_configured_nad_matches_only_its_own_address():
    from ise_exporter3.datasets import network_devices

    keys = network_devices.device_addresses(
        {"NetworkDeviceIPList": [{"ipaddress": "10.99.0.1", "mask": 32}]})
    assert keys == ["10.99.0.1/32"]

    directory = nad_directory.shared()
    directory.replace([{"nad": "sim-nad-0001", "location": "site",
                        "ops_owner": "net-ops", "keys": keys}])
    assert directory.ops_owner("10.99.0.1") == "net-ops"
    assert directory.ops_owner("10.99.0.2") == "unknown"


def test_the_longest_prefix_owns_the_address():
    directory = nad_directory.shared()
    directory.replace([
        {"nad": "campus", "location": "site", "ops_owner": "campus-team",
         "keys": ("10.200.0.0/16",)},
        {"nad": "lab-carveout", "location": "site", "ops_owner": "lab-team",
         "keys": ("10.200.40.0/24",)},
    ])
    assert directory.ops_owner("10.200.40.12") == "lab-team"
    assert directory.ops_owner("10.200.9.12") == "campus-team"


def test_a_default_route_nad_does_not_own_the_whole_estate():
    # 0.0.0.0/0 as a NAD address would attribute every session in the deployment
    # to one owner, which is worse than attributing none of them.
    directory = nad_directory.shared()
    directory.replace([{"nad": "catch-all", "location": "site",
                        "ops_owner": "someone", "keys": ("0.0.0.0/0", "catch-all")}])
    assert directory.ops_owner("10.200.40.12") == "unknown"
    assert directory.ops_owner("catch-all") == "someone"


def test_a_malformed_mask_leaves_the_host_key_alone():
    from ise_exporter3.datasets import network_devices

    assert network_devices.device_addresses(
        {"NetworkDeviceIPList": [{"ipaddress": "10.99.0.1", "mask": "n/a"}]}) == [
            "10.99.0.1"]
