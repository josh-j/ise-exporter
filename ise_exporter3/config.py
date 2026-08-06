"""TOML configuration: six sections, and everything else is derived.

v2 had roughly ninety flat fields, and its safety limits were spread across
config hard ranges, client-side clamps and collector helpers, so no one could
read a config and say what it would do. Here a config answers five questions:

    profile   how big is this deployment, and what is it allowed to spend
    scale     how many NADs / endpoints / sessions / Device Admin entities
    targets   which ISE personas can be reached, and as whom
    budget    the ceiling per target
    limits    the ceilings on what one statement, batch and snapshot may return
    datasets  which are on, which source to prefer, how often, and any bound
              a dataset puts on its own breakdowns

Pacing and cooldowns are still not knobs: they fall out of the budget and the
provider's declared cost. The one timeout an operator can set is a heavy Data
Connect dataset's declared ``statement_timeout`` option, because the right
budget for a multi-hour aggregate depends on fleet size, not on the transport. Row ceilings used to be in that list, as
module constants nobody could see -- and three of them silently contradicted
each other until ``tacacs_activity`` stopped publishing at the declared scale.
They are derived from ``[scale]`` now and shown in ``[limits]``, because a
ceiling an operator cannot see or name is not a contract. See ``limits.py`` for
the derivation and for which values are refused outright.

The same rule governs ``datasets.<name>.options``: a dataset that bounds one of
its own dimensions must declare that bound here, so no breakdown is narrowed by
a constant in a collector. Hard safety floors still live in code.

Secrets never appear here: passwords come from the environment only.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib

from . import limits as limits_module
from .model import SCALE_DIMENSIONS, TARGETS, Scale


DEFAULT_CONFIG_FILE = "/etc/ise-exporter3/config.toml"

# Passwords are environment-only, one per target that needs one. MnT reuses the
# PAN account, so it has no credential of its own.
SECRET_ENV = MappingProxyType({
    "pan": "ISE_PASS",
    "oracle": "ISE_DATACONNECT_PASSWORD",
    "pxgrid": "ISE_PXGRID_PASSWORD",
})

_DURATION = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$")
_DURATION_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}

_TOP_LEVEL = frozenset({"profile", "exporter", "scale", "targets", "budget",
                        "limits", "datasets"})

_EXPORTER_FIELDS = frozenset({
    "port", "api_port", "api_host", "state_db", "log_level", "enforce_budget",
    "scrape_interval",
})

# Declared per target so an unknown or misplaced key names itself in the error.
_TARGET_FIELDS = MappingProxyType({
    "pan": frozenset({"host", "user", "ers_port", "ca_bundle", "verify_tls",
                      "request_timeout"}),
    "mnt": frozenset({"host", "ca_bundle", "verify_tls", "request_timeout"}),
    "oracle": frozenset({"host", "port", "service", "user", "ca_bundle",
                         "verify_tls"}),
    "pxgrid": frozenset({"host", "port", "node_name", "client_cert",
                         "client_key", "ca_bundle", "verify_tls",
                         "request_timeout"}),
})

_BUDGET_FIELDS = frozenset({"requests_per_hour", "duty_cycle_percent",
                            "warmup_requests_per_hour"})
_DATASET_FIELDS = frozenset({"enabled", "providers", "interval", "options"})

# A profile is a size and a spending ceiling. It deliberately does not override
# cadences: those belong to the dataset that knows what its data is worth.
#
# "production" is the default on purpose. The deployment this exporter is built
# for is ~90,000 endpoints, ~60,000 concurrent sessions and ~5,000 NADs, and an
# operator who installs it without choosing a profile must get that size. A
# profile that under-declares the fleet makes every cost estimate optimistic,
# which is the one direction the load model must not fail in.
DEFAULT_PROFILE = "production"

PROFILES = MappingProxyType({
    "lab": {
        "scale": {"nads": 200, "endpoints": 5_000, "sessions": 1_000,
                  "accounts": 50, "policy_sets": 20},
        "budget": {
            "pan": {"requests_per_hour": 600},
            "mnt": {"requests_per_hour": 600},
            "pxgrid": {"requests_per_hour": 200},
            "oracle": {"duty_cycle_percent": 1.0},
        },
    },
    "production": {
        "scale": {"nads": 5_000, "endpoints": 90_000, "sessions": 60_000,
                  "accounts": 1_000, "policy_sets": 100},
        "budget": {
            "pan": {"requests_per_hour": 3_000},
            # Sized for full per-endpoint coverage, not a sample: session detail
            # is one fetch per MAC cached for the session's life, so the steady
            # cost is churn. At 60k sessions, 1% turnover per five-minute cycle
            # is 600 fetches a cycle -- 7,200 an hour, plus the ActiveList reads.
            # The ceiling keeps the same headroom over that as it always has.
            "mnt": {"requests_per_hour": 12_000},
            "pxgrid": {"requests_per_hour": 200},
            # Full v2 workflow parity includes accounting, diagnostic, posture,
            # profiling, and correlated failure scans. Their declared steady
            # load is ~2.64%, so 3% preserves the operational cadences with
            # explicit headroom instead of lowering a cost estimate to fit.
            "oracle": {"duty_cycle_percent": 3.0},
        },
    },
})


class ConfigError(ValueError):
    """The configuration is missing, malformed, or unsafe."""


def parse_duration(value, *, key):
    """Accept '30s', '5m', '6h', '1d', or a bare integer count of seconds."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ConfigError(f"{key} must be a duration like '5m', got {value!r}")
    if isinstance(value, int):
        seconds = value
    else:
        match = _DURATION.match(value)
        if not match:
            raise ConfigError(
                f"{key} must be a duration like '30s', '5m', '6h', got {value!r}")
        seconds = int(match.group(1)) * _DURATION_UNITS[match.group(2)]
    if seconds < 1:
        raise ConfigError(f"{key} must be at least one second")
    return seconds


