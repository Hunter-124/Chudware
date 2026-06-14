r"""High-index edge/face resolution in the dress-up compilers (fillet/chamfer/shell).

Run:  & $py -X utf8 -m pytest engine\doc\test_dress_resolve.py -q   (from repo root)

Regression guard for the latent index bug in ``dress._resolve_edges`` /
``dress._resolve_faces``: ``selectors.describe_edges`` / ``describe_faces``
enumerate the solid with a raw ``TopExp_Explorer`` that visits each edge ONCE PER
INCIDENT FACE, so a plain box yields 24 edge-descriptors (indices 0..23) while
``body.edges()`` de-duplicates to the 12 unique edges. The resolved index therefore
lives in the *wider* describe-space; the old code indexed ``body.edges()[idx]``
directly, which raises ``IndexError`` for any high-index pick (e.g. a box's top-rim
edge at describe-index 23) or, when the enumeration orders differ, silently selects
the WRONG entity.

The fix maps each resolved descriptor back to the nearest UNIQUE
``body.edges()`` / ``body.faces()`` element by edge midpoint / face centroid — the
same safeguard ``dress._resolve_vertices`` already applies to duplicated box-corner
vertices. These tests:

  1. Prove the bug is live: a box's ``describe_edges`` index space (24) really does
     exceed ``len(body.edges())`` (12), so the old ``edge_objs[idx]`` indexing would
     ``IndexError`` on the chosen high-index pick.
  2. Drive a real fillet AND a real chamfer through a HIGH-INDEX edge selector
     (describe-index >= len(body.edges())) and assert each resolves to the correct
     edge — no IndexError, correct removed-material geometry.
  3. Drive a real shell through a face selector built from the HIGHEST face
     descriptor and assert it opens exactly that face (centroid-mapped, not index).
  4. Unit-level: assert ``_resolve_edges`` / ``_resolve_faces`` map by midpoint /
     centroid (the resolved entity's centre matches the descriptor's centroid),
     proving the de-dup mapping rather than blind ``[idx]`` indexing.
"""

from __future__ import annotations

import pytest

from engine.doc.compiler import compile_document
from engine.doc.schema import validate_part
from engine.doc import selectors as sel
from engine.doc.features.dress import _resolve_edges, _resolve_faces


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


def _box(fid: str, l: float, w: float, h: float, at: str = "origin") -> dict:
    params = {"length": _dim(l), "width": _dim(w), "height": _dim(h),
              "centered": False}
    return _feat(fid, "box", params, refs={"at": at})


def _status(res, fid: str) -> dict:
    for s in res.feature_status:
        if s["id"] == fid:
            return s
    raise AssertionError(f"no status for {fid}")


def _compile_ok(doc: dict, *fids: str, validate: bool = True):
    if validate:
        errs = validate_part(doc)
        assert errs == [], "doc must validate before compile:\n" + "\n".join(errs)
    res = compile_document(doc)
    for fid in fids:
        s = _status(res, fid)
        assert s["ok"], f"{fid} failed: {s['error']}"
    return res


def _highest_index_edge_descriptor(body):
    """Return the ``describe_edges`` descriptor with the maximum index for *body*.

    For a box this is index 23 (24 descriptors, one per incident face) — far above
    ``len(body.edges()) == 12``, so the old ``edge_objs[idx]`` indexing IndexErrors.
    """
    infos = sel.describe_edges(body.shape.wrapped)
    return max(infos, key=lambda i: i.index), infos


def _highest_index_face_descriptor(body):
    infos = sel.describe_faces(body.shape.wrapped)
    return max(infos, key=lambda i: i.index), infos


# =========================================================================== edges


def test_describe_edge_index_space_exceeds_unique_edges() -> None:
    """The premise of the bug: a box's describe-edge index space is WIDER than
    ``body.edges()`` — so a high-index pick would IndexError under naive indexing."""
    base = _box("f1", 20, 20, 20)
    res0 = _compile_ok(_doc([base]), "f1")
    body = res0.bodies[0]

    n_describe = len(sel.describe_edges(body.shape.wrapped))
    n_unique = len(body.shape.edges())
    assert n_describe == 24, f"box should yield 24 edge-descriptors; got {n_describe}"
    assert n_unique == 12, f"box should have 12 unique edges; got {n_unique}"

    hi, _ = _highest_index_edge_descriptor(body)
    assert hi.index >= n_unique, (
        f"highest edge descriptor index {hi.index} must exceed unique-edge count "
        f"{n_unique} to exercise the bug"
    )
    # Old code path: ``body.edges()[hi.index]`` would IndexError. Confirm it does.
    with pytest.raises(IndexError):
        _ = body.shape.edges()[hi.index]


