# core/sim + services/sim — FEA / CFD / SPICE

Scope: core/sim/**, services/sim/**
Contracts: contracts/mesh_format.md (AICM carries FEA results + CFD streamlines), contracts/worker_rpc.md (study RPCs)

## Entry points
- core/sim/fea_static.cpp — static FEA solver (C++ parity 4/4 with Python per README)
- core/sim/fea_modal.cpp — modal FEA solver
- core/sim/fea_fatigue.cpp — fatigue analysis
- core/sim/lbm.cpp — Lattice-Boltzmann CFD flow (the only C++ CFD path)
- core/sim/flow_run.cpp — CFD run driver
- core/sim/mesher.cpp, mesh_size_band.cpp, mesh_quality.cpp — Polyhedral-FEA tet meshing
- core/sim/spice.cpp — SPICE engine (35/35 device parity, epic complete)
- core/sim/netlist.cpp — netlist / symbolic circuit sim (SymbolSim ~L904; 10/10 netlist parity)
- core/sim/overlay.cpp — result overlay, feeds AICM mesh chunks for viewport display
- services/sim/openfoam.py, foam_case.py, foam_runner.py, foam_post.py — OpenFOAM "CFD accurate" path (Python-ONLY, no C++ port)
- services/sim/spice.py, subckt.py — Python SPICE services (paralleled by core/sim/spice.cpp)
- services/sim/job_worker.py — Python sim job worker

## How it connects
Upstream: study RPCs arrive via contracts/worker_rpc.md (doc-session layer); core/tests/test_document.cpp
~L928 registers `test_studies` covering FEA/CFD study RPC. Downstream: results are serialized as AICM
chunks (contracts/mesh_format.md) rendered by host/src/render/aicm_loader.cpp and shown in
host/src/ui/sim_manager.cpp (GUI Simulation panel). MCP surface: run_fea_static / run_fea_modal /
run_cfd_flow / run_cfd_accurate / ecad_simulate. CFD-accurate (OpenFOAM) is still Python-only —
"Still Python-only: CFD" (README); LBM flow + FEA + SPICE are cut over to C++.

## Debugging playbook
- Build core: `cmake -S core -B core/build-occt -DCHUDWARE_WITH_OCCT=ON && cmake --build core/build-occt`
- Sim tests: `ctest --test-dir core/build-occt -R "sim|modal|tet10|spice_netlist|fatigue|studies" --output-on-failure`
- Other targets: `mesh`, `mesh_quality`, `mesher_advanced`, `overlay_surface`
- Python side (needs runtime venv): `<venv>/python -X utf8 -m pytest services/sim -q` (SKIP if venv absent — AGENTS.md)
- Gotcha: docs/REFACTOR_LOG.md has NO sim entries yet — sim work is not logged there; check core/tests/ directly
- Gotcha: differential-oracle rule (README): never weaken an assertion; match Python unless Python is provably buggy

## Graphify starters
- graphify query "how does the FEA static solver get its mesh and boundary conditions"
- graphify explain "lbm"
- graphify query "how do SPICE netlist and device models connect to ecad simulation"

## Related tasks/docs
- work/tasks: grep `fea|cfd|spice` hits T-A4, T-C1/C2, T-E1/E4, T-F2/F7/F9/F13-F15 (many are GUI-adjacent; read before claiming)
- docs/review/GEOMETRY_LLM_REPRESENTATION_REVIEW.md (GSL narration of sim state), contracts/mesh_format.md
- host/src/ui/sim_manager.cpp glass-styling notes in docs/REFACTOR_LOG.md (GUI, not solver)

## FIN content (2026-07-10)
- Cross-sim seam: `aicad.simhandoff/1` (`services/mcp/simhandoff.py`, contracts via T-FIN15)
- Transient thermal: `services/sim/fea_thermal_transient.py` (T-FIN18)
- OpenFOAM CHT: `services/sim/foam_cht.py` (T-FIN19 / T-SIM5)
- Worker multiphysics mirror for host Simulate tab: `services/sim/multiphysics_rpc.py`
  (T-FIN24) — sim_caps, ccx buckling/contact, freq-response, ecad_thermal_handoff, heatsink_select
- Golden demo: `scripts/demo_sensor_node.py` (T-FIN25) + gate stage 4.7
- T-SIM10 superseded by T-FIN24; T-SIM11 = deferred deeper EM solvers (GUI forms already live)