def format_duration(seconds):
    """Render seconds back into the shortest exact unit, for plan output."""
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


@dataclass(frozen=True)
class TargetConfig:
    """How to reach one ISE persona. Absent host means 'not available'."""

    name: str
    host: str = ""
    user: str = ""
    password: str = ""
    port: int = 0
    ers_port: int = 9060
    service: str = ""
    node_name: str = ""
    client_cert: str = ""
    client_key: str = ""
    ca_bundle: str = ""
    verify_tls: bool = True
    request_timeout: int = 30

    @property
    def configured(self):
        """True when this target has everything needed to attempt a connection."""
        if not self.host:
            return False
        if self.name == "pan":
            return bool(self.user and self.password)
        if self.name == "mnt":
            return bool(self.user and self.password)  # mirrors the PAN account
        if self.name == "oracle":
            return bool(self.user and self.password and self.service)
        if self.name == "pxgrid":
            return bool(self.node_name
                        and (self.password or (self.client_cert and self.client_key)))
        return False

    def unconfigured_reason(self):
        """One short phrase naming what is missing, for the plan output."""
        if self.configured:
            return ""
        if not self.host:
            return f"targets.{self.name}.host is not set"
        missing = {
            "pan": "targets.pan.user and ISE_PASS",
            "mnt": "targets.pan.user and ISE_PASS",
            "oracle": "targets.oracle.user/service and ISE_DATACONNECT_PASSWORD",
            "pxgrid": "targets.pxgrid.node_name and a password or client certificate",
        }.get(self.name, "required credentials")
        return f"{missing} are not set"


@dataclass(frozen=True)
class TargetBudget:
    """What one target may be spent per hour. Zero means no declared ceiling.

    ``warmup_requests_per_hour`` is the one place where "what this costs once
    warm" and "what it may cost while filling" are allowed to differ. A cold
    start is a genuinely different decision from a steady state -- 20,000 MnT
    requests to reach full session coverage is either two hours of burst or
    twelve hours of patience -- and the exporter should make an operator say
    which, rather than picking one silently.
    """

    target: str
    requests_per_hour: float = 0.0
    duty_cycle_percent: float = 0.0
    warmup_requests_per_hour: float = 0.0

    @property
    def declared(self):
        return bool(self.requests_per_hour or self.duty_cycle_percent)

    @property
    def warmup_multiple(self):
        """How many times the steady ceiling the declared burst allows."""
        if not (self.warmup_requests_per_hour and self.requests_per_hour):
            return 0.0
        return self.warmup_requests_per_hour / self.requests_per_hour


@dataclass(frozen=True)
class DatasetConfig:
    """Operator intent for one dataset: three keys plus its declared options."""

    name: str
    enabled: bool = True
    providers: tuple = ()   # empty means the dataset's own declared order
    interval: int = 0       # zero means the dataset's default cadence
    # Every option the dataset declares, defaults filled in, so a collector
    # never has to ask whether a bound was configured -- only what it is.
    options: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))
    # The subset the operator set by hand, for `plan` and for the warnings.
    declared_options: tuple = ()

    def option(self, name):
        try:
            return self.options[name]
        except KeyError:
            raise KeyError(
                f"dataset {self.name!r} has no option {name!r}; declare it in "
                "its Dataset(options=...)") from None


