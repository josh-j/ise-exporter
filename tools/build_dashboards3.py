"""Generate the ise-exporter3 Grafana dashboards with the Grafana Foundation SDK.

Dashboards are built as code rather than hand-edited JSON. v2's were 60-90 KB of
JSON per file, which meant a panel's query could drift from the metric registry
and only a test would notice. Here the queries are written once, in Python, next
to the constants that name the metrics.

Two dashboards, matching the two questions v3 exists to answer:

    sources  which source is supplying each dataset, and when did that change
    load     what is this exporter costing each ISE persona, against its budget

Run:  python tools/build_dashboards3.py --out dashboards3
Needs the SDK:  pip install 'grafana-foundation-sdk'
"""
from __future__ import annotations

import argparse
import pathlib
import sys

from grafana_foundation_sdk.builders import common, dashboard as db
from grafana_foundation_sdk.builders import prometheus, stat, table, timeseries
from grafana_foundation_sdk.cog.encoder import JSONEncoder
from grafana_foundation_sdk.models.dashboard import (
    DashboardCursorSync,
    DataSourceRef,
)


DATASOURCE = DataSourceRef(type_val="prometheus", uid="${prometheus}")
TAGS = ["ise", "ise-exporter3"]


def query(expr, legend="", instant=False, ref="A"):
    built = (prometheus.Dataquery()
             .expr(expr)
             .legend_format(legend)
             .ref_id(ref))
    return built.instant() if instant else built.range()


def datasource_variable():
    """File-provisioned dashboards cannot hardcode a datasource uid."""
    return (db.DatasourceVariable("prometheus")
            .label("Prometheus")
            .type("prometheus"))


def base(title, uid, description):
    return (db.Dashboard(title)
            .uid(uid)
            .description(description)
            .tags(TAGS)
            .tooltip(DashboardCursorSync.CROSSHAIR)
            .refresh("1m")
            .time("now-6h", "now")
            .editable()
            .with_variable(datasource_variable()))


def ts(title, description, targets, *, unit="short", stacking=False):
    panel = (timeseries.Panel()
             .title(title)
             .description(description)
             .datasource(DATASOURCE)
             .unit(unit)
             .fill_opacity(10 if stacking else 0)
             .legend(common.VizLegendOptions()
                     .display_mode("table")
                     .placement("bottom")
                     .calcs(["lastNotNull", "max"])))
    for target in targets:
        panel = panel.with_target(target)
    return panel


def tbl(title, description, targets):
    panel = (table.Panel()
             .title(title)
             .description(description)
             .datasource(DATASOURCE))
    for target in targets:
        panel = panel.with_target(target)
    return panel


def stat_panel(title, description, targets, *, unit="short"):
    panel = (stat.Panel()
             .title(title)
             .description(description)
             .datasource(DATASOURCE)
             .unit(unit))
    for target in targets:
        panel = panel.with_target(target)
    return panel


def sources_dashboard():
    """Which source is answering, and when did that change."""
    return (
        base("ISE Exporter 3 — Sources",
             "ise3-sources",
             "Which source supplies each dataset, when it changed, and why. "
             "Sources differ in meaning, so a dataset running on a fallback may "
             "be answering a narrower question than its preferred source would.")
        .with_row(db.Row("Active source"))
        .with_panel(
            tbl("Live source per dataset",
                "The source currently supplying each dataset. A dataset with a "
                "row here is being collected; one that vanished has no usable "
                "source at all, which the Collection row explains.",
                [query("ise3_dataset_provider_active == 1",
                       "{{dataset}} / {{provider}}", instant=True)])
            .height(9).span(12))
        .with_panel(
            ts("Datasets not on their preferred source",
               "Non-zero means a dataset fell back. This is the signal v1 never "
               "had: it swapped sources silently, so a panel changed meaning "
               "with nothing to show for it.",
               [query("sum(ise3_dataset_provider_degraded)", "degraded"),
                query("ise3_dataset_provider_degraded == 1", "{{dataset}}")])
            .height(9).span(12))
        .with_row(db.Row("Why it changed"))
        .with_panel(
            tbl("Reason each fallback engaged",
                "The bounded reason recorded when a dataset stepped to another "
                "source, and the source it stepped to.",
                [query("ise3_dataset_provider_reason_info == 1",
                       "{{dataset}} {{provider}} {{reason}}", instant=True)])
            .height(8).span(12))
        .with_panel(
            tbl("Declared but unusable sources",
                "Sources an operator listed that this build cannot run: not "
                "implemented, target not configured, or a required reporting "
                "view the account cannot see.",
                [query("ise3_dataset_provider_available == 0",
                       "{{dataset}} / {{provider}}", instant=True)])
            .height(8).span(12))
        .with_row(db.Row("Collection"))
        .with_panel(
            tbl("Dataset health",
                "up is the last attempt; fresh means the snapshot is inside two "
                "cadences. A dataset that is up but not fresh is collecting more "
                "slowly than it claims.",
                [query("ise3_dataset_up", "{{dataset}} up", instant=True),
                 query("ise3_dataset_fresh", "{{dataset}} fresh",
                       instant=True, ref="B")])
            .height(9).span(12))
        .with_panel(
            tbl("Latest failure per dataset",
                "One bounded operator explanation per failing dataset. Empty is "
                "the healthy state.",
                [query("ise3_dataset_last_failure_detail_info == 1",
                       "{{dataset}} {{reason}}", instant=True)])
            .height(9).span(12))
    )


