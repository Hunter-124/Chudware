# C07 — CFD: D3Q19 lattice-Boltzmann wind tunnel

Model: **sonnet** · Wave 0 · Depends: none · Follow `docs/sessions/README.md` conventions.

## Mission
`engine/sim/lbm.py`: a qualitative voxel wind tunnel in pure numpy. Honest, stable, bounded
runtime. Returns numpy results (npz); AICM/GUI packaging is a later card — keep this engine-pure.

## Read first
1. `docs/PLAN.md` §3.1 CFD bullet and §5-M7 (guards, presets, outputs, verification bands)
2. `docs/contracts/worker_rpc.md` §3 `sim_run_flow` (the result fields you must produce)
3. `docs/contracts/events.md` `sim_progress` (your `on_progress` callback payloads)

## Owned paths
`engine/sim/lbm.py` · `engine/sim/test_lbm.py`

## Deliverables
1. `run_wind_tunnel(stl_path, direction=(1,0,0), speed_mps=10.0, preset="coarse",
   on_progress=None, max_steps=None) -> FlowResult`:
   - Voxelize via `trimesh.voxel.creation.voxelize` (solid fill: `.fill()`); domain = part
     bbox padded ~2× upstream/lateral, ~4× downstream along flow; presets coarse/medium/fine/
     xfine = 48/64/96/128 cells on the longest axis.
   - D3Q19 BGK with Smagorinsky LES (Cs≈0.17); half-way bounce-back on solid cells; inlet
     velocity BC; outflow (zero-gradient) at exit; periodic or slip lateral walls (choose,
     document).
   - Lattice velocity scaled so u_lattice ≤ 0.1 (Mach guard); tau from a target Reynolds
     proxy, clamped ≥ 0.51; float32 throughout, two-population ping-pong, fully vectorized
     (no per-cell Python loops in the step).
   - Watchdog: NaN/divergence check every ~50 steps → halve relaxation toward tau=1 and
     continue; second failure → abort with partial results + `diverged=True`.
   - Convergence: run until drag varies < 2% over the trailing 20% of steps, or step cap
     (preset-scaled, e.g. 1500 coarse). `on_progress({pct, text, trace_point:{step, drag}})`.
   - Outputs (`FlowResult` + `save_npz(path)`): `drag_n`, `lift_n` (momentum exchange on solid
     boundary links, converted to physical N), `cd` (vs frontal area projected along flow),
     `frontal_area_mm2`, `reynolds_note` (string: lattice Re + "qualitative" disclaimer),
     `trace` (list of (step, drag)), 3 velocity-magnitude slices (mid-planes normal to x/y/z,
     2D arrays + their world transforms), streamlines (RK2 advection from an inlet seed grid,
     ~40 lines, arrays of points + speed per point), `diverged: bool`.
2. `test_lbm.py` (keep TOTAL runtime under ~4 min):
   - sphere (`trimesh.creation.icosphere(2, radius=20)` exported to a temp STL) at coarse,
     capped steps: finishes, `diverged False`, no NaNs, `0.15 < cd < 1.2` (wide qualitative
     band at coarse res), drag > 0
   - flat plate (thin box 40×40×2 normal to flow) drag > sphere drag at same preset/speed
   - stability: 400 extra steps on the sphere → still finite
   - determinism: same inputs → identical drag (no RNG, or seeded)

## Constraints
- numpy + trimesh only (no numba — note in report if you think it's needed later).
- Physical-unit conversions documented in the module docstring (lattice→SI scaling).
- Memory guard: refuse presets whose domain would exceed ~2 GB with a clear error.

## Verification
`& $py -m engine.sim.test_lbm` green; report the sphere Cd you got and the runtime per preset.
