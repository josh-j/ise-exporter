"""Dashboards are a contract against the metric registry.

v2 proved the value of this: dashboards are normally the least-verified artifact
in a monitoring stack, and a panel querying a metric that no longer exists shows
an empty graph rather than an error. These tests assert every metric and every
label a panel references is one this exporter actually publishes.

The dashboards are generated (tools/build_dashboards3.py), so a second test
checks the committed JSON still matches a fresh build. That one skips where the
Grafana SDK is not installed, because the SDK is a build-time dependency and
should not be needed to run the suite.
"""
import json
import re
from pathlib import Path

import pytest
from prometheus_client import REGISTRY

# Importing the package registers every metric family the exporter can publish.
import ise_exporter3.datasets  # noqa: F401
import ise_exporter3.telemetry  # noqa: F401


DASHBOARDS = Path(__file__).parents[1] / "dashboards3"
METRIC_NAME = re.compile(r"\b(ise3_[a-z0-9_]+)\b")
LABEL_IN_LEGEND = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}")
LABEL_IN_SELECTOR = re.compile(r"([a-z_][a-z0-9_]*)\s*=\s*['\"]")
EXTERNAL_LABELS = {"instance", "job"}


def _dashboards():
    files = sorted(DASHBOARDS.glob("*.json"))
    assert files, f"no dashboards were generated into {DASHBOARDS}"
    return [(path.name, json.loads(path.read_text())) for path in files]


def _dashboard(name):
    return json.loads((DASHBOARDS / name).read_text())


def _panels(dashboard):
    for panel in dashboard.get("panels", []):
        yield panel
        yield from panel.get("panels", [])


def _targets(dashboard):
    for panel in _panels(dashboard):
        for target in panel.get("targets", []):
            yield panel, target


def _exported():
    """Every metric name and its label names, as this build publishes them."""
    exported = {}
    for metric in REGISTRY.collect():
        if not metric.name.startswith("ise3_"):
            continue
        labels = set()
        for sample in metric.samples:
            labels.update(sample.labels)
        for suffix in ("", "_total", "_count", "_sum", "_bucket", "_created"):
            exported.setdefault(metric.name + suffix, set()).update(labels)
        # A family with no samples yet still declares its label names.
        exported[metric.name] = exported.get(metric.name, set())
    return exported


EXPORTED = _exported()


def _declared_labels(name):
    """Label names declared by a family, whether or not it has samples yet."""
    from prometheus_client import REGISTRY as registry

    collectors = list(registry._collector_to_names)
    for collector in collectors:
        if getattr(collector, "_name", None) == name:
            return set(getattr(collector, "_labelnames", ()))
    base_name = name
    for suffix in ("_total", "_count", "_sum", "_bucket", "_created"):
        if base_name.endswith(suffix):
            base_name = base_name.removesuffix(suffix)
            break
    for collector in collectors:
        family_name = getattr(collector, "_name", None)
        if family_name and base_name == family_name:
            return set(getattr(collector, "_labelnames", ()))
    return set()


@pytest.mark.parametrize("filename,dashboard", _dashboards())
def test_every_metric_a_panel_queries_is_one_this_exporter_publishes(
        filename, dashboard):
    unknown = []
    for panel, target in _targets(dashboard):
        for name in METRIC_NAME.findall(target.get("expr", "")):
            if name not in EXPORTED and name.removesuffix("_total") not in EXPORTED:
                unknown.append(f"{panel.get('title')}: {name}")
    assert not unknown, f"{filename} queries metrics that do not exist: {unknown}"


@pytest.mark.parametrize("filename,dashboard", _dashboards())
def test_every_label_a_panel_uses_is_one_that_metric_carries(filename, dashboard):
    problems = []
    for panel, target in _targets(dashboard):
        expr = target.get("expr", "")
        names = METRIC_NAME.findall(expr)
        if not names:
            continue
        available = set()
        for name in names:
            available |= _declared_labels(name) | EXPORTED.get(name, set())
        used = set(LABEL_IN_LEGEND.findall(target.get("legendFormat", "")))
        used |= set(LABEL_IN_SELECTOR.findall(expr))
        for label in used - available - EXTERNAL_LABELS:
            problems.append(f"{panel.get('title')}: {label} not on {names}")
    assert not problems, f"{filename} uses labels that do not exist: {problems}"


