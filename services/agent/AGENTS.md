# services/agent — agent support services (providers, roles, turns)

**Owner:** N5e (C++ cutover) / current Python implementation.

The single agent entry is the C++ core (`core/agent/run.cpp`, `agent_run_v2`); the legacy
build123d code-gen loop (`loop.py` `run`/`run_turn`, the `agent_design` RPC, and the
`run_agent.py` CLI) was removed in T-RF5. What remains here: provider adapters in
`providers/` (Anthropic, OpenAI, Gemini, llamacpp, mock), `agent_turn.py` (single-turn
tool-calling used by the DAG/PLM surfaces), roles/playbooks, and `loop.py` reduced to the
legacy prompt constants + `apply_context_refs` (kept only for `engine.worker._get_prompts`
and `agent_turn.py`; shrinks to nothing once those migrate).

## Entry points
- `pytest services/agent -q` — the support-services suite

## Migration
- Python now → C++ agent loop in `core/agent/` at N5e.
- Events contract: `contracts/events.md`
