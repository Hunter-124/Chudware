"""Tests for the structural element schema + OCCT B-rep generation (structural_schema.md).

The pure-data schema tests always run. The geometry tests require OCP (cadquery-ocp); they
``importorskip`` it so the suite stays green on a box without the OCCT binding, and exercise a real
solid (non-null + expected volume) where it is present.
"""

from __future__ import annotations

import math

import pytest

from engine.structural import elements as el


# --------------------------------------------------------------------------- schema / data


def test_package_importable_and_doc_kind():
    doc = el.new_structure("Frame")
    assert doc["schema"] == "aicad.structure/1"
    assert el.is_structure(doc)
    assert el.doc_kind(doc) == "structure"
    assert el.doc_kind({"schema": "aicad.pcb/1"}) == ""


def test_make_element_all_types():
    for et in el.ELEMENT_TYPES:
        e = el.make_element(et, "100x100", "cedar", 2400.0)
        assert e.element_type == et
        assert e.length_mm == 2400.0
        assert len(e.transform) == 16


def test_invalid_element_type_rejected():
    with pytest.raises(ValueError):
        el.make_element("truss", "100x100", "steel", 1000.0)


def test_structure_doc_add_get_roundtrip():
    doc = el.StructureDoc(name="Deck")
    post = doc.add(el.make_element("post", "100x100", "cedar", 2400.0))
    beam = doc.add(el.make_element("beam", "W200x100", "steel_a36", 3600.0))
    assert doc.get(post.id).material == "cedar"
    assert doc.get(beam.id).section_name == "W200x100"

    d = doc.to_dict()
    doc2 = el.StructureDoc.from_dict(d)
    assert len(doc2.elements) == 2
    assert doc2.get(post.id).length_mm == 2400.0
    assert doc2.to_dict() == d

    with pytest.raises(KeyError):
        doc.get("nope")


def test_from_dict_rejects_wrong_schema():
    with pytest.raises(ValueError):
        el.StructureDoc.from_dict({"schema": "aicad.pcb/1"})


# --------------------------------------------------------------------------- geometry (OCP)


@pytest.fixture()
def brep():
    pytest.importorskip("OCP")
    from engine.structural import brep as _brep
    return _brep


def test_element_to_brep_valid_for_each_type(brep):
    for et in el.ELEMENT_TYPES:
        section = "W200x100" if et in ("beam", "column") else "150x150"
        e = el.make_element(et, section, "steel_a36", 2400.0)
        shape = brep.element_to_brep(e)
        assert shape is not None and not shape.IsNull(), f"{et} produced a null shape"
        assert brep.volume_mm3(shape) > 0.0, f"{et} produced zero volume"


def test_post_prism_volume_matches_section(brep):
    # DoD smoke: 100×100 mm cedar post, 2400 mm tall → solid prism of exactly 100*100*2400 mm³.
    post = el.make_element("post", "100x100", "cedar", 2400.0)
    shape = brep.element_to_brep(post)
    assert not shape.IsNull()
    assert math.isclose(brep.volume_mm3(shape), 100.0 * 100.0 * 2400.0, rel_tol=1e-6)


def test_i_shape_volume_less_than_bounding_box(brep):
    # An I-section solid must have less volume than its enclosing rectangular prism.
    beam = el.make_element("beam", "I200x100x6x10", "steel_a36", 1000.0)
    shape = brep.element_to_brep(beam)
    vol = brep.volume_mm3(shape)
    assert 0.0 < vol < 200.0 * 100.0 * 1000.0


def test_section_profile_returns_wire(brep):
    from OCP.TopoDS import TopoDS_Wire
    for name in ("100x100", "RHS100x50x5", "W200x100", "I300x150x8x12", "garbage"):
        w = brep.section_profile(name, element_type="beam")
        assert isinstance(w, TopoDS_Wire)


def test_rhs_produces_true_hollow_prism_not_solid_envelope(brep):
    # T-BL21: RHS100x50x5 must be a true outer-minus-inner prism (100x50 outer, wall 5mm), not the
    # solid 100x50 envelope it used to extrude as.
    length = 2000.0
    e = el.make_element("beam", "RHS100x50x5", "steel_a36", length)
    shape = brep.element_to_brep(e)
    assert not shape.IsNull()
    vol = brep.volume_mm3(shape)
    solid_vol = 100.0 * 50.0 * length
    hollow_vol = (100.0 * 50.0 - 90.0 * 40.0) * length
    assert math.isclose(vol, hollow_vol, rel_tol=1e-6)
    assert vol < solid_vol * 0.5, "RHS solid should be far lighter than its solid outer envelope"


