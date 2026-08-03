"""Reporting breakdowns must ramp to complete coverage, not cap.

A top-K breakdown that drops its tail looks exactly like a complete one, and the
tail is where the quiet NAD, the rare error code and the one broken policy live.
These tests pin the two things that make completeness affordable: grouping on
marginals rather than a cross product, and carrying outcome as a measure rather
than as a dimension.
"""
import pytest

from ise_exporter3 import datasets as registry
from ise_exporter3 import limits as limits_module
from ise_exporter3 import reporting
from ise_exporter3.model import Scale
from ise_exporter3.datasets import (
    posture_history,
    radius_accounting,
    radius_errors,
    radius_reporting,
    tacacs_activity,
)


REPORTING_DATASETS = (radius_reporting, radius_accounting, radius_errors,
                      posture_history, tacacs_activity)


# The ceilings a production-scale configuration resolves to, so a statement
# here is built against exactly what the transport would accept.
LIMITS = limits_module.for_scale(
    Scale(nads=5_000, endpoints=100_000, sessions=20_000, accounts=1_000))


def _all_statements(module):
    return module.statements(6, LIMITS).values()


# --- the completeness contract ---------------------------------------------

def test_no_data_connect_provider_settles_for_partial_coverage():
    # The remaining bounded providers are pxGrid endpoint sources, where the
    # limit is ISE's publishing behaviour rather than our query shape.
    partial = [
        f"{dataset.name}/{provider.name}"
        for dataset in registry.all_datasets()
        for provider in dataset.providers
        if provider.coverage == "bounded" and provider.target == "oracle"]
    assert not partial, f"reporting datasets should be complete: {partial}"


def test_rolling_posture_and_source_freshness_update_within_an_hour():
    from ise_exporter3.datasets import source_freshness

    assert posture_history.DATASET.default_interval <= 3600
    assert source_freshness.DATASET.default_interval <= 3600


@pytest.mark.parametrize("module", REPORTING_DATASETS,
                         ids=lambda m: m.DATASET.name)
def test_breakdowns_group_on_marginals_not_a_cross_product(module):
    # A cross product is what makes a breakdown unaffordable and forces a cap.
    grouped = [sql for sql in _all_statements(module) if "GROUP BY" in sql]
    assert grouped, f"{module.DATASET.name} has no grouped statement"
    assert any("GROUPING SETS" in sql for sql in grouped), (
        f"{module.DATASET.name} groups without marginals, so it will need a cap")


@pytest.mark.parametrize("module", REPORTING_DATASETS,
                         ids=lambda m: m.DATASET.name)
def test_every_statement_bounds_its_scan(module):
    for sql in _all_statements(module):
        assert "NUMTODSINTERVAL" in sql or "epoch_time >=" in sql, (
            f"{module.DATASET.name} has an unbounded scan")


@pytest.mark.parametrize("module", REPORTING_DATASETS,
                         ids=lambda m: m.DATASET.name)
def test_every_grouped_statement_can_report_its_own_truncation(module):
    for sql in _all_statements(module):
        if "GROUP BY" in sql:
            assert "COUNT(*) OVER ()" in sql, (
                f"{module.DATASET.name} cannot tell whether it truncated")


def test_outcome_is_a_measure_so_it_cannot_double_the_group_count():
    # Passed/failed as a dimension doubles every group; as two columns it adds
    # two. That is what keeps a 5,000-NAD breakdown inside one statement.
    marginals = radius_reporting.statements(6, LIMITS)["summary_marginals"]
    assert "AS passed" in marginals and "AS failed" in marginals
    assert "GROUPING SETS" in marginals
    dimensions = dict(radius_reporting.SUMMARY_DIMENSIONS)
    assert "status" not in dimensions and "failed" not in dimensions


def test_a_bounded_pair_is_still_allowed_where_both_sides_are_small():
    # policy x status is tens by six, and the pair is the question: "how many
    # endpoints failed which policy" cannot be rebuilt from marginals. The test
    # is whether both sides are bounded, not a blanket ban on cross products.
    sql = posture_history.statements(6, LIMITS)["policy_status"]
    assert "GROUP BY NVL(posture_policy" in sql
    assert "GROUPING SETS" not in sql


# --- the helper -------------------------------------------------------------

def test_marginals_produce_one_row_set_per_dimension():
    sql = reporting.marginals("some_view", "1 = 1", (
        ("a", "NVL(col_a, 'unknown')"),
        ("b", "NVL(col_b, 'unknown')"),
    ), limits=LIMITS)
    assert "GROUPING SETS ((NVL(col_a, 'unknown')), (NVL(col_b, 'unknown')))" in sql
    # GROUPING() distinguishes the super-aggregate NULL from a literal 'unknown',
    # which is why the dimension name cannot be inferred from the value alone.
    assert "GROUPING(NVL(col_a, 'unknown')) = 0 THEN 'a'" in sql
    assert "COALESCE(" in sql