@dataclass(frozen=True)
class ExporterConfig:
    port: int = 9618
    api_host: str = "127.0.0.1"
    api_port: int = 9619
    state_db: str = "/var/lib/ise-exporter3/state.sqlite3"
    log_level: str = "INFO"
    scrape_interval: int = 60
    # A plan that exceeds its declared budget is a configuration error, not a
    # surprise discovered in production. Operators who want to run over budget
    # deliberately can turn this into a warning.
    enforce_budget: bool = True


@dataclass(frozen=True)
class Config:
    path: str
    profile: str
    scale: Scale
    exporter: ExporterConfig
    targets: MappingProxyType
    budget: MappingProxyType
    limits: object                   # limits.Limits, derived from scale
    datasets: MappingProxyType
    warnings: tuple = field(default=())

    def target(self, name):
        return self.targets[name]

    def dataset(self, name):
        """Operator intent for one dataset, with its option defaults filled in.

        A dataset absent from the file is configured -- by its own declared
        defaults -- rather than unconfigured, so no collector has to carry a
        second copy of a bound in case nobody wrote one down.
        """
        existing = self.datasets.get(name)
        if existing is not None:
            return existing
        from . import datasets as registry

        try:
            declared = registry.get(name)
        except KeyError:
            return DatasetConfig(name=name)
        return DatasetConfig(
            name=name, options=MappingProxyType(declared.option_defaults()))

    def budget_for(self, target):
        return self.budget[target]

    def with_scale(self, scale):
        """Return a preview config with costs and derived limits at ``scale``.

        Explicit limit overrides remain explicit. Dynamic warnings are
        recomputed so a live-scale preview cannot retain advice written for the
        old declared scale.
        """
        if not isinstance(scale, Scale):
            raise ConfigError("live scale must be a Scale")

        old_dynamic = list(limits_module.dangerous(self.limits, self.scale))
        _warn_dangerous_options(self.datasets, self.scale, old_dynamic)
        stable = [warning for warning in self.warnings if warning not in old_dynamic]

        overrides = {
            name: getattr(self.limits, name)
            for name in self.limits.overridden
        }
        dynamic = []
        try:
            resolved_limits = limits_module.derive(scale, overrides, dynamic)
        except limits_module.LimitsError as error:
            raise ConfigError(str(error)) from error
        _warn_dangerous_options(self.datasets, scale, dynamic)
        return replace(
            self,
            scale=scale,
            limits=resolved_limits,
            warnings=tuple(stable + dynamic),
        )

    @classmethod
    def load(cls, path=None, *, environ=None):
        environ = os.environ if environ is None else environ
        configured = path or environ.get("ISE_EXPORTER3_CONFIG")
        config_path = Path(configured) if configured else Path(DEFAULT_CONFIG_FILE)
        if not config_path.exists():
            raise ConfigError(f"configuration file does not exist: {config_path}")
        try:
            with config_path.open("rb") as stream:
                document = tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise ConfigError(f"cannot load {config_path}: {error}") from error
        return cls.from_document(document, path=str(config_path), environ=environ)

    @classmethod
    def from_document(cls, document, *, path="", environ=None):
        environ = os.environ if environ is None else environ
        _reject_unknown(document, _TOP_LEVEL, "top-level")
        warnings = []

        profile_name = document.get("profile", DEFAULT_PROFILE)
        if profile_name not in PROFILES:
            raise ConfigError(
                f"profile must be one of {', '.join(sorted(PROFILES))}, "
                f"got {profile_name!r}")
        preset = PROFILES[profile_name]

        scale = _build_scale(document.get("scale", {}), preset["scale"])
        exporter = _build_exporter(document.get("exporter", {}))
        targets = _build_targets(document.get("targets", {}), environ)
        budget = _build_budget(document.get("budget", {}), preset["budget"], warnings)
        limits = _build_limits(document.get("limits", {}), scale, warnings)
        datasets = _build_datasets(document.get("datasets", {}), scale, warnings)

        return cls(
            path=path,
            profile=profile_name,
            scale=scale,
            exporter=exporter,
            targets=MappingProxyType(targets),
            budget=MappingProxyType(budget),
            limits=limits,
            datasets=MappingProxyType(datasets),
            warnings=tuple(warnings),
        )


def _reject_unknown(document, allowed, where):
    unknown = sorted(set(document) - set(allowed))
    if unknown:
        suffix = "s" if len(unknown) != 1 else ""
        raise ConfigError(
            f"unknown {where} key{suffix}: {', '.join(unknown)}; "
            f"known keys: {', '.join(sorted(allowed))}")


