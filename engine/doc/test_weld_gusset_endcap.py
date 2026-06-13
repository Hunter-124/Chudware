"""Weldment dress features — `weld_gusset` + `weld_end_cap`.

Run:  & $py -m pytest engine/doc/test_weld_gusset_endcap.py   (from repo root)

Both features resolve from PICKED PLANAR FACES on the existing weld_member bodies (the simplest
ref form that stays robust): the gusset from two faces (one on each member at an L-corner), the end
cap from one member END face.

Covers:
  * gusset on a 2-member L-frame → a triangular plate body, volume ≈ ½·d1·d2·thickness, that
    touches BOTH members; polygonal type is smaller than triangular; location offset shifts it.
  * end cap (outer) on a square-tube open end → a plate covering the tube end, area ≈ profile bbox
    × thickness, mounted on the end face; overhang grows the plate.
  * end cap (inner) → a plate inset into the bore (smaller than the outer envelope).
  * error cases: not two faces (gusset); parallel members (gusset); non-existent face; inner cap on
    a solid section; bad params.
"""

from __future__ import annotations

import math

import engine.doc.test_features1 as T
import engine.doc.test_weldment as TW
from engine.doc.compiler import CompileContext
from engine.doc.features import get_compiler
from engine.doc.features.datum import reserved_datums
from engine.doc.selectors import describe_faces, make_selector


# --------------------------------------------------------------------------- harness


def _build_l_frame(profile: str = "square_tube", params: dict | None = None):
    """Compile a 2-member L-frame (e1 along +X to the corner, e2 up +Y) in one context.

    Returns ``(ctx, member_output)`` with ``ctx.bodies`` already pointing at the member bodies, so a
    dress feature driven next sees them exactly as the real compiler would thread them forward."""
    p = {"size": 20.0, "thickness": 2.0, "trim_corners": True}
    p.update(params or {})
    sk = TW._sketch("f1", [
        TW._line("e1", [0.0, 0.0], [100.0, 0.0]),
        TW._line("e2", [100.0, 0.0], [100.0, 80.0]),
    ])
    wm = TW._weld("f2", "f1", profile, params=p)
    ctx = CompileContext(equations_env={}, bodies=[], datums=reserved_datums())
    get_compiler("sketch")(sk, ctx)
    out = get_compiler("weld_member")(wm, ctx)
    ctx.bodies = list(out.bodies)
    return ctx, out


def _build_single_member(profile: str = "square_tube", params: dict | None = None):
    """Compile a single straight member along +X (open ends at x=0 and x=100)."""
    p = {"trim_corners": False}
    p.update(params or {})
    sk = TW._sketch("f1", [TW._line("e1", [0.0, 0.0], [100.0, 0.0])])
    wm = TW._weld("f2", "f1", profile, params=p)
    ctx = CompileContext(equations_env={}, bodies=[], datums=reserved_datums())
    get_compiler("sketch")(sk, ctx)
    out = get_compiler("weld_member")(wm, ctx)
    ctx.bodies = list(out.bodies)
    return ctx, out


def _top_face_sel(body) -> dict:
    """Selector for the body's largest planar +Z (top) face — a clean coplanar gusset pick."""
    best = None
    for info in describe_faces(body.wrapped):
        if info.surf == "plane" and info.axis is not None and abs(info.axis[2] - 1.0) < 1e-3:
            if best is None or (info.measure or 0) > (best.measure or 0):
                best = info
    assert best is not None, "no +Z top face found"
    return make_selector(best)


def _end_face_sel(body, x_target: float = 0.0) -> dict:
    """Selector for the member END face nearest ``x_target`` (the open tube end)."""
    best = None
    best_d = 1e18
    for info in describe_faces(body.wrapped):
        if info.surf != "plane":
            continue
        d = abs(info.centroid[0] - x_target)
        # End face is the small cross-section face (≈144 mm² for a 20×20 tube), not a long side.
        if d < best_d and (info.measure or 1e9) < 500.0:
            best_d = d
            best = info
    assert best is not None, "no end face found"
    return make_selector(best)


def _gusset(fid: str, sel_a: dict, sel_b: dict, **params) -> dict:
    return T._feat(fid, "weld_gusset", dict(params), refs={"faces": [sel_a, sel_b]})


