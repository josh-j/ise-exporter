"""ERS / PAN OpenAPI / MnT HTTPS transport.

Ported from v2's ``clients/rest.py``, which was sound; the changes are structural
rather than behavioural:

- one target per instance instead of a client spanning two planes;
- always raise ``TransportError`` instead of the ``propagate_failures`` toggle
  that made every call site handle both ``None`` and an exception;
- count each wire attempt into the measured-load counter.

Behaviours deliberately preserved: urllib3 retries are disabled so one scheduled
request is exactly one wire attempt and one telemetry observation (retries
beneath the exporter hide appliance pressure and multiply shutdown latency);
``trust_env`` is off so ambient REQUESTS_CA_BUNDLE cannot silently override the
configured trust policy; the configured timeout is split across connect and read
phases rather than allowed once per phase; response bodies are size-capped while
streaming; and error snippets are redacted before they reach a log.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import warnings
from urllib.parse import urlsplit

import xml.etree.ElementTree as ET

import requests
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth
from urllib3.exceptions import InsecureRequestWarning, ReadTimeoutError
from urllib3.util.retry import Retry

from .. import telemetry
from ..auth_guard import PersistentAuthGuard
from ..rate_limit import BudgetLimiter
from . import Transport, TransportError, guard_path


logger = logging.getLogger(__name__)

ERS_MAX_PAGES = 2000
ERS_MAX_ROWS = 200_000
# A total-only response retains one integer, not that many rows. Keep a sanity
# bound without inheriting the much smaller enumeration-memory ceiling.
ERS_MAX_TOTAL = 10_000_000
PAN_MAX_PAGES = 100
PAN_MAX_ROWS = 100_000
HTTP_READ_CHUNK_BYTES = 64 * 1024
MAX_HTTP_RESPONSE_BYTES = 64 * 1024 * 1024
HTTP_ERROR_SNIPPET_BYTES = 200
# Redaction runs over a wider window than the logged snippet, so a secret whose
# closing quote or tag falls past the snippet boundary is still matched before
# the text is cut down to the logged length.
HTTP_ERROR_SCAN_BYTES = 4 * HTTP_ERROR_SNIPPET_BYTES
# A body read resets its socket timeout on every chunk, so a drip-feeding peer
# can hold the transport lock indefinitely. Cap the whole read in wall time at a
# small multiple of the configured budget.
BODY_READ_TIMEOUT_FACTOR = 4
# MnT hands back a document rather than a record set, so it needs its own
# ceilings. 250k records covers a deployment far larger than supported; the
# element ceiling bounds a document that is wide rather than long.
MAX_XML_RECORDS = 250_000
MAX_XML_ELEMENTS = 2_000_000
MAX_XML_DEPTH = 64
UNSAFE_XML = re.compile(rb"<!DOCTYPE|<!ENTITY", re.IGNORECASE)

AUTH_FAILURE_THRESHOLD = 3
AUTH_FAILURE_BACKOFF_SECONDS = 900

_SECRET_NAMES = "password|passwd|passphrase|secret|token|authorization|cookie"
# An unquoted or unterminated value counts: a snippet is a truncated body, so a
# value whose closing quote or tag was cut off must still be redacted.
_SENSITIVE_JSON = re.compile(
    r'(?i)(["\']?(?:' + _SECRET_NAMES + r')["\']?\s*:\s*)'
    r'(?:["\'][^"\']*(?:["\']|$)|[^,}\s]+)')
_SENSITIVE_PAIR = re.compile(
    r'(?i)(\b(?:' + _SECRET_NAMES + r')\s*=)[^&\s,;]+')
_SENSITIVE_XML = re.compile(
    r'(?i)(<(?:[\w.-]+:)?(?:' + _SECRET_NAMES + r')\b[^>]*>)'
    r'.*?(</(?:[\w.-]+:)?(?:' + _SECRET_NAMES + r')>|$)')
# Basic in particular: it is the credential this transport itself sends, and a
# proxy or WAF block page routinely echoes the request headers back.
_SENSITIVE_SCHEME = re.compile(
    r'(?i)(\b(?:Bearer|Basic|Digest|Negotiate|NTLM)\s+)[A-Za-z0-9._~+/=-]+')


class ResponseTooLarge(RuntimeError):
    """The remote API tried to return more data than this process retains."""


def redact(value):
    text = str(value or "")
    # Scheme first: once a JSON substitution has eaten the scheme keyword the
    # credential after it would no longer match.
    text = _SENSITIVE_SCHEME.sub(r"\1<redacted>", text)
    text = _SENSITIVE_JSON.sub(r'\1"<redacted>"', text)
    text = _SENSITIVE_PAIR.sub(r"\1<redacted>", text)
    return _SENSITIVE_XML.sub(r"\1<redacted>\2", text)


def _is_read_timeout(error):
    """Whether a requests ConnectionError is really a read timeout underneath."""
    candidates = [error.__cause__, error.__context__]
    candidates.extend(getattr(error, "args", ()))
    return any(isinstance(item, ReadTimeoutError) for item in candidates)


def close_response(response):
    close = getattr(response, "close", None)
    if callable(close):
        close()


def error_snippet(response):
    """Read a log-sized prefix of an error without retaining its body."""
    iterator = getattr(response, "iter_content", None)
    try:
        if callable(iterator):
            body = bytearray()
            for chunk in iterator(chunk_size=HTTP_ERROR_SNIPPET_BYTES):
                if chunk:
                    body.extend(chunk[:HTTP_ERROR_SCAN_BYTES - len(body)])
                if len(body) >= HTTP_ERROR_SCAN_BYTES:
                    break
            raw = bytes(body)
        else:
            content = getattr(response, "content", None)
            if content is None:
                content = str(getattr(response, "text", "") or "").encode(
                    "utf-8", "replace")
            raw = bytes(content)[:HTTP_ERROR_SCAN_BYTES]
    except Exception:  # noqa: BLE001 - a broken body must not lose the status
        raw = b""
    finally:
        close_response(response)
    text = raw.decode("utf-8", "replace").replace("\n", " ").replace("\r", " ")
    # Redact the wide window, then cut: cutting first would strand a secret's
    # terminator outside the text the patterns can see.
    return redact(text)[:HTTP_ERROR_SNIPPET_BYTES]


def split_timeout(total):
    """Split one configured wall budget across the connect and read phases.

    A scalar Requests timeout permits the full value once per phase, so a 30s
    budget can take 60s. This keeps the sum equal to what was configured.
    """
    try:
        total = float(total)
    except (TypeError, ValueError):
        total = 30.0
    total = max(1.0, min(30.0, total))
    connect = min(5.0, total / 2)
    return connect, total - connect


class RestTransport(Transport):
    """HTTPS transport for one ISE persona."""

    def __init__(self, config, target):
        settings = config.targets[target]
        credentials = config.targets["pan"]      # MnT authenticates as the PAN account
        self.target = target
        self.settings = settings
        self.timeout = settings.request_timeout
        self.ers_url = f"https://{settings.host}:{settings.ers_port}/ers"
        self.openapi_url = f"https://{settings.host}/api/v1"
        self.mnt_url = f"https://{settings.host}/admin/API/mnt"
        self._lock = threading.RLock()
        self._shutdown = None
        # The declared budget, enforced rather than merely measured. Before this
        # the plan checked that the steady state fitted and nothing stopped a
        # cold start from spending 2.8x the ceiling for its first two hours.
        # `exporter.enforce_budget = false` turns it back into an observation,
        # which is the same thing that flag already means for a plan over budget.
        self.limiter = BudgetLimiter(
            target, config.budget_for(target),
            enforce=config.exporter.enforce_budget)
        self._guard = PersistentAuthGuard(
            guard_path(config, "rest"),
            (credentials.user, config.targets["pan"].host, config.targets["mnt"].host),
            "REST authentication",
        )
        self.session = self._build_session(
            HTTPBasicAuth(credentials.user, credentials.password),
            "application/xml" if target == "mnt" else "application/json",
            self._verify(settings))

    @staticmethod
    def _verify(settings):
        if not settings.verify_tls:
            return False
        return (settings.ca_bundle or "").strip() or True

    @staticmethod
    def _build_session(auth, content_type, verify):
        session = requests.Session()
        session.auth = auth
        session.verify = verify
        # Explicit configuration, not ambient REQUESTS_CA_BUNDLE state, owns
        # trust policy. Nix in particular sets that variable process-wide.
        session.trust_env = False
        # read=False, not read=0: a count of zero is still spent by urllib3 and
        # surfaces a read timeout as MaxRetryError -> ConnectionError, so a slow
        # node is reported as unreachable. False re-raises the ReadTimeoutError,
        # which requests maps to ReadTimeout. Retries stay disabled either way.
        session.mount("https://", HTTPAdapter(max_retries=Retry(
            total=0, connect=0, read=False, status=0, other=0, redirect=0,
            allowed_methods=frozenset({"GET"}), backoff_factor=0,
            respect_retry_after_header=False, raise_on_status=False)))
        # Accept-only: a Content-Type on a GET is non-standard and trips WAFs.
        session.headers.update({"Accept": content_type})
        return session

    def set_shutdown_event(self, shutdown):
        self._shutdown = shutdown
        self.limiter.set_shutdown_event(shutdown)

    def set_warming(self, warming):
        """Whether a converging cache on this target still has work to do.

        Set by the scheduler from the ``deferred`` count a collection reports,
        because that is the only place that knows. Without a declared warm-up
        burst this changes nothing.
        """
        self.limiter.set_warming(warming)

    def close(self):
        # Deliberately not under the lock: an in-flight request holds it for the
        # whole body read, and closing the session is what unblocks a socket
        # that is still being drip-fed. The stalled read then fails as a
        # connection error, which _request_locked already classifies.
        session, self.session = self.session, None
        if session is not None:
            session.close()

    # --- request plumbing -------------------------------------------------

    def _request(self, url, params=None, api="unknown"):
        with self._lock:
            return self._request_locked(url, params, api)

    def _count(self, api, status):
        telemetry.api_requests_total.labels(
            target=self.target, api=api, status=status).inc()

    def _error(self, api, error_type, http_code="0"):
        telemetry.api_errors_total.labels(
            target=self.target, api=api, error_type=error_type,
            http_code=str(http_code)).inc()

    def _request_locked(self, url, params, api):
        if self._shutdown is not None and self._shutdown.is_set():
            raise TransportError("unexpected_error", "Shutting down")
        if self.session is None:
            raise TransportError("unexpected_error", "The transport is closed")

        try:
            blocked = self._guard.blocked(time.time())
        except Exception as error:
            self._count(api, "auth_guard_error")
            self._error(api, "auth_guard")
            raise TransportError(
                "state_unavailable",
                "The shared REST authentication guard is unavailable") from error
        if blocked:
            self._count(api, "auth_blocked")
            self._error(api, "auth_blocked", 401)
            raise TransportError("authentication_backoff")

        # Wait for budget before the wire attempt, and after the auth guard: a
        # request the guard is going to refuse should not first consume an
        # hour's worth of allowance waiting for a slot it will not use.
        self.limiter.acquire(api)
        if self._shutdown is not None and self._shutdown.is_set():
            raise TransportError("unexpected_error", "Shutting down")

        # Every attempt past this point reaches the wire, so it is what the
        # measured-load counter must reflect.
        telemetry.load_measured_requests_total.labels(target=self.target).inc()
        started = time.monotonic()
        budget = split_timeout(self.timeout)
        deadline = started + sum(budget) * BODY_READ_TIMEOUT_FACTOR
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", InsecureRequestWarning)
                response = self.session.get(
                    url, params=params, timeout=budget,
                    stream=True, allow_redirects=False)
            status = int(getattr(response, "status_code", 200))
            if not 200 <= status < 300:
                self._raise_http(response, url, status, api)
            self._buffer(response, deadline=deadline, shutdown=self._shutdown)
            self._count(api, "success")
            return response
        except TransportError:
            raise
        except ResponseTooLarge as error:
            logger.warning("oversized response for %s: %s", url, error)
            self._count(api, "response_too_large")
            self._error(api, "response_too_large")
            raise TransportError("response_too_large") from error
        except requests.exceptions.Timeout as error:
            logger.warning("timeout for %s", url)
            self._count(api, "timeout")
            self._error(api, "timeout")
            raise TransportError("timeout") from error
        except requests.exceptions.SSLError as error:
            logger.warning("TLS error for %s: %s", url, error)
            self._count(api, "tls_error")
            self._error(api, "tls_error")
            raise TransportError("tls_failed") from error
        except requests.exceptions.ConnectionError as error:
            logger.warning("connection error for %s: %s", url, error)
            self._count(api, "connection_error")
            self._error(api, "connection_error")
            raise TransportError(
                "connection_failed",
                f"{self.settings.host} could not be reached") from error
        except Exception as error:
            logger.error("request failed for %s: %s", url, error)
            self._count(api, "error")
            self._error(api, "unknown")
            raise TransportError("unexpected_error") from error
        finally:
            telemetry.api_request_duration_seconds.labels(
                target=self.target, api=api).observe(
                    max(0.0, time.monotonic() - started))

    def _raise_http(self, response, url, status, api):
        snippet = self._error_snippet(response)
        logger.warning("HTTP %s for %s  body: %s", status, url, snippet)
        if status == 401:
            self._record_auth_failure(api)
        self._count(api, f"http_{status}")
        self._error(api, "http_error", status)
        if status == 401:
            raise TransportError("authentication_failed")
        if status == 403:
            raise TransportError("authorization_failed")
        raise TransportError(
            "http_error", f"ISE returned HTTP {status} for {api}")

    @staticmethod
    def _close(response):
        close_response(response)

    @classmethod
    def _buffer(cls, response, deadline=None, shutdown=None):
        """Retain a successful body only when it fits the hard ceiling.

        ``deadline`` bounds the read in wall time and ``shutdown`` lets it be
        abandoned: the per-socket read timeout restarts on every chunk, so
        neither the byte cap nor the timeout stops a drip-feeding peer.
        """
        raw = str(getattr(response, "headers", {}).get("Content-Length", "") or "").strip()
        try:
            declared = int(raw)
        except (TypeError, ValueError):
            declared = None
        if declared is not None and declared > MAX_HTTP_RESPONSE_BYTES:
            cls._close(response)
            raise ResponseTooLarge(
                f"Content-Length {declared} exceeds {MAX_HTTP_RESPONSE_BYTES} bytes")

        iterator = getattr(response, "iter_content", None)
        if not callable(iterator):
            body = bytes(getattr(response, "content", b"") or b"")
            if len(body) > MAX_HTTP_RESPONSE_BYTES:
                cls._close(response)
                raise ResponseTooLarge(f"body exceeds {MAX_HTTP_RESPONSE_BYTES} bytes")
        else:
            retained = bytearray()
            try:
                for chunk in iterator(chunk_size=HTTP_READ_CHUNK_BYTES):
                    if deadline is not None and time.monotonic() > deadline:
                        raise requests.exceptions.ReadTimeout(
                            "the response body exceeded its read deadline")
                    if shutdown is not None and shutdown.is_set():
                        raise TransportError("unexpected_error", "Shutting down")
                    if not chunk:
                        continue
                    if len(retained) + len(chunk) > MAX_HTTP_RESPONSE_BYTES:
                        raise ResponseTooLarge(
                            f"streamed body exceeds {MAX_HTTP_RESPONSE_BYTES} bytes")
                    retained.extend(chunk)
                body = bytes(retained)
            except requests.exceptions.ConnectionError as error:
                # requests wraps a body-phase read timeout as ConnectionError,
                # which would be published as "unreachable" rather than a
                # timeout. Restore the classification.
                if _is_read_timeout(error):
                    raise requests.exceptions.ReadTimeout(str(error)) from error
                raise
            finally:
                cls._close(response)
        response._content = body
        response._content_consumed = True

    @classmethod
    def _error_snippet(cls, response):
        return error_snippet(response)

    def _record_auth_failure(self, api):
        try:
            failures, deadline = self._guard.failure(
                AUTH_FAILURE_THRESHOLD, AUTH_FAILURE_BACKOFF_SECONDS, time.time())
        except Exception as error:
            self._error(api, "auth_guard")
            raise TransportError(
                "state_unavailable",
                "The REST authentication guard could not record a failure") from error
        if deadline:
            logger.error(
                "ISE rejected these credentials %d times; pausing requests for %ds "
                "to avoid an account lockout", failures, AUTH_FAILURE_BACKOFF_SECONDS)

    def _record_auth_success(self, api):
        try:
            self._guard.success()
        except Exception as error:
            self._error(api, "auth_guard")
            raise TransportError(
                "state_unavailable",
                "The REST authentication guard could not record success") from error

    def _json(self, url, params=None, api="unknown"):
        response = self._request(url, params, api)
        try:
            data = response.json()
        except (RecursionError, ValueError) as error:
            logger.warning("invalid JSON from %s: %s", url, error)
            self._error(api, "parse", getattr(response, "status_code", 0) or 0)
            raise TransportError(
                "invalid_response", f"ISE returned invalid JSON for {api}") from error
        self._record_auth_success(api)
        return data

    def _malformed(self, api, message, *args):
        logger.warning(message, *args)
        self._error(api, "protocol", 200)
        raise TransportError(
            "invalid_response", "ISE returned a malformed or incomplete response")

    # --- OpenAPI ----------------------------------------------------------

    def get_openapi(self, path, *, api="openapi", unwrap=True, params=None):
        """GET one PAN OpenAPI resource, unwrapping the `response` envelope."""
        data = self._json(f"{self.openapi_url}{path}", params, api)
        return data.get("response", data) if unwrap and isinstance(data, dict) else data

    def get_openapi_all(self, path, *, api="openapi", params=None,
                        max_pages=PAN_MAX_PAGES, max_rows=10_000):
        """Enumerate a paginated OpenAPI list fail-closed.

        OpenAPI pagination carries no total, so completeness means following
        every valid nextPage href until the server omits it. The href's host is
        ignored and each page is rebuilt under the configured origin, so a
        rogue link cannot redirect credentials to another host.
        """
        if not 1 <= max_pages <= PAN_MAX_PAGES or not 1 <= max_rows <= PAN_MAX_ROWS:
            raise ValueError("invalid OpenAPI pagination bound")
        url = f"{self.openapi_url}{path}"
        data = self._json(url, params, api)
        rows, visited, pages = [], set(), 0
        resource = path.lstrip("/").split("?", 1)[0]
        while True:
            pages += 1
            if (not isinstance(data, dict) or not isinstance(data.get("response"), list)):
                self._malformed(api, "malformed OpenAPI page envelope for %s", url)
            page = data["response"]
            if len(rows) + len(page) > max_rows:
                self._malformed(
                    api, "OpenAPI pagination exceeded %d rows for %s", max_rows, url)
            rows.extend(page)
            next_page = data.get("nextPage")
            if next_page is None:
                return rows
            if pages >= max_pages:
                self._malformed(
                    api, "OpenAPI pagination exceeded %d pages for %s", max_pages, url)
            if not isinstance(next_page, dict):
                self._malformed(api, "malformed OpenAPI nextPage for %s", url)
            href = next_page.get("href", "")
            if not isinstance(href, str) or "/api/v1/" not in href or href in visited:
                self._malformed(api, "invalid OpenAPI nextPage href for %s: %r", url, href)
            visited.add(href)
            following = href.split("/api/v1/", 1)[1]
            if following.split("?", 1)[0] != resource:
                self._malformed(
                    api, "OpenAPI nextPage changed resource for %s: %r", url, href)
            url = f"{self.openapi_url}/{following}"
            data = self._json(url, api=api)

    # --- ERS --------------------------------------------------------------

    def get_ers(self, path, *, params=None, get_all=False, api="ers"):
        """GET an ERS resource list, following nextPage when ``get_all``.

        A failed or malformed page invalidates the whole enumeration: returning
        a partial list would let a dataset publish a plausible but wrong
        inventory. A genuinely empty result stays the distinct value ``[]``.
        """
        url = f"{self.ers_url}{path}"
        data = self._json(url, params, api)
        if not isinstance(data, dict):
            self._malformed(api, "malformed ERS envelope for %s", url)
        if "SearchResult" not in data:
            if get_all:
                self._malformed(api, "missing ERS SearchResult for %s", url)
            return data

        result = data["SearchResult"]
        if not isinstance(result, dict) or not isinstance(result.get("resources", []), list):
            self._malformed(api, "malformed ERS SearchResult for %s", url)
        resources = list(result.get("resources", []))
        if len(resources) > ERS_MAX_ROWS:
            self._malformed(api, "ERS response exceeded %d rows for %s", ERS_MAX_ROWS, url)

        expected = result.get("total")
        if get_all:
            try:
                expected = int(expected)
            except (TypeError, ValueError):
                self._malformed(api, "malformed ERS total for %s: %r", url, expected)
            if not 0 <= expected <= ERS_MAX_ROWS:
                self._malformed(api, "ERS total out of bounds for %s: %r", url, expected)

        visited, pages = set(), 1
        resource = path.split("?", 1)[0]
        while get_all:
            next_page = result.get("nextPage")
            if next_page is None:
                break
            if pages >= ERS_MAX_PAGES:
                self._malformed(
                    api, "ERS pagination exceeded %d pages for %s", ERS_MAX_PAGES, url)
            if not isinstance(next_page, dict):
                self._malformed(api, "malformed ERS nextPage for %s", url)
            href = next_page.get("href", "")
            if not isinstance(href, str) or href in visited:
                self._malformed(api, "invalid ERS nextPage href for %s: %r", url, href)
            # Match on the path, not the whole URL: "//" before a host beginning
            # with "ers" puts the first "/ers" inside the authority, which used
            # to strand every paginated dataset on such a node.
            try:
                parts = urlsplit(href)
            except ValueError:
                self._malformed(api, "invalid ERS nextPage href for %s: %r", url, href)
            if not parts.path.startswith("/ers"):
                self._malformed(api, "invalid ERS nextPage href for %s: %r", url, href)
            visited.add(href)
            if parts.path[4:] != resource:
                self._malformed(
                    api, "ERS nextPage changed resource for %s: %r", url, href)
            following = parts.path[4:] + (f"?{parts.query}" if parts.query else "")
            page = self._json(f"{self.ers_url}{following}", api=api)
            if not isinstance(page, dict):
                self._malformed(api, "malformed ERS page envelope for %s", href)
            result = page.get("SearchResult")
            if not isinstance(result, dict) or not isinstance(
                    result.get("resources", []), list):
                self._malformed(api, "malformed ERS page for %s", href)
            page_rows = result.get("resources", [])
            if len(resources) + len(page_rows) > ERS_MAX_ROWS:
                self._malformed(
                    api, "ERS pagination exceeded %d rows for %s", ERS_MAX_ROWS, url)
            resources.extend(page_rows)
            pages += 1

        if get_all and len(resources) != expected:
            self._malformed(
                api, "incomplete ERS pagination for %s: got %d of %d rows",
                url, len(resources), expected)
        return resources

    # --- MnT XML ----------------------------------------------------------

    def get_mnt_xml(self, path, *, api="mnt_xml"):
        """GET one MnT XML resource and return it as bounded plain data.

        Parsed with an explicit element, record and depth ceiling that bound the
        allocation as it happens, and with DOCTYPE and ENTITY refused outright:
        this is the one interface that hands the exporter a document rather than
        a record set, and an unbounded or entity-expanding document is the
        obvious way to hurt it.

        Returns ``{"total": int, "sessions": [ {tag: text}, ... ]}`` for
        ActiveList-shaped responses, or ``{"total": 0, "sessions": [record]}``
        for a single-record response such as Session/MACAddress.
        """
        response = self._request(f"{self.mnt_url}{path}", None, api)
        body = getattr(response, "content", b"") or b""
        if not body:
            raise TransportError("invalid_response", f"MnT returned no body for {api}")
        # The whole body, not a prefix: XML allows unbounded comments, PIs and
        # whitespace before the doctypedecl, so a padded prolog would otherwise
        # carry a DOCTYPE straight past the guard.
        if UNSAFE_XML.search(body):
            self._error(api, "unsafe_xml", 200)
            raise TransportError(
                "invalid_response", "MnT response declared a DOCTYPE or ENTITY")
        try:
            root, records = self._parse_mnt(body)
        except ET.ParseError as error:
            self._error(api, "parse", 200)
            raise TransportError(
                "invalid_response", f"MnT returned unparseable XML for {api}") from error
        if root is None:
            self._error(api, "parse", 200)
            raise TransportError(
                "invalid_response", f"MnT returned unparseable XML for {api}")
        self._record_auth_success(api)

        try:
            total = int(root.attrib.get("noOfActiveSession", 0))
        except (TypeError, ValueError):
            total = 0

        if not records:
            # A single-record response (Session/MACAddress) has its fields as
            # direct children of the root rather than inside activeSession.
            record = self._mnt_record(root)
            if record:
                records.append(record)

        return {"total": total, "sessions": records}

    @staticmethod
    def _mnt_record(item):
        record = {}
        for child in item:
            if child.text and child.text.strip():
                record[child.tag.split("}")[-1]] = child.text.strip()
        return record

    @classmethod
    def _parse_mnt(cls, body):
        """Pull-parse MnT XML, enforcing the ceilings during the parse.

        A materialised tree costs ~20x its serialised size, so a ceiling tested
        after ``fromstring`` bounds nothing. Extracted records are detached from
        the tree as they complete, keeping retained memory proportional to what
        is actually returned rather than to the document.
        """
        parser = ET.XMLPullParser(events=("start", "end"))
        root, records, stack, elements = None, [], [], 0
        for offset in range(0, len(body), HTTP_READ_CHUNK_BYTES):
            parser.feed(body[offset:offset + HTTP_READ_CHUNK_BYTES])
            for event, item in parser.read_events():
                if event == "start":
                    elements += 1
                    if elements > MAX_XML_ELEMENTS:
                        raise TransportError(
                            "response_too_large",
                            "MnT response exceeded its element ceiling")
                    if len(stack) >= MAX_XML_DEPTH:
                        raise TransportError(
                            "response_too_large",
                            "MnT response exceeded its depth ceiling")
                    stack.append(item)
                    if root is None:
                        root = item
                    continue
                if stack:
                    stack.pop()
                if item.tag.split("}")[-1] != "activeSession":
                    continue
                if len(records) >= MAX_XML_RECORDS:
                    raise TransportError(
                        "response_too_large",
                        f"MnT returned more than {MAX_XML_RECORDS} records")
                records.append(cls._mnt_record(item))
                item.clear()
                if stack:
                    stack[-1].remove(item)
        parser.close()
        return root, records

    def get_ers_total(self, path, *, params=None, api="ers"):
        """Return SearchResult.total without enumerating the resource."""
        url = f"{self.ers_url}{path}"
        query = dict(params or {})
        query.setdefault("size", 1)
        data = self._json(url, query, api)
        if not isinstance(data, dict) or not isinstance(data.get("SearchResult"), dict):
            self._malformed(api, "malformed ERS total response for %s", url)
        total = data["SearchResult"].get("total")
        try:
            total = int(total)
        except (TypeError, ValueError):
            self._malformed(api, "malformed ERS total for %s: %r", url, total)
        if not 0 <= total <= ERS_MAX_TOTAL:
            self._malformed(api, "ERS total out of bounds for %s: %d", url, total)
        return total
