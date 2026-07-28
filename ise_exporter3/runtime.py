"""One collection attempt: fetch, publish atomically, record health.

A provider's ``fetch`` receives a context and writes through it. It never touches
metric families directly, which is what lets the runner guarantee two things the
provider cannot: the dataset's data and its health metadata commit inside one
scrape boundary, and a failed attempt leaves the previous snapshot in place
rather than publishing a convincing empty one.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from . import telemetry
from .labels import bounded_detail
from .snapshots import Publication, snapshot_lock
from .transports import FAILURE_EXPLANATIONS, PENDING_REASONS, TransportError


logger = logging.getLogger(__name__)

HEALTH_FAMILIES = (
    telemetry.dataset_up,
    telemetry.dataset_fresh,
    telemetry.dataset_last_success_timestamp,
    telemetry.dataset_last_failure_info,
    telemetry.dataset_last_failure_detail_info,
    telemetry.dataset_consecutive_failures,
    telemetry.dataset_collection_duration_seconds,
)
_HEALTH_FAMILIES = HEALTH_FAMILIES


def _forget_children(family, dataset, provider):
    """Drop one (dataset, provider) pair's children from a metric family."""
    names = tuple(getattr(family, "_labelnames", ()))
    if "dataset" not in names or "provider" not in names:
        return
    left, right = names.index("dataset"), names.index("provider")
    for labels in [key for key in getattr(family, "_metrics", {})
                   if key[left] == dataset and key[right] == provider]:
        family._metrics.pop(labels, None)


def _forget_failure(previous):
    """Drop a recorded failure's children, if they are still present.

    A provider step removes the departing source's health children, so the
    record kept here can outlive them. Letting that raise would roll back the
    very snapshot that notices the source is working again.
    """
    try:
        telemetry.dataset_last_failure_info.remove(
            previous["dataset"], previous["provider"], previous["reason"])
    except KeyError:
        pass
    try:
        telemetry.dataset_last_failure_detail_info.remove(
            previous["dataset"], previous["provider"], previous["reason"],
            previous["detail"])
    except KeyError:
        pass


def forget_provider_health(dataset, provider, extra_families=()):
    """Retire one (dataset, provider) pair's health series.

    Called when a dataset changes source. ``up`` and ``fresh`` are zeroed rather
    than removed, so an alert reads an explicit "this source is no longer
    supplying" instead of a series that silently vanishes; everything else is
    removed, because a timestamp or a duration from a source that is no longer
    running is a fact with no owner. ``dataset_failures_total`` is deliberately
    left alone: it is a Counter, and the failure history is exactly the forensic
    record wanted after a step.
    """
    with snapshot_lock:
        telemetry.dataset_up.labels(dataset=dataset, provider=provider).set(0)
        telemetry.dataset_fresh.labels(dataset=dataset, provider=provider).set(0)
        for family in (telemetry.dataset_last_success_timestamp,
                       telemetry.dataset_collection_duration_seconds,
                       *extra_families):
            _forget_children(family, dataset, provider)


def classify(error):
    """Map an arbitrary exception onto the bounded failure vocabulary.

    Providers raise TransportError for anything they understand. This exists so
    a genuine bug in normalisation still produces a bounded label instead of an
    exception string becoming a Prometheus dimension.
    """
    if isinstance(error, TransportError):
        return error.reason, error.detail
    name = type(error).__name__.lower()
    message = str(error).lower()
    if "timeout" in name or "timed out" in message:
        return "timeout", str(error)
    if "connection" in name:
        return "connection_failed", str(error)
    if "json" in name or "decode" in name or isinstance(error, (ValueError, KeyError)):
        return "invalid_response", str(error)
    return "unexpected_error", str(error)


@dataclass(frozen=True)
class Outcome:
    dataset: str
    provider: str
    ok: bool
    duration: float = 0.0
    reason: str = ""
    detail: str = ""
    # Entities this collection left for the next one because its warm-up budget
    # ran out. Carried out of the collection because the scheduler is the only
    # thing that can act on it: a cache with work outstanding should be revisited
    # at the rate the budget affords, not at the cadence the data deserves once
    # it is warm. Zero means "nothing pending", which is the steady state.
    deferred: int = 0


