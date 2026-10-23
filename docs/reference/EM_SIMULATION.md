# Full-wave electromagnetic simulation (EM sim family)

Simulation family added 2026-07-07: eigenmode/modal analysis of waveguide
cross-sections, frequency-domain driven S-parameters, and broadband time-domain response —
behind a pluggable solver adapter layer with openEMS (FDTD) as the primary full-wave engine.

- Code: `services/sim/em/`
- RPCs: `em_status`, `em_eigenmode`, `em_driven`, `em_timedomain` — registered by
  `services/worker_main.py` (`_register("em", "services.sim.em.worker_rpc", ...)`) and
  exposed to agents as the same-named tools
- Tests: `python3 -m pytest services/sim/em/test_em.py` (the 2026-07-07 landing recorded
  21 real + 1 gated live test; current count not re-verified here)
- Planned follow-ups: FEM and Meep adapters, direct document-solid intake, open-radiation MoM
- License containment: `contracts/license_debt.md` row 9

## Solver matrix (capability x solver x status; status column as recorded 2026-07-07)

| Solver | Method | Eigenmode | Driven (S-params) | Time-domain | Linkage | License | Status on this repo |
|--------|--------|-----------|-------------------|-------------|---------|---------|---------------------|
| **openems** | 3-D FDTD | — (delegated to FDFD) | yes | yes | python-module (lazy, sim-worker only) | GPLv3 | **Adapter implemented, mock-proven**; openEMS has no PyPI wheel and must be installed separately |
| **builtin-fdfd** | 2-D FDFD scalar Helmholtz (numpy/scipy) | yes (rectangular + circular hollow guides) | — | — | builtin | in-repo | **Real + validated** (WR-90 TE10 within 0.006 %) |
| **analytic-uniform** | closed-form hollow-guide model | yes (cutoff tables) | uniform straight sections only (refuses geometry) | uniform sections via dispersive IFFT | builtin | in-repo | **Real + validated** |
| gmsh-getdp | FEM (freq-domain, eigenmode, curved geometry) | planned | planned | — | external-process | GPL-2+ | Detection + install hint only; adapter planned |
| meep | 3-D FDTD | — | planned | planned | python-module | GPL-2+ | Detection only; adapter planned |
| elmer | FEM (VectorHelmholtz) | planned | planned | — | external-process | GPL-2+ (LGPL lib) | Detection only; adapter planned |
| MoM (open radiation / antennas) | — | — | — | — | — | — | Deferred (low priority for closed waveguides) |

Auto-selection (`solver: "auto"` or omitted): registry preference order is
openems → builtin-fdfd → analytic-uniform → scaffolds; the first *installed* adapter
offering the requested analysis wins. `em_status` reports the live matrix + per-analysis
defaults; the GUI's EM tab uses the probe to enable or grey its commands.

## Architecture

```
GUI SimManager ── st.rpc / WorkerClient ──> chudware-hostd ── WorkerBridge ──> services/worker_main.py
               (legacy in-process mode, CHUDWARE_ENGINE_DAEMON=0: local stdio worker pipe)
                                                        │  em_* WORKER_METHODS
                                                        v
                                        services/sim/em/worker_rpc.py
                                                        │ select_adapter(analysis, solver)
                                                        v
                    adapters.py registry ── EmAdapter subclasses
                    ├─ openems_adapter.py   build_plan() [pure] → _execute() [GPL seam] → shape
                    ├─ BuiltinFdfdAdapter   fdfd.py (scipy.sparse eigsh, cell-centred FD)
                    ├─ AnalyticAdapter      analytic.py (Pozar closed forms, IFFT TD)
                    └─ Getdp/Meep/Elmer     detection-only scaffolds
```

Key seams:

- `OpenEmsAdapter.build_plan(spec)` is pure (spec → mesh lines / modal ports / boundaries /
  Gaussian excitation plan); plan-builder unit tests run without the solver.
- `OpenEmsAdapter._execute(plan)` is the ONLY code importing `openEMS`/`CSXCAD` — the GPL
  containment and the mock seam for tests. Swapping it to external-process CLI invocation
  (if legal review requires) touches nothing else.
- Results are shaped like the existing sim results (`{"ok": ..., "solver": ..., summary
  arrays}`): S-params as `freq_ghz` + `s11_db`/`s21_db` + complex `[re, im]` pairs; mode
  tables as `{type, name, fc_ghz, kc_per_m, analytic_err_pct, field?}`; time domain as
  `t_ns` + `ports.port1_v/port2_v`.

## RPC contract

