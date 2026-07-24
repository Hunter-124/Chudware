# spec-sketch bench (IP-13)

**Measures:** given a compact spec (sketch family + target dims + tolerance), does the
produced sketch's solver land **full** (every degree of freedom pinned by a relation or a
driving dimension) **and** are all requested dimensions realized within `spec.tol`? Success
bar (survey §6, dense solver reward): **full-solve AND in-tol** on every example.

**Spec model** — one JSON file per example under `specs/`, e.g.:
```json
{"id": "rect_plate_01", "family": "rect_plate", "dims": {"width": 80, "height": 50}, "tol": 0.01}
```

**Dataset** — 10 committed spec fixtures spanning 5 sketch families (2 each):

| family               | dims                                                    |
|----------------------|----------------------------------------------------------|
| `rect_plate`         | `width`, `height`                                         |
| `circle_hole`        | `diameter`                                                 |
| `slotted_plate`      | `width`, `height`, `slot_length`, `slot_dia`               |
| `l_bracket`          | `width`, `height`, `leg_width`                              |
| `bolt_circle_layout` | `bolt_circle_diameter`, `hole_diameter`, `num_holes`        |

`spec.json` freezes the content hash over `specs/` (see `tools/flywheel/bench/dataset.py`);
a second machine loading the same files gets the same hash. Unlike `regen_fix`, this dataset
is not generated — the spec fixtures themselves ARE the frozen corpus, committed verbatim.

**Deterministic baseline** = `fill_template(spec)`: a parametric template filler, one builder
function per family, that maps a spec directly to a fully-constrained sketch feature
(`aicad.part/1` storage shape — `params.entities` / `params.constraints` / `refs.plane`, see
`contracts/fixtures/part_bracket.json`) — entities placed at the exact requested coordinates,
plus every coincident/horizontal/vertical/fix/distance/diameter constraint needed to pin all
degrees of freedom. This is the kill-switch candidate: given an already-well-formed spec, it
always succeeds (1.0 on every fixture) — the interesting residual for a future model lives
**upstream** of this bench (turning a natural-language request into a well-formed spec), not
in template-filling itself.

**Scoring — two paths:**

- **offline (default, no core):** realized dimensions are measured **geometrically** from
  the produced sketch's entity coordinates (bounding boxes, point-to-point distances, circle
  radii) — never echoed back from the input spec (see `measure_dims`, exercised directly by
  `tools/flywheel/bench/tests/test_spec_sketch.py::test_realized_dims_are_computed_from_geometry_not_echoed`).
  "Solver status" is stood in for by `solver_status_proxy` — an **honest DOF-counting
  proxy**: every entity contributes its raw degrees of freedom (line=4, circle=3); every
  constraint removes the DOF it is documented to remove (coincident=2, horizontal/vertical=1,
  distance=1, diameter=1, fix=2); `full` iff the remainder is exactly 0. **This is a
  stand-in for the real constraint solver, not the real thing** — it can't detect a
  genuinely conflicting/over-defined sketch the way the C++ solver would; it only tells you
  whether enough constraints exist in principle.
- **live (core present):** the same filler's sketch is built via `core_bridge`
  (`doc_new` + `doc_apply_ops`, op-wire shape) and the REAL solver status is read off
  `doc_sketch_solve` — the same RPC the `check_sketch` MCP tool wraps. Note:
  `services.verify.battery.verify(doc_id, ["check_sketch"])`'s binary score only reflects
  whether the RPC call itself succeeded — `check_sketch` returns `ok: True` on a successful
  solve regardless of full/under/over-defined — so the battery's score alone cannot carry
  the full/under/over distinction; this bench still calls it (recorded as
  `detail.battery_scores`, "score through the battery") and separately reads `status`
  directly off the `doc_sketch_solve` result for the actual pass/fail. Guarded
  skip-with-reason (falls back to fixture-mode scoring, `detail.live_error`) when the core
  is unavailable — resolution is entirely `core_bridge`'s own
  (`$CHUDWARE_LIBCHUDWARE` / `$CHUDWARE_DIST` / known build dirs); never a hardcoded sibling
  worktree path.

**Baselines:** `deterministic` (template filler, measured offline — 1.0 on every fixture by
construction) and `frontier` (pending T-H7 logging + credentials).

**Run:**
```
python -m tools.flywheel.bench.runner spec_sketch          # full (10)
python -m tools.flywheel.bench.runner spec_sketch --smoke  # one example per family (5)
```

`results/smoke_result.json` is a committed sample report.
