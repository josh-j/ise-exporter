"""Ad-hoc Data Connect navigation: the only operator surface that spends budget.

Two properties carry this module. The first is that ``build_query`` is the
whole injection surface -- every identifier is a member of the discovered
catalog, every value leaves as a bind -- so the hostile cases are pushed
through *every* string parameter rather than through the one that looks most
dangerous. The second is that an interactive statement is charged exactly like
a scheduled one: single flight, the shared cooldown honoured, and a refusal
that names the wait rather than holding the lane.
"""
import threading
import time

import pytest
from prometheus_client import REGISTRY

from ise_exporter3 import explore, limits as limits_module
from ise_exporter3.api import OperatorApi
from ise_exporter3.config import Config
from ise_exporter3.explore import (
    VIEW_CATALOG,
    ExploreError,
    Explorer,
    build_query,
    parse_request,
)
from ise_exporter3.model import Scale
from ise_exporter3.plan import build_plan
from ise_exporter3.transports import TransportError
from ise_exporter3.transports.dataconnect import _VIEWS, view_of


LIMITS = limits_module.for_scale(
    Scale(nads=5_000, endpoints=100_000, sessions=20_000, accounts=1_000))

RADIUS_COLUMNS = {
    "TIMESTAMP", "USERNAME", "CALLING_STATION_ID", "DEVICE_NAME", "ISE_NODE",
    "AUTHENTICATION_METHOD", "AUTHORIZATION_RULE", "FAILED", "RESPONSE_TIME",
}
ENDPOINT_COLUMNS = {
    "MAC_ADDRESS", "ENDPOINT_POLICY", "IDENTITY_GROUP_ID", "POSTURE_APPLICABLE",
    "UPDATE_TIME",
}
TACACS_COLUMNS = {"EPOCH_TIME", "USERNAME", "DEVICE_NAME", "STATUS"}

SCHEMA = {
    "RADIUS_AUTHENTICATIONS": set(RADIUS_COLUMNS),
    "ENDPOINTS_DATA": set(ENDPOINT_COLUMNS),
    "TACACS_AUTHENTICATION_LAST_TWO_DAYS": set(TACACS_COLUMNS),
}

# Every value a caller can push through the wire, in the shapes that would end
# a statement early and start another one if any of them were interpolated.
INJECTIONS = (
    "TIMESTAMP; DROP TABLE RADIUS_AUTHENTICATIONS",
    "TIMESTAMP--",
    "TIMESTAMP/*",
    "1=1 OR TIMESTAMP IS NOT NULL",
    "TIMESTAMP,(SELECT password FROM dba_users)",
    "TIMESTAMP FROM dual UNION SELECT 1 FROM user_views",
    "'||(SELECT banner FROM v$version)||'",
    "TIMESTAMP)",
    "DBMS_LOCK.SLEEP(60)",
    "..\\..\\etc",
    'TIMESTAMP"',
)


def _query(**parameters):
    """A parse_qs-shaped dict: every value is a list, repeatable or not."""
    query = {}
    for name, value in parameters.items():
        query[name] = [str(item) for item in value] if isinstance(
            value, (list, tuple)) else [str(value)]
    return query


def _build(schema=None, **parameters):
    schema = SCHEMA if schema is None else schema
    request = parse_request(_query(**parameters), LIMITS)
    return build_query(
        request, schema.get(request.entry.view, ()), LIMITS)


_UNSET = object()


class StubTransport:
    """A Data Connect transport with the surface the explorer actually reads."""

    target = "oracle"
    duty_cycle = 3.0
    limits = LIMITS

    def __init__(self, schema=_UNSET, rows=None, error=None, wait=0.0,
                 cooldown=None, hold=None):
        self.schema = SCHEMA if schema is _UNSET else schema
        self.rows = rows if rows is not None else [{"timestamp": "t"}]
        self.error = error
        self.wait = wait
        # What the gate says once a statement has been charged for, which is
        # never what it said before one.
        self.cooldown = wait if cooldown is None else cooldown
        self.hold = hold
        self.calls = []
        self.forced_calls = []

    def query(self, sql, parameters=None, *, adaptive=True):
        self.calls.append((sql, parameters, adaptive))
        if self.hold is not None:
            self.hold.wait(5)
        if self.error is not None:
            raise self.error
        return [dict(row) for row in self.rows]

    def query_forced(self, sql, parameters=None, *, lane_timeout=None):
        self.forced_calls.append((sql, parameters, lane_timeout))
        if self.error is not None:
            raise self.error
        return [dict(row) for row in self.rows]

    def pacing_wait_hint(self):
        return self.cooldown if self.calls else self.wait


def _explorer(**kwargs):
    return Explorer(StubTransport(**kwargs))


def _counted(result):
    return REGISTRY.get_sample_value(
        "ise3_dataconnect_explorer_queries_total", {"result": result}) or 0.0


# --- the curated catalog ----------------------------------------------------

def test_every_view_the_transport_knows_about_is_navigable():
    # The transport's _VIEWS is what the pacing telemetry labels statements
    # with; a curated entry missing from it, or missing here, means a view an
    # operator can reach without the metrics naming it.
    assert set(VIEW_CATALOG) == set(_VIEWS)
    for name, entry in VIEW_CATALOG.items():
        assert entry.view == name.upper()
        assert entry.description


