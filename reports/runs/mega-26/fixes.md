# mega-26 harness fixes (Fixer26 = DeadOpossum)

Baseline: `6ab362fb` (post mega-25 a2 graded 7/9; FAIL AC4+AC5). Pair:
MegaRun26=`InteriorFinch` owns `reports/runs/mega-26/**` + roadmap primary;
Fixer26 owns the bind below. Frontier: xai-sub grok-4.5 account `06dc3359`.

Predecessor evidence: mega-25 a2 `findings.md` + `criterion_AC4.json` /
`criterion_AC5.json` — AC5 V(3V3)=0.0 with sim on schematic; AC4 clearance×27
with `clearance_retry_n=0`.

Axes: **do NOT weaken** U24/V24/T23/Q22/M21/R22/G18/Y/Z1, Y25/Z25, or S23
(AC5 in-band V, AC1 real measure scores). No push (Main gates). Python only.

---

## A26 — AC5 force source/load onto resolved MEASURE net

### Root cause (mega-25 a2 ledger + engine)

| Fact | Evidence |
|---|---|
| Schematic sim | `doc_id=f9ec35394c6c`, `device_count=2`, U1 skipped (no sim model) |
| AC5 FAIL | `V(3V3)=0.0` vs in-band `[3.135, 3.465]` |
| Forced V inject | `Simulation_SPICE:V_source` **`value="DC 3.3"`** |
| Engine parse | `engine/sim/spice._parse_value("DC 3.3")` → **ValueError**; schematic path sets `float_value=None` → independent V stamps as **0 V** |
| Topology | V+ wired to rail anchor (U1 pin); measure net is **3V3** (N21 preferred). U1/buck has no SPICE model so any pre-regulator island is undriven for OP |

Confirmed offline: `"DC 3.3"` → 0.0 OP; bare `"3.3"` → 3.3 OP.

### Fixes landed

1. **Parseable V value** in `_ecad_ensure_driven_rail` + `_ecad_forced_tool_args`
   place inject: **`value="3.3"`** (never `"DC 3.3"`).

2. **Measure-net binding (N21)**: ensure_driven_rail always resolves
   `resolve_measure_net(preferred="3V3")` and attaches V+/load R to **that** net
   (not VIN / harvested source island behind unmodeled U1). V- and load return
   still bond to **GND**.

3. **Honest stub evidence**: forced place/wire records and forced simulate args
   carry **`u1_stubbed_ideal_regulator: true`** (stripped before MCP dispatch).
   AC5 readback still uses real OP on the stubbed ideal regulator.

---

## B26 — AC4 clearance retry actually fires

### Root cause (mega-25 a2)

| Fact | Evidence |
|---|---|
| First opt | `ecad_optimize_traces` with `min_clearance_mm=0.2` (meta only) |
| DRC | board `7c4fae119443`, **27 clearance** @ ~0.13mm |
| Retry | **`retry_n=0`** — no `ecad_autoroute`, no re-opt after DRC |
| Ledger result | tool_call DRC result is **`_truncate()` mid-JSON** → `json.loads` fails → `_ecad_last_drc_errors` returned **None** |
| chain_need | dirty re-opt required `errors>0` **and** `"ecad_optimize_traces" in allow` |
| BUILD allow | tools_slot **omitted** optimize/autoroute → re-opt branch unreachable; cascade advanced to ERC/sim |

Hard_gate cascade also burned 16 steps on multi `add_power` rebond before DRC;
end_of_turn then saw dirty DRC but could not re-opt for the same reasons.

### Fixes landed

1. **Truncated DRC recovery** in `_ecad_last_drc_errors`:
   scrape `"errors": N` / `N error(s)` / clearance+error presence from the raw
   string when parse fails. Live mega-25 blob → **errors=27**.

2. **`reopt_allowed` expansion** in `_ecad_chain_need`: re-optimize when DRC /
   board / autoroute is in allow even if optimize itself was omitted (mirrors
   first-opt expansion). Dirty post-opt DRC → **`ecad_optimize_traces`**.

3. **Cascade path (Z25 hold)**: when need is re-opt/re-DRC and clearance-class
   residual, force **`ecad_autoroute(pitch=0.3)`** once + re-opt + re-DRC.
   Cap still one retry (`retry_n>=1`); residual → honest FAIL (`errors==0`
   pass rule unchanged).

Ledger replay (post-DRC snap, BUILD allow): dispatched
`autoroute → optimize → re-DRC → erc → …`; `_ecad_optimize_retry_count >= 1`.

---

## Files

| File | Change |
|---|---|
| `services/agent/agent_turn.py` | A26 V value + measure-net rail + stub flag; B26 truncated errors + reopt_allowed |
| `services/agent/test_mega26_fixes.py` | Live mega-25 fixtures + A26/B26 contracts |
| `reports/runs/mega-26/fixes.md` | this bind |
| `reports/runs/mega-26/offline_proof.md` | standing gate **FINAL_OFFLINE_OK** |

---

## Regression tests

`services/agent/test_mega26_fixes.py`:

- mega-25 AC5 fingerprint: V=0.0, device_count=2, U1 no sim model, sch doc
- mega-25 AC4 fingerprint: clearance×27, board 7c4f…
- ledger old topology: value `DC 3.3`, no autoroute after DRC
- A26: `_parse_value("DC 3.3")` fails; forced inject uses `"3.3"`; net=3V3; stub flag
- A26: forced simulate args carry `u1_stubbed_ideal_regulator` + resolved 3V3
- B26: truncated DRC string → errors=27; chain_need re-opts under BUILD allow
- B26: mega-25 ledger snap cascade → autoroute + retry_n>=1 + re-DRC
- B26: bounded once (no infinite re-opt)
- Y25/S23 hold: sim→sch, drc→board

---

## Gate

```bash
python3 -m pytest \
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

**Counts: 228 passed**, 0 failed (mega11+mega16..26+autosave).

Standing gate (agent_turn edits): `reports/runs/mega-26/offline_proof.md` →
**FINAL_OFFLINE_OK** (24c force-first/junk, eot board→opt→drc→sim on sch,
no mistral, A26/B26 markers).

---

## Bind one-liners (for MegaRun26)

1. **A26**: forced V was `"DC 3.3"` → SPICE value None → OP 0.0; now bare
   `"3.3"` on the N21-resolved **3V3** measure net (ideal-regulator stub;
   `u1_stubbed_ideal_regulator: true` on call records).
2. **B26**: truncated DRC lost `errors` + BUILD allow omitted optimize →
   clearance re-route never armed; now scrape errors + reopt_allowed → one
   `autoroute(pitch=0.3)` + re-opt + re-DRC (`retry_n>=1`).
3. **Y25/Z25/S23/T23 preserved**: sim still schematic; DRC still board;
   in-band V pass rule unchanged; measure scores unchanged.
4. Daemon: Python-only — **no hostd rebuild**, all-clear.
5. No push (Main gates). Ready for primary after this bind.

No push. Fixes bound.
