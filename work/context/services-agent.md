# services/agent — built-in agent loop & provider adapters

Scope: services/agent/** (Python service root `agent` symlink — agent support services: provider adapters, agent_turn, roles/playbooks; the autonomous design loop itself is C++ in core/agent/)
Contracts: contracts/worker_rpc.md (protocol 2), contracts/events.md

## Entry points
- (T-RF5: the legacy build123d design loop — `run_agent.py`, `loop.run`/`run_turn`/`Session`, the `agent_design` RPC — is DELETED. The C++ core's `agent_run_v2` is the single agent entry; `loop.py` keeps only the legacy prompt constants + `apply_context_refs` for `engine.worker._get_prompts` / `agent_turn.py`.)
- services/agent/config.py:112 `get_provider()` — resolve the active backend (Anthropic/OpenAI/local LM Studio; offline mode enforcement at C35)
- services/agent/providers/base.py:27 `Provider` — abstract vision+text chat backend; concrete impls: AnthropicProvider, OpenAIProvider, GeminiProvider, LlamaCppProvider, MockProvider
- services/agent/local_runtime.py `ensure_running(model_id=)` (T-W26/T-W31) — bundled llama.cpp lifecycle; delegates spawn to services/installer/llamacpp.py which now auto-selects the CUDA build (`build-cuda/bin`) when an NVIDIA GPU is present (CHUDWARE_LLAMA_VARIANT=cuda|cpu override, graceful CPU fallback) and defaults the integrator-verified smoke config (-ngl 99, --parallel 8 -c 32768) on CUDA; pin = b9860/fdb1db8 (`LLAMA_CPP_PINNED_BUILD`)
- services/agent/model_manager.py `structured_output_for()` / `model_for_role_shape()` (T-W31, W29 mapping) — per-model structured-output capability (qwen3-4b = prompted_json fan-out, GBNF broken; gemma3-4b-qat = gbnf worker); tiers.yaml `local_models:` block + `tiers.load_local_models()` carry the shipped role-shape defaults
- services/agent/roles.py `load_role()` / `load_all_playbooks()` / `validate_suite()` / `assemble_context()` / `validate_handoff()` / `schema_to_gbnf()` (T-W7) — loads the 13 authored role cards (services/agent/roles/*.yaml, verbatim) + 6 playbooks (services/agent/playbooks/*.yaml); validates schema/allowlist/stage-vocab; assembles per-turn context in contract §9.1 order; validates handoff envelopes + emits a llama.cpp GBNF grammar per handoff schema. Host: services/dag/node_runner.py (node `role:` field).
- services/agent/tool_guidance/ — built-in curated per-tool options (one-line `prompt_note` for every registry tool + `timeout_s` on the slow FEA/CFD/CAM/EM/reveng/DAG calls). Layering in `config.get_tool_options`: stock defaults < curated guidance < config.json (user wins per-field; `""` silences a note). One module per family in `_FAMILIES`; `tool_schemas` renders the note as the `Note:` line in the tools slot; the Settings → AI → Tools panel shows curated values as dimmed placeholders.
- Tools panel data path: `prompt_preview.tool_catalog()` (worker RPC `ai_tool_catalog`) feeds the live Settings → AI → Tools tree; `tools/gen_tool_panel_tables.py` (venv) bakes the offline fallback `host/src/ui/tool_panel_tables.h` + `.snapshot.json`. After adding/renaming a tool: add its `prompt_note` in `tool_guidance/<family>.py` (new family → new module + `_FAMILIES` entry + `config.DEFAULT_TOOL_FAMILIES`) and re-run the generator. Gates: `scripts/check.py` tool_panel_sync, `services/agent/test_tool_guidance.py` (registry set is frozen at collection import — in-test fake registrations cannot flake it).
- `compaction.py` — context-window compaction: `snapcompact_archive()` (text-phase cut, canonical local semantics) + `reinject_archive()`. Under `CHUDWARE_COMPACTION_BACKEND=omp` + a vision provider, the archive is ADDITIONALLY rendered to OMP snapcompact PNG frames via the bridge `snap_render` RPC (text→image only; the old full-bridge `compact` path is deleted). OG source + frames persist in `snap_store.py` (content-addressed, `<appdata>/snaps/<snap_id>/`); a `[snap:<id> api=… model=…]` stamp line leads the reinjected message so the next round re-snaps cumulatively (`_snap_anchor`), and a provider switch re-renders the stored OG in the new provider's shape (`_resnap_for_provider`). `snap_spec.py` maps provider→(api, model, vision, image budget). Knobs: `agent.compaction.{snap_frames,snap_dir,snap_max_frames}`. Any bridge/store failure degrades to text-only with `archive["warning"]`.

## How it connects
Autonomous part design runs in the C++ core (`core/agent/run.cpp`, `agent_run_v2` — doc-ops orchestrator with modes, missions via `agent_mission_v1`); the GUI launches it via `launchAgentRunV2` (host/src/core/jobs.cpp) and streams orch_-prefixed events to the ActivityPanel. This Python package keeps the provider adapters, `agent_turn.py` (single-turn tool-calling for the DAG/PLM surfaces), roles/playbooks, and support services. Provider-agnostic abstraction allows swapping Anthropic/OpenAI/local LM without touching agent logic; offline mode (C35) enforces loopback-only endpoints for ITAR compliance.

## Debugging playbook
- `pytest services/agent/test_compaction.py -xvs` — token overflow + context window compaction edge cases
- `pytest services/agent/test_snap_compaction.py -q` — snap compaction with a faked bridge (store round-trip, cumulative re-snap chain, provider-switch re-snap, budgets, text fallbacks); `CHUDWARE_SNAP_LIVE=1` enables the live-bridge render test
- Gotcha (snap): the snap store root resolves via `agent.compaction.snap_dir` config → `CHUDWARE_SNAP_DIR` env → `<appdata>/snaps`; on legacy installs that's `~/.local/share/AI-CAD/snaps`. OMP `planArchive` images only text beyond ~2 edge capacities, so small archives legitimately store 0 frames (everything rides the text edges)
- Gotcha: offline mode is enforced twice: at provider resolution (config.py:132) AND at loop start (config.py:847); disable via config.json `"offline_mode": false` or `CHUDWARE_PROVIDER` env var override
- Gotcha: `MockProvider` replies use fixed scripts; for real design, configure ANTHROPIC_API_KEY env var or %APPDATA%/Chudware/config.json
- `pytest services/agent/test_roles.py -q` — role cards + playbooks + handoff/GBNF machinery (T-W7), fully headless (no engine, no LLM); includes a self-contained GBNF acceptor
- Gotcha (T-W7): role allowlist enforcement is bound via `mcp_server.tool_registry.set_policy(allow=...)` — the ONE dispatch chokepoint; the loader flags `delegate`/`workspace_status` as not-yet-registered (they land with sibling W tasks), not as errors

## Graphify starters
- graphify explain "built-in agent loop"
- graphify query "agent_turn provider complete tool calls"

## Related tasks/docs
- work/tasks: T-F* tasks (AI experience workstream; T-F9 ≈ agentic workflow UX); T-A* tasks (DAG orchestrator; agent runs execute in core/agent/run.cpp)
- docs/PLATFORM_ADR.md (pillar "DAG workflow orchestrator" + pillar 14 "agentic workflow completion")
- docs/AI_TOOLING_MASTER_PLAN.md (pillars 8–14: MCP integration, copilot modes, offline design)
- memory/ai-tooling-master-plan.md (quick ref: no subscription-OAuth reuse, MCP-first, Qwen3-4B translator)
