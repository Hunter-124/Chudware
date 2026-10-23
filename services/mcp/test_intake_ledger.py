"""test_intake_ledger.py — REAL reopen test for the doc-scoped IntakeLedger.

Contract: contracts/interaction.md §5 scope=doc — answers persist in the document's
IntakeState record, round-tripped as JSON into the .step+, so a dimension confirmed
in one session is never re-asked after the doc is saved and reopened.

This suite drives the REAL engine (libchudware via core_bridge): no meta-store
injection, no path injection — the production lookup (doc_get_meta/doc_set_meta
through the .step+ save/open cycle) is what is exercised.  Critically, doc_open
mints a FRESH doc_id (core/doc/session.cpp new_doc_id is a per-process counter),
and the test asserts replay works anyway because the record travels with the doc.

Run:  CHUDWARE_LIBCHUDWARE=$PWD/core/build-linux/bin/libchudware.so \
      python -m pytest services/mcp/test_intake_ledger.py -v
"""
from __future__ import annotations

import asyncio
import json
import math
import uuid

import pytest

from services.mcp import core_bridge as cb
from services.agent import agent_turn as at

pytestmark = pytest.mark.skipif(
    not cb.available(),
    reason="libchudware not staged; engine-backed IntakeLedger reopen test unavailable",
)

@pytest.fixture()
def minted_documents():
    """Track and discard every document minted by a real reopen test."""
    close_part = at._bind_tool("close_part")
    if close_part is None:
        pytest.skip("close_part unavailable")

    doc_ids: list[str] = []

    def track(doc_id: str) -> str:
        doc_ids.append(doc_id)
        return doc_id

    yield track

    for doc_id in reversed(doc_ids):
        result = json.loads(close_part(doc_id=doc_id, discard=True))
        assert result.get("ok"), (
            f"failed to discard intake-ledger scratch document {doc_id}: "
            f"{result.get('error')}"
        )


def _question():
    return {
        "id": "output_shaft.dia",
        "kind": "dimension",
        "family": "length",
        "prompt": "Output shaft diameter?",
        "suggested": {"value": 8.0, "unit": "mm"},
        "required": False,
    }


def test_real_reopen_never_re_asks(tmp_path, minted_documents):
    """Session 1: record a doc-scoped answer, doc_save.  Session 2: doc_open the
    file (fresh doc_id), construct the ledger FROM THE DOC ALONE, replay."""
    from services.agent.interaction import ask_user
    from services.mcp.intake_ledger import IntakeLedger

    # --- Session 1 ---------------------------------------------------------- #
    r = cb.call("doc_new", {"kind": "part", "name": f"il_{uuid.uuid4().hex[:8]}"})
    doc1 = minted_documents(r["doc_id"])
    q = _question()

    ledger1 = IntakeLedger(doc1)                    # production accessors — no injection
    r1 = ask_user([q], scope="doc", doc_id=doc1, ledger=ledger1, autonomy="auto")
    assert r1["output_shaft.dia"]["source"] == "default"
    assert r1["output_shaft.dia"]["canonical"] == {"value": 8.0, "unit": "mm"}

    path = tmp_path / "part.step+"
    cb.call("doc_save", {"doc_id": doc1, "path": str(path)})
    assert path.exists() and path.stat().st_size > 0

    # --- Session 2: reopen — the engine mints a NEW doc_id ------------------- #
    opened = cb.call("doc_open", {"path": str(path)})
    doc2 = minted_documents(opened["doc_id"])
    assert doc2 != doc1, (
        "doc_open re-used the doc_id — this test exists because it does NOT; "
        "if id semantics changed, revisit the ledger keying rationale"
    )

    transport_calls: list = []

    class BoomTransport:
        """Fails the test if invoked — the reopened doc must replay from its record."""
        def ask(self, batch):  # pragma: no cover
            transport_calls.append(batch.batch_id)
            return {}

    ledger2 = IntakeLedger(doc2)                    # constructed from the doc alone
    r2 = ask_user([q], scope="doc", doc_id=doc2, ledger=ledger2,
                  transport=BoomTransport(), blocking=True, autonomy="approve")
    assert len(transport_calls) == 0, (
        "Transport invoked after reopen — the answer did not travel with the doc"
    )
    assert r2["output_shaft.dia"]["source"] == "resumed"
    assert r2["output_shaft.dia"]["canonical"] == {"value": 8.0, "unit": "mm"}


