# mega-22 harness fixes (Fixer22 = GrossShark)

Baseline: `0f43d126`. Pair: MegaRun22=`UnusualBaboon` owns
`reports/runs/mega-22/**` + roadmap primary; Fixer22 owns
`services/agent/agent_turn.py`, `services/verify/battery.py`,
`services/agent/test_mega22_fixes.py` + this file.

Axes targeted (grounded in mega-21 a1 findings):

- **P22** AC5 undriven rail — `resolved_net=3V3` but `V(3V3)=0.0`,
  `device_count=1`, U1 skipped (no sim model). Injected source/load never
  both attached to the resolved rail. Force driven topology before simulate.
- **Q22** AC4 clearance residual — ERC=0 but DRC errors=31 on board
  `6f30f5cdb225`. Forced chain was `board_from_schematic → drc` without
  `ecad_optimize_traces`. Contract: board → optimize_traces → drc; one
  re-opt+re-DRC if still dirty; then honest FAIL with error class.
- **R22** AC1 measure_between soft zero — adapter issued `'top face'` /
  free-text queries the geom engine rejects (valid sides:
  top/bottom/left/right/front/back or ±x/±y/±z only). Derive from
  query_geometry/bbox; never guessed multi-word names.

Regression: M21/N21/A20/B20/C19/D19/Z1–Z4/Y/W/X/Fix8 + mega-11..21 hold.
Criteria not weakened (AC5 still needs finite in-band V on the real rail;
AC4 needs errors==0 or honest FAIL; AC1 needs real check scores).

---

## Live fingerprints (mega-21 a1)

| Observation | Detail |
|---|---|
| Terminal | `needs_correction` / `verify_failed: ['AC1','AC4','AC5']` |
| AC5 | `resolved_net=3V3`, `V(3V3)=0.0`, `device_count=1`, U1 no sim model |
| AC4 | `errors=31` clearance on board `6f30f5cdb225`; ERC=0; no optimize in chain |
| AC1 | `soft_scores_zero=['measure_between']` — bad side `'top face'` |
| Live AC | 6/9 (AC2,3,6,7,8,9) |

---

## Fix P22 — driven SPICE topology on resolved rail

**Root cause:** Net resolution landed on `3V3`, but the SPICE circuit only
had the skipped MCU (device_count=1). No voltage source + load resistor both
touched the resolved rail → V=0.0 honest FAIL.

**Fix (`agent_turn.py`):**

1. `_ecad_sim_topology` — read back sim payload / call history for
   `{rail, has_source, has_load, device_count, driven}`.
2. `_ecad_source_on_rail` / `_ecad_load_on_rail` — pin/wire attachment checks.
3. `_ecad_ensure_driven_rail` — before `ecad_simulate`, force-place
   `Device:V` + load `Device:R` if missing, wire both onto the resolved rail
   (and GND return), optional rail power flag.
4. Cascade invokes ensure-driven on every simulate step so the mega-21
   `device_count=1` fingerprint re-completes topology before re-sim.

**Fixture:** mega-21 AC5 evidence shape (device_count=1, V=0.0 on 3V3).

---

## Fix Q22 — board → optimize_traces → DRC

**Root cause:** Forced product chain jumped `board_from_schematic →
ecad_run_drc`, skipping polish. Clearance×31 residual never got a re-route
pass.

**Fix (`agent_turn.py`):**

1. `_ecad_chain_need` inserts `ecad_optimize_traces` after board and before
   first DRC (also when allow-list omits the polish tool but DRC is allowed
   and DRC has not yet run).
2. After optimize+dirty DRC: one re-optimize then re-DRC
   (`_ecad_optimize_retry_count` caps at 1 extra polish).
3. Forced args for optimize carry board `doc_id` (same as DRC).
4. Post-success cascade and force-cascade product lists include optimize.
5. Does **not** retroactively demand optimize after DRC already ran without
   it (preserves post-ERC → simulate scheduling).

**Fixture:** mega-21 ledger board→drc with zero optimize between.

---

## Fix R22 — valid measure_between endpoints

**Root cause:** `_adapt_check_kwargs(measure_between)` emitted plain words
`'top face'` / `'bottom face'`. Engine `_as_face_query` treats the whole
string as a `side` token → `bad 'side' 'top face'`.

**Fix (`battery.py`):**

1. `_normalize_side_query` — strip ` face`/` side` suffixes; accept only
   valid side words.
2. `_query_geometry_faces` / `_bbox_extents` / `_derive_measure_face_pair` —
   derive largest planar face pair or bbox extent sides.
3. Adapter rewrites bad caller sides and empty endpoints; never emits
   multi-word side phrases. Honest SKIP when no geometry exists.
4. Two-feature-id path (mega-19) preserved.

**Fixture:** mega-21 AC1 `soft_scores_zero=['measure_between']` + bad side.

---

## Tests / gate

```bash
python3 -m pytest \
  services/agent/test_mega22_fixes.py \
  services/agent/test_mega21_fixes.py \
  services/agent/test_mega20_fixes.py \
  services/agent/test_mega19_fixes.py \
  services/agent/test_mega18_fixes.py \
  services/agent/test_mega17_fixes.py \
  services/agent/test_mega16_fixes.py \
  services/agent/test_mega11_legs.py \
  services/agent/test_mega11_grading.py -q
```

**Counts (this bind):** **158 passed**, 0 failed.

---

## Bind one-liners (for MegaRun22)

1. **P22** — pre-simulate ensure V-source + load R both attach to the
   resolved rail; read back sim topology (device_count=1 triggers
   completion); finite in-band V still required.
2. **Q22** — forced chain is board → `ecad_optimize_traces` → DRC; one
   re-opt+re-DRC on dirty; else honest FAIL with clearance class.
3. **R22** — measure_between derives faces from query_geometry/bbox (valid
   side words only); never `'top face'`.
4. Frontier: xai-sub grok-4.5 EXACT account `06dc3359`; mid deepseek paid;
   ollama leaves.
5. No push (Main gates). Ready for primary after this bind.

No push. Fixes bound.
