"""Behavioral checks for cad_guide topic aliases."""

from services.mcp.cad_guide import cad_guide_text


def test_thickness_guide_aliases_route_to_evaluate():
    text = cad_guide_text("evaluate")
    assert cad_guide_text("thickness") == text
    assert cad_guide_text("wall_thickness") == text


def test_sketch_tool_names_route_to_the_sketch_topic():
    text = cad_guide_text("sketch")
    for alias in ("sketch_probe", "sketch_health", "check_sketch", "sketch_fully_define"):
        assert cad_guide_text(alias) == text


def test_stress_guide_alias():
    assert cad_guide_text("stress-concentration") == cad_guide_text("stress_risers")
