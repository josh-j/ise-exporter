"""The runner's contract: a dataset's data and its health commit together, and a
failed attempt never becomes a convincing empty snapshot."""
import threading

import pytest
from prometheus_client import REGISTRY, Gauge

from ise_exporter3 import reporting, telemetry
from ise_exporter3.config import Config
from ise_exporter3.lanes import Lane
from ise_exporter3.model import Cost, Dataset, Provider
from ise_exporter3.plan import PlannedDataset
from ise_exporter3.runtime import Runner, classify
from ise_exporter3.limits import for_scale
from ise_exporter3.model import Scale
from ise_exporter3.snapshots import Publication, SnapshotError
from ise_exporter3.transports import TransportError


VALUE = Gauge("ise3_test_value", "test value", ["provider", "kind"])
OTHER = Gauge("ise3_test_other", "test other", ["provider"])
UNDECLARED = Gauge("ise3_test_undeclared", "not in the dataset", ["provider"])
# A dataset may legitimately name a label `value` -- tacacs_activity does, for
# the value of whichever dimension a marginal row belongs to. `family` is the
# other name a publication signature could collide with.
COLLIDING = Gauge(
    "ise3_test_colliding", "labels named like the publication signature",
    ["provider", "dimension", "value", "family"])


def _dataset(fetch):
    return Dataset(
        name="probe", description="test dataset", default_interval=60,
        metrics=(VALUE, OTHER, COLLIDING),
        providers=(Provider(name="openapi", cost=Cost(target="pan", requests=1),
                            supplies=frozenset({"value"}), fetch=fetch),))


def _entry(fetch):
    dataset = _dataset(fetch)
    return PlannedDataset(
        name=dataset.name, description=dataset.description, enabled=True,
        interval=60, dataset=dataset, provider=dataset.providers[0])


def _config():
    return Config.from_document(
        {"targets": {"pan": {"host": "pan1", "user": "ro"}}},
        path="test.toml", environ={"ISE_PASS": "secret"})


def _sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels)


def _runner():
    return Runner(_config())


def test_a_successful_attempt_publishes_data_and_health_together():
    def fetch(ctx):
        ctx.set(VALUE, 7, kind="alpha")
        ctx.set(OTHER, 1)

    outcome = _runner().run(_entry(fetch), transport=None)

    assert outcome.ok
    assert _sample("ise3_test_value", provider="openapi", kind="alpha") == 7
    assert _sample("ise3_dataset_up", dataset="probe", provider="openapi") == 1
    assert _sample("ise3_dataset_fresh", dataset="probe", provider="openapi") == 1
    assert _sample("ise3_dataset_consecutive_failures", dataset="probe") == 0


def test_the_provider_label_is_bound_without_the_dataset_repeating_it():
    # Sources differ in meaning, so provider is a label on the data. Making the
    # runner bind it means a dataset cannot forget to.
    _runner().run(_entry(lambda ctx: ctx.set(VALUE, 3, kind="beta")), transport=None)
    assert _sample("ise3_test_value", provider="openapi", kind="beta") == 3


def test_a_failed_attempt_keeps_the_previous_snapshot():
    runner = _runner()
    runner.run(_entry(lambda ctx: ctx.set(VALUE, 42, kind="kept")), transport=None)

    def failing(ctx):
        raise TransportError("timeout", "took too long")

    outcome = runner.run(_entry(failing), transport=None)

    assert not outcome.ok and outcome.reason == "timeout"
    # The old value survives: an outage must not read as a legitimate zero.
    assert _sample("ise3_test_value", provider="openapi", kind="kept") == 42
    assert _sample("ise3_dataset_up", dataset="probe", provider="openapi") == 0
    assert _sample("ise3_dataset_last_failure_info", dataset="probe",
                   provider="openapi", reason="timeout") == 1


