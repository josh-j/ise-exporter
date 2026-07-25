"""The declarative core: what a dataset is, who can supply it, what that costs.

Everything else derives from three types. A ``Dataset`` names the metrics it owns
and the ``Provider`` objects that can supply them, in preference order. A provider
names the target persona it talks to and declares the ``Cost`` of one collection.
``Cost.load()`` turns (cost, interval, fleet scale) into hourly ``Load`` on that
target, which is what the budget is checked against.

Cost declarations are estimates written by hand -- the one part of this design
that can quietly lie. The runtime therefore exports planned against measured load
per target, and drift between them is the signal that a declaration is wrong.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Optional


# The ISE personas the exporter can put load on. A provider talks to exactly one.
TARGETS = ("pan", "mnt", "oracle", "pxgrid")

# Fleet dimensions a cost may scale with. Declared once in config's [scale] so a
# plan is predictive for the deployment it will actually run against, rather than
# for whatever size the author had in mind.
SCALE_DIMENSIONS = ("nads", "endpoints", "sessions", "accounts")


class ModelError(ValueError):
    """A dataset, provider, or cost declaration is malformed."""


# How much of the fleet a provider actually measures. This is a declaration, not
# a hope: a per-cycle request budget silently becoming a coverage ceiling is the
# single easiest way to ship a metric that looks like a fleet number and is not.
#
#   complete    every entity, every collection -- an aggregate, or a small set
#   converging  cached per-entity detail; reaches complete over several cycles
#   bounded     genuinely top-K; the rest of the fleet is never measured
#
# A bounded provider must say what it leaves out, and `plan` prints it. There is
# no fourth option and no default that lets the question go unanswered.
COVERAGE_KINDS = ("complete", "converging", "bounded")


@dataclass(frozen=True)
class Scale:
    """Deployment size, used to resolve costs that scale with the fleet."""

    nads: int = 500
    endpoints: int = 10_000
    sessions: int = 2_000
    # Internal Device Administration accounts. A human-managed set, but it sizes
    # the TACACS configuration sweep and a wrong dimension there made that cache
    # believe it needed fifty cycles to warm when it needs ten.
    accounts: int = 200

    def __post_init__(self):
        for dimension in SCALE_DIMENSIONS:
            value = getattr(self, dimension)
            if not isinstance(value, int) or value < 1:
                raise ModelError(f"scale.{dimension} must be a positive integer")

    def units_of(self, dimension: str) -> float:
        """Return the dimension in thousands, the unit costs are declared in."""
        if not dimension:
            return 0.0
        if dimension not in SCALE_DIMENSIONS:
            raise ModelError(f"unknown scale dimension {dimension!r}")
        return getattr(self, dimension) / 1000.0


@dataclass(frozen=True)
class Load:
    """Hourly load on one target, from one dataset or a whole plan."""

    target: str
    requests_per_hour: float = 0.0
    db_seconds_per_hour: float = 0.0
    streams: int = 0

    @property
    def duty_cycle_percent(self) -> float:
        """Oracle work as a percentage of wall time -- the Data Connect budget."""
        return self.db_seconds_per_hour / 36.0

    def __add__(self, other: "Load") -> "Load":
        if other.target != self.target:
            raise ModelError(
                f"cannot add {other.target} load to {self.target} load")
        return Load(
            target=self.target,
            requests_per_hour=self.requests_per_hour + other.requests_per_hour,
            db_seconds_per_hour=self.db_seconds_per_hour + other.db_seconds_per_hour,
            streams=self.streams + other.streams,
        )


@dataclass(frozen=True)
class Cost:
    """What one collection from one provider costs its target.

    A fixed part plus an optional part that scales with a fleet dimension. A
    provider that holds a persistent connection sets ``streaming``; its per-cycle
    requests still count, because a stream usually needs a periodic re-poll for
    the bulk baseline the change feed does not carry.

    ``shares`` names a cost pool. Two providers on the same target that draw from
    one pool of work -- MnT active sessions and MnT current posture both come out
    of a single per-MAC detail fan-out -- charge that pool once rather than each
    declaring the full amount. Without this the plan double-counts, which is the
    exact class of quiet lie the load model exists to prevent.
    """

    target: str
    requests: float = 0.0
    db_seconds: float = 0.0
    scales_with: str = ""
    requests_per_1k: float = 0.0
    db_seconds_per_1k: float = 0.0
    max_rows: int = 0
    streaming: bool = False
    shares: str = ""

    # A provider that caches per-entity detail has two different costs, and
    # reporting only one of them misleads in whichever direction you pick.
    #
    # Some ISE facts are stable for the life of the thing they describe -- the
    # authorization decision for a session does not change while that session
    # lasts. Fetch each one once, cache it, and the per-cycle budget stops being
    # a coverage ceiling and becomes a warm-up rate limit: coverage converges to
    # the whole active set, and steady-state cost falls to whatever turned over.
    #
    #   warmup_requests   per-cycle ceiling while the cache fills
    #   churn_fraction    share of `scales_with` needing a fetch once warm,
    #                     per cycle at the dataset's own cadence
    warmup_requests: float = 0.0
    churn_fraction: float = 0.0

    def __post_init__(self):
        if self.target not in TARGETS:
            raise ModelError(
                f"cost target must be one of {TARGETS}, got {self.target!r}")
        if self.scales_with and self.scales_with not in SCALE_DIMENSIONS:
            raise ModelError(
                f"cost scales_with must be one of {SCALE_DIMENSIONS}, "
                f"got {self.scales_with!r}")
        for name in ("requests", "db_seconds", "requests_per_1k",
                     "db_seconds_per_1k", "max_rows"):
            if getattr(self, name) < 0:
                raise ModelError(f"cost {name} cannot be negative")
        scaled = self.requests_per_1k or self.db_seconds_per_1k
        if scaled and not self.scales_with:
            raise ModelError(
                "cost declares a per-1k component without scales_with")
        if self.db_seconds and self.target != "oracle":
            raise ModelError(
                f"only the oracle target has db_seconds, not {self.target!r}")

    def magnitude_for(self, scale: Scale) -> float:
        """Single number for comparing two costs drawing on the same pool."""
        return self.requests_for(scale) + self.db_seconds_for(scale)

    @property
    def converges(self) -> bool:
        """True when this provider caches and its coverage reaches the fleet."""
        return bool(self.warmup_requests or self.churn_fraction)

    def requests_for(self, scale: Scale) -> float:
        """Steady-state per-collection requests -- what you live with.

        For a converging provider this is the churn, not the warm-up burst: once
        the cache is full, only what turned over needs fetching.
        """
        if self.converges:
            units = scale.units_of(self.scales_with) * 1000.0
            return self.requests + units * self.churn_fraction
        return self.requests + self.requests_per_1k * scale.units_of(self.scales_with)

    def warmup_requests_for(self, scale: Scale) -> float:
        """Per-collection requests while the cache is still filling."""
        if not self.converges:
            return self.requests_for(scale)
        return self.requests + self.warmup_requests

    def cycles_to_warm(self, scale: Scale) -> int:
        """Collections needed before coverage reaches the whole active set."""
        if not self.converges or self.warmup_requests <= 0:
            return 0
        units = scale.units_of(self.scales_with) * 1000.0
        return max(1, math.ceil(units / self.warmup_requests))

    def db_seconds_for(self, scale: Scale) -> float:
        return self.db_seconds + self.db_seconds_per_1k * scale.units_of(self.scales_with)

    def load(self, interval_seconds: int, scale: Scale) -> Load:
        """Steady-state hourly load -- what the budget is checked against.

        A converging provider is budgeted on what it costs once warm, because
        that is what it costs for all but the first few minutes. The warm-up
        burst is reported separately rather than inflating the budget forever.
        """
        cycles_per_hour = 3600.0 / max(1, int(interval_seconds))
        return Load(
            target=self.target,
            requests_per_hour=self.requests_for(scale) * cycles_per_hour,
            db_seconds_per_hour=self.db_seconds_for(scale) * cycles_per_hour,
            streams=1 if self.streaming else 0,
        )

    def warmup_load(self, interval_seconds: int, scale: Scale) -> Load:
        """Hourly load while the cache is still filling."""
        cycles_per_hour = 3600.0 / max(1, int(interval_seconds))
        return Load(
            target=self.target,
            requests_per_hour=self.warmup_requests_for(scale) * cycles_per_hour,
            db_seconds_per_hour=self.db_seconds_for(scale) * cycles_per_hour,
            streams=1 if self.streaming else 0,
        )


@dataclass(frozen=True)
class Provider:
    """One way to supply a dataset.

    ``requires`` holds only preconditions that need a live appliance -- a Data
    Connect view, or a capability probe. Needing its own target configured is
    implied by ``cost.target`` and is checked offline, so ``ise-exporter3 plan``
    can tell you which datasets have no viable source without credentials.
    """

    name: str
    cost: Cost
    supplies: frozenset = frozenset()
    requires: tuple = ()
    notes: str = ""
    coverage: str = "complete"
    fetch: Optional[Callable] = None

    def __post_init__(self):
        if not self.name:
            raise ModelError("provider name is required")
        for requirement in self.requires:
            if not str(requirement).startswith(("view:", "capability:")):
                raise ModelError(
                    f"provider {self.name!r} requirement {requirement!r} must be "
                    "'view:NAME' or 'capability:NAME'")
        if self.coverage not in COVERAGE_KINDS:
            raise ModelError(
                f"provider {self.name!r} coverage must be one of {COVERAGE_KINDS}, "
                f"got {self.coverage!r}")
        if self.cost.converges and self.coverage != "converging":
            raise ModelError(
                f"provider {self.name!r} declares a warm-up cost, so its coverage "
                "is 'converging'")
        if self.coverage == "converging" and not self.cost.converges:
            raise ModelError(
                f"provider {self.name!r} claims converging coverage without "
                "declaring warmup_requests or churn_fraction, so nothing makes "
                "it converge")
        if self.coverage == "bounded" and not self.notes:
            raise ModelError(
                f"provider {self.name!r} measures only part of the fleet and must "
                "say in `notes` what it leaves out")

    @property
    def target(self) -> str:
        return self.cost.target


@dataclass(frozen=True)
class Dataset:
    """A unit of collection: the metrics it owns and who can supply them."""

    name: str
    description: str
    providers: tuple
    default_interval: int
    metrics: tuple = field(default=())

    def __post_init__(self):
        if not self.name:
            raise ModelError("dataset name is required")
        if not self.providers:
            raise ModelError(f"dataset {self.name!r} declares no providers")
        names = [provider.name for provider in self.providers]
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            raise ModelError(
                f"dataset {self.name!r} declares duplicate providers: "
                f"{', '.join(sorted(duplicates))}")
        if self.default_interval < 1:
            raise ModelError(
                f"dataset {self.name!r} needs a positive default_interval")

    @property
    def provider_names(self) -> tuple:
        return tuple(provider.name for provider in self.providers)

    def provider(self, name: str) -> Provider:
        for provider in self.providers:
            if provider.name == name:
                return provider
        raise ModelError(
            f"dataset {self.name!r} has no provider {name!r}; "
            f"choose from {', '.join(self.provider_names)}")

    @property
    def multi_source(self) -> bool:
        return len(self.providers) > 1
