"""Copper zone-fill tests — the copper-pour geometry of the ECAD PCB module.

House style: ``from __future__ import annotations``, plain ``main()`` + asserts, run as a module.
Fixtures are self-contained — a small board built inline from :func:`schema.new_board` with a
rectangular outline, a GND pour on F.Cu, a couple of GND pads and a couple of different-net (SIG)
pads/tracks inside the pour. Coordinates are mm.

Run:  python -m services.ecad.test_zonefill

We verify the fill by POINT SAMPLING the resulting ``filled_polys`` with an even-odd
point-in-polyset test. This is the convention the fill emits (a flat list of simple polygons, holes
folded in via zero-width keyhole bridges → see zonefill module docstring), so an even-odd test over
the whole list is exactly "is this (x,y) bare copper after fill?". Sampling is far more robust than
area arithmetic over self-touching keyholed polygons.

Covers:
  1. A different-net pad / track carves a copper-free clearance gap (near = not copper, far = copper).
  2. A same-net GND THT pad is thermally relieved: a gap ring is bare but spoke corridors are copper.
  3. Total filled copper area < zone-polygon area (clearance removed some) and > 0.
  4. ``fill_zones`` returns a copy and never mutates the input board.
  5. Determinism: two runs are byte-identical.
  6. SMD same-net pad floods solid (no thermal gap); a tiny island below min_width is dropped.
"""

from __future__ import annotations

import json
import math

import numpy as np

from services.ecad import schema, zonefill


# ── fixture builders ───────────────────────────────────────────────────────────────

def _add_net(board: dict, name: str, klass: str = "default") -> None:
    if not any(n.get("name") == name for n in board["nets"]):
        board["nets"].append({"name": name, "class": klass})


def _smd_pad(pid, pos, net, size=(2.0, 2.0), shape="circle", layer="F.Cu") -> dict:
    return {"id": pid, "number": "1", "pos": list(pos), "angle": 0.0, "shape": shape,
            "size": list(size), "type": "smd", "layers": [layer], "drill": None, "net": net}


def _tht_pad(pid, pos, net, size=(2.0, 2.0), drill=1.0) -> dict:
    return {"id": pid, "number": "1", "pos": list(pos), "angle": 0.0, "shape": "circle",
            "size": list(size), "type": "tht", "layers": ["F.Cu", "B.Cu"],
            "drill": {"diameter": drill, "shape": "round", "offset": [0, 0]}, "net": net}


def _fp(fid, pos, pads, side="top", attr="smd") -> dict:
    return {"id": fid, "fpid": "Test:FP", "ref": fid.upper(), "value": "", "pos": list(pos),
            "angle": 0.0, "side": side, "pads": pads, "graphics": [], "model_3d": None,
            "attr": attr, "linked_symbol": None}


def _track(tid, p1, p2, net, width=0.5, layer="F.Cu") -> dict:
    return {"id": tid, "net": net, "layer": layer, "width": width, "kind": "segment",
            "p1": list(p1), "p2": list(p2)}


def _rect_outline(board: dict, w: float, h: float, x0=0.0, y0=0.0) -> None:
    x1, y1 = x0 + w, y0 + h
    board["outline"] = {"shapes": [
        {"type": "line", "p1": [x0, y0], "p2": [x1, y0]},
        {"type": "line", "p1": [x1, y0], "p2": [x1, y1]},
        {"type": "line", "p1": [x1, y1], "p2": [x0, y1]},
        {"type": "line", "p1": [x0, y1], "p2": [x0, y0]},
    ], "closed": True}


def _zone(zid, net, polygon, clearance=0.3, min_width=0.25,
          thermal=(0.4, 0.5), layer="F.Cu") -> dict:
    return {"id": zid, "net": net, "layer": layer, "polygon": [list(p) for p in polygon],
            "clearance": clearance, "min_width": min_width, "fill": "solid",
            "thermal": {"gap": thermal[0], "spoke_width": thermal[1]}, "filled_polys": []}


