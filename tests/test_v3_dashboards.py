"""Dashboards are a contract, against the metric registry and against a design.

Two kinds of assertion live here.

The first kind is the one v2 proved the value of: dashboards are normally the
least-verified artifact in a monitoring stack, and a panel querying a metric
that no longer exists shows an empty graph rather than an error. Those tests
assert every metric and every label a panel references is one this exporter
actually publishes.

The second kind is newer. DASHBOARD_DESIGN_PRINCIPLES.md states how this set is
organised -- tiers by audience and time horizon, symptoms above causes, a
breakdown is a table rather than a capped bar gauge, nothing renders ISE data
without saying whether that data is current. Every one of those is a property
that erodes silently under maintenance, so each is asserted here rather than
left to review.

The dashboards are generated (tools/build_dashboards3.py), so a final test
checks the committed JSON still matches a fresh build. That one skips where the
Grafana SDK is not installed, because the SDK is a build-time dependency and
should not be needed to run the suite.
"""
import json
import re
import sys
from pathlib import Path

import pytest
from prometheus_client import REGISTRY

# Importing the package registers every metric family the exporter can publish.
import ise_exporter3.datasets  # noqa: F401
import ise_exporter3.telemetry  # noqa: F401


DASHBOARDS = Path(__file__).parents[1] / "dashboards3"
TOOLS = Path(__file__).parents[1] / "tools"
METRIC_NAME = re.compile(r"\b(ise3_[a-z0-9_]+)\b")
LABEL_IN_LEGEND = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}")
LABEL_IN_SELECTOR = re.compile(r"([a-z_][a-z0-9_]*)\s*=\s*['\"]")
DASHBOARD_LINK = re.compile(r"/d/([a-z0-9-]+)")
EXTERNAL_LABELS = {"instance", "job"}


def _builder():
    sys.path.insert(0, str(TOOLS))
    import build_dashboards3

    return build_dashboards3


def _dashboards():
    files = sorted(DASHBOARDS.glob("*.json"))
    assert files, f"no dashboards were generated into {DASHBOARDS}"
    return [(path.name, json.loads(path.read_text())) for path in files]


def _dashboard(name):
    return json.loads((DASHBOARDS / name).read_text())


def _panels(dashboard):
    """Every panel, whether laid out at the top level or nested in a row."""
    for panel in dashboard.get("panels", []):
        yield panel
        yield from panel.get("panels", [])


def _visible_panels(dashboard):
    """Panels an operator sees on load: top level, excluding row headers.

    A collapsed row's panels are nested inside the row object and are not on
    screen until it is expanded, so they do not count against the ceiling.
    """
    return [
        panel for panel in dashboard.get("panels", [])
        if panel.get("type") != "row"
    ]


def _targets(dashboard):
    for panel in _panels(dashboard):
        for target in panel.get("targets", []):
            yield panel, target
    # Annotation queries reference metrics and labels exactly the way panel
    # targets do, and a broken one fails the same way: silently, as a graph
    # with no change markers. Shaping them as pseudo-targets runs them through
    # every contract below; the title/text formats stand in for the legend so
    # the label check covers them too.
    for annotation in dashboard.get("annotations", {}).get("list", []):
        if "expr" not in annotation:
            continue
        formats = " ".join(
            (annotation.get("titleFormat", ""), annotation.get("textFormat", ""))
        )
        yield (
            {"title": f"annotation: {annotation.get('name')}"},
            {"expr": annotation["expr"], "legendFormat": formats},
        )


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


# ---------------------------------------------------------------------------
# The metric contract
# ---------------------------------------------------------------------------

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
def test_every_dashboard_has_a_stable_identity(filename, dashboard):
    assert dashboard.get("uid"), f"{filename} has no uid"
    assert dashboard["uid"] == filename.removesuffix(".json")
    assert "ise-exporter3" in dashboard.get("tags", [])


