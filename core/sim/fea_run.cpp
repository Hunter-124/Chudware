// fea_run.cpp — see fea_run.h. The combined mesh+resolve+solve glue.
#include "sim/fea_run.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <string>
#include <unordered_set>
#include <vector>

#include "sim/fea_modal.h"
#include "sim/fea_static.h"
#include "sim/mesher.h"

using nlohmann::json;

namespace aicad::sim {

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
        MeshOut m = mesh_step(step_path, opts);
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
            const double nx = nvx / area, ny = nvy / area, nz = nvz / area;
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
    std::vector<int> out;
    const size_t n = points.size() / 3;
    const size_t k = sample.size() / 3;
    if (k == 0 || n == 0) return out;
    const double tol = tol_mm;
    const bool use_normal = (face_normal != nullptr) && (perp_tol_mm > 0.0);
    const double nx = use_normal ? face_normal[0] : 0.0;
    const double ny = use_normal ? face_normal[1] : 0.0;
    const double nz = use_normal ? face_normal[2] : 0.0;
    for (size_t i = 0; i < n; ++i) {
        const double px = points[i * 3 + 0];
        const double py = points[i * 3 + 1];
        const double pz = points[i * 3 + 2];
        bool hit = false;
        for (size_t s = 0; s < k; ++s) {
            const double dx = px - sample[s * 3 + 0];
            const double dy = py - sample[s * 3 + 1];
            const double dz = pz - sample[s * 3 + 2];
            if (use_normal) {
                // Signed perpendicular component: how far the node is from the face plane
                // (at this sample point). Tight threshold excludes off-face nodes.
                const double d_perp = std::fabs(dx * nx + dy * ny + dz * nz);
                if (d_perp > perp_tol_mm) continue;  // not on this face plane
                // In-plane distance: sqrt(3D_dist² - d_perp²). Covers the entire face.
                const double d3sq = dx*dx + dy*dy + dz*dz;
                const double d_plane = std::sqrt(std::max(0.0, d3sq - d_perp * d_perp));
                if (d_plane <= tol) { hit = true; break; }
            } else {
                // Legacy mode: pure 3D Euclidean distance (no face normal available).
                const double dist = std::sqrt(dx * dx + dy * dy + dz * dz);
                if (dist <= tol) { hit = true; break; }
            }
        }
        if (hit) out.push_back(static_cast<int>(i));
    }
    return out;
}

nlohmann::json fea_run_static(const nlohmann::json& study) {
    std::vector<double> points;
    std::vector<int> tets;
    int nodes_per_elem = 4;
    std::string mesh_type_used, mesh_note;
    std::string err;
    if (!resolve_mesh(study, points, tets, nodes_per_elem, mesh_type_used, mesh_note, err))
        return json{{"ok", false}, {"error", err}};

    const int n_nodes = static_cast<int>(points.size() / 3);
    const int n_dof = n_nodes * 3;

    const json material = study.value("material", json::object());
    const double E_mpa = material.value("E_mpa", 0.0);
    const double nu = material.value("nu", 0.0);
    const double yield_mpa = material.value("yield_mpa", 250.0);

    std::vector<std::string> notes;
    if (!mesh_note.empty()) notes.push_back("mesh: " + mesh_note);

    // ── Force vector (replicate run_static's load loop EXACTLY) ──────────────
    std::vector<double> nodal_forces(static_cast<size_t>(n_dof), 0.0);
    if (study.contains("loads") && study["loads"].is_array()) {
        for (const auto& load : study["loads"]) {
            const json faces = load.contains("faces") ? load["faces"] : json::object();
            std::vector<double> sample = faceset_sample(faces.value("points", json::array()));
            const double tol = faces.value("tol_mm", 1.0);
            std::array<double, 3> fn{}; double perp_tol = 0.0;
            const bool has_fn = faceset_normal(faces, fn, perp_tol);
            std::vector<int> node_ids = nodes_in_faceset(points, sample, tol,
                has_fn ? fn.data() : nullptr, perp_tol);
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
            std::vector<double> node_w(static_cast<size_t>(n_nodes), 0.0);
            std::vector<double> node_nw(static_cast<size_t>(n_nodes) * 3, 0.0);
            const double loaded_area = accumulate_face_weights(points, tets, nodes_per_elem,
                                                               node_set, node_w, node_nw);
            double w_sum = 0.0;
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
            std::vector<double> sample = faceset_sample(fx.value("points", json::array()));
            const double tol = fx.value("tol_mm", 1.0);
            std::array<double, 3> fn{}; double perp_tol = 0.0;
            const bool has_fn = faceset_normal(fx, fn, perp_tol);
            std::vector<int> node_ids = nodes_in_faceset(points, sample, tol,
                has_fn ? fn.data() : nullptr, perp_tol);
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

    FeaStaticResult r = fea_static_solve(points, tets, E_mpa, nu, yield_mpa, fixed_nodes,
                                         nodal_forces, nodes_per_elem);
    if (!r.ok) return json{{"ok", false}, {"error", r.error}};
    if (nodes_per_elem == 10) notes.push_back("second-order (tet10) elements");

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

    return json{{"ok", true},
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
}

nlohmann::json fea_run_modal(const nlohmann::json& study, int n_modes) {
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
    if (!resolve_mesh(modal_study, points, tets, nodes_per_elem, mesh_type_used, mesh_note, err))
        return json{{"ok", false}, {"error", err}};
    notes.push_back(nodes_per_elem == 10 ? "modal uses 2nd-order (tet10) elements"
                                         : "modal uses 1st-order (tet4) elements");
    if (!mesh_note.empty()) notes.push_back("mesh: " + mesh_note);

    const json material = study.value("material", json::object());
    const double E_mpa = material.value("E_mpa", 0.0);
    const double nu = material.value("nu", 0.0);
    const double density_g_cm3 = material.value("density_g_cm3", 0.0);

    // Fixtures -> fixed nodes (union, sorted unique). Modal ignores loads.
    std::vector<int> fixed_nodes;
    if (study.contains("fixtures") && study["fixtures"].is_array()) {
        std::unordered_set<int> seen;
        for (const auto& fx : study["fixtures"]) {
            std::vector<double> sample = faceset_sample(fx.value("points", json::array()));
            const double tol = fx.value("tol_mm", 1.0);
            std::array<double, 3> fn{}; double perp_tol = 0.0;
            const bool has_fn = faceset_normal(fx, fn, perp_tol);
            std::vector<int> node_ids = nodes_in_faceset(points, sample, tol,
                has_fn ? fn.data() : nullptr, perp_tol);
            for (int nid : node_ids)
                if (seen.insert(nid).second) fixed_nodes.push_back(nid);
        }
    }
    std::sort(fixed_nodes.begin(), fixed_nodes.end());

    FeaModalResult r = fea_modal_solve(points, tets, E_mpa, nu, density_g_cm3, fixed_nodes,
                                       n_modes, nodes_per_elem);
    if (!r.ok) return json{{"ok", false}, {"error", r.error}};

    return json{{"ok", true},
                {"freqs_hz", r.freqs_hz},
                {"mode_shapes", r.mode_shapes},
                {"n_modes", r.n_modes},
                {"n_nodes", r.n_nodes},
                {"n_dof", r.n_dof},
                {"mesh_type_used", mesh_type_used},
                {"solver_notes", notes}};
}

}  // namespace aicad::sim
