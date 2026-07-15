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
- [x] Daemon preflight + account + spawn_probe + auto_pick + xai clear
- [x] Fixer13 **fixes bound** (1–4 pre-run; 5–8 post incl HARD chain gate)
- [x] Attempt-1 archived (`attempt-1-ollama-all/`)
- [x] Attempt-2 archived (`attempt-2-model-not-found/`)
- [x] Attempt-3 **primary graded** pid **3500779**, wall **610.86s**
- [x] Grade P/Q/R + A–O; findings + criterion_ACn.json + roadmap append
- [x] Attempt-4 aborted for mega-14 (`attempt-4-aborted-for-mega14/`) — Main opened mega-14 pair
- [x] MegaRun13 **idle** (lane closed)

## Scoreboard (attempt-3 primary)

- **P FAIL** — board_from_schematic=0, ecad_run_drc=0
- **Q PASS** — 6 live PASS (AC2+AC3+AC6+AC7+AC8+AC9)
- **R FAIL** — AC5 empty-netlist
- **N PASS** / **O FAIL** / **A–M regression PASS**
- Terminal: `needs_correction` / `verify_failed: ['AC1', 'AC4', 'AC5']`
- Fix8 hard chain gate bound after a3; not exercised live (a4 aborted)