@pytest.mark.parametrize("filename,dashboard", _dashboards())
def test_every_dashboard_carries_the_change_annotations(filename, dashboard):
    # The first question about a step in any line here is whether ISE changed
    # or the exporter's view of ISE changed. Every dashboard has to be able to
    # answer it without leaving the page.
    names = {
        item.get("name")
        for item in dashboard.get("annotations", {}).get("list", [])
    }
    for required in ("Provider changed", "Node state changed",
                     "Exporter build changed"):
        assert required in names, f"{filename} lost the {required!r} annotation"


# ---------------------------------------------------------------------------
# The design contract -- DASHBOARD_DESIGN_PRINCIPLES.md, asserted
# ---------------------------------------------------------------------------

# Principle 2: separate dashboards by audience and time horizon. The tiers and
# their defaults live in the builder; this is the assertion that the generated
# JSON actually carries them.
TIER_DEFAULTS = {
    "triage": ("now-3h", "1m"),
    "diagnostic": ("now-6h", "5m"),
    "exporter": ("now-6h", "1m"),
    "capacity": ("now-30d", "30m"),
}

# ise3-load is tier 3 but reads on a slower horizon than the pipeline: cost is
# a daily question, not a per-minute one.
TIER_EXCEPTIONS = {"ise3-load": ("now-24h", "5m")}


def test_the_set_is_organised_into_tiers():
    build = _builder()
    tiered = {uid for uids in build.TIERS.values() for uid in uids}
    assert tiered == set(build.DASHBOARDS), (
        "every dashboard must declare which tier it belongs to")
    generated = {path.stem for path in DASHBOARDS.glob("*.json")}
    assert generated == set(build.DASHBOARDS)
    # A tier with nothing in it is a tier that has quietly been abandoned.
    for tier, uids in build.TIERS.items():
        assert uids, f"the {tier} tier is empty"
    # Triage is the entry point and there must be exactly one of it: two
    # dashboards claiming to be the first thing you open is the same as none.
    assert len(build.TIERS["triage"]) == 1


@pytest.mark.parametrize("filename,dashboard", _dashboards())
def test_each_dashboard_uses_its_tier_time_horizon(filename, dashboard):
    build = _builder()
    uid = filename.removesuffix(".json")
    tier = next(name for name, uids in build.TIERS.items() if uid in uids)
    window, refresh = TIER_EXCEPTIONS.get(uid, TIER_DEFAULTS[tier])
    assert dashboard["time"]["from"] == window, (
        f"{filename} is in the {tier} tier but opens on "
        f"{dashboard['time']['from']}")
    assert dashboard["refresh"] == refresh, (
        f"{filename} is in the {tier} tier but refreshes every "
        f"{dashboard['refresh']}")


# Principle 4 and the wall-of-panels anti-pattern. Deep material belongs in a
# collapsed row, not on the page at load.
VISIBLE_PANEL_CEILING = 16


@pytest.mark.parametrize("filename,dashboard", _dashboards())
def test_no_dashboard_is_a_wall_of_panels(filename, dashboard):
    visible = _visible_panels(dashboard)
    assert len(visible) <= VISIBLE_PANEL_CEILING, (
        f"{filename} shows {len(visible)} panels on load; move the deep "
        "material into a collapsed row")


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
def test_every_dashboard_states_its_purpose_and_its_owner(filename, dashboard):
    # Principle 10. An unowned dashboard with no stated purpose rots, and the
    # place an operator will actually look for the purpose is on the dashboard.
    assert len((dashboard.get("description") or "").strip()) > 80, (
        f"{filename} does not say what it is for")
    notes = [panel for panel in _panels(dashboard)
             if panel.get("type") == "text"]
    assert len(notes) == 1, f"{filename} has no About panel"
    body = notes[0].get("options", {}).get("content", "")
    for required in ("Purpose", "Read it in this order", "Runbook"):
        assert required in body, f"{filename}'s About panel lost {required!r}"


