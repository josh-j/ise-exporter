"""Per-endpoint authorization detail, and the property that makes it affordable.

The authorization decision is stable for the life of a session, so each MAC is
fetched once and cached. That turns the per-cycle budget from a coverage ceiling
into a warm-up rate limit: these tests pin that coverage actually reaches the
whole active set, that a warm cache costs only churn, and that the aggregates are
distinct-MAC counts rather than one series per endpoint.
"""
import time
from types import SimpleNamespace

import pytest
from prometheus_client import REGISTRY

from ise_exporter3 import detail_cache
from ise_exporter3.config import Config
from ise_exporter3.datasets import session_authorization as authz
from ise_exporter3.model import Scale
from ise_exporter3.plan import PlannedDataset
from ise_exporter3.runtime import Runner
from ise_exporter3.transports import Transport


def _detail(mac, *, nad="sw-01", location="All Locations#Germany#Ramstein AB",
            nas_ip="10.1.1.1", passed=True, method="dot1x", profile="PermitAccess",
            rule="Basic_Authenticated_Access", policy_set="Wired Open Mode",
            failure_reason="", total_latency="", step_latency="",
            response_time="", message_code=""):
    # Shaped like the appliance: ISE 3.3 carries both latencies as attributes of
    # other_attr_string and neither as an element, and sends no failure_reason.
    attributes = [
        f"ISEPolicySetName={policy_set}",
        f"AuthorizationPolicyMatchedRule={rule}",
    ]
    if step_latency:
        attributes.append(f"StepLatency={step_latency}")
    if total_latency:
        attributes.append(f"TotalAuthenLatency={total_latency}")
    record = {
        "calling_station_id": mac,
        "network_device_name": nad,
        "location": location,
        "nas_ip_address": nas_ip,
        "authentication_method": method,
        "selected_azn_profiles": profile,
        "other_attr_string": ":!:" + ":!:".join(attributes) + ":!:",
    }
    record["passed" if passed else "failed"] = "true"
    if response_time:
        record["response_time"] = response_time
    if message_code:
        record["message_code"] = message_code
    if failure_reason:
        record["failure_reason"] = failure_reason
    return record


class MnT(Transport):
    """Serves an ActiveList and per-MAC detail, counting detail requests."""

    target = "mnt"

    def __init__(self, macs, details=None, fail=()):
        self.macs = list(macs)
        self.details = details or {}
        self.fail = set(fail)
        self.detail_requests = []
        # The active list is a session index: ISE 3.3 sends no identity group,
        # NAD name or posture status, so scope can key only on the NAS address.
        self.nas_ips = {}
        self.extra_sessions = []

    def _session(self, mac):
        session = {"calling_station_id": mac}
        if mac in self.nas_ips:
            session["nas_ip_address"] = self.nas_ips[mac]
        return session

    def get_mnt_xml(self, path, *, api="mnt_xml"):
        if path.endswith("/ActiveList"):
            rows = [self._session(mac) for mac in self.macs] + self.extra_sessions
            return {"total": len(rows), "sessions": rows}
        mac = path.rsplit("/", 1)[-1]
        self.detail_requests.append(mac)
        if mac in self.fail:
            raise RuntimeError("MnT said no")
        return {"total": 0, "sessions": [self.details.get(mac, _detail(mac))]}


def _config():
    return Config.from_document(
        {"targets": {"pan": {"host": "pan1", "user": "ro"},
                     "mnt": {"host": "mnt1"}}},
        path="test.toml", environ={"ISE_PASS": "secret"})


def _entry():
    return PlannedDataset(
        name=authz.DATASET.name, description="", enabled=True, interval=300,
        dataset=authz.DATASET, provider=authz.DATASET.providers[0])


def _run(transport):
    """One collection cycle.

    The active list is cached for a minute so two datasets in the same tick
    share one read. These tests simulate successive *cycles* in quick
    succession, so the cache is cleared between them -- production gets a fresh
    read every cadence because the cadence is longer than the TTL.
    """
    detail_cache._CACHES.pop(authz.ACTIVE_LIST_CACHE, None)
    return Runner(_config()).run(_entry(), transport)


