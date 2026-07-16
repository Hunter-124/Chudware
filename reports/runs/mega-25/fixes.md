# mega-25 harness fixes (Fixer25 = ExcellentPiranha)

Baseline: `871e0fb9` (post mega-24 a3 graded 7/9). Pair: MegaRun25=`PresentClam`
owns `reports/runs/mega-25/**` + roadmap primary; Fixer25 owns the bind below.

Predecessor evidence: mega-24 a3 `findings.md` + `criterion_AC4.json` /
`criterion_AC5.json` — AC5 NEVER passed (simulate on PCB doc); AC4 oscillates
(clearance×6 at 0.1296 < 0.2mm).

Axes: **do NOT weaken** U24/V24/T23/Q22/M21/R22/G18/Y/Z1 or S23 (AC5 in-band V,
AC1 real measure scores). No push (Main gates). Python only.

---

## Y25 — AC5 simulate → schematic doc (root cause + fix)

### Root cause (mega-24 a3 ledger)

| Fact | Evidence |
|---|---|
| Schematic | `ecad_new_schematic` → `a6e440d0e2fc` |
| Board | `board_from_schematic` → `767f0f0fa678` (+ `sch_doc_id=a6e440d0e2fc`) |
| AC5 FAIL | `doc '767f0f0fa678' is a pcb, not a schematic` |
| Who called | **reviewer** `run_acceptance_checks` (not BUILD forced sim — BUILD never
  reached simulate; VERIFY re-ran SPICE with wrong doc) |

`reviewer_exec._resolve_domain_doc_id("ecad_simulate")` walked the ledger reverse
and hit force-inject / optimize / DRC events whose only `doc_id` was the **board**.
The generic `ecad_*` fallback then promoted that board id to "schematic":

```
tn='ecad_optimize_traces' is_board=False is_sch=False → sch_id = board_id
RETURN sch 767f0f0fa678   # BUG
```

Forced-tool args already preferred schematic for simulate (`_ecad_forced_tool_args`),
but the **reviewer path** (and model-path when board was current) did not.

### Fixes landed

1. **`reviewer_exec._resolve_domain_doc_id`** (Y25):
   - Explicit `prefer_sch` for `ecad_simulate` / ERC / place / wire / power / label.
   - Schematic-preferring tools **never fall back to board**.
   - Separate `known_board` / `known_sch` sets; never promote board tool args to sch.
   - `board_from_schematic` **args.doc_id is schematic input**; board only from
     result `board_doc_id` / result `doc_id`.
   - Live mega-24 ledger fixture: simulate→`a6e440d0e2fc`, DRC→`767f0f0fa678`.

2. **`agent_turn` model-path + forced-dispatch rewrite** (Y25 mirror of B20):
   - Board tools (`drc`/`autoroute`/`optimize_traces`/…) force `doc_id=board`.
   - SPICE/ERC/capture tools force `doc_id=schematic`.
   - Forced simulate still strips `resolved_net*` before MCP (mega-22 a2 hold).

---

## Z25 — AC4 clearance-aware board chain

### Root cause (mega-24 a3)

- Board `767f0f0fa678` DRC: **6 clearance errors**, 0.1296mm < required **0.2mm**.
- Forced chain ran `ecad_optimize_traces` once (polish only — arcs/teardrops).
- Engine `optimize_traces` opts: corner/teardrop/impedance only — **no
  min_clearance / spacing / seed param** (verified `engine_oracle/ecad/trace_opt.py`
  + MCP `ecad_optimize_traces(targets, doc_id)`).
- Teardrops can shrink edge-to-edge spacing further; re-polish alone does not
  widen tracks.

### Fixes landed

1. **Clearance detection** `_ecad_last_drc_has_clearance(calls)` — mega-24 AC4
   shape (`violations[].rule=clearance`, verdict text, by_rule).

