# services/mcp — MCP server (AI-CAD engine exposed to external agents)

Scope: services/mcp/**, root `mcp_server` symlink (the stdio MCP server)
Contracts: contracts/doc_ops.md, contracts/doc_schema.md, contracts/worker_rpc.md, contracts/feature_params.md, contracts/ecad/ecad_rpc.md

## Entry points
- services/mcp/server.py:26 `mcp` FastMCP instance — the root MCP server object; registers all tool groups via `register_*` functions L55–80
- services/mcp/server.py:153 `main()` — entrypoint; spawns libaicad.dll warmup thread, redirects native stdout (OCCT prints) to a log file to protect the JSON-RPC channel
- services/mcp/cad_tools.py:945 `register_cad_tools(mcp)` — mechanical CAD + simulation tools (new_part, add_features, render_part, run_fea_static, run_cfd_flow, export_part, measure_part, etc.)
- services/mcp/gsl_tools.py:516 `register_gsl_tools(mcp)` — compact geometry read (describe_part, query_adjacency, hover, describe_sketch, describe_scene)
- services/mcp/recon_tools.py:677 `register_recon_tools(mcp)` — reverse-engineering/intake (reverse_engineer, compare_parts, convert_face_to_sketch, resolve_process, dfm_check)
- services/mcp/ecad_tools.py:420 `register_ecad_tools(mcp)` — schematic/PCB tools (ecad_new_schematic, ecad_new_pcb, ecad_place_symbol, ecad_autoroute, ecad_simulate, ecad_to_3d)
- services/mcp/cam_tools.py:49 `register_cam_tools(mcp)` — CAM/toolpath (cam_new_job, cam_add_setup, cam_add_tool, cam_add_operation, cam_simulate, cam_post)
- services/mcp/core_bridge.py:20 `call(method, params)` — thin ctypes RPC bridge to libaicad.dll (JSON string-in/out); resolves library via $AI_CAD_LIBAICAD / $AI_CAD_DIST / build dirs
- services/mcp/test_roundtrip.py:5 `python -m mcp_server.test_roundtrip` — integration test: list tools, new_part, add_features, render, measure, run_fea_static, export

## How it connects
The stdio MCP server (server.py) wraps the SAME C++ core (`libaicad.dll`) that the desktop app uses, exposing it to external MCP clients (Claude Code, Cursor, etc.) as a tool palette. Each tool group (cad_tools, gsl_tools, recon_tools, ecad_tools, cam_tools, intake_tools) registers its tools via FastMCP decorators (`@mcp.tool()`) and dispatches into the C++ engine through `core_bridge.call()` — a ctypes wrapper over the native `aicad_call(method, params_json)` C-ABI entry. This is fundamentally different from `services/mcp/../work/tools/team_mcp.py` (an unrelated MCP server for dev-team coordination); the MCP server is pillar 12 ("MCP-first distribution") of docs/AI_TOOLING_MASTER_PLAN.md. Mechanical and electrical CAD share the engine; ECAD is pure Python until 3D (board_to_step). Python engine_oracle modules (worker.py, kernel.py, formats.py) are called in-process by MCP tools, NOT through the worker socket.

## Debugging playbook
- Run: `python -m mcp_server.test_roundtrip` — full MCP roundtrip (create part, add features, render, FEA, export); prints each step
- Run: `python -m mcp_server.test_cad_tools` — pytest suite for mechanical CAD tools
- Run: `python -m mcp_server.test_gsl_tools` — GSL describe/query/hover tests
- Run: `python -m mcp_server.test_recon_tools` — reverse-engineering tests
- Run: `python -m mcp_server.test_ecad_tools` — ECAD tool tests
- Environment: `$AI_CAD_LIBAICAD=/path/to/libaicad.so` (Linux) or `$AI_CAD_DIST=dist/AI-CAD` (Windows); core_bridge.py L75–92 lists candidates
- Gotcha: server.py `_install_clean_stdout()` L83–107 redirects fd 1 to a log file — native C++ prints (OCCT, Netgen) corrupt the JSON-RPC channel; set $TEMP to see logs
- Gotcha: `_warm_core()` L110–150 pre-loads libaicad.dll + ECAD deps in a background thread to avoid transport timeouts on the FIRST tool call; blocking the cold load here is cheaper than timing out a client call

## Graphify starters
- graphify query "mcp_server call cad_guide new_part add_features render_part"
- graphify explain "core_bridge call"
- graphify path "server.py" "core_bridge.py"

## Related tasks/docs
- docs/AI_TOOLING_MASTER_PLAN.md (pillar 12 — MCP-first distribution as the flagship path)
- contracts/doc_ops.md (feature op vocabulary), contracts/doc_schema.md (part/assembly/drawing schema)
- contracts/worker_rpc.md (protocol between MCP + C++ core via aicad_call)
- contracts/ecad/ecad_rpc.md (ECAD RPC surface)
- work/tasks: grep `MCP|services/mcp` in work/tasks/*.md for active work (T-F* tasks in AI experience lane)
