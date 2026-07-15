# Mega-22 progress

- Run ID: `mega-22-fc-rocket-20260715`
- Baseline: `0f43d126`
- Fixer22: GrossShark (P22/Q22/R22)
- MegaRun22: UnusualBaboon
- Frontier: xai-sub grok-4.5 @ 06dc3359; mid openrouter deepseek; leaves ollama

## Checklist

- [x] Copy run_mega/grade_run/_preflight/wait_and_launch from mega-21
- [x] Retarget RUN_ID/baseline/cycle; add P22/Q22/R22 axes to grade + probe
- [x] Preflight: daemon stamp vs disk, spawn_probe, auto_pick, account ACTIVE, no other xai
- [x] Wait Fixer22 `fixes bound` DM + fixes.md (P22/Q22/R22 probe PASS; 158p)
- [x] Launch primary (pid 1025640 / status pid 1025641)
- [x] Monitor + grade P22/Q22/R22 + regression (a1 live 4/9; P22/Q22 FAIL; R22 PARTIAL)
- [x] findings.md + criterion_ACn + status JSON + roadmap append
