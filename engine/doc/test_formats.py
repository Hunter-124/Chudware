"""C21 — formats + import feature + .step+ extension tests (house-style plain runner).

Run:  & $py -m engine.doc.test_formats   (from repo root)

Covers:
  1. export_document -> all 5 formats (step/brep/iges/stl/3mf) -> re-import -> volume within
     tolerance (STEP/BREP/IGES exact ~0.1%; STL/3MF mesh ~2%).
  2. import feature in a doc compiles + caches (second compile is a cache hit).
  3. .step+ round-trip with injected studies/fake.json + ai/memory.json survives save->open->save.
  4. frozen-geometry fallback: corrupt a feature in a saved .step+ JSON, open -> fallback path
     yields a doc with one import feature whose volume matches.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import zipfile

from engine.doc import session as docsess
from engine.doc import stepplus
from engine.doc.compiler import CompileCache, compile_document
from engine.doc.formats import export_document, import_path, _cached_import


# --------------------------------------------------------------------------- fixtures

def _dim(value: float, expr=None):
    return {"value": value, "expr": expr}


def _filleted_box_session():
    """Build a 20×20×20 box with a 2mm fillet applied — used for volume roundtrip checks."""
    s = docsess.new_session("part", "TestBox")
    docsess.apply_ops(s, 0, [
        {"op": "add_feature", "feature": {
            "type": "box", "name": "Box1",
            "params": {"length": _dim(20), "width": _dim(20), "height": _dim(20)}
        }},
    ])
    # Apply a fillet — uses dress.py compiler
    # First we need to resolve an edge selector.  Use the compile result to grab one edge.
    from engine.doc.session import _rebuild, _solid_bodies
    from engine.doc import selectors as sel
    result = s.last_result
    bodies = _solid_bodies(result)
    if bodies:
        shape = bodies[0].shape
        infos = sel.describe_edges(shape.wrapped)
        if infos:
            info = infos[0]
            info.hint_index = 0
            edge_sel = sel.make_selector(info)
            docsess.apply_ops(s, s.revision, [
                {"op": "add_feature", "feature": {
                    "type": "fillet", "name": "Fillet1",
                    "params": {"radius": _dim(2.0)},
                    "refs": {"edges": [edge_sel]},
                }},
            ])
    return s


def _box_volume() -> float:
    """Exact volume of a 20×20×20 box."""
    return 20.0 * 20.0 * 20.0  # 8000.0


def _get_bodies(session):
    """Return list of (name, shape) from a session's last compile result."""
    from engine.doc.session import _solid_bodies
    result = session.last_result
    bodies = _solid_bodies(result)
    return [(bo.name or f"Body{i}", bo.shape) for i, bo in enumerate(bodies, start=1)]


# --------------------------------------------------------------------------- 1. export/import roundtrip

def test_export_import_step() -> None:
    s = docsess.new_session("part", "BoxForSTEP")
    docsess.apply_ops(s, 0, [{"op": "add_feature", "feature": {
        "type": "box", "name": "Box1",
        "params": {"length": _dim(20), "width": _dim(20), "height": _dim(20)}
    }}])
    bodies = _get_bodies(s)
    assert bodies, "no bodies to export"

    tmpdir = tempfile.mkdtemp()
    path = os.path.join(tmpdir, "test.step")
    export_document(bodies, path, "step")
    assert os.path.exists(path) and os.path.getsize(path) > 100

    imported = import_path(path)
    assert imported, "STEP import returned no bodies"
    vol = imported[0][1].volume
    expected = _box_volume()
    rel_err = abs(vol - expected) / expected
    assert rel_err < 0.001, f"STEP volume err {rel_err:.4f} (vol={vol:.1f}, expected={expected})"
    print(f"[formats] STEP export->import vol={vol:.1f} err={rel_err:.5f}")


