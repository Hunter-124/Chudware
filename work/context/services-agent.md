# services/agent — built-in agent loop & provider adapters

Scope: services/agent/** (Python service root `agent` symlink — the autonomous design loop that generates build123d code from a natural-language request)
Contracts: contracts/worker_rpc.md (protocol 2), contracts/events.md

## Entry points
- services/agent/run_agent.py:17 `main()` — CLI entry (`python -m agent.run_agent "<request>"`)
- services/agent/loop.py:1044 `run()` — one-shot design API (stateless); entry point from CLI/tests
- services/agent/loop.py:835 `run_turn()` — per-turn refinement loop (takes a persistent `Session`); the core orchestrator
- services/agent/loop.py:707 `Session` — conversation state, live across follow-up requests ("make it 30mm taller")
- services/agent/config.py:112 `get_provider()` — resolve the active backend (Anthropic/OpenAI/local LM Studio; offline mode enforcement at C35)
- services/agent/providers/base.py:27 `Provider` — abstract vision+text chat backend; concrete impls: AnthropicProvider, OpenAIProvider, GeminiProvider, LlamaCppProvider, MockProvider
- services/agent/roles.py `load_role()` / `load_all_playbooks()` / `validate_suite()` / `assemble_context()` / `validate_handoff()` / `schema_to_gbnf()` (T-W7) — loads the 13 authored role cards (services/agent/roles/*.yaml, verbatim) + 6 playbooks (services/agent/playbooks/*.yaml); validates schema/allowlist/stage-vocab; assembles per-turn context in contract §9.1 order; validates handoff envelopes + emits a llama.cpp GBNF grammar per handoff schema. Hosts: `loop.run(..., role=)` and services/dag/node_runner.py (node `role:` field).

## How it connects
The loop drives fully autonomous part design: user request → `run_turn()` → ask provider for build123d code (using SYSTEM_PROMPT) → compile via `kernel.run_code_multi()` → render viewport (multi-view PNG via engine/render.py) → feedback the screenshot to the model → model self-corrects or declares done → save contact sheet + final code to `%APPDATA%/AI-CAD/cache/`. Integrated with DAG orchestrator (docs/PLATFORM_ADR.md pillar "DAG workflow orchestrator") via `services/mcp/` tool registration; the orchestrator's `run_design` node (core/agent/run.cpp) delegates to this loop when agentic workflow is required. Provider-agnostic abstraction allows swapping Anthropic/OpenAI/local LM without touching the loop's logic; offline mode (C35) enforces loopback-only endpoints for ITAR compliance.

## Debugging playbook
- `AI_CAD_PROVIDER=mock python -m agent.run_agent "a 50mm cube"` — full loop with deterministic mock, no API key
- `pytest services/agent/test_loop_mock.py::test_happy_path -xvs` — end-to-end orchestration self-test
- `pytest services/agent/test_compaction.py -xvs` — token overflow + context window compaction edge cases
- Gotcha: offline mode is enforced twice: at provider resolution (config.py:132) AND at loop start (config.py:847); disable via config.json `"offline_mode": false` or `AI_CAD_PROVIDER` env var override
- Gotcha: `MockProvider` replies use fixed scripts (test_loop_mock.py:19); for real design, configure ANTHROPIC_API_KEY env var or %APPDATA%/AI-CAD/config.json
- `pytest services/agent/test_roles.py -q` — role cards + playbooks + handoff/GBNF machinery (T-W7), fully headless (no engine, no LLM); includes a self-contained GBNF acceptor
- Gotcha (T-W7): role allowlist enforcement is bound via `mcp_server.tool_registry.set_policy(allow=...)` — the ONE dispatch chokepoint; the loader flags `delegate`/`workspace_status` as not-yet-registered (they land with sibling W tasks), not as errors

## Graphify starters
- graphify explain "built-in agent loop"
- graphify query "loop.py run_turn Session provider complete"
- graphify path "run_agent.py" "kernel.run_code_multi"

## Related tasks/docs
- work/tasks: T-F* tasks (AI experience workstream; T-F9 ≈ agentic workflow UX); T-A* tasks (DAG orchestrator, which calls `run_turn()` from core/agent/run.cpp)
- docs/PLATFORM_ADR.md (pillar "DAG workflow orchestrator" + pillar 14 "agentic workflow completion")
- docs/AI_TOOLING_MASTER_PLAN.md (pillars 8–14: MCP integration, copilot modes, offline design)
- memory/ai-tooling-master-plan.md (quick ref: no subscription-OAuth reuse, MCP-first, Qwen3-4B translator)
