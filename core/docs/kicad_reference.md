# KiCad as a Clean-Room Reference for `core/`

Survey of KiCad master (2026-06-15): which parts may be reused directly and which are
reference material only. Chudware is MIT-licensed; the governing dependency policy is
[`contracts/licenses.md`](../../contracts/licenses.md).

> **License posture.** KiCad's application code and geometry/math library (`libs/kimath`)
> are **GPL-2/3-or-later** (every file header carries a GPL notice). GPL code is never
> copied, linked or vendored into Chudware. KiCad's GPL sources serve only as a
> *behavioral* reference: Chudware's geometry and ECAD code is written clean-room from public
> documentation, file-format descriptions and standard computational-geometry literature.
> Only the permissively licensed third-party libraries KiCad itself bundles (table below)
> may be vendored, and each would get its own row in `contracts/licenses.md`.
>
> Independent of license, `libs/kimath` is entangled with wxWidgets and KiCad-wide headers,
> so its primitives are as quick to reimplement as to extract. KiCad has **no parametric
> constraint solver**; Chudware's `nsolve`/sketch solver has no KiCad analog.

## 1. License table

| Path | License | Reusable? |
|---|---|---|
| `libs/kimath/**` (all geometry/math) | **GPL-2-or-later** | **No — reference only** |
| `libs/core/**`, `libs/sexpr/**` | **GPL** (+ wxWidgets dep) | **No — reference only** |
| `eeschema/`, `pcbnew/`, `common/`, `gerbview/`, `kicad/` (app) | **GPL-3-or-later** | **No** |
| `thirdparty/clipper2/` | **Boost 1.0** | **Yes — vendorable** (polygon booleans) |
| `thirdparty/delaunator/` | **MIT** | **Yes — vendorable** (Delaunay triangulation) |
| `thirdparty/tinyspline_lib/` | **MIT** | **Yes** (B-spline / NURBS) |
| `thirdparty/nlohmann_json/`, `fmt/`, `magic_enum/`, `rectpack2d/`, `thread-pool/` | **MIT** | **Yes** (Chudware already uses nlohmann/json) |
| `thirdparty/nanosvg/` (ZLib), `gzip-hpp/` (BSD-2), `lemon/` (PD) | permissive | **Yes** |
| `thirdparty/dxflib_qcad/`, `thirdparty/potrace/` | **GPL-2+** | **No** — use an MIT/LGPL DXF library instead |

## 2. Algorithms worth reimplementing clean-room

These are the concepts KiCad's geometry layer covers; Chudware implements equivalents from
first principles, not from KiCad source.

1. **Integer 2D vector** — `int64_t` "extended type" for cross/dot intermediates
   (overflow-safe integer CAD coordinates).
2. **Segment operations** — segment–point distance, segment–segment intersection, closest
   point, side test. The core 2D workhorse.
3. **Arcs** — center/start/mid/end representation (the mid-point form avoids arc
   ambiguity), tessellation to polylines, collision.
4. **Typed angles** — normalization to [0, 360) / (−180, 180], cardinal-direction tests.
5. **Apollonius tangent-circle construction** — circle tangent to two entities through a
   point (needed for fillets).
6. **Unified intersection dispatch** — `std::variant` + `std::visit` across {segment, line,
   ray, circle, arc, box}. A general C++ pattern.
7. **Bézier flattening** — de Casteljau Bézier → polyline.

## 3. Permissive libraries to consider

- **Clipper2** (Boost) — polygon union/subtract/intersect/offset; useful for sketch region
  booleans, copper pours and DRC clearance zones. No wx dependency.
- **Delaunator** (MIT) — fast O(n log n) Delaunay triangulation; polygon fill meshing.

Neither is vendored today; add one only when a feature needs it, with a license row.

## 4. Coordinate / unit notes

- KiCad uses **integer nanometers** (`IU_PER_MM = 1e6`) for PCB coordinates with `int64_t`
  intermediate products, which avoids floating-point drift. This is worth considering for
  ECAD geometry; the constraint solver stays float64.
- A homogeneous 3×3 matrix is the natural 2D affine-transform representation.

## 5. ECAD behavior references (GPL — reference only)

For interoperability and behavior parity, KiCad's public file formats and documented
behavior are the reference; the source is not:

- S-expression file syntax (`.kicad_pcb`, `.kicad_sym`, `.kicad_sch`) — Chudware's reader and
  writer are in `core/ecad/kicad.{h,cpp}`.
- Net connectivity over wires, buses and labels.
- DRC rules and the rule-expression language.
- Gerber/Excellon export conventions (both formats have public specifications).
