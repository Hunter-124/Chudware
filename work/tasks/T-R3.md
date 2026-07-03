---
id: T-R3
title: PLM change intelligence - plain-language summaries, AI-assist/AI-managed tiers, compare views
workstream: R
owner: unassigned
status: todo
branch: task/T-R3-plm-change-intelligence
depends_on: [T-R2]
---

## Context

The layer that makes the timeline readable and the AI helpful
([docs/PLM_VERSION_CONTROL_PLAN.md](../../docs/PLM_VERSION_CONTROL_PLAN.md) §2, §4).
Semantic diff already exists (`services/mcp/doc_diff_tools.py`, T-H2); the narration
layer (Pillar 11 describe_*) turns its structured output into one-line human
summaries ("Increased wall 3→5 mm, added 4× M6 bolt circle"). Deterministic-first
per Pillar-13 rules: templates from doc_diff fields; any learned polish is optional
garnish. Three assist tiers with manual always primary: manual-only /
AI-assisted (default: draft messages, suggest checkpoints, flag stale refs) /
AI-managed (opt-in: Copilot cuts checkpoints at task boundaries, attributed +
one-click discard).

## Files / areas touched

- services/plm/summarize.py — NEW: doc_diff → templated plain-language summary
  (deterministic; numbers copied, never invented), stored as summary.txt per entry
- services/plm/assist.py — NEW: checkpoint-worthiness heuristics (op count/time/
  magnitude), stale-ref detection (item consumes a superseded rev — needs T-R4's
  manifest fields; land behind a capability check if T-R4 not merged), AI-managed
  policy hooks for the copilot loop (services/agent/loop.py seam)
- plm_compare: semantic (doc_diff) + visual — two-revision eval_compare
  side-by-side (engine doc-load contract: doc_new+eq_set+add_feature per
  docs/… engine-doc-load-verify-contract), drawings/ECAD as 2D overlay
- Telemetry: AI-cut entries route through the T-H1 envelope with attribution

## Definition of done

- [ ] Every history entry gets a deterministic plain-language summary; numerals
      provably copied from doc_diff output (test: no numeral in summary absent
      from the diff)
- [ ] Copilot drafts checkpoint/release messages via plm tools; user-editable;
      manual path works with AI fully disabled
- [ ] AI-managed tier: copilot cuts an attributed checkpoint at end-of-task in a
      scripted agent run; timeline marks it AI; one-click discard works
- [ ] plm_compare returns semantic change list + renders visual side-by-side for
      two part revisions (screenshot artifact in task notes)
- [ ] Tier selection persisted per project; default = AI-assisted; manual-only
      floor verified (no AI calls issued)
- [ ] Tests: summary determinism, numeral-fidelity, tier gating; gate +
      graphify update .
