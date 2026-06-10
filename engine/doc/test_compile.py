"""C01 — document compiler tests (house-style plain runner, NOT pytest).

Run:  & $py -m engine.doc.test_compile   (from repo root)

Covers the card's deliverable obligations:
  * box volume == l*w*h; box + cut cylinder reduces volume by pi r^2 h (within 1%)
  * expression-driven dim resolves; editing the equation changes the volume
  * script feature builds one body from a legacy build123d snippet
  * error containment: a bad feature errors but neighbors still build
  * caching: edit feature 2-of-3 -> feature 1 is a cache hit (counters asserted)
  * owner tags: a cut cylinder's cylindrical face is owned by the cut feature

Every compiled document is also run through the schema validator first, so the fixtures used
here double as a check that the compiler consumes contract-valid documents.
"""

from __future__ import annotations

import math

from build123d import GeomType

from engine.doc.compiler import CompileCache, compile_document
from engine.doc.schema import validate_part


# --------------------------------------------------------------------------- doc builders

def _dim(value: float, expr: str | None = None) -> dict:
    return {"value": value, "expr": expr}


def _feat(fid: str, ftype: str, params: dict, refs: dict | None = None,
          name: str | None = None, suppressed: bool = False) -> dict:
    return {"id": fid, "type": ftype, "name": name or fid,
            "suppressed": suppressed, "params": params, "refs": refs or {}}


def _doc(features: list[dict], globals_list: list[dict] | None = None) -> dict:
    return {
        "schema": "aicad.part/1",
        "id": "test-doc",
        "name": "test",
        "units": {"system": "MMGS"},
        "material": None,
        "equations": {"globals": globals_list or []},
        "features": features,
    }


def _assert_valid(doc: dict) -> None:
    errs = validate_part(doc)
    assert errs == [], "doc must validate before compile:\n" + "\n".join(errs)


def _only_status(res, fid: str) -> dict:
    for s in res.feature_status:
        if s["id"] == fid:
            return s
    raise AssertionError(f"no status for {fid}")


# --------------------------------------------------------------------------- tests

def test_box_volume() -> None:
    doc = _doc([_feat("f1", "box",
                      {"length": _dim(10), "width": _dim(20), "height": _dim(30)})])
    _assert_valid(doc)
    res = compile_document(doc)
    assert _only_status(res, "f1")["ok"], _only_status(res, "f1")["error"]
    assert len(res.bodies) == 1
    vol = res.bodies[0].shape.volume
    assert abs(vol - 10 * 20 * 30) < 1e-6, f"box volume {vol} != 6000"
    print(f"[box] single box volume == l*w*h ({vol:.1f} mm^3)")


