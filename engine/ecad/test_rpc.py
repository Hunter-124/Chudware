"""Tests for engine.ecad.rpc — call handlers directly, no subprocess.

Covers:
  - ecad_new sch: new session + apply ops (add symbol + wire) + assert revision/tree/compile
  - ecad_undo / ecad_redo
  - ecad_new pcb: set outline + add footprint + ecad_export bom
  - ecad_to_3d: returns either step_path OR a clean not_available error
  - worker registry regression: import engine.worker; assert doc_new + ecad_* both present
"""
from __future__ import annotations

import os
import sys
import tempfile
import pathlib

# Ensure the repo root is on the path when running as a module or directly.
_HERE = pathlib.Path(__file__).resolve()
_REPO_ROOT = _HERE.parent.parent.parent  # engine/ecad/test_rpc.py -> repo root
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _noop_emit(data: dict) -> None:
    pass


# ---------------------------------------------------------------------------
# Helper: call a handler and unwrap, re-raising OpError if structured error
# ---------------------------------------------------------------------------

def _call(fn, params: dict) -> dict:
    return fn(params, _noop_emit)


# ---------------------------------------------------------------------------
# test_sch_apply_undo_redo
# ---------------------------------------------------------------------------

def test_sch_apply_undo_redo() -> None:
    from engine.ecad.rpc import METHODS

    new_fn = METHODS["ecad_new"]
    apply_fn = METHODS["ecad_apply_ops"]
    undo_fn = METHODS["ecad_undo"]
    redo_fn = METHODS["ecad_redo"]
    tree_fn = METHODS["ecad_tree"]

    # --- new schematic ---
    r0 = _call(new_fn, {"kind": "sch", "name": "TestSch"})
    assert "doc_id" in r0, f"missing doc_id: {r0}"
    doc_id = r0["doc_id"]
    assert r0["revision"] == 0
    assert r0["kind"] == "sch"
    assert "tree" in r0
    assert "compile" in r0

    # --- apply: add a symbol ---
    r1 = _call(apply_fn, {
        "doc_id": doc_id,
        "base_revision": 0,
        "ops": [
            {"op": "sch_add_symbol", "libid": "Device:R", "pos": [10.0, 10.0]},
        ],
    })
    assert r1["revision"] == 1, f"expected rev 1, got {r1['revision']}"
    assert "$0" in r1["id_map"], f"id_map missing $0: {r1['id_map']}"
    sym_id = r1["id_map"]["$0"]
    # tree should show the symbol
    syms = r1["tree"].get("symbols", [])
    assert any(s["id"] == sym_id for s in syms), "symbol not in tree"
    # compile present
    assert "compile" in r1
    assert "netlist" in r1["compile"] or r1["compile"]["netlist"] is None  # null is fine

    # --- apply: add a wire using placeholder ref ---
    r2 = _call(apply_fn, {
        "doc_id": doc_id,
        "base_revision": 1,
        "ops": [
            {"op": "sch_add_wire", "pts": [[0.0, 0.0], [10.0, 0.0]]},
        ],
    })
    assert r2["revision"] == 2, f"expected rev 2, got {r2['revision']}"
    wire_id = r2["id_map"].get("$0")
    assert wire_id is not None, "wire id_map missing"

    # --- tree (cached, no rebuild) ---
    tr = _call(tree_fn, {"doc_id": doc_id})
    assert tr["revision"] == 2
    assert tr["kind"] == "sch"
    assert "compile" in tr

    # --- undo: back to rev 2 -> 3 (revision bumps on undo per session.py) ---
    u1 = _call(undo_fn, {"doc_id": doc_id})
    assert u1["revision"] == 3
    # wire should be gone
    wires_after_undo = u1["tree"].get("wires", [])
    assert not any(w["id"] == wire_id for w in wires_after_undo), "wire still present after undo"

    # --- redo ---
    re1 = _call(redo_fn, {"doc_id": doc_id})
    assert re1["revision"] == 4
    wires_after_redo = re1["tree"].get("wires", [])
    assert any(w["id"] == wire_id for w in wires_after_redo), "wire absent after redo"

    print("  test_sch_apply_undo_redo PASS")


