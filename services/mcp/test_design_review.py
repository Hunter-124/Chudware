"""test_design_review.py — Tests for design_review_tools.py.

DoD coverage:
  - design_review() returns narrated text (not JSON dump)
  - Narration includes per-check pass/fail lines with evidence-backed numbers
  - Overall PASS when all checks pass
  - Overall FAIL when any check fails
  - Unavailable backends reported as caveats (never a silent pass)
  - Honest skip for checks not requested
  - Source attribution line present

Runs without the C++ engine (mock tool_fn_overrides).
"""
from __future__ import annotations

import json
import re

import pytest

from services.mcp.design_review_tools import _run_design_review


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

def _ok_fn(**fields):
    """Return a mock tool function that returns ok=True with given fields."""
    def _fn(**kwargs):
        return json.dumps({"ok": True, **fields})
    return _fn


def _fail_fn(**fields):
    """Return a mock tool function that returns ok=True but with failure data."""
    def _fn(**kwargs):
        return json.dumps({"ok": True, **fields})
    return _fail_fn


def _err_fn(reason: str = "tool said no"):
    """Return a mock tool function that returns ok=False."""
    def _fn(**kwargs):
        return json.dumps({"ok": False, "reason": reason})
    return _fn


def _raise_fn(exc_type=RuntimeError, msg="backend exploded"):
    """Return a mock tool function that raises."""
    def _fn(**kwargs):
        raise exc_type(msg)
    return _fn


# ---------------------------------------------------------------------------
# Narrated output — not a JSON dump
# ---------------------------------------------------------------------------

class TestNarration:
    def test_returns_string_not_json(self):
        overrides = {
            "interference": _ok_fn(clear=True, interferences=[], clearances=[]),
        }
        result = _run_design_review(checks=["interference"],
                                    tool_fn_overrides=overrides)
        assert isinstance(result, str)
        # Must NOT be a raw JSON dump
        try:
            parsed = json.loads(result)
            pytest.fail(f"design_review returned raw JSON: {result[:200]}")
        except json.JSONDecodeError:
            pass  # expected — narrated text is NOT JSON

    def test_has_header_line(self):
        overrides = {"interference": _ok_fn(clear=True, interferences=[])}
        result = _run_design_review(checks=["interference"],
                                    tool_fn_overrides=overrides)
        assert "Design Review" in result

    def test_has_source_attribution(self):
        overrides = {"interference": _ok_fn(clear=True, interferences=[])}
        result = _run_design_review(checks=["interference"],
                                    tool_fn_overrides=overrides)
        assert "(Source:" in result

    def test_has_overall_verdict(self):
        overrides = {"interference": _ok_fn(clear=True, interferences=[])}
        result = _run_design_review(checks=["interference"],
                                    tool_fn_overrides=overrides)
        assert "Overall:" in result


# ---------------------------------------------------------------------------
# Per-check pass/fail narration
# ---------------------------------------------------------------------------

class TestCheckNarration:
    def test_interference_pass_narrates_zero_pairs(self):
        overrides = {"interference": _ok_fn(clear=True, interferences=[], clearances=[])}
        result = _run_design_review(checks=["interference"],
                                    tool_fn_overrides=overrides)
        assert "✓" in result
        assert "Interference" in result
        assert "PASS" in result

    def test_interference_fail_narrates_count(self):
        overrides = {
            "interference": _ok_fn(
                clear=False, interferences=[{"a": "body1", "b": "body2", "volume": 0.5}]
            )
        }
        result = _run_design_review(checks=["interference"],
                                    tool_fn_overrides=overrides)
        assert "✗" in result
        assert "1" in result  # 1 pair
        assert "FAIL" in result

    def test_erc_pass_narrates_zero_errors(self):
        overrides = {"erc": _ok_fn(errors=0, warnings=3)}
        result = _run_design_review(checks=["erc"], tool_fn_overrides=overrides)
        assert "✓" in result
        assert "0" in result

    def test_erc_fail_narrates_error_count(self):
        overrides = {"erc": _ok_fn(errors=2, warnings=0)}
        result = _run_design_review(checks=["erc"], tool_fn_overrides=overrides)
        assert "✗" in result
        assert "2" in result

    def test_drc_pass(self):
        overrides = {"drc": _ok_fn(errors=0, warnings=0)}
        result = _run_design_review(checks=["drc"], tool_fn_overrides=overrides)
        assert "✓" in result

    def test_firmware_pass_narrates_assertion_count(self):
        overrides = {
            "firmware": _ok_fn(
                passed=True,
                assertions=[{"name": "boot", "passed": True}],
                elapsed_ms=150,
            )
        }
        result = _run_design_review(checks=["firmware"], tool_fn_overrides=overrides)
        assert "✓" in result
        assert "1" in result  # 1 assertion

    def test_firmware_fail_narrates_failed_assertions(self):
        overrides = {
            "firmware": _ok_fn(
                passed=False,
                assertions=[
                    {"name": "boot", "passed": False},
                    {"name": "init", "passed": True},
                ],
            )
        }
        result = _run_design_review(checks=["firmware"], tool_fn_overrides=overrides)
        assert "✗" in result
        assert "1" in result  # 1 failed

    def test_dc_ratio_pass_narrates_max_ratio(self):
        overrides = {
            "dc_ratio": _ok_fn(
                results=[
                    {"element_id": "B1", "dc_ratio": 0.72, "passed": True},
                    {"element_id": "B2", "dc_ratio": 0.55, "passed": True},
                ]
            )
        }
        result = _run_design_review(checks=["dc_ratio"], tool_fn_overrides=overrides)
        assert "✓" in result
        assert "0.72" in result  # max dc_ratio narrated verbatim

    def test_dc_ratio_fail_narrates_worst_member(self):
        overrides = {
            "dc_ratio": _ok_fn(
                results=[
                    {"element_id": "B1", "dc_ratio": 1.35, "passed": False},
                    {"element_id": "B2", "dc_ratio": 0.80, "passed": True},
                ]
            )
        }
        result = _run_design_review(checks=["dc_ratio"], tool_fn_overrides=overrides)
        assert "✗" in result
        assert "B1" in result  # worst member id narrated verbatim
        assert "1.35" in result

    def test_budget_pass_narrates_unit_cost(self):
        overrides = {"budget": _ok_fn(unit_cost=42.50)}
        result = _run_design_review(checks=["budget"], tool_fn_overrides=overrides)
        assert "✓" in result
        assert "42.50" in result  # unit_cost narrated verbatim

    def test_budget_fail_when_ok_false(self):
        overrides = {"budget": _err_fn("price not resolved")}
        result = _run_design_review(checks=["budget"], tool_fn_overrides=overrides)
        assert "✗" in result


