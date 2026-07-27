"""Plan resolution is where source selection and load budgeting meet. These tests
pin the two behaviours the whole design rests on: the first *reachable* provider
in the declared order wins, and every rejection is recorded with a reason rather
than leaving a silently empty dataset."""
import json
from pathlib import Path

import pytest

from ise_exporter3 import datasets as registry
from ise_exporter3.config import Config
from ise_exporter3.model import TARGETS, Scale
from ise_exporter3.plan import (
    build_plan,
    render_plan,
    warming_entries,
    warmup_report,
)

EXAMPLE_CONFIG = Path(__file__).parents[1] / "ise-exporter3.toml.example"


FULL_ENV = {
    "ISE_PASS": "pan-secret",
    "ISE_DATACONNECT_PASSWORD": "oracle-secret",
    "ISE_PXGRID_PASSWORD": "pxgrid-secret",
}

ALL_TARGETS = {
    "pan": {"host": "pan1.example.com", "user": "ers.readonly"},
    "mnt": {"host": "mnt1.example.com"},
    "oracle": {"host": "mnt1.example.com", "user": "dataconnect", "service": "cpm10"},
    "pxgrid": {"host": "px1.example.com", "node_name": "ise-exporter"},
}


def _plan(*, targets=None, datasets=None, budget=None, environ=None, profile="production"):
    document = {
        "profile": profile,
        "targets": ALL_TARGETS if targets is None else targets,
        "datasets": datasets or {},
    }
    if budget is not None:
        document["budget"] = budget
    config = Config.from_document(
        document, path="test.toml",
        environ=FULL_ENV if environ is None else environ)
    return build_plan(config)


def _entry(plan, name):
    return next(entry for entry in plan.entries if entry.name == name)


def test_first_reachable_provider_in_the_declared_order_wins():
    entry = _entry(_plan(), "active_sessions")
    assert entry.provider.name == "pxgrid"
    assert not entry.degraded
    assert not entry.rejected
    # The remaining reachable providers stay available as fallbacks.
    assert [p.name for p in entry.alternatives] == ["mnt", "dataconnect"]


def test_an_unreachable_preference_falls_back_and_says_why():
    without_pxgrid = {key: value for key, value in ALL_TARGETS.items() if key != "pxgrid"}
    entry = _entry(_plan(targets=without_pxgrid), "active_sessions")
    assert entry.provider.name == "mnt"
    assert entry.degraded
    assert entry.rejected[0].name == "pxgrid"
    assert "targets.pxgrid.host" in entry.rejected[0].reason
    assert [p.name for p in entry.alternatives] == ["dataconnect"]


def test_a_dataset_with_no_reachable_provider_is_named_not_silently_empty():
    plan = _plan(targets={"mnt": {"host": "mnt1.example.com"}}, environ={})
    entry = _entry(plan, "endpoint_attributes")
    assert not entry.resolved
    assert entry.provider is None
    assert [item.name for item in entry.rejected] == ["pxgrid"]
    assert entry in plan.unresolved
    assert entry.load is None


def test_operator_provider_order_overrides_the_declared_preference():
    plan = _plan(datasets={"active_sessions": {"providers": ["dataconnect", "mnt"]}})
    entry = _entry(plan, "active_sessions")
    assert entry.provider.name == "dataconnect"
    # pxGrid was not offered at all, so it is not a rejection.
    assert not entry.rejected
    assert [p.name for p in entry.alternatives] == ["mnt"]


def test_a_disabled_dataset_costs_nothing():
    enabled = _plan()
    disabled = _plan(datasets={"radius_reporting": {"enabled": False}})
    entry = _entry(disabled, "radius_reporting")
    assert not entry.enabled
    assert entry.load is None
    before = next(t for t in enabled.targets if t.target == "oracle")
    after = next(t for t in disabled.targets if t.target == "oracle")
    assert after.load.db_seconds_per_hour < before.load.db_seconds_per_hour


def test_halving_an_interval_doubles_that_dataset_s_load():
    slow = _plan(datasets={"psn_performance": {"interval": "10m"}})
    fast = _plan(datasets={"psn_performance": {"interval": "5m"}})
    slow_load = _entry(slow, "psn_performance").load.db_seconds_per_hour
    fast_load = _entry(fast, "psn_performance").load.db_seconds_per_hour
    assert fast_load == pytest.approx(slow_load * 2)


