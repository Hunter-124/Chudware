#include "sim/overlay.h"

#include "prof/phases.h"  // per-phase timers (overlay generation studies)

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <limits>
#include <map>
#include <string>
#include <vector>

#include "geom/aicm.h"
#include "util/aicm_handoff.h"

namespace chudware::sim {

namespace {

// ── Result-overlay size cap ───────────────────────────────────────────────────
// The result overlay is the FEA BOUNDARY surface: its triangles are uploaded to a
// single GL VBO, re-deformed on the CPU on every deform-scale change, and each vertex
// carries ~15 per-node scalar fields. That triangle budget is what bounds interactive
// render + upload cost. We CANNOT decimate to honour the cap: every surface vertex is
// tied to a volume node via surf_to_vol, and the per-node solver fields are mapped by
// DIRECT INDEXING through it — dropping vertices would corrupt that mapping and silently
// falsify the plotted field. So the cap is enforced as a HARD GUARD, not a decimation:
// above it we refuse to emit the overlay (return "" — the study still returns its scalar
// summary + narration; only the spatial contour is withheld, and the UI already handles
// an empty overlay path by falling back to the editable model mesh). The number below is
// deliberately generous — a correctly-set mesh_fineness never approaches it; hitting it
// means the mesh is pathologically fine and the user should lower mesh_fineness (or use
// mesh_order 1 / draft) to get a contour. ~1.5M boundary tris ≈ 750k surface verts ≈
// 45 MB of f32 positions + normals + 15 scalar fields, an interactive-render ceiling.
// The cap itself, kMaxOverlayTris, is declared in sim/overlay.h so every AICM result
// surface writer (FEA overlays, topology surfaces) enforces the same render budget.

// True when the built boundary surface is within the render/upload budget. `where` is a
// short label for the diagnostic log (best-effort, cap breach is rare).
bool overlay_within_cap(const aicm::AicmBody& bt, const char* where) {
    const size_t ntri = bt.idx.size() / 3;
    if (ntri <= kMaxOverlayTris) return true;
    std::fprintf(stderr,
                 "[sim] %s: result surface has %zu boundary triangles (cap %zu) — "
                 "skipping contour overlay; lower mesh_fineness or use draft (order 1) "
                 "meshing for a spatial plot.\n",
                 where, ntri, kMaxOverlayTris);
    return false;
}

// Surface vertex positions promoted to double for deformation metadata.
std::vector<double> surface_verts(const aicm::AicmBody& bt) {
    std::vector<double> out(bt.pos.size());
    for (size_t i = 0; i < bt.pos.size(); ++i) out[i] = static_cast<double>(bt.pos[i]);
    return out;
}


bool finite_f32(double x) {
    return std::isfinite(x) &&
           std::abs(x) <= static_cast<double>(std::numeric_limits<float>::max());
}

bool to_f32_exact(const std::vector<double>& v, std::vector<float>& out) {
    out.clear();
    out.reserve(v.size());
    for (double x : v) {
        if (!finite_f32(x)) {
            out.clear();
            return false;
        }
        out.push_back(static_cast<float>(x));
    }
    return true;
}

bool finite_field(const std::vector<double>& v, size_t nodes, size_t dim) {
    if (dim == 0 || nodes > std::numeric_limits<size_t>::max() / dim ||
        v.size() != nodes * dim)
        return false;
    for (double x : v)
        if (!finite_f32(x)) return false;
    return true;
}

struct ScalarRange {
    float min = 0.0f;
    float max = 1.0f;
};

bool scalar_range(const std::vector<double>& v, bool include_zero, ScalarRange& range) {
    if (v.empty()) return false;
    double mn = std::numeric_limits<double>::infinity();
    double mx = -std::numeric_limits<double>::infinity();
    for (double x : v) {
        if (!finite_f32(x)) return false;
        mn = std::min(mn, x);
        mx = std::max(mx, x);
    }
    if (include_zero) {
        mn = std::min(mn, 0.0);
        mx = std::max(mx, 0.0);
    }
    range.min = static_cast<float>(mn);
    range.max = static_cast<float>(mx);
    if (!(range.max > range.min)) {
        if (range.min == 0.0f) {
            range.max = 1.0f;
        } else {
            range.min = std::nextafter(range.min, -std::numeric_limits<float>::infinity());
            range.max = std::nextafter(range.max, std::numeric_limits<float>::infinity());
        }
    }
    return std::isfinite(range.min) && std::isfinite(range.max) && range.max > range.min;
}

// bbox diagonal of finite surface verts. Invalid/degenerate geometry returns 0.
double bbox_diag(const std::vector<double>& surf) {
    if (surf.empty() || surf.size() % 3 != 0) return 0.0;
    double lo[3] = {surf[0], surf[1], surf[2]};
    double hi[3] = {surf[0], surf[1], surf[2]};
    for (double c : surf)
        if (!std::isfinite(c)) return 0.0;
    for (size_t i = 1; i < surf.size() / 3; ++i)
        for (int k = 0; k < 3; ++k) {
            const double c = surf[3 * i + static_cast<size_t>(k)];
            lo[k] = std::min(lo[k], c);
            hi[k] = std::max(hi[k], c);
        }
    const double dx = hi[0] - lo[0], dy = hi[1] - lo[1], dz = hi[2] - lo[2];
    const double diag = std::sqrt(dx * dx + dy * dy + dz * dz);
    return std::isfinite(diag) ? diag : 0.0;
}

// Exact per-node mapping. A short/malformed/non-finite field invalidates the
// overlay; silently zero-filling would attach values to the wrong surface nodes.
bool map_to_surface_direct(const std::vector<int>& surf_to_vol,
                           const std::vector<double>& vol_values, size_t vol_nodes,
                           size_t dim, std::vector<double>& out) {
    if (!finite_field(vol_values, vol_nodes, dim)) return false;
    if (surf_to_vol.size() > std::numeric_limits<size_t>::max() / dim) return false;
    out.resize(surf_to_vol.size() * dim);
    for (size_t i = 0; i < surf_to_vol.size(); ++i) {
        const int g = surf_to_vol[i];
        if (g < 0 || static_cast<size_t>(g) >= vol_nodes) {
            out.clear();
            return false;
        }
        for (size_t k = 0; k < dim; ++k)
            out[i * dim + k] = vol_values[static_cast<size_t>(g) * dim + k];
    }
    return true;
}

bool append_scalar(std::vector<std::vector<uint8_t>>& chunks, const char* name,
                   const std::vector<double>& values, bool include_zero) {
    std::vector<float> f32;
    ScalarRange range;
    if (!to_f32_exact(values, f32) || !scalar_range(values, include_zero, range))
        return false;
    chunks.push_back(aicm::scalar_chunk(1, name, f32, range.min, range.max));
    return !chunks.back().empty();
}

bool append_displacement(std::vector<std::vector<uint8_t>>& chunks, double scale_hint,
                         const std::vector<double>& displacement) {
    if (!finite_f32(scale_hint) || !(scale_hint > 0.0)) return false;
    std::vector<float> f32;
    if (!to_f32_exact(displacement, f32) || f32.empty()) return false;
    chunks.push_back(aicm::disp_chunk(1, static_cast<float>(scale_hint), f32));
    return !chunks.back().empty();
}

}  // namespace

FeaSurface fea_surface_body(const std::vector<double>& vol_points,
                            const std::vector<int>& tets, int nodes_per_elem) {
    FeaSurface fs;
    if (nodes_per_elem != 4 && nodes_per_elem != 8 && nodes_per_elem != 10)
        return fs;
    const bool hex = nodes_per_elem == 8;
    const size_t stride = static_cast<size_t>(nodes_per_elem);
    if (vol_points.empty() || vol_points.size() % 3 != 0 ||
        tets.empty() || tets.size() % stride != 0)
        return fs;
    const size_t n_nodes = vol_points.size() / 3;
    const size_t n_tets = tets.size() / stride;
    for (double c : vol_points)
        if (!finite_f32(c)) return FeaSurface{};
    auto corner_jacobian = [&](size_t e) {
        const int* el = &tets[e * stride];
        const int i0 = el[0], i1 = el[1];
        const int i2 = hex ? el[3] : el[2];
        const int i3 = hex ? el[4] : el[3];
        const double* p0 = &vol_points[static_cast<size_t>(i0) * 3];
        const double* p1 = &vol_points[static_cast<size_t>(i1) * 3];
        const double* p2 = &vol_points[static_cast<size_t>(i2) * 3];
        const double* p3 = &vol_points[static_cast<size_t>(i3) * 3];
        const double a[3] = {p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2]};
        const double b[3] = {p2[0] - p0[0], p2[1] - p0[1], p2[2] - p0[2]};
        const double c[3] = {p3[0] - p0[0], p3[1] - p0[1], p3[2] - p0[2]};
        return a[0] * (b[1] * c[2] - b[2] * c[1]) -
               a[1] * (b[0] * c[2] - b[2] * c[0]) +
               a[2] * (b[0] * c[1] - b[1] * c[0]);
    };
    for (size_t e = 0; e < n_tets; ++e) {
        for (size_t j = 0; j < stride; ++j) {
            const int g = tets[e * stride + j];
            if (g < 0 || static_cast<size_t>(g) >= n_nodes) return FeaSurface{};
            for (size_t k = 0; k < j; ++k)
                if (tets[e * stride + k] == g) return FeaSurface{};
        }
        const double jacobian = corner_jacobian(e);
        if (!std::isfinite(jacobian) || jacobian == 0.0) return FeaSurface{};
    }

