#!/usr/bin/env python
"""Run ise-exporter3 against a synthetic ISE at production scale.

v3's production defaults target ~100,000 endpoints and ~5,000 NADs, and the
plan says what that should cost. This runs it: the real scheduler, the real
transports, the real datasets and the real publication boundary against
`tools/fake_ise3.py`, on a virtual clock, and reports what actually happened --
cost per target against the declared budget, series and scrape size, cache
convergence, Data Connect duty cycle, and anything that hit a ceiling.

    nix develop -c python tools/simulate_scale3.py
    nix develop -c python tools/simulate_scale3.py --hours 24 --sessions 40000
    nix develop -c python tools/simulate_scale3.py --json report.json

What it measures is cost, cardinality, convergence and pacing. What it cannot
measure is whether a number is right: the appliance is synthetic, so aggregate
values are plausible rather than meaningful, and the appliance latency model is
a declared assumption (printed in the report, scaled by --latency-scale).
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import time
import tomllib
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.fake_ise3 import (                                   # noqa: E402
    Estate,
    FakeIseHttp,
    Latency,
    SimulatedDataConnect,
    SimulatedRestTransport,
    VirtualClock,
    VirtualTime,
)


DEFAULT_CONFIG = "ise-exporter3.toml.example"


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(
        prog="simulate_scale3",
        description="Run ise-exporter3 against a synthetic ISE at production scale")
    parser.add_argument("--config", default=DEFAULT_CONFIG,
                        help=f"configuration to simulate (default: {DEFAULT_CONFIG})")
    parser.add_argument("--hours", type=float, default=6.0,
                        help="simulated hours to run (default: 6)")
    parser.add_argument("--nads", type=int, default=5000)
    parser.add_argument("--endpoints", type=int, default=100_000)
    parser.add_argument("--sessions", type=int, default=20_000)
    parser.add_argument("--accounts", type=int, default=1000)
    parser.add_argument("--churn-per-hour", type=float, default=0.12,
                        help="fraction of the active set that turns over each "
                             "hour (default 0.12, the 1%%-per-5-minutes the cost "
                             "model declares)")
    parser.add_argument("--latency-scale", type=float, default=1.0,
                        help="multiply every modelled appliance latency")
    parser.add_argument("--json", metavar="PATH",
                        help="also write the measurements as JSON")
    parser.add_argument("--show-plan", action="store_true",
                        help="print the resolved plan before running")
    parser.add_argument("--verbose", action="store_true",
                        help="let the exporter's own warnings through; they are "
                             "summarised in the report either way")
    return parser.parse_args(argv)


def rss_mib():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def load_configuration(path, scale, state_directory, appliance_host="127.0.0.1"):
    """Take the shipped configuration and point it at the fake appliance."""
    from ise_exporter3.config import Config

    document = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    document["scale"] = dict(scale)
    exporter = dict(document.get("exporter", {}))
    exporter["state_db"] = str(Path(state_directory) / "state.sqlite3")
    exporter["log_level"] = "WARNING"
    document["exporter"] = exporter

    targets = {name: dict(values) for name, values in document["targets"].items()}
    for name in ("pan", "mnt", "oracle"):
        targets[name]["host"] = appliance_host
        targets[name]["verify_tls"] = False
        targets[name].pop("ca_bundle", None)
    document["targets"] = targets

    environment = dict(os.environ)
    environment.setdefault("ISE_PASS", "simulated")
    environment.setdefault("ISE_DATACONNECT_PASSWORD", "simulated")
    return Config.from_document(document, path=str(path), environ=environment)


def declared_views():
    """Every reporting view the shipped datasets declare they need."""
    from ise_exporter3 import datasets as registry
    from ise_exporter3.datasets import psn_performance

    views = {}
    for dataset in registry.all_datasets():
        for provider in dataset.providers:
            for requirement in provider.requires:
                if str(requirement).startswith("view:"):
                    name = str(requirement).split(":", 1)[1].upper()
                    views.setdefault(name, {"ISE_NODE", "TIMESTAMP"})
    # psn_performance selects optional columns against the catalog, so this pair
    # has to carry its real column set or the dataset degrades for the wrong
    # reason.
    views.setdefault("KEY_PERFORMANCE_METRICS", set()).update(
        {"ISE_NODE", "LOGGED_TIME", *(name.upper() for name in psn_performance._KPI_COLUMNS)})
    views.setdefault("SYSTEM_SUMMARY", set()).update(
        {"ISE_NODE", "TIMESTAMP",
         *(name.upper() for name in psn_performance._SYSTEM_COLUMNS)})
    return views


def metric_samples(name):
    """Yield (labels, value) for one metric family from the global registry."""
    from prometheus_client import REGISTRY

    for family in REGISTRY.collect():
        for sample in family.samples:
            if sample.name == name:
                yield sample.labels, sample.value


def series_by_family():
    from prometheus_client import REGISTRY

    counts = {}
    for family in REGISTRY.collect():
        if not family.samples:
            continue
        counts[family.name] = counts.get(family.name, 0) + len(family.samples)
    return counts


class Recorder:
    """Per-collection facts the exporter's own telemetry does not keep."""

    def __init__(self, clock, appliance):
        self.clock = clock
        self.appliance = appliance
        self.events = []
        self.converged = {}

    def wrap(self, scheduler):
        run = scheduler.runner.run

        def recorded(entry, transport):
            started_virtual = self.clock.time()
            charged = dict(self.clock.charged)
            slept = self.clock.slept
            # The fake appliance runs in this process, so its own CPU has to come
            # off the top or every collection would look more expensive than it
            # is. What is left is the exporter's real cost.
            appliance_cpu = self.appliance.cpu_seconds
            started_real = time.perf_counter()
            outcome = run(entry, transport)
            elapsed = time.perf_counter() - started_real
            worked = sum(self.clock.charged.get(target, 0.0) - charged.get(target, 0.0)
                         for target in ("pan", "mnt", "oracle"))
            self.events.append({
                "dataset": entry.name,
                "provider": outcome.provider,
                "ok": outcome.ok,
                "reason": outcome.reason,
                "detail": outcome.detail,
                "appliance_seconds": worked,
                "wait_seconds": self.clock.slept - slept,
                "elapsed_seconds": self.clock.time() - started_virtual,
                "exporter_seconds": max(
                    0.0, elapsed - (self.appliance.cpu_seconds - appliance_cpu)),
                "at": started_virtual,
            })
            self._note_coverage(started_virtual)
            return outcome

        scheduler.runner.run = recorded

    def _note_coverage(self, elapsed_since):
        # "Warm" is 99%, not 100%: with real session churn a cache that is
        # keeping up still has a handful of just-arrived endpoints outstanding
        # on any given cycle, and waiting for an exact 1.0 would report a
        # converged cache as never converged.
        for labels, value in metric_samples("ise3_detail_cache_coverage"):
            cache = labels.get("cache", "unknown")
            if value >= 0.99 and cache not in self.converged:
                self.converged[cache] = self.clock.time()
        del elapsed_since


