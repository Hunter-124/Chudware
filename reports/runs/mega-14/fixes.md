# mega-14 harness fixes (Fixer14 = ExceptionalYak)

Baseline: `7a1e1e5d`. Pair: MegaRun14=`WholeCatfish` owns `reports/runs/mega-14/**` +
roadmap; Fixer14 owns `services/agent|dag|mcp` + gym + this file.

Axes targeted:
- **S** `board_from_schematic` + `ecad_run_drc` executed live (P upgraded).
- **T** AC5 SPICE on a wired netlist (R upgraded).
- **U** AC1 battery: per-check diagnosis in findings / criterion_AC1.json
  (which checks zero, geometry evidence, concrete brief hints).
- Target: ≥7 live AC passes. Regression: A–Q hold.

Criteria were **not** weakened. Battery never weakened.

---

## Pre-run verify — Fix8 at baseline

Fix8 (hard ECAD chain gate, mega-13 post-a3) is present on baseline and
**unit-green**:

- `test_ecad_hard_gate_blocks_place_until_wire` — PASS
- Soft enablement scaffolds retained as secondary

Live residual from mega-13 a3 (pre-Fix8 primary): place×6 → max_iters, soft
nudges ignored, zero wire/board/DRC. Fix8 denies thrash but a model that only
retries blocked tools or emits bare text can still burn budget without ever
calling wire/board.

---

## Fix 1 — Hard-gate force-inject after denial cap (axes S / T)

**Fingerprint:** model thrash on place/find/ledger after schematic; hard
`tool_denied` fires but voluntary next tool never arrives → max_iters with
board_from_schematic=0, ecad_wire=0.

**Fix (agent_turn):**
1. Keep Fix8 hard deny of thrash tools after place-without-wire and after
   wire-without-board.
2. **New:** after `_ECAD_GATE_FORCE_AFTER` (2) denials, force-dispatch the next
   chain tool via `_ecad_chain_need` + `_ecad_forced_tool_args`:
   - place, no wire → `ecad_add_power(net=GND)` (or `ecad_wire` with placed refs)
   - wire/power present, no board → `board_from_schematic`
   - board present, no DRC → `ecad_run_drc`
3. Same force path on bootstrap-incomplete bare-text loops (cap=2 nudges).
4. Board→DRC hard gate: thrash after board product is denied until DRC runs.
5. Ledger events: `ecad_chain_gate` (denials), `ecad_chain_force_inject` (forced
   tool + args + reason).

**Regression:**
- `test_ecad_hard_gate_blocks_place_until_wire` (held)
- `test_ecad_hard_gate_force_injects_wire_after_thrash` (new)
- `test_ecad_chain_need_helpers` (new)
- `test_ecad_force_chain_injects_board_after_place` (updated: expects product tools)
- `test_ecad_wire_then_board_then_drc_chain`

---

## Fix 2 — AC1 per-check diagnosis (axis U)

**Fingerprint:** AC1 FAIL with only `soft_scores_zero=[...]` detail; live
mega-13 zeros were check_sketch / dim_coverage / ECAD-on-CAD / measure_between
arg misses while is_manifold+measure_part PASS — not hollow-solid, but U axis
not gradeable without re-parsing battery_result.

**Fix (reviewer_exec, honest — no battery weaken):**
1. `_battery_per_check_diagnosis(evidence, bat)` builds:
   - `per_check[]` name/verdict/score/message
   - `zero_checks[]`
   - `geometry_evidence` (manifold/measure/sketch/…)
   - `brief_hints[]` concrete next brief changes (wall≥1.5, bay dims, ignore
     ECAD-on-CAD on mechanical doc, sketch arg miss class)
2. `run_acceptance_checks` for `verify_battery` embeds diagnosis into
   criterion_result evidence so `criterion_AC1.json` is U-gradeable from
   artifacts alone.

**Regression:**
- `test_ac1_per_check_diagnosis_on_battery_payload`

---

## Fix 3 — Frontier measured rank floor → 5 (routing residual)

**Fingerprint (mega-13 archives):**
- attempt-1: measured grok-4.5 pass_rate=0 → rank 1 → all nodes ollama
- attempt-2: enabled `grok-4-5` (hyphen) → API HTTP 400 Model not found
- attempt-3: harness pinned `model_rankings["grok-4.5"]=5` because floor=4 still
  required a user override for analysis min_rank paths; findings TOP residual #4
  noted **rank floor ≥5**

**Decision: floor = 5.** Rationale:
- Shipped frontier rank is 5; measured zero-pass gym demotion must not drop
  below frontier band.
- analysis task_class min_rank=4; floor 5 keeps xai-sub/grok-4.5 eligible and
  preferred over ollama without per-run user pins.
- Leaves (min_rank=1) still auto_pick ollama (cheapest eligible).
- API model id remains **`grok-4.5` exact** (hyphen alias is not valid on xAI).

**Fix:** `_FRONTIER_MEASURED_FLOOR = 5` in `role_models.load_rankings`.