def _base_board() -> dict:
    """30x20 board, GND pour over most of F.Cu, with GND + SIG pads/tracks inside."""
    b = schema.new_board("zonefill-test")
    _add_net(b, "GND")
    _add_net(b, "SIG")
    _rect_outline(b, 30.0, 20.0)
    b["footprints"] = [
        # different-net SMD pad in the middle -> clearance hole
        _fp("fp1", (15.0, 10.0), [_smd_pad("p1", (0.0, 0.0), "SIG", size=(2.0, 2.0))]),
        # same-net GND THT pad -> thermal relief
        _fp("fp2", (6.0, 6.0), [_tht_pad("p2", (0.0, 0.0), "GND", size=(2.0, 2.0), drill=1.0)],
            attr="tht"),
        # same-net GND SMD pad -> flooded solid
        _fp("fp3", (24.0, 6.0), [_smd_pad("p3", (0.0, 0.0), "GND", size=(2.0, 2.0))]),
    ]
    # a different-net SIG track -> clearance corridor
    b["tracks"] = [_track("t1", (10.0, 15.0), (20.0, 15.0), "SIG", width=0.5)]
    b["zones"] = [_zone("z1", "GND", [[2, 2], [28, 2], [28, 18], [2, 18]])]
    return b


# ── point sampling over the keyholed polygon list ───────────────────────────────────

def _is_copper(polys, px, py) -> bool:
    """Even-odd point membership over the flat polygon list = 'is (x,y) bare copper after fill?'."""
    xs = np.array([float(px)])
    ys = np.array([float(py)])
    acc = False
    for p in polys:
        acc ^= bool(zonefill._points_in_polygon(xs, ys, p)[0])
    return acc


def _poly_area(poly) -> float:
    s = 0.0
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def _grid_copper_area(polys, bbox, res=0.05) -> float:
    """Monte-carlo-free copper area: sample a fine grid, count even-odd-inside cells * cell area."""
    minx, miny, maxx, maxy = bbox
    nx = max(1, int((maxx - minx) / res))
    ny = max(1, int((maxy - miny) / res))
    cx = minx + (np.arange(nx) + 0.5) * res
    cy = miny + (np.arange(ny) + 0.5) * res
    gx, gy = np.meshgrid(cx, cy)
    acc = np.zeros(gx.shape, dtype=bool)
    for p in polys:
        acc ^= zonefill._points_in_polygon(gx, gy, p)
    return int(acc.sum()) * res * res


# ── Test 1: different-net pad + track carve clearance gaps ───────────────────────────

def test_clearance_gap():
    b = _base_board()
    out = zonefill.fill_zones(b)
    polys = out["zones"][0]["filled_polys"]
    assert polys, "expected at least one filled polygon"

    # SIG pad at (15,10), r=1.0, clearance 0.3 -> void disc of radius ~1.3.
    assert not _is_copper(polys, 15.0, 10.0), "different-net pad centre must be bare (clearance)"
    assert not _is_copper(polys, 15.0, 11.0), "1mm above pad centre still inside the void"
    # well clear of the pad -> copper.
    assert _is_copper(polys, 15.0, 13.0), "3mm from pad centre must be copper again"

    # SIG track from (10,15)-(20,15), width 0.5 (half 0.25), clearance 0.3 -> void within ~0.55 of
    # the centreline. A point right on the track is bare; a point 2mm below it is copper.
    assert not _is_copper(polys, 15.0, 15.0), "on the different-net track -> bare clearance"
    assert _is_copper(polys, 15.0, 12.5), "2.5mm from the track -> copper"
    print("  test_clearance_gap OK")


# ── Test 2: same-net THT pad thermal relief (gap ring + spokes) ──────────────────────

