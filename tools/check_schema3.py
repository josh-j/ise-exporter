#!/usr/bin/env python
"""Validate the shipped Data Connect SQL against a live ISE catalogue.

This is the check the scale simulator structurally cannot perform. `fake_ise3`
answers every statement from that statement's own SELECT list, so a column that
does not exist on a real appliance is invisible there and fails only on first
contact with an MnT -- which is exactly the risk `docs/v3-status.md` records as
"expect to fix column names on first contact".

It costs the appliance **one dictionary read**. `query_catalog` keeps the pacing
gate, the timeout and every ceiling but is exempt from duty-cycle amplification,
because the Oracle data dictionary does not scale with the event history. The
result is cached to JSON so re-runs cost nothing at all.

    python tools/check_schema3.py --config lab.toml --cache schema.json

Two things are settled here:

- **view requirements**: every `view:NAME` a provider declares, resolved through
  the same `Transport.satisfies` the runtime uses, so this agrees with what the
  scheduler would decide rather than approximating it;
- **column names**: every identifier the generated SQL names, checked against
  the columns the catalogue reports for the views that statement reads.

Exit status is 0 when everything resolves, 1 when anything does not, so this can
gate a lab run.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


# SQL keywords and Oracle built-ins that appear in these statements and are not
# columns. A name missing from here shows up as a false positive, which is the
# safe direction: it asks a human to look rather than staying quiet.
NOISE = set("""
SELECT FROM WHERE GROUP BY ORDER HAVING AND OR NOT IN AS ON CASE WHEN THEN ELSE
END NULL IS COUNT SUM MAX MIN AVG DISTINCT OVER PARTITION ROW_NUMBER GROUPING
SETS COALESCE NVL CAST TIMESTAMP DATE INTERVAL NUMTODSINTERVAL SYSTIMESTAMP
FETCH FIRST NEXT ROWS ONLY OFFSET UNION ALL LIKE TRIM LOWER UPPER REGEXP_SUBSTR
HOUR DUAL DESC ASC LEFT RIGHT JOIN INNER OUTER EXISTS BETWEEN TO_CHAR TO_DATE
TO_NUMBER SUBSTR INSTR LENGTH ROUND FLOOR CEIL DECODE EXTRACT
""".split())

# Aliases the statements define for their own projections.
ALIASES = set("""
dimension value events group_total passed failed endpoints grouped nad policy
status recent_rows age_seconds view_name row_num total psn seconds
mean_response timed unprofiled posture_yes posture_no
""".upper().split())


def load_schema(config, cache_path):
    """The live catalogue, read once and cached."""
    if cache_path and cache_path.exists():
        return {k: set(v) for k, v in json.loads(cache_path.read_text()).items()}
    from ise_exporter3.transports.dataconnect import DataConnectTransport

    schema = DataConnectTransport(config).discover_schema()
    if cache_path:
        cache_path.write_text(
            json.dumps({k: sorted(v) for k, v in schema.items()}, indent=1))
    return schema


def columns_in(sql):
    """Identifiers a statement names, excluding its own string literals.

    Stripping quoted text first is not cosmetic: without it every NVL default
    and every dimension label -- 'unknown', 'none', LIKE 'PASS%' -- is reported
    as a missing column, and the noise buries the one finding that matters.
    """
    sql = re.sub(r"'[^']*'", " ", sql)
    words = {word.upper() for word in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", sql)}
    return words - NOISE - ALIASES


def views_in(sql, schema):
    return {name for name in schema if re.search(rf"\b{name}\b", sql, re.IGNORECASE)}


def statements_for(limits, schema):
    """Every statement the reporting datasets build, by dataset and name."""
    from ise_exporter3.datasets import (
        endpoint_inventory,
        nad_health,
        posture_history,
        psn_performance,
        radius_errors,
        radius_reporting,
        source_freshness,
        tacacs_activity,
    )

    built = {
        "radius_errors": radius_errors.statements(6, limits),
        "radius_reporting": radius_reporting.statements(6, limits),
        "posture_history": posture_history.statements(6, limits),
        "tacacs_activity": tacacs_activity.statements(6, limits),
        "endpoint_inventory": endpoint_inventory.statements(limits),
        "source_freshness": source_freshness.statements(),
        "psn_performance": psn_performance.statements(limits, schema),
        # Built through a private pager rather than a statements() map.
        "nad_health": {"by_nad": nad_health._page(6, 0, limits)},
    }
    for dataset, mapping in built.items():
        for name, sql in mapping.items():
            yield dataset, name, sql


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="check_schema3",
        description="Validate the shipped Data Connect SQL against a live ISE")
    parser.add_argument("--config", required=True)
    parser.add_argument("--cache", help="read/write the catalogue here")
    arguments = parser.parse_args(argv)

    from ise_exporter3 import datasets as registry
    from ise_exporter3.config import Config
    from ise_exporter3.transports.dataconnect import DataConnectTransport

    config = Config.load(arguments.config)
    cache_path = pathlib.Path(arguments.cache) if arguments.cache else None
    schema = load_schema(config, cache_path)
    print(f"catalogue: {len(schema)} views, "
          f"{sum(len(c) for c in schema.values())} columns")

    # Resolved through the same call the scheduler uses, so this agrees with
    # what the runtime would decide rather than approximating it.
    prober = DataConnectTransport.__new__(DataConnectTransport)
    prober._schema = schema

    failures = 0
    print("\nview requirements:")
    for dataset in registry.all_datasets():
        for provider in dataset.providers:
            if not any(str(r).startswith("view:") for r in provider.requires):
                continue
            ok, reason, detail = prober.satisfies(provider.requires)
            print(f"  {'ok ' if ok else 'MISSING'} "
                  f"{dataset.name}/{provider.name}")
            if not ok:
                failures += 1
                print(f"        {reason}: {detail}")

    print("\ncolumns named by the generated SQL:")
    unknown_total = 0
    for dataset, name, sql in statements_for(config.limits, schema):
        views = views_in(sql, schema)
        if not views:
            continue
        known = set().union(*(schema[view] for view in views))
        unknown = sorted(
            column for column in columns_in(sql)
            if column not in known and column not in schema)
        if unknown:
            unknown_total += len(unknown)
            failures += 1
            print(f"  {dataset}/{name} ({', '.join(sorted(views))}):")
            for column in unknown:
                print(f"      {column}")
    if not unknown_total:
        print("  every column exists on the live catalogue")

    print()
    print("SCHEMA OK" if not failures else f"{failures} problem(s) found")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
