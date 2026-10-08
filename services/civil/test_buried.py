"""Offline V&V: published USBR example plus independently reduced hand calculations.

Reference fixture: USBR M-25 Third Edition (February 2019), Appendix A-1 and
Tables 1/2/5. No reference solver, daemon, network access, or generated fixtures.
"""
import json
import math

import pytest

from services.civil import buried
from services.civil.errors import CivilError


def test_prism_closed_form_and_zero_cover():
    # γ=18 kN/m³, H=2 m, OD=0.6 m: q=36 kPa; W=21.6 kN/m.
    result = buried.prism_load(18, 2, 0.6)
    assert result["load_kn_m"] == pytest.approx(21.6)
    assert result["pressure_kpa"] == pytest.approx(36)
    assert buried.prism_load(18, 0, 0.6)["load_kn_m"] == 0


@pytest.mark.parametrize("ku", [0.13, 0.150, 0.165, 0.19])
def test_marston_deep_and_shallow_limits(ku):
    # Marston: H/Bd→∞ gives Cd=1/(2Ku); shallow flexible load→pipe prism.
    deep = buried.marston_trench_load(18, 1000, 2, 0.6, ku)
    assert deep["cd"] == pytest.approx(1/(2*ku), rel=1e-12)
    shallow = buried.marston_trench_load(18, 1e-10, 2, 0.6, ku)
    assert shallow["cd"] == pytest.approx(5e-11, rel=1e-9)
    assert shallow["flexible_load_kn_m"] == pytest.approx(1.08e-9, rel=1e-9)
    assert shallow["rigid_load_kn_m"] == pytest.approx(3.6e-9, rel=1e-9)
    assert deep["rigid_load_kn_m"]/deep["flexible_load_kn_m"] == pytest.approx(2/0.6)


def test_marston_exact_zero_friction_and_named_soil():
    result = buried.marston_trench_load(18, 2, 1, 0.6, 0)
    assert result["cd"] == pytest.approx(2)
    assert result["rigid_load_kn_m"] == pytest.approx(36)
    assert result["flexible_load_kn_m"] == pytest.approx(21.6)
    clay = buried.marston_trench_load(18, 2, 1, 0.6, "ordinary_clay")
    assert clay["cd"] == pytest.approx((1-math.exp(-0.52))/0.26)
    assert buried.marston_trench_load(18, 0, 1, 0.6)["cd"] == 0


def test_explicit_wheel_load_spread_no_hidden_truck():
    # 80 kN wheel, impact 1.25, contact .2x.4 m, H=1m, LLDF=1.15:
    # area=1.35*1.55=2.0925m², pressure=100/2.0925=47.789725...kPa.
    result = buried.live_load_pressure(80, 0.2, 0.4, 1, 1.25)
    assert result["distributed_area_m2"] == pytest.approx(2.0925)
    assert result["pressure_kpa"] == pytest.approx(47.78972520908005)
    custom = buried.live_load_pressure(80, 0.2, 0.4, 1, 1, lldf=2)
    assert custom["pressure_kpa"] == pytest.approx(80/5.28)
    assert buried.live_load_pressure(160, 0.2, 0.4, 1, 1.25)["pressure_kpa"] == pytest.approx(2*result["pressure_kpa"])
    assert buried.live_load_pressure(80, 0.2, 0.4, 0, 1)["pressure_kpa"] == pytest.approx(1000)


def test_boussinesq_finite_patch_surface_and_far_field():
    # At a=b=z=1, four-corner influence is 2/pi*(pi/6+1/sqrt(3)).
    # This hand-reduced Fadum/Newmark value is 0.700885930281194.
    result = buried.live_load_pressure(400, 2, 2, 1, 1, "boussinesq")
    assert result["pressure_kpa"] == pytest.approx(70.0885930281194, rel=1e-12)
    surface = buried.live_load_pressure(400, 2, 2, 0, 1, "boussinesq")
    assert surface["pressure_kpa"] == pytest.approx(100)
    # Remote small footprint approaches Boussinesq point load 3P/(2πz²).
    remote = buried.live_load_pressure(100, 0.002, 0.002, 10, 1, "boussinesq")
    assert remote["pressure_kpa"] == pytest.approx(3/(2*math.pi), rel=1e-7)


