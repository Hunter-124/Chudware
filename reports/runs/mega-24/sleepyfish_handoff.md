# SleepyFish (ex-Fixer24) → ComingOcelot handoff — mega-24 U24 root-cause

Status: **root-cause identified with evidence; fix NOT implemented; tests NOT written; fixes.md NOT bound.**

Baseline: `8ce92d74`. Diff range investigated: `d21bcaec..8ce92d74` on
`services/agent/agent_turn.py`, `services/verify/battery.py`,
`engine_oracle/sim/circuit.py`, `engine_oracle/sim/ngspice_backend.py`.

## U24 — exact live thrash root cause

### Primary (why BUILD nodes died with no_code / max_iters / delegate_required)

**Not S23/T23 logic itself.** The S23 ensure_driven + T23 measure_between edits
are not what killed the live loop. Evidence:

1. mega-23 BUILD assignments (events.jsonl):
   - n_build_DL1/2/3/4 ALL got `ollama/llama3.2:latest` with reason
     `no candidate met min_rank=N; fell back to cheapest enabled`
   - mega-22 a2 same nodes got `xai-sub/grok-4.5`

2. primary.log + ledger at ~17:06:15:
   - `role_models: provider xai-sub marked unavailable` —
     `personal-team-blocked:spending-limit` (process-local via
     `mark_provider_unavailable` in agent_turn on provider_unavailable)
   - Immediately after: openrouter 402 → **also** marked unavailable
     (`Insufficient credits. This account never purchased credits`)
   - After both frontiers dead, auto_pick only has ollama left for BUILD

3. BUILD DL1 under ollama: tools_slot rendered, ONE completion, **zero tool
   calls** → stop `no_code` (budget exhausted). That matches the thrash
   signature in the ticket.

4. Terminal gates never recovered product: battery `doc_id=null`, phases only
   intake+specify, AC1–AC5 FAIL, 4/9 ungraded.

**Key nuance vs ticket claim "grok-4.5 account 06dc3359 probes OK":**
probe-at-start can be green while mid-run SuperGrok returns spending-limit
once token burn hits the personal-team cap. Process-local mark then poisons
the rest of the mission. openrouter paid mid was never funded (402) so the
Y-axis fallback also died.

### Secondary (tool thrash once on ollama — not the primary switch)

Live ledger tool failures under BUILD (ollama thrashing budget to max_iters):

| Bug | Evidence | File |
|---|---|---|
| `fw_sensor_scenario` NameError `_err` | m23 BUILD DL4: `"name '_err' is not defined"` on every non-empty call; empty profile "succeeds" | `services/mcp/firmware_tools.py:383` uses `_err(...)` inside nested `_parse` but `_err` is **never defined** in that module (only `_dumps`). Pre-existing; also hit in m22 but grok recovered via empty profile + fw_sim_run. |
| `ecad_wire` forced with `net=` kwarg | m22 cascade already logged `got an unexpected keyword argument 'net'`; S23 still injects `wargs["net"] = net or rail` in `_wire` (agent_turn ~3039) | `agent_turn.py` `_ecad_ensure_driven_rail` → `_wire`; MCP `ecad_wire(from_ref, from_pin, to_ref, to_pin, doc_id="")` — **no net** |
| `ledger_note` string payload | ollama sent `payload: '{}'` JSON-as-string → `dict(payload)` → `dictionary update sequence element #0 has length 1` | `mission_dispatch.py:257` `body = dict(payload or {})` — no JSON-string coerce |
| GrossShark kwargs strip (suspect) | **NOT the primary.** Only `ecad_simulate` strips `resolved_net`/`resolved_net_source`/`source_net` before force-dispatch (mega-22 a2, correct). Signature-based kwargs filter in `_resolve_tool_dispatch` is intentional. | agent_turn ~3754–3762, ~4270–4273 |

S23 ensure_driven (always force V-→GND) did **not** fire in mega-23 because
BUILD never produced ECAD product / force cascade (`ecad_chain_force_inject`
count m23=0 vs m22=16). So it cannot be the thrash source for this run.