@pytest.mark.parametrize("filename,dashboard", _dashboards())
def test_every_dashboard_is_file_provisionable(filename, dashboard):
    # A hardcoded datasource uid breaks file provisioning on any other Grafana.
    variables = {item["name"] for item in dashboard["templating"]["list"]}
    assert "prometheus" in variables, f"{filename} has no datasource variable"
    for panel in _panels(dashboard):
        if panel.get("type") == "row":
            continue
        uid = (panel.get("datasource") or {}).get("uid")
        assert uid == "${prometheus}", f"{filename}: {panel.get('title')} uses {uid}"


@pytest.mark.parametrize("filename,dashboard", _dashboards())
def test_every_panel_explains_itself(filename, dashboard):
    # A panel an operator cannot interpret at 3am is not monitoring. v2's
    # ISSUES.md is largely a list of panels whose meaning was unclear.
    for panel in _panels(dashboard):
        if panel.get("type") == "row":
            continue
        description = (panel.get("description") or "").strip()
        assert len(description) > 40, (
            f"{filename}: {panel.get('title')} has no useful description")


@pytest.mark.parametrize("filename,dashboard", _dashboards())
def test_every_dashboard_has_a_stable_identity(filename, dashboard):
    assert dashboard.get("uid"), f"{filename} has no uid"
    assert dashboard["uid"] == filename.removesuffix(".json")
    assert "ise-exporter3" in dashboard.get("tags", [])


def test_the_source_dashboard_answers_which_provider_is_live():
    # The whole argument of v3 is that a source change is visible. If these
    # queries disappear, that claim stops being true on the operator's screen.
    body = (DASHBOARDS / "ise3-sources.json").read_text()
    assert "ise3_dataset_provider_active" in body
    assert "ise3_dataset_provider_degraded" in body
    assert "ise3_dataset_provider_reason_info" in body


def test_the_load_dashboard_compares_planned_against_measured():
    # Cost declarations are the one thing in this design that can quietly lie,
    # so the panel that catches them is not optional.
    body = (DASHBOARDS / "ise3-load.json").read_text()
    assert "ise3_load_planned_requests_per_hour" in body
    assert "ise3_load_measured_requests_total" in body
    assert "ise3_load_measured_db_seconds_total" in body
    assert "ise3_load_budget_utilisation" in body


def test_every_v2_operator_workflow_has_a_generated_v3_dashboard():
    import sys
    sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))
    import build_dashboards3

    expected = {
        "ise-overview.json",
        "ise-access-troubleshooting.json",
        "ise-endpoints-devices.json",
        "ise-exporter-health.json",
        "ise-pan-mnt-troubleshooting.json",
        "ise-psn-troubleshooting.json",
        "ise-secureclient.json",
        "ise-tacacs.json",
    }
    assert set(build_dashboards3.V2_WORKFLOW_PARITY) == expected
    generated = {path.stem for path in DASHBOARDS.glob("*.json")}
    assert set(build_dashboards3.V2_WORKFLOW_PARITY.values()) <= generated