@pytest.fixture(autouse=True)
def _fresh_cache():
    # The detail cache and the NAD directory are process-global shared state, so
    # a test that leaves either populated changes another test's answer.
    from ise_exporter3 import nad_directory
    for cache in (authz.CACHE, authz.ACTIVE_LIST_CACHE):
        detail_cache._CACHES.pop(cache, None)
    nad_directory.shared().replace(())
    yield
    for cache in (authz.CACHE, authz.ACTIVE_LIST_CACHE):
        detail_cache._CACHES.pop(cache, None)
    nad_directory.shared().replace(())


def _sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels)


def _coverage():
    return _sample("ise3_detail_cache_coverage", cache=authz.CACHE)


# --- convergence ------------------------------------------------------------

def test_coverage_reaches_the_whole_active_set_rather_than_sampling_it(monkeypatch):
    # The point of the cache. A flat per-cycle budget with no cache would leave
    # coverage stuck at budget/fleet forever.
    monkeypatch.setattr(authz, "WARMUP_FETCHES_PER_CYCLE", 10)
    macs = [f"00:11:22:33:44:{index:02X}" for index in range(50)]
    transport = MnT(macs)

    coverages = []
    for _ in range(5):
        assert _run(transport).ok
        coverages.append(_coverage())

    assert coverages == [pytest.approx(v) for v in (0.2, 0.4, 0.6, 0.8, 1.0)]
    assert len(transport.detail_requests) == 50


def test_a_warm_cache_costs_only_what_turned_over(monkeypatch):
    monkeypatch.setattr(authz, "WARMUP_FETCHES_PER_CYCLE", 100)
    macs = [f"00:11:22:33:44:{index:02X}" for index in range(20)]
    transport = MnT(macs)

    _run(transport)
    assert len(transport.detail_requests) == 20
    # Nothing changed, so nothing is re-fetched: the decision is stable for the
    # life of the session.
    _run(transport)
    assert len(transport.detail_requests) == 20

    # One new endpoint appears; only it is fetched.
    transport.macs.append("00:11:22:33:44:FF")
    _run(transport)
    assert len(transport.detail_requests) == 21
    assert transport.detail_requests[-1] == "00:11:22:33:44:FF"


def test_a_still_active_session_is_re_read_only_after_its_declared_refresh():
    # retain() already drops departed MACs, so this bound is not what keeps the
    # cache finite. Its only effect is re-reading a session that never left --
    # the one thing that catches a mid-session CoA, and the one avoidable
    # request when nothing changed.
    macs = ["00:11:22:33:44:01"]
    transport = MnT(macs)
    _run(transport)
    assert len(transport.detail_requests) == 1

    cache = detail_cache.shared(authz.CACHE)
    aged = time.time() - 25 * 3600
    with cache._lock:
        cache._entries[macs[0]]["at"] = aged
    _run(transport)
    assert len(transport.detail_requests) == 2, "24h default must re-read"

    with cache._lock:
        cache._entries[macs[0]]["at"] = aged
    _run_with({"detail_refresh_hours": 168}, macs, None)
    assert len(transport.detail_requests) == 2, (
        "a longer declared refresh must not re-read a session still active")


def test_departed_endpoints_are_dropped_so_the_cache_tracks_the_active_set():
    macs = ["00:11:22:33:44:01", "00:11:22:33:44:02"]
    transport = MnT(macs)
    _run(transport)
    assert _sample("ise3_detail_cache_entries", cache=authz.CACHE) == 2

    # Held through the grace window, so the size gauge still counts it -- it is
    # real memory -- but coverage counts only endpoints that are actually here.
    transport.macs = ["00:11:22:33:44:01"]
    _run(transport)
    assert _sample("ise3_detail_cache_entries", cache=authz.CACHE) == 2
    assert _coverage() == 1.0

    # Past the window it goes, so the cache still tracks the active set.
    cache = detail_cache.shared(authz.CACHE)
    cache.retain({"00:11:22:33:44:01"}, 0)
    assert len(cache) == 1


