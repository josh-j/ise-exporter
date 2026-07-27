"""Runtime provider failover.

v1 had a live fallback that swapped a metric's meaning silently; v2 removed
fallback entirely and left blank panels instead. v3 keeps the fallback and makes
every step visible, which is the whole argument of this rewrite. These tests pin
that: the step happens, it is exported, the data carries which source produced
it, and no sample from one source survives under another's name.
"""
import pytest
from prometheus_client import REGISTRY, Gauge

from ise_exporter3 import reporting, telemetry
from ise_exporter3.config import Config
from ise_exporter3.model import Cost, Dataset, Provider
from ise_exporter3.plan import PlannedDataset
from ise_exporter3.scheduler import (
    PROVIDER_FAILOVER_THRESHOLD,
    PROVIDER_RECHECK_SECONDS,
    Scheduler,
)
from ise_exporter3.transports import Transport, TransportError
from tests.test_v3_scheduler import Clock


SESSIONS = Gauge("ise3_test_sessions", "test sessions", ["provider", "psn"])

PREFERRED = "pxgrid"
FALLBACK = "mnt"
LAST = "dataconnect"


class Source(Transport):
    """A transport that can be told to start and stop answering."""

    def __init__(self, target, *, healthy=True, value=1.0, pending="",
                 coverage=None):
        self.target = target
        # Returned-versus-existing for a bounded breakdown, when this source
        # publishes one at all. A fallback usually does not.
        self.coverage = coverage
        self.healthy = healthy
        self.value = value
        # A reason from PENDING_REASONS refused out of prepare(), the way Data
        # Connect refuses while schema discovery cools down.
        self.pending = pending
        self.attempts = 0

    def prepare(self):
        if self.pending:
            raise TransportError(self.pending, f"{self.target} is not ready yet")

    def collect(self, ctx):
        self.attempts += 1
        if not self.healthy:
            raise TransportError("connection_failed", f"{self.target} is down")
        if self.coverage:
            reporting.publish_coverage(ctx, "marginals", *self.coverage)
        ctx.set(SESSIONS, self.value, psn="psn1")


def _provider(name, target):
    def fetch(ctx):
        ctx.transport.collect(ctx)

    return Provider(name=name, cost=Cost(target=target, requests=1),
                    supplies=frozenset({"session"}), fetch=fetch)


DATASET = Dataset(
    name="sessions", description="test sessions", default_interval=300,
    metrics=(SESSIONS,),
    providers=(_provider(PREFERRED, "pxgrid"), _provider(FALLBACK, "mnt"),
               _provider(LAST, "oracle")))


class Plan:
    """The minimum a scheduler needs, without a whole config resolution."""

    def __init__(self, entry):
        self.entries = (entry,)
        self.enabled = (entry,)
        self.unresolved = ()
        self.degraded = ()
        self.targets = ()
        self.overages = ()
        self.fits = True


class RecordingLanes:
    """Runs work inline, but remembers which lane each task was given to."""

    def __init__(self, lanes):
        self.lanes = lanes
        self.submissions = []

    def start(self):
        pass

    def stop(self, timeout=30):
        return True

    def submit(self, target, name, work):
        self.submissions.append((target, name))
        work()
        return True


def _setup(*, clock=None, sources=None, interval=300):
    clock = clock or Clock()
    if sources is None:     # an empty mapping is a meaningful argument here
        sources = {
            "pxgrid": Source("pxgrid", value=10.0),
            "mnt": Source("mnt", value=20.0),
            "oracle": Source("oracle", value=30.0),
        }
    entry = PlannedDataset(
        name=DATASET.name, description="", enabled=True, interval=interval,
        dataset=DATASET, provider=DATASET.providers[0],
        alternatives=DATASET.providers[1:])
    config = Config.from_document(
        {"targets": {"pan": {"host": "pan1", "user": "ro"}}},
        path="test.toml", environ={"ISE_PASS": "secret"})
    scheduler = Scheduler(config, Plan(entry), sources,
                          asynchronous=False, clock=clock)
    scheduler.bootstrap()
    return scheduler, sources, clock


