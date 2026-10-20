"""Pure-Python battery sketch aggregation; the public check is injected, never solved."""
from __future__ import annotations

import json

import pytest

from services.verify import battery


def _full(sketch, **changes):
    return {"ok": True, "sketch": sketch, "status": "full", "dof": 0,
            "fully_defined": True, "pinned_only": False, "findings": [], **changes}


def _install(monkeypatch, results):
    from services.mcp import core_bridge

    calls = []

    def rpc(method, params):
        assert method == "doc_tree"
        assert params == {"doc_id": "d1"}
        return {"tree": {"features": [
            {"id": "f1", "type": "sketch"},
            {"id": "f2", "type": "extrude"},
            {"id": "f13", "type": "sketch"},
            {"id": "f20", "type": "sketch3d"},
        ]}}

    def check(sketch, doc_id=None):
        calls.append((doc_id, sketch))
        value = results[sketch]
        if isinstance(value, Exception):
            raise value
        return json.dumps(value)

    monkeypatch.setattr(core_bridge, "call", rpc)
    monkeypatch.setattr(battery, "_get_fn", lambda module, name: check)
    return calls


def test_battery_checks_all_sketches_and_preserves_later_findings(monkeypatch):
    finding = {"kind": "under_dimensioned", "severity": "warning",
               "message": "Add a radius dimension", "entities": ["e3"]}
    calls = _install(monkeypatch, {
        "f1": _full("f1"),
        "f13": _full("f13", status="under", dof=1, fully_defined=False,
                     findings=[finding], summary="1 DOF remains"),
    })
    result = battery.verify("d1", ["check_sketch"])
    quality = result["check_results"]["check_sketch"]
    assert calls == [("d1", "f1"), ("d1", "f13")]
    assert result["scores"] == {"check_sketch": 0.0}
    assert quality["ok"] is True
    assert quality["fully_defined"] is False
    assert quality["pinned_only"] is False
    assert [f["sketch"] for f in quality["failures"]] == ["f13"]
    assert quality["findings"] == [{**finding, "sketch": "f13"}]
    assert any(f.get("entities") == ["e3"] and f["sketch"] == "f13"
               for f in result["findings"])
    assert any(f.get("ok") is False and f["sketch"] == "f13" for f in result["findings"])
    assert result["skipped"] == []


def test_battery_pinned_only_never_passes_even_if_tool_claimed_full(monkeypatch):
    calls = _install(monkeypatch, {
        "f1": _full("f1"), "f13": _full("f13", pinned_only=True),
    })
    result = battery.verify("d1", ["check_sketch"])
    quality = result["check_results"]["check_sketch"]
    assert len(calls) == 2
    assert quality["pinned_only"] is True
    assert quality["fully_defined"] is False
    assert quality["sketches"][1]["fully_defined"] is False
    assert quality["failures"][0]["pinned_only"] is True
    assert result["scores"]["check_sketch"] == 0.0


def test_battery_explicit_sketch_override_does_not_inspect_other_sketches(monkeypatch):
    calls = _install(monkeypatch, {"f13": _full("f13")})
    monkeypatch.setattr(battery, "_sketch_ids", lambda doc_id: pytest.fail("override ignored"))
    result = battery.verify("d1", ["check_sketch"], sketch="f13")
    assert calls == [("d1", "f13")]
    assert result["scores"] == {"check_sketch": 1.0}
    assert result["check_results"]["check_sketch"]["fully_defined"] is True


def test_battery_continues_after_one_sketch_check_raises(monkeypatch):
    calls = _install(monkeypatch, {
        "f1": RuntimeError("health failed"), "f13": _full("f13", pinned_only=True),
    })
    result = battery.verify("d1", ["check_sketch"])
    quality = result["check_results"]["check_sketch"]
    assert calls == [("d1", "f1"), ("d1", "f13")]
    assert quality["ok"] is False
    assert quality["pinned_only"] is True
    assert {f["sketch"] for f in quality["failures"]} == {"f1", "f13"}
    assert "health failed" in quality["failures"][0]["detail"]
    assert result["scores"]["check_sketch"] == 0.0


@pytest.mark.parametrize("changes", [
    {"open_endpoints": ["e1.end"]},
    {"conflicting": ["c7"]},
    {"converged": False},
    {"skipped": True, "skip_reason": "solver unavailable"},
])
def test_battery_rejects_inconsistent_full_or_skipped_later_result(monkeypatch, changes):
    _install(monkeypatch, {"f1": _full("f1"), "f13": _full("f13", **changes)})
    result = battery.verify("d1", ["check_sketch"])
    assert result["scores"]["check_sketch"] == 0.0
    assert result["check_results"]["check_sketch"]["fully_defined"] is False
    assert result["skipped"] == []


def test_battery_roster_failure_is_not_a_no_sketches_skip(monkeypatch):
    from services.mcp import core_bridge

    def unavailable(*args):
        raise RuntimeError("cannot read feature tree")

    monkeypatch.setattr(core_bridge, "call", unavailable)
    monkeypatch.setattr(battery, "_get_fn", lambda *args: lambda sketch, doc_id=None: None)
    result = battery.verify("d1", ["check_sketch"])
    assert result["scores"]["check_sketch"] == 0.0
    assert result["skipped"] == []
    assert "cannot read feature tree" in result["findings"][0]["detail"]


def test_battery_empty_roster_honestly_skips(monkeypatch):
    from services.mcp import core_bridge

    monkeypatch.setattr(core_bridge, "call", lambda *args: {"tree": {"features": []}})
    monkeypatch.setattr(battery, "_get_fn", lambda *args: lambda sketch, doc_id=None: None)
    result = battery.verify("d1", ["check_sketch"])
    assert result["scores"] == {}
    assert result["skipped"] == ["check_sketch"]