@pytest.mark.parametrize("pavement,pressure_psi", [("rigid", 6.7), ("flexible", 7.6)])
def test_usbr_hs20_three_foot_published_benchmark(pavement, pressure_psi):
    # USBR M-25, 2019, Tables 1/2: impact already included. 1psi=6.894757293168kPa.
    result = buried.usbr_hs20_table(0.9144, pavement=pavement)
    assert result["pressure_psi"] == pressure_psi
    assert result["pressure_kpa"] == pytest.approx(pressure_psi*6.894757293168)
    assert "NOT HL-93" in result["warnings"][0]
    five_ft = buried.usbr_hs20_table(1.524, pavement=pavement)
    assert five_ft["pressure_psi"] == (3.2 if pavement == "rigid" else 2.5)


def test_usbr_embedment_compaction_and_saturation():
    # M-25 Table 5 Class III moderate: E'b=1000psi, Fd=.67, Tf=2 (sat Tf=4).
    dry = buried.soil_modulus("III", "moderate")
    wet = buried.soil_modulus("III", "moderate", saturated=True)
    assert dry["e_prime_kpa"] == pytest.approx(6894.757293168)
    assert dry["design_factor"] == 0.67
    assert dry["time_lag_factor"] == 2
    assert wet["time_lag_factor"] == 4
    assert buried.soil_modulus("II", "high")["e_prime_kpa"] == pytest.approx(27579.029172672)


def test_modified_iowa_hand_calculation_and_material_criterion():
    # qearth=10kPa, qlive=2kPa, DL=1.5,K=.1, EI/r³=10kPa,E'=1000kPa:
    # ratio=.1*(1.5*10+2)/(10+.061*1000)=1.7/71, 2.39436619718%.
    result = buried.iowa_deflection(10, 2, 10, 1, e_prime_kpa=1000,
                                   deflection_lag_factor=1.5, material="PVC",
                                   criteria={"allowable_deflection_pct_by_material": {"PVC": 3}})
    assert result["deflection_pct"] == pytest.approx(2.394366197183099)
    assert result["deflection_mm"] == pytest.approx(23.94366197183099)
    assert result["checks"][0]["pass"] is True
    fail = buried.iowa_deflection(10, 2, 10, 1, e_prime_kpa=1000,
                                 deflection_lag_factor=1.5, material="PVC",
                                 criteria={"overrides": {"allowable_deflection_pct_by_material": {"PVC": 2}}})
    assert fail["checks"][0]["pass"] is False
    assert fail["checks"][0]["criterion"] == "allowable_deflection_pct_by_material.PVC"


def test_usbr_appendix_a1_published_example():
    # Engine/provenance: USBR M-25 Third Edition, 2019, Appendix A-1 (pp A-1/A-2).
    # 90in steel, 0.5in wall; γ=120pcf, cover=10ft; WL=0; EI/r³=3.315psi;
    # combined E'=1225psi (Sc=1.225), Fd=.67,Tf=2. Published result=3.15%.
    # Source typo prints 1.315*1000=1225; the stated E'=1225 and worked denominator
    # identify the intended value. We use the explicit COMBINED E', not embedment 1000.
    psi_to_kpa = 6.894757293168
    qe_kpa = 120*10/144*psi_to_kpa
    result = buried.iowa_deflection(qe_kpa, 0, 3.315*psi_to_kpa, 2.286,
                                   e_prime_kpa=1225*psi_to_kpa, method="usbr_m25",
                                   time_lag_factor=2, design_factor=0.67)
    assert result["deflection_pct"] == pytest.approx(3.15, abs=0.005)
    assert result["deflection_mm"] == pytest.approx(result["deflection_pct"]*22.86)


def test_usbr_live_load_has_time_lag_distinct_from_classic_dl():
    # M-25 includes live load INSIDE Tf multiplier; classic Iowa DL affects earth only.
    usbr = buried.iowa_deflection(0, 10, 100, 1, e_prime_kpa=0,
                                 method="usbr_m25", time_lag_factor=2, design_factor=1)
    classic = buried.iowa_deflection(0, 10, 100, 1, e_prime_kpa=0,
                                    deflection_lag_factor=2)
    assert usbr["deflection_pct"] == pytest.approx(2)
    assert classic["deflection_pct"] == pytest.approx(1)


