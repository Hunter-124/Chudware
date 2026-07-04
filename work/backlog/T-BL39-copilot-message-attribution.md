---
id: T-BL39
title: Copilot messages — remove the "Assistant"/"Hi! Describe a part…" greeting, show the generating model(s) as attribution, compact the user/model display
workstream: backlog
owner: sonnet-pell
status: doing
priority: 3
lane: app-gui
kind: overhaul
depends_on: []
branch: task/T-BL39-copilot-message-attribution
---

## Context

Direction from Hunter (2026-07-03). Follow-on to T-G17 (Copilot cleanup). The Copilot
conversation has an empty-state greeting and generic "Assistant" attribution that Hunter
wants replaced with real, compact model attribution.

## Definition of done

- [ ] **Remove the empty-state greeting** — the "Assistant" header + "Hi! Describe a
      part…" placeholder block in the Copilot tab. (Decide the empty state: either nothing,
      or a minimal hint — but not the current greeting card.)
- [ ] **Model attribution instead of "Assistant":** each assistant message shows the actual
      **model(s) that generated that output**, not the word "Assistant". Read the real model
      from the message's model metadata — the routing/telemetry seam that landed in T-BL22
      stamps `model_info` / adapter on each turn (`_emit_ai_artifact`, T-H1 envelope). If
      multiple models contributed (routing / multi-agent), show them. Do NOT hardcode a
      model name — read what actually produced the message.
- [ ] **Compact the user text / model display:** tighter vertical rhythm on the user message
      and the model attribution line (smaller/secondary attribution, less padding) so the
      conversation is denser. Keep it readable in dark mode (see T-BL33 — no black-on-black).
- [ ] Screenshots light+dark of a real multi-turn conversation; eyeball. Confirm the model
      attribution reflects the actually-used model and updates if the model changes mid-session.
- [ ] flock GUI build; scripts/gate.sh + `graphify update .` before review.

## Driver notes

- host/src + ui/imgui; `ui/imgui/src/ui/agent_panel.cpp` is the Copilot panel hot-spot.
- Model metadata source: the ai_artifact envelope / `model_info` from T-H1 + T-BL22's
  live routing tag. `graphify query "ai artifact model_info telemetry"` to find where the
  per-turn model is recorded and how the GUI can read it.
- Coordinate with T-BL38 (context bar / model picker in the input row) — both surface
  "which model"; keep the attribution style consistent between the message list and the
  input row. If T-BL38 lands first, reuse its model-name resolution.

## Progress log
