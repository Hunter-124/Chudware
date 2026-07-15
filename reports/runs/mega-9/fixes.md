# mega-9 harness fixes (Fixer9 = ReadySparrow)

Baseline: `b8181b97`. Pair: MegaRun9=`DizzyGerbil` owns `reports/runs/mega-9/**` +
roadmap; Fixer9 owns `services/agent|dag|mcp` + gym + this file.

Axes targeted:
- **F** terminal `stop_reason` clean string classes (no raw blobs) — Fix11 residual
- **E** mission-built doc reaches `verify_battery` with per-check results in ledger
- **G** typed-refusal envelope validates end-to-end + prompts teach schema
- Regression: A–D stay green (mega-8 suite still PASS)

---

## Fix F — `dag_error:<class>` clean stop_reason (Fix11 residual)

**Fingerprint (mega-8 a4):** mission terminal
`stop_reason=dag_error: string or blob too big` — raw Sqlite/LangGraph exception
text embedded after the prefix. Axis D treated that as “explicit” (not
`running`+empty); mega-9 axis F requires **class tokens only**.

**Root cause:** `run_mission` catch block did
`stop_reason=f"dag_error: {exc}"`, so any exception body (multi-line concurrent
update URLs, blob messages, etc.) became the terminal string.

**Fix:**
1. `services/agent/loop.classify_dag_error(exc)` → always
   `dag_error:<token>` with no payload:
   - `string or blob too big` → `dag_error:checkpoint_blob`
   - concurrent graph update needles → `dag_error:concurrent_update`
   - sqlite locked / io / timeout / connection → matching tokens
   - unknown → `dag_error:execution` (never embeds `str(exc)`)
2. Catch site uses `stop_reason=classify_dag_error(exc)`.
3. Hardened `_compact_agent_result` (node_runner): drop unknown fat handoff
   output keys (>120 char strings / dict/list dumps); hard cap 8k JSON.

**Regression:** `services/agent/test_mega9_fixes.py`
- `test_classify_dag_error_blob_too_big`
- `test_classify_dag_error_concurrent_update`
- `test_classify_dag_error_unknown_is_stable_class`
- `test_compact_agent_result_drops_fat_payloads`

**Evidence:**
- `services/agent/loop.py` — `classify_dag_error` + catch site
- `services/dag/node_runner.py` — `_compact_agent_result` slim + 8k cap

---

## Fix E — verify_battery wiring + ledger per-check evidence

**Fingerprint:** BUILD produced solids (mega-8 a4 add_features×5 ok) but VERIFY
path could complete AC tools without a guaranteed `verify_battery` run /
`battery_result` event. Axis E requires the battery to **execute** with honest
PASS/WARN/FAIL (or NOT-CHECKED) in the ledger — geometry quality may still FAIL.

**Fix:**
1. `reviewer_exec.run_verify_battery(ledger, doc_id=…)` — resolves + calls
   `verify_battery`, always appends:
   - `battery_result` event (`verdict`, `checks[]`, `ok`, `detail`, `doc_id`)
   - `artifact` kind=battery
   - synthetic `criterion_result` id=`BATTERY` (check_tool=verify_battery) for
     scanners; VERIFY gate still enumerates only **declared** AC ids so this
     does not fabricate AC PASS.
2. `loop._run_reviewer_checks` always calls `run_verify_battery` after ACs
   (best-effort; on total miss still writes NOT-CHECKED battery_result).
3. `agent_turn._mirror_c1_grading_evidence` on successful `verify_battery` tool
   call also emits `battery_result` (not only the artifact).
4. Role/tool surface:
   - `feature_modeler` allowlist gains `verify_battery` (dropped `undo` to stay
     under `MAX_TOOLS_PER_ROLE=14` so `delegate` still seats).
   - Prompt: BUILD must run battery before done.
5. Mission template BUILD/VERIFY request text now names `verify_battery`.

**Regression:**
- `test_run_verify_battery_logs_honest_not_checked_when_unresolved`
- `test_run_verify_battery_logs_pass_and_per_check`
- `test_run_verify_battery_logs_fail_honestly`
- `test_run_reviewer_checks_invokes_battery`
- `test_feature_modeler_has_verify_battery_tool`
- `test_build_and_verify_requests_mention_battery`

**Evidence:**
- `services/agent/reviewer_exec.py` — `run_verify_battery`
- `services/agent/loop.py` — `_run_reviewer_checks`
- `services/agent/agent_turn.py` — battery_result mirror
- `services/agent/roles/feature_modeler.yaml`
- `services/dag/mission_template.py` — BUILD/VERIFY request strings

---

## Fix G — F9 typed-refusal schema taught + validates E2E

**Fingerprint:** gate blocked layout/plan with
`delegate_or_typed_refusal_required`; scaffold mentioned keys but models never
emitted a validating envelope. F9 residual: teach exact schema + prove validate.

**Fix:**
1. `_build_request` CAPABILITY scaffold expanded with full JSON shape + worked
   example (`reason` / `domains_considered` / `why_flat`, non-empty rules,
   handoff.outputs placement).
2. Role cards (`layout_engineer`, `orchestrator`) spell EXACT keys + example
   envelope in system_prompt.
