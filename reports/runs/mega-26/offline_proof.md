# mega-26 offline proof (Fixer26 = DeadOpossum)

Contract: re-run mega-24 offline planning-node repro after agent_turn A26/B26 edits.

## Fixer24c planning-node suite (scripted = cheap; no frontier spend)

- **PASS** `24c_prose_force`
- **PASS** `24c_junk_force`
- **PASS** `24c_force_then_junk_bare`
- **PASS** `24c_n_plan_typed_refusal`
- **PASS** `empty_no_code`: 'forced'
- **PASS** `seed_need_nonzero`: 'ecad_add_power'
- **PASS** `eot_force_board_drc`: {'forced': ['ecad_wire', 'board_from_schematic', 'ecad_optimize_traces', 'ecad_run_drc', 'ecad_run_erc', 'ecad_place_symbol', 'ecad_wire', 'ecad_add_power', 'ecad_wire', 'ecad_simulate'], 'has_board': True, 'has_opt': True, 'has_drc': True, 'has_sim': True, 'sim_docs': ['9a9edcf3cdad'], 'made': 10}
- **PASS** `y25_sim_on_sch`: ['9a9edcf3cdad']
- **PASS** `no_mistral_model_assign`
- **PASS** `deepseek_in_fallback`
- **PASS** `a26_b26_source_markers`

## Verdict: **FINAL_OFFLINE_OK**

### A26/B26 offline

1. V_source value bare `3.3` (not `DC 3.3`) — SPICE schematic path parses.
2. Clearance re-opt reachable with BUILD allow (no optimize listed).
3. Truncated DRC JSON still yields errors count for dirty retry.
4. Y25/Z25/24c holds.

No push. Python-only.
