"""cad_guide topic texts + the cad_guide_text(topic) dispatcher (T-RF8 split of cad_tools.py).
Consumed by the cad_guide tool (cad_doc_tools), tools/manual_gen.py, and re-exported
through the cad_tools facade."""

from pathlib import Path
from .cad_common import cb  # noqa: F401
import os


# --------------------------------------------------------------------- the guide text

_GUIDE_PROMPTS = ["overview", "review", "simulation", "motion"]


def _prompts_dir() -> Path:
    d = os.environ.get("CHUDWARE_PROMPTS_DIR")
    if d and Path(d).is_dir():
        return Path(d)
    # Fall backs: the staged dist prompts, then the repo source prompts.
    staged = cb.dist_dir() / "app" / "agent" / "prompts"
    if staged.is_dir():
        return staged
    return cb._repo_root() / "agent" / "prompts"


def _read_prompt(rel: str) -> str:
    p = _prompts_dir() / rel
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


_SIM_GUIDE = """\
SIMULATION GUIDE — FEA + CFD on the live part.

Faces for fixtures and loads are named with the SAME plain-English geometry queries you
build with: a bare side word ("bottom", "+z") or a full query ({"of":"face","side":"bottom",
"pick":"largest"}). Use query_geometry(...) first if unsure what faces a part has.

run_fea_static(fixtures, loads, material) — linear static stress.
  fixtures : list of face queries that are held fixed (encastre). e.g. ["bottom"].
  loads    : list of dicts, each a face query plus a force or pressure:
               {"face": "top", "force": [0, 0, -500]}      # newtons, world XYZ
               {"face": "top", "pressure": 0.5}            # MPa, inward on the face
  material : "steel" | "aluminum" | "titanium" | "stainless" | "brass" | "abs" | "pla"
             (or an explicit dict with E_mpa / nu / density_g_cm3 / yield_mpa).
  Returns max von-Mises stress (MPa), max displacement (mm), and the min factor of safety
  (yield / max stress). FoS > 1 means it holds; design to a target margin.

run_fea_modal(fixtures, material, n_modes) — natural frequencies (Hz) of the held part.

run_fea_fatigue(fixtures, loads, material, load_type, theory) — high-cycle FATIGUE life.
  Reuses the static stress field, then applies the mean-stress theory (goodman/gerber/
  soderberg/asme/morrow) + Marin endurance correction + Shigley S-N line to report cycles-
  to-failure and a factor of safety. load_type "reversed"|"zero_based"|"ratio" sets the
  cyclic spectrum. regime="infinite" ⇒ runout (>1e6 cycles). Same FEA backend as run_fea_static.

run_fea_thermal(hot_faces, cold_faces, material) — steady-state THERMAL (heat conduction) via
  CalculiX (ccx). hot_faces/cold_faces each carry a {face + temperature °C}. Solves a prismatic
  box proxy sized to the part bbox (through-conduction estimate). ccx is a GPL-2.0 subprocess
  (PATH-discovered); if absent it returns {skipped:true} — never a fabricated result.

run_fea_thermal(..., transient={...}, loads=[...]) — TRANSIENT thermal on the REAL part mesh
  (C++ core, backward Euler; no ccx, no box proxy). transient={"t_end_s":60, "dt_s":0.5,
  "t_init_c":20, "frame_times_s":[10,30,60]}. hot/cold faces are Dirichlet held for the run
  (may be empty — flux-only heat-up is fine). loads carry time-varying heat sources:
    {"face":"top", "flux_w_m2":5000, "schedule":{"duty":0.25, "period_s":1.0}}   duty cycle —
       the direct shape for an aicad.powerprofile/1 per-rail duty profile
    {"face":"top", "flux_w_m2":5000, "schedule":{"points":[[0,1],[2,0.2]], "period_s":4}}
       piecewise sample-and-hold; each load keeps its OWN independent schedule
    {"face":"left", "h_w_m2k":25, "t_amb_c":20}                                 convection film
  Result: summary.max_temp_c = the PEAK over the whole run (max-temp envelope) at
  summary.max_temp_time_s, plus per-frame max temps at the requested frame times. Use this to
  check duty-cycled electronics/heater loads that never reach the 100%-on steady state.

run_fea_buckling(fixtures, load, material, n_modes) — linear (eigenvalue) BUCKLING via CalculiX.
  Returns the critical load factors λ (applied load × λ = Pcr); λ<1 buckles below the load.
  Box-proxy Euler-column estimate for slender members. ccx subprocess; absent ⇒ {skipped:true}.

run_fea_contact(pressure_mpa, material) — nonlinear large-deflection CONTACT FEA via CalculiX
  (*STATIC,NLGEOM + *CONTACT PAIR). Two-block proxy demonstrating load transfer across a touching
  interface (press/snap-fits). ccx subprocess; absent ⇒ {skipped:true}.

run_fea_frequency_response(fixtures, excitation_dir, sweep_hz, zeta, base_accel_g,
  psd_breakpoints, vibration_spec, ...) — harmonic + random vibration response (T-BL17).
  Answers "does this part resonate and fail under the vibe spec", not just "what are its
  natural frequencies". Builds on run_fea_modal's modes via modal superposition:
    Harmonic: SDOF-per-mode transmissibility sweep → response acceleration (g) vs frequency
              (peak_accel_g, peak_freq_hz). base_accel_g sets the input amplitude.
    Random:   Miles' equation per mode (Steinberg Eq. 6.6) → per-mode + total GRMS.
              REQUIRES psd_breakpoints=[[f_hz, g2_per_hz], ...] — with no PSD given the
              random field degrades to {ok:false, reason:"no_psd_provided"} (it NEVER
              assumes a default vibration profile).
  vibration_spec {"max_peak_accel_g": g, "max_grms": g} → pass/fail verdict (DAG-gateable).
  zeta damping default 0.02 (2%) — an ASSUMPTION, always echoed in the result.
  Response is acceleration amplification only: mode shapes are unit-normalized, so physical
  displacement/stress amplitudes are NOT recovered (see result.limitations).

run_cfd_flow(velocity, aoa, fluid) — LBM wind tunnel over the whole part/assembly.
  velocity m/s, aoa degrees, fluid "air"|"water". Returns drag (N), lift (N), Cd, frontal
  area, and Reynolds number. Coarse grid by default — a directional estimate, raise `cells`
  for more resolution.
  CONVENTIONS (Z-up): freestream +X, lift +Z, span Y; +aoa pitches nose up. `reynolds`/`cd`
  use the part's LONGEST span as reference length (reported as `ref_length_mm`).
  TRUST THE DIAGNOSTICS: the result reports `voxel_mm` / `min_feature_voxels` (thin features
  under ~8 voxels are unresolved → lift reads ~0, a `warning` is set — raise `cells` or run the
  thin part alone) and `converged` / `residual` (a non-converged run is under-iterated, not
  physically zero). For heavy runs pass background=True and poll get_job(job_id).

run_cfd_accurate(velocity, aoa, fluid, preset) — HIGH-FIDELITY OpenFOAM RANS (k-omega SST + prism
  layers): STL export → snappyHexMesh body-fit → simpleFoam steady RANS, all via OpenFOAM
  subprocesses (GPL isolation — never linked). The TRUSTWORTHY absolute Cd/Cl path (validated:
  sphere/cube land in the documented ±10-15%-class experiment bands, see tools/foam_validation.py)
  vs run_cfd_flow's coarse LBM estimate. Requires OpenFOAM — detected automatically on Linux
  (sourced env / system / user-space conda env), native Windows blueCFD, or WSL; if absent the
  tool returns an honest {ok:false} with the provisioning command
  (`python -m tools.provision_openfoam --install` — user-space on Linux, no sudo). Runs for
  MINUTES, always background → poll get_job(job_id) (live meshing/solve progress, like the LBM
  path). preset coarse|medium|fine|xfine. Use run_cfd_flow first to scan trends fast, then
  run_cfd_accurate to certify the chosen design.
  Conjugate-heat-transfer variant (buoyant chtMultiRegionFoam) lives at the engine seam:
  engine.sim.openfoam.run_openfoam_tunnel(thermal={enabled, mode:"conjugate", inlet_temp_c,
  solids:[{name, stl_path, kappa_w_mk, power_w}, ...]}) — multi-solid regions with per-solid
  volumetric heat sources (board + components handoff).

Heavy solves: run_cfd_flow / run_fea_* accept background=True (run_cfd_accurate is always
background) → return a job_id immediately; poll get_job(job_id) for the result. This keeps a long
solve from tripping the transport timeout (which would otherwise drop the session). After a
restart, recover_parts() reloads auto-saved docs.

CROSS-SIM HANDOFF (aicad.simhandoff/1 — contracts/sim_handoff.md):
  When one physics domain feeds another (1D piping → 3D CFD, hydraulic → piping /
  hydraulic → FEA, motion → FEA and ECAD → thermal FEA today; thermal → CFD
  conjugate as it lands),
  the coupling follows one seam: a HANDOFF tool solves the source domain, derives geometry
  + boundary conditions with explicit units and provenance, and runs (or stages) the target
  study; a VERIFY tool then cross-checks a shared quantity between the two solvers at a
  tolerance and returns a PASS/FAIL verdict.
  Conforming tools embed a validated envelope under the reserved "simhandoff" key of their
  result: kind:"handoff" carries {source/target study refs, geometry[], conditions[] (each
  {name, kind, value, units, applied_to, provenance}), expectations[]}; kind:"verify"
  carries {checks[] ({quantity, units, source_value, target_value, deviation_pct,
  tolerance_pct, pass}), pass, verdict, advisories}. Study refs {domain, tool, doc_id?,
  study?, handoff_id?} let any sim result name exactly what produced it.
  Proven pair: piping_cfd_handoff (one-call 1D solve → bore STEP → run_cfd_flow with
  1D-derived BCs) then piping_verify_cfd (1D vs 3D pressure-drop verdict + velocity-limit
  advisories). Hydraulic pairs: hydraulic_piping_handoff/hydraulic_verify_piping (hose
  runs → piping 1D solve, real line losses back into the pump margin; doc-to-doc — the
  generated piping doc rides INLINE in the envelope, consumable via
  piping_solve(source_handoff=...)) and hydraulic_fea_handoff/hydraulic_verify_fea
  (cylinder mount loads at max pressure → case-labelled static-FEA load cases).
  motion → FEA (LIVE): motion_fea_handoff(component, fixtures, stop=…) runs the motion
  study WITH rigid-body dynamics (simulate_motion dynamics={} — mass/inertia from
  geometry x density, gravity/loads/damping, joint reactions via the mate constraint
  jacobian), picks the worst-case frame (or `at`=time s), applies the joint reaction
  forces on the selected component's mate faces and runs run_fea_static on it — "is this
  link strong enough at the worst instant" in one call. Conditions are `case`-labelled
  per frame; dry_run=True builds loads + envelope without solving.
  ECAD → thermal FEA (LIVE, T-FIN16): ecad_thermal_handoff(environment='still_air'|
  'forced_air'|'cold_plate'|custom h/t_amb, include_trace_heating=…) takes the ACTIVE
  routed board to a solved board-level thermal study in one call — fused board+component
  FEA solid with the FR4/copper stackup HOMOGENIZED to one effective material (in-plane
  rule-of-mixtures k, every assumption listed in the result), one heat_source condition
  PER component from its boardphys power metadata (components without power_w get NO
  source and are named in coverage.unpowered_refs), optional PDN I2R trace heating from
  ecad_run_pdn as distributed flux, environment-preset convection, quasi-steady core
  transient solve, then per-component case temp + Tj (case + P*theta_jc) map-back.
  ecad_verify_thermal() gives the per-component Tj-vs-limit margin table and a theta_ja
  hand-calc cross-check at a GENEROUS tolerance (JEDEC theta_ja assumes a standard test
  board; minor power contributors get advisories, not pass/fail — neighbor heating is
  invisible to theta_ja). dry_run=True builds geometry + BCs + envelope without solving.
  ECAD → CHT airflow (LIVE, T-FIN19, coupling ecad_to_cht): when the convection PRESETS
  aren't enough (enclosures, fans), ecad_airflow_handoff(inlet_mps=…, inlet_temp_c=…,
  enclosure={"clearance_mm":10,"wall_mm":2}?) exports the board slab + every POWERED
  component as separate watertight STL solids and runs OpenFOAM chtMultiRegionFoam via
  the T-SIM5 multi-solid seam (per-region volumetric heat sources; optional passive
  4-wall enclosure duct open along the flow axis), mapping per-region mean/peak °C back
  to component refs. ecad_verify_airflow() then answers "was the film-coefficient
  assumption sane?" — a per-component preset-h vs CHT rise deviation table with
  guidance + an effective-h re-run suggestion whenever the preset was OPTIMISTIC.
  Without OpenFOAM the handoff degrades honestly (skipped:true + full envelope) and
  offers the LBM wind tunnel as a qualitative-flow ADVISORY, clearly labeled
  NON-THERMAL. Budget: even the 'coarse' preset is minutes of meshing + solving.
  ECAD → structural FEA (LIVE, T-FIN21): ecad_vibe_check(band_lo_hz=20, band_hi_hz=2000,
  psd_breakpoints=…, mount_holes=…) — vibration reliability screen of the mounted board:
  reuses the T-FIN16 fused board+components solid with a mass-corrected effective
  density (component masses from package dims x a mold-compound density assumption, or
  eng mass metadata), detects mounting holes (mounting-hole footprints / NPTH pads /
  large un-netted plated holes, or explicit mount_holes=[[x,y],…]) and fixes them, runs
  the core modal solve, FLAGS every natural frequency inside the excitation band
  (default 20-2000 Hz random-vibe screening), then estimates the 3-sigma board
  deflection at the worst in-band mode (Miles' response, Z=9.8*G/f^2) and screens each
  component against Steinberg's 0.00022*B/(C*h*r*sqrt(L)) deflection limit — a CITED
  HEURISTIC for solder-joint fatigue risk, not a life prediction. Default PSD is a
  flat 0.04 g^2/Hz screening level (echoed as an assumption); pass the qualification
  spectrum for a spec-true screen. dry_run=True builds mounts + mass model + envelope
  without solving.
  HEATSINK SELECTION (T-FIN17): heatsink_select(component_ref, tj_target_c |
  t_case_target_c) closes the "pick a heatsink" loop on the LAST ecad_thermal_handoff
  study. Candidates = curated parts-library heatsinks (part_search domain "thermal",
  datasheet-class theta_sa + mass + cost) PLUS a parametric plain-finned aluminum
  generator sized to the component footprint (fin-efficiency theta_sa estimate;
  buildable via services.mcp.heatsink.sink_ops engine ops). Each candidate is
  "mounted" as bonded contact through a TIM bondline (default 0.1 mm @ 3.0 W/m-K,
  override tim_t_mm/tim_k_w_mk) and RE-SOLVED sequentially: because the core solver
  is single-material, the sink becomes an effective convection
  h_eff = 1/((R_tim + R_sa)*A_top) over the component top — board geometry is
  unchanged, so the baseline board MESH IS REUSED per candidate and board spreading +
  neighbor heating stay in the solve (sink-base overhang spreading is not modeled —
  conservative). Returns a ranked table {candidate, achieved_tj_c,
  delta_vs_target_c, mass_g, cost_usd}; winner = lightest candidate meeting the
  target, its study kept, result carries a verify envelope (coupling
  ecad_heatsink_to_thermal_fea). IMPOSSIBLE targets are honest: target_met=false with
  the best achievable candidate named; without a sim-enabled libchudware it degrades to
  a flagged network estimate (Tj = Ta + P*(theta_jc + R_tim + R_sa)).
  Helper builders/validator: services/mcp/simhandoff.py.

SYSTEM POWER BUDGET (aicad.powerbudget/1 — T-FIN23, tool: power_budget):
  Bookkeeping (NOT SPICE) rail-tree energy budget composing three seams: an
  aicad.boardphys/1 sidecar (ecad_to_3d — per-component supply-rail currents, sequencing,
  optional PDN block from ecad_run_pdn) supplies the LOADS; an aicad.powerprofile/1
  artifact (fw_sim_run / fw_power_profile — firmware emulation duty cycle) supplies the
  WEIGHTING; you supply the TREE: source {battery|supply, voltage_v, capacity_mah} →
  converters [{input, output, topology ldo|buck|boost, efficiency: fixed eta or
  [[load_frac, eta]] curve points, i_out_max_a}]. Verdict: battery life for the scenario,
  per-rail avg/peak vs converter rating with margins + worst-rail warning, PDN
  cross-check (copper drop / hotspots / stale-analysis), sequencing sanity note.
  No firmware profile → DEGRADED mode: datasheet-typ currents assumed always active,
  explicitly labelled; unknown currents stay null (sums become labelled floors), every
  number carries provenance. Also runs inside verify_battery as the 'power_budget'
  check (kwargs power_source / power_converters / board_path / power_profile_path).

GOLDEN DEMO (T-FIN25 — living E2E worked example):
  scripts/demo_sensor_node.py chains the full FIN surface on an STM32F4 sensor-node
  board: verification ladder (ERC/DRC/hostcheck/sicheck/FPGA/PDN) → firmware project +
  co-verify (fw_sim_run with fixture:i2c_temp_tmp103 when Renode is present) →
  ecad_thermal_handoff → heatsink_select(U1, tj_target_c=85) → power_budget →
  make_report. Wired into scripts/gate.sh as stage 4.7 (CHUDWARE_SKIP_FIN25=1 to opt out).
  Narrative + artifacts: docs/FIN25_SENSOR_NODE_GOLDEN_DEMO.md. Prefer this script over
  inventing a new end-to-end recipe when demonstrating the content is finished.

SIMULATE TAB (T-FIN24, host GUI): one multiphysics Simulate workspace hosts every live
  study kind with progressive disclosure by open doc type, plus handoff actions
  (Send board to thermal FEA, Pick heatsink, …) driven by aicad.simhandoff/1. Worker
  seam: engine_oracle/sim/multiphysics_rpc.py (sim_caps / sim_run_buckling /
  sim_run_contact / sim_freq_response / ecad_thermal_handoff / heatsink_select /
  piping_verify_cfd). EM forms wire the live EM backend; deeper EM solvers stay on
  T-SIM11. Supersedes the deferred T-SIM10 host-path brief.
"""

