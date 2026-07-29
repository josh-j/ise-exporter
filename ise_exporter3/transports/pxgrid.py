"""Bounded pxGrid 2.0 control, REST, and STOMP/WSS transport.

The session topic is event-sourced but not replayable.  Each connection
therefore subscribes first, buffers events while ``getSessions`` supplies the
baseline, and then drains the newer events over that snapshot.  Reconnects and
sequence gaps repeat the same transaction, so neither loses a disconnect that
races the baseline nor publishes a partial state.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import ssl
import threading
import time
from urllib.parse import urlsplit

import requests
from requests.auth import HTTPBasicAuth
from urllib3.exceptions import InsecureRequestWarning

from .. import telemetry
from ..auth_guard import PersistentAuthGuard
from ..pxgrid import GONE_SESSION_STATES, as_list, session_key, session_state
from ..rate_limit import BudgetLimiter
from . import Transport, TransportError, guard_path
from .rest import (
    AUTH_FAILURE_BACKOFF_SECONDS,
    AUTH_FAILURE_THRESHOLD,
    HTTP_READ_CHUNK_BYTES,
    MAX_HTTP_RESPONSE_BYTES,
    ResponseTooLarge,
    error_snippet,
    redact,
    split_timeout,
)


logger = logging.getLogger(__name__)

SESSION_SERVICE = "com.cisco.ise.session"
ENDPOINT_SERVICE = "com.cisco.ise.endpoint"
PUBSUB_SERVICE = "com.cisco.ise.pubsub"

ENDPOINT_BULK_START = "1970-01-01T00:00:00.000Z"
ENDPOINT_PAGE_SIZE = 1000
MAX_ENDPOINTS = 200_000
STREAM_PING_SECONDS = 10
STREAM_RECONNECT_MAX_SECONDS = 60


class _SequenceGap(RuntimeError):
    pass


def _rest_base(properties):
    return properties.get("restBaseUrl") or properties.get("restBaseURL")


def _safe_https_url(value, label):
    parsed = urlsplit(str(value or ""))
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise TransportError(
            "invalid_response", f"pxGrid {label} returned an unsafe URL")
    return str(value).rstrip("/")


def _endpoint_identity(row):
    for field in ("id", "mac", "macAddress"):
        value = row.get(field)
        if value:
            return str(value)
    return json.dumps(row, sort_keys=True, default=str)


def _safe_wss_url(value):
    parsed = urlsplit(str(value or ""))
    if parsed.scheme not in {"wss", "https"} or not parsed.hostname:
        raise TransportError(
            "invalid_response", "pxGrid pubsub service returned an unsafe URL")
    if parsed.username or parsed.password:
        raise TransportError(
            "invalid_response", "pxGrid pubsub URL contains credentials")
    value = str(value)
    return (
        value.replace("https://", "wss://", 1)
        if value.startswith("https://") else value
    )


class PxGridTransport(Transport):
    target = "pxgrid"

    def __init__(self, config):
        self.config = config
        self.settings = config.targets["pxgrid"]
        self.timeout = self.settings.request_timeout
        port = self.settings.port or 8910
        self.control_base = (
            f"https://{self.settings.host}:{port}/pxgrid/control")
        self.limiter = BudgetLimiter(
            self.target, config.budget_for(self.target),
            enforce=config.exporter.enforce_budget)
        self._guard = PersistentAuthGuard(
            guard_path(config, self.target),
            (self.settings.node_name, self.settings.host),
            "pxGrid authentication",
        )

        self.session = requests.Session()
        self.session.trust_env = False
        self.session.auth = HTTPBasicAuth(
            self.settings.node_name, self.settings.password)
        self.session.verify = self._verify()
        if self.settings.client_cert:
            self.session.cert = (
                self.settings.client_cert, self.settings.client_key)
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

        self._request_lock = threading.RLock()
        self._prepare_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._sync_lock = threading.Lock()
        # Separate from _prepare_lock, which prepare() holds and is not
        # reentrant: the stream is started from satisfies(), not from prepare().
        self._stream_lock = threading.Lock()
        self._shutdown = None
        self._closed = threading.Event()
        self._connected = threading.Event()
        self._supervisor = None
        self._ws = None
        self._ws_lock = threading.Lock()

        self._activated = False
        self._discovered = False
        self._services = {}
        self._rest_services = {}
        self._peer_secrets = {}
        self._capabilities = set()
        self._stream_error = None
        self._sessions = {}
        self._session_snapshot_at = 0.0
        self._endpoints = []
        self._endpoint_snapshot_at = 0.0
        self._syncing = False
        self._buffer = []
        self._sequence = {}
        self._awaiting_stream = False

    def _verify(self):
        if not self.settings.verify_tls:
            return False
        return (self.settings.ca_bundle or "").strip() or True

    def _stopping(self):
        return self._closed.is_set() or (
            self._shutdown is not None and self._shutdown.is_set())

    def set_shutdown_event(self, shutdown):
        self._shutdown = shutdown
        self.limiter.set_shutdown_event(shutdown)

    def set_warming(self, warming):
        self.limiter.set_warming(warming)

    def close(self):
        self._closed.set()
        self._connected.clear()
        self._close_ws()
        thread = self._supervisor
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        with self._request_lock:
            session, self.session = self.session, None
        if session is not None:
            session.close()
        telemetry.pxgrid_stream_connected.set(0)

    # --- HTTP ------------------------------------------------------------

    def _count(self, api, status):
        telemetry.api_requests_total.labels(
            target=self.target, api=api, status=status).inc()

    def _error(self, api, kind, code="0"):
        telemetry.api_errors_total.labels(
            target=self.target, api=api, error_type=kind,
            http_code=str(code)).inc()

    @staticmethod
    def _buffer_response(response, max_bytes):
        raw = str(response.headers.get("Content-Length", "") or "").strip()
        try:
            declared = int(raw)
        except (TypeError, ValueError):
            declared = None
        if declared is not None and declared > max_bytes:
            response.close()
            raise ResponseTooLarge(
                f"Content-Length {declared} exceeds {max_bytes} bytes")
        retained = bytearray()
        try:
            for chunk in response.iter_content(chunk_size=HTTP_READ_CHUNK_BYTES):
                if not chunk:
                    continue
                if len(retained) + len(chunk) > max_bytes:
                    raise ResponseTooLarge(
                        f"streamed body exceeds {max_bytes} bytes")
                retained.extend(chunk)
        finally:
            response.close()
        response._content = bytes(retained)
        response._content_consumed = True

    def _post(
        self, url, body, *, auth=None, api, account_auth=False,
        max_bytes=MAX_HTTP_RESPONSE_BYTES,
    ):
        if self._stopping():
            raise TransportError("unexpected_error", "Shutting down")
        try:
            blocked = self._guard.blocked(time.time())
        except Exception as error:
            self._error(api, "auth_guard")
            raise TransportError(
                "state_unavailable",
                "The shared pxGrid authentication guard is unavailable") from error
        if blocked:
            self._count(api, "auth_blocked")
            self._error(api, "auth_blocked", 401)
            raise TransportError("authentication_backoff")

        self.limiter.acquire(api)
        telemetry.load_measured_requests_total.labels(target=self.target).inc()
        started = time.monotonic()
        with self._request_lock:
            if self.session is None:
                raise TransportError("unexpected_error", "The transport is closed")
            try:
                import warnings
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", InsecureRequestWarning)
                    response = self.session.post(
                        url, json=body or {}, auth=auth,
                        timeout=split_timeout(self.timeout), stream=True,
                        allow_redirects=False)
                status = int(response.status_code)
                if not 200 <= status < 300:
                    snippet = error_snippet(response)
                    self._count(api, f"http_{status}")
                    self._error(api, "http_error", status)
                    if status == 401:
                        if account_auth:
                            self._record_auth_failure(api)
                        raise TransportError("authentication_failed")
                    if status == 403:
                        raise TransportError("authorization_failed")
                    raise TransportError(
                        "http_error",
                        f"ISE returned HTTP {status} for {api}: {snippet}")
                self._buffer_response(response, max_bytes)
                try:
                    data = response.json()
                except ValueError as error:
                    self._count(api, "invalid_json")
                    self._error(api, "invalid_response")
                    raise TransportError(
                        "invalid_response",
                        f"ISE returned invalid JSON for {api}") from error
                if account_auth:
                    self._guard.success()
                self._count(api, "success")
                return data
            except TransportError:
                raise
            except ResponseTooLarge as error:
                self._count(api, "response_too_large")
                self._error(api, "response_too_large")
                raise TransportError("response_too_large", str(error)) from error
            except requests.exceptions.Timeout as error:
                self._count(api, "timeout")
                self._error(api, "timeout")
                raise TransportError("timeout") from error
            except requests.exceptions.SSLError as error:
                self._count(api, "tls_error")
                self._error(api, "tls_error")
                raise TransportError("tls_failed", str(error)) from error
            except requests.exceptions.ConnectionError as error:
                self._count(api, "connection_error")
                self._error(api, "connection_error")
                raise TransportError(
                    "connection_failed",
                    f"{urlsplit(url).hostname} could not be reached") from error
            except (
                requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ContentDecodingError,
            ) as error:
                # A body that dies in transit is a node problem, so classify it
                # as a connection failure and let _query try the next provider.
                self._count(api, "connection_error")
                self._error(api, "connection_error")
                raise TransportError(
                    "connection_failed",
                    f"{urlsplit(url).hostname} closed the response body early",
                ) from error
            except Exception as error:  # noqa: BLE001 - bounded failure vocabulary
                logger.error(
                    "pxGrid request failed for %s: %s", api, redact(str(error)))
                self._count(api, "error")
                self._error(api, "unknown")
                raise TransportError("unexpected_error") from error
            finally:
                telemetry.api_request_duration_seconds.labels(
                    target=self.target, api=api).observe(
                        max(0.0, time.monotonic() - started))

    def _record_auth_failure(self, api):
        try:
            failures, _deadline = self._guard.failure(
                AUTH_FAILURE_THRESHOLD, AUTH_FAILURE_BACKOFF_SECONDS, time.time())
        except Exception as error:
            self._error(api, "auth_guard")
            raise TransportError(
                "state_unavailable",
                "Could not persist the pxGrid authentication failure") from error
        if failures >= AUTH_FAILURE_THRESHOLD:
            logger.error(
                "pxGrid authentication failed %d times; pausing requests",
                failures)

    def _control(self, operation, body=None):
        return self._post(
            f"{self.control_base}/{operation}", body or {},
            api=f"pxgrid_{operation.lower()}", account_auth=True)

    # --- discovery and service REST -------------------------------------

    def _activate(self):
        if self._activated:
            return
        data = self._control(
            "AccountActivate", {"description": "ise-exporter3"})
        if not isinstance(data, dict):
            raise TransportError(
                "invalid_response", "pxGrid AccountActivate was not an object")
        state = str(data.get("accountState") or "").strip().upper()
        if state == "ENABLED":
            self._activated = True
            return
        if state in {"PENDING", "DISABLED"}:
            raise TransportError(
                "authorization_failed",
                f"pxGrid account is {state}; approve or enable it in ISE")
        raise TransportError(
            "invalid_response",
            f"pxGrid AccountActivate returned state {state or 'missing'}")

    def _lookup(self, service):
        self._activate()
        data = self._control("ServiceLookup", {"name": service})
        if not isinstance(data, dict):
            raise TransportError(
                "invalid_response", f"ServiceLookup({service}) was not an object")
        rows = as_list(data.get("services"), "service")
        return [row for row in rows if isinstance(row, dict)]

    def _discover(self):
        if self._discovered:
            return
        # Session + pubsub are the steady live path. Endpoint discovery is
        # delayed until an endpoint dataset asks for it, avoiding a control
        # request at every session-only deployment start.
        for service in (SESSION_SERVICE, PUBSUB_SERVICE):
            self._services[service] = self._lookup(service)

        sessions = self._services[SESSION_SERVICE]
        if any(
            (row.get("properties") or {}).get("sessionTopic")
            or (row.get("properties") or {}).get("sessionTopicAll")
            for row in sessions
        ):
            self._capabilities.add("capability:pxgrid_session_topic")
        self._discovered = True

    def _access_secret(self, peer):
        cached = self._peer_secrets.get(peer)
        if cached:
            return cached
        data = self._control("AccessSecret", {"peerNodeName": peer})
        secret = data.get("secret") if isinstance(data, dict) else None
        if not secret:
            raise TransportError(
                "invalid_response",
                f"pxGrid AccessSecret returned no secret for {peer}")
        self._peer_secrets[peer] = str(secret)
        return self._peer_secrets[peer]

    def _resolve_rest(self, service, *, refresh=False):
        if not refresh and service in self._rest_services:
            return self._rest_services[service]
        if refresh or service not in self._services:
            if refresh:
                for peer, _base, _secret in self._rest_services.get(service, ()):
                    self._peer_secrets.pop(peer, None)
            self._services[service] = self._lookup(service)
        resolved = []
        for row in self._services.get(service, ()):
            peer = str(row.get("nodeName") or "").strip()
            base = _rest_base(row.get("properties") or {})
            if not peer or not base:
                continue
            resolved.append((
                peer, _safe_https_url(base, f"{service} REST service"),
                self._access_secret(peer),
            ))
        if not resolved:
            raise TransportError(
                "not_configured",
                f"pxGrid service {service} has no usable REST provider")
        self._rest_services[service] = tuple(resolved)
        return self._rest_services[service]

    def _query(
        self, service, endpoint, body=None, *, api,
        max_bytes=MAX_HTTP_RESPONSE_BYTES,
    ):
        last = None
        for resolution in (False, True):
            candidates = self._resolve_rest(service, refresh=resolution)
            for candidate in candidates:
                peer, base, secret = candidate
                try:
                    result = self._post(
                        f"{base}/{endpoint.lstrip('/')}", body or {},
                        auth=HTTPBasicAuth(self.settings.node_name, secret),
                        api=api, max_bytes=max_bytes)
                    # ISE can advertise every node even when one persona or
                    # hostname is unreachable from the exporter. Remember the
                    # provider that answered so a periodic baseline does not
                    # pay for the same known-dead attempt on every cycle.
                    cached = self._rest_services.get(service, ())
                    if cached and cached[0] != candidate:
                        self._rest_services[service] = (
                            candidate,
                            *(row for row in cached if row != candidate),
                        )
                    return result
                except TransportError as error:
                    last = error
                    if error.reason not in {
                        "authentication_failed", "connection_failed",
                        "http_error", "timeout",
                    }:
                        raise
                    logger.info(
                        "pxGrid %s provider %s failed (%s); trying another",
                        endpoint, peer, error.reason)
        raise last or TransportError(
            "connection_failed", f"pxGrid {endpoint} had no reachable provider")

    # --- persistent session stream --------------------------------------

    def prepare(self):
        with self._prepare_lock:
            self._validate_files()
            self._activate()
            self._discover()

    def _ensure_stream(self):
        # Only a dataset that actually requires the session topic pays for the
        # subscription and its getSessions baseline. An endpoint-only pxGrid
        # deployment plans streams=0, so it must not hold one.
        with self._stream_lock:
            if self._supervisor is None:
                self._awaiting_stream = True
            self._start_supervisor()

    def _await_stream(self):
        # Only the first attempt pays for the initial connect and baseline, and
        # only a dataset that needs the stream. Later outages are reported by
        # satisfies() instead of parking the lane on a known failure.
        if not self._awaiting_stream:
            return
        self._awaiting_stream = False
        deadline = time.monotonic() + max(1, self.timeout * 2)
        while not self._stopping() and not self._connected.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._connected.wait(min(0.5, remaining))

    def _validate_files(self):
        for label_name, path in (
            ("client certificate", self.settings.client_cert),
            ("client key", self.settings.client_key),
            ("CA bundle", self.settings.ca_bundle),
        ):
            if path and (not os.path.isfile(path) or not os.access(path, os.R_OK)):
                raise TransportError(
                    "tls_failed", f"pxGrid {label_name} is not readable: {path}")

    def satisfies(self, requirements):
        for requirement in requirements:
            if (
                requirement == "capability:pxgrid_endpoints"
                and requirement not in self._capabilities
            ):
                try:
                    rows = self._lookup(ENDPOINT_SERVICE)
                except TransportError as error:
                    return False, error.reason, error.detail
                self._services[ENDPOINT_SERVICE] = rows
                if any(_rest_base(row.get("properties") or {}) for row in rows):
                    self._capabilities.add(requirement)
            if requirement not in self._capabilities:
                return False, "not_configured", (
                    f"ISE did not advertise {requirement.removeprefix('capability:')}")
            if requirement == "capability:pxgrid_session_topic":
                self._ensure_stream()
                self._await_stream()
            if (
                requirement == "capability:pxgrid_session_topic"
                and not self._connected.is_set()
            ):
                error = self._stream_error
                if isinstance(error, TransportError):
                    return False, error.reason, error.detail
                # No stream error means the first connect and getSessions
                # baseline are still in flight. A production baseline can
                # outlive the bounded wait in _await_stream, and calling that
                # a connection failure counted strikes until the scheduler
                # stepped a source that was only warming over to a coarser
                # one. "Not yet" is a pending reason, not a failure.
                return False, "baseline_pending", (
                    "pxGrid session stream has not completed its baseline")
        return True, "", ""

    def _start_supervisor(self):
        if self._supervisor is not None and self._supervisor.is_alive():
            return
        self._supervisor = threading.Thread(
            target=self._supervise, name="pxgrid-stream", daemon=True)
        self._supervisor.start()

    def _supervise(self):
        backoff = 1
        while not self._stopping():
            receiver = None
            try:
                with self._state_lock:
                    self._syncing = True
                    self._buffer.clear()
                    # A new subscription starts its own sequence; the previous
                    # connection's counter is not comparable to it.
                    self._sequence.clear()
                ws = self._connect_ws()
                receiver = threading.Thread(
                    target=self._receive, args=(ws,),
                    name="pxgrid-receive", daemon=True)
                receiver.start()
                self._refresh_sessions(force=True)
                if not receiver.is_alive():
                    raise TransportError(
                        "connection_failed",
                        "pxGrid stream closed during its baseline")
                self._stream_error = None
                self._connected.set()
                telemetry.pxgrid_stream_connected.set(1)
                telemetry.pxgrid_stream_reconnects_total.inc()
                logger.info(
                    "pxGrid STREAM UP host=%s sessions=%d",
                    self.settings.host, len(self._sessions))
                backoff = 1
                receiver.join()
                if not self._stopping():
                    raise TransportError(
                        "connection_failed", "pxGrid stream connection closed")
            except Exception as error:  # noqa: BLE001 - classified for health
                self._stream_error = self._classify(error)
                if not self._stopping():
                    logger.warning(
                        "pxGrid STREAM DOWN reason=%s detail=%s; reconnecting in %ds",
                        self._stream_error.reason, self._stream_error.detail, backoff)
            finally:
                self._connected.clear()
                telemetry.pxgrid_stream_connected.set(0)
                self._close_ws()
                if receiver is not None and receiver is not threading.current_thread():
                    receiver.join(timeout=2)
            if self._wait(backoff):
                break
            backoff = min(backoff * 2, STREAM_RECONNECT_MAX_SECONDS)

    def _wait(self, seconds):
        deadline = time.monotonic() + seconds
        while not self._stopping() and time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if self._shutdown is not None:
                if self._shutdown.wait(min(1, remaining)):
                    return True
            elif self._closed.wait(min(1, remaining)):
                return True
        return self._stopping()

    @staticmethod
    def _classify(error):
        if isinstance(error, TransportError):
            return error
        if isinstance(error, ssl.SSLError):
            return TransportError("tls_failed", redact(str(error)))
        # A rejected handshake carries the response headers and body, so the
        # detail reaches a log line and a metric label only redacted.
        text = redact(str(error))
        lower = text.lower()
        if "certificate" in lower or "ssl" in lower:
            return TransportError("tls_failed", text)
        if "timed out" in lower or "timeout" in lower:
            return TransportError("timeout", text)
        if "401" in lower:
            return TransportError("authentication_failed")
        if "403" in lower:
            return TransportError("authorization_failed")
        return TransportError("connection_failed", text)

    def _pubsub(self):
        rows = self._services.get(PUBSUB_SERVICE) or self._lookup(PUBSUB_SERVICE)
        candidates = []
        for row in rows:
            peer = str(row.get("nodeName") or "").strip()
            properties = row.get("properties") or {}
            urls = properties.get("wsUrl") or properties.get("wsPubsubService")
            for url in as_list(urls):
                if peer and url:
                    candidates.append((
                        peer, _safe_wss_url(url),
                    ))
        if not candidates:
            raise TransportError(
                "not_configured", "ISE did not advertise a pxGrid pubsub URL")
        return candidates

    def _session_topic(self):
        for row in self._services.get(SESSION_SERVICE, ()):
            properties = row.get("properties") or {}
            topic = properties.get("sessionTopic") or properties.get("sessionTopicAll")
            if topic:
                return str(topic)
        raise TransportError(
            "not_configured", "ISE did not advertise a pxGrid session topic")

    def _ssl_options(self):
        options = {}
        if self.settings.client_cert:
            options["certfile"] = self.settings.client_cert
            options["keyfile"] = self.settings.client_key
        if self.settings.verify_tls:
            options["cert_reqs"] = ssl.CERT_REQUIRED
            options["check_hostname"] = True
            if self.settings.ca_bundle:
                options["ca_certs"] = self.settings.ca_bundle
        else:
            options["cert_reqs"] = ssl.CERT_NONE
            options["check_hostname"] = False
        return options

    def _connect_ws(self):
        try:
            import websocket
        except ImportError as error:  # pragma: no cover - packaging regression
            raise TransportError(
                "not_implemented",
                "websocket-client is required for pxGrid streaming") from error
        last = None
        topic = self._session_topic()
        for peer, url in self._pubsub():
            try:
                secret = self._access_secret(peer)
                token = base64.b64encode(
                    f"{self.settings.node_name}:{secret}".encode()).decode()
                ws = websocket.create_connection(
                    url, header=[f"Authorization: Basic {token}"],
                    sslopt=self._ssl_options(), timeout=max(1, self.timeout),
                    # Like Requests' trust_env=False above: appliance traffic
                    # must not leave through an ambient HTTPS proxy.
                    http_no_proxy=[urlsplit(url).hostname],
                    redirect_limit=0)
                self._ws = ws
                self._send(
                    "CONNECT\naccept-version:1.2\nheart-beat:0,0\n"
                    f"host:{self.settings.host}\n\n\x00")
                self._await_connected(ws)
                self._send(
                    f"SUBSCRIBE\nid:{topic}\ndestination:{topic}\n\n\x00")
                logger.info("pxGrid subscribed session topic %s via %s", topic, url)
                return ws
            except Exception as error:  # noqa: BLE001 - try every advertised URL
                last = error
                self._peer_secrets.pop(peer, None)
                self._close_ws()
        # Every advertised node failed, so the cached discovery may be stale;
        # the next supervisor pass re-looks it up.
        self._services.pop(PUBSUB_SERVICE, None)
        raise self._classify(last or RuntimeError("no pxGrid pubsub URL connected"))

    def _send(self, frame):
        with self._ws_lock:
            if self._ws is None:
                raise TransportError("connection_failed", "pxGrid WebSocket is closed")
            payload = frame.encode() if isinstance(frame, str) else frame
            if hasattr(self._ws, "send_binary"):
                self._ws.send_binary(payload)
            else:  # pragma: no cover - old websocket-client compatibility
                self._ws.send(payload, opcode=2)

    def _ping(self):
        with self._ws_lock:
            if self._ws is not None:
                self._ws.ping()

    def _await_connected(self, ws):
        deadline = time.monotonic() + max(1, self.timeout)
        while time.monotonic() < deadline and not self._stopping():
            raw = ws.recv()
            telemetry.pxgrid_stream_last_frame_timestamp.set(time.time())
            text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            command = text.split("\n", 1)[0].strip()
            if command == "CONNECTED":
                return
            if command == "ERROR":
                raise TransportError(
                    "authorization_failed",
                    f"pxGrid STOMP CONNECT failed: {self._stomp_body(text)[:200]}")
        raise TransportError("timeout", "pxGrid STOMP CONNECT timed out")

    def _receive(self, ws):
        try:
            import websocket
            ws.settimeout(STREAM_PING_SECONDS)
            last_ping = time.monotonic()
            while not self._stopping() and ws is self._ws:
                try:
                    opcode, raw = ws.recv_data(control_frame=True)
                except websocket.WebSocketTimeoutException:
                    opcode, raw = None, None
                now = time.monotonic()
                if now - last_ping >= STREAM_PING_SECONDS:
                    self._ping()
                    last_ping = now
                if opcode == websocket.ABNF.OPCODE_CLOSE:
                    return
                if opcode in (websocket.ABNF.OPCODE_PING, websocket.ABNF.OPCODE_PONG):
                    telemetry.pxgrid_stream_last_frame_timestamp.set(time.time())
                    continue
                if not raw:
                    continue
                telemetry.pxgrid_stream_last_frame_timestamp.set(time.time())
                body = self._stomp_body(raw)
                if not body:
                    continue
                try:
                    payload = json.loads(body)
                except (TypeError, ValueError):
                    logger.warning("dropping a non-JSON pxGrid session frame")
                    continue
                if self._sequence_gap(payload):
                    raise _SequenceGap("pxGrid session sequence gap")
                for record in self._session_events(payload):
                    self._apply_session(record)
        except Exception as error:  # noqa: BLE001 - wakes the supervisor
            self._stream_error = self._classify(error)
        finally:
            self._close_ws(ws)

    @staticmethod
    def _stomp_body(frame):
        if isinstance(frame, bytes):
            frame = frame.decode("utf-8", "replace")
        if "\n\n" not in str(frame):
            return ""
        return str(frame).split("\n\n", 1)[1].rstrip("\x00").strip()

    def _sequence_gap(self, payload):
        if not isinstance(payload, dict) or "sequence" not in payload:
            return False
        try:
            current = int(payload["sequence"])
        except (TypeError, ValueError):
            return False
        with self._state_lock:
            previous = self._sequence.get("session")
            self._sequence["session"] = current
        return previous is not None and (
            current == 0 or current != previous + 1)

    @staticmethod
    def _session_events(payload):
        if not isinstance(payload, dict):
            return []
        if "sessions" in payload:
            return [row for row in as_list(payload["sessions"], "session")
                    if isinstance(row, dict)]
        if "session" in payload:
            return [row for row in as_list(payload["session"], "session")
                    if isinstance(row, dict)]
        return [payload]

    def _apply_session(self, record):
        key = session_key(record)
        if not key:
            return
        with self._state_lock:
            if self._syncing:
                self._buffer.append(record)
            elif session_state(record) in GONE_SESSION_STATES:
                self._sessions.pop(key, None)
            else:
                self._sessions[key] = record
            telemetry.pxgrid_stream_sessions.set(len(self._sessions))

    def _close_ws(self, expected=None):
        with self._ws_lock:
            if expected is not None and self._ws is not expected:
                return
            ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001 - teardown is best effort
                pass

    # --- dataset-facing snapshots ---------------------------------------

    def _refresh_sessions(self, *, force=False):
        with self._sync_lock:
            if not force and self._session_snapshot_at:
                return
            with self._state_lock:
                self._syncing = True
                self._buffer.clear()
            try:
                data = self._query(
                    SESSION_SERVICE, "getSessions", {},
                    api="pxgrid_get_sessions",
                    max_bytes=self.config.limits.pxgrid_session_bytes)
                if not isinstance(data, (dict, list)):
                    raise TransportError(
                        "invalid_response", "pxGrid getSessions was not a collection")
                rows = as_list(
                    data.get("sessions") if isinstance(data, dict) else data,
                    "session")
                snapshot = {
                    session_key(row): row for row in rows
                    if isinstance(row, dict) and session_key(row)
                    and session_state(row) not in GONE_SESSION_STATES
                }
            except Exception:
                with self._state_lock:
                    for row in self._buffer:
                        key = session_key(row)
                        if not key:
                            continue
                        if session_state(row) in GONE_SESSION_STATES:
                            self._sessions.pop(key, None)
                        else:
                            self._sessions[key] = row
                    self._buffer.clear()
                    self._syncing = False
                raise
            with self._state_lock:
                self._sessions = snapshot
                for row in self._buffer:
                    key = session_key(row)
                    if not key:
                        continue
                    if session_state(row) in GONE_SESSION_STATES:
                        self._sessions.pop(key, None)
                    else:
                        self._sessions[key] = row
                self._buffer.clear()
                self._syncing = False
                self._session_snapshot_at = time.time()
                telemetry.pxgrid_stream_sessions.set(len(self._sessions))

    def snapshot_age(self):
        """How old the held session snapshot is, in seconds.

        Wall clock, matching ``_session_snapshot_at``. Returns ``None`` when no
        baseline has been taken, so a caller reporting the age says "unknown"
        rather than "fifty-six years".
        """
        if not self._session_snapshot_at:
            return None
        return max(0.0, time.time() - self._session_snapshot_at)

    def get_sessions(self, *, max_age=300, refresh=True):
        """The held sessions, reconciling first if the baseline has aged out.

        ``refresh=False`` asks for whatever is held and nothing more. That is
        not the same as a very large ``max_age``: a reconcile is a full
        getSessions against the appliance, and a caller that must never cause
        one -- the operator API, where a page reload would otherwise schedule
        it -- has to be able to say so rather than pick a number big enough
        that it usually does not happen. The pubsub subscription keeps the held
        set current between baselines, so what comes back is stale only in the
        sense that a session lost with the stream has not been noticed yet.
        """
        if not self._connected.is_set():
            error = self._stream_error
            raise error if isinstance(error, TransportError) else TransportError(
                "connection_failed", "pxGrid session stream is not connected")
        if refresh and time.time() - self._session_snapshot_at >= max(1, max_age):
            self._refresh_sessions(force=True)
        with self._state_lock:
            return [dict(row) for row in self._sessions.values()]

    def get_endpoints(self, *, max_age=21600):
        if (
            self._endpoint_snapshot_at
            and time.time() - self._endpoint_snapshot_at < max(1, max_age)
        ):
            with self._state_lock:
                return [dict(row) for row in self._endpoints]

        rows, seen, start, pages = [], set(), 0, 0
        max_pages = max(1, MAX_ENDPOINTS // max(1, ENDPOINT_PAGE_SIZE))
        while True:
            data = self._query(
                ENDPOINT_SERVICE, "getEndpoints", {
                    "startCreateTimestamp": ENDPOINT_BULK_START,
                    "startIndex": start,
                    "count": ENDPOINT_PAGE_SIZE,
                    "order": "ASC",
                }, api="pxgrid_get_endpoints")
            page = as_list(
                (data.get("endpoints") or data.get("endpoint"))
                if isinstance(data, dict) else data,
                "endpoint")
            if any(not isinstance(row, dict) for row in page):
                raise TransportError(
                    "invalid_response", "pxGrid getEndpoints contained a non-object")
            fresh = [row for row in page if _endpoint_identity(row) not in seen]
            seen.update(_endpoint_identity(row) for row in fresh)
            if page and not fresh:
                logger.warning(
                    "pxGrid getEndpoints repeated its page at startIndex=%d "
                    "(%d rows)", start, len(page))
                raise TransportError(
                    "invalid_response",
                    "pxGrid getEndpoints ignored startIndex; the same page "
                    f"repeated at startIndex={start}")
            rows.extend(fresh)
            if len(rows) > MAX_ENDPOINTS:
                raise TransportError(
                    "response_too_large",
                    f"pxGrid endpoint directory exceeds {MAX_ENDPOINTS} records")
            if len(page) < ENDPOINT_PAGE_SIZE:
                break
            pages += 1
            if pages >= max_pages:
                raise TransportError(
                    "response_too_large",
                    f"pxGrid getEndpoints did not finish within {max_pages} pages")
            start += len(page)
        with self._state_lock:
            self._endpoints = [dict(row) for row in rows]
            self._endpoint_snapshot_at = time.time()
            return [dict(row) for row in self._endpoints]
