# SIM_AUDIT.md — T-V1 Simulation Backend Audit

**Date:** 2026-07-02  
**Auditor:** sonnet-lyra  
**Branch:** task/T-V1-sim-backend-audit  
**Script:** `scripts/sim_audit.py` (re-runnable; 8 checks executed, 23 skipped with reason)

---
## Full-suite follow-up — 2026-08-07

The production C++ core, Python simulation engines, MCP workflows, and desktop setup/results
surfaces were audited together rather than source-inspected in isolation. The live audit now
executes 42 of 43 checks with zero broken findings. openEMS/CSXCAD is installed and its real
WR-90 solve passes the EM worker suite; the normal audit intentionally skips that comparatively
heavy full-wave run after validating its plan. The provisioned OpenFOAM v2412 path was exercised,
not merely probed: the coarse sphere case converged at $Re=10{,}000$ with $C_d=0.329$ inside the
accepted steady-RANS band $[0.25, 0.55]$.

Key corrections include: physically valid FEA setup/load/constraint handling and tet10 result
accounting; thermal, modal, fatigue, assembly, hydraulic, piping, SPICE, ECAD, CHT, motion, and
firmware workflow completion; deterministic cancellation/cache/error contracts; and live result
validation instead of fabricated or empty success envelopes. The LBM path now samples momentum
exchange on the solid destination before bounce-back, uses a window-mean convergence estimator,
stops only on positive passive-body drag plus mass continuity, and applies a disclosed
$\tau=0.55$ stability floor. It reports both physical and effective lattice Reynolds numbers and
invalidates older cached results. OpenFOAM's current flat `force.dat` format is accepted alongside
the legacy nested-vector format.

The desktop flow workflow now exports an actual `.stl`, reports numeric-only LBM results without
fake plot/temperature fields, preserves the editable model when no contour exists, and displays
the reduced-Re limitation prominently. The motion smoke now creates both components in one
revision-safe transaction, grounds the base, drives a resolved distance mate through 24
collision-free frames, and visibly advances playback. Headless `flow`, `fea`, and `motion`
scenarios completed against the freshly built in-process engine.

Verification at this checkpoint:

- `services/sim`: **168 passed, 5 skipped** (the skips are optional external capabilities).
- End-to-end MCP simulation workflows: **173 passed**.
- EM adapter/worker suite: **28 passed**, including the installed openEMS full-wave solve.
- Focused native simulation targets: **15/15 passed**.
- `scripts/sim_audit.py`: **43 checks, 42 executed, 1 intentionally skipped, 0 broken** with
  live OpenFOAM.

Re-run the live audit from the repository root:

```bash
CHUDWARE_ENGINE_DAEMON=0 \
CHUDWARE_LIBCHUDWARE="$PWD/core/build-occt-linux/bin/libchudware.so" \
CHUDWARE_SIM_AUDIT_LIVE_FOAM=1 \
.venv-mcp/bin/python -X utf8 scripts/sim_audit.py
```

The detailed 2026-07-02 tables below are retained as historical baseline; their provisioning and
raw-bridge limitations are superseded by this follow-up.

---


## Original Executive Summary (2026-07-02)

| Verdict | Count | Tools |
|---------|-------|-------|
| VALIDATED | 7 | hydraulic_size_cylinder/pump, ecad impedance ×3, ecad_simulate ac+op |
| REAL | 12 | FEA static/modal, CFD LBM, piping solver (source), struct_analyze (source), ecad_tf, schema tools |
| BROKEN (provisioning) | 3 | piping_internal_volume, piping_cfd_handoff, piping_verify_cfd |
| SKIP (dep missing) | 5 | piping live run (fluids), OpenFOAM, PyNite, ngspice |

**No solver returns physically wrong numbers. Every BROKEN finding is a missing OS/Python package.**

---

## Per-Tool Verdict Table

### Hydraulic