class FetchContext:
    """What a provider is given: its transport, the config, and a way to write."""

    def __init__(self, *, dataset, provider, transport, config, publication,
                 interval=None):
        self.dataset = dataset
        self.provider = provider
        self.transport = transport
        self.config = config
        self.scale = config.scale
        # The same ceilings the transport will enforce, so a provider builds its
        # statements against what will actually be accepted rather than against
        # a constant that used to agree.
        self.limits = config.limits
        self.settings = config.dataset(dataset.name)
        self.interval = int(interval or dataset.default_interval)
        self.deferred = 0
        self._publication = publication

    def defer(self, count):
        """Report entities this collection left for the next one.

        A converging provider calls this with whatever its per-cycle warm-up
        budget could not reach. It is what turns "this cache is still filling"
        into a scheduling decision instead of a gauge nobody acts on.
        """
        self.deferred = max(0, int(count or 0))

    def option(self, name):
        """One of this dataset's declared options, defaulted and validated.

        A provider that bounds a breakdown reads the bound from here. There is
        no other source: an option not declared on the Dataset raises, which is
        what stops a cap from re-appearing as a constant in a collector.
        """
        return self.settings.option(name)

    def set(self, family, value, /, **label_values):
        """Record one gauge value. The ``provider`` label is bound for you.

        Positional-only: a dataset may legitimately have a label named ``value``
        (``tacacs_activity`` does), and a keyword parameter here would collide
        with it at runtime for every row.
        """
        self._publication.set(family, value, **label_values)

    def set_shared(self, family, value, /, **label_values):
        """Record one write to a family shared across datasets.

        For the coverage and truncation gauges: they describe this snapshot, so
        they must not survive a discard describing rows that were rolled back,
        but they live in a family every dataset writes to and so cannot be
        declared in ``Dataset.metrics``. See Publication.aux_set.
        """
        self._publication.aux_set(family, value, **label_values)

    def fail(self, reason, detail=""):
        raise TransportError(reason, detail)


