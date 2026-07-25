"""Resolve a config against the dataset registry into an executable plan.

The plan is the single answer to both questions v3 exists to answer: which source
supplies each dataset, and what that costs each ISE persona per hour. It is built
entirely from configuration and declared costs, so ``ise-exporter3 plan`` runs
with no credentials and no appliance -- you can see what a config will do before
you point it at production.

Requirements that genuinely need a live appliance (a Data Connect view, a pxGrid
capability probe) are reported as deferred rather than guessed at. The scheduler
re-runs the same selection at runtime with those checks resolved, and any change
of provider is exported.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import datasets as registry
from .config import format_duration
from .model import TARGETS, Load


@dataclass(frozen=True)
class RejectedProvider:
    name: str
    reason: str


@dataclass(frozen=True)
class PlannedDataset:
    """One dataset's resolved source, cadence, and hourly cost."""

    name: str
    description: str
    enabled: bool
    interval: int
    dataset: object = None           # model.Dataset this entry resolves
    provider: object = None          # model.Provider, or None when unresolvable
    load: object = None              # model.Load charged to this dataset
    alternatives: tuple = ()         # viable providers behind the active one
    rejected: tuple = ()             # RejectedProvider, in preference order
    deferred: tuple = ()             # live checks the plan cannot settle offline
    shared_with: tuple = ()          # datasets drawing on the same pool of work
    charged: bool = True             # False when a pool peer carries the cost

    @property
    def resolved(self):
        return self.provider is not None

    @property
    def degraded(self):
        """True when the active provider is not the operator's first choice."""
        return self.resolved and bool(self.rejected)

    @property
    def target(self):
        return self.provider.target if self.resolved else ""

    def to_dict(self):
        return {
            "dataset": self.name,
            "description": self.description,
            "enabled": self.enabled,
            "interval_seconds": self.interval,
            "provider": self.provider.name if self.resolved else None,
            "target": self.target,
            "degraded": self.degraded,
            "requests_per_hour": round(self.load.requests_per_hour, 2) if self.load else 0.0,
            "db_seconds_per_hour": round(self.load.db_seconds_per_hour, 2) if self.load else 0.0,
            "streaming": bool(self.load.streams) if self.load else False,
            "shared_with": list(self.shared_with),
            "charged": self.charged,
            "alternatives": [provider.name for provider in self.alternatives],
            "rejected": [{"provider": item.name, "reason": item.reason}
                         for item in self.rejected],
            "deferred_checks": list(self.deferred),
            "coverage": self.provider.coverage if self.resolved else None,
            "supplies": sorted(self.provider.supplies) if self.resolved else [],
            "notes": self.provider.notes if self.resolved else "",
        }


@dataclass(frozen=True)
class TargetPlan:
    """Planned load on one target against its declared ceiling."""

    target: str
    load: Load
    budget: object                   # config.TargetBudget

    @property
    def over_budget(self):
        return bool(self.overage_reason)

    @property
    def overage_reason(self):
        if self.target == "oracle":
            ceiling = self.budget.duty_cycle_percent
            if ceiling and self.load.duty_cycle_percent > ceiling:
                return (f"{self.load.duty_cycle_percent:.2f}% duty cycle exceeds "
                        f"the {ceiling:.2f}% ceiling")
            return ""
        ceiling = self.budget.requests_per_hour
        if ceiling and self.load.requests_per_hour > ceiling:
            return (f"{self.load.requests_per_hour:,.0f} requests/hour exceeds "
                    f"the {ceiling:,.0f} ceiling")
        return ""

    @property
    def utilisation(self):
        """Fraction of the declared ceiling used, or None when none is declared."""
        if self.target == "oracle":
            ceiling = self.budget.duty_cycle_percent
            return self.load.duty_cycle_percent / ceiling if ceiling else None
        ceiling = self.budget.requests_per_hour
        return self.load.requests_per_hour / ceiling if ceiling else None

    def to_dict(self):
        return {
            "target": self.target,
            "requests_per_hour": round(self.load.requests_per_hour, 2),
            "db_seconds_per_hour": round(self.load.db_seconds_per_hour, 2),
            "duty_cycle_percent": round(self.load.duty_cycle_percent, 3),
            "streams": self.load.streams,
            "budget_requests_per_hour": self.budget.requests_per_hour,
            "budget_duty_cycle_percent": self.budget.duty_cycle_percent,
            "utilisation": round(self.utilisation, 4) if self.utilisation is not None else None,
            "over_budget": self.over_budget,
            "overage_reason": self.overage_reason,
        }


