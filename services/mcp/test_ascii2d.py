"""Deterministic ASCII 2D tools — PCB / sketch / experimental silhouette.

Acceptance (owner item 5):
  * PCB fixture with a known same-layer crossing is flagged.
  * Open-profile sketch is flagged.
  * ASCII outputs are stable across runs (deterministic).
  * Mutation readbacks (sketch + board) include ascii+summary, enforce caps,
    and are byte-stable.

Run:  python3 -m pytest services/mcp/test_ascii2d.py -q
"""
from __future__ import annotations

import json

from mcp_server.ascii2d import (
    READBACK_MAX_H,
    READBACK_MAX_W,
    attach_board_readback,
    attach_sketch_readback,
    board_ascii_report,
    board_mutation_readback,
    check_board,
    check_sketch_geometry,
    register_ascii2d_tools,
    render_ascii_3d,
    render_board_layer_ascii,
    segments_cross,
    sketch_ascii_report,
    sketch_ids_from_ops,
    sketch_mutation_readback,
)


class _StubMCP:
    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, *args, **kwargs):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def _crossing_board() -> dict:
    """Two different-net tracks that properly cross on F.Cu + two courtyard-overlap parts."""
    return {
        "name": "cross_fixture",
        "stackup": {
            "copper_layers": 2,
            "layers": [
                {"id": "F.Cu", "type": "copper", "thickness": 0.035},
                {"id": "diel1", "type": "core", "thickness": 1.53},
                {"id": "B.Cu", "type": "copper", "thickness": 0.035},
            ],
        },
        "outline": {
            "closed": True,
            "shapes": [
                {"type": "line", "p1": [0, 0], "p2": [40, 0]},
                {"type": "line", "p1": [40, 0], "p2": [40, 30]},
                {"type": "line", "p1": [40, 30], "p2": [0, 30]},
                {"type": "line", "p1": [0, 30], "p2": [0, 0]},
            ],
        },
        "footprints": [
            # R1 and R2 intentionally share courtyard (overlap)
            {"id": "fp1", "ref": "R1", "pos": [10.0, 10.0], "angle": 0.0, "side": "top",
             "pads": [
                 {"id": "p1", "number": "1", "pos": [-1.0, 0.0], "size": [1.0, 1.0],
                  "type": "smd", "layers": ["F.Cu"], "net": "A"},
                 {"id": "p2", "number": "2", "pos": [1.0, 0.0], "size": [1.0, 1.0],
                  "type": "smd", "layers": ["F.Cu"], "net": "B"},
             ],
             "graphics": [
                 {"type": "rect", "layer": "F.CrtYd", "p1": [-3, -3], "p2": [3, 3]},
             ]},
            {"id": "fp2", "ref": "R2", "pos": [12.0, 11.0], "angle": 0.0, "side": "top",
             "pads": [
                 {"id": "p1", "number": "1", "pos": [-1.0, 0.0], "size": [1.0, 1.0],
                  "type": "smd", "layers": ["F.Cu"], "net": "C"},
                 {"id": "p2", "number": "2", "pos": [1.0, 0.0], "size": [1.0, 1.0],
                  "type": "smd", "layers": ["F.Cu"], "net": "D"},
             ],
             "graphics": [
                 {"type": "rect", "layer": "F.CrtYd", "p1": [-3, -3], "p2": [3, 3]},
             ]},
            # far parts for the crossing nets A and B (multi-pad, unconnected copper)
            {"id": "fp3", "ref": "U1", "pos": [5.0, 5.0], "angle": 0.0, "side": "top",
             "pads": [
                 {"id": "p1", "number": "1", "pos": [0.0, 0.0], "size": [1.0, 1.0],
                  "type": "smd", "layers": ["F.Cu"], "net": "A"},
             ]},
            {"id": "fp4", "ref": "U2", "pos": [35.0, 25.0], "angle": 0.0, "side": "top",
             "pads": [
                 {"id": "p1", "number": "1", "pos": [0.0, 0.0], "size": [1.0, 1.0],
                  "type": "smd", "layers": ["F.Cu"], "net": "A"},
             ]},
            {"id": "fp5", "ref": "U3", "pos": [5.0, 25.0], "angle": 0.0, "side": "top",
             "pads": [
                 {"id": "p1", "number": "1", "pos": [0.0, 0.0], "size": [1.0, 1.0],
                  "type": "smd", "layers": ["F.Cu"], "net": "B"},
             ]},
            {"id": "fp6", "ref": "U4", "pos": [35.0, 5.0], "angle": 0.0, "side": "top",
             "pads": [
                 {"id": "p1", "number": "1", "pos": [0.0, 0.0], "size": [1.0, 1.0],
                  "type": "smd", "layers": ["F.Cu"], "net": "B"},
             ]},
        ],
        # Diagonal A and B cross at centre of board
        "tracks": [
            {"id": "tA", "kind": "segment", "p1": [5.0, 5.0], "p2": [35.0, 25.0],
             "width": 0.3, "net": "A", "layer": "F.Cu"},
            {"id": "tB", "kind": "segment", "p1": [5.0, 25.0], "p2": [35.0, 5.0],
             "width": 0.3, "net": "B", "layer": "F.Cu"},
        ],
        "vias": [
            {"id": "v1", "pos": [20.0, 15.0], "net": "A", "diameter": 0.6},
        ],
        "nets": [
            {"name": "A", "class": "default"},
            {"name": "B", "class": "default"},
            {"name": "C", "class": "default"},
            {"name": "D", "class": "default"},
            {"name": "GND", "class": "default"},
        ],
        "zones": [],
        "graphics": [],
    }


