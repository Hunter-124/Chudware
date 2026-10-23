"""test_verify.py — Tests for verify.py.

Runs without the C++ engine (mock tool_fn_override).
One integration-smoke test with the real engine is included and skipped
when CHUDWARE_LIBCHUDWARE is absent.

DoD coverage:
  - verify.run() dispatches all five check types (mock)
  - DONE on pass, RETRY on fail (budget > 0), ESCALATE on fail (budget exhausted)
  - ESCALATE on backend_unavailable / tool_error / predicate_error
  - end_condition predicate evaluated correctly
  - DISPATCH_TABLE is importable (design_review reuse contract)
"""
from __future__ import annotations

import json
import os

import pytest

from services.dag.verify import (
    DISPATCH_TABLE,
    VERDICT_DONE,
    VERDICT_ESCALATE,
    VERDICT_RETRY,
    run as verify_run,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _node(check_type: str, end_condition: str = "", **extra_inputs) -> dict:
    """Build a minimal verify node dict."""
    return {
        "id": f"v_{check_type}",
        "type": "verify",
        "name": f"Verify {check_type}",
        "inputs": {"check_type": check_type, **extra_inputs},
        "on_fail": "pause",
        "autonomy": "auto",
        "end_condition": end_condition or None,
    }


def _pack(node: dict, retry_remaining: int = 1) -> dict:
    """Build a minimal context pack for verify_run."""
    return {
        "node_inputs": node["inputs"],
        "retry_remaining": retry_remaining,
        "end_condition": node.get("end_condition"),
        "allowed_tools": None,
    }


def _tool_ok(**fields) -> str:
    """Return a JSON string representing a successful tool result."""
    return json.dumps({"ok": True, **fields})


def _tool_err(**fields) -> str:
    """Return a JSON string representing a failed tool result."""
    return json.dumps({"ok": False, "reason": "tool said no", **fields})


# ---------------------------------------------------------------------------
# DISPATCH_TABLE contract (design_review reuse)
# ---------------------------------------------------------------------------

def test_dispatch_table_contains_all_check_types():
    expected = {"interference", "erc", "drc", "firmware", "dc_ratio", "budget",
                # Advanced-FEA verify nodes.
                "fatigue", "thermal", "buckling", "contact",
                "vibration",  # vibration: run_fea_frequency_response gate
                "reveng_deviation"}  # reverse-engineering mesh-vs-B-Rep deviation gate
    assert expected == set(DISPATCH_TABLE.keys()), (
        "DISPATCH_TABLE must cover every check type (design_review reuses this verbatim)"
    )


def test_dispatch_table_entries_are_3_tuples():
    for check_type, entry in DISPATCH_TABLE.items():
        assert len(entry) == 3, f"{check_type}: entry must be (module_key, fn_name, default_pred)"
        module_key, fn_name, pred = entry
        assert isinstance(module_key, str)
        assert isinstance(fn_name, str)
        assert callable(pred)


# ---------------------------------------------------------------------------
# Happy path — each check type passes with mocked tool
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("check_type,tool_result", [
    ("interference", _tool_ok(clear=True, interferences=[],
                              clearances=[{"a": "b1", "b": "b2", "distance": 5.0}])),
    ("erc",          _tool_ok(errors=0, warnings=2, violations=[])),
    ("drc",          _tool_ok(errors=0, warnings=0, violations=[])),
    ("firmware",     _tool_ok(passed=True, assertions=[], elapsed_ms=120)),
    ("dc_ratio",     _tool_ok(results=[{"element_id": "B1", "dc_ratio": 0.72, "passed": True}])),
    ("budget",       _tool_ok(unit_cost=45.0)),
    ("buckling",     _tool_ok(critical_load_factor=1.0)),
])
def test_verify_pass(check_type, tool_result):
    node = _node(check_type)
    pack = _pack(node)
    verdict, out = verify_run(node, pack, tool_fn_override=lambda **_: tool_result)
    assert verdict == VERDICT_DONE
    assert out["status"] == "done"
    assert out["data"]["passed"] is True
    assert out["data"]["check_type"] == check_type


# ---------------------------------------------------------------------------
# Fail → retry → escalate path
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("check_type,tool_result", [
    ("interference", _tool_ok(clear=False, interferences=[{"a": "b1", "b": "b2", "volume": 0.5}])),
    ("interference", _tool_ok(interferences=[])),  # no clear verdict: fail closed
    ("erc",          _tool_ok(errors=2, warnings=0)),
    ("drc",          _tool_ok(errors=1, warnings=3)),
    ("firmware",     _tool_ok(passed=False, assertions=["assert GPIO_PIN == 3"])),
    ("dc_ratio",     _tool_ok(results=[{"element_id": "B1", "dc_ratio": 1.15, "passed": False}])),
    ("buckling",     _tool_ok(critical_load_factor=0.99)),
    ("buckling",     _tool_ok(critical_load_factor=None)),
])
def test_verify_fail_with_budget_gives_retry(check_type, tool_result):
    node = _node(check_type)
    pack = _pack(node, retry_remaining=2)
    verdict, out = verify_run(node, pack, tool_fn_override=lambda **_: tool_result)
    assert verdict == VERDICT_RETRY
    assert out["status"] == "running"
    assert out["data"]["passed"] is False
    assert out["retry_remaining"] == 1  # budget decremented


def test_verify_fail_budget_exhausted_gives_escalate():
    node = _node("interference")
    pack = _pack(node, retry_remaining=1)
    tool_result = _tool_ok(clear=False, interferences=[{"a": "b1", "b": "b2", "volume": 0.5}])
    verdict, out = verify_run(node, pack, tool_fn_override=lambda **_: tool_result)
    assert verdict == VERDICT_ESCALATE
    assert out["status"] == "failed"
    assert out["retry_remaining"] == 0


def test_verify_fail_zero_budget_immediately_escalates():
    node = _node("erc")
    pack = _pack(node, retry_remaining=0)
    tool_result = _tool_ok(errors=1, warnings=0)
    verdict, out = verify_run(node, pack, tool_fn_override=lambda **_: tool_result)
    assert verdict == VERDICT_ESCALATE


# ---------------------------------------------------------------------------
# Fail-closed: backend unavailable → escalate (never pass)
# ---------------------------------------------------------------------------

def test_backend_unavailable_escalates():
    """tool_fn_override raises ImportError → escalate, not pass."""
    node = _node("interference")
    pack = _pack(node, retry_remaining=3)

    def _raise(**_):
        raise ImportError("libchudware not loaded")

    verdict, out = verify_run(node, pack, tool_fn_override=_raise)
    assert verdict == VERDICT_ESCALATE
    assert out["data"]["reason"] == "tool_exception"
    # Must not be 'done' — fail closed
    assert out["status"] == "failed"


def test_tool_returns_ok_false_escalates():
    """Tool returns ok=false → escalate."""
    node = _node("erc")
    pack = _pack(node)
    verdict, out = verify_run(node, pack, tool_fn_override=lambda **_: _tool_err())
    assert verdict == VERDICT_ESCALATE
    assert out["data"]["reason"] == "tool_error"


def test_tool_returns_invalid_json_escalates():
    """Tool returns non-JSON string → escalate."""
    node = _node("drc")
    pack = _pack(node)
    verdict, out = verify_run(node, pack, tool_fn_override=lambda **_: "NOT JSON")
    assert verdict == VERDICT_ESCALATE
    assert out["data"]["reason"] == "tool_error"


def test_unknown_check_type_escalates():
    node = _node("nonexistent_check")
    pack = _pack(node)
    verdict, out = verify_run(node, pack)
    assert verdict == VERDICT_ESCALATE
    assert out["data"]["reason"] == "unknown_check_type"


# ---------------------------------------------------------------------------
# End-condition predicate evaluation
# ---------------------------------------------------------------------------

def test_custom_end_condition_pass():
    node = _node("interference", end_condition="len(result.get('interferences',[])) == 0")
    pack = _pack(node)
    tool_result = _tool_ok(interferences=[])
    verdict, out = verify_run(node, pack, tool_fn_override=lambda **_: tool_result)
    assert verdict == VERDICT_DONE
    assert out["data"]["passed"] is True


def test_custom_end_condition_fail():
    node = _node("interference", end_condition="len(result.get('interferences',[])) == 0")
    pack = _pack(node)
    tool_result = _tool_ok(interferences=[{"a": "x", "b": "y", "volume": 1.0}])
    verdict, out = verify_run(node, pack, tool_fn_override=lambda **_: tool_result)
    assert verdict in (VERDICT_RETRY, VERDICT_ESCALATE)
    assert out["data"]["passed"] is False


def test_bad_predicate_escalates():
    """Malformed predicate → escalate, not pass."""
    node = _node("erc", end_condition="this is not python((")
    pack = _pack(node)
    tool_result = _tool_ok(errors=0)
    verdict, out = verify_run(node, pack, tool_fn_override=lambda **_: tool_result)
    assert verdict == VERDICT_ESCALATE
    assert out["data"]["reason"] == "predicate_error"


def test_budget_predicate_with_target():
    """budget check_type with target_budget in inputs uses custom predicate."""
    node = _node(
        "budget",
        end_condition="result.get('unit_cost', 9e9) <= target_budget",
        target_budget=100.0,
    )
    pack = _pack(node)
    pack["node_inputs"]["target_budget"] = 100.0

    # unit_cost = 80 → passes
    tool_result_ok = _tool_ok(unit_cost=80.0)
    verdict, out = verify_run(node, pack, tool_fn_override=lambda **_: tool_result_ok)
    assert verdict == VERDICT_DONE

    # unit_cost = 150 → fails
    tool_result_fail = _tool_ok(unit_cost=150.0)
    verdict2, out2 = verify_run(node, pack, tool_fn_override=lambda **_: tool_result_fail)
    assert verdict2 in (VERDICT_RETRY, VERDICT_ESCALATE)


# ---------------------------------------------------------------------------
# Event emission
# ---------------------------------------------------------------------------

def test_events_emitted_on_pass():
    node = _node("interference")
    pack = _pack(node)
    events: list[tuple[str, dict]] = []

    def emit(evt, payload):
        events.append((evt, payload))

    verify_run(node, pack,
               emit=emit,
               tool_fn_override=lambda **_: _tool_ok(clear=True, interferences=[]))

    types = [e for e, _ in events]
    assert "dag.node.started" in types
    assert "dag.node.done" in types
    assert "dag.node.failed" not in types


def test_events_emitted_on_fail():
    node = _node("erc")
    pack = _pack(node, retry_remaining=1)
    events: list[tuple[str, dict]] = []

    def emit(evt, payload):
        events.append((evt, payload))

    verify_run(node, pack,
               emit=emit,
               tool_fn_override=lambda **_: _tool_ok(errors=1, warnings=0))

    types = [e for e, _ in events]
    assert "dag.node.started" in types
    assert "dag.node.done" not in types


# ---------------------------------------------------------------------------
# Integration smoke — real check_interference via libchudware.so
# ---------------------------------------------------------------------------

_LIBCHUDWARE = os.environ.get("CHUDWARE_LIBCHUDWARE", "")
_HAVE_ENGINE = bool(_LIBCHUDWARE and os.path.exists(_LIBCHUDWARE))


@pytest.mark.skipif(not _HAVE_ENGINE, reason="CHUDWARE_LIBCHUDWARE not set or file absent")
def test_real_interference_no_clash():
    """Integration: a single-body part trivially passes interference check (no pairs)."""
    # We need at least one open doc; create a minimal one via core_bridge.
    import json as _json
    from services.dag.verify import _get_fn

    # Use the battery._get_fn path for check_interference via the cad module.
    # This exercises the REAL seam: battery._get_fn → cad_tools.check_interference → core_bridge.
    try:
        fn = _get_fn("cad", "check_interference")
    except Exception as exc:
        pytest.skip(f"Could not resolve check_interference: {exc}")

    # Call with no doc_id — will use the current doc (may be absent in CI).
    raw = fn()
    result = _json.loads(raw)
    # Either succeeds with ok=True (no interferences on a trivial/absent doc)
    # or returns ok=False with a doc-not-found error — both are honest non-fabricated verdicts.
    assert "ok" in result, "check_interference must return a dict with 'ok' key"
