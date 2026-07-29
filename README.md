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
every then-implemented non-pxGrid dataset collecting, no `schema_incompatible`,
and every view and column the generated SQL names present on the real catalogue.
One limitation remains:

- **Aggregate values are unverified.** A statement that runs and returns
  plausible numbers can still measure the wrong thing, and neither the lab nor
  the simulator settles that: the lab has almost no RADIUS or TACACS event
  volume, and the simulator's appliance is synthetic. Behaviour at 5,000 NADs
  rests on the simulator alone.

**pxGrid 2.0 is operational and lab-verified.** The exporter activates and
discovers services through the control API, reconciles `getSessions` with the
persistent STOMP-over-WSS topic, and pages/caches `getEndpoints`. On the live
lab it established the stream, reconciled and published all 27 active sessions,
matched the previous MnT count, completed the bounded endpoint read, and was
scraped successfully by Prometheus. Session reconnects and sequence gaps take a
fresh baseline before data is exposed again. Password or client-certificate
authentication is supported, with the same persistent account-lockout guard
and enforced request budget as the other transports.

`getSessions` is one unpaged response. The plan therefore shows
`limits.pxgrid_session_bytes`, derived as 8 KiB per declared active session with
a 64 MiB floor and 512 MiB hard cap. This is separate from Data Connect's
`result_bytes`: a production session object can carry enough AD, MDM, posture,
and authorization attributes to be much wider than a reporting row. A
`STREAM DOWN reason=response_too_large` during startup or reconciliation means
this REST baseline crossed that ceiling; it does not by itself mean the WSS
topic sent an oversized frame. Set `[scale].sessions` to the actual active
session count first. If records are unusually wide, explicitly raise
`[limits].pxgrid_session_bytes`; the warning detail reports the received
`Content-Length` when ISE supplies it and always reports the enforced ceiling.

### ISE-side pxGrid configuration required

The successful ISE 3.3 Patch 11 lab run required all of the following on ISE:

1. Under **Administration > System > Deployment**, at least one reachable node
   had the **pxGrid** persona enabled.
2. The pxGrid system certificate served on TCP 8910 was trusted by the exporter
   and its DNS SAN matched the hostnames ISE advertised in its pxGrid REST and
   WSS service URLs. Those advertised names must also resolve from the exporter
   host. In the lab ISE still advertised the legacy `ise01.ise.lab` identity, so
   that alias, its matching certificate, and the CA bundle all had to be kept.
3. For the password mode used by the lab, **Administration > pxGrid Services >
   Settings > Allow password based account creation** was enabled while the
   client was created. The client name exactly matched
   `targets.pxgrid.node_name`; its generated password was stored as
   `ISE_PXGRID_PASSWORD`, never in TOML. The setting may be disabled again after
   creating the account.
4. Under **Administration > pxGrid Services > Client Management > Clients**,
   that client was approved and enabled. The exporter deliberately fails
   activation while ISE reports it as `PENDING` or `DISABLED`; automatic
   approval is not required.
5. The client could read `com.cisco.ise.session` (`getSessions`) and
   `com.cisco.ise.endpoint` (`getEndpoints`), and subscribe to the session topic
   advertised by `com.cisco.ise.pubsub`—on ISE 3.3 this was
   `/topic/com.cisco.ise.session`. If custom pxGrid policies are used, grant only
   those read operations (`gets`) and that exact `subscribe` topic; the exporter
   does not need `sets`, `publish`, or pxGrid administrator access.
6. TCP 8910 and every HTTPS/WSS provider returned by `ServiceLookup` were
   reachable from the exporter. A deployment may advertise more than one
   provider; v3 tries the available peers and remembers the one that answers.

Certificate authentication is the alternative to steps 3's password. Generate
or import a separate client certificate under **Administration > pxGrid
Services > Client Management > Certificates**, make its identity match
`node_name`, configure `client_cert` and `client_key`, and omit
`ISE_PXGRID_PASSWORD`. The `ca_bundle` still verifies the ISE server and is not
a client credential.

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

Direct `plan` and `run` invocations also load that credentials file when it is
readable, so their provider selection matches the systemd service rather than
reporting credential-backed providers as unavailable. An explicit process
environment wins over the file. Use `--credentials-file PATH` for another
root/private file or `--no-credentials-file` for a deliberately offline plan.
The loader accepts only the three password keys above and refuses symlinks,
non-regular files, unexpected keys, duplicate keys, and group/world-readable
files.

To compare the declared `[scale]` with the current ISE deployment and preview
the plan at the observed size:

```bash
sudo /opt/ise-exporter3/.venv/bin/ise-exporter3 plan --live-scale
```

This performs five bounded reads: ERS totals for NADs, endpoint records, and
Internal Users; PAN OpenAPI for Device Admin policy sets; and MnT `ActiveCount`
for current sessions. It shows declared, observed, and effective values plus
their real-world meanings, then builds the preview plan from the effective
values. It does not edit `config.toml`. If a read is unavailable, its declared
value is shown as the fallback and the command exits nonzero instead of silently
calling a mixed observed/declared plan complete. `--discover-scale` is an alias,
and `--json` includes the same provenance as structured data.

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

Migration deliberately leaves v2 intact, so retiring it is a separate, explicit
step — taken once v3 has proven itself and the rollback target is no longer
worth its disk or the cleartext appliance passwords in its configuration:

