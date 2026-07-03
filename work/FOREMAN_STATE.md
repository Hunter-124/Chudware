# Foreman hot state (overwrite each wake — cheap re-orientation after compaction)

Updated: 2026-07-04T02:00Z by Fable foreman (overnight, ~5h in).

## Standing orders (Erik)
- Work until usage runs out; conserve Fable (44% left) — delegate to Sonnet agents, terse wakes.
- OSS-first before recoding; license guardrails absolute (MIT/BSD/Apache in-process, GPL/MPL subprocess-only, AGPL never).
- Local-first dev; push master to GitHub after every merge. Fable = master orchestrator.
- No visible GUI windows. Main checkout foreman-only (claims excepted). Never pull --rebase.

## Wake loop
1. Read watcher output → if driver REVIEW/QUESTION: fetch branch, review (delegate big reviews to a Sonnet agent), merge --no-ff, targeted verify, flip status done, ledger line, push.
2. Re-dispatch: any free lane → next `work.py ready` priority task as Sonnet agent (worktree, autopilot rules).
3. Restart watcher: `bash <scratchpad>/foreman-watch.sh` (run_in_background).
4. Overwrite this file. Keep responses short.

## In flight (as of update time)
- opus-sky → T-G8 ONLY remaining task (hard-subsystem ports; checkpoint pushed, CAM/ECAD content-swap restructure in progress; branch cold 4h, session shows live CPU — nudge posted, finisher NOT yet dispatched)
- Blocked behind G8: T-G9 → T-G10 → T-G11 (the last chain)
- COMPLETE workstreams tonight: A, B, C, D, E, F, H, M, P, Q, U, V. ~55+ tasks merged this session.
- Erik human items: G12 round-1 walkthrough, vendor keys (T-S1), H-lane runbook halves, live-backend capstone round 2 (great first demo)

## Verification recipes
- pytest: `AI_CAD_LIBAICAD=$PWD/core/build-linux/bin/libaicad.so ~/.local/share/AI-CAD/mcp-venv/bin/python -m pytest <dirs> -q`
- core: `flock /tmp/host-build-linux.lock bash -c 'cmake --build core/build-linux -j4 && cd core/build-linux && ctest -R <re>'`
- Full-sweep cross-suite pollution (~27F/44E) is PRE-EXISTING — verify per-suite, not one big sweep. 4 collection-error ignores: engine_oracle/doc/test_aicm.py, engine_oracle/sim/test_foam_case.py, engine_oracle/sim/test_lbm.py, services/agent/test_loop_mock.py.
- Erik human items pending: G12 round-1 walkthrough; PID 663281 ownership; two-machine H6 smoke; serving runbook halves.