def _typed(value, expected, key):
    if expected is bool:
        valid = isinstance(value, bool)
    elif expected is int:
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif expected is float:
        valid = isinstance(value, (int, float)) and not isinstance(value, bool)
    else:
        valid = isinstance(value, expected)
    if not valid:
        raise ConfigError(
            f"{key} must be {expected.__name__}, got {type(value).__name__}")
    return float(value) if expected is float else value


def _build_scale(document, preset):
    _reject_unknown(document, SCALE_DIMENSIONS, "scale")
    values = dict(preset)
    for dimension in SCALE_DIMENSIONS:
        if dimension in document:
            value = _typed(document[dimension], int, f"scale.{dimension}")
            if value < 1:
                raise ConfigError(f"scale.{dimension} must be at least 1")
            values[dimension] = value
    return Scale(**values)


def _build_exporter(document):
    _reject_unknown(document, _EXPORTER_FIELDS, "exporter")
    defaults = ExporterConfig()
    values = {}
    for name in ("port", "api_port"):
        if name in document:
            value = _typed(document[name], int, f"exporter.{name}")
            if not 1 <= value <= 65535:
                raise ConfigError(f"exporter.{name} must be between 1 and 65535")
            values[name] = value
    for name in ("api_host", "state_db"):
        if name in document:
            value = _typed(document[name], str, f"exporter.{name}").strip()
            if not value:
                raise ConfigError(f"exporter.{name} cannot be empty")
            values[name] = value
    if "log_level" in document:
        level = _typed(document["log_level"], str, "exporter.log_level").upper()
        if level == "WARN":
            level = "WARNING"
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigError(f"exporter.log_level is unsupported: {level!r}")
        values["log_level"] = level
    if "scrape_interval" in document:
        values["scrape_interval"] = parse_duration(
            document["scrape_interval"], key="exporter.scrape_interval")
    if "enforce_budget" in document:
        values["enforce_budget"] = _typed(
            document["enforce_budget"], bool, "exporter.enforce_budget")
    return ExporterConfig(**{**defaults.__dict__, **values})


def _build_targets(document, environ):
    _reject_unknown(document, TARGETS, "targets")
    targets = {}
    for name in TARGETS:
        section = document.get(name, {})
        if not isinstance(section, dict):
            raise ConfigError(f"targets.{name} must be a table")
        _reject_unknown(section, _TARGET_FIELDS[name], f"targets.{name}")
        values = {"name": name}
        for key, value in section.items():
            expected = bool if key == "verify_tls" else (
                int if key in ("port", "ers_port", "request_timeout") else str)
            values[key] = _typed(value, expected, f"targets.{name}.{key}")
        secret_env = SECRET_ENV.get(name)
        if secret_env and environ.get(secret_env):
            values["password"] = environ[secret_env]
        targets[name] = TargetConfig(**values)

    # MnT authenticates with the PAN account, so mirror it rather than making an
    # operator configure the same credential twice.
    pan, mnt = targets["pan"], targets["mnt"]
    targets["mnt"] = TargetConfig(
        **{**mnt.__dict__, "user": pan.user, "password": pan.password})
    return targets


def _build_budget(document, preset, warnings):
    _reject_unknown(document, TARGETS, "budget")
    budget = {}
    for name in TARGETS:
        section = document.get(name, {})
        if not isinstance(section, dict):
            raise ConfigError(f"budget.{name} must be a table")
        _reject_unknown(section, _BUDGET_FIELDS, f"budget.{name}")
        values = dict(preset.get(name, {}))
        for key, value in section.items():
            values[key] = _typed(value, float, f"budget.{name}.{key}")
            if values[key] < 0:
                raise ConfigError(f"budget.{name}.{key} cannot be negative")
        if name != "oracle" and values.get("duty_cycle_percent"):
            raise ConfigError(
                f"budget.{name}.duty_cycle_percent does not apply; only the "
                "oracle target has a duty cycle")
        if name == "oracle" and values.get("warmup_requests_per_hour"):
            raise ConfigError(
                "budget.oracle.warmup_requests_per_hour does not apply; the "
                "oracle target is budgeted as a duty cycle, and no Data Connect "
                "provider caches per-entity detail")
        entry = TargetBudget(target=name, **values)
        if not entry.declared:
            warnings.append(
                f"budget.{name} declares no ceiling; load on this target is unbounded")
        if entry.warmup_requests_per_hour and not entry.requests_per_hour:
            raise ConfigError(
                f"budget.{name}.warmup_requests_per_hour is declared without a "
                f"budget.{name}.requests_per_hour to burst above")
        if 0 < entry.warmup_requests_per_hour < entry.requests_per_hour:
            raise ConfigError(
                f"budget.{name}.warmup_requests_per_hour "
                f"({entry.warmup_requests_per_hour:,.0f}) is below the steady "
                f"budget.{name}.requests_per_hour ({entry.requests_per_hour:,.0f}), "
                "so it would slow a cold start rather than shorten it")
        if entry.warmup_multiple > 1:
            # Deliberate, declared, and worth restating at start: this is the
            # one setting that lets the exporter knowingly exceed its own
            # steady ceiling, and the appliance is what absorbs it.
            warnings.append(
                f"budget.{name}.warmup_requests_per_hour allows "
                f"{entry.warmup_requests_per_hour:,.0f} requests/hour while a "
                f"cache is still filling -- {entry.warmup_multiple:.1f}x the "
                f"declared steady ceiling of {entry.requests_per_hour:,.0f}. "
                "`plan` prints how long that burst is expected to last")
        budget[name] = entry
    return budget


