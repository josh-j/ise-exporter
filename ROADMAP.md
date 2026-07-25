# ise-exporter3 roadmap

Work this build needs before it can replace v2, ordered by severity.

**All six items are done.** Each was found by a production-scale simulation run
on 2026-07-25 — 24 simulated hours of `ise-exporter3.toml.example` against a
synthetic estate of 5,000 NADs / 100,000 endpoints / 20,000 sessions / 8 PSNs —
and each was re-measured in the same harness afterwards. Both figures are kept
below, because "we fixed it" is a claim and the pair is evidence. Reproduce with:

```bash
python tools/simulate_scale3.py --hours 24 --json scale.json
```

Every number below is measured in that harness unless it says otherwise. The
harness runs the real scheduler, transports, datasets and publication boundary;
what it cannot check is whether a value is *correct*, because the appliance is
synthetic. Where an item depends on a modelled assumption rather than a
measurement, it says so and names the measurement that would settle it — and one
still does: **item 4's warm-up burst value is deliberately left undeclared until
the MnT API is timed on a real appliance.**

What remains before this can replace v2 is in **Already known** at the end, and
none of it is code: live validation, the pxGrid port, and the cutover itself.

Status key: `[ ]` open · `[~]` partly done · `[x]` done

---

## 1. `[x]` `tacacs_activity` cannot run at the declared scale

**Was: the dataset published nothing.** 8 of 8 collections failed over 24
simulated hours with `response_too_large: batch exceeded the 12000-row ceiling`,
and every failure still paid for the Oracle scans it made before aborting.

The ceilings contradicted each other. `reporting.MAX_GROUPS = 5500` bounded one
statement; `MAX_BATCH_QUERIES = 5` allowed five per batch; but
`MAX_BATCH_RESULT_ROWS = 12_000` was smaller than three full-size marginals.
TACACS tripped it because both its dimensions are large — roughly 1,000 admin
accounts plus every device doing Device Admin — so each of its three statements
returned at the group cap.

**Re-measured after the fix, same 24-hour harness: 4 of 4 collections
succeeded, 0 failures, 6,074 series.** The only truncation reported is the
declared device bound from 1b. Total surface 66,377 series / 6.4 MiB / 341 ms;
oracle duty 0.41 % measured against the 2.00 % budget.

- [x] **1a. Derive the batch ceiling instead of choosing one.** Done, and wider
  than the item asked: the root cause was not the value but that a ceiling on
  *how many groups a fleet produces* was a constant at all. `limits.py` now
  derives the whole chain from `[scale]` —

      group_ceiling     = nads + accounts + 500     (marginals add, so this is a sum)
      result_rows       = group_ceiling + 500       (a full set of groups is a success)
      batch_result_rows = result_rows x batch_queries

  which resolves to 6,500 / 7,000 / 35,000 at the declared scale. The contract
  test the item asks for is `test_a_full_batch_of_full_statements_fits_the_batch_ceiling`,
  asserted over every profile scale, and the same invariant is checked at config
  load — the old 5,500/12,000 pair now raises `ConfigError` rather than loading.
  Nothing is given up: the real safety bound is `result_bytes` (64 MiB) and
  35,000 rows is about 7 MB.

  The ceilings are configuration now (`[limits]`), printed by `plan` with the
  origin of each value, and dangerous-but-legal values warn at start.

- [x] **1b. Bound the `device` dimension.** Done as recommended — the
  `ops_owner` rollup **plus** a top-K of failing devices, both from the same
  complete scan, and both declared in configuration rather than chosen in the
  collector:

  ```toml
  [datasets.tacacs_activity.options]
  device_rollup = true   # ~12 series, joined through the NAD directory
  top_devices = 200      # worst failures first; 0 publishes every device
  ```

  Every device is still counted and still rolled up; `top_devices` chooses only
  how many get their own time series. The breakdown exports
  `ise3_topk_groups_returned` against `ise3_topk_groups_total` with
  `ise3_topk_truncated`, so a panel showing 200 of 5,000 says so, and
  `top_devices = 0` warns at start with the ~25,000 series it implies. This is
  what took the dataset from 27,501 series (1a alone, three families truncated)
  to 6,074 with nothing hidden.

**If per-device series are genuinely wanted**, `top_devices = 0` is now the
supported way to ask for them: no data lost, ~25k series budgeted against the
50k ceiling as a deliberate decision rather than an accident, and a startup
warning saying so.

## 2. `[x]` A label named `value` crashed publication

`FetchContext.set(self, family, value, **label_values)` collided with
`tacacs_activity`'s own `value=` label, raising `TypeError` for every row —
permanently, for any dataset that names a label `value`. Fixed by making both
publication signatures positional-only (`runtime.py`, `snapshots.py`), which
removes the whole class of collision rather than the one instance.

