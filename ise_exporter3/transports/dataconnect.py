"""Read-only Cisco ISE Data Connect transport.

Data Connect is Oracle over TCPS on the MnT node, sitting in front of an 80-200 GB
event history on a live appliance. This is the only place in the exporter where a
single mistake can degrade production ISE, so it is also the only transport with
this much machinery. Ported from v2's client, which earned every one of these
guards; the structural change is that the duty cycle is no longer a tuning knob
but comes from ``budget.oracle.duty_cycle_percent``.

What the guards do, and why each exists:

- A **cross-process pacing gate** (a small lock file) serialises statements
  between the exporter and any other authorised process. Without it, two
  processes each honour their own cooldown and the appliance sees double.
- A **pre-work crash lease** is written before Oracle work starts, because the
  flock dies with the process. Without it, SIGKILL during a query means the next
  start hits the database immediately, and a crash-loop becomes a hammer.
- An **adaptive cooldown** of ``duration x (100/duty - 1)`` after each statement
  is what actually enforces the duty cycle.
- **Row, byte, field and nesting ceilings** bound what a malformed or hostile
  result can make this process retain.
- ``ALTER SESSION DISABLE PARALLEL QUERY`` is a precondition, not a nicety: a
  small aggregate can otherwise fan out across parallel workers on the cluster.
- One reconnect is retried, because ISE expires healthy sessions on a fixed
  lifetime. Authentication and SQL errors are never retried.
"""
from __future__ import annotations

import base64
import fcntl
import logging
import math
import os
import stat
import ssl
import threading
import time

import oracledb

from .. import telemetry
from ..auth_guard import PersistentAuthGuard
from ..compatibility import valid_hostname
from . import Transport, TransportError, guard_path


logger = logging.getLogger(__name__)

# Row, byte and nesting ceilings come from ``config.limits``, derived from the
# declared scale -- see limits.py. They used to be three constants here, and two
# of them contradicted a third in reporting.py: a batch was allowed five
# statements and 5,500 groups each, but only 12,000 rows in total, so any
# dataset with two large dimensions failed every collection *after* paying for
# the Oracle scans. Deriving them together is what makes that unrepresentable.
#
# How many rows to pull per round trip. Not a ceiling: purely how often the
# fetch loop gets to re-check the deadline and the ceilings above.
FETCH_BATCH_ROWS = 100

# A statement may spend one timeout connecting and one executing, then repeat
# both after the single permitted reconnect. A crash lease must reserve all four.
MAX_STATEMENT_TIMEOUT_PERIODS = 4
# Hard safety floor between statements. Not configurable: this is a floor, not a
# preference, and the duty cycle is the knob that actually shapes load.
MIN_QUERY_INTERVAL_SECONDS = 5.0
QUERY_TIMEOUT_SECONDS = 15
# The pre-work lease only matters post-mortem -- the flock already stops any live
# process from double-querying. Capping it at an hour keeps a crashed process
# from stranding all reporting for most of a day at a low duty cycle. Measured
# post-completion cooldowns are exempt and may legitimately exceed this.
MAX_CRASH_LEASE_SECONDS = 3600
MAX_LEASE_FUTURE_SECONDS = 36 * 86400

AUTH_FAILURE_THRESHOLD = 3
AUTH_FAILURE_BACKOFF_SECONDS = 900
CONNECT_FAILURE_THRESHOLD = 3

_BUSY = object()

_VIEWS = (
    "tacacs_authentication_last_two_days", "tacacs_authorization_last_two_days",
    "tacacs_accounting_last_two_days", "posture_assessment_by_condition",
    "posture_assessment_by_endpoint", "profiled_endpoints_summary",
    "radius_authentication_summary", "radius_authentications_week",
    "radius_authentications", "radius_accounting", "radius_errors_view",
    "key_performance_metrics", "system_diagnostics_view", "aaa_diagnostics_view",
    "system_summary", "endpoints_data",
)

