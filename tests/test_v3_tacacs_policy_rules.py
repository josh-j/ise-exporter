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


def test_a_rule_row_nests_its_identity_and_is_still_counted():
    """ISE nests rule identity under a "rule" sub-object; the count must not
    depend on flat id/name fields the appliance does not send."""
    class Nested(PAN):
        def get_openapi(self, path, *, api="openapi", unwrap=True, params=None):
            rows = super().get_openapi(path, api=api, unwrap=unwrap, params=params)
            if path == "/policy/device-admin/policy-set":
                return rows
            return [{"rule": {"id": row["id"], "name": "Default", "rank": 0},
                     "identitySourceName": "All_User_ID_Stores"} for row in rows]

    assert Runner(_config()).run(_entry(), Nested()).ok
    assert _sample("ise3_tacacs_policy_rules_total", provider="openapi",
                   rule_type="authorization") == 14


# --- tacacs_config, against the real ERS and Device Admin payloads ----------

TACACS_CONFIG_RESPONSES = {
    "/config/internaluser": [
        {"id": "u1", "name": "cmluser"},
        {"id": "u2", "name": "svc-never-expires"},
        {"id": "u3", "name": "retired"},
    ],
    # As ISE 3.3 P11 sends it: passwordNeverExpires top-level, enabled a real
    # JSON boolean, and no passwordInfo object anywhere.
    "/config/internaluser/u1": {"InternalUser": {
        "id": "u1", "name": "cmluser", "enabled": True,
        "passwordNeverExpires": False, "daysForPasswordExpiration": 39}},
    "/config/internaluser/u2": {"InternalUser": {
        "id": "u2", "name": "svc-never-expires", "enabled": True,
        "passwordNeverExpires": True}},
    "/config/internaluser/u3": {"InternalUser": {
        "id": "u3", "name": "retired", "enabled": False,
        "passwordNeverExpires": False}},
    "/policy/device-admin/policy-set": [{"id": "ps1", "name": "Default"}],
    # Bare lists, and DenyAllCommands appears in both under one id.
    "/policy/device-admin/command-sets": [
        {"name": "DenyAllCommands", "id": "cs1"}],
    "/policy/device-admin/shell-profiles": [
        {"name": "WLC ALL", "id": "sp1"},
        {"name": "WLC MONITOR", "id": "sp2"},
        {"name": "Deny All Shell Profile", "id": "sp3"},
        {"name": "Default Shell Profile", "id": "sp4"},
        {"name": "DenyAllCommands", "id": "cs1"},
    ],
}


def _run_tacacs_config():
    from ise_exporter3.datasets import tacacs_config
    from tests.test_v3_datasets import FakeTransport

    detail_cache._CACHES.pop(tacacs_config.CACHE, None)
    entry = PlannedDataset(
        name=tacacs_config.DATASET.name, description="", enabled=True,
        interval=tacacs_config.DATASET.default_interval,
        dataset=tacacs_config.DATASET,
        provider=tacacs_config.DATASET.providers[0])
    outcome = Runner(_config()).run(entry, FakeTransport(TACACS_CONFIG_RESPONSES))
    detail_cache._CACHES.pop(tacacs_config.CACHE, None)
    return outcome


def test_password_never_expires_is_read_from_where_ise_sends_it():
    """The headline hygiene signal, read from passwordInfo -- an object ISE has
    never sent -- was a hardcoded 0 across every account on the appliance."""
    assert _run_tacacs_config().ok
    assert _sample("ise3_tacacs_internal_account_hygiene_risk", provider="ers",
                   username="svc-never-expires",
                   risk="password_never_expires") == 1
    assert _sample("ise3_tacacs_internal_account_hygiene_risk", provider="ers",
                   username="cmluser", risk="password_never_expires") == 0
    # A JSON boolean false, not the string "false", still reads as enabled.
    assert _sample("ise3_tacacs_internal_account_enabled", provider="ers",
                   username="retired") == 0
    assert _sample("ise3_tacacs_internal_account_hygiene_risk", provider="ers",
                   username="retired", risk="disabled_account_retained") == 1


def test_shell_profiles_do_not_count_the_command_sets_ise_mirrors_into_them():
    assert _run_tacacs_config().ok
    assert _sample("ise3_tacacs_policy_objects", provider="ers",
                   object_type="command_sets") == 1
    # Five rows come back, but one of them is the command set by id.
    assert _sample("ise3_tacacs_policy_objects", provider="ers",
                   object_type="shell_profiles") == 4


def test_no_never_used_risk_is_invented():
    # ISE returns no last-login field of any kind, so the dataset must not
    # publish a risk it cannot evaluate.
    from ise_exporter3.datasets import tacacs_config

    risks = tacacs_config.hygiene_risks(
        {"name": "cmluser", "enabled": True, "passwordNeverExpires": False})
    assert set(risks) == {"password_never_expires", "disabled_account_retained"}