def test_a_reconnect_inside_the_grace_window_costs_no_request():
    # The fetch is per arrival, and a roam or a sleep/wake reads as an arrival.
    # Paying again for a decision that did not change is the cheapest waste to
    # remove, so it is removed by default.
    macs = ["00:11:22:33:44:01", "00:11:22:33:44:02"]
    transport = MnT(macs)
    _run(transport)
    assert len(transport.detail_requests) == 2

    transport.macs = ["00:11:22:33:44:01"]
    _run(transport)
    transport.macs = list(macs)
    _run(transport)
    assert len(transport.detail_requests) == 2, "a return must reuse the entry"


def test_a_zero_grace_window_refetches_a_returning_endpoint():
    # The opposite of the case above, so the option is doing the work rather
    # than something else keeping the entry alive.
    macs = ["00:11:22:33:44:01", "00:11:22:33:44:02"]
    transport = MnT(macs)
    _run_with({"detail_grace_minutes": 0}, None, None, transport=transport)
    assert len(transport.detail_requests) == 2

    transport.macs = macs[:1]
    _run_with({"detail_grace_minutes": 0}, None, None, transport=transport)
    transport.macs = list(macs)
    _run_with({"detail_grace_minutes": 0}, None, None, transport=transport)
    assert len(transport.detail_requests) == 3, "dropped at once, so refetched"


def test_a_skipped_segment_is_never_detail_fetched():
    # The NAS address is on the active list, so this is decided before a request
    # is spent. The endpoint still exists -- it just leaves the authorization
    # breakdowns, which is the trade being made.
    macs = ["00:11:22:33:44:01", "00:11:22:33:44:02"]
    transport = MnT(macs)
    transport.nas_ips = {"00:11:22:33:44:01": "10.10.0.1",
                         "00:11:22:33:44:02": "10.20.0.1"}
    _run_with({"skip_nas_ip_prefixes": "10.20."}, macs, None, transport=transport)
    assert transport.detail_requests == ["00:11:22:33:44:01"]


def test_an_endpoint_is_kept_when_only_one_of_its_sessions_is_skipped():
    macs = ["00:11:22:33:44:01"]
    transport = MnT(macs)
    transport.nas_ips = {"00:11:22:33:44:01": "10.20.0.1"}
    transport.extra_sessions = [
        {"calling_station_id": "00:11:22:33:44:01",
         "nas_ip_address": "10.10.0.1"}]
    _run_with({"skip_nas_ip_prefixes": "10.20."}, macs, None, transport=transport)
    assert transport.detail_requests == ["00:11:22:33:44:01"]


def test_the_budget_reports_what_it_left_for_the_next_cycle(monkeypatch):
    monkeypatch.setattr(authz, "WARMUP_FETCHES_PER_CYCLE", 3)
    transport = MnT([f"00:11:22:33:44:{i:02X}" for i in range(10)])
    _run(transport)
    assert _sample("ise3_detail_fetches_deferred", cache=authz.CACHE) == 7


def test_a_warm_up_pass_gives_the_lane_back_within_its_own_cadence():
    # The per-cycle count stopped bounding lane time once the request budget
    # became something the transport enforces by blocking: one pass held the
    # serialized mnt lane for the whole warm-up and starved every other mnt
    # dataset, whose freshness then dropped for reasons nothing explained.
    import time as _time

    class Slow(MnT):
        def get_mnt_xml(self, path, *, api="mnt_xml"):
            if not path.endswith("/ActiveList"):
                _time.sleep(0.02)       # a blocking budget, in miniature
            return super().get_mnt_xml(path, api=api)

    transport = Slow([f"00:11:22:33:44:{index:02X}" for index in range(200)])
    entry = PlannedDataset(
        name=authz.DATASET.name, description="", enabled=True, interval=2,
        dataset=authz.DATASET, provider=authz.DATASET.providers[0])
    detail_cache._CACHES.pop(authz.ACTIVE_LIST_CACHE, None)

    started = _time.monotonic()
    outcome = Runner(_config()).run(entry, transport)
    elapsed = _time.monotonic() - started

    assert outcome.ok
    # A 2s cadence gives the warm-up 1s of it, not the four seconds 200 blocking
    # requests would have taken.
    assert elapsed < 2.0
    assert len(transport.detail_requests) < 200
    assert outcome.deferred == 200 - len(transport.detail_requests)
    assert _sample("ise3_detail_fetches_deferred", cache=authz.CACHE) == (
        outcome.deferred)