def test_scaled_costs_track_the_declared_fleet_size():
    small = _plan(profile="lab")
    large = _plan(profile="production")
    # network_devices scales with NAD count: 200 nads -> 5000 nads.
    assert (_entry(large, "network_devices").load.requests_per_hour
            == pytest.approx(_entry(small, "network_devices").load.requests_per_hour * 25))


def test_shared_work_is_charged_once_not_once_per_dataset():
    # session_authorization and posture_current are separate datasets, but one
    # cached per-MAC fan-out answers both. Charging each the full budget would
    # report double the real load.
    without_pxgrid = {key: value for key, value in ALL_TARGETS.items() if key != "pxgrid"}
    plan = _plan(targets=without_pxgrid)
    authorization = _entry(plan, "session_authorization")
    posture = _entry(plan, "posture_current")

    assert authorization.provider.name == "mnt" and posture.provider.name == "mnt"
    assert posture.shared_with == ("session_authorization",)
    assert authorization.charged and not posture.charged
    assert posture.load.requests_per_hour == 0
    # Steady state is churn: 1% of 20,000 sessions per 5-minute cycle.
    assert authorization.load.requests_per_hour == pytest.approx((1 + 200) * 12)


def test_session_authorization_interval_changes_its_pooled_request_rate():
    # posture_current reads the same ActiveList and detail cache, but it never
    # fetches uncached per-session details. When authorization slows from five
    # to ten minutes, posture's five-minute runs add only the six intervening
    # ActiveList requests -- they must not keep 200 detail fetches/cycle charged
    # at the old cadence.
    without_pxgrid = {key: value for key, value in ALL_TARGETS.items()
                      if key != "pxgrid"}
    fast = _plan(
        targets=without_pxgrid,
        datasets={"session_authorization": {"interval": "5m"}})
    slow = _plan(
        targets=without_pxgrid,
        datasets={"session_authorization": {"interval": "10m"}})

    fast_load = _entry(fast, "session_authorization").load.requests_per_hour
    slow_load = _entry(slow, "session_authorization").load.requests_per_hour
    assert fast_load == pytest.approx(2_412)
    assert slow_load == pytest.approx(1_212)
    assert slow_load < fast_load
    assert next(t for t in slow.targets if t.target == "mnt").load.requests_per_hour < (
        next(t for t in fast.targets if t.target == "mnt").load.requests_per_hour)


def test_a_converging_provider_is_budgeted_on_what_it_costs_once_warm():
    # The warm-up burst is bounded and transient. Budgeting on it would overstate
    # the steady state forever; hiding it would understate the first hour. The
    # plan reports both, and the budget checks the steady figure.
    without_pxgrid = {key: value for key, value in ALL_TARGETS.items() if key != "pxgrid"}
    entry = _entry(_plan(targets=without_pxgrid), "session_authorization")
    cost = entry.provider.cost
    scale = Scale(sessions=20_000)

    assert cost.converges
    assert cost.warmup_requests_for(scale) > cost.requests_for(scale) * 5
    assert cost.cycles_to_warm(scale) == 10       # 20,000 at 2,000 per cycle
    assert entry.load.requests_per_hour == pytest.approx(
        cost.requests_for(scale) * 12)


def test_the_plan_reports_the_warmup_cost_and_time_to_full_coverage():
    without_pxgrid = {key: value for key, value in ALL_TARGETS.items() if key != "pxgrid"}
    text = render_plan(_plan(targets=without_pxgrid))
    assert "then get cheaper" in text
    assert "while warming" in text
    assert "full coverage in" in text


def test_the_plan_quotes_the_cadence_the_scheduler_will_actually_use():
    # It used to quote cycles times the dataset's own interval, so it said
    # network_devices reached full coverage in 60 hours. The scheduler paces a
    # filling cache from the budget instead, and the plan has to say the same
    # number the runtime will produce or it is not a plan.
    without_pxgrid = {key: value for key, value in ALL_TARGETS.items()
                      if key != "pxgrid"}
    plan = _plan(targets=without_pxgrid)
    entry = _entry(plan, "network_devices")
    report = warmup_report(plan, entry)

    assert report.interval < entry.interval
    assert report.seconds < 6 * 3600, "60 hours was the defect"
    assert "while filling rather than every" in render_plan(plan)


