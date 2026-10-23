"""Behavioral tests for finite advisor math, setup lint, and evidence-gated verdicts.

The suite covers current public argument shapes across structural, thermal,
vibration, CFD, motion, and EM setup. Analytic tests use literal closed-form
references; seeded FEA values exercise advisor comparison logic only and do not
claim to validate the solver backend.
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
    _mesh_convergence_evidence,
    _mesh_suggestion,
    _surface_issues,
    _thermal_bc_issues,
    compute_beam_anchor,
    narrate_postcheck,
    postcheck,
    precheck,
    resolve_material_class,
    thermal_anchor,
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

    def test_free_free_modal_has_no_fixture_or_load_blocker(self):
        issues = _bc_issues({"type": "fea_modal", "fixtures": [], "loads": []})
        assert issues == []

    def test_zero_load_diagnostic_is_never_a_blocker(self):
        items = precheck({
            "type": "fea_static",
            "fixtures": [],
            "loads": [],
            "zero_load_diagnostic": True,
        })
        zero = [item for item in items if "zero-load diagnostic" in item["finding"].lower()]
        assert zero and zero[0]["severity"] == "info"
        assert not any(item["severity"] == "blocker" for item in items)

    def test_zero_force_entry_is_caught_as_no_load(self):
        issues = _bc_issues({
            "type": "fea_static",
            "fixtures": ["bottom"],
            "loads": [{"face": "top", "force": [0, 0, 0]}],
        })
        assert any(issue["kind"] == "no_load" for issue in issues)

    def test_pressure_is_a_driving_load(self):
        issues = _bc_issues({
            "type": "fea_static",
            "fixtures": ["bottom"],
            "loads": [{"face": "top", "pressure": 0.5}],
        })
        assert not any(issue["kind"] == "no_load" for issue in issues)

    def test_contact_requires_a_positive_gap_window(self):
        issues = _surface_issues({"type": "fea_contact", "max_gap_mm": 0.0})
        assert any(issue["kind"] == "invalid_contact_gap" for issue in issues)


    def test_cfd_uses_velocity_not_structural_loads(self):
        issues = _surface_issues({"type": "cfd_flow", "velocity": 0.0, "fluid": "air"})
        assert any(issue["kind"] == "invalid_excitation" for issue in issues)
        assert not any(issue["kind"] == "no_fixture" for issue in _bc_issues(
            {"type": "cfd_flow", "fixtures": [], "loads": []}
        ))

    def test_motion_requires_stop_and_two_samples(self):
        issues = _surface_issues({"type": "motion", "steps": 1})
        assert len([issue for issue in issues if issue["kind"] == "invalid_motion_range"]) == 2

    def test_em_requires_real_cross_section(self):
        issues = _surface_issues({"type": "em_eigenmode"})
        assert any(issue["kind"] == "missing_em_geometry" for issue in issues)
        assert _surface_issues({"type": "em_eigenmode", "standard": "WR-90"}) == []

    def test_unavailable_backend_is_blocker_not_passable_fallback(self):
        items = precheck({
            "type": "em_driven",
            "standard": "WR-90",
            "solver": "openems",
            "capabilities": {"openems": False},
        })
        backend = [item for item in items if item["check"] == "sim_bc"
                   and "unavailable" in item["finding"]]
        assert backend and backend[0]["severity"] == "blocker"
        assert backend[0]["helper_tools"] == ["em_status"]

    def test_actual_em_status_solver_list_blocks_absent_openems(self):
        items = precheck({
            "type": "em_driven",
            "standard": "WR-90",
            "solver": "openems",
            "capabilities": {
                "ok": True,
                "solvers": [{"name": "openems", "installed": False}],
            },
        })
        assert any(
            item["severity"] == "blocker" and "unavailable" in item["finding"]
            for item in items
        )

    def test_explicit_modal_material_requires_density(self):
        issues = _surface_issues({
            "type": "fea_modal",
            "material": {"E_mpa": 70000, "nu": 0.33},
        })
        material = [issue for issue in issues if issue["kind"] == "missing_material_properties"]
        assert material and "density_g_cm3" in material[0]["detail"]

    def test_transient_thermal_material_requires_k_rho_cp(self):
        issues = _surface_issues({
            "type": "fea_thermal_transient",
            "transient": {"t_end_s": 10},
            "loads": [{"face": "top", "flux_w_m2": 1000}],
            "material": {"E_mpa": 70000, "nu": 0.33, "density_g_cm3": 2.7},
        })
        detail = next(
            issue["detail"] for issue in issues
            if issue["kind"] == "missing_material_properties"
        )
        assert "cp_j_kgk" in detail and "conductivity_w_mk" in detail

    def test_transient_top_level_thermal_overrides_satisfy_explicit_material(self):
        issues = _surface_issues({
            "type": "fea_thermal_transient",
            "transient": {"t_end_s": 10},
            "material": {"E_mpa": 70000, "nu": 0.33, "density_g_cm3": 2.7},
            "conductivity_w_mk": 167.0,
            "specific_heat_j_kgk": 896.0,
        })
        assert not any(issue["kind"] == "missing_material_properties" for issue in issues)

    def test_transient_thermal_requires_positive_end_time(self):
        issues = _surface_issues({
            "type": "fea_thermal_transient",
            "transient": {"t_end_s": 0},
            "material": "steel",
        })
        assert any(issue["kind"] == "invalid_thermal_time" for issue in issues)

    def test_invalid_top_level_thermal_override_is_not_accepted(self):
        issues = _surface_issues({
            "type": "fea_thermal_transient",
            "transient": {"t_end_s": 10},
            "material": {"E_mpa": 70000, "nu": 0.33, "density_g_cm3": 2.7},
            "conductivity_w_mk": float("nan"),
            "specific_heat_j_kgk": 896.0,
        })
        detail = next(
            issue["detail"] for issue in issues
            if issue["kind"] == "missing_material_properties"
        )
        assert "conductivity_w_mk" in detail

    def test_invalid_named_material_override_is_not_accepted(self):
        issues = _surface_issues({
            "type": "fea_thermal_transient",
            "transient": {"t_end_s": 10},
            "material": "steel",
            "specific_heat_j_kgk": -1.0,
        })
        assert any(issue["kind"] == "invalid_thermal_properties" for issue in issues)

# ---------------------------------------------------------------------------
# 2. Mesh suggestion
# ---------------------------------------------------------------------------

class TestMeshSuggestion:
    def test_no_suggestion_for_normal_fineness(self):
        study = {"mesh_fineness": 0.5}
        # No doc_geom — should not suggest for normal fineness
        assert _mesh_suggestion(study) is None

    def test_public_mesh_preset_is_normalized(self):
        assert _mesh_suggestion({"mesh_fineness": "fine"}) is None

    def test_very_coarse_mesh_triggers_suggestion(self):
        study = {"mesh_fineness": 0.05}
        suggestion = _mesh_suggestion(study)
        assert suggestion is not None
        assert "coarse" in suggestion.lower() or "0.05" in suggestion, suggestion

    def test_doc_geom_reports_feature_without_fake_length_conversion(self):
        suggestion = _mesh_suggestion(
            {"mesh_fineness": 0.5},
            {"min_fillet_radius_mm": 0.5},
        )
        assert suggestion is not None
        assert "dimensionless" in suggestion
        assert "<10%" in suggestion

    def test_numeric_two_mesh_evidence_suppresses_suggestion(self):
        study = {
            "mesh_fineness": 0.75,
            "mesh_convergence": {"values": [100.0, 96.0], "tolerance_pct": 10.0},
        }
        evidence = _mesh_convergence_evidence(study)
        assert evidence and evidence["stable"]
        assert _mesh_suggestion(study, {"min_wall_mm": 0.5}) is None

    def test_boolean_convergence_claim_is_not_evidence(self):
        assert _mesh_convergence_evidence({"mesh_convergence": {"stable": True}}) is None

    def test_loose_caller_tolerance_cannot_bless_large_change(self):
        evidence = _mesh_convergence_evidence({
            "mesh_convergence": {"values": [100.0, 60.0], "tolerance_pct": 90.0},
        })
        assert evidence and not evidence["stable"]
        assert evidence["tolerance_pct"] == 10.0

    def test_exactly_ten_percent_change_does_not_meet_less_than_ten_rule(self):
        evidence = _mesh_convergence_evidence({
            "mesh_convergence": {"values": [100.0, 90.0]},
        })
        assert evidence and evidence["relative_change_pct"] == pytest.approx(10.0)
        assert not evidence["stable"]

    def test_mesh_guidance_names_the_selected_solver_family(self):
        items = precheck({"type": "fea_modal", "fixtures": [], "mesh_fineness": "coarse"})
        mesh = next(item for item in items if item["check"] == "sim_mesh")
        assert mesh["helper_tools"] == ["run_fea_modal"]


# ---------------------------------------------------------------------------
# 3. Beam anchor
# ---------------------------------------------------------------------------

class TestBeamAnchor:
    def _cantilever_bbox(self, L=200.0, b=20.0, h=20.0):
        return {"x_mm": L, "y_mm": b, "z_mm": h}

    def test_confident_geometry_returns_anchor(self):
        bbox = self._cantilever_bbox(L=200, b=20, h=20)  # aspect = 200/max(20,20)=10
        anchor = compute_beam_anchor(bbox, E_mpa=200000, load_n=1000)
        assert anchor is not None, "Expected anchor for aspect=10"
        assert anchor["max_stress_mpa"] > 0
        assert "aspect_ratio" in anchor

    def test_low_aspect_returns_none(self):
        bbox = {"x_mm": 50, "y_mm": 40, "z_mm": 40}  # aspect = 50/max(40,40)=1.25
        anchor = compute_beam_anchor(bbox, E_mpa=200000, load_n=1000)
        assert anchor is None, f"Expected None for low-aspect geometry, got {anchor}"

    def test_flat_plate_is_not_misclassified_as_a_beam(self):
        bbox = {"x_mm": 200, "y_mm": 100, "z_mm": 1}
        assert compute_beam_anchor(bbox, E_mpa=200000, load_n=1000) is None

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
        # L=60, larger cross-section side=20, aspect=3 → confident
        bbox = {"x_mm": 60, "y_mm": 20, "z_mm": 20}
        confident, aspect, _ = _beam_aspect_ok(bbox)
        assert confident, f"Expected confident at aspect=3, got {aspect}"

    def test_aspect_below_three_not_confident(self):
        # L=59, larger cross-section side=20, aspect=2.95 → not confident
        bbox = {"x_mm": 59, "y_mm": 20, "z_mm": 20}
        confident, aspect, _ = _beam_aspect_ok(bbox)
        assert not confident, f"Expected not confident at aspect={aspect}"

    def test_nonfinite_or_nonpositive_inputs_return_none(self):
        bbox = self._cantilever_bbox()
        assert compute_beam_anchor(bbox, E_mpa=float("inf"), load_n=1000) is None
        assert compute_beam_anchor(bbox, E_mpa=200000, load_n=float("nan")) is None
        assert compute_beam_anchor(bbox, E_mpa=200000, load_n=0) is None
        assert compute_beam_anchor(
            {"x_mm": float("inf"), "y_mm": 20, "z_mm": 20},
            E_mpa=200000,
            load_n=1000,
        ) is None

    def test_subnormal_cross_section_returns_not_confident(self):
        confident, aspect, _ = _beam_aspect_ok(
            {"x_mm": 1.0, "y_mm": 1e-323, "z_mm": 1e-323}
        )
        assert not confident and aspect == 0.0

    def test_axial_force_is_not_mislabeled_as_bending_anchor(self):
        anchor = compute_beam_anchor(
            {"x_mm": 200, "y_mm": 10, "z_mm": 20},
            E_mpa=200000,
            load_n=1000,
            load_vector_n=[1000, 0, 0],
        )
        assert anchor is None

    def test_force_direction_selects_section_depth(self):
        bbox = {"x_mm": 200, "y_mm": 10, "z_mm": 20}
        anchor = compute_beam_anchor(
            bbox, E_mpa=200000, load_n=1000, load_vector_n=[0, 0, -1000]
        )
        expected = beam_calc_raw(
            support="cantilever", load="point", section="rect",
            L_mm=200, E_mpa=200000, F_n=1000, b_mm=10, h_mm=20,
        )
        assert anchor is not None
        assert anchor["load_axis"] == "z"
        assert anchor["max_stress_mpa"] == pytest.approx(expected["max_stress_mpa"])


    def test_precheck_requires_opposite_beam_end_faces(self):
        items = precheck({
            "type": "fea_static",
            "fixtures": ["bottom"],
            "loads": [{"face": "top", "force": [0, 0, -1000]}],
            "material": {"E_mpa": 200000, "nu": 0.3, "yield_mpa": 370},
            "bbox": {"x_mm": 200, "y_mm": 20, "z_mm": 20},
        })
        anchor = next(item for item in items if item["check"] == "sim_anchor")
        assert anchor["_anchor"] is None

# ---------------------------------------------------------------------------
# 4. Margin policy
# ---------------------------------------------------------------------------

class TestMarginPolicy:
    def _study(self, vm, yield_mpa, mat_name="Steel AISI 1018", ultimate_mpa=None,
               *, evidence=True):
        mat = {"name": mat_name, "yield_mpa": yield_mpa, "E_mpa": 200000, "nu": 0.29}
        if ultimate_mpa:
            mat["ultimate_mpa"] = ultimate_mpa
        study = {
            "type": "fea_static",
            "material": mat,
            "results": {"max_von_mises_mpa": vm},
            "fixtures": ["left"],
            "loads": [{"face": "right", "force": [0, 0, -100]}],
        }
        if evidence:
            study.update({
                "mesh_fineness": 0.75,
                "mesh_convergence": {"values": [vm * 1.04, vm], "tolerance_pct": 10.0},
            })
        return study

    def test_pass_verdict_steel(self):
        # Steel SF=1.5; design_stress = 50*1.5=75 ≤ yield=370 → PASS
        r = postcheck(self._study(vm=50, yield_mpa=370, ultimate_mpa=440))
        assert r["verdict"] == "PASS", r
        assert r["mat_class"] == "metal_steel"
        assert r["sf_used"] == 1.5

    def test_warn_verdict_steel(self):
        # 260 MPa is below nominal yield but above allowable=370/1.5.
        r = postcheck(self._study(vm=260, yield_mpa=370, ultimate_mpa=440))
        assert r["verdict"] == "WARN", r

    def test_nominal_stress_below_yield_is_warn_not_fracture(self):
        # Required margin fails (310*1.5>370), but nominal 310 MPa is below yield.
        r = postcheck(self._study(vm=310, yield_mpa=370, ultimate_mpa=440))
        assert r["verdict"] == "WARN", r
        assert "fracture" not in r["items"][0]["finding"].lower()

    def test_nominal_yield_is_fail(self):
        r = postcheck(self._study(vm=380, yield_mpa=370, ultimate_mpa=440))
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
        r = postcheck(self._study(vm=380, yield_mpa=370, ultimate_mpa=440))
        margin_items = [i for i in r["items"] if i["check"] == "sim_margin"]
        assert margin_items, "Expected sim_margin item"
        assert margin_items[0]["severity"] == "blocker", margin_items[0]

    def test_pass_margin_item_is_info(self):
        r = postcheck(self._study(vm=50, yield_mpa=370, ultimate_mpa=440))
        margin_items = [i for i in r["items"] if i["check"] == "sim_margin"]
        assert margin_items[0]["severity"] == "info", margin_items[0]

    def test_missing_yield_withholds_verdict(self):
        study = {"type": "fea_static", "material": {"name": "steel"},
                 "results": {"max_von_mises_mpa": 50.0}}
        r = postcheck(study)
        margin_items = [i for i in r["items"] if i["check"] == "sim_margin"]
        assert r["verdict"] == "WARN"
        assert r["confidence"] == "insufficient"
        assert margin_items[0]["severity"] == "action"
        assert "finite static margin" in margin_items[0]["finding"].lower()

    def test_unresolved_named_material_withholds_without_crashing(self):
        r = postcheck({
            "type": "fea_static",
            "material": "steel",
            "results": {"max_von_mises_mpa": 50.0},
        })
        assert r["verdict"] == "WARN"
        assert r["margin_mpa"] is None

    def test_margin_is_against_allowable_not_raw_yield(self):
        r = postcheck(self._study(vm=200, yield_mpa=370, ultimate_mpa=440))
        assert r["margin_mpa"] == pytest.approx(370 / 1.5 - 200)
        numbers = next(i["numbers"] for i in r["items"] if i["check"] == "sim_margin")
        assert numbers["allowable_mpa"] == pytest.approx(round(370 / 1.5, 3))

    def test_safe_single_mesh_is_warn_with_insufficient_confidence(self):
        r = postcheck(self._study(vm=50, yield_mpa=370, ultimate_mpa=440, evidence=False))
        assert r["verdict"] == "WARN"
        assert r["confidence"] == "insufficient"
        assert any(item["check"] == "sim_mesh"
                   and item["severity"] == "action" for item in r["items"])

    def test_stable_two_mesh_evidence_supports_pass(self):
        r = postcheck(self._study(vm=50, yield_mpa=370, ultimate_mpa=440))
        assert r["verdict"] == "PASS"
        assert r["confidence"] == "supported"

    def test_zero_load_diagnostic_cannot_become_capacity_pass(self):
        study = self._study(vm=0, yield_mpa=370, ultimate_mpa=440)
        study["loads"] = []
        study["zero_load_diagnostic"] = True
        r = postcheck(study)
        assert r["verdict"] == "WARN"
        assert r["confidence"] == "insufficient"

    def test_invalid_explicit_material_cannot_support_pass(self):
        study = self._study(vm=50, yield_mpa=370, ultimate_mpa=440)
        study["material"].pop("nu")
        r = postcheck(study)
        assert r["verdict"] == "WARN"
        assert r["confidence"] == "insufficient"

    def test_reported_unavailable_backend_cannot_support_pass(self):
        study = self._study(vm=50, yield_mpa=370, ultimate_mpa=440)
        study["backend"] = "ccx"
        study["capabilities"] = {"ccx": False}
        r = postcheck(study)
        assert r["verdict"] == "WARN"
        assert r["confidence"] == "insufficient"

    def test_public_mesh_preset_can_support_numeric_convergence(self):
        study = self._study(vm=50, yield_mpa=370, ultimate_mpa=440)
        study["mesh_fineness"] = "fine"
        r = postcheck(study)
        assert r["verdict"] == "PASS"
        assert r["confidence"] == "supported"

    @pytest.mark.parametrize("terminal", [
        {"skipped": True},
        {"state": "failed"},
        {"partial": True},
        {"results": {"max_von_mises_mpa": 50.0, "converged": False}},
    ])
    def test_unavailable_or_unfinished_result_never_passes(self, terminal):
        study = self._study(vm=50, yield_mpa=370, ultimate_mpa=440)
        if "results" in terminal:
            study["results"].update(terminal["results"])
        else:
            study.update(terminal)
        r = postcheck(study)
        assert r["verdict"] == "WARN"
        assert r["confidence"] == "insufficient"

    def test_finite_extreme_values_remain_finite(self):
        r = postcheck(self._study(vm=1e308, yield_mpa=1.0, evidence=False))
        numbers = next(item["numbers"] for item in r["items"]
                       if item["check"] == "sim_margin")
        assert r["verdict"] == "FAIL"
        assert math.isfinite(numbers["design_stress_mpa"])
        assert math.isfinite(numbers["utilization"])
        assert math.isfinite(numbers["margin_mpa"])

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), -1.0, True])
    def test_nonfinite_or_negative_stress_never_passes(self, value):
        r = postcheck(self._study(vm=value, yield_mpa=370, evidence=False))
        assert r["verdict"] != "PASS"
        assert r["margin_mpa"] is None


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
            "fixtures": ["left"],
            "loads": [{"face": "right", "force": [0, 0, -1000]}],
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
            "fixtures": ["left"],
            "loads": [{"face": "right", "force": [0, 0, -1000]}],
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
            "fixtures": ["left"],
            "loads": [{"face": "right", "force": [0, 0, -1000]}],
        }
        r = postcheck(study)
        div_items = [i for i in r["items"] if i["check"] == "sim_divergence"]
        assert div_items, f"Expected divergence WARN for low-FEA case"

    def test_anchor_shared_from_precheck(self):
        """Anchor computed once in precheck; postcheck reads it from precheck_items."""
        study = {
            "type": "fea_static",
            "material": {"name": "Steel AISI 1018", "yield_mpa": 9999.0, "E_mpa": 200000},
            "fixtures": ["left"],
            "loads": [{"face": "right", "force": [0, 0, -1000]}],
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
# 6. Stage battery wiring (advisory-engine seam)
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
                         "nu": 0.29, "ultimate_mpa": 440},
            "results": {"max_von_mises_mpa": 50},
            "fixtures": ["left"],
            "loads": [{"face": "right", "force": [0, 0, -100]}],
            "mesh_fineness": 0.75,
            "mesh_convergence": {"values": [52.0, 50.0]},
        })
        text = narrate_postcheck(r)
        assert "PASS" in text, text
        assert "SF=" in text, text

    def test_fail_narration_contains_verdict(self):
        r = postcheck({
            "type": "fea_static",
            "material": {"name": "Steel AISI 1018", "yield_mpa": 370, "E_mpa": 200000,
                         "nu": 0.29, "ultimate_mpa": 440},
            "results": {"max_von_mises_mpa": 380},
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
            "material": {"name": "Steel AISI 1018", "yield_mpa": 9999, "E_mpa": 200000,
                         "nu": 0.29},
            "results": {"max_von_mises_mpa": fea_vm},
            "bbox": {"x_mm": 200, "y_mm": 20, "z_mm": 20},
            "fixtures": ["left"],
            "loads": [{"face": "right", "force": [0, 0, -1000]}],
        })
        text = narrate_postcheck(r)
        assert "diverges" in text or "fea" in text.lower(), text

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
            "material": {"name": "Steel AISI 1018", "yield_mpa": 9999, "E_mpa": 200000,
                         "nu": 0.29},
            "results": {"max_von_mises_mpa": fea_vm},
            "bbox": {"x_mm": 200, "y_mm": 20, "z_mm": 20},
            "fixtures": ["left"],
            "loads": [{"face": "right", "force": [0, 0, -1000]}],
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
            "material": {"name": "Steel AISI 1018", "yield_mpa": 9999, "E_mpa": 200000,
                         "nu": 0.29},
            "results": {"max_von_mises_mpa": fea_vm},
            "bbox": {"x_mm": 200, "y_mm": 20, "z_mm": 20},
            "fixtures": ["left"],
            "loads": [{"face": "right", "force": [0, 0, -1000]}],
        }
        r = postcheck(study)
        div_items = [i for i in r["items"] if i["check"] == "sim_divergence"]
        assert div_items, (
            f"Expected divergence WARN at ratio 10× "
            f"(anchor={anchor_s:.3f} fea={fea_vm:.3f})"
        )
        assert div_items[0]["numbers"]["divergence_ratio"] > DIVERGENCE_WARN_RATIO


# ---------------------------------------------------------------------------
# 9. Thermal anchor (1D plane-wall Fourier reference)
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


    def test_subnormal_thickness_underflow_returns_none(self):
        assert thermal_anchor(
            self._wall_bbox(L_mm=1e-323), 50.0, 100.0, t_cold_c=20.0
        ) is None

    def test_no_cold_datum_returns_none(self):
        # Only a hot face, no cold temp and no convection -> not confident.
        assert thermal_anchor(self._wall_bbox(), k_w_mk=50.0, t_hot_c=100.0) is None

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0])
    def test_nonfinite_or_negative_geometry_is_rejected(self, bad):
        bbox = self._wall_bbox()
        bbox["z_mm"] = bad
        assert thermal_anchor(bbox, 50.0, 100.0, t_cold_c=20.0) is None

    def test_infinite_conductivity_is_rejected(self):
        assert thermal_anchor(self._wall_bbox(), float("inf"), 100.0,
                              t_cold_c=20.0) is None


# ---------------------------------------------------------------------------
# 10. Thermal BC lint (steady versus transient public schemas)
# ---------------------------------------------------------------------------

class TestThermalBcLint:
    def test_flux_only_transient_is_well_posed(self):
        issues = _thermal_bc_issues({
            "type": "fea_thermal_transient",
            "transient": {"t_end_s": 10},
            "hot_faces": [],
            "cold_faces": [],
            "loads": [{"face": "top", "flux_w_m2": 500.0}],
        })
        assert issues == []

    def test_undriven_transient_is_reported(self):
        issues = _thermal_bc_issues({
            "type": "fea_thermal_transient",
            "transient": {"t_end_s": 10},
            "loads": [],
        })
        assert any(issue["kind"] == "thermal_no_load" for issue in issues)

    def test_temperature_equal_to_initial_field_is_not_a_drive(self):
        issues = _thermal_bc_issues({
            "type": "fea_thermal_transient",
            "transient": {"t_end_s": 10, "t_init_c": 20.0},
            "hot_faces": [{"face": "+z", "temperature": 20.0}],
            "loads": [{"face": "-z", "h_w_m2k": 10.0, "t_amb_c": 20.0}],
        })
        assert any(issue["kind"] == "thermal_no_load" for issue in issues)

    def test_undriven_transient_precheck_is_blocker(self):
        items = precheck({
            "type": "fea_thermal_transient",
            "transient": {"t_end_s": 10},
            "material": "steel",
            "loads": [],
        })
        assert any(
            item["check"] == "sim_bc" and item["severity"] == "blocker"
            for item in items
        )

    def test_steady_requires_nonzero_excitation(self):
        issues = _thermal_bc_issues({
            "type": "fea_thermal",
            "hot_faces": [{"face": "+x", "temperature": 100.0}],
            "cold_faces": [],
        })
        assert any(issue["kind"] == "thermal_steady_bcs" for issue in issues)

    def test_steady_accepts_multiple_temperature_faces_and_flux(self):
        issues = _thermal_bc_issues({
            "type": "fea_thermal",
            "hot_faces": [
                {"face": "+x", "temperature": 100.0},
                {"face": "+y", "temperature": 80.0},
            ],
            "cold_faces": [{"face": "-x", "temperature": 20.0}],
            "loads": [{"face": "+z", "flux_w_m2": 100.0}],
        })
        assert issues == []

    def test_steady_public_hot_and_cold_faces_are_valid(self):
        issues = _thermal_bc_issues({
            "type": "fea_thermal",
            "hot_faces": [{"face": "+x", "temperature": 100.0}],
            "cold_faces": [{"face": "-x", "temperature": 20.0}],
        })
        assert issues == []

    def test_precheck_routes_invalid_steady_setup_as_blocker(self):
        study = {"type": "fea_thermal", "hot_faces": [], "cold_faces": [],
                 "material": {"conductivity_w_mk": 50.0}}
        items = precheck(study)
        bc = [item for item in items if item["check"] == "sim_bc"]
        assert any(item["severity"] == "blocker" for item in bc), bc
        assert all(item["candidate_fixes"] for item in items if item["severity"] != "info")

    def test_precheck_emits_thermal_anchor(self):
        study = {
            "type": "thermal",
            "material": {"E_mpa": 200000, "nu": 0.29, "k_w_mk": 50.0},
            "bbox": {"x_mm": 100, "y_mm": 100, "z_mm": 10},
            "hot_faces": [{"face": "+z", "temperature": 100.0}],
            "cold_faces": [{"face": "-z", "temperature": 20.0}],
            "loads": [],
        }
        items = precheck(study)
        anchor_items = [i for i in items if i["check"] == "sim_anchor"]
        assert anchor_items, items
        assert anchor_items[0]["_anchor"] is not None
        assert anchor_items[0]["_anchor"]["mode"] == "conduction"

    def test_precheck_rejects_thermal_anchor_across_non_thickness_faces(self):
        study = {
            "type": "thermal",
            "material": {"E_mpa": 200000, "nu": 0.29, "k_w_mk": 50.0},
            "bbox": {"x_mm": 100, "y_mm": 100, "z_mm": 10},
            "hot_faces": [{"face": "+x", "temperature": 100.0}],
            "cold_faces": [{"face": "-x", "temperature": 20.0}],
            "loads": [],
        }
        anchor = next(item for item in precheck(study) if item["check"] == "sim_anchor")
        assert anchor["_anchor"] is None


# ---------------------------------------------------------------------------
# 10. Fatigue anchor — Goodman / Soderberg S-N hand-calc reference
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

    def test_explicit_nonfinite_endurance_is_not_silently_estimated(self):
        assert fatigue_anchor(200.0, -200.0, ultimate_mpa=400.0,
                              endurance_mpa=float("inf")) is None

    def test_degenerate_sn_line_returns_none_instead_of_dividing_by_zero(self):
        # Se == 0.9*Sut makes the two log-line anchor points coincide.
        assert fatigue_anchor(380.0, -380.0, ultimate_mpa=400.0,
                              endurance_mpa=360.0) is None

    def test_overflowing_stress_range_returns_none(self):
        assert fatigue_anchor(1e308, -1e308, ultimate_mpa=400.0) is None