def _open_sketch() -> dict:
    """Three lines forming an open U (not closed at the top)."""
    return {
        "entities": [
            {"id": "e1", "type": "line", "p1": [0.0, 0.0], "p2": [0.0, 20.0]},
            {"id": "e2", "type": "line", "p1": [0.0, 0.0], "p2": [30.0, 0.0]},
            {"id": "e3", "type": "line", "p1": [30.0, 0.0], "p2": [30.0, 20.0]},
        ],
        "constraints": [
            {"id": "c1", "type": "vertical", "entities": ["e1"]},
            {"id": "c2", "type": "horizontal", "entities": ["e2"]},
            {"id": "c3", "type": "distance", "value": 30.0, "entities": ["e2"]},
        ],
        "health": {
            "ok": True,
            "status": "under",
            "dof": 4,
            "dof_narration": "4 free variables remain (position + size)",
            "closure": {
                "closed_chains_ok": False,
                "open_endpoints": ["e1.end", "e3.end"],
                "material_regions": 0,
            },
            "statuses": {"e1": "under", "e2": "under", "e3": "under"},
            "schedule": [
                {"order": 1, "kind": "relation", "action": "coincident",
                 "handles": ["e1.end", "e3.end"], "why": "close the profile"},
            ],
        },
    }


# --------------------------------------------------------------------------- unit


def test_segments_cross_detects_proper_intersection():
    assert segments_cross((0, 0), (10, 10), (0, 10), (10, 0)) is True
    # shared endpoint is NOT a cross
    assert segments_cross((0, 0), (10, 0), (10, 0), (10, 10)) is False
    # parallel no cross
    assert segments_cross((0, 0), (10, 0), (0, 1), (10, 1)) is False


def test_pcb_crossing_flagged():
    board = _crossing_board()
    findings = check_board(board)
    codes = {f["code"] for f in findings}
    assert "trace_crossing" in codes, findings
    crosses = [f for f in findings if f["code"] == "trace_crossing"]
    assert any("A" in f.get("nets", []) and "B" in f.get("nets", []) for f in crosses), crosses
    # courtyard overlap of R1/R2
    assert "courtyard_overlap" in codes, findings
    overs = [f for f in findings if f["code"] == "courtyard_overlap"]
    assert any(set(f.get("refs", [])) == {"R1", "R2"} for f in overs), overs


