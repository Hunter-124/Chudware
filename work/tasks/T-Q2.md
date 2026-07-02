---
id: T-Q2
title: Autosave, crash recovery & consent-tied diagnostics
workstream: Q
owner: sonnet-moss
status: review
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

## Progress log
- 2026-07-02 23:12 UTC [sonnet-moss] Implementation complete on branch task/T-Q2-prod (bd6b2aa). Gate PASS. 20 new tests all green. Changes: (1) fixed _autosave_dir() Linux APPDATA bug → now uses engine.paths.appdata_root() (XDG); (2) periodic autosave timer (60s default, configurable, p95 logging); (3) crash_bundle.py module with consent-gated upload (off by default, dispatches only at full); (4) crash handler installed at server startup; (5) gate.sh: added mcp_server/ to pytest sweep, added ignore for services/mcp/test_autosave_recovery.py (mcp namespace collision). GUI recovery prompt deferred — depends on unmerged T-U1. Ready for review/merge.
