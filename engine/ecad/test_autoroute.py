"""Autorouter tests — basic grid (Lee/A*) router for AI-ECAD boards.

House style: ``from __future__ import annotations``, plain ``main()`` + asserts, run as a module.
Fixtures are self-contained — small boards built inline from :func:`schema.new_board`, with a
rectangular outline, footprints/pads on a couple of nets, and no tracks (so the ratsnest is
non-empty). Coordinates are mm.

Run:  & "<venv>/python.exe" -m engine.ecad.test_autoroute

Covers (per the task spec):
  1. autoroute connects the simple nets: after applying its tracks, ``ratsnest`` has FEWER airwires
     (0 for a trivially-routable 2-pad net).
  2. The routed tracks introduce NO new ``clearance`` DRC violations vs the pre-route board.
  3. A genuinely unroutable net (a pad walled off by other-net copper) is in ``report["failed"]``,
     not crashed.
  4. Determinism: two runs produce byte-identical tracks/vias.
  5. autoroute does not mutate the input board.
"""

from __future__ import annotations

import json

from engine.ecad import schema
from engine.ecad.autoroute import autoroute, route_board
from engine.ecad.drc import ratsnest, run_drc


# ── fixture builders (mirror test_drc.py) ────────────────────────────────────────────

def _board(name: str = "rt") -> dict:
    return schema.new_board(name)


def _add_net(board: dict, name: str, klass: str = "default") -> None:
    if not any(n.get("name") == name for n in board["nets"]):
        board["nets"].append({"name": name, "class": klass})


def _smd_pad(pid: str, pos, net, size=(0.6, 0.6), shape="circle", layer="F.Cu") -> dict:
    return {"id": pid, "number": "1", "pos": list(pos), "angle": 0.0, "shape": shape,
            "size": list(size), "type": "smd", "layers": [layer], "drill": None, "net": net}


def _fp(fid: str, pos, pads, side="top") -> dict:
    return {"id": fid, "fpid": "Test:FP", "ref": fid.upper(), "value": "", "pos": list(pos),
            "angle": 0.0, "side": side, "pads": pads, "graphics": [], "model_3d": None,
            "attr": "smd", "linked_symbol": None}


def _rect_outline(board: dict, w: float, h: float, x0: float = 0.0, y0: float = 0.0) -> None:
    x1, y1 = x0 + w, y0 + h
    board["outline"] = {
        "shapes": [
            {"type": "line", "p1": [x0, y0], "p2": [x1, y0]},
            {"type": "line", "p1": [x1, y0], "p2": [x1, y1]},
            {"type": "line", "p1": [x1, y1], "p2": [x0, y1]},
            {"type": "line", "p1": [x0, y1], "p2": [x0, y0]},
        ],
        "closed": True,
    }


def _clearance_viols(board: dict) -> list[dict]:
    return [v for v in run_drc(board) if v["rule"] == "clearance"]


def _apply(board: dict, res: dict) -> dict:
    """Apply autoroute output to a deep copy of the board (caller's job in production)."""
    out = json.loads(json.dumps(board))
    out["tracks"] = list(out.get("tracks", [])) + res["tracks"]
    out["vias"] = list(out.get("vias", [])) + res["vias"]
    return out


# ── a small, realistic two-net board ─────────────────────────────────────────────────

def _two_net_board() -> dict:
    """40x30 board, three footprints. Net SIG connects fp1.p1 <-> fp3.p1 (the long span);
    net VCC connects fp1.p2 <-> fp2.p1. No tracks yet."""
    b = _board()
    _add_net(b, "SIG"); _add_net(b, "VCC")
    _rect_outline(b, 40.0, 30.0)
    b["footprints"] = [
        _fp("fp1", (8.0, 15.0), [
            _smd_pad("p1", (0.0, 2.0), "SIG"),
            _smd_pad("p2", (0.0, -2.0), "VCC"),
        ]),
        _fp("fp2", (20.0, 8.0), [_smd_pad("p3", (0.0, 0.0), "VCC")]),
        _fp("fp3", (32.0, 18.0), [_smd_pad("p4", (0.0, 0.0), "SIG")]),
    ]
    return b


# ── Test 1: connects the simple nets, ratsnest shrinks ───────────────────────────────

