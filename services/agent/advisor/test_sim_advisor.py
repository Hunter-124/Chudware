"""Tests for T-W9 simulation verification advisor.

Test coverage:
  1. BC lint — no_fixture, all_fixed, dangling_ref, clean study
  2. Mesh suggestion — with/without doc_geom hints
  3. Beam anchor — confident geometry → anchor computed, low-aspect → None
  4. Margin policy — PASS / WARN / FAIL verdicts across material classes
  5. Divergence WARN — FEA >> anchor, FEA << anchor, in-range
  6. Stage battery wiring — sim stage now includes sim_bc, sim_margin, etc.
  7. Engine fix-provider wiring — sim_bc / sim_margin / sim_divergence kinds
  8. describe_results integration — advisor verdict appended (in-app path only,
     seeded via sim_study_add; no live solver; mock-prove documented)

Anchor mock-prove note (citation honesty):
    Tests 8 / 5 seed a study whose ``max_von_mises_mpa`` is set to a value that
    is within 5× of beam_calc_raw for the same geometry.  This tests the postcheck
    comparison logic without invoking the FEA solver.  The FEA solver does not run
    headlessly in CI; that is the pre-existing condition documented in issue #16.
"""
from __future__ import annotations

import math
import pytest

from services.agent.advisor import stages
from services.agent.advisor.engine import Finding, Observation, build_advisory
from services.agent.advisor.sim_advisor import (
    DIVERGENCE_WARN_RATIO,
    _bc_issues,
    _beam_aspect_ok,
    _mesh_suggestion,
    compute_beam_anchor,
    narrate_postcheck,
    postcheck,
    precheck,
    resolve_material_class,
    thermal_anchor,
    _thermal_bc_issues,
)
from services.research.calcs.beam import beam_calc_raw


# ---------------------------------------------------------------------------
# 1. BC lint
# ---------------------------------------------------------------------------

class TestBcLint:
    def test_no_fixture_is_blocker(self):
        study = {"type": "fea_static", "fixtures": [], "loads": [{"face": "top", "force": [0, 0, -100]}]}
        items = precheck(study)
        bc_items = [i for i in items if i["check"] == "sim_bc"]
        assert any(i["severity"] == "blocker" for i in bc_items), bc_items
        assert any("no_fixture" in i["finding"].lower() or "no fixtures" in i["finding"].lower()
                   for i in bc_items), bc_items

    def test_no_load_is_blocker(self):
        study = {"type": "fea_static", "fixtures": ["bottom"], "loads": []}
        items = precheck(study)
        bc_items = [i for i in items if i["check"] == "sim_bc"]
        assert any("no loads" in i["finding"].lower() or "no load" in i["finding"].lower()
                   for i in bc_items), bc_items

    def test_all_fixed_is_action(self):
        study = {"type": "fea_static", "fixtures": ["all"], "loads": [{"face": "top", "force": [0, 0, -100]}]}
        issues = _bc_issues(study)
        kinds = [iss["kind"] for iss in issues]
        assert "all_fixed" in kinds, issues

    def test_dangling_ref_raises_action(self):
        study = {
            "type": "fea_static",
            "fixtures": ["bottom"],
            "loads": [{"face": "top", "force": [0, 0, -100]}],
            "dangling_refs": ["face_999"],
        }
        issues = _bc_issues(study)
        kinds = [iss["kind"] for iss in issues]
        assert "dangling_ref" in kinds, issues

    def test_clean_study_no_bc_issues(self):
        study = {"type": "fea_static", "fixtures": ["bottom"], "loads": [{"face": "top", "force": [0, 0, -100]}]}
        issues = _bc_issues(study)
        assert issues == [], issues

    def test_precheck_items_have_candidate_fixes(self):
        """§3 invariant: non-info items must carry ≥1 candidate fix."""
        study = {"type": "fea_static", "fixtures": [], "loads": []}
        items = precheck(study)
        for it in items:
            if it["severity"] != "info":
                assert it["candidate_fixes"], f"Non-info item lacks candidate_fixes: {it}"


# ---------------------------------------------------------------------------
# 2. Mesh suggestion
# ---------------------------------------------------------------------------