All methods return `{"ok": bool, ...}`; failures return `{"ok": false, "error", "solvers"}`
(the capability report rides along so callers can explain what to install).

```jsonc
em_status {}
  -> { ok, solvers: [{name, kind, license, linkage, capabilities, installed,
                      version, detail, install}],
       defaults: {eigenmode, driven, timedomain}, analyses: [...] }

em_eigenmode { standard: "WR-90" | waveguide: {shape: "rectangular"|"circular",
               a_mm, b_mm | radius_mm, eps_r?},
               n_modes?: 6, resolution?: 80, fields?: false, solver?: "auto" }
  -> { ok, solver, cross_section, modes: [{type, name, m?, n?, fc_ghz, fc_hz,
       kc_per_m, analytic_fc_hz?, analytic_err_pct?, field?}], solver_detail }

em_driven { standard|waveguide, length_mm?: 50, mode?: "TE10",
            f_start_ghz?, f_stop_ghz?, n_freq?: 201,
            geometry?: {stl_path}, solver?: "auto" }
  -> { ok, solver, mode, fc_ghz, length_mm, freq_ghz[], s11_db[], s21_db[],
       s11[[re,im]], s21[[re,im]], solver_detail }

em_timedomain { standard|waveguide, length_mm?, mode?, f0_ghz?, bw_ghz?, solver? }
  -> { ok, solver, mode, fc_ghz, excitation{f0_ghz,bw_ghz,shape},
       t_ns[], ports{port1_v[], port2_v[]}, spectrum?{...em_driven payload} }
```

Geometry intake v1: `geometry.stl_path` feeds openEMS as a PEC polyhedron
(CSXCAD `AddPolyhedronReader`) — export the doc body with `export_part` first.
Direct doc solids/tess + material assignment intake is planned.

## Validation results (dated record, 2026-07-07 — not re-measured since)

| Case | Reference | Computed | Error |
|------|-----------|----------|-------|
| WR-90 TE10 cutoff (builtin-fdfd, 80x36 grid) | 6.5571 GHz analytic | 6.5567 GHz | **0.006 %** |
| WR-90 TE20 / TE01 / TE11 / TM11 cutoffs | analytic table | labeled + matched | < 0.04 % |
| Circular r=10 mm TE11 (degenerate pair) | 8.7850 GHz (p'11=1.8412) | 8.761 GHz | 0.27 % |
| TE10 group delay, 200 mm @ 10 GHz (analytic TD path) | L/vg closed form | pulse-peak delay | < 0.15 ns |
| openEMS driven pipeline (mocked `_execute`) | analytic S21=exp(-γL) | s21_db: <-40 dB below cutoff, \|·\|<0.01 dB above | pass |

The live openEMS WR-90 run (`test_live_openems_if_installed`) asserts ~0 dB S21 in the
propagating band and runs automatically wherever openEMS is present.

## Install recipes

openEMS has **no PyPI wheel** (`pip install openEMS` fails by design) — it is a C++ build
with Python bindings:

```bash
# Fedora build deps
sudo dnf install cmake gcc-c++ git tinyxml-devel hdf5-devel vtk-devel \
    boost-devel CGAL-devel fparser-devel python3-devel cython
git clone --recursive https://github.com/thliebig/openEMS-Project.git
cd openEMS-Project && ./update_openEMS.sh ~/opt/openEMS --python
# then make sure the venv the sim worker uses can import openEMS + CSXCAD
```

Secondary solvers:

```bash
pip install gmsh                       # meshing half of the FEM path (BSD-style wheel, GPL engine)
sudo dnf install getdp                 # GetDP FEM solver (or https://getdp.info binaries)
sudo dnf install elmerfem              # ElmerSolver
conda install -c conda-forge pymeep    # Meep FDTD (conda-only distribution)
```

`em_status` picks any of these up automatically once importable/on PATH (detection-only
adapters report `installed: true, detail: "adapter not implemented yet"` until their
adapters land).

## GUI wiring

Wired. The Simulate workspace's **EM** ribbon tab (`host/src/ui/ribbon_def.cpp`,
`sim_em_*` commands) routes to `SimManager::handleEmCommand`
(`host/src/ui/sim_manager.cpp`). The first EM command probes `em_status` once
asynchronously; if the call fails or reports an error code (e.g. the worker does not
serve `em_*`), the whole `sim_em_*` set is greyed with an "EM solver not installed"
tooltip. Otherwise the EM panel opens and issues `em_eigenmode` / `em_driven` /
`em_timedomain`. EM runs are stateless one-shot analyses, not doc-persisted studies
(`host/src/ui/sim_manager.h`).