- [x] Regression tests, in `tests/test_v3_runtime.py`: one publishing a label
  named `value` (and `family`, the other name that could collide) through
  `FetchContext.set`, one doing the same through `Publication.set` directly,
  and one asserting `tacacs_activity` still names a label `value` — so if it
  ever stops, whoever removes it knows these tests became a museum piece rather
  than a guard.

**Still open, and it is a repository decision rather than a code one:** `tests/`
is untracked here, as in the v2 repository, so none of this suite is committed.
`git add -f tests/` if it should be.

## 3. `[x]` NAD classification took ~60 hours to converge

`network_devices` warmed 500 devices per run at a 6 h cadence, so it reached
**2,000 of 5,000 (coverage 0.400) after a full simulated day**, with ~36 h to
go — while the PAN budget sat at 1 % utilisation the entire time.

This was not cosmetic: `ops_owner` exists only in ERS groups, and an
unclassified device is skipped before the directory is built, so it cannot
attribute anything. Until the cache fills, every ops-owner breakdown in every
dashboard attributes most sessions to `unknown`. `tacacs_config` had the same
shape (100 accounts per run, 6 h) and reached 400 of 1,000.

- [x] **The warm-up cadence is budget-derived.** A collection now reports what
  its warm-up budget could not reach via `ctx.defer(...)`, the runner carries it
  out on `Outcome.deferred`, and `Scheduler._interval_after` revisits a filling
  cache at the rate the target's budget affords instead of at the dataset's own
  cadence. The moment nothing is outstanding it goes back to the cadence the
  data deserves.

  The derivation (`plan.warmup_interval`) is **what is left of the target's
  ceiling after the steady state everything else on it costs, divided between
  the caches actually filling** — not the whole ceiling, which is where this
  differs from the sketch above. That sketch assumed `network_devices` had all
  3,000 req/h to itself; it shares PAN with `tacacs_config`, and promising a
  rate the token bucket in item 4 would then refuse is worse than a slower rate
  both agree on. `plan` prints the cadence the scheduler will actually use, so
  the two can no longer disagree.

  Two corrections to the arithmetic fell out of building it, both in
  `Cost.requests_for` / `warmup_requests_for`: they dropped the per-1k component
  for converging providers entirely, so `network_devices` — which enumerates the
  whole NAD list at ERS's 100-row page cap on *every* collection, 50 requests at
  5,000 NADs — was declared at 0.8 req/h when it costs about 9, and its warm-up
  would have been paced ~10 % above the ceiling.

- [x] Stopgap not needed; `WARMUP_FETCHES_PER_CYCLE` is unchanged at 500.

**One thing the sketch above did not anticipate.** The budget is no longer the
binding constraint — the appliance is. A 500-device pass is 500 serialised ERS
detail requests, which the latency model puts at roughly 1,000 s of appliance
time, so the effective period is that plus the derived cadence rather than the
cadence alone. Full coverage lands in hours rather than the ~100 minutes the
sketch predicted, and no amount of scheduling improves it further: the exporter
cannot go faster than ISE answers. That is the correct place for the limit to
sit, and it is visible as `ise3_detail_cache_coverage` climbing steadily instead
of a budget going unspent.

## 4. `[x]` MnT cold start ran at 2.8× the declared budget

Warming the session-detail cache for 20,000 sessions costs 20,000 MnT requests,
concentrated into the first **108 minutes at roughly 11,300 requests/hour
against a 4,000/hour budget**. Once warm it settles to 2,512/h — 63 % of budget,
and within 4 % of the 2,424/h the plan declares, so the steady-state cost model
was sound. Only the cold start was un-budgeted, because REST budget was measured
rather than throttled.

- [x] **The budget is enforced.** A token bucket per REST target
  (`rate_limit.py`), sized from `budget.<target>.requests_per_hour` and taken in
  `RestTransport._request_locked` — after the auth guard, so a request that is
  about to be refused does not first consume an hour's allowance waiting for a
  slot it will not use. Debt goes negative deliberately, so ten simultaneous
  requests against an empty bucket take ten token-times rather than all being
  quoted the same wait and arriving together. `exporter.enforce_budget = false`
  turns it back into an observation, which is what that flag already meant for a
  plan over budget.

