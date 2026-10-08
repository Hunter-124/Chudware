"""Buried-pipe and hydrostatic-thrust screening calculations in SI units.

Methods are attributed below; these are not a substitute for geotechnical investigation,
manufacturer joint ratings, or jurisdiction-adopted design/load combinations. Ring EI
is stiffness PER UNIT AXIAL LENGTH (kN m), not the pipe beam's section EI (kN m²).
"""
from __future__ import annotations

import math
from numbers import Real

from .errors import CivilError

USBR_REFERENCE = (
    "USBR, Method for Prediction of Flexible Pipe Deflection, M-25, 3rd ed., "
    "February 2019, §§3.2–3.7, Tables 1, 2, 5, Appendix A; "
    "https://www.usbr.gov/tsc/techreferences/mands/mands-pdfs/"
    "M-25Report-February2019-FINAL.pdf"
)
DIPRA_REFERENCE = (
    "DIPRA, Thrust Restraint Design for Ductile Iron Pipe, 7th ed., May 2016, "
    "pp. 4, 7, 9–11, equations 3–6; https://dipra.org/wp-content/uploads/2025/02/"
    "Thrust-Restraint-Design-for-Ductile-Iron-Pipe-English.pdf"
)
PSI_KPA = 6.894757293168
FT_M = 0.3048
PCF_KN_M3 = 0.157087463846246

# Marston soil/trench-wall friction maxima, not site-specific measured properties.
MARSTON_KU_PRIME = {
    "ordinary_clay": 0.13,
    "saturated_topsoil": 0.150,
    "sand_and_gravel": 0.165,
    "cohesionless_maximum": 0.19,
}

# Numeric facts from M-25 (2019), Table 5, not the different 1977 Howard table.
# E' in psi, Fd, unsaturated Tf, saturated Tf. I uses a single compacted category.
_USBR_EMBEDMENT = {
    "I": {"uncompacted": (1000, 0.67, 2, 2), "compacted": (6000, 1, 2, 2)},
    "II": {"uncompacted": (500, 0.67, 1.5, 1.5),
           "moderate": (2000, 0.75, 2, 2), "high": (4000, 1, 2, 2)},
    "III": {"uncompacted": (200, 0.5, 1.5, 3),
            "moderate": (1000, 0.67, 2, 4), "high": (2500, 0.75, 2, 4)},
    "IV": {"uncompacted": (100, 0.5, 1.5, 3),
           "moderate": (400, 0.67, 2, 4), "high": (1500, 0.75, 2, 4)},
}


def _number(value, name, *, positive=False, nonnegative=False):
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
        raise CivilError("invalid_input", f"{name} must be a finite number", field=name)
    value = float(value)
    if (positive and value <= 0) or (nonnegative and value < 0):
        raise CivilError("invalid_input", f"{name} must be {'positive' if positive else 'nonnegative'}",
                         field=name)
    return value


def _result(name, reference, *, warnings=None, assumptions=None, **values):
    return {"ok": True, **values, "warnings": warnings or [],
            "assumptions": assumptions or [], "method": {"name": name, "reference": reference}}


def _check(name, value, limit, units, passed, reference, criterion="user_input"):
    return {"check": name, "element_id": None, "value": value, "limit": limit,
            "units": units, "pass": bool(passed), "criterion": criterion, "reference": reference}


def prism_load(gamma_kn_m3, h_m, od_m):
    """Soil prism: W=γ H OD (kN/m), q=γ H (kPa); USBR M-25 §3.3."""
    gamma = _number(gamma_kn_m3, "gamma_kn_m3", positive=True)
    h = _number(h_m, "h_m", nonnegative=True)
    od = _number(od_m, "od_m", positive=True)
    return _result("soil_prism", USBR_REFERENCE, load_kn_m=gamma*h*od,
                   pressure_kpa=gamma*h, assumptions=["Cover is measured from ground to pipe crown."])


