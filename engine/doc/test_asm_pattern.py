"""Component-pattern engine tests (Task 2A): asm_linear_pattern / asm_circular_pattern.

These exercise engine.doc.assembly.compile_assembly's pattern pass directly (no worker subprocess
needed): two part .step+ files are authored + saved, assembled, then a pattern feature is added to
the assembly doc and we assert the SYNTHETIC instance bodies are emitted at the right world poses.

Run:  python -m engine.doc.test_asm_pattern   (or via pytest)
"""

from __future__ import annotations

import math
import tempfile
from pathlib import Path

import numpy as np

from engine.doc import assembly as asm
from engine.doc import drawing
from engine.doc import session as docsess
from engine.doc import stepplus


REPO = Path(__file__).resolve().parents[2]


def _dim(v: float) -> dict:
    return {"value": v, "expr": None}


def _make_part(name: str, ops: list, path: str) -> None:
    s = docsess.new_session(kind="part", name=name)
    docsess.apply_ops(s, s.revision, ops)
    stepplus.save(s, path)
    docsess.close_session(s.doc_id)


def _body_origin(body) -> np.ndarray:
    """World translation (tx,ty,tz) of an AsmBodyOut's row-major transform."""
    t = body.transform
    return np.array([t[3], t[7], t[11]], dtype=float)


def _build_two_part_assembly(tmp: Path):
    """A thick non-square plate (asymmetric, to avoid the symmetric-ambiguity selector trap) + peg.

    Returns (doc, base_dir). The peg is inserted offset; no mates, so both keep their inserted pose
    (base auto-fixed as first)."""
    base_p = str(tmp / "plate.step+")
    peg_p = str(tmp / "peg.step+")
    # 60 x 30 x 12 plate (clearly non-square so the long/short axes are unambiguous).
    _make_part("plate", [
        {"op": "add_feature", "feature": {"type": "box", "name": "Plate", "suppressed": False,
            "params": {"length": _dim(60), "width": _dim(30), "height": _dim(12),
                       "centered": True}, "refs": {}}},
    ], base_p)
    _make_part("peg", [
        {"op": "add_feature", "feature": {"type": "cylinder", "name": "Peg", "suppressed": False,
            "params": {"radius": _dim(3), "height": _dim(20), "centered": True}, "refs": {}}},
    ], peg_p)

    s = docsess.new_session(kind="assembly", name="patterned")
    r = docsess.apply_ops(s, s.revision,
                          [{"op": "asm_insert_component", "source": {"path": base_p}}])
    # peg placed at x=20 (well within the plate footprint) so a +X pattern marches across it.
    off = [1, 0, 0, 20, 0, 1, 0, 0, 0, 0, 1, 12, 0, 0, 0, 1]  # row-major
    docsess.apply_ops(s, s.revision,
                      [{"op": "asm_insert_component", "source": {"path": peg_p},
                        "transform": off}])
    return s


def test_linear_pattern_emits_instances():
    tmp = Path(tempfile.mkdtemp(prefix="aicad_patlin_"))
    s = _build_two_part_assembly(tmp)
    # Pattern the peg (k2) 3-up along +X, 15mm spacing.
    docsess.apply_ops(s, s.revision, [
        {"op": "add_feature", "feature": {
            "type": "asm_linear_pattern", "name": "LPattern", "suppressed": False,
            "params": {"count1": _dim(3), "spacing1": _dim(15)},
            "refs": {"dir1": "axis.x", "components": ["k2"]}}},
    ])

    ares = asm.compile_assembly(s.doc)
    comp_bodies = [b for b in ares.bodies if "#" not in b.component_id]
    inst_bodies = [b for b in ares.bodies if "#" in b.component_id]

    assert len(comp_bodies) == 2, f"two real component bodies, got {len(comp_bodies)}"
    assert len(inst_bodies) == 2, f"count1=3 -> 2 ADDITIONAL instances, got {len(inst_bodies)}"

    # Synthetic ids must not collide with the component ordinals (1,2).
    assert all(b.body_id >= 1_000_000 for b in inst_bodies), \
        [b.body_id for b in inst_bodies]
    assert {b.component_id for b in inst_bodies} == {"k2#1", "k2#2"}

    # The seed peg sits at x=20; instances at x=35 and x=50, same y,z.
    seed = next(b for b in comp_bodies if b.component_id == "k2")
    sx, sy, sz = _body_origin(seed)
    assert abs(sx - 20) < 1e-6
    by_cid = {b.component_id: _body_origin(b) for b in inst_bodies}
    assert abs(by_cid["k2#1"][0] - 35) < 1e-6 and abs(by_cid["k2#1"][1] - sy) < 1e-6 \
        and abs(by_cid["k2#1"][2] - sz) < 1e-6, by_cid["k2#1"]
    assert abs(by_cid["k2#2"][0] - 50) < 1e-6, by_cid["k2#2"]

    # interference iterates ares.bodies (asm_world_bodies) -> must not crash with instances present,
    # and must world-place every body (the 2 components + 2 instances).
    s.asm_result = ares
    shapes = docsess.asm_world_bodies(s, explode=False)
    assert len(shapes) == len(ares.bodies), (len(shapes), len(ares.bodies))

    docsess.close_session(s.doc_id)
    print("[asm-pat] linear: 2 instances at x=35,50  OK")


