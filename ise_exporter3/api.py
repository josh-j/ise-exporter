"""Local read-only operator API.

This replaces v2's 3394-line Python CLI. The exporter already holds every answer
an operator wants -- which source is live, what it costs, why a dataset is down --
and it holds them behind the pacing gate and the authentication guard. Serving
them from the running process means the operator surface cannot bypass those
guards, because there is no second process to bypass them from.

It binds to localhost by default and is strictly read-only. Every route but one
namespace reports state the exporter already computed and reaches nothing.

The exception is ``/api/v1/dataconnect``, and it is stated rather than papered
over: those routes run bounded SELECTs against the reporting views through the
same ``DataConnectTransport`` the scheduler uses -- same pacing gate, same
adaptive cooldown, same ceilings, same authentication guard. That is the point.
Ad-hoc navigation either happens here, inside the process that owns the budget,
or it happens in a second process that owns nothing.
"""
from __future__ import annotations

import fnmatch
import json
import re
import time

from . import __version__
from .config import format_duration
from .explore import ExploreError, unconfigured_error, unconfigured_status
from .plan import render_plan
from .pxgrid import normalize_mac, project_session
from .transports import TransportError


CONTENT_TYPE = "application/json; charset=utf-8"

# A live MnT session record carries about fifty fields. The cap is a
# backstop against a future release growing that without bound, not a
# curation: the fields nothing else has are the reason to call this.
MNT_MAX_FIELDS = 200


def _json(payload, status=200):
    body = json.dumps(payload, indent=2, default=str).encode("utf-8")
    return status, body, CONTENT_TYPE


def _one(query, name):
    """One value from a parsed query string, whatever shape the parser used."""
    value = (query or {}).get(name)
    if isinstance(value, (list, tuple)):
        return value[0] if value else ""
    return value or ""


def _positive_int(value):
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


# The shape normalize_mac produces, and the only shape allowed into a URL path.
_CANONICAL_MAC = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")


def _bounded_record(record, limits):
    """Serve the whole MnT record, minus anything that would be unbounded.

    MnT is the one interface that hands over a document rather than a fixed
    row, and the point of this route is the fields nothing else carries -- so
    it is not projected down to a curated list. It is bounded instead: a field
    ceiling and a value ceiling, both already derived from the declared scale.

    Returns the fields and how many the ceiling dropped, because a bound that
    is hit silently reads as a complete record. Same reasoning as the probe
    column's note: only the answer knows whether it is partial, so it has to
    say.
    """
    out, kept = {}, 0
    for name, value in sorted(record.items()):
        if kept >= MNT_MAX_FIELDS:
            break
        text = str(value)
        if len(text) > limits.field_bytes:
            text = text[:limits.field_bytes] + "...[truncated]"
        out[name] = text
        kept += 1
    return out, max(0, len(record) - kept)


def _mac_matches(mac, pattern):
    """Match on hex digits, because a MAC has as many spellings as writers.

    The caller may type AA:BB:CC:*, aabb.cc*, or the bare digits; ISE writes a
    fourth thing. Comparing the normalised forms means an operator who pasted a
    MAC out of one screen can use it against another.

    Wildcards are matched, not stripped. The cmdlets pass the same pattern here
    that they pass to a Data Connect ``LIKE``, so ``*1234*`` has to mean the
    same thing on both -- stripping the stars and treating a trailing one as a
    prefix made a mid-pattern wildcard match the wrong endpoints rather than
    refuse. fnmatch's ``*``/``?``/``[]`` are PowerShell's ``-like`` operators,
    which is what the caller was writing for.
    """
    wanted = normalize_mac(pattern).replace(":", "")
    have = normalize_mac(mac).replace(":", "")
    if not wanted:
        return False
    return fnmatch.fnmatchcase(have, wanted)