def test_posture_refuses_rather_than_publishing_an_empty_estate():
    # posture_current only reads the cache this dataset fills. Publishing an
    # empty snapshot with dataset_up=1 reads as "nothing is non-compliant" on a
    # compliance panel, which is the one answer it must never invent.
    from ise_exporter3.datasets import posture_current

    transport = MnT(["00:11:22:33:44:01"])
    entry = PlannedDataset(
        name=posture_current.DATASET.name, description="", enabled=True,
        interval=300, dataset=posture_current.DATASET,
        provider=posture_current.DATASET.provider("mnt"))
    runner = Runner(_config())

    detail_cache._CACHES.pop(authz.ACTIVE_LIST_CACHE, None)
    outcome = runner.run(entry, transport)
    assert not outcome.ok
    assert outcome.reason == "dependency_pending"
    assert _sample("ise3_posture_endpoints", provider="mnt",
                   status="Unavailable", ops_owner="unknown") is None

    # Once its peer has filled the cache, the same collection succeeds.
    _run(transport)
    detail_cache._CACHES.pop(authz.ACTIVE_LIST_CACHE, None)
    assert runner.run(entry, transport).ok
    assert _sample("ise3_posture_endpoints", provider="mnt",
                   status="Unavailable", ops_owner="unknown") == 1



def test_posture_reports_coverage_rather_than_dropping_the_dimension():
    # A lab with no Secure Client leaves the posture fields empty. That is an
    # estate fact, not an API limit, so the provider keeps reading them and
    # coverage says 0.0 -- the signal an operator uses to tell "nothing runs
    # posture here" from "the collector lost it".
    from ise_exporter3.datasets import posture_current

    mac = "00:11:22:33:44:01"
    transport = MnT([mac], details={mac: _detail(
        mac, total_latency="20", step_latency="1=5")})
    entry = PlannedDataset(
        name=posture_current.DATASET.name, description="", enabled=True,
        interval=300, dataset=posture_current.DATASET,
        provider=posture_current.DATASET.provider("mnt"))

    _run(transport)
    detail_cache._CACHES.pop(authz.ACTIVE_LIST_CACHE, None)
    assert Runner(_config()).run(entry, transport).ok

    for field in ("posture_report", "agent_version", "operating_system",
                  "posture_status"):
        assert _sample("ise3_session_detail_field_coverage",
                       provider="mnt", field=field) == 0.0
    assert _sample("ise3_session_detail_field_coverage",
                   provider="mnt", field="step_latency") == 1.0
    assert _sample("ise3_session_detail_field_coverage", provider="mnt",
                   field="total_authentication_latency") == 1.0


def test_posture_publishes_the_dimensions_once_secure_client_reports_them():
    from ise_exporter3.datasets import posture_current

    mac = "00:11:22:33:44:01"
    detail = _detail(mac)
    detail["posture_status"] = "Compliant"
    detail["posture_report"] = "AV_Installed:Passed;Firewall:Failed"
    detail["posture_agent_version"] = "5.1.2.42"
    detail["operating_system"] = "Windows 11"
    transport = MnT([mac], details={mac: detail})
    entry = PlannedDataset(
        name=posture_current.DATASET.name, description="", enabled=True,
        interval=300, dataset=posture_current.DATASET,
        provider=posture_current.DATASET.provider("mnt"))

    _run(transport)
    detail_cache._CACHES.pop(authz.ACTIVE_LIST_CACHE, None)
    assert Runner(_config()).run(entry, transport).ok

    assert _sample("ise3_posture_agent_version_endpoints",
                   provider="mnt", agent_version="5.1.2.42") == 1
    assert _sample("ise3_posture_endpoints_by_os",
                   provider="mnt", os="Windows 11") == 1
    assert _sample("ise3_posture_policy_results", provider="mnt",
                   policy="Firewall", result="Failed") == 1