def test_marginal_rows_separate_cleanly_by_dimension():
    rows = [
        {"dimension": "nad", "value": "sw-01", "events": 5},
        {"dimension": "nad", "value": "sw-02", "events": 3},
        {"dimension": "psn", "value": "psn1", "events": 8},
    ]
    grouped = reporting.by_dimension(rows)
    assert set(grouped) == {"nad", "psn"}
    assert len(grouped["nad"]) == 2


def test_the_row_cap_is_a_safety_valve_not_a_design_target():
    # It sits under the transport's row ceiling so a fleet far past the declared
    # scale degrades loudly rather than taking the dataset down -- but at the
    # declared scale nothing should reach it, which is the half that was wrong:
    # a fixed 5,500 was below the 6,000 groups a 5,000-NAD, 1,000-account estate
    # produces, so the valve fired at exactly the size this exporter is built for.
    assert reporting.group_limit(LIMITS) < LIMITS.result_rows
    assert reporting.group_limit(LIMITS) >= 5_000 + 1_000


def test_a_statement_may_ask_for_fewer_groups_but_never_for_more():
    assert reporting.group_limit(LIMITS, 100) == 100
    assert reporting.group_limit(LIMITS, LIMITS.group_ceiling * 10) == \
        LIMITS.group_ceiling


def _scan_ctx(default_interval, interval):
    from types import SimpleNamespace
    return SimpleNamespace(
        dataset=SimpleNamespace(default_interval=default_interval),
        interval=interval, limits=LIMITS)


def test_a_longer_configured_cadence_widens_the_scan_it_is_paired_with():
    # radius_reporting at "12h" scanned one hour and published a twelfth of the
    # traffic as if it were all of it, with dataset_up and dataset_fresh at 1.
    assert reporting.scan_window(_scan_ctx(1800, 43200)) == LIMITS.window_hours
    assert reporting.scan_window(_scan_ctx(1800, 7200)) == 2


def test_a_shorter_configured_cadence_never_shrinks_the_window():
    # The window is the question for nad_health: six hours of silence is a dead
    # switch, seventy minutes of it is a quiet one.
    assert reporting.scan_window(_scan_ctx(21600, 3600)) == LIMITS.window_hours


def test_the_scan_never_exceeds_the_declared_ceiling():
    assert reporting.scan_window(_scan_ctx(21600, 86400)) == LIMITS.window_hours


def test_every_window_scanned_dataset_declares_itself_windowed():
    # The plan's uncoverable-cadence check reads this flag; a reporting dataset
    # added without it would be missed silently.
    for module in REPORTING_DATASETS:
        assert module.DATASET.windowed, module.DATASET.name


def test_truncation_reports_returned_against_existing():
    class Ctx:
        class dataset:
            name = "probe"

        shared = []

        @classmethod
        def set_shared(cls, family, sample_value, /, **labels):
            cls.shared.append((family._name, sample_value, labels))

    rows = [{"group_total": 4200}] * 100
    assert reporting.publish_truncation(Ctx, "marginals", rows) is True

    exact = [{"group_total": 3}] * 3
    assert reporting.publish_truncation(Ctx, "marginals", exact) is False


def test_a_single_dimension_probe_does_not_emit_a_one_argument_coalesce():
    # COALESCE needs at least two arguments; Oracle answers ORA-00938. A
    # one-dimension marginal is a legitimate shape -- posture conditions have
    # exactly one -- and this was only caught against a live appliance.
    sql = reporting.marginals(
        "some_view", "1 = 1", (("a", "NVL(col_a, 'x')"),), limits=LIMITS)
    assert "COALESCE" not in sql
    assert "GROUPING SETS ((NVL(col_a, 'x')))" in sql

    pair = reporting.marginals("some_view", "1 = 1", (
        ("a", "NVL(col_a, 'x')"), ("b", "NVL(col_b, 'y')")), limits=LIMITS)
    assert "COALESCE(NVL(col_a, 'x'), NVL(col_b, 'y'))" in pair


# --- dimensions that exist and hold nothing --------------------------------

class _DimensionCtx:
    """Minimal context that records what a fetch published."""

    def __init__(self, name="probe"):
        from types import SimpleNamespace
        self.dataset = SimpleNamespace(name=name, default_interval=21600)
        self.limits = LIMITS
        self.samples = []
        self.shared = []

    def set(self, family, sample_value, /, **labels):
        self.samples.append((family._name, sample_value, labels))

    def set_shared(self, family, sample_value, /, **labels):
        self.shared.append((family._name, sample_value, labels))


