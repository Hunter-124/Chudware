# 3D Sketcher — Design & Phased Build Plan (proposal for sign-off)

**Task:** T-BL45 · **Author:** opus-corm (autopilot) · **Date:** 2026-07-04
**Status:** PROPOSAL — awaiting Hunter's sign-off. Not a frozen contract.
**Precedent:** T-R1 (PLM contract freeze) — design-only task, deliverable is this doc + deferred build tasks.

> This document *proposes* a schema and build plan for a 3D sketcher. It does **not**
> edit `contracts/doc_schema.md` or any other contract — those are append/create-only and
> the actual schema addition lands at build time under advisor approval. Where a decision is
> genuinely Hunter's to make, it is called out in a **DECISION-NEEDED** block; the rest of the
> plan proceeds around it.

---

## 0. Why a 3D sketcher

Direction from Hunter (2026-07-03, re-confirmed 2026-07-04 as a top priority): add a **3D
sketcher** so the modeler can draw points/lines/arcs/splines free in space (not locked to a
plane). This is the foundation for advanced features:

- **3D sweep paths** — pipe/tube/wire runs that turn corners out of a single plane.
- **Space frames / weldments** — 3D strut skeletons.
- **Wire & harness routing** — cable paths through an assembly.
- **Complex lofts** — guide curves and 3D section placement.

The 2D sketcher (plane-locked, `type:"sketch"`, `refs.plane`) stays exactly as it is. The 3D
sketcher is an **additive, sibling** capability.

---

## 1. OSS-first survey (per OSS-first directive)

We surveyed existing 3D geometric constraint solvers / sketchers. **Bottom line: there is no
mature, permissively-licensed, in-process-linkable 3D geometric constraint solver.** The mature
3D solver (SolveSpace) is GPLv3; the permissive solvers are 2D-only or experimental.

| Project | Approach | 3D? | License | In-process link? | Fit |
|---|---|---|---|---|---|
| **SolveSpace** (`libslvs`) | Modified Newton's method, constraints as symbolic-algebra equations, least-squares min of residuals via Jacobian; entities live in **full 3D** with optional **workplane projection** of a constraint | **Yes (native 3D)** | **GPLv3** | **No** — GPL forbids linking into closed-source; commercial relicense on request | Best 3D reference *design*, but license blocks in-process use. Subprocess or clean-room only. |
| **planegcs** (FreeCAD GCS) | Numeric opt (DogLeg / Levenberg–Marquardt / BFGS / SQP); mature, full 2D relation set | **No — 2D by design** ("plane gcs"; 3D was discussed and rejected) | **LGPL-2.1+** | Yes (dynamic link) | The only *linkable* full solver, but **2D-only** — cannot solve free-space 3D constraints. Not applicable to a 3D sketcher. |
| **KittyCAD/ezpz** (Zoo) | Iterative numeric solver, Rust | **No — 2D only** | **MIT** | Yes | Permissive + modern, but 2D-only and experimental. Not a 3D fit. |
| **Geoplexity/Frontier** | Frontier-Vertex DOF-graph decomposition + Maple algebraic/numeric backend, JNI | Partial 3D (old, Java3D-broken) | **GPL-3.0** | No | GPL + Maple dependency + dated/broken. Unusable. |
| **Ceres Solver** (Google) | General non-linear least-squares with **autodiff**; not a geometric solver, a numeric backbone | n/a (dim-agnostic NLLS) | **BSD-3** | Yes | Permissive; could back the 3D NLLS core **if** we choose autodiff over analytic Jacobians. See §3 + DECISION-NEEDED-A. |

### Survey conclusion

1. **Every mature 3D constraint solver in the wild is GPL** (SolveSpace, Frontier). GPL forbids
   in-process linking into our commercial closed-source product (CONTRIBUTING.md guardrail:
   GPL = subprocess-only, AGPL = never).
2. **Every permissively-licensed geometric solver is 2D-only** (planegcs LGPL, ezpz MIT).
3. We **already own a clean-room 2D geometric constraint solver**: `core/sketch/sketch.{h,cpp}`
   is a hand-authored C++ port of `engine/doc/sketch_solver.py` — a Newton loop over Eigen with
   analytic Jacobians, residual blocks per constraint, warm-start + drag support. This is
   *precisely* the same architecture SolveSpace uses (Newton least-squares on residuals with a
   Jacobian), built independently. **Extending our own solver to 3D is the natural path** and
   inherits our existing differential-oracle discipline.

