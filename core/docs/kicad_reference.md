# KiCad Reference — Reuse Map for `core/`

Source tree: `kicad/` (KiCad master). Survey date: 2026-06-15.

> **LICENSE — read first.** KiCad's application code and geometry/math library
> (`libs/kimath`) are **GPL-2/3-or-later** (every file header carries a GPL notice) — it is
> NOT BSD. BUT: GPL copyleft is triggered by **distribution**, not by use. GPLv2 §0 / GPLv3 §2
> permit unlimited **private/internal** use and modification with no source-release obligation.
> So:
> - **Internal-only tool (not distributed): we MAY copy / link / vendor KiCad GPL code freely.**
> - **If the program is ever distributed or sold:** the GPL code makes the whole combined work
>   GPL (must offer source under GPL) — at that point we'd have to relicense the product GPL or
>   remove the GPL-derived code. To preserve the option to ship/sell/close-source later, keep the
>   hot geometry clean-room now; otherwise borrow directly.
>
> Project stance (current): internal use — borrowing KiCad geometry is on the table. Revisit if
> distribution becomes a goal. Independent of license, `libs/kimath` is entangled with wxWidgets +
> KiCad-wide headers, so a few primitives are about as quick to reimplement as to extract. KiCad has
> **no parametric constraint solver** — our `nsolve`/sketch solver has no KiCad analog regardless.

## 1. License table (what we may actually use)

| Path | License | Reusable? |
|---|---|---|
| `libs/kimath/**` (all geometry/math) | **GPL-2-or-later** | **No — study only** |
| `libs/core/**`, `libs/sexpr/**` | **GPL** (+ wxWidgets dep) | **No — study only** |
| `eeschema/`, `pcbnew/`, `common/`, `gerbview/`, `kicad/` (app) | **GPL-3-or-later** | **No** |
| `thirdparty/clipper2/` | **Boost 1.0** | **Yes — vendorable** (polygon booleans) |
| `thirdparty/delaunator/` | **MIT** | **Yes — vendorable** (Delaunay triangulation) |
| `thirdparty/tinyspline_lib/` | **MIT** | **Yes** (B-spline / NURBS) |
| `thirdparty/nlohmann_json/`, `fmt/`, `magic_enum/`, `rectpack2d/`, `thread-pool/` | **MIT** | **Yes** (we already use nlohmann/json) |
| `thirdparty/nanosvg/` (ZLib), `gzip-hpp/` (BSD-2), `lemon/` (PD) | permissive | **Yes** |
| `thirdparty/dxflib_qcad/`, `thirdparty/potrace/` | **GPL-2+** | **No** — use an MIT/LGPL DXF lib instead |

## 2. Highest-value algorithm references (clean-room reimplement; all GPL)

1. **`VECTOR2<T>`** — `libs/kimath/include/math/vector2d.h`. Integer 2D vector using `int64_t`
   "extended type" for cross/dot intermediates (overflow-safe integer CAD coords). ~150 lines.
2. **`SEG`** — `libs/kimath/include/geometry/seg.{h,cpp}`. Segment–point distance, segment–segment
   intersection, closest point, side test. The core 2D workhorse. Study & reimplement.
3. **`SHAPE_ARC`** — `libs/kimath/.../shape_arc.{h,cpp}`. Arc (center/start/mid/end/width) +
   tessellation to polyline + collision. The mid-point representation avoids arc ambiguity.
4. **`EDA_ANGLE`** — `libs/kimath/.../eda_angle.{h,cpp}`. Typed angle (deg internal), Normalize()/
   Normalize180()/IsCardinal(). Trivial reimplement.
5. **`CIRCLE::ConstructFromTanTanPt`** — `.../circle.cpp`. Apollonius tangent-circle construction
   (needed for fillets).
6. **`INTERSECTION_VISITOR`** — `.../intersection.{h,cpp}`. `std::variant`+`std::visit` unified
   intersection dispatch across {seg, line, ray, circle, arc, box}. Borrow the *pattern*.
7. **`BEZIER_POLY`** — `.../bezier_curves.{h,cpp}`. de Casteljau Bezier→polyline. ~100 lines.

## 3. Vendorable now (permissive) — when we reach geometry Wave 2

- **Clipper2** (`thirdparty/clipper2/`, Boost) — polygon Union/Subtract/Intersect/Offset; needed
  for sketch region booleans, copper pours, DRC clearance zones. No wx dependency; drop-in.
- **Delaunator** (`thirdparty/delaunator/`, MIT) — fast O(n log n) Delaunay; polygon fill meshing.

**Do not vendor until our own project license posture is confirmed** and we actually need them
(the solver does not). Track in the geometry-wave plan.

## 4. Coordinate / unit notes worth adopting

- KiCad uses **integer nanometers** (IU, `IU_PER_MM = 1e6`) for PCB coords + `int64_t` intermediate
  products — avoids float drift in CAD. Worth considering for our geometry layer (our solver stays
  float64). No dedicated polar class; angle via `EDA_ANGLE`, magnitude via `VECTOR2::EuclideanNorm`.
- `MATRIX3x3<T>` (`include/math/matrix3x3.h`) is the homogeneous 2D affine transform reference.

## 5. ECAD references (Wave 4, all GPL — study only)

- S-expression parser: `libs/sexpr/` (depends on wx; reimplement clean without wx, ~300 lines).
- `.kicad_pcb`/`.kicad_sym` IO: `pcbnew/pcb_io/kicad_sexpr/`, `eeschema/sch_io/kicad/`.
- Connectivity: `eeschema/connection_graph.{h,cpp}` (BFS over wires/buses/labels).
- DRC engine + rule expression language: `pcbnew/drc/drc_engine.cpp`, `drc_rule_parser.cpp`.
- Gerber/Excellon export: `pcbnew/exporters/gendrill_*`, `gerber_jobfile_writer.cpp`.

## 6. Bottom line

KiCad gives us **geometry primitives** (VECTOR2/SEG/SHAPE_ARC/CIRCLE/…) and **ECAD format/
connectivity/DRC** code, plus two permissive libs (Clipper2, Delaunator). For **internal use we may
borrow it directly** (GPL allows private use); if distribution is ever planned, treat `libs/kimath`
+ app code as clean-room reference instead. It gives us **nothing** for the constraint solver.