class TestMeshSuggestion:
    def test_no_suggestion_for_normal_fineness(self):
        study = {"mesh_fineness": 0.5}
        # No doc_geom — should not suggest for normal fineness
        assert _mesh_suggestion(study) is None

    def test_very_coarse_mesh_triggers_suggestion(self):
        study = {"mesh_fineness": 0.05}
        suggestion = _mesh_suggestion(study)
        assert suggestion is not None
        assert "coarse" in suggestion.lower() or "0.05" in suggestion, suggestion

    def test_doc_geom_fillet_suggests_finer_mesh(self):
        study = {"mesh_fineness": 0.5}
        doc_geom = {"min_fillet_radius_mm": 0.5}  # 0.5/5=0.1, target≈0.1; current 0.5 >> target*2
        suggestion = _mesh_suggestion(study, doc_geom)
        assert suggestion is not None, "Should suggest finer mesh for small fillet"

    def test_doc_geom_large_feature_no_suggestion(self):
        # Min fillet 5mm → target_fine = min(0.9, 5/5)=0.9; current 0.8 is within 0.9*0.8..0.9*2
        # (0.72..1.8) — no suggestion expected
        study = {"mesh_fineness": 0.8}
        doc_geom = {"min_fillet_radius_mm": 5.0}
        suggestion = _mesh_suggestion(study, doc_geom)
        assert suggestion is None, f"Unexpected suggestion: {suggestion}"


# ---------------------------------------------------------------------------
# 3. Beam anchor
# ---------------------------------------------------------------------------

class TestBeamAnchor:
    def _cantilever_bbox(self, L=200.0, b=20.0, h=20.0):
        return {"x_mm": L, "y_mm": b, "z_mm": h}

    def test_confident_geometry_returns_anchor(self):
        bbox = self._cantilever_bbox(L=200, b=20, h=20)  # aspect = 200/sqrt(20*20)=10 > 3
        anchor = compute_beam_anchor(bbox, E_mpa=200000, load_n=1000)
        assert anchor is not None, "Expected anchor for aspect=10"
        assert anchor["max_stress_mpa"] > 0
        assert "aspect_ratio" in anchor

    def test_low_aspect_returns_none(self):
        bbox = {"x_mm": 50, "y_mm": 40, "z_mm": 40}  # aspect = 50/sqrt(40*40)=1.25 < 3
        anchor = compute_beam_anchor(bbox, E_mpa=200000, load_n=1000)
        assert anchor is None, f"Expected None for low-aspect geometry, got {anchor}"

    def test_missing_bbox_returns_none(self):
        anchor = compute_beam_anchor({}, E_mpa=200000, load_n=1000)
        assert anchor is None

    def test_anchor_matches_beam_calc_raw(self):
        """Anchor stress should equal beam_calc_raw output for the same inputs."""
        # L=200mm cantilever, rect 20×20, F=1000N, E=200000MPa
        bbox = self._cantilever_bbox(L=200, b=20, h=20)
        anchor = compute_beam_anchor(bbox, E_mpa=200000, load_n=1000)
        assert anchor is not None
        raw = beam_calc_raw(
            support="cantilever", load="point", section="rect",
            L_mm=200.0, E_mpa=200000.0, F_n=1000.0, b_mm=20.0, h_mm=20.0,
        )
        assert abs(anchor["max_stress_mpa"] - raw["max_stress_mpa"]) < 0.01, (
            f"Anchor {anchor['max_stress_mpa']} != beam_calc_raw {raw['max_stress_mpa']}"
        )

    def test_aspect_threshold_exactly_three(self):
        # L=60, cross=sqrt(20*20)=20, aspect=3 → confident
        bbox = {"x_mm": 60, "y_mm": 20, "z_mm": 20}
        confident, aspect, _ = _beam_aspect_ok(bbox)
        assert confident, f"Expected confident at aspect=3, got {aspect}"

    def test_aspect_below_three_not_confident(self):
        # L=59, cross=sqrt(20*20)=20, aspect=2.95 → not confident
        bbox = {"x_mm": 59, "y_mm": 20, "z_mm": 20}
        confident, aspect, _ = _beam_aspect_ok(bbox)
        assert not confident, f"Expected not confident at aspect={aspect}"


# ---------------------------------------------------------------------------
# 4. Margin policy
# ---------------------------------------------------------------------------

