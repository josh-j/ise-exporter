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
| `Get-IseContextVisibility -ViaPxGrid` | the session half, from the live pxGrid snapshot |

`Get-IseDcView`, `Get-IseDcColumn` and `Get-IseDcStatus` carry the `Dc` in
their noun but spend nothing: they read the catalog the exporter already
discovered, not Oracle. `-ViaPxGrid` is free for the same reason -- the session
snapshot is already held in the exporter -- but the endpoint read it enriches
still costs one statement.

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

One cmdlet spends a third budget. `Get-IseContextVisibility -WithSession` costs
no Oracle duty cycle, but it issues one MnT request **per endpoint** against the
PAN request budget, which is why it is capped rather than scaled.

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
Get-IseContextVisibility -Mac 'AA:BB:CC:*' -ViaPxGrid      # live, no Oracle cost
```

Live Logs rows carry a `status` of `Pass` or `Fail`. That is the screen's word
for the `FAILED` flag this view actually stores; the flag stays on the row for
anything that would rather test a number. Rows come back newest-first, ordered
server-side, so the row cap keeps the newest rows rather than an arbitrary page.

Context Visibility reads the endpoint database, which has no authenticated user
-- only the portal one -- so the identity column the screen leads with is blank
until something fills it. Three sources can, and they are nearly disjoint, so
they add rather than compete. `-Full` turns on all three.

| switch | adds | source | costs |
| --- | --- | --- | --- |
| `-WithProbe` | `probe_*` | the endpoint's profiling attributes | nothing |
| `-WithLastAuth` | `auth_*` | the last RADIUS authentication | a second statement |
| `-ViaPxGrid` | `session_*` | the live pxGrid session | nothing |
| `-WithSession` | `mnt_*` | MnT's full session detail | one request **per endpoint** |

Measured on a live appliance: **50 of an endpoint's 53 readable probe
attributes have no pxGrid counterpart, and 28 of pxGrid's 31 session fields
have no probe counterpart.** Only three overlap. Making them exclusive threw
away most of what is knowable about an endpoint.

`-WithProbe` is free because `PROBE_DATA` is already in the row this cmdlet
fetches and was being discarded. Cisco's view truncates it, so a busy endpoint
arrives partial and warns.

`-WithLastAuth` reaches anything that authenticated inside `-AuthLast`
(default `1d`); `-ViaPxGrid` reaches what holds a session **now**. Both are one
extra read for the whole result, not one per endpoint, and an endpoint outside
a source's reach gets the columns present and empty -- which says "not seen"
rather than implying something false.

`-WithSession` is the odd one and is not in `-Full`. MnT carries what nothing
else does -- accounting counters (`mnt_acct_input_octets`), the policy
`mnt_execution_steps`, and the correlation ids (`mnt_audit_session_id`,
`mnt_cpmsession_id`) that pxGrid's session record lacks: 39 of its 47 fields
have no counterpart in any other source. But MnT has no bulk form, so it is one
request per endpoint against the PAN budget. It therefore refuses rather than
scales: past `-SessionLimit` (default 25) it stops and says how many it left,
instead of issuing hundreds of requests nobody asked for.

```powershell
Get-IseContextVisibility -Mac 'AA:BB:CC:11:22:33' -Full -WithSession
```

Whatever supplied them, `identity` and `nad` are resolved from whichever source
had one, so the default table reads the same under any combination.

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

**Data Connect shows only part of an endpoint's profiling data, by design.**
Cisco's view projects the column as

```sql
utl_raw.cast_to_varchar2(dbms_lob.substr(EDF_KRYOBUFFER, 2000)) AS Probe_data
```

`EDF_KRYOBUFFER` is a LOB holding the whole attribute set; the view exposes its
first 2000 bytes and reinterprets them as text, which is also why the value is
binary in a character column. The 2000 is Cisco's constant, not a column width
-- the column itself is declared `VARCHAR2(32767)`.

So a busy endpoint gets cut. The serialised header still declares the real
count, which is the only reason this is visible at all: 137 declared and 53
shown is ordinary. The cmdlet warns naming how many are missing rather than
presenting a prefix as the whole set.

The rest is not lost, only unreachable here -- it is still in ISE, behind a
view that does not project it. Nothing in this shell or the exporter can widen
that. Read the endpoint over ERS for a complete attribute set.

On a 200-endpoint lab sample only 2 endpoints hit the cut, so most rows are
complete; `truncated` says which.

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

### Lookups do not wait

A keyed lookup in a current-state view is served straight through a cooldown
that would refuse an ordinary read, and needs no `-Force` to do it:

```powershell
Get-IseDcEndpoint -Mac 'AA:BB:CC:11:22:33' -First 1
Get-IseEndpointProbe -Mac 'AA:BB:CC:11:22:33'
Get-IseContextVisibility -Mac 'AA:BB:CC:11:22:33'
```

"Which policy is this MAC on" costs the same on a loud appliance as a quiet
one, so queueing it behind a scan's cooldown delays an answer that was never
part of the load the cooldown exists to shape. It is deliberately narrow: a
current-state view, unwindowed, at least one exact-value filter, at most 25
rows, and no grouping. An event view is never a lookup however it is filtered,
because its window is the work. Anything outside that waits its turn.

It skips the wait, not the accounting. A lookup pays its full cooldown *added*
to the outstanding one, so the appliance sees the same total Oracle time over
any window and only the order of service changes. That is what makes it
different from `-Force`.

Cutting in is bounded, because a loop of lookups is a different thing from a
lookup. Each one charges a cooldown and waits none of it, so a script in a
tight loop would push the shared deadline out faster than it drains and the
scheduled datasets behind it would stop running. The exporter tracks how much
the lookups themselves owe the scheduler and stops letting them cut in past
about a minute of it; past that a lookup queues and is refused with the same
`cooldown` as a scan, which is the honest answer -- the wait it would have been
skipping is one the lookups built. One lookup during a long cooldown is
unaffected, which is the case this exists for.

`-Force` is the incident override: run now instead of waiting out the cooldown,
and charge only the measured Oracle time. It overrides the *waits*, never the
guards -- ceilings, statement timeout, authentication guard and the
one-at-a-time lane all still apply, the hard floor between statements is still
charged, and forced use is counted separately in the exporter's metrics.

## Every cmdlet, one example

Free, answered from what the exporter already knows:

```powershell
Get-IseHealth                                   # healthy, and inside budget?
Get-IseDataset -Unhealthy                       # what is failing right now
Get-IseDataset radius* | Format-Table dataset, provider, interval
Get-IseDegraded                                 # what fell back, and to what
Get-IseProvider -Dataset active_sessions        # who can supply it, who is
Get-IseTarget                                   # planned load per persona
Get-IsePlan -AsText                             # the whole plan, rendered
Get-IseApiRoot                                  # which exporter am I talking to
Set-IseApiRoot -Uri http://mon-02:9619          # talk to a different one
Get-IseDcStatus                                 # busy? cooling down? for how long?
Get-IseDcView                                   # every view, and whether it is visible
Get-IseDcView endpoints*                        # by wildcard
Get-IseDcColumn -View radius_authentications    # its columns, defaults marked
Get-IseDcColumn radius_authentications *station*
```

Paced, each one a real Oracle statement:

```powershell
Get-IseDcRadiusAuth -Failed -Last 2h
Get-IseDcRadiusAccounting -Mac 'AA:BB:CC:*' -Last 1d
Get-IseDcRadiusError -Last 4h -Nad 'core-*'
Get-IseDcEndpoint -Policy 'Cisco-IP-Phone*' -First 500
Get-IseDcTacacsAuth -User jdoe -Last 1d
Get-IseDcTacacsCommand -User jdoe -Last 1d
Get-IseDcTacacsAuthorization -Device 'core-*' -Last 1d
Get-IseDcPosture -Mac 'AA:BB:*' -Last 4h
Get-IseDcNodeHealth -Last 1h
Get-IseDcNodePerformance -Node 'laba-psn-*' -Last 1h
Get-IseRadiusLiveLog -Last 1h
Get-IseContextVisibility -Mac 'AA:BB:CC:11:22:33' -Full
Get-IseEndpointProbe -Mac 'AA:BB:CC:11:22:33'
Invoke-IseDcQuery -View profiled_endpoints_summary -Last 1d
```

## Recipes

Questions an operator actually arrives with, and the shortest honest answer to
each.

### Why can this user not get on?

```powershell
Get-IseRadiusLiveLog -Identity jdoe -Last 4h
Get-IseRadiusLiveLog -Identity jdoe -Status Fail -Last 1d |
    Select-Object timestamp, calling_station_id, device_name, failure_reason
