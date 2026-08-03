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
import errno
import fcntl
import logging
import math
import os
import stat
import ssl
import threading
import time

import oracledb

from .. import probe_data, telemetry
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

# A statement may spend one timeout on the whole logon and one on the statement
# itself (the session precondition is issued under the statement's own deadline),
# then repeat both after the single permitted reconnect. A crash lease must
# reserve all four.
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

# How far light cut-ins may push the shared deadline before they have to queue
# like everything else. One lookup during a long cooldown is the point of the
# light lane and costs the scheduler a few seconds; a loop of them charges a
# full cooldown each time while never waiting itself, which would push the
# deadline out faster than real time drains it and starve the scheduler for as
# long as the loop ran. Bounding the light lane's own contribution -- rather
# than the total backlog -- is what tells those two apart: the single lookup
# still cuts in during a five-minute scan cooldown, the loop stops cutting in
# after a minute of it and starts waiting.
LIGHT_DEBT_CEILING_SECONDS = 60.0

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

# Runtime schema visibility for every optional capability a dashboard can lose
# without making its whole dataset unusable. Keeping this bounded expected set
# avoids exporting arbitrary catalogue contents as Prometheus labels.
SCHEMA_COLUMN_CONTRACTS = {
    "KEY_PERFORMANCE_METRICS": {
        "required": ("ISE_NODE", "LOGGED_TIME"),
        "optional": (
            "RADIUS_REQUESTS_HR", "LOGGED_TO_MNT_HR", "NOISE_HR",
            "SUPPRESSION_HR", "AVG_LOAD", "AVG_LATENCY_PER_REQ", "AVG_TPS",
        ),
    },
    "SYSTEM_SUMMARY": {
        "required": ("ISE_NODE", "TIMESTAMP"),
        "optional": (
            "CPU_UTILIZATION", "MEMORY_UTILIZATION", "DISKSPACE_ROOT",
            "DISKSPACE_BOOT", "DISKSPACE_OPT", "DISKSPACE_TMP",
        ),
    },
    "AAA_DIAGNOSTICS_VIEW": {
        "required": ("ISE_NODE", "TIMESTAMP"),
        "optional": ("MESSAGE_SEVERITY", "CATEGORY", "MESSAGE_CODE"),
    },
    "SYSTEM_DIAGNOSTICS_VIEW": {
        "required": ("ISE_NODE", "TIMESTAMP"),
        "optional": ("MESSAGE_SEVERITY", "CATEGORY", "MESSAGE_CODE"),
    },
    "RADIUS_ACCOUNTING": {
        "required": ("TIMESTAMP",),
        "optional": (
            "ACCT_STATUS_TYPE", "ACCT_SESSION_TIME", "DEVICE_NAME", "ISE_NODE",
            "AUTHORIZATION_POLICY",
        ),
    },
    "RADIUS_AUTHENTICATION_SUMMARY": {
        "required": ("TIMESTAMP", "PASSED_COUNT", "FAILED_COUNT"),
        "optional": (
            "CALLING_STATION_ID", "FAILURE_REASON", "AUTHORIZATION_PROFILES",
            "LOCATION", "IDENTITY_STORE", "IDENTITY_GROUP", "DEVICE_TYPE",
            "SECURITY_GROUP",
        ),
    },
    "RADIUS_AUTHENTICATIONS": {
        "required": ("TIMESTAMP",),
        "optional": (
            "FAILED", "DEVICE_NAME", "AUTHENTICATION_METHOD",
            "AUTHENTICATION_PROTOCOL", "AUTHORIZATION_RULE", "ISE_NODE",
            "RESPONSE_TIME",
        ),
    },
    "RADIUS_ERRORS_VIEW": {
        # radius_errors has no schema-conditional degradation path, so a rename
        # here is an ORA-00904 rather than a lost dimension. Contracted so the
        # gap is visible before the statement fails.
        "required": ("TIMESTAMP", "MESSAGE_CODE"),
        "optional": ("NETWORK_DEVICE_NAME", "AUTHENTICATION_METHOD", "ISE_NODE"),
    },
    "PROFILED_ENDPOINTS_SUMMARY": {
        "required": ("TIMESTAMP",),
        "optional": ("SOURCE", "ENDPOINT_ACTION_NAME"),
    },
    # ENDPOINTS_DATA is a current-state view: no TIMESTAMP and no ISE_NODE, and
    # three datasets read it. UPDATE_TIME is TIMESTAMP WITH TIME ZONE, which the
    # thin driver only returns through a CAST.
    "ENDPOINTS_DATA": {
        "required": ("MAC_ADDRESS", "ENDPOINT_POLICY"),
        "optional": ("IDENTITY_GROUP_ID", "POSTURE_APPLICABLE", "UPDATE_TIME"),
    },
    "POSTURE_ASSESSMENT_BY_ENDPOINT": {
        "required": ("TIMESTAMP", "ENDPOINT_MAC_ADDRESS"),
        "optional": (
            "ISE_NODE", "POSTURE_STATUS", "POSTURE_POLICY_MATCHED",
            "POSTURE_AGENT_VERSION", "ENDPOINT_OPERATING_SYSTEM",
        ),
    },
    # The two posture views disagree on spelling: this one keys on ENDPOINT_ID
    # and times on LOGGED_AT.
    "POSTURE_ASSESSMENT_BY_CONDITION": {
        "required": ("LOGGED_AT", "ENDPOINT_ID"),
        "optional": ("CONDITION_NAME", "CONDITION_STATUS"),
    },
    "TACACS_AUTHENTICATION_LAST_TWO_DAYS": {
        "required": ("EPOCH_TIME",),
        "optional": ("USERNAME", "DEVICE_NAME", "STATUS"),
    },
    "TACACS_ACCOUNTING_LAST_TWO_DAYS": {
        "required": ("EPOCH_TIME",),
        "optional": ("USERNAME", "DEVICE_NAME", "COMMAND", "COMMAND_ARGS"),
    },
    "TACACS_AUTHORIZATION_LAST_TWO_DAYS": {
        "required": ("EPOCH_TIME",),
        "optional": (
            "USERNAME", "DEVICE_NAME", "STATUS", "AUTHORIZATION_POLICY",
            "SHELL_PROFILE", "MATCHED_COMMAND_SET",
        ),
    },
}

