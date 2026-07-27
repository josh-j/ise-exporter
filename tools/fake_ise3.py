"""A synthetic ISE deployment, sized to production, for scale simulation.

This is the appliance half of `tools/simulate_scale3.py`. It answers the exact
interfaces v3 collects from, so the exporter's own code -- paging, XML parsing,
response ceilings, the Data Connect pacing gate, the detail caches, the
scheduler and the publication boundary -- runs unmodified against a fleet the
lab does not have.

Three seams, in descending order of fidelity:

1. **PAN and MnT are real HTTP.** A TLS listener serves ERS, the PAN OpenAPI and
   the MnT XML API, so `transports/rest.py` does its real paging, its real
   `SearchResult` completeness check and its real bounded XML parse over an
   8 MB ActiveList. Only the three base URLs are rewritten, to reach this
   process instead of an appliance.

2. **Oracle is a fake cursor under the real transport.** `DataConnectTransport`
   is subclassed, not replaced: the duty-cycle gate, the adaptive cooldown, the
   batch lease, the row/byte ceilings, the statement timeout and the telemetry
   are all the shipped code. Only `oracledb`'s connection is synthetic, and it
   synthesises result sets by reading the statement's own SELECT list, so a
   dataset gets the columns it asked for at the row count its GROUP BY implies.

3. **Time is virtual.** Modelled appliance latency (a declared assumption, see
   `Latency`) advances a shared clock instead of sleeping. The scheduler, the
   Data Connect cooldowns and the statement timeouts all read that clock, so a
   24-hour soak with real 2%-duty-cycle pacing runs in minutes and still
   throttles, starves and times out where the real thing would.

What is *not* simulated: ISE's own semantics. Aggregate values are plausible,
not meaningful. This measures cost, cardinality, convergence and pacing -- never
whether a number is correct.
"""
from __future__ import annotations

import http.server
import json
import re
import ssl
import subprocess
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from ise_exporter3.compatibility import SUPPORTED_ISE_VERSION, SUPPORTED_PATCH_LEVEL
from ise_exporter3.transports.dataconnect import DataConnectTransport
from ise_exporter3.transports.rest import RestTransport


# --------------------------------------------------------------------------
# Virtual time
# --------------------------------------------------------------------------

class VirtualClock:
    """One timeline shared by the scheduler, the transports and this fake ISE.

    Everything that would have been a sleep -- appliance latency, the Data
    Connect cooldown, the minimum inter-query interval -- advances this instead.
    """

    def __init__(self, start=None):
        self._now = float(time.time() if start is None else start)
        self._lock = threading.Lock()
        self.slept = 0.0
        self.charged = defaultdict(float)

    def time(self):
        return self._now

    def advance(self, seconds, reason=""):
        value = float(seconds or 0.0)
        if value <= 0:
            return self._now
        with self._lock:
            self._now += value
            if reason:
                self.charged[reason] += value
            return self._now

    def sleep(self, seconds):
        value = float(seconds or 0.0)
        if value > 0:
            self.slept += value
            self.advance(value, "pacing")


class LaneClock:
    """One target's own timeline, running ahead of the shared clock by its debt.

    Production runs each target on its own thread, so a Data Connect cooldown
    does not stall the MnT lane. A single simulated timeline would serialise
    them and make every other lane look slower than it is. So Oracle pacing
    accumulates here instead: the shared clock advances only for work actually
    done, and this lane's accrued wait becomes a measurement -- pacing debt. A
    lane whose debt keeps growing cannot sustain its configured cadences, which
    is exactly the starvation signal worth reporting.
    """

    def __init__(self, clock):
        self._clock = clock
        self._lane_now = clock.time()
        self.waited = 0.0

    @property
    def debt(self):
        """How far this lane is behind wall clock, in seconds.

        Zero means its pacing fits inside the gaps between collections. A debt
        that grows means the configured cadences ask for more of this target
        than the budget can pace, and the lane will fall behind.
        """
        return max(0.0, self._lane_now - self._clock.time())

    def time(self):
        return max(self._clock.time(), self._lane_now)

    def advance(self, seconds, reason=""):
        self._clock.advance(seconds, reason)
        self._lane_now = self.time()
        return self._lane_now

    def sleep(self, seconds):
        value = float(seconds or 0.0)
        if value > 0:
            self.waited += value
            self._clock.slept += value
            self._lane_now = self.time() + value


class VirtualTime:
    """Stands in for the `time` module inside the Data Connect transport."""

    def __init__(self, clock):
        self._clock = clock

    def time(self):
        return self._clock.time()

    def monotonic(self):
        return self._clock.time()

    def perf_counter(self):
        return self._clock.time()

    def sleep(self, seconds):
        self._clock.sleep(seconds)


# --------------------------------------------------------------------------
# Declared latency assumptions
# --------------------------------------------------------------------------

class Latency:
    """How long the modelled appliance takes to answer, in seconds.

    These are assumptions, not measurements, and they are the least defensible
    numbers in the simulation -- so they are declared in one place, printed in
    the report, and scalable from the command line. They are sized from what ISE
    3.3 does on an SNS-class node: ERS serialises and pages 100 rows at a time,
    the MnT XML API is slow and gets slower with the active list, and Data
    Connect is an Oracle reporting view competing with ISE's own reporting.
    """

    def __init__(self, scale=1.0):
        self.scale = float(scale)

    def _s(self, value):
        return value * self.scale

    def ers_page(self):
        return self._s(0.25)

    def ers_detail(self):
        return self._s(0.12)

    def openapi(self, rows=0):
        return self._s(0.30 + 0.002 * rows)

    def mnt_active_list(self, sessions):
        return self._s(2.0 + 0.00012 * sessions)

    def mnt_detail(self):
        return self._s(0.15)

    # Oracle: a bounded aggregate over a reporting view. The base is the scan,
    # which is what actually costs, plus a small per-row materialisation cost.
    def oracle_query(self, view, rows):
        base = {
            "radius_accounting": 9.0,
            "radius_authentications": 7.0,
            "radius_authentications_week": 9.0,
            "radius_authentication_summary": 3.0,
            "radius_errors_view": 4.0,
            "posture_assessment_by_endpoint": 3.5,
            "posture_assessment_by_condition": 3.5,
            "tacacs_accounting_last_two_days": 2.5,
            "tacacs_authentication_last_two_days": 2.5,
            "endpoints_data": 3.0,
            "key_performance_metrics": 1.2,
            "system_summary": 1.0,
            "profiled_endpoints_summary": 1.5,
            "schema_metadata": 0.4,
            "freshness_probe": 0.8,
        }.get(view, 2.0)
        return self._s(base + 0.0004 * rows)


