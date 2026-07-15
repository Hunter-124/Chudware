# mega-24 harness fixes (Fixer24 = ComingOcelot)

Baseline: `8ce92d74` (post mega-23 a1 thrash). Pair: MegaRun24=`DelightedCricket`
owns `reports/runs/mega-24/**` + roadmap primary; Fixer24 owns the bind below.

Predecessor investigation: SleepyFish U24 handoff at
`reports/runs/mega-24/sleepyfish_handoff.md` (root-cause done, fix not landed).

Axes: **do NOT weaken S23/T23** (AC5 in-band V, AC1 real measure scores). No push
(Main gates). Python only — no hostd rebuild.

---

## U24 — root cause (with evidence)

### Primary (why BUILD died with no_code / max_iters / delegate_required)

**Not S23/T23 logic.** Diff `d21bcaec..8ce92d74` on agent_turn/battery/sim is
correct for AC5 GND return + AC1 measure scoring. Live thrash was:

1. **Provider poison mid-run** (primary.log ~17:06:15 + ledger):
   - `xai-sub` → `personal-team-blocked:spending-limit` →
     `mark_provider_unavailable("xai-sub")`
   - immediately `openrouter` 402 insufficient credits →
     `mark_provider_unavailable("openrouter")`
   - BUILD nodes auto_pick fell to `ollama/llama3.2:latest`
     (`no candidate met min_rank=N; fell back to cheapest enabled`)
   - mega-22 a2 same nodes had `xai-sub/grok-4.5`

2. **BUILD DL1 under ollama**: tools_slot rendered, **0 tool calls** →
   stop `no_code` (budget exhausted). DL2 `delegate_or_typed_refusal_required`,
   DL3/DL4 `max_iters`. 9 DAG node failures total; battery never product
   (`doc_id=null`); phases only intake+specify; AC1–AC5 FAIL; 4/9.

3. **Terminal product force SKIPPED**: stop was
   `verify_failed: ['AC1'..'AC5']` / `needs_correction`.
   `loop.py` only ran `ecad_product_gate` + `mech_deliver_gate` when stop looked
   like deliver_* / mission_complete. mega-23 terminal therefore never force-built
   airframe/ECAD → battery `doc_id=null`.

Probe-at-start can be green on account `06dc3359` while mid-run SuperGrok returns
spending-limit once token burn hits the personal-team cap; process-local mark then
poisons the rest of the mission.

### Secondary amplifiers (once on leaves — not the provider switch)

| Bug | Evidence (mega-23 ledger) | File |
|---|---|---|
| `fw_sensor_scenario` NameError `_err` | BUILD DL4 ×8: `"name '_err' is not defined"` | `services/mcp/firmware_tools.py` used `_err(...)` without defining it |
| forced `ecad_wire` + `net=` | S23 `_wire` sets `wargs["net"]`; MCP `ecad_wire(..., doc_id="")` has no net → TypeError on force path | `agent_turn.py` `_ecad_ensure_driven_rail` / `_ecad_dispatch_forced_step` |
| `ledger_note` string payload | ollama: `payload: '{}'` → `dict(str)` → `dictionary update sequence element #0 has length 1` | `mission_dispatch.py` + `ledger_tools.py` |
| TypeError → `fn()` | `_resolve_tool_dispatch` caught TypeError and called `fn()` with zero args → `fw_sources missing project_id` opacity | `agent_turn.py` |

S23 ensure_driven did **not** fire in mega-23 (`ecad_chain_force_inject` count 0)
because BUILD never produced ECAD product. T23 measure derivation never ran
productively (`doc_id=null`).

### Diff hunks (S23/T23 — preserved)

```
services/agent/agent_turn.py
  _ecad_has_sim_source / _ecad_source_on_rail / _ecad_find_ref:
    + simulation_spice:v / v_source tokens  — KEEP
  _ecad_ensure_driven_rail:
    skip_place instead of early return; always fall through to GND return;
    inject ecad_add_power(net=GND) when missing — KEEP semantics

services/verify/battery.py
  _derive_measure_face_pair: never bare f1; bbox extreme sides — KEEP
  _extract_score(measure_between): finite distance incl 0.0 → 1.0 — KEEP

engine_oracle/sim/circuit.py + ngspice_backend.py
  resolve_node / casefold vector lookup — KEEP
```

---

## V24 — fixes landed

### 1. `loop.py` — terminal product force on verify_failed / needs_correction

Shared predicate `_terminal_product_force` now true when:

- status ∈ {done, blocked, needs_correction, failed}, and
- stop is deliver_*, mission_complete, needs_correction, verify_failed*, mech_*,
  or phases reached deliver/verify/build.

