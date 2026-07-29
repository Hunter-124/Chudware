"""tool_guidance.sim — curated options for the sim tool family (FEA, CFD, motion, piping, hydraulics, structural)."""
from __future__ import annotations

from typing import Any

ENTRIES: dict[str, dict[str, Any]] = {
    "bearing_life": {
        "prompt_note": (
            "Standalone hand-calc — no document needed. Set X/Y when Fa "
            "matters (defaults zero the axial term); C must come from a "
            "datasheet."
        ),
    },
    "gear_check": {
        "prompt_note": (
            "SF fields stay null until you pass the allowable stresses — "
            "supply S_t/S_c for a pass/fail read; take J/I from AGMA 908 "
            "charts."
        ),
    },
    "hydraulic_add_component": {
        "prompt_note": (
            "Needs an active circuit (hydraulic_new first). Per-type param "
            "keys are in contracts/hydraulic_schema.md; fill sourcing so "
            "hydraulic_bom isn't un-sourced."
        ),
    },
    "hydraulic_add_hose": {
        "prompt_note": (
            "Connects ports by component_id+port from "
            "hydraulic_add_component's returned ids; a net takes >=2 "
            "connections (pressure/tank/work_a/work_b/drain)."
        ),
    },
    "hydraulic_bom": {
        "prompt_note": (
            "Roll up after components are in; lines without mpn come back "
            "flagged un-sourced — the summary lists what still needs "
            "sourcing."
        ),
    },
    "hydraulic_fea_handoff": {
        "prompt_note": (
            "Only builds run_fea_static arg sets — it never solves. Pass "
            "fea_args to run_fea_static, then close the chain with "
            "hydraulic_verify_fea."
        ),
    },
    "hydraulic_new": {
        "prompt_note": (
            "First call of the hydraulic workflow — the returned doc becomes "
            "the implicit session doc for all later hydraulic_* calls."
        ),
    },
    "hydraulic_piping_handoff": {
        "prompt_note": (
            "Runs the real piping solve inline and returns the simhandoff "
            "envelope — pass load_pressure_bar for the pump margin; verify "
            "with hydraulic_verify_piping."
        ),
    },
    "hydraulic_size_cylinder": {
        "prompt_note": (
            "Pure sizing hand-calc, no document needed; rod_mm=0 makes "
            "retract=extend. Feed the recommended flow to "
            "hydraulic_size_pump next."
        ),
    },
    "hydraulic_size_pump": {
        "prompt_note": (
            "Hand-calc counterpart to hydraulic_size_cylinder; displacement "
            "scales inversely with speed_rpm, so set the real motor speed."
        ),
    },
    "hydraulic_verify_fea": {
        "prompt_note": (
            "Verify node for the hydraulic-to-FEA envelope (defaults to the "
            "session's last); fea_result is advisory — the force-magnitude "
            "checks gate the verdict."
        ),
    },
    "hydraulic_verify_piping": {
        "prompt_note": (
            "Verify node for the piping coupling; no args reuses the "
            "session's last handoff+solve. Pass supply/load pressures to "
            "refresh the pump margin."
        ),
    },
    "motion_fea_handoff": {
        "prompt_note": (
            "One-call worst-frame stress check on an assembly link (needs "
            "mates). dry_run=True shows the loads before you pay for the "
            "FEA solve."
        ),
        "timeout_s": 600,
    },
    "piping_cfd_handoff": {
        "prompt_note": (
            "Solve -> bore STEP -> LBM CFD in one call; use background=True "
            "on big grids. RANS accuracy needs a manual run_cfd_accurate + "
            "piping_verify_cfd."
        ),
        "timeout_s": 600,
    },
    "piping_fluid": {
        "prompt_note": (
            "Pre-flight the fluid before piping_solve — same sourced values "
            "the solver uses; unknown names return ok:false with a hint, "
            "never invented numbers."
        ),
    },
    "piping_internal_volume": {
        "prompt_note": (
            "Geometry bridge to CFD: import the STEP as a part, then "
            "run_cfd_flow. piping_cfd_handoff already does this whole "
            "chain."
        ),
    },
    "piping_operating_point": {
        "prompt_note": (
            "Pump-curve CSV (flow m3/h, head m) as text or path; omit "
            "system to derive the system curve from a piping doc + "
            "boundary."
        ),
    },
    "piping_report": {
        "prompt_note": (
            "No-arg call re-reads the most recent piping_solve — cheapest "
            "K-factor breakdown; pass doc+boundary only for a fresh "
            "network."
        ),
    },
    "piping_solve": {
        "prompt_note": (
            "At least one fixed pressure/head node is required; node ids "
            "come from snapped run endpoints — read them from the result "
            "before writing boundary entries."
        ),
    },
    "piping_verify_cfd": {
        "prompt_note": (
            "Verify node after piping_cfd_handoff (reuses its stored "
            "result with no args); compares dP budgets at tolerance_pct, "
            "not flow fields."
        ),
    },
    "run_cfd_accurate": {
        "prompt_note": (
            "The trustworthy absolute Cd/Cl path — needs OpenFOAM and takes "
            "minutes. Always returns a job_id immediately; poll get_job. "
            "Start preset='coarse'."
        ),
    },
    "run_cfd_flow": {
        "prompt_note": (
            "Directional LBM for A-vs-B trends, not certified Cd (use "
            "run_cfd_accurate). No material= argument — the medium is "
            "fluid='air'|'water'. Check min_feature_voxels >= ~8; "
            "background=True for cells>=120."
        ),
        "timeout_s": 300,
    },
    "run_fea_buckling": {
        "prompt_note": (
            "ccx box-proxy column, not the real geometry; lambda scales "
            "the applied load (lambda<1 means buckles). Needs CalculiX — "
            "absent ccx returns ok:false, skipped."
        ),
        # Synchronous ccx subprocess solve, same solver class as run_fea_modal
        # (already 300s) — a fine-mesh eigenvalue run can grind for minutes.
        "timeout_s": 300,
    },
    "run_fea_contact": {
        "prompt_note": (
            "ccx two-block proxy proving load transfer across an "
            "interface, not your actual contact pair; needs CalculiX. Use "
            "run_fea_static for real stresses."
        ),
        # Synchronous NONLINEAR (NLGEOM) ccx subprocess solve — the slowest of
        # the fea_advanced proxies; give it the same budget as the other ccx runs.
        "timeout_s": 300,
    },
    "run_fea_fatigue": {
        "prompt_note": (
            "Reuses the static stress field — pass the same fixtures/loads "
            "as run_fea_static. regime='infinite' means >1e6 cycles; "
            "compare the theories block."
        ),
        "timeout_s": 300,
    },
    "run_fea_frequency_response": {
        "prompt_note": (
            "Modal-superposition vibration on the held part; without "
            "psd_breakpoints the random field degrades. Set vibration_spec "
            "for a pass/fail verdict."
        ),
        "timeout_s": 300,
    },
    "run_fea_modal": {
        "prompt_note": (
            "Fix the part like it is mounted — an unconstrained part "
            "returns rigid-body modes near 0 Hz. Density drives the "
            "frequencies, and it comes from material= (registry name or "
            "dict), not from set_material."
        ),
        "timeout_s": 300,
    },
    "run_fea_static": {
        "prompt_note": (
            "Workhorse stress check; query_geometry first to confirm "
            "fixture/load faces resolve. Material properties enter HERE and "
            "only here: material='aluminum' (registry name) or "
            "material={'E_mpa':70000,'nu':0.33,'density_g_cm3':2.7,"
            "'yield_mpa':250}; set_material sets appearance only. min_fos>1 "
            "holds; background=True for fine meshes."
        ),
        "timeout_s": 300,
    },
    "run_fea_thermal": {
        "prompt_note": (
            "Steady state is a ccx box-proxy estimate; pass "
            "transient={t_end_s,...} for the real-mesh C++ solve with "
            "flux/convection schedules."
        ),
        "timeout_s": 300,
    },
    "simulate_motion": {
        "prompt_note": (
            "Drive an angle/distance mate (revolute/concentric have no "
            "value to sweep). Per-step interference dominates runtime — "
            "False for a fast pass."
        ),
        "timeout_s": 300,
    },
    "struct_analyze": {
        "prompt_note": (
            "struct_check already runs this same analysis plus D/C ratios "
            "— use struct_analyze only when you want raw member forces, "
            "no pass/fail."
        ),
    },
    "struct_check": {
        "prompt_note": (
            "The go/no-go member check (AISC LRFD steel / NDS ASD wood); "
            "dc_ratio<=1 passes. Input is an aicad.structure/1 JSON file "
            "on disk."
        ),
    },
    "struct_estimate": {
        "prompt_note": (
            "Rate-table budget figures, not vendor prices (those are "
            "bom_price — don't mix). Same structure doc as struct_check."
        ),
    },
}