def test_hollow_wall_geometry_is_visible_to_interference_checks(brep):
    # T-BL21 DoD: check_interference (eval_interference) works by boolean-intersecting real solid
    # bodies, so it can only "see" a hollow member's true wall geometry if element_to_brep produces
    # one. Reproduce that boolean-common check directly: a 60x60 rod centred inside an
    # RHS100x100x10 tube's 80x80 void (10mm clearance to the wall on every side) must NOT overlap
    # the tube's solid material. Under the old solid-outer-envelope bug this would have reported a
    # full-cross-section false interference.
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common

    tube = el.make_element("column", "RHS100x100x10", "steel_a36", 1000.0)
    tube_shape = brep.element_to_brep(tube)
    rod = el.make_element("post", "60x60", "steel_a36", 1000.0)
    rod_shape = brep.element_to_brep(rod)

    common = BRepAlgoAPI_Common(tube_shape, rod_shape).Shape()
    assert brep.volume_mm3(common) < 1e-6, "rod in the tube's void must not interfere with its wall"


def test_fractional_imperial_hss_designation_does_not_fabricate_geometry(brep):
    # HSS4X4X1/4 is the actual catalog designation format used by estimate._AISC_LBS_PER_FT
    # (imperial, fractional wall). It is NOT parseable by this module's mm-based digit grammar —
    # naively extracting digits would mis-tokenize "1/4" into dims [1, 4] and silently produce a
    # plausible-but-wrong 4mm x 4mm x 1mm-wall tube. It must instead fall back to the safe
    # per-type default section rather than fabricate geometry (see work/questions/T-BL21.md).
    assert brep._parse_section("HSS4X4X1/4") is None
    e = el.make_element("column", "HSS4X4X1/4", "steel_a36", 1000.0)
    shape = brep.element_to_brep(e)
    assert not shape.IsNull()
    default_vol = brep.volume_mm3(
        brep.element_to_brep(el.make_element("column", "W200x100", "steel_a36", 1000.0))
    )
    assert math.isclose(brep.volume_mm3(shape), default_vol, rel_tol=1e-9)


def test_hss_alias_matches_rhs(brep):
    # HSS is parsed identically to RHS (same WxHxT grammar).
    a = brep.element_to_brep(el.make_element("column", "HSS100x100x6", "steel_a36", 1000.0))
    b = brep.element_to_brep(el.make_element("column", "RHS100x100x6", "steel_a36", 1000.0))
    assert math.isclose(brep.volume_mm3(a), brep.volume_mm3(b), rel_tol=1e-9)


def test_rhs_invalid_wall_thickness_falls_back_to_solid_envelope(brep):
    # A wall thickness that doesn't physically fit the outer envelope (t >= half the smaller outer
    # dim) degrades gracefully to the solid outer rectangle instead of a self-intersecting face.
    length = 1000.0
    e = el.make_element("beam", "RHS100x50x30", "steel_a36", length)
    shape = brep.element_to_brep(e)
    assert not shape.IsNull()
    assert math.isclose(brep.volume_mm3(shape), 100.0 * 50.0 * length, rel_tol=1e-6)


def test_chs_pipe_matches_asme_b36_10m_handbook_area(brep):
    # Reference: ASME B36.10M welded/seamless wrought steel pipe, NPS 4 Schedule 40 —
    # OD 4.500 in, wall 0.237 in, cross-sectional area 3.174 in^2 (bundled official dimensional
    # table via mcp__ai-cad__research_standard_section("pipe 4 sch 40")). A pipe's cross-section is
    # a true annulus, so our outer-minus-inner CHS model should match this to within noise.
    od_mm = 114.3           # 4.500 in
    wall_mm = 6.0198        # 0.237 in
    length_mm = 3048.0      # 10 ft, arbitrary
    handbook_area_mm2 = 3.174 * 25.4 * 25.4   # 3.174 in^2 -> mm^2

    e = el.make_element("column", f"CHS{od_mm}x{wall_mm}", "steel_a36", length_mm)
    shape = brep.element_to_brep(e)
    assert not shape.IsNull()
    area_mm2 = brep.volume_mm3(shape) / length_mm
    assert math.isclose(area_mm2, handbook_area_mm2, rel_tol=5e-3)


def test_pipe_alias_matches_chs(brep):
    a = brep.element_to_brep(el.make_element("post", "CHS114.3x6.02", "steel_a36", 1000.0))
    b = brep.element_to_brep(el.make_element("post", "PIPE114.3x6.02", "steel_a36", 1000.0))
    assert math.isclose(brep.volume_mm3(a), brep.volume_mm3(b), rel_tol=1e-9)


def test_section_profile_still_returns_outer_wire_only_for_hollow_forms(brep):
    from OCP.TopoDS import TopoDS_Wire
    for name in ("RHS100x50x5", "HSS100x100x6", "CHS114.3x6.02", "PIPE114.3x6.02"):
        w = brep.section_profile(name, element_type="beam")
        assert isinstance(w, TopoDS_Wire)


def test_transform_translation_places_solid(brep):
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    tf = list(el.IDENTITY_4X4)
    tf[3] = 500.0   # translate +500 in X (row-major top-right column, row 0)
    e = el.make_element("post", "100x100", "cedar", 2400.0, transform=tf)
    shape = brep.element_to_brep(e)
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    xmin, _, _, xmax, _, _ = box.Get()
    # Local post spans x∈[-50,50]; after +500 translation it should be centred near x=500.
    assert 440.0 < (xmin + xmax) / 2.0 < 560.0