def marston_trench_load(gamma, h, bd, od, ku_prime=0.13):
    """Marston trench load, SI: Cd=(1-exp(-2Kμ'H/Bd))/(2Kμ').

    Rigid Wd=Cd γ Bd²; flexible Wc=Cd γ Bd OD. gamma is kN/m³; all
    lengths are metres. ku_prime is a measured number or a MARSTON_KU_PRIME
    class key. Zero friction uses the exact Cd=H/Bd limit. This is NOT an
    embankment installation, and USBR M-25 Table 5 must not calibrate this load.
    Reference: Marston theory; ASCE MOP 60, Gravity Sanitary Sewer Design and
    Construction, 2nd ed., 2007, external load design; Uni-Bell, Deflection:
    The Pipe/Soil Mechanism, trench-load coefficient chart.
    """
    gamma = _number(gamma, "gamma_kn_m3", positive=True)
    h = _number(h, "h_m", nonnegative=True)
    bd = _number(bd, "trench_width_m", positive=True)
    od = _number(od, "od_m", positive=True)
    if bd < od:
        raise CivilError("trench_narrower_than_pipe", "Trench width must be at least OD")
    soil_class = None
    if isinstance(ku_prime, str):
        soil_class = ku_prime
        if soil_class not in MARSTON_KU_PRIME:
            raise CivilError("unknown_soil_class", "Select a MARSTON_KU_PRIME key or supply a number")
        ku_prime = MARSTON_KU_PRIME[soil_class]
    ku = _number(ku_prime, "ku_prime", nonnegative=True)
    cd = h/bd if ku == 0 else -math.expm1(-2*ku*h/bd)/(2*ku)
    rigid = cd*gamma*bd*bd
    flexible = cd*gamma*bd*od
    return _result("marston_trench", "Marston trench theory; ASCE MOP 60, 2nd ed. (2007); "
                   "https://www.uni-bell.org/Portals/0/ResourceFile/deflection-the-pipe-soil-mechanism.pdf",
                   cd=cd, ku_prime=ku, soil_class=soil_class,
                   rigid_load_kn_m=rigid, flexible_load_kn_m=flexible,
                   rigid_pressure_kpa=rigid/od, flexible_pressure_kpa=flexible/od,
                   assumptions=["Trench installation with wall friction; no embankment coefficient.",
                                "Rigid and flexible alternatives are separate, not additive loads."],
                   warnings=["Do not use USBR prism-calibrated E' tables with Marston loads."])


def live_load_pressure(wheel_load_kn, contact_w_m, contact_l_m, h_m, impact_factor,
                       distribution="aashto_lldf_1.15", *, lldf=1.15):
    """Explicit single-wheel pressure below its contact centre, never hidden HL-93.

    AASHTO LRFD §3.6.1.2.6 spread: footprint dimensions grow by LLDF*H,
    q=P*impact/[(w+LLDF*H)(l+LLDF*H)]. impact_factor is a total multiplier
    (1=no impact), not the fractional increment. The 1.15 LLDF is explicit
    and overridable; wheel/axle overlap and multiple presence are caller loads.
    Boussinesq integrates a uniform rectangular patch over an elastic half-space
    (Newmark 1935; Bowles, Foundation Analysis and Design, 5th ed., 1996).
    """
    p = _number(wheel_load_kn, "wheel_load_kn", nonnegative=True)
    w = _number(contact_w_m, "contact_w_m", positive=True)
    length = _number(contact_l_m, "contact_l_m", positive=True)
    h = _number(h_m, "h_m", nonnegative=True)
    impact = _number(impact_factor, "impact_factor", positive=True)
    if impact < 1:
        raise CivilError("invalid_input", "impact_factor is a total multiplier and must be >= 1")
    if distribution == "aashto_lldf_1.15":
        spread = _number(lldf, "lldf", nonnegative=True)
        area = (w+spread*h)*(length+spread*h)
        return _result("aashto_explicit_wheel_spread", "AASHTO LRFD Bridge Design Specifications, "
                       "§3.6.1.2.6 (buried structures); user must select adopted edition",
                       pressure_kpa=p*impact/area, distributed_area_m2=area,
                       distributed_width_m=w+spread*h, distributed_length_m=length+spread*h,
                       wheel_load_kn=p, impact_factor=impact, lldf=spread,
                       assumptions=["Single wheel; no adjacent-wheel overlap, lane load, or multiple presence.",
                                    "This implements load spread only, not complete HL-93 design."])
    if distribution != "boussinesq":
        raise CivilError("unsupported_distribution", "Use aashto_lldf_1.15 or boussinesq")
    if lldf != 1.15:
        raise CivilError("inapplicable_parameter", "LLDF is not a Boussinesq parameter")
    if h == 0:
        influence = 1.0  # exact surface limit at interior of the loaded contact
    else:
        a, b = w/2, length/2
        radius = math.sqrt(a*a+b*b+h*h)
        influence = 2/math.pi*(math.atan2(a*b, h*radius) +
                    a*b*h/radius*(1/(a*a+h*h)+1/(b*b+h*h)))
    return _result("boussinesq_rectangular_contact", "Newmark (1935), integrated Boussinesq "
                   "solution; Bowles, Foundation Analysis and Design, 5th ed. (1996)",
                   pressure_kpa=p*impact/(w*length)*influence, influence_factor=influence,
                   wheel_load_kn=p, impact_factor=impact,
                   assumptions=["Uniform rectangular contact; observation directly under its centre.",
                                "Homogeneous linear-elastic half-space; no pavement stiffness or wheel overlap."])


