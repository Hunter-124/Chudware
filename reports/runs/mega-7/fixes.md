# mega-7 harness fixes (Fixer7 = HomelessBadger)

Live mission: MegaRun7 = RudeCockroach (`reports/runs/mega-7/**`).
Baseline commit: `84963165` (Fixer6 Fix 6–7 rebind target).
Scope is harness enablement only; no grading changes. No C++.

Owner watch-list this cycle:
1. DELIVER phase mortality (`deliver_incomplete` twice-running class)
2. Delegation regression (mega-5: 2 calls → mega-6: 0)
3. `new_part` empty solids (`add_features` never followed)
4. live cheap-share 0% (depth0 frontier correct, but leaves never classify low)

---

## Live fingerprints that drove fixes

### mega-6 terminal (attempt-2)
- Mission: `blocked` / `deliver_incomplete`; phases_completed intake/specify/plan only
- DAG: 19/19 started; 17 done / 2 failed (`n_spec_DL5`, `n_organize` no_code)
- Tools: `new_part`×2, `preflight_ops`×12, **0** `add_features`, **0** `delegate`
- Cheap share: **0%** (all depth0, `task_class` unset → min_rank=3 → xai-sub)
- DELIVER: `n_deliver_DL2` done, DL1/3/4/5 failed → soft-join missing (same class as specify)

### mega-7 process note
- This process bound baseline 84963165 at launch; Fixer7 source lands on **next relaunch**.
- Preflight PASS: spawn_probe + auto_pick_smoke (depth2→ollama).

---

## Fix 1 — DELIVER soft-join (deliver_incomplete class)

**Fingerprint:** mega-6 (and prior) terminal `stop_reason=deliver_incomplete` while at least one `n_deliver_*` was `done` and upstream phases had product work. DELIVER is a terminal fan-out (`on_fail=skip`) with **no join node** — same soft-join class as mega-2/3 `specify_incomplete`.

**Fix (`services/agent/loop.py` `_phase_completion`):**
- For phase `deliver`, if **any** worker is `done`, treat the phase as advanced (real completion — mission report path ran).
- Remaining failed/skipped siblings are honest partial delivery, not a freeze.
- If **all** deliver workers are terminal-failed/skipped with zero success → still `deliver_incomplete`.

**Regression:**
- `test_deliver_soft_join_any_success_completes_phase`
- `test_deliver_still_incomplete_when_all_deliver_failed`

---

## Fix 2 — Restore delegation preference (without burning quota)

**Fingerprint:** mega-5 had 2 `delegate` calls / 1 spawn; mega-6 had **0** despite tools_slot rendering `delegate` + `budget_tokens`. Conservation config did not remove the tool — models simply never called it.

**Fix (role cards):**
- `feature_modeler`, `orchestrator`, `spec_writer`, `layout_engineer`, `department_manager`: strengthen MUST-spawn language when ≥2 independent slices exist.
- Explicit note: **children auto_pick cheap** — spawning does not burn SuperGrok quota.
- `budget_tokens` contract retained (mega-5 spawn_probe still green).

**Regression:**
- `test_delegate_roles_still_prefer_delegate` (parametrized over 6 roles)
- Existing mega-5 `test_all_delegate_roles_use_budget_tokens_contract` non-regression

---

## Fix 3 — Empty solid / add_features after new_part (rebind + harden)

**Fingerprint:** BUILD DL1 tools_slot had mutators; live sequence `new_part` → `preflight_ops`×7 → max_iters auto-complete with **zero `add_features`** → empty solid marked done. Fixer6 Fix 7 landed post-process.

**Fix:**
- `agent_turn`: stronger post-`new_part` nudge (next tool MUST be `add_features`; ban preflight/ledger/describe until then).
- After **≥1** `preflight_ops` with prior `new_part` and no `add_features`, inject hard stop-thrash message (was ≥2).
- Keep mega-6 productive-tool gate: `new_part` alone is **not** product success (`bootstrapped_only`).
- `feature_modeler` system prompt: fixed sequence `new_part → add_features → (optional one preflight) → add_features`.

**Regression:**
- `test_new_part_without_add_features_not_auto_done`
- `test_new_part_then_add_features_auto_completes`
- `test_feature_modeler_requires_add_features_after_new_part`

---

## Fix 4 — task_class stamp on mission nodes (cheap-share path)

**Fingerprint:** live cheap-share 0%. auto_pick_smoke PASS for depth2 leaf, but **every mission template node ran depth0 with `task_class` unset** → `min_rank_for_difficulty` default 3 → ollama rank1 filtered → 100% SuperGrok.

