"""The Data Connect transport's safety machinery.

This is the only transport that can degrade a production appliance, so its
guards are tested directly rather than inferred: the duty cycle comes from the
budget and nowhere else, the adaptive cooldown is what enforces it, ceilings
bound what a result can make this process retain, and the cross-process gate
survives a process that dies mid-query.
"""
import os
import threading
import time

import oracledb
import pytest
from prometheus_client import REGISTRY

from ise_exporter3 import telemetry
from ise_exporter3.config import Config
from ise_exporter3.datasets import psn_performance
from ise_exporter3.plan import PlannedDataset
from ise_exporter3.runtime import Runner
from ise_exporter3.transports import TransportError
from ise_exporter3.transports.dataconnect import (
    MAX_CRASH_LEASE_SECONDS,
    MAX_STATEMENT_TIMEOUT_PERIODS,
    MIN_QUERY_INTERVAL_SECONDS,
    SCHEMA_COLUMN_CONTRACTS,
    DataConnectTransport,
    classify_oracle_error,
    publish_schema_contract,
    view_of,
)


def _config(tmp_path, duty=2.0):
    return Config.from_document(
        {"targets": {
            "pan": {"host": "pan1", "user": "ro"},
            "oracle": {"host": "mnt1", "user": "dataconnect", "service": "cpm10"}},
         "budget": {"oracle": {"duty_cycle_percent": duty}},
         "exporter": {"state_db": str(tmp_path / "state.sqlite3")}},
        path="test.toml",
        environ={"ISE_PASS": "x", "ISE_DATACONNECT_PASSWORD": "y"})


@pytest.fixture
def transport(tmp_path):
    return DataConnectTransport(_config(tmp_path))


# --- the duty cycle comes from the budget -----------------------------------

@pytest.mark.parametrize("duty", [0.1, 1.0, 2.0])
def test_the_enforced_duty_cycle_is_the_declared_budget(tmp_path, duty):
    # v2 had this as a config knob that could disagree with the documented
    # value in three places. Here there is one source.
    assert DataConnectTransport(_config(tmp_path, duty)).duty_cycle == duty
    assert REGISTRY.get_sample_value(
        "ise3_dataconnect_effective_duty_cycle_percent") == duty


def test_the_cooldown_is_what_actually_enforces_the_duty_cycle(tmp_path):
    # A 5s statement at 2% duty must be followed by 5 * (100/2 - 1) = 245s of
    # silence across every reporting dataset.
    transport = DataConnectTransport(_config(tmp_path, duty=2.0))
    assert transport._cooldown(5.0) == pytest.approx(245.0)
    # At a tenth of the budget the same statement costs twenty times the wait.
    lean = DataConnectTransport(_config(tmp_path, duty=0.1))
    assert lean._cooldown(5.0) == pytest.approx(4995.0)


def test_a_fast_statement_still_respects_the_hard_minimum_gap(transport):
    assert transport._cooldown(0.001) == MIN_QUERY_INTERVAL_SECONDS


def test_the_crash_lease_is_capped_so_one_kill_cannot_strand_a_day(tmp_path):
    # The lease only matters post-mortem: the flock already stops a live process
    # from double-querying. At a low duty cycle the uncapped worst case was most
    # of a day of silence from a single SIGKILL.
    lean = DataConnectTransport(_config(tmp_path, duty=0.1))
    assert lean._crash_lease() == MAX_CRASH_LEASE_SECONDS


# --- the cross-process gate -------------------------------------------------

def test_the_gate_publishes_a_lease_before_any_oracle_work_starts(transport):
    descriptor = transport._acquire_gate(view="test")
    try:
        with open(transport.pacing_file) as handle:
            deadline = float(handle.read().strip())
        # Written before the query, not after: a process killed mid-statement
        # cannot release the flock, so the file is the only thing left to stop
        # the next start from hitting the database immediately.
        assert deadline > time.time()
    finally:
        transport._release_gate(descriptor, 1.0)


