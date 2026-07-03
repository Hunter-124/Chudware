---
id: T-BL35
title: Surfaces — surf_revolve axis well rejects datum axis (K_EDGE→K_EDGEAXIS) + gate readyForPreview on the axis well
workstream: backlog
owner: unassigned
status: todo
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