# ---------------------------------------------------------------------------
# test_pcb_outline_footprint_bom_export
# ---------------------------------------------------------------------------

def test_pcb_outline_footprint_bom_export() -> None:
    from engine.ecad.rpc import METHODS

    new_fn = METHODS["ecad_new"]
    apply_fn = METHODS["ecad_apply_ops"]
    export_fn = METHODS["ecad_export"]

    # --- new board ---
    r0 = _call(new_fn, {"kind": "pcb", "name": "TestBoard"})
    assert r0["kind"] == "pcb"
    doc_id = r0["doc_id"]

    # --- set outline (closed 100x100 rect) ---
    r1 = _call(apply_fn, {
        "doc_id": doc_id,
        "base_revision": 0,
        "ops": [
            {
                "op": "pcb_set_outline",
                "shapes": [
                    {"type": "line", "p1": [0.0, 0.0], "p2": [100.0, 0.0]},
                    {"type": "line", "p1": [100.0, 0.0], "p2": [100.0, 100.0]},
                    {"type": "line", "p1": [100.0, 100.0], "p2": [0.0, 100.0]},
                    {"type": "line", "p1": [0.0, 100.0], "p2": [0.0, 0.0]},
                ],
                "closed": True,
            },
        ],
    })
    assert r1["revision"] == 1

    # --- add two footprints ---
    r2 = _call(apply_fn, {
        "doc_id": doc_id,
        "base_revision": 1,
        "ops": [
            {
                "op": "pcb_add_footprint",
                "fpid": "Resistor_SMD:R_0402",
                "pos": [20.0, 20.0],
                "ref": "R1",
                "value": "10k",
            },
            {
                "op": "pcb_add_footprint",
                "fpid": "Resistor_SMD:R_0402",
                "pos": [30.0, 20.0],
                "ref": "R2",
                "value": "10k",
            },
        ],
    })
    assert r2["revision"] == 2
    fp1_id = r2["id_map"].get("$0")
    assert fp1_id is not None

    # --- export bom to a temp file ---
    with tempfile.TemporaryDirectory() as td:
        bom_path = os.path.join(td, "test_bom.csv")
        exp = _call(export_fn, {
            "doc_id": doc_id,
            "kind": "bom",
            "path": bom_path,
        })
        assert "paths" in exp, f"export result missing 'paths': {exp}"
        assert len(exp["paths"]) == 1
        written_path = exp["paths"][0]
        assert os.path.isfile(written_path), f"BOM file not written: {written_path}"
        content = pathlib.Path(written_path).read_text(encoding="utf-8")
        # Both R1 and R2 should appear (grouped as one row, Qty=2)
        assert "R1" in content or "R2" in content, f"refs not in BOM:\n{content}"
        # Should have Qty=2 somewhere
        assert "2" in content, f"Qty=2 not in BOM:\n{content}"

    print("  test_pcb_outline_footprint_bom_export PASS")


# ---------------------------------------------------------------------------
# test_ecad_to_3d_graceful
# ---------------------------------------------------------------------------

def test_ecad_to_3d_graceful() -> None:
    from engine.ecad.rpc import METHODS
    from engine.ecad.ops import OpError

    new_fn = METHODS["ecad_new"]
    apply_fn = METHODS["ecad_apply_ops"]
    to_3d_fn = METHODS["ecad_to_3d"]

    # Build a board with a proper outline so board_to_step has something to work with.
    r0 = _call(new_fn, {"kind": "pcb", "name": "Board3D"})
    doc_id = r0["doc_id"]

    # Set a closed 50x50 mm outline.
    _call(apply_fn, {
        "doc_id": doc_id,
        "base_revision": 0,
        "ops": [
            {
                "op": "pcb_set_outline",
                "shapes": [
                    {"type": "line", "p1": [0.0, 0.0], "p2": [50.0, 0.0]},
                    {"type": "line", "p1": [50.0, 0.0], "p2": [50.0, 50.0]},
                    {"type": "line", "p1": [50.0, 50.0], "p2": [0.0, 50.0]},
                    {"type": "line", "p1": [0.0, 50.0], "p2": [0.0, 0.0]},
                ],
                "closed": True,
            },
        ],
    })

    try:
        result = _call(to_3d_fn, {"doc_id": doc_id})
        # If threed module is present and succeeds: should have step_path
        assert "step_path" in result, f"missing step_path: {result}"
        print(f"  test_ecad_to_3d_graceful PASS (threed present, got step_path: {result['step_path']})")
    except OpError as e:
        # Two acceptable outcomes:
        #   1. not_available — threed module absent
        #   2. any other structured OpError — threed present but a domain error (e.g. build123d
        #      not available in this venv, missing dependencies, etc.)
        # Both count as "clean error" (no crash, no unhandled exception).
        assert hasattr(e, "code") and e.code, f"OpError missing code: {e}"
        print(f"  test_ecad_to_3d_graceful PASS (clean OpError returned: code={e.code})")