def test_releasing_the_gate_publishes_the_measured_cooldown(transport):
    descriptor = transport._acquire_gate(view="test")
    transport._release_gate(descriptor, 120.0)
    with open(transport.pacing_file) as handle:
        assert float(handle.read().strip()) == pytest.approx(time.time() + 120, abs=5)


def test_a_corrupt_gate_deadline_is_refused_rather_than_trusted(transport):
    os.makedirs(os.path.dirname(transport.pacing_file), exist_ok=True)
    with open(transport.pacing_file, "w") as handle:
        handle.write("not-a-deadline\n")
    with pytest.raises(TransportError) as raised:
        transport._acquire_gate(view="test")
    assert raised.value.reason == "state_unavailable"


def test_an_implausibly_distant_deadline_is_refused(transport):
    os.makedirs(os.path.dirname(transport.pacing_file), exist_ok=True)
    with open(transport.pacing_file, "w") as handle:
        handle.write(f"{time.time() + 400 * 86400:.6f}\n")
    with pytest.raises(TransportError) as raised:
        transport._acquire_gate(view="test")
    assert raised.value.reason == "state_unavailable"


def test_the_crash_lease_covers_both_bounded_periods_of_both_attempts(tmp_path):
    # One attempt costs one bounded logon and one bounded statement (the session
    # precondition runs under the statement's deadline), and the single permitted
    # reconnect repeats both. At 3% the cap is not binding, so a drift in the
    # constant would show up directly as under-reserved crash pacing.
    transport = DataConnectTransport(_config(tmp_path, duty=3.0))
    worst_case = MAX_STATEMENT_TIMEOUT_PERIODS * transport.timeout
    assert transport._crash_lease() < MAX_CRASH_LEASE_SECONDS
    assert transport._crash_lease() == pytest.approx(worst_case * (100 / 3.0 - 1))


# --- a stub Oracle session --------------------------------------------------

class _StubColumn:
    def __init__(self, name):
        self.name = name


class _StubCursor:
    def __init__(self, connection):
        self._connection = connection
        self.description = None
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_exception):
        return False

    def execute(self, sql, parameters=None):
        self._connection.statements.append(sql)
        if str(sql).upper().startswith("ALTER SESSION"):
            return
        self.description = [_StubColumn(name) for name in self._connection.columns]
        self._rows = list(self._connection.rows)

    def fetchmany(self, size):
        batch, self._rows = self._rows[:size], self._rows[size:]
        return batch


class _StubConnection:
    def __init__(self, columns=(), rows=()):
        self.columns = columns
        self.rows = rows
        self.statements = []
        self.call_timeout = 0

    def cursor(self):
        return _StubCursor(self)

    def close(self):
        return None


def test_the_session_precondition_is_issued_once_under_the_statement_deadline(
        transport):
    # It used to run inside connect(), which made one attempt cost three bounded
    # periods instead of the two the crash lease reserves.
    connection = _StubConnection(columns=("X",), rows=[(1,)])
    transport._connection = connection
    transport._execute("SELECT x FROM user_views", None, "schema_metadata")
    assert connection.statements[0] == "ALTER SESSION DISABLE PARALLEL QUERY"
    transport._execute("SELECT x FROM user_views", None, "schema_metadata")
    assert connection.statements.count("ALTER SESSION DISABLE PARALLEL QUERY") == 1


