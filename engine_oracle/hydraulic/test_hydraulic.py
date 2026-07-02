"""Tests for engine.hydraulic — schema, solve, bom.

Run with:
    ~/.local/share/AI-CAD/mcp-venv/bin/pytest engine/hydraulic/ -v -q
"""

from __future__ import annotations

import math
import pytest

from engine.hydraulic import schema as hyd_schema
from engine.hydraulic import solve as hyd_solve
from engine.hydraulic import bom as hyd_bom


# ═══════════════════════════════════════════════════════════════════════════════
# Schema factories
# ═══════════════════════════════════════════════════════════════════════════════

class TestNewHydraulic:
    def test_schema_field(self):
        doc = hyd_schema.new_hydraulic("Test Circuit")
        assert doc["schema"] == hyd_schema.SCHEMA_HYD

    def test_name_field(self):
        doc = hyd_schema.new_hydraulic("My Circuit")
        assert doc["name"] == "My Circuit"

    def test_units_present(self):
        doc = hyd_schema.new_hydraulic()
        assert doc["units"]["pressure"] == "bar"
        assert doc["units"]["flow"] == "L/min"
        assert doc["units"]["force"] == "kN"
        assert doc["units"]["position"] == "mm"

    def test_uuid_id(self):
        doc = hyd_schema.new_hydraulic()
        assert isinstance(doc["id"], str) and len(doc["id"]) == 36

    def test_empty_lists(self):
        doc = hyd_schema.new_hydraulic()
        assert doc["components"] == []
        assert doc["hoses"] == []
        assert doc["annotations"] == []


class TestNewComponent:
    def test_basic_fields(self):
        c = hyd_schema.new_component("c1", "pump", name="Main Pump", x=100, y=200)
        assert c["id"] == "c1"
        assert c["type"] == "pump"
        assert c["name"] == "Main Pump"
        assert c["position"] == [100.0, 200.0]
        assert c["angle"] == 0.0

    def test_sourcing_defaults(self):
        c = hyd_schema.new_component("c1", "reservoir")
        sourcing = c["sourcing"]
        assert sourcing["mpn"] is None
        assert sourcing["manufacturer"] is None
        assert sourcing["quantity"] == 1
        assert sourcing["unit"] == "ea"

    def test_extra_kwargs_go_to_params(self):
        c = hyd_schema.new_component("c1", "pump", bore_mm=80, stroke_mm=200)
        assert c["params"]["bore_mm"] == 80
        assert c["params"]["stroke_mm"] == 200

    def test_default_name_fallback(self):
        c = hyd_schema.new_component("c1", "cylinder_linear")
        assert c["name"] == "cylinder_linear"


class TestNewHose:
    def test_basic_hose(self):
        conns = [{"component_id": "c1", "port": "p1"},
                 {"component_id": "c2", "port": "p2"}]
        h = hyd_schema.new_hose("h1", "PRESSURE_LINE", conns)
        assert h["id"] == "h1"
        assert h["net_name"] == "PRESSURE_LINE"
        assert len(h["connections"]) == 2


class TestNextId:
    def test_empty_doc_returns_one(self):
        doc = hyd_schema.new_hydraulic()
        assert hyd_schema.next_id(doc, "c") == "c1"
        assert hyd_schema.next_id(doc, "h") == "h1"

    def test_increments_past_existing(self):
        doc = hyd_schema.new_hydraulic()
        doc["components"].append(hyd_schema.new_component("c1", "pump"))
        doc["components"].append(hyd_schema.new_component("c3", "reservoir"))
        assert hyd_schema.next_id(doc, "c") == "c4"

    def test_hose_prefix(self):
        doc = hyd_schema.new_hydraulic()
        doc["hoses"].append(hyd_schema.new_hose("h1", "P", []))
        assert hyd_schema.next_id(doc, "h") == "h2"


class TestIsHydraulic:
    def test_valid_doc(self):
        doc = hyd_schema.new_hydraulic()
        assert hyd_schema.is_hydraulic(doc)

    def test_wrong_schema(self):
        assert not hyd_schema.is_hydraulic({"schema": "aicad.piping/1"})

    def test_not_a_dict(self):
        assert not hyd_schema.is_hydraulic("not a dict")


