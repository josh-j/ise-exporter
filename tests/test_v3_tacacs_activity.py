"""The one bounded breakdown in the exporter, and what makes it honest.

``tacacs_activity`` has two large marginal dimensions: one group per Device
Administration account and one per device doing Device Admin. At the declared
scale that is 6,000 groups per marginal statement -- which is why the group
ceiling had to be derived rather than chosen -- and a series per device across
its three marginal statements is about 25,000 series for this dataset alone.

So the device dimension is bounded, and these tests pin the three properties
that separate a bound from a cap:

- the **scan stays complete**: every device is counted and rolled up, so the
  ops-owner totals are the whole fleet even when 200 devices are published;
- the bound is **declared in configuration**, not chosen in the collector;
- the bound **says so**, through ise3_topk_groups_returned / _total /
  ise3_topk_truncated, so a panel showing 200 of 5,000 cannot read as 5,000.
"""
import pytest
from prometheus_client import REGISTRY

from ise_exporter3 import nad_directory
from ise_exporter3.config import Config, ConfigError
from ise_exporter3.datasets import tacacs_activity as tacacs
from ise_exporter3.plan import PlannedDataset
from ise_exporter3.runtime import Runner
from ise_exporter3.transports import Transport


DEVICES = 300
ACCOUNTS = 20


class Oracle(Transport):
    """Answers each marginal statement with a full set of grouped rows."""

    target = "oracle"

    def __init__(self, devices=DEVICES, accounts=ACCOUNTS):
        self.devices, self.accounts = devices, accounts
        self.batches = []

    def _rows(self, measures):
        rows = []
        for index in range(self.accounts):
            rows.append({"dimension": "username", "value": f"admin{index:03d}",
                         **measures(index)})
        for index in range(self.devices):
            rows.append({"dimension": "device", "value": f"sw-{index:04d}",
                         **measures(index)})
        for row in rows:
            row["group_total"] = len(rows)
        return rows

    def query_many(self, statements, parameters=None):
        self.batches.append(list(statements))
        results = {}
        for name in statements:
            if name == "authorization_details":
                results[name] = [{
                    "username": "admin000",
                    "device": "sw-0000",
                    "policy": "Device Admin",
                    "shell_profile": "IOS Admin",
                    "command_set": "Show Commands",
                    "passed": 8.0,
                    "failed": 2.0,
                    "last_seen": 1_234_567.0,
                    "group_total": 1,
                }]
                continue
            if name == "commands":
                results[name] = self._rows(
                    lambda index: {
                        "events": float(index + 1),
                        "last_seen": 1_234_000.0 + index,
                    })
            else:
                results[name] = self._rows(
                    lambda index: {
                        "passed": 10.0,
                        "failed": float(index),
                        "last_seen": 1_234_000.0 + index,
                    })
        return results

    def prepare(self):
        return None

    def satisfies(self, requirements):
        return True, "", ""


def _config(**options):
    document = {"targets": {
        "pan": {"host": "pan1", "user": "ro"},
        "oracle": {"host": "mnt1", "user": "dataconnect", "service": "cpm10"}}}
    if options:
        document["datasets"] = {"tacacs_activity": {"options": options}}
    return Config.from_document(
        document, path="test.toml",
        environ={"ISE_PASS": "x", "ISE_DATACONNECT_PASSWORD": "y"})


def _entry():
    return PlannedDataset(
        name=tacacs.DATASET.name, description="", enabled=True, interval=21600,
        dataset=tacacs.DATASET, provider=tacacs.DATASET.providers[0])


def _run(config, transport=None):
    outcome = Runner(config).run(_entry(), transport or Oracle())
    assert outcome.ok, f"{outcome.reason}: {outcome.detail}"
    return outcome


@pytest.fixture(autouse=True)
def _directory():
    # The NAD directory is process-global; a test that leaves it populated
    # changes the ops_owner another test resolves.
    nad_directory.shared().replace(())
    yield
    nad_directory.shared().replace(())


