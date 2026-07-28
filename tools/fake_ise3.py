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
   Two things cannot be read off a statement and so are declared instead:
   `LAB_EMPTY_VIEWS`, the views that hold no rows, and `EMPTY_DIMENSIONS`, the
   columns that exist and are NULL on every row. Both are ordinary appliance
   states, both make a dataset publish nothing, and a synthesiser that answers
   every statement with plausible rows can never show either.

3. **Time is virtual.** Modelled appliance latency (a declared assumption, see
   `Latency`) advances a shared clock instead of sleeping. The scheduler, the
   Data Connect cooldowns and the statement timeouts all read that clock, so a
   24-hour soak with real 2%-duty-cycle pacing runs in minutes and still
   throttles, starves and times out where the real thing would.

What is *not* simulated: ISE's own semantics. Aggregate values are plausible,
not meaningful. This measures cost, cardinality, convergence and pacing -- never
whether a number is correct.

What *is* held to the appliance is every payload's **shape**, because a field
the simulator invents is a design that passes here and collects nothing in
production. Every response below was captured from ISE 3.3.0.430 Patch 11 on
2026-07-27 and is documented in `docs/DATASETS_FACTS.md`; the estate around them
is scaled up, the shapes are not.
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
# Node names are short everywhere ISE reports them -- /deployment/node hostname,
# the MnT acs_server element, the Data Connect ISE_NODE column -- and the FQDN
# arrives beside them as its own field. Modelling the FQDN as the identity gave
# the `node` and `psn` labels a shape production never has.
NODE_DOMAIN = "ise.example.net"

# One NAD in this many is configured in ISE as a subnet rather than a host, and
# one in this many is left at the network-device-group roots. Both are the
# awkward real cases: a subnet NAD's configured address is the network address,
# which no session's NAS IP can equal, and a rooted NAD's group strings have two
# segments and carry no location or device type at all. A uniform estate of
# /32 NADs in fully-populated groups made both invisible.
SUBNET_NAD_EVERY = 4
ROOT_NDG_EVERY = 3


def mac_of(index):
    """A locally-administered MAC, deterministic in the endpoint index."""
    value = 0x0A5EED000000 + (index & 0xFFFFFF)
    text = f"{value:012X}"
    return ":".join(text[position:position + 2] for position in range(0, 12, 2))