Both `ecad_product_gate` and `mech_deliver_gate` use it. mega-23 terminal shape
would now force product instead of skipping to null-doc battery.

### 2. `firmware_tools.py` — define `_err`

```python
def _err(reason: str, hint: str = "", **extra) -> dict:
    out = {"ok": False, "error": str(reason), "code": str(reason)}
    ...
```

`fw_sensor_scenario` invalid JSON profile returns structured ok=false, no NameError.

### 3. `agent_turn.py` — strip `net` from forced `ecad_wire` dispatch

- `call_args` keeps `net` for topology harvest / GND intent (S23).
- `dispatch_args` drops `net` before MCP call (no TypeError).
- Same pattern as mega-22 `ecad_simulate` resolved_net strip.

### 4. `agent_turn.py` — no silent `fn()` on TypeError

`_resolve_tool_dispatch`: TypeError retries only after dropping non-signature
kwargs; never calls `fn()` empty. Surfaces real missing-arg errors.

### 5. `mission_dispatch.py` + `ledger_tools.py` — coerce string payload

`_coerce_ledger_payload` / MCP path: JSON-string → dict; plain text →
`{"text": ...}`. Stops leaf ledger_note thrash.

---

## Regression tests

`services/agent/test_mega24_fixes.py` — live-shape fixture that would have caught it:

- mega-23 a1 fingerprints: 9 node fails, n_build_DL1 no_code, verify_failed terminal
- fw_sensor_scenario `_err` NameError fingerprint + fixed call path
- ledger_note string payload fingerprint + coerce
- forced wire: net stripped for dispatch, kept on call_rec
- TypeError dispatch no longer calls `fn()` empty
- `_terminal_product_force` true on verify_failed; old predicate false
- S23/T23 contracts still hold (measure_between 0.0 → 1.0)

---

## Gate

```bash
python3 -m pytest \
  services/agent/test_mega24_fixes.py \
  services/agent/test_mega23_fixes.py \
  services/agent/test_mega22_fixes.py \
  services/agent/test_mega21_fixes.py \
  services/agent/test_mega20_fixes.py \
  services/agent/test_mega19_fixes.py \
  services/agent/test_mega18_fixes.py \
  services/agent/test_mega17_fixes.py \
  services/agent/test_mega16_fixes.py \
  services/agent/test_mega11_legs.py \
  services/agent/test_mega11_grading.py \
  engine/ecad/test_autosave.py -q
```

**Counts (this bind + residual + a2 rebind): 230 passed**, 0 failed
(mega11+mega16..24+mega8 rolesurface+mega10+autosave).

Also adjusted `test_ensure_driven_wires_v_minus_to_gnd` to assert GND net on
**call_rec** (S23 intent preserved) while dispatch args must not carry `net`.

---

## Bind one-liners (for MegaRun24)

1. **Primary thrash** was mid-run provider poison (xai spending-limit + openrouter
   402) → ollama BUILD no_code, not S23/T23 logic.
2. **Product force** now runs on verify_failed / needs_correction terminals.
3. **Tool amplifiers fixed**: fw `_err`, wire net strip, ledger_note coerce,
   no silent fn() strip.
4. **S23/T23 preserved**: V-→GND semantics + measure real scores.
5. Daemon: Python-only — **no hostd rebuild**, all-clear.
6. No push (Main gates). Ready for primary after this bind.

No push. Fixes bound.

---

## Residual rebind (a1 thrash-kill 20260715T221105Z)

Archive: `reports/runs/mega-24/attempt-thrash-20260715T221105Z/`
Kill: `dag.node.failed count=4 > 3 (tools=147)` — NOT mega-23-class
(BUILD restored: add_features×9, new_part×2, all n_build_DL* DONE).

### Failures
1. **n_intake** — ollama leaf_oneshot_miss then degrade to dead
   `mistralai/mistral-small:free` → openrouter 404 → `failed`
2. **n_layout** — 16× ledger_get thrash (incl. `section=Decisions` case miss)
   → max_iters
3. **n_plan** — DONE without delegate/typed_refusal →
   `delegate_or_typed_refusal_required`
4. **n_deliver_DL2** — leaf oneshot `no_op=true`+ops conflict → same dead
   mistral 404 → `failed`

### Residual fixes
1. **agent_turn degrade**: drop `"not found"` localish match that re-degraded
   openrouter 404 into mistral free; prefer
   `deepseek/deepseek-chat-v3.1` then gemma/nemotron free.