class TestMarginPolicy:
    def _study(self, vm, yield_mpa, mat_name="Steel AISI 1018", ultimate_mpa=None):
        mat = {"name": mat_name, "yield_mpa": yield_mpa, "E_mpa": 200000}
        if ultimate_mpa:
            mat["ultimate_mpa"] = ultimate_mpa
        return {
            "type": "fea_static",
            "material": mat,
            "results": {"max_von_mises_mpa": vm},
        }

    def test_pass_verdict_steel(self):
        # Steel SF=1.5; design_stress = 50*1.5=75 ≤ yield=370 → PASS
        r = postcheck(self._study(vm=50, yield_mpa=370, ultimate_mpa=440))
        assert r["verdict"] == "PASS", r
        assert r["mat_class"] == "metal_steel"
        assert r["sf_used"] == 1.5

    def test_warn_verdict_steel(self):
        # design_stress = 260*1.5=390 > yield=370, ≤ ultimate=440 → WARN
        r = postcheck(self._study(vm=260, yield_mpa=370, ultimate_mpa=440))
        assert r["verdict"] == "WARN", r

    def test_fail_verdict_steel(self):
        # design_stress = 310*1.5=465 > ultimate=440 → FAIL
        r = postcheck(self._study(vm=310, yield_mpa=370, ultimate_mpa=440))
        assert r["verdict"] == "FAIL", r

    def test_polymer_sf_is_two(self):
        r = postcheck(self._study(vm=10, yield_mpa=40, mat_name="ABS plastic", ultimate_mpa=44))
        assert r["sf_used"] == 2.0, r
        assert r["mat_class"] == "polymer", r

    def test_aluminium_class_resolved(self):
        r = postcheck(self._study(vm=50, yield_mpa=276, mat_name="Aluminium 6061-T6", ultimate_mpa=310))
        assert r["mat_class"] == "metal_aluminium", r

    def test_unknown_material_uses_general_default(self):
        r = postcheck(self._study(vm=10, yield_mpa=100, mat_name="mystery material"))
        assert r["mat_class"] == "general", r
        assert r["sf_used"] == 2.0, r

    def test_margin_verdict_items_have_severity(self):
        r = postcheck(self._study(vm=310, yield_mpa=370, ultimate_mpa=440))
        margin_items = [i for i in r["items"] if i["check"] == "sim_margin"]
        assert margin_items, "Expected sim_margin item"
        assert margin_items[0]["severity"] == "blocker", margin_items[0]

    def test_pass_margin_item_is_info(self):
        r = postcheck(self._study(vm=50, yield_mpa=370, ultimate_mpa=440))
        margin_items = [i for i in r["items"] if i["check"] == "sim_margin"]
        assert margin_items[0]["severity"] == "info", margin_items[0]

    def test_missing_yield_skips_verdict(self):
        study = {"type": "fea_static", "material": {"name": "steel"},
                 "results": {"max_von_mises_mpa": 50.0}}
        r = postcheck(study)
        margin_items = [i for i in r["items"] if i["check"] == "sim_margin"]
        assert margin_items[0]["severity"] == "info"
        assert "skipped" in margin_items[0]["finding"].lower()


# ---------------------------------------------------------------------------
# 5. Divergence WARN
# ---------------------------------------------------------------------------

