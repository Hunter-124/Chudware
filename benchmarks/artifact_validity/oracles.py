"""Independent engineering references for the facilities tasks (F1–F4).

Pure Python, no engine and no Chudware imports: every number a facilities grader compares
a saved study against is recomputed here from first principles, never by asking the solver
that produced the study. Formulas are the textbook/code equations named at each function;
units are stated in every name or docstring.

Sources:
  * Fluid power: Q = D·n·η_v; cylinder speed v = Q / (π/4·d²).
  * Pipe flow: Colebrook–White (solved by fixed-point iteration), Darcy–Weisbach with
    constant minor-loss K, ΔP = (f·L/D + ΣK)·ρ·v²/2.
  * ASME B31.3 §304.1.2 eq. (3a): t = P·D / (2(S·E·W + P·Y)); t_m = (t + c)/(1 − mill);
    MAWP = 2·S·E·W·t_a / (D − 2·Y·t_a) with t_a = t_nom·(1 − mill) − c.
  * ASME VIII-1 UG-27(c)(1) t = P·R/(S·E − 0.6P); UG-32(d) t = P·D/(2·S·E − 0.2P);
    MAWP back-calculated from the same equations; UG-99(b) hydrotest 1.3·MAWP·LSR;
    UG-22 static head ρ·g·h.
  * Ducts: ideal-gas air density, Sutherland viscosity, Huebscher equivalent diameter
    De = 1.30·(a·b)^0.625/(a+b)^0.25, Colebrook friction, equal-friction diameter by
    bisection, path sums through the tree (critical path = fan static).
"""
from __future__ import annotations

import math
from typing import Iterable, Mapping, Sequence

G = 9.80665                      # m/s², standard gravity
P_ATM = 101_325.0                # Pa
R_UNIVERSAL = 8.314462618        # J/(mol·K)
M_AIR = 0.0289647                # kg/mol, dry air
LAMINAR_RE = 2300.0

# ── fluid power ───────────────────────────────────────────────────────────────


def pump_flow_lpm(displacement_cc_rev: float, speed_rpm: float, vol_efficiency: float) -> float:
    """Delivered pump flow Q = D·n·η_v (L/min)."""
    return displacement_cc_rev * speed_rpm * vol_efficiency / 1000.0


def circle_area_mm2(diameter_mm: float) -> float:
    return math.pi / 4.0 * diameter_mm * diameter_mm


def cylinder_speed_mms(flow_lpm: float, bore_mm: float) -> float:
    """Extension speed v = Q / A_bore (mm/s) with Q in L/min."""
    return flow_lpm * 1.0e6 / 60.0 / circle_area_mm2(bore_mm)


def stroke_time_s(stroke_mm: float, speed_mms: float) -> float:
    """Ideal constant-speed stroke time (s)."""
    return stroke_mm / speed_mms


# ── pipe flow ─────────────────────────────────────────────────────────────────


def reynolds(density: float, velocity: float, diameter_m: float, viscosity: float) -> float:
    return density * abs(velocity) * diameter_m / viscosity


def colebrook(re: float, rel_roughness: float, *, tol: float = 1e-13, max_iter: int = 200) -> float:
    """Darcy friction factor; 64/Re below Re 2300, else Colebrook–White by iteration on
    x = 1/√f:  x = −2·log10(ε/(3.7D) + 2.51·x/Re)."""
    if re <= 0:
        raise ValueError("Reynolds number must be positive")
    if re < LAMINAR_RE:
        return 64.0 / re
    x = -2.0 * math.log10(rel_roughness / 3.7 + 5.74 / re ** 0.9)  # Swamee–Jain start
    for _ in range(max_iter):
        nxt = -2.0 * math.log10(rel_roughness / 3.7 + 2.51 * x / re)
        if abs(nxt - x) < tol:
            x = nxt
            break
        x = nxt
    return 1.0 / (x * x)


def pipe_velocity_ms(flow_m3h: float, inner_dia_mm: float) -> float:
    return flow_m3h / 3600.0 / (circle_area_mm2(inner_dia_mm) * 1e-6)