def _end_cap(fid: str, sel: dict, **params) -> dict:
    return T._feat(fid, "weld_end_cap", dict(params), refs={"face": sel})


# --------------------------------------------------------------------------- gusset geometry


def test_gusset_triangular_volume_and_touch() -> None:
    """A triangular gusset on an L-corner → plate of vol ≈ ½·d1·d2·thickness that touches both
    members, with the requested thickness."""
    ctx, out = _build_l_frame()
    s0 = _top_face_sel(out.bodies[0])
    s1 = _top_face_sel(out.bodies[1])
    g = _gusset("f3", s0, s1, thickness=5.0, d1=30.0, d2=30.0, type="triangular")
    gout = get_compiler("weld_gusset")(g, ctx)

    plate = gout.bodies[-1]
    # A new SEPARATE body was appended (2 members + 1 gusset).
    assert len(gout.bodies) == 3
    expected = 0.5 * 30.0 * 30.0 * 5.0  # ½·d1·d2·thickness
    assert abs(plate.volume - expected) < 1.0, f"gusset vol {plate.volume} != {expected}"
    assert abs(gout.resolved["_triangle_area"] - 0.5 * 30.0 * 30.0) < 1e-6

    # Thickness: the bbox extent along the gusset normal (+Z here) ≈ thickness.
    bb = plate.bounding_box()
    assert abs((bb.max.Z - bb.min.Z) - 5.0) < 1e-6, "gusset is not `thickness` thick"

    # Touches BOTH members (non-zero intersection volume with each).
    for bi in (0, 1):
        iv = getattr(plate & out.bodies[bi], "volume", 0.0)
        assert iv > 1.0, f"gusset does not touch member {bi} (overlap {iv})"


def test_gusset_legs_run_along_members() -> None:
    """The gusset's two legs run d1 / d2 along the two member axes from the shared corner."""
    ctx, out = _build_l_frame()
    s0 = _top_face_sel(out.bodies[0])
    s1 = _top_face_sel(out.bodies[1])
    gout = get_compiler("weld_gusset")(
        _gusset("f3", s0, s1, thickness=5.0, d1=25.0, d2=40.0), ctx)
    bb = gout.bodies[-1].bounding_box()
    # Member 0 runs along X, member 1 along Y; the corner is at x≈100, y≈0. Leg along -X spans 25,
    # leg along +Y spans 40.
    assert abs((bb.max.X - bb.min.X) - 25.0) < 0.5, f"X-leg span {bb.max.X - bb.min.X} != 25"
    assert abs((bb.max.Y - bb.min.Y) - 40.0) < 0.5, f"Y-leg span {bb.max.Y - bb.min.Y} != 40"


def test_gusset_polygonal_smaller_than_triangular() -> None:
    """A polygonal (cut-corner) gusset removes the outer tip → less volume than the full triangle."""
    ctx, out = _build_l_frame()
    s0 = _top_face_sel(out.bodies[0])
    s1 = _top_face_sel(out.bodies[1])
    tri = get_compiler("weld_gusset")(
        _gusset("f3", s0, s1, thickness=5.0, d1=30.0, d2=30.0, type="triangular"), ctx)
    poly = get_compiler("weld_gusset")(
        _gusset("f4", s0, s1, thickness=5.0, d1=30.0, d2=30.0, type="polygonal"), ctx)
    assert poly.bodies[-1].volume < tri.bodies[-1].volume
    assert poly.bodies[-1].volume > 0.0


def test_gusset_location_offsets_plate() -> None:
    """`location` shifts the plate off the shared plane by ±half thickness vs centred."""
    ctx, out = _build_l_frame()
    s0 = _top_face_sel(out.bodies[0])
    s1 = _top_face_sel(out.bodies[1])
    centered = get_compiler("weld_gusset")(
        _gusset("f3", s0, s1, thickness=5.0, d1=30.0, d2=30.0, location="centered"), ctx)
    inner = get_compiler("weld_gusset")(
        _gusset("f4", s0, s1, thickness=5.0, d1=30.0, d2=30.0, location="inner"), ctx)
    zc = centered.bodies[-1].bounding_box()
    zi = inner.bodies[-1].bounding_box()
    # 'inner' sinks the plate by half thickness toward the body (lower Z centre).
    cen_mid = (zc.min.Z + zc.max.Z) / 2.0
    inn_mid = (zi.min.Z + zi.max.Z) / 2.0
    assert inn_mid < cen_mid - 1e-6, "location='inner' should lower the plate"


