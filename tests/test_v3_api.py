"""The local operator API.

This is the surface that replaces v2's Python CLI, and the reason it lives in
the exporter process: an operator asking "which source is live" cannot bypass
the pacing gate or the authentication guard, because there is no second process
to bypass them from.
"""
import json
import urllib.error
import urllib.request

import pytest

from ise_exporter3.api import OperatorApi
from ise_exporter3.config import Config
from ise_exporter3.plan import build_plan
from ise_exporter3.scheduler import PROVIDER_FAILOVER_THRESHOLD, Scheduler
from ise_exporter3.server import HttpServer
from ise_exporter3.snapshots import LockedCollectorRegistry
from ise_exporter3.transports import TransportError
from tests.test_v3_scheduler import Clock, ScriptedTransport


def _config():
    return Config.from_document(
        {"targets": {"pan": {"host": "pan1", "user": "ro"}}},
        path="test.toml", environ={"ISE_PASS": "secret"})


def _api(transport=None, collect=True):
    config = _config()
    plan = build_plan(config)
    scheduler = Scheduler(config, plan, {"pan": transport or ScriptedTransport()},
                          asynchronous=False, clock=Clock())
    scheduler.bootstrap()
    if collect:
        scheduler.tick()
    return OperatorApi(config, plan, scheduler), scheduler


@pytest.fixture
def served():
    api, _scheduler = _api()
    server = HttpServer("127.0.0.1", 0, LockedCollectorRegistry(), routes=api.routes())
    server.start()
    try:
        yield f"http://127.0.0.1:{server.address[1]}"
    finally:
        server.stop(timeout=5)


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def test_health_answers_is_it_working_and_is_it_within_budget():
    api, _scheduler = _api()
    health = api.health()
    assert health["datasets_enabled"] > 0
    assert health["datasets_collecting"] == 8
    assert health["fits_budget"] in (True, False)
    assert isinstance(health["datasets_unresolved"], list)


def test_datasets_report_the_live_source_and_why_one_is_failing():
    class Selective(ScriptedTransport):
        def _answer(self, path):
            self.calls.append(path)
            if path == "/patch":
                raise TransportError("http_error", "ISE returned HTTP 503")
            return self.responses[path]

    api, _scheduler = _api(Selective())
    rows = {row["dataset"]: row for row in api.datasets()}

    assert rows["deployment"]["provider"] == "openapi"
    assert rows["deployment"]["consecutive_failures"] == 0
    assert rows["deployment"]["last_success_age_seconds"] is not None

    assert rows["patches"]["failure_reason"] == "http_error"
    assert "503" in rows["patches"]["failure_detail"]
    assert rows["patches"]["consecutive_failures"] == 1


def test_a_dataset_with_no_usable_source_is_reported_as_unscheduled():
    api, _scheduler = _api()
    rows = {row["dataset"]: row for row in api.datasets()}
    # No Oracle target is configured in this fixture.
    assert rows["radius_reporting"]["scheduled"] is False
    assert "radius_reporting" in api.health()["datasets_unresolved"] or (
        rows["radius_reporting"]["scheduled"] is False)


def test_providers_expose_what_each_source_can_actually_supply():
    # A fallback often answers a narrower question than the preferred source.
    # That difference is the thing an operator most needs to see.
    api, _scheduler = _api()
    rows = [row for row in api.providers()
            if row["dataset"] == "endpoint_inventory"]
    dataconnect = next(row for row in rows if row["provider"] == "dataconnect")
    ers = next(row for row in rows if row["provider"] == "ers")
    assert "identity_group" in dataconnect["supplies"]
    assert "identity_group" not in ers["supplies"]
    assert "ERS caps page size" in ers["notes"]


def test_targets_report_planned_load_against_the_declared_budget():
    api, _scheduler = _api()
    targets = {row["target"]: row for row in api.targets()}
    assert targets["pan"]["requests_per_hour"] > 0
    assert "budget_requests_per_hour" in targets["pan"]
    assert "over_budget" in targets["pan"]


def test_a_degraded_dataset_is_visible_through_the_api():
    from tests.test_v3_failover import _setup, _run_until_next

    scheduler, sources, clock = _setup()
    sources["pxgrid"].healthy = False
    for _ in range(PROVIDER_FAILOVER_THRESHOLD):
        _run_until_next(scheduler, clock)

    api = OperatorApi(scheduler.config, scheduler.plan, scheduler)
    assert api.health()["datasets_degraded"] == ["sessions"]
    row = next(row for row in api.datasets() if row["dataset"] == "sessions")
    assert row["degraded"] is True
    assert row["provider"] == "mnt"


def test_every_route_is_reachable_and_returns_json(served):
    for path in ("/api/v1", "/api/v1/health", "/api/v1/datasets",
                 "/api/v1/providers", "/api/v1/targets", "/api/v1/plan"):
        payload = _get(f"{served}{path}")
        assert payload is not None


def test_the_plain_text_plan_is_the_same_report_the_command_prints(served):
    with urllib.request.urlopen(f"{served}/api/v1/plan.txt", timeout=5) as response:
        body = response.read().decode("utf-8")
    assert "DATASET" in body and "TARGET" in body
    assert "budget" in body.lower()


def test_metrics_are_still_served_beside_the_api(served):
    with urllib.request.urlopen(f"{served}/metrics", timeout=5) as response:
        assert "ise3_dataset_up" in response.read().decode("utf-8")


def test_an_unknown_route_is_a_404_not_a_stack_trace(served):
    with pytest.raises(urllib.error.HTTPError) as raised:
        _get(f"{served}/api/v1/nonsense")
    assert raised.value.code == 404


def test_the_api_serves_state_and_never_reaches_ise():
    # Every route must answer from what the exporter already computed. If one
    # of them queried ISE, an operator refreshing a page would be load.
    transport = ScriptedTransport()
    api, _scheduler = _api(transport)
    before = len(transport.calls)
    for route in api.routes().values():
        route()
    assert len(transport.calls) == before