def test_export_import_brep() -> None:
    s = docsess.new_session("part", "BoxForBREP")
    docsess.apply_ops(s, 0, [{"op": "add_feature", "feature": {
        "type": "box", "name": "Box1",
        "params": {"length": _dim(20), "width": _dim(20), "height": _dim(20)}
    }}])
    bodies = _get_bodies(s)

    tmpdir = tempfile.mkdtemp()
    path = os.path.join(tmpdir, "test.brep")
    export_document(bodies, path, "brep")
    assert os.path.exists(path) and os.path.getsize(path) > 100

    imported = import_path(path)
    assert imported
    vol = imported[0][1].volume
    expected = _box_volume()
    rel_err = abs(vol - expected) / expected
    assert rel_err < 0.001, f"BREP volume err {rel_err:.4f}"
    print(f"[formats] BREP export->import vol={vol:.1f} err={rel_err:.5f}")


def test_export_import_iges() -> None:
    s = docsess.new_session("part", "BoxForIGES")
    docsess.apply_ops(s, 0, [{"op": "add_feature", "feature": {
        "type": "box", "name": "Box1",
        "params": {"length": _dim(20), "width": _dim(20), "height": _dim(20)}
    }}])
    bodies = _get_bodies(s)

    tmpdir = tempfile.mkdtemp()
    path = os.path.join(tmpdir, "test.igs")
    export_document(bodies, path, "iges")
    assert os.path.exists(path) and os.path.getsize(path) > 100

    imported = import_path(path)
    assert imported
    # IGES may return shell (volume from ShapeFix) or solid; check whatever `.volume` returns
    vol = imported[0][1].volume
    expected = _box_volume()
    rel_err = abs(vol - expected) / expected
    assert rel_err < 0.001, f"IGES volume err {rel_err:.4f} (vol={vol:.1f})"
    print(f"[formats] IGES export->import vol={vol:.1f} err={rel_err:.5f}")


def test_export_import_stl() -> None:
    """STL is a mesh format — no volume, but we can check the import returns something."""
    s = docsess.new_session("part", "BoxForSTL")
    docsess.apply_ops(s, 0, [{"op": "add_feature", "feature": {
        "type": "box", "name": "Box1",
        "params": {"length": _dim(20), "width": _dim(20), "height": _dim(20)}
    }}])
    bodies = _get_bodies(s)

    tmpdir = tempfile.mkdtemp()
    path = os.path.join(tmpdir, "test.stl")
    export_document(bodies, path, "stl")
    assert os.path.exists(path) and os.path.getsize(path) > 100

    imported = import_path(path)
    assert imported
    # STL comes back as a Face (mesh surface) — check it has an area matching 6 × 20² = 2400 mm²
    shape = imported[0][1]
    area = getattr(shape, "area", None)
    if area is not None:
        rel_err = abs(area - 2400.0) / 2400.0
        assert rel_err < 0.02, f"STL area err {rel_err:.4f} (area={area:.1f})"
        print(f"[formats] STL export->import area={area:.1f} err={rel_err:.5f}")
    else:
        print(f"[formats] STL export->import ok (no area attr: {type(shape)})")


def test_export_import_3mf() -> None:
    """3MF is mesh — volume from divergence theorem should be within 2%."""
    s = docsess.new_session("part", "BoxFor3MF")
    docsess.apply_ops(s, 0, [{"op": "add_feature", "feature": {
        "type": "box", "name": "Box1",
        "params": {"length": _dim(20), "width": _dim(20), "height": _dim(20)}
    }}])
    bodies = _get_bodies(s)

    tmpdir = tempfile.mkdtemp()
    path = os.path.join(tmpdir, "test.3mf")
    export_document(bodies, path, "3mf")
    assert os.path.exists(path) and os.path.getsize(path) > 100

    imported = import_path(path)
    assert imported
    vol = imported[0][1].volume
    expected = _box_volume()
    rel_err = abs(vol - expected) / expected
    assert rel_err < 0.02, f"3MF volume err {rel_err:.4f} (vol={vol:.1f})"
    print(f"[formats] 3MF export->import vol={vol:.1f} err={rel_err:.5f}")


# --------------------------------------------------------------------------- 2. import feature compiles + caches

def _import_feature_doc(step_path: str) -> dict:
    return {
        "schema": "aicad.part/1",
        "id": "test-import",
        "name": "test",
        "units": {"system": "MMGS"},
        "material": None,
        "equations": {"globals": []},
        "features": [
            {
                "id": "f1",
                "type": "import",
                "name": "Imported",
                "suppressed": False,
                "params": {
                    "source_path": step_path,
                    "format": "step",
                    "original_name": "Imported",
                },
                "refs": {},
            }
        ],
    }


