#include "sim/overlay.h"

#include "prof/phases.h"  // per-phase timers (overlay generation studies)

#include <algorithm>
#include <array>
#include <atomic>
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

namespace aicad::sim {

namespace {

// ── Result-overlay size cap (T-SIM2) ──────────────────────────────────────────
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
constexpr size_t kMaxOverlayTris = 1500000;

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

// (n_verts*3) surface vertex positions (float pos -> double), like _surface_verts.
std::vector<double> surface_verts(const aicm::AicmBody& bt) {
    std::vector<double> out(bt.pos.size());
    for (size_t i = 0; i < bt.pos.size(); ++i) out[i] = static_cast<double>(bt.pos[i]);
    return out;
}

// Publish overlay AICM via shared-mem handoff (file fallback inside publish).
std::string publish_overlay_aicm(const aicm::AicmBody& bt,
                                 const std::vector<std::vector<uint8_t>>& chunks) {
    static std::atomic<unsigned long long> seq{0};
    unsigned long long n = seq.fetch_add(1);
    std::vector<uint8_t> blob = aicm::write_aicm({bt}, chunks);
    if (blob.empty()) return {};
    return aicad::aicm_handoff::publish("overlay_" + std::to_string(n), blob);
}

// double -> float vector (chunk payloads are float32, like the np.float32 cast in
// scalar_chunk/disp_chunk).
std::vector<float> to_f32(const std::vector<double>& v) {
    std::vector<float> out(v.size());
    for (size_t i = 0; i < v.size(); ++i) out[i] = static_cast<float>(v[i]);
    return out;
}

double field_max(const std::vector<double>& v) {
    double mx = -std::numeric_limits<double>::infinity();
    for (double x : v) if (x > mx) mx = x;
    return std::isfinite(mx) ? mx : 0.0;
}

double field_min(const std::vector<double>& v) {
    double mn = std::numeric_limits<double>::infinity();
    for (double x : v) if (x < mn) mn = x;
    return std::isfinite(mn) ? mn : 0.0;
}

// bbox diagonal of surface verts (||max - min||), 1.0 if empty — like overlay.py.
double bbox_diag(const std::vector<double>& surf) {
    const size_t m = surf.size() / 3;
    if (m == 0) return 1.0;
    double lo[3], hi[3];
    for (int k = 0; k < 3; ++k) {
        lo[k] = std::numeric_limits<double>::max();
        hi[k] = -std::numeric_limits<double>::max();
    }
    for (size_t i = 0; i < m; ++i)
        for (int k = 0; k < 3; ++k) {
            double c = surf[3 * i + k];
            if (c < lo[k]) lo[k] = c;
            if (c > hi[k]) hi[k] = c;
        }
    double dx = hi[0] - lo[0], dy = hi[1] - lo[1], dz = hi[2] - lo[2];
    return std::sqrt(dx * dx + dy * dy + dz * dz);
}

// Map a per-VOLUME-NODE field to the FEA boundary surface by DIRECT INDEXING through
// surf_to_vol (field_surf[i*dim+k] = field_vol[surf_to_vol[i]*dim+k]). Relies on the
// solver's per-node arrays sharing vol_points' node order (same deterministic mesh).
// Out-of-range / short fields are filled 0 rather than crashing (assert-guarded).
std::vector<double> map_to_surface_direct(const std::vector<int>& surf_to_vol,
                                          const std::vector<double>& vol_values, int dim) {
    const size_t m = surf_to_vol.size();
    const size_t d = static_cast<size_t>(dim);
    std::vector<double> out(m * d, 0.0);
    const size_t n_vals = vol_values.size() / d;  // number of source nodes
    for (size_t i = 0; i < m; ++i) {
        const int g = surf_to_vol[i];
        if (g < 0 || static_cast<size_t>(g) >= n_vals) continue;  // safe skip -> 0
        for (size_t k = 0; k < d; ++k)
            out[i * d + k] = vol_values[static_cast<size_t>(g) * d + k];
    }
    return out;
}

}  // namespace

FeaSurface fea_surface_body(const std::vector<double>& vol_points,
                            const std::vector<int>& tets, int nodes_per_elem) {
    FeaSurface fs;
    fs.body.body_id = 1;
    fs.body.name = "Body1";
    fs.body.color[0] = 0.71f;
    fs.body.color[1] = 0.76f;
    fs.body.color[2] = 0.82f;

    const bool hex = (nodes_per_elem == 8);
    const int stride = (nodes_per_elem == 10) ? 10 : (hex ? 8 : 4);
    const size_t n_tets = (stride > 0) ? (tets.size() / static_cast<size_t>(stride)) : 0;
    const size_t n_nodes = vol_points.size() / 3;
    if (n_tets == 0 || n_nodes == 0) return fs;

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
    if (!hex)
        for (size_t e = 0; e < n_tets; ++e) {
            const int* t = &tets[e * static_cast<size_t>(stride)];
            for (int f = 0; f < 4; ++f) {
                std::array<int, 3> key = {t[FACE_LOCAL[f][0]], t[FACE_LOCAL[f][1]],
                                          t[FACE_LOCAL[f][2]]};
                std::sort(key.begin(), key.end());
                ++face_count[key];
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

    // ── Per-vertex normals: accumulate each triangle's (un-normalized) geometric
    //    normal onto its 3 vertices (area-weighted), then normalize (guard -> +Z). ──
    const size_t nv = fs.body.pos.size() / 3;
    fs.body.nrm.assign(nv * 3, 0.0f);
    const size_t ntri = fs.body.idx.size() / 3;
    for (size_t ti = 0; ti < ntri; ++ti) {
        const uint32_t ia = fs.body.idx[ti * 3 + 0];
        const uint32_t ib = fs.body.idx[ti * 3 + 1];
        const uint32_t ic = fs.body.idx[ti * 3 + 2];
        const float* PA = &fs.body.pos[ia * 3];
        const float* PB = &fs.body.pos[ib * 3];
        const float* PC = &fs.body.pos[ic * 3];
        const double u[3] = {PB[0] - PA[0], PB[1] - PA[1], PB[2] - PA[2]};
        const double v[3] = {PC[0] - PA[0], PC[1] - PA[1], PC[2] - PA[2]};
        const double nx = u[1] * v[2] - u[2] * v[1];
        const double ny = u[2] * v[0] - u[0] * v[2];
        const double nz = u[0] * v[1] - u[1] * v[0];
        for (uint32_t vi : {ia, ib, ic}) {
            fs.body.nrm[vi * 3 + 0] += static_cast<float>(nx);
            fs.body.nrm[vi * 3 + 1] += static_cast<float>(ny);
            fs.body.nrm[vi * 3 + 2] += static_cast<float>(nz);
        }
    }
    for (size_t i = 0; i < nv; ++i) {
        float* N = &fs.body.nrm[i * 3];
        const double len = std::sqrt(static_cast<double>(N[0]) * N[0] +
                                     static_cast<double>(N[1]) * N[1] +
                                     static_cast<double>(N[2]) * N[2]);
        if (len > 1e-20) {
            N[0] = static_cast<float>(N[0] / len);
            N[1] = static_cast<float>(N[1] / len);
            N[2] = static_cast<float>(N[2] / len);
        } else {
            N[0] = 0.0f; N[1] = 0.0f; N[2] = 1.0f;
        }
    }
    return fs;
}

std::vector<double> map_to_surface(const std::vector<double>& surf_xyz,
                                   const std::vector<double>& vol_xyz,
                                   const std::vector<double>& vol_values, int dim) {
    const size_t m = surf_xyz.size() / 3;
    const size_t n = vol_xyz.size() / 3;
    std::vector<double> out(m * static_cast<size_t>(dim), 0.0);
    if (n == 0) return out;
    for (size_t i = 0; i < m; ++i) {
        const double sx = surf_xyz[3 * i + 0];
        const double sy = surf_xyz[3 * i + 1];
        const double sz = surf_xyz[3 * i + 2];
        // Nearest volume node, first-minimum tie-break (np.argmin returns first min).
        double best = std::numeric_limits<double>::infinity();
        size_t best_j = 0;
        for (size_t j = 0; j < n; ++j) {
            const double dx = sx - vol_xyz[3 * j + 0];
            const double dy = sy - vol_xyz[3 * j + 1];
            const double dz = sz - vol_xyz[3 * j + 2];
            const double d2 = dx * dx + dy * dy + dz * dz;
            if (d2 < best) {
                best = d2;
                best_j = j;
            }
        }
        for (int k = 0; k < dim; ++k)
            out[i * static_cast<size_t>(dim) + k] =
                vol_values[best_j * static_cast<size_t>(dim) + k];
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
    (void)step_path;  // surface now comes from the FEA boundary mesh, not the STEP.
    try {
        aicad::prof::phases::StudyScope _study("overlay_static");  // total overlay-build time
        FeaSurface fs = fea_surface_body(vol_points, tets, nodes_per_elem);
        aicm::AicmBody bt = fs.body;
        if (!overlay_within_cap(bt, "static_overlay")) return std::string();
        std::vector<double> surf = surface_verts(bt);

        std::vector<double> vm_s = map_to_surface_direct(fs.surf_to_vol, von_mises, 1);
        std::vector<double> dm_s = map_to_surface_direct(fs.surf_to_vol, disp_mag, 1);
        std::vector<double> dvec_s = map_to_surface_direct(fs.surf_to_vol, displacement, 3);

        // fos = clip(yield/vm where vm>1e-9 else 15, 0..15).
        std::vector<double> fos_s(vm_s.size());
        for (size_t i = 0; i < vm_s.size(); ++i) {
            double f = (vm_s[i] > 1e-9) ? (yield_mpa / vm_s[i]) : 15.0;
            if (f < 0.0) f = 0.0;
            if (f > 15.0) f = 15.0;
            fos_s[i] = f;
        }

        // scale_hint: peak displacement ~10% of bbox diagonal at scale 1.
        const double bb_diag = bbox_diag(surf);
        const double peak_dm = field_max(dm_s);
        const double peak = (!dm_s.empty() && peak_dm > 1e-12) ? peak_dm : 1.0;
        const double scale_hint = (peak > 1e-12) ? (0.1 * bb_diag / peak) : 1.0;

        // vmin/vmax sourcing matches overlay.py: from summary, else field max.
        const double vmax_vm =
            summary.contains("max_von_mises_mpa") && summary["max_von_mises_mpa"].is_number()
                ? summary["max_von_mises_mpa"].get<double>()
                : field_max(vm_s);
        const double vmax_dm =
            summary.contains("max_disp_mm") && summary["max_disp_mm"].is_number()
                ? summary["max_disp_mm"].get<double>()
                : field_max(dm_s);

        std::vector<std::vector<uint8_t>> chunks;
        chunks.push_back(aicm::scalar_chunk(1, "von_mises", to_f32(vm_s), 0.0f,
                                            static_cast<float>(vmax_vm)));
        chunks.push_back(aicm::scalar_chunk(1, "displacement_mag", to_f32(dm_s), 0.0f,
                                            static_cast<float>(vmax_dm)));
        chunks.push_back(aicm::scalar_chunk(1, "fos", to_f32(fos_s), 0.0f, 5.0f));
        chunks.push_back(
            aicm::disp_chunk(1, static_cast<float>(scale_hint), to_f32(dvec_s)));

        // ── Additive scalar chunks (mapped from the volume to the surface) ────
        // Signed principal stresses use the field min/max as the legend range.
        std::vector<double> p1_s = map_to_surface_direct(fs.surf_to_vol, principal1, 1);
        std::vector<double> p3_s = map_to_surface_direct(fs.surf_to_vol, principal3, 1);
        std::vector<double> ms_s = map_to_surface_direct(fs.surf_to_vol, max_shear, 1);
        chunks.push_back(aicm::scalar_chunk(1, "p1", to_f32(p1_s),
                                            static_cast<float>(field_min(p1_s)),
                                            static_cast<float>(field_max(p1_s))));
        chunks.push_back(aicm::scalar_chunk(1, "p3", to_f32(p3_s),
                                            static_cast<float>(field_min(p3_s)),
                                            static_cast<float>(field_max(p3_s))));
        chunks.push_back(aicm::scalar_chunk(1, "max_shear", to_f32(ms_s), 0.0f,
                                            static_cast<float>(field_max(ms_s))));

        // Per-component displacement, sliced out of the already-mapped dvec_s (dim=3).
        const size_t m_surf = dvec_s.size() / 3;
        std::vector<double> dx_s(m_surf), dy_s(m_surf), dz_s(m_surf);
        for (size_t i = 0; i < m_surf; ++i) {
            dx_s[i] = dvec_s[3 * i + 0];
            dy_s[i] = dvec_s[3 * i + 1];
            dz_s[i] = dvec_s[3 * i + 2];
        }
        chunks.push_back(aicm::scalar_chunk(1, "disp_x", to_f32(dx_s),
                                            static_cast<float>(field_min(dx_s)),
                                            static_cast<float>(field_max(dx_s))));
        chunks.push_back(aicm::scalar_chunk(1, "disp_y", to_f32(dy_s),
                                            static_cast<float>(field_min(dy_s)),
                                            static_cast<float>(field_max(dy_s))));
        chunks.push_back(aicm::scalar_chunk(1, "disp_z", to_f32(dz_s),
                                            static_cast<float>(field_min(dz_s)),
                                            static_cast<float>(field_max(dz_s))));

        // ── Additive failure-criterion chunks ────────────────────────────────
        // Derived from the already-mapped surface principal stresses, so they
        // are sampled at the same surface vertices as p1/p3 (no extra kNN pass).
        //   tresca        = p1 - p3        (max-shear-stress / Tresca equivalent)
        //   abs_principal = max(|p1|,|p3|) (max-normal-stress / Rankine)
        //   fos_tresca    = clamp(yield / max(tresca,1e-9), 0, 15)
        //   fos_rankine   = clamp(yield / max(abs_principal,1e-9), 0, 15)
        std::vector<double> tr_s(p1_s.size()), ap_s(p1_s.size());
        std::vector<double> fos_tr(p1_s.size()), fos_rk(p1_s.size());
        for (size_t i = 0; i < p1_s.size(); ++i) {
            const double tr = p1_s[i] - p3_s[i];
            const double ap = std::max(std::abs(p1_s[i]), std::abs(p3_s[i]));
            tr_s[i] = tr;
            ap_s[i] = ap;
            double ft = yield_mpa / std::max(tr, 1e-9);
            double fr = yield_mpa / std::max(ap, 1e-9);
            if (ft < 0.0) ft = 0.0;
            if (ft > 15.0) ft = 15.0;
            if (fr < 0.0) fr = 0.0;
            if (fr > 15.0) fr = 15.0;
            fos_tr[i] = ft;
            fos_rk[i] = fr;
        }
        chunks.push_back(aicm::scalar_chunk(1, "tresca", to_f32(tr_s), 0.0f,
                                            static_cast<float>(field_max(tr_s))));
        chunks.push_back(aicm::scalar_chunk(1, "abs_principal", to_f32(ap_s), 0.0f,
                                            static_cast<float>(field_max(ap_s))));
        chunks.push_back(aicm::scalar_chunk(1, "fos_tresca", to_f32(fos_tr), 0.0f, 5.0f));
        chunks.push_back(aicm::scalar_chunk(1, "fos_rankine", to_f32(fos_rk), 0.0f, 5.0f));

        return publish_overlay_aicm(bt, chunks);
    } catch (...) {
        return std::string();
    }
}

std::string fatigue_overlay(const std::string& step_path,
                            const std::vector<double>& vol_points,
                            const std::vector<int>& tets, int nodes_per_elem,
                            const std::vector<double>& fatigue_fos,
                            const std::vector<double>& life_log10,
                            const std::vector<double>& von_mises_peak) {
    (void)step_path;  // surface now comes from the FEA boundary mesh, not the STEP.
    try {
        aicad::prof::phases::StudyScope _study("overlay_fatigue");  // total overlay-build time
        FeaSurface fs = fea_surface_body(vol_points, tets, nodes_per_elem);
        aicm::AicmBody bt = fs.body;
        if (!overlay_within_cap(bt, "fatigue_overlay")) return std::string();

        std::vector<double> ff_s = map_to_surface_direct(fs.surf_to_vol, fatigue_fos, 1);
        std::vector<double> life_s = map_to_surface_direct(fs.surf_to_vol, life_log10, 1);
        std::vector<double> vm_s = map_to_surface_direct(fs.surf_to_vol, von_mises_peak, 1);

        std::vector<std::vector<uint8_t>> chunks;
        chunks.push_back(aicm::scalar_chunk(1, "fatigue_fos", to_f32(ff_s), 0.0f, 5.0f));
        chunks.push_back(aicm::scalar_chunk(1, "life_log10", to_f32(life_s),
                                            static_cast<float>(field_min(life_s)),
                                            static_cast<float>(field_max(life_s))));
        chunks.push_back(aicm::scalar_chunk(1, "von_mises", to_f32(vm_s), 0.0f,
                                            static_cast<float>(field_max(vm_s))));

        return publish_overlay_aicm(bt, chunks);
    } catch (...) {
        return std::string();
    }
}

std::string modal_overlay(const std::string& step_path,
                          const std::vector<double>& vol_points,
                          const std::vector<int>& tets, int nodes_per_elem,
                          const std::vector<double>& mode_shape, double freq_hz) {
    (void)freq_hz;  // freq is metadata only (Python takes it but does not encode it).
    (void)step_path;  // surface now comes from the FEA boundary mesh, not the STEP.
    try {
        aicad::prof::phases::StudyScope _study("overlay_modal");  // total overlay-build time
        FeaSurface fs = fea_surface_body(vol_points, tets, nodes_per_elem);
        aicm::AicmBody bt = fs.body;
        if (!overlay_within_cap(bt, "modal_overlay")) return std::string();
        std::vector<double> surf = surface_verts(bt);

        std::vector<double> dvec_s = map_to_surface_direct(fs.surf_to_vol, mode_shape, 3);
        const size_t m = dvec_s.size() / 3;
        std::vector<double> mag(m);
        for (size_t i = 0; i < m; ++i) {
            double x = dvec_s[3 * i + 0], y = dvec_s[3 * i + 1], z = dvec_s[3 * i + 2];
            mag[i] = std::sqrt(x * x + y * y + z * z);
        }
        const double peak_mag = field_max(mag);
        const double norm = (!mag.empty() && peak_mag > 1e-12) ? peak_mag : 1.0;

        const double bb_diag = bbox_diag(surf);
        const double scale_hint = (norm > 1e-12) ? (0.15 * bb_diag / norm) : 1.0;

        std::vector<std::vector<uint8_t>> chunks;
        chunks.push_back(aicm::scalar_chunk(1, "mode_amplitude", to_f32(mag), 0.0f,
                                            static_cast<float>(norm)));
        chunks.push_back(
            aicm::disp_chunk(1, static_cast<float>(scale_hint), to_f32(dvec_s)));

        return publish_overlay_aicm(bt, chunks);
    } catch (...) {
        return std::string();
    }
}

}  // namespace aicad::sim
