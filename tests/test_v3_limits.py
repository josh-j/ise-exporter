"""The ceilings, and the contract that stops them drifting apart again.

This file exists because of a specific production-scale failure. Three constants
in three modules bounded the same piece of work and disagreed:
``reporting.MAX_GROUPS = 5500`` capped one statement, ``MAX_BATCH_QUERIES = 5``
allowed five per batch, and ``MAX_BATCH_RESULT_ROWS = 12_000`` was smaller than
three full-size statements. ``tacacs_activity`` failed 8 of 8 collections over a
simulated day with ``response_too_large`` -- and paid for every Oracle scan it
made before aborting.

Two things had to become true, and each has a test here:

- the batch ceiling is a **product**, not a choice, so it cannot be smaller than
  the statements allowed into it;
- the group ceiling clears the **declared scale**, so the safety valve does not
  fire at exactly the size the exporter is built for.
"""
import pytest

from ise_exporter3 import limits as limits_module
from ise_exporter3.config import Config, ConfigError
from ise_exporter3.limits import Limits, LimitsError
from ise_exporter3.model import Scale


PRODUCTION = Scale(nads=5_000, endpoints=100_000, sessions=20_000, accounts=1_000)
LAB = Scale(nads=200, endpoints=5_000, sessions=1_000, accounts=50)


def _config(document=None):
    return Config.from_document(
        document or {}, path="test.toml", environ={})


# --- item 1a: the batch ceiling is derived ----------------------------------

def test_the_batch_ceiling_is_the_product_it_was_supposed_to_be():
    limits = limits_module.derive(PRODUCTION)
    assert limits.batch_result_rows == limits.batch_queries * limits.result_rows


@pytest.mark.parametrize("scale", [PRODUCTION, LAB, Scale()])
def test_a_full_batch_of_full_statements_fits_the_batch_ceiling(scale):
    # The exact assertion the roadmap asks for, over every scale a profile can
    # produce: five statements of MAX_GROUPS rows each must be representable,
    # because that is a batch the reporting datasets legitimately build.
    limits = limits_module.derive(scale)
    assert limits.group_ceiling * limits.batch_queries <= limits.batch_result_rows


def test_a_statement_returning_every_group_it_asked_for_is_not_refused():
    # _check_ceilings refuses at equality, so result_rows sitting *on* the group
    # ceiling would fail any statement that returned a full set of groups.
    limits = limits_module.derive(PRODUCTION)
    assert limits.result_rows > limits.group_ceiling


def test_the_derived_batch_is_far_inside_the_byte_ceiling():
    # Nothing is given up by the larger row ceiling: the real safety bound is
    # bytes, and the largest permitted batch is a few MB against 64 MiB.
    limits = limits_module.derive(PRODUCTION)
    estimated = limits.batch_result_rows * limits_module.ESTIMATED_ROW_BYTES
    assert estimated < limits.result_bytes / 8


def test_a_contradiction_is_refused_at_load_not_discovered_in_production():
    # This is the old configuration, written out by hand. It must not load.
    with pytest.raises(ConfigError, match="smaller than 5 full-size statements"):
        _config({"limits": {"group_ceiling": 5_500, "batch_result_rows": 12_000}})


def test_a_row_ceiling_under_the_group_ceiling_is_refused():
    with pytest.raises(LimitsError, match="must exceed"):
        Limits(group_ceiling=6_000, result_rows=6_000, batch_queries=5,
               batch_result_rows=35_000)


def test_a_hard_bound_is_not_a_preference():
    with pytest.raises(ConfigError, match="hard bound"):
        _config({"limits": {"batch_queries": 500}})


def test_an_unknown_limit_names_itself():
    with pytest.raises(ConfigError, match="unknown limits key: max_rows"):
        _config({"limits": {"max_rows": 6_000}})


def test_a_limit_must_be_an_integer():
    with pytest.raises(ConfigError, match="must be an integer"):
        _config({"limits": {"group_ceiling": 6_500.5}})


# --- the group ceiling follows the fleet ------------------------------------

def test_the_group_ceiling_clears_the_declared_scale():
    # The defect in one line: 5,000 NADs plus 1,000 accounts is 6,000 groups in
    # a single marginal statement, and the ceiling was 5,500.
    limits = limits_module.derive(PRODUCTION)
    assert limits.group_ceiling >= PRODUCTION.nads + PRODUCTION.accounts


def test_the_group_ceiling_follows_the_scale_rather_than_a_constant():
    small = limits_module.derive(LAB)
    large = limits_module.derive(Scale(nads=40_000, endpoints=100_000,
                                       sessions=20_000, accounts=5_000))
    assert small.group_ceiling < large.group_ceiling
    assert large.group_ceiling >= 45_000