def usbr_hs20_table(h_m=None, *, pavement="rigid"):
    """M-25 (2019) Tables 1/2, including their impact factors; comparison ONLY.

    With no h_m returns all tabulated rows. Query requires an exact tabulated
    cover: no interpolation, extrapolation, or additional impact multiplier.
    """
    depths_ft = [2, 2.5, 3, 4, 5, 6, 7, 8]
    values = {"rigid": [11.1, 8.5, 6.7, 4.3, 3.2, 2.5, 2.0, 1.6],
              "flexible": [16.8, 11.5, 7.6, 4.2, 2.5, 1.6, 1.1, 0.9]}
    if pavement not in values:
        raise CivilError("unsupported_pavement", "Use rigid or flexible")
    rows = [{"cover_m": d*FT_M, "pressure_psi": p, "pressure_kpa": p*PSI_KPA}
            for d, p in zip(depths_ft, values[pavement])]
    out = _result("usbr_hs20_benchmark", USBR_REFERENCE, pavement=pavement, rows=rows,
                  warnings=["HS-20 benchmark only, NOT HL-93 or a project design load."],
                  assumptions=["Published pressures already include impact."])
    if h_m is not None:
        h = _number(h_m, "h_m", nonnegative=True)
        matches = [row for row in rows if math.isclose(h, row["cover_m"], rel_tol=0, abs_tol=1e-9)]
        if not matches:
            raise CivilError("cover_not_tabulated", "Use an exact M-25 table cover or compute explicit wheel loads")
        out.update(matches[0])
    return out


def soil_modulus(soil_class, compaction, *, saturated=False):
    """Embedment E'b, Fd and Tf from USBR M-25 (2019) Table 5.

    Classes II–IV: moderate = 85 <= C <95%; high = C>=95% (ASTM D698).
    Class I (specified crushed rock) only distinguishes uncompacted/compacted.
    Class V is expressly not recommended. This is embedment E'b, NOT automatically
    combined E': evaluate trench-wall interaction under M-25 §3.10 separately.
    """
    if not isinstance(saturated, bool):
        raise CivilError("invalid_input", "saturated must be boolean")
    if soil_class == "V":
        raise CivilError("soil_not_recommended", "M-25 supplies no E' for highly compressible Class V")
    if soil_class not in _USBR_EMBEDMENT:
        raise CivilError("unknown_soil_class", "Use M-25 soil class I, II, III or IV")
    table = _USBR_EMBEDMENT[soil_class]
    if compaction not in table:
        raise CivilError("unsupported_compaction", f"Class {soil_class} categories: {', '.join(table)}")
    e, fd, tf, sat_tf = table[compaction]
    return _result("usbr_embedment_parameters", USBR_REFERENCE, soil_class=soil_class,
                   compaction=compaction, saturated=saturated, e_prime_kpa=e*PSI_KPA,
                   design_factor=fd, time_lag_factor=sat_tf if saturated else tf,
                   assumptions=["E' is embedment E'b; trench-wall combining factor is not assumed."])