    fs.body.body_id = 1;
    fs.body.name = "Body1";
    fs.body.color[0] = 0.71f;
    fs.body.color[1] = 0.76f;
    fs.body.color[2] = 0.82f;

    // The 4 triangular faces of a tet (corner locals) + the opposite corner local for
    // each (used to orient the face outward).
    static const int FACE_LOCAL[4][3] = {{0, 1, 2}, {0, 1, 3}, {0, 2, 3}, {1, 2, 3}};
    static const int FACE_OPP[4] = {3, 2, 1, 0};
    // tet10 mid-edge locals per face (m_ab, m_bc, m_ca on the face corners A,B,C).
    // gmsh tet10 mids: 4=edge(0,1),5=edge(1,2),6=edge(2,0),7=edge(3,0),8=edge(3,2),9=edge(3,1).
    //   F0 (0,1,2): m01=4, m12=5, m20=6
    //   F1 (0,1,3): m01=4, m13=9, m30=7
    //   F2 (0,2,3): m02=6, m23=8, m30=7
    //   F3 (1,2,3): m12=5, m23=8, m31=9
    static const int FACE_MID[4][3] = {{4, 5, 6}, {4, 9, 7}, {6, 8, 7}, {5, 8, 9}};

    auto pt = [&](int g, double out[3]) {
        out[0] = vol_points[static_cast<size_t>(g) * 3 + 0];
        out[1] = vol_points[static_cast<size_t>(g) * 3 + 1];
        out[2] = vol_points[static_cast<size_t>(g) * 3 + 2];
    };

