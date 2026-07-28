"""Ad-hoc navigation of the Data Connect reporting views.

Every other operator route answers from state the exporter already computed.
This one reaches Oracle, and that is the whole reason it lives here rather than
in a client: an operator who wants to look at last night's failed
authentications would otherwise write a second process, and a second process is
exactly what the pacing gate, the crash lease and the authentication guard
cannot protect against. Serving ad-hoc reads from inside the exporter means an
interactive statement is charged the same adaptive cooldown as a scheduled
collection -- so operator curiosity is self-limiting, and no new budget knob is
needed to make it so.

Three things bound what an ad-hoc statement can be:

- **Identifiers come from the catalog, values come from binds.** Every view,
  column and ordering in the generated SQL is checked for membership in the
  schema the transport discovered, and checked again for identifier shape. No
  operator input is ever interpolated into the statement text. ``build_query``
  is the only place SQL is assembled, so it is the only place that has to be
  right.
- **The window is a validated integer**, built by ``reporting.recent`` (or its
  epoch twin) exactly like a scheduled statement's, and clamped to
  ``limits.window_hours``. A view with no time column cannot be given one.
  ``last=all`` is the explicit opt-out: the row limit becomes the only bound,
  read newest-first so Oracle can stop at ``FETCH FIRST`` rather than sort the
  history, with the statement timeout and the byte ceilings as the backstop --
  and the duty cycle still charges whatever the scan really cost.
- **One statement at a time, and only when the lane is nearly free.** A
  non-blocking lock makes concurrent requests fail fast rather than queue, and a
  request that would sit through a long cooldown is refused with the wait, so
  the caller decides whether to wait rather than holding a connection open for
  minutes.

The curated ``VIEW_CATALOG`` is metadata *about* the views, not a substitute for
the catalog: its column lists are a preference for what to show first, resolved
against what the account can actually see. A column ISE stopped shipping simply
drops out of the projection instead of becoming an ORA-00904, and a filter may
name any column the appliance really has, whether or not this file mentions it.
"""
from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass
from types import MappingProxyType

from . import reporting, telemetry
from .config import ConfigError, parse_duration
from .transports import TransportError


# What an unqualified ``first`` asks for, and the most any request may ask for
# before the row ceiling has an opinion. Ad-hoc navigation is reading, not
# extraction: a thousand rows is already more than a terminal can show.
DEFAULT_ROWS = 100
MAX_ROWS = 1000

# How long an interactive request will sit in the shared cooldown before being
# told to come back. Long enough to absorb the hard five-second floor between
# statements, short enough that an HTTP client is not left holding a connection
# through a production duty cycle -- at 3% a two-second statement costs a minute.
MAX_PACING_WAIT_SECONDS = 15.0

# Bounds on the request itself, so a malformed or hostile query string cannot
# make a statement large before any of it is validated.
MAX_FILTERS = 20
MAX_VALUE_LENGTH = 512

# Every parameter the wire accepts. Named as a set so an unrecognised one is
# reported rather than ignored: a typo in a filter silently returning the whole
# window is worse than a refusal.
PARAMETERS = frozenset({
    "view", "last", "eq", "like", "cols", "order", "desc", "first", "explain",
    "force",
})

# Oracle's unquoted identifier grammar. The catalog is Oracle's own dictionary,
# so its contents are already legal names -- but an allowlist is only as good as
# what went into it, and this is the last check before a name reaches the
# statement text. Both must pass.
_IDENTIFIER = re.compile(r"[A-Z][A-Z0-9_$#]{0,127}$")


@dataclass(frozen=True)
class View:
    """Curated metadata for one reporting view."""

    name: str
    view: str
    description: str
    time_column: str = ""
    # "timestamp", "epoch", or "" for a current-state view. The TACACS two-day
    # views carry EPOCH_TIME, a NUMBER of seconds, so their window bound is
    # arithmetic rather than an interval.
    time_kind: str = ""
    # A preference, not a contract: resolved against the discovered catalog, and
    # empty means "show every column this account can see".
    default_columns: tuple = ()
    # TIMESTAMP WITH TIME ZONE columns, which the thin driver refuses to return
    # raw (DPY-3022, docs/DATASETS_FACTS 5.1). Projected through a CAST the way
    # source_freshness does, so they are readable rather than a 502.
    zoned_columns: tuple = ()
    # An event view is always window-bounded -- that bound is what keeps a scan
    # of an 80 GB history cheap. A current-state view sets this instead: its
    # time column says when a row last changed, so a window is a filter the
    # operator may ask for, never a default that would silently hide the rows
    # that have not changed lately.
    window_optional: bool = False