- [x] **The warm-up policy is explicit, and the plan states it as a decision.**
  `budget.<target>.warmup_requests_per_hour` declares what a target may burst to
  while a cache is filling. Left unset, a cold start runs at the steady ceiling
  and `plan` prints the real duration rather than the one the cadence implies:

      session_authorization (mnt): 24,012 req/h while warming, 2,412 once warm;
                                   full coverage in ~12.7h
          that rate exceeds the 1,576 req/h its budget leaves for warming, so
          the budget paces it, not the cadence. Declare
          budget.mnt.warmup_requests_per_hour to shorten it, or accept the time

  Declaring 12,000 takes the same cold start to **~2.1 h**, and `plan` prints
  "budget.mnt permits 12,000 req/h while any of them is filling — 3.0x the
  steady 4,000 req/h, for about 2.1h". That ~12.7 h figure is the ~12.5 h this
  item predicted, arrived at independently.

**Which value to declare is still blocked on a measurement, and deliberately
left unset.** The mechanism is built and the trade-off is printed; what nobody
knows yet is what the MnT API really costs, which the simulation assumes rather
than measures. Time `/Session/ActiveList` and `/Session/MACAddress/<mac>` on the
lab appliance, then re-run with `--latency-scale` set to the ratio and read the
duration `plan` gives. The shipped example leaves the line commented with that
note beside it — picking a burst rate from a modelled latency would be inventing
a number, not measuring one.

## 5. `[x]` `session_authorization` published 20,420 series

Past the 20,000-sample soft warning and 41 % of the hard
`limits.snapshot_samples` ceiling on its own, before any other dataset published
anything. 20,000 of them were `ise3_session_policy_set_endpoints_by_nad` — the
dataset's one deliberate cross product, policy set × NAD.

Not cosmetic: at 40,000 sessions it crossed the hard ceiling, and crossing it
raises `SnapshotError` — the dataset stops publishing entirely rather than
degrading.

- [x] **Bounded, not re-keyed.** The `ops_owner` rollup this item suggested
  already existed as `ise3_session_policy_set_endpoints`; what was missing was a
  bound on the per-switch view beside it. Re-keying would have deleted the
  answer to "which switches are still in open mode", which is a per-switch
  question with no useful coarser form — so the per-switch view stays and is
  bounded instead, exactly as item 1b did for TACACS devices, using the same
  mechanism rather than a second one:

  ```toml
  [datasets.session_authorization.options]
  policy_set_by_nad = true   # publish the per-switch view at all
  top_nads = 200             # switches per policy set, most endpoints first
  ```

  Ranked **within each policy set** rather than across all of them: a global
  top-K lets the busiest policy set crowd every other one out of an answer that
  is asked per set. Every session is still counted and still rolled up, so
  bounding this narrows what is published and never what is measured — and
  `ise3_topk_groups_returned` / `_total` / `ise3_topk_truncated` say which.
  Turning the view off entirely still publishes `0 of N` rather than nothing,
  because an absent coverage series and a complete one look identical.

**Measured after the change: no dataset crosses the 20,000-series soft warning,
and `ise3_session_policy_set_endpoints_by_nad` is out of the largest-families
list entirely.**

## 6. `[x]` Scrape and footprint

At the declared scale a scrape was **66,377 series / 6.4 MiB / 341 ms**, resident
memory ~313 MiB, and on-disk state 2.8 KiB (the no-history invariant holds).

**After items 5 and 6: 52,553 series / 5.1 MiB / 262 ms, 274.4 KiB on the wire,
resident memory 268 MiB.**

- [x] **6a. `/metrics` is compressed.** Real content negotiation in
  `server.py`, not a substring match: `gzip;q=0` is a client declining gzip and
  is answered uncompressed, `Vary: Accept-Encoding` is sent on both answers so a
  cache cannot serve one to the other, and a body that does not shrink is sent
  as it was. **Measured 18.9x, not the ~10x estimated here: 5.1 MiB becomes
  274.4 KiB on the wire.**
- [x] **6b. Compression happens outside the lock.** `generate_latest` holds
  `snapshot_lock` for its whole collection; the payload is produced first and
  compressed after, so the 16 ms of gzip is not added to the window that blocks
  publication. Asserted structurally in `tests/test_v3_server.py`, because it is
  the kind of property a later refactor silently reverses by nesting one call in
  the other.
- [x] **6c. The session-detail cache holds a projection.**
  `session_detail.project` keeps the thirteen fields its two readers use out of
  the dozens an MnT session record carries, resolves ISE's spelling variants
  once instead of on every read of every record on every cycle, and parses
  `other_attr_string` at the boundary so the raw string is never retained.
  Location is gone entirely — it was retained, parsed, and then discarded unread
  at the only place that asked for it, while `network_devices` already publishes
  it once per NAD for a dashboard to join against. **Measured 268 MiB resident
  against 313 MiB before, at full 20,000-session coverage in both.**

  The one thing the projection must not do is flatten "absent" into "false": an
  accounting-only record carries no verdict, and counting it as an authorization
  would dilute every ratio built from this cache. `has_verdict` is kept beside
  `passed`/`failed` for that reason and is tested directly.