def _state(scheduler):
    return scheduler.states[0]


def _sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels)


def _run_until_next(scheduler, clock):
    """Run one collection, then move the clock to when it is next due."""
    scheduler.tick()
    clock.now = max(clock.now, scheduler.next_run[DATASET.name])


def test_the_preferred_source_is_used_while_it_answers():
    scheduler, _sources, clock = _setup()
    _run_until_next(scheduler, clock)
    assert _state(scheduler).provider.name == PREFERRED
    assert not _state(scheduler).degraded
    assert _sample("ise3_test_sessions", provider=PREFERRED, psn="psn1") == 10.0


def test_repeated_failure_steps_to_the_next_declared_source():
    scheduler, sources, clock = _setup()
    sources["pxgrid"].healthy = False
    for _ in range(PROVIDER_FAILOVER_THRESHOLD):
        _run_until_next(scheduler, clock)
    assert _state(scheduler).provider.name == FALLBACK
    assert _state(scheduler).degraded
    _run_until_next(scheduler, clock)
    assert _sample("ise3_test_sessions", provider=FALLBACK, psn="psn1") == 20.0


def test_the_step_is_exported_not_silent():
    # This is the v1 defect the design exists to prevent: a source swap that a
    # dashboard cannot see and a human cannot explain after the fact.
    scheduler, sources, clock = _setup()
    sources["pxgrid"].healthy = False
    for _ in range(PROVIDER_FAILOVER_THRESHOLD):
        _run_until_next(scheduler, clock)

    assert _sample("ise3_dataset_provider_active",
                   dataset="sessions", provider=FALLBACK) == 1
    assert _sample("ise3_dataset_provider_active",
                   dataset="sessions", provider=PREFERRED) == 0
    assert _sample("ise3_dataset_provider_degraded", dataset="sessions") == 1
    assert _sample("ise3_dataset_provider_reason_info", dataset="sessions",
                   provider=FALLBACK, reason="connection_failed") == 1


def test_no_sample_from_one_source_survives_under_another_s_name():
    # Two sources for one dataset do not mean the same thing. A stale series
    # under the old provider label would keep answering a question that is now
    # about something else.
    scheduler, sources, clock = _setup()
    _run_until_next(scheduler, clock)
    assert _sample("ise3_test_sessions", provider=PREFERRED, psn="psn1") == 10.0

    sources["pxgrid"].healthy = False
    for _ in range(PROVIDER_FAILOVER_THRESHOLD):
        _run_until_next(scheduler, clock)
    assert _sample("ise3_test_sessions", provider=PREFERRED, psn="psn1") is None


def test_a_dataset_steps_through_every_declared_source_before_giving_up():
    scheduler, sources, clock = _setup()
    sources["pxgrid"].healthy = False
    sources["mnt"].healthy = False
    for _ in range(PROVIDER_FAILOVER_THRESHOLD * 2):
        _run_until_next(scheduler, clock)
    assert _state(scheduler).provider.name == LAST
    _run_until_next(scheduler, clock)
    assert _sample("ise3_test_sessions", provider=LAST, psn="psn1") == 30.0


def test_the_last_source_is_not_abandoned_when_there_is_nowhere_left_to_go():
    scheduler, sources, clock = _setup()
    for source in sources.values():
        source.healthy = False
    for _ in range(PROVIDER_FAILOVER_THRESHOLD * 4):
        _run_until_next(scheduler, clock)
    assert _state(scheduler).provider.name == LAST
    assert _sample("ise3_dataset_up", dataset="sessions", provider=LAST) == 0