def test_the_curated_metadata_is_the_shape_the_wire_promises():
    for entry in VIEW_CATALOG.values():
        assert entry.time_kind in ("timestamp", "epoch", "tstz", "")
        assert bool(entry.time_column) == bool(entry.time_kind)
        for column in entry.default_columns + entry.zoned_columns:
            assert explore._IDENTIFIER.match(column), column
        if entry.default_columns and entry.time_column:
            # The default projection has to carry the column the default
            # ordering uses, or the first page reads as unordered.
            assert entry.time_column in entry.default_columns


def test_the_views_that_do_not_time_on_timestamp_say_so():
    # Each of these cost a live appliance to establish (DATASETS_FACTS 5.3);
    # an invented time column is an ORA-00904 on every statement.
    assert VIEW_CATALOG["key_performance_metrics"].time_column == "LOGGED_TIME"
    assert VIEW_CATALOG["posture_assessment_by_condition"].time_column == "LOGGED_AT"
    # Current state, not events: its zoned change time is an optional filter,
    # never the always-on bound the event views get.
    assert VIEW_CATALOG["endpoints_data"].time_column == "UPDATE_TIME"
    assert VIEW_CATALOG["endpoints_data"].time_kind == "tstz"
    assert VIEW_CATALOG["endpoints_data"].window_optional is True
    assert all(not entry.window_optional
               for name, entry in VIEW_CATALOG.items() if name != "endpoints_data")
    for name in ("tacacs_authentication_last_two_days",
                 "tacacs_authorization_last_two_days",
                 "tacacs_accounting_last_two_days"):
        assert VIEW_CATALOG[name].time_kind == "epoch"


# --- the statement builder --------------------------------------------------

def test_a_plain_request_is_a_bounded_windowed_select():
    sql, binds = _build(view="radius_authentications")
    assert sql.startswith("SELECT TIMESTAMP, USERNAME")
    assert "FROM RADIUS_AUTHENTICATIONS" in sql
    assert "TIMESTAMP >= CAST(SYSTIMESTAMP - NUMTODSINTERVAL(6, 'HOUR')" in sql
    assert sql.endswith("ORDER BY TIMESTAMP DESC FETCH FIRST :limit ROWS ONLY")
    assert binds == {"limit": 100}


def test_the_default_projection_is_the_curated_columns_the_account_can_see():
    narrow = {"RADIUS_AUTHENTICATIONS": {"TIMESTAMP", "DEVICE_NAME"}}
    sql, _binds = _build(schema=narrow, view="radius_authentications")
    # USERNAME is curated but absent from this catalogue, so it drops out
    # rather than becoming an ORA-00904.
    assert sql.startswith("SELECT TIMESTAMP, DEVICE_NAME FROM")


def test_a_view_with_no_curated_columns_projects_what_the_catalog_holds():
    # Explicitly, not SELECT *: the statement has to say what it fetched, and
    # the zoned columns need their CAST.
    schema = {"RADIUS_AUTHENTICATIONS_WEEK": {"TIMESTAMP", "USERNAME"}}
    sql, _binds = _build(schema=schema, view="radius_authentications_week")
    assert sql.startswith("SELECT TIMESTAMP, USERNAME FROM RADIUS_AUTHENTICATIONS_WEEK")


def test_a_timezone_column_is_cast_rather_than_selected_raw():
    # DPY-3022: the thin driver refuses TIMESTAMP WITH TIME ZONE outright, so
    # the endpoint database would be unreadable without this.
    sql, _binds = _build(view="endpoints_data")
    assert "CAST(UPDATE_TIME AS DATE) AS UPDATE_TIME" in sql
    assert ", UPDATE_TIME," not in sql


def test_a_current_state_view_is_only_windowed_on_request():
    # Unwindowed, the endpoint database shows every row it has, in stable key
    # order. An implicit window here would silently hide every endpoint that
    # has not changed lately, which is most of them.
    sql, _binds = _build(view="endpoints_data")
    assert "WHERE" not in sql
    assert "ORDER BY MAC_ADDRESS ASC" in sql

    # Asked for, the window bounds on UPDATE_TIME -- zoned on both sides, no
    # CAST, because stripping the zone would compare digits across offsets --
    # and the answer reads newest-first: a window is "what changed lately".
    sql, _binds = _build(view="endpoints_data", last="4h")
    assert ("WHERE UPDATE_TIME >= SYSTIMESTAMP - "
            "NUMTODSINTERVAL(4, 'HOUR')") in sql
    assert "ORDER BY UPDATE_TIME DESC" in sql


def test_a_zoned_column_the_dictionary_reports_is_cast_without_curation():
    # TIMESTAMP_TIMEZONE exists on most event views and is curated nowhere.
    # With the whole row as the default projection, the dictionary's own types
    # are what keep every default query from dying on DPY-3022.
    wide = set(RADIUS_COLUMNS) | {"TIMESTAMP_TIMEZONE"}
    request = parse_request(_query(view="radius_authentications"), LIMITS)
    sql, _binds = build_query(request, wide, LIMITS,
                              zoned={"TIMESTAMP_TIMEZONE"})
    assert "CAST(TIMESTAMP_TIMEZONE AS DATE) AS TIMESTAMP_TIMEZONE" in sql
    assert ", TIMESTAMP_TIMEZONE," not in sql