def test_one_unreachable_endpoint_does_not_fail_the_dataset():
    macs = ["00:11:22:33:44:01", "00:11:22:33:44:02"]
    transport = MnT(macs, fail={"00:11:22:33:44:02"})
    outcome = _run(transport)
    assert outcome.ok
    assert _sample("ise3_detail_fetches_total",
                   cache=authz.CACHE, result="failed") == 1
    assert _coverage() == pytest.approx(0.5)


# --- aggregation ------------------------------------------------------------

def test_policy_set_and_matched_rule_come_from_other_attr_string():
    # The ground-truth open-mode signal, and the reason this dataset earns its
    # cost: far stronger than inferring intent from profile names.
    transport = MnT(["00:11:22:33:44:01"])
    assert _run(transport).ok
    assert _sample("ise3_session_authz_rule_endpoints", provider="mnt",
                   authz_rule="Basic_Authenticated_Access",
                   ops_owner="unknown") == 1
    assert _sample("ise3_session_policy_set_endpoints", provider="mnt",
                   policy_set="Wired Open Mode", ops_owner="unknown") == 1


def test_the_policy_set_is_also_kept_per_switch():
    # "Which switches are still in open mode" is a per-switch question with no
    # useful coarser form, so this one dimension keeps the NAD.
    transport = MnT(["00:11:22:33:44:01"])
    assert _run(transport).ok
    assert _sample("ise3_session_policy_set_endpoints_by_nad", provider="mnt",
                   policy_set="Wired Open Mode", nad="sw-01") == 1


# --- the per-switch breakdown is bounded, and says so -----------------------

def _many_nads(count, policy_sets=("Wired Open Mode", "Wired Closed Mode")):
    """One MAC per switch, spread across policy sets, busiest switch last."""
    macs, details = [], {}
    for index in range(count):
        mac = f"00:11:22:33:{index // 256:02X}:{index % 256:02X}"
        macs.append(mac)
        details[mac] = _detail(
            mac, nad=f"sw-{index:04d}", nas_ip=f"10.1.{index // 256}.{index % 256}",
            policy_set=policy_sets[index % len(policy_sets)])
    return macs, details


def _nad_coverage(breakdown="policy_set_nad"):
    """Returned / total / truncated for the bounded per-switch breakdown.

    Deliberately not named _coverage: that one is the detail cache's warm-up
    coverage, which is a different question with a different answer.
    """
    return (
        _sample("ise3_topk_groups_returned", dataset="session_authorization",
                breakdown=breakdown),
        _sample("ise3_topk_groups_total", dataset="session_authorization",
                breakdown=breakdown),
        _sample("ise3_topk_truncated", dataset="session_authorization",
                breakdown=breakdown),
    )


def _run_with(options, macs, details, transport=None):
    document = {"targets": {"pan": {"host": "pan1", "user": "ro"},
                            "mnt": {"host": "mnt1"}},
                "datasets": {"session_authorization": {"options": options}}}
    config = Config.from_document(
        document, path="test.toml", environ={"ISE_PASS": "secret"})
    detail_cache._CACHES.pop(authz.ACTIVE_LIST_CACHE, None)
    outcome = Runner(config).run(
        _entry(), transport if transport is not None else MnT(macs, details))
    assert outcome.ok, f"{outcome.reason}: {outcome.detail}"
    return outcome


def _nad_series():
    return sum(
        1 for metric in REGISTRY.collect()
        if metric.name == "ise3_session_policy_set_endpoints_by_nad"
        for _sample_row in metric.samples)


def test_only_the_declared_number_of_switches_gets_its_own_series():
    # 20,000 series for one metric family -- 41% of the hard ceiling before any
    # other dataset publishes -- was this cross product unbounded.
    macs, details = _many_nads(40)
    _run_with({"top_nads": 5}, macs, details)
    # Five switches for each of the two policy sets.
    assert _nad_series() == 10