# Principle 5: nothing renders ISE data without saying whether that data is
# current. The two tier-3 dashboards are exempt because they are themselves the
# answer to that question.
TRUST_EXEMPT = {"ise3-pipeline", "ise3-load"}


@pytest.mark.parametrize("filename,dashboard", _dashboards())
def test_every_ise_dashboard_says_whether_its_data_is_current(
        filename, dashboard):
    if filename.removesuffix(".json") in TRUST_EXEMPT:
        return
    titles = [panel.get("title") for panel in _visible_panels(dashboard)]
    for required in ("Data trustworthy", "Collection age"):
        assert required in titles, (
            f"{filename} shows ISE data without a {required!r} panel")
    # It has to be in the first row, beside the outcome it qualifies -- a trust
    # panel below the fold is a caveat nobody reads.
    assert titles.index("Data trustworthy") < 8, (
        f"{filename} buries its trust panels below the first row")


@pytest.mark.parametrize("filename,dashboard", _dashboards())
def test_no_panel_silently_truncates_its_data(filename, dashboard):
    # The old set capped every breakdown to its top 25 bars because a bar gauge
    # cannot scroll. Breakdowns are tables now, so a topk() in a visual query
    # means a cap has crept back in.
    capped = [
        panel.get("title") for panel, target in _targets(dashboard)
        if re.search(r"\b(topk|bottomk)\s*\(", target.get("expr", ""))
    ]
    assert not capped, (
        f"{filename} caps these panels without scrolling: {capped}")


def test_breakdowns_are_tables_rather_than_bar_gauges():
    # Principle 7. A bar gauge is right for a bounded, low-cardinality
    # comparison and wrong for anything unbounded, so the set should be
    # overwhelmingly tables. This is the guard against the monoculture the old
    # set had, where 90 of ~230 panels were capped bar gauges.
    counts = {}
    for _, dashboard in _dashboards():
        for panel in _panels(dashboard):
            kind = panel.get("type")
            counts[kind] = counts.get(kind, 0) + 1
    assert counts.get("bargauge", 0) * 4 <= counts.get("table", 0), (
        f"bar gauges are creeping back: {counts}")


@pytest.mark.parametrize("filename,dashboard", _dashboards())
def test_every_drilldown_points_at_a_dashboard_that_exists(filename, dashboard):
    generated = {path.stem for path in DASHBOARDS.glob("*.json")}
    body = json.dumps(dashboard)
    for uid in set(DASHBOARD_LINK.findall(body)):
        assert uid in generated, (
            f"{filename} links to /d/{uid}, which this set does not build")


def _triage_exprs():
    """Every triage panel's query expressions, keyed by panel title."""
    exprs = {}
    for panel, target in _targets(_dashboard("ise3-triage.json")):
        exprs.setdefault(panel.get("title"), []).append(target.get("expr", ""))
    return {title: " ".join(bodies) for title, bodies in exprs.items()}


def test_triage_declares_the_operations_owner_variable():
    # An ops owner owns a region's endpoints and NADs. Selecting themselves
    # must turn the diagnosis layer into their own fix list; All must keep the
    # fleet-wide behaviour, which is why the variable is multi with an All
    # default rather than a forced single choice.
    dashboard = _dashboard("ise3-triage.json")
    variables = {item["name"]: item for item in dashboard["templating"]["list"]}
    owner = variables.get("ops_owner")
    assert owner is not None, "triage lost the Operations owner variable"
    assert owner["multi"] and owner["includeAll"]
    assert owner["allValue"] == ".*", (
        "the All option must expand to a regex the =~ matchers accept")
    assert owner["query"] == (
        "label_values(ise3_network_devices_by_ops_owner, ops_owner)"), (
        "the option list must come from the by-owner census, which still "
        "offers 'unknown' when the directory classifies nothing")


