"""Rib + Split feature tests — the last two greyed Features-tab commands.

Run:  & $py -m pytest engine/doc/test_rib_split.py -q   (from repo root, -X utf8)

RIB — build a U-channel (a U cross-section extruded along X: a base plate + two upright walls),
sketch an OPEN line bridging the wall tops, rib it. Assert a single connected solid whose volume
grew and whose rib material reaches BOTH walls (a real, connected rib — the SolidWorks check).

SPLIT — a box split by a mid datum plane: keep="both" -> two ~half bodies; keep="above" -> one
~half body. Also split by a planar MODEL FACE (an L-block's inner wall) -> two bodies. Error cases
(rib closed contour / no body; split non-intersecting tool / no body) are contained, not crashes.

Harness mirrors test_sheetmetal_flat.py.
"""

from __future__ import annotations

from build123d import Solid, Vector

from engine.doc.compiler import compile_document
from engine.doc.schema import validate_part
from engine.doc.selectors import describe_faces, make_selector


# --------------------------------------------------------------------------- harness


def _dim(value: float, expr: str | None = None) -> dict:
    return {"value": value, "expr": expr}


def _feat(fid: str, ftype: str, params: dict, refs: dict | None = None,
          name: str | None = None, suppressed: bool = False) -> dict:
    return {"id": fid, "type": ftype, "name": name or fid,
            "suppressed": suppressed, "params": params, "refs": refs or {}}


def _doc(features: list[dict]) -> dict:
    return {
        "schema": "aicad.part/1",
        "id": "test-rib-split",
        "name": "test",
        "units": {"system": "MMGS"},
        "material": None,
        "equations": {"globals": []},
        "features": features,
    }


def _assert_valid(doc: dict) -> None:
    errs = validate_part(doc)
    assert errs == [], "doc must validate before compile:\n" + "\n".join(errs)


def _status(res, fid: str) -> dict:
    for s in res.feature_status:
        if s["id"] == fid:
            return s
    raise AssertionError(f"no status for {fid}")


def _line(eid: str, p1, p2) -> dict:
    return {"id": eid, "type": "line", "construction": False, "p1": p1, "p2": p2}


def _has_material(shape, lo: tuple[float, float, float], hi: tuple[float, float, float]) -> bool:
    """True if the shape has solid material inside the AABB [lo, hi] (probe-box intersection)."""
    box = Solid.make_box(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]).translate(Vector(*lo))
    try:
        return shape.intersect(box).volume > 1e-6
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- U-channel fixture


# A U cross-section on plane.right (local u=Y, v=Z): base z 0..4 over y -20..20, two upright walls
# (4 wide) at the y-ends rising to z=30. Extruded mid-plane along X to total length 60 (x -30..30).
_U_PTS = [[-20, 0], [20, 0], [20, 30], [16, 30], [16, 4], [-16, 4], [-16, 30], [-20, 30], [-20, 0]]
_U_VOL = 22080.0  # base(60*40*4) + two walls(60*4*26 each)


def _u_channel() -> list[dict]:
    edges = [_line(f"e{i + 1}", _U_PTS[i], _U_PTS[i + 1]) for i in range(len(_U_PTS) - 1)]
    return [
        _feat("f1", "sketch", {"entities": edges, "constraints": []},
              refs={"plane": "plane.right"}),
        _feat("f2", "extrude", {"mode": "boss", "end": "mid_plane", "depth": _dim(60.0)},
              refs={"profile": [{"kind": "region", "sketch": "f1"}]}),
    ]


# --------------------------------------------------------------------------- RIB: connected wall


