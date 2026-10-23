"""Regression tests for interface_check — real engine geometry, asserted deltas.

Drives the registered interface_check MCP tool end-to-end against the staged libchudware.so.
The worked example: a 4-hole plate stack (bracket + plate). A deliberately MISALIGNED version
must be FLAGGED with the correct position delta; the CORRECTED (registered) version must PASS.

Also covers the shared doc_query `body_id` selector this tool depends on:
per-body face selection, world-placed assembly bodies and the out-of-range guard.

Run:  python -m pytest services/mcp/test_interface_check.py -v
"""
from __future__ import annotations

import asyncio
import itertools
import json
import sys
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest

from services.mcp import core_bridge as cb

pytestmark = pytest.mark.skipif(
    not cb.available(),
    reason="libchudware not staged; engine-backed interface_check unavailable",
)


def _call(name: str, args: dict):
    from services.mcp import server
    content, structured = asyncio.run(server.mcp.call_tool(name, args))
    text = structured.get("result") if isinstance(structured, dict) else None
    parsed = None
    if isinstance(text, str):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
    return parsed, content


# --- fixture: a two-body stack -----------------------------------------------------------------
# Body A (bracket): 60x40x10 plate spanning z[-5,5] with a 4-hole bolt circle (Ø6, pcd 30).
# Body B (plate):   a transform-copy lifted +10 in Z so its bottom face (z=5) sits FLUSH on A's
#                   top face (z=5).  The copy's holes are coaxial with A's -> aligned.
# Misaligned variant: the copy is ALSO shifted +dx in X, so its whole hole pattern is offset dx
#                   in X relative to A while the contact faces stay flush.

# Every test needs its OWN document: new_part(name=...) deliberately RESUMES an already-open
# part of that name (cad_doc_tools.py:4426 — "a part of that name that is ALREADY OPEN is
# resumed, not duplicated"), so a fixed name made all six stack tests build into
# doc-cpp00000001 and each one saw the bodies left by its predecessors.
_STACK_SEQ = itertools.count(1)


def _fresh_doc(prefix: str) -> str:
    r, _ = _call("new_part", {"name": f"{prefix}_{next(_STACK_SEQ)}", "fresh": True})
    assert r and r.get("ok") and not r.get("reused"), r
    return r["doc_id"]


def _build_stack(dx: float):
    doc = _fresh_doc("stack")
    ops = [
        cb.feat("box", "Bracket", {"length": cb.dim(60), "width": cb.dim(40), "height": cb.dim(10)}),
        {"op": "add_feature", "feature": {
            "type": "bolt_circle", "name": "BC",
            "params": {"diameter": 6, "count": 4, "pcd": 30},
            "refs": {"face": {"of": "face", "side": "top"}}}},
        cb.feat("transform", "Plate", {"translate": [dx, 0.0, 10.0], "copy": True}),
    ]
    r, _ = _call("add_features", {"ops": ops, "doc_id": doc})
    assert r["ok"], r
    assert r["body_count"] == 2, r
    return doc


# --- doc_query body_id selector (shared engine path) -------------------------------------------
def test_doc_query_body_id_selects_per_body():
    """The 1-based body_id routes doc_query to a SPECIFIC body; default = the WHOLE doc.

    Default contract changed 2026-07-12 (asm-fea F1): an un-scoped query resolves across
    EVERY body of a multi-body/assembly doc, not bodies[-1] — the last-body default made
    FEA fixtures/loads unable to address any other component (constraints on an
    assembly's wheels literally could not resolve). body_id keeps per-body selection.
    """
    doc = _build_stack(dx=0.0)  # two coaxial plates, B lifted +10 in Z
    # Body 1 = bracket (holes around z=0), body 2 = plate copy (holes around z=10).
    q = {"of": "face", "surf": "round"}
    r1 = cb.call("doc_query", {"doc_id": doc, "query": {**q, "body_id": 1}})
    r2 = cb.call("doc_query", {"doc_id": doc, "query": {**q, "body_id": 2}})
    rd = cb.call("doc_query", {"doc_id": doc, "query": dict(q)})  # default -> whole doc

    def zmean(res):
        cyl = [m for m in res.get("matches", []) if m.get("surf") == "cylinder"]
        return sum(m["centroid"][2] for m in cyl) / max(len(cyl), 1), len(cyl)

    z1, n1 = zmean(r1)
    z2, n2 = zmean(r2)
    zd, nd = zmean(rd)
    assert n1 > 0 and n2 > 0, (n1, n2)
    # body 1 holes sit lower in Z than body 2 holes (lifted +10) -> per-body selection works.
    assert z1 < z2 - 3.0, (z1, z2)
    # default query == UNION of both bodies' faces (whole-doc resolution).
    assert nd == n1 + n2, (nd, n1, n2)
    lo, hi = min(z1, z2), max(z1, z2)
    assert lo - 1e-6 < zd < hi + 1e-6, (zd, z1, z2)