@dataclass(frozen=True)
class Plan:
    config: object
    entries: tuple
    targets: tuple

    @property
    def enabled(self):
        return tuple(entry for entry in self.entries if entry.enabled)

    @property
    def unresolved(self):
        return tuple(entry for entry in self.enabled if not entry.resolved)

    @property
    def degraded(self):
        return tuple(entry for entry in self.enabled if entry.degraded)

    @property
    def overages(self):
        return tuple(target for target in self.targets if target.over_budget)

    @property
    def fits(self):
        return not self.overages

    def to_dict(self):
        return {
            "config_file": self.config.path,
            "profile": self.config.profile,
            "scale": {
                "nads": self.config.scale.nads,
                "endpoints": self.config.scale.endpoints,
                "sessions": self.config.scale.sessions,
            },
            "fits_budget": self.fits,
            "limits": {
                name: {"value": value, "origin": origin}
                for name, value, origin, _description
                in self.config.limits.describe(self.config.scale)
            },
            "datasets": [entry.to_dict() for entry in self.entries],
            "targets": [target.to_dict() for target in self.targets],
            "warnings": list(self.config.warnings),
        }


# A filling cache is never revisited faster than this, whatever the arithmetic
# says. Below a few seconds the scheduler tick dominates and the collection
# overhead stops being negligible against the work it does.
MIN_WARMUP_INTERVAL_SECONDS = 30


def warming_entries(plan, target):
    """Enabled datasets on ``target`` that pay to fill a cache of their own.

    ``charged`` matters here: ``posture_current`` reads the cache
    ``session_authorization`` fills and issues no detail requests itself, so
    counting it as a second consumer of the budget would halve the rate the one
    that actually spends gets.
    """
    return tuple(
        entry for entry in plan.enabled
        if entry.resolved and entry.target == target and entry.charged
        and entry.provider.cost.converges
        and entry.provider.cost.warmup_requests > 0)


def warmup_share(plan, entry):
    """Requests per hour this dataset may spend while its cache fills.

    What is left of the target's ceiling after the steady state everything else
    on it costs, divided between the caches actually filling. Zero when nothing
    can be derived -- no declared ceiling, or not a converging provider.
    """
    if not entry.resolved or not entry.provider.cost.converges:
        return 0.0
    if entry.provider.cost.warmup_requests <= 0:
        return 0.0
    budget = plan.config.budget_for(entry.target)
    # The declared burst if there is one: that is exactly what it is for.
    ceiling = budget.warmup_requests_per_hour or budget.requests_per_hour
    if ceiling <= 0:
        return 0.0
    planned = next((target.load.requests_per_hour for target in plan.targets
                    if target.target == entry.target), 0.0)
    # Conservative on purpose: a filling cache is paying warm-up instead of its
    # own steady churn, so subtracting the whole planned figure slightly
    # understates what is available. Erring toward the appliance is the right
    # direction for a number that decides how hard to push it.
    available = max(0.0, ceiling - planned)
    peers = len(warming_entries(plan, entry.target)) or 1
    return available / peers


