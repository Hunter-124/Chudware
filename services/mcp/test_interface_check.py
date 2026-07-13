"""Regression tests for interface_check (T-BL49) — real engine geometry, asserted deltas.

Drives the registered interface_check MCP tool end-to-end against the staged libaicad.so.
The worked example: a 4-hole plate stack (bracket + plate). A deliberately MISALIGNED version
must be FLAGGED with the correct position delta; the CORRECTED (registered) version must PASS.

Also covers the shared doc_query `body_id` selector this tool depends on (T-BL49 engine change):
per-body face selection + out-of-range guard + default (bodies[-1]) behavior.

Run:  python -m pytest services/mcp/test_interface_check.py -v
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest

from mcp_server import core_bridge as cb

pytestmark = pytest.mark.skipif(
    not cb.available(),
    reason="libaicad not staged; engine-backed interface_check unavailable",
)


def _call(name: str, args: dict):
    from mcp_server import server
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

def _build_stack(dx: float):
    r, _ = _call("new_part", {"name": "stack"})
    doc = r["doc_id"]
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
    assert "body_id" in str(exc.value) or "out of range" in str(exc.value), str(exc.value)


# --- the worked regression: misaligned flagged, corrected passes -------------------------------
def test_interface_check_misaligned_bracket_plate_is_flagged():
    """A plate whose 4-hole pattern is offset 1.5 mm in +X from the bracket is FLAGGED with the
    correct position delta and fix direction."""
    dx = 1.5
    doc = _build_stack(dx=dx)
    r, _ = _call("interface_check", {"doc_id": doc, "components": ["f2", "f3"]})
    assert r is not None and r["tool"] == "interface_check" and r["version"] == 1, r
    assert r["ok"] is False, r  # misaligned -> not ok
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
    r, _ = _call("new_part", {"name": "solo"})
    doc = r["doc_id"]
    _call("add_features", {"ops": [
        cb.feat("box", "B", {"length": cb.dim(20), "width": cb.dim(20), "height": cb.dim(10)})],
        "doc_id": doc})
    r2, _ = _call("interface_check", {"doc_id": doc})
    assert r2["ok"] is True and r2["pairs"] == [], r2


def test_interface_check_in_design_review_battery():
    """design_review runs interface_check and reports the alignment failure in its verdict."""
    from services.mcp.design_review_tools import _run_design_review
    doc = _build_stack(dx=1.5)
    text = _run_design_review(doc_id=doc, checks=["interface_check"])
    assert "Interface alignment" in text, text
    assert "FAIL" in text, text
