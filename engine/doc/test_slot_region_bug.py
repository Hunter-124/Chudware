"""Reproduce + guard the user-reported SLOT-in-profile extrude bug.

A sketch with an outer square + a SLOT inner loop (2 lines + 2 arc caps) must extrude as a
plate WITH the slot hole. The bug: the slot's loop was silently dropped by the region/loop
detector (the slot edges came back from SlotOverall().edges() in face-topology order with
inconsistent orientation + a small junction gap, so Wire(...) reported invalid and
_loop_to_face returned None), leaving ruled-surface wedges instead of a capped slot hole.
"""

from __future__ import annotations

import math

import engine.doc.test_features1 as T
from engine.doc.compiler import compile_document
from engine.doc.schema import validate_document


def _square_slot_sketch(fid: str = "f1") -> dict:
    """80 x 50 square with a horizontal slot: spine (25,25)->(55,25), width 10."""
    return T._feat(fid, "sketch", {"entities": [
        {"id": "e1", "type": "line", "construction": False, "p1": [0.0, 0.0], "p2": [80.0, 0.0]},
        {"id": "e2", "type": "line", "construction": False, "p1": [80.0, 0.0], "p2": [80.0, 50.0]},
        {"id": "e3", "type": "line", "construction": False, "p1": [80.0, 50.0], "p2": [0.0, 50.0]},
        {"id": "e4", "type": "line", "construction": False, "p1": [0.0, 50.0], "p2": [0.0, 0.0]},
        {"id": "e5", "type": "slot", "construction": False,
         "p1": [25.0, 25.0], "p2": [55.0, 25.0], "width": 10.0},
    ], "constraints": []}, refs={"plane": "plane.top"})


def _extrude(fid: str, profile: list, depth: float = 10.0) -> dict:
    return T._feat(fid, "extrude", {"mode": "boss", "end": "blind",
                                    "depth": {"value": depth, "expr": None}},
                   refs={"profile": profile})


# slot capsule area = rectangle (len * width) + two semicircles (pi * r^2)
_SPINE_LEN = 30.0
_WIDTH = 10.0
_SLOT_AREA = _SPINE_LEN * _WIDTH + math.pi * (_WIDTH / 2.0) ** 2
_DEPTH = 10.0
_EXPECTED_VOL = (80.0 * 50.0 - _SLOT_AREA) * _DEPTH


def test_slot_in_profile_extrudes_as_plate_with_slot_hole() -> None:
    doc = T._doc([_square_slot_sketch("f1"),
                  _extrude("f2", [{"kind": "region", "sketch": "f1"}])])
    assert validate_document(doc) == [], "slot-in-profile sketch must validate"
    res = compile_document(doc)
    s = T._status(res, "f2")
    assert s["ok"], f"slot-in-profile extrude failed: {s.get('error')}"
    body = res.bodies[-1].shape
    vol = body.volume
    # If the slot loop is dropped, the plate is filled (40000) or malformed — not 36214.6.
    assert abs(vol - _EXPECTED_VOL) < 5.0, \
        f"plate-with-slot volume {vol:.1f} != {_EXPECTED_VOL:.1f} (slot loop dropped?)"


def test_slot_region_is_a_distinct_region() -> None:
    """square + slot -> 2 selectable regions (plate-with-slot + the slot interior)."""
    res = compile_document(T._doc([_square_slot_sketch("f1")]))
    rc = T._status(res, "f1")["resolved"].get("_region_count")
    assert rc == 2.0, f"square+slot should yield 2 regions, got {rc}"


