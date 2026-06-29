"""Autorouter v2 tests -- rip-up-and-reroute + differential pairs.

Companion to ``test_autoroute.py`` (which must stay green and is NOT modified). House style:
``from __future__ import annotations``, plain ``main()`` + asserts, run as a module. Fixtures are
self-contained small boards from :func:`schema.new_board`. Coordinates are mm.

Run:  & "<venv>/python.exe" -m engine.ecad.test_autoroute2

Covers (per the v2 task spec):
  1. Rip-up: a board where the greedy single pass leaves a net unrouted purely due to ordering
     (a short net steals the only exit of a pocketed net), but rip-up-and-reroute routes BOTH ->
     assert routed count increases vs ``autoroute(board, {"ripup_passes": 0})`` and the final
     ratsnest is smaller, with NO new clearance DRC violations.
  2. Diff pair: CLK_P/CLK_N -> coupled route at the net-class ``diff_gap`` with the two tracks
     parallel + adjacent along the coupled span; after length-match the skew <= tolerance; no
     clearance violations to OTHER nets.
  3. Diff-pair length match: a pair whose two members route to different lengths gets the shorter
     one meandered until skew <= ``length_tol``.
  4. Determinism: same board + opts -> byte-identical output.
  5. Regression: ``autoroute`` with no new opts behaves exactly as the legacy single greedy pass
     (``ripup_passes`` 0 == default on a board with nothing to rip up), and a board with no
     diff-pair-named nets is untouched by auto-detect.
"""

from __future__ import annotations

import json
import math

from engine.ecad import schema
from engine.ecad.autoroute import autoroute, route_diff_pair
from engine.ecad.drc import ratsnest, run_drc, seg_seg_dist


# -- fixture builders -----------------------------------------------------------------

def _board(name: str = "rt2") -> dict:
    return schema.new_board(name)


def _add_net(board: dict, name: str, klass: str = "default") -> None:
    if not any(n.get("name") == name for n in board["nets"]):
        board["nets"].append({"name": name, "class": klass})


def _pad(pid: str, pos, net, size=(0.4, 0.4), shape="circle", layer="F.Cu") -> dict:
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


def _one_layer(board: dict) -> None:
    """Make the board single copper layer (F.Cu) so there is no via escape -- this lets a rip-up
    scenario be constructed deterministically (a planar blocking is a true block)."""
    board["stackup"]["copper_layers"] = 1
    board["stackup"]["layers"] = [{"id": "F.Cu", "type": "copper", "thickness": 0.035}]


def _wall(tid: str, p1, p2, width=1.0) -> dict:
    return {"id": tid, "net": "WALL", "layer": "F.Cu", "width": width, "kind": "segment",
            "p1": list(p1), "p2": list(p2)}


def _apply(board: dict, res: dict) -> dict:
    out = json.loads(json.dumps(board))
    out["tracks"] = list(out.get("tracks", [])) + res["tracks"]
    out["vias"] = list(out.get("vias", [])) + res["vias"]
    return out


def _clearance_viols(board: dict) -> list[dict]:
    return [v for v in run_drc(board) if v["rule"] == "clearance"]


# -- rip-up fixture ------------------------------------------------------------------

def _ripup_board() -> dict:
    """Single-layer 40x30 board with a vertical WALL at x=20 (full height) pierced by ONE
    single-track gap at y=15, and a short 'tube' (two stub walls) on the right that narrows the
    gap's exit to one lane.

      * Net Y connects a LEFT pad to a RIGHT pad through the gap -> the gap is Y's ONLY route.
      * Net X has two pads on the RIGHT, straddling the gap mouth vertically -> the GREEDY router
        (shortest airwire first) routes X straight down across the gap mouth, sealing Y in.

    Greedy therefore routes the short net X and FAILS the long net Y. Rip-up should tear X out, route
    Y through the freed gap, and re-route X around the mouth -> BOTH routed.
    """
    b = _board("ripup")
    _one_layer(b)
    for n in ("X", "Y", "WALL"):
        _add_net(b, n)
    _rect_outline(b, 40.0, 30.0)
    gh = 1.1  # half-gap
    tracks: list[dict] = []
    tid = 1
    for (a, c) in [(0.0, 15.0 - gh), (15.0 + gh, 30.0)]:
        tracks.append(_wall(f"t{tid}", (20.0, a), (20.0, c)))
        tid += 1
    # right-side stub walls forming a one-lane tube at the gap exit
    tracks.append(_wall(f"t{tid}", (20.0, 15.0 + gh), (26.0, 15.0 + gh)))
    tid += 1
    tracks.append(_wall(f"t{tid}", (20.0, 15.0 - gh), (26.0, 15.0 - gh)))
    tid += 1
    b["tracks"] = tracks
    b["footprints"] = [
        _fp("yl", (6.0, 15.0), [_pad("yl", (0.0, 0.0), "Y")]),
        _fp("yr", (34.0, 15.0), [_pad("yr", (0.0, 0.0), "Y")]),
        _fp("xa", (23.0, 11.0), [_pad("xa", (0.0, 0.0), "X")]),
        _fp("xb", (23.0, 19.0), [_pad("xb", (0.0, 0.0), "X")]),
    ]
    return b