WORKFLOW_CONTRACTS = {
    "ise3-overview.json": {
        "ise3_deployment_node_state",
        "ise3_active_sessions_total",
        "ise3_endpoints_total",
        "ise3_network_devices_total",
        "ise3_certificate_expiry_days",
        "ise3_backup_age_hours",
        "ise3_patch_level",
        "ise3_license_consumption",
    },
    "ise3-access.json": {
        "ise3_radius_authentications",
        "ise3_radius_authentication_latency_seconds",
        "ise3_radius_accounting_events",
        "ise3_radius_accounting_session_duration_seconds",
        "ise3_radius_distinct_endpoints_total",
        "ise3_radius_failures_by_nad_method",
        "ise3_radius_failure_summary",
        "ise3_radius_errors_total",
        "ise3_active_sessions_total",
        "ise3_session_failure_reason_endpoints",
        "ise3_session_authz_profile_endpoints",
        "ise3_session_authz_rule_endpoints",
        "ise3_session_failed_authz_profile_endpoints",
        "ise3_session_failed_authz_rule_endpoints",
        "ise3_session_failed_policy_set_endpoints",
        "ise3_session_policy_set_endpoints_by_nad",
        "ise3_network_device_assignment",
    },
    "ise3-endpoints.json": {
        "ise3_endpoints_by_profile",
        "ise3_endpoints_by_identity_group",
        "ise3_endpoint_inventory_field_coverage",
        "ise3_endpoint_profile_events",
        "ise3_endpoint_model",
        "ise3_endpoint_mdm_compliant",
        "ise3_network_device_assignment",
        "ise3_detail_cache_coverage",
        "ise3_nad_authentications",
        "ise3_nad_last_authentication_age_seconds",
    },
    "ise3-health.json": {
        "ise3_dataset_provider_active",
        "ise3_dataset_last_failure_detail_info",
        "ise3_source_latest_row_age_seconds",
        "ise3_dataconnect_schema_view_available",
        "ise3_dataconnect_schema_column_available",
        "ise3_dataconnect_query_last_duration_seconds",
        "ise3_api_requests_total",
        "ise3_budget_throttled_total",
        "ise3_topk_groups_total",
        "ise3_detail_cache_coverage",
        "ise3_posture_eligible_without_recent_assessment_total",
        "ise3_exporter_build_info",
    },
    "ise3-pan-mnt.json": {
        "ise3_deployment_node_state",
        "ise3_deployment_node_service_enabled",
        "ise3_backup_age_hours",
        "ise3_certificate_expiry_days",
        "ise3_detail_cache_coverage",
        "ise3_posture_endpoints",
        "ise3_node_cpu_utilization_percent",
        "ise3_session_authentication_latency_seconds",
        "ise3_session_authentication_step_latency_seconds",
        "ise3_psn_diagnostic_events",
        "ise3_dataset_last_failure_detail_info",
    },
    "ise3-psn.json": {
        "ise3_active_sessions_by_psn",
        "ise3_radius_authentications_by_psn",
        "ise3_radius_accounting_events",
        "ise3_radius_errors_by_psn",
        "ise3_psn_radius_requests_per_hour",
        "ise3_psn_average_latency_seconds",
        "ise3_psn_diagnostic_events",
        "ise3_psn_diagnostic_events_total",
        "ise3_dataconnect_schema_column_available",
        "ise3_node_disk_utilization_percent",
        "ise3_source_latest_row_age_seconds",
    },
    "ise3-secureclient.json": {
        "ise3_posture_endpoints",
        "ise3_posture_endpoints_by_psn",
        "ise3_posture_policy_results",
        "ise3_posture_agent_version_endpoints",
        "ise3_session_detail_field_coverage",
        "ise3_detail_cache_coverage",
        "ise3_posture_assessments",
        "ise3_posture_assessments_by_psn",
        "ise3_posture_assessments_by_policy",
        "ise3_posture_failed_conditions",
        "ise3_posture_eligible_endpoints_total",
        "ise3_posture_eligible_recently_assessed_total",
        "ise3_posture_eligible_without_recent_assessment_total",
    },
    "ise3-tacacs.json": {
        "ise3_tacacs_internal_account_enabled",
        "ise3_tacacs_internal_account_hygiene_risk",
        "ise3_tacacs_authentications",
        "ise3_tacacs_authorizations",
        "ise3_tacacs_authorization_details",
        "ise3_tacacs_account_last_seen_timestamp",
        "ise3_tacacs_commands",
        "ise3_tacacs_policy_objects",
        "ise3_tacacs_policy_rule_count",
        "ise3_tacacs_policy_rules_total",
        "ise3_topk_groups_total",
        "ise3_nad_directory_entries",
    },
}


@pytest.mark.parametrize("filename,required", WORKFLOW_CONTRACTS.items())
def test_operator_workflow_retains_its_capability_domains(filename, required):
    dashboard = _dashboard(filename)
    body = json.dumps(dashboard)
    missing = sorted(metric for metric in required if metric not in body)
    assert not missing, f"{filename} lost capability metrics: {missing}"
    panels = [panel for panel in dashboard["panels"] if panel.get("type") != "row"]
    assert len(panels) >= 12, f"{filename} is no longer a full operator workflow"


@pytest.mark.parametrize("filename", WORKFLOW_CONTRACTS)
def test_operator_workflows_are_deployment_aware(filename):
    dashboard = _dashboard(filename)
    variables = {item["name"] for item in dashboard["templating"]["list"]}
    assert "deployment" in variables
    expressions = " ".join(
        target.get("expr", "")
        for panel, target in _targets(dashboard)
    )
    assert 'instance=~"$deployment"' in expressions


def test_the_committed_dashboards_match_a_fresh_build(tmp_path):
    sdk = pytest.importorskip(
        "grafana_foundation_sdk",
        reason="the Grafana SDK is a build-time dependency; "
               "install with pip install grafana-foundation-sdk")
    assert sdk is not None
    import sys
    sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))
    import build_dashboards3

    for path in build_dashboards3.build(tmp_path):
        committed = (DASHBOARDS / path.name).read_text()
        assert path.read_text() == committed, (
            f"{path.name} is stale; regenerate with "
            "python tools/build_dashboards3.py --out dashboards3")