# --------------------------------------------------------------------------- gusset errors


def test_gusset_requires_two_faces() -> None:
    ctx, out = _build_l_frame()
    s0 = _top_face_sel(out.bodies[0])
    g = T._feat("f3", "weld_gusset", {"thickness": 5.0}, refs={"faces": [s0]})
    try:
        get_compiler("weld_gusset")(g, ctx)
        assert False, "one-face gusset should fail"
    except Exception as exc:  # noqa: BLE001
        assert "exactly two" in str(exc)


def test_gusset_parallel_members_rejected() -> None:
    """Two faces whose member axes are parallel/collinear are not an L-corner → clean error."""
    ctx, out = _build_single_member()  # one member → both picks land on the same axis
    s0 = _top_face_sel(out.bodies[0])
    # Pick the same top face twice (parallel axes).
    g = _gusset("f3", s0, s0, thickness=5.0, d1=30.0, d2=30.0)
    try:
        get_compiler("weld_gusset")(g, ctx)
        assert False, "parallel-member gusset should fail"
    except Exception as exc:  # noqa: BLE001
        assert "parallel" in str(exc) or "collinear" in str(exc)


def test_gusset_bad_thickness_rejected() -> None:
    ctx, out = _build_l_frame()
    s0 = _top_face_sel(out.bodies[0])
    s1 = _top_face_sel(out.bodies[1])
    try:
        get_compiler("weld_gusset")(
            _gusset("f3", s0, s1, thickness=0.0, d1=30.0, d2=30.0), ctx)
        assert False, "zero thickness should fail"
    except Exception as exc:  # noqa: BLE001
        assert "thickness" in str(exc)


def test_gusset_dangling_face_rejected() -> None:
    """A face selector that resolves on no member body is a contained dangling error."""
    ctx, out = _build_l_frame()
    s0 = _top_face_sel(out.bodies[0])
    bogus = {"kind": "face", "surf": "plane",
             "centroid": [9999.0, 9999.0, 9999.0], "axis": [0, 0, 1],
             "area_or_len": 1.0, "owner_feature": None, "hint_index": 0}
    try:
        get_compiler("weld_gusset")(_gusset("f3", s0, bogus, thickness=5.0), ctx)
        assert False, "dangling face gusset should fail"
    except Exception as exc:  # noqa: BLE001
        assert "resolve" in str(exc) or "dangl" in str(exc).lower()


# --------------------------------------------------------------------------- end cap geometry


def test_end_cap_outer_covers_tube_end() -> None:
    """An outer end cap on a 20×20 square tube → a plate covering the whole end (area ≈ 20×20),
    `thickness` thick, mounted on the end face."""
    ctx, out = _build_single_member()
    sel = _end_face_sel(out.bodies[0], x_target=0.0)
    ec = _end_cap("f3", sel, thickness=5.0, type="outer")
    ecout = get_compiler("weld_end_cap")(ec, ctx)

    cap = ecout.bodies[-1]
    assert len(ecout.bodies) == 2  # member + cap (separate body)
    assert abs(cap.volume - 20.0 * 20.0 * 5.0) < 1.0, f"outer cap vol {cap.volume} != 2000"
    assert abs(ecout.resolved["_cap_area"] - 400.0) < 1e-3
    bb = cap.bounding_box()
    # The cap covers the full 20×20 end envelope.
    assert abs((bb.max.Y - bb.min.Y) - 20.0) < 1e-3
    assert abs((bb.max.Z - bb.min.Z) - 20.0) < 1e-3
    # Mounted ON the end (x ∈ [-5, 0], extruding outward from the x=0 end).
    assert abs((bb.max.X - bb.min.X) - 5.0) < 1e-6


def test_end_cap_outer_overhang_grows_plate() -> None:
    """`overhang` grows the cap past the tube profile on every side."""
    ctx, out = _build_single_member()
    sel = _end_face_sel(out.bodies[0], x_target=0.0)
    base = get_compiler("weld_end_cap")(_end_cap("f3", sel, thickness=5.0, type="outer"), ctx)
    grown = get_compiler("weld_end_cap")(
        _end_cap("f4", sel, thickness=5.0, type="outer", overhang=3.0), ctx)
    assert grown.bodies[-1].volume > base.bodies[-1].volume
    bb = grown.bodies[-1].bounding_box()
    # 20 + 2·3 = 26 mm across (corners may be slightly rounded by the offset).
    assert (bb.max.Y - bb.min.Y) > 24.0