def test_fillet_resolves_high_index_edge() -> None:
    """A fillet driven by a HIGH-INDEX edge selector (describe-index 23 >> 12 unique
    edges) resolves to the correct edge and removes the expected material — no
    IndexError, correct geometry (the old ``edge_objs[idx]`` path would crash)."""
    base = _box("f1", 20, 20, 20)
    res0 = _compile_ok(_doc([base]), "f1")
    body = res0.bodies[0]

    hi, _ = _highest_index_edge_descriptor(body)
    assert hi.index >= len(body.shape.edges())  # genuinely high index
    edge_sel = sel.make_selector(hi)

    # Unit check: the resolver maps to the unique edge whose midpoint == descriptor.
    edges, dang = _resolve_edges([edge_sel], body.shape, "edges")
    assert not dang, f"high-index edge must resolve (no dangling): {dang}"
    assert len(edges) == 1
    c = edges[0].center()
    assert (abs(c.X - hi.centroid[0]) < 1e-3 and abs(c.Y - hi.centroid[1]) < 1e-3
            and abs(c.Z - hi.centroid[2]) < 1e-3), (
        f"resolved edge midpoint {(c.X, c.Y, c.Z)} must match descriptor "
        f"centroid {tuple(hi.centroid)} (centroid-mapped, not blind index)"
    )

    # Full feature: a single-edge fillet at r=3 removes (1 - pi/4)*r^2 along a 20-long
    # edge = (1 - pi/4)*9*20 ~ 38.65 mm^3. Just assert material was removed correctly.
    r = 3.0
    fil = _feat("f2", "fillet", {"radius": _dim(r)}, refs={"edges": [edge_sel]})
    res = _compile_ok(_doc([base, fil]), "f1", "f2")
    vol = res.bodies[0].shape.volume
    cut = 8000.0 - vol
    import math
    expected = (1.0 - math.pi / 4.0) * r * r * 20.0
    assert cut > 1.0, f"fillet must remove material; cut={cut:.3f}"
    assert abs(cut - expected) / expected < 0.05, (
        f"high-index-edge fillet cut {cut:.3f} vs expected single-edge {expected:.3f}"
    )


def test_chamfer_resolves_high_index_edge() -> None:
    """A chamfer driven by the same HIGH-INDEX edge selector resolves correctly and
    removes the symmetric distance*distance wedge along the 20-long edge."""
    base = _box("f1", 20, 20, 20)
    res0 = _compile_ok(_doc([base]), "f1")
    body = res0.bodies[0]

    hi, _ = _highest_index_edge_descriptor(body)
    assert hi.index >= len(body.shape.edges())
    edge_sel = sel.make_selector(hi)

    d = 3.0
    ch = _feat("f2", "chamfer", {"length": _dim(d)}, refs={"edges": [edge_sel]})
    res = _compile_ok(_doc([base, ch]), "f1", "f2")
    vol = res.bodies[0].shape.volume
    cut = 8000.0 - vol
    expected = 0.5 * d * d * 20.0  # right-triangle wedge along the 20-long edge
    assert cut > 1.0, f"chamfer must remove material; cut={cut:.3f}"
    assert abs(cut - expected) / expected < 0.03, (
        f"high-index-edge chamfer cut {cut:.3f} vs expected {expected:.3f}"
    )


# =========================================================================== faces