    // ── Pass 1: count occurrences of each tet-face key (sorted global corner ids). ──
    std::map<std::array<int, 3>, int> face_count;
    std::map<std::array<int, 3>, std::array<int, 3>> face_mids;
    if (!hex)
        for (size_t e = 0; e < n_tets; ++e) {
            const int* t = &tets[e * stride];
            for (int f = 0; f < 4; ++f) {
                std::array<int, 3> key = {t[FACE_LOCAL[f][0]], t[FACE_LOCAL[f][1]],
                                          t[FACE_LOCAL[f][2]]};
                std::sort(key.begin(), key.end());
                if (++face_count[key] > 2) return FeaSurface{};
                if (nodes_per_elem == 10) {
                    std::array<int, 3> mids = {t[FACE_MID[f][0]], t[FACE_MID[f][1]],
                                               t[FACE_MID[f][2]]};
                    std::sort(mids.begin(), mids.end());
                    auto [it, inserted] = face_mids.emplace(key, mids);
                    if (!inserted && it->second != mids) return FeaSurface{};
                }
            }
        }

    // ── Pass 2: for each boundary face, decide outward winding + emit triangles. ──
    // Global node id -> compacted surface vertex index (deduped, first-seen order).
    std::map<int, int> g2s;
    auto compact = [&](int g) -> int {
        auto it = g2s.find(g);
        if (it != g2s.end()) return it->second;
        const int s = static_cast<int>(fs.surf_to_vol.size());
        g2s.emplace(g, s);
        fs.surf_to_vol.push_back(g);
        fs.body.pos.push_back(static_cast<float>(vol_points[static_cast<size_t>(g) * 3 + 0]));
        fs.body.pos.push_back(static_cast<float>(vol_points[static_cast<size_t>(g) * 3 + 1]));
        fs.body.pos.push_back(static_cast<float>(vol_points[static_cast<size_t>(g) * 3 + 2]));
        return s;
    };
    // Emit one triangle by GLOBAL ids (with the already-decided winding flip applied).
    auto emit_tri = [&](int gA, int gB, int gC, bool flip) {
        const int a = compact(gA), b = compact(gB), c = compact(gC);
        if (flip) {
            fs.body.idx.push_back(static_cast<uint32_t>(a));
            fs.body.idx.push_back(static_cast<uint32_t>(c));
            fs.body.idx.push_back(static_cast<uint32_t>(b));
        } else {
            fs.body.idx.push_back(static_cast<uint32_t>(a));
            fs.body.idx.push_back(static_cast<uint32_t>(b));
            fs.body.idx.push_back(static_cast<uint32_t>(c));
        }
    };

