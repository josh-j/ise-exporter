# ise-exporter3

A Prometheus exporter for Cisco ISE that treats **source selection and load
budgeting as the same decision**. Each dataset declares which sources can supply
it, in preference order, and what one collection costs the ISE persona it talks
to. Pacing, cooldowns, row ceilings and query timeouts are derived from a
declared budget and scale plus those costs rather than chosen, the budget is
**enforced** rather than merely checked, and what the exporter actually spent is
exported beside what it planned to spend.

This is the v3 build, split out of the v2 repository. v2 remains deployed and is
maintained separately; nothing here is a drop-in replacement for it yet (see
**Status**).

## Status

The architecture is complete end to end: `plan` reports cost without
credentials, `run` collects and serves, sources fail over visibly, the operator
API answers from live state, and dashboards are generated and contract-tested.

`ROADMAP.md` carries the work this build needs before it can replace v2, with
the measurements behind each item. Every item in it is closed.

It has run against a live appliance — `laba-ise-001`, ISE 3.3 Patch 11 — with
all 18 datasets collecting, no `schema_incompatible`, and every view and column
the generated SQL names present on the real catalogue. Two things remain
deliberately not done:

- **Aggregate values are unverified.** A statement that runs and returns
  plausible numbers can still measure the wrong thing, and neither the lab nor
  the simulator settles that: the lab has almost no RADIUS or TACACS event
  volume, and the simulator's appliance is synthetic. Behaviour at 5,000 NADs
  rests on the simulator alone.
- **pxGrid is not ported.** 18 of 19 datasets collect; `endpoint_attributes`
  (model, OS, MDM) is pxGrid-only by design and is unavailable until the v1
  streaming client is ported. Datasets that can fall back do so visibly, and
  `plan` prints `fell back from pxgrid`.

## Quick start

```bash
nix develop                                   # or: pip install -e '.[dev]'

# What will this configuration cost each ISE persona? No credentials, no network.
python -m ise_exporter3 plan --config ise-exporter3.toml.example

# Collect and serve.
export ISE_PASS=...                           # PAN/MnT read-only account
export ISE_DATACONNECT_PASSWORD=...           # the fixed `dataconnect` account
python -m ise_exporter3 run --config /etc/ise-exporter3/config.toml
```

`plan` exits non-zero when the configuration is over budget, which makes it
usable as a pre-deploy gate. Passwords are never written to the config file.

## Configuration

Six sections, roughly thirty keys — see `ise-exporter3.toml.example`, which is
commented and is what CI plans against.

| Section | What it decides |
|---|---|
| `profile` | `production` (~100k endpoints / 5k NADs) or `lab`; everything below overrides it |
| `[scale]` | how many NADs / endpoints / sessions / Device Admin accounts and policy sets, so the plan is predictive for your estate |
| `[targets]` | which ISE personas are reachable, and as whom |
| `[budget]` | the ceiling per target — requests/hour for REST, duty cycle for Data Connect |
| `[limits]` | what one statement, batch and snapshot may return; derived from `[scale]`, shown so it can be read |
| `[datasets]` | per dataset: `enabled`, `providers` (an ordered preference list), `interval`, and `options` — any bound a dataset puts on its own breakdowns |

Configuration selects sources and cadences. It does not tune load — that is
`[budget]` — but every ceiling and every bound is **visible** in it. A ceiling
nobody can read is not a contract: three of them once disagreed as constants in
three modules, and `tacacs_activity` failed every collection at the declared
scale as a result. `plan` prints all of them with where each value came from,
and a value that is legal but unwise warns at start.

## What it exports

| Plane | Datasets |
|---|---|
| PAN (ERS / OpenAPI) | `deployment`, `network_devices`, `certificates`, `licensing`, `backup`, `patches`, `tacacs_config`, `tacacs_policy_rules` |
| MnT | `active_sessions`, `session_authorization`, `posture_current` |
| Data Connect | `endpoint_inventory`, `profile_events`, `psn_performance`, `radius_reporting`, `radius_accounting`, `radius_errors`, `posture_history`, `nad_health`, `tacacs_activity`, `source_freshness` |
| pxGrid | `endpoint_attributes` (declared, not yet built) |

