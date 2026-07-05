---
id: T-R2
title: PLM vault core - services/plm/ engine, auto-history, checkpoint/release/restore, plm_* MCP tools
workstream: R
owner: opus-quarry
status: doing
branch: task/T-R2-plm-vault-core
depends_on: [T-R1]
lane: plm-projects
kind: new
priority: 3
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

## Progress log
- 2026-07-05 04:59 UTC [opus-quarry] PLM vault core implemented on the frozen aicad.plm/1 contract. DEPENDENCY: built on the T-R1 contract branch (merged into T-R2 worktree); T-R1 merges to master separately — merge T-R1 first. services/plm/: gitbind.py (dulwich-primary binding per contract §7 verdict; system git = optional subprocess gc/repack accelerator, pure-dulwich guaranteed fallback, never bundled), snapshot.py (explode/reassemble .step+/.ecad+ ZIPs; document.json canonicalized byte-stable, ALL other entries incl netlist.json preserved verbatim under extra/ — lossless restore), manifest.py (aicad.plm/1 registry: rev letters A->B->AA, fixed lifecycle In work->Released->Superseded, forward-tolerance), history.py (4 kinds + meta.json §3.4; no timestamps/attribution in document.json), vault.py (zero-config open==init, save/checkpoint/release/restore/status/history; restore rebuilds working container + APPENDS forward), client.py (§7.1 in-process PLMVaultClient — the substrate T-W18 plm_bridge imports behind AICAD_PLM_BRIDGE=1; AI-authored twin of plm_checkpoint, ONLY writer of §3.4 ai block). services/mcp/plm_tools.py: plm_init/open/save/checkpoint/release/history/restore/status/compare, §0 envelope, registered append-only in server.py; plm_compare reuses doc_diff (visual eval_compare narration is T-R3). SAVE-PATH HOOK DECISION (task said decide+record): MCP-side — plm_save/plm_checkpoint ARE the hook; no worker RPC seam in v1 (lower-risk). DEPS: dulwich==1.2.7 added to requirements.txt + contracts/licenses.md same commit (no silent license debt). NON-GOALS recorded: remote/lock tools (plm_add_remote/sync/lock/unlock) are T-R7; drawing-titleblock rev stamp + full preflight battery are T-R4; visual compare narration is T-R3; CAM/piping/hydraulic/structural persistence not yet exploded (only .step+/.ecad+ containers versioned in v1 — those disciplines' on-disk formats are TBD). TESTS: 39 pass in gate venv (dulwich installed). Advisor review caught + fixed a release-pin null-persistence data-corruption bug and 2 test proof-gaps (byte-equality for drawing/ecad round-trip; real FastMCP dispatch). Existing plm_bridge 57 tests still green. Gate scripts/check.py 4/4 PASS. graphify updated. Branch pushed.
