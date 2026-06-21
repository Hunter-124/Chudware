# OpenFOAM CFD bridge

The Flow study can run on either of two CFD engines, chosen by the **Solver** dropdown in the
Flow form:

| Solver | Method | Use it for |
|--------|--------|-----------|
| **LBM (fast)** | D3Q19 Lattice-Boltzmann (built in, pure C++/Python) | Quick directional comparisons ("is body A lower-drag than B?"). Seconds to run. |
| **OpenFOAM RANS (accurate)** | Steady incompressible RANS, k-ω SST, body-fitted mesh **with prism boundary layers** | Trustworthy Cd / Cl / drag / lift for engineering decisions. Minutes to run. |

The OpenFOAM option appears only when a working OpenFOAM is detected; otherwise it is greyed out
with a hint to provision one.

### Heat transfer

When the OpenFOAM solver is selected, a **Heat transfer (thermal)** section offers two models:

| Thermal model | Solver | Use it for |
|---------------|--------|-----------|
| **Fluid (surface heat)** | `buoyantSimpleFoam` (steady, compressible buoyant) | A heated/cooled part surface warming the air: surface temperature, the thermal plume, and total wall heat flow. Wall BC = fixed temperature, heat flux (W/m²), or total power (W). |
| **Conjugate (solid + fluid)** | `chtMultiRegionFoam` (CHT) | The part *itself* conducts heat from an internal dissipation while the airflow cools it — electronics / heat-sink cooling. Inputs: solid thermal conductivity (W/mK) and dissipated power (W). Meshes a solid region + fluid region from the one part STL; takes a few minutes. |

CHT note: the part STL must be watertight (a closed solid). The steady solid temperature is
independent of the solid's thermal *mass*, so the bridge runs the transient solver with a low
artificial rho·Cp to reach the same steady answer in a few fluid flow-through times rather than
the solid's real (hours-long) thermal time constant.

## Architecture

```
host Flow study (Solver = OpenFOAM)
  -> sim_run_flow {solver:"openfoam", ...}            (engine/sim/worker.py)
       -> engine/sim/openfoam.py  run_openfoam_tunnel  (same signature/return as the LBM tunnel)
            ├─ foam_case.py    aero + fluid-thermal case (blockMesh + snappyHexMesh + simpleFoam
            │                  / buoyantSimpleFoam dicts)
            ├─ foam_cht.py     conjugate case (multi-region snappy + splitMeshRegions +
            │                  chtMultiRegionFoam)
            ├─ foam_runner.py  launch utilities (NativeRunner | WslRunner)
            └─ foam_post.py    coefficient.dat -> Cd/Cl; pyvista -> Cp / temperature + streamlines
```

`run_openfoam_tunnel` returns the **same `FlowResult`** as the LBM solver, so the host, the
`studies` persistence, and the viewport overlay (pressure_cp SCLR + streamline LINS) are reused
unchanged. Only `foam_runner.py` knows native-vs-WSL.

## Provisioning

OpenFOAM is not bundled. Two ways to get it (the bridge auto-detects either):

* **Native Windows (preferred)** — install [blueCFD-Core](https://bluecfd.github.io/) (an
  ESI/MinGW build that keeps the classic `simpleFoam`), then either set `%AICAD_OPENFOAM%` to its
  root or drop it in `third_party/openfoam/`.
* **WSL2 (scriptable fallback)** —
  ```
  python -m tools.provision_openfoam --install
  ```
  adds the OpenFOAM.com apt repo and installs `openfoam2412-default` into your default WSL distro
  (needs passwordless sudo). Case directories live on the Windows filesystem and are reached from
  Linux under `/mnt/...`.

Check what's detected:
```
python -m tools.provision_openfoam        # prints kind (native|wsl) + version, or what's missing
```

## Expected accuracy (read before trusting a number)

Steady RANS is accurate for **attached / mildly-separated** flows (streamlined bodies, airfoils
near cruise, ducts) and progressively less accurate as separation grows. Validate with:

```
python -m tools.foam_validation --preset medium     # sphere + cube vs literature
python -m tools.foam_validation --preset fine        # finer mesh, slower
```

Current canonical results (Re ~ 1e4):

| Case | Steady-RANS Cd | Experimental Cd | Verdict |
|------|----------------|-----------------|---------|
| Sphere | ~0.35 | ~0.47 | Slightly low — RANS under-resolves the separated wake. Good enough for ranking. |
| Cube (face-on) | ~1.7 | ~1.05 | **Over-predicted.** The real wake sheds vortices; steady RANS time-averages to too-low a base pressure. Use an unsteady/LES run for sharp bluff bodies. |

Takeaways:
* Trust **trends and streamlined-body coefficients** strongly.
* Treat **sharp bluff-body absolute drag** as an upper bound from this steady solver.
* Refine the **preset** (coarse→xfine = finer surface refinement + more prism layers + more
  iterations) when you need the mesh-converged answer.

## Mesh & domain

* Domain padding mirrors the LBM wind tunnel: ~1× longest-axis upstream, 3× downstream, 1×
  lateral, with the part rotated so the flow runs along +X (inlet −X, outlet +X, slip sides).
* `snappyHexMesh` adds `nSurfaceLayers` prism layers at the wall (1→4 by preset) — this near-wall
  resolution is the main accuracy gain over the uniform LBM lattice.
* Reference area for Cd/Cl is the projected frontal area (rasterised silhouette) unless the study
  overrides it.
