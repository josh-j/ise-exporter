"""Entry point.

``plan`` comes first on purpose: you should be able to see what a configuration
will cost each ISE persona before anything connects to an appliance. It remains
offline unless ``--live-scale`` is requested. ``run`` executes that same plan.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import signal
import sys
import threading

from . import __version__
from .api import OperatorApi
from .config import Config, ConfigError
from .credentials import (
    DEFAULT_CREDENTIALS_FILE,
    SECRET_KEYS,
    CredentialsError,
    load_credentials,
)
from .explore import Explorer
from .plan import build_plan, render_plan
from .scale_discovery import discover_scale, render_scale_discovery
from .scheduler import Scheduler
from .server import HttpServer
from .snapshots import LockedCollectorRegistry
from .transports import build_transports, close_transports


logger = logging.getLogger("ise_exporter3")

EXIT_OK = 0
EXIT_OVER_BUDGET = 1
EXIT_CONFIG_ERROR = 2


def _add_config_arguments(parser):
    parser.add_argument(
        "--config", "-c", metavar="PATH",
        help="configuration file (default: $ISE_EXPORTER3_CONFIG, "
             "else /etc/ise-exporter3/config.toml)")
    credentials = parser.add_mutually_exclusive_group()
    credentials.add_argument(
        "--credentials-file", metavar="PATH",
        help="load exporter passwords from this root/private EnvironmentFile "
             "(default: $ISE_EXPORTER3_CREDENTIALS_FILE, else "
             f"{DEFAULT_CREDENTIALS_FILE} when readable)")
    credentials.add_argument(
        "--no-credentials-file", action="store_true",
        help="use only credentials already present in the process environment")


def _load_config(args):
    environment = dict(os.environ)
    loaded = ""
    if not args.no_credentials_file:
        path = (
            args.credentials_file
            or environment.get("ISE_EXPORTER3_CREDENTIALS_FILE")
            or DEFAULT_CREDENTIALS_FILE
        )
        environment, loaded = load_credentials(
            path,
            environ=environment,
            optional=args.credentials_file is None,
        )
        if (
            not loaded
            and args.credentials_file is None
            and Path(path).exists()
            and not os.access(path, os.R_OK)
            and not SECRET_KEYS.issubset(environment)
        ):
            logger.warning(
                "credentials file exists but is not readable: %s; run as root, "
                "use --credentials-file, or use --no-credentials-file for an "
                "environment-only plan",
                path,
            )
    config = Config.load(args.config, environ=environment)
    return config, loaded


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
                    "each ISE persona per hour. Offline unless --live-scale is used.")
    _add_config_arguments(plan_parser)
    plan_parser.add_argument(
        "--json", action="store_true", help="emit the plan as JSON")
    plan_parser.add_argument(
        "--strict", action="store_true",
        help="also fail when an enabled dataset has no viable provider")
    plan_parser.add_argument(
        "--live-scale", "--discover-scale", action="store_true",
        help="perform five bounded ISE reads, show the observed fleet counts, "
             "and preview the plan using them without changing the TOML")

    run_parser = subcommands.add_parser(
        "run", help="collect and serve metrics",
        description="Execute the plan: serve /metrics and collect each dataset "
                    "from its selected provider on its target's lane.")
    _add_config_arguments(run_parser)
    return parser


def command_plan(args):
    try:
        config, credentials_file = _load_config(args)
    except (ConfigError, CredentialsError) as error:
        logger.error("%s", error)
        return EXIT_CONFIG_ERROR

    discovery = None
    if args.live_scale:
        discovery = discover_scale(config)
        try:
            config = config.with_scale(discovery.scale)
        except ConfigError as error:
            logger.error("%s", error)
            return EXIT_CONFIG_ERROR

    plan = build_plan(config)
    if args.json:
        document = plan.to_dict()
        document["credentials_file"] = credentials_file or None
        if discovery is not None:
            document["scale_discovery"] = discovery.to_dict()
        print(json.dumps(document, indent=2))
    else:
        if credentials_file:
            print(f"credentials loaded from {credentials_file}")
        if discovery is not None:
            print(render_scale_discovery(discovery))
            print()
        print(render_plan(plan))

    if discovery is not None and not discovery.complete:
        return EXIT_CONFIG_ERROR
    if not plan.fits:
        return EXIT_OVER_BUDGET
    if args.strict and plan.unresolved:
        return EXIT_OVER_BUDGET
    return EXIT_OK


def command_run(args):
    try:
        config, _credentials_file = _load_config(args)
    except (ConfigError, CredentialsError) as error:
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
    # The explorer shares the scheduler's transport rather than building its
    # own: two transports would mean two in-process cooldowns, and the gate
    # file would be the only thing left keeping them apart.
    oracle = transports.get("oracle")
    explorer = Explorer(oracle) if oracle is not None else None
    # Two listeners, not one: Prometheus must reach /metrics from off-host, and
    # the operator API must not leave the host. Binding them together would mean
    # choosing one of those, and neither is the right thing to give up.
    metrics_http = HttpServer("0.0.0.0", config.exporter.port, registry)
    api_http = HttpServer(
        config.exporter.api_host, config.exporter.api_port, registry,
        routes=OperatorApi(config, plan, scheduler, explorer).routes())
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
