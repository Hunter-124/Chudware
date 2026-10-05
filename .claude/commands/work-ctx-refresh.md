---
description: Audit and refresh the section context packs in work/context/ (source maps for the /ctx-* commands)
---
Available context packs:

!`ls work/context/ 2>/dev/null || echo "(none yet)"`

The /ctx-* slash commands feed these packs to sessions, so nobody has to re-map the
codebase. Your job is to bring them up to date. $ARGUMENTS

Refresh the packs named in the arguments. With no arguments, use the current task's
changed areas and the session driver's supplied scope; do not start a repository-wide
history sweep. The driver assigns disjoint pack ownership when delegating.
For each pack:
1. Orient with `graphify query` / `graphify explain` when the map is available, following
   the graphify skill. Under an edit-only assignment, use the supplied audit/source
   evidence and return graph refresh to the driver rather than running diagnostics.
2. Verify every cited path, symbol and command against the current tree.
   - Fix moved or renamed entries and add new load-bearing entry points.
   - Cite symbols (`file` + function/class), not line numbers; line numbers rot.
   - Fold new gotchas in from docs/REFACTOR_LOG.md.
   - Drop claims about deleted subsystems instead of annotating them.
3. Keep the pack concise and useful, preserving its Scope / Entry points / How it
   connects / Debugging playbook / Graphify starters / Related tasks-docs format.
   Retain load-bearing capability, safety, persistence and ownership invariants; trim
   line counts, incident narratives and completed-task procedure. Repo-wide rules stay
   in the canonical skills (`chudware-dev`, `cad-chudcode-integration`,
   `gui-orchestrator`) and task policy stays in `work/WORKFLOW.md`.

Edit only. Do not run builds, gates, tests, formatters or graph regeneration per pack.
Return changed paths, source evidence, unresolved prerequisites and applicable checks
to the session driver. It owns the combined verification, generated outputs and one
graph refresh (work/WORKFLOW.md §3). Leave edits uncommitted; no leaf-agent git commands.
