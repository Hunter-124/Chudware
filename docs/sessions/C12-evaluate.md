# C12 — Evaluate compute layer

Model: **sonnet** · Wave 1 · Depends: C02 (scalar_chunk writer) · Follow
`docs/sessions/README.md` conventions.

## Mission
`engine/doc/evaluate.py`: the math behind the Evaluate tab — mass properties, measure, section
properties, check, symmetry, draft/thickness/curvature per-vertex fields emitted as AICM SCLR.

## Read first
1. `docs/contracts/worker_rpc.md` §2 Evaluate (result shapes per method)
2. `docs/contracts/mesh_format.md` SCLR chunk; `engine/doc/tessellate.py` (C02 helpers)
3. `docs/PLAN.md` §5-M5 (analytic verification fixtures)
4. `engine/kernel.py` `_fit_report` (interference port), `_face_census`

## Owned paths
`engine/doc/evaluate.py` · `engine/doc/test_evaluate.py`

## Deliverables (each a pure function over build123d/OCP shapes + optional tessellation)
1. `mass_props(shape, density_g_cm3)` → volume_mm3, mass_g, com, inertia tensor (BRepGProp).
2. `measure(infos: list)` — given 1–2 resolved TopoInfo/shape elements: edge length/radius,
   face area, point-point / face-face min distance (`BRepExtrema_DistShapeShape`),
   line-line / plane-plane angle. Return the union shape from the contract.
3. `section_props(shape, plane)` — `BRepAlgoAPI_Section` + planar face GProps (area, centroid,
   2nd moments).
4. `interference(bodies)` — port `_fit_report` pairwise logic to a body list.
5. `check(shape)` — `BRepCheck_Analyzer` + free-edge census + tiny-edge/sliver-face census
   (`geometry_analysis` shares this) → issues list per contract.
6. `symmetry(shape, plane)` — mirror, boolean diff both ways, `{symmetric, deviation_mm3}`.
7. Per-vertex SCLR fields over a C02 `BodyTess` (use its UV/topology access or recompute per
   face): `draft_field(tess, shape, pull_dir, angle_deg)` (angle between vertex normal and
   pull dir → signed degrees vs threshold), `thickness_field(tess, shape)` (inward ray cast
   via trimesh on the tessellation; ray from vertex along −normal, distance to next hit),
   `curvature_field(tess, shape, kind)` (exact: `BRepLProp_SLProps` at each vertex's UV on its
   owning face — C02 stores per-face vertex ranges; if UV nodes aren't retained, evaluate via
   `ShapeAnalysis_Surface.ValueOfUV` projection — note which path you used). Each returns
   values + (vmin, vmax) and a ready SCLR chunk via C02's `scalar_chunk`.
8. `test_evaluate.py` — analytic gates: 20mm steel cube mass; cylinder r=5 curvature max
   |κ|=0.2±2%; 10mm-wall hollow cube thickness ≈10 on outer faces (median ±10%); draft: box
   side faces = 0° draft vs +Z pull, 5°-tapered prism (loft or script feature) reports ≈5°;
   symmetric part true / asymmetric false; interference of two overlapping boxes = exact
   overlap volume; measure distance between parallel faces.

## Constraints
- No worker wiring (a later card registers eval_* methods — list the handler signatures you
  recommend in your report). No GUI.

## Verification
`& $py -m engine.doc.test_evaluate` green; C02's suite still green.