def iowa_deflection(earth_pressure_kpa, live_pressure_kpa, ring_stiffness_kpa,
                    diameter_m, *, e_prime_kpa=None, soil_class=None, compaction=None,
                    saturated=False, deflection_lag_factor=1.0, bedding_constant=0.1,
                    method="modified_iowa", time_lag_factor=None, design_factor=None,
                    material=None, criteria=None, earth_load_basis="prism"):
    """Modified Iowa Δy/D=K(DL*qearth+qlive)/(EI/r³+0.061E'), SI.

    Inputs are pressure (line load divided by OD), NOT kN/m. diameter_m is the
    diameter against which deflection is measured; ring_stiffness_kpa is EI/r³
    based on mean ring radius. Classic DL affects earth load only.

    method='usbr_m25' implements M-25 equation 3.1 exactly after SI conversion:
    Δy/D=Tf*(0.1008*qearth+0.10*qlive)/(EI/r³+0.061*Fd*E').
    Tf is NOT Spangler's DL. Select explicit Tf/Fd with user E', or obtain them
    from Table 5. Earth load must be prism when using that table or M-25 method.
    Optional criteria: {'allowable_deflection_pct_by_material': {material: limit},
    'references': {'allowable_deflection_pct_by_material': citation}}; a criteria
    request without the material/limit refuses rather than assuming an allowable.
    """
    qe = _number(earth_pressure_kpa, "earth_pressure_kpa", nonnegative=True)
    ql = _number(live_pressure_kpa, "live_pressure_kpa", nonnegative=True)
    stiffness = _number(ring_stiffness_kpa, "ring_stiffness_kpa", nonnegative=True)
    diameter = _number(diameter_m, "diameter_m", positive=True)
    dl = _number(deflection_lag_factor, "deflection_lag_factor", positive=True)
    k = _number(bedding_constant, "bedding_constant", positive=True)
    if dl < 1:
        raise CivilError("invalid_input", "deflection_lag_factor must be >=1")
    if earth_load_basis not in {"prism", "marston"}:
        raise CivilError("unsupported_load_basis", "Use prism or marston")
    if method not in {"modified_iowa", "usbr_m25"}:
        raise CivilError("unsupported_deflection_method", "Use modified_iowa or usbr_m25")
    parameters = None
    if e_prime_kpa is None:
        parameters = soil_modulus(soil_class, compaction, saturated=saturated)
        e_prime = parameters["e_prime_kpa"]
        if earth_load_basis != "prism":
            raise CivilError("incompatible_load_basis", "USBR E' table is calibrated to prism earth load")
    else:
        if soil_class is not None or compaction is not None:
            raise CivilError("ambiguous_soil_modulus", "Supply E' or soil class/compaction, not both")
        e_prime = _number(e_prime_kpa, "e_prime_kpa", nonnegative=True)
    assumptions = ["Ring stiffness and E' are in kPa; input earth/live loads are pressures.",
                   "Calculated average deflection depends on achieving the specified installation."]
    if parameters:
        assumptions.append("Table E' is embedment-only: caller confirms trench-wall stiffness does not reduce it.")
    if method == "usbr_m25":
        if earth_load_basis != "prism":
            raise CivilError("incompatible_load_basis", "M-25 equation requires prism earth load")
        if dl != 1 or k != 0.1:
            raise CivilError("inapplicable_parameter", "USBR uses Tf/Fd, not classic DL/bedding_constant")
        if time_lag_factor is None or design_factor is None:
            if parameters is None:
                raise CivilError("usbr_factors_required", "Explicit E' requires explicit time_lag_factor and design_factor")
        tf = _number(time_lag_factor if time_lag_factor is not None else parameters["time_lag_factor"],
                     "time_lag_factor", positive=True)
        fd = _number(design_factor if design_factor is not None else parameters["design_factor"],
                     "design_factor", positive=True)
        if tf < 1 or fd > 1:
            raise CivilError("invalid_input", "USBR requires Tf>=1 and 0<Fd<=1")
        numerator = tf*(0.1008*qe+0.1*ql)
        denominator = stiffness+0.061*fd*e_prime
        factors = {"time_lag_factor": tf, "design_factor": fd}
        reference = USBR_REFERENCE
    else:
        if time_lag_factor is not None or design_factor is not None:
            raise CivilError("inapplicable_parameter", "Tf/Fd apply only to usbr_m25")
        numerator = k*(dl*qe+ql)
        denominator = stiffness+0.061*e_prime
        factors = {"deflection_lag_factor": dl, "bedding_constant": k}
        reference = "Spangler (1941), Iowa formula; Watkins and Spangler (1958), modified Iowa; USBR M-25 Appendix C"
    if denominator <= 0:
        raise CivilError("zero_ring_and_soil_stiffness", "Provide positive ring stiffness or soil support")
    ratio = numerator/denominator
    pct = ratio*100
    checks = []
    if criteria is not None:
        if not isinstance(criteria, dict):
            raise CivilError("invalid_input", "criteria must be a dict")
        limits = criteria.get("overrides", criteria)
        if not isinstance(limits, dict):
            raise CivilError("invalid_input", "criteria.overrides must be a dict")
        allowable = limits.get("allowable_deflection_pct_by_material", {})
        if not isinstance(allowable, dict) or material is None or material not in allowable:
            raise CivilError("deflection_criterion_required", "Supply material and allowable_deflection_pct_by_material[material]")
        limit = _number(allowable[material], "allowable_deflection_pct", positive=True)
        if limit > 100:
            raise CivilError("invalid_input", "Allowable deflection cannot exceed 100%")
        refs = limits.get("references", criteria.get("references", {}))
        citation = refs.get("allowable_deflection_pct_by_material", "User-supplied material allowable") if isinstance(refs, dict) else "User-supplied material allowable"
        checks.append(_check("vertical_deflection", pct, limit, "%", pct <= limit, citation,
                             f"allowable_deflection_pct_by_material.{material}"))
    warnings = ["Calculated diameter loss exceeds 100%; small-deformation method is outside its range."] if ratio >= 1 else []
    return _result(method, reference, deflection_ratio=ratio, deflection_pct=pct,
                   deflection_m=ratio*diameter, deflection_mm=ratio*diameter*1000,
                   e_prime_kpa=e_prime, ring_stiffness_kpa=stiffness, factors=factors,
                   checks=checks, assumptions=assumptions, warnings=warnings)


