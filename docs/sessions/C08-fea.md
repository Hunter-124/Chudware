# C08 — FEA pack: mesher + static + modal + sim worker

Model: **sonnet** · Wave 0 · Depends: wheels preinstalled (gmsh, scikit-fem, meshio, pyamg are
already in the venv — verify with `& $py -c "import gmsh, skfem, meshio"` first). Follow
`docs/sessions/README.md` conventions.

## Mission
Linear-static + modal FEA over gmsh tet meshes, plus the separate sim-worker process that hosts
all simulation methods (crash isolation from the doc worker).

## Read first
1. `contracts/worker_rpc.md` §3 (sim worker methods + result shapes) and §1 (wire format)
2. `contracts/events.md` (`sim_progress`)
3. `engine/worker.py` — copy its stdio JSON-RPC serve loop pattern (methods dict, emit_event)
4. `docs/PLAN.md` §5-M6

## Owned paths
`engine/sim/mesher.py` · `engine/sim/fea_static.py` · `engine/sim/fea_modal.py` ·
`engine/sim/materials.json` · `engine/sim/worker.py` · `engine/sim/test_fea.py`

## Deliverables
1. `materials.json`: steel_1018, alu_6061, abs, pla, petg, nylon_pa12, ti_6al4v — each
   `{name, E_mpa, nu, density_g_cm3, yield_mpa}` (real textbook values).
2. `mesher.py`: `mesh_step(step_path, density: float 0..1) -> MeshResult {points (n,3),
   tets (m,4 or m,10), mesh_path (.msh saved), char_len_mm, n_nodes, n_elems}` via the gmsh
   Python API (initialize/finalize per call, `gmsh.option` CharacteristicLength from bbox ×
   lerp(0.25, 0.05, density)); start with FIRST-ORDER tets (tet4) for tonight, P2 enrichment
   happens inside scikit-fem (ElementTetP1 vs P2 — see below). IMPORTANT: run gmsh in-process
   but guard with try/finally finalize; it is GPL — never import it anywhere except engine/sim.
3. `fea_static.py`: `run_static(mesh: MeshResult, material: dict, fixtures: list[FaceSet],
   loads: list[Load], on_progress) -> StaticResult` where `FaceSet = {points: (k,3) array,
   tol_mm}` (nodes within tol of any point are constrained — the doc worker will hand face
   point clouds later; tests build them geometrically) and `Load = {faceset, type:
   "force"|"pressure", vector or magnitude}`.
   - scikit-fem: `ElementVector(ElementTetP1)` on `MeshTet` (use P2 if it fits runtime; note
     choice), linear elasticity (lambda/mu from E, nu), Dirichlet via `condense`, nodal force
     spreading over the load node set (pressure → area-weighted via face triangles if
     available, else equal split — note it).
   - Solve: `scipy.sparse.linalg.spsolve`; if pyamg present and n_dof > 60k use CG+AMG.
   - Post: per-node displacement magnitude, von Mises (element stress → node-averaged),
     `summary {max_von_mises_mpa, max_disp_mm, min_fos}` (FoS = yield/vm, cap display at 15).
   - `save_npz(path)` with all arrays.
4. `fea_modal.py`: `run_modal(mesh, material, fixtures, n_modes=6)` → frequencies (Hz) via
   `scipy.sparse.linalg.eigsh` on (K, M) shift-invert at sigma=0; mode shapes per node.
5. `worker.py`: stdio JSON-RPC process implementing `ping` (`{pong, protocol: 2, sim: true}`),
   `sim_mesh`, `sim_run_static`, `sim_run_modal`, `sim_run_flow` (import and call
   `engine.sim.lbm.run_wind_tunnel` — it lands in parallel card C07; code the call per its
   card signature and mark the test skipped if the module is absent), `sim_cancel` (sets a
   flag checked by on_progress callbacks), `shutdown`. Stream `sim_progress` events.
6. `test_fea.py` (runtime budget ~3 min):
   - cantilever: 100×10×10 mm steel box via build123d → STEP → mesh at density 0.5 → fix x≈0
     face nodes, tip load 100 N down → tip deflection vs Euler–Bernoulli PL³/3EI within
     **20%** (coarse tet4 is stiff; print actual %, we tighten with P2 later)
   - reaction sanity: max von Mises near the fixed end, not the tip
   - modal: free-free runs; fixed-base first frequency positive and finite
   - worker subprocess: spawn `& $py -m engine.sim.worker`, ping, sim_mesh on the cantilever
     STEP, shutdown — over real pipes (mirror `engine/test_worker_stream.py` style)

## Constraints
- gmsh imports ONLY in engine/sim (license isolation). Do NOT touch requirements.txt or the
  wheelhouse (runtime-stamp implications) — note the new deps in your report instead.

## Verification
`& $py -m engine.sim.test_fea` green; report deflection error %, n_dof, solve times.
