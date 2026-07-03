# Foreman hot state (overwrite each wake — cheap re-orientation after compaction)

Updated: 2026-07-03 by fable-foreman (wave-1 dispatch session).

## Standing orders (Hunter, 2026-07-03)
- CONSERVE FABLE: one Fable session (this one), foreman only, never drives. Delegate every build/test/bulk-read/review to Sonnet subagents; terse turns; HANDOFF at ~60-70% context.
- Priority directive: core CAD + AI-harness + GUI first; ML/fine-tuning deferred (WORKFLOW.md §3).
- Identity: all commits author as Hunter-124. NEVER `git config user.*`, NEVER `whoami` — pass --owner explicitly everywhere.
- mooseinatree owns T-BL02 (Windows competitor-CAD reversing); may be dark for a long time. Never release/reassign it, never cut tasks consuming his deliverables until he lands them. openNURBS/.3dm + DXF importers proceed independently.
- Drivers Sonnet by default, Opus only where flagged; stop at status: review; foreman merges.

## Board state at this wake
- T-G12 round-1 fully landed (T-G14..T-G20 + T-BL19 merged, atlases regenerated, validate green 142 tasks). No open roadblocks.
- Hygiene done this wake: T-BL27/T-BL28 got lane=modeling-core kind=fix P2; T-G12 stale sonnet-tess claim released, parked blocked under fable-foreman (human-gated on Hunter round-2 walk).
- Icon-recipe follow-up (T-G15 leftover groups) being cut as next free T-BL id.

## Wave 1 (dispatched this session)
| Task | Driver | Tier |
|---|---|---|
| T-G21 vertical collapse FM+Copilot | sonnet-wick | Sonnet |
| T-BL28 surfaces edge-well refs | opus-briar | Opus (default: fix the forms) |
| T-BL27 surf_replace_face healing | opus-tarn | Opus |
| T-BL26 MCP freshness audit | sonnet-loch | Sonnet |
| T-BL22 dormant AI seams | sonnet-eddy | Sonnet |
| T-BL23 CAM scoring keys | sonnet-gorse | Sonnet |
| T-BL25 doc-scoped IntakeLedger | sonnet-heath | Sonnet |
| icon-recipe follow-up (new T-BL) | sonnet-lumen | Sonnet (after cut) |

## Wave 2 (as slots free)
- SONNET: T-BL21 hollow sections, T-BL20 FEA/CFD V&V, T-BL09 req→verify wiring, T-BL24 JLCPCB signed auth (.env keys), .3dm importer + DXF importer (cut as new T-BL tasks, clean-room, no mooseinatree deps).
- OPUS: T-R1 PLM contract freeze (unblocks R2..R7 — next big push per Pillar 19); tier R2..R7 individually after R1.
- Later (P4 sim lane): T-BL17, T-BL18, T-BL04 Sonnet; T-BL16 Opus-leaning if it reaches the mate solver.

## Human-gated (keep in every summary for Hunter)
- T-G12 round-2 walkthrough (fixes merged, needs his walk).
- T-G11 Windows verify + host/ deletion (his Windows machine).
- T-S1 vendor API applications.
- Disjoint-fuse severity call (work/questions/T-BL19.md).
- T-BL28 fix-direction question + T-G20 KEEP ratification.
- Hover-state spot-checks headless can't verify (listed in T-G14/T-G18 task files).

## Verification recipes
- pytest: `AI_CAD_LIBAICAD=$PWD/core/build-linux/bin/libaicad.so ~/.local/share/AI-CAD/mcp-venv/bin/python -m pytest <dirs> -q`
- core: `flock /tmp/host-build-linux.lock bash -c 'cmake --build core/build-linux -j4 && cd core/build-linux && ctest -R <re>'`
- Full-sweep cross-suite pollution (~27F/44E) is PRE-EXISTING — verify per-suite, not one big sweep. 4 collection-error ignores: engine_oracle/doc/test_aicm.py, engine_oracle/sim/test_foam_case.py, engine_oracle/sim/test_lbm.py, services/agent/test_loop_mock.py.
- Headless screenshots: `--ui-screenshot` NOT `--shot`. Icon atlases are DERIVED — regenerate (host/assets/make_icons.py, then xvfb-run -a python3 scripts/icon_pipeline.py --render), never hand-merge PNGs.
