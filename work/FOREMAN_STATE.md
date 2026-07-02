# Foreman hot state (overwrite each wake — cheap re-orientation after compaction)

Updated: 2026-07-03T08:45Z by Fable foreman (overnight autonomous mode, Erik asleep ~10h).

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
- opus-sky → T-G13 (issue #7 deadlock) | sonnet-reed → T-U1 (style kit) | sonnet-vega → T-V2 (MCP runtime audit) | sonnet-orin → T-INF1 (gate stage fix)
- opus-iron free (M1+M6 merged) — will auto-claim (next P2: T-Q1, then P3s)
- Merged tonight: M1, M6, V1(+same-night provisioning: fluids/build123d/ngspice; piping_internal_volume @mcp.tool() bug fixed), H10-residual (NO-GO zero-shot F1 0.034), G6, G7, M2, M4, M5, H9, H11, F11, B5, Q4, A3, D4.
- Ready queue next: Q1(P2), Q2, A4, C5, D5, F4 (P3), then P4s. U2-U8 unlock on U1; G8 on G13.

## Verification recipes
- pytest: `AI_CAD_LIBAICAD=$PWD/core/build-linux/bin/libaicad.so ~/.local/share/AI-CAD/mcp-venv/bin/python -m pytest <dirs> -q`
- core: `flock /tmp/host-build-linux.lock bash -c 'cmake --build core/build-linux -j4 && cd core/build-linux && ctest -R <re>'`
- Full-sweep cross-suite pollution (~27F/44E) is PRE-EXISTING — verify per-suite, not one big sweep. 4 collection-error ignores: engine_oracle/doc/test_aicm.py, engine_oracle/sim/test_foam_case.py, engine_oracle/sim/test_lbm.py, services/agent/test_loop_mock.py.
- Erik human items pending: G12 round-1 walkthrough; PID 663281 ownership; two-machine H6 smoke; serving runbook halves.
