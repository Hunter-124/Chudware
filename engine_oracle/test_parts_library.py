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


def main() -> None:
    test_scan_registers_and_is_idempotent()
    test_scan_detects_changed_mtime()
    test_worker_method_registered()
    test_parse_specs_from_name()
    print("PARTS LIBRARY TESTS PASSED")


if __name__ == "__main__":
    main()