Metrics are prefixed `ise3_`. `provider` is a label on the data itself, because
two sources for one dataset rarely mean exactly the same thing — a dashboard can
gate on it, and when a dataset changes source the previous source's series are
dropped rather than merged.

Every provider also declares how much of the fleet it measures — `complete`,
`converging` or `bounded` — and a bounded one must say what it leaves out.

## Operator surface

`/metrics` on port 9618 for Prometheus — gzipped when the client offers it,
which it always does, taking a ~6.4 MiB scrape to well under a megabyte on the
wire — and a read-only operator API bound to localhost on 9619 that answers from
state the exporter already computed and never reaches ISE:

```
/api/v1/health   /api/v1/datasets   /api/v1/providers
/api/v1/targets  /api/v1/plan       /api/v1/plan.txt
```

`powershell/Ise.Cli3/` wraps those routes as cmdlets (`Get-IseHealth`,
`Get-IseDataset`, `Get-IseProvider`, `Get-IseTarget`, `Get-IsePlan`,
`Get-IseDegraded`); `powershell/ise-cli3` and `Ise.Cli3.Profile.ps1` are the
shell entry points.

## Dashboards

`dashboards3/*.json` are **generated**, not hand-edited:

```bash
pip install -e '.[dashboards]'
python tools/build_dashboards3.py --out dashboards3
```

The generated set includes the eight v2 operator workflows—overview, access,
endpoints and NADs, exporter health, PAN/MnT, PSN, Secure Client/posture, and
TACACS—plus v3's provider-source and declared-versus-measured-load dashboards.
Every workflow is deployment-aware and contract-tested against the metric
registry; see `dashboards3/README.md` for the capability map.

## Simulating production scale

`tools/simulate_scale3.py` runs the real scheduler, transports and datasets
against a synthetic ISE (`tools/fake_ise3.py`) sized to the declared production
scale, on a virtual clock, and reports cost per target against the declared
budget, series and scrape size, cache convergence, Data Connect duty cycle and
anything that hit a ceiling.

```bash
python tools/simulate_scale3.py --hours 24
python tools/simulate_scale3.py --hours 12 --sessions 40000 --latency-scale 5
```

PAN and MnT are answered over real HTTPS so the shipped paging, XML parsing and
response ceilings run unmodified; Data Connect uses a synthetic cursor beneath
the real transport, so the pacing gate, duty cycle, batch lease and row/byte
ceilings are the shipped code. It measures cost, cardinality, convergence and
pacing — never whether a value is correct.

## Checking against a real appliance

The simulator answers every statement from that statement's own SELECT list, so
a column that does not exist on real ISE is invisible to it. `check_schema3.py`
is the check that is not:

```bash
python tools/check_schema3.py --config /etc/ise-exporter3/config.toml --cache schema.json
```

It costs one Oracle dictionary read — cached after the first run — and settles
both halves of "will this work here": every `view:` a provider declares, and
every column the generated SQL names. Non-zero exit on a mismatch, so it can
gate a deployment.

`tools/seed_lab3.py` creates tagged `ise3-sim-*` NADs and internal users on a
**lab** appliance, for exercising ERS paging and cache convergence against more
than a handful of devices, and removes them again with `--remove`. It writes to
ISE, which nothing else in this repository does.

## Repository layout

```
ise_exporter3/          the exporter; one dataset per file under datasets/
  limits.py             every row/byte/series ceiling, derived from [scale]
  rate_limit.py         the token bucket that enforces [budget]
  transports/           one connection per ISE persona
dashboards3/            generated Grafana dashboards
tools/                  dashboard generator, scale simulator,
                        live-appliance schema check, lab seeder
powershell/             operator cmdlets over the local API
ise-exporter3.toml.example
```

`docs/` and `tests/` are intentionally untracked here, as in the v2 repository;
they exist in a working checkout but are not committed.