def run_simulation(arguments):
    import logging

    from ise_exporter3.plan import build_plan, render_plan
    from ise_exporter3.scheduler import TICK_SECONDS, Scheduler
    from ise_exporter3 import detail_cache
    from ise_exporter3.snapshots import LockedCollectorRegistry
    from ise_exporter3.transports import dataconnect as dataconnect_module

    # The exporter warns per collection about anything notable; over a 24-hour
    # soak that is thousands of identical lines. The report counts them instead.
    logging.getLogger("ise_exporter3").setLevel(
        logging.WARNING if arguments.verbose else logging.ERROR)

    scale = {"nads": arguments.nads, "endpoints": arguments.endpoints,
             "sessions": arguments.sessions, "accounts": arguments.accounts}

    import tempfile
    workspace = tempfile.TemporaryDirectory(prefix="ise3-scale-")
    baseline_rss = rss_mib()

    print(f"building a synthetic estate: {arguments.nads:,} NADs, "
          f"{arguments.endpoints:,} endpoints, {arguments.sessions:,} sessions, "
          f"{arguments.accounts:,} accounts", file=sys.stderr)
    build_started = time.perf_counter()
    clock = VirtualClock()
    estate = Estate(nads=arguments.nads, endpoints=arguments.endpoints,
                    sessions=arguments.sessions, accounts=arguments.accounts,
                    churn_per_hour=arguments.churn_per_hour, clock=clock)
    estate_seconds = time.perf_counter() - build_started
    estate_rss = rss_mib() - baseline_rss

    latency = Latency(arguments.latency_scale)
    appliance = FakeIseHttp(estate, clock, latency, directory=workspace.name)
    appliance.start()

    # The detail caches age their entries against the wall clock. A simulated
    # day passes in minutes of real time, so without this a 60-second
    # active-list cache would never expire and the whole run would collect
    # against one frozen snapshot of the fleet.
    detail_cache.time = VirtualTime(clock)

    config = load_configuration(arguments.config, scale, workspace.name)
    plan = build_plan(config)
    if arguments.show_plan:
        print(render_plan(plan))

    oracle = SimulatedDataConnect(config, estate, clock, latency)
    oracle.catalog(declared_views())
    # The Data Connect transport reads time through its own lane clock, so its
    # cooldowns, its minimum inter-query interval and its statement timeout are
    # all enforced without stalling the other targets' timeline.
    dataconnect_module.time = VirtualTime(oracle.lane)
    transports = {
        "pan": SimulatedRestTransport(config, "pan", appliance),
        "mnt": SimulatedRestTransport(config, "mnt", appliance),
        "oracle": oracle,
    }

    registry = LockedCollectorRegistry()
    scheduler = Scheduler(config, plan, transports, asynchronous=False,
                          clock=clock.time)
    recorder = Recorder(clock, appliance)
    recorder.wrap(scheduler)

    started = clock.time()
    deadline = started + arguments.hours * 3600
    scheduler.bootstrap(started)
    wall_started = time.perf_counter()
    ticks = 0
    steady_from = deadline - 3600
    steady_baseline = None
    while clock.time() < deadline:
        if steady_baseline is None and clock.time() >= steady_from:
            # A cold start is not steady state: the warm-up burst would otherwise
            # be averaged into the hourly cost for the life of the run.
            steady_baseline = (clock.time(), request_totals())
        before = clock.time()
        scheduler.tick(clock.time())
        ticks += 1
        if clock.time() == before:
            clock.advance(TICK_SECONDS)
        if ticks % 200 == 0:
            print(f"  simulated {(clock.time() - started) / 3600:5.2f}h "
                  f"({time.perf_counter() - wall_started:5.1f}s real)",
                  file=sys.stderr)
    wall_seconds = time.perf_counter() - wall_started
    scheduler.publish_state_size()

    steady = None
    if steady_baseline is not None:
        since, baseline = steady_baseline
        window = max(clock.time() - since, 1e-9) / 3600.0
        current = request_totals()
        steady = {target: (current.get(target, 0) - baseline.get(target, 0)) / window
                  for target in current}

    measurements = measure(
        arguments, config, plan, estate, clock, appliance, oracle, recorder,
        registry, started, wall_seconds, estate_seconds, estate_rss, baseline_rss,
        steady)

    for transport in transports.values():
        transport.close()
    appliance.stop()
    workspace.cleanup()
    return measurements