def test_a_warmup_the_budget_cannot_pace_says_the_budget_will_stretch_it():
    # session_authorization runs every five minutes, which is already faster
    # than its share of the MnT budget, so scheduling cannot slow it down --
    # the token bucket does. Reporting the unthrottled rate here is how "2.8x
    # the declared budget" got quoted as a plan.
    without_pxgrid = {key: value for key, value in ALL_TARGETS.items()
                      if key != "pxgrid"}
    plan = _plan(targets=without_pxgrid)
    report = warmup_report(plan, _entry(plan, "session_authorization"))

    assert report.throttled
    assert report.requests_per_hour > report.available_per_hour
    # Paced by the budget, the cold start is hours rather than the ~50 minutes
    # the unenforced cadence implies.
    assert report.seconds > 10 * 3600
    assert "the budget paces it, not the cadence" in render_plan(plan)


def test_a_pool_member_that_pays_nothing_is_not_charged_a_warmup_of_its_own():
    # posture_current reads the cache session_authorization fills. Printing a
    # warm-up budget for it too would read as two datasets each wanting the
    # whole MnT ceiling.
    without_pxgrid = {key: value for key, value in ALL_TARGETS.items()
                      if key != "pxgrid"}
    plan = _plan(targets=without_pxgrid)
    assert [entry.name for entry in warming_entries(plan, "mnt")] == [
        "session_authorization"]
    assert "posture_current (mnt): no requests of its own" in render_plan(plan)


def test_a_declared_warmup_burst_shortens_the_cold_start_it_is_declared_for():
    without_pxgrid = {key: value for key, value in ALL_TARGETS.items()
                      if key != "pxgrid"}
    baseline = warmup_report(
        _plan(targets=without_pxgrid), _entry(
            _plan(targets=without_pxgrid), "session_authorization"))
    plan = _plan(targets=without_pxgrid,
                 budget={"mnt": {"requests_per_hour": 4000,
                                 "warmup_requests_per_hour": 12000}})
    burst = warmup_report(plan, _entry(plan, "session_authorization"))

    assert burst.available_per_hour > baseline.available_per_hour
    assert burst.seconds < baseline.seconds
    text = render_plan(plan)
    assert "budget.mnt permits 12,000 req/h while any of them is filling" in text


def test_the_enumeration_a_converging_provider_pays_every_cycle_is_counted():
    # network_devices enumerates the whole NAD list at ERS's 100-row page cap on
    # every collection -- 50 requests at 5,000 NADs -- whether or not a detail
    # fetch follows. Dropping that from the converging path understated the
    # dataset tenfold and would have paced its warm-up above the ceiling.
    from ise_exporter3.datasets import network_devices

    scale = Scale(nads=5_000, endpoints=100_000, sessions=20_000, accounts=1_000)
    cost = network_devices.DATASET.providers[0].cost
    enumeration = cost.requests_per_1k * scale.units_of("nads")

    assert enumeration == pytest.approx(50)
    assert cost.warmup_requests_for(scale) == pytest.approx(
        enumeration + cost.warmup_requests)
    assert cost.requests_for(scale) == pytest.approx(
        enumeration + scale.nads * cost.churn_fraction)


def test_pooling_does_not_apply_when_members_choose_different_providers():
    # pxGrid posture rides the session stream and costs nothing extra there, so
    # the MnT fan-out is charged alone.
    plan = _plan(datasets={
        "session_authorization": {"providers": ["mnt"]},
        "posture_current": {"providers": ["pxgrid"]}})
    authorization = _entry(plan, "session_authorization")
    posture = _entry(plan, "posture_current")
    assert authorization.shared_with == () and posture.shared_with == ()
    assert posture.target == "pxgrid"


