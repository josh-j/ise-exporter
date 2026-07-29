#!/usr/bin/env python3
"""Put real RADIUS sessions on a lab appliance, so session code can be tested.

Most of the exporter's session handling could not be tested against the lab at
all, because an idle lab has no sessions: eleven checks in
``tests/test_v3_lab_facts.py`` skip whenever nothing is connected, which is
nearly always. This makes something to connect.

It found a real bug the first time it ran. ``project_session`` read the device
name from ``nasName``; ISE 3.3 sends ``nasIdentifier``, and the NAD column came
back empty on every row of a live deployment. A field name that is wrong does
not fail -- it returns an empty column -- so the only way to catch it is to look
at a record the appliance actually sent.

    export ISE_LAB_TEST_PASSWORD='...'          # for the internal user it makes
    python tools/lab_sessions3.py setup         # endpoints + internal user
    python tools/lab_sessions3.py start         # authenticate and open sessions
    python tools/lab_sessions3.py stop          # close them
    python tools/lab_sessions3.py teardown      # remove what setup created

**Lab only, and it writes.** It creates endpoints and an internal user through
ERS and sends RADIUS as a configured NAD. Nothing here belongs near production.

Two things it is not. It is not an 802.1X supplicant: authentication is PAP
carrying Service-Type=Framed, which is what the stock ``Wired_802.1X`` condition
matches, so the sessions it makes lack the EAP exchange, the policy step
latencies and the ``other_attr_string`` shape that real sessions carry. Five of
the MnT checks expect those and fail against this traffic -- run ``stop`` before
the lab suite, or accept that they are measuring the generator rather than the
appliance. And it is not a load generator; ``simulate_scale3.py`` is that.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import random
import socket
import struct
import sys
import warnings

try:
    import requests
    import urllib3
except ImportError:                              # pragma: no cover - lab only
    sys.exit("this tool needs requests; run it from the project venv")

warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)

ACCESS_REQUEST, ACCESS_ACCEPT = 1, 2
ACCOUNTING_REQUEST, ACCOUNTING_RESPONSE = 4, 5
ACCT_START, ACCT_STOP = 1, 2

ATTRIBUTES = {
    "User-Name": 1, "User-Password": 2, "NAS-IP-Address": 4, "NAS-Port": 5,
    "Service-Type": 6, "Framed-IP-Address": 8, "Called-Station-Id": 30,
    "Calling-Station-Id": 31, "NAS-Identifier": 32, "Acct-Status-Type": 40,
    "Acct-Session-Id": 44, "NAS-Port-Type": 61, "NAS-Port-Id": 87,
}

DESCRIPTION = "ise-exporter3 lab session generator; safe to delete"


# --- RADIUS ------------------------------------------------------------------

def _cisco_avpair(text):
    """VSA 26 / vendor 9 / sub-attribute 1.

    How ISE learns ``audit-session-id``. Without it MnT's ActiveList omits the
    field entirely, which is a difference from real switch traffic and shows up
    as a failing contract test rather than as anything obvious.
    """
    inner = struct.pack(">BB", 1, len(text) + 2) + text.encode()
    body = struct.pack(">I", 9) + inner
    return struct.pack(">BB", 26, len(body) + 2) + body


def _encode(attributes):
    out = b""
    for name, value in attributes:
        if name == "Cisco-AVPair":
            out += _cisco_avpair(value)
            continue
        code = ATTRIBUTES[name]
        if isinstance(value, int):
            value = struct.pack(">I", value)
        elif name.endswith("IP-Address"):
            value = socket.inet_aton(value)
        else:
            value = str(value).encode()
        out += struct.pack(">BB", code, len(value) + 2) + value
    return out


def _hide(password, secret, authenticator):
    """RFC 2865 User-Password obfuscation."""
    padded = password.encode()
    padded += b"\x00" * (-len(padded) % 16)
    out, last = b"", authenticator
    for offset in range(0, len(padded), 16):
        digest = hashlib.md5(secret + last).digest()
        block = bytes(a ^ b for a, b in zip(padded[offset:offset + 16], digest))
        out += block
        last = block
    return out


def _exchange(packet, args, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((args.nas_ip, 0))
    sock.settimeout(args.timeout)
    try:
        sock.sendto(packet, (args.radius_host, port))
        reply, _ = sock.recvfrom(8192)
        return reply[0]
    except socket.timeout:
        return None
    finally:
        sock.close()


def _base(args, mac, index, session_id):
    return [
        ("NAS-IP-Address", args.nas_ip),
        ("NAS-Port", 50100 + index),
        ("NAS-Port-Id", f"GigabitEthernet1/0/{index}"),
        ("NAS-Port-Type", 15),                   # Ethernet
        ("Calling-Station-Id", mac),
        ("Called-Station-Id", "00:11:22:33:44:55"),
        ("NAS-Identifier", args.nad_name),
        ("Acct-Session-Id", session_id),
        ("Cisco-AVPair", f"audit-session-id={session_id}"),
    ]


def authenticate(args, secret, mac, index, session_id):
    """PAP with Service-Type=Framed, which Wired_802.1X matches on."""
    authenticator = os.urandom(16)
    attributes = [("User-Name", args.user), ("Service-Type", 2)] \
        + _base(args, mac, index, session_id)
    hidden = _hide(args.password, secret, authenticator)
    body = _encode(attributes) + struct.pack(
        ">BB", ATTRIBUTES["User-Password"], len(hidden) + 2) + hidden
    packet = struct.pack(
        ">BBH", ACCESS_REQUEST, random.randint(0, 255), 20 + len(body)) \
        + authenticator + body
    return _exchange(packet, args, args.auth_port)


def account(args, secret, mac, index, session_id, status):
    attributes = [("User-Name", args.user), ("Acct-Status-Type", status),
                  ("Framed-IP-Address", f"10.200.40.{210 + index}")] \
        + _base(args, mac, index, session_id)
    body = _encode(attributes)
    header = struct.pack(
        ">BBH", ACCOUNTING_REQUEST, random.randint(0, 255), 20 + len(body))
    authenticator = hashlib.md5(header + b"\x00" * 16 + body + secret).digest()
    return _exchange(header + authenticator + body, args, args.acct_port)


# --- ERS ---------------------------------------------------------------------

def _session(args):
    http = requests.Session()
    http.auth = requests.auth.HTTPBasicAuth(args.admin_user, args.admin_password)
    http.verify = False
    http.trust_env = False
    return http


def _ers(args, http, method, path, **kwargs):
    return http.request(
        method, f"https://{args.ise_host}:9060/ers/config/{path}",
        headers={"Accept": "application/json",
                 "Content-Type": "application/json"},
        timeout=args.timeout, **kwargs)


def setup(args):
    http = _session(args)
    for mac in args.macs:
        found = _ers(args, http, "GET", f"endpoint?filter=mac.EQ.{mac}")
        if found.ok and found.json().get("SearchResult", {}).get("resources"):
            print(f"  endpoint {mac} already present")
            continue
        created = _ers(args, http, "POST", "endpoint", json={"ERSEndPoint": {
            "mac": mac, "description": DESCRIPTION,
            "staticProfileAssignment": False, "staticGroupAssignment": False}})
        print(f"  endpoint {mac}: {created.status_code}")

    found = _ers(args, http, "GET", f"internaluser?filter=name.EQ.{args.user}")
    if found.ok and found.json().get("SearchResult", {}).get("resources"):
        print(f"  user {args.user} already present")
        return
    created = _ers(args, http, "POST", "internaluser", json={"InternalUser": {
        "name": args.user, "password": args.password, "enabled": True,
        "changePassword": False, "description": DESCRIPTION}})
    print(f"  user {args.user}: {created.status_code}"
          + ("" if created.ok else f" {created.text[:160]}"))


def teardown(args):
    http = _session(args)
    for mac in args.macs:
        found = _ers(args, http, "GET", f"endpoint?filter=mac.EQ.{mac}")
        for row in (found.json().get("SearchResult", {}).get("resources", [])
                    if found.ok else []):
            gone = _ers(args, http, "DELETE", f"endpoint/{row['id']}")
            print(f"  endpoint {mac}: {gone.status_code}")
    found = _ers(args, http, "GET", f"internaluser?filter=name.EQ.{args.user}")
    for row in (found.json().get("SearchResult", {}).get("resources", [])
                if found.ok else []):
        gone = _ers(args, http, "DELETE", f"internaluser/{row['id']}")
        print(f"  user {args.user}: {gone.status_code}")


# --- sessions ----------------------------------------------------------------

def start(args):
    secret = args.radius_secret.encode()
    for index, mac in enumerate(args.macs, 1):
        session_id = f"0B0B0B0B0000{index:04X}"
        code = authenticate(args, secret, mac, index, session_id)
        if code != ACCESS_ACCEPT:
            print(f"  {mac} auth -> "
                  + ("timeout" if code is None else f"code {code}")
                  + "  (check the policy set and the identity store)")
            continue
        reply = account(args, secret, mac, index, session_id, ACCT_START)
        print(f"  {mac} auth -> Access-Accept, acct -> "
              + ("Accounting-Response" if reply == ACCOUNTING_RESPONSE
                 else f"code {reply}"))


def stop(args):
    secret = args.radius_secret.encode()
    for index, mac in enumerate(args.macs, 1):
        reply = account(args, secret, mac, index,
                        f"0B0B0B0B0000{index:04X}", ACCT_STOP)
        print(f"  {mac} acct-stop -> "
              + ("Accounting-Response" if reply == ACCOUNTING_RESPONSE
                 else f"code {reply}"))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action",
                        choices=("setup", "start", "stop", "teardown"))
    parser.add_argument("--ise-host", default=os.environ.get(
        "ISE_LAB_HOST", "laba-ise-001.ise.lab"))
    parser.add_argument("--radius-host", default=os.environ.get(
        "ISE_LAB_RADIUS_HOST", "10.200.30.10"))
    parser.add_argument("--nas-ip", default=os.environ.get(
        "ISE_LAB_NAS_IP", "10.200.30.1"),
        help="must match a configured network device in ISE")
    parser.add_argument("--nad-name", default=os.environ.get(
        "ISE_LAB_NAD_NAME", "ise-exporter-live-test"))
    parser.add_argument("--user", default=os.environ.get(
        "ISE_LAB_TEST_USER", "ise-exporter-test-user"))
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--auth-port", type=int, default=1812)
    parser.add_argument("--acct-port", type=int, default=1813)
    parser.add_argument("--timeout", type=int, default=10)
    args = parser.parse_args(argv)

    args.macs = [f"02:1E:5E:00:00:{index:02X}"
                 for index in range(1, args.count + 1)]
    args.password = os.environ.get("ISE_LAB_TEST_PASSWORD", "")
    args.radius_secret = os.environ.get("ISE_LAB_RADIUS_SECRET", "")
    args.admin_user = os.environ.get("ISE_LAB_USER", "admin")
    args.admin_password = os.environ.get("ISE_LAB_ADMIN_PASSWORD", "")

    if args.action in ("setup", "teardown") and not args.admin_password:
        sys.exit("set ISE_LAB_ADMIN_PASSWORD for the ERS calls")
    if args.action == "setup" and not args.password:
        sys.exit("set ISE_LAB_TEST_PASSWORD for the internal user")
    if args.action in ("start", "stop") and not args.radius_secret:
        sys.exit("set ISE_LAB_RADIUS_SECRET to the NAD's shared secret")
    if args.action == "start" and not args.password:
        sys.exit("set ISE_LAB_TEST_PASSWORD to authenticate as the test user")

    {"setup": setup, "start": start,
     "stop": stop, "teardown": teardown}[args.action](args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
