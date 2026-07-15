# MegaRun21 Findings

## Run summary

- **Primary graded run ID:** `mega-21-fc-rocket-20260715` (**a1**)
- **Baseline commit:** `175c4b81`
- **Fixer peer:** Fixer21=`TroubledSlug` (M21 deliver-gate mech force + N21 AC5 net resolve; 142p)
- **Harness peer:** MegaRun21=`IndividualThrush`
- **Daemon:** hostd `night2-3075-gb0e66f3d-dirty` matches disk (FINE; Python-only baseline)
- **Frontier:** `xai-sub:grok-4.5` EXACT on `06dc3359` (xai-sub×11 live)
- **Mid-fallback:** openrouter paid deepseek enabled; **Leaves:** ollama×13
- **Spend Δ:** `$0.0` (guard $8 OK; frontier xai-sub)
- **Wall a1:** **433.95s**
- **Terminal a1:** `needs_correction` / `verify_failed: ['AC1', 'AC4', 'AC5']`

### Pre-flight: PASS

Account ACTIVE, hard→grok-4.5, mid openrouter deepseek enabled, leaf→ollama, hostd match, no other xai consumer, Fixer21 fixes bound (142p).

---

## Axis scoreboard (a1 primary)

| Axis | Verdict | Evidence |
|---|---|---|
| **M21** | **PASS** | new_part+add_features live (airframe doc-cpp00000001 vol≈687k mm³); AC2/AC3 PASS; model path (force not required) |
| **N21** | **PASS** | AC5 evidence `resolved_net=3V3` / `resolved_net_source=preferred_exact`; finite `V(3V3)=0.0` (honest FAIL vs band) |
| **A20** | **PARTIAL** | force wire U1↔R1; ERC power_not_driven=0; AC4 FAIL on DRC clearance×31 |
| **B20** | **PARTIAL** | board_from_schematic→`6f30f5cdb225`; DRC on board doc; errors=31 → S PARTIAL |
| **C19** | **PASS** | sim source path + AC1 zero-invocation defects |
| **D19** | **PASS** | unit/structural |
| **Z1** | **PASS** | 9/9 criteria |
| **Z2** | **PASS** | power then wire live |
| **Z2r** | **PARTIAL** | force power×11 + wire; AC4 not clean |
| **Z3** | **PASS** | simulate executed; not empty-netlist |
| **Z4** | **PASS** | AC5 FAIL with numeric V(3V3)=0.0 |
| **G18** | **PARTIAL** | live 6 < 7; xai-sub live |
| **Y** | **PASS** | mid deepseek enabled; leaf ollama |
| **W** | **PASS** | wire+power tools; ERC clean |
| **X** | **PASS** | honest FAIL not empty-netlist |
| **S/P** | **PARTIAL** | board+DRC executed; errors=31 |
| **V** | **PASS** | no sig errors |
| **Fix8** | **PASS** | gate live |
| **A** | **PASS** | new_part+add_features accepted |

**Live AC PASS:** **6**/9 — AC2, AC3, AC6, AC7, AC8, AC9. FAIL: AC1, AC4, AC5.

- AC1: `soft_scores_zero=['measure_between']` (geom query bad side `'top face'`) — manifold/measure_part OK
- AC2/AC3: **PASS** (payload volume + interference empty) — mech starvation residual closed
- AC4: `errors=31` DRC clearance on board `6f30f5cdb225`; ERC errors=0
- AC5: `v(3v3)=0.0 vs [3.135,3.465]`; `resolved_net=3V3`; device_count=1; U1 no sim model skipped

---

## M21/N21 vs mega-20 residual

mega-20 a1: deliver_partial with new_part=0/add_features=0; AC5 V(3V3) null on VIN path.

mega-21 a1: **M21** model (or gate-ready) airframe product → AC2/AC3 PASS. **N21** probe resolves `3V3` and records net; finite V extracted (0.0) so null-name sink closed — AC5 still FAIL on voltage band (criteria not weakened).

---

## TOP residual (next)

1. AC5 in-band V: inject/drive real source on resolved rail so V ∉ {0,null} and ∈ [3.135,3.465] (or honest non-zero fail with source topology).
2. AC4 DRC clearance×31: force/optimize traces or pad-aware layout before grade; keep ERC=0.
3. AC1 measure_between side vocab (`top` not `top face`) so battery soft zero clears.
4. Hold M21/N21/C19/D19/Z1/Z3/Z4/Y/W/X/Fix8/A; restore A20/B20/S to PASS via AC4 clean.

---

## Artifacts

```
reports/runs/mega-21/
  run_mega.py grade_run.py _preflight.py wait_and_launch.sh fixes.md findings.md
  preflight_*, spawn_probe, auto_pick_smoke, daemon_preflight, no_other_xai_consumer
  events.jsonl ledger.json mission.json status.json primary.log
  criterion_AC1..AC9 grade_summary run_summary status_report status_yield
```
