# ise-exporter3

A Prometheus exporter for Cisco ISE that treats **source selection and load
budgeting as the same decision**. Each dataset declares which sources can supply
it, in preference order, and what one collection costs the ISE persona it talks
to. Pacing, cooldowns, row ceilings and query timeouts are derived from a
declared budget and scale plus those costs rather than chosen, the budget is
**enforced** rather than merely checked, and what the exporter actually spent is
exported beside what it planned to spend.

This is the v3 build, split out of the v2 repository. Its configuration schema
is intentionally not a drop-in replacement for v2; the native installer below
stages both versions separately and performs an explicit health-checked handoff.

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

## Native systemd install, update, and v2 migration

Debian 12/13 and Ubuntu Server 24.04 can use the idempotent native installer:

```bash
sudo ./deploy/install.sh
```

It creates a locked `ise-exporter3` account, installs the application under
`/opt/ise-exporter3`, seeds `/etc/ise-exporter3/config.toml` once, installs the
hardened `ise-exporter3.service`, and exposes the PowerShell operator shell as
`/usr/local/bin/ise-cli3`. Re-run the same command from any newer checkout to
upgrade. Existing v3 configuration, certificates, passwords, and an
intentionally stopped service are preserved.

A fresh install without v2 present is enabled but left stopped until its hosts
and credentials validate. When v2 is detected, v3 remains disabled as well as
stopped so both cannot claim port 9618 after a reboot. Set or rotate passwords
without putting them in TOML, command arguments, or shell history:

```bash
sudoedit /etc/ise-exporter3/config.toml
sudo ise-exporter3-set-passwords --start
sudo systemctl status ise-exporter3
curl --fail --silent http://127.0.0.1:9618/metrics | head
```

The helper reads secrets without echo and atomically writes the systemd
environment file `/etc/ise-exporter3/credentials` as `root:root` mode `0600`.
It manages `ISE_PASS`, `ISE_DATACONNECT_PASSWORD`, and optional
`ISE_PXGRID_PASSWORD`. Blank input preserves the existing value. Use
`--no-restart` to stage a rotation or `--check` for a non-secret readiness
check. An active service restarts by default; an inactive one stays stopped
unless `--start` is given.

### Migrating an existing v2 service

The installer detects `ise-exporter.service` and stages v3 beside it without
stopping v2 or claiming its port. It preserves the complete v2 `/opt` and
`/etc` trees, copies CA material only when the v3 certificate directory is
empty, and can import non-placeholder v2 passwords without printing them:

```bash
sudo ./deploy/install.sh
sudoedit /etc/ise-exporter3/config.toml       # v2 TOML is not schema-compatible
sudo ise-exporter3-set-passwords --import-v2 --no-restart
sudo ./deploy/install.sh --migrate-v2
```

The explicit migration stops and disables v2, starts v3, and requires an active
unit, a successful unauthenticated local `/metrics` request, an in-budget
operator-health response, and at least one successfully collecting dataset. If
any check fails, it disables v3 and restores v2's previous enabled/running
state. Once migrated, later `deploy/install.sh` runs are ordinary in-place v3
updates.

The service runs `ise-exporter3 plan` as `ExecStartPre`, so invalid or
over-budget configuration is rejected before collection begins. It is limited
to three starts per hour with five minutes between failure restarts, uses
`StateDirectory=ise-exporter3`, and has a read-only system sandbox around its
configuration and certificates.

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
