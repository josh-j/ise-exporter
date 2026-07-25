#!/usr/bin/env python
"""Seed the lab with tagged NADs and internal users, or remove them again.

Everything created here is named `ise3-sim-*` and described as safe to delete,
so the cleanup is a name filter rather than a list someone has to keep. The
point is to exercise paths the 4-NAD lab cannot reach:

- ERS enumeration paging (100 rows a page, so 500 devices is five pages) and the
  cross-page duplicate handling `network_devices` carries a `seen` set for;
- the converging detail cache actually converging over several cycles, since
  WARMUP_FETCHES_PER_CYCLE is 500 for devices and 100 for accounts;
- `tacacs_config`'s account sweep against more than one account.

It does NOT manufacture RADIUS or TACACS *events*: those come from real
authentications, so the reporting views stay near-empty regardless.
"""
from __future__ import annotations

import argparse
import sys
import urllib3
import requests
from requests.auth import HTTPBasicAuth

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PREFIX = "ise3-sim"
NOTE = "ise-exporter3 scale probe - safe to delete"


def session(host, password):
    made = requests.Session()
    made.auth = HTTPBasicAuth("admin", password)
    made.verify = False
    made.trust_env = False
    made.headers.update({"Content-Type": "application/json",
                         "Accept": "application/json"})
    made.base = f"https://{host}:9060/ers"
    return made


def device_body(index):
    # 10.99.a.b, unique per device and outside anything the lab routes.
    a, b = divmod(index - 1, 250)
    return {"NetworkDevice": {
        "name": f"{PREFIX}-nad-{index:04d}",
        "description": NOTE,
        "NetworkDeviceIPList": [{"ipaddress": f"10.99.{a}.{b + 1}", "mask": 32}],
        "authenticationSettings": {"networkProtocol": "RADIUS",
                                   "radiusSharedSecret": "ise3simprobe"},
    }}


def user_body(index):
    return {"InternalUser": {
        "name": f"{PREFIX}-user-{index:03d}",
        "description": NOTE,
        "password": "Ise3Probe!2026x",
        "enabled": True,
        "changePassword": False,
    }}


def create(made, path, body, label):
    response = made.post(f"{made.base}/config/{path}", json=body, timeout=60)
    if response.status_code in (200, 201):
        return "created"
    if response.status_code == 400 and "already exist" in response.text.lower():
        return "exists"
    if response.status_code == 409:
        return "exists"
    print(f"  {label}: HTTP {response.status_code} {response.text[:180]}")
    return "failed"


def page_all(made, path):
    """Every resource of one kind, following ERS's own paging."""
    found, page = [], 1
    while True:
        response = made.get(f"{made.base}/config/{path}",
                            params={"size": 100, "page": page}, timeout=60)
        response.raise_for_status()
        resources = response.json().get("SearchResult", {}).get("resources", [])
        if not resources:
            return found
        found.extend(resources)
        page += 1
        if page > 200:
            return found


def main(argv=None):
    parser = argparse.ArgumentParser(prog="seed_lab")
    parser.add_argument("--host", required=True,
                        help="the lab appliance; deliberately not defaulted, "
                             "because this is the one tool here that writes")
    parser.add_argument("--password", required=True)
    parser.add_argument("--nads", type=int, default=500)
    parser.add_argument("--users", type=int, default=250)
    parser.add_argument("--remove", action="store_true",
                        help=f"delete everything named {PREFIX}-* instead")
    arguments = parser.parse_args(argv)
    made = session(arguments.host, arguments.password)

    if arguments.remove:
        for path, kind in (("networkdevice", "NAD"), ("internaluser", "user")):
            targets = [r for r in page_all(made, path)
                       if str(r.get("name", "")).startswith(PREFIX)]
            removed = 0
            for resource in targets:
                response = made.delete(
                    f"{made.base}/config/{path}/{resource['id']}", timeout=60)
                if response.status_code in (200, 204):
                    removed += 1
                else:
                    print(f"  {kind} {resource['name']}: "
                          f"HTTP {response.status_code}")
            print(f"{kind}s removed: {removed} of {len(targets)}")
        return 0

    for kind, count, path, builder in (
        ("NAD", arguments.nads, "networkdevice", device_body),
        ("user", arguments.users, "internaluser", user_body),
    ):
        tally = {"created": 0, "exists": 0, "failed": 0}
        for index in range(1, count + 1):
            tally[create(made, path, builder(index),
                         f"{kind} {index}")] += 1
            if index % 100 == 0:
                print(f"  {kind} {index}/{count} {tally}", flush=True)
        print(f"{kind}s: {tally}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
