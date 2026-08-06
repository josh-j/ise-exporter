"""Per-entity detail cache for facts that are stable while their subject lives.

The authorization decision for a session does not change while that session
lasts. Neither does the policy set it matched, the rule it hit, or the method it
authenticated with. So the expensive part -- one MnT request per MAC -- only has
to happen **once per session**, not once per collection.

That single observation is what turns a per-cycle request budget from a coverage
ceiling into a warm-up rate limit:

- a cold cache fills at the budget rate and coverage climbs to 1.0;
- once warm, only MACs that appeared since the last cycle need fetching, which
  is session churn rather than fleet size;
- entries for MACs that left the active list are dropped, so the cache tracks
  the active set rather than growing without bound.

Coverage is exported, so "the number is low" and "the cache is still filling"
are distinguishable, and an operator can watch it converge instead of wondering
whether a panel is lying.

The cache is in memory on purpose. Persisting it would trade a bounded warm-up
for permanent on-disk state, and this exporter keeps no history on disk.
Re-warming after a restart is visible in the coverage metric and costs minutes.
"""
from __future__ import annotations

import time
from threading import Lock

from prometheus_client import Counter, Gauge


entries = Gauge(
    "ise3_detail_cache_entries", "Cached per-entity detail records", ["cache"])
coverage = Gauge(
    "ise3_detail_cache_coverage",
    "Fraction of the active set whose detail is cached; 1.0 is fully warm",
    ["cache"])
fetches_total = Counter(
    "ise3_detail_fetches_total", "Per-entity detail fetches by result",
    ["cache", "result"])
deferred = Gauge(
    "ise3_detail_fetches_deferred",
    "Entities left for the next cycle by the warm-up budget", ["cache"])


class DetailCache:
    """MAC (or other stable key) to a bounded detail record."""

    def __init__(self, name, ttl_seconds=86400):
        self.name = name
        self.ttl = ttl_seconds
        self._entries = {}
        self._lock = Lock()

    def get(self, key, *, now=None):
        now = time.time() if now is None else now
        with self._lock:
            entry = self._entries.get(key)
            if entry and now - entry["at"] < self.ttl:
                return entry["detail"]
        return None

    def put(self, key, detail, *, now=None):
        now = time.time() if now is None else now
        with self._lock:
            self._entries[key] = {"detail": detail, "at": now}

    def retain(self, active, grace_seconds=0, *, now=None):
        """Drop entries whose subject is no longer active. Returns how many.

        A subject that left is held for ``grace_seconds`` first. The expensive
        fetch is per *arrival*, and on a wireless estate most arrivals are the
        same device coming back -- a roam between controllers, a sleep/wake, a
        session that straddled a cycle boundary. Dropping it the moment it
        blinks means paying full price again for a decision that did not change.
        Staleness stays bounded by the TTL, which applies to held entries too.
        """
        now = time.time() if now is None else now
        active = set(active)
        with self._lock:
            dropped = 0
            for key, entry in list(self._entries.items()):
                if key in active:
                    entry.pop("gone_at", None)
                    continue
                gone_at = entry.setdefault("gone_at", now)
                if now - gone_at >= grace_seconds:
                    del self._entries[key]
                    dropped += 1
            return dropped

    def resident(self):
        """Entries whose subject is currently active, not merely held."""
        with self._lock:
            return sum(1 for entry in self._entries.values()
                       if "gone_at" not in entry)

    def uncached(self, keys, *, now=None):
        now = time.time() if now is None else now
        return [key for key in keys if self.get(key, now=now) is None]

    def __len__(self):
        with self._lock:
            return len(self._entries)

    def publish(self, active_count, deferred_count=0):
        """Export size, coverage and what the budget left for next time."""
        entries.labels(cache=self.name).set(len(self))
        deferred.labels(cache=self.name).set(deferred_count)
        # Coverage counts only entries whose subject is active. Entries held
        # through the grace window are real memory, so they belong in the size
        # gauge, but counting them here would report a warm cache while active
        # endpoints were still missing from it.
        cached = self.resident()
        covered = min(cached, active_count) / active_count if active_count else 1.0
        coverage.labels(cache=self.name).set(covered)
        return covered

    def count(self, result):
        fetches_total.labels(cache=self.name, result=result).inc()


# A cache's fetch counter only gains a child when its first fetch resolves, and
# a labelled family with no children exports no samples at all -- so a cache
# that has simply not fetched anything yet reads on a dashboard exactly like an
# exporter that is down. Seeding the children at startup makes "zero fetches so
# far" a visible fact. The table names, per cache, the (dataset, provider)
# pairs whose call sites tick it and the bounded result vocabulary those sites
# can emit; grep the dataset for ``cache.count(`` when changing either side.
FETCH_OUTCOMES = (
    ("mnt_active_list",
     (("session_authorization", "mnt"), ("posture_current", "mnt")),
     ("cache_hit", "fetched")),
    ("mnt_session_detail",
     (("session_authorization", "mnt"),),
     ("fetched", "empty", "gone", "failed", "invalid")),
    ("ers_network_device",
     (("network_devices", "ers"),),
     ("fetched", "empty", "failed", "inline")),
    ("ers_tacacs_user",
     (("tacacs_config", "ers"),),
     ("fetched", "empty", "failed")),
    ("openapi_tacacs_policy_rules",
     (("tacacs_policy_rules", "openapi"),),
     ("fetched", "empty", "failed")),
)


def seed(resolved):
    """Create zero-valued fetch children for the caches this plan will use.

    ``resolved`` is the set of ``(dataset, provider)`` name pairs the plan
    settled on. A cache is seeded when any dataset that ticks it resolved to
    the provider that does the ticking, and only with the results its call
    sites can produce: a zero for an outcome no code path emits would promise
    a distinction this deployment cannot make.
    """
    for cache, tickers, results in FETCH_OUTCOMES:
        if any(pair in resolved for pair in tickers):
            for result in results:
                fetches_total.labels(cache=cache, result=result)


# One cache per process, keyed by name. Datasets that draw on the same fan-out
# share an instance, which is also why they share a cost pool: one fetch answers
# all of them.
_CACHES = {}


def shared(name, ttl_seconds=86400):
    cache = _CACHES.get(name)
    if cache is None:
        cache = DetailCache(name, ttl_seconds=ttl_seconds)
        _CACHES[name] = cache
    return cache