def warmup_interval(plan, entry):
    """The cadence a still-filling cache is revisited at, from the budget.

    A converging cache used to be revisited at the cadence its *data* deserves
    once warm -- ``network_devices`` at six hours -- which meant 500 NADs
    classified every six hours and 5,000 of them taking sixty hours, while the
    PAN budget sat at 1% utilisation the entire time. The cadence a cold cache
    wants is not "how often is this worth re-reading" but "how fast may I
    spend", and the budget already answers that.

    Never faster than the derived share, even though one cache could finish
    sooner if it took the whole ceiling while the others waited: a rate this
    predicts and the token bucket then refuses is worse than a slower rate both
    agree on. Never slower than the dataset's own cadence either -- that is the
    case where enforcement, not scheduling, sets the pace.
    """
    share = warmup_share(plan, entry)
    if share <= 0:
        return 0
    # The whole per-cycle cost, not just the warm-up batch: enumeration and any
    # other fixed requests are spent on every pass too, and a cadence derived
    # from the batch alone quietly asks for more than the budget allows.
    per_cycle = entry.provider.cost.warmup_requests_for(plan.config.scale)
    if per_cycle <= 0:
        return 0
    seconds = per_cycle / share * 3600.0
    return int(max(MIN_WARMUP_INTERVAL_SECONDS, min(entry.interval, seconds)))


@dataclass(frozen=True)
class WarmupReport:
    """What a cold start for one dataset costs, and what actually paces it."""

    interval: int                 # cadence the scheduler uses while filling
    cycles: int                   # passes needed at the per-cycle budget
    requests_per_hour: float      # what that cadence asks for
    available_per_hour: float     # what the target's budget leaves for it
    seconds: float                # time to full coverage
    throttled: bool               # True when the budget sets the pace, not the cadence

    @property
    def paced(self):
        """True when the warm-up cadence differs from the dataset's own."""
        return not self.throttled


def warmup_report(plan, entry):
    """Time to full coverage using the pace that will really be applied.

    Two different things can set that pace. Usually it is the cadence above, and
    the answer is cycles times interval. But when a dataset's own cadence is
    already shorter than its share of the budget -- ``session_authorization``
    wants 20,000 MnT requests and runs every five minutes -- scheduling cannot
    slow it down and the token bucket is what stretches it instead. Reporting
    the first number in that case is how "2.8x the declared budget" got quoted
    as a plan for as long as it did.
    """
    cost = entry.provider.cost
    scale = plan.config.scale
    cycles = cost.cycles_to_warm(scale)
    interval = warmup_interval(plan, entry) or entry.interval
    per_cycle = cost.warmup_requests_for(scale)
    rate = per_cycle * 3600.0 / max(1, interval)
    share = warmup_share(plan, entry)
    # A percent of slack, so floating-point noise does not read as throttling.
    throttled = bool(share) and rate > share * 1.01
    seconds = per_cycle * cycles / share * 3600.0 if throttled else cycles * interval
    return WarmupReport(
        interval=interval, cycles=cycles, requests_per_hour=rate,
        available_per_hour=share, seconds=seconds, throttled=throttled)


def _select(dataset, dataset_config, config):
    """Pick the first provider whose target is configured, keeping the reasons."""
    order = dataset_config.providers or dataset.provider_names
    rejected = []
    for index, name in enumerate(order):
        provider = dataset.provider(name)
        target = config.targets[provider.target]
        if not target.configured:
            rejected.append(RejectedProvider(name, target.unconfigured_reason()))
            continue
        alternatives = tuple(
            dataset.provider(later) for later in order[index + 1:]
            if config.targets[dataset.provider(later).target].configured
        )
        return provider, tuple(rejected), alternatives
    return None, tuple(rejected), ()


@dataclass(frozen=True)
class _Selection:
    dataset: object
    interval: int
    enabled: bool
    provider: object = None
    rejected: tuple = ()
    alternatives: tuple = ()