def test_box_minus_cylinder_cut() -> None:
    r, h = 4.0, 30.0
    doc = _doc([
        _feat("f1", "box", {"length": _dim(20), "width": _dim(20), "height": _dim(30)}),
        _feat("f2", "cylinder", {"radius": _dim(r), "height": _dim(h), "mode": "cut"}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    assert _only_status(res, "f2")["ok"], _only_status(res, "f2")["error"]
    vol = res.bodies[0].shape.volume
    expected = 20 * 20 * 30 - math.pi * r * r * h
    assert abs(vol - expected) / expected < 0.01, f"cut volume {vol} vs expected {expected}"
    print(f"[cut] box - cut cylinder reduces by pi r^2 h (vol {vol:.1f}, expected {expected:.1f})")


def test_expression_dim_resolves_and_drives() -> None:
    doc = _doc(
        [_feat("f1", "box",
               {"length": _dim(10), "width": _dim(10), "height": _dim(0, "depth")})],
        globals_list=[{"name": "depth", "expr": "5"}],
    )
    _assert_valid(doc)
    res = compile_document(doc)
    st = _only_status(res, "f1")
    assert st["ok"], st["error"]
    assert abs(st["resolved"]["height"] - 5.0) < 1e-9, f"resolved height {st['resolved']}"
    vol1 = res.bodies[0].shape.volume
    assert abs(vol1 - 10 * 10 * 5) < 1e-6, f"expr-driven volume {vol1}"

    # Change the equation -> the volume changes.
    doc["equations"]["globals"][0]["expr"] = "8"
    res2 = compile_document(doc)
    vol2 = res2.bodies[0].shape.volume
    assert abs(vol2 - 10 * 10 * 8) < 1e-6, f"re-driven volume {vol2}"
    assert vol2 != vol1
    print(f"[equations] expr dim resolves ({vol1:.0f}) and re-drives on equation edit ({vol2:.0f})")


def test_script_feature_one_body() -> None:
    doc = _doc([_feat("f1", "script", {"code": "result = Box(10, 10, 10)"})])
    _assert_valid(doc)
    res = compile_document(doc)
    st = _only_status(res, "f1")
    assert st["ok"], st["error"]
    assert len(res.bodies) == 1, f"expected 1 body, got {len(res.bodies)}"
    assert abs(res.bodies[0].shape.volume - 1000.0) < 1e-6
    print("[script] legacy build123d snippet -> one body via forgiving kernel")


def test_error_containment() -> None:
    # f2 is a cut cylinder placed so it removes nothing -> the compiler surfaces a contained
    # error; f1 (before) and f3 (after) still build. f3 adds a separate body (merge:false).
    doc = _doc([
        _feat("f1", "box", {"length": _dim(20), "width": _dim(20), "height": _dim(20)}),
        _feat("f2", "cylinder",
              {"radius": _dim(2), "height": _dim(5), "mode": "cut"},
              refs={"at": "axis.z"}),  # axis.z is not a point/plane -> placement error
        _feat("f3", "box",
              {"length": _dim(5), "width": _dim(5), "height": _dim(5), "merge": False},
              refs={"at": "origin"}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    assert _only_status(res, "f1")["ok"]
    assert not _only_status(res, "f2")["ok"], "bad cut feature should error"
    assert _only_status(res, "f2")["error"]
    assert _only_status(res, "f3")["ok"], "neighbor after the error must still build"
    # f1's body survives + f3 added a separate body; f2 contributed nothing.
    assert len(res.bodies) == 2, f"expected 2 bodies (f1 + f3), got {len(res.bodies)}"
    print("[containment] bad feature errors; upstream + downstream features still build")


def test_cache_hit_on_unedited_prefix() -> None:
    cache = CompileCache()
    doc = _doc([
        _feat("f1", "box", {"length": _dim(20), "width": _dim(20), "height": _dim(20)}),
        _feat("f2", "cylinder", {"radius": _dim(5), "height": _dim(40), "mode": "cut"}),
        _feat("f3", "box",
              {"length": _dim(6), "width": _dim(6), "height": _dim(6), "merge": False},
              refs={"at": "origin"}),
    ])
    _assert_valid(doc)
    r1 = compile_document(doc, cache)
    assert r1.cache_hits == 0 and r1.cache_misses == 3, (r1.cache_hits, r1.cache_misses)

    # Edit feature 2 (middle). f1 must be a cache HIT; f2 and f3 recompute (key chains forward).
    doc["features"][1]["params"]["radius"] = _dim(4)
    r2 = compile_document(doc, cache)
    assert r2.cache_hits == 1, f"expected 1 cache hit (f1), got {r2.cache_hits}"
    assert r2.cache_misses == 2, f"expected 2 misses (f2,f3), got {r2.cache_misses}"
    # Result still correct after the edit. The hole drilled through the 20 mm-tall box removes
    # pi r^2 * 20 (the box height), not the full 40 mm cylinder length.
    expected = 20 * 20 * 20 - math.pi * 4 * 4 * 20
    assert abs(r2.bodies[0].shape.volume - expected) / expected < 0.01
    print(f"[cache] edit feature 2-of-3 -> f1 hit, f2+f3 miss "
          f"(hits={r2.cache_hits}, misses={r2.cache_misses})")


def test_owner_tags_cut_face() -> None:
    doc = _doc([
        _feat("f1", "box", {"length": _dim(30), "width": _dim(30), "height": _dim(30)}),
        _feat("f2", "cylinder", {"radius": _dim(5), "height": _dim(40), "mode": "cut"}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    assert len(res.bodies) == 1
    body = res.bodies[0]
    owners = res.face_owners[body.body_id]
    # Find the cylindrical face index and assert the cut feature (f2) owns it.
    faces = body.shape.faces()
    cyl_idx = None
    for i, f in enumerate(faces):
        if f.geom_type == GeomType.CYLINDER:
            # face order from .faces() matches the TopExp map order (both walk TopAbs_FACE).
            cyl_idx = i + 1
            break
    assert cyl_idx is not None, "no cylindrical face found on the cut body"
    assert owners.get(cyl_idx) == "f2", (
        f"cylindrical face {cyl_idx} owner is {owners.get(cyl_idx)}, expected f2; owners={owners}")
    # And at least one planar face is still owned by the original box feature f1.
    assert "f1" in owners.values(), f"box faces should still be owned by f1; owners={owners}"
    print(f"[owners] cut cylinder's cylindrical face owned by the cut feature (f2); "
          f"box faces still f1")


def test_suppressed_feature_skipped() -> None:
    doc = _doc([
        _feat("f1", "box", {"length": _dim(10), "width": _dim(10), "height": _dim(10)}),
        _feat("f2", "cylinder", {"radius": _dim(3), "height": _dim(20), "mode": "cut"},
              suppressed=True),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    st = _only_status(res, "f2")
    assert st["skipped"] and st["ok"] and st["ms"] == 0.0, st
    assert abs(res.bodies[0].shape.volume - 1000.0) < 1e-6, "suppressed cut must not apply"
    print("[suppress] suppressed feature skipped (ok, ms=0), geometry unaffected")


def main() -> None:
    test_box_volume()
    test_box_minus_cylinder_cut()
    test_expression_dim_resolves_and_drives()
    test_script_feature_one_body()
    test_error_containment()
    test_cache_hit_on_unedited_prefix()
    test_owner_tags_cut_face()
    test_suppressed_feature_skipped()
    print("\nDOC COMPILER TESTS PASSED")


if __name__ == "__main__":
    main()
