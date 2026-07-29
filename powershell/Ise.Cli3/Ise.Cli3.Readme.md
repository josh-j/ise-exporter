# ise-cli3

The operator surface for ise-exporter3. Everything here is read-only.

The cmdlets talk to the exporter's local API over HTTP. There is no second ISE
client and no second Oracle session: an ad-hoc query travels the exporter's own
transports, so it cannot bypass the pacing gate, the authentication guard, or
the row and byte ceilings, because there is no other process to bypass them
from.

`Get-IseCliReadme -Section <name>` prints one part of this; `-List` names them.

## What costs what

Two groups, because they cost different things.

**Free.** Answered from state the exporter already computed. Run them as often
as you like.

| Cmdlet | Answers |
| --- | --- |
| `Get-IseHealth` | is it healthy, and inside its budget |
| `Get-IseDataset` | every dataset, its provider, interval, and failures |
| `Get-IseDegraded` | what fell back, and to what |
| `Get-IseProvider` | which source is supplying each dataset |
| `Get-IseTarget` | planned load against the declared budget |
| `Get-IsePlan` | the full plan report (`-AsText` for the rendered one) |
| `Get-IseDcView` | the reporting views, their columns, and availability |
| `Get-IseDcColumn` | one view's columns, and which are curated defaults |
| `Get-IseDcStatus` | configured, discovered, busy, cooling down |
| `Get-IseApiRoot` / `Set-IseApiRoot` | which exporter this session talks to |

`Get-IseDcView`, `Get-IseDcColumn` and `Get-IseDcStatus` carry the `Dc` in
their noun but spend nothing: they read the catalog the exporter already
discovered, not Oracle.

**Paced.** Every cmdlet below issues a real Oracle statement through Data
Connect and is charged against the declared duty cycle exactly like a scheduled
collection. One runs at a time, and each one earns a cooldown that every
reporting dataset also waits out.

| Cmdlet | View |
| --- | --- |
| `Invoke-IseDcQuery` | any view in the discovered catalog |
| `Get-IseDcRadiusAuth` | RADIUS authentications |
| `Get-IseDcRadiusAccounting` | RADIUS accounting |
| `Get-IseDcRadiusError` | RADIUS errors |
| `Get-IseDcEndpoint` | the endpoint database |
| `Get-IseDcTacacsAuth` | TACACS authentications |
| `Get-IseDcTacacsCommand` | TACACS accounting, the commands run |
| `Get-IseDcTacacsAuthorization` | TACACS authorizations |
| `Get-IseDcPosture` | posture by endpoint |
| `Get-IseDcNodeHealth` | node system summary |
| `Get-IseDcNodePerformance` | node key performance metrics |
| `Get-IseRadiusLiveLog` | the Live Logs screen |
| `Get-IseContextVisibility` | the Context Visibility screen |
| `Get-IseEndpointProbe` | one endpoint's profiling attributes |

## Pointing at an exporter

Defaults to `http://127.0.0.1:9619`. Override for the session with
`Set-IseApiRoot -Uri http://host:9619`, or for the shell with the
`ISE_EXPORTER_API` environment variable.

## The two screens

`Get-IseRadiusLiveLog` and `Get-IseContextVisibility` answer what the web UI
answers, in the shape it answers it. They are ordinary Data Connect reads; what
makes them replicas is the column set and the ordering.

```powershell
Get-IseRadiusLiveLog -Last 1h
Get-IseRadiusLiveLog -Status Fail -Last 4h | Group-Object failure_reason
Get-IseRadiusLiveLog -Nad 'core-*' -Node 'laba-psn-01' -Last 30m

Get-IseContextVisibility -Profile 'Cisco-IP-Phone*' -First 500
Get-IseContextVisibility -Mac 'AA:BB:CC:*' -WithLastAuth | Format-List *
```

Live Logs rows carry a `status` of `Pass` or `Fail`. That is the screen's word
for the `FAILED` flag this view actually stores; the flag stays on the row for
anything that would rather test a number. Rows come back newest-first, ordered
server-side, so the row cap keeps the newest rows rather than an arbitrary page.

Context Visibility reads the endpoint database, which has no authenticated user
-- only the portal one -- so the identity column the screen leads with is blank
until `-WithLastAuth` attaches each endpoint's most recent authentication as
`auth_time`, `auth_identity`, `auth_nad`, `auth_profiles`, `auth_method`,
`auth_posture` and `auth_node`. That is one extra statement for the whole
result, not one per endpoint, and it reaches only endpoints that authenticated
inside `-AuthLast` (default `1d`). Anything quieter comes back with those
columns present and empty, which says "did not authenticate in the window"
rather than implying something false.

Neither auto-refreshes. The UI does; these deliberately do not, because polling
on that cadence would spend the whole Oracle duty cycle on one terminal and
starve every scheduled dataset sharing it.

## Filters

Values travel as bind variables against the discovered catalog. The shell never
sends SQL.

`*` and `?` in a value make it a pattern; anything else is an exact match. `%`
and `_` are literal, so `svc_backup` does not quietly match `svcXbackup`.

```powershell
Get-IseDcRadiusAuth -User jdoe -Last 2h          # exact
Get-IseDcRadiusAuth -Nad 'core-*' -Last 2h       # pattern
```

`Invoke-IseDcQuery` takes the general forms: `-Filter` (equals), `-Match`
(pattern), `-Min` and `-Max` (inclusive bounds; both on one column says
between), `-Exclude`, `-IsNull`, `-NotNull`.

```powershell
Invoke-IseDcQuery -View radius_authentications -Last 1h `
    -Min @{ RESPONSE_TIME = 500 } -Max @{ RESPONSE_TIME = 2000 } `
    -Exclude @{ USERNAME = 'svc_probe' } -NotNull DEVICE_NAME
```