def darcy_dp_pa(friction: float, length_m: float, diameter_m: float, k_total: float,
                density: float, velocity: float) -> float:
    """ΔP = (f·L/D + ΣK)·ρ·v²/2 (Pa)."""
    return (friction * length_m / diameter_m + k_total) * 0.5 * density * velocity * velocity


def pipe_segment(flow_m3h: float, inner_dia_mm: float, length_m: float, k_total: float,
                 density: float, viscosity: float, roughness_mm: float) -> dict:
    """Velocity, Re, Colebrook f and Darcy–Weisbach ΔP of one liquid pipe segment."""
    d = inner_dia_mm / 1000.0
    v = pipe_velocity_ms(flow_m3h, inner_dia_mm)
    re = reynolds(density, v, d, viscosity)
    f = colebrook(re, roughness_mm / inner_dia_mm)
    return {"velocity_ms": v, "reynolds": re, "friction_factor": f,
            "dp_kpa": darcy_dp_pa(f, length_m, d, k_total, density, v) / 1000.0}


# ASME B36.10M: NPS → (OD mm, {schedule: wall mm}); only the sizes the tasks use.
PIPE_DIMENSIONS: dict[float, tuple[float, dict[str, float]]] = {
    2.0: (60.3, {"40": 3.91, "80": 5.54}),
}


def pipe_od_wall_mm(nps: float, schedule: str) -> tuple[float, float]:
    od, walls = PIPE_DIMENSIONS[float(nps)]
    return od, walls[str(schedule)]


def b31_3_pressure_thickness_mm(p_mpa: float, od_mm: float, s_mpa: float, e: float = 1.0,
                                w: float = 1.0, y: float = 0.4) -> float:
    """B31.3 §304.1.2 eq. (3a): t = P·D / (2(S·E·W + P·Y))."""
    return p_mpa * od_mm / (2.0 * (s_mpa * e * w + p_mpa * y))


def b31_3_required_nominal_mm(t_pressure_mm: float, corrosion_mm: float, mill_frac: float) -> float:
    """t_m = t + c, grossed up over the mill under-tolerance: (t + c)/(1 − mill)."""
    return (t_pressure_mm + corrosion_mm) / (1.0 - mill_frac)


def b31_3_mawp_kpa(t_nominal_mm: float, corrosion_mm: float, mill_frac: float, od_mm: float,
                   s_mpa: float, e: float = 1.0, w: float = 1.0, y: float = 0.4) -> float:
    """Eq. (3a) solved for P on the available wall t_a = t_nom·(1 − mill) − c."""
    t_a = t_nominal_mm * (1.0 - mill_frac) - corrosion_mm
    return 2.0 * s_mpa * e * w * t_a / (od_mm - 2.0 * y * t_a) * 1000.0


# ── pressure vessels (ASME VIII-1) ────────────────────────────────────────────


def static_head_kpa(density: float, height_mm: float) -> float:
    """UG-22 hydrostatic head ρ·g·h (kPa)."""
    return density * G * height_mm / 1000.0 / 1000.0


def ug27_shell_thickness_mm(p_mpa: float, inside_radius_mm: float, s_mpa: float, e: float) -> float:
    """UG-27(c)(1) circumferential stress: t = P·R/(S·E − 0.6P)."""
    return p_mpa * inside_radius_mm / (s_mpa * e - 0.6 * p_mpa)


def ug32d_ellipsoidal_thickness_mm(p_mpa: float, inside_diameter_mm: float, s_mpa: float,
                                   e: float) -> float:
    """UG-32(d) 2:1 ellipsoidal head: t = P·D/(2·S·E − 0.2P)."""
    return p_mpa * inside_diameter_mm / (2.0 * s_mpa * e - 0.2 * p_mpa)


