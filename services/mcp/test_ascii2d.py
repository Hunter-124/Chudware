"""Deterministic ASCII 2D tools — PCB / schematic / sketch / sim / edits.

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
    apply_ascii_edits,
    attach_board_readback,
    attach_schematic_readback,
    attach_sketch_readback,
    board_ascii_report,
    board_mutation_readback,
    cell_to_world,
    check_board,
    check_schematic,
    check_sketch_geometry,
    parse_highlight,
    plan_ascii_edits,
    register_ascii2d_tools,
    render_ascii_3d,
    render_board_layer_ascii,
    render_schematic_ascii,
    render_sim_ascii,
    schematic_ascii_report,
    segments_cross,
    sketch_ascii_report,
    sketch_ids_from_ops,
    sketch_mutation_readback,
    world_to_cell,
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




def _divider_schematic() -> dict:
    """Minimal voltage-divider-like schematic for ASCII + edit tests."""
    return {
        "schema": "aicad.sch/1",
        "name": "divider_ascii",
        "sheets": [{"id": "sh1", "name": "root", "parent": None,
                    "size": [297, 210], "ports": []}],
        "symbols": [
            {"id": "s1", "libid": "Device:R", "ref": "R1", "value": "10k",
             "pos": [20.0, 10.0], "angle": 0.0, "sheet": "sh1"},
            {"id": "s2", "libid": "Device:R", "ref": "R2", "value": "10k",
             "pos": [40.0, 10.0], "angle": 0.0, "sheet": "sh1"},
            {"id": "s3", "libid": "Simulation_SPICE:V_source", "ref": "V1",
             "value": "5", "pos": [0.0, 10.0], "angle": 0.0, "sheet": "sh1"},
        ],
        "wires": [
            {"id": "w1", "sheet": "sh1",
             "pts": [[5.0, 10.0], [15.0, 10.0]], "bus": False},
            {"id": "w2", "sheet": "sh1",
             "pts": [[25.0, 10.0], [35.0, 10.0]], "bus": False},
        ],
        "junctions": [],
        "labels": [
            {"id": "l1", "text": "MID", "scope": "local",
             "pos": [30.0, 10.0], "sheet": "sh1"},
        ],
        "power": [
            {"id": "pw1", "net": "GND", "pos": [45.0, 5.0], "sheet": "sh1"},
            {"id": "pw2", "net": "+5V", "pos": [0.0, 15.0], "sheet": "sh1"},
        ],
        "buses": [],
        "no_connect": [],
        "graphics": [],
        "sheet_symbols": [],
        "lib_cache": {},
        "annotations": {},
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
    assert rb["ascii_ruled"].startswith("[sketch_readback")
    assert "summary" in rb
    assert "Entities:" in rb["summary"] or "entit" in rb["summary"].lower()
    assert "Dimensions:" in rb["summary"]
    assert "Constraints:" in rb["summary"]
    # open profile + under-dim should surface
    codes = {f.get("code") for f in rb["findings"]}
    assert "open_profile" in codes or "OPEN" in rb["summary"]
    # NL parity: named entity / dim callouts
    assert "e1" in rb["summary"] or "line" in rb["summary"].lower()
    assert "distance" in rb["summary"].lower() or "c3" in rb["summary"]


def test_sketch_readback_ships_exactly_one_picture():
    """Live 2026-07-30: one add_features batch adding a 3-rectangle profile returned
    26k chars, and the readback shipped the SAME drawing three times — `ascii` (bare),
    `ascii_block` (header + bare) and `ascii_ruled` (rulers). Only the ruled variant
    carries the 0-based col/row numbers a model reads a cell address off, and cell
    addresses are the contract apply_ascii_edits(kind='sketch') resolves against, so
    that is the one that ships. The bare variants must NOT come back."""
    rb = sketch_mutation_readback(_open_sketch(), pitch=1.0, sketch_id="s1")
    pictures = [k for k in rb if k == "ascii" or k.startswith("ascii_")]
    assert pictures == ["ascii_ruled"], pictures
    ruled = rb["ascii_ruled"].split("\n")
    # header, then a col ruler, then rows prefixed with their 0-based row number.
    assert ruled[0].startswith("[sketch_readback id=s1 ")
    assert " 0 |" in rb["ascii_ruled"]


def test_sketch_mutation_readback_caps_and_determinism():
    sk = _open_sketch()
    a = sketch_mutation_readback(sk, pitch=0.1, max_h=40, max_w=120)
    b = sketch_mutation_readback(sk, pitch=0.1, max_h=40, max_w=120)
    assert a["ascii_ruled"] == b["ascii_ruled"]
    assert a["summary"] == b["summary"]
    assert a["findings"] == b["findings"]
    assert a["grid"] == b["grid"]
    assert a["height"] <= READBACK_MAX_H
    assert a["width"] <= READBACK_MAX_W
    # force tiny cap to exercise truncation path
    tiny = sketch_mutation_readback(sk, pitch=0.05, max_h=5, max_w=10)
    assert tiny["height"] <= 5
    assert tiny["width"] <= 10


def test_truncated_sketch_readback_index_never_claims_off_picture_cells():
    """Measured 2026-07-30 on a 30x200 mm profile: the readback shipped 40 rows and said
    `truncated: True`, while `grid.height` still said 41 and `index` still advertised
    entities at cell "0,40" — a row the model cannot see and world_to_cell can never
    return. grid/index describe the SHIPPED picture, and whatever fell off is named."""
    tall = [{"id": "e1", "type": "line", "p1": [0, 0], "p2": [30, 0]},
            {"id": "e2", "type": "line", "p1": [30, 0], "p2": [30, 200]},
            {"id": "e3", "type": "line", "p1": [30, 200], "p2": [0, 200]},
            {"id": "e4", "type": "line", "p1": [0, 200], "p2": [0, 0]}]
    rb = sketch_mutation_readback({"id": "s1", "entities": tall})
    assert rb["truncated"] is True
    assert rb["grid"]["width"] == rb["width"]
    assert rb["grid"]["height"] == rb["height"]
    for key in rb["index"]:
        col, row = (int(t) for t in key.split(","))
        assert 0 <= col < rb["width"] and 0 <= row < rb["height"], key
    # the entities that fell off the crop are NAMED, not silently dropped
    assert rb["clipped_entities"] == ["e1", "e2"], rb.get("clipped_entities")
    assert "CROPPED" in rb["hint"]


def test_sketch_readback_grid_cell_to_world_inverts_the_rasterizer():
    """The `index` maps a cell to the entities STARTING there, so cell_to_world applied
    to an index key must land exactly on that entity's p1. Live 2026-07-30 it did not:
    the raster put e1.p1=(10,-6) in cell (1,19) and the documented half-pitch formula
    answered (10.5,-6.5), while world_to_cell(10,-6) answered (0,18) — a full cell off.
    apply_ascii_edits resolves `to_cell` through cell_to_world."""
    ents = [{"id": "e1", "type": "line", "p1": [10, -6], "p2": [16, -6]},
            {"id": "e2", "type": "line", "p1": [16, -6], "p2": [16, 12]}]
    rb = sketch_mutation_readback({"id": "s1", "entities": ents}, pitch=1.0)
    g = rb["grid"]
    assert g["cell_to_world"] == (
        "x = bbox[0] + col * pitch; "
        "y = bbox[3] - row * pitch  # y_up, row0=top; cell = lattice point, not box centre")
    assert rb["index"], rb
    for key, hits in rb["index"].items():
        col, row = (int(t) for t in key.split(","))
        x, y = cell_to_world(col, row, g)
        for hit in hits:
            assert (x, y) == tuple(hit["p1"]), (key, x, y, hit)
            assert world_to_cell(hit["p1"][0], hit["p1"][1], g) == (col, row)


def test_board_mutation_readback_has_ascii_summary_net_delta():
    board = _crossing_board()
    rb = board_mutation_readback(board, pitch_mm=2.0, layer="F.Cu")
    assert rb["ok"] is True
    assert rb["kind"] == "board_readback"
    assert rb["ascii_ruled"].startswith("[board_readback")
    # ONE picture only: `ascii` (bare) and `ascii_block` (header+bare) were the same
    # drawing as `ascii_ruled` minus its col/row rulers, which are the cell addressing
    # apply_ascii_edits(kind='pcb') resolves against.
    assert "ascii" not in rb and "ascii_block" not in rb
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
    assert a["ascii_ruled"] == b["ascii_ruled"]
    assert a["summary"] == b["summary"]
    assert a["findings"] == b["findings"]
    assert a["net_delta"] == b["net_delta"]
    rows = a["ascii_ruled"].split("\n")
    assert len(rows) <= READBACK_MAX_H + 4  # header + col-ruler lines
    assert all(len(r) <= READBACK_MAX_W + 8 for r in rows)  # + row-number gutter
    tiny = board_mutation_readback(board, pitch_mm=0.5, max_h=6, max_w=12)
    assert tiny["height"] <= 6
    assert tiny["width"] <= 12


def test_attach_helpers_mutate_response():
    sk = _open_sketch()
    board = _crossing_board()
    resp_s: dict = {"ok": True, "doc_id": "d1"}
    attach_sketch_readback(resp_s, sk, sketch_id="s1", pitch=1.0)
    assert resp_s["ascii_readback"]["ok"] is True
    assert resp_s["ascii_readback"]["ascii_ruled"]
    # The picture and the summary live at ONE path — the helper no longer clones them
    # onto the response as ascii / ascii_block / sketch_summary.
    assert "ascii" not in resp_s and "ascii_block" not in resp_s
    assert "sketch_summary" not in resp_s
    # …but a response with no headline of its own still gets the summary as one.
    assert resp_s["summary"] == resp_s["ascii_readback"]["summary"]

    resp_b: dict = {"ok": True, "doc_id": "pcb1"}
    attach_board_readback(resp_b, board, pitch_mm=2.0, layer="F.Cu")
    # Same single-path contract as the sketch helper above.
    assert "ascii" not in resp_b and "ascii_block" not in resp_b
    assert "board_summary" not in resp_b and "net_delta" not in resp_b
    assert resp_b["ascii_readback"]["net_delta"]["tracks"] >= 1
    assert resp_b["ascii_readback"]["ok"] is True
    assert resp_b["summary"] == resp_b["ascii_readback"]["summary"]


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


# --------------------------------------------------------------------------- schematic / sim / edits


def test_schematic_ascii_deterministic_and_indexed():
    sch = _divider_schematic()
    a = schematic_ascii_report(sch, pitch_mm=2.54)
    b = schematic_ascii_report(sch, pitch_mm=2.54)
    assert a["ok"] and b["ok"]
    assert a["ascii"] == b["ascii"]
    assert a["summary"] == b["summary"]
    assert a["grid"] and a["grid"]["kind"] == "schematic"
    assert a["grid"]["y_flip"] is True
    assert a["index"], "expected non-empty cell index"
    # R1 should appear in the ascii or index
    refs = []
    for hits in a["index"].values():
        for h in hits:
            if h.get("kind") == "symbol":
                refs.append(h.get("ref"))
    assert "R1" in refs and "R2" in refs
    assert "R1" in a["ascii"] or "R1" in a["ascii_ruled"]
    assert "col" not in a["ascii_ruled"] or "|" in a["ascii_ruled"]


def test_schematic_findings_duplicate_ref():
    sch = _divider_schematic()
    sch["symbols"].append({
        "id": "s9", "libid": "Device:R", "ref": "R1", "value": "1k",
        "pos": [60.0, 10.0], "angle": 0.0, "sheet": "sh1",
    })
    findings = check_schematic(sch)
    assert any(f.get("code") == "duplicate_ref" for f in findings), findings


def test_cell_world_roundtrip():
    sch = _divider_schematic()
    rep = schematic_ascii_report(sch, pitch_mm=2.54)
    g = rep["grid"]
    # pick a mid cell
    col, row = g["width"] // 2, g["height"] // 2
    x, y = cell_to_world(col, row, g)
    c2, r2 = world_to_cell(x, y, g)
    assert (c2, r2) == (col, row)


def test_plan_swap_and_move_schematic():
    sch = _divider_schematic()
    rep = schematic_ascii_report(sch, pitch_mm=2.54)
    plan = plan_ascii_edits(
        "schematic",
        [
            {"op": "swap", "a": "R1", "b": "R2"},
            {"op": "set_value", "ref": "R1", "value": "4.7k"},
            {"op": "place_label", "text": "VCC", "cell": [1, 1], "scope": "global"},
        ],
        grid=rep["grid"], index=rep["index"], doc=sch,
    )
    assert plan["ops"], plan
    kinds = [o["op"] for o in plan["ops"]]
    assert kinds.count("sch_edit_symbol") >= 3  # 2 swap + 1 set_value
    assert "sch_add_label" in kinds
    assert plan["ok"] or not plan["errors"]


def test_plan_pcb_section_replace_and_move():
    board = _crossing_board()
    rep = board_ascii_report(board, pitch_mm=2.0)
    assert rep["grid"] and rep["index"]
    # move R1 to a far cell
    # find a free-ish cell
    g = rep["grid"]
    to_cell = [g["width"] - 2, 1]
    plan = plan_ascii_edits(
        "pcb",
        [
            {"op": "move", "ref": "R1", "to_cell": to_cell},
            {"op": "swap", "a": "U1", "b": "U2"},
            {"op": "replace_section",
             "cell_min": [0, 0], "cell_max": [3, 3],
             "clear": ["track"],
             "place": []},
        ],
        grid=g, index=rep["index"], doc=board, layer="F.Cu",
    )
    kinds = [o["op"] for o in plan["ops"]]
    assert "pcb_edit_footprint" in kinds
    # section clear may or may not hit tracks in that corner
    assert plan["n_ops"] >= 3, plan


def test_plan_sketch_add_line():
    sk = _open_sketch()
    rep = sketch_ascii_report(sk, pitch=1.0)
    assert rep.get("grid"), rep
    plan = plan_ascii_edits(
        "sketch",
        [{"op": "sketch_add_line", "from_cell": [1, 1], "to_cell": [5, 1]}],
        grid=rep["grid"], index=rep.get("index") or {}, doc=sk, sketch_id="s1",
    )
    assert plan["ops"] and plan["ops"][0]["op"] == "sketch_add_entity"
    assert plan["ops"][0]["entity"]["type"] == "line"


def test_apply_ascii_edits_dry_run():
    sch = _divider_schematic()
    rep = schematic_ascii_report(sch, pitch_mm=2.54)
    out = apply_ascii_edits(
        "schematic",
        [{"op": "swap", "a": "R1", "b": "R2"}],
        grid=rep["grid"], index=rep["index"], doc=sch, dry_run=True,
    )
    assert out.get("dry_run") is True
    assert out.get("applied") is False
    assert any(o["op"] == "sch_edit_symbol" for o in out["ops"])


def test_sim_ascii_stable():
    circuit = {
        "devices": [
            {"kind": "R", "name": "R1", "nodes": ["MID", "GND"],
             "params": {"r": 10000}},
            {"kind": "V", "name": "V1", "nodes": ["+5V", "GND"],
             "params": {"dc": 5}},
        ],
        "nodes": ["MID", "GND", "+5V"],
    }
    r1 = render_sim_ascii(circuit=circuit, results={"V(MID)": 2.5})
    r2 = render_sim_ascii(circuit=circuit, results={"V(MID)": 2.5})
    assert r1["ascii"] == r2["ascii"]
    assert "R1" in r1["ascii"] and "V(MID)" in r1["ascii"]
    assert r1["grid"] is None


def test_mcp_registration_includes_schematic_and_apply():
    mcp = _StubMCP()
    register_ascii2d_tools(mcp)
    for name in ("render_board_ascii", "render_sketch_ascii", "render_ascii",
                 "render_schematic_ascii", "render_sim_ascii", "apply_ascii_edits"):
        assert name in mcp.tools, name
    sch = _divider_schematic()
    out = json.loads(mcp.tools["render_schematic_ascii"](
        sch_json=json.dumps(sch), pitch_mm=2.54))
    assert out["ok"], out
    assert out.get("grid")
    # dry-run apply via tool
    out2 = json.loads(mcp.tools["apply_ascii_edits"](
        kind="schematic",
        edits_json=json.dumps([{"op": "swap", "a": "R1", "b": "R2"}]),
        grid_json=json.dumps(out["grid"]),
        index_json=json.dumps(out["index"]),
        dry_run=True,
    ))
    # dry_run path may still need doc offline — plan-only works with grid/index
    # When dry_run and no live doc, apply_ascii_edits still plans from grid/index
    assert "ops" in out2, out2
    sim = json.loads(mcp.tools["render_sim_ascii"](
        circuit_json=json.dumps({
            "devices": [{"kind": "R", "name": "R1", "nodes": ["a", "0"],
                         "params": {"r": 1e3}}],
            "nodes": ["a", "0"],
        })))
    assert sim["ok"] and "R1" in sim["ascii"]


def test_board_report_exposes_grid_and_ruled():
    board = _crossing_board()
    r = board_ascii_report(board, pitch_mm=2.0)
    assert r.get("grid") and r.get("index")
    assert r.get("ascii_ruled")
    # cell index should mention a footprint or track
    flat = [h for hits in r["index"].values() for h in hits]
    kinds = {h.get("kind") for h in flat}
    assert "footprint" in kinds or "track" in kinds


def test_attach_schematic_readback():
    sch = _divider_schematic()
    resp = {"ok": True}
    attach_schematic_readback(resp, sch)
    rb = resp["ascii_readback"]
    assert rb["ok"] is True
    assert rb["ascii_ruled"].startswith("[schematic_readback")
    # ONE picture, one summary, one grid/index — nothing cloned to the top level.
    for k in ("ascii", "ascii_block", "schematic_summary", "grid", "index",
              "readback_hint"):
        assert k not in resp, f"{k} duplicated outside ascii_readback"
    assert resp["summary"] == rb["summary"]



# ------------------------------------------------- highlight / legend / scale


def _hl_sketch() -> dict:
    """Closed rectangle + two circles, so a highlight has something to pick out."""
    return {
        "entities": [
            {"id": "e1", "type": "line", "p1": [0.0, 0.0], "p2": [0.0, 40.0]},
            {"id": "e2", "type": "line", "p1": [0.0, 40.0], "p2": [60.0, 40.0]},
            {"id": "e3", "type": "line", "p1": [60.0, 40.0], "p2": [60.0, 0.0]},
            {"id": "e4", "type": "line", "p1": [60.0, 0.0], "p2": [0.0, 0.0]},
            {"id": "e5", "type": "circle", "center": [15.0, 20.0], "radius": 6.0},
            {"id": "e6", "type": "circle", "center": [45.0, 20.0], "radius": 6.0},
        ],
        "constraints": [],
    }


def test_parse_highlight_accepts_every_shape():
    assert parse_highlight(None) == []
    assert parse_highlight("") == []
    assert parse_highlight("e3") == ["e3"]
    assert parse_highlight("e3, e4;e5") == ["e3", "e4", "e5"]
    assert parse_highlight(["e3", {"net": "GND"}]) == ["e3", "net:GND"]
    assert parse_highlight('["e3","net:GND"]') == ["e3", "net:GND"]
    assert parse_highlight('{"ref":"R1"}') == ["ref:R1"]
    # duplicates collapse, order preserved
    assert parse_highlight("e3,e3,e1") == ["e3", "e1"]
    # box tokens keep their own commas
    assert parse_highlight("box:0,0,10,10") == ["box:0,0,10,10"]


def test_every_render_states_scale_and_origin():
    """Acceptance: a model must be able to convert a glyph position to coords."""
    reports = {
        "sketch": sketch_ascii_report(_hl_sketch(), pitch=2.0),
        "pcb": board_ascii_report(_crossing_board(), pitch_mm=2.0),
        "schematic": schematic_ascii_report(_divider_schematic()),
        "silhouette3d": render_ascii_3d(bbox={"min": [0, 0, 0], "max": [10, 4, 6]},
                                        view="front", pitch=1.0),
    }
    for kind, rep in reports.items():
        lines = "\n".join(rep["scale"]["lines"])
        assert "SCALE:" in lines, kind
        assert "ORIGIN:" in lines, kind
        assert "CONVERT:" in lines, kind
        assert lines in rep["ascii_annotated"], kind
        assert lines in rep["summary"], kind
    sim = render_sim_ascii(circuit={"devices": [
        {"kind": "R", "name": "R1", "nodes": ["a", "b"], "params": {"r": "1k"}}],
        "nodes": ["a", "b"]})
    sim_scale = "\n".join(sim["scale"]["lines"])
    assert "SCALE: TOPOLOGICAL" in sim_scale
    assert "ORIGIN: none" in sim_scale
    assert sim_scale in sim["ascii_annotated"]


def test_every_render_carries_a_legend_and_callouts():
    for rep in (sketch_ascii_report(_hl_sketch(), pitch=2.0),
                board_ascii_report(_crossing_board(), pitch_mm=2.0),
                schematic_ascii_report(_divider_schematic()),
                render_sim_ascii(spice_text="R1 a b 1k\n.end"),
                render_ascii_3d(bbox={"min": [0, 0, 0], "max": [10, 4, 6]})):
        assert rep["legend"] and rep["legend"][0].startswith("LEGEND ")
        assert "\n".join(rep["legend"]) in rep["ascii_annotated"]
        assert "CALLOUTS (marker -> id ->" in rep["ascii_annotated"]
        assert "CALLOUTS (marker -> id ->" in rep["summary"]


def test_default_output_shape_unchanged_without_highlight():
    """The raw drawing must be byte-identical whether or not the extras exist."""
    sk = _hl_sketch()
    plain = sketch_ascii_report(sk, pitch=2.0)
    again = sketch_ascii_report(sk, pitch=2.0)
    assert plain["ascii"] == again["ascii"]
    for key in ("ok", "kind", "ascii", "ascii_ruled", "summary", "findings",
                "pitch", "width", "height", "bbox", "grid", "index", "hint"):
        assert key in plain, key
    assert plain["highlight"]["requested"] == []
    assert plain["callouts"] == [] or all(
        c["marker"] == "-" for c in plain["callouts"])


def test_sketch_highlight_marks_only_the_target():
    sk = _hl_sketch()
    plain = sketch_ascii_report(sk, pitch=2.0)
    marked = sketch_ascii_report(sk, pitch=2.0, highlight="e5")
    assert marked["ascii"] != plain["ascii"], "highlight must change the drawing"
    assert "A" in marked["ascii"] and "A" not in plain["ascii"]
    assert ">A<" in marked["ascii"], "anchor must be bracketed"
    # e6 (the other circle) keeps its ordinary glyphs
    assert marked["ascii"].count("A") < plain["ascii"].count("+") + \
        plain["ascii"].count("-") + plain["ascii"].count("|")
    ids = [c["id"] for c in marked["callouts"]]
    assert ids == ["e5"], marked["callouts"]
    c = marked["callouts"][0]
    assert c["marker"] == "A" and "r=6.0" in c["desc"]
    assert c["anchor_cell"] and c["anchor_world"]
    assert "A -> e5 ->" in marked["ascii_annotated"]


def test_sketch_highlight_by_type_and_list_gets_distinct_markers():
    marked = sketch_ascii_report(_hl_sketch(), pitch=2.0, highlight=["e1", "circle"])
    markers = {c["marker"] for c in marked["callouts"]}
    assert markers == {"A", "B"}, marked["callouts"]
    assert {c["id"] for c in marked["callouts"]} == {"e1", "e5", "e6"}
    assert "A" in marked["ascii"] and "B" in marked["ascii"]


def test_unmatched_highlight_is_reported_not_silently_dropped():
    marked = sketch_ascii_report(_hl_sketch(), pitch=2.0, highlight="e1,nope9")
    assert marked["highlight"]["unmatched"] == ["nope9"]
    assert "REQUESTED BUT NOT FOUND" in marked["ascii_annotated"]


def test_board_highlight_marks_footprint_and_net():
    board = _crossing_board()
    plain = board_ascii_report(board, layer="F.Cu", pitch_mm=2.0)
    marked = board_ascii_report(board, layer="F.Cu", pitch_mm=2.0,
                                highlight="R1,net:A")
    assert marked["ascii"] != plain["ascii"]
    ids = {c["id"] for c in marked["callouts"] if c["marker"] != "-"}
    assert "R1" in ids and "tA" in ids and "v1" in ids, marked["callouts"]
    by_marker = {c["marker"] for c in marked["callouts"] if c["marker"] != "-"}
    assert by_marker == {"A", "B"}
    # the marked track is drawn with its letter, not with a path glyph
    assert "B" in marked["ascii"]


def test_schematic_highlight_marks_symbol_label_and_power():
    sch = _divider_schematic()
    plain = schematic_ascii_report(sch)
    marked = schematic_ascii_report(sch, highlight=["R2", "MID", "GND"])
    assert marked["ascii"] != plain["ascii"]
    got = {c["marker"]: c["id"] for c in marked["callouts"] if c["marker"] != "-"}
    assert got == {"A": "R2", "B": "l1", "C": "pw1"}, marked["callouts"]
    for mk in ("A", "B", "C"):
        assert mk in marked["ascii"]


def test_sim_highlight_marks_rows_via_gutter():
    circuit = {"devices": [
        {"kind": "R", "name": "R1", "nodes": ["in", "mid"], "params": {"r": "10k"}},
        {"kind": "R", "name": "R2", "nodes": ["mid", "0"], "params": {"r": "10k"}}],
        "nodes": ["in", "mid", "0"]}
    results = {"probes": {"V(in)": 5.0, "V(mid)": 2.5}}
    plain = render_sim_ascii(circuit=circuit, results=results)
    marked = render_sim_ascii(circuit=circuit, results=results,
                              highlight="R2,node:mid")
    assert ">A<" not in plain["ascii"] and ">B<" not in plain["ascii"]
    assert ">A< R R2 nodes=[mid,0] r=10k" in marked["ascii"]
    assert ">B< R R1 nodes=[in,mid] r=10k" in marked["ascii"]
    assert ">B< V(mid) = 2.5" in marked["ascii"]
    # every payload row keeps the 4-char gutter so columns still line up
    for line in plain["ascii"].split("\n"):
        if not line.startswith("["):
            assert line.startswith("    "), line
    ids = {c["id"] for c in marked["callouts"]}
    assert {"R1", "R2", "V(mid)"} <= ids, marked["callouts"]


def test_silhouette_highlight_by_polyline_and_box():
    bb = {"min": [0, 0, 0], "max": [10, 4, 6]}
    plain = render_ascii_3d(bbox=bb, view="front", pitch=1.0)
    # no highlight → the p-handles are still enumerated so a model can address them
    assert any(c["id"] == "p0" for c in plain["callouts"])
    marked = render_ascii_3d(bbox=bb, view="front", pitch=1.0, highlight="p2")
    assert marked["ascii"] != plain["ascii"]
    assert "A" in marked["ascii"]
    assert [c["id"] for c in marked["callouts"] if c["marker"] == "A"] == ["p2"]
    boxed = render_ascii_3d(bbox=bb, view="front", pitch=1.0,
                            highlight="box:-1,-1,1,1")
    assert [c["id"] for c in boxed["callouts"] if c["marker"] == "A"], boxed["callouts"]


def test_registered_tools_expose_highlight_and_round_trip():
    import inspect

    mcp = _StubMCP()
    register_ascii2d_tools(mcp)
    for name in ("render_sketch_ascii", "render_board_ascii",
                 "render_schematic_ascii", "render_sim_ascii", "render_ascii"):
        assert "highlight" in inspect.signature(mcp.tools[name]).parameters, name

    out = json.loads(mcp.tools["render_sketch_ascii"](
        sketch_json=json.dumps(_hl_sketch()), pitch=2.0, highlight="e5"))
    assert out["ok"] and "A -> e5 ->" in out["ascii_annotated"]

    out = json.loads(mcp.tools["render_board_ascii"](
        board_json=json.dumps(_crossing_board()), pitch_mm=2.0, highlight="R1"))
    assert out["ok"] and "A -> R1 ->" in out["ascii_annotated"]

    out = json.loads(mcp.tools["render_schematic_ascii"](
        sch_json=json.dumps(_divider_schematic()), highlight="R2"))
    assert out["ok"] and "A -> R2 ->" in out["ascii_annotated"]

    out = json.loads(mcp.tools["render_sim_ascii"](
        circuit_json=json.dumps({"devices": [
            {"kind": "R", "name": "R1", "nodes": ["a", "b"], "params": {"r": "1k"}}],
            "nodes": ["a", "b"]}), highlight="R1"))
    assert out["ok"] and ">A< R R1" in out["ascii"]

    out = json.loads(mcp.tools["render_ascii"](
        bbox_json=json.dumps({"min": [0, 0, 0], "max": [10, 4, 6]}),
        view="front", pitch=1.0, highlight="p1"))
    assert out["ok"] and "A -> p1 ->" in out["ascii_annotated"]
