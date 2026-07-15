# MegaRun13 Progress

- **Run ID:** `mega-13-fc-rocket-20260715`
- **Baseline:** `6894f19d`
- **Driver:** `reports/runs/mega-13/run_mega.py`
- **Account:** `17d17fb1-247a-4800-ba97-e7b5150bc53a`
- **Peers:** Fixer13=`DustyGibbon`, MegaRun13=`CurlyTermite`
- **Engine:** hostd `night2-3054-gcb65bfd9-dirty` (running git_rev matches disk binary)
- **Axes:** P (board_from_schematic+DRC executed) + Q (live AC PASS ≥6) + R (AC5 wired SPICE) + A–O regression

## Status

- [x] Pairing confirmed (Main: Fixer13=DustyGibbon)
- [x] Daemon preflight: running == disk hostd (`night2-3054-gcb65bfd9-dirty`, pid 2980908)
- [x] Account `17d17fb1-…` + spawn_probe PASS (ecad chain tools present) + auto_pick_smoke PASS
- [x] No other xAI consumer live
- [x] Fixer13 **fixes bound** DM (4 fixes, 70 passed)
- [x] Primary attempt-1 archived (`attempt-1-ollama-all/` — rank miss)
- [x] Primary attempt-2 archived (`attempt-2-model-not-found/` — API id)
- [x] Primary attempt-3 pid **3500779**, wall **610.86s**, finished
- [x] Grade P/Q/R + A–O; findings + criterion_ACn.json + roadmap append

## Scoreboard (attempt-3 primary)

- **P FAIL** — board_from_schematic=0, ecad_run_drc=0 (schematic+place only; force-chain residual)
- **Q PASS** — 6 live PASS (AC2+AC3+AC6+AC7+AC8+AC9)
- **R FAIL** — AC5 empty-netlist / no wire tools
- **N PASS** (reg) — 6 ≥ 4 with AC3+AC6+AC7+AC2
- **O FAIL** (reg) — same as P
- **A–M regression PASS**
- Terminal: `needs_correction` / `verify_failed: ['AC1', 'AC4', 'AC5']`