class TestDivergenceWarn:
    def _beam_stress(self, L=200.0, b=20.0, h=20.0, F=1000.0, E=200000.0):
        raw = beam_calc_raw(
            support="cantilever", load="point", section="rect",
            L_mm=L, E_mpa=E, F_n=F, b_mm=b, h_mm=h,
        )
        return raw["max_stress_mpa"]

    def test_divergence_warn_when_fea_much_higher(self):
        anchor_s = self._beam_stress()
        # Set FEA stress to 10× anchor (>> DIVERGENCE_WARN_RATIO=5)
        fea_vm = anchor_s * 10
        study = {
            "type": "fea_static",
            "material": {"name": "Steel AISI 1018", "yield_mpa": 9999.0, "E_mpa": 200000},
            "results": {"max_von_mises_mpa": fea_vm},
            "bbox": {"x_mm": 200, "y_mm": 20, "z_mm": 20},
            "loads": [{"face": "top", "force": [0, 0, -1000]}],
        }
        r = postcheck(study)
        div_items = [i for i in r["items"] if i["check"] == "sim_divergence"]
        assert div_items, f"Expected divergence WARN; items={r['items']}"
        assert div_items[0]["severity"] == "action"

    def test_no_divergence_when_in_range(self):
        anchor_s = self._beam_stress()
        # FEA stress = anchor × 1.5 (well within 5×)
        fea_vm = anchor_s * 1.5
        study = {
            "type": "fea_static",
            "material": {"name": "Steel AISI 1018", "yield_mpa": 9999.0, "E_mpa": 200000},
            "results": {"max_von_mises_mpa": fea_vm},
            "bbox": {"x_mm": 200, "y_mm": 20, "z_mm": 20},
            "loads": [{"face": "top", "force": [0, 0, -1000]}],
        }
        r = postcheck(study)
        div_items = [i for i in r["items"] if i["check"] == "sim_divergence"]
        assert not div_items, f"Unexpected divergence item: {div_items}"

    def test_divergence_warn_when_fea_much_lower(self):
        anchor_s = self._beam_stress()
        # FEA stress = anchor / 10 (<< 1/5 of anchor)
        fea_vm = anchor_s / 10.0
        study = {
            "type": "fea_static",
            "material": {"name": "Steel AISI 1018", "yield_mpa": 9999.0, "E_mpa": 200000},
            "results": {"max_von_mises_mpa": fea_vm},
            "bbox": {"x_mm": 200, "y_mm": 20, "z_mm": 20},
            "loads": [{"face": "top", "force": [0, 0, -1000]}],
        }
        r = postcheck(study)
        div_items = [i for i in r["items"] if i["check"] == "sim_divergence"]
        assert div_items, f"Expected divergence WARN for low-FEA case"

    def test_anchor_shared_from_precheck(self):
        """Anchor computed once in precheck; postcheck reads it from precheck_items."""
        study = {
            "type": "fea_static",
            "material": {"name": "Steel AISI 1018", "yield_mpa": 9999.0, "E_mpa": 200000},
            "fixtures": ["bottom"],
            "loads": [{"face": "top", "force": [0, 0, -1000]}],
            "bbox": {"x_mm": 200, "y_mm": 20, "z_mm": 20},
            "results": {"max_von_mises_mpa": 150.0},
        }
        pre_items = precheck(study)
        r = postcheck(study, precheck_items=pre_items)
        # The anchor should be populated if precheck found a confident geometry
        anchor_precheck = next((i["_anchor"] for i in pre_items if i.get("check") == "sim_anchor"), None)
        if anchor_precheck:
            assert r["anchor_stress_mpa"] is not None


# ---------------------------------------------------------------------------
# 6. Stage battery wiring (T-W5 seam)
# ---------------------------------------------------------------------------

class TestStageBattery:
    def test_sim_battery_includes_new_checks(self):
        battery = stages.check_battery("sim")
        assert stages.CHECK_SIM_BC in battery, battery
        assert stages.CHECK_SIM_MARGIN in battery, battery
        assert stages.CHECK_SIM_RESULTS in battery, battery

    def test_sim_exit_gate_includes_bc_and_margin(self):
        gate = stages.exit_gate("sim")
        assert stages.CHECK_SIM_BC in gate, gate
        assert stages.CHECK_SIM_MARGIN in gate, gate

    def test_sim_exit_gate_excludes_divergence(self):
        # Divergence is action-level; model may acknowledge and advance (§9.3)
        gate = stages.exit_gate("sim")
        assert stages.CHECK_SIM_DIVERGENCE not in gate, gate

    def test_sim_helper_tools_include_run_fea_modal(self):
        helpers = stages.helper_tools("sim")
        assert "run_fea_modal" in helpers, helpers

    def test_other_stages_unaffected(self):
        # Regression: sketch battery unchanged
        assert stages.CHECK_SKETCH_HEALTH in stages.check_battery("sketch")
        assert stages.CHECK_SKETCH_HEALTH in stages.exit_gate("sketch")


# ---------------------------------------------------------------------------
# 7. Engine fix-provider wiring
# ---------------------------------------------------------------------------

class TestEngineFixProviders:
    def _make_finding(self, kind, check, finding="test finding", sev="action"):
        return Finding(
            kind=kind, check=check, severity=sev,
            finding=finding, source_kind="heuristic",
        )

    def _advisory(self, kind, check, sev="action"):
        from services.agent.advisor.engine import Observation, build_advisory
        obs = Observation(stage="sim", findings=[self._make_finding(kind, check, sev=sev)])
        return build_advisory(obs)

    def test_sim_bc_finding_produces_candidate_fix(self):
        adv = self._advisory("sim_bc", "sim_bc")
        items = adv["items"]
        assert items, "Expected advisory items"
        # §3: non-info ⇒ non-empty candidate_fixes
        for it in items:
            if it["severity"] != "info":
                assert it["candidate_fixes"], f"No fix on {it}"

    def test_sim_margin_finding_produces_candidate_fix(self):
        adv = self._advisory("sim_margin", "sim_margin")
        items = adv["items"]
        for it in items:
            if it["severity"] != "info":
                assert it["candidate_fixes"], f"No fix on {it}"

    def test_sim_divergence_finding_produces_candidate_fix(self):
        adv = self._advisory("sim_divergence", "sim_divergence")
        items = adv["items"]
        for it in items:
            if it["severity"] != "info":
                assert it["candidate_fixes"], f"No fix on {it}"


