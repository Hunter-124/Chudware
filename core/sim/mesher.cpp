// mesher.cpp — see mesher.h. The FEA tet mesher (wraps gmsh 4.15.2).
//
// gmsh session sequence:
//   initialize -> Verbosity=1 -> model.add -> occ.importShapes -> occ.synchronize
//   -> getBoundingBox -> base size from the bbox DIAGONAL + fineness
//   -> MeshSizeMin/Max + MeshSizeFromCurvature (curvature-adaptive) + Extend/FromPoints
//   -> ElementOrder (curved tet10 via SecondOrderLinear=0) -> Algorithm3D
//   -> Optimize + OptimizeNetgen -> mesh.generate(3) -> optional Netgen optimize pass
//   -> write(.msh) -> getNodes -> tag->0-based remap -> getElements(dim=3), tet4/tet10.
//
// The Python oracle (engine/sim/mesher.py) that this once mirrored bit-for-bit has been
// deleted, so the sizing/quality strategy here is free to be a professional one rather
// than a parity-frozen reimplementation.
//
// IMPLEMENTATION NOTE — we use the C API (gmshc.h), NOT the C++ API (gmsh.h):
// the official prebuilt Windows SDK's libgmsh is built with MinGW/GCC, so its C++
// symbols carry Itanium (GCC) name mangling that MSVC's import resolver can never
// match (verified: dumpbin shows `_ZN4gmsh...`). The `extern "C"` C API
// (gmshInitialize/...) is ABI-stable across compilers and links cleanly from MSVC.
// The Python oracle's `gmsh` wheel calls these SAME C entry points (lib/gmsh.py ->
// ctypes lib.gmshInitialize ...), so this is the identical code path, not a reimpl.

#include "sim/mesher.h"

#include "sim/hex_mesher.h"  // mesh_box_hex (structured hex path)

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <filesystem>
#include <mutex>
#include <limits>
#include <unordered_map>

// gmshc.h is a pure-C header with NO `extern "C"` guard of its own — wrap it so the
// C++ compiler emits C linkage (the DLL exports plain C names: gmshInitialize, …).
extern "C" {
#include <gmshc.h>
}

