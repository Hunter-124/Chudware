# core/sim + engine/sim — FEA / CFD / SPICE

Scope: core/sim/**, engine/sim/** (engine → engine_oracle symlink)
Contracts: contracts/mesh_format.md (AICM carries FEA results + CFD streamlines), contracts/worker_rpc.md (study RPCs)

## Entry points
- core/sim/fea_static.cpp — static FEA solver (C++ parity 4/4 with Python per README)
- core/sim/fea_modal.cpp — modal FEA solver
- core/sim/fea_fatigue.cpp — fatigue analysis
- core/sim/lbm.cpp — Lattice-Boltzmann CFD flow (the only C++ CFD path)
- core/sim/flow_run.cpp — CFD run driver
- core/sim/mesher.cpp, core/sim/hex_mesher.cpp — FEA/CFD meshing (tet/hex)
- core/sim/spice.cpp — SPICE engine (35/35 device parity, epic complete)
- core/sim/netlist.cpp — netlist / symbolic circuit sim (SymbolSim ~L904; 10/10 netlist parity)
- core/sim/overlay.cpp — result overlay, feeds AICM mesh chunks for viewport display
- engine/sim/openfoam.py, foam_case.py, foam_runner.py, foam_post.py — OpenFOAM "CFD accurate" path (Python-ONLY, no C++ port)
- engine/sim/circuit.py, devices.py, spice.py, subckt.py — Python SPICE oracle (paralleled by core/sim/spice.cpp)
- engine/sim/worker.py — Python sim RPC worker

## How it connects
Upstream: study RPCs arrive via contracts/worker_rpc.md (doc-session layer); core/tests/test_document.cpp
~L928 registers `test_studies` covering FEA/CFD study RPC. Downstream: results are serialized as AICM
chunks (contracts/mesh_format.md) rendered by host/src/render/aicm_loader and shown in
host/src/ui/sim_manager.cpp (GUI Simulation panel). MCP surface: run_fea_static / run_fea_modal /
run_cfd_flow / run_cfd_accurate / ecad_simulate. CFD-accurate (OpenFOAM) is still Python-only —
"Still Python-only: CFD" (README); LBM flow + FEA + SPICE are cut over to C++.

## Debugging playbook
- Build core: `cmake -S core -B core/build-occt -DAICAD_WITH_OCCT=ON && cmake --build core/build-occt`
- Sim tests: `ctest --test-dir core/build-occt -R "sim|modal|tet10|spice_netlist|fatigue|studies" --output-on-failure`
- Other targets: `netgen_mesh`, `mesher_autopick`, `mesh_quality`, `overlay_surface` (fatigue/studies are OCCT-gated)
- Python side (needs runtime venv): `<venv>/python -X utf8 -m pytest engine/sim -q` (SKIP if venv absent — AGENTS.md)
- Gotcha: docs/REFACTOR_LOG.md has NO sim entries yet — sim work is not logged there; check core/tests/ directly
- Gotcha: differential-oracle rule (README): never weaken an assertion; match Python unless Python is provably buggy

## Graphify starters
- graphify query "how does the FEA static solver get its mesh and boundary conditions"
- graphify explain "lbm"
- graphify query "how do SPICE netlist and device models connect to ecad simulation"

## Related tasks/docs
- work/tasks: grep `fea|cfd|spice` hits T-A4, T-C1/C2, T-E1/E4, T-F2/F7/F9/F13-F15 (many are GUI-adjacent; read before claiming)
- docs/GEOMETRY_LLM_REPRESENTATION_REVIEW.md (GSL narration of sim state), contracts/mesh_format.md
- host/src/ui/sim_manager.cpp glass-styling notes in docs/REFACTOR_LOG.md (GUI, not solver)
