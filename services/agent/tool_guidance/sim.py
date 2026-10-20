"""tool_guidance.sim — curated options for the sim tool family (FEA, CFD, motion, piping, hydraulics, structural)."""
from __future__ import annotations

from typing import Any

# OUTER harness ceiling for the run_fea_* family: the deadline
# agent_turn._dispatch_tool enforces with ThreadPoolExecutor.result(timeout=...)
# from the instant the tool is dispatched. It MUST stay strictly larger than the
# INNER CalculiX subprocess clock (services.mcp.fea_advanced.HARNESS_CCX_TIMEOUT_S,
# 300 s) by fea_advanced.CCX_SETUP_MARGIN_S (120 s): 420 = 300 + 120. The margin
# is the pre-solve work — face queries, autosave, STEP export, tet mesh build,
# deck generation — which burns outer-clock seconds BEFORE the inner clock even
# starts. Both were 300 s, i.e. equal, so the outer deadline always fired first
# and buried CalculiX's own actionable "ccx timed out ... try a coarser mesh"
# result under the harness's "abandoned in the background, may still be running"
# payload, which names no remedy. services/mcp/test_fea_ccx_advanced.py pins
# these values against the fea_advanced constants so they cannot drift apart.
_CCX_TIMEOUT_S = 300        # fea_advanced.HARNESS_CCX_TIMEOUT_S
_CCX_SETUP_MARGIN_S = 120   # fea_advanced.CCX_SETUP_MARGIN_S

#: run_fea_* tools that spend more than ONE inner CalculiX clock per dispatch.
#: run_fea_convergence solves the simulation runbook's three-mesh ladder under a
#: single dispatch; the setup (STEP export, fixture/load resolution) is resolved
#: ONCE and reused by every leg, so only the solver term multiplies. The runbook
#: nominal of 3 legs sizes the ceiling; a longer explicit ladder that overruns
#: degrades to background job_ids by itself rather than being lost.
FEA_SOLVER_LEGS: dict[str, int] = {"run_fea_convergence": 3}


def fea_timeout_s(legs: int = 1) -> int:
    """Outer ceiling for a run_fea_* entry that burns `legs` inner ccx clocks."""
    return legs * _CCX_TIMEOUT_S + _CCX_SETUP_MARGIN_S


_FEA_TIMEOUT_S = fea_timeout_s()

