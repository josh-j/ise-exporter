"""Every ceiling the exporter enforces, derived from one declaration.

v2 spread its safety limits across config hard ranges, client-side clamps and
collector helpers. v3's first cut moved them into module constants, which is
tidier but no more coherent, because nothing checked that they agreed -- and
they did not. ``reporting.MAX_GROUPS = 5500`` bounded one statement,
``MAX_BATCH_QUERIES = 5`` allowed five statements per batch, and
``MAX_BATCH_RESULT_ROWS = 12_000`` was smaller than three full-size statements.
``tacacs_activity`` published nothing at the declared scale for that reason
alone, and every failure still paid for the Oracle scans it made before
aborting.

The mistake was not the numbers. It was choosing them at all: what a marginal
breakdown returns is *the size of the fleet*, so a ceiling above it has to be
derived from the fleet, in one order, from one declaration:

    scale.nads + scale.accounts + DIMENSION_HEADROOM  ->  group_ceiling
    group_ceiling + ROW_HEADROOM                      ->  result_rows
    result_rows * batch_queries                       ->  batch_result_rows

Everything here is visible in ``[limits]`` and printed by ``plan``, because a
ceiling nobody can see or name is not a contract -- it is a surprise waiting for
an estate large enough to find it. Everything here is also overridable, subject
to two rules:

- an override that breaks the ordering above is a **configuration error**,
  refused at load rather than discovered when a dataset stops publishing;
- an override that is merely unwise **warns at start**, by name, with the value
  it should have had.

The hard floors and caps are not preferences and are not configurable: below a
floor the exporter cannot answer at any scale, and above a cap one statement can
retain more than this process should hold.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from types import MappingProxyType


class LimitsError(ValueError):
    """A declared ceiling contradicts another one, or is outside a hard bound."""


# --- what the derivation is built from --------------------------------------

# The dimensions of a marginal breakdown that do *not* scale with the fleet:
# statuses, authentication methods, failure codes, policy sets, PSNs, command
# families. Several hundred of any one of them would be a remarkable estate, so
# this is headroom rather than a target.
DIMENSION_HEADROOM = 500

# A statement may legitimately return every group it asked for, so the row
# ceiling sits *above* the group ceiling rather than on it: the transport refuses
# at equality, and a statement returning exactly its groups is a success.
ROW_HEADROOM = 500

# One dashboard update often needs several bounded statements, charged one
# duty-cycle cooldown between them. Five is what the reporting datasets ask for;
# it is the one number here with no fleet dimension behind it.
DEFAULT_BATCH_QUERIES = 5

# A marginal row is a dimension name, a bounded label value and a few numbers.
# Used only to check declared row ceilings against the declared byte ceiling.
ESTIMATED_ROW_BYTES = 200

DEFAULT_RESULT_BYTES = 64 * 1024 * 1024
DEFAULT_FIELD_BYTES = 1024 * 1024
DEFAULT_FIELD_NESTING_DEPTH = 16

# Schema discovery reads one row per (view, column) from the Oracle data
# dictionary, and that result is a property of the ISE release -- roughly thirty
# reporting views and their columns -- not of the estate. It must therefore NOT
# derive from [scale], and it took a live 4-NAD lab to prove why: a scale-derived
# `result_rows` of 1,005 refused the catalogue outright, which leaves every Data
# Connect dataset at `schema_pending` forever on any small deployment. The
# reporting ceilings scale because breakdowns scale; this one does not, so it
# gets a fixed floor and says so.
DEFAULT_CATALOG_ROWS = 20_000
# Below this, a real ISE catalogue almost certainly does not fit. Kept as a
# warning rather than a hard bound because the number of reporting views an
# account can see is ISE's decision, not ours, and a future release could
# legitimately expose fewer.
MIN_PLAUSIBLE_CATALOG_ROWS = 5_000

# The exporter samples a bounded window; it never aggregates the whole interval
# between runs. Cisco's TACACS views retain two days, which is what this stops a
# six-hourly dataset from scanning by accident.
DEFAULT_WINDOW_HOURS = 6

# One dataset must never turn a bounded query into row-like Prometheus state.
# The real invariant is *what a series may be keyed on*: NAD-keyed is legitimate
# (dead-switch detection needs one series per switch), endpoint-keyed is not.
# So this is sized as "a fleet dimension times a small constant" with headroom,
# and the guard that matters is the test asserting no dataset keys on an
# endpoint identity.
DEFAULT_SNAPSHOT_SAMPLES = 50_000
# Crossing this is not an error, but it is worth knowing before the hard ceiling
# arrives: it means a dataset is growing with something it should not be.
DEFAULT_SNAPSHOT_SAMPLE_WARNING = 20_000


# --- hard bounds, in code, not configurable ---------------------------------

HARD_BOUNDS = MappingProxyType({
    "group_ceiling": (100, 100_000),
    "result_rows": (101, 200_000),
    "catalog_rows": (1_000, 1_000_000),
    "batch_queries": (1, 10),
    "batch_result_rows": (101, 1_000_000),
    "result_bytes": (1024 * 1024, 512 * 1024 * 1024),
    "field_bytes": (4096, 64 * 1024 * 1024),
    "field_nesting_depth": (2, 64),
    "window_hours": (1, 24),
    "snapshot_samples": (1_000, 500_000),
    "snapshot_sample_warning": (100, 500_000),
})

FIELDS = tuple(HARD_BOUNDS)

# Set from [scale] unless the operator names them. Overriding one of these
# breaks the chain the section docstring describes, so each says so at start.
DERIVED_FIELDS = ("group_ceiling", "result_rows", "batch_result_rows")

DESCRIPTIONS = MappingProxyType({
    "group_ceiling": "groups one reporting statement may return",
    "result_rows": "rows one Data Connect statement may return",
    "catalog_rows": "rows one Oracle dictionary read may return",
    "batch_queries": "statements one Data Connect batch may hold",
    "batch_result_rows": "rows one Data Connect batch may return in total",
    "result_bytes": "bytes one statement or batch may retain",
    "field_bytes": "bytes one field of one row may retain",
    "field_nesting_depth": "levels a nested field may be expanded to",
    "window_hours": "hours back any event statement may scan",
    "snapshot_samples": "series one dataset may publish",
    "snapshot_sample_warning": "series after which a dataset is reported as wide",
})


@dataclass(frozen=True)
class Limits:
    """Resolved ceilings for one configuration.

    ``overridden`` names the fields the operator set explicitly, so ``plan`` can
    distinguish a derived contract from a hand-chosen one without guessing.
    """

    group_ceiling: int
    result_rows: int
    batch_queries: int
    batch_result_rows: int
    catalog_rows: int = DEFAULT_CATALOG_ROWS
    result_bytes: int = DEFAULT_RESULT_BYTES
    field_bytes: int = DEFAULT_FIELD_BYTES
    field_nesting_depth: int = DEFAULT_FIELD_NESTING_DEPTH
    window_hours: int = DEFAULT_WINDOW_HOURS
    snapshot_samples: int = DEFAULT_SNAPSHOT_SAMPLES
    snapshot_sample_warning: int = DEFAULT_SNAPSHOT_SAMPLE_WARNING
    overridden: tuple = ()

    def __post_init__(self):
        _check_bounds(self)
        _check_contract(self)

    @property
    def derived(self):
        """The fields still following the scale, rather than a declared value."""
        return tuple(name for name in DERIVED_FIELDS if name not in self.overridden)

    def fits_scale(self, scale):
        """Groups the largest marginal statement needs at this declared scale."""
        return scale.nads + scale.accounts

    def describe(self, scale):
        """One row per ceiling, for ``plan``: name, value, and where it came from."""
        rows = []
        for name in FIELDS:
            if name in self.overridden:
                origin = "declared"
            elif name in DERIVED_FIELDS:
                origin = "derived from scale"
            else:
                origin = "default"
            rows.append((name, getattr(self, name), origin, DESCRIPTIONS[name]))
        return tuple(rows)


def group_ceiling_for(scale):
    """The groups one marginal statement can produce at this declared scale.

    Marginal cardinalities *add* rather than multiply -- roughly one group per
    NAD, plus one per account, plus a few hundred for every bounded dimension
    put together -- which is exactly why the reporting datasets can be complete
    rather than top-K. That sum is what this ceiling has to clear.
    """
    return _clamp("group_ceiling", scale.nads + scale.accounts + DIMENSION_HEADROOM)


def derive(scale, document=None, warnings=None):
    """Resolve ``[limits]`` against the declared scale.

    ``document`` is the raw ``[limits]`` table. ``warnings`` collects the values
    that are legal but dangerous, so they reach the operator at start instead of
    being found by an estate large enough to trip them.
    """
    document = dict(document or {})
    collected = [] if warnings is None else warnings

    unknown = sorted(set(document) - set(FIELDS))
    if unknown:
        raise LimitsError(
            f"unknown limits key{'s' if len(unknown) != 1 else ''}: "
            f"{', '.join(unknown)}; known keys: {', '.join(FIELDS)}")

    declared = {}
    for name, value in document.items():
        if isinstance(value, bool) or not isinstance(value, int):
            raise LimitsError(
                f"limits.{name} must be an integer, got {type(value).__name__}")
        declared[name] = value

    group_ceiling = declared.get("group_ceiling", group_ceiling_for(scale))
    result_rows = declared.get("result_rows", group_ceiling + ROW_HEADROOM)
    batch_queries = declared.get("batch_queries", DEFAULT_BATCH_QUERIES)
    # The whole point of item 1a: this is a product, not a preference. A batch is
    # allowed to return what the statements in it are individually allowed to
    # return, and nothing is given up by saying so -- the real safety bound on a
    # batch is result_bytes, and even the largest permitted batch is a few MB.
    batch_result_rows = declared.get(
        "batch_result_rows", batch_queries * result_rows)

    limits = Limits(
        group_ceiling=group_ceiling,
        result_rows=result_rows,
        batch_queries=batch_queries,
        batch_result_rows=batch_result_rows,
        catalog_rows=declared.get("catalog_rows", DEFAULT_CATALOG_ROWS),
        result_bytes=declared.get("result_bytes", DEFAULT_RESULT_BYTES),
        field_bytes=declared.get("field_bytes", DEFAULT_FIELD_BYTES),
        field_nesting_depth=declared.get(
            "field_nesting_depth", DEFAULT_FIELD_NESTING_DEPTH),
        window_hours=declared.get("window_hours", DEFAULT_WINDOW_HOURS),
        snapshot_samples=declared.get(
            "snapshot_samples", DEFAULT_SNAPSHOT_SAMPLES),
        snapshot_sample_warning=declared.get(
            "snapshot_sample_warning", DEFAULT_SNAPSHOT_SAMPLE_WARNING),
        overridden=tuple(name for name in FIELDS if name in declared),
    )
    collected.extend(dangerous(limits, scale))
    return limits


def _clamp(name, value):
    low, high = HARD_BOUNDS[name]
    return max(low, min(high, int(value)))


def _check_bounds(limits):
    for name in FIELDS:
        value = getattr(limits, name)
        if isinstance(value, bool) or not isinstance(value, int):
            raise LimitsError(f"limits.{name} must be an integer")
        low, high = HARD_BOUNDS[name]
        if not low <= value <= high:
            raise LimitsError(
                f"limits.{name} must be between {low:,} and {high:,}, "
                f"got {value:,}; this is a hard bound, not a preference")


def _check_contract(limits):
    """The ordering the derivation guarantees, enforced however it was reached.

    This is the drift guard item 1a asks for. It holds whether the values came
    from the scale or from an operator, which is the only version worth having:
    a contract that only the default path honours is a comment.
    """
    if limits.result_rows <= limits.group_ceiling:
        raise LimitsError(
            f"limits.result_rows ({limits.result_rows:,}) must exceed "
            f"limits.group_ceiling ({limits.group_ceiling:,}); a statement that "
            "returns every group it asked for would otherwise be refused as "
            "response_too_large")
    needed = limits.group_ceiling * limits.batch_queries
    if limits.batch_result_rows < needed:
        raise LimitsError(
            f"limits.batch_result_rows ({limits.batch_result_rows:,}) is smaller "
            f"than {limits.batch_queries} full-size statements "
            f"({limits.group_ceiling:,} groups each = {needed:,} rows); a batch "
            "would fail after paying for the scans it already made")
    if limits.batch_result_rows < limits.result_rows:
        raise LimitsError(
            f"limits.batch_result_rows ({limits.batch_result_rows:,}) is smaller "
            f"than one statement's limits.result_rows ({limits.result_rows:,})")
    if limits.field_bytes > limits.result_bytes:
        raise LimitsError(
            f"limits.field_bytes ({limits.field_bytes:,}) exceeds "
            f"limits.result_bytes ({limits.result_bytes:,}), so one field could "
            "never fit in a result")
    if limits.snapshot_sample_warning > limits.snapshot_samples:
        raise LimitsError(
            f"limits.snapshot_sample_warning ({limits.snapshot_sample_warning:,}) "
            f"is above the hard limits.snapshot_samples "
            f"({limits.snapshot_samples:,}), so it could never warn first")


def dangerous(limits, scale):
    """Legal values worth saying out loud at start.

    Each of these is a configuration the exporter will honour and an operator
    probably did not mean. They are warnings rather than errors because the
    estate, not this file, is the authority on what is reasonable -- but silence
    is how "NAD coverage capped at 1000 of 3693" survived a whole release of v2.
    """
    messages = []

    needed = limits.fits_scale(scale)
    if limits.group_ceiling < needed:
        messages.append(
            f"limits.group_ceiling is {limits.group_ceiling:,}, below the "
            f"{needed:,} groups a marginal breakdown needs at the declared scale "
            f"({scale.nads:,} nads + {scale.accounts:,} accounts); every "
            "reporting breakdown will publish truncated=1 and drop its tail")

    estimated = limits.batch_result_rows * ESTIMATED_ROW_BYTES
    if estimated > limits.result_bytes:
        messages.append(
            f"limits.batch_result_rows ({limits.batch_result_rows:,}) is about "
            f"{estimated / 1048576:.0f} MiB of rows against a "
            f"{limits.result_bytes / 1048576:.0f} MiB limits.result_bytes, so "
            "the byte ceiling will stop batches before the row ceiling does")

    for name in DERIVED_FIELDS:
        if name in limits.overridden:
            expected = _expected(limits, scale, name)
            if expected is not None and expected != getattr(limits, name):
                messages.append(
                    f"limits.{name} is set to {getattr(limits, name):,}, "
                    f"overriding the {expected:,} derived from the declared "
                    "scale; the derivation no longer protects this value")

    if limits.catalog_rows < MIN_PLAUSIBLE_CATALOG_ROWS:
        messages.append(
            f"limits.catalog_rows is {limits.catalog_rows:,}, below the "
            f"{MIN_PLAUSIBLE_CATALOG_ROWS:,} rows an ISE reporting catalogue "
            "plausibly holds; schema discovery would fail and every Data "
            "Connect dataset would stay at schema_pending")

    if limits.window_hours > DEFAULT_WINDOW_HOURS:
        messages.append(
            f"limits.window_hours is {limits.window_hours}, above the "
            f"{DEFAULT_WINDOW_HOURS}-hour default; every reporting statement "
            "scans that much event history on each collection")

    if limits.snapshot_samples > DEFAULT_SNAPSHOT_SAMPLES:
        messages.append(
            f"limits.snapshot_samples is {limits.snapshot_samples:,}, above the "
            f"{DEFAULT_SNAPSHOT_SAMPLES:,} default; a dataset keyed on an "
            "endpoint identity would no longer be stopped by this ceiling")

    return tuple(messages)


def _expected(limits, scale, name):
    if name == "group_ceiling":
        return group_ceiling_for(scale)
    if name == "result_rows":
        return limits.group_ceiling + ROW_HEADROOM
    if name == "batch_result_rows":
        return limits.batch_queries * limits.result_rows
    return None


def for_scale(scale, **overrides):
    """Derived limits for a scale, for tests and tools that have no config."""
    limits = derive(scale)
    if not overrides:
        return limits
    return replace(limits, overridden=tuple(
        dict.fromkeys(limits.overridden + tuple(overrides))), **overrides)