def test_doc_query_body_id_out_of_range():
    """An out-of-range body_id is a clear error, not a silent fallback."""
    doc = _build_stack(dx=0.0)
    with pytest.raises(cb.CoreError) as exc:
        cb.call("doc_query", {"doc_id": doc, "query": {"of": "face", "surf": "round", "body_id": 99}})
    assert exc.value.code == "invalid_op"


@pytest.mark.parametrize("dx", [0.0, 1.5])
def test_interface_check_world_placed_assembly(tmp_path, dx):
    """A real assembly must measure placed component faces, not its empty part tree."""
    plate = _fresh_doc("interface_source")
    assembly = None
    try:
        result, _ = _call("add_features", {"doc_id": plate, "ops": [
            cb.feat("box", "Plate", {"length": cb.dim(60), "width": cb.dim(40),
                                    "height": cb.dim(10)}),
            cb.feat("bolt_circle", "Holes", {"diameter": 6, "count": 4, "pcd": 30},
                    {"face": {"of": "face", "side": "top"}}),
        ]})
        assert result["ok"], result
        path = str(tmp_path / "interface_source.step+")
        result, _ = _call("save_part", {"doc_id": plate, "path": path})
        assert result["ok"], result
        result, _ = _call("new_assembly", {"name": f"interface_assembly_{next(_STACK_SEQ)}"})
        assert result["ok"], result
        assembly = result["doc_id"]
        for placement in ([100, 200, 30], [100 + dx, 200, 40]):
            result, _ = _call("asm_add_component",
                              {"doc_id": assembly, "path": path, "at": placement})
            assert result["ok"], result
        for body, center in enumerate(([100, 200, 30], [100 + dx, 200, 40]), 1):
            result = cb.call("doc_query", {"doc_id": assembly,
                             "query": {"of": "face", "surf": "round", "body_id": body}})
            cylinders = [m for m in result["matches"] if m["surf"] == "cylinder"]
            assert len(cylinders) == 4, result
            mean = [sum(face["centroid"][axis] for face in cylinders) / 4
                    for axis in range(3)]
            assert mean == pytest.approx(center, abs=1e-6)
        with pytest.raises(cb.CoreError):
            cb.call("doc_query", {"doc_id": assembly,
                    "query": {"of": "face", "body_id": 3}})
        result, _ = _call("interface_check",
                          {"doc_id": assembly, "components": ["k1", "k2"]})
        assert result["ok"] is True, result  # a misalignment is a finding, not a tool failure
        assert result["status"] == ("pass" if dx == 0 else "fail"), result
        assert len(result["pairs"]) == 1, result
        holes = result["pairs"][0]["checks"]["hole_pattern"]
        assert holes["status"] == ("pass" if dx == 0 else "fail"), holes
        records = holes["matched"] if dx == 0 else holes["misaligned"]
        assert len(records) == 4, holes
        assert [record["pos_delta_mm"] for record in records] == pytest.approx([dx] * 4)
    finally:
        if assembly is not None:
            _call("close_part", {"doc_id": assembly})
        _call("close_part", {"doc_id": plate})


# --- the worked regression: misaligned flagged, corrected passes -------------------------------
def test_interface_check_misaligned_bracket_plate_is_flagged():
    """A plate whose 4-hole pattern is offset 1.5 mm in +X from the bracket is FLAGGED with the
    correct position delta and fix direction."""
    dx = 1.5
    doc = _build_stack(dx=dx)
    r, _ = _call("interface_check", {"doc_id": doc, "components": ["f2", "f3"]})
    assert r is not None and r["tool"] == "interface_check" and r["version"] == 1, r
    assert r["ok"] is True and r["status"] == "fail", r  # misaligned -> finding, check ran
    assert len(r["pairs"]) == 1, r

    hp = r["pairs"][0]["checks"]["hole_pattern"]
    assert hp["status"] == "fail", hp
    assert hp["misaligned"], hp
    # every matched-but-misaligned hole should report ~1.5 mm position delta.
    deltas = [m["pos_delta_mm"] for m in hp["misaligned"]]
    assert deltas, hp
    for d in deltas:
        assert abs(d - dx) < 0.05, (d, dx, hp)
    # the offset vector points predominantly in X.
    for m in hp["misaligned"]:
        ov = m["offset_vec_mm"]
        assert abs(ov[0]) > abs(ov[1]) and abs(ov[0]) > abs(ov[2]), ov

    # findings are actionable: name the components + numeric error + fix direction.
    hp_finds = [f for f in r["findings"] if f["check"] == "hole_pattern" and f["severity"] == "fail"]
    assert hp_finds, r["findings"]
    f0 = hp_finds[0]
    assert f0["component_a"] == "f2" and f0["component_b"] == "f3", f0
    assert abs(f0["error_mm"] - dx) < 0.05, f0
    assert "X" in f0["detail"] and "f3" not in f0["detail"] or "plate" in f0["detail"].lower() \
        or "offset" in f0["detail"], f0
    assert f0["fix"], f0


