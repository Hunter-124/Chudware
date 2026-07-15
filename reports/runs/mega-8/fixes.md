# mega-8 harness fixes (Fixer8 = CollectiveElk)

Live mission: MegaRun8 = SecretScorpion (`reports/runs/mega-8/**`).
Baseline commit: `7ef7b577` (cycle-8 wave bind target).
Scope: harness enablement only (services/agent|dag|mcp + gym). No C++. No grading changes.

Owner watch-list this cycle:
1. op-vocab teaching not reaching the model (cheatsheet/injection bugs)
2. typed-refusal gate misfiring or blocking wrongly
3. leaf envelope validation bugs
4. auth/routing/timeout regressions

---

## Live fingerprints that drove fixes

### mega-8 attempt-1 (pid 2410582, account 17d17fb1, pre-Fixer8 rebind)

Preflight PASS (spawn_probe cap_sep; auto_pick depth2→ollama). Live early (~2–3 min):

| Signal | Evidence |
|---|---|
| `leaf_oneshot_miss` ×6+ | INTAKE, n_spec_DL5, n_spec_done, n_deliver_DL3/4/5 — error `"string indices must be integers, not 'str'"` with provider=ollama/llama3.2 |
| `delegate_or_typed_refusal_required` | n_spec_DL1/DL2/DL4 escalated after budget — gate fired on SPECIFY workers |
| `no_code` ollama leaves | n_intake, n_deliver_DL4 after oneshot miss fell through to empty tool loop |
| model_assignment | 6 ollama / 4 xai early — conservation OK |
| delegate / add_features | 0 / 0 (still specify fan-in at fingerprint time) |
| op-vocab cheatsheet | not exercised (no add_features yet); run_id missing from context_pack (latent) |

---

## Fix 1 — Cheap-leaf oneshot + OpenAI/ollama string content (axis C)

**Fingerprint:** Every ollama leaf hit `leaf_oneshot_miss` with
`string indices must be integers, not 'str'` before any model call completed.
Root cause: `_maybe_run_cheap_leaf_oneshot` built
`messages=[{"role":"user","content": <plain str>}]`, but
`OpenAIProvider._message` assumed list-of-parts and did `p["type"]` on each
character. XAISubProvider already accepted strings; OpenAI/ollama did not.
Consequence: axis C zero oneshot completions; leaves degraded to no_code.

**Fix:**
1. `services/agent/providers/openai_provider.py` `_message`: accept plain-string
   `parts` (same shape as XAISubProvider) before the list-of-parts path.
2. `services/dag/node_runner.py` oneshot: emit agent_turn list-of-parts
   `[{"type":"text","text": …}]` so Anthropic-shaped providers also work.

**Regression:**
- `test_openai_provider_accepts_string_content`
- `test_leaf_oneshot_messages_use_parts_form`
- existing `test_leaf_oneshot_success_skips_tool_loop` / miss fallback

**Evidence (file:line):**
- `services/agent/providers/openai_provider.py:57-69` (`_message` string branch)
- `services/dag/node_runner.py:576-582` (oneshot messages parts form)
- Live ledger: `leaf_oneshot_miss` payloads with the TypeError string

---

## Fix 2 — run_id on context_pack (axis A cheatsheet keying)

**Fingerprint:** `context_pack.assemble()` never put `run_id` on the pack;
`graph_loader` assembled packs without `state["run_id"]`.
`agent_turn.run_turn(..., run_id=context_pack.get("run_id"))` therefore got
`None`, so `_maybe_inject_op_vocab_cheatsheet` keyed every turn as
`"_standalone"`. First unknown-op inject works once process-wide, but
parallel missions / multi-turn isolation and overseer join telemetry lose the
mission id. Comment in `node_runner` already noted the missing thread.

**Fix:**
1. `context_pack.assemble`: promote `run_id` / `domain_tags` from inputs;
   also accept `run_id` from `node_state`.
2. `graph_loader._make_node_handler`: after assemble, seed `pack["run_id"]`
   from LangGraph `state["run_id"]` (agent path + verify path).
3. `_default_agent_fn`: if pack still missing run_id, copy from bound ledger.

**Regression:**
- `test_context_pack_and_graph_seed_run_id`

**Evidence:**
- `services/dag/context_pack.py:123-133`
- `services/dag/graph_loader.py:285-289, 317-324`
- `services/dag/node_runner.py:420-429`
- Pre-fix probe: `assemble(...).get("run_id") is None`

---

## Fix 3 — Typed-refusal gate over-applied to SPECIFY (axis B misfire)

**Fingerprint:** n_spec_DL1/DL2/DL4 died
`escalated: delegate_or_typed_refusal_required (budget exhausted)`.
Mission template stamps **all** workers `type=design` (including `n_spec_*`).
`is_multi_domain_plan_or_build` treated `ntype in ("design","build")` OR
`tc in (assembly, multi_feature_part)` as BUILD, so every multi-feature/assembly
SPECIFY worker was gated. Free-form ledger_note auto-complete then blocked until
retry budget exhausted — wrong forcing function (axis B is PLAN/layout +
multi-domain BUILD, not SPECIFY product writers).

