r"""SW-parity extensions to the dress-up compilers (fillet / chamfer / shell).

Run:  & $py -m pytest engine\doc\test_dress_ext.py -q   (from repo root)

Pytest-style (also importable as plain functions). Mirrors the harness in
engine\doc\test_extrude_ends.py (a thin _doc / _feat / compile_document wrapper),
plus a two-pass selector-extraction helper: dress-up features need *real* edge /
face / vertex selectors, so we compile the base body first, describe its topology,
build a make_selector ref against the entity we want, then re-compile with the
dress feature added. That is exactly how the live pipeline mints selectors.

Covers (docs/SW_PARITY_SPEC.md §2 + docs/SW_PARITY_GAPS.md):

FILLET
 1. Face fillet blends the edge chain between two face sets (volume drops vs plain box;
    matches the equivalent single-edge fillet).
 2. Conic profile (rho != 0.5) differs from the circular fillet at the same radius, and
    rho is monotonic (leaner rho removes more, fuller rho less).
 3. Tangent propagation auto-fillets a tangent edge chain — more rounded edges than the
    lone seed (a cylinder's top circle continues onto its tangent neighbours; here the
    discriminating geometry is a tangent fillet-band rib).

CHAMFER
 4. Distance-angle chamfer removes the analytic distance×angle wedge (differs from the
    symmetric distance×distance chamfer).
 5. Vertex chamfer planes off a 3-edge corner (corner-triangle removed).
 6. Tangent propagation grows the chamfer to the tangent chain.

SHELL
 7. Multi-thickness shell: one wall thicker than the base -> interior cavity smaller
    than a uniform shell, and the per-face wall thickness is actually larger.
"""

from __future__ import annotations

import math

from engine.doc.compiler import compile_document
from engine.doc.schema import validate_part
from engine.doc import selectors as sel


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
        "id": "test-doc",
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


def _box(fid: str, l: float, w: float, h: float, at: str = "origin",
         merge: bool | None = None) -> dict:
    params = {"length": _dim(l), "width": _dim(w), "height": _dim(h), "centered": False}
    if merge is not None:
        params["merge"] = merge
    return _feat(fid, "box", params, refs={"at": at})


def _compile_ok(doc: dict, *fids: str, validate: bool = True):
    """Compile *doc*, assert the named features all succeeded, return the result.

    ``validate=False`` skips the schema pre-check. The SW-parity additions introduce new
    ref slots (face_set_a/face_set_b, vertices, reference_face, thickness_overrides) that
    ``engine/doc/schema.py`` does not yet register — registering them is the one
    unavoidable shared-file change this work needs (noted in the report / the schema is
    off-limits to this compiler-only task). The compiler itself handles them; these tests
    bypass the validator for features that use the not-yet-registered slots.
    """
    if validate:
        _assert_valid(doc)
    res = compile_document(doc)
    for fid in fids:
        s = _status(res, fid)
        assert s["ok"], f"{fid} failed: {s['error']}"
    return res


def _body_of(res, *prefix_fids):
    """The first body of a compile result (single-body lineage)."""
    return res.bodies[0]


# -- two-pass selector extraction ------------------------------------------------

def _edge_selector(res, *, surf: str | None = None, key=None):
    """Build a make_selector edge ref against a described edge of res.bodies[0].

    *key* (callable info->float) chooses which edge (max key); *surf* filters by curve
    type ("line" / "circle"). Defaults to the first edge.
    """
    infos = sel.describe_edges(res.bodies[0].shape.wrapped)
    cands = [i for i in infos if (surf is None or i.surf == surf)]
    assert cands, f"no edge with surf={surf}"
    pick = max(cands, key=key) if key is not None else cands[0]
    return sel.make_selector(pick)


def _face_selector(res, *, key):
    infos = sel.describe_faces(res.bodies[0].shape.wrapped)
    pick = max(infos, key=key)
    return sel.make_selector(pick)


