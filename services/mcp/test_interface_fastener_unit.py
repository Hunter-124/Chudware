"""Deterministic fastener-stack checks; no live engine or daemon required."""
import pytest

from services.mcp.interface_check_tools import _check_fastener_passthrough


def _bore(dia, x=0.0, axis=(0.0, 0.0, 1.0)):
    return {"dia": dia, "radius": dia / 2, "centroid": (x, 0.0, 0.0),
            "axis": axis, "kind": "bore"}


def _check(a, b, fastener="M8", **kwargs):
    findings = []
    result = _check_fastener_passthrough(a, b, fastener, findings,
                                         "a", "b", "A", "B", **kwargs)
    return result, findings


def test_metric_series_and_parallel_stack():
    result, findings = _check([_bore(9)], [_bore(9, .25)], position_tolerance_mm=.2)
    assert result["status"] == "pass" and not findings
    assert result["iso273"]["diameter_mm"] == 9
    assert result["stacks"][0]["radial_margin_mm"] == pytest.approx(.55)
    assert result["stacks"][0]["iso273_meets_series"] is True


def test_clear_diameters_do_not_prove_offset_stack_clearance():
    result, findings = _check([_bore(9)], [_bore(9, .8)], position_tolerance_mm=.25)
    assert result["status"] == "fail" and findings
    assert result["stacks"][0]["clearance_mm"] == 1
    assert result["stacks"][0]["radial_margin_mm"] == pytest.approx(-.05)


def test_nominal_clearance_and_series_are_distinct():
    result, _ = _check([_bore(8.5)], [_bore(8.5)], clearance_class="normal")
    assert result["status"] == "pass"
    assert result["stacks"][0]["iso273_meets_series"] is False
    result, _ = _check([_bore(8.5)], [_bore(8.5)], clearance_class="fine")
    assert result["stacks"][0]["iso273_meets_series"] is True


def test_unmatched_and_nonparallel_are_not_evaluated():
    result, _ = _check([_bore(9)], [])
    assert result["status"] == "skip" and result["stacks"][0]["passes"] is None
    result, _ = _check([], [_bore(9)])
    assert result["status"] == "skip" and result["stacks"][0]["status"] == "not_evaluated"
    result, _ = _check([_bore(9)], [_bore(9, axis=(0.0174524, 0, 0.9998477))])
    assert result["status"] == "skip" and result["stacks"][0]["status"] == "not_evaluated"


def test_inch_or_unlisted_metric_does_not_claim_iso273():
    result, _ = _check([_bore(8)], [_bore(8)], "1/4-20")
    assert result["status"] == "pass"
    assert result["iso273"]["status"] == "not_evaluated"
    result, _ = _check([_bore(16)], [_bore(16)], "M13x1.5")
    assert result["iso273"]["reason"] == "unsupported_fastener_size"


def test_invalid_tolerance_and_unknown_fastener_are_skipped():
    for tolerance in (-1, float("nan")):
        result, _ = _check([_bore(9)], [_bore(9)], position_tolerance_mm=tolerance)
        assert result["status"] == "skip"
    result, _ = _check([_bore(9)], [_bore(9)], "unknown")
    assert result["status"] == "skip"


def test_bounded_stack_output_never_claims_complete_pass():
    holes = [_bore(9, x=10 * i) for i in range(101)]
    result, _ = _check(holes, holes)
    assert len(result["stacks"]) == 100
    assert result["truncated"] is True
    assert result["status"] == "skip"