def test_a_dimension_that_is_null_on_every_row_is_withheld_not_published():
    # ISE 3.3 P11 carries four of these: the column exists, passes every schema
    # presence check, and is NULL on every row. The marginal then repeats the
    # dataset total under one 'unknown' label and reads as a breakdown.
    ctx = _DimensionCtx()
    rows = [
        {"dimension": "policy", "value": "unknown", "events": 370},
        {"dimension": "nad", "value": "campus-corp-wired", "events": 327},
        {"dimension": "nad", "value": "adlab-workstations", "events": 43},
    ]
    live = reporting.live_dimensions(ctx, rows)

    assert set(live) == {"nad"}
    assert ("ise3_breakdown_dimension_populated", 0,
            {"dataset": "probe", "dimension": "policy"}) in ctx.shared
    assert ("ise3_breakdown_dimension_populated", 1,
            {"dataset": "probe", "dimension": "nad"}) in ctx.shared


def test_a_dimension_with_no_rows_at_all_is_not_declared_dead():
    # An empty window is no evidence either way, and calling it a dead column
    # would be the same invention this check exists to stop.
    ctx = _DimensionCtx()
    assert reporting.live_dimensions(ctx, []) == {}
    assert ctx.shared == []


def test_one_real_value_beside_placeholders_keeps_the_dimension():
    ctx = _DimensionCtx()
    rows = [{"dimension": "identity_group", "value": "none", "endpoints": 24_000},
            {"dimension": "identity_group", "value": "Employee", "endpoints": 16}]
    assert set(reporting.live_dimensions(ctx, rows)) == {"identity_group"}


def test_endpoint_inventory_names_the_unavailable_identity_group_breakdown():
    from ise_exporter3.datasets import endpoint_inventory

    class Transport:
        def query_many(self, statements):
            return {
                "totals": [{
                    "endpoints": 24_016, "posture_yes": 24_016, "posture_no": 0,
                    "unprofiled": 80, "profile_present": 24_016,
                    "identity_group_present": 0,
                    "posture_applicable_present": 24_016,
                }],
                # IDENTITY_GROUP_ID is NULL on all 24,016 rows of the appliance.
                "marginals": [
                    {"dimension": "identity_group", "value": "none",
                     "endpoints": 24_016, "group_total": 21},
                    {"dimension": "profile", "value": "Windows10-Workstation",
                     "endpoints": 8_990, "group_total": 21},
                ],
            }

    ctx = _DimensionCtx("endpoint_inventory")
    ctx.transport = Transport()
    endpoint_inventory.fetch(ctx)

    published = {name for name, _value, _labels in ctx.samples}
    assert "ise3_endpoints_by_profile" in published
    assert "ise3_endpoints_by_identity_group" in published
    assert ("ise3_endpoints_by_identity_group", 0,
            {"identity_group": "Unavailable from Data Connect"}) in ctx.samples
    assert ("ise3_endpoint_inventory_field_coverage", 0.0,
            {"field": "identity_group"}) in ctx.samples
    assert ("ise3_breakdown_dimension_populated", 0,
            {"dataset": "endpoint_inventory",
             "dimension": "identity_group"}) in ctx.shared


# --- freshness: stale, empty and missing are three different answers -------

def test_the_freshness_probe_ages_the_view_not_the_window():
    from ise_exporter3.datasets import source_freshness

    for sql in source_freshness.statements().values():
        # The age must not sit under any predicate: a view staler than the
        # window is exactly the view an operator is asking about, and it used
        # to publish no age at all. No predicate also means no COUNT-shaped
        # full scan -- every fact reads off the MAX() the timestamp index
        # answers, which is what lets the probe fit the statement budget at
        # production row counts.
        assert "WHERE" not in sql
        assert "COUNT" not in sql
        assert "MAX(" in sql
        assert "AS has_rows" in sql
        # The marker routes the statement to the freshness_probe telemetry
        # label instead of billing the first view the batch happens to name.
        assert "ise_exporter:freshness" in sql


