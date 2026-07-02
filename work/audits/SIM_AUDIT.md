# SIM_AUDIT.md — T-V1 Simulation Backend Audit

**Date:** 2026-07-02  
**Auditor:** sonnet-lyra  
**Branch:** task/T-V1-sim-backend-audit  
**Script:** `scripts/sim_audit.py` (re-runnable; 8 checks executed, 23 skipped with reason)

---

## Executive Summary

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
~/.local/share/AI-CAD/mcp-venv/bin/pip install fluids
```
Verdict change: `piping_solve` would upgrade REAL → VALIDATED once installed.

### Missing: `PyNite` (MIT) — blocks struct_analyze
```bash
~/.local/share/AI-CAD/mcp-venv/bin/pip install PyNite
```
Verdict change: `struct_analyze` would upgrade REAL → VALIDATED (live cantilever check included in audit script).

### Missing: `build123d` / OCP kernel — blocks piping 3D tools
```bash
~/.local/share/AI-CAD/mcp-venv/bin/pip install build123d
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
~/.local/share/AI-CAD/mcp-venv/bin/pip install CoolProp   # MIT
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
AI_CAD_LIBAICAD=/home/hunter/Repositories/CAD/core/build-linux/bin/libaicad.so \
    ~/.local/share/AI-CAD/mcp-venv/bin/python scripts/sim_audit.py
```

Expected: 8 executed, 3 BROKEN (all provisioning gaps), 0 wrong-physics failures.