def test_pcb_ascii_report_stable_and_complete():
    board = _crossing_board()
    r1 = board_ascii_report(board, pitch_mm=2.0)
    r2 = board_ascii_report(board, pitch_mm=2.0)
    assert r1["ok"] and r2["ok"]
    assert r1["ascii"] == r2["ascii"], "ASCII must be deterministic across runs"
    assert r1["summary"] == r2["summary"]
    assert r1["findings"] == r2["findings"]
    assert "trace_crossing" in {f["code"] for f in r1["findings"]}
    assert "F.Cu" in r1["ascii"] or any(L["layer"] == "F.Cu" for L in r1["layers"])
    # layer block non-empty
    assert r1["layers"] and r1["layers"][0]["ascii"]
    assert isinstance(r1["summary"], str) and "crossing" in r1["summary"].lower() \
        or "Checks:" in r1["summary"]


def test_pcb_layer_contains_trace_glyphs():
    board = _crossing_board()
    block = render_board_layer_ascii(board, layer="F.Cu", pitch_mm=2.0)
    ascii_txt = block["ascii"]
    # some path / pad / via / cross glyph present
    assert any(ch in ascii_txt for ch in ("-", "|", "/", "\\", "X", "#", "o", "+")), ascii_txt
    assert "R1" in ascii_txt or "U1" in ascii_txt


def test_open_profile_sketch_flagged():
    sk = _open_sketch()
    report = sketch_ascii_report(sk, pitch=1.0)
    assert report["ok"]
    codes = {f["code"] for f in report["findings"]}
    assert "open_profile" in codes, report["findings"]
    # health-driven under-dimensioned
    assert "under_dimensioned" in codes, report["findings"]
    assert "OPEN" in report["summary"] or "dangling" in report["summary"].lower()


def test_open_profile_geometry_only():
    """Without health, geometry-based dangling endpoint detection still fires."""
    ents = [
        {"id": "a", "type": "line", "p1": [0, 0], "p2": [10, 0]},
        {"id": "b", "type": "line", "p1": [10, 0], "p2": [10, 10]},
    ]
    findings = check_sketch_geometry(ents, near_tol=0.01, health=None)
    assert any(f["code"] == "open_profile" for f in findings), findings


def test_closed_rect_no_open_profile():
    ents = [
        {"id": "a", "type": "line", "p1": [0, 0], "p2": [10, 0]},
        {"id": "b", "type": "line", "p1": [10, 0], "p2": [10, 10]},
        {"id": "c", "type": "line", "p1": [10, 10], "p2": [0, 10]},
        {"id": "d", "type": "line", "p1": [0, 10], "p2": [0, 0]},
    ]
    health = {
        "status": "full", "dof": 0,
        "closure": {"closed_chains_ok": True, "open_endpoints": [], "material_regions": 1},
        "dof_narration": "fully constrained",
    }
    findings = check_sketch_geometry(ents, health=health)
    assert not any(f["code"] == "open_profile" for f in findings), findings


def test_sketch_ascii_deterministic():
    sk = _open_sketch()
    a = sketch_ascii_report(sk, pitch=1.0)
    b = sketch_ascii_report(sk, pitch=1.0)
    assert a["ascii"] == b["ascii"]
    assert a["summary"] == b["summary"]
    assert a["findings"] == b["findings"]
    assert a["ascii"].strip()  # non-empty grid


def test_render_ascii_3d_bbox_experimental_stable():
    bb = {"min": [0, 0, 0], "max": [10, 4, 6]}
    r1 = render_ascii_3d(bbox=bb, view="front", pitch=1.0)
    r2 = render_ascii_3d(bbox=bb, view="front", pitch=1.0)
    assert r1["ok"] and r1["experimental"] is True
    assert r1["ascii"] == r2["ascii"]
    assert "EXPERIMENTAL" in r1["summary"]
    assert r1["ascii"].strip()