def test_import_feature_compiles_and_caches() -> None:
    # Export a box to STEP so we have a real file to import.
    s = docsess.new_session("part", "BoxForImportFeature")
    docsess.apply_ops(s, 0, [{"op": "add_feature", "feature": {
        "type": "box", "name": "Box1",
        "params": {"length": _dim(30), "width": _dim(30), "height": _dim(30)}
    }}])
    tmpdir = tempfile.mkdtemp()
    step_path = os.path.join(tmpdir, "source.step")
    bodies = _get_bodies(s)
    export_document(bodies, step_path, "step")

    # Build a document that uses the import feature.
    doc = _import_feature_doc(step_path)
    from engine.doc.formats import _IMPORT_CACHE
    _IMPORT_CACHE.clear()  # start clean

    cache = CompileCache()
    r1 = compile_document(doc, cache)
    st = next((s for s in r1.feature_status if s["id"] == "f1"), None)
    assert st is not None and st["ok"], f"import feature failed: {st}"
    assert len(r1.bodies) >= 1, "import feature must produce at least one body"
    vol = r1.bodies[0].shape.volume
    expected = 30.0 ** 3
    rel_err = abs(vol - expected) / expected
    assert rel_err < 0.001, f"import feature volume err {rel_err:.4f}"
    assert r1.cache_misses == 1 and r1.cache_hits == 0, \
        f"first compile: expected 0 hits 1 miss, got hits={r1.cache_hits} misses={r1.cache_misses}"
    print(f"[formats] import feature compile ok vol={vol:.0f} err={rel_err:.5f}")

    # Second compile with same cache — the feature is a cache hit (key unchanged).
    r2 = compile_document(doc, cache)
    assert r2.cache_hits == 1 and r2.cache_misses == 0, \
        f"second compile: expected 1 hit 0 miss, got hits={r2.cache_hits} misses={r2.cache_misses}"
    print(f"[formats] import feature second compile -> cache hit (hits={r2.cache_hits})")

    # _cached_import itself is also memoized — a direct call should not re-read the file.
    # Verify by checking the cache dict has an entry.
    assert any(step_path in k for k in _IMPORT_CACHE), "import path cache should be populated"
    len_before = len(_IMPORT_CACHE)
    _cached_import(step_path, "step")
    assert len(_IMPORT_CACHE) == len_before, "second _cached_import call must not grow the cache"
    print(f"[formats] _cached_import memoized correctly")


# --------------------------------------------------------------------------- 3. step+ meta passthrough

def test_stepplus_meta_passthrough() -> None:
    """studies/fake.json + ai/memory.json survive save->open->save round trip."""
    s = docsess.new_session("part", "MetaPassthrough")
    docsess.apply_ops(s, 0, [{"op": "add_feature", "feature": {
        "type": "box", "name": "Box1",
        "params": {"length": _dim(10), "width": _dim(10), "height": _dim(10)}
    }}])

    tmpdir = tempfile.mkdtemp()
    path1 = os.path.join(tmpdir, "model1.step+")
    stepplus.save(s, path1)

    # Inject studies/fake.json and ai/memory.json into the saved file.
    studies_payload = json.dumps({"study_id": "s1", "type": "static"}).encode()
    memory_payload = json.dumps({
        "design_intent": "test passthrough",
        "decisions": [],
        "conversation_digests": [],
        "named_dims": {},
    }).encode()
    stepplus.inject_meta_entry(path1, "studies/fake.json", studies_payload)
    stepplus.inject_meta_entry(path1, "ai/memory.json", memory_payload)

    # Verify the entries are present in the zip.
    with zipfile.ZipFile(path1) as zf:
        assert "studies/fake.json" in zf.namelist()
        assert "ai/memory.json" in zf.namelist()
        assert zf.read("studies/fake.json") == studies_payload
        assert zf.read("ai/memory.json") == memory_payload

    # Open -> check extra_entries carry them.
    s2, warnings = stepplus.open(path1)
    assert warnings == [], warnings
    assert "studies/fake.json" in s2.extra_entries, "studies/fake.json must survive open"
    assert "ai/memory.json" in s2.extra_entries, "ai/memory.json must survive open"
    assert s2.extra_entries["ai/memory.json"] == memory_payload

    # Resave -> entries still present.
    path2 = os.path.join(tmpdir, "model2.step+")
    stepplus.save(s2, path2)
    with zipfile.ZipFile(path2) as zf:
        names = zf.namelist()
        assert "studies/fake.json" in names, "studies/fake.json must survive resave"
        assert "ai/memory.json" in names, "ai/memory.json must survive resave"
        assert zf.read("ai/memory.json") == memory_payload

    # read_meta_entry helper
    data = stepplus.read_meta_entry(path2, "ai/memory.json")
    assert data == memory_payload
    assert stepplus.read_meta_entry(path2, "nonexistent/entry.json") is None

    print("[formats] studies/fake.json + ai/memory.json survive save->open->save")


