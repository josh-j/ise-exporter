from types import SimpleNamespace

from ise_exporter3 import limits as limits_module
from ise_exporter3.datasets import radius_reporting
from ise_exporter3.model import Scale


LIMITS = limits_module.for_scale(
    Scale(nads=5_000, endpoints=100_000, sessions=20_000, accounts=1_000)
)


def test_failure_context_and_distinct_endpoint_queries_are_explicitly_bounded():
    statements = radius_reporting.statements(6, LIMITS)
    assert len(statements) == LIMITS.batch_queries
    assert "COUNT(DISTINCT calling_station_id)" in statements["detail_totals"]
    context = statements["failure_context"]
    assert "device_name" in context
    assert "authentication_method" in context
    assert "COUNT(*) OVER ()" in context


def test_missing_optional_summary_fields_preserve_core_radius_breakdowns():
    schema = {
        "RADIUS_AUTHENTICATION_SUMMARY": {
            "TIMESTAMP", "DEVICE_NAME", "ISE_NODE", "PASSED_COUNT",
            "FAILED_COUNT",
        },
        "RADIUS_AUTHENTICATIONS": {
            "TIMESTAMP", "DEVICE_NAME", "AUTHENTICATION_METHOD",
        },
    }
    statements = radius_reporting.statements(6, LIMITS, schema)
    assert "detail_totals" not in statements
    summary = statements["summary_marginals"]
    assert "failure_reason" not in summary
    assert "authorization_profiles" not in summary
    assert "GROUPING SETS" in summary


def test_fetch_publishes_failure_marginals_correlation_and_repeat_auth_inputs():
    class Transport:
        schema = {
            "RADIUS_AUTHENTICATION_SUMMARY": {
                "TIMESTAMP", "DEVICE_NAME", "ISE_NODE", "PASSED_COUNT",
                "FAILED_COUNT", "CALLING_STATION_ID", "FAILURE_REASON",
                "AUTHORIZATION_PROFILES", "LOCATION", "IDENTITY_STORE",
            },
            "RADIUS_AUTHENTICATIONS": {
                "TIMESTAMP", "DEVICE_NAME", "AUTHENTICATION_METHOD",
                "AUTHENTICATION_PROTOCOL", "AUTHORIZATION_RULE", "ISE_NODE",
                "FAILED", "RESPONSE_TIME",
            },
        }

        def query_many(self, statements):
            assert set(statements) == {
                "totals",
                "summary_marginals",
                "detail_marginals",
                "failure_context",
                "detail_totals",
            }
            return {
                "totals": [{"passed": 90, "failed": 10}],
                "summary_marginals": [
                    {
                        "dimension": "failure_class",
                        "value": "credentials",
                        "passed": 0,
                        "failed": 6,
                        "group_total": 2,
                    },
                    {
                        "dimension": "identity_store",
                        "value": "Active Directory",
                        "passed": 80,
                        "failed": 8,
                        "group_total": 2,
                    },
                ],
                "detail_marginals": [],
                "failure_context": [{
                    "nad": "switch-1",
                    "method": "dot1x",
                    "events": 4,
                    "group_total": 1,
                }],
                "detail_totals": [{"endpoints": 25}],
            }

    class Context:
        dataset = SimpleNamespace(
            name="radius_reporting",
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
    radius_reporting.fetch(ctx)

    assert (
        "ise3_radius_distinct_endpoints_total",
        25.0,
        {},
    ) in ctx.samples
    assert (
        "ise3_radius_failure_summary",
        6.0,
        {"dimension": "failure_class", "value": "credentials"},
    ) in ctx.samples
    assert (
        "ise3_radius_failures_by_nad_method",
        4.0,
        {"nad": "switch-1", "method": "dot1x"},
    ) in ctx.samples