_RETRYABLE_DISCONNECT = (
    "ORA-02399", "ORA-03113", "ORA-03114", "ORA-03135",
    "DPY-1001", "DPY-4010", "DPY-4011",
)
_AUTH_FAILURE = (
    "ORA-01005", "ORA-01017", "ORA-28000", "ORA-28001", "DPY-4001",
    "INVALID CREDENTIAL", "INVALID USERNAME/PASSWORD",
)
_AUTHORIZATION_FAILURE = ("ORA-00942", "ORA-01031")
_CONNECTION_FAILURE = ("ORA-12170", "ORA-12514", "ORA-12541")
# Thin-mode python-oracledb wraps every connect-path failure in DPY-6005 with
# the real cause appended, TLS handshake failures included, so it only means
# "host unreachable" once the certificate indicators have been ruled out.
_WRAPPED_CONNECT_FAILURE = ("DPY-6005",)


def publish_schema_contract(schema):
    """Expose bounded required/optional capability gaps from one catalogue."""
    schema = schema or {}
    for view, contract in SCHEMA_COLUMN_CONTRACTS.items():
        columns = schema.get(view, set())
        telemetry.dataconnect_schema_view_available.labels(view=view).set(
            int(view in schema))
        for requirement in ("required", "optional"):
            for column in contract[requirement]:
                telemetry.dataconnect_schema_column_available.labels(
                    view=view,
                    column=column,
                    requirement=requirement,
                ).set(int(column in columns))


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
    if "CERTIFICATE" in message or "SSL" in message or "TLS" in message:
        return "tls_failed"
    if any(code in message for code in _WRAPPED_CONNECT_FAILURE):
        return "connection_failed"
    if isinstance(error, TimeoutError) or "TIMEOUT" in message or "DPY-4011" in message:
        return "timeout"
    if _is_broken_connection(error):
        return "connection_failed"
    return "invalid_response"