**Fix:**
- `mission_template.build_mission_graph`: stamp `inputs.task_class` on every node:
  - INTAKE / ORGANIZE / n_spec_done / VERIFY → `leaf` (min_rank 1)
  - DELIVER → `simple_part` + `risk=low` (min_rank 1)
  - SPECIFY/BUILD → kind map (`part`→`multi_feature_part`, `assembly`→`assembly`, …)
  - PLAN → `multi_feature_part`; layout → `assembly`
  - Safety net: any missing → `leaf`
- `role_models._TASK_CLASS_MIN_RANK`: add `leaf` and `simple` aliases at rank 1.
- `context_pack.assemble`: promote `task_class` / `depth` / `risk` / `role` to pack root so `_resolve_provider` sees them.

**Smoke (this process):**
```
n_intake leaf
n_verify_DL1 leaf low
n_deliver_DL1 simple_part low
n_build_DL1 multi_feature_part
auto_pick(task_class=simple_part, depth=0) → ollama/llama3.2:latest rank1
```

**Regression:**
- `test_mission_nodes_stamp_task_class`
- `test_context_pack_promotes_task_class`
- `test_leaf_task_class_min_rank_is_one`
- `test_depth0_leaf_auto_pick_not_forced_frontier`

---

## Verification

```text
python3 -m pytest services/agent/test_mega7_fixes.py services/agent/test_mega6_fixes.py services/dag/test_mission_template.py -q
32 passed
```

## Files touched

| Path | Change |
|---|---|
| `services/agent/loop.py` | DELIVER soft-join in `_phase_completion` |
| `services/dag/mission_template.py` | `task_class` stamp + kind map |
| `services/dag/context_pack.py` | promote routing fields to pack root |
| `services/agent/role_models.py` | `leaf` / `simple` min_rank=1 |
| `services/agent/agent_turn.py` | harder empty-solid thrash nudge |
| `services/agent/roles/{feature_modeler,orchestrator,spec_writer,layout_engineer,department_manager}.yaml` | delegate MUST + add_features sequence |
| `services/agent/test_mega7_fixes.py` | new regression suite |

## Bind note for MegaRun7

Attempt-1 (pid 2101380) bound baseline 84963165 **before** Fixer7 edits — archived
under `attempt-1-pre-fixer7-rebind/`. Attempt-2 (pid 2154280) relaunched and
**bound Fix 1–4**.

---

## Attempt-2 live evidence (pid 2154280, wall 382.98s, account 17d17fb1)

| Axis | Evidence | Verdict |
|---|---|---|
| Fix4 task_class stamp | MA×19: leaf/simple_part/multi_feature/assembly/analysis — **0 unknown** on attempt-2 | **PASS** |
| Fix4 cheap routing | leaf/simple_part → ollama (MA ollama×11); hard work → xai-sub (×8). Usage tokens: llama3.2×11 completes (~29k tok as `openai` label) / xai×79 | **PASS** (routing); label residual |
| Fix3 sequence | `new_part`×3, `add_features`×21 (events), `preflight_ops` thrash **gone** | **PASS** sequence; solids still fail on bad op schema (`missing 'op'` / `unknown op 'box'`) |
| Fix2 delegation | **`delegate`×2** + `agent.spawn`×2 (`part_designer`, `edit-part`) | **PASS** calls restored (mega-6 was 0) |
| Fix1 DELIVER soft-join | deliver DL1/3/4/5 done, DL2 failed → stage exit `mission_report_ledger_closed`; **no** `deliver_incomplete`; phases_completed includes deliver | **PASS** |
| BUILD nodes | n_build_DL1/DL2 **done** | improve |
| Wall | **383s** (mega-6: 889s) | improve |

### Follow-up landed mid/post attempt-2

- Expanded `_ROLE_ALIASES`: `part_designer`, `edit-part`, `edit_part`, `part_builder`,
  `solid_modeler` → `feature_modeler` so invented spawn roles get geometry tools
  (live spawns used those names; without alias children are bare).

### Residual (not this wave / next)

- `add_features` op vocabulary: model sent `kind:box` / `op:box` — engine wants
  cad_guide ops (e.g. sketch+extrude). Sequence fixed; geometry still hollow.
- provider_usage labels ollama as `provider=openai` (compat client) — cheap-share
  **token** % undercounts unless grader keys on `model=llama*`.
- Mission wrapper `status=running` / empty stop_reason after deliver exit —
  cosmetic vs phase-gate freeze.

### Tests

```text
python3 -m pytest services/agent/test_mega7_fixes.py \
  services/agent/test_mega6_fixes.py services/dag/test_mission_template.py -q
32 passed
```