def request_totals():
    """Requests sent per target so far, from the transport's own counter."""
    totals = defaultdict(float)
    for labels, value in metric_samples("ise3_api_requests_total"):
        totals[labels["target"]] += value
    return dict(totals)


# Which dataset fills each detail cache, so an unconverged cache can be scored
# against the cadence that actually fills it.
CACHE_OWNERS = {
    "ers_network_device": "network_devices",
    "ers_tacacs_user": "tacacs_config",
    "mnt_session_detail": "session_authorization",
    "mnt_active_list": "session_authorization",
}


def measure(arguments, config, plan, estate, clock, appliance, oracle, recorder,
            registry, started, wall_seconds, estate_seconds, estate_rss,
            baseline_rss, steady=None):
    from prometheus_client import generate_latest

    from ise_exporter3.snapshots import MAX_SNAPSHOT_SAMPLES, SOFT_SAMPLE_WARNING

    hours = max((clock.time() - started) / 3600.0, 1e-9)

    scrapes = []
    for _attempt in range(5):
        scrape_started = time.perf_counter()
        payload = generate_latest(registry)
        scrapes.append((time.perf_counter() - scrape_started, len(payload)))
    scrape_seconds = sorted(seconds for seconds, _size in scrapes)[2]
    scrape_bytes = scrapes[-1][1]

    families = series_by_family()
    total_series = sum(families.values())

    per_dataset = defaultdict(lambda: {
        "collections": 0, "failures": 0, "appliance_seconds": 0.0,
        "wait_seconds": 0.0, "exporter_seconds": 0.0,
        "worst_exporter_seconds": 0.0, "worst_elapsed_seconds": 0.0,
        "provider": "", "reason": "", "detail": ""})
    for event in recorder.events:
        entry = per_dataset[event["dataset"]]
        entry["collections"] += 1
        entry["provider"] = event["provider"]
        entry["appliance_seconds"] += event["appliance_seconds"]
        entry["wait_seconds"] += event["wait_seconds"]
        entry["exporter_seconds"] += event["exporter_seconds"]
        entry["worst_exporter_seconds"] = max(
            entry["worst_exporter_seconds"], event["exporter_seconds"])
        entry["worst_elapsed_seconds"] = max(
            entry["worst_elapsed_seconds"], event["elapsed_seconds"])
        if not event["ok"]:
            entry["failures"] += 1
            entry["reason"] = event["reason"]
            entry["detail"] = event["detail"]

    series = {labels["dataset"]: value
              for labels, value in metric_samples("ise3_dataset_series")}
    up = {labels["dataset"]: value
          for labels, value in metric_samples("ise3_dataset_up")}

    requests = defaultdict(float)
    api_requests = defaultdict(float)
    for labels, value in metric_samples("ise3_api_requests_total"):
        requests[labels["target"]] += value
        api_requests[f"{labels['target']}/{labels['api']}"] += value

    db_seconds = sum(value for _labels, value
                     in metric_samples("ise3_load_measured_db_seconds_total"))
    queries = defaultdict(float)
    for labels, value in metric_samples("ise3_dataconnect_queries_total"):
        queries[f"{labels['view']}/{labels['result']}"] += value

    caches = {}
    for name in ("entries", "coverage", "deferred"):
        for labels, value in metric_samples(f"ise3_detail_cache_{name}"):
            caches.setdefault(labels["cache"], {})[name] = value
    for cache, when in recorder.converged.items():
        caches.setdefault(cache, {})["converged_after_seconds"] = when - started

    fetched = defaultdict(float)
    for labels, value in metric_samples("ise3_detail_fetches_total"):
        if labels.get("result") in ("fetched", "inline"):
            fetched[labels["cache"]] += value
    cadence = {}
    for name, entry in sorted(per_dataset.items()):
        moments = [event["at"] for event in recorder.events
                   if event["dataset"] == name]
        if len(moments) > 1:
            cadence[name] = (max(moments) - min(moments)) / (len(moments) - 1)
    for cache, values in caches.items():
        coverage = values.get("coverage", 0)
        entries = values.get("entries", 0)
        owner = CACHE_OWNERS.get(cache, "")
        runs = per_dataset.get(owner, {}).get("collections", 0)
        if coverage >= 1.0 or not coverage or not runs:
            continue
        population = entries / coverage
        per_run = fetched.get(cache, 0) / runs
        interval = cadence.get(owner) or config.datasets.get(owner, None)
        interval = getattr(interval, "interval", None) or cadence.get(owner) or 0
        if per_run > 0 and interval:
            values["projected_full_coverage_hours"] = (
                (population - entries) / per_run * interval / 3600.0)
        values["fetches_per_run"] = per_run
        values["population"] = population

    planned = {}
    for labels, value in metric_samples("ise3_load_planned_requests_per_hour"):
        planned.setdefault(labels["target"], {})["requests_per_hour"] = value
    for labels, value in metric_samples("ise3_load_planned_duty_cycle_percent"):
        planned.setdefault(labels["target"], {})["duty_cycle_percent"] = value
    for labels, value in metric_samples("ise3_load_budget_requests_per_hour"):
        planned.setdefault(labels["target"], {})["budget_requests_per_hour"] = value
    for labels, value in metric_samples("ise3_load_budget_duty_cycle_percent"):
        planned.setdefault(labels["target"], {})["budget_duty_cycle_percent"] = value

    state_bytes = next(
        (value for _labels, value in metric_samples("ise3_exporter_state_bytes")), 0)

    return {
        "scale": {"nads": arguments.nads, "endpoints": arguments.endpoints,
                  "sessions": arguments.sessions, "accounts": arguments.accounts,
                  "psns": len(estate.psns), "profile": config.profile},
        "run": {
            "simulated_hours": hours,
            "wall_seconds": wall_seconds,
            "collections": len(recorder.events),
            "latency_scale": arguments.latency_scale,
            "churn_per_hour": arguments.churn_per_hour,
            "config": arguments.config,
        },
        "plan": {
            "fits_budget": plan.fits,
            "unresolved": sorted(entry.name for entry in (plan.unresolved or ())),
            "targets": planned,
        },
        "datasets": {
            name: {
                **entry,
                "series": series.get(name, 0),
                "up": up.get(name, 0),
                "mean_exporter_seconds": entry["exporter_seconds"] / entry["collections"],
                "mean_appliance_seconds": entry["appliance_seconds"] / entry["collections"],
                "mean_wait_seconds": entry["wait_seconds"] / entry["collections"],
            }
            for name, entry in sorted(per_dataset.items())
        },
        "load": {
            "requests": {target: value for target, value in sorted(requests.items())},
            "steady_state_requests_per_hour": steady or {},
            "requests_per_hour": {target: value / hours
                                  for target, value in sorted(requests.items())},
            "by_api": dict(sorted(api_requests.items(), key=lambda item: -item[1])),
            "oracle_db_seconds": db_seconds,
            "oracle_duty_cycle_percent": db_seconds / (hours * 36.0),
            "oracle_queries": dict(sorted(queries.items())),
            "oracle_statements": len(oracle.statements),
            "appliance_seconds_by_target": dict(clock.charged),
            "paced_seconds": clock.slept,
            "oracle_pacing_debt_hours": oracle.lane.debt / 3600.0,
            "lane_utilisation": {
                target: clock.charged.get(target, 0.0) / (hours * 3600.0)
                for target in ("pan", "mnt", "oracle")},
        },
        "series": {
            "total": total_series,
            "scrape_bytes": scrape_bytes,
            "scrape_seconds": scrape_seconds,
            "largest_families": dict(sorted(
                families.items(), key=lambda item: -item[1])[:12]),
        },
        "caches": caches,
        "ceilings": {
            "soft_sample_warning": SOFT_SAMPLE_WARNING,
            "hard_sample_limit": MAX_SNAPSHOT_SAMPLES,
            "over_soft_limit": {
                name: series.get(name, 0)
                for name in sorted(per_dataset)
                if series.get(name, 0) > SOFT_SAMPLE_WARNING},
            "truncated_breakdowns": sorted(
                f"{labels['dataset']}/{labels['breakdown']}"
                for labels, value in metric_samples("ise3_topk_truncated")
                if value),
            "failure_reasons": sorted({
                event["reason"] for event in recorder.events if not event["ok"]}),
        },
        "footprint": {
            "appliance_cpu_seconds": appliance.cpu_seconds,
            "state_bytes": state_bytes,
            "rss_mib": rss_mib(),
            "baseline_rss_mib": baseline_rss,
            "estate_rss_mib": estate_rss,
            "estate_build_seconds": estate_seconds,
            "active_list_bytes": len(estate.active_list_xml()),
        },
        "unhandled_requests": sorted(set(appliance.unhandled)),
    }


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def _bytes(value):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(value) < 1024 or unit == "GiB":
            return f"{value:,.1f} {unit}" if unit != "B" else f"{value:,.0f} B"
        value /= 1024.0
    return f"{value:.1f} GiB"