def test_the_explorer_passes_the_discovered_types_into_the_statement():
    transport = StubTransport(
        schema={"RADIUS_AUTHENTICATIONS":
                set(RADIUS_COLUMNS) | {"TIMESTAMP_TIMEZONE"}})
    transport.zoned_columns = lambda view: {"TIMESTAMP_TIMEZONE"}
    explorer = Explorer(transport)
    answer = explorer.query(_query(view="radius_authentications", explain=1))
    assert "CAST(TIMESTAMP_TIMEZONE AS DATE)" in answer["sql"]


def test_the_default_projection_is_the_whole_row_curated_first():
    # A live RADIUS_AUTHENTICATIONS carries ~30 columns; the curated list is a
    # reading order, not a filter. Serving only the curated subset by default
    # read as "missing data across all views" on a real appliance.
    wide = set(RADIUS_COLUMNS) | {"POLICY_SET_NAME", "NAS_PORT_TYPE", "AUDIT_ID"}
    schema = {"RADIUS_AUTHENTICATIONS": wide}
    sql, _binds = _build(schema=schema, view="radius_authentications")
    # Curated order leads...
    assert sql.startswith("SELECT TIMESTAMP, USERNAME, CALLING_STATION_ID")
    # ...and every remaining catalog column follows, alphabetically.
    for column in ("POLICY_SET_NAME", "NAS_PORT_TYPE", "AUDIT_ID"):
        assert column in sql
    select_list = sql.split(" FROM ")[0]
    assert select_list.count(",") == len(wide) - 1

    # Asking for less is still asking for less.
    sql, _binds = _build(schema=schema, view="radius_authentications",
                         cols="TIMESTAMP,USERNAME")
    assert sql.split(" FROM ")[0] == "SELECT TIMESTAMP, USERNAME"


def test_last_all_makes_the_row_limit_the_only_bound():
    # The explicit trade: no time bound, newest-first so Oracle stops at FETCH
    # FIRST rather than sorting the history, and the row limit does the
    # bounding. Distinct from omitting last, which keeps the default window.
    sql, binds = _build(view="radius_authentications", last="all", first=50)
    assert "WHERE" not in sql
    assert "ORDER BY TIMESTAMP DESC" in sql
    assert sql.endswith("FETCH FIRST :limit ROWS ONLY")
    assert binds["limit"] == 50

    # Filters still filter; only the window bound is gone.
    sql, binds = _build(view="radius_authentications", last="ALL",
                        eq="USERNAME:alice")
    assert "WHERE USERNAME = :b0" in sql
    assert "NUMTODSINTERVAL" not in sql


def test_last_all_on_a_current_state_view_is_the_plain_browse():
    sql, _binds = _build(view="endpoints_data", last="all")
    assert "WHERE" not in sql
    assert "ORDER BY MAC_ADDRESS ASC" in sql


def test_a_view_whose_catalog_lost_its_time_column_refuses_a_window():
    # The curated time column is a preference; the catalog is the fact. When a
    # release does not carry it, last= must refuse rather than build ORA-00904.
    schema = {"ENDPOINTS_DATA": {"MAC_ADDRESS", "ENDPOINT_POLICY"}}
    sql, _binds = _build(schema=schema, view="endpoints_data")
    assert "WHERE" not in sql
    with pytest.raises(ExploreError) as raised:
        _build(schema=schema, view="endpoints_data", last="1h")
    assert raised.value.error == "invalid_request"
    assert raised.value.status == 400


def test_an_epoch_view_bounds_its_window_arithmetically():
    sql, _binds = _build(view="tacacs_authentication_last_two_days", last="2h")
    assert "EPOCH_TIME >= (CAST(SYSTIMESTAMP AS DATE) - DATE '1970-01-01')" in sql
    assert "* 86400 - 7200" in sql


@pytest.mark.parametrize("last,expected", [
    ("30m", 1),         # rounded up: a half hour still scans the hour holding it
    ("1h", 1),
    ("2h", 2),
    ("1d", LIMITS.window_hours),        # clamped to the declared ceiling
    ("30d", LIMITS.window_hours),
    (3600, 1),
])
def test_the_window_is_whole_hours_clamped_to_the_declared_ceiling(last, expected):
    sql, _binds = _build(view="radius_authentications", last=last)
    assert f"NUMTODSINTERVAL({expected}, 'HOUR')" in sql


def test_an_unparseable_window_is_a_readable_refusal():
    with pytest.raises(ExploreError) as raised:
        _build(view="radius_authentications", last="yesterday")
    assert raised.value.error == "invalid_request"
    assert "last" in raised.value.detail