def test_triage_owner_scopes_the_panels_an_owner_can_act_on():
    exprs = _triage_exprs()

    failing = exprs["Failing network devices"]
    assert '$ops_owner' in failing
    assert "ise3_network_device_assignment" in failing
    # The assignment family is not total -- an unclassified or uninventoried
    # NAD has no row -- so a bare join would silently drop a failing device.
    # The expression must keep the unmatched remainder, labelled unknown.
    assert "unless" in failing and '"unknown"' in failing

    union = exprs["Attention needed"]
    assert 'ise3_posture_endpoints' in union
    assert union.count('$ops_owner') >= 2, (
        "both actionable attention rows (failing NADs and posture) must "
        "respect the owner variable")

    # The symptom row stays fleet-wide: "is ISE serving right now" is not an
    # owner-scoped question.
    for fleet in ("Authentication success rate", "Failed authentications",
                  "Active sessions", "Nodes not connected"):
        assert '$ops_owner' not in exprs[fleet], (
            f"{fleet} is a fleet symptom and must not be owner-scoped")


def test_triage_breaks_live_failures_down_by_reason_for_an_owner():
    exprs = _triage_exprs()
    reason = exprs.get("Failure reasons for the selected owner")
    assert reason, "triage lost the per-owner failure reason breakdown"
    assert "ise3_session_failure_reason_endpoints" in reason
    assert '$ops_owner' in reason
    assert "sum by (reason_code)" in reason
    # Gated on its own dataset, like every ISE-sourced expression.
    assert 'dataset="session_authorization"' in reason


def test_triage_names_the_instance_behind_each_count():
    # A count is enough to know something is wrong and never enough to know
    # what to do about it: "three certificates expired" sent an operator to
    # another dashboard to learn which three. The named table is the answer to
    # that, so it has to keep naming things -- one row per offender, carrying
    # the reason where the exporter records one.
    exprs = _triage_exprs()
    exact = exprs.get("What exactly is wrong")
    assert exact, "triage lost the named-instance table"
    # The identity of the offender travels in a joined label, not in the count.
    assert 'label_join' in exact and '"detail"' in exact
    assert "max by (issue, detail)" in exact
    for named in ("ise3_deployment_node_state", "ise3_certificate_expiry_days",
                  "ise3_license_compliant", "ise3_node_cpu_utilization_percent",
                  "ise3_dataset_last_failure_info"):
        assert named in exact, f"the named table stopped naming {named}"
    # Why, not just what: a failing collection names its reason, and prefers
    # the reason recorded against the provider actually serving.
    assert "ise3_dataset_provider_active" in exact


def test_the_network_device_dashboard_keeps_unclassified_devices_visible():
    # The assignment family is not total, so a bare join drops exactly the
    # devices nobody has taken ownership of -- which are the ones most likely
    # to be broken. Every scoped panel keeps the unmatched remainder and labels
    # it unknown instead.
    scoped = [
        (panel.get("title"), target.get("expr", ""))
        for panel, target in _targets(_dashboard("ise3-nad.json"))
        if "ise3_network_device_assignment" in target.get("expr", "")
        and "group_left" in target.get("expr", "")
    ]
    assert scoped, "the network device dashboard stopped joining the inventory"
    for title, expr in scoped:
        assert "unless" in expr and '"unknown"' in expr, (
            f"{title} drops devices that carry no inventory assignment")


def test_the_device_fix_list_is_one_row_per_thing_to_fix():
    # The queue is per device *and* method, and the device's own figures have
    # to land on every one of its method rows. Grafana's merge joins frames on
    # the columns they share, and a device-level frame has no method column, so
    # the join is done in PromQL -- lose that and one of a device's two rows
    # comes back with empty counts.
    exprs = [target.get("expr", "")
             for panel, target in _targets(_dashboard("ise3-nad.json"))
             if panel.get("title") == "Device fix list"]
    assert len(exprs) == 5, "the fix list lost one of its columns"
    assert any("ise3_radius_failures_by_nad_method" in e for e in exprs)
    per_device = [e for e in exprs if "group_right" in e]
    assert len(per_device) == 4, (
        "the per-device columns must be joined onto the method rows")
    # And a device failing with no method paired to it still gets a row: the
    # cross-product behind the method is bounded, so requiring one would drop
    # exactly the devices the exporter could not afford to describe.
    assert all(_builder().NO_METHOD in e for e in exprs), (
        "the fix list drops devices whose failures carry no method")