```

Then the endpoint's side of it:

```powershell
Get-IseContextVisibility -Mac 'AA:BB:CC:11:22:33' -Full | Format-List *
```

### What is failing across the estate, and is it one thing or many?

```powershell
Get-IseRadiusLiveLog -Status Fail -Last 4h | Group-Object failure_reason |
    Sort-Object Count -Descending | Select-Object Count, Name
```

One statement instead of paging rows, when the window is wide:

```powershell
Invoke-IseDcQuery -View radius_authentications -Last 1d `
    -Filter @{ FAILED = 1 } -GroupBy FAILURE_REASON -First 20
```

### Which NAD is generating the failures?

```powershell
Invoke-IseDcQuery -View radius_authentications -Last 1d `
    -Filter @{ FAILED = 1 } -GroupBy DEVICE_NAME -First 20
```

Narrow to one site, if the device names carry it:

```powershell
Invoke-IseDcQuery -View radius_authentications -Last 1d `
    -Filter @{ FAILED = 1 } -Match @{ DEVICE_NAME = 'bld3-*' } `
    -GroupBy DEVICE_NAME, FAILURE_REASON -First 30
```

### Is a PSN unhealthy, or just busy?

```powershell
Get-IseDcNodePerformance -Last 2h |
    Sort-Object avg_latency_per_req -Descending
