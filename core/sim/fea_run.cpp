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

// Resolve the mesh: inline points/tets if present, else mesh the STEP file.
// Returns true on success (fills points/tets), false with `err` set otherwise.
// nodes_per_elem is set to 4 (tet4) or 10 (tet10): for an inline mesh it is taken
// from study["nodes_per_elem"] (default 4); for a STEP mesh it follows the chosen
// mesh_order (study["mesh_order"], default 1).
bool resolve_mesh(const json& study, std::vector<double>& points, std::vector<int>& tets,
                  int& nodes_per_elem, std::string& err) {
    nodes_per_elem = 4;
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
        MeshOut m = mesh_step(step_path, opts);
        if (!m.ok) {
            err = "mesh failed: " + m.error;
            return false;
        }
        points = m.points;
        tets = m.tets;
        nodes_per_elem = m.nodes_per_elem;
        return true;
    }
    err = "study must have inline points/tets or a step_path";
    return false;
}

}  // namespace

std::vector<int> nodes_in_faceset(const std::vector<double>& points,
                                  const std::vector<double>& sample, double tol_mm) {
    std::vector<int> out;
    const size_t n = points.size() / 3;
    const size_t k = sample.size() / 3;
    if (k == 0 || n == 0) return out;
    const double tol = tol_mm;
    // Match _nodes_in_faceset: a node is selected if its MIN distance to any
    // sample point is <= tol. (No squared-tol shortcut quirks — the oracle
    // compares the true Euclidean distance to tol.) np.where(mask)[0] yields
    // ascending node indices, so iterating i in order produces a sorted result.
    for (size_t i = 0; i < n; ++i) {
        const double px = points[i * 3 + 0];
        const double py = points[i * 3 + 1];
        const double pz = points[i * 3 + 2];
        bool hit = false;
        for (size_t s = 0; s < k; ++s) {
            const double dx = px - sample[s * 3 + 0];
            const double dy = py - sample[s * 3 + 1];
            const double dz = pz - sample[s * 3 + 2];
            const double dist = std::sqrt(dx * dx + dy * dy + dz * dz);
            if (dist <= tol) {
                hit = true;
                break;
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
    std::string err;
    if (!resolve_mesh(study, points, tets, nodes_per_elem, err))
        return json{{"ok", false}, {"error", err}};

    const int n_nodes = static_cast<int>(points.size() / 3);
    const int n_dof = n_nodes * 3;

    const json material = study.value("material", json::object());
    const double E_mpa = material.value("E_mpa", 0.0);
    const double nu = material.value("nu", 0.0);
    const double yield_mpa = material.value("yield_mpa", 250.0);

    std::vector<std::string> notes;

    // ── Force vector (replicate run_static's load loop EXACTLY) ──────────────
    std::vector<double> nodal_forces(static_cast<size_t>(n_dof), 0.0);
    if (study.contains("loads") && study["loads"].is_array()) {
        for (const auto& load : study["loads"]) {
            const json faces = load.contains("faces") ? load["faces"] : json::object();
            std::vector<double> sample = faceset_sample(faces.value("points", json::array()));
            const double tol = faces.value("tol_mm", 1.0);
            std::vector<int> node_ids = nodes_in_faceset(points, sample, tol);
            if (node_ids.empty()) {
                notes.push_back("Load on empty node set (tol=" + std::to_string(tol) +
                                " mm) — skipped");
                continue;
            }
            const std::string type = load.value("type", std::string());
            if (type == "force") {
                // force_vec / len(node_ids) added to each node's 3 dofs.
                std::array<double, 3> fv{0.0, 0.0, 0.0};
                if (load.contains("vector") && load["vector"].is_array()) {
                    const auto& v = load["vector"];
                    for (size_t c = 0; c < 3 && c < v.size(); ++c) fv[c] = v[c].get<double>();
                }
                const double inv = 1.0 / static_cast<double>(node_ids.size());
                for (int nid : node_ids)
                    for (int c = 0; c < 3; ++c) nodal_forces[nid * 3 + c] += fv[c] * inv;
                notes.push_back("Force spread equally over " +
                                std::to_string(node_ids.size()) + " nodes");
            } else if (type == "pressure") {
                const double pressure =
                    load.contains("magnitude") && !load["magnitude"].is_null()
                        ? load["magnitude"].get<double>()
                        : 0.0;
                std::unordered_set<int> node_set(node_ids.begin(), node_ids.end());
                // Element boundary faces: 4 triangular faces for a tet (corners 0..3,
                // SAME order as the oracle — tet path stays byte-identical), or 6 quad
                // faces for a hex8. The area-weighted polygon normal via a fan
                // triangulation from the face's first corner handles tris (1 fan tri) and
                // quads (2 fan tris) uniformly.
                static const int tet_faces[4][4] = {
                    {0, 1, 2, -1}, {0, 1, 3, -1}, {0, 2, 3, -1}, {1, 2, 3, -1}};
                static const int hex_faces[6][4] = {{0, 1, 2, 3}, {4, 5, 6, 7}, {0, 1, 5, 4},
                                                    {2, 3, 7, 6}, {0, 3, 7, 4}, {1, 2, 6, 5}};
                const bool hexEl = (nodes_per_elem == 8);
                const int n_faces = hexEl ? 6 : 4;
                const int fk = hexEl ? 4 : 3;  // corners per face
                std::vector<double> node_force(static_cast<size_t>(n_nodes) * 3, 0.0);
                double total_area = 0.0;
                int face_count = 0;
                const size_t stride = static_cast<size_t>(nodes_per_elem);
                const size_t m = tets.size() / stride;
                for (size_t e = 0; e < m; ++e) {
                    const int* el = &tets[e * stride];
                    for (int f = 0; f < n_faces; ++f) {
                        const int* fc = hexEl ? hex_faces[f] : tet_faces[f];
                        int fn[4];
                        bool all_in = true;
                        for (int i = 0; i < fk; ++i) {
                            fn[i] = el[fc[i]];
                            if (!node_set.count(fn[i])) { all_in = false; break; }
                        }
                        if (!all_in) continue;
                        // Area-weighted normal = sum of fan-triangle (p0,pi,pi+1) cross/2.
                        const double* p0 = &points[static_cast<size_t>(fn[0]) * 3];
                        double nvx = 0.0, nvy = 0.0, nvz = 0.0;
                        for (int i = 1; i < fk - 1; ++i) {
                            const double* pa = &points[static_cast<size_t>(fn[i]) * 3];
                            const double* pb = &points[static_cast<size_t>(fn[i + 1]) * 3];
                            const double ax = pa[0] - p0[0], ay = pa[1] - p0[1], az = pa[2] - p0[2];
                            const double bx = pb[0] - p0[0], by = pb[1] - p0[1], bz = pb[2] - p0[2];
                            nvx += 0.5 * (ay * bz - az * by);
                            nvy += 0.5 * (az * bx - ax * bz);
                            nvz += 0.5 * (ax * by - ay * bx);
                        }
                        const double area = std::sqrt(nvx * nvx + nvy * nvy + nvz * nvz);
                        if (area < 1e-12) continue;
                        const double nx = nvx / area, ny = nvy / area, nz = nvz / area;
                        // face_force = pressure * area * normal, split equally per corner.
                        const double ffx = pressure * area * nx / fk;
                        const double ffy = pressure * area * ny / fk;
                        const double ffz = pressure * area * nz / fk;
                        for (int i = 0; i < fk; ++i) {
                            node_force[static_cast<size_t>(fn[i]) * 3 + 0] += ffx;
                            node_force[static_cast<size_t>(fn[i]) * 3 + 1] += ffy;
                            node_force[static_cast<size_t>(fn[i]) * 3 + 2] += ffz;
                        }
                        total_area += area;
                        face_count += 1;
                    }
                }
                if (face_count == 0) {
                    // Fallback: equal split of (pressure * 1.0) over node_ids.
                    const double equiv = pressure * 1.0;
                    const double inv = 1.0 / static_cast<double>(node_ids.size());
                    for (int nid : node_ids)
                        for (int c = 0; c < 3; ++c) nodal_forces[nid * 3 + c] += equiv * inv;
                    notes.push_back(
                        "Pressure load: no boundary faces found — equal split over " +
                        std::to_string(node_ids.size()) + " nodes (area unknown)");
                } else {
                    for (int nid : node_ids)
                        for (int c = 0; c < 3; ++c)
                            nodal_forces[nid * 3 + c] +=
                                node_force[static_cast<size_t>(nid) * 3 + c];
                    notes.push_back("Pressure on " + std::to_string(face_count) +
                                    " faces (total area " + std::to_string(total_area) +
                                    " mm2)");
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
            std::vector<int> node_ids = nodes_in_faceset(points, sample, tol);
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
    std::string err;
    std::vector<std::string> notes;
    // Modal stays on 1st-order elements (the quadratic mass matrix is not implemented
    // yet). Force order 1 regardless of mesh_order so the eigen-solve gets tet4.
    json modal_study = study;
    if (study.value("mesh_order", 1) == 2)
        notes.push_back("modal uses 1st-order elements (mesh_order=2 ignored)");
    modal_study["mesh_order"] = 1;
    if (study.contains("nodes_per_elem")) modal_study["nodes_per_elem"] = 4;
    if (!resolve_mesh(modal_study, points, tets, nodes_per_elem, err))
        return json{{"ok", false}, {"error", err}};

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
            std::vector<int> node_ids = nodes_in_faceset(points, sample, tol);
            for (int nid : node_ids)
                if (seen.insert(nid).second) fixed_nodes.push_back(nid);
        }
    }
    std::sort(fixed_nodes.begin(), fixed_nodes.end());

    FeaModalResult r =
        fea_modal_solve(points, tets, E_mpa, nu, density_g_cm3, fixed_nodes, n_modes);
    if (!r.ok) return json{{"ok", false}, {"error", r.error}};

    return json{{"ok", true},
                {"freqs_hz", r.freqs_hz},
                {"mode_shapes", r.mode_shapes},
                {"n_modes", r.n_modes},
                {"n_nodes", r.n_nodes},
                {"n_dof", r.n_dof},
                {"solver_notes", notes}};
}

}  // namespace aicad::sim