def test_ripup_routes_more_nets():
    b = _ripup_board()
    before = ratsnest(b)
    assert len(before) == 2, f"fixture should have 2 airwires (X, Y), got {before}"

    greedy = autoroute(b, {"ripup_passes": 0})
    ripped = autoroute(b, {"ripup_passes": 3})

    g_routed = set(greedy["report"]["routed"])
    r_routed = set(ripped["report"]["routed"])
    # greedy leaves a net unrouted purely due to ordering
    assert len(g_routed) < 2, f"greedy should fail a net on this fixture: {greedy['report']}"
    # rip-up routes strictly more nets
    assert len(r_routed) > len(g_routed), (
        f"rip-up must route more nets: greedy={sorted(g_routed)} ripup={sorted(r_routed)}")
    assert r_routed == {"X", "Y"}, f"rip-up should route BOTH nets: {ripped['report']}"

    # final ratsnest strictly smaller after rip-up
    g_board = _apply(b, greedy)
    r_board = _apply(b, ripped)
    assert len(ratsnest(r_board)) < len(ratsnest(g_board)), (
        f"rip-up ratsnest must shrink: greedy_after={ripped['report']['airwires_after']}")
    assert ripped["report"]["airwires_after"] == 0
    assert ripped["report"]["ripup_iterations"] >= 1, "rip-up should have performed a recovery"

    # no NEW clearance violations from the rip-up route (board started clean of routed copper)
    assert _clearance_viols(r_board) == [], _clearance_viols(r_board)

    print(f"  test_ripup_routes_more_nets OK  (greedy routed={sorted(g_routed)} -> "
          f"ripup routed={sorted(r_routed)}; airwires {greedy['report']['airwires_after']} -> "
          f"{ripped['report']['airwires_after']}; iters={ripped['report']['ripup_iterations']})")


def test_ripup_via_autoroute_default():
    """The default opts (no ripup_passes given -> default 2) also rescue the net."""
    b = _ripup_board()
    res = autoroute(b)  # defaults
    assert set(res["report"]["routed"]) == {"X", "Y"}, res["report"]
    assert res["report"]["airwires_after"] == 0
    assert _clearance_viols(_apply(b, res)) == []
    print(f"  test_ripup_via_autoroute_default OK  (routed={sorted(res['report']['routed'])}, "
          f"iters={res['report']['ripup_iterations']})")


# -- differential-pair fixtures -------------------------------------------------------

def _diff_pair_board(diff_gap=0.25, track_width=0.2, pad_sep=0.75, length=28.0) -> dict:
    """40x20 single-layer board, two footprints carrying CLK_P/CLK_N pads at ``pad_sep`` apart so the
    pair runs as a straight parallel couple across the board. Pad positions are grid-aligned (pitch
    0.25). Pads are small (0.2mm) and separated enough that the opposite-net pads + the coupled tracks
    all clear the global 0.2mm clearance (so the routed pair is DRC-clean while the intra-pair gap is
    the intentional ``diff_gap``)."""
    b = _board("diffpair")
    _one_layer(b)
    _add_net(b, "CLK_P")
    _add_net(b, "CLK_N")
    b["net_classes"]["default"]["diff_gap"] = diff_gap
    b["net_classes"]["default"]["track_width"] = track_width
    _rect_outline(b, 40.0, 20.0)
    h = pad_sep / 2.0
    x0 = 6.0
    x1 = x0 + length
    sz = (0.2, 0.2)
    b["footprints"] = [
        _fp("f1", (x0, 10.0), [_pad("p1", (0.0, h), "CLK_P", sz), _pad("p2", (0.0, -h), "CLK_N", sz)]),
        _fp("f2", (x1, 10.0), [_pad("p3", (0.0, h), "CLK_P", sz), _pad("p4", (0.0, -h), "CLK_N", sz)]),
    ]
    return b


def _longest_track(tracks: list[dict]) -> dict:
    return max(tracks, key=lambda t: math.hypot(t["p1"][0] - t["p2"][0], t["p1"][1] - t["p2"][1]))


