"""The request budget, enforced rather than observed.

`budget.<target>.requests_per_hour` used to be checked by the plan and exported
by the runtime, and nothing stopped the process from exceeding it. A cold start
did: warming 20,000 sessions of MnT detail ran at roughly 11,300 requests/hour
for its first 108 minutes against a 4,000/hour ceiling -- 2.8x the declared
number, absorbed silently by the appliance.

These tests pin what "enforced" has to mean: the long-run rate really is the
declared one, a burst is only as large as the declared allowance, several
callers queue rather than all being told the same wait, and an operator can
still turn the whole thing back into an observation.
"""
import threading

import pytest

from ise_exporter3.config import Config, ConfigError, TargetBudget
from ise_exporter3.rate_limit import BudgetLimiter, TokenBucket


class FakeClock:
    """A monotonic clock the test advances by hand."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _bucket(per_hour=3600.0, **kwargs):
    clock = FakeClock()
    return TokenBucket("pan", per_hour, clock=clock, **kwargs), clock


# --- the rate is the declared one -------------------------------------------

def test_a_request_inside_the_allowance_never_waits():
    bucket, _clock = _bucket()
    assert bucket.take() == 0.0


def test_the_bucket_starts_full_so_a_collection_is_not_paced_from_cold():
    # A collection arrives as a burst, and pacing the first one request-by-
    # request would stretch work the budget can perfectly well afford.
    bucket, _clock = _bucket(per_hour=3600.0)      # one per second, 60 in reserve
    assert all(bucket.take() == 0.0 for _ in range(60))


def test_beyond_the_allowance_a_request_waits_for_its_token():
    bucket, _clock = _bucket(per_hour=3600.0)
    for _ in range(60):
        bucket.take()
    assert bucket.take() == pytest.approx(1.0)


def test_callers_queue_behind_each_other_rather_than_sharing_one_wait():
    # The bug this shape avoids: ten callers each told "wait one second" all
    # arrive one second later and the rate is ten times what was declared.
    bucket, _clock = _bucket(per_hour=3600.0)
    for _ in range(60):
        bucket.take()
    waits = [bucket.take() for _ in range(5)]
    assert waits == [pytest.approx(w) for w in (1.0, 2.0, 3.0, 4.0, 5.0)]


def test_the_long_run_rate_is_the_declared_one():
    bucket, clock = _bucket(per_hour=3600.0)
    granted, waited = 0, 0.0
    for _ in range(500):
        wait = bucket.take()
        waited += wait
        clock.advance(wait)
        granted += 1
    # 500 requests at one per second, less the 60 the bucket held in reserve.
    assert waited == pytest.approx(granted - 60, abs=1.0)


def test_idle_time_does_not_bank_an_unbounded_burst():
    bucket, clock = _bucket(per_hour=3600.0)
    for _ in range(60):
        bucket.take()
    clock.advance(86400)          # a whole idle day
    # One minute of allowance, not one day of it.
    assert sum(1 for _ in range(60) if bucket.take() == 0.0) == 60
    assert bucket.take() > 0


def test_no_declared_ceiling_enforces_nothing():
    # `budget.<target>` already warns at start that this target is unbounded.
    # Inventing a limit the operator did not ask for would be worse.
    bucket, _clock = _bucket(per_hour=0.0)
    assert not bucket.enforcing
    assert all(bucket.take() == 0.0 for _ in range(1000))


# --- the declared warm-up burst ---------------------------------------------

def test_a_warmup_burst_applies_only_while_a_cache_is_filling():
    bucket, _clock = _bucket(per_hour=3600.0, warmup_requests_per_hour=36000.0)
    assert bucket.enforced_per_hour == 3600.0
    bucket.set_warming(True)
    assert bucket.enforced_per_hour == 36000.0
    bucket.set_warming(False)
    assert bucket.enforced_per_hour == 3600.0


def test_without_a_declared_burst_warming_changes_nothing():
    bucket, _clock = _bucket(per_hour=3600.0)
    bucket.set_warming(True)
    assert bucket.enforced_per_hour == 3600.0


def test_the_burst_really_is_faster():
    steady, steady_clock = _bucket(per_hour=3600.0)
    burst, burst_clock = _bucket(per_hour=3600.0, warmup_requests_per_hour=36000.0)
    burst.set_warming(True)

    def drain(bucket, clock, count):
        total = 0.0
        for _ in range(count):
            wait = bucket.take()
            total += wait
            clock.advance(wait)
        return total

    assert drain(burst, burst_clock, 600) < drain(steady, steady_clock, 600) / 5


def test_a_rate_change_neither_forfeits_nor_invents_allowance():
    bucket, clock = _bucket(per_hour=3600.0, warmup_requests_per_hour=36000.0)
    for _ in range(60):
        bucket.take()
    clock.advance(30)                 # 30 tokens earned at the steady rate
    bucket.set_warming(True)
    # Those 30 survive the change; the capacity simply grows with the rate.
    assert sum(1 for _ in range(30) if bucket.take() == 0.0) == 30


# --- the limiter around it ---------------------------------------------------

def test_enforcement_can_be_turned_back_into_an_observation():
    # `exporter.enforce_budget = false` already means "let me run over budget
    # deliberately" for a plan; it means the same thing here.
    limiter = BudgetLimiter(
        "pan", TargetBudget(target="pan", requests_per_hour=1.0), enforce=False)
    assert not limiter.enforcing
    assert all(limiter.acquire() == 0.0 for _ in range(100))


def test_a_waiting_request_stops_waiting_when_the_exporter_shuts_down():
    limiter = BudgetLimiter(
        "pan", TargetBudget(target="pan", requests_per_hour=1.0))
    shutdown = threading.Event()
    limiter.set_shutdown_event(shutdown)
    # Drain the reserve so the next request faces a very long wait.
    while limiter.bucket.take() == 0.0:
        pass
    shutdown.set()
    assert limiter.acquire() < 5.0, "a shutdown must not wait out an hour of debt"


def test_the_limiter_reads_the_budget_the_operator_declared():
    config = Config.from_document(
        {"targets": {"pan": {"host": "pan1", "user": "ro"},
                     "mnt": {"host": "mnt1"}},
         "budget": {"mnt": {"requests_per_hour": 4000,
                            "warmup_requests_per_hour": 12000}}},
        path="test.toml", environ={"ISE_PASS": "x"})
    limiter = BudgetLimiter("mnt", config.budget_for("mnt"))
    assert limiter.bucket.steady_per_hour == 4000
    assert limiter.bucket.warmup_per_hour == 12000


# --- what the configuration refuses -----------------------------------------

def _config(budget):
    return Config.from_document(
        {"targets": {"pan": {"host": "pan1", "user": "ro"}}, "budget": budget},
        path="test.toml", environ={"ISE_PASS": "x"})


def test_a_burst_below_the_steady_ceiling_is_refused():
    with pytest.raises(ConfigError, match="would slow a cold start"):
        _config({"mnt": {"requests_per_hour": 4000,
                         "warmup_requests_per_hour": 1000}})


def test_a_burst_without_a_ceiling_to_burst_above_is_refused():
    with pytest.raises(ConfigError, match="without a"):
        _config({"mnt": {"requests_per_hour": 0,
                         "warmup_requests_per_hour": 12000}})


def test_the_oracle_target_has_no_warmup_burst_because_it_has_no_cache():
    with pytest.raises(ConfigError, match="does not apply"):
        _config({"oracle": {"warmup_requests_per_hour": 100}})


def test_a_declared_burst_warns_at_start_with_what_it_permits():
    config = _config({"mnt": {"requests_per_hour": 4000,
                              "warmup_requests_per_hour": 12000}})
    assert any("12,000 requests/hour while a cache is still filling" in warning
               and "3.0x" in warning for warning in config.warnings), config.warnings
