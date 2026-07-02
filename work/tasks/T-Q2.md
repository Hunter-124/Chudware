---
id: T-Q2
title: Autosave, crash recovery & consent-tied diagnostics
workstream: Q
owner: unassigned
status: todo
branch: task/T-Q2-prod
day: 2
priority: 3
depends_on: []
---

## Context

PROJECT-LEAD ADDITION: parametric docs + long agent runs + a young codebase = data loss
risk. Need autosave/journal recovery and crash diagnostics that respect consent.

## Files / areas touched

- Worker-side: periodic doc journal/autosave snapshots (.step+ sidecar or existing undo
  journal extension — T-H1 added undo journal persistence for CAM/ECAD; extend to part
  docs), recover-on-open flow (recover_parts tool exists — wire + test the full loop)
- Crash diagnostics: signal/exception handler writing a local crash bundle (stack, log
  tail, doc journal ref); UPLOAD only at consent full, else local-only w/ "show folder"
- GUI: recovery prompt on next launch (T-U1 patterns when merged)

## Definition of done

- [ ] Kill -9 during edits → next open offers recovery → tree intact (scenario test)
- [ ] Crash bundle written locally on induced crash; uploaded ONLY at consent full
      (test both tiers)
- [ ] Autosave cadence configurable; measurable overhead < 50ms p95 per snapshot
- [ ] gate; graphify update .

## Driver notes (Fable)

- graphify query "undo journal recover_parts" first — half of this exists; wire and
  harden before writing anything new. Consent gating reuses T-H1's tier resolution
  (offline ⇒ off) — import, don't reimplement.