def test_a_catalog_read_does_not_erase_another_processs_cooldown(transport):
    # A catalog read is exempt from duty amplification, so it does not wait the
    # shared deadline out. It must not delete it either: an exporter restart or
    # an operator running check_schema3 would otherwise release every other
    # process from a duty-cycle cooldown it never paid for.
    far_future = time.time() + 3600
    os.makedirs(os.path.dirname(transport.pacing_file), exist_ok=True)
    with open(transport.pacing_file, "w") as handle:
        handle.write(f"{far_future:.6f}\n")
    transport._connection = _StubConnection(
        columns=("TABLE_NAME", "COLUMN_NAME"),
        rows=[("SYSTEM_SUMMARY", "ISE_NODE")])

    transport.query_catalog("SELECT table_name, column_name FROM user_tab_columns")

    with open(transport.pacing_file) as handle:
        assert float(handle.read().strip()) == pytest.approx(far_future, abs=1)


# --- a logon that never answers ---------------------------------------------

def test_a_stalled_logon_cannot_hold_the_lane_and_the_gate_forever(
        transport, monkeypatch):
    # tcp_connect_timeout only bounds getting the socket up. A wedged MnT node
    # that accepts the connection and never completes the logon used to block
    # the oracle lane for the life of the process, holding the pacing flock.
    transport.timeout = 0.2
    answer = threading.Event()
    closed = []

    class _LateConnection:
        def close(self):
            closed.append(True)

    def stall(**_kwargs):
        answer.wait(10)
        return _LateConnection()

    monkeypatch.setattr(oracledb, "connect", stall)
    started = time.monotonic()
    with pytest.raises(TransportError) as raised:
        transport.connect()
    assert raised.value.reason == "timeout"
    assert time.monotonic() - started < 5

    # The abandoned attempt may still succeed; its session must not be left
    # open, or the appliance accumulates one per stalled attempt.
    answer.set()
    for _ in range(100):
        if closed:
            break
        time.sleep(0.05)
    assert closed == [True]


# --- result ceilings --------------------------------------------------------

def test_the_row_ceiling_refuses_an_unbounded_result(transport):
    with pytest.raises(TransportError) as raised:
        transport._check_ceilings(transport.limits.result_rows, 0)
    assert raised.value.reason == "response_too_large"


def test_the_byte_ceiling_refuses_an_oversized_result(transport):
    with pytest.raises(TransportError) as raised:
        transport._check_ceilings(1, transport.limits.result_bytes + 1)
    assert raised.value.reason == "response_too_large"


def test_the_transport_enforces_the_same_ceilings_the_statements_are_built_for(
        tmp_path):
    # The defect this replaced: reporting.py capped a statement at 5,500 groups
    # while the transport refused a batch above 12,000 rows, so three full-size
    # statements failed *after* paying for their scans. One derivation now feeds
    # both, and the transport reads it rather than a constant of its own.
    transport = DataConnectTransport(_config(tmp_path))
    limits = _config(tmp_path).limits
    assert transport.limits == limits
    assert limits.batch_result_rows >= limits.group_ceiling * limits.batch_queries


# --- failure classification -------------------------------------------------

@pytest.mark.parametrize("message,reason", [
    ("ORA-01017: invalid username/password", "authentication_failed"),
    ("ORA-28000: the account is locked", "authentication_failed"),
    ("ORA-00942: table or view does not exist", "authorization_failed"),
    ("ORA-12541: TNS:no listener", "connection_failed"),
    # Thin mode wraps a TLS handshake failure in the generic connect error, so
    # a rotated MnT certificate must not be reported as an unreachable host.
    ("DPY-6005: cannot connect to database (CONNECTION_ID=x). "
     "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed", "tls_failed"),
    ("DPY-6005: cannot connect to database (CONNECTION_ID=x). "
     "[Errno 111] Connection refused", "connection_failed"),
    ("DPY-4011: the database connection was closed", "timeout"),
    ("ORA-00933: SQL command not properly ended", "invalid_response"),
])
def test_oracle_errors_map_onto_bounded_reasons(message, reason):
    assert classify_oracle_error(Exception(message)) == reason


def test_view_labels_are_bounded_and_never_contain_raw_sql():
    assert view_of("SELECT * FROM radius_accounting WHERE x") == "radius_accounting"
    assert view_of("SELECT * FROM user_tab_columns") == "schema_metadata"
    assert view_of("SELECT 1 FROM dual") == "other"


