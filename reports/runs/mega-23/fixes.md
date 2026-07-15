# mega-23 harness fixes (Fixer23 = HappyWorm)

Baseline: `d21bcaec`. Pair: MegaRun23=`GoverningDolphin` owns
`reports/runs/mega-23/**` + roadmap primary; Fixer23 owns
`services/agent/agent_turn.py`, `services/verify/battery.py`,
`engine/sim/circuit.py`, `engine/sim/ngspice_backend.py`,
`services/agent/test_mega23_fixes.py` + this file.

Axes targeted (grounded in mega-22 a2 primary —
`reports/runs/mega-22/attempt-2-primary-graded-live7/`):

- **S23 / AC5** — SPICE circuit driven (`device_count=2`, source+load on rail)
  but `V(3V3)` extracts null. a2 evidence: singular matrix (V- shorted onto
  rail) + null probe. Net→vector resolution must be case/format-robust
  (`v(3v3)`/`V(3V3)`/aliases). Pass rule NOT weakened: still
  `V(3V3) in [3.135, 3.465]` on the real rail.
- **T23 / AC1** — measure_between issued a geometrically valid query path
  after R22 but still scored 0.0 (engine rejected feature id `f1` as side
  word). Derive bbox extreme pair (nonzero expected separation) AND score
  any successful finite measurement (0.0 distance with valid refs =
  honest coincidence finding, not silent zero).

Regression: all mega-22 green axes hold (Q22 DRC=0, M21, N21, Z*, Y, W, X…).
No push (Main gates). Python-only — **no hostd rebuild** (daemon all-clear).

---

## Live fingerprints (mega-22 a2)

| Observation | Detail |
|---|---|
| Terminal | `done` / `deliver_partial` wall 676s |
| Live AC | **7**/9 (FAIL AC1+AC5) |
| AC5 | `device_count=2`, `op.V(3V3)=null`, singular matrix; backend=mna |
| AC1 | `soft_scores_zero=['measure_between']` — `bad 'side' 'f1'` |
| Q22 | **PASS** board→optimize→drc errors=0 |

---

## Fix S23 — AC5 probe readback + GND return

**Root cause (two layers):**

1. **Topology residual:** a2 force-inject placed `Simulation_SPICE:V_source`
   but V- could still land on the rail net → singular MNA → null probes
   even with `device_count=2`.
2. **Readback residual:** probe eval used exact-case `node_index.get(name)`;
   ngspice vectors are lower-case (`v(3v3)`). Case/format miss → null even
   when the node has a finite solution.

**Fix:**

1. `engine/sim/circuit.py`
   - `_Sim` builds `_node_aliases` (lower→canonical).
   - `resolve_node()` — case/format tolerant (`3v3`, `+3V3`, `gnd`, …).
   - `_eval_probe` uses `resolve_node` for V(node) / differential / I(R).
2. `engine/sim/ngspice_backend.py`
   - `_lookup` expands candidates (`v(arg)`, bare, `+arg`) + casefold map.
3. `services/agent/agent_turn.py`
   - `_ecad_has_sim_source` / `_ecad_find_ref` / `_ecad_source_on_rail`
     recognize `Simulation_SPICE:V_source` / `v_source`.
   - `_ecad_ensure_driven_rail`: never early-return solely because
     `driven=True`; always force V- → GND return. When no GND member,
     inject `ecad_add_power(net=GND)` first, then wire V- and load return
     with explicit `net="GND"`.
   - Place steps still gated when topology already has source+load; GND
     return path always runs.

**Fixture:** mega-22 a2 AC5 evidence (`device_count=2`, `V(3V3)=null`,
singular log).

---

## Fix T23 — AC1 measure_between real score

**Root cause:**

1. `_derive_measure_face_pair` could emit bare face/feature ids (`f1`) from
   query_geometry — engine treats bare strings as side words →
   `bad 'side' 'f1'` → score 0.
2. `_extract_score` for scalar reward used only
   `match_pct/score/min_fos/…` then fell through to `ok` — but a failed
   geom query is `ok=false`. When a successful measure returns
   `{ok:true, distance:N}`, older paths could still soft-zero if distance
   was 0.0 (treated as falsey elsewhere) or if the check never adapted.

**Fix (`services/verify/battery.py`):**

1. `_derive_measure_face_pair` — **never** emit feature/face id strings;
   prefer valid side words from bbox extremes (largest span first →
   nonzero expected separation). Structured `{"of":"face",…}` dicts only
   as last structured form; prefer bbox pair over two anonymous picks.
2. `_valid_endpoint` accepts structured face-query dicts; still rejects
   bare `f1` / free-text.
3. `_extract_score(measure_between, scalar)`: if `ok` and `distance` is
   finite (including **0.0**), score **1.0**. Zero distance surfaces an
   honest coincidence finding — not a soft-zero FAIL.

**Fixture:** mega-22 a2 battery/AC1 (`measure_between=0.0`, bad side `f1`).

---

## Tests / gate

```bash
python3 -m pytest \
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

**Counts (this bind):** **182 passed**, 0 failed.

Also: `services/mcp/test_verify_battery.py::test_adapt_measure_between_runs_with_derived_queries`
updated to expect valid side words (not legacy `'top face'`).

---

## Bind one-liners (for MegaRun23)

1. **S23** — pre-sim ensure V+→rail and **V-→GND** (force GND power flag if
   missing); probe eval case/format robust. AC5 still needs finite in-band V
   on the real rail `[3.135, 3.465]`.
2. **T23** — measure_between derives bbox extreme sides (never `f1`);
   scorer awards 1.0 on any successful finite distance (0.0 = coincidence).
3. Frontier: xai-sub grok-4.5 EXACT account `06dc3359`; mid deepseek paid;
   ollama leaves.
4. **Daemon:** Python-only bind — **no hostd rebuild**, all-clear.
5. No push (Main gates). Ready for primary after this bind.

No push. Fixes bound.
