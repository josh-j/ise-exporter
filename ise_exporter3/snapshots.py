"""Atomic publication boundary between collection and Prometheus scrapes.

The Prometheus client exposes each labelled metric as a mutable child map, and
the HTTP server collects them from another thread. One lock is shared by both
operations, so a scrape observes either the whole previous snapshot or the whole
new one -- never a half-written dataset, and never new values paired with stale
health.

This is v2's ``snapshots`` module with its thread-local staging replaced by an
explicit ``Publication`` the runner owns. v2 needed the thread-local because
collectors called the replacement helper from deep inside themselves; here the
runner opens the transaction and hands it to the provider, so the object can be
passed directly and the control flow is visible.
"""
from __future__ import annotations

import math
from threading import RLock

from prometheus_client import REGISTRY

from .labels import MAX_LABEL_BYTES


snapshot_lock = RLock()

# One dataset must never turn a bounded query into row-like Prometheus state.
#
# The real invariant is *what a series may be keyed on*, not a round number:
# NAD-keyed is legitimate (~5,000 at the declared scale, and dead-switch
# detection needs one series per switch), endpoint-keyed is not (~100,000, which
# is row storage by another name). So the ceiling is sized to "a fleet dimension
# times a small constant" with headroom, and the guard that actually matters is
# the test asserting no dataset keys on an endpoint identity.
#
# 20,000 was a round number, and at the declared scale nad_health landed on
# 20,002 -- a hard failure at exactly the size this exporter is built for.
MAX_SNAPSHOT_SAMPLES = 50_000
# Crossing this is not an error, but it is worth knowing about before the hard
# ceiling arrives: it means a dataset is growing with something it should not be.
SOFT_SAMPLE_WARNING = 20_000


class SnapshotError(ValueError):
    """A publication would have produced unsafe or unbounded metric state."""


class LockedCollectorRegistry:
    """Registry facade holding ``snapshot_lock`` for one complete scrape."""

    def __init__(self, registry=REGISTRY):
        self.registry = registry

    def collect(self):
        with snapshot_lock:
            yield from self.registry.collect()

    def restricted_registry(self, names):
        restricted = getattr(self.registry, "restricted_registry", None)
        if restricted is None:
            return self
        return LockedCollectorRegistry(restricted(names))


def count_samples(families):
    return sum(len(family._metrics) if hasattr(family, "_metrics") else 1
               for family in families)


def _validate_sample_count(families):
    total = count_samples(families)
    if total > MAX_SNAPSHOT_SAMPLES:
        raise SnapshotError(
            f"snapshot of {total} samples exceeds the hard "
            f"{MAX_SNAPSHOT_SAMPLES}-sample limit")
    return total


def _validate_finite(families):
    """Reject poisoned numeric samples before they cross the boundary."""
    for family in families:
        children = (family._metrics.values()
                    if hasattr(family, "_metrics") else (family,))
        for child in children:
            value = getattr(child, "_value", None)
            if isinstance(value, dict):      # Info payloads hold strings
                continue
            if hasattr(value, "get"):
                value = value.get()
            try:
                finite = math.isfinite(float(value))
            except (TypeError, ValueError, OverflowError) as error:
                raise SnapshotError(
                    f"snapshot value is invalid for {family._name}") from error
            if not finite:
                raise SnapshotError(f"snapshot value is invalid for {family._name}")


def _backup(families):
    saved = {}
    for family in families:
        if hasattr(family, "_metrics"):
            saved[family] = ("labelled", dict(family._metrics))
        elif hasattr(family, "info") and isinstance(
                getattr(family, "_value", None), dict):
            saved[family] = ("info", dict(family._value))
        elif hasattr(family, "_value"):
            saved[family] = ("scalar", family._value.get())
        else:
            raise SnapshotError(f"unsupported metric family {family!r}")
    return saved


def _restore(saved):
    for family, (kind, previous) in saved.items():
        if kind == "labelled":
            family._metrics.clear()
            family._metrics.update(previous)
        elif kind == "info":
            family.info(previous)
        else:
            family.set(previous)


class Publication:
    """Buffered writes for one dataset, applied atomically or not at all.

    Writes are recorded rather than applied so that a normalisation failure
    halfway through a result set rolls the whole dataset back to its previous
    snapshot, instead of leaving a partial one that reads as authoritative.
    """

    def __init__(self, families, *, provider=""):
        self.families = tuple(dict.fromkeys(families))
        self.provider = provider
        self.samples = 0
        self._writers = []

    def set(self, family, value, /, **label_values):
        """Record one gauge write. The provider label is bound automatically.

        Positional-only, so a label named ``value`` or ``family`` cannot collide
        with this signature.
        """
        if family not in self.families:
            raise SnapshotError(
                f"{getattr(family, '_name', family)!r} is not declared in this "
                "dataset's metrics; add it to Dataset.metrics")
        names = tuple(getattr(family, "_labelnames", ()))
        if "provider" in names and "provider" not in label_values:
            label_values = {**label_values, "provider": self.provider}
        for key, text in label_values.items():
            if len(str(text).encode("utf-8")) > MAX_LABEL_BYTES:
                raise SnapshotError(
                    f"label {key} for {family._name} exceeds "
                    f"{MAX_LABEL_BYTES} bytes; bound it with labels.label()")
        numeric = float(value)
        if not math.isfinite(numeric):
            raise SnapshotError(f"{family._name} value is not finite")
        self._writers.append(
            lambda: (family.labels(**label_values).set(numeric) if names
                     else family.set(numeric)))

    def commit(self, extra_families=(), extra_writers=()):
        """Replace this dataset's families and its health metadata as one unit."""
        extra_families = tuple(dict.fromkeys(extra_families))
        everything = tuple(dict.fromkeys((*self.families, *extra_families)))
        with snapshot_lock:
            saved = _backup(everything)
            try:
                for family in self.families:
                    kind, _previous = saved[family]
                    if kind == "labelled":
                        family._metrics.clear()
                    elif kind == "info":
                        family.info({})
                    else:
                        family.set(0)
                for writer in self._writers:
                    writer()
                for writer in extra_writers:
                    writer()
                self.samples = _validate_sample_count(self.families)
                _validate_finite(everything)
            except Exception:
                _restore(saved)
                raise

    def discard(self, extra_families=(), extra_writers=()):
        """Leave the previous snapshot in place, publishing only health writes.

        A failed collection must not be represented as a valid empty snapshot,
        so the data families are untouched and only the failure metadata moves.
        """
        extra_families = tuple(dict.fromkeys(extra_families))
        with snapshot_lock:
            saved = _backup(extra_families)
            try:
                for writer in extra_writers:
                    writer()
                _validate_finite(extra_families)
            except Exception:
                _restore(saved)
                raise