| Tool | Verdict | Evidence |
|------|---------|----------|
| `hydraulic_size_cylinder` | **VALIDATED** | bore=50mm, P=200bar → F=37.306 kN; independent: A·P·η = π/4·(0.05)²·200e5·0.95 = 37.306 kN; err=0.00% |
| `hydraulic_size_pump` | **VALIDATED** | Q=20 L/min, ΔP=200bar, N=1450 rpm → disp=13.793 cc/rev; independent: Q×1000/N = 13.793; shaft_pwr=7.843 kW; err=0.00% |
| `hydraulic_new` | REAL | Schema factory only (`engine.hydraulic.schema`), no physics |
| `hydraulic_add_component` | REAL | Schema mutation only |
| `hydraulic_add_hose` | REAL | Schema mutation only |
| `hydraulic_bom` | REAL | BOM aggregation from schema |

**Independent check (first principles, never read from engine):**
- F_extend = A_bore × P × η = π/4 × (bore_mm × 1e-3)² × pressure_bar × 1e5 × efficiency
- displacement_cc_rev = Q_lpm × 1000 / speed_rpm
- shaft_power_kW = (Q_m3s × ΔP_pa) / (1000 × efficiency)

### Piping

| Tool | Verdict | Evidence |
|------|---------|----------|
| `piping_solve` | REAL (source) | `engine/piping/solve.py`: Darcy-Weisbach + Colebrook friction, nodal MNA-analogue solver, K-factor fitting losses, gravity via head H = z + p/ρg. Runtime requires `fluids` (MIT) — graceful degradation on import failure confirmed |
| `piping_operating_point` | REAL (source) | `solve.py:operating_point` — pump curve intersection + NPSH margin |
| `piping_report` | REAL (source) | `solve.py:pressure_drop_report` — K-factor breakdown per segment |
| `piping_fluid` | REAL | CoolProp/thermo if available, else built-in table; degradation confirmed |
| `piping_internal_volume` | **BROKEN** | Requires `build123d`/OCP kernel — not installed in mcp-venv |
| `piping_cfd_handoff` | **BROKEN** | Requires `build123d`/OCP for bore extraction |
| `piping_verify_cfd` | **BROKEN** | Requires `build123d`/OCP for 1D vs 3D comparison |

**Live piping_solve skipped** because `fluids` (MIT) not in mcp-venv. Once installed, the Darcy-Weisbach solver should pass a live check (code implements real physics).

**Independent Darcy-Weisbach reference** (would validate against):
- h_f = f × (L/D) × v²/(2g); Q = π/4 × D² × v
- For dH=10m, L=100m, DN50 (D≈0.0525m), f≈0.02: v≈2.2 m/s, Q≈15 m³/h

### ECAD Impedance

| Tool | Verdict | Evidence |
|------|---------|----------|
| `ecad_check_impedance` (microstrip) | **VALIDATED** | w=3mm, h=1.6mm, t=0.035mm, er=4.5 → Z0=48.974 Ω; IPC-2141A: 87/√(er+1.41)×ln(5.98h/(0.8w+t)) = 48.974 Ω; err=0.000% |
| `ecad_check_impedance` (stripline) | **VALIDATED** | w=3mm, h=3.2mm → Z0=25.887 Ω; IPC-2141A: 60/√er × ln(4h/(0.67π(0.8w+t))) = 25.887 Ω; err=0.000% |
| `ecad_check_diff_impedance` | **VALIDATED** | s=0.2mm → Z_diff=56.249 Ω; coupling formula: 2×Z0_se×(1−0.48×exp(−0.96s/h)) = 56.249 Ω; err=0.000% |

All three use IPC-2141A closed-form formulas. Accuracy ±5% vs field solver (quasi-static approximation).

### ECAD Circuit Simulation

| Tool | Verdict | Evidence |
|------|---------|----------|
| `ecad_simulate` (AC) | **VALIDATED** | RC LP (R=1kΩ, C=159.155nF, fc=1kHz): \|H(fc)\|=0.70711; expected 1/√2=0.70711; err=0.00% |
| `ecad_simulate` (DC op) | **VALIDATED** | R-divider 10V, R1=1kΩ, R2=2kΩ: V(OUT)=6.66667V; expected 10×2k/3k=6.66667V; err=0.0000% |
| `ecad_tf` | REAL | Wraps `engine.sim.circuit.run_tf` — same MNA solver; validated through AC/op gates |
| `ecad_simulate` (ngspice) | SKIP | ngspice not on PATH; install: `sudo dnf install ngspice` (GPL2; subprocess-only) |