_RETRYABLE_DISCONNECT = (
    "ORA-02399", "ORA-03113", "ORA-03114", "ORA-03135",
    "DPY-1001", "DPY-4010", "DPY-4011",
)
_AUTH_FAILURE = (
    "ORA-01005", "ORA-01017", "ORA-28000", "ORA-28001", "DPY-4001",
    "INVALID CREDENTIAL", "INVALID USERNAME/PASSWORD",
)
_AUTHORIZATION_FAILURE = ("ORA-00942", "ORA-01031")
_CONNECTION_FAILURE = ("ORA-12170", "ORA-12514", "ORA-12541", "DPY-6005")


def view_of(sql):
    """Return a bounded metric label; never put arbitrary SQL into Prometheus."""
    text = str(sql or "").lower()
    if "ise_exporter:freshness" in text:
        return "freshness_probe"
    # Schema discovery names every reporting view in an IN clause, so classify
    # catalog access before scanning for view literals.
    if "user_tab_columns" in text or "user_views" in text:
        return "schema_metadata"
    for view in _VIEWS:
        if view in text:
            return view
    return "other"


def classify_oracle_error(error):
    """Map an Oracle failure onto the bounded transport vocabulary."""
    message = str(error).upper()
    if any(code in message for code in _AUTH_FAILURE):
        return "authentication_failed"
    if any(code in message for code in _AUTHORIZATION_FAILURE):
        return "authorization_failed"
    if any(code in message for code in _CONNECTION_FAILURE):
        return "connection_failed"
    if isinstance(error, TimeoutError) or "TIMEOUT" in message or "DPY-4011" in message:
        return "timeout"
    if "CERTIFICATE" in message or "SSL" in message or "TLS" in message:
        return "tls_failed"
    return "invalid_response"


def _materialize(value, limits, *, depth=0):
    """Convert one Oracle field without expanding an unbounded nested value."""
    if depth > limits.field_nesting_depth:
        raise TransportError(
            "invalid_response",
            f"field exceeded the {limits.field_nesting_depth}-level nesting ceiling")
    ceiling = limits.field_bytes
    if hasattr(value, "read") and callable(value.read):
        size = getattr(value, "size", None)
        if not callable(size):
            raise TransportError(
                "invalid_response", "LOB has no bounded size; refusing to read it")
        if int(size()) > ceiling:
            raise TransportError(
                "response_too_large", f"field exceeded {ceiling} bytes")
        value = value.read()
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytes):
        if len(value) > ceiling:
            raise TransportError(
                "response_too_large", f"field exceeded {ceiling} bytes")
        return "base64:" + base64.b64encode(value).decode("ascii")
    if isinstance(value, str) and len(value.encode("utf-8")) > ceiling:
        raise TransportError(
            "response_too_large", f"field exceeded {ceiling} bytes")
    if isinstance(value, (list, tuple)):
        retained, result = 0, []
        for item in value:
            materialized = _materialize(item, limits, depth=depth + 1)
            retained += _size_of(materialized, limits, depth=depth + 1)
            if retained > ceiling:
                raise TransportError(
                    "response_too_large", "nested field exceeded the byte ceiling")
            result.append(materialized)
        return result
    if isinstance(value, dict):
        retained, result = 0, {}
        for key, item in value.items():
            materialized = _materialize(item, limits, depth=depth + 1)
            retained += _size_of(key, limits, depth=depth + 1)
            retained += _size_of(materialized, limits, depth=depth + 1)
            if retained > ceiling:
                raise TransportError(
                    "response_too_large", "nested field exceeded the byte ceiling")
            result[key] = materialized
        return result
    return value