def test_end_cap_inner_insets_into_bore() -> None:
    """An inner end cap plugs the bore (≈16×16 for a 20×20 tube with 2 mm wall) and insets INTO the
    tube, so it is smaller than the outer-envelope cap."""
    ctx, out = _build_single_member()
    sel = _end_face_sel(out.bodies[0], x_target=0.0)
    inner = get_compiler("weld_end_cap")(_end_cap("f3", sel, thickness=5.0, type="inner"), ctx)
    cap = inner.bodies[-1]
    # Bore is 16×16 → 16·16·5 = 1280.
    assert abs(cap.volume - 16.0 * 16.0 * 5.0) < 2.0, f"inner cap vol {cap.volume} != 1280"
    bb = cap.bounding_box()
    assert (bb.max.Y - bb.min.Y) < 20.0, "inner cap should be narrower than the tube"
    # Insets INTO the tube (x ∈ [0, 5], i.e. inward of the x=0 end).
    assert bb.min.X >= -1e-6 and bb.max.X <= 5.0 + 1e-6


def test_end_cap_round_tube() -> None:
    """An outer cap on a round tube produces a disc-ish plate covering the OD."""
    ctx, out = _build_single_member("round_tube", params={"od": 30.0, "thickness": 3.0})
    sel = _end_face_sel(out.bodies[0], x_target=0.0)
    ec = get_compiler("weld_end_cap")(_end_cap("f3", sel, thickness=4.0, type="outer"), ctx)
    cap = ec.bodies[-1]
    # Disc of OD 30 × thickness 4 ≈ π·15²·4.
    expected = math.pi * 15.0 * 15.0 * 4.0
    assert abs(cap.volume - expected) < 0.05 * expected, f"round cap vol {cap.volume} != {expected}"


# --------------------------------------------------------------------------- end cap errors


def test_end_cap_inner_on_solid_section_rejected() -> None:
    """A solid flat bar has no bore → an inner cap is a clean error."""
    ctx, out = _build_single_member("flat_bar", params={"w": 40.0, "thickness": 6.0})
    sel = _end_face_sel(out.bodies[0], x_target=0.0)
    try:
        get_compiler("weld_end_cap")(_end_cap("f3", sel, thickness=5.0, type="inner"), ctx)
        assert False, "inner cap on a solid section should fail"
    except Exception as exc:  # noqa: BLE001
        assert "hollow" in str(exc) or "inner" in str(exc)


def test_end_cap_missing_face_rejected() -> None:
    ctx, out = _build_single_member()
    ec = T._feat("f3", "weld_end_cap", {"thickness": 5.0}, refs={})
    try:
        get_compiler("weld_end_cap")(ec, ctx)
        assert False, "missing face should fail"
    except Exception as exc:  # noqa: BLE001
        assert "face is required" in str(exc)


def test_end_cap_dangling_face_rejected() -> None:
    ctx, out = _build_single_member()
    bogus = {"kind": "face", "surf": "plane",
             "centroid": [9999.0, 9999.0, 9999.0], "axis": [1, 0, 0],
             "area_or_len": 1.0, "owner_feature": None, "hint_index": 0}
    try:
        get_compiler("weld_end_cap")(_end_cap("f3", bogus, thickness=5.0), ctx)
        assert False, "dangling face should fail"
    except Exception as exc:  # noqa: BLE001
        assert "resolve" in str(exc) or "dangl" in str(exc).lower()


def test_end_cap_bad_type_rejected() -> None:
    ctx, out = _build_single_member()
    sel = _end_face_sel(out.bodies[0], x_target=0.0)
    try:
        get_compiler("weld_end_cap")(_end_cap("f3", sel, thickness=5.0, type="sideways"), ctx)
        assert False, "bad type should fail"
    except Exception as exc:  # noqa: BLE001
        assert "type must be" in str(exc)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"[weld_dress] {name} ok")
    print("[weld_dress] all tests passed")