def test_linear_pattern_rolls_up_in_bom():
    """A count1=3 linear pattern of one peg yields ONE BOM row for the peg with quantity 3 (1 seed +
    2 instances), source = the peg's path — patterned instances do NOT spawn separate line items."""
    tmp = Path(tempfile.mkdtemp(prefix="aicad_patbom_"))
    s = _build_two_part_assembly(tmp)
    peg_path = str(tmp / "peg.step+")
    docsess.apply_ops(s, s.revision, [
        {"op": "add_feature", "feature": {
            "type": "asm_linear_pattern", "name": "LPattern", "suppressed": False,
            "params": {"count1": _dim(3), "spacing1": _dim(15)},
            "refs": {"dir1": "axis.x", "components": ["k2"]}}},
    ])

    # asm_component_bodies skips the synthetic instances but rolls their count into the seed quantity.
    comps = docsess.asm_component_bodies(s)
    assert len(comps) == 2, f"two REAL components (no instance rows), got {len(comps)}"
    assert all("#" not in c["component"] for c in comps), comps
    peg = next(c for c in comps if c["component"] == "k2")
    assert peg["quantity"] == 3, f"peg seed + 2 instances -> quantity 3, got {peg['quantity']}"
    plate = next(c for c in comps if c["component"] == "k1")
    assert plate["quantity"] == 1, plate["quantity"]

    # The BOM rolls that up into ONE peg row of quantity 3 at the peg's source path.
    res = drawing.new_drawing(s.doc_id)
    bom = drawing.drawing_bom(res["doc_id"])
    assert bom["is_assembly"] is True, bom
    peg_rows = [r for r in bom["rows"] if r["source"] == peg_path]
    assert len(peg_rows) == 1, f"exactly one peg BOM row, got {peg_rows}"
    assert peg_rows[0]["quantity"] == 3, f"peg row quantity 3, got {peg_rows[0]}"
    assert "#" not in peg_rows[0]["name"], peg_rows[0]["name"]

    drawing.close_drawing(res["doc_id"])
    docsess.close_session(s.doc_id)
    print("[asm-pat] BOM rollup: peg qty=3 in one row  OK")


def test_circular_pattern_emits_instances():
    tmp = Path(tempfile.mkdtemp(prefix="aicad_patcir_"))
    s = _build_two_part_assembly(tmp)
    # 4-up full circle about world Z; equal spacing -> step = 360/4 = 90 deg.
    docsess.apply_ops(s, s.revision, [
        {"op": "add_feature", "feature": {
            "type": "asm_circular_pattern", "name": "CPattern", "suppressed": False,
            "params": {"count": _dim(4), "angle": _dim(360), "equal_spacing": True},
            "refs": {"axis": "axis.z", "components": ["k2"]}}},
    ])

    ares = asm.compile_assembly(s.doc)
    inst = [b for b in ares.bodies if "#" in b.component_id]
    assert len(inst) == 3, f"count=4 -> 3 additional instances, got {len(inst)}"

    seed = next(b for b in ares.bodies if b.component_id == "k2")
    sx, sy, _ = _body_origin(seed)
    r = math.hypot(sx, sy)  # seed radius from the Z axis (=20)
    assert abs(r - 20) < 1e-6, r
    # Each instance is the seed rotated by 90*k about Z: radius preserved, z preserved.
    by_cid = {b.component_id: _body_origin(b) for b in inst}
    # k=1 -> 90deg: (20,0) -> (0,20)
    p1 = by_cid["k2#1"]
    assert abs(p1[0] - 0) < 1e-6 and abs(p1[1] - 20) < 1e-6, p1
    # k=2 -> 180deg: (20,0) -> (-20,0)
    p2 = by_cid["k2#2"]
    assert abs(p2[0] + 20) < 1e-6 and abs(p2[1]) < 1e-6, p2
    # k=3 -> 270deg: (20,0) -> (0,-20)
    p3 = by_cid["k2#3"]
    assert abs(p3[0]) < 1e-6 and abs(p3[1] + 20) < 1e-6, p3
    # all preserve radius from the Z axis
    for cid, p in by_cid.items():
        assert abs(math.hypot(p[0], p[1]) - 20) < 1e-6, (cid, p)

    docsess.close_session(s.doc_id)
    print("[asm-pat] circular: 3 instances at 90/180/270 deg  OK")


def test_dangling_direction_is_skipped():
    """A pattern whose dir1 is an unsupported model-edge pick emits NO instances (no crash)."""
    tmp = Path(tempfile.mkdtemp(prefix="aicad_patdang_"))
    s = _build_two_part_assembly(tmp)
    docsess.apply_ops(s, s.revision, [
        {"op": "add_feature", "feature": {
            "type": "asm_linear_pattern", "name": "LBad", "suppressed": False,
            "params": {"count1": _dim(3), "spacing1": _dim(15)},
            # a (well-formed) edge selector is a v1-unsupported world-frame ref -> skipped
            "refs": {"dir1": {"kind": "edge", "surf": "line", "centroid": [0.0, 0.0, 0.0],
                              "axis": [1.0, 0.0, 0.0], "area_or_len": 30.0,
                              "owner_feature": "f1", "hint_index": 0},
                     "components": ["k2"]}}},
    ])
    ares = asm.compile_assembly(s.doc)
    inst = [b for b in ares.bodies if "#" in b.component_id]
    assert inst == [], f"unsupported dir ref must emit no instances, got {inst}"
    docsess.close_session(s.doc_id)
    print("[asm-pat] dangling dir: 0 instances (skipped)  OK")


def main() -> None:
    test_linear_pattern_emits_instances()
    test_linear_pattern_rolls_up_in_bom()
    test_circular_pattern_emits_instances()
    test_dangling_direction_is_skipped()
    print("\nASM PATTERN TESTS PASSED")


if __name__ == "__main__":
    main()
