"""Dataset registry.

Every dataset lives in exactly one module in this package and exports exactly one
``DATASET``. Adding a dataset is adding a file and one line here -- there is no
priority table, no persisted-metrics map, and no scheduler call list to keep in
sync, which is what made adding a dataset a six-to-eight-file change in v2.
"""
from ..model import Dataset
from . import (
    active_sessions,
    backup,
    certificates,
    deployment,
    endpoint_attributes,
    endpoint_inventory,
    licensing,
    nad_health,
    network_devices,
    patches,
    posture_current,
    posture_history,
    psn_performance,
    radius_errors,
    radius_reporting,
    session_authorization,
    source_freshness,
    tacacs_activity,
    tacacs_config,
)

_MODULES = (
    deployment,
    network_devices,
    certificates,
    licensing,
    backup,
    patches,
    tacacs_config,
    active_sessions,
    session_authorization,
    posture_current,
    endpoint_inventory,
    endpoint_attributes,
    psn_performance,
    radius_reporting,
    radius_errors,
    posture_history,
    nad_health,
    tacacs_activity,
    source_freshness,
)


def _build_registry():
    registry = {}
    for module in _MODULES:
        dataset = getattr(module, "DATASET", None)
        if not isinstance(dataset, Dataset):
            raise TypeError(f"{module.__name__} does not export a DATASET")
        if dataset.name in registry:
            raise ValueError(f"duplicate dataset name {dataset.name!r}")
        registry[dataset.name] = dataset
    return registry


REGISTRY = _build_registry()


def all_datasets():
    """Return every registered dataset in declaration order."""
    return tuple(REGISTRY.values())


def get(name):
    """Return one dataset by name."""
    try:
        return REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"unknown dataset {name!r}; known datasets: "
            f"{', '.join(sorted(REGISTRY))}") from None


def names():
    return tuple(REGISTRY)