def test_interface_check_corrected_bracket_plate_passes():
    """The same stack with the plate registered (dx=0) passes the hole-pattern + coaxiality
    checks — no alignment failures."""
    doc = _build_stack(dx=0.0)
    r, _ = _call("interface_check", {"doc_id": doc, "components": ["f2", "f3"]})
    assert r is not None, r
    hp = r["pairs"][0]["checks"]["hole_pattern"]
    assert hp["status"] == "pass", hp
    assert not hp["misaligned"], hp
    assert hp["matched"], hp  # holes matched across the interface
    for m in hp["matched"]:
        assert m["pos_delta_mm"] < 0.05, m
    # no hole_pattern / coaxiality FAIL findings.
    fails = [f for f in r["findings"]
             if f["severity"] == "fail" and f["check"] in ("hole_pattern", "coaxiality")]
    assert not fails, fails


def test_interface_check_fastener_passthrough_flags_undersized():
    """An M8 fastener does NOT pass the Ø6 bores -> fastener_passthrough flags every stack."""
    doc = _build_stack(dx=0.0)
    r, _ = _call("interface_check", {"doc_id": doc, "components": ["f2", "f3"], "fastener": "M8"})
    fp = r["pairs"][0]["checks"]["fastener_passthrough"]
    assert fp["status"] == "fail", fp
    assert fp["fastener"] == "M8", fp
    assert all(not s["passes"] for s in fp["stacks"]), fp
    # nominal M8 major diameter is 8 mm; clearance = 6 - 8 = -2 mm.
    for s in fp["stacks"]:
        assert s["nominal_mm"] == pytest.approx(8.0, abs=0.01), s
        assert s["clearance_mm"] < 0, s


def test_interface_check_fastener_passthrough_passes_when_clear():
    """An M5 fastener clears the Ø6 bores -> fastener_passthrough passes."""
    doc = _build_stack(dx=0.0)
    r, _ = _call("interface_check", {"doc_id": doc, "components": ["f2", "f3"], "fastener": "M5"})
    fp = r["pairs"][0]["checks"]["fastener_passthrough"]
    assert fp["status"] == "pass", fp
    assert all(s["passes"] for s in fp["stacks"]), fp


def test_interface_check_single_body_is_noop():
    """<2 bodies -> nothing to compare, ok True, no pairs."""
    doc = _fresh_doc("solo")
    _call("add_features", {"ops": [
        cb.feat("box", "B", {"length": cb.dim(20), "width": cb.dim(20), "height": cb.dim(10)})],
        "doc_id": doc})
    r2, _ = _call("interface_check", {"doc_id": doc})
    assert r2["ok"] is True and r2["status"] == "skip" and r2["pairs"] == [], r2


def test_unknown_component_is_refused_not_a_silent_pass():
    """An id this document does not have used to be filtered out silently, leaving zero pairs,
    zero findings and ok=True — the caller asked "do these two line up?" and was told YES about
    components that do not exist. It must be a teaching refusal instead."""
    doc = _build_stack(dx=0.0)
    r, _ = _call("interface_check", {"doc_id": doc, "components": ["bogusA", "bogusB"]})
    assert r["ok"] is False, r
    assert r["code"] == "unknown_component", r
    assert r["unknown_components"] == ["bogusA", "bogusB"], r
    # teaches: the offending values, the accepted set, and the corrected call.
    assert "bogusA" in r["error"], r
    assert set(r["available_components"]) == {"f2", "f3"}, r
    assert "f2" in r["error"] and "interface_check(doc_id=" in r["error"], r


def test_partially_unknown_components_is_refused():
    """One good id + one bogus id is still a refusal — not a check of the good one alone."""
    doc = _build_stack(dx=0.0)
    r, _ = _call("interface_check", {"doc_id": doc, "components": ["f2", "nope"]})
    assert r["ok"] is False and r["code"] == "unknown_component", r
    assert r["unknown_components"] == ["nope"], r