def test_real_reopen_default_ledger_path(tmp_path, minted_documents):
    """Same reopen guarantee through ask_user's DEFAULT ledger (ledger=None,
    scope=doc): the production wiring users actually hit — nothing injected."""
    from services.agent.interaction import ask_user

    r = cb.call("doc_new", {"kind": "part", "name": f"il_{uuid.uuid4().hex[:8]}"})
    doc1 = minted_documents(r["doc_id"])
    q = _question()

    r1 = ask_user([q], scope="doc", doc_id=doc1, autonomy="auto")   # ledger=None
    assert r1["output_shaft.dia"]["source"] == "default"

    path = tmp_path / "part.step+"
    cb.call("doc_save", {"doc_id": doc1, "path": str(path)})

    doc2 = minted_documents(cb.call("doc_open", {"path": str(path)})["doc_id"])

    r2 = ask_user([q], scope="doc", doc_id=doc2, autonomy="auto")   # ledger=None
    assert r2["output_shaft.dia"]["source"] == "resumed", (
        "ask_user's default doc-scope ledger did not replay after a real reopen"
    )


def test_confirm_dimensions_persists_through_reopen(tmp_path, minted_documents):
    """The MCP call-site route (contract §5 'ask_user/intake call sites'):
    confirm_dimensions with no run_id lands the answer in the doc record, and it
    is still there — replayable — after save + reopen."""
    import json

    from services.mcp.intake_ledger import IntakeLedger
    from services.mcp import cad_tools
    from services.mcp.intake_tools import register_intake_tools
    from mcp.server.fastmcp import FastMCP

    # Register the intake tools on a throwaway FastMCP to reach confirm_dimensions.
    mcp = FastMCP("t")
    register_intake_tools(mcp)

    r = cb.call("doc_new", {"kind": "part", "name": f"il_{uuid.uuid4().hex[:8]}"})
    doc1 = minted_documents(r["doc_id"])

    # Seed a critical-dim ledger so confirm_dimensions has a row to close.
    from services.intake import intake as ik
    mp = ik.new_measured_part("mp_1", "motor", source="photo")
    mp.features = [ik.new_measured_feature("output_shaft", "shaft_in_bore",
                                           dims={"dia": 8.0})]
    ledger = ik.build_ledger(mp, process="fdm")
    cb.call("doc_set_meta", {"doc_id": doc1, "key": "measured_parts",
                             "value": [mp.to_dict()]})
    cb.call("doc_set_meta", {"doc_id": doc1, "key": "crit_dims",
                             "value": ledger.to_dict()})

    import asyncio
    content, structured = asyncio.run(mcp.call_tool("confirm_dimensions", {
        "confirmations": [{"key": "output_shaft.dia", "value_mm": 8.2}],
        "doc_id": doc1,
    }))
    out = json.loads(structured["result"]) if isinstance(structured, dict) else None
    assert out and out.get("ok"), out

    # Save + reopen; the interaction answer must have travelled with the doc.
    path = tmp_path / "part.step+"
    cb.call("doc_save", {"doc_id": doc1, "path": str(path)})
    doc2 = minted_documents(cb.call("doc_open", {"path": str(path)})["doc_id"])

    replay = IntakeLedger(doc2).answered("intake.dim.output_shaft.dia")
    assert replay is not None, (
        "confirm_dimensions answer missing after reopen — scope=doc routing broken"
    )
    assert replay["canonical"] == {"value": 8.2, "unit": "mm"}


def _call_dfm(monkeypatch, *, process: str, profile_params: dict, engine_call,
              **options) -> dict:
    from mcp.server.fastmcp import FastMCP
    from services.mcp import intake_tools

    monkeypatch.setattr(intake_tools, "_doc", lambda _doc_id: "dfm-doc")
    monkeypatch.setattr(intake_tools, "_dfm", lambda method, params: {
        "process": process, "params": profile_params,
    })
    monkeypatch.setattr(intake_tools.cb, "call", engine_call)
    mcp = FastMCP("dfm-test")
    intake_tools.register_intake_tools(mcp)
    _content, structured = asyncio.run(mcp.call_tool(
        "dfm_check", {"process": process, "doc_id": "dfm-doc", **options}))
    return json.loads(structured["result"])


def _cnc_profile() -> dict:
    return {
        "min_wall_mm": 0.5,
        "min_feature_mm": 0.5,
        "draft": {"required": False, "min_deg": 0.0},
        "thread": {"strategy": "tapped", "printed_ok_min": None},
    }


