"""Behavioral regressions for geometry evaluation and its error envelopes."""
import json

import pytest

from services.agent import agent_turn as at
from services.mcp import core_bridge as cb


needs_engine = pytest.mark.skipif(not cb.available(), reason="CAD engine unavailable")


def call(tool_name, **kwargs):
    fn = at._bind_tool(tool_name)
    assert fn is not None, tool_name
    bound, _, err = at._reconcile_kwargs(tool_name, fn, kwargs)
    assert err is None, err
    return json.loads(fn(**bound))


@pytest.fixture
def two_bodies():
    doc = call("new_part", name="eval_regression_bodies")["doc_id"]
    try:
        ops = [cb.feat("box", "First", {"length": 10, "width": 10, "height": 10,
                                             "at": [0, 0, 0], "mode": "new"}),
               cb.feat("box", "Second", {"length": 20, "width": 10, "height": 10,
                                              "at": [40, 0, 0], "mode": "new"})]
        result = call("add_features", doc_id=doc, ops=ops)
        assert result["ok"], result
        yield doc
    finally:
        cb.call("doc_close", {"doc_id": doc})


@needs_engine
def test_symmetry_custom_planes_and_body_selection(two_bodies):
    first = call("check_symmetry", doc_id=two_bodies, body=1, plane="right")
    second_wrong = call("check_symmetry", doc_id=two_bodies, body=2, plane="right")
    second = call("check_symmetry", doc_id=two_bodies, body_id=2,
                  origin=[40, 0, 0], normal=[1, 0, 0])
    offset = call("check_symmetry", doc_id=two_bodies, body=2, plane="right", offset=40)
    assert first["ok"] and first["symmetric"] and first["body_id"] == 1, first
    assert second_wrong["ok"] and not second_wrong["symmetric"], second_wrong
    for result in (second, offset):
        assert result["ok"] and result["symmetric"] and result["body_id"] == 2, result
        assert result["plane"]["origin"] == pytest.approx([40, 0, 0])
        assert result["deviation_mm3"] == pytest.approx(0, abs=1e-5)


def test_symmetry_boolean_failure_is_not_an_asymmetry(monkeypatch):
    from services.mcp import cad_eval_tools as ev
    monkeypatch.setattr(ev, "_doc", lambda doc_id: doc_id)
    def fail(method, params):
        assert method == "eval_symmetry"
        raise cb.CoreError("Boolean operation failed", "symmetry_failed")
    monkeypatch.setattr(cb, "call", fail)
    result = call("check_symmetry", doc_id="test", plane="right")
    assert result["ok"] is False and result["code"] == "symmetry_failed"
    assert "symmetric" not in result


@needs_engine
def test_mass_density_units_and_body_selection(two_bodies):
    bad = call("measure_part", doc_id=two_bodies, density_g_cm3=7850)
    assert bad["ok"] is False and "7.85" in bad["error"], bad
    whole = call("measure_part", doc_id=two_bodies, density_g_cm3=7.85)
    first = call("measure_part", doc_id=two_bodies, density_g_cm3=7.85, body_id=1)
    second = call("measure_part", doc_id=two_bodies, density_g_cm3=7.85, body_id=2)
    assert first["ok"] and second["ok"] and whole["ok"]
    assert first["volume_mm3"] == pytest.approx(1000)
    assert second["volume_mm3"] == pytest.approx(2000)
    assert whole["volume_mm3"] == pytest.approx(3000)
    assert first["mass"]["mass_g"] == pytest.approx(7.85)
    assert second["mass"]["mass_g"] == pytest.approx(15.7)


def test_mass_default_warning_and_failed_check_are_surfaced(monkeypatch):
    from services.mcp import cad_eval_tools as ev
    monkeypatch.setattr(ev, "_doc", lambda doc_id: doc_id)
    def rpc(method, params):
        if method == "eval_mass":
            return {"volume_mm3": 1000, "mass_g": 7.85,
                    "material": {"source": "default", "density_g_cm3": 7.85}}
        assert method == "eval_check"
        raise cb.CoreError("check transport failed", "transport_error")
    monkeypatch.setattr(cb, "call", rpc)
    result = call("measure_part", doc_id="test")
    assert result["ok"] and result["check"]["ran"] is False
    assert "check transport failed" in result["check"]["error"]
    warnings = " ".join(result["warnings"]).lower()
    assert "7.85" in warnings and "assumed" in warnings
    assert "check" in warnings and "unchecked" in warnings


@needs_engine
def test_measure_rejects_multiple_matches_and_reports_cylinder_axes():
    doc = call("new_part", name="eval_regression_bores")["doc_id"]
    try:
        ops = [cb.feat("box", "Plate", {"length": 80, "width": 50, "height": 10}),
               cb.feat("hole", "Bores", {"style": "simple", "end": "through_all",
                                           "diameter": cb.dim(6)},
                       {"face": {"of": "face", "side": "top"},
                        "points": [[-20, 0], [20, 0]]})]
        built = call("add_features", doc_id=doc, ops=ops)
        assert built["ok"], built
        query = {"of": "face", "surf": "cylinder"}
        matches = call("query_geometry", doc_id=doc, query=query)["matches"]
        assert len(matches) == 2, matches
        ambiguous = call("measure_between", doc_id=doc, a_query={**query, "count": 2})
        assert ambiguous["ok"] is False, ambiguous
        assert "exactly one" in ambiguous["error"].lower(), ambiguous
        a, b = [row["sid"] for row in sorted(matches, key=lambda row: row["centroid"][0])]
        face = call("measure_between", doc_id=doc, a_query=a)
        assert face["ok"] and face["radius"] == pytest.approx(3)
        assert face["diameter"] == pytest.approx(6)
        assert abs(face["axis"][2]) == pytest.approx(1)
        pair = call("measure_between", doc_id=doc, a_query=a, b_query=b)
        assert pair["ok"] and pair["axes_parallel"] is True, pair
        assert pair["axis_distance"] == pytest.approx(40, abs=1e-5)
        assert pair["distance"] == pytest.approx(34, abs=1e-5)
    finally:
        cb.call("doc_close", {"doc_id": doc})