    for (size_t e = 0; !hex && e < n_tets; ++e) {
        const int* t = &tets[e * static_cast<size_t>(stride)];
        for (int f = 0; f < 4; ++f) {
            const int gA = t[FACE_LOCAL[f][0]];
            const int gB = t[FACE_LOCAL[f][1]];
            const int gC = t[FACE_LOCAL[f][2]];
            std::array<int, 3> key = {gA, gB, gC};
            std::sort(key.begin(), key.end());
            if (face_count[key] != 1) continue;  // interior face (shared) -> skip

            // Outward-winding decision: normal of (A,B,C); flip if it points TOWARD the
            // opposite corner (dot(n, opp - centroid) > 0 means n faces the interior).
            double A[3], B[3], C[3], O[3];
            pt(gA, A); pt(gB, B); pt(gC, C); pt(t[FACE_OPP[f]], O);
            const double e1[3] = {B[0] - A[0], B[1] - A[1], B[2] - A[2]};
            const double e2[3] = {C[0] - A[0], C[1] - A[1], C[2] - A[2]};
            const double nx = e1[1] * e2[2] - e1[2] * e2[1];
            const double ny = e1[2] * e2[0] - e1[0] * e2[2];
            const double nz = e1[0] * e2[1] - e1[1] * e2[0];
            const double cen[3] = {(A[0] + B[0] + C[0]) / 3.0, (A[1] + B[1] + C[1]) / 3.0,
                                   (A[2] + B[2] + C[2]) / 3.0};
            const double dot = nx * (O[0] - cen[0]) + ny * (O[1] - cen[1]) +
                               nz * (O[2] - cen[2]);
            const bool flip = (dot > 0.0);

            if (stride == 4) {
                emit_tri(gA, gB, gC, flip);
            } else {
                // tet10: subdivide using the 3 mid-edge global nodes. mAB on A-B,
                // mBC on B-C, mCA on C-A. Sub-tris: (A,mAB,mCA),(mAB,B,mBC),
                // (mCA,mBC,C),(mAB,mBC,mCA) — same flip applied to all four.
                const int mAB = t[FACE_MID[f][0]];
                const int mBC = t[FACE_MID[f][1]];
                const int mCA = t[FACE_MID[f][2]];
                emit_tri(gA, mAB, mCA, flip);
                emit_tri(mAB, gB, mBC, flip);
                emit_tri(mCA, mBC, gC, flip);
                emit_tri(mAB, mBC, mCA, flip);
            }
        }
    }

