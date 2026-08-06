"""Framework metrics: dataset health, provider selection, and load.

Only metrics the framework itself owns live here. Data metrics belong to the
dataset module that produces them, listed in its ``Dataset.metrics`` -- that is
what keeps dataset facts in one file instead of scattered across a registry.

The load families are the honesty mechanism. Planned load comes from declared
costs; measured load is counted by the transports as requests actually leave.
Drift between the two means a cost declaration is wrong, and it is the one defect
this design cannot catch by construction.
"""
from prometheus_client import Counter, Gauge, Histogram

from . import detail_cache


# --- identity ---
exporter_build_info = Gauge(
    "ise3_exporter_build_info", "Exporter version and compatibility target",
    ["version", "target_ise_release"])
dataconnect_schema_view_available = Gauge(
    "ise3_dataconnect_schema_view_available",
    "Expected Data Connect reporting view is visible to the configured account",
    ["view"])
dataconnect_schema_column_available = Gauge(
    "ise3_dataconnect_schema_column_available",
    "Expected Data Connect column is visible; requirement distinguishes core "
    "from optional dashboard capability",
    ["view", "column", "requirement"])

# --- dataset health ---
dataset_enabled = Gauge(
    "ise3_dataset_enabled", "Dataset is enabled in configuration", ["dataset"])
dataset_interval_seconds = Gauge(
    "ise3_dataset_interval_seconds", "Configured collection cadence", ["dataset"])
dataset_up = Gauge(
    "ise3_dataset_up", "Latest collection attempt succeeded",
    ["dataset", "provider"])
dataset_fresh = Gauge(
    "ise3_dataset_fresh", "Latest successful snapshot is within two cadences",
    ["dataset", "provider"])
dataset_last_success_timestamp = Gauge(
    "ise3_dataset_last_success_timestamp", "Unix time of the last success",
    ["dataset", "provider"])
dataset_last_attempt_timestamp = Gauge(
    "ise3_dataset_last_attempt_timestamp", "Unix time of the last attempt",
    ["dataset", "provider"])
dataset_next_run_timestamp = Gauge(
    "ise3_dataset_next_run_timestamp", "Unix time this dataset is next due",
    ["dataset", "provider"])
dataset_collection_duration_seconds = Gauge(
    "ise3_dataset_collection_duration_seconds", "Duration of the last collection",
    ["dataset", "provider"])
dataset_consecutive_failures = Gauge(
    "ise3_dataset_consecutive_failures", "Consecutive failed attempts", ["dataset"])
dataset_failures_total = Counter(
    "ise3_dataset_failures_total", "Failed collection attempts by bounded reason",
    ["dataset", "provider", "reason"])
dataset_last_failure_info = Gauge(
    "ise3_dataset_last_failure_info", "Bounded reason for the latest failure",
    ["dataset", "provider", "reason"])
dataset_last_failure_detail_info = Gauge(
    "ise3_dataset_last_failure_detail_info",
    "One bounded operator explanation for the latest failure",
    ["dataset", "provider", "reason", "detail"])

# --- provider selection ---
# Sources differ in meaning, so which one is live is data, not just health. A
# dashboard reading a multi-source dataset must gate on provider_active.
dataset_provider_active = Gauge(
    "ise3_dataset_provider_active", "Provider currently supplying this dataset",
    ["dataset", "provider"])
dataset_provider_available = Gauge(
    "ise3_dataset_provider_available", "Provider is configured and usable",
    ["dataset", "provider"])
dataset_provider_degraded = Gauge(
    "ise3_dataset_provider_degraded",
    "Active provider is not the operator's first choice", ["dataset"])
dataset_provider_reason_info = Gauge(
    "ise3_dataset_provider_reason_info",
    "Why the active provider was chosen over an earlier preference",
    ["dataset", "provider", "reason"])

# --- declared and measured load ---
load_planned_requests_per_hour = Gauge(
    "ise3_load_planned_requests_per_hour",
    "Requests per hour this plan intends to send a target", ["target"])
load_planned_db_seconds_per_hour = Gauge(
    "ise3_load_planned_db_seconds_per_hour",
    "Database seconds per hour this plan intends to consume", ["target"])
load_planned_duty_cycle_percent = Gauge(
    "ise3_load_planned_duty_cycle_percent",
    "Planned share of wall time spent in database work", ["target"])
load_budget_requests_per_hour = Gauge(
    "ise3_load_budget_requests_per_hour",
    "Configured request ceiling; zero means none declared", ["target"])
load_budget_duty_cycle_percent = Gauge(
    "ise3_load_budget_duty_cycle_percent",
    "Configured duty-cycle ceiling; zero means none declared", ["target"])
load_budget_utilisation = Gauge(
    "ise3_load_budget_utilisation",
    "Planned load as a fraction of the declared ceiling", ["target"])
load_measured_requests_total = Counter(
    "ise3_load_measured_requests_total",
    "Requests actually sent to a target", ["target"])
