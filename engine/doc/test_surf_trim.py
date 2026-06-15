r"""Surfacing — trim / untrim surface features (``surf_trim`` / ``surf_untrim``).

Run:  & $py -X utf8 -m pytest engine\doc\test_surf_trim.py -q   (from repo root)

``surf_trim`` cuts a SURFACE body with a tool (another face/surface) and keeps the picked side(s)
per ``params.keep`` (positive | negative | both); the result REPLACES the input surface body and
stays ``kind="surface"`` (volume ≈ 0). ``surf_untrim`` regrows a trimmed face back out to the full
natural domain of its underlying surface, so its area grows back.

Covers:
1.  Trim a 40x40 planar surface patch (area 1600) with a planar box face that crosses it at x=20:
    keep='both' keeps the whole patch (1600); the result is still a surface body (vol ≈ 0).
2.  keep='positive' / keep='negative' each keep exactly ONE half (area 800 — strictly LESS than the
    untrimmed 1600), and the two halves are complementary (re-sum to the whole).
3.  Trim a curved surf_extrude CYLINDER wall (area 1885) with a horizontal plane at mid-height:
    keep='positive' halves the wall (≈ 942.5), proving the surface-split path works on a curved
    body exactly as on a planar one.
4.  surf_untrim regrows a TRIMMED half-patch face: its area grows back (larger than the trimmed
    800), staying a surface body.
5.  Error containment: trim with no body, a non-intersecting tool, an unknown keep, and untrim with
    no faces all fail cleanly (ok:false, contained) without crashing the rebuild.

Harness mirrors engine/doc/test_extrude_ends.py and engine/doc/test_surf_extrude.py.
"""

from __future__ import annotations

import math

# Import the feature modules so their @register decorators run. surf_trim is imported ONLY here.
from engine.doc.features import surf_trim  # noqa: F401  (registers surf_trim + surf_untrim)
from engine.doc.features import surf_planar  # noqa: F401  (builds the planar test surface)
from engine.doc.features import surf_extrude  # noqa: F401  (builds the curved test surface)
from engine.doc.features import extrude  # noqa: F401  (shared profile resolvers)

from engine.doc.compiler import compile_document
from engine.doc.evaluate import mass_props
from engine.doc.schema import validate_part
from engine.doc.selectors import describe_faces, make_selector


# --------------------------------------------------------------------------- harness

def _dim(value: float, expr: str | None = None) -> dict:
    return {"value": value, "expr": expr}


def _feat(fid: str, ftype: str, params: dict, refs: dict | None = None,
          name: str | None = None) -> dict:
    return {"id": fid, "type": ftype, "name": name or fid,
            "suppressed": False, "params": params, "refs": refs or {}}


