# C10 — Sketch constraint solver (full SolidWorks relation set)

Model: **sonnet** · Wave 1 · Depends: C06 (nsolve) · Follow `docs/sessions/README.md` conventions.

## Mission
`engine/doc/sketch_solver.py`: parameterize sketch entities, express every contract constraint
as nsolve residuals with analytic Jacobians, and report SolidWorks-style statuses.

## Read first
1. `docs/contracts/doc_schema.md` §3 (entities, handles, constraints — the full table)
2. `engine/doc/nsolve.py` (C06's API — read its module docstring and tests)
3. `docs/contracts/worker_rpc.md` sketch methods (what callers need back)

## Owned paths
`engine/doc/sketch_solver.py` · `engine/doc/test_sketch_solver.py`

## Deliverables
1. Parameterization (document it in the module docstring): point=(x,y); line=2 points;
   circle=(cx,cy,r); arc=(cx,cy,r,a0,a1); ellipse=(cx,cy,rx,ry,rot); slot=2 points + w;
   spline=through-points. Handles (`e3.p1`, `e3.center`, arc `start/end` = derived points
   cx+r·cos(a), …) map to variable indices or derived expressions with chain-rule Jacobians.
2. Residuals for ALL of: coincident, horizontal, vertical (entity and point-pair forms),
   parallel, perpendicular, tangent (line-circle/arc, circle-circle), equal (length/radius),
   concentric, midpoint, symmetric, fix, point_on (line/circle/arc), distance (pt-pt, pt-line,
   parallel line-line), angle, radius, diameter. Driving dims use the dim value; `driven:true`
   dims are excluded from the system and instead measured after solve.
3. `solve_sketch(sketch_params: dict, equations_env: dict, warm: SolveState | None)
   -> SketchSolution {entities: updated coords dict, statuses: {entity_id: "under"|"full"|
   "over"}, dof: int, redundant: [constraint ids], conflicting: [constraint ids],
   converged: bool, state: SolveState}` — per-entity status from nsolve's analysis restricted
   to that entity's variables (a variable is "fully defined" when removing it from the free
   set doesn't change rank — practical approach: an entity is `full` if all its variables are
   determined, i.e. perturbing them violates residuals; implement via nullspace: entity vars
   with zero projection onto the Jacobian nullspace are determined).
4. `drag(state, handle, target_xy)` → soft-target solve (nsolve `soft_target`), time budget
   10 ms, returns updated coords without committing.
5. `test_sketch_solver.py`:
   - the bracket fixture's Sketch1 (4 lines, h/v, coincidents, 2 dims, fix) solves: converged,
     dof 0, every entity `full`
   - remove a dim → dof 1, the affected entities `under`
   - duplicate the width dim → redundant lists it; contradict it (80 vs 90) → conflicting
   - change dim value 80→100 → solved width 100 (parametric edit path)
   - tangent line-circle, concentric arcs, symmetric about a line, midpoint: each a small
     canonical sketch asserting the geometric truth
   - drag: pull a free corner of an under-defined rect → constraints hold (sides stay h/v),
     dragged point lands near target; warm re-drag ≤ 3 iterations
   - expression dim: value comes from `equations_env` (e.g. `W`)

## Constraints
- Pure: dict in → dict out; no document/compiler imports. Worker wiring is C11/C18's job.
- Every residual gets an analytic Jacobian (nsolve forbids finite differences).

## Verification
`& $py -m engine.doc.test_sketch_solver` green; C06's `test_nsolve` still green.