class OperatorApi:
    """Builds the route table from live scheduler state."""

    def __init__(self, config, plan, scheduler=None, explorer=None,
                 pxgrid=None, mnt=None, clock=time.time):
        self.config = config
        self.plan = plan
        self.scheduler = scheduler
        # Absent whenever no oracle target is configured, which is why every
        # dataconnect route has to answer without one.
        self.explorer = explorer
        # Absent whenever no pxgrid target is configured, on the same terms.
        self.pxgrid = pxgrid
        # The MnT persona's transport, for the per-endpoint session detail.
        self.mnt = mnt
        self.clock = clock

    # --- views ------------------------------------------------------------

    def health(self):
        states = self.scheduler.states if self.scheduler else ()
        collecting = sum(1 for state in states
                         if self.scheduler.last_success.get(state.name))
        return {
            "version": __version__,
            "config_file": self.config.path,
            "profile": self.config.profile,
            "datasets_total": len(self.plan.entries),
            "datasets_enabled": len(self.plan.enabled),
            "datasets_collecting": collecting,
            "datasets_unresolved": [entry.name for entry in self.plan.unresolved],
            "datasets_degraded": sorted(
                state.name for state in states if state.degraded),
            "fits_budget": self.plan.fits,
            "over_budget": [target.target for target in self.plan.overages],
        }

    def datasets(self):
        now = self.clock()
        rows = []
        for entry in self.plan.entries:
            state = self._state(entry.name)
            success = (self.scheduler.last_success.get(entry.name)
                       if self.scheduler else None)
            failure = (self.scheduler.runner.failures.get(entry.name)
                       if self.scheduler else None)
            rows.append({
                "dataset": entry.name,
                "description": entry.description,
                "enabled": entry.enabled,
                "interval": format_duration(entry.interval),
                "provider": state.provider.name if state else (
                    entry.provider.name if entry.resolved else None),
                "target": state.target if state else entry.target,
                "degraded": bool(state.degraded) if state else entry.degraded,
                "scheduled": state is not None,
                "last_success_age_seconds": (
                    round(now - success, 1) if success else None),
                "next_run_in_seconds": self._next_run_in(entry.name, now),
                "consecutive_failures": failure["count"] if failure else 0,
                "failure_reason": failure["reason"] if failure else None,
                "failure_detail": failure["detail"] if failure else None,
            })
        return rows

    def providers(self):
        rows = []
        for entry in self.plan.entries:
            state = self._state(entry.name)
            declared = entry.dataset.providers if entry.dataset else ()
            for provider in declared:
                usable = state is not None and provider in state.candidates
                rows.append({
                    "dataset": entry.name,
                    "provider": provider.name,
                    "target": provider.target,
                    "active": bool(state and state.provider is provider),
                    "usable": usable,
                    "coverage": provider.coverage,
                    "supplies": sorted(provider.supplies),
                    "requires": list(provider.requires),
                    "notes": provider.notes,
                })
        return rows

    def targets(self):
        return [target.to_dict() for target in self.plan.targets]

    def plan_view(self):
        return self.plan.to_dict()

    def _state(self, name):
        if not self.scheduler:
            return None
        for state in self.scheduler.states:
            if state.name == name:
                return state
        return None

    def _next_run_in(self, name, now):
        if not self.scheduler:
            return None
        when = self.scheduler.next_run.get(name)
        return round(max(0.0, when - now), 1) if when else None

    # --- data connect -----------------------------------------------------

    def dataconnect_views(self, _query):
        return self._explored(lambda explorer: explorer.views())

    def dataconnect_query(self, query):
        return self._explored(lambda explorer: explorer.query(query))

    def dataconnect_status(self, _query):
        if self.explorer is None:
            return _json(unconfigured_status())
        return _json(self.explorer.status())

    # --- pxgrid -----------------------------------------------------------

    # The session directory is the one live source the operator surface can
    # offer that costs no Oracle duty cycle: the transport already holds a
    # snapshot, kept current by the pubsub subscription the active_sessions
    # dataset pays for. Serving it is a read of memory this process already has.
    #
    # Which is exactly why it must not force a refresh. get_sessions reconciles
    # with a full getSessions when its baseline ages out, and that is a bounded
    # but large call against the appliance. An operator refreshing a page must
    # not be able to schedule one, so this asks for whatever is held --
    # refresh=False, not an age so large it usually does not happen -- and
    # reports how old it is instead of quietly making it fresher.

    def pxgrid_sessions(self, query):
        if self.pxgrid is None:
            return _json({
                "error": "pxgrid_unconfigured",
                "detail": ("this exporter has no pxGrid target configured, so "
                           "there is no session directory to read"),
            }, 409)
        try:
            records = self.pxgrid.get_sessions(refresh=False)
        except TransportError as error:
            # A disconnected stream is a refusal with a reason, not a 500: the
            # caller can act on "pxGrid is not connected" and cannot act on a
            # traceback.
            return _json({"error": error.reason, "detail": error.detail}, 503)

        pattern = str(_one(query, "mac") or "").strip()
        rows = []
        for record in records:
            projected = project_session(record)
            if projected is None:
                continue
            if pattern and not _mac_matches(projected["mac_address"], pattern):
                continue
            rows.append(projected)
        rows.sort(key=lambda row: row["mac_address"])

        ceiling = self.config.limits.result_rows
        requested = _positive_int(_one(query, "first"))
        if requested:
            ceiling = min(ceiling, requested)
        truncated = len(rows) > ceiling
        age = self.pxgrid.snapshot_age()
        return _json({
            "sessions": rows[:ceiling],
            "row_count": min(len(rows), ceiling),
            "matched": len(rows),
            "truncated": truncated,
            # Stated rather than hidden: these are the sessions as of a moment,
            # and a caller joining them onto endpoint rows should know which.
            # None until a baseline has been taken, which is honest -- an age
            # measured from the epoch is not an age.
            "snapshot_age_seconds": None if age is None else round(age, 1),
        })

    # --- mnt ---------------------------------------------------------------

    # MnT carries per-endpoint detail no other interface does: accounting
    # counters, the policy execution steps, and the correlation ids -- 39 of its
    # 47 fields have no PROBE_DATA counterpart. What it does not have is a bulk
    # form, so this is one request per endpoint and the caller has to be
    # deliberate about how many it asks for.
    MNT_NO_SESSION = "34110"

    def mnt_session(self, query):
        if self.mnt is None:
            return _json({
                "error": "mnt_unconfigured",
                "detail": ("this exporter has no MnT target configured, so "
                           "there is no session detail to read"),
            }, 409)
        # This value reaches a URL path. Normalising and then insisting on the
        # canonical shape is what keeps it from reaching any other MnT resource;
        # the check is on the normalised form so it cannot be evaded by
        # spelling the MAC differently.
        mac = normalize_mac(_one(query, "mac"))
        if not _CANONICAL_MAC.match(mac):
            return _json({
                "error": "invalid_request",
                "detail": "mac must be a MAC address",
            }, 400)
        try:
            answer = self.mnt.get_mnt_xml(
                f"/Session/MACAddress/{mac}", api="mnt_session_detail")
        except TransportError as error:
            if error.code == self.MNT_NO_SESSION:
                # An ordinary event dressed as a server error: MnT answers a
                # MAC it holds no session for with 500 and this code. Reporting
                # it as a failure would make "not connected" look like an
                # outage on every quiet endpoint.
                return _json({"mac_address": mac, "session": None,
                              "found": False})
            return _json({"error": error.reason, "detail": error.detail}, 502)
        records = answer.get("sessions") or []
        if not records:
            return _json({"mac_address": mac, "session": None, "found": False})
        session, omitted = _bounded_record(records[0], self.config.limits)
        return _json({
            "mac_address": mac,
            "found": True,
            "session": session,
            # Zero on every release that exists; named anyway, because the one
            # thing a caller cannot recover from a silently short record is
            # that it was short.
            "fields_omitted": omitted,
        })

    def _explored(self, work):
        """One place where an explorer refusal becomes a status and a body."""
        try:
            if self.explorer is None:
                raise unconfigured_error()
            return _json(work(self.explorer))
        except ExploreError as error:
            return _json(error.payload(), error.status)

    # --- routes -----------------------------------------------------------

    def routes(self):
        # Every handler takes the parsed query string, whether it reads it or
        # not: one signature means the listener never has to know which routes
        # take parameters.
        return {
            "/api/v1/health": lambda query: _json(self.health()),
            "/api/v1/datasets": lambda query: _json(self.datasets()),
            "/api/v1/providers": lambda query: _json(self.providers()),
            "/api/v1/targets": lambda query: _json(self.targets()),
            "/api/v1/plan": lambda query: _json(self.plan_view()),
            "/api/v1/plan.txt": lambda query: (
                200, render_plan(self.plan).encode("utf-8"),
                "text/plain; charset=utf-8"),
            "/api/v1/dataconnect/views": self.dataconnect_views,
            "/api/v1/dataconnect/query": self.dataconnect_query,
            "/api/v1/dataconnect/status": self.dataconnect_status,
            "/api/v1/pxgrid/sessions": self.pxgrid_sessions,
            "/api/v1/mnt/session": self.mnt_session,
            "/api/v1": lambda query: _json({
                "version": __version__,
                "routes": ["/api/v1/health", "/api/v1/datasets",
                           "/api/v1/providers", "/api/v1/targets",
                           "/api/v1/plan", "/api/v1/plan.txt",
                           "/api/v1/dataconnect/views",
                           "/api/v1/dataconnect/query",
                           "/api/v1/dataconnect/status",
                           "/api/v1/pxgrid/sessions",
                           "/api/v1/mnt/session"],
            }),
        }