def test_the_switches_kept_are_the_ones_with_the_most_endpoints():
    macs, details = _many_nads(10, policy_sets=("Wired Open Mode",))
    # Give sw-0003 a second session so it outranks every other switch.
    extra = "00:11:22:33:AA:BB"
    macs.append(extra)
    details[extra] = _detail(extra, nad="sw-0003", nas_ip="10.9.9.9",
                             policy_set="Wired Open Mode")
    _run_with({"top_nads": 1}, macs, details)
    assert _sample("ise3_session_policy_set_endpoints_by_nad", provider="mnt",
                   policy_set="Wired Open Mode", nad="sw-0003") == 2
    assert _nad_series() == 1


def test_each_policy_set_gets_its_own_ranking_rather_than_one_global_list():
    # A global top-K would let the busiest policy set crowd every other one out
    # of the answer, and "which switches are in open mode" is asked per set.
    macs, details = _many_nads(40)
    _run_with({"top_nads": 3}, macs, details)
    for policy_set in ("Wired Open Mode", "Wired Closed Mode"):
        published = sum(
            1 for metric in REGISTRY.collect()
            if metric.name == "ise3_session_policy_set_endpoints_by_nad"
            for row in metric.samples
            if row.labels.get("policy_set") == policy_set)
        assert published == 3


def test_the_bounded_breakdown_reports_what_it_is_showing_against_what_exists():
    macs, details = _many_nads(40)
    _run_with({"top_nads": 5}, macs, details)
    assert _nad_coverage() == (10, 40, 1)


def test_publishing_every_switch_reports_itself_as_untruncated():
    macs, details = _many_nads(40)
    _run_with({"top_nads": 0}, macs, details)
    assert _nad_coverage() == (40, 40, 0)
    assert _nad_series() == 40


def test_the_rollup_stays_complete_when_the_per_switch_view_is_bounded():
    # The point of bounding publication rather than measurement: every session
    # is still counted, so the per-owner totals are the whole active set.
    macs, details = _many_nads(40)
    _run_with({"top_nads": 2}, macs, details)
    for policy_set in ("Wired Open Mode", "Wired Closed Mode"):
        assert _sample("ise3_session_policy_set_endpoints", provider="mnt",
                       policy_set=policy_set, ops_owner="unknown") == 20


def test_turning_the_per_switch_view_off_still_publishes_a_coverage_signal():
    # An absent coverage series and a complete one look identical on a
    # dashboard, so "off" has to say zero of forty rather than nothing.
    macs, details = _many_nads(40)
    _run_with({"policy_set_by_nad": False, "top_nads": 5}, macs, details)
    assert _nad_series() == 0
    assert _nad_coverage() == (0, 40, 1)


def test_the_dangerous_settings_warn_at_start():
    config = Config.from_document(
        {"targets": {"pan": {"host": "pan1", "user": "ro"},
                     "mnt": {"host": "mnt1"}},
         "datasets": {"session_authorization": {"options": {"top_nads": 0}}}},
        path="test.toml", environ={"ISE_PASS": "secret"})
    assert any("top_nads = 0" in warning and "20,000 series" in warning
               for warning in config.warnings), config.warnings
    assert not [warning for warning in _config().warnings
                if "session_authorization" in warning]


def test_dimensions_are_keyed_on_ops_owner_not_on_every_nad():
    # Keying each dimension on the NAD is a cross product: 5,000 switches times
    # ten dimension values is 50,000 series to answer questions asked per owner.
    from ise_exporter3.datasets import session_authorization

    for gauge in session_authorization._METRICS:
        labels = set(gauge._labelnames)
        if gauge is session_authorization.policy_set_by_nad:
            continue
        assert "nad" not in labels, f"{gauge._name} still keys on the NAD"
        assert "location" not in labels, (
            f"{gauge._name} duplicates location, which network_devices owns")


def test_one_device_with_several_sessions_counts_once():
    # Every series is a distinct-MAC count, never one series per endpoint.
    mac = "00:11:22:33:44:01"
    transport = MnT([mac, mac, mac])
    assert _run(transport).ok
    assert _sample("ise3_session_status_endpoints", provider="mnt", status="passed",
                   ops_owner="unknown") == 1


