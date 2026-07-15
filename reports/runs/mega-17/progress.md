# Mega-17 progress

- **Peers:** Fixer17=`AlrightMosquito`, MegaRun17=`RequiredWoodpecker`
- **Engine:** hostd `night2-3054-gcb65bfd9-dirty` (running git_rev matches disk binary)
- **Baseline:** `cd764e92`
- **Frontier:** `openrouter:deepseek/deepseek-chat-v3.1` PAID
- **Axes:** Z1/Z2/Z3 + A–Y regression; target ≥8 live AC PASS

## Status

- [x] Pairing confirmed (Main: Fixer17=AlrightMosquito, MegaRun17=RequiredWoodpecker)
- [x] Daemon preflight: running == disk hostd
- [x] spawn_probe PASS + auto_pick_smoke PASS (axis Y)
- [x] Spend snapshot before primary
- [x] Fixer17 **fixes bound** DM (Z1/Z2/Z3; 91 tests)
- [x] Attempt-1 aborted double-launch archived
- [x] Primary a2 finished wall **145.86s** terminal `needs_correction`
- [x] Graded: live AC PASS **4**/9; Z1 **PASS**, Z2 **FAIL**, Z3 **PASS**, Y **PASS**
- [x] findings.md + run_summary.json + roadmap append
- [x] DM Fixer17 live residuals
- [ ] Live PASS ≥8 (missed)