def test_curation_is_enrichment_and_the_catalog_is_the_gate():
    # A malformed name is a 404 before anything else looks at it: it could
    # reach FROM-clause position, so the identifier grammar is its own check.
    for name in ("no such view", "1nope", "x;drop", "nope)", ""):
        with pytest.raises(ExploreError) as raised:
            _build(view=name)
        assert raised.value.status in (400, 404)

    # A well-formed name nobody curated is not refused by name -- reachability
    # belongs to the discovered catalog. dba_users is the case that matters:
    # Oracle dictionary views are not in user_tab_columns for this account,
    # so the same gate that admits an uncurated reporting view keeps the
    # dictionary out, without a second mechanism.
    with pytest.raises(ExploreError) as raised:
        _build(view="dba_users")
    assert raised.value.error == "view_unavailable"
    assert raised.value.status == 409


def test_an_uncurated_view_in_the_catalog_is_fully_explorable():
    schema = {"NODE_LIST": {"HOSTNAME", "NODE_TYPE", "GATEWAY", "NODE_ROLE"}}
    sql, binds = _build(schema=schema, view="node_list",
                        eq="NODE_TYPE:PAN", first=10)
    assert "FROM NODE_LIST" in sql
    assert "NODE_TYPE = :b0" in sql and binds["b0"] == "PAN"
    # No curated preference: the whole row, alphabetically, stably ordered.
    assert sql.startswith("SELECT GATEWAY, HOSTNAME, NODE_ROLE, NODE_TYPE ")
    assert "ORDER BY GATEWAY ASC" in sql

    # No time column is known, so a window cannot be built -- but the
    # explicit no-window trade still works.
    with pytest.raises(ExploreError) as raised:
        _build(schema=schema, view="node_list", last="1h")
    assert raised.value.status == 400
    assert _build(schema=schema, view="node_list", last="all")[0]


def test_the_listing_carries_uncurated_views_marked_as_such():
    schema = {"ENDPOINTS_DATA": set(ENDPOINT_COLUMNS),
              "NODE_LIST": {"HOSTNAME", "NODE_TYPE"}}
    explorer = Explorer(StubTransport(schema=schema))
    views = {view["name"]: view for view in explorer.views()}
    assert len(views) == len(VIEW_CATALOG) + 1
    assert views["node_list"]["curated"] is False
    assert views["node_list"]["available"] is True
    assert views["node_list"]["columns"] == ["HOSTNAME", "NODE_TYPE"]
    assert views["node_list"]["time_column"] is None
    assert views["endpoints_data"]["curated"] is True


def test_a_view_name_is_case_insensitive():
    sql, _binds = _build(view="RADIUS_Authentications")
    assert "FROM RADIUS_AUTHENTICATIONS" in sql


def test_a_view_the_account_cannot_see_is_a_409_not_a_failed_statement():
    with pytest.raises(ExploreError) as raised:
        _build(schema={}, view="radius_authentications")
    assert raised.value.error == "view_unavailable"
    assert raised.value.status == 409


def test_values_reach_oracle_only_as_binds():
    sql, binds = _build(
        view="radius_authentications", eq=["USERNAME:alice", "ISE_NODE:psn1"],
        like="DEVICE_NAME:sw-*")
    assert "alice" not in sql and "psn1" not in sql and "sw-" not in sql
    assert binds == {"b0": "alice", "b1": "psn1", "b2": "sw-%", "limit": 100}
    assert "USERNAME = :b0" in sql and "ISE_NODE = :b1" in sql
    assert "UPPER(DEVICE_NAME) LIKE UPPER(:b2)" in sql


@pytest.mark.parametrize("pattern,expected", [
    ("sw-*", "sw-%"),
    ("sw-1?", "sw-1_"),
    ("*core*", "%core%"),
    # Oracle's own wildcards are literals to the caller, who typed a PowerShell
    # wildcard or nothing at all.
    ("svc_backup", "svc\\_backup"),
    ("100%", "100\\%"),
    ("a\\b", "a\\\\b"),
])
def test_powershell_wildcards_translate_and_oracle_wildcards_stay_literal(
        pattern, expected):
    sql, binds = _build(view="radius_authentications", like=f"USERNAME:{pattern}")
    assert binds["b0"] == expected
    assert "ESCAPE '\\'" in sql


def test_a_match_is_case_insensitive_on_both_sides():
    sql, _binds = _build(view="radius_authentications", like="USERNAME:Alice")
    assert "UPPER(USERNAME) LIKE UPPER(:b0)" in sql


def test_a_projection_is_validated_deduplicated_and_ordered_as_asked():
    sql, _binds = _build(
        view="radius_authentications", cols="username, TIMESTAMP,username")
    assert sql.startswith("SELECT USERNAME, TIMESTAMP FROM")


def test_ordering_defaults_to_newest_first_and_can_be_reversed():
    sql, _binds = _build(view="radius_authentications", order="USERNAME")
    # A non-time ordering ascends by default; time descends, because the first
    # question about an event view is always "what happened last".
    assert "ORDER BY USERNAME ASC" in sql
    sql, _binds = _build(view="radius_authentications", order="USERNAME", desc=1)
    assert "ORDER BY USERNAME DESC" in sql
    sql, _binds = _build(view="radius_authentications", desc=0)
    assert "ORDER BY TIMESTAMP ASC" in sql


@pytest.mark.parametrize("first,expected", [
    (1, 1), (250, 250), (0, 1), (-5, 1), (5000, explore.MAX_ROWS),
])
def test_the_row_limit_is_clamped_rather_than_refused(first, expected):
    _sql, binds = _build(view="radius_authentications", first=first)
    assert binds["limit"] == expected


