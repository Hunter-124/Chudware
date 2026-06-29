# services/agent — built-in agent loop

**Owner:** N5e (C++ cutover) / current Python implementation.

Drives the Copilot panel: `generate → run_model → screenshot → vision self-check`.
Provider adapters in `providers/` (Anthropic, OpenAI, Gemini, llamacpp, mock).

## Entry points
- `python -m agent.run_agent "<prompt>"`
- `python -m agent.test_loop_mock`

## Migration
- Python now → C++ agent loop in `core/agent/` at N5e.
- Events contract: `contracts/events.md`
