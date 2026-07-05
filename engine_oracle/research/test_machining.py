"""Tests for engine_oracle/research/machining.py — feeds/speeds + tap_drill.

Contract: aicad.calc/1 (contracts/advisor.md §7) and aicad.advisory/1 (§3).
Run: pytest engine_oracle/research/test_machining.py -v
"""
from __future__ import annotations

import math
import sys
import pathlib

import pytest

# Make the repo root importable when run without install.
_REPO = pathlib.Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from engine_oracle.research.machining import (
    check_envelope,
    feeds_speeds,
    tap_drill,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _assert_calc_envelope(result: dict, calc: str) -> None:
    """Assert the result conforms to the aicad.calc/1 schema (contracts/advisor.md §7)."""
    assert result.get("schema") == "aicad.calc/1", f"Missing/wrong schema: {result.get('schema')}"
    assert result.get("calc") == calc, f"Wrong calc name: {result.get('calc')}"
    assert "answer" in result, "Missing 'answer' key"
    assert "units" in result, "Missing 'units' key"
    assert "equation" in result, "Missing 'equation' key"
    assert "inputs" in result, "Missing 'inputs' key"
    assert "source" in result, "Missing 'source' key"
    src = result["source"]
    assert "kind" in src and "citation" in src, f"source must have kind+citation: {src}"
    assert "notes" in result, "Missing 'notes' key"


# ---------------------------------------------------------------------------
# feeds_speeds — happy-path tests
# ---------------------------------------------------------------------------

class TestFeedsSpeedsBasic:
    def test_alu_6061_endmill_roughing_schema(self):
        """alu_6061 + 10mm endmill_flat roughing must return aicad.calc/1 envelope."""
        result = feeds_speeds("alu_6061", "endmill_flat", 10.0, "roughing")
        _assert_calc_envelope(result, "feeds_speeds")

    def test_alu_6061_endmill_roughing_rpm_in_range(self):
        """alu_6061 10mm endmill: rpm mid should be in 3k–12k on default limits."""
        result = feeds_speeds("alu_6061", "endmill_flat", 10.0, "roughing")
        ans = result["answer"]
        rpm_mid = ans["rpm"]["mid"]
        assert 3000 <= rpm_mid <= 12000, (
            f"Expected rpm_mid in [3000, 12000], got {rpm_mid}. "
            f"Full rpm range: {ans['rpm']}"
        )

    def test_alu_6061_endmill_roughing_envelope_shape(self):
        """rpm/feed/stepdown/stepover must each have min <= mid <= max."""
        result = feeds_speeds("alu_6061", "endmill_flat", 10.0, "roughing")
        ans = result["answer"]
        for field in ("rpm", "feed", "stepdown", "stepover"):
            lo, mid, hi = ans[field]["min"], ans[field]["mid"], ans[field]["max"]
            assert lo <= mid <= hi, f"{field}: min={lo} mid={mid} max={hi} — not ordered"

    def test_titanium_slower_than_aluminum(self):
        """Titanium roughing rpm_mid must be significantly lower than aluminum."""
        al = feeds_speeds("ti_6al4v", "endmill_flat", 10.0, "roughing")["answer"]["rpm"]["mid"]
        ti = feeds_speeds("alu_6061", "endmill_flat", 10.0, "roughing")["answer"]["rpm"]["mid"]
        # Al rpm should be at least 3× faster than Ti (sfm 900 vs 150).
        assert ti > al * 3, f"Al rpm_mid={ti} should be >> Ti rpm_mid={al}"

    def test_steel_1018_drill(self):
        """steel_1018 drill should return valid envelope."""
        result = feeds_speeds("steel_1018", "drill", 8.0, "drilling")
        _assert_calc_envelope(result, "feeds_speeds")
        ans = result["answer"]
        assert ans["rpm"]["mid"] > 0
        assert ans["feed"]["mid"] > 0

    def test_stainless_slower_than_aluminum(self):
        """Stainless 304 roughing should have lower rpm than aluminum for same dia."""
        al = feeds_speeds("alu_6061",          "endmill_flat", 10.0, "roughing")["answer"]["rpm"]["mid"]
        ss = feeds_speeds("steel_304_stainless","endmill_flat", 10.0, "roughing")["answer"]["rpm"]["mid"]
        assert al > ss, f"Al mid={al} should > SS mid={ss}"

    def test_nylon_pa12_flat_endmill_roughing(self):
        """nylon_pa12 roughing returns valid calc/1 envelope."""
        result = feeds_speeds("nylon_pa12", "endmill_flat", 6.0, "roughing")
        _assert_calc_envelope(result, "feeds_speeds")

    def test_inconel_718_face_mill(self):
        """inconel_718 face_mill roughing — slow speeds, valid envelope."""
        result = feeds_speeds("inconel_718", "face_mill", 50.0, "roughing")
        _assert_calc_envelope(result, "feeds_speeds")
        # Inconel face mill should be well under 1000 rpm for 50mm dia.
        assert result["answer"]["rpm"]["mid"] < 1000

    def test_machine_limits_respected(self):
        """Machine max_rpm=5000 must clamp the rpm_mid."""
        result = feeds_speeds("alu_6061", "endmill_flat", 10.0, "roughing",
                              machine_limits={"max_rpm": 5000})
        assert result["answer"]["rpm"]["max"] <= 5000

    def test_alias_resolution(self):
        """'aluminum' alias should resolve to alu_6061 data row."""
        r1 = feeds_speeds("alu_6061",  "endmill_flat", 10.0, "roughing")
        r2 = feeds_speeds("aluminum",  "endmill_flat", 10.0, "roughing")
        # Both should succeed and return the same rpm_mid (same row).
        assert r1.get("schema") == "aicad.calc/1"
        assert r2.get("schema") == "aicad.calc/1"
        assert r1["answer"]["rpm"]["mid"] == r2["answer"]["rpm"]["mid"]

    def test_provenance_in_answer(self):
        """answer must include a 'provenance' string identifying the table row."""
        result = feeds_speeds("alu_6061", "endmill_flat", 10.0, "roughing")
        prov = result["answer"].get("provenance", "")
        assert "alu_6061" in prov or "aluminum" in prov or "endmill_flat" in prov

    def test_chipload_present(self):
        """answer.chipload should be populated for milling ops."""
        result = feeds_speeds("alu_6061", "endmill_flat", 10.0, "roughing")
        cl = result["answer"]["chipload"]
        assert cl["min"] is not None and cl["min"] > 0
        assert cl["max"] is not None and cl["max"] > cl["min"]


# ---------------------------------------------------------------------------
# feeds_speeds — error cases
# ---------------------------------------------------------------------------

class TestFeedsSpeedsErrors:
    def test_unknown_material_returns_error(self):
        """Unknown material must return ok=False (not raise)."""
        result = feeds_speeds("unobtainium", "endmill_flat", 10.0, "roughing")
        assert result.get("ok") is False
        assert "error" in result

    def test_unknown_tool_type_returns_error(self):
        """Unknown tool type must return ok=False."""
        result = feeds_speeds("alu_6061", "laser_sword", 10.0, "roughing")
        assert result.get("ok") is False

    def test_unknown_operation_returns_error(self):
        """Unknown operation must return ok=False."""
        result = feeds_speeds("alu_6061", "endmill_flat", 10.0, "warp_speed")
        assert result.get("ok") is False


# ---------------------------------------------------------------------------
# check_envelope — advisory shape tests
# ---------------------------------------------------------------------------

class TestCheckEnvelope:
    def test_in_envelope_returns_none(self):
        """Params within the envelope should return None (no advisory)."""
        result = feeds_speeds("alu_6061", "endmill_flat", 10.0, "roughing")
        ans = result["answer"]
        # Use midpoint values — guaranteed in range.
        adv = check_envelope(
            "alu_6061", "endmill_flat", 10.0, "roughing",
            rpm=ans["rpm"]["mid"],
            feed=ans["feed"]["mid"],
        )
        assert adv is None, f"In-envelope params should return None, got: {adv}"

    def test_4x_chipload_fires_advisory(self):
        """Feed 4× above max should trigger an out-of-envelope advisory."""
        result = feeds_speeds("alu_6061", "endmill_flat", 10.0, "roughing")
        max_feed = result["answer"]["feed"]["max"]
        adv = check_envelope(
            "alu_6061", "endmill_flat", 10.0, "roughing",
            feed=max_feed * 4.0,
        )
        assert adv is not None, "4× max feed should produce an advisory"
        # Verify aicad.advisory/1 shape (contracts/advisor.md §3).
        assert adv.get("schema") == "aicad.advisory/1", f"Wrong schema: {adv.get('schema')}"
        items = adv.get("items", [])
        assert len(items) >= 1
        item = items[0]
        assert item["severity"] == "hint"
        assert "check" in item
        assert "finding" in item
        assert "candidate_fixes" in item
        assert len(item["candidate_fixes"]) >= 1
        fix = item["candidate_fixes"][0]
        assert fix["confidence"] == "table"
        assert "predicted_effect" in fix

    def test_advisory_clamped_fix_within_envelope(self):
        """The candidate_fix value must land inside the valid envelope."""
        result = feeds_speeds("alu_6061", "endmill_flat", 10.0, "roughing")
        max_feed = result["answer"]["feed"]["max"]
        min_feed = result["answer"]["feed"]["min"]
        adv = check_envelope(
            "alu_6061", "endmill_flat", 10.0, "roughing",
            feed=max_feed * 10.0,  # absurdly high
        )
        assert adv is not None
        fix = adv["items"][0]["candidate_fixes"][0]
        clamped_feed = fix["params"].get("feed")
        assert clamped_feed is not None
        assert min_feed <= clamped_feed <= max_feed, (
            f"Clamped feed {clamped_feed} not in [{min_feed}, {max_feed}]"
        )

    def test_advisory_refs_fields(self):
        """Advisory item should carry refs with material/tool info."""
        result = feeds_speeds("alu_6061", "endmill_flat", 10.0, "roughing")
        adv = check_envelope(
            "alu_6061", "endmill_flat", 10.0, "roughing",
            rpm=99999,
        )
        assert adv is not None
        item = adv["items"][0]
        assert "refs" in item
        assert "numbers" in item
        assert "helper_tools" in item
        assert "source" in item


# ---------------------------------------------------------------------------
# tap_drill tests
# ---------------------------------------------------------------------------

class TestTapDrill:
    def test_m6x1_returns_5mm(self):
        """M6x1.0 tap drill must be 5.0 mm."""
        result = tap_drill("M6x1.0")
        _assert_calc_envelope(result, "tap_drill")
        assert result["answer"]["tap_drill_mm"] == pytest.approx(5.0, abs=0.05)

    def test_m8x1_25(self):
        """M8x1.25 tap drill should be 6.8 mm."""
        result = tap_drill("M8x1.25")
        _assert_calc_envelope(result, "tap_drill")
        assert result["answer"]["tap_drill_mm"] == pytest.approx(6.8, abs=0.1)

    def test_m3_coarse(self):
        """M3 (bare, coarse) tap drill should be 2.5 mm."""
        result = tap_drill("M3x0.5")
        _assert_calc_envelope(result, "tap_drill")
        assert result["answer"]["tap_drill_mm"] == pytest.approx(2.5, abs=0.05)

    def test_unc_1_4_20(self):
        """1/4-20 UNC tap drill should be ~5.1 mm."""
        result = tap_drill("1/4-20")
        _assert_calc_envelope(result, "tap_drill")
        # Standard tap drill for 1/4-20 is #7 = 0.201 in ≈ 5.1 mm.
        assert 4.8 <= result["answer"]["tap_drill_mm"] <= 5.4, (
            f"1/4-20 tap_drill_mm={result['answer']['tap_drill_mm']}, expected ~5.1"
        )

    def test_unc_10_24(self):
        """#10-24 UNC tap drill should be ~3.8 mm."""
        result = tap_drill("#10-24")
        _assert_calc_envelope(result, "tap_drill")
        assert 3.5 <= result["answer"]["tap_drill_mm"] <= 4.1

    def test_unf_1_4_28(self):
        """1/4-28 UNF tap drill should be ~5.4 mm (finer thread = larger drill)."""
        result = tap_drill("1/4-28")
        _assert_calc_envelope(result, "tap_drill")
        unc_r = tap_drill("1/4-20")["answer"]["tap_drill_mm"]
        unf_r = result["answer"]["tap_drill_mm"]
        # UNF drill is larger than UNC for the same nominal.
        assert unf_r > unc_r, f"UNF drill {unf_r} should > UNC drill {unc_r}"

    def test_schema_conformance(self):
        """tap_drill must return schema=aicad.calc/1 with required keys."""
        result = tap_drill("M10x1.5")
        _assert_calc_envelope(result, "tap_drill")
        ans = result["answer"]
        assert "tap_drill_mm" in ans
        assert "pct_thread" in ans
        assert ans["pct_thread"] > 0

    def test_unknown_thread_returns_error(self):
        """Unknown thread designation must return ok=False, not raise."""
        result = tap_drill("ZQ99x99")
        assert result.get("ok") is False or "error" in result

    def test_inputs_prefilled_present(self):
        """inputs.prefilled must be present (§7 requirement)."""
        result = tap_drill("M6x1.0")
        assert "inputs" in result
        assert "prefilled" in result["inputs"]


# ---------------------------------------------------------------------------
# Cross-check: Ti significantly slower than Al (quantitative)
# ---------------------------------------------------------------------------

class TestRelativeSpeeds:
    def test_ti_roughing_rpm_less_than_half_alu(self):
        """Ti-6Al-4V roughing RPM should be < 1/3 of Al 6061 for same dia (sfm ratio ≈ 6×)."""
        al = feeds_speeds("alu_6061", "endmill_flat", 10.0, "roughing")["answer"]["rpm"]["mid"]
        ti = feeds_speeds("ti_6al4v", "endmill_flat", 10.0, "roughing")["answer"]["rpm"]["mid"]
        assert ti < al / 3, f"Ti rpm_mid={ti:.0f} not < Al/3={al/3:.0f} (al={al:.0f})"

    def test_all_materials_produce_positive_rpm(self):
        """Every material in the table must produce positive rpm for endmill_flat roughing."""
        from engine_oracle.research.machining import _load_db
        db = _load_db()
        for mat_id, mat_row in db["materials"].items():
            if "endmill_flat" not in mat_row.get("tool_types", {}):
                continue
            if "roughing" not in mat_row["tool_types"]["endmill_flat"]:
                continue
            result = feeds_speeds(mat_id, "endmill_flat", 10.0, "roughing")
            assert result.get("schema") == "aicad.calc/1", f"{mat_id}: {result}"
            assert result["answer"]["rpm"]["mid"] > 0, f"{mat_id}: rpm_mid=0"
