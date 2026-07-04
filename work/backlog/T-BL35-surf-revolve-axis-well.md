---
id: T-BL35
title: Surfaces — surf_revolve axis well rejects datum axis (K_EDGE→K_EDGEAXIS) + gate readyForPreview on the axis well
workstream: backlog
owner: mooseinatree
status: doing
priority: 3
lane: app-gui
kind: fix
depends_on: []
branch: task/T-BL35-surf-revolve-axis-well
---

## Context

T-G20 cheap-fix #1 (surfaces audit). Split out of T-BL28 (2026-07-03): T-BL28 fixed
ruled/fill/boundary engine-side via Option A and correctly did NOT touch the forms, so
this form-layer fix — folded into T-BL28's scope by an earlier foreman note — was never
done there. It is a genuinely separate bug (the surf_revolve axis well), tracked here so
it isn't lost.

Full evidence: `work/questions/T-G20-surfaces-audit.md` "Recommended cheap fix #1".

The surf_revolve form's axis well is `K_EDGE` (`host/src/ui/pm_forms.cpp` ~L681), so it
can't accept a datum axis (`axis.z`); the solid `revolve` form (L228) already uses
`K_EDGEAXIS`. Additionally `readyForPreview` gates surf_revolve only on the `profile` well
(~L1815), so OK can fire with no axis → engine "axis required" error.

## Definition of done

- [ ] `pm_forms.cpp` ~L681: surf_revolve axis well `K_EDGE` → `K_EDGEAXIS` (mirror the
      solid `revolve` form at L228). Engine already accepts a datum-axis ref for
      surf_revolve (`routeDatumAxis` L1163 fills `selKinds.axis` with
      `{kind:datum,feature:axis.z}` — confirmed via MCP in T-G20).
- [ ] Gate `readyForPreview` on the axis well (~L1815) so OK cannot fire without an axis.
- [ ] Live-pick GUI verify: build under the host-build lock, drive surf_revolve with a
      real datum-axis pick to a successful surface, screenshot light+dark, LOOK at every
      PNG. (This one DOES need the live check — it's a form-behavior change, unlike
      T-BL28's engine-only fix.)
- [ ] scripts/gate.sh + `graphify update .` before review.

## Driver notes

Small, but needs a GUI build to verify — flock-serialize. host/src is NOT graphify-indexed;
targeted reads at the cited lines are expected. Could share a build cycle with T-BL33
(other GUI form/chrome fixes) if a driver picks up both.

## Progress log
- 2026-07-04 04:42 UTC [mooseinatree] Both form-layer fixes done and committed on task/T-BL35-surf-revolve-axis-well (pushed).

CHANGES:
- pm_forms.cpp L681: surf_revolve axis well K_EDGE -> K_EDGEAXIS (mirrors solid revolve L228). K_EDGEAXIS sets selKinds.axis, which is exactly what routeDatumAxis() gates on (property_manager.cpp:1166), so a datum axis now routes into the well instead of being silently rejected.
- property_manager.cpp readyForPreview(): surf_revolve pulled out of the profile-only group and given a dedicated branch requiring BOTH profile and axis picks. commit() (L2001) and applyPreview() (L1869) both hard-return on !readyForPreview, so OK can no longer fire without an axis -> kills the engine "axis required" error.

VERIFICATION DONE:
- Built the host GUI (VS 2026, Release) with the change; fresh AI-CAD.exe links clean. NOTE: the build first failed on a PRE-EXISTING C2733 in host/extern/fix_math.h (nexttowardf(float,float) vs the updated Win SDK's nexttowardf(float,long double)) that blocks ALL host builds on this SDK -> fixed in a separate commit on this branch (1a3f2ff2). Safe across SDK states.
- Headless pmform render (AI_CAD_SHOT=pmform AI_CAD_PMCMD=surf_revolve AI_CAD_PMBOX=1) in BOTH light and dark: the Revolved Surface form now shows the Axis well, and the footer reads "set parameters to preview" (gate OFF) with empty wells. Looked at both PNGs - text readable, no black-on-black.
- check.py: all 4 gates PASS (surfacing no drift). graphify update . done.

RESIDUAL (for human merge-time check): the DoD's fully-interactive "datum-axis pick -> successful surface" could NOT be completed in a Claude-launched instance. Even with AI_CAD_PYTHON pointed at the real Roaming venv, the live app fell back to the Python worker with "unknown method: doc_new" â€” libaicad.dll (staged in dist/AI-CAD with all 102 OCCT siblings) did not load/wire in the sandbox-launched process. This is the known "my launches get redirected; the user's Explorer launch gets the real runtime" limitation, orthogonal to this change. A normal Explorer launch should complete the live pick trivially. Filing an issue for the runtime-wiring + fix_math.h build breakage.