# ═══════════════════════════════════════════════════════════════════════════════
# validate_hydraulic
# ═══════════════════════════════════════════════════════════════════════════════

class TestValidateHydraulic:
    def _minimal_doc(self):
        doc = hyd_schema.new_hydraulic("Test")
        doc["components"].append(
            hyd_schema.new_component("c1", "pump", name="Pump", x=0, y=0)
        )
        doc["components"].append(
            hyd_schema.new_component("c2", "reservoir", name="Tank", x=100, y=0)
        )
        doc["hoses"].append(
            hyd_schema.new_hose("h1", "PRESSURE", [
                {"component_id": "c1", "port": "p1"},
                {"component_id": "c2", "port": "p2"},
            ])
        )
        return doc

    def test_valid_doc_passes(self):
        doc = self._minimal_doc()
        errs = hyd_schema.validate_hydraulic(doc)
        assert errs == [], f"unexpected errors: {errs}"

    def test_bad_component_type_rejected(self):
        doc = self._minimal_doc()
        doc["components"][0]["type"] = "rocket_engine"
        errs = hyd_schema.validate_hydraulic(doc)
        assert any("HYD_COMPONENT_TYPES" in e for e in errs)

    def test_bad_port_type_rejected(self):
        doc = self._minimal_doc()
        doc["components"][0]["ports"] = [{"id": "p1", "port_type": "exhaust"}]
        errs = hyd_schema.validate_hydraulic(doc)
        assert any("HYD_PORT_TYPES" in e for e in errs)

    def test_duplicate_component_id_rejected(self):
        doc = self._minimal_doc()
        doc["components"].append(
            hyd_schema.new_component("c1", "filter")  # duplicate id
        )
        errs = hyd_schema.validate_hydraulic(doc)
        assert any("duplicate" in e for e in errs)

    def test_duplicate_hose_id_rejected(self):
        doc = self._minimal_doc()
        doc["hoses"].append(hyd_schema.new_hose("h1", "TANK", []))  # duplicate
        errs = hyd_schema.validate_hydraulic(doc)
        assert any("duplicate" in e for e in errs)

    def test_wrong_schema_error(self):
        doc = hyd_schema.new_hydraulic()
        doc["schema"] = "wrong/1"
        errs = hyd_schema.validate_hydraulic(doc)
        assert any("schema" in e for e in errs)

    def test_hose_bad_component_ref_flagged(self):
        doc = self._minimal_doc()
        doc["hoses"].append(
            hyd_schema.new_hose("h2", "DRAIN", [
                {"component_id": "c999", "port": "drain"},  # nonexistent
            ])
        )
        errs = hyd_schema.validate_hydraulic(doc)
        assert any("c999" in e for e in errs)

    def test_alias_validate(self):
        doc = hyd_schema.new_hydraulic()
        assert hyd_schema.validate(doc) == hyd_schema.validate_hydraulic(doc)


# ═══════════════════════════════════════════════════════════════════════════════
# size_cylinder
# ═══════════════════════════════════════════════════════════════════════════════

class TestSizeCylinder:
    def test_hand_calc_50mm_bore_200bar(self):
        """
        50 mm bore, 200 bar, efficiency=1.0:
        A_bore = π/4 × (0.05)² = 1.9635e-3 m²
        F = 1.9635e-3 × 200×10⁵ × 1 / 1000 = 39.270 kN
        """
        result = hyd_solve.size_cylinder(
            bore_mm=50, rod_mm=0, pressure_bar=200,
            stroke_mm=100, efficiency=1.0
        )
        assert result["ok"]
        # π/4 × 0.05² × 200e5 N → kN: ≈ 39.27 kN
        assert abs(result["extend_force_kn"] - 39.270) < 0.01, (
            f"extend_force_kn={result['extend_force_kn']}, expected ≈39.27"
        )

    def test_retract_force_less_than_extend(self):
        result = hyd_solve.size_cylinder(
            bore_mm=80, rod_mm=56, pressure_bar=200, stroke_mm=300
        )
        assert result["ok"]
        assert result["retract_force_kn"] < result["extend_force_kn"]

    def test_cycle_time_calculation(self):
        result = hyd_solve.size_cylinder(
            bore_mm=50, pressure_bar=100, stroke_mm=500, velocity_mm_s=100
        )
        assert result["ok"]
        assert abs(result["cycle_time_s"] - 5.0) < 0.001

    def test_flow_extend_positive(self):
        result = hyd_solve.size_cylinder(
            bore_mm=63, pressure_bar=150, stroke_mm=200
        )
        assert result["ok"]
        assert result["flow_extend_lpm"] > 0

    def test_bad_bore_rejected(self):
        result = hyd_solve.size_cylinder(bore_mm=0, pressure_bar=100, stroke_mm=100)
        assert not result["ok"]
        assert "bore_mm" in result["reason"]

    def test_rod_ge_bore_rejected(self):
        result = hyd_solve.size_cylinder(bore_mm=50, rod_mm=50, pressure_bar=100, stroke_mm=100)
        assert not result["ok"]

    def test_area_bore_cm2(self):
        result = hyd_solve.size_cylinder(bore_mm=100, pressure_bar=200, stroke_mm=500)
        assert result["ok"]
        # A_bore = π/4 × 10² cm² ≈ 78.54 cm²
        assert abs(result["area_bore_cm2"] - 78.54) < 0.1


