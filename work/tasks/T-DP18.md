---
id: T-DP18
title: AI time-remaining estimator — tokens/s x expected tokens per subtask
workstream: DP
owner: sonnet-fenn1
status: doing
branch: task/T-DP18-time-estimator
depends_on: []
lane: ai-copilot
kind: new
priority: 2
---

## Context

Presentation layer of the guided design pipeline — see
[docs/AGENT_DESIGN_PIPELINE.md](../../docs/AGENT_DESIGN_PIPELINE.md) §9 (Hunter directive,
2026-07-05). MANDATORY: graphify to orient before editing; REUSE existing ui/imgui workspace
machinery (workspace_bar.cpp, doc-tabs, the ECAD full-viewport takeover pattern, the glass
copilot overlay column, agent_panel, watch_grid), aicad.mission event streams,
aicad.delegation/1 tree, aicad.buildgraph/1. ui/imgui is the ONLY live GUI (host/ reference-
only). Design language LOCKED: pinned glass columns, no docked layout, dark-mode contrast per
the T-U1 kit, ui_style_check clean. Verify HEADLESS only (xvfb-run AI_CAD_SHOT=...), never
launch headed; EYEBALL every shot PNG. Numbers cite real output.

## Scope

Doc §9.4. Live ETA-to-part-complete = remaining_tokens / effective_tokens_per_sec.
remaining_tokens = sum over not-done subtasks of expected token cost (from gym-measured
per-role/per-stage token tables — the W31/W32 measurements, fed by the tuning loop T-DP10);
effective_tokens_per_sec = live aggregate rate across running slots (CUDA server throughput).
Backend calc (python) + a status-bar clock (whole mission) + per-node ETAs surfaced to the DAG
widget (T-DP17). Honest about uncertainty: show a RANGE when a subtask has few samples; never
invent a number. Frontier runs use the provider's observed rate. Mostly backend + a small
status-bar element — low GUI collision, can start now.

## Definition of done

- [ ] Estimator computes mission ETA from measured per-role token costs + live tok/s (tests
      with recorded fixtures — real measured numbers, uncertainty range when sparse).
- [ ] Status-bar clock shows whole-mission ETA; per-node ETA exposed for the DAG widget.
- [ ] Degrades honestly (range/unknown) with no history; frontier uses observed rate.
- [ ] gate.sh PASS; graphify update.