def test_routes_reduce_ratsnest():
    b = _two_net_board()
    before = ratsnest(b)
    assert len(before) == 2, f"fixture should have 2 airwires (SIG, VCC), got {before}"

    res = autoroute(b)
    assert res["tracks"], "router produced no tracks for a trivially-routable board"

    routed = _apply(b, res)
    assert schema.validate_board(routed) == [], schema.validate_board(routed)
    after = ratsnest(routed)
    assert len(after) < len(before), \
        f"routing must reduce the ratsnest: before={len(before)} after={len(after)}"
    # both simple 2-pad nets are trivially routable on a wide-open board -> fully routed
    assert len(after) == 0, f"both nets should route to completion on an open board: {after}"
    assert set(res["report"]["routed"]) == {"SIG", "VCC"}, res["report"]["routed"]
    assert res["report"]["failed"] == [], res["report"]["failed"]
    assert res["report"]["airwires_before"] == 2
    assert res["report"]["airwires_after"] == 0
    assert res["report"]["length_mm"] > 0
    print(f"  test_routes_reduce_ratsnest OK  (airwires {len(before)} -> {len(after)}, "
          f"len={res['report']['length_mm']}mm, vias={res['report']['vias']})")


# ── Test 2: no new clearance DRC violations ──────────────────────────────────────────

def test_no_new_clearance_violations():
    b = _two_net_board()
    pre = _clearance_viols(b)  # should be none on this clean fixture
    res = autoroute(b)
    routed = _apply(b, res)
    post = _clearance_viols(routed)
    assert len(post) <= len(pre), \
        f"routing introduced clearance violations: pre={len(pre)} post={len(post)}\n{post}"
    assert post == [], f"clean board must stay clearance-clean after routing: {post}"
    print(f"  test_no_new_clearance_violations OK  (pre={len(pre)}, post={len(post)})")


# ── Test 2b: tight two-net board, still clearance-clean ──────────────────────────────

def test_tight_board_clean():
    """Pads packed closer together with both nets — exercise the obstacle halo so tracks weave
    without violating clearance."""
    b = _board()
    _add_net(b, "A"); _add_net(b, "B")
    _rect_outline(b, 20.0, 16.0)
    b["footprints"] = [
        _fp("fp1", (4.0, 8.0), [_smd_pad("p1", (0.0, 1.5), "A"), _smd_pad("p2", (0.0, -1.5), "B")]),
        _fp("fp2", (16.0, 8.0), [_smd_pad("p3", (0.0, 1.5), "A"), _smd_pad("p4", (0.0, -1.5), "B")]),
    ]
    res = autoroute(b)
    routed = _apply(b, res)
    assert _clearance_viols(routed) == [], f"tight board must stay clean: {_clearance_viols(routed)}"
    # at least one net should route
    assert res["report"]["routed"], f"expected some routed nets, report={res['report']}"
    print(f"  test_tight_board_clean OK  (routed={res['report']['routed']}, "
          f"failed={res['report']['failed']})")


# ── Test 3: unroutable net is reported, not crashed ──────────────────────────────────

def test_unroutable_reported():
    """Wall a target pad off with a closed box of other-net copper on BOTH layers so no grid path
    (on either layer, through any via) can reach it. The net must appear in report['failed'] and the
    call must not raise."""
    b = _board()
    _add_net(b, "NET"); _add_net(b, "WALL")
    _rect_outline(b, 30.0, 30.0)
    # source out in the open; target pad boxed in the centre
    src = _fp("fsrc", (3.0, 15.0), [_smd_pad("src", (0.0, 0.0), "NET")])
    target = _fp("ftgt", (15.0, 15.0), [_smd_pad("tgt", (0.0, 0.0), "NET", size=(0.4, 0.4))])
    b["footprints"] = [src, target]

    # A closed square WALL of fat tracks (width 1.0, so halo ~0.7mm) on F.Cu AND B.Cu around the
    # target. With the box ~2mm to a side and clearance 0.2, the halos seal every grid cell on both
    # layers -> the pocket is unreachable, even via a layer change inside it.
    x0, y0, x1, y1 = 13.0, 13.0, 17.0, 17.0
    box = [((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)),
           ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))]
    tracks = []
    tid = 1
    for layer in ("F.Cu", "B.Cu"):
        for (p1, p2) in box:
            tracks.append({"id": f"t{tid}", "net": "WALL", "layer": layer, "width": 1.0,
                           "kind": "segment", "p1": list(p1), "p2": list(p2)})
            tid += 1
    b["tracks"] = tracks

    before = ratsnest(b)
    assert any(a["net"] == "NET" for a in before), "fixture must have a NET airwire to attempt"

    res = autoroute(b)  # must not raise
    assert "NET" in res["report"]["failed"], \
        f"walled-off net must be reported failed: report={res['report']}"
    assert "NET" not in res["report"]["routed"]
    # whatever the router did produce must still be clearance-clean (no forced bad route)
    routed = _apply(b, res)
    assert _clearance_viols(routed) == [], _clearance_viols(routed)
    print(f"  test_unroutable_reported OK  (failed={res['report']['failed']})")