# ═══════════════════════════════════════════════════════════════════════════════
# size_pump
# ═══════════════════════════════════════════════════════════════════════════════

class TestSizePump:
    def test_hand_calc_10lpm_200bar(self):
        """
        10 L/min, 200 bar, η=0.85:
        hydraulic_kW = (10/60/1000) × (200×1e5) / 1000
                     = 1.6667e-4 × 2e7 / 1000 = 3.333 kW
        shaft_kW = 3.333 / 0.85 ≈ 3.922 kW
        """
        result = hyd_solve.size_pump(flow_lpm=10, pressure_bar=200, efficiency=0.85)
        assert result["ok"]
        assert abs(result["hydraulic_power_kw"] - 3.333) < 0.01, (
            f"hydraulic_power_kw={result['hydraulic_power_kw']}"
        )
        assert abs(result["shaft_power_kw"] - 3.922) < 0.01, (
            f"shaft_power_kw={result['shaft_power_kw']}"
        )

    def test_displacement_cc_rev(self):
        # 15 L/min @ 1500 rpm → 10 cc/rev
        result = hyd_solve.size_pump(flow_lpm=15, pressure_bar=100, speed_rpm=1500)
        assert result["ok"]
        assert abs(result["displacement_cc_rev"] - 10.0) < 0.01

    def test_torque_positive(self):
        result = hyd_solve.size_pump(flow_lpm=20, pressure_bar=200)
        assert result["ok"]
        assert result["torque_nm"] > 0

    def test_bad_flow_rejected(self):
        result = hyd_solve.size_pump(flow_lpm=0, pressure_bar=100)
        assert not result["ok"]

    def test_bad_efficiency_rejected(self):
        result = hyd_solve.size_pump(flow_lpm=10, pressure_bar=100, efficiency=0.0)
        assert not result["ok"]


# ═══════════════════════════════════════════════════════════════════════════════
# size_accumulator
# ═══════════════════════════════════════════════════════════════════════════════

class TestSizeAccumulator:
    def test_hand_calc_5l_stored(self):
        """
        pre=100 bar, max=250 bar, min=150 bar, stored=5 L:
        factor = 100 × (1/150 − 1/250) = 100 × 0.002667 ≈ 0.2667
        total = 5 / 0.2667 ≈ 18.73 L
        """
        result = hyd_solve.size_accumulator(
            pre_charge_bar=100,
            max_pressure_bar=250,
            min_pressure_bar=150,
            volume_stored_l=5,
        )
        assert result["ok"]
        assert abs(result["total_volume_l"] - 18.73) < 0.1, (
            f"total_volume_l={result['total_volume_l']}, expected ≈18.73"
        )

    def test_pressure_ratio(self):
        result = hyd_solve.size_accumulator(
            pre_charge_bar=100, max_pressure_bar=300,
            min_pressure_bar=150, volume_stored_l=2
        )
        assert result["ok"]
        assert result["pressure_ratio"] == pytest.approx(3.0, rel=1e-4)

    def test_pre_charge_ge_min_rejected(self):
        result = hyd_solve.size_accumulator(
            pre_charge_bar=150, max_pressure_bar=300,
            min_pressure_bar=150, volume_stored_l=5
        )
        assert not result["ok"]

    def test_min_ge_max_rejected(self):
        result = hyd_solve.size_accumulator(
            pre_charge_bar=100, max_pressure_bar=150,
            min_pressure_bar=200, volume_stored_l=5
        )
        assert not result["ok"]