2. **Forced optimize/autoroute args** carry intent:
   - `min_clearance_mm=0.2` (call-record metadata; stripped before MCP).
   - On clearance-dirty DRC: `clearance_retry=True`, `skip_teardrops=True`.
   - Clearance re-route: `ecad_autoroute(pitch=0.3)` (coarser grid / more space).

3. **Bounded cascade retry** (one shot):
   - When post-opt DRC is dirty and `retry_count < 1`:
     - if clearance-class → force `ecad_autoroute(pitch=0.3)` once, then
       re-`ecad_optimize_traces`, then re-DRC;
     - else re-optimize once (Q22 hold).
   - After one retry, chain advances; residual errors → honest FAIL
     (`errors==0` pass rule unchanged — never weakened).

4. **Dispatch strips** unknown kwargs (`min_clearance_mm`, `clearance_retry`,
   `skip_teardrops`) so MCP signatures stay clean.

---

## Files

| File | Change |
|---|---|
| `services/agent/reviewer_exec.py` | Y25 domain doc_id: simulate→sch, never board fallback |
| `services/agent/agent_turn.py` | Y25 doc rewrite; Z25 clearance detect + re-route/re-opt |
| `services/agent/test_mega25_fixes.py` | Live mega-24 fixtures + Y25/Z25 contracts |
| `reports/runs/mega-25/fixes.md` | this bind |

---

## Regression tests

`services/agent/test_mega25_fixes.py`:

- mega-24 AC5 fingerprint: simulate on PCB doc / "not a schematic"
- mega-24 AC4 fingerprint: clearance×6, 0.1296 < 0.2mm, board 767f…
- Y25: live ledger resolve simulate→sch, DRC→board (B20 hold)
- Y25: forced simulate args + dispatch rewrite board→sch
- Z25: clearance detection from AC4 shape
- Z25: optimize/autoroute args min_clearance ≥0.2; cascade one re-route
- Z25: dispatch strips meta, keeps board doc
- S23/T23 contracts still hold

---

## Gate

```bash
python3 -m pytest \
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

**Counts: 216 passed**, 0 failed (mega11+mega16..25+autosave).

---

## Bind one-liners (for MegaRun25)

1. **Y25**: AC5 was reviewer doc routing — board id promoted to "schematic";
   now simulate/ERC always resolve schematic `a6e…`-class, never board.
2. **Z25**: clearance residual → one autoroute(pitch=0.3)+re-opt, then honest
   DRC FAIL if still dirty; min_clearance intent ≥0.2mm on call records.
3. **B20/Q22/S23/T23 preserved**: DRC still board; in-band V + measure scores.
4. Engine has no optimize min_clearance param — bounded re-route is the fix.
5. Daemon: Python-only — **no hostd rebuild**, all-clear.
6. No push (Main gates). Ready for primary after this bind.

No push. Fixes bound.

---

## Thrash rebind (a1 20260715T234708Z)

Archive: `reports/runs/mega-25/attempt-thrash-20260715T234708Z/`
Kill: `dag.node.failed count=4 > 3 (tools=139)`.

### Failures
1. **INTAKE / DELIVER DL1** — leaf_oneshot_miss → hard-coded
   `mistralai/mistral-small:free` 404 (node_runner frontier fallback).
2. **BUILD DL3** — sch+place+orphan power; **0 force-inject**; budget exhaust
   on find_part thrash; no board/opt/drc/sim.
3. Planning 24c paths (force-first/junk) still green offline — **not** the Y25/Z25 collision.

### Rebind
1. `node_runner._resolve_provider` + `_frontier_fallback_provider`: deepseek mid
   then gemma/nemotron free — **never** mistral-small:free.
2. `agent_turn` end-of-turn: if ECAD chain incomplete on max_iters/no_code,
   `end_of_turn_ecad_force` cascade with budget headroom.
3. Offline proof: `reports/runs/mega-25/offline_proof_a2.md` → **FINAL_OFFLINE_OK**.

Y25/Z25 preserved. No push.
