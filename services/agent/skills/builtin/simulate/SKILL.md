---
name: simulate
description: Run FEA, thermal, vibration, CFD, motion, and EM studies with actual setup units, backend checks, convergence, and analytic anchors.
inject: auto
triggers:
  keywords: [fea, cfd, simulate, simulation, stress, modal, thermal, vibration, wind tunnel, drag, motion, eigenmode, s-parameter, antenna, contact, load case, strong enough, will this hold, can this hold, factor of safety, deflection, buckling, von mises]
  tools: [run_fea_static, run_fea_modal, run_fea_fatigue, run_fea_frequency_response, run_fea_thermal, run_fea_buckling, run_fea_contact, run_cfd_flow, run_cfd_accurate, simulate_motion, em_eigenmode, em_driven, em_timedomain, ecad_antenna_design, ecad_antenna_analyze]
  phases: [sim]
---

# Simulate

## Execute

1. State one criterion, case, pass rule, and units. Use each FEA tool's `material=`;
   `set_material` changes appearance only. Confirm faces with `query_geometry`.
   Every explicit FEA material dict needs `E_mpa` and `nu`; add `yield_mpa` for
   static/fatigue, density for modal/frequency/transient thermal,
   `ultimate_mpa`/`endurance_mpa` for fatigue, `conductivity_w_mk`/`k_w_mk` for
   steady thermal, and conductivity plus `cp_j_kgk` (or top-level
   `specific_heat_j_kgk`) for transient thermal. Registry material names resolve
   their stored properties.
2. Record an independent closed-form anchor before solving, or explicitly record
   `no anchor`. Never convert pressure to force without loaded area.
   Useful anchors: justified beam stress/mode order, Fourier heat flow, vibration
   resonance at modal Hz, CFD dynamic pressure/V², motion closure/clearance, and
   EM TE10 cutoff/antenna resonance. Analytic-uniform S21 proves no discontinuity.
3. Run a cheap discretization, then a finer one with identical setup. Report both
   discretizations and require the governing value to settle (normally <10% change)
   or remain consistent with the independent anchor. One favorable mesh is not PASS.
4. Read the envelope literally: `ok:false`, `skipped:true`, failed/cancelled,
   `converged:false`, `random.ok:false`, or `verdict:null` proves nothing.

## Current surfaces

- **Static/fatigue:** mounted face plus non-zero force in N or pressure in MPa;
  these are real-part FEA. **Buckling:** exactly one mount and one inward axial force
  in N; pressure/shear/tension cannot map to its CalculiX fixed-free box proxy.
- **Contact:** `run_fea_contact` is a CalculiX two-block load-transfer proxy, not the
  document's actual contact pair. Check base reaction against applied load.
- **Modal/vibration:** modal takes no load; `fixtures=[]` is legitimate free-free and
  should show six near-zero rigid modes. Frequency response uses `excitation_dir`,
  `base_accel_g`, damping `zeta`, and optional PSD in g²/Hz; it returns acceleration
  amplification, not stress or displacement.
- **Thermal:** steady `run_fea_thermal` is a CalculiX 1-D box proxy requiring
  exactly one `hot_faces` and one `cold_faces` entry with distinct `temperature`
  values in °C; it rejects loads. `transient={t_end_s,...}` uses the real mesh and
  accepts `flux_w_m2` or `h_w_m2k`/`t_amb_c`; flux-only transient heat-up is valid.
  State k W/(m·K), cp J/(kg·K), and time seconds.
- **CFD:** `run_cfd_flow` is directional LBM; require `converged=true` and
  `min_feature_voxels>=~8`, then refine `cells`. Absolute Cd/Cl needs
  `run_cfd_accurate`; unavailable OpenFOAM returns SKIP and started runs use `get_job`.
- **Motion:** `simulate_motion` needs one dimensional mate, required `stop`, degrees
  or mm, and `steps>=2`. Refine samples near first contact. `dynamics={}` adds inverse
  reactions but no contact dynamics.
- **EM:** call `em_status` before `solver='openems'`. Builtin FDFD eigenmodes and
  analytic-uniform guide responses are not full-wave evidence. Track GHz versus Hz
  and mm; refine resolution/frequency samples and compare cutoff/resonance anchors.

## Verdicts

Static margin is `yield/SF - stress`. Exceeding that allowable means required margin
is not met; it does not itself mean yielding. Stress at/above yield supports yielding,
and fracture language requires stress at/above ultimate. Name convergence/anchor
evidence for PASS and the proxy/backend/linear-model limits for every verdict.