def ring_buckling(diameter_m, ring_ei_kn_m, e_prime_kpa, h_m, hw_m,
                  applied_pressure_kpa, *, fs=2.5):
    """AWWA M45/C950 soil-supported allowable qa=√(32 Rw B' E' EI/D³)/FS.

    Rw=1-0.33 hw/H, B'=1/(1+4 exp(-0.213 H)) with H in METRES.
    hw is groundwater height above crown, 0<=hw<=H; groundwater above
    ground needs a different loading model. EI is ring EI per axial length.
    Reference: AWWA M45 (2013), buckling; ASTM/AWWA C950; metric exponent
    independently documented in WSAA/Monash Theory Manual M4 Part 2 (2021), eq.7.
    """
    d = _number(diameter_m, "diameter_m", positive=True)
    ei = _number(ring_ei_kn_m, "ring_ei_kn_m", positive=True)
    ep = _number(e_prime_kpa, "e_prime_kpa", positive=True)
    h = _number(h_m, "h_m", positive=True)
    hw = _number(hw_m, "hw_m", nonnegative=True)
    pressure = _number(applied_pressure_kpa, "applied_pressure_kpa", nonnegative=True)
    sf = _number(fs, "fs", positive=True)
    if sf < 1:
        raise CivilError("invalid_input", "fs must be >=1")
    if hw > h:
        raise CivilError("groundwater_above_ground", "This formula requires 0<=hw<=cover")
    rw = 1-0.33*hw/h
    bp = 1/(1+4*math.exp(-0.213*h))
    critical = math.sqrt(32*rw*bp*ep*ei/d**3)
    allowable = critical/sf
    ref = "AWWA M45 (2013), soil-supported ring buckling/C950; WSAA/Monash Theory Manual M4 P2 (2021), eq.7; https://mpep.wsaa.asn.au/assets/docs/TM_M4_Part_2-Lined_pipe_analysis.pdf"
    return _result("awwa_soil_supported_ring_buckling", ref, rw=rw, b_prime=bp,
                   critical_pressure_kpa=critical, allowable_pressure_kpa=allowable,
                   applied_pressure_kpa=pressure, safety_factor=sf,
                   utilization=pressure/allowable,
                   checks=[_check("ring_buckling", pressure, allowable, "kPa", pressure <= allowable, ref)],
                   assumptions=["Supported buried circular ring, not an unsupported/exposed cylinder.",
                                "Caller supplies net applied external pressure and appropriate long-term EI/E'.",
                                "No ovality, construction damage, or lining enhancement factor included."])