def test_target_totals_sum_only_the_datasets_actually_using_that_target():
    plan = _plan()
    for target in plan.targets:
        expected = sum(
            entry.load.requests_per_hour
            for entry in plan.enabled
            if entry.resolved and entry.target == target.target)
        assert target.load.requests_per_hour == pytest.approx(expected)


def test_a_plan_over_its_request_ceiling_is_reported_with_the_reason():
    plan = _plan(budget={"pan": {"requests_per_hour": 1}})
    pan = next(target for target in plan.targets if target.target == "pan")
    assert pan.over_budget
    assert "exceeds" in pan.overage_reason
    assert not plan.fits
    assert pan in plan.overages


def test_a_plan_over_its_duty_cycle_ceiling_is_reported_with_the_reason():
    plan = _plan(budget={"oracle": {"duty_cycle_percent": 0.001}})
    oracle = next(target for target in plan.targets if target.target == "oracle")
    assert oracle.over_budget
    assert "duty cycle" in oracle.overage_reason
    assert not plan.fits


def test_a_generous_budget_fits():
    plan = _plan(budget={
        "pan": {"requests_per_hour": 1_000_000},
        "mnt": {"requests_per_hour": 1_000_000},
        "pxgrid": {"requests_per_hour": 1_000_000},
        "oracle": {"duty_cycle_percent": 100.0},
    })
    assert plan.fits
    assert not plan.overages


def test_utilisation_is_none_when_no_ceiling_is_declared():
    plan = _plan(budget={"pan": {"requests_per_hour": 0}})
    pan = next(target for target in plan.targets if target.target == "pan")
    assert pan.utilisation is None
    assert not pan.over_budget


def test_live_only_requirements_are_deferred_not_guessed():
    entry = _entry(_plan(), "radius_reporting")
    assert "view:RADIUS_AUTHENTICATION_SUMMARY" in entry.deferred
    # Offline planning must not claim a view exists.
    assert entry.resolved


def test_plan_is_json_serialisable_for_the_operator_api():
    payload = _plan().to_dict()
    assert json.loads(json.dumps(payload))["profile"] == "production"
    active = next(item for item in payload["datasets"]
                  if item["dataset"] == "active_sessions")
    assert active["provider"] == "pxgrid"
    assert active["streaming"] is True
    assert active["alternatives"] == ["mnt", "dataconnect"]
    assert "posture_status" in active["supplies"]


def test_rendered_report_states_the_source_the_cost_and_the_verdict():
    without_pxgrid = {key: value for key, value in ALL_TARGETS.items() if key != "pxgrid"}
    text = render_plan(_plan(targets=without_pxgrid,
                             budget={"pan": {"requests_per_hour": 1}}))
    assert "active_sessions" in text
    assert "fell back from pxgrid" in text
    assert "EXCEEDS the declared budget" in text
    assert "Raise the budget" in text


def test_rendered_report_confirms_a_plan_that_fits():
    text = render_plan(_plan(budget={
        "pan": {"requests_per_hour": 1_000_000},
        "mnt": {"requests_per_hour": 1_000_000},
        "pxgrid": {"requests_per_hour": 1_000_000},
        "oracle": {"duty_cycle_percent": 100.0},
    }))
    assert "Plan fits the declared budget." in text


def test_the_shipped_example_config_fits_its_own_budget():
    # The example is the reference an operator copies. If it ships over budget
    # it teaches that the ceiling is decorative, which is the habit v3 exists to
    # break. Adding a dataset or shortening a cadence must not quietly break it.
    config = Config.load(EXAMPLE_CONFIG, environ={
        "ISE_PASS": "x", "ISE_DATACONNECT_PASSWORD": "y"})
    plan = build_plan(config)
    assert plan.fits, render_plan(plan)
    # The example leaves pxGrid unconfigured, which is the common case. Exactly
    # one dataset has no other source, and it is the one whose data genuinely
    # has none -- anything else unresolved would be a broken example.
    assert [entry.name for entry in plan.unresolved] == ["endpoint_attributes"]


def test_the_example_config_documents_every_registered_dataset_or_takes_defaults():
    # A dataset absent from the example is fine -- it takes its declared default
    # -- but it must still resolve, so a new dataset cannot land unplanned.
    config = Config.load(EXAMPLE_CONFIG, environ={
        "ISE_PASS": "x", "ISE_DATACONNECT_PASSWORD": "y"})
    planned = {entry.name for entry in build_plan(config).entries}
    assert planned == set(registry.names())


