---
id: T-BL15
title: SUPERSEDED by workstream R — in-app document version control (Pillar 19)
workstream: backlog
owner: unassigned
status: backlog
branch: task/T-BL15-doc-version-control
depends_on: []
lane: plm-projects
kind: new
---

## Context

**This is a raw proposal, not a scoped task.** Do not claim or implement — it needs a
human (Erik) to refine the idea and decide an approach before it becomes real work.
Recorded 2026-07-02 per explicit instruction while building this repo's *dev-team*
git/issue coordination tooling (`work/tools/teamsync.py`, `team_mcp.py`,
`issue_reporter.py` — see [work/context/work-system.md](../context/work-system.md)):
that tooling is deliberately internal-only, for developing Chudware itself, and is
**not** what this proposal is about.

The idea: once the dev-team tooling above is up and proven, evaluate adding
**source/version control for the documents Chudware's *users* create** — i.e. history,
branching/comparison, and possibly multi-user coordination for `.step+` parametric
documents and assemblies, analogous to PDM/PLM version control in SolidWorks/Onshape,
not to be confused with this repo's own git history. Open questions a human needs to
settle before this is schedulable:

- What does "version control" mean for a parametric feature-tree document — full git-
  style diffable history of doc_ops (contracts/doc_ops.md already gives an append-only
  op vocabulary, which may make this cheaper than it looks), snapshot/checkpoint saves,
  or true branching/merging of feature trees?
- Local-only (like git) vs. requiring Chudware Cloud / an account (ties into
  `docs/AI_TOOLING_MASTER_PLAN.md` pillar 12's provider/session layer and the
  company-hosted relay)?
- Multi-user concurrent editing of one document — in scope at all, or out of scope
  for v1?
- Where does this sit relative to the DAG orchestrator's own checkpointed-graph state
  (`docs/PLATFORM_ADR.md`'s "DAG workflow orchestrator" pillar) — same mechanism
  reused, or a separate concern?
- UI surface: does this live in the FeatureManager tree (undo/redo already exists —
  `doc_undo`/`doc_redo` per `contracts/worker_rpc.md`), a separate history panel, or
  something else?

## Definition of done

- [ ] Human (Erik) has reviewed and either refined this into a scoped proposal (with
      the open questions above answered) or explicitly declined it
- [ ] If refined: a follow-up task/doc section is written with an actual design, and
      *this* task is closed/superseded, not implemented as-is

**PARKED (Erik, 2026-07-03): out of scope for the v1 prototype push — low priority; revisit after the five objectives close.**

**SUPERSEDED (Hunter directive, 2026-07-03, later the same day): un-parked and refined.
The human review this task asked for happened — all open questions above are answered
in [docs/PLM_VERSION_CONTROL_PLAN.md](../../docs/PLM_VERSION_CONTROL_PLAN.md)
(Pillar 19). Execution is workstream R: T-R1 (contract) → T-R7 (team sync). Do not
claim this file; claim the T-R tasks.**

**CONTRACT FROZEN (T-R1, 2026-07-03):** every open question above is answered by
[docs/PLM_VERSION_CONTROL_PLAN.md](../../docs/PLM_VERSION_CONTROL_PLAN.md) +
[contracts/plm.md](../../contracts/plm.md) (`aicad.plm/1`, frozen). Mapping of this
file's open questions → answers:
- *What "version control" means for a feature-tree doc* → snapshot-based on the
  T-H2 canonical `document.json` (contracts/plm.md §0, §2; plan §3.1). Not
  op-log, not branch/merge (non-goal, plan §6).
- *Local-only vs Chudware Cloud/account* → local-first, zero-account; any git remote
  the user controls for self-hosting; `chudware-cloud` remote RESERVED config-only
  behind the Pillar-12 gate (contracts/plm.md §6.1).
- *Multi-user concurrent editing* → out of scope; team v1 = advisory locks +
  no-merge + copy-as-new-item conflict guard (contracts/plm.md §6).
- *Relation to the DAG checkpoint store* → deliberately NOT reused (plan §3.5).
- *UI surface* → separate History panel + rev badges + Project Hub, not the
  FeatureManager; undo/redo stay session-scoped and distinct from history
  (plan §3.5, §4; contracts/plm.md §9).
