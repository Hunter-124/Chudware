# MegaRun14 Progress

- **Run ID:** `mega-14-fc-rocket-20260715`
- **Baseline:** `7a1e1e5d`
- **Driver:** `reports/runs/mega-14/run_mega.py`
- **Account:** `17d17fb1-247a-4800-ba97-e7b5150bc53a`
- **Peers:** Fixer14=`ExceptionalYak`, MegaRun14=`WholeCatfish`
- **Engine:** hostd `night2-3054-gcb65bfd9-dirty` (running git_rev matches disk binary)
- **Model id:** `grok-4.5` EXACT (`grok-4-5` = HTTP 400)
- **Axes:** S (board_from_schematic+DRC executed; upgrades P) + T (AC5 wired SPICE; upgrades R) + U (AC1 battery PASS or per-check diagnosis) + A–Q regression

## Status

- [x] Pairing confirmed (Main: Fixer14=ExceptionalYak, MegaRun14=WholeCatfish)
- [x] Daemon preflight: running == disk hostd (`night2-3054-gcb65bfd9-dirty`, pid 2980908)
- [x] Account `17d17fb1-…` + spawn_probe PASS (incl Fix8 gate wiring) + auto_pick_smoke PASS
- [x] No other xAI consumer live
- [x] Fixer14 **fixes bound** DM (Fix8 force-inject + AC1 diagnosis + rank floor 5; 77 passed)
- [x] Primary pid **3590131**, wall **797.46s**, finished
- [x] Grade S/T/U + A–Q; findings + criterion_ACn.json + roadmap append

## Scoreboard (primary)

- **S PASS** — board_from_schematic + ecad_run_drc executed; DRC errors=0 (force_inject after place thrash)
- **T FAIL** — AC5 empty-netlist / U* no sim models; ecad_wire=0 (only add_power)
- **U PASS** — AC1 FAIL with full per_check / zero_checks / brief_hints diagnosis
- **Q PASS** — 6 live PASS (AC2+AC3+AC6+AC7+AC8+AC9); target ≥7 missed
- **P/O PASS** (reg, upgraded)
- **R FAIL** (reg)
- **N PASS** (reg)
- **A–M regression** mostly PASS (B/G/J live residual)
- Terminal: `needs_correction` / `verify_failed: ['AC1', 'AC4', 'AC5']`

## Post-primary (Fixer14 Fix4 — not re-launched)

- ExceptionalYak bound Fix4 unit-green (77): orphan `ecad_add_power` no longer
  unlocks board; require real `ecad_wire`; force Device:R value=1k when IC-only
  for SPICE; order passive→wire→board→DRC. Targets T residual for **next** launch.