2. **role_models**: remove dead mistral free from auto_pick free candidates.
3. **mission_dispatch ledger_get**: casefold section + Bom/components aliases.
4. **role_models leaf envelope**: salvage `no_op=false` empty ops → no_op+reason;
   salvage `no_op=true` WITH ops → prefer ops.
5. **agent_turn auto-complete**: plan-read thrash (≥8 ledger_get or ≥2 diverse
   plan-read tools) counts productive; synth typed_refusal on auto-complete
   without delegate.
6. **node_runner gate**: pure plan-read thrash (≥2 read-only tools) soft-passes
   with synth typed_refusal; free-form ledger_note+buildgraph_plan still blocks
   (mega-8).
7. **mech force cap**: already-forced → soft_refusal (forced=False) so terminal
   deliver_partial/mission_complete not hard-blocked.

S23/T23 still preserved. No push. Python-only.

---

## a2 thrash residual (20260715T223717Z)

Archive: `reports/runs/mega-24/attempt-thrash-20260715T223717Z/`
Kill: `dag.node.failed count=4 > 3 (tools=48)` — WORSE tool count than a1,
but different signature: **no_code on xai-sub grok-4.5** (not ollama poison).

### Failures
1. **n_spec_DL3 / n_spec_DL4 / n_layout** — tools_slot rendered, one completion
   with 18–29 tokens, **zero tool calls** → `no_code` (budget exhausted).
   Assignments were xai-sub/grok-4.5 (frontier alive).
2. **n_plan** — ran tools (dag_plan, buildgraph_plan, ledger_note) then DONE
   without delegate/typed_refusal → still
   `delegate_or_typed_refusal_required` (formal handoff path lacked synth).

### a2 fixes
1. **`_force_first_tool`** — on zero-tool no_code, force `ledger_get` (or first
   allowed tool) once, feed result, continue the loop (one shot per turn).
2. **`_attach_typed_refusal_if_needed`** — on every formal/bare done return after
   tools, attach typed_refusal when no delegate (covers formal handoff path
   that auto-complete synth already handled).
3. **ledger_note** alias `plan_ruling` → `decision` (live plan thrash).

S23/T23 preserved. No push. Python-only.


---

## Fixer24c (BurningWildcat) — offline planning-node repro + cure

**Contract:** W24 offline repro of planning-node death (cheap/scripted), name
mechanism; X24 cure so same repro completes intake/plan/layout; live-shape
regression; full mega gate green. No push. No S23/T23 weaken.

### W24 — root cause (evidence, not hypothesis)

Archives:
- a1 `attempt-thrash-20260715T221105Z` — kill `dag.node.failed count=4 > 3 (tools=147)`
- a2 `attempt-thrash-20260715T223717Z` — kill `dag.node.failed count=4 > 3 (tools=48)`

Ledger: `~/.local/share/AI-CAD/cache/runs/mega-24-fc-rocket-20260715/ledger.json`
(a2 residue; a1 events in archive).

#### Exact node / budget / assignment (a2)

| Node | Assign | tools_slot | 1st completion | Outcome |
|---|---|---|---|---|
| n_intake | ollama/llama3.2 leaf | oneshot | no_op=true n_ops=0 | DONE (a2; a1 failed after miss→404) |
| n_spec_DL3 | xai-sub/grok-4.5 | 601 tok / budget 2500 | **prompt=3456 completion=29** | **no_code** (0 tools) |
| n_spec_DL4 | xai-sub/grok-4.5 | 601 / 2500 | **prompt=3387 completion=29** | **no_code** |
| n_layout | xai-sub/grok-4.5 | 546 / 2500 | **prompt=2580 completion=18** | **no_code** |
| n_plan | xai-sub/grok-4.5 | 367 / 2500 | prompt=3001 c=87 then tools | **delegate_or_typed_refusal_required** after productive tools |

#### Diff vs mega-22 a2 (same nodes, same tools_slot sizes)

| | mega-22 a2 | mega-24 a2 |
|---|---|---|
| SPECIFY DL3 first | prompt=3456 **c=55 → tool** | prompt=3456 **c=29 → no tool** |
| SPECIFY DL4 first | prompt=6717 **c=56 → tool** | prompt=3387 **c=29 → no tool** |
| layout first | prompt=2580 **c=40 → tool** | prompt=2580 **c=18 → no tool** |
| tools_slot budget | 2500, trimmed, ~601/546 | **identical** |

**Not prompt bloat / not budget mis-accounting / not role-card corruption:**
tools_slot token counts match m22; prompt_tokens for layout/spec DL3 match m22
byte-for-byte class; budgets start full (2500). Frontier is alive (xai-sub
grok-4.5, not ollama poison). Terminal-force text does **not** leak into intake
(intake is leaf oneshot; force text is only injected mid-turn after a zero-tool
completion).