def load_dashboard():
    """What this exporter costs each ISE persona, against its declared budget."""
    return (
        base("ISE Exporter 3 — Load and budget",
             "ise3-load",
             "Planned load comes from each provider's declared cost; measured "
             "load is counted as requests actually leave. Drift between them "
             "means a cost declaration is wrong — the one defect this design "
             "cannot catch by construction.")
        .with_row(db.Row("Budget"))
        .with_panel(
            stat_panel("Budget used",
                       "Planned load as a fraction of the declared ceiling, per "
                       "target. Above 1 means the exporter refuses to start "
                       "unless enforce_budget is off.",
                       [query("ise3_load_budget_utilisation",
                              "{{target}}", instant=True)],
                       unit="percentunit")
            .height(5).span(24))
        .with_panel(
            ts("Requests per hour: planned against measured",
               "Two lines per target. They should track. A measured line above "
               "planned means a provider costs more than it declares; below "
               "means the exporter is being throttled short of its cadence.",
               [query("ise3_load_planned_requests_per_hour", "{{target}} planned"),
                query("rate(ise3_load_measured_requests_total[15m]) * 3600",
                      "{{target}} measured", ref="B")],
               unit="reqps")
            .height(9).span(12))
        .with_panel(
            ts("Request ceiling per target",
               "The declared budget. Zero means no ceiling was declared, which "
               "the exporter warns about at startup.",
               [query("ise3_load_budget_requests_per_hour", "{{target}} budget")])
            .height(9).span(12))
        .with_row(db.Row("Data Connect duty cycle"))
        .with_panel(
            ts("Duty cycle: planned against measured",
               "The share of wall-clock time spent inside an Oracle statement. "
               "This is the one budget expressed in time rather than requests, "
               "and the adaptive cooldown is what enforces it.",
               [query("ise3_load_planned_duty_cycle_percent{target='oracle'}",
                      "planned"),
                query("rate(ise3_load_measured_db_seconds_total{target='oracle'}[15m])"
                      " * 100", "measured", ref="B"),
                query("ise3_dataconnect_effective_duty_cycle_percent", "ceiling",
                      ref="C")],
               unit="percent")
            .height(9).span(12))
        .with_panel(
            ts("Cooldown imposed on all reporting datasets",
               "After each statement every Data Connect dataset waits this long. "
               "At a low duty cycle one slow statement can freeze reporting for "
               "far longer than any configured cadence — which is what made v2's "
               "cadences fictional at scale.",
               [query("ise3_dataconnect_query_cooldown_seconds", "{{view}}")],
               unit="s")
            .height(9).span(12))
        .with_row(db.Row("Statement cost"))
        .with_panel(
            ts("Statement duration by view",
               "What each reporting view actually costs Oracle. This is the "
               "input to the cooldown, so a view that slows down throttles every "
               "other reporting dataset with it.",
               [query("ise3_dataconnect_query_last_duration_seconds{result='success'}",
                      "{{view}}")],
               unit="s")
            .height(8).span(12))
        .with_panel(
            ts("Rows returned by view",
               "Aggregation happens in the database. A view trending toward the "
               "6000-row statement ceiling is returning rows, not aggregates.",
               [query("ise3_dataconnect_query_rows", "{{view}}")])
            .height(8).span(12))
        .with_row(db.Row("Scheduler"))
        .with_panel(
            ts("Lane queue depth",
               "Datasets waiting on a target's lane. Sustained depth means "
               "collection is slower than the configured cadence.",
               [query("ise3_lane_queue_depth", "{{target}}")])
            .height(8).span(12))
        .with_panel(
            ts("Collection duration by dataset",
               "How long each dataset takes, labelled by the source that "
               "produced it.",
               [query("ise3_dataset_collection_duration_seconds",
                      "{{dataset}} / {{provider}}")],
               unit="s")
            .height(8).span(12))
    )


DASHBOARDS = {
    "ise3-sources": sources_dashboard,
    "ise3-load": load_dashboard,
}


def build(output):
    output.mkdir(parents=True, exist_ok=True)
    encoder = JSONEncoder(sort_keys=True, indent=2)
    written = []
    for name, builder in DASHBOARDS.items():
        path = output / f"{name}.json"
        path.write_text(encoder.encode(builder().build()) + "\n")
        written.append(path)
    return written


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="dashboards3", type=pathlib.Path,
                        help="directory to write dashboard JSON into")
    args = parser.parse_args(argv)
    for path in build(args.out):
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
