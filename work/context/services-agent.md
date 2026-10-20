# services/agent — Chudcode sessions, CAD host tools, roles/missions and provider adapters

Scope: services/agent/** (online turns are hosted by the compiled Chudcode runtime; native C++ `core/agent/run.cpp` is the offline-safe local-modeling fallback)
Contracts: contracts/worker_rpc.md (protocol 2, worker lanes), contracts/agent_events.md + agent_events.json, contracts/events.md, contracts/copilot_modes.json
Canonical guidance:
- `.claude/skills/cad-chudcode-integration/SKILL.md`: topology, file table, session/tool/
  event/recovery rules, compaction choice.
- `.claude/skills/chudware-dev/SKILL.md`: CAD-side agent policy, worker lanes, prompt
  override isolation.

## Entry points
- `services/agent/worker_rpc.py` — `ADMIN_METHODS` (`agent_admin_turn`, `agent_cancel`,
  `agent_steer`, `chudcode_*`, `memory_*`), `PREVIEW_METHODS` (`ai_tool_catalog`,
  `ai_prompt_*`), and the merged `WORKER_METHODS` that `worker_main` registers.
  `_agent_admin_turn` assembles the CAD role prompt and run-scoped host tools, and runs
  `save_touched_docs` in `finally`.
- `services/agent/chudcode_host.py` `ChudcodeSessionHost` / `get_default_chudcode_host()` —
  one persistent `chud --mode rpc` process per conversation.
  - The binary resolves via `_resolve_binary` (`CHUDWARE_CHUDCODE_BIN`).
  - Session count is bounded by `CHUDWARE_CHUDCODE_MAX_SESSIONS`.
  - `run_turn` requires a terminal `agent_end`.
- `services/agent/chudcode_tools.py` `build_host_tools` / `dispatch_host_tool` — projects the
  live MCP/mission registries as host tools. `_effective_names` applies the allowlist
  tri-state, mode policy and floors.
  `host_tool_result` always supplies content and details: JSON objects/arrays stay
  structured, plain cad_guide/cad_skill text stays a string. Both are UTF-8 bounded;
  over-budget text becomes `{items: <cut string>, _truncated: {full_result, …}}`,
  with the full JSON string spilled at that path (no invented `ok:true`).
  In eval, await `tool.write`, check outer `hasError` before
  `details.xdev.inner`, then inner `ok:false` before success-only keys.
  Text has no `ok`; complete spills load with `json.load(open(full_result))`.
  A reporting exception is not permission to repeat successful mutations:
  recover recorded replies and inspect current state first.
- `services/agent/chudcode_events.py` `map_rpc_frame` / `RpcFrameState` — the only RPC-frame
  → feed mapper.
- `services/agent/chudcode_control.py` `ControlServer` — the authenticated private
  cancel/steer/status lane; `worker_control_info` exposes it.
- `services/agent/online_turn.py` — `resolve_workspace`, `resolve_settings`,
  `resolve_model_and_effort`, `activate_turn_env`.
  `resolve_workspace` retains the exact canonical selected directory, even beneath
  another project's markers. It does not infer an ancestor root; the same selection
  drives tool scope, save root and Chudcode cwd.
- `services/agent/chudcode_roles.py` `run_role_turn` and
  `mission_dispatch.MissionDispatch` — delegated role/mission turns through Chudcode.
- `services/agent/tool_categories.py` (`MAIN_CHAT_CORE_TOOLS`, `resolve_tool_requests`),
  `copilot_mode_tools.py`, `progress_watchdog.ProgressWatchdog`, and
  `subagent_registry.py` + `subagent_rpc.py`.
- `services/agent/roles.py` (`load_role`, `load_all_roles`, `load_all_playbooks`,
  `validate_suite`, `assemble_context`, `validate_handoff`, `list_editable_prompts`) — loads
  the `roles/*.yaml` cards (`schema: aicad.role/1`; `council_seats.yaml` is data) and
  `playbooks/*.yaml`.
  `general_agent.yaml` requires ownership recovery from roster/history and the
  layout contract after compaction, not replacement on a missing handle. A failed
  provider child is incomplete; takeover requires a stopped owner and released
  document. Children freeze, verify and save revisioned handoffs before parent
  assembly integration/sweeps; no concurrent sweeps against mutating child parts.
  User-requested case/body centering remains distinct from shaft coaxiality.
- `services/agent/skills/builtin/<name>/SKILL.md` — CAD skills fetched via `cad_skill(name)`,
  never `skill://`.
- `services/agent/tool_guidance/` — a curated `prompt_note`/`timeout_s` per tool family
  (`_FAMILIES`). It is layered by `config.get_tool_options` (stock < curated < config.json).
  - `prompt_preview.tool_catalog()` → `ai_tool_catalog` feeds Settings → AI → Tools.
  - `tools/gen_tool_panel_tables.py` bakes the offline fallback
    `host/src/ui/tool_panel_tables.h` + `.snapshot.json`.
  - CAD guidance distinguishes feature rename from document naming and daemon live
    mirroring from legacy handoff publication. `wait_job` is a standalone tool call;
    eval's separate deadline includes bridged waits even when the solve remains running.
- Local execution and non-turn helpers:
  - `agent_turn.run_turn` is the explicit-offline/test provider loop, entered via
    `chudcode_roles._run_offline` → `mission_dispatch.run_linear_role_turn` and gated by
    `config.provider_offline_safe`. Its helpers (`_dispatch_tool`, `resolver_for`,
    guardrails) are shared with the online path.
  - `providers/chudcode_provider.py` `helper_provider` / `ChudcodeProvider` route ECAD
    planning and regeneration diagnosis through `ChudcodeSessionHost.complete_text`.
    During host-tool dispatch they inherit the owning turn's provider/model/credential
    scope, Stop and compaction policy; they have no session/tools/skills/rules and
    memory is off. Outside a turn, governed role/config selections apply.
    Explicit offline/local-only helpers require `provider_offline_safe`, without
    online fallback. Other provider adapters retain explicitly bound standalone/
    bench consumers; do not remove SDK capability merely because chat uses Chudcode.
  - `compaction.py` is the deterministic local compaction; online sessions use Chudcode
    compaction per the user's choice (`config.ONLINE_COMPACTION_METHODS`).
- Online invariants: no implicit turn wall-clock cap; an explicit positive timeout is
  the user's budget. Compaction uses the selected method, never a substituted default.
  Ask/Plan remain read-only after tool loads/floors; advertise and dispatch the same set.
  Terminal persistence saves agent documents, never closes them or overwrites user tabs.

## How it connects
1. Host `launchAgentRunV2` (`host/src/core/jobs.cpp`) sends online sends to
   services-worker `agent_admin_turn` on hostd's agent lane.
2. `ChudcodeSessionHost` drives the Chudcode session; Chudcode owns providers, auth,
   models, history, compaction and nested agents.
3. CAD tools run in-process (`build_host_tools` → `agent_turn._dispatch_tool`). Events map
   back through `chudcode_events` into the `orch_` feed.

Explicit-offline runs go to native `agent_run_v2`/`agent_mission_v1`
(`core/agent/run.cpp`) or the offline role loop. There is never a second online provider
loop.

## Debugging playbook
- `<venv>/python -X utf8 -m pytest services/agent -q` (whole package, as the gate runs it).
  - Online path: `test_chudcode_{host,tools,events,control,roles,questions,progress,compaction_policy}.py`,
    `test_worker_rpc_chudcode.py`, `test_online_compaction.py`.
  - Registry/projection: `test_subagent_registry.py`, `test_mission_dispatch.py`,
    `test_roles.py` (headless), `test_tool_guidance.py`.
- Headless real turn: `scripts/run_mission.py --workspace <project> --prompt-file p.md --model <id>`
  (`--resume` for follow-ups). It uses the user's Chudcode auth.
- Gotcha: after adding or renaming a tool:
  - add its `prompt_note` in `tool_guidance/<family>.py`;
  - re-run `tools/gen_tool_panel_tables.py`;
  - `scripts/check.py` `tool_panel_sync` / `advertised_tool_audit` fail until both are done.
- Gotcha: a new stateful agent RPC family must join hostd's agent-lane prefixes
  (`WorkerBridge::forMethod`).
- Gotcha: prompt overrides are split by consumer:
  - `appdata/prompts/<id>.md` is for native C++;
  - `<id>.mission.md` is for YAML roles.
  Never collapse them.

## Graphify starters
- graphify query "ChudcodeSessionHost run_turn agent_admin_turn"
- graphify explain "build_host_tools"
- graphify path "worker_rpc.py" "chudcode_host.py"

## Related tasks/docs
- services/agent/AGENTS.md, AGENTS.md (online-agent split), ARCHITECTURE.md (component map)
- docs/design/PLATFORM_ADR.md, docs/AI_TOOLING_MASTER_PLAN.md (pillars 8–14)