def test_a_stale_view_publishes_an_age_and_an_empty_one_does_not():
    from ise_exporter3.datasets import source_freshness

    class Transport:
        def query_many(self, statements, *, tolerant=False):
            assert tolerant
            return {"batch_0": [
                # Four days stale on the appliance, but it does hold rows.
                {"view_name": "radius_accounting", "has_rows": 1,
                 "age_seconds": 367_104},
                # Empty all-time: no newest row, so no age exists to publish.
                {"view_name": "radius_errors_view", "has_rows": 0,
                 "age_seconds": -1},
                # Newest row inside the window: recent, and the age publishes.
                {"view_name": "radius_authentications", "has_rows": 1,
                 "age_seconds": 90},
            ]}, {}

    ctx = _DimensionCtx("source_freshness")
    ctx.interval = 21600
    ctx.transport = Transport()
    source_freshness.fetch(ctx)

    assert ("ise3_source_latest_row_age_seconds", 367_104.0,
            {"view": "radius_accounting"}) in ctx.samples
    assert ("ise3_source_has_rows", 1,
            {"view": "radius_accounting"}) in ctx.samples
    assert ("ise3_source_has_recent_rows", 0,
            {"view": "radius_accounting"}) in ctx.samples
    assert ("ise3_source_has_rows", 0,
            {"view": "radius_errors_view"}) in ctx.samples
    assert ("ise3_source_has_recent_rows", 0,
            {"view": "radius_errors_view"}) in ctx.samples
    assert ("ise3_source_has_recent_rows", 1,
            {"view": "radius_authentications"}) in ctx.samples
    assert not [sample for sample in ctx.samples
                if sample[0] == "ise3_source_latest_row_age_seconds"
                and sample[2] == {"view": "radius_errors_view"}]


def test_endpoints_data_recency_respects_the_documented_sync_delay():
    from ise_exporter3.datasets import source_freshness

    # Cisco syncs the view's non-real-time attributes up to 12 hours behind.
    # An 8-hour-old newest row is therefore normal operation for
    # endpoints_data and a stale feed for an event view -- the same age, two
    # different verdicts, and the probe must not report the documented sync
    # as an outage.
    class Transport:
        def query_many(self, statements, *, tolerant=False):
            return {"batch_1": [
                {"view_name": "endpoints_data", "has_rows": 1,
                 "age_seconds": 8 * 3600},
                {"view_name": "radius_errors_view", "has_rows": 1,
                 "age_seconds": 8 * 3600},
            ]}, {}

    ctx = _DimensionCtx("source_freshness")
    ctx.interval = 21600
    ctx.transport = Transport()
    source_freshness.fetch(ctx)

    assert ("ise3_source_has_recent_rows", 1,
            {"view": "endpoints_data"}) in ctx.samples
    assert ("ise3_source_has_recent_rows", 0,
            {"view": "radius_errors_view"}) in ctx.samples
    # Beyond even the documented sync delay, stale is stale.
    ctx = _DimensionCtx("source_freshness")
    ctx.interval = 21600
    ctx.transport = type("T", (), {"query_many": lambda self, s, tolerant=False: (
        {"batch_1": [{"view_name": "endpoints_data", "has_rows": 1,
                      "age_seconds": 14 * 3600}]}, {})})()
    source_freshness.fetch(ctx)
    assert ("ise3_source_has_recent_rows", 0,
            {"view": "endpoints_data"}) in ctx.samples


def test_a_slow_view_silences_only_its_own_batch():
    from ise_exporter3.datasets import source_freshness
    from ise_exporter3.transports import TransportError

    # A diagnostic dataset that goes completely dark because one view cannot
    # aggregate inside the statement budget is silent exactly when it is
    # needed. The views the failed statements carried are named by probed=0
    # and publish no freshness facts -- unknown, not missing.
    class Transport:
        def query_many(self, statements, *, tolerant=False):
            assert tolerant
            return (
                {"batch_2": [
                    {"view_name": "key_performance_metrics", "has_rows": 1,
                     "age_seconds": 120},
                ]},
                {"batch_0": TransportError("timeout"),
                 "batch_1": TransportError("timeout")},
            )

    ctx = _DimensionCtx("source_freshness")
    ctx.interval = 21600
    ctx.transport = Transport()
    source_freshness.fetch(ctx)

    assert ("ise3_source_probed", 0,
            {"view": "radius_authentications"}) in ctx.samples
    assert ("ise3_source_probed", 1,
            {"view": "key_performance_metrics"}) in ctx.samples
    assert ("ise3_source_has_recent_rows", 1,
            {"view": "key_performance_metrics"}) in ctx.samples
    assert not [sample for sample in ctx.samples
                if sample[0] == "ise3_source_has_rows"
                and sample[2] == {"view": "radius_authentications"}]


def test_a_probe_where_nothing_answered_fails_with_a_classified_reason():
    from ise_exporter3.datasets import source_freshness
    from ise_exporter3.transports import TransportError

    class Transport:
        def query_many(self, statements, *, tolerant=False):
            return {}, {name: TransportError("timeout") for name in statements}

    ctx = _DimensionCtx("source_freshness")
    ctx.interval = 21600
    ctx.transport = Transport()
    with pytest.raises(TransportError) as caught:
        source_freshness.fetch(ctx)
    assert caught.value.reason == "timeout"
    assert not ctx.samples


def test_nad_health_publishes_the_age_of_the_feed_it_judges_silence_by():
    from ise_exporter3.datasets import nad_health

    sql = nad_health._source_age()
    assert "WHERE" not in sql
    assert "MAX(timestamp)" in sql
