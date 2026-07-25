"""Token-bucket enforcement of a declared per-target request budget.

Until this existed, `budget.<target>.requests_per_hour` was **measured, not
throttled**: the plan checked that the steady state fitted, the runtime exported
planned against measured, and nothing stopped the process from exceeding it. The
gap that made visible is a cold start. Warming the session-detail cache for
20,000 sessions costs 20,000 MnT requests, and at a five-minute cadence with a
2,000-per-cycle warm-up ceiling that lands as roughly **11,300 requests/hour for
the first 108 minutes against a 4,000/hour budget** -- 2.8x the number the
operator declared, absorbed silently by the appliance.

A declared ceiling that the process can exceed is not a budget, it is a comment.
So this is where a request waits.

Two rates, because a cold start and a steady state are different decisions:

    requests_per_hour          what this target costs once every cache is warm
    warmup_requests_per_hour   what it may burst to while one is still filling

The second is optional and off by default. Declaring it is how an operator says
"yes, spend 11,000/hour for two hours to get coverage sooner" as a decision that
`plan` prints, rather than as something discovered afterwards in an appliance
graph. Without it, warm-up runs at the steady ceiling and simply takes longer --
which is visible as `ise3_detail_fetches_deferred` staying above zero for more
cycles, not as a number quietly going wrong.

The bucket lets debt go negative on purpose. A caller that arrives with an empty
bucket is charged its token immediately and told how long to wait for it, so the
next caller queues *behind* that wait rather than being quoted the same one. Ten
simultaneous requests against an empty bucket therefore take ten token-times in
total, not one.
"""
from __future__ import annotations

import threading
import time

from . import telemetry


# How much of the hourly rate the bucket may hold in reserve. A collection tends
# to arrive as a burst of requests rather than a smooth trickle, and pacing every
# request individually would stretch a warm-up pass that the budget can perfectly
# well afford. One minute of allowance smooths that without letting a long idle
# period bank an hour of requests to spend at once.
CAPACITY_SECONDS = 60.0

# Never sleep longer than this in one go, so a shutdown is noticed promptly even
# when a request is waiting out a large debt.
WAIT_SLICE_SECONDS = 1.0


class TokenBucket:
    """One target's request budget, enforced rather than observed.

    Thread-safe: several datasets share a target's lane serially, but the
    transport is also reachable from the operator API's health probes.
    """

    def __init__(self, target, requests_per_hour, *,
                 warmup_requests_per_hour=0.0, capacity_seconds=CAPACITY_SECONDS,
                 clock=None):
        # Resolved through the module attribute rather than captured as a
        # default, so the scale simulator can substitute its virtual clock the
        # same way it already does for detail_cache and the Data Connect
        # transport. Bound at construction, a real monotonic clock inside a
        # simulated timeline would throttle a modelled day to a real one.
        clock = clock or time.monotonic
        self.target = target
        self.steady_per_hour = max(0.0, float(requests_per_hour or 0.0))
        self.warmup_per_hour = max(0.0, float(warmup_requests_per_hour or 0.0))
        self._capacity_seconds = float(capacity_seconds)
        self._clock = clock
        self._warming = False
        self._lock = threading.Lock()
        self._updated = clock()
        self._tokens = self._capacity()
        self._publish()

    # --- rate ------------------------------------------------------------

    @property
    def enforced_per_hour(self):
        """The ceiling in force right now, in requests per hour."""
        if self._warming and self.warmup_per_hour:
            return self.warmup_per_hour
        return self.steady_per_hour

    @property
    def enforcing(self):
        """False when no ceiling was declared, and nothing is enforced."""
        return self.enforced_per_hour > 0

    def _rate(self):
        return self.enforced_per_hour / 3600.0

    def _capacity(self):
        return max(1.0, self._rate() * self._capacity_seconds)

    def set_warming(self, warming):
        """Say whether a cache on this target is still filling.

        Only meaningful when a warm-up burst was declared; without one the two
        rates are the same and this changes nothing.
        """
        warming = bool(warming)
        with self._lock:
            if warming == self._warming:
                return
            # Settle the tokens earned at the old rate before changing it, so a
            # rate change neither forfeits nor invents allowance.
            self._refill(self._clock())
            self._warming = warming
            self._tokens = min(self._tokens, self._capacity())
        self._publish()

    # --- accounting -------------------------------------------------------

    def _refill(self, now):
        elapsed = max(0.0, now - self._updated)
        self._updated = now
        self._tokens = min(self._capacity(), self._tokens + elapsed * self._rate())

    def take(self):
        """Charge one request and return how long it must wait first."""
        with self._lock:
            rate = self._rate()
            if rate <= 0:
                # No declared ceiling. `budget.<target>` already warned at start
                # that load on this target is unbounded; do not invent a limit
                # the operator did not ask for.
                return 0.0
            now = self._clock()
            self._refill(now)
            self._tokens -= 1.0
            if self._tokens >= 0:
                return 0.0
            return -self._tokens / rate

    def _publish(self):
        telemetry.budget_enforced_requests_per_hour.labels(
            target=self.target).set(self.enforced_per_hour)


class BudgetLimiter:
    """A bucket plus the waiting, so a transport has one thing to call."""

    def __init__(self, target, budget, *, enforce=True, clock=None):
        self.target = target
        self.enforce = bool(enforce)
        self.bucket = TokenBucket(
            target, budget.requests_per_hour,
            warmup_requests_per_hour=getattr(
                budget, "warmup_requests_per_hour", 0.0),
            clock=clock)
        self._shutdown = None

    def set_shutdown_event(self, shutdown):
        self._shutdown = shutdown

    def set_warming(self, warming):
        self.bucket.set_warming(warming)

    @property
    def enforcing(self):
        return self.enforce and self.bucket.enforcing

    def acquire(self, api="unknown"):
        """Block until this request fits the budget. Returns seconds waited.

        The whole wait is exported: a budget that is actually biting should be
        visible as time, not inferred from a rate that looks suspiciously round.
        """
        if not self.enforce:
            return 0.0
        wait = self.bucket.take()
        if wait <= 0:
            return 0.0
        telemetry.budget_throttled_total.labels(
            target=self.target, api=api).inc()
        waited = self._sleep(wait)
        telemetry.budget_wait_seconds_total.labels(target=self.target).inc(waited)
        return waited

    def _sleep(self, wait):
        """Wait out a budget debt, interruptibly when there is a shutdown.

        Sliced only when something can interrupt it. Without a shutdown event
        there is nothing to re-check, and slicing an hour into a thousand
        one-second naps would just be a thousand naps -- which matters under the
        scale simulator's virtual clock, where each one is a loop iteration
        rather than a second.
        """
        if self._shutdown is None:
            time.sleep(wait)
            return wait
        remaining, waited = wait, 0.0
        while remaining > 0:
            if self._shutdown.is_set():
                break
            slice_seconds = min(WAIT_SLICE_SECONDS, remaining)
            interrupted = self._shutdown.wait(slice_seconds)
            remaining -= slice_seconds
            waited += slice_seconds
            if interrupted:
                break
        return waited