def _catalog(*views):
    return MappingProxyType({view.name: view for view in views})


# Column spellings come from SCHEMA_COLUMN_CONTRACTS, the reporting datasets and
# docs/DATASETS_FACTS -- never from Cisco's documentation or from guesswork. The
# two posture views really do disagree (ENDPOINT_ID/LOGGED_AT against
# ENDPOINT_MAC_ADDRESS/TIMESTAMP), KEY_PERFORMANCE_METRICS really has no
# TIMESTAMP, and ENDPOINTS_DATA really has no time column an event window could
# use. Each of those is a fact about ISE 3.3, verified on an appliance.
VIEW_CATALOG = _catalog(
    View(
        name="radius_authentications",
        view="RADIUS_AUTHENTICATIONS",
        description="one row per RADIUS authentication event",
        time_column="TIMESTAMP",
        time_kind="timestamp",
        default_columns=(
            "TIMESTAMP", "USERNAME", "CALLING_STATION_ID", "DEVICE_NAME",
            "ISE_NODE", "AUTHENTICATION_METHOD", "AUTHORIZATION_RULE",
            "FAILED", "RESPONSE_TIME",
        ),
    ),
    View(
        name="radius_authentications_week",
        view="RADIUS_AUTHENTICATIONS_WEEK",
        description="RADIUS authentication events over Cisco's week retention",
        time_column="TIMESTAMP",
        time_kind="timestamp",
    ),
    View(
        name="radius_authentication_summary",
        view="RADIUS_AUTHENTICATION_SUMMARY",
        description="pre-aggregated RADIUS pass/fail counts per context",
        time_column="TIMESTAMP",
        time_kind="timestamp",
        default_columns=(
            "TIMESTAMP", "USERNAME", "CALLING_STATION_ID", "DEVICE_NAME",
            "ISE_NODE", "PASSED_COUNT", "FAILED_COUNT", "FAILURE_REASON",
        ),
    ),
    View(
        name="radius_accounting",
        view="RADIUS_ACCOUNTING",
        description="one row per RADIUS accounting record, start and stop",
        time_column="TIMESTAMP",
        time_kind="timestamp",
        default_columns=(
            "TIMESTAMP", "USERNAME", "CALLING_STATION_ID", "DEVICE_NAME",
            "ISE_NODE", "ACCT_STATUS_TYPE", "ACCT_SESSION_TIME",
            "AUTHORIZATION_POLICY",
        ),
    ),
    View(
        name="radius_errors_view",
        view="RADIUS_ERRORS_VIEW",
        description="RADIUS error events by ISE message code and device",
        time_column="TIMESTAMP",
        time_kind="timestamp",
        # NETWORK_DEVICE_NAME here where every other view says DEVICE_NAME.
        default_columns=(
            "TIMESTAMP", "MESSAGE_CODE", "NETWORK_DEVICE_NAME",
            "AUTHENTICATION_METHOD", "ISE_NODE",
        ),
    ),
    View(
        name="tacacs_authentication_last_two_days",
        view="TACACS_AUTHENTICATION_LAST_TWO_DAYS",
        description="Device Admin authentications, two-day retention",
        time_column="EPOCH_TIME",
        time_kind="epoch",
        default_columns=("EPOCH_TIME", "USERNAME", "DEVICE_NAME", "STATUS"),
        zoned_columns=("GENERATED_TIME",),
    ),
    View(
        name="tacacs_authorization_last_two_days",
        view="TACACS_AUTHORIZATION_LAST_TWO_DAYS",
        description="Device Admin authorizations with profile and command set",
        time_column="EPOCH_TIME",
        time_kind="epoch",
        default_columns=(
            "EPOCH_TIME", "USERNAME", "DEVICE_NAME", "STATUS",
            "AUTHORIZATION_POLICY", "SHELL_PROFILE", "MATCHED_COMMAND_SET",
        ),
        zoned_columns=("GENERATED_TIME",),
    ),
    View(
        name="tacacs_accounting_last_two_days",
        view="TACACS_ACCOUNTING_LAST_TWO_DAYS",
        description="commands run through Device Admin, two-day retention",
        time_column="EPOCH_TIME",
        time_kind="epoch",
        default_columns=(
            "EPOCH_TIME", "USERNAME", "DEVICE_NAME", "COMMAND", "COMMAND_ARGS",
        ),
        zoned_columns=("GENERATED_TIME",),
    ),
    View(
        name="posture_assessment_by_endpoint",
        view="POSTURE_ASSESSMENT_BY_ENDPOINT",
        description="one row per posture assessment, keyed on endpoint MAC",
        time_column="TIMESTAMP",
        time_kind="timestamp",
        default_columns=(
            "TIMESTAMP", "ENDPOINT_MAC_ADDRESS", "POSTURE_STATUS",
            "POSTURE_POLICY_MATCHED", "ENDPOINT_OPERATING_SYSTEM",
            "POSTURE_AGENT_VERSION", "ISE_NODE",
        ),
    ),
    View(
        name="posture_assessment_by_condition",
        view="POSTURE_ASSESSMENT_BY_CONDITION",
        description="one row per posture condition evaluated on an endpoint",
        # This view times on LOGGED_AT and keys on ENDPOINT_ID; its sibling
        # spells both differently.
        time_column="LOGGED_AT",
        time_kind="timestamp",
        default_columns=(
            "LOGGED_AT", "ENDPOINT_ID", "CONDITION_NAME", "CONDITION_STATUS",
            "ENDPOINT_OS",
        ),
    ),
    View(
        name="profiled_endpoints_summary",
        view="PROFILED_ENDPOINTS_SUMMARY",
        description="profiling events and the probe that produced each one",
        time_column="TIMESTAMP",
        time_kind="timestamp",
        # No ISE_NODE on this view, unlike every other event view.
        default_columns=(
            "TIMESTAMP", "ENDPOINT_ID", "ENDPOINT_PROFILE", "SOURCE",
            "IDENTITY_GROUP", "ENDPOINT_ACTION_NAME",
        ),
    ),
    View(
        name="endpoints_data",
        view="ENDPOINTS_DATA",
        description="the endpoint database: one current row per known endpoint",
        time_column="UPDATE_TIME",
        time_kind="tstz",
        default_columns=(
            "MAC_ADDRESS", "ENDPOINT_POLICY", "IDENTITY_GROUP_ID",
            "POSTURE_APPLICABLE", "UPDATE_TIME",
        ),
        zoned_columns=("UPDATE_TIME", "CREATE_TIME", "REG_TIMESTAMP"),
        window_optional=True,
    ),
    View(
        name="system_summary",
        view="SYSTEM_SUMMARY",
        description="per-node CPU, memory and disk utilisation samples",
        time_column="TIMESTAMP",
        time_kind="timestamp",
        default_columns=(
            "TIMESTAMP", "ISE_NODE", "CPU_UTILIZATION", "MEMORY_UTILIZATION",
            "DISKSPACE_ROOT", "DISKSPACE_OPT",
        ),
    ),
    View(
        name="key_performance_metrics",
        view="KEY_PERFORMANCE_METRICS",
        description="hourly per-node RADIUS throughput, load and latency",
        # There is no TIMESTAMP column here; naming one is an ORA-00904.
        time_column="LOGGED_TIME",
        time_kind="timestamp",
        default_columns=(
            "LOGGED_TIME", "ISE_NODE", "RADIUS_REQUESTS_HR", "AVG_TPS",
            "AVG_LATENCY_PER_REQ", "AVG_LOAD", "MAX_LOAD", "NOISE_HR",
            "SUPPRESSION_HR", "LOGGED_TO_MNT_HR",
        ),
    ),
    View(
        name="aaa_diagnostics_view",
        view="AAA_DIAGNOSTICS_VIEW",
        description="AAA diagnostic messages by node, severity and category",
        time_column="TIMESTAMP",
        time_kind="timestamp",
        default_columns=(
            "TIMESTAMP", "ISE_NODE", "USERNAME", "MESSAGE_SEVERITY",
            "CATEGORY", "MESSAGE_CODE", "MESSAGE_TEXT",
        ),
    ),
    View(
        name="system_diagnostics_view",
        view="SYSTEM_DIAGNOSTICS_VIEW",
        description="system diagnostic messages by node, severity and category",
        time_column="TIMESTAMP",
        time_kind="timestamp",
        default_columns=(
            "TIMESTAMP", "ISE_NODE", "MESSAGE_SEVERITY", "CATEGORY",
            "MESSAGE_CODE", "MESSAGE_TEXT",
        ),
    ),
)