_MOTION_GUIDE = """\
MOTION / KINEMATICS GUIDE — make an assembly MOVE, and check it clears as it goes.

simulate_motion(mate_id, start, stop, steps, ...) is the assembly-motion study: it DRIVES one
mate through a range and re-solves the whole mate graph at each sample, so the rest of the
mechanism follows — SolidWorks-Motion / Onshape-Assembly-Motion for the common KINEMATIC case.

WHAT IT ANSWERS: "does this linkage / crank / cam / gear-train move the way I meant, and does
anything CLASH as it sweeps?" It returns a trajectory (every body's world 4x4 pose per step) plus
a per-step interference sweep that flags the FIRST colliding step.

SCOPE: kinematics by default — position (and, by sampling, velocity). Pass dynamics={...} (even
{}) to ALSO run RIGID-BODY INVERSE DYNAMICS on the same sweep: mass/inertia from each component's
geometry x density (densities/masses overridable per component), gravity, applied forces/torques,
simple viscous damping, and per-mate JOINT REACTION forces/moments solved through the mate
constraint jacobian (per-frame min-norm least squares; V&V'd against analytic pendulum +
slider-crank reactions). The result gains a `dynamics` block with per-frame reactions,
per_joint_worst and worst_case. Contact dynamics is still out of scope.

MOTION → FEA: motion_fea_handoff(component, fixtures, stop=…, at="worst"|t_s) chains it —
worst-instant joint reactions become force loads on that component's mate faces and
run_fea_static answers "is this link strong enough at the worst instant" in one call,
conforming to aicad.simhandoff/1 (see cad_guide('simulation'), CROSS-SIM HANDOFF).

HOW TO USE
  1. Open/build an ASSEMBLY with mates (asm_insert_component + asm_add_mate; solve_mates to seat
     it). You DRIVE a DIMENSIONAL mate — one whose `value` means motion: angle / angle_limit
     (deg), distance / distance_limit (mm), gear / rack_pinion / screw. A revolute/concentric/
     slot mate has no value to sweep; drive the angle/distance mate that DIMENSIONS that joint.
  2. simulate_motion(mate_id="m3", start=0, stop=360, steps=48, loop=True) — sweep the driver
     0->360 and close the cycle. Omit mate_id if there's exactly one drivable mate; omit start to
     begin at the mate's current value.
  3. Read the result: trajectory[i].bodies[j].transform is body j's world pose at sample i (the
     datum a viewer animates); interference.first_collision_step / _value is the first clash (with
     the interfering pair in `detail`). The study RESTORES the driven mate to its original value
     when it finishes — it reads motion, it doesn't leave the assembly moved.

TIPS
  * More `steps` = smoother playback and safer against a re-solve flipping a tight mechanism to a
    mirror pose (positions carry forward step-to-step, so a fine sweep stays on one branch).
  * A single angle/distance mate under-constrains a free body (it removes one DOF of six); pin the
    unwanted DOF with extra mates (e.g. a `parallel` to keep a planar linkage in-plane) so the
    driven pose is unique.
  * Swept interference reuses check_interference per step — it does not reimplement collision;
    tune it with min_clearance_mm just like a static check.
"""

_ASSEMBLY_GUIDE = """\
ASSEMBLY GUIDE — put SEPARATE PARTS together so they actually fit (mates + solve + checks).

TWO WAYS TO ASSEMBLE
  A) TRUE ASSEMBLY (mates — SolidWorks/Onshape model; use for anything that must fit or move):
     components are SAVED PART FILES placed by a mate solver. Gives per-component identity
     (BOM), mates, kinematics (simulate_motion), exploded views.
  B) MULTI-BODY PART (quick static stack-up): import_part(into=doc) / make_standard_part then
     `transform` features with explicit coordinates. No mates, no motion — fine for a weldment
     or a one-off clearance check, wrong for a real mechanism.

TRUE-ASSEMBLY WORKFLOW
  1. Build/obtain each part: new_part + add_features, make_standard_part (fasteners/bearings),
     or import_part (a downloaded STEP). save_part(path) each one to .step+.
  2. new_assembly("motor_mount")                    create the assembly doc (it becomes current).
  3. asm_add_component(path, fixed=True)            the grounded base (first one defaults fixed).
     asm_add_component(path2, at=[x,y,z])           free components; `at` is a starting pose
                                                    (helps the solver pick the right side).
  4. asm_mate(mate_type, a, b, ...)                 constrain pairs of faces/edges:
       a/b = {"component": "k1", "query": {"of":"face","side":"top"}}
       The query is the SAME plain-word geometry query as query_geometry, resolved against
       that component's SOURCE PART, and must match exactly ONE face/edge (narrow with
       pick/near/count; the error lists candidates when it matches several).
       mate types: coincident | parallel | perpendicular | tangent | concentric |
                   distance | angle | lock   (distance mm / angle deg take value=)
       align="aligned"|"anti" flips which way normals/axes point.
     SEATING PATTERNS (what a person clicks in SolidWorks):
       bolt in hole      concentric (shank ↔ bore) + coincident (head seat ↔ plate face)
       motor on bracket  concentric (pilot boss ↔ pilot bore) + coincident (flange ↔ plate)
                         + parallel or a second concentric (bolt ↔ hole) to stop spin
       pin joint         concentric (pin ↔ bore) + coincident/distance for axial position
  5. asm_mate solves after each add (solve=True default) — or call solve_mates() explicitly.
  6. VALIDATE: describe_assembly() (world bboxes + solve status), check_interference()
     (clashes + tightest clearances; min_clearance_mm flags too-tight fits), interface_check
     (do hole patterns/axes actually line up), measure_between (spot dimensions).
  7. save_part(path) the assembly .step+; simulate_motion drives a dimensional mate for
     kinematics + swept-clash.

BEST PRACTICE (structure)
  * Build DEEP products as SUBASSEMBLIES — each its own .step+, mated into the parent —
    never one monolithic file. It keeps mates, interference checks, and the BOM legible.
  * Before a large assembly build call assembly_plan_review (extract-subassembly / group /
    split-multibody advice) and use cad_graph to TRAVERSE an existing assembly's structure
    and dimensional links.

GOTCHAS
  * asm_* ops REQUIRE an assembly doc (new_assembly) — they are rejected on a part doc.
  * Components reference their source FILES: keep the .step+ files where the assembly can
    find them (same directory is safest) and re-save parts before re-solving the assembly.
  * An unconstrained component has 6 DOF: a single concentric leaves it free to slide+spin —
    add the seating coincident (and an anti-spin mate) before trusting positions.
  * Set the FIRST component fixed (default) so the solve has a ground.
"""

_OVERVIEW_HEADER = """\
Chudware MODELING GUIDE (C++ parametric engine — same as the desktop app).

WORKFLOW
  1. new_part(name)                         start an editable part document.
  2. add_features([ ...ops... ])            build with standard features (vocabulary below).
                                            Returns per-feature status + body count + volume
                                            so you can see a dropped cut or a stray body.
                                            A `sketch` feature should carry geometric RELATIONS
                                            (coincident/horizontal/parallel/tangent/...) + DRIVING
                                            DIMENSIONS, not loose geometry; check_sketch("s1")
                                            reports dof + conflicts so you can reach fully-defined.
     preflight_ops(ops)                     OPTIONAL but recommended before big edits: dry-runs
                                            the planned ops on a shadow clone — checks dangling
                                            refs, impossible fillets, hole-through-nothing, and
                                            downstream feature breaks — WITHOUT touching the live
                                            doc. Returns aicad.preflight/1 with advisory_items.
                                            A fail verdict is advisory (never blocks); add_features
                                            runs preflight automatically (CHUDWARE_PREFLIGHT=1).
  3. render_part()                          SEE it — an annotated, colour-coded image (faces
                                            tinted + labelled TOP/FRONT/RIGHT...). Self-review
                                            against the request; iterate add_features to fix.
                                            render_part(view="right" | {"azimuth":30,"elevation":20})
                                            for a specific angle; render_part(emphasize="f2") for
                                            feature EMPHASIS (high-contrast target, desaturated
                                            context). render_views() for the full 6-ortho+iso
                                            labelled grid; contact_sheet() for a smaller multi-
                                            angle sheet. inspect_part(target) / inspect_feature(
                                            target) for a CLOSE-UP (feature AABB frame + dims;
                                            inspect_feature also returns a describe_part excerpt
                                            + emphasis palette). look_around(target) for N orbit
                                            close-ups. VERIFY LOOP after every mutation.
  4. measure_part() / query_geometry(q)     check mass/bbox, or explore faces/edges.
     set_material("aluminum" | {color,...})  give a body its appearance (colour + finish); it
                                            persists in the .step+ and shows in render_part.
  5. run_fea_static(...) / run_cfd_flow(...) prove it (see cad_guide("simulation")).
  6. save_part()                            save the NATIVE .step+ — the editable feature tree
                                            PLUS its simulation studies + any drawings, in one
                                            file the desktop app reopens fully editable. This is
                                            Chudware's native format; save to it to hand the user a
                                            part they can keep working on (not a flattened mesh).
  7. export_part(fmt) / make_drawing()      hand off a NON-native interchange/mesh file
                                            (STEP/IGES/BREP exact, STL/OBJ/3MF mesh) or a
                                            dimensioned 2D drawing.

Address geometry in PLAIN WORDS (no coordinates, no face numbers): "the top face", "all
edges", "the vertical edges", "the largest round face". The engine resolves the words to
topology. cad_guide("review") explains how to read the annotated render. The full op
vocabulary, with worked examples, follows.

DOC OWNERSHIP & SAVE DISCIPLINE
  Docs you mint (new_part/new_assembly/import) are AI-OWNED. The user sees them live in a
  mirror tab while you work; when you finish (detach/close/disconnect) the app AUTO-CLOSES
  that live view unless the user is actively viewing or has adopted it by editing. So the
  responsibility to preserve work is YOURS, not the user's:
    * save_part(path=...) every DELIVERABLE before you finish — this is the hand-off.
    * close_part(discard=True) for SCRATCH docs you don't want kept.
  An unsaved AI doc that's just abandoned is retired to a crash-recovery tier — a safety
  net, NOT a delivery mechanism, and never surfaced to the user as a warning. If the user
  edits your doc they ADOPT it (it becomes theirs); you keep co-editing the same live doc.

================================================================================
"""


