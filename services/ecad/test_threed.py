r"""ECAD → 3D bridge tests (contracts/ecad/fab_outputs.md §5). House style: main() + asserts.

Run (from repo root):
    python -m services.ecad.test_threed

Uses build123d / OCP for STEP export/import.

Covers:
  1. Board solid volume ≈ outline_area × thickness − drill holes (±2%).
  2. A placed proxy component's centroid x,y ≈ its footprint world position.
  3. board_to_part_doc emits a structurally correct import envelope; the generated
     STEP re-imports to a non-empty shape. Native host compilation is not exercised here.
  4. An empty/open outline raises a clear BoardGeometryError; a no-pad footprint is skipped.
"""

from __future__ import annotations

import math
import os
import tempfile

from services.ecad import schema
from services.ecad.threed import (
    BoardGeometryError,
    board_to_part_doc,
    board_to_step,
)


# --------------------------------------------------------------------------- fixture


def _rect_outline(w: float, h: float) -> dict:
    """A closed CCW rectangular Edge.Cuts outline from (0,0) to (w,h), as 4 line shapes."""
    return {
        "shapes": [
            {"type": "line", "p1": [0.0, 0.0], "p2": [w, 0.0]},
            {"type": "line", "p1": [w, 0.0], "p2": [w, h]},
            {"type": "line", "p1": [w, h], "p2": [0.0, h]},
            {"type": "line", "p1": [0.0, h], "p2": [0.0, 0.0]},
        ],
        "closed": True,
    }


def _smd_footprint(fid: str, ref: str, pos, angle=0.0, side="top") -> dict:
    """A simple 2-pad SMD footprint with NO model_3d (forces the box-proxy fallback)."""
    return {
        "id": fid, "fpid": "Resistor_SMD:R_0805_2012Metric", "ref": ref, "value": "10k",
        "pos": list(pos), "angle": angle, "side": side, "attr": "smd",
        "pads": [
            {"id": f"{fid}_1", "number": "1", "pos": [-1.0, 0.0], "angle": 0.0,
             "shape": "rect", "size": [1.2, 1.4], "type": "smd",
             "layers": ["F.Cu", "F.Mask", "F.Paste"], "drill": None, "net": "GND", "pin": "1"},
            {"id": f"{fid}_2", "number": "2", "pos": [1.0, 0.0], "angle": 0.0,
             "shape": "rect", "size": [1.2, 1.4], "type": "smd",
             "layers": ["F.Cu", "F.Mask", "F.Paste"], "drill": None, "net": "VCC", "pin": "2"},
        ],
        "graphics": [], "model_3d": None, "linked_symbol": None,
    }


def _make_board() -> dict:
    """50×30 rectangular board, 2 proxy SMD footprints (one rotated), and one drilled via."""
    board = schema.new_board("TestBridgeBoard")
    board["nets"] = [{"name": "GND", "class": "default"}, {"name": "VCC", "class": "default"}]
    board["outline"] = _rect_outline(50.0, 30.0)
    board["footprints"] = [
        _smd_footprint("fp1", "R1", [12.0, 8.0], angle=0.0),
        _smd_footprint("fp2", "R2", [30.0, 20.0], angle=90.0),
    ]
    board["vias"] = [
        {"id": "v1", "net": "GND", "at": [40.0, 25.0], "drill": 0.4, "diameter": 0.8,
         "layers": ["F.Cu", "B.Cu"], "type": "through"},
    ]
    return board


# --------------------------------------------------------------------------- 1. board volume