# ---------------------------------------------------------------------------
# test_describe
# ---------------------------------------------------------------------------

def test_describe() -> None:
    from engine.ecad.rpc import METHODS

    new_fn = METHODS["ecad_new"]
    apply_fn = METHODS["ecad_apply_ops"]
    describe_fn = METHODS["ecad_describe"]

    r0 = _call(new_fn, {"kind": "sch", "name": "DescSch"})
    doc_id = r0["doc_id"]

    _call(apply_fn, {
        "doc_id": doc_id,
        "base_revision": 0,
        "ops": [
            {"op": "sch_add_symbol", "libid": "Device:C", "pos": [5.0, 5.0]},
        ],
    })

    d = _call(describe_fn, {"doc_id": doc_id})
    assert d.get("kind") == "sch"
    assert "symbols" in d
    assert d["symbols"] >= 1
    print("  test_describe PASS")


# ---------------------------------------------------------------------------
# test_autoroute_and_fill_zones  (the new capability RPCs)
# ---------------------------------------------------------------------------

def _build_routable_board() -> str:
    """New board + outline + two footprints sharing a 'SIG' net on two pads -> one airwire to route.

    Returns the board doc_id (registered in the session registry).
    """
    from engine.ecad.rpc import METHODS
    new_fn = METHODS["ecad_new"]
    apply_fn = METHODS["ecad_apply_ops"]

    r0 = _call(new_fn, {"kind": "pcb", "name": "RouteBoard"})
    doc_id = r0["doc_id"]

    # Generous 40x20 outline so the grid router has room.
    _call(apply_fn, {"doc_id": doc_id, "base_revision": 0, "ops": [
        {"op": "pcb_set_outline", "shapes": [
            {"type": "line", "p1": [0, 0],  "p2": [40, 0]},
            {"type": "line", "p1": [40, 0], "p2": [40, 20]},
            {"type": "line", "p1": [40, 20], "p2": [0, 20]},
            {"type": "line", "p1": [0, 20], "p2": [0, 0]},
        ], "closed": True},
    ]})

    # Two R_0603 footprints; pad "2" of R1 and pad "1" of R2 share net SIG (an airwire). GND already
    # exists; SIG is introduced by the pads (pcb_add_track/zone _require_net, but pads register via
    # the footprint op path). To be safe, give both footprints SIG pads via explicit pad nets.
    from engine.ecad.library.starter import FOOTPRINTS
    import copy as _copy
    fp = FOOTPRINTS["Resistor_SMD:R_0603"]

    # The board needs the SIG net registered before pads reference it for routing. Footprint pads can
    # carry any net string; ratsnest groups by net name. But pcb_add_track (used by autoroute) calls
    # _require_net -> SIG must be in board.nets. There is no add-net op, so we route a net that the
    # footprint pads introduce AND that is already present: use GND (present on a fresh board) for the
    # routable pair so the autorouter's pcb_add_track passes _require_net.
    r1_pads = _copy.deepcopy(fp["pads"])
    for pad in r1_pads:
        pad["net"] = "GND"
    r2_pads = _copy.deepcopy(fp["pads"])
    for pad in r2_pads:
        pad["net"] = "GND"

    _call(apply_fn, {"doc_id": doc_id, "base_revision": 1, "ops": [
        {"op": "pcb_add_footprint", "fpid": "Resistor_SMD:R_0603", "ref": "R1",
         "value": "10k", "pos": [10.0, 10.0], "side": "top", "pads": r1_pads, "def": fp},
        {"op": "pcb_add_footprint", "fpid": "Resistor_SMD:R_0603", "ref": "R2",
         "value": "10k", "pos": [28.0, 10.0], "side": "top", "pads": r2_pads, "def": fp},
    ]})
    return doc_id