_GSL_GUIDE = """\
GEOMETRY-SEMANTICS LAYER — read a part WITHOUT drowning in a flat face dump, and get a
usable read on geometry EVEN WITHOUT VISION (no image, just text).

On a complex/imported part, do NOT page through query_geometry's whole face list. Instead:
  describe_part()        a compact NAMED outline: bbox, the feature groups WITH a placement
                         narrative (which face each feature sits on / touches), any detected
                         symmetry/instanced PATTERN (e.g. "6-fold rotational about +Z" with the
                         6 blade faces collapsed to one line), the bores/holes and flat faces by
                         side, and a stable `sid` for every addressable face/edge. Token cost
                         scales with the number of DISTINCT roles, not the raw face count.
  describe_sketch(id)    a sketch's plane placement, entity roster (line/circle/arc + dims),
                         constraint roster with driving-dimension values, closed-region count/
                         area, and the DOF (fully/under/over-defined) verdict — the sketch
                         equivalent of describe_part, for reviewing layout before you extrude it.
  sketch_health(id)      the narrated constraint read: WHICH free variables remain (dof in plain
                         words), open chain endpoints, per-entity under/full/over, redundant/
                         conflicting with removal hints, and an ORDERED schedule (relations first,
                         then driving dims) to reach dof 0 — the sketch loop is edit -> sketch_health
                         -> add the scheduled relations / sketch_auto_constrain -> repeat.
  sketch_auto_constrain(id)  stitch near-touching endpoints coincident (apply=True, default) or
                         just propose them (apply=False); each add is rollback-safe (skips any that
                         would conflict) and it reports which chains closed. Runs automatically after
                         edits when doc meta sketch.auto_coincident is on (default).
  describe_scene(views)  the TEXT equivalent of render_part/contact_sheet for a model with no
                         vision: per requested view (default front/right/top/iso), which faces
                         face the camera (by sid/surf/side/dims) plus the part bbox — computed
                         from B-rep normals with NO occlusion modeling (says so inline). This is
                         the non-vision self-review step; do it instead of skipping the review.
  inspect_part(target)   CLOSE-UP of a face/edge/feature (plain-word query or feature id fN):
                         face-normal camera + projected-bbox crop; emphasize=True for high-
                         contrast target + desaturated context (palette in caption).
  inspect_feature(target) ONE-CALL: framed EMPHASIS render + describe_part excerpt + dims +
                         auto-section when the target is internal. Prefer after mutations.
  render_views(views?)   6-ortho+iso labelled contact sheet (or a custom view list); optional
                         target= frames every tile to a feature AABB.
  look_around(target)    N orbit close-ups of the same target tiled as a contact sheet.
  hover(sid)             expand ONE face/edge by the sid describe_part / query_geometry gave —
                         its full card (surf, side, signed normal, concavity bore/boss, radius).
  query_adjacency()      the face-adjacency graph: which faces touch, and whether each shared
                         edge is a CONVEX outside-corner or a CONCAVE inside/pocket edge. This is
                         how you reason about "the edge between the top and front face".

Every query_geometry/describe_part match now also carries: `side` (top/bottom/left/right/front/
back), `normal` (SIGNED outward dir — unlike the unsigned `axis`), `concavity` (bore vs boss),
`radius`, and a stable `sid`. add_features returns a `changes` write-diff ("added 1 cylinder; 1
new bore; volume -3927") so you can confirm an op did what you meant instead of guessing — and,
best-effort, `overlap_warnings` when the batch introduced a boss/cut overlap (see
check_feature_overlap for the dedicated tool).
"""

_INTAKE_GUIDE = """\
INTAKE / FIRST-TRY-FIT — get real-world parts to fit on the first print.

NORTH STAR: never silently guess a dimension that decides whether parts FIT. Spend questions only
on the dims that gate a mating interface; default everything else and flag it.

ISO 286 FIT CALCULATOR — call fit_calc BEFORE setting any bore or shaft diameter:
  fit_calc(nominal, fit)          ISO 286 tolerance bands + fit character. Always call this
                                  BEFORE hole_sizing or setting a bore dimension. Example:
                                  fit_calc(20.0, "H7/g6") → hole 20.000–20.021,
                                  shaft 19.980–19.993, clearance 0.007–0.041 mm.
  recommend_fit(function)         not sure which fit class? "sliding", "press fit", "bearing",
                                  "dowel pin" → recommended ISO 286 classes + rationale.
                                  Call recommend_fit FIRST, then fit_calc with the class it gives.
  suggest_dimension_scheme(doc, sketch, process)
                                  ordered dimension plan for a sketch: which dims to drive,
                                  default IT-grade tolerances by process, bolt-pattern datum rules,
                                  and needs_user_confirm flags for function-critical dims.
                                  CNC/lathe → IT7, FDM → IT12, sheet/laser → IT10.

WHEN the design must incorporate a real part you have NO model/datasheet for:
  1. LOOK IT UP FIRST — list_standard_parts / make_standard_part. Only a real miss triggers photos.
  2. resolve_process(request) — pick the manufacturing process (FDM 3D-printing is the default).
     If it returns ambiguous, ASK the user the one `clarification` question (e.g. sheet-metal vs
     CNC for a flat metal plate); then set_process(process). The process sets the fit clearances.
  3. request_part_photos(label, critical_features) — ask for photos WITH a scale reference (a
     printable scale card / calipers-in-frame / ruler) in the SAME PLANE as the feature, plus the
     ortho views + close-ups it lists. The user replies with photos next turn.
  4. submit_part_photos(claims) — report the dims YOU read from the photos as structured claims
     (with scale_source / in_plane / agrees_across_views / edge_quality). It scores each and
     returns `open_demands`: the fit-critical dims whose confidence is too low for their fit.
  5. confirm_dimensions([{key,value_mm}]) — get the user to TYPE a caliper number for each demand
     (a press/slip fit won't work off a photo guess). When the gate clears, build the geometry.
  6. Build the RECEPTACLE to fit, not a copy — the process clearance is already in the numbers.
  7. fit_report() to review modeled-vs-measured + clearances before export; dfm_check() for wall/
     draft vs the process minimums; make_fit_coupon(key) to print a tiny test of one interface
     first. All of this persists in the .step+ (reopens in-app intact).
"""

_CAM_GUIDE = """\
CAM / MACHINING GUIDE — generate real CNC toolpaths + G-code from the part.

The CAM suite turns a part into a machining JOB: a tree of SETUPS (each a stock + work
coordinate system + machine/platform) holding OPERATIONS (toolpaths), driven by a tool
library and a material-aware feeds-&-speeds engine, posted to controller G-code.

TYPICAL LOOP
  1. cam_new_job("Bracket", post="grbl")            create the job (pick a post dialect).
  2. cam_add_tool(number=1, type="endmill_flat", diameter=6, flutes=3)   build a tool library.
  3. cam_extract(kind="all")  OR  cam_extract(kind="stock"|"holes"|"section"|"mesh")
     pull stock bounds, a mesh, planar regions, and drill points FROM THE CURRENT PART.
  4. cam_add_setup(name="Op1", machine="mill3", stock=<from extract>, material="aluminum").
  5. cam_add_operation(setup_id, type=..., tool_number=..., geometry=<from extract>, ...).
        2.5D mill: face, contour2d, pocket2d (islands), slot, chamfer2d, bore2d, engrave, adaptive2d
        holes:     drill (cycle=drill|peck|chipbreak|tap|ream|bore|spot), thread_mill
        3D mill:   parallel3d, contour3d (waterline), scallop3d, pencil3d, pocket3d, adaptive_clear3d, radial3d, spiral_finish3d
        turning:   turn_face, turn_rough, turn_finish, turn_groove, turn_thread, turn_part, turn_drill
        cutting:   cut2d (laser/plasma/waterjet — kerf, pierce, lead in/out); probe
     Feeds/speeds resolve automatically from the material + tool; override any of rpm/feed/
     plunge/stepover/stepdown on the op to pin it.
  6. cam_generate()    build the toolpaths (per setup / op).
  7. cam_simulate()    material-removal sim: removed/remaining volume, gouge + rapid-collision
                       checks, estimated cycle time.
  8. cam_post("fanuc") emit the NC program; cam_setup_sheet() for the operator sheet.
  9. Audit: cam_audit — modal parse, envelope, feeds, tool and round-trip checks; ship only ok:true.

FEEDS & SPEEDS ADVISOR (T-W6 — table-driven, aicad.calc/1 contract)
  Before cam_add_operation, call feeds_speeds() to get a recommended envelope:
    feeds_speeds("alu_6061", "endmill_flat", 10.0, "roughing")
    → {schema:"aicad.calc/1", answer:{rpm:{min,mid,max}, feed:{min,mid,max},
       stepdown:{min,mid,max}, stepover:{min,mid,max}, chipload, coolant, provenance}}
  After adding the op, call cam_check_feeds_envelope() to flag risky values:
    cam_check_feeds_envelope("alu_6061", "endmill_flat", 10.0, "roughing", rpm=50000)
    → {schema:"aicad.advisory/1", items:[{severity:"hint", candidate_fixes:[clamped_values]}]}
  For tap operations, get the drill size first:
    tap_drill("M6x1.0")  → {answer:{tap_drill_mm:5.0, pct_thread:75}}
    tap_drill("1/4-20")  → {answer:{tap_drill_mm:5.1, ...}}
  Supported tool_types: endmill_flat, endmill_ball, drill, tap, face_mill.
  Supported operations: roughing, finishing, slotting, drilling.
  Materials: alu_6061, steel_1018, steel_304_stainless, ti_6al4v, abs, pla,
             nylon_pa12, cast_iron_gray, inconel_718 (+ aliases from cam_materials()).

PLATFORMS: 3-axis mill, router, 4/5-axis (3+2), lathe / mill-turn, laser, plasma, waterjet.
POSTS: grbl, linuxcnc, fanuc, haas, mach3, marlin (laser), siemens (840D), centroid/tormach —
  fully data-driven (cam_post_dialects() lists them); per-controller canned cycles, arc IJK/R,
  work offsets, tool changes, coolant, laser power gating.
REFERENCE: cam_post_dialects(), cam_materials() (cutting-data DB), cam_tool_types(), cam_op_types(),
           feeds_speeds(), cam_check_feeds_envelope(), tap_drill().
Address geometry the same plain-English way as modeling — extract regions/holes/sections from the
part, don't hand-type coordinates unless you must.
"""


