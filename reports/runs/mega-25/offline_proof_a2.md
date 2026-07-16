# mega-25 offline proof (ExcellentPiranha thrash rebind)

Contract: re-run Fixer24c planning-node offline repro against edited
`agent_turn.py` + thrash rebind (end-of-turn ECAD force + no mistral).
Y25/Z25 must not collide with force-first/junk-JSON paths.

## Fixer24c planning-node suite (scripted = cheap; no frontier spend)

- **PASS** `24c_prose_force`: `'confirmed'`
- **PASS** `24c_junk_force`: `'confirmed'`
- **PASS** `24c_force_then_junk_bare`: `'confirmed'`
- **PASS** `empty_no_code`: `'no_code'`
- **PASS** `seed_need_nonzero`: `'ecad_add_power'`
- **PASS** `eot_force_board_drc`: `{'forced': ['ecad_add_power', 'ecad_place_symbol', 'ecad_wire', 'board_from_schematic', 'ecad_optimize_traces', 'ecad_run_drc', 'ecad_run_erc', 'ecad_place_symbol', 'ecad_place_symbol', 'ecad_wire', 'ecad_wire', 'ecad_wire', 'ecad_wire', 'ecad_simulate'], 'has_board': True, 'has_opt': True, 'has_drc': True, 'has_sim': True, 'sim_docs': ['9a9edcf3cdad'], 'made': 30}`
- **PASS** `y25_sim_on_sch`: `['9a9edcf3cdad']`
- **PASS** `no_mistral_model_assign`: `['                                cfg_or["model"] = mid', '                    cfg_or["model"] = mid']`
- **PASS** `deepseek_in_fallback`: `None`

## Verdict: **FINAL_OFFLINE_OK**

### Pre-rebind fingerprint (mega-25 a1 thrash)
- kill: `dag.node.failed count=4 > 3 (tools=139)`
- INTAKE/DELIVER: leaf_oneshot_miss → `mistralai/mistral-small:free` 404
- BUILD DL3: sch+place+orphan power, 0 force-inject, budget exhaust, no board

### Post-rebind mechanisms
1. **24c hold**: zero-tool prose/junk → force-first + bare-handoff; empty → no_code.
2. **end_of_turn_ecad_force**: incomplete ECAD product at budget → cascade with
   headroom (wire→board→opt→drc→erc→sim).
3. **no dead mistral**: `node_runner` resolve + frontier_fallback use deepseek mid
   then gemma/nemotron free (never mistral-small:free).
4. **Y25 hold**: forced simulate doc_id = schematic, not board.

No push. Python-only.
