"""Installed version and patch inventory.

OpenAPI only. This is also the dataset that proves the appliance still matches
the supported contract, so it fails closed on an unexpected version or patch
level rather than exporting a number for a release nobody has tested.
"""
from prometheus_client import Gauge

from ..compatibility import SUPPORTED_ISE_VERSION, SUPPORTED_PATCH_LEVEL
from ..model import Cost, Dataset, Provider


patch_level = Gauge(
    "ise3_patch_level", "Highest installed patch number", ["provider"])
patch_installed = Gauge(
    "ise3_patch_installed", "Patch is installed", ["provider", "patch_number"])
version_supported = Gauge(
    "ise3_version_supported",
    "Connected release matches the exporter's supported contract",
    ["provider", "version"])

_METRICS = (patch_level, patch_installed, version_supported)


def fetch(ctx):
    payload = ctx.transport.get_openapi("/patch", api="pan_patches")
    if not isinstance(payload, dict):
        ctx.fail("invalid_response", "patch inventory was not an object")

    version = str(payload.get("iseVersion") or "").strip()
    if version != SUPPORTED_ISE_VERSION:
        ctx.fail("invalid_response",
                 f"ISE reported version {version!r}; this exporter supports "
                 f"{SUPPORTED_ISE_VERSION}")

    versions = payload.get("patchVersion") or []
    if not isinstance(versions, list):
        ctx.fail("invalid_response", "patchVersion was not a list")

    installed = set()
    for patch in versions:
        if not isinstance(patch, dict):
            ctx.fail("invalid_response", "patchVersion contained a non-object")
        try:
            number = int(patch.get("patchNumber"))
        except (TypeError, ValueError):
            ctx.fail("invalid_response", "patchVersion contained an invalid number")
        if not 0 < number <= 999 or number in installed:
            ctx.fail("invalid_response", "patchVersion contained an invalid number")
        installed.add(number)

    highest = max(installed, default=0)
    if highest != SUPPORTED_PATCH_LEVEL:
        ctx.fail("invalid_response",
                 f"ISE reported patch level {highest}; this exporter supports "
                 f"Patch {SUPPORTED_PATCH_LEVEL}")

    ctx.set(patch_level, highest)
    ctx.set(version_supported, 1, version=version)
    for number in sorted(installed):
        ctx.set(patch_installed, 1, patch_number=str(number))


DATASET = Dataset(
    name="patches",
    description="Installed ISE version and patch inventory",
    default_interval=21600,
    metrics=_METRICS,
    providers=(
        Provider(
            name="openapi",
            cost=Cost(target="pan", requests=1),
            supplies=frozenset({"version", "patch"}),
            fetch=fetch,
        ),
    ),
)
