"""Vendor part cache tests.

These avoid importing CAD geometry so they can run without OCP/build123d. They verify the
metadata/cache/ops layer that local workflows use before the import feature compiles geometry.
"""
from __future__ import annotations

import tempfile
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


def main() -> None:
    test_register_list_context_and_import_ops()
    print("VENDOR PART TESTS PASSED")


if __name__ == "__main__":
    main()