**Fix:** Narrow `is_multi_domain_plan_or_build`:
- Hard-exclude `spec_writer`, `reviewer`, `intake_analyst`, `clarifier`, `overseer`.
- PLAN = coordinating roles / `ntype=="plan"` only.
- BUILD = geometry/assembly **roles** or `ntype=="build"` only — **not** bare
  `type=design` or task_class alone.
- Keep multi-tag / layout_engineer plan-role sticky rules.

**Regression:**
- `test_typed_refusal_gate_skips_specify_workers` (schema + live-shaped node run)
- existing PLAN free-form block / typed refusal accept / delegate accept / max depth

**Evidence:**
- `services/agent/role_models.py:994-1050` (`is_multi_domain_plan_or_build`)
- Live events: `n_spec_DL1/2/4` stop_reason `delegate_or_typed_refusal_required`
- Probe: `is_multi_domain_plan_or_build(role_id=spec_writer, node_type=design, …)` was True

---

## Verification

```text
python3 -m pytest services/agent/test_mega8_op_vocab.py \
  services/agent/test_mega8_rolesurface.py \
  services/agent/test_providers_conformance.py::test_openai_wire_shape -q
30 passed
```

## Files touched

| Path | Change |
|---|---|
| `services/agent/providers/openai_provider.py` | `_message` accepts plain-string content |
| `services/dag/node_runner.py` | oneshot parts form; seed run_id from ledger |
| `services/dag/context_pack.py` | promote run_id + domain_tags |
| `services/dag/graph_loader.py` | seed pack run_id from DagState |
| `services/agent/role_models.py` | narrow multi-domain gate (no SPECIFY) |
| `services/agent/test_mega8_rolesurface.py` | regressions for Fix 1–3 |

## Bind note for MegaRun8

Attempt-1 (pid 2410582) bound baseline `7ef7b577` **before** Fixer8 edits —
needs **relaunch** for Fixes 1–3. Recommend distinct attempt archive if
artifacts are mixed under the same RUN_ID.

DM'd SecretScorpion mid-run on Fix 1 + gate over-apply; relaunch is their call.

---

## Fix 4 — Leaf envelope salvage for truncated ollama JSON (axis C residual)

**Fingerprint (attempt-2, Fix1 bound):** INTAKE `leaf_oneshot_ok` (n_ops=3) —
string-index crash gone. Residual misses:
- SPECIFY DL5: `json parse error: Expecting ',' delimiter` on truncated
  `ops:[{"type":"fit_calc",` blob (reply_head in ledger)
- DELIVER DL5 / n_spec_done: similar control-character / property-name errors

**Fix:** `parse_leaf_envelope_text` strips trailing commas, accepts unclosed
`{…` blobs, and salvages `{goal, no_op:true, reason}` when ops[] is junk so
cheap leaves can still complete the envelope contract.

**Regression:** `test_leaf_envelope_salvages_truncated_ops_json`

**Evidence:** `services/agent/role_models.py:1086-1149`; live
`leaf_oneshot_miss` reply_head on SPECIFY DL5.

**Bind:** landed mid attempt-2 — needs relaunch if residual DL5-class misses
must flip to ok on the same process.

---

## Fix 5 — Payload `ok:false` must mark tool call failed (axis A residual)

**Fingerprint (attempt-2):** BUILD DL1 `new_part` ok → `add_features` with
`{"op":"cylinder",…}` → engine `"unknown op 'cylinder'"`. Ledger recorded
`tool_call ok: True` because dispatch only sets ok=False on **raised**
exceptions; MCP returns JSON `{ok:false}` as a normal string/dict. Downstream:
- `productive_tools` included `add_features` → max_iters **auto-complete** on
  hollow solid (bootstrapped_only gate defeated).
- Preflight/add_features success counters treated the reject as success.

Cheatsheet inject still fires (uses result text, not call ok) — but the turn
then celebrated a failed op as product work.

**Fix:** After `dispatch`, if result (or nested JSON) carries `ok`, use that
boolean as the call outcome.

**Regression:** `test_dispatch_ok_false_when_payload_rejects_ops`

**Evidence:** `services/agent/agent_turn.py` post-dispatch ok derivation;
ledger seq 396 `add_features` result unknown op cylinder with `ok: True`.

---

## Attempt-2 evidence (Fixer8 1–3 bound, pid 2453640, wall 926s)

| Axis | Evidence | Verdict |
|---|---|---|
| A add_features accepted | new_part ok + add_features×1 with `unknown op 'cylinder'`; call_rec.ok wrongly True (pre-Fix5); hollow auto-complete | **PARTIAL** (sequence yes, solids no; Fix5 unbinds) |
| B delegate / typed refusal | delegate×1 on BUILD DL2 → source-parts spawn depth1; n_layout/n_plan `delegate_or_typed_refusal_required`; SPECIFY **not** false-gated | **PASS** forcing function |
| C leaf oneshot | leaf_oneshot_ok×3 (INTAKE, DELIVER DL3, VERIFY DL1); miss residual truncated JSON (Fix4 unbound) | **PASS** (>0 ok + miss telemetry) |
| D terminal status | `needs_correction` / `verify_failed: [AC1…AC9]` — **not** running+empty | **PASS** (explicit stop_reason) |

