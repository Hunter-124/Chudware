---
id: T-BL16b
title: GUI viewport playback/scrub for a simulate_motion trajectory
workstream: backlog
owner: opus-sweep
status: doing
branch: task/T-BL16b-motion-playback-gui
depends_on: [T-BL16]
lane: app-gui
kind: new
priority: 4
---

## Context

**Follow-up to T-BL16 (backend shipped 2026-07-04).** T-BL16 landed the `simulate_motion` MCP
tool: it drives one mate over a range, re-solves the mate graph each step, and returns a
trajectory + swept interference. The backend is complete and tested; this task adds the
**viewport playback/scrub UI** so a human can watch the mechanism move (T-BL16's DoD explicitly
allowed deferring the GUI to a follow-up as long as the seam is documented — it is, below).

## The seam (what the GUI consumes — already stable, no backend change needed)

`simulate_motion(...)` returns JSON:

```
{ "ok": true, "doc_id": "...", "mate_id": "m1",
  "driver": {"type":"angle","unit":"deg","start":0,"stop":360,"steps":48,"samples":48,"loop":true},
  "trajectory": [
    { "i": 0, "value": 0.0, "converged": true, "dof": 0, "interference": false,
      "bodies": [ {"component":"k1","name":"Ground","transform":[16 floats row-major 4x4]},
                  {"component":"k2","name":"Arm",   "transform":[...]}, ... ] },
    { "i": 1, "value": 7.5, ... },
    ...
  ],
  "interference": {"checked":true, "first_collision_step":31, "first_collision_value":232.5,
                   "detail":[{"a":"Arm","b":"Obstacle","volume":...}]},
  "summary": "..." }
```

Each `trajectory[i].bodies[j].transform` is body j's **world 4x4 (row-major 16)** at sample i —
exactly the per-body pose the viewport needs to place each body for that frame. This is the SAME
transform layout the assembly renderer already applies (`doc_tree`'s `components[].transform`,
`asm_world_bodies`), so the GUI does not need new geometry — only to re-apply these transforms
per frame. NOTE: `asm_world_bodies` returns only a bbox and is NOT sufficient to orient a body;
the trajectory carries the full transform precisely so the GUI can animate rotation, not just
translation.

## Definition of done

- [ ] A playback control in the viewport (play/pause, a scrub slider over `trajectory` samples,
      speed) that, for the current frame `i`, applies each `bodies[j].transform` to the placed
      assembly bodies and redraws — animating the mechanism through the solved trajectory.
- [ ] Steps flagged in `interference` (and `first_collision_step`) are marked on the timeline
      (e.g. a red tick) so the user sees WHERE it clashes; scrubbing to that frame shows the clash.
- [ ] The GUI obtains a trajectory by calling `simulate_motion` (via the app's MCP/agent path or
      a direct engine call) — no re-implementation of the solve/interference loop in the GUI.
- [ ] Verification is HEADLESS only (xvfb / AI_CAD_SHOT), worker built from the task branch (NOT
      demo mode); light + dark screenshots of a frame mid-motion, eyeballed. (See team memory
      "GUI migration shot-verify blocker" + the foreman's demo-mode-is-not-verification rule.)

## AI hints

- `simulate_motion` (T-BL16, services/mcp/cad_tools.py) — the backend + the exact return schema
  above; `cad_guide('motion')` documents it.
- The assembly render path already applies `components[].transform`; reuse it per frame.
- `graphify query "viewport assembly render transform"` before touching the viewport code.
```
