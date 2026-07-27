"""The cost/load model is the basis of every budget claim, so its arithmetic and
its validation rules are pinned here."""
import pytest

from ise_exporter3.model import (
    Cost,
    Dataset,
    Load,
    ModelError,
    Provider,
    Scale,
)


def test_fixed_cost_scales_only_with_cadence():
    cost = Cost(target="pan", requests=4)
    scale = Scale(nads=5000, endpoints=90000)
    hourly = cost.load(3600, scale)
    assert hourly.requests_per_hour == pytest.approx(4)
    assert cost.load(300, scale).requests_per_hour == pytest.approx(48)


def test_scaled_cost_resolves_against_declared_fleet_size():
    cost = Cost(target="pan", scales_with="nads", requests_per_1k=20)
    # 5000 NADs is five units of a thousand.
    assert cost.requests_for(Scale(nads=5000)) == pytest.approx(100)
    assert cost.requests_for(Scale(nads=500)) == pytest.approx(10)


def test_duty_cycle_is_database_seconds_as_a_share_of_wall_time():
    # 36 seconds of Oracle work per hour is 1% of wall time.
    load = Load(target="oracle", db_seconds_per_hour=36.0)
    assert load.duty_cycle_percent == pytest.approx(1.0)
    every_five_minutes = Cost(target="oracle", db_seconds=3.0).load(300, Scale())
    assert every_five_minutes.db_seconds_per_hour == pytest.approx(36.0)
    assert every_five_minutes.duty_cycle_percent == pytest.approx(1.0)


def test_streaming_provider_reports_a_stream_and_still_charges_its_repolls():
    cost = Cost(target="pxgrid", requests=1, streaming=True)
    load = cost.load(900, Scale())
    assert load.streams == 1
    assert load.requests_per_hour == pytest.approx(4)


def test_loads_add_within_a_target_and_refuse_across_targets():
    combined = Load("pan", requests_per_hour=10) + Load("pan", requests_per_hour=5)
    assert combined.requests_per_hour == pytest.approx(15)
    with pytest.raises(ModelError):
        Load("pan") + Load("mnt")


def test_only_the_oracle_target_may_declare_database_seconds():
    # Charging "database time" to the PAN would silently corrupt the duty-cycle
    # budget, which is the one ceiling expressed in time rather than requests.
    with pytest.raises(ModelError, match="oracle"):
        Cost(target="pan", db_seconds=5.0)


def test_a_per_1k_component_requires_naming_the_dimension_it_scales_with():
    with pytest.raises(ModelError, match="scales_with"):
        Cost(target="pan", requests_per_1k=10)


def test_multi_request_entities_scale_warmup_and_churn_honestly():
    cost = Cost(
        target="pan",
        requests=1,
        scales_with="policy_sets",
        warmup_requests=20,
        churn_fraction=0.01,
        churn_interval=300,
        requests_per_entity=2,
    )
    scale = Scale(policy_sets=100)
    assert cost.warmup_requests_for(scale) == 21
    assert cost.cycles_to_warm(scale) == 10
    assert cost.requests_for(scale) == pytest.approx(3)
    # Turnover is a rate: three times the cadence, three times the share, so
    # the per-cycle cost rises and the hourly cost does not fall.
    assert cost.requests_for(scale, 900) == pytest.approx(7)


def test_a_churn_share_must_name_the_cadence_it_was_measured_over():
    # Without it a longer interval silently reports a saving the runtime cannot
    # deliver, which is the one thing the load model exists to prevent.
    with pytest.raises(ModelError):
        Cost(target="mnt", scales_with="sessions", churn_fraction=0.01)


def test_churn_cannot_exceed_the_whole_fleet():
    # Past the point where a cycle outlives the average session everything has
    # turned over, so cost stops rising per cycle and does start falling hourly.
    cost = Cost(target="mnt", requests=1, scales_with="sessions",
                warmup_requests=2000, churn_fraction=0.01, churn_interval=300)
    assert cost.churn_for(300) == pytest.approx(0.01)
    assert cost.churn_for(900) == pytest.approx(0.03)
    assert cost.churn_for(30_000) == pytest.approx(1.0)
    assert cost.churn_for(86_400) == pytest.approx(1.0)


def test_unknown_targets_and_dimensions_are_rejected():
    with pytest.raises(ModelError):
        Cost(target="switch")
    with pytest.raises(ModelError):
        Cost(target="pan", scales_with="racks", requests_per_1k=1)


def test_provider_target_comes_from_its_cost():
    provider = Provider(name="ers", cost=Cost(target="pan", requests=1))
    assert provider.target == "pan"


def test_live_requirements_must_be_recognisable_kinds():
    # Anything the plan cannot classify offline would silently become
    # "unknown, assume fine", which is what v2 did with schema state.
    Provider(name="dc", cost=Cost(target="oracle"), requires=("view:ENDPOINTS_DATA",))
    Provider(name="px", cost=Cost(target="pxgrid"), requires=("capability:endpoints",))
    with pytest.raises(ModelError, match="view:"):
        Provider(name="dc", cost=Cost(target="oracle"), requires=("oracle_is_up",))


def test_dataset_rejects_duplicate_and_missing_providers():
    provider = Provider(name="ers", cost=Cost(target="pan", requests=1))
    with pytest.raises(ModelError, match="no providers"):
        Dataset(name="x", description="", providers=(), default_interval=60)
    with pytest.raises(ModelError, match="duplicate"):
        Dataset(name="x", description="", providers=(provider, provider),
                default_interval=60)


def test_dataset_provider_lookup_names_the_alternatives_on_a_miss():
    dataset = Dataset(
        name="x", description="", default_interval=60,
        providers=(Provider(name="ers", cost=Cost(target="pan", requests=1)),))
    assert dataset.provider("ers").name == "ers"
    assert dataset.provider_names == ("ers",)
    assert not dataset.multi_source
    with pytest.raises(ModelError, match="choose from ers"):
        dataset.provider("pxgrid")
