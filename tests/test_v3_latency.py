"""RADIUS latency is untrusted input.

ISE 3.3 Patch 11 does not populate these fields uniformly, so the exporter's job
is to distinguish a measurement from a field that merely has a number in it, and
to make the difference visible rather than averaging it away.
"""
import pytest
from prometheus_client import REGISTRY

from ise_exporter3.latency import (
    MAX_PLAUSIBLE_SECONDS,
    LatencyAccumulator,
    normalize,
    observe,
)


class Ctx:
    class provider:
        name = "dataconnect"


def _samples(result, quality):
    return REGISTRY.get_sample_value(
        "ise3_radius_latency_samples_total",
        {"provider": "dataconnect", "result": result, "quality": quality}) or 0.0


def test_a_real_measurement_converts_from_milliseconds():
    assert normalize(35, unit="ms") == (0.035, "ok")
    assert normalize("35", unit="ms") == (0.035, "ok")
    assert normalize(0.035, unit="s") == (0.035, "ok")


def test_zero_is_not_measured_rather_than_instantaneous():
    # This is the one that quietly ruins a latency panel: ISE populates the
    # field with 0 for flows it did not time, and a zero admitted into a mean
    # drags the whole series toward zero while still looking like data.
    assert normalize(0) == (None, "not_measured")
    assert normalize("0.0") == (None, "not_measured")


@pytest.mark.parametrize("value,quality", [
    (None, "missing"),
    ("", "missing"),
    ("n/a", "non_numeric"),
    (float("nan"), "non_numeric"),
    (float("inf"), "non_numeric"),
    (-5, "negative"),
])
def test_untrustworthy_values_are_classified_not_repaired(value, quality):
    seconds, reported = normalize(value)
    assert seconds is None
    assert reported == quality


def test_values_beyond_plausibility_are_dropped():
    # A RADIUS exchange this slow is a timeout or a field that does not mean
    # what it appears to, not a latency reading.
    assert normalize(MAX_PLAUSIBLE_SECONDS * 1000 + 1)[1] == "implausible"
    # Sub-millisecond is indistinguishable from an unpopulated field, because
    # ISE reports whole milliseconds.
    assert normalize(0.4, unit="ms")[1] == "implausible"


def test_sample_quality_is_exported_so_a_gap_is_distinguishable_from_a_drop():
    before_ok = _samples("passed", "ok")
    before_bad = _samples("passed", "not_measured")
    assert observe(Ctx, 42, result="passed") == pytest.approx(0.042)
    assert observe(Ctx, 0, result="passed") is None
    assert _samples("passed", "ok") == before_ok + 1
    assert _samples("passed", "not_measured") == before_bad + 1


def test_passed_and_failed_are_counted_separately():
    # They are timed on different code paths, so their mixture moves with the
    # failure rate rather than with latency.
    before = _samples("failed", "ok")
    observe(Ctx, 10, result="failed")
    assert _samples("failed", "ok") == before + 1


def test_the_accumulator_reports_how_much_of_the_sample_it_could_use():
    accumulator = LatencyAccumulator()
    for value in (20, 40, 0, None, 60):
        accumulator.add(normalize(value)[0])
    assert accumulator.mean == pytest.approx(0.04)      # 20, 40, 60 ms
    assert accumulator.coverage == pytest.approx(3 / 5)


def test_an_accumulator_with_nothing_usable_reports_no_value_not_zero():
    # Publishing 0 here would claim the fleet is instantaneous.
    accumulator = LatencyAccumulator()
    for value in (0, None, -1):
        accumulator.add(normalize(value)[0])
    assert accumulator.mean is None
    assert accumulator.coverage == 0.0


def test_an_empty_accumulator_has_no_coverage_to_report():
    assert LatencyAccumulator().coverage is None


# --- known ISE defects ------------------------------------------------------

def test_a_defect_that_no_validation_can_catch_is_declared_beside_the_metric():
    # CSCwm43211 inflates RADIUS accounting latency: the reported value is
    # plausible, a packet capture shows no matching delay, and nothing about
    # the number itself says so. Silently republishing it would be the worst
    # option; silently dropping it would be the second worst.
    from ise_exporter3 import known_defects

    assert known_defects.publish() >= 1
    defect = next(d for d in known_defects.DEFECTS if d.identifier == "CSCwm43211")
    assert defect.impact == "inflated"
    assert "ise3_radius_accounting_latency_seconds" in defect.metrics
    assert REGISTRY.get_sample_value(
        "ise3_ise_known_defect_info",
        {"defect": "CSCwm43211",
         "metric": "ise3_radius_accounting_latency_seconds",
         "impact": "inflated",
         "confirmed_on_supported_release": "unconfirmed"}) == 1


def test_an_inflated_but_plausible_latency_still_passes_validation():
    # Pinning the limit of the value-level defence, so nobody later assumes the
    # normaliser protects against CSCwm43211. Eight seconds is a legitimate
    # reading and an inflated one, and they are indistinguishable here.
    assert normalize(8000)[1] == "ok"


def test_every_declared_defect_names_a_metric_and_an_impact():
    from ise_exporter3 import known_defects

    for defect in known_defects.DEFECTS:
        assert defect.identifier.startswith("CSC")
        assert defect.metrics and all(m.startswith("ise3_") for m in defect.metrics)
        assert defect.impact
        assert len(defect.detail) > 60, f"{defect.identifier} needs a usable explanation"