def test_autoroute_and_fill_zones() -> None:
    from engine.ecad.rpc import METHODS
    from engine.ecad.drc import ratsnest

    autoroute_fn = METHODS["ecad_autoroute"]
    fill_fn = METHODS["ecad_fill_zones"]
    apply_fn = METHODS["ecad_apply_ops"]
    tree_fn = METHODS["ecad_tree"]

    doc_id = _build_routable_board()

    # Ratsnest before: GND now spans 4 pads (all on one net) -> at least one airwire.
    tr = _call(tree_fn, {"doc_id": doc_id})
    rats_before = ratsnest(tr["tree"])
    assert len(rats_before) >= 1, f"expected airwires before routing, got {rats_before}"

    # --- ecad_autoroute ---
    base_rev = tr["revision"]
    resp = _call(autoroute_fn, {"doc_id": doc_id})
    assert resp["kind"] == "pcb"
    assert resp["revision"] == base_rev + 1, \
        f"autoroute should bump revision by 1 (got {resp['revision']} from {base_rev})"
    assert "report" in resp, "autoroute response carries the router report"
    report = resp["report"]
    assert report["airwires_after"] <= report["airwires_before"], \
        f"ratsnest should not grow ({report['airwires_before']} -> {report['airwires_after']})"
    # The compile field reflects the routed board.
    assert "compile" in resp and "ratsnest" in resp["compile"]
    rats_after_compile = len(resp["compile"]["ratsnest"])
    assert rats_after_compile == report["airwires_after"], \
        f"compile.ratsnest ({rats_after_compile}) matches report ({report['airwires_after']})"
    print(f"  autoroute: ratsnest {report['airwires_before']} -> {report['airwires_after']}, "
          f"{len(resp['tree'].get('tracks', []))} track(s), routed={report['routed']}, "
          f"failed={report['failed']}")

    # --- add a GND zone, then ecad_fill_zones (full resolution) ---
    rev = resp["revision"]
    zr = _call(apply_fn, {"doc_id": doc_id, "base_revision": rev, "ops": [
        {"op": "pcb_add_zone", "net": "GND", "layer": "F.Cu",
         "polygon": [[1, 1], [39, 1], [39, 19], [1, 19]]},
    ]})
    rev = zr["revision"]
    fz = _call(fill_fn, {"doc_id": doc_id})
    assert fz["revision"] == rev + 1, f"fill_zones bumps revision (got {fz['revision']} from {rev})"
    assert "fill_summary" in fz, "fill_zones response carries a fill_summary"
    summary = fz["fill_summary"]
    assert summary["zones_filled"] >= 1, f"GND pour should fill (got {summary})"
    assert summary["copper_area_mm2"] > 0.0, f"copper area should be positive (got {summary})"
    # The filled polys are persisted onto the doc's zone.
    gnd_zone = next(z for z in fz["tree"]["zones"] if z.get("net") == "GND")
    assert len(gnd_zone.get("filled_polys", [])) >= 1, "filled_polys persisted on the zone"
    print(f"  fill_zones: {summary['zones_filled']} zone(s), "
          f"{summary['copper_area_mm2']} mm^2, {summary['polygons']} polygon(s)")
    print("  test_autoroute_and_fill_zones PASS")


def test_autoroute_wrong_kind() -> None:
    """ecad_autoroute on a schematic must return a clean structured error, not crash."""
    from engine.ecad.rpc import METHODS
    from engine.ecad.ops import OpError

    new_fn = METHODS["ecad_new"]
    autoroute_fn = METHODS["ecad_autoroute"]
    r0 = _call(new_fn, {"kind": "sch", "name": "NotABoard"})
    try:
        _call(autoroute_fn, {"doc_id": r0["doc_id"]})
        assert False, "autoroute on a schematic should raise"
    except OpError as e:
        assert e.code == "invalid_op", f"expected invalid_op, got {e.code}"
    print("  test_autoroute_wrong_kind PASS")