class ExploreError(Exception):
    """A refusal carrying the status and body the operator API will send."""

    def __init__(self, error, detail="", status=400, retry_after=None):
        super().__init__(detail or error)
        self.error = str(error)
        self.detail = str(detail or "")
        self.status = int(status)
        self.retry_after = retry_after

    def payload(self):
        body = {"error": self.error}
        if self.detail:
            body["detail"] = self.detail
        if self.retry_after is not None:
            body["retry_after_seconds"] = self.retry_after
        return body


@dataclass(frozen=True)
class Request:
    """One validated ad-hoc request, before any catalog is consulted."""

    entry: View
    hours: int = 0
    window_requested: bool = False
    # last=all: the operator explicitly traded the time bound for the row
    # bound. Distinct from an omitted last, which keeps an event view's
    # default window.
    window_disabled: bool = False
    equals: tuple = ()
    matches: tuple = ()
    columns: tuple = ()
    order: str = ""
    # None means "the default for whichever column ends up ordering this".
    descending: bool | None = None
    first: int = DEFAULT_ROWS
    explain: bool = False
    force: bool = False


# --- request parsing --------------------------------------------------------

def _echo(value, width=64):
    """Quote operator input for an error message without carrying all of it."""
    text = str(value)
    return repr(text if len(text) <= width else text[:width] + "...")


