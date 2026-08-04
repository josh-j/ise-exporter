# Dashboard Design Principles

Good dashboards are answers to questions, not displays of available metrics. The
failure mode in Grafana specifically is that instrumentation is cheap, so
dashboards accrete panels until nobody can tell what "normal" looks like.

## 1. Design around a decision, not a data source

Before adding a panel, name the question it answers and what you'd *do*
differently based on it. Panels that fail this test belong in an explore/ad-hoc
view, not a dashboard.

A useful discipline: write the dashboard's purpose in one sentence at the top
("Is the ISE export pipeline keeping up, and if not, where's the stall?").
Anything not serving that sentence goes elsewhere.

## 2. Separate dashboards by audience and time horizon

Three distinct kinds, and mixing them is the most common structural mistake:

- **Triage / "is it broken"** — a handful of panels, mostly symptom-level
  (user-visible), readable in under 10 seconds during an incident. Short time
  range.
- **Diagnostic / drill-down** — causes, subsystem breakdowns, high cardinality.
  Linked *from* the triage dashboard, not merged into it.
- **Trend / capacity** — weeks-to-months, for planning, not paging.

## 3. Use a method for the metric selection

Don't freestyle. The standard frameworks exist because they produce complete
coverage without sprawl:

- **RED** for request-driven services: Rate, Errors, Duration.
- **USE** for resources (CPU, disk, connection pools, queues): Utilization,
  Saturation, Errors.
- **Four Golden Signals** (latency, traffic, errors, saturation) as a general
  default.

Symptoms above causes, always. The top row should be what a user would notice;
the rows below explain it.

## 4. Layout follows reading order

- Most important panel top-left; scanning goes left-to-right, top-to-bottom.
  Nothing that matters lives below the fold.
- Group related panels into collapsible rows with real names ("Ingest",
  "Scheduler", "Downstream ISE API"), and collapse the deep-diagnostic rows by
  default.
- Keep the panel grid consistent — a few standard widths, aligned time axes.
  Ragged sizing makes everything look equally important, which means nothing is.
- Consistent time range across panels so eyes can correlate a spike vertically
  down the page. This is the single highest-value property of an operational
  dashboard.

## 5. Panel-level rules

- **One question per panel.** Ten series is usually the ceiling for a readable
  time series; beyond that, aggregate and put the breakdown in a drill-down.
- **Titles state the question or the claim**, not the metric name. "p99 export
  latency" beats `histogram_quantile(0.99, ...)`.
- **Always set units and decimals.** An unlabeled `1.4e7` is noise.
- **Percentiles, not averages,** for latency — and show more than one
  (p50/p90/p99) so you can see whether it's everyone or a tail.
- **Encode "good" visually**: thresholds, a shaded SLO band, or a reference
  line. A number is only meaningful against an expectation. If a panel has a
  threshold, it should be the *same* threshold the alert uses — dashboards that
  disagree with alerting destroy trust.
- **Show absence.** Null vs. zero matters enormously in monitoring; a panel that
  renders a flat zero line when scraping actually stopped is actively harmful.
  Configure "no data" explicitly.
- Stat/big-number panels need a trend or sparkline; a bare number has no
  context.

## 6. Color is signal, not decoration

- Reserve red/amber/green *exclusively* for state. If green means "healthy,"
  don't also use it as series #3 in an unrelated graph.
- Pin semantic series to fixed colors via overrides — `error` should be red on
  every dashboard, and it should not shuffle when a series drops out.
- Check it works in both light and dark themes, and don't rely on hue alone
  (~8% of men have some color vision deficiency). Position, label, or shape
  should carry the meaning too.

## 7. Chart-type discipline

- Time series for trends over time — the default.
- Bars for comparing categories; baseline at zero, mandatory.
- Heatmaps for distributions over time (latency histograms) — far better than
  stacking percentiles when you care about shape/multimodality.
- Tables for exact values and top-N lists.
- Avoid pie/donut for anything with more than ~3 slices, and avoid gauges
  without a threshold context.
- Be careful with stacking: it's right for parts-of-a-whole (CPU by mode) and
  misleading for independent series, since only the bottom band is readable.

## 8. Respect the query cost

Grafana-specific but real: dashboards run their queries on every load and
refresh. Wide time ranges × high-cardinality label breakdowns × a 10s
auto-refresh is a recipe for a dashboard that takes the monitoring system down
during the incident it was built for. Use recording rules for expensive
expressions, set sane default refresh intervals (30s–1m is plenty for most), and
keep default time ranges tight.

## 9. Build for navigation, not completeness

Data links and drill-downs from an overview panel to the detail dashboard
(carrying variables and time range through) beat one giant dashboard. Template
variables for environment/instance/tenant let one dashboard replace fifteen —
but give them sensible defaults, and avoid a "select everything" default that
fans out to thousands of series.

## 10. Treat dashboards as code with an owner

Provision from version control (JSON/Grafonnet/Terraform), review changes, and
delete aggressively. Add a text panel with the runbook link, the owning team,
and what to do when the panel goes red. An unowned dashboard rots into a
liability within about two quarters.

## Anti-patterns

- The wall of 40 panels nobody reads.
- Graphs with no threshold, so you can't tell good from bad.
- Duplicated near-identical dashboards with subtle drift.
- Panels that exist because the metric existed.
- Dashboards whose thresholds contradict the alerting rules.
