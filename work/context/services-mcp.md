# services/mcp — the CAD tool registry (stdio MCP server + online-agent host tools)

Scope: services/mcp/**; consumers: services/agent/chudcode_tools.py, services/agent/tool_categories.py
Contracts:
- contracts/doc_ops.md, doc_schema.md, worker_rpc.md, feature_params.md
- contracts/ecad/ecad_rpc.md
- contracts/narration.md, interaction.md, requirements.md, dag_schema.md
- contracts/parts.md, sourcing.md, deliverables.md, cad_graph.md
- contracts/copilot_modes.json (`mutating_tools`)

Canonical guidance:
- .claude/skills/cad-chudcode-integration/SKILL.md: host-tool projection, floors,
  chokepoints.
- .claude/skills/chudware-dev/SKILL.md: persistence and save rules.

## Entry points
- `services/mcp/server.py` — the module-level `mcp = FastMCP("Chudware", …)` is THE tool
  registry.
  - Every family registers as `register_*_tools(_reg.view(mcp, "<family>"))`.
  - Registered families come from the live `tool_registry.catalog()`; do not maintain
    a second list in an agent adapter or infer dead tools from absent GUI callers.
  - `_resolve_mcp_profile()` (`CHUDWARE_MCP_PROFILE=full|lite`, else capability tier) skips
    the `if not _LITE:` block. Instructions are `_FULL_INSTRUCTIONS` /
    `_LITE_INSTRUCTIONS`.
  - At the end of the module: `_reg.mark_warm_baseline()`, then `_install_arg_teaching(mcp)`.
- `server.py::main` installs terminal savers and the parent-watchdog, redirects
  native stdout before warming the core, then serves stdio over the retained real
  stream (`mcp.run` only when fd redirection is unsupported).

  `finally` runs `terminal_save()`, which calls `cad_common.save_touched_docs`.
- `services/mcp/tool_registry.py` — `view` stamps `{family, risk}`, and `_guard` enforces
  it at dispatch (the workspace write-lease check is part of `_guard`).
  - `deny_reason(name)` checks: Settings per-tool/per-family flags (`manufacture` is
    default-OFF), `_MOTION_TOOLS` (need an explicit allow), `CHUDWARE_TOOLS_ALLOW` /
    `CHUDWARE_TOOLS_DENY_RISK`, and `set_policy`. Policy errors fail closed.
  - `model_selectors` decorator: `@model_selectors("run_id")` below `@mcp.tool()`.
  - `catalog` / `warm_catalog` (reset-to-warm replay of `_WARM_BASELINE`).
- `services/mcp/core_bridge.py` `call(method, params)` — the one engine funnel.
  - Default: `_call_via_daemon` → `daemon_bridge.client()` (`DaemonClient`) to the shared
    `chudware-hostd`.
  - `CHUDWARE_ENGINE_DAEMON=0`, or a hostd startup failure (sticky), takes the legacy
    in-process ctypes `chudware_call`; `_lib_candidates()` lists where it looks.
  - `services/mcp/daemon_bridge.py`: `resolve_hostd_bin`, `socket_path`,
    `spawn_agent_worker`.
- `services/mcp/cad_common.py` — doc registry/handoff, `_publish_current`, autosave (daemon
  mode: hostd is the only autosave authority), `save_touched_docs`, durable jobs;
  `cad_guide.py` `cad_guide_text(topic)` backs the `cad_guide` tool.
  - `resolve_agent_path` and `_sanitize_output_path` resolve explicit relative destinations
    against `CHUDWARE_PROJECT_DIR`, then `CHUDWARE_FS_ROOT`, never process cwd. The first
    configured root is authoritative: canonicalize it without widening to parent project
    markers; refuse missing/non-directory roots instead of falling back. Absolute/tilde
    destinations need no root and remain outside the fs_* jail; the output sanitizer
    denies credential/key locations after symlink resolution.
  - `show_in_app` in daemon mode confirms the document is open and returns `live:true`;
    it does not publish a handoff file or force the user's active tab. Legacy mode still
    writes the handoff.
  - Call `wait_job` directly, not inside eval: the cell deadline includes the wait and can
    expire before the job finishes. Cancelling the wait leaves the job/result retrievable.
    `add_features` rename requires a feature id; document/file names use `new_part`,
    `save_part`, or a saved-native copy through `relocate_native_files`.
- Per-family modules. `cad_tools.py`, `ecad_tools.py` and `reveng_tools.py` are
  compat facades for importers only. New tools go to the matching family module, never
  into a facade.
  - CAD: `cad_{doc,eval,render,exchange,sim,asm,drawing,motion,animation}_tools.py` over
    `cad_common.py`.
    Atomic mutation diagnosis uses native `rejected_tree` and `feature_status`,
    not the restored live tree; provider failure must retain native error evidence.
    Live verification replays canonical feature envelopes with sparse-ID remapping
    in internal temporary documents, never handmade STEP+ containers.
    Native STL checks serialized facets and source-solid edge closure; preserve
    export refusals instead of quietly healing output or claiming print readiness.
  - ECAD: `ecad_{sch,pcb,sim,qa}_tools.py` over `ecad_common.py`. ECAD docs live in
    `core/ecad`; the facade is RPC-backed.
  - Reverse engineering: `reveng_{session,build}_tools.py` over `reveng_common.py`.
    `recon_tools` recognizes imported native topology: coplanar closed-mesh regions
    can recover planar features, while curved facets do not recover exact analytic
    surfaces. Do not assume every STL import has zero reconstruction yield.
  - Others: `gsl_tools`, `recon_tools`, `intake_tools`, `cam_tools`, `cam_audit`,
    `print_tools` (`slice_part`, print media), `mfg_tools`/`mfg_plan_tools`,
    `topology_tools`, `sim_evidence_tools`, `fs_tools` (`fs_*`, `run_shell`), `plm_tools`,
    `graph_tools`, `parts_tools`, `research_tools`, `verify_tools`, `design_review_tools`,
    `interaction_tools`, `firmware_tools`, `ecad_thermal`/`ecad_reliability`,
    `bugreport_tools`.
  - Outside this directory: `services/dag/mcp_tools.py`, `services/deliverables/mcp_tools.py`.

## How it connects
ONE Python registry serves two consumers:
1. External MCP clients (Claude Code, Cursor, …) via `python -m services.mcp.server` over
   stdio. The server id is `chudware`, registered by `services/installer/register_mcp.py` /
   `setup_mcp_clients.py`.
2. The online Chudcode agent, in-process with no MCP hop: services-worker `agent_admin_turn`
   (`services/agent/worker_rpc.py`) → `chudcode_tools.build_host_tools`. That function
   imports `services.mcp.server.mcp`, then:
   - filters with `_effective_names` (role/mode/`tool_categories.resolve_tool_requests`,
     then floors) and `tool_registry.deny_reason`;
   - dispatches through `dispatch_host_tool` → `agent_turn._dispatch_tool`.

   `tool_categories.MAIN_CHAT_CORE_TOOLS` is the main chat's default set; `enable_tools`
   loads the rest.

Engine-bound tools use `core_bridge.call` → hostd → libchudware. Files, research and
other service-only tools keep their own implementations. There is no C++ MCP server.

## Debugging playbook
- Tests use the service venv: `<venv>/python -X utf8 -m pytest services/mcp/<test> -q`.
  - `services/mcp/conftest.py` starts a PRIVATE hostd (`private_hostd.private_daemon`, from
    `core/build-occt-linux/bin`). It SKIPs the suite when none can start, unless
    `CHUDWARE_ENGINE_DAEMON=0`.
  - Never point tests at the user's live daemon: that fans `doc.opened` events into the
    open GUI.
- `python -m services.mcp.test_roundtrip` runs IN-PROCESS (`mcp.call_tool`). In daemon mode
  it attaches to whichever hostd is running; use a private daemon or
  `CHUDWARE_ENGINE_DAEMON=0`.
- Freshness: `test_mcp_freshness.py` requires each family in `_TOOL_FAMILIES` to have a
  `_FAMILY_SIGNALS` string in server instructions or a `cad_guide_text` topic. A new family
  updates all three.
- Every tool named in prose must be reachable: `scripts/advertised_tool_audit.py` (check.py).
- After adding/renaming a tool: add its `prompt_note` in `services/agent/tool_guidance/` and
  regenerate `host/src/ui/tool_panel_tables.h` (`tools/gen_tool_panel_tables.py`).
- Gotcha: native stdout is redirected (`_install_clean_stdout`); OCCT prints would
  otherwise corrupt JSON-RPC.
- Gotcha: `core_bridge.available()` dlopens libchudware in-process even in daemon mode. It
  is a presence probe, not a hostd connect.

- Printing precision: `print_tools._write_slicer_stl` refuses topology lost during float32
  placement; origin-centred single-part meshes defer bed placement to PrusaSlicer.
  Requested 3MF load-time repair evidence participates in readiness. Native DFM retains
  face attribution; raw-mesh bore recognition must not merge tangent flares into the bore.

## Graphify starters
- graphify query "services.mcp server register tool_registry view core_bridge call"
- graphify explain "build_host_tools"
- graphify path "server.py" "core_bridge.py"

## Related tasks/docs
- docs/AI_TOOLING_MASTER_PLAN.md (pillar 12 — MCP-first distribution)
- work/WORKFLOW.md §3 (MCP freshness rule), services/mcp/AGENTS.md