    // ── hex8 boundary surface: 6 quad faces; a quad on exactly one element is on the
    //    boundary; emit it as 2 triangles (winding from the element centroid). ──
    if (hex) {
        static const int HEX_FACE[6][4] = {{0, 1, 2, 3}, {4, 5, 6, 7}, {0, 1, 5, 4},
                                           {2, 3, 7, 6}, {0, 3, 7, 4}, {1, 2, 6, 5}};
        std::map<std::array<int, 4>, int> qcount;
        for (size_t e = 0; e < n_tets; ++e) {
            const int* t = &tets[e * static_cast<size_t>(stride)];
            for (int f = 0; f < 6; ++f) {
                std::array<int, 4> key = {t[HEX_FACE[f][0]], t[HEX_FACE[f][1]],
                                          t[HEX_FACE[f][2]], t[HEX_FACE[f][3]]};
                std::sort(key.begin(), key.end());
                ++qcount[key];
            }
        }
        for (const auto& [key, count] : qcount) {
            (void)key;
            if (count > 2) return FeaSurface{};
        }
        for (size_t e = 0; e < n_tets; ++e) {
            const int* t = &tets[e * static_cast<size_t>(stride)];
            double cen[3] = {0, 0, 0};
            for (int j = 0; j < 8; ++j) {
                double p[3];
                pt(t[j], p);
                cen[0] += p[0] / 8.0; cen[1] += p[1] / 8.0; cen[2] += p[2] / 8.0;
            }
            for (int f = 0; f < 6; ++f) {
                const int gA = t[HEX_FACE[f][0]], gB = t[HEX_FACE[f][1]];
                const int gC = t[HEX_FACE[f][2]], gD = t[HEX_FACE[f][3]];
                std::array<int, 4> key = {gA, gB, gC, gD};
                std::sort(key.begin(), key.end());
                if (qcount[key] != 1) continue;  // interior quad (shared) -> skip
                double A[3], B[3], C[3];
                pt(gA, A); pt(gB, B); pt(gC, C);
                const double e1[3] = {B[0] - A[0], B[1] - A[1], B[2] - A[2]};
                const double e2[3] = {C[0] - A[0], C[1] - A[1], C[2] - A[2]};
                const double nx = e1[1] * e2[2] - e1[2] * e2[1];
                const double ny = e1[2] * e2[0] - e1[0] * e2[2];
                const double nz = e1[0] * e2[1] - e1[1] * e2[0];
                // flip if the face normal points toward the element centroid (interior).
                const double dot = nx * (cen[0] - A[0]) + ny * (cen[1] - A[1]) +
                                   nz * (cen[2] - A[2]);
                const bool flip = (dot > 0.0);
                emit_tri(gA, gB, gC, flip);  // quad ABCD -> tris ABC + ACD
                emit_tri(gA, gC, gD, flip);
            }
        }
    }

    // Per-vertex normals use double accumulation so large-but-finite coordinates
    // cannot overflow a float accumulator. Degenerate rendered triangles fail closed.
    const size_t nv = fs.body.pos.size() / 3;
    const size_t ntri = fs.body.idx.size() / 3;
    if (nv == 0 || ntri == 0 || fs.surf_to_vol.size() != nv) return FeaSurface{};
    std::vector<double> normal_accum(nv * 3, 0.0);
    for (size_t ti = 0; ti < ntri; ++ti) {
        const uint32_t ia = fs.body.idx[ti * 3 + 0];
        const uint32_t ib = fs.body.idx[ti * 3 + 1];
        const uint32_t ic = fs.body.idx[ti * 3 + 2];
        if (ia >= nv || ib >= nv || ic >= nv) return FeaSurface{};
        const float* PA = &fs.body.pos[ia * 3];
        const float* PB = &fs.body.pos[ib * 3];
        const float* PC = &fs.body.pos[ic * 3];
        const double u[3] = {PB[0] - PA[0], PB[1] - PA[1], PB[2] - PA[2]};
        const double v[3] = {PC[0] - PA[0], PC[1] - PA[1], PC[2] - PA[2]};
        const double nx = u[1] * v[2] - u[2] * v[1];
        const double ny = u[2] * v[0] - u[0] * v[2];
        const double nz = u[0] * v[1] - u[1] * v[0];
        const double area2 = nx * nx + ny * ny + nz * nz;
        if (!std::isfinite(area2) || !(area2 > 0.0)) return FeaSurface{};
        for (uint32_t vi : {ia, ib, ic}) {
            normal_accum[vi * 3 + 0] += nx;
            normal_accum[vi * 3 + 1] += ny;
            normal_accum[vi * 3 + 2] += nz;
        }
    }
    fs.body.nrm.resize(nv * 3);
    for (size_t i = 0; i < nv; ++i) {
        const double nx = normal_accum[i * 3 + 0];
        const double ny = normal_accum[i * 3 + 1];
        const double nz = normal_accum[i * 3 + 2];
        const double len = std::sqrt(nx * nx + ny * ny + nz * nz);
        if (!std::isfinite(len) || !(len > 0.0)) return FeaSurface{};
        fs.body.nrm[i * 3 + 0] = static_cast<float>(nx / len);
        fs.body.nrm[i * 3 + 1] = static_cast<float>(ny / len);
        fs.body.nrm[i * 3 + 2] = static_cast<float>(nz / len);
    }
    return fs;
}

