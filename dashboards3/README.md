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

## Why these two

| Dashboard | Answers |
|---|---|
| `ise3-sources` | Which source is supplying each dataset, when that changed, and why |
| `ise3-load` | What this exporter costs each ISE persona, against its declared budget |

They are deliberately about the exporter rather than about ISE, because those are
the two questions v3 was built to answer and neither v1 nor v2 could.

**Sources.** v1 swapped between pxGrid, MnT and ERS silently, so a panel could
change meaning with nothing on screen to say so. v2 removed fallback entirely and
left blank panels instead. v3 keeps the fallback and exports every step:
`ise3_dataset_provider_active` says which source is live, `_degraded` says it is
not the preferred one, and `_reason_info` says why. Because sources differ in
meaning, `provider` is a label on the data itself — two sources for one dataset
never merge into one series.

**Load.** Planned load comes from each provider's declared cost; measured load is
counted as requests actually leave the process. The panel that puts them side by
side is the important one: cost declarations are hand-written, and they are the
one thing in this design that can quietly lie. Drift between the two lines is how
you find out.

## Provisioning

Both dashboards use a `${prometheus}` datasource variable rather than a hardcoded
uid, so they can be file-provisioned into any Grafana:

```yaml
apiVersion: 1
providers:
  - name: ise-exporter3
    type: file
    options:
      path: /var/lib/grafana/dashboards/ise-exporter3
```

## Adding a panel

Edit the generator, not the JSON. Every panel needs a description — the test
enforces it. v2's `ISSUES.md` is largely a list of panels whose meaning was
unclear to the person on call, which is a design failure rather than a
documentation one.