def _doc(features: list[dict]) -> dict:
    return {
        "schema": "aicad.part/1",
        "id": "test-surf-trim",
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


def _rect_sketch(fid: str, x0: float, y0: float, w: float, h: float, plane_ref) -> dict:
    return _feat(fid, "sketch", {
        "entities": [
            {"id": "e1", "type": "line", "construction": False,
             "p1": [x0, y0], "p2": [x0 + w, y0]},
            {"id": "e2", "type": "line", "construction": False,
             "p1": [x0 + w, y0], "p2": [x0 + w, y0 + h]},
            {"id": "e3", "type": "line", "construction": False,
             "p1": [x0 + w, y0 + h], "p2": [x0, y0 + h]},
            {"id": "e4", "type": "line", "construction": False,
             "p1": [x0, y0 + h], "p2": [x0, y0]},
        ],
        "constraints": [],
    }, refs={"plane": plane_ref})


def _circle_sketch(fid: str, cx: float, cy: float, r: float, plane_ref) -> dict:
    return _feat(fid, "sketch", {
        "entities": [{"id": "e1", "type": "circle", "construction": False,
                      "center": [cx, cy], "radius": r}],
        "constraints": [],
    }, refs={"plane": plane_ref})


def _region_sel(sketch_id: str, cx: float, cy: float, area: float) -> dict:
    return {"kind": "region", "sketch": sketch_id, "centroid": [cx, cy],
            "area_or_len": area, "hint_index": 0}


def _body_sel(owner: str) -> dict:
    """A BODY selector naming the surface body produced by feature *owner*."""
    return {"kind": "body", "owner_feature": owner}


def _point(fid: str, x: float, y: float, z: float) -> dict:
    return _feat(fid, "point", {"method": "coordinate",
                                "x": _dim(x), "y": _dim(y), "z": _dim(z)})


def _planar_face_sel(cx: float, cy: float, cz: float, area: float, owner: str,
                     axis: list | None = None) -> dict:
    """A planar-face selector (for untrim picks)."""
    return {"kind": "face", "surf": "plane", "centroid": [cx, cy, cz],
            "axis": axis or [0.0, 0.0, 1.0], "area_or_len": area,
            "owner_feature": owner, "hint_index": 0}


def _tool_face_selector(base_feats: list[dict], owner_fid: str,
                        pick) -> dict:
    """Compile *base_feats*, find the face on body *owner_fid* matching *pick(info)*, and return a
    validated face selector for it (owner_feature = owner_fid). Mirrors test_split_ext.py."""
    res = compile_document(_doc(base_feats))
    tool_body = res.bodies[-1].shape  # the tool body (built merge=False) is appended LAST
    infos = describe_faces(tool_body.wrapped)
    matches = [i for i in infos if pick(i)]
    assert matches, f"no tool face matched on body {owner_fid}"
    sel = make_selector(matches[0])
    sel["owner_feature"] = owner_fid
    return sel


def _surface_area(res) -> float:
    """Area of the (top) surface body in *res*."""
    sb = res.bodies[-1]
    return mass_props(sb.shape)["area_mm2"]


# --------------------------------------------------------------------------- fixtures

def _planar_patch_base() -> tuple[list[dict], dict]:
    """A 40x40 planar surface patch (area 1600) on plane.top + a box whose -X face is a vertical
    plane at x=20 that crosses the patch interior. Returns (base_features, tool_face_selector)."""
    base = [
        _rect_sketch("f1", 0, 0, 40, 40, "plane.top"),
        _feat("f2", "surf_planar", {},
              refs={"profile": [_region_sel("f1", 20, 20, 1600.0)]}),
        _point("f3", 40, 20, 0),
        _feat("f4", "box", {"length": _dim(40), "width": _dim(80), "height": _dim(20),
                            "centered": True, "merge": False}, refs={"at": "f3"}),
    ]
    # The box spans x[20,60]; its -X face (a vertical plane at x=20, X-aligned normal) crosses the
    # patch interior at x=20, dividing the 40x40 patch into two 20x40 halves.
    sel = _tool_face_selector(
        base, "f4",
        pick=lambda i: i.surf == "plane" and abs(i.centroid[0] - 20.0) < 0.5
        and abs(abs(i.axis[0]) - 1.0) < 0.1,
    )
    return base, sel


# --------------------------------------------------------------------------- test 1: keep both

def test_surf_trim_keep_both_keeps_whole_patch() -> None:
    """Trim a 40x40 planar surface (area 1600) with a planar tool crossing it at x=20.
    keep='both' sub-divides the patch but keeps every piece → area is still 1600; the result is a
    surface body (kind=='surface', volume ≈ 0), NOT a new body (trim replaces in place)."""
    base, sel = _planar_patch_base()
    doc = _doc(base + [
        _feat("f5", "surf_trim", {"keep": "both"},
              refs={"surface": _body_sel("f2"), "tool": sel}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f5")
    assert s["ok"], f"surf_trim keep=both failed: {s['error']}"

    sb = res.bodies[-1]
    assert sb.kind == "surface", f"trim result must stay a surface body; got {sb.kind!r}"
    mp = mass_props(sb.shape)
    assert mp.get("is_surface") is True, f"mass_props should flag is_surface; got {mp}"
    assert abs(mp["volume_mm3"]) < 1e-6, f"surface volume should be ≈0; got {mp['volume_mm3']}"
    assert abs(mp["area_mm2"] - 1600.0) / 1600.0 < 0.01, \
        f"keep=both area {mp['area_mm2']:.1f} should equal the untrimmed patch (1600)"
    # The tool split exactly one face into two pieces, both kept.
    assert abs(s["resolved"].get("_faces_split", 0.0) - 1.0) < 1e-9
    assert abs(s["resolved"].get("_faces_kept", 0.0) - 2.0) < 1e-9
    # Trim REPLACES the surface body — no extra body is added (only the trimmed patch + tool box).
    assert len(res.bodies) == 2, \
        f"trim must not add a body (patch + tool box = 2); got {len(res.bodies)} bodies"
    print(f"[trim-both] kind={sb.kind} area={mp['area_mm2']:.1f} (whole patch 1600)")


# --------------------------------------------------------------------------- test 2: keep sides

def test_surf_trim_keep_side_halves_area() -> None:
    """keep='positive' / keep='negative' each keep exactly ONE half of the split patch (area 800 —
    strictly LESS than the untrimmed 1600), and the two halves are complementary (re-sum to 1600).
    This is the defining trim property: a kept side has reduced area vs the untrimmed surface."""
    base, sel = _planar_patch_base()

    def _run(keep: str):
        doc = _doc(base + [
            _feat("f5", "surf_trim", {"keep": keep},
                  refs={"surface": _body_sel("f2"), "tool": sel}),
        ])
        _assert_valid(doc)
        res = compile_document(doc)
        s = _status(res, "f5")
        assert s["ok"], f"surf_trim keep={keep} failed: {s['error']}"
        sb = res.bodies[-1]
        assert sb.kind == "surface", f"keep={keep} must stay a surface body"
        mp = mass_props(sb.shape)
        assert abs(mp["volume_mm3"]) < 1e-6, "kept surface volume should be ≈0"
        return s, mp["area_mm2"]

    s_pos, area_pos = _run("positive")
    s_neg, area_neg = _run("negative")

    # Each side is a single half: 20x40 = 800, strictly LESS than the untrimmed 1600.
    for label, area in (("positive", area_pos), ("negative", area_neg)):
        assert abs(area - 800.0) / 800.0 < 0.01, \
            f"keep={label} area {area:.1f} should be one 20x40 half (800)"
        assert area < 1600.0 - 1e-6, \
            f"a kept side ({area:.1f}) must have LESS area than the untrimmed patch (1600)"
    # Exactly one piece kept per side.
    assert abs(s_pos["resolved"].get("_faces_kept", 0.0) - 1.0) < 1e-9
    assert abs(s_neg["resolved"].get("_faces_kept", 0.0) - 1.0) < 1e-9
    # The two complementary halves re-sum to the whole patch.
    assert abs((area_pos + area_neg) - 1600.0) / 1600.0 < 0.01, \
        f"positive+negative ({area_pos:.1f}+{area_neg:.1f}) must re-sum to the whole 1600"
    print(f"[trim-side] positive={area_pos:.1f} negative={area_neg:.1f} "
          f"sum={area_pos + area_neg:.1f} (whole 1600)")


# --------------------------------------------------------------------------- test 3: curved wall

def test_surf_trim_curved_cylinder_wall() -> None:
    """Trim a CURVED surface — a surf_extrude cylinder wall (r=10, depth=30, area 2*pi*10*30 ≈
    1885) — with a horizontal plane at mid-height (z=15). keep='positive' keeps one ring half
    (≈ 942.5, strictly less than the full wall), proving the surface-split path works on a curved
    body exactly as on the planar patch."""
    r, depth = 10.0, 30.0
    full_area = 2.0 * math.pi * r * depth
    base = [
        _circle_sketch("f1", 0, 0, r, "plane.top"),
        _feat("f2", "surf_extrude", {"end": "blind", "depth": _dim(depth)},
              refs={"profile": [_region_sel("f1", 0, 0, math.pi * r * r)]}),
        # A box whose bottom face is a horizontal plane at z=15 (mid-height) crossing the wall.
        _point("f3", 0, 0, 35),
        _feat("f4", "box", {"length": _dim(60), "width": _dim(60), "height": _dim(40),
                            "centered": True, "merge": False}, refs={"at": "f3"}),
    ]
    sel = _tool_face_selector(
        base, "f4",
        pick=lambda i: i.surf == "plane" and abs(i.centroid[2] - 15.0) < 0.5
        and abs(abs(i.axis[2]) - 1.0) < 0.1,
    )
    doc = _doc(base + [
        _feat("f5", "surf_trim", {"keep": "positive"},
              refs={"surface": _body_sel("f2"), "tool": sel}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f5")
    assert s["ok"], f"surf_trim curved wall failed: {s['error']}"
    sb = res.bodies[-1]
    assert sb.kind == "surface"
    mp = mass_props(sb.shape)
    assert abs(mp["volume_mm3"]) < 1e-6
    half = full_area / 2.0
    assert abs(mp["area_mm2"] - half) / half < 0.02, \
        f"trimmed cylinder-wall half {mp['area_mm2']:.1f} vs expected {half:.1f} (half the wall)"
    assert mp["area_mm2"] < full_area - 1e-6, \
        f"kept half ({mp['area_mm2']:.1f}) must be less than the full wall ({full_area:.1f})"
    print(f"[trim-curved] half area={mp['area_mm2']:.1f} (full wall {full_area:.1f})")


# --------------------------------------------------------------------------- test 4: untrim

def test_surf_untrim_regrows_trimmed_face() -> None:
    """surf_untrim regrows a TRIMMED face back to the full natural domain of its base surface, so
    its area GROWS back. We trim the 40x40 patch (keep one 800-area half), then untrim that half:
    the rebuilt face is strictly LARGER than the trimmed 800, still a surface body (vol ≈ 0)."""
    base, sel = _planar_patch_base()

    # Step 1 — trim to one half (area 800). keep='negative' keeps the x[20,40] half (centroid x=30).
    trim_feat = _feat("f5", "surf_trim", {"keep": "negative"},
                      refs={"surface": _body_sel("f2"), "tool": sel})
    res_trim = compile_document(_doc(base + [trim_feat]))
    s_trim = _status(res_trim, "f5")
    assert s_trim["ok"], f"setup trim failed: {s_trim['error']}"
    trimmed_area = _surface_area(res_trim)
    assert abs(trimmed_area - 800.0) / 800.0 < 0.01, \
        f"trimmed half should be 800; got {trimmed_area:.1f}"
    # The kept half-patch is a single planar face; locate its centroid for the untrim pick.
    tinfos = describe_faces(res_trim.bodies[-1].shape.wrapped)
    assert len(tinfos) == 1, f"trimmed half should be one face; got {len(tinfos)}"
    tc = tinfos[0].centroid

    # Step 2 — untrim that face: it grows back to the full (capped) natural plane patch.
    doc = _doc(base + [trim_feat, _feat("f6", "surf_untrim", {}, refs={
        "faces": [_planar_face_sel(tc[0], tc[1], tc[2], trimmed_area, "f5")]})])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f6")
    assert s["ok"], f"surf_untrim failed: {s['error']}"

    sb = res.bodies[-1]
    assert sb.kind == "surface", f"untrim result must stay a surface body; got {sb.kind!r}"
    mp = mass_props(sb.shape)
    assert mp.get("is_surface") is True
    assert abs(mp["volume_mm3"]) < 1e-6, "untrimmed surface volume should be ≈0"
    # The defining untrim property: the face's area GROWS back (larger than the trimmed 800).
    assert mp["area_mm2"] > trimmed_area + 1e-6, \
        f"untrim must GROW area: {mp['area_mm2']:.1f} should exceed the trimmed {trimmed_area:.1f}"
    assert abs(s["resolved"].get("_faces_untrimmed", 0.0) - 1.0) < 1e-9
    assert s["resolved"].get("_grew_area", 0.0) == 1.0, "untrim should report a grown face"
    print(f"[untrim] trimmed={trimmed_area:.1f} -> untrimmed={mp['area_mm2']:.1f} (grows back)")


# --------------------------------------------------------------------------- test 5: errors

def test_surf_trim_untrim_errors_contained() -> None:
    """Contained-error discipline: every bad input fails ok:false without crashing the rebuild."""
    # (a) trim with no body at all.
    doc_a = _doc([
        _feat("f1", "surf_trim", {"keep": "both"},
              refs={"surface": _body_sel("nope"),
                    "tool": _planar_face_sel(0, 0, 0, 1.0, "nope")}),
    ])
    _assert_valid(doc_a)
    res_a = compile_document(doc_a)
    s_a = _status(res_a, "f1")
    assert not s_a["ok"], "trim with no body should fail"
    assert "no body" in s_a["error"].lower(), f"got: {s_a['error']}"

    # (b) unknown keep value.
    base, sel = _planar_patch_base()
    doc_b = _doc(base + [
        _feat("f5", "surf_trim", {"keep": "sideways"},
              refs={"surface": _body_sel("f2"), "tool": sel}),
    ])
    _assert_valid(doc_b)
    res_b = compile_document(doc_b)
    s_b = _status(res_b, "f5")
    assert not s_b["ok"], "unknown keep should fail"
    assert "keep" in s_b["error"].lower(), f"got: {s_b['error']}"

    # (c) a tool that does not cross the surface divides nothing → contained error.
    far_base = [
        _rect_sketch("f1", 0, 0, 40, 40, "plane.top"),
        _feat("f2", "surf_planar", {},
              refs={"profile": [_region_sel("f1", 20, 20, 1600.0)]}),
        _point("f3", 200, 20, 0),  # box parked far away in +X
        _feat("f4", "box", {"length": _dim(40), "width": _dim(80), "height": _dim(20),
                            "centered": True, "merge": False}, refs={"at": "f3"}),
    ]
    far_sel = _tool_face_selector(
        far_base, "f4",
        pick=lambda i: i.surf == "plane" and abs(i.centroid[0] - 180.0) < 0.5
        and abs(abs(i.axis[0]) - 1.0) < 0.1,
    )
    doc_c = _doc(far_base + [
        _feat("f5", "surf_trim", {"keep": "both"},
              refs={"surface": _body_sel("f2"), "tool": far_sel}),
    ])
    _assert_valid(doc_c)
    res_c = compile_document(doc_c)
    s_c = _status(res_c, "f5")
    assert not s_c["ok"], "a non-intersecting tool must error"
    assert "intersect" in s_c["error"].lower() or "divide" in s_c["error"].lower(), s_c["error"]

    # (d) untrim with a dangling face selector (a valid non-empty list, but the face resolves to
    # nothing on the surface body) → contained dangling error, never a crash. (An empty faces list
    # is rejected at schema-validation time, so the runtime miss is the dangling pick here.)
    doc_d = _doc(base + [_feat("f5", "surf_untrim", {}, refs={
        "faces": [_planar_face_sel(9999, 9999, 9999, 1.0, "f2")]})])
    _assert_valid(doc_d)
    res_d = compile_document(doc_d)
    s_d = _status(res_d, "f5")
    assert not s_d["ok"], "untrim with a dangling face should fail"
    assert "dangling" in s_d["error"].lower() or "faces" in s_d["error"].lower(), \
        f"got: {s_d['error']}"
    print("[trim-errors] no-body, bad-keep, non-intersecting, untrim-dangling all clean")


# --------------------------------------------------------------------------- main

def main() -> None:
    test_surf_trim_keep_both_keeps_whole_patch()
    test_surf_trim_keep_side_halves_area()
    test_surf_trim_curved_cylinder_wall()
    test_surf_untrim_regrows_trimmed_face()
    test_surf_trim_untrim_errors_contained()
    print("\nSURF-TRIM TESTS PASSED")


if __name__ == "__main__":
    main()