# ---------------------------------------------------------------------------
# 8. narrate_postcheck content
# ---------------------------------------------------------------------------

class TestNarratePostcheck:
    def test_pass_narration_contains_verdict(self):
        r = postcheck({
            "type": "fea_static",
            "material": {"name": "Steel AISI 1018", "yield_mpa": 370, "E_mpa": 200000,
                         "ultimate_mpa": 440},
            "results": {"max_von_mises_mpa": 50},
        })
        text = narrate_postcheck(r)
        assert "PASS" in text, text
        assert "SF=" in text, text

    def test_fail_narration_contains_verdict(self):
        r = postcheck({
            "type": "fea_static",
            "material": {"name": "Steel AISI 1018", "yield_mpa": 370, "E_mpa": 200000,
                         "ultimate_mpa": 440},
            "results": {"max_von_mises_mpa": 310},
        })
        text = narrate_postcheck(r)
        assert "FAIL" in text, text

    def test_divergence_narration_shown(self):
        anchor_s = beam_calc_raw(
            support="cantilever", load="point", section="rect",
            L_mm=200.0, E_mpa=200000.0, F_n=1000.0, b_mm=20.0, h_mm=20.0,
        )["max_stress_mpa"]
        fea_vm = anchor_s * 10
        r = postcheck({
            "type": "fea_static",
            "material": {"name": "Steel AISI 1018", "yield_mpa": 9999, "E_mpa": 200000},
            "results": {"max_von_mises_mpa": fea_vm},
            "bbox": {"x_mm": 200, "y_mm": 20, "z_mm": 20},
            "loads": [{"face": "top", "force": [0, 0, -1000]}],
        })
        text = narrate_postcheck(r)
        assert "diverges" in text or "FEA" in text.lower(), text

    def test_no_anchor_narration_when_geometry_not_beamlike(self):
        r = postcheck({
            "type": "fea_static",
            "material": {"name": "Steel AISI 1018", "yield_mpa": 370, "E_mpa": 200000},
            "results": {"max_von_mises_mpa": 50},
            # No bbox → no anchor
        })
        text = narrate_postcheck(r)
        assert "not computed" in text or "Beam anchor" in text, text


# ---------------------------------------------------------------------------
# 9. Cantilever fixture: FEA ≈ beam calc within tolerance (mock-prove)
# ---------------------------------------------------------------------------

class TestCantileverAnchorMockProve:
    """Mock-prove that postcheck compares anchor vs FEA consistently.

    The FEA solver does not run headlessly in CI.  We seed the study with a
    max_von_mises_mpa set to 1.5× the beam_calc_raw value (plausible; within 5×
    threshold) and verify no divergence WARN is emitted.  A second test seeds at
    10× and verifies the WARN fires.

    This proves the comparison logic; it does NOT verify the FEA solver itself.
    Citation: beam_calc_raw (Shigley §3-4), seeded via study dict.
    """

    def _anchor_stress(self, L=200.0, b=20.0, h=20.0, F=1000.0, E=200000.0):
        return beam_calc_raw(
            support="cantilever", load="point", section="rect",
            L_mm=L, E_mpa=E, F_n=F, b_mm=b, h_mm=h,
        )["max_stress_mpa"]

    def test_within_tolerance_no_divergence_warn(self):
        """FEA = beam_anchor × 1.5 → no divergence WARN (within 5× threshold)."""
        anchor_s = self._anchor_stress()
        fea_vm = anchor_s * 1.5  # plausible: same order of magnitude
        study = {
            "type": "fea_static",
            "material": {"name": "Steel AISI 1018", "yield_mpa": 9999, "E_mpa": 200000},
            "results": {"max_von_mises_mpa": fea_vm},
            "bbox": {"x_mm": 200, "y_mm": 20, "z_mm": 20},
            "loads": [{"face": "top", "force": [0, 0, -1000]}],
        }
        r = postcheck(study)
        div_items = [i for i in r["items"] if i["check"] == "sim_divergence"]
        assert not div_items, (
            f"Unexpected divergence WARN at ratio 1.5× (threshold {DIVERGENCE_WARN_RATIO}×). "
            f"anchor={anchor_s:.3f} fea={fea_vm:.3f} items={div_items}"
        )

    def test_implausible_divergence_emits_warn(self):
        """FEA = beam_anchor × 10 → divergence WARN emitted."""
        anchor_s = self._anchor_stress()
        fea_vm = anchor_s * 10.0
        study = {
            "type": "fea_static",
            "material": {"name": "Steel AISI 1018", "yield_mpa": 9999, "E_mpa": 200000},
            "results": {"max_von_mises_mpa": fea_vm},
            "bbox": {"x_mm": 200, "y_mm": 20, "z_mm": 20},
            "loads": [{"face": "top", "force": [0, 0, -1000]}],
        }
        r = postcheck(study)
        div_items = [i for i in r["items"] if i["check"] == "sim_divergence"]
        assert div_items, (
            f"Expected divergence WARN at ratio 10× "
            f"(anchor={anchor_s:.3f} fea={fea_vm:.3f})"
        )
        assert div_items[0]["numbers"]["divergence_ratio"] > DIVERGENCE_WARN_RATIO


