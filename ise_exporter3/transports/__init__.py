"""Transports: one connection to one ISE persona.

A transport owns credentials, TLS policy, timeouts and the authentication guard
for its target, and it counts every request it actually sends. That count is the
measured half of the load model -- planned load is a declaration, this is what
really left the process.

Failures are a closed, bounded set of reasons. v2 let its REST client return
``None`` for the CLI and raise for the runtime, which meant two behaviours per
call site; v3 always raises, because the operator surface is the API in M5 and
it wants structured errors too.
"""
from __future__ import annotations

import logging
import os

from ..config import TargetConfig


logger = logging.getLogger(__name__)


# The complete set of reasons a collection can fail. Anything outside this set
# would be an unbounded label, so unknown exceptions map to unexpected_error.
FAILURE_REASONS = frozenset({
    "authentication_backoff",
    "authentication_failed",
    "authorization_failed",
    "connection_failed",
    "dependency_pending",
    "http_error",
    "invalid_response",
    "not_configured",
    "not_implemented",
    "response_too_large",
    "schema_incompatible",
    "schema_pending",
    "state_unavailable",
    "timeout",
    "tls_failed",
    "unexpected_error",
})

FAILURE_EXPLANATIONS = {
    "authentication_backoff": "Requests are paused by the shared authentication backoff",
    "authentication_failed": "ISE rejected the configured credentials",
    "authorization_failed": "The configured account lacks access to this API",
    "connection_failed": "The configured host could not be reached",
    "dependency_pending": "Shared state another dataset fills is not ready yet",
    "http_error": "ISE returned an unexpected HTTP status",
    "invalid_response": "ISE returned a response the exporter could not parse",
    "not_configured": "This target is not configured",
    "not_implemented": "This provider is declared but not built yet",
    "response_too_large": "ISE returned a response above the safety limit",
    "schema_incompatible": "A required reporting view or column is absent",
    "schema_pending": "Schema discovery has not completed successfully yet",
    "state_unavailable": "Required shared safety state is unavailable",
    "timeout": "The request exceeded its bounded timeout",
    "tls_failed": "TLS certificate or protocol validation failed",
    "unexpected_error": "The transport failed unexpectedly",
}


class TransportError(RuntimeError):
    """A bounded, typed transport failure carried into dataset health."""

    def __init__(self, reason, detail="", status=0, code=""):
        reason = reason if reason in FAILURE_REASONS else "unexpected_error"
        detail = str(detail or FAILURE_EXPLANATIONS[reason])
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        # The HTTP status, and for MnT the <cpm-code> out of its error body.
        # A reader that has to recover either from the detail text is reading
        # a sentence written for a human: MnT answers a MAC with no current
        # session with 500 and cpm-code 34110, which is an ordinary event, and
        # telling it apart from an outage decides whether a dataset counts a
        # failure.
        self.status = int(status or 0)
        self.code = str(code or "")


class Transport:
    """Base class. Subclasses implement whatever their providers need."""

    target = ""

    def close(self):
        """Release connections. Safe to call more than once."""

    def prepare(self):
        """Do any one-off live discovery this transport needs.

        Called on the target's own lane before an attempt, so discovery is
        serialised and paced like everything else and its failure is reported
        as that dataset's failure rather than taking down startup.
        """

    def satisfies(self, requirements):
        """Check a provider's live preconditions against the real appliance.

        The plan defers ``view:`` and ``capability:`` requirements because they
        cannot be settled without connecting. This is where they are settled.
        Returns ``(ok, reason, detail)``; a transport with nothing to check
        simply agrees.
        """
        return True, "", ""


def guard_path(config, target):
    """Derive the shared guard file from the state directory.

    One less configured knob: the guard lives beside the state database, so an
    operator who has already pointed the exporter at a writable directory has
    nothing further to set.
    """
    state_directory = os.path.dirname(
        os.path.abspath(os.path.expanduser(config.exporter.state_db)))
    return os.path.join(state_directory, "shared", f"{target}-auth.guard")


def build_transports(config, *, kinds=None):
    """Construct a transport for every configured target v3 can talk to.

    Unconfigured targets simply get no transport; the plan has already reported
    which datasets that makes unavailable, and by name.
    """
    from .dataconnect import DataConnectTransport
    from .pxgrid import PxGridTransport
    from .rest import RestTransport

    builders = {
        "pan": lambda: RestTransport(config, "pan"),
        "mnt": lambda: RestTransport(config, "mnt"),
        "oracle": lambda: DataConnectTransport(config),
        "pxgrid": lambda: PxGridTransport(config),
    }
    if kinds is not None:
        builders = {name: build for name, build in builders.items() if name in kinds}

    transports = {}
    for name, build in builders.items():
        target = config.targets.get(name, TargetConfig(name=name))
        if not target.configured:
            logger.info(
                "transport %s not built: %s", name, target.unconfigured_reason())
            continue
        transports[name] = build()
    return transports


def close_transports(transports):
    for name, transport in (transports or {}).items():
        try:
            transport.close()
        except Exception as error:      # noqa: BLE001 - teardown must not raise
            logger.warning("could not close the %s transport: %s", name, error)