def _vertex_selector(res, *, key):
    infos = sel.describe_vertices(res.bodies[0].shape.wrapped)
    pick = max(infos, key=key)
    return sel.make_selector(pick)


# =========================================================================== FILLET


def test_face_fillet_blends_between_two_faces() -> None:
    """Face fillet between the TOP (+Z) and FRONT (-Y) faces of a 20-cube blends the
    edge separating them. Result volume < plain box and equals the same single-edge
    circular fillet (the rolling ball touches both faces along that edge)."""
    base = _box("f1", 20, 20, 20)
    res0 = _compile_ok(_doc([base]), "f1")

    # face_set_a = top (+Z, max area-weighted Z), face_set_b = front (-Y).
    top_sel = _face_selector(res0, key=lambda i: i.centroid[2])
    front_sel = _face_selector(res0, key=lambda i: -i.centroid[1])

    ff = _feat("f2", "fillet", {"face_fillet": True, "radius": _dim(4.0)},
               refs={"face_set_a": [top_sel], "face_set_b": [front_sel]})
    res = _compile_ok(_doc([base, ff]), "f1", "f2", validate=False)
    vol_ff = res.bodies[0].shape.volume
    assert vol_ff < 8000.0 - 1.0, f"face fillet should remove material; vol={vol_ff:.2f}"

    # Reference: filleting the single shared edge (top&front, at y=-10,z=+10) directly.
    edge_sel = _edge_selector(
        res0, surf="line",
        key=lambda i: i.centroid[2] - i.centroid[1])  # max (z up, y down) = the shared edge
    fe = _feat("f2", "fillet", {"radius": _dim(4.0)}, refs={"edges": [edge_sel]})
    res_e = _compile_ok(_doc([base, fe]), "f1", "f2")
    vol_e = res_e.bodies[0].shape.volume
    assert abs(vol_ff - vol_e) / vol_e < 0.02, \
        f"face fillet vol {vol_ff:.2f} should match shared-edge fillet {vol_e:.2f}"
    s = _status(res, "f2")
    assert s["resolved"].get("face_fillet") == 1.0, "resolved should flag face_fillet"
    print(f"[face-fillet] vol={vol_ff:.2f} (edge-fillet ref {vol_e:.2f}, box 8000)")


def test_conic_profile_differs_from_circular() -> None:
    """A conic fillet (rho != 0.5) at the same radius removes a different amount than the
    circular fillet, and the rho->fullness map is monotonic: leaner rho (0.25) removes
    MORE material, fuller rho (0.75) removes LESS, with rho=0.5 == circular."""
    base = _box("f1", 20, 20, 20)
    res0 = _compile_ok(_doc([base]), "f1")
    edge_sel = _edge_selector(res0, surf="line",
                              key=lambda i: i.centroid[2] - i.centroid[1])

    def fillet_vol(params):
        f = _feat("f2", "fillet", params, refs={"edges": [edge_sel]})
        res = _compile_ok(_doc([base, f]), "f1", "f2")
        return res.bodies[0].shape.volume

    v_circ = fillet_vol({"radius": _dim(4.0)})
    v_lean = fillet_vol({"radius": _dim(4.0), "conic": True, "rho": _dim(0.25)})
    v_full = fillet_vol({"radius": _dim(4.0), "conic": True, "rho": _dim(0.75)})

    # circular removes 8000 - v_circ; lean removes more, full removes less.
    cut_circ = 8000.0 - v_circ
    cut_lean = 8000.0 - v_lean
    cut_full = 8000.0 - v_full
    assert cut_lean > cut_circ + 1.0, \
        f"lean conic (rho .25) should remove MORE than circular: {cut_lean:.2f} vs {cut_circ:.2f}"
    assert cut_full < cut_circ - 1.0, \
        f"full conic (rho .75) should remove LESS than circular: {cut_full:.2f} vs {cut_circ:.2f}"
    # conic must measurably differ from circular (the spec's testable requirement).
    assert abs(v_lean - v_circ) > 1.0 and abs(v_full - v_circ) > 1.0
    # resolved echoes rho for the conic runs.
    f = _feat("f2", "fillet", {"radius": _dim(4.0), "conic": True, "rho": _dim(0.75)},
              refs={"edges": [edge_sel]})
    res = _compile_ok(_doc([base, f]), "f2")
    assert abs(_status(res, "f2")["resolved"].get("rho", 0.0) - 0.75) < 1e-6
    print(f"[conic] cut lean(.25)={cut_lean:.2f} > circ={cut_circ:.2f} > full(.75)={cut_full:.2f}")


