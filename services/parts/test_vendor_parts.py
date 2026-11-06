"""Vendor part cache tests.

These avoid importing CAD geometry so they can run without OCP/build123d. They verify the
metadata/cache/ops layer that local workflows use before the import feature compiles geometry.
"""
from __future__ import annotations

import tempfile
from concurrent.futures import ThreadPoolExecutor
import pytest
from pathlib import Path

from services.parts import vendor_parts


def test_register_list_context_and_import_ops() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        vendor_parts.paths.VENDOR_PARTS = root / "vendor_parts"
        vendor_parts.INDEX = vendor_parts.paths.VENDOR_PARTS / "index.json"
        src = root / "91251A542.step"
        src.write_text("ISO-10303-21;\nEND-ISO-10303-21;\n", encoding="utf-8")

        rec = vendor_parts.register_local_cad(
            supplier="McMaster-Carr",
            part_number="91251A542",
            description="Alloy steel socket head screw",
            product_url="https://www.mcmaster.com/91251A542",
            cad_path=str(src),
            specs={"thread": "M3", "length": "10 mm"},
        )

        assert rec["supplier"] == "McMaster-Carr"
        assert rec["cad_format"] == "step"
        assert Path(rec["cad_path"]).exists()

        rows = vendor_parts.list_parts(query="socket")
        assert len(rows) == 1
        assert rows[0]["part_number"] == "91251A542"

        card = vendor_parts.context_card(query="M3")
        assert "91251A542" in card["llm_context"]
        assert card["capabilities"]

        ops = vendor_parts.import_ops_for_part("McMaster-Carr", "91251A542")
        feat = ops[0]["feature"]
        assert feat["type"] == "import"
        assert feat["params"]["vendor_supplier"] == "McMaster-Carr"
        assert feat["params"]["vendor_part_number"] == "91251A542"


def test_dot_only_vendor_components_stay_inside_vendor_library(tmp_path, monkeypatch) -> None:
    root = tmp_path / "vendor_parts"
    monkeypatch.setattr(vendor_parts.paths, "VENDOR_PARTS", root)
    monkeypatch.setattr(vendor_parts, "INDEX", root / "index.json")
    src = tmp_path / "part.step"
    src.write_text("STEP data", encoding="utf-8")

    for value in (".", "..", "...", "-..-"):
        assert vendor_parts.slug(value) == "part"
    rec = vendor_parts.register_local_cad(
        supplier="..", part_number="..", cad_path=str(src),
    )
    dest = Path(rec["cad_path"]).resolve()
    assert dest.is_relative_to(root.resolve())
    assert dest == root / "part" / "part" / src.name

    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "unsafe").symlink_to(outside, target_is_directory=True)
    try:
        vendor_parts.register_local_cad(
            supplier="unsafe", part_number="item", cad_path=str(src),
        )
    except ValueError as exc:
        assert "outside the vendor library" in str(exc)
    else:
        raise AssertionError("symlinked vendor directory escaped the library")
    assert not (outside / "item" / src.name).exists()
    assert dest.read_text() == "STEP data"
    assert not (tmp_path / "part" / src.name).exists()


def test_step_plus_rescan_preserves_native_price_and_unknown_metadata(tmp_path, monkeypatch):
    from services.parts import parts_library
    root = tmp_path / "vendor"
    monkeypatch.setattr(vendor_parts.paths, "VENDOR_PARTS", root)
    monkeypatch.setattr(vendor_parts, "INDEX", root / "index.json")
    monkeypatch.setattr(vendor_parts, "_rebuild_parts_index", lambda: None)
    lib = tmp_path / "watched"
    lib.mkdir()
    src = lib / "widget.step+"
    src.write_text("native part")
    parts_library.scan_library_folder(str(lib))
    idx = vendor_parts._load_index()
    row = idx["library::widget"]
    row.update({"unit_price": 17.5, "currency": "EUR", "stock": 4,
                "provenance": {"source": "user"}, "extra": {"nested": 42},
                "description": "Detailed human description"})
    vendor_parts._write_index(idx)
    vendor_parts.register_local_cad(supplier="library", part_number="widget", cad_path=str(src),
                                    copy_file=False, source="library")
    parts_library.scan_library_folder(str(lib))
    row = vendor_parts.get_part("library", "widget")
    assert row["cad_format"] == "step+"
    assert row["unit_price"] == 17.5 and row["currency"] == "EUR" and row["stock"] == 4
    assert row["provenance"] == {"source": "user"} and row["extra"] == {"nested": 42}
    assert row["description"] == "Detailed human description"


def test_windows_registry_lock_does_not_read_locked_byte(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    root = tmp_path / "vendor"
    monkeypatch.setattr(vendor_parts, "INDEX", root / "index.json")
    monkeypatch.setattr(vendor_parts, "os", SimpleNamespace(name="nt"))
    calls = []
    fake = SimpleNamespace(LK_LOCK=1, LK_UNLCK=2,
                           locking=lambda fd, mode, length: calls.append((mode, length)))
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    with vendor_parts._registry_lock():
        assert calls == [(fake.LK_LOCK, 1)]
    assert calls == [(fake.LK_LOCK, 1), (fake.LK_UNLCK, 1)]
    assert (root / "index.json.lock").read_bytes() == b""


def test_rejected_batch_never_overwrites_existing_cad(tmp_path, monkeypatch):
    root = tmp_path / "vendor"
    monkeypatch.setattr(vendor_parts.paths, "VENDOR_PARTS", root)
    monkeypatch.setattr(vendor_parts, "INDEX", root / "index.json")
    monkeypatch.setattr(vendor_parts, "_rebuild_parts_index", lambda: None)
    first_dir, second_dir = tmp_path / "first", tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first, second = first_dir / "model.step", second_dir / "model.step"
    first.write_text("original")
    second.write_text("replacement")
    original = vendor_parts.register_local_cad(supplier="Vendor A", part_number="x", cad_path=str(first))
    before = vendor_parts._load_index()
    with pytest.raises(ValueError, match="collision"):
        vendor_parts.register_local_cads([
            {"supplier": "new", "part_number": "first", "cad_path": str(first)},
            {"supplier": "Vendor-A", "part_number": "x", "cad_path": str(second)},
        ])
    assert vendor_parts._load_index() == before
    assert Path(original["cad_path"]).read_text() == "original"
    assert not (root / "new" / "first" / "model.step").exists()


def test_concurrent_registration_and_slug_collision(tmp_path, monkeypatch):
    root = tmp_path / "vendor"
    monkeypatch.setattr(vendor_parts.paths, "VENDOR_PARTS", root)
    monkeypatch.setattr(vendor_parts, "INDEX", root / "index.json")
    monkeypatch.setattr(vendor_parts, "_rebuild_parts_index", lambda: None)
    src = tmp_path / "part.step"
    src.write_text("STEP")
    def register(n):
        return vendor_parts.register_local_cad(supplier="vendor", part_number=f"p{n}",
                                               cad_path=str(src), copy_file=False)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(register, range(40)))
    assert len(vendor_parts.list_parts()) == 40
    vendor_parts.register_local_cad(supplier="Vendor A", part_number="x",
                                    cad_path=str(src), copy_file=False)
    with pytest.raises(ValueError, match="collision"):
        vendor_parts.register_local_cad(supplier="Vendor-A", part_number="x",
                                        cad_path=str(src), copy_file=False)


def main() -> None:
    test_register_list_context_and_import_ops()
    print("VENDOR PART TESTS PASSED")


if __name__ == "__main__":
    main()