def test_rib_bridges_two_walls_connected() -> None:
    """A line bridging the U-channel wall tops -> a single connected rib that touches BOTH walls."""
    base = _u_channel()
    # Open contour: a single line near the wall tops (z=24), spanning the gap (y -18..18).
    rib_sketch = _feat("f3", "sketch",
                       {"entities": [_line("e1", [-18, 24], [18, 24])], "constraints": []},
                       refs={"plane": "plane.right"})
    rib = _feat("f4", "rib", {"thickness": _dim(3.0), "side": "both"},
                refs={"profile": [{"kind": "region", "sketch": "f3"}]})
    doc = _doc(base + [rib_sketch, rib])
    _assert_valid(doc)
    res = compile_document(doc)

    assert _status(res, "f2")["ok"], _status(res, "f2")["error"]
    s4 = _status(res, "f4")
    assert s4["ok"], f"rib failed: {s4['error']}"

    body = res.bodies[-1].shape
    # --- one connected solid (the rib fused into the channel, no stray lumps) ---
    assert len(body.solids()) == 1, f"rib must be one connected body; got {len(body.solids())}"

    # --- volume grew by the rib material ---
    assert body.volume > _U_VOL + 1.0, \
        f"rib must add material: {body.volume:.1f} vs base {_U_VOL:.1f}"
    assert abs(s4["resolved"]["_rib_volume"] - (body.volume - _U_VOL)) < 1e-3

    # --- the rib reaches BOTH walls (material at each wall band, high on the wall) ---
    # Walls span y in [-20,-16] (A) and [16,20] (B). Probe at the rib mid-x, high z, into each wall.
    touches_a = _has_material(body, (-1.0, -18.0, 18.0), (1.0, -16.0, 22.0))
    touches_b = _has_material(body, (-1.0, 16.0, 18.0), (1.0, 18.0, 22.0))
    assert touches_a and touches_b, \
        f"rib must connect both walls (A={touches_a}, B={touches_b})"

    # --- the rib spans the gap centre too (genuinely bridges, not two stubs) ---
    assert _has_material(body, (-1.0, -1.0, 18.0), (1.0, 1.0, 22.0)), \
        "rib must have material across the gap centre (a real bridge)"

    print(f"[rib] one body, vol {body.volume:.1f} (+{s4['resolved']['_rib_volume']:.1f}); "
          f"touches both walls + spans centre")


def test_rib_one_sided_and_both_sided() -> None:
    """side='one' grows the wall to one side; side='both' centres it. Both stay one connected body."""
    base = _u_channel()
    rs = _feat("f3", "sketch",
               {"entities": [_line("e1", [-18, 24], [18, 24])], "constraints": []},
               refs={"plane": "plane.right"})

    for side in ("both", "one"):
        rib = _feat("f4", "rib", {"thickness": _dim(3.0), "side": side},
                    refs={"profile": [{"kind": "region", "sketch": "f3"}]})
        doc = _doc(base + [rs, rib])
        _assert_valid(doc)
        res = compile_document(doc)
        s4 = _status(res, "f4")
        assert s4["ok"], f"rib (side={side}) failed: {s4['error']}"
        body = res.bodies[-1].shape
        assert len(body.solids()) == 1, f"rib (side={side}) must be one body"
        assert body.volume > _U_VOL + 1.0, f"rib (side={side}) must add material"
        print(f"[rib-{side}] one body, +{s4['resolved']['_rib_volume']:.1f} mm^3")


def test_rib_draft_tapers_the_wall() -> None:
    """A drafted rib still fuses to a single connected body (and adds slightly less than no-draft)."""
    base = _u_channel()
    rs = _feat("f3", "sketch",
               {"entities": [_line("e1", [-18, 24], [18, 24])], "constraints": []},
               refs={"plane": "plane.right"})
    plain = compile_document(_doc(base + [rs, _feat(
        "f4", "rib", {"thickness": _dim(3.0)},
        refs={"profile": [{"kind": "region", "sketch": "f3"}]})]))
    drafted = compile_document(_doc(base + [rs, _feat(
        "f4", "rib", {"thickness": _dim(3.0), "draft": _dim(6.0)},
        refs={"profile": [{"kind": "region", "sketch": "f3"}]})]))
    sp, sd = _status(plain, "f4"), _status(drafted, "f4")
    assert sp["ok"] and sd["ok"], (sp.get("error"), sd.get("error"))
    assert len(drafted.bodies[-1].shape.solids()) == 1
    assert sd["resolved"]["draft"] == 6.0
    # Draft narrows the rib toward the extrude direction -> less added material than the prism.
    assert sd["resolved"]["_rib_volume"] < sp["resolved"]["_rib_volume"], \
        "drafted rib should add less material than the un-drafted prism"
    print(f"[rib-draft] drafted +{sd['resolved']['_rib_volume']:.1f} < plain "
          f"+{sp['resolved']['_rib_volume']:.1f}")


# --------------------------------------------------------------------------- RIB: error cases


