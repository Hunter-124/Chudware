# Engine (Python kernel worker / doc compiler / formats)

Scope: engine_oracle/ (root symlink `engine` — `ls -la engine` → `engine -> engine_oracle`), engine/doc/
Contracts: contracts/doc_schema.md, contracts/doc_ops.md, contracts/worker_rpc.md, contracts/mesh_format.md, contracts/stepplus.md

## Entry points
- engine_oracle/kernel.py:347 `_exec_model`, :367 `run_code`, :539 `run_code_multi`, :625 `run_code_isolated` — executes AI-generated build123d code in a sandboxed namespace, resolves `result`/`parts`/`assembly`, computes mass props + fit report (interference/clearance, :486 `_fit_report`), exports STEP/STL
- engine_oracle/worker.py:1-24 (module docstring — the wire protocol), :51 `_ping`, :61 `_run_model`, :71 `_agent_design`, :104 `_get_prompts`, :131 `_agent_cancel`, :192 `serve()` — newline-delimited JSON-RPC loop over stdio; this is what the C++ host/worker-bridge spawns
- engine_oracle/render.py:83 `render_views`, :112 `render_grid`, :153 `render_assembly_grid` — headless multi-view PNG screenshots (PyVista/VTK off-screen), used by MCP-headless mode + tests
- engine_oracle/run_model.py:29 `main` — CLI entry (`-m engine.run_model <sample>`)
- engine/doc/formats.py:155 `export_document`, :252 `import_path` — multi-format import/export dispatcher (STEP/IGES/BREP/STL/3MF/OBJ/3dm, `_import_step/_import_iges/_import_brep/_import_stl/_import_3mf/_import_3dm`)
- engine/doc/formats_solidworks.py, sw_translate.py, sw_feature_import.py — SolidWorks COM/Parasolid interop
- engine/doc/mesh_body.py:128 `MeshBody`, :101 `mesh_mass_props` — tessellation + mesh mass-property math shared by viewport/export paths
- engine/doc/tessellate.py — B-rep → triangle mesh for the viewport
- engine_oracle/parts_library.py, parts_providers.py, vendor_parts.py — standard/vendor parts catalog backing `make_standard_part`/`list_standard_parts`
- engine_oracle/paths.py — `%APPDATA%\AI-CAD` runtime/cache path resolution
- engine_oracle/samples/ — feature-rich reference models used as smoke tests (e.g. `mounting_plate.py`)
- Note: ARCHITECTURE.md references a `protocol.py` "result schema shared across kernel/worker/MCP/host" — **this file does not exist on disk** (verified via `find`); the schema is implicit in `kernel.ModelResult`/`AssemblyResult` dataclasses instead. Treat that ARCHITECTURE.md line as stale.

## How it connects
`worker.py` is spawned by `services/worker/` (native host / new ui-imgui) over stdio and only exposes `ping`/`run_model`/`agent_design`/`shutdown` — it is deliberately thin, delegating to `kernel.run_code_multi`. The MCP layer (`services/mcp/`) and ECAD layer (`engine/ecad/`) mostly call `engine_oracle` modules **directly in-process**, not through the worker socket (see contracts/worker_rpc.md and contracts/ecad/ecad_rpc.md:43). `core/` (C++/libaicad) is strangling this module subsystem-by-subsystem — engine_oracle is explicitly "deleted LAST" per AGENTS.md, kept as the parity oracle. `services/worker/` decides whether a call routes to this Python worker or to libaicad.

## Debugging playbook
- `$py = "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe"` (Windows) — the managed runtime; on Linux check for an equivalent local venv (see linux-build-port memory / build-test.md)
- `& $py -m engine.run_model mounting_plate` — build + render a sample to `%APPDATA%\AI-CAD\cache`
- `& $py -m engine.doc.test_sketch_solver` — run one engine test suite (house style: every `test_*.py` is a plain-`main()` runner, invoke as `-m engine.<pkg>.test_<name>`)
- `& $py -m mcp_server.test_roundtrip` — MCP roundtrip self-test that exercises this layer end-to-end
- `<venv>/python -X utf8 -m pytest engine/doc agent -q` — the pytest-style suites AGENTS.md's gate runs (SKIP, not fail, if the runtime venv is absent on Linux)
- engine_oracle root also has its own test modules: `test_kernel_aliases.py`, `test_parts_library.py`, `test_parts_providers.py`, `test_vendor_parts.py`, `test_worker_stream.py`

## Graphify starters
- graphify query "engine_oracle kernel worker run_code_multi export_document"
- graphify explain "run_code_multi"
- graphify path "worker.py" "kernel.py"

## Related tasks/docs
- AGENTS.md (engine_oracle lifecycle: "frozen while core/ catches up... deleted LAST (N6b)")
- ARCHITECTURE.md §1 (component description — note the stale `protocol.py` reference above)
- contracts/doc_schema.md, contracts/doc_ops.md, contracts/stepplus.md (native `.step+` format)
- docs/PYTHON_CUTOVER_STATUS.md is referenced by ARCHITECTURE.md but was deleted per current `git status` — check `docs/CPP_MIGRATION.md` or REFACTOR_LOG.md for current cutover state instead
