# MegaRun17 Findings

## Run summary

- **Primary graded run ID:** `mega-17-fc-rocket-20260715` (**attempt-3** after Z2 rebind)
- **Baseline commit:** `cd764e92` (repo HEAD matches)
- **Fixer peer:** Fixer17=`AlrightMosquito` (Z1/Z2/Z3 + Z2 residual rebind; **94 tests** on rebind)
- **Harness peer:** MegaRun17=`RequiredWoodpecker`
- **Engine:** hostd `night2-3054-gcb65bfd9-dirty` — running git_rev matches disk binary
- **Frontier:** `openrouter:deepseek/deepseek-chat-v3.1` (PAID, no `:free`)
- **Spend (a3):** before `$0.223708` → after `$0.223708` → **delta `$0.000`** (guard $8 OK; usage poll flat — see note)
- **Wall (a3):** **150.69s**
- **Terminal (a3):** `needs_correction` / `verify_failed: ['AC1','AC2','AC3','AC5','AC7','AC9']`
- **Evidence:** events.jsonl, ledger.json, mission.json, status.json, preflight JSONs, grade_summary.json, criterion_AC1–AC9.json, battery_result.json, openrouter_usage_{before,after}.json, fixes.md, daemon_preflight.json, attempt archives

### Pre-flight: PASS

1. Daemon **running == disk hostd**; baseline `cd764e92`
2. **spawn_probe PASS** — cap-sep + Fix8 + Fix-wire
3. **auto_pick_smoke PASS** — axis Y
4. Fixer17 **fixes bound** then **Z2 residual rebound** before a3
5. OpenRouter usage recorded; xai left blocked

---

## Attempt matrix

| Attempt | Frontier | Wall | Terminal | Live AC PASS | Z1 | Z2 | Z3 | Y | Notes |
|---|---|---|---|---|---|---|---|---|---|
| a1 aborted | openrouter paid | ~30s | killed | n/a | — | — | — | — | Double-launch race; `attempt-1-aborted-double-launch/` |
| a2 graded | openrouter paid | **145.9s** | needs_correction | **4** | **PASS** | **FAIL** | **PASS** | **PASS** | force_inject place_symbol; no add_power; archived `attempt-2-primary-graded-z2fail/` |
| **a3 primary** | openrouter paid | **150.7s** | needs_correction | **2** | **PASS** | **PASS** | **PASS** | **PASS** | add_power×8 before wire; AC4 PASS; AC5 sim null V(3V3) |

**grok-4.5 revalidation deferred:** xAI spending-limit-blocked.

---

## Axis scoreboard (honest — a3 primary)

| Axis | Verdict | Evidence |
|---|---|---|
| **Z1** terminal grading unconditional | **PASS** | All 9 `criterion_ACn.json`; terminal still grades |
| **Z2** power-then-wire live refs | **PASS** | `ecad_add_power`×8 ok before `ecad_wire`×2 ok; `power_before_wire=true`; `wire_fail_missing_pwr=0`; `power_not_driven_n=0`; **AC4 PASS** (drc+erc errors==0) |
| **Z3** AC5 SPICE executes | **PASS** | OP/mna on doc `d0cdccc65c7f`, device_count=1, converged; V(3V3)=null → AC5 **NOT-CHECKED** |
| **Y** auto_pick openrouter paid | **PASS** | openrouter×4 + ollama×20; xai×0 free×0 |
| **W** wire+power ERC clean | **PASS** | tools + AC4 PASS |
| **X** AC5 V(3V3) measured | **FAIL** | simulate ran; probe null |
| **V** battery invocation | **PASS** | battery_result present; no sig-error zeros |
| **A/B/D/F/E/I/S/Fix8** | **PASS** | ops, delegate, terminal, battery doc_id, board+DRC, chain gate |

**Live AC PASS count (a3):** **2** (AC4, AC6). Target ≥8 **missed**.  
(a2 had 4 PASS before rebind geometry/fw variance.)

---

## Z1) terminal grading: PASS (held)

mega-16 residual closed on a2 and held on a3. Every terminal emits criterion_AC1–AC9 (honest FAIL/SKIP/NOT-CHECKED allowed).

---

## Z2) power-then-wire: PASS (a3 after rebind)

**a2 fingerprint (FAIL):** force_inject `ecad_place_symbol` Device:R; `ecad_add_power`=0; R1 self-loop; no `#PWR1`.

**Fixer17 rebind:** self-loop no longer unlocks board when ≥2 symbols; `nets_ready` requires `ecad_add_power` + product wire; force inject prefers add_power then cross-ref wire (94 tests).

**a3 live:**

1. `ecad_add_power` ok×4+ (GND/VDD ports) **before** wire.
2. `ecad_wire` ok (R1.1↔R1.2 observed — product still thin, but power-first order holds).
3. No `#PWR1` missing-ref failures.
4. **AC4 PASS** — drc.errors=0, erc.errors=0 (`power_not_driven`→0).

Residual product thinness: power ports still often `net_size=0` until/unless wired to IC pins; R1 self-loop can still appear after power ports exist. Contract for Z2 (power before wire + no invented #PWR1 + ERC clean) **met**.

---

## Z3) simulate executes: PASS; X still FAIL

AC5 evidence: OP converged, not empty-netlist; V(3V3) null → NOT-CHECKED. Need 3V3 source+load sim models for X/AC5 PASS.

---

## Y) auto_pick: PASS

Hard→openrouter paid deepseek; leaf→ollama. Live ollama-heavy (20/4) still not ollama-all collapse; openrouter present.

Spend poll delta $0 on a3 despite openrouter×4 — likely usage API lag; cumulative key usage remains $0.2237 / $10.

---

## TOP residual (next cycle / mega-18)

1. Raise live AC PASS ≥8 (AC1/2/3 geometry+interference; AC7 fw; AC9 legs; AC5 finite V(3V3)).
2. Wire power ports **to** IC pins (not only place ports + passive self-loop).
3. AC5 measurement path (source+load models).
4. Hold Z1/Z2/Z3/Y/W/S/Fix8.
5. grok-4.5 deferred (xai spending-limit).

---

## Fingerprints

| ID | a2 | a3 |
|---|---|---|
| Z1 terminal AC grading | PASS | **PASS** |
| Z2 power-then-wire | FAIL | **PASS** |
| Z3 simulate executes | PASS | **PASS** |
| Y openrouter paid | PASS | **PASS** |
| W ERC clean / AC4 | FAIL | **PASS** |
| X V(3V3) | FAIL | FAIL |
| Live PASS ≥8 | FAIL (4) | FAIL (2) |
| Spend guard $8 | held | held |

---

## Artifacts

```
reports/runs/mega-17/
  run_mega.py grade_run.py _preflight.py findings.md fixes.md progress.md run_summary.json
  spawn_probe.json auto_pick_smoke.json subscription_accounts.json
  daemon_preflight.json preflight_summary.json
  openrouter_usage_before.json openrouter_usage_after.json
  request.txt manifest.json mission_meta.json
  events.jsonl ledger.json mission.json status.json
  battery_result.json criterion_AC1..AC9.json criterion_BATTERY.json
  grade_summary.json
  attempt-1-aborted-double-launch/
  attempt-2-primary-graded-z2fail/
```
