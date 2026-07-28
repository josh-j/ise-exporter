"""Generate the complete ise-exporter3 Grafana dashboard set.

The eight operator workflows from ise-exporter v2 are preserved here, translated
onto v3's metric contract, and joined by the two v3-specific dashboards for
provider selection and declared-versus-measured load.

Run:  python tools/build_dashboards3.py --out dashboards3
Needs the SDK:  pip install 'grafana-foundation-sdk'
"""
from __future__ import annotations

import argparse
import pathlib
import sys

from grafana_foundation_sdk.builders import (
    bargauge,
    common,
    dashboard as db,
    prometheus,
    stat,
    table,
    timeseries,
)
from grafana_foundation_sdk.cog.encoder import JSONEncoder
from grafana_foundation_sdk.models.common import (
    BarGaugeDisplayMode,
    GraphTresholdsStyleMode,
    TableCellBackgroundDisplayMode,
    TableColoredBackgroundCellOptions,
    VizOrientation,
)
from grafana_foundation_sdk.models.dashboard import (
    AnnotationQuery,
    DashboardCursorSync,
    DashboardLinkType,
    DataSourceRef,
    DataTransformerConfig,
    DynamicConfigValue,
    Threshold,
    ThresholdsMode,
    ValueMap,
    ValueMappingResult,
)
from grafana_foundation_sdk.models.prometheus import PromQueryFormat


DATASOURCE = DataSourceRef(type_val="prometheus", uid="${prometheus}")
TAGS = ["ise", "ise-exporter3"]

# The authoritative workflow mapping used by the parity contract test. A v2
# workflow may be reorganised, but it may not silently disappear.
V2_WORKFLOW_PARITY = {
    "ise-overview.json": "ise3-overview",
    "ise-access-troubleshooting.json": "ise3-access",
    "ise-endpoints-devices.json": "ise3-endpoints",
    "ise-exporter-health.json": "ise3-health",
    "ise-pan-mnt-troubleshooting.json": "ise3-pan-mnt",
    "ise-psn-troubleshooting.json": "ise3-psn",
    "ise-secureclient.json": "ise3-secureclient",
    "ise-tacacs.json": "ise3-tacacs",
}


def query(expr, legend="", instant=False, ref="A"):
    built = (
        prometheus.Dataquery()
        .expr(expr)
        .legend_format(legend)
        .ref_id(ref)
    )
    return built.instant() if instant else built.range()


def instant(expr, legend="", ref="A"):
    return query(expr, legend, instant=True, ref=ref)


def metric(name, selectors=""):
    """Select one exporter metric inside the chosen deployment(s)."""
    labels = 'instance=~"$deployment"'
    if selectors:
        labels += f",{selectors}"
    return f"{name}{{{labels}}}"


def active_health(name, dataset):
    """Health for the provider currently selected for one dataset."""
    health = metric(name, f'dataset="{dataset}"')
    active = metric(
        "ise3_dataset_provider_active",
        f'dataset="{dataset}"',
    )
    return (
        f"max by (instance) ({health} and on(instance,dataset,provider) "
        f"({active} == 1))"
    )


def ready(dataset):
    return (
        f"(({active_health('ise3_dataset_up', dataset)}) == 1) and "
        f"(({active_health('ise3_dataset_fresh', dataset)}) == 1)"
    )


def ready_bool(dataset):
    """Readiness as an explicit 0/1 series, for display only.

    ready() filters with `== 1`, so an unready dataset returns no series and a
    stat renders "No data" rather than a failure. This form always returns a
    value. It must never be used by gate(): a 0-valued series still matches
    `and on(instance)`, which would silently stop stale data being hidden.
    """
    return (
        f"(({active_health('ise3_dataset_up', dataset)}) == bool 1) * "
        f"(({active_health('ise3_dataset_fresh', dataset)}) == bool 1)"
    )


def gate(expr, dataset):
    """Drop stale data instead of showing it as current."""
    return f"({expr}) and on(instance) ({ready(dataset)})"


# A converging cache is not a failure, so `ready` stays 1 while it fills. Panels
# fed from the whole active list and panels fed from cached per-entity detail
# therefore disagree about how many endpoints exist until it is warm -- honest,
# but it reads as a bug. Gate the detail-fed ones on coverage so they blank
# rather than under-report next to a complete neighbour.
DETAIL_COVERAGE_FLOOR = 0.99


def covered(expr, dataset, cache):
    """Drop a detail-fed panel while its cache is still filling."""
    return (
        f"({gate(expr, dataset)}) and on(instance) "
        f"((max by (instance) (ise3_detail_cache_coverage{{instance=~\"$deployment\","
        f"cache=\"{cache}\"}})) >= {DETAIL_COVERAGE_FLOOR})"
    )


# A bar gauge draws one bar per series and cannot scroll, so a complete per-NAD
# breakdown is unreadable in a deployment with hundreds of them. Capping is only
# acceptable because the cap is named in the panel description and the complete
# data stays reachable -- see capped() below.
TOP_SERIES = 25


def top(expr, limit=TOP_SERIES, *, lowest=False):
    """Cap a bar gauge's visual query to its most interesting series.

    Which end that is depends on the metric: volume is read from the top, but a
    coverage fraction is read from the bottom, and topk() there would hide
    exactly the devices worth looking at.
    """
    return f"{'bottomk' if lowest else 'topk'}({limit}, {expr})"


def capped(purpose, complete, *, lowest=False):
    """Description for a top-K capped visual: what it shows, and what it hides.

    Silent truncation is the failure mode this project refuses everywhere else,
    so the cap and the route back to the complete data are both on screen.
    """
    end = "lowest" if lowest else "highest"
    return (
        f"{purpose} Capped to the {TOP_SERIES} {end}-valued bars so the gauge "
        f"stays readable in a deployment with hundreds of network devices. "
        f"{complete} The Network device variable also brings any device outside "
        "the cap into this panel."
    )


# ISE message codes seen in the RADIUS error view, worded from Cisco's
# catalogue where the string could be sourced. 12300 and 12500 are not failure
# descriptions but the last recorded flow step ("Prepared EAP-Request proposing
# <method> with challenge"): an error row carrying one died while proposing
# that method, usually because the client walked away from the TLS exchange.
# A code without a confidently sourced string is left out and renders bare,
# which is the honest default.
ISE_MESSAGE_CODES = {
    "12300": "last step: proposed PEAP to the client",
    "12321": "PEAP handshake failed, client rejected the ISE certificate",
    "12500": "last step: proposed EAP-TLS to the client",
    "12511": "unexpected TLS alert, treated as client rejection",
    "5400": "authentication failed",
    "5411": "supplicant stopped responding to ISE",
    "5440": "endpoint abandoned the EAP session and started a new one",
    "11007": "could not locate network device or AAA client",
    "11036": "invalid RADIUS Message-Authenticator attribute",
    "15039": "rejected by the matched authorization profile",
    "22040": "wrong password or invalid shared secret",
    "22056": "subject not found in the identity store",
    "24408": "Active Directory authentication failed, wrong password",
}

# The certificate, trust, and public-key codes the PKI stat panel counts.
PKI_MESSAGE_CODES = ("12300", "12321", "12500", "12501", "12511", "12625",
                     "22056")


def named_codes(expr, label="message_code"):
    """Rewrite a bare ISE code label into "<code> · <meaning>".

    One label_replace per known code, nested: label_replace matches the whole
    label value, so a rewritten value can no longer match a later code, and an
    unknown code falls through every layer and stays bare.
    """
    for code, meaning in ISE_MESSAGE_CODES.items():
        expr = (
            f'label_replace({expr}, "{label}", "{code} · {meaning}", '
            f'"{label}", "{code}")'
        )
    return expr


# Appended to every panel whose code labels named_codes() rewrites.
NAMED_CODES_NOTE = (
    " Well-known codes are annotated with what they mean; a code this "
    "exporter cannot name is shown bare."
)


def attention(issue, expr):
    """One row of a triage table: a count, labelled with what it means.

    Unioning unlike metrics into one table needs a label they all share, and
    the aggregation that makes each row a comparable count drops every label
    the metric carried, so the meaning has to be attached afterwards.
    """
    return f'label_replace({expr}, "issue", "{issue}", "", "")'


# Threshold vocabularies, as absolute (value, colour) steps. The first step is
# the base and must carry None, which serialises to the -Infinity bound.
NONZERO_CRITICAL = ((None, "green"), (1, "red"))
NONZERO_WARNING = ((None, "green"), (1, "orange"))
REQUIRED_BOOLEAN = ((None, "red"), (1, "green"))
NEUTRAL = ((None, "text"),)
UTILISATION = ((None, "green"), (80, "orange"), (90, "red"))
RATIO_HIGH_IS_GOOD = ((None, "red"), (0.85, "orange"), (0.95, "green"))
COVERAGE = ((None, "red"), (0.8, "orange"), (0.95, "green"))
BUDGET_USED = ((None, "green"), (0.8, "orange"), (1.0, "red"))
# A daily backup that has not run is the first sign of a stalled PAN, so the
# triage table and the age panel must agree on when "yesterday" has passed.
BACKUP_STALE_HOURS = 26
BACKUP_AGE_HOURS = ((None, "green"), (BACKUP_STALE_HOURS, "orange"), (50, "red"))

# Value mappings for the booleans ISE reports as 1 and 0.
YES_NO = ((1, "Yes", "green"), (0, "No", "orange"))
CONFIGURED = ((1, "Configured", "green"), (0, "Not configured", "red"))
SUPPORTED = ((1, "Supported", "green"), (0, "Unsupported", "red"))
ENABLED = ((1, "Yes", "green"), (0, "No", "red"))
TRUNCATED = ((1, "Truncated", "orange"), (0, "Complete", "green"))
READINESS = ((1, "OK", "green"), (0, "NOT READY", "red"))
RISK = ((1, "At risk", "red"), (0, "Clear", "green"))

# Blank panels are ambiguous on call: these say which kind of blank it is.
NO_DATA_EXPORTER = "no data — exporter absent?"
NO_DATA_STALE = "no data — stale, see readiness"


def _thresholds(steps):
    return (
        db.ThresholdsConfig()
        .mode(ThresholdsMode.ABSOLUTE)
        .steps([Threshold(value=value, color=color) for value, color in steps])
    )


def _mappings(entries):
    return [
        ValueMap(
            options={
                str(value): ValueMappingResult(
                    text=text, color=color, index=index
                )
                for index, (value, text, color) in enumerate(entries)
            }
        )
    ]


class _DataLink:
    """One field-level link out of a panel.

    The SDK types field links as DashboardLink, which serialises the
    dashboard-menu fields (tags, asDropdown, includeVars) Grafana ignores on a
    data link. A data link is only a title, a URL, and a target.
    """

    def __init__(self, title, url):
        self._json = {"title": title, "url": url, "targetBlank": False}

    def build(self):
        # data_links() expects a builder; this model is its own.
        return self

    def to_json(self):
        return dict(self._json)


def drilldown(title, uid, **variables):
    """A link into another dashboard that carries the current context.

    `${__url_time_range}` reproduces the window being looked at, and the
    `:queryparam` format expands the multi-value deployment variable into one
    `var-deployment=` pair per selected deployment rather than one comma-joined
    value, which Grafana would read as a single deployment name.
    """
    parameters = ["${__url_time_range}", "${deployment:queryparam}"]
    parameters += [f"var-{name}={value}" for name, value in variables.items()]
    return _DataLink(title, f"/d/{uid}?" + "&".join(parameters))


# The two ways a clicked value names an entity. A bar gauge clicks a series, so
# the label is read off the field; a table clicks a row, so it is read off the
# named column of that row.
def by_series(label):
    return "${__field.labels." + label + "}"


def by_row(column):
    return "${__data.fields." + column + "}"


def by_ref(ref, *, unit=None, thresholds=None, mappings=None,
           minimum=None, maximum=None):
    """Field config for one query refId.

    Stat and bar-gauge panels share one field config across every target, so a
    panel that shows a boolean beside an age, or a latency beside a coverage
    fraction, needs the differing target overridden rather than a second panel.
    """
    properties = []
    if unit is not None:
        properties.append(DynamicConfigValue("unit", unit))
    if thresholds is not None:
        properties.append(
            DynamicConfigValue("thresholds", _thresholds(thresholds).build())
        )
    if mappings is not None:
        properties.append(DynamicConfigValue("mappings", _mappings(mappings)))
    if minimum is not None:
        properties.append(DynamicConfigValue("min", minimum))
    if maximum is not None:
        properties.append(DynamicConfigValue("max", maximum))
    return ref, properties


def by_column(name, *, unit=None, thresholds=None, mappings=None,
              colour_cells=False, links=()):
    """Field config for one named table column.

    A table's columns are matched by the name they carry after transformation,
    so by_ref() stops working the moment several queries are merged into one
    frame: every column then belongs to the same refId.
    """
    properties = []
    if unit is not None:
        properties.append(DynamicConfigValue("unit", unit))
    if thresholds is not None:
        properties.append(
            DynamicConfigValue("thresholds", _thresholds(thresholds).build())
        )
    if mappings is not None:
        properties.append(DynamicConfigValue("mappings", _mappings(mappings)))
    if colour_cells:
        properties.append(
            DynamicConfigValue(
                "custom.cellOptions",
                TableColoredBackgroundCellOptions(
                    mode=TableCellBackgroundDisplayMode.BASIC
                ),
            )
        )
    if links:
        # Scoped to one column so only the identifying cell is clickable; a
        # panel-wide data link turns every cell of every row into the same link.
        properties.append(DynamicConfigValue("links", list(links)))
    return name, properties


# The two column shapes that repeat across the table set: a 1/0 flag, which is
# unreadable as a bare number in a grid of them, and an age in seconds.
BOOLEAN_CELL = {
    "mappings": ENABLED,
    "thresholds": REQUIRED_BOOLEAN,
    "colour_cells": True,
}
SECONDS_CELL = {"unit": "s"}


def _visual_state(panel, thresholds, mappings, minimum, maximum, overrides,
                  data_links=()):
    if data_links:
        panel = panel.data_links(list(data_links))
    if thresholds is not None:
        panel = panel.thresholds(_thresholds(thresholds))
    if mappings is not None:
        panel = panel.mappings(_mappings(mappings))
    if minimum is not None:
        panel = panel.min(minimum)
    if maximum is not None:
        panel = panel.max(maximum)
    for ref, properties in overrides:
        panel = panel.override_by_query(ref, properties)
    return panel


def datasource_variable():
    """File-provisioned dashboards cannot hardcode a datasource uid."""
    return (
        db.DatasourceVariable("prometheus")
        .label("Prometheus")
        .type("prometheus")
    )


def deployment_variable():
    """Scrape instance is the deployment boundary shared with v2 dashboards."""
    return (
        db.QueryVariable("deployment")
        .label("Deployment")
        .datasource(DATASOURCE)
        .query("label_values(ise3_dataset_enabled, instance)")
        .include_all(True)
        .all_value(".*")
        .multi(True)
    )


def label_variable(name, label, source_metric, source_label, selectors=""):
    return (
        db.QueryVariable(name)
        .label(label)
        .datasource(DATASOURCE)
        .query(f"label_values({metric(source_metric, selectors)}, {source_label})")
        .include_all(True)
        .all_value(".*")
        .multi(True)
    )


class _PrometheusAnnotationQuery(AnnotationQuery):
    """An annotation query carrying the Prometheus-only formatting fields.

    The generated model stops at `expr`, but Grafana reads titleFormat and
    textFormat off the annotation object itself: without them a marker says
    only that something happened, not to which dataset, node, or build.
    """

    def __init__(self, formats, **fields):
        super().__init__(**fields)
        self._formats = formats

    def to_json(self):
        return {**super().to_json(), **self._formats}

    def build(self):
        # Dashboard.annotation() expects a builder; this model is its own.
        return self


def annotation(name, expr, *, title, text, colour, step="1m"):
    """One class of change event, painted across every panel of a dashboard."""
    return _PrometheusAnnotationQuery(
        {"titleFormat": title, "textFormat": text, "step": step},
        name=name,
        datasource=DATASOURCE,
        enable=True,
        # hide=False keeps each annotation's own toggle in the dashboard bar,
        # so one class of event can be switched off without editing anything.
        hide=False,
        icon_color=colour,
        expr=expr,
    )


def change_annotations():
    """The exporter-side changes that explain a discontinuity in a graph.

    Every dashboard gets these, because the first question about a step in any
    line here is whether ISE changed or the exporter's view of ISE changed.
    """
    return (
        annotation(
            "Provider changed",
            f"changes({metric('ise3_dataset_provider_active')}[5m]) > 0",
            title="Provider changed: {{dataset}}",
            text="{{dataset}} is now served by {{provider}}",
            colour="purple",
        ),
        annotation(
            # Aggregated to the node: node_state is one series per state, so an
            # unaggregated changes() fires twice for a single transition, and
            # again whenever the roles or services labels churn.
            "Node state changed",
            "max by (instance,node) (changes("
            f"{metric('ise3_deployment_node_state')}[10m])) > 0",
            title="Node state changed: {{node}}",
            text="{{node}} changed deployment state",
            colour="orange",
        ),
        annotation(
            # changes() can never see a build change: the version is a label,
            # so an upgrade retires the old series and starts a new one whose
            # value never changed. Presence-churn does fire: the new series
            # exists where its five-minute-older self does not, and Grafana
            # folds the contiguous samples into one region annotation.
            "Exporter build changed",
            f"{metric('ise3_exporter_build_info')} unless "
            f"({metric('ise3_exporter_build_info')} offset 5m)",
            title="Exporter build changed",
            text="exporter {{version}} targeting ISE {{target_ise_release}}",
            colour="blue",
        ),
    )


def base(title, uid, description, *, refresh="5m", variables=()):
    navigation = (
        db.DashboardLink("ISE Exporter 3 dashboards")
        .type(DashboardLinkType.DASHBOARDS)
        .tags(["ise-exporter3"])
        .as_dropdown(True)
        .include_vars(True)
        .keep_time(True)
    )
    dashboard = (
        db.Dashboard(title)
        .uid(uid)
        .description(description)
        .tags(TAGS)
        .tooltip(DashboardCursorSync.CROSSHAIR)
        .refresh(refresh)
        .time("now-6h", "now")
        .editable()
        .link(navigation)
        .with_variable(datasource_variable())
        .with_variable(deployment_variable())
    )
    for change in change_annotations():
        dashboard = dashboard.annotation(change)
    for variable in variables:
        dashboard = dashboard.with_variable(variable)
    return dashboard


def ts(title, description, targets, *, unit="short", stacking=False,
       thresholds=None, mappings=None, minimum=None, maximum=None,
       overrides=()):
    panel = (
        timeseries.Panel()
        .title(title)
        .description(description)
        .datasource(DATASOURCE)
        .unit(unit)
        .fill_opacity(10 if stacking else 0)
        .legend(
            common.VizLegendOptions()
            .display_mode("table")
            .placement("bottom")
            .calcs(["lastNotNull", "max"])
        )
    )
    if thresholds is not None:
        # A time series only draws its thresholds when a style asks it to.
        panel = panel.thresholds_style(
            common.GraphThresholdsStyleConfig().mode(
                GraphTresholdsStyleMode.DASHED
            )
        )
    for target in targets:
        panel = panel.with_target(target)
    return _visual_state(
        panel, thresholds, mappings, minimum, maximum, overrides
    )


# Prometheus table format emits these beside the label columns. `instance` is
# deliberately not here: it is the deployment boundary every dashboard is scoped
# by, so on a multi-deployment selection it is the first thing to read.
NOISE_COLUMNS = ("Time", "__name__", "job")

# Targets are declared in refId order everywhere in this file, as query() and
# instant() default to "A" and each extra target names the next letter.
REF_IDS = "ABCDEFGH"