# --------------------------------------------------------------------------
# The estate
# --------------------------------------------------------------------------

ENDPOINT_PROFILES = (
    "Windows10-Workstation", "Windows11-Workstation", "Microsoft-Workstation",
    "Linux-Workstation", "Apple-MacBook", "Apple-iPhone", "Apple-iPad",
    "Android-Device", "Cisco-IP-Phone-8845", "Cisco-IP-Phone-7841",
    "Cisco-Access-Point", "HP-Printer", "Canon-Printer", "Zebra-Scanner",
    "VMWare-Device", "Axis-Camera", "Crestron-Device", "Unknown",
)
AUTHZ_PROFILES = (
    "PermitAccess", "Corp-Full-Access", "Corp-Limited", "Guest-Internet",
    "Voice-Access", "Printer-MAB", "Camera-MAB", "Quarantine", "DenyAccess",
    "Remediation",
)
POLICY_SETS = (
    "Corporate-Wired", "Corporate-Wireless", "Guest-Wireless", "Voice",
    "IoT-MAB", "VPN-RemoteAccess", "Device-Admin",
)
COMMAND_SETS = (
    "Permit-Show", "Permit-Interface", "Permit-Operations", "Deny-Dangerous",
)
SHELL_PROFILES = (
    "IOS-Admin", "IOS-ReadOnly", "NXOS-Admin", "Firewall-Admin",
)
AUTHZ_RULES = (
    "Corp-Compliant-Full", "Corp-NonCompliant-Remediate", "Domain-Computer",
    "Employee-EAP-TLS", "Contractor-Limited", "Voice-Device", "Printer-Static",
    "Camera-Static", "Guest-Portal", "Unknown-MAB", "Default",
)
AUTH_METHODS = ("dot1x", "mab", "PEAP", "EAP-TLS", "EAP-FAST", "PAP_ASCII")
FAILURE_CODES = (
    "5400", "5411", "5440", "11007", "11036", "12321", "22040", "24408",
    "15039", "5449", "11353", "12934",
)
POSTURE_STATUSES = ("Compliant", "NonCompliant", "Pending", "NotApplicable")
POSTURE_POLICIES = (
    "Corp-AV-Required", "Corp-Disk-Encryption", "Corp-Patch-Level",
    "Corp-Firewall-On", "Corp-USB-Policy",
)
AGENT_VERSIONS = ("5.1.2.42", "5.1.1.36", "4.10.07061", "5.0.4.74")
OPERATING_SYSTEMS = (
    "Windows 11 Enterprise", "Windows 10 Enterprise", "macOS 15.3",
    "Ubuntu 24.04", "iOS 18.2", "Android 15",
)
SESSION_STATES = ("STARTED", "AUTHENTICATED", "POSTURED")


def mac_of(index):
    """A locally-administered MAC, deterministic in the endpoint index."""
    value = 0x0A5EED000000 + (index & 0xFFFFFF)
    text = f"{value:012X}"
    return ":".join(text[position:position + 2] for position in range(0, 12, 2))