load_measured_db_seconds_total = Counter(
    "ise3_load_measured_db_seconds_total",
    "Database seconds actually consumed on a target", ["target"])

# The request budget is enforced rather than merely observed, so what the
# enforcement is doing has to be visible. A budget that silently costs an hour of
# wall time is as surprising as one that is silently exceeded.
budget_enforced_requests_per_hour = Gauge(
    "ise3_budget_enforced_requests_per_hour",
    "Request rate the token bucket is currently enforcing on this target; "
    "rises to the declared warm-up burst while a cache is still filling",
    ["target"])
budget_wait_seconds_total = Counter(
    "ise3_budget_wait_seconds_total",
    "Seconds requests spent waiting for budget on this target", ["target"])
budget_throttled_total = Counter(
    "ise3_budget_throttled_total",
    "Requests that had to wait for budget before reaching the wire",
    ["target", "api"])
budget_warming = Gauge(
    "ise3_budget_warming",
    "A converging cache on this target still has work outstanding", ["target"])

# --- transport ---
api_requests_total = Counter(
    "ise3_api_requests_total", "API requests by target, API, and outcome",
    ["target", "api", "status"])
api_errors_total = Counter(
    "ise3_api_errors_total", "API errors by target, API, kind, and HTTP code",
    ["target", "api", "error_type", "http_code"])
api_request_duration_seconds = Histogram(
    "ise3_api_request_duration_seconds", "API request duration",
    ["target", "api"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30))

# --- pxGrid stream ---
pxgrid_stream_connected = Gauge(
    "ise3_pxgrid_stream_connected",
    "The pxGrid session STOMP/WebSocket is connected with a reconciled baseline")
pxgrid_stream_last_frame_timestamp = Gauge(
    "ise3_pxgrid_stream_last_frame_timestamp",
    "Unix time of the most recent pxGrid WebSocket data or control frame")
pxgrid_stream_sessions = Gauge(
    "ise3_pxgrid_stream_sessions",
    "Active sessions held in the reconciled pxGrid stream state")
pxgrid_stream_reconnects_total = Counter(
    "ise3_pxgrid_stream_reconnects_total",
    "Successful pxGrid stream connections, including the initial connection")

# --- Data Connect ---
# The duty cycle is the one budget expressed in time rather than requests, so it
# gets its own visibility: what was configured, what each view actually cost, and
# how long the resulting global cooldown froze every reporting dataset.
dataconnect_queries_total = Counter(
    "ise3_dataconnect_queries_total", "Data Connect statements by view and result",
    ["view", "result"])
dataconnect_query_duration_seconds = Histogram(
    "ise3_dataconnect_query_duration_seconds", "Oracle statement duration",
    ["view"], buckets=(0.1, 0.5, 1, 2, 5, 10, 15, 30))
dataconnect_query_last_duration_seconds = Gauge(
    "ise3_dataconnect_query_last_duration_seconds",
    "Duration of the most recent statement for a view", ["view", "result"])
dataconnect_query_rows = Gauge(
    "ise3_dataconnect_query_rows", "Rows returned by the most recent statement",
    ["view"])
dataconnect_query_cooldown_seconds = Gauge(
    "ise3_dataconnect_query_cooldown_seconds",
    "Global cooldown the most recent statement imposed on all reporting datasets",
    ["view"])
dataconnect_effective_duty_cycle_percent = Gauge(
    "ise3_dataconnect_effective_duty_cycle_percent",
    "Duty cycle the running process is enforcing, from the oracle budget")
# Repairing an undecodable byte silently would make a mangled value
# indistinguishable from the one ISE actually stores, so the repair is counted
# where the row was read. A non-zero rate here is a fact about the appliance's
# data, not about the exporter: something wrote bytes into a character column
# that the database's own character set cannot describe.
dataconnect_replaced_characters_total = Counter(
    "ise3_dataconnect_replaced_characters_total",
    "Character fields returned with an undecodable byte replaced by U+FFFD",
    ["view"])
# Ad-hoc operator statements charge the same duty cycle as a scheduled
# collection, so an unexplained cooldown on the reporting datasets has to be
# attributable to the person who spent it. The per-view families above already
# count what the statements cost; this counts who asked and what they got.
dataconnect_explorer_queries_total = Counter(
    "ise3_dataconnect_explorer_queries_total",
    "Ad-hoc Data Connect statements requested through the operator API, by "
    "outcome", ["result"])

# --- bounded breakdowns ---
# A top-K breakdown that silently drops the tail is indistinguishable from a
# complete one. Every bounded provider publishes what it returned against what
# existed, so a dashboard can show "top 1000 of 3693" rather than implying 1000
# was the whole answer.
topk_groups_returned = Gauge(
    "ise3_topk_groups_returned", "Groups published for a bounded breakdown",
    ["dataset", "breakdown"])