def _invalid(detail):
    return ExploreError("invalid_request", detail, status=400)


def _single(query, name):
    values = query.get(name) or []
    if len(values) > 1:
        raise _invalid(f"{name} was given {len(values)} times; it takes one value")
    return values[0] if values else None


def _flag(query, name):
    raw = _single(query, name)
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if text in ("1", "true", "yes"):
        return True
    if text in ("0", "false", "no"):
        return False
    raise _invalid(f"{name} must be 1 or 0, got {_echo(raw)}")


def _pairs(query, name):
    """Split repeatable ``COLUMN:value`` filters on their first colon."""
    pairs = []
    for raw in query.get(name) or []:
        column, separator, value = str(raw).partition(":")
        if not separator:
            raise _invalid(f"{name} must be COLUMN:value, got {_echo(raw)}")
        column = column.strip()
        if not column:
            raise _invalid(f"{name} is missing a column name: {_echo(raw)}")
        if not value:
            # Oracle reads the empty string as NULL, so this would silently
            # match nothing rather than the rows the operator meant.
            raise _invalid(f"{name} on {_echo(column)} has no value to match")
        if len(value) > MAX_VALUE_LENGTH:
            raise _invalid(
                f"{name} value on {_echo(column)} exceeds "
                f"{MAX_VALUE_LENGTH} characters")
        pairs.append((column, value))
    return pairs


def parse_request(query, limits):
    """Validate a parsed query string into a request. Touches nothing live."""
    query = {name: list(values) for name, values in (query or {}).items()}
    unknown = sorted(set(query) - PARAMETERS)
    if unknown:
        raise _invalid(
            f"unknown parameter{'s' if len(unknown) != 1 else ''}: "
            f"{', '.join(unknown)}; known: {', '.join(sorted(PARAMETERS))}")

    name = _single(query, "view")
    if not name:
        raise _invalid("view is required; see /api/v1/dataconnect/views")
    entry = VIEW_CATALOG.get(str(name).strip().lower())
    if entry is None:
        raise ExploreError(
            "unknown_view",
            f"{_echo(name)} is not a curated view; see "
            f"/api/v1/dataconnect/views", status=404)

    hours = 0
    window_disabled = False
    last = _single(query, "last")
    if last is not None and str(last).strip().lower() == "all":
        window_disabled = True
        last = None
    elif last is not None:
        try:
            seconds = parse_duration(last, key="last")
        except ConfigError as error:
            raise _invalid(str(error)) from error
        # Rounded up, so `last=30m` scans the hour that contains the half hour
        # rather than nothing: the window bound is whole hours by construction.
        hours = max(1, min(limits.window_hours, math.ceil(seconds / 3600)))

    equals = _pairs(query, "eq")
    matches = _pairs(query, "like")
    if len(equals) + len(matches) > MAX_FILTERS:
        raise _invalid(f"a query may carry at most {MAX_FILTERS} filters")

    columns = []
    raw_columns = _single(query, "cols")
    if raw_columns is not None:
        for part in str(raw_columns).split(","):
            part = part.strip()
            # Deduplicated rather than refused: a repeated column is a typo
            # with an obvious intent, and dedup is also what bounds the width
            # of a projection built from operator input.
            if part and part.upper() not in [column.upper() for column in columns]:
                columns.append(part)
        if not columns:
            raise _invalid("cols was given but names no columns")

    first = DEFAULT_ROWS
    raw_first = _single(query, "first")
    if raw_first is not None:
        try:
            first = int(str(raw_first).strip())
        except ValueError as error:
            raise _invalid(
                f"first must be a whole number, got {_echo(raw_first)}") from error
        first = max(1, min(row_ceiling(limits), first))

    return Request(
        entry=entry,
        hours=hours,
        window_requested=last is not None,
        window_disabled=window_disabled,
        equals=tuple(equals),
        matches=tuple(matches),
        columns=tuple(columns),
        order=(_single(query, "order") or "").strip(),
        descending=_flag(query, "desc"),
        first=first,
        explain=bool(_flag(query, "explain")),
        force=bool(_flag(query, "force")),
    )