def flotation(od, empty_weight, cover, gw_depth, gamma_dry, gamma_sat,
              fs_required, *, gamma_water_kn_m3=9.80665):
    """Archimedes buoyancy versus empty pipe and effective OD-wide soil prism.

    od/cover/gw_depth in m, empty_weight in kN/m, unit weights in kN/m³.
    Groundwater depth is measured downward from grade. Saturated cover uses
    γsat-γwater; partial immersion uses the exact circular segment displaced.
    Reference: WSSC Pipeline Design Manual, Part Three §4, Buoyancy of Pipelines.
    No side-shear, trench arching, ballast, or internal contents are credited.
    """
    d = _number(od, "od_m", positive=True)
    wp = _number(empty_weight, "empty_weight_kn_m", nonnegative=True)
    h = _number(cover, "cover_m", nonnegative=True)
    gw = _number(gw_depth, "gw_depth_m", nonnegative=True)
    dry = _number(gamma_dry, "gamma_dry_kn_m3", positive=True)
    sat = _number(gamma_sat, "gamma_sat_kn_m3", positive=True)
    water = _number(gamma_water_kn_m3, "gamma_water_kn_m3", positive=True)
    sf = _number(fs_required, "fs_required", positive=True)
    if sf < 1 or sat <= water:
        raise CivilError("invalid_input", "Require fs_required>=1 and gamma_sat>gamma_water")
    dry_cover = h if gw >= h else gw
    submerged_cover = h-dry_cover
    soil_weight = d*(dry*dry_cover+(sat-water)*submerged_cover)
    if gw >= h+d:
        submerged_area = 0.0
    elif gw <= h:
        submerged_area = math.pi*d*d/4
    else:
        depth = h+d-gw
        radius = d/2
        submerged_area = radius**2*math.acos((radius-depth)/radius) - (
            radius-depth)*math.sqrt(depth*(2*radius-depth))
    buoyancy = water*submerged_area
    resisting = wp+soil_weight
    ratio = resisting/buoyancy if buoyancy > 0 else None
    reference = "Archimedes; WSSC Pipeline Design Manual, Part Three §4, Buoyancy of Pipelines"
    return _result("empty_pipe_flotation", reference, buoyancy_kn_m=buoyancy,
                   resisting_kn_m=resisting, soil_resistance_kn_m=soil_weight,
                   submerged_area_m2=submerged_area, safety_factor=ratio,
                   safety_factor_required=sf, margin_kn_m=resisting-sf*buoyancy,
                   checks=[_check("flotation", ratio, sf, "dimensionless",
                                  buoyancy == 0 or resisting >= sf*buoyancy, reference)],
                   assumptions=["OD-wide effective-weight soil prism; no side shear or ballast.",
                                "Groundwater is at/below grade; the pipe is empty.",
                                "Safety factor is null when there is no buoyancy."])