def test_only_the_network_device_dashboard_owns_the_nad_health_families():
    # Two dashboards answering the same question drift apart, and the device
    # material used to live on both this and ise3-endpoints. The activity and
    # silence families belong to one page now; ise3-pipeline reads them to
    # report on the collection itself, which is a different question.
    allowed = {"ise3-nad", "ise3-pipeline"}
    for path in sorted(DASHBOARDS.glob("*.json")):
        if path.stem in allowed:
            continue
        queried = {name for name in _queried_metrics(path.name)
                   if name.startswith("ise3_nad_")}
        assert not queried, f"{path.name} duplicates {sorted(queried)}"


def test_triage_links_down_into_every_diagnostic_it_names():
    # Tier 1 is only useful if it hands off. A triage panel that identifies a
    # problem and offers nowhere to go is a dead end.
    build = _builder()
    body = json.dumps(_dashboard("ise3-triage.json"))
    linked = set(DASHBOARD_LINK.findall(body))
    for uid in ("ise3-access", "ise3-psn", "ise3-control", "ise3-pipeline"):
        assert uid in linked, f"triage does not link to {uid}"
    assert build.TIERS["triage"] == ("ise3-triage",)


# ---------------------------------------------------------------------------
# Capability contracts
#
# A dashboard may be reorganised freely, but the operator question it answers
# may not silently disappear. Each entry is the set of metrics without which
# that dashboard stops answering its question.
# ---------------------------------------------------------------------------

