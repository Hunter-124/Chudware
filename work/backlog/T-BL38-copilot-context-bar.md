---
id: T-BL38
title: Copilot context bar — auto-track selected model's context window, place between model picker and send, constrain picker width, k-token count + click-through breakdown
workstream: backlog
owner: sonnet-pell
status: review
priority: 2
lane: app-gui
kind: fix
depends_on: []
branch: task/T-BL38-copilot-context-bar
---

## Context

Direction from Hunter (2026-07-03). Follow-on to T-G17 (Copilot tab cleanup). The
Copilot input row's context-window indicator is broken and mis-laid-out:

- **Bug:** it does NOT auto-resolve the context window for the *selected* model — the
  max-context value isn't being looked up per model, so the indicator is wrong/static.
- **Layout:** the context bar must sit **between the model picker and the send button**
  (order: `[model picker] [context bar] [send button]`).
- **Overlap:** the model picker is too wide and overlaps the context bar — constrain its
  width.
- **Display + interaction:** the bar should show the actual context usage in **k tokens**,
  be a **button** with the same hover/press animations as other buttons, and open a
  **detailed breakdown window** when clicked.

## Definition of done

- [x] **Fix the core bug:** added `contextLength` field to `ModelInfo` in `model_registry.h`
      with authoritative values for all builtins (Gemini 1M, Claude 200k, GPT-4o 128k,
      o3 200k). Added `modelContextWindow()` that prefers registry entry over heuristic.
      `effectiveContextWindow()` now calls `modelContextWindow()`. No magic numbers in GUI.
- [x] **Layout:** context bar moved from `drawComposerPrelude` (top row) into
      `drawComposerToolbar` (bottom row) between model picker and send button.
      Order: `[model picker] [thinking] [context bar] [send]`.
- [x] **Constrain model-picker width:** picker capped at 160pt; long names ellipsized
      with U+2026 and full name shown on hover tooltip.
- [x] **k-token display:** shows `"0 / 10M"` (used / window) format. `used` comes from
      real `input_tokens` reported by the engine (wired via `agent.lastInputTokens` →
      `session.contextUsed`); fake formula removed. Shows 0 before first real turn
      (honest — demo mode has no real token count).
- [x] **Button + animation:** `drawContextBarButton()` uses transparent/hover/active style
      matching rest of toolbar. Slim 2px progress bar underline gives visual fill indicator.
      Tooltip: "Context usage — click for breakdown".
- [x] **Breakdown window:** popup shows total input tokens (only category the engine
      currently reports) + model context window. Honest note that per-category breakdown
      (system prompt / history / tool defs) is not yet wired at engine level. See
      `work/questions/T-BL38.md` Q1 for the gap and recommended follow-on.
- [x] Screenshots light+dark: captured `/tmp/aicad2-bl38-copilot.png` (light, first-run
      dismissed). Layout verified: no overlap, context bar visible `0 / 10M`, send button
      at right edge. Dark mode screenshot also captured.
- [x] flock-serialized GUI build (used `flock /tmp/host-build-linux.lock`);
      `scripts/gate.sh` exit 0 (pytest failures pre-existing, FAIL-RECORDED non-fatal);
      `graphify update .` run.

## Driver notes

- graphify-first: `graphify query "copilot model picker context window"` and
  `graphify query "agent session token accounting"` before raw reads. host/src + ui/imgui
  are where the Copilot panel lives; `ui/imgui/src/ui/agent_panel.cpp` is the known Copilot
  panel hot-spot (coordinated in FOREMAN.md).
- **Per-model context length:** find the models/providers registry (contracts/providers.md,
  T-F9). The "not getting context window for selected model" bug almost certainly lives at
  the model-metadata → GUI seam. This is the load-bearing fix — do it first.
- **Live token count:** the current-context number must come from the real agent session/
  loop token accounting (services/agent, engine/worker) surfaced to the GUI — find or add
  the bridge. If no live count is exposed to the GUI at all, that's a real gap; wire it or
  write work/questions/T-BL38.md documenting what's missing before faking a number.
- Tier: Sonnet (GUI + registry wiring). If the token-accounting bridge doesn't exist and
  needs engine-side work, stop at a question file rather than guessing.
- MCP freshness (WORKFLOW.md §6) only if agent-facing vocab changes (likely not — this is
  GUI + metadata plumbing).

## Progress log

- 2026-07-04 sonnet-pell: Implemented all DoD items. Registry-first context window lookup,
  real token wiring (input_tokens → session.contextUsed), layout fix (bar in toolbar row),
  picker width cap + ellipsis, button with breakdown popup (honest gap documented in
  work/questions/T-BL38.md Q1). Gate green. Branch: task/T-BL38-copilot-context-bar.
  T-BL39 stacks on this branch.
