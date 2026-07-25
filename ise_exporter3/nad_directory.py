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
"""
from __future__ import annotations

from threading import Lock

from prometheus_client import Gauge


entries = Gauge(
    "ise3_nad_directory_entries",
    "NAD identities resolvable to a group classification")
attributed = Gauge(
    "ise3_nad_directory_attributed",
    "Session-bearing NADs matched to an inventory entry", ["matched"])


class NadDirectory:
    """NAS IP or device name to (nad, location, ops_owner)."""

    def __init__(self):
        self._by_key = {}
        self._lock = Lock()

    def replace(self, records):
        """Swap in a whole classification set.

        Replacement rather than merge: an entry that survived a NAD's removal
        from ERS would keep attributing live sessions to an owner that no longer
        owns anything.
        """
        table = {}
        for record in records:
            classification = (record["nad"], record["location"], record["ops_owner"])
            for key in record.get("keys", ()):
                if key:
                    table[str(key).strip().lower()] = classification
        with self._lock:
            self._by_key = table
        entries.set(len(table))
        return len(table)

    def lookup(self, *keys):
        """Resolve the first key that matches; unknown is a normal answer."""
        with self._lock:
            for key in keys:
                if not key:
                    continue
                found = self._by_key.get(str(key).strip().lower())
                if found:
                    return found
        return None

    def ops_owner(self, *keys):
        found = self.lookup(*keys)
        return found[2] if found else "unknown"

    def classifications(self):
        """Every distinct (nad, location, ops_owner) currently known."""
        with self._lock:
            return set(self._by_key.values())

    def __len__(self):
        with self._lock:
            return len(self._by_key)


_DIRECTORY = NadDirectory()


def shared():
    return _DIRECTORY


def record_attribution(matched, unmatched):
    attributed.labels(matched="yes").set(matched)
    attributed.labels(matched="no").set(unmatched)