def _own_every_device(owners=("net-ops", "campus-ops")):
    nad_directory.shared().replace([
        {"nad": f"sw-{index:04d}", "location": "site",
         "ops_owner": owners[index % len(owners)],
         "keys": (f"sw-{index:04d}",)}
        for index in range(DEVICES)])


def _sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels)


def _series(name, dimension):
    return sum(
        1 for metric in REGISTRY.collect() if metric.name == name
        for sample in metric.samples
        if sample.labels.get("dimension") == dimension)


def _coverage(breakdown):
    return (
        _sample("ise3_topk_groups_returned", dataset="tacacs_activity",
                breakdown=breakdown),
        _sample("ise3_topk_groups_total", dataset="tacacs_activity",
                breakdown=breakdown),
        _sample("ise3_topk_truncated", dataset="tacacs_activity",
                breakdown=breakdown),
    )


# --- the bound is declared, not chosen in the collector ---------------------

def test_the_device_bound_lives_in_configuration():
    assert tacacs.DATASET.option_names == ("device_rollup", "top_devices")
    assert _config().dataset("tacacs_activity").option("top_devices") == 200
    assert _config(top_devices=25).dataset("tacacs_activity").option(
        "top_devices") == 25


def test_an_unknown_option_names_itself_rather_than_being_ignored():
    with pytest.raises(ConfigError, match="unknown datasets.tacacs_activity"):
        _config(top_nads=25)


def test_a_negative_bound_is_refused():
    with pytest.raises(ConfigError, match="at least 0"):
        _config(top_devices=-1)


# --- item 1b: the device dimension is bounded -------------------------------

def test_only_the_declared_number_of_devices_gets_its_own_series():
    _run(_config(top_devices=10))
    # Ten devices, passed and failed, for each of the two outcome statements.
    assert _series("ise3_tacacs_authentications", "device") == 20
    assert _series("ise3_tacacs_authorizations", "device") == 20
    assert _series("ise3_tacacs_commands", "device") == 10


def test_the_devices_kept_are_the_ones_that_failed_most():
    # The whole justification for a top-K here: the tail is quiet devices, and
    # the question a Device Admin dashboard asks is "what is failing".
    _run(_config(top_devices=3))
    for index in range(DEVICES - 3, DEVICES):
        assert _sample("ise3_tacacs_authentications", provider="dataconnect",
                       dimension="device", value=f"sw-{index:04d}",
                       status="failed") == float(index)
    assert _sample("ise3_tacacs_authentications", provider="dataconnect",
                   dimension="device", value="sw-0000", status="failed") is None


def test_a_bounded_breakdown_says_how_much_of_the_fleet_it_is_showing():
    _run(_config(top_devices=10))
    for breakdown in ("authentications", "authorizations", "commands"):
        returned, total, truncated = _coverage(f"{breakdown}.device")
        assert (returned, total, truncated) == (10, DEVICES, 1)


def test_publishing_every_device_reports_itself_as_untruncated():
    _run(_config(top_devices=0))
    returned, total, truncated = _coverage("authentications.device")
    assert (returned, total, truncated) == (DEVICES, DEVICES, 0)
    assert _series("ise3_tacacs_authentications", "device") == DEVICES * 2


def test_the_scan_stays_complete_even_when_publication_is_bounded():
    # The rollup is computed from every device row, not from the published ones,
    # so the ops-owner totals are the fleet. This is the difference between
    # bounding what is published and bounding what is measured.
    _own_every_device()
    _run(_config(top_devices=5))

    published = sum(
        _sample("ise3_tacacs_authentications", provider="dataconnect",
                dimension="ops_owner", value=owner, status="failed")
        for owner in ("net-ops", "campus-ops"))
    assert published == sum(range(DEVICES))


def test_the_rollup_collapses_a_fleet_of_devices_into_a_handful_of_owners():
    _own_every_device()
    _run(_config(top_devices=0))
    assert _series("ise3_tacacs_authentications", "ops_owner") == 4  # 2 owners x 2