# --- catalog reads ----------------------------------------------------------

def test_a_catalog_read_may_only_touch_the_dictionary(transport):
    with pytest.raises(ValueError, match="dictionary view"):
        transport.query_catalog("SELECT * FROM radius_accounting")
    with pytest.raises(ValueError, match="dictionary view"):
        transport.query_catalog("DELETE FROM user_views")


class _FailingCatalogTransport(DataConnectTransport):
    """Schema discovery always fails, and no cooldown is actually slept."""

    def __init__(self, config):
        super().__init__(config)
        self.executed = 0
        self.cooldowns = []

    def _wait(self, seconds):
        return None

    def _execute(self, sql, parameters, view):
        self.executed += 1
        time.sleep(0.05)
        raise TransportError("timeout", "DPY-4011: the connection was closed")

    def _cooldown(self, duration, adaptive=True):
        cooldown = super()._cooldown(duration, adaptive)
        self.cooldowns.append((adaptive, cooldown))
        return cooldown


def test_a_failing_schema_discovery_is_not_hammered_at_the_exempt_rate(tmp_path):
    # prepare() runs on every attempt of every oracle dataset, so a dictionary
    # scan that keeps failing used to repeat back to back at the catalog
    # exemption's ~50% duty instead of the declared budget.
    transport = _FailingCatalogTransport(_config(tmp_path, duty=0.1))
    with pytest.raises(TransportError):
        transport.prepare()
    assert transport.executed == 1
    # The first, cheap compatibility check keeps the fast lane.
    assert transport.cooldowns[-1] == (False, MIN_QUERY_INTERVAL_SECONDS)

    for _ in range(5):
        with pytest.raises(TransportError) as raised:
            transport.prepare()
        assert raised.value.reason == "schema_pending"
    assert transport.executed == 1      # refused from cache, no round trip

    transport._schema_retry_at = 0.0
    with pytest.raises(TransportError):
        transport.prepare()
    assert transport.executed == 2
    # A repeat is charged the full duty amplification like any other statement.
    adaptive, cooldown = transport.cooldowns[-1]
    assert adaptive
    assert cooldown == pytest.approx(0.05 * (100 / 0.1 - 1), rel=0.5)


# --- schema capability ------------------------------------------------------

class StubTransport(DataConnectTransport):
    """A transport whose statements are scripted, so no Oracle is required."""

    def __init__(self, config, results=None, schema=None, failure=None):
        super().__init__(config)
        self.results = results or {}
        self.failure = failure
        self.statements = []
        self._schema = schema

    def query(self, sql, parameters=None, *, adaptive=True):
        self.statements.append(sql)
        if self.failure is not None:
            raise self.failure
        return self.results.get("rows", [])

    def query_many(self, statements, parameters=None):
        self.statements.extend(statements.values())
        if self.failure is not None:
            raise self.failure
        return {name: self.results.get(name, []) for name in statements}


def test_a_missing_view_blocks_only_the_datasets_that_need_it(tmp_path):
    transport = StubTransport(_config(tmp_path), schema={"SYSTEM_SUMMARY": {"ISE_NODE"}})
    ready, reason, detail = transport.satisfies(
        ("view:KEY_PERFORMANCE_METRICS", "view:SYSTEM_SUMMARY"))
    assert not ready and reason == "schema_incompatible"
    assert "KEY_PERFORMANCE_METRICS" in detail
    # The view it can see is fine on its own.
    assert transport.satisfies(("view:SYSTEM_SUMMARY",))[0]


def test_requirements_are_pending_not_satisfied_before_discovery(tmp_path):
    transport = StubTransport(_config(tmp_path))
    ready, reason, _ = transport.satisfies(("view:SYSTEM_SUMMARY",))
    assert not ready and reason == "schema_pending"