def test_rib_closed_contour_errors() -> None:
    """A CLOSED rib sketch is rejected cleanly (a closed region is an extrude, not a rib)."""
    base = _u_channel()
    closed = _feat("f3", "sketch", {"entities": [
        _line("e1", [-5, 24], [5, 24]), _line("e2", [5, 24], [5, 28]),
        _line("e3", [5, 28], [-5, 28]), _line("e4", [-5, 28], [-5, 24]),
    ], "constraints": []}, refs={"plane": "plane.right"})
    rib = _feat("f4", "rib", {"thickness": _dim(3.0)},
                refs={"profile": [{"kind": "region", "sketch": "f3"}]})
    doc = _doc(base + [closed, rib])
    _assert_valid(doc)
    res = compile_document(doc)
    s4 = _status(res, "f4")
    assert not s4["ok"], "a closed rib contour must error"
    assert "open" in s4["error"].lower() or "closed" in s4["error"].lower(), s4["error"]
    # Body survives the contained error (still the plain U-channel).
    assert abs(res.bodies[-1].shape.volume - _U_VOL) < 1e-3
    print(f"[rib-closed] -> '{s4['error'][:70]}'")


def test_rib_no_body_errors() -> None:
    """A rib with no existing body to grow against errors cleanly."""
    rs = _feat("f1", "sketch",
               {"entities": [_line("e1", [-18, 24], [18, 24])], "constraints": []},
               refs={"plane": "plane.right"})
    rib = _feat("f2", "rib", {"thickness": _dim(3.0)},
                refs={"profile": [{"kind": "region", "sketch": "f1"}]})
    doc = _doc([rs, rib])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f2")
    assert not s["ok"], "rib with no body must error"
    assert "body" in s["error"].lower(), s["error"]
    print(f"[rib-nobody] -> '{s['error'][:70]}'")


# --------------------------------------------------------------------------- SPLIT: datum plane


def _box40() -> list[dict]:
    # A 40-cube centred on the origin (z -20..20), volume 64000.
    return [_feat("f1", "box",
                  {"length": _dim(40), "width": _dim(40), "height": _dim(40), "centered": True})]


def test_split_datum_plane_keep_both() -> None:
    """Split a centred box by plane.top (z=0): keep='both' -> two bodies, each ~half volume."""
    doc = _doc(_box40() + [_feat("f2", "split", {"keep": "both"}, refs={"tool": "plane.top"})])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f2")
    assert s["ok"], s["error"]
    assert abs(s["resolved"]["_split_bodies"] - 1.0) < 1e-9
    assert abs(s["resolved"]["_pieces_kept"] - 2.0) < 1e-9

    assert len(res.bodies) == 2, f"keep=both -> 2 bodies; got {len(res.bodies)}"
    vols = sorted(bo.shape.volume for bo in res.bodies)
    assert abs(vols[0] - 32000.0) < 1e-2 and abs(vols[1] - 32000.0) < 1e-2, vols
    # One piece sits above z=0, the other below.
    zmins = sorted(bo.shape.bounding_box().min.Z for bo in res.bodies)
    zmaxs = sorted(bo.shape.bounding_box().max.Z for bo in res.bodies)
    assert abs(zmins[0] + 20.0) < 1e-3 and abs(zmaxs[1] - 20.0) < 1e-3
    assert abs(zmaxs[0]) < 1e-3 and abs(zmins[1]) < 1e-3, "the two halves meet at z=0"
    print(f"[split-both] 2 bodies, vols {vols}")


def test_split_keep_above_one_body() -> None:
    """keep='above' keeps only the +normal (z>0) half -> one ~half body."""
    doc = _doc(_box40() + [_feat("f2", "split", {"keep": "above"}, refs={"tool": "plane.top"})])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f2")
    assert s["ok"], s["error"]
    assert len(res.bodies) == 1, f"keep=above -> 1 body; got {len(res.bodies)}"
    body = res.bodies[0].shape
    assert abs(body.volume - 32000.0) < 1e-2, body.volume
    bb = body.bounding_box()
    assert abs(bb.min.Z) < 1e-3 and abs(bb.max.Z - 20.0) < 1e-3, "kept piece is the z>0 half"
    print(f"[split-above] 1 body, vol {body.volume:.1f}, z in [{bb.min.Z:.1f},{bb.max.Z:.1f}]")