Mission `phases_completed=["intake"]` only (VERIFY gate failed → CORRECT) — soft-join deliver path not the terminal class this attempt.

### Tests at attempt-2 close

```text
python3 -m pytest services/agent/test_mega8_op_vocab.py \
  services/agent/test_mega8_rolesurface.py -q
31 passed
```

### Pending bind on attempt-3

- Fix 4 leaf JSON salvage
- Fix 5 payload ok:false → call failed (honest productive_tools / no hollow auto-complete)

---

## Fix 6 — Normalize null reason / string artifacts in leaf envelope

**Fingerprint (attempt-3):** INTAKE miss `leaf_envelope.reason: expected string,
got NoneType` on a payload with valid ops[]; DL5 miss on string artifacts.

**Fix:** `normalize_leaf_envelope` drops null reason/no_op, wraps string
artifacts as `{name:…}`, filters non-object ops; parse returns normalized obj.

**Evidence:** `services/agent/role_models.py` normalize + validate; live
attempt-3 INTAKE reply_head with `"reason"` absent/null + ops present.

---

## Attempt-3 evidence (Fixer8 1–5 bound, pid 2544922, wall 1003.84s)

| Axis | Evidence | Verdict |
|---|---|---|
| A add_features accepted | new_part×1 on BUILD DL1; **0** add_features (no_code max_iters after ledger thrash); Fix5 unbound for unknown-op path this attempt | **FAIL** solids (Fix5 ready, not exercised) |
| B delegate / typed refusal | delegate×1; n_layout + n_build_DL2 `delegate_or_typed_refusal_required`; n_plan no_code | **PASS** |
| C leaf oneshot | leaf_oneshot_ok×3 + miss telemetry (Fix4 salvage bound; Fix6 null-reason residual mid-run) | **PASS** |
| D terminal | `needs_correction` / `verify_failed: [AC1…AC9]`; phases_completed intake+specify | **PASS** (not running+empty) |

### Tests

```text
python3 -m pytest services/agent/test_mega8_op_vocab.py \
  services/agent/test_mega8_rolesurface.py -q
31 passed
```

### Files touched (all Fixer8)

| Path | Fixes |
|---|---|
| `services/agent/providers/openai_provider.py` | Fix1 string content |
| `services/dag/node_runner.py` | Fix1 oneshot parts; Fix2 run_id from ledger |
| `services/dag/context_pack.py` | Fix2 run_id/domain_tags promote |
| `services/dag/graph_loader.py` | Fix2 run_id from DagState |
| `services/agent/role_models.py` | Fix3 gate narrow; Fix4/6 leaf parse+normalize |
| `services/agent/agent_turn.py` | Fix5 payload ok:false |
| `services/agent/test_mega8_*.py` | regressions |

---

## Fix 7–10 — Graded OPEN defects (post attempt-3)

MegaRun8 findings F7–F10 after a3 grade. Source landed; needs next relaunch.

### Fix 7 — `UNKNOWN_OP_THIN_ERROR` (rich closest + cad_guide)

**Fingerprint:** a2 `add_features` returned only `op[0]: unknown op 'cylinder'`
(no closest, no cad_guide). Python twin `unknown_doc_op_message` already rich.

**Fix:** `services/mcp/op_vocab.enrich_unknown_op_error` + wire in
`add_features` return path. Rewrites thin errors with feature-type hint,
`closest:`, and `see cad_guide()`.

**Regression:** `test_enrich_unknown_op_error_adds_closest_and_cad_guide`

### Fix 8 — `OP_VOCAB_CHEATSHEET_NOT_OBSERVED`

**Fingerprint:** inject only mutated ephemeral messages; mega grading grepped
ledger/events → 0 hits.

**Fix:** on inject, `_log` ledger event `op_vocab_cheatsheet_inject` with head
snippet (once-per-mission still holds).

### Fix 9 — typed-refusal success path unused

**Fingerprint:** layout/plan die `delegate_or_typed_refusal_required` without
ever seeing a typed-refusal template.

**Fix:** `_build_request` appends CAPABILITY/DELEGATION GATE scaffold with
exact `typed_refusal` JSON shape when multi-domain PLAN/BUILD; retry path
includes prior `delegate_gate` blocked hint on context_pack.

**Regression:** `test_multi_domain_request_includes_typed_refusal_scaffold`

### Fix 10 — a3 new_part then 0 add_features

**Fingerprint:** BUILD thrash ledger_get after new_part; soft nudge only once.

**Fix:** stronger post-`new_part` message with concrete `add_feature`+box
example; re-nudge on every subsequent ledger/describe/preflight until
`add_features` ok:true.

### Tests

```text
python3 -m pytest services/agent/test_mega8_op_vocab.py \
  services/agent/test_mega8_rolesurface.py -q
```