def test_an_unclassified_device_rolls_up_as_unknown_rather_than_vanishing():
    # network_devices fills the directory over its first several cycles, so a
    # cold start attributes everything to unknown -- which must still be counted.
    _run(_config(top_devices=0))
    assert _sample("ise3_tacacs_authentications", provider="dataconnect",
                   dimension="ops_owner", value="unknown",
                   status="failed") == sum(range(DEVICES))


def test_turning_the_rollup_off_publishes_no_owner_series():
    _own_every_device()
    _run(_config(device_rollup=False, top_devices=5))
    assert _series("ise3_tacacs_authentications", "ops_owner") == 0


# --- the dangerous settings warn at start -----------------------------------

def test_publishing_every_device_warns_with_the_series_count_it_implies():
    warnings = _config(top_devices=0).warnings
    assert any("top_devices = 0" in warning and "25,000 series" in warning
               for warning in warnings), warnings


def test_dropping_the_rollup_warns_that_devices_fall_out_of_every_breakdown():
    warnings = _config(device_rollup=False).warnings
    assert any("device_rollup = false" in warning for warning in warnings), warnings


def test_the_recommended_defaults_warn_about_nothing():
    assert not [warning for warning in _config().warnings
                if "tacacs_activity" in warning]


def test_a_disabled_dataset_says_nothing_about_bounds_it_will_never_apply():
    document = {
        "targets": {"pan": {"host": "pan1", "user": "ro"},
                    "oracle": {"host": "mnt1", "user": "dataconnect",
                               "service": "cpm10"}},
        "datasets": {"tacacs_activity": {
            "enabled": False, "options": {"top_devices": 0}}}}
    config = Config.from_document(
        document, path="test.toml",
        environ={"ISE_PASS": "x", "ISE_DATACONNECT_PASSWORD": "y"})
    assert not [warning for warning in config.warnings
                if "tacacs_activity" in warning]


# --- the statements themselves ----------------------------------------------

def test_the_dataset_fits_its_batch_at_the_declared_scale():
    # Item 1a and 1b together: three marginal statements, each able to return a
    # full set of groups, inside one batch. This is the exact arithmetic that
    # failed 8 of 8 collections before the ceilings were derived together.
    config = _config()
    limits = config.limits
    statements = tacacs.statements(6, limits)
    assert len(statements) <= limits.batch_queries
    needed = config.scale.nads + config.scale.accounts
    assert needed <= limits.group_ceiling
    assert len(statements) * limits.group_ceiling <= limits.batch_result_rows


def test_the_statements_still_group_on_marginals_rather_than_a_cross_product():
    statements = tacacs.statements(6, _config().limits)
    for name, sql in statements.items():
        if name != "authorization_details":
            assert "GROUPING SETS" in sql
        assert "epoch_time >=" in sql
        assert "COUNT(*) OVER ()" in sql
    detail = statements["authorization_details"]
    assert "authorization_policy" in detail
    assert "shell_profile" in detail
    assert "matched_command_set" in detail


def test_authorization_context_and_account_last_seen_are_published_together():
    _run(_config())
    detail_labels = {
        "provider": "dataconnect",
        "username": "admin000",
        "device": "sw-0000",
        "policy": "Device Admin",
        "shell_profile": "IOS Admin",
        "command_set": "Show Commands",
    }
    assert _sample(
        "ise3_tacacs_authorization_details",
        **detail_labels,
        status="passed",
    ) == 8.0
    assert _sample(
        "ise3_tacacs_authorization_details",
        **detail_labels,
        status="failed",
    ) == 2.0
    assert _sample(
        "ise3_tacacs_account_last_seen_timestamp",
        provider="dataconnect",
        username="admin000",
        event_type="authorizations",
    ) == 1_234_000.0


def test_ranking_is_stable_between_collections_on_a_tie():
    rows = [{"value": "sw-b", "passed": 1, "failed": 5},
            {"value": "sw-a", "passed": 1, "failed": 5},
            {"value": "sw-c", "passed": 9, "failed": 0}]
    assert [row["value"] for row in tacacs.rank_devices(rows, 2)] == ["sw-a", "sw-b"]
    assert len(tacacs.rank_devices(rows, 0)) == 3