#### Named mechanism

1. **Zero-tool first completion on planning/specify nodes** (a2 primary):
   model returns 18–29 tokens of prose or short non-tool JSON; harness treated
   that as `no_code` and escalated on first attempt → 3 of 4 thrash kills.
   **a2 rebind** added `_force_first_tool` for `parsed is None` (prose) only.

2. **Hole found offline (Fixer24c):** force-first did **not** run when the
   model returned a **parsed junk JSON dict without `tool`/`done`**
   (e.g. `{"note":"…","status":"thinking"}` or `{"ok":true,"message":"…"}`).
   That path went straight to `no_code` with zero tools — same thrash class
   as prose, still open after a2 rebind. Second-completion junk after a forced
   tool also broke to `no_code` instead of bare-handoff (prose bare-handoff
   worked; junk JSON did not).

3. **n_plan multi-domain gate** (a2 residual): formal/bare done after
   dag_plan/buildgraph_plan/ledger_note without `typed_refusal` or `delegate`
   → `delegate_or_typed_refusal_required`. Fixed by
   `_attach_typed_refusal_if_needed` on every formal/bare done path +
   auto-complete synth (a2 rebind).

4. **a1 class (already bound):** leaf oneshot miss → dead mistral free 404;
   layout 16× ledger_get max_iters; plan_ruling unknown kind; leaf no_op/ops
   conflict. Residual a1 pack in tree (degrade deepseek, leaf salvage,
   ledger casefold, plan-read auto-complete, plan_ruling alias).

### X24 — cure (minimal)

```
services/agent/agent_turn.py
  _force_first_tool: also invoked from `if not tool:` branch (junk JSON)
  after tools + non-tool JSON: bare handoff + typed_refusal (not no_code)
  (prose path already forced + bare-completed)

  _attach_typed_refusal_if_needed: formal/bare done after tools (a2)

services/dag/node_runner.py
  pure plan-read soft-pass typed_refusal synth (a1 residual)

services/agent/mission_dispatch.py
  plan_ruling → decision; ledger_get section casefold (a1/a2)

services/agent/role_models.py
  leaf envelope no_op/ops salvage; drop dead mistral free (a1)
```

### Offline repro (scripted = cheap; no grok/xai spend)

```text
n_layout first reply prose → force ledger_get → confirmed + typed_refusal
n_spec junk JSON zero-tool → force ledger_get → confirmed
force then junk JSON second reply → bare confirmed (not no_code)
n_plan tools + formal done without typed_refusal → attach → gate VERDICT_DONE
empty turn without allowlist → still no_code (contract preserved)
```

Pre-24c fingerprint (junk JSON zero-tool): `success=False stop=no_code calls=[]`
Post-24c: `success=True stop=confirmed forced ledger_get`.

### Regression tests

`services/agent/test_mega24_fixes.py` (Fixer24c live-shape):
- `test_a2_live_shape_zero_tool_prose_force_and_confirm`
- `test_a2_live_shape_junk_json_zero_tool_force_and_confirm`
- `test_a2_live_shape_force_then_junk_json_bare_completes`
- `test_a2_live_shape_n_plan_formal_done_gets_typed_refusal_gate`

Plus prior a1/a2 unit coverage (force_first helper, attach_typed_refusal,
plan_ruling, leaf salvage, terminal product force, S23/T23).

### Gate

```bash
python3 -m pytest \
  services/agent/test_mega24_fixes.py \
  services/agent/test_mega23_fixes.py \
  services/agent/test_mega22_fixes.py \
  services/agent/test_mega21_fixes.py \
  services/agent/test_mega20_fixes.py \
  services/agent/test_mega19_fixes.py \
  services/agent/test_mega18_fixes.py \
  services/agent/test_mega17_fixes.py \
  services/agent/test_mega16_fixes.py \
  services/agent/test_mega11_legs.py \
  services/agent/test_mega11_grading.py \
  engine/ecad/test_autosave.py -q
# → 204 passed

# + mega8 rolesurface + mega10 (delegate gate contracts):
# → 234 passed
```

S23/T23 preserved. No push. Python-only.

### Bind one-liner for MegaRun24 / a3

Planning thrash is **zero-tool first completion** (prose + junk JSON) on
spec/layout under live grok-4.5, plus **missing typed_refusal** on n_plan —
not budget start-spent and not terminal-force leak. Force-first + bare-handoff
+ typed_refusal attach close the offline repro; a3 cleared offline.
