# services/agent — built-in agent loop & provider adapters

Scope: services/agent/** (Python service root `agent` symlink — agent support services: provider adapters, agent_turn, roles/playbooks; the autonomous design loop itself is C++ in core/agent/)
Contracts: contracts/worker_rpc.md (protocol 2), contracts/events.md

## Entry points
- (T-RF5: the legacy build123d design loop — `run_agent.py`, `loop.run`/`run_turn`/`Session`, the `agent_design` RPC — is DELETED. The C++ core's `agent_run_v2` is the single agent entry; `loop.py` keeps only the legacy prompt constants + `apply_context_refs` for `engine.worker._get_prompts` / `agent_turn.py`.)
- services/agent/config.py:112 `get_provider()` — resolve the active backend (Anthropic/OpenAI/local LM Studio; offline mode enforcement at C35)
- services/agent/providers/base.py:27 `Provider` — abstract vision+text chat backend; concrete impls: AnthropicProvider, OpenAIProvider, GeminiProvider, LlamaCppProvider, MockProvider
- services/agent/local_runtime.py `ensure_running(model_id=)` (T-W26/T-W31) — bundled llama.cpp lifecycle; delegates spawn to services/installer/llamacpp.py which now auto-selects the CUDA build (`build-cuda/bin`) when an NVIDIA GPU is present (AICAD_LLAMA_VARIANT=cuda|cpu override, graceful CPU fallback) and defaults the integrator-verified smoke config (-ngl 99, --parallel 8 -c 32768) on CUDA; pin = b9860/fdb1db8 (`LLAMA_CPP_PINNED_BUILD`)
- services/agent/model_manager.py `structured_output_for()` / `model_for_role_shape()` (T-W31, W29 mapping) — per-model structured-output capability (qwen3-4b = prompted_json fan-out, GBNF broken; gemma3-4b-qat = gbnf worker); tiers.yaml `local_models:` block + `tiers.load_local_models()` carry the shipped role-shape defaults
- services/agent/roles.py `load_role()` / `load_all_playbooks()` / `validate_suite()` / `assemble_context()` / `validate_handoff()` / `schema_to_gbnf()` (T-W7) — loads the 13 authored role cards (services/agent/roles/*.yaml, verbatim) + 6 playbooks (services/agent/playbooks/*.yaml); validates schema/allowlist/stage-vocab; assembles per-turn context in contract §9.1 order; validates handoff envelopes + emits a llama.cpp GBNF grammar per handoff schema. Host: services/dag/node_runner.py (node `role:` field).

## How it connects
Autonomous part design runs in the C++ core (`core/agent/run.cpp`, `agent_run_v2` — doc-ops orchestrator with modes, missions via `agent_mission_v1`); the GUI launches it via `launchAgentRunV2` (host/src/core/jobs.cpp) and streams orch_-prefixed events to the ActivityPanel. This Python package keeps the provider adapters, `agent_turn.py` (single-turn tool-calling for the DAG/PLM surfaces), roles/playbooks, and support services. Provider-agnostic abstraction allows swapping Anthropic/OpenAI/local LM without touching agent logic; offline mode (C35) enforces loopback-only endpoints for ITAR compliance.

## Debugging playbook
- `pytest services/agent/test_compaction.py -xvs` — token overflow + context window compaction edge cases
- Gotcha: offline mode is enforced twice: at provider resolution (config.py:132) AND at loop start (config.py:847); disable via config.json `"offline_mode": false` or `AI_CAD_PROVIDER` env var override
- Gotcha: `MockProvider` replies use fixed scripts; for real design, configure ANTHROPIC_API_KEY env var or %APPDATA%/AI-CAD/config.json
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
