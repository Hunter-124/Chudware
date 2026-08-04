# T-D2 — questions / decisions for the Fable supervisor
> ARCHIVED 2026-08-04 — task closed; the driver proceeded on its recommended default, per work/questions/README.md.

Driver: opus-dawn. Chained off `task/T-D1-structural-schema-scaffold` (merge order D1 → D2).
All items below are **non-blocking** — implemented with the recommended answer, proceeding.
None change D1's frozen `structural_schema.md`; they interpret gaps the contract left open.

## Q1 — `struct_analyze` signature: `(doc_path, load_combo)` vs contract §5 `{combo?}`

`structural_schema.md` §5 lists `struct_analyze {combo?} -> {results}`, implying a **stateful
session doc** (like the CAD tools' current-`doc_id` model). The task work order (step 4) instead
specifies `struct_analyze(doc_path: str, load_combo: str) -> dict`.

**Recommended (implemented):** follow the task's `(doc_path, load_combo)` form. It is
stateless/testable today, before the day-3 session tools (`struct_new`/`struct_add_element`) that
would give a "current doc" exist. When those land, a thin `{combo?}` wrapper can resolve the
session doc to a path and delegate here — additive, no rework. Not treated as a contract change
(the §5 result shape `{results:[AnalysisResult]}` is honored exactly).

## Q2 — load-case → member mapping (contract §3 `LoadCase` has no element target)

The §3 `LoadCase` carries `gravity` / `distributed` / `point` but **no element id**, and no nodal
or axial load concept. To run the DoD case ("2 m post, 1 kN axial") I interpreted:

- Each doc load case is applied to **every non-footing member**, in the dominant global direction
  of its `gravity` vector, scaled by the case's own `factor`.
- `point.p_n` at `point.at_mm` → a PyNite member point load. On a vertical post (member axis +Z)
  a `gravity=[0,0,-1]` point load is therefore **pure axial** — exactly the DoD case. On a
  horizontal beam the same field is a transverse load.
- `distributed.w_n_per_mm` → a uniform member load in the gravity direction.

**Recommended (implemented):** keep this "apply to all line members" rule for v1. If per-element
targeting is wanted, the cleanest additive extension is an optional `element_ids: [str]` field on
`LoadCase` (absent ⇒ all members) — a **contract addition** I did *not* make unilaterally. Flag for
ratification if D3/D4 need targeted loads.

## Q3 — supports & the ASCE combo/kind layering (design notes, no decision needed)

- **Supports:** a `footing` element's nodes are fully fixed; a doc with no footing gets its single
  lowest node fixed as ground, so a lone post/beam is solvable. v1 idealizes footings as **fixed**
  (moment) connections — pinned/roller variants are a later refinement. The canonical
  simply-supported (pinned) hand-calc validation therefore drives `run_analysis` on a directly-built
  pinned model rather than the doc path (see `test_analysis.py`).
- **Combos vs kinds:** the new `load_cases.py` `LoadCombo` presets (DEAD_ONLY=1.4D, DEAD_LIVE,
  DEAD_LIVE_WIND) are ASCE 7-16 §2.3.1 strength combinations layered as PyNite load *combinations*
  over the doc's kind-tagged load *cases*. Named `LoadCombo` (not `LoadCase`) to avoid colliding
  with the contract's doc-level `LoadCase`.

## Environment note (not a question)

- PyNiteFEA (MIT) installed into **`.venv-mcp`** (Python 3.11), which is the venv carrying
  numpy/scipy. The alternate `~/.local/share/Chudware/mcp-venv` (Python 3.14) has **no** scipy, so
  PyNite cannot run there — hence tests run under `.venv-mcp/bin/python`, overriding the prompt's
  mcp-venv hint on primary-evidence grounds. `licenses.md` row added.
