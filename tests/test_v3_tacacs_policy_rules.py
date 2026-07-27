import pytest
from prometheus_client import REGISTRY

from ise_exporter3 import detail_cache
from ise_exporter3.config import Config
from ise_exporter3.datasets import tacacs_policy_rules as rules
from ise_exporter3.model import Scale
from ise_exporter3.plan import PlannedDataset
from ise_exporter3.runtime import Runner
from ise_exporter3.transports import Transport


POLICIES = [
    {"id": "ps1", "name": "Device Admin"},
    {"id": "ps2", "name": "Break Glass"},
    {"id": "ps3", "name": "Automation"},
]


class PAN(Transport):
    target = "pan"

    def __init__(self):
        self.calls = []

    def get_openapi(self, path, *, api="openapi", unwrap=True, params=None):
        self.calls.append(path)
        if path == "/policy/device-admin/policy-set":
            return POLICIES
        policy_id, rule_type = path.rsplit("/", 2)[-2:]
        count = {
            ("ps1", "authentication"): 2,
            ("ps1", "authorization"): 5,
            ("ps2", "authentication"): 1,
            ("ps2", "authorization"): 3,
            ("ps3", "authentication"): 4,
            ("ps3", "authorization"): 6,
        }[(policy_id, rule_type)]
        return [{"id": f"{policy_id}-{rule_type}-{index}"} for index in range(count)]


def _config():
    return Config.from_document(
        {"targets": {"pan": {"host": "pan1", "user": "ro"}}},
        path="test.toml",
        environ={"ISE_PASS": "secret"},
    )


def _entry():
    return PlannedDataset(
        name=rules.DATASET.name,
        description="",
        enabled=True,
        interval=rules.DATASET.default_interval,
        dataset=rules.DATASET,
        provider=rules.DATASET.providers[0],
    )


@pytest.fixture(autouse=True)
def _fresh_cache():
    detail_cache._CACHES.pop(rules.CACHE, None)
    yield
    detail_cache._CACHES.pop(rules.CACHE, None)


def _sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels)


def test_rule_inventory_converges_instead_of_sampling_policy_sets(monkeypatch):
    monkeypatch.setattr(rules, "WARMUP_POLICY_SETS_PER_CYCLE", 1)
    transport = PAN()
    coverage = []
    for _ in POLICIES:
        outcome = Runner(_config()).run(_entry(), transport)
        assert outcome.ok
        coverage.append(_sample("ise3_detail_cache_coverage", cache=rules.CACHE))

    assert coverage == [
        pytest.approx(1 / 3),
        pytest.approx(2 / 3),
        pytest.approx(1),
    ]
    assert _sample(
        "ise3_tacacs_policy_rule_count",
        provider="openapi",
        policy_set="Device Admin",
        rule_type="authorization",
    ) == 5
    assert _sample(
        "ise3_tacacs_policy_rules_total",
        provider="openapi",
        rule_type="authentication",
    ) == 7


def test_declared_cost_counts_two_requests_per_policy_set():
    cost = rules.DATASET.providers[0].cost
    scale = Scale(policy_sets=100)
    assert cost.warmup_requests_for(scale) == 21
    assert cost.cycles_to_warm(scale) == 10
    assert cost.requests_for(scale) == pytest.approx(1.2)