def test_the_row_limit_never_reaches_the_ceiling_the_transport_refuses_at():
    # The transport refuses at equality with limits.result_rows, so a statement
    # allowed to ask for exactly that would pay for the scan and then be thrown
    # away as response_too_large.
    tiny = limits_module.for_scale(Scale(nads=1, accounts=1))
    assert explore.row_ceiling(tiny) < tiny.result_rows


def test_a_bad_row_limit_is_a_readable_refusal():
    with pytest.raises(ExploreError) as raised:
        _build(view="radius_authentications", first="lots")
    assert raised.value.error == "invalid_request"


# --- the injection surface --------------------------------------------------

@pytest.mark.parametrize("hostile", INJECTIONS)
@pytest.mark.parametrize("parameter", ["cols", "order"])
def test_an_identifier_parameter_refuses_anything_not_in_the_catalog(
        parameter, hostile):
    with pytest.raises(ExploreError) as raised:
        _build(view="radius_authentications", **{parameter: hostile})
    assert raised.value.error == "invalid_request"
    assert raised.value.status == 400


@pytest.mark.parametrize("hostile", INJECTIONS)
@pytest.mark.parametrize("parameter", ["eq", "like"])
def test_a_filter_column_refuses_anything_not_in_the_catalog(parameter, hostile):
    with pytest.raises(ExploreError) as raised:
        _build(view="radius_authentications", **{parameter: f"{hostile}:x"})
    assert raised.value.error == "invalid_request"


@pytest.mark.parametrize("hostile", INJECTIONS)
@pytest.mark.parametrize("parameter", ["eq", "like"])
def test_a_hostile_filter_value_leaves_as_a_bind_and_not_as_sql(
        parameter, hostile):
    benign, _binds = _build(
        view="radius_authentications", **{parameter: "USERNAME:alice"})
    sql, binds = _build(
        view="radius_authentications", **{parameter: f"USERNAME:{hostile}"})
    # Byte-identical: a value cannot influence the statement text at all, which
    # is a stronger claim than "the value does not appear in it".
    assert sql == benign
    assert binds["b0"] and sql.count(":b0") == 1


@pytest.mark.parametrize("hostile", INJECTIONS)
def test_a_hostile_view_name_is_an_unknown_view_not_a_statement(hostile):
    with pytest.raises(ExploreError) as raised:
        _build(view=hostile)
    assert raised.value.error == "unknown_view"


@pytest.mark.parametrize("hostile", ["1 OR 1=1", "6); DROP", "'6'"])
def test_a_hostile_window_never_reaches_the_statement(hostile):
    with pytest.raises(ExploreError) as raised:
        _build(view="radius_authentications", last=hostile)
    assert raised.value.error == "invalid_request"


def test_a_catalog_entry_that_is_not_an_identifier_is_never_usable():
    # The catalog is Oracle's own dictionary, so this cannot normally happen --
    # which is exactly why the allowlist checks the shape of what went into it
    # rather than trusting where it came from.
    schema = {"RADIUS_AUTHENTICATIONS": {"TIMESTAMP", "X FROM DUAL--"}}
    with pytest.raises(ExploreError):
        _build(schema=schema, view="radius_authentications",
               cols="X FROM DUAL--")


def test_an_unknown_parameter_is_named_rather_than_ignored():
    with pytest.raises(ExploreError) as raised:
        parse_request({"view": ["endpoints_data"], "fitler": ["x"]}, LIMITS)
    assert "fitler" in raised.value.detail


def test_a_parameter_that_takes_one_value_refuses_two():
    with pytest.raises(ExploreError) as raised:
        parse_request({"view": ["endpoints_data", "system_summary"]}, LIMITS)
    assert raised.value.error == "invalid_request"


def test_a_filter_needs_a_column_and_a_value():
    for bad in ("USERNAME", ":alice", "USERNAME:"):
        with pytest.raises(ExploreError) as raised:
            _build(view="radius_authentications", eq=bad)
        assert raised.value.error == "invalid_request"


def test_a_value_only_splits_on_its_first_colon():
    # MACs and timestamps carry colons; splitting on all of them would make the
    # most obvious filter in the tool unusable.
    _sql, binds = _build(
        view="radius_authentications", eq="CALLING_STATION_ID:B8:27:EB:83:21:0E")
    assert binds["b0"] == "B8:27:EB:83:21:0E"


def test_a_request_cannot_be_made_arbitrarily_large():
    filters = [f"USERNAME:u{index}" for index in range(explore.MAX_FILTERS + 1)]
    with pytest.raises(ExploreError):
        _build(view="radius_authentications", eq=filters)
    with pytest.raises(ExploreError):
        _build(view="radius_authentications",
               eq="USERNAME:" + "a" * (explore.MAX_VALUE_LENGTH + 1))


# --- the explorer service ---------------------------------------------------

def test_explain_returns_the_statement_without_touching_oracle():
    explorer = _explorer()
    answer = explorer.query(_query(view="radius_authentications", explain=1))
    assert answer["rows"] is None
    assert answer["row_count"] is None and answer["truncated"] is None
    assert answer["elapsed_seconds"] is None and answer["cooldown_seconds"] is None
    assert answer["sql"].startswith("SELECT")
    assert answer["binds"] == {"limit": 100}
    assert explorer.transport.calls == []


