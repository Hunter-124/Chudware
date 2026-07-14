"""Watched parts-library scanner tests.

Pure-metadata: dummy STEP text files (no geometry) so this runs without OCP/build123d.
Mirrors test_vendor_parts.py by monkeypatching the registry paths to a temp dir.
"""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

from engine import parts_library, vendor_parts


_STEP = "ISO-10303-21;\nEND-ISO-10303-21;\n"


def _isolate_registry(root: Path) -> None:
    vendor_parts.paths.VENDOR_PARTS = root / "vendor_parts"
    vendor_parts.INDEX = vendor_parts.paths.VENDOR_PARTS / "index.json"


def test_scan_registers_and_is_idempotent() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _isolate_registry(root)
        lib = root / "library"
        (lib / "fasteners").mkdir(parents=True)
        (lib / "fasteners" / "M3x10.step").write_text(_STEP, encoding="utf-8")
        (lib / "washers").mkdir(parents=True)
        (lib / "washers" / "flat_washer.step").write_text(_STEP, encoding="utf-8")
        (lib / "loose.step").write_text(_STEP, encoding="utf-8")  # no subfolder -> "library"

        first = parts_library.scan_library_folder(str(lib))
        assert first["scanned"] == 3
        assert first["added"] == 3
        assert first["updated"] == 0

        rows = vendor_parts.list_parts()
        assert len(rows) == 3
        # supplier inferred from top-level subfolder, "library" when none
        suppliers = {r["supplier"] for r in rows}
        assert suppliers == {"fasteners", "washers", "library"}

        # specs parsed from filename "M3x10"
        screw = vendor_parts.get_part("fasteners", "M3x10")
        assert screw["specs"]["thread"] == "M3"
        assert screw["specs"]["length"] == 10
        assert screw["source_mtime"] > 0
        # registered in place (no copy) -> path points inside the library folder
        assert Path(screw["cad_path"]).resolve() == (lib / "fasteners" / "M3x10.step").resolve()

        # Re-scan with nothing changed -> idempotent (no dupes, nothing added/updated).
        second = parts_library.scan_library_folder(str(lib))
        assert second["scanned"] == 3
        assert second["added"] == 0
        assert second["updated"] == 0
        assert len(vendor_parts.list_parts()) == 3


def test_scan_detects_changed_mtime() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _isolate_registry(root)
        lib = root / "library"
        lib.mkdir(parents=True)
        f = lib / "part.step"
        f.write_text(_STEP, encoding="utf-8")

        assert parts_library.scan_library_folder(str(lib))["added"] == 1

        # Bump mtime into the future so the change is unambiguous on coarse-resolution FS.
        future = time.time() + 100
        os.utime(f, (future, future))

        res = parts_library.scan_library_folder(str(lib))
        assert res["added"] == 0
        assert res["updated"] == 1
        assert len(vendor_parts.list_parts()) == 1  # still a single record


def test_worker_method_registered() -> None:
    assert "vendor_part_scan_library" in vendor_parts.WORKER_METHODS


def test_parse_specs_from_name() -> None:
    assert parts_library.parse_specs_from_name("M3x10") == {"thread": "M3", "length": 10}
    assert parts_library.parse_specs_from_name("M6-1.0x20") == {
        "thread": "M6", "pitch": 1.0, "length": 20}
    assert parts_library.parse_specs_from_name("flat_washer") == {}


def test_scanned_step_is_searchable_via_parts_hub(tmp_path, monkeypatch) -> None:
    """A dropped local STEP becomes searchable by title/part-number after scan,
    and part_place routes it as import_file (editable)."""
    _isolate_registry(tmp_path)
    lib = tmp_path / "lib"
    lib.mkdir()
    step = lib / "CustomBracket-A1.step"
    step.write_text(_STEP, encoding="utf-8")

    # Avoid full hub rebuild thrash against the real bolts tables during unit test:
    # rebuild_index is still invoked; we just ensure the adapter sees the registry.
    rebuilt: list[int] = []

    def _rebuild():
        rebuilt.append(1)
        return 0

    monkeypatch.setattr("engine_oracle.parts_hub.rebuild_index", _rebuild, raising=False)
    # parts_library imports rebuild lazily inside the function; patch the hub module.
    import engine_oracle.parts_hub as ph
    monkeypatch.setattr(ph, "rebuild_index", _rebuild)

    result = parts_library.scan_library_folder(str(lib))
    assert result["added"] == 1
    assert rebuilt, "scan must refresh the PartsHub index"

    from engine_oracle.parts_hub.adapters.local_library_adapter import LocalLibraryAdapter
    adapter = LocalLibraryAdapter()
    recs = list(adapter.iter_records())
    assert recs, "local library adapter saw no records after scan"
    assert any("CustomBracket-A1" in (r.get("title") or "") for r in recs)
    hits = adapter.search("CustomBracket")
    assert hits and hits[0]["geometry_ref"]["kind"] == "import_file"
    assert hits[0]["geometry_ref"]["path"] == str(step.resolve())

    # Removal invalidates: delete the file → adapter drops it.
    step.unlink()
    assert list(adapter.iter_records()) == []


def test_local_library_place_is_import_file(tmp_path, monkeypatch) -> None:
    """part_place on a library: record must route through import_file."""
    _isolate_registry(tmp_path)
    src = tmp_path / "Widget-X.step"
    src.write_text(_STEP, encoding="utf-8")
    # Suppress hub rebuild during pure-metadata registration.
    import engine_oracle.parts_hub as ph
    monkeypatch.setattr(ph, "rebuild_index", lambda: 0)

    vendor_parts.register_local_cad(
        supplier="library", part_number="Widget-X",
        description="Widget X", cad_path=str(src), copy_file=False,
    )
    from engine_oracle.parts_hub.adapters.local_library_adapter import LocalLibraryAdapter
    adapter = LocalLibraryAdapter()
    part = next(iter(adapter.iter_records()), None)
    assert part is not None
    assert part["geometry_ref"]["kind"] == "import_file"
    assert part["geometry_ref"]["path"] == str(src.resolve())

    from services.mcp import core_bridge as cb
    monkeypatch.setattr(
        cb, "call",
        lambda verb, params: {"doc_id": "doc-lib-1", "body_count": 1}
        if verb == "import_file" else {},
    )
    from engine_oracle.parts_hub.place import place
    monkeypatch.setattr(ph, "get", lambda pid: part)
    r = place(part["part_id"])
    assert r["ok"] is True, f"place failed: {r}"
    assert r["editable"] is True
    assert r["doc_id"] == "doc-lib-1"

def main() -> None:
    test_scan_registers_and_is_idempotent()
    test_scan_detects_changed_mtime()
    test_worker_method_registered()
    test_parse_specs_from_name()
    print("PARTS LIBRARY TESTS PASSED")


if __name__ == "__main__":
    main()
