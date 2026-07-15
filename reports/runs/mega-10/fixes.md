# mega-10 harness fixes (Fixer10 = ComprehensiveWhale)

Baseline: `df8fb2e5`. Pair: MegaRun10=`UniformGoat` owns `reports/runs/mega-10/**` +
roadmap; Fixer10 owns `services/agent|dag|mcp` + gym + this file.

Axes targeted:
- **H** AC closure: ≥1 mission AC graded PASS/FAIL from real battery/measure
  artifacts (not NOT-CHECKED when evidence exists). Fixing mega-9 residual
  “AC1 NOT-CHECKED despite battery PASS” counts toward H when a criterion then
  grades from evidence.
- **I** battery `doc_id` correct with multiple parts open.
- **J** typed-refusal envelope exercised live OR harness E2E unit proof.
- Regression: A–G stay green (mega-8 / mega-9 suites).

---

## Fix H — AC1 grades from battery_result (mega-9 residual)

**Fingerprint (mega-9 a2):** `battery_result` + synthetic `criterion_result`
id=`BATTERY` logged PASS (15 checks) while **AC1** (`check.tool=verify_battery`,
`pass_rule=PASS`) stayed **NOT-CHECKED** with detail
`tool output carries no verdict/status field to match 'PASS'`. Root causes:

1. AC sweep ran **before** `run_verify_battery`, so AC1 re-invoked the battery
   (or graded a raw `{scores,findings}` without the a2 interpreter path being
   wired at the call site order).
2. Even when scores/findings were present, an optimistic top-level
   `verdict=PASS` short-circuited honesty while `is_manifold=0.0` /
   `ok:false` findings were on the payload.
3. AC1 never **reused** the ledger `battery_result` evidence — it always
   re-called the tool.

**Fix:**
1. `loop._run_reviewer_checks` runs **`run_verify_battery` first**, then
   `run_acceptance_checks(..., battery_payload=…)`.
2. `run_acceptance_checks` short-circuits `check.tool == verify_battery` to
   grade from `battery_payload` / latest ledger `battery_result` via
   `_battery_evidence_for_ac` + `evaluate_pass_rule` — writes
   `criterion_result` with `evidence.source=battery_result` + doc_id.
3. `interpret_battery_result` now prefers **scores/findings honesty** over an
   explicit optimistic PASS (critical zeros / ok:false → FAIL).
4. `evaluate_pass_rule("PASS", …)` re-checks scores/findings even when
   `verdict` says PASS; also accepts nested `evidence` from battery envelopes.
5. Mechanical shape for interference rules
   (`interferences empty or volume==0`) so AC3 is gradeable when the tool
   returns a pairs list.
6. `_call_tool` forwards scalar `params_hint` kwargs (measure/query hints)
   in addition to `doc_id`.

**Regression:** `services/agent/test_mega10_fixes.py`
- `test_ac1_grades_from_battery_payload_not_not_checked`
- `test_ac1_fail_when_battery_critical_zeros`
- `test_run_reviewer_checks_battery_before_acs`
- `test_explicit_pass_contradicted_by_failing_scores_is_fail`
- `test_interference_pass_rule_mechanical`

**Evidence:**
- `services/agent/reviewer_exec.py` — evaluate / interpret / run_acceptance_checks
- `services/agent/loop.py` — `_run_reviewer_checks` battery-first order

---

## Fix I — battery doc_id with multi-part open (mega-9 residual)

**Fingerprint:** `battery_result.doc_id: null` while two parts open
(`doc-cpp00000001` geometry-bearing, `doc-cpp00000003` empty spawn shell).
Several checks failed with “Ambiguous: 2 parts are open… Pass doc_id=”.

**Fix:**
1. `resolve_mission_doc_id` ranks candidates:
   - BUILD node outputs high
   - `add_features` with `volume_mm3>0` / `body_count>0` highest
   - bare `new_part` lower (so empty spawn shells lose)
   - checkpoints / weak signals lowest
2. Always thread the resolved id into `run_verify_battery` + AC tools;
   ledger `battery_result` records `doc_id`.
3. Never falls back to process-global current part.

**Regression:**
- `test_resolve_prefers_geometry_bearing_doc_over_empty_spawn`
- `test_run_verify_battery_threads_doc_id_with_two_docs`
- `test_call_tool_forwards_params_hint_and_doc_id`
- existing `test_resolve_mission_doc_id_prefers_build_and_ledger` (mega-9)

**Live replay (mega-9 ledger):**
`resolve_mission_doc_id(ledger=mega9) → doc-cpp00000001` (geometry doc wins
over later empty `doc-cpp00000003`).

---

## Fix J — typed-refusal harness E2E

**Fingerprint:** mega-9 taught schema + unit green; live models still budget-die
without emitting the envelope. Axis J accepts harness E2E proof.

**Fix / proof:**
- Existing teach path (scaffold + role cards) unchanged and still green.
- New harness test seeds a **deliberately flat multi-domain** BUILD/PLAN node
  emitting `{reason, domains_considered, why_flat}` under
  `handoff.outputs.typed_refusal`; node_runner gate returns DONE (not
  `delegate_or_typed_refusal_required`).

**Regression:**
- `test_typed_refusal_flat_multi_domain_gate_e2e`
- existing mega-8/9 typed-refusal suite (still PASS)

---

## Mission AC1–AC9 gradeability review (no silent weaken)

Source: `reports/runs/mega-9/manifest.json` + live criterion_results.

