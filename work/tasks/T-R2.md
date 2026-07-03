---
id: T-R2
title: PLM vault core - services/plm/ engine, auto-history, checkpoint/release/restore, plm_* MCP tools
workstream: R
owner: unassigned
status: todo
branch: task/T-R2-plm-vault-core
depends_on: [T-R1]
---

## Context

The storage engine behind Pillar 19
([docs/PLM_VERSION_CONTROL_PLAN.md](../../docs/PLM_VERSION_CONTROL_PLAN.md)),
implementing the frozen `contracts/plm.md`. Local-first, zero-config: a project is
a folder; the vault is a hidden git repo at `.aicadvault/` storing the exploded
canonical form of each container (T-H2 canonical writers already exist:
`core/doc/stepplus.cpp::canonical_doc_json`, `engine/doc/canonical.py`,
`engine/ecad/formats.py`). Auto-history entries are cut on explicit save (NOT on
autosave — T-Q2's ring stays the crash-recovery layer). Restore never rewrites
history: restoring an old state appends a new entry.

## Files / areas touched

- services/plm/ — NEW: vault.py (init/open/status), snapshot.py (explode
  .step+/.ecad+ → shadow tree, re-assemble on restore), history.py (entries,
  checkpoint, release, restore), gitbind.py (the T-R1-chosen binding)
- services/mcp/plm_tools.py — NEW: plm_project_status, plm_history,
  plm_checkpoint, plm_release, plm_restore, plm_compare (semantic stub — T-R3
  fills narration/visual) — register append-only
- Save-path hook: doc_save/ecad_save flows also land a vault entry when the doc
  lives in a project (worker RPC seam or MCP-side hook — decide in-task, record)
- CAM/piping/hydraulic/structural/requirements docs: include whatever of these
  already persists to disk; note gaps (CAM persistence is TBD per survey) as
  explicit non-goals in the task note rather than silently skipping

## Definition of done

- [ ] Zero-config init: opening/saving a doc inside a project folder creates the
      vault silently; no user prompt, no visible files beyond `.aicadvault/`
- [ ] Auto-history on explicit save; checkpoint(message) and release() produce
      entries per contract; release freezes geometry.step; restore(entry) rebuilds
      the working container byte-faithfully and appends (never rewrites)
- [ ] Round-trip proven: save → checkpoint → edit → restore → doc_diff vs
      original == empty for part, assembly, drawing, and .ecad+ docs
- [ ] plm_* MCP tools registered and exercised end-to-end via MCP client
- [ ] Vault stays lightweight: checkpoint of a typical part adds O(KB) (canonical
      JSON delta), not O(geometry); measured and recorded
- [ ] 20+ tests in mcp-venv (init, round-trip per doc kind, release freeze,
      restore-appends, corrupted-vault graceful degradation → app still opens docs)
- [ ] gate + graphify update .