def _value_column(index, total):
    """What the Prometheus datasource calls one target's value column.

    It is "Value" for a lone query and "Value #<refId>" as soon as a panel has
    more than one, which is also the name the merge and organize
    transformations match on.
    """
    return "Value" if total == 1 else f"Value #{REF_IDS[index]}"


def _table_transformations(count, columns, sort):
    """Turn one frame per query into a single joined, labelled table.

    Without this a multi-target table renders as a frame picker showing one
    query at a time. `merge` joins the frames on every column they share, which
    is why the noise columns are filtered off first: an instant query carries a
    Time column, and a shared column is part of the join key.
    """
    hidden = list(NOISE_COLUMNS)
    renamed = {}
    for index, name in enumerate(columns):
        column = _value_column(index, count)
        if name is None:
            # An info metric's value is always 1; its labels carry the meaning.
            hidden.append(column)
        else:
            renamed[column] = name
    transformations = [
        DataTransformerConfig(
            id_val="filterFieldsByName", options={"exclude": {"names": hidden}}
        )
    ]
    if count > 1:
        transformations.append(DataTransformerConfig(id_val="merge", options={}))
    if renamed:
        transformations.append(
            DataTransformerConfig(
                id_val="organize", options={"renameByName": renamed}
            )
        )
    if sort is not None:
        column, descending = sort
        transformations.append(
            DataTransformerConfig(
                id_val="sortBy",
                options={"sort": [{"field": column, "desc": descending}]},
            )
        )
    return transformations


def tbl(title, description, targets, *, columns, sort=None, thresholds=None,
        mappings=None, overrides=(), column_overrides=()):
    """A joined table.

    `columns` names one value column per target, in refId order, or None to
    drop that target's value and keep only the labels it joins in. `sort`
    reorders rows worst-first where the query itself does not already.
    """
    assert len(columns) == len(targets), title
    panel = (
        table.Panel()
        .title(title)
        .description(description)
        .datasource(DATASOURCE)
        .transformations(_table_transformations(len(targets), columns, sort))
    )
    for target in targets:
        # Label columns only exist in table format; time_series format gives
        # the panel one series per row and nothing to join on.
        panel = panel.with_target(target.format(PromQueryFormat.TABLE))
    panel = _visual_state(panel, thresholds, mappings, None, None, overrides)
    for column, properties in column_overrides:
        panel = panel.override_by_name(column, properties)
    return panel


def stat_panel(title, description, targets, *, unit="short", thresholds=None,
               mappings=None, minimum=None, maximum=None, overrides=(),
               no_value=None, data_links=()):
    panel = (
        stat.Panel()
        .title(title)
        .description(description)
        .datasource(DATASOURCE)
        .unit(unit)
    )
    if no_value is not None:
        panel = panel.no_value(no_value)
    for target in targets:
        panel = panel.with_target(target)
    return _visual_state(
        panel, thresholds, mappings, minimum, maximum, overrides, data_links
    )


def bar(title, description, targets, *, unit="short", thresholds=None,
        mappings=None, minimum=None, maximum=None, overrides=(),
        data_links=()):
    panel = (
        bargauge.Panel()
        .title(title)
        .description(description)
        .datasource(DATASOURCE)
        .unit(unit)
        .orientation(VizOrientation.HORIZONTAL)
        .display_mode(BarGaugeDisplayMode.GRADIENT)
        .show_unfilled(True)
    )
    for target in targets:
        panel = panel.with_target(target)
    return _visual_state(
        panel, thresholds, mappings, minimum, maximum, overrides, data_links
    )


def sized(panel, height=8, span=12):
    return panel.height(height).span(span)


# A third element on a section marks the row as folded away on load, for
# material that is needed during an investigation but not to start one.
COLLAPSED = "collapsed"


def assemble(title, uid, description, sections, *, variables=(), refresh="5m"):
    dashboard = base(
        title, uid, description, refresh=refresh, variables=variables
    )
    for section in sections:
        row_title, panels = section[:2]
        row = db.Row(row_title)
        if COLLAPSED in section[2:]:
            # Grafana drops a collapsed row's panels unless they are nested
            # inside the row object itself; Row.with_panel() nests them there
            # and sets the collapsed flag, and with_row() then lays them out.
            for panel in panels:
                row = row.with_panel(panel)
            dashboard = dashboard.with_row(row)
            continue
        dashboard = dashboard.with_row(row)
        for panel in panels:
            dashboard = dashboard.with_panel(panel)
    return dashboard