def test_a_provider_is_not_run_against_a_schema_that_cannot_answer(tmp_path):
    transport = StubTransport(_config(tmp_path), schema={"SYSTEM_SUMMARY": {"ISE_NODE"}})
    entry = PlannedDataset(
        name="psn_performance", description="", enabled=True, interval=300,
        dataset=psn_performance.DATASET,
        provider=psn_performance.DATASET.providers[0])
    outcome = Runner(_config(tmp_path)).run(entry, transport)
    assert not outcome.ok and outcome.reason == "schema_incompatible"
    # No SQL was issued for a dataset that could not be answered.
    assert transport.statements == []


# --- the reporting dataset --------------------------------------------------

FULL_SCHEMA = {
    "KEY_PERFORMANCE_METRICS": {
        "ISE_NODE", "LOGGED_TIME", "RADIUS_REQUESTS_HR", "LOGGED_TO_MNT_HR",
        "NOISE_HR", "SUPPRESSION_HR", "AVG_LOAD", "AVG_LATENCY_PER_REQ", "AVG_TPS"},
    "SYSTEM_SUMMARY": {
        "ISE_NODE", "TIMESTAMP", "CPU_UTILIZATION", "MEMORY_UTILIZATION",
        "DISKSPACE_ROOT", "DISKSPACE_OPT"},
}


def test_psn_performance_publishes_the_latest_sample_per_node(tmp_path):
    transport = StubTransport(_config(tmp_path), schema=FULL_SCHEMA, results={
        "kpi": [{"ise_node": "psn1", "radius_requests_hr": 4200,
                 "avg_latency_per_req": 0.031, "avg_tps": 1.2}],
        "system": [{"ise_node": "psn1", "cpu_utilization": 41,
                    "memory_utilization": 63, "diskspace_root": 22}],
    })
    entry = PlannedDataset(
        name="psn_performance", description="", enabled=True, interval=300,
        dataset=psn_performance.DATASET,
        provider=psn_performance.DATASET.providers[0])
    outcome = Runner(_config(tmp_path)).run(entry, transport)

    assert outcome.ok, outcome.detail
    assert REGISTRY.get_sample_value(
        "ise3_psn_radius_requests_per_hour",
        {"provider": "dataconnect", "node": "psn1"}) == 4200
    assert REGISTRY.get_sample_value(
        "ise3_node_disk_utilization_percent",
        {"provider": "dataconnect", "node": "psn1", "filesystem": "root"}) == 22


def test_absent_optional_columns_degrade_one_dimension_not_the_dataset(tmp_path):
    # This ISE exposes the views but not the TPS column.
    schema = {
        "KEY_PERFORMANCE_METRICS": {"ISE_NODE", "LOGGED_TIME", "RADIUS_REQUESTS_HR"},
        "SYSTEM_SUMMARY": {"ISE_NODE", "TIMESTAMP", "CPU_UTILIZATION"},
    }
    statements = psn_performance.statements(_config(tmp_path).limits, schema)
    assert "avg_tps" not in statements["kpi"]
    assert "radius_requests_hr" in statements["kpi"]


def test_schema_contract_publishes_required_and_optional_capability_gaps():
    schema = {
        "KEY_PERFORMANCE_METRICS": {"ISE_NODE", "LOGGED_TIME"},
        "RADIUS_ACCOUNTING": {"TIMESTAMP", "DEVICE_NAME"},
    }
    publish_schema_contract(schema)

    assert REGISTRY.get_sample_value(
        "ise3_dataconnect_schema_view_available",
        {"view": "KEY_PERFORMANCE_METRICS"},
    ) == 1
    assert REGISTRY.get_sample_value(
        "ise3_dataconnect_schema_view_available",
        {"view": "SYSTEM_SUMMARY"},
    ) == 0
    assert REGISTRY.get_sample_value(
        "ise3_dataconnect_schema_column_available",
        {
            "view": "KEY_PERFORMANCE_METRICS",
            "column": "ISE_NODE",
            "requirement": "required",
        },
    ) == 1
    assert REGISTRY.get_sample_value(
        "ise3_dataconnect_schema_column_available",
        {
            "view": "KEY_PERFORMANCE_METRICS",
            "column": "AVG_TPS",
            "requirement": "optional",
        },
    ) == 0
    assert set(SCHEMA_COLUMN_CONTRACTS) <= {
        sample.labels["view"]
        for family in REGISTRY.collect()
        for sample in family.samples
        if sample.name == "ise3_dataconnect_schema_view_available"
    }