def _rounded_box(box_fid: str, fillet_fid: str, radius: float = 3.0):
    """A 20-cube with its 4 vertical edges filleted -> the top rim is a tangent chain
    (line-arc-line-arc, 8 edges, all G1-continuous). Returns (features, compiled_result,
    body) so a test can seed one top straight edge and exercise propagation.

    SW's tangent-propagation poster child: pick one rounded-rim edge, propagation walks
    the whole smooth rim. (A plain box's 90° top edges are NOT tangent, so they never
    propagate — the negative control in ``test_tangent_propagation_negative_control``.)
    """
    box = _box(box_fid, 20, 20, 20)
    res0 = _compile_ok(_doc([box]), box_fid)
    # All four vertical (±Z axis) line edges, de-duplicated by xy.
    infos = sel.describe_edges(res0.bodies[0].shape.wrapped)
    vert = [i for i in infos if i.surf == "line" and i.axis is not None
            and abs(abs(i.axis[2]) - 1.0) < 1e-3]
    seen: set = set()
    vsel: list = []
    for i in vert:
        k = (round(i.centroid[0], 2), round(i.centroid[1], 2))
        if k in seen:
            continue
        seen.add(k)
        vsel.append(sel.make_selector(i))
    assert len(vsel) == 4, f"expected 4 vertical edges; got {len(vsel)}"
    fil = _feat(fillet_fid, "fillet", {"radius": _dim(radius)}, refs={"edges": vsel})
    res = _compile_ok(_doc([box, fil]), box_fid, fillet_fid)
    return [box, fil], res, res.bodies[0]


def _top_rim_seed_selector(body):
    """A make_selector ref against one straight top-rim edge of a rounded box."""
    from build123d import GeomType
    zmax = body.shape.bounding_box().max.Z
    top = [e for e in body.shape.edges()
           if all(abs(v.Z - zmax) < 1e-4 for v in e.vertices())]
    seed_obj = next(e for e in top if e.geom_type == GeomType.LINE)
    sc = seed_obj.center()
    infos = sel.describe_edges(body.shape.wrapped)
    match = min(infos, key=lambda i: (i.centroid[0] - sc.X) ** 2
                + (i.centroid[1] - sc.Y) ** 2 + (i.centroid[2] - sc.Z) ** 2)
    return sel.make_selector(match)


