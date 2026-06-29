# C01 — Doc compiler core + primitive/script/datum features

Model: **opus** · Wave 0 · Depends: none · Follow `docs/sessions/README.md` conventions.

## Mission
The heart of M1-WS-1A: compile an `aicad.part/1` document dict into OCCT solids, sequentially,
with per-feature caching, timing, error containment, and topology owner-tagging.

## Read first
1. `contracts/doc_schema.md`, `contracts/feature_params.md` (params/refs per type)
2. `contracts/selectors.md` §1–2 (what owner tags feed)
3. `engine/doc/schema.py` (validators you call before compiling), `engine/doc/equations.py`
4. `engine/kernel.py` — the forgiving namespace + `_exec_model`-style execution you will REUSE
   for the `script` feature (do not duplicate that logic; import and call it)
5. `docs/PLAN.md` §3.1

## Owned paths
`engine/doc/compiler.py` · `engine/doc/features/__init__.py` · `engine/doc/features/base.py` ·
`engine/doc/features/datum.py` · `engine/doc/features/primitives.py` ·
`engine/doc/features/script.py` · `engine/doc/test_compile.py`

## Deliverables
1. `features/base.py`: feature-compiler registry — `@register("box")` decorator;
   `CompileContext` (resolved equations env, bodies so far, datum lookup incl. reserved datums
   per doc_schema §5, helper `dim(params, key)` that returns the resolved float — expr already
   evaluated against the env).
2. `features/primitives.py`: box/cylinder/sphere/cone/torus per feature_params.md (mode
   boss/cut, merge, centered, optional `at` datum).
3. `features/datum.py`: plane/axis/point/csys → datum objects (build123d `Plane`/`Axis`/vectors)
   stored in the context, not bodies. Reserved datums (`plane.top` etc.) per doc_schema §5.
4. `features/script.py`: executes `params.code` through the EXISTING forgiving kernel machinery;
   resulting solid(s)/parts become this feature's bodies.
5. `compiler.py`:
   - `compile_document(doc: dict, cache: CompileCache | None) -> CompileResult`
   - `CompileResult`: `bodies: list[BodyOut]` (`body_id, name, shape(build123d Solid/Compound),
     color`), `feature_status: list[{id, ok, ms, error, dangling_refs}]`, `face_owners` /
     `edge_owners`: per body `{topo_index: feature_id}` (see tagging below), `tool_bodies:
     {feature_id: shape}` (the solid each feature fused/cut — patterns need this later),
     `equations_env: dict`.
   - **Caching**: per-feature key = sha256 of (prev key + canonical JSON of feature + equations
     env values it uses). Cache hit → reuse stored shape, skip compile. Expose hit/miss counters.
   - **Error containment**: a failing feature records `ok:false, error:<msg+hint>` and is
     SKIPPED; downstream continues on the last good body (SolidWorks behavior). Never raise out.
   - **Equations**: `evaluate_globals` once; resolve every dim `{value, expr}` (expr wins) into a
     WORKING COPY; report resolved values back in feature_status as `resolved: {param: float}`.
   - **Topology tagging**: after each feature, map faces/edges via
     `OCP.TopExp.TopExp.MapShapes_s(shape.wrapped, TopAbs_FACE, map)`; entries whose TShape was
     not in the previous feature's map (`IsSame` comparison via the indexed map `Contains`)
     get `owner = current feature id`. Unmatched leftovers keep their old owner. This is a
     heuristic — document it in the module docstring.
   - Suppressed features: skipped, status `ok:true, ms:0, skipped:true`.
6. `test_compile.py` (house-style runner):
   - box doc → volume == l*w*h; box + cylinder `mode:cut` → volume reduced by π r² h (within 1%)
   - expression-driven dim (`depth` expr) resolves; changing the equation changes the volume
   - `script` feature: legacy build123d snippet (`result = Box(10,10,10)`) → one body
   - error containment: bad feature mid-tree → its status error, neighbors still build
   - cache: recompile with feature 2-of-3 edited → feature 1 is a cache hit (assert counters)
   - owner tags: a cut cylinder's cylindrical face is owned by the cut feature

## Constraints
- Shapes stay in-process (no file export here — C02 owns tessellation, C11 owns persistence).
- Do NOT edit `engine/doc/__init__.py`, `engine/worker.py`, `engine/kernel.py` — report needed
  exports/registrations instead.
- Multi-body is real: `bodies` is a list; primitives with `merge:false` start new bodies.

## Verification
`& $py -m engine.doc.test_compile` green, AND `& $py -m engine.doc.test_schema_fixtures` still
green, AND `& $py -m engine.test_kernel_aliases` still green.