Invoke-IseDcQuery -View radius_authentications -Last 2h `
    -GroupBy ISE_NODE -Aggregate avg:RESPONSE_TIME, max:RESPONSE_TIME
Get-IseDcNodeHealth -Last 1h |
    Select-Object ise_node, cpu_utilization, memory_utilization
```

### Who is slow?

```powershell
Invoke-IseDcQuery -View radius_authentications -Last 1h `
    -Min @{ RESPONSE_TIME = 2000 } -OrderBy RESPONSE_TIME -Descending
```

### What does ISE actually know about this endpoint?

```powershell
Get-IseEndpointProbe -Mac 'AA:BB:CC:11:22:33'
Get-IseEndpointProbe -Mac 'AA:BB:CC:11:22:33' -Name '*AD-*'
(Get-IseEndpointProbe -Mac 'AA:BB:CC:11:22:33' -AsObject).OUI
```

### Which endpoints changed lately?

```powershell
Get-IseContextVisibility -Last 1h -First 200
Get-IseDcEndpoint -Last 30m -First 100 |
    Group-Object endpoint_policy | Sort-Object Count -Descending
```

### Who ran what on the switches?

```powershell
Get-IseDcTacacsCommand -Last 1d -First 200 |
    Select-Object epoch_time, username, device_name, command, command_args
Get-IseDcTacacsCommand -User jdoe -Last 2d
Invoke-IseDcQuery -View tacacs_accounting_last_two_days -Last 1d `
    -GroupBy USERNAME -First 20
```

### Is posture actually working?

```powershell
Invoke-IseDcQuery -View posture_assessment_by_endpoint -Last 1d `
    -GroupBy POSTURE_STATUS
Get-IseDcPosture -Last 4h -First 100 |
    Where-Object posture_status -ne 'Compliant'
```

### What is this exporter costing ISE?

```powershell
Get-IseTarget
Get-IseDcStatus                                  # duty cycle, cooldown, busy
Get-IsePlan -AsText
```

### Hand it to somebody else

```powershell
Get-IseRadiusLiveLog -Status Fail -Last 1d -All |
    Export-Csv failures.csv -NoTypeInformation
Get-IseContextVisibility -Mac 'AA:BB:*' -Full |
    ConvertTo-Json -Depth 6 | Out-File endpoint.json
```

## Shaping the output

Every cmdlet emits real objects, so the pipeline does the work:

```powershell
... | Format-Table col1, col2          # pick columns
... | Format-List *                    # every column, one per line
... | Select-Object -First 20          # or Sort-Object, Group-Object
... | Where-Object failed -eq 1        # filter client-side
... | Export-Csv out.csv -NoTypeInformation
... | ConvertTo-Json -Depth 6
... | Out-Host -Paging                 # page a long result
```

Filter server-side when you can and client-side when you must: a `-Filter`
travels as a bind and narrows the scan, a `Where-Object` runs after the rows
were already fetched and paid for.

## Troubleshooting

| Symptom | What it means |
| --- | --- |
| every cmdlet fails to connect | the exporter is not running, or `Get-IseApiRoot` points elsewhere |
| `schema_pending` | Data Connect discovery has not finished; `Get-IseDcStatus` shows when |
| a column is always empty | the projection may not carry it; `Get-IseDcColumn -View <name>` says what exists |
| a table looks narrower than the data | that is the format view; `-All` or `Format-List *` |
| a grouped query returns fewer groups than expected | `-First` caps groups too, largest-first |
| a window returns nothing | the view may be current-state, or the window is shorter than the sync interval |
| an endpoint has no `auth_*` | it did not authenticate inside `-AuthLast` |
| an endpoint has no `session_*` | it is not connected now |
| an endpoint has no `mnt_*` | MnT holds no session for it (reported as cpm-code 34110) |
| `-WithSession` warns about a shortfall | more endpoints than `-SessionLimit`; narrow the result |
| `probe_data.truncated` is true | Cisco's view exposes only the first 2000 bytes |

## Where to look next

`Get-IseDcView` names every view the account can reach and whether it is
available; `Get-IseDcColumn -View <name>` names its columns. Anything in that
catalog is queryable with `Invoke-IseDcQuery` whether or not a typed cmdlet
wraps it.