```bash
sudo ./deploy/uninstall-v2.sh --dry-run    # print the plan, change nothing
sudo ./deploy/uninstall-v2.sh
```

It removes v2's unit, `/opt`, `/etc` and state trees, `ise-cli`, the `Ise.Cli`
module and the service account, and refuses to run at all unless
`ise-exporter3` is active — because removing the rollback while v3 is down
leaves the host with no exporter rather than with an older one. `--force`
overrides that check, `--yes` the confirmation.

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
| `[scale]` | how many NADs / endpoint records / active sessions / ISE Internal Users / Device Admin policy sets, so the plan is predictive for your estate |
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
| pxGrid | `active_sessions`, `posture_current`, `endpoint_inventory`, `endpoint_attributes` |

Metrics are prefixed `ise3_`. `provider` is a label on the data itself, because
two sources for one dataset rarely mean exactly the same thing — a dashboard can
gate on it, and when a dataset changes source the previous source's series are
dropped rather than merged.

Every provider also declares how much of the fleet it measures — `complete`,
`converging` or `bounded` — and a bounded one must say what it leaves out.

## Operator surface

`/metrics` on port 9618 for Prometheus — gzipped when the client offers it,
which it always does, taking a ~6.4 MiB scrape to well under a megabyte on the
wire — and a read-only operator API bound to localhost on 9619:

```
/api/v1/health   /api/v1/datasets   /api/v1/providers
/api/v1/targets  /api/v1/plan       /api/v1/plan.txt
/api/v1/dataconnect/views   /api/v1/dataconnect/query
/api/v1/dataconnect/status
```

The first six answer from state the exporter already computed and never reach
ISE. The `dataconnect` namespace is the deliberate exception: it runs bounded,
server-built SELECTs against the reporting views **through the same transport
the scheduled datasets use** — same pacing gate, same adaptive cooldown, same
row/byte ceilings, same authentication guard. An ad-hoc operator query charges
the declared duty cycle exactly like a scheduled collection, so navigation
cannot out-spend the budget; it can only wait its turn. One explorer query runs
at a time, and a query that would wait long for its turn is answered with
"cooling down, retry in Ns" rather than blocking.

`powershell/Ise.Cli3/` wraps those routes as cmdlets; `powershell/ise-cli3` and
`Ise.Cli3.Profile.ps1` are the shell entry points. The free, local-state
cmdlets are `Get-IseHealth`, `Get-IseDataset`, `Get-IseProvider`,
`Get-IseTarget`, `Get-IsePlan`, `Get-IseDegraded`. Everything that reaches
Oracle carries the `Dc` mark in its noun, so the cost of a command is visible
in its name.

### Navigating Data Connect

PowerCLI-style navigation of every reporting view the Data Connect account
can see, from the shell — sixteen curated views carry time windows, default
ordering and typed cmdlets; the rest of the catalogue (Cisco documents ~70
views) is queryable exactly as discovered:

```powershell
ise> Get-IseDcView                     # what is there, and is it available
ise> Get-IseDcColumn -View radius_authentications

ise> Get-IseDcRadiusAuth -Failed -Last 2h |
       Group-Object failure_reason | Sort-Object Count -Descending

ise> Get-IseDcTacacsCommand -User jdoe -Last 1d
ise> Get-IseDcEndpoint -Policy 'Cisco-IP-Phone*' -First 500

ise> Invoke-IseDcQuery -View radius_errors_view -Last 4h `
       -Match @{ NETWORK_DEVICE_NAME = 'core-*' } -First 200

ise> Invoke-IseDcQuery -View endpoints_data -First 1 -All   # every column
```

Two cmdlets answer the questions the web UI answers, in the shape it answers
them — same columns, same order, same newest-first ordering, so the table is
recognisable without reading the help:

```powershell
ise> Get-IseRadiusLiveLog -Last 1h                  # Operations > RADIUS > Live Logs
ise> Get-IseRadiusLiveLog -Status Fail -Last 4h | Group-Object failure_reason
ise> Get-IseContextVisibility -Profile 'Cisco-IP-Phone*' -First 500
ise> Get-IseContextVisibility -Last 1h -WithLastAuth   # + the Authentication tab
```

They are ordinary Data Connect reads on the same paced transport, so they cost
what any other query costs — and `-WithLastAuth` costs twice, because attaching
each endpoint's most recent authentication is a second statement against a
second view. Neither refreshes on a timer: polling on the UI's cadence would
spend the whole Oracle duty cycle on one terminal and starve the scheduled
datasets sharing it.

Filters, projections, ordering and row limits are applied server-side through
bind variables against the discovered catalog — the shell never sends SQL. A
query returns the whole row; what the default table shows is a curated handful
of columns per view, so it reads at a glance in a narrow terminal. `-All`
declines that trimming and shows every column returned — a display choice on
rows already fetched, so it costs no extra duty cycle.
`-AsSql` shows the statement a query would run without spending Oracle time,
`-Wait` sits out a duty-cycle cooldown, and truncated results say so. `-Force`
is the incident override: it skips the cooldown waits and charges only measured
Oracle time, while keeping every ceiling, the statement timeout, the auth guard
and the one-statement-at-a-time lane — and forced use is counted separately in
the exporter's own metrics. The operator guide is `docs/ise-cli3.md` in a
working checkout.

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
