# mega-27 harness fixes (Fixer27 = AshamedLungfish)

Baseline: `dce2e872` (post mega-26 a1 graded 8/9; FAIL AC4 only). Pair:
MegaRun27=`CertainCatshark` owns `reports/runs/mega-27/**` + roadmap primary;
Fixer27 owns the bind below. Frontier: xai-sub grok-4.5 account `06dc3359`.

Predecessor evidence: mega-26 a1 `findings.md` + `criterion_AC4.json` —
AC4 clearance×23 after B26 retry_n≥1 / autoroute(pitch=0.3). Root residual:
board_from_schematic AUTO-OUTLINE packs components so the router cannot
physically meet 0.2mm min clearance.

Axes: **do NOT weaken** AC4 (errors==0 or honest FAIL), A26/B26, Y25/Z25,
S23/P22/U24/V24/T23/Q22/M21/R22/G18/Y/Z1. No push (Main gates). Python preferred;
tiny outline-param plumb on MCP/board_flow accepted.

---

## Tool schemas inspected (no invented knobs)

| Surface | Outline / margin / grid knobs |
|---|---|
| MCP `board_from_schematic` (pre-fix) | **none** — only `sch_doc_id`, `optimize`, `doc_id` |
| MCP `ecad_autoplace` | **none** (seed only) |
| MCP `ecad_autoroute` | `pitch`, `diff_pairs`, `ripup_passes`, `doc_id` — **no clearance** |
| MCP `ecad_optimize_traces` | `targets`, `doc_id` — **no min_clearance** |
| `board_flow.board_from_schematic` opts | `place_opts`, `route_opts`, …; `_ensure_outline` hard-coded **margin=5** |
| `engine.ecad.place.place_board` | **`board_size: [w,h]`** when no outline (existed) |
| Engine ops | `pcb_set_outline` shapes/closed only |

**Cure direction taken:** plumb explicit outline room into board_from_schematic
(`board_size` / `outline_margin` / `outline_area_scale` / `outline` /
`force_outline`) and force ≥3× component bbox at agent force time.

---

## C27 — board has verifiably more routing room

### Root cause (mega-26 a1)

| Fact | Evidence |
|---|---|
| DRC residual | board `52e90b67d49f`, **23 clearance** @ ~0.08–0.13mm < 0.2mm |
| Retry | `clearance_retry_n≥1`, autoroute pitch=0.3 fired (B26 PASS) |
| Layout | AUTO-OUTLINE margin=5 around packed place — no room for 0.2mm tracks |
| Force args | `board_from_schematic` carried only sch doc_id — **no board_size** |

### Fixes landed

1. **`board_flow._ensure_outline`**: accepts `margin`, `board_size`,
   `outline` polygon, `area_scale`, `force`; records
   `outline_area_mm2` / `component_bbox_area_mm2` / `outline_size_mm` on report.

2. **`board_from_schematic` opts**: top-level `board_size` / `outline_margin` /
   `outline_area_scale` / `outline` / `force_outline`; pre-seeds outline before
   place so placer bbox is the generous room; returns outline metrics.

3. **MCP `board_from_schematic`**: plumbs the new knobs into board_flow; payload
   exposes `outline_area_mm2` + `component_bbox_area_mm2` for C27 grade.

4. **`agent_turn._ecad_outline_sizing`**: from place count estimate component
   bbox; size board so **outline_area ≥ 3× component_bbox_area** (floor 80×60);
   `outline_margin ≥ 15` (engine-margin equivalent pass path).

5. **`_ecad_forced_tool_args(board_from_schematic)`**: always attaches
   `board_size`, `outline_margin`, area diagnostics, `min_clearance_mm=0.2` on
   the call record (diagnostics stripped before MCP dispatch).

---

## D27 — live DRC errors==0 at 0.2mm (or honest reduced FAIL)

### Root cause (mega-26 residual)

B26 retry only re-routed at pitch=0.3 on the **same dense outline** — cannot
close clearance. Need outline-grow before re-route.

### Fixes landed

1. **Clearance retry ladder** (bounded once, `retry_n < 1` cap held):
   - **outline-grow ×1.5** via forced `board_from_schematic` (`force_outline`,
     larger `board_size`, `outline_grow≥1.5` on call record)
   - **`ecad_autoroute(pitch=0.3)`**
   - re-opt + re-DRC
   - residual → **honest FAIL** (`errors==0` pass rule unchanged; reduced
     count preferred)

2. Dispatch strip allows MCP outline knobs through; keeps area diagnostics on
   call records only.

---

## Files

| File | Change |
|---|---|
| `services/mcp/board_flow.py` | outline knobs + metrics on `_ensure_outline` / board_from_schematic |
| `services/mcp/ecad_tools.py` | MCP board_from_schematic params + payload metrics |
| `services/agent/agent_turn.py` | `_ecad_outline_sizing`, forced board args, grow retry ladder |
| `services/agent/test_mega27_fixes.py` | C27/D27 fixtures + mega-26 ledger replay |
| `reports/runs/mega-27/fixes.md` | this bind |
| `reports/runs/mega-27/offline_proof.md` | standing gate **FINAL_OFFLINE_OK** |

---

## Regression tests

`services/agent/test_mega27_fixes.py`:

- mega-26 AC4 fingerprint: clearance residual ≥20, FAIL
- mega-26 ledger: old board force lacked board_size
- C27: outline sizing ratio ≥3 for n=1..24; margin≥15
- C27: forced board args carry board_size + areas + ratio≥3
- C27: mega-26 ledger replay gets outline knobs
- D27: dirty DRC → outline-grow + autoroute(pitch≥0.3) + re-DRC; retry_n≥1
- D27: honest FAIL when still dirty (errors>0, no infinite loop)
- D27: grow=1.5 expands outline_area vs grow=1.0
- board_flow outline knobs accepted engine-side
- Y25/S23/B26 hold: sim→sch, drc→board, autoroute pitch≥0.3

---

## Gate

```bash
python3 -m pytest \
  services/agent/test_mega27_fixes.py \
  services/agent/test_mega26_fixes.py \
  services/agent/test_mega25_fixes.py \
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

**Counts: 237 passed**, 1 skipped, 0 failed (mega11+mega16..27+autosave).

Standing gate (agent_turn edits): `reports/runs/mega-27/offline_proof.md` →
**FINAL_OFFLINE_OK** (24c force-first contracts, eot board→opt→drc→sim on sch
with outline knobs, C27 ratio≥3, D27 grow ladder, A26/B26 markers).

---

## Bind one-liners (for MegaRun27)

1. **C27**: force-time `board_from_schematic` now passes `board_size` so
   outline area ≥ **3×** estimated component bbox (margin≥15); areas recorded
   on call records + tool payload.
2. **D27**: clearance retry = **outline-grow ×1.5** + autoroute(pitch=0.3) +
   re-opt + re-DRC once; residual is honest FAIL (pass rule still errors==0).
3. **Schemas**: MCP previously had no outline knob — plumbed through
   board_flow; place `board_size` and autoroute `pitch` already existed.
4. **Y25/Z25/A26/B26 preserved**: sim still schematic; DRC still board;
   clearance intent 0.2mm; B26 retry_n path still fires.
5. Daemon: Python-only MCP/agent — **no hostd rebuild**, all-clear.
6. No push (Main gates). Ready for primary after this bind.

No push. Fixes bound.
