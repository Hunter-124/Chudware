SIMULATION — define one case, check capability, anchor, refine, then interpret.

REAL-PART:
  run_fea_static/fatigue/modal/frequency_response/thermal/buckling/contact;
  run_cfd_flow; simulate_motion.
ANALYTIC/REDUCED:
  EM defaults are builtin FDFD eigenmode or analytic-uniform guide response.

SETUP:
  - Confirm face queries. FEA properties enter via material=; set_material is
    appearance only. Explicit dicts need E/nu; add yield for static/fatigue, density
    for modal/frequency/transient thermal, ultimate/endurance for fatigue,
    conductivity_w_mk/k_w_mk for steady thermal, and conductivity plus cp_j_kgk (or
    top-level specific_heat_j_kgk) for transient. Force N, pressure MPa, geometry mm.
  - Static/fatigue: mounted face + non-zero force N or pressure MPa. Buckling:
    one mount + one inward axial force N; its proxy rejects pressure/shear/tension.
    Modal: no load; fixtures=[] is valid free-free (expect six near-zero rigid modes).
  - Vibration: excitation_dir, base_accel_g, zeta; PSD is g²/Hz. No PSD means no
    random result; the tool does not recover stress/deflection.
  - Thermal steady: exactly one hot and one cold face at distinct °C values; loads
    are rejected. Transient: seconds plus W/m² flux or W/(m²·K) convection;
    flux-only transient heat-up is valid.
  - CFD: m/s, air|water, +X flow/+Z lift. Motion: stop required, degrees/mm,
    steps>=2. EM: em_status before openEMS; guide tools use GHz/mm, antennas Hz.

ANCHORS:
  beam only for justified support/load; Fourier q=kΔT/L; modal/resonance order;
  CFD dynamic pressure/V²; motion closure/clearance; EM TE10/antenna resonance.
  Analytic-uniform S21 does not validate a discontinuity.

EVIDENCE:
  - Refine mesh/cells/resolution and require the governing result to settle
    (normally <10%) or agree in order with an independent pre-solve anchor.
  - LBM absolute Cd is uncalibrated; use run_cfd_accurate for absolute Cd/Cl.
  - ok:false, SKIP, failed/cancelled, converged:false, verdict:null = no proof.
  - Static margin = yield/SF - stress. Below allowable is margin failure, not
    yielding; yielding starts at yield, fracture language at ultimate.