def test_failed_authentications_publish_their_reason_code():
    transport = MnT(["00:11:22:33:44:09"], details={
        "00:11:22:33:44:09": _detail(
            "00:11:22:33:44:09", passed=False,
            failure_reason="11512 Extracted EAP-Response containing...")})
    assert _run(transport).ok
    assert _sample("ise3_session_failure_reason_endpoints", provider="mnt",
                   reason_code="11512", ops_owner="unknown") == 1


def test_failed_authentications_retain_authorization_context():
    transport = MnT(["00:11:22:33:44:09"], details={
        "00:11:22:33:44:09": _detail(
            "00:11:22:33:44:09",
            passed=False,
            profile="Quarantine",
            rule="Default_Deny",
            policy_set="Wired Closed Mode",
        )})
    assert _run(transport).ok
    assert _sample(
        "ise3_session_failed_authz_profile_endpoints",
        provider="mnt",
        authz_profile="Quarantine",
        ops_owner="unknown",
    ) == 1
    assert _sample(
        "ise3_session_failed_authz_rule_endpoints",
        provider="mnt",
        authz_rule="Default_Deny",
        ops_owner="unknown",
    ) == 1
    assert _sample(
        "ise3_session_failed_policy_set_endpoints",
        provider="mnt",
        policy_set="Wired Closed Mode",
        ops_owner="unknown",
    ) == 1


def test_authentication_and_step_latency_are_read_from_other_attr_string():
    # The appliance has no step_latency or total_authen_latency element: both
    # live in other_attr_string, and reading them as top-level fields published
    # no series at all against real ISE.
    details = {
        "00:11:22:33:44:01": _detail(
            "00:11:22:33:44:01",
            total_latency="20",
            step_latency="1=5;2=10",
        ),
        "00:11:22:33:44:02": _detail(
            "00:11:22:33:44:02",
            total_latency="40",
            step_latency="1=15;2=30",
        ),
    }
    assert _run(MnT(list(details), details=details)).ok
    assert _sample(
        "ise3_session_authentication_latency_seconds",
        provider="mnt",
        statistic="mean",
    ) == pytest.approx(0.03)
    assert _sample(
        "ise3_session_authentication_latency_samples", provider="mnt") == 2
    # Labelled by StepLatency position. ISE reports one more execution step than
    # it reports step latencies, so no position resolves to a message code.
    assert _sample(
        "ise3_session_authentication_step_latency_seconds",
        provider="mnt",
        step="1",
        statistic="max",
    ) == pytest.approx(0.015)
    assert _sample(
        "ise3_session_authentication_step_latency_samples",
        provider="mnt",
        step="2",
    ) == 2


def test_total_latency_falls_back_to_the_response_time_element():
    # response_time carried exactly the TotalAuthenLatency value on every
    # sampled session, so it answers when other_attr_string does not.
    mac = "00:11:22:33:44:03"
    details = {mac: _detail(mac, response_time="50")}
    assert _run(MnT([mac], details=details)).ok
    assert _sample(
        "ise3_session_authentication_latency_seconds",
        provider="mnt",
        statistic="max",
    ) == pytest.approx(0.05)


def test_a_departed_endpoint_is_not_counted_as_a_detail_failure():
    # ISE answers a MAC with no current session with HTTP 500 (cpm-code 34110),
    # not an empty document, and that is the ordinary churn case: every session
    # that ends between the ActiveList read and its detail fetch arrives here.
    from ise_exporter3.transports import TransportError

    class Departed(MnT):
        def get_mnt_xml(self, path, *, api="mnt_xml"):
            if not path.endswith("/ActiveList"):
                self.detail_requests.append(path.rsplit("/", 1)[-1])
                raise TransportError(
                    "http_error", "ISE returned HTTP 500 for mnt_session_detail",
                    status=500, code=authz.MNT_NO_SESSION_CODE)
            return super().get_mnt_xml(path, api=api)

    before = _sample("ise3_detail_fetches_total",
                     cache=authz.CACHE, result="failed") or 0
    assert _run(Departed(["00:11:22:33:44:0B"])).ok
    assert _sample("ise3_detail_fetches_total",
                   cache=authz.CACHE, result="gone") == 1
    assert _sample("ise3_detail_fetches_total",
                   cache=authz.CACHE, result="failed") == before


