"""One serialized execution lane per target.

Datasets sharing a target share a lane, so the exporter never has two
collections in flight against the same ISE persona; datasets on different
targets run concurrently, so a slow Oracle scan cannot stall PAN health.

v2 grew four hand-written lanes -- a persistent Data Connect worker plus three
near-identical "spawn a thread unless one is already running" blocks for MnT,
devices and TACACS. This is that pattern written once, keyed by target, which is
also the natural place to enforce a target's budget later.
"""
from __future__ import annotations

import logging
import queue
import threading

from . import telemetry


logger = logging.getLogger(__name__)

_STOP = object()


class Lane:
    """A single worker thread draining one target's queue."""

    def __init__(self, target):
        self.target = target
        self._queue = queue.Queue()
        self._queued = set()
        self._lock = threading.Lock()
        self._thread = None
        self._stopping = False
        self._publish()

    @property
    def alive(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.alive:
            return
        self._stopping = False
        self._thread = threading.Thread(
            target=self._loop, name=f"ise3-lane-{self.target}", daemon=True)
        self._thread.start()

    def submit(self, name, work):
        """Queue one dataset's collection. Duplicate submissions are dropped.

        A dataset still queued or running from a previous tick must not be
        queued again: that is how a slow collection turns a fixed cadence into
        an unbounded backlog against the appliance.
        """
        with self._lock:
            if self._stopping or name in self._queued:
                return False
            self._queued.add(name)
        self._queue.put((name, work))
        self._publish()
        return True

    def _loop(self):
        while True:
            item = self._queue.get()
            try:
                if item is _STOP:
                    return
                name, work = item
                telemetry.lane_busy.labels(target=self.target).set(1)
                try:
                    work()
                except Exception:       # noqa: BLE001 - a lane must outlive its work
                    logger.exception(
                        "lane %s failed running %s", self.target, name)
                finally:
                    telemetry.lane_busy.labels(target=self.target).set(0)
                    with self._lock:
                        self._queued.discard(name)
                    self._publish()
            finally:
                self._queue.task_done()

    def stop(self, timeout=30):
        with self._lock:
            self._stopping = True
        if not self.alive:
            self._drain()
            return True
        self._queue.put(_STOP)
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            logger.warning("lane %s did not stop within %ss", self.target, timeout)
            return False
        self._drain()
        return True

    def _drain(self):
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
            else:
                self._queue.task_done()
        with self._lock:
            self._queued.clear()
        telemetry.lane_busy.labels(target=self.target).set(0)
        self._publish()

    def _publish(self):
        with self._lock:
            depth = len(self._queued)
        telemetry.lane_queue_depth.labels(target=self.target).set(depth)


class LaneSet:
    """The lanes for a run, or a synchronous stand-in for tests."""

    def __init__(self, targets, *, asynchronous=True):
        self.asynchronous = asynchronous
        self.lanes = {target: Lane(target) for target in targets}

    def start(self):
        if not self.asynchronous:
            return
        for lane in self.lanes.values():
            lane.start()

    def submit(self, target, name, work):
        lane = self.lanes.get(target)
        if lane is None:
            logger.warning("no lane for target %s; skipping %s", target, name)
            return False
        if not self.asynchronous:
            # Deterministic ordering for tests: run inline, same guards.
            work()
            return True
        return lane.submit(name, work)

    def stop(self, timeout=30):
        return all(lane.stop(timeout) for lane in self.lanes.values())