**Independent check (RC low-pass, first principles):**
- fc = 1/(2πRC); at f=fc: |H(jωc)| = 1/√(1+(ωcRC)²) = 1/√2

### FEA Static + Modal (C++ core)

| Tool | Verdict | Evidence |
|------|---------|----------|
| `run_fea_static` | REAL | `cad_tools.py` L2003-2105: exports STEP, resolves face points, calls C++ `sim_run_static` (tet10 mesh). Geometry pre-step verified live: box built OK, STEP exported, 53 face points resolved. **sim_run_static is a C++ simulation plugin not exposed on raw JSON-RPC bridge** — only callable through the full MCP server process |
| `run_fea_modal` | REAL (source) | `cad_tools.py` L2107-2167: same C++ FEA kernel, calls `sim_run_modal`. Prismatic cantilever first-mode beam theory: f₁=(1.8751²/2πL²)√(EI/ρA) ≈ 40.8 Hz for 50×50mm steel, L=1m |

**Independent cantilever reference (Euler-Bernoulli):**
- δ = PL³/(3EI); I = bh³/12 = 50⁴/12 = 520833 mm⁴
- For L=1000mm, P=1000N, E=200000 MPa: δ = 3.200 mm

**Key finding:** `sim_run_static`/`sim_run_modal` are C++ plugins that register within the MCP server process. The raw `core_bridge.py` JSON-RPC bridge only exposes geometry methods. FEA can only be executed via the full `services/mcp/cad_tools.py` MCP server.

### CFD

| Tool | Verdict | Evidence |
|------|---------|----------|
| `run_cfd_flow` (LBM) | REAL | `cad_tools.py` L2170-2241: calls `cb.call("sim_run_flow")`. C++ LBM lattice-Boltzmann wind tunnel. Engine docstring explicitly states: "ABSOLUTE Cd IS UNCALIBRATED — coarse voxel LBM staircased surface over-counts force". REAL physics, qualitative trends correct (drag ∝ V², Cd velocity-independent), absolute values unvalidated by design |
| `run_cfd_accurate` (OpenFOAM) | SKIP | OpenFOAM not installed; `foam_runner.capabilities()` → `{'openfoam': False}`. Install: `python -m tools.provision_openfoam --install` (GPL2; subprocess isolation) |

**LBM verdict: REAL, not VALIDATED.** The engine itself documents the calibration limitation. For absolute Cd/Cl, OpenFOAM RANS (k-omega SST) is the validated path per the docstring ("sphere/cube within ~10-15% of experiment").

### Structural

| Tool | Verdict | Evidence |
|------|---------|----------|
| `struct_analyze` | REAL (source) | `engine/structural/analysis.py`: StructureDoc → PyNite FE model → member forces/deflections under ASCE 7 load combos. N·mm·MPa unit system. Node topology from placed extrusion transforms. Requires PyNite (MIT) — not installed in mcp-venv |
| `struct_check` | REAL (source) | `engine/structural/checks.py`: LRFD D/C ratio checks — dc_ratio_axial, dc_ratio_bending (steel + wood). Pure math using analysis results |

---

## Provisioning Gaps

### Missing: `fluids` (MIT) — blocks piping live execution
```bash
~/.local/share/Chudware/mcp-venv/bin/pip install fluids
```
Verdict change: `piping_solve` would upgrade REAL → VALIDATED once installed.

### Missing: `PyNite` (MIT) — blocks struct_analyze
```bash
~/.local/share/Chudware/mcp-venv/bin/pip install PyNite
```
Verdict change: `struct_analyze` would upgrade REAL → VALIDATED (live cantilever check included in audit script).

### Missing: `build123d` / OCP kernel — blocks piping 3D tools
```bash
~/.local/share/Chudware/mcp-venv/bin/pip install build123d
```
License: Apache 2.0. Enables `piping_internal_volume`, `piping_cfd_handoff`, `piping_verify_cfd`.  
Verdict change: BROKEN → REAL (if code works) → VALIDATED.

