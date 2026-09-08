# Simulation setup context pack
# Tools: run_fea_*, run_cfd_flow/accurate, sim_run/status/results, simulate_motion,
#        em_status/eigenmode/driven/timedomain, query_geometry, describe_results

## Before launch
1. State one criterion/case/pass rule and all units. FEA properties enter through
   the solve's `material=` argument; `set_material` stores document identity and
   appearance but does not silently choose solver properties. Explicit FEA dicts need
   E/nu; add yield for static/fatigue, density for modal/frequency/transient thermal,
   ultimate/endurance for fatigue, conductivity_w_mk/k_w_mk for steady thermal, and
   conductivity plus cp_j_kgk (or top-level specific_heat_j_kgk) for transient.
   Registry material names resolve their stored properties. Confirm every face query.
2. Anchor before solving, or record `no anchor`. Force is N, pressure MPa, geometry
   mm. Never turn pressure into force without measured area.
3. Static/fatigue/buckling need mounted faces and non-zero force N or pressure MPa.
   Buckling uses real tet10 (size_mm=8); max_load_factor=10000 bounds its search.
   No mode below that ceiling is a refusal, not infinite strength.
   Modal takes no load; `fixtures=[]` is intentional free-free with six rigid modes.
4. Steady and transient thermal use the real mesh with °C temperature faces,
   W/m² flux and W/(m²·K) convection. Steady needs a temperature/convection datum;
   schedules require transient. Flux-only transient heat-up is valid.
5. CFD uses m/s and air|water; LBM is trends only. Motion uses degrees/mm and
   `stop`+`steps>=2`. EM uses GHz/mm (antenna APIs use Hz) and needs `em_status`
   before openEMS.

## Useful independent anchors
- Static: beam stress/deflection only for a justified support/load path. Modal:
  beam first-mode order and separation from the named excitation.
- Thermal: 1-D Fourier flux/mid-plane temperature. Vibration: resonance at modal
  Hz and SDOF/Miles order only with stated damping/PSD.
- CFD: dynamic pressure and V² force scaling, not an invented absolute Cd. Motion:
  endpoint/loop closure, clearance, and force/reaction balance.
- EM: rectangular-guide TE10 cutoff or antenna closed-form resonance; analytic-
  uniform S21 cannot validate a discontinuity.

## Evidence
- Run a cheap discretization and a finer one; report mesh/cells/resolution and
  require the governing result to settle (normally <10%) or match an independent
  analytic anchor. A single favorable mesh is not confidence.
- Treat `ok:false`, SKIP, failed/cancelled, `converged:false`, missing PSD/verdict,
  or unavailable backend as no result.
- Static margin is `yield/SF - stress`. Missing required margin is not nominal
  yielding; yielding begins at yield and fracture language requires ultimate.
