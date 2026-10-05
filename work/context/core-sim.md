# core/sim + services/sim — FEA, CFD, SPICE and topology optimization

Scope: core/sim/**, services/sim/**; agent tools in services/mcp/cad_sim_tools.py, sim_evidence_tools.py, topology_tools.py
Contracts: contracts/mesh_format.md, worker_rpc.md; study/tool schemas remain the wire authority.
Canonical guidance: .claude/skills/chudware-dev/SKILL.md (evidence, refusal-before-mesh, resource safety, persisted studies).

## Entry points
- `core/sim/fea_run.cpp`, `fea_prepare.h` — CAD mesh/BC preparation and native FEA
  orchestration; `fea_static.cpp`, `fea_modal.cpp`, `fea_contact.cpp`, `fea_buckling.cpp`,
  `fea_thermal.cpp`, `fea_fatigue.cpp` implement the solver families.
- `core/sim/mesher.cpp`, `mesh_size_band.cpp`, `mesh_quality.cpp` — Polyhedral-FEA
  meshing, delivered-size diagnostics and mesh-quality evidence.
- `core/sim/topopt_run.cpp`, `topopt_model.cpp`, `topopt_optimizer.cpp` and the
  `topopt_*` physics/manufacturing modules — native topology optimization.
- `core/sim/lbm.cpp`, `flow_run.cpp` — native Lattice-Boltzmann CFD.
- `core/sim/spice.cpp`, `netlist.cpp` — native SPICE and symbolic circuit simulation.
- `core/sim/overlay.cpp` — viewport result overlays; `vtu_writer.cpp` — VTU output.
- `services/sim/jobs.py`, `job_worker.py` — durable jobs, atomic result publication,
  phase snapshots and 30 s heartbeats in `partial.json`.
- `services/sim/openfoam.py`, `foam_case.py`, `foam_runner.py`, `foam_post.py`,
  `foam_cht.py` — OpenFOAM/CHT integration, not a C++ solver port.
- `services/sim/multiphysics_rpc.py` — worker capability/contact proxy/frequency-response
  and thermal/heatsink handoffs. Its CalculiX two-block contact proxy is distinct
  from native CAD contact FEA.
- `services/sim/fea_thermal_transient.py`, `vibration.py`, `spice.py`, `subckt.py`,
  `em/`, `credibility.py` — service-side physics, adapters and credibility evaluation.
- `services/mcp/simhandoff.py` — finite, provenance-bearing cross-simulation envelopes.

## How it connects
Study tools resolve real geometry through `core_bridge.call` and persist user-openable
studies. Native solves execute in libchudware; long jobs publish progress/results
through the existing durable-job tools. AICM overlays use the shipped
`host/src/render/aicm_loader.cpp` and `host/src/ui/sim_manager.cpp`. OpenFOAM and
external adapters keep their own honest capability/prerequisite reporting.

`services/mcp/topology_tools.py` exposes `topology_capabilities` and
`run_topology_optimization` over native `sim_run_topology`; the host's topology UI
uses the same engine capabilities. Preserve both human and agent reachability.

## Debugging playbook
- Integrator build: `./build.sh core`; gate-tree tests: `ctest --test-dir core/build-linux -R 'sim|modal|tet10|spice_netlist|fatigue|studies|mesh|topopt|overlay_surface' --output-on-failure`.
  Use `core/build-occt-linux` for a separately configured OCCT development tree.
- Python: `<venv>/python -X utf8 -m pytest services/sim -q`.
- Keep top-level `mesh_quality`, reactions where applicable, and provenance; report
  requested vs delivered size and named refusals. Never pass off a proxy or
  under-resolved study as validated full-vehicle evidence.
- Inspect job phase/heartbeat and process exit before reading terminal results;
  partial output is not success. Do not increase solve budgets over a busy machine.
- `docs/REFACTOR_LOG.md` and `docs/refactor-log/` contain simulation history;
  current behavior comes from the solver, job code and regression tests.
- External OpenFOAM/ngspice/CalculiX/openEMS capabilities are optional; absence is
  a named refusal or documented skip, never a fabricated successful result.

## Graphify starters
- graphify query "FEA mesh boundary conditions fea_run"
- graphify explain "sim_run_topology"
- graphify query "SPICE netlist ECAD simulation"

## Related tasks/docs
- work/context/build-test.md — centralized gate/build tree rules
- services/agent/skills/builtin/simulate/SKILL.md — evidence-grade agent workflow (`cad_skill`)
- docs/vv/FEA_VV_REPORT.md — numerical validation evidence
- docs/reference/FIN25_SENSOR_NODE_GOLDEN_DEMO.md — cross-domain demo
