# contracts/ — frozen interface seams

This directory holds the **frozen interface contracts** for AI-CAD. These are
THE seams between layers: the C++ `core/` (`libaicad`), the Python `engine/`
oracle, the `services/worker` routing layer, and the UI. They were promoted here
from `docs/contracts/` in refactor node N0c so they sit at the top level next to
the code they bind, not buried under `docs/`.

## Frozen-seam policy

These contracts are **append-only**. Any change to an existing seam requires
**integrator sign-off** and an explicit version bump. Leaf agents may read them
freely but must not edit them. One owner per round per append-only file (see
`AGENTS.md` swarm rules).

## Frozen contracts

- `worker_rpc.md` — flat C ABI / worker RPC wire format + method catalogue.
- `doc_ops.md` — document op vocabulary, envelope, transactionality, errors.
- `doc_schema.md` — document tree schema (entities, handles, constraints).
- `selectors.md` — selector normative algorithm (weights, thresholds, tiebreak).
- `mesh_format.md` — AICM v1 mesh byte-level format.
- `stepplus.md` — STEP+ persistence format (required entries, load order, save).
- `events.md` — async event payloads (`sim_progress`, etc.).
- `feature_params.md` — per-feature-type param/field tables (PM form order).

## Agent-layer contracts (Pillar 20 / design pipeline)

- `advisor.md` — advisory layer: design ledger (`aicad.ledger/1`), roles, triggers,
  councils, tiers, effort presets, cognition ladder (`aicad.ladder/1`).
- `design_pipeline.md` — guided design-thinking pipeline (`aicad.designpipe/1`, T-DP1):
  the six-stage IO schemas (`feature`/`geometry_spec`/`linkage`/`build_node`), the new
  ledger event kinds (**extends `aicad.ledger/1`**), the ledger-reading stage-gate
  definitions, the effort/auto-mode hooks, and the geometry-kind → trigger bindings.
  Refines (does not replace) `advisor.md` §2/§20/§22/§23 and `phases.py`.

## Frozen subdirectories /附属 files

- `ecad/` — ECAD sub-contracts (`ecad_rpc.md`, `ecad_ops.md`, `sch_schema.md`,
  `pcb_schema.md`, `netlist.md`, `library.md`, `fab_outputs.md`, `sim.md`).
- `fixtures/` — golden contract fixtures (`box.aicm`, `part_bracket.json`,
  `asm_clamp.json`, `mock_replies_v2.json`, `make_box_aicm.py`).
- `icons.md` — icon atlas inventory (names, sizes, style, JSON shape).
- `licenses.md` — third-party license posture / obligations per dependency.
