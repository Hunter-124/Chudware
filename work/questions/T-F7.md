# T-F7 — questions / decisions log (sonnet-nell)

## Q1 (FYI, resolved by primary source): driver-notes CAM key names were stale

The driver notes said `cam_simulate` returns `gouge_count`, `rapid_through_count`,
`machined_fraction_pct`. The C++ core (`core/cam/stock_sim.cpp SimResult::to_json`)
actually emits `gouges`, `rapid_collisions`, `machined_fraction` (0..1), plus
`remaining_volume_mm3` / `cycle_time_s`, nested as `{"results": [...]}` keyed by
`setup_id`. I narrated the real keys and codified them in `contracts/narration.md`.
No action needed unless the core intends to rename.

## Q2 (decision made, flagging for review): max-stress location cannot be a sid join today

**Question:** Step 3 asks for "where max stress is (named faces via sid join)". The
persisted study record (`_persist_study` → `sim_study_add`) stores ONLY summary
scalars (`max_von_mises_mpa`, `max_disp_mm`, `min_fos`, `n_dof`) — the per-node
von Mises field and node coordinates are not persisted, and no RPC exposes a
per-face stress maximum (`doc_study_overlay` does not exist).

**My answer (implemented):** honest degradation + the traceable join that IS
possible: `describe_results` sid-joins the STORED fixture/load face queries against
the live face layer (`doc_query`, describe_part's sid namespace) — "Fixed at: face
F… (plane on bottom)", "Load applied on: face F… (plane on top)" — and states
explicitly: "Max-stress node location: not stored in the study record — the study
persists summary scalars only; re-run run_fea_static for the live field." Guessing
a location from the load faces would violate the every-number-traceable rule.

**Alternatives:** (a) extend `_persist_study` to store the max-VM node's XYZ and
nearest face — a contract change to the study record, out of my scope fence;
(b) re-run the solver inside describe_results — forbidden ("never recompute").
If (a) is wanted, it is a small follow-up: `fea_static.cpp` already has the
per-node array at summary time.

## Q3 (decision made): per-op stepdown/stepover not exposed by any read RPC

`cam_tree` (the only job read-back) returns ops as {id, name, type, tool_number,
suppressed} — no cut parameters. The narration keeps the stepdown/stepover/
stock-to-leave lines CONDITIONAL (they render if/when the tree carries them) and
the contract documents the gap. Alternative: extend `tree_of` in
`core/cam/session.cpp` to include op params — core change, out of scope fence.

## Q4 (environment, FYI): staged libchudware.so predates sim_run_static

The staged `services/dist/Chudware/libchudware.so` (built Jul 1) has `sim_study_list`/
`sim_study_add`/`cam_*` but NOT `sim_run_static`. Tests therefore seed studies
through `sim_study_add` — the same RPC `_persist_study` writes through after a real
solve — giving deterministic exact-slot fixtures instead of solver-dependent ones.
This matches the house "deterministic fixtures, exact slot assertions" instruction
and stays valid after a core rebuild.

---

ANSWER (Fable, 2026-07-02):

- **Q2 (max-VM location not persisted):** Your handling is correct per the narration
  contract — state it explicitly, sid-join the BC faces. Extending `_persist_study`
  to store the max-VM node/face is APPROVED as a follow-up; I've seeded it into
  T-F8's notes (natural Pillar-11 continuation). Do not do it on this branch.
- **Q3 (stepdown/stepover not exposed):** Conditional rendering is right. Exposing
  per-op params through cam_extract/cam_tree is a small core-cam follow-up — also
  seeded into T-F8's notes for whoever owns the next narration slice.
- **Q4 (staged .so predates sim_run_static):** Not blocking. Whoever next runs a full
  core rebuild (any C++-touching task) refreshes the staged .so; the solver-path test
  can be re-run then. Noted on FOREMAN.
- **Driver-notes corrections:** thank you for verifying against primary sources —
  the contract now codifying the REAL RPC shapes (results[].machined_fraction etc.)
  is exactly right; primary source beats my notes whenever they conflict.
