"""Hydraulic component sizing calculations for engine.hydraulic.

Physics: cylinder force/flow/area, pump power/pressure, accumulator pre-charge,
valve Kv flow equation.  No exotic dependencies — math only.
Returns contract-shaped dicts: ``{"ok": True, ...}`` or ``{"ok": False, "reason": ..., "hint": ...}``.

FMU simulation via fmpy is an optional soft-fail seam:
``fmu_simulate`` returns ``{"ok": False, "skipped": True}`` when fmpy is not installed.
"""

from __future__ import annotations

import math
from typing import Any, Optional


# ── contract-shaped error helper ──────────────────────────────────────────────

def _err(reason: str, hint: Optional[str] = None, **extra: Any) -> dict:
    out: dict[str, Any] = {"ok": False, "reason": reason}
    if hint:
        out["hint"] = hint
    out.update(extra)
    return out


# ── cylinder sizing ───────────────────────────────────────────────────────────

def size_cylinder(*, bore_mm: float, rod_mm: float = 0.0,
                  pressure_bar: float, stroke_mm: float,
                  velocity_mm_s: float = 50.0,
                  efficiency: float = 0.95) -> dict:
    """Size a hydraulic cylinder: force, flow, and cycle time.

    Physics
    -------
    Extend (full bore area):
        A_bore  = π/4 · bore_mm² (mm²)
        F_ext   = A_bore · pressure_bar · 1e5 (N) → kN / 1000
        Q_ext   = A_bore_m² · velocity_mm_s·1e-3 (m/s) · 60 (s/min) · 1000 (L/m³)

    Retract (annulus area):
        A_ann   = π/4 · (bore_mm² − rod_mm²)
        F_ret   = A_ann · pressure_bar · 1e5 (N) → kN
        Q_ret   = A_ann_m² · velocity (same conversion)

    Cycle time = stroke_mm / velocity_mm_s  (seconds, one direction)

    Hand-calc check (50 mm bore, 200 bar):
        A_bore = π/4 · 0.05² = 1.9635e-3 m²
        F = 1.9635e-3 × 200e5 = 39 270 N ≈ 39.27 kN  ✓
    """
    if bore_mm <= 0:
        return _err("bore_mm must be > 0", hint="typical cylinders 25–500 mm bore")
    if rod_mm < 0:
        return _err("rod_mm must be >= 0", hint="use 0 for single-acting / rodless")
    if rod_mm >= bore_mm:
        return _err("rod_mm must be < bore_mm",
                    hint="rod diameter must be smaller than bore")
    if pressure_bar <= 0:
        return _err("pressure_bar must be > 0")
    if stroke_mm <= 0:
        return _err("stroke_mm must be > 0")
    if velocity_mm_s <= 0:
        return _err("velocity_mm_s must be > 0")
    if not (0.0 < efficiency <= 1.0):
        return _err("efficiency must be in (0, 1]")

    # Areas in m²
    a_bore_m2 = math.pi / 4.0 * (bore_mm * 1e-3) ** 2
    a_ann_m2 = math.pi / 4.0 * ((bore_mm * 1e-3) ** 2 - (rod_mm * 1e-3) ** 2)

    pressure_pa = pressure_bar * 1e5  # Pa

    # Forces (kN)
    extend_force_kn = a_bore_m2 * pressure_pa * efficiency * 1e-3
    retract_force_kn = a_ann_m2 * pressure_pa * efficiency * 1e-3

    # Velocity in m/s
    velocity_m_s = velocity_mm_s * 1e-3

    # Flows (L/min): Q = A(m²) × v(m/s) × 60(s/min) × 1000(L/m³)
    flow_extend_lpm = a_bore_m2 * velocity_m_s * 60.0 * 1000.0
    flow_retract_lpm = a_ann_m2 * velocity_m_s * 60.0 * 1000.0

    # Cycle time (seconds, one direction)
    cycle_time_s = stroke_mm / velocity_mm_s

    return {
        "ok": True,
        "bore_mm": bore_mm,
        "rod_mm": rod_mm,
        "pressure_bar": pressure_bar,
        "stroke_mm": stroke_mm,
        "velocity_mm_s": velocity_mm_s,
        "efficiency": efficiency,
        "area_bore_cm2": round(a_bore_m2 * 1e4, 4),
        "area_annulus_cm2": round(a_ann_m2 * 1e4, 4),
        "extend_force_kn": round(extend_force_kn, 4),
        "retract_force_kn": round(retract_force_kn, 4),
        "flow_extend_lpm": round(flow_extend_lpm, 4),
        "flow_retract_lpm": round(flow_retract_lpm, 4),
        "cycle_time_s": round(cycle_time_s, 4),
        "next_actions": [
            "confirm pump flow >= flow_extend_lpm to meet velocity target",
            "check rod buckling for long strokes (Euler column formula)",
        ],
    }


# ── pump sizing ────────────────────────────────────────────────────────────────

