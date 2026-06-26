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

#include "sim/hex_mesher.h"  // mesh_box_hex (structured hex path)
#include "geom/booleans.h"   // geom::unify_same_domain (mesh-time geometry healing)

#include <algorithm>
#include <cmath>
#include <cstddef>
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

// The Netgen meshing core (defined below); the public mesh_step wraps it with the heal-and-retry
// fallback for fused-primitive geometry (Issue 6).
MeshOut mesh_step_netgen(const std::string& step_path, const MeshOptions& options);

MeshOut mesh_step(const std::string& step_path, const MeshOptions& options) {
    // Structured hexahedral request: delegate to the OCCT box-grid mesher (no Netgen).
    if (options.structured) return mesh_box_hex(step_path, options);

    MeshOut out = mesh_step_netgen(step_path, options);
    if (out.ok) return out;

    // Robust-fallback meshing (Issue 6): the mesher failed on the raw geometry — almost always
    // a fused-primitive part whose coincident coplanar faces / sliver edges defeat tet
    // generation. Heal the shape and retry ONCE on the healed STEP before reporting failure.
    // The common (clean-geometry) path never pays this cost: it only runs after a real miss.
    std::string healed = heal_step_for_mesh(step_path);
    if (!healed.empty() && healed != step_path) {
        MeshOut out2 = mesh_step_netgen(healed, options);
        std::error_code ec;
        std::filesystem::remove(healed, ec);
        if (out2.ok) return out2;
    }
    return out;  // surface the original mesher error
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

MeshOut mesh_step_netgen(const std::string& step_path, const MeshOptions& options) {
    std::lock_guard<std::mutex> mesh_lock(mesh_mutex());  // serialize: Netgen is not thread-safe
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
    try {
        STEPControl_Reader rd;
        if (rd.ReadFile(step_path.c_str()) != IFSelect_RetDone)
            return fail("could not read the STEP (is the path valid?)");
        rd.TransferRoots();
        TopoDS_Shape shape = rd.OneShape();
        if (shape.IsNull()) return fail("STEP contained no shape");
        Bnd_Box bb;
        BRepBndLib::Add(shape, bb);
        if (bb.IsVoid()) return fail("degenerate bounding box (void)");
        double xmin, ymin, zmin, xmax, ymax, zmax;
        bb.Get(xmin, ymin, zmin, xmax, ymax, zmax);
        dx = std::fabs(xmax - xmin);
        dy = std::fabs(ymax - ymin);
        dz = std::fabs(zmax - zmin);
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
        sess.geo = nglib::Ng_OCC_Load_STEP(step_path.c_str());
        if (!sess.geo) return fail("could not load the OCC geometry from the STEP");

        sess.mesh = nglib::Ng_NewMesh();

        nglib::Ng_Meshing_Parameters mp;  // exported ctor sets Netgen's defaults
        mp.uselocalh = 1;                  // honor the local size field (size band + curvature)
        mp.maxh = max_len;
        mp.minh = min_len;
        mp.fineness = fineness;
        mp.grading = 0.3;                  // smooth 2:1 gradation between min/max
        mp.elementsperedge = 2.0;
        // Curvature-adaptive refinement around holes/fillets. Netgen's elementspercurve is
        // "elements per curvature RADIUS"; a full circle (2*pi radians) therefore gets
        // ~2*pi*elementspercurve elements, so divide curv_elems_per_circle (elements per full
        // turn) by 2*pi. Off => a low value so sizing is uniform.
        constexpr double kTwoPi = 6.283185307179586;
        mp.elementspercurve =
            (options.curvature && options.curv_elems_per_circle > 0.0)
                ? std::max(1.0, options.curv_elems_per_circle / kTwoPi)
                : 1.0;
        mp.second_order = 0;  // elevate explicitly below (with CAD-surface projection)
        // Quality optimization — Netgen's native passes (the same engine gmsh invoked via
        // OptimizeNetgen). Disabled => 0 passes.
        mp.optsurfmeshenable = options.optimize ? 1 : 0;
        mp.optvolmeshenable = options.optimize ? 1 : 0;
        mp.optsteps_2d = options.optimize ? 3 : 0;
        mp.optsteps_3d = options.optimize ? 3 : 0;
        mp.closeedgeenable = 0;
        // NOTE: options.algorithm3d (gmsh Mesh.Algorithm3D: Delaunay/HXT) has no Netgen
        // equivalent — Netgen uses its Delaunay + advancing-front mesher unconditionally —
        // so the field is accepted for API compatibility but intentionally not mapped.

        nglib::Ng_OCC_SetLocalMeshSize(sess.geo, sess.mesh, &mp);
        if (nglib::Ng_OCC_GenerateEdgeMesh(sess.geo, sess.mesh, &mp) != nglib::NG_OK)
            return fail("edge meshing failed (check that the geometry is a valid solid)");
        if (nglib::Ng_OCC_GenerateSurfaceMesh(sess.geo, sess.mesh, &mp) != nglib::NG_OK)
            return fail("surface meshing failed");
        if (nglib::Ng_GenerateVolumeMesh(sess.mesh, &mp) != nglib::NG_OK)
            return fail("volume meshing failed");
        if (order2) {
            // Elevate to 10-node tets; the OCC refinement PROJECTS the new mid-edge nodes
            // onto the CAD surface (curved elements) so tet10 stress on fillets/holes is
            // accurate.
            nglib::Ng_OCC_Generate_SecondOrder(sess.geo, sess.mesh);
        }
    } catch (const std::exception& e) {
        return fail(std::string("meshing raised: ") + e.what());
    } catch (...) {
        return fail("meshing raised an unknown exception");
    }

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

    if (!any_tet) {
        out.ok = false;
        out.error = std::string("netgen produced no ") + (order2 ? "tet10" : "tet4") +
                    " elements for '" + step_path +
                    "'. Check that the geometry is a valid closed solid.";
        return out;
    }

    out.char_len_mm = base;
    out.n_nodes = static_cast<int>(out.points.size() / 3);
    out.n_elems = static_cast<int>(out.tets.size() / npe);
    fill_quality(out);
    out.ok = true;
    return out;
}

}  // namespace aicad::sim
