"""Shared NAD classification, published by inventory and read by session data.

`ops_owner` is the label every dashboard groups by, and it exists only in ERS
network-device groups. Session data arrives from MnT carrying a NAS IP and, at
best, a device name -- so something has to join the two.

This is that join, and it is deliberately small: one process-local map from NAS
IP and device name to the classification the inventory dataset already resolved.
It holds no history and survives nothing; a restart rebuilds it from the next
inventory collection.

Two rules keep this from becoming the cross-collector coupling v2 warned about:

- exactly one dataset writes it (``network_devices``), and it replaces the whole
  directory rather than merging, so a NAD that left ERS stops being attributed;
- readers treat a miss as ``unknown`` and carry on. A session whose NAD is not in
  the inventory is still counted -- it just cannot be attributed to an owner,
  which is itself worth seeing.

A NAD is frequently configured in ISE as a subnet rather than a host --
``10.200.40.0/24`` covering every access switch on that segment -- and the NAS IP
a session arrives with is one address inside it, never the network address. So a
key that carries a prefix is held as a network and matched by containment,
longest prefix first; everything else stays an exact key.
"""
from __future__ import annotations

import ipaddress
from threading import Lock

from prometheus_client import Gauge


entries = Gauge(
    "ise3_nad_directory_entries",
    "NAD identities resolvable to a group classification")
attributed = Gauge(
    "ise3_nad_directory_attributed",
    "Session-bearing NADs matched to an inventory entry", ["matched"])


def _as_network(key):
    """The network a key covers, or None if it is not an address range.

    A bare address parses as a host network, which stays an exact key -- the
    lookup never has to scan for it. A /0 is refused: a NAD configured as the
    default route would attribute every session in the estate to one owner.
    """
    try:
        network = ipaddress.ip_network(key, strict=False)
    except ValueError:
        return None
    if network.prefixlen in (0, network.max_prefixlen):
        return None
    return network


def _match_network(key, networks):
    """Containment match for a NAS IP against subnet-configured NADs."""
    if not networks or not ("." in key or ":" in key):
        return None
    try:
        address = ipaddress.ip_address(key)
    except ValueError:
        return None
    for network, classification in networks:
        if address.version == network.version and address in network:
            return classification
    return None


class NadDirectory:
    """NAS IP, NAS subnet or device name to (nad, location, ops_owner)."""

    def __init__(self):
        self._by_key = {}
        self._networks = ()
        self._lock = Lock()

    def replace(self, records):
        """Swap in a whole classification set.

        Replacement rather than merge: an entry that survived a NAD's removal
        from ERS would keep attributing live sessions to an owner that no longer
        owns anything.
        """
        table, networks = {}, []
        for record in records:
            classification = (record["nad"], record["location"], record["ops_owner"])
            for key in record.get("keys", ()):
                if not key:
                    continue
                text = str(key).strip().lower()
                if not text:
                    continue
                network = _as_network(text)
                if network is not None:
                    networks.append((network, classification))
                    continue
                if "." in text or ":" in text:
                    try:
                        text = str(ipaddress.ip_network(
                            text, strict=False).network_address)
                    except ValueError:
                        pass    # a device name that merely looks addressy
                table[text] = classification
        # Longest prefix first, so a /28 carve-out wins over the /16 around it.
        networks.sort(key=lambda entry: entry[0].prefixlen, reverse=True)
        with self._lock:
            self._by_key, self._networks = table, tuple(networks)
        entries.set(len(table) + len(networks))
        return len(table) + len(networks)

    def lookup(self, *keys):
        """Resolve the first key that matches; unknown is a normal answer."""
        with self._lock:
            table, networks = self._by_key, self._networks
        for key in keys:
            if not key:
                continue
            text = str(key).strip().lower()
            found = table.get(text)
            if found:
                return found
            found = _match_network(text, networks)
            if found:
                return found
        return None

    def ops_owner(self, *keys):
        found = self.lookup(*keys)
        return found[2] if found else "unknown"

    def classifications(self):
        """Every distinct (nad, location, ops_owner) currently known."""
        with self._lock:
            return set(self._by_key.values()) | {
                classification for _, classification in self._networks}

    def __len__(self):
        with self._lock:
            return len(self._by_key) + len(self._networks)


_DIRECTORY = NadDirectory()


def shared():
    return _DIRECTORY


def record_attribution(matched, unmatched):
    attributed.labels(matched="yes").set(matched)
    attributed.labels(matched="no").set(unmatched)