def test_a_degraded_dataset_probes_its_preferred_source_and_returns():
    scheduler, sources, clock = _setup()
    sources["pxgrid"].healthy = False
    for _ in range(PROVIDER_FAILOVER_THRESHOLD):
        _run_until_next(scheduler, clock)
    assert _state(scheduler).provider.name == FALLBACK

    sources["pxgrid"].healthy = True
    probes_before = sources["pxgrid"].attempts
    clock.advance(PROVIDER_RECHECK_SECONDS)
    _run_until_next(scheduler, clock)

    assert sources["pxgrid"].attempts > probes_before
    assert _state(scheduler).provider.name == PREFERRED
    assert not _state(scheduler).degraded
    assert _sample("ise3_dataset_provider_degraded", dataset="sessions") == 0
    assert _sample("ise3_test_sessions", provider=FALLBACK, psn="psn1") is None


def test_a_failed_recovery_probe_keeps_the_working_source():
    scheduler, sources, clock = _setup()
    sources["pxgrid"].healthy = False
    for _ in range(PROVIDER_FAILOVER_THRESHOLD):
        _run_until_next(scheduler, clock)

    clock.advance(PROVIDER_RECHECK_SECONDS)
    _run_until_next(scheduler, clock)
    # The probe failed, so the dataset stays where it is rather than flapping.
    assert _state(scheduler).provider.name == FALLBACK
    assert _state(scheduler).degraded


def test_a_recovery_probe_does_not_count_against_the_working_source():
    # Otherwise probing a dead preference would eventually evict a healthy
    # fallback, which is the opposite of what failover is for.
    scheduler, sources, clock = _setup()
    sources["pxgrid"].healthy = False
    for _ in range(PROVIDER_FAILOVER_THRESHOLD):
        _run_until_next(scheduler, clock)

    for _ in range(PROVIDER_FAILOVER_THRESHOLD + 2):
        clock.advance(PROVIDER_RECHECK_SECONDS)
        _run_until_next(scheduler, clock)
    assert _state(scheduler).provider.name == FALLBACK
    assert _state(scheduler).failures == 0


def test_a_degraded_dataset_does_not_probe_on_every_collection():
    scheduler, sources, clock = _setup()
    sources["pxgrid"].healthy = False
    for _ in range(PROVIDER_FAILOVER_THRESHOLD):
        _run_until_next(scheduler, clock)
    probes = sources["pxgrid"].attempts

    # Two healthy collections at the 5m cadence stay inside the recheck window.
    for _ in range(2):
        _run_until_next(scheduler, clock)
    assert clock.now - _state(scheduler).last_probe < PROVIDER_RECHECK_SECONDS
    assert sources["pxgrid"].attempts == probes


def test_a_probe_does_not_cost_a_long_cadence_dataset_its_collection():
    # The probe used to replace the cycle instead of preceding it, so any
    # degraded dataset whose cadence exceeds the recheck window -- every
    # multi-source dataset at 30 minutes or six hours -- probed a dead source on
    # every cycle and collected from its fallback exactly once, at the moment of
    # failover, with dataset_up=1 over frozen gauges the whole time.
    scheduler, sources, clock = _setup(interval=21600)
    sources["pxgrid"].healthy = False
    for _ in range(PROVIDER_FAILOVER_THRESHOLD):
        _run_until_next(scheduler, clock)
    assert _state(scheduler).provider.name == FALLBACK

    # The first cycle after a step runs immediately; from the next one on, a
    # cadence this long means every cycle also carries a probe.
    _run_until_next(scheduler, clock)
    for cycle in range(3):
        probes, collections = sources["pxgrid"].attempts, sources["mnt"].attempts
        sources["mnt"].value = 20.0 + cycle
        _run_until_next(scheduler, clock)
        assert sources["pxgrid"].attempts > probes
        assert sources["mnt"].attempts > collections
        assert _sample("ise3_test_sessions", provider=FALLBACK,
                       psn="psn1") == 20.0 + cycle