class Estate:
    """A deterministic synthetic deployment of the requested size."""

    def __init__(self, *, nads=5000, endpoints=100_000, sessions=20_000,
                 accounts=1000, policy_sets=len(POLICY_SETS),
                 churn_per_hour=0.12, clock=None):
        self.nad_count = int(nads)
        self.endpoint_count = int(endpoints)
        self.session_count = int(sessions)
        self.account_count = int(accounts)
        self.policy_set_count = max(1, int(policy_sets))

        site_count = max(1, min(40, self.nad_count // 100 or 1))
        self.locations = tuple(f"Site-{index:02d}" for index in range(site_count))
        self.ops_owners = tuple(f"net-team-{index:02d}" for index in range(12))
        self.device_types = ("Switch", "WLC", "Router", "AccessPoint", "Firewall")

        psn_count = max(2, min(50, -(-self.session_count // 2500)))
        self.psns = tuple(f"psn{index:02d}.ise.example.net"
                          for index in range(1, psn_count + 1))
        self.policy_sets = self._build_policy_sets()
        self.nodes = self._build_nodes()
        self.nads = self._build_nads()
        self.accounts = self._build_accounts()
        # Sessions are computed from their index rather than stored: the active
        # set is a window sliding over the endpoint population, so endpoints
        # join and leave at a declared rate and the detail caches face real
        # churn instead of a fixture that never changes.
        self.churn_per_hour = float(churn_per_hour)
        self._clock = clock
        self._started = clock.time() if clock else 0.0
        self._chunks = {}

    # --- inventory ------------------------------------------------------

    def _build_nodes(self):
        nodes = [
            {"hostname": "pan1.ise.example.net", "roles": ["PrimaryAdmin"],
             "services": [], "nodeStatus": "Connected"},
            {"hostname": "pan2.ise.example.net", "roles": ["SecondaryAdmin"],
             "services": [], "nodeStatus": "Connected"},
            {"hostname": "mnt1.ise.example.net", "roles": ["PrimaryMonitoring"],
             "services": [], "nodeStatus": "Connected"},
            {"hostname": "mnt2.ise.example.net", "roles": ["SecondaryMonitoring"],
             "services": [], "nodeStatus": "Connected"},
        ]
        for hostname in self.psns:
            nodes.append({
                "hostname": hostname, "roles": [],
                "services": ["Session", "Profiler", "DeviceAdmin"],
                "nodeStatus": "Connected"})
        return nodes

    def _build_nads(self):
        devices = []
        for index in range(self.nad_count):
            site = self.locations[index % len(self.locations)]
            owner = self.ops_owners[(index * 7) % len(self.ops_owners)]
            kind = self.device_types[(index * 3) % len(self.device_types)]
            octet_high, octet_low = divmod(index, 254)
            devices.append({
                "id": f"{index:08x}-0000-4000-8000-{index:012x}",
                "name": f"sw-{site.lower()}-{index:04d}",
                "ip": f"10.{16 + octet_high // 254}.{octet_high % 254}.{octet_low + 1}",
                "location": site,
                "ops_owner": owner,
                "device_type": kind,
            })
        return devices

    def _build_accounts(self):
        return [{"id": f"user-{index:06d}", "name": f"netadmin{index:04d}",
                 "enabled": index % 17 != 0}
                for index in range(self.account_count)]

    def _build_policy_sets(self):
        names = list(POLICY_SETS[:self.policy_set_count])
        names.extend(
            f"Device-Admin-{index:03d}"
            for index in range(len(names), self.policy_set_count)
        )
        return tuple(names)

    def window_start(self):
        """Which endpoint index the active set currently starts at."""
        if self._clock is None or self.churn_per_hour <= 0:
            return 0
        hours = max(0.0, (self._clock.time() - self._started) / 3600.0)
        moved = int(self.session_count * self.churn_per_hour * hours)
        return moved % max(1, self.endpoint_count - self.session_count)

    def active_indices(self):
        start = self.window_start()
        return range(start, start + self.session_count)

    def session_fields(self, index):
        """Everything MnT would report for one session, as plain strings."""
        nad = index % self.nad_count
        psn = index % len(self.psns)
        authz = index % len(AUTHZ_PROFILES)
        rule = index % len(AUTHZ_RULES)
        policy = index % len(self.policy_sets)
        method = index % len(AUTH_METHODS)
        posture = index % len(POSTURE_STATUSES)
        system = index % len(OPERATING_SYSTEMS)
        agent = index % len(AGENT_VERSIONS)
        passed = index % 23 != 0
        failure = index % len(FAILURE_CODES)
        device = self.nads[nad]
        mac = mac_of(index)
        return {
            "mac": mac,
            "user_name": f"user{index % 8000:05d}@example.net",
            "nas_ip_address": device["ip"],
            "network_device_name": device["name"],
            "location": f"All Locations#{device['location']}",
            "server": self.psns[psn],
            "framed_ip_address": f"172.{16 + index % 15}.{index // 254 % 254}."
                                 f"{index % 254 + 1}",
            "nas_port_id": f"GigabitEthernet1/0/{index % 48 + 1}",
            "session_state": SESSION_STATES[index % len(SESSION_STATES)],
            "authentication_method": AUTH_METHODS[method],
            "selected_azn_profiles": AUTHZ_PROFILES[authz],
            "identity_group": "Profiled" if index % 3 else "Employee",
            "passed": "true" if passed else "false",
            "failed": "false" if passed else "true",
            "failure_reason": "" if passed else
                              f"{FAILURE_CODES[failure]} Authentication failed",
            "posture_status": POSTURE_STATUSES[posture],
            "posture_agent_version": AGENT_VERSIONS[agent],
            "operating_system": OPERATING_SYSTEMS[system],
            "execution_steps": "1001,1002,1003",
            "step_latency": (
                f"1={2 + index % 4};2={7 + index % 11};"
                f"3={15 + index % 23}"
            ),
            "total_authentication_latency": str(35 + index % 90),
            "posture_report": ";".join(
                f"{policy_name}:{'Passed' if (index + offset) % 4 else 'Failed'}"
                for offset, policy_name in enumerate(POSTURE_POLICIES)),
            "other_attr_string": (
                f":!:AuthorizationPolicyMatchedRule={AUTHZ_RULES[rule]}"
                f":!:ISEPolicySetName={self.policy_sets[policy]}"
                f":!:AuthenticationIdentityStore=AD-example"
                f":!:SelectedAccessService=Default Network Access"),
        }

    # --- MnT documents --------------------------------------------------

    _ACTIVE_FIELDS = (
        "user_name", "calling_station_id", "nas_ip_address",
        "network_device_name", "framed_ip_address", "nas_port_id", "server",
        "session_state", "identity_group", "posture_status",
    )

    def _chunk(self, index):
        """One rendered <activeSession>, cached: churn only replaces a few."""
        cached = self._chunks.get(index)
        if cached is None:
            fields = self.session_fields(index)
            fields["calling_station_id"] = fields["mac"]
            body = "".join(
                f"<{name}>{_escape(fields.get(name, ''))}</{name}>"
                for name in self._ACTIVE_FIELDS)
            cached = f"<activeSession>{body}</activeSession>".encode("utf-8")
            if len(self._chunks) > 3 * self.session_count:
                self._chunks.clear()
            self._chunks[index] = cached
        return cached

    def active_list_xml(self):
        header = (f'<?xml version="1.0" encoding="UTF-8"?>'
                  f'<activeSessions noOfActiveSession="{self.session_count}">')
        return b"".join(
            [header.encode("utf-8")]
            + [self._chunk(index) for index in self.active_indices()]
            + [b"</activeSessions>"])

    def index_of(self, mac):
        """The endpoint index behind a MAC, or None if it is not active now."""
        try:
            value = int(str(mac).replace(":", "").replace("-", ""), 16)
        except ValueError:
            return None
        index = value - 0x0A5EED000000
        return index if index in self.active_indices() else None

    def session_detail_xml(self, mac):
        index = self.index_of(mac)
        if index is None:
            # A session that ended between the listing and the detail request.
            return b'<?xml version="1.0" encoding="UTF-8"?><sessionParameters/>'
        fields = self.session_fields(index)
        fields["calling_station_id"] = fields["mac"]
        fields.pop("mac")
        body = "".join(f"<{name}>{_escape(value)}</{name}>"
                       for name, value in fields.items() if value != "")
        return (f'<?xml version="1.0" encoding="UTF-8"?>'
                f'<sessionParameters>{body}</sessionParameters>').encode("utf-8")


def _escape(value):
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


# --------------------------------------------------------------------------
# The HTTP appliance: ERS, PAN OpenAPI, MnT
# --------------------------------------------------------------------------

class FakeIseHttp:
    """Serves the three HTTPS interfaces v3 collects from."""

    def __init__(self, estate, clock, latency, *, directory):
        self.estate = estate
        self.clock = clock
        self.latency = latency
        self.requests = defaultdict(int)
        self.cpu_seconds = 0.0
        self.unhandled = []
        self.server = None
        self._tls = _self_signed(directory)

    # --- lifecycle ------------------------------------------------------

    def start(self):
        appliance = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            # Without this, the header write and the body write leave as separate
            # segments and the client waits out a 40 ms delayed ACK on every
            # request -- which would have been measured as exporter cost.
            disable_nagle_algorithm = True

            def do_GET(self):                          # noqa: N802 - stdlib API
                appliance.handle(self)

            def log_message(self, *_args):
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(*self._tls)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        return self.port

    def stop(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    @property
    def port(self):
        return self.server.server_address[1]

    @property
    def base_url(self):
        return f"https://127.0.0.1:{self.port}"

    # --- routing --------------------------------------------------------

    def handle(self, request):
        started = time.perf_counter()
        try:
            return self._route(request)
        finally:
            self.cpu_seconds += time.perf_counter() - started

    def _route(self, request):
        parsed = urlparse(request.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path.startswith("/ers/"):
                self.requests["ers"] += 1
                return self._ers(request, path[len("/ers"):], query)
            if path.startswith("/api/v1/"):
                self.requests["openapi"] += 1
                return self._openapi(request, path[len("/api/v1"):], query)
            if path.startswith("/admin/API/mnt/"):
                self.requests["mnt"] += 1
                return self._mnt(request, path[len("/admin/API/mnt"):])
        except BrokenPipeError:
            return None
        self.unhandled.append(path)
        return self._send(request, 404, b'{"error":"not found"}', "application/json")

    def _send(self, request, status, body, content_type):
        request.send_response(status)
        request.send_header("Content-Type", content_type)
        request.send_header("Content-Length", str(len(body)))
        request.end_headers()
        request.wfile.write(body)
        return None

    def _json(self, request, payload):
        return self._send(request, 200, json.dumps(payload).encode("utf-8"),
                          "application/json")

    # --- ERS ------------------------------------------------------------

    def _page(self, query):
        try:
            page = int(query.get("page", ["1"])[0])
            size = int(query.get("size", ["100"])[0])
        except (TypeError, ValueError):
            page, size = 1, 100
        return max(1, page), max(1, min(100, size))

    def _ers(self, request, path, query):
        if path.startswith("/config/networkdevice"):
            remainder = path[len("/config/networkdevice"):].strip("/")
            if remainder:
                return self._ers_device_detail(request, remainder)
            return self._ers_device_list(request, query)
        if path.startswith("/config/internaluser"):
            remainder = path[len("/config/internaluser"):].strip("/")
            if remainder:
                return self._ers_user_detail(request, remainder)
            return self._ers_user_list(request, query)
        self.unhandled.append(f"ers{path}")
        return self._send(request, 404, b'{"error":"no such ERS resource"}',
                          "application/json")

    def _search_result(self, request, resource, rows, page, size, total):
        start = (page - 1) * size
        window = rows[start:start + size]
        result = {"total": total, "resources": window}
        if start + size < total:
            result["nextPage"] = {
                "rel": "next",
                "href": f"{self.base_url}/ers{resource}?size={size}&page={page + 1}",
            }
        self.clock.advance(self.latency.ers_page(), "pan")
        return self._json(request, {"SearchResult": result})

    def _ers_device_list(self, request, query):
        page, size = self._page(query)
        rows = [{"id": device["id"], "name": device["name"],
                 "description": f"{device['device_type']} at {device['location']}"}
                for device in self.estate.nads]
        return self._search_result(request, "/config/networkdevice", rows, page,
                                   size, len(rows))

    def _ers_device_detail(self, request, device_id):
        self.clock.advance(self.latency.ers_detail(), "pan")
        found = next((device for device in self.estate.nads
                      if device["id"] == device_id), None)
        if found is None:
            return self._send(request, 404, b'{"error":"no such device"}',
                              "application/json")
        return self._json(request, {"NetworkDevice": {
            "id": found["id"],
            "name": found["name"],
            "NetworkDeviceIPList": [{"ipaddress": found["ip"], "mask": 32}],
            "NetworkDeviceGroupList": [
                f"Location#All Locations#{found['location']}",
                f"Ops Owner#All Ops Owners#{found['ops_owner']}",
                f"Device Type#All Device Types#{found['device_type']}",
                "IPSEC#Is IPSEC Device#No",
            ],
            "profileName": "Cisco",
            "coaPort": 1700,
        }})

    def _ers_user_list(self, request, query):
        page, size = self._page(query)
        rows = [{"id": account["id"], "name": account["name"]}
                for account in self.estate.accounts]
        return self._search_result(request, "/config/internaluser", rows, page,
                                   size, len(rows))

    def _ers_user_detail(self, request, account_id):
        self.clock.advance(self.latency.ers_detail(), "pan")
        found = next((account for account in self.estate.accounts
                      if account["id"] == account_id), None)
        if found is None:
            return self._send(request, 404, b'{"error":"no such user"}',
                              "application/json")
        index = int(account_id.rsplit("-", 1)[-1])
        return self._json(request, {"InternalUser": {
            "id": found["id"],
            "name": found["name"],
            "enabled": "true" if found["enabled"] else "false",
            "changePassword": "false" if index % 5 else "true",
            "passwordNeverExpires": index % 11 == 0,
            "identityGroups": "Network Admins",
            "expiryDateEnabled": index % 7 == 0,
        }})

    # --- PAN OpenAPI ----------------------------------------------------

    def _openapi(self, request, path, query):
        if path == "/deployment/node":
            self.clock.advance(self.latency.openapi(len(self.estate.nodes)), "pan")
            return self._json(request, {"response": self.estate.nodes})
        if path == "/deployment/pan-ha":
            self.clock.advance(self.latency.openapi(), "pan")
            return self._json(request, {"response": {"isEnabled": True}})
        if path == "/patch":
            self.clock.advance(self.latency.openapi(), "pan")
            return self._json(request, {"response": {
                "iseVersion": SUPPORTED_ISE_VERSION,
                "patchVersion": [{"patchNumber": number, "installDate": "2026-05-02"}
                                 for number in range(1, SUPPORTED_PATCH_LEVEL + 1)],
            }})
        if path == "/license/system/tier-state":
            self.clock.advance(self.latency.openapi(), "pan")
            return self._json(request, [
                {"name": "ESSENTIAL", "status": "ENABLED", "compliance": "COMPLIANT",
                 "consumptionCounter": self.estate.endpoint_count},
                {"name": "ADVANTAGE", "status": "ENABLED", "compliance": "COMPLIANT",
                 "consumptionCounter": int(self.estate.endpoint_count * 0.62)},
                {"name": "PREMIER", "status": "ENABLED", "compliance": "EVALUATION",
                 "consumptionCounter": int(self.estate.endpoint_count * 0.11)},
                {"name": "DEVICEADMIN", "status": "ENABLED", "compliance": "COMPLIANT",
                 "consumptionCounter": 1},
            ])
        if path == "/backup-restore/config/last-backup-status":
            self.clock.advance(self.latency.openapi(), "pan")
            started = datetime.now(timezone.utc) - timedelta(hours=9, minutes=12)
            return self._json(request, {"response": {
                "status": "COMPLETED",
                "startDate": started.strftime("%a %b %d %H:%M:%S UTC %Y"),
                "name": "nightly-config",
            }})
        if path.startswith("/certs/system-certificate/"):
            node = path.rsplit("/", 1)[-1]
            return self._certificates(request, query, f"/certs/system-certificate/{node}",
                                      self._system_certificates(node))
        if path == "/certs/trusted-certificate":
            return self._certificates(request, query, "/certs/trusted-certificate",
                                      self._trusted_certificates())
        if path == "/policy/device-admin/policy-set":
            self.clock.advance(self.latency.openapi(), "pan")
            return self._json(request, {"response": [
                {"id": f"ps-{index}", "name": name, "rank": index}
                for index, name in enumerate(self.estate.policy_sets)]})
        policy_rules = re.fullmatch(
            r"/policy/device-admin/policy-set/(ps-\d+)/(authentication|authorization)",
            path,
        )
        if policy_rules:
            policy_id, rule_type = policy_rules.groups()
            index = int(policy_id.removeprefix("ps-"))
            count = 3 + index % 5 if rule_type == "authentication" else 8 + index % 9
            self.clock.advance(self.latency.openapi(count), "pan")
            return self._json(request, {"response": [
                {
                    "id": f"{policy_id}-{rule_type}-{rule}",
                    "name": f"{rule_type}-{rule}",
                    "rank": rule,
                }
                for rule in range(count)
            ]})
        if path == "/policy/device-admin/command-sets":
            self.clock.advance(self.latency.openapi(), "pan")
            return self._json(request, {"response": [
                {"id": f"cs-{index}", "name": name}
                for index, name in enumerate(COMMAND_SETS)]})
        if path == "/policy/device-admin/shell-profiles":
            self.clock.advance(self.latency.openapi(), "pan")
            return self._json(request, {"response": [
                {"id": f"sp-{index}", "name": name}
                for index, name in enumerate(SHELL_PROFILES)]})
        self.unhandled.append(f"api/v1{path}")
        return self._send(request, 404, b'{"error":"no such OpenAPI resource"}',
                          "application/json")

    def _certificates(self, request, query, resource, rows):
        page, size = self._page(query)
        start = (page - 1) * size
        window = rows[start:start + size]
        self.clock.advance(self.latency.openapi(len(window)), "pan")
        payload = {"response": window}
        if start + size < len(rows):
            payload["nextPage"] = {
                "rel": "next",
                "href": f"{self.base_url}/api/v1{resource}?size={size}&page={page + 1}",
            }
        return self._json(request, payload)

    def _certificate(self, name, node, days, usage):
        expires = datetime.now(timezone.utc) + timedelta(days=days)
        return {
            "id": f"cert-{node}-{name}",
            "friendlyName": f"{name} ({node})",
            "expirationDate": expires.strftime("%a %b %d %H:%M:%S UTC %Y"),
            "issuedTo": node,
            "issuedBy": "example-issuing-ca",
            "keySize": 2048,
            "selfSigned": False,
            "usedBy": usage,
        }

    def _system_certificates(self, node):
        offset = sum(ord(character) for character in node)
        usages = ("Admin", "EAP Authentication", "pxGrid", "Portal", "RADIUS DTLS",
                  "SAML", "ISE Messaging Service")
        return [self._certificate(usage.replace(" ", "-"), node,
                                  30 + (offset + position * 97) % 700, usage)
                for position, usage in enumerate(usages)]

    def _trusted_certificates(self):
        return [self._certificate(f"trusted-ca-{index:03d}", "trust_store",
                                  45 + (index * 37) % 3000, "Infrastructure")
                for index in range(180)]

    # --- MnT ------------------------------------------------------------

    def _mnt(self, request, path):
        if path == "/Session/ActiveList":
            self.clock.advance(
                self.latency.mnt_active_list(self.estate.session_count), "mnt")
            return self._send(request, 200, self.estate.active_list_xml(),
                              "application/xml")
        if path.startswith("/Session/MACAddress/"):
            self.clock.advance(self.latency.mnt_detail(), "mnt")
            mac = path.rsplit("/", 1)[-1]
            return self._send(request, 200, self.estate.session_detail_xml(mac),
                              "application/xml")
        self.unhandled.append(f"mnt{path}")
        return self._send(request, 404, b"<error/>", "application/xml")


def _self_signed(directory):
    """A throwaway certificate for the fake listener. TLS stays real."""
    certificate = f"{directory}/fake-ise.pem"
    key = f"{directory}/fake-ise.key"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", key, "-out", certificate, "-days", "2",
         "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1"],
        check=True, capture_output=True)
    return certificate, key


# --------------------------------------------------------------------------
# Transports
# --------------------------------------------------------------------------

class SimulatedRestTransport(RestTransport):
    """The real REST transport, pointed at the fake listener.

    Only the three base URLs move. Paging, the completeness check, the bounded
    XML parse, the auth guard and the telemetry are the shipped code.
    """

    def __init__(self, config, target, appliance):
        super().__init__(config, target)
        self.appliance = appliance
        self.ers_url = f"{appliance.base_url}/ers"
        self.openapi_url = f"{appliance.base_url}/api/v1"
        self.mnt_url = f"{appliance.base_url}/admin/API/mnt"


# --------------------------------------------------------------------------
# Data Connect: a synthetic cursor under the real transport
# --------------------------------------------------------------------------

class Column:
    """Stands in for oracledb's cursor description entry."""

    def __init__(self, name):
        self.name = name


class FakeCursor:
    def __init__(self, connection):
        self._connection = connection
        self.description = None
        self._rows = []
        self._position = 0
        self._per_batch = 0.0

    def __enter__(self):
        return self

    def __exit__(self, *_exception):
        return False

    def execute(self, sql, parameters=None):
        columns, rows, duration = self._connection.oracle.plan(sql, parameters)
        self.description = [Column(name) for name in columns]
        self._rows = rows
        self._position = 0
        batches = max(1, -(-len(rows) // 100))
        # Most of a bounded aggregate's time is the scan, before the first row.
        self._connection.oracle.clock.advance(duration * 0.8, "oracle")
        self._per_batch = (duration * 0.2) / batches

    def fetchmany(self, size):
        batch = self._rows[self._position:self._position + size]
        self._position += len(batch)
        if batch:
            self._connection.oracle.clock.advance(self._per_batch, "oracle")
        return batch

    def close(self):
        self._rows = []


class FakeConnection:
    def __init__(self, oracle):
        self.oracle = oracle
        self.call_timeout = 0

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        return None


class SimulatedDataConnect(DataConnectTransport):
    """The real Data Connect transport over a synthetic Oracle.

    The gate, the duty cycle, the batch lease, the row and byte ceilings and the
    statement timeout are all the shipped implementation; only the driver is
    replaced. Statements are answered by reading their own SELECT list, so a
    dataset receives exactly the columns it asked for.
    """

    def __init__(self, config, estate, clock, latency):
        super().__init__(config)
        self.estate = estate
        self.clock = clock
        self.lane = LaneClock(clock)
        self.latency = latency
        self.statements = []
        self._catalog = None

    def connect(self):
        if self._connection is None:
            self._connection = FakeConnection(self)
        return self._connection

    def catalog(self, views):
        """The reporting views this simulated account can see."""
        self._catalog = dict(views)

    def plan(self, sql, parameters=None):
        """Return (columns, rows, modelled_seconds) for one statement."""
        from ise_exporter3.transports.dataconnect import view_of

        text = normalize_sql(sql)
        view = view_of(text)
        if "user_tab_columns" in text.lower():
            rows = [(table, column)
                    for table, columns in sorted((self._catalog or {}).items())
                    for column in sorted(columns)]
            self.statements.append((view, len(rows)))
            return ["table_name", "column_name"], rows, self.latency.oracle_query(
                view, len(rows))

        columns, rows = synthesize(text, self.estate, parameters)
        self.statements.append((view, len(rows)))
        return columns, rows, self.latency.oracle_query(view, len(rows))


# --------------------------------------------------------------------------
# Statement synthesis
# --------------------------------------------------------------------------

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#]*$")
_KEYWORDS = frozenset({
    "asc", "desc", "null", "end", "distinct", "all", "and", "or", "not", "as",
})
_LIMIT = re.compile(r"fetch\s+(?:first|next)\s+(\d+)\s+rows?\s+only", re.I)
_OFFSET = re.compile(r"offset\s+(\d+)\s+rows?", re.I)
_ROWNUM = re.compile(r"rownum\s*<=?\s*(\d+)", re.I)
_PARTITION = re.compile(r"partition\s+by\s+([A-Za-z0-9_.,\s]+?)\s+order\s+by", re.I)
_GROUPING_CASE = re.compile(r"grouping\s*\((.+?)\)\s*=\s*0\s+then\s+'([^']+)'", re.I)
_LEADING_LITERAL = re.compile(r"^'([^']*)'")

# A synthesised result set is never allowed to be unbounded, whatever the
# statement implies. The transport's own 6,000-row ceiling is what should stop
# an over-large result, so this sits above it purely as a backstop.
MAX_SYNTHETIC_ROWS = 60_000


def normalize_sql(sql):
    return " ".join(str(sql or "").split())


def _norm(text):
    return " ".join(str(text).split()).lower()


def _split_top_level(text, separator=","):
    parts, depth, current = [], 0, []
    for character in text:
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        if character == separator and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(character)
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def _top_level_positions(text, needle):
    lowered = text.lower()
    depth, found = 0, []
    for position, character in enumerate(text):
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        elif depth == 0 and lowered.startswith(needle, position):
            found.append(position)
    return found


def _split_on_keyword(text, keyword):
    """Split a statement on a top-level keyword such as UNION ALL."""
    positions = _top_level_positions(text, keyword)
    if not positions:
        return [text]
    parts, previous = [], 0
    for position in positions:
        parts.append(text[previous:position].strip())
        previous = position + len(keyword)
    parts.append(text[previous:].strip())
    return [part for part in parts if part]


def _outer_select_list(text):
    """The SELECT list of the outermost query; subqueries are ignored."""
    starts = _top_level_positions(text, "select ")
    if not starts:
        return []
    start = starts[-1] + len("select ")
    lowered = text.lower()
    if lowered.startswith("distinct ", start):
        start += len("distinct ")
    froms = [position for position in _top_level_positions(text, " from ")
             if position > start]
    end = froms[0] if froms else len(text)
    return _split_top_level(text[start:end])


def _inner_select(text):
    """The first parenthesised subquery, which is where a wrapper's rows live."""
    depth, start = 0, None
    for position, character in enumerate(text):
        if character == "(":
            depth += 1
            if depth == 1:
                start = position + 1
        elif character == ")":
            depth -= 1
            if depth == 0 and start is not None:
                candidate = text[start:position].strip()
                if candidate.lower().startswith("select "):
                    return candidate
                start = None
    return ""


def _clause(text, keyword, stops=(" order by", " fetch ", " having ", " offset ")):
    positions = _top_level_positions(text, keyword)
    if not positions:
        return []
    start = positions[0] + len(keyword)
    end = len(text)
    for stop in stops:
        for position in _top_level_positions(text, stop):
            if start < position < end:
                end = position
    return _split_top_level(text[start:end])


def _split_alias(expression):
    """Return (expression body, alias) for one SELECT-list item."""
    positions = _top_level_positions(expression, " as ")
    if positions:
        cut = positions[-1]
        return (expression[:cut].strip(),
                expression[cut + len(" as "):].strip().strip('"').lower())
    tokens = expression.split()
    if len(tokens) > 1 and _IDENTIFIER.match(tokens[-1]):
        return " ".join(tokens[:-1]), tokens[-1].lower()
    return expression, None


def _column_name(expression, index):
    body, alias = _split_alias(expression)
    if alias and alias not in _KEYWORDS:
        return alias
    bare = body.strip().strip('"').split(".")[-1].lower()
    if _IDENTIFIER.match(bare) and bare not in _KEYWORDS:
        return bare
    return f"col_{index}"


def _row_limit(text):
    limits = [MAX_SYNTHETIC_ROWS]
    for pattern in (_LIMIT, _ROWNUM):
        match = pattern.search(text)
        if match:
            limits.append(int(match.group(1)))
    return min(limits)


def _row_offset(text):
    match = _OFFSET.search(text)
    return int(match.group(1)) if match else 0


class Domains:
    """What a grouped column's values are, and how many there are.

    Row counts come from the statement's own GROUP BY (or GROUPING SETS), so a
    statement that breaks down by NAD gets one row per NAD at the configured
    scale. That is the point: the row ceiling, the byte ceiling, the truncation
    signal and the resulting Prometheus cardinality are then all exercised at
    the size the exporter claims to support.
    """

    def __init__(self, estate):
        self.estate = estate

    def values(self, name):
        estate = self.estate
        name = name.lower()
        if name in ("ise_node", "node", "psn", "server", "acs_server"):
            return list(estate.psns)
        if name in ("nad", "network_device_name", "device_name", "device",
                    "nas_name", "nas_identifier"):
            return [device["name"] for device in estate.nads]
        if name in ("nas_ip_address", "nas_ipv4_address", "ip_address"):
            return [device["ip"] for device in estate.nads]
        if name in ("endpoint", "mac", "mac_address", "calling_station_id",
                    "endpoint_id", "endpoint_mac", "endpoint_mac_address"):
            return [mac_of(index) for index in range(estate.endpoint_count)]
        if name in ("username", "user_name", "identity", "user"):
            return [account["name"] for account in estate.accounts]
        if name in ("location", "device_location"):
            return list(estate.locations)
        if name in ("ops_owner", "owner"):
            return list(estate.ops_owners)
        if name in ("device_type", "nad_type"):
            return list(estate.device_types)
        if "policy_set" in name:
            return list(estate.policy_sets)
        if "azn" in name or "authz" in name or "authorization_profile" in name:
            return list(AUTHZ_PROFILES)
        if "profile" in name:
            return list(ENDPOINT_PROFILES)
        if "rule" in name or name == "policy":
            return list(AUTHZ_RULES)
        if "condition" in name or "posture_policy" in name:
            return list(POSTURE_POLICIES)
        if "method" in name or "protocol" in name:
            return list(AUTH_METHODS)
        if "error" in name or "code" in name or "reason" in name:
            return list(FAILURE_CODES)
        if "status" in name or "result" in name or "state" in name:
            return list(POSTURE_STATUSES)
        if name == "os" or "operating_system" in name:
            return list(OPERATING_SYSTEMS)
        if "version" in name:
            return list(AGENT_VERSIONS)
        if "group" in name:
            return ["Employee", "Profiled", "Contractor", "Guest", "IoT"]
        if "service" in name or "store" in name:
            return ["AD-example", "Internal Users", "Guest Users"]
        return [f"{name}-{index:02d}" for index in range(8)]


_COUNT_HINTS = (
    "count", "total", "num", "hits", "sessions", "starts", "stops", "requests",
    "attempts", "passed", "failed", "endpoints", "logged", "noise", "events",
    "suppression", "records", "rows", "assessments", "devices", "timed",
)
_RATE_HINTS = (
    "percent", "utilization", "utilisation", "load", "latency", "tps", "avg",
    "average", "ratio", "rate", "seconds", "duration", "diskspace", "cpu",
    "memory", "response",
)
_TIME_HINTS = ("timestamp", "_time", "time_", "date", "last_seen", "logged_time")


_AGGREGATE = re.compile(r"\b(sum|count|avg|min|max)\s*\(", re.I)


def _measure(name, row_index, estate, numeric=False):
    """A plausible value for a column the statement did not group on."""
    if name.endswith("_id") or name == "id":
        return 100_000 + row_index
    if any(hint in name for hint in _TIME_HINTS):
        return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            minutes=(row_index * 7) % 360)
    if any(hint in name for hint in _RATE_HINTS):
        return round(3.5 + ((row_index * 13) % 640) / 10.0, 2)
    if numeric or any(hint in name for hint in _COUNT_HINTS):
        base = max(2, estate.session_count // 50)
        return base + (row_index * 37) % base
    return f"{name}-{row_index % 12:02d}"


def _fill(columns, assigned, row_index, estate, numeric=()):
    return tuple(assigned[name] if name in assigned
                 else _measure(name, row_index, estate, name in numeric)
                 for name in columns)


def _grouped_columns(text, inner, expressions, columns):
    """Which output columns the statement actually groups by."""
    source, items = text, _clause(text, "group by")
    if not items and inner:
        source, items = inner, _clause(inner, "group by")

    bodies = {}
    for expression, name in zip(expressions, columns):
        body, _alias = _split_alias(expression)
        bodies[_norm(body)] = name

    grouped = []
    for item in items:
        key = _norm(item)
        if key in bodies:
            grouped.append(bodies[key])
            continue
        bare = item.strip().split(".")[-1].lower()
        if bare in columns:
            grouped.append(bare)
    if grouped:
        return grouped

    match = _PARTITION.search(source) or _PARTITION.search(text)
    if match:
        return [name.strip().split(".")[-1].lower()
                for name in match.group(1).split(",")
                if name.strip().split(".")[-1].lower() in columns]
    return []


def _marginal_rows(text, columns, estate, limit, offset, numeric=()):
    """One row per (dimension, value) pair across every GROUPING SET.

    This is `reporting.marginals`: the breakdowns add rather than multiply, and
    the whole set is bounded by one FETCH FIRST. Reproducing it faithfully is
    what makes the truncation signal (returned versus existing) meaningful at
    scale -- at 5,000 NADs some of these genuinely do not fit.
    """
    domains = Domains(estate)
    pairs = [(dimension, value)
             for _expression, dimension in _GROUPING_CASE.findall(text)
             for value in domains.values(dimension)]
    total = len(pairs)
    rows = []
    for index, (dimension, value) in enumerate(pairs[offset:offset + limit]):
        rows.append(_fill(columns,
                          {"dimension": dimension, "value": value,
                           "group_total": total},
                          index, estate, numeric))
    return rows


def synthesize(text, estate, parameters=None):
    """Answer one statement from its own shape.

    The SELECT list gives the columns, the GROUP BY / GROUPING SETS / PARTITION
    BY gives the row count, and everything left over becomes a measure.
    """
    del parameters
    branches = _split_on_keyword(text, " union all ")
    if len(branches) > 1:
        columns, rows = [], []
        for branch in branches:
            branch_columns, branch_rows = synthesize(branch, estate)
            columns = columns or branch_columns
            rows.extend(branch_rows)
        return columns, rows[:_row_limit(text)]

    expressions = _outer_select_list(text)
    inner = _inner_select(text)
    if inner:
        expanded = []
        for expression in expressions:
            if expression == "*" or expression.endswith(".*"):
                expanded.extend(_outer_select_list(inner))
            else:
                expanded.append(expression)
        expressions = expanded
    if not expressions:
        return ["value"], [(1,)]

    columns = [_column_name(expression, index)
               for index, expression in enumerate(expressions)]
    literals = {}
    for expression, name in zip(expressions, columns):
        body, _alias = _split_alias(expression)
        match = _LEADING_LITERAL.match(body.strip())
        if match:
            literals[name] = match.group(1)

    numeric = {name for name, expression in zip(columns, expressions)
               if _AGGREGATE.search(expression)}
    limit, offset = _row_limit(text), _row_offset(text)
    if "grouping sets" in text.lower():
        return columns, _marginal_rows(
            text, columns, estate, limit, offset, numeric)

    grouped = _grouped_columns(text, inner, expressions, columns)
    if not grouped:
        return columns, [
            _fill(columns, {**literals, "group_total": 1}, 0, estate, numeric)]

    domains = Domains(estate)
    spaces = [domains.values(name) for name in grouped]
    total = 1
    for space in spaces:
        total *= max(1, len(space))
    total = min(total, MAX_SYNTHETIC_ROWS)

    rows = []
    for row_index in range(offset, min(total, offset + limit)):
        assigned, remainder = dict(literals), row_index
        for name, space in zip(grouped, spaces):
            size = max(1, len(space))
            assigned[name] = space[remainder % size] if space else name
            remainder //= size
        assigned.setdefault("group_total", total)
        rows.append(_fill(columns, assigned, row_index, estate, numeric))
    return columns, rows