def test_a_partial_fetch_rolls_back_rather_than_publishing_half_a_dataset():
    runner = _runner()
    runner.run(_entry(lambda ctx: ctx.set(VALUE, 1, kind="before")), transport=None)

    def half_way(ctx):
        ctx.set(VALUE, 99, kind="after")
        raise TransportError("invalid_response", "row 2 was malformed")

    assert not runner.run(_entry(half_way), transport=None).ok
    assert _sample("ise3_test_value", provider="openapi", kind="before") == 1
    assert _sample("ise3_test_value", provider="openapi", kind="after") is None


def test_consecutive_failures_accumulate_and_reset_on_recovery():
    runner = _runner()

    def failing(ctx):
        raise TransportError("connection_failed")

    runner.run(_entry(failing), transport=None)
    runner.run(_entry(failing), transport=None)
    assert runner.consecutive_failures("probe") == 2
    assert _sample("ise3_dataset_consecutive_failures", dataset="probe") == 2

    runner.run(_entry(lambda ctx: ctx.set(OTHER, 1)), transport=None)
    assert runner.consecutive_failures("probe") == 0
    assert _sample("ise3_dataset_consecutive_failures", dataset="probe") == 0


def test_a_dataset_cannot_publish_a_family_it_did_not_declare():
    # Otherwise a dataset could write into another's series and the atomic
    # replacement boundary would not cover it.
    outcome = _runner().run(
        _entry(lambda ctx: ctx.set(UNDECLARED, 1)), transport=None)
    assert not outcome.ok
    assert "not declared" in outcome.detail


@pytest.mark.parametrize("error,reason", [
    (TransportError("tls_failed"), "tls_failed"),
    (TimeoutError("slow"), "timeout"),
    (ConnectionResetError("reset"), "connection_failed"),
    (ValueError("bad json"), "invalid_response"),
    (RuntimeError("who knows"), "unexpected_error"),
])
def test_every_exception_maps_onto_the_bounded_failure_vocabulary(error, reason):
    # An exception string reaching a label would make failures unbounded state.
    assert classify(error)[0] == reason


def test_failure_detail_is_bounded_even_when_the_error_is_not():
    outcome = _runner().run(
        _entry(lambda ctx: (_ for _ in ()).throw(
            TransportError("invalid_response", "x" * 5000))), transport=None)
    assert not outcome.ok
    assert len(outcome.detail.encode("utf-8")) <= 256


# --- a label may be named like the publication signature --------------------

def test_a_dataset_may_publish_a_label_named_value():
    """Regression: `FetchContext.set(self, family, value, **label_values)` took
    `value` as a keyword, so a dataset with its own `value=` label raised
    TypeError for every row -- permanently, and for any dataset that named a
    label `value`. tacacs_activity did, so it published nothing at all.

    Both publication signatures are positional-only now, which removes the whole
    class of collision rather than the one instance. This test would have caught
    it, and it fails loudly if either signature grows a keyword name again.
    """
    def fetch(ctx):
        ctx.set(COLLIDING, 3, dimension="device", value="sw-01", family="alpha")

    outcome = _runner().run(_entry(fetch), transport=None)
    assert outcome.ok, f"{outcome.reason}: {outcome.detail}"
    assert _sample("ise3_test_colliding", provider="openapi", dimension="device",
                   value="sw-01", family="alpha") == 3


def test_the_publication_boundary_takes_the_same_names_as_labels():
    # The same collision one layer down: Publication.set is what FetchContext
    # delegates to, and a keyword there would fail identically.
    publication = Publication(
        (COLLIDING,), limits=for_scale(Scale()), provider="openapi")
    publication.set(COLLIDING, 5, dimension="username", value="admin",
                    family="beta")
    publication.commit()
    assert _sample("ise3_test_colliding", provider="openapi",
                   dimension="username", value="admin", family="beta") == 5


def test_the_real_dataset_that_found_this_still_names_a_label_value():
    # If tacacs_activity ever stops using `value` as a label, the tests above
    # become a museum piece rather than a regression guard, and someone should
    # know that before deciding they are still earning their place.
    from ise_exporter3.datasets import tacacs_activity

    assert any("value" in family._labelnames
               for family in tacacs_activity.DATASET.metrics)