def row_ceiling(limits):
    """The most rows one ad-hoc statement may ask for.

    One below ``limits.result_rows`` because the transport refuses at equality:
    a request that filled its own limit must come back as a truncated answer,
    not as response_too_large after paying for the scan.
    """
    return max(1, min(MAX_ROWS, limits.result_rows - 1))


# --- statement building -----------------------------------------------------

def known_columns(catalog_columns):
    """The subset of a discovered column set that may appear in a statement."""
    return frozenset(
        name for name in (str(column).upper() for column in catalog_columns or ())
        if _IDENTIFIER.match(name))


def resolve_time_column(entry, columns):
    """The curated time column, only if this account can really see it.

    A declared column the catalog does not carry is reported as no time column
    at all, so a release that renamed one refuses ``last`` with a readable error
    instead of failing every statement with ORA-00904.
    """
    if entry.time_column and entry.time_column in columns:
        return entry.time_column
    return ""


def default_projection(entry, columns):
    """Curated columns this account can see, in the curated order."""
    return tuple(column for column in entry.default_columns if column in columns)


def full_projection(entry, columns):
    """Every column the account can see, curated-first.

    The curated list orders the leading fields the way a table reads; it must
    never narrow the data. Serving only the curated subset by default read as
    "missing data" against a live catalogue of thirty-column views: a row is
    the unit of navigation, and trimming it is display's job -- which the
    shell's format views already do client-side. cols= remains the way to ask
    for less on purpose.
    """
    preferred = [column for column in entry.default_columns if column in columns]
    chosen = set(preferred)
    return tuple(preferred + sorted(
        column for column in columns if column not in chosen))


def _projected(column, entry):
    if column in entry.zoned_columns:
        return f"CAST({column} AS DATE) AS {column}"
    return column


def _validate(name, columns, entry, what):
    upper = str(name).strip().upper()
    if not _IDENTIFIER.match(upper) or upper not in columns:
        raise _invalid(f"{entry.view} has no column {_echo(name)} to {what}")
    return upper


def _like_pattern(value):
    """Translate PowerShell wildcards, keeping everything else literal.

    ``*`` and ``?`` are the wildcards the caller typed; ``%`` and ``_`` are
    wildcards only to Oracle, so a username like ``svc_backup`` has to be
    escaped or it quietly matches ``svcXbackup`` too.
    """
    out = []
    for character in str(value):
        if character in ("%", "_", "\\"):
            out.append("\\" + character)
        elif character == "*":
            out.append("%")
        elif character == "?":
            out.append("_")
        else:
            out.append(character)
    return "".join(out)


