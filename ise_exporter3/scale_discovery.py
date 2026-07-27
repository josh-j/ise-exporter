"""Bounded live discovery of the five fleet dimensions used by the plan."""
from __future__ import annotations

from dataclasses import dataclass

from .model import SCALE_DESCRIPTIONS, SCALE_DIMENSIONS, Scale
from .transports import TransportError, build_transports, close_transports


SOURCES = {
    "nads": "PAN ERS /config/networkdevice total",
    "endpoints": "PAN ERS /config/endpoint total",
    "sessions": "MnT /Session/ActiveCount",
    "accounts": "PAN ERS /config/internaluser total",
    "policy_sets": "PAN OpenAPI Device Admin policy sets",
}

MAX_LIVE_SCALE_COUNT = 10_000_000


@dataclass(frozen=True)
class ScaleObservation:
    dimension: str
    declared: int
    observed: int | None
    source: str
    description: str
    error: str = ""

    @property
    def available(self):
        return self.observed is not None

    @property
    def used(self):
        # The planning model has a one-entity safety floor. A genuinely empty
        # feature therefore costs its fixed work plus the smallest scale unit.
        return max(1, self.observed) if self.available else self.declared

    def to_dict(self):
        return {
            "dimension": self.dimension,
            "declared": self.declared,
            "observed": self.observed,
            "used": self.used,
            "source": self.source,
            "description": self.description,
            "available": self.available,
            "error": self.error,
        }


@dataclass(frozen=True)
class ScaleDiscovery:
    observations: tuple

    @property
    def complete(self):
        return all(item.available for item in self.observations)

    @property
    def scale(self):
        values = {item.dimension: item.used for item in self.observations}
        return Scale(**values)

    def to_dict(self):
        return {
            "complete": self.complete,
            "effective_scale": {
                dimension: getattr(self.scale, dimension)
                for dimension in SCALE_DIMENSIONS
            },
            "observations": [item.to_dict() for item in self.observations],
        }


def _count_active_sessions(transport):
    response = transport.get_mnt_xml(
        "/Session/ActiveCount", api="scale_active_sessions")
    records = response.get("sessions") if isinstance(response, dict) else None
    record = records[0] if isinstance(records, list) and records else {}
    for key in ("count", "active_count", "activeCount"):
        if key not in record:
            continue
        try:
            count = int(record[key])
        except (TypeError, ValueError):
            break
        if count >= 0:
            return count
    raise TransportError(
        "invalid_response", "MnT ActiveCount returned no usable session count")


def _count_policy_sets(transport):
    rows = transport.get_openapi(
        "/policy/device-admin/policy-set", api="scale_policy_sets")
    if not isinstance(rows, list):
        raise TransportError(
            "invalid_response", "PAN returned no Device Admin policy-set list")
    return len(rows)


def _error_text(error):
    if isinstance(error, TransportError):
        return f"{error.reason}: {error.detail}"
    return f"unexpected_error: {error}"


def discover_scale(config):
    """Perform five bounded reads and retain a result for every dimension."""
    transports = build_transports(config, kinds={"pan", "mnt"})
    pan = transports.get("pan")
    mnt = transports.get("mnt")
    declared = config.scale
    results = {}

    def observe(name, transport, callback, unavailable):
        if transport is None:
            results[name] = (None, unavailable)
            return "not_configured"
        try:
            value = callback(transport)
            if (
                not isinstance(value, int)
                or not 0 <= value <= MAX_LIVE_SCALE_COUNT
            ):
                raise TransportError(
                    "invalid_response",
                    f"{name} count was not between 0 and "
                    f"{MAX_LIVE_SCALE_COUNT:,}")
            results[name] = (value, "")
            return ""
        except Exception as error:  # each count remains independently useful
            results[name] = (None, _error_text(error))
            return error.reason if isinstance(error, TransportError) else "unexpected_error"

    try:
        pan_reason = config.targets["pan"].unconfigured_reason()
        pan_checks = (
            (
                "nads",
                lambda client: client.get_ers_total(
                    "/config/networkdevice", api="scale_nads"),
            ),
            (
                "endpoints",
                lambda client: client.get_ers_total(
                    "/config/endpoint", api="scale_endpoints"),
            ),
            (
                "accounts",
                lambda client: client.get_ers_total(
                    "/config/internaluser", api="scale_accounts"),
            ),
            ("policy_sets", _count_policy_sets),
        )
        terminal_pan_failures = {
            "authentication_backoff",
            "authentication_failed",
            "connection_failed",
            "state_unavailable",
            "tls_failed",
        }
        pan_failure = ""
        pan_terminal_error = ""
        # One bad password must remain one bad attempt. Four independent PAN
        # counts must not walk the shared account to its lockout threshold.
        for name, callback in pan_checks:
            if pan_terminal_error:
                results[name] = (None, pan_terminal_error)
                continue
            pan_failure = observe(name, pan, callback, pan_reason)
            if pan_failure in terminal_pan_failures:
                pan_terminal_error = results[name][1]

        mnt_reason = (
            "targets.mnt.host is not set" if not config.targets["mnt"].host
            else "targets.pan.user and ISE_PASS are not set")
        # MnT's TargetConfig reuses PAN credentials, so make that dependency
        # explicit instead of sending an empty password and calling it an auth
        # failure.
        if pan_failure in {
            "authentication_backoff",
            "authentication_failed",
            "state_unavailable",
        }:
            mnt_client = None
            mnt_reason = (
                "skipped after the PAN credential check failed: "
                f"{pan_terminal_error}")
        else:
            mnt_client = mnt if config.targets["pan"].configured else None
        observe("sessions", mnt_client, _count_active_sessions, mnt_reason)
    finally:
        close_transports(transports)

    return ScaleDiscovery(tuple(
        ScaleObservation(
            dimension=name,
            declared=getattr(declared, name),
            observed=results[name][0],
            source=SOURCES[name],
            description=SCALE_DESCRIPTIONS[name],
            error=results[name][1],
        )
        for name in SCALE_DIMENSIONS
    ))


def render_scale_discovery(discovery):
    """Render observed versus declared inputs before the effective plan."""
    headers = ("COUNT", "DECLARED", "OBSERVED", "USED", "SOURCE")
    rows = []
    for item in discovery.observations:
        observed = f"{item.observed:,}" if item.available else "unavailable"
        rows.append((
            item.dimension,
            f"{item.declared:,}",
            observed,
            f"{item.used:,}",
            item.source,
        ))
    columns = list(zip(*([headers] + rows)))
    widths = [max(len(str(cell)) for cell in column) for column in columns]
    lines = ["Live scale discovery (the TOML is not changed):"]
    lines.append("  ".join(str(value).ljust(width)
                           for value, width in zip(headers, widths)).rstrip())
    lines.append("  ".join("-" * width for width in widths))
    for row in rows:
        lines.append("  ".join(str(value).ljust(width)
                               for value, width in zip(row, widths)).rstrip())
    failures = [item for item in discovery.observations if not item.available]
    if failures:
        lines += [
            "",
            "Unavailable observations used their declared TOML value:",
        ]
        for item in failures:
            lines.append(f"  {item.dimension}: {item.error}")
    zeroes = [item for item in discovery.observations
              if item.available and item.observed == 0]
    if zeroes:
        lines += [
            "",
            "Observed zero uses the planning model's conservative minimum of 1:",
            "  " + ", ".join(item.dimension for item in zeroes),
        ]
    return "\n".join(lines)
