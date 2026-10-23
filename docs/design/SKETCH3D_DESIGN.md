# 3D Sketcher — Solver Strategy and Design Rationale

**Status:** design rationale (2026-07-04), implemented. The shipped 3D sketcher is a
`sketch3d` feature solved in-process by `core/sketch/sketch3d.{h,cpp}` on the shared
`core/solver/nsolve.h` residual/Jacobian framework, with 3D wire chaining in
`core/geom/profile3d.{h,cpp}` and the interactive mode in `host/src/ui/sketch3d_mode.{h,cpp}`.

The normative schema and RPC surface are
[`contracts/feature_params.md` §sketch3d](../../contracts/feature_params.md),
`contracts/doc_schema.md` §3B and
[`contracts/sketch3d_gui_contract.md`](../../contracts/sketch3d_gui_contract.md). This
document records *why* the sketcher is built the way it is: the solver survey, the
license-isolation reasoning, the clean-room protocol and the integration approach. Where
the original proposal and the contracts differ (entity names such as `point`/`line`/`arc`
instead of `point3d`/`line3d`/`arc3d`, three-point arcs instead of center/normal/radius, the
module living in `core/sketch/` instead of a separate `core/sketch3d/` tree, and the full
v2 relation set), the contracts are authoritative.

---

## 0. Why a 3D sketcher

A 3D sketcher lets the modeler draw points, lines, arcs and splines free in space, not
locked to a plane. It is the foundation for:

- **3D sweep paths** — pipe/tube/wire runs that turn corners out of a single plane.
- **Space frames / weldments** — 3D strut skeletons.
- **Wire & harness routing** — cable paths through an assembly.
- **Complex lofts** — guide curves and 3D section placement.

The 2D sketcher (plane-locked, `type:"sketch"`, `refs.plane`) is unchanged. The 3D
sketcher is an **additive, sibling** capability.

---

## 1. OSS-first survey

Existing 3D geometric constraint solvers and sketchers were surveyed first. **Bottom line:
there is no mature, permissively licensed, in-process-linkable 3D geometric constraint
solver.** The mature 3D solver (SolveSpace) is GPLv3; the permissive solvers are 2D-only or
experimental.

| Project | Approach | 3D? | License | In-process link? | Fit |
|---|---|---|---|---|---|
| **SolveSpace** (`libslvs`) | Modified Newton's method, constraints as symbolic-algebra equations, least-squares minimization of residuals via Jacobian; entities live in **full 3D** with optional **workplane projection** of a constraint | **Yes (native 3D)** | **GPLv3** | **No** — the project's license-isolation policy keeps copyleft code out-of-process ([`contracts/licenses.md`](../../contracts/licenses.md)) | Best 3D reference *design*, but the policy rules out in-process use. Subprocess or clean-room only. |
| **planegcs** (FreeCAD GCS) | Numeric optimization (DogLeg / Levenberg–Marquardt / BFGS / SQP); mature, full 2D relation set | **No — 2D by design** ("plane gcs"; 3D was discussed and rejected upstream) | **LGPL-2.1+** | Yes (dynamic link) | The only *linkable* full solver, but **2D-only** — cannot solve free-space 3D constraints. |
| **KittyCAD/ezpz** (Zoo) | Iterative numeric solver, Rust | **No — 2D only** | **MIT** | Yes | Permissive and modern, but 2D-only and experimental. |
| **Geoplexity/Frontier** | Frontier-Vertex DOF-graph decomposition + Maple algebraic/numeric backend, JNI | Partial 3D (old, Java3D-broken) | **GPL-3.0** | No | GPL + Maple dependency + dated/broken. Unusable. |
| **Ceres Solver** (Google) | General non-linear least squares with **autodiff**; a numeric backbone, not a geometric solver | n/a (dimension-agnostic NLLS) | **BSD-3** | Yes | Permissive; could back a 3D NLLS core if autodiff were preferred over analytic Jacobians (§3.3). |

