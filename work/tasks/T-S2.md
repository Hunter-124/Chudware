---
id: T-S2
title: Land the AI-experience lane (recon/GSL) uncommitted files
workstream: setup
owner: main-checkout
status: todo
branch: task/T-S2-land-ai-experience
day: 1
depends_on: []
---

## Context

The AI-experience lane finished and verified its work (`docs/HANDOFF_AI_EXPERIENCE.md`,
summarized in `docs/HANDOFF_GUI_OVERHAUL.md` §4) but its files sit uncommitted in the
main checkout — Commit B of the approved commit plan in `HANDOFF_GUI_OVERHAUL.md` §6.

**SPECIAL TASK — main checkout only, human-supervised** (dirty working-tree files are
invisible to worktrees; owner stays `main-checkout`). Commits land on `master` per the
approved protocol.

## Files / areas touched

- `services/mcp/recon_tools.py`, `services/mcp/server.py`, `services/mcp/test_gsl_tools.py`,
  `services/mcp/test_recon_tools.py`, `services/mcp/test_cad_tools.py`
- `services/agent/prompts/shared/cheatsheets/reverse_engineer.md`, `tools/gsl_eval.py`
- `docs/RECOGNITION_RESIDUAL.md` (untracked — add it)
- `docs/GEOMETRY_LLM_REPRESENTATION_REVIEW.md` (header note)
- EXCLUDED (other lanes): `ui/abstract/panel_spec.*`, `services/mcp/cad_tools.py`,
  `docs/REFACTOR_LOG.md`

## Steps

1. Re-run the acceptance suite:
   `python3 -m pytest services/mcp/test_gsl_tools.py services/mcp/test_recon_tools.py -q`.
2. `git add` the exact Commit B file list; commit with a summary of the lane
   (recon tools + GSL tests + eval + GNN go/no-go verdict doc).
3. Confirm `git status` leaves only the panel_spec lane (and any GUI files if T-G1
   hasn't run yet) dirty.

## Definition of done

- [ ] pytest green before committing
- [ ] Commit B landed with the exact file list, no scooped lanes
- [ ] `graphify update .` run
