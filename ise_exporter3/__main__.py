"""Entry point.

``plan`` comes first on purpose: you should be able to see what a configuration
will cost each ISE persona before anything connects to an appliance. It needs no
credentials and no network. ``run`` executes that same plan.
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading

from . import __version__
from .api import OperatorApi
from .config import Config, ConfigError
from .plan import build_plan, render_plan
from .scheduler import Scheduler
from .server import HttpServer
from .snapshots import LockedCollectorRegistry
from .transports import build_transports, close_transports


logger = logging.getLogger("ise_exporter3")

EXIT_OK = 0
EXIT_OVER_BUDGET = 1
EXIT_CONFIG_ERROR = 2


def _add_config_argument(parser):
    parser.add_argument(
        "--config", "-c", metavar="PATH",
        help="configuration file (default: $ISE_EXPORTER3_CONFIG, "
             "else /etc/ise-exporter3/config.toml)")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="ise-exporter3",
        description="Cisco ISE exporter with provider adapters and declared load")
    parser.add_argument("--version", action="version",
                        version=f"ise-exporter3 {__version__}")
    subcommands = parser.add_subparsers(dest="command", required=True)

    plan_parser = subcommands.add_parser(
        "plan",
        help="show the resolved source and hourly load for every dataset",
        description="Resolve the configuration against the dataset registry and "
                    "report which source supplies each dataset and what it costs "
                    "each ISE persona per hour. Requires no appliance access.")
    _add_config_argument(plan_parser)
    plan_parser.add_argument(
        "--json", action="store_true", help="emit the plan as JSON")
    plan_parser.add_argument(
        "--strict", action="store_true",
        help="also fail when an enabled dataset has no viable provider")

    run_parser = subcommands.add_parser(
        "run", help="collect and serve metrics",
        description="Execute the plan: serve /metrics and collect each dataset "
                    "from its selected provider on its target's lane.")
    _add_config_argument(run_parser)
    return parser


def command_plan(args):
    try:
        config = Config.load(args.config)
    except ConfigError as error:
        logger.error("%s", error)
        return EXIT_CONFIG_ERROR

    plan = build_plan(config)
    if args.json:
        print(json.dumps(plan.to_dict(), indent=2))
    else:
        print(render_plan(plan))

    if not plan.fits:
        return EXIT_OVER_BUDGET
    if args.strict and plan.unresolved:
        return EXIT_OVER_BUDGET
    return EXIT_OK


def command_run(args):
    try:
        config = Config.load(args.config)
    except ConfigError as error:
        logger.error("%s", error)
        return EXIT_CONFIG_ERROR
    logging.getLogger().setLevel(config.exporter.log_level)

    plan = build_plan(config)
    # Print the plan on every start. An operator reading the journal after an
    # incident should not have to guess which source was in use or what the
    # exporter thought it was spending.
    logger.info("resolved plan:\n%s", render_plan(plan))
    for warning in config.warnings:
        logger.warning("%s", warning)

    if not plan.fits:
        for target in plan.overages:
            logger.error("over budget on %s: %s", target.target, target.overage_reason)
        if config.exporter.enforce_budget:
            logger.error(
                "refusing to start over budget; raise the ceiling, lengthen an "
                "interval, choose a cheaper provider, disable a dataset, or set "
                "exporter.enforce_budget = false")
            return EXIT_OVER_BUDGET

    transports = build_transports(config)
    if not transports:
        logger.error("no target is configured; nothing can be collected")
        return EXIT_CONFIG_ERROR

    shutdown = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: shutdown.set())
    signal.signal(signal.SIGINT, lambda *_: shutdown.set())

    registry = LockedCollectorRegistry()
    scheduler = Scheduler(config, plan, transports)
    # Two listeners, not one: Prometheus must reach /metrics from off-host, and
    # the operator API must not leave the host. Binding them together would mean
    # choosing one of those, and neither is the right thing to give up.
    metrics_http = HttpServer("0.0.0.0", config.exporter.port, registry)
    api_http = HttpServer(
        config.exporter.api_host, config.exporter.api_port, registry,
        routes=OperatorApi(config, plan, scheduler).routes())
    try:
        metrics_http.start()
        api_http.start()
        logger.info("operator API on http://%s:%d/api/v1",
                    config.exporter.api_host, config.exporter.api_port)
        scheduler.loop(shutdown)     # blocks until SIGTERM/SIGINT
    finally:
        api_http.stop()
        metrics_http.stop()
        close_transports(transports)
    logger.info("shutdown complete")
    return EXIT_OK


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(name)s - %(levelname)s - %(message)s")
    args = build_parser().parse_args(argv)
    if args.command == "plan":
        return command_plan(args)
    if args.command == "run":
        return command_run(args)
    raise AssertionError(f"unhandled command {args.command!r}")


if __name__ == "__main__":
    sys.exit(main())