std::vector<double> map_to_surface(const std::vector<double>& surf_xyz,
                                   const std::vector<double>& vol_xyz,
                                   const std::vector<double>& vol_values, int dim) {
    if (dim <= 0 || surf_xyz.size() % 3 != 0 || vol_xyz.empty() ||
        vol_xyz.size() % 3 != 0)
        return {};
    const size_t m = surf_xyz.size() / 3;
    const size_t n = vol_xyz.size() / 3;
    const size_t d = static_cast<size_t>(dim);
    if (!finite_field(vol_values, n, d) ||
        m > std::numeric_limits<size_t>::max() / d)
        return {};
    for (double c : surf_xyz)
        if (!finite_f32(c)) return {};
    for (double c : vol_xyz)
        if (!finite_f32(c)) return {};

    std::vector<double> out(m * d);
    for (size_t i = 0; i < m; ++i) {
        const double sx = surf_xyz[3 * i + 0];
        const double sy = surf_xyz[3 * i + 1];
        const double sz = surf_xyz[3 * i + 2];
        double best = std::numeric_limits<double>::infinity();
        size_t best_j = 0;
        for (size_t j = 0; j < n; ++j) {
            const double dx = sx - vol_xyz[3 * j + 0];
            const double dy = sy - vol_xyz[3 * j + 1];
            const double dz = sz - vol_xyz[3 * j + 2];
            const double d2 = dx * dx + dy * dy + dz * dz;
            if (!std::isfinite(d2)) return {};
            if (d2 < best) {
                best = d2;
                best_j = j;
            }
        }
        for (size_t k = 0; k < d; ++k)
            out[i * d + k] = vol_values[best_j * d + k];
    }
    return out;
}