def test_dfm_empty_evaluation_is_not_evaluated_never_a_pass(monkeypatch):
    def empty_engine(method, params=None):
        if method == "eval_thickness":
            return {}
        if method == "doc_tree":
            return {"tree": {"features": []}}
        raise AssertionError((method, params))

    result = _call_dfm(
        monkeypatch, process="cnc_mill", profile_params=_cnc_profile(),
        engine_call=empty_engine,
    )

    assert result["status"] == "not_evaluated", result
    assert result["dfm_pass"] is None, result
    assert result["checks"], result
    assert any(check["status"] == "not_evaluated" for check in result["checks"]), result


def test_dfm_checks_min_feature_and_deep_machined_hole(monkeypatch):
    def small_deep_hole(method, params=None):
        if method == "eval_thickness":
            return {"min_thickness_mm": 2.0}
        if method == "doc_tree":
            return {"tree": {"features": [{
                "id": "h1", "type": "hole",
                "params": {
                    "style": "tap", "diameter": {"value": 0.3},
                    "depth": {"value": 3.0}, "end": "blind", "thread": "M1x0.25",
                },
            }]}}
        raise AssertionError((method, params))

    result = _call_dfm(
        monkeypatch, process="cnc_mill", profile_params=_cnc_profile(),
        engine_call=small_deep_hole,
    )

    checks = {check["check"]: check for check in result["checks"]}
    assert checks["min_feature"]["status"] == "fail", result
    assert checks["hole_depth_diameter"]["status"] == "fail", result
    assert checks["hole_depth_diameter"]["holes"][0]["ratio"] == 10.0, result
    assert result["status"] == "fail" and result["dfm_pass"] is False, result


def test_dfm_rejects_printed_thread_strategy_for_machining(monkeypatch):
    def printed_thread(method, params=None):
        if method == "eval_thickness":
            return {"min_thickness_mm": 2.0}
        if method == "doc_tree":
            return {"tree": {"features": [
                {"id": "base", "type": "box",
                 "params": {"length": 20, "width": 20, "height": 5}},
                {"id": "thread1", "type": "thread",
                 "params": {"thread": "M6x1.0", "thread_strategy": "printed"}},
            ]}}
        raise AssertionError((method, params))

    result = _call_dfm(
        monkeypatch, process="cnc_mill", profile_params=_cnc_profile(),
        engine_call=printed_thread,
    )

    thread_check = next(check for check in result["checks"]
                        if check["check"] == "thread_strategy")
    assert thread_check["status"] == "fail", result
    assert thread_check["threads"][0]["observed_strategy"] == "printed", result
    assert result["dfm_pass"] is False, result


def test_dfm_evaluation_exceptions_are_reported_not_swallowed(monkeypatch):
    def broken_engine(method, params=None):
        raise RuntimeError(f"{method} exploded")

    result = _call_dfm(
        monkeypatch, process="cnc_mill", profile_params=_cnc_profile(),
        engine_call=broken_engine,
    )

    assert result["status"] == "not_evaluated" and result["dfm_pass"] is None, result
    errors = [check.get("error", "") for check in result["checks"]
              if check["status"] == "not_evaluated"]
    assert any("eval_thickness exploded" in error for error in errors), result
    assert any("doc_tree exploded" in error for error in errors), result


def test_dfm_overhang_engine_unavailable_is_not_evaluated(monkeypatch):
    def missing_query(method, params=None):
        if method == "eval_thickness":
            return {"min_thickness_mm": 4}
        if method == "doc_tree":
            return {"tree": {"features": [
                {"id": "f1", "type": "box", "params": {"length": 20}},
            ]}}
        if method == "doc_query":
            raise RuntimeError("geometry backend unavailable")
        raise AssertionError(method)

    result = _call_dfm(
        monkeypatch, process="fdm",
        profile_params={"min_wall_mm": 1, "min_feature_mm": 1,
                        "overhang": {"max_deg": 45}}, engine_call=missing_query,
    )
    check = _overhang(result)
    assert check["status"] == "not_evaluated" and check["ok"] is None, result
    assert "geometry backend unavailable" in check["note"], result
    assert result["status"] == "not_evaluated" and result["dfm_pass"] is None, result


def test_dfm_overhang_sub_one_square_millimeter_is_not_a_failure(minted_documents):
    doc = _part_with_ops(minted_documents, [
        cb.feat("box", "Stem", {"length": cb.dim(10), "width": cb.dim(10),
                                 "height": cb.dim(20)}),
        cb.feat("box", "Cap", {"length": cb.dim(10.08), "width": cb.dim(10),
                                "height": cb.dim(5), "at": [0, 0, 12.5]}),
    ])
    result = _real_dfm(doc)
    check = _overhang(result)
    assert check["status"] == "pass", result
    # A 0.04 mm ledge is narrower than half an extrusion line: nothing to support.
    assert check["unsupported_area_mm2"] <= 1.0, result