def test_a_probe_runs_on_its_own_target_s_lane_not_the_fallback_s():
    # One collection per persona at a time. Choosing the provider inside the
    # worker ran a Data Connect scan from the PAN lane, where PAN health then
    # queued behind a minutes-long duty-cycle wait that no metric explained.
    scheduler, sources, clock = _setup()
    lanes = RecordingLanes(scheduler.lanes.lanes)
    scheduler.lanes = lanes
    sources["pxgrid"].healthy = False
    for _ in range(PROVIDER_FAILOVER_THRESHOLD):
        _run_until_next(scheduler, clock)

    lanes.submissions.clear()
    clock.advance(PROVIDER_RECHECK_SECONDS)
    _run_until_next(scheduler, clock)
    assert lanes.submissions[0] == ("pxgrid", "sessions")
    # ...and the collection the probe did not replace runs on the fallback's.
    assert ("mnt", "sessions") in lanes.submissions[1:]


def test_a_source_that_is_not_ready_yet_is_not_a_source_that_failed():
    # schema_pending out of prepare() is "ask again", not evidence the source is
    # bad. Counting it stepped endpoint_inventory off Data Connect and onto a
    # provider whose own notes say it is not a fleet total.
    scheduler, sources, clock = _setup()
    sources["pxgrid"].pending = "schema_pending"
    for _ in range(PROVIDER_FAILOVER_THRESHOLD + 2):
        _run_until_next(scheduler, clock)
    assert _state(scheduler).provider.name == PREFERRED
    assert _state(scheduler).failures == 0


def test_a_pending_refusal_does_not_bank_a_strike_for_the_next_real_failure():
    scheduler, sources, clock = _setup()
    sources["pxgrid"].healthy = False
    for _ in range(PROVIDER_FAILOVER_THRESHOLD - 1):
        _run_until_next(scheduler, clock)
    assert _state(scheduler).failures == PROVIDER_FAILOVER_THRESHOLD - 1

    sources["pxgrid"].pending = "schema_pending"
    for _ in range(3):
        _run_until_next(scheduler, clock)
    assert _state(scheduler).provider.name == PREFERRED

    sources["pxgrid"].pending = ""
    _run_until_next(scheduler, clock)
    assert _state(scheduler).provider.name == FALLBACK


def test_the_source_left_behind_stops_claiming_to_be_healthy():
    # Health that outlives the data it described is the same lie as data that
    # outlives its source: _switch_to drops the departing provider's samples, so
    # its fresh=1 was asserting a snapshot that no longer exists.
    scheduler, sources, clock = _setup()
    _run_until_next(scheduler, clock)
    assert _sample("ise3_dataset_fresh", dataset="sessions",
                   provider=PREFERRED) == 1

    sources["pxgrid"].healthy = False
    for _ in range(PROVIDER_FAILOVER_THRESHOLD):
        _run_until_next(scheduler, clock)
    assert _sample("ise3_dataset_fresh", dataset="sessions",
                   provider=PREFERRED) == 0
    assert _sample("ise3_dataset_up", dataset="sessions",
                   provider=PREFERRED) == 0
    assert _sample("ise3_dataset_last_success_timestamp", dataset="sessions",
                   provider=PREFERRED) is None


def test_a_source_abandoned_mid_chain_is_not_frozen_at_healthy():
    # Nothing ever writes an intermediate provider's health again: the recovery
    # canary only re-probes the preferred source.
    scheduler, sources, clock = _setup()
    sources["pxgrid"].healthy = False
    for _ in range(PROVIDER_FAILOVER_THRESHOLD):
        _run_until_next(scheduler, clock)
    _run_until_next(scheduler, clock)
    assert _sample("ise3_dataset_fresh", dataset="sessions",
                   provider=FALLBACK) == 1

    sources["mnt"].healthy = False
    for _ in range(PROVIDER_FAILOVER_THRESHOLD):
        _run_until_next(scheduler, clock)
    assert _state(scheduler).provider.name == LAST
    assert _sample("ise3_dataset_fresh", dataset="sessions",
                   provider=FALLBACK) == 0
    assert _sample("ise3_dataset_up", dataset="sessions",
                   provider=FALLBACK) == 0
    assert _sample("ise3_dataset_next_run_timestamp", dataset="sessions",
                   provider=FALLBACK) is None