3. Unit tests: schema validate + extract + node_runner gate ACCEPTS taught
   envelope; scaffold contains all three keys.

**Regression:**
- `test_typed_refusal_envelope_validates_end_to_end`
- `test_typed_refusal_gate_accepts_taught_envelope`
- `test_multi_domain_request_teaches_exact_schema_keys`
- `test_role_cards_teach_typed_refusal_schema`
- Existing mega-8: `test_typed_refusal_schema_strict`,
  `test_typed_refusal_gate_accepts_valid_envelope`,
  `test_multi_domain_request_includes_typed_refusal_scaffold` — still PASS

**Evidence:**
- `services/dag/node_runner.py` — `_build_request` scaffold
- `services/agent/roles/layout_engineer.yaml`, `orchestrator.yaml`
- `services/agent/role_models.py` — existing `validate_typed_refusal` /
  `TYPED_REFUSAL_SCHEMA` (unchanged contract)

---

## Test command (own files only)

```text
python3 -m pytest services/agent/test_mega9_fixes.py \
  services/agent/test_mega8_rolesurface.py -q
```

**Result (this session):** `36 passed`.

---

## Files touched

| Path | Change |
|---|---|
| `services/agent/loop.py` | `classify_dag_error`; clean catch; battery in `_run_reviewer_checks` |
| `services/agent/reviewer_exec.py` | `run_verify_battery` + slim evidence |
| `services/agent/agent_turn.py` | `battery_result` mirror on tool path |
| `services/dag/node_runner.py` | richer typed-refusal scaffold; tighter compact |
| `services/dag/mission_template.py` | BUILD/VERIFY request → verify_battery |
| `services/agent/roles/feature_modeler.yaml` | +verify_battery; -undo (cap) |
| `services/agent/roles/layout_engineer.yaml` | exact typed-refusal example |
| `services/agent/roles/orchestrator.yaml` | exact typed-refusal example |
| `services/agent/test_mega9_fixes.py` | new regressions (F/E/G) |
| `reports/runs/mega-9/fixes.md` | this log |

---

## Ready for MegaRun9 relaunch

Fixes landed and unit-green. Relaunch when process ready; expected terminal
`stop_reason` on checkpoint crash path is now `dag_error:checkpoint_blob`
(not raw blob text). VERIFY entry should leave `battery_result` /
`criterion_result` BATTERY / battery artifact even if geometry WARNs/FAILs.
Typed-refusal success path is taught + unit-proven; live model emission still
depends on the run.

---

## a2 residuals (MegaRun9 live feedback, wall 415.77s)

**Live axes (MegaRun9 graded):** E/F/G + A–D PASS with residuals below.
Terminal: `needs_correction` / `verify_failed: [AC1…AC9]` (clean F).
`battery_result` present but `doc_id=null`; AC1 NOT-CHECKED despite battery
shape; `is_manifold` 0.0 under fabricated overall PASS; n_build_DL2 still
gate-died without typed-refusal envelope.

### Residual 1 — explicit mission `doc_id`

**Fix:**
- `reviewer_exec.resolve_mission_doc_id(dag_result, ledger)` prefers BUILD node
  `doc_id`, then latest `new_part`/`add_features` from ledger tool_calls.
- `loop._run_reviewer_checks` uses it for AC sweep + battery.
- `node_runner` lifts `doc_id` from agent_result (`new_part` result / handoff
  outputs) onto node output_state when inputs lack it.

**Tests:** `test_resolve_mission_doc_id_prefers_build_and_ledger`,
`test_extract_doc_id_from_agent_result`

### Residual 2 — AC1 pass_rule vs battery `{scores,findings}`

**Fingerprint:** AC1 `check.tool=verify_battery` `pass_rule=PASS` →
`NOT-CHECKED: tool output carries no verdict/status field` while scores
included `is_manifold: 0.0` and findings `ok:false`.

**Fix:**
- `interpret_battery_result()` — FAIL on critical zero scores / ok:false
  findings; WARN on soft zeros; PASS only when clean.
- `evaluate_pass_rule("PASS", …)` derives from scores/findings when no
  top-level verdict (so AC1 gets FAIL not NOT-CHECKED on bad geometry).
- `run_verify_battery` uses the same interpreter (no more PASS on empty bodies).

**Tests:** `test_interpret_battery_fails_on_zero_manifold`,
`test_evaluate_pass_rule_pass_on_clean_battery_scores`,
`test_run_verify_battery_logs_fail_honestly` (scores path)

### Residual 3 — live typed-refusal still unused

**Fix:**
- `extract_typed_refusal` salvages schema-valid JSON from `ledger_note` args /
  text (models bury the envelope there).
- Gate-retry scaffold pastes a complete valid envelope to copy.

**Tests:** `test_typed_refusal_salvaged_from_ledger_note_text` + existing gate
accept tests.

### a2 patch test result

```text
python3 -m pytest services/agent/test_mega9_fixes.py \
  services/agent/test_mega8_rolesurface.py -q
# 41 passed
```

**Note:** MegaRun9 closed grading on a2 without further relaunch unless Main
asks. These patches are ready for the next process/rebind.
