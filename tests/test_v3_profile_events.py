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


class _Context:
    dataset = SimpleNamespace(name="profile_events", default_interval=21600)
    limits = LIMITS

    def __init__(self, transport):
        self.transport = transport
        self.samples = []
        self.shared = []

    def set(self, family, sample_value, /, **labels):
        self.samples.append((family._name, sample_value, labels))

    def set_shared(self, family, sample_value, /, **labels):
        self.shared.append((family._name, sample_value, labels))


def test_an_always_null_action_column_leaves_the_source_breakdown_standing():
    # ENDPOINT_ACTION_NAME is NULL in 290 of 290 rows on ISE 3.3 P11, so the
    # pair is a source breakdown with a constant label -- but SOURCE itself
    # carries four real probe names and must keep publishing.
    class Transport:
        schema = {"PROFILED_ENDPOINTS_SUMMARY": {
            "TIMESTAMP", "SOURCE", "ENDPOINT_ACTION_NAME"}}

        def query_many(self, statements):
            return {
                "totals": [{"events": 290}],
                "source_action": [
                    {"source": "RADIUS Probe", "action": "unknown",
                     "events": 229, "group_total": 4},
                    {"source": "DHCP Probe", "action": "unknown",
                     "events": 8, "group_total": 4},
                ],
            }

    ctx = _Context(Transport())
    profile_events.fetch(ctx)

    assert ("ise3_endpoint_profile_events_by_source", 229.0,
            {"source": "RADIUS Probe"}) in ctx.samples
    assert not [sample for sample in ctx.samples
                if sample[0] == "ise3_endpoint_profile_events"]
    assert ("ise3_breakdown_dimension_populated", 0,
            {"dataset": "profile_events", "dimension": "action"}) in ctx.shared


def test_a_populated_action_column_still_publishes_the_pair():
    class Transport:
        schema = {"PROFILED_ENDPOINTS_SUMMARY": {
            "TIMESTAMP", "SOURCE", "ENDPOINT_ACTION_NAME"}}

        def query_many(self, statements):
            return {
                "totals": [{"events": 12}],
                "source_action": [
                    {"source": "RADIUS Probe", "action": "Profiled",
                     "events": 7, "group_total": 2},
                    {"source": "RADIUS Probe", "action": "unknown",
                     "events": 5, "group_total": 2},
                ],
            }

    ctx = _Context(Transport())
    profile_events.fetch(ctx)

    assert ("ise3_endpoint_profile_events", 7.0,
            {"source": "RADIUS Probe", "action": "Profiled"}) in ctx.samples
    assert ("ise3_endpoint_profile_events_by_source", 12.0,
            {"source": "RADIUS Probe"}) in ctx.samples
    assert ("ise3_breakdown_dimension_populated", 1,
            {"dataset": "profile_events", "dimension": "action"}) in ctx.shared
