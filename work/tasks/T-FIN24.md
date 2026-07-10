---
id: T-FIN24
title: Simulate tab multi-physics tree (ui/imgui) - all studies + handoff actions (supersedes T-SIM10)
workstream: FIN
owner: unassigned
status: todo
branch: task/T-FIN24-simulate-tab-multiphysics
depends_on: [T-FIN1, T-FIN15, T-FIN16]
lane: app-gui
kind: overhaul
priority: 2
---

## Context

Retarget T-SIM10 (written against deleted host/ paths) to the imgui GUI and extend
it with the new content: one Simulate workspace hosting every study kind — FEA (all 7 incl.
transient thermal), CFD fast/accurate, motion (now dynamics), electronics sim, firmware
emulation, EM (wire the live EM backend's three forms — closes the EM toast-only gap) —
with progressive disclosure by open doc types, per T-SIM10's original spec. New: handoff
actions in the study tree ('Send board to thermal FEA', 'Pick heatsink', 'Send reactions
to static FEA', 'Verify vs 1D') driven by the simhandoff/1 envelopes so cross-sim flows
are one click in the GUI, and study provenance shown on results.

## Files / areas touched

- ui/imgui/ — Simulate workspace setup tree + study forms + handoff buttons
- services/worker — endpoints for new handoff tools
- scenario shots: disclosure matrix (mech-only vs PCB open), a handoff round trip
- mark T-SIM10 superseded (note + deferred) when this lands

## Definition of done

- Every live study kind creatable+runnable from the tab; honest 'engine pending' states where gated
- Disclosure matrix verified by headless shots (both cases)
- ECAD->thermal handoff round trip works from the GUI including heatsink pick
- EM forms run against the live backend (toast-only gap closed); light+dark verified