### Survey conclusion

1. **Every mature 3D constraint solver found is GPL** (SolveSpace, Frontier). Under the
   project's license-isolation policy, copyleft code stays out-of-process: GPL tools are
   subprocess-only and AGPL is not used (`contracts/licenses.md`, CONTRIBUTING.md).
2. **Every permissively licensed geometric solver is 2D-only** (planegcs LGPL, ezpz MIT).
3. Chudware **already has a clean-room 2D geometric constraint solver**
   (`core/sketch/sketch.{h,cpp}`): a Newton loop over Eigen with analytic Jacobians,
   residual blocks per constraint, warm-start and drag support. This is the same
   architecture SolveSpace uses (Newton least squares on residuals with a Jacobian), built
   independently. **Extending it to 3D is the natural path.**

The chosen strategy (§3) is therefore **build-own (clean-room, extend the existing solver
framework to 3D)**, not link or wrap an OSS solver.

### Clean-room hygiene protocol

Because the closest reference (SolveSpace) is GPLv3, all 3D-sketcher work observes
clean-room discipline so the solver is defensibly independent:

- **Do not read SolveSpace / Frontier / any GPL solver *source*** (e.g. `src/system.cpp`,
  `constrainteq.cpp`) to derive residual or Jacobian formulas.
- Work only from public *documentation* (solvespace.com tech/constraints/tutorial pages),
  published constraint-type lists, standard geometric-constraint-solving literature (e.g. the
  review papers arXiv:2202.13795, arXiv:1006.1126), and Chudware's own 2D solver.
- Residuals are re-derived from first principles (the geometry of the constraint), not
  transcribed from GPL code. Cite the math source in code comments.

---

## 2. Data model decisions

### 2.1 A separate feature type: `sketch3d`

**A distinct `type:"sketch3d"` feature, not a flag on `sketch`.** The solver math differs
(three coordinates and free directions vs. two plane coordinates), handle geometry differs,
and there is **no `refs.plane`** — entity coordinates are absolute `[x,y,z]` in the document
world frame (Z-up, matching `doc_schema.md §5`). Overloading `sketch` with a mode flag would
fork every 2D solver path with `if(is3d)` branches; a sibling type keeps both clean.

The document carries the **last-solved** `[x,y,z]` coordinates, so documents open without
solving — the same rule as 2D (`doc_schema.md §3.4`).

### 2.2 Persistence and coexistence

- `sketch3d` features live in the same `document.features` list as every other feature —
  no new top-level container — and serialize through the existing feature save/open path
  (`core/doc/document.{h,cpp}`) with a new `type` string.
- A `sketch3d` never produces a body. It compiles to ordered 3D wire chains that downstream
  features reference.
- Handles extend the 2D handle grammar to 3-vectors (`"e3"`, `"e3.p"`, `"e3.p1"`/`"e3.p2"`,
  `"e3.p0"…"e3.pN"`); the exact grammar is in the GUI/RPC contract.

---

## 3. Constraint solver approach

### 3.1 Strategy: build-own, clean-room, in-process

The 2D solver parameterizes entities into a variable vector, wires each constraint to
residual blocks with **analytic Jacobians**, and runs a Newton loop over **Eigen** with
warm-start and constrained-drag support. The 3D solver follows the same pattern in
3-space on the shared `nsolve` framework:

- Each point contributes three variables `(x,y,z)` instead of two.
- Residual + analytic-Jacobian blocks per 3D constraint type.
- The same DOF analysis (`under`/`full`/`over`), redundant/conflicting detection, and
  warm-start/drag machinery, generalized to 3-vectors.

**Why not link or wrap OSS:** planegcs (the only linkable full solver) is 2D-only;
SolveSpace (the only 3D solver) is GPLv3. Wrapping SolveSpace as a **subprocess** is
technically possible but carries three costs that favor build-own:

1. **Interactive-drag latency.** 3D dragging needs a solve per mouse-move frame. A
   subprocess means an IPC round-trip per frame, which makes dragging sluggish. In-process
   solving is required for the intended UX.