class Estate:
    """A deterministic synthetic deployment of the requested size."""

    def __init__(self, *, nads=5000, endpoints=100_000, sessions=20_000,
                 accounts=1000, policy_sets=len(POLICY_SETS),
                 churn_per_hour=0.12, clock=None,
                 subnet_nad_every=SUBNET_NAD_EVERY, posture_share=0.0):
        self.subnet_nad_every = max(0, int(subnet_nad_every))
        # Share of sessions whose detail carries Secure Client posture. 0.0 is
        # the probed appliance, where nothing runs posture; raise it to exercise
        # the path a deployment that does run it takes.
        self.posture_share = min(1.0, max(0.0, float(posture_share)))
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
        self.psns = tuple(f"psn{index:02d}" for index in range(1, psn_count + 1))
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

    def _node(self, index, hostname, roles, services):
        """One /deployment/node row: short hostname, fqdn and ipAddress beside it."""
        return {
            "hostname": hostname,
            "fqdn": f"{hostname}.{NODE_DOMAIN}",
            "ipAddress": f"10.10.{index // 254}.{index % 254 + 1}",
            "roles": list(roles),
            "services": list(services),
            "nodeStatus": "Connected",
        }

    def _build_nodes(self):
        # Roles and services are both lists ISE will happily return empty, and a
        # node commonly holds two personas, so the shapes are modelled here.
        nodes = [
            self._node(0, "pan1", ["PrimaryAdmin", "PrimaryMonitoring"], []),
            self._node(1, "pan2", ["SecondaryAdmin", "SecondaryMonitoring"], []),
            self._node(2, "mnt1", ["PrimaryMonitoring"], []),
            self._node(3, "mnt2", [], []),
        ]
        nodes.extend(
            self._node(4 + position, hostname, [],
                       ["Session", "Profiler", "DeviceAdmin"])
            for position, hostname in enumerate(self.psns))
        return nodes

    def _build_nads(self):
        devices = []
        for index in range(self.nad_count):
            site = self.locations[index % len(self.locations)]
            owner = self.ops_owners[(index * 7) % len(self.ops_owners)]
            kind = self.device_types[(index * 3) % len(self.device_types)]
            # One /24 per NAD, and a declared share of them configured as the
            # subnet rather than as a host: ISE then stores 10.x.y.0/24 while the
            # session arrives from 10.x.y.1, so the directory has to match by
            # containment rather than by string equality.
            second, third = 16 + index // 256, index % 256
            host = f"10.{second}.{third}.1"
            subnet = self.subnet_nad_every and index % self.subnet_nad_every == 0
            devices.append({
                "id": f"{index:08x}-0000-4000-8000-{index:012x}",
                "name": f"sw-{site.lower()}-{index:04d}",
                "ip": f"10.{second}.{third}.0" if subnet else host,
                "mask": 24 if subnet else 32,
                "nas_ip": host,
                # A NAD left at the group roots yields two-segment strings that
                # name a category and nothing else, so it classifies to Unknown.
                "rooted": index % ROOT_NDG_EVERY == 0,
                "location": site,
                "ops_owner": owner,
                "device_type": kind,
            })
        return devices

    def device_groups(self, device):
        """The NetworkDeviceGroupList ISE returns for one NAD."""
        if device["rooted"]:
            return [
                "IPSEC#Is IPSEC Device",
                "Device Type#All Device Types",
                "Location#All Locations",
                f"Ops Owner#All Ops Owners#{device['ops_owner']}",
            ]
        return [
            f"Location#All Locations#{device['location']}",
            f"Ops Owner#All Ops Owners#{device['ops_owner']}",
            f"Device Type#All Device Types#{device['device_type']}",
            "IPSEC#Is IPSEC Device#No",
        ]

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

    # --- MnT documents --------------------------------------------------

    # Exactly what ISE 3.3 Patch 11 returns from /Session/ActiveList, captured
    # from laba-ise-001 on 2026-07-27. It is a session *index*, not a summary:
    # no NAD name, no identity group, no posture status, no session state. The
    # simulator used to invent those five, which made designs that read them
    # from the bulk list look viable when they collect nothing on a real
    # appliance. Anything richer than this list costs a per-MAC detail request.
    # framed_ipv6_address is the eighth child and is emitted empty, so the
    # transport's empty-text filter drops it and seven keys survive the parse.
    _ACTIVE_FIELDS = (
        "user_name", "calling_station_id", "nas_ip_address",
        "acct_session_id", "audit_session_id", "server", "framed_ip_address",
        "framed_ipv6_address",
    )

    def session_fields(self, index):
        """The session identity the ActiveList carries. Everything else is a
        per-MAC detail request, which is the point of the split."""
        device = self.nads[index % self.nad_count]
        session_id = f"{index:08X}{index * 2654435761 & 0xFFFFFFFF:08X}"
        return {
            "mac": mac_of(index),
            "user_name": f"user{index % 8000:05d}@example.net",
            "calling_station_id": mac_of(index),
            "nas_ip_address": device["nas_ip"],
            "acct_session_id": session_id,
            "audit_session_id": session_id,
            "server": self.psns[index % len(self.psns)],
            "framed_ip_address": f"172.{16 + index % 15}.{index // 254 % 254}."
                                 f"{index % 254 + 1}",
            # Present on every row of the real document and empty on every one
            # of them; this lab has no IPv6-addressed session to show.
            "framed_ipv6_address": "",
        }

    # The 28 ISE message codes a 3.3 dot1x session reports, verbatim: codes
    # repeat, and there is one more of them than there are StepLatency entries.
    _EXECUTION_STEPS = (
        "11001,11017,15049,15008,15041,15048,15013,24430,24325,24313,24319,"
        "24323,24343,24402,22037,24715,15036,24209,24217,15048,15048,15048,"
        "15048,15048,15016,22081,22080,11002")
    # ISE types these four with an inline XML Schema namespace on every element.
    # The tag names stay unnamespaced, which is why the projection still finds
    # them, but a reader that assumed bare elements would not have been caught.
    _BOOLEAN_ATTRIBUTES = (
        ' xsi:type="xs:boolean" xmlns:xs="http://www.w3.org/2001/XMLSchema"'
        ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"')
    _BOOLEAN_TAGS = frozenset({"passed", "failed", "started", "stopped"})

    def other_attributes(self, index):
        """The 43-attribute other_attr_string, in ISE's own shape.

        The awkward parts are the point of reproducing it: a LEADING ``:!:`` so
        the first split part is empty, keys containing spaces, values containing
        ``=`` and ``;``, and both authentication latencies carried here because
        no element of the document reports them.
        """
        rule = AUTHZ_RULES[index % len(AUTHZ_RULES)]
        policy_set = self.policy_sets[index % len(self.policy_sets)]
        device = self.nads[index % self.nad_count]
        user = f"user{index % 8000:05d}"
        total_latency = 15 + index % 40
        steps = ";".join(f"{position}={(index + position) % 8}"
                         for position in range(1, 28))
        switch = f"{device['name']}-sw{index % 24:02d}"
        groups = "".join(f":!:{part}" for part in (
            "Location=Location#All Locations"
            if device["rooted"] else
            f"Location=Location#All Locations#{device['location']}",
            "Device Type=Device Type#All Device Types"
            if device["rooted"] else
            f"Device Type=Device Type#All Device Types#{device['device_type']}",
            "IPSEC=IPSEC#Is IPSEC Device",
            f"Ops Owner=Ops Owner#All Ops Owners#{device['ops_owner']}",
        ))
        return (
            ":!:ConfigVersionId=770"
            ":!:DestinationPort=1812"
            ":!:Protocol=Radius"
            f":!:NAS-Port={50000 + index % 1000}"
            ":!:Framed-MTU=1500"
            f":!:OriginalUserName={user}"
            ":!:NetworkDeviceProfileId=b0699505-3150-4215-a80e-6753d45bf56c"
            ":!:IsThirdPartyDeviceFlow=false"
            f":!:AcsSessionID={self.psns[index % len(self.psns)]}/{index}/155"
            ":!:SelectedAuthenticationIdentityStores=lab.local"
            ":!:AuthenticationStatus=AuthenticationPassed"
            ":!:IdentityPolicyMatchedRule=Dot1X"
            f":!:AuthorizationPolicyMatchedRule={rule}"
            f":!:EndPointMACAddress={mac_of(index).replace(':', '-')}"
            f":!:ISEPolicySetName={policy_set}"
            ":!:IdentitySelectionMatchedRule=Dot1X"
            f":!:StepLatency={steps}"
            f":!:AD-User-Resolved-Identities={user}@lab.local"
            f":!:AD-User-Candidate-Identities={user}@lab.local"
            f":!:TotalAuthenLatency={total_latency}"
            ":!:ClientLatency=0"
            f":!:AD-User-Resolved-DNs=CN={user},OU=Lab,DC=lab,DC=local"
            ":!:AD-User-DNS-Domain=lab.local"
            ":!:AD-User-NetBios-Name=LAB"
            ":!:IsMachineIdentity=false"
            ":!:UserAccountControl=66048"
            f":!:AD-User-SamAccount-Name={user}"
            f":!:AD-User-Qualified-Name={user}@lab.local"
            ":!:DTLSSupport=Unknown"
            ":!:Network Device Profile=Cisco"
            f"{groups}"
            ":!:IdentityAccessRestricted=false"
            ':!:StepData="5= Normalised Radius.RadiusFlowType","6=lab.local",'
            '"7=lab.local","8=' + user + '","9=lab.local","12=' + user +
            '@lab.local","19= Radius.NAS-Port-Type","20= Network Access.UserName"'
            ',"21= IdentityGroup.Name","23= Network Access.AuthenticationStatus"'
            "=StepData"
            f":!:RADIUS Username={user}"
            f":!:NAS-Identifier={switch}"
            f":!:Device IP Address={device['nas_ip']}"
            f":!:CPMSessionID={index:016X}"
            f":!:Called-Station-ID=00-11-22-33-0C-0C:{switch}"
            f":!:CiscoAVPair=audit-session-id={index:016X},"
            f"AuthenticationIdentityStore=lab.local,"
            f"FQSubjectName=968cd8c0-7b02-11f1-ad2d-8a4e8c5a954a#{user}@lab.local"
        )

    _POSTURE_STATUSES = ("Compliant", "NonCompliant", "Pending")
    _POSTURE_AGENTS = ("5.1.2.42", "5.1.3.62", "4.10.07061")
    _POSTURE_SYSTEMS = ("Windows 11", "Windows 10", "macOS 14.5")

    def _posture_elements(self, index):
        """The posture elements, empty unless this estate runs Secure Client."""
        if index % 100 >= self.posture_share * 100:
            return (("posture_status", ""),)
        status = self._POSTURE_STATUSES[index % len(self._POSTURE_STATUSES)]
        failed = status == "NonCompliant"
        return (
            ("posture_status", status),
            ("posture_report",
             "AV_Installed:Passed;Firewall_Enabled:"
             + ("Failed" if failed else "Passed")),
            ("posture_agent_version",
             self._POSTURE_AGENTS[index % len(self._POSTURE_AGENTS)]),
            ("operating_system",
             self._POSTURE_SYSTEMS[index % len(self._POSTURE_SYSTEMS)]),
        )

    def detail_fields(self, index):
        """The 43 elements /Session/MACAddress/<mac> really returns, in order.

        Not one of ``server``, ``session_state``, ``identity_group``,
        ``failure_reason``, ``posture_report``, ``posture_agent_version``,
        ``operating_system``, ``step_latency`` or ``total_authentication_latency``
        exists on the real document -- the simulator used to emit all nine, which
        made every posture and latency metric in this family look healthy while
        collecting nothing on an appliance.

        The posture elements are the one case where the lab is not the last
        word. ``posture_status`` is emitted empty because the probed estate runs
        no Secure Client, not because ISE cannot answer it, and an estate that
        does run posture populates it along with the report, the agent version
        and the endpoint OS. ``posture_share`` models that estate, so the path
        stays exercisable rather than being untestable at any scale; it defaults
        to 0.0, which reproduces the appliance exactly.
        """
        session = self.session_fields(index)
        device = self.nads[index % self.nad_count]
        passed = index % 23 != 0
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        session_key = f"{index:016X}"
        return (
            ("passed", "true" if passed else "false"),
            ("failed", "false" if passed else "true"),
            ("user_name", session["user_name"]),
            ("nas_ip_address", session["nas_ip_address"]),
            ("calling_station_id", session["calling_station_id"]),
            ("orig_calling_station_id", session["mac"].replace(":", "-")),
            ("cpmsession_id", session_key),
            ("destination_ip_address", "10.10.0.1"),
            # The switch, which differs from the NAS IP the session reports.
            ("device_ip_address", device["nas_ip"]),
            # The ISE NAD object, not the switch: on a subnet-defined NAD every
            # session on the segment reports the same value here.
            ("network_device_name", device["name"]),
            ("acs_server", session["server"]),
            ("authentication_method",
             AUTH_METHODS[index % len(AUTH_METHODS)]),
            ("authentication_protocol", "PAP_ASCII"),
            ("framed_ip_address", session["framed_ip_address"]),
            ("auth_acs_timestamp", stamp),
            ("execution_steps", self._EXECUTION_STEPS),
            ("response", f"{{Class=CACS:{session_key}; LicenseTypes=1; }}"),
            ("audit_session_id", session["audit_session_id"]),
            ("nas_port_id", f"GigabitEthernet1/0/{index % 48 + 1}"),
            *self._posture_elements(index),
            ("selected_azn_profiles",
             AUTHZ_PROFILES[index % len(AUTHZ_PROFILES)]),
            ("service_type", "Framed"),
            ("message_code",
             "5200" if passed else FAILURE_CODES[index % len(FAILURE_CODES)]),
            ("auth_acsview_timestamp", stamp),
            ("auth_id", str(1784566916869177 + index)),
            ("identity_store", "lab.local"),
            # The NDG path, not the leaf, and rooted NADs report the bare root.
            ("location", "All Locations" if device["rooted"]
             else f"All Locations#{device['location']}"),
            ("device_type", "All Device Types" if device["rooted"]
             else f"All Device Types#{device['device_type']}"),
            # Milliseconds, and the same value TotalAuthenLatency carries.
            ("response_time", str(15 + index % 40)),
            ("other_attr_string", self.other_attributes(index)),
            ("acct_id", str(1784566916869181 + index)),
            ("acct_acs_timestamp", stamp),
            ("acct_acsview_timestamp", stamp),
            ("acct_session_id", session["acct_session_id"]),
            ("acct_status_type", "Start"),
            ("acct_input_octets", str(index * 1024 % 10_000_000)),
            ("acct_output_octets", str(index * 2048 % 10_000_000)),
            ("acct_input_packets", str(index * 7 % 90_000)),
            ("acct_output_packets", str(index * 11 % 90_000)),
            ("acct_authentic", "RADIUS"),
            ("started", "true"),
            ("stopped", "false"),
            # The real profiling verdict, which nothing currently reads.
            ("endpoint_policy",
             ENDPOINT_PROFILES[index % len(ENDPOINT_PROFILES)]),
        )

    def _chunk(self, index):
        """One rendered <activeSession>, cached: churn only replaces a few."""
        cached = self._chunks.get(index)
        if cached is None:
            fields = self.session_fields(index)
            body = "".join(
                f"<{name}/>" if fields.get(name, "") == ""
                else f"<{name}>{_escape(fields[name])}</{name}>"
                for name in self._ACTIVE_FIELDS)
            cached = f"<activeSession>{body}</activeSession>".encode("utf-8")
            if len(self._chunks) > 3 * self.session_count:
                self._chunks.clear()
            self._chunks[index] = cached
        return cached

    def active_list_xml(self):
        # The root is <activeList>. The transport keys off the child tag and the
        # count attribute so both parse alike, but a simulator that names the
        # root something ISE never sends cannot be used to check the one thing a
        # root tag is for.
        header = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                  f'<activeList noOfActiveSession="{self.session_count}">')
        return b"".join(
            [header.encode("utf-8")]
            + [self._chunk(index) for index in self.active_indices()]
            + [b"</activeList>"])

    def index_of(self, mac):
        """The endpoint index behind a MAC, or None if it is not active now."""
        try:
            value = int(str(mac).replace(":", "").replace("-", ""), 16)
        except ValueError:
            return None
        index = value - 0x0A5EED000000
        return index if index in self.active_indices() else None

    def session_detail_response(self, mac):
        """(status, body) for one per-MAC detail request.

        A MAC with no current session is answered with HTTP 500 and an
        <mnt-rest-result> document, not with 200 and an empty one. That is the
        normal churn case -- a session that ended between the ActiveList read and
        its detail fetch -- and modelling it as an empty 200 meant the reader's
        empty branch looked exercised while the branch production actually takes
        was never reached at all.
        """
        index = self.index_of(mac)
        if index is None:
            return 500, (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                "<mnt-rest-result><http-code>500</http-code>"
                "<cpm-code>34110</cpm-code>"
                "<description>Server has encountered error while processing the "
                "REST request</description><module-name>MnT</module-name>"
                "<internal-error-info>Error in generating XML output. Error "
                f"message = Session data is not available for {_escape(mac)}."
                "</internal-error-info>"
                "<requested-operation>Get By Type</requested-operation>"
                "<resource-id>N/A</resource-id><resource-name>N/A</resource-name>"
                "<resource-type>RESTSDStatus</resource-type>"
                "<status>SERVER_ERROR</status></mnt-rest-result>"
            ).encode("utf-8")
        body = "".join(
            f"<{tag}{self._BOOLEAN_ATTRIBUTES if tag in self._BOOLEAN_TAGS else ''}>"
            f"{_escape(value)}</{tag}>"
            for tag, value in self.detail_fields(index))
        return 200, (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                     f"<sessionParameters>{body}</sessionParameters>"
                     ).encode("utf-8")


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
        if path == "/config/endpoint":
            return self._ers_endpoint_list(request, query)
        self.unhandled.append(f"ers{path}")
        return self._send(request, 404, b'{"error":"no such ERS resource"}',
                          "application/json")

    def _search_result(self, request, resource, rows, page, size, total):
        start = (page - 1) * size
        window = rows[start:start + size]
        result = {"total": total, "resources": window}
        # ERS OMITS nextPage on the last page and offers previousPage instead;
        # it never sends the null OpenAPI uses.
        if start + size < total:
            result["nextPage"] = {
                "rel": "next",
                "href": f"{self.base_url}/ers{resource}?size={size}&page={page + 1}",
            }
        if page > 1:
            result["previousPage"] = {
                "rel": "previous",
                "href": f"{self.base_url}/ers{resource}?size={size}&page={page - 1}",
            }
        self.clock.advance(self.latency.ers_page(), "pan")
        return self._json(request, {"SearchResult": result})

    def _ers_device_list(self, request, query):
        page, size = self._page(query)
        # id, name, description and link, and nothing else: NetworkDeviceGroupList
        # is never inline on 3.3 P11 at any page size, so the per-NAD detail
        # fan-out is mandatory rather than opportunistic.
        rows = [{"id": device["id"], "name": device["name"],
                 "description": f"{device['device_type']} at {device['location']}",
                 "link": {"rel": "self", "type": "application/json",
                          "href": f"{self.base_url}/ers/config/networkdevice/"
                                  f"{device['id']}"}}
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
            # ISE stores a base address and a mask, and the mask is real: a
            # subnet-defined NAD's ipaddress is the network address, which no
            # session's NAS IP will ever equal.
            "NetworkDeviceIPList": [
                {"ipaddress": found["ip"], "mask": found["mask"]}],
            "NetworkDeviceGroupList": self.estate.device_groups(found),
            "profileName": "Cisco",
            "coaPort": 1700,
        }})

    def _ers_user_list(self, request, query):
        page, size = self._page(query)
        rows = [{"id": account["id"], "name": account["name"]}
                for account in self.estate.accounts]
        return self._search_result(request, "/config/internaluser", rows, page,
                                   size, len(rows))

    def _ers_endpoint_list(self, request, query):
        """A paged identity-only view; never materialize the whole fake estate."""
        page, size = self._page(query)
        start = (page - 1) * size
        stop = min(self.estate.endpoint_count, start + size)
        rows = [
            {"id": f"endpoint-{index:08d}", "name": mac_of(index)}
            for index in range(start, stop)
        ]
        result = {"total": self.estate.endpoint_count, "resources": rows}
        if stop < self.estate.endpoint_count:
            result["nextPage"] = {
                "rel": "next",
                "href": (
                    f"{self.base_url}/ers/config/endpoint"
                    f"?size={size}&page={page + 1}"
                ),
            }
        self.clock.advance(self.latency.ers_page(), "pan")
        return self._json(request, {"SearchResult": result})

    def _ers_user_detail(self, request, account_id):
        self.clock.advance(self.latency.ers_detail(), "pan")
        found = next((account for account in self.estate.accounts
                      if account["id"] == account_id), None)
        if found is None:
            return self._send(request, 404, b'{"error":"no such user"}',
                              "application/json")
        index = int(account_id.rsplit("-", 1)[-1])
        # No identityGroups (an invented field), no passwordInfo object, and no
        # last-login or login-count field of any kind: the whole per-account
        # fan-out buys enabled and passwordNeverExpires, both real JSON booleans
        # at the top level.
        return self._json(request, {"InternalUser": {
            "id": found["id"],
            "name": found["name"],
            "description": "",
            "enabled": bool(found["enabled"]),
            "changePassword": index % 5 == 0,
            "passwordNeverExpires": index % 11 == 0,
            "daysForPasswordExpiration": 30 + index % 30,
            "expiryDateEnabled": index % 7 == 0,
            "expiryDate": "",
            "dateCreated": "2026-07-06",
            "dateModified": "2026-07-06",
            "customAttributes": {},
            "passwordIDStore": "Internal Users",
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
            # A bare object -- no response envelope, no version -- listing only
            # the HIGHEST installed patch. ISE does not enumerate the ones
            # beneath it, so this is one element and not SUPPORTED_PATCH_LEVEL.
            return self._json(request, {
                "iseVersion": SUPPORTED_ISE_VERSION,
                "patchVersion": [{"patchNumber": SUPPORTED_PATCH_LEVEL,
                                  "installDate": "Fri Jul 10 15:53:58 2026"}],
            })
        if path == "/license/system/tier-state":
            self.clock.advance(self.latency.openapi(), "pan")
            # EVALUATION on every tier, which is what an unlicensed deployment
            # reports and is a recognised state that is not a compliant one.
            # daysOutOfCompliance and lastAuthorization arrive as the literal
            # string "-", not as numbers and not absent.
            consumption = {"ESSENTIAL": self.estate.session_count,
                           "ADVANTAGE": 0, "PREMIER": 0, "DEVICEADMIN": 1}
            return self._json(request, [
                {"name": tier, "status": "ENABLED", "compliance": "EVALUATION",
                 "consumptionCounter": used,
                 "daysOutOfCompliance": "-", "lastAuthorization": "-"}
                for tier, used in consumption.items()
            ])
        if path == "/backup-restore/config/last-backup-status":
            self.clock.advance(self.latency.openapi(), "pan")
            # A deployment that has never been backed up answers 200 with every
            # one of the fourteen fields as explicit JSON null rather than
            # omitting them, and that is the state this resource is usually in.
            return self._json(request, {"response": {
                name: None for name in (
                    "repository", "type", "name", "startDate", "error",
                    "action", "scheduled", "status", "message", "justComplete",
                    "percentComplete", "details", "hostName", "initiatedFrom")
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
            return self._json(request, {"version": "1.0.0", "response": [
                {"default": index == 0, "id": f"ps-{index}", "name": name,
                 "description": f"{name} policy set", "hitCounts": 0,
                 "rank": index, "state": "enabled", "condition": None,
                 "serviceName": "Default Device Admin", "isProxy": False}
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
            # The rule's identity is NESTED under a "rule" sub-object; what sits
            # at the top level beside it is the rule's effect. A flat row made
            # any reader of a rule name or state look like it would work.
            extra = (
                {"identitySourceName": "All_User_ID_Stores",
                 "ifAuthFail": "REJECT", "ifUserNotFound": "REJECT",
                 "ifProcessFail": "DROP"}
                if rule_type == "authentication" else
                {"commands": [COMMAND_SETS[0]], "profile": SHELL_PROFILES[0]})
            return self._json(request, {"version": "1.0.0", "response": [
                {
                    "rule": {
                        "default": rule == count - 1,
                        "id": f"{policy_id}-{rule_type}-{rule}",
                        "name": f"{rule_type}-{rule}",
                        "hitCounts": 0,
                        "rank": rule,
                        "state": "enabled",
                        "condition": None,
                    },
                    **extra,
                }
                for rule in range(count)
            ]})
        # Both lists are BARE JSON, with no response envelope, and ISE mirrors
        # its deny-all command set into the shell-profile list under the same
        # id -- so the two lists overlap and counting the profile list verbatim
        # reports a shell profile that does not exist.
        if path == "/policy/device-admin/command-sets":
            self.clock.advance(self.latency.openapi(), "pan")
            return self._json(request, self._command_sets())
        if path == "/policy/device-admin/shell-profiles":
            self.clock.advance(self.latency.openapi(), "pan")
            mirrored = [row for row in self._command_sets()
                        if row["name"].startswith("Deny")]
            return self._json(request, [
                {"id": f"sp-{index}", "name": name}
                for index, name in enumerate(SHELL_PROFILES)] + mirrored)
        self.unhandled.append(f"api/v1{path}")
        return self._send(request, 404, b'{"error":"no such OpenAPI resource"}',
                          "application/json")

    def _certificates(self, request, query, resource, rows):
        page, size = self._page(query)
        start = (page - 1) * size
        window = rows[start:start + size]
        self.clock.advance(self.latency.openapi(len(window)), "pan")
        # OpenAPI signals the last page with an explicit nextPage: null, where
        # ERS omits the key -- two different terminations on one appliance.
        payload = {"response": window, "nextPage": None, "version": "1.0.1"}
        if start + size < len(rows):
            payload["nextPage"] = {
                "rel": "next",
                "href": f"{self.base_url}/api/v1{resource}?size={size}&page={page + 1}",
            }
        return self._json(request, payload)

    def _command_sets(self):
        return [{"name": name, "id": f"cs-{index}"}
                for index, name in enumerate(COMMAND_SETS)]

    def _certificate(self, name, node, days, **fields):
        expires = datetime.now(timezone.utc) + timedelta(days=days)
        return {
            "id": f"cert-{node}-{name}",
            "friendlyName": f"{name} ({node})",
            "expirationDate": expires.strftime("%a %b %d %H:%M:%S UTC %Y"),
            "issuedTo": node,
            "issuedBy": "example-issuing-ca",
            "signatureAlgorithm": "SHA256withRSA",
            "sha256Fingerprint": f"{abs(hash((node, name))):064x}"[:64],
            **fields,
        }

    # The system store's usage is a comma-joined multi-value whose member order
    # differs between nodes for the same set, so the same logical usage mints a
    # different label on each node unless the reader sorts it. A single-valued
    # enum hid that entirely.
    _SYSTEM_USAGES = (
        ("Admin", "EAP Authentication", "Portal", "RADIUS DTLS"),
        ("pxGrid",), ("Portal",), ("ISE Messaging Service",),
        ("SAML",), ("Not in use",), ("EAP Authentication",),
    )

    def _system_certificates(self, node):
        offset = sum(ord(character) for character in node)
        rows = []
        for position, usage in enumerate(self._SYSTEM_USAGES):
            members = list(usage)
            if offset % 2:
                members.reverse()
            rows.append(self._certificate(
                f"system-{position}", node, 30 + (offset + position * 97) % 700,
                keySize=2048, selfSigned=False, groupTag="",
                usedBy=", ".join(members)))
        return rows

    def _trusted_certificates(self):
        # No selfSigned, no usedBy, and keySize as a STRING: the trusted store
        # is a different shape from the system store, which the reader has to
        # survive rather than merely happen to.
        return [self._certificate(
            f"trusted-ca-{index:03d}", "trust_store", 45 + (index * 37) % 3000,
            keySize="2048", status="Enabled",
            trustedFor="Infrastructure,Cisco Services"
            if index % 3 else "Cisco Services")
            for index in range(180)]

    # --- MnT ------------------------------------------------------------

    def _mnt(self, request, path):
        if path == "/Session/ActiveCount":
            self.clock.advance(self.latency.mnt_detail(), "mnt")
            body = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                f"<sessionCount><count>{self.estate.session_count}</count>"
                "</sessionCount>"
            ).encode("utf-8")
            return self._send(request, 200, body, "application/xml")
        if path == "/Session/ActiveList":
            self.clock.advance(
                self.latency.mnt_active_list(self.estate.session_count), "mnt")
            return self._send(request, 200, self.estate.active_list_xml(),
                              "application/xml")
        if path.startswith("/Session/MACAddress/"):
            self.clock.advance(self.latency.mnt_detail(), "mnt")
            mac = path.rsplit("/", 1)[-1]
            status, body = self.estate.session_detail_response(mac)
            return self._send(request, status, body, "application/xml")
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
        if str(sql).upper().startswith("ALTER SESSION"):
            self.description = None
            self._rows = []
            self._position = 0
            return
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

    def __init__(self, config, estate, clock, latency, *, empty_views=()):
        super().__init__(config)
        self.estate = estate
        self.clock = clock
        self.lane = LaneClock(clock)
        self.latency = latency
        self.statements = []
        # Views to answer with no rows. An empty reporting view is a real and
        # common appliance state -- seven of them are empty on the lab -- and it
        # is the one answer a synthesiser that reads the statement's own SELECT
        # list can never produce on its own, which is why it is declared here.
        self.empty_views = frozenset(name.lower() for name in empty_views)
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

        columns, rows = synthesize(text, self.estate, parameters,
                                   empty_views=self.empty_views)
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


# Columns that exist in these views and are NULL on every row of a 3.3 P11
# appliance, with the placeholder the dataset's own NVL substitutes. Keyed by
# view because the same dimension name is live in one and dead in another:
# radius_reporting's `policy` is AUTHORIZATION_RULE and carries values,
# radius_accounting's is AUTHORIZATION_POLICY and never does. A synthesiser that
# manufactures a value domain for these reports a breakdown where the appliance
# publishes one placeholder series.
EMPTY_DIMENSIONS = {
    "endpoints_data": {"identity_group": "none"},
    "profiled_endpoints_summary": {"action": "unknown"},
    "radius_accounting": {"policy": "unknown"},
    "radius_authentication_summary": {"security_group": "unknown"},
}

# The views this appliance answers with no rows at all, for a run that wants the
# lab's shape rather than a populated production one.
LAB_EMPTY_VIEWS = frozenset({
    "radius_errors_view", "system_diagnostics_view",
    "posture_assessment_by_endpoint", "posture_assessment_by_condition",
    "tacacs_authentication_last_two_days", "tacacs_authorization_last_two_days",
    "tacacs_accounting_last_two_days",
})


class Domains:
    """What a grouped column's values are, and how many there are.

    Row counts come from the statement's own GROUP BY (or GROUPING SETS), so a
    statement that breaks down by NAD gets one row per NAD at the configured
    scale. That is the point: the row ceiling, the byte ceiling, the truncation
    signal and the resulting Prometheus cardinality are then all exercised at
    the size the exporter claims to support.

    ``empty`` names the dimensions this statement's view carries and never
    populates; each answers with its single placeholder value.
    """

    def __init__(self, estate, empty=None):
        self.estate = estate
        self.empty = empty or {}

    def values(self, name):
        estate = self.estate
        name = name.lower()
        if name in self.empty:
            return [self.empty[name]]
        if name in ("ise_node", "node", "psn", "server", "acs_server"):
            return list(estate.psns)
        if name in ("nad", "network_device_name", "device_name", "device",
                    "nas_name", "nas_identifier"):
            return [device["name"] for device in estate.nads]
        if name in ("nas_ip_address", "nas_ipv4_address", "ip_address"):
            return [device["nas_ip"] for device in estate.nads]
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


_FROM_VIEW = re.compile(r"\bfrom\s+([A-Za-z_][A-Za-z0-9_$#]*)", re.I)


def from_views(text):
    """Every table name this statement selects from, lowercased."""
    return {match.group(1).lower() for match in _FROM_VIEW.finditer(text)}


def _empty_row(columns, expressions, literals):
    """The single row Oracle returns for a bare aggregate over an empty view.

    COUNT and SUM answer 0, every other aggregate and every plain column answers
    NULL, and a literal in the SELECT list is still a literal. A GROUP BY over
    the same view returns no rows at all -- the two shapes are different and the
    exporter has to survive both, which is the whole reason for modelling this.
    """
    row = []
    for name, expression in zip(columns, expressions):
        if name in literals:
            row.append(literals[name])
            continue
        found = _AGGREGATE.search(expression)
        row.append(0 if found and found.group(1).lower() in ("sum", "count")
                   else None)
    return tuple(row)


def _marginal_rows(text, columns, estate, limit, offset, numeric=(), empty=None):
    """One row per (dimension, value) pair across every GROUPING SET.

    This is `reporting.marginals`: the breakdowns add rather than multiply, and
    the whole set is bounded by one FETCH FIRST. Reproducing it faithfully is
    what makes the truncation signal (returned versus existing) meaningful at
    scale -- at 5,000 NADs some of these genuinely do not fit.
    """
    domains = Domains(estate, empty)
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


def synthesize(text, estate, parameters=None, empty_views=()):
    """Answer one statement from its own shape.

    The SELECT list gives the columns, the GROUP BY / GROUPING SETS / PARTITION
    BY gives the row count, and everything left over becomes a measure.

    ``empty_views`` is the one thing that cannot be read off the statement: a
    view holding no rows looks identical to a busy one from its SQL, so it has
    to be declared. Each UNION branch is decided on its own view, which is what
    makes a freshness probe over a mix of empty and populated views work.
    """
    del parameters
    branches = _split_on_keyword(text, " union all ")
    if len(branches) > 1:
        columns, rows = [], []
        for branch in branches:
            branch_columns, branch_rows = synthesize(
                branch, estate, empty_views=empty_views)
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
    views = from_views(text)
    is_empty = bool(views & frozenset(empty_views))
    empty_dimensions = {}
    for view in views:
        empty_dimensions.update(EMPTY_DIMENSIONS.get(view, {}))
    if "grouping sets" in text.lower():
        if is_empty:
            return columns, []
        return columns, _marginal_rows(
            text, columns, estate, limit, offset, numeric, empty_dimensions)

    if is_empty:
        # A GROUP BY over an empty view returns nothing; a bare aggregate still
        # returns its one row of zeros and NULLs. The test is the clause and not
        # whether the grouped columns resolved, so a grouping expression this
        # parser cannot follow does not silently fall back to the wrong shape.
        if _clause(text, "group by") or (inner and _clause(inner, "group by")):
            return columns, []
        return columns, [_empty_row(columns, expressions, literals)]

    grouped = _grouped_columns(text, inner, expressions, columns)
    if not grouped:
        return columns, [
            _fill(columns, {**literals, "group_total": 1}, 0, estate, numeric)]

    domains = Domains(estate, empty_dimensions)
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