def overview_dashboard():
    # The union that answers "is anything wrong?" before anything is read. Each
    # row counts one class of problem and says what it is, so an empty table is
    # the healthy state and needs no interpretation. ISE-sourced rows are gated
    # so a stale collection cannot raise an alarm that has already been fixed.
    attention_needed = " or ".join(
        (
            attention(
                "Datasets on a fallback provider",
                "count by (instance) "
                f"({metric('ise3_dataset_provider_degraded')} > 0)",
            ),
            attention(
                "Certificates already expired",
                "sum by (instance) "
                f"(({gate(metric('ise3_certificates_expired'), 'certificates')}) > 0)",
            ),
            attention(
                f"Deployments whose backup is older than {BACKUP_STALE_HOURS} hours",
                "count by (instance) "
                f"(({gate(metric('ise3_backup_age_hours'), 'backup')}) "
                f"> {BACKUP_STALE_HOURS})",
            ),
            attention(
                "Deployments running an unsupported ISE version",
                "count by (instance) "
                f"(({gate(metric('ise3_version_supported'), 'patches')}) == 0)",
            ),
            attention(
                "Enabled licence tiers out of compliance",
                "count by (instance) "
                f"((({gate(metric('ise3_license_compliant'), 'licensing')}) == 0) "
                "and on(instance,provider,tier) "
                f"(({metric('ise3_license_enabled')}) == 1))",
            ),
            attention(
                "Datasets whose latest collection attempt failed",
                "count by (instance) "
                f"({metric('ise3_dataset_last_failure_detail_info')} == 1)",
            ),
        )
    )
    return assemble(
        "ISE 3 — Overview",
        "ise3-overview",
        "Deployment, session, inventory, certificate, backup, licensing, and "
        "software posture at a glance. Every value is gated on fresh data.",
        (
            (
                "Platform status",
                (
                    sized(
                        tbl(
                            "Attention needed",
                            "Every condition on this dashboard that warrants "
                            "action, counted and named. An empty table is the "
                            "healthy state; each row says which panel below "
                            "explains it.",
                            [instant(attention_needed, "attention")],
                            columns=("count",),
                            sort=("count", True),
                            column_overrides=(
                                by_column("count", thresholds=NONZERO_CRITICAL,
                                          colour_cells=True),
                            ),
                        ),
                        6,
                        24,
                    ),
                    sized(
                        stat_panel(
                            "Ready datasets",
                            "Enabled datasets whose currently selected provider "
                            "completed successfully and remains fresh.",
                            [
                                instant(
                                    f"sum(({metric('ise3_dataset_up')} == 1) and "
                                    "on(instance,dataset,provider) "
                                    f"({metric('ise3_dataset_provider_active')} == 1))",
                                    "ready",
                                )
                            ],
                        ),
                        5,
                        8,
                    ),
                    sized(
                        stat_panel(
                            "Degraded datasets",
                            "Datasets running on a lower-preference provider. "
                            "The Sources dashboard explains every fallback.",
                            [
                                instant(
                                    f"sum({metric('ise3_dataset_provider_degraded')} > 0)",
                                    "degraded",
                                )
                            ],
                            thresholds=NONZERO_CRITICAL,
                            data_links=(
                                drilldown(
                                    "Which source, and why", "ise3-sources"
                                ),
                            ),
                        ),
                        5,
                        8,
                    ),
                    sized(
                        stat_panel(
                            "Active sessions",
                            "Current RADIUS sessions from the active provider; "
                            "the provider label preserves source semantics.",
                            [
                                instant(
                                    f"sum({gate(metric('ise3_active_sessions_total'), 'active_sessions')})",
                                    "sessions",
                                )
                            ],
                            no_value=NO_DATA_STALE,
                        ),
                        5,
                        8,
                    ),
                    sized(
                        bar(
                            "Deployment nodes by role",
                            "Connected deployment capacity by assigned ISE role, "
                            "including distributed and standalone deployments.",
                            [
                                instant(
                                    "sort_desc(sum by (role) "
                                    f"({gate(metric('ise3_deployment_nodes'), 'deployment')}))",
                                    "{{role}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        tbl(
                            "Deployment node status",
                            "Current state, role, and service assignment for "
                            "every ISE node in the selected deployments.",
                            [
                                instant(
                                    gate(
                                        metric(
                                            "ise3_deployment_node_state",
                                            'state=~".*"',
                                        ),
                                        "deployment",
                                    )
                                    + " == 1",
                                    "{{node}} · {{roles}} · {{state}}",
                                )
                            ],
                            columns=(None,),
                        )
                    ),
                    sized(
                        stat_panel(
                            "PAN HA enabled",
                            "Whether administrative high availability is enabled "
                            "for each selected ISE deployment.",
                            [
                                instant(
                                    metric("ise3_deployment_pan_ha_enabled"),
                                    "{{instance}}",
                                )
                            ],
                            mappings=YES_NO,
                        ),
                        5,
                        12,
                    ),
                    sized(
                        stat_panel(
                            "Endpoint and NAD inventory",
                            "Authoritative endpoint and configured network-device "
                            "totals from their currently selected providers.",
                            [
                                instant(
                                    f"sum({gate(metric('ise3_endpoints_total'), 'endpoint_inventory')})",
                                    "endpoints",
                                ),
                                instant(
                                    f"sum({gate(metric('ise3_network_devices_total'), 'network_devices')})",
                                    "network devices",
                                    "B",
                                ),
                            ],
                            no_value=NO_DATA_STALE,
                        ),
                        5,
                        12,
                    ),
                ),
            ),
            (
                "Certificates, backup, and software",
                (
                    sized(
                        stat_panel(
                            "Certificate findings",
                            "Expired and soon-to-expire certificate counts across "
                            "all scanned deployment nodes.",
                            [
                                instant(
                                    f"sum({gate(metric('ise3_certificates_expired'), 'certificates')})",
                                    "expired",
                                ),
                                instant(
                                    f"sum({gate(metric('ise3_certificates_expiring_soon'), 'certificates')})",
                                    "expiring soon",
                                    "B",
                                ),
                            ],
                            thresholds=NONZERO_CRITICAL,
                            overrides=(by_ref("B", thresholds=NONZERO_WARNING),),
                            no_value=NO_DATA_STALE,
                        ),
                        5,
                        8,
                    ),
                    sized(
                        stat_panel(
                            "Backup state",
                            "Whether backup is configured and the reported age "
                            "of the latest successful backup.",
                            [
                                instant(
                                    metric("ise3_backup_configured"), "configured"
                                ),
                                instant(
                                    metric("ise3_backup_age_hours"),
                                    "age",
                                    "B",
                                ),
                            ],
                            unit="h",
                            thresholds=BACKUP_AGE_HOURS,
                            # The configured flag is a boolean, not an age.
                            overrides=(
                                by_ref(
                                    "A",
                                    unit="short",
                                    mappings=CONFIGURED,
                                    thresholds=REQUIRED_BOOLEAN,
                                ),
                            ),
                        ),
                        5,
                        8,
                    ),
                    sized(
                        stat_panel(
                            "Patch and version support",
                            "Installed patch level and whether the appliance "
                            "version is supported by this exporter build.",
                            [
                                instant(metric("ise3_patch_level"), "patch"),
                                instant(
                                    metric("ise3_version_supported"),
                                    "{{version}} supported",
                                    "B",
                                ),
                            ],
                            overrides=(
                                by_ref(
                                    "B",
                                    mappings=SUPPORTED,
                                    thresholds=REQUIRED_BOOLEAN,
                                ),
                            ),
                        ),
                        5,
                        8,
                    ),
                    sized(
                        tbl(
                            "Certificate expiry",
                            "Soonest certificate expiry by node, store, usage, "
                            "and certificate identity for renewal planning.",
                            [
                                instant(
                                    "sort("
                                    f"{gate(metric('ise3_certificate_expiry_days'), 'certificates')}"
                                    ")",
                                    "{{node}} · {{store}} · {{certificate}}",
                                )
                            ],
                            columns=("days to expiry",),
                        )
                    ),
                    sized(
                        tbl(
                            "Installed patches",
                            "The installed-state record for each known patch "
                            "number on the selected deployments.",
                            [
                                instant(
                                    metric("ise3_patch_installed"),
                                    "patch {{patch_number}}",
                                )
                            ],
                            columns=("installed",),
                            column_overrides=(by_column("installed", **BOOLEAN_CELL),),
                        )
                    ),
                ),
            ),
            (
                "Licensing",
                (
                    sized(
                        bar(
                            "License consumption",
                            "Current consumption by license tier, preserving "
                            "each deployment as a separate series.",
                            [
                                instant(
                                    "sort_desc(sum by (tier) "
                                    f"({gate(metric('ise3_license_consumption'), 'licensing')}))",
                                    "{{tier}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        tbl(
                            "License enabled and compliant",
                            "Enabled and compliance state for every reported "
                            "license tier in the selected deployments.",
                            [
                                instant(
                                    metric("ise3_license_enabled"),
                                    "{{tier}} enabled",
                                ),
                                instant(
                                    metric("ise3_license_compliant"),
                                    "{{tier}} compliant",
                                    "B",
                                ),
                            ],
                            columns=("enabled", "compliant"),
                            column_overrides=(
                                by_column("enabled", colour_cells=True),
                                by_column("compliant", colour_cells=True),
                            ),
                            mappings=ENABLED,
                        )
                    ),
                ),
            ),
        ),
    )


def access_dashboard():
    psn = label_variable(
        "psn", "PSN", "ise3_radius_authentications_by_psn", "psn"
    )
    nad = label_variable(
        "nad", "Network device", "ise3_radius_authentications_by_nad", "nad"
    )
    owner = label_variable(
        "ops_owner",
        "Ops owner",
        "ise3_session_status_endpoints",
        "ops_owner",
    )
    # Every NAD or PSN named on this dashboard is a place the next question is
    # asked somewhere else, so each one is a link to where that question lives.
    nad_series_drilldown = drilldown(
        "This network device on Endpoints and devices",
        "ise3-endpoints",
        nad=by_series("nad"),
    )
    nad_row_drilldown = drilldown(
        "This network device on Endpoints and devices",
        "ise3-endpoints",
        nad=by_row("nad"),
    )
    # The accounting metrics carry the device in a generic `value` label, since
    # one metric serves every breakdown dimension, so they need their own link.
    nad_value_drilldown = drilldown(
        "This network device on Endpoints and devices",
        "ise3-endpoints",
        nad=by_series("value"),
    )
    psn_drilldown = drilldown(
        "This node on PSN troubleshooting", "ise3-psn", node=by_series("psn")
    )
    auth = metric("ise3_radius_authentications")
    passed = metric("ise3_radius_authentications", 'status="passed"')
    failed = metric("ise3_radius_authentications", 'status="failed"')
    error_nads = gate(
        metric("ise3_radius_errors_by_nad", 'nad=~"$nad"'),
        "radius_errors",
    )
    assignments = metric(
        "ise3_network_device_assignment",
        'nad=~"$nad",ops_owner=~"$ops_owner"',
    )
    # One union rather than three targets: profile, rule, and policy set share
    # only ops_owner, so joining them into one table would pair an unrelated
    # profile, rule, and policy set onto the same row.
    failed_context = " or ".join(
        "("
        + gate(
            metric(name, 'ops_owner=~"$ops_owner"'), "session_authorization"
        )
        + ")"
        for name in (
            "ise3_session_failed_authz_profile_endpoints",
            "ise3_session_failed_authz_rule_endpoints",
            "ise3_session_failed_policy_set_endpoints",
        )
    )
    return assemble(
        "ISE 3 — Access Troubleshooting",
        "ise3-access",
        "RADIUS outcomes, latency, errors, active sessions, and the live "
        "authorization decisions needed to troubleshoot access failures.",
        (
            (
                "Outcome and volume",
                (
                    sized(
                        stat_panel(
                            "Dataset readiness",
                            "Freshness of reporting, accounting, error, live-session, "
                            "and session-authorization datasets used below. NOT "
                            "READY means the panels fed by that dataset stay blank.",
                            [
                                instant(ready_bool("radius_reporting"), "reporting"),
                                instant(ready_bool("radius_errors"), "errors", "B"),
                                instant(
                                    ready_bool("active_sessions"), "sessions", "C"
                                ),
                                instant(
                                    ready_bool("session_authorization"),
                                    "authorization",
                                    "D",
                                ),
                                instant(
                                    ready_bool("radius_accounting"),
                                    "accounting",
                                    "E",
                                ),
                            ],
                            mappings=READINESS,
                            no_value=NO_DATA_EXPORTER,
                        ),
                        5,
                        8,
                    ),
                    sized(
                        stat_panel(
                            "Pass rate",
                            "Passed authentications divided by all authentication "
                            "outcomes in the current reporting window.",
                            [
                                instant(
                                    f"sum({gate(passed, 'radius_reporting')}) / "
                                    f"clamp_min(sum({gate(auth, 'radius_reporting')}), 1)",
                                    "pass rate",
                                )
                            ],
                            unit="percentunit",
                            thresholds=RATIO_HIGH_IS_GOOD,
                            minimum=0,
                            maximum=1,
                            no_value=NO_DATA_STALE,
                        ),
                        5,
                        8,
                    ),
                    sized(
                        stat_panel(
                            "Collection age",
                            "Seconds since the most recent successful "
                            "authentication, accounting, and error snapshots.",
                            [
                                instant(
                                    "time() - "
                                    + metric(
                                        "ise3_dataset_last_success_timestamp",
                                        'dataset="radius_reporting"',
                                    ),
                                    "authentication",
                                ),
                                instant(
                                    "time() - "
                                    + metric(
                                        "ise3_dataset_last_success_timestamp",
                                        'dataset="radius_accounting"',
                                    ),
                                    "accounting",
                                    "B",
                                ),
                                instant(
                                    "time() - "
                                    + metric(
                                        "ise3_dataset_last_success_timestamp",
                                        'dataset="radius_errors"',
                                    ),
                                    "errors",
                                    "C",
                                ),
                            ],
                            unit="s",
                        ),
                        5,
                        8,
                    ),
                    sized(
                        stat_panel(
                            "Reporting windows",
                            "Exact time span each window gauge on this dashboard "
                            "represents. Authentication and accounting totals are "
                            "not counters: they are the volume seen in these "
                            "windows, so a total only means what the window says.",
                            [
                                instant(
                                    metric("ise3_radius_reporting_window_seconds"),
                                    "authentication window",
                                ),
                                instant(
                                    metric("ise3_radius_accounting_window_seconds"),
                                    "accounting window",
                                    "B",
                                ),
                            ],
                            unit="s",
                            no_value=NO_DATA_EXPORTER,
                        ),
                        5,
                        8,
                    ),
                    sized(
                        stat_panel(
                            "Failed authentications",
                            "Exact failed-authentication total for the reporting "
                            "window, independent of dimensional breakdowns.",
                            [
                                instant(
                                    f"sum({gate(failed, 'radius_reporting')})",
                                    "failed",
                                )
                            ],
                            no_value=NO_DATA_STALE,
                        ),
                        5,
                        8,
                    ),
                    sized(
                        stat_panel(
                            "Repeat authentication intensity",
                            "Authentication attempts divided by distinct "
                            "authenticating endpoints in the same exact reporting "
                            "window; values above one identify repeated attempts.",
                            [
                                instant(
                                    f"sum({gate(auth, 'radius_reporting')}) / "
                                    "clamp_min(sum("
                                    + gate(
                                        metric(
                                            "ise3_radius_distinct_endpoints_total"
                                        ),
                                        "radius_reporting",
                                    )
                                    + "), 1)",
                                    "attempts per endpoint",
                                )
                            ],
                            no_value=NO_DATA_STALE,
                        ),
                        5,
                        8,
                    ),
                    sized(
                        ts(
                            "Authentication outcome",
                            "Passed and failed authentication totals over time; "
                            "these are window gauges rather than event counters.",
                            [
                                query(
                                    gate(auth, "radius_reporting"),
                                    "{{status}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        ts(
                            "Active sessions and endpoints",
                            "Current session count and distinct endpoints holding "
                            "sessions from the active live-session provider.",
                            [
                                query(
                                    gate(
                                        metric("ise3_active_sessions_total"),
                                        "active_sessions",
                                    ),
                                    "{{provider}} sessions",
                                ),
                                query(
                                    gate(
                                        metric("ise3_active_session_endpoints"),
                                        "active_sessions",
                                    ),
                                    "{{provider}} endpoints",
                                    ref="B",
                                ),
                            ],
                        )
                    ),
                ),
            ),
            (
                "Failure isolation",
                (
                    sized(
                        stat_panel(
                            "RADIUS errors",
                            "Exact number of error-view rows in the current "
                            "reporting window from the selected provider.",
                            [
                                instant(
                                    f"sum({gate(metric('ise3_radius_errors_total'), 'radius_errors')})",
                                    "errors",
                                )
                            ],
                            no_value=NO_DATA_STALE,
                        ),
                        5,
                        12,
                    ),
                    sized(
                        stat_panel(
                            "Certificate and PKI errors",
                            "RADIUS error events carrying ISE message codes "
                            "associated with certificate validation, trust, or "
                            "public-key infrastructure failures.",
                            [
                                instant(
                                    "sum("
                                    + gate(
                                        metric(
                                            "ise3_radius_errors_by_message_code",
                                            "message_code=~\""
                                            + "|".join(PKI_MESSAGE_CODES)
                                            + "\"",
                                        ),
                                        "radius_errors",
                                    )
                                    + ")",
                                    "PKI errors",
                                )
                            ],
                            thresholds=NONZERO_WARNING,
                            no_value=NO_DATA_STALE,
                        ),
                        5,
                        12,
                    ),
                    sized(
                        bar(
                            "Top error codes",
                            "ISE message-code distribution for the current error "
                            "window, sorted from most to least frequent."
                            + NAMED_CODES_NOTE,
                            [
                                instant(
                                    "sort_desc("
                                    + named_codes(
                                        "sum by (message_code) "
                                        f"({gate(metric('ise3_radius_errors_by_message_code'), 'radius_errors')})"
                                    )
                                    + ")",
                                    "{{message_code}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        ts(
                            "Errors by message code over time",
                            "When did this error start: the same message-code "
                            "distribution as the bar above, plotted over time. "
                            "These are window gauges rather than counters, so a "
                            "line is the volume seen in each reporting window and "
                            "its onset is the moment the failure began."
                            + NAMED_CODES_NOTE,
                            [
                                query(
                                    named_codes(
                                        "sum by (message_code) "
                                        f"({gate(metric('ise3_radius_errors_by_message_code'), 'radius_errors')})"
                                    ),
                                    "{{message_code}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        bar(
                            "Failure classes",
                            "Historical failed authentications classified into "
                            "bounded operational causes from ISE failure text.",
                            [
                                instant(
                                    "sort_desc("
                                    + gate(
                                        metric(
                                            "ise3_radius_failure_summary",
                                            'dimension="failure_class"',
                                        ),
                                        "radius_reporting",
                                    )
                                    + ")",
                                    "{{value}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        ts(
                            "Failure classes over time",
                            "When did this class of failure start: the bounded "
                            "operational causes from the bar above, plotted over "
                            "time. The window gauge makes onset and recovery "
                            "visible, which a single current value cannot show.",
                            [
                                query(
                                    gate(
                                        metric(
                                            "ise3_radius_failure_summary",
                                            'dimension="failure_class"',
                                        ),
                                        "radius_reporting",
                                    ),
                                    "{{value}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        bar(
                            "Failed authorization and identity summary",
                            "Historical failed authentications by authorization "
                            "profile, identity store/group, device type, and "
                            "security group. These are marginals and are not "
                            "additive across dimensions.",
                            [
                                instant(
                                    "sort_desc("
                                    + gate(
                                        metric(
                                            "ise3_radius_failure_summary",
                                            'dimension=~"authorization_profile|identity_store|identity_group|device_type|security_group"',
                                        ),
                                        "radius_reporting",
                                    )
                                    + ")",
                                    "{{dimension}} · {{value}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        bar(
                            "Failing network devices",
                            capped(
                                "Network devices represented in the RADIUS "
                                "error view, worst first.",
                                "The complete per-device error list, including "
                                "every low-volume device, is the \"RADIUS "
                                "failure work queue\" table below.",
                            ),
                            [
                                instant(
                                    "sort_desc("
                                    + top(
                                        "sum by (nad) "
                                        "("
                                        + gate(
                                            metric(
                                                "ise3_radius_errors_by_nad",
                                                'nad=~"$nad"',
                                            ),
                                            "radius_errors",
                                        )
                                        + ")"
                                    )
                                    + ")",
                                    "{{nad}}",
                                )
                            ],
                            data_links=(nad_series_drilldown,),
                        )
                    ),
                    sized(
                        bar(
                            "Failure methods",
                            "Authentication methods represented in RADIUS errors, "
                            "which separates protocol issues from device issues.",
                            [
                                instant(
                                    "sort_desc(sum by (method) "
                                    f"({gate(metric('ise3_radius_errors_by_method'), 'radius_errors')}))",
                                    "{{method}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        bar(
                            "Authentication methods at failing NADs",
                            "The deliberately bounded NAD-by-method failure "
                            "correlation. Unlike separate marginals, this retains "
                            "which method failed at which network device.",
                            [
                                instant(
                                    "sort_desc("
                                    + gate(
                                        metric(
                                            "ise3_radius_failures_by_nad_method",
                                            'nad=~"$nad"',
                                        ),
                                        "radius_reporting",
                                    )
                                    + ")",
                                    "{{nad}} · {{method}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        stat_panel(
                            "Failure-context coverage",
                            "Published versus existing NAD-by-method failure "
                            "groups, making the troubleshooting bound explicit.",
                            [
                                instant(
                                    metric(
                                        "ise3_topk_groups_returned",
                                        'dataset="radius_reporting",breakdown="failure_context"',
                                    ),
                                    "published",
                                ),
                                instant(
                                    metric(
                                        "ise3_topk_groups_total",
                                        'dataset="radius_reporting",breakdown="failure_context"',
                                    ),
                                    "total",
                                    "B",
                                ),
                                instant(
                                    metric(
                                        "ise3_topk_truncated",
                                        'dataset="radius_reporting",breakdown="failure_context"',
                                    ),
                                    "truncated",
                                    "C",
                                ),
                            ],
                            overrides=(
                                by_ref(
                                    "C",
                                    mappings=TRUNCATED,
                                    thresholds=NONZERO_WARNING,
                                ),
                            ),
                        ),
                        5,
                        12,
                    ),
                    sized(
                        bar(
                            "Errors by PSN",
                            "RADIUS error volume by policy service node for the "
                            "currently selected PSN scope.",
                            [
                                instant(
                                    "sort_desc(sum by (psn) "
                                    "("
                                    + gate(
                                        metric(
                                            "ise3_radius_errors_by_psn",
                                            'psn=~"$psn"',
                                        ),
                                        "radius_errors",
                                    )
                                    + "))",
                                    "{{psn}}",
                                )
                            ],
                            data_links=(psn_drilldown,),
                        )
                    ),
                    sized(
                        bar(
                            "Failure locations",
                            "RADIUS error volume joined to authoritative NAD "
                            "location assignments for operational routing.",
                            [
                                instant(
                                    "sort_desc(sum by (location) (("
                                    + error_nads
                                    + ") * on(instance,nad) "
                                    "group_left(location) ("
                                    + assignments
                                    + ")))",
                                    "{{location}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        bar(
                            "Errors by ops owner",
                            "RADIUS error volume joined to the operations group "
                            "responsible for each network device.",
                            [
                                instant(
                                    "sort_desc(sum by (ops_owner) (("
                                    + error_nads
                                    + ") * on(instance,nad) "
                                    "group_left(ops_owner) ("
                                    + assignments
                                    + ")))",
                                    "{{ops_owner}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        tbl(
                            "RADIUS failure work queue",
                            "Per-NAD error evidence enriched with location and "
                            "ops owner so each failure can be routed directly.",
                            [
                                instant(
                                    "sort_desc(("
                                    + error_nads
                                    + ") * on(instance,nad) "
                                    "group_left(location,ops_owner) ("
                                    + assignments
                                    + "))",
                                    "{{nad}} · {{location}} · {{ops_owner}}",
                                )
                            ],
                            columns=("errors",),
                            column_overrides=(
                                by_column("nad", links=(nad_row_drilldown,)),
                            ),
                        )
                    ),
                ),
            ),
            (
                "Authentication dimensions",
                (
                    sized(
                        bar(
                            "Authentication methods",
                            "Complete authentication-method marginal split by "
                            "outcome for the current reporting window.",
                            [
                                instant(
                                    "sort_desc(sum by (method,status) "
                                    f"({gate(metric('ise3_radius_authentications_by_method'), 'radius_reporting')}))",
                                    "{{method}} · {{status}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        bar(
                            "Authentication protocols",
                            "Complete protocol marginal split by outcome without "
                            "a top-K tail that could hide quiet protocols.",
                            [
                                instant(
                                    "sort_desc(sum by (protocol,status) "
                                    f"({gate(metric('ise3_radius_authentications_by_protocol'), 'radius_reporting')}))",
                                    "{{protocol}} · {{status}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        bar(
                            "Authorization policies",
                            "Matched authorization-policy volume by outcome from "
                            "the raw authentication reporting view.",
                            [
                                instant(
                                    "sort_desc(sum by (policy,status) "
                                    f"({gate(metric('ise3_radius_authentications_by_policy'), 'radius_reporting')}))",
                                    "{{policy}} · {{status}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        bar(
                            "Authentication by PSN",
                            "Authentication volume by policy service node and "
                            "outcome, scoped by the selected PSN variable.",
                            [
                                instant(
                                    "sort_desc(sum by (psn,status) "
                                    "("
                                    + gate(
                                        metric(
                                            "ise3_radius_authentications_by_psn",
                                            'psn=~"$psn"',
                                        ),
                                        "radius_reporting",
                                    )
                                    + "))",
                                    "{{psn}} · {{status}}",
                                )
                            ],
                            data_links=(psn_drilldown,),
                        )
                    ),
                    sized(
                        ts(
                            "Authentication rate per PSN",
                            "Rolling-window authentication volume divided by the "
                            "exact represented window, preserving passed and "
                            "failed outcomes per selected PSN.",
                            [
                                query(
                                    gate(
                                        metric(
                                            "ise3_radius_authentications_by_psn",
                                            'psn=~"$psn"',
                                        ),
                                        "radius_reporting",
                                    )
                                    + " / on(instance,provider) group_left "
                                    + metric(
                                        "ise3_radius_reporting_window_seconds"
                                    ),
                                    "{{psn}} · {{status}}",
                                )
                            ],
                            unit="reqps",
                        )
                    ),
                    sized(
                        bar(
                            "Authentication by network device",
                            capped(
                                "Per-NAD authentication volume by outcome.",
                                "The complete sortable per-device list is the "
                                "\"Per-NAD authentication volume\" table beside "
                                "this gauge.",
                            ),
                            [
                                instant(
                                    "sort_desc("
                                    + top(
                                        "sum by (nad,status) "
                                        "("
                                        + gate(
                                            metric(
                                                "ise3_radius_authentications_by_nad",
                                                'nad=~"$nad"',
                                            ),
                                            "radius_reporting",
                                        )
                                        + ")"
                                    )
                                    + ")",
                                    "{{nad}} · {{status}}",
                                )
                            ],
                            data_links=(nad_series_drilldown,),
                        )
                    ),
                    sized(
                        tbl(
                            "Per-NAD authentication volume",
                            "The complete per-device marginal behind the capped "
                            "gauge: passed and failed volume for every selected "
                            "network device in the reporting window, with no "
                            "top-K applied.",
                            [
                                instant(
                                    "sum by (instance,nad,status) ("
                                    + gate(
                                        metric(
                                            "ise3_radius_authentications_by_nad",
                                            'nad=~"$nad"',
                                        ),
                                        "radius_reporting",
                                    )
                                    + ")",
                                    "{{nad}} · {{status}}",
                                )
                            ],
                            columns=("authentications",),
                            sort=("authentications", True),
                            column_overrides=(
                                by_column("nad", links=(nad_row_drilldown,)),
                            ),
                        )
                    ),
                    sized(
                        bar(
                            "Authentication latency by PSN",
                            "Mean trusted authentication latency per PSN beside "
                            "the fraction of samples whose latency was usable.",
                            [
                                instant(
                                    metric(
                                        "ise3_radius_authentication_latency_seconds",
                                        'psn=~"$psn"',
                                    ),
                                    "{{psn}} latency",
                                ),
                                instant(
                                    metric(
                                        "ise3_radius_authentication_latency_coverage",
                                        'psn=~"$psn"',
                                    ),
                                    "{{psn}} coverage",
                                    "B",
                                ),
                            ],
                            unit="s",
                            # Coverage is a fraction, not a latency.
                            overrides=(
                                by_ref(
                                    "B",
                                    unit="percentunit",
                                    thresholds=COVERAGE,
                                    minimum=0,
                                    maximum=1,
                                ),
                            ),
                            data_links=(psn_drilldown,),
                        )
                    ),
                ),
            ),
            (
                "Accounting",
                (
                    sized(
                        stat_panel(
                            "Accounting starts and stops",
                            "Exact accounting start, stop, and other event totals "
                            "for the current reporting window.",
                            [
                                instant(
                                    "sum("
                                    + gate(
                                        metric(
                                            "ise3_radius_accounting_events",
                                            'dimension="total",event_type="starts"',
                                        ),
                                        "radius_accounting",
                                    )
                                    + ")",
                                    "starts",
                                ),
                                instant(
                                    "sum("
                                    + gate(
                                        metric(
                                            "ise3_radius_accounting_events",
                                            'dimension="total",event_type="stops"',
                                        ),
                                        "radius_accounting",
                                    )
                                    + ")",
                                    "stops",
                                    "B",
                                ),
                                instant(
                                    "sum("
                                    + gate(
                                        metric(
                                            "ise3_radius_accounting_events",
                                            'dimension="total",event_type="other"',
                                        ),
                                        "radius_accounting",
                                    )
                                    + ")",
                                    "other",
                                    "C",
                                ),
                            ],
                            no_value=NO_DATA_STALE,
                        ),
                        5,
                        24,
                    ),
                    sized(
                        bar(
                            "Accounting events by network device",
                            capped(
                                "Start and stop accounting volume per selected "
                                "NAD.",
                                "The exact deployment-wide event totals, "
                                "unaffected by this cap, are the \"Accounting "
                                "starts and stops\" panel above.",
                            ),
                            [
                                instant(
                                    "sort_desc("
                                    + top(
                                        "sum by (value,event_type) ("
                                        + gate(
                                            metric(
                                                "ise3_radius_accounting_events",
                                                'dimension="nad",value=~"$nad"',
                                            ),
                                            "radius_accounting",
                                        )
                                        + ")"
                                    )
                                    + ")",
                                    "{{value}} · {{event_type}}",
                                )
                            ],
                            data_links=(nad_value_drilldown,),
                        )
                    ),
                    sized(
                        bar(
                            "Accounting session duration",
                            capped(
                                "Mean and maximum completed-session duration "
                                "per selected NAD, with zero-duration records "
                                "excluded.",
                                "Coverage for the same records is in "
                                "\"Accounting duration coverage\" below.",
                            ),
                            [
                                instant(
                                    "sort_desc("
                                    + top(
                                        gate(
                                            metric(
                                                "ise3_radius_accounting_session_duration_seconds",
                                                'dimension="nad",value=~"$nad"',
                                            ),
                                            "radius_accounting",
                                        )
                                    )
                                    + ")",
                                    "{{value}} · {{statistic}}",
                                )
                            ],
                            unit="s",
                            data_links=(nad_value_drilldown,),
                        )
                    ),
                    sized(
                        ts(
                            "Accounting events by PSN",
                            "Start and stop accounting-window volume over time "
                            "for each selected policy service node.",
                            [
                                query(
                                    gate(
                                        metric(
                                            "ise3_radius_accounting_events",
                                            'dimension="psn",value=~"$psn"',
                                        ),
                                        "radius_accounting",
                                    ),
                                    "{{value}} · {{event_type}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        ts(
                            "Accounting event rate per PSN",
                            "Rolling-window accounting events divided by the exact "
                            "represented window for each selected PSN and event "
                            "type.",
                            [
                                query(
                                    gate(
                                        metric(
                                            "ise3_radius_accounting_events",
                                            'dimension="psn",value=~"$psn"',
                                        ),
                                        "radius_accounting",
                                    )
                                    + " / on(instance,provider) group_left "
                                    + metric(
                                        "ise3_radius_accounting_window_seconds"
                                    ),
                                    "{{value}} · {{event_type}}",
                                )
                            ],
                            unit="eps",
                        )
                    ),
                    sized(
                        bar(
                            "Accounting duration coverage",
                            capped(
                                "Fraction of accounting records with a usable "
                                "positive session duration for each NAD, worst "
                                "coverage first.",
                                "Devices above the cap are the ones already "
                                "reporting usable durations.",
                                lowest=True,
                            ),
                            [
                                instant(
                                    "sort("
                                    + top(
                                        gate(
                                            metric(
                                                "ise3_radius_accounting_duration_coverage",
                                                'dimension="nad",value=~"$nad"',
                                            ),
                                            "radius_accounting",
                                        ),
                                        lowest=True,
                                    )
                                    + ")",
                                    "{{value}}",
                                )
                            ],
                            unit="percentunit",
                            thresholds=COVERAGE,
                            minimum=0,
                            maximum=1,
                            data_links=(nad_value_drilldown,),
                        )
                    ),
                ),
                COLLAPSED,
            ),
            (
                "Live authorization decisions",
                (
                    sized(
                        bar(
                            "Current endpoint status by ops owner",
                            "Distinct active endpoints by authentication status "
                            "and operational owner from MnT session detail.",
                            [
                                instant(
                                    "sort_desc(sum by (ops_owner,status) "
                                    "("
                                    + gate(
                                        metric(
                                            "ise3_session_status_endpoints",
                                            'ops_owner=~"$ops_owner"',
                                        ),
                                        "session_authorization",
                                    )
                                    + "))",
                                    "{{ops_owner}} · {{status}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        bar(
                            "Failure reasons by ops owner",
                            "Distinct active endpoints by ISE failure reason code "
                            "and owning operations group. The reason code is the "
                            "leading code of the session's failure reason, which "
                            "shares ISE's message-code namespace."
                            + NAMED_CODES_NOTE,
                            [
                                instant(
                                    "sort_desc("
                                    + named_codes(
                                        "sum by (reason_code,ops_owner) "
                                        "("
                                        + gate(
                                            metric(
                                                "ise3_session_failure_reason_endpoints",
                                                'ops_owner=~"$ops_owner"',
                                            ),
                                            "session_authorization",
                                        )
                                        + ")",
                                        label="reason_code",
                                    )
                                    + ")",
                                    "{{reason_code}} · {{ops_owner}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        tbl(
                            "Failed authorization and policy context",
                            "Selected profile, matched rule, and matched policy "
                            "set retained only for failed active endpoints and "
                            "grouped by the responsible operations owner.",
                            [
                                instant(
                                    f"sort_desc({failed_context})",
                                    "{{ops_owner}} · {{authz_profile}} "
                                    "{{authz_rule}} {{policy_set}}",
                                ),
                            ],
                            columns=("failed endpoints",),
                        )
                    ),
                    sized(
                        bar(
                            "Live authentication methods",
                            "Methods currently used by distinct active endpoints, "
                            "grouped by the owner responsible for their NAD.",
                            [
                                instant(
                                    "sort_desc(sum by (method,ops_owner) "
                                    "("
                                    + gate(
                                        metric(
                                            "ise3_session_auth_method_endpoints",
                                            'ops_owner=~"$ops_owner"',
                                        ),
                                        "session_authorization",
                                    )
                                    + "))",
                                    "{{method}} · {{ops_owner}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        bar(
                            "Selected authorization profiles",
                            "Authorization profiles selected for active endpoints, "
                            "grouped by operational owner.",
                            [
                                instant(
                                    "sort_desc(sum by (authz_profile,ops_owner) "
                                    "("
                                    + gate(
                                        metric(
                                            "ise3_session_authz_profile_endpoints",
                                            'ops_owner=~"$ops_owner"',
                                        ),
                                        "session_authorization",
                                    )
                                    + "))",
                                    "{{authz_profile}} · {{ops_owner}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        bar(
                            "Matched authorization rules",
                            "Ground-truth matched rules for active sessions, used "
                            "to distinguish open-mode from closed-mode behavior.",
                            [
                                instant(
                                    "sort_desc(sum by (authz_rule,ops_owner) "
                                    "("
                                    + gate(
                                        metric(
                                            "ise3_session_authz_rule_endpoints",
                                            'ops_owner=~"$ops_owner"',
                                        ),
                                        "session_authorization",
                                    )
                                    + "))",
                                    "{{authz_rule}} · {{ops_owner}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        bar(
                            "Matched policy sets",
                            "Policy sets matched by active endpoints per operations "
                            "owner, parsed from the live authorization decision.",
                            [
                                instant(
                                    "sort_desc(sum by (policy_set,ops_owner) "
                                    "("
                                    + gate(
                                        metric(
                                            "ise3_session_policy_set_endpoints",
                                            'ops_owner=~"$ops_owner"',
                                        ),
                                        "session_authorization",
                                    )
                                    + "))",
                                    "{{policy_set}} · {{ops_owner}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        tbl(
                            "Policy sets by network device",
                            "Per-switch policy-set evidence for finding NADs still "
                            "operating under an unintended access mode.",
                            [
                                instant(
                                    "sort_desc("
                                    + gate(
                                        metric(
                                            "ise3_session_policy_set_endpoints_by_nad",
                                            'nad=~"$nad"',
                                        ),
                                        "session_authorization",
                                    )
                                    + ")",
                                    "{{nad}} · {{policy_set}}",
                                )
                            ],
                            columns=("endpoints",),
                        )
                    ),
                    sized(
                        stat_panel(
                            "Policy-set NAD coverage",
                            "Published versus available NAD groups for the bounded "
                            "per-policy-set switch breakdown.",
                            [
                                instant(
                                    metric(
                                        "ise3_topk_groups_returned",
                                        'dataset="session_authorization",breakdown=~".*nad.*"',
                                    ),
                                    "published",
                                ),
                                instant(
                                    metric(
                                        "ise3_topk_groups_total",
                                        'dataset="session_authorization",breakdown=~".*nad.*"',
                                    ),
                                    "total",
                                    "B",
                                ),
                            ],
                        )
                    ),
                ),
                COLLAPSED,
            ),
        ),
        variables=(psn, nad, owner),
    )


def endpoints_dashboard():
    nad = label_variable(
        "nad", "Network device", "ise3_network_device_assignment", "nad"
    )
    return assemble(
        "ISE 3 — Endpoints and Network Devices",
        "ise3-endpoints",
        "Endpoint inventory and attributes alongside authoritative NAD "
        "classification, detail coverage, and authentication activity.",
        (
            (
                "Endpoint inventory",
                (
                    sized(
                        stat_panel(
                            "Endpoint inventory readiness",
                            "Availability and freshness of inventory, profiling "
                            "events, and optional live endpoint attributes. NOT "
                            "READY means the panels fed by that dataset stay blank.",
                            [
                                instant(
                                    ready_bool("endpoint_inventory"), "inventory"
                                ),
                                instant(
                                    ready_bool("endpoint_attributes"),
                                    "live attributes",
                                    "B",
                                ),
                                instant(
                                    ready_bool("profile_events"),
                                    "profile events",
                                    "C",
                                ),
                            ],
                            mappings=READINESS,
                            no_value=NO_DATA_EXPORTER,
                        ),
                        5,
                        8,
                    ),
                    sized(
                        stat_panel(
                            "Dataset collection age",
                            "Seconds since the latest successful endpoint inventory "
                            "and profiling-event snapshots.",
                            [
                                instant(
                                    "time() - "
                                    + metric(
                                        "ise3_dataset_last_success_timestamp",
                                        'dataset="endpoint_inventory"',
                                    ),
                                    "inventory",
                                ),
                                instant(
                                    "time() - "
                                    + metric(
                                        "ise3_dataset_last_success_timestamp",
                                        'dataset="profile_events"',
                                    ),
                                    "profile events",
                                    "B",
                                ),
                            ],
                            unit="s",
                        ),
                        5,
                        8,
                    ),
                    sized(
                        stat_panel(
                            "Endpoints",
                            "Authoritative endpoint database total from the active "
                            "inventory provider.",
                            [
                                instant(
                                    f"sum({gate(metric('ise3_endpoints_total'), 'endpoint_inventory')})",
                                    "endpoints",
                                )
                            ],
                            no_value=NO_DATA_STALE,
                        ),
                        5,
                        8,
                    ),
                    sized(
                        stat_panel(
                            "Unprofiled and posture-applicable",
                            "Endpoint counts needing profile attention and those "
                            "eligible for posture assessment.",
                            [
                                instant(
                                    f"sum({gate(metric('ise3_endpoints_unprofiled'), 'endpoint_inventory')})",
                                    "unprofiled",
                                ),
                                instant(
                                    "sum("
                                    + gate(
                                        metric(
                                            "ise3_endpoints_posture_applicable",
                                            'applicable="true"',
                                        ),
                                        "endpoint_inventory",
                                    )
                                    + ")",
                                    "posture applicable",
                                    "B",
                                ),
                            ],
                            no_value=NO_DATA_STALE,
                        ),
                        5,
                        8,
                    ),
                    sized(
                        stat_panel(
                            "Populated profiles and identity groups",
                            "Number of non-empty endpoint profile and identity-group "
                            "categories currently represented in inventory.",
                            [
                                instant(
                                    "count("
                                    + gate(
                                        metric("ise3_endpoints_by_profile"),
                                        "endpoint_inventory",
                                    )
                                    + " > 0)",
                                    "profiles",
                                ),
                                instant(
                                    "count("
                                    + gate(
                                        metric(
                                            "ise3_endpoints_by_identity_group"
                                        ),
                                        "endpoint_inventory",
                                    )
                                    + " > 0)",
                                    "identity groups",
                                    "B",
                                ),
                            ],
                            no_value=NO_DATA_STALE,
                        ),
                        5,
                        8,
                    ),
                    sized(
                        bar(
                            "Endpoints by profile",
                            "Complete endpoint-policy distribution from the "
                            "authoritative endpoint inventory.",
                            [
                                instant(
                                    "sort_desc(sum by (profile) "
                                    f"({gate(metric('ise3_endpoints_by_profile'), 'endpoint_inventory')}))",
                                    "{{profile}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        bar(
                            "Endpoints by identity group",
                            "Complete identity-group distribution for endpoints "
                            "currently stored in ISE.",
                            [
                                instant(
                                    "sort_desc(sum by (identity_group) "
                                    f"({gate(metric('ise3_endpoints_by_identity_group'), 'endpoint_inventory')}))",
                                    "{{identity_group}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        bar(
                            "Endpoint operational field coverage",
                            "Fraction of endpoint inventory rows carrying profile, "
                            "identity-group, and posture-applicability fields.",
                            [
                                instant(
                                    "sort_desc("
                                    + gate(
                                        metric(
                                            "ise3_endpoint_inventory_field_coverage"
                                        ),
                                        "endpoint_inventory",
                                    )
                                    + ")",
                                    "{{field}}",
                                )
                            ],
                            unit="percentunit",
                            thresholds=COVERAGE,
                            minimum=0,
                            maximum=1,
                        )
                    ),
                    sized(
                        bar(
                            "Profile events by source and action",
                            "Historical endpoint profiling changes grouped by the "
                            "source that classified them and the resulting action.",
                            [
                                instant(
                                    "sort_desc(sum by (source,action) ("
                                    + gate(
                                        metric(
                                            "ise3_endpoint_profile_events"
                                        ),
                                        "profile_events",
                                    )
                                    + "))",
                                    "{{source}} · {{action}}",
                                )
                            ],
                        )
                    ),
                ),
            ),
            (
                "Live endpoint attributes",
                (
                    sized(
                        stat_panel(
                            "Attribute-source coverage",
                            "Endpoints for which ISE publishes live model, OS, and "
                            "MDM context; this source is intentionally partial.",
                            [
                                instant(
                                    metric("ise3_endpoint_attributes_published"),
                                    "{{provider}}",
                                )
                            ],
                        ),
                        5,
                        24,
                    ),
                    sized(
                        bar(
                            "Endpoints by hardware model",
                            "Manufacturer and model distribution for endpoints "
                            "visible to the live attribute source.",
                            [
                                instant(
                                    "sort_desc(sum by (manufacturer,model) "
                                    f"({gate(metric('ise3_endpoint_model'), 'endpoint_attributes')}))",
                                    "{{manufacturer}} · {{model}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        bar(
                            "Endpoints by operating system",
                            "Operating-system distribution for endpoints visible "
                            "to the live attribute source.",
                            [
                                instant(
                                    "sort_desc(sum by (os) "
                                    f"({gate(metric('ise3_endpoint_operating_system'), 'endpoint_attributes')}))",
                                    "{{os}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        bar(
                            "MDM registration",
                            "Registration-state distribution from endpoint MDM "
                            "context published by ISE.",
                            [
                                instant(
                                    "sort_desc(sum by (registered) "
                                    f"({gate(metric('ise3_endpoint_mdm_registered'), 'endpoint_attributes')}))",
                                    "{{registered}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        bar(
                            "MDM compliance",
                            "Compliance-state distribution from endpoint MDM "
                            "context published by ISE.",
                            [
                                instant(
                                    "sort_desc(sum by (compliant) "
                                    f"({gate(metric('ise3_endpoint_mdm_compliant'), 'endpoint_attributes')}))",
                                    "{{compliant}}",
                                )
                            ],
                        )
                    ),
                ),
            ),
            (
                "Network device inventory",
                (
                    sized(
                        stat_panel(
                            "Network devices and classification",
                            "Configured NAD total beside the number whose group "
                            "detail has converged into the local classification.",
                            [
                                instant(
                                    metric("ise3_network_devices_total"),
                                    "total",
                                ),
                                instant(
                                    metric("ise3_network_devices_classified"),
                                    "classified",
                                    "B",
                                ),
                            ],
                        ),
                        5,
                        24,
                    ),
                    sized(
                        bar(
                            "Devices by location",
                            "Configured network devices grouped by normalized ISE "
                            "location hierarchy.",
                            [
                                instant(
                                    "sort_desc(sum by (location) "
                                    f"({gate(metric('ise3_network_devices_by_location'), 'network_devices')}))",
                                    "{{location}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        bar(
                            "Devices by ops owner",
                            "Configured network devices grouped by the operational "
                            "owner derived from Network Device Groups.",
                            [
                                instant(
                                    "sort_desc(sum by (ops_owner) "
                                    f"({gate(metric('ise3_network_devices_by_ops_owner'), 'network_devices')}))",
                                    "{{ops_owner}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        bar(
                            "Devices by type",
                            "Configured network devices grouped by normalized "
                            "device-type membership.",
                            [
                                instant(
                                    "sort_desc(sum by (device_type) "
                                    f"({gate(metric('ise3_network_devices_by_type'), 'network_devices')}))",
                                    "{{device_type}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        tbl(
                            "Network device assignments",
                            "Per-NAD location, operations owner, and device type "
                            "for inventory and session correlation.",
                            [
                                instant(
                                    metric(
                                        "ise3_network_device_assignment",
                                        'nad=~"$nad"',
                                    ),
                                    "{{nad}} · {{location}} · {{ops_owner}}",
                                )
                            ],
                            columns=(None,),
                        )
                    ),
                    sized(
                        ts(
                            "NAD detail-cache coverage",
                            "Warm-up coverage, deferred detail reads, and cache "
                            "entries for network-device classification.",
                            [
                                query(
                                    metric(
                                        "ise3_detail_cache_coverage",
                                        'cache="ers_network_device"',
                                    ),
                                    "coverage",
                                ),
                                query(
                                    metric(
                                        "ise3_detail_fetches_deferred",
                                        'cache="ers_network_device"',
                                    ),
                                    "deferred",
                                    ref="B",
                                ),
                                query(
                                    metric(
                                        "ise3_detail_cache_entries",
                                        'cache="ers_network_device"',
                                    ),
                                    "entries",
                                    ref="C",
                                ),
                            ],
                        )
                    ),
                ),
            ),
            (
                "NAD activity",
                (
                    sized(
                        stat_panel(
                            "Silent and covered NADs",
                            "Configured network devices with no recent activity "
                            "beside the inventory surface reached by the scan.",
                            [
                                instant(metric("ise3_nad_silent_total"), "silent"),
                                instant(
                                    metric("ise3_nad_activity_covered"),
                                    "covered",
                                    "B",
                                ),
                            ],
                            thresholds=NONZERO_WARNING,
                            # Covered NADs are inventory reach, not a finding.
                            overrides=(by_ref("B", thresholds=NEUTRAL),),
                        ),
                        5,
                        24,
                    ),
                    sized(
                        tbl(
                            "Stalest network devices",
                            "Seconds since each selected NAD last authenticated "
                            "anything in the reporting scan window.",
                            [
                                instant(
                                    "sort_desc("
                                    + gate(
                                        metric(
                                            "ise3_nad_last_authentication_age_seconds",
                                            'nad=~"$nad"',
                                        ),
                                        "nad_health",
                                    )
                                    + ")",
                                    "{{nad}}",
                                )
                            ],
                            columns=("age s",),
                            column_overrides=(by_column("age s", **SECONDS_CELL),),
                        )
                    ),
                    sized(
                        tbl(
                            "Never-authenticated network devices",
                            "Configured NADs with zero accumulated authentication "
                            "activity in the current scan window.",
                            [
                                instant(
                                    "sum by (instance,nad) ("
                                    + gate(
                                        metric(
                                            "ise3_nad_authentications",
                                            'nad=~"$nad"',
                                        ),
                                        "nad_health",
                                    )
                                    + ") == 0",
                                    "{{nad}}",
                                )
                            ],
                            columns=("authentications",),
                        )
                    ),
                    sized(
                        bar(
                            "Per-NAD authentication activity (top 10)",
                            "Ten busiest selected network devices by passed and "
                            "failed authentication volume in the scan window.",
                            [
                                instant(
                                    "topk(10, sum by (nad,status) "
                                    "("
                                    + gate(
                                        metric(
                                            "ise3_nad_authentications",
                                            'nad=~"$nad"',
                                        ),
                                        "nad_health",
                                    )
                                    + "))",
                                    "{{nad}} · {{status}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        stat_panel(
                            "Session attribution coverage",
                            "Session-bearing NADs matched and unmatched against "
                            "the authoritative network-device directory. Unmatched "
                            "devices carry sessions but no location or ops owner.",
                            [
                                instant(
                                    metric(
                                        "ise3_nad_directory_attributed",
                                        'matched="yes"',
                                    ),
                                    "matched",
                                ),
                                instant(
                                    metric(
                                        "ise3_nad_directory_attributed",
                                        'matched="no"',
                                    ),
                                    "unmatched",
                                    "B",
                                ),
                            ],
                            # Unmatched NADs are a routing gap, matched ones are not.
                            overrides=(by_ref("B", thresholds=NONZERO_WARNING),),
                        ),
                        5,
                        12,
                    ),
                    sized(
                        stat_panel(
                            "NAD directory entries",
                            "Current number of NAD identities resolvable to a "
                            "normalized location and operational owner.",
                            [
                                instant(
                                    metric("ise3_nad_directory_entries"),
                                    "entries",
                                )
                            ],
                        ),
                        5,
                        12,
                    ),
                ),
            ),
        ),
        variables=(nad,),
    )


def health_dashboard():
    dataset = label_variable(
        "dataset", "Dataset", "ise3_dataset_enabled", "dataset"
    )
    return assemble(
        "ISE Exporter 3 — Health",
        "ise3-health",
        "Collection readiness, failure detail, transport health, Data Connect "
        "cost, bounded-breakdown coverage, caches, and exporter identity.",
        (
            (
                "Dataset readiness",
                (
                    sized(
                        tbl(
                            "Dataset collection status",
                            "Enabled, active-provider, up, fresh, and next-run "
                            "state for every selected dataset.",
                            [
                                instant(
                                    # ise3_dataset_enabled carries no provider
                                    # label, and merge attaches an unbroadcast
                                    # value only to the first provider row of
                                    # each dataset; multiplying it onto the
                                    # per-provider active series gives every
                                    # provider row its enabled cell.
                                    "("
                                    + metric(
                                        "ise3_dataset_provider_active",
                                        'dataset=~"$dataset"',
                                    )
                                    + " * 0) + on(instance, dataset) group_left() "
                                    + metric(
                                        "ise3_dataset_enabled",
                                        'dataset=~"$dataset"',
                                    ),
                                    "{{dataset}} enabled",
                                ),
                                instant(
                                    metric(
                                        "ise3_dataset_provider_active",
                                        'dataset=~"$dataset"',
                                    ),
                                    "{{dataset}} · {{provider}} active",
                                    "B",
                                ),
                                instant(
                                    metric(
                                        "ise3_dataset_up",
                                        'dataset=~"$dataset"',
                                    ),
                                    "{{dataset}} · {{provider}} up",
                                    "C",
                                ),
                                instant(
                                    metric(
                                        "ise3_dataset_fresh",
                                        'dataset=~"$dataset"',
                                    ),
                                    "{{dataset}} · {{provider}} fresh",
                                    "D",
                                ),
                            ],
                            columns=("enabled", "active", "up", "fresh"),
                            column_overrides=(
                                by_column("enabled", **BOOLEAN_CELL),
                                by_column("active", **BOOLEAN_CELL),
                                by_column("up", **BOOLEAN_CELL),
                                by_column("fresh", **BOOLEAN_CELL),
                            ),
                        )
                    ),
                    sized(
                        tbl(
                            "Latest dataset failure",
                            "One bounded reason and operator explanation for each "
                            "currently failing dataset.",
                            [
                                instant(
                                    metric(
                                        "ise3_dataset_last_failure_detail_info",
                                        'dataset=~"$dataset"',
                                    ),
                                    "{{dataset}} · {{provider}} · {{reason}} · {{detail}}",
                                )
                            ],
                            columns=(None,),
                        )
                    ),
                    sized(
                        ts(
                            "Collection attempt and success age",
                            "Time since each dataset last attempted collection "
                            "and last completed successfully.",
                            [
                                query(
                                    "time() - "
                                    + metric(
                                        "ise3_dataset_last_attempt_timestamp",
                                        'dataset=~"$dataset"',
                                    ),
                                    "{{dataset}} attempt",
                                ),
                                query(
                                    "time() - "
                                    + metric(
                                        "ise3_dataset_last_success_timestamp",
                                        'dataset=~"$dataset"',
                                    ),
                                    "{{dataset}} success",
                                    ref="B",
                                ),
                            ],
                            unit="s",
                        )
                    ),
                    sized(
                        ts(
                            "Collection duration",
                            "Latest collection wall time by dataset and provider, "
                            "used to find cadences the collector cannot sustain.",
                            [
                                query(
                                    metric(
                                        "ise3_dataset_collection_duration_seconds",
                                        'dataset=~"$dataset"',
                                    ),
                                    "{{dataset}} · {{provider}}",
                                )
                            ],
                            unit="s",
                        )
                    ),
                    sized(
                        ts(
                            "Consecutive and cumulative failures",
                            "Current consecutive failures beside the rate of "
                            "failures by bounded reason.",
                            [
                                query(
                                    metric(
                                        "ise3_dataset_consecutive_failures",
                                        'dataset=~"$dataset"',
                                    ),
                                    "{{dataset}} consecutive",
                                ),
                                query(
                                    "sum by (dataset,reason) (rate("
                                    + metric(
                                        "ise3_dataset_failures_total",
                                        'dataset=~"$dataset"',
                                    )
                                    + "[15m]))",
                                    "{{dataset}} · {{reason}} rate",
                                    ref="B",
                                ),
                            ],
                            thresholds=NONZERO_CRITICAL,
                        )
                    ),
                    sized(
                        tbl(
                            "Provider availability and fallback reason",
                            "Declared providers that cannot run and the bounded "
                            "reason a lower-preference source was selected.",
                            [
                                instant(
                                    metric(
                                        "ise3_dataset_provider_available",
                                        'dataset=~"$dataset"',
                                    ),
                                    "{{dataset}} · {{provider}}",
                                ),
                                instant(
                                    metric(
                                        "ise3_dataset_provider_reason_info",
                                        'dataset=~"$dataset"',
                                    ),
                                    "{{dataset}} · {{provider}} · {{reason}}",
                                    "B",
                                ),
                            ],
                            columns=("available", None),
                            column_overrides=(by_column("available", **BOOLEAN_CELL),),
                        )
                    ),
                ),
            ),
            (
                "Reporting and transport",
                (
                    sized(
                        tbl(
                            "Data Connect source freshness",
                            "Whether each reporting view's probe answered, "
                            "whether the view has recent rows, and the age of "
                            "its newest row. Probed zero means the statement "
                            "carrying that view failed, so its other cells are "
                            "unknown rather than empty. endpoints_data is "
                            "judged against Cisco's documented 12h attribute "
                            "sync, so an age inside that horizon still counts "
                            "as recent there.",
                            [
                                instant(
                                    metric("ise3_source_probed"),
                                    "{{view}} probed",
                                ),
                                instant(
                                    metric("ise3_source_has_recent_rows"),
                                    "{{view}} recent",
                                    "B",
                                ),
                                instant(
                                    metric("ise3_source_latest_row_age_seconds"),
                                    "{{view}} age",
                                    "C",
                                ),
                            ],
                            columns=("probed", "recent", "age s"),
                            sort=("age s", True),
                            column_overrides=(
                                by_column("probed", **BOOLEAN_CELL),
                                by_column("recent", **BOOLEAN_CELL),
                                by_column("age s", **SECONDS_CELL),
                            ),
                        )
                    ),
                    sized(
                        tbl(
                            "Data Connect schema capability gaps",
                            "Missing required columns are dataset blockers; missing "
                            "optional columns identify the exact dashboard "
                            "breakdowns this ISE release or account cannot supply.",
                            [
                                # A union, not two targets: a missing column and
                                # a missing view are different rows of one gap
                                # list, and joining them on view would attach a
                                # missing view to an unrelated column.
                                instant(
                                    "(1 - "
                                    + metric(
                                        "ise3_dataconnect_schema_column_available"
                                    )
                                    + ") > 0 or (1 - "
                                    + metric(
                                        "ise3_dataconnect_schema_view_available"
                                    )
                                    + ") > 0",
                                    "{{requirement}} · {{view}}.{{column}}",
                                ),
                            ],
                            columns=(None,),
                        )
                    ),
                    sized(
                        ts(
                            "Data Connect statements",
                            "Statement execution rate by reporting view and "
                            "result, exposing failing or unexpectedly busy views.",
                            [
                                query(
                                    "sum by (view,result) (rate("
                                    + metric("ise3_dataconnect_queries_total")
                                    + "[15m]))",
                                    "{{view}} · {{result}}",
                                )
                            ],
                            unit="ops",
                        )
                    ),
                    sized(
                        bar(
                            "Latest Data Connect duration",
                            "Most recent statement duration by reporting view and "
                            "result, sorted by cost.",
                            [
                                instant(
                                    "sort_desc("
                                    + metric(
                                        "ise3_dataconnect_query_last_duration_seconds"
                                    )
                                    + ")",
                                    "{{view}} · {{result}}",
                                )
                            ],
                            unit="s",
                        )
                    ),
                    sized(
                        bar(
                            "Latest rows returned",
                            "Rows returned by each reporting view's most recent "
                            "statement, compared with configured safety limits.",
                            [
                                instant(
                                    "sort_desc("
                                    + metric("ise3_dataconnect_query_rows")
                                    + ")",
                                    "{{view}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        ts(
                            "Data Connect cooldown",
                            "Global cooldown imposed after each view completes; "
                            "one slow view delays every reporting dataset.",
                            [
                                query(
                                    metric(
                                        "ise3_dataconnect_query_cooldown_seconds"
                                    ),
                                    "{{view}}",
                                )
                            ],
                            unit="s",
                        )
                    ),
                    sized(
                        tbl(
                            "pxGrid session stream",
                            "Persistent STOMP/WebSocket health, reconciled "
                            "session state, last-frame age, and successful "
                            "connection count. A connected value of one means "
                            "the stream also completed its getSessions baseline.",
                            [
                                instant(
                                    metric("ise3_pxgrid_stream_connected"),
                                    "connected",
                                ),
                                instant(
                                    metric("ise3_pxgrid_stream_sessions"),
                                    "sessions",
                                    "B",
                                ),
                                instant(
                                    "time() - "
                                    + metric(
                                        "ise3_pxgrid_stream_last_frame_timestamp"
                                    ),
                                    "last frame age",
                                    "C",
                                ),
                                instant(
                                    metric(
                                        "ise3_pxgrid_stream_reconnects_total"
                                    ),
                                    "connections",
                                    "D",
                                ),
                            ],
                            columns=(
                                "connected", "sessions", "last frame age s", "connections",
                            ),
                            column_overrides=(
                                by_column("connected", **BOOLEAN_CELL),
                                by_column("last frame age s", **SECONDS_CELL),
                            ),
                        )
                    ),
                    sized(
                        ts(
                            "API request rate",
                            "Outbound requests by ISE target, API surface, and "
                            "outcome after all rate limiting.",
                            [
                                query(
                                    "sum by (target,api,status) (rate("
                                    + metric("ise3_api_requests_total")
                                    + "[5m]))",
                                    "{{target}} · {{api}} · {{status}}",
                                )
                            ],
                            unit="reqps",
                        )
                    ),
                    sized(
                        ts(
                            "API errors",
                            "Outbound API error rate by target, API, bounded error "
                            "type, and HTTP status code.",
                            [
                                query(
                                    "sum by (target,api,error_type,http_code) (rate("
                                    + metric("ise3_api_errors_total")
                                    + "[5m]))",
                                    "{{target}} · {{api}} · {{error_type}} · {{http_code}}",
                                )
                            ],
                            unit="reqps",
                        )
                    ),
                    sized(
                        ts(
                            "API latency p95",
                            "Ninety-fifth percentile outbound request duration by "
                            "target and API surface.",
                            [
                                query(
                                    "histogram_quantile(0.95, sum by "
                                    "(le,target,api) (rate("
                                    + metric(
                                        "ise3_api_request_duration_seconds_bucket"
                                    )
                                    + "[5m])))",
                                    "{{target}} · {{api}}",
                                )
                            ],
                            unit="s",
                        )
                    ),
                ),
            ),
            (
                "Scheduler, budget, and bounds",
                (
                    sized(
                        ts(
                            "Lane activity and queue depth",
                            "Serialized target lanes currently busy and datasets "
                            "waiting behind each lane.",
                            [
                                query(metric("ise3_lane_busy"), "{{target}} busy"),
                                query(
                                    metric("ise3_lane_queue_depth"),
                                    "{{target}} queued",
                                    ref="B",
                                ),
                            ],
                        )
                    ),
                    sized(
                        ts(
                            "Budget enforcement",
                            "Enforced request rate, warm-up state, and cumulative "
                            "time requests spent waiting for budget.",
                            [
                                query(
                                    metric(
                                        "ise3_budget_enforced_requests_per_hour"
                                    ),
                                    "{{target}} enforced",
                                ),
                                query(
                                    metric("ise3_budget_warming"),
                                    "{{target}} warming",
                                    ref="B",
                                ),
                                query(
                                    metric("ise3_budget_wait_seconds_total"),
                                    "{{target}} wait",
                                    ref="C",
                                ),
                            ],
                        )
                    ),
                    sized(
                        ts(
                            "Budget throttling rate",
                            "Requests delayed by budget enforcement, grouped by "
                            "target and API surface.",
                            [
                                query(
                                    "sum by (target,api) (rate("
                                    + metric("ise3_budget_throttled_total")
                                    + "[15m]))",
                                    "{{target}} · {{api}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        ts(
                            "Bounded breakdown coverage",
                            "Published groups divided by groups that existed for "
                            "every explicitly bounded breakdown.",
                            [
                                query(
                                    metric("ise3_topk_groups_returned")
                                    + " / clamp_min("
                                    + metric("ise3_topk_groups_total")
                                    + ", 1)",
                                    "{{dataset}} · {{breakdown}}",
                                )
                            ],
                            unit="percentunit",
                            thresholds=COVERAGE,
                            minimum=0,
                            maximum=1,
                        )
                    ),
                    sized(
                        bar(
                            "Dataset series",
                            "Series published by each dataset in its latest "
                            "snapshot, exposing cardinality growth.",
                            [
                                instant(
                                    "sort_desc("
                                    + metric(
                                        "ise3_dataset_series",
                                        'dataset=~"$dataset"',
                                    )
                                    + ")",
                                    "{{dataset}}",
                                )
                            ],
                        )
                    ),
                ),
            ),
            (
                "Caches and identity",
                (
                    sized(
                        ts(
                            "Detail-cache coverage",
                            "Coverage and deferred work for each converging "
                            "per-entity detail cache.",
                            [
                                query(
                                    metric("ise3_detail_cache_coverage"),
                                    "{{cache}} coverage",
                                ),
                                query(
                                    metric("ise3_detail_fetches_deferred"),
                                    "{{cache}} deferred",
                                    ref="B",
                                ),
                            ],
                        )
                    ),
                    sized(
                        ts(
                            "Detail-fetch results",
                            "Per-entity detail fetch rate by cache and bounded "
                            "result classification.",
                            [
                                query(
                                    "sum by (cache,result) (rate("
                                    + metric("ise3_detail_fetches_total")
                                    + "[15m]))",
                                    "{{cache}} · {{result}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        stat_panel(
                            "Posture assessment eligibility backlog",
                            "Endpoints subject to posture policy beside those "
                            "recently assessed and those still lacking recent "
                            "assessment evidence.",
                            [
                                instant(
                                    metric(
                                        "ise3_posture_eligible_endpoints_total"
                                    ),
                                    "eligible",
                                ),
                                instant(
                                    metric(
                                        "ise3_posture_eligible_recently_assessed_total"
                                    ),
                                    "recently assessed",
                                    "B",
                                ),
                                instant(
                                    metric(
                                        "ise3_posture_eligible_without_recent_assessment_total"
                                    ),
                                    "without recent assessment",
                                    "C",
                                ),
                            ],
                            overrides=(by_ref("C", thresholds=NONZERO_WARNING),),
                        )
                    ),
                    sized(
                        stat_panel(
                            "Exporter state footprint",
                            "Bytes of persistent local exporter state; sustained "
                            "growth means history is accumulating in the wrong place.",
                            [
                                instant(
                                    metric("ise3_exporter_state_bytes"),
                                    "{{instance}}",
                                )
                            ],
                            unit="bytes",
                        ),
                        5,
                        12,
                    ),
                    sized(
                        tbl(
                            "Exporter build identity",
                            "Running exporter version and supported ISE release "
                            "target for every selected deployment.",
                            [
                                instant(
                                    metric("ise3_exporter_build_info"),
                                    "{{version}} · ISE {{target_ise_release}}",
                                )
                            ],
                            columns=(None,),
                        ),
                        5,
                        12,
                    ),
                ),
            ),
        ),
        variables=(dataset,),
        refresh="1m",
    )


def psn_dashboard():
    node = label_variable(
        "node", "PSN", "ise3_psn_radius_requests_per_hour", "node"
    )
    return assemble(
        "ISE 3 — PSN Troubleshooting",
        "ise3-psn",
        "Per-PSN sessions, authentication volume, errors, throughput, latency, "
        "resource utilisation, and reporting health.",
        (
            (
                "PSN service",
                (
                    sized(
                        stat_panel(
                            "Dataset readiness",
                            "Freshness of deployment, performance, session, "
                            "reporting, accounting, and error datasets used here. "
                            "NOT READY means the panels fed by that dataset stay "
                            "blank.",
                            [
                                instant(ready_bool("deployment"), "deployment"),
                                instant(
                                    ready_bool("psn_performance"), "performance", "B"
                                ),
                                instant(
                                    ready_bool("active_sessions"), "sessions", "C"
                                ),
                                instant(
                                    ready_bool("radius_reporting"), "RADIUS", "D"
                                ),
                                instant(ready_bool("radius_errors"), "errors", "E"),
                                instant(
                                    ready_bool("radius_accounting"),
                                    "accounting",
                                    "F",
                                ),
                            ],
                            mappings=READINESS,
                            no_value=NO_DATA_EXPORTER,
                        ),
                        5,
                        24,
                    ),
                    sized(
                        stat_panel(
                            "Dataset collection age",
                            "Seconds since the latest successful performance, "
                            "session, RADIUS, and deployment snapshots used here.",
                            [
                                instant(
                                    "time() - "
                                    + metric(
                                        "ise3_dataset_last_success_timestamp",
                                        'dataset="psn_performance"',
                                    ),
                                    "performance",
                                ),
                                instant(
                                    "time() - "
                                    + metric(
                                        "ise3_dataset_last_success_timestamp",
                                        'dataset="active_sessions"',
                                    ),
                                    "sessions",
                                    "B",
                                ),
                                instant(
                                    "time() - "
                                    + metric(
                                        "ise3_dataset_last_success_timestamp",
                                        'dataset="radius_reporting"',
                                    ),
                                    "RADIUS",
                                    "C",
                                ),
                                instant(
                                    "time() - "
                                    + metric(
                                        "ise3_dataset_last_success_timestamp",
                                        'dataset="deployment"',
                                    ),
                                    "deployment",
                                    "D",
                                ),
                            ],
                            unit="s",
                        ),
                        5,
                        24,
                    ),
                    sized(
                        stat_panel(
                            "Reporting nodes and diagnostic events",
                            "PSNs represented in performance reporting beside "
                            "AAA and system diagnostic events in the bounded "
                            "reporting window.",
                            [
                                instant(
                                    "count("
                                    + metric(
                                        "ise3_psn_radius_requests_per_hour",
                                        'node=~"$node"',
                                    )
                                    + ")",
                                    "reporting nodes",
                                ),
                                instant(
                                    "sum("
                                    + metric(
                                        "ise3_psn_diagnostic_events_total"
                                    )
                                    + ")",
                                    "diagnostic events",
                                    "B",
                                ),
                            ],
                        ),
                        5,
                        12,
                    ),
                    sized(
                        stat_panel(
                            "PSN schema compatibility",
                            "Missing required columns in core PSN reporting views "
                            "and in the optional AAA/system diagnostic views. Core "
                            "must be zero; diagnostic gaps explain reduced work-queue "
                            "coverage without failing performance collection.",
                            [
                                instant(
                                    "sum(1 - "
                                    + metric(
                                        "ise3_dataconnect_schema_column_available",
                                        'view=~"KEY_PERFORMANCE_METRICS|SYSTEM_SUMMARY",'
                                        'requirement="required"',
                                    )
                                    + ")",
                                    "core gaps",
                                ),
                                instant(
                                    "sum(1 - "
                                    + metric(
                                        "ise3_dataconnect_schema_column_available",
                                        'view=~"AAA_DIAGNOSTICS_VIEW|'
                                        'SYSTEM_DIAGNOSTICS_VIEW",'
                                        'requirement="required"',
                                    )
                                    + ")",
                                    "diagnostic gaps",
                                    "B",
                                ),
                            ],
                            thresholds=NONZERO_CRITICAL,
                            # A diagnostic gap narrows coverage, it does not
                            # fail performance collection.
                            overrides=(by_ref("B", thresholds=NONZERO_WARNING),),
                        ),
                        5,
                        12,
                    ),
                    sized(
                        ts(
                            "Active sessions per PSN",
                            "Current active-session count by serving policy "
                            "service node from sources that carry a PSN field.",
                            [
                                query(
                                    gate(
                                        metric(
                                            "ise3_active_sessions_by_psn",
                                            'psn=~"$node"',
                                        ),
                                        "active_sessions",
                                    ),
                                    "{{psn}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        ts(
                            "Session delta per PSN",
                            "Change in active sessions over five minutes by PSN, "
                            "highlighting abrupt load movement.",
                            [
                                query(
                                    "delta("
                                    + metric(
                                        "ise3_active_sessions_by_psn",
                                        'psn=~"$node"',
                                    )
                                    + "[5m])",
                                    "{{psn}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        ts(
                            "Accounting events per PSN",
                            "Accounting starts, stops, and other events in the "
                            "reporting window for each selected PSN.",
                            [
                                query(
                                    gate(
                                        metric(
                                            "ise3_radius_accounting_events",
                                            'dimension="psn",value=~"$node"',
                                        ),
                                        "radius_accounting",
                                    ),
                                    "{{value}} · {{event_type}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        bar(
                            "Authentication volume by PSN",
                            "Passed and failed authentication volume per PSN in "
                            "the reporting scan window.",
                            [
                                instant(
                                    "sort_desc(sum by (psn,status) "
                                    "("
                                    + gate(
                                        metric(
                                            "ise3_radius_authentications_by_psn",
                                            'psn=~"$node"',
                                        ),
                                        "radius_reporting",
                                    )
                                    + "))",
                                    "{{psn}} · {{status}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        bar(
                            "RADIUS errors by PSN",
                            "Error-view volume per selected PSN in the current "
                            "reporting window.",
                            [
                                instant(
                                    "sort_desc(sum by (psn) "
                                    "("
                                    + gate(
                                        metric(
                                            "ise3_radius_errors_by_psn",
                                            'psn=~"$node"',
                                        ),
                                        "radius_errors",
                                    )
                                    + "))",
                                    "{{psn}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        ts(
                            "RADIUS errors by PSN over time",
                            "When did this PSN start failing: the same per-PSN "
                            "error volume as the bar beside it, plotted over "
                            "time. It separates a PSN that has always been noisy "
                            "from one that broke at a specific moment.",
                            [
                                query(
                                    "sum by (psn) ("
                                    + gate(
                                        metric(
                                            "ise3_radius_errors_by_psn",
                                            'psn=~"$node"',
                                        ),
                                        "radius_errors",
                                    )
                                    + ")",
                                    "{{psn}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        bar(
                            "RADIUS error codes",
                            "ISE message-code distribution for the current error "
                            "window while troubleshooting the selected PSN scope."
                            + NAMED_CODES_NOTE,
                            [
                                instant(
                                    "sort_desc("
                                    + named_codes(
                                        "sum by (message_code) ("
                                        + gate(
                                            metric(
                                                "ise3_radius_errors_by_message_code"
                                            ),
                                            "radius_errors",
                                        )
                                        + ")"
                                    )
                                    + ")",
                                    "{{message_code}}",
                                )
                            ],
                        )
                    ),
                ),
            ),
            (
                "Throughput and latency",
                (
                    sized(
                        bar(
                            "RADIUS requests per hour",
                            "ISE performance-rollup request rate per selected "
                            "policy service node.",
                            [
                                instant(
                                    "sort_desc("
                                    + metric(
                                        "ise3_psn_radius_requests_per_hour",
                                        'node=~"$node"',
                                    )
                                    + ")",
                                    "{{node}}",
                                )
                            ],
                            unit="reqps",
                        )
                    ),
                    sized(
                        bar(
                            "Average RADIUS latency",
                            "Average request latency from ISE performance rollups "
                            "for each selected node.",
                            [
                                instant(
                                    "sort_desc("
                                    + metric(
                                        "ise3_psn_average_latency_seconds",
                                        'node=~"$node"',
                                    )
                                    + ")",
                                    "{{node}}",
                                )
                            ],
                            unit="s",
                        )
                    ),
                    sized(
                        bar(
                            "Average transactions per second",
                            "Average TPS reported by ISE for each selected policy "
                            "service node.",
                            [
                                instant(
                                    "sort_desc("
                                    + metric(
                                        "ise3_psn_average_tps",
                                        'node=~"$node"',
                                    )
                                    + ")",
                                    "{{node}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        bar(
                            "Trusted authentication latency",
                            "Authentication-side latency beside usable-sample "
                            "coverage, which catches missing latency fields.",
                            [
                                instant(
                                    metric(
                                        "ise3_radius_authentication_latency_seconds",
                                        'psn=~"$node"',
                                    ),
                                    "{{psn}} latency",
                                ),
                                instant(
                                    metric(
                                        "ise3_radius_authentication_latency_coverage",
                                        'psn=~"$node"',
                                    ),
                                    "{{psn}} coverage",
                                    "B",
                                ),
                            ],
                            unit="s",
                            # Coverage is a fraction, not a latency.
                            overrides=(
                                by_ref(
                                    "B",
                                    unit="percentunit",
                                    thresholds=COVERAGE,
                                    minimum=0,
                                    maximum=1,
                                ),
                            ),
                        )
                    ),
                ),
            ),
            (
                "Node resources and reporting",
                (
                    sized(
                        ts(
                            "Node load",
                            "Average load reported by ISE for each selected node "
                            "over the dashboard time range.",
                            [
                                query(
                                    metric(
                                        "ise3_psn_load_percent",
                                        'node=~"$node"',
                                    ),
                                    "{{node}}",
                                )
                            ],
                            unit="percent",
                            thresholds=UTILISATION,
                            minimum=0,
                            maximum=100,
                        )
                    ),
                    sized(
                        ts(
                            "CPU utilisation",
                            "CPU utilisation reported by ISE for each selected "
                            "deployment node.",
                            [
                                query(
                                    metric(
                                        "ise3_node_cpu_utilization_percent",
                                        'node=~"$node"',
                                    ),
                                    "{{node}}",
                                )
                            ],
                            unit="percent",
                            thresholds=UTILISATION,
                            minimum=0,
                            maximum=100,
                        )
                    ),
                    sized(
                        ts(
                            "Memory utilisation",
                            "Memory utilisation reported by ISE for each selected "
                            "deployment node.",
                            [
                                query(
                                    metric(
                                        "ise3_node_memory_utilization_percent",
                                        'node=~"$node"',
                                    ),
                                    "{{node}}",
                                )
                            ],
                            unit="percent",
                            thresholds=UTILISATION,
                            minimum=0,
                            maximum=100,
                        )
                    ),
                    sized(
                        bar(
                            "Highest disk utilisation",
                            "Filesystem utilisation by node and mount point, "
                            "sorted to surface the highest value first.",
                            [
                                instant(
                                    "sort_desc("
                                    + metric(
                                        "ise3_node_disk_utilization_percent",
                                        'node=~"$node"',
                                    )
                                    + ")",
                                    "{{node}} · {{filesystem}}",
                                )
                            ],
                            unit="percent",
                            thresholds=UTILISATION,
                            minimum=0,
                            maximum=100,
                        )
                    ),
                    sized(
                        ts(
                            "Reporting logs, noise, and suppression",
                            "MnT log volume beside noise and suppression rates for "
                            "each selected node.",
                            [
                                query(
                                    metric(
                                        "ise3_psn_mnt_logs_per_hour",
                                        'node=~"$node"',
                                    ),
                                    "{{node}} logs",
                                ),
                                query(
                                    metric(
                                        "ise3_psn_noise_per_hour",
                                        'node=~"$node"',
                                    ),
                                    "{{node}} noise",
                                    ref="B",
                                ),
                                query(
                                    metric(
                                        "ise3_psn_suppression_per_hour",
                                        'node=~"$node"',
                                    ),
                                    "{{node}} suppression",
                                    ref="C",
                                ),
                            ],
                        )
                    ),
                    sized(
                        tbl(
                            "PSN node state",
                            "Current deployment state, roles, and services for "
                            "the selected policy service nodes.",
                            [
                                instant(
                                    metric(
                                        "ise3_deployment_node_state",
                                        'node=~"$node",state=~".*"',
                                    )
                                    + " == 1",
                                    "{{node}} · {{roles}} · {{state}}",
                                )
                            ],
                            columns=(None,),
                        )
                    ),
                    sized(
                        tbl(
                            "Reporting source freshness",
                            "Newest-row age and recent-row presence for reporting "
                            "views that drive PSN troubleshooting.",
                            [
                                instant(
                                    metric(
                                        "ise3_source_has_recent_rows",
                                        'view=~"radius_.*|key_performance_metrics|system_summary"',
                                    ),
                                    "{{view}} recent",
                                ),
                                instant(
                                    metric(
                                        "ise3_source_latest_row_age_seconds",
                                        'view=~"radius_.*|key_performance_metrics|system_summary"',
                                    ),
                                    "{{view}} age",
                                    "B",
                                ),
                            ],
                            columns=("recent", "age s"),
                            sort=("age s", True),
                            column_overrides=(
                                by_column("recent", **BOOLEAN_CELL),
                                by_column("age s", **SECONDS_CELL),
                            ),
                        )
                    ),
                    sized(
                        tbl(
                            "Diagnostic work queue",
                            "Worst-first bounded AAA and system diagnostic "
                            "evidence by PSN, severity, category, and message code.",
                            [
                                instant(
                                    "sort_desc("
                                    + metric(
                                        "ise3_psn_diagnostic_events",
                                        'node=~"$node"',
                                    )
                                    + ")",
                                    "{{node}} · {{source}} · {{severity}} · "
                                    "{{category}} · {{message_code}}",
                                )
                            ],
                            columns=("events",),
                        )
                    ),
                    sized(
                        stat_panel(
                            "Diagnostic coverage",
                            "Published versus existing diagnostic groups for each "
                            "optional reporting view.",
                            [
                                instant(
                                    metric(
                                        "ise3_topk_groups_returned",
                                        'dataset="psn_performance",breakdown=~".*_diagnostics"',
                                    ),
                                    "{{breakdown}} published",
                                ),
                                instant(
                                    metric(
                                        "ise3_topk_groups_total",
                                        'dataset="psn_performance",breakdown=~".*_diagnostics"',
                                    ),
                                    "{{breakdown}} total",
                                    "B",
                                ),
                            ],
                        ),
                        5,
                        24,
                    ),
                ),
            ),
        ),
        variables=(node,),
    )


def pan_mnt_dashboard():
    node = label_variable(
        "node", "PAN or MnT node", "ise3_deployment_node_state", "node"
    )
    # A PSN named here is answered on the PSN dashboard, not this one.
    psn_drilldown = drilldown(
        "This node on PSN troubleshooting", "ise3-psn", node=by_series("psn")
    )
    return assemble(
        "ISE 3 — PAN and MnT Troubleshooting",
        "ise3-pan-mnt",
        "Administrative and monitoring-node state, services, backup, "
        "certificates, posture cache, node resources, and collector failures.",
        (
            (
                "Deployment and services",
                (
                    sized(
                        stat_panel(
                            "Core dataset readiness",
                            "Freshness of deployment, performance, backup, "
                            "certificates, patches, and current-posture data. NOT "
                            "READY means the panels fed by that dataset stay blank.",
                            [
                                instant(ready_bool("deployment"), "deployment"),
                                instant(
                                    ready_bool("psn_performance"), "performance", "B"
                                ),
                                instant(ready_bool("backup"), "backup", "C"),
                                instant(
                                    ready_bool("certificates"), "certificates", "D"
                                ),
                                instant(ready_bool("patches"), "patches", "E"),
                                instant(
                                    ready_bool("posture_current"), "posture", "F"
                                ),
                            ],
                            mappings=READINESS,
                            no_value=NO_DATA_EXPORTER,
                        ),
                        5,
                        24,
                    ),
                    sized(
                        stat_panel(
                            "Core snapshot age",
                            "Seconds since the latest successful deployment, "
                            "performance, and current-posture snapshots.",
                            [
                                instant(
                                    "time() - "
                                    + metric(
                                        "ise3_dataset_last_success_timestamp",
                                        'dataset="deployment"',
                                    ),
                                    "deployment",
                                ),
                                instant(
                                    "time() - "
                                    + metric(
                                        "ise3_dataset_last_success_timestamp",
                                        'dataset="psn_performance"',
                                    ),
                                    "performance",
                                    "B",
                                ),
                                instant(
                                    "time() - "
                                    + metric(
                                        "ise3_dataset_last_success_timestamp",
                                        'dataset="posture_current"',
                                    ),
                                    "posture",
                                    "C",
                                ),
                            ],
                            unit="s",
                        ),
                        5,
                        12,
                    ),
                    sized(
                        stat_panel(
                            "Connected PANs and MnTs",
                            "Connected administrative and monitoring personas "
                            "derived from the authoritative deployment snapshot.",
                            [
                                instant(
                                    "count("
                                    + metric(
                                        "ise3_deployment_node_state",
                                        'roles=~".*(Admin|Standalone).*",state="Connected"',
                                    )
                                    + " == 1)",
                                    "PANs",
                                ),
                                instant(
                                    "count("
                                    + metric(
                                        "ise3_deployment_node_state",
                                        'roles=~".*(Monitoring|Standalone).*",state="Connected"',
                                    )
                                    + " == 1)",
                                    "MnTs",
                                    "B",
                                ),
                            ],
                        ),
                        5,
                        12,
                    ),
                    sized(
                        tbl(
                            "PAN and MnT node state",
                            "Current state, persona roles, and service list for "
                            "each selected core ISE node.",
                            [
                                instant(
                                    metric(
                                        "ise3_deployment_node_state",
                                        'node=~"$node",state=~".*"',
                                    )
                                    + " == 1",
                                    "{{node}} · {{roles}} · {{state}}",
                                )
                            ],
                            columns=(None,),
                        )
                    ),
                    sized(
                        tbl(
                            "Node service assignments",
                            "Explicit services enabled on each selected "
                            "administrative or monitoring node.",
                            [
                                instant(
                                    metric(
                                        "ise3_deployment_node_service_enabled",
                                        'node=~"$node"',
                                    ),
                                    "{{node}} · {{service}}",
                                )
                            ],
                            columns=("enabled",),
                            column_overrides=(by_column("enabled", **BOOLEAN_CELL),),
                        )
                    ),
                    sized(
                        stat_panel(
                            "PAN HA and backup",
                            "Administrative HA state beside configured backup and "
                            "latest successful backup age.",
                            [
                                instant(
                                    metric("ise3_deployment_pan_ha_enabled"),
                                    "PAN HA",
                                ),
                                instant(
                                    metric("ise3_backup_configured"),
                                    "backup configured",
                                    "B",
                                ),
                                instant(
                                    metric("ise3_backup_age_hours"),
                                    "backup age",
                                    "C",
                                ),
                            ],
                            # Two booleans and an age share one field config.
                            overrides=(
                                by_ref("A", mappings=YES_NO),
                                by_ref(
                                    "B",
                                    mappings=CONFIGURED,
                                    thresholds=REQUIRED_BOOLEAN,
                                ),
                                by_ref(
                                    "C",
                                    unit="h",
                                    thresholds=BACKUP_AGE_HOURS,
                                ),
                            ),
                        ),
                        5,
                        12,
                    ),
                    sized(
                        stat_panel(
                            "Patch and certificate findings",
                            "Patch level, expired certificates, and near-term "
                            "certificate expiry findings for core nodes.",
                            [
                                instant(metric("ise3_patch_level"), "patch"),
                                instant(
                                    metric("ise3_certificates_expired"),
                                    "expired",
                                    "B",
                                ),
                                instant(
                                    metric("ise3_certificates_expiring_soon"),
                                    "expiring",
                                    "C",
                                ),
                            ],
                            overrides=(
                                by_ref("B", thresholds=NONZERO_CRITICAL),
                                by_ref("C", thresholds=NONZERO_WARNING),
                            ),
                        ),
                        5,
                        12,
                    ),
                    sized(
                        tbl(
                            "Core-node certificate expiry",
                            "Certificate expiry by selected node, store, usage, "
                            "and certificate identity.",
                            [
                                instant(
                                    "sort("
                                    + metric(
                                        "ise3_certificate_expiry_days",
                                        'node=~"$node"',
                                    )
                                    + ")",
                                    "{{node}} · {{store}} · {{certificate}}",
                                )
                            ],
                            columns=("days to expiry",),
                        )
                    ),
                ),
            ),
            (
                "MnT session and posture coverage",
                (
                    sized(
                        stat_panel(
                            "Active sessions",
                            "Current live-session count and distinct endpoints "
                            "from the selected active provider.",
                            [
                                instant(
                                    f"sum({gate(metric('ise3_active_sessions_total'), 'active_sessions')})",
                                    "sessions",
                                ),
                                instant(
                                    f"sum({gate(metric('ise3_active_session_endpoints'), 'active_sessions')})",
                                    "endpoints",
                                    "B",
                                ),
                            ],
                            no_value=NO_DATA_STALE,
                        ),
                        5,
                        12,
                    ),
                    sized(
                        stat_panel(
                            "MnT detail cache",
                            "Coverage and deferred endpoint-detail fetches shared "
                            "by authorization and current-posture datasets.",
                            [
                                instant(
                                    metric(
                                        "ise3_detail_cache_coverage",
                                        'cache="mnt_session_detail"',
                                    ),
                                    "coverage",
                                ),
                                instant(
                                    metric(
                                        "ise3_detail_fetches_deferred",
                                        'cache="mnt_session_detail"',
                                    ),
                                    "deferred",
                                    "B",
                                ),
                            ],
                        ),
                        5,
                        12,
                    ),
                    sized(
                        bar(
                            "Active posture by ops owner",
                            "Distinct active endpoints by posture state and the "
                            "operations owner responsible for their NAD.",
                            [
                                instant(
                                    "sort_desc(sum by (ops_owner,status) "
                                    f"({gate(metric('ise3_posture_endpoints'), 'posture_current')}))",
                                    "{{ops_owner}} · {{status}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        bar(
                            "Active sessions by serving PSN",
                            "Current active-session distribution across serving "
                            "policy service nodes.",
                            [
                                instant(
                                    "sort_desc(sum by (psn) "
                                    f"({gate(metric('ise3_active_sessions_by_psn'), 'active_sessions')}))",
                                    "{{psn}}",
                                )
                            ],
                            data_links=(psn_drilldown,),
                        )
                    ),
                    sized(
                        bar(
                            "Active posture by serving PSN",
                            "Distinct active endpoints by serving policy service "
                            "node and current posture state.",
                            [
                                instant(
                                    "sort_desc(sum by (psn,status) ("
                                    + gate(
                                        metric(
                                            "ise3_posture_endpoints_by_psn"
                                        ),
                                        "posture_current",
                                    )
                                    + "))",
                                    "{{psn}} · {{status}}",
                                )
                            ],
                            data_links=(psn_drilldown,),
                        )
                    ),
                ),
            ),
            (
                "Node performance and collector diagnostics",
                (
                    sized(
                        ts(
                            "Node load, CPU, and memory",
                            "Core-node load, CPU, and memory utilisation over the "
                            "selected time range.",
                            [
                                query(
                                    metric(
                                        "ise3_psn_load_percent",
                                        'node=~"$node"',
                                    ),
                                    "{{node}} load",
                                ),
                                query(
                                    metric(
                                        "ise3_node_cpu_utilization_percent",
                                        'node=~"$node"',
                                    ),
                                    "{{node}} CPU",
                                    ref="B",
                                ),
                                query(
                                    metric(
                                        "ise3_node_memory_utilization_percent",
                                        'node=~"$node"',
                                    ),
                                    "{{node}} memory",
                                    ref="C",
                                ),
                            ],
                            unit="percent",
                            thresholds=UTILISATION,
                            minimum=0,
                            maximum=100,
                        )
                    ),
                    sized(
                        bar(
                            "Highest disk utilisation",
                            "Filesystem utilisation for selected core nodes, "
                            "sorted to surface capacity risk.",
                            [
                                instant(
                                    "sort_desc("
                                    + metric(
                                        "ise3_node_disk_utilization_percent",
                                        'node=~"$node"',
                                    )
                                    + ")",
                                    "{{node}} · {{filesystem}}",
                                )
                            ],
                            unit="percent",
                            thresholds=UTILISATION,
                            minimum=0,
                            maximum=100,
                        )
                    ),
                    sized(
                        ts(
                            "Reporting logs, noise, and suppression",
                            "MnT log throughput beside noise and suppression for "
                            "the selected core nodes.",
                            [
                                query(
                                    metric(
                                        "ise3_psn_mnt_logs_per_hour",
                                        'node=~"$node"',
                                    ),
                                    "{{node}} logs",
                                ),
                                query(
                                    metric(
                                        "ise3_psn_noise_per_hour",
                                        'node=~"$node"',
                                    ),
                                    "{{node}} noise",
                                    ref="B",
                                ),
                                query(
                                    metric(
                                        "ise3_psn_suppression_per_hour",
                                        'node=~"$node"',
                                    ),
                                    "{{node}} suppression",
                                    ref="C",
                                ),
                            ],
                        )
                    ),
                    sized(
                        tbl(
                            "PAN and MnT failure work queue",
                            "Latest bounded failure explanation for deployment, "
                            "backup, certificate, posture, and performance collectors.",
                            [
                                instant(
                                    metric(
                                        "ise3_dataset_last_failure_detail_info",
                                        'dataset=~"deployment|backup|certificates|patches|posture_current|psn_performance"',
                                    ),
                                    "{{dataset}} · {{provider}} · {{reason}} · {{detail}}",
                                )
                            ],
                            columns=(None,),
                        )
                    ),
                    sized(
                        ts(
                            "ISE API latency",
                            "Outbound request duration percentiles for PAN and "
                            "MnT API targets, replacing opaque collector timing.",
                            [
                                query(
                                    "histogram_quantile(0.95, sum by "
                                    "(le,target,api) (rate("
                                    + metric(
                                        "ise3_api_request_duration_seconds_bucket",
                                        'target=~"pan|mnt"',
                                    )
                                    + "[5m])))",
                                    "{{target}} · {{api}} p95",
                                )
                            ],
                            unit="s",
                        )
                    ),
                    sized(
                        ts(
                            "Total authentication latency",
                            "Mean and maximum total authentication latency from "
                            "usable current MnT session-detail samples.",
                            [
                                query(
                                    metric(
                                        "ise3_session_authentication_latency_seconds"
                                    ),
                                    "{{statistic}}",
                                )
                            ],
                            unit="s",
                        )
                    ),
                    sized(
                        bar(
                            "Authentication step latency",
                            "Mean and maximum latency by bounded numeric ISE "
                            "authentication step code.",
                            [
                                instant(
                                    "sort_desc("
                                    + metric(
                                        "ise3_session_authentication_step_latency_seconds"
                                    )
                                    + ")",
                                    "step {{step}} · {{statistic}}",
                                )
                            ],
                            unit="s",
                        )
                    ),
                    sized(
                        bar(
                            "Authentication step samples",
                            "Usable MnT session-detail samples behind each "
                            "authentication-step latency value.",
                            [
                                instant(
                                    "sort_desc("
                                    + metric(
                                        "ise3_session_authentication_step_latency_samples"
                                    )
                                    + ")",
                                    "step {{step}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        tbl(
                            "PAN and MnT diagnostic work queue",
                            "Bounded AAA and system diagnostics for selected core "
                            "nodes, retaining source, severity, category, and code.",
                            [
                                instant(
                                    "sort_desc("
                                    + metric(
                                        "ise3_psn_diagnostic_events",
                                        'node=~"$node"',
                                    )
                                    + ")",
                                    "{{node}} · {{source}} · {{severity}} · "
                                    "{{category}} · {{message_code}}",
                                )
                            ],
                            columns=("events",),
                        )
                    ),
                ),
            ),
        ),
        variables=(node,),
    )


def secureclient_dashboard():
    owner = label_variable(
        "ops_owner",
        "Ops owner",
        "ise3_posture_endpoints",
        "ops_owner",
    )
    psn_drilldown = drilldown(
        "This node on PSN troubleshooting", "ise3-psn", node=by_series("psn")
    )
    current = metric("ise3_posture_endpoints", 'ops_owner=~"$ops_owner"')
    compliant = metric(
        "ise3_posture_endpoints",
        'ops_owner=~"$ops_owner",status=~"(?i)compliant|passed"',
    )
    noncompliant = metric(
        "ise3_posture_endpoints",
        'ops_owner=~"$ops_owner",status=~"(?i)non.?compliant|failed|error"',
    )
    history = metric("ise3_posture_assessments")
    history_passed = metric(
        "ise3_posture_assessments", 'status=~"(?i)compliant|passed"'
    )
    history_failed = metric(
        "ise3_posture_assessments",
        'status=~"(?i)non.?compliant|failed|error"',
    )
    return assemble(
        "ISE 3 — Secure Client and Posture",
        "ise3-secureclient",
        "Current active posture from MnT beside historical Data Connect "
        "assessments, policy outcomes, client versions, operating systems, and "
        "detail-cache coverage.",
        (
            (
                "Current active posture",
                (
                    sized(
                        stat_panel(
                            "Posture dataset readiness",
                            "Availability and freshness of current MnT posture and "
                            "historical Data Connect assessment datasets. NOT READY "
                            "means the panels fed by that dataset stay blank.",
                            [
                                instant(ready_bool("posture_current"), "current"),
                                instant(
                                    ready_bool("posture_history"), "history", "B"
                                ),
                            ],
                            mappings=READINESS,
                            no_value=NO_DATA_EXPORTER,
                        ),
                        5,
                        8,
                    ),
                    sized(
                        stat_panel(
                            "Active compliance share",
                            "Compliant active endpoints divided by endpoints with "
                            "a conclusive posture state.",
                            [
                                instant(
                                    f"sum({gate(compliant, 'posture_current')}) / "
                                    f"clamp_min(sum({gate(compliant, 'posture_current')}) + "
                                    f"sum({gate(noncompliant, 'posture_current')}), 1)",
                                    "compliant",
                                )
                            ],
                            unit="percentunit",
                            thresholds=RATIO_HIGH_IS_GOOD,
                            minimum=0,
                            maximum=1,
                            no_value=NO_DATA_STALE,
                        ),
                        5,
                        8,
                    ),
                    sized(
                        stat_panel(
                            "Posture snapshot age",
                            "Seconds since the latest successful current MnT "
                            "posture and historical Data Connect assessments.",
                            [
                                instant(
                                    "time() - "
                                    + metric(
                                        "ise3_dataset_last_success_timestamp",
                                        'dataset="posture_current"',
                                    ),
                                    "current",
                                ),
                                instant(
                                    "time() - "
                                    + metric(
                                        "ise3_dataset_last_success_timestamp",
                                        'dataset="posture_history"',
                                    ),
                                    "historical",
                                    "B",
                                ),
                            ],
                            unit="s",
                        ),
                        5,
                        8,
                    ),
                    sized(
                        stat_panel(
                            "Active non-compliant endpoints",
                            "Distinct active endpoints with a failed, error, or "
                            "non-compliant current posture state.",
                            [
                                instant(
                                    f"sum({gate(noncompliant, 'posture_current')})",
                                    "non-compliant",
                                )
                            ],
                            thresholds=NONZERO_WARNING,
                            no_value=NO_DATA_STALE,
                        ),
                        5,
                        8,
                    ),
                    sized(
                        ts(
                            "Current posture status",
                            "Distinct active endpoints over time by posture state "
                            "and operational owner.",
                            [
                                query(
                                    gate(current, "posture_current"),
                                    "{{ops_owner}} · {{status}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        ts(
                            "MnT detail coverage",
                            "Coverage, entries, and deferred work for the shared "
                            "session-detail cache behind current posture.",
                            [
                                query(
                                    metric(
                                        "ise3_detail_cache_coverage",
                                        'cache="mnt_session_detail"',
                                    ),
                                    "coverage",
                                ),
                                query(
                                    metric(
                                        "ise3_detail_cache_entries",
                                        'cache="mnt_session_detail"',
                                    ),
                                    "entries",
                                    ref="B",
                                ),
                                query(
                                    metric(
                                        "ise3_detail_fetches_deferred",
                                        'cache="mnt_session_detail"',
                                    ),
                                    "deferred",
                                    ref="C",
                                ),
                            ],
                        )
                    ),
                    sized(
                        bar(
                            "MnT source field coverage",
                            "Fraction of cached active-session detail carrying "
                            "each posture, client, operating-system, and "
                            "authentication-latency field.",
                            [
                                instant(
                                    "sort_desc("
                                    + metric(
                                        "ise3_session_detail_field_coverage"
                                    )
                                    + ")",
                                    "{{field}}",
                                )
                            ],
                            unit="percentunit",
                            thresholds=COVERAGE,
                            minimum=0,
                            maximum=1,
                        )
                    ),
                    sized(
                        bar(
                            "Active Secure Client versions",
                            "Distinct active endpoints by Secure Client posture "
                            "agent version from cached MnT detail.",
                            [
                                instant(
                                    "sort_desc(sum by (agent_version) "
                                    f"({covered(metric('ise3_posture_agent_version_endpoints'), 'posture_current', 'mnt_session_detail')}))",
                                    "{{agent_version}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        bar(
                            "Active endpoint operating systems",
                            "Distinct active posture endpoints grouped by reported "
                            "operating system.",
                            [
                                instant(
                                    "sort_desc(sum by (os) "
                                    f"({covered(metric('ise3_posture_endpoints_by_os'), 'posture_current', 'mnt_session_detail')}))",
                                    "{{os}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        bar(
                            "Active posture policy outcomes",
                            "Distinct active endpoints per posture policy and "
                            "parsed policy result.",
                            [
                                instant(
                                    "sort_desc(sum by (policy,result) "
                                    f"({covered(metric('ise3_posture_policy_results'), 'posture_current', 'mnt_session_detail')}))",
                                    "{{policy}} · {{result}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        bar(
                            "Failed active posture policies",
                            "Policies currently failing active endpoints, sorted "
                            "by affected endpoint count.",
                            [
                                instant(
                                    "sort_desc(sum by (policy) "
                                    "("
                                    + covered(
                                        metric(
                                            "ise3_posture_policy_results",
                                            'result=~"(?i)fail(ed)?|error"',
                                        ),
                                        "posture_current",
                                        "mnt_session_detail",
                                    )
                                    + "))",
                                    "{{policy}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        bar(
                            "Active posture by ops owner",
                            "Current posture state grouped by the operational "
                            "owner responsible for the endpoint's NAD.",
                            [
                                instant(
                                    "sort_desc(sum by (ops_owner,status) "
                                    f"({gate(current, 'posture_current')}))",
                                    "{{ops_owner}} · {{status}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        bar(
                            "Active endpoints by PSN",
                            "Distinct current posture endpoints by serving policy "
                            "service node and posture status.",
                            [
                                instant(
                                    "sort_desc(sum by (psn,status) ("
                                    + gate(
                                        metric(
                                            "ise3_posture_endpoints_by_psn"
                                        ),
                                        "posture_current",
                                    )
                                    + "))",
                                    "{{psn}} · {{status}}",
                                )
                            ],
                            data_links=(psn_drilldown,),
                        )
                    ),
                    sized(
                        bar(
                            "Active compliance share by ops owner",
                            "Conclusive compliant share for each operations owner, "
                            "keeping responsibility attached to the posture result.",
                            [
                                instant(
                                    "sum by (ops_owner) ("
                                    + gate(compliant, "posture_current")
                                    + ") / clamp_min(sum by (ops_owner) ("
                                    + gate(compliant, "posture_current")
                                    + ") + sum by (ops_owner) ("
                                    + gate(noncompliant, "posture_current")
                                    + "), 1)",
                                    "{{ops_owner}}",
                                )
                            ],
                            unit="percentunit",
                            thresholds=RATIO_HIGH_IS_GOOD,
                            minimum=0,
                            maximum=1,
                        )
                    ),
                    sized(
                        bar(
                            "Active non-compliant endpoints by ops owner",
                            "Current non-compliant endpoint count routed to the "
                            "operations owner responsible for each NAD.",
                            [
                                instant(
                                    "sort_desc(sum by (ops_owner) ("
                                    + gate(noncompliant, "posture_current")
                                    + "))",
                                    "{{ops_owner}}",
                                )
                            ],
                        )
                    ),
                ),
            ),
            (
                "Historical assessments",
                (
                    sized(
                        stat_panel(
                            "Historical compliance share",
                            "Compliant endpoints divided by conclusive posture "
                            "assessments in the reporting window.",
                            [
                                instant(
                                    f"sum({gate(history_passed, 'posture_history')}) / "
                                    f"clamp_min(sum({gate(history_passed, 'posture_history')}) + "
                                    f"sum({gate(history_failed, 'posture_history')}), 1)",
                                    "compliant",
                                )
                            ],
                            unit="percentunit",
                            thresholds=RATIO_HIGH_IS_GOOD,
                            minimum=0,
                            maximum=1,
                            no_value=NO_DATA_STALE,
                        ),
                        5,
                        12,
                    ),
                    sized(
                        stat_panel(
                            "Assessed and failed endpoints",
                            "Distinct assessed endpoints and conclusive failures "
                            "in the historical reporting window.",
                            [
                                instant(
                                    f"sum({gate(history, 'posture_history')})",
                                    "assessed",
                                ),
                                instant(
                                    f"sum({gate(history_failed, 'posture_history')})",
                                    "failed",
                                    "B",
                                ),
                            ],
                            no_value=NO_DATA_STALE,
                        ),
                        5,
                        12,
                    ),
                    sized(
                        stat_panel(
                            "Eligible posture coverage",
                            "Currently eligible endpoints assessed in the reporting "
                            "window beside those without a recent assessment.",
                            [
                                instant(
                                    metric(
                                        "ise3_posture_eligible_recently_assessed_total"
                                    ),
                                    "recently assessed",
                                ),
                                instant(
                                    metric(
                                        "ise3_posture_eligible_without_recent_assessment_total"
                                    ),
                                    "without recent assessment",
                                    "B",
                                ),
                                instant(
                                    metric(
                                        "ise3_posture_eligible_endpoints_total"
                                    ),
                                    "eligible",
                                    "C",
                                ),
                            ],
                            overrides=(by_ref("B", thresholds=NONZERO_WARNING),),
                        ),
                        5,
                        12,
                    ),
                    sized(
                        stat_panel(
                            "Fleet posture coverage",
                            "Fraction of currently eligible endpoints with an "
                            "assessment in the historical reporting window.",
                            [
                                instant(
                                    metric(
                                        "ise3_posture_eligible_recently_assessed_total"
                                    )
                                    + " / clamp_min("
                                    + metric(
                                        "ise3_posture_eligible_endpoints_total"
                                    )
                                    + ", 1)",
                                    "coverage",
                                )
                            ],
                            unit="percentunit",
                            thresholds=COVERAGE,
                            minimum=0,
                            maximum=1,
                        ),
                        5,
                        12,
                    ),
                    sized(
                        ts(
                            "Historical assessment status",
                            "Distinct endpoints assessed in each posture state "
                            "over the historical reporting window.",
                            [
                                query(
                                    gate(history, "posture_history"),
                                    "{{status}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        bar(
                            "Historical assessments by policy",
                            "Distinct endpoints per policy and outcome; endpoints "
                            "are not double-counted across failed conditions.",
                            [
                                instant(
                                    "sort_desc(sum by (policy,status) "
                                    f"({gate(metric('ise3_posture_assessments_by_policy'), 'posture_history')}))",
                                    "{{policy}} · {{status}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        tbl(
                            "Historical failed conditions",
                            "Distinct endpoints failing each posture condition in "
                            "the reporting window, sorted by impact.",
                            [
                                instant(
                                    "sort_desc("
                                    f"{gate(metric('ise3_posture_failed_conditions'), 'posture_history')}"
                                    ")",
                                    "{{condition}}",
                                )
                            ],
                            columns=("endpoints",),
                        )
                    ),
                    sized(
                        bar(
                            "Historical assessments by client version",
                            "Distinct assessed endpoints grouped by Secure Client "
                            "posture agent version.",
                            [
                                instant(
                                    "sort_desc(sum by (agent_version) "
                                    f"({gate(metric('ise3_posture_assessments_by_agent_version'), 'posture_history')}))",
                                    "{{agent_version}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        bar(
                            "Historical assessments by operating system",
                            "Distinct assessed endpoints grouped by reported "
                            "operating system.",
                            [
                                instant(
                                    "sort_desc(sum by (os) "
                                    f"({gate(metric('ise3_posture_assessments_by_os'), 'posture_history')}))",
                                    "{{os}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        bar(
                            "Historical assessments by PSN",
                            "Distinct assessed endpoints grouped by serving PSN "
                            "and posture outcome.",
                            [
                                instant(
                                    "sort_desc(sum by (psn,status) ("
                                    + gate(
                                        metric(
                                            "ise3_posture_assessments_by_psn"
                                        ),
                                        "posture_history",
                                    )
                                    + "))",
                                    "{{psn}} · {{status}}",
                                )
                            ],
                            data_links=(psn_drilldown,),
                        )
                    ),
                    sized(
                        ts(
                            "Posture assessment volume per PSN",
                            "Historical assessment-window volume per PSN and "
                            "posture state over dashboard time.",
                            [
                                query(
                                    gate(
                                        metric(
                                            "ise3_posture_assessments_by_psn"
                                        ),
                                        "posture_history",
                                    ),
                                    "{{psn}} · {{status}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        tbl(
                            "Historical posture failure work queue",
                            "Failed policy and condition evidence for prioritising "
                            "posture remediation without storing endpoint rows.",
                            [
                                # A union, not two targets: a failed policy and
                                # a failed condition are separate rows of one
                                # queue. They share no key, so joining them
                                # would pair unrelated evidence on one row.
                                instant(
                                    "sort_desc(("
                                    + gate(
                                        metric(
                                            "ise3_posture_assessments_by_policy",
                                            'status=~"(?i)non.?compliant|failed|error"',
                                        ),
                                        "posture_history",
                                    )
                                    + ") or ("
                                    + gate(
                                        metric(
                                            "ise3_posture_failed_conditions"
                                        ),
                                        "posture_history",
                                    )
                                    + "))",
                                    "{{policy}} · {{status}} · {{condition}}",
                                ),
                            ],
                            columns=("endpoints",),
                        )
                    ),
                ),
            ),
        ),
        variables=(owner,),
    )


def tacacs_dashboard():
    username = label_variable(
        "username",
        "Username",
        "ise3_tacacs_internal_account_enabled",
        "username",
    )
    # A TACACS device is the same NAD the endpoints dashboard classifies; the
    # activity metrics carry its name in the generic `value` dimension label.
    device_drilldown = drilldown(
        "This device on Endpoints and devices",
        "ise3-endpoints",
        nad=by_series("value"),
    )
    return assemble(
        "ISE 3 — TACACS Device Administration",
        "ise3-tacacs",
        "Internal account hygiene, policy-set inventory, authentication, "
        "authorization, commands, operational-owner rollups, and bounded-device "
        "coverage.",
        (
            (
                "Configuration and account health",
                (
                    sized(
                        stat_panel(
                            "Dataset readiness",
                            "Freshness of TACACS configuration and historical "
                            "activity datasets used by this workflow. NOT READY "
                            "means the panels fed by that dataset stay blank.",
                            [
                                instant(
                                    ready_bool("tacacs_config"), "configuration"
                                ),
                                instant(
                                    ready_bool("tacacs_activity"), "activity", "B"
                                ),
                                instant(
                                    ready_bool("tacacs_policy_rules"),
                                    "policy rules",
                                    "C",
                                ),
                            ],
                            mappings=READINESS,
                            no_value=NO_DATA_EXPORTER,
                        ),
                        5,
                        8,
                    ),
                    sized(
                        stat_panel(
                            "Collection age",
                            "Seconds since the latest successful TACACS "
                            "configuration and historical-activity collections.",
                            [
                                instant(
                                    "time() - "
                                    + metric(
                                        "ise3_dataset_last_success_timestamp",
                                        'dataset="tacacs_config"',
                                    ),
                                    "configuration",
                                ),
                                instant(
                                    "time() - "
                                    + metric(
                                        "ise3_dataset_last_success_timestamp",
                                        'dataset="tacacs_activity"',
                                    ),
                                    "activity",
                                    "B",
                                ),
                            ],
                            unit="s",
                        ),
                        5,
                        8,
                    ),
                    sized(
                        stat_panel(
                            "Internal accounts",
                            "Configured internal Device Administration accounts "
                            "beside those whose detail cache is classified.",
                            [
                                instant(
                                    metric("ise3_tacacs_internal_accounts"),
                                    "accounts",
                                ),
                                instant(
                                    metric(
                                        "ise3_tacacs_internal_accounts_classified"
                                    ),
                                    "classified",
                                    "B",
                                ),
                            ],
                        ),
                        5,
                        8,
                    ),
                    sized(
                        stat_panel(
                            "Policy sets and active accounts",
                            "Configured Device Administration policy sets beside "
                            "distinct accounts with reporting-window activity.",
                            [
                                instant(
                                    metric("ise3_tacacs_policy_sets"),
                                    "policy sets",
                                ),
                                instant(
                                    metric("ise3_tacacs_active_accounts"),
                                    "active accounts",
                                    "B",
                                ),
                            ],
                        ),
                        5,
                        8,
                    ),
                    sized(
                        stat_panel(
                            "Recent TACACS events",
                            "Authentication, authorization, and command-accounting "
                            "events retained by the activity reporting window.",
                            [
                                instant(
                                    "sum("
                                    + metric("ise3_tacacs_authentications")
                                    + ")",
                                    "authentications",
                                ),
                                instant(
                                    "sum("
                                    + metric("ise3_tacacs_authorizations")
                                    + ")",
                                    "authorizations",
                                    "B",
                                ),
                                instant(
                                    "sum(" + metric("ise3_tacacs_commands") + ")",
                                    "commands",
                                    "C",
                                ),
                            ],
                        ),
                        5,
                        8,
                    ),
                    sized(
                        bar(
                            "Configured Device Admin objects",
                            "Complete configured policy-set, command-set, and "
                            "shell-profile inventories from the PAN OpenAPI.",
                            [
                                instant(
                                    "sort_desc("
                                    + metric("ise3_tacacs_policy_objects")
                                    + ")",
                                    "{{object_type}}",
                                ),
                                instant(
                                    "sort_desc("
                                    + metric("ise3_tacacs_policy_rules_total")
                                    + ")",
                                    "{{rule_type}} rules",
                                    "B",
                                ),
                            ],
                        )
                    ),
                    sized(
                        tbl(
                            "Policy rule inventory",
                            "Authentication and authorization rule counts for "
                            "each covered Device Administration policy set.",
                            [
                                instant(
                                    "sort_desc("
                                    + metric(
                                        "ise3_tacacs_policy_rule_count"
                                    )
                                    + ")",
                                    "{{policy_set}} · {{rule_type}}",
                                )
                            ],
                            columns=("rules",),
                        )
                    ),
                    sized(
                        tbl(
                            "Internal account inventory",
                            "Enabled state for each selected internal Device "
                            "Administration account.",
                            [
                                instant(
                                    metric(
                                        "ise3_tacacs_internal_account_enabled",
                                        'username=~"$username"',
                                    ),
                                    "{{username}}",
                                )
                            ],
                            columns=("enabled",),
                            column_overrides=(by_column("enabled", **BOOLEAN_CELL),),
                        )
                    ),
                    sized(
                        tbl(
                            "Account hygiene review queue",
                            "Named hygiene risks for selected internal accounts; "
                            "zero-valued risks remain visible for completeness.",
                            [
                                instant(
                                    metric(
                                        "ise3_tacacs_internal_account_hygiene_risk",
                                        'username=~"$username"',
                                    ),
                                    "{{username}} · {{risk}}",
                                )
                            ],
                            columns=("flagged",),
                            column_overrides=(
                                by_column(
                                    "flagged", mappings=RISK, thresholds=NONZERO_CRITICAL,
                                    colour_cells=True,
                                ),
                            ),
                        )
                    ),
                    sized(
                        tbl(
                            "Enabled accounts without window activity",
                            "Enabled internal accounts absent from TACACS "
                            "authentication activity in the retained reporting window.",
                            [
                                instant(
                                    metric(
                                        "ise3_tacacs_internal_account_enabled",
                                        'username=~"$username"',
                                    )
                                    + " == 1 unless on(instance,username) "
                                    + "label_replace("
                                    + metric(
                                        "ise3_tacacs_authentications",
                                        'dimension="username"',
                                    )
                                    + ', "username", "$1", "value", "(.*)")',
                                    "{{username}}",
                                )
                            ],
                            columns=(None,),
                        )
                    ),
                    sized(
                        tbl(
                            "Last TACACS evidence by account",
                            "Seconds since the newest authentication, "
                            "authorization, or command-accounting event for each "
                            "selected account inside the retained reporting window.",
                            [
                                instant(
                                    "time() - "
                                    + metric(
                                        "ise3_tacacs_account_last_seen_timestamp",
                                        'username=~"$username"',
                                    ),
                                    "{{username}} · {{event_type}}",
                                )
                            ],
                            columns=("age s",),
                            column_overrides=(by_column("age s", **SECONDS_CELL),),
                        )
                    ),
                    sized(
                        ts(
                            "Account detail-cache coverage",
                            "Coverage, entries, and deferred work for internal "
                            "account detail classification.",
                            [
                                query(
                                    metric(
                                        "ise3_detail_cache_coverage",
                                        'cache="ers_tacacs_user"',
                                    ),
                                    "coverage",
                                ),
                                query(
                                    metric(
                                        "ise3_detail_cache_entries",
                                        'cache="ers_tacacs_user"',
                                    ),
                                    "entries",
                                    ref="B",
                                ),
                                query(
                                    metric(
                                        "ise3_detail_fetches_deferred",
                                        'cache="ers_tacacs_user"',
                                    ),
                                    "deferred",
                                    ref="C",
                                ),
                            ],
                        )
                    ),
                    sized(
                        ts(
                            "Policy rule count coverage",
                            "Coverage, cached policy sets, and deferred work for "
                            "the converging authentication/authorization rule "
                            "inventory.",
                            [
                                query(
                                    metric(
                                        "ise3_detail_cache_coverage",
                                        'cache="openapi_tacacs_policy_rules"',
                                    ),
                                    "coverage",
                                ),
                                query(
                                    metric(
                                        "ise3_detail_cache_entries",
                                        'cache="openapi_tacacs_policy_rules"',
                                    ),
                                    "cached policy sets",
                                    ref="B",
                                ),
                                query(
                                    metric(
                                        "ise3_detail_fetches_deferred",
                                        'cache="openapi_tacacs_policy_rules"',
                                    ),
                                    "deferred",
                                    ref="C",
                                ),
                            ],
                        )
                    ),
                    sized(
                        ts(
                            "Policy rule refresh failures",
                            "Failed policy-rule detail reads by bounded result "
                            "class, without discarding previously cached counts.",
                            [
                                query(
                                    "sum by (result) (rate("
                                    + metric(
                                        "ise3_detail_fetches_total",
                                        'cache="openapi_tacacs_policy_rules",result=~"failed|empty"',
                                    )
                                    + "[15m]))",
                                    "{{result}}",
                                )
                            ],
                        )
                    ),
                ),
            ),
            (
                "Authentication and authorization",
                (
                    sized(
                        bar(
                            "Authentication by account and result",
                            "Passed and failed TACACS authentications for selected "
                            "accounts in the reporting window.",
                            [
                                instant(
                                    "sort_desc("
                                    + metric(
                                        "ise3_tacacs_authentications",
                                        'dimension="username",value=~"$username"',
                                    )
                                    + ")",
                                    "{{value}} · {{status}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        bar(
                            "Authentication by device and result",
                            "Worst-first bounded device authentication view; the "
                            "coverage panel states how much was published.",
                            [
                                instant(
                                    "sort_desc("
                                    + metric(
                                        "ise3_tacacs_authentications",
                                        'dimension="device"',
                                    )
                                    + ")",
                                    "{{value}} · {{status}}",
                                )
                            ],
                            data_links=(device_drilldown,),
                        )
                    ),
                    sized(
                        bar(
                            "Authentication by ops owner",
                            "Complete operational-owner rollup of TACACS "
                            "authentication outcomes.",
                            [
                                instant(
                                    "sort_desc("
                                    + metric(
                                        "ise3_tacacs_authentications",
                                        'dimension="ops_owner"',
                                    )
                                    + ")",
                                    "{{value}} · {{status}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        bar(
                            "Authorization by account and result",
                            "Passed and failed TACACS authorizations for selected "
                            "accounts in the reporting window.",
                            [
                                instant(
                                    "sort_desc("
                                    + metric(
                                        "ise3_tacacs_authorizations",
                                        'dimension="username",value=~"$username"',
                                    )
                                    + ")",
                                    "{{value}} · {{status}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        bar(
                            "Authorization by device and result",
                            "Worst-first bounded device authorization view with "
                            "explicit published-versus-total coverage.",
                            [
                                instant(
                                    "sort_desc("
                                    + metric(
                                        "ise3_tacacs_authorizations",
                                        'dimension="device"',
                                    )
                                    + ")",
                                    "{{value}} · {{status}}",
                                )
                            ],
                            data_links=(device_drilldown,),
                        )
                    ),
                    sized(
                        bar(
                            "Authorization by ops owner",
                            "Complete operational-owner rollup of TACACS "
                            "authorization outcomes.",
                            [
                                instant(
                                    "sort_desc("
                                    + metric(
                                        "ise3_tacacs_authorizations",
                                        'dimension="ops_owner"',
                                    )
                                    + ")",
                                    "{{value}} · {{status}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        tbl(
                            "Authorization policy, profile, command set and account",
                            "Worst-first bounded authorization evidence retaining "
                            "the account, device, matched policy, shell profile, "
                            "command set, and result together.",
                            [
                                instant(
                                    "sort_desc("
                                    + metric(
                                        "ise3_tacacs_authorization_details",
                                        'username=~"$username"',
                                    )
                                    + ")",
                                    "{{username}} · {{device}} · {{policy}} · "
                                    "{{shell_profile}} · {{command_set}} · {{status}}",
                                )
                            ],
                            columns=("authorizations",),
                        ),
                        10,
                        24,
                    ),
                ),
            ),
            (
                "Command accounting and bounds",
                (
                    sized(
                        bar(
                            "Command families",
                            "TACACS accounting records grouped by the first "
                            "command token, never by full operator-entered text.",
                            [
                                instant(
                                    "sort_desc("
                                    + metric(
                                        "ise3_tacacs_commands",
                                        'dimension="command_family"',
                                    )
                                    + ")",
                                    "{{value}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        bar(
                            "Commands by account",
                            "TACACS command accounting volume for each selected "
                            "internal account.",
                            [
                                instant(
                                    "sort_desc("
                                    + metric(
                                        "ise3_tacacs_commands",
                                        'dimension="username",value=~"$username"',
                                    )
                                    + ")",
                                    "{{value}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        bar(
                            "Commands by device",
                            "Worst-first bounded device command-accounting view "
                            "for troubleshooting administrative activity.",
                            [
                                instant(
                                    "sort_desc("
                                    + metric(
                                        "ise3_tacacs_commands",
                                        'dimension="device"',
                                    )
                                    + ")",
                                    "{{value}}",
                                )
                            ],
                            data_links=(device_drilldown,),
                        )
                    ),
                    sized(
                        bar(
                            "Commands by ops owner",
                            "Complete operational-owner rollup of TACACS command "
                            "accounting activity.",
                            [
                                instant(
                                    "sort_desc("
                                    + metric(
                                        "ise3_tacacs_commands",
                                        'dimension="ops_owner"',
                                    )
                                    + ")",
                                    "{{value}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        tbl(
                            "Bounded device coverage",
                            "Published versus existing device groups for TACACS "
                            "authentication, authorization, and accounting.",
                            [
                                instant(
                                    metric(
                                        "ise3_topk_groups_returned",
                                        'dataset="tacacs_activity",breakdown=~".*device"',
                                    ),
                                    "{{breakdown}} published",
                                ),
                                instant(
                                    metric(
                                        "ise3_topk_groups_total",
                                        'dataset="tacacs_activity",breakdown=~".*device"',
                                    ),
                                    "{{breakdown}} total",
                                    "B",
                                ),
                                instant(
                                    metric(
                                        "ise3_topk_truncated",
                                        'dataset="tacacs_activity",breakdown=~".*device"',
                                    ),
                                    "{{breakdown}} truncated",
                                    "C",
                                ),
                            ],
                            columns=("published", "total", "truncated"),
                            column_overrides=(
                                by_column(
                                    "truncated", mappings=TRUNCATED,
                                    thresholds=NONZERO_WARNING,
                                    colour_cells=True,
                                ),
                            ),
                        )
                    ),
                    sized(
                        stat_panel(
                            "NAD directory coverage",
                            "NAD classification entries available to roll device "
                            "activity onto operational owners.",
                            [
                                instant(
                                    metric("ise3_nad_directory_entries"),
                                    "entries",
                                )
                            ],
                        ),
                        5,
                        24,
                    ),
                ),
            ),
        ),
        variables=(username,),
    )


def sources_dashboard():
    return assemble(
        "ISE Exporter 3 — Sources",
        "ise3-sources",
        "Which source supplies each dataset, when it changed, and why. A "
        "fallback may answer a narrower question than the preferred source.",
        (
            (
                "Active source",
                (
                    sized(
                        tbl(
                            "Live source per dataset",
                            "The provider currently supplying each dataset; a "
                            "missing row means no provider is usable.",
                            [
                                instant(
                                    metric(
                                        "ise3_dataset_provider_active",
                                        "",
                                    )
                                    + " == 1",
                                    "{{dataset}} · {{provider}}",
                                )
                            ],
                            columns=(None,),
                        ),
                        9,
                        12,
                    ),
                    sized(
                        ts(
                            "Datasets not on their preferred source",
                            "Non-zero means a dataset fell back, making a change "
                            "in data semantics visible rather than silent.",
                            [
                                query(
                                    f"sum({metric('ise3_dataset_provider_degraded')})",
                                    "degraded",
                                ),
                                query(
                                    metric(
                                        "ise3_dataset_provider_degraded",
                                        "",
                                    )
                                    + " == 1",
                                    "{{dataset}}",
                                    ref="B",
                                ),
                            ],
                            thresholds=NONZERO_CRITICAL,
                        ),
                        9,
                        12,
                    ),
                ),
            ),
            (
                "Why it changed",
                (
                    sized(
                        tbl(
                            "Reason each fallback engaged",
                            "Bounded reason recorded when a dataset stepped to a "
                            "different provider.",
                            [
                                instant(
                                    metric(
                                        "ise3_dataset_provider_reason_info"
                                    )
                                    + " == 1",
                                    "{{dataset}} · {{provider}} · {{reason}}",
                                )
                            ],
                            columns=(None,),
                        )
                    ),
                    sized(
                        tbl(
                            "Declared but unusable sources",
                            "Configured sources this build cannot run because a "
                            "target, capability, or reporting view is unavailable.",
                            [
                                instant(
                                    metric(
                                        "ise3_dataset_provider_available"
                                    )
                                    + " == 0",
                                    "{{dataset}} · {{provider}}",
                                )
                            ],
                            columns=(None,),
                        )
                    ),
                ),
            ),
            (
                "Collection",
                (
                    sized(
                        tbl(
                            "Dataset health",
                            "Last-attempt success and freshness for each active "
                            "dataset provider.",
                            [
                                instant(
                                    metric("ise3_dataset_up"),
                                    "{{dataset}} · {{provider}} up",
                                ),
                                instant(
                                    metric("ise3_dataset_fresh"),
                                    "{{dataset}} · {{provider}} fresh",
                                    "B",
                                ),
                            ],
                            columns=("up", "fresh"),
                            column_overrides=(
                                by_column("up", **BOOLEAN_CELL),
                                by_column("fresh", **BOOLEAN_CELL),
                            ),
                        )
                    ),
                    sized(
                        tbl(
                            "Latest failure per dataset",
                            "One bounded operator explanation per failing dataset; "
                            "empty is the healthy state.",
                            [
                                instant(
                                    metric(
                                        "ise3_dataset_last_failure_detail_info"
                                    )
                                    + " == 1",
                                    "{{dataset}} · {{reason}} · {{detail}}",
                                )
                            ],
                            columns=(None,),
                        )
                    ),
                ),
            ),
        ),
        refresh="1m",
    )


def load_dashboard():
    return assemble(
        "ISE Exporter 3 — Load and Budget",
        "ise3-load",
        "Planned load from provider declarations beside measured requests and "
        "database time, target ceilings, enforced pacing, and scheduler queues.",
        (
            (
                "Budget",
                (
                    sized(
                        stat_panel(
                            "Budget used",
                            "Planned load as a fraction of each declared target "
                            "ceiling; above one is over budget.",
                            [
                                instant(
                                    metric("ise3_load_budget_utilisation"),
                                    "{{target}}",
                                )
                            ],
                            unit="percentunit",
                            thresholds=BUDGET_USED,
                            minimum=0,
                        ),
                        5,
                        24,
                    ),
                    sized(
                        ts(
                            "Requests per hour: planned against measured",
                            "Declared request cost beside the measured rate "
                            "actually leaving the exporter.",
                            [
                                query(
                                    metric(
                                        "ise3_load_planned_requests_per_hour"
                                    ),
                                    "{{target}} planned",
                                ),
                                query(
                                    "rate("
                                    + metric(
                                        "ise3_load_measured_requests_total"
                                    )
                                    + "[15m]) * 3600",
                                    "{{target}} measured",
                                    ref="B",
                                ),
                            ],
                            unit="reqps",
                        )
                    ),
                    sized(
                        ts(
                            "Request ceiling and enforcement",
                            "Configured request ceiling beside the rate currently "
                            "enforced by the token bucket.",
                            [
                                query(
                                    metric(
                                        "ise3_load_budget_requests_per_hour"
                                    ),
                                    "{{target}} ceiling",
                                ),
                                query(
                                    metric(
                                        "ise3_budget_enforced_requests_per_hour"
                                    ),
                                    "{{target}} enforced",
                                    ref="B",
                                ),
                            ],
                        )
                    ),
                ),
            ),
            (
                "Data Connect duty cycle",
                (
                    sized(
                        ts(
                            "Duty cycle: planned against measured",
                            "Planned wall-clock share in Oracle beside measured "
                            "statement time and the enforced ceiling.",
                            [
                                query(
                                    metric(
                                        "ise3_load_planned_duty_cycle_percent",
                                        'target="oracle"',
                                    ),
                                    "planned",
                                ),
                                query(
                                    "rate("
                                    + metric(
                                        "ise3_load_measured_db_seconds_total",
                                        'target="oracle"',
                                    )
                                    + "[15m]) * 100",
                                    "measured",
                                    ref="B",
                                ),
                                query(
                                    metric(
                                        "ise3_dataconnect_effective_duty_cycle_percent"
                                    ),
                                    "ceiling",
                                    ref="C",
                                ),
                            ],
                            unit="percent",
                        )
                    ),
                    sized(
                        ts(
                            "Cooldown imposed on reporting datasets",
                            "Global wait after each statement, which is how the "
                            "configured duty-cycle budget is enforced.",
                            [
                                query(
                                    metric(
                                        "ise3_dataconnect_query_cooldown_seconds"
                                    ),
                                    "{{view}}",
                                )
                            ],
                            unit="s",
                        )
                    ),
                ),
            ),
            (
                "Statement cost and scheduler",
                (
                    sized(
                        ts(
                            "Statement duration by view",
                            "Latest successful statement duration for every "
                            "reporting view.",
                            [
                                query(
                                    metric(
                                        "ise3_dataconnect_query_last_duration_seconds",
                                        'result="success"',
                                    ),
                                    "{{view}}",
                                )
                            ],
                            unit="s",
                        )
                    ),
                    sized(
                        ts(
                            "Rows returned by view",
                            "Rows returned by each reporting statement after "
                            "database-side aggregation.",
                            [
                                query(
                                    metric("ise3_dataconnect_query_rows"),
                                    "{{view}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        ts(
                            "Lane queue depth",
                            "Datasets waiting on each serialized target lane; "
                            "sustained depth means cadence is not achievable.",
                            [
                                query(
                                    metric("ise3_lane_queue_depth"),
                                    "{{target}}",
                                )
                            ],
                        )
                    ),
                    sized(
                        ts(
                            "Collection duration by dataset",
                            "Latest collection duration by dataset and active "
                            "provider.",
                            [
                                query(
                                    metric(
                                        "ise3_dataset_collection_duration_seconds"
                                    ),
                                    "{{dataset}} · {{provider}}",
                                )
                            ],
                            unit="s",
                        )
                    ),
                ),
            ),
        ),
        refresh="1m",
    )


DASHBOARDS = {
    "ise3-overview": overview_dashboard,
    "ise3-access": access_dashboard,
    "ise3-endpoints": endpoints_dashboard,
    "ise3-health": health_dashboard,
    "ise3-pan-mnt": pan_mnt_dashboard,
    "ise3-psn": psn_dashboard,
    "ise3-secureclient": secureclient_dashboard,
    "ise3-tacacs": tacacs_dashboard,
    "ise3-sources": sources_dashboard,
    "ise3-load": load_dashboard,
}


def build(output):
    output.mkdir(parents=True, exist_ok=True)
    encoder = JSONEncoder(sort_keys=True, indent=2)
    written = []
    expected = {f"{name}.json" for name in DASHBOARDS}
    for stale in output.glob("ise3-*.json"):
        if stale.name not in expected:
            stale.unlink()
    for name, builder in DASHBOARDS.items():
        path = output / f"{name}.json"
        path.write_text(encoder.encode(builder().build()) + "\n")
        written.append(path)
    return written


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default="dashboards3",
        type=pathlib.Path,
        help="directory to write dashboard JSON into",
    )
    args = parser.parse_args(argv)
    for path in build(args.out):
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