def test_explain_still_answers_while_the_lane_is_cooling_down():
    # Reading the statement is how an operator decides whether to spend the
    # budget, so the one answer that costs nothing stays available.
    explorer = _explorer(wait=600.0)
    assert explorer.query(_query(view="endpoints_data", explain=1))["sql"]


def test_a_query_returns_its_rows_its_cost_and_what_it_charged():
    explorer = _explorer(
        rows=[{"timestamp": "t", "username": "alice"}], cooldown=41.5)
    answer = explorer.query(_query(view="radius_authentications", first=2))
    assert answer["view"] == "radius_authentications"
    assert answer["rows"] == [{"timestamp": "t", "username": "alice"}]
    assert answer["row_count"] == 1
    assert answer["truncated"] is False
    assert answer["elapsed_seconds"] >= 0
    # What this statement just imposed on every reporting dataset, which is the
    # number an operator needs before running another one.
    assert answer["cooldown_seconds"] == 41.5


def test_a_full_page_reports_itself_as_truncated():
    explorer = _explorer(rows=[{"a": index} for index in range(3)])
    answer = explorer.query(_query(view="radius_authentications", first=3))
    assert answer["row_count"] == 3 and answer["truncated"] is True


def test_a_query_runs_on_the_adaptive_lane_like_any_collection():
    explorer = _explorer()
    explorer.query(_query(view="radius_authentications"))
    _sql, binds, adaptive = explorer.transport.calls[0]
    assert adaptive is True
    assert binds["limit"] == 100


def test_an_ad_hoc_statement_is_attributed_to_the_view_it_read():
    # The per-view cost families are how an unexplained cooldown is traced back
    # to what spent it, so an explorer statement must not land on "other".
    schema = {entry.view: {"TIMESTAMP", "EPOCH_TIME", "MAC_ADDRESS",
                           "LOGGED_TIME", "LOGGED_AT"}
              for entry in VIEW_CATALOG.values()}
    explorer = _explorer(schema=schema)
    for name in VIEW_CATALOG:
        answer = explorer.query(_query(view=name, explain=1))
        assert view_of(answer["sql"]) == name


def test_a_second_query_is_refused_rather_than_queued():
    hold = threading.Event()
    explorer = Explorer(StubTransport(hold=hold))
    started = threading.Event()

    def run():
        started.set()
        explorer.query(_query(view="radius_authentications"))

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    started.wait(5)
    # The first statement is in flight; a queue here would let impatience spend
    # the whole duty cycle.
    for _ in range(100):
        if explorer._lock.locked():
            break
        time.sleep(0.01)
    with pytest.raises(ExploreError) as raised:
        explorer.query(_query(view="endpoints_data"))
    assert raised.value.error == "busy"
    assert raised.value.status == 429
    assert explorer.status()["busy"] is True
    hold.set()
    worker.join(5)
    assert explorer.status()["busy"] is False


def test_a_long_cooldown_is_refused_with_the_wait_rather_than_waited_out():
    explorer = _explorer(wait=explore.MAX_PACING_WAIT_SECONDS + 23.4)
    with pytest.raises(ExploreError) as raised:
        explorer.query(_query(view="radius_authentications"))
    assert raised.value.error == "cooldown"
    assert raised.value.status == 503
    assert raised.value.retry_after == pytest.approx(38.4)
    assert raised.value.payload()["retry_after_seconds"] == pytest.approx(38.4)
    assert explorer.transport.calls == []


def test_a_short_cooldown_is_simply_waited_out_by_the_transport():
    explorer = _explorer(wait=explore.MAX_PACING_WAIT_SECONDS - 1)
    assert explorer.query(_query(view="radius_authentications"))["row_count"] == 1


def test_force_runs_through_a_cooldown_that_would_otherwise_refuse():
    explorer = _explorer(wait=explore.MAX_PACING_WAIT_SECONDS + 300)
    answer = explorer.query(_query(view="radius_authentications", force=1))
    assert answer["row_count"] == 1
    assert answer["forced"] is True
    # On the forced lane, never the adaptive one -- and the lane wait is
    # bounded, because forcing must not hang an HTTP thread behind a sleeping
    # scheduled statement.
    assert explorer.transport.calls == []
    [(_sql, binds, lane_timeout)] = explorer.transport.forced_calls
    assert binds["limit"] == 100
    assert lane_timeout == explorer.max_wait
    assert explorer.status()["last_query"]["result"] == "forced"


def test_an_unforced_answer_says_it_was_not_forced():
    explorer = _explorer()
    assert explorer.query(
        _query(view="radius_authentications"))["forced"] is False
    assert explorer.query(
        _query(view="radius_authentications", explain=1))["forced"] is None


def test_force_is_counted_apart_from_paced_successes():
    explorer = _explorer()
    before = _counted("forced")
    explorer.query(_query(view="radius_authentications", force=1))
    assert _counted("forced") == before + 1