Therefore the recommended solver strategy (§3) is **BUILD-OWN (clean-room, extend the existing
2D solver to 3D)**, not link/wrap an OSS solver.

### Clean-room hygiene protocol (binding for all deferred build tasks)

Because the closest reference (SolveSpace) is GPLv3, the build tasks that descend from this plan
**must** observe clean-room discipline so our 3D solver is defensibly independent:

- **Do NOT read SolveSpace / Frontier / any GPL solver *source*** (e.g. `src/system.cpp`,
  `constrainteq.cpp`) to derive residual or Jacobian formulas.
- Work only from: public *documentation* (solvespace.com tech/constraints/tutorial pages),
  published constraint-type lists, standard geometric-constraint-solving literature
  (e.g. the review papers arXiv:2202.13795, arXiv:1006.1126), and **our own existing 2D solver**.
- Residuals are re-derived from first principles (the geometry of the constraint), not
  transcribed from GPL code. Cite the math source in code comments.
- This protocol is inherited by every T-BL51+ build task below.

---

## 2. Data model — 3D sketch entities

### 2.1 New feature type: `sketch3d` (separate from `sketch`)

**Recommendation: a distinct `type:"sketch3d"` feature, NOT a flag on `sketch`.** Rationale: the
solver math differs (3 coords + free directions vs. 2 plane coords), handle geometry differs, and
there is **no `refs.plane`** — entity coordinates are absolute `[x,y,z]` in the document world
frame (Z-up, matching `doc_schema.md §5`). Overloading `sketch` with a mode flag would fork every
2D solver path with `if(is3d)` branches; a sibling type keeps both clusters clean and lets the 2D
work (T-BL41/42/43/44/48) proceed untouched.

```json
{
  "id": "f7", "type": "sketch3d",
  "params": {
    "entities": [ /* 3D entities, coords in [x,y,z] mm, world frame */ ],
    "constraints": [ /* 3D constraints */ ]
  }
  // NO refs.plane — plane-free by definition.
}
```

The document carries the **last-solved** `[x,y,z]` coordinates (documents open without solving,
same rule as 2D — `doc_schema.md §3.4`).

### 2.2 Entities (Phase 1 minimal; Phase 2+ curves)

| type | fields (world coords, mm) | phase |
|---|---|---|
| `point3d` | `p:[x,y,z]` | 1 |
| `line3d` | `p1:[x,y,z]`, `p2:[x,y,z]` | 1 |
| `arc3d` | `center:[x,y,z]`, `normal:[nx,ny,nz]`, `radius:r`, `start:[x,y,z]`, `end:[x,y,z]` (arc lies in the plane through `center` with the given `normal`; endpoints resolved to that plane) | 2 |
| `spline3d` | `points:[[x,y,z],…]` (cubic through-points, 3D interpolation) | 2 |
| `polyline3d` | `points:[[x,y,z],…]` (chained line3d, convenience) | 2 |

All entities carry `construction:bool` (mirrors 2D). Splines/arcs emit `TopoDS_Edge`s exactly the
way the helix/2D-profile path already does (§4).

### 2.3 Handles

Extend the 2D handle grammar to 3D entities. Handles reference sub-points that are now 3-vectors:

- `"e3"` whole entity; `"e3.p"` (point3d); `"e3.p1"/"e3.p2"` (line3d endpoints);
  `"e3.center"/"e3.start"/"e3.end"` (arc3d); `"e3.p0"…"e3.pN"` (spline3d/polyline3d points).
- Grammar (proposed): `^e\d+(\.(p|p1|p2|center|start|end|p\d+))?$` — same shape as 2D, values
  are 3-vectors.

### 2.4 Persistence & coexistence

- `sketch3d` features live in the same `document.features` list as `sketch` features and every
  other feature — no new top-level container. They serialize through the existing feature
  save/open path (`core/doc/document.{h,cpp}`), just with a new `type` string.
- The GSL / describe layer (`services/mcp/gsl_tools.py`) gains a `describe_sketch3d` sibling
  (Phase 4) so text-only agents can read 3D sketches.

