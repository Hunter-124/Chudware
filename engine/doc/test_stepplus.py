"""C11 — .step+ container tests (house-style plain runner, NOT pytest).

Run:  & $py -m engine.doc.test_stepplus   (from repo root)

Covers:
  * save -> open roundtrip preserves document JSON + revision resets cleanly on open
  * required entries present (thumbnail STORED first, meta/document/geometry)
  * unknown zip entries are preserved on resave (forward-compat, stepplus.md §3)
  * atomic save leaves no .tmp behind
"""

from __future__ import annotations

import os
import tempfile
import zipfile

from engine.doc import session as docsess
from engine.doc import stepplus


def _box_session(name="Box1"):
    s = docsess.new_session("part", name)
    docsess.apply_ops(s, 0, [{"op": "add_feature", "feature": {
        "type": "box", "name": "Box1",
        "params": {"length": {"value": 20, "expr": None},
                   "width": {"value": 20, "expr": None},
                   "height": {"value": 20, "expr": None}}}}])
    return s


def test_save_open_roundtrip() -> None:
    s = _box_session()
    s.doc["features"][0]["params"]["height"]["value"] = 25  # tweak so we can detect preservation
    s.revision = 7  # arbitrary non-zero revision before save

    tmpdir = tempfile.mkdtemp()
    path = os.path.join(tmpdir, "model.step+")
    out = stepplus.save(s, path)
    assert out == path and os.path.exists(path)
    assert not os.path.exists(path + ".tmp"), "atomic save must not leave a .tmp file"

    # ZIP layout: thumbnail FIRST and STORED, required entries present.
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        assert names[0] == "thumbnail.png", f"thumbnail must be first, got {names}"
        info = zf.getinfo("thumbnail.png")
        assert info.compress_type == zipfile.ZIP_STORED, "thumbnail must be STORED (uncompressed)"
        for required in ("meta.json", "document.json", "geometry.step"):
            assert required in names, f"missing required entry {required}"

    # Open -> doc JSON preserved, revision RESETS to 0 (fresh session), path recorded.
    s2, warnings = stepplus.open(path)
    assert warnings == [], warnings
    assert s2.revision == 0, "open must reset revision to 0 cleanly"
    assert s2.doc["features"][0]["params"]["height"]["value"] == 25, "doc JSON must roundtrip"
    assert s2.doc["name"] == s.doc["name"]
    assert s2.path == path
    print(f"[stepplus] save->open roundtrip: doc JSON preserved, revision reset 7->0, "
          f"thumbnail STORED first")


def test_unknown_entries_preserved_on_resave() -> None:
    s = _box_session()
    tmpdir = tempfile.mkdtemp()
    path = os.path.join(tmpdir, "model.step+")
    stepplus.save(s, path)

    # Inject an unknown future entry into the saved file.
    payload = b'{"design_intent": "future AI memory"}'
    with zipfile.ZipFile(path, "a") as zf:
        zf.writestr("ai/memory.json", payload)

    # Re-open (carries unknown entries onto the session) then resave; the entry must survive.
    s2, _ = stepplus.open(path)
    assert "ai/memory.json" in s2.extra_entries, "open must carry unknown entries onto the session"
    path2 = os.path.join(tmpdir, "model2.step+")
    stepplus.save(s2, path2)
    with zipfile.ZipFile(path2) as zf:
        assert "ai/memory.json" in zf.namelist(), "unknown entry must survive resave"
        assert zf.read("ai/memory.json") == payload, "unknown entry bytes preserved"
    print("[stepplus] unknown zip entry (ai/memory.json) preserved across open->resave")


def test_open_rejects_bad_container() -> None:
    tmpdir = tempfile.mkdtemp()
    path = os.path.join(tmpdir, "bad.step+")
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("meta.json", '{"container": "aicad.step+/2"}')
        zf.writestr("document.json", "{}")
    try:
        stepplus.open(path)
        raise AssertionError("unknown MAJOR container version must be rejected")
    except ValueError as e:
        assert "major" in str(e).lower() or "version" in str(e).lower(), str(e)
    print("[stepplus] open rejects unknown major container version")


def main() -> None:
    test_save_open_roundtrip()
    test_unknown_entries_preserved_on_resave()
    test_open_rejects_bad_container()
    print("\nSTEP+ CONTAINER TESTS PASSED")


if __name__ == "__main__":
    main()
