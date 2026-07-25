# ise-exporter3 roadmap

Work this build needs before it can replace v2, ordered by severity.

Items 1 and 3–6 come from a production-scale simulation run on 2026-07-25:
24 simulated hours of `ise-exporter3.toml.example` against a synthetic estate of
5,000 NADs / 100,000 endpoints / 20,000 sessions / 8 PSNs, 1,309 collections.
Reproduce with:

```bash
python tools/simulate_scale3.py --hours 24 --json scale.json
```

Every number below is measured in that harness unless it says otherwise. The
harness runs the real scheduler, transports, datasets and publication boundary;
what it cannot check is whether a value is *correct*, because the appliance is
synthetic. Where an item depends on a modelled assumption rather than a
measurement, it says so and names the measurement that would settle it.

Status key: `[ ]` open · `[~]` partly done · `[x]` done

---

## 1. `tacacs_activity` cannot run at the declared scale

**Severity: the dataset publishes nothing.** 8 of 8 collections failed over 24
simulated hours with `response_too_large: batch exceeded the 12000-row ceiling`,
and every failure still pays for the Oracle scans it made before aborting.

The ceilings contradict each other. `reporting.MAX_GROUPS = 5500` bounds one
statement; `MAX_BATCH_QUERIES = 5` allows five per batch; but
`MAX_BATCH_RESULT_ROWS = 12_000` (`transports/dataconnect.py:50`) is smaller
than three full-size marginals. TACACS trips it because both its dimensions are
large — roughly 1,000 admin accounts plus every device doing Device Admin — so
each of its three statements returns at the group cap. It fails once TACACS
touches more than about 3,000 devices, which the declared scale exceeds.

- [ ] **1a. Derive the batch ceiling instead of choosing one.**
  `MAX_BATCH_RESULT_ROWS = MAX_BATCH_QUERIES * MAX_RESULT_ROWS` (30,000), with a
  contract test asserting `reporting.MAX_GROUPS * MAX_BATCH_QUERIES <=
  MAX_BATCH_RESULT_ROWS` so the two cannot drift apart again. Nothing is given
  up: the real safety bound is `MAX_RESULT_BYTES` (64 MiB) and 30,000 rows is
  about 6 MB.

  **Necessary but not sufficient — verified.** With only 1a applied the dataset
  runs and publishes **27,501 series (55 % of the 50,000 hard ceiling)** with
  `authentications`, `authorizations` and `commands` all reporting truncated.
  That trades a loud failure for the second-largest metric family in the
  exporter plus a permanently incomplete breakdown.

- [ ] **1b. Bound the `device` dimension** (`datasets/tacacs_activity.py:46`),
  which is the whole problem: 5,000 NADs × 2 statuses × 2 statements. Either
  re-key it to `ops_owner` through the NAD-directory join the other datasets
  already do (~12 series), or keep per-device as a top-K by failures using
  `reporting.top_groups` and its truncation signal (~200–500 series).
  Recommended: the `ops_owner` marginal plus top-K failing devices. A Device
  Admin dashboard wants "who logged into what, and what failed", not a series
  per switch.

**If per-device series are genuinely wanted**, the alternative to 1b is
splitting the batch in two (auth + authz, then commands): no data lost, one
extra duty-cycle cooldown per 6 h collection, and ~27k series budgeted against
the 50k ceiling as a deliberate decision rather than an accident.

## 2. `[x]` A label named `value` crashed publication

`FetchContext.set(self, family, value, **label_values)` collided with
`tacacs_activity`'s own `value=` label, raising `TypeError` for every row —
permanently, for any dataset that names a label `value`. Fixed by making both
publication signatures positional-only (`runtime.py:75`, `snapshots.py:139`),
which removes the whole class of collision rather than the one instance.

- [ ] Regression test publishing a label named `value`. `tests/` is untracked
  here, so it needs `git add -f` if it should be committed.

## 3. NAD classification takes ~60 hours to converge

`network_devices` warms 500 devices per run at a 6 h cadence, so it reached
**2,000 of 5,000 (coverage 0.400) after a full simulated day**, with ~36 h to
go. The dataset's own note — "5,000 NADs converge in ten cycles" — is true, but
ten cycles is two and a half days.

This is not cosmetic: `ops_owner` exists only in ERS groups, and an unclassified
device is skipped before the directory is built (`network_devices.py:166`, feeding
the `replace` at :186), so it cannot attribute anything. Until the cache fills,
every ops-owner breakdown in every dashboard attributes most sessions to
`unknown`. `tacacs_config` has the same shape (100
accounts per run, 6 h) and reached 400 of 1,000.

- [ ] **Make the warm-up cadence budget-derived.** The signal already exists —
  `cache.publish(..., deferred_count=outstanding)` computes exactly how much
  work is left. Carry it out of the collection on `Outcome`, and have
  `Scheduler._collect` reschedule at `warmup_requests / budget_requests_per_hour`
  while work is pending, instead of at the dataset's cadence. At 500 per pass
  against a 3,000/h PAN budget that is a pass every 10 minutes: **5,000 NADs
  classified in ~100 minutes instead of 60 hours, at exactly the declared budget
  rate.** Same mechanism fixes `tacacs_config` (~20 minutes), and it is the
  natural home for item 4.