def test_thermal_relief():
    b = _base_board()
    out = zonefill.fill_zones(b)
    polys = out["zones"][0]["filled_polys"]

    # GND THT pad at (6,6): copper r=1.0, thermal gap 0.4 -> relief ring [1.0, 1.4]. Spoke half
    # width 0.25 along ±x/±y.
    # Diagonal in the ring (r=1.2, 45deg) is NOT in a spoke -> bare.
    d = 1.2 / math.sqrt(2.0)
    assert not _is_copper(polys, 6.0 + d, 6.0 + d), "thermal gap ring (diagonal) must be bare"
    # Along +x at r=1.2 -> inside a spoke corridor -> copper.
    assert _is_copper(polys, 6.0 + 1.2, 6.0), "thermal +x spoke must be copper"
    # Along +y at r=1.2 -> copper.
    assert _is_copper(polys, 6.0, 6.0 + 1.2), "thermal +y spoke must be copper"
    # Beyond the ring (r=2.0, diagonal) -> copper (pour resumes outside the relief).
    db = 2.0 / math.sqrt(2.0)
    assert _is_copper(polys, 6.0 + db, 6.0 + db), "outside the relief ring -> copper"
    print("  test_thermal_relief OK")


# ── Test 2b: same-net SMD pad floods solid (no thermal gap) ──────────────────────────

def test_smd_same_net_solid():
    b = _base_board()
    out = zonefill.fill_zones(b)
    polys = out["zones"][0]["filled_polys"]
    # GND SMD pad at (24,6): no thermal relief -> copper right up to and around it (no gap ring).
    # Sample a point just outside the pad copper (r=1.0) where a THT pad would have a bare ring.
    assert _is_copper(polys, 24.0 + 1.2, 6.0), "same-net SMD pad must be solidly flooded (no ring)"
    assert _is_copper(polys, 24.0 + 1.2 / math.sqrt(2), 6.0 + 1.2 / math.sqrt(2)), \
        "same-net SMD pad diagonal must be copper too (no relief ring)"
    print("  test_smd_same_net_solid OK")


# ── Test 3: filled area is between 0 and the zone polygon area ───────────────────────

def test_area_bounds():
    b = _base_board()
    zpoly = b["zones"][0]["polygon"]
    zarea = _poly_area(zpoly)
    out = zonefill.fill_zones(b)
    polys = out["zones"][0]["filled_polys"]

    bbox = (1.0, 1.0, 29.0, 19.0)
    farea = _grid_copper_area(polys, bbox, res=0.05)
    assert farea > 0.0, "fill produced no copper"
    assert farea < zarea, f"filled area {farea:.2f} should be < zone area {zarea:.2f} (clearance)"
    # sanity: clearance/thermal removed a modest fraction, not everything.
    assert farea > 0.5 * zarea, f"filled area {farea:.2f} suspiciously small vs {zarea:.2f}"
    print(f"  test_area_bounds OK  (zone={zarea:.2f}mm^2  filled={farea:.2f}mm^2  "
          f"removed={zarea - farea:.2f}mm^2)")


# ── Test 4: fill_zones is pure (returns a copy, no mutation) ─────────────────────────

def test_purity():
    b = _base_board()
    before = json.dumps(b, sort_keys=True)
    out = zonefill.fill_zones(b)
    after = json.dumps(b, sort_keys=True)
    assert before == after, "fill_zones mutated its input board"
    assert out is not b, "fill_zones must return a copy, not the same object"
    assert out["zones"][0]["filled_polys"], "the returned copy must carry the fill"
    assert b["zones"][0]["filled_polys"] == [], "the input zone's filled_polys must be untouched"
    print("  test_purity OK")


# ── Test 5: determinism ──────────────────────────────────────────────────────────────

def test_determinism():
    b = _base_board()
    a = json.dumps(zonefill.fill_zones(b)["zones"][0]["filled_polys"], sort_keys=True)
    c = json.dumps(zonefill.fill_zones(b)["zones"][0]["filled_polys"], sort_keys=True)
    assert a == c, "fill is not deterministic"
    print("  test_determinism OK")


# ── Test 6: tiny island below the min_width area threshold is dropped ─────────────────