def test_a_scale_beyond_the_hard_cap_clamps_rather_than_derives_forever():
    huge = limits_module.derive(Scale(nads=5_000_000, endpoints=100_000,
                                      sessions=20_000, accounts=1_000))
    low, high = limits_module.HARD_BOUNDS["group_ceiling"]
    assert huge.group_ceiling == high


# --- dangerous values are said out loud at start ----------------------------

def test_a_group_ceiling_below_the_fleet_warns_by_name():
    config = _config({"limits": {"group_ceiling": 5_500}})
    assert any("limits.group_ceiling is 5,500" in warning
               and "truncated=1" in warning
               for warning in config.warnings), config.warnings


def test_overriding_a_derived_ceiling_says_the_derivation_no_longer_applies():
    config = _config({"limits": {"result_rows": 9_000}})
    assert any("overriding the 7,000 derived from the declared scale" in warning
               for warning in config.warnings), config.warnings


def test_a_row_ceiling_the_byte_ceiling_would_stop_first_warns():
    config = _config({
        "limits": {"batch_result_rows": 200_000, "result_bytes": 2 * 1024 * 1024}})
    assert any("byte ceiling will stop batches before the row ceiling" in warning
               for warning in config.warnings), config.warnings


def test_a_widened_snapshot_ceiling_warns_that_the_guard_is_weaker():
    config = _config({"limits": {"snapshot_samples": 400_000}})
    assert any("no longer be stopped by this ceiling" in warning
               for warning in config.warnings), config.warnings


def test_a_default_configuration_produces_no_limit_warnings():
    # The warnings have to stay rare enough to be read. A config that names no
    # ceiling at all should produce none of them.
    config = _config()
    assert not [warning for warning in config.warnings
                if warning.startswith("limits.")], config.warnings


# --- what the operator can see ----------------------------------------------

def test_every_ceiling_is_named_described_and_attributed():
    limits = limits_module.derive(PRODUCTION, {"batch_queries": 4})
    described = {name: (value, origin)
                 for name, value, origin, _text in limits.describe(PRODUCTION)}
    assert set(described) == set(limits_module.FIELDS)
    assert described["batch_queries"] == (4, "declared")
    assert described["group_ceiling"][1] == "derived from scale"
    assert described["window_hours"][1] == "default"
    assert all(text for _n, _v, _o, text in limits.describe(PRODUCTION))


def test_the_plan_publishes_the_ceilings_it_will_enforce():
    from ise_exporter3.plan import build_plan, render_plan

    plan = build_plan(_config())
    assert plan.to_dict()["limits"]["group_ceiling"]["value"] == \
        plan.config.limits.group_ceiling
    rendered = render_plan(plan)
    assert "group_ceiling" in rendered and "batch_result_rows" in rendered


# --- the catalogue is not a reporting query ---------------------------------

def test_the_catalogue_ceiling_does_not_shrink_with_a_small_estate():
    """Found on first contact with a live 4-NAD lab, not by the simulator.

    Schema discovery reads one row per (view, column) from the Oracle data
    dictionary. That result is a property of the ISE release -- the real
    appliance returned 70 views and 1,090 columns -- and has nothing to do with
    fleet size. Deriving its ceiling from [scale] gave `result_rows = 1005` on
    that lab, which refused the catalogue outright and left every Data Connect
    dataset at `schema_pending` forever.
    """
    tiny = limits_module.derive(Scale(nads=4, endpoints=24_016, sessions=28,
                                      accounts=1))
    assert tiny.result_rows < 1_090, "the estate really is that small"
    assert tiny.catalog_rows > 1_090, "the catalogue must still fit"
    assert tiny.catalog_rows == limits_module.DEFAULT_CATALOG_ROWS

    # And it does not move with the estate in either direction.
    huge = limits_module.derive(PRODUCTION)
    assert huge.catalog_rows == tiny.catalog_rows


def test_a_catalogue_ceiling_too_small_for_a_real_ise_warns():
    config = _config({"limits": {"catalog_rows": 1_200}})
    assert any("schema_pending" in warning for warning in config.warnings), \
        config.warnings


def test_a_large_estate_may_exceed_the_catalogue_ceiling_without_complaint():
    # result_rows and catalog_rows bound different shapes of query and are
    # deliberately independent: a 40,000-NAD estate legitimately reads more rows
    # in one breakdown than the whole data dictionary holds.
    large = limits_module.derive(Scale(nads=40_000, endpoints=100_000,
                                       sessions=20_000, accounts=5_000))
    assert large.result_rows > large.catalog_rows