def test_tangent_propagation_fillets_chain() -> None:
    """Tangent propagation grows ONE seed edge to the whole G1 rim chain.

    On a box whose 4 vertical edges are pre-filleted, the top rim is a tangent
    line-arc-line-arc chain (8 edges). Seeding one straight rim edge with
    tangent_propagation ON resolves the full 8-edge chain (resolved.propagated_edges==8),
    and the resulting solid matches explicitly filleting all 8 rim edges — proving the
    walk found exactly the tangent contour. (OCCT's filleter auto-propagates a single
    fillet seed internally, so volume alone can't distinguish; the observable here is the
    explicit chain the engine now reports, which the gap doc requires the engine to emit.)
    """
    feats, _res1, body = _rounded_box("f1", "f2", radius=3.0)
    seed_sel = _top_rim_seed_selector(body)

    f_prop = _feat("f3", "fillet",
                   {"radius": _dim(1.0), "tangent_propagation": True},
                   refs={"edges": [seed_sel]})
    res_prop = _compile_ok(_doc(feats + [f_prop]), "f3")
    s = _status(res_prop, "f3")
    n_prop = s["resolved"].get("propagated_edges", 0.0)
    assert n_prop == 8.0, \
        f"propagation should resolve the full 8-edge tangent rim; got {n_prop}"
    vol_prop = res_prop.bodies[0].shape.volume

    # Reference: explicitly fillet all 8 rim edges (no propagation flag). Must match.
    from build123d import GeomType
    zmax = body.shape.bounding_box().max.Z
    rim_objs = [e for e in body.shape.edges()
                if all(abs(v.Z - zmax) < 1e-4 for v in e.vertices())]
    rim_infos = sel.describe_edges(body.shape.wrapped)
    rim_sels = []
    for ro in rim_objs:
        rc = ro.center()
        m = min(rim_infos, key=lambda i: (i.centroid[0] - rc.X) ** 2
                + (i.centroid[1] - rc.Y) ** 2 + (i.centroid[2] - rc.Z) ** 2)
        rim_sels.append(sel.make_selector(m))
    f_all = _feat("f3", "fillet", {"radius": _dim(1.0)}, refs={"edges": rim_sels})
    res_all = _compile_ok(_doc(feats + [f_all]), "f3")
    vol_all = res_all.bodies[0].shape.volume
    assert abs(vol_prop - vol_all) / vol_all < 0.01, \
        f"propagated chain vol {vol_prop:.2f} should match all-8-edge fillet {vol_all:.2f}"
    print(f"[tangent-prop] seed=1 -> {int(n_prop)} edges; "
          f"vol {vol_prop:.2f} == all-rim {vol_all:.2f}")


def test_tangent_propagation_negative_control() -> None:
    """A plain box's top edges meet at sharp 90° corners (not tangent), so propagation
    from one top edge stays at 1 edge — confirming the walk does NOT over-grow across
    creases (SW only propagates across tangent junctions)."""
    base = _box("f1", 20, 20, 20)
    res0 = _compile_ok(_doc([base]), "f1")
    # one top edge (z=20)
    infos = sel.describe_edges(res0.bodies[0].shape.wrapped)
    top = [i for i in infos if i.surf == "line" and abs(i.centroid[2] - 20.0) < 1e-4]
    seed_sel = sel.make_selector(top[0])
    f = _feat("f2", "fillet", {"radius": _dim(2.0), "tangent_propagation": True},
              refs={"edges": [seed_sel]})
    res = _compile_ok(_doc([base, f]), "f1", "f2")
    n = _status(res, "f2")["resolved"].get("propagated_edges", 0.0)
    assert n == 1.0, f"sharp-corner box must NOT propagate; got {n} edges"
    print(f"[tangent-prop-neg] plain box seed stays at {int(n)} edge (no crease crossing)")


# =========================================================================== CHAMFER