CAPABILITY_CONTRACTS = {
    "ise3-triage.json": {
        "ise3_radius_authentications",
        "ise3_active_sessions_total",
        "ise3_deployment_node_state",
        "ise3_dataset_up",
        "ise3_dataset_fresh",
        "ise3_dataset_provider_degraded",
        "ise3_certificates_expired",
        "ise3_backup_age_hours",
        "ise3_license_compliant",
        "ise3_radius_errors_by_nad",
        "ise3_radius_errors_by_psn",
        "ise3_radius_errors_by_message_code",
        "ise3_session_authentication_latency_seconds",
        "ise3_network_device_assignment",
        "ise3_session_failure_reason_endpoints",
    },
    "ise3-access.json": {
        "ise3_radius_authentications",
        "ise3_radius_authentication_latency_seconds",
        "ise3_radius_distinct_endpoints_total",
        "ise3_radius_reporting_window_seconds",
        "ise3_radius_failures_by_nad_method",
        "ise3_radius_failure_summary",
        "ise3_radius_errors_total",
        "ise3_radius_errors_by_method",
        "ise3_radius_authentications_by_method",
        "ise3_radius_authentications_by_protocol",
        "ise3_radius_authentications_by_policy",
        "ise3_radius_authentications_by_nad",
        "ise3_radius_accounting_events",
        "ise3_radius_accounting_session_duration_seconds",
        "ise3_session_failure_reason_endpoints",
        "ise3_session_authz_profile_endpoints",
        "ise3_session_failed_authz_profile_endpoints",
        "ise3_session_failed_policy_set_endpoints",
        "ise3_session_policy_set_endpoints_by_nad",
        "ise3_active_sessions_by_ops_owner",
    },
    "ise3-psn.json": {
        "ise3_psn_load_percent",
        "ise3_psn_average_latency_seconds",
        "ise3_psn_radius_requests_per_hour",
        "ise3_psn_diagnostic_events",
        "ise3_psn_diagnostic_events_total",
        "ise3_psn_mnt_logs_per_hour",
        "ise3_psn_noise_per_hour",
        "ise3_psn_suppression_per_hour",
        "ise3_node_cpu_utilization_percent",
        "ise3_node_memory_utilization_percent",
        "ise3_node_disk_utilization_percent",
        "ise3_active_sessions_by_psn",
        "ise3_radius_errors_by_psn",
        "ise3_source_latest_row_age_seconds",
    },
    "ise3-control.json": {
        "ise3_deployment_node_state",
        "ise3_deployment_node_service_enabled",
        "ise3_deployment_pan_ha_enabled",
        "ise3_backup_age_hours",
        "ise3_backup_configured",
        "ise3_backup_last_status",
        "ise3_certificate_expiry_days",
        "ise3_certificates_expired",
        "ise3_certificates_expiring_soon",
        "ise3_certificate_nodes_unreachable",
        "ise3_patch_level",
        "ise3_version_supported",
        "ise3_node_cpu_utilization_percent",
        "ise3_session_authentication_latency_seconds",
        "ise3_session_authentication_step_latency_seconds",
        "ise3_detail_cache_coverage",
        "ise3_dataset_last_failure_detail_info",
    },
    "ise3-endpoints.json": {
        "ise3_endpoints_total",
        "ise3_endpoints_unprofiled",
        "ise3_endpoints_by_profile",
        "ise3_endpoints_by_identity_group",
        "ise3_endpoint_inventory_field_coverage",
        "ise3_endpoint_profile_events",
        "ise3_endpoint_model",
        "ise3_endpoint_operating_system",
        "ise3_endpoint_mdm_compliant",
        "ise3_endpoints_posture_applicable",
        "ise3_network_devices_total",
    },
    # The device-owner's dashboard. Everything keyed by the switch or router
    # rather than by ISE lives here, which is why the per-NAD families moved
    # off ise3-endpoints: two dashboards answering the same question is the
    # duplication anti-pattern the design principles name.
    "ise3-nad.json": {
        "ise3_network_device_assignment",
        "ise3_network_devices_total",
        "ise3_network_devices_classified",
        "ise3_network_devices_by_type",
        "ise3_network_devices_by_location",
        "ise3_network_devices_by_ops_owner",
        "ise3_radius_errors_by_nad",
        "ise3_radius_failures_by_nad_method",
        "ise3_radius_authentications_by_nad",
        "ise3_nad_authentications",
        "ise3_nad_last_authentication_age_seconds",
        "ise3_nad_silent_total",
        "ise3_nad_activity_covered",
        "ise3_nad_activity_source_age_seconds",
        "ise3_active_sessions_by_nad",
        "ise3_session_policy_set_endpoints_by_nad",
        "ise3_session_authz_profile_endpoints",
        "ise3_session_authz_rule_endpoints",
        "ise3_session_failed_policy_set_endpoints",
        "ise3_session_failed_authz_rule_endpoints",
        "ise3_session_failed_authz_profile_endpoints",
        "ise3_tacacs_authentications",
        "ise3_tacacs_authorization_details",
        "ise3_detail_cache_coverage",
    },
    "ise3-posture.json": {
        "ise3_posture_endpoints",
        "ise3_posture_endpoints_by_psn",
        "ise3_posture_endpoints_by_os",
        "ise3_posture_policy_results",
        "ise3_posture_agent_version_endpoints",
        "ise3_posture_assessments",
        "ise3_posture_assessments_by_psn",
        "ise3_posture_assessments_by_policy",
        "ise3_posture_failed_conditions",
        "ise3_posture_eligible_endpoints_total",
        "ise3_posture_eligible_recently_assessed_total",
        "ise3_posture_eligible_without_recent_assessment_total",
        "ise3_session_detail_field_coverage",
    },
    "ise3-tacacs.json": {
        "ise3_tacacs_authentications",
        "ise3_tacacs_authorizations",
        "ise3_tacacs_authorization_details",
        "ise3_tacacs_commands",
        "ise3_tacacs_internal_accounts",
        "ise3_tacacs_internal_account_enabled",
        "ise3_tacacs_internal_account_hygiene_risk",
        "ise3_tacacs_account_last_seen_timestamp",
        "ise3_tacacs_active_accounts",
        "ise3_tacacs_policy_objects",
        "ise3_tacacs_policy_rule_count",
        "ise3_tacacs_policy_rules_total",
        "ise3_tacacs_policy_sets",
    },
    "ise3-pipeline.json": {
        "ise3_dataset_up",
        "ise3_dataset_fresh",
        "ise3_dataset_provider_active",
        "ise3_dataset_provider_available",
        "ise3_dataset_provider_degraded",
        "ise3_dataset_provider_reason_info",
        "ise3_dataset_last_failure_detail_info",
        "ise3_dataset_failures_total",
        "ise3_dataset_next_run_timestamp",
        "ise3_dataconnect_schema_view_available",
        "ise3_dataconnect_schema_column_available",
        "ise3_source_latest_row_age_seconds",
        "ise3_api_requests_total",
        "ise3_api_request_duration_seconds_bucket",
        "ise3_pxgrid_stream_connected",
        "ise3_detail_cache_coverage",
        "ise3_topk_groups_total",
        "ise3_breakdown_dimension_populated",
        "ise3_exporter_build_info",
    },
    "ise3-load.json": {
        "ise3_load_planned_requests_per_hour",
        "ise3_load_measured_requests_total",
        "ise3_load_measured_db_seconds_total",
        "ise3_load_planned_db_seconds_per_hour",
        "ise3_load_budget_utilisation",
        "ise3_budget_enforced_requests_per_hour",
        "ise3_budget_throttled_total",
        "ise3_dataconnect_effective_duty_cycle_percent",
        "ise3_dataconnect_query_last_duration_seconds",
        "ise3_dataconnect_explorer_queries_total",
        "ise3_dataset_collection_duration_seconds",
    },
    "ise3-capacity.json": {
        "ise3_license_consumption",
        "ise3_license_compliant",
        "ise3_license_compliance_state",
        "ise3_endpoints_total",
        "ise3_network_devices_total",
        "ise3_active_sessions_total",
        "ise3_node_cpu_utilization_percent",
        "ise3_certificate_expiry_days",
        "ise3_patch_installed",
        "ise3_load_budget_utilisation",
    },
}


