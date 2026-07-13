// mesher.cpp — see mesher.h. The FEA tet mesher (wraps Netgen / nglib 6.2, LGPL-2.1).
//
// nglib OCC session sequence (the C library interface, nglib.h + nglib_occ.h):
//   Ng_Init -> Ng_OCC_Load_STEP -> Ng_NewMesh -> fill Ng_Meshing_Parameters from the
//   bbox-DIAGONAL base size + fineness (maxh/minh), curvature (elementspercurve) and the
//   optimizer (optsteps/optvolmeshenable) -> Ng_OCC_SetLocalMeshSize ->
//   Ng_OCC_GenerateEdgeMesh -> Ng_OCC_GenerateSurfaceMesh -> Ng_GenerateVolumeMesh ->
//   (order 2) Ng_OCC_Generate_SecondOrder (mid-side nodes PROJECTED to the CAD surface) ->
//   Ng_GetNP/Ng_GetPoint + Ng_GetNE/Ng_GetVolumeElement (tet4 NG_TET / tet10 NG_TET10).
//
// LICENSE: Netgen/nglib is LGPL-2.1 (commercial-safe under DYNAMIC linking — we ship
// nglib.dll + ngcore.dll next to libaicad.dll and offer the Netgen source). This replaced
// gmsh (GPL-2+, no linking exception) which had been linked into libaicad.dll — the #1
// commercialization blocker. gmsh already wrapped Netgen for its OptimizeNetgen pass, so
// the volume optimizer is literally the same engine; quality parity is therefore realistic.
//
// IMPLEMENTATION NOTE — the nglib C interface (Ng_*) is the cleanest, most stable surface
// and mirrors the old gmshc.h C-API pattern. Netgen is BUILT FROM SOURCE with MSVC against
// our exact OCCT 7.9.3 (core/cmake/ProvisionNetgen.cmake), so (a) the C++ ABI matches MSVC
// and (b) nglib.dll and libaicad.dll share ONE OCCT — Ng_OCC_Load_STEP reads the STEP with
// the same kernel, no two-OCCT symbol clash. nglib defines its functions inside
// `namespace nglib`, so we include the header in that namespace and qualify the calls.
//
// tet10 node ordering: Netgen numbers the 6 mid-edge nodes in LEXICOGRAPHIC edge order
// (0,1)(0,2)(0,3)(1,2)(1,3)(2,3); the FEA solvers + tests expect the gmsh MSH_TET_10 order
// (0,1)(1,2)(2,0)(3,0)(3,2)(3,1). We remap with the fixed permutation kNg2Gmsh below
// (VERIFIED invariant across 51k+ mid-nodes on curved parts — see ProvisionNetgen notes).

#include "sim/mesher.h"

#include "prof/phases.h"     // per-phase timers (StudyScope "mesh_tet")
#include "sim/hex_mesher.h"  // mesh_box_hex (structured hex path)
#include "geom/booleans.h"   // geom::unify_same_domain (mesh-time geometry healing)
#include "util/resource_governor.h"  // host-memory element ceiling (graceful degrade)

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdio>
#include <filesystem>
#include <mutex>
#include <limits>

// OCCT data-exchange + healing for the mesh-time defeaturing fallback (Issue 6): a STEP
// read/write round-trip plus ShapeFix + same-domain face merge, used only when the volume
// mesher rejects fused-primitive geometry (coincident coplanar faces / sliver edges). The
// reader is also used to compute the bounding box (diagonal + smallest side) that drives the
// base element size, since the nglib OCC path takes a file path rather than a shape.
#include <IFSelect_ReturnStatus.hxx>
#include <STEPControl_Reader.hxx>
#include <STEPControl_Writer.hxx>
#include <STEPControl_StepModelType.hxx>
#include <ShapeFix_Shape.hxx>
#include <TopoDS_Shape.hxx>
#include <Bnd_Box.hxx>
#include <BRepBndLib.hxx>
#include <TopExp_Explorer.hxx>
#include <TopAbs_ShapeEnum.hxx>
#include <GProp_GProps.hxx>
#include <BRepGProp.hxx>

// nglib's C interface. The header declares plain function names but the implementation lives
// inside `namespace nglib`, so include it in that namespace (the canonical usage — see
// netgen's own nglib/ng_occ.cpp). OCCGEOMETRY pulls in the Ng_OCC_* declarations.
#define OCCGEOMETRY 1
namespace nglib {
#include <nglib.h>
}