def test_ring_buckling_closed_form_and_water_reduction():
    # Select H=ln(4)/.213m -> B'=1/2 exactly. Rw=1, D=1m,EI=1kNm,
    # E'=1000kPa, FS=2 -> qa=√(16000)/2=63.2455532033676kPa.
    cover = math.log(4)/0.213
    result = buried.ring_buckling(1, 1, 1000, cover, 0, 60, fs=2)
    assert result["b_prime"] == pytest.approx(0.5)
    assert result["allowable_pressure_kpa"] == pytest.approx(63.24555320336759)
    assert result["checks"][0]["pass"] is True
    wet = buried.ring_buckling(1, 1, 1000, cover, cover, 60, fs=2)
    assert wet["rw"] == pytest.approx(0.67)
    assert wet["allowable_pressure_kpa"] == pytest.approx(math.sqrt(2680))
    assert wet["checks"][0]["pass"] is False


def test_flotation_exact_factor_one_boundary_and_effective_soil_weight():
    # OD=1m,γw=10,γsat=20,empty pipe=0,groundwater at grade.
    # Fb=10π/4 kN/m; cover=π/4m -> effective soil=10*cover=Fb, FS=1.
    result = buried.flotation(1, 0, math.pi/4, 0, 18, 20, 1, gamma_water_kn_m3=10)
    assert result["buoyancy_kn_m"] == pytest.approx(2.5*math.pi)
    assert result["soil_resistance_kn_m"] == pytest.approx(2.5*math.pi)
    assert result["safety_factor"] == pytest.approx(1)
    assert result["margin_kn_m"] == pytest.approx(0, abs=1e-12)
    assert result["checks"][0]["pass"] is True
    insufficient = buried.flotation(1, 0, math.pi/4, 0, 18, 20, 1.01, gamma_water_kn_m3=10)
    assert insufficient["checks"][0]["pass"] is False


def test_flotation_half_immersed_circle_and_dry_pipe():
    # OD=1m, cover=1m, groundwater=1.5m: half immersed, area=π/8;
    # entire cover dry -> resistance=18kN/m (no contents/side shear).
    half = buried.flotation(1, 0, 1, 1.5, 18, 20, 1, gamma_water_kn_m3=10)
    assert half["submerged_area_m2"] == pytest.approx(math.pi/8)
    assert half["resisting_kn_m"] == pytest.approx(18)
    dry = buried.flotation(1, 2, 1, 3, 18, 20, 1)
    assert dry["buoyancy_kn_m"] == 0
    assert dry["safety_factor"] is None
    assert dry["checks"][0]["pass"] is True
    json.dumps(dry, allow_nan=False)


def test_ninety_degree_thrust_and_vector_and_other_fittings():
    # DIPRA: P=1000kPa,A=.01m² gives PA=10kN; 90° T=√2*10kN.
    bend = buried.thrust_force(1000, 0.01, angle_rad=math.pi/2, direction=[0, 1, 0])
    assert bend["thrust_kn"] == pytest.approx(14.142135623730951)
    assert bend["force_vector_n"] == pytest.approx([0, 14142.13562373095, 0])
    assert "force_vector_n" not in buried.thrust_force(1000, 0.01, angle_rad=math.pi/2)
    assert buried.thrust_force(1000, 0.01, "dead_end")["thrust_kn"] == pytest.approx(10)
    assert buried.thrust_force(1000, 0.01, "tee")["thrust_kn"] == pytest.approx(10)
    assert buried.thrust_force(1000, 0.01, "reducer", a2=0.004)["thrust_kn"] == pytest.approx(6)