def _real_dfm(doc_id: str, **options) -> dict:
    from mcp.server.fastmcp import FastMCP
    from services.mcp.intake_tools import register_intake_tools

    mcp = FastMCP("fdm-overhang")
    register_intake_tools(mcp)
    _content, structured = asyncio.run(mcp.call_tool(
        "dfm_check", {"doc_id": doc_id, "process": "fdm", **options}))
    return json.loads(structured["result"])


def _overhang(result: dict) -> dict:
    return next(check for check in result["checks"] if check["check"] == "overhang")


def _part_with_ops(minted_documents, ops: list) -> str:
    doc = minted_documents(cb.call(
        "doc_new", {"kind": "part", "name": f"fdm_{uuid.uuid4().hex[:8]}"})["doc_id"])
    result = cb.call("doc_apply_ops", {"doc_id": doc, "ops": ops})
    assert cb.call("doc_query", {"doc_id": doc, "query": {"of": "face"}})["matches"], result
    return doc


def test_dfm_fdm_box_on_plate_passes_and_recommends_zero_support(minted_documents):
    doc = _part_with_ops(minted_documents, [
        cb.feat("box", "Block", {"length": cb.dim(30), "width": cb.dim(20),
                                  "height": cb.dim(20)}),
    ])
    result = _real_dfm(doc, suggest_orientation=True)
    check = _overhang(result)
    assert check["status"] == "pass" and check["ok"] is True, result
    assert check["unsupported_area_mm2"] == 0 and check["unsupported_face_count"] == 0
    best = result["orientation"][0]
    assert best["metrics"]["support_volume_mm3"] == 0, result["orientation"]
    assert best["metrics"]["contact_area_mm2"] > 0, result["orientation"]
    assert result["dfm_pass"] is True, result


def test_dfm_fdm_cantilever_support_area_and_flipped_plate(minted_documents):
    doc = _part_with_ops(minted_documents, [
        cb.feat("box", "Stem", {"length": cb.dim(10), "width": cb.dim(20),
                                 "height": cb.dim(20)}),
        cb.feat("box", "Cap", {"length": cb.dim(30), "width": cb.dim(20),
                                "height": cb.dim(5), "at": [0, 0, 12.5]}),
    ])
    result = _real_dfm(doc, suggest_orientation=True)
    check = _overhang(result)
    assert check["status"] == "fail" and result["dfm_pass"] is False, result
    assert check["unsupported_area_mm2"] == pytest.approx(400, rel=0.05), check
    assert check["unsupported_face_count"] >= 1
    assert all(face["face"] and len(face["centroid"]) == 3 for face in check["worst"])
    support = next(row for row in result["checks"] if row["check"] == "support")
    # Two 10x20 shelves (cap underside z=10) over a plate at the stem foot (z=-10).
    assert support["metrics"]["support_volume_mm3"] == pytest.approx(2 * 10 * 20 * 20,
                                                                     rel=0.1), support
    # Cap down, stem up: the T stands on its own flat top with nothing to support.
    assert result["orientation"][0]["metrics"]["support_volume_mm3"] == 0, result
    flipped = _real_dfm(doc, build_dir=[0, 0, -3])
    assert _overhang(flipped)["status"] == "pass", flipped
    assert _overhang(flipped)["build_dir"] == [0.0, 0.0, -1.0]


@pytest.mark.parametrize("slope, verdict", [(1.0, "pass"), (0.5773502692, "fail")])
def test_dfm_fdm_chamfered_underside_boundary(minted_documents, slope, verdict):
    # XZ prism: a ten-millimetre base reaches the plate; the following 20 mm
    # underside rises by 20*slope, making exactly 45° or 30° to horizontal.
    rise = 20 * slope
    points = [(0, 0), (10, 0), (30, rise), (30, 35), (0, 35)]
    ops = [{"op": "add_feature", "feature": {
        "type": "sketch", "name": "Section",
        "params": {"entities": [], "constraints": []},
        "refs": {"plane": "plane.right"}}}]
    for start, end in zip(points, points[1:] + points[:1]):
        ops.append({"op": "sketch_add_entity", "sketch": "$0",
                    "entity": {"type": "line", "p1": list(start), "p2": list(end)}})
    ops.append({"op": "add_feature", "feature": {
        "type": "extrude", "name": "Prism",
        "params": {"mode": "boss", "end": "blind", "depth": cb.dim(20)},
        "refs": {"profile": [{"kind": "region", "sketch": "$0",
                              "centroid": [12, 10], "hint_index": 0}]}}})
    doc = _part_with_ops(minted_documents, ops)
    result = _real_dfm(doc)
    assert _overhang(result)["status"] == verdict, result


