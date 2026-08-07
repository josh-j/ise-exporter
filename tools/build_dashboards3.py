"""Generate the ise-exporter3 Grafana dashboard set.

The set is organised by *audience and time horizon*, not by data source, which
is the first principle in DASHBOARD_DESIGN_PRINCIPLES.md:

  Tier 1  triage      ise3-triage      is ISE serving authentications, now?
  Tier 2  diagnostic  ise3-access      why are RADIUS authentications failing?
                      ise3-psn         are the policy service nodes coping?
                      ise3-control     is the PAN/MnT control plane healthy?
                      ise3-endpoints   what is on the network, and what is not?
                      ise3-nad         which switch or router is failing, how?
                      ise3-posture     is the fleet compliant?
                      ise3-tacacs      who is administering the devices?
  Tier 3  exporter    ise3-pipeline    is the exporter's view of ISE current?
                      ise3-load        what is the exporter costing ISE?
  Tier 4  capacity    ise3-capacity    where does this run out?

Tier 1 is the only dashboard that should ever be opened first. Every panel on
it links down into the tier that explains it, and every tier-2 dashboard links
back up. Tier 3 is about the *exporter*, not about ISE: an operator reading it
is asking whether to believe the other ten.

Two design rules are enforced by construction rather than by review:

  * A breakdown is a table, not a bar gauge. A bar gauge cannot scroll, so the
    old set capped every breakdown to its top 25 bars and told the operator so
    in the description. A table shows all of it, sorts worst-first, and needs
    no cap. Bar gauges survive only for bounded, low-cardinality comparisons
    where the bar length is the point.
  * Nothing renders ISE data without saying whether that data is current.
    gate() drops stale series, and every ISE dashboard carries a two-panel
    trust pair in its first row.

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
    statetimeline,
    table,
    text,
    timeseries,
)
from grafana_foundation_sdk.cog.encoder import JSONEncoder
from grafana_foundation_sdk.models.common import (
    BarGaugeDisplayMode,
    BigValueColorMode,
    BigValueGraphMode,
    BigValueTextMode,
    GraphTresholdsStyleMode,
    StackingMode,
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
    FieldColor,
    FieldColorModeId,
    Threshold,
    ThresholdsMode,
    ValueMap,
    ValueMappingResult,
)
from grafana_foundation_sdk.models.prometheus import PromQueryFormat
from grafana_foundation_sdk.models.text import TextMode


DATASOURCE = DataSourceRef(type_val="prometheus", uid="${prometheus}")
TAGS = ["ise", "ise-exporter3"]


# ---------------------------------------------------------------------------
# Tiers
#
# A dashboard's default window and refresh are properties of the question it
# answers, not of the data behind it. Triage is read during an incident and has
# to be current; a capacity trend read at 30 days is noise at one minute.
# ---------------------------------------------------------------------------

TRIAGE = {"window": "now-3h", "refresh": "1m"}
DIAGNOSTIC = {"window": "now-6h", "refresh": "5m"}
EXPORTER = {"window": "now-6h", "refresh": "1m"}
COST = {"window": "now-24h", "refresh": "5m"}
CAPACITY = {"window": "now-30d", "refresh": "30m"}

# Grid vocabulary. Grafana's row is 24 units wide; a handful of standard widths
# keeps panels aligned, and alignment is what lets an operator correlate a spike
# vertically down the page.
FULL, HALF, THIRD, QUARTER, SIXTH = 24, 12, 8, 6, 4
FIFTH, EIGHTH = 5, 3
STAT_H, PANEL_H, TALL_H, STRIP_H = 4, 8, 10, 6


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

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
    """Select one exporter metric.

    One exporter serves one ISE deployment, so there is no deployment dimension
    to select on here: a second deployment is a second Prometheus job, not a
    label to filter. Everything below therefore aggregates without `instance`,
    and tables drop it as scrape metadata -- see NOISE_COLUMNS.
    """
    return f"{name}{{{selectors}}}" if selectors else name


def _aggregate(operator, by, expr):
    """`op(expr)` or `op by (labels) (expr)`, with no empty `by ()` to explain."""
    return f"{operator} by ({by}) ({expr})" if by else f"{operator}({expr})"


def _healthy(name, selectors, by=""):
    """One dataset-health metric, restricted to the provider actually serving.

    A dataset publishes health per candidate provider. Only the active one is
    the truth about whether the data on screen is good, so every readiness
    expression joins against ise3_dataset_provider_active first.
    """
    health = metric(name, selectors)
    active = metric("ise3_dataset_provider_active", selectors)
    return _aggregate(
        "max", by,
        f"{health} and on(dataset,provider) ({active} == 1)")


def active_health(name, dataset):
    return _healthy(name, f'dataset="{dataset}"')


def ready(dataset):
    """Readiness as a filter: no series at all when the dataset is not ready."""
    return (
        f"(({active_health('ise3_dataset_up', dataset)}) == 1) and "
        f"(({active_health('ise3_dataset_fresh', dataset)}) == 1)"
    )


def ready_bool(dataset):
    """Readiness as an explicit 0/1 series, for display only.

    ready() filters with `== 1`, so an unready dataset returns no series and a
    stat renders "No data" rather than a failure. This form always returns a
    value. It must never be used by gate(): a 0-valued series still matches
    `and on()`, which would silently stop stale data being hidden.
    """
    return (
        f"(({active_health('ise3_dataset_up', dataset)}) == bool 1) * "
        f"(({active_health('ise3_dataset_fresh', dataset)}) == bool 1)"
    )


def readiness_per_dataset(selectors=""):
    """Every selected dataset's readiness as a 0/1 series keyed by dataset.

    This is what makes readiness legible over time rather than as a number:
    fed to a state timeline it draws exactly when each dataset stopped being
    trustworthy, which is the first thing anyone asks about a graph with a step
    in it.
    """
    up = _healthy("ise3_dataset_up", selectors, "dataset")
    fresh = _healthy("ise3_dataset_fresh", selectors, "dataset")
    return f"(({up}) == bool 1) * (({fresh}) == bool 1)"


def _dataset_selector(datasets):
    return f'dataset=~"{"|".join(datasets)}"'


def ready_share(datasets):
    """The fraction of one dashboard's own datasets that are trustworthy."""
    return f"avg({readiness_per_dataset(_dataset_selector(datasets))})"


def oldest_collection(datasets):
    """Seconds since the least recently collected of these datasets succeeded."""
    stamps = _healthy(
        "ise3_dataset_last_success_timestamp",
        _dataset_selector(datasets),
        "dataset",
    )
    return f"max(time() - ({stamps}))"


def gate(expr, dataset):
    """Drop stale data instead of showing it as current.

    `on()` rather than `on(instance)`: readiness aggregates to a single
    unlabelled series, so the join key is deliberately empty.
    """
    return f"({expr}) and on() ({ready(dataset)})"


# A converging cache is not a failure, so `ready` stays 1 while it fills. Panels
# fed from the whole active list and panels fed from cached per-entity detail
# therefore disagree about how many endpoints exist until it is warm -- honest,
# but it reads as a bug. Gate the detail-fed ones on coverage so they blank
# rather than under-report next to a complete neighbour.
DETAIL_COVERAGE_FLOOR = 0.99


def covered(expr, dataset, cache):
    """Drop a detail-fed panel while its cache is still filling."""
    coverage = metric("ise3_detail_cache_coverage", f'cache="{cache}"')
    return (
        f"({gate(expr, dataset)}) and on() "
        f"((max({coverage})) >= {DETAIL_COVERAGE_FLOOR})"
    )


def share(numerator, denominator):
    """A ratio that stays defined when nothing happened in the window."""
    return f"({numerator}) / clamp_min(({denominator}), 1)"


def summed(expr, by=""):
    return _aggregate("sum", by, expr)


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
    """One row of the triage table: a count, labelled with what it means.

    Unioning unlike metrics into one table needs a label they all share, and
    the aggregation that makes each row a comparable count drops every label
    the metric carried, so the meaning has to be attached afterwards.
    """
    return f'label_replace({expr}, "issue", "{issue}", "", "")'


def offender(issue, expr, labels=(), detail=None):
    """One row of the named-instance table: which thing, by name.

    attention() counts a class of problem; a count is enough to know something
    is wrong and never enough to know what to do about it. This names the
    instances inside that class instead: the identifying labels are joined into
    one `detail` string so unlike metrics can share a column, and the result is
    aggregated to (issue, detail) so no metric's own labels leak into the table
    as a column every other row leaves empty.

    `detail` names the row directly for a metric whose subject is the whole
    deployment and which therefore carries no label to name it.
    """
    if detail is not None:
        named = f'label_replace({expr}, "detail", "{detail}", "", "")'
    else:
        keys = ", ".join('"%s"' % name for name in labels)
        named = f'label_join({expr}, "detail", " · ", {keys})'
    return f'max by (issue, detail) (label_replace({named}, "issue", "{issue}", "", ""))'


def with_reason(info, condition):
    """An info metric's reason label, restricted to the datasets in trouble.

    The reason lives on an info series and the trouble lives on a counter, so
    the two have to be joined. Two things make that join awkward, and both are
    handled here rather than at each call site. A dataset carries one reason
    per *candidate* provider, so the info side is restricted to the provider
    actually serving — otherwise one failing dataset produces a row per
    provider and the operator reads two different reasons for one fault. And
    the info side is deliberately the many side of the join: a group_left with
    a duplicated right-hand series fails the whole query rather than dropping a
    row, which would blank the panel instead of losing one line of it.
    """
    active = f"({metric('ise3_dataset_provider_active')} == 1)"
    preferred = (f"max by (dataset, reason) "
                 f"({info} and on(dataset, provider) {active})")
    whoever = f"max by (dataset, reason) ({info})"
    # Not simply the active provider's reason: a recovery probe fails under the
    # preferred source's label while the active source collects, so a dataset
    # in trouble can hold no reason at all under the provider serving it.
    # Preferring the active one and falling back to whichever provider recorded
    # a reason keeps the row rather than dropping it to say nothing.
    reason = f"({preferred} or ({whoever} unless on(dataset) ({preferred})))"
    return f"{reason} * on(dataset) group_left() (max by (dataset) ({condition}))"


# ---------------------------------------------------------------------------
# Colour
#
# Red, amber and green mean state and nothing else. Series colour is pinned by
# name so "failed" is the same red on every dashboard and does not shuffle when
# a neighbouring series drops out.
# ---------------------------------------------------------------------------

NONZERO_CRITICAL = ((None, "green"), (1, "red"))
NONZERO_WARNING = ((None, "green"), (1, "orange"))
REQUIRED_BOOLEAN = ((None, "red"), (1, "green"))
NEUTRAL = ((None, "text"),)
# For a quantity that is signed rather than good or bad. Draws the zero line and
# nothing else: on a delta panel the sign is the whole reading, and red for
# "lost sessions" would claim a node shedding load is failing when a planned
# failover looks identical.
ZERO_REFERENCE = ((None, "text"), (0, "text"))
UTILISATION = ((None, "green"), (80, "orange"), (90, "red"))
RATIO_HIGH_IS_GOOD = ((None, "red"), (0.85, "orange"), (0.95, "green"))
COVERAGE = ((None, "red"), (0.8, "orange"), (0.95, "green"))
BUDGET_USED = ((None, "green"), (0.8, "orange"), (1.0, "red"))
# Collection age, in seconds. A five-minute dataset that has not landed for a
# quarter of an hour is late; an hour is a stall.
COLLECTION_AGE = ((None, "green"), (900, "orange"), (3600, "red"))
LATENCY_SECONDS = ((None, "green"), (1, "orange"), (3, "red"))
# A daily backup that has not run is the first sign of a stalled PAN, so the
# triage table and the age panel must agree on when "yesterday" has passed.
BACKUP_STALE_HOURS = 26
BACKUP_AGE_HOURS = ((None, "green"), (BACKUP_STALE_HOURS, "orange"), (50, "red"))
# Certificate runway, in days. Amber at a quarter's notice, red inside a month.
CERT_RUNWAY_DAYS = ((None, "red"), (30, "orange"), (90, "green"))

# Value mappings for the booleans ISE reports as 1 and 0.
YES_NO = ((1, "Yes", "green"), (0, "No", "orange"))
CONFIGURED = ((1, "Configured", "green"), (0, "Not configured", "red"))
SUPPORTED = ((1, "Supported", "green"), (0, "Unsupported", "red"))
ENABLED = ((1, "Yes", "green"), (0, "No", "red"))
TRUNCATED = ((1, "Truncated", "orange"), (0, "Complete", "green"))
READINESS = ((1, "Trustworthy", "green"), (0, "NOT CURRENT", "red"))
CONNECTED = ((1, "Connected", "green"), (0, "Not connected", "red"))
RISK = ((1, "At risk", "red"), (0, "Clear", "green"))

# Blank panels are ambiguous on call: these say which kind of blank it is.
NO_DATA_EXPORTER = "no data — exporter absent?"
NO_DATA_STALE = "no data — stale, see Collection pipeline"
NO_DATA_CLEAN = "none — nothing to see"


def colour(name, value):
    return DynamicConfigValue(
        name, FieldColor(mode=FieldColorModeId.FIXED, fixed_color=value)
    )


def _pinned(pattern, value):
    return pattern, [colour("color", value)]


