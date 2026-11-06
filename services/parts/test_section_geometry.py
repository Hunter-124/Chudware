"""Section cards retain the structural-workspace reference and catalog dimensions."""

import math

import pytest

from services.parts.adapters.structural_sections_adapter import StructuralSectionsAdapter
from services.structural.sections import get_aisc_section, get_lumber_section


@pytest.mark.parametrize(
    "designation,dimensions",
    [
        # AISC v16 inch values converted to mm, mm² and mm⁴.
        ("C6x13", {"d": 6 * 25.4, "bf": 2.16 * 25.4, "tf": 0.343 * 25.4,
                    "tw": 0.437 * 25.4, "A": 3.82 * 25.4**2, "Ix": 17.3 * 25.4**4}),
        ("W4x13", {"d": 4.16 * 25.4, "bf": 4.06 * 25.4, "tf": 0.345 * 25.4,
                    "tw": 0.280 * 25.4, "A": 3.83 * 25.4**2, "Ix": 11.3 * 25.4**4}),
        ("W8x31", {"d": 8 * 25.4, "bf": 8 * 25.4, "tf": 0.435 * 25.4,
                    "tw": 0.285 * 25.4, "A": 9.13 * 25.4**2, "Ix": 110 * 25.4**4}),
        ("S8x23", {"d": 8 * 25.4, "bf": 4.17 * 25.4, "tf": 0.425 * 25.4,
                    "tw": 0.441 * 25.4, "A": 6.76 * 25.4**2, "Ix": 64.7 * 25.4**4}),
    ],
)
def test_aisc_cards_reference_their_catalog_dimensions(designation, dimensions):
    record = StructuralSectionsAdapter().get(f"section:AISC360:{designation}")
    assert record is not None
    assert record["domain"] == "section"
    assert record["geometry_ref"] == {
        "kind": "section_profile", "standard": "AISC360", "designation": designation,
    }
    assert record["params"]["standard"] == "AISC360"
    assert record["params"]["nominal_size"] == designation
    assert record["license"] == "clean-room"
    assert "curated table" in record["provenance"]
    lookup = get_aisc_section(designation)
    for key, value in dimensions.items():
        assert record["params"][key] == pytest.approx(value, rel=0.003)
        assert lookup[key] == pytest.approx(value, rel=0.003)
    # Preserve catalog section properties as data, not as a claim that the
    # generated idealized polygon has precisely the catalog area/inertia.
    for key, value in lookup.items():
        assert record["params"][key] == value


@pytest.mark.parametrize(
    "designation,breadth,depth",
    [("2x4", 38, 89), ("4x6", 89, 140), ("6x12", 140, 286)],
)
def test_lumber_cards_use_actual_dressed_dimensions(designation, breadth, depth):
    record = StructuralSectionsAdapter().get(f"section:NDS:{designation}")
    assert record is not None
    assert record["domain"] == "section"
    assert record["geometry_ref"] == {
        "kind": "section_profile", "standard": "NDS", "designation": designation,
    }
    assert record["params"]["standard"] == "NDS"
    assert record["params"]["nominal_size"] == designation
    assert record["params"]["b_actual_mm"] == breadth
    assert record["params"]["d_actual_mm"] == depth
    assert record["license"] == "public-domain"
    assert "PS 20" in record["provenance"]
    lookup = get_lumber_section(designation)
    assert lookup["b_actual_mm"] == breadth
    assert lookup["d_actual_mm"] == depth
    for prop, card_prop in (("A", "A_mm2"), ("Ix", "Ix_mm4"), ("Sx", "Sx_mm3"),
                            ("Iy", "Iy_mm4"), ("Sy", "Sy_mm3")):
        assert lookup[prop] == record["params"][card_prop]


def test_catalog_sections_have_complete_positive_dimensions_and_matching_references():
    records = list(StructuralSectionsAdapter().iter_records())
    assert {record["standard"] for record in records} == {"AISC360", "NDS"}
    for record in records:
        standard = record["standard"]
        designation = record["designation"]
        assert record["part_id"] == f"section:{standard}:{designation}"
        assert record["geometry_ref"] == {
            "kind": "section_profile", "standard": standard, "designation": designation,
        }
        if standard == "AISC360":
            props = get_aisc_section(designation)
            keys = ("d", "bf", "tf", "tw")
            assert designation[0] in "WSC"
            assert 2 * props["tf"] < props["d"]
            assert props["tw"] < props["bf"]
        else:
            props = get_lumber_section(designation)
            keys = ("b_actual_mm", "d_actual_mm")
        for key in keys:
            assert math.isfinite(props[key]) and props[key] > 0
            assert record["params"][key] == props[key]


def test_every_aisc_section_places_with_matching_native_catalog_properties():
    from services.mcp import core_bridge

    if not core_bridge.available():
        pytest.skip("native core unavailable")
    records = [record for record in StructuralSectionsAdapter().iter_records()
               if record["standard"] == "AISC360"]
    assert records
    for record in records:
        result = core_bridge.call("part_place", {"part_id": record["part_id"], "record": record})
        assert result["ok"] and result["editable"], (record["part_id"], result)
        doc_id = result["doc_id"]
        try:
            specs = result["detail"]["meta"]["specs"]
            catalog = get_aisc_section(record["designation"])
            assert specs["catalog_properties"] == pytest.approx(catalog)
            bbox = core_bridge.call("doc_bbox", {"doc_id": doc_id})
            extents = [hi - lo for lo, hi in zip(bbox["min"], bbox["max"])]
            assert extents == pytest.approx([catalog["bf"], catalog["d"], 3 * catalog["d"]],
                                            abs=1e-3)
        finally:
            core_bridge.call("doc_close", {"doc_id": doc_id})


def test_structural_service_lookups_accept_placement_prefixes():
    assert get_aisc_section("AISC360:C6x13") == get_aisc_section("C6x13")
    assert get_aisc_section("AISC:W8x31") == get_aisc_section("W8x31")
    assert get_lumber_section("NDS:2x4") == get_lumber_section("2x4")


@pytest.mark.parametrize("part_id", ["section:AISC360:C999x999", "section:NDS:3x7"])
def test_unknown_sections_are_not_fabricated(part_id):
    assert StructuralSectionsAdapter().get(part_id) is None