def _size_of(value, limits, *, depth=0):
    if depth > limits.field_nesting_depth:
        raise TransportError("invalid_response", "field nesting ceiling exceeded")
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    if isinstance(value, (bytes, bytearray, memoryview)):
        return len(value)
    if isinstance(value, dict):
        return sum(_size_of(key, limits, depth=depth + 1)
                   + _size_of(item, limits, depth=depth + 1)
                   for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return sum(_size_of(item, limits, depth=depth + 1) for item in value)
    return 8


class DataConnectTransport(Transport):
    target = "oracle"

    def __init__(self, config):
        settings = config.targets["oracle"]
        if not valid_hostname(settings.host):
            raise ValueError(
                "targets.oracle.host must be a bare DNS hostname or IPv4 address")
        self.host = settings.host
        self.port = settings.port or 2484
        self.service = settings.service
        self.user = settings.user
        self.password = settings.password
        self.ca_bundle = settings.ca_bundle
        self.verify = settings.verify_tls
        self.timeout = QUERY_TIMEOUT_SECONDS
        self.min_query_interval = MIN_QUERY_INTERVAL_SECONDS
        # One resolved set of ceilings for the whole process. The reporting
        # statements are built against the same object, so "what a statement may
        # ask for" and "what this transport will accept" cannot disagree.
        self.limits = config.limits

        # The duty cycle is a budget decision, not a tuning knob. This is the
        # whole point of v3: what the operator declared they will spend is what
        # the runtime enforces, with no second place to disagree.
        declared = config.budget["oracle"].duty_cycle_percent
        self.duty_cycle = declared if declared > 0 else 1.0
        telemetry.dataconnect_effective_duty_cycle_percent.set(self.duty_cycle)

        self.pacing_file = os.path.join(
            os.path.dirname(guard_path(config, "oracle")), "dataconnect.pacing")
        self._auth_guard = PersistentAuthGuard(
            guard_path(config, "oracle"),
            (self.user, self.host, self.port, self.service),
            "Data Connect authentication")

        self._connection = None
        self._connect_failures = 0
        self._blocked_until = 0.0
        self._next_query_at = 0.0
        self._shutdown = None
        self._lock = threading.RLock()
        self._batch_active = False
        # A catalogue read is a different shape of query from a reporting one --
        # fixed-size, set by the ISE release rather than by the estate -- so it
        # is bounded by limits.catalog_rows instead of limits.result_rows.
        self._catalog_active = False
        self._batch_gate = None
        self._batch_duration = 0.0
        self._batch_rows = 0
        self._batch_bytes = 0
        self._batch_views = []

    # --- lifecycle --------------------------------------------------------

    def set_shutdown_event(self, shutdown):
        self._shutdown = shutdown

    def close(self):
        connection, self._connection = self._connection, None
        if connection is not None:
            try:
                connection.close()
            except Exception as error:      # noqa: BLE001 - teardown must not raise
                logger.debug("closing the Data Connect session failed: %s", error)

    def _wait(self, seconds):
        """Sleep, but let shutdown interrupt a long adaptive cooldown."""
        if seconds <= 0:
            return
        if self._shutdown is not None:
            if self._shutdown.wait(seconds):
                raise TransportError(
                    "unexpected_error", "cancelled during exporter shutdown")
        else:
            time.sleep(seconds)

    def _ssl_context(self):
        if not self.verify:
            return ssl._create_unverified_context()
        return ssl.create_default_context(cafile=self.ca_bundle or None)

    def connect(self):
        if self._connection is not None:
            return self._connection
        try:
            blocked = self._auth_guard.blocked(time.time())
        except Exception as error:
            raise TransportError(
                "state_unavailable",
                "the Data Connect authentication guard is unavailable") from error
        if blocked:
            raise TransportError("authentication_backoff")
        remaining = self._blocked_until - time.monotonic()
        if remaining > 0:
            raise TransportError(
                "connection_failed",
                f"reconnect suppressed for {remaining:.0f}s after "
                f"{self._connect_failures} connection failures")

        connection = None
        try:
            connection = oracledb.connect(
                user=self.user, password=self.password, host=self.host,
                port=self.port, service_name=self.service, protocol="tcps",
                ssl_context=self._ssl_context(), ssl_server_dn_match=self.verify,
                tcp_connect_timeout=self.timeout)
            connection.call_timeout = self.timeout * 1000
            # A bounded aggregate can still consume disproportionate cluster
            # resources if Oracle parallelises the view scan behind it. This is
            # monitoring, never a batch workload.
            with connection.cursor() as cursor:
                cursor.execute("ALTER SESSION DISABLE PARALLEL QUERY", {})
        except Exception as error:
            if connection is not None:
                try:
                    connection.close()
                except Exception:       # noqa: BLE001
                    pass
            self._connect_failures += 1
            if self._connect_failures >= CONNECT_FAILURE_THRESHOLD:
                self._blocked_until = time.monotonic() + AUTH_FAILURE_BACKOFF_SECONDS
            reason = classify_oracle_error(error)
            if reason == "authentication_failed":
                self._auth_guard.failure(
                    AUTH_FAILURE_THRESHOLD, AUTH_FAILURE_BACKOFF_SECONDS, time.time())
            raise TransportError(reason, str(error)) from error

        try:
            self._auth_guard.success()
        except Exception as error:
            connection.close()
            raise TransportError(
                "state_unavailable",
                "the Data Connect authentication guard could not record success"
            ) from error
        self._connection = connection
        self._connect_failures = 0
        self._blocked_until = 0.0
        return connection

    # --- cross-process pacing gate ---------------------------------------

    def _acquire_gate(self, *, view, adaptive=True):
        """Take the shared gate, waiting out any cooldown another process left."""
        path = os.path.abspath(os.path.expanduser(self.pacing_file))
        descriptor = None
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True, mode=0o750)
            descriptor = os.open(
                path,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0), 0o660)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError("the pacing gate is not a regular file")
            if metadata.st_size > 64:
                raise OSError("pacing gate state exceeds 64 bytes")
            if metadata.st_uid == os.geteuid():
                os.fchown(descriptor, -1, os.stat(os.path.dirname(path)).st_gid)
                os.fchmod(descriptor, 0o660)

            # A blocking flock cannot be interrupted, and another process may
            # hold the gate through a long cooldown. Poll instead, so shutdown
            # does not have to wait minutes on a kernel lock.
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    self._wait(0.25)

            raw = os.read(descriptor, 64).decode("ascii").strip()
            deadline = float(raw) if raw else 0.0
            if not math.isfinite(deadline) or deadline < 0:
                raise OSError("the pacing gate deadline is not a finite value")
            remaining = deadline - time.time()
            if remaining > MAX_LEASE_FUTURE_SECONDS:
                raise OSError("the pacing gate deadline is implausibly far ahead")
            if remaining > 0 and adaptive:
                logger.info(
                    "Data Connect waiting view=%s seconds=%.1f "
                    "reason=shared_duty_cycle_cooldown", view, remaining)
                self._wait(remaining)

            self._write_lease(descriptor, self._crash_lease(adaptive), deadline
                              if not adaptive else 0.0)
            return descriptor
        except TransportError:
            if descriptor is not None:
                os.close(descriptor)
            raise
        except Exception as error:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise TransportError(
                "state_unavailable",
                f"the Data Connect pacing gate is unavailable at {path}") from error

    def _crash_lease(self, adaptive=True, elapsed=0.0):
        worst_case = elapsed + MAX_STATEMENT_TIMEOUT_PERIODS * self.timeout
        cooldown = max(
            self.min_query_interval,
            worst_case * (100 / self.duty_cycle - 1) if adaptive else worst_case)
        return min(cooldown, MAX_CRASH_LEASE_SECONDS)

    @staticmethod
    def _write_lease(descriptor, cooldown, floor=0.0):
        deadline = max(time.time() + cooldown, floor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.ftruncate(descriptor, 0)
        os.write(descriptor, f"{deadline:.6f}\n".encode("ascii"))
        os.fsync(descriptor)

    @classmethod
    def _release_gate(cls, descriptor, cooldown):
        if descriptor is None:
            return
        try:
            cls._write_lease(descriptor, cooldown)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    # --- statements -------------------------------------------------------

    def query(self, sql, parameters=None, *, adaptive=True):
        """Run one bounded read-only statement under the pacing gate."""
        with self._lock:
            return self._query(sql, parameters, adaptive=adaptive)

    def _query(self, sql, parameters=None, *, adaptive=True):
        view = view_of(sql)
        if not self._batch_active:
            self._wait(self._next_query_at - time.monotonic())

        gate = self._batch_gate
        if not self._batch_active:
            gate = self._acquire_gate(view=view, adaptive=adaptive)

        started = time.monotonic()
        result = "error"
        try:
            rows = self._execute(sql, parameters, view)
            result = "success"
            return rows
        finally:
            duration = max(0.0, time.monotonic() - started)
            telemetry.load_measured_db_seconds_total.labels(
                target="oracle").inc(duration)
            telemetry.dataconnect_queries_total.labels(view=view, result=result).inc()
            telemetry.dataconnect_query_duration_seconds.labels(view=view).observe(duration)
            telemetry.dataconnect_query_last_duration_seconds.labels(
                view=view, result=result).set(duration)
            if result == "error":
                telemetry.dataconnect_query_rows.labels(view=view).set(0)

            if self._batch_active:
                self._batch_duration += duration
                self._batch_views.append(view)
            else:
                cooldown = self._cooldown(duration, adaptive)
                self._next_query_at = time.monotonic() + cooldown
                telemetry.dataconnect_query_cooldown_seconds.labels(view=view).set(cooldown)
                self._release_gate(gate, cooldown)

    def _cooldown(self, duration, adaptive=True):
        """The adaptive cooldown is what actually enforces the duty cycle."""
        spend = duration * (100 / self.duty_cycle - 1) if adaptive else duration
        return max(self.min_query_interval, spend)

    def _execute(self, sql, parameters, view):
        for attempt in range(2):
            try:
                connection = self.connect()
                deadline = time.perf_counter() + self.timeout
                with connection.cursor() as cursor:
                    self._apply_timeout(connection, deadline)
                    cursor.execute(sql, parameters or {})
                    columns = [column.name.lower() for column in cursor.description]
                    rows, retained = [], 0
                    while True:
                        self._apply_timeout(connection, deadline)
                        batch = cursor.fetchmany(FETCH_BATCH_ROWS)
                        if not batch:
                            break
                        for raw in batch:
                            self._check_ceilings(len(rows), retained)
                            row = dict(zip(columns, (
                                _materialize(value, self.limits) for value in raw)))
                            retained += _size_of(row, self.limits)
                            rows.append(row)
                if self._batch_active:
                    self._batch_rows += len(rows)
                    self._batch_bytes += retained
                telemetry.dataconnect_query_rows.labels(view=view).set(len(rows))
                return rows
            except TransportError:
                self.close()
                raise
            except Exception as error:
                self.close()
                # ISE expires healthy sessions on a fixed lifetime, so one
                # reconnect inside the same paced statement avoids losing a whole
                # cadence to an idle period. Nothing else is ever retried.
                if attempt == 0 and any(
                        code in str(error).upper() for code in _RETRYABLE_DISCONNECT):
                    logger.info("Data Connect session expired; reconnecting once")
                    continue
                raise TransportError(
                    classify_oracle_error(error), str(error)) from error
        raise TransportError("unexpected_error", "the reconnect retry fell through")

    def _check_ceilings(self, row_count, retained):
        limits = self.limits
        ceiling = (limits.catalog_rows if self._catalog_active
                   else limits.result_rows)
        if row_count >= ceiling:
            raise TransportError(
                "response_too_large",
                f"result exceeded the {ceiling}-row ceiling")
        if (self._batch_active
                and self._batch_rows + row_count >= limits.batch_result_rows):
            raise TransportError(
                "response_too_large",
                f"batch exceeded the {limits.batch_result_rows}-row ceiling")
        carried = self._batch_bytes if self._batch_active else 0
        if carried + retained > limits.result_bytes:
            raise TransportError(
                "response_too_large",
                f"result exceeded the {limits.result_bytes}-byte ceiling")

    @staticmethod
    def _apply_timeout(connection, deadline):
        """Bound every round trip to one total per-attempt budget."""
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            raise TimeoutError("statement exceeded its hard attempt timeout")
        connection.call_timeout = max(1, math.ceil(remaining * 1000))

    def query_many(self, statements, parameters=None):
        """Run a small atomic set of statements under one duty-cycle lease.

        One dashboard update often needs several bounded statements. Charging
        each its own cooldown would put hours between them at a low duty cycle;
        charging the batch once, on combined Oracle time, keeps the long-run
        duty cycle honest while letting a snapshot actually complete.
        """
        with self._lock:
            items = list(statements.items())
            if not items:
                return {}
            if len(items) > self.limits.batch_queries:
                raise ValueError(
                    f"batch exceeds the {self.limits.batch_queries}-statement "
                    "ceiling")
            if self._batch_active:
                raise RuntimeError("nested Data Connect batches are not supported")

            self._wait(self._next_query_at - time.monotonic())
            views = ",".join(dict.fromkeys(view_of(sql) for _name, sql in items))
            gate = self._acquire_gate(view=views)
            self._batch_active = True
            self._batch_gate = gate
            self._batch_duration = 0.0
            self._batch_rows = 0
            self._batch_bytes = 0
            self._batch_views = []
            failed = False
            try:
                results = {}
                given = parameters or {}
                for index, (name, sql) in enumerate(items):
                    if index:
                        self._wait(self.min_query_interval)
                        # Advance the crash lease before each later statement so
                        # a kill early in a batch cannot strand every reporting
                        # dataset behind a whole-batch worst case.
                        self._write_lease(
                            gate, self._crash_lease(elapsed=self._batch_duration))
                    results[name] = self._query(sql, given.get(name))
                return results
            except BaseException:
                failed = True
                raise
            finally:
                cooldown = self._cooldown(self._batch_duration)
                self._next_query_at = time.monotonic() + cooldown
                for view in set(self._batch_views):
                    telemetry.dataconnect_query_cooldown_seconds.labels(
                        view=view).set(cooldown)
                self._batch_active = False
                self._batch_gate = None
                self._batch_duration = 0.0
                self._batch_rows = 0
                self._batch_bytes = 0
                self._batch_views = []
                try:
                    self._release_gate(gate, cooldown)
                except Exception:       # noqa: BLE001
                    # A failed release means the cross-process deadline was not
                    # durably published; do not report success. Preserve an
                    # existing error rather than masking it with cleanup.
                    if failed:
                        logger.exception("releasing the Data Connect gate also failed")
                    else:
                        raise

    # --- schema capability ------------------------------------------------

    def discover_schema(self):
        """Read the view/column catalog once, on the same paced lane.

        This is what turns a provider's declared ``view:`` requirement into a
        real check. A missing view blocks only the datasets that depend on it;
        everything else keeps collecting, which is the difference between one
        absent view and a dead exporter.
        """
        rows = self.query_catalog(
            "SELECT table_name, column_name FROM user_tab_columns")
        schema = {}
        for row in rows:
            table = str(row.get("table_name") or "").upper()
            column = str(row.get("column_name") or "").upper()
            if table and column:
                schema.setdefault(table, set()).add(column)
        self._schema = schema
        logger.info("discovered %d Data Connect reporting views", len(schema))
        return schema

    @property
    def schema(self):
        return getattr(self, "_schema", None)

    def prepare(self):
        if self.schema is None:
            self.discover_schema()

    def satisfies(self, requirements):
        """Settle a provider's deferred view requirements against the catalog."""
        views = [str(item).split(":", 1)[1].upper()
                 for item in requirements if str(item).startswith("view:")]
        if not views:
            return True, "", ""
        schema = self.schema
        if schema is None:
            return False, "schema_pending", (
                "Data Connect schema discovery has not completed yet")
        missing = sorted(view for view in views if view not in schema)
        if missing:
            return False, "schema_incompatible", (
                f"this Data Connect account cannot see {', '.join(missing)}")
        return True, "", ""

    def query_catalog(self, sql, parameters=None):
        """Read bounded Oracle dictionary metadata without duty amplification.

        Catalog reads keep the gate, the timeout and every ceiling, but the
        dictionary does not scale with the event history. Charging a one-second
        compatibility check as reporting duty would postpone the first real
        query by many minutes for no reduction in production load.
        """
        text = " ".join(str(sql or "").lower().split())
        referenced = set()
        for keyword in ("from ", "join "):
            parts = text.split(keyword)[1:]
            referenced.update(part.split()[0] for part in parts if part.split())
        if not text.startswith("select ") or not referenced <= {
                "user_tab_columns", "user_views"}:
            raise ValueError(
                "a catalog query must be a SELECT from an allowed dictionary view")
        with self._lock:
            self._catalog_active = True
            try:
                return self._query(sql, parameters, adaptive=False)
            finally:
                self._catalog_active = False