def test_board_volume() -> None:
    board = _make_board()
    with tempfile.TemporaryDirectory() as td:
        out = board_to_step(board, os.path.join(td, "board.step"),
                            {"components": True, "copper": False})
        summ = out["summary"]

        w, h, t = 50.0, 30.0, 1.6
        outline_area = w * h
        # One via drill: 0.4 mm diameter through the full thickness.
        drill_r = 0.4 / 2.0
        hole_vol = math.pi * drill_r * drill_r * t
        expected = outline_area * t - hole_vol

        assert summ["n_holes"] == 1, f"expected 1 drill, got {summ['n_holes']}"
        assert abs(summ["outline_area"] - outline_area) < 1e-6, summ["outline_area"]
        rel = abs(summ["board_volume"] - expected) / expected
        assert rel < 0.02, (f"board volume {summ['board_volume']:.3f} vs expected {expected:.3f} "
                            f"(rel err {rel:.4f})")
        assert os.path.getsize(out["step_path"]) > 100, "STEP file not written"
        print(f"[threed] board volume={summ['board_volume']:.2f} mm^3 expected={expected:.2f} "
              f"(rel err {rel:.5f}); holes={summ['n_holes']}")


# --------------------------------------------------------------------------- 2. proxy placement


def test_proxy_placement() -> None:
    """A placed proxy component's centroid x,y must match its footprint world position."""
    from build123d import import_step

    board = _make_board()
    with tempfile.TemporaryDirectory() as td:
        step_path = os.path.join(td, "board.step")
        out = board_to_step(board, step_path, {"components": True})
        summ = out["summary"]
        assert summ["n_proxies"] == 2, f"expected 2 proxies, got {summ['n_proxies']}"
        assert summ["n_components"] == 2, summ

        # Re-import the assembly and find the proxy solids (small boxes well above the board).
        shape = import_step(step_path)
        children = list(getattr(shape, "children", []) or [])
        assert children, "imported STEP compound has no children"

        # Component proxies are the small boxes whose center z is above the board top (1.6mm).
        # Collect each child's center; match the two footprint positions (12,8) and (30,20).
        targets = {(12.0, 8.0), (30.0, 20.0)}
        found = []
        for ch in children:
            try:
                c = ch.center()
            except Exception:  # noqa: BLE001
                continue
            found.append((c.X, c.Y, c.Z))

        for tx, ty in targets:
            match = [p for p in found if abs(p[0] - tx) < 0.5 and abs(p[1] - ty) < 0.5
                     and p[2] > 1.0]
            assert match, (f"no proxy centroid near footprint ({tx},{ty}); centers seen: "
                           + ", ".join(f"({x:.1f},{y:.1f},{z:.1f})" for x, y, z in found))
            print(f"[threed] proxy centroid ({match[0][0]:.2f},{match[0][1]:.2f},"
                  f"{match[0][2]:.2f}) ~= footprint ({tx},{ty})")


# --------------------------------------------------------------------------- 3. part doc round-trip


def test_part_doc_compiles() -> None:
    """board_to_part_doc produces a re-importable STEP + a well-formed aicad.part/1 envelope.

    Native compile and schema validation happen when the host opens the part document.
    This test covers the Python half: ECAD->STEP geometry round-trips, and the part-doc
    envelope is structurally correct (one `import` feature referencing the STEP)."""
    from services.formats.formats import import_path, _IMPORT_CACHE

    board = _make_board()
    with tempfile.TemporaryDirectory() as td:
        step_path = os.path.join(td, "board.step")
        out = board_to_step(board, step_path, {"components": True})

        # (a) STEP re-imports to a non-empty shape.
        _IMPORT_CACHE.clear()
        imported = import_path(step_path)
        assert imported, "STEP re-import returned no bodies"
        assert any(getattr(s, "wrapped", None) is not None for _n, s in imported), \
            "STEP re-import produced no OCCT solids"
        print(f"[threed] STEP re-import: {len(imported)} body(ies)")

        # (b) part-doc envelope is well-formed (compiled/validated in the C++ core now).
        doc = board_to_part_doc(board, step_path)
        assert doc.get("schema") == "aicad.part/1", f"bad schema: {doc.get('schema')}"
        feats = doc.get("features")
        assert isinstance(feats, list) and len(feats) >= 1, "part doc has no features"
        f1 = feats[0]
        assert f1.get("type") == "import", f"expected an import feature, got {f1.get('type')}"
        assert f1.get("params", {}).get("source_path"), "import feature missing source_path"
        print(f"[threed] part doc envelope OK ({len(feats)} feature(s), import->STEP)")