def test_a_forced_query_is_still_refused_while_the_lane_is_held():
    # Forcing skips the waits, not the serialisation: the transport reports a
    # held lane as busy, and that must reach the operator as 429, not 502.
    explorer = _explorer(error=TransportError(
        "busy", "a scheduled statement held the Data Connect lane for 15s"))
    with pytest.raises(ExploreError) as raised:
        explorer.query(_query(view="radius_authentications", force=1))
    assert raised.value.error == "busy"
    assert raised.value.status == 429


def test_force_still_flows_through_the_single_flight_lock():
    hold = threading.Event()
    explorer = Explorer(StubTransport(hold=hold))
    started = threading.Event()

    def run():
        started.set()
        explorer.query(_query(view="radius_authentications"))

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    started.wait(5)
    for _ in range(100):
        if explorer._lock.locked():
            break
        time.sleep(0.01)
    with pytest.raises(ExploreError) as raised:
        explorer.query(_query(view="endpoints_data", force=1))
    assert raised.value.error == "busy"
    hold.set()
    worker.join(5)


def test_the_routes_answer_pending_until_discovery_has_run():
    explorer = _explorer(schema=None)
    for call in (lambda: explorer.views(),
                 lambda: explorer.query(_query(view="radius_authentications"))):
        with pytest.raises(ExploreError) as raised:
            call()
        assert raised.value.error == "schema_pending"
        assert raised.value.status == 503


def test_the_explorer_never_triggers_discovery_itself():
    # Discovery is a paced statement and belongs to the scheduler; running it
    # from a page refresh would take the lane from the datasets waiting on it.
    class Discovering(StubTransport):
        def __init__(self):
            super().__init__(schema=None)
            self.discovered = False

        def prepare(self):
            self.discovered = True

        def discover_schema(self):
            self.discovered = True
            return {}

    transport = Discovering()
    explorer = Explorer(transport)
    with pytest.raises(ExploreError):
        explorer.views()
    explorer.status()
    assert transport.discovered is False


@pytest.mark.parametrize("reason", [
    "timeout", "invalid_response", "response_too_large", "connection_failed",
    "authentication_backoff",
])
def test_a_transport_failure_keeps_its_reason_and_becomes_a_502(reason):
    explorer = _explorer(error=TransportError(reason, "ORA-something"))
    with pytest.raises(ExploreError) as raised:
        explorer.query(_query(view="radius_authentications"))
    assert raised.value.error == reason
    assert raised.value.status == 502
    assert raised.value.detail == "ORA-something"
    # And the lane is free again: a failed statement must not wedge the lock.
    assert explorer._lock.locked() is False


def test_status_is_cheap_and_says_what_the_lane_is_doing():
    explorer = _explorer(wait=12.34)
    status = explorer.status()
    assert status["configured"] is True
    assert status["schema_discovered"] is True
    assert status["views_total"] == len(VIEW_CATALOG)
    assert status["views_available"] == len(SCHEMA)
    assert status["duty_cycle_percent"] == 3.0
    assert status["cooldown_remaining_seconds"] == 12.3
    assert status["busy"] is False
    assert status["last_query"] is None
    assert explorer.transport.calls == []


def test_the_last_query_is_remembered_with_its_age_and_its_outcome():
    clock = iter([1000.0, 1042.0])
    explorer = Explorer(StubTransport(), clock=lambda: next(clock))
    explorer.query(_query(view="radius_authentications"))
    last = explorer.status()["last_query"]
    assert last["view"] == "radius_authentications"
    assert last["rows"] == 1
    assert last["result"] == "success"
    assert last["at_age_seconds"] == 42.0


def test_a_failed_query_is_remembered_as_the_reason_it_failed():
    explorer = _explorer(error=TransportError("timeout", "too slow"))
    with pytest.raises(ExploreError):
        explorer.query(_query(view="radius_authentications"))
    assert explorer.status()["last_query"]["result"] == "timeout"
    assert explorer.status()["last_query"]["rows"] == 0


def test_a_view_descriptor_tells_the_truth_about_this_account():
    explorer = _explorer(schema={"ENDPOINTS_DATA": set(ENDPOINT_COLUMNS)})
    views = {view["name"]: view for view in explorer.views()}
    assert len(views) == len(VIEW_CATALOG)

    endpoints = views["endpoints_data"]
    assert endpoints["available"] is True
    assert endpoints["view"] == "ENDPOINTS_DATA"
    assert endpoints["time_column"] == "UPDATE_TIME"
    assert endpoints["time_kind"] == "tstz"
    assert endpoints["window_optional"] is True
    assert endpoints["columns"] == sorted(ENDPOINT_COLUMNS)
    assert endpoints["default_columns"][0] == "MAC_ADDRESS"

    missing = views["radius_authentications"]
    assert missing["available"] is False
    assert missing["columns"] == []
    assert missing["default_columns"] is None


def test_a_declared_time_column_the_catalog_lacks_is_reported_as_absent():
    # Better a view that cannot be windowed than every statement failing with
    # ORA-00904 on a release that renamed the column.
    explorer = _explorer(schema={"RADIUS_AUTHENTICATIONS": {"USERNAME"}})
    view = next(item for item in explorer.views()
                if item["name"] == "radius_authentications")
    assert view["available"] is True
    assert view["time_column"] is None
    with pytest.raises(ExploreError) as raised:
        explorer.query(_query(view="radius_authentications", last="1h"))
    assert raised.value.error == "invalid_request"