def test_split_keep_below_one_body() -> None:
    """keep='below' keeps only the -normal (z<0) half -> one ~half body."""
    doc = _doc(_box40() + [_feat("f2", "split", {"keep": "below"}, refs={"tool": "plane.top"})])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f2")
    assert s["ok"], s["error"]
    assert len(res.bodies) == 1
    bb = res.bodies[0].shape.bounding_box()
    assert abs(bb.max.Z) < 1e-3 and abs(bb.min.Z + 20.0) < 1e-3, "kept piece is the z<0 half"
    print(f"[split-below] 1 body, z in [{bb.min.Z:.1f},{bb.max.Z:.1f}]")


# --------------------------------------------------------------------------- SPLIT: model face


def test_split_by_planar_model_face() -> None:
    """Split an L-block by its inner vertical wall face (plane x=20) -> two bodies summing to whole.

    The L cross-section (on plane.front) extruded along -Y has an inner wall at x=20 whose PLANE
    passes through the lower solid block, so picking it splits the body into x<20 / x>20 pieces.
    """
    pts = [[0, 0], [40, 0], [40, 20], [20, 20], [20, 40], [0, 40], [0, 0]]
    sk = _feat("f1", "sketch",
               {"entities": [_line(f"e{i + 1}", pts[i], pts[i + 1]) for i in range(len(pts) - 1)],
                "constraints": []}, refs={"plane": "plane.front"})
    ext = _feat("f2", "extrude", {"mode": "boss", "end": "blind", "depth": _dim(30.0)},
                refs={"profile": [{"kind": "region", "sketch": "f1"}]})
    res0 = compile_document(_doc([sk, ext]))
    assert _status(res0, "f2")["ok"], _status(res0, "f2")["error"]
    body = res0.bodies[-1].shape
    whole_vol = body.volume

    # Pick the inner vertical wall: a planar face with an X-aligned normal at x≈20.
    infos = describe_faces(body.wrapped)
    cands = [i for i in infos
             if i.surf == "plane" and i.axis is not None
             and abs(abs(i.axis[0]) - 1.0) < 1e-3 and abs(i.centroid[0] - 20.0) < 0.5]
    assert cands, "expected an inner x=20 wall face on the L-block"
    sel = make_selector(cands[0])
    sel["owner_feature"] = "f2"

    doc = _doc([sk, ext, _feat("f3", "split", {"keep": "both"}, refs={"tool": sel})])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f3")
    assert s["ok"], f"face-tool split failed: {s['error']}"
    assert len(res.bodies) == 2, f"face split keep=both -> 2 bodies; got {len(res.bodies)}"
    vols = sorted(bo.shape.volume for bo in res.bodies)
    assert abs(sum(vols) - whole_vol) < 1e-2, f"split pieces must sum to the whole: {vols}"
    # The split is at x=20: one piece x<20, the other x>20.
    xmaxs = sorted(bo.shape.bounding_box().max.X for bo in res.bodies)
    xmins = sorted(bo.shape.bounding_box().min.X for bo in res.bodies)
    assert abs(xmaxs[0] - 20.0) < 1e-3 and abs(xmins[1] - 20.0) < 1e-3, \
        "the two pieces meet at the x=20 face plane"
    print(f"[split-face] 2 bodies, vols {vols} (sum {sum(vols):.1f} == whole {whole_vol:.1f})")


# --------------------------------------------------------------------------- SPLIT: error cases


def test_split_non_intersecting_tool_errors() -> None:
    """A datum plane offset clear of the box (z=100) splits nothing -> contained error."""
    box = _box40()
    dp = _feat("f2", "plane", {"method": "offset", "offset": _dim(100.0)},
               refs={"base": "plane.top"})
    sp = _feat("f3", "split", {"keep": "both"},
               refs={"tool": {"kind": "datum", "feature": "f2"}})
    doc = _doc(box + [dp, sp])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f3")
    assert not s["ok"], "a non-intersecting split tool must error"
    assert "intersect" in s["error"].lower(), s["error"]
    # The body survives unchanged.
    assert len(res.bodies) == 1 and abs(res.bodies[0].shape.volume - 64000.0) < 1e-2
    print(f"[split-miss] -> '{s['error'][:70]}'")


def test_split_no_body_errors() -> None:
    """Split with no body to divide errors cleanly."""
    doc = _doc([_feat("f1", "split", {"keep": "both"}, refs={"tool": "plane.top"})])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f1")
    assert not s["ok"], "split with no body must error"
    assert "body" in s["error"].lower(), s["error"]
    print(f"[split-nobody] -> '{s['error'][:70]}'")
