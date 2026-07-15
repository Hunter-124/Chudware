# MegaRun15 Findings

## Run summary

- **Primary graded run ID:** `mega-15-fc-rocket-20260715-a3` (attempt-3 local-only)
- **Also archived:** a1 xai quota, a2 openrouter daily cap
- **Baseline commit:** `17b05635` (repo HEAD matches)
- **Fixer peer:** Fixer15=`CloseLynx` (battery adapters + honest SKIP + pin-aware wire/power force-inject + ERC-after-DRC; **128 tests**)
- **Harness peer:** MegaRun15=`PrecisePanda`
- **Engine:** hostd `night2-3054-gcb65bfd9-dirty` — running git_rev matches disk binary
- **Evidence:** events.jsonl, ledger.json, mission.json, status.json, preflight JSONs, battery_result.json, criterion_AC*.json, grade_summary.json, fixes.md, progress.md, attempt-1/2 archives

### Pre-flight: PASS (all attempts)

1. Daemon **running == disk hostd**; baseline `17b05635`
2. **spawn_probe PASS** — cap-sep + Fix8 + **Fix-wire** gate wiring
3. **auto_pick_smoke PASS**
4. Fixer15 **fixes bound** before primary
5. No other xAI consumer at launch

---

## Attempt matrix

| Attempt | Frontier | Wall | Terminal | Live AC PASS | V | W | X | Notes |
|---|---|---|---|---|---|---|---|---|
| **a1** | xai-sub/`grok-4.5` | 22.3s | deliver_partial | 0 | n/a | n/a | n/a | Both SuperGrok accounts `personal-team-blocked:spending-limit`. Archive: `attempt-1-xai-quota-blocked/` |
| **a2** | openrouter `nvidia/nemotron-3-ultra-550b-a55b:free` | 240.4s | verify_failed AC1,2,3,4,5,7,9 | **1** (AC6) | **PASS** | **FAIL** | **PASS** | OpenRouter **free-models-per-day** 429 on BUILD; force_inject never reached. Archive: `attempt-2-openrouter-daily-cap/` |
| **a3** | offline `local-24k-gemma3-qat` (llama-server :1234) | **73.7s** | verify_failed AC1,2,4,5 | **5** (AC3,6,7,8,9) | **PASS** | **UNTESTABLE** | **PASS** | BUILD DL3: `ecad_new_schematic`×1 then **context 4096 overflow** (n_ctx_slot); no place thrash → no force_inject |

**grok-4.5 revalidation deferred:** xAI quota exhausted (both accounts). Rerun on quota reset with model id **grok-4.5** EXACT and account `17d17fb1`.

---

## Axis scoreboard (honest — a3 primary)

| Axis | Verdict | Evidence |
|---|---|---|
| **V** battery invocation bugs fixed | **PASS** | No signature / missing-positional-arg zeros across a2+a3. Adapters return empty-doc / no-sketch / no-board messages. |
| **W** ecad_wire + power; ERC clean; AC4 | **UNTESTABLE this cycle** | force_inject_n=0; wire=0; power=0. a3 reached schematic open only; never placement thrash (dispatcher inject gate). Provider/ctx limited — **not** inject-logic residual. |
| **X** AC5 wired SPICE class | **PASS** | AC5 FAIL is missing schematic/doc class, not empty-netlist / no-simulatable-devices. |
| **U** AC1 diagnosis | **PASS** | criterion_AC1 per_check/findings; battery doc_id=`doc-cpp0000004a` on a3 |
| **S/P/O** board+DRC | **FAIL** | board_from_schematic=0, ecad_run_drc=0 |
| **Q** live ≥6 | **FAIL** | 5 live PASS (close) |
| **N** live ≥4 | **PASS** | 5 ≥ 4; AC3+AC6 held; AC7 held |
| **K** gradeable | **PASS** | 9/9 |
| **L** live ≥2 | **PASS** | 5 |
| **M** non-CAD | **PASS** | schematic×2 calls; AC6/antenna; fw path |
| **A** accepted ops | **PARTIAL/FAIL** | new_part_ok×2; add_features_ok=0 |
| **B** delegate | **FAIL** | tool-path 0 |
| **C** oneshot | **PASS** | oneshot_ok×12 |
| **D/F** terminal | **PASS** | needs_correction + clean verify_failed |
| **E/I** battery | **PASS** | battery_result + doc_id on a3 |