def test_publication_rejects_a_non_finite_value():
    publication = Publication((VALUE,), limits=for_scale(Scale()), provider="openapi")
    with pytest.raises(SnapshotError):
        publication.set(VALUE, float("inf"), kind="bad")


def test_publication_rejects_an_unbounded_label():
    publication = Publication((VALUE,), limits=for_scale(Scale()), provider="openapi")
    with pytest.raises(SnapshotError, match="bound it"):
        publication.set(VALUE, 1, kind="x" * 5000)


def test_a_lane_runs_one_dataset_at_a_time():
    lane = Lane("pan")
    lane.start()
    concurrent, peak = [], []
    gate = threading.Lock()

    def work():
        with gate:
            concurrent.append(1)
            peak.append(len(concurrent))
        threading.Event().wait(0.01)
        with gate:
            concurrent.pop()

    try:
        for index in range(5):
            lane.submit(f"dataset-{index}", work)
        deadline = threading.Event()
        for _ in range(200):
            if not lane._queued:
                break
            deadline.wait(0.01)
    finally:
        lane.stop(timeout=5)
    assert peak and max(peak) == 1


def test_a_lane_drops_a_duplicate_submission_of_a_dataset_already_queued():
    # A collection slower than its own cadence must not queue without bound.
    lane = Lane("pan")
    assert lane.submit("probe", lambda: None) is True
    assert lane.submit("probe", lambda: None) is False
    assert lane.submit("other", lambda: None) is True
    lane.stop(timeout=1)


def test_lane_stop_is_safe_before_start():
    assert Lane("pan").stop(timeout=1) is True


def test_telemetry_publishes_the_plan_including_rejected_providers():
    from ise_exporter3.plan import build_plan

    config = Config.from_document(
        {"profile": "production",
         "targets": {"pan": {"host": "pan1", "user": "ro"},
                     "mnt": {"host": "mnt1"}}},
        path="test.toml", environ={"ISE_PASS": "secret"})
    telemetry.publish_plan(build_plan(config))

    # active_sessions prefers pxGrid, which is not configured here.
    assert _sample("ise3_dataset_provider_active",
                   dataset="active_sessions", provider="mnt") == 1
    assert _sample("ise3_dataset_provider_available",
                   dataset="active_sessions", provider="pxgrid") == 0
    assert _sample("ise3_dataset_provider_degraded", dataset="active_sessions") == 1
    assert _sample("ise3_load_planned_requests_per_hour", target="mnt") > 0


# Everything below asserts the same defect from three angles: a labelled
# counter with no children exports no samples at all, so until its first event
# a family reads on a dashboard as an absent exporter rather than as zero
# events. Publishing the plan must therefore seed a zero-valued child for every
# combination the resolved plan can actually increment -- and no others. The
# registry is process-global, so a test earlier in the run may already have
# counted real events into a child; seeding must then leave that count alone,
# which is why each test captures the value first and asserts "present, and
# exactly what it was -- or exactly zero".


def _full_config():
    return Config.from_document(
        {"profile": "production",
         "targets": {
             "pan": {"host": "pan1", "user": "ro"},
             "mnt": {"host": "mnt1"},
             "oracle": {"host": "mnt1", "user": "dataconnect",
                        "service": "cpm10"},
             "pxgrid": {"host": "px1", "node_name": "ise-exporter"},
         }},
        path="test.toml",
        environ={"ISE_PASS": "secret",
                 "ISE_DATACONNECT_PASSWORD": "secret",
                 "ISE_PXGRID_PASSWORD": "secret"})