def test_slot_only_extrudes_to_a_capsule() -> None:
    """A lone slot (no outer square) extrudes to a clean capsule of the expected volume."""
    sk = T._feat("f1", "sketch", {"entities": [
        {"id": "e1", "type": "slot", "construction": False,
         "p1": [25.0, 25.0], "p2": [55.0, 25.0], "width": 10.0},
    ], "constraints": []}, refs={"plane": "plane.top"})
    res = compile_document(T._doc([sk, _extrude("f2", [{"kind": "region", "sketch": "f1"}])]))
    s = T._status(res, "f2")
    assert s["ok"], f"slot-only extrude failed: {s.get('error')}"
    vol = res.bodies[-1].shape.volume
    expected = _SLOT_AREA * _DEPTH
    assert abs(vol - expected) < 2.0, f"capsule volume {vol:.1f} != {expected:.1f}"
    assert len(res.bodies[-1].shape.solids()) == 1


# --------------------------------------------------------------------------- the actual bug
# The host's Slot TOOL does NOT emit a single `slot` entity — it DECOMPOSES the drawn slot into
# 2 side lines + 2 cap arcs (ccw, start/end = axis±90) + a construction centerline (see
# SketchMode::addDecomposedSlot). That decomposed loop is the one that broke (the arc caps go
# through _detect_loops' edge chaining, where an end-matched arc isn't reversed, so the loop's
# wire is inconsistent and _loop_to_face drops it). Reproduce EXACTLY what a drawn slot produces.


def _decomposed_slot_entities(sp1, sp2, width: float, base: int) -> list:
    hw = width / 2.0
    ax = math.degrees(math.atan2(sp2[1] - sp1[1], sp2[0] - sp1[0]))
    nx, ny = -math.sin(math.radians(ax)), math.cos(math.radians(ax))
    A1 = [sp1[0] + nx * hw, sp1[1] + ny * hw]
    B1 = [sp2[0] + nx * hw, sp2[1] + ny * hw]
    A2 = [sp1[0] - nx * hw, sp1[1] - ny * hw]
    B2 = [sp2[0] - nx * hw, sp2[1] - ny * hw]
    return [
        {"id": f"e{base}",   "type": "line", "construction": False, "p1": A1, "p2": B1},
        {"id": f"e{base+1}", "type": "line", "construction": False, "p1": A2, "p2": B2},
        {"id": f"e{base+2}", "type": "arc", "construction": False, "center": list(sp1),
         "radius": hw, "start": ax + 90.0, "end": ax - 90.0, "ccw": True},
        {"id": f"e{base+3}", "type": "arc", "construction": False, "center": list(sp2),
         "radius": hw, "start": ax - 90.0, "end": ax + 90.0, "ccw": True},
        {"id": f"e{base+4}", "type": "line", "construction": True, "p1": list(sp1), "p2": list(sp2)},
    ]


def test_decomposed_slot_in_profile_extrudes_with_hole() -> None:
    """THE reported bug: a drawn (decomposed) slot inside a square must cut the slot hole."""
    ents = [
        {"id": "e1", "type": "line", "construction": False, "p1": [0.0, 0.0], "p2": [80.0, 0.0]},
        {"id": "e2", "type": "line", "construction": False, "p1": [80.0, 0.0], "p2": [80.0, 50.0]},
        {"id": "e3", "type": "line", "construction": False, "p1": [80.0, 50.0], "p2": [0.0, 50.0]},
        {"id": "e4", "type": "line", "construction": False, "p1": [0.0, 50.0], "p2": [0.0, 0.0]},
    ] + _decomposed_slot_entities([25.0, 25.0], [55.0, 25.0], 10.0, base=5)
    sk = T._feat("f1", "sketch", {"entities": ents, "constraints": []}, refs={"plane": "plane.top"})
    res = compile_document(T._doc([sk, _extrude("f2", [{"kind": "region", "sketch": "f1"}])]))
    s = T._status(res, "f2")
    assert s["ok"], f"decomposed-slot extrude failed: {s.get('error')}"
    body = res.bodies[-1].shape
    assert abs(body.volume - _EXPECTED_VOL) < 5.0, \
        f"decomposed-slot plate volume {body.volume:.1f} != {_EXPECTED_VOL:.1f} (slot loop dropped?)"
    assert len(body.solids()) == 1, f"expected 1 clean solid, got {len(body.solids())}"