**Regression:**
- `test_auto_pick_frontier_floor_not_demoted_to_ollama` (asserts floor≥5 +
  load_rankings clamp)

---

## Test command

```text
python3 -m pytest services/agent/test_mega11_legs.py \
  services/agent/test_mega11_grading.py \
  services/agent/test_mega10_fixes.py -q
```

**Result (this session):** `77 passed`.

---

## Files touched

| Path | Change |
|---|---|
| `services/agent/agent_turn.py` | Fix8 force-inject after denial/bootstrap cap; board→DRC hard gate; helpers `_ecad_chain_need` / `_ecad_forced_tool_args` |
| `services/agent/reviewer_exec.py` | AC1 per-check diagnosis on battery criterion evidence |
| `services/agent/role_models.py` | frontier measured floor 4→5 |
| `services/agent/test_mega11_legs.py` | force-inject + chain helpers + floor≥5 regressions |
| `services/agent/test_mega11_grading.py` | AC1 diagnosis regression |
| `reports/runs/mega-14/fixes.md` | this log |

---

## Ready for MegaRun14 primary launch

Fixes 1–3 are **bound and unit-green**. DM `WholeCatfish` `fixes bound`.

Expected on primary (account `17d17fb1`, conservation unchanged, model id
**grok-4.5 exact**):
- ECAD BUILD thrash hits hard gate then force-inject → wire/power →
  `board_from_schematic` → `ecad_run_drc` (axis S / P upgrade; AC4 honest)
- AC5 either measures V(3V3) on wired netlist or fails honestly (axis T / R)
- AC1 criterion carries `per_check` / `zero_checks` / `brief_hints` for U
- Frontier auto_pick stays xai-sub/grok-4.5 on analysis (floor 5)
- Path to **≥7 live AC PASS** when product legs complete
- A–Q regression holds

### Live AC1 diagnosis seed (from mega-13 primary — for U grading)

| Check | Score | Class |
|---|---|---|
| is_manifold | 1.0 | geometry OK |
| measure_part | 1.0 | volume class OK (AC2 also PASS) |
| eval_check / groundedness / layout / query / unit / power | 1.0 | OK |
| check_sketch | 0.0 | arg miss (`sketch` required) — not hollow solid |
| dim_coverage | 0.0 | no open drawing |
| ecad_* / erc | 0.0 | ECAD-on-CAD doc mismatch (mechanical doc) |
| measure_between | 0.0 | arg miss (`a_query`) |

**Brief next change (do not weaken battery):** keep concrete wall≥1.5 / bay dims;
prefer solid feature path over free sketches; ignore ECAD soft zeros on airframe
doc for geometry product judgment.

---

## Live primary (pid 3590131, wall 797.46s) — post-grade residual fix

**Terminal:** `needs_correction` / `verify_failed: ['AC1','AC4','AC5']`; live PASS=6
(AC2,3,6,7,8,9). Axes: **S PASS**, **T FAIL**, **U PASS**.

### What held
- Fix8 + force-inject: `ecad_chain_gate` denials ×2 → `ecad_chain_force_inject`
  `board_from_schematic` → `ecad_run_drc` errors=0 (S / P upgrade).
- AC1 diagnosis live: `zero_checks` / `per_check` / `brief_hints` / geometry_evidence
  on criterion_AC1 (U).
- Frontier floor 5: xai-sub/grok-4.5 rank=5 on analysis (no ollama wipe).

### Residuals observed
1. **T / AC5:** only `ecad_add_power(GND)` with `net_size=0` (orphan port); **zero
   `ecad_wire`**; IC-only places (U1–U3) with no sim models → empty-netlist SPICE.
   Force path treated power as "wired" and advanced to board too early.
2. **AC4:** DRC clean (errors=0) but ERC `power_not_driven` ×18 (honest FAIL on
   combined errors==0 rule — not missing board).
3. **AC1:** soft zeros diagnosed (sketch/dim/ECAD-on-CAD/measure_between); manifold
   + measure_part PASS — geometry class OK, battery still honest FAIL.

### Fix 4 — Real wire + passive before board (post-primary, unit-green)

**Fingerprint:** force-inject board after orphan `ecad_add_power`; no pin-to-pin
wire; no Device:R/C VALUES → AC5 empty netlist.

**Fix:**
1. `_ecad_has_real_wire`: only `ecad_wire`/`ecad_label`, or power with
   `net_size>=1` (orphan power does not count).
2. Hard gate + `_ecad_chain_need`: require real wire before board; block premature
   `board_from_schematic` while nets empty.
3. Force `ecad_place_symbol(libid=Device:R, value=1k)` when only ICs placed
   (SPICE-capable passive for AC5).
4. Force order: passive R → power (optional) → **ecad_wire** → board → DRC.

**Regression:** `test_ecad_chain_need_helpers`,
`test_ecad_hard_gate_force_injects_wire_after_thrash` (wire before board).

**Tests after Fix 4:** `77 passed` (same suite).

Ready for MegaRun14 relaunch when harness chooses; Fixer14 will live-watch.

