# mega-20 harness fixes (Fixer20 = LiberalWarbler)

Baseline: `b0e66f3d`. Pair: MegaRun20=`HugeAlpaca` owns
`reports/runs/mega-20/**` + roadmap primary; Fixer20 owns
`services/agent/agent_turn.py`, `services/agent/test_mega20_fixes.py` + this file.

Axes targeted (grounded in mega-19 **a2** + **a3** ledgers):

- **A20** rail-correct power bonding / wire pairs live: ERC `power_not_driven`
  must clear.
  - **a2:** `ecad_add_power` returns no `#PWR`/`ref`; cascade stopped before
    pin bonds; orphans at (0,10).
  - **a3 (fresh):** force wire+board+DRC **did** run; DRC on board
    `5cc769570819` (B20 live OK). Residual exactly A20: forced wire
    `U1.3/GND ↔ R1.1` while bonded rail member was `U1.6` on 3V3 → ERC
    `power_not_driven` on N$3; 3V3 left as single-pin floating.
- **B20** forced DRC carries board doc id — **confirmed live on a3**
  (`doc_id=5cc769570819`). Still hardened nested/truncated harvest +
  pre-dispatch override so model path cannot pass sch id.

Regression: mega-11 legs/grading + mega-16..19 fixes hold. Criteria not weakened.

---

## Live fingerprints

### a2 (`attempt-2-primary-graded-wire-zero/ledger.json`)

| Observation | Detail |
|---|---|
| sch doc | `cd7516e36580` |
| add_power | orphans + forced @ (0,10); **no ref**; net_size=0 |
| wire / board | 0 |
| AC4 DRC | `doc 'cd7516e36580' is a sch, not a board` |

### a3 (root `reports/runs/mega-19/ledger.json`)

| Observation | Detail |
|---|---|
| sch / board | `596497ba8ad5` / `5cc769570819` |
| add_power 3V3 | forced @ (30.16,40) → `net_members=["U1.6"]` net_size=1 |
| forced wire | **U1.3 ↔ R1.1** (wrong — unbonded GND power_in) |
| forced DRC | `doc_id=5cc769570819` board ✓ |
| ERC | `power_not_driven` on N$3; 3V3 single-pin warning |

Engine contract: `ecad_add_power` places geometric power ports only — **never** a
wireable `#PWR` symbol ref. Bonding = co-located pin xy; product wire must join
other pins onto those bonded members.

---

## Fix A20 — pin→rail mapping (a2 rebond + a3 wire members)

**Root cause (two layers):**

1. **a2:** any successful `ecad_add_power` unlocked wire; orphan ports left
   power_in undriven.
2. **a3:** 3V3 bonded `U1.6` but `_ecad_derive_wire_args` preferred IC power_in
   pin 3 (GND) ↔ R1 → undriven power net.

**Fix (`agent_turn.py`):**

1. `_ecad_unbonded_power_pins` + chain step **2b** — rebond remaining power_in
   at pin xy before wire.
2. `_ecad_forced_tool_args(ecad_add_power)` — prefer unbonded power pin xy.
3. `_ecad_rail_member_pins` — harvest `net_members` (`U1.6`) from bonded
   add_power.
4. `_ecad_derive_wire_args` — wire passive ↔ rail member when members exist;
   else IC↔passive uses **signal** pins (never unbonded power_in). Still honors
   engine `#PWR` refs when present.
5. `_ECAD_FORCE_CASCADE_MAX` 8→16.

**Fixtures:** a2 rebond coverage; a3 U1.6↔R1 (not U1.3↔R1); a3 ledger replay.

---

## Fix B20 — board doc_id on forced/model DRC

**Live a3:** already board-correct. Hardening retained:

1. `_ecad_board_doc_id` — nested envelopes + truncated JSON; never return known
   sch ids as board.
2. Pre-dispatch: `ecad_run_drc` / autoroute / to_3d / pdn / export force
   `doc_id = board` when known.

---

## Tests / gate

```bash
python3 -m pytest \
  services/agent/test_mega20_fixes.py \
  services/agent/test_mega19_fixes.py \
  services/agent/test_mega18_fixes.py \
  services/agent/test_mega17_fixes.py \
  services/agent/test_mega16_fixes.py \
  services/agent/test_mega11_legs.py \
  services/agent/test_mega11_grading.py -q
```

**Counts (this bind):** **127 passed**, 0 failed.

---

## Bind one-liners (for MegaRun20)

1. **A20** — rebond power_in at pin xy; forced wire joins rail `net_members`
   (e.g. U1.6↔R1), never unbonded power_in↔R (a3 U1.3 residual).
2. **B20** — forced/model DRC always board doc_id (live a3 already OK; hardened).
3. Frontier: xai-sub grok-4.5 EXACT account `06dc3359`; mid deepseek paid; ollama leaves.
4. No push (Main gates). Ready for primary after this bind.

No push. Fixes bound.