### Missing: `ngspice` — blocks SPICE netlist pass-through
```bash
sudo dnf install ngspice   # GPL2 — subprocess isolation only, never import
```
The internal MNA solver (`engine.sim.circuit`) is validated. ngspice adds a second pass-through path for externally generated SPICE decks.

### Missing: OpenFOAM — blocks run_cfd_accurate
```bash
python -m tools.provision_openfoam --install   # GPL2 — subprocess isolation only
```
The LBM (`run_cfd_flow`) is available. OpenFOAM adds validated absolute Cd/Cl capability.

### Missing: `CoolProp` / `thermo` — piping_fluid degrades to built-in table
```bash
~/.local/share/Chudware/mcp-venv/bin/pip install CoolProp   # MIT
```
Not critical — built-in table covers water and common fluids.

---

## Gap → Proposed-Task Table

| Gap | Impact | OSS Recommendation | License | Priority |
|-----|--------|-------------------|---------|----------|
| FEA validation gap (tet10 vs beam theory) | Cannot cross-check absolute deflection via bridge | Run full MCP server + test harness | Internal | Medium |
| `fluids` not installed | piping_solve falls back to stub response | `pip install fluids` (MIT) | MIT, in-process OK | High — 1-line fix |
| `PyNite` not installed | struct_analyze always fails | `pip install PyNite` (MIT) | MIT, in-process OK | High — 1-line fix |
| `build123d` not installed | piping 3D CFD tools broken | `pip install build123d` (Apache 2.0) | Apache, in-process OK | Medium |
| `ngspice` not on PATH | SPICE pass-through missing | `sudo dnf install ngspice` | GPL2, subprocess-only | Low |
| OpenFOAM not installed | No validated absolute Cd/Cl | `provision_openfoam --install` | GPL2, subprocess-only | Low (LBM covers trends) |
| LBM Cd uncalibrated | Absolute drag wrong by design | Validated against sphere/cube benchmark once OpenFOAM available | n/a | Medium |
| `core_bridge` sim methods | sim_run_static not on raw bridge | Wire FEA validation into full MCP integration test | Internal | Medium |

---

## Key Architectural Finding

`sim_run_static` and `sim_run_modal` are **C++ simulation plugins** registered inside the MCP server process (`services/mcp/cad_tools.py`). They are **not accessible via the `core_bridge.py` JSON-RPC bridge** (`unknown method: sim_run_static`). This means:

1. FEA and LBM CFD can only be invoked through the full MCP server, not standalone Python scripts.
2. Integration tests for FEA must spin up the server or mock the cb.call layer.
3. The raw bridge only exposes core geometry/modelling methods (doc_new, doc_apply_ops, doc_query, etc.).

---

## Re-Run Instructions

```bash
cd /home/hunter/Repositories/CAD-T-V1
CHUDWARE_LIBCHUDWARE=/home/hunter/Repositories/CAD/core/build-linux/bin/libchudware.so \
    ~/.local/share/Chudware/mcp-venv/bin/python scripts/sim_audit.py
```

Expected: 8 executed, 3 BROKEN (all provisioning gaps), 0 wrong-physics failures.

---

## Post-provisioning update (foreman, 2026-07-03)

Acted on the gap table the same night. `fluids==1.3.1` + `build123d==0.10.0`
installed into mcp-venv (both were already declared in requirements.txt +
licenses.md — the PyNite pattern again: declared, never provisioned); `ngspice`
installed via dnf (GPL2, subprocess-only per the standing policy).

Also fixed four bugs in THIS audit script that the live runs exposed
(wrong-case `PyNite` imports ×2 — PyNiteFEA installs module `Pynite`; hardcoded
instead of probed build123d verdicts; Pynite local-axis mix-up in the cantilever
hand-calc; wrong link-flow key + malformed boundary format in the piping check).
The PyNite-in-mcp-venv "not installed" rows in the original table were the
audit's own probe bug — the package was present.

Re-run verdict: **PASS — all executed checks green.**
- `piping_solve` now VALIDATED LIVE: Q=17.05 m³/h vs 17.69 Darcy-Weisbach rough
  estimate (dH=10 m, L=100 m, DN50).
- `struct_analyze` now VALIDATED LIVE: cantilever δ=6.7500 mm vs Euler-Bernoulli
  6.7500 mm (0.00%).