def _coupled_span_edge_gap(tracks: list[dict], net_p: str, net_n: str) -> float:
    """Edge-to-edge spacing between the two members' COUPLED span (their longest/parallel run) -- the
    region where the diff gap is held. (Near the pads the two members fan in/out, so the gap there is
    not the coupled spacing; the spec asks for the gap *along the coupled span*.)"""
    a = _longest_track([t for t in tracks if t["net"] == net_p])
    c = _longest_track([t for t in tracks if t["net"] == net_n])
    d = seg_seg_dist(tuple(a["p1"]), tuple(a["p2"]), tuple(c["p1"]), tuple(c["p2"]))
    return d - a["width"] / 2.0 - c["width"] / 2.0


def test_diff_pair_coupled_and_matched():
    b = _diff_pair_board(diff_gap=0.25, track_width=0.2, pad_sep=0.5)
    target_gap = 0.25
    clearance = float(b["rules"]["clearance"])

    res = route_diff_pair(b, "CLK_P", "CLK_N")
    rep = res["report"]
    assert rep["routed"], f"diff pair should route: {rep}"
    assert rep["nets"] == ["CLK_P", "CLK_N"]

    # both members connected (no airwire left for either)
    routed = _apply(b, res)
    after = {a["net"] for a in ratsnest(routed)}
    assert "CLK_P" not in after and "CLK_N" not in after, f"both members must connect: {after}"

    # the two tracks run parallel + adjacent along the coupled span
    assert rep["coupled_pct"] >= 90.0, f"pair should be highly coupled: {rep['coupled_pct']}%"

    # intra-pair spacing ~ the target gap along the coupled span (>= clearance so it is itself
    # DRC-acceptable). Grid snapping (pitch 0.25) means it lands at/near the target.
    gap = _coupled_span_edge_gap(res["tracks"], "CLK_P", "CLK_N")
    assert gap >= clearance - 1e-6, f"coupled-span edge gap {gap} should be >= clearance {clearance}"
    assert abs(gap - target_gap) <= 0.06, (
        f"coupled-span edge gap {gap} should be near the target {target_gap} (grid-snapped)")

    # after length match the skew is within tolerance
    assert rep["skew_mm"] <= 0.2 + 1e-9, f"skew should be matched within tol: {rep['skew_mm']}"

    # no clearance violations at all (intra-pair gap is intentional + >= clearance here)
    assert _clearance_viols(routed) == [], _clearance_viols(routed)

    print(f"  test_diff_pair_coupled_and_matched OK  (coupled={rep['coupled_pct']}%, "
          f"intra-pair edge gap={round(gap, 4)}mm ~ {target_gap}, skew={rep['skew_mm']}mm, "
          f"len_p={rep['length_p']}, len_n={rep['length_n']})")


def test_diff_pair_via_autoroute_autodetect():
    """autoroute auto-detects CLK_P/CLK_N by naming, routes them coupled first, and reports per-pair
    stats. Routing stays clearance-clean against the (here, none) other nets."""
    b = _diff_pair_board(diff_gap=0.25, track_width=0.2, pad_sep=0.5)
    res = autoroute(b)
    pairs = res["report"]["diff_pairs"]
    assert len(pairs) == 1, f"should auto-detect one pair: {pairs}"
    p = pairs[0]
    assert p["nets"] == ["CLK_P", "CLK_N"] and p["routed"], p
    assert set(res["report"]["routed"]) == {"CLK_P", "CLK_N"}, res["report"]["routed"]
    assert res["report"]["airwires_after"] == 0
    assert _clearance_viols(_apply(b, res)) == []
    print(f"  test_diff_pair_via_autoroute_autodetect OK  (pair stats={p})")


def test_diff_pair_length_match_meander():
    """Force a length mismatch: drop the CLK_N target pad away from the couple so the complement
    routes longer than the primary. The shorter member (primary) must be meandered to match skew."""
    b = _board("dpskew")
    _one_layer(b)
    _add_net(b, "CLK_P")
    _add_net(b, "CLK_N")
    b["net_classes"]["default"]["diff_gap"] = 0.25
    b["net_classes"]["default"]["track_width"] = 0.2
    _rect_outline(b, 40.0, 24.0)
    sz = (0.2, 0.2)  # small pads so the opposite-net source pads stay clearance-clean
    b["footprints"] = [
        _fp("f1", (6.0, 12.0), [_pad("p1", (0.0, 0.375), "CLK_P", sz),
                                _pad("p2", (0.0, -0.375), "CLK_N", sz)]),
        _fp("f2", (34.0, 12.375), [_pad("p3", (0.0, 0.0), "CLK_P", sz)]),
        _fp("f3", (34.0, 4.0), [_pad("p4", (0.0, 0.0), "CLK_N", sz)]),  # N target dropped -> N longer
    ]

    # without matching the skew is real
    raw = route_diff_pair(b, "CLK_P", "CLK_N", {"length_tol": 999.0})
    assert raw["report"]["skew_mm"] > 0.3, f"fixture should produce a real skew: {raw['report']}"

    # with matching the meander closes the skew
    matched = route_diff_pair(b, "CLK_P", "CLK_N", {"length_tol": 0.2})
    rep = matched["report"]
    assert rep["routed"], rep
    assert rep["skew_mm"] <= 0.2 + 1e-9, f"skew should be matched within tol: {rep['skew_mm']}"
    # the meander only ADDS copper, so both members still connect + stay clearance-clean
    routed = _apply(b, matched)
    after = {a["net"] for a in ratsnest(routed)}
    assert "CLK_P" not in after and "CLK_N" not in after, f"both connect after meander: {after}"
    assert _clearance_viols(routed) == [], _clearance_viols(routed)
    print(f"  test_diff_pair_length_match_meander OK  (skew before={raw['report']['skew_mm']}mm "
          f"-> after match={rep['skew_mm']}mm; len_p={rep['length_p']} len_n={rep['length_n']})")