- [ ] Stopgap, one line: `WARMUP_FETCHES_PER_CYCLE = 2500`
  (`datasets/network_devices.py:39`) → ~12 h. It bursts 2,500 requests into a
  few minutes, which is exactly the behaviour item 4 is about.

## 4. MnT cold start runs at 2.8× the declared budget

Warming the session-detail cache for 20,000 sessions costs 20,000 MnT requests,
concentrated into the first **108 minutes at roughly 11,300 requests/hour
against a 4,000/hour budget**. Once warm it settles to 2,512/h — 63 % of budget,
and within 4 % of the 2,424/h the plan declares, so the steady-state cost model
is sound. Only the cold start is un-budgeted, and `docs/v3-status.md` already
records why: REST budget is measured, not throttled.

- [ ] **Enforce the budget** with a token bucket per REST target, sized from
  `budget.<target>.requests_per_hour` and checked in
  `RestTransport._request_locked`. Warm-up then stretches to fit and `deferred`
  climbs visibly, instead of the appliance absorbing the difference silently.

- [ ] **Then choose the warm-up policy explicitly**, because enforcement alone
  means a cold start takes ~12.5 h: only 1,600/h of the 4,000/h budget is left
  once 2,400/h of churn is paid. Either declare the burst
  (`budget.mnt.warmup_requests_per_hour`, so `plan` prints "11,000/h for ~2 h"
  as a decision), or raise `budget.mnt.requests_per_hour`.

**Blocked on a measurement, not a decision.** Which option is right depends on
what the MnT API actually costs, which the simulation assumes rather than knows.
Time `/Session/ActiveList` and `/Session/MACAddress/<mac>` on the lab appliance,
then re-run with `--latency-scale` set to the ratio. For reference the harness
puts the MnT lane at 15.7 % busy under its default model and 77 % at 5×, while
Oracle stays comfortable in both.

## 5. `session_authorization` publishes 20,420 series

Past the 20,000-sample soft warning and 41 % of the hard `MAX_SNAPSHOT_SAMPLES`
ceiling on its own. 20,000 of them are `ise3_session_policy_set_endpoints_by_nad`
— the dataset's one deliberate cross product, policy set × NAD.

- [ ] **Re-key it to `ops_owner`** (~84 series, and it matches how every other
  breakdown is grouped), or keep per-NAD as a top-K with `publish_truncation`.
  One metric family and the `policy_set_nad` bucket in `aggregate()`.

Not cosmetic: at 40,000 sessions this crosses the hard ceiling, and crossing it
raises `SnapshotError` — the dataset stops publishing entirely rather than
degrading.

## 6. Scrape and footprint

At the declared scale a scrape is **60,287 series / 5.8 MiB / 335 ms**, resident
memory ~305 MiB, and on-disk state 2.8 KiB (the no-history invariant holds).

- [ ] **6a. Compress `/metrics`.** `server.py:_respond` does no content
  negotiation; Prometheus always offers gzip and this text compresses roughly
  10×, so 5.8 MiB becomes ~600 KiB on the wire for a few lines of code.
- [ ] **6b. Compress outside the lock.** Generation holds `snapshot_lock` for
  335 ms per scrape, which blocks publication for that window. Fine at a 15 s
  interval; worth keeping compression out of the locked section when 6a lands.
- [ ] **6c. Slim the session-detail cache.** `mnt_session_detail` stores the
  whole MnT record per MAC × 20,000, where `network_devices` deliberately
  retains only a normalised projection. Applying the same discipline is the real
  memory reduction.

Measured and rejected: streaming the 9.5 MiB ActiveList with `iterparse` +
`clear` instead of a DOM saves ~12 MiB of a 46 MiB transient peak while costing
25 % more CPU (243 ms → 301 ms). The retained records are the cost, not the
parse.

---

## Sequencing

1. **1a + 1b** and **5** are contained — one file each plus a test, and the
   simulator verifies each directly.
2. **3 and 4** are one piece of work, not two: both are "a converging cache
   should fill at the rate the budget allows, not at the dataset's cadence", and
   both want the same signal carried from the cache to the scheduler.
3. **4's** second half waits on the lab latency measurement above.
4. **6** is polish; 6a is worth doing whenever `/metrics` is next touched.

## Already known, and not from the simulation

Tracked in `docs/v3-status.md`; repeated here because they gate the items above.

- **No live validation yet.** The Data Connect SQL and the ERS/MnT parsing have
  never met a real appliance. Expect to fix column names on first contact. The
  simulator answers whatever a statement asks for, so it structurally *cannot*
  catch a wrong column.
- **pxGrid is not ported.** `endpoint_attributes` is unavailable and datasets
  that prefer pxGrid fall back visibly.
- **Cutover is not attempted.** The sequence is: run v3 on a side port against
  the lab, diff `/metrics` against the running v2, then switch the unit.