---

## 3. Constraint solver approach

### 3.1 Strategy: BUILD-OWN, clean-room, extend the existing 2D Newton solver to 3D

The existing `aicad::sketch::solve_sketch` (`core/sketch/sketch.h`) parameterizes entities into a
variable vector, wires each constraint to residual blocks with **analytic Jacobians**, and runs a
Newton loop over **Eigen**, with warm-start + constrained-drag support. The 3D solver
(`aicad::sketch3d::solve_sketch3d`, new module `core/sketch3d/`) mirrors this pattern exactly, in
3-space:

- Each point contributes 3 variables `(x,y,z)` instead of 2.
- Directions/normals for `line3d`/`arc3d` are additional variables (unit-vector or, for arc
  planes, a normal). Rotational DOF where needed is carried the way SolveSpace uses quaternions
  for 3D orientation — **we re-derive our own minimal parameterization** (unit-vector residual +
  normalization constraint) clean-room; no GPL code consulted.
- Residual + analytic-Jacobian blocks per 3D constraint type (§3.2).
- Same Newton loop, DOF analysis (`under`/`full`/`over`), redundant/conflicting detection, and
  warm-start/drag machinery, generalized to 3-vectors.

**Why not link/wrap OSS:** planegcs (the only linkable full solver) is 2D-only; SolveSpace (the
only 3D solver) is GPLv3. Wrapping SolveSpace as a **subprocess** is technically possible but
carries three costs that tip the decision to build-own:

1. **Interactive-drag latency.** 3D dragging (§5) needs a solve per mouse-move frame. A subprocess
   means an IPC round-trip per frame → sluggish drag. In-process solving is required for the UX we
   want.
2. **Installer source-offer obligation.** GPLv3-via-subprocess still requires offering the
   SolveSpace source in the installer (CONTRIBUTING.md "must offer source in installer").
3. **License-debt row.** Any GPL subprocess is a `contracts/license_debt.md` entry that must be
   resolved before SaaS.

Build-own has none of these and reuses code we already trust. **See DECISION-NEEDED-B** — the
build-vs-subprocess call is foundational and is Hunter's to confirm.

### 3.2 3D constraint types (proposed)

Phase-1 minimal set (enough for space-frame skeletons and simple 3D paths), then expand:

| constraint | refs | phase | notes |
|---|---|---|---|
| `coincident3d` | 2 points | 1 | Δ(x,y,z)=0 |
| `fix3d` | 1 handle | 1 | pin a point in space |
| `distance3d` | 2 points, or point+line3d | 1 | Euclidean gap (mm) |
| `parallel3d` | 2 line3d | 2 | direction cross-product = 0 |
| `perpendicular3d` | 2 line3d | 2 | direction dot = 0 |
| `angle3d` | 2 line3d | 2 | dihedral/between-vectors angle (deg) |
| `point_on3d` | point + line3d/arc3d/spline3d | 2 | closest-point projection residual (mirrors 2D `point_on`) |
| `on_plane` | point/entity + datum plane | 2 | pin an entity into a reference plane (the bridge to 2D-style planar sub-constraints) |
| `horizontal3d`/`vertical3d` | line3d + axis/plane | 3 | axis-aligned, needs a reference direction |
| `tangent3d` | curve pair | 3 | deferred, harder in 3D |
| `symmetric3d` | 2 entities + plane | 3 | mirror across a datum plane |