def thrust_force(pressure_kpa, area_m2, fitting="bend", angle_rad=None, a2=None,
                 *, direction=None):
    """Static hydrostatic thrust; DIPRA 7th ed. Figure 4 (no flow momentum).

    bend: 2PA sin(θ/2); tee: PA of branch; dead_end: PA; reducer: P|A-A2|.
    Optional direction is a caller-supplied unit global direction of the resultant;
    no global direction can be inferred from a scalar bend angle.
    """
    p = _number(pressure_kpa, "pressure_kpa", nonnegative=True)
    area = _number(area_m2, "area_m2", positive=True)
    if fitting == "bend":
        angle = _number(angle_rad, "angle_rad", nonnegative=True)
        if angle > math.pi:
            raise CivilError("invalid_input", "Bend angle must be between 0 and pi")
        if a2 is not None:
            raise CivilError("inapplicable_parameter", "Bend does not use a second cross-sectional area")
        thrust = 2*p*area*math.sin(angle/2)
    elif fitting in {"tee", "dead_end"}:
        if angle_rad is not None or a2 is not None:
            raise CivilError("inapplicable_parameter", "Tee/dead-end uses only pressure and area")
        thrust = p*area
    elif fitting == "reducer":
        second = _number(a2, "a2_m2", positive=True)
        if angle_rad is not None:
            raise CivilError("inapplicable_parameter", "Reducer does not use a bend angle")
        thrust = p*abs(area-second)
    else:
        raise CivilError("unsupported_fitting", "Use bend, tee, dead_end or reducer")
    out = _result("hydrostatic_thrust", DIPRA_REFERENCE, thrust_kn=thrust,
                  fitting=fitting, assumptions=["Design pressure includes the caller's test/surge case; no hydrodynamic thrust.",
                                              "For tee, area_m2 is branch cross-sectional area."])
    if direction is not None:
        if not isinstance(direction, (list, tuple)) or len(direction) != 3:
            raise CivilError("invalid_direction", "direction must be a three-component unit vector")
        vec = [_number(v, "direction") for v in direction]
        if not math.isclose(math.sqrt(sum(v*v for v in vec)), 1, rel_tol=0, abs_tol=1e-9):
            raise CivilError("invalid_direction", "direction must be a unit vector; it is not silently normalized")
        out["force_vector_n"] = [1000*thrust*v for v in vec]
    return out


def thrust_block(thrust_kn, bearing_kpa, fs=1.5, *, depth, height_max, od_m=None):
    """DIPRA 7th ed. p7: bearing area Ab=Sf*T/Sb; rectangular face sizing.

    depth and height_max are metres; depth is grade-to-block-bottom depth.
    The bearing face height satisfies h<=depth/2 and the user height_max.
    Prefer width/height in [1,2]; report a warning if depth prevents it. Optional
    OD imposes DIPRA's minimum block height. This sizes a bearing face, NOT its
    thickness/volume, reinforcement, or concrete-to-fitting load transfer.
    """
    t = _number(thrust_kn, "thrust_kn", nonnegative=True)
    bearing = _number(bearing_kpa, "bearing_kpa", positive=True)
    sf = _number(fs, "fs", positive=True)
    depth = _number(depth, "depth_m", positive=True)
    max_height = _number(height_max, "height_max_m", positive=True)
    if sf < 1:
        raise CivilError("invalid_input", "fs must be >=1")
    minimum = 0 if od_m is None else _number(od_m, "od_m", positive=True)
    cap = min(max_height, depth/2)
    if minimum > cap:
        raise CivilError("block_height_infeasible", "OD minimum height exceeds depth/2 or height_max")
    area = t*sf/bearing
    height = min(cap, max(minimum, math.sqrt(area))) if area else 0.0
    width = area/height if height else 0.0
    warnings = []
    if area and not height <= width <= 2*height:
        warnings.append("Bearing face width/height is outside DIPRA's suggested 1–2; revise depth or geometry.")
    return _result("dipra_bearing_thrust_block", DIPRA_REFERENCE, bearing_area_m2=area,
                   width_m=width, height_m=height, bearing_pressure_kpa=t/area if area else 0.0,
                   factored_bearing_pressure_kpa=sf*t/area if area else 0.0,
                   allowable_bearing_pressure_kpa=bearing,
                   safety_factor=sf, warnings=warnings,
                   assumptions=["Soil bearing strength supplied by the design engineer; bearing face is vertical.",
                                "Depth is measured to bottom of block; face height does not exceed half-depth.",
                                "Block thickness, concrete strength, reinforcement, sliding and overturning not evaluated."])