std::string static_overlay(const std::string& step_path,
                           const std::vector<double>& vol_points,
                           const std::vector<int>& tets, int nodes_per_elem,
                           const std::vector<double>& von_mises,
                           const std::vector<double>& disp_mag,
                           const std::vector<double>& displacement,
                           const std::vector<double>& principal1,
                           const std::vector<double>& principal3,
                           const std::vector<double>& max_shear,
                           double yield_mpa, const nlohmann::json& summary) {
    (void)step_path;
    (void)summary;  // rounded summaries must never define exact contour ranges.
    try {
        chudware::prof::phases::StudyScope _study("overlay_static");
        if (!std::isfinite(yield_mpa) || !(yield_mpa > 0.0)) return {};
        FeaSurface fs = fea_surface_body(vol_points, tets, nodes_per_elem);
        aicm::AicmBody bt = fs.body;
        const size_t vol_nodes = vol_points.size() / 3;
        if (bt.pos.empty() || fs.surf_to_vol.empty() ||
            !overlay_within_cap(bt, "static_overlay"))
            return {};

        std::vector<double> vm_s, dm_s, dvec_s;
        if (!map_to_surface_direct(fs.surf_to_vol, von_mises, vol_nodes, 1, vm_s) ||
            !map_to_surface_direct(fs.surf_to_vol, disp_mag, vol_nodes, 1, dm_s) ||
            !map_to_surface_direct(fs.surf_to_vol, displacement, vol_nodes, 3, dvec_s))
            return {};
        for (size_t i = 0; i < dm_s.size(); ++i) {
            const double magnitude =
                std::hypot(dvec_s[3 * i + 0],
                           std::hypot(dvec_s[3 * i + 1], dvec_s[3 * i + 2]));
            const double tolerance = 1e-10 * std::max(1.0, magnitude);
            if (!finite_f32(magnitude) || std::abs(dm_s[i] - magnitude) > tolerance)
                return {};
        }
        for (double x : vm_s)
            if (x < 0.0) return {};
        for (double x : dm_s)
            if (x < 0.0) return {};

        std::vector<double> fos_s(vm_s.size());
        for (size_t i = 0; i < vm_s.size(); ++i)
            fos_s[i] = std::clamp(vm_s[i] > 1e-9 ? yield_mpa / vm_s[i] : 15.0,
                                  0.0, 15.0);

        const double bb_diag = bbox_diag(surface_verts(bt));
        if (!(bb_diag > 0.0)) return {};
        const double peak_dm = *std::max_element(dm_s.begin(), dm_s.end());
        const double scale_hint = peak_dm > 1e-12 ? 0.1 * bb_diag / peak_dm : 1.0;

        std::vector<std::vector<uint8_t>> chunks;
        if (!append_scalar(chunks, "von_mises", vm_s, true) ||
            !append_scalar(chunks, "displacement_mag", dm_s, true) ||
            !append_scalar(chunks, "fos", fos_s, true) ||
            !append_displacement(chunks, scale_hint, dvec_s))
            return {};

        const size_t m = fs.surf_to_vol.size();
        std::vector<double> dx_s(m), dy_s(m), dz_s(m);
        for (size_t i = 0; i < m; ++i) {
            dx_s[i] = dvec_s[3 * i + 0];
            dy_s[i] = dvec_s[3 * i + 1];
            dz_s[i] = dvec_s[3 * i + 2];
        }
        if (!append_scalar(chunks, "disp_x", dx_s, false) ||
            !append_scalar(chunks, "disp_y", dy_s, false) ||
            !append_scalar(chunks, "disp_z", dz_s, false))
            return {};

        std::vector<double> p1_s, p3_s;
        if (!principal1.empty()) {
            if (!map_to_surface_direct(fs.surf_to_vol, principal1, vol_nodes, 1, p1_s) ||
                !append_scalar(chunks, "p1", p1_s, false))
                return {};
        }
        if (!principal3.empty()) {
            if (!map_to_surface_direct(fs.surf_to_vol, principal3, vol_nodes, 1, p3_s) ||
                !append_scalar(chunks, "p3", p3_s, false))
                return {};
        }
        if (!max_shear.empty()) {
            std::vector<double> ms_s;
            if (!map_to_surface_direct(fs.surf_to_vol, max_shear, vol_nodes, 1, ms_s) ||
                !append_scalar(chunks, "max_shear", ms_s, true))
                return {};
        }

        // Derived criteria exist only when both principal fields exist. Empty
        // optional inputs must not become fabricated all-zero contour fields.
        if (!p1_s.empty() && !p3_s.empty()) {
            std::vector<double> tr_s(m), ap_s(m), fos_tr(m), fos_rk(m);
            for (size_t i = 0; i < m; ++i) {
                const double tr = p1_s[i] - p3_s[i];
                const double ap = std::max(std::abs(p1_s[i]), std::abs(p3_s[i]));
                if (!finite_f32(tr) || !finite_f32(ap) || tr < 0.0) return {};
                tr_s[i] = tr;
                ap_s[i] = ap;
                fos_tr[i] = std::clamp(yield_mpa / std::max(tr, 1e-9), 0.0, 15.0);
                fos_rk[i] = std::clamp(yield_mpa / std::max(ap, 1e-9), 0.0, 15.0);
            }
            if (!append_scalar(chunks, "tresca", tr_s, true) ||
                !append_scalar(chunks, "abs_principal", ap_s, true) ||
                !append_scalar(chunks, "fos_tresca", fos_tr, true) ||
                !append_scalar(chunks, "fos_rankine", fos_rk, true))
                return {};
        }
        return publish_overlay_aicm(bt, chunks);
    } catch (...) {
        return {};
    }
}