def test_thrust_block_area_and_depth_height_constraints():
    # T=20kN,Sf=1.5,Sb=100kPa -> .3m² bearing, square .sqrt(.3)m face.
    result = buried.thrust_block(20, 100, depth=2, height_max=1)
    assert result["bearing_area_m2"] == pytest.approx(0.3)
    assert result["width_m"] == pytest.approx(math.sqrt(0.3))
    assert result["height_m"] == pytest.approx(math.sqrt(0.3))
    assert result["bearing_pressure_kpa"] == pytest.approx(200/3)
    assert result["factored_bearing_pressure_kpa"] == pytest.approx(100)
    shallow = buried.thrust_block(20, 100, depth=0.5, height_max=1)
    assert shallow["height_m"] == pytest.approx(0.25)
    assert shallow["width_m"] == pytest.approx(1.2)
    assert len(shallow["warnings"]) == 1


def test_dipra_restraint_triangular_bearing_and_polyethylene_factor():
    # DIPRA 7th ed. equations 3,4a,5,6, hand-selected cohesive φ=0 case:
    # OD=1m, H=1m,γ=20, Cs=10kPa,fc=1,fphi=0,Kn=1.
    # Fs=π/2*10=5π kN/m; Pp=20*1.5+2*10=50kPa; Rs=50kN/m.
    # P*A=10kN,angle90°,Sf1.5 => L=15/(5π+25)=.368479...m EACH leg.
    args = (100, 0.1, math.pi/2, 1, 1, 20, 1, 1, 10, 0, 1, 0, 1)
    coated = buried.restrained_length(*args)
    assert coated["unit_friction_kn_m"] == pytest.approx(5*math.pi)
    assert coated["unit_bearing_kn_m"] == pytest.approx(50)
    assert coated["effective_bearing_kn_m"] == pytest.approx(25)
    assert coated["restrained_length_m"] == pytest.approx(15/(5*math.pi+25))
    pe = buried.restrained_length(*args, polyethylene_encased=True)
    assert pe["unit_friction_kn_m"] == pytest.approx(3.5*math.pi)
    assert pe["restrained_length_m"] == pytest.approx(15/(3.5*math.pi+25))
    assert pe["restrained_length_m"] > coated["restrained_length_m"]
    assert pe["total_restrained_length_m"] == pytest.approx(2*pe["length_each_leg_m"])


@pytest.mark.parametrize("call,reason", [
    (lambda: buried.prism_load(18, -1, 1), "invalid_input"),
    (lambda: buried.prism_load(float("nan"), 1, 1), "invalid_input"),
    (lambda: buried.marston_trench_load(18, 2, 0.5, 0.6), "trench_narrower_than_pipe"),
    (lambda: buried.marston_trench_load(18, 2, 1, 0.6, "unknown"), "unknown_soil_class"),
    (lambda: buried.live_load_pressure(80, 0.2, 0.4, 1, 0.5), "invalid_input"),
    (lambda: buried.live_load_pressure(80, 0.2, 0.4, 1, 1, "hl93"), "unsupported_distribution"),
    (lambda: buried.usbr_hs20_table(1), "cover_not_tabulated"),
    (lambda: buried.soil_modulus("V", "high"), "soil_not_recommended"),
    (lambda: buried.iowa_deflection(10, 0, 0, 1, e_prime_kpa=0), "zero_ring_and_soil_stiffness"),
    (lambda: buried.iowa_deflection(10, 0, 10, 1, soil_class="III", compaction="moderate", earth_load_basis="marston"), "incompatible_load_basis"),
    (lambda: buried.iowa_deflection(10, 0, 10, 1, e_prime_kpa=1000, method="usbr_m25"), "usbr_factors_required"),
    (lambda: buried.iowa_deflection(10, 0, 10, 1, e_prime_kpa=1000, criteria={}), "deflection_criterion_required"),
    (lambda: buried.ring_buckling(1, 1, 1000, 1, 2, 50), "groundwater_above_ground"),
    (lambda: buried.thrust_force(100, 1, angle_rad=math.pi/2, direction=[1, 1, 0]), "invalid_direction"),
    (lambda: buried.thrust_block(20, 100, depth=0.4, height_max=1, od_m=0.3), "block_height_infeasible"),
    (lambda: buried.restrained_length(100, 0.1, math.pi/2, 1, 1, 20, 1, 1, 0, 0, 0, 0, 0), "no_soil_restraint"),
])
def test_stable_refusals(call, reason):
    with pytest.raises(CivilError) as exc:
        call()
    assert exc.value.reason == reason