# ═══════════════════════════════════════════════════════════════════════════════
# valve_flow
# ═══════════════════════════════════════════════════════════════════════════════

class TestValveFlow:
    def test_basic_flow_positive(self):
        result = hyd_solve.valve_flow(Cv=10, pressure_drop_bar=5)
        assert result["ok"]
        assert result["flow_lpm"] > 0

    def test_kv_conversion(self):
        # Kv = Cv × 0.865
        result = hyd_solve.valve_flow(Cv=10, pressure_drop_bar=1, fluid_sg=1.0)
        assert result["ok"]
        assert abs(result["Kv"] - 8.65) < 0.001

    def test_zero_cv_rejected(self):
        result = hyd_solve.valve_flow(Cv=0, pressure_drop_bar=5)
        assert not result["ok"]

    def test_zero_dp_rejected(self):
        result = hyd_solve.valve_flow(Cv=10, pressure_drop_bar=0)
        assert not result["ok"]

    def test_higher_dp_gives_more_flow(self):
        r1 = hyd_solve.valve_flow(Cv=5, pressure_drop_bar=2)
        r2 = hyd_solve.valve_flow(Cv=5, pressure_drop_bar=8)
        assert r1["ok"] and r2["ok"]
        assert r2["flow_lpm"] > r1["flow_lpm"]


# ═══════════════════════════════════════════════════════════════════════════════
# fmu_simulate — soft-fail path (fmpy not installed in CI)
# ═══════════════════════════════════════════════════════════════════════════════

class TestFmuSimulate:
    def test_soft_fail_no_fmpy_or_no_path(self):
        """
        Either fmpy is missing (skipped=True, ok=False) or fmu_path is None.
        Either way the function must return a skipped dict, never raise.
        """
        doc = hyd_schema.new_hydraulic("FMU test")
        result = hyd_solve.fmu_simulate(doc, fmu_path=None)
        assert result.get("ok") is False
        assert result.get("skipped") is True
        assert "reason" in result
        assert "hint" in result

    def test_soft_fail_always_returns_dict(self):
        doc = hyd_schema.new_hydraulic()
        result = hyd_solve.fmu_simulate(doc)
        assert isinstance(result, dict)
        assert "ok" in result


# ═══════════════════════════════════════════════════════════════════════════════
# BOM helpers
# ═══════════════════════════════════════════════════════════════════════════════

class TestBomHelpers:
    def _circuit(self):
        doc = hyd_schema.new_hydraulic("BOM test")
        c1 = hyd_schema.new_component("c1", "pump", name="Main Pump")
        c1["sourcing"]["mpn"] = "HYD-PUMP-001"
        c1["sourcing"]["manufacturer"] = "Bosch Rexroth"
        c1["sourcing"]["description"] = "Axial piston pump 18 cc/rev"
        c1["sourcing"]["category"] = "Hydraulic pump"
        doc["components"].append(c1)

        c2 = hyd_schema.new_component("c2", "cylinder_linear", name="Lift Cylinder")
        # No MPN — should appear in missing_mpn
        doc["components"].append(c2)

        c3 = hyd_schema.new_component("c3", "reservoir", name="Tank")
        c3["sourcing"]["mpn"] = "TANK-020L"
        doc["components"].append(c3)
        return doc

    def test_bom_lines_count(self):
        doc = self._circuit()
        lines = hyd_bom.hydraulic_bom_lines(doc)
        assert len(lines) == 3

    def test_bom_line_shape(self):
        doc = self._circuit()
        lines = hyd_bom.hydraulic_bom_lines(doc)
        required_keys = {"id", "type", "name", "mpn", "manufacturer",
                         "description", "quantity", "unit", "category"}
        for line in lines:
            assert required_keys.issubset(set(line.keys())), (
                f"missing keys in {line}"
            )

    def test_mpn_preserved(self):
        doc = self._circuit()
        lines = hyd_bom.hydraulic_bom_lines(doc)
        pump_line = next(l for l in lines if l["id"] == "c1")
        assert pump_line["mpn"] == "HYD-PUMP-001"
        assert pump_line["manufacturer"] == "Bosch Rexroth"

    def test_sourcing_summary_counts(self):
        doc = self._circuit()
        summary = hyd_bom.sourcing_summary(doc)
        assert summary["total"] == 3
        assert summary["sourced"] == 2  # c1 and c3 have MPN
        assert "c2" in summary["missing_mpn"]

    def test_sourcing_summary_by_type(self):
        doc = self._circuit()
        summary = hyd_bom.sourcing_summary(doc)
        assert summary["by_type"]["pump"] == 1
        assert summary["by_type"]["cylinder_linear"] == 1
        assert summary["by_type"]["reservoir"] == 1

    def test_suppressed_component_excluded(self):
        doc = self._circuit()
        doc["components"][0]["suppressed"] = True
        lines = hyd_bom.hydraulic_bom_lines(doc)
        assert len(lines) == 2
        assert all(l["id"] != "c1" for l in lines)