def build_query(request, catalog_columns, limits):
    """Assemble one bounded statement. The only place SQL is built.

    Identifiers are members of the discovered catalog and nothing else; every
    value the caller supplied leaves as a bind. There is no path from a query
    string into the statement text, which is what makes the injection surface
    reviewable in one function.
    """
    entry = request.entry
    columns = known_columns(catalog_columns)
    if not columns:
        raise ExploreError(
            "view_unavailable",
            f"this Data Connect account cannot see {entry.view}", status=409)

    time_column = resolve_time_column(entry, columns)
    if request.window_requested and not time_column:
        raise _invalid(
            f"{entry.view} has no event time column, so last cannot bound it")

    windowed = bool(time_column) and not request.window_disabled and (
        request.window_requested or not entry.window_optional)
    binds, where = {}, []
    if windowed:
        hours = request.hours or limits.window_hours
        where.append(window_bound(time_column, hours, limits, entry.time_kind))
    for column, value in request.equals:
        name = _validate(column, columns, entry, "filter on")
        key = f"b{len(binds)}"
        binds[key] = value
        where.append(f"{name} = :{key}")
    for column, value in request.matches:
        name = _validate(column, columns, entry, "match on")
        key = f"b{len(binds)}"
        binds[key] = _like_pattern(value)
        where.append(f"UPPER({name}) LIKE UPPER(:{key}) ESCAPE '\\'")

    selected = tuple(
        _validate(column, columns, entry, "select") for column in request.columns)
    if not selected:
        # The whole row, named explicitly rather than SELECT *: the zoned
        # columns still get their CAST and the statement says what it fetched.
        selected = full_projection(entry, columns)

    # An unwindowed current-state browse keeps its stable key order. Everything
    # else that has a time column reads newest-first -- a window is "what
    # changed lately", and last=all is "the newest N rows": ordering by time is
    # also what lets Oracle stop at FETCH FIRST instead of sorting the history.
    order = (_validate(request.order, columns, entry, "order by")
             if request.order
             else (selected[0] if not time_column
                   or (entry.window_optional and not windowed)
                   else time_column))
    descending = (request.descending if request.descending is not None
                  else order == time_column)
    binds["limit"] = request.first

    sql = (
        f"SELECT {', '.join(_projected(column, entry) for column in selected)} "
        f"FROM {entry.view}"
        + (f" WHERE {' AND '.join(where)}" if where else "")
        + f" ORDER BY {order} {'DESC' if descending else 'ASC'}"
        " FETCH FIRST :limit ROWS ONLY"
    )
    return sql, binds


def window_bound(column, hours, limits, kind):
    """The window bound in whichever form this view's time column takes."""
    if kind == "epoch":
        return reporting.recent_epoch(column, hours, limits)
    if kind == "tstz":
        return reporting.recent_zoned(column, hours, limits)
    return reporting.recent(column, hours, limits)


# --- view descriptors -------------------------------------------------------

def describe(entry, catalog_columns):
    """One view descriptor, told from what the account can actually see."""
    columns = known_columns(catalog_columns)
    time_column = resolve_time_column(entry, columns)
    projection = default_projection(entry, columns)
    return {
        "name": entry.name,
        "view": entry.view,
        "description": entry.description,
        "time_column": time_column or None,
        "time_kind": entry.time_kind if time_column else None,
        # True means last= is a filter this view accepts, not a bound it always
        # gets: a current-state view unwindowed shows every row it has.
        "window_optional": entry.window_optional if time_column else None,
        "default_columns": list(projection) or None,
        "columns": sorted(columns),
        "available": bool(columns),
    }


def unconfigured_status():
    """The status page for a deployment with no Data Connect target.

    Answered rather than refused: "is Data Connect configured at all" is the
    first question this route exists to settle, and a 503 answers it less
    clearly than the field does.
    """
    return {
        "configured": False,
        "schema_discovered": False,
        "views_total": len(VIEW_CATALOG),
        "views_available": 0,
        "duty_cycle_percent": 0.0,
        "cooldown_remaining_seconds": 0.0,
        "busy": False,
        "last_query": None,
    }


def unconfigured_error():
    return ExploreError(
        "dataconnect_unconfigured",
        "no Data Connect (oracle) target is configured, so there is nothing to "
        "query", status=503)


# --- the service ------------------------------------------------------------