def ug27_shell_mawp_mpa(t_mm: float, inside_radius_mm: float, s_mpa: float, e: float) -> float:
    """UG-27(c)(1) solved for P: P = S·E·t/(R + 0.6t)."""
    return s_mpa * e * t_mm / (inside_radius_mm + 0.6 * t_mm)


def ug32d_ellipsoidal_mawp_mpa(t_mm: float, inside_diameter_mm: float, s_mpa: float,
                               e: float) -> float:
    """UG-32(d) solved for P: P = 2·S·E·t/(D + 0.2t)."""
    return 2.0 * s_mpa * e * t_mm / (inside_diameter_mm + 0.2 * t_mm)


def ug99b_hydrotest_kpa(mawp_kpa: float, stress_ratio: float = 1.0) -> float:
    """UG-99(b): 1.3 × MAWP × LSR (lowest test-to-design allowable stress ratio)."""
    return 1.3 * mawp_kpa * stress_ratio


def round_up_to_series(value: float, series: Sequence[float]) -> float | None:
    """Smallest member of ``series`` (ascending) that is ≥ ``value``; None beyond it."""
    for s in series:
        if s >= value - 1e-9:
            return float(s)
    return None


def ellipsoidal_2_1_depth_mm(inside_diameter_mm: float) -> float:
    return inside_diameter_mm / 4.0


def vessel_volume_m3(inside_diameter_mm: float, tan_tan_mm: float) -> float:
    """Cylinder π/4·D²·L plus two 2:1 ellipsoidal heads of π·D³/24 each."""
    d = inside_diameter_mm / 1000.0
    return math.pi / 4.0 * d * d * tan_tan_mm / 1000.0 + 2.0 * math.pi * d ** 3 / 24.0


def vessel_metal_volume_mm3(inside_diameter_mm: float, tan_tan_mm: float, t_shell_mm: float,
                            t_head_mm: float) -> float:
    """Shell annulus over the tangent length plus two semi-ellipsoidal head shells whose
    outer semi-axes are the inner ones grown by the head thickness."""
    a, b = inside_diameter_mm / 2.0, inside_diameter_mm / 4.0
    shell = math.pi / 4.0 * ((inside_diameter_mm + 2.0 * t_shell_mm) ** 2
                             - inside_diameter_mm ** 2) * tan_tan_mm
    head = 2.0 / 3.0 * math.pi * ((a + t_head_mm) ** 2 * (b + t_head_mm) - a * a * b)
    return shell + 2.0 * head


# ── air ducts ─────────────────────────────────────────────────────────────────

# EN 1506 / ISO 3 R20 preferred circular duct diameters (mm).
ROUND_DUCT_SERIES_MM: tuple[float, ...] = (
    100, 112, 125, 140, 150, 160, 180, 200, 224, 250, 280, 300, 315, 355, 400, 450, 500,
    560, 600, 630, 710, 800, 900, 1000)


def air_density(temp_c: float, pressure_pa: float = P_ATM) -> float:
    """Ideal-gas dry air ρ = P·M/(R·T) (kg/m³)."""
    return pressure_pa * M_AIR / (R_UNIVERSAL * (temp_c + 273.15))


def air_viscosity(temp_c: float) -> float:
    """Sutherland's law for air: μ = μ0·(T/T0)^1.5·(T0 + S)/(T + S) (Pa·s)."""
    t, t0, s, mu0 = temp_c + 273.15, 273.15, 110.4, 1.716e-5
    return mu0 * (t / t0) ** 1.5 * (t0 + s) / (t + s)


def huebscher_de_mm(width_mm: float, height_mm: float) -> float:
    """Huebscher equal-friction equivalent diameter of a rectangular duct (mm)."""
    return 1.30 * (width_mm * height_mm) ** 0.625 / (width_mm + height_mm) ** 0.25