def test_every_registered_dataset_is_planned_exactly_once():
    plan = _plan()
    planned = [entry.name for entry in plan.entries]
    assert sorted(planned) == sorted(registry.names())
    assert len(planned) == len(set(planned))


def test_every_declared_provider_targets_a_known_persona():
    for dataset in registry.all_datasets():
        for provider in dataset.providers:
            assert provider.target in TARGETS, (
                f"{dataset.name}/{provider.name} targets {provider.target!r}")


def test_multi_source_datasets_declare_what_each_provider_can_supply():
    # A fallback that silently supplies fewer dimensions is the failure mode
    # this design exists to make visible, so `supplies` must not be empty.
    for dataset in registry.all_datasets():
        if not dataset.multi_source:
            continue
        for provider in dataset.providers:
            assert provider.supplies, f"{dataset.name}/{provider.name} supplies nothing"


def test_the_default_profile_plans_within_budget_at_production_scale():
    # "Out of the box" means an operator who sets no profile, at ~100k endpoints
    # and ~5k NADs, gets a plan the exporter will actually start with.
    config = Config.load(EXAMPLE_CONFIG, environ={
        "ISE_PASS": "x", "ISE_DATACONNECT_PASSWORD": "y"})
    assert config.profile == "production"
    assert config.scale.endpoints == 100_000
    assert config.scale.nads == 5_000
    plan = build_plan(config)
    assert plan.fits, render_plan(plan)


def test_the_plan_states_what_a_selected_source_cannot_supply():
    # A fallback often answers a narrower question, and an operator who reads the
    # cadence but not this will trust a panel further than it deserves.
    text = render_plan(_plan())
    assert "What the selected source does and does not cover" in text
    assert "publishing profiling context" in text


def test_a_dataset_with_no_substitute_is_unavailable_rather_than_faked():
    # endpoint_attributes is pxGrid-only on purpose: model, OS and MDM have no
    # other source on ISE 3.3, and the ERS provider that used to stand in for it
    # supplied only `profile`, which endpoint_inventory already covers.
    without_pxgrid = {key: value for key, value in ALL_TARGETS.items()
                      if key != "pxgrid"}
    plan = _plan(targets=without_pxgrid)
    entry = _entry(plan, "endpoint_attributes")
    assert not entry.resolved
    assert entry in plan.unresolved
    assert "targets.pxgrid.host" in entry.rejected[0].reason


def test_every_provider_answers_how_much_of_the_fleet_it_measures():
    # There is no default that lets the question go unanswered: a per-cycle
    # request budget silently becoming a coverage ceiling is the easiest way to
    # ship a metric that looks like a fleet number and is not.
    from ise_exporter3.model import COVERAGE_KINDS

    for dataset in registry.all_datasets():
        for provider in dataset.providers:
            assert provider.coverage in COVERAGE_KINDS, (
                f"{dataset.name}/{provider.name}")
            if provider.cost.converges:
                assert provider.coverage == "converging", (
                    f"{dataset.name}/{provider.name} warms a cache but does not "
                    "declare converging coverage")
            if provider.coverage == "bounded":
                assert provider.notes, (
                    f"{dataset.name}/{provider.name} measures part of the fleet "
                    "and must say what it leaves out")


def test_a_partial_provider_is_called_out_in_the_plan():
    from ise_exporter3.model import Cost, Dataset, Provider

    bounded = Dataset(
        name="probe", description="", default_interval=300,
        providers=(Provider(
            name="mnt", cost=Cost(target="mnt", requests=1), coverage="bounded",
            notes="top 100 talkers only; the rest of the fleet is never sampled"),))
    assert bounded.providers[0].coverage == "bounded"


def test_a_cache_without_a_warmup_budget_cannot_claim_to_converge():
    from ise_exporter3.model import Cost, ModelError, Provider

    with pytest.raises(ModelError, match="nothing makes it converge"):
        Provider(name="mnt", cost=Cost(target="mnt", requests=1),
                 coverage="converging")