`on_plane` is the key bridge: it lets a 3D sketch borrow the well-tested 2D relation set *within*
a chosen plane (SolveSpace's "workplane projection" idea, re-derived clean-room) without forcing
the whole sketch onto a plane.

### 3.3 Numeric backbone — analytic Jacobians (Ceres optional, not baseline)

Baseline: **hand-derived analytic Jacobians over Eigen**, matching the 2D solver's discipline.
**Ceres Solver (BSD-3) is explicitly *not* the baseline** — autodiff would break the
differential-oracle parity (see §3.4 / DECISION-NEEDED-A) and add a dependency the 2D path never
needed. Ceres is noted only as a **future robustness fallback** if analytic Jacobians prove
fragile on hard 3D systems; adopting it would be its own scoped task with a license row
(BSD-3 = OK-to-bundle, no debt).

### 3.4 DECISION-NEEDED-A — the parity oracle for the 3D solver

> The 2D C++ solver is developed against `engine/doc/sketch_solver.py` as a **differential
> oracle** — the C++ matches the Python step-for-step, which is why it uses a hand-rolled Newton
> loop + analytic Jacobians rather than a black-box optimizer. **There is no Python 3D solver
> today**, so the 3D build has no oracle out of the box. Two options:
>
> - **(A1) Mirror the 2D discipline:** write a Python `sketch3d_solver.py` reference *first*, then
>   port to C++ against it (differential harness). Slower, but keeps the whole engine's parity
>   invariant intact and gives a second implementation to catch bugs. **(recommended)**
> - **(A2) C++-first:** write the 3D solver directly in C++ with a self-contained test suite
>   (analytic closed-form cases + DOF assertions), and *breaks* the "Python is the oracle"
>   pattern for this subsystem.
>
> **Recommendation: A1.** This is foundational and Hunter steers it — please confirm A1 vs A2.

---

## 4. Integration plan

### 4.1 Feeding features — mostly already wired (major de-risk)

The sweep feature **already accepts a 3D edge as its path** and resolves it geometrically — no
compiler change is needed for the primary use case:

- `feature_params.md` sweep: `refs.path: sel(edge, region-chain)` — accepts **any model edge**
  (not confined to a sketch plane) OR a sketch wire chain.
- The selector resolves `kind:"edge"` by geometric fingerprint (centroid, axis, length, surface
  type) — `selectors.md §1` — with **no plane-of-origin distinction**. A 3D-sketch edge is picked
  the same way a helix edge is.
- The compiler `sweep_wire_solid(profile, spine, orientation)` (`core/geom/features.cpp`
  ~L364) feeds the spine wire straight into `BRepOffsetAPI_MakePipeShell` — it already accepts an
  arbitrary `TopoDS_Wire` spine.
- **Precedent: the `helix` feature** (`feature_params.md`) emits an **edge body pickable as a
  sweep path** (built via `make_helix` → `TopoDS_Wire` in `core/geom/curves.cpp`). A 3D sketch is
  the general case of the same idea: emit a `TopoDS_Wire`/edge body from solved 3D entities via
  the *same* edge-construction primitives `profile.cpp::build_edge` already uses (line →
  `BRepBuilderAPI_MakeEdge`, arc → `GC_MakeArcOfCircle`, spline → `GeomAPI_Interpolate`), just
  built directly in 3D rather than lifted from a plane.

**Consequence:** the integration work is mostly "make `sketch3d` emit an edge body," after which
sweep/loft guide-curves consume it through existing machinery. Loft guide-curves and section
placement are a Phase-3 extension.

### 4.2 Coexistence with the 2D sketch cluster (T-BL41/42/43/44/48)

- The 2D cluster is in flight **today** (T-BL43 external refs, T-BL48 sketch-entry wiring,
  T-BL41/42/44). The `sketch3d` type touches **none** of their files — separate feature type,
  separate solver module (`core/sketch3d/`), separate UI entry. No write-set overlap.
- Shared, append-only surfaces (`contracts/doc_schema.md`, `core/api/libaicad.h`) are extended in
  dedicated commits at build time (per CONTRIBUTING), not now.
- The `on_plane` constraint (§3.2) lets a 3D sketch reuse 2D relation semantics inside a plane,
  giving a smooth conceptual bridge without code coupling.

### 4.3 Viewport / UX implications

- **3D dragging:** dragging a 3D handle needs a projection choice (screen-ray → which DOF moves).
  Proposed: drag moves the handle in the **camera-facing plane** through the handle by default,
  with axis/plane locks (X/Y/Z or a datum plane) via modifier keys. This reuses the 2D
  constrained-drag solver (`sketch.h::drag`) generalized to 3-vectors (soft-target at weight 0.01,
  time-budgeted). **Requires in-process solving** (reason build-own beats subprocess, §3.1).
- **Plane-free constraints:** no active sketch plane, so the toolbar exposes 3D constraint verbs
  (parallel/perp/coincident/on-plane) directly. Reference geometry (datum planes/axes) is pickable
  as constraint targets.
- **Entry point:** a `sketch3d` mode mirrors the 2D `sketch_mode` (`host/src/ui/sketch_mode.cpp`),
  gated behind the same sketch-entry wiring T-BL48 is standing up (note:
  `uiimgui-sketcher-entry-unwired` — interactive sketch dispatch in `ui/imgui` is still being
  wired; 3D UX depends on that landing first). Phase 4 work.

---

## 5. Phased build plan (each phase = one deferred task, greenlit individually)

Every phase ships its `contracts/` update the same day the module lands (CONTRIBUTING "no module
without a contract"), observes the §1 clean-room protocol, and passes `scripts/gate.sh`.

| Phase | Deferred task | Scope | Acceptance |
|---|---|---|---|
| **P1** | **T-BL51** | `sketch3d` data model + core solver skeleton: `point3d`/`line3d`, `coincident3d`/`fix3d`/`distance3d`; Newton loop in `core/sketch3d/`, DOF analysis. Schema addition to `doc_schema.md`. Per DECISION-NEEDED-A, may include the Python `sketch3d_solver.py` oracle. | A 3D sketch of points+lines with distance/coincident/fix constraints solves to `full` DOF; ctest parity (vs Python oracle if A1) green; document round-trips save/open. |
| **P2** | **T-BL52** | Curves + relational constraints: `arc3d`/`spline3d`/`polyline3d`; `parallel3d`/`perpendicular3d`/`angle3d`/`point_on3d`/`on_plane`. Emit `TopoDS_Wire`/edge body from solved entities. | 3D curves solve + emit valid OCCT edges; `on_plane` reproduces a planar constraint result; edge body is selectable. |
| **P3** | **T-BL53** | Feature integration: 3D-sketch edge body consumed as `sweep` path (verify existing `sel(edge,…)` path); loft guide-curves + 3D section placement. | A 3D sketch path drives a sweep end-to-end via MCP/engine (pipe around a 3D bend); loft with a 3D guide builds; `verify_battery` PASS. |
| **P4** | **T-BL54** | GUI + GSL: `sketch3d` viewport mode (3D dragging w/ axis/plane locks), toolbar verbs, `describe_sketch3d`. Depends on T-BL48 sketch-entry wiring. | User draws + drags a 3D sketch in-app; drag stays interactive (in-process solve); text-only describe reads it. |
| **P5** | **T-BL55** | Advanced constraints + robustness: `horizontal3d`/`vertical3d`/`symmetric3d`/`tangent3d`; warm-start/drag hardening; (optional) Ceres fallback eval. | Advanced constraints solve; drag warm-start stable; robustness suite green. |

P1→P5 are strictly ordered (`depends_on`). All start **deferred: yes** and are unparked only on
Hunter's greenlight (`work_set`).

---

## 6. DECISION-NEEDED summary (for Hunter)

- **DECISION-NEEDED-A (§3.4):** 3D-solver parity oracle — write a Python `sketch3d_solver.py`
  reference first (A1, recommended, preserves the differential-oracle invariant) vs. C++-first
  (A2). 
- **DECISION-NEEDED-B (§3.1):** solver sourcing — **build-own clean-room** (recommended;
  in-process, no license debt, reuses our 2D solver) vs. **wrap SolveSpace as a GPL subprocess**
  (adds drag-latency, installer source-offer, and a license-debt row). 
- **DECISION-NEEDED-C (scope/priority):** how far to go now — is P1–P3 (3D paths feeding sweeps,
  the highest-value slice) the near-term target, with P4–P5 later? Confirm the greenlight order.

---

## 7. Follow-on (deferred) tasks cut by this plan

- `work/backlog/T-BL51-sketch3d-core-solver.md` — P1 data model + core solver (deferred)
- `work/backlog/T-BL52-sketch3d-curves.md` — P2 curves + relational constraints (deferred)
- `work/backlog/T-BL53-sketch3d-features.md` — P3 feature integration (sweep/loft) (deferred)
- `work/backlog/T-BL54-sketch3d-gui.md` — P4 GUI + GSL (deferred)
- `work/backlog/T-BL55-sketch3d-advanced.md` — P5 advanced constraints + robustness (deferred)

All reference this document and stay parked (`deferred: yes`) until Hunter greenlights the build.