def duct_friction(flow_m3s: float, diameter_mm: float, roughness_mm: float, density: float,
                  viscosity: float) -> dict:
    """Round (or equivalent-round) duct: velocity through πD²/4, Re, Colebrook f and the
    friction gradient f/D·ρv²/2 (Pa/m)."""
    d = diameter_mm / 1000.0
    v = flow_m3s / (math.pi / 4.0 * d * d)
    re = reynolds(density, v, d, viscosity)
    f = colebrook(re, roughness_mm / diameter_mm)
    return {"velocity_ms": v, "reynolds": re, "friction_factor": f,
            "gradient_pa_m": f / d * 0.5 * density * v * v}


def equal_friction_diameter_mm(flow_m3s: float, rate_pa_m: float, roughness_mm: float,
                               density: float, viscosity: float) -> float:
    """Exact round diameter whose friction gradient equals ``rate_pa_m`` (bisection; the
    gradient falls monotonically with D)."""
    lo, hi = math.log(5.0), math.log(20_000.0)
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        g = duct_friction(flow_m3s, math.exp(mid), roughness_mm, density, viscosity)
        if g["gradient_pa_m"] > rate_pa_m:
            lo = mid
        else:
            hi = mid
    return math.exp(0.5 * (lo + hi))


def select_round_duct_mm(flow_m3s: float, rate_pa_m: float, vmax_ms: float, roughness_mm: float,
                         density: float, viscosity: float,
                         series: Sequence[float] = ROUND_DUCT_SERIES_MM) -> float | None:
    """Smallest series diameter meeting both the friction rate and the velocity cap."""
    d_exact = equal_friction_diameter_mm(flow_m3s, rate_pa_m, roughness_mm, density, viscosity)
    for d in series:
        if d < d_exact - 1e-6:
            continue
        if flow_m3s / (math.pi / 4.0 * (d / 1000.0) ** 2) <= vmax_ms + 1e-9:
            return float(d)
    return None


def duct_segment(flow_m3s: float, length_m: float, k_fittings: float, *, density: float,
                 viscosity: float, roughness_mm: float, diameter_mm: float | None = None,
                 width_mm: float | None = None, height_mm: float | None = None) -> dict:
    """Pressure terms of one duct segment at its ACTUAL section.

    Friction uses the (Huebscher equivalent) round diameter at the segment flow; fitting
    losses K·ρv²/2 use the mean velocity through the actual section."""
    if diameter_mm is not None:
        de = float(diameter_mm)
        area_m2 = math.pi / 4.0 * (de / 1000.0) ** 2
    else:
        if width_mm is None or height_mm is None:
            raise ValueError("a rectangular segment needs width_mm and height_mm")
        de = huebscher_de_mm(width_mm, height_mm)
        area_m2 = width_mm * height_mm * 1e-6
    fr = duct_friction(flow_m3s, de, roughness_mm, density, viscosity)
    v = flow_m3s / area_m2
    dp_f = fr["gradient_pa_m"] * length_m
    dp_k = k_fittings * 0.5 * density * v * v
    return {"equivalent_diameter_mm": de, "velocity_ms": v, "reynolds": fr["reynolds"],
            "friction_factor": fr["friction_factor"], "friction_rate_pa_per_m": fr["gradient_pa_m"],
            "dp_friction_pa": dp_f, "dp_fittings_pa": dp_k, "dp_total_pa": dp_f + dp_k}


def critical_path(segments: Iterable[Mapping]) -> tuple[list[str], float]:
    """Highest cumulative root-to-segment loss in a tree of ``{id, parent, dp_total_pa}``.

    Returns ``(path ids from the root, path total Pa)`` — the required fan static."""
    rows = {str(s["id"]): s for s in segments}
    best: tuple[list[str], float] = ([], -math.inf)
    for sid in rows:
        path, total, cur, seen = [], 0.0, sid, set()
        while cur is not None:
            if cur in seen or cur not in rows:
                raise ValueError(f"segment tree broken at {cur!r}")
            seen.add(cur)
            path.append(cur)
            total += float(rows[cur]["dp_total_pa"])
            parent = rows[cur].get("parent")
            cur = str(parent) if parent not in (None, "") else None
        if total > best[1]:
            best = (list(reversed(path)), total)
    return best