# -- determinism + regression ---------------------------------------------------------

def test_determinism_v2():
    for b, opts in [
        (_ripup_board(), {"ripup_passes": 3}),
        (_diff_pair_board(), {}),
    ]:
        r1 = autoroute(b, opts)
        r2 = autoroute(b, opts)
        assert json.dumps(r1, sort_keys=True) == json.dumps(r2, sort_keys=True), (
            "v2 routing not deterministic")
    # route_diff_pair determinism too
    b = _diff_pair_board()
    d1 = route_diff_pair(b, "CLK_P", "CLK_N")
    d2 = route_diff_pair(b, "CLK_P", "CLK_N")
    assert json.dumps(d1, sort_keys=True) == json.dumps(d2, sort_keys=True), (
        "route_diff_pair not deterministic")
    print("  test_determinism_v2 OK")


def test_regression_no_new_opts():
    """With no new opts AND nothing to rip up / no diff-pair names, the v2 router produces the same
    routed/failed result as the explicit legacy single pass (ripup_passes=0, no diff pairs)."""
    # a plain 2-net board (no diff naming, fully routable) -> rip-up never triggers
    b = _board("plain")
    _add_net(b, "SIG")
    _add_net(b, "VCC")
    _rect_outline(b, 40.0, 30.0)
    b["footprints"] = [
        _fp("fp1", (8.0, 15.0), [_pad("p1", (0.0, 2.0), "SIG"), _pad("p2", (0.0, -2.0), "VCC")]),
        _fp("fp2", (20.0, 8.0), [_pad("p3", (0.0, 0.0), "VCC")]),
        _fp("fp3", (32.0, 18.0), [_pad("p4", (0.0, 0.0), "SIG")]),
    ]
    legacy = autoroute(b, {"ripup_passes": 0})
    default = autoroute(b)
    # identical tracks/vias on a board with no rip-up needed + no diff pairs
    assert json.dumps(legacy["tracks"], sort_keys=True) == json.dumps(default["tracks"],
                                                                       sort_keys=True), \
        "default opts changed the route on a board with nothing to rip up"
    assert json.dumps(legacy["vias"], sort_keys=True) == json.dumps(default["vias"],
                                                                     sort_keys=True)
    assert default["report"]["diff_pairs"] == [], "no diff pairs should be detected here"
    assert default["report"]["ripup_iterations"] == 0, "nothing to rip up -> 0 iterations"
    assert set(default["report"]["routed"]) == {"SIG", "VCC"}
    print("  test_regression_no_new_opts OK")


def test_no_mutation_v2():
    b = _ripup_board()
    snap = json.dumps(b, sort_keys=True)
    autoroute(b, {"ripup_passes": 3})
    assert json.dumps(b, sort_keys=True) == snap, "autoroute mutated the board (rip-up)"
    b2 = _diff_pair_board()
    snap2 = json.dumps(b2, sort_keys=True)
    route_diff_pair(b2, "CLK_P", "CLK_N")
    assert json.dumps(b2, sort_keys=True) == snap2, "route_diff_pair mutated the board"
    print("  test_no_mutation_v2 OK")


def main() -> None:
    print("=" * 60)
    print("AI-ECAD AUTOROUTER v2 TESTS (rip-up + diff pairs)")
    print("=" * 60)
    test_ripup_routes_more_nets()
    test_ripup_via_autoroute_default()
    test_diff_pair_coupled_and_matched()
    test_diff_pair_via_autoroute_autodetect()
    test_diff_pair_length_match_meander()
    test_determinism_v2()
    test_regression_no_new_opts()
    test_no_mutation_v2()
    print("ALL AUTOROUTER v2 TESTS PASSED")


if __name__ == "__main__":
    main()