def test_face_resolver_maps_by_centroid_not_index() -> None:
    """``_resolve_faces`` maps EACH describe-face descriptor back to the unique
    ``body.faces()`` face whose centroid matches — for every descriptor index, the
    resolved face's centre equals the descriptor centroid. This holds regardless of
    whether the describe order happens to coincide with ``body.faces()`` order, which
    is exactly what the centroid mapping guarantees (vs. blind ``face_objs[idx]``)."""
    base = _box("f1", 30, 30, 30)
    res0 = _compile_ok(_doc([base]), "f1")
    body = res0.bodies[0]

    infos = sel.describe_faces(body.shape.wrapped)
    for info in infos:
        faces, dang = _resolve_faces([sel.make_selector(info)], body.shape, "remove")
        assert not dang, f"face descriptor {info.index} must resolve: {dang}"
        assert len(faces) == 1
        c = faces[0].center()
        assert (abs(c.X - info.centroid[0]) < 1e-3
                and abs(c.Y - info.centroid[1]) < 1e-3
                and abs(c.Z - info.centroid[2]) < 1e-3), (
            f"face {info.index}: resolved centre {(c.X, c.Y, c.Z)} must match "
            f"descriptor centroid {tuple(info.centroid)} (centroid-mapped)"
        )


def test_shell_opens_high_index_face() -> None:
    """A shell driven by a face selector built from the HIGHEST face descriptor opens
    exactly that face: the resulting hollow body has an opening on the descriptor's
    plane (its outer face on that plane is gone), and the solid is hollowed (volume
    drops well below the solid box). Exercises ``compile_shell`` -> ``_resolve_faces``
    end-to-end with the centroid-mapping fix."""
    base = _box("f1", 30, 30, 30)
    res0 = _compile_ok(_doc([base]), "f1")
    body = res0.bodies[0]

    hi, _ = _highest_index_face_descriptor(body)
    face_sel = sel.make_selector(hi)

    # Unit check: resolver returns the face whose centre matches the descriptor.
    faces, dang = _resolve_faces([face_sel], body.shape, "remove")
    assert not dang and len(faces) == 1, f"high-index face must resolve: {dang}"
    fc = faces[0].center()
    assert (abs(fc.X - hi.centroid[0]) < 1e-3 and abs(fc.Y - hi.centroid[1]) < 1e-3
            and abs(fc.Z - hi.centroid[2]) < 1e-3), (
        f"resolved face centre {(fc.X, fc.Y, fc.Z)} must match descriptor "
        f"centroid {tuple(hi.centroid)}"
    )

    thickness = 3.0
    shell = _feat("f2", "shell", {"thickness": _dim(thickness)},
                  refs={"remove": [face_sel]})
    res = _compile_ok(_doc([base, shell]), "f1", "f2")
    shelled = res.bodies[0].shape

    # Hollowed: a 30-cube with 3 mm walls (one face open) is far lighter than solid.
    vol = shelled.volume
    assert vol < 27000.0 * 0.6, f"shell must hollow the body; vol={vol:.1f}"

    # The opened face's plane must no longer carry the FULL 30x30 (900 mm^2) outer
    # face — opening it leaves only the thin wall rim (30^2 - 24^2 = 324 mm^2) on that
    # plane. If the resolver had opened the WRONG face, the original full top face
    # would still be present here. Tolerance: anything >= ~half the original area.
    cx, cy, cz = (float(hi.centroid[0]), float(hi.centroid[1]), float(hi.centroid[2]))
    on_plane = [
        f for f in shelled.faces()
        if abs(f.center().X - cx) < 0.6
        and abs(f.center().Y - cy) < 0.6
        and abs(f.center().Z - cz) < 0.6
    ]
    full_outer = [f for f in on_plane if f.area > 900.0 * 0.5]
    assert not full_outer, (
        f"the full outer face on the opened plane (centroid {(cx, cy, cz)}) should be "
        f"gone; found a {[round(f.area, 1) for f in full_outer]} mm^2 face still there "
        f"-> resolver opened the wrong face"
    )
    # And the wall rim (~324 mm^2) IS still present, confirming the shell ran there.
    rim = [f for f in on_plane if 200.0 < f.area < 500.0]
    assert rim, (
        f"expected the ~324 mm^2 wall rim on the opened plane; on-plane face areas: "
        f"{[round(f.area, 1) for f in on_plane]}"
    )


# --------------------------------------------------------------------------- main

def main() -> None:
    test_describe_edge_index_space_exceeds_unique_edges()
    test_fillet_resolves_high_index_edge()
    test_chamfer_resolves_high_index_edge()
    test_face_resolver_maps_by_centroid_not_index()
    test_shell_opens_high_index_face()
    print("\nDRESS-RESOLVE TESTS PASSED")


if __name__ == "__main__":
    main()
