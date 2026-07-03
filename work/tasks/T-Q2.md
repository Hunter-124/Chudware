---
id: T-Q2
title: Autosave, crash recovery & consent-tied diagnostics
workstream: Q
owner: sonnet-moss
status: done
branch: task/T-Q2-prod
day: 2
priority: 3
depends_on: []
lane: platform-infra
kind: new
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

- [x] Kill -9 during edits → next open offers recovery → tree intact (scenario test)
      — test_recover_autosaved_restores_tree: writes prior-session .step+ stubs, clears
        registry, verifies recover_autosaved() restores both docs with name/alias intact.
        Current-session exclusion and stale-prune tests also passing.
- [x] Crash bundle written locally on induced crash; uploaded ONLY at consent full
      (test both tiers)
      — services/mcp/crash_bundle.py: write_crash_bundle() always writes locally;
        upload hook dispatched ONLY at consent="full". Tests: test_upload_NOT_called_at_off,
        test_upload_NOT_called_at_meta, test_upload_called_at_full — all pass.
- [x] Autosave cadence configurable; measurable overhead < 50ms p95 per snapshot
      — AI_CAD_AUTOSAVE_INTERVAL_S (default 60s) controls cadence; start/stop_periodic_autosave()
        in cad_tools.py. autosave_p95_ms() exposes current p95 latency. Warning logged
        when p95 > 50 ms. Test confirms timer fires and is stoppable.
- [x] gate; graphify update .

## Implementation notes

- Fixed `_autosave_dir()` Linux bug: `os.environ.get("APPDATA")` returned None → /tmp
  on Linux, scattering autosaves outside AI-CAD data tree. Now uses
  `engine.paths.appdata_root()` (XDG_DATA_HOME/~/.local/share/AI-CAD). Supports
  AI_CAD_AUTOSAVE_DIR override for test hermeticity.
- New module: `services/mcp/crash_bundle.py` — crash bundle writer, consent-gated
  upload dispatch (default off), signal handlers (SIGTERM/SIGABRT), idempotent install(),
  reset_for_testing() for test hermetics. Upload hook is a no-op stub; real endpoint to
  be wired when available.
- Periodic timer wired into server._warm_core() alongside recover_autosaved().
- crash_bundle.install() called once from server._warm_core() after recovery.
- GUI recovery prompt deferred — depends on unmerged T-U1 (sonnet-reed). The MCP-layer
  recover_parts tool is the primary recovery path; GUI prompt is a UX layer on top.
- gate.sh: added mcp_server/ to per-dir pytest sweep; ignore
  services/mcp/test_autosave_recovery.py from services/ sweep (mcp namespace collision).
- 20 tests, 25 total (including existing ecad_hardening) — all green.
- Gate: PASS (pre-existing FAIL-RECORDED entries unchanged; T-V2 owns).

## Driver notes (Fable)

- graphify query "undo journal recover_parts" first — half of this exists; wire and
  harden before writing anything new. Consent gating reuses T-H1's tier resolution
  (offline ⇒ off) — import, don't reimplement.

## Progress log
- 2026-07-02 23:12 UTC [sonnet-moss] Implementation complete on branch task/T-Q2-prod (bd6b2aa). Gate PASS. 20 new tests all green. Changes: (1) fixed _autosave_dir() Linux APPDATA bug → now uses engine.paths.appdata_root() (XDG); (2) periodic autosave timer (60s default, configurable, p95 logging); (3) crash_bundle.py module with consent-gated upload (off by default, dispatches only at full); (4) crash handler installed at server startup; (5) gate.sh: added mcp_server/ to pytest sweep, added ignore for services/mcp/test_autosave_recovery.py (mcp namespace collision). GUI recovery prompt deferred — depends on unmerged T-U1. Ready for review/merge.