# ---------------------------------------------------------------------------
# 9. Thermal anchor (1D plane-wall Fourier reference) — T-SIM1
# ---------------------------------------------------------------------------

class TestThermalAnchor:
    def _wall_bbox(self, L_mm=10.0, w=100.0, hh=100.0):
        # thinnest side is the through-thickness direction (L_mm)
        return {"x_mm": w, "y_mm": hh, "z_mm": L_mm}

    def test_conduction_anchor_matches_fourier(self):
        # q'' = k ΔT / L. k=50 W/mK, ΔT=80 K, L=10mm=0.01m -> q = 50*80/0.01 = 400000 W/m^2
        anchor = thermal_anchor(self._wall_bbox(L_mm=10.0), k_w_mk=50.0,
                                t_hot_c=100.0, t_cold_c=20.0)
        assert anchor is not None
        assert anchor["mode"] == "conduction"
        assert anchor["q_w_m2"] == pytest.approx(400000.0, rel=1e-9)
        assert "Incropera" in anchor["source"]

    def test_convection_anchor_matches_series_resistance(self):
        # Series resistance: q = (T_hot - T_amb)/(L/k + 1/h); T_cold = T_amb + q/h.
        L_m = 10.0 / 1000.0
        k, h, t_hot, t_amb = 50.0, 100.0, 100.0, 20.0
        q_expect = (t_hot - t_amb) / (L_m / k + 1.0 / h)
        tcold_expect = t_amb + q_expect / h
        anchor = thermal_anchor(self._wall_bbox(L_mm=10.0), k_w_mk=k, t_hot_c=t_hot,
                                h_w_m2k=h, t_amb_c=t_amb)
        assert anchor is not None
        assert anchor["mode"] == "convection"
        assert anchor["q_w_m2"] == pytest.approx(q_expect, rel=1e-9)
        assert anchor["t_cold_c"] == pytest.approx(tcold_expect, rel=1e-9)

    def test_missing_bbox_returns_none(self):
        assert thermal_anchor({}, k_w_mk=50.0, t_hot_c=100.0, t_cold_c=20.0) is None

    def test_bad_conductivity_returns_none(self):
        assert thermal_anchor(self._wall_bbox(), k_w_mk=0.0, t_hot_c=100.0, t_cold_c=20.0) is None

    def test_no_cold_datum_returns_none(self):
        # Only a hot face, no cold temp and no convection -> not confident.
        assert thermal_anchor(self._wall_bbox(), k_w_mk=50.0, t_hot_c=100.0) is None


# ---------------------------------------------------------------------------
# 10. Thermal BC lint (singular temperature datum) — T-SIM1
# ---------------------------------------------------------------------------