- `piping_internal_volume` / `piping_cfd_handoff` / `piping_verify_cfd`:
  BROKEN → REAL (build123d/OCP imports clean).
- `ecad_simulate [ngspice]`: subprocess path now present.
- Remaining SKIP: OpenFOAM (`run_cfd_accurate`) — heavyweight install, deferred.

---

## T-SIM4 re-run (2026-07-05) — live FEA validation depth

`scripts/sim_audit.py` extended: the FEA family now solves LIVE on the raw
bridge, plus two new families — fatigue (S-N math anchors + live engine
cross-check) and thermal (plane-wall anchors + live conduction solve).

**Skip count: 23 → 15. Executed: 8 → 16. BROKEN: 0. Verdict: PASS.**

### Correction to the T-V1 "Key Architectural Finding"

The T-V1 claim that `sim_run_static`/`sim_run_modal` are "not accessible via the
core_bridge JSON-RPC bridge" is true only for the plain `core/build-linux` .so.
The **CHUDWARE_WITH_NETGEN** build (`core/build-occt-linux/bin/libchudware.so`)
registers `sim_run_static`, `sim_run_modal`, `sim_run_fatigue` and
`sim_run_thermal` directly in `core/api/libchudware.cpp` — raw `cb.call()` reaches
them with a pre-resolved study (step_path + facesets from
`doc_query`/`doc_face_points`). No full MCP server needed. The standalone-pytest
consequence: `services/mcp/test_fea_live_bridge.py` (T-SIM4) now runs
cantilever static+modal against beam theory in the local gate.

### New / upgraded verdict rows (2026-07-05)

| Tool | Verdict | Evidence |
|---|---|---|
| `run_fea_static` [live] | **VALIDATED** | Cantilever 50×50mm × 1 m steel, P=1000 N: max_disp = 3.1876 mm vs Roark 8th Table 8.1 case 1a δ=PL³/3EI = 3.2000 mm — **0.39%** (n_dof 6630, tet10) |
| `run_fea_modal` [live] | **VALIDATED** | f₁ = 40.86 Hz vs Rao 5th §8.2 Table 8.1 (β₁L=1.87510) 40.77 Hz — **0.22%**; square-section bending pair degenerate (40.86/40.87), next pair 253 Hz |
| `sn_life` (fea_advanced) | **VALIDATED** | sa_eq=250, Se=200, Sut=400 MPa → N=72 627 vs Shigley 10th Eq 6-14..6-16 hand calc 72 627 — 0.00% |
| `fatigue_anchor` (sim_advisor, T-SIM4) | **VALIDATED** | Goodman R=0 case: sa_eq=133.333 MPa vs Eq 6-46 hand calc 133.333 — 0.000%; regime=infinite, FoS=1.50 |
| `run_fea_fatigue` [live] | **VALIDATED** | Fully-reversed load on the live static field: engine Goodman sa_eq = static peak vM = 34.3938 MPa (0.000%) and = fatigue_anchor (0.000%) — engine/host/anchor triple agreement |
| `thermal_anchor` (sim_advisor) | **VALIDATED** | Fourier plane wall q=kΔT/L: 500 000 W/m² vs hand calc — 0.000% (Incropera §3.1) |
| `sim_run_thermal` [live] | **VALIDATED** | Bar 100×20×20 mm, k=50 W/mK, 100 °C Dirichlet + convection h=500 W/m²K→20 °C: cold face 60.26 °C vs series-resistance closed form 60.0 °C — **0.43%**; hot face exact |

Remaining SKIPs (15) are provisioning/degradation rows (OpenFOAM absent,
ngspice pass-through, schema-only tools, source-audit REAL rows) — none are
physics checks that could run on this machine but didn't.

### Re-run instructions (updated)

```bash
CHUDWARE_LIBCHUDWARE=core/build-occt-linux/bin/libchudware.so \
    ~/.local/share/Chudware/mcp-venv/bin/python scripts/sim_audit.py
```

Expected: 16 executed, 0 BROKEN, PASS. (With the plain build-linux .so the FEA
families fall back to source-audit REAL verdicts + honest skips.)