namespace aicad::sim {

namespace {

// RAII guard so gmshFinalize() runs on every exit path — mirrors Python's
// try/finally: finalize() that always runs even when generate() raises.
struct GmshGuard {
    ~GmshGuard() {
        int ierr = 0;
        gmshFinalize(&ierr);  // best-effort; never throw out of a destructor
    }
};

// gmsh keeps PROCESS-GLOBAL state (one model registry) and is NOT thread-safe: two
// concurrent gmshInitialize/generate sessions corrupt that state and crash. The host can
// trigger overlapping meshes — e.g. a background study solve (SimClient thread) while the
// user hits "Preview mesh" (main thread), or rapid re-runs across a study-type switch. This
// mutex serializes the entire session so those paths queue instead of racing.
std::mutex& gmsh_mutex() {
    static std::mutex m;
    return m;
}

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

MeshOut mesh_step(const std::string& step_path, const MeshOptions& options) {
    // Structured hexahedral request: delegate to the OCCT box-grid mesher (no gmsh).
    if (options.structured) return mesh_box_hex(step_path, options);

    std::lock_guard<std::mutex> gmsh_lock(gmsh_mutex());  // serialize: gmsh is not thread-safe
    MeshOut out;

    // Second-order (tet10) selection. order==1 keeps the prior tet4 path
    // BYTE-IDENTICAL (same gmsh option sequence, MSH element type 4). order==2 sets
    // Mesh.ElementOrder=2 and extracts MSH type 11 (10-node tets). Any other value
    // is treated as order 1 (defensive).
    const bool order2 = (options.order == 2);
    const int elem_order = order2 ? 2 : 1;
    const int want_etype = order2 ? 11 : 4;   // MSH_TET_10 vs MSH_TET_4
    const int npe = order2 ? 10 : 4;
    out.nodes_per_elem = npe;

    // Resolve the primary sizing control. A legacy `density` (>= 0) wins and maps
    // directly onto fineness (both use "higher = finer"); otherwise use `fineness`.
    const double fineness = std::min(
        1.0, std::max(0.0, options.density >= 0.0 ? options.density : options.fineness));

    // Derive a .msh output path next to the step file (Path.with_suffix(".msh")).
    std::filesystem::path mesh_path_fs = std::filesystem::path(step_path);
    mesh_path_fs.replace_extension(".msh");
    const std::string mesh_path = mesh_path_fs.string();

    int ierr = 0;

    // gmsh.initialize(): argc=0, argv=NULL, readConfigFiles=1, run=0 (the wheel's
    // gmsh.initialize() defaults — lib/gmsh.py passes exactly these).
    gmshInitialize(0, nullptr, 1, 0, &ierr);
    if (ierr != 0) {
        out.ok = false;
        out.error = "gmsh: initialize failed";
        return out;
    }
    GmshGuard guard;  // gmshFinalize() on any return past this point

    auto fail = [&](const std::string& msg) -> MeshOut {
        out.ok = false;
        out.error = "gmsh: " + msg;
        return out;
    };

    gmshOptionSetNumber("General.Verbosity", 1, &ierr);
    if (ierr != 0) return fail("setNumber(Verbosity)");

    gmshModelAdd("model", &ierr);
    if (ierr != 0) return fail("model.add");

    int* out_dim_tags = nullptr;
    std::size_t out_dim_tags_n = 0;
    // highestDimOnly=1, format="" — the C++/Python default (highestDimOnly=true).
    gmshModelOccImportShapes(step_path.c_str(), &out_dim_tags, &out_dim_tags_n, 1, "",
                             &ierr);
    if (out_dim_tags) gmshFree(out_dim_tags);
    if (ierr != 0) return fail("occ.importShapes (is the STEP path valid?)");

    gmshModelOccSynchronize(&ierr);
    if (ierr != 0) return fail("occ.synchronize");

    // Bounding box -> base element size, derived from the DIAGONAL (not the smallest
    // side). Keying off the diagonal gives a sane element count across very different
    // proportions: a thin plate, a long bracket and a cube all mesh reasonably instead
    // of the smallest-dimension heuristic exploding thin parts.
    double xmin, ymin, zmin, xmax, ymax, zmax;
    gmshModelGetBoundingBox(-1, -1, &xmin, &ymin, &zmin, &xmax, &ymax, &zmax, &ierr);
    if (ierr != 0) return fail("getBoundingBox");
    const double dx = std::fabs(xmax - xmin), dy = std::fabs(ymax - ymin),
                 dz = std::fabs(zmax - zmin);
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

    double min_len =
        (std::isfinite(options.min_size_mm) && options.min_size_mm > 0.0)
            ? options.min_size_mm
            : base * 0.1;
    double max_len =
        (std::isfinite(options.max_size_mm) && options.max_size_mm > 0.0)
            ? options.max_size_mm
            : base;
    if (max_len < min_len) std::swap(max_len, min_len);

    // Element size bounds (modern option names; the old CharacteristicLength* are
    // deprecated aliases of these).
    gmshOptionSetNumber("Mesh.MeshSizeMin", min_len, &ierr);
    if (ierr != 0) return fail("setNumber(MeshSizeMin)");
    gmshOptionSetNumber("Mesh.MeshSizeMax", max_len, &ierr);
    if (ierr != 0) return fail("setNumber(MeshSizeMax)");
    // Spread the boundary sizing into the interior for a smooth gradation.
    gmshOptionSetNumber("Mesh.MeshSizeExtendFromBoundary", 1, &ierr);
    if (ierr != 0) return fail("setNumber(MeshSizeExtendFromBoundary)");
    gmshOptionSetNumber("Mesh.MeshSizeFromPoints", 1, &ierr);
    if (ierr != 0) return fail("setNumber(MeshSizeFromPoints)");
    // Curvature-adaptive refinement — refine to ~curv_elems_per_circle elements per
    // full turn of curvature so holes/fillets (stress concentrations) are resolved.
    const double curv = (options.curvature && options.curv_elems_per_circle > 0.0)
                            ? options.curv_elems_per_circle
                            : 0.0;
    gmshOptionSetNumber("Mesh.MeshSizeFromCurvature", curv, &ierr);
    if (ierr != 0) return fail("setNumber(MeshSizeFromCurvature)");

    gmshOptionSetNumber("Mesh.ElementOrder", elem_order, &ierr);
    if (ierr != 0) return fail("setNumber(ElementOrder)");
    if (order2) {
        // Curved (non-linear) second-order elements: snap mid-side nodes to the CAD
        // surface so tet10 stress on fillets/holes is accurate; optimize the high-order
        // nodes to avoid invalid curved elements.
        gmshOptionSetNumber("Mesh.SecondOrderLinear", 0, &ierr);
        if (ierr != 0) return fail("setNumber(SecondOrderLinear)");
        gmshOptionSetNumber("Mesh.HighOrderOptimize", 1, &ierr);
        if (ierr != 0) return fail("setNumber(HighOrderOptimize)");
    }
    gmshOptionSetNumber("Mesh.Algorithm3D", options.algorithm3d > 0 ? options.algorithm3d : 1, &ierr);
    if (ierr != 0) return fail("setNumber(Algorithm3D)");
    // Quality optimization: gmsh's own pass plus the stronger Netgen optimizer.
    gmshOptionSetNumber("Mesh.Optimize", options.optimize ? 1 : 0, &ierr);
    if (ierr != 0) return fail("setNumber(Optimize)");
    gmshOptionSetNumber("Mesh.OptimizeNetgen", options.optimize ? 1 : 0, &ierr);
    if (ierr != 0) return fail("setNumber(OptimizeNetgen)");

    gmshModelMeshGenerate(3, &ierr);
    if (ierr != 0) return fail("mesh.generate(3)");
    // NOTE: do NOT run an explicit post-generate gmshModelMeshOptimize() pass here. The
    // Mesh.Optimize / Mesh.OptimizeNetgen flags above already optimize the volume mesh
    // DURING generate(3) (on the linear topology, before 2nd-order elevation). Running a
    // Netgen optimize pass AFTER generate operates on the already-elevated tet10 mesh and
    // wipes the high-order elements ("no tet10 elements"), so it is intentionally omitted.
    gmshWrite(mesh_path.c_str(), &ierr);
    if (ierr != 0) return fail("write(.msh)");

    // Extract nodes (coords flat -> reshape (-1,3)).
    std::size_t* node_tags = nullptr;
    std::size_t node_tags_n = 0;
    double* coord = nullptr;
    std::size_t coord_n = 0;
    double* param_coord = nullptr;
    std::size_t param_coord_n = 0;
    gmshModelMeshGetNodes(&node_tags, &node_tags_n, &coord, &coord_n, &param_coord,
                          &param_coord_n, -1, -1, 0, 1, &ierr);
    if (ierr != 0) {
        if (node_tags) gmshFree(node_tags);
        if (coord) gmshFree(coord);
        if (param_coord) gmshFree(param_coord);
        return fail("mesh.getNodes");
    }
    out.points.assign(coord, coord + coord_n);  // flat n*3 row-major float64

    // Contiguous tag -> 0-based index map (gmsh tags may not be 0-based).
    std::unordered_map<std::size_t, int> tag_to_idx;
    tag_to_idx.reserve(node_tags_n * 2);
    for (std::size_t i = 0; i < node_tags_n; ++i) {
        tag_to_idx[node_tags[i]] = static_cast<int>(i);
    }
    if (node_tags) gmshFree(node_tags);
    if (coord) gmshFree(coord);
    if (param_coord) gmshFree(param_coord);

    // Elements of dim 3; keep only tet4 (element type 4).
    int* elem_types = nullptr;
    std::size_t elem_types_n = 0;
    std::size_t** elem_tags = nullptr;        // [block][i]
    std::size_t* elem_tags_n = nullptr;       // length of each block
    std::size_t elem_tags_nn = 0;             // number of blocks
    std::size_t** elem_node_tags = nullptr;   // [block][i]
    std::size_t* elem_node_tags_n = nullptr;  // length of each block
    std::size_t elem_node_tags_nn = 0;        // number of blocks
    gmshModelMeshGetElements(&elem_types, &elem_types_n, &elem_tags, &elem_tags_n,
                             &elem_tags_nn, &elem_node_tags, &elem_node_tags_n,
                             &elem_node_tags_nn, 3, -1, &ierr);

    bool any_tet = false;
    if (ierr == 0) {
        for (std::size_t b = 0; b < elem_types_n; ++b) {
            if (elem_types[b] != want_etype) continue;  // tet4 (4) or tet10 (11) only
            any_tet = true;
            const std::size_t* entags = elem_node_tags[b];
            const std::size_t cnt = elem_node_tags_n[b];
            out.tets.reserve(out.tets.size() + cnt);
            for (std::size_t t = 0; t < cnt; ++t) {
                out.tets.push_back(tag_to_idx.at(entags[t]));
            }
        }
    }

    // Free the nested C-API arrays (each inner pointer + each outer pointer).
    if (elem_types) gmshFree(elem_types);
    if (elem_tags) {
        for (std::size_t b = 0; b < elem_tags_nn; ++b)
            if (elem_tags[b]) gmshFree(elem_tags[b]);
        gmshFree(elem_tags);
    }
    if (elem_tags_n) gmshFree(elem_tags_n);
    if (elem_node_tags) {
        for (std::size_t b = 0; b < elem_node_tags_nn; ++b)
            if (elem_node_tags[b]) gmshFree(elem_node_tags[b]);
        gmshFree(elem_node_tags);
    }
    if (elem_node_tags_n) gmshFree(elem_node_tags_n);

    if (ierr != 0) return fail("mesh.getElements");

    if (!any_tet) {
        out.ok = false;
        out.error = std::string("gmsh produced no ") + (order2 ? "tet10" : "tet4") +
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
