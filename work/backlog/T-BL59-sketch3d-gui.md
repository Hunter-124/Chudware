---
id: T-BL59
title: 3D sketcher P4 — GUI viewport mode (3D dragging) + GSL describe
workstream: backlog
owner: unassigned
status: backlog
branch: task/T-BL59-sketch3d-gui
depends_on: [T-BL57, T-BL48]
lane: app-gui
kind: new
priority: 4
deferred: yes
---

## Context

Phase 4 of the 3D sketcher plan (`docs/SKETCH3D_PLAN.md`, T-BL45). PARKED until greenlight.
Depends on P2 (T-BL52) for the solver+entities and on T-BL48 (sketch-entry wiring) — see
`uiimgui-sketcher-entry-unwired`: interactive sketch dispatch in `ui/imgui` must be wired first.

## Scope

- `sketch3d` viewport mode mirroring `host/src/ui/sketch_mode.cpp`.
- 3D dragging: handle moves in the camera-facing plane by default, with X/Y/Z / datum-plane
  locks via modifiers; reuses the constrained-drag solver generalized to 3-vectors (IN-PROCESS —
  interactivity is why build-own beat subprocess, T-BL45 §3.1).
- Plane-free constraint toolbar verbs (parallel/perp/coincident/on-plane); reference geometry
  pickable as constraint targets.
- `describe_sketch3d` in `services/mcp/gsl_tools.py` for text-only agents.

## Acceptance

- User draws + drags a 3D sketch in-app; drag stays interactive (no IPC stall).
- Text-only `describe_sketch3d` reads the sketch.
- `bash scripts/gate.sh` green; `graphify update .`.
