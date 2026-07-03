"""Tests for structural cost takeoff — estimate.py and bom_bridge.py (T-D5).

Fully offline: no DLL, no network, no API keys.

Covers:
- board_feet() formula correctness (DoD gate: 4x4 × 8ft = 10.67 BF)
- steel_weight_lbs() from AISC lbs/ft table
- fastener_count() per element type
- takeoff() produces correct line items for a mixed structure
- estimate() plausibility gate: cedar post total in $50–200
- bom_bridge.structural_bom_lines() returns BomLine-compatible output
- integration demo: cedar post under sensor-enclosure load
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from engine_oracle.structural.elements import (
    Beam, Column, Joist, Post, StructureDoc, StudWall, make_element,
)
from engine_oracle.structural.estimate import (
    DISCLAIMER,
    TakeoffLine,
    board_feet,
    estimate,
    fastener_count,
    steel_weight_lbs,
    takeoff,
)


# --------------------------------------------------------------------------- fixtures

def _cedar_post(length_mm: float = 2438.4) -> Post:
    """4x4 cedar post, default 8ft (2438.4 mm = 8 × 304.8)."""
    return Post(section_name="4x4", material="cedar", length_mm=length_mm)


def _simple_doc() -> StructureDoc:
    """A minimal one-element structure doc (4×4 cedar post, 8 ft)."""
    doc = StructureDoc(name="TestPost")
    doc.add(_cedar_post())
    return doc


def _mixed_doc() -> StructureDoc:
    """A multi-element structure doc: cedar post + W8X31 steel beam + 2 joists."""
    doc = StructureDoc(name="Mixed")
    doc.add(Post(section_name="4x4", material="cedar", length_mm=2438.4))     # 8 ft
    doc.add(Beam(section_name="W8X31", material="steel", length_mm=3048.0))   # 10 ft
    doc.add(Joist(section_name="2x10", material="lumber", length_mm=3657.6))  # 12 ft
    doc.add(Joist(section_name="2x10", material="lumber", length_mm=3657.6))  # 12 ft
    return doc


# --------------------------------------------------------------------------- board_feet

class TestBoardFeet:
    def test_4x4_8ft_dod_gate(self):
        """DoD: 4x4 × 8ft = 10.67 BF (rounded to 2 dp)."""
        post = _cedar_post(length_mm=8.0 * 304.8)  # exactly 8.0 ft
        bf = board_feet(post)
        assert bf == pytest.approx(10.6667, abs=0.01)

    def test_2x10_12ft(self):
        """2x10 × 12ft: (2×10×12)/12 = 20.0 BF."""
        joist = Joist(section_name="2x10", material="lumber", length_mm=12.0 * 304.8)
        assert board_feet(joist) == pytest.approx(20.0, abs=0.01)

    def test_4x4_5ft(self):
        """4x4 × 5ft: (4×4×5)/12 ≈ 6.667 BF."""
        post = Post(section_name="4x4", material="cedar", length_mm=5.0 * 304.8)
        assert board_feet(post) == pytest.approx(6.6667, abs=0.01)

    def test_6x6_10ft(self):
        """6x6 × 10ft: (6×6×10)/12 = 30.0 BF."""
        col = Column(section_name="6x6", material="Douglas Fir", length_mm=10.0 * 304.8)
        assert board_feet(col) == pytest.approx(30.0, abs=0.01)

    def test_invalid_section_raises_valueerror(self):
        """Non-parseable section name raises ValueError."""
        post = Post(section_name="W8X31", material="steel", length_mm=2438.4)
        with pytest.raises(ValueError, match="Cannot parse nominal lumber size"):
            board_feet(post)


# --------------------------------------------------------------------------- steel_weight_lbs

class TestSteelWeight:
    def test_w8x31_10ft(self):
        """W8X31 × 10ft = 31 lbs/ft × 10 ft = 310 lbs."""
        beam = Beam(section_name="W8X31", material="steel", length_mm=10.0 * 304.8)
        assert steel_weight_lbs(beam) == pytest.approx(310.0, abs=0.01)

    def test_w12x35_8ft(self):
        """W12X35 × 8ft = 35 × 8 = 280 lbs."""
        beam = Beam(section_name="W12X35", material="steel", length_mm=8.0 * 304.8)
        assert steel_weight_lbs(beam) == pytest.approx(280.0, abs=0.01)

    def test_unknown_section_raises_keyerror(self):
        beam = Beam(section_name="W99X999", material="steel", length_mm=3000.0)
        with pytest.raises(KeyError, match="W99X999"):
            steel_weight_lbs(beam)


# --------------------------------------------------------------------------- fastener_count

class TestFastenerCount:
    def test_single_post(self):
        """One post: 4 base + 4 cap = 8 fasteners."""
        doc = StructureDoc()
        doc.add(_cedar_post())
        assert fastener_count(doc) == 8

    def test_single_joist(self):
        """One joist: 2 hangers × 10 = 20 fasteners."""
        doc = StructureDoc()
        doc.add(Joist(section_name="2x10", material="lumber", length_mm=3000.0))
        assert fastener_count(doc) == 20

    def test_single_beam(self):
        """One beam: 1 splice = 6 fasteners."""
        doc = StructureDoc()
        doc.add(Beam(section_name="W8X31", material="steel", length_mm=3000.0))
        assert fastener_count(doc) == 6

    def test_mixed_doc_fasteners(self):
        """Mixed doc: 1 post (8) + 1 beam (6) + 2 joists (20+20) = 54."""
        doc = _mixed_doc()
        assert fastener_count(doc) == 54

    def test_empty_doc(self):
        """Empty structure: 0 fasteners."""
        doc = StructureDoc()
        assert fastener_count(doc) == 0


# --------------------------------------------------------------------------- takeoff

class TestTakeoff:
    def test_cedar_post_line(self):
        """Cedar post takeoff line: BF quantity, BF unit, non-zero material cost."""
        doc = _simple_doc()
        lines = takeoff(doc)
        assert len(lines) == 1
        ln = lines[0]
        assert ln.element_type == "post"
        assert ln.unit == "BF"
        assert ln.quantity == pytest.approx(10.6667, abs=0.01)
        assert ln.material_cost > 0
        assert ln.labor_cost > 0

    def test_steel_beam_line(self):
        """W8X31 beam takeoff line: weight in lbs, non-zero material cost."""
        doc = StructureDoc()
        doc.add(Beam(section_name="W8X31", material="steel", length_mm=10.0 * 304.8))
        lines = takeoff(doc)
        assert len(lines) == 1
        ln = lines[0]
        assert ln.unit == "lbs"
        assert ln.quantity == pytest.approx(310.0, abs=0.5)
        assert ln.material_cost > 0

    def test_takeoff_returns_takeoff_line_objects(self):
        doc = _mixed_doc()
        lines = takeoff(doc)
        assert all(isinstance(ln, TakeoffLine) for ln in lines)
        assert len(lines) == 4  # 1 post + 1 beam + 2 joists


# --------------------------------------------------------------------------- estimate (DoD gate)

class TestEstimate:
    def test_cedar_post_cost_in_range(self):
        """DoD: cedar post estimate total in $50–200."""
        doc = _simple_doc()
        result = estimate(doc)
        assert 50.0 <= result.total_cost <= 200.0, (
            f"Expected $50–200 for cedar post estimate, got ${result.total_cost:.2f}"
        )

    def test_estimate_has_required_fields(self):
        doc = _simple_doc()
        result = estimate(doc)
        assert result.total_material_cost >= 0
        assert result.total_labor_cost >= 0
        assert result.total_cost == pytest.approx(
            result.total_material_cost + result.total_labor_cost, abs=0.01
        )
        assert result.currency == "USD"
        assert "ENR" in result.rate_table_date
        assert DISCLAIMER in result.disclaimer

    def test_estimate_includes_fastener_line(self):
        doc = _simple_doc()
        result = estimate(doc)
        fl = result.fastener_line
        assert fl.element_id == "__fasteners__"
        assert fl.quantity == 8.0  # 1 post = 8 fasteners
        assert fl.material_cost > 0

    def test_estimate_line_items_count(self):
        """Mixed doc: 4 elements → 4 line items (fastener line is separate)."""
        doc = _mixed_doc()
        result = estimate(doc)
        assert len(result.line_items) == 4


# --------------------------------------------------------------------------- bom_bridge

class TestBomBridge:
    def test_structural_bom_lines_returns_bom_lines(self):
        from engine_oracle.bom import BomLine
        from engine_oracle.structural.bom_bridge import structural_bom_lines

        doc = _simple_doc()
        bom_lines = structural_bom_lines(doc)
        assert len(bom_lines) == 2  # 1 element + fasteners
        for bl in bom_lines:
            assert isinstance(bl, BomLine)
            assert bl.kind == "made"
            assert bl.currency == "USD"

    def test_bom_lines_have_cost_model(self):
        from engine_oracle.structural.bom_bridge import structural_bom_lines

        doc = _simple_doc()
        bom_lines = structural_bom_lines(doc)
        for bl in bom_lines:
            assert bl.cost_model is not None
            assert "source" in bl.cost_model
            assert bl.cost_model["source"] == "struct_estimate"

    def test_structural_bom_dicts_consumable_by_bom_rollup(self):
        """structural_bom_dicts() output can be consumed by services.deliverables.joins.bom_rollup."""
        from engine_oracle.structural.bom_bridge import structural_bom_dicts

        doc = _simple_doc()
        dicts = structural_bom_dicts(doc)
        assert len(dicts) == 2
        for d in dicts:
            assert "item" in d
            assert "qty" in d
            # unit_cost present for priced lines (post is lumber — should be priced)
            assert "unit_cost" in d

    def test_bom_rollup_accepts_structural_output(self):
        """End-to-end: structural_bom_dicts passes through bom_rollup without error."""
        from engine_oracle.structural.bom_bridge import structural_bom_dicts

        doc = _simple_doc()
        dicts = structural_bom_dicts(doc)
        # bom_rollup expects {item, qty, unit_cost?, source?}
        from services.deliverables.joins import bom_rollup
        result = bom_rollup(dicts)
        assert "block" in result
        assert "attached" in result


# --------------------------------------------------------------------------- integration demo

class TestIntegrationDemo:
    """Integration demo: cedar post under sensor-enclosure load (T-D5 DoD).

    Uses the doc written to a temp JSON file to exercise the full path:
    serialize → disk → struct_estimate → verify costs and format.
    """

    def _make_cedar_post_doc(self) -> dict:
        """4×4 cedar post, 8ft, with a DEAD_ONLY load case (sensor enclosure weight)."""
        doc = StructureDoc(name="SensorEnclosurePost")
        doc.add(Post(section_name="4x4", material="cedar", length_mm=8.0 * 304.8))
        d = doc.to_dict()
        # Add a simple dead load case matching D2/D4 demo convention
        d["load_cases"] = [{
            "id": "DEAD",
            "description": "Dead load — sensor enclosure",
            "nodal_loads": [
                {"node": "top", "Fx": 0, "Fy": 0, "Fz": -500, "Mx": 0, "My": 0, "Mz": 0}
            ]
        }]
        return d

    def test_cedar_post_estimate_plausible(self):
        """Cedar post estimate returns a plausible total in $50–200."""
        from engine_oracle.structural.elements import StructureDoc as SD
        from engine_oracle.structural.estimate import estimate

        doc = StructureDoc(name="Demo")
        doc.add(Post(section_name="4x4", material="cedar", length_mm=8.0 * 304.8))
        result = estimate(doc)

        # DoD gate: $50–200
        assert 50.0 <= result.total_cost <= 200.0, (
            f"Plausibility gate failed: total_cost=${result.total_cost:.2f} not in $50–200"
        )
        # Must have line items
        assert len(result.line_items) == 1
        post_line = result.line_items[0]
        assert post_line.element_type == "post"
        assert post_line.unit == "BF"
        assert post_line.quantity == pytest.approx(10.67, abs=0.01)
        assert result.disclaimer == DISCLAIMER

    def test_struct_estimate_via_json_file(self):
        """Write a cedar post doc to JSON, run the estimate engine from it."""
        from engine_oracle.structural.elements import StructureDoc as SD
        from engine_oracle.structural.estimate import estimate

        doc_dict = self._make_cedar_post_doc()
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as fh:
            json.dump(doc_dict, fh)
            fpath = fh.name

        try:
            # Reload from disk (same path the MCP tool takes)
            with open(fpath, encoding="utf-8") as fh:
                loaded = json.load(fh)
            doc = SD.from_dict(loaded)
            result = estimate(doc)
            assert result.total_cost > 0
            assert result.currency == "USD"
        finally:
            Path(fpath).unlink(missing_ok=True)