# --------------------------------------------------------------------------- 4. robustness


def test_open_outline_raises() -> None:
    """An empty outline and an open (non-closing) outline both raise a clear error."""
    board = schema.new_board("Empty")
    board["outline"] = {"shapes": [], "closed": False}
    try:
        board_to_step(board, os.path.join(tempfile.gettempdir(), "_never.step"))
        raise AssertionError("empty outline should have raised BoardGeometryError")
    except BoardGeometryError as exc:
        print(f"[threed] empty outline raised cleanly: {exc}")

    # Open outline: 3 of 4 rectangle sides (does not close).
    board2 = schema.new_board("Open")
    board2["outline"] = {
        "shapes": [
            {"type": "line", "p1": [0, 0], "p2": [10, 0]},
            {"type": "line", "p1": [10, 0], "p2": [10, 10]},
            {"type": "line", "p1": [10, 10], "p2": [3, 10]},  # gap back to origin
        ],
        "closed": False,
    }
    try:
        board_to_step(board2, os.path.join(tempfile.gettempdir(), "_never2.step"))
        raise AssertionError("open outline should have raised BoardGeometryError")
    except BoardGeometryError as exc:
        print(f"[threed] open outline raised cleanly: {exc}")


def test_nopad_footprint_skipped() -> None:
    """A footprint with no pads is skipped (recorded in summary), not crashed on."""
    board = _make_board()
    board["footprints"].append({
        "id": "fp9", "fpid": "x:y", "ref": "X1", "value": "", "pos": [5.0, 5.0],
        "angle": 0.0, "side": "top", "attr": "virtual", "pads": [], "graphics": [],
        "model_3d": None, "linked_symbol": None,
    })
    with tempfile.TemporaryDirectory() as td:
        out = board_to_step(board, os.path.join(td, "board.step"), {"components": True})
        summ = out["summary"]
        assert summ["n_skipped"] >= 1, f"expected a skipped footprint, got {summ}"
        assert any("no pads" in w for w in summ["warnings"]), summ["warnings"]
        # The two real footprints still produced proxies.
        assert summ["n_proxies"] == 2, summ
        print(f"[threed] no-pad footprint skipped (n_skipped={summ['n_skipped']}, "
              f"n_proxies={summ['n_proxies']})")


# --------------------------------------------------------------------------- 5. copper smoke


def test_copper_smoke() -> None:
    """opts.copper=True adds copper without crashing (smoke test; bounding realism only)."""
    board = _make_board()
    board["tracks"] = [
        {"id": "t1", "net": "GND", "layer": "F.Cu", "width": 0.25, "kind": "segment",
         "p1": [12.0, 8.0], "p2": [30.0, 20.0]},
        {"id": "t2", "net": "VCC", "layer": "B.Cu", "width": 0.3, "kind": "segment",
         "p1": [5.0, 5.0], "p2": [45.0, 5.0]},
    ]
    with tempfile.TemporaryDirectory() as td:
        out = board_to_step(board, os.path.join(td, "board_cu.step"),
                            {"components": False, "copper": True})
        assert os.path.getsize(out["step_path"]) > 100
        print(f"[threed] copper smoke ok (warnings={len(out['summary']['warnings'])})")


# --------------------------------------------------------------------------- main


def main() -> None:
    test_board_volume()
    test_proxy_placement()
    test_part_doc_compiles()
    test_open_outline_raises()
    test_nopad_footprint_skipped()
    test_copper_smoke()
    print("\nTHREED TESTS PASSED")


if __name__ == "__main__":
    main()
