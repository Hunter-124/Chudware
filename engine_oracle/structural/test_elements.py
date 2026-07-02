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