def test_mcp_registration_and_board_json_tool():
    mcp = _StubMCP()
    register_ascii2d_tools(mcp)
    for name in ("render_board_ascii", "render_sketch_ascii", "render_ascii"):
        assert name in mcp.tools, name

    board = _crossing_board()
    out = json.loads(mcp.tools["render_board_ascii"](
        board_json=json.dumps(board), pitch_mm=2.0, layer="F.Cu"))
    assert out["ok"], out
    assert any(f["code"] == "trace_crossing" for f in out["findings"]), out["findings"]
    assert out["ascii"]

    sk = _open_sketch()
    out2 = json.loads(mcp.tools["render_sketch_ascii"](
        sketch_json=json.dumps(sk), pitch=1.0))
    assert out2["ok"], out2
    assert any(f["code"] == "open_profile" for f in out2["findings"]), out2["findings"]

    out3 = json.loads(mcp.tools["render_ascii"](
        bbox_json=json.dumps({"min": [0, 0, 0], "max": [5, 5, 5]}), view="iso"))
    assert out3["ok"] and out3.get("experimental") is True
    # determinism via tool
    out3b = json.loads(mcp.tools["render_ascii"](
        bbox_json=json.dumps({"min": [0, 0, 0], "max": [5, 5, 5]}), view="iso"))
    assert out3["ascii"] == out3b["ascii"]


def test_near_coincident_flagged():
    ents = [
        {"id": "a", "type": "line", "p1": [0, 0], "p2": [10, 0]},
        {"id": "b", "type": "line", "p1": [10.005, 0.0], "p2": [20, 0]},
    ]
    findings = check_sketch_geometry(ents, near_tol=0.001, health={
        "status": "under", "dof": 2,
        "closure": {"open_endpoints": ["a.start", "b.end"]},
    })
    assert any(f["code"] == "near_coincident" for f in findings), findings


# --------------------------------------------------------------------------- mutation readback


def test_sketch_mutation_readback_has_ascii_and_summary():
    sk = _open_sketch()
    rb = sketch_mutation_readback(sk, pitch=1.0, sketch_id="s1")
    assert rb["ok"] is True
    assert rb["kind"] == "sketch_readback"
    assert rb["ascii"] and isinstance(rb["ascii"], str)
    assert rb["ascii_block"].startswith("[sketch_readback")
    assert "ascii" in rb and "summary" in rb
    assert "Entities:" in rb["summary"] or "entit" in rb["summary"].lower()
    assert "Dimensions:" in rb["summary"]
    assert "Constraints:" in rb["summary"]
    # open profile + under-dim should surface
    codes = {f.get("code") for f in rb["findings"]}
    assert "open_profile" in codes or "OPEN" in rb["summary"]
    # NL parity: named entity / dim callouts
    assert "e1" in rb["summary"] or "line" in rb["summary"].lower()
    assert "distance" in rb["summary"].lower() or "c3" in rb["summary"]


def test_sketch_mutation_readback_caps_and_determinism():
    sk = _open_sketch()
    a = sketch_mutation_readback(sk, pitch=0.1, max_h=40, max_w=120)
    b = sketch_mutation_readback(sk, pitch=0.1, max_h=40, max_w=120)
    assert a["ascii"] == b["ascii"]
    assert a["summary"] == b["summary"]
    assert a["findings"] == b["findings"]
    assert a["ascii_block"] == b["ascii_block"]
    rows = a["ascii"].split("\n")
    assert len(rows) <= READBACK_MAX_H
    assert all(len(r) <= READBACK_MAX_W for r in rows)
    assert a["height"] <= READBACK_MAX_H
    assert a["width"] <= READBACK_MAX_W
    # force tiny cap to exercise truncation path
    tiny = sketch_mutation_readback(sk, pitch=0.05, max_h=5, max_w=10)
    assert tiny["height"] <= 5
    assert tiny["width"] <= 10
    assert all(len(r) <= 10 for r in tiny["ascii"].split("\n"))


