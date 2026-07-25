"""Local read-only operator API.

This replaces v2's 3394-line Python CLI. The exporter already holds every answer
an operator wants -- which source is live, what it costs, why a dataset is down --
and it holds them behind the pacing gate and the authentication guard. Serving
them from the running process means the operator surface cannot bypass those
guards, because there is no second process to bypass them from.

It binds to localhost by default and is strictly read-only: every route reports
state the exporter already computed. Nothing here reaches ISE.
"""
from __future__ import annotations

import json
import time

from . import __version__
from .config import format_duration
from .plan import render_plan


CONTENT_TYPE = "application/json; charset=utf-8"


def _json(payload, status=200):
    body = json.dumps(payload, indent=2, default=str).encode("utf-8")
    return status, body, CONTENT_TYPE


class OperatorApi:
    """Builds the route table from live scheduler state."""

    def __init__(self, config, plan, scheduler=None, clock=time.time):
        self.config = config
        self.plan = plan
        self.scheduler = scheduler
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

    # --- routes -----------------------------------------------------------

    def routes(self):
        return {
            "/api/v1/health": lambda: _json(self.health()),
            "/api/v1/datasets": lambda: _json(self.datasets()),
            "/api/v1/providers": lambda: _json(self.providers()),
            "/api/v1/targets": lambda: _json(self.targets()),
            "/api/v1/plan": lambda: _json(self.plan_view()),
            "/api/v1/plan.txt": lambda: (
                200, render_plan(self.plan).encode("utf-8"),
                "text/plain; charset=utf-8"),
            "/api/v1": lambda: _json({
                "version": __version__,
                "routes": ["/api/v1/health", "/api/v1/datasets",
                           "/api/v1/providers", "/api/v1/targets",
                           "/api/v1/plan", "/api/v1/plan.txt"],
            }),
        }