std::string fatigue_overlay(const std::string& step_path,
                            const std::vector<double>& vol_points,
                            const std::vector<int>& tets, int nodes_per_elem,
                            const std::vector<double>& fatigue_fos,
                            const std::vector<double>& life_log10,
                            const std::vector<double>& von_mises_peak) {
    (void)step_path;
    try {
        chudware::prof::phases::StudyScope _study("overlay_fatigue");
        FeaSurface fs = fea_surface_body(vol_points, tets, nodes_per_elem);
        aicm::AicmBody bt = fs.body;
        const size_t vol_nodes = vol_points.size() / 3;
        if (bt.pos.empty() || fs.surf_to_vol.empty() ||
            !overlay_within_cap(bt, "fatigue_overlay"))
            return {};

        std::vector<double> ff_s, life_s, vm_s;
        if (!map_to_surface_direct(fs.surf_to_vol, fatigue_fos, vol_nodes, 1, ff_s) ||
            !map_to_surface_direct(fs.surf_to_vol, life_log10, vol_nodes, 1, life_s) ||
            !map_to_surface_direct(fs.surf_to_vol, von_mises_peak, vol_nodes, 1, vm_s))
            return {};
        for (double x : ff_s)
            if (x < 0.0) return {};
        for (double x : vm_s)
            if (x < 0.0) return {};

        std::vector<std::vector<uint8_t>> chunks;
        if (!append_scalar(chunks, "fatigue_fos", ff_s, true) ||
            !append_scalar(chunks, "life_log10", life_s, false) ||
            !append_scalar(chunks, "von_mises", vm_s, true))
            return {};
        return publish_overlay_aicm(bt, chunks);
    } catch (...) {
        return {};
    }
}

std::string modal_overlay(const std::string& step_path,
                          const std::vector<double>& vol_points,
                          const std::vector<int>& tets, int nodes_per_elem,
                          const std::vector<double>& mode_shape, double freq_hz) {
    (void)step_path;
    try {
        chudware::prof::phases::StudyScope _study("overlay_modal");
        if (!std::isfinite(freq_hz) || freq_hz < 0.0) return {};
        FeaSurface fs = fea_surface_body(vol_points, tets, nodes_per_elem);
        aicm::AicmBody bt = fs.body;
        const size_t vol_nodes = vol_points.size() / 3;
        if (bt.pos.empty() || fs.surf_to_vol.empty() ||
            !overlay_within_cap(bt, "modal_overlay"))
            return {};

        std::vector<double> dvec_s;
        if (!map_to_surface_direct(fs.surf_to_vol, mode_shape, vol_nodes, 3, dvec_s))
            return {};
        const size_t m = fs.surf_to_vol.size();
        std::vector<double> mag(m);
        for (size_t i = 0; i < m; ++i) {
            mag[i] = std::hypot(dvec_s[3 * i + 0],
                                std::hypot(dvec_s[3 * i + 1], dvec_s[3 * i + 2]));
            if (!finite_f32(mag[i])) return {};
        }
        const double peak_mag = *std::max_element(mag.begin(), mag.end());
        const double bb_diag = bbox_diag(surface_verts(bt));
        if (!(bb_diag > 0.0)) return {};
        const double scale_hint = peak_mag > 1e-12 ? 0.15 * bb_diag / peak_mag : 1.0;

        std::vector<std::vector<uint8_t>> chunks;
        if (!append_scalar(chunks, "mode_amplitude", mag, true) ||
            !append_displacement(chunks, scale_hint, dvec_s))
            return {};
        return publish_overlay_aicm(bt, chunks);
    } catch (...) {
        return {};
    }
}

}  // namespace chudware::sim