def test_every_dashboard_has_a_capability_contract():
    generated = {path.name for path in DASHBOARDS.glob("*.json")}
    assert set(CAPABILITY_CONTRACTS) == generated, (
        "a dashboard without a capability contract can lose its whole reason "
        "to exist without any test noticing")


@pytest.mark.parametrize("filename,required", CAPABILITY_CONTRACTS.items())
def test_each_dashboard_still_answers_its_question(filename, required):
    dashboard = _dashboard(filename)
    body = json.dumps(dashboard)
    missing = sorted(metric for metric in required if metric not in body)
    assert not missing, f"{filename} lost capability metrics: {missing}"


@pytest.mark.parametrize("filename", CAPABILITY_CONTRACTS)
def test_no_dashboard_carries_a_deployment_dimension(filename):
    # One exporter serves one ISE deployment, so `instance` cannot vary within a
    # dashboard: as a selector it filters nothing, as a grouping key it splits
    # nothing, and as a variable it is a one-option picker. It came back once as
    # a `by (instance,...)` on a new panel copied from an old one, which is why
    # this is asserted rather than left to review.
    dashboard = _dashboard(filename)
    variables = {item["name"] for item in dashboard["templating"]["list"]}
    assert "deployment" not in variables
    for panel, target in _targets(dashboard):
        expr = target.get("expr", "")
        assert "instance" not in expr, f"{filename}: {panel.get('title')}: {expr}"


@pytest.mark.parametrize("filename,dashboard", _dashboards())
def test_no_table_renders_scrape_metadata(filename, dashboard):
    # `instance` and `job` name the exporter that answered. That is the same
    # exporter on every row, so the column is width spent saying nothing.
    for panel in _panels(dashboard):
        if panel.get("type") != "table":
            continue
        excluded = set()
        for transformation in panel.get("transformations", []):
            if transformation["id"] == "filterFieldsByName":
                excluded |= set(
                    transformation["options"].get("exclude", {}).get("names", []))
        missing = {"instance", "job", "__name__"} - excluded
        assert not missing, (
            f"{filename}: {panel.get('title')} shows {sorted(missing)}")