def _is_broken_connection(error):
    """Recognise socket teardown even when python-oracledb leaks the OS error.

    Most dropped Oracle sessions arrive wrapped in a DPY/ORA code.  A peer that
    closes while the thin driver writes the next request can instead escape as
    a bare ``BrokenPipeError`` (or a neighbouring reset/abort ``OSError``).
    That is the same expired-session condition and is safe to reconnect once;
    classifying it as an invalid response both skipped the existing retry and
    made the dashboard blame the statement/view.
    """
    if isinstance(error, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
        return True
    return isinstance(error, OSError) and error.errno in {
        errno.EPIPE, errno.ECONNRESET, errno.ECONNABORTED,
    }


def _retryable_disconnect(error):
    return _is_broken_connection(error) or any(
        code in str(error).upper() for code in _RETRYABLE_DISCONNECT)


# The four character types the reporting views project. Thin-mode
# python-oracledb decodes every one of them as UTF-8 with no way to configure
# the codec -- NLS is ignored and the `encoding` parameter is desupported -- so
# the only lever left is the per-column one below.
_CHARACTER_TYPES = frozenset({
    oracledb.DB_TYPE_VARCHAR, oracledb.DB_TYPE_NVARCHAR,
    oracledb.DB_TYPE_CHAR, oracledb.DB_TYPE_NCHAR,
})
REPLACEMENT_CHARACTER = "�"


def _lenient_characters(cursor, metadata):
    """Fetch character columns with undecodable bytes replaced, not fatal.

    Except the ones Cisco documents as binary. PROBE_DATA is a byte stream in a
    VARCHAR2, so replacing its undecodable bytes would destroy the value rather
    than repair it: every non-UTF-8 byte becomes U+FFFD and the original is
    unrecoverable, which is a field of tofu where profiling attributes were.
    Those bypass the decode entirely and arrive as bytes for probe_data to read.

    ISE stores what the network supplied it: a NAD description typed in cp1252,
    a username carried through a pass-through insert, a profiling attribute
    copied verbatim off the wire. Any of those can leave bytes in a VARCHAR2
    that the database's character set cannot describe, and the database returns
    them unchanged. Strict decoding then makes one bad byte in one row cost the
    entire result -- a whole view unreadable, and a dataset permanently down,
    because of a single endpoint.

    A monitoring read has to be able to report the other rows. The byte becomes
    U+FFFD and the field is counted, so the value is visibly damaged rather than
    quietly wrong, and the count says how much of the answer to distrust.
    """
    if metadata.type_code not in _CHARACTER_TYPES:
        # Every other type -- numbers, dates, LOBs -- keeps the driver's own
        # handling. Only character data has a decode step to relax.
        return None
    size = max(metadata.internal_size or 0, metadata.type_code.default_size)
    if str(metadata.name or "").upper() in probe_data.BINARY_COLUMNS:
        return cursor.var(
            metadata.type_code, size=size, arraysize=cursor.arraysize,
            bypass_decode=True)
    return cursor.var(
        metadata.type_code, size=size, arraysize=cursor.arraysize,
        encoding_errors="replace")


def _field(name, value, limits):
    """One column's value, decoded if the column is one that needs decoding.

    Only the documented binary columns take this path, and only they can: they
    are the ones fetched as bytes. Everything else is ordinary data and goes
    through _materialize unchanged, so this costs one set membership per field.
    """
    if str(name or "").upper() in probe_data.BINARY_COLUMNS:
        # Decoded rather than materialized: _materialize would base64 the bytes,
        # which is the right answer for an opaque blob and the wrong one for a
        # blob whose whole content is attributes somebody wants to read.
        return probe_data.decode(value, ceiling=limits.field_bytes)
    return _materialize(value, limits)


def _replaced_fields(row):
    """How many fields of one row carry a byte the database could not describe."""
    return sum(1 for value in row.values()
               if isinstance(value, str) and REPLACEMENT_CHARACTER in value)


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
        self._session_prepared = False
        self._connect_failures = 0
        self._blocked_until = 0.0
        self._next_query_at = 0.0
        # How far light cut-ins have pushed that deadline, and when. Drained at
        # real time, because the scheduler is meanwhile waiting at real time.
        self._light_debt = 0.0
        self._light_debt_at = 0.0
        # A deadline another process published that this query did not wait out
        # (the catalog path). Releasing the gate must never publish less.
        self._gate_floor = 0.0
        self._catalog_failures = 0
        self._schema_error = None
        self._schema_retry_at = 0.0
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
        self._session_prepared = False
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
            connection = self._logon()
            connection.call_timeout = self.timeout * 1000
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
        self._session_prepared = False
        self._connect_failures = 0
        self._blocked_until = 0.0
        return connection

    def _logon(self):
        """Bound the whole logon, not just the transport establishment.

        ``tcp_connect_timeout`` only covers getting the socket up; the Oracle
        negotiation and logon round trips after it have no deadline of their own,
        and this runs on the serialised lane while the pacing gate flock is held.
        """
        outcome = {}
        abandoned = []
        settled = threading.Event()
        guard = threading.Lock()

        def attempt():
            try:
                connection = oracledb.connect(
                    user=self.user, password=self.password, host=self.host,
                    port=self.port, service_name=self.service, protocol="tcps",
                    ssl_context=self._ssl_context(),
                    ssl_server_dn_match=self.verify,
                    tcp_connect_timeout=self.timeout)
            except BaseException as error:      # noqa: BLE001 - reported below
                outcome["error"] = error
                settled.set()
                return
            with guard:
                if abandoned:
                    # Nobody is waiting any more. A session left open here would
                    # accumulate on the appliance on every stalled attempt.
                    try:
                        connection.close()
                    except Exception:       # noqa: BLE001
                        pass
                    return
                outcome["connection"] = connection
            settled.set()

        # Daemon, not a pooled worker: a wedged logon must never join at exit.
        threading.Thread(target=attempt, name="oracle-logon", daemon=True).start()
        if not settled.wait(self.timeout):
            with guard:
                if not outcome:
                    abandoned.append(True)
                    raise TimeoutError(
                        f"the Data Connect logon exceeded {self.timeout}s")
        if "error" in outcome:
            raise outcome["error"]
        return outcome["connection"]

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

            # A non-adaptive query does not wait the shared cooldown out, so the
            # deadline it skipped has to survive both the lease it writes now and
            # the one written when it releases the gate.
            self._gate_floor = deadline if not adaptive else 0.0
            self._write_lease(descriptor, self._crash_lease(adaptive),
                              self._gate_floor)
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

    def pacing_wait_hint(self):
        """How long a statement issued now would wait, without taking the gate.

        An operator asking "may I query yet" must not be able to answer that by
        blocking a collection: this takes no flock, writes no lease, and never
        sleeps. It is a hint by construction -- another process can move the
        deadline between this read and the next statement -- which is why it is
        only ever used to refuse an ad-hoc query early, never to skip a wait.

        The two deadlines are on different clocks: ``_next_query_at`` is this
        process's monotonic cooldown, the gate file carries a wall-clock time
        another process published. Both are compared on their own clock.
        """
        remaining = self._next_query_at - time.monotonic()
        try:
            with open(os.path.abspath(os.path.expanduser(self.pacing_file)),
                      "rb") as handle:
                raw = handle.read(64).decode("ascii").strip()
            deadline = float(raw) if raw else 0.0
            if math.isfinite(deadline):
                remaining = max(remaining, deadline - time.time())
        except (OSError, ValueError, UnicodeDecodeError):
            # No gate yet, or a torn read of one being rewritten. Neither is
            # worth failing an operator's status page over; the real gate is
            # taken under a flock by whoever actually queries.
            pass
        return max(0.0, remaining)

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
    def _release_gate(cls, descriptor, cooldown, floor=0.0):
        if descriptor is None:
            return
        try:
            cls._write_lease(descriptor, cooldown, floor)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    # --- statements -------------------------------------------------------

    def _acquire_lane(self, *, forced=False, lane_timeout=None):
        """Take the lane lock without sleeping a cooldown inside it.

        The in-process cooldown used to be waited out while holding this lock,
        which made the lock busy for whole cooldowns at a time: at a production
        duty cycle the oracle lane spends most of its life asleep, so a forced
        statement -- whose entire point is to run during a cooldown -- could
        only ever see "busy". Waiting outside the lock and re-checking after
        taking it keeps the lock an execution lock rather than a schedule.
        """
        while True:
            if not forced:
                self._wait(self._next_query_at - time.monotonic())
            if not self._lock.acquire(timeout=(
                    lane_timeout if lane_timeout is not None else -1)):
                raise TransportError(
                    "busy",
                    f"a statement held the Data Connect lane for "
                    f"{lane_timeout:.0f}s")
            if forced or time.monotonic() >= self._next_query_at:
                return
            # Another statement ran while this one slept and pushed the
            # deadline. Go back to waiting rather than jumping the cooldown it
            # just charged.
            self._lock.release()

    def query(self, sql, parameters=None, *, adaptive=True):
        """Run one bounded read-only statement under the pacing gate."""
        self._acquire_lane()
        try:
            return self._query(sql, parameters, adaptive=adaptive)
        finally:
            self._lock.release()

    def light_debt_seconds(self):
        """How far light cut-ins have currently pushed the shared deadline.

        Drained at real time: a debt charged a minute ago has been paid by the
        minute the scheduler spent waiting it out, so this measures the cut-ins
        still standing between the scheduler and the lane rather than every one
        ever made.
        """
        return max(0.0, self._light_debt - (
            time.monotonic() - self._light_debt_at))

    def light_available(self):
        """Whether a lookup may still cut in, or has to queue like a scan.

        A hint by construction, exactly like ``pacing_wait_hint``: it takes no
        lock and the answer can change before the statement runs. It is only
        ever used to refuse a cut-in early, never to grant one that the
        accounting would not otherwise allow.
        """
        return self.light_debt_seconds() < LIGHT_DEBT_CEILING_SECONDS

    def query_light(self, sql, parameters=None, *, lane_timeout=None):
        """Run one bounded lookup now, and charge it in full.

        The lane and every guard still apply; what this skips is the *wait*. A
        keyed read of a current-state view costs the same on a loud appliance as
        a quiet one, so making it queue behind a scan's cooldown delays an
        answer that was never part of the load the cooldown exists to shape.

        The accounting is the difference from ``query_forced``. A forced
        statement charges only measured time, which erodes the duty cycle by
        design because an incident is worth that. This charges the full adaptive
        cooldown and *adds* it to the outstanding one rather than taking the
        later of the two, so the shared deadline moves out by exactly what the
        lookup spent. Over any window the appliance sees the same total Oracle
        time it would have; only the order of service changed.
        """
        self._acquire_lane(forced=True, lane_timeout=lane_timeout)
        try:
            return self._query(sql, parameters, adaptive=True, additive=True)
        finally:
            self._lock.release()

    def query_forced(self, sql, parameters=None, *, lane_timeout=None):
        """Run one statement without waiting out the duty-cycle cooldowns.

        An operator overriding the pacing is overriding the *waits*, nothing
        else: the statement keeps the flock serialisation, the timeout, the
        auth guard and every ceiling, and it still charges the next cooldown --
        non-adaptively, so the floor between statements holds. What it skips is
        the in-process cooldown and the deadline another process published,
        both of which exist to shape sustained load, not to bound one read.

        ``lane_timeout`` bounds how long to wait for a statement actually
        executing on the lane; forcing must not turn into an unbounded block on
        an HTTP thread behind live Oracle work.
        """
        self._acquire_lane(forced=True, lane_timeout=lane_timeout)
        try:
            return self._query(sql, parameters, adaptive=False)
        finally:
            self._lock.release()

    def _query(self, sql, parameters=None, *, adaptive=True, additive=False):
        view = view_of(sql)
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
                if additive:
                    # A statement that cut into an outstanding cooldown pays on
                    # top of it, not into it. max() would let a lookup hide
                    # inside a scan's cooldown and cost the appliance real time
                    # the duty cycle never accounted for; adding keeps the
                    # budget exact while still answering immediately.
                    now = time.monotonic()
                    # Charged to the light lane as well as to the deadline, so
                    # the next cut-in can see what the last one already owes.
                    self._light_debt = self.light_debt_seconds() + cooldown
                    self._light_debt_at = now
                    self._next_query_at = max(self._next_query_at, now) + cooldown
                else:
                    # max(), not assignment: a forced statement skipped the
                    # pending deadline rather than waiting it out, and
                    # overriding one wait must not also refund the cooldown the
                    # scheduler still owes.
                    self._next_query_at = max(
                        self._next_query_at, time.monotonic() + cooldown)
                telemetry.dataconnect_query_cooldown_seconds.labels(view=view).set(cooldown)
                floor, self._gate_floor = self._gate_floor, 0.0
                self._release_gate(gate, cooldown, floor)

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
                    self._prepare_session(connection, cursor, deadline)
                    self._apply_timeout(connection, deadline)
                    cursor.outputtypehandler = _lenient_characters
                    cursor.execute(sql, parameters or {})
                    columns = [column.name.lower() for column in cursor.description]
                    rows, retained, replaced = [], 0, 0
                    while True:
                        self._apply_timeout(connection, deadline)
                        batch = cursor.fetchmany(FETCH_BATCH_ROWS)
                        if not batch:
                            break
                        for raw in batch:
                            self._check_ceilings(len(rows), retained)
                            row = {name: _field(name, value, self.limits)
                                   for name, value in zip(columns, raw)}
                            retained += _size_of(row, self.limits)
                            replaced += _replaced_fields(row)
                            rows.append(row)
                if replaced:
                    telemetry.dataconnect_replaced_characters_total.labels(
                        view=view).inc(replaced)
                    logger.warning(
                        "%s returned %d field(s) holding bytes this database's "
                        "character set cannot describe; they were read with the "
                        "undecodable bytes replaced", view, replaced)
                if self._batch_active:
                    self._batch_rows += len(rows)
                    self._batch_bytes += retained
                telemetry.dataconnect_query_rows.labels(view=view).set(len(rows))
                return rows
            except TransportError:
                self.close()
                raise
            except UnicodeDecodeError as error:
                # Character columns are read leniently above, so reaching here
                # means a type that has no per-column decode setting -- a LOB, or
                # a LONG. Reconnecting cannot help: the bytes are what the
                # database holds. Say that, because the bare codec message sends
                # an operator looking for a fault in the exporter.
                self.close()
                raise TransportError(
                    "invalid_response",
                    f"{view} holds a value this database's character set cannot "
                    f"describe, so it could not be read ({error}); the bytes are "
                    "in ISE, not in this statement") from error
            except Exception as error:
                self.close()
                # ISE expires healthy sessions on a fixed lifetime, so one
                # reconnect inside the same paced statement avoids losing a whole
                # cadence to an idle period. Nothing else is ever retried.
                if attempt == 0 and _retryable_disconnect(error):
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

    def _prepare_session(self, connection, cursor, deadline):
        """Issue the session precondition once per connection.

        A bounded aggregate can still consume disproportionate cluster resources
        if Oracle parallelises the view scan behind it. This is monitoring, never
        a batch workload. It runs on the statement's own deadline rather than in
        ``connect``, so one attempt costs one connect period and one statement
        period -- which is what ``MAX_STATEMENT_TIMEOUT_PERIODS`` reserves.
        """
        if self._session_prepared:
            return
        self._apply_timeout(connection, deadline)
        cursor.execute("ALTER SESSION DISABLE PARALLEL QUERY", {})
        self._session_prepared = True

    @staticmethod
    def _apply_timeout(connection, deadline):
        """Bound every round trip to one total per-attempt budget."""
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            raise TimeoutError("statement exceeded its hard attempt timeout")
        connection.call_timeout = max(1, math.ceil(remaining * 1000))

    def query_many(self, statements, parameters=None, *, tolerant=False):
        """Run a small atomic set of statements under one duty-cycle lease.

        One dashboard update often needs several bounded statements. Charging
        each its own cooldown would put hours between them at a low duty cycle;
        charging the batch once, on combined Oracle time, keeps the long-run
        duty cycle honest while letting a snapshot actually complete.

        With ``tolerant`` a failed statement does not abort the statements
        behind it: the call returns ``(results, errors)`` and the caller
        decides what a partial answer means. A diagnostic dataset wants this
        -- the freshness probe going dark because one view is slow silences
        its verdict on the other eight, exactly when it is needed.
        """
        items = list(statements.items())
        if not items:
            return {}
        if len(items) > self.limits.batch_queries:
            raise ValueError(
                f"batch exceeds the {self.limits.batch_queries}-statement "
                "ceiling")

        self._acquire_lane()
        try:
            if self._batch_active:
                raise RuntimeError("nested Data Connect batches are not supported")

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
                results, errors = {}, {}
                given = parameters or {}
                for index, (name, sql) in enumerate(items):
                    if index:
                        self._wait(self.min_query_interval)
                        # Advance the crash lease before each later statement so
                        # a kill early in a batch cannot strand every reporting
                        # dataset behind a whole-batch worst case.
                        self._write_lease(
                            gate, self._crash_lease(elapsed=self._batch_duration))
                    if not tolerant:
                        results[name] = self._query(sql, given.get(name))
                        continue
                    try:
                        results[name] = self._query(sql, given.get(name))
                    except TransportError as error:
                        # The Oracle time it burned is already in the batch
                        # duration, so the shared cooldown still charges it.
                        errors[name] = error
                        logger.warning(
                            "Data Connect statement %s failed in a tolerant "
                            "batch: %s: %s", name, error.reason, error.detail)
                return (results, errors) if tolerant else results
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
        finally:
            self._lock.release()

    # --- schema capability ------------------------------------------------

    def discover_schema(self):
        """Read the view/column catalog once, on the same paced lane.

        This is what turns a provider's declared ``view:`` requirement into a
        real check. A missing view blocks only the datasets that depend on it;
        everything else keeps collecting, which is the difference between one
        absent view and a dead exporter.
        """
        rows = self.query_catalog(
            "SELECT table_name, column_name, data_type FROM user_tab_columns")
        schema, zoned = {}, {}
        for row in rows:
            table = str(row.get("table_name") or "").upper()
            column = str(row.get("column_name") or "").upper()
            if not (table and column):
                continue
            schema.setdefault(table, set()).add(column)
            # TIMESTAMP WITH TIME ZONE cannot be fetched raw by the thin driver
            # when it carries a named region (DPY-3022); the reader of this set
            # projects such columns through a CAST. LOCAL time zone converts on
            # fetch and needs no help. Typed here because the dictionary is the
            # only place the types exist, and this read was already paid for.
            dtype = str(row.get("data_type") or "").upper()
            if "WITH TIME ZONE" in dtype and "LOCAL" not in dtype:
                zoned.setdefault(table, set()).add(column)
        self._schema = schema
        self._schema_zoned = zoned
        publish_schema_contract(schema)
        logger.info("discovered %d Data Connect reporting views", len(schema))
        return schema

    @property
    def schema(self):
        return getattr(self, "_schema", None)

    def zoned_columns(self, view):
        """Discovered TIMESTAMP WITH TIME ZONE columns of one view."""
        return getattr(self, "_schema_zoned", {}).get(str(view).upper(), set())

    def prepare(self):
        if self.schema is not None:
            return
        if self._schema_error is not None and time.monotonic() < self._schema_retry_at:
            # Every dataset attempt calls prepare, so a dictionary scan that
            # keeps failing would otherwise be re-issued back to back. Refuse
            # from cache -- without touching Oracle -- until the cooldown the
            # failed read earned has elapsed.
            raise TransportError("schema_pending", self._schema_error.detail)
        try:
            self.discover_schema()
        except TransportError as error:
            self._schema_error = error
            self._schema_retry_at = self._next_query_at
            raise
        self._schema_error = None

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
        self._acquire_lane()
        try:
            self._catalog_active = True
            try:
                # The exemption is for one cheap successful compatibility check.
                # A dictionary scan that already failed once is charged the full
                # duty amplification like any other statement, so a discovery
                # that never succeeds cannot run unpaced forever.
                rows = self._query(
                    sql, parameters, adaptive=self._catalog_failures > 0)
            except Exception:
                self._catalog_failures += 1
                raise
            finally:
                self._catalog_active = False
            self._catalog_failures = 0
            return rows
        finally:
            self._lock.release()
