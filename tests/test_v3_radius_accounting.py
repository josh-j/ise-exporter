from types import SimpleNamespace

from ise_exporter3 import limits as limits_module
from ise_exporter3.datasets import radius_accounting
from ise_exporter3.model import Scale


LIMITS = limits_module.for_scale(
    Scale(nads=5_000, endpoints=100_000, sessions=20_000, accounts=1_000)
)


def test_accounting_uses_outcomes_as_measures_and_complete_marginals():
    sql = radius_accounting.statements(6, LIMITS)["marginals"]
    assert "AS starts" in sql and "AS stops" in sql
    assert "GROUPING SETS" in sql
    assert "acct_status_type" not in sql.split("GROUPING SETS", 1)[1]
    assert "COUNT(*) OVER ()" in sql


def test_missing_optional_columns_preserve_total_accounting_events():
    schema = {"RADIUS_ACCOUNTING": {"TIMESTAMP", "ACCT_STATUS_TYPE"}}
    statements = radius_accounting.statements(6, LIMITS, schema)

    assert "device_name" not in statements["marginals"]
    assert "ise_node" not in statements["marginals"]
    assert "authorization_policy" not in statements["marginals"]
    assert "acct_session_time" not in statements["totals"]
    assert "'all'" in statements["marginals"]


def test_fetch_publishes_totals_dimensions_duration_and_coverage():
    class Transport:
        schema = {
            "RADIUS_ACCOUNTING": {
                "TIMESTAMP",
                "ACCT_STATUS_TYPE",
                "DEVICE_NAME",
                "ISE_NODE",
                "ACCT_SESSION_TIME",
            }
        }

        def query_many(self, statements, *, timeout=None):
            assert set(statements) == {"totals", "marginals"}
            # The accounting aggregates are the heaviest reads this exporter
            # issues; the dataset must declare the wider statement budget.
            assert timeout == radius_accounting.DATASET.option(
                "statement_timeout").default == 45
            return {
                "totals": [{
                    "starts": 10,
                    "stops": 8,
                    "other": 1,
                    "mean_duration": 120,
                    "max_duration": 900,
                    "duration_samples": 8,
                    "records": 19,
                }],
                "marginals": [{
                    "dimension": "nad",
                    "value": "switch-1",
                    "starts": 4,
                    "stops": 3,
                    "other": 0,
                    "mean_duration": 60,
                    "max_duration": 180,
                    "duration_samples": 3,
                    "records": 7,
                    "group_total": 1,
                }],
            }

    class Context:
        dataset = SimpleNamespace(
            name="radius_accounting",
            default_interval=1800,
        )
        limits = LIMITS
        transport = Transport()

        def __init__(self):
            self.samples = []

        def option(self, name):
            return radius_accounting.DATASET.option(name).default

        def set(self, family, sample_value, /, **labels):
            self.samples.append((family._name, sample_value, labels))

        def set_shared(self, family, sample_value, /, **labels):
            self.samples.append((family._name, sample_value, labels))

    ctx = Context()
    radius_accounting.fetch(ctx)

    assert (
        "ise3_radius_accounting_events",
        10.0,
        {"dimension": "total", "value": "all", "event_type": "starts"},
    ) in ctx.samples
    assert (
        "ise3_radius_accounting_events",
        3.0,
        {"dimension": "nad", "value": "switch-1", "event_type": "stops"},
    ) in ctx.samples
    assert (
        "ise3_radius_accounting_session_duration_seconds",
        180.0,
        {"dimension": "nad", "value": "switch-1", "statistic": "max"},
    ) in ctx.samples
    assert (
        "ise3_radius_accounting_duration_coverage",
        3 / 7,
        {"dimension": "nad", "value": "switch-1"},
    ) in ctx.samples


def test_the_always_null_policy_dimension_is_withheld_rather_than_published():
    # AUTHORIZATION_POLICY exists in RADIUS_ACCOUNTING and is NULL in 370 of 370
    # rows on ISE 3.3 P11, so the policy marginal is the grand total wearing a
    # dimension label. The schema presence check cannot see that.
    class Transport:
        schema = {"RADIUS_ACCOUNTING": {
            "TIMESTAMP", "ACCT_STATUS_TYPE", "DEVICE_NAME", "ISE_NODE",
            "AUTHORIZATION_POLICY"}}

        def query_many(self, statements, *, timeout=None):
            return {
                "totals": [{"starts": 338, "stops": 32, "other": 0,
                            "duration_samples": 4, "records": 370}],
                "marginals": [
                    {"dimension": "policy", "value": "unknown", "starts": 338,
                     "stops": 32, "other": 0, "duration_samples": 4,
                     "records": 370, "group_total": 6},
                    {"dimension": "nad", "value": "adlab-workstations",
                     "starts": 42, "stops": 3, "other": 0,
                     "duration_samples": 0, "records": 45, "group_total": 6},
                ],
            }

    class Context:
        dataset = SimpleNamespace(name="radius_accounting", default_interval=1800)
        limits = LIMITS
        transport = Transport()

        def __init__(self):
            self.samples = []
            self.shared = []

        def option(self, name):
            return radius_accounting.DATASET.option(name).default

        def set(self, family, sample_value, /, **labels):
            self.samples.append((family._name, sample_value, labels))

        def set_shared(self, family, sample_value, /, **labels):
            self.shared.append((family._name, sample_value, labels))

    ctx = Context()
    radius_accounting.fetch(ctx)

    dimensions = {labels.get("dimension") for _name, _value, labels in ctx.samples}
    assert "policy" not in dimensions
    assert {"total", "nad"} <= dimensions
    assert ("ise3_breakdown_dimension_populated", 0,
            {"dataset": "radius_accounting", "dimension": "policy"}) in ctx.shared