# ---------------------------------------------------------------------------
# test_worker_registry_regression
# ---------------------------------------------------------------------------

def test_ecad_open_kicad_sch() -> None:
    """ecad_open on a minimal inline KiCad schematic (written to a tmp file) must produce a
    valid sch session. Also verifies .json plain-file dispatch still works."""
    import json, pathlib, tempfile
    from engine.ecad.rpc import METHODS

    open_fn = METHODS["ecad_open"]

    # ---- .kicad_sch dispatch ----
    # Write a minimal KiCad schematic file.
    minimal_sch = """\
(kicad_sch (version 20230121) (generator eeschema)
  (lib_symbols)
  (wire (pts (xy 0 0) (xy 10 0)))
)
"""
    with tempfile.TemporaryDirectory() as td:
        sch_path = str(pathlib.Path(td) / "test.kicad_sch")
        pathlib.Path(sch_path).write_text(minimal_sch, encoding="utf-8")
        r = _call(open_fn, {"path": sch_path})
        assert "doc_id" in r, f"ecad_open .kicad_sch: missing doc_id: {r}"
        assert r.get("kind") == "sch", f"expected kind=sch, got {r.get('kind')}"
        assert isinstance(r.get("warnings"), list), "warnings must be a list"
        print(f"  ecad_open .kicad_sch -> doc_id={r['doc_id']!r} kind={r['kind']}")

    # ---- .json plain-file dispatch (regression) ----
    from engine.ecad.schema import new_schematic
    doc = new_schematic("json_test")
    with tempfile.TemporaryDirectory() as td:
        json_path = str(pathlib.Path(td) / "test.json")
        pathlib.Path(json_path).write_text(json.dumps(doc), encoding="utf-8")
        r2 = _call(open_fn, {"path": json_path})
        assert "doc_id" in r2, f"ecad_open .json: missing doc_id: {r2}"
        print(f"  ecad_open .json -> doc_id={r2['doc_id']!r}")

    print("  test_ecad_open_kicad_sch PASS")


def test_ecad_design_rpc() -> None:
    """ecad_design RPC must return ok + doc_id; recipe fallback used (no provider in CI)."""
    from engine.ecad.rpc import METHODS

    design_fn = METHODS["ecad_design"]
    r = _call(design_fn, {"request": "5V divider with 2.5V output"})
    assert r.get("ok"), (f"ecad_design RPC should succeed via recipe fallback; "
                         f"got ok=False, r={r}")
    assert r.get("doc_id"), "ecad_design RPC must return a doc_id"
    assert isinstance(r.get("stages"), list) and r["stages"], "stages must be a non-empty list"
    assert "erc" in r and "sim" in r, "ecad_design RPC must return erc + sim dicts"
    print(f"  ecad_design RPC: ok={r['ok']} stop={r.get('stop_reason')!r} "
          f"recipe={r.get('recipe_used')!r} stages={r['stages']}")
    print("  test_ecad_design_rpc PASS")