# ═══════════════════════════════════════════════════════════════════════════════
# Round-trip: build doc -> validate -> extract BOM
# ═══════════════════════════════════════════════════════════════════════════════

class TestRoundTrip:
    def test_full_circuit_round_trip(self):
        """Build a complete circuit, validate it, size components, extract BOM."""
        doc = hyd_schema.new_hydraulic("Press Circuit")

        # Add pump
        pump_id = hyd_schema.next_id(doc, "c")
        pump = hyd_schema.new_component(pump_id, "pump", name="Main Pump",
                                        x=0, y=0,
                                        displacement_cc_rev=18, speed_rpm=1450)
        pump["sourcing"]["mpn"] = "A10VSO18DFR"
        pump["ports"].append({"id": "p1", "port_type": "pressure"})
        pump["ports"].append({"id": "p2", "port_type": "tank"})
        doc["components"].append(pump)

        # Add cylinder
        cyl_id = hyd_schema.next_id(doc, "c")
        cyl = hyd_schema.new_component(cyl_id, "cylinder_linear",
                                       name="Press Cylinder",
                                       x=200, y=0,
                                       bore_mm=80, rod_mm=56, stroke_mm=400)
        cyl["ports"].append({"id": "p1", "port_type": "work_a"})
        cyl["ports"].append({"id": "p2", "port_type": "work_b"})
        doc["components"].append(cyl)

        # Add reservoir
        res_id = hyd_schema.next_id(doc, "c")
        reservoir = hyd_schema.new_component(res_id, "reservoir", name="Tank", x=0, y=200)
        reservoir["ports"].append({"id": "p1", "port_type": "tank"})
        doc["components"].append(reservoir)

        # Add hose
        h_id = hyd_schema.next_id(doc, "h")
        hose = hyd_schema.new_hose(h_id, "PRESSURE_LINE", [
            {"component_id": pump_id, "port": "p1"},
            {"component_id": cyl_id, "port": "p1"},
        ])
        doc["hoses"].append(hose)

        # Validate
        errs = hyd_schema.validate_hydraulic(doc)
        assert errs == [], f"validation errors: {errs}"

        # Size the cylinder at 200 bar
        cyl_result = hyd_solve.size_cylinder(
            bore_mm=80, rod_mm=56, pressure_bar=200, stroke_mm=400
        )
        assert cyl_result["ok"]
        # Annulus area: π/4 × (80² - 56²) = π/4 × (6400 - 3136) = π/4 × 3264 ≈ 25.6 cm²
        # Force = 25.6e-4 m² × 200e5 Pa × 0.95 ≈ 486 kN (extend bore area)
        assert cyl_result["extend_force_kn"] > 0

        # Size the pump for the cylinder flow
        pump_result = hyd_solve.size_pump(
            flow_lpm=cyl_result["flow_extend_lpm"],
            pressure_bar=200
        )
        assert pump_result["ok"]
        assert pump_result["displacement_cc_rev"] > 0

        # BOM
        lines = hyd_bom.hydraulic_bom_lines(doc)
        assert len(lines) == 3

        summary = hyd_bom.sourcing_summary(doc)
        assert summary["total"] == 3
        # Only pump has mpn
        assert summary["sourced"] == 1
        assert len(summary["missing_mpn"]) == 2