Measured and rejected: streaming the 9.5 MiB ActiveList with `iterparse` +
`clear` instead of a DOM saves ~12 MiB of a 46 MiB transient peak while costing
25 % more CPU (243 ms → 301 ms). The retained records are the cost, not the
parse — which is what 6c acted on instead.

---

## Sequencing

Every item above is done and re-measured. What the sequencing said, and how it
held up:

1. ~~**1a + 1b** and **5** are contained~~ — both were, and **5** turned out to
   be cheaper than written because **1b** built the mechanism it needed first.
2. ~~**3 and 4** are one piece of work, not two~~ — correct, and the reason is
   worth keeping: the same `deferred` signal drives both, and either alone is
   wrong. Budget-derived scheduling without enforcement promises a rate nothing
   holds it to; enforcement without scheduling leaves a six-hourly dataset
   six-hourly while the budget goes unspent.
3. **4's** second half still waits on the lab latency measurement — the
   mechanism is built and `plan` prints the trade-off, but the value is left
   undeclared on purpose. See item 4.
4. ~~**6** is polish~~ — 6a and 6b were; 6c was not. It is 45 MiB of resident
   memory, and it removed a per-read parse that ran 20,000 times a cycle.

### What one 24-hour run at declared scale now reports

| | Before | After |
|---|---|---|
| Failed collections | 8 of 8 for `tacacs_activity` | **0 of 667** |
| Series / scrape | 66,377 / 6.4 MiB / 341 ms | **52,553 / 5.1 MiB / 262 ms** |
| On the wire | 6.4 MiB | **274.4 KiB** (18.9x) |
| Resident memory | ~313 MiB | **268 MiB** |
| NAD classification | 0.400 coverage after 24 h, ~36 h to go | **warm in 388 min** |
| TACACS accounts | 0.400 after 24 h | **warm in 390 min** |
| Session detail | warm in 108 min at 2.8x budget | **warm in 658 min, inside budget** |
| MnT load | 11,300 req/h peak vs 4,000 budget | **2,765 req/h measured, 69 % of budget** |
| Datasets over the soft series limit | `session_authorization` | **none** |

Every remaining truncation is a bound an operator declared and can see:
`session_authorization/policy_set_nad` and the three `tacacs_activity` device
breakdowns, each publishing returned-against-total.

## Found on the lab, not by the simulation

These came out of the first run against `laba-ise-001` (ISE 3.3 P11) on
2026-07-25. All three are fixed. None was reachable from the simulator: two
needed a real Oracle catalogue and the third needed a cold start against an
estate large enough for the wrong answer to be obvious.

- `[x]` **Schema discovery failed on a small estate.** `result_rows` derives from
  `[scale]`, which gave 1,005 on a 4-NAD lab against a 1,090-row Oracle data
  dictionary. The dictionary is fixed-size -- a property of the ISE release, not
  the fleet -- so every Data Connect dataset would have sat at `schema_pending`
  forever. Fixed with a `limits.catalog_rows` ceiling that deliberately does not
  scale, and pinned by three tests. The simulator could not find this: its
  synthetic catalogue is tiny, and the production profile's `result_rows` of
  7,000 is large enough to hide it.

- `[x]` **`plan` overstated a cold start below the warm-up batch size.**
  `warmup_requests_for` reported the per-cycle ceiling rather than
  `min(ceiling, fleet)`, so it claimed 24,012 req/h to warm 28 sessions.

- `[x]` **`nad_health` was blind to dead switches until `network_devices`
  warmed.** Silence is the signal there, so the dead-switch series come from the
  NAD directory rather than from activity -- and on a cold start `nad_health`
  runs before the directory has been filled. On the seeded lab it published 2
  series against 504 configured NADs and reported **zero silent switches**,
  which is not a degraded answer but a wrong one.

  Fixed by refusing the question instead of guessing at it. An empty directory
  now fails the collection with a new `dependency_pending` reason, which keeps
  the previous snapshot and names what is missing. The reason is deliberately
  **not** in `SLOW_RETRY_REASONS`, so the scheduler returns in a minute rather
  than fifteen and the first inventory collection unblocks it -- the ordering
  resolves itself without anyone tuning a cadence.

  Worth separating from item 3, which looks similar and is not: there, an
  unclassified NAD attributes to `ops_owner="unknown"`, which is a *true*
  statement about a session. Here there was no true statement available, so the
  only honest output is none.

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