class Explorer:
    """Runs ad-hoc statements on the collection's own paced lane."""

    def __init__(self, transport, limits=None, *, clock=time.time,
                 max_wait=MAX_PACING_WAIT_SECONDS):
        self.transport = transport
        self.limits = limits if limits is not None else transport.limits
        self.clock = clock
        self.max_wait = float(max_wait)
        # Non-blocking, and deliberately not a queue: an operator who has to
        # wait wants to be told so, and a queue of interactive statements is a
        # way to spend the whole duty cycle on impatience.
        self._lock = threading.Lock()
        self._last = None

    # --- routes -----------------------------------------------------------

    def views(self):
        schema = self._schema()
        return [describe(entry, schema.get(entry.view, ()))
                for entry in VIEW_CATALOG.values()]

    def query(self, parameters):
        try:
            payload = self._query(parameters)
        except ExploreError as error:
            telemetry.dataconnect_explorer_queries_total.labels(
                result=error.error).inc()
            raise
        telemetry.dataconnect_explorer_queries_total.labels(
            result="explain" if payload["rows"] is None
            else "forced" if payload["forced"] else "success").inc()
        return payload

    def status(self):
        schema = self.transport.schema
        available = 0 if schema is None else sum(
            1 for entry in VIEW_CATALOG.values() if schema.get(entry.view))
        return {
            "configured": True,
            "schema_discovered": schema is not None,
            "views_total": len(VIEW_CATALOG),
            "views_available": available,
            "duty_cycle_percent": float(self.transport.duty_cycle),
            "cooldown_remaining_seconds": round(self._pacing_wait(), 1),
            "busy": self._lock.locked(),
            "last_query": self._last_query(),
        }

    # --- internals --------------------------------------------------------

    def _schema(self):
        """The discovered catalog, never a discovery.

        Discovery is a paced statement and belongs to the scheduler; running it
        from an operator request would let a page refresh take the lane before
        the datasets that need it.
        """
        schema = self.transport.schema
        if schema is None:
            raise ExploreError(
                "schema_pending",
                "Data Connect schema discovery has not completed yet",
                status=503)
        return schema

    def _pacing_wait(self):
        return max(0.0, float(self.transport.pacing_wait_hint()))

    def _query(self, parameters):
        request = parse_request(parameters, self.limits)
        schema = self._schema()
        sql, binds = build_query(
            request, schema.get(request.entry.view, ()), self.limits)
        if request.explain:
            # Explain is the one answer that costs nothing, so it stays
            # available while the lane is busy or cooling down -- reading the
            # statement is how an operator decides whether to spend the budget.
            return self._answer(request, sql, binds)

        if not self._lock.acquire(blocking=False):
            raise ExploreError(
                "busy", "another ad-hoc query is already running; Data Connect "
                "runs one statement at a time", status=429)
        try:
            if not request.force:
                wait = self._pacing_wait()
                if wait > self.max_wait:
                    raise ExploreError(
                        "cooldown",
                        f"Data Connect is {wait:.0f}s into its duty-cycle "
                        "cooldown", status=503, retry_after=round(wait, 1))
            started = time.monotonic()
            try:
                if request.force:
                    # The override skips the cooldown waits and the adaptive
                    # charge, nothing else: ceilings, timeout, auth guard and
                    # the lane serialisation all still apply, and the lane wait
                    # is bounded so forcing cannot hang an HTTP thread behind
                    # a sleeping scheduled statement.
                    rows = self.transport.query_forced(
                        sql, binds, lane_timeout=self.max_wait)
                else:
                    rows = self.transport.query(sql, binds)
            except TransportError as error:
                self._record(request, 0, time.monotonic() - started, error.reason)
                raise ExploreError(
                    error.reason, error.detail,
                    status=429 if error.reason == "busy" else 502) from error
            elapsed = time.monotonic() - started
            self._record(request, len(rows), elapsed,
                         "forced" if request.force else "success")
            return self._answer(
                request, sql, binds, rows=rows, elapsed=elapsed,
                cooldown=self._pacing_wait())
        finally:
            self._lock.release()

    def _answer(self, request, sql, binds, *, rows=None, elapsed=None,
                cooldown=None):
        return {
            "view": request.entry.name,
            "sql": sql,
            "binds": dict(binds),
            "rows": rows,
            "row_count": None if rows is None else len(rows),
            # A full page is the only truncation an ad-hoc statement can show:
            # every ceiling above it refuses the whole result rather than
            # trimming it, so `row_count == first` means rows were left behind.
            "truncated": None if rows is None else len(rows) == request.first,
            "elapsed_seconds": None if elapsed is None else round(elapsed, 3),
            "cooldown_seconds": None if cooldown is None else round(cooldown, 1),
            "forced": None if rows is None else request.force,
        }

    def _record(self, request, rows, elapsed, result):
        self._last = {
            "view": request.entry.name,
            "rows": rows,
            "elapsed_seconds": round(elapsed, 3),
            "at": self.clock(),
            "result": result,
        }

    def _last_query(self):
        if self._last is None:
            return None
        last = dict(self._last)
        last["at_age_seconds"] = round(max(0.0, self.clock() - last.pop("at")), 1)
        return last