@pytest.mark.parametrize("direction", [[0, 0, 0], [1, 0], [1, float("nan"), 0],
                                             [1, float("inf"), 0], ["bad", 0, 1],
                                             [True, 0, 0]])
def test_dfm_rejects_invalid_build_direction(minted_documents, direction):
    doc = _part_with_ops(minted_documents, [
        cb.feat("box", "Block", {"length": cb.dim(10), "width": cb.dim(10),
                                  "height": cb.dim(10)}),
    ])
    result = _real_dfm(doc, build_dir=direction)
    assert result["ok"] is False and "build_dir" in result["error"], result


def test_dfm_fdm_vertical_through_hole_does_not_require_support(minted_documents):
    doc = _part_with_ops(minted_documents, [
        cb.feat("box", "Block", {"length": cb.dim(30), "width": cb.dim(30),
                                  "height": cb.dim(20)}),
        cb.feat("hole", "Bore", {"style": "simple", "end": "through_all",
                                 "diameter": cb.dim(6)},
                {"face": {"of": "face", "side": "top"}}),
    ])
    result = _real_dfm(doc)
    assert _overhang(result)["status"] == "pass", result


def test_dfm_fdm_upward_top_fillet_is_evaluated(minted_documents):
    doc = _part_with_ops(minted_documents, [
        cb.feat("box", "Block", {"length": cb.dim(30), "width": cb.dim(20),
                                  "height": cb.dim(20)}),
        cb.feat("fillet", "Top radius", {"radius": cb.dim(3)},
                {"edges": {"of": "edge", "side": "+z"}}),
    ])
    result = _real_dfm(doc)
    assert _overhang(result)["status"] == "pass", result
    assert _overhang(result)["unsupported_area_mm2"] == 0, result


def test_dfm_fdm_bottom_fillet_has_bounded_support_area(minted_documents):
    doc = _part_with_ops(minted_documents, [
        cb.feat("box", "Block", {"length": cb.dim(30), "width": cb.dim(20),
                                  "height": cb.dim(20)}),
        cb.feat("fillet", "Bottom radius", {"radius": cb.dim(3)},
                {"edges": {"of": "edge", "side": "-z"}}),
    ])
    result = _real_dfm(doc)
    check = _overhang(result)
    fillet_area = sum(face["area_or_len"] for face in cb.call(
        "doc_query", {"doc_id": doc, "query": {"of": "face"}})["matches"]
        if face["surf"] != "plane")
    assert fillet_area > 0
    assert check["status"] in {"pass", "fail"}, result
    assert 0 <= check["unsupported_area_mm2"] <= fillet_area * 1.02, result


def test_dfm_fdm_horizontal_cylinder_supports_lower_arc(minted_documents):
    radius, length = 10, 30
    doc = _part_with_ops(minted_documents, [
        cb.feat("cylinder", "Raised bar", {
            "radius": cb.dim(radius), "height": cb.dim(length),
            "at": [0, 0, 30], "axis": [1, 0, 0]}),
    ])
    result = _real_dfm(doc)
    check = _overhang(result)
    # Lower arc beyond 45° from vertical, minus the strip within one 0.2 mm layer of the
    # bed (the first layer lies there; nothing below it to support).
    layer = 0.2
    expected = 2 * radius * length * (math.pi / 4 - math.acos(1 - layer / radius))
    assert check["status"] == "fail", result
    assert check["unsupported_area_mm2"] == pytest.approx(expected, rel=0.05), result


def test_dfm_fdm_support_counts_first_of_multiple_bodies(minted_documents):
    doc = _part_with_ops(minted_documents, [
        cb.feat("box", "Stem", {"length": cb.dim(10), "width": cb.dim(20),
                                 "height": cb.dim(20)}),
        cb.feat("box", "Cap", {"length": cb.dim(30), "width": cb.dim(20),
                                "height": cb.dim(5), "at": [0, 0, 12.5]}),
        cb.feat("box", "Separate block", {"length": cb.dim(10),
                                           "width": cb.dim(10), "height": cb.dim(20),
                                           "at": [100, 0, 0], "mode": "new",
                                           "merge": False}),
    ])
    result = _real_dfm(doc)
    check = _overhang(result)
    assert check["status"] == "fail", result
    assert check["unsupported_area_mm2"] == pytest.approx(400, rel=0.05), result