def cad_guide_text(topic: str = "overview") -> str:
    topic = (topic or "overview").strip().lower()
    if topic in ("overview", "features", "ops", "geometry", "vocabulary", "vocab"):
        return _OVERVIEW_HEADER + _read_prompt("shared/ops_cheatsheet.md")
    if topic in ("review", "render", "vision"):
        return _read_prompt("reviewer.md") or "(reviewer guide unavailable)"
    if topic in ("simulation", "sim", "fea", "cfd"):
        return _SIM_GUIDE
    if topic in ("motion", "kinematics", "kinematic", "mechanism", "simulate_motion",
                 "linkage", "animate", "playback"):
        return _MOTION_GUIDE
    if topic in ("assembly", "assemblies", "asm", "mate", "mates", "component",
                 "components"):
        return _ASSEMBLY_GUIDE
    if topic in ("intake", "fit", "fits", "iso286", "tolerance",
                 "manufacturing", "dfm", "photos"):
        return _INTAKE_GUIDE
    if topic in ("gsl", "describe", "outline", "digest", "adjacency"):
        return _GSL_GUIDE
    if topic in ("cam", "machining", "toolpath", "toolpaths", "gcode", "g-code", "mill", "nc"):
        return _CAM_GUIDE
    if topic in ("narration", "narrate", "describe_toolpaths", "describe_results",
                 "describe_assembly", "describe_board", "describe_project",
                 "results", "fea_results", "cfd_results", "board", "project"):
        return (
            "NARRATION TOOLS — compact text summaries of every domain state:\n\n"
            "  describe_toolpaths([job_id])\n"
            "    Narrates an active CAM job: per-op strategy, tool, stepdown/stepover/stock,\n"
            "    cycle time + gouge/rapid counts (from cam_simulate), and a G-code digest\n"
            "    (bounds, feed range, tool changes) from cam_post.\n"
            "    Use INSTEAD of dumping cam_job_tree JSON.\n"
            "    Call cam_generate + cam_simulate first for full output.\n\n"
            "  describe_results([doc_id], [study_name])\n"
            "    Narrates the latest (or named) FEA/CFD study on a part: max von Mises stress,\n"
            "    max displacement, factor of safety, margin vs yield, the fixed/loaded faces\n"
            "    named by sid, modal frequencies, and mesh convergence caveat.\n"
            "    Use INSTEAD of dumping run_fea_static / run_fea_modal JSON.\n"
            "    Returns a graceful 'no results attached' message when no study exists.\n\n"
            "  describe_assembly([doc_id])\n"
            "    Narrates an assembly or multi-body part: per-component world-frame bounding\n"
            "    boxes and origins (from asm_world_bodies), interference/clearance pairs\n"
            "    (volumes + gap distances from eval_interference), and mate solve status.\n"
            "    Use INSTEAD of dumping asm_world_bodies JSON.\n"
            "    Call solve_mates first for the latest placement. To validate that mating\n"
            "    interfaces LINE UP (hole patterns, coaxial bores, flush contact faces,\n"
            "    fastener pass-through) — not just that nothing clashes — run interface_check.\n\n"
            "  describe_board([doc_id])\n"
            "    Narrates an active schematic or PCB: component roster (ref/value/footprint),\n"
            "    net list with members, ERC/DRC violation counts, and unconnected pins.\n"
            "    Promotes ecad_status into the GSL narration layer.\n"
            "    Open a design first with ecad_new_schematic or ecad_new_pcb.\n\n"
            "  describe_project([run])\n"
            "    Narrates a DAG project run: nodes done/doing/blocked/pending, budget usage,\n"
            "    and open questions. Feeds WS-A dag_status and the T-F17 mission-control UI.\n"
            "    Pass run=dag_status()['run'] from your WS-A driver.\n"
            "    See contracts/dag_schema.md for the run dict shape.\n\n"
            "All tools narrate EXISTING numbers only — they never recompute values.\n"
            "See contracts/narration.md for the full style guide (rounding, attribution,\n"
            "graceful-degradation phrases, section order)."
        )
    if topic in ("deliverables", "deliverable", "report", "presentation", "deck", "workbook",
                 "spreadsheet", "artifacts", "artifact"):
        # The deliverables family (make_report/make_presentation/make_workbook/render_document/
        # list_artifacts) ships in a separate `deliverables` package (T-F14) so this file stays
        # read-only w.r.t. that node's write-set; lazy-import so the guide degrades to a pointer
        # if that package isn't present and picks up the real text the moment it lands.
        try:
            from deliverables import DELIVERABLES_GUIDE  # noqa: PLC0415
            return DELIVERABLES_GUIDE
        except ImportError:
            return ("Deliverables (spec-driven .pptx/.docx/.pdf/.xlsx with a provenance rule) ship "
                    "with the deliverables package; make_report/make_presentation/make_workbook/"
                    "render_document/list_artifacts.")
    if topic in ("parts", "standard_parts", "standard-parts", "library", "catalog",
                 "part_search", "part_get", "part_place", "hardware", "bom_parts",
                 "parts_taxonomy", "taxonomy", "facets", "faceted_search"):
        return (
            "STANDARD-PARTS LIBRARY — search → get → place (T-P3, aicad.parts/1)\n\n"
            "BIAS: always prefer standard parts over custom geometry for hardware.\n"
            "Standard parts are editable, sourced, and BOM-ready out of the box.\n\n"
            "WORKFLOW:\n"
            "  1. part_search(query, domain?, limit?)\n"
            "       Search the federated catalog.  Returns ranked PartRecord list.\n"
            "       Examples:\n"
            "         part_search('M8 socket head')         → fastener records\n"
            "         part_search('6205')                   → ISO 6205 bearing\n"
            "         part_search('W8x31')                  → AISC W8×31 section\n"
            "         part_search('resistor', domain='ecad')→ KiCad resistor symbols\n"
            "         part_search('elbow NPS1')             → piping fitting\n"
            "         part_search('pump', domain='hydraulic')→ ISO 1219 pump\n"
            "       Domains: fastener | bearing | mech_family | section | fitting |\n"
            "                hydraulic | ecad\n\n"
            "  2. part_get(part_id)\n"
            "       Fetch the full PartRecord for a stable part_id:\n"
            "         part_get('fastener:ISO4762:M8')       → M8 SHCS record\n"
            "         part_get('bearing:ISO 15:6205')       → 6205 bearing record\n"
            "       Inspect params + geometry_ref to confirm dimensions before placing.\n\n"
            "  3. part_place(part_id, doc?, params?)\n"
            "       Insert the part — always editable, never an opaque solid.\n\n"
            "       Mechanical (fastener / bearing / mech_family):\n"
            "         part_place('fastener:ISO4762:M8')\n"
            "         part_place('fastener:ISO4762:M8', params='{\"length_mm\": 25}')\n"
            "         → Creates a new editable part doc_id.  Use make_standard_part\n"
            "           equivalently if you already know family/size.\n\n"
            "       ECAD (schematic symbol):\n"
            "         part_place('ecad:Device:R',\n"
            "                    doc=json.dumps(sch_doc),\n"
            "                    params='{\"pos\":{\"x\":10,\"y\":20},\"value\":\"1k\"}')\n"
            "         → Appends sch_add_symbol op; symbol appears in doc['symbols'].\n\n"
            "       Structural section:\n"
            "         part_place('section:AISC:W8x31',\n"
            "                    doc=json.dumps(struct_doc),\n"
            "                    params='{\"kind\":\"beam\",\"length_mm\":3000}')\n"
            "         → Appends StructuralElement to doc['elements'].\n\n"
            "       Piping fitting:\n"
            "         part_place('fitting:elbow:NPS1',\n"
            "                    doc=json.dumps(piping_doc),\n"
            "                    params='{\"type\":\"elbow\",\"nps\":1.0}')\n"
            "         → Appends fitting record to doc['fittings'].\n\n"
            "       Hydraulic component:\n"
            "         part_place('hydraulic:pump:gear-1cc',\n"
            "                    doc=json.dumps(hyd_doc),\n"
            "                    params='{\"type\":\"pump\",\"displacement_cc_rev\":1.0}')\n"
            "         → Appends component record to doc['components'].\n\n"
            "DEDUPLICATION NOTE:\n"
            "  Threaded-hardware queries may surface both fastener: and mech_family: records\n"
            "  for the same designation.  The search ranks fastener > mech_family for\n"
            "  standard-hardware queries, and same-designation results carry a related_ids\n"
            "  field pointing to the sibling.  Prefer the top-ranked result; both are\n"
            "  placeable and editable.\n\n"
            "EDITABILITY GUARANTEE:\n"
            "  part_place always returns {'editable': true}.  A placed part that isn't\n"
            "  editable is a bug — report it and use part_get to inspect geometry_ref.\n\n"
            "TAXONOMY BROWSE + FACETED SEARCH (Parts Library V2):\n"
            "  parts_taxonomy()                       → root categories (fasteners,\n"
            "      bearings_motion, structural_stock, drivetrain, tools, electronics,\n"
            "      fluid_power, hardware_misc, vendors, ...)\n"
            "  parts_taxonomy('bearings_motion.ball_bearings.deep_groove')\n"
            "      → children + breadcrumbs + EFFECTIVE facets (the facet keys this\n"
            "        category accepts: bore_mm, od_mm, width_mm, seals, ...)\n"
            "  part_search(query, category_id=..., facets='{...}')\n"
            "      → taxonomy-ROUTED search (right domains + the category's vendor\n"
            "        sites) with facet post-filtering.  Examples:\n"
            "    part_search('tube', category_id='structural_stock.metal_tube',\n"
            "                facets='{\"od_mm\": 25.4, \"alloy\": \"4130\"}')\n"
            "    part_search('bearing', category_id='bearings_motion.ball_bearings',\n"
            "                facets='{\"bore_mm\": {\"min\": 10, \"max\": 14}}')\n"
            "    part_search('end mill', category_id='tools.cutting_tools.end_mills',\n"
            "                facets='{\"flutes\": \"4\", \"coating\": \"AlTiN\"}',\n"
            "                price_max=40, vendors='mcmaster', in_stock=True)\n"
            "  The response's facet_summary shows the top param values in THIS result\n"
            "  set — read it to see what to narrow by next.\n\n"
            "NEED-DRIVEN SOURCING: part_source('4130 chromoly tube OD 25.4 wall 1.25')\n"
            "  deterministically matches the taxonomy + extracts facets + returns scored\n"
            "  candidates, each with an artifact_plan — see cad_guide('sourcing').\n\n"
            "SOURCING:\n"
            "  Each PartRecord carries a sourcing[] list of VendorPart dicts (aicad.sourcing/1).\n"
            "  Use bom_rollup after placing multiple parts to get a costed BOM.\n"
        )
    if topic in ("reverse", "re", "recognize", "recon"):
        # WS-C (wave 3) ships the recognition pipeline in a separate recon_tools module so this
        # file stays read-only w.r.t. that node's write-set; lazy-import so cad_guide("reverse")
        # works TODAY (degrades to a pointer) and picks up the real guide the moment it lands.
        try:
            from mcp_server.recon_tools import _RE_GUIDE  # noqa: PLC0415
            return _RE_GUIDE
        except ImportError:
            return ("Reverse-engineering guide ships with the recon tools (wave 3); use "
                    "import_part + describe_part + compare workflow meanwhile.")
    if topic in ("requirements", "req", "req_create", "req_update", "req_status", "req_report",
                 "brief", "design_brief", "layout_plan", "layout", "critical_dim"):
        return (
            "REQUIREMENTS (aicad.req/1) — design-brief CRUD + markdown projection\n\n"
            "Schema: contracts/requirements.md  |  Engine: services/requirements/\n\n"
            "WORKFLOW:\n"
            "  req_create(title, body)              Create a new requirements doc; returns req_id.\n"
            "  req_update(req_id, needs?, reqs?)    Add/edit individual need or requirement rows.\n"
            "  req_status(req_id)                   Current fulfilment table (met/partial/open).\n"
            "  req_report(req_id)                   Full markdown projection for sharing —\n"
            "                                       battery-populated evidence is listed\n"
            "                                       separately from hand-entered evidence.\n"
            "  req_verify_by_id(requirement_id,     AUTO-VERIFY one requirement: runs its\n"
            "      doc_id?, req_path?)              verify_by verifiers via the battery and\n"
            "                                       writes status flip + evidence back into\n"
            "                                       the doc (no manual req_update needed).\n\n"
            "AUTOMATIC PROOF LOOP: a DAG verify node with inputs\n"
            "  {check_type: 'requirement', req_id: 'R1', req_path|req_doc_id, doc_id}\n"
            "runs the requirement's named verify_by verifier and auto-flips its status\n"
            "with a source='battery' evidence ref — closing idea → requirement → proof.\n\n"
            "BIAS: capture requirements BEFORE modeling — req_create at project start, then\n"
            "reference req_id in every verify_battery call so the battery knows what to check.\n"
            "A requirement without a verifier is aspirational; wire them up.\n\n"
            "LAYOUT-FIRST — MANDATORY before any modeling (T-BL50):\n"
            "  layout_plan(doc_id,\n"
            "              envelope_x_mm, envelope_y_mm, envelope_z_mm,\n"
            "              critical_dims=[{param, value, unit, tol_plus, tol_minus, why, verify_by},...],\n"
            "              interfaces=[{id, desc, verify_by},...],\n"
            "              note='intent summary')\n\n"
            "  Call layout_plan() FIRST — before add_features — to record:\n"
            "    - Overall bounding envelope (x/y/z mm)\n"
            "    - Critical dimensions: value + tolerance + why-critical (each becomes a req)\n"
            "    - Component interfaces: what mates to what, via which features\n\n"
            "  Each entry becomes an aicad.req/1 requirement (kind=dimensional or interface)\n"
            "  visible in req_report and checked by the coverage gate at sign-off.\n\n"
            "  layout_plan_report(doc_id)  — human-readable coverage state per dim/interface.\n\n"
            "COVERAGE GATE — mandatory before declaring done:\n"
            "  design_review(doc_id, checks=['layout_coverage', ...])\n"
            "    WARN/FAIL if any critical dim or interface has no verification evidence.\n"
            "    Passes when every critical dim has status=verified+evidence, user_confirmed,\n"
            "    or waived. An agent finishing a part with an unverified critical dim gets\n"
            "    WARN/FAIL — not silence.\n\n"
            "What counts as evidence per dim type:\n"
            "  dimensional  → measure_between, measure_part, query_geometry, confirm_dimensions,\n"
            "                  run_fea_static/modal, fit_report\n"
            "  interface    → check_interference, solve_mates, measure_between, fit_report,\n"
            "                  interface_check (T-BL49, when available)\n\n"
            "FULL LOOP:\n"
            "  1. layout_plan(doc_id, ...)           capture before modeling\n"
            "  2. add_features(...)                  model to the plan\n"
            "  3. measure_between(...) / req_status(requirement_id, 'verified', evidence=[...])\n"
            "                                        record evidence as each dim is confirmed\n"
            "  4. design_review(doc_id, ['layout_coverage'])  gate before done\n"
            "  5. verify_battery(doc_id)             final battery\n"
        )
    if topic in ("dag", "dag_plan", "dag_run", "dag_status", "dag_approve", "dag_abort",
                 "dag_replan", "orchestrate", "mission", "project_run", "workflow"):
        return (
            "DAG ORCHESTRATOR — multi-step missions with gates, budgets, and human approvals\n\n"
            "Tools: dag_plan / dag_run / dag_status / dag_approve / dag_abort / dag_replan\n"
            "Schema: contracts/dag_schema.md\n\n"
            "WORKFLOW:\n"
            "  dag_plan(goal, nodes)     Draft a DAG of tool-call nodes with retry budgets\n"
            "                            and end_conditions.  Returns a plan dict.\n"
            "  dag_run(plan)             Execute the plan; checkpoints each node; returns run_id.\n"
            "  dag_status(run_id)        Poll: nodes done/doing/blocked/pending, budget usage.\n"
            "  dag_approve(run_id, node) Ungate a human-approval node so the run continues.\n"
            "  dag_abort(run_id)         Cancel a running mission cleanly.\n"
            "  dag_replan(run_id, nodes) Splice new nodes into a RUNNING checkpointed run\n"
            "                            (validates end_condition + retry_budget on every node).\n\n"
            "NARRATION: describe_project(run=dag_status(...)['run']) gives a human-readable\n"
            "  summary (nodes done/doing/blocked, budget, open questions) instead of raw JSON.\n\n"
            "BIAS: use DAG for any mission with 3+ sequential tool calls that could fail\n"
            "  independently — it checkpoints progress and resumes after transient errors."
        )
    if topic in ("verify", "verify_battery", "battery", "check", "done", "sign-off"):
        return (
            "VERIFY BATTERY — deterministic done-check before calling a design finished\n\n"
            "Tools: verify_battery(doc_id, checks?) / check_metadata()\n\n"
            "MANDATORY before declaring done:\n"
            "  verify_battery(doc_id)   Runs the full battery: feature overlap, interference,\n"
            "                           wall thickness, draft angles, DFM, and any req_id-linked\n"
            "                           requirement checks.  Returns a verdict card per\n"
            "                           contracts/narration.md — PASS/WARN/FAIL per check.\n"
            "  check_metadata()         Confirms doc has title, material, and process set.\n\n"
            "ALSO: check_feature_overlap() and check_interference() run individual checks.\n\n"
            "REQUIREMENT AUTO-VERIFICATION (aicad.req/1): req_verify_by_id(requirement_id,\n"
            "  doc_id?, req_path?) runs a requirement's named verify_by verifiers through\n"
            "  the battery and writes the verified/failed status + evidence ref back into\n"
            "  the requirements doc automatically.  DAG verify nodes do the same with\n"
            "  inputs {check_type: 'requirement', req_id, req_path|req_doc_id, doc_id}.\n\n"
            "BIAS: never report 'done' to a user until verify_battery returns no FAILs.\n"
            "  A WARN is a judgment call — narrate it; don't silently ignore it.\n"
            "  Wire req_id into the battery call so requirement fulfilment is checked too."
        )
    if topic in ("interaction", "elicitation", "ask_user", "clarify", "questions"):
        return (
            "INTERACTION / ELICITATION — the single ask_user seam (contracts/interaction.md)\n\n"
            "Tool: ask_user(questions, scope?)\n\n"
            "  DO NOT ask by default. Prefer a typed suggested default + required:false, or\n"
            "  skip the tool and record an assumption. Ask only when the answer changes the\n"
            "  build (fit-critical dim, process branch, design option, which feature to edit).\n\n"
            "  ask_user([\n"
            "    # Design decision — single-select choice (recommended option in suggested):\n"
            "    {'id': 'material', 'kind': 'choice', 'prompt': 'What material?',\n"
            "     'options': [{'value':'aluminum','label':'6061-T6'},\n"
            "                 {'value':'steel','label':'1018 steel'}],\n"
            "     'suggested': 'aluminum', 'required': false,\n"
            "     'why': 'Drives stock + feeds.'},\n"
            "    # Multi-select design options (pick any that apply):\n"
            "    {'id': 'keep_feats', 'kind': 'choice', 'multi': true,\n"
            "     'prompt': 'Which features should stay?',\n"
            "     'options': [{'value':'rib'},{'value':'fillet'},{'value':'boss'}],\n"
            "     'suggested': ['rib','fillet']},\n"
            "    # Geometry pick — user clicks the model (do NOT invent sids):\n"
            "    {'id': 'edit_face', 'kind': 'geometry_select',\n"
            "     'prompt': 'Which face should get the pocket?',\n"
            "     'targets': ['face'], 'suggested': null, 'required': true},\n"
            "    # Dimension / text / approval also supported.\n"
            "  ])\n\n"
            "  kinds: choice (multi?:bool), dimension, text (multiline?:bool),\n"
            "         file_request, approval, geometry_select (targets, multi?).\n"
            "  Returns {id: {value, canonical?, source, at}}. Answers persist + replay:\n"
            "  same scope+id returns the stored answer without re-prompting.\n\n"
            "BIAS: batch unknowns into ONE ask_user call (≤4). Use suggested everywhere.\n"
            "  Design decisions → choice options. Can't see the model → geometry_select.\n"
            "  Scope='run' (default) per run; scope='doc' persists in the .step+."
        )
    if topic in ("sourcing", "bom", "bom_rollup", "bom_price", "vendor", "purchase",
                 "part_source", "vendors_health", "cam_tool_import", "procure",
                 "scrape", "scrape_enqueue", "import_artifact"):
        return (
            "SOURCING — need → candidates → geometry → BOM → quote (Parts Library V2)\n\n"
            "THE FULL LOOP:\n"
            "  1. parts_taxonomy(node_id?)          Browse categories; a node's facets\n"
            "                                       are the filter keys it supports\n"
            "                                       (cad_guide('parts') for examples).\n"
            "  2. part_source(need, constraints?, limit?)\n"
            "       Deterministic sourcing fan-out for a plain-language need:\n"
            "         part_source('4130 chromoly tube OD 25.4 wall 1.25')\n"
            "         part_source('bearing 12mm bore', constraints='sealed, under $10')\n"
            "       It matches taxonomy categories, extracts spec facets from the text\n"
            "       (OD/bore/wall/alloy/...), searches the routed domains, and returns\n"
            "       scored candidates: {part_id, title, vendor, price, specs, score,\n"
            "       rationale, artifact_plan}.  Schema: contracts/sourcing.md.\n"
            "  3. Narrow interactively when needed: part_search(query, category_id,\n"
            "       facets, price_max, vendors, in_stock) + its facet_summary.\n"
            "       part_get(part_id) for the full record before committing.\n"
            "  4. Follow each candidate's artifact_plan.how to get REAL geometry:\n"
            "       part_place          → local record with geometry_ref: place it.\n"
            "       scrape_import       → vendor catalog record: scrape_enqueue(site,\n"
            "                             query) → poll scrape_queue_status() → done →\n"
            "                             import_artifact(artifact_path).\n"
            "       make_standard_part  → standard hardware family: make_standard_part.\n"
            "       find_model          → discrete manufactured product (MPN/brand, no\n"
            "                             model, not stock/hardware): match_model(part_id).\n"
            "                             AUTO-PLACE only confidence 'exact'/attach:true;\n"
            "                             strong/weak need user confirmation.\n"
            "       reverse_engineer    → match_model's fallback when no confident match:\n"
            "                             reverse_engineer_product(part_id|url) harvests\n"
            "                             photos+datasheets into a seeded RE session.\n"
            "  5. CUTTING TOOLS are special: an end mill / drill from the library goes\n"
            "       into the CAM job via cam_tool_import(part_id, number=0) — it maps\n"
            "       the vendor record (tool_diameter_mm→diameter, flutes, coating) into\n"
            "       the tool library with provenance and suggests conservative\n"
            "       stepover/stepdown.  Feeds/speeds ALWAYS come from the CuttingDB\n"
            "       (feeds_speeds / cam_check_feeds_envelope), never the vendor row.\n"
            "  6. BOM + COST: bom_rollup(doc_path) rolls the tree into a flat BOM\n"
            "       (PartRecord sourcing[] rows merge in automatically); bom_price\n"
            "       resolves live prices via approved providers (Nexar, DigiKey,\n"
            "       Mouser); request_quote(doc_path, vendor, qty) for PO decisions;\n"
            "       cam_cost_estimate for made-part cost seeds.\n\n"
            "VENDOR LOGIN ETIQUETTE:\n"
            "  vendors_health(sites?) runs a canary search per adapter and reports\n"
            "  {site, ok, latency_ms, logged_in, hit_count, error, needs_human_login}.\n"
            "  needs_human_login=true means a HUMAN must run vendor_login(site) — a\n"
            "  headed browser window only a person can complete.  Record a question\n"
            "  for the human and continue with the healthy sites; NEVER fabricate\n"
            "  results for a logged-out site, and never retry a login-required site\n"
            "  in a loop.  vendor_status() lists adapter login state cheaply.\n\n"
            "BIAS: always prefer part_search/part_source + part_place for hardware —\n"
            "  candidates arrive pre-sourced and editable.  Never invent prices or\n"
            "  specs: every number in a recommendation comes from a tool result."
        )
    if topic in ("cost", "cost_estimate", "cam_cost", "quote", "request_quote", "pricing"):
        return (
            "COST-OUT — made-part cost model + approved quote-out APIs\n\n"
            "Tools: cam_cost_estimate(doc_path, material?, process?) / request_quote(doc_path, vendor, qty?)\n\n"
            "  cam_cost_estimate(doc_path)   Material + machining cost seed from the CAM job.\n"
            "                                Returns cost_usd_low/mid/high, cycle_time_min.\n"
            "                                Call AFTER cam_generate so toolpaths exist.\n"
            "  request_quote(doc_path, vendor, quantity)\n"
            "                                Submit a quote request to an approved provider.\n"
            "                                Returns quote_id; provider contacts out-of-band.\n\n"
            "BIAS: cam_cost_estimate first for internal budget; request_quote for PO decisions.\n"
            "  Never invent prices — use these tools; the provenance rule applies here too."
        )
    if topic in ("spec", "spec_sheet", "spec_sheet_match", "match_part_number",
                 "datasheet", "mpn", "part_number"):
        return (
            "SPEC-SHEET MATCHING — deterministic part-number → standard-part model\n\n"
            "Tools: match_part_number(mpn) / spec_sheet_match(text_or_url, mpn, template) / list_spec_templates()\n\n"
            "  match_part_number(mpn)              Resolve an MPN to a PartRecord (BOLTS/KiCad).\n"
            "                                      Returns part_id usable in part_place.\n"
            "  spec_sheet_match(text_or_url, mpn, template)\n"
            "                                      Extract structured parameters from a datasheet\n"
            "                                      PDF/URL using a named template.\n"
            "  list_spec_templates()               Show available extraction templates.\n\n"
            "WORKFLOW: paste/URL a datasheet → spec_sheet_match → part_place with extracted params.\n"
            "BIAS: use match_part_number before custom geometry — if the MPN resolves, you get\n"
            "  an editable, pre-sourced part in one call."
        )
    if topic in ("piping", "pipe", "pipe_network", "fluid", "pressure_drop",
                 "piping_solve", "piping_report"):
        return (
            "PIPING — routed pipe networks: steady-state 1-D hydraulic solve + CFD handoff\n\n"
            "Tools: piping_solve / piping_report / piping_operating_point / piping_fluid /\n"
            "       piping_internal_volume / piping_cfd_handoff / piping_verify_cfd\n\n"
            "WORKFLOW:\n"
            "  1. Build pipe network doc (nodes + segments + fittings via part_place).\n"
            "  2. piping_fluid(name, T, P)          Resolve fluid properties at operating point.\n"
            "  3. piping_solve(doc, boundary)        1-D hydraulic solve: flows + pressure map.\n"
            "  4. piping_report(doc)                 Narrated pressure-drop + velocity report.\n"
            "  5. piping_operating_point(pump_curve, system_curve)\n"
            "                                        Intersect pump + system curves → duty point.\n"
            "  6. piping_internal_volume(doc)        Total fluid volume (for line-sizing / purge).\n"
            "  7. piping_cfd_handoff(doc)            Export mesh + BCs for external CFD solver.\n"
            "  8. piping_verify_cfd(solve_result)    Validate CFD result against 1-D baseline.\n\n"
            "EXTERNALLY-SUPPLIED NETWORKS: piping_solve(source_handoff=<aicad.simhandoff/1\n"
            "  handoff envelope>) consumes a network generated by another domain — e.g.\n"
            "  hydraulic_piping_handoff hands its hose runs over as an inline piping doc with\n"
            "  boundary + fluid in the envelope meta; runs may carry an explicit bore_mm to\n"
            "  bypass the NPS schedule catalog (hydraulic hose IDs aren't catalog pipes).\n\n"
            "BIAS: use piping_solve first (fast, deterministic); escalate to CFD only when\n"
            "  3-D effects (mixing, cavitation, complex geometry) matter."
        )
    if topic in ("hydraulic", "hydraulics", "iso1219", "hydraulic_circuit",
                 "hydraulic_new", "cylinder", "pump"):
        return (
            "HYDRAULIC — ISO 1219 circuit design + component sizing\n\n"
            "Tools: hydraulic_new / hydraulic_add_component / hydraulic_add_hose /\n"
            "       hydraulic_size_cylinder / hydraulic_size_pump / hydraulic_bom /\n"
            "       hydraulic_piping_handoff / hydraulic_verify_piping /\n"
            "       hydraulic_fea_handoff / hydraulic_verify_fea\n\n"
            "WORKFLOW:\n"
            "  hydraulic_new(name)                  Create a new hydraulic circuit doc.\n"
            "  hydraulic_add_component(doc, type, params)\n"
            "                                       Add pump, valve, cylinder, reservoir, etc.\n"
            "                                       (or use part_place with domain='hydraulic').\n"
            "  hydraulic_add_hose(doc, from, to, params)\n"
            "                                       Connect components with sized hose/tube.\n"
            "  hydraulic_size_cylinder(doc, force, stroke, pressure)\n"
            "                                       Size cylinder bore + rod from load + pressure.\n"
            "  hydraulic_size_pump(doc, flow, pressure)\n"
            "                                       Size pump displacement + motor power.\n"
            "  hydraulic_bom(doc)                   BOM with ISO 1219 part numbers + sourcing.\n\n"
            "CROSS-SIM COUPLINGS (aicad.simhandoff/1 — cad_guide('simulation')):\n"
            "  hydraulic_piping_handoff(supply_pressure_bar, flow_lpm, load_pressure_bar)\n"
            "      Hand the circuit's hose runs to the piping 1-D solver for REAL per-line\n"
            "      pressure drops at the operating point (each hose keeps its exact bore).\n"
            "      Solves live when the 'fluids' lib is installed (else stage 'solve_pending'\n"
            "      + piping_solve(source_handoff=...) elsewhere); real losses feed the pump\n"
            "      margin back (margin.pump_margin_solved_bar).\n"
            "  hydraulic_verify_piping(handoff, solve_result)\n"
            "      Per-hose Darcy estimate vs piping solve deviation verdict + margin.\n"
            "  hydraulic_fea_handoff(bore_mm, max_pressure_bar, rod_mm, axis, ...)\n"
            "      Cylinder mount loads at max pressure → static-FEA load cases (extend/\n"
            "      retract, case-labelled). Returns run_fea_static-ready fea_args per case.\n"
            "  hydraulic_verify_fea(handoff, fea_result)\n"
            "      Independent area×pressure recompute vs the envelope's force conditions.\n\n"
            "BIAS: size with hydraulic_size_cylinder/size_pump BEFORE placing components —\n"
            "  it confirms the pressure/flow budget is achievable; once hoses are routed,\n"
            "  hydraulic_piping_handoff replaces per-hose loss estimates with a real solve."
        )
    if topic in ("firmware", "fw", "embedded", "mcu", "fw_build", "fw_new_project",
                 "flash", "debug", "fw_sim_run", "fw_verify_board", "coverify",
                 "fw_power_profile", "powerprofile", "power_profile"):
        return (
            "FIRMWARE — embedded compile loop: build, sim, flash, debug\n\n"
            "Tools: fw_status / fw_new_project / fw_build / fw_sources / fw_sim_run /\n"
            "       fw_verify_board / fw_power_profile / fw_flash / fw_debug_start /\n"
            "       fw_debug_halt / fw_debug_read_mem\n\n"
            "WORKFLOW:\n"
            "  fw_status()                          FIRST CALL — provisioning doctor. Reports\n"
            "                                       each toolchain/emulator/flasher (found,\n"
            "                                       version, path, install hint) plus per-MCU-\n"
            "                                       family can_build/can_emulate/can_flash.\n"
            "                                       Never fails; use it to explain any\n"
            "                                       toolchain_not_found before retrying.\n"
            "  fw_new_project(name, mcu_family)     Scaffold a CMake firmware project.\n"
            "                                       Auto-selects toolchain; generates pinmap.h\n"
            "                                       from linked ECAD board netlist.\n"
            "  fw_sources(project_id, add)          Add source files to the project.\n"
            "  fw_build(project_id)                 Compile; returns ELF path + errors.\n"
            "  fw_sim_run(project_id, scenario)     Run ELF through Renode/QEMU with assertions:\n"
            "                                       uart:<text>, gpio:PA5=1 (real pin states),\n"
            "                                       gpio:PA5=1@>10ms (timeline). Emits a JSON\n"
            "                                       trace artifact (trace_path) of pin+UART\n"
            "                                       events; QEMU pin events need the project's\n"
            "                                       chudware_trace.h CHUDWARE_GPIO_TRACE macro.\n"
            "  fw_verify_board(board_doc_ref, ...)  Firmware<->board co-verification (T-FIN6)\n"
            "                                       BEFORE fab: static checks (every PIN_<NET>\n"
            "                                       exists on the MCU/net, strap/reset pins\n"
            "                                       BOOT0/NRST/GPIO0 not repurposed, direction\n"
            "                                       clashes vs the schematic netlist, decoupling\n"
            "                                       per MCU supply rail) + dynamic diff of\n"
            "                                       emulated pin activity vs the netlist (pass\n"
            "                                       trace_path=<elf>.trace.json or emulate=true).\n"
            "                                       Findings use the ERC/DRC violation shape\n"
            "                                       {rule, severity, net, pins, message, fix}.\n"
            "                                       Pass sch_doc_ref for direction checks.\n"
            "  fw_power_profile(trace_path, ...)    Power/duty-cycle profile (T-FIN7,\n"
            "                                       aicad.powerprofile/1, contract §11) from\n"
            "                                       an existing trace: CPU run/sleep residency\n"
            "                                       (CHUDWARE_CPU_TRACE markers, else assumed\n"
            "                                       active), per-pin GPIO duty, and per-rail\n"
            "                                       i_avg/i_peak from the board's T-FIN8\n"
            "                                       eng.supply.rails currents weighted by duty.\n"
            "                                       BOOKKEEPING not SPICE; nulls stay null.\n"
            "                                       rails[*].schedule = {duty, period_s} — feed\n"
            "                                       it straight to run_fea_thermal transient\n"
            "                                       heat-load schedules (duty-cycle heating).\n"
            "                                       fw_sim_run auto-emits <elf>.power.json\n"
            "                                       (power_profile_path) for board-linked\n"
            "                                       projects.\n"
            "  fw_flash(elf_path, chip, port)       Flash ELF to target hardware.\n"
            "  fw_debug_start/halt/read_mem(chip)   GDB-over-SWD debug session controls.\n\n"
            "SUPPORTED TARGETS (T-FIN2 Renode platform pack — what the bundled platform\n"
            "actually models; fw_sim_run refuses honestly when an assertion needs a\n"
            "peripheral the platform lacks):\n"
            "  family    emulate  uart gpio-observe i2c  spi  timers adc  notes\n"
            "  stm32f4   renode   yes  yes          yes  yes  yes    no   USART1/2, GPIO A-E, I2C1-3, SPI1-3, TIM1-5\n"
            "  stm32f1   renode   yes  yes          yes  no   yes    no   upstream stm32f103.repl (USART1-5, GPIO A-G)\n"
            "  nrf52     renode   yes  no           yes  yes  yes    no   upstream nrf52840.repl; gpio: asserts unsupported\n"
            "  rp2040    minimal  yes  no           no   no   no     no   bare-metal PL011 UART only (no stock Renode model)\n"
            "  esp32     NONE     -    -            -    -    -      -    build (idf.py) + flash (esptool) ONLY, no emulation\n"
            "  cortex-m  renode   no   no           no   no   no     no   generic fallback (CPU+memory); QEMU semihosting path\n"
            "  (no family has an ADC peripheral model upstream — but see adc_waveform below:\n"
            "   a Python-peripheral stub feeds programmed samples on stm32f4/stm32f1)\n\n"
            "SENSOR STIMULUS SCENARIOS (T-FIN3, aicad.fwscenario/1 — contract §9):\n"
            "  fw_sim_run's scenario param drives simulated sensors/GPIO into the emulated\n"
            "  firmware (Renode path only). Pass a fixture name or a JSON object:\n"
            "    fixtures: fixture:i2c_temp_tmp103   I2C TMP103 temp sensor @0x70, 25.5 C\n"
            "              fixture:spi_imu_stub      SPI IMU connectivity stub on spi1\n"
            "              fixture:quadrature_encoder PA0/PA1 encoder, 24 steps CW @5ms\n"
            "              fixture:adc_thermistor    NTC heat-up curve via ADC register stub\n"
            "              fixture:user_button       3x 80ms presses on PA0\n"
            "  track kinds: gpio_edges | button | quadrature | i2c_sensor | i2c_registers |\n"
            "               spi_stub | uart_rx | adc_waveform (sine/ramp/samples; stm32 only)\n"
            "  WORKED EXAMPLE — firmware reads an I2C temp sensor + button, prints readings:\n"
            "    fw_sim_run(project_id, scenario='{\n"
            "      \"schema\": \"aicad.fwscenario/1\",\n"
            "      \"tracks\": [\n"
            "        {\"kind\": \"i2c_sensor\", \"model\": \"tmp103\", \"bus\": \"i2c1\",\n"
            "         \"address\": 72, \"temperature_c\": 31.0},\n"
            "        {\"kind\": \"button\", \"pin\": \"PA0\",\n"
            "         \"presses\": [{\"t_ms\": 200, \"hold_ms\": 80}]}\n"
            "      ]}',\n"
            "      assertions=['uart:temp=31', 'uart_within:temp=31@2000ms',\n"
            "                  'toggles:PA5>=2', 'period:PA5=100ms+-20ms'])\n"
            "  New assertion forms (T-FIN3): toggles:<PIN>>=<N> (toggle count),\n"
            "  period:<PIN>=<P>ms+-<TOL>ms (mean rising-edge period),\n"
            "  uart_within:<text>@<T>ms (needs scenario time marks). Invalid/unsupported\n"
            "  scenarios refuse honestly (invalid_scenario / scenario_error) — e.g.\n"
            "  adc_waveform on nrf52 explains that no ADC stub window is registered.\n\n"
            "BIAS: always fw_sim_run before fw_flash — catches assertion failures without\n"
            "  hardware.  Link the ECAD board doc so pinmap.h is auto-generated.\n\n"
            "GOLDEN DEMO (T-FIN25): scripts/demo_sensor_node.py builds an STM32F4 sensor-\n"
            "  node board, runs fw_new_project + fw_verify_board, and fw_sim_run with\n"
            "  fixture:i2c_temp_tmp103 when Renode is provisioned (honest SKIP markers\n"
            "  otherwise). See docs/FIN25_SENSOR_NODE_GOLDEN_DEMO.md + cad_guide('simulation')."
        )
    if topic in ("structural", "structure", "struct", "struct_analyze", "struct_check",
                 "struct_estimate", "beam", "frame", "truss", "steel_design"):
        return (
            "STRUCTURAL — member forces & deflections for aicad.structure/1 docs (PyNite)\n\n"
            "Tools: struct_analyze / struct_check / struct_estimate\n\n"
            "WORKFLOW:\n"
            "  Build structure doc via part_place(domain='section') for beams/columns.\n"
            "  struct_analyze(doc_path, load_combo)  Member forces, deflections, reactions.\n"
            "                                        load_combo: 'DEAD_LIVE' (default),\n"
            "                                        'DEAD_LIVE_WIND', 'DEAD_LIVE_SEISMIC'.\n"
            "  struct_check(doc_path, load_combo)    Code check (AISC 360 / NDS) — PASS/FAIL\n"
            "                                        per member with UC ratios.\n"
            "  struct_estimate(doc_path)             Quick material + fabrication cost estimate.\n\n"
            "BIAS: struct_check after struct_analyze — the check references the analysis results.\n"
            "  Use part_search('W8x31') + part_place for AISC sections; standard-before-custom."
        )
    if topic in ("regen", "regen_diagnose", "rebuild", "repair", "fix_ops"):
        return (
            "REGEN DIAGNOSIS — on a rebuild failure, diagnose + propose editable fix-ops\n\n"
            "Tool: regen_diagnose(document_json, reference_json?)\n\n"
            "  regen_diagnose(document_json)         Diagnose a doc that failed to rebuild.\n"
            "                                        Returns a verdict card: root cause,\n"
            "                                        proposed fix-ops as an accept/edit/reject\n"
            "                                        card, and a correction trace (IP-18 log).\n\n"
            "WHEN TO USE: call after add_features returns any feature with ok=false, or when\n"
            "  close_part / export_part / run_fea_* fail with a regen error.\n"
            "WORKFLOW: add_features → check feature ok flags → if any false, regen_diagnose\n"
            "  → apply the proposed fix-ops → re-run add_features → verify_battery.\n"
            "NOTE: regen_diagnose is the backend seam; the copilot binding (T-F17) will\n"
            "  surface it in the desktop UI automatically."
        )
    if topic in ("code_compliance", "code_check", "building_code", "compliance",
                 "ibc", "asce", "nfpa"):
        return (
            "CODE COMPLIANCE — deterministic rule engine + agentic RAG for building-code checks\n\n"
            "Tool: code_check(doc_path, codes?, jurisdiction?)\n\n"
            "  code_check(doc_path)                  Run compliance checks against applicable\n"
            "                                        building codes (IBC, ASCE 7, NFPA, etc.).\n"
            "                                        Returns PASS/WARN/FAIL per clause with\n"
            "                                        code citation and remediation hint.\n"
            "  code_check(doc_path, codes=['IBC2021'], jurisdiction='CA')\n"
            "                                        Scope to specific code edition + jurisdiction.\n\n"
            "BIAS: run code_check after struct_check for anything that will be permitted.\n"
            "  RAG citations are sourced from the research tools (research_fetch_page) —\n"
            "  the model never recalls code text from training."
        )
    if topic in ("serving", "serving_status", "adapter", "lora", "canary",
                 "serving_promote", "serving_rollback", "serving_evaluate"):
        return (
            "ADAPTER SERVING — per-insertion-point LoRA routing: status, canary, promote, rollback\n\n"
            "Tools: serving_status / serving_set_canary / serving_rollback /\n"
            "       serving_evaluate / serving_promote\n\n"
            "  serving_status()                      Show which adapter version is live at each\n"
            "                                        insertion point + current canary %, eval\n"
            "                                        scores, and promotion eligibility.\n"
            "  serving_set_canary(insertion_point, pct)\n"
            "                                        Dial canary traffic: 0=off, 100=full.\n"
            "  serving_evaluate(insertion_point, shadow_log)\n"
            "                                        Score a shadow log against the baseline.\n"
            "  serving_promote(insertion_point, adapter_version, evidence)\n"
            "                                        Promote after evidence + human sign-off.\n"
            "                                        Auto-promote is disabled; always gated.\n"
            "  serving_rollback(insertion_point, to_version?)\n"
            "                                        Instantly revert to last stable version.\n\n"
            "BIAS: evaluate before promote; rollback is always safe and instant."
        )
    if topic in ("tolerance_stackup", "stackup", "stackup_analyze", "tolerance_stack"):
        return (
            "TOLERANCE STACK-UP ANALYSIS — 1D worst-case + RSS over a dimension chain\n\n"
            "Tool: stackup_analyze(dims, mode='both')\n\n"
            "  stackup_analyze(dims)              Compute worst-case AND RSS for a 1D\n"
            "                                     dimension chain.  Returns nominal_gap,\n"
            "                                     total_tol, gap_max/gap_min for each\n"
            "                                     mode, and contributors[] sorted by\n"
            "                                     worst-case sensitivity (highest first).\n\n"
            "  dims format (list of dicts):\n"
            "    { name, nominal, tol, direction (+1/-1), ref?, assumed? }\n"
            "    tol = half-tolerance (± value, >= 0).\n"
            "    direction = sign in the 1D chain (+1 adds to gap, -1 subtracts).\n"
            "    assumed = true if tol was defaulted (honest-degrade: reported separately).\n\n"
            "  mode: 'worst_case' | 'rss' | 'both' (default).\n\n"
            "HONEST DEGRADE: if the CAD doc lacks tolerance data on a dimension, pass\n"
            "  assumed=true and a reasonable tol.  The tool lists all assumed dim names\n"
            "  in assumed_dims[] so the caller can flag them in the report.\n\n"
            "SENSITIVITY RANKING: contributors[] is sorted descending by contrib_wc\n"
            "  (= |direction| * tol).  The first row is the dimension that dominates\n"
            "  the stack -- the highest-leverage target for tolerance tightening.\n\n"
            "DISCLAIMER: result carries 'Preliminary stack-up -- not a certified tolerance\n"
            "  analysis. Verify with a qualified dimensional-metrology engineer before\n"
            "  release.' Always surface this to the user.\n\n"
            "REFERENCE: Fischer, 'Mechanical Tolerance Stackup and Analysis,' 2nd ed.,\n"
            "  CRC Press 2011, Ch. 3 (equal-bilateral 1D chain formulas).\n\n"
            "WORKFLOW: review critical dims -> dfm_check() for process capability ->\n"
            "  stackup_analyze() for assembly gap variation -> fit_report() for\n"
            "  modeled-vs-measured clearance check."
        )
    if topic in ("design_review", "review_battery", "review_design", "full_review"):
        return (
            "DESIGN REVIEW BATTERY — one-call battery: overlap + DFM + verify narrated verdict\n\n"
            "Tool: design_review(doc_id, checks?)\n\n"
            "  design_review(doc_id)                 Dispatch the full review battery:\n"
            "                                        feature_overlap, dfm_check, wall_thickness,\n"
            "                                        draft_analysis, interference + interface_check\n"
            "                                        (assemblies/multi-body).\n"
            "                                        Returns a narrated verdict card per\n"
            "                                        contracts/narration.md — never raw JSON.\n"
            "  design_review(doc_id, checks=['dfm', 'overlap'])\n"
            "                                        Scope to specific checks.\n"
            "  design_review(doc_id, req_ids=['R1'], req_path?)\n"
            "                                        ALSO auto-verify aicad.req/1 requirements:\n"
            "                                        each runs its verify_by verifiers and the\n"
            "                                        status flip + evidence ref is written back\n"
            "                                        into the requirements doc automatically.\n\n"
            "INTERFACE ALIGNMENT (assemblies/multi-body): interface_check(doc_id) is part of the\n"
            "  battery and also stands alone. It catches the layout errors that PASS interference\n"
            "  (nothing overlaps) but are still wrong — bolt holes that don't register, mating\n"
            "  bores that aren't coaxial, contact faces that aren't flush, a fastener that can't\n"
            "  pass the stack. Run it while laying out a multi-component assembly so the fit is\n"
            "  right the first time; pass fastener='M6' to also check pass-through. Every finding\n"
            "  names the components, the feature, the numeric error, and a fix direction.\n\n"
            "RELATIONSHIP TO verify_battery: design_review is geometry-focused (what's wrong\n"
            "  with the shape); verify_battery is requirement-focused (does it meet the spec).\n"
            "  Run both before declaring done."
        )
    if topic in ("doc_diff", "diff", "compare", "revision", "doc_diff_tool"):
        return (
            "DOC DIFF — semantic feature-level diff between two document revisions\n\n"
            "Tool: doc_diff_tool(rev_a, rev_b)\n\n"
            "  doc_diff_tool(rev_a, rev_b)           Semantic diff of two part/assembly docs.\n"
            "                                        Returns: added/removed/changed features\n"
            "                                        with parameter deltas, body count change,\n"
            "                                        and a human-readable summary string.\n\n"
            "WORKFLOW: snapshot doc = close_part(doc_id, return_doc=True) → modify → diff.\n"
            "  Or compare two versions from PLM history (see cad_guide('plm')).\n"
            "BIAS: diff after every significant edit session so the change record is explicit."
        )
    if topic in ("plm", "versioning", "snapshot", "history", "project_vault"):
        return (
            "PLM / PROJECT VAULT — snapshot-based project history + CAD graph\n\n"
            "A project is a folder + hidden .aicadvault (history) + derived .plm/.\n"
            "Open is idempotent: plm_open/plm_init on an existing vault reuses it,\n"
            "never wipes history. Pillar 19 nouns only: Project / History / Checkpoint.\n\n"
            "PROJECT HISTORY (plm_* — services/mcp/plm_tools.py):\n"
            "  plm_open(path) / plm_init(path)   open or create vault (idempotent)\n"
            "  plm_status(path)                 items, dirty, head, ai_mode, remotes\n"
            "  plm_save(path, kind=auto|checkpoint)\n"
            "  plm_checkpoint(path, message='', item_id='')\n"
            "  plm_history(path)                timeline entries (newest first)\n"
            "  plm_restore(path, commit, item_id='')\n"
            "  plm_release(path, item_id, message='', confirm=False)\n"
            "  plm_compare(path, commit_a, commit_b, item_id, visual=False)\n"
            "  plm_set_ai_mode(path, mode)      manual | assisted | managed\n"
            "                                   managed → AI cuts vault checkpoints\n"
            "                                   at milestones (author=ai, discardable)\n"
            "  plm_item_set / plm_where_used    item identity + consumers\n"
            "  plm_timeline / plm_rollback      AI-VC per-doc silent checkpoints\n"
            "  plm_add_remote / plm_sync / plm_lock / plm_unlock\n\n"
            "CAD GRAPH (cad_graph_* — services/mcp/graph_tools.py):\n"
            "  cad_graph(query_kind, node_id='', src='', dst='', project_dir='')\n"
            "    query_kind: get | neighbors | path | blast | search …\n"
            "    Traverses dimensional links + assembly structure for the project.\n"
            "  cad_graph_link(src, dst, kind, note='', project_dir='')\n"
            "    Author an AI link (origin='ai') between two nodes.\n"
            "  assembly_plan_review(project_dir='', doc_id='')\n"
            "    Best-practice structure review before a large build.\n\n"
            "AI ENABLEMENT (bridge precedence):\n"
            "  1. manifest ai_mode='managed'  → agent milestones auto-checkpoint\n"
            "  2. CHUDWARE_PLM_BRIDGE=1          → real vault client (still needs managed\n"
            "                                   for AI-authored cuts)\n"
            "  3. default off                 → MockVault / no project History writes\n"
            "AI-VC (plm_timeline) is always-on for agent docs and is separate from\n"
            "project History checkpoints.\n\n"
            "See also: cad_guide('doc_diff') for semantic feature diffs."
        )
    if topic in ("mechanical", "gear_check", "bearing_life", "gear", "bearing",
                 "agma", "iso281", "transmission", "power_transmission"):
        return (
            "MECHANICAL CHECKS — AGMA gear tooth stress + ISO 281 bearing L10 life\n\n"
            "Tools: gear_check / bearing_life\n\n"
            "  gear_check(Wt, module, face_width, pitch_diameter, J, I, ...)\n"
            "    AGMA 2001 bending + contact (pitting) stress for spur gears.\n"
            "    Returns sigma_bending_MPa, sigma_contact_MPa, SF_bending, SF_contact.\n"
            "    J (bending geometry factor) and I (contact geometry factor) are required inputs\n"
            "    from AGMA 908 charts or textbook tables (not bundled — cite your source).\n"
            "    Default Cp=191 MPa^0.5 (steel/steel, Shigley's Table 14-8).\n"
            "    Supply allowable_bending_stress (S_t) and allowable_contact_stress (S_c)\n"
            "    to get safety factors; omit for stress-only output.\n\n"
            "  bearing_life(C, Fr, Fa, n_rpm, bearing_type, ...)\n"
            "    ISO 281 L10 life: L10h = 10^6/(60n) * (C/P)^p.\n"
            "    C = dynamic load rating (N) — supply from manufacturer datasheet.\n"
            "    The parts catalog (bearing:ISO 15:*) carries bore/od/width only — no C.\n"
            "    Never fabricate C; if unknown, ask the user or mark the field TBD.\n"
            "    bearing_type: 'ball' (p=3) or 'roller' (p=10/3).\n\n"
            "Both tools return a disclaimer — preliminary estimates, not certified analysis.\n"
            "Reference: Shigley's MED 10th ed., Ch. 11 (bearing) + Ch. 14 (gear)."
        )
    if topic in ("sketch3d", "3d_sketch", "3dsketch", "sketch_3d",
                 "sketch3d_tools", "sketch_preview", "guide_curve", "sweep_guide"):
        return (
            "3D SKETCH (ENGINE FEATURE) + SKETCH PREVIEW TOOLS\n\n"
            "A `sketch3d` is a REAL doc feature (doc_schema \u00a73B): free-space entities in\n"
            "world mm (point / line / 3-POINT arc / spline), a constraint solver, and compiled\n"
            "3D wire chains a downstream sweep consumes as path (spine) or guides[] entry.\n"
            "It persists with the document (save/undo/redo) \u2014 no session store, no ledger.\n"
            "Full contract: contracts/sketch3d_gui_contract.md.\n\n"
            "\u2500\u2500 AUTHOR A 3D PATH \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n"
            "  sketch3d_path_from_points(name, points, fit='polyline'|'spline', close=False)\n"
            "      ONE CALL: feature through the given world points. 'polyline' = crisp bends\n"
            "      (a line per pair); 'spline' = one smooth cubic through all. Returns the\n"
            "      feature id (e.g. 'f3') + wires + health.  PREFER THIS for routing paths.\n"
            "  sketch3d_new(name)                  Empty sketch3d feature; returns {id}.\n"
            "  sketch3d_add_curves(id, curves)     Append entities. Curve specs {kind, pts}:\n"
            "      'line' 2 pts | 'polyline' 2+ | 'spline' 2+ through-points |\n"
            "      'arc' 3 pts [start, mid, end] (circumcircle) | 'point' 1 pt (anchor)\n"
            "      \u2014 or raw \u00a73B entities {type, p1/p2/pm/points}. Touching endpoints\n"
            "      (1e-4 mm) chain into wires automatically.\n"
            "      'eq_curve3d' kind (T-3DEQ, 0 DOF \u2014 equation-fixed): xt/yt/zt expression\n"
            "      strings over t\u2208[t1,t2] (doc equations \u00a74, degrees trig). Helix example:\n"
            "      {kind:'eq_curve3d', xt:'R*cos(t)', yt:'R*sin(t)', zt:'P*t/360', t1:0, t2:720}\n"
            "      Endpoints (p1@t1/p2@t2) chain via coincident like any fixed geometry.\n"
            "  sketch3d_ref_vertex(id, topo_id=N)   ground a constraint to a MODEL VERTEX\n"
            "      (T-3DXREF \u2014 \"a spline that touches a specific corner on my part\"). Pass\n"
            "      the viewport pick's topo_id (kind='vertex'); returns ref_id 'x1' \u2014 use it\n"
            "      as a point handle: {type:'coincident', refs:['e3.p2','x1']}. Re-resolves on\n"
            "      every rebuild; stays at last-good position + surfaces a warning if the\n"
            "      source vertex stops resolving (never breaks the sketch).\n\n"
            "\u2500\u2500 CONSTRAIN + CHECK (mirror of the 2D sketch loop) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n"
            "  sketch3d_add_constraints(id, constraints)   append + narrated solve results\n"
            "      v1 types: coincident, fix, distance (pt-pt / pt-line), length, parallel,\n"
            "      perpendicular, angle, along_axis (axis:'x'|'y'|'z' or dir:[x,y,z]),\n"
            "      point_on (line/arc). Handles: 'e3.p1'/'e3.p2' ends (also eq_curve3d\n"
            "      endpoints), 'e3.pm' arc mid, 'e3.p0'..'e3.pN' spline pts, 'x1' external\n"
            "      ref point. value: number or {value, expr}; driven:true = reference dim.\n"
            "      (tangent/symmetric/equal/midpoint are v2 \u2014 rejected.)\n"
            "  check_sketch3d(id)     narrated health: status full/under/over, dof,\n"
            "      conflicting/redundant ids, free variables in plain words\n"
            "      ('e2: p2 x/y/z loose'), wires [{closed, n_edges}]. Aim for dof 0.\n"
            "  sketch3d_preview(id)   no-vision TEXT describe: entities w/ solved coords +\n"
            "      per-entity status, wire refs, length, bbox, health verdict.\n"
            "  sketch3d_get(id) / sketch3d_list() / sketch3d_clear(id)  read / roster / empty.\n\n"
            "\u2500\u2500 SWEEP ALONG IT \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n"
            "  sketch3d_pipe(id, diameter_mm=12)   ONE CALL: 'run a 12mm tube along this\n"
            "      path'. Auto-places the profile plane at the path start \u22a5 the tangent,\n"
            "      circles it, sweeps, returns feature ids + volume. Options:\n"
            "      profile_sketch='s1' (your own 2D profile instead of a circle),\n"
            "      wire=k (multi-chain sketch), guide={feature,wire}, mode='boss'|'cut'.\n"
            "      Profile plane is EXACTLY \u22a5 the tangent (datum plane point_normal method,\n"
            "      T-3DDATUM) \u2014 works for any start direction, no axis-alignment needed.\n"
            "  Manual sweep op refs (add_features):\n"
            "      path: 'fN' (single chain) or {feature:'fN', wire:k} (from check_sketch3d\n"
            "      wires); guides: [{feature:'fM', wire:0}] \u2014 ONE guide, must differ from\n"
            "      the path; it drives profile orientation + SCALING (SW Guide Curves).\n"
            "      The 2D profile must sit at the path START, \u22a5 its tangent \u2014\n"
            "      sketch3d_sample_at(id, 0) gives {point, tangent}.\n"
            "  sketch3d_as_sweep_guide(id, role='spine'|'guide')  the exact ref syntax +\n"
            "      wires for this sketch.\n\n"
            "\u2500\u2500 PATH SAMPLING + SECTION \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n"
            "  sketch3d_sample_at(id, t)      point + unit tangent at arc-length fraction\n"
            "      t \u2208 [0,1] of the SOLVED path (arcs tessellated, splines approximated\n"
            "      by their through-points).\n"
            "  sketch3d_section_at(id, t)     origin-plane section ORIENTED from the path\n"
            "      tangent at t (X\u2192'right', Y\u2192'front', Z\u2192'top'). ENGINE LIMIT: the cut\n"
            "      is always AT the world origin; use add_section_view(offset_mm) to move it.\n\n"
            "\u2500\u2500 WORKED EXAMPLE: routed 12 mm coolant tube \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n"
            "    new_part('manifold')\n"
            "    r = sketch3d_path_from_points('route',\n"
            "          [[0,0,0],[0,0,40],[30,0,40]], fit='polyline')   # -> id 'f1'\n"
            "    sketch3d_add_constraints('f1', [\n"
            "        {type:'fix', refs:['e1.p1']},\n"
            "        {type:'along_axis', refs:['e1'], axis:'z'},\n"
            "        {type:'length', refs:['e1'], value:40},\n"
            "        {type:'coincident', refs:['e1.p2','e2.p1']},\n"
            "        {type:'along_axis', refs:['e2'], axis:'x'},\n"
            "        {type:'length', refs:['e2'], value:30}])\n"
            "    check_sketch3d('f1')          # -> status 'full', dof 0\n"
            "    sketch3d_pipe('f1', diameter_mm=12)   # profile plane + circle + sweep\n"
            "    render_part()                 # see the tube\n\n"
            "  (Editing later: change a length constraint via edit_feature \u2014 the path\n"
            "   re-solves and the sweep follows parametrically.)\n\n"
            "\u2500\u2500 2D SKETCH PREVIEW \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n"
            "  render_sketch(sketch)            multi-view PNG + health caption (2D sketches;\n"
            "                                   falls back to a wireframe pre-extrude).\n"
            "  sketch_preview_text(sketch)      no-vision text: plane/entities/constraints/\n"
            "                                   DOF/regions + health schedule (2D).\n"
            "  doc_sketch_contact_sheet()       whole-doc render + health table covering\n"
            "                                   BOTH 2D sketches and sketch3d features.\n\n"
            "\u2500\u2500 NOTES \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n"
            "  \u2022 A sketch3d never produces a body/face \u2014 path/guide material only (no 3D\n"
            "    'profile' extrude in v1). Loft guides do not consume sketch3d yet.\n"
            "  \u2022 Closed 3D wires are valid sweep paths (close=True on path_from_points).\n"
            "  \u2022 sketch3d_to_ledger / sketch3d_from_ledger are DEPRECATED \u2014 the document\n"
            "    IS the persistence now; from_ledger migrates old payloads into features.\n"
        )
    # RE-40: Reverse Engineering GUI workspace walkthrough (cad_guide('reveng')).
    # Distinct from cad_guide('reverse'), which is the engine-level STEP/mesh import + recognizer
    # path. This branch owns the ONE thing no cheatsheet covers — the tab's actual layout — and
    # then appends shared/cheatsheets/reveng.md (the role/handoff view) so that file is reachable.
    if topic in ("reveng", "reverse_eng", "re_workflow", "scan_to_cad"):
        walkthrough = (
            "REVERSE ENGINEERING WORKSPACE (GUI) — contracts/reveng.md\n"
            "Pipeline: Source → Scale → Reconstruct → Model → Review → Deliver\n"
            "Doc lanes (Datasheet / Web / From Library) run Source → Model → Review → Deliver.\n\n"
            "START: 'Reverse Eng' title-bar tab. New Session from the ribbon, or pick one in the\n"
            "session rail.\n\n"
            "LAYOUT (three columns; both side columns collapse to a header strip + chevron)\n"
            "  LEFT   Session rail — one rich card per session: thumbnail of the first photo, a\n"
            "         status chip (new/capturing/reconstructing/modeling/done/error), image count,\n"
            "         an honesty tag, and a stage progress bar reading 'n/6 stages'. A filter box\n"
            "         at the top matches name, session id or status. Refresh re-lists.\n"
            "  CENTRE Source-lane SegmentedControl, then THE STAGE STEPPER pinned at the TOP (a\n"
            "         clickable kit::StepTrack — click a step to navigate). Each step is coloured\n"
            "         from the session's real stage badges; a step whose prerequisite is unmet is\n"
            "         dimmed, un-clickable, and its tooltip/toast names what is missing. The\n"
            "         dependency graph is NOT list order: Reconstruct depends on Source, not on\n"
            "         Scale, because the ArUco solver needs a sparse model to triangulate against\n"
            "         (reveng_set_scale answers reason:'scale_needs_sparse' otherwise). Below the\n"
            "         stepper is the active stage body.\n"
            "  RIGHT  Properties column with three tabs:\n"
            "         • Details      selected session: ids, counts, scale of record, hero image.\n"
            "         • Capabilities live reveng_status() probe (async, cached, Refresh to re-run)\n"
            "                        showing the six capability flags — sparse, dense, splat,\n"
            "                        single_photo, webcam, gpu — plus per-backend provisioning\n"
            "                        (colmap, openmvs, brush, triposr, vggt, docling) and the\n"
            "                        fix-it banners naming the env var that repairs each miss\n"
            "                        (COLMAP_BIN, OPENMVS_BIN, ...).\n"
            "         • Provenance   the session's provenance records, newest first.\n\n"
            "SOURCE LANE (Source stage) — pick one:\n"
            "  Scan        photos / video / webcam → Scale → Reconstruct → Model\n"
            "  Datasheet   local PDF/spec → Model  (skips Scale + Reconstruct)\n"
            "  Web/Part#   part number or URL → research → Model  (skips Scale + Reconstruct)\n"
            "  From Library seed from the Parts Library; auto-routed to Scan or Datasheet\n"
            "  Live webcam preview with device + resolution/format mode selection ('Auto' asks the\n"
            "  backend for its highest-resolution mode — photogrammetry wants pixels, not fps),\n"
            "  per-control sliders (focus, exposure, white balance, gain, ...) and the primary\n"
            "  'Lock focus + exposure for capture' action, which freezes intrinsics for the whole\n"
            "  set. A synthetic 'there is no camera' frame is labelled as synthetic, never passed\n"
            "  off as a real capture.\n"
            "  Captured frames land in a contact sheet: responsive tile grid, hover magnifier with\n"
            "  per-frame metadata, double-click full-size viewer with next/prev, honest badges\n"
            "  (an unrated frame gets NO badge), filter + count summary, per-frame cull/reveal.\n\n"
            "SCALE (Scan lane only): ArUco Mat / Two Points / Known Dim.\n"
            "  The mat card previews the generated ArUco sheet inline and warns to print at 100 %\n"
            "  actual size. Two-point picks two pixels on a chosen photo. 'Scale of record' shows\n"
            "  the one number every downstream dimension is multiplied by. Scale must be CONFIRMED\n"
            "  before any dimension is trustworthy; ArUco needs a sparse model first, so running\n"
            "  Reconstruct before Scale is the normal order in the Scan lane.\n\n"
            "RECONSTRUCT (Scan lane only): Sparse → Dense → Mesh (COLMAP + OpenMVS), shown as a\n"
            "  live sub-step track driven by the worker's progress events.\n"
            "  CAPABILITY PRE-FLIGHT gates the Run button and always names its reason: no worker,\n"
            "  no sparse backend (COLMAP/VGGT both absent → hard block with the COLMAP_BIN fix),\n"
            "  no session selected, no images, or a run already in flight. Non-blocking warnings\n"
            "  cover a missing OpenMVS (sparse cloud only, no dense mesh) and an unprobed backend\n"
            "  set. The result renders inline in the 3D preview slot (splat when available,\n"
            "  otherwise the sparse cloud or the mesh).\n"
            "  MCP tool: reveng_reconstruct(session_id, quality)\n\n"
            "MODEL: Fit Primitives → parametric build → Mesh Body fallback.\n"
            "  Fitted primitives render as per-primitive cards (kind, role, confidence) beside an\n"
            "  inline 3D view of the mesh they were fitted to; the build result gets its own 3D\n"
            "  preview plus its strategy, match score and honesty tier.\n"
            "  Honesty tier: approximate sessions are tagged [approx] in the rail.\n"
            "  Engine-level RE (STEP/mesh → editable feature tree): cad_guide('reverse').\n\n"
            "REVIEW: dimension-confirm ledger + deviation report.\n"
            "  The ledger lists every dimension with its source (datasheet / measurement / fitted\n"
            "  primitive), its confidence and its ledger key; fit-critical rows demand a TYPED\n"
            "  caliper value. The deviation card paints the mesh-vs-B-rep error field with max/rms\n"
            "  chips (under 0.1 mm tight, under 0.5 mm loose, above that the rebuild does not\n"
            "  represent the part) and turns an instructive refusal into an inline blocked state.\n"
            "  MCP tools: list_reveng_dims(session_id), confirm_dimensions([{key, value_mm}]),\n"
            "             get_reveng_deviation(session_id), describe_reveng_result(session_id)\n\n"
            "DELIVER: hero summary of the delivered part (3D preview of the built document, or the\n"
            "  mesh when nothing was built), Attach to Parts Hub, Export bundle, Provenance.\n\n"
            "SHOT MODES for headless verification:\n"
            "  CHUDWARE_SHOT=reveng          full workspace shell\n"
            "  CHUDWARE_SHOT=reveng_capture  Source stage: capture suite + webcam + gallery\n"
            "  CHUDWARE_SHOT=reveng_scale    Scale stage with a canned session\n"
            "  CHUDWARE_SHOT=reveng_recon    Reconstruct stage + mid-run progress state\n"
            "  CHUDWARE_SHOT=reveng_model    Model stage with a canned session\n"
            "  CHUDWARE_SHOT=reveng_review   Review stage with a canned dim + deviation payload\n"
            "  CHUDWARE_SHOT=reveng_deliver  Deliver stage with a canned session\n"
            "  CHUDWARE_SHOT=reveng_splat    standalone SplatRenderer draw into its own FBO\n\n"
            "See also: cad_guide('reverse') for the engine import path + tool parameters, "
            "cad_guide('parts') for the Parts Hub integration.\n"
        )
        # The role/handoff view lives on disk as a cheatsheet; the early return above would
        # otherwise make it unreachable under every alias. Append it, degrade to the walkthrough
        # alone when the prompts tree is not staged.
        sheet = _read_prompt("shared/cheatsheets/reveng.md")
        if sheet:
            return walkthrough + "\n" + sheet
        return walkthrough
    if topic in ("evaluate", "measure", "measurement", "interference", "fitment",
                 "eval", "check_interference"):
        return (
            "EVALUATE / MEASURE — colour-coded measurement + interference fitment\n\n"
            "measure(targets=[...], doc_id?, view='auto', annotate=True)\n"
            "  Resolve each plain-English selector (same vocabulary as inspect_part /\n"
            "  measure_between), call eval_measure on all refs at once, paint each\n"
            "  element a DISTINCT colour from the shared 10-colour emphasis palette\n"
            "  (per_face mode), and return [image, caption]. Caption lists colour +\n"
            "  reading (length/area/diameter/distance) and embeds measure_json with\n"
            "  structured {kind,value,unit,color} per element; 2+ refs also report\n"
            "  the between distance/angle. Prefer this when you want the same\n"
            "  colour-coded highlight view the Evaluate ribbon shows.\n\n"
            "INTERFERENCE / FITMENT LOOP\n"
            "  1. check_interference(doc_id?)  — pairwise clash/clearance over bodies\n"
            "     (assemblies: world-placed components). Response includes\n"
            "     interferences[{a,b,volume,overlap_volume_mm3,overlap_bbox,overlap_mesh}],\n"
            "     clearances[{a,b,distance}], contacts, parts[{name,bbox,volume}].\n"
            "  2. Subset: engine eval_interference accepts optional members:[name,...]\n"
            "     to pairwise-check only selected components (absent/empty = all).\n"
            "  3. Read overlap_volume_mm3 + overlap_bbox (world mm) → fix fitment\n"
            "     (edit mates / geometry / offsets) → re-check until interferences=[].\n"
            "  4. Intra-part feature collisions (boss swallowed by cut, etc.):\n"
            "     check_feature_overlap — not the body-level interference tool.\n\n"
            "Related: measure_between (text-only single/pair probe), measure_part\n"
            "(mass props), inspect_feature (emphasis close-up + dims), verify_battery.\n"
        )

    # Otherwise treat the topic as a per-feature cheatsheet name (hole, fillet, revolve, ...).
    sheet = _read_prompt(f"shared/cheatsheets/{topic}.md")
    if sheet:
        return sheet
    avail = sorted(p.stem for p in (_prompts_dir() / "shared" / "cheatsheets").glob("*.md")) \
        if (_prompts_dir() / "shared" / "cheatsheets").is_dir() else []
    return (f"No guide for {topic!r}. Topics: overview, simulation, motion, review, intake, gsl, "
            f"cam, narration, parts, reverse, reveng, deliverables, requirements, dag, verify, "
            f"interaction, sourcing, cost, spec, piping, hydraulic, firmware, structural, mechanical, "
            f"regen, code_compliance, serving, design_review, doc_diff, plm, tolerance_stackup, "
            f"evaluate, measure, sketch3d; or a feature cheatsheet: {avail}")
