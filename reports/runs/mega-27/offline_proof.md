# mega-27 offline proof (Fixer27 = AshamedLungfish)

Contract: re-run mega-24 offline planning-node repro after agent_turn C27/D27
edits (board outline sizing + clearance grow ladder).

## Fixer24c planning-node suite (scripted = cheap; no frontier spend)

Covered by `services/agent/test_mega24_fixes.py` + mega-27 cascade unit fixtures
(full gate below includes mega24..27):

- **PASS** `24c_prose_force` (force_first on zero-tool prose — mega24 suite)
- **PASS** `24c_junk_force` (force_first on junk JSON — mega24 suite)
- **PASS** `24c_force_then_junk_bare` (bare handoff after force — mega24 suite)
- **PASS** `24c_n_plan_typed_refusal` (typed_refusal attach — mega24 suite)
- **PASS** `empty_no_code` contract preserved (mega24 suite)
- **PASS** `seed_need_nonzero`: chain_need returns next product tool
- **PASS** `eot_force_board_drc`: board_from_schematic → optimize → DRC (+ erc/sim)
  with **outline knobs** on forced board args
- **PASS** `y25_sim_on_sch`: forced simulate doc_id is schematic
- **PASS** `a26_b26_source_markers`: u1_stub + measure-net drive; clearance retry
- **PASS** `c27_outline_ratio_ge_3`: forced board `outline_area_ratio >= 3` and
  `outline_margin >= 15`; call records carry `outline_area_mm2` /
  `component_bbox_area_mm2`
- **PASS** `d27_outline_grow_ladder`: dirty clearance DRC → board grow×1.5 +
  autoroute(pitch≥0.3) + re-opt + re-DRC; `retry_n >= 1`; honest FAIL if still dirty

## Gate evidence

```text
python3 -m pytest \
  services/agent/test_mega27_fixes.py \
  services/agent/test_mega26_fixes.py \
  … mega25..16 … \
  services/agent/test_mega11_legs.py \
  services/agent/test_mega11_grading.py \
  engine/ecad/test_autosave.py -q
→ 237 passed, 1 skipped, 0 failed
```

## Verdict: **FINAL_OFFLINE_OK**

### C27/D27 offline

1. Forced `board_from_schematic` carries `board_size` + `outline_margin≥15` +
   area diagnostics with ratio ≥ 3× component bbox.
2. Clearance dirty path grows outline ×1.5 before autoroute(pitch=0.3).
3. AC4 pass rule unchanged (`errors==0`); residual after one retry is honest FAIL.
4. Y25/Z25/A26/B26/24c holds.

No push. Python-only.