2. **Source-offer obligation.** A GPLv3 tool distributed with the installer, even as a
   subprocess, brings a source-offer obligation.
3. **License-debt entry.** Any GPL subprocess becomes a `contracts/license_debt.md` row.

Build-own has none of these costs and reuses code the project already trusts.

### 3.2 Constraint vocabulary

The minimal first set was coincident, fix and distance (enough for space-frame skeletons
and simple 3D paths), expanded with parallel, perpendicular, angle, point-on-curve,
on-plane, axis-aligned, tangent and symmetric relations. The shipped relation set is
listed in `core/sketch/sketch3d.h` and the GUI/RPC contract.

`on_plane` is the key bridge: it lets a 3D sketch borrow the well-tested 2D relation
semantics *within* a chosen plane (the "workplane projection" idea, re-derived clean-room)
without forcing the whole sketch onto a plane.

### 3.3 Numeric backbone — analytic Jacobians

The baseline is **hand-derived analytic Jacobians over Eigen**, matching the 2D solver's
discipline. Ceres Solver (BSD-3) is not used; it remains only a possible future robustness
fallback if analytic Jacobians prove fragile on hard 3D systems, which would be its own
scoped change with a license row (BSD-3 is compatible with bundling).

### 3.4 Verification oracle

The original 2D solver was developed against a Python reference solver as a differential
oracle. No Python 3D solver existed, so two options were weighed: write a Python reference
first and port against it, or write the 3D solver directly in C++ with a self-contained
suite of analytic closed-form cases and DOF assertions. The implementation took the
C++-first route: `core/tests/test_sketch3d.cpp` (solver) and
`core/tests/test_sketch3d_doc.cpp` (document compile, sweep consumption, RPC round-trip).

---

## 4. Integration

### 4.1 Feeding features

The sweep feature already accepted an arbitrary spine wire, so integration is mostly
"make `sketch3d` emit wire chains":

- The compiler `sweep_wire_solid(profile, spine, orientation)` (`core/geom/features.cpp`)
  feeds the spine wire straight into `BRepOffsetAPI_MakePipeShell`, which accepts any
  `TopoDS_Wire`.
- **Precedent: the `helix` feature** emits an edge body usable as a sweep path (built via
  `make_helix` → `TopoDS_Wire` in `core/geom/curves.cpp`). A 3D sketch is the general case:
  solved entities become edges through the same OCCT primitives the 2D profile path uses
  (line → `BRepBuilderAPI_MakeEdge`, arc → `GC_MakeArcOfCircle`, spline →
  `GeomAPI_Interpolate`), built directly in 3D rather than lifted from a plane.
- `sweep` consumes a `sketch3d` chain as `path` or as a guide; loft guides are reserved
  (see `contracts/feature_params.md`).

### 4.2 Coexistence with the 2D sketcher

The `sketch3d` type is a separate feature type with its own solver entry point and UI mode,
so it touches none of the 2D sketch code paths. Shared, append-only surfaces
(`contracts/doc_schema.md`, `core/api/libchudware.h`) were extended in dedicated changes.

### 4.3 Viewport / UX

- **3D dragging:** dragging a 3D handle needs a projection choice (screen ray → which DOF
  moves). The design moves the handle in a working plane through the handle, with
  axis/plane locks, reusing the 2D constrained-drag approach (soft target, time-budgeted)
  generalized to 3-vectors. This is why in-process solving is required (§3.1). The shipped
  working-plane scheme is specified in the GUI/RPC contract §8.
- **Plane-free constraints:** with no active sketch plane, the toolbar exposes 3D
  constraint verbs directly, and reference geometry (datum planes/axes) is pickable as
  constraint targets.
- **Entry point:** a `sketch3d` mode (`host/src/ui/sketch3d_mode.cpp`) mirrors the 2D sketch
  mode (`host/src/ui/sketch_mode.cpp`).