namespace aicad::sim {

namespace {

// Netgen keeps PROCESS-GLOBAL meshing state (netgen::mparam / occparam, message streams)
// and is NOT thread-safe across concurrent mesh generations. The host can trigger
// overlapping meshes — a background study solve (SimClient thread) while the user hits
// "Preview mesh" (main thread), or rapid re-runs across a study-type switch. This mutex
// serializes the whole session so those paths queue instead of racing.
std::mutex& mesh_mutex() {
    static std::mutex m;
    return m;
}

// Ng_Init() only wires up Netgen's message streams; run it once for the process.
void ensure_ng_init() {
    static std::once_flag once;
    std::call_once(once, [] { nglib::Ng_Init(); });
}

// Netgen tet10 local index -> gmsh MSH_TET_10 local index. out.tets[e*10 + k] takes the
// Netgen element node at kNg2Gmsh[k]. Corners (0..3) pass through; the 6 mid nodes are
// reordered from Netgen's lexicographic edges to the gmsh edge order the FEA shape
// functions / load builders assume. (Verified invariant on real curved meshes.)
constexpr int kNg2Gmsh[10] = {0, 1, 2, 3, 4, 7, 5, 6, 9, 8};

double tet_volume6(const std::vector<double>& p, int a, int b, int c, int d) {
    const double* A = &p[static_cast<size_t>(a) * 3];
    const double* B = &p[static_cast<size_t>(b) * 3];
    const double* C = &p[static_cast<size_t>(c) * 3];
    const double* D = &p[static_cast<size_t>(d) * 3];
    const double ux = B[0] - A[0], uy = B[1] - A[1], uz = B[2] - A[2];
    const double vx = C[0] - A[0], vy = C[1] - A[1], vz = C[2] - A[2];
    const double wx = D[0] - A[0], wy = D[1] - A[1], wz = D[2] - A[2];
    return ux * (vy * wz - vz * wy) - uy * (vx * wz - vz * wx) +
           uz * (vx * wy - vy * wx);
}

double edge_len(const std::vector<double>& p, int a, int b) {
    const double* A = &p[static_cast<size_t>(a) * 3];
    const double* B = &p[static_cast<size_t>(b) * 3];
    const double dx = A[0] - B[0], dy = A[1] - B[1], dz = A[2] - B[2];
    return std::sqrt(dx * dx + dy * dy + dz * dz);
}

// Triangle area for face (a,b,c) via half the cross-product magnitude.
double tri_area(const std::vector<double>& p, int a, int b, int c) {
    const double* A = &p[static_cast<size_t>(a) * 3];
    const double* B = &p[static_cast<size_t>(b) * 3];
    const double* C = &p[static_cast<size_t>(c) * 3];
    const double ux = B[0] - A[0], uy = B[1] - A[1], uz = B[2] - A[2];
    const double vx = C[0] - A[0], vy = C[1] - A[1], vz = C[2] - A[2];
    const double cx = uy * vz - uz * vy, cy = uz * vx - ux * vz, cz = ux * vy - uy * vx;
    return 0.5 * std::sqrt(cx * cx + cy * cy + cz * cz);
}

// Per-element quality of the corner tet (nodes a,b,c,d):
//   aspect  = (longest edge / shortest altitude) normalized so a regular tet -> 1.0.
//             altitude_from_vertex = 3*Volume / area(opposite face); the shortest
//             altitude corresponds to the largest opposite-face area.
//   sjac    = scaled Jacobian, min over the 4 corners, normalized so a regular tet -> 1.0
//             (sqrt(2) * 6V / product of the 3 incident edge lengths). <=0 => inverted.
struct TetQuality {
    double aspect;
    double sjac;
};
TetQuality tet_quality(const std::vector<double>& p, int a, int b, int c, int d) {
    const int v[4] = {a, b, c, d};
    static const int FACES[4][3] = {{1, 2, 3}, {0, 2, 3}, {0, 1, 3}, {0, 1, 2}};
    static const int ED[6][2] = {{0, 1}, {0, 2}, {0, 3}, {1, 2}, {1, 3}, {2, 3}};
    // Edges incident to each corner (indices into v).
    static const int INC[4][3] = {{1, 2, 3}, {0, 2, 3}, {0, 1, 3}, {0, 1, 2}};

    const double vol = std::abs(tet_volume6(p, a, b, c, d)) / 6.0;
    TetQuality q{std::numeric_limits<double>::infinity(), 0.0};
    if (!(vol > 0.0)) return q;  // degenerate -> aspect inf, sjac 0

    double longest = 0.0;
    for (const auto& e : ED) longest = std::max(longest, edge_len(p, v[e[0]], v[e[1]]));

    double max_face = 0.0;
    for (const auto& f : FACES)
        max_face = std::max(max_face, tri_area(p, v[f[0]], v[f[1]], v[f[2]]));

    if (max_face > 0.0 && longest > 0.0) {
        const double shortest_alt = 3.0 * vol / max_face;
        if (shortest_alt > 0.0) {
            // 1.224744871 = sqrt(3/2) = the (longest edge / shortest altitude) of a
            // regular tetrahedron; dividing normalizes that ideal case to 1.0.
            q.aspect = (longest / shortest_alt) / 1.224744871391589;
        }
    }

    const double det6 = 6.0 * vol;  // |det of incident-edge matrix| at any corner
    double sjac_min = std::numeric_limits<double>::infinity();
    for (int ci = 0; ci < 4; ++ci) {
        double prod = 1.0;
        for (int k = 0; k < 3; ++k) prod *= edge_len(p, v[ci], v[INC[ci][k]]);
        if (prod > 0.0) sjac_min = std::min(sjac_min, std::sqrt(2.0) * det6 / prod);
    }
    q.sjac = std::isfinite(sjac_min) ? sjac_min : 0.0;
    return q;
}

void fill_quality(MeshOut& out) {
    const int stride = out.nodes_per_elem == 10 ? 10 : 4;
    const size_t n = out.tets.size() / static_cast<size_t>(stride);
    out.aspect_hist.assign(10, 0);
    if (n == 0) return;
    double min_vol = std::numeric_limits<double>::infinity();
    double max_ratio = 0.0;       // legacy longest/shortest edge ratio
    double a_min = std::numeric_limits<double>::infinity();
    double a_max = 0.0, a_sum = 0.0;
    double j_min = std::numeric_limits<double>::infinity();
    size_t poor = 0, counted = 0;
    static const int ED[6][2] = {{0, 1}, {0, 2}, {0, 3}, {1, 2}, {1, 3}, {2, 3}};
    for (size_t e = 0; e < n; ++e) {
        const int* t = &out.tets[e * static_cast<size_t>(stride)];
        const double vol = std::abs(tet_volume6(out.points, t[0], t[1], t[2], t[3])) / 6.0;
        if (vol > 0.0 && vol < min_vol) min_vol = vol;
        double lo = std::numeric_limits<double>::infinity(), hi = 0.0;
        for (const auto& ed : ED) {
            const double L = edge_len(out.points, t[ed[0]], t[ed[1]]);
            if (L > 0.0) {
                lo = std::min(lo, L);
                hi = std::max(hi, L);
            }
        }
        if (std::isfinite(lo) && lo > 0.0) max_ratio = std::max(max_ratio, hi / lo);

        const TetQuality q = tet_quality(out.points, t[0], t[1], t[2], t[3]);
        if (!std::isfinite(q.aspect)) continue;  // skip degenerate from the stats
        ++counted;
        a_min = std::min(a_min, q.aspect);
        a_max = std::max(a_max, q.aspect);
        a_sum += q.aspect;
        j_min = std::min(j_min, q.sjac);
        if (q.aspect > kPoorAspect) ++poor;
        // Histogram over [1, kPoorAspect]; last bucket also catches the overflow.
        int b = static_cast<int>((q.aspect - 1.0) / (kPoorAspect - 1.0) * 10.0);
        if (b < 0) b = 0;
        if (b > 9) b = 9;
        out.aspect_hist[static_cast<size_t>(b)]++;
    }
    out.min_tet_volume_mm3 = std::isfinite(min_vol) ? min_vol : 0.0;
    out.max_tet_edge_ratio = max_ratio;
    out.aspect_min = std::isfinite(a_min) ? a_min : 0.0;
    out.aspect_max = a_max;
    out.aspect_avg = counted ? a_sum / static_cast<double>(counted) : 0.0;
    out.jacobian_min = std::isfinite(j_min) ? j_min : 0.0;
    out.pct_poor = counted ? 100.0 * static_cast<double>(poor) / static_cast<double>(counted) : 0.0;
}

}  // namespace

MeshOut mesh_step(const std::string& step_path, double density, int order) {
    MeshOptions options;
    options.density = density;  // legacy [0,1]; mapped to fineness inside (higher = finer)
    options.order = order;
    return mesh_step(step_path, options);
}

namespace {

// Heal a STEP for meshing (Issue 6). Fused-primitive parts (many boxes booleaned with
// boss/merge) meet at coincident coplanar faces with sliver edges at the box-box junctions;
// gmsh's volume mesher (mesh.generate(3)) rejects that topology even though the body is a
// valid solid. We read the STEP back into OCCT, run ShapeFix (sew gaps / fix small edges),
// then ShapeUpgrade_UnifySameDomain (the same .clean() the booleans apply — collapses the
// flush coplanar junction faces to one), and write a healed STEP. Returns the healed path,
// or "" if anything failed (caller falls back to the original). Best-effort: never throws.
std::string heal_step_for_mesh(const std::string& step_path) {
    try {
        STEPControl_Reader rd;
        if (rd.ReadFile(step_path.c_str()) != IFSelect_RetDone) return std::string();
        rd.TransferRoots();
        TopoDS_Shape shape = rd.OneShape();
        if (shape.IsNull()) return std::string();

        Handle(ShapeFix_Shape) fixer = new ShapeFix_Shape(shape);
        fixer->Perform();
        TopoDS_Shape fixed = fixer->Shape();
        if (fixed.IsNull()) fixed = shape;

        TopoDS_Shape healed = ::geom::unify_same_domain(fixed);
        if (healed.IsNull()) healed = fixed;

        std::filesystem::path out_fs = std::filesystem::path(step_path);
        out_fs.replace_extension(".healed.step");
        const std::string out = out_fs.string();
        STEPControl_Writer wr;
        if (wr.Transfer(healed, STEPControl_AsIs) != IFSelect_RetDone) return std::string();
        if (wr.Write(out.c_str()) != IFSelect_RetDone) return std::string();
        return out;
    } catch (...) {
        return std::string();  // best-effort — the caller keeps the original STEP
    }
}

}  // namespace

// The Netgen meshing core (defined below); the public mesh_step wraps it with the
// heal-and-retry fallback for fused-primitive geometry (Issue 6), the structured-hex ->
// tet fallback, the quad-surface -> tet fallback, and the Simple-mode auto-pick.
// `pre_shape` (P4): the caller's already-parsed OCCT shape for THIS step_path — used for
// the bbox/volume sizing so the file isn't re-parsed (nglib still loads it internally, its
// API is path-based). nullptr = parse here (old behaviour).
MeshOut mesh_step_netgen(const std::string& step_path, const MeshOptions& options,
                         const TopoDS_Shape* pre_shape = nullptr);

namespace {

// One STEP parse for the whole mesh_step pipeline (P4). The old flow parsed the SAME file
// up to 4 times per mesh (auto-pick classify, auto-pick bbox, netgen bbox/volume, plus
// twice inside mesh_box_hex) — each a full STEPControl_Reader pass. Null shape on failure;
// every consumer falls back to its own parse-and-fail path, so errors surface unchanged.
TopoDS_Shape read_step_shape(const std::string& step_path) {
    try {
        STEPControl_Reader rd;
        if (rd.ReadFile(step_path.c_str()) != IFSelect_RetDone) return TopoDS_Shape();
        rd.TransferRoots();
        return rd.OneShape();
    } catch (...) {
        return TopoDS_Shape();
    }
}

// Unstructured tet meshing with the heal-and-retry fallback (Issue 6): if the raw geometry
// defeats the volume mesher (coincident coplanar faces / sliver edges on a fused-primitive
// part), heal the STEP and retry ONCE before reporting failure. Clean geometry never pays
// the cost — the heal only runs after a real miss.
MeshOut mesh_tet_healed(const std::string& step_path, const MeshOptions& options,
                        const TopoDS_Shape* pre_shape) {
    MeshOut out = mesh_step_netgen(step_path, options, pre_shape);
    if (out.ok) return out;
    std::string healed = heal_step_for_mesh(step_path);
    if (!healed.empty() && healed != step_path) {
        // Healed geometry differs from the original — never reuse pre_shape for it.
        MeshOut out2 = mesh_step_netgen(healed, options, nullptr);
        std::error_code ec;
        std::filesystem::remove(healed, ec);
        if (out2.ok) return out2;
    }
    return out;  // surface the original mesher error
}

// quad_surface is a SURFACE-meshing HINT (Netgen mp.quad_dominated): it asks for a
// quad-dominated surface mesh while the VOLUME stays tetrahedral. On this Netgen build
// (6.2.2504 / nglib) a quad-dominated surface can leave the volume mesher with "open quads"
// and FAIL to close the volume — even on a trivial box. A surface hint must never break the
// volume mesh, so when a quad_surface request yields no valid tet volume, retry ONCE without
// quad-dominance and annotate the degradation. Non-quad requests take the first attempt
// verbatim (no extra cost, identical behavior).
MeshOut mesh_tet_quad_aware(const std::string& step_path, const MeshOptions& options,
                            const TopoDS_Shape* pre_shape) {
    MeshOut out = mesh_tet_healed(step_path, options, pre_shape);
    if (out.ok || !options.quad_surface) return out;

    // The quad-dominated surface defeated the volume mesher; fall back to a plain tet volume.
    MeshOptions tet_opts = options;
    tet_opts.quad_surface = false;
    MeshOut fb = mesh_tet_healed(step_path, tet_opts, pre_shape);
    if (!fb.ok) return out;  // even plain tet failed -> surface the ORIGINAL quad error

    // A valid tet volume was produced, but the requested quad surface was unavailable here.
    fb.mesh_type_used = (options.order == 2) ? "tet10-quad-surface-fallback"
                                             : "tet4-quad-surface-fallback";
    const std::string fbnote =
        "quad_surface requested but Netgen's volume mesher rejected the quad-dominated "
        "surface (open quads); fell back to a tetrahedral volume mesh";
    fb.note = fb.note.empty() ? fbnote : (fbnote + "; " + fb.note);
    return fb;
}

// Simple-mode auto-pick: classify the geometry and choose the element type + sizing.
//   • axis-aligned box     -> structured hex8.
//   • thin-wall part       -> tet, sized for ~thin_wall_layers elements through the smallest
//                             dimension (else a thin part gets one element through its
//                             thickness, which is FEA-inaccurate).
//   • general curved solid -> tet with curvature-adaptive refinement.
// Returns the effective MeshOptions; `note` describes the decision (for MeshOut::note).
MeshOptions pick_options_for_geometry(const TopoDS_Shape& shape, const MeshOptions& base,
                                      std::string& note) {
    MeshOptions opts = base;
    const BoxGeomInfo info = detect_box_geometry(shape);  // shape overload — no re-parse (P4)
    if (info.is_box) {
        opts.structured = true;
        note = "auto-pick: axis-aligned box -> hex8";
        return opts;
    }
    // Non-box: tetrahedral. A box's vertex bbox is exact, but a curved solid needs a real
    // Bnd_Box to bound it — computed from the shared parsed shape.
    opts.structured = false;
    opts.curvature = true;
    double dx = 0.0, dy = 0.0, dz = 0.0;
    try {
        if (!shape.IsNull()) {
            Bnd_Box bb;
            BRepBndLib::Add(shape, bb);
            if (!bb.IsVoid()) {
                double xmin, ymin, zmin, xmax, ymax, zmax;
                bb.Get(xmin, ymin, zmin, xmax, ymax, zmax);
                dx = std::fabs(xmax - xmin);
                dy = std::fabs(ymax - ymin);
                dz = std::fabs(zmax - zmin);
            }
        }
    } catch (...) {
        dx = dy = dz = 0.0;
    }
    const double diag = std::sqrt(dx * dx + dy * dy + dz * dz);
    const double min_side = std::min({dx, dy, dz});
    // Thin-wall heuristic: the smallest dimension is a small fraction of the diagonal (a
    // plate / shell / thin disc). diag/6 is a tunable threshold.
    constexpr double kThinWallRatio = 1.0 / 6.0;
    if (diag > 0.0 && min_side > 0.0 && min_side < diag * kThinWallRatio) {
        const double layers = (base.thin_wall_layers > 0.0) ? base.thin_wall_layers : 4.0;
        // Size so ~`layers` tet layers span the smallest dimension. Set max == target so the
        // through-thickness sizing isn't coarsened away (mesh_step_netgen maps max_size_mm
        // onto Netgen's maxh); min allows a little local refinement.
        opts.target_size_mm = min_side / layers;
        opts.min_size_mm = min_side / (2.0 * layers);
        opts.max_size_mm = min_side / layers;
        char buf[160];
        std::snprintf(buf, sizeof(buf),
                      "auto-pick: thin-wall (min_side/diag=%.3f), sized for %d tet layers",
                      min_side / diag, static_cast<int>(layers));
        note = buf;
        return opts;
    }
    note = "auto-pick: general solid -> tet";
    return opts;
}

}  // namespace

namespace {

// The single-solid meshing pipeline (auto-pick -> hex/tet dispatch). `shape` is the
// already-parsed geometry for `step_path` (P4 — may be null on parse failure; consumers
// keep their own parse-and-fail paths, so error messages are unchanged).
MeshOut mesh_step_single(const std::string& step_path, const MeshOptions& options,
                         const TopoDS_Shape& shape) {
    const TopoDS_Shape* pre = shape.IsNull() ? nullptr : &shape;

    // (3) Auto-pick (Simple mode): classify the geometry and resolve concrete options before
    // dispatching. Gated behind auto_type so existing callers are unaffected.
    MeshOptions eff = options;
    std::string auto_note;
    const bool auto_picked = options.auto_type;
    if (auto_picked) {
        eff = pick_options_for_geometry(shape, options, auto_note);
        eff.auto_type = false;  // resolved to concrete options; never re-enter auto-pick
    }

    if (eff.structured) {
        // Structured hexahedral: delegate to the OCCT box-grid mesher (no Netgen).
        MeshOut hx = pre ? mesh_box_hex(shape, eff) : mesh_box_hex(step_path, eff);
        if (hx.ok) {
            if (auto_picked) {
                hx.mesh_type_used = "hex8-autopick";
                hx.note = auto_note;
            }
            return hx;
        }
        // (1) Structured hex failed — the part is not a mappable axis-aligned box. With
        // fallback_to_tet (the default) fall back to tet meshing and annotate, instead of the
        // post-gmsh hard failure. Set fallback_to_tet=false to require hex and surface the
        // original box error unchanged.
        if (eff.fallback_to_tet) {
            MeshOptions tet_opts = eff;
            tet_opts.structured = false;
            MeshOut to = mesh_tet_quad_aware(step_path, tet_opts, pre);
            if (to.ok) {
                to.mesh_type_used = (eff.order == 2) ? "tet10-fallback" : "tet4-fallback";
                to.note = "structured hex not applicable, tet fallback used: " + hx.error;
            }
            return to;
        }
        return hx;  // fallback disabled: surface the original box error unchanged
    }

    // Unstructured tetrahedral path (with heal-and-retry).
    MeshOut out = mesh_tet_quad_aware(step_path, eff, pre);
    if (auto_picked && out.ok) {
        // mesh_step_netgen set mesh_type_used to tet4/tet10; relabel as the auto-pick variant
        // (thin-wall sizing vs. a general-solid pick) and surface the decision note.
        const bool thin = auto_note.find("thin-wall") != std::string::npos;
        const std::string tet = (eff.order == 2) ? "tet10" : "tet4";
        out.mesh_type_used = tet + (thin ? "-thinwall" : "-autopick");
        out.note = out.note.empty() ? auto_note : (auto_note + "; " + out.note);
    }
    return out;
}

}  // namespace

MeshOut mesh_step(const std::string& step_path, const MeshOptions& options) {
    // P4: parse the STEP ONCE and thread the shape through classify/pick/hex/netgen.
    TopoDS_Shape shape = read_step_shape(step_path);

    // (asm-mesh) A multi-solid STEP is an exported assembly / multi-body document. Netgen
    // meshes every solid's boundary independently, so bodies that touch or overlap (real
    // assemblies do both — press fits, coincident mount faces) produce intersecting surface
    // elements and the volume mesher aborts with "boundary mesh is overlapping". Bond the
    // bodies first: one general fuse welds overlapping volumes and glues flush contact,
    // UnifySameDomain collapses the seam faces, and the bonded solid meshes as one conforming
    // domain (load transfers across every interface — bonded-contact semantics). Outer faces
    // keep their world position, so faceset resolution is unaffected. nglib's loader is
    // path-based, so the bonded shape goes through a temp STEP beside the original. Fuse
    // failure falls through to the raw compound — single-solid behavior and its error
    // surfaces are unchanged.
    std::string mesh_path = step_path;
    std::string bonded_tmp;
    std::string bonded_note;
    int n_solids = 0;
    if (!shape.IsNull())
        for (TopExp_Explorer e(shape, TopAbs_SOLID); e.More(); e.Next()) ++n_solids;
    if (n_solids > 1) {
        try {
            std::vector<TopoDS_Shape> solids;
            solids.reserve(static_cast<std::size_t>(n_solids));
            for (TopExp_Explorer e(shape, TopAbs_SOLID); e.More(); e.Next())
                solids.push_back(e.Current());
            const std::vector<TopoDS_Shape> tools(solids.begin() + 1, solids.end());
            TopoDS_Shape fused = ::geom::fuse_many(solids.front(), tools);
            if (!fused.IsNull()) {
                TopoDS_Shape unified = ::geom::unify_same_domain(fused);
                if (!unified.IsNull()) fused = unified;
            }
            int n_after = 0;
            if (!fused.IsNull())
                for (TopExp_Explorer e(fused, TopAbs_SOLID); e.More(); e.Next()) ++n_after;
            if (n_after >= 1) {
                std::filesystem::path tmp(step_path);
                tmp.replace_extension(".bonded.step");
                STEPControl_Writer wr;
                if (wr.Transfer(fused, STEPControl_AsIs) == IFSelect_RetDone &&
                    wr.Write(tmp.string().c_str()) == IFSelect_RetDone) {
                    bonded_tmp = tmp.string();
                    mesh_path = bonded_tmp;
                    shape = fused;
                    bonded_note = "assembly bonded for meshing: " + std::to_string(n_solids) +
                                  " solid bodies fused into " + std::to_string(n_after);
                }
            }
        } catch (...) {
            // best-effort — the raw compound still goes to the mesher, which reports
            // its own (now-annotated) failure below
        }
    }

    MeshOut out = mesh_step_single(mesh_path, options, shape);

    if (!bonded_tmp.empty()) {
        std::error_code ec;
        std::filesystem::remove(bonded_tmp, ec);
    }
    if (n_solids > 1) {
        if (out.ok && !bonded_note.empty())
            out.note = out.note.empty() ? bonded_note : (bonded_note + "; " + out.note);
        if (!out.ok)
            out.error += " [the STEP contains " + std::to_string(n_solids) +
                         " solid bodies (assembly): bodies were " +
                         (bonded_note.empty() ? std::string("NOT bondable (fuse failed)")
                                              : std::string("fused for bonded meshing")) +
                         " — check for invalid/self-intersecting component geometry]";
    }
    return out;
}

// RAII holder so the Netgen mesh + OCC geometry are freed on EVERY exit path (including the
// throw paths — Ng_OCC_Load_STEP / the mesh generators raise on bad geometry). Delete the
// mesh BEFORE the geometry (the geometry-first order also works, but this matches the
// validated teardown order). Best-effort: never throws out of the destructor.
struct NetgenSession {
    nglib::Ng_Mesh* mesh = nullptr;
    nglib::Ng_OCC_Geometry* geo = nullptr;
    ~NetgenSession() {
        try {
            if (mesh) nglib::Ng_DeleteMesh(mesh);
            if (geo) nglib::Ng_OCC_DeleteGeometry(geo);
        } catch (...) {
        }
    }
};

MeshOut mesh_step_netgen(const std::string& step_path, const MeshOptions& options,
                         const TopoDS_Shape* pre_shape) {
    std::lock_guard<std::mutex> mesh_lock(mesh_mutex());  // serialize: Netgen is not thread-safe
    // Per-phase record (prof/phases.h): load_step / generate / second_order / extract.
    // Opened AFTER the meshing mutex so lock-wait is not misattributed to meshing time.
    aicad::prof::phases::StudyScope _study("mesh_tet");
    MeshOut out;

    // Second-order (tet10) selection. order==1 produces 4-node tets (NG_TET); order==2
    // elevates to 10-node tets (NG_TET10) with mid-side nodes projected to the CAD surface.
    // Any other value is treated as order 1 (defensive).
    const bool order2 = (options.order == 2);
    const int npe = order2 ? 10 : 4;
    out.nodes_per_elem = npe;

    auto fail = [&](const std::string& msg) -> MeshOut {
        out.ok = false;
        out.error = "netgen: " + msg;
        return out;
    };

    // Resolve the primary sizing control. A legacy `density` (>= 0) wins and maps
    // directly onto fineness (both use "higher = finer"); otherwise use `fineness`.
    const double fineness = std::min(
        1.0, std::max(0.0, options.density >= 0.0 ? options.density : options.fineness));

    // Bounding box -> base element size, derived from the DIAGONAL (not the smallest side).
    // The nglib OCC path takes a STEP file path rather than an in-memory shape, so we read
    // the same STEP with OCCT to get the bbox. Keying off the diagonal gives a sane element
    // count across very different proportions: a thin plate, a long bracket and a cube all
    // mesh reasonably instead of the smallest-dimension heuristic exploding thin parts.
    double dx = 0.0, dy = 0.0, dz = 0.0;
    double solid_vol = 0.0;  // real occupied volume (mm^3) — drives the element-count budget
    try {
        TopoDS_Shape shape;  // P4: reuse the caller's parse; only read the file ourselves
        if (pre_shape && !pre_shape->IsNull()) {
            shape = *pre_shape;
        } else {
            STEPControl_Reader rd;
            if (rd.ReadFile(step_path.c_str()) != IFSelect_RetDone)
                return fail("could not read the STEP (is the path valid?)");
            rd.TransferRoots();
            shape = rd.OneShape();
        }
        if (shape.IsNull()) return fail("STEP contained no shape");
        Bnd_Box bb;
        BRepBndLib::Add(shape, bb);
        if (bb.IsVoid()) return fail("degenerate bounding box (void)");
        double xmin, ymin, zmin, xmax, ymax, zmax;
        bb.Get(xmin, ymin, zmin, xmax, ymax, zmax);
        dx = std::fabs(xmax - xmin);
        dy = std::fabs(ymax - ymin);
        dz = std::fabs(zmax - zmin);
        // Real solid volume for the mesh budget below (a bulky part has volume ~ diag^3, so
        // diagonal-only sizing over-meshes it). Cheap; failure just falls back to bbox volume.
        try {
            GProp_GProps vp;
            BRepGProp::VolumeProperties(shape, vp);
            solid_vol = std::fabs(vp.Mass());
        } catch (...) { solid_vol = 0.0; }
    } catch (...) {
        return fail("failed to read STEP bounding box");
    }
    const double diag = std::sqrt(dx * dx + dy * dy + dz * dz);
    const double min_side =
        std::min({dx > 1e-9 ? dx : 1e30, dy > 1e-9 ? dy : 1e30, dz > 1e-9 ? dz : 1e30});
    if (!(diag > 1e-6)) return fail("degenerate bounding box (diagonal <= 1e-6)");

    // base = diag / lerp(8, 60, fineness): coarse ~= diag/8, fine ~= diag/60. Explicit
    // target_size_mm overrides it outright (and is respected as-is).
    const bool explicit_size =
        std::isfinite(options.target_size_mm) && options.target_size_mm > 0.0;
    const double divisor = 8.0 + 52.0 * fineness;
    double base = explicit_size ? options.target_size_mm : diag / divisor;
    // For auto sizing, never let the base exceed the part's smallest real dimension, or a
    // chunky/thin part gets only a single element through its thickness. (An explicit
    // target is honored verbatim.)
    if (!explicit_size && std::isfinite(min_side) && min_side < 1e29)
        base = std::min(base, min_side);

    // Volume-aware element budget. `base` comes from the DIAGONAL, but the tet count grows with
    // the occupied VOLUME (~ base^-3): a BULKY solid (a plain block) silently explodes to
    // hundreds of thousands of DOF — minutes of meshing + solving — while a slender part of the
    // same diagonal stays small (measured: a 120x100x150 block hit 412k DOF / 96k tets at
    // fineness 0.7). For AUTO sizing, estimate the linear-tet count and coarsen `base` upward so
    // an interactive study stays fast. Budget scales with fineness (Draft small .. Fine large)
    // and is overridable via AI_CAD_MESH_MAX_ELEMS. Explicit target_size_mm is honored verbatim.
    // n_tets ~= C * vol / base^3, C~=6 (empirical Netgen tet packing).
    // Host-memory ceiling on the element count (governor). The downstream SOLVE is the
    // real OOM risk — tet10 assembly+factorization runs tens of KB per element (same
    // derivation as fea_estimate_bytes in fea_run.cpp: (3·npe)²·16 B triplets, +K, +fill·K)
    // — so on a low-end machine the mesh coarsens here, with a note, instead of the solve
    // OOM-crashing the app. Applies to BOTH auto sizing (folded into the interactivity
    // budget below) and an explicit target_size_mm (verbatim honor yields only under
    // memory pressure — graceful lag beats a crash). AICAD_GOVERNOR=0 disables.
    const long mem_max_elems = [&]() -> long {
        const uint64_t mem_budget = aicad::governor::budget_bytes();
        if (mem_budget == UINT64_MAX) return 0;  // governor off / unknowable host
        const int npe = (options.order == 2) ? 10 : 4;
        const double band = 3.0 * npe;
        const double per_elem = band * band * 16.0 * 4.5;  // triplets(1) + K(0.5) + fill(6)*K
        const double cap = static_cast<double>(mem_budget) / per_elem;
        return (cap >= 1.0 && cap < 2e18) ? static_cast<long>(cap) : 0;
    }();
    const double vol_est = solid_vol > 1e-9 ? solid_vol : (dx * dy * dz);
    constexpr double kTetPack = 6.0;
    if (!explicit_size) {
        const double vol = vol_est;
        long budget = static_cast<long>(8000.0 + 52000.0 * fineness);  // ~8k Draft .. 60k Fine
        if (const char* e = std::getenv("AI_CAD_MESH_MAX_ELEMS")) {
            const long b = std::atol(e);
            if (b > 0) budget = b;
        }
        if (mem_max_elems > 0 && mem_max_elems < budget) budget = mem_max_elems;
        if (vol > 1e-9 && base > 1e-9) {
            const double est = kTetPack * vol / (base * base * base);
            if (est > static_cast<double>(budget)) {
                const double base_new = base * std::cbrt(est / static_cast<double>(budget));
                char buf[224];
                std::snprintf(buf, sizeof(buf),
                    "auto-coarsened for interactivity: est ~%.0fk tets exceeds %ldk budget -> "
                    "element size %.2f->%.2f mm (raise Fine or set a target size for more detail)",
                    est / 1000.0, budget / 1000, base, base_new);
                out.note = out.note.empty() ? std::string(buf) : (out.note + "; " + buf);
                base = base_new;
            }
        }
    } else if (mem_max_elems > 0 && vol_est > 1e-9 && base > 1e-9) {
        // Explicit target: honored verbatim UNLESS the projected solve cannot fit the
        // host's memory budget — then coarsen just enough to fit and say so.
        const double est = kTetPack * vol_est / (base * base * base);
        if (est > static_cast<double>(mem_max_elems)) {
            const double base_new =
                base * std::cbrt(est / static_cast<double>(mem_max_elems));
            char buf[240];
            std::snprintf(buf, sizeof(buf),
                "governor: target size %.2f mm would need ~%.0fk elements, beyond this "
                "machine's memory budget (~%ldk) -> element size raised to %.2f mm "
                "(raise AICAD_MEM_FRAC or use a smaller model for more detail)",
                base, est / 1000.0, mem_max_elems / 1000, base_new);
            out.note = out.note.empty() ? std::string(buf) : (out.note + "; " + buf);
            base = base_new;
        }
    }

    // Auto size band. The lower bound used to be base*0.1 — a 10:1 spread. Combined with the
    // curvature refinement that can drive the local size down to MeshSizeMin, that let the
    // FINEST settings emit very small SLIVER tets next to much larger ones: a poorly graded,
    // ill-conditioned mesh whose static solve drifted AWAY from the analytic answer (Bug F7:
    // fineness 0.9 read 0.21 mm vs the 0.80 mm analytic, worse than the 0.5 setting). A
    // tighter 2:1 band (base*0.5 .. base) gradates cleanly and keeps element quality high, so
    // a finer mesh now converges TOWARD the reference instead of degrading. (Explicit
    // min/max overrides are still honored verbatim.)
    double min_len =
        (std::isfinite(options.min_size_mm) && options.min_size_mm > 0.0)
            ? options.min_size_mm
            : base * 0.5;
    double max_len =
        (std::isfinite(options.max_size_mm) && options.max_size_mm > 0.0)
            ? options.max_size_mm
            : base;
    if (max_len < min_len) std::swap(max_len, min_len);
    // Never let the floor collapse to a near-zero absolute size (a degenerate bbox or an
    // aggressive curvature setting could otherwise push minh to a sliver scale that the
    // mesher fills with near-zero-volume tets). Keep it a sane fraction of the diagonal.
    const double abs_floor = diag * 1e-3;
    if (min_len < abs_floor) min_len = abs_floor;
    if (max_len < min_len) max_len = min_len;

    ensure_ng_init();
    NetgenSession sess;

    try {
        // Load the OCC geometry from the STEP via Netgen's OCCT (the SAME OCCT libaicad
        // links). Raises a netgen exception on unreadable geometry; treat null OR throw as a
        // load failure so the heal-and-retry wrapper can take over.
        {
            aicad::prof::phases::PhaseScope _p("load_step");
            sess.geo = nglib::Ng_OCC_Load_STEP(step_path.c_str());
        }
        if (!sess.geo) return fail("could not load the OCC geometry from the STEP");

        sess.mesh = nglib::Ng_NewMesh();

        nglib::Ng_Meshing_Parameters mp;  // exported ctor sets Netgen's defaults
        mp.uselocalh = 1;                  // honor the local size field (size band + curvature)
        mp.maxh = max_len;
        mp.minh = min_len;
        mp.fineness = fineness;
        // Grading controls how fast element size may change between neighbours (nglib doc:
        // 0 => uniform mesh, 1 => aggressive local grading). 0.2 ("fine gradation") keeps the
        // size field smooth so a curvature-refined wall blends GRADUALLY into the coarser
        // interior instead of jumping a step — this, more than the min/max band, is what makes
        // a cylinder mesh look EVEN (it was 0.3, Netgen's default, which let sizes step). The
        // 2:1 min/max band above is kept deliberately tight (Bug F7); grading does the rest.
        //
        // This local size field (uselocalh) is BOUNDARY-ALIGNED: edge + curvature refinement is
        // seeded on the geometry boundary and propagated inward, so elements are naturally finer
        // near feature edges / curved faces and coarsen toward the bulk. Honest scope note:
        // nglib's OCC pipeline meshes the VOLUME as TETRAHEDRA (NG_TET / NG_TET10). It does NOT
        // expose true boundary-layer prism extrusion or mixed hex/prism/tet volume meshing; the
        // only non-tet volume elements it ever emits are the stray pyramid/prism at a
        // quad-dominated-surface transition, which we split back into tets below. So we grade
        // tets toward the boundary — we do not (and must not claim to) build a hybrid volume.
        mp.grading = 0.2;
        mp.elementsperedge = 2.0;
        // Curvature-adaptive refinement around holes / fillets / cylinders — THE control that
        // resolves a curved face evenly. Netgen's elementspercurve maps to curvaturesafety, and
        // occ/occgenmesh.cpp::ComputeH restricts the local size on a curve of radius r to
        // r/curvaturesafety (capped at maxh); a full 2*pi circle therefore gets
        // ~2*pi*curvaturesafety elements. To request curv_elems_per_circle elements per full
        // turn we set elementspercurve = curv_elems_per_circle / (2*pi). (Conversion VERIFIED
        // against ComputeH.) Curvature off => a low value (1.0) so sizing stays uniform.
        constexpr double kTwoPi = 6.283185307179586;
        mp.elementspercurve =
            (options.curvature && options.curv_elems_per_circle > 0.0)
                ? std::max(1.0, options.curv_elems_per_circle / kTwoPi)
                : 1.0;
        mp.second_order = 0;  // elevate explicitly below (with CAD-surface projection)
        // Quality optimization — Netgen's native surface + volume passes (the same engine gmsh
        // invoked via OptimizeNetgen): edge/face swaps, point-combine and smoothing that remove
        // sliver tets, which is what RAISES jacobian_min and LOWERS aspect_max / pct_poor. Run
        // 5 passes (was 3) so the worst elements get cleaned up; the optimizer is cheap next to
        // generation, so the extra passes are well worth the quality. Disabled => 0 passes.
        mp.optsurfmeshenable = options.optimize ? 1 : 0;
        mp.optvolmeshenable = options.optimize ? 1 : 0;
        mp.optsteps_2d = options.optimize ? 5 : 0;
        mp.optsteps_3d = options.optimize ? 5 : 0;
        mp.closeedgeenable = 0;
        // Quad-dominated SURFACE mesh (exotic type): the VOLUME stays tetrahedral, but Netgen
        // lays a quad/quad8 surface mesh. Off by default. The quad->tet transition can
        // introduce pyramids, which the volume-element extraction below splits into tets.
        mp.quad_dominated = options.quad_surface ? 1 : 0;
        // NOTE: options.algorithm3d (gmsh Mesh.Algorithm3D: Delaunay/HXT) has no Netgen
        // equivalent — Netgen uses its Delaunay + advancing-front mesher unconditionally —
        // so the field is accepted for API compatibility but intentionally not mapped.

        {
            aicad::prof::phases::PhaseScope _p("generate");
            nglib::Ng_OCC_SetLocalMeshSize(sess.geo, sess.mesh, &mp);
            if (nglib::Ng_OCC_GenerateEdgeMesh(sess.geo, sess.mesh, &mp) != nglib::NG_OK)
                return fail("edge meshing failed (check that the geometry is a valid solid)");
            if (nglib::Ng_OCC_GenerateSurfaceMesh(sess.geo, sess.mesh, &mp) != nglib::NG_OK)
                return fail("surface meshing failed");
            if (nglib::Ng_GenerateVolumeMesh(sess.mesh, &mp) != nglib::NG_OK)
                return fail("volume meshing failed");
        }
        if (order2) {
            // Elevate to 10-node tets; the OCC refinement PROJECTS the new mid-edge nodes
            // onto the CAD surface (curved elements) so tet10 stress on fillets/holes is
            // accurate.
            aicad::prof::phases::PhaseScope _p("second_order");
            nglib::Ng_OCC_Generate_SecondOrder(sess.geo, sess.mesh);
        }
    } catch (const std::exception& e) {
        return fail(std::string("meshing raised: ") + e.what());
    } catch (...) {
        return fail("meshing raised an unknown exception");
    }

    // Extraction + quality fill — timed as one "extract" phase (runs to function exit;
    // destroyed before _study, so it lands inside the mesh_tet study).
    aicad::prof::phases::PhaseScope _p_extract("extract");
    // Extract nodes (1-based in Netgen) into a flat n*3 row-major array (0-based).
    const int np = nglib::Ng_GetNP(sess.mesh);
    if (np <= 0) return fail("mesh produced no points");
    out.points.resize(static_cast<size_t>(np) * 3);
    for (int i = 1; i <= np; ++i) {
        double x[3];
        nglib::Ng_GetPoint(sess.mesh, i, x);
        const size_t b = static_cast<size_t>(i - 1) * 3;
        out.points[b + 0] = x[0];
        out.points[b + 1] = x[1];
        out.points[b + 2] = x[2];
    }

    // Volume elements: keep tets of the requested order, remapping Netgen's node order to the
    // gmsh MSH_TET_10 convention the FEA solvers assume (corners pass through; mid nodes via
    // kNg2Gmsh). Netgen node indices are 1-based -> store 0-based.
    const auto want = order2 ? nglib::NG_TET10 : nglib::NG_TET;
    const int ne = nglib::Ng_GetNE(sess.mesh);
    out.tets.reserve(static_cast<size_t>(ne) * npe);
    bool any_tet = false;
    int pi[NG_VOLUME_ELEMENT_MAXPOINTS];
    for (int e = 1; e <= ne; ++e) {
        if (nglib::Ng_GetVolumeElement(sess.mesh, e, pi) != want) continue;
        any_tet = true;
        for (int k = 0; k < npe; ++k) {
            const int src = order2 ? kNg2Gmsh[k] : k;
            out.tets.push_back(pi[src] - 1);  // 1-based -> 0-based
        }
    }

    // Second pass — exotic volume elements: Netgen can emit PYRAMID (5-node) and PRISM
    // (6-node) elements in boundary-layer / quad-dominated-surface / degenerate contexts.
    // fea_static accepts only 4/10/8-node elements, so rather than silently dropping these
    // (the prior behavior — a partly pyramid/prism mesh then failed with "no tet elements"),
    // split each into tet4s and append. Only the tet4 path absorbs them: the split yields
    // 4-node tets that share the stride of a tet4 array. (In a second-order mesh Netgen
    // elevates these to quadratic forms its own Ng_GetVolumeElement reports as the default
    // NG_TET, so they never surface here as NG_PYRAMID/NG_PRISM.)
    bool any_split = false;
    if (!order2) {
        // Netgen local node order (libsrc/meshing/topology.cpp reference points):
        //   pyramid: base quad {0,1,2,3} CCW, apex 4 -> 2 tets via the 0-2 base diagonal.
        //   prism:   bottom tri {0,1,2}, top tri {3,4,5} (i+3 above i) -> 3 tets.
        static const int kPyr[2][4] = {{0, 1, 2, 4}, {0, 2, 3, 4}};
        static const int kPrism[3][4] = {{0, 1, 2, 3}, {1, 2, 3, 4}, {2, 3, 4, 5}};
        auto append_split = [&](const int* loc, const int* t) {
            const int a = loc[t[0]] - 1, b = loc[t[1]] - 1, c = loc[t[2]] - 1, d = loc[t[3]] - 1;
            // Skip a degenerate split (sliver / mis-ordered source) rather than hand FEA an
            // inverted or zero-volume tet; |6V| <= 0 flags it.
            if (std::abs(tet_volume6(out.points, a, b, c, d)) <= 0.0) return;
            out.tets.push_back(a);
            out.tets.push_back(b);
            out.tets.push_back(c);
            out.tets.push_back(d);
            any_split = true;
        };
        for (int e = 1; e <= ne; ++e) {
            const auto et = nglib::Ng_GetVolumeElement(sess.mesh, e, pi);
            if (et == nglib::NG_PYRAMID) {
                for (const auto& t : kPyr) append_split(pi, t);
            } else if (et == nglib::NG_PRISM) {
                for (const auto& t : kPrism) append_split(pi, t);
            }
        }
        if (any_split)
            out.note = "split Netgen pyramid/prism volume elements into tets";
    }

    if (!any_tet && !any_split) {
        out.ok = false;
        out.error = std::string("netgen produced no ") + (order2 ? "tet10" : "tet4") +
                    " elements for '" + step_path +
                    "'. Check that the geometry is a valid closed solid.";
        return out;
    }

    out.char_len_mm = base;
    out.n_nodes = static_cast<int>(out.points.size() / 3);
    out.n_elems = static_cast<int>(out.tets.size() / npe);
    out.mesh_type_used = order2 ? (options.quad_surface ? "tet10-quad-surface" : "tet10")
                                : (options.quad_surface ? "tet4-quad-surface" : "tet4");
    fill_quality(out);
    out.ok = true;
    return out;
}

}  // namespace aicad::sim