def test_every_outcome_is_counted_against_the_shared_duty_cycle():
    explorer = _explorer()
    before = {name: _counted(name)
              for name in ("success", "explain", "unknown_view", "busy")}
    explorer.query(_query(view="radius_authentications"))
    explorer.query(_query(view="radius_authentications", explain=1))
    with pytest.raises(ExploreError):
        explorer.query(_query(view="not a legal name"))
    assert _counted("success") == before["success"] + 1
    assert _counted("explain") == before["explain"] + 1
    assert _counted("unknown_view") == before["unknown_view"] + 1


# --- the routes -------------------------------------------------------------

def _api(explorer=None):
    config = Config.from_document(
        {"targets": {"pan": {"host": "pan1", "user": "ro"}}},
        path="test.toml", environ={"ISE_PASS": "secret"})
    return OperatorApi(config, build_plan(config), None, explorer)


def _route(api, path, query=None):
    import json

    status, body, content_type = api.routes()[path](query or {})
    assert content_type.startswith("application/json")
    return status, json.loads(body.decode("utf-8"))


def test_the_routes_carry_the_status_the_refusal_asked_for():
    api = _api(_explorer())
    status, payload = _route(
        api, "/api/v1/dataconnect/query", _query(view="not a legal name"))
    assert status == 404 and payload["error"] == "unknown_view"

    status, payload = _route(
        api, "/api/v1/dataconnect/query", _query(view="dba_users"))
    assert status == 409 and payload["error"] == "view_unavailable"

    status, payload = _route(
        api, "/api/v1/dataconnect/query",
        _query(view="radius_authentications", cols="PASSWORD"))
    assert status == 400 and payload["error"] == "invalid_request"

    status, payload = _route(
        api, "/api/v1/dataconnect/query", _query(view="system_summary"))
    assert status == 409 and payload["error"] == "view_unavailable"


def test_a_route_query_is_the_parse_qs_shape_the_listener_produces():
    api = _api(_explorer())
    status, payload = _route(
        api, "/api/v1/dataconnect/query",
        {"view": ["radius_authentications"], "eq": ["USERNAME:alice"],
         "like": ["DEVICE_NAME:sw-*"], "first": ["5"]})
    assert status == 200
    assert payload["binds"] == {"b0": "alice", "b1": "sw-%", "limit": 5}
    assert payload["row_count"] == 1


def test_the_views_route_answers_the_curated_catalog():
    status, payload = _route(_api(_explorer()), "/api/v1/dataconnect/views")
    assert status == 200
    assert {view["name"] for view in payload} == set(VIEW_CATALOG)


def test_without_an_oracle_target_the_namespace_refuses_by_name():
    api = _api(None)
    for path in ("/api/v1/dataconnect/views", "/api/v1/dataconnect/query"):
        status, payload = _route(api, path)
        assert status == 503
        assert payload["error"] == "dataconnect_unconfigured"
    # Status still answers: "is Data Connect configured at all" is the first
    # question this route exists to settle.
    status, payload = _route(api, "/api/v1/dataconnect/status")
    assert status == 200
    assert payload["configured"] is False
    assert payload["views_total"] == len(VIEW_CATALOG)


def test_the_contract_holds_over_real_http_the_way_a_client_sees_it():
    # The PowerShell module is built against this shape, so the whole path --
    # query string, parse_qs, handler, status code, JSON body -- is asserted
    # once end to end rather than only through the route table.
    import json
    import urllib.error
    import urllib.request

    from ise_exporter3.server import HttpServer
    from ise_exporter3.snapshots import LockedCollectorRegistry

    server = HttpServer(
        "127.0.0.1", 0, LockedCollectorRegistry(),
        routes=_api(_explorer()).routes())
    server.start()
    try:
        base = f"http://127.0.0.1:{server.address[1]}"
        with urllib.request.urlopen(
                f"{base}/api/v1/dataconnect/query?view=radius_authentications"
                "&eq=USERNAME:alice&like=DEVICE_NAME:sw-*&last=2h&first=5",
                timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        assert response.status == 200
        assert payload["view"] == "radius_authentications"
        assert payload["binds"] == {"b0": "alice", "b1": "sw-%", "limit": 5}
        assert payload["rows"] == [{"timestamp": "t"}]
        assert payload["truncated"] is False

        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(
                f"{base}/api/v1/dataconnect/query?view=nope", timeout=5)
        # A legal name nobody curated is judged by the catalog, not by name.
        assert raised.value.code == 409
        # The error body is JSON too: a client must be able to say why rather
        # than surfacing a raw HTTP exception.
        assert json.loads(raised.value.read())["error"] == "view_unavailable"
    finally:
        server.stop(timeout=5)


def test_the_index_lists_the_namespace_that_spends_budget():
    _status, payload = _route(_api(), "/api/v1")
    assert "/api/v1/dataconnect/views" in payload["routes"]
    assert "/api/v1/dataconnect/query" in payload["routes"]
    assert "/api/v1/dataconnect/status" in payload["routes"]
