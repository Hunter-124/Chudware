"""cad_guide topics the HERC audit found missing, and the paged operation compendium."""
from __future__ import annotations

from services.mcp.cad_guide import cad_guide_text


def _missing(text: str) -> bool:
    return text.startswith("No guide for")


def test_audit_topics_resolve():
    for topic in ("transform", "plane", "planes", "primitive", "primitives", "sphere",
                  "material", "materials", "set_material", "weld_member",
                  "asm_edit_component", "centroid", "regions"):
        assert not _missing(cad_guide_text(topic)), topic
    assert cad_guide_text("weld_member") == cad_guide_text("weldment")
    assert cad_guide_text("sphere") == cad_guide_text("primitives")
    assert cad_guide_text("plane") == cad_guide_text("planes")
    assert cad_guide_text("set_material") == cad_guide_text("material")


def test_topics_state_the_engine_conventions():
    planes = cad_guide_text("planes")
    assert "-Y" in planes and "offset:-1385" in planes and "NOT the top face" in planes
    prims = cad_guide_text("primitives")
    assert "CENTRE" in prims and "CURRENT body only" in prims and "merge:false" in prims
    transform = cad_guide_text("transform")
    assert "transform takes no refs" in transform and '"b1"' in transform
    assert "rotate about `pivot` first, then translate" in transform
    pose = cad_guide_text("asm_edit_component")
    assert "DEGREES" in pose and "REPLACES" in pose
    regions = cad_guide_text("centroid")
    assert "LARGER-AREA" in regions and "INNER DISC" in regions
    material = cad_guide_text("material")
    assert '"key"' in material and "Property table" in material


def test_ops_is_a_readable_index_with_full_pages():
    index = cad_guide_text("ops")
    assert len(index) < 20_000
    full = cad_guide_text("ops_full")
    assert len(full) > 2 * len(index)
    for page in ("ops_workflow", "ops_features", "ops_sketch", "ops_vocab"):
        text = cad_guide_text(page)
        assert text and not _missing(text) and len(text) < 30_000, page
        assert page in index
    assert cad_guide_text("ops_workflow") in full
    assert cad_guide_text("ops_sketch").startswith("-----") and "SKETCH OPS" in full