ENTRIES: dict[str, dict[str, Any]] = {
    "topology_capabilities": {
        "prompt_note": (
            "Read-only. Call FIRST to learn supported topology methods, physics, constraints, "
            "limits and print_processes before configuring a study; never assume an unlisted "
            "method or constraint. For printing choose a preset and a build_plate bed face."
        ),
    },
    "run_topology_optimization": {
        "prompt_note": (
            "Mutating (writes study, jobs, artifacts). Query topology_capabilities first, then "
            "run the same persisted UI study: run_topology_optimization(study=..., doc_id=..., "
            "background=True). Poll with get_job/wait_job; stop with sim_cancel. Report "
            "computation, convergence and feasibility separately — a finished run is not a "
            "converged or feasible one. Inspect the density fields, thresholded surface, "
            "actual-domain reanalysis and manufacturing checks; the source CAD stays editable. "
            "For printing set print_process and build_plate, read DFAM, then pass print_handoff "
            "slice_part arguments with the user's printer. Geometry/DFAM alone are not "
            "print-ready: slice_part must validate the G-code."
        ),
    },
    "sim_cancel": {
        "prompt_note": (
            "job_id from sim_run. Really terminates the solver process, not "
            "a flag flip; sim_status then reports cancelled. The last "
            "partial snapshot survives, so sim_results(partial=True) may "
            "still have something."
        ),
    },
    "sim_jobs": {
        "prompt_note": (
            "Lists recent background solves with their state — use it to "
            "recover a job_id from an earlier turn before assuming a solve "
            "was lost."
        ),
    },
    "sim_results": {
        "prompt_note": (
            "job_id from sim_run, after sim_status says done. partial=True "
            "returns whatever converged so far, clearly labelled — never "
            "quote a partial as a converged answer. partial=False on an "
            "unfinished job is refused rather than returning half a result."
        ),
    },
    "sim_run": {
        "prompt_note": (
            "Cancellable process job for analysis='fea_static' or 'fea_modal' ONLY. "
            "Static needs fixtures plus a non-zero loads list; modal takes the fixtures "
            "argument but fixtures=[] intentionally requests free-free modes and takes no loads. Poll "
            "sim_status, then sim_results; an unavailable/failed backend never becomes PASS."
        ),
    },
    "sim_status": {
        "prompt_note": (
            "job_id from sim_run. Reports "
            "queued/running/done/failed/cancelled and is reconciled against "
            "the OS — it never says running for a dead worker. Poll this "
            "before sim_results; call sim_jobs() if you lost the id."
        ),
    },
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
    "hydraulic_templates": {
        "prompt_note": (
            "Read-only catalog of native circuit templates; pass an entry's id as "
            "hydraulic_new(template=..., doc_id=...) to start from a complete circuit and "
            "suggested simulation setup instead of adding every component by hand."
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
    "hydraulic_simulate": {
        "prompt_note": (
            "Transient of the active circuit (or doc / doc_id's saved one): every port wired, "
            "required params set, valve_schedule [{t_s, component_id, position a|b|center|0..1}], "
            "loads per cylinder/motor. Cite summary (peak_pressure_bar, stroke_time_s, "
            "steady_speed_rpm, efficiency) and ratings.exceeded; series are cut to <=400 points "
            "— series_path holds full resolution. missing_params/unconnected_port name the fix; "
            "pass doc_id to save the run in the hydraulic_circuit study."
        ),
        "timeout_s": 300,
    },
    "hydraulic_size_motor": {
        "prompt_note": (
            "pressure_bar is the DIFFERENTIAL across the motor (supply minus back pressure). "
            "Put the returned standard displacement (result field standard_displacement_cc_rev) "
            "on the motor_hydraulic component and feed "
            "flow_lpm to hydraulic_size_pump; doc_id saves it under results.sizing.motors."
        ),
    },
    "hydraulic_load": {
        "prompt_note": (
            "Restore a hydraulic_circuit study (doc_id, optional circuit name) as the active "
            "circuit, with its saved sizing/bom/simulate and valve schedule; call it after a "
            "reopen instead of rebuilding the circuit from chat memory."
        ),
    },
    "hydraulic_layout_3d": {
        "prompt_note": (
            "Place circuit components in 3D (mm) for the GUI's Hydraulics > 3D Layout: positions "
            "{component_id: [x, y, z]} merge into the saved study's layout3d (doc_id persists it; "
            "results are kept). hydraulic_load returns the full layout including defaults."
        ),
    },
    "pump_drive_size": {
        "prompt_note": (
            "Electric motor for a pump: kind=hydraulic (flow_lpm + pressure_bar) or centrifugal "
            "(flow_m3h + head_m). Rounds shaft power x service_factor up the IEC kW or NEMA hp "
            "ladder; quote motor_rating, poles, frame and full_load_current_a. For a solved "
            "piping network use piping_pump_drive instead."
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
    "motion_fea_handoff_sweep": {
        "prompt_note": (
            "motion_fea_handoff at several mechanism positions in one call; "
            "returns a per-position stress/reaction table plus the worst "
            "position. Use background=True beyond a few positions."
        ),
        "timeout_s": 1800,
    },
    "sweep_envelope": {
        "prompt_note": (
            "Drive a mate through its range and return the union bbox the "
            "listed components sweep; fuse=True also builds the swept solid. "
            "No dimensional mate (steering uprights on concentric kingpin "
            "mates)? Pass steer={components,axis|mate_id,angle_deg} instead "
            "and it sweeps 0..lock. This is the folded/deployed envelope "
            "evidence, not a clash check."
        ),
        "timeout_s": 600,
    },
    "steering_turning_radius": {
        "prompt_note": (
            "Turning radius from the posed wheel axle lines (ft and m), with "
            "the closed-form Ackermann numbers beside it. An assembly whose "
            "uprights are posed by concentric/coincident kingpin mates has NO "
            "mate to drive: pass steer={components:[...], axis:{point,dir} or "
            "mate_id, angle_deg or angles_deg:[...]} (plus rear_steer for "
            "4WS) and it rotates them on a detached clone. Pass "
            "wheelbase_mm/track_mm when the axle heuristic refuses; "
            "check_interference=True names the first angle that clashes."
        ),
    },
    "run_fea_convergence": {
        "prompt_note": (
            "Same static study at 2+ explicit mesh sizes (sizes_mm or "
            "fineness ladder); the table with delta_pct_vs_prev is the "
            "convergence evidence a release needs. Never cite one mesh. "
            "Pass vtu_dir=/abs/dir to get one .vtu per leg: the reply's "
            "released_leg then names the finest solved leg's field, which "
            "sim_evidence_packet hashes — without it the ladder has no "
            "field and needs a fourth solve. min_size_mm=<mm> lowers the "
            "element-size band floor for EVERY leg when a leg is refused for "
            "needing an element smaller than that floor to resolve a feature; "
            "it costs elements and memory, so keep the ladder's sizes coarse."
        ),
        "timeout_s": fea_timeout_s(FEA_SOLVER_LEGS["run_fea_convergence"]),
    },
    "sim_evidence_packet": {
        "prompt_note": (
            "Write the case folder (inputs/result/provenance/convergence/"
            "checklist) for a finished study; unsatisfied_checklist names "
            "what still needs a hand calc or judgement. A re-run into the "
            "same folder ARCHIVES the previous packet (archived_to) instead "
            "of overwriting it; overwrite=True is the only way to replace."
        ),
    },
    "verify_packet": {
        "prompt_note": (
            "Read a packet folder back and check its files describe one run "
            "(same input hash, same engine build, result.json matching the "
            "hash convergence.md cites, reduction fields present). Run it "
            "before citing a packet; ok:false means do not cite it."
        ),
    },
    "polymesh_cli": {
        "prompt_note": (
            "Independent Polyhedral-FEA CLI lane (check|mesh|solve|diag) on "
            "an exported STEP with explicit fix/load boxes in metres; the "
            "runbook's cross-check for the critical case, not a replacement "
            "for run_fea_static."
        ),
        "timeout_s": 3600,
    },
    "piping_edit": {
        "prompt_note": (
            "Atomic draw/edit: add_run paths in mm auto-create elbows/tees; add_fitting "
            "inserts valves/orifices/custom K. Pass doc_id to persist, then re-solve."
        ),
    },
    "piping_fitting_k": {
        "prompt_note": (
            "Deterministic K preview and Reynolds curve. Quote the returned method; "
            "custom fittings need a k_model, not an invented default."
        ),
    },
    "piping_fitting_k_cfd": {
        "prompt_note": (
            "Shared GUI/agent CFD K pipeline: quick=2 Re points at 12 cells across. "
            "Use background=True; poll this tool with job_id, cancel=True to stop. "
            "apply=True + fitting_id writes params.k_model; doc_id saves the network. "
            "Only elbows generate bores; other fittings require a closed fluid-volume geometry."
        ),
        "timeout_s": 60,
    },
    "piping_cfd_handoff": {
        "prompt_note": (
            "Solve -> bore STEP -> LBM CFD in one call; use background=True "
            "on big grids. RANS accuracy needs a manual run_cfd_accurate + "
            "piping_verify_cfd."
        ),
        "timeout_s": 600,
    },
    "piping_bom": {
        "prompt_note": (
            "Line list, pipe/fitting/flange quantities with masses, and shop spools "
            "broken at flanges and max_spool_length_mm; pass the routed piping (or "
            "a session network name) and doc_id to merge the BOM into the doc's "
            "piping_network study."
        ),
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
    "piping_load": {
        "prompt_note": (
            "Restore a piping_network study persisted in a doc (doc_id, optional "
            "network name) into the session before re-solving or re-checking; a "
            "doc with several networks refuses and lists their names."
        ),
    },
    "piping_pump_drive": {
        "prompt_note": (
            "Reads the pump run's flow_m3h + pump_head_m from the network's last piping_solve "
            "(solve with pumps=[...] first; run_id when several pumps) and sizes its electric "
            "motor via pump_drive_size; doc_id saves it in the piping_network study."
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
    "piping_route": {
        "prompt_note": (
            "Orthogonal obstacle-avoiding route (mm) between start/end points; dir is "
            "the travel direction (start: nozzle outward normal, end: MINUS the end "
            "nozzle's outward normal); doc_id injects every solid body as an obstacle "
            "(ignore_bodies exempts the equipment you connect to). Elbows are added "
            "at each bend; no_route means widen the gap, lower clearance_mm or "
            "change grid_mm, never hand-draw through solids."
        ),
    },
    "piping_spec_check": {
        "prompt_note": (
            "ASME B31.3 eq. 3a wall check per run plus B16.5 flange rating and "
            "support span; design_pressure_kpa is gauge, design_temp_c in °C. Cite "
            "t_required_mm/mawp_kpa/utilization per run; all_pass:false is a "
            "finding to fix (schedule, material, class), not a warning."
        ),
    },
    "run_cfd_accurate": {
        "prompt_note": (
            "OpenFOAM external-aero RANS; the tool capability-checks first and returns "
            "ok:false, skipped:true when unavailable. Always returns job_id when started; "
            "poll get_job. Inputs are velocity m/s, aoa degrees, fluid air|water. Start "
            "preset='coarse', then refine; only quote Cd/Cl when converged is true."
        ),
    },
    "run_pipe_flow": {
        "prompt_note": (
            "Native D3Q19 internal flow through a fluid-volume solid (pipe bore, "
            "e.g. piping + run_id derives it); inlet/outlet normals point INTO the "
            "domain. Quote dp_pa only with converged:true and read caveats when "
            "physical Re is under-resolved; compare against darcy_estimate_pa and "
            "re-run at a higher cells_across (default 20) before trusting a fitting dP. "
            "background=True returns job_id for get_job."
        ),
        "timeout_s": 300,
    },
    "run_cfd_flow": {
        "prompt_note": (
            "Built-in directional LBM: velocity is m/s, aoa degrees, fluid is air|water; "
            "freestream +X and lift +Z. Check V² force scaling/dynamic pressure, but absolute "
            "Cd is uncalibrated. Require converged=true and min_feature_voxels>=~8, then "
            "refine cells; use run_cfd_accurate for absolute coefficients. background=True for cells>=120."
        ),
        "timeout_s": 300,
    },
    "run_fea_adaptive": {
        "prompt_note": (
            "Adaptive remesh-and-resolve for a decision-driving peak stress; use "
            "run_fea_static for an initial screen, then this when the peak is "
            "mesh-sensitive. Its relative indicator is not a stress-error bound; "
            "background=True returns job_id for get_job."
        ),
        "timeout_s": _FEA_TIMEOUT_S,
    },
    "run_fea_buckling": {
        "prompt_note": (
            "Engine linear buckling on the real CAD tet10 mesh, size_mm=8 by default. "
            "Use the static fixtures/load/material dialect: face forces in N or pressures "
            "in MPa, including multiple loads. Read modes[].load_factor/participation, "
            "critical_load_factor, critical_load_n, mesh_quality and provenance.solver='engine'. "
            "A factor below 1 predicts buckling below the reference load. max_load_factor "
            "(default10000) is the search ceiling; no mode below it is refused, not a PASS "
            "or infinite strength. Refine size_mm for convergence; vtu_path exports mode "
            "shapes. Linear elastic instability is not nonlinear collapse strength."
        ),
        "timeout_s": _FEA_TIMEOUT_S,
    },
    "run_fea_contact": {
        "prompt_note": (
            "REAL frictionless unilateral contact on the document's own meshed bodies: "
            "surface pairs facing across <=max_gap_mm carry compression only, solved by an "
            "active-set loop. Same fixtures/load dialect as run_fea_static, plus max_gap_mm "
            "just above the measured clearance. Read contact.active_pairs (0 = nothing "
            "touched), contact.converged, contact.pairs[].contact_force_n / "
            "max_contact_pressure_mpa, and reactions.equilibrium_error_pct. No friction, no "
            "large sliding, no preload — apply a clamp load as a face load instead. A load "
            "that pulls the parts apart is refused as a mechanism. Model press fits as "
            "bonded and running clearances as contact in the same study with "
            'interfaces={"bonded":[[1,2]],"contact":[[2,3]],"default":"free"} '
            "(one-based STEP solid ordinals or body names; explicit pairs override the "
            "default, which is contact on contact runs and free on ordinary static runs). "
            "bond_gap_mm can constrain the bonded search when interfaces is supplied; "
            "otherwise bonded pairs use their measured gap within max_gap_mm. "
            "For active-set chatter, tune the positive finite penalty_scale (default 1.0) "
            "and max_iterations (default 25), then check convergence and penetration. "
            "Read contact.interfaces.bonded/contact pair counts and area_mm2, unmatched "
            "declared pairs, and the echoed contact.penalty_scale/max_iterations. "
            'For mixed bodies pass material={"per_body":{"1":"steel","2":'
            '{"E_mpa":110000,"nu":0.34,"yield_mpa":125}},"default":"steel"} '
            "(one-based STEP solid ordinals); each body's own yield governs its FoS. "
            "When mean_contact_pressure_mpa comes back well above yield the ELASTIC "
            "answer is the artefact, not the part: re-run with "
            'plasticity={"hardening_mpa": 1200} so the seat yields and the patch '
            "spreads (the active-set loop runs inside each load increment), then quote "
            "the plastic contact_area_mm2 / mean pressure and the plasticity block. "
            "min_size_mm=<mm> lowers the element-size band floor when the run is "
            "refused for needing an element smaller than that floor to resolve a "
            "feature away from the interface; it costs elements and memory, so pair it "
            "with a coarse mesh_size_mm."
        ),
        "timeout_s": _FEA_TIMEOUT_S,
    },
    "run_fea_fatigue": {
        "prompt_note": (
            "Runs real static FEA then S-N postprocessing. Needs the same mounted faces and "
            "non-zero N/MPa loads as run_fea_static plus material ultimate_mpa and "
            "endurance_mpa. State load_type/load_ratio, surface, reliability and theory; "
            "regime='infinite' means >1e6-cycle runout, not immortal. Refine the static mesh."
        ),
        "timeout_s": _FEA_TIMEOUT_S,
    },
    "run_fea_frequency_response": {
        "prompt_note": (
            "Base-excited modal superposition. fixtures must represent the mount; "
            "excitation_dir is a unit direction, base_accel_g is g, damping zeta is assumed, "
            "and material density drives Hz. Response peaks should align with returned modal "
            "Hz. PSD breakpoints are g²/Hz; no PSD means random.ok=false and no "
            "vibration_spec means verdict=null. Output is acceleration only, not stress/deflection."
        ),
        "timeout_s": _FEA_TIMEOUT_S,
    },
    "run_fea_modal": {
        "prompt_note": (
            "Material density drives Hz. fixtures=[] is a legitimate free-free solve: expect "
            "six near-zero rigid-body modes before elastic modes; otherwise fix it exactly as "
            "mounted. Compare the first elastic mode to a justified beam/order anchor and across "
            "mesh_fineness presets; properties come from material=, never set_material."
        ),
        "timeout_s": _FEA_TIMEOUT_S,
    },
    "run_fea_static": {
        "prompt_note": (
            "Real-part linear static FEA. Confirm face queries first. fixtures must react a "
            "non-zero load: force vectors/magnitude are N, pressure is MPa; zero loads are "
            "refused and there is no gravity/self-weight term. An explicit material dict needs "
            "E_mpa, nu, and yield_mpa; named registry materials resolve them. "
            'For mixed bodies use material={"per_body":{"1":"steel","2":'
            '{"E_mpa":110000,"nu":0.34,"yield_mpa":125}},"default":"steel"} '
            "(one-based STEP solid ordinals, default optional if every body is assigned). "
            "set_material is appearance only. Repeat at a finer mesh and compare governing stress/deflection "
            "(<10% change) before PASS. mesh_size_mm=<mm> is the exact element size "
            "(mutually exclusive with mesh_fineness) — use it with vtu_path to re-solve "
            "one run_fea_convergence leg on its own mesh. min_size_mm=<mm> lowers the "
            "element-size band floor when a run is refused for needing an element "
            "smaller than that floor to resolve a feature; it costs elements and "
            "memory, so pair it with a coarse mesh_size_mm. "
            'plasticity={"hardening_mpa": 1200} switches the material to small-strain '
            "J2 (yield_mpa defaults to the material's own; hardening_mpa is a TANGENT "
            "MODULUS in MPa, 0 = perfectly plastic): use it when an elastic peak sits "
            "well above yield in a small region, because a linear-elastic solve cannot "
            "redistribute and reports a stress the material could never carry. Past "
            "the part's limit load it REFUSES as a collapse mechanism naming the load "
            "fraction carried; over 5 % plastic strain it warns (small strain, no mesh "
            "update). Not accepted by modal/buckling/thermal/fatigue or adaptive runs."
        ),
        "timeout_s": _FEA_TIMEOUT_S,
    },
    "run_fea_thermal": {
        "prompt_note": (
            "Steady and transient use the real part tet mesh in the engine. "
            "hot_faces/cold_faces entries need {face,temperature °C}; loads support "
            "flux_w_m2 or h_w_m2k/t_amb_c. Steady requires a fixed-temperature or "
            "convection datum and non-zero thermal excitation, returns temperature extrema, "
            "flux_max_w_m2, mesh_quality/provenance, and supports vtu_path. "
            "Steady needs conductivity_w_mk/k_w_mk in W/(m·K). "
            "transient={t_end_s,dt_s,...} additionally supports schedules; "
            "flux-only heat-up is valid and needs conductivity, "
            "density_g_cm3, and cp_j_kgk/specific_heat_j_kgk in J/(kg·K)."
        ),
        "timeout_s": _FEA_TIMEOUT_S,
    },
    "simulate_motion": {
        "prompt_note": (
            "Kinematic sweep of one dimensional mate (mate_id/start/stop) or several at once "
            "(drivers=[{mate_id,start,stop}]). Angle ranges are degrees, distance ranges mm, "
            "steps>=2. fixtures/loads do not apply. check_interference samples each step, so "
            "refine steps near first contact. dynamics={} adds inverse-dynamics reactions "
            "but not contact dynamics."
        ),
        # A 36-body arm sweep with interference checks outran 300 s (live 2026-09-25) and
        # was abandoned while still posing the assembly; render_animation already gets 900.
        "timeout_s": 900,
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
    "vessel_size": {
        "prompt_note": (
            "ASME VIII-1 UG-27 shell + UG-32 head thickness from inner_diameter_mm and "
            "tan_tan_length_mm or volume_m3; design_pressure_kpa gauge, temps °C, "
            "corrosion allowance included. Quote t_nominal_mm, mawp_kpa and "
            "hydrotest_kpa from the result; doc_id+name persists a vessel study. "
            "Model the vessel afterwards with the returned thicknesses."
        ),
    },
    "duct_size": {
        "prompt_note": (
            "Equal-friction duct sizing over a parent-linked segment tree (airflow_m3s, "
            "length_m, k_fittings); returns standard round or rect sizes, velocity, "
            "pressure drops, the critical path and fan_static_pa. A velocity warning "
            "means lower friction_pa_per_m (bigger duct) or split the branch, not "
            "ignore it."
        ),
    },
}