def _apply_cost_pools(selections, scale):
    """Charge each pool of shared work once instead of once per dataset.

    Two datasets fed by one fan-out (MnT active sessions and MnT current posture
    both come out of the same per-MAC detail walk) would otherwise each declare
    the full request budget and double the reported MnT load. The pool runs at
    the shortest cadence any member asks for and is sized by the member that
    needs the most from it; the first member carries the charge and the rest
    report zero with a pointer to their peer.
    """
    loads, shared_with, charged = {}, {}, {}
    pools = {}

    for selection in selections:
        name = selection.dataset.name
        if not (selection.enabled and selection.provider):
            loads[name] = None
            continue
        pool = selection.provider.cost.shares
        if not pool:
            loads[name] = selection.provider.cost.load(selection.interval, scale)
            continue
        pools.setdefault((selection.provider.target, pool), []).append(selection)

    for (target, _pool), members in pools.items():
        interval = min(member.interval for member in members)
        heaviest = max(members, key=lambda m: m.provider.cost.magnitude_for(scale))
        owner, names = members[0], [member.dataset.name for member in members]
        loads[owner.dataset.name] = heaviest.provider.cost.load(interval, scale)
        for member in members:
            name = member.dataset.name
            shared_with[name] = tuple(other for other in names if other != name)
            charged[name] = member is owner
            if member is not owner:
                loads[name] = Load(target=target)

    return loads, shared_with, charged


def build_plan(config):
    """Resolve every registered dataset against the configuration."""
    selections = []
    for dataset in registry.all_datasets():
        dataset_config = config.dataset(dataset.name)
        interval = dataset_config.interval or dataset.default_interval
        if not dataset_config.enabled:
            selections.append(_Selection(dataset, interval, enabled=False))
            continue
        provider, rejected, alternatives = _select(dataset, dataset_config, config)
        selections.append(_Selection(
            dataset, interval, True, provider, rejected, alternatives))

    loads, shared_with, charged = _apply_cost_pools(selections, config.scale)

    entries = []
    totals = {target: Load(target=target) for target in TARGETS}
    for selection in selections:
        name = selection.dataset.name
        load = loads[name]
        if load is not None:
            totals[load.target] = totals[load.target] + load
        entries.append(PlannedDataset(
            name=name,
            description=selection.dataset.description,
            enabled=selection.enabled,
            interval=selection.interval,
            dataset=selection.dataset,
            provider=selection.provider,
            load=load,
            alternatives=selection.alternatives,
            rejected=selection.rejected,
            deferred=tuple(selection.provider.requires) if selection.provider else (),
            shared_with=shared_with.get(name, ()),
            charged=charged.get(name, True),
        ))

    targets = tuple(
        TargetPlan(target=name, load=totals[name], budget=config.budget_for(name))
        for name in TARGETS
    )
    return Plan(config=config, entries=tuple(entries), targets=targets)


def _dataset_rows(plan):
    rows = []
    for entry in plan.entries:
        if not entry.enabled:
            rows.append((entry.name, "-", "-", format_duration(entry.interval),
                         "-", "-", "disabled"))
            continue
        if not entry.resolved:
            reason = entry.rejected[0].reason if entry.rejected else "no provider"
            rows.append((entry.name, "NONE", "-", format_duration(entry.interval),
                         "-", "-", f"unavailable: {reason}"))
            continue
        note = []
        if entry.provider.coverage == "bounded":
            # Loud on purpose: a metric that measures part of the fleet and
            # reads like all of it is the failure this column exists to stop.
            note.append("PARTIAL COVERAGE")
        elif entry.provider.coverage == "converging":
            note.append("converges")
        if entry.load.streams:
            note.append("stream")
        if entry.shared_with:
            peers = ", ".join(entry.shared_with)
            note.append(f"covers {peers}" if entry.charged
                        else f"no extra cost; shared with {peers}")
        if entry.degraded:
            note.append(f"fell back from {entry.rejected[0].name}")
        elif entry.alternatives:
            count = len(entry.alternatives)
            note.append(f"{count} fallback{'s' if count > 1 else ''}")
        rows.append((
            entry.name,
            entry.provider.name,
            entry.target,
            format_duration(entry.interval),
            f"{entry.load.requests_per_hour:,.1f}" if entry.load.requests_per_hour else "-",
            f"{entry.load.db_seconds_per_hour:,.1f}" if entry.load.db_seconds_per_hour else "-",
            ", ".join(note),
        ))
    return rows