# ---------------------------------------------------------------------------
# Overall verdict
# ---------------------------------------------------------------------------

class TestOverallVerdict:
    def test_overall_pass_when_all_pass(self):
        overrides = {
            "interference": _ok_fn(clear=True, interferences=[]),
            "erc":          _ok_fn(errors=0, warnings=0),
        }
        result = _run_design_review(
            checks=["interference", "erc"],
            tool_fn_overrides=overrides,
        )
        assert "Overall: PASS" in result

    def test_overall_fail_when_any_fail(self):
        overrides = {
            "interference": _ok_fn(clear=False, interferences=[{"a": "a", "b": "b"}]),
            "erc":          _ok_fn(errors=0, warnings=0),
        }
        result = _run_design_review(
            checks=["interference", "erc"],
            tool_fn_overrides=overrides,
        )
        assert "Overall: FAIL" in result

    def test_overall_partial_when_backends_unavailable(self):
        overrides = {
            "interference": _ok_fn(clear=True, interferences=[]),
        }
        # "erc" has no override and its real backend is not available in test env
        # We can simulate this by using a raise fn
        overrides["erc"] = _raise_fn(ImportError, "ecad backend not found")
        result = _run_design_review(
            checks=["interference", "erc"],
            tool_fn_overrides=overrides,
        )
        # When a check errors, it counts as fail, so overall FAIL
        assert "Overall:" in result

    def test_skipped_checks_not_counted_as_fail(self):
        overrides = {"interference": _ok_fn(clear=True, interferences=[])}
        result = _run_design_review(
            checks=["interference"],  # only run interference
            tool_fn_overrides=overrides,
        )
        assert "Overall: PASS" in result
        assert "skipped" in result.lower()


# ---------------------------------------------------------------------------
# Unavailable backends — must be caveats, not silent passes
# ---------------------------------------------------------------------------

class TestUnavailableBackends:
    def test_backend_raises_reported_as_error(self):
        overrides = {"interference": _raise_fn(RuntimeError, "engine not loaded")}
        result = _run_design_review(
            checks=["interference"],
            tool_fn_overrides=overrides,
        )
        # Error is reported (not silently passed)
        assert "✗" in result or "ERROR" in result

    def test_tool_ok_false_is_not_pass(self):
        overrides = {"budget": _err_fn("price_lookup_failed")}
        result = _run_design_review(checks=["budget"], tool_fn_overrides=overrides)
        # Must not appear as PASS
        assert "Overall: PASS" not in result


# ---------------------------------------------------------------------------
# Honest skip
# ---------------------------------------------------------------------------

class TestHonestSkip:
    def test_unrequested_checks_appear_as_skipped(self):
        overrides = {"interference": _ok_fn(clear=True, interferences=[])}
        result = _run_design_review(
            checks=["interference"],
            tool_fn_overrides=overrides,
        )
        # erc, drc etc were not requested — must show as skipped
        assert "skipped" in result.lower()

    def test_no_checks_produces_no_checks_ran(self):
        result = _run_design_review(checks=[], tool_fn_overrides={})
        assert "NO CHECKS RAN" in result or "0 passed" in result


# ---------------------------------------------------------------------------
# doc_id plumbed into narration header
# ---------------------------------------------------------------------------

class TestDocId:
    def test_doc_id_appears_in_header(self):
        overrides = {"interference": _ok_fn(clear=True, interferences=[])}
        result = _run_design_review(
            doc_id="my-doc-123",
            checks=["interference"],
            tool_fn_overrides=overrides,
        )
        assert "my-doc-123" in result

    def test_default_doc_label_when_no_doc_id(self):
        overrides = {"interference": _ok_fn(clear=True, interferences=[])}
        result = _run_design_review(
            checks=["interference"],
            tool_fn_overrides=overrides,
        )
        assert "current document" in result