def test_board_mutation_readback_has_ascii_summary_net_delta():
    board = _crossing_board()
    rb = board_mutation_readback(board, pitch_mm=2.0, layer="F.Cu")
    assert rb["ok"] is True
    assert rb["kind"] == "board_readback"
    assert rb["ascii"] and isinstance(rb["ascii"], str)
    assert rb["ascii_block"].startswith("[board_readback")
    assert "summary" in rb and isinstance(rb["summary"], str)
    assert "net_delta" in rb
    nd = rb["net_delta"]
    assert nd["tracks"] >= 2
    assert nd["footprints"] >= 2
    assert nd["crossings"] >= 1 or any(
        f.get("code") == "trace_crossing" for f in rb["findings"])
    # NL connectivity
    assert "Net connectivity" in rb["summary"] or "pad" in rb["summary"].lower()


def test_board_mutation_readback_caps_and_determinism():
    board = _crossing_board()
    a = board_mutation_readback(board, pitch_mm=1.0, max_h=40, max_w=120)
    b = board_mutation_readback(board, pitch_mm=1.0, max_h=40, max_w=120)
    assert a["ascii"] == b["ascii"]
    assert a["summary"] == b["summary"]
    assert a["findings"] == b["findings"]
    assert a["net_delta"] == b["net_delta"]
    rows = a["ascii"].split("\n")
    assert len(rows) <= READBACK_MAX_H
    assert all(len(r) <= READBACK_MAX_W for r in rows)
    tiny = board_mutation_readback(board, pitch_mm=0.5, max_h=6, max_w=12)
    assert tiny["height"] <= 6
    assert tiny["width"] <= 12


def test_attach_helpers_mutate_response():
    sk = _open_sketch()
    board = _crossing_board()
    resp_s: dict = {"ok": True, "doc_id": "d1"}
    attach_sketch_readback(resp_s, sk, sketch_id="s1", pitch=1.0)
    assert resp_s["ascii"]
    assert resp_s["ascii_block"]
    assert resp_s["sketch_summary"]
    assert resp_s["ascii_readback"]["ok"] is True

    resp_b: dict = {"ok": True, "doc_id": "pcb1"}
    attach_board_readback(resp_b, board, pitch_mm=2.0, layer="F.Cu")
    assert resp_b["ascii"]
    assert resp_b["net_delta"]["tracks"] >= 1
    assert resp_b["ascii_readback"]["ok"] is True


def test_sketch_ids_from_ops_deterministic():
    ops = [
        {"op": "add_feature", "feature": {"type": "box"}},
        {"op": "sketch_add_entity", "sketch": "s2",
         "entity": {"type": "line", "p1": [0, 0], "p2": [1, 0]}},
        {"op": "sketch_add_constraint", "sketch": "s1",
         "constraint": {"type": "horizontal", "refs": ["e1"]}},
        {"op": "sketch_set_dim", "sketch": "s2", "id": "c1", "value": 10},
        {"op": "sketch_add_entity", "sketch": "$0",
         "entity": {"type": "circle"}},  # placeholder — ignored
    ]
    ids = sketch_ids_from_ops(ops)
    assert ids == ["s2", "s1"]
    assert sketch_ids_from_ops(ops) == ids  # stable


def test_summarize_sketch_nl_parity_fields():
    """Text-only models need named dims, constraint list, open/closed notes."""
    sk = _open_sketch()
    report = sketch_ascii_report(sk, pitch=1.0)
    s = report["summary"]
    assert "Dimensions:" in s
    assert "Constraints:" in s
    # open status from health
    assert "OPEN" in s or "open" in s.lower() or "dangling" in s.lower()
    # entity id callouts
    assert "e1" in s and "e2" in s
