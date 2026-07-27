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

        def query_many(self, statements):
            assert set(statements) == {"totals", "marginals"}
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