def size_pump(*, flow_lpm: float, pressure_bar: float,
              efficiency: float = 0.85,
              speed_rpm: float = 1500.0) -> dict:
    """Size a hydraulic pump: displacement, power, and shaft torque.

    Physics
    -------
    Displacement  = Q(L/min) × 1000(cc/L) / speed(rpm)       [cc/rev]
    Hydraulic kW  = Q(m³/s) × ΔP(Pa) / 1000
                  = (flow_lpm/60/1000) × (pressure_bar×1e5) / 1000
    Shaft kW      = hydraulic_kW / efficiency
    Torque        = shaft_kW×1000 / (2π × speed_rpm/60)       [N·m]

    Hand-calc check (10 L/min, 200 bar, η=0.85):
        hydraulic_kW = (10/60/1000) × (200×1e5) / 1000 = 3.333 kW
        shaft_kW     = 3.333 / 0.85 = 3.922 kW  ✓
    """
    if flow_lpm <= 0:
        return _err("flow_lpm must be > 0")
    if pressure_bar <= 0:
        return _err("pressure_bar must be > 0")
    if not (0.0 < efficiency <= 1.0):
        return _err("efficiency must be in (0, 1]")
    if speed_rpm <= 0:
        return _err("speed_rpm must be > 0")

    displacement_cc_rev = flow_lpm * 1000.0 / speed_rpm

    # Hydraulic power (kW)
    flow_m3s = flow_lpm / 60.0 / 1000.0
    pressure_pa = pressure_bar * 1e5
    power_kw = flow_m3s * pressure_pa / 1000.0

    shaft_power_kw = power_kw / efficiency

    # Shaft torque (N·m): P(W) = T(N·m) × ω(rad/s)
    omega_rad_s = 2.0 * math.pi * speed_rpm / 60.0
    torque_nm = shaft_power_kw * 1000.0 / omega_rad_s

    return {
        "ok": True,
        "flow_lpm": flow_lpm,
        "pressure_bar": pressure_bar,
        "efficiency": efficiency,
        "speed_rpm": speed_rpm,
        "displacement_cc_rev": round(displacement_cc_rev, 4),
        "hydraulic_power_kw": round(power_kw, 4),
        "shaft_power_kw": round(shaft_power_kw, 4),
        "torque_nm": round(torque_nm, 4),
        "next_actions": [
            "select a pump with displacement_cc_rev >= computed value",
            "verify motor shaft power >= shaft_power_kw",
        ],
    }


# ── accumulator sizing ────────────────────────────────────────────────────────

def size_accumulator(*, pre_charge_bar: float, max_pressure_bar: float,
                     min_pressure_bar: float, volume_stored_l: float) -> dict:
    """Size a gas-charged hydraulic accumulator using Boyle's law (isothermal).

    Physics (ideal gas, isothermal)
    --------------------------------
    At P_max, gas occupies: V_gas_max = V_total × P_pre / P_max
    At P_min, gas occupies: V_gas_min = V_total × P_pre / P_min
    Usable (stored) volume = V_gas_min − V_gas_max
                           = V_total × P_pre × (1/P_min − 1/P_max)

    Inverting for V_total given V_stored:
        V_total = V_stored / (P_pre × (1/P_min − 1/P_max))

    Constraints: P_pre < P_min < P_max  (standard accumulator sizing rule)

    Hand-calc check (pre=100 bar, max=250 bar, min=150 bar, stored=5 L):
        factor = 100 × (1/150 − 1/250) = 100 × (0.006667 − 0.004) = 100 × 0.002667 = 0.2667
        V_total = 5 / 0.2667 ≈ 18.75 L  (≈ 18.73 L with full precision)  ✓
    """
    if pre_charge_bar <= 0:
        return _err("pre_charge_bar must be > 0")
    if min_pressure_bar <= 0:
        return _err("min_pressure_bar must be > 0")
    if max_pressure_bar <= 0:
        return _err("max_pressure_bar must be > 0")
    if volume_stored_l <= 0:
        return _err("volume_stored_l must be > 0")
    if not (pre_charge_bar < min_pressure_bar):
        return _err("pre_charge_bar must be < min_pressure_bar",
                    hint="nitrogen pre-charge pressure must be below the minimum working pressure")
    if not (min_pressure_bar < max_pressure_bar):
        return _err("min_pressure_bar must be < max_pressure_bar")

    factor = pre_charge_bar * (1.0 / min_pressure_bar - 1.0 / max_pressure_bar)
    if factor <= 0:
        return _err("accumulator sizing factor is zero or negative — check pressure values")

    total_volume_l = volume_stored_l / factor
    pressure_ratio = max_pressure_bar / pre_charge_bar

    return {
        "ok": True,
        "pre_charge_bar": pre_charge_bar,
        "min_pressure_bar": min_pressure_bar,
        "max_pressure_bar": max_pressure_bar,
        "volume_stored_l": volume_stored_l,
        "total_volume_l": round(total_volume_l, 4),
        "pressure_ratio": round(pressure_ratio, 4),
        "assumption": "isothermal (Boyle's law); ideal gas; no thermal correction",
        "next_actions": [
            "select accumulator with total_volume_l >= computed value",
            "verify pre-charge pressure with nitrogen at ambient temperature before fill",
        ],
    }