def test_distance_angle_chamfer_wedge() -> None:
    """Distance-angle chamfer: distance=3, angle=30deg on one 20-long box edge removes a
    right-triangle wedge of legs (3) and (3·tan30) over length 20 -> 0.5·3·(3·tan30)·20.
    This differs from a symmetric 3x3 chamfer (0.5·3·3·20)."""
    base = _box("f1", 20, 20, 20)
    res0 = _compile_ok(_doc([base]), "f1")
    edge_sel = _edge_selector(res0, surf="line",
                              key=lambda i: i.centroid[2] - i.centroid[1])

    da = _feat("f2", "chamfer", {"length": _dim(3.0), "angle": _dim(30.0)},
               refs={"edges": [edge_sel]})
    res = _compile_ok(_doc([base, da]), "f1", "f2")
    vol = res.bodies[0].shape.volume
    cut = 8000.0 - vol
    expected_cut = 0.5 * 3.0 * (3.0 * math.tan(math.radians(30.0))) * 20.0
    assert abs(cut - expected_cut) / expected_cut < 0.03, \
        f"distance-angle cut {cut:.3f} vs expected {expected_cut:.3f}"

    # Symmetric 3x3 reference cut = 0.5*3*3*20 = 90 -> must differ from the angled wedge.
    sym = _feat("f2", "chamfer", {"length": _dim(3.0)}, refs={"edges": [edge_sel]})
    res_s = _compile_ok(_doc([base, sym]), "f2")
    cut_sym = 8000.0 - res_s.bodies[0].shape.volume
    assert abs(cut - cut_sym) > 1.0, \
        f"angled chamfer ({cut:.2f}) must differ from symmetric ({cut_sym:.2f})"
    s = _status(res, "f2")
    assert abs(s["resolved"].get("angle", 0.0) - 30.0) < 1e-6, "resolved should echo angle"
    print(f"[chamfer-DA] cut={cut:.3f} (expected {expected_cut:.3f}, sym {cut_sym:.3f})")


def test_vertex_chamfer_lops_corner() -> None:
    """Vertex chamfer at a box corner planes off the 3-edge corner. With setback 4 on a
    cube corner the removed solid is the corner tetrahedron 1/6·4^3 = 10.667 mm3
    (legs 4 along the three orthogonal edges)."""
    base = _box("f1", 20, 20, 20)
    res0 = _compile_ok(_doc([base]), "f1")
    # corner at (max x, max y, max z) = (20,20,20)
    v_sel = _vertex_selector(res0, key=lambda i: i.centroid[0] + i.centroid[1] + i.centroid[2])

    vc = _feat("f2", "chamfer", {"vertex_chamfer": True, "length": _dim(4.0)},
               refs={"vertices": [v_sel]})
    res = _compile_ok(_doc([base, vc]), "f1", "f2", validate=False)
    vol = res.bodies[0].shape.volume
    cut = 8000.0 - vol
    expected_cut = (4.0 ** 3) / 6.0  # corner tetra
    assert cut > 1.0, f"vertex chamfer must remove the corner; cut={cut:.3f}"
    assert abs(cut - expected_cut) / expected_cut < 0.06, \
        f"vertex-chamfer cut {cut:.3f} vs expected corner tetra {expected_cut:.3f}"
    s = _status(res, "f2")
    assert s["resolved"].get("vertex_chamfer") == 1.0, "resolved should flag vertex_chamfer"
    print(f"[vertex-chamfer] cut={cut:.3f} (expected corner tetra {expected_cut:.3f})")


def test_chamfer_tangent_propagation_grows_chain() -> None:
    """Tangent propagation on a chamfer: seed one straight top-rim edge of a rounded box
    (4 vertical edges pre-filleted -> tangent rim chain). Propagation resolves the full
    8-edge rim chain (resolved.propagated_edges == 8), same contour the fillet uses."""
    feats, _res1, body = _rounded_box("f1", "f2", radius=3.0)
    seed_sel = _top_rim_seed_selector(body)

    prop = _feat("f3", "chamfer", {"length": _dim(1.0), "tangent_propagation": True},
                 refs={"edges": [seed_sel]})
    res_p = _compile_ok(_doc(feats + [prop]), "f3")
    s = _status(res_p, "f3")
    n = s["resolved"].get("propagated_edges", 0.0)
    assert n == 8.0, f"chamfer propagation should resolve the 8-edge rim; got {n}"
    # The chamfer must actually have run and removed material along the chain.
    vol_p = res_p.bodies[0].shape.volume
    assert vol_p < body.shape.volume - 1.0, \
        f"chamfer propagation should remove material; vol {vol_p:.2f} vs {body.shape.volume:.2f}"
    print(f"[chamfer-prop] seed=1 -> {int(n)} edges; vol {vol_p:.2f}")


