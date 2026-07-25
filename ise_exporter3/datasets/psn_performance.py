"""PSN request volume, load, latency, TPS, and node resource utilisation.

Data Connect only. pxGrid's system-health service reports node health but not the
RADIUS throughput and MnT log-volume rollups these panels are built on.

Four short statements in one atomic batch under a single duty-cycle lease. That
batching is the point: charging each statement its own adaptive cooldown would
put hours between the parts of one dashboard update at a low duty cycle, while
charging the batch once on combined Oracle time keeps the long-run duty cycle
exactly as declared.

Columns are selected against the discovered schema rather than assumed, so an
absent optional column degrades one dimension instead of failing the dataset.
"""
from prometheus_client import Gauge

from .. import reporting
from ..labels import label
from ..model import Cost, Dataset, Provider
from ..parsing import finite


radius_requests_per_hour = Gauge(
    "ise3_psn_radius_requests_per_hour", "RADIUS requests per hour",
    ["provider", "node"])
mnt_logs_per_hour = Gauge(
    "ise3_psn_mnt_logs_per_hour", "Records logged to MnT per hour",
    ["provider", "node"])
noise_per_hour = Gauge(
    "ise3_psn_noise_per_hour", "Noise events per hour", ["provider", "node"])
suppression_per_hour = Gauge(
    "ise3_psn_suppression_per_hour", "Suppressed events per hour",
    ["provider", "node"])
load_percent = Gauge(
    "ise3_psn_load_percent", "Average PSN load", ["provider", "node"])
average_latency_seconds = Gauge(
    "ise3_psn_average_latency_seconds", "Average latency per request",
    ["provider", "node"])
average_tps = Gauge(
    "ise3_psn_average_tps", "Average transactions per second", ["provider", "node"])
cpu_percent = Gauge(
    "ise3_node_cpu_utilization_percent", "Node CPU utilisation", ["provider", "node"])
memory_percent = Gauge(
    "ise3_node_memory_utilization_percent", "Node memory utilisation",
    ["provider", "node"])
disk_percent = Gauge(
    "ise3_node_disk_utilization_percent", "Node filesystem utilisation",
    ["provider", "node", "filesystem"])

_METRICS = (
    radius_requests_per_hour, mnt_logs_per_hour, noise_per_hour,
    suppression_per_hour, load_percent, average_latency_seconds, average_tps,
    cpu_percent, memory_percent, disk_percent,
)

_KPI_COLUMNS = (
    "radius_requests_hr", "logged_to_mnt_hr", "noise_hr", "suppression_hr",
    "avg_load", "avg_latency_per_req", "avg_tps",
)
_SYSTEM_COLUMNS = (
    "cpu_utilization", "memory_utilization", "diskspace_root", "diskspace_boot",
    "diskspace_opt", "diskspace_tmp",
)
_DISK_COLUMNS = {
    "diskspace_root": "root", "diskspace_boot": "boot",
    "diskspace_opt": "opt", "diskspace_tmp": "tmp",
}

_KPI_GAUGES = {
    "radius_requests_hr": radius_requests_per_hour,
    "logged_to_mnt_hr": mnt_logs_per_hour,
    "noise_hr": noise_per_hour,
    "suppression_hr": suppression_per_hour,
    "avg_load": load_percent,
    "avg_latency_per_req": average_latency_seconds,
    "avg_tps": average_tps,
}


def _available(schema, view, required, optional):
    """Take the required columns plus whichever optional ones this ISE has."""
    columns = None if schema is None else schema.get(view.upper())
    return tuple(required) + tuple(
        column for column in optional
        if columns is None or column.upper() in columns)


def _latest_per_node(view, time_column, columns, hours):
    """Newest row per node inside a bounded window.

    The window bound comes first so Oracle can use the timestamp index rather
    than ranking the whole view; the exporter never scans unbounded history.
    """
    selected = ", ".join(columns)
    return f"""
        SELECT {selected} FROM (
            SELECT {selected}, ROW_NUMBER() OVER (
                PARTITION BY ise_node ORDER BY {time_column} DESC) AS row_num
            FROM {view}
            WHERE {time_column} >= CAST(
                SYSTIMESTAMP - NUMTODSINTERVAL({hours}, 'HOUR') AS TIMESTAMP)
        ) WHERE row_num = 1
    """


def statements(limits, schema=None, hours=0):
    # The same window ceiling every reporting statement scans under, rather than
    # a second copy of the number here.
    hours = reporting.window_hours(
        (hours or limits.window_hours) * 3600, limits)
    return {
        "kpi": _latest_per_node(
            "key_performance_metrics", "logged_time",
            _available(schema, "KEY_PERFORMANCE_METRICS", ("ise_node",), _KPI_COLUMNS),
            hours),
        "system": _latest_per_node(
            "system_summary", "timestamp",
            _available(schema, "SYSTEM_SUMMARY", ("ise_node",), _SYSTEM_COLUMNS),
            hours),
    }


def fetch(ctx):
    schema = getattr(ctx.transport, "schema", None)
    results = ctx.transport.query_many(statements(ctx.limits, schema))

    for row in results.get("kpi", []):
        node = label(row.get("ise_node"))
        for column, gauge in _KPI_GAUGES.items():
            if column in row and row[column] is not None:
                ctx.set(gauge, finite(row[column]), node=node)

    for row in results.get("system", []):
        node = label(row.get("ise_node"))
        if row.get("cpu_utilization") is not None:
            ctx.set(cpu_percent, finite(row["cpu_utilization"]), node=node)
        if row.get("memory_utilization") is not None:
            ctx.set(memory_percent, finite(row["memory_utilization"]), node=node)
        for column, filesystem in _DISK_COLUMNS.items():
            if row.get(column) is not None:
                ctx.set(disk_percent, finite(row[column]),
                        node=node, filesystem=filesystem)


DATASET = Dataset(
    name="psn_performance",
    description="PSN request volume, load, latency, TPS, node resources",
    default_interval=300,
    metrics=_METRICS,
    providers=(
        Provider(
            name="dataconnect",
            # Two short statements in one batch under a single lease.
            cost=Cost(target="oracle", db_seconds=3.0),
            supplies=frozenset({"psn", "tps", "latency", "load", "resources"}),
            requires=("view:KEY_PERFORMANCE_METRICS", "view:SYSTEM_SUMMARY"),
            fetch=fetch,
        ),
    ),
)
