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

## Dashboard set

| Dashboard | Answers |
|---|---|
| `ise3-overview` | Is the deployment healthy, licensed, backed up, patched, and ready? |
| `ise3-access` | What is failing RADIUS access, where, at which NAD/method, and under which live authorization decision? |
| `ise3-endpoints` | What endpoints and NADs exist, how are they classified, and which devices are silent? |
| `ise3-health` | Which collector, source, transport, cache, bound, or scheduler lane is unhealthy? |
| `ise3-pan-mnt` | Are the administrative and monitoring nodes, services, certificates, authentication steps, and collectors healthy? |
| `ise3-psn` | Which PSN is carrying sessions, errors, diagnostics, load, latency, or resource pressure? |
| `ise3-secureclient` | What is the current and historical posture state by policy, client version, OS, and owner? |
| `ise3-tacacs` | Which Device Administration accounts, policies, rules, devices, owners, and commands need attention? |
| `ise3-sources` | Which source is supplying each dataset, when that changed, and why |
| `ise3-load` | What this exporter costs each ISE persona, against its declared budget |

The first eight preserve the complete operator-workflow surface from v2. They are
translated onto v3 metrics rather than copied query-for-query: v3 exposes current
provider semantics, explicit bounded-breakdown coverage, and converging cache
coverage, so panels use those contracts instead of v2's collector-specific
truncation and readiness metrics. `V2_WORKFLOW_PARITY` in the generator and the
dashboard contract tests prevent any of the eight workflows from disappearing.

The parity contract is capability-based, because several v2 collector-specific
signals have a more explicit v3 equivalent:

| v2 workflow | Capability retained in v3 |
|---|---|
| Overview | persona state and services, active sessions, endpoint/NAD inventory, certificates, backup, patch, and licence state |
| Access troubleshooting | RADIUS history and accounting, distinct endpoints, latency, failure class and work queue, NAD/method correlation with coverage, live authorization context, and owner/location routing |
| Endpoints and devices | profile and identity inventory, model/MDM fields, profile events, NAD activity/last-seen, assignment metadata, and converging detail coverage |
| Exporter health | provider readiness and failure detail, source age, runtime schema gaps, query/API cost, scheduler and budget pressure, bounded-breakdown coverage, cache convergence, posture eligibility backlog, and build identity |
| PAN/MnT troubleshooting | persona/service state, snapshot age, sessions/posture, authentication and step latency, diagnostics, resources, certificates, and cache health |
| PSN troubleshooting | sessions and deltas, RADIUS/accounting/error volume, throughput, latency, resources, diagnostics, source age, and required/optional schema compatibility |
| Secure Client | current and historical posture by status, policy, condition, PSN, client version, OS, and owner, plus source-field coverage and exact assessment eligibility |
| TACACS | internal-account hygiene, authentication/authorization/accounting activity, last-seen evidence, policy/profile/command-set detail, policy objects and rules, owner rollups, and explicit truncation/cache coverage |

Every dashboard is deployment-aware through the Prometheus `instance` label,
keeps the selected deployment while navigating through the shared dashboard
menu, and uses the same five-minute refresh as the underlying operational
datasets. Exporter health, source selection, and load refresh once per minute.

The final two are deliberately about the exporter rather than ISE, because those
are the questions v3 added and neither earlier version could answer.

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

## Adding a panel

Edit the generator, not the JSON. Every panel needs a description — the test
enforces it. v2's `ISSUES.md` is largely a list of panels whose meaning was
unclear to the person on call, which is a design failure rather than a
documentation one.