# =========================================================================== SHELL


def test_multi_thickness_shell_varies_wall() -> None:
    """Multi-thickness shell: base wall 2 mm with the BOTTOM face overridden to 5 mm.
    The thicker bottom wall removes a smaller interior cavity than a uniform 2 mm shell,
    so the multi-thickness solid has MORE material (larger volume). Also assert the
    bottom wall is physically thicker by sampling the floor thickness via z-extent of
    the inner cavity floor."""
    base = _box("f1", 30, 30, 30)            # 27000 mm3 solid
    res0 = _compile_ok(_doc([base]), "f1")

    # Open the TOP face (+Z) so the cavity is reachable; override the BOTTOM (-Z) wall.
    top_sel = _face_selector(res0, key=lambda i: i.centroid[2])
    bottom_sel = _face_selector(res0, key=lambda i: -i.centroid[2])

    # Uniform 2 mm shell (open top) reference.
    uni = _feat("f2", "shell", {"thickness": _dim(2.0)},
                refs={"remove": [top_sel]})
    res_u = _compile_ok(_doc([base, uni]), "f1", "f2")
    vol_u = res_u.bodies[0].shape.volume

    # Multi-thickness: base 2 mm, bottom face 5 mm.
    mt = _feat("f2", "shell", {"thickness": _dim(2.0)},
               refs={"remove": [top_sel],
                     "thickness_overrides": [
                         {"face": [bottom_sel], "thickness": _dim(5.0)}]})
    res_m = _compile_ok(_doc([base, mt]), "f1", "f2", validate=False)
    vol_m = res_m.bodies[0].shape.volume
    s = _status(res_m, "f2")

    assert vol_m > vol_u + 1.0, \
        f"thicker bottom wall -> more material: multi {vol_m:.1f} vs uniform {vol_u:.1f}"
    assert s["resolved"].get("override_count", 0.0) >= 1.0, \
        "resolved should report override_count"

    # Verify the floor is physically thicker: the inner cavity's lowest interior face
    # should sit at z=5 (multi) vs z=2 (uniform). Measure the min-Z of the *inner*
    # horizontal faces (planar, +Z/-Z normal) excluding the z=0 outer floor.
    def inner_floor_z(res) -> float:
        body = res.bodies[0].shape
        zs = []
        for f in body.faces():
            try:
                c = f.center()
                n = f.normal_at(c)
            except Exception:  # noqa: BLE001
                continue
            # interior floor: normal ~ +Z (faces up, i.e. the cavity bottom) and 0 < z < 30
            if abs(abs(n.Z) - 1.0) < 1e-3 and 0.5 < c.Z < 29.5:
                zs.append(c.Z)
        return min(zs) if zs else -1.0

    z_uni = inner_floor_z(res_u)
    z_mt = inner_floor_z(res_m)
    assert z_mt > z_uni + 1.0, \
        f"multi-thickness floor (z={z_mt:.2f}) should sit higher than uniform (z={z_uni:.2f})"
    assert abs(z_mt - 5.0) < 0.6, f"bottom wall should be ~5 mm thick; inner floor z={z_mt:.2f}"
    print(f"[multi-thick] vol uni={vol_u:.1f} < multi={vol_m:.1f}; "
          f"floor z {z_uni:.2f}->{z_mt:.2f} (bottom wall 2->5 mm)")


# --------------------------------------------------------------------------- main

def main() -> None:
    test_face_fillet_blends_between_two_faces()
    test_conic_profile_differs_from_circular()
    test_tangent_propagation_fillets_chain()
    test_tangent_propagation_negative_control()
    test_distance_angle_chamfer_wedge()
    test_vertex_chamfer_lops_corner()
    test_chamfer_tangent_propagation_grows_chain()
    test_multi_thickness_shell_varies_wall()
    print("\nDRESS-EXT TESTS PASSED")


if __name__ == "__main__":
    main()