def test_the_session_delta_window_spans_more_than_one_collection():
    # delta() over a window shorter than two collection cadences measures the
    # scrape republishing the previous snapshot, not the fleet moving, so the
    # panel reads flat straight through a failover -- the one event it exists
    # to show. This is the reason the window is 15m rather than the 5m the
    # panel carried in v3's first dashboard set.
    from ise_exporter3.datasets import REGISTRY

    dataset = next(entry for entry in REGISTRY.values()
                   if entry.name == "session_distribution")
    window = _builder().SESSION_DELTA_WINDOW
    assert window.endswith("m"), f"window {window} is not in minutes"
    seconds = int(window[:-1]) * 60
    assert seconds >= 2 * dataset.default_interval, (
        f"the session delta window is {seconds}s but session_distribution "
        f"collects every {dataset.default_interval}s; a delta needs two "
        f"collections")


def test_the_psn_dashboard_shows_where_sessions_moved_to():
    # The level alone cannot distinguish a node shedding load from the fleet
    # shrinking; that is what the delta is for, and it was lost once already
    # when the set was reorganised by question rather than by data source.
    body = (DASHBOARDS / "ise3-psn.json").read_text()
    assert "ise3_active_sessions_by_psn" in body
    assert "ise3_active_sessions_by_psn offset " in body


def test_the_pipeline_dashboard_answers_which_provider_is_live():
    # The whole argument of v3 is that a source change is visible. If these
    # queries disappear, that claim stops being true on the operator's screen.
    body = (DASHBOARDS / "ise3-pipeline.json").read_text()
    assert "ise3_dataset_provider_active" in body
    assert "ise3_dataset_provider_degraded" in body
    assert "ise3_dataset_provider_reason_info" in body
    assert "ise3_dataset_provider_available" in body


def _queried_metrics(name):
    """Metric names referenced by an actual query expression in a dashboard.

    A substring check on the raw JSON is satisfied by a panel description that
    merely mentions the metric; the contract these tests state is that a query
    reads it.
    """
    queried = set()
    for _panel, target in _targets(_dashboard(name)):
        queried.update(METRIC_NAME.findall(target.get("expr", "")))
    return queried


def test_the_load_dashboard_compares_planned_against_measured():
    # Cost declarations are the one thing in this design that can quietly lie,
    # so the panel that catches them is not optional.
    queried = _queried_metrics("ise3-load.json")
    assert "ise3_load_planned_requests_per_hour" in queried
    assert "ise3_load_measured_requests_total" in queried
    assert "ise3_load_measured_db_seconds_total" in queried
    assert "ise3_load_budget_utilisation" in queried


def test_the_pipeline_dashboard_states_what_it_had_to_truncate():
    # Silent truncation is the failure mode this project refuses everywhere
    # else. Where the exporter does bound a breakdown, the cost of that bound
    # has to be on screen.
    body = (DASHBOARDS / "ise3-pipeline.json").read_text()
    assert "ise3_topk_groups_total" in body
    assert "ise3_topk_groups_returned" in body
    assert "ise3_topk_truncated" in body


def test_the_committed_dashboards_match_a_fresh_build(tmp_path):
    sdk = pytest.importorskip(
        "grafana_foundation_sdk",
        reason="the Grafana SDK is a build-time dependency; "
               "install with pip install grafana-foundation-sdk")
    assert sdk is not None
    build_dashboards3 = _builder()

    for path in build_dashboards3.build(tmp_path):
        committed = (DASHBOARDS / path.name).read_text()
        assert path.read_text() == committed, (
            f"{path.name} is stale; regenerate with "
            "python tools/build_dashboards3.py --out dashboards3")