def _bound_rows(plan):
    """Every dataset option that narrows what gets published, and its value.

    A dataset that keeps the top K of something states K here. The alternative
    is the one this table exists to prevent: a panel that looks like the fleet
    and is the busiest slice of it, with the number that decided so living in a
    collector nobody reads.
    """
    rows = []
    for entry in plan.enabled:
        settings = plan.config.dataset(entry.name)
        for option in entry.dataset.options:
            value = settings.options[option.name]
            rows.append((
                f"{entry.name}.{option.name}",
                "true" if value is True else "false" if value is False else str(value),
                "declared" if option.name in settings.declared_options else "default",
                option.description,
            ))
    return rows


def _approx_duration(seconds):
    """A duration to read, not to compute with.

    ``format_duration`` is exact and falls back to raw seconds, which is right
    for a configured interval and unreadable for a derived one -- "45708s" is a
    number nobody converts in their head while deciding whether to declare a
    warm-up burst.
    """
    seconds = max(0, int(seconds))
    if seconds < 120:
        return f"{seconds}s"
    if seconds < 7200:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def _render_table(headers, rows):
    columns = list(zip(*([headers] + rows))) if rows else [(header,) for header in headers]
    widths = [max(len(str(cell)) for cell in column) for column in columns]
    lines = ["  ".join(str(h).ljust(w) for h, w in zip(headers, widths)).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        lines.append("  ".join(str(c).ljust(w) for c, w in zip(row, widths)).rstrip())
    return lines


def render_plan(plan):
    """Render the plan as the operator-facing text report."""
    config = plan.config
    out = [
        f"ise-exporter3 plan  {config.path or '(no file)'}",
        f"profile={config.profile}  scale: {config.scale.nads:,} nads, "
        f"{config.scale.endpoints:,} endpoints, {config.scale.sessions:,} sessions",
        "",
    ]
    out += _render_table(
        ("DATASET", "PROVIDER", "TARGET", "EVERY", "REQ/H", "DBSEC/H", "NOTE"),
        _dataset_rows(plan))
    out.append("")

    budget_rows = []
    for target in plan.targets:
        if target.target == "oracle":
            planned = f"{target.load.duty_cycle_percent:.2f}% duty"
            ceiling = (f"{target.budget.duty_cycle_percent:.2f}% duty"
                       if target.budget.duty_cycle_percent else "none declared")
        else:
            planned = f"{target.load.requests_per_hour:,.0f} req/h"
            ceiling = (f"{target.budget.requests_per_hour:,.0f} req/h"
                       if target.budget.requests_per_hour else "none declared")
        used = (f"{target.utilisation * 100:.0f}%"
                if target.utilisation is not None else "-")
        streams = f"{target.load.streams} stream" if target.load.streams else ""
        budget_rows.append((
            target.target, planned, ceiling, used,
            "OVER BUDGET" if target.over_budget else streams))
    out += _render_table(
        ("TARGET", "PLANNED", "BUDGET", "USED", ""), budget_rows)

    unresolved = plan.unresolved
    if unresolved:
        out += ["", f"{len(unresolved)} enabled dataset(s) have no viable provider:"]
        for entry in unresolved:
            out.append(f"  {entry.name}")
            for item in entry.rejected:
                out.append(f"      {item.name}: {item.reason}")

    degraded = plan.degraded
    if degraded:
        out += ["", f"{len(degraded)} dataset(s) are not using their first choice:"]
        for entry in degraded:
            first = entry.rejected[0]
            out.append(
                f"  {entry.name}: using {entry.provider.name} because "
                f"{first.name} is unavailable ({first.reason})")

    # Converging providers cost more while their cache fills than they will
    # afterwards. Budgeting on the warm-up figure would overstate the steady
    # state forever; reporting only the steady state would hide the first hour.
    warming = [entry for entry in plan.enabled
               if entry.resolved and entry.provider.cost.converges]
    if warming:
        out += ["", "Caches that fill over the first few cycles, then get cheaper:"]
        for entry in warming:
            report = warmup_report(plan, entry)
            if not entry.charged:
                # It converges because a pool peer pays for the fan-out, so its
                # coverage is that peer's timeline and it needs no budget of its
                # own. Repeating the peer's advice here would read as two
                # datasets each wanting 24,000 req/h.
                peers = ", ".join(entry.shared_with) or "its pool peer"
                out.append(
                    f"  {entry.name} ({entry.provider.name}): no requests of "
                    f"its own; converges with {peers}")
                continue
            out.append(
                f"  {entry.name} ({entry.provider.name}): "
                f"{report.requests_per_hour:,.0f} req/h while warming, "
                f"{entry.load.requests_per_hour:,.0f} req/h once warm; "
                f"full coverage in ~{_approx_duration(report.seconds)}")
            if report.throttled:
                # The honest version of what used to be printed here. Left
                # unsaid, this line reads as a plan to spend 24,000 req/h
                # against a 4,000 ceiling, which is what the budget refuses.
                declared = config.budget_for(entry.target).warmup_requests_per_hour
                advice = (
                    f"Raise budget.{entry.target}.warmup_requests_per_hour "
                    "further" if declared else
                    f"Declare budget.{entry.target}.warmup_requests_per_hour")
                out.append(
                    f"      that rate exceeds the "
                    f"{report.available_per_hour:,.0f} req/h its budget leaves "
                    f"for warming, so the budget paces it, not the cadence. "
                    f"{advice} to shorten it, or accept the time above")
            elif report.interval != entry.interval:
                out.append(
                    f"      revisited every "
                    f"{_approx_duration(report.interval)} while filling rather "
                    f"than every {format_duration(entry.interval)}, which is "
                    f"what its share of the {entry.target} budget affords")
        # The burst is the one setting that lets the exporter knowingly exceed
        # its own steady ceiling, so it is stated as a decision with a duration.
        for target in plan.targets:
            if not target.budget.warmup_requests_per_hour:
                continue
            longest = max(
                (warmup_report(plan, entry).seconds for entry in warming
                 if entry.target == target.target), default=0)
            out.append(
                f"  budget.{target.target} permits "
                f"{target.budget.warmup_requests_per_hour:,.0f} req/h while any "
                f"of them is filling -- "
                f"{target.budget.warmup_multiple:.1f}x the steady "
                f"{target.budget.requests_per_hour:,.0f} req/h, for about "
                f"{_approx_duration(longest)}")

    # What the selected source cannot tell you. On a large estate this is the
    # difference between a number and a sample, and an operator who reads the
    # cadence but not this will trust a panel further than it deserves.
    caveats = [entry for entry in plan.enabled
               if entry.resolved and entry.provider.notes]
    if caveats:
        out += ["", "What the selected source does and does not cover:"]
        for entry in caveats:
            out.append(f"  {entry.name} ({entry.provider.name}): "
                       f"{entry.provider.notes}")

    # The ceilings, before the caveats, because a truncated breakdown is usually
    # a ceiling doing its job and the operator should be able to see which one.
    out += ["", "Ceilings, derived from the declared scale unless stated:"]
    out += _render_table(
        ("LIMIT", "VALUE", "ORIGIN", "BOUNDS"),
        [(name, f"{value:,}", origin, description)
         for name, value, origin, description
         in config.limits.describe(config.scale)])

    bounds = _bound_rows(plan)
    if bounds:
        out += ["", "What each dataset chooses to publish less than all of:"]
        out += _render_table(("OPTION", "VALUE", "ORIGIN", "MEANING"), bounds)

    deferred = [entry for entry in plan.enabled if entry.deferred]
    if deferred:
        out += ["", "Checked against the live appliance, not by this command:"]
        for entry in deferred:
            out.append(f"  {entry.name} ({entry.provider.name}): "
                       f"{', '.join(entry.deferred)}")

    if config.warnings:
        out += [""] + [f"warning: {message}" for message in config.warnings]

    out += [""]
    if plan.fits:
        out.append("Plan fits the declared budget.")
    else:
        out.append("Plan EXCEEDS the declared budget:")
        for target in plan.overages:
            out.append(f"  {target.target}: {target.overage_reason}")
        out.append(
            "  Raise the budget, lengthen an interval, choose a cheaper provider, "
            "or disable a dataset.")
    return "\n".join(out)