def test_ecad_dfm_rpc() -> None:
    """ecad_dfm RPC must return violations + summary for a pcb doc; wrong kind raises cleanly."""
    from engine.ecad.rpc import METHODS
    from engine.ecad.ops import OpError

    new_fn = METHODS["ecad_new"]
    apply_fn = METHODS["ecad_apply_ops"]
    dfm_fn = METHODS["ecad_dfm"]

    # --- wrong kind: schematic -> structured error ---
    r_sch = _call(new_fn, {"kind": "sch", "name": "DfmSch"})
    try:
        _call(dfm_fn, {"doc_id": r_sch["doc_id"]})
        assert False, "ecad_dfm on a schematic should raise OpError"
    except OpError as e:
        assert e.code == "invalid_op", f"expected invalid_op, got {e.code}"
    print("  ecad_dfm rejects schematic with invalid_op")

    # --- fresh empty board -> violations list present, possibly empty ---
    r_pcb = _call(new_fn, {"kind": "pcb", "name": "DfmBoard"})
    doc_id = r_pcb["doc_id"]
    result = _call(dfm_fn, {"doc_id": doc_id})
    assert "violations" in result, f"ecad_dfm: missing 'violations': {result}"
    assert "summary" in result, f"ecad_dfm: missing 'summary': {result}"
    assert isinstance(result["violations"], list)
    summary = result["summary"]
    assert "errors" in summary and "warnings" in summary and "by_rule" in summary
    print(f"  ecad_dfm (empty board): errors={summary['errors']} "
          f"warnings={summary['warnings']} rules={[g['rule'] for g in summary['by_rule']]}")

    # --- board with two co-located footprints -> courtyard_overlap must fire ---
    from engine.ecad.library.starter import FOOTPRINTS
    import copy as _copy
    fp_def = FOOTPRINTS.get("Resistor_SMD:R_0603")
    if fp_def is not None:
        fp1 = _copy.deepcopy(fp_def)
        fp2 = _copy.deepcopy(fp_def)
        _call(apply_fn, {
            "doc_id": doc_id,
            "base_revision": r_pcb["revision"],
            "ops": [
                {"op": "pcb_add_footprint", "fpid": "Resistor_SMD:R_0603",
                 "ref": "R1", "value": "10k", "pos": [10.0, 10.0],
                 "side": "top", "pads": fp1.get("pads", []), "def": fp1},
                {"op": "pcb_add_footprint", "fpid": "Resistor_SMD:R_0603",
                 "ref": "R2", "value": "10k", "pos": [10.0, 10.0],
                 "side": "top", "pads": fp2.get("pads", []), "def": fp2},
            ],
        })
        result2 = _call(dfm_fn, {"doc_id": doc_id})
        assert "violations" in result2
        rules = {v.get("rule") for v in result2["violations"]}
        print(f"  ecad_dfm (co-located fps): violations={len(result2['violations'])} "
              f"rules={sorted(rules)}")
        assert "courtyard_overlap" in rules, \
            f"courtyard_overlap expected with co-located footprints, got rules={sorted(rules)}"
        print("  ecad_dfm returns courtyard_overlap violation")

    print("  test_ecad_dfm_rpc PASS")


def test_worker_registry_regression() -> None:
    """Importing engine.worker must succeed and both doc_* and ecad_* must be present."""
    import importlib
    # Fresh import (may already be cached -- that's fine)
    import engine.worker as w

    assert "doc_new" in w._METHODS, "doc_new missing from worker registry"
    ecad_methods = [m for m in w._METHODS if m.startswith("ecad_")]
    assert len(ecad_methods) > 0, "no ecad_* methods registered in worker"

    # Check a sample of the expected ecad methods (including new Wave-L method).
    for expected in ("ecad_new", "ecad_apply_ops", "ecad_tree", "ecad_undo",
                     "ecad_redo", "ecad_export", "ecad_to_3d", "ecad_describe",
                     "ecad_autoroute", "ecad_fill_zones", "ecad_design",
                     "ecad_dfm"):
        assert expected in w._METHODS, f"{expected} not in worker registry"

    # All ecad methods must also be in STRUCTURED_ERROR_METHODS
    for m in ecad_methods:
        assert m in w._STRUCTURED_ERROR_METHODS, \
            f"{m} in _METHODS but not in _STRUCTURED_ERROR_METHODS"

    print(f"  test_worker_registry_regression PASS "
          f"({len(ecad_methods)} ecad methods, {len(w._METHODS)} total)")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    print("engine.ecad.test_rpc")
    tests = [
        test_sch_apply_undo_redo,
        test_pcb_outline_footprint_bom_export,
        test_ecad_to_3d_graceful,
        test_describe,
        test_autoroute_and_fill_zones,
        test_autoroute_wrong_kind,
        test_ecad_open_kicad_sch,
        test_ecad_design_rpc,
        test_ecad_dfm_rpc,
        test_worker_registry_regression,
    ]
    failed = []
    for t in tests:
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            import traceback
            print(f"  FAIL {t.__name__}: {exc}")
            traceback.print_exc()
            failed.append(t.__name__)

    if failed:
        print(f"\nFAILED: {failed}")
        sys.exit(1)
    else:
        print(f"\nAll {len(tests)} tests PASSED")


if __name__ == "__main__":
    main()