topk_groups_total = Gauge(
    "ise3_topk_groups_total", "Groups that existed for a bounded breakdown",
    ["dataset", "breakdown"])
topk_truncated = Gauge(
    "ise3_topk_truncated", "A bounded breakdown dropped part of its tail",
    ["dataset", "breakdown"])

dataset_series = Gauge(
    "ise3_dataset_series", "Series this dataset published in its last snapshot",
    ["dataset"])

# --- on-disk footprint ---
# The exporter is not a database. Prometheus already stores time series far
# better than a SQLite file beside the process, so anything time-shaped is
# emitted and left to Prometheus. What remains on disk is cross-process safety
# state measured in hundreds of bytes. This gauge exists so that stays true:
# growth here means someone started accumulating history in the wrong place.
exporter_state_bytes = Gauge(
    "ise3_exporter_state_bytes", "Bytes the exporter keeps on local disk")

# --- scheduler ---
lane_busy = Gauge(
    "ise3_lane_busy", "A collection is running on this target's lane", ["target"])
lane_queue_depth = Gauge(
    "ise3_lane_queue_depth", "Datasets waiting on this target's lane", ["target"])


def _reason_slug(text):
    """Compress a human reason into a bounded, stable label value."""
    slug = "".join(
        character if character.isalnum() else "_"
        for character in str(text).lower()).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug[:96] or "unavailable"


def _seed_event_counters(plan):
    """Give every event counter this plan can increment a zero-valued child.

    A labelled counter with no children exports no samples at all, so until the
    first event arrives these families are indistinguishable on a dashboard
    from an exporter that is not running. Zero rather than absent: "nothing has
    happened yet" and "nobody is counting" are different facts, and only one of
    them is reassuring. Only children the resolved plan can actually increment
    are created -- a zero for a target this deployment never talks to would be
    a promise, not a fact. ``api_errors_total`` is deliberately not here: its
    error_type x http_code space is unbounded, so the dashboards degrade it to
    an explicit zero instead.
    """
    for target in plan.targets:
        if not plan.config.targets[target.target].configured:
            continue
        # Load on Data Connect is measured in database seconds, not requests;
        # each transport only ever increments one of the two for its target.
        if target.target == "oracle":
            load_measured_db_seconds_total.labels(target=target.target)
        else:
            load_measured_requests_total.labels(target=target.target)

    for entry in plan.entries:
        if not (entry.enabled and entry.resolved):
            continue
        if entry.provider.target != "oracle":
            continue
        # A Data Connect provider names the views it reads in ``requires``, and
        # the transport labels its statement counter with the same name,
        # lowercased (transports.dataconnect.view_of). Both results, because a
        # panel split by result needs the failure series at zero to say
        # "no failures" rather than nothing.
        for requirement in entry.provider.requires:
            name = str(requirement)
            if not name.startswith("view:"):
                continue
            view = name[len("view:"):].lower()
            for result in ("success", "error"):
                dataconnect_queries_total.labels(view=view, result=result)

    detail_cache.seed(
        {(entry.name, entry.provider.name)
         for entry in plan.entries if entry.enabled and entry.resolved})


def publish_plan(plan):
    """Export the planned load and provider selection for a resolved plan."""
    _seed_event_counters(plan)
    for target in plan.targets:
        labels = {"target": target.target}
        load_planned_requests_per_hour.labels(**labels).set(
            target.load.requests_per_hour)
        load_planned_db_seconds_per_hour.labels(**labels).set(
            target.load.db_seconds_per_hour)
        load_planned_duty_cycle_percent.labels(**labels).set(
            target.load.duty_cycle_percent)
        load_budget_requests_per_hour.labels(**labels).set(
            target.budget.requests_per_hour)
        load_budget_duty_cycle_percent.labels(**labels).set(
            target.budget.duty_cycle_percent)
        load_budget_utilisation.labels(**labels).set(target.utilisation or 0.0)

    for entry in plan.entries:
        dataset_enabled.labels(dataset=entry.name).set(int(entry.enabled))
        dataset_interval_seconds.labels(dataset=entry.name).set(entry.interval)
        dataset_provider_degraded.labels(dataset=entry.name).set(int(entry.degraded))
        for rejected in entry.rejected:
            dataset_provider_available.labels(
                dataset=entry.name, provider=rejected.name).set(0)
            dataset_provider_reason_info.labels(
                dataset=entry.name, provider=rejected.name,
                reason=_reason_slug(rejected.reason)).set(1)
        if not entry.resolved:
            continue
        dataset_provider_active.labels(
            dataset=entry.name, provider=entry.provider.name).set(1)
        dataset_provider_available.labels(
            dataset=entry.name, provider=entry.provider.name).set(1)
        for alternative in entry.alternatives:
            dataset_provider_active.labels(
                dataset=entry.name, provider=alternative.name).set(0)
            dataset_provider_available.labels(
                dataset=entry.name, provider=alternative.name).set(1)