def _build_limits(document, scale, warnings):
    """Resolve the row, byte and series ceilings against the declared scale.

    A contradiction here is refused rather than warned about: the ceilings that
    disagreed before this section existed did not produce a degraded dataset,
    they produced a dataset that failed every collection after paying for the
    scan. That is a configuration error and it should look like one.
    """
    if not isinstance(document, dict):
        raise ConfigError("limits must be a table")
    try:
        return limits_module.derive(scale, document, warnings)
    except limits_module.LimitsError as error:
        raise ConfigError(str(error)) from error


def _build_dataset_options(dataset, document):
    """Validate one dataset's declared options and fill in its defaults."""
    if not isinstance(document, dict):
        raise ConfigError(f"datasets.{dataset.name}.options must be a table")
    _reject_unknown(document, set(dataset.option_names),
                    f"datasets.{dataset.name}.options")
    values = dataset.option_defaults()
    for name, value in document.items():
        option = dataset.option(name)
        key = f"datasets.{dataset.name}.options.{name}"
        value = _typed(value, option.kind, key)
        if option.choices and value not in option.choices:
            raise ConfigError(
                f"{key} must be one of "
                f"{', '.join(repr(choice) for choice in option.choices)}, "
                f"got {value!r}")
        if option.minimum is not None and value < option.minimum:
            raise ConfigError(f"{key} must be at least {option.minimum}")
        if option.maximum is not None and value > option.maximum:
            raise ConfigError(f"{key} must be at most {option.maximum}")
        values[name] = value
    return MappingProxyType(values), tuple(sorted(document))


def _warn_dangerous_options(configured, scale, warnings):
    """Say out loud what each enabled dataset's bounds will actually do.

    Judged on the **effective** value rather than on whether an operator typed
    it, because the warning describes what the exporter is about to publish, not
    who chose it. A disabled dataset publishes nothing, so it says nothing.
    """
    from . import datasets as registry

    for dataset in registry.all_datasets():
        settings = configured.get(dataset.name)
        if settings is not None and not settings.enabled:
            continue
        values = (settings.options if settings is not None
                  else dataset.option_defaults())
        for option in dataset.options:
            if option.danger is None:
                continue
            message = option.danger(values[option.name], scale)
            if message:
                warnings.append(message)


def _build_datasets(document, scale, warnings):
    from . import datasets as registry

    known = set(registry.names())
    _reject_unknown(document, known, "datasets")
    configured = {}
    for name, section in document.items():
        if not isinstance(section, dict):
            raise ConfigError(f"datasets.{name} must be a table")
        _reject_unknown(section, _DATASET_FIELDS, f"datasets.{name}")
        dataset = registry.get(name)
        values = {"name": name}
        values["options"], values["declared_options"] = _build_dataset_options(
            dataset, section.get("options", {}))
        if "enabled" in section:
            values["enabled"] = _typed(
                section["enabled"], bool, f"datasets.{name}.enabled")
        if "interval" in section:
            values["interval"] = parse_duration(
                section["interval"], key=f"datasets.{name}.interval")
        if "providers" in section:
            providers = section["providers"]
            if not isinstance(providers, list) or not providers:
                raise ConfigError(
                    f"datasets.{name}.providers must be a non-empty list")
            declared = dataset.provider_names
            for provider in providers:
                if provider not in declared:
                    raise ConfigError(
                        f"datasets.{name}.providers has no provider {provider!r}; "
                        f"choose from {', '.join(declared)}")
            if len(set(providers)) != len(providers):
                raise ConfigError(
                    f"datasets.{name}.providers lists a provider twice")
            values["providers"] = tuple(providers)
        configured[name] = DatasetConfig(**values)
    _warn_dangerous_options(configured, scale, warnings)
    return configured