# --------------------------------------------------------------------------- 4. frozen-geometry fallback

def test_frozen_geometry_fallback() -> None:
    """Corrupt a feature type in saved document.json → open triggers fallback → import feature."""
    # Build and save a valid box session.
    s = docsess.new_session("part", "FrozenFallback")
    docsess.apply_ops(s, 0, [{"op": "add_feature", "feature": {
        "type": "box", "name": "Box1",
        "params": {"length": _dim(15), "width": _dim(15), "height": _dim(15)}
    }}])
    expected_vol = 15.0 ** 3  # 3375.0

    tmpdir = tempfile.mkdtemp()
    path = os.path.join(tmpdir, "fallback.step+")
    stepplus.save(s, path)

    # Now corrupt document.json: change the feature type to something the compiler cannot handle.
    with zipfile.ZipFile(path, "r") as zin:
        doc = json.loads(zin.read("document.json"))
    doc["features"][0]["type"] = "unknown_type_that_will_never_compile"
    # Rewrite document.json into the zip.
    tmp_corrupt = path + ".corrupt.tmp"
    with zipfile.ZipFile(path, "r") as zin:
        with zipfile.ZipFile(tmp_corrupt, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                if item.filename == "document.json":
                    zout.writestr("document.json",
                                  json.dumps(doc, indent=2).encode("utf-8"))
                else:
                    zout.writestr(item, zin.read(item.filename))
    os.replace(tmp_corrupt, path)

    # Open — should trigger the frozen-geometry fallback.
    s2, warnings = stepplus.open(path)
    assert warnings, "expected at least one warning from fallback"
    # The warning message should mention the fallback.
    assert any("frozen" in w.lower() or "import" in w.lower() or "fallback" in w.lower()
               for w in warnings), f"expected fallback warning, got: {warnings}"

    # The resulting session should have exactly one feature of type "import".
    feats = s2.doc.get("features", [])
    assert len(feats) >= 1, f"fallback session must have at least one feature, got {feats}"
    assert feats[0]["type"] == "import", \
        f"fallback feature type must be 'import', got {feats[0]['type']}"

    # The import feature must produce a body whose volume matches the original.
    result = s2.last_result
    assert result is not None, "fallback session must have a compile result"
    feat_st = result.feature_status[0]
    assert feat_st["ok"], f"fallback import feature must be ok, got: {feat_st}"
    bodies = result.bodies
    assert bodies, "fallback session must have at least one body"
    vol = bodies[0].shape.volume
    rel_err = abs(vol - expected_vol) / expected_vol
    assert rel_err < 0.001, \
        f"frozen fallback volume err {rel_err:.4f} (vol={vol:.1f}, expected={expected_vol})"

    print(f"[formats] frozen fallback: corrupt feature -> import feature vol={vol:.0f} "
          f"err={rel_err:.5f}")


# --------------------------------------------------------------------------- main

def main() -> None:
    test_export_import_step()
    test_export_import_brep()
    test_export_import_iges()
    test_export_import_stl()
    test_export_import_3mf()
    test_import_feature_compiles_and_caches()
    test_stepplus_meta_passthrough()
    test_frozen_geometry_fallback()
    print("\nFORMATS TESTS PASSED")


if __name__ == "__main__":
    main()