# ── valve flow ────────────────────────────────────────────────────────────────

def valve_flow(*, Cv: float, pressure_drop_bar: float,
               fluid_sg: float = 0.87) -> dict:
    """Estimate valve flow rate using the Kv/Cv metric flow equation.

    Metric Kv form (ISA/IEC):
        Q (m³/h) = Kv × √(ΔP_bar / SG)
        Q (L/min) = Q(m³/h) × 1000 / 60

    Cv → Kv conversion: Kv = Cv × 0.865  (1 US gpm·psi^-0.5 = 0.865 m³/h·bar^-0.5)

    Parameters
    ----------
    Cv:               valve flow coefficient (US gpm / psi^0.5)
    pressure_drop_bar: differential pressure across valve (bar)
    fluid_sg:         specific gravity relative to water at 15°C (default 0.87 for hydraulic oil)
    """
    if Cv <= 0:
        return _err("Cv must be > 0", hint="typical control valves 0.1–500 Cv")
    if pressure_drop_bar <= 0:
        return _err("pressure_drop_bar must be > 0")
    if fluid_sg <= 0:
        return _err("fluid_sg must be > 0", hint="hydraulic oil ≈ 0.87; water = 1.0")

    kv = Cv * 0.865  # m³/h per bar^0.5
    flow_m3h = kv * math.sqrt(pressure_drop_bar / fluid_sg)
    flow_lpm = flow_m3h * 1000.0 / 60.0

    return {
        "ok": True,
        "Cv": Cv,
        "Kv": round(kv, 4),
        "pressure_drop_bar": pressure_drop_bar,
        "fluid_sg": fluid_sg,
        "flow_m3h": round(flow_m3h, 4),
        "flow_lpm": round(flow_lpm, 4),
        "next_actions": [
            "confirm valve Cv rating meets or exceeds required flow at design dP",
        ],
    }


# ── FMU simulation (optional soft-fail seam) ──────────────────────────────────

def fmu_simulate(circuit_doc: dict, *, fmu_path: Optional[str] = None,
                 stop_time: float = 10.0) -> dict:
    """Simulate a hydraulic circuit via an OpenHydraulics FMU using fmpy.

    This is a **soft-fail seam**: if fmpy is not installed or no fmu_path is given,
    the function returns ``{"ok": False, "skipped": True}`` rather than raising.
    This mirrors the CoolProp/thermo pattern in engine.piping.properties.

    Parameters
    ----------
    circuit_doc:  ``aicad.hydraulic/1`` document (for context / metadata only)
    fmu_path:     path to a compiled OpenHydraulics .fmu file (Modelica/FMI 2.0)
    stop_time:    simulation stop time in seconds (default 10.0)
    """
    # Soft-fail: fmpy not installed
    try:
        import fmpy  # noqa: F401
    except ImportError:
        return {
            "ok": False,
            "skipped": True,
            "reason": "fmpy not installed",
            "hint": (
                "pip install fmpy; provide a compiled OpenHydraulics FMU via fmu_path. "
                "OpenHydraulics Modelica source: https://openmodelica.org/; "
                "compile with OpenModelica (omc) or Dymola to produce an .fmu file."
            ),
        }

    # Soft-fail: no FMU path provided
    if fmu_path is None:
        return {
            "ok": False,
            "skipped": True,
            "reason": "fmu_path not provided",
            "hint": (
                "pass fmu_path='path/to/circuit.fmu' (a compiled OpenHydraulics FMU). "
                "fmpy is installed and ready."
            ),
        }

    # FMU execution path (only reached when fmpy is installed and fmu_path given)
    try:
        from fmpy import read_model_description, simulate_fmu
        model_desc = read_model_description(fmu_path)
        variables = [v.name for v in model_desc.modelVariables]
        result = simulate_fmu(
            fmu_path,
            stop_time=float(stop_time),
            output=variables[:20],  # cap output variables for memory safety
        )
        # result is a numpy structured array; convert first few rows to list for JSON
        result_rows = [list(row) for row in result[:10]]
        return {
            "ok": True,
            "fmu_path": fmu_path,
            "stop_time": stop_time,
            "variables": variables[:20],
            "result_rows": result_rows,
            "next_actions": [
                "inspect variables for pressure/flow transients",
                "compare steady-state values to size_pump / size_cylinder results",
            ],
        }
    except Exception as exc:  # noqa: BLE001
        return _err(f"fmpy simulation failed: {exc}",
                    hint="check fmu_path exists and is a valid FMI 2.0 .fmu file")
