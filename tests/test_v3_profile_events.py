from types import SimpleNamespace

from ise_exporter3 import limits as limits_module
from ise_exporter3.datasets import profile_events
from ise_exporter3.model import Scale


LIMITS = limits_module.for_scale(
    Scale(nads=5_000, endpoints=100_000, sessions=20_000, accounts=1_000)
)


def test_profile_events_keep_the_small_source_action_pair():
    sql = profile_events.statements(6, LIMITS)["source_action"]
    assert "GROUP BY NVL(source, 'unknown'), NVL(endpoint_action_name, 'unknown')" in sql
    assert "COUNT(*) OVER ()" in sql
    assert "NUMTODSINTERVAL" in sql


def test_missing_optional_profile_columns_degrade_to_unknown():
    schema = {"PROFILED_ENDPOINTS_SUMMARY": {"TIMESTAMP"}}
    sql = profile_events.statements(6, LIMITS, schema)["source_action"]
    assert "NVL(source" not in sql
    assert "endpoint_action_name" not in sql
    assert "SELECT 'unknown' AS source, 'unknown' AS action" in sql


def test_fetch_publishes_total_and_source_action_pairs():
    class Transport:
        schema = {
            "PROFILED_ENDPOINTS_SUMMARY": {
                "TIMESTAMP",
                "SOURCE",
                "ENDPOINT_ACTION_NAME",
            }
        }

        def query_many(self, statements):
            assert set(statements) == {"totals", "source_action"}
            return {
                "totals": [{"events": 9}],
                "source_action": [{
                    "source": "RADIUS Probe",
                    "action": "Profiled",
                    "events": 7,
                    "group_total": 1,
                }],
            }

    class Context:
        dataset = SimpleNamespace(name="profile_events", default_interval=21600)
        limits = LIMITS
        transport = Transport()

        def __init__(self):
            self.samples = []

        def set(self, family, sample_value, /, **labels):
            self.samples.append((family._name, sample_value, labels))

        def set_shared(self, family, sample_value, /, **labels):
            self.samples.append((family._name, sample_value, labels))

    ctx = Context()
    profile_events.fetch(ctx)
    assert (
        "ise3_endpoint_profile_events_total",
        9.0,
        {},
    ) in ctx.samples
    assert (
        "ise3_endpoint_profile_events",
        7.0,
        {"source": "RADIUS Probe", "action": "Profiled"},
    ) in ctx.samples