def test_single_component_is_refused_not_silently_widened():
    """A 1-element components list used to fall through to whole-doc proximity pairing, so the
    caller got a verdict about a pair it never named, reported as if it had asked for it."""
    doc = _build_stack(dx=0.0)
    r, _ = _call("interface_check", {"doc_id": doc, "components": ["f2"]})
    assert r["ok"] is False and r["code"] == "components_needs_pair", r
    assert "f3" in r["error"], r


def test_verdict_surfaces_the_scope_it_considered():
    """The verdict names how many bodies it weighed and which pairing rule it used, so a doc
    carrying a scratch body cannot yield a silently narrow-looking clash report."""
    doc = _build_stack(dx=0.0)
    r, _ = _call("interface_check", {"doc_id": doc, "components": ["f2", "f3"]})
    assert r["body_count"] == 2, r
    assert r["pair_selection"] == "explicit", r
    assert "scope: 2 body/bodies" in r["summary"], r["summary"]

    # A third, unrelated scratch body 300 mm away (a transform copy — a plain box at an offset
    # fuses into the existing body instead of becoming its own). Only the bracket/plate pair is
    # adjacent, so `pairs` still has 1 entry: "All 1 interface(s) aligned" on its own would hide
    # that the doc holds 3 bodies. body_count/pair_selection + the scope line say so.
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("transform", "Scratch", {"translate": [300.0, 0.0, 0.0], "copy": True})]})
    r2, _ = _call("interface_check", {"doc_id": doc})
    assert r2["body_count"] == 3, r2
    assert r2["pair_selection"] == "proximity", r2
    assert len(r2["pairs"]) == 1, r2
    assert "scope: 3 body/bodies" in r2["summary"], r2["summary"]
    assert "1 pair(s) checked" in r2["summary"], r2["summary"]


def test_interface_check_in_design_review_battery():
    """design_review runs interface_check and reports the alignment failure in its verdict."""
    from services.mcp.design_review_tools import _run_design_review
    doc = _build_stack(dx=1.5)
    text = _run_design_review(doc_id=doc, checks=["interface_check"])
    assert "Interface alignment" in text, text
    assert "FAIL" in text, text


@pytest.mark.parametrize("dx", [0.0, 0.75])
def test_partial_cylinder_coaxiality_uses_geometric_axis(tmp_path, dx):
    """A D-shaped shaft's surface centroid must not displace its geometric axis."""
    shaft = _fresh_doc("partial_shaft")
    plate = _fresh_doc("partial_shaft_socket")
    assembly = None
    try:
        result, _ = _call("add_features", {"doc_id": shaft, "ops": [
            cb.feat("cylinder", "Shaft", {"radius": cb.dim(2), "height": cb.dim(10),
                                        "axis": "+z", "centered": True}),
            cb.feat("box", "Flat", {"length": cb.dim(4), "width": cb.dim(8),
                                    "height": cb.dim(12), "at": [-2, 0, 0], "mode": "cut"}),
        ]})
        assert result["ok"], result
        result, _ = _call("add_features", {"doc_id": plate, "ops": [
            cb.feat("box", "Socket", {"length": cb.dim(12), "width": cb.dim(12),
                                      "height": cb.dim(4)}),
            cb.feat("cylinder", "Bore", {"radius": cb.dim(2.2), "height": cb.dim(6),
                                       "axis": "+z", "centered": True, "mode": "cut"}),
        ]})
        assert result["ok"], result
        paths = [str(tmp_path / "shaft.step+"), str(tmp_path / "socket.step+")]
        for document, path in zip((shaft, plate), paths):
            result, _ = _call("save_part", {"doc_id": document, "path": path})
            assert result["ok"], result
        result, _ = _call("new_assembly", {"name": f"partial_axis_{next(_STACK_SEQ)}"})
        assert result["ok"], result
        assembly = result["doc_id"]
        for path, placement in zip(paths, ([0, 0, 0], [dx, 0, 4])):
            result, _ = _call("asm_add_component",
                              {"doc_id": assembly, "path": path, "at": placement})
            assert result["ok"], result
        result, _ = _call("interface_check",
                          {"doc_id": assembly, "components": ["k1", "k2"]})
        coax = result["pairs"][0]["checks"]["coaxiality"]
        assert coax["status"] == ("pass" if dx == 0 else "fail"), coax
        assert len(coax["pairs"]) == 1, coax
        assert coax["pairs"][0]["axis_offset_mm"] == pytest.approx(dx), coax
        assert coax["pairs"][0]["axis_angle_deg"] == pytest.approx(0), coax
    finally:
        if assembly is not None:
            _call("close_part", {"doc_id": assembly})
        _call("close_part", {"doc_id": shaft})
        _call("close_part", {"doc_id": plate})