**Live AC PASS count (a3):** **5** (AC3, AC6, AC7, AC8, AC9). Target ≥8 **missed**.

---

## V) battery invocation: PASS (held a2+a3)

Formerly mega-14 signature zeros are gone:

| Check | a3 message class |
|---|---|
| check_sketch | adapter empty-doc / no sketches |
| measure_between | no features / empty |
| ecad_check_* impedance | no open board |
| dim_coverage / manifold / measure_part | empty-doc or real geometry fail (not TypeError) |

A check that can run still runs. Axis V contract met.

---

## W) wire + power force-inject: UNTESTABLE this cycle

**Contract for this cycle (Main):** grade W on force-inject fired + tools executed, not model quality.

**Observed:**
- a1: no BUILD tools (xai blocked)
- a2: BUILD assigned openrouter → 429 daily free cap before product tools
- a3: BUILD DL3 tools_slot includes wire/power/board/DRC; **`ecad_new_schematic` ok once**; next turns hit `request exceeds available context size (4096 tokens)` (llama-server n_ctx_slot=4096 with parallel=8). **No place thrash → hard-gate force_inject never fires.**

Fixer15 pin-aware inject + ERC-after-DRC remain **unit-bound (128p)** but **live-unexercised**.

---

## X) AC5 SPICE class: PASS

Not empty-netlist. Failures are no-schematic / wrong-doc class. When inject path runs post-place, expect Device:R + nets → V(3V3) or numeric fail.

---

## Provider quota story (full)

1. **SuperGrok (both accounts):** `personal-team-blocked:spending-limit` — blocked a1 and any grok-4.5 primary.
2. **OpenRouter free:** a2 worked until **free-models-per-day** (limit 50; needs +10 credits for 1000/day) mid-BUILD.
3. **Local gemma3-4b-qat:** server healthy on :1234 but **per-slot ctx 4096** too small for full ECAD BUILD prompts → provider_auth/budget exhaust after schematic open.

---

## TOP residual (next cycle)

1. Restore **SuperGrok credits** or OpenRouter credits; revalidate **grok-4.5** EXACT.
2. Local path: restart llama-server with **higher n_ctx_slot** (or parallel=1, ctx=32k) so BUILD can thrash place → force-inject W.
3. Live AC ≥8 needs product BUILD (wire/board/DRC/SPICE) while holding a3's AC3/6/7/8/9.
4. Phase cursor honesty residual continues.

---

## Fingerprints

| ID | Status |
|---|---|
| V battery invocation | **PASS** |
| W wire+power force-inject live | **UNTESTABLE** (quota/ctx; inject bound unit-green) |
| X AC5 class | **PASS** |
| Live PASS ≥8 | **FAIL** (5 on a3) |
| Fixer15 bind | **held** |
| grok-4.5 primary | **deferred** |

---

## Artifacts

```
reports/runs/mega-15/
  run_mega.py grade_run.py _preflight.py findings.md progress.md fixes.md
  spawn_probe.json auto_pick_smoke.json subscription_accounts.json
  daemon_preflight.json preflight_summary.json
  request.txt manifest.json mission_meta.json
  events.jsonl ledger.json mission.json status.json
  battery_result.json fw_artifact.json grade_summary.json
  criterion_AC1.json … criterion_AC9.json criterion_BATTERY.json
  attempt-1-xai-quota-blocked/
  attempt-2-openrouter-daily-cap/
```

## Attempt-3 (local-only, integrator-driven)

- `run_mega_a3.py`, FALLBACK_TIER=local-24k-gemma3-qat, offline, wall 74s.
- 0 tool calls: the local 4B frontier never reached ECAD placement, so the
  wire/power force-inject (dispatcher-side) had no trigger point.
- **W verdict: untestable this cycle** — requires any placement-capable model.
  First run after ANY quota reset (SuperGrok or OpenRouter daily) exercises it;
  the gate + inject unit tests (128 passed) cover the mechanics until then.

## Quota state at close (2026-07-15 ~05:30Z)

- xai SuperGrok `17d17fb1` (active) + `79066dcc` (parked): personal-team-blocked:spending-limit.
- OpenRouter free tier: free-models-per-day exhausted (429).
- Next cycle (mega-16): rerun this exact driver with a working frontier; expected
  to close AC4 (post-wire ERC) + AC5 (SPICE on wired netlist) -> 8/9.