| AC | tool | pass_rule | Gradeable now? | Notes / proposal |
|---|---|---|---|---|
| **AC1** | `verify_battery` | `PASS` | **Yes** | Fixed: grades from battery scores/findings. PASS only on clean critical scores; FAIL (not NOT-CHECKED) on zeros. |
| **AC2** | `measure_part` | `bay envelope >= board outline` | **No (prose)** | `measure_part` returns volume/bbox/mass, not a bay-vs-board comparison. **Proposal:** change check to `query_geometry`/`measure_part` with mechanical rule e.g. `volume_mm3 in [Vmin,Vmax]` **or** a dedicated `fit_report`/`params_hint` that names bay feature + board envelope dims; keep intent (bay fits FC board). Do **not** weaken to manual. |
| **AC3** | `check_interference` | `interferences empty or volume==0` | **Yes (mechanical)** | New pass_rule shape evaluates `interferences`/`clashes` list + volumes. Needs assembly doc with board+airframe bodies and explicit `doc_id`. |
| **AC4** | `manual` | legs parts/schematic/board not FAIL | **Partial** | Manual by construction. **Proposal:** grade from ledger artifacts / `ecad_run_drc` + `ecad_run_erc` tool outputs (errors==0) via non-manual tools; keep SKIP path for unprovisioned. |
| **AC5** | `manual` | `V(3V3) in [3.135, 3.465]` | **Partial** | Interval shape is mechanical **if** tool returns numeric V(3V3). **Proposal:** `check.tool=ecad_simulate` (or SPICE result artifact) with same pass_rule — interval evaluator already supports it. |
| **AC6** | `manual` | PASS or SKIP with provision note | **No** | **Proposal:** `ecad_antenna_design` / `ecad_antenna_analyze` with pass_rule `PASS\|SKIP` and evidence of provision note in tool result / ledger_note. |
| **AC7** | `manual` | `fw_build returncode==0` | **No** | **Proposal:** `check.tool=fw_build` (or ledger scan of fw_build tool_call) with pass_rule on `returncode==0` / `ok==true` + ELF path evidence. |
| **AC8** | `manual` | `PASS\|SKIP(renode)` | **No** | **Proposal:** `fw_sim_run` / `fw_verify_board` with honest SKIP when Renode absent (`ok` + `skipped` reason). |
| **AC9** | `manual` | all legs have PASS\|WARN\|FAIL\|SKIP | **No** | **Proposal:** deterministic report-card grader over ledger `stage`/`decision` events + deliverable artifacts; or `make_report` tool output schema check. |

**Policy:** criteria were **not** weakened. AC2/AC4–AC9 remain honest NOT-CHECKED /
manual until mission manifest maps them to executable tools (proposals above).
H is satisfied by AC1 becoming evidence-graded (PASS or FAIL from battery).

---

## Test command (own files only)

```text
python3 -m pytest services/agent/test_mega10_fixes.py \
  services/agent/test_mega9_fixes.py \
  services/agent/test_mega8_rolesurface.py -q
```

**Result (this session):** `50 passed`.

---

## Files touched

| Path | Change |
|---|---|
| `services/agent/reviewer_exec.py` | battery-evidence AC grading; honest interpret; multi-doc resolve rank; params_hint forward; interference rule |
| `services/agent/loop.py` | battery-first `_run_reviewer_checks` + pass battery_payload |
| `services/agent/test_mega10_fixes.py` | H/I/J regressions |
| `services/agent/test_mega9_fixes.py` | order assertion for battery-before-acs |
| `reports/runs/mega-10/fixes.md` | this log |

---

## Ready for MegaRun10 primary launch

Fixes 1–2 (H AC grading + I doc_id) are **bound and unit-green**. DM
`UniformGoat` to launch on these modules. Expected:

- `battery_result.doc_id` non-null when BUILD produced geometry
- AC1 = PASS or FAIL from battery evidence (never residual NOT-CHECKED “no
  verdict field” when scores/findings present)
- AC2–AC9 still fail/not-checked until product geometry/ECAD/fw legs complete
  (honest); H only requires ≥1 AC evidence-graded
- typed-refusal: harness E2E green; live emission still model-dependent

---

## Live mega-10 primary (UniformGoat, wall 315.96s)

**Terminal:** `needs_correction` /
`verify_failed: ['AC1','AC2','AC4','AC5','AC6','AC7','AC8','AC9']`
(**AC3 absent** — it PASSed).

| Axis | Live | Evidence |
|---|---|---|
| **H** AC closure | **PASS** | **AC3 PASS** from real `check_interference` artifact (`interferences empty`, `doc_id=doc-cpp00000001`, volume≈84868). **AC1 FAIL** (not NOT-CHECKED) from battery_result evidence (`source=battery_result`, scores+findings, `is_manifold=1.0` geometry-ok; FAIL driven by ecad/sketch arg checks). Closure > 0. |
| **I** battery doc_id | **PASS** | `battery_result.doc_id=doc-cpp00000001` (only open geometry-bearing part; add_features×4). No multi-part ambiguity on measure/interference. |
| **J** typed-refusal | **PASS** (harness) | Unit E2E green; live refuse envelope still 0 (model residual). |
| A–G regression | **PASS** | Geometry ops accepted; clean verify_failed stop_reason; cap-sep/oneshot/terminal intact. |

Post-primary polish (detail only, no relaunch): when AC envelope has
`ok=false` plus scores/findings, detail prefers failing-findings string over
bare `ok=false` (tests still 50 passed).