def test_freshness_is_not_asserted_for_a_source_that_has_not_run_yet():
    scheduler, sources, clock = _setup()
    _run_until_next(scheduler, clock)
    sources["pxgrid"].healthy = False
    sources["mnt"].healthy = False
    for _ in range(PROVIDER_FAILOVER_THRESHOLD):
        _run_until_next(scheduler, clock)
    # mnt has been selected but has never produced a snapshot of its own.
    assert _sample("ise3_dataset_fresh", dataset="sessions",
                   provider=FALLBACK) == 0


def test_retiring_one_dataset_s_health_leaves_another_dataset_s_alone():
    # The health families are shared by every dataset, so forgetting them by
    # provider label alone would wipe nad_health, posture_current and the rest.
    telemetry.dataset_up.labels(dataset="other", provider=PREFERRED).set(1)
    telemetry.dataset_fresh.labels(dataset="other", provider=PREFERRED).set(1)
    scheduler, sources, clock = _setup()
    sources["pxgrid"].healthy = False
    for _ in range(PROVIDER_FAILOVER_THRESHOLD):
        _run_until_next(scheduler, clock)
    assert _sample("ise3_dataset_up", dataset="other", provider=PREFERRED) == 1
    assert _sample("ise3_dataset_fresh", dataset="other", provider=PREFERRED) == 1


def test_only_usable_sources_become_candidates():
    # An unbuilt provider or an unconfigured target must not sit in the failover
    # chain pretending to be an option.
    scheduler, _sources, _clock = _setup(sources={"mnt": Source("mnt")})
    assert [provider.name for provider in _state(scheduler).candidates] == [FALLBACK]
    assert _sample("ise3_dataset_provider_available",
                   dataset="sessions", provider=PREFERRED) == 0


def test_a_dataset_with_no_usable_source_is_named_not_scheduled():
    scheduler, _sources, _clock = _setup(sources={})
    assert scheduler.states == ()
    assert _sample("ise3_dataset_up", dataset="sessions", provider=PREFERRED) == 0


def test_failover_retries_immediately_rather_than_serving_a_borrowed_backoff():
    # The backoff was earned by the source that failed. Making the new source
    # wait it out would add minutes of blank panel for no reason.
    scheduler, sources, clock = _setup()
    sources["pxgrid"].healthy = False
    for _ in range(PROVIDER_FAILOVER_THRESHOLD - 1):
        _run_until_next(scheduler, clock)
    before = clock.now
    scheduler.tick()
    assert _state(scheduler).provider.name == FALLBACK
    assert scheduler.next_run["sessions"] == pytest.approx(before, abs=1)


def test_coverage_from_a_departed_source_does_not_outlive_the_step():
    # ise3_topk_groups_* carry no provider label and the incoming source need
    # not publish them at all, so left alone the departed source's "3,693 of
    # 3,693, complete" sits beside a breakdown of a different size.
    scheduler, sources, clock = _setup(sources={
        "pxgrid": Source("pxgrid", value=10.0, coverage=(200, 3693)),
        "mnt": Source("mnt", value=20.0),
        "oracle": Source("oracle", value=30.0),
    })
    _run_until_next(scheduler, clock)
    assert _sample("ise3_topk_groups_total",
                   dataset="sessions", breakdown="marginals") == 3693

    sources["pxgrid"].healthy = False
    for _ in range(PROVIDER_FAILOVER_THRESHOLD):
        _run_until_next(scheduler, clock)
    _run_until_next(scheduler, clock)

    assert _state(scheduler).provider.name == FALLBACK
    assert _sample("ise3_topk_groups_total",
                   dataset="sessions", breakdown="marginals") is None