class Runner:
    """Executes one dataset attempt and owns everything it publishes."""

    def __init__(self, config):
        self.config = config
        self.failures = {}

    def run(self, entry, transport):
        """Run ``entry``'s active provider against ``transport``."""
        dataset = entry.dataset
        provider = entry.provider
        name, provider_name = entry.name, provider.name
        started = time.monotonic()
        attempted = time.time()

        telemetry.dataset_last_attempt_timestamp.labels(
            dataset=name, provider=provider_name).set(attempted)

        publication = Publication(
            dataset.metrics, limits=self.config.limits, provider=provider_name)
        context = FetchContext(
            dataset=dataset, provider=provider, transport=transport,
            config=self.config, publication=publication, interval=entry.interval)

        try:
            # Settle the preconditions the offline plan had to defer before
            # spending an attempt on a source that cannot answer.
            if transport is not None:
                transport.prepare()
                ready, reason, detail = transport.satisfies(provider.requires)
                if not ready:
                    raise TransportError(reason, detail)
            provider.fetch(context)
        except Exception as error:      # noqa: BLE001 - classified below
            reason, detail = classify(error)
            duration = max(0.0, time.monotonic() - started)
            self._publish_failure(publication, name, provider_name, reason,
                                  detail, duration)
            # "Not yet" is an expected state on the way to an answer, not an
            # alarm: cold starts pend on inventories and baselines by design,
            # and logging those as failed collections trains operators to
            # ignore the log line that matters when something actually breaks.
            if reason in PENDING_REASONS:
                logger.info(
                    "collection pending dataset=%s provider=%s reason=%s detail=%s",
                    name, provider_name, reason, bounded_detail(detail))
            else:
                level = (logger.warning if reason != "unexpected_error"
                         else logger.exception)
                level("collection failed dataset=%s provider=%s reason=%s detail=%s",
                      name, provider_name, reason, bounded_detail(detail))
            return Outcome(name, provider_name, False, duration, reason,
                           bounded_detail(detail))

        duration = max(0.0, time.monotonic() - started)
        try:
            self._publish_success(publication, name, provider_name, duration)
        except Exception as error:      # noqa: BLE001 - publication is a failure too
            reason, detail = classify(error)
            self._publish_failure(publication, name, provider_name, reason,
                                  detail, duration)
            logger.warning(
                "publication failed dataset=%s provider=%s reason=%s detail=%s",
                name, provider_name, reason, bounded_detail(detail))
            return Outcome(name, provider_name, False, duration, reason,
                           bounded_detail(detail))

        warn_at = self.config.limits.snapshot_sample_warning
        if publication.samples > warn_at:
            # Not an error, but a dataset this wide is usually keyed on
            # something it should not be. Say so before the hard ceiling.
            logger.warning(
                "dataset %s published %d series, past the %d soft limit; check "
                "it is not keyed on an endpoint identity",
                name, publication.samples, warn_at)
        logger.info(
            "collection ok dataset=%s provider=%s duration=%.3fs series=%d",
            name, provider_name, duration, publication.samples)
        return Outcome(name, provider_name, True, duration,
                       deferred=context.deferred)

    def _publish_success(self, publication, name, provider_name, duration):
        completed = time.time()
        previous = self.failures.pop(name, None)

        def writers():
            telemetry.dataset_up.labels(dataset=name, provider=provider_name).set(1)
            telemetry.dataset_fresh.labels(dataset=name, provider=provider_name).set(1)
            telemetry.dataset_last_success_timestamp.labels(
                dataset=name, provider=provider_name).set(completed)
            telemetry.dataset_consecutive_failures.labels(dataset=name).set(0)
            telemetry.dataset_collection_duration_seconds.labels(
                dataset=name, provider=provider_name).set(duration)
            if previous:
                _forget_failure(previous)

        publication.commit(_HEALTH_FAMILIES, (writers,))
        # Published after the commit, because the count is a property of the
        # snapshot that commit just validated -- a writer running inside it
        # would always observe zero. This is an observation about the boundary,
        # not part of what crosses it.
        telemetry.dataset_series.labels(dataset=name).set(publication.samples)

    def _publish_failure(self, publication, name, provider_name, reason, detail,
                         duration):
        bounded = bounded_detail(detail or FAILURE_EXPLANATIONS.get(reason, reason))
        previous = self.failures.get(name)
        count = (previous["count"] if previous else 0) + 1

        def writers():
            telemetry.dataset_failures_total.labels(
                dataset=name, provider=provider_name, reason=reason).inc()
            telemetry.dataset_up.labels(dataset=name, provider=provider_name).set(0)
            telemetry.dataset_consecutive_failures.labels(dataset=name).set(count)
            telemetry.dataset_collection_duration_seconds.labels(
                dataset=name, provider=provider_name).set(duration)
            if previous and (
                    previous["provider"], previous["reason"], previous["detail"]
            ) != (provider_name, reason, bounded):
                _forget_failure(previous)
            telemetry.dataset_last_failure_info.labels(
                dataset=name, provider=provider_name, reason=reason).set(1)
            telemetry.dataset_last_failure_detail_info.labels(
                dataset=name, provider=provider_name, reason=reason,
                detail=bounded).set(1)

        # The previous snapshot survives: a reporting failure must never be
        # published as a valid empty result.
        publication.discard(_HEALTH_FAMILIES, (writers,))
        # The provider is part of the record: a recovery probe fails under the
        # preferred source's label while the active source keeps collecting, and
        # clearing that child under the wrong label raises inside the commit.
        self.failures[name] = {"dataset": name, "provider": provider_name,
                               "reason": reason, "detail": bounded, "count": count}

    def consecutive_failures(self, name):
        entry = self.failures.get(name)
        return entry["count"] if entry else 0

    def reset_failures(self, name):
        """Forget a dataset's failure run.

        Called when a dataset changes provider: the backoff was earned by the
        source that failed, and making its replacement serve that sentence would
        add minutes of blank panel for a source that has not failed at all.
        """
        self.failures.pop(name, None)
        telemetry.dataset_consecutive_failures.labels(dataset=name).set(0)
