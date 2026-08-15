# Python services (kernel executor / worker / formats)

Scope: services/kernel_exec.py, services/worker_main.py, services/formats/, services/sw_import/, services/parts/, services/paths.py
Contracts: contracts/doc_schema.md, contracts/doc_ops.md, contracts/worker_rpc.md, contracts/mesh_format.md, contracts/stepplus.md

## Entry points
- services/kernel_exec.py — executes AI-generated build123d code, resolves single- and multi-part results, computes mass properties and fit reports, and exports STEP/STL
- services/worker_main.py — newline-delimited services-side JSON-RPC worker for run_model, editable prompts, vendor parts, deliverables, parts hub, agent GUI seams, reveng, DAG steering, PLM, EM, and multiphysics
- services/formats/formats.py — multi-format import/export dispatcher
- services/formats/formats_solidworks.py, services/sw_import/sw_translate.py, services/sw_import/sw_feature_import.py — SolidWorks COM/Parasolid interop
- services/formats/mesh_body.py — tessellation + mesh mass-property math shared by viewport/export paths
- services/sim/tessellate.py — B-rep → triangle mesh for simulation paths
- services/parts/parts_library.py, services/parts/vendor_parts.py — standard/vendor parts catalog
- services/paths.py — runtime/cache path resolution

## How it connects
`services/worker_main.py` is spawned by the native worker bridge for service methods the C++ engine does not implement. It delegates build123d execution to `services/kernel_exec.py`; document, sketch, CAM, and most simulation/ECAD engine methods are now implemented in `core/`. The Python subsystem therefore survives under `services/`, but the former `engine/` and `engine_oracle/` trees do not.

## Debugging playbook
- Windows managed runtime: `$py = "$env:APPDATA\Chudware\runtime\venv\Scripts\python.exe"`; on Linux use the provisioned runtime venv
- `& $py -m pytest services/test_kernel_exec_aliases.py services/test_worker_stream.py -q` — kernel and worker smoke suites
- `& $py -m services.mcp.test_roundtrip` — MCP roundtrip self-test
- `<venv>/python -X utf8 -m pytest services/formats services/sw_import services/parts -q` — migrated service suites

## Graphify starters
- graphify query "services kernel_exec worker_main run_code_multi formats"
- graphify explain "run_code_multi"
- graphify path "worker_main.py" "kernel_exec.py"

## Related tasks/docs
- ARCHITECTURE.md §1 (component description)
- contracts/doc_schema.md, contracts/doc_ops.md, contracts/stepplus.md (native `.step+` format)