def test_psn_diagnostics_publish_a_bounded_work_queue_when_views_exist(tmp_path):
    schema = {
        **FULL_SCHEMA,
        "AAA_DIAGNOSTICS_VIEW": {
            "ISE_NODE", "TIMESTAMP", "MESSAGE_SEVERITY", "CATEGORY",
            "MESSAGE_CODE",
        },
        "SYSTEM_DIAGNOSTICS_VIEW": {
            "ISE_NODE", "TIMESTAMP", "MESSAGE_SEVERITY", "CATEGORY",
            "MESSAGE_CODE",
        },
    }
    row = {
        "node": "psn1",
        "severity": "WARN",
        "category": "RADIUS",
        "message_code": "5100",
        "events": 3,
        "total_events": 11,
        "group_total": 4,
    }
    transport = StubTransport(_config(tmp_path), schema=schema, results={
        "aaa_diagnostics": [row],
        "system_diagnostics": [{**row, "category": "SYSTEM"}],
    })
    entry = PlannedDataset(
        name="psn_performance",
        description="",
        enabled=True,
        interval=300,
        dataset=psn_performance.DATASET,
        provider=psn_performance.DATASET.providers[0],
    )
    outcome = Runner(_config(tmp_path)).run(entry, transport)

    assert outcome.ok, outcome.detail
    assert REGISTRY.get_sample_value(
        "ise3_psn_diagnostic_events",
        {
            "provider": "dataconnect",
            "source": "aaa",
            "node": "psn1",
            "severity": "WARN",
            "category": "RADIUS",
            "message_code": "5100",
        },
    ) == 3
    assert REGISTRY.get_sample_value(
        "ise3_psn_diagnostic_events_total",
        {"provider": "dataconnect", "source": "aaa"},
    ) == 11
    assert REGISTRY.get_sample_value(
        "ise3_topk_truncated",
        {"dataset": "psn_performance", "breakdown": "aaa_diagnostics"},
    ) == 1


def test_psn_diagnostics_omit_a_view_missing_its_required_columns(tmp_path):
    schema = {
        **FULL_SCHEMA,
        "AAA_DIAGNOSTICS_VIEW": {"MESSAGE_SEVERITY", "CATEGORY"},
    }
    statements = psn_performance.statements(_config(tmp_path).limits, schema)
    assert "aaa_diagnostics" not in statements


def test_the_scan_window_is_bounded_in_the_statement_itself(tmp_path):
    # An unbounded scan of an 80-200 GB history is the failure this prevents,
    # and the bound has to be in the SQL, not in the caller's intentions.
    for sql in psn_performance.statements(_config(tmp_path).limits, FULL_SCHEMA).values():
        assert "NUMTODSINTERVAL" in sql
        assert "SYSTIMESTAMP" in sql


def test_measured_database_seconds_are_counted_for_the_load_model(tmp_path):
    before = REGISTRY.get_sample_value(
        "ise3_load_measured_db_seconds_total", {"target": "oracle"}) or 0.0
    telemetry.load_measured_db_seconds_total.labels(target="oracle").inc(1.5)
    after = REGISTRY.get_sample_value(
        "ise3_load_measured_db_seconds_total", {"target": "oracle"})
    assert after == pytest.approx(before + 1.5)