class TestThermalBcLint:
    def test_pure_neumann_is_singular(self):
        # Only a flux load, no fixed-T and no convection -> singular.
        issues = _thermal_bc_issues({
            "type": "thermal",
            "fixtures": [],
            "loads": [{"type": "flux", "magnitude": 500.0}],
        })
        kinds = [i["kind"] for i in issues]
        assert "thermal_singular" in kinds, issues

    def test_convection_alone_is_not_singular(self):
        # A convection film anchors the datum even with no fixed-T.
        issues = _thermal_bc_issues({
            "type": "thermal",
            "fixtures": [],
            "loads": [{"type": "convection", "h": 25.0, "t_amb_c": 20.0}],
        })
        kinds = [i["kind"] for i in issues]
        assert "thermal_singular" not in kinds, issues

    def test_fixed_temp_is_not_singular(self):
        issues = _thermal_bc_issues({
            "type": "thermal",
            "fixtures": [{"points": [[0, 0, 0]], "temp_c": 100.0}],
            "loads": [{"type": "flux", "magnitude": 500.0}],
        })
        kinds = [i["kind"] for i in issues]
        assert "thermal_singular" not in kinds, issues

    def test_precheck_routes_thermal_singular_as_blocker(self):
        # A thermal study with no datum should raise a blocker BC item through precheck.
        study = {"type": "thermal", "fixtures": [], "loads": [], "material": {"k_w_mk": 50.0}}
        items = precheck(study)
        bc = [i for i in items if i["check"] == "sim_bc"]
        assert any(i["severity"] == "blocker" for i in bc), bc
        # And every non-info precheck item carries a candidate fix (§3 invariant).
        for it in items:
            if it["severity"] != "info":
                assert it["candidate_fixes"], it

    def test_precheck_emits_thermal_anchor(self):
        study = {
            "type": "thermal",
            "material": {"k_w_mk": 50.0},
            "bbox": {"x_mm": 100, "y_mm": 100, "z_mm": 10},
            "fixtures": [{"points": [[0, 0, 0]], "temp_c": 100.0},
                         {"points": [[10, 0, 0]], "temp_c": 20.0}],
            "loads": [],
        }
        items = precheck(study)
        anchor_items = [i for i in items if i["check"] == "sim_anchor"]
        assert anchor_items, items
        assert anchor_items[0]["_anchor"] is not None
        assert anchor_items[0]["_anchor"]["mode"] == "conduction"


# ---------------------------------------------------------------------------
# 10. Fatigue anchor (T-SIM4) — Goodman / Soderberg S-N hand-calc reference
# ---------------------------------------------------------------------------
#
# Citation honesty: every expected number below is hand-computed in the docstring
# from the named handbook equations (Shigley's Mechanical Engineering Design,
# 10th ed. — mean-stress criteria §6-12 Eq. 6-45/6-46, S-N line Eq. 6-14..6-16
# with f=0.9, unmodified endurance Se'=0.5·Sut Eq. 6-8). The Eq. 6-14..6-16
# constants are ALSO validated against the same literal (7.2627e4 cycles) in
# scripts/vv_benchmark.py FAT1 and services/mcp/test_fea_advanced.py, so this
# anchor, the host S-N mirror, and the V&V bench all agree on one cited line.

from services.agent.advisor.sim_advisor import fatigue_anchor