def report(measurements):
    scale = measurements["scale"]
    run = measurements["run"]
    lines = []
    add = lines.append

    add("=" * 78)
    add(f"ise-exporter3 at {scale['profile']} scale: {scale['nads']:,} NADs, "
        f"{scale['endpoints']:,} endpoints, {scale['sessions']:,} sessions, "
        f"{scale['psns']} PSNs")
    add(f"{run['simulated_hours']:.1f} simulated hours of {run['config']} in "
        f"{run['wall_seconds']:.0f}s real, {run['collections']} collections")
    add("=" * 78)

    add("")
    add("DATASETS")
    add(f"  {'dataset':<23}{'provider':<12}{'runs':>5}{'fail':>5}{'series':>7}"
        f"{'cpu s':>8}{'worst':>7}{'ISE s':>8}{'wait s':>9}")
    for name, entry in measurements["datasets"].items():
        add(f"  {name:<23}{entry['provider']:<12}{entry['collections']:>5}"
            f"{entry['failures']:>5}{entry['series']:>7.0f}"
            f"{entry['mean_exporter_seconds']:>8.3f}"
            f"{entry['worst_exporter_seconds']:>7.2f}"
            f"{entry['mean_appliance_seconds']:>8.1f}"
            f"{entry['mean_wait_seconds']:>9.1f}")
    failing = {name: entry for name, entry in measurements["datasets"].items()
               if entry["failures"]}
    if failing:
        add("")
        add("  failures")
        for name, entry in failing.items():
            add(f"    {name}: {entry['failures']} x {entry['reason']} "
                f"-- {entry['detail'][:90]}")
    unresolved = measurements["plan"]["unresolved"]
    if unresolved:
        add(f"  unresolved by the plan: {', '.join(unresolved)}")

    add("")
    add("LOAD AGAINST BUDGET")
    load = measurements["load"]
    for target, planned in sorted(measurements["plan"]["targets"].items()):
        measured = load["requests_per_hour"].get(target)
        if target == "oracle":
            add(f"  oracle   duty cycle  planned {planned.get('duty_cycle_percent', 0):.2f}%"
                f"  measured {load['oracle_duty_cycle_percent']:.2f}%"
                f"  budget {planned.get('budget_duty_cycle_percent', 0):.2f}%"
                f"  ({load['oracle_db_seconds']:,.0f} db-seconds,"
                f" {load['oracle_statements']} statements)")
            continue
        if measured is None:
            continue
        budget = planned.get("budget_requests_per_hour", 0)
        share = (measured / budget * 100) if budget else 0
        steady_rate = load["steady_state_requests_per_hour"].get(target)
        steady_text = (f"  final hour {steady_rate:>8.0f}"
                       if steady_rate is not None else "")
        add(f"  {target:<9}requests/h  planned {planned.get('requests_per_hour', 0):>8.0f}"
            f"  measured {measured:>8.0f}  budget {budget:>8.0f}  ({share:.0f}% used)"
            f"{steady_text}")
    add("  lane utilisation: " + "  ".join(
        f"{target} {share * 100:.1f}%"
        for target, share in load["lane_utilisation"].items())
        + f"   oracle pacing debt {load['oracle_pacing_debt_hours']:.1f} h")
    add("  busiest interfaces:")
    for api, count in list(load["by_api"].items())[:6]:
        add(f"    {api:<32}{count:>10,.0f} requests")

    add("")
    add("PROMETHEUS SURFACE")
    series = measurements["series"]
    add(f"  {series['total']:,} series, scrape {_bytes(series['scrape_bytes'])} "
        f"in {series['scrape_seconds'] * 1000:.0f} ms")
    add("  largest families:")
    for name, count in list(series["largest_families"].items())[:8]:
        add(f"    {name:<48}{count:>8,}")

    add("")
    add("CONVERGENCE")
    for cache, values in sorted(measurements["caches"].items()):
        converged = values.get("converged_after_seconds")
        projected = values.get("projected_full_coverage_hours")
        if converged is not None:
            when = f"warm (>=99%) after {converged / 60:.0f} min"
        elif projected:
            when = (f"NOT warm -- {values.get('fetches_per_run', 0):.0f}/run implies "
                    f"{projected:.0f} h to full coverage")
        else:
            when = "NOT warm at the end of the run"
        add(f"  {cache:<28}{values.get('entries', 0):>8,.0f} entries  "
            f"coverage {values.get('coverage', 0):.3f}  "
            f"deferred {values.get('deferred', 0):>6,.0f}  {when}")

    add("")
    add("CEILINGS")
    ceilings = measurements["ceilings"]
    if ceilings["over_soft_limit"]:
        for name, count in ceilings["over_soft_limit"].items():
            add(f"  {name} published {count:,.0f} series, past the "
                f"{ceilings['soft_sample_warning']:,} soft limit "
                f"({count / ceilings['hard_sample_limit'] * 100:.0f}% of the hard one)")
    else:
        add(f"  no dataset crossed the {ceilings['soft_sample_warning']:,}-sample "
            f"soft limit")
    if ceilings["truncated_breakdowns"]:
        add(f"  truncated breakdowns: {', '.join(ceilings['truncated_breakdowns'])}")
    if ceilings["failure_reasons"]:
        add(f"  failure reasons seen: {', '.join(ceilings['failure_reasons'])}")

    add("")
    add("FOOTPRINT")
    footprint = measurements["footprint"]
    add(f"  on-disk state {_bytes(footprint['state_bytes'])}"
        f"   process RSS {footprint['rss_mib']:,.0f} MiB"
        f"   (simulated ISE accounts for {footprint['estate_rss_mib']:,.0f} MiB;"
        f" ActiveList alone is {_bytes(footprint['active_list_bytes'])})")
    if measurements["unhandled_requests"]:
        add(f"  UNHANDLED appliance requests: "
            f"{', '.join(measurements['unhandled_requests'][:6])}")
    add("")
    return "\n".join(lines)


def main(argv=None):
    arguments = parse_arguments(argv)
    measurements = run_simulation(arguments)
    print(report(measurements))
    if arguments.json:
        Path(arguments.json).write_text(
            json.dumps(measurements, indent=2, default=str), encoding="utf-8")
        print(f"measurements written to {arguments.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