def test_publishing_the_plan_seeds_the_measured_load_counters():
    from ise_exporter3.plan import build_plan

    request_targets = ("pan", "mnt", "pxgrid")
    before = {target: _sample("ise3_load_measured_requests_total", target=target)
              for target in request_targets}
    oracle_before = _sample("ise3_load_measured_db_seconds_total",
                            target="oracle")
    telemetry.publish_plan(build_plan(_full_config()))
    for target in request_targets:
        assert _sample("ise3_load_measured_requests_total",
                       target=target) == (before[target] or 0.0)
    # Data Connect load is measured in database seconds, not requests, so the
    # oracle target gets a seed in that family and never a requests series.
    assert _sample("ise3_load_measured_db_seconds_total",
                   target="oracle") == (oracle_before or 0.0)
    assert _sample("ise3_load_measured_requests_total", target="oracle") is None


def test_publishing_the_plan_seeds_a_zero_per_planned_dataconnect_view():
    from ise_exporter3.plan import build_plan

    plan = build_plan(_full_config())
    views = {requirement[len("view:"):].lower()
             for entry in plan.entries
             if entry.enabled and entry.resolved
             and entry.provider.target == "oracle"
             for requirement in entry.provider.requires
             if str(requirement).startswith("view:")}
    assert views, "this config was meant to plan Data Connect work"
    before = {(view, result): _sample("ise3_dataconnect_queries_total",
                                      view=view, result=result)
              for view in views for result in ("success", "error")}
    telemetry.publish_plan(plan)
    for (view, result), prior in before.items():
        assert _sample("ise3_dataconnect_queries_total",
                       view=view, result=result) == (prior or 0.0)


def test_publishing_the_plan_seeds_detail_fetch_outcomes_for_resolved_caches():
    from ise_exporter3 import detail_cache
    from ise_exporter3.plan import build_plan

    plan = build_plan(_full_config())
    resolved = {(entry.name, entry.provider.name)
                for entry in plan.entries if entry.enabled and entry.resolved}
    # The pairs the seed table keys on must be ones this plan really produces,
    # or the loop below would pass by testing nothing.
    assert ("session_authorization", "mnt") in resolved
    assert ("network_devices", "ers") in resolved
    expected = [(cache, result)
                for cache, tickers, results in detail_cache.FETCH_OUTCOMES
                if any(pair in resolved for pair in tickers)
                for result in results]
    before = {pair: _sample("ise3_detail_fetches_total",
                            cache=pair[0], result=pair[1])
              for pair in expected}
    telemetry.publish_plan(plan)
    for (cache, result), prior in before.items():
        assert _sample("ise3_detail_fetches_total",
                       cache=cache, result=result) == (prior or 0.0)


def test_coverage_is_rolled_back_with_the_snapshot_it_describes():
    # publish_coverage wrote straight to the registry, so a failure after it --
    # a normalisation error, or the sample ceiling tripping on commit -- left
    # "20,000 of 20,000 groups, complete" describing rows that were discarded.
    def good(ctx):
        ctx.set(VALUE, 1, kind="alpha")
        reporting.publish_coverage(ctx, "marginals", 5, 5)

    def bad(ctx):
        reporting.publish_coverage(ctx, "marginals", 20_000, 20_000)
        ctx.fail("invalid_response", "nothing usable in the result")

    runner = _runner()
    assert runner.run(_entry(good), transport=None).ok
    assert _sample("ise3_topk_groups_returned",
                   dataset="probe", breakdown="marginals") == 5

    assert not runner.run(_entry(bad), transport=None).ok
    assert _sample("ise3_topk_groups_returned",
                   dataset="probe", breakdown="marginals") == 5


def test_coverage_does_not_count_against_the_dataset_sample_ceiling():
    # It is one gauge per breakdown in a family every dataset shares, not part
    # of this dataset's snapshot width.
    def fetch(ctx):
        ctx.set(VALUE, 1, kind="alpha")
        reporting.publish_coverage(ctx, "marginals", 1, 2)

    outcome = _runner().run(_entry(fetch), transport=None)
    assert outcome.ok
    assert _sample("ise3_topk_truncated",
                   dataset="probe", breakdown="marginals") == 1
    assert _sample("ise3_dataset_series", dataset="probe") == 1