## Windows and row limits

`-Last` takes `30m`, `2h`, `1d`, and is clamped to the exporter's configured
window ceiling. `-Last all` trades the time bound for the row bound: the newest
`-First` rows, however old. The TACACS `*_last_two_days` views carry 48 hours
regardless.

`-First` caps rows (server default 100, clamped server-side). A truncated
result says so rather than looking complete.

## Grouping

`-GroupBy` turns a query into a bounded top-N over the whole window -- the
fleet aggregated in one statement instead of a thousand rows paged through
`Group-Object`.

```powershell
# Which NADs failed the most in the last day?
Invoke-IseDcQuery -View radius_authentications -Last 1d `
    -Filter @{ FAILED = 1 } -GroupBy DEVICE_NAME -First 20

# Mean and worst response time per PSN
Invoke-IseDcQuery -View radius_authentications -Last 4h `
    -GroupBy ISE_NODE -Aggregate avg:RESPONSE_TIME, max:RESPONSE_TIME
```

Without `-Aggregate` a grouped query counts. Without `-GroupBy` an aggregate
answers for the whole window in one row.

## What comes back

Real objects carrying the whole row -- every column the Data Connect account
can see. What narrows the output is display: each view has a format table
naming a readable few columns so the default fits a terminal.

`-All` declines that trimming and shows every column the row carries. It
changes nothing about the query: same statement, same rows, same duty cycle.

```powershell
Get-IseDcEndpoint -First 1 -All
Get-IseDcEndpoint -First 1 | Format-List *      # the same columns
Get-IseDcEndpoint -Column MAC_ADDRESS,ENDPOINT_POLICY    # fetch less on purpose
```

The pipeline does the rest: `Where-Object`, `Group-Object`, `Export-Csv`,
`ConvertTo-Json`.

### Probe data

`PROBE_DATA` on the endpoint database holds everything ISE profiled about an
endpoint as a byte stream, not text. The exporter decodes it, and
`Get-IseEndpointProbe` is the readable form: one row per attribute, sorted,
ready for `Where-Object` and `Export-Csv`.

```powershell
Get-IseEndpointProbe -Mac 'AA:BB:CC:11:22:33'
Get-IseEndpointProbe -Mac 'AA:BB:*' -Name '*MFCInfo*'
(Get-IseEndpointProbe -Mac 'AA:BB:CC:11:22:33' -AsObject).OUI
```

```
   Endpoint: 10:66:6a:69:19:42

Name                                   Value
----                                   -----
AD-Join-Point                          LAB.LOCAL
AD-User-SamAccount-Name                user1
AuthenticationMethod                   PAP_ASCII
AuthenticationStatus                   AuthenticationPassed
NetworkDeviceName                      campus-corp-wired
OUI                                    Zabbly
```

It fetches only `MAC_ADDRESS` and `PROBE_DATA`, so it is much cheaper than a
whole-row endpoint read, though it charges the same duty cycle.

Empty attributes are hidden. ISE names an attribute whether or not it has a
value, so a real endpoint carries dozens of blanks; `-IncludeEmpty` puts them
back.

**ISE stores more attributes than its own column holds.** The serialised header
declares the full count and the column keeps a prefix -- 137 declared and 53
kept is ordinary. That truncation happens in the database, not in the exporter
or the shell, and the cmdlet warns naming how many were lost rather than
presenting a prefix as the whole set. Nothing can recover the rest through Data
Connect; the ERS endpoint API is the place to go for a complete attribute set.

The raw field is still there on an ordinary endpoint row:

```powershell
(Get-IseDcEndpoint -Mac 'AA:BB:CC:11:22:33' -First 1).probe_data |
    Select-Object encoding, count, declared, truncated
```

`encoding` is `ise-tlv` when the appliance's own framing was read. If a future
release changes that framing, `attributes` comes back empty and the field turns
into a report on itself -- `strings`, `head`, `separators` and the base64 `raw`
-- rather than guessing. An empty `attributes` with a populated `raw` means
"not understood", never "nothing there": a half-parsed attribute set would look
exactly like a real one, so it is not offered.

`PROBE_DATA` is large, and the whole row is returned by default. On a big
endpoint database, `-Column` is how to leave it behind:

```powershell
Get-IseDcEndpoint -Column MAC_ADDRESS,ENDPOINT_POLICY,ENDPOINT_IP -First 5000
```

## Judging cost before spending it

`-AsSql` returns the statement and its binds without touching Oracle. It is
free, and allowed even during a cooldown, so a heavy query can be read before
it is run.

```powershell
Invoke-IseDcQuery -View radius_authentications -Last 24h -AsSql
```

## When a query is refused

A refusal is information, not a failure. The message names the guard.

| Refusal | Meaning |
| --- | --- |
| cooling down | the duty cycle is still being paid off; `-Wait` sits it out |
| busy | another query is in flight; the exporter runs one at a time |
| schema_pending | catalog discovery has not finished; `Get-IseDcStatus` shows when |
| unknown_view | not a legal view name; `Get-IseDcView` lists what is there |
| view_unavailable | the account cannot see that view |

`-Wait` honours the retry the exporter asked for rather than polling.

`-Force` is the incident override: run now instead of waiting out the cooldown,
and charge only the measured Oracle time. It overrides the *waits*, never the
guards -- ceilings, statement timeout, authentication guard and the
one-at-a-time lane all still apply, the hard floor between statements is still
charged, and forced use is counted separately in the exporter's metrics.

## Where to look next

`Get-IseDcView` names every view the account can reach and whether it is
available; `Get-IseDcColumn -View <name>` names its columns. Anything in that
catalog is queryable with `Invoke-IseDcQuery` whether or not a typed cmdlet
wraps it.
