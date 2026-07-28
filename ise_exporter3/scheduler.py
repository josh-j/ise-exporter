"""Plan executor, including runtime provider failover.

The plan decided what runs, from which source, and how often. This turns that
into collections: it tracks when each dataset is next due, submits it to its
target's lane, and reschedules from completion.

It also owns the one decision the offline plan cannot make -- what to do when the
chosen source stops answering. After a few consecutive failures a dataset steps
to the next source its operator declared, and every step is exported. While
degraded it sends one canary to its preferred source periodically, and returns
the moment that source answers again.

Two rules keep this from becoming v1's silent fallback:

- A step never happens quietly. ``provider_active``, ``provider_degraded`` and
  ``provider_reason_info`` move together with the data, so a dashboard can gate
  on which source is live and a human can see when it changed.
- ``provider`` is a label on the data itself, so samples from two sources with
  different meanings never merge into one series.

It holds no dataset knowledge. There is no priority table, no per-dataset call
list and no persisted-metrics map -- those were five parallel tables in v2 that
had to be edited in lockstep.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from functools import partial

from . import known_defects, reporting, telemetry
from .labels import bounded_detail
from .lanes import LaneSet
from .plan import warmup_interval
from .runtime import Runner, forget_provider_health
from .snapshots import snapshot_lock
from .telemetry import _reason_slug
from .transports import FAILURE_EXPLANATIONS


logger = logging.getLogger(__name__)

# How often the loop looks for due datasets. Cheap (a scan of ~20 entries) and
# small enough that a five-minute cadence is honoured to the second.
TICK_SECONDS = 5

FAST_RETRY_SECONDS = 60
BACKOFF_SECONDS = 900
MAX_CONSECUTIVE_FAILURES = 5

# Step to the next source before the protected backoff engages: when an
# alternative exists, trying it beats waiting fifteen minutes on a dead one.
PROVIDER_FAILOVER_THRESHOLD = 3
# While degraded, retry the preferred source this often. Long enough that a
# genuinely down source is not probed constantly, short enough that recovery is
# noticed within one slow dataset's cadence.
PROVIDER_RECHECK_SECONDS = 900

# Failures the exporter must not hammer through: retrying fast against an
# account guard or missing safety state makes the situation worse.
SLOW_RETRY_REASONS = frozenset({
    "authentication_backoff", "authentication_failed", "authorization_failed",
    "state_unavailable",
})

# Reasons that mean "not yet", not "broken". A dataset waiting on shared state
# another dataset fills, or on schema discovery, has not failed at anything --
# it asked a question that cannot be answered yet, and the answer arrives on
# someone else's schedule rather than after a repair.
#
# They are exempt from two things, not one: the consecutive-failure backoff
# below, and the failover counter. A source that cannot answer yet is not
# evidence that the source is bad, and stepping away from it on that evidence
# lands on a provider that means something coarser -- for as long as the
# recovery probe cadence, which is usually longer than the wait itself.
#
# These must never escalate into the consecutive-failure backoff. They did, and
# it was caught in production: nad_health refused five times at one-minute
# intervals while the NAD inventory warmed, hit MAX_CONSECUTIVE_FAILURES, and
# was then pushed out to a six-hour retry -- reinstating exactly the blind
# window the refusal existed to close, only now with an honest label on it.
# Five minutes of waiting turned into six hours of waiting.
PENDING_REASONS = frozenset(
    {"baseline_pending", "dependency_pending", "schema_pending"})


@dataclass
class DatasetState:
    """One dataset's live provider selection."""

    entry: object
    candidates: tuple           # providers in declared preference order
    unusable: tuple = ()        # declared, but not runnable in this build
    active: int = 0
    failures: int = 0
    last_probe: float = 0.0
    reason: str = ""
    history: list = field(default_factory=list)

    @property
    def name(self):
        return self.entry.name

    @property
    def interval(self):
        return self.entry.interval

    @property
    def provider(self):
        return self.candidates[self.active]

    @property
    def target(self):
        return self.provider.target

    @property
    def degraded(self):
        return self.active > 0

    @property
    def preferred(self):
        return self.candidates[0]