T23 battery measure derivation also never ran productively (`doc_id=null`).

## Diff hunks that matter (for fixes.md)

### agent_turn.py (S23) — preserve semantics; fix kwargs
- `_ecad_has_sim_source` / `_ecad_source_on_rail` / `_ecad_find_ref`: add
  `simulation_spice:v` / `v_source` tokens — KEEP
- `_ecad_ensure_driven_rail`: `skip_place` instead of early return; always
  fall through to GND return; inject `ecad_add_power(net=GND)` — KEEP
  semantics
- **BUG to fix:** `_wire` always sets `wargs["net"]` but MCP rejects it →
  strip `net` before dispatch (same pattern as ecad_simulate resolved_net
  strip), keep net metadata on call_rec for topology harvest

### firmware_tools.py — real defect
- `fw_sensor_scenario` line ~383: `_err("invalid_request", ...)` → NameError
- Fix: define local `_err` like other mcp modules, or return
  `{"ok": False, "error": ...}` via `_dumps`

### mission_dispatch.py — live thrash amplifier on leaves
- `ledger_note`: if payload is str, `json.loads` before `dict()`

### role_models / agent_turn — process-local poison (optional but load-bearing)
- openrouter 402 marks **whole** openrouter unavailable → kills paid deepseek
  mid forever for the process even if key later works / different model
- Consider: do not mark openrouter unavailable on single-account 402 when
  xai-sub is the primary and mid is supposed to catch; OR scope mark to
  (provider, model) not whole provider; OR clear mark between phases
- Ticket said "failure was NOT provider quota" for 06dc3359 probe — but live
  log shows mid-run spending-limit. Fix should make BUILD survive when
  frontiers die: ensure terminal `mech_deliver_gate` + `ecad_product_gate`
  still run on `needs_correction`/`verify_failed` paths (currently only on
  deliver_partial/mission_complete/deliver*) — m23 stop was
  `verify_failed: ['AC1'..'AC5']` so **both terminal forces SKIPPED**

### Critical gate bug (likely high ROI)
`loop.py` ~2419–2496 terminal ECAD/mech force only when stop looks like
deliver. mega-23 terminal `needs_correction` + `verify_failed:...` → gates
never fire → no forced geometry/ECAD → battery doc_id null. Expand gate
predicates to include `needs_correction` / `verify_failed*` so product force
still lands when BUILD nodes thrash.

## Recommended fix pack (priority)

1. **loop.py**: run `ecad_product_gate` + `mech_deliver_gate` on
   `needs_correction` / `verify_failed*` terminals (not only deliver_*)
2. **firmware_tools.py**: fix `_err` NameError in `fw_sensor_scenario`
3. **agent_turn.py**: strip `net` from forced `ecad_wire` dispatch args
   (preserve on call record); keep S23 GND semantics
4. **mission_dispatch.py**: coerce string `payload` in `ledger_note`
5. **role_models** (if in scope): avoid marking entire openrouter dead on 402
   when only free/mistral endpoint failed, or don't poison mid forever
6. Regression test (live-shape fixture, not pure unit):
   - Simulate BUILD node with ollama-like no_code + verify_failed terminal →
     assert mech+ecad product force still runs
   - Call `fw_sensor_scenario(profile="{bad")` → no NameError, ok=false
   - Forced wire with net= does not TypeError
   - ledger_note(payload='{"name":"x"}') succeeds

## Gate command (from mega-23 fixes.md; re-run after fix)

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

Do NOT weaken S23/T23 pass rules. No push. DM DelightedCricket `fixes bound`
when ready + write `reports/runs/mega-24/fixes.md`.

## Artifact paths

- m23 ledger: `reports/runs/mega-23/ledger.json`
- m23 events: `reports/runs/mega-23/events.jsonl`
- m23 primary.log: spending-limit + openrouter 402
- m22 good: `reports/runs/mega-22/attempt-2-primary-graded-live7/`
- this handoff: `reports/runs/mega-24/sleepyfish_handoff.md`