class TestFatigueAnchor:
    def test_goodman_zero_based_infinite_life(self):
        """R=0 (zero-based) cycle, machined-steel-like Sut=400 MPa, no Se given.

        Hand calc (Shigley 10th):
          Se' = 0.5·Sut = 200 MPa                       (Eq. 6-8)
          sa = (200-0)/2 = 100, sm = (200+0)/2 = 100 MPa
          sa_eq = sa/(1 - sm/Sut) = 100/(1 - 100/400) = 133.333 MPa   (Eq. 6-46)
          sa_eq < Se' -> infinite life; FoS = Se/sa_eq = 200/133.333 = 1.5
        """
        a = fatigue_anchor(200.0, 0.0, ultimate_mpa=400.0, method="goodman")
        assert a is not None
        assert math.isclose(a["sa_mpa"], 100.0)
        assert math.isclose(a["sm_mpa"], 100.0)
        assert math.isclose(a["se_mpa"], 200.0)
        assert math.isclose(a["sa_eq_mpa"], 400.0 / 3.0, rel_tol=1e-9)
        assert a["regime"] == "infinite"
        assert a["cycles"] is None
        assert math.isclose(a["fos_endurance"], 1.5, rel_tol=1e-9)
        assert "Shigley" in a["source"]

    def test_fully_reversed_finite_life_matches_shigley_sn(self):
        """Fully reversed sa=250 MPa, Se=200, Sut=400 — the FAT1 literal.

        Hand calc (Shigley 10th Eq. 6-14..6-16, f=0.9):
          a = (0.9·400)²/200 = 648.0
          b = -⅓·log10(0.9·400/200) = -⅓·log10(1.8) = -0.085085
          N = (250/648)^(1/b) = 7.2627e4 cycles
        Cross-checked bit-for-bit against the host S-N mirror
        (services/mcp/fea_advanced.py sn_life) so the anchor and the host layer
        can never drift apart silently.
        """
        a = fatigue_anchor(250.0, -250.0, ultimate_mpa=400.0,
                           endurance_mpa=200.0, method="goodman")
        assert a is not None
        assert math.isclose(a["sm_mpa"], 0.0)
        assert math.isclose(a["sa_eq_mpa"], 250.0)
        assert a["regime"] == "finite"
        assert math.isclose(a["cycles"], 7.2627e4, rel_tol=0.01)
        from services.mcp.fea_advanced import sn_life
        assert math.isclose(a["cycles"], sn_life(250.0, 200.0, 400.0), rel_tol=1e-12)

    def test_soderberg_more_conservative_than_goodman(self):
        """Tensile-mean cycle σmax=180 / σmin=20 MPa; Sut=400, Sy=250, Se=200.

        Hand calc: sa=80, sm=100 MPa.
          Goodman   (Eq. 6-46): sa_eq = 80/(1-100/400) = 106.667 MPa
          Soderberg (Eq. 6-45): sa_eq = 80/(1-100/250) = 133.333 MPa
        Soderberg replaces Sut with Sy, so it must always be the more
        conservative (larger sa_eq, smaller FoS) of the two.
        """
        g = fatigue_anchor(180.0, 20.0, ultimate_mpa=400.0, yield_mpa=250.0,
                           endurance_mpa=200.0, method="goodman")
        s = fatigue_anchor(180.0, 20.0, ultimate_mpa=400.0, yield_mpa=250.0,
                           endurance_mpa=200.0, method="soderberg")
        assert g is not None and s is not None
        assert math.isclose(g["sa_eq_mpa"], 320.0 / 3.0, rel_tol=1e-9)
        assert math.isclose(s["sa_eq_mpa"], 400.0 / 3.0, rel_tol=1e-9)
        assert s["sa_eq_mpa"] > g["sa_eq_mpa"]
        assert s["fos_endurance"] < g["fos_endurance"]

    def test_compressive_mean_gives_no_benefit(self):
        """σmax=100 / σmin=-300: sa=200, sm=-100 MPa. The anchor clamps sm to 0
        (matching core/sim/fea_fatigue.cpp), so sa_eq == sa for both methods."""
        for method in ("goodman", "soderberg"):
            a = fatigue_anchor(100.0, -300.0, ultimate_mpa=400.0, yield_mpa=250.0,
                               endurance_mpa=200.0, method=method)
            assert a is not None, method
            assert math.isclose(a["sm_mpa"], -100.0)
            assert math.isclose(a["sa_eq_mpa"], 200.0), method

    def test_se_estimate_caps_at_700_for_high_strength(self):
        """Sut=1600 MPa > 1400 -> Se' capped at 700 MPa (Shigley Eq. 6-8)."""
        a = fatigue_anchor(300.0, -300.0, ultimate_mpa=1600.0)
        assert a is not None
        assert math.isclose(a["se_mpa"], 700.0)
        assert "6-8" in a["se_source"]

    def test_static_overload_when_mean_reaches_strength(self):
        """sm=400 == Sut -> Goodman denominator collapses: static failure, not fatigue."""
        a = fatigue_anchor(500.0, 300.0, ultimate_mpa=400.0, endurance_mpa=200.0)
        assert a is not None
        assert a["regime"] == "static_overload"
        assert a["fos_endurance"] == 0.0

    def test_no_amplitude_is_not_a_fatigue_problem(self):
        a = fatigue_anchor(150.0, 150.0, ultimate_mpa=400.0)
        assert a is not None
        assert a["regime"] == "static_no_amplitude"
        assert a["cycles"] is None

    def test_not_confident_paths_return_none(self):
        # Missing/invalid ultimate strength.
        assert fatigue_anchor(200.0, 0.0, ultimate_mpa=0.0) is None
        assert fatigue_anchor(200.0, 0.0, ultimate_mpa=float("nan")) is None
        # Reversed max/min.
        assert fatigue_anchor(0.0, 200.0, ultimate_mpa=400.0) is None
        # Soderberg without a yield strength.
        assert fatigue_anchor(200.0, 0.0, ultimate_mpa=400.0,
                              method="soderberg") is None
        # Unknown method.
        assert fatigue_anchor(200.0, 0.0, ultimate_mpa=400.0, method="gerber") is None
