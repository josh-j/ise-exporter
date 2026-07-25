"""RADIUS latency normalisation.

Latency is the least trustworthy number ISE reports. On 3.3.0.430 Patch 11 the
values that reach `RESPONSE_TIME` and the MnT per-step timings are not uniformly
measurements: some flows never populate them, some report zero for an exchange
that plainly took time, and failed authentications are timed on a different code
path from passed ones. Averaging that mixture produces a plausible number that is
wrong, and a plausible wrong number is worse than a gap.

So latency is treated as untrusted input:

- every sample is classified before use, and the classification is exported, so
  "the average dropped" and "ISE stopped reporting latency" are distinguishable;
- zero means *not measured*, never *instantaneous*. A zero admitted into a mean
  drags it toward zero silently, which is the failure mode that makes a latency
  panel quietly useless;
- passed and failed latency are never mixed, because they measure different code
  paths and their mixture moves with the failure rate rather than with latency;
- the exporter never averages ISE's own averages. A mean of means is only correct
  when the groups are equal in size, and RADIUS groups never are -- use ISE's
  aggregate view, or publish a histogram, but do not compute it here.

Nothing here invents a measurement. A sample that cannot be trusted is dropped
and counted, not repaired.
"""
from __future__ import annotations

import math

from prometheus_client import Counter


# A RADIUS exchange slower than this is not a latency measurement -- it is a
# timeout, a clock problem, or a field that does not mean what it appears to.
MAX_PLAUSIBLE_SECONDS = 120.0

# Below this, a reported duration is indistinguishable from an unpopulated
# field. ISE reports whole milliseconds, so anything under one is not a reading.
MIN_PLAUSIBLE_SECONDS = 0.001

QUALITIES = (
    "ok",             # a usable measurement
    "missing",        # the field was absent or null
    "non_numeric",    # present but not a number
    "not_measured",   # zero: ISE populated the field without timing anything
    "negative",       # a negative duration; a clock or a field-meaning problem
    "implausible",    # beyond the plausibility ceiling
)

samples_total = Counter(
    "ise3_radius_latency_samples_total",
    "RADIUS latency samples by result and how trustworthy each one was",
    ["provider", "result", "quality"])


def normalize(value, *, unit="ms"):
    """Return ``(seconds, quality)``; ``seconds`` is None unless quality is ok.

    ``unit`` is the unit ISE reported in -- ``RESPONSE_TIME`` is milliseconds,
    the MnT step timings are milliseconds, and anything already in seconds
    should say so rather than being pre-scaled by the caller.
    """
    if value is None or value == "":
        return None, "missing"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None, "non_numeric"
    if not math.isfinite(number):
        return None, "non_numeric"

    seconds = number / 1000.0 if unit == "ms" else number
    if seconds < 0:
        return None, "negative"
    if seconds == 0:
        # Deliberately not "ok, zero seconds". An unpopulated field and a
        # genuinely instant exchange are indistinguishable here, and admitting
        # the former into an average is how a latency panel goes quietly wrong.
        return None, "not_measured"
    if seconds < MIN_PLAUSIBLE_SECONDS or seconds > MAX_PLAUSIBLE_SECONDS:
        return None, "implausible"
    return seconds, "ok"


def observe(ctx, value, *, result, unit="ms"):
    """Normalise one sample, count its quality, and return usable seconds or None.

    ``result`` separates passed from failed: they are timed differently and must
    never share a series.
    """
    seconds, quality = normalize(value, unit=unit)
    samples_total.labels(
        provider=ctx.provider.name, result=result, quality=quality).inc()
    return seconds


class LatencyAccumulator:
    """Collects trustworthy samples for one series and reports its own coverage.

    Coverage is published alongside the value on purpose. A mean over three
    samples out of four thousand is not wrong, but it is not the fleet either,
    and the operator needs both numbers to know which they are looking at.
    """

    def __init__(self):
        self.total = 0.0
        self.usable = 0
        self.seen = 0

    def add(self, seconds):
        self.seen += 1
        if seconds is None:
            return
        self.usable += 1
        self.total += seconds

    @property
    def mean(self):
        return self.total / self.usable if self.usable else None

    @property
    def coverage(self):
        """Fraction of samples that were usable; None when nothing was seen."""
        return self.usable / self.seen if self.seen else None
