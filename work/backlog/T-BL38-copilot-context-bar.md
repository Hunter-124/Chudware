---
id: T-BL38
title: Copilot context bar — auto-track selected model's context window, place between model picker and send, constrain picker width, k-token count + click-through breakdown
workstream: backlog
owner: unassigned
status: todo
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

- [ ] **Fix the core bug:** the context bar resolves the SELECTED model's max context
      window and updates when the model picker selection changes. Root cause is likely a
      missing/!wired per-model context-length lookup — find where models are defined
      (providers/models registry, T-F9) and ensure each carries its context length; wire
      the bar to `selected_model.context_length`. If the registry lacks the field, add it
      (do not hard-code per-model magic numbers in the GUI).
- [ ] **Layout:** input row is `[model picker] [context bar] [send button]`, bar between
      picker and send.
- [ ] **Constrain model-picker width:** give the picker a max width (ellipsize long model
      names) so it never overlaps the context bar. Verify with the longest available model
      name.
- [ ] **k-token display:** show the real current context usage in k tokens (e.g. `12k` or,
      preferred, `12k / 200k` used-vs-max for the selected model). Number must reflect the
      actual live session context, and change as the conversation grows.
- [ ] **Button + animation:** the context bar is a real button with the standard hover/
      press animations (match T-G16 liquid-glass button styling / T-G17 Copilot chrome; if
      T-BL36 lands a shared animated-state treatment, reuse it).
- [ ] **Breakdown window:** clicking opens a detailed breakdown popup — token counts by
      category the session actually tracks (system prompt, conversation history, current
      input, tool/MCP tool definitions, etc.). Do NOT fabricate numbers — show what the
      agent session genuinely accounts for; if a category isn't tracked, either wire the
      count or omit it honestly.
- [ ] Screenshots light+dark: the input row (picker + bar + send, NO overlap, longest model
      name) and the breakdown popup. Eyeball. Confirm the number updates (a) as context
      grows and (b) when switching models.
- [ ] flock-serialized GUI build; scripts/gate.sh + `graphify update .` before review.

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
