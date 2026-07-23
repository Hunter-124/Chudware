// fea_run.cpp — see fea_run.h. The combined mesh+resolve+solve glue.
#include "sim/fea_run.h"

#include "prof/phases.h"

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <functional>
#include <set>
#include <iterator>
#include <limits>
#include <list>
#include <mutex>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "sim/fea_modal.h"
#include "sim/fea_static.h"
#include "sim/fea_thermal.h"
#include "sim/mesher.h"
#include "sim/sim_cache.h"
#include "util/resource_governor.h"

using nlohmann::json;

namespace chudware::sim {

namespace {

// Round half-away-from-zero to `nd` decimals, matching Python's round() closely
// enough for the summary fields (values are positive magnitudes here). Python's
// round() is banker's rounding, but the oracle's parity gate compares the
// summary at rel 1e-5, far coarser than the rounding step, so a standard
// half-away round reproduces the displayed value within tolerance. We keep the
// rounding ONLY to mirror the oracle's reported precision.
double round_nd(double v, int nd) {
    double scale = std::pow(10.0, nd);
    return std::round(v * scale) / scale;
}

// Pull a flat double array from a JSON node (array of numbers); empty if absent.
std::vector<double> json_doubles(const json& j) {
    std::vector<double> v;
    if (j.is_array()) {
        v.reserve(j.size());
        for (const auto& x : j) v.push_back(x.get<double>());
    }
    return v;
}

// Pull a flat int array from a JSON node.
std::vector<int> json_ints(const json& j) {
    std::vector<int> v;
    if (j.is_array()) {
        v.reserve(j.size());
        for (const auto& x : j) v.push_back(x.get<int>());
    }
    return v;
}

// Flatten a faceset's points field ([[x,y,z],...] OR an already-flat [x,y,z,...])
// into a flat k*3 sample array.
std::vector<double> faceset_sample(const json& points_node) {
    std::vector<double> sample;
    if (!points_node.is_array()) return sample;
    for (const auto& p : points_node) {
        if (p.is_array()) {
            for (const auto& c : p) sample.push_back(c.get<double>());
        } else {
            sample.push_back(p.get<double>());  // tolerate a pre-flattened list
        }
    }
    return sample;
}

// Extract face_normal and perp_tol from a faceset JSON object.
// Returns true when the normal is present and valid (all 3 components set).
// face_normal out: [nx, ny, nz], perp_tol out: mm threshold in normal direction.
bool faceset_normal(const json& fs, std::array<double, 3>& face_normal, double& perp_tol) {
    if (!fs.contains("face_normal") || !fs["face_normal"].is_array() ||
        fs["face_normal"].size() < 3)
        return false;
    const auto& nj = fs["face_normal"];
    face_normal = {nj[0].get<double>(), nj[1].get<double>(), nj[2].get<double>()};
    perp_tol = fs.value("perp_tol_mm", 1.5);
    return true;
}

// Resolve the mesh: inline points/tets if present, else mesh the STEP file.
// Returns true on success (fills points/tets), false with `err` set otherwise.
// nodes_per_elem is set to 4 (tet4) or 10 (tet10): for an inline mesh it is taken
// from study["nodes_per_elem"] (default 4); for a STEP mesh it follows the chosen
// mesh_order (study["mesh_order"], default 1).
bool resolve_mesh(const json& study, std::vector<double>& points, std::vector<int>& tets,
                  int& nodes_per_elem, std::string& mesh_type_used, std::string& note,
                  std::string& err) {
    nodes_per_elem = 4;
    mesh_type_used.clear();
    note.clear();
    // A present step_path ALWAYS re-meshes with the current options. Inline points/tets
    // are only used as a fallback when there is no step_path, so a cached/stale inline
    // mesh can never silently override the GUI mesh controls.
    const bool has_step = study.contains("step_path") && study["step_path"].is_string() &&
                          !study["step_path"].get<std::string>().empty();
    if (!has_step && study.contains("points") && study["points"].is_array() &&
        study.contains("tets") && study["tets"].is_array()) {
        points = json_doubles(study["points"]);
        tets = json_ints(study["tets"]);
        if (points.empty() || tets.empty()) {
            err = "inline mesh has empty points/tets";
            return false;
        }
        nodes_per_elem = study.value("nodes_per_elem", 4);
        return true;
    }
    if (has_step) {
        std::string step_path = study["step_path"].get<std::string>();
        MeshOptions opts;
        // fineness is the primary control; `density` is read as a legacy alias (the
        // mesher maps a >=0 density onto fineness with the same higher=finer sense).
        opts.fineness = study.value("fineness", 0.5);
        opts.density = study.value("density", -1.0);
        opts.order = study.value("mesh_order", study.value("order", 1));
        opts.curvature = study.value("curvature", true);
        opts.curv_elems_per_circle = study.value("curv_elems_per_circle", 12.0);
        opts.target_size_mm = study.value("target_size_mm", 0.0);
        opts.min_size_mm = study.value("min_size_mm", 0.0);
        opts.max_size_mm = study.value("max_size_mm", 0.0);
        opts.optimize = study.value("optimize", true);
        opts.algorithm3d = study.value("algorithm3d", 1);
        opts.structured = study.value("structured", study.value("mesh_type", std::string()) == "hex");
        // Exotic-type + auto-pick controls (Simple/Advanced mode). Defaults preserve existing
        // behavior: auto_type off; structured-hex falls back to tet on a non-box part.
        opts.auto_type = study.value("auto_type", false);
        opts.quad_surface = study.value("quad_surface", false);
        opts.fallback_to_tet = study.value("fallback_to_tet", true);
        opts.thin_wall_layers = study.value("thin_wall_layers", 4.0);
        MeshOut m = mesh_step_cached(step_path, opts);
        if (!m.ok) {
            err = "mesh failed: " + m.error;
            return false;
        }
        points = m.points;
        tets = m.tets;
        nodes_per_elem = m.nodes_per_elem;
        mesh_type_used = m.mesh_type_used;
        note = m.note;
        return true;
    }
    err = "study must have inline points/tets or a step_path";
    return false;
}

// ── Work-equivalent (consistent) surface-load distribution ───────────────────────
// A uniform surface traction does NOT split equally across the nodes of a quadratic
// (tet10) face: the work-equivalent rule is corner=0, each mid-side=1/3 of the face's
// total (verified: ∫ N_corner dA = 0, ∫ N_midside dA = A/3 for a flat tri6). The prior
// code lumped the whole face onto the 3 CORNER nodes (and never touched the mid-side
// nodes that actually carry the quadratic load), which is why an axial FORCE produced a
// large corner stress spike and a PRESSURE on a tet10 bar read δ/σ several-fold high.
//
// We accumulate, per loaded boundary face, the consistent nodal weights w_i = ∫ N_i dA
// and the area-weighted outward normal, then:
//   • force  load: distribute the TOTAL force F by w_i / Σw (the resultant stays exactly
//                  F, but the spatial distribution is correct so no spurious corner spike).
//   • pressure load: add p · (∫ N_i dA) · n̂ per node (the textbook consistent pressure
//                    load), the area-weighted form of the same weights.
// For a LINEAR face (tet4 tri3 / hex8 quad) every node weight is A/n, so the tet4/hex8
// behavior is byte-identical to the old equal split (parity preserved).
struct FaceTab {
    int corner[4];  // corner local node indices (3 used for tets, 4 for hex)
    int mid[3];     // tet10 mid-edge local node indices (unused for tet4/hex8)
};
// tet faces with the matching tet10 mid-edge local node per edge of the face (mesher edge
// order {0,1}=4 {1,2}=5 {2,0}=6 {3,0}=7 {3,2}=8 {3,1}=9).
static const FaceTab kTetFaces[4] = {
    {{0, 1, 2, -1}, {4, 5, 6}},  // edges (0,1)=4 (1,2)=5 (2,0)=6
    {{0, 1, 3, -1}, {4, 9, 7}},  // edges (0,1)=4 (1,3)=9 (3,0)=7
    {{0, 2, 3, -1}, {6, 8, 7}},  // edges (0,2)=6 (2,3)=8 (3,0)=7
    {{1, 2, 3, -1}, {5, 8, 9}},  // edges (1,2)=5 (2,3)=8 (3,1)=9
};
static const int kHexFaces[6][4] = {{0, 1, 2, 3}, {4, 5, 6, 7}, {0, 1, 5, 4},
                                    {2, 3, 7, 6}, {0, 3, 7, 4}, {1, 2, 6, 5}};

// Accumulate the consistent boundary integral over every element face whose CORNER nodes
// are ALL in `node_set`. For each loaded face it adds, per node:
//   • node_w[i]   += ∫ N_i dA                  (scalar tributary, for the force split)
//   • node_nw[3i] += (∫ N_i dA) · n̂_face       (vector pressure weight ∫ N_i n̂ dA — the
//                                               per-face outward normal so a CURVED loaded
//                                               surface gets the right directional load).
// Returns the loaded boundary area. Handles tet4/tet10/hex8. The mid-side weighting
// (corner 0, mid-side A/3) is the tri6 work-equivalent rule; tet4/hex8 keep the equal
// per-corner split, so their behavior is byte-identical to the prior code.
double accumulate_face_weights(const std::vector<double>& points, const std::vector<int>& tets,
                               int nodes_per_elem, const std::unordered_set<int>& node_set,
                               std::vector<double>& node_w, std::vector<double>& node_nw) {
    // P5: a boundary face needs ALL 3 (tet) / 4 (hex) corners in the set — under 3 nodes
    // no face can qualify, so the loaded area is exactly 0 and the whole element sweep is
    // a no-op. Exact short-circuit (the caller's w_sum fallback branch fires, as before);
    // saves O(n_loads * n_elems) on oracle-style many-single-node-load studies.
    if (node_set.size() < 3) return 0.0;
    const bool hexEl = (nodes_per_elem == 8);
    const bool tet10 = (nodes_per_elem == 10);
    const int n_faces = hexEl ? 6 : 4;
    const int fk = hexEl ? 4 : 3;  // corners per face
    const size_t stride = static_cast<size_t>(nodes_per_elem);
    const size_t m = tets.size() / stride;
    double total_area = 0.0;
    for (size_t e = 0; e < m; ++e) {
        const int* el = &tets[e * stride];
        for (int f = 0; f < n_faces; ++f) {
            const int* fc = hexEl ? kHexFaces[f] : kTetFaces[f].corner;
            int cn[4];
            bool all_in = true;
            for (int i = 0; i < fk; ++i) {
                cn[i] = el[fc[i]];
                if (!node_set.count(cn[i])) { all_in = false; break; }
            }
            if (!all_in) continue;
            // Area-weighted normal via fan triangulation from the first corner.
            const double* p0 = &points[static_cast<size_t>(cn[0]) * 3];
            double nvx = 0.0, nvy = 0.0, nvz = 0.0;
            for (int i = 1; i < fk - 1; ++i) {
                const double* pa = &points[static_cast<size_t>(cn[i]) * 3];
                const double* pb = &points[static_cast<size_t>(cn[i + 1]) * 3];
                const double ax = pa[0] - p0[0], ay = pa[1] - p0[1], az = pa[2] - p0[2];
                const double bx = pb[0] - p0[0], by = pb[1] - p0[1], bz = pb[2] - p0[2];
                nvx += 0.5 * (ay * bz - az * by);
                nvy += 0.5 * (az * bx - ax * bz);
                nvz += 0.5 * (ax * by - ay * bx);
            }
            const double area = std::sqrt(nvx * nvx + nvy * nvy + nvz * nvz);
            if (area < 1e-12) continue;
            double nx = nvx / area, ny = nvy / area, nz = nvz / area;
            // Orient the fan normal OUTWARD. The face-table windings (kTetFaces/kHexFaces)
            // are not consistently CCW-from-outside, so the raw fan normal can point into
            // the element for some faces — mixed signs across a loaded surface corrupt the
            // pressure resultant (measured 585% error on a plain bar). A boundary face's
            // outward normal always points AWAY from its own element's centroid, so flip
            // when it doesn't. Corner-node centroid is enough — exact for straight-edged
            // faces and never near-tangent for any non-degenerate element.
            {
                const int nc = hexEl ? 8 : 4;  // corner nodes of the element
                double cx = 0.0, cy = 0.0, cz = 0.0;
                for (int i = 0; i < nc; ++i) {
                    const double* p = &points[static_cast<size_t>(el[i]) * 3];
                    cx += p[0]; cy += p[1]; cz += p[2];
                }
                cx /= nc; cy /= nc; cz /= nc;
                double fx = 0.0, fy = 0.0, fz = 0.0;
                for (int i = 0; i < fk; ++i) {
                    const double* p = &points[static_cast<size_t>(cn[i]) * 3];
                    fx += p[0]; fy += p[1]; fz += p[2];
                }
                fx /= fk; fy /= fk; fz /= fk;
                if (nx * (fx - cx) + ny * (fy - cy) + nz * (fz - cz) < 0.0) {
                    nx = -nx; ny = -ny; nz = -nz;
                }
            }
            auto add = [&](int node, double w) {
                node_w[static_cast<size_t>(node)] += w;
                node_nw[static_cast<size_t>(node) * 3 + 0] += w * nx;
                node_nw[static_cast<size_t>(node) * 3 + 1] += w * ny;
                node_nw[static_cast<size_t>(node) * 3 + 2] += w * nz;
            };
            if (tet10) {
                // tri6 consistent weights: corners 0, each mid-side A/3.
                const FaceTab& ft = kTetFaces[f];
                for (int i = 0; i < 3; ++i) add(el[ft.mid[i]], area / 3.0);
            } else {
                // tri3 (tet4) / quad (hex8): equal A/fk per corner (== the old split).
                for (int i = 0; i < fk; ++i) add(cn[i], area / static_cast<double>(fk));
            }
            total_area += area;
        }
    }
    return total_area;
}

// Complete a tet10 loaded-face node set with its MID-SIDE (mid-edge) nodes.
//
// WHY: nodes_in_faceset() resolves the load faceset GEOMETRICALLY — a node is kept iff it
// lies within tol of one of the CAD face's triangulation sample points. For a flat end
// face the CAD triangulation is just the 4 face corners, so on a real tet10 mesh only
// the FEA corner nodes (and the handful of mid-side nodes that happen to fall within tol of
// a corner sample) get picked; the mid-side nodes sitting in the face INTERIOR are dropped.
// But the tri6 work-equivalent rule (accumulate_face_weights) puts ALL the consistent load
// on the mid-side nodes (corner weight = 0, each mid-side = A/3). So `w_sum`, summed over
// the corner-dominated geometric set, collapses toward 0 → the work-consistent branch is
// skipped / divides by ~0 and the force falls back to an equal per-corner split, which is
// both wrong (puts load where the consistent rule says 0) AND non-convergent (a uniform
// axial traction read ~21× / 2.1× high, mesh-fineness dependent). See Bug Z1.
//
// FIX: a mid-edge node belongs to the loaded face exactly when BOTH of its endpoint corner
// nodes are already in the set. We walk the tet10 connectivity and add every such mid-edge
// node. Topologically exact, mesh-independent, and adds nothing for tet4/hex8 (no mid
// nodes). After this, the mid-side nodes carrying the A/3 weights are IN node_ids, so
// w_sum == loaded face area and F is split F/3 onto each mid-side node (resultant = F).
void add_midside_face_nodes(const std::vector<int>& tets, int nodes_per_elem,
                            std::unordered_set<int>& node_set) {
    if (nodes_per_elem != 10) return;  // tet4 / hex8 have no mid-side nodes — unchanged.
    // P5: an edge completes only when BOTH endpoint corners are in the set — with fewer
    // than 2 nodes nothing can ever be added, so skip the full element sweep. Exact
    // short-circuit; matters because oracle-style consistent-load studies carry hundreds
    // of single-node loads, each of which used to pay this whole-mesh scan.
    if (node_set.size() < 2) return;
    const size_t stride = 10;
    const size_t m = tets.size() / stride;
    // Mesher edge order: local 4={0,1} 5={1,2} 6={2,0} 7={3,0} 8={3,2} 9={3,1}.
    static const int kEdgeEnds[6][3] = {{4, 0, 1}, {5, 1, 2}, {6, 2, 0},
                                        {7, 3, 0}, {8, 3, 2}, {9, 3, 1}};
    for (size_t e = 0; e < m; ++e) {
        const int* el = &tets[e * stride];
        for (const auto& ee : kEdgeEnds) {
            const int a = el[ee[1]], b = el[ee[2]];
            if (node_set.count(a) && node_set.count(b)) node_set.insert(el[ee[0]]);
        }
    }
}

// ── Governor admission: projected peak memory for a sparse FEA solve ─────────────
// Triplet assembly storage is exact: (dof_per_node*npe)^2 entries/element × 16 B
// (Eigen Triplet<double> = 8 B value + 2×4 B indices). The assembled CSC matrix is
// bounded above by half the triplets (duplicates merge). Factor fill-in is the honest
// unknown and the real killer — CHOLMOD's supernodal fill on bulky 3D solids was
// MEASURED at multi-GB (see the CHOLMOD note in core/CMakeLists.txt) — so it enters
// as a multiplier of K, env-tunable via CHUDWARE_FEA_FILL for hosts where the default
// proves loose/tight. Result fields (~20 doubles/node) are minor but included.
uint64_t fea_estimate_bytes(size_t n_elems, size_t n_nodes, int npe, int dof_per_node) {
    const double band = static_cast<double>(dof_per_node * npe);
    const double triplets = static_cast<double>(n_elems) * band * band * 16.0;
    double fill = 6.0;
    if (const char* e = std::getenv("CHUDWARE_FEA_FILL")) {
        const double v = std::atof(e);
        if (v > 0.0) fill = v;
    }
    const double K = triplets * 0.5;
    const double fields = static_cast<double>(n_nodes) * 20.0 * 8.0;
    return static_cast<uint64_t>(triplets + K + fill * K + fields);
}

}  // namespace

std::vector<int> nodes_in_faceset(const std::vector<double>& points,
                                  const std::vector<double>& sample, double tol_mm,
                                  const double* face_normal, double perp_tol_mm) {
    // face_normal (optional, may be nullptr): unit normal of the face plane.
    // When provided, the proximity check is SPLIT into:
    //   (1) perp component: |dot(node-sample, normal)| <= perp_tol_mm — tight to exclude
    //       nodes on adjacent parallel faces (e.g. the opposite face of a thin plate).
    //   (2) in-plane distance: sqrt(3D_dist² - perp²) <= tol_mm — large, covering the
    //       whole face surface regardless of how far sample points are spaced.
    // When face_normal is nullptr (old mode), falls back to pure Euclidean tol for backward
    // compatibility with manually-constructed studies that omit the normal field.
    // This is the Z1/Z2 fix: a 100×100mm fixture face on a 5mm plate uses tol=70mm (full
    // face coverage) + perp_tol=1.5mm (only nodes exactly on the face), so the fixture
    // correctly grabs all 100×100 bottom-face nodes but NOT the 5mm-away top-face nodes.
    //
    // Spatial-hash acceleration: samples are binned into a deterministic 3D grid of cell
    // size = tol_mm. Each node only probes its 27-neighbour sample cells. Membership
    // decisions are identical to the O(n*k) scan (same distance tests, same node order).
    std::vector<int> out;
    const size_t n = points.size() / 3;
    const size_t k = sample.size() / 3;
    if (k == 0 || n == 0) return out;
    const double tol = tol_mm;
    const bool use_normal = (face_normal != nullptr) && (perp_tol_mm > 0.0);
    const double nx = use_normal ? face_normal[0] : 0.0;
    const double ny = use_normal ? face_normal[1] : 0.0;
    const double nz = use_normal ? face_normal[2] : 0.0;
    // P5 fast reject (planar mode): every sample lies ON the face plane, so a node's
    // perpendicular distance is the same against ANY sample up to coplanarity roundoff.
    // One O(1) plane test per node culls off-plane nodes before the sample scan —
    // the surviving on-plane nodes still run the ORIGINAL per-sample loop, so membership
    // decisions are unchanged (the 1e-9 mm slack dwarfs fp noise but admits nothing new).
    const double plane_d0 =
        use_normal ? (sample[0] * nx + sample[1] * ny + sample[2] * nz) : 0.0;

    // Build sample spatial hash. Cell size = tol so any sample within tol of a node is in
    // the node's cell or a neighbour. Deterministic: samples inserted in order 0..k-1;
    // each cell's vector preserves insertion order.
    const double cell = std::max(tol, 1e-9);
    const double inv_cell = 1.0 / cell;
    auto cell_key = [inv_cell](double x, double y, double z) -> int64_t {
        const int64_t ix = static_cast<int64_t>(std::floor(x * inv_cell));
        const int64_t iy = static_cast<int64_t>(std::floor(y * inv_cell));
        const int64_t iz = static_cast<int64_t>(std::floor(z * inv_cell));
        return (ix * 73856093LL) ^ (iy * 19349663LL) ^ (iz * 83492791LL);
    };
    std::unordered_map<int64_t, std::vector<size_t>> grid;
    grid.reserve(k * 2);
    for (size_t s = 0; s < k; ++s) {
        grid[cell_key(sample[s * 3 + 0], sample[s * 3 + 1], sample[s * 3 + 2])].push_back(s);
    }

    for (size_t i = 0; i < n; ++i) {
        const double px = points[i * 3 + 0];
        const double py = points[i * 3 + 1];
        const double pz = points[i * 3 + 2];
        if (use_normal &&
            std::fabs(px * nx + py * ny + pz * nz - plane_d0) > perp_tol_mm + 1e-9)
            continue;
        bool hit = false;
        const int64_t ix0 = static_cast<int64_t>(std::floor(px * inv_cell));
        const int64_t iy0 = static_cast<int64_t>(std::floor(py * inv_cell));
        const int64_t iz0 = static_cast<int64_t>(std::floor(pz * inv_cell));
        for (int dx = -1; dx <= 1 && !hit; ++dx) {
            for (int dy = -1; dy <= 1 && !hit; ++dy) {
                for (int dz = -1; dz <= 1 && !hit; ++dz) {
                    const int64_t key = ((ix0 + dx) * 73856093LL) ^
                                        ((iy0 + dy) * 19349663LL) ^
                                        ((iz0 + dz) * 83492791LL);
                    auto it = grid.find(key);
                    if (it == grid.end()) continue;
                    for (size_t s : it->second) {
                        const double ddx = px - sample[s * 3 + 0];
                        const double ddy = py - sample[s * 3 + 1];
                        const double ddz = pz - sample[s * 3 + 2];
                        if (use_normal) {
                            const double d_perp = std::fabs(ddx * nx + ddy * ny + ddz * nz);
                            if (d_perp > perp_tol_mm) continue;
                            const double d3sq = ddx * ddx + ddy * ddy + ddz * ddz;
                            const double d_plane = std::sqrt(std::max(0.0, d3sq - d_perp * d_perp));
                            if (d_plane <= tol) { hit = true; break; }
                        } else {
                            const double dist = std::sqrt(ddx * ddx + ddy * ddy + ddz * ddz);
                            if (dist <= tol) { hit = true; break; }
                        }
                    }
                }
            }
        }
        if (hit) out.push_back(static_cast<int>(i));
    }
    return out;
}

// Parse faceset["surface"] (see fea_run.h). Returns true iff a usable descriptor exists.
bool faceset_surface(const json& fs, FacesetSurface& out) {
    if (!fs.contains("surface") || !fs["surface"].is_object()) return false;
    const json& sj = fs["surface"];
    const std::string type = sj.value("type", std::string());
    auto vec3 = [&](const char* key, double* dst) {
        if (!sj.contains(key) || !sj[key].is_array() || sj[key].size() < 3) return false;
        for (int i = 0; i < 3; ++i) dst[i] = sj[key][i].get<double>();
        return true;
    };
    auto unit_axis = [&] {
        const double n = std::sqrt(out.axis[0] * out.axis[0] + out.axis[1] * out.axis[1] +
                                   out.axis[2] * out.axis[2]);
        if (n < 1e-12) return false;
        for (double& c : out.axis) c /= n;
        return true;
    };
    if (type == "cylinder") {
        out.kind = FacesetSurface::kCylinder;
        out.r1 = sj.value("radius", 0.0);
        return vec3("origin", out.origin) && vec3("axis", out.axis) && unit_axis() &&
               out.r1 > 0.0;
    }
    if (type == "sphere") {
        out.kind = FacesetSurface::kSphere;
        out.r1 = sj.value("radius", 0.0);
        return vec3("origin", out.origin) && out.r1 > 0.0;
    }
    if (type == "cone") {
        out.kind = FacesetSurface::kCone;
        out.semi_angle = sj.value("semi_angle_rad", 0.0);
        return vec3("origin", out.origin) && vec3("axis", out.axis) && unit_axis() &&
               out.semi_angle > 0.0 && out.semi_angle < 1.5707963;
    }
    if (type == "torus") {
        out.kind = FacesetSurface::kTorus;
        out.r1 = sj.value("major_radius", 0.0);
        out.r2 = sj.value("minor_radius", 0.0);
        return vec3("origin", out.origin) && vec3("axis", out.axis) && unit_axis() &&
               out.r1 > 0.0 && out.r2 > 0.0;
    }
    return false;
}

double faceset_surface_distance(const FacesetSurface& s, const double* p) {
    const double vx = p[0] - s.origin[0], vy = p[1] - s.origin[1], vz = p[2] - s.origin[2];
    switch (s.kind) {
        case FacesetSurface::kSphere:
            return std::fabs(std::sqrt(vx * vx + vy * vy + vz * vz) - s.r1);
        case FacesetSurface::kCylinder: {
            const double h = vx * s.axis[0] + vy * s.axis[1] + vz * s.axis[2];
            const double rx = vx - h * s.axis[0], ry = vy - h * s.axis[1], rz = vz - h * s.axis[2];
            return std::fabs(std::sqrt(rx * rx + ry * ry + rz * rz) - s.r1);
        }
        case FacesetSurface::kCone: {
            const double h = vx * s.axis[0] + vy * s.axis[1] + vz * s.axis[2];
            const double rx = vx - h * s.axis[0], ry = vy - h * s.axis[1], rz = vz - h * s.axis[2];
            const double r = std::sqrt(rx * rx + ry * ry + rz * rz);
            return std::fabs(r * std::cos(s.semi_angle) - h * std::sin(s.semi_angle));
        }
        case FacesetSurface::kTorus: {
            const double h = vx * s.axis[0] + vy * s.axis[1] + vz * s.axis[2];
            const double rx = vx - h * s.axis[0], ry = vy - h * s.axis[1], rz = vz - h * s.axis[2];
            const double r = std::sqrt(rx * rx + ry * ry + rz * rz);
            const double dr = r - s.r1;
            return std::fabs(std::sqrt(dr * dr + h * h) - s.r2);
        }
        case FacesetSurface::kNone: break;
    }
    return std::numeric_limits<double>::infinity();
}

std::vector<int> nodes_in_faceset_surface(const std::vector<double>& points,
                                          const std::vector<double>& sample, double tol_mm,
                                          const FacesetSurface& surface, double perp_tol_mm) {
    // Curved analogue of the planar split-tolerance mode: analytic distance-to-surface
    // is the tight ON-face test; 3D proximity to the sample cloud delimits the trimmed
    // extent. Spatial hash over samples — same membership as the O(n*k) scan.
    std::vector<int> out;
    const size_t n = points.size() / 3;
    const size_t k = sample.size() / 3;
    if (k == 0 || n == 0 || surface.kind == FacesetSurface::kNone) return out;
    const double cell = std::max(tol_mm, 1e-9);
    const double inv_cell = 1.0 / cell;
    auto cell_key = [inv_cell](double x, double y, double z) -> int64_t {
        const int64_t ix = static_cast<int64_t>(std::floor(x * inv_cell));
        const int64_t iy = static_cast<int64_t>(std::floor(y * inv_cell));
        const int64_t iz = static_cast<int64_t>(std::floor(z * inv_cell));
        return (ix * 73856093LL) ^ (iy * 19349663LL) ^ (iz * 83492791LL);
    };
    std::unordered_map<int64_t, std::vector<size_t>> grid;
    grid.reserve(k * 2);
    for (size_t s = 0; s < k; ++s)
        grid[cell_key(sample[s * 3 + 0], sample[s * 3 + 1], sample[s * 3 + 2])].push_back(s);
    const double tol2 = tol_mm * tol_mm;
    for (size_t i = 0; i < n; ++i) {
        const double* p = &points[i * 3];
        if (faceset_surface_distance(surface, p) > perp_tol_mm) continue;
        bool hit = false;
        const int64_t ix0 = static_cast<int64_t>(std::floor(p[0] * inv_cell));
        const int64_t iy0 = static_cast<int64_t>(std::floor(p[1] * inv_cell));
        const int64_t iz0 = static_cast<int64_t>(std::floor(p[2] * inv_cell));
        for (int dx = -1; dx <= 1 && !hit; ++dx) {
            for (int dy = -1; dy <= 1 && !hit; ++dy) {
                for (int dz = -1; dz <= 1 && !hit; ++dz) {
                    const int64_t key = ((ix0 + dx) * 73856093LL) ^
                                        ((iy0 + dy) * 19349663LL) ^
                                        ((iz0 + dz) * 83492791LL);
                    auto it = grid.find(key);
                    if (it == grid.end()) continue;
                    for (size_t s : it->second) {
                        const double ddx = p[0] - sample[s * 3 + 0];
                        const double ddy = p[1] - sample[s * 3 + 1];
                        const double ddz = p[2] - sample[s * 3 + 2];
                        if (ddx * ddx + ddy * ddy + ddz * ddz <= tol2) {
                            hit = true;
                            break;
                        }
                    }
                }
            }
        }
        if (hit) out.push_back(static_cast<int>(i));
    }
    return out;
}


// Resolve a faceset object to mesh node indices with the best available geometry test,
// in priority order: analytic surface (curved faces, C2) > face plane normal (planar,
// Z1/Z2) > legacy pure-Euclidean proximity. Single entry point for every load/fixture/BC
// resolution in this file so the three paths can never drift apart.
//
// Per-mesh faceset cache: multi-load studies re-resolve the same faceset against the
// same points vector many times. Key = FNV of faceset dump + points data pointer +
// points size. Empty-fixture HARD FAILURE is preserved by callers.
namespace {
uint64_t faceset_fnv1a(const void* p, size_t n, uint64_t h = 1469598103934665603ull) {
    const auto* b = static_cast<const unsigned char*>(p);
    for (size_t i = 0; i < n; ++i) { h ^= b[i]; h *= 1099511628211ull; }
    return h;
}
std::vector<double> faceset_sample_local(const nlohmann::json& points_node) {
    std::vector<double> sample;
    if (!points_node.is_array()) return sample;
    for (const auto& p : points_node) {
        if (p.is_array() && p.size() >= 3) {
            sample.push_back(p[0].get<double>());
            sample.push_back(p[1].get<double>());
            sample.push_back(p[2].get<double>());
        } else if (p.is_number()) {
            sample.push_back(p.get<double>());
        }
    }
    return sample;
}
bool faceset_normal_local(const nlohmann::json& fs, std::array<double, 3>& face_normal,
                          double& perp_tol) {
    if (!fs.contains("face_normal") || !fs["face_normal"].is_array() ||
        fs["face_normal"].size() < 3)
        return false;
    const auto& nj = fs["face_normal"];
    face_normal = {nj[0].get<double>(), nj[1].get<double>(), nj[2].get<double>()};
    perp_tol = fs.value("perp_tol_mm", 1.5);
    return true;
}
}  // namespace

std::vector<int> faceset_membership(const nlohmann::json& fs,
                                    const std::vector<double>& points) {
    uint64_t key = faceset_fnv1a(points.data(),
                                 std::min(points.size(), size_t(24)) * sizeof(double));
    const void* ptr = points.data();
    key = faceset_fnv1a(&ptr, sizeof(ptr), key);
    const size_t npts = points.size();
    key = faceset_fnv1a(&npts, sizeof npts, key);
    const std::string fsdump = fs.dump();
    key = faceset_fnv1a(fsdump.data(), fsdump.size(), key);

    static std::mutex cache_mtx;
    static std::unordered_map<uint64_t, std::vector<int>> cache;
    static std::list<uint64_t> lru;
    constexpr size_t kCap = 64;
    {
        std::lock_guard<std::mutex> lk(cache_mtx);
        auto it = cache.find(key);
        if (it != cache.end()) {
            lru.remove(key);
            lru.push_front(key);
            return it->second;
        }
    }

    const std::vector<double> sample = faceset_sample_local(fs.value("points", nlohmann::json::array()));
    const double tol = fs.value("tol_mm", 1.0);
    std::vector<int> ids;
    FacesetSurface surf;
    if (faceset_surface(fs, surf)) {
        const double perp0 = fs.value("perp_tol_mm", 1.5);
        for (double scale = 1.0; scale <= 8.0; scale *= 2.0) {
            ids = nodes_in_faceset_surface(points, sample, tol, surf, perp0 * scale);
            if (!ids.empty()) break;
        }
    } else {
        std::array<double, 3> fn{};
        double perp_tol = 0.0;
        const bool has_fn = faceset_normal_local(fs, fn, perp_tol);
        if (has_fn) {
            for (double scale = 1.0; scale <= 8.0; scale *= 2.0) {
                ids = nodes_in_faceset(points, sample, tol, fn.data(), perp_tol * scale);
                if (!ids.empty()) break;
            }
        } else {
            ids = nodes_in_faceset(points, sample, tol, nullptr, perp_tol);
        }
    }
    {
        std::lock_guard<std::mutex> lk(cache_mtx);
        cache[key] = ids;
        lru.remove(key);
        lru.push_front(key);
        while (lru.size() > kCap) {
            cache.erase(lru.back());
            lru.pop_back();
        }
    }
    return ids;
}

nlohmann::json fea_run_static(const nlohmann::json& study) {
    // Content-addressed solve-result cache (sim_cache): HIT returns the stored result
    // (O(load)); ANY input change is a MISS. Tag the result with cache_hit so callers
    // / tests can observe the path honestly.
    const std::string input_hash =
        chudware::sim::cache::hash_fea_study(chudware::sim::cache::Kind::FeaStatic, study);
    {
        auto hit = chudware::sim::cache::lookup(chudware::sim::cache::Kind::FeaStatic, input_hash);
        if (hit.hit) {
            json out = hit.result;
            out["cache_hit"] = true;
            out["cache_reason"] = "hit";
            out["input_hash"] = input_hash;
            return out;
        }
    }
    // Per-study structured phase record (prof/phases.h): "mesh" / "setup" / "solve" here,
    // plus the solver-internal "solve.<label>" phases from fea_static_solve's FeaProf marks.
    // Surfaced via the prof_phases RPC (engine_profile action="phases").
    chudware::prof::phases::StudyScope _study("fea_static");
    // CHUDWARE_FEA_PROFILE=1 -> coarse mesh-vs-solve wall-clock split on stderr (the per-solve
    // phase breakdown is emitted by fea_static_solve under the same env var). Flushed so it
    // survives an abort() on a too-fine mesh.
    const char* pe = std::getenv("CHUDWARE_FEA_PROFILE");
    const bool fprof = pe && pe[0] && pe[0] != '0';
    const auto t_start = std::chrono::steady_clock::now();

    std::vector<double> points;
    std::vector<int> tets;
    int nodes_per_elem = 4;
    std::string mesh_type_used, mesh_note;
    std::string err;
    {
        chudware::prof::phases::PhaseScope _p("mesh");
        if (!resolve_mesh(study, points, tets, nodes_per_elem, mesh_type_used, mesh_note, err))
            return json{{"ok", false}, {"error", err}};
    }
    if (fprof) {
        const double ms = std::chrono::duration<double, std::milli>(
                              std::chrono::steady_clock::now() - t_start).count();
        std::fprintf(stderr, "[fea] %-22s %10.2f ms  (%zu nodes, %zu elems, order %d)\n",
                     "MESH (resolve_mesh)", ms, points.size() / 3,
                     tets.size() / static_cast<size_t>(nodes_per_elem), nodes_per_elem);
        std::fflush(stderr);
    }

    const int n_nodes = static_cast<int>(points.size() / 3);
    const int n_dof = n_nodes * 3;

    // Governor admission: refuse a solve that would blow past the host's memory
    // budget with a clear, actionable error instead of OOM-crashing the process
    // (the GUI/MCP already render {"ok":false,"error"} gracefully).
    {
        std::string why;
        const size_t n_elems_est = tets.size() / static_cast<size_t>(nodes_per_elem);
        if (!chudware::governor::admit(
                fea_estimate_bytes(n_elems_est, static_cast<size_t>(n_nodes), nodes_per_elem, 3),
                "static FEA solve", why))
            return json{{"ok", false}, {"error", why}};
    }

    const json material = study.value("material", json::object());
    const double E_mpa = material.value("E_mpa", 0.0);
    const double nu = material.value("nu", 0.0);
    const double yield_mpa = material.value("yield_mpa", 250.0);

    std::vector<std::string> notes;
    if (!mesh_note.empty()) notes.push_back("mesh: " + mesh_note);

    const auto t_setup0 = std::chrono::steady_clock::now();  // BC/load resolution phase
    // ── Force vector (replicate run_static's load loop EXACTLY) ──────────────
    std::vector<double> nodal_forces(static_cast<size_t>(n_dof), 0.0);
    if (study.contains("loads") && study["loads"].is_array()) {
        for (const auto& load : study["loads"]) {
            const json faces = load.contains("faces") ? load["faces"] : json::object();
            const double tol = faces.value("tol_mm", 1.0);
            std::vector<int> node_ids = faceset_membership(faces, points);
            if (node_ids.empty()) {
                notes.push_back("Load on empty node set (tol=" + std::to_string(tol) +
                                " mm) — skipped");
                continue;
            }
            const std::string type = load.value("type", std::string());
            // Consistent boundary-load weights w_i = ∫ N_i dA over the loaded faces
            // (corner=0, mid-side=A/3 for tet10; A/n per corner for tet4/hex8 — the latter
            // reproduces the old equal split exactly). Shared by force + pressure.
            std::unordered_set<int> node_set(node_ids.begin(), node_ids.end());
            // For tet10, the geometric faceset pick (corners only on a flat face) MUST be
            // completed with the mid-side nodes that actually carry the work-equivalent load,
            // or w_sum collapses to ~0 and the force degenerates to a wrong, non-convergent
            // corner split (Bug Z1). add_midside_face_nodes inserts every mid-edge node whose
            // two endpoint corners are already selected; no-op for tet4/hex8.
            add_midside_face_nodes(tets, nodes_per_elem, node_set);
            // Re-materialise node_ids from the completed set (sorted, unique) so the force /
            // pressure deposition loops below land on the SAME nodes the weights were put on.
            node_ids.assign(node_set.begin(), node_set.end());
            std::sort(node_ids.begin(), node_ids.end());
            // P5: the consistent-weight arrays are only meaningful when the set can hold a
            // complete boundary face (>= 3 nodes) — oracle-style studies carry hundreds of
            // single-node loads, and zero-filling 4*n_nodes doubles per load was real time.
            std::vector<double> node_w, node_nw;
            double loaded_area = 0.0;
            if (node_set.size() >= 3) {
                node_w.assign(static_cast<size_t>(n_nodes), 0.0);
                node_nw.assign(static_cast<size_t>(n_nodes) * 3, 0.0);
                loaded_area = accumulate_face_weights(points, tets, nodes_per_elem,
                                                      node_set, node_w, node_nw);
            }
            double w_sum = 0.0;
            if (!node_w.empty())
                for (int nid : node_ids) w_sum += node_w[static_cast<size_t>(nid)];

            if (type == "force") {
                std::array<double, 3> fv{0.0, 0.0, 0.0};
                if (load.contains("vector") && load["vector"].is_array()) {
                    const auto& v = load["vector"];
                    for (size_t c = 0; c < 3 && c < v.size(); ++c) fv[c] = v[c].get<double>();
                }
                // tet10 gets the WORK-EQUIVALENT split; tet4/hex8 keep the equal-per-node
                // split byte-identical to the oracle (their linear faces are equal-weighted
                // anyway, but routing them through the identical branch avoids any drift).
                const bool tet10 = (nodes_per_elem == 10);
                if (tet10 && w_sum > 1e-12) {
                    // Each node carries the SHARE of the total force proportional to its
                    // boundary tributary w_i/Σw. Resultant == F exactly; on a tet10 face the
                    // corners get 0 and the mid-sides 1/3 each, which removes the bogus corner
                    // stress spike the old equal split produced (Bug F1).
                    const double invw = 1.0 / w_sum;
                    for (int nid : node_ids) {
                        const double s = node_w[static_cast<size_t>(nid)] * invw;
                        for (int c = 0; c < 3; ++c) nodal_forces[nid * 3 + c] += fv[c] * s;
                    }
                    notes.push_back("Force spread work-consistently over " +
                                    std::to_string(node_ids.size()) + " nodes");
                } else {
                    // tet4/hex8, OR a tet10 node set with no complete boundary face (a sparse
                    // point pick): equal split so the resultant is still F.
                    const double inv = 1.0 / static_cast<double>(node_ids.size());
                    for (int nid : node_ids)
                        for (int c = 0; c < 3; ++c) nodal_forces[nid * 3 + c] += fv[c] * inv;
                    notes.push_back("Force spread equally over " +
                                    std::to_string(node_ids.size()) + " nodes");
                }
            } else if (type == "pressure") {
                const double pressure =
                    load.contains("magnitude") && !load["magnitude"].is_null()
                        ? load["magnitude"].get<double>()
                        : 0.0;
                if (loaded_area > 1e-12 && w_sum > 1e-12) {
                    // Consistent pressure load: f_i = p · ∫ N_i n̂ dA, accumulated per node
                    // with the PER-FACE outward normal (node_nw), so a curved loaded surface
                    // gets the right directional load and the resultant spans p·A·n̂. The load
                    // lands on the mid-side nodes for tet10 (the prior code lumped p·A onto
                    // corner nodes only, which drove the δ/σ several-fold error).
                    for (int nid : node_ids)
                        for (int c = 0; c < 3; ++c)
                            nodal_forces[nid * 3 + c] +=
                                pressure * node_nw[static_cast<size_t>(nid) * 3 + c];
                    notes.push_back("Pressure on loaded area " + std::to_string(loaded_area) +
                                    " mm2 (work-consistent)");
                } else {
                    // Fallback: equal split of (pressure * 1.0) over node_ids (area unknown).
                    const double equiv = pressure * 1.0;
                    const double inv = 1.0 / static_cast<double>(node_ids.size());
                    for (int nid : node_ids)
                        for (int c = 0; c < 3; ++c) nodal_forces[nid * 3 + c] += equiv * inv;
                    notes.push_back(
                        "Pressure load: no boundary faces found — equal split over " +
                        std::to_string(node_ids.size()) + " nodes (area unknown)");
                }
            }
        }
    }

    // ── Fixtures -> fixed nodes (union, sorted unique) ───────────────────────
    std::vector<int> fixed_nodes;
    if (study.contains("fixtures") && study["fixtures"].is_array()) {
        std::unordered_set<int> seen;
        for (const auto& fx : study["fixtures"]) {
            const double tol = fx.value("tol_mm", 1.0);
            std::vector<int> node_ids = faceset_membership(fx, points);
            if (node_ids.empty()) {
                notes.push_back("Fixture on empty node set (tol=" + std::to_string(tol) +
                                " mm) — skipped");
                continue;
            }
            for (int nid : node_ids)
                if (seen.insert(nid).second) fixed_nodes.push_back(nid);
        }
    }
    std::sort(fixed_nodes.begin(), fixed_nodes.end());
    if (fixed_nodes.empty() && study.contains("fixtures") && study["fixtures"].is_array() &&
        !study["fixtures"].empty()) {
        // Fixtures were REQUESTED but none matched a mesh node — solving would be
        // unconstrained garbage (or singular). Say exactly what happened.
        return json{{"ok", false},
                    {"error", "fea_static: the fixture faces resolved to 0 mesh nodes — "
                              "the mesh does not sample those faces (tessellated assembly "
                              "mesh vs analytic face test). This is a solver-side matching "
                              "bug; report the model."},
                    {"notes", notes}};
    }

    // ── Bonded gap ties (asm-fea) ────────────────────────────────────────────
    // An imported assembly meshes into DISCONNECTED components wherever parts have real
    // design clearances (bolted joints without bolt geometry, pin fits): the fuse only
    // welds actual overlap/contact. A loaded component not reached by any fixture makes
    // the reduced system singular. Detect components (union-find over element nodes) and
    // tie each cross-component node pair closer than the gap tolerance with stiff penalty
    // springs (k = E * char_len — element-stiffness scale), the standard bonded-contact
    // idealization. Single-component meshes skip all of this (empty springs, no behaviour
    // change). Gap default 1 mm, override CHUDWARE_FEA_BOND_GAP_MM.
    std::vector<BondSpring> bond_springs;
    {
        const int n_nodes_i = static_cast<int>(points.size() / 3);
        std::vector<int> comp(static_cast<size_t>(n_nodes_i));
        for (int i = 0; i < n_nodes_i; ++i) comp[static_cast<size_t>(i)] = i;
        std::function<int(int)> find = [&](int x) {
            while (comp[static_cast<size_t>(x)] != x) {
                comp[static_cast<size_t>(x)] =
                    comp[static_cast<size_t>(comp[static_cast<size_t>(x)])];
                x = comp[static_cast<size_t>(x)];
            }
            return x;
        };
        auto unite = [&](int a, int b) {
            a = find(a); b = find(b);
            if (a != b) comp[static_cast<size_t>(std::max(a, b))] = std::min(a, b);
        };
        const int npe_i = nodes_per_elem;
        const int n_el = static_cast<int>(tets.size()) / npe_i;
        for (int e = 0; e < n_el; ++e)
            for (int k = 1; k < npe_i; ++k)
                unite(tets[static_cast<size_t>(e) * npe_i],
                      tets[static_cast<size_t>(e) * npe_i + k]);
        std::unordered_set<int> roots;
        for (int i = 0; i < n_nodes_i; ++i) roots.insert(find(i));
        if (roots.size() > 1) {
            // Characteristic element length for the tie stiffness scale: mean first-edge
            // length over a sample of elements (corner 0 -> corner 1).
            double h_sum = 0.0;
            int h_n = 0;
            const int h_step = std::max(1, n_el / 1000);
            for (int e = 0; e < n_el; e += h_step) {
                const int a = tets[static_cast<size_t>(e) * npe_i];
                const int b = tets[static_cast<size_t>(e) * npe_i + 1];
                const double ddx = points[a * 3] - points[b * 3];
                const double ddy = points[a * 3 + 1] - points[b * 3 + 1];
                const double ddz = points[a * 3 + 2] - points[b * 3 + 2];
                h_sum += std::sqrt(ddx * ddx + ddy * ddy + ddz * ddz);
                ++h_n;
            }
            const double char_len = (h_n > 0 && h_sum > 0.0) ? (h_sum / h_n) : 1.0;
            double gap_mm = 1.0;
            if (const char* env = std::getenv("CHUDWARE_FEA_BOND_GAP_MM")) {
                const double g = std::atof(env);
                if (g > 0.0) gap_mm = g;
            }
            // Spatial hash (cell = gap): each node ties to its NEAREST other-component
            // node within the gap; pairs dedupe via (min,max) keys.
            const double cell = gap_mm;
            auto cell_key = [&](double x, double y, double z) {
                const long long ix = static_cast<long long>(std::floor(x / cell));
                const long long iy = static_cast<long long>(std::floor(y / cell));
                const long long iz = static_cast<long long>(std::floor(z / cell));
                return (ix * 73856093LL) ^ (iy * 19349663LL) ^ (iz * 83492791LL);
            };
            std::unordered_map<long long, std::vector<int>> grid;
            grid.reserve(static_cast<size_t>(n_nodes_i));
            for (int i = 0; i < n_nodes_i; ++i)
                grid[cell_key(points[i * 3], points[i * 3 + 1], points[i * 3 + 2])]
                    .push_back(i);
            std::set<std::pair<int, int>> tied;
            for (int i = 0; i < n_nodes_i; ++i) {
                const int ci = find(i);
                const double xi = points[i * 3], yi = points[i * 3 + 1], zi = points[i * 3 + 2];
                // Nearest node of EVERY distinct foreign component within the gap — a
                // single nearest-only tie can spend a cluster's one link on another
                // floating cluster and never reach the anchored one, leaving the super-
                // graph disconnected even though an in-gap pair exists.
                std::unordered_map<int, std::pair<double, int>> best_per_root;
                for (int ox = -1; ox <= 1; ++ox)
                    for (int oy = -1; oy <= 1; ++oy)
                        for (int oz = -1; oz <= 1; ++oz) {
                            const auto it = grid.find(
                                cell_key(xi + ox * cell, yi + oy * cell, zi + oz * cell));
                            if (it == grid.end()) continue;
                            for (int j : it->second) {
                                const int cj = find(j);
                                if (cj == ci) continue;
                                const double dx2 = points[j * 3] - xi;
                                const double dy2 = points[j * 3 + 1] - yi;
                                const double dz2 = points[j * 3 + 2] - zi;
                                const double d2 = dx2 * dx2 + dy2 * dy2 + dz2 * dz2;
                                if (d2 > gap_mm * gap_mm) continue;
                                auto bit = best_per_root.find(cj);
                                if (bit == best_per_root.end() || d2 < bit->second.first)
                                    best_per_root[cj] = {d2, j};
                            }
                        }
                for (const auto& kv : best_per_root) {
                    const int best = kv.second.second;
                    if (tied.emplace(std::min(i, best), std::max(i, best)).second)
                        bond_springs.push_back(BondSpring{i, best, E_mpa * char_len});
                }
            }
            notes.push_back("assembly mesh has " + std::to_string(roots.size()) +
                            " disconnected component(s): " +
                            std::to_string(bond_springs.size()) +
                            " bonded gap tie(s) added (gap <= " + std::to_string(gap_mm) +
                            " mm; CHUDWARE_FEA_BOND_GAP_MM overrides)");

            // ── Reachability: EVERY cluster must have a stiffness path to a fixture ──
            // A free-floating cluster — even an UNLOADED one — contributes unconstrained
            // DOFs and makes the whole reduced system singular. Union the tie edges into
            // super-clusters, then:
            //   * a LOADED unreachable cluster is AUTO-BRIDGED to its nearest neighbouring
            //     cluster (nearest sampled node pair, tied regardless of the gap knob) with
            //     a loud note — an answer with a stated idealization beats refusing, and
            //     the pass loops so chained joints (seat -> bracket -> frame) resolve;
            //   * unloaded unreachable clusters are rigidly GROUNDED (all nodes fixed —
            //     they carry no load, so the rest of the model is exact).
            for (int pass = 0; ; ++pass) {
                std::vector<int> super(comp);  // component forest + tie edges
                std::function<int(int)> sfind = [&](int x) {
                    while (super[static_cast<size_t>(x)] != x) {
                        super[static_cast<size_t>(x)] =
                            super[static_cast<size_t>(super[static_cast<size_t>(x)])];
                        x = super[static_cast<size_t>(x)];
                    }
                    return x;
                };
                for (const BondSpring& s : bond_springs) {
                    const int a = sfind(s.a), b = sfind(s.b);
                    if (a != b) super[static_cast<size_t>(std::max(a, b))] = std::min(a, b);
                }
                std::unordered_set<int> anchored;   // super-roots containing a fixed node
                for (int nid : fixed_nodes) anchored.insert(sfind(nid));
                std::unordered_set<int> loaded_roots;
                for (int i = 0; i < n_nodes_i; ++i)
                    if (nodal_forces[i * 3] != 0.0 || nodal_forces[i * 3 + 1] != 0.0 ||
                        nodal_forces[i * 3 + 2] != 0.0)
                        loaded_roots.insert(sfind(i));

                std::vector<int> grounded_nodes;
                std::vector<int> stranded;  // nodes of ONE unreachable LOADED cluster
                int stranded_root = -1;
                for (int i = 0; i < n_nodes_i; ++i) {
                    const int r0 = sfind(i);
                    if (anchored.count(r0)) continue;
                    if (loaded_roots.count(r0)) {
                        if (stranded_root < 0) stranded_root = r0;
                        if (r0 == stranded_root) stranded.push_back(i);
                    } else {
                        grounded_nodes.push_back(i);
                    }
                }
                if (stranded.empty()) {
                    // All loaded clusters anchored; ground the unloaded stragglers.
                    if (!grounded_nodes.empty()) {
                        std::unordered_set<int> groots;
                        for (int nid : grounded_nodes) groots.insert(sfind(nid));
                        std::unordered_set<int> fixed_set(fixed_nodes.begin(),
                                                          fixed_nodes.end());
                        for (int nid : grounded_nodes)
                            if (fixed_set.insert(nid).second) fixed_nodes.push_back(nid);
                        std::sort(fixed_nodes.begin(), fixed_nodes.end());
                        notes.push_back(std::to_string(groots.size()) +
                                        " unloaded part cluster(s) had no contact path to "
                                        "any fixture and were rigidly grounded (they carry "
                                        "no load; raise CHUDWARE_FEA_BOND_GAP_MM to bond "
                                        "them instead)");
                    }
                    break;
                }
                // Each pass anchors at least one cluster (a tie either reaches an anchored
                // super or merges two floating ones, strictly reducing the count), so the
                // pass budget scales with the initial cluster count.
                const int max_passes = 8 + 2 * static_cast<int>(roots.size());
                if (pass >= max_passes) {
                    return json{{"ok", false},
                                {"error", "fea_static: could not connect every loaded part "
                                          "cluster to a fixture after " +
                                          std::to_string(max_passes) +
                                          " bridge passes — check the assembly for "
                                          "floating parts"}};
                }
                // Nearest cross-cluster node pair for the stranded cluster (sampled).
                double best = std::numeric_limits<double>::infinity();
                int best_a = -1, best_b = -1;
                const std::size_t stepA = std::max<std::size_t>(1, stranded.size() / 400);
                const int stepB = std::max(1, n_nodes_i / 8000);
                for (std::size_t ia = 0; ia < stranded.size(); ia += stepA) {
                    const int a = stranded[ia];
                    for (int b = 0; b < n_nodes_i; b += stepB) {
                        if (sfind(b) == stranded_root) continue;
                        const double dxg = points[a * 3] - points[b * 3];
                        const double dyg = points[a * 3 + 1] - points[b * 3 + 1];
                        const double dzg = points[a * 3 + 2] - points[b * 3 + 2];
                        const double d2 = dxg * dxg + dyg * dyg + dzg * dzg;
                        if (d2 < best) { best = d2; best_a = a; best_b = b; }
                    }
                }
                if (best_a < 0) {
                    return json{{"ok", false},
                                {"error", "fea_static: a loaded part cluster has no "
                                          "neighbouring geometry to bond to"}};
                }
                // Tie the best pair plus every sampled stranded node within (d + char_len)
                // of ITS nearest foreign node — a small bonded patch instead of a single
                // hinge point.
                const double reach = std::sqrt(best) + char_len;
                int n_ties = 0;
                for (std::size_t ia = 0; ia < stranded.size() && n_ties < 24; ia += stepA) {
                    const int a = stranded[ia];
                    double bd = std::numeric_limits<double>::infinity();
                    int bb = -1;
                    for (int b = 0; b < n_nodes_i; b += stepB) {
                        if (sfind(b) == stranded_root) continue;
                        const double dxg = points[a * 3] - points[b * 3];
                        const double dyg = points[a * 3 + 1] - points[b * 3 + 1];
                        const double dzg = points[a * 3 + 2] - points[b * 3 + 2];
                        const double d2 = dxg * dxg + dyg * dyg + dzg * dzg;
                        if (d2 < bd) { bd = d2; bb = b; }
                    }
                    if (bb >= 0 && std::sqrt(bd) <= reach) {
                        bond_springs.push_back(BondSpring{a, bb, E_mpa * char_len});
                        ++n_ties;
                    }
                }
                char buf[240];
                std::snprintf(buf, sizeof(buf),
                              "auto-bridged a loaded part cluster across a ~%.2f mm gap "
                              "(%d tie(s); beyond the %.2f mm bonded-gap tolerance — "
                              "verify this joint is physically bonded)",
                              std::sqrt(best), n_ties, gap_mm);
                notes.push_back(buf);
            }
        }
    }

    const auto t_solve0 = std::chrono::steady_clock::now();
    chudware::prof::phases::add("setup", std::chrono::duration<double, std::milli>(
                                          t_solve0 - t_setup0).count());
    FeaStaticResult r = fea_static_solve(points, tets, E_mpa, nu, yield_mpa, fixed_nodes,
                                         nodal_forces, nodes_per_elem, bond_springs);
    chudware::prof::phases::add("solve", std::chrono::duration<double, std::milli>(
                                          std::chrono::steady_clock::now() - t_solve0).count());
    if (fprof) {
        const auto now = std::chrono::steady_clock::now();
        std::fprintf(stderr, "[fea] %-22s %10.2f ms\n[fea] %-22s %10.2f ms\n",
                     "SOLVE (fea_static_solve)",
                     std::chrono::duration<double, std::milli>(now - t_solve0).count(),
                     "TOTAL (mesh+solve)",
                     std::chrono::duration<double, std::milli>(now - t_start).count());
        std::fflush(stderr);
    }
    if (!r.ok) {
        std::string msg = r.error;
        if (!bond_springs.empty() && msg.find("singular") != std::string::npos)
            msg += " — the assembly mesh has disconnected part clusters and " +
                   std::to_string(bond_springs.size()) +
                   " bonded gap ties were added, but a loaded part may still not reach any "
                   "fixtured part. Raise CHUDWARE_FEA_BOND_GAP_MM (default 1.0) or check the "
                   "assembly's contact/clearances.";
        return json{{"ok", false}, {"error", msg}, {"notes", notes}};
    }
    if (nodes_per_elem == 10) notes.push_back("second-order (tet10) elements");
    // Post-processing (failure criteria + result packaging) — dtor fires at function exit,
    // BEFORE _study's (declared earlier), so the phase lands inside this study.
    chudware::prof::phases::PhaseScope _post("post");

    // ── Additive failure-criterion summary (Tresca / Rankine) ────────────────
    // Computed from the per-node principal stresses the solver already produced;
    // does NOT touch the existing summary keys.
    //   tresca_i        = p1_i - p3_i           (max-shear-stress equivalent)
    //   abs_principal_i = max(|p1_i|, |p3_i|)   (max-normal-stress / Rankine)
    double max_tresca = 0.0, max_abs_principal = 0.0;
    {
        const size_t nn = std::min(r.principal1.size(), r.principal3.size());
        for (size_t i = 0; i < nn; ++i) {
            const double tr = r.principal1[i] - r.principal3[i];
            const double ap =
                std::max(std::abs(r.principal1[i]), std::abs(r.principal3[i]));
            if (tr > max_tresca) max_tresca = tr;
            if (ap > max_abs_principal) max_abs_principal = ap;
        }
    }
    auto clamp_fos = [](double yld, double denom) -> double {
        if (yld <= 0.0) return 15.0;  // no yield strength -> leave at max FoS
        double f = yld / std::max(denom, 1e-9);
        if (f < 0.0) f = 0.0;
        if (f > 15.0) f = 15.0;
        return f;
    };
    const double min_fos_tresca = clamp_fos(yield_mpa, max_tresca);
    const double min_fos_rankine = clamp_fos(yield_mpa, max_abs_principal);

    json out = {{"ok", true},
                {"displacement", r.displacement},
                {"disp_magnitude", r.disp_magnitude},
                {"von_mises", r.von_mises},
                // Additive stress fields (do not affect existing keys / parity).
                {"principal1", r.principal1},
                {"principal3", r.principal3},
                {"max_shear", r.max_shear},
                {"stress_xx", r.stress_xx},
                {"stress_yy", r.stress_yy},
                {"stress_zz", r.stress_zz},
                {"n_dof", r.n_dof},
                {"n_nodes", r.n_nodes},
                {"n_elems", r.n_elems},
                {"mesh_type_used", mesh_type_used},
                {"summary",
                 {{"max_von_mises_mpa", round_nd(r.max_von_mises_mpa, 4)},
                  {"max_disp_mm", round_nd(r.max_disp_mm, 6)},
                  {"min_fos", round_nd(r.min_fos, 4)},
                  {"max_principal_mpa", round_nd(r.max_principal_mpa, 4)},
                  {"min_principal_mpa", round_nd(r.min_principal_mpa, 4)},
                  {"max_shear_mpa", round_nd(r.max_shear_mpa, 4)},
                  // Additive failure-criterion summary (does not alter existing keys).
                  {"min_fos_tresca", round_nd(min_fos_tresca, 4)},
                  {"min_fos_rankine", round_nd(min_fos_rankine, 4)},
                  {"max_tresca_mpa", round_nd(max_tresca, 4)},
                  {"max_abs_principal_mpa", round_nd(max_abs_principal, 4)}}},
                {"solver_notes", notes}};
    chudware::sim::cache::store(chudware::sim::cache::Kind::FeaStatic, input_hash, out);
    out["cache_hit"] = false;
    out["cache_reason"] = "miss";
    out["input_hash"] = input_hash;
    return out;
}

nlohmann::json fea_run_modal(const nlohmann::json& study, int n_modes) {
    // Cache key folds n_modes into the study inputs so a different mode count is a MISS.
    json modal_key = study.is_object() ? study : json::object();
    modal_key["n_modes"] = n_modes;
    const std::string input_hash =
        chudware::sim::cache::hash_fea_study(chudware::sim::cache::Kind::FeaModal, modal_key);
    {
        auto hit = chudware::sim::cache::lookup(chudware::sim::cache::Kind::FeaModal, input_hash);
        if (hit.hit) {
            json out = hit.result;
            out["cache_hit"] = true;
            out["cache_reason"] = "hit";
            out["input_hash"] = input_hash;
            return out;
        }
    }
    chudware::prof::phases::StudyScope _study("fea_modal");  // per-phase record (prof/phases.h)
    std::vector<double> points;
    std::vector<int> tets;
    int nodes_per_elem = 4;
    std::string mesh_type_used, mesh_note;
    std::string err;
    std::vector<std::string> notes;
    // Modal now solves with 2nd-order (tet10) elements by default — the quadratic stiffness
    // + consistent mass capture bending far better than tet4, which cured the spurious
    // square-section first-bending split and the low-frequency bias (Bug N-1). A caller can
    // still force tet4 with mesh_order=1 (faster, less accurate).
    json modal_study = study;
    const int want_order = study.value("mesh_order", 2);
    modal_study["mesh_order"] = want_order;
    if (modal_study.contains("nodes_per_elem"))
        modal_study["nodes_per_elem"] = (want_order == 2) ? 10 : 4;
    {
        chudware::prof::phases::PhaseScope _p("mesh");
        if (!resolve_mesh(modal_study, points, tets, nodes_per_elem, mesh_type_used, mesh_note,
                          err))
            return json{{"ok", false}, {"error", err}};
    }
    notes.push_back(nodes_per_elem == 10 ? "modal uses 2nd-order (tet10) elements"
                                         : "modal uses 1st-order (tet4) elements");
    if (!mesh_note.empty()) notes.push_back("mesh: " + mesh_note);

    // Governor admission (2x the static estimate: K + M plus eigensolver workspace).
    {
        std::string why;
        const size_t ne = tets.size() / static_cast<size_t>(nodes_per_elem);
        if (!chudware::governor::admit(
                2 * fea_estimate_bytes(ne, points.size() / 3, nodes_per_elem, 3),
                "modal FEA solve", why))
            return json{{"ok", false}, {"error", why}};
    }

    const json material = study.value("material", json::object());
    const double E_mpa = material.value("E_mpa", 0.0);
    const double nu = material.value("nu", 0.0);
    const double density_g_cm3 = material.value("density_g_cm3", 0.0);

    // Fixtures -> fixed nodes (union, sorted unique). Modal ignores loads.
    std::vector<int> fixed_nodes;
    if (study.contains("fixtures") && study["fixtures"].is_array()) {
        std::unordered_set<int> seen;
        for (const auto& fx : study["fixtures"]) {
            for (int nid : faceset_membership(fx, points))
                if (seen.insert(nid).second) fixed_nodes.push_back(nid);
        }
    }
    std::sort(fixed_nodes.begin(), fixed_nodes.end());

    FeaModalResult r = [&] {
        chudware::prof::phases::PhaseScope _p("solve");  // assemble + shift-invert eigensolve
        return fea_modal_solve(points, tets, E_mpa, nu, density_g_cm3, fixed_nodes,
                               n_modes, nodes_per_elem);
    }();
    if (!r.ok) return json{{"ok", false}, {"error", r.error}};

    json out = {{"ok", true},
                {"freqs_hz", r.freqs_hz},
                {"mode_shapes", r.mode_shapes},
                {"n_modes", r.n_modes},
                {"n_nodes", r.n_nodes},
                {"n_dof", r.n_dof},
                {"mesh_type_used", mesh_type_used},
                {"solver_notes", notes}};
    chudware::sim::cache::store(chudware::sim::cache::Kind::FeaModal, input_hash, out);
    out["cache_hit"] = false;
    out["cache_reason"] = "miss";
    out["input_hash"] = input_hash;
    return out;
}

namespace {

// Resolve a faceset object ({points,tol_mm[,face_normal,perp_tol_mm]}) to mesh node indices,
// completing tet10 mid-side nodes so surface loads/BCs land on the nodes that carry the
// consistent weights (identical treatment to fea_run_static's load loop). Returns sorted
// unique node ids; empty when the faceset resolves to nothing.
std::vector<int> resolve_face_nodes(const json& faces, const std::vector<double>& points,
                                    const std::vector<int>& tets, int nodes_per_elem) {
    std::vector<int> ids = faceset_membership(faces, points);
    if (ids.empty()) return ids;
    std::unordered_set<int> node_set(ids.begin(), ids.end());
    add_midside_face_nodes(tets, nodes_per_elem, node_set);
    ids.assign(node_set.begin(), node_set.end());
    std::sort(ids.begin(), ids.end());
    return ids;
}

}  // namespace

namespace {

// Parse a per-load time schedule (T-FIN18). Accepts the duty-cycle shorthand
// {duty, period_s} — the direct mapping for an aicad.powerprofile/1 rail — or the general
// piecewise form {points:[[t_s,scale],...], period_s?}. An absent/empty schedule = always
// on. Returns false + err on a malformed schedule.
bool parse_thermal_schedule(const nlohmann::json& js, chudware::sim::ThermalSchedule& out,
                            std::string& err) {
    using nlohmann::json;
    if (js.is_null() || !js.is_object()) return true;  // no schedule = constant
    if (js.contains("duty")) {
        const double d = js.value("duty", 1.0);
        const double p = js.value("period_s", 0.0);
        if (!(d >= 0.0 && d <= 1.0)) { err = "schedule duty must be in [0,1]"; return false; }
        if (d >= 1.0) return true;                       // always on
        if (d <= 0.0) { out.times = {0.0}; out.scales = {0.0}; return true; }
        if (!(p > 0.0)) { err = "duty schedule needs period_s > 0"; return false; }
        out.times = {0.0, d * p};
        out.scales = {1.0, 0.0};
        out.period = p;
        return true;
    }
    if (js.contains("points") && js["points"].is_array()) {
        for (const auto& pt : js["points"]) {
            if (!pt.is_array() || pt.size() != 2) { err = "schedule points must be [t_s, scale] pairs"; return false; }
            out.times.push_back(pt[0].get<double>());
            out.scales.push_back(pt[1].get<double>());
        }
        out.period = js.value("period_s", 0.0);
        return true;
    }
    err = "schedule needs {duty, period_s} or {points:[[t,scale],...]}";
    return false;
}

}  // namespace

nlohmann::json fea_run_thermal(const nlohmann::json& study) {
    const std::string input_hash =
        chudware::sim::cache::hash_fea_study(chudware::sim::cache::Kind::FeaThermal, study);
    {
        auto hit = chudware::sim::cache::lookup(chudware::sim::cache::Kind::FeaThermal, input_hash);
        if (hit.hit) {
            json out = hit.result;
            out["cache_hit"] = true;
            out["cache_reason"] = "hit";
            out["input_hash"] = input_hash;
            return out;
        }
    }
    chudware::prof::phases::StudyScope _study("fea_thermal");  // per-phase record (prof/phases.h)
    std::vector<double> points;
    std::vector<int> tets;
    int nodes_per_elem = 4;
    std::string mesh_type_used, mesh_note;
    std::string err;
    {
        chudware::prof::phases::PhaseScope _p("mesh");
        if (!resolve_mesh(study, points, tets, nodes_per_elem, mesh_type_used, mesh_note, err))
            return json{{"ok", false}, {"error", err}};
    }

    std::vector<std::string> notes;
    if (!mesh_note.empty()) notes.push_back("mesh: " + mesh_note);
    if (nodes_per_elem == 10) notes.push_back("second-order (tet10) elements");

    // Governor admission (scalar field: 1 DOF/node).
    {
        std::string why;
        const size_t ne = tets.size() / static_cast<size_t>(nodes_per_elem);
        if (!chudware::governor::admit(
                fea_estimate_bytes(ne, points.size() / 3, nodes_per_elem, 1),
                "thermal FEA solve", why))
            return json{{"ok", false}, {"error", why}};
    }

    // Conductivity: W/(m·K) in the study, converted to the mesh unit (mm): W/(mm·K) = W/(m·K)/1000.
    const json material = study.value("material", json::object());
    const double k_w_mk = material.value("k_w_mk", study.value("k_w_mk", 0.0));
    if (!(k_w_mk > 0.0))
        return json{{"ok", false},
                    {"error", "thermal study needs a positive conductivity (material.k_w_mk, W/m·K)"}};
    const double k_cond = k_w_mk / 1000.0;  // W/(mm·K)

    // ── Dirichlet fixed-temperature faces (fixtures with a temp_c) ──────────────────────
    std::vector<int> fixed_nodes;
    std::vector<double> fixed_temps;
    std::unordered_set<int> fixed_seen;
    if (study.contains("fixtures") && study["fixtures"].is_array()) {
        for (const auto& fx : study["fixtures"]) {
            // A thermal fixture MUST carry a temperature; a plain (mechanical) fixture with
            // no temp_c is not a Dirichlet BC here — skip it with a note rather than pinning T=0.
            if (!fx.contains("temp_c") || fx["temp_c"].is_null()) {
                notes.push_back("Fixture without temp_c ignored (thermal needs a fixed temperature)");
                continue;
            }
            const double t_c = fx["temp_c"].get<double>();
            std::vector<int> ids = resolve_face_nodes(fx, points, tets, nodes_per_elem);
            if (ids.empty()) {
                notes.push_back("Fixed-temperature face resolved to no nodes — skipped");
                continue;
            }
            for (int nid : ids)
                if (fixed_seen.insert(nid).second) { fixed_nodes.push_back(nid); fixed_temps.push_back(t_c); }
        }
    }

    // ── Loads: heat flux + convection film ──────────────────────────────────────────────
    std::vector<chudware::sim::ThermalConvection> convections;
    std::vector<chudware::sim::ThermalFluxLoad> flux_loads;  // per-load: steady AND transient
    struct FluxFaceLog { size_t n_nodes; double q_wm2; bool scheduled; };
    std::vector<FluxFaceLog> flux_log;
    const bool transient = study.contains("transient") && study["transient"].is_object();
    if (study.contains("loads") && study["loads"].is_array()) {
        for (const auto& load : study["loads"]) {
            const std::string type = load.value("type", std::string());
            const json faces = load.contains("faces") ? load["faces"] : json::object();
            std::vector<int> ids = resolve_face_nodes(faces, points, tets, nodes_per_elem);
            if (ids.empty()) {
                notes.push_back("Thermal load on empty node set — skipped");
                continue;
            }
            if (type == "flux") {
                const double q_wm2 = load.contains("magnitude") && !load["magnitude"].is_null()
                                         ? load["magnitude"].get<double>()
                                         : 0.0;  // W/m^2 (positive = heat into the body)
                flux_log.push_back({ids.size(), q_wm2, load.contains("schedule")});
                // Every load stays SEPARATE with its own node set + magnitude (+ schedule for
                // transient). The old steady path collapsed all loads onto one node set with
                // the LAST load's magnitude — silently wrong for >1 flux load (C3 defect).
                chudware::sim::ThermalFluxLoad fl;
                fl.nodes = ids;
                fl.q = q_wm2 / 1.0e6;  // W/m^2 -> W/mm^2
                std::string serr;
                if (!parse_thermal_schedule(load.value("schedule", json()), fl.schedule, serr))
                    return json{{"ok", false}, {"error", "thermal load schedule: " + serr}};
                flux_loads.push_back(std::move(fl));
                if (!transient && load.contains("schedule"))
                    notes.push_back("Flux schedule ignored in a steady-state study");
            } else if (type == "convection") {
                chudware::sim::ThermalConvection cv;
                cv.nodes = ids;
                // h in W/(m^2·K) -> W/(mm^2·K) = h/1e6; t_amb in °C (same scale as fixed temps).
                cv.h = (load.value("h", 0.0)) / 1.0e6;
                cv.t_amb = load.value("t_amb_c", 20.0);
                if (cv.h > 0.0) convections.push_back(std::move(cv));
                else notes.push_back("Convection load with non-positive h ignored");
            } else {
                notes.push_back("Unknown thermal load type '" + type + "' ignored");
            }
        }
    }
    // ── Transient branch (T-FIN18) — the steady path below stays bit-for-bit intact. ─────
    if (transient) {
        const json& tr = study["transient"];
        // Volumetric heat capacity rho*cp in J/(mm^3·K):
        //   rho [kg/m^3] * cp [J/(kg·K)] / 1e9  (1 m^3 = 1e9 mm^3);  g/cm^3 = 1000 kg/m^3.
        const double cp = material.value("cp_j_kgk", study.value("cp_j_kgk", 0.0));
        double rho_kg_m3 = material.value("rho_kg_m3", 0.0);
        if (rho_kg_m3 <= 0.0) rho_kg_m3 = material.value("density_g_cm3", 0.0) * 1000.0;
        if (!(cp > 0.0) || !(rho_kg_m3 > 0.0))
            return json{{"ok", false},
                        {"error", "transient thermal needs material.cp_j_kgk (J/kg·K) and "
                                  "density (density_g_cm3 or rho_kg_m3)"}};
        chudware::sim::ThermalTransientOptions opts;
        opts.rho_cp = rho_kg_m3 * cp / 1.0e9;
        opts.t_end = tr.value("t_end_s", 0.0);
        opts.dt = tr.value("dt_s", 0.0);
        opts.t_init = tr.value("t_init_c", 20.0);
        opts.lumped = tr.value("capacitance", std::string("lumped")) != "consistent";
        if (tr.contains("frame_times_s") && tr["frame_times_s"].is_array())
            for (const auto& t : tr["frame_times_s"]) opts.frame_times.push_back(t.get<double>());
        if (opts.dt <= 0.0 && opts.t_end > 0.0) {
            opts.dt = opts.t_end / 200.0;  // sensible default resolution
            notes.push_back("transient dt_s not given — defaulted to t_end_s/200");
        }

        chudware::sim::ThermalTransientResult r = [&] {
            chudware::prof::phases::PhaseScope _p("solve.transient");
            return chudware::sim::fea_thermal_transient(
                points, tets, k_cond, fixed_nodes, fixed_temps, flux_loads, convections,
                opts, nodes_per_elem);
        }();
        if (!r.ok) return json{{"ok", false}, {"error", r.error}, {"solver_notes", notes}};

        json frames = json::array();
        for (const auto& fr : r.frames)
            frames.push_back({{"t_s", round_nd(fr.time, 6)}, {"temperature", fr.temperature}});
        json flux_summary = json::array();
        for (const auto& fl : flux_log)
            flux_summary.push_back({{"type", "flux"}, {"q_w_m2", fl.q_wm2},
                                    {"n_nodes", fl.n_nodes}, {"scheduled", fl.scheduled}});
        for (const auto& cv : convections)
            flux_summary.push_back({{"type", "convection"}, {"h_w_m2k", cv.h * 1.0e6},
                                    {"t_amb_c", cv.t_amb}, {"n_nodes", cv.nodes.size()}});
        json out = {{"ok", true},
                    {"transient", true},
                    {"frames", frames},
                    {"temperature", r.frames.back().temperature},  // final field (steady-compat)
                    {"envelope_max", r.envelope_max},
                    {"n_nodes", r.n_nodes},
                    {"n_dof", r.n_dof},
                    {"n_elems", r.n_elems},
                    {"n_steps", r.n_steps},
                    {"mesh_type_used", mesh_type_used},
                    {"summary",
                     {{"max_temp_c", round_nd(r.max_temp, 4)},
                      {"max_temp_time_s", round_nd(r.max_temp_time, 6)},
                      {"min_temp_c", round_nd(r.min_temp, 4)},
                      {"final_max_temp_c",
                       round_nd(*std::max_element(r.frames.back().temperature.begin(),
                                                  r.frames.back().temperature.end()), 4)}}},
                    {"flux_summary", flux_summary},
                    {"solver_notes", notes}};
        chudware::sim::cache::store(chudware::sim::cache::Kind::FeaThermal, input_hash, out);
        out["cache_hit"] = false;
        out["cache_reason"] = "miss";
        out["input_hash"] = input_hash;
        return out;
    }

    chudware::sim::ThermalResult r = [&] {
        chudware::prof::phases::PhaseScope _p("solve");
        return chudware::sim::fea_thermal_solve(
            points, tets, k_cond, fixed_nodes, fixed_temps, flux_loads, convections,
            nodes_per_elem);
    }();
    if (!r.ok) return json{{"ok", false}, {"error", r.error}, {"solver_notes", notes}};

    // ── Per-face flux summary (advisory): report the applied flux/convection per BC. ─────
    json flux_summary = json::array();
    for (const auto& fl : flux_log)
        flux_summary.push_back({{"type", "flux"}, {"q_w_m2", fl.q_wm2}, {"n_nodes", fl.n_nodes}});
    for (const auto& cv : convections)
        flux_summary.push_back({{"type", "convection"},
                                {"h_w_m2k", cv.h * 1.0e6},
                                {"t_amb_c", cv.t_amb},
                                {"n_nodes", cv.nodes.size()}});

    json out = {{"ok", true},
                {"temperature", r.temperature},
                {"n_nodes", r.n_nodes},
                {"n_dof", r.n_dof},
                {"n_elems", r.n_elems},
                {"mesh_type_used", mesh_type_used},
                {"summary",
                 {{"max_temp_c", round_nd(r.max_temp, 4)}, {"min_temp_c", round_nd(r.min_temp, 4)}}},
                {"flux_summary", flux_summary},
                {"solver_notes", notes}};
    chudware::sim::cache::store(chudware::sim::cache::Kind::FeaThermal, input_hash, out);
    out["cache_hit"] = false;
    out["cache_reason"] = "miss";
    out["input_hash"] = input_hash;
    return out;
}

// ── Mesh cache (public) ──────────────────────────────────────────────────────────
// Memoize mesh_step keyed on STEP file CONTENTS + every MeshOptions field. Re-running a
// study that changed only loads/material/fixtures reuses the mesh (the mesher is the #1 FEA
// cost). Content-keyed (not path) because the caller may write a fresh temp STEP each solve.
// P4: the cache stores shared_ptr<const MeshOut>, so a HIT hands back the resident mesh
// without deep-copying megabytes of points/tets (the old map<key, MeshOut> copied the whole
// mesh on every hit). LRU eviction just drops the cache's reference — an outstanding caller
// keeps its mesh alive via its own shared_ptr.
// Residency is also bounded by a byte budget derived from the resource governor (not only
// entry count): fine assembly meshes are MBs each and a fixed kCap of 8 can still OOM a
// low-end box.
namespace {
size_t mesh_out_bytes(const MeshOut& m) {
    return m.points.size() * sizeof(double) + m.tets.size() * sizeof(int) +
           m.aspect_hist.size() * sizeof(int) + m.mesh_type_used.size() + m.note.size() +
           m.error.size() + 256;
}
uint64_t mesh_cache_budget_bytes() {
    const uint64_t host = chudware::governor::budget_bytes();
    if (host == UINT64_MAX) return 512ull * 1024ull * 1024ull;  // 512 MiB when governor off
    // Cap mesh-cache residency at 12.5% of the study budget, floored at 64 MiB so a
    // small machine still keeps a couple of warm meshes.
    const uint64_t frac = host / 8;
    return std::max<uint64_t>(64ull * 1024ull * 1024ull, frac);
}
}  // namespace

// Process-wide mesh-cache counters (whole-STEP + per-solid). Shared with mesher.cpp
// via mesh_cache_note_generation / mesh_cache_stats. File-scope (not anonymous) so the
// public mesh_cache_* API can touch them.
namespace mesh_cache_detail {

inline uint64_t fnv1a(const void* p, size_t n, uint64_t h = 1469598103934665603ull) {
    const auto* b = static_cast<const unsigned char*>(p);
    for (size_t i = 0; i < n; ++i) { h ^= b[i]; h *= 1099511628211ull; }
    return h;
}
inline uint64_t mesh_opts_key(const MeshOptions& o, uint64_t h) {
    static_assert(sizeof(MeshOptions) == 80,
                  "MeshOptions size changed — a field was added/removed. Update mesh_opts_key "
                  "to hash the new field(s), then update this expected size. The mesh cache "
                  "MUST fold in every field or it can return a stale mesh for changed settings.");
    h = fnv1a(&o.fineness, sizeof o.fineness, h);
    h = fnv1a(&o.density, sizeof o.density, h);
    h = fnv1a(&o.order, sizeof o.order, h);
    h = fnv1a(&o.curvature, sizeof o.curvature, h);
    h = fnv1a(&o.curv_elems_per_circle, sizeof o.curv_elems_per_circle, h);
    h = fnv1a(&o.target_size_mm, sizeof o.target_size_mm, h);
    h = fnv1a(&o.min_size_mm, sizeof o.min_size_mm, h);
    h = fnv1a(&o.max_size_mm, sizeof o.max_size_mm, h);
    h = fnv1a(&o.optimize, sizeof o.optimize, h);
    h = fnv1a(&o.algorithm3d, sizeof o.algorithm3d, h);
    h = fnv1a(&o.structured, sizeof o.structured, h);
    h = fnv1a(&o.auto_type, sizeof o.auto_type, h);
    h = fnv1a(&o.quad_surface, sizeof o.quad_surface, h);
    h = fnv1a(&o.fallback_to_tet, sizeof o.fallback_to_tet, h);
    h = fnv1a(&o.thin_wall_layers, sizeof o.thin_wall_layers, h);
    return h;
}
struct MeshCacheCounters {
    std::mutex mtx;
    MeshCacheStats s;
};
inline MeshCacheCounters& mesh_cache_counters() {
    static MeshCacheCounters c;
    return c;
}
}  // namespace mesh_cache_detail
using mesh_cache_detail::mesh_cache_counters;
using mesh_cache_detail::fnv1a;
using mesh_cache_detail::mesh_opts_key;

MeshCacheStats mesh_cache_stats() {
    auto& c = mesh_cache_counters();
    std::lock_guard<std::mutex> lk(c.mtx);
    return c.s;
}

void mesh_cache_reset_stats() {
    auto& c = mesh_cache_counters();
    std::lock_guard<std::mutex> lk(c.mtx);
    c.s = MeshCacheStats{};
}

void mesh_cache_note_generation() {
    auto& c = mesh_cache_counters();
    std::lock_guard<std::mutex> lk(c.mtx);
    ++c.s.generations;
}

// Internal hooks used by the per-solid component cache in mesher.cpp (same counters).
void mesh_cache_note_component_hit() {
    auto& c = mesh_cache_counters();
    std::lock_guard<std::mutex> lk(c.mtx);
    ++c.s.hits;
    ++c.s.component_hits;
}
void mesh_cache_note_component_miss() {
    auto& c = mesh_cache_counters();
    std::lock_guard<std::mutex> lk(c.mtx);
    ++c.s.misses;
    ++c.s.component_misses;
}
void mesh_cache_set_resident(uint64_t bytes, uint64_t budget) {
    auto& c = mesh_cache_counters();
    std::lock_guard<std::mutex> lk(c.mtx);
    c.s.resident_bytes = bytes;
    c.s.budget_bytes = budget;
}

std::shared_ptr<const MeshOut> mesh_step_cached_ptr(const std::string& step_path,
                                                    const MeshOptions& options) {
    // Hash the STEP bytes (one block read — not the old istreambuf char-at-a-time slurp).
    // On any read failure, skip the cache and mesh directly.
    std::string bytes;
    {
        std::ifstream f(step_path, std::ios::binary | std::ios::ate);
        if (f) {
            const std::streamsize n = f.tellg();
            if (n > 0) {
                bytes.resize(static_cast<size_t>(n));
                f.seekg(0);
                if (!f.read(&bytes[0], n)) bytes.clear();
            }
        }
    }
    if (bytes.empty())
        return std::make_shared<const MeshOut>(mesh_step(step_path, options));
    uint64_t key = fnv1a(bytes.data(), bytes.size());
    key = mesh_opts_key(options, key);

    static std::mutex mtx;
    static std::unordered_map<uint64_t, std::shared_ptr<const MeshOut>> cache;
    static std::list<uint64_t> lru;   // most-recent at front
    static uint64_t resident = 0;
    constexpr size_t kCap = 16;       // soft entry cap; byte budget is the hard bound
    {
        std::lock_guard<std::mutex> lk(mtx);
        auto it = cache.find(key);
        if (it != cache.end()) {      // HIT: identical geometry + options — no copy
            lru.remove(key);
            lru.push_front(key);
            {
                auto& c = mesh_cache_counters();
                std::lock_guard<std::mutex> ck(c.mtx);
                ++c.s.hits;
                c.s.resident_bytes = resident;
                c.s.budget_bytes = mesh_cache_budget_bytes();
            }
            return it->second;
        }
    }
    // MISS: mesh OUTSIDE the lock (mesh_step self-serializes), then store.
    {
        auto& c = mesh_cache_counters();
        std::lock_guard<std::mutex> ck(c.mtx);
        ++c.s.misses;
    }
    auto m = std::make_shared<const MeshOut>(mesh_step(step_path, options));
    if (m->ok) {
        const uint64_t need = static_cast<uint64_t>(mesh_out_bytes(*m));
        const uint64_t budget = mesh_cache_budget_bytes();
        std::lock_guard<std::mutex> lk(mtx);
        // Evict LRU until the new mesh fits (or the cache is empty).
        while ((!lru.empty()) &&
               (lru.size() >= kCap || resident + need > budget)) {
            auto bit = cache.find(lru.back());
            if (bit != cache.end()) {
                resident -= static_cast<uint64_t>(mesh_out_bytes(*bit->second));
                cache.erase(bit);
            }
            lru.pop_back();
        }
        // Insert even if need > budget (keep the single most-recent mesh).
        if (cache.find(key) == cache.end()) {
            cache[key] = m;
            lru.push_front(key);
            resident += need;
        } else {
            // Concurrent store of the same key: prefer the existing entry.
            lru.remove(key);
            lru.push_front(key);
        }
        mesh_cache_set_resident(resident, budget);
    }
    return m;
}

MeshOut mesh_step_cached(const std::string& step_path, const MeshOptions& options) {
    return *mesh_step_cached_ptr(step_path, options);  // legacy by-value form (one copy)
}

}  // namespace chudware::sim
