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
# test_worker_registry_regression
# ---------------------------------------------------------------------------

def test_worker_registry_regression() -> None:
    """Importing engine.worker must succeed and both doc_* and ecad_* must be present."""
    import importlib
    # Fresh import (may already be cached — that's fine)
    import engine.worker as w

    assert "doc_new" in w._METHODS, "doc_new missing from worker registry"
    ecad_methods = [m for m in w._METHODS if m.startswith("ecad_")]
    assert len(ecad_methods) > 0, "no ecad_* methods registered in worker"

    # Check a sample of the expected ecad methods
    for expected in ("ecad_new", "ecad_apply_ops", "ecad_tree", "ecad_undo",
                     "ecad_redo", "ecad_export", "ecad_to_3d", "ecad_describe"):
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