def test_min_width_island():
    # The fill drops copper components whose area is below min_width**2 (a square thinner than
    # min_width can't hold a min_width-side square — the documented island filter). We carve the
    # pour into a big region plus a small detached island and assert the small one is removed.
    #
    # Pour spans x[1..19]. A vertical SIG track at x=3 (width 0.5, clearance 0.3) voids
    # x in [~2.45, ~3.55] for the full height, isolating a left strip x[1..2.45]. With a large
    # min_width=6.0 -> threshold 36 mm^2; the left strip area ~ 1.45*18 = 26 mm^2 < 36 -> dropped.
    b = schema.new_board("island")
    _add_net(b, "GND")
    _add_net(b, "SIG")
    _rect_outline(b, 20.0, 20.0)
    b["tracks"] = [_track("t1", (3.0, 1.0), (3.0, 19.0), "SIG", width=0.5)]
    z = _zone("z1", "GND", [[1, 1], [19, 1], [19, 19], [1, 19]], clearance=0.3, min_width=6.0,
              thermal=(0.4, 0.5))
    b["zones"] = [z]
    out = zonefill.fill_zones(b)
    polys = out["zones"][0]["filled_polys"]
    # the left strip near x=1.5 should NOT survive (island area below the threshold).
    assert not _is_copper(polys, 1.5, 10.0), "small island below min_width^2 area must be dropped"
    assert _is_copper(polys, 12.0, 10.0), "the main region must remain copper"

    # With a tiny min_width (0.25 -> threshold 0.06 mm^2) the same strip is KEPT.
    z2 = _zone("z2", "GND", [[1, 1], [19, 1], [19, 19], [1, 19]], clearance=0.3, min_width=0.25,
               thermal=(0.4, 0.5))
    b["zones"] = [z2]
    polys2 = zonefill.fill_zones(b)["zones"][0]["filled_polys"]
    assert _is_copper(polys2, 1.5, 10.0), "with a small min_width the strip survives"
    print("  test_min_width_island OK")


# ── Test 7: empty / degenerate zones, non-board guard ────────────────────────────────

def test_edge_cases():
    # zone with too-few points -> []
    b = schema.new_board("deg")
    _add_net(b, "GND")
    b["zones"] = [_zone("z1", "GND", [[0, 0], [1, 1]])]
    out = zonefill.fill_zones(b)
    assert out["zones"][0]["filled_polys"] == [], "degenerate zone -> empty fill"

    # non-board input returned as-is copy, no raise
    sch = {"schema": "aicad.sch/1", "symbols": []}
    assert zonefill.fill_zones(sch).get("schema") == "aicad.sch/1"

    # zone on a non-copper layer -> empty (defensive; validator would also reject, but fill is safe)
    b2 = schema.new_board("deg2")
    _add_net(b2, "GND")
    z = _zone("z1", "GND", [[0, 0], [5, 0], [5, 5], [0, 5]])
    z["layer"] = "F.SilkS"
    b2["zones"] = [z]
    assert zonefill.fill_zones(b2)["zones"][0]["filled_polys"] == [], "non-copper zone -> empty"
    print("  test_edge_cases OK")


# ── Test 8: filled_polys are well-formed simple polygons (G36-ready) ─────────────────

def test_wellformed_polys():
    b = _base_board()
    out = zonefill.fill_zones(b)
    polys = out["zones"][0]["filled_polys"]
    assert isinstance(polys, list)
    for p in polys:
        assert isinstance(p, list) and len(p) >= 3, "each filled poly needs >=3 vertices"
        for pt in p:
            assert isinstance(pt, list) and len(pt) == 2, "each vertex is [x,y]"
            assert all(isinstance(c, float) for c in pt), "coords are floats (JSON-friendly)"
    print(f"  test_wellformed_polys OK  ({len(polys)} polygon(s))")


def main() -> None:
    print("=" * 60)
    print("ECAD COPPER ZONE-FILL TESTS")
    print("=" * 60)
    test_clearance_gap()
    test_thermal_relief()
    test_smd_same_net_solid()
    test_area_bounds()
    test_purity()
    test_determinism()
    test_min_width_island()
    test_edge_cases()
    test_wellformed_polys()
    print("ALL ZONE-FILL TESTS PASSED")


if __name__ == "__main__":
    main()