def test_a_failure_without_a_failure_reason_falls_back_to_the_message_code():
    # No failure_reason element exists on a real session document; message_code
    # is the ISE result code it does carry.
    mac = "00:11:22:33:44:0C"
    details = {mac: _detail(mac, passed=False, message_code="11007")}
    assert _run(MnT([mac], details=details)).ok
    assert _sample("ise3_session_failure_reason_endpoints", provider="mnt",
                   reason_code="11007", ops_owner="unknown") == 1


def test_an_accounting_only_record_is_not_counted_as_an_authorization():
    # Stop records carry no verdict; counting them would dilute every ratio.
    transport = MnT(["00:11:22:33:44:0A"], details={
        "00:11:22:33:44:0A": {"calling_station_id": "00:11:22:33:44:0A",
                              "network_device_name": "sw-01",
                              "acct_status_type": "Stop"}})
    assert _run(transport).ok
    assert _sample("ise3_session_status_endpoints", provider="mnt",
                   status="passed", ops_owner="unknown") is None
    # It is still cached, so it is not re-fetched every cycle.
    assert _coverage() == 1.0


def test_an_empty_active_list_publishes_full_coverage_not_a_failure():
    assert _run(MnT([])).ok
    assert _coverage() == 1.0


# --- helpers ----------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("00-11-22-33-44-55", "00:11:22:33:44:55"),
    ("00:11:22:33:44:55", "00:11:22:33:44:55"),
    (" 00:11:22:33:44:aa ", "00:11:22:33:44:AA"),
    # Cisco's dotted three-group form: not what the detail URL accepts, so a key
    # in that shape was re-requested every cycle for the life of the session.
    ("aabb.ccdd.eeff", "AA:BB:CC:DD:EE:FF"),
    ("", ""),
])
def test_mac_normalisation_matches_what_the_detail_url_accepts(raw, expected):
    assert authz.normalize_mac(raw) == expected


def test_both_active_session_providers_derive_the_same_endpoint_identity():
    # Both publish ise3_active_session_endpoints, so a failover that changed the
    # identity rule would move the count without the fleet moving.
    from ise_exporter3.datasets import active_sessions

    dotted = [{"calling_station_id": "aabb.ccdd.eeff",
               "nas_ip_address": "10.0.0.1",
               "network_device_name": "sw-01", "server": "psn-1"},
              {"calling_station_id": "AA:BB:CC:DD:EE:FF",
               "nas_ip_address": "10.0.0.1",
               "network_device_name": "sw-01", "server": "psn-1"}]

    class Ctx:
        interval = 300

        def __init__(self, sessions, pxgrid=False):
            self.samples = []
            self.transport = SimpleNamespace(
                get_mnt_xml=lambda path, *, api="": {
                    "total": len(sessions), "sessions": sessions},
                get_sessions=lambda *, max_age=0: sessions)

        def set(self, family, sample_value, /, **labels):
            self.samples.append((family._name, sample_value))

    def endpoints(ctx):
        return [value for name, value in ctx.samples
                if name == "ise3_active_session_endpoints"][0]

    mnt, pxgrid = Ctx(dotted), Ctx(dotted)
    active_sessions.fetch_mnt(mnt)
    active_sessions.fetch_pxgrid(pxgrid)
    # One device, spelled two ways.
    assert endpoints(mnt) == endpoints(pxgrid) == 1


def test_the_declared_cost_matches_the_implemented_warmup_budget():
    # If the budget changes and the cost declaration does not, the plan's
    # time-to-coverage becomes fiction.
    cost = authz.DATASET.providers[0].cost
    assert cost.warmup_requests == authz.WARMUP_FETCHES_PER_CYCLE
    assert cost.cycles_to_warm(Scale(sessions=20_000)) == 10