# ── Test 4: determinism ──────────────────────────────────────────────────────────────

def test_determinism():
    b = _two_net_board()
    r1 = autoroute(b)
    r2 = autoroute(b)
    assert json.dumps(r1["tracks"], sort_keys=True) == json.dumps(r2["tracks"], sort_keys=True), \
        "tracks differ between runs"
    assert json.dumps(r1["vias"], sort_keys=True) == json.dumps(r2["vias"], sort_keys=True), \
        "vias differ between runs"
    assert json.dumps(r1["report"], sort_keys=True) == json.dumps(r2["report"], sort_keys=True), \
        "report differs between runs"
    print("  test_determinism OK")


# ── Test 5: purity — input board untouched ──────────────────────────────────────────

def test_no_mutation():
    b = _two_net_board()
    snap = json.dumps(b, sort_keys=True)
    res = autoroute(b)
    assert json.dumps(b, sort_keys=True) == snap, "autoroute mutated the input board"
    assert res["tracks"], "sanity: it did route something"

    # route_board returns a NEW board with copper added; the original is still untouched
    rb = route_board(b)
    assert json.dumps(b, sort_keys=True) == snap, "route_board mutated the input board"
    assert len(rb["tracks"]) == len(b.get("tracks", [])) + len(res["tracks"])
    assert len(rb["vias"]) == len(b.get("vias", [])) + len(res["vias"])
    # the returned board validates and has a smaller ratsnest
    assert schema.validate_board(rb) == [], schema.validate_board(rb)
    assert len(ratsnest(rb)) < len(ratsnest(b))
    print("  test_no_mutation OK")


# ── Test 6: ids never collide with existing track/via ids ────────────────────────────

def test_id_allocation():
    b = _two_net_board()
    # seed an existing track + via so the counter must start past them
    b["tracks"] = [{"id": "t7", "net": "SIG", "layer": "F.Cu", "width": 0.25,
                    "kind": "segment", "p1": [0.0, 0.0], "p2": [0.0, 1.0]}]
    b["vias"] = [{"id": "v3", "net": "SIG", "at": [0.0, 0.0], "drill": 0.4, "diameter": 0.8,
                  "layers": ["F.Cu", "B.Cu"], "type": "through"}]
    res = autoroute(b)
    new_t = {t["id"] for t in res["tracks"]}
    new_v = {v["id"] for v in res["vias"]}
    assert "t7" not in new_t, "new track id collided with an existing one"
    assert "v3" not in new_v, "new via id collided with an existing one"
    # all new track ids are t8+
    for tid in new_t:
        assert int(tid[1:]) >= 8, f"track id {tid} not allocated past t7"
    print(f"  test_id_allocation OK  (new tracks={sorted(new_t)})")


# ── Test 7: empty / non-board inputs never crash ─────────────────────────────────────

def test_edge_inputs():
    # non-board input -> empty result, no crash
    res = autoroute({"schema": "aicad.sch/1"})
    assert res["tracks"] == [] and res["vias"] == []
    assert res["report"]["routed"] == [] and res["report"]["failed"] == []

    # a board with no nets to route (single isolated pad) -> nothing to do
    b = _board()
    _add_net(b, "X")
    b["footprints"] = [_fp("fp1", (5.0, 5.0), [_smd_pad("p1", (0.0, 0.0), "X")])]
    res2 = autoroute(b)
    assert res2["tracks"] == [], "single-pad net has nothing to route"
    assert res2["report"]["airwires_before"] == 0
    print("  test_edge_inputs OK")


def main() -> None:
    print("=" * 60)
    print("AI-ECAD AUTOROUTER TESTS")
    print("=" * 60)
    test_routes_reduce_ratsnest()
    test_no_new_clearance_violations()
    test_tight_board_clean()
    test_unroutable_reported()
    test_determinism()
    test_no_mutation()
    test_id_allocation()
    test_edge_inputs()
    print("ALL AUTOROUTER TESTS PASSED")


if __name__ == "__main__":
    main()
