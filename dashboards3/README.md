# ise-exporter3 dashboards

These are **generated**, not hand-edited. The source of truth is
[`tools/build_dashboards3.py`](../tools/build_dashboards3.py), which builds them
with the [Grafana Foundation SDK](https://grafana.github.io/grafana-foundation-sdk/).

```bash
pip install -e '.[dashboards]'
python tools/build_dashboards3.py --out dashboards3
```

`tests/test_v3_dashboards.py` fails if the committed JSON drifts from the
generator, and — more importantly — if a panel queries a metric or a label this
exporter does not publish. Dashboards are normally the least-verified artifact in
a monitoring stack: a query against a metric that no longer exists renders an
empty graph, not an error.

The set is organised by the principles in
[`DASHBOARD_DESIGN_PRINCIPLES.md`](../DASHBOARD_DESIGN_PRINCIPLES.md), and those
principles are asserted by the same test file rather than left to review — tier
time horizons, the visible-panel ceiling, the trust pair, the absence of silent
truncation, and the table-over-bar-gauge rule are all contract-tested.

## The set

Four tiers, by **audience and time horizon** rather than by data source.

### Tier 1 — triage

| Dashboard | Answers | Opens on |
|---|---|---|
| `ise3-triage` | Is ISE authenticating users right now? | 3h / 1m |

The only dashboard anyone should open first. Symptom-level only: success rate,
failures, sessions, node state, an attention table, and the three "where it
hurts" breakdowns. Every panel links down into the dashboard that explains it.

### Tier 2 — diagnostic

| Dashboard | Answers | Opens on |
|---|---|---|
| `ise3-access` | Why are RADIUS authentications failing, at which device and method, under which authorization decision? | 6h / 5m |
| `ise3-psn` | Are the policy service nodes coping, and which one is not? | 6h / 5m |
| `ise3-control` | Is the PAN/MnT control plane healthy — nodes, certificates, backup, and the MnT pipeline? | 6h / 5m |
| `ise3-endpoints` | What is on the network, how is it classified, and which devices are silent? | 6h / 5m |
| `ise3-posture` | Is the fleet compliant, and is that compliance figure trustworthy? | 6h / 5m |
| `ise3-tacacs` | Who is administering the network devices, and is the account estate clean? | 6h / 5m |

Each one leads with its own service level, then isolation, then the deep
material in collapsed rows. None shows more than sixteen panels on load.

### Tier 3 — the exporter, not ISE

| Dashboard | Answers | Opens on |
|---|---|---|
| `ise3-pipeline` | Is the exporter's view of ISE current, and which source is serving each dataset? | 6h / 1m |
| `ise3-load` | What is this exporter costing the appliance, against what it declared? | 24h / 5m |

An operator reading these is asking whether to believe the other eight.

### Tier 4 — capacity

| Dashboard | Answers | Opens on |
|---|---|---|
| `ise3-capacity` | Where does this deployment run out — licence, node capacity, certificate validity, exporter budget? | 30d / 30m |

A planning dashboard. Nothing on it should page anyone.

## What the design commits to

**Nothing renders ISE data without saying whether that data is current.**
`gate()` drops stale series rather than showing them as live, and every ISE
dashboard carries a **Data trustworthy** / **Collection age** pair at the end of
its first row — after the outcome the operator came for, before anything else.
`ise3-pipeline` is the long-form answer, and every trust panel links to it.

**A breakdown is a table, not a capped bar gauge.** A bar gauge cannot scroll,
so any complete per-device breakdown had to be truncated to stay readable. Tables
scroll, sort worst-first, and need no cap — so there is no cap, and no risk that
the device worth looking at fell off the bottom of the visual. Bar gauges survive
only where the data is bounded and low-cardinality and the bar length is
genuinely the message.

**Truncation, where it still exists, is stated.** The exporter does bound some
breakdowns for cost reasons. `ise3_topk_groups_total` against
`ise3_topk_groups_returned` is on the pipeline dashboard, so the cost of that
bound is on screen rather than implied.

**Colour means state.** Red, amber and green are reserved for it. Outcome series
(`passed`, `failed`, `compliant`, `error`) are pinned to fixed colours so they do
not shuffle when a neighbouring series drops out.

**Every dashboard says what it is for.** Each ends with an About panel carrying
its purpose, a reading order, what to do when a panel goes red, and a place to
put the owning team and the runbook link.

## Sources and load

**Sources.** v1 swapped between pxGrid, MnT and ERS silently, so a panel could
change meaning with nothing on screen to say so. v2 removed fallback entirely and
left blank panels instead. v3 keeps the fallback and exports every step:
`ise3_dataset_provider_active` says which source is live, `_degraded` says it is
not the preferred one, and `_reason_info` says why. Because sources differ in
meaning, `provider` is a label on the data itself — two sources for one dataset
never merge into one series. All of it is on `ise3-pipeline`.

**Load.** Planned load comes from each provider's declared cost; measured load is
counted as requests actually leave the process. The panel that puts them side by
side is the important one: cost declarations are hand-written, and they are the
one thing in this design that can quietly lie. Drift between the two lines is how
you find out.

## Provisioning

All dashboards use a `${prometheus}` datasource variable rather than a hardcoded
uid, so they can be file-provisioned into any Grafana:

```yaml
apiVersion: 1
providers:
  - name: ise-exporter3
    type: file
    options:
      path: /var/lib/grafana/dashboards/ise-exporter3
```

One exporter serves one ISE deployment, so no dashboard carries a deployment
variable: `instance` cannot vary within a set of panels fed by one exporter, and
a selector that filters nothing is a control that lies about being one. Point a
second deployment at a second scrape job and a second Grafana folder. Drill-down
links carry the time range and whatever entity was clicked, which is the whole
of the context there is to keep.

## Adding a panel

Edit the generator, not the JSON. Every panel needs a description — the test
enforces it. v2's `ISSUES.md` is largely a list of panels whose meaning was
unclear to the person on call, which is a design failure rather than a
documentation one.

Before adding one, check it against `CAPABILITY_CONTRACTS` in the test file and
against the visible-panel ceiling. If a dashboard is already at the ceiling, the
new panel belongs in a collapsed row or on a different dashboard — that ceiling
exists because the previous set reached 45 panels on one page and stopped being
readable.