# Outcome is the one vocabulary shared by RADIUS, TACACS and posture panels, so
# it is pinned once and applied wherever those words appear in a legend.
OUTCOME_COLOURS = (
    _pinned(".*(passed|Passed|compliant|Compliant|success).*", "green"),
    _pinned(".*(failed|Failed|non-compliant|NonCompliant|error).*", "red"),
    _pinned(".*(unknown|Unknown|pending|Pending).*", "text"),
)


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
                    text=text_value, color=color, index=index
                )
                for index, (value, text_value, color) in enumerate(entries)
            }
        )
    ]


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------

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

    `${__url_time_range}` reproduces the window being looked at; the rest of
    the context is whatever variables the caller names.
    """
    parameters = ["${__url_time_range}"]
    parameters += [f"var-{name}={value}" for name, value in variables.items()]
    return _DataLink(title, f"/d/{uid}?" + "&".join(parameters))


# The two ways a clicked value names an entity. A time series or bar gauge
# clicks a series, so the label is read off the field; a table clicks a row, so
# it is read off the named column of that row.
def by_series(label):
    return "${__field.labels." + label + "}"


def by_row(column):
    return "${__data.fields." + column + "}"


def by_ref(ref, *, unit=None, thresholds=None, mappings=None,
           minimum=None, maximum=None, links=()):
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
    if links:
        properties.append(DynamicConfigValue("links", list(links)))
    return ref, properties


def by_column(name, *, unit=None, thresholds=None, mappings=None,
              colour_cells=False, links=(), width=None):
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
    if width is not None:
        properties.append(DynamicConfigValue("custom.width", width))
    if links:
        # Scoped to one column so only the identifying cell is clickable; a
        # panel-wide data link turns every cell of every row into the same link.
        properties.append(DynamicConfigValue("links", list(links)))
    return name, properties


# The column shapes that repeat across the table set.
BOOLEAN_CELL = {
    "mappings": ENABLED,
    "thresholds": REQUIRED_BOOLEAN,
    "colour_cells": True,
}
SECONDS_CELL = {"unit": "s"}


def _visual_state(panel, thresholds, mappings, minimum, maximum, overrides,
                  data_links=(), regexp_overrides=()):
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
    for pattern, properties in regexp_overrides:
        panel = panel.override_by_regexp(pattern, properties)
    return panel


# ---------------------------------------------------------------------------
# Variables
# ---------------------------------------------------------------------------

def datasource_variable():
    """File-provisioned dashboards cannot hardcode a datasource uid."""
    return (
        db.DatasourceVariable("prometheus")
        .label("Prometheus")
        .type("prometheus")
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


# ---------------------------------------------------------------------------
# Annotations
# ---------------------------------------------------------------------------

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


def annotation(name, expr, *, title, body, colour_name, step="1m"):
    """One class of change event, painted across every panel of a dashboard."""
    return _PrometheusAnnotationQuery(
        {"titleFormat": title, "textFormat": body, "step": step},
        name=name,
        datasource=DATASOURCE,
        enable=True,
        # hide=False keeps each annotation's own toggle in the dashboard bar,
        # so one class of event can be switched off without editing anything.
        hide=False,
        icon_color=colour_name,
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
            body="{{dataset}} is now served by {{provider}}",
            colour_name="purple",
        ),
        annotation(
            # Aggregated to the node: node_state is one series per state, so an
            # unaggregated changes() fires twice for a single transition, and
            # again whenever the roles or services labels churn.
            "Node state changed",
            "max by (node) (changes("
            f"{metric('ise3_deployment_node_state')}[10m])) > 0",
            title="Node state changed: {{node}}",
            body="{{node}} changed deployment state",
            colour_name="orange",
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
            body="exporter {{version}} targeting ISE {{target_ise_release}}",
            colour_name="blue",
        ),
    )


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------

def ts(title, description, targets, *, unit="short", stacked=False,
       filled=False, thresholds=None, mappings=None, minimum=None,
       maximum=None, overrides=(), series_colours=(), legend_calcs=None,
       legend_placement="bottom"):
    panel = (
        timeseries.Panel()
        .title(title)
        .description(description)
        .datasource(DATASOURCE)
        .unit(unit)
        .line_width(2)
        .fill_opacity(25 if (stacked or filled) else 0)
        .legend(
            common.VizLegendOptions()
            .display_mode("table")
            .placement(legend_placement)
            .calcs(list(legend_calcs if legend_calcs is not None
                        else ["lastNotNull", "max"]))
        )
    )
    if stacked:
        # Stacking is only honest for parts of a whole. Every caller passing it
        # here is drawing an outcome split (passed/failed) or a status split.
        panel = panel.stacking(
            common.StackingConfig().mode(StackingMode.NORMAL).group("A")
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
        panel, thresholds, mappings, minimum, maximum, overrides,
        regexp_overrides=series_colours,
    )


# Prometheus table format emits these beside the label columns. All four are
# scrape metadata rather than anything ISE said: `instance` and `job` name the
# exporter that answered, which is the same exporter on every row of every
# table here, and a column that cannot vary is width spent saying nothing.
NOISE_COLUMNS = ("Time", "__name__", "job", "instance")

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


def _table_transformations(count, columns, sort, labels):
    """Turn one frame per query into a single joined, labelled table.

    Without this a multi-target table renders as a frame picker showing one
    query at a time. `merge` joins the frames on every column they share, which
    is why the noise columns are filtered off first: an instant query carries a
    Time column, and a shared column is part of the join key.
    """
    hidden = list(NOISE_COLUMNS)
    renamed = dict(labels or {})
    # `provider` names the source that answered. On a table about ISE that is
    # the same source on every row, so it is width spent saying nothing and it
    # splits a merge that would otherwise have joined two targets into one row.
    # It survives only where the table is *about* the source, which a panel
    # declares by renaming it into a column heading.
    if "provider" not in renamed:
        hidden.append("provider")
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
        mappings=None, overrides=(), column_overrides=(), labels=None):
    """A joined table.

    `columns` names one value column per target, in refId order, or None to
    drop that target's value and keep only the labels it joins in. `labels`
    renames a raw Prometheus label column into something an operator reads.
    `sort` reorders rows worst-first where the query itself does not already.
    """
    assert len(columns) == len(targets), title
    panel = (
        table.Panel()
        .title(title)
        .description(description)
        .datasource(DATASOURCE)
        .transformations(_table_transformations(len(targets), columns, sort, labels))
    )
    for target in targets:
        # Label columns only exist in table format; time_series format gives
        # the panel one series per row and nothing to join on.
        panel = panel.with_target(target.format(PromQueryFormat.TABLE))
    panel = _visual_state(panel, thresholds, mappings, None, None, overrides)
    for column, properties in column_overrides:
        panel = panel.override_by_name(column, properties)
    return panel


def ranked(title, description, expr, *, label, header, unit="short",
           descending=True, value_thresholds=None, colour_cells=False,
           links=(), mappings=None, label_header=None):
    """A complete breakdown of one labelled metric, worst first.

    This replaces the capped bar gauge the old set used for every breakdown. A
    table scrolls, so there is no cap, no truncation note, and no risk that the
    device worth looking at fell off the bottom of the visual.
    """
    column_overrides = [
        by_column(header, unit=unit, thresholds=value_thresholds,
                  mappings=mappings, colour_cells=colour_cells)
    ]
    if links:
        column_overrides.append(by_column(label, links=list(links)))
    return tbl(
        title,
        description,
        [instant(expr)],
        columns=[header],
        sort=(header, descending),
        labels={label: label_header} if label_header else None,
        column_overrides=column_overrides,
    )


def stat_panel(title, description, targets, *, unit="short", thresholds=None,
               mappings=None, minimum=None, maximum=None, overrides=(),
               no_value=None, data_links=(), sparkline=False, text_mode=None,
               colour_mode=BigValueColorMode.VALUE):
    panel = (
        stat.Panel()
        .title(title)
        .description(description)
        .datasource(DATASOURCE)
        .unit(unit)
        .color_mode(colour_mode)
        .graph_mode(
            BigValueGraphMode.AREA if sparkline else BigValueGraphMode.NONE
        )
    )
    if text_mode is not None:
        panel = panel.text_mode(text_mode)
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
    """A bar gauge.

    Reserved for bounded, low-cardinality comparisons -- coverage fractions,
    role counts, a handful of named categories -- where the length of the bar
    against a common maximum is the message. Anything unbounded is a table.
    """
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


def states(title, description, targets, *, mappings=READINESS,
           thresholds=REQUIRED_BOOLEAN):
    """A state timeline.

    The right shape for a boolean that persists: it answers "was this good, and
    if not, from when until when" in one read, which neither a stat nor a table
    of current values can do.
    """
    panel = (
        statetimeline.Panel()
        .title(title)
        .description(description)
        .datasource(DATASOURCE)
        .merge_values(True)
        .show_value("never")
        .row_height(0.85)
        .fill_opacity(80)
        .legend(
            common.VizLegendOptions()
            .display_mode("list")
            .placement("bottom")
            .calcs([])
        )
    )
    for target in targets:
        panel = panel.with_target(target)
    return _visual_state(panel, thresholds, mappings, None, None, ())


def note(title, description, body):
    """A markdown panel.

    Every dashboard ends with one. An unowned dashboard with no stated purpose
    rots, and the panel is where the purpose, the reading order, and the
    runbook pointer live where an operator will actually find them.
    """
    return (
        text.Panel()
        .title(title)
        .description(description)
        .datasource(DATASOURCE)
        .mode(TextMode.MARKDOWN)
        .content(body)
    )


def about(purpose, first, then, siblings):
    """The standard closing panel: what this is, how to read it, where next."""
    body = [f"**Purpose** — {purpose}", "", "**Read it in this order**", ""]
    body += [f"{index}. {step}" for index, step in enumerate(first, start=1)]
    body += ["", "**When something is red**", ""]
    body += [f"- {step}" for step in then]
    body += [
        "",
        "**Related dashboards** — " + ", ".join(siblings),
        "",
        "*Owner: set this to the team that answers the page. "
        "Runbook: link it here — a red panel with no runbook is a puzzle, "
        "not an alert.*",
    ]
    return note(
        "About this dashboard",
        "Purpose, reading order, and where to go next. Kept with the "
        "dashboard so the meaning cannot drift away from the panels.",
        "\n".join(body),
    )


def sized(panel, height=PANEL_H, span=HALF):
    return panel.height(height).span(span)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

# A third element on a section marks the row as folded away on load, for
# material that is needed during an investigation but not to start one.
COLLAPSED = "collapsed"


def base(title, uid, description, *, tier, variables=()):
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
        .refresh(tier["refresh"])
        .time(tier["window"], "now")
        .editable()
        .link(navigation)
        .with_variable(datasource_variable())
    )
    for change in change_annotations():
        dashboard = dashboard.annotation(change)
    for variable in variables:
        dashboard = dashboard.with_variable(variable)
    return dashboard


def assemble(title, uid, description, sections, *, tier, variables=()):
    dashboard = base(title, uid, description, tier=tier, variables=variables)
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


def trust_pair(datasets, *, span=EIGHTH):
    """The two panels that say whether anything else on the page is current.

    They sit at the end of the first row, after the outcome the operator came
    for: outcome first, then the caveat on it. Both link into the pipeline
    dashboard, which is where an untrustworthy answer gets explained.
    """
    to_pipeline = (drilldown("Why — collection pipeline", "ise3-pipeline"),)
    names = ", ".join(sorted(datasets))
    return [
        sized(
            stat_panel(
                "Data trustworthy",
                "Share of this dashboard's own datasets that both collected "
                f"successfully and are inside their freshness window ({names}). "
                "Below 100% means some panel here is blank or stale, and the "
                "pipeline dashboard says which.",
                [instant(ready_share(datasets))],
                unit="percentunit",
                thresholds=((None, "red"), (1, "green")),
                minimum=0,
                maximum=1,
                no_value=NO_DATA_EXPORTER,
                data_links=to_pipeline,
            ),
            STAT_H,
            span,
        ),
        sized(
            stat_panel(
                "Collection age",
                "Time since the least recently collected of this dashboard's "
                "datasets last succeeded. Every number on this page is at "
                "least this old; a value climbing past its collection interval "
                "means ISE is answering slowly or not at all.",
                [instant(oldest_collection(datasets))],
                unit="s",
                thresholds=COLLECTION_AGE,
                no_value=NO_DATA_EXPORTER,
                data_links=to_pipeline,
            ),
            STAT_H,
            span,
        ),
    ]


# ---------------------------------------------------------------------------
# Shared expressions
# ---------------------------------------------------------------------------

AUTH_PASSED = summed(
    gate(metric("ise3_radius_authentications", 'status="passed"'),
         "radius_reporting"))
AUTH_FAILED = summed(
    gate(metric("ise3_radius_authentications", 'status="failed"'),
         "radius_reporting"))
AUTH_TOTAL = summed(
    gate(metric("ise3_radius_authentications"), "radius_reporting"))
AUTH_SUCCESS_RATE = share(AUTH_PASSED, AUTH_TOTAL)

ACTIVE_SESSIONS = summed(
    gate(metric("ise3_active_sessions_total"), "active_sessions"))

NODES_DISCONNECTED = (
    "count((" +
    gate(metric("ise3_deployment_node_state", 'state!="Connected"'),
         "deployment") +
    ") == 1)"
)

NODE_CONNECTED = summed(
    gate(metric("ise3_deployment_node_state", 'state="Connected"'), "deployment"),
    by="node",
)


# ---------------------------------------------------------------------------
# Tier 1 — triage
# ---------------------------------------------------------------------------

# The Operations owner variable turns the diagnosis layer of the triage page
# into one owner's fix list. The symptom row above it stays fleet-wide: "is ISE
# serving right now" is not an owner-scoped question.
TRIAGE_OWNER = 'ops_owner=~"$ops_owner"'


# What an unclassified device is called in each attribute. classify() defaults
# the same way, so a device the exporter could not classify and a device it
# never saw share a bucket rather than splitting into two.
NAD_ATTRIBUTE_DEFAULTS = {
    "ops_owner": "unknown",
    "location": "Unknown",
    "device_type": "unknown",
}


def nad_attributed(expr, selectors, attributes=("ops_owner", "location")):
    """A NAD-keyed metric with its inventory attributes joined on.

    The assignment family is not total: network_devices publishes a row only
    once a NAD's group detail is cached, and a NAD that MnT reports errors for
    can be absent from ERS entirely — so a bare join would silently drop a
    failing device. The unmatched remainder is therefore kept and labelled
    unknown; an unclassified device stays visible under every selection,
    because scoping it to an owner nobody is would hide it from all of them.
    With All selected the union carries every row exactly once, so nothing
    changes. The `max by` collapses provider and the unused attributes off the
    info metric so the join has one row per device.
    """
    body = f"({expr})"
    keys = ", ".join(attributes)
    owned = (f"max by (nad, {keys}) "
             f"({metric('ise3_network_device_assignment', selectors)})")
    inventoried = f"max by (nad) ({metric('ise3_network_device_assignment')})"
    remainder = f"{body} unless on(nad) ({inventoried})"
    for attribute in attributes:
        remainder = (f'label_replace({remainder}, "{attribute}", '
                     f'"{NAD_ATTRIBUTE_DEFAULTS[attribute]}", "", "")')
    return f"{body} * on(nad) group_left({keys}) ({owned}) or {remainder}"


def owner_scoped_nad_errors():
    """RADIUS errors per NAD, scoped to the triage owner variable.

    Aggregated to the device before the join so the table carries the columns
    an operator reads and not `provider`, which names the source and says the
    same thing on every row.
    """
    return nad_attributed(
        summed(gate(metric("ise3_radius_errors_by_nad"), "radius_errors"),
               by="nad"),
        TRIAGE_OWNER,
    )


def exact_problems():
    """The named instances behind the attention counts, as one table.

    Every row of `Attention needed` is a count of a class of problem, which is
    where triage stopped: an operator learned that three certificates had
    expired and had to open another dashboard to learn which three. This is the
    same set of checks resolved down to the individual offender, and where the
    exporter knows why — a collection failure reason, a fallback reason, a node
    state — the why travels in the name rather than being left for the
    diagnostic tier to explain.

    Deliberately not here: failing network devices. They are per-device
    already, there can be hundreds of them, and they have a table of their own
    below that scopes to one owner and links into the device dashboard.
    """
    return " or ".join((
        offender(
            "Collection failing",
            with_reason(
                metric("ise3_dataset_last_failure_info"),
                f"{metric('ise3_dataset_consecutive_failures')} > 0"),
            ("dataset", "reason"),
        ),
        offender(
            "Collection on a fallback provider",
            with_reason(
                metric("ise3_dataset_provider_reason_info"),
                f"{metric('ise3_dataset_provider_degraded')} > 0"),
            ("dataset", "reason"),
        ),
        offender(
            "Node not connected",
            "(" + gate(metric("ise3_deployment_node_state", 'state!="Connected"'),
                       "deployment") + ") == 1",
            ("node", "state"),
        ),
        offender(
            "Certificate expired",
            "(" + gate(metric("ise3_certificate_expiry_days"), "certificates") +
            ") < 0",
            ("node", "certificate", "usage"),
        ),
        offender(
            "PSN above 90% CPU",
            "(" + gate(metric("ise3_node_cpu_utilization_percent"),
                       "psn_performance") + ") > 90",
            ("node",),
        ),
        offender(
            "Licence tier out of compliance",
            "(" + gate(metric("ise3_license_compliant"), "licensing") + ") == 0",
            ("tier",),
        ),
        offender(
            f"Backup older than {BACKUP_STALE_HOURS} hours",
            "(" + gate(metric("ise3_backup_age_hours"), "backup") +
            f") > {BACKUP_STALE_HOURS}",
            detail="deployment backup",
        ),
        offender(
            "TACACS account flagged for review",
            "(" + gate(metric("ise3_tacacs_internal_account_hygiene_risk"),
                       "tacacs_config") + ") > 0",
            ("username", "risk"),
        ),
    ))


def triage_dashboard():
    # The union that answers "is anything wrong?" before anything is read. Each
    # row counts one class of problem and says what it is, so an empty table is
    # the healthy state and needs no interpretation. ISE-sourced rows are gated
    # so a stale collection cannot raise an alarm that has already been fixed.
    attention_needed = " or ".join((
        attention(
            "Datasets not collecting",
            f"count(({readiness_per_dataset()}) == 0)",
        ),
        attention(
            "Datasets on a fallback provider",
            "count"
            f"({metric('ise3_dataset_provider_degraded')} > 0)",
        ),
        attention(
            "Deployment nodes not connected",
            NODES_DISCONNECTED,
        ),
        attention(
            "Certificates already expired",
            "sum"
            f"(({gate(metric('ise3_certificates_expired'), 'certificates')}) > 0)",
        ),
        attention(
            "ISE nodes unreachable for the certificate scan",
            "sum((" +
            gate(metric("ise3_certificate_nodes_unreachable"), "certificates") +
            ") > 0)",
        ),
        attention(
            f"Deployments whose backup is older than {BACKUP_STALE_HOURS} hours",
            "count"
            f"(({gate(metric('ise3_backup_age_hours'), 'backup')}) "
            f"> {BACKUP_STALE_HOURS})",
        ),
        attention(
            "Licence tiers out of compliance",
            "count"
            f"(({gate(metric('ise3_license_compliant'), 'licensing')}) == 0)",
        ),
        attention(
            "PSNs above 90% CPU",
            "count"
            f"(({gate(metric('ise3_node_cpu_utilization_percent'), 'psn_performance')})"
            " > 90)",
        ),
        attention(
            "Network devices with failing authentications",
            f"count(({owner_scoped_nad_errors()}) > 0)",
        ),
        attention(
            "Endpoints failing posture",
            "sum((" +
            gate(metric("ise3_posture_endpoints",
                        f'status!~"Compliant|compliant",{TRIAGE_OWNER}'),
                 "posture_current") +
            ") > 0)",
        ),
        attention(
            "TACACS accounts flagged for hygiene review",
            "count"
            f"(({gate(metric('ise3_tacacs_internal_account_hygiene_risk'), 'tacacs_config')})"
            " > 0)",
        ),
    ))

    now = [
        sized(
            stat_panel(
                "Authentication success rate",
                "Share of RADIUS authentications in the reporting window that "
                "passed. This is the symptom a user would notice, so it is the "
                "first thing on the page. Amber below 95%, red below 85% — "
                "tune both to what this deployment normally does.",
                [instant(AUTH_SUCCESS_RATE)],
                unit="percentunit",
                thresholds=RATIO_HIGH_IS_GOOD,
                minimum=0,
                maximum=1,
                sparkline=True,
                no_value=NO_DATA_STALE,
                data_links=(drilldown("Why — RADIUS access", "ise3-access"),),
            ),
            STAT_H,
            QUARTER,
        ),
        sized(
            stat_panel(
                "Failed authentications",
                "Count of failed RADIUS authentications in the reporting "
                "window. Deliberately uncoloured: every real deployment fails "
                "some authentications, so the rate above is the judgement and "
                "this is the magnitude behind it.",
                [instant(AUTH_FAILED)],
                thresholds=NEUTRAL,
                sparkline=True,
                no_value=NO_DATA_STALE,
                data_links=(drilldown("Why — RADIUS access", "ise3-access"),),
            ),
            STAT_H,
            SIXTH,
        ),
        sized(
            stat_panel(
                "Active sessions",
                "Sessions ISE currently considers live, and the distinct "
                "endpoints behind them. A cliff in sessions with a healthy "
                "success rate usually means a network event rather than an ISE "
                "one; many more sessions than endpoints means sessions are not "
                "being torn down.",
                [
                    instant(ACTIVE_SESSIONS, "sessions"),
                    instant(summed(gate(metric("ise3_active_session_endpoints"),
                                        "active_sessions")),
                            "endpoints", ref="B"),
                ],
                thresholds=NEUTRAL,
                sparkline=True,
                no_value=NO_DATA_STALE,
                data_links=(drilldown("Where — by PSN", "ise3-psn"),),
            ),
            STAT_H,
            SIXTH,
        ),
        sized(
            stat_panel(
                "Nodes not connected",
                "Deployment nodes in any state other than Connected. One node "
                "out of sync degrades policy distribution; a PAN out of sync "
                "stops configuration changes reaching the deployment.",
                [instant(NODES_DISCONNECTED)],
                thresholds=NONZERO_CRITICAL,
                no_value=NO_DATA_CLEAN,
                data_links=(drilldown("Which — control plane", "ise3-control"),),
            ),
            STAT_H,
            SIXTH,
        ),
    ] + trust_pair(("radius_reporting", "active_sessions", "deployment"),
                   span=EIGHTH)

    diagnosis = [
        sized(
            tbl(
                "Attention needed",
                "One row per class of problem currently detected anywhere in "
                "this deployment, with how many instances of it there are. An "
                "empty table is the healthy state and needs no interpretation. "
                "Rows sourced from ISE are suppressed while their dataset is "
                "stale, so a fixed problem cannot keep raising an alarm from a "
                "collection that has not refreshed yet. The failing-devices "
                "and posture rows respect the Operations owner variable; with "
                "All selected they count the whole deployment.",
                [instant(attention_needed)],
                columns=["Count"],
                sort=("Count", True),
                labels={"issue": "Issue"},
                column_overrides=[
                    by_column("Count", thresholds=NONZERO_WARNING,
                              colour_cells=True, width=110),
                ],
            ),
            TALL_H,
            HALF,
        ),
        sized(
            tbl(
                "What exactly is wrong",
                "The same checks as the table beside it, resolved to the "
                "individual thing that is failing and — where the exporter "
                "knows it — why. A count says three certificates have expired; "
                "this says which three, on which node, for which usage. Empty "
                "is the healthy state. Failing network devices are not listed "
                "here: they are per-device already, and the table below scopes "
                "them to one owner and links into the device dashboard.",
                [instant(exact_problems())],
                columns=[None],
                sort=("Issue", False),
                labels={"issue": "Issue", "detail": "What"},
                column_overrides=[
                    by_column("Issue", width=230),
                ],
            ),
            TALL_H,
            HALF,
        ),
        sized(
            ts(
                "Authentication outcome",
                "Passed and failed RADIUS authentications over time, stacked "
                "because together they are the whole of the window's traffic. "
                "A failure band that widens without the total growing is a "
                "policy or identity-store problem; both growing together is "
                "usually a retry storm.",
                [
                    query(AUTH_PASSED, "passed"),
                    query(AUTH_FAILED, "failed", ref="B"),
                ],
                stacked=True,
                series_colours=OUTCOME_COLOURS,
            ),
            PANEL_H,
            THIRD,
        ),
        sized(
            ts(
                "Authentication latency",
                "Mean and worst-case end-to-end authentication time as MnT "
                "recorded it. The dashed lines are the thresholds an operator "
                "should care about: past one second users notice, past three "
                "supplicants start timing out and retrying.",
                [
                    query(
                        gate(metric("ise3_session_authentication_latency_seconds",
                                    'statistic="mean"'), "session_authorization"),
                        "mean",
                    ),
                    query(
                        gate(metric("ise3_session_authentication_latency_seconds",
                                    'statistic="max"'), "session_authorization"),
                        "worst", ref="B",
                    ),
                ],
                unit="s",
                thresholds=LATENCY_SECONDS,
            ),
            PANEL_H,
            THIRD,
        ),
        sized(
            states(
                "Dataset trustworthiness",
                "One lane per dataset, green while that dataset both collected "
                "and stayed inside its freshness window. This is how to tell "
                "whether a step in any graph on any dashboard is ISE changing "
                "or the exporter's view of ISE changing.",
                [query(readiness_per_dataset(), "{{dataset}}")],
            ),
            PANEL_H,
            THIRD,
        ),
    ]

    where = [
        sized(
            tbl(
                "Failing network devices",
                "Every network device with RADIUS errors in the window, worst "
                "first, with the owner and location ISE holds for it — scoped "
                "by the Operations owner variable. Complete — the table "
                "scrolls, so nothing is hidden below a cut-off, and a device "
                "with no inventory assignment has no owner to filter by, so "
                "it is kept and shown as unknown to every owner rather than "
                "silently dropped. A single device dominating this list is a "
                "shared-secret or supplicant problem at that site, not an ISE "
                "problem.",
                [instant(owner_scoped_nad_errors())],
                columns=["Errors"],
                sort=("Errors", True),
                labels={"nad": "Network device",
                        "ops_owner": "Operations owner",
                        "location": "Location"},
                column_overrides=[
                    by_column("Errors", unit="short",
                              thresholds=NONZERO_WARNING, colour_cells=True),
                    by_column("nad", links=[
                        drilldown("Open this switch or router", "ise3-nad",
                                  nad=by_row("Network device")),
                        drilldown("Isolate this device", "ise3-access",
                                  nad=by_row("Network device"))]),
                ],
            ),
            PANEL_H,
            QUARTER,
        ),
        sized(
            ranked(
                "Failing policy service nodes",
                "RADIUS errors attributed to each PSN. Errors concentrated on "
                "one PSN point at that node — resources, certificates, or its "
                "identity-store connection. Errors spread evenly across all of "
                "them point at policy or at the clients.",
                gate(metric("ise3_radius_errors_by_psn"), "radius_errors"),
                label="psn",
                label_header="PSN",
                header="Errors",
                value_thresholds=NONZERO_WARNING,
                colour_cells=True,
                links=(drilldown("Inspect this PSN", "ise3-psn",
                                 node=by_row("PSN")),),
            ),
            PANEL_H,
            QUARTER,
        ),
        sized(
            ranked(
                "Failure codes",
                "ISE message codes behind the failures in this window, most "
                "frequent first." + NAMED_CODES_NOTE + " The code is usually "
                "enough to decide whether this is a client, a policy, or a "
                "certificate problem before opening anything else.",
                named_codes(
                    gate(metric("ise3_radius_errors_by_message_code"),
                         "radius_errors")),
                label="message_code",
                label_header="ISE message code",
                header="Count",
                value_thresholds=NONZERO_WARNING,
                colour_cells=True,
            ),
            PANEL_H,
            QUARTER,
        ),
        sized(
            ranked(
                "Failure reasons for the selected owner",
                "Reason codes attached to endpoints failing authorization "
                "right now, scoped by the Operations owner variable, worst "
                "first." + NAMED_CODES_NOTE + " A reason concentrated in one "
                "owner's region points at that region's device configuration "
                "or identities; the same reason spread across every owner "
                "points at policy — flip the variable between yourself and "
                "All to tell the two apart.",
                named_codes(
                    summed(gate(metric("ise3_session_failure_reason_endpoints",
                                       TRIAGE_OWNER),
                                "session_authorization"),
                           by="reason_code"),
                    label="reason_code"),
                label="reason_code",
                label_header="Reason code",
                header="Endpoints",
                value_thresholds=NONZERO_WARNING,
                colour_cells=True,
            ),
            PANEL_H,
            QUARTER,
        ),
    ]

    closing = [sized(about(
        "answer, in ten seconds, whether ISE is authenticating users right now "
        "— and nothing else.",
        [
            "Read the success rate. If it is green and the trust pair is 100%, "
            "ISE is serving.",
            "Read **Attention needed**. An empty table means nothing else is "
            "known to be wrong.",
            "If a row is populated, read **What exactly is wrong** beside it: "
            "the same checks named down to the individual node, dataset, "
            "certificate or account, with the reason where there is one.",
            "For a failing switch or router, work **Failing network devices** "
            "and open the device from its row.",
            "Then click into the dashboard named in that panel's link rather "
            "than reading further here.",
            "If you own a region, select yourself in the **Operations owner** "
            "variable: the failing-devices, posture, and failure-reason "
            "panels in the two lower sections become your own fix list. The "
            "**Right now** row and the platform rows stay fleet-wide.",
        ],
        [
            "Blank panels are not the same as zero — check **Data trustworthy** "
            "first, then the Collection pipeline dashboard.",
            "A change marker (purple, orange, blue) across every graph means "
            "the exporter's view changed, not necessarily ISE.",
        ],
        ["RADIUS access", "Network devices", "PSN service", "Control plane",
         "Collection pipeline"],
    ), STRIP_H, FULL)]

    return assemble(
        "ISE · Triage",
        "ise3-triage",
        "Tier 1. Is ISE authenticating users right now? Symptom-level only — "
        "every panel links down into the dashboard that explains it. Open this "
        "one first and nothing else until it points somewhere.",
        [
            ("Right now", now),
            ("What is wrong", diagnosis),
            ("Where it hurts", where),
            ("Reference", closing, COLLAPSED),
        ],
        tier=TRIAGE,
        variables=(
            # Sourced from the by-owner census rather than the assignment
            # rows so the option list stays one entry per owner. A directory
            # that classifies nothing still yields "unknown" as an option,
            # because classify() defaults the owner rather than omitting it.
            label_variable("ops_owner", "Operations owner",
                           "ise3_network_devices_by_ops_owner", "ops_owner"),
        ),
    )


# ---------------------------------------------------------------------------
# Tier 2 — RADIUS access
# ---------------------------------------------------------------------------

ACCESS_PSN = 'psn=~"$psn"'
ACCESS_NAD = 'nad=~"$nad"'


def access_dashboard():
    service = [
        sized(
            stat_panel(
                "Success rate",
                "Share of RADIUS authentications in the reporting window that "
                "passed — the R and the E of RED for this service. Compare it "
                "against what this deployment normally does before treating "
                "amber as an incident.",
                [instant(AUTH_SUCCESS_RATE)],
                unit="percentunit",
                thresholds=RATIO_HIGH_IS_GOOD,
                minimum=0,
                maximum=1,
                sparkline=True,
                no_value=NO_DATA_STALE,
            ),
            STAT_H,
            FIFTH,
        ),
        sized(
            stat_panel(
                "Authentications",
                "Total RADIUS authentications MnT recorded in the reporting "
                "window — the rate half of RED. A collapse here with a healthy "
                "success rate means clients stopped asking, which is a network "
                "or supplicant story rather than an ISE one.",
                [instant(AUTH_TOTAL)],
                thresholds=NEUTRAL,
                sparkline=True,
                no_value=NO_DATA_STALE,
            ),
            STAT_H,
            FIFTH,
        ),
        sized(
            stat_panel(
                "Failed",
                "Failed RADIUS authentications in the reporting window. The "
                "work queue below breaks this number down by device and "
                "method; the error-code table says why each one failed.",
                [instant(AUTH_FAILED)],
                thresholds=NEUTRAL,
                sparkline=True,
                no_value=NO_DATA_STALE,
            ),
            STAT_H,
            FIFTH,
        ),
        sized(
            stat_panel(
                "Endpoints and window",
                "How many distinct endpoints authenticated, and how long the "
                "reporting scan window is. Every count on this page is over "
                "that window — read it before quoting any of them. Many "
                "authentications from few endpoints is a retry loop, and the "
                "ratio is the cheapest way to spot one.",
                [
                    instant(summed(gate(metric("ise3_radius_distinct_endpoints_total"),
                                        "radius_reporting")), "endpoints"),
                    instant(gate(metric("ise3_radius_reporting_window_seconds"),
                                 "radius_reporting"), "window", ref="B"),
                ],
                thresholds=NEUTRAL,
                overrides=[by_ref("B", unit="s")],
                no_value=NO_DATA_STALE,
            ),
            STAT_H,
            EIGHTH,
        ),
    ] + trust_pair(("radius_reporting", "radius_errors"), span=EIGHTH)

    over_time = [
        sized(
            ts(
                "Authentication outcome over time",
                "Passed and failed authentications stacked, because together "
                "they are the window's whole traffic. The shape of the failure "
                "band is the diagnosis: a step means something changed, a ramp "
                "means something is degrading, a spike means a retry storm.",
                [
                    query(AUTH_PASSED, "passed"),
                    query(AUTH_FAILED, "failed", ref="B"),
                ],
                stacked=True,
                series_colours=OUTCOME_COLOURS,
            ),
            PANEL_H,
            HALF,
        ),
        sized(
            ts(
                "Authentication latency by PSN",
                "Mean RADIUS authentication latency each PSN recorded, "
                "filtered by the PSN variable. One node diverging from the "
                "others is that node's problem; all of them rising together is "
                "the identity store or the network path to it.",
                [query(
                    gate(metric("ise3_radius_authentication_latency_seconds",
                                ACCESS_PSN), "radius_reporting"),
                    "{{psn}}")],
                unit="s",
                thresholds=LATENCY_SECONDS,
            ),
            PANEL_H,
            HALF,
        ),
    ]

    isolation = [
        sized(
            tbl(
                "Failure work queue",
                "Every network device and authentication method pairing that "
                "produced failures in the window, worst first. This is the "
                "list to work through: each row is one concrete thing to go "
                "and look at, and the device column links into a filtered view "
                "of this same dashboard.",
                [instant(gate(metric("ise3_radius_failures_by_nad_method",
                                     f"{ACCESS_NAD}"), "radius_reporting"))],
                columns=["Failures"],
                sort=("Failures", True),
                labels={"nad": "Network device", "method": "Method"},
                column_overrides=[
                    by_column("Failures", thresholds=NONZERO_WARNING,
                              colour_cells=True, width=120),
                    by_column("Network device", links=[
                        drilldown("Filter to this device", "ise3-access",
                                  nad=by_row("Network device"))]),
                ],
            ),
            TALL_H,
            HALF,
        ),
        sized(
            ranked(
                "Failure codes",
                "ISE message codes behind the failures, most frequent first." +
                NAMED_CODES_NOTE + " Read this before the work queue when the "
                "failures are spread across many devices: one dominant code "
                "usually explains all of them at once.",
                named_codes(gate(metric("ise3_radius_errors_by_message_code"),
                                 "radius_errors")),
                label="message_code",
                label_header="ISE message code",
                header="Count",
                value_thresholds=NONZERO_WARNING,
                colour_cells=True,
            ),
            TALL_H,
            HALF,
        ),
        sized(
            ts(
                "Failure codes over time",
                "The same codes as a rate, with the error dataset's own total "
                "drawn over them. A code that appears at one instant and stops "
                "is an event; one that persists is a configuration problem "
                "nobody has fixed yet. A total well above the sum of the named "
                "codes means most failures carry a code this exporter cannot "
                "name." + NAMED_CODES_NOTE,
                [
                    query(named_codes(gate(metric("ise3_radius_errors_by_message_code"),
                                           "radius_errors")),
                          "{{message_code}}"),
                    query(summed(gate(metric("ise3_radius_errors_total"),
                                      "radius_errors")),
                          "all errors", ref="B"),
                ],
                legend_placement="right",
            ),
            PANEL_H,
            HALF,
        ),
        sized(
            ranked(
                "Failure classes",
                "Failures grouped into the class of thing that went wrong — "
                "credentials, certificate or TLS, identity, timeout, policy "
                "denial — derived by the exporter from the failure reason "
                "string. The fastest way to decide which team owns the "
                "problem.",
                gate(metric("ise3_radius_failure_summary",
                            'dimension="failure_class"'), "radius_reporting"),
                label="value",
                label_header="Failure class",
                header="Failures",
                value_thresholds=NONZERO_WARNING,
                colour_cells=True,
            ),
            PANEL_H,
            HALF,
        ),
        sized(
            ranked(
                "Errors by PSN",
                "RADIUS errors attributed to each policy service node. "
                "Concentration on one node is that node's problem; an even "
                "spread is policy or clients. Links into the PSN dashboard "
                "for the node's own resources and latency.",
                gate(metric("ise3_radius_errors_by_psn", ACCESS_PSN),
                     "radius_errors"),
                label="psn",
                label_header="PSN",
                header="Errors",
                value_thresholds=NONZERO_WARNING,
                colour_cells=True,
                links=(drilldown("Inspect this PSN", "ise3-psn",
                                 node=by_row("PSN")),),
            ),
            PANEL_H,
            THIRD,
        ),
        sized(
            ranked(
                "Errors by method",
                "Which authentication methods are failing. A method-specific "
                "failure is nearly always certificates, supplicant "
                "configuration, or an identity store that does not support "
                "what the client is offering.",
                gate(metric("ise3_radius_errors_by_method"), "radius_errors"),
                label="method",
                label_header="Method",
                header="Errors",
                value_thresholds=NONZERO_WARNING,
                colour_cells=True,
            ),
            PANEL_H,
            THIRD,
        ),
        sized(
            ranked(
                "Failures by location",
                "Failures grouped by the location attribute of the network "
                "device, when ISE's schema carries one. A location that "
                "dominates points at a site-wide cause — a switch template, a "
                "shared secret, an upstream outage — rather than at ISE.",
                gate(metric("ise3_radius_failure_summary", 'dimension="location"'),
                     "radius_reporting"),
                label="value",
                label_header="Location",
                header="Failures",
                value_thresholds=NONZERO_WARNING,
                colour_cells=True,
            ),
            PANEL_H,
            THIRD,
        ),
    ]

    dimensions = [
        sized(
            ranked(
                "By method",
                "Every authentication method in use and how much traffic each "
                "carries, passed and failed together. Read it to know what the "
                "estate actually runs before changing a policy that assumes "
                "otherwise.",
                summed(gate(metric("ise3_radius_authentications_by_method"),
                            "radius_reporting"), by="method"),
                label="method",
                label_header="Method",
                header="Authentications",
            ),
            PANEL_H,
            THIRD,
        ),
        sized(
            ranked(
                "By protocol",
                "Authentication volume by RADIUS protocol. Useful when a "
                "protocol is being retired: this is the panel that proves "
                "whether anything still uses it.",
                summed(gate(metric("ise3_radius_authentications_by_protocol"),
                            "radius_reporting"), by="protocol"),
                label="protocol",
                label_header="Protocol",
                header="Authentications",
            ),
            PANEL_H,
            THIRD,
        ),
        sized(
            ranked(
                "By authorization policy",
                "Which authorization policies are being matched, and how "
                "often. A policy with no traffic is either dead or shadowed by "
                "one above it; either way it is worth knowing before an audit "
                "asks.",
                summed(gate(metric("ise3_radius_authentications_by_policy"),
                            "radius_reporting"), by="policy"),
                label="policy",
                label_header="Authorization policy",
                header="Authentications",
            ),
            PANEL_H,
            THIRD,
        ),
        sized(
            ts(
                "Authentication rate per PSN",
                "Authentication volume each PSN is carrying over time, "
                "filtered by the PSN variable. Divergence between nodes that "
                "should be load-balanced equally is a load-balancer or "
                "network-device configuration problem.",
                [query(summed(gate(metric("ise3_radius_authentications_by_psn",
                                          ACCESS_PSN), "radius_reporting"),
                              by="psn"),
                       "{{psn}}")],
            ),
            PANEL_H,
            HALF,
        ),
        sized(
            ranked(
                "By network device",
                "Authentication volume per network device, complete and sorted. "
                "Read beside the failure work queue: a device high in both is "
                "busy and broken, a device high only here is just busy.",
                summed(gate(metric("ise3_radius_authentications_by_nad",
                                   ACCESS_NAD), "radius_reporting"),
                       by="nad"),
                label="nad",
                label_header="Network device",
                header="Authentications",
            ),
            PANEL_H,
            HALF,
        ),
    ]

    authorization = [
        sized(
            ranked(
                "Endpoint status by owner",
                "Live authorization status of currently connected endpoints, "
                "grouped by the operations owner attribute. This is the "
                "current state of the network rather than the window's "
                "history, so it answers 'who is affected right now'.",
                summed(gate(metric("ise3_session_status_endpoints"),
                            "session_authorization"),
                       by="ops_owner,status"),
                label="ops_owner",
                label_header="Operations owner",
                header="Endpoints",
            ),
            PANEL_H,
            THIRD,
        ),
        sized(
            ranked(
                "Live failure reasons",
                "Failure reason codes attached to endpoints that are failing "
                "authorization right now, from the live session table rather "
                "than the reporting window. The complement to the historical "
                "error codes above.",
                summed(gate(metric("ise3_session_failure_reason_endpoints"),
                            "session_authorization"),
                       by="reason_code"),
                label="reason_code",
                label_header="Reason code",
                header="Endpoints",
                value_thresholds=NONZERO_WARNING,
                colour_cells=True,
            ),
            PANEL_H,
            THIRD,
        ),
        sized(
            ranked(
                "Failed authorization profiles",
                "Authorization profiles that endpoints matched on their way to "
                "failing. A profile appearing here that should never fail is "
                "usually a missing attribute or a broken downloadable ACL.",
                summed(gate(metric("ise3_session_failed_authz_profile_endpoints"),
                            "session_authorization"),
                       by="authz_profile"),
                label="authz_profile",
                label_header="Authorization profile",
                header="Endpoints",
                value_thresholds=NONZERO_WARNING,
                colour_cells=True,
            ),
            PANEL_H,
            THIRD,
        ),
        sized(
            ranked(
                "Failed authorization rules",
                "The authorization rules those failing endpoints matched. Read "
                "with the profiles panel: rule plus profile is enough to find "
                "the policy line responsible.",
                summed(gate(metric("ise3_session_failed_authz_rule_endpoints"),
                            "session_authorization"),
                       by="authz_rule"),
                label="authz_rule",
                label_header="Authorization rule",
                header="Endpoints",
                value_thresholds=NONZERO_WARNING,
                colour_cells=True,
            ),
            PANEL_H,
            HALF,
        ),
        sized(
            ranked(
                "Failed policy sets",
                "Policy sets containing the failures. A single policy set "
                "carrying every failure narrows the search to one branch of "
                "the policy tree.",
                summed(gate(metric("ise3_session_failed_policy_set_endpoints"),
                            "session_authorization"),
                       by="policy_set"),
                label="policy_set",
                label_header="Policy set",
                header="Endpoints",
                value_thresholds=NONZERO_WARNING,
                colour_cells=True,
            ),
            PANEL_H,
            HALF,
        ),
        sized(
            ranked(
                "Selected authorization profiles",
                "The authorization profiles endpoints are actually landing on "
                "right now, successful ones included. The failure tables above "
                "say what went wrong; this one says what normally happens, "
                "which is what you need to know a change did what was "
                "intended.",
                summed(gate(metric("ise3_session_authz_profile_endpoints"),
                            "session_authorization"),
                       by="authz_profile"),
                label="authz_profile",
                label_header="Authorization profile",
                header="Endpoints",
            ),
            PANEL_H,
            THIRD,
        ),
        sized(
            ranked(
                "Matched authorization rules",
                "Which authorization rules live sessions matched. A rule with "
                "no traffic is dead or shadowed; a catch-all rule with most of "
                "the traffic means the specific rules above it are not "
                "matching.",
                summed(gate(metric("ise3_session_authz_rule_endpoints"),
                            "session_authorization"),
                       by="authz_rule"),
                label="authz_rule",
                label_header="Authorization rule",
                header="Endpoints",
            ),
            PANEL_H,
            THIRD,
        ),
        sized(
            ranked(
                "Matched policy sets",
                "Which policy set each live session was evaluated in. An "
                "unexpected distribution here is a policy-set condition "
                "problem, and it explains authorization results that look "
                "wrong for reasons no individual rule accounts for.",
                summed(gate(metric("ise3_session_policy_set_endpoints"),
                            "session_authorization"),
                       by="policy_set"),
                label="policy_set",
                label_header="Policy set",
                header="Endpoints",
            ),
            PANEL_H,
            THIRD,
        ),
        sized(
            ranked(
                "Live authentication methods",
                "Authentication methods in use by currently connected "
                "endpoints, from the live session list rather than the "
                "reporting window. The 'what is on the network now' complement "
                "to the historical method breakdown.",
                summed(gate(metric("ise3_session_auth_method_endpoints"),
                            "session_authorization"),
                       by="method"),
                label="method",
                label_header="Method",
                header="Endpoints",
            ),
            PANEL_H,
            HALF,
        ),
        sized(
            ranked(
                "Live sessions by owner",
                "Live session count grouped by the operations owner attribute "
                "of the network device. Turns a session number into a list of "
                "teams — the fastest way to work out who to tell during an "
                "access incident.",
                gate(metric("ise3_active_sessions_by_ops_owner"), "active_sessions"),
                label="ops_owner",
                label_header="Operations owner",
                header="Sessions",
            ),
            PANEL_H,
            HALF,
        ),
        sized(
            tbl(
                "Policy sets by network device",
                "Which policy set each network device's live sessions matched. "
                "A device landing in an unexpected policy set is a device "
                "group, location, or NAD attribute problem. Bounded by the "
                "exporter's top-devices limit; the coverage panel on the "
                "pipeline dashboard says how much is shown.",
                [instant(gate(metric("ise3_session_policy_set_endpoints_by_nad",
                                     ACCESS_NAD), "session_authorization"))],
                columns=["Endpoints"],
                sort=("Endpoints", True),
                labels={"nad": "Network device", "policy_set": "Policy set"},
            ),
            PANEL_H,
            FULL,
        ),
    ]

    accounting = [
        sized(
            stat_panel(
                "Accounting starts and stops",
                "RADIUS accounting starts against stops in the scan window. "
                "Persistently more starts than stops means sessions are not "
                "being torn down cleanly, which inflates the live session "
                "count and eventually the licence consumption.",
                [
                    instant(summed(gate(metric("ise3_radius_accounting_events",
                                               'dimension="total",event_type="starts"'),
                                        "radius_accounting")), "starts"),
                    instant(summed(gate(metric("ise3_radius_accounting_events",
                                               'dimension="total",event_type="stops"'),
                                        "radius_accounting")), "stops", ref="B"),
                ],
                thresholds=NEUTRAL,
                no_value=NO_DATA_STALE,
            ),
            STAT_H,
            THIRD,
        ),
        sized(
            stat_panel(
                "Duration coverage and scan window",
                "Share of accounting records that carried a usable session "
                "duration, and how long the accounting scan window is. The "
                "duration panels are only as meaningful as the coverage "
                "figure: low coverage means the averages beside it are drawn "
                "from a minority of sessions.",
                [
                    instant(summed(gate(metric("ise3_radius_accounting_duration_coverage",
                                               'dimension="total"'),
                                        "radius_accounting")), "coverage"),
                    instant(gate(metric("ise3_radius_accounting_window_seconds"),
                                 "radius_accounting"), "window", ref="B"),
                ],
                unit="percentunit",
                thresholds=COVERAGE,
                overrides=[by_ref("B", unit="s", thresholds=NEUTRAL)],
                no_value=NO_DATA_STALE,
            ),
            STAT_H,
            THIRD,
        ),
        sized(
            stat_panel(
                "Mean session duration",
                "Average completed session length across the accounting scan "
                "window. A collapse here alongside steady authentication "
                "volume is the classic signature of a flapping link or an "
                "aggressive session timeout.",
                [instant(gate(metric("ise3_radius_accounting_session_duration_seconds",
                                     'dimension="total",statistic="mean"'),
                              "radius_accounting"))],
                unit="s",
                thresholds=NEUTRAL,
                no_value=NO_DATA_STALE,
            ),
            STAT_H,
            THIRD,
        ),
        sized(
            ts(
                "Accounting events by PSN",
                "Accounting starts and stops each PSN is processing. Uneven "
                "distribution here but even authentication distribution means "
                "network devices are pointing accounting somewhere different "
                "from authentication.",
                [query(summed(gate(metric("ise3_radius_accounting_events",
                                          'dimension="psn"'), "radius_accounting"),
                              by="value,event_type"),
                       "{{value}} · {{event_type}}")],
            ),
            PANEL_H,
            HALF,
        ),
        sized(
            ranked(
                "Accounting volume by network device",
                "Accounting records per network device across the scan window, "
                "complete and sorted. A device sending far more accounting "
                "than authentication traffic is usually re-authenticating or "
                "sending interim updates too aggressively.",
                summed(gate(metric("ise3_radius_accounting_events",
                                   'dimension="nad"'), "radius_accounting"),
                       by="value"),
                label="value",
                label_header="Network device",
                header="Records",
            ),
            PANEL_H,
            HALF,
        ),
    ]

    closing = [sized(about(
        "explain why RADIUS authentications are failing, once triage has said "
        "that they are.",
        [
            "Confirm the success rate and the trust pair at the top.",
            "Read **Failure codes** first — one dominant code often explains "
            "everything below it.",
            "Work the **Failure work queue** row by row; each row is one "
            "device and method to go and look at.",
            "Use the PSN and Network device variables to narrow the whole page "
            "to one suspect.",
        ],
        [
            "Errors on one PSN only: continue on the PSN service dashboard.",
            "Errors on one device only: check its shared secret and the "
            "supplicant configuration at that site.",
            "Certificate or TLS failure classes: continue on the Control plane "
            "dashboard's certificate panels.",
        ],
        ["Triage", "PSN service", "Control plane", "Endpoints"],
    ), STRIP_H, FULL)]

    return assemble(
        "ISE · RADIUS access",
        "ise3-access",
        "Tier 2 diagnostic. Why are RADIUS authentications failing? Service "
        "level first, then failure isolation, then the dimensions and live "
        "authorization detail needed to close it out.",
        [
            ("Service level", service),
            ("Over time", over_time),
            ("Failure isolation", isolation),
            ("Authentication dimensions", dimensions, COLLAPSED),
            ("Live authorization decisions", authorization, COLLAPSED),
            ("Accounting", accounting, COLLAPSED),
            ("Reference", closing, COLLAPSED),
        ],
        tier=DIAGNOSTIC,
        variables=(
            label_variable("psn", "PSN", "ise3_radius_authentications_by_psn", "psn"),
            label_variable("nad", "Network device",
                           "ise3_radius_authentications_by_nad", "nad"),
        ),
    )


# ---------------------------------------------------------------------------
# Tier 2 — PSN service
# ---------------------------------------------------------------------------

PSN_NODE = 'node=~"$node"'

# Session counts are a gauge collected every 300s and then republished on every
# scrape until the next collection, so a delta window shorter than two cadences
# measures the scrape repeating itself and reads flat through a real failover.
# Three cadences, so one skipped collection still leaves two points in the
# window. Asserted against session_distribution's declared interval in the
# tests -- lengthen it there too if that cadence ever changes.
SESSION_DELTA_WINDOW = "15m"

# Subtraction rather than delta(). delta() extrapolates its result to the full
# window, so 2500 sessions moving reads as 2679 and the overshoot drifts as the
# window slides past the step -- a number wobbling while nothing is happening,
# on the one panel whose entire content is a number of sessions. Subtracting the
# earlier sample is exact and still. The cost is that it has nothing to draw
# until the window has filled, which is stated in the panel description.
SESSION_DELTA = (f"ise3_active_sessions_by_psn - ise3_active_sessions_by_psn "
                 f"offset {SESSION_DELTA_WINDOW}")


def psn_dashboard():
    service = [
        sized(
            stat_panel(
                "Busiest node load",
                "Highest RADIUS load percentage reported by any selected PSN — "
                "the saturation signal of USE for this service. Amber at 80%, "
                "red at 90%: past that, latency rises before throughput falls, "
                "so this leads the symptom.",
                [instant(f"max("
                         f"{gate(metric('ise3_psn_load_percent', PSN_NODE), 'psn_performance')})")],
                unit="percent",
                thresholds=UTILISATION,
                minimum=0,
                maximum=100,
                sparkline=True,
                no_value=NO_DATA_STALE,
            ),
            STAT_H,
            FIFTH,
        ),
        sized(
            stat_panel(
                "Worst node latency",
                "Slowest average RADIUS latency across the selected PSNs. A "
                "single slow node is that node's resources or identity-store "
                "path; all of them slow together is the identity store itself.",
                [instant(f"max("
                         f"{gate(metric('ise3_psn_average_latency_seconds', PSN_NODE), 'psn_performance')})")],
                unit="s",
                thresholds=LATENCY_SECONDS,
                sparkline=True,
                no_value=NO_DATA_STALE,
            ),
            STAT_H,
            FIFTH,
        ),
        sized(
            stat_panel(
                "RADIUS requests per hour",
                "Combined RADIUS request rate across the selected PSNs, as ISE "
                "itself reports it. The throughput half of the picture: read it "
                "beside load to tell a busy deployment from a struggling one.",
                [instant(summed(gate(metric("ise3_psn_radius_requests_per_hour",
                                            PSN_NODE), "psn_performance")))],
                thresholds=NEUTRAL,
                sparkline=True,
                no_value=NO_DATA_STALE,
            ),
            STAT_H,
            FIFTH,
        ),
        sized(
            stat_panel(
                "Diagnostic events",
                "Diagnostic events ISE raised on the selected PSNs in the "
                "window. Not all are errors, but a step change is worth "
                "reading — the diagnostic work queue below names them.",
                [instant(summed(gate(metric("ise3_psn_diagnostic_events_total"),
                                     "psn_performance")))],
                thresholds=NEUTRAL,
                no_value=NO_DATA_STALE,
            ),
            STAT_H,
            EIGHTH,
        ),
    ] + trust_pair(("psn_performance", "session_distribution"), span=EIGHTH)

    throughput = [
        sized(
            ts(
                "Active sessions per PSN",
                "Live sessions each PSN is holding. Nodes that should be "
                "load-balanced equally and are not point at the network "
                "devices' RADIUS server ordering rather than at ISE.",
                [query(gate(metric("ise3_active_sessions_by_psn"),
                            "session_distribution"), "{{psn}}")],
            ),
            PANEL_H,
            THIRD,
        ),
        sized(
            ts(
                "Session delta per PSN",
                f"Change in each PSN's session count over "
                f"{SESSION_DELTA_WINDOW}. Flat is the normal state, including "
                "at the busiest hour of the day: a PSN that is holding steady "
                "under load reads the same here as one holding steady at "
                "midnight. A fall on one node matched by a rise on another is "
                "load moving between them, which is what a failover, a node "
                "restart, or a NAD's RADIUS server order changing looks like "
                "from here. A fall with no matching rise is sessions leaving "
                "ISE rather than moving inside it — read it beside the total "
                "on Access. Blank for the first "
                f"{SESSION_DELTA_WINDOW} after an exporter restart, and for a "
                "PSN that has been serving for less than that: there is no "
                "earlier count to subtract yet.",
                [query(gate(SESSION_DELTA, "session_distribution"), "{{psn}}")],
                thresholds=ZERO_REFERENCE,
            ),
            PANEL_H,
            THIRD,
        ),
        sized(
            ts(
                "RADIUS requests per hour by PSN",
                "Per-node request rate over time. Compare the shape against "
                "active sessions: rate rising while sessions stay flat is "
                "re-authentication churn, not growth.",
                [query(gate(metric("ise3_psn_radius_requests_per_hour", PSN_NODE),
                            "psn_performance"), "{{node}}")],
            ),
            PANEL_H,
            THIRD,
        ),
    ]

    saturation = [
        sized(
            ts(
                "CPU utilisation",
                "Per-node CPU as ISE reports it, with the amber and red "
                "thresholds drawn. Sustained time above 80% is the point at "
                "which authentication latency starts to move.",
                [query(gate(metric("ise3_node_cpu_utilization_percent", PSN_NODE),
                            "psn_performance"), "{{node}}")],
                unit="percent",
                thresholds=UTILISATION,
                minimum=0,
                maximum=100,
            ),
            PANEL_H,
            THIRD,
        ),
        sized(
            ts(
                "Memory utilisation",
                "Per-node memory as ISE reports it. A node climbing steadily "
                "without a matching rise in sessions is usually a leak or a "
                "log-processing backlog rather than load.",
                [query(gate(metric("ise3_node_memory_utilization_percent", PSN_NODE),
                            "psn_performance"), "{{node}}")],
                unit="percent",
                thresholds=UTILISATION,
                minimum=0,
                maximum=100,
            ),
            PANEL_H,
            THIRD,
        ),
        sized(
            ts(
                "Disk utilisation, worst filesystem per node",
                "The fullest filesystem on each node. ISE degrades badly when "
                "a node runs out of disk — logging stops first, then services "
                "— so this is a saturation signal worth watching ahead of CPU.",
                [query(f"max by (node) ("
                       f"{gate(metric('ise3_node_disk_utilization_percent', PSN_NODE), 'psn_performance')})",
                       "{{node}}")],
                unit="percent",
                thresholds=UTILISATION,
                minimum=0,
                maximum=100,
            ),
            PANEL_H,
            THIRD,
        ),
        sized(
            ts(
                "Average RADIUS latency by PSN",
                "Per-node average RADIUS transaction time. This is the number "
                "users experience; the thresholds mark where they notice and "
                "where supplicants start retrying.",
                [query(gate(metric("ise3_psn_average_latency_seconds", PSN_NODE),
                            "psn_performance"), "{{node}}")],
                unit="s",
                thresholds=LATENCY_SECONDS,
            ),
            PANEL_H,
            HALF,
        ),
        sized(
            ts(
                "RADIUS errors by PSN",
                "Errors attributed to each node over the window. Read against "
                "the CPU and latency panels above: errors that follow "
                "saturation are a capacity problem, errors without it are a "
                "configuration or certificate problem.",
                [query(gate(metric("ise3_radius_errors_by_psn"), "radius_errors"),
                       "{{psn}}")],
                series_colours=OUTCOME_COLOURS,
            ),
            PANEL_H,
            HALF,
        ),
    ]

    reporting = [
        sized(
            states(
                "PSN connection state",
                "One lane per node, green while ISE reported it Connected. A "
                "node dropping out here explains a simultaneous drop in its "
                "sessions and request rate without any of it being a RADIUS "
                "problem.",
                [query(NODE_CONNECTED, "{{node}}")],
                mappings=CONNECTED,
            ),
            STRIP_H,
            HALF,
        ),
        sized(
            ts(
                "Logging, noise, and suppression per hour",
                "MnT log volume each node is generating, with the share ISE "
                "classified as noise and the share it suppressed. Suppression "
                "climbing is ISE protecting itself, and it makes the reporting "
                "views this exporter reads less complete.",
                [
                    query(gate(metric("ise3_psn_mnt_logs_per_hour", PSN_NODE),
                               "psn_performance"), "{{node}} logs"),
                    query(gate(metric("ise3_psn_noise_per_hour", PSN_NODE),
                               "psn_performance"), "{{node}} noise", ref="B"),
                    query(gate(metric("ise3_psn_suppression_per_hour", PSN_NODE),
                               "psn_performance"), "{{node}} suppressed", ref="C"),
                ],
            ),
            PANEL_H,
            HALF,
        ),
        sized(
            tbl(
                "Diagnostic work queue",
                "Diagnostic events ISE raised, by node, severity, and message "
                "code, worst first. Each row is a concrete thing ISE is "
                "complaining about on a named node — the closest thing this "
                "exporter has to reading the appliance's own alarm list.",
                [instant(gate(metric("ise3_psn_diagnostic_events", PSN_NODE),
                              "psn_performance"))],
                columns=["Events"],
                sort=("Events", True),
                labels={"node": "Node", "severity": "Severity",
                        "message_code": "Code", "category": "Category",
                        "source": "Source"},
                column_overrides=[
                    by_column("Events", thresholds=NONZERO_WARNING,
                              colour_cells=True, width=100),
                ],
            ),
            TALL_H,
            HALF,
        ),
        sized(
            tbl(
                "Reporting source freshness",
                "Age of the newest row in each Data Connect view this exporter "
                "reads from MnT. A view that stops advancing means ISE stopped "
                "writing to it, which makes every panel fed from it quietly "
                "stale rather than obviously broken.",
                [
                    instant(metric("ise3_source_latest_row_age_seconds"), ref="A"),
                    instant(metric("ise3_source_has_recent_rows"), ref="B"),
                ],
                columns=["Newest row age", "Recent rows"],
                sort=("Newest row age", True),
                labels={"view": "View"},
                column_overrides=[
                    by_column("Newest row age", **SECONDS_CELL),
                    by_column("Recent rows", **BOOLEAN_CELL),
                ],
            ),
            TALL_H,
            HALF,
        ),
    ]

    closing = [sized(about(
        "decide whether the policy service nodes are coping, and which one is "
        "not.",
        [
            "Read busiest load and worst latency. Both green means the PSNs "
            "are not the problem — go back to RADIUS access.",
            "Find the outlier node in the utilisation panels.",
            "Confirm it in the diagnostic work queue and the connection-state "
            "timeline.",
        ],
        [
            "Saturation with errors: a capacity problem — rebalance or add a "
            "node.",
            "Errors without saturation: certificates, the identity store, or "
            "policy — continue on RADIUS access.",
            "Suppression climbing: ISE is dropping logs, so treat the reporting "
            "panels on every dashboard as incomplete until it recovers.",
        ],
        ["Triage", "RADIUS access", "Control plane", "Collection pipeline"],
    ), STRIP_H, FULL)]

    return assemble(
        "ISE · PSN service",
        "ise3-psn",
        "Tier 2 diagnostic. Are the policy service nodes coping? Service level "
        "first, then utilisation and saturation per node, then the reporting "
        "pipeline those nodes feed.",
        [
            ("Service level", service),
            ("Throughput", throughput),
            ("Utilisation and saturation", saturation),
            ("Node reporting", reporting, COLLAPSED),
            ("Reference", closing, COLLAPSED),
        ],
        tier=DIAGNOSTIC,
        variables=(
            label_variable("node", "PSN", "ise3_psn_radius_requests_per_hour",
                           "node"),
        ),
    )


# ---------------------------------------------------------------------------
# Tier 2 — control plane (PAN and MnT)
# ---------------------------------------------------------------------------

def control_dashboard():
    status = [
        sized(
            stat_panel(
                "Nodes not connected",
                "Deployment nodes in any state other than Connected. A PAN out "
                "of sync stops configuration reaching the deployment; an MnT "
                "out of sync stops every reporting panel in this dashboard set "
                "from advancing.",
                [instant(NODES_DISCONNECTED)],
                thresholds=NONZERO_CRITICAL,
                no_value=NO_DATA_CLEAN,
            ),
            STAT_H,
            SIXTH,
        ),
        sized(
            stat_panel(
                "PAN high availability",
                "Whether automatic PAN failover is enabled. Without it, losing "
                "the primary admin node is a manual promotion under pressure "
                "rather than an automatic one.",
                [instant(gate(metric("ise3_deployment_pan_ha_enabled"), "deployment"))],
                mappings=ENABLED,
                thresholds=REQUIRED_BOOLEAN,
                no_value=NO_DATA_STALE,
                text_mode=BigValueTextMode.VALUE,
            ),
            STAT_H,
            SIXTH,
        ),
        sized(
            stat_panel(
                "Backup state",
                "Hours since the last successful configuration backup, and "
                "whether a backup schedule is configured at all. A daily backup "
                "that has not run is the earliest visible sign of a stalled "
                f"PAN, so age goes amber at {BACKUP_STALE_HOURS} hours rather "
                "than waiting for a failure — and 'not configured' is a "
                "different problem from 'stale', which is why both are here.",
                [
                    instant(gate(metric("ise3_backup_age_hours"), "backup"), "age"),
                    instant(gate(metric("ise3_backup_configured"), "backup"),
                            "scheduled", ref="B"),
                ],
                unit="h",
                thresholds=BACKUP_AGE_HOURS,
                overrides=[by_ref("B", unit="short", mappings=CONFIGURED,
                                  thresholds=REQUIRED_BOOLEAN)],
                no_value=NO_DATA_STALE,
            ),
            STAT_H,
            SIXTH,
        ),
        sized(
            stat_panel(
                "Certificates expired",
                "Certificates in the deployment's trust and system stores that "
                "are already past their expiry date. Any number above zero is "
                "red: an expired EAP or admin certificate breaks "
                "authentication outright.",
                [instant(gate(metric("ise3_certificates_expired"), "certificates"))],
                thresholds=NONZERO_CRITICAL,
                no_value=NO_DATA_STALE,
            ),
            STAT_H,
            SIXTH,
        ),
    ] + trust_pair(("deployment", "certificates", "backup"), span=SIXTH) + [
        sized(
            states(
                "Node connection state",
                "One lane per deployment node, green while ISE reported it "
                "Connected. The single most useful panel during a control "
                "plane incident: it shows exactly when a node left and whether "
                "it came back.",
                [query(NODE_CONNECTED, "{{node}}")],
                mappings=CONNECTED,
            ),
            STRIP_H,
            FULL,
        ),
    ]

    detail = [
        sized(
            stat_panel(
                "Certificates expiring soon",
                "Certificates inside the exporter's warning horizon but not "
                "yet expired. This is the number that should be worked down to "
                "zero through change control; the expired count on the strip "
                "above is what happens when it is not.",
                [instant(gate(metric("ise3_certificates_expiring_soon"),
                              "certificates"))],
                thresholds=NONZERO_WARNING,
                no_value=NO_DATA_STALE,
            ),
            STAT_H,
            THIRD,
        ),
        sized(
            stat_panel(
                "Certificate scan coverage",
                "Nodes the certificate scan read, and nodes it could not "
                "reach. Certificates on an unreachable node are counted "
                "nowhere on this page, so a non-zero second value means the "
                "expiry numbers here are incomplete rather than clean.",
                [
                    instant(gate(metric("ise3_certificate_nodes_scanned"),
                                 "certificates"), "scanned"),
                    instant(gate(metric("ise3_certificate_nodes_unreachable"),
                                 "certificates"), "unreachable", ref="B"),
                ],
                thresholds=NEUTRAL,
                overrides=[by_ref("B", thresholds=NONZERO_WARNING)],
                no_value=NO_DATA_STALE,
            ),
            STAT_H,
            THIRD,
        ),
        sized(
            stat_panel(
                "Patch level and release support",
                "The deployment's patch level, and whether the running release "
                "is one this exporter and Cisco still support. Unsupported "
                "means schema drift is expected and some panels may quietly "
                "stop being populated.",
                [
                    instant(gate(metric("ise3_patch_level"), "patches"), "patch"),
                    instant(gate(metric("ise3_version_supported"), "patches"),
                            "supported", ref="B"),
                ],
                thresholds=NEUTRAL,
                overrides=[by_ref("B", mappings=SUPPORTED,
                                  thresholds=REQUIRED_BOOLEAN)],
                no_value=NO_DATA_STALE,
            ),
            STAT_H,
            THIRD,
        ),
        sized(
            tbl(
                "Node roles and services",
                "Every node with the personas and services ISE has assigned to "
                "it. Read it when the deployment is not behaving as designed — "
                "a service running where it should not be, or missing where it "
                "should, explains a surprising amount.",
                [
                    instant(gate(metric("ise3_deployment_node_service_enabled"),
                                 "deployment"), ref="A"),
                ],
                columns=[None],
                labels={"node": "Node", "service": "Service"},
            ),
            TALL_H,
            HALF,
        ),
        sized(
            tbl(
                "Certificate expiry, soonest first",
                "Every certificate the scan could read, with days remaining. "
                "Red inside 30 days, amber inside 90 — enough notice to renew "
                "through change control rather than at 3am. Sorted ascending "
                "so the next thing to expire is always the first row.",
                [instant(gate(metric("ise3_certificate_expiry_days"),
                              "certificates"))],
                columns=["Days left"],
                sort=("Days left", False),
                labels={"node": "Node", "certificate": "Certificate",
                        "store": "Store", "usage": "Usage"},
                column_overrides=[
                    by_column("Days left", thresholds=CERT_RUNWAY_DAYS,
                              colour_cells=True, width=110),
                ],
            ),
            TALL_H,
            HALF,
        ),
    ]

    resources = [
        sized(
            ts(
                "CPU utilisation",
                "Per-node CPU across the whole deployment, PAN and MnT "
                "included. An MnT pinned at high CPU is the usual reason "
                "reporting data goes stale while ISE itself keeps "
                "authenticating normally.",
                [query(gate(metric("ise3_node_cpu_utilization_percent"),
                            "psn_performance"), "{{node}}")],
                unit="percent",
                thresholds=UTILISATION,
                minimum=0,
                maximum=100,
            ),
            PANEL_H,
            THIRD,
        ),
        sized(
            ts(
                "Memory utilisation",
                "Per-node memory across the deployment. Sustained pressure on "
                "an admin node shows up as slow configuration changes long "
                "before it shows up as an outage.",
                [query(gate(metric("ise3_node_memory_utilization_percent"),
                            "psn_performance"), "{{node}}")],
                unit="percent",
                thresholds=UTILISATION,
                minimum=0,
                maximum=100,
            ),
            PANEL_H,
            THIRD,
        ),
        sized(
            ts(
                "Disk utilisation, worst filesystem per node",
                "The fullest filesystem on each node. On an MnT this is the "
                "one to watch: when its log partition fills, ISE purges "
                "aggressively and the reporting views lose history without any "
                "error being raised.",
                [query("max by (node) (" +
                       gate(metric("ise3_node_disk_utilization_percent"),
                            "psn_performance") + ")", "{{node}}")],
                unit="percent",
                thresholds=UTILISATION,
                minimum=0,
                maximum=100,
            ),
            PANEL_H,
            THIRD,
        ),
    ]

    mnt = [
        sized(
            ts(
                "End-to-end authentication latency",
                "Mean and worst-case authentication time reconstructed from "
                "MnT session detail. This is the number to quote when someone "
                "says logging in is slow, and the step breakdown below says "
                "which phase is responsible.",
                [
                    query(gate(metric("ise3_session_authentication_latency_seconds",
                                      'statistic="mean"'), "session_authorization"),
                          "mean"),
                    query(gate(metric("ise3_session_authentication_latency_seconds",
                                      'statistic="max"'), "session_authorization"),
                          "worst", ref="B"),
                ],
                unit="s",
                thresholds=LATENCY_SECONDS,
            ),
            PANEL_H,
            HALF,
        ),
        sized(
            tbl(
                "Authentication step latency",
                "Mean time in each phase of the authentication exchange, with "
                "how many sessions contributed. A step whose sample count is "
                "small is not evidence — read the two columns together before "
                "concluding anything from a slow phase.",
                [
                    instant(gate(metric("ise3_session_authentication_step_latency_seconds",
                                        'statistic="mean"'), "session_authorization"),
                            ref="A"),
                    instant(gate(metric("ise3_session_authentication_step_latency_samples"),
                                 "session_authorization"), ref="B"),
                ],
                columns=["Mean", "Samples"],
                sort=("Mean", True),
                labels={"step": "Step"},
                column_overrides=[
                    by_column("Mean", unit="s", thresholds=LATENCY_SECONDS,
                              colour_cells=True),
                ],
            ),
            PANEL_H,
            HALF,
        ),
        sized(
            ts(
                "MnT detail-cache coverage",
                "How much of the live session list the exporter has fetched "
                "per-session detail for. Panels fed from that detail are "
                "suppressed below 99% so they cannot under-report beside a "
                "complete neighbour — this panel is where the gap is visible.",
                [query(metric("ise3_detail_cache_coverage", 'cache="mnt_session_detail"'),
                       "session detail")],
                unit="percentunit",
                thresholds=COVERAGE,
                minimum=0,
                maximum=1,
            ),
            PANEL_H,
            HALF,
        ),
        sized(
            tbl(
                "Backup history",
                "The status ISE recorded for the last backup attempt, and how "
                "long ago it succeeded. Read it when the age stat is amber: a "
                "failed status is a backup target problem, no status at all is "
                "a scheduler problem on the PAN.",
                [
                    instant(f"{gate(metric('ise3_backup_last_status'), 'backup')} == 1",
                            ref="A"),
                    instant("time() - (" + gate(
                        metric("ise3_backup_last_success_timestamp"), "backup") + ")",
                        ref="B"),
                ],
                columns=[None, "Since last success"],
                labels={"status": "Last status"},
                column_overrides=[
                    by_column("Since last success", **SECONDS_CELL),
                ],
            ),
            PANEL_H,
            HALF,
        ),
        sized(
            tbl(
                "Control plane collection failures",
                "The most recent failure recorded for each of the control "
                "plane datasets, with the reason and the detail the exporter "
                "captured. Empty is the healthy state; a populated row is the "
                "actual error text rather than a generic failure count.",
                [instant(metric("ise3_dataset_last_failure_detail_info",
                                'dataset=~"deployment|backup|certificates|patches|licensing"'))],
                columns=[None],
                labels={"dataset": "Dataset", "provider": "Provider",
                        "reason": "Reason", "detail": "Detail"},
            ),
            PANEL_H,
            HALF,
        ),
    ]

    closing = [sized(about(
        "answer whether the administration and monitoring plane is healthy — "
        "the nodes, their certificates, their backups, and the MnT pipeline "
        "the rest of the set depends on.",
        [
            "Read the status strip. Anything red here undermines dashboards "
            "elsewhere.",
            "Check the node connection timeline for when a node left.",
            "Read certificate expiry ascending — the first row is the next "
            "thing that will break.",
        ],
        [
            "MnT disk or CPU high: reporting data across every dashboard is "
            "about to go stale.",
            "Backup stale: check the PAN before assuming it is a backup target "
            "problem.",
            "Certificates expired: RADIUS access will already be showing "
            "certificate failure classes.",
        ],
        ["Triage", "RADIUS access", "PSN service", "Capacity"],
    ), STRIP_H, FULL)]

    return assemble(
        "ISE · Control plane",
        "ise3-control",
        "Tier 2 diagnostic. Is the PAN and MnT control plane healthy? Node "
        "state, certificates, backup and patching first, then node resources, "
        "then the MnT collection that feeds every other dashboard.",
        [
            ("Control plane status", status),
            ("Certificates, software, and services", detail),
            ("Node resources", resources),
            ("MnT collection", mnt, COLLAPSED),
            ("Reference", closing, COLLAPSED),
        ],
        tier=DIAGNOSTIC,
    )


# ---------------------------------------------------------------------------
# Tier 2 — endpoints
# ---------------------------------------------------------------------------


def endpoints_dashboard():
    inventory = [
        sized(
            stat_panel(
                "Endpoints",
                "Total endpoints in the ISE endpoint database. This is the "
                "inventory count, not the live session count — the difference "
                "between the two is the estate that is currently switched off.",
                [instant(gate(metric("ise3_endpoints_total"), "endpoint_inventory"))],
                thresholds=NEUTRAL,
                sparkline=True,
                no_value=NO_DATA_STALE,
            ),
            STAT_H,
            FIFTH,
        ),
        sized(
            stat_panel(
                "Unprofiled endpoints",
                "Endpoints ISE has not managed to profile. These fall through "
                "profile-based authorization rules, so a growing number here "
                "usually shows up later as unexpected policy matches.",
                [instant(gate(metric("ise3_endpoints_unprofiled"),
                              "endpoint_inventory"))],
                thresholds=NONZERO_WARNING,
                sparkline=True,
                no_value=NO_DATA_STALE,
            ),
            STAT_H,
            FIFTH,
        ),
        sized(
            stat_panel(
                "Network devices",
                "Network devices configured in ISE, for scale beside the "
                "endpoint counts. Everything about those devices — which are "
                "failing, which are silent, who owns them — is on the Network "
                "devices dashboard rather than here.",
                [instant(gate(metric("ise3_network_devices_total"),
                              "network_devices"))],
                thresholds=NEUTRAL,
                sparkline=True,
                no_value=NO_DATA_STALE,
                data_links=(drilldown("Open — network devices", "ise3-nad"),),
            ),
            STAT_H,
            FIFTH,
        ),
        sized(
            stat_panel(
                "Endpoints eligible for posture",
                "Endpoints whose session says a posture agent applies to them. "
                "The denominator behind every compliance figure on the posture "
                "dashboard: a number well below the endpoint total means most "
                "of the estate is outside posture entirely.",
                [instant(summed(gate(metric("ise3_endpoints_posture_applicable",
                                            'applicable="yes"'),
                                     "endpoint_inventory")))],
                thresholds=NEUTRAL,
                sparkline=True,
                no_value=NO_DATA_STALE,
                data_links=(drilldown("Why — posture", "ise3-posture"),),
            ),
            STAT_H,
            EIGHTH,
        ),
    ] + trust_pair(("endpoint_inventory", "network_devices"), span=EIGHTH)

    growth = [
        sized(
            ts(
                "Inventory over time",
                "Endpoints and network devices as counted at each collection. "
                "A step in either is a bulk import, a purge, or a collection "
                "problem — the change annotations across the top say which of "
                "those the exporter knows about.",
                [
                    query(gate(metric("ise3_endpoints_total"), "endpoint_inventory"),
                          "endpoints"),
                    query(gate(metric("ise3_network_devices_total"), "network_devices"),
                          "network devices", ref="B"),
                ],
            ),
            PANEL_H,
            HALF,
        ),
        sized(
            ts(
                "Profiling activity",
                "Profile change events by action and source. A burst of "
                "re-profiling is normal after a feed update and suspicious "
                "otherwise: endpoints changing profile change authorization.",
                [query(gate(metric("ise3_endpoint_profile_events"), "profile_events"),
                       "{{action}} · {{source}}")],
                filled=True,
            ),
            PANEL_H,
            HALF,
        ),
    ]

    composition = [
        sized(
            ranked(
                "Endpoints by profile",
                "The complete profile distribution, largest first. This is the "
                "estate as ISE understands it — worth comparing against what "
                "the network team believes is deployed.",
                gate(metric("ise3_endpoints_by_profile"), "endpoint_inventory"),
                label="profile",
                label_header="Profile",
                header="Endpoints",
            ),
            PANEL_H,
            THIRD,
        ),
        sized(
            ranked(
                "Endpoints by identity group",
                "Endpoint identity group membership, complete. Groups are what "
                "most authorization policies actually key on, so an unexpected "
                "distribution here explains unexpected policy matches.",
                gate(metric("ise3_endpoints_by_identity_group"), "endpoint_inventory"),
                label="identity_group",
                label_header="Identity group",
                header="Endpoints",
            ),
            PANEL_H,
            THIRD,
        ),
        sized(
            bar(
                "Endpoint field coverage",
                "Share of endpoints carrying each operationally useful "
                "attribute. Bounded between nought and one and only a handful "
                "of fields, so the bar length is genuinely the message: a short "
                "bar is a field no policy or report can rely on.",
                [instant(gate(metric("ise3_endpoint_inventory_field_coverage"),
                              "endpoint_inventory"), "{{field}}")],
                unit="percentunit",
                thresholds=COVERAGE,
                minimum=0,
                maximum=1,
            ),
            PANEL_H,
            THIRD,
        ),
    ]

    attributes = [
        sized(
            ranked(
                "Endpoints by hardware model",
                "Manufacturer and model as the live attribute feed reports it. "
                "Suppressed until the attribute cache is warm, so it cannot "
                "under-report beside the inventory counts above.",
                gate(metric("ise3_endpoint_model"), "endpoint_attributes"),
                label="model",
                label_header="Model",
                header="Endpoints",
            ),
            PANEL_H,
            THIRD,
        ),
        sized(
            ranked(
                "Endpoints by operating system",
                "Operating system distribution from the live attribute feed. "
                "Read beside the posture dashboard: an OS with no posture "
                "coverage is an OS nobody is assessing.",
                gate(metric("ise3_endpoint_operating_system"), "endpoint_attributes"),
                label="os",
                label_header="Operating system",
                header="Endpoints",
            ),
            PANEL_H,
            THIRD,
        ),
        sized(
            bar(
                "MDM registration and compliance",
                "Endpoints split by whether an MDM has them registered and "
                "whether it considers them compliant. Two bounded categories, "
                "which is what a bar gauge is for; anything unregistered is "
                "outside MDM enforcement entirely.",
                [
                    instant(gate(metric("ise3_endpoint_mdm_registered"),
                                 "endpoint_attributes"),
                            "registered: {{registered}}"),
                    instant(gate(metric("ise3_endpoint_mdm_compliant"),
                                 "endpoint_attributes"),
                            "compliant: {{compliant}}", ref="B"),
                ],
                thresholds=NEUTRAL,
            ),
            PANEL_H,
            THIRD,
        ),
    ]

    closing = [sized(about(
        "describe what is connected to the network and what ISE knows about "
        "it — the inventory view, not the incident view.",
        [
            "Read the endpoint counts and the unprofiled share.",
            "Use the composition tables to check the estate matches "
            "expectation.",
            "For anything about the switches and routers themselves — which "
            "are failing, which are silent, who owns them — go to the Network "
            "devices dashboard; this one is about what connects to them.",
        ],
        [
            "Growing unprofiled count: check the profiling feed before "
            "changing policy.",
            "Blank attribute panels: the detail cache is still filling, see "
            "the coverage panel on the Network devices dashboard.",
            "Endpoint total stepping without a matching import: read the "
            "change annotations before treating it as growth.",
        ],
        ["Triage", "RADIUS access", "Network devices", "Posture", "Capacity"],
    ), STRIP_H, FULL)]

    return assemble(
        "ISE · Endpoints",
        "ise3-endpoints",
        "Tier 2 diagnostic. What is connected to the network, and what does "
        "ISE know about it? Inventory and profiling first, then composition, "
        "then the live attribute detail. The devices themselves are on the "
        "Network devices dashboard.",
        [
            ("Inventory", inventory),
            ("Over time", growth),
            ("Composition", composition),
            ("Live endpoint attributes", attributes, COLLAPSED),
            ("Reference", closing, COLLAPSED),
        ],
        tier=DIAGNOSTIC,
    )


# ---------------------------------------------------------------------------
# Tier 2 — network devices (switches and routers)
#
# The device-owner's dashboard. Everything here is keyed by the network device
# rather than by ISE: which switch is failing, with which method, how much of
# its traffic that is, whether it is silent, and who owns it. The four
# variables narrow the whole page to one site, one owner, one platform, or one
# switch, which is the scope an operator actually works in.
# ---------------------------------------------------------------------------

NAD_FILTER = 'nad=~"$nad"'
# For the session-sourced authorization families, which ISE labels with the
# device's operations owner and nothing else: there is no location, platform or
# device to select on, so those panels answer for one owner's estate rather
# than for one switch, and say so.
NAD_OWNER = 'ops_owner=~"$ops_owner"'
NAD_SCOPE = ('ops_owner=~"$ops_owner",location=~"$location",'
             'device_type=~"$device_type",nad=~"$nad"')
NAD_ATTRIBUTES = ("ops_owner", "location", "device_type")
NAD_COLUMNS = {"nad": "Network device", "ops_owner": "Operations owner",
               "location": "Location", "device_type": "Type"}


def scoped_nad(name, dataset, selectors="", by="nad"):
    """One per-NAD metric, aggregated to the device and scoped to the variables.

    The aggregation is not decoration: `provider` differs between the sources
    behind these metrics, and a table merging four of them on their shared
    columns would split every device into one row per source without it.
    """
    selected = f"{NAD_FILTER},{selectors}" if selectors else NAD_FILTER
    return nad_attributed(
        summed(gate(metric(name, selected), dataset), by=by),
        NAD_SCOPE, NAD_ATTRIBUTES)


# What the method column says for a device whose failures carry no method
# pairing. The pairing is a bounded cross-product, and MnT and the activity
# view do not always agree, so a device can be failing with nothing to pair it
# to — which is a row to work, not a row to drop.
NO_METHOD = "not recorded"


def fix_list_rows():
    """The (device, method) pairs to be worked, one row each.

    Every device that is failing appears, whether or not the bounded
    device-and-method cross-product managed to name a method for it, and
    whether the failure came from MnT's error view or from the activity view.
    """
    methods = scoped_nad("ise3_radius_failures_by_nad_method",
                         "radius_reporting", by="nad,method")
    errors = scoped_nad("ise3_radius_errors_by_nad", "radius_errors")
    failed = scoped_nad("ise3_nad_authentications", "nad_health",
                        'status="failed"')
    trouble = f"(({errors}) > 0) or (({failed}) > 0)"
    unpaired = (f"label_replace(({trouble}) unless on(nad) ({methods}), "
                f'"method", "{NO_METHOD}", "", "")')
    return methods, unpaired


def fix_list_failures():
    """The failure count for each row, zero where no method could be paired."""
    methods, unpaired = fix_list_rows()
    return f"({methods}) or (({unpaired}) * 0)"


def per_device_column(expr):
    """A per-device number repeated down that device's method rows.

    The join is done here rather than left to the table's merge: a table merges
    frames on the columns they share, and a device-level frame has no method
    column to share, so one of its two method rows would silently come back
    empty. group_right makes the row skeleton the many side, so the device's
    figure lands on every row that names it.
    """
    methods, unpaired = fix_list_rows()
    skeleton = f"(({methods}) * 0 + 1) or (({unpaired}) * 0 + 1)"
    return f"({expr}) * on(nad) group_right() ({skeleton})"


def nad_dashboard():
    fleet = [
        sized(
            stat_panel(
                "Devices in scope",
                "Network devices matching the four variables above, from the "
                "ISE inventory. This is the denominator for everything else on "
                "the page: with All selected it is the whole configured "
                "estate, and a device the exporter has not classified yet "
                "counts here only once its group detail is cached.",
                [instant("count(max by (nad) (" +
                         gate(metric("ise3_network_device_assignment", NAD_SCOPE),
                              "network_devices") + "))")],
                thresholds=NEUTRAL,
                no_value=NO_DATA_STALE,
            ),
            STAT_H,
            FIFTH,
        ),
        sized(
            stat_panel(
                "Devices with errors",
                "How many of those devices produced at least one RADIUS error "
                "in the window. One device out of hundreds is a job for the "
                "fix list below; a whole location at once is an upstream or "
                "template problem rather than a device one.",
                [instant("count((" + scoped_nad("ise3_radius_errors_by_nad",
                                                "radius_errors") + ") > 0)")],
                thresholds=NONZERO_WARNING,
                sparkline=True,
                no_value=NO_DATA_CLEAN,
            ),
            STAT_H,
            FIFTH,
        ),
        sized(
            stat_panel(
                "Silent devices",
                "Configured devices that authenticated nothing in the scan "
                "window, beside the number that did. Both are counts of "
                "devices, and together they are the configured estate. "
                "Fleet-wide rather than scoped, because silence is measured "
                "against the whole inventory: a device absent from the "
                "inventory cannot be found missing from it. Read the activity "
                "feed age beside this before acting on either number.",
                [
                    instant(summed(gate(metric("ise3_nad_silent_total"),
                                        "nad_health")), "silent"),
                    instant(summed(gate(metric("ise3_nad_activity_covered"),
                                        "nad_health")), "seen active", ref="B"),
                ],
                thresholds=NONZERO_WARNING,
                overrides=[by_ref("B", thresholds=NEUTRAL)],
                no_value=NO_DATA_STALE,
            ),
            STAT_H,
            FIFTH,
        ),
        sized(
            stat_panel(
                "Devices classified",
                "Share of the configured estate carrying the location, type "
                "and owner groups this page's variables filter on. Low "
                "classification is why a device shows up as unknown here and "
                "in every other owner breakdown in the set.",
                [instant(share(
                    gate(metric("ise3_network_devices_classified"),
                         "network_devices"),
                    gate(metric("ise3_network_devices_total"), "network_devices")))],
                unit="percentunit",
                thresholds=COVERAGE,
                minimum=0,
                maximum=1,
                no_value=NO_DATA_STALE,
            ),
            STAT_H,
            EIGHTH,
        ),
    ] + trust_pair(("network_devices", "nad_health", "radius_errors",
                    "radius_reporting", "active_sessions",
                    "session_authorization"), span=EIGHTH)

    failing = [
        sized(
            tbl(
                "Device fix list",
                "The work queue: one row per network device and authentication "
                "method that is failing, worst first, with the owner and "
                "location ISE holds for the device and the device's own "
                "figures beside it. Read across a row rather than down a "
                "column — failures on one method while the passed count stays "
                "high is a supplicant or certificate problem on a subset of "
                "that device's clients, while failures with no passes and no "
                "sessions is the shared secret, the AAA configuration, or a "
                "device that cannot reach a PSN. A device's error, pass and "
                "session counts are per device, so they repeat across its "
                "method rows. A device failing with no method paired to it "
                f"reads {NO_METHOD!r}: the pairing is bounded for cost and MnT "
                "and the activity view need not agree, and a row is kept "
                "either way rather than dropped. Healthy devices are absent by "
                "design — silence is the table below, inventory the row above.",
                [
                    instant(fix_list_failures(), ref="A"),
                    instant(per_device_column(
                        scoped_nad("ise3_radius_errors_by_nad", "radius_errors")),
                        ref="B"),
                    instant(per_device_column(
                        scoped_nad("ise3_nad_authentications", "nad_health",
                                   'status="failed"')), ref="C"),
                    instant(per_device_column(
                        scoped_nad("ise3_nad_authentications", "nad_health",
                                   'status="passed"')), ref="D"),
                    instant(per_device_column(
                        scoped_nad("ise3_active_sessions_by_nad",
                                   "active_sessions")), ref="E"),
                ],
                columns=["Failures", "Errors", "Failed", "Passed", "Sessions"],
                sort=("Failures", True),
                labels={**NAD_COLUMNS, "method": "Method"},
                column_overrides=[
                    by_column("Failures", thresholds=NONZERO_WARNING,
                              colour_cells=True, width=100),
                    by_column("Errors", thresholds=NONZERO_WARNING, width=90),
                    by_column("Failed", width=90),
                    by_column("Passed", width=90),
                    by_column("Sessions", width=100),
                    by_column("Network device", links=[
                        drilldown("Isolate this device on RADIUS access",
                                  "ise3-access", nad=by_row("Network device")),
                        drilldown("Narrow this page to it", "ise3-nad",
                                  nad=by_row("Network device")),
                    ]),
                ],
            ),
            TALL_H,
            FULL,
        ),
    ]

    over_time = [
        sized(
            ts(
                "Errors over time, by device",
                "RADIUS errors per device across the window, for the devices "
                "in scope. A step is a change — a configuration push, a "
                "certificate rollover, a link event; a ramp is something "
                "degrading. Narrow the Network device variable to a handful "
                "before reading this on a large estate.",
                [query(scoped_nad("ise3_radius_errors_by_nad", "radius_errors"),
                       "{{nad}}")],
                series_colours=OUTCOME_COLOURS,
                legend_placement="right",
            ),
            PANEL_H,
            HALF,
        ),
        sized(
            ts(
                "Authentication volume, by device and outcome",
                "Passed and failed authentications per device over the same "
                "window. Read it against the errors beside it: volume "
                "collapsing to nothing is a device that stopped talking to "
                "ISE, which produces no errors and would otherwise look like "
                "a quiet night.",
                [query(scoped_nad("ise3_radius_authentications_by_nad",
                                  "radius_reporting", by="nad,status"),
                       "{{nad}} · {{status}}")],
                series_colours=OUTCOME_COLOURS,
                legend_placement="right",
            ),
            PANEL_H,
            HALF,
        ),
    ]

    silence = [
        sized(
            tbl(
                "Silent and stale devices",
                "Time since each device in scope last authenticated anything, "
                "longest first. A device at the top is decommissioned, "
                "misconfigured, or unable to reach ISE — and no other panel "
                "will tell you, because a silent device produces no errors. "
                "Check the activity feed age beside it before acting: a whole "
                "estate reported silent is a stale feed, not a dead estate.",
                [instant(scoped_nad("ise3_nad_last_authentication_age_seconds",
                                    "nad_health"))],
                columns=["Silent for"],
                sort=("Silent for", True),
                labels=NAD_COLUMNS,
                column_overrides=[
                    by_column("Silent for", unit="s", width=140),
                ],
            ),
            TALL_H,
            HALF,
        ),
        sized(
            stat_panel(
                "Activity feed age",
                "Age of the newest row in the activity view the silence "
                "figures are computed from, measured over the whole view "
                "rather than the scan window. Older than the window means "
                "every device will report silent whether or not it is: this "
                "panel is what stops that being read as an estate-wide "
                "outage. -1 means the view has never held a row.",
                [instant(summed(gate(metric("ise3_nad_activity_source_age_seconds"),
                                     "nad_health")))],
                unit="s",
                thresholds=COLLECTION_AGE,
                no_value=NO_DATA_STALE,
                data_links=(drilldown("Why — collection pipeline",
                                      "ise3-pipeline"),),
            ),
            STAT_H,
            HALF,
        ),
    ]

    authorization = [
        sized(
            tbl(
                "Policy sets by device",
                "Which policy set the live sessions on each device matched. "
                "This is the panel that answers 'which switches are still in "
                "open mode' — a device landing in an unexpected policy set is "
                "a device group, location, or NAD attribute problem rather "
                "than a policy one, and the fix is on the device's ISE "
                "configuration. Bounded by the exporter's top-devices limit; "
                "the coverage panel on the pipeline dashboard says how much of "
                "the estate is shown.",
                [instant(scoped_nad("ise3_session_policy_set_endpoints_by_nad",
                                    "session_authorization", by="nad,policy_set"))],
                columns=["Endpoints"],
                sort=("Endpoints", True),
                labels={**NAD_COLUMNS, "policy_set": "Policy set"},
                column_overrides=[
                    by_column("Network device", links=[
                        drilldown("Narrow this page to it", "ise3-nad",
                                  nad=by_row("Network device"))]),
                ],
            ),
            TALL_H,
            HALF,
        ),
        sized(
            ranked(
                "Authorization profiles being selected",
                "The authorization profiles live sessions are landing on, for "
                "the selected Operations owner. Scoped by owner alone and not "
                "by the other three variables: ISE attaches the owner to the "
                "session, so a location or a single device cannot be selected "
                "here. Read it after a policy change to confirm the change did "
                "what was intended.",
                summed(gate(metric("ise3_session_authz_profile_endpoints",
                                   NAD_OWNER), "session_authorization"),
                       by="authz_profile"),
                label="authz_profile",
                label_header="Authorization profile",
                header="Endpoints",
            ),
            TALL_H,
            QUARTER,
        ),
        sized(
            ranked(
                "Authorization rules being matched",
                "The authorization rules those sessions matched, for the "
                "selected Operations owner — the ground-truth open-mode versus "
                "closed-mode signal where rule names follow a convention. A "
                "catch-all rule carrying most of the traffic means the specific "
                "rules above it are not matching. Owner-scoped only, for the "
                "same reason as the profiles beside it.",
                summed(gate(metric("ise3_session_authz_rule_endpoints",
                                   NAD_OWNER), "session_authorization"),
                       by="authz_rule"),
                label="authz_rule",
                label_header="Authorization rule",
                header="Endpoints",
            ),
            TALL_H,
            QUARTER,
        ),
    ]

    authorization_failures = [
        sized(
            ranked(
                "Policy sets containing the failures",
                "Policy sets holding endpoints that are failing authorization "
                "right now, for the selected owner. A single policy set "
                "carrying every failure narrows the search to one branch of "
                "the policy tree.",
                summed(gate(metric("ise3_session_failed_policy_set_endpoints",
                                   NAD_OWNER), "session_authorization"),
                       by="policy_set"),
                label="policy_set",
                label_header="Policy set",
                header="Endpoints",
                value_thresholds=NONZERO_WARNING,
                colour_cells=True,
            ),
            PANEL_H,
            THIRD,
        ),
        sized(
            ranked(
                "Authorization rules the failures matched",
                "The rules those failing endpoints matched on their way to "
                "being denied. Rule plus profile is enough to find the policy "
                "line responsible.",
                summed(gate(metric("ise3_session_failed_authz_rule_endpoints",
                                   NAD_OWNER), "session_authorization"),
                       by="authz_rule"),
                label="authz_rule",
                label_header="Authorization rule",
                header="Endpoints",
                value_thresholds=NONZERO_WARNING,
                colour_cells=True,
            ),
            PANEL_H,
            THIRD,
        ),
        sized(
            ranked(
                "Authorization profiles the failures selected",
                "Profiles those endpoints matched on their way to failing. A "
                "profile appearing here that should never fail is usually a "
                "missing attribute or a broken downloadable ACL — a policy "
                "fault rather than a device one, so it is the one row of this "
                "page whose fix is not on the switch.",
                summed(gate(metric("ise3_session_failed_authz_profile_endpoints",
                                   NAD_OWNER), "session_authorization"),
                       by="authz_profile"),
                label="authz_profile",
                label_header="Authorization profile",
                header="Endpoints",
                value_thresholds=NONZERO_WARNING,
                colour_cells=True,
            ),
            PANEL_H,
            THIRD,
        ),
    ]

    estate = [
        sized(
            ranked(
                "Devices by type",
                "Network devices per device-type group — the switch and router "
                "platforms ISE believes it is talking to. A large unknown "
                "bucket means the Type variable on this page filters less than "
                "it appears to.",
                gate(metric("ise3_network_devices_by_type"), "network_devices"),
                label="device_type",
                label_header="Type",
                header="Devices",
            ),
            PANEL_H,
            THIRD,
        ),
        sized(
            ranked(
                "Devices by location",
                "Network devices per location group, computed by ISE rather "
                "than from the exporter's device cache — so this stays "
                "populated while that cache is still filling.",
                gate(metric("ise3_network_devices_by_location"), "network_devices"),
                label="location",
                label_header="Location",
                header="Devices",
            ),
            PANEL_H,
            THIRD,
        ),
        sized(
            ranked(
                "Devices by operations owner",
                "Network devices per operations owner. The lookup behind every "
                "owner-scoped panel in this set — a team missing here has its "
                "devices appearing as unknown everywhere else.",
                gate(metric("ise3_network_devices_by_ops_owner"), "network_devices"),
                label="ops_owner",
                label_header="Operations owner",
                header="Devices",
            ),
            PANEL_H,
            THIRD,
        ),
        sized(
            tbl(
                "Device inventory",
                "Every network device in scope with the type, location and "
                "operations owner ISE holds for it. The lookup table behind "
                "every grouping on this page; a device missing attributes here "
                "is why it appears as unknown above.",
                [instant(gate(metric("ise3_network_device_assignment", NAD_SCOPE),
                              "network_devices"))],
                columns=[None],
                labels=NAD_COLUMNS,
            ),
            TALL_H,
            HALF,
        ),
        sized(
            ts(
                "Device detail-cache coverage",
                "How much of the device list the exporter has fetched group "
                "detail for. Group membership needs one request per device, so "
                "a cold start fills over several cycles — a dip here explains "
                "devices appearing as unknown in the tables above.",
                [query(metric("ise3_detail_cache_coverage",
                              'cache="ers_network_device"'), "device detail")],
                unit="percentunit",
                thresholds=COVERAGE,
                minimum=0,
                maximum=1,
            ),
            PANEL_H,
            HALF,
        ),
    ]

    administration = [
        sized(
            tbl(
                "Device administration activity",
                "TACACS authentications against the network devices in scope, "
                "passed and failed. Failures concentrated on one device are "
                "usually its shared secret or an AAA configuration that "
                "drifted from the template — the same root cause as a RADIUS "
                "failure on that device, which is why both live on this page.",
                [
                    # Aggregated to the device: `status` differs between the
                    # two targets, and a table merges its frames on the columns
                    # they share, so leaving it on would split every device
                    # into a failed row and a passed row instead of giving it
                    # one row with two columns.
                    instant(summed(gate(metric("ise3_tacacs_authentications",
                                               'dimension="device",'
                                               'status="failed",value=~"$nad"'),
                                        "tacacs_activity"), by="value"),
                            ref="A"),
                    instant(summed(gate(metric("ise3_tacacs_authentications",
                                               'dimension="device",'
                                               'status="passed",value=~"$nad"'),
                                        "tacacs_activity"), by="value"),
                            ref="B"),
                ],
                columns=["Failed", "Passed"],
                sort=("Failed", True),
                labels={"value": "Network device"},
                column_overrides=[
                    by_column("Failed", thresholds=NONZERO_WARNING,
                              colour_cells=True, width=110),
                ],
            ),
            TALL_H,
            HALF,
        ),
        sized(
            tbl(
                "Who administered which device",
                "Individual authorization decisions on the devices in scope, "
                "with the account, policy, shell profile and command set "
                "involved. The audit answer to 'who touched this switch', and "
                "the first thing to read when a device's configuration changed "
                "without a ticket.",
                [instant(gate(metric("ise3_tacacs_authorization_details",
                                     'device=~"$nad"'), "tacacs_activity"))],
                columns=["Decisions"],
                sort=("Decisions", True),
                labels={"username": "Account", "device": "Network device",
                        "policy": "Policy", "shell_profile": "Shell profile",
                        "command_set": "Command set", "status": "Status"},
            ),
            TALL_H,
            HALF,
        ),
    ]

    closing = [sized(about(
        "answer which switch or router is broken, in what way, and who owns "
        "it — the device-owner's view rather than ISE's.",
        [
            "Set the variables to your scope: an owner, a location, a "
            "platform, or one device.",
            "Read **Devices with errors**. One device is a device problem; a "
            "whole location at once is not.",
            "Work the **Device fix list**: errors beside passes and sessions "
            "say whether the device is broken or merely busy.",
            "Use **Which device, failing which method** to turn a row into a "
            "concrete cause, then open the device on RADIUS access.",
            "Read **Policy sets by device** when the device authenticates but "
            "lands somewhere unexpected — that is an ISE device-group problem, "
            "not a switch one.",
            "Before closing, check **Silent and stale devices** — silence "
            "produces no errors and appears nowhere else.",
        ],
        [
            "Errors on one device with passes continuing: supplicant or "
            "certificate trouble on a subset of its clients.",
            "Errors with no passes and no sessions: shared secret, AAA "
            "configuration, or the device cannot reach a PSN.",
            "Every device silent at once: read the activity feed age before "
            "anything else — that is a collection fault, not an outage.",
            "Devices showing as unknown: their ISE group assignment is "
            "missing, and the detail-cache coverage panel says whether the "
            "exporter is still filling.",
        ],
        ["Triage", "RADIUS access", "Endpoints", "Device administration",
         "Collection pipeline"],
    ), STRIP_H, FULL)]

    return assemble(
        "ISE · Network devices",
        "ise3-nad",
        "Tier 2 diagnostic. Which switch or router is failing, in what way, "
        "and who owns it? Scoped by owner, location, platform and device — "
        "errors and volume first, then the method behind them, then silence.",
        [
            ("Device fleet", fleet),
            ("Which device is broken", failing),
            ("Over time", over_time),
            ("Authorization", authorization),
            ("Silence", silence),
            ("Authorization failures", authorization_failures, COLLAPSED),
            ("The estate", estate, COLLAPSED),
            ("Device administration", administration, COLLAPSED),
            ("Reference", closing, COLLAPSED),
        ],
        tier=DIAGNOSTIC,
        variables=(
            label_variable("ops_owner", "Operations owner",
                           "ise3_network_devices_by_ops_owner", "ops_owner"),
            label_variable("location", "Location",
                           "ise3_network_devices_by_location", "location"),
            label_variable("device_type", "Type",
                           "ise3_network_devices_by_type", "device_type"),
            label_variable("nad", "Network device",
                           "ise3_network_device_assignment", "nad"),
        ),
    )


# ---------------------------------------------------------------------------
# Tier 2 — posture
# ---------------------------------------------------------------------------

POSTURE_COMPLIANT = gate(
    metric("ise3_posture_endpoints", 'status=~"Compliant|compliant"'),
    "posture_current")
POSTURE_ALL = gate(metric("ise3_posture_endpoints"), "posture_current")


def posture_dashboard():
    now = [
        sized(
            stat_panel(
                "Compliant share",
                "Share of endpoints with a live posture result that ISE "
                "considers compliant. The headline compliance number — but "
                "read it beside the assessment coverage stat, because it only "
                "counts endpoints that were assessed at all.",
                [instant(share(summed(POSTURE_COMPLIANT), summed(POSTURE_ALL)))],
                unit="percentunit",
                thresholds=RATIO_HIGH_IS_GOOD,
                minimum=0,
                maximum=1,
                sparkline=True,
                no_value=NO_DATA_STALE,
            ),
            STAT_H,
            FIFTH,
        ),
        sized(
            stat_panel(
                "Non-compliant endpoints",
                "Endpoints currently connected with a posture status that is "
                "not compliant. These are the ones sitting in a quarantine or "
                "remediation authorization profile right now.",
                [instant(summed(gate(
                    metric("ise3_posture_endpoints",
                           'status!~"Compliant|compliant"'), "posture_current")))],
                thresholds=NONZERO_WARNING,
                sparkline=True,
                no_value=NO_DATA_STALE,
            ),
            STAT_H,
            FIFTH,
        ),
        sized(
            stat_panel(
                "Eligible but unassessed",
                "Endpoints eligible for posture that have no recent assessment "
                "at all. The most under-read number in posture reporting: "
                "these are not compliant and not non-compliant, they are "
                "invisible, and the compliant share above excludes them.",
                [instant(gate(metric("ise3_posture_eligible_without_recent_assessment_total"),
                              "posture_history"))],
                thresholds=NONZERO_WARNING,
                sparkline=True,
                no_value=NO_DATA_STALE,
            ),
            STAT_H,
            FIFTH,
        ),
        sized(
            stat_panel(
                "Assessment coverage",
                "Share of posture-eligible endpoints that were actually "
                "assessed recently. This is the denominator honesty check for "
                "the compliant share: low coverage makes a high compliance "
                "figure meaningless.",
                [instant(share(
                    gate(metric("ise3_posture_eligible_recently_assessed_total"),
                         "posture_history"),
                    gate(metric("ise3_posture_eligible_endpoints_total"),
                         "posture_history")))],
                unit="percentunit",
                thresholds=COVERAGE,
                minimum=0,
                maximum=1,
                no_value=NO_DATA_STALE,
            ),
            STAT_H,
            EIGHTH,
        ),
    ] + trust_pair(("posture_current", "posture_history"), span=EIGHTH)

    over_time = [
        sized(
            ts(
                "Posture status over time",
                "Live posture status of connected endpoints, stacked because "
                "the statuses together are the whole assessed population. A "
                "widening non-compliant band without a matching drop in "
                "compliant usually means new endpoints arrived unassessed.",
                [query(summed(POSTURE_ALL, by="status"), "{{status}}")],
                stacked=True,
                series_colours=OUTCOME_COLOURS,
            ),
            PANEL_H,
            HALF,
        ),
        sized(
            ts(
                "Assessment volume per PSN",
                "Posture assessments each PSN performed, split by outcome. "
                "Uneven distribution is a load-balancing question; a node "
                "producing only failures is a node whose posture "
                "configuration or connectivity is broken.",
                [query(gate(metric("ise3_posture_assessments_by_psn"),
                            "posture_history"), "{{psn}} · {{status}}")],
                series_colours=OUTCOME_COLOURS,
            ),
            PANEL_H,
            HALF,
        ),
    ]

    where = [
        sized(
            ranked(
                "Non-compliance by owner",
                "Live posture status grouped by the operations owner attribute "
                "of the network device the endpoint is attached to. This is "
                "the panel that turns a compliance number into a list of teams "
                "to talk to.",
                summed(gate(metric("ise3_posture_endpoints",
                                   'status!~"Compliant|compliant"'),
                            "posture_current"), by="ops_owner"),
                label="ops_owner",
                label_header="Operations owner",
                header="Non-compliant",
                value_thresholds=NONZERO_WARNING,
                colour_cells=True,
            ),
            PANEL_H,
            THIRD,
        ),
        sized(
            ranked(
                "Failing posture policies",
                "Posture policies with failing results, worst first. A single "
                "policy dominating usually means the policy is wrong rather "
                "than the fleet — check what it requires before chasing "
                "endpoints. Served from the assessment history, which carries "
                "the per-policy verdict as its own column; the live MnT "
                "breakdown is the fallback where that view is unavailable.",
                (summed(gate(metric("ise3_posture_failing_policies"),
                             "posture_history"), by="policy")
                 + " or "
                 + summed(gate(metric("ise3_posture_policy_results",
                                      'result!~"pass|Passed|passed"'),
                               "posture_current"), by="policy")),
                label="policy",
                label_header="Posture policy",
                header="Failures",
                value_thresholds=NONZERO_WARNING,
                colour_cells=True,
            ),
            PANEL_H,
            THIRD,
        ),
        sized(
            ranked(
                "Failed conditions",
                "The individual posture conditions endpoints failed — the "
                "specific missing patch, absent process, or out-of-date "
                "definition file. The most directly actionable panel here.",
                gate(metric("ise3_posture_failed_conditions"), "posture_history"),
                label="condition",
                label_header="Condition",
                header="Endpoints",
                value_thresholds=NONZERO_WARNING,
                colour_cells=True,
            ),
            PANEL_H,
            THIRD,
        ),
        sized(
            tbl(
                "Failing requirements, by mandate",
                "The requirement level, which a policy hides: a policy rolls "
                "its requirements up, so an Audit requirement that failed "
                "leaves the policy reading Passed. Mandate is the column that "
                "matters — a failed Mandatory requirement is denying access, "
                "while Audit and Optional only record. A long list with nothing "
                "Mandatory in it means posture is running in observation mode.",
                [instant(gate(metric("ise3_posture_requirement_results",
                                     'result!~"Passed|passed|pass"'),
                              "posture_current"))],
                columns=["Endpoints"],
                sort=("Endpoints", True),
                labels={"requirement": "Requirement", "mandate": "Mandate",
                        "result": "Result"},
                thresholds=NONZERO_WARNING,
            ),
            PANEL_H,
            THIRD,
        ),
    ]

    population = [
        sized(
            ranked(
                "Secure Client versions",
                "Agent versions across endpoints with a live posture result. "
                "Old agents are the usual explanation for a cluster of "
                "unassessed or failing endpoints in one part of the estate.",
                gate(metric("ise3_posture_agent_version_endpoints"),
                     "posture_current"),
                label="agent_version",
                label_header="Secure Client version",
                header="Endpoints",
            ),
            PANEL_H,
            HALF,
        ),
        sized(
            ranked(
                "Operating systems assessed",
                "Operating systems among posture-assessed endpoints. Compare "
                "against the endpoints dashboard's OS breakdown: an OS present "
                "there and absent here is an OS nobody is posturing.",
                gate(metric("ise3_posture_endpoints_by_os"), "posture_current"),
                label="os",
                label_header="Operating system",
                header="Endpoints",
            ),
            PANEL_H,
            HALF,
        ),
    ]

    history = [
        sized(
            stat_panel(
                "Historical compliance share",
                "Compliance across the assessment history window rather than "
                "the live session list. Diverging from the live figure means "
                "the population that is connected right now is not "
                "representative of the fleet.",
                [instant(share(
                    summed(gate(metric("ise3_posture_assessments",
                                       'status=~"Compliant|compliant|Pass|passed"'),
                                "posture_history")),
                    summed(gate(metric("ise3_posture_assessments"),
                                "posture_history"))))],
                unit="percentunit",
                thresholds=RATIO_HIGH_IS_GOOD,
                minimum=0,
                maximum=1,
                no_value=NO_DATA_STALE,
            ),
            STAT_H,
            THIRD,
        ),
        sized(
            stat_panel(
                "MnT session field coverage",
                "Share of MnT session records carrying the posture fields "
                "these panels read. Low coverage means the live posture "
                "picture is drawn from a subset of sessions, not all of them.",
                [instant(f"avg("
                         f"{gate(metric('ise3_session_detail_field_coverage'), 'posture_current')})")],
                unit="percentunit",
                thresholds=COVERAGE,
                minimum=0,
                maximum=1,
                no_value=NO_DATA_STALE,
            ),
            STAT_H,
            THIRD,
        ),
        sized(
            stat_panel(
                "Eligible endpoints",
                "How many endpoints are in scope for posture, as the "
                "assessment history counts them and as the endpoint inventory "
                "counts them. The denominator behind assessment coverage — the "
                "two disagreeing means posture scope and inventory scope are "
                "not the same population.",
                [
                    instant(gate(metric("ise3_posture_eligible_endpoints_total"),
                                 "posture_history"), "eligible"),
                    instant(gate(metric("ise3_endpoints_posture_applicable",
                                        'applicable="yes"'), "endpoint_inventory"),
                            "applicable", ref="B"),
                ],
                thresholds=NEUTRAL,
                no_value=NO_DATA_STALE,
            ),
            STAT_H,
            THIRD,
        ),
        sized(
            tbl(
                "Live posture by PSN",
                "Current posture status of connected endpoints, per serving "
                "PSN. A node whose endpoints are disproportionately unknown or "
                "non-compliant usually has a posture configuration or "
                "connectivity problem of its own.",
                [instant(gate(metric("ise3_posture_endpoints_by_psn"),
                              "posture_current"))],
                columns=["Endpoints"],
                sort=("Endpoints", True),
                labels={"psn": "PSN", "status": "Status"},
            ),
            PANEL_H,
            HALF,
        ),
        sized(
            ts(
                "Historical assessment outcomes",
                "Assessment results over the history window, stacked by "
                "status. The trend view of compliance, as opposed to the live "
                "snapshot at the top of the page.",
                [query(summed(gate(metric("ise3_posture_assessments"),
                                   "posture_history"), by="status"),
                       "{{status}}")],
                stacked=True,
                series_colours=OUTCOME_COLOURS,
            ),
            PANEL_H,
            HALF,
        ),
        sized(
            tbl(
                "Assessment results by policy",
                "Historical assessment counts per policy and status. Read it "
                "when a policy has been changed and you need to see whether "
                "the change did what was intended.",
                [instant(gate(metric("ise3_posture_assessments_by_policy"),
                              "posture_history"))],
                columns=["Assessments"],
                sort=("Assessments", True),
                labels={"policy": "Posture policy", "status": "Status"},
            ),
            PANEL_H,
            HALF,
        ),
    ]

    closing = [sized(about(
        "answer whether the fleet is compliant, and separate 'assessed and "
        "failing' from 'never assessed at all'.",
        [
            "Read compliant share and assessment coverage together — one "
            "without the other is misleading.",
            "Read **Eligible but unassessed**: invisible endpoints are the "
            "real gap.",
            "Use failed conditions to get to something actionable.",
        ],
        [
            "High compliance, low coverage: the number is not trustworthy; fix "
            "the agents before reporting it.",
            "One policy dominating failures: inspect the policy, not the "
            "endpoints.",
            "One PSN producing all failures: continue on the PSN service "
            "dashboard.",
        ],
        ["Triage", "Endpoints and devices", "PSN service"],
    ), STRIP_H, FULL)]

    return assemble(
        "ISE · Posture",
        "ise3-posture",
        "Tier 2 diagnostic. Is the fleet compliant, and is the compliance "
        "figure trustworthy? Live posture first, then where non-compliance "
        "sits, then the client population and the assessment history.",
        [
            ("Compliance now", now),
            ("Over time", over_time),
            ("Where non-compliance sits", where),
            ("Client population", population),
            ("Assessment history", history, COLLAPSED),
            ("Reference", closing, COLLAPSED),
        ],
        tier=DIAGNOSTIC,
    )


# ---------------------------------------------------------------------------
# Tier 2 — device administration
# ---------------------------------------------------------------------------

TACACS_PASSED = summed(gate(
    metric("ise3_tacacs_authentications", 'dimension="username",status="passed"'),
    "tacacs_activity"))
TACACS_ALL = summed(gate(
    metric("ise3_tacacs_authentications", 'dimension="username"'),
    "tacacs_activity"))


def tacacs_dashboard():
    activity = [
        sized(
            stat_panel(
                "Authentication success rate",
                "Share of TACACS+ device administration logins that succeeded. "
                "Unlike RADIUS, a low rate here is usually a small number of "
                "people getting it wrong rather than a systemic fault — read "
                "the per-account table before escalating.",
                [instant(share(TACACS_PASSED, TACACS_ALL))],
                unit="percentunit",
                thresholds=RATIO_HIGH_IS_GOOD,
                minimum=0,
                maximum=1,
                sparkline=True,
                no_value=NO_DATA_STALE,
            ),
            STAT_H,
            FIFTH,
        ),
        sized(
            stat_panel(
                "Authorizations",
                "TACACS+ authorization decisions in the window. A step change "
                "without a matching change in authentications means shell "
                "profiles or command sets changed, not the number of people "
                "logging in.",
                [instant(summed(gate(metric("ise3_tacacs_authorizations",
                                            'dimension="username"'),
                                     "tacacs_activity")))],
                thresholds=NEUTRAL,
                sparkline=True,
                no_value=NO_DATA_STALE,
            ),
            STAT_H,
            FIFTH,
        ),
        sized(
            stat_panel(
                "Commands accounted",
                "Commands recorded by TACACS+ accounting. This is the audit "
                "trail; a collapse to zero while authentications continue "
                "means command accounting has been switched off somewhere.",
                [instant(summed(gate(metric("ise3_tacacs_commands",
                                            'dimension="username"'),
                                     "tacacs_activity")))],
                thresholds=NEUTRAL,
                sparkline=True,
                no_value=NO_DATA_STALE,
            ),
            STAT_H,
            FIFTH,
        ),
        sized(
            stat_panel(
                "Internal accounts",
                "Internal device-administration accounts configured in ISE, "
                "and how many showed activity in the window. A large gap "
                "between the two is the hygiene problem the review queue below "
                "enumerates.",
                [
                    instant(gate(metric("ise3_tacacs_internal_accounts"),
                                 "tacacs_config"), "configured"),
                    instant(gate(metric("ise3_tacacs_active_accounts"),
                                 "tacacs_activity"), "active", ref="B"),
                ],
                thresholds=NEUTRAL,
                no_value=NO_DATA_STALE,
            ),
            STAT_H,
            EIGHTH,
        ),
    ] + trust_pair(("tacacs_activity", "tacacs_config"), span=EIGHTH)

    over_time = [
        sized(
            ts(
                "Authentication outcome over time",
                "TACACS+ logins by outcome, stacked. Repeated failures from "
                "one account are usually a stale saved credential on a "
                "jumpbox; failures across many accounts at once are an "
                "identity-store problem.",
                [query(summed(gate(metric("ise3_tacacs_authentications",
                                          'dimension="username"'), "tacacs_activity"),
                              by="status"), "{{status}}")],
                stacked=True,
                series_colours=OUTCOME_COLOURS,
            ),
            PANEL_H,
            HALF,
        ),
        sized(
            ts(
                "Authorization outcome over time",
                "Authorization decisions by outcome. Failures here are people "
                "being refused a command or a shell profile — expected in "
                "small numbers, worth investigating as a pattern.",
                [query(summed(gate(metric("ise3_tacacs_authorizations",
                                          'dimension="username"'), "tacacs_activity"),
                              by="status"), "{{status}}")],
                stacked=True,
                series_colours=OUTCOME_COLOURS,
            ),
            PANEL_H,
            HALF,
        ),
    ]

    who = [
        sized(
            tbl(
                "Activity by account",
                "Every account with device-administration activity in the "
                "window, passed and failed. Sorted by failures because that is "
                "what needs attention; the whole list is present, so this "
                "doubles as the audit answer to 'who logged into the network "
                "kit'.",
                [
                    # Aggregated to the account: `status` differs between the
                    # two targets, and a table merges its frames on the columns
                    # they share, so leaving it on splits every account into a
                    # failed row and a passed row.
                    instant(summed(gate(metric("ise3_tacacs_authentications",
                                               'dimension="username",'
                                               'status="failed"'),
                                        "tacacs_activity"), by="value"),
                            ref="A"),
                    instant(summed(gate(metric("ise3_tacacs_authentications",
                                               'dimension="username",'
                                               'status="passed"'),
                                        "tacacs_activity"), by="value"),
                            ref="B"),
                ],
                columns=["Failed", "Passed"],
                sort=("Failed", True),
                labels={"value": "Account"},
                column_overrides=[
                    by_column("Failed", thresholds=NONZERO_WARNING,
                              colour_cells=True, width=110),
                ],
            ),
            TALL_H,
            HALF,
        ),
        sized(
            tbl(
                "Activity by device",
                "Device-administration activity per network device. A device "
                "with many failures is often one where the shared secret or "
                "the AAA configuration drifted from the template.",
                [
                    # Aggregated to the device: `status` differs between the
                    # two targets, and a table merges its frames on the columns
                    # they share, so leaving it on splits every device into a
                    # failed row and a passed row.
                    instant(summed(gate(metric("ise3_tacacs_authentications",
                                               'dimension="device",'
                                               'status="failed"'),
                                        "tacacs_activity"), by="value"),
                            ref="A"),
                    instant(summed(gate(metric("ise3_tacacs_authentications",
                                               'dimension="device",'
                                               'status="passed"'),
                                        "tacacs_activity"), by="value"),
                            ref="B"),
                ],
                columns=["Failed", "Passed"],
                sort=("Failed", True),
                labels={"value": "Network device"},
                column_overrides=[
                    by_column("Failed", thresholds=NONZERO_WARNING,
                              colour_cells=True, width=110),
                ],
            ),
            TALL_H,
            HALF,
        ),
    ]

    hygiene = [
        sized(
            tbl(
                "Account hygiene review queue",
                "Internal accounts the exporter has flagged as a hygiene risk, "
                "with the reason. This is a standing review list rather than "
                "an incident panel — but it is the one thing on this dashboard "
                "an auditor will ask for.",
                [instant(gate(metric("ise3_tacacs_internal_account_hygiene_risk"),
                              "tacacs_config"))],
                columns=["Flagged"],
                sort=("Flagged", True),
                labels={"username": "Account", "risk": "Risk"},
                column_overrides=[
                    by_column("Flagged", thresholds=NONZERO_WARNING,
                              colour_cells=True, width=100),
                ],
            ),
            TALL_H,
            HALF,
        ),
        sized(
            tbl(
                "Last evidence of use, per account",
                "When each account was last seen authenticating or "
                "authorizing, oldest first. An enabled account with no recent "
                "evidence is the classic dormant-credential finding, and this "
                "is where it becomes visible.",
                [
                    instant("time() - (" + gate(
                        metric("ise3_tacacs_account_last_seen_timestamp"),
                        "tacacs_activity") + ")", ref="A"),
                    instant(gate(metric("ise3_tacacs_internal_account_enabled"),
                                 "tacacs_config"), ref="B"),
                ],
                columns=["Last seen", "Enabled"],
                sort=("Last seen", True),
                labels={"username": "Account", "event_type": "Event"},
                column_overrides=[
                    by_column("Last seen", unit="s", width=130),
                    by_column("Enabled", **BOOLEAN_CELL),
                ],
            ),
            TALL_H,
            HALF,
        ),
    ]

    policy = [
        sized(
            tbl(
                "Policy rule inventory",
                "Authentication and authorization rule counts per device "
                "administration policy set. Sudden growth in a rule count is a "
                "policy change nobody announced; a set with no rules is dead "
                "configuration.",
                [instant(gate(metric("ise3_tacacs_policy_rule_count"),
                              "tacacs_policy_rules"))],
                columns=["Rules"],
                sort=("Rules", True),
                labels={"policy_set": "Policy set", "rule_type": "Rule type"},
            ),
            PANEL_H,
            HALF,
        ),
        sized(
            bar(
                "Configured device administration objects",
                "How many of each Device Admin object type exist — shell "
                "profiles, command sets, policy sets. A bounded handful of "
                "categories, which is what a bar gauge suits; the shape is the "
                "size of the configuration surface.",
                [instant(gate(metric("ise3_tacacs_policy_objects"), "tacacs_config"),
                         "{{object_type}}")],
                thresholds=NEUTRAL,
            ),
            PANEL_H,
            HALF,
        ),
        sized(
            stat_panel(
                "Policy sets and rules",
                "How many device administration policy sets exist and how many "
                "rules they contain in total. The size of the configuration "
                "surface: a rule total that jumps is a policy change nobody "
                "announced, and the per-set table beside it says where.",
                [
                    instant(gate(metric("ise3_tacacs_policy_sets"), "tacacs_config"),
                            "policy sets"),
                    instant(summed(gate(metric("ise3_tacacs_policy_rules_total"),
                                        "tacacs_policy_rules")), "rules", ref="B"),
                ],
                thresholds=NEUTRAL,
                no_value=NO_DATA_STALE,
            ),
            STAT_H,
            HALF,
        ),
        sized(
            ranked(
                "Command families",
                "The first word of each accounted command, ranked. Enough to "
                "see the shape of what engineers are doing without ever "
                "recording a full command line as a metric label.",
                gate(metric("ise3_tacacs_commands", 'dimension="command_family"'),
                     "tacacs_activity"),
                label="value",
                label_header="Command family",
                header="Commands",
            ),
            PANEL_H,
            HALF,
        ),
        sized(
            tbl(
                "Authorization detail",
                "Individual authorization decisions with the policy, shell "
                "profile, command set, account and device involved. The "
                "forensic view: use it when one row of the summary tables "
                "above needs explaining.",
                [instant(gate(metric("ise3_tacacs_authorization_details"),
                              "tacacs_activity"))],
                columns=["Decisions"],
                sort=("Decisions", True),
                labels={"username": "Account", "device": "Network device",
                        "policy": "Policy", "shell_profile": "Shell profile",
                        "command_set": "Command set", "status": "Status"},
            ),
            TALL_H,
            HALF,
        ),
    ]

    closing = [sized(about(
        "show who is administering the network devices, whether they are "
        "succeeding, and whether the account estate behind it is clean.",
        [
            "Read the success rate and the account/active gap.",
            "Work the per-account and per-device failure columns.",
            "Review the hygiene queue and the last-seen table on a schedule, "
            "not only during an incident.",
        ],
        [
            "Failures concentrated on one account: a stale saved credential.",
            "Failures concentrated on one device: check its AAA configuration "
            "against the template.",
            "Command accounting at zero while logins continue: accounting is "
            "off somewhere, and the audit trail has a hole in it.",
        ],
        ["Triage", "Endpoints and devices", "Collection pipeline"],
    ), STRIP_H, FULL)]

    return assemble(
        "ISE · Device administration",
        "ise3-tacacs",
        "Tier 2 diagnostic. Who is administering the network devices over "
        "TACACS+, are they succeeding, and is the account estate behind it "
        "clean? Activity first, then accounts and devices, then hygiene and "
        "policy.",
        [
            ("Device administration activity", activity),
            ("Over time", over_time),
            ("Who and where", who),
            ("Account hygiene", hygiene),
            ("Policy and commands", policy, COLLAPSED),
            ("Reference", closing, COLLAPSED),
        ],
        tier=DIAGNOSTIC,
    )


# ---------------------------------------------------------------------------
# Tier 3 — collection pipeline
# ---------------------------------------------------------------------------

def pipeline_dashboard():
    trust = [
        sized(
            stat_panel(
                "Datasets trustworthy",
                "How many datasets both collected successfully and are inside "
                "their freshness window. This is the number that decides "
                "whether to believe the other ten dashboards.",
                [instant(f"count(({readiness_per_dataset()}) == 1)")],
                thresholds=NEUTRAL,
                no_value=NO_DATA_EXPORTER,
            ),
            STAT_H,
            SIXTH,
        ),
        sized(
            stat_panel(
                "Datasets not collecting",
                "Datasets that are enabled but currently either failing or "
                "stale. Every panel fed by one of these is blank or frozen "
                "somewhere in the set.",
                [instant(f"count(({readiness_per_dataset()}) == 0)")],
                thresholds=NONZERO_CRITICAL,
                no_value=NO_DATA_EXPORTER,
            ),
            STAT_H,
            SIXTH,
        ),
        sized(
            stat_panel(
                "Datasets on a fallback provider",
                "Datasets being served by something other than their preferred "
                "source. Not a failure — the exporter is doing its job — but "
                "the data may be coarser or older than the design intended, "
                "and the reason table below says why.",
                [instant(summed(metric("ise3_dataset_provider_degraded")))],
                thresholds=NONZERO_WARNING,
                no_value=NO_DATA_EXPORTER,
            ),
            STAT_H,
            SIXTH,
        ),
        sized(
            stat_panel(
                "Oldest successful collection",
                "Seconds since the least recently successful dataset last "
                "landed. The single worst-case staleness anywhere in the "
                "exporter's view of ISE.",
                [instant("max(time() - (" +
                         _healthy("ise3_dataset_last_success_timestamp", "",
                                  "dataset") + "))")],
                unit="s",
                thresholds=COLLECTION_AGE,
                no_value=NO_DATA_EXPORTER,
            ),
            STAT_H,
            SIXTH,
        ),
        sized(
            stat_panel(
                "Worst consecutive failures",
                "The longest current run of consecutive failures on any "
                "dataset. One failure is noise; a run means something is "
                "broken rather than flaky, and the failure table below has the "
                "error text.",
                [instant(f"max({metric('ise3_dataset_consecutive_failures')})")],
                thresholds=NONZERO_WARNING,
                no_value=NO_DATA_EXPORTER,
            ),
            STAT_H,
            SIXTH,
        ),
        sized(
            stat_panel(
                "Exporter build",
                "Which exporter version is running and which ISE release it "
                "was built to target. A mismatch between this and the "
                "appliance's actual release is the first thing to check when "
                "panels are unexpectedly empty.",
                [instant(metric("ise3_exporter_build_info"),
                         "{{version}} → ISE {{target_ise_release}}")],
                thresholds=NEUTRAL,
                text_mode=BigValueTextMode.NAME,
                colour_mode=BigValueColorMode.NONE,
                no_value=NO_DATA_EXPORTER,
            ),
            STAT_H,
            SIXTH,
        ),
        sized(
            states(
                "Dataset trustworthiness over time",
                "One lane per dataset, green while that dataset was both "
                "collecting and fresh. Every unexplained step, gap or plateau "
                "on any other dashboard should be checked against this panel "
                "before it is treated as an ISE event.",
                [query(readiness_per_dataset(), "{{dataset}}")],
            ),
            PANEL_H,
            FULL,
        ),
    ]

    detail = [
        sized(
            tbl(
                "Dataset status",
                "Every dataset with whether it is up and fresh, how long since "
                "it last succeeded, how long its collection takes, and when it "
                "runs next. Read 'Age' against 'Since attempt': the two far "
                "apart means the dataset is being attempted and failing, the "
                "two together means it simply has not run.",
                [
                    instant(_healthy("ise3_dataset_up", "", "dataset"),
                            ref="A"),
                    instant(_healthy("ise3_dataset_fresh", "", "dataset"),
                            ref="B"),
                    instant("time() - (" + _healthy(
                        "ise3_dataset_last_success_timestamp", "",
                        "dataset") + ")", ref="C"),
                    instant(f"max by (dataset) ("
                            f"{metric('ise3_dataset_collection_duration_seconds')})",
                            ref="D"),
                    instant(metric("ise3_dataset_interval_seconds"), ref="E"),
                    instant("time() - (" + _healthy(
                        "ise3_dataset_last_attempt_timestamp", "",
                        "dataset") + ")", ref="F"),
                    instant("(" + _healthy("ise3_dataset_next_run_timestamp", "",
                                           "dataset") + ") - time()",
                            ref="G"),
                ],
                columns=["Up", "Fresh", "Age", "Duration", "Interval",
                         "Since attempt", "Next run in"],
                sort=("Age", True),
                labels={"dataset": "Dataset"},
                column_overrides=[
                    by_column("Up", **BOOLEAN_CELL),
                    by_column("Fresh", **BOOLEAN_CELL),
                    by_column("Age", unit="s", thresholds=COLLECTION_AGE,
                              colour_cells=True),
                    by_column("Duration", **SECONDS_CELL),
                    by_column("Interval", **SECONDS_CELL),
                    by_column("Since attempt", **SECONDS_CELL),
                    by_column("Next run in", **SECONDS_CELL),
                ],
            ),
            TALL_H,
            HALF,
        ),
        sized(
            tbl(
                "Latest failure per dataset",
                "The most recent failure each dataset recorded, with the "
                "reason and the detail text the exporter captured from ISE. "
                "Empty is healthy. This is the panel that turns 'collection "
                "failed' into something specific enough to act on.",
                [instant(metric("ise3_dataset_last_failure_detail_info"))],
                columns=[None],
                labels={"dataset": "Dataset", "provider": "Provider",
                        "reason": "Reason", "detail": "Detail"},
            ),
            TALL_H,
            HALF,
        ),
        sized(
            tbl(
                "Provider selection and fallback reason",
                "Which provider each dataset is using, whether that is a "
                "downgrade from the preferred one, and the reason the exporter "
                "recorded for choosing it. The whole argument for this "
                "exporter's design is that a source change is visible rather "
                "than silent — this is where it is visible.",
                [
                    instant(f"{metric('ise3_dataset_provider_active')} == 1", ref="A"),
                    instant(metric("ise3_dataset_provider_degraded"), ref="B"),
                    instant(metric("ise3_dataset_provider_reason_info"), ref="C"),
                ],
                columns=[None, "Degraded", None],
                labels={"dataset": "Dataset", "provider": "Provider",
                        "reason": "Reason"},
                column_overrides=[
                    by_column("Degraded", mappings=YES_NO,
                              thresholds=NONZERO_WARNING, colour_cells=True,
                              width=110),
                ],
            ),
            TALL_H,
            HALF,
        ),
        sized(
            tbl(
                "Declared but unusable providers",
                "Providers configured for a dataset that the exporter could "
                "not use. Each row is a capability the deployment was "
                "configured to have and does not — usually a licence, a "
                "credential, or a schema the appliance does not expose.",
                [instant(f"{metric('ise3_dataset_provider_available')} == 0")],
                columns=[None],
                labels={"dataset": "Dataset", "provider": "Provider"},
            ),
            TALL_H,
            HALF,
        ),
    ]

    transports = [
        sized(
            ts(
                "Collection failures by reason",
                "Failure rate per dataset and reason. The consecutive-failure "
                "stat above says whether something is broken now; this says "
                "how often it has been breaking and for which stated reason, "
                "which is what separates a flaky appliance from a "
                "misconfiguration.",
                [query(f"sum by (dataset,reason) (rate("
                       f"{metric('ise3_dataset_failures_total')}[$__rate_interval]))",
                       "{{dataset}} · {{reason}}")],
                legend_placement="right",
            ),
            PANEL_H,
            HALF,
        ),
        sized(
            ts(
                "Data Connect statements",
                "Query rate against the Oracle reporting views, split by "
                "result. Failures here mean the reporting datasets fall back "
                "or go stale; the schema-gap table below usually explains "
                "them.",
                [query(f"sum by (result) (rate("
                       f"{metric('ise3_dataconnect_queries_total')}[$__rate_interval]))",
                       "{{result}}")],
                unit="reqps",
                series_colours=OUTCOME_COLOURS,
            ),
            PANEL_H,
            HALF,
        ),
        sized(
            ts(
                "Data Connect statement duration by view",
                "How long each reporting query is taking. Duration rising "
                "across all views is the appliance under load; one view rising "
                "alone is usually that view's data volume growing past what "
                "the current scan window can handle.",
                [query(metric("ise3_dataconnect_query_last_duration_seconds"),
                       "{{view}}")],
                unit="s",
            ),
            PANEL_H,
            HALF,
        ),
        sized(
            tbl(
                "Reporting source freshness",
                "Whether each Data Connect view exists in this appliance's "
                "schema at all, whether it has rows, whether it has recent "
                "ones, and how old its newest row is. A view that stops "
                "advancing makes every panel fed from it silently stale rather "
                "than obviously broken; a view that does not exist is a "
                "capability this release does not have.",
                [
                    instant(metric("ise3_source_probed"), ref="A"),
                    instant(metric("ise3_source_has_rows"), ref="B"),
                    instant(metric("ise3_source_has_recent_rows"), ref="C"),
                    instant(metric("ise3_source_latest_row_age_seconds"), ref="D"),
                    instant(metric("ise3_dataconnect_schema_view_available"),
                            ref="E"),
                ],
                columns=["Probed", "Has rows", "Recent", "Newest row age",
                         "View exists"],
                sort=("Newest row age", True),
                labels={"view": "View"},
                column_overrides=[
                    by_column("Probed", **BOOLEAN_CELL),
                    by_column("Has rows", **BOOLEAN_CELL),
                    by_column("Recent", **BOOLEAN_CELL),
                    by_column("Newest row age", **SECONDS_CELL),
                    by_column("View exists", **BOOLEAN_CELL),
                ],
            ),
            TALL_H,
            HALF,
        ),
        sized(
            tbl(
                "Schema capability gaps",
                "Columns this exporter wants that the appliance's Data Connect "
                "schema does not provide. Each row is a panel somewhere in the "
                "set that will be empty or coarser than designed, named at the "
                "column level rather than guessed at.",
                [instant(f"{metric('ise3_dataconnect_schema_column_available')} == 0")],
                columns=[None],
                labels={"view": "View", "column": "Column",
                        "requirement": "Requirement"},
            ),
            TALL_H,
            HALF,
        ),
        sized(
            ts(
                "ISE API request and error rate",
                "REST and OpenAPI traffic the exporter is generating, with the "
                "error rate beside it. Errors climbing here precede datasets "
                "falling back to a different provider.",
                [
                    query(f"sum(rate("
                          f"{metric('ise3_api_requests_total')}[$__rate_interval]))",
                          "requests"),
                    # The error counter's label space (error_type x http_code)
                    # is unbounded, so it is never seeded at startup and has no
                    # series until the first error. `or vector(0)` renders that
                    # absence as an explicit zero line -- otherwise the
                    # healthiest state this panel can show reads as "no data".
                    query(f"sum(rate("
                          f"{metric('ise3_api_errors_total')}[$__rate_interval])) "
                          f"or vector(0)",
                          "errors", ref="B"),
                ],
                unit="reqps",
                series_colours=OUTCOME_COLOURS,
            ),
            PANEL_H,
            HALF,
        ),
        sized(
            ts(
                "ISE API latency, 95th percentile",
                "How long the appliance is taking to answer, by API. Rising "
                "latency here is the earliest warning that collection "
                "durations and then freshness are about to suffer.",
                [query("histogram_quantile(0.95, sum by (api,le) (rate("
                       f"{metric('ise3_api_request_duration_seconds_bucket')}"
                       "[$__rate_interval])))", "{{api}}")],
                unit="s",
                thresholds=LATENCY_SECONDS,
            ),
            PANEL_H,
            HALF,
        ),
        sized(
            tbl(
                "pxGrid session stream",
                "Whether the pxGrid stream is connected, how many sessions it "
                "is tracking, how long since the last frame, and how often it "
                "has reconnected in this window. A silent but connected stream "
                "is the failure mode worth catching here; repeated reconnects "
                "mean it is flapping rather than working.",
                [
                    instant(metric("ise3_pxgrid_stream_connected"), ref="A"),
                    instant(metric("ise3_pxgrid_stream_sessions"), ref="B"),
                    instant("time() - " +
                            metric("ise3_pxgrid_stream_last_frame_timestamp"),
                            ref="C"),
                    instant(f"sum(increase("
                            f"{metric('ise3_pxgrid_stream_reconnects_total')}[$__range]))",
                            ref="D"),
                ],
                columns=["Connected", "Sessions", "Since last frame",
                         "Reconnects"],
                column_overrides=[
                    by_column("Connected", **BOOLEAN_CELL),
                    by_column("Since last frame", unit="s",
                              thresholds=COLLECTION_AGE, colour_cells=True),
                    by_column("Reconnects", thresholds=NONZERO_WARNING,
                              colour_cells=True),
                ],
            ),
            PANEL_H,
            HALF,
        ),
    ]

    bounds = [
        sized(
            ts(
                "Detail-cache coverage",
                "How much of each entity list the exporter has full detail "
                "for. Panels fed from a cache are suppressed below 99% so they "
                "cannot under-report beside a complete neighbour — a dip here "
                "is the explanation for a table that has gone blank.",
                [query(metric("ise3_detail_cache_coverage"), "{{cache}}")],
                unit="percentunit",
                thresholds=COVERAGE,
                minimum=0,
                maximum=1,
            ),
            PANEL_H,
            HALF,
        ),
        sized(
            ts(
                "Detail-cache size",
                "How many entities each cache is holding. Read beside "
                "coverage: entries flat while coverage falls means the entity "
                "list grew and the cache has not caught up yet, which is "
                "normal after a bulk import and a problem if it persists.",
                [query(metric("ise3_detail_cache_entries"), "{{cache}}")],
            ),
            PANEL_H,
            HALF,
        ),
        sized(
            tbl(
                "Bounded breakdown truncation",
                "Breakdowns where the exporter published fewer groups than ISE "
                "reported, and by how much. Truncation is a deliberate cost "
                "control, but it must never be silent: this table is where the "
                "cost of that bound is stated.",
                [
                    instant(metric("ise3_topk_groups_total"), ref="A"),
                    instant(metric("ise3_topk_groups_returned"), ref="B"),
                    instant(metric("ise3_topk_truncated"), ref="C"),
                ],
                columns=["Groups in ISE", "Published", "Truncated"],
                sort=("Groups in ISE", True),
                labels={"dataset": "Dataset", "breakdown": "Breakdown"},
                column_overrides=[
                    by_column("Truncated", mappings=TRUNCATED,
                              thresholds=NONZERO_WARNING, colour_cells=True,
                              width=120),
                ],
            ),
            TALL_H,
            HALF,
        ),
        sized(
            ts(
                "Scheduler lanes",
                "Per-target queue depth and whether the lane is busy. A queue "
                "that does not drain means the exporter is asking for more "
                "than the appliance will answer in the time available, and "
                "freshness will slip next.",
                [
                    query(metric("ise3_lane_queue_depth"), "{{target}} queued"),
                    query(metric("ise3_lane_busy"), "{{target}} busy", ref="B"),
                ],
            ),
            PANEL_H,
            HALF,
        ),
        sized(
            ts(
                "Detail fetch outcomes",
                "Per-entity detail fetches by result, with how many were "
                "deferred. Deferrals are the budget working as designed; "
                "failures are not, and they are what stops a cache reaching "
                "the coverage floor.",
                [
                    query(f"sum by (cache,result) (rate("
                          f"{metric('ise3_detail_fetches_total')}[$__rate_interval]))",
                          "{{cache}} · {{result}}"),
                    query(metric("ise3_detail_fetches_deferred"),
                          "{{cache}} deferred", ref="B"),
                ],
                series_colours=OUTCOME_COLOURS,
            ),
            PANEL_H,
            HALF,
        ),
    ]

    completeness = [
        sized(
            tbl(
                "Populated breakdown dimensions",
                "Which optional breakdown dimensions the appliance's schema "
                "actually populates. A dimension marked unpopulated is one "
                "whose panel elsewhere in the set will be empty by design — a "
                "capability limit stated plainly rather than a graph that "
                "silently never fills.",
                [instant(metric("ise3_breakdown_dimension_populated"))],
                columns=["Populated"],
                sort=("Populated", False),
                labels={"dataset": "Dataset", "dimension": "Dimension"},
                column_overrides=[by_column("Populated", **BOOLEAN_CELL)],
            ),
            PANEL_H,
            HALF,
        ),
        sized(
            tbl(
                "NAD directory attribution",
                "How many network devices the directory holds, how many "
                "activity records it could attribute to one, and how old the "
                "activity source is. Unattributed activity is why some "
                "per-device panels have an 'unknown' row.",
                [
                    instant(metric("ise3_nad_directory_entries"), ref="A"),
                    instant(metric("ise3_nad_directory_attributed"), ref="B"),
                    instant(gate(metric("ise3_nad_activity_source_age_seconds"),
                                 "nad_health"), ref="C"),
                ],
                columns=["Directory entries", "Attributed", "Source age"],
                labels={"matched": "Matched"},
                column_overrides=[
                    by_column("Source age", unit="s",
                              thresholds=COLLECTION_AGE, colour_cells=True),
                ],
            ),
            PANEL_H,
            HALF,
        ),
        sized(
            stat_panel(
                "Latency measurement coverage",
                "Share of authentications that carried a usable latency "
                "measurement, and how many samples the reconstructed latency "
                "figures are drawn from. Every latency panel in the set is "
                "only as trustworthy as these two numbers.",
                [
                    instant(f"avg("
                            f"{gate(metric('ise3_radius_authentication_latency_coverage'), 'radius_reporting')})",
                            "coverage"),
                    instant(gate(metric("ise3_session_authentication_latency_samples"),
                                 "session_authorization"), "samples", ref="B"),
                    instant(f"sum(increase("
                            f"{metric('ise3_radius_latency_samples_total')}[$__range]))",
                            "reporting samples", ref="C"),
                ],
                unit="percentunit",
                thresholds=COVERAGE,
                overrides=[
                    by_ref("B", unit="short", thresholds=NEUTRAL),
                    by_ref("C", unit="short", thresholds=NEUTRAL),
                ],
                no_value=NO_DATA_STALE,
            ),
            STAT_H,
            HALF,
        ),
        sized(
            stat_panel(
                "Attribute and classification coverage",
                "How many endpoints the live attribute feed published, how "
                "many internal TACACS accounts could be classified, and how "
                "much state the exporter is holding on disk. Low publication "
                "or classification means the corresponding breakdowns are "
                "drawn from a subset.",
                [
                    instant(gate(metric("ise3_endpoint_attributes_published"),
                                 "endpoint_attributes"), "attributes published"),
                    instant(gate(metric("ise3_tacacs_internal_accounts_classified"),
                                 "tacacs_config"), "accounts classified", ref="B"),
                    instant(metric("ise3_exporter_state_bytes"), "state on disk",
                            ref="C"),
                ],
                thresholds=NEUTRAL,
                overrides=[by_ref("C", unit="bytes")],
                no_value=NO_DATA_EXPORTER,
            ),
            STAT_H,
            HALF,
        ),
        sized(
            ts(
                "Characters replaced in Data Connect results",
                "Rate at which the exporter had to substitute characters the "
                "appliance returned in an encoding it declared but did not "
                "honour. Non-zero is not fatal, but it means some label values "
                "on the reporting dashboards are approximations of what ISE "
                "actually holds.",
                [query(f"sum by (view) (rate("
                       f"{metric('ise3_dataconnect_replaced_characters_total')}[$__rate_interval]))",
                       "{{view}}")],
            ),
            PANEL_H,
            FULL,
        ),
    ]

    closing = [sized(about(
        "answer one question — is the exporter's view of ISE current enough to "
        "believe? Everything here is about the exporter, not about ISE.",
        [
            "Read the trust strip and the trustworthiness timeline.",
            "For anything not trustworthy, read its row in **Dataset status** "
            "and then **Latest failure**.",
            "If a dataset is on a fallback provider, read why in the provider "
            "table before treating the data as wrong.",
        ],
        [
            "Schema gaps: the appliance does not expose what a panel needs — "
            "that is a capability limit, not a bug.",
            "Queue depth not draining or API latency rising: the exporter is "
            "asking for more than ISE will answer — see the Load dashboard.",
            "Cache coverage low: dependent panels are suppressed on purpose "
            "and will return on their own.",
        ],
        ["Triage", "Exporter load", "Control plane"],
    ), STRIP_H, FULL)]

    return assemble(
        "ISE Exporter · Collection pipeline",
        "ise3-pipeline",
        "Tier 3. Is the exporter's view of ISE current, and which source is "
        "serving each dataset? Read this before concluding that anything on "
        "the ISE dashboards is real.",
        [
            ("Is the data current?", trust),
            ("Per dataset", detail),
            ("Transports", transports, COLLAPSED),
            ("Bounds and caches", bounds, COLLAPSED),
            ("Measurement completeness", completeness, COLLAPSED),
            ("Reference", closing, COLLAPSED),
        ],
        tier=EXPORTER,
        variables=(
            label_variable("dataset", "Dataset", "ise3_dataset_enabled", "dataset"),
        ),
    )


# ---------------------------------------------------------------------------
# Tier 3 — load against ISE
# ---------------------------------------------------------------------------

def load_dashboard():
    budget = [
        sized(
            stat_panel(
                "Budget used",
                "Share of the configured request budget the exporter is "
                "actually consuming. Above 80% there is no headroom for a "
                "backfill or a manual query; at 100% the scheduler is "
                "throttling and freshness starts to slip.",
                [instant(f"max({metric('ise3_load_budget_utilisation')})")],
                unit="percentunit",
                thresholds=BUDGET_USED,
                minimum=0,
                sparkline=True,
                no_value=NO_DATA_EXPORTER,
            ),
            STAT_H,
            SIXTH,
        ),
        sized(
            stat_panel(
                "Measured requests per hour",
                "What the exporter is really asking ISE for, derived from the "
                "request counter rather than from the plan. The number to "
                "quote when someone asks what this exporter costs.",
                [instant(f"sum(rate("
                         f"{metric('ise3_load_measured_requests_total')}[1h]) * 3600)")],
                thresholds=NEUTRAL,
                sparkline=True,
                no_value=NO_DATA_EXPORTER,
            ),
            STAT_H,
            SIXTH,
        ),
        sized(
            stat_panel(
                "Declared requests per hour",
                "What the configuration says the exporter will cost. Read it "
                "against the measured figure beside it: a declaration that "
                "does not match reality is the one thing in this design that "
                "can quietly lie.",
                [instant(summed(metric("ise3_load_planned_requests_per_hour")))],
                thresholds=NEUTRAL,
                no_value=NO_DATA_EXPORTER,
            ),
            STAT_H,
            SIXTH,
        ),
        sized(
            stat_panel(
                "Data Connect duty cycle",
                "Share of wall-clock time the exporter's Oracle session is "
                "actually executing. This is the figure a database "
                "administrator will care about, and the one the appliance "
                "enforces against.",
                [instant(metric("ise3_dataconnect_effective_duty_cycle_percent"))],
                unit="percent",
                thresholds=UTILISATION,
                minimum=0,
                sparkline=True,
                no_value=NO_DATA_EXPORTER,
            ),
            STAT_H,
            SIXTH,
        ),
        sized(
            stat_panel(
                "Throttle wait",
                "Cumulative seconds the scheduler has spent waiting for budget "
                "rather than querying. Non-zero means the budget is binding: "
                "the exporter wanted to collect and was not allowed to.",
                [instant(f"sum(rate("
                         f"{metric('ise3_budget_wait_seconds_total')}[$__rate_interval]))")],
                unit="s",
                thresholds=NONZERO_WARNING,
                no_value=NO_DATA_EXPORTER,
            ),
            STAT_H,
            SIXTH,
        ),
        sized(
            stat_panel(
                "Budget warming",
                "Whether the budget accounting is still filling its first "
                "window. While this is true the utilisation figures beside it "
                "are provisional rather than wrong.",
                [instant(f"max({metric('ise3_budget_warming')})")],
                mappings=YES_NO,
                thresholds=NEUTRAL,
                text_mode=BigValueTextMode.VALUE,
                no_value=NO_DATA_EXPORTER,
            ),
            STAT_H,
            SIXTH,
        ),
    ]

    declared = [
        sized(
            ts(
                "Requests per hour: declared, measured, and enforced",
                "The plan against reality against the ceiling. Measured "
                "exceeding declared means a cost declaration is wrong; "
                "measured touching enforced means the scheduler is being held "
                "back and collection intervals are effectively longer than "
                "configured.",
                [
                    query(summed(metric("ise3_load_planned_requests_per_hour")),
                          "declared"),
                    query(f"sum(rate("
                          f"{metric('ise3_load_measured_requests_total')}[1h]) * 3600)",
                          "measured", ref="B"),
                    query(summed(metric("ise3_budget_enforced_requests_per_hour")),
                          "enforced ceiling", ref="C"),
                    query(summed(metric("ise3_load_budget_requests_per_hour")),
                          "configured budget", ref="D"),
                ],
            ),
            PANEL_H,
            HALF,
        ),
        sized(
            ts(
                "Duty cycle: declared, measured, and allowed",
                "Planned Oracle duty cycle against what the session actually "
                "used, against the ceiling the budget allows. The Data Connect "
                "equivalent of the request comparison beside it, and the one an "
                "appliance will enforce hardest.",
                [
                    query(summed(metric("ise3_load_planned_duty_cycle_percent")),
                          "declared"),
                    query(metric("ise3_dataconnect_effective_duty_cycle_percent"),
                          "measured", ref="B"),
                    query(summed(metric("ise3_load_budget_duty_cycle_percent")),
                          "allowed", ref="C"),
                ],
                unit="percent",
                thresholds=UTILISATION,
            ),
            PANEL_H,
            HALF,
        ),
    ]

    where = [
        sized(
            tbl(
                "Statement cost by view",
                "Duration and row count of the last query against each "
                "reporting view, most expensive first. The list of candidates "
                "whenever the duty cycle needs to come down: one view usually "
                "dominates.",
                [
                    instant(f"max by (view) ("
                            f"{metric('ise3_dataconnect_query_last_duration_seconds')})",
                            ref="A"),
                    instant(metric("ise3_dataconnect_query_rows"), ref="B"),
                    instant(metric("ise3_dataconnect_query_cooldown_seconds"),
                            ref="C"),
                ],
                columns=["Last duration", "Rows", "Cooldown"],
                sort=("Last duration", True),
                labels={"view": "View"},
                column_overrides=[
                    by_column("Last duration", unit="s", thresholds=LATENCY_SECONDS,
                              colour_cells=True),
                    by_column("Cooldown", **SECONDS_CELL),
                ],
            ),
            TALL_H,
            HALF,
        ),
        sized(
            tbl(
                "Collection cost by dataset",
                "How long each dataset takes to collect and how often it runs "
                "— duration over interval is the share of the schedule that "
                "dataset consumes. The cheapest saving is usually a long "
                "dataset on a short interval.",
                [
                    instant(f"max by (dataset) ("
                            f"{metric('ise3_dataset_collection_duration_seconds')})",
                            ref="A"),
                    instant(metric("ise3_dataset_interval_seconds"), ref="B"),
                    instant(metric("ise3_dataset_series"), ref="C"),
                ],
                columns=["Duration", "Interval", "Series published"],
                sort=("Duration", True),
                labels={"dataset": "Dataset"},
                column_overrides=[
                    by_column("Duration", **SECONDS_CELL),
                    by_column("Interval", **SECONDS_CELL),
                ],
            ),
            TALL_H,
            HALF,
        ),
        sized(
            ts(
                "Database time consumed",
                "Oracle seconds per second the exporter is consuming, against "
                "the hourly figure the configuration declared. A step in the "
                "measured line without a configuration change means a view's "
                "data volume grew.",
                [
                    query(f"sum by (target) (rate("
                          f"{metric('ise3_load_measured_db_seconds_total')}[$__rate_interval]))",
                          "measured {{target}}"),
                    query(f"sum("
                          f"{metric('ise3_load_planned_db_seconds_per_hour')}) / 3600",
                          "declared", ref="B"),
                ],
                filled=True,
            ),
            PANEL_H,
            HALF,
        ),
        sized(
            ts(
                "Statement duration, 95th percentile",
                "The distribution behind the last-duration column beside it. A "
                "p95 well above the typical last duration means one statement "
                "occasionally runs far longer than it usually does, which is "
                "what actually breaches a duty cycle.",
                [query("histogram_quantile(0.95, sum by (view,le) (rate("
                       f"{metric('ise3_dataconnect_query_duration_seconds_bucket')}"
                       "[$__rate_interval])))", "{{view}}")],
                unit="s",
                legend_placement="right",
            ),
            PANEL_H,
            HALF,
        ),
        sized(
            ts(
                "Throttling, cooldown, and ad-hoc queries",
                "Requests the budget refused, the cooldown the exporter "
                "imposed on reporting queries in response, and any explorer "
                "queries an operator ran by hand. The last one matters: ad-hoc "
                "reads come out of the same budget as scheduled collection.",
                [
                    query(f"sum(rate("
                          f"{metric('ise3_budget_throttled_total')}[$__rate_interval]))",
                          "throttled"),
                    query(f"max("
                          f"{metric('ise3_dataconnect_query_cooldown_seconds')})",
                          "peak cooldown", ref="B"),
                    query(f"sum by (result) (rate("
                          f"{metric('ise3_dataconnect_explorer_queries_total')}[$__rate_interval]))",
                          "ad-hoc {{result}}", ref="C"),
                ],
            ),
            PANEL_H,
            HALF,
        ),
    ]

    closing = [sized(about(
        "hold the exporter's declared cost against its measured cost, so a "
        "cost declaration cannot quietly become fiction.",
        [
            "Read budget used and the declared-against-measured graph.",
            "If measured exceeds declared, the configuration is under-stating "
            "cost — fix the declaration or the interval.",
            "If measured is pinned to the enforced ceiling, collection "
            "intervals are effectively longer than configured.",
        ],
        [
            "Duty cycle high: find the dominant view in **Statement cost** and "
            "lengthen its scan window or its interval.",
            "Throttling non-zero: the exporter is already holding itself back; "
            "freshness on the pipeline dashboard will show the cost.",
        ],
        ["Collection pipeline", "Triage", "Capacity"],
    ), STRIP_H, FULL)]

    return assemble(
        "ISE Exporter · Load against ISE",
        "ise3-load",
        "Tier 3. What is this exporter costing the appliance, and does the "
        "measured cost match the declared cost? The panel set that catches a "
        "cost declaration drifting away from reality.",
        [
            ("Budget", budget),
            ("Declared against measured", declared),
            ("Where the cost goes", where),
            ("Reference", closing, COLLAPSED),
        ],
        tier=COST,
    )


# ---------------------------------------------------------------------------
# Tier 4 — capacity and growth
# ---------------------------------------------------------------------------

def capacity_dashboard():
    licensing = [
        sized(
            stat_panel(
                "Licence tiers out of compliance",
                "Licence tiers ISE currently reports as non-compliant. Any "
                "value above zero is a procurement conversation with a "
                "deadline attached, not an operational one.",
                [instant(f"count"
                         f"(({gate(metric('ise3_license_compliant'), 'licensing')}) == 0)")],
                thresholds=NONZERO_CRITICAL,
                no_value=NO_DATA_CLEAN,
            ),
            STAT_H,
            QUARTER,
        ),
        sized(
            stat_panel(
                "Endpoints",
                "Total endpoint inventory over the trend window. The primary "
                "driver of licence consumption, so its growth rate is the "
                "input to any capacity forecast.",
                [instant(gate(metric("ise3_endpoints_total"), "endpoint_inventory"))],
                thresholds=NEUTRAL,
                sparkline=True,
                no_value=NO_DATA_STALE,
            ),
            STAT_H,
            QUARTER,
        ),
        sized(
            stat_panel(
                "Peak concurrent sessions",
                "The highest concurrent session count seen across the trend "
                "window. Sizing is done against the peak, not the average, so "
                "this is the number that matters for node capacity.",
                [instant(f"max_over_time(({ACTIVE_SESSIONS})[$__range:])")],
                thresholds=NEUTRAL,
                no_value=NO_DATA_STALE,
            ),
            STAT_H,
            QUARTER,
        ),
    ] + trust_pair(("licensing", "endpoint_inventory", "certificates"),
                   span=EIGHTH) + [
        sized(
            ts(
                "Licence consumption by tier",
                "Consumption against each licence tier over the trend window. "
                "Read the gradient, not the value: the question this panel "
                "answers is when a tier runs out, not whether it has.",
                [query(gate(metric("ise3_license_consumption"), "licensing"),
                       "{{tier}}")],
                filled=True,
            ),
            PANEL_H,
            HALF,
        ),
        sized(
            tbl(
                "Licence state by tier",
                "Whether each tier is enabled, compliant, and what state ISE "
                "reports for it. The reference behind the consumption graph "
                "beside it.",
                [
                    instant(gate(metric("ise3_license_enabled"), "licensing"),
                            ref="A"),
                    instant(gate(metric("ise3_license_compliant"), "licensing"),
                            ref="B"),
                    instant(gate(metric("ise3_license_consumption"), "licensing"),
                            ref="C"),
                    instant(f"{gate(metric('ise3_license_compliance_state'), 'licensing')} == 1",
                            ref="D"),
                ],
                columns=["Enabled", "Compliant", "Consumption", None],
                sort=("Consumption", True),
                labels={"tier": "Tier", "state": "ISE state"},
                column_overrides=[
                    by_column("Enabled", **BOOLEAN_CELL),
                    by_column("Compliant", **BOOLEAN_CELL),
                ],
            ),
            PANEL_H,
            HALF,
        ),
    ]

    growth = [
        sized(
            ts(
                "Endpoint and device growth",
                "Inventory over the trend window. A steady gradient is "
                "planning input; a step is an import or a purge and should be "
                "explained before it is extrapolated from.",
                [
                    query(gate(metric("ise3_endpoints_total"), "endpoint_inventory"),
                          "endpoints"),
                    query(gate(metric("ise3_network_devices_total"),
                               "network_devices"), "network devices", ref="B"),
                ],
            ),
            PANEL_H,
            THIRD,
        ),
        sized(
            ts(
                "Concurrent sessions",
                "Live session count over the trend window. The peaks are what "
                "PSN sizing is done against; the troughs say how much of the "
                "estate is genuinely idle overnight.",
                [query(ACTIVE_SESSIONS, "sessions")],
                filled=True,
            ),
            PANEL_H,
            THIRD,
        ),
        sized(
            ts(
                "Node saturation trend",
                "Peak CPU per node across the trend window. This is the panel "
                "that says whether a node is trending towards needing help, as "
                "opposed to the PSN dashboard, which says whether it needs help "
                "right now.",
                [query("max by (node) (" +
                       gate(metric("ise3_node_cpu_utilization_percent"),
                            "psn_performance") + ")", "{{node}}")],
                unit="percent",
                thresholds=UTILISATION,
                minimum=0,
                maximum=100,
            ),
            PANEL_H,
            THIRD,
        ),
    ]

    runway = [
        sized(
            tbl(
                "Certificate runway",
                "Every certificate by days remaining, soonest first. Read on a "
                "monthly cadence rather than during an incident: everything on "
                "this list is avoidable with notice and unavoidable without "
                "it.",
                [instant(gate(metric("ise3_certificate_expiry_days"),
                              "certificates"))],
                columns=["Days left"],
                sort=("Days left", False),
                labels={"node": "Node", "certificate": "Certificate",
                        "store": "Store", "usage": "Usage"},
                column_overrides=[
                    by_column("Days left", thresholds=CERT_RUNWAY_DAYS,
                              colour_cells=True, width=110),
                ],
            ),
            TALL_H,
            HALF,
        ),
        sized(
            tbl(
                "Software currency",
                "Installed patches, the deployment's patch level, and whether "
                "the running release is still supported. Falling behind here "
                "is what eventually makes the reporting schema drift away from "
                "what this exporter expects.",
                [
                    instant(gate(metric("ise3_patch_installed"), "patches"),
                            ref="A"),
                    instant(gate(metric("ise3_version_supported"), "patches"),
                            ref="B"),
                ],
                columns=["Installed", "Release supported"],
                labels={"patch_number": "Patch", "version": "Release"},
                column_overrides=[
                    by_column("Installed", **BOOLEAN_CELL),
                    by_column("Release supported", mappings=SUPPORTED,
                              thresholds=REQUIRED_BOOLEAN, colour_cells=True),
                ],
            ),
            TALL_H,
            HALF,
        ),
    ]

    headroom = [
        sized(
            ts(
                "Exporter budget headroom",
                "Budget utilisation over the trend window. Growth in the "
                "estate shows up here as a slow climb long before it shows up "
                "as stale data, which makes this the earliest capacity signal "
                "the exporter produces.",
                [query(metric("ise3_load_budget_utilisation"), "{{target}}")],
                unit="percentunit",
                thresholds=BUDGET_USED,
                minimum=0,
            ),
            PANEL_H,
            HALF,
        ),
        sized(
            ts(
                "Collection duration trend",
                "How long each dataset takes to collect, over the trend "
                "window. A duration growing with the estate is the signal to "
                "lengthen an interval or narrow a scan window before freshness "
                "starts failing.",
                [query(f"max by (dataset) ("
                       f"{metric('ise3_dataset_collection_duration_seconds')})",
                       "{{dataset}}")],
                unit="s",
                legend_placement="right",
            ),
            PANEL_H,
            HALF,
        ),
    ]

    closing = [sized(about(
        "answer where this deployment runs out — of licence, of node capacity, "
        "of certificate validity, of exporter budget. Read on a monthly "
        "cadence, never during an incident.",
        [
            "Read licence consumption gradients, not values.",
            "Read the certificate runway top-down and diary the first few "
            "rows.",
            "Read node saturation and exporter budget together — they grow "
            "with the same estate.",
        ],
        [
            "A step rather than a gradient is an event, not growth. Check the "
            "change annotations before extrapolating.",
            "Everything here is a planning signal. Nothing here should page "
            "anyone.",
        ],
        ["Triage", "Control plane", "Exporter load"],
    ), STRIP_H, FULL)]

    return assemble(
        "ISE · Capacity and growth",
        "ise3-capacity",
        "Tier 4. Where does this deployment run out? Thirty-day trends for "
        "licence, fleet growth, node saturation, certificate runway and "
        "exporter budget. A planning dashboard, not an operational one.",
        [
            ("Licence", licensing),
            ("Fleet growth", growth),
            ("Runway", runway),
            ("Exporter headroom", headroom),
            ("Reference", closing, COLLAPSED),
        ],
        tier=CAPACITY,
    )


# ---------------------------------------------------------------------------

DASHBOARDS = {
    "ise3-triage": triage_dashboard,
    "ise3-access": access_dashboard,
    "ise3-psn": psn_dashboard,
    "ise3-control": control_dashboard,
    "ise3-endpoints": endpoints_dashboard,
    "ise3-nad": nad_dashboard,
    "ise3-posture": posture_dashboard,
    "ise3-tacacs": tacacs_dashboard,
    "ise3-pipeline": pipeline_dashboard,
    "ise3-load": load_dashboard,
    "ise3-capacity": capacity_dashboard,
}

# The tier each dashboard belongs to, asserted by the test suite so the
# structure cannot quietly collapse back into one undifferentiated pile.
TIERS = {
    "triage": ("ise3-triage",),
    "diagnostic": ("ise3-access", "ise3-psn", "ise3-control", "ise3-endpoints",
                   "ise3-nad", "ise3-posture", "ise3-tacacs"),
    "exporter": ("ise3-pipeline", "ise3-load"),
    "capacity": ("ise3-capacity",),
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