class Scheduler:
    def __init__(self, config, plan, transports, *, asynchronous=True,
                 clock=time.time):
        self.config = config
        self.plan = plan
        self.transports = transports
        # One clock for all scheduling decisions. Mixing an injected time with
        # a real one in the completion path makes cadence untestable and hides
        # the wall-clock assumptions this class makes.
        self.clock = clock
        self.runner = Runner(config)
        self.next_run = {}
        self.last_success = {}
        # What each dataset's last successful collection left for the next one,
        # as (target, count). A cache with work outstanding is revisited at the
        # rate the budget affords rather than at its own cadence, and its target
        # is told so it can allow a declared warm-up burst.
        self.deferred = {}
        self.states = self._resolve_states()
        self.lanes = LaneSet(
            sorted({provider.target
                    for state in self.states
                    for provider in state.candidates}),
            asynchronous=asynchronous)

    # --- resolution -------------------------------------------------------

    def _usable(self, provider):
        """A provider this build can actually run, and why not if it cannot."""
        if provider.fetch is None:
            return False, "not_implemented", (
                f"the {provider.name} provider is not built in this release")
        if provider.target not in self.transports:
            return False, "not_configured", (
                f"no {provider.target} transport is available")
        return True, "", ""

    def _resolve_states(self):
        """Keep every usable source per dataset, in the operator's order."""
        states = []
        for entry in self.plan.enabled:
            if not entry.resolved:
                continue
            ordered = (entry.provider,) + tuple(entry.alternatives)
            candidates, rejected = [], []
            for provider in ordered:
                usable, reason, detail = self._usable(provider)
                if usable:
                    candidates.append(provider)
                else:
                    rejected.append((provider, reason, detail))
            if not candidates:
                provider, reason, detail = rejected[0]
                self._publish_unavailable(entry, provider.name, reason, detail)
                continue
            for provider, reason, detail in rejected:
                logger.info(
                    "provider unusable dataset=%s provider=%s reason=%s detail=%s",
                    entry.name, provider.name, reason, detail)
            states.append(DatasetState(
                entry=entry, candidates=tuple(candidates),
                unusable=tuple(provider.name for provider, _r, _d in rejected)))
        return tuple(states)

    def _publish_unavailable(self, entry, provider, reason, detail):
        bounded = bounded_detail(detail or FAILURE_EXPLANATIONS.get(reason, reason))
        telemetry.dataset_up.labels(dataset=entry.name, provider=provider).set(0)
        telemetry.dataset_fresh.labels(dataset=entry.name, provider=provider).set(0)
        telemetry.dataset_last_failure_info.labels(
            dataset=entry.name, provider=provider, reason=reason).set(1)
        telemetry.dataset_last_failure_detail_info.labels(
            dataset=entry.name, provider=provider, reason=reason,
            detail=bounded).set(1)
        logger.warning(
            "dataset unavailable dataset=%s provider=%s reason=%s detail=%s",
            entry.name, provider, reason, bounded)

    # --- provider selection ----------------------------------------------

    def _publish_selection(self, state):
        # Re-asserted rather than set once: the plan publishes what configuration
        # allows, and this publishes what this build can actually run. The second
        # answer must win, and must survive a later republication of the plan.
        for name in state.unusable:
            telemetry.dataset_provider_available.labels(
                dataset=state.name, provider=name).set(0)
            telemetry.dataset_provider_active.labels(
                dataset=state.name, provider=name).set(0)
        for index, provider in enumerate(state.candidates):
            telemetry.dataset_provider_active.labels(
                dataset=state.name, provider=provider.name).set(
                    int(index == state.active))
            telemetry.dataset_provider_available.labels(
                dataset=state.name, provider=provider.name).set(1)
        telemetry.dataset_provider_degraded.labels(
            dataset=state.name).set(int(state.degraded))
        if state.reason:
            telemetry.dataset_provider_reason_info.labels(
                dataset=state.name, provider=state.provider.name,
                reason=_reason_slug(state.reason)).set(1)

    def _switch_to(self, state, index, reason):
        previous = state.provider.name
        # One unit, so a scrape cannot observe the half-switched state.
        with snapshot_lock:
            # Clear the old selection's data series: two sources for one dataset
            # do not mean the same thing, so a stale sample under the previous
            # provider label would keep answering a query that is now about
            # something else.
            for family in state.entry.dataset.metrics:
                self._forget_provider(family, previous)
            # Coverage carries no provider label and the incoming source may not
            # publish it at all, so it is dropped rather than relabelled.
            reporting.forget_coverage(state.name)
            # And its health, which is the same lie in the other direction: left
            # alone, the departing source keeps asserting fresh=1 over a snapshot
            # this method just deleted, and a provider abandoned mid-chain is
            # never written again at all.
            forget_provider_health(
                state.name, previous,
                extra_families=(telemetry.dataset_next_run_timestamp,
                                telemetry.dataset_last_attempt_timestamp))
            if state.reason:
                telemetry.dataset_provider_reason_info.remove(
                    state.name, previous, _reason_slug(state.reason))
            state.active = index
        state.failures = 0
        state.reason = reason
        # Freshness was earned by the source we just left. The incoming one
        # publishes fresh=0 until it has a success of its own.
        self.last_success.pop(state.name, None)
        # Start the recovery clock now. Left at zero, a dataset would probe the
        # source it just escaped on its very next collection, so it would spend
        # every other cycle failing against a source already known to be down.
        state.last_probe = self.clock()
        # The failure run belonged to the source we just left.
        self.runner.reset_failures(state.name)
        self._publish_selection(state)
        logger.warning(
            "provider changed dataset=%s from=%s to=%s reason=%s degraded=%s",
            state.name, previous, state.provider.name, reason, state.degraded)

    @staticmethod
    def _forget_provider(family, provider):
        """Drop one provider's children from a metric family."""
        names = tuple(getattr(family, "_labelnames", ()))
        if "provider" not in names:
            return
        position = names.index("provider")
        for labels in [key for key in getattr(family, "_metrics", {})
                       if key[position] == provider]:
            family._metrics.pop(labels, None)

    def _choose(self, state, now):
        """Return the provider to use now, and whether it is a recovery canary."""
        if not state.degraded:
            return state.provider, False
        if now - state.last_probe >= PROVIDER_RECHECK_SECONDS:
            return state.preferred, True
        return state.provider, False

    # --- scheduling -------------------------------------------------------

    def bootstrap(self, now=None):
        """Publish identity and the plan, and make every dataset due."""
        from . import SUPPORTED_ISE_RELEASE, __version__

        now = self.clock() if now is None else now
        telemetry.exporter_build_info.labels(
            version=__version__, target_ise_release=SUPPORTED_ISE_RELEASE).set(1)
        telemetry.publish_plan(self.plan)
        self.publish_state_size()
        known_defects.publish()
        for state in self.states:
            self._publish_selection(state)
            self._set_next_run(state, now)
        # Zero rather than absent: "no cache is filling" and "nobody has looked"
        # are different facts, and only one of them is reassuring.
        for target in self.lanes.lanes:
            self._publish_warming(target)
        logger.info(
            "collecting %d dataset(s) across %d target(s); %d unresolved, "
            "%d with a declared fallback",
            len(self.states), len(self.lanes.lanes), len(self.plan.unresolved),
            sum(1 for state in self.states if len(state.candidates) > 1))

    def publish_state_size(self):
        """Report what the exporter keeps on disk.

        It should be the pacing gate and the authentication guards -- hundreds
        of bytes. Prometheus already stores time series properly; an exporter
        that accumulates history beside it is building a worse second one, and
        v2's restart-persistent snapshots grew to a documented 256 MiB ceiling.
        """
        import os

        directory = os.path.dirname(
            os.path.abspath(os.path.expanduser(self.config.exporter.state_db)))
        total = 0
        for root, _dirs, files in os.walk(directory):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    continue
        telemetry.exporter_state_bytes.set(total)
        return total

    def _set_next_run(self, state, when):
        self.next_run[state.name] = when
        telemetry.dataset_next_run_timestamp.labels(
            dataset=state.name, provider=state.provider.name).set(when)

    # --- warm-up ----------------------------------------------------------

    def _interval_after(self, state, entry, outcome):
        """How long until this dataset runs again.

        Its own cadence, unless its cache is still filling -- then the cadence
        the budget affords, which is what turns "5,000 NADs in sixty hours" into
        hours instead. The moment nothing is outstanding it goes back to the
        cadence the data actually deserves.
        """
        if outcome.deferred <= 0:
            return state.interval
        warmup = warmup_interval(self.plan, entry)
        if not warmup or warmup >= state.interval:
            return state.interval
        logger.info(
            "warm-up pacing dataset=%s provider=%s deferred=%d next_in=%ds "
            "(cadence %ds)", state.name, entry.provider.name, outcome.deferred,
            warmup, state.interval)
        return warmup

    def _record_deferred(self, state, target, outcome):
        """Track outstanding warm-up work, and tell the target about it."""
        self.deferred[state.name] = (target, max(0, int(outcome.deferred)))
        self._publish_warming(target)

    def _publish_warming(self, target):
        warming = any(count > 0 for other, count in self.deferred.values()
                      if other == target)
        telemetry.budget_warming.labels(target=target).set(int(warming))
        setter = getattr(self.transports.get(target), "set_warming", None)
        if setter is not None:
            setter(warming)

    def tick(self, now=None):
        """Submit every dataset that is due. Returns the names submitted."""
        now = self.clock() if now is None else now
        submitted = []
        for state in self.states:
            if now < self.next_run.get(state.name, 0):
                continue
            # Reserve the next slot before running: a collection slower than its
            # own cadence must not re-arm on every tick and queue without bound.
            self._set_next_run(state, now + state.interval)
            # Decided here, from the same clock reading as the due check, so the
            # work is submitted to the lane of the persona it will actually talk
            # to. Choosing inside the worker ran a recovery probe against one
            # target from another target's lane.
            provider, canary = self._choose(state, now)
            if self.lanes.submit(provider.target, state.name,
                                 partial(self._collect, state, provider, canary)):
                submitted.append(state.name)
        self.refresh_freshness(now)
        return submitted

    def _collect(self, state, provider=None, canary=False):
        if provider is None:
            provider, canary = self._choose(state, self.clock())
        if canary:
            return self._probe(state, provider)

        entry = replace(state.entry, provider=provider)
        outcome = self.runner.run(entry, self.transports[provider.target])
        completed = self.clock()

        if outcome.ok:
            state.failures = 0
            self.last_success[state.name] = completed
            self._record_deferred(state, provider.target, outcome)
            self._set_next_run(
                state, completed + self._interval_after(state, entry, outcome))
        else:
            # A pending reason is not a strike against the source: it is the
            # answer "not yet" from a source that is answering. Counting it both
            # steps away from a working provider and leaves the counter armed
            # for the next real failure.
            if outcome.reason not in PENDING_REASONS:
                state.failures += 1
            if (outcome.reason not in PENDING_REASONS
                    and state.failures >= PROVIDER_FAILOVER_THRESHOLD
                    and state.active + 1 < len(state.candidates)):
                self._switch_to(state, state.active + 1, outcome.reason)
                # Try the new source now rather than waiting out a backoff that
                # was earned by a different source.
                self._set_next_run(state, completed)
            else:
                self._set_next_run(
                    state, completed + self._retry_delay(state, outcome))
        self.refresh_freshness(completed)
        return outcome

    def _probe(self, state, provider):
        """Try the preferred source once; the active one still collects.

        The probe is additive, not a replacement for the cycle. As a replacement
        it starved any degraded dataset whose cadence is longer than the recheck
        window: endpoint_inventory at six hours probed a dead source on every
        single cycle and collected from its fallback exactly once, at the moment
        of failover, while dataset_up and the fallback's gauges said otherwise.
        """
        entry = replace(state.entry, provider=provider)
        outcome = self.runner.run(entry, self.transports[provider.target])
        completed = self.clock()
        state.last_probe = completed
        if outcome.ok:
            # The preferred source answered again. Return to it rather than
            # staying on a fallback that means something slightly different.
            self._switch_to(state, 0, "preferred_provider_recovered")
            self.last_success[state.name] = completed
            self._record_deferred(state, provider.target, outcome)
            self._set_next_run(
                state, completed + self._interval_after(state, entry, outcome))
            self.refresh_freshness(completed)
            return outcome

        logger.info(
            "recovery probe failed dataset=%s provider=%s reason=%s; "
            "staying on %s", state.name, provider.name, outcome.reason,
            state.provider.name)
        # The failure belonged to the preferred source, so it does not touch
        # state.failures. The active source still owes this cycle its data, and
        # it must produce it on its own target's lane rather than on this one.
        active = state.provider
        if active.target == provider.target:
            return self._collect(state, active, False)
        self.lanes.submit(active.target, state.name,
                          partial(self._collect, state, active, False))
        return outcome

    def _retry_delay(self, state, outcome):
        """Retry soon after a transient failure, slowly after a systemic one."""
        if outcome.reason in SLOW_RETRY_REASONS:
            return max(state.interval, BACKOFF_SECONDS)
        # A pending dependency is not a failure to back off from. Escalating it
        # would make the wait longer the longer it has already waited, which is
        # precisely backwards: the thing it is waiting for is on its way.
        if outcome.reason in PENDING_REASONS:
            return min(state.interval, FAST_RETRY_SECONDS)
        if self.runner.consecutive_failures(state.name) >= MAX_CONSECUTIVE_FAILURES:
            return max(state.interval, BACKOFF_SECONDS)
        return min(state.interval, FAST_RETRY_SECONDS)

    def refresh_freshness(self, now=None):
        """A snapshot older than two cadences is no longer fresh."""
        now = self.clock() if now is None else now
        for state in self.states:
            success = self.last_success.get(state.name)
            fresh = success is not None and (now - success) <= 2 * state.interval
            telemetry.dataset_fresh.labels(
                dataset=state.name, provider=state.provider.name).set(int(fresh))

    def loop(self, shutdown):
        """Collect until ``shutdown`` is set."""
        self.bootstrap()
        # Handed over before any work starts, not only during teardown. A
        # transport that learns about shutdown only once the loop has exited
        # cannot interrupt a wait that began before then -- and waits are long
        # now: a Data Connect cooldown at a low duty cycle, or a request holding
        # for its share of an enforced request budget.
        self._share_shutdown(shutdown)
        self.lanes.start()
        try:
            while not shutdown.is_set():
                try:
                    self.tick()
                except Exception:       # noqa: BLE001 - the loop must survive
                    logger.exception("scheduler tick failed")
                shutdown.wait(TICK_SECONDS)
        finally:
            # Also cancel on an unexpected exit so in-flight work stops rather
            # than racing transport teardown.
            shutdown.set()
            self._share_shutdown(shutdown)
            self.lanes.stop()

    def _share_shutdown(self, shutdown):
        for transport in self.transports.values():
            setter = getattr(transport, "set_shutdown_event", None)
            if setter is not None:
                setter(shutdown)