def restrained_length(pressure_kpa, area_m2, angle_rad, od_m, cover_m, gamma_kn_m3,
                      pipe_weight_kn_m, contents_weight_kn_m, cohesion_kpa,
                      friction_angle_rad, adhesion_factor, friction_angle_factor,
                      bearing_reduction_factor, *, fs=1.5, polyethylene_encased=False):
    """DIPRA 7th ed. horizontal-bend length on EACH leg, equations 3–6 in SI.

    L = Sf P A tan(Δ/2)/(Ff + Rs/2), not (Fs+Rs). The half comes from
    triangular passive resistance along each restrained leg.
    Fs=Ap*C+W*tan(δ), Ap=π OD/2 (m² per m), C=fc*Cs, δ=fφ*φ;
    W=2We+Wp+Ww, We=γ H OD; Pp=γ(H+OD/2)Nφ+2Cs√Nφ;
    Nφ=tan²(π/4+φ/2); Rs=Kn*Pp*OD. Ff=Fs for asphaltic coating,
    Ff=0.70 Fs for polyethylene film encasement (DIPRA p10).
    All geotechnical parameters are explicit; no assumed universal soil values.
    Joint axial capacity, bends interacting, groundwater corrections and laying
    condition selection must be checked separately by the design engineer.
    """
    p = _number(pressure_kpa, "pressure_kpa", nonnegative=True)
    area = _number(area_m2, "area_m2", positive=True)
    angle = _number(angle_rad, "angle_rad", nonnegative=True)
    d = _number(od_m, "od_m", positive=True)
    h = _number(cover_m, "cover_m", nonnegative=True)
    gamma = _number(gamma_kn_m3, "gamma_kn_m3", positive=True)
    wp = _number(pipe_weight_kn_m, "pipe_weight_kn_m", nonnegative=True)
    ww = _number(contents_weight_kn_m, "contents_weight_kn_m", nonnegative=True)
    cs = _number(cohesion_kpa, "cohesion_kpa", nonnegative=True)
    phi = _number(friction_angle_rad, "friction_angle_rad", nonnegative=True)
    fc = _number(adhesion_factor, "adhesion_factor", nonnegative=True)
    fphi = _number(friction_angle_factor, "friction_angle_factor", nonnegative=True)
    kn = _number(bearing_reduction_factor, "bearing_reduction_factor", nonnegative=True)
    sf = _number(fs, "fs", positive=True)
    if angle >= math.pi or phi >= math.pi/2:
        raise CivilError("invalid_input", "Require bend angle<pi and soil friction angle<pi/2")
    if sf < 1 or max(fc, fphi, kn) > 1 or not isinstance(polyethylene_encased, bool):
        raise CivilError("invalid_input", "Require fs>=1, reduction factors in [0,1], and boolean encasement")
    earth = gamma*h*d
    normal = 2*earth+wp+ww
    contact = math.pi*d/2
    friction = contact*fc*cs+normal*math.tan(fphi*phi)
    coating_factor = 0.7 if polyethylene_encased else 1.0
    ff = coating_factor*friction
    nphi = math.tan(math.pi/4+phi/2)**2
    passive = gamma*(h+d/2)*nphi+2*cs*math.sqrt(nphi)
    rs = kn*passive*d
    resistance = ff+0.5*rs
    if resistance <= 0:
        raise CivilError("no_soil_restraint", "Friction plus half passive bearing must be positive")
    length = sf*p*area*math.tan(angle/2)/resistance
    return _result("dipra_horizontal_bend_restraint", DIPRA_REFERENCE, restrained_length_m=length,
                   length_each_leg_m=length, total_restrained_length_m=2*length,
                   soil_friction_kn_m=friction, unit_friction_kn_m=ff,
                   unit_bearing_kn_m=rs, effective_bearing_kn_m=0.5*rs,
                   total_resistance_kn_m=resistance, passive_pressure_kpa=passive,
                   earth_load_kn_m=earth, encasement_factor=coating_factor, safety_factor=sf,
                   assumptions=["Isolated horizontal bend; restrained length is required on both legs.",
                                "Friction is constant; passive bearing falls linearly to zero along each leg.",
                                "No joint-capacity, encroachment, future-excavation or groundwater correction."])
