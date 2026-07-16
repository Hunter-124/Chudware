# mega-25 a1 thrash kill

**RUN_ID:** mega-25-fc-rocket-20260715  
**Archive:** `attempt-thrash-20260715T234708Z`  
**Kill:** dag.node.failed count=4 > 3 (tools≈139)  
**Wall:** ~10 min before kill  

## Node fails
1. `n_intake` — escalated: failed (budget exhausted)
2. `n_build_DL3` — escalated: **no_code** (budget exhausted) — ecad_engineer grok-4.5
3. `n_build_DL2` — escalated: max_iters (budget exhausted)
4. `n_deliver_DL1` — escalated: failed (budget exhausted)

## ECAD surface (live tools)
- ecad_new_schematic×1, ecad_place_symbol×1, ecad_add_power×3, ecad_find_part×9
- **Missing:** board_from_schematic, ecad_optimize_traces, ecad_run_drc, ecad_simulate, ecad_wire
- Force-inject events: **0** observed in events.jsonl
- Model for BUILD DL3: xai-sub grok-4.5 / ecad_engineer / rank 5

## Mech/fw that did run
- new_part×1, add_features×6, verify_battery×1, measure_between×4
- fw_new_project, fw_build, fw_sim_run, fw_sensor_scenario
- DL1 BUILD + DL4 BUILD completed (auto-complete)

## Note
Y25/Z25 source bound unit-green preflight; live path never reached simulate/DRC so axes untested.
Verify/deliver nodes for DL3 still marked done after BUILD fail (no terminal criterion artifacts — process killed before mission.json).

## Next
Fixer25 rebind force-chain on no_code DL3; relaunch a2 with liveness watch.
