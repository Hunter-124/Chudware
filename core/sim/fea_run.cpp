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
#include <cstring>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <functional>
#include <map>
#include <set>
#include <iterator>
#include <limits>
#include <list>
#include <mutex>
#include <string>
#include <system_error>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "sim/fea_contact.h"
#include "sim/fea_modal.h"
#include "sim/fea_static.h"
#include "sim/fea_thermal.h"
#include "sim/mesher.h"
#include "sim/sim_cache.h"
#include "sim/sim_cancel.h"
#include "sim/vtu_writer.h"
#include "util/resource_governor.h"
#include "version_info.h"  // CMake-configured CHUDWARE_HOSTD_BUILD_ID (engine build id)

using nlohmann::json;

namespace chudware::sim {

namespace detail {
// The one advisor->JSON shape. Every study route that can carry a learned-advisor
// envelope reports THIS object, sim/fea_adaptive.cpp included (it declares this
// function and attaches the envelope its own mesher produced), so no route can drift
// into a second spelling of the same telemetry.
json mesh_advisor_json(const MeshAdvisor& advisor) {
    return json{{"available", advisor.available},
                {"applied", advisor.applied},
                {"vetoed", advisor.vetoed},
                {"budget_refusal", advisor.budget_refusal},
                {"mesher", advisor.mesher},
                {"h_rel", advisor.h_rel},
                {"order", advisor.order},
                {"adapt_passes", advisor.adapt_passes},
                {"eta_target", advisor.eta_target},
                {"predicted_dof", advisor.predicted_dof},
                {"predicted_mesh_ms", advisor.predicted_mesh_ms},
                {"predicted_solve_ms", advisor.predicted_solve_ms},
                {"failure_prob", advisor.failure_prob},
                {"ood_distance", advisor.ood_distance},
                {"model_revision", advisor.model_revision},
                {"note", advisor.note}};
}

// ── Run evidence: mesh quality, provenance, and the optional .vtu artifact ───────
// Every solve route reports the same three objects, assembled here so a static, an
// adaptive, a modal and a thermal result cannot describe their mesh or their build
// in three different dialects.

// Quality metrics of the mesh a study actually solved, carried out of resolve_mesh.
// `valid` is false when the metrics do not apply (non-tetrahedral inline mesh) or
// could not be computed, in which case `note` says why and the metric values are
// reported as null rather than as a plausible-looking zero.
struct MeshQualityFacts {
    bool valid = false;
    double aspect_min = 0.0;
    double aspect_avg = 0.0;
    double aspect_max = 0.0;
    double jacobian_min = 0.0;
    double pct_poor = 0.0;
    double min_tet_volume_mm3 = 0.0;
    double max_tet_edge_ratio = 0.0;
    // How many tet10 elements kept straight mid-edge nodes because projecting
    // them onto the CAD boundary folded the element (thin walls / tight
    // curvature). Always reported: it is a count, not a metric, and a nonzero
    // value is exactly why a thin-wall study can run at order 2 at all.
    int quadratic_fallback_elems = 0;
    // Node-adjacency components over the elements (mesher.h MeshOut). 0 means
    // the count was not available for this mesh.
    int connected_components = 0;
    // Components that reach NO fixture DOF, resolved by the static route once
    // its fixtures are known: each one is free to translate and rotate, so the
    // system is a mechanism and its "solution" is arbitrary. -1 means the route
    // never resolved fixtures (a mesh-level report, modal, thermal), which is
    // NOT the same statement as zero.
    int unfixed_components = -1;
    // Hanging-element repair (mesher.h MeshOut). A corner node referenced by ONE
    // element is a rigid-body mechanism that every connectivity check passes, so
    // the counts ride with every mesh: repaired is what the mesher dissolved,
    // remaining is what it could not, and remaining > 0 is a refusal.
    int lonely_nodes_repaired = 0;
    int lonely_nodes_remaining = 0;
    int hanging_elems_dropped = 0;
    // Thinnest wall in element sizes (mesher.h MeshOut). 0 = unavailable.
    double thin_wall_ratio = 0.0;
    std::string note;
};

json mesh_quality_json(const MeshQualityFacts& quality, std::size_t n_nodes,
                       std::size_t n_elems, int nodes_per_elem) {
    json out = {{"n_nodes", n_nodes},
                {"n_elems", n_elems},
                {"order", nodes_per_elem == 10 ? 2 : 1}};
    static constexpr const char* kMetrics[] = {
        "aspect_min", "aspect_avg", "aspect_max", "jacobian_min",
        "pct_poor",   "min_tet_volume_mm3",       "max_tet_edge_ratio"};
    const double values[] = {quality.aspect_min,        quality.aspect_avg,
                             quality.aspect_max,        quality.jacobian_min,
                             quality.pct_poor,          quality.min_tet_volume_mm3,
                             quality.max_tet_edge_ratio};
    for (std::size_t i = 0; i < std::size(kMetrics); ++i)
        out[kMetrics[i]] = quality.valid ? json(values[i]) : json(nullptr);
    out["lonely_nodes_repaired"] = quality.lonely_nodes_repaired;
    out["lonely_nodes_remaining"] = quality.lonely_nodes_remaining;
    out["hanging_elems_dropped"] = quality.hanging_elems_dropped;
    out["quadratic_fallback_elems"] = quality.quadratic_fallback_elems;
    // Connectivity and thin-wall facts are null when unavailable rather than a
    // plausible-looking 1 / 0 the reader would take for a measurement.
    out["connected_components"] = quality.connected_components > 0
                                      ? json(quality.connected_components)
                                      : json(nullptr);
    out["thin_wall_ratio"] = quality.thin_wall_ratio > 0.0
                                 ? json(quality.thin_wall_ratio)
                                 : json(nullptr);
    // Present only on a route that actually resolved fixtures; a modal or
    // mesh-level report must not imply it checked.
    if (quality.unfixed_components >= 0)
        out["unfixed_components"] = quality.unfixed_components;
    if (!quality.note.empty()) out["note"] = quality.note;
    return out;
}

std::string utc_now_iso8601() {
    const std::time_t now = std::time(nullptr);
    std::tm parts{};
#if defined(_WIN32)
    gmtime_s(&parts, &now);
#else
    gmtime_r(&now, &parts);
#endif
    char buf[32] = {0};
    if (std::strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%SZ", &parts) == 0)
        return std::string();
    return std::string(buf);
}

// Which engine, which mesher, which commit, when — the reproducibility half of a
// result. `solver` is the canonical route name ("chudware/fea_static", …).
json provenance_json(const char* solver) {
    return json{{"engine_build_id", CHUDWARE_HOSTD_BUILD_ID},
                {"polymesh_commit", CHUDWARE_POLYMESH_COMMIT},
                {"solver", solver},
                {"mesher", "polyhedral-fea/graded-tet"},
                {"timestamp_utc", utc_now_iso8601()}};
}

bool vtu_evidence_missing(const nlohmann::json& study) {
    if (!study.is_object() || !study.contains("vtu_path") ||
        !study["vtu_path"].is_string())
        return false;
    const std::string path = study["vtu_path"].get<std::string>();
    if (path.empty()) return false;
    std::error_code ec;
    const bool present = std::filesystem::exists(std::filesystem::path(path), ec);
    return ec ? true : !present;
}

bool write_result_vtu(const nlohmann::json& study, const std::vector<double>& points,
                      const std::vector<int>& tets, int nodes_per_elem,
                      const nlohmann::json& result, std::string& path_out,
                      std::string& error) {
    path_out.clear();
    error.clear();
    if (!study.is_object() || !study.contains("vtu_path")) return true;
    const json& requested = study["vtu_path"];
    if (!requested.is_string() || requested.get<std::string>().empty()) {
        error = "vtu_path must be a non-empty absolute path";
        return false;
    }
    const std::string path = requested.get<std::string>();
    if (!std::filesystem::path(path).is_absolute()) {
        error = "vtu_path must be an absolute path, got '" + path + "'";
        return false;
    }
    const std::size_t n_nodes = points.size() / 3;
    auto nodal = [&](const char* key) {
        std::vector<double> values;
        if (result.contains(key) && result[key].is_array()) {
            values.reserve(result[key].size());
            for (const auto& x : result[key])
                values.push_back(x.is_number() ? x.get<double>() : 0.0);
        }
        return values;
    };
    std::vector<VtuPointField> fields;
    {
        std::vector<double> disp = nodal("displacement");
        if (disp.size() == n_nodes * 3)
            fields.push_back(VtuPointField{"displacement", 3, std::move(disp)});
    }
    // von_mises is the node-averaged nodal field the summary reduces; summary_node
    // is the 1/0 mask that reduction uses (0 = inside the ideal-fixture ring the
    // solver excludes as a clamp singularity). Both ride in the file, so
    //   max{ von_mises[i] : summary_node[i] == 1 } == summary.max_von_mises_mpa
    //   max{ von_mises[i] }                        == summary.max_von_mises_raw_mpa
    // hold by construction and the field can never silently contradict the packet.
    for (const char* scalar : {"von_mises", "summary_node", "temperature"}) {
        std::vector<double> values = nodal(scalar);
        if (values.size() == n_nodes)
            fields.push_back(VtuPointField{scalar, 1, std::move(values)});
    }
    // Modal: mode_shapes is flat n_modes*n*3, so each eigenvector becomes its own
    // 3-vector field and a reader can animate one mode at a time.
    {
        const std::vector<double> shapes = nodal("mode_shapes");
        const std::size_t per_mode = n_nodes * 3;
        if (per_mode > 0 && !shapes.empty() && shapes.size() % per_mode == 0) {
            const std::size_t n_modes = shapes.size() / per_mode;
            for (std::size_t m = 0; m < n_modes; ++m)
                fields.push_back(VtuPointField{
                    "mode_" + std::to_string(m + 1), 3,
                    std::vector<double>(shapes.begin() + static_cast<std::ptrdiff_t>(m * per_mode),
                                        shapes.begin() +
                                            static_cast<std::ptrdiff_t>((m + 1) * per_mode))});
        }
    }
    if (fields.empty()) {
        error = "vtu_path was requested but the result carries no nodal field "
                "matching the solved mesh";
        return false;
    }
    if (!write_vtu(path, points, tets, nodes_per_elem, fields, error)) return false;
    path_out = path;
    return true;
}
}  // namespace detail

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

class SimJobScope {
public:
    explicit SimJobScope(const json& study,
                         const SimCancelToken& inherited_token = {})
        : id_(study.is_object() && study.contains("job_id") &&
                      study["job_id"].is_string()
                  ? study["job_id"].get<std::string>()
                  : std::string()),
          token_(inherited_token ? inherited_token : sim_job_register(id_)),
          owns_registration_(!inherited_token) {}
    ~SimJobScope() {
        if (owns_registration_) sim_job_unregister(id_, token_);
    }
    const SimCancelToken& token() const { return token_; }
    bool cancelled() const {
        return token_ && token_->load(std::memory_order_relaxed);
    }

private:
    std::string id_;
    SimCancelToken token_;
    bool owns_registration_ = false;
};

bool finite3(const json& v) {
    if (!v.is_array() || v.size() != 3) return false;
    for (const auto& x : v) {
        if (!x.is_number()) return false;
        const double d = x.get<double>();
        if (!std::isfinite(d)) return false;
    }
    return true;
}

bool nonzero3(const json& v) {
    double n2 = 0.0;
    for (const auto& x : v) {
        const double d = x.get<double>();
        n2 += d * d;
    }
    return std::isfinite(n2) && n2 > 0.0;
}

bool has_mesh_source(const json& study) {
    const bool step = study.contains("step_path") && study["step_path"].is_string() &&
                      !study["step_path"].get<std::string>().empty();
    const bool inl = study.contains("points") && study["points"].is_array() &&
                     !study["points"].empty() && study.contains("tets") &&
                     study["tets"].is_array() && !study["tets"].empty();
    return step || inl;
}
bool finite_vec3_json(const json& value) {
    if (!value.is_array() || value.size() < 3) return false;
    for (int i = 0; i < 3; ++i)
        if (!value[i].is_number() ||
            !std::isfinite(value[i].get<double>()))
            return false;
    return true;
}

bool validate_faceset_input(const json& fs, std::string& error) {
    if (!fs.is_object()) {
        error = "faceset must be an object";
        return false;
    }
    if (!fs.contains("points") || !fs["points"].is_array() ||
        fs["points"].empty()) {
        error = "faceset requires a non-empty points array";
        return false;
    }
    size_t flat_count = 0;
    for (const auto& point : fs["points"]) {
        if (point.is_array()) {
            if (!finite_vec3_json(point)) {
                error = "faceset points must contain finite 3-vectors";
                return false;
            }
            flat_count += 3;
        } else if (point.is_number() &&
                   std::isfinite(point.get<double>())) {
            ++flat_count;
        } else {
            error = "faceset points must contain finite numbers";
            return false;
        }
    }
    if (flat_count < 3 || flat_count % 3 != 0) {
        error = "faceset points must contain complete 3-vectors";
        return false;
    }
    for (const char* key : {"tol_mm", "perp_tol_mm"}) {
        if (!fs.contains(key)) continue;
        if (!fs[key].is_number() ||
            !std::isfinite(fs[key].get<double>()) ||
            !(fs[key].get<double>() > 0.0)) {
            error = std::string("faceset ") + key +
                    " must be finite and positive";
            return false;
        }
    }
    if (fs.contains("face_normal")) {
        if (!finite_vec3_json(fs["face_normal"])) {
            error = "faceset face_normal must be a finite 3-vector";
            return false;
        }
        double norm2 = 0.0;
        for (int i = 0; i < 3; ++i) {
            const double x = fs["face_normal"][i].get<double>();
            norm2 += x * x;
        }
        if (!std::isfinite(norm2) || !(norm2 > 0.0)) {
            error = "faceset face_normal must be non-zero";
            return false;
        }
    }
    if (fs.contains("surface")) {
        const json& surface = fs["surface"];
        if (!surface.is_object() || !surface.contains("type") ||
            !surface["type"].is_string()) {
            error = "faceset surface requires a string type";
            return false;
        }
        const std::string type = surface["type"].get<std::string>();
        auto positive_number = [&](const char* key) {
            return surface.contains(key) && surface[key].is_number() &&
                   std::isfinite(surface[key].get<double>()) &&
                   surface[key].get<double>() > 0.0;
        };
        auto valid_axis = [&](const json& value) {
            if (!finite_vec3_json(value)) return false;
            double norm2 = 0.0;
            for (int i = 0; i < 3; ++i) {
                const double x = value[i].get<double>();
                norm2 += x * x;
            }
            return std::isfinite(norm2) && norm2 > 0.0;
        };
        if (!finite_vec3_json(surface.value("origin", json::array()))) {
            error = "faceset surface requires a finite origin";
            return false;
        }
        if (type == "sphere") {
            if (!positive_number("radius")) {
                error = "sphere faceset requires a positive finite radius";
                return false;
            }
        } else if (type == "cylinder") {
            if (!positive_number("radius") ||
                !valid_axis(surface.value("axis", json::array()))) {
                error = "cylinder faceset requires positive radius and non-zero finite axis";
                return false;
            }
        } else if (type == "cone") {
            if (!positive_number("semi_angle_rad") ||
                surface["semi_angle_rad"].get<double>() >= 1.5707963 ||
                !valid_axis(surface.value("axis", json::array()))) {
                error = "cone faceset requires a valid semi-angle and non-zero finite axis";
                return false;
            }
        } else if (type == "torus") {
            if (!positive_number("major_radius") ||
                !positive_number("minor_radius") ||
                !valid_axis(surface.value("axis", json::array()))) {
                error = "torus faceset requires positive radii and non-zero finite axis";
                return false;
            }
        } else {
            error = "unsupported faceset surface type '" + type + "'";
            return false;
        }
    }
    return true;
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
    const double norm = std::sqrt(face_normal[0] * face_normal[0] +
                                  face_normal[1] * face_normal[1] +
                                  face_normal[2] * face_normal[2]);
    if (!std::isfinite(norm) || !(norm > 0.0) || !std::isfinite(perp_tol) ||
        !(perp_tol > 0.0))
        return false;
    for (double& x : face_normal) x /= norm;
    return true;
}

// Resolve the mesh: inline points/tets if present, otherwise mesh the STEP
// file. `quality` reports the metrics of whichever mesh was resolved, so every
// study result can describe the discretization it actually solved.
bool resolve_mesh(const json &study, std::vector<double> &points,
                  std::vector<int> &tets, int &nodes_per_elem,
                  std::string &mesh_type_used, std::string &note,
                  std::optional<MeshAdvisor> &advisor,
                  detail::MeshQualityFacts &quality, std::string &err) {
  nodes_per_elem = 4;
  mesh_type_used.clear();
  note.clear();
  advisor.reset();
  quality = detail::MeshQualityFacts{};
  const bool has_step = study.contains("step_path") &&
                        study["step_path"].is_string() &&
                        !study["step_path"].get<std::string>().empty();
  if (has_step) {
    static constexpr const char *kRemovedMeshOptions[] = {
        "density",         "curvature",        "curv_elems_per_circle",
        "optimize",        "mesher_backend",   "structured",
        "mesh_type",       "auto_type",        "quad_surface",
        "fallback_to_tet", "thin_wall_layers", "algorithm3d"};
    for (const char *key : kRemovedMeshOptions) {
      if (study.contains(key)) {
        err = std::string("mesh option '") + key +
              "' was removed with the Polyhedral-FEA cutover";
        return false;
      }
    }
  }
  if (!has_step && study.contains("points") && study["points"].is_array() &&
      study.contains("tets") && study["tets"].is_array()) {
    points = json_doubles(study["points"]);
    tets = json_ints(study["tets"]);
    if (points.empty() || tets.empty()) {
      err = "inline mesh has empty points/tets";
      return false;
    }
    nodes_per_elem = study.value("nodes_per_elem", 4);
    if (study.value("learned_advisor", false)) {
      MeshAdvisor unavailable;
      unavailable.order = nodes_per_elem == 10 ? 2 : 1;
      unavailable.mesher = "inline";
      unavailable.model_revision = "not-loaded";
      unavailable.note =
          "advisor unavailable: learned advising requires STEP geometry; "
          "configured inline mesh retained";
      advisor = std::move(unavailable);
    }
    // Inline meshes report the SAME metrics a meshed study does: fill_quality only
    // reads points/tets/nodes_per_elem, so the arrays are moved into a probe and
    // straight back out rather than copied.
    if (nodes_per_elem == 4 || nodes_per_elem == 10) {
      MeshOut probe;
      probe.points = std::move(points);
      probe.tets = std::move(tets);
      probe.nodes_per_elem = nodes_per_elem;
      if (detail::fill_quality(probe)) {
        quality.valid = true;
        quality.aspect_min = probe.aspect_min;
        quality.aspect_avg = probe.aspect_avg;
        quality.aspect_max = probe.aspect_max;
        quality.jacobian_min = probe.jacobian_min;
        quality.pct_poor = probe.pct_poor;
        quality.min_tet_volume_mm3 = probe.min_tet_volume_mm3;
        quality.max_tet_edge_ratio = probe.max_tet_edge_ratio;
        quality.connected_components = probe.connected_components;
        // An inline mesh is the caller's own array; it is COUNTED, never silently
        // repaired, so a hanging fragment becomes a refusal naming it rather than
        // an edit to a mesh someone else owns.
        quality.lonely_nodes_remaining = detail::count_lonely_nodes(probe);
      } else {
        quality.note = probe.error;
      }
      points = std::move(probe.points);
      tets = std::move(probe.tets);
    } else {
      quality.note = "tetrahedral quality metrics do not apply to a " +
                     std::to_string(nodes_per_elem) + "-node element mesh";
    }
    return true;
  }
  if (has_step) {
    MeshOptions options;
    options.fineness = study.value("fineness", 0.5);
    options.order = study.value("mesh_order", study.value("order", 1));
    options.target_size_mm = study.value("target_size_mm", 0.0);
    options.min_size_mm = study.value("min_size_mm", 0.0);
    options.max_size_mm = study.value("max_size_mm", 0.0);
    options.learned_advisor = study.value("learned_advisor", false);
    options.advisor_max_dof = study.value("advisor_max_dof", uint32_t{0});
    const auto mesh =
        mesh_step_cached(study["step_path"].get<std::string>(), options);
    if (!mesh->ok) {
      err = "mesh failed: " + mesh->error;
      return false;
    }
    points = mesh->points;
    tets = mesh->tets;
    nodes_per_elem = mesh->nodes_per_elem;
    mesh_type_used = mesh->mesh_type_used;
    note = mesh->note;
    advisor = mesh->advisor;
    quality.valid = true;
    quality.aspect_min = mesh->aspect_min;
    quality.aspect_avg = mesh->aspect_avg;
    quality.aspect_max = mesh->aspect_max;
    quality.jacobian_min = mesh->jacobian_min;
    quality.pct_poor = mesh->pct_poor;
    quality.quadratic_fallback_elems = mesh->quadratic_fallback_elems;
    quality.min_tet_volume_mm3 = mesh->min_tet_volume_mm3;
    quality.max_tet_edge_ratio = mesh->max_tet_edge_ratio;
    quality.connected_components = mesh->connected_components;
    quality.thin_wall_ratio = mesh->thin_wall_ratio;
    quality.lonely_nodes_repaired = mesh->lonely_nodes_repaired;
    quality.lonely_nodes_remaining = mesh->lonely_nodes_remaining;
    quality.hanging_elems_dropped = mesh->hanging_elems_dropped;
    return true;
  }
  err = "study must have inline points/tets or a step_path";
  return false;
}

// ── Work-equivalent (consistent) surface-load distribution ───────────────────────
// A uniform surface traction does NOT split equally across the nodes of a quadratic
// (tet10) face. On a flat tri6, the work-equivalent rule is corner=0 and each midside=1/3
// of the total; mapped quadrature generalizes those weights to curved quadratic faces. The prior
// code lumped the whole face onto the 3 CORNER nodes (and never touched the mid-side
// nodes that actually carry the quadratic load), which is why an axial FORCE produced a
// large corner stress spike and a PRESSURE on a tet10 bar read δ/σ several-fold high.
//
// We accumulate, per loaded boundary face, the consistent nodal weights w_i = ∫ N_i dA
// and the area-weighted outward normal, then:
//   • force  load: distribute the TOTAL force F by w_i / Σw (the resultant stays exactly
//                  F, but the spatial distribution is correct so no spurious corner spike).
//   • pressure load: add −p · (∫ N_i n̂ dA) per node (the textbook consistent pressure
//                    load), the area-weighted form of the same weights. The MINUS is the
//                    convention: n̂ is the OUTWARD normal and a positive pressure PUSHES
//                    INTO the surface, so +p on a bore is internal pressurisation
//                    (documented as "MPa, inward" by run_fea_static / the resultant the
//                    render annotation quotes).
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

bool tri6_face_weights(const std::vector<double>& points, const int* el,
                       const FaceTab& ft, double weights[6],
                       double normal_weights[6][3], double& area) {
    int fn[6] = {el[ft.corner[0]], el[ft.corner[1]], el[ft.corner[2]],
                 el[ft.mid[0]], el[ft.mid[1]], el[ft.mid[2]]};
    auto coord = [&](int local, int component) {
        return points[static_cast<size_t>(fn[local]) * 3 + component];
    };
    auto eval = [&](double r, double s, double N[6], double dNr[6],
                    double dNs[6], double p[3], double xr[3], double xs[3]) {
        const double L0 = 1.0 - r - s;
        const double Nv[6] = {
            L0 * (2.0 * L0 - 1.0), r * (2.0 * r - 1.0),
            s * (2.0 * s - 1.0), 4.0 * L0 * r, 4.0 * r * s,
            4.0 * s * L0};
        const double drv[6] = {
            1.0 - 4.0 * L0, 4.0 * r - 1.0, 0.0,
            4.0 * (L0 - r), 4.0 * s, -4.0 * s};
        const double dsv[6] = {
            1.0 - 4.0 * L0, 0.0, 4.0 * s - 1.0,
            -4.0 * r, 4.0 * r, 4.0 * (L0 - s)};
        for (int c = 0; c < 3; ++c) p[c] = xr[c] = xs[c] = 0.0;
        for (int i = 0; i < 6; ++i) {
            N[i] = Nv[i];
            dNr[i] = drv[i];
            dNs[i] = dsv[i];
            for (int c = 0; c < 3; ++c) {
                p[c] += N[i] * coord(i, c);
                xr[c] += dNr[i] * coord(i, c);
                xs[c] += dNs[i] * coord(i, c);
            }
        }
    };
    double center[3] = {0.0, 0.0, 0.0};
    for (int i = 0; i < 4; ++i)
        for (int c = 0; c < 3; ++c)
            center[c] +=
                points[static_cast<size_t>(el[i]) * 3 + c] / 4.0;
    double N0[6], dr0[6], ds0[6], face_center[3], xr0[3], xs0[3];
    eval(1.0 / 3.0, 1.0 / 3.0, N0, dr0, ds0, face_center, xr0, xs0);
    const double reference_normal[3] = {
        xr0[1] * xs0[2] - xr0[2] * xs0[1],
        xr0[2] * xs0[0] - xr0[0] * xs0[2],
        xr0[0] * xs0[1] - xr0[1] * xs0[0]};
    const double reference_norm2 =
        reference_normal[0] * reference_normal[0] +
        reference_normal[1] * reference_normal[1] +
        reference_normal[2] * reference_normal[2];
    if (!(reference_norm2 > 0.0) || !std::isfinite(reference_norm2))
        return false;
    const double outward_dot =
        reference_normal[0] * (face_center[0] - center[0]) +
        reference_normal[1] * (face_center[1] - center[1]) +
        reference_normal[2] * (face_center[2] - center[2]);
    if (outward_dot == 0.0 || !std::isfinite(outward_dot)) return false;
    const double orientation = outward_dot > 0.0 ? 1.0 : -1.0;
    static constexpr double gx[5] = {
        0.0469100770306680, 0.2307653449471585, 0.5,
        0.7692346550528415, 0.9530899229693320};
    static constexpr double gw[5] = {
        0.1184634425280945, 0.2393143352496832, 0.2844444444444444,
        0.2393143352496832, 0.1184634425280945};
    area = 0.0;
    for (int i = 0; i < 6; ++i) {
        weights[i] = 0.0;
        for (int c = 0; c < 3; ++c) normal_weights[i][c] = 0.0;
    }
    for (int iu = 0; iu < 5; ++iu)
        for (int iv = 0; iv < 5; ++iv) {
            const double r = gx[iu], s = (1.0 - r) * gx[iv];
            double N[6], dNr[6], dNs[6], p[3], xr[3], xs[3];
            eval(r, s, N, dNr, dNs, p, xr, xs);
            const double cross[3] = {
                xr[1] * xs[2] - xr[2] * xs[1],
                xr[2] * xs[0] - xr[0] * xs[2],
                xr[0] * xs[1] - xr[1] * xs[0]};
            const double jac =
                std::sqrt(cross[0] * cross[0] + cross[1] * cross[1] +
                          cross[2] * cross[2]);
            const double alignment =
                cross[0] * reference_normal[0] +
                cross[1] * reference_normal[1] +
                cross[2] * reference_normal[2];
            if (!(jac > 0.0) || !(alignment > 0.0) ||
                !std::isfinite(jac) || !std::isfinite(alignment))
                return false;
            const double duffy_weight =
                (1.0 - r) * gw[iu] * gw[iv];
            area += jac * duffy_weight;
            for (int i = 0; i < 6; ++i) {
                weights[i] += N[i] * jac * duffy_weight;
                for (int c = 0; c < 3; ++c)
                    normal_weights[i][c] +=
                        orientation * N[i] * cross[c] * duffy_weight;
            }
        }
    return std::isfinite(area) && area > 0.0;
}

// Accumulate the consistent boundary integral over every element face whose CORNER nodes
// are ALL in `node_set`. For each loaded face it adds, per node:
//   • node_w[i]   += ∫ N_i dA                  (scalar tributary, for the force split)
//   • node_nw[3i] += (∫ N_i dA) · n̂_face       (vector pressure weight ∫ N_i n̂ dA — the
//                                               per-face outward normal so a CURVED loaded
//                                               surface gets the right directional load).
// Returns the loaded boundary area. Handles tet4/tet10/hex8. Tet10 uses mapped tri6
// quadrature so curved midside geometry contributes the correct scalar and vector surface
// Jacobians; tet4/hex8 retain their linear equal-per-corner rule.
//
// A tet10 face whose projected mid nodes still fold it after the mesher's element-local
// straight-edge fallback (detail::repair_folded_quadratic) is unusable: the surface
// Jacobian reverses inside the face, so the integral is meaningless. That returns NaN and
// fills `fold_detail` with the offending element index and its location, because "somewhere
// in your mesh a face is folded" is not an actionable diagnostic on a 200k-element frame.
double accumulate_face_weights(const std::vector<double>& points, const std::vector<int>& tets,
                               int nodes_per_elem, const std::unordered_set<int>& node_set,
                               std::vector<double>& node_w, std::vector<double>& node_nw,
                               std::string& fold_detail) {
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
    auto face_key = [&](const int* el, const int* fc) {
        std::array<int, 4> key{-1, -1, -1, -1};
        for (int i = 0; i < fk; ++i) key[i] = el[fc[i]];
        std::sort(key.begin(), key.begin() + fk);
        return key;
    };
    std::map<std::array<int, 4>, int> face_owners;
    for (size_t e = 0; e < m; ++e) {
        const int* el = &tets[e * stride];
        for (int f = 0; f < n_faces; ++f) {
            const int* fc = hexEl ? kHexFaces[f] : kTetFaces[f].corner;
            ++face_owners[face_key(el, fc)];
        }
    }
    double total_area = 0.0;
    for (size_t e = 0; e < m; ++e) {
        const int* el = &tets[e * stride];
        for (int f = 0; f < n_faces; ++f) {
            const int* fc = hexEl ? kHexFaces[f] : kTetFaces[f].corner;
            const auto own = face_owners.find(face_key(el, fc));
            if (own == face_owners.end() || own->second != 1) continue;
            int cn[4];
            bool all_in = true;
            for (int i = 0; i < fk; ++i) {
                cn[i] = el[fc[i]];
                if (!node_set.count(cn[i])) { all_in = false; break; }
            }
            if (!all_in) continue;
            if (tet10) {
                double weights[6], normal_weights[6][3], curved_area = 0.0;
                const FaceTab& ft = kTetFaces[f];
                if (!tri6_face_weights(points, el, ft, weights, normal_weights,
                                       curved_area)) {
                    double centroid[3] = {0.0, 0.0, 0.0};
                    for (int i = 0; i < 4; ++i)
                        for (int c = 0; c < 3; ++c)
                            centroid[c] +=
                                points[static_cast<size_t>(el[i]) * 3 + c] / 4.0;
                    char where[160];
                    std::snprintf(where, sizeof(where),
                                  " (element %zu, face %d, at (%.3f, %.3f, %.3f) mm)",
                                  e, f, centroid[0], centroid[1], centroid[2]);
                    fold_detail = where;
                    return std::numeric_limits<double>::quiet_NaN();
                }
                const int fn[6] = {cn[0], cn[1], cn[2], el[ft.mid[0]],
                                   el[ft.mid[1]], el[ft.mid[2]]};
                for (int i = 0; i < 6; ++i) {
                    node_w[static_cast<size_t>(fn[i])] += weights[i];
                    for (int c = 0; c < 3; ++c)
                        node_nw[static_cast<size_t>(fn[i]) * 3 + c] +=
                            normal_weights[i][c];
                }
                total_area += curved_area;
                continue;
            }
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
            // tri3 (tet4) / quad (hex8): equal A/fk per corner.
            for (int i = 0; i < fk; ++i)
                add(cn[i], area / static_cast<double>(fk));
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
// On a flat tri6 face, the work-equivalent rule puts ALL the consistent load on the
// mid-side nodes (corner weight = 0, each mid-side = A/3). So `w_sum`, summed over
// the corner-dominated geometric set, collapses toward 0 → the work-consistent branch is
// skipped / divides by ~0 and the force falls back to an equal per-corner split, which is
// both wrong (puts load where the consistent rule says 0) AND non-convergent (a uniform
// axial traction read ~21× / 2.1× high, mesh-fineness dependent). See Bug Z1.
//
// FIX: a mid-edge node belongs to the loaded face exactly when BOTH of its endpoint corner
// nodes are already in the set. We walk the tet10 connectivity and add every such mid-edge
// node. Topologically exact, mesh-independent, and adds nothing for tet4/hex8 (no mid
// nodes). After this, every midside node needed by either flat or curved tri6 quadrature is
// in node_ids and the consistent resultant is preserved.
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

bool faceset_surface_normal(const FacesetSurface& s, const double* p,
                            double* normal) {
    const double vx = p[0] - s.origin[0], vy = p[1] - s.origin[1],
                 vz = p[2] - s.origin[2];
    const double h = vx * s.axis[0] + vy * s.axis[1] + vz * s.axis[2];
    const double rx = vx - h * s.axis[0], ry = vy - h * s.axis[1],
                 rz = vz - h * s.axis[2];
    const double radial = std::sqrt(rx * rx + ry * ry + rz * rz);
    if (s.kind == FacesetSurface::kSphere) {
        const double n = std::sqrt(vx * vx + vy * vy + vz * vz);
        if (!(n > 1e-12)) return false;
        normal[0] = vx / n; normal[1] = vy / n; normal[2] = vz / n;
        return true;
    }
    if (!(radial > 1e-12)) return false;
    const double ux = rx / radial, uy = ry / radial, uz = rz / radial;
    if (s.kind == FacesetSurface::kCylinder) {
        normal[0] = ux; normal[1] = uy; normal[2] = uz;
        return true;
    }
    if (s.kind == FacesetSurface::kCone) {
        normal[0] = std::cos(s.semi_angle) * ux -
                    std::sin(s.semi_angle) * s.axis[0];
        normal[1] = std::cos(s.semi_angle) * uy -
                    std::sin(s.semi_angle) * s.axis[1];
        normal[2] = std::cos(s.semi_angle) * uz -
                    std::sin(s.semi_angle) * s.axis[2];
        return true;
    }
    if (s.kind == FacesetSurface::kTorus) {
        const double dr = radial - s.r1;
        const double q = std::sqrt(dr * dr + h * h);
        if (!(q > 1e-12)) return false;
        normal[0] = (dr * ux + h * s.axis[0]) / q;
        normal[1] = (dr * uy + h * s.axis[1]) / q;
        normal[2] = (dr * uz + h * s.axis[2]) / q;
        return true;
    }
    return false;
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
    const double norm = std::sqrt(face_normal[0] * face_normal[0] +
                                  face_normal[1] * face_normal[1] +
                                  face_normal[2] * face_normal[2]);
    if (!std::isfinite(norm) || !(norm > 0.0) || !std::isfinite(perp_tol) ||
        !(perp_tol > 0.0))
        return false;
    for (double& x : face_normal) x /= norm;
    return true;
}
}  // namespace

std::vector<int> faceset_membership(const nlohmann::json& fs,
                                    const std::vector<double>& points) {
    uint64_t key =
        faceset_fnv1a(points.data(), points.size() * sizeof(double));
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
        if (std::isfinite(perp0) && perp0 > 0.0)
            ids = nodes_in_faceset_surface(points, sample, tol, surf, perp0);
    } else {
        std::array<double, 3> fn{};
        double perp_tol = 0.0;
        const bool has_fn = faceset_normal_local(fs, fn, perp_tol);
        if (has_fn)
            ids = nodes_in_faceset(points, sample, tol, fn.data(), perp_tol);
        else
            ids = nodes_in_faceset(points, sample, tol, nullptr, 0.0);
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

// Face selections describe the exterior skin. Analytic/planar tolerances are
// deliberately wider than a fine volume-mesh layer, so applying them to every
// volume node also captures near-surface interior nodes. Intersect the geometric
// match with nodes that actually belong to an exterior element face.
std::vector<int> boundary_faceset_membership(
    const nlohmann::json& fs, const std::vector<double>& points,
    const std::vector<int>& tets, int nodes_per_elem) {
    std::vector<int> ids = faceset_membership(fs, points);
    if (ids.empty() || tets.empty()) return ids;
    const bool hex = nodes_per_elem == 8;
    if (!hex && nodes_per_elem != 4 && nodes_per_elem != 10) return ids;
    const int nfaces = hex ? 6 : 4;
    const int corners_per_face = hex ? 4 : 3;
    const size_t stride = static_cast<size_t>(nodes_per_elem);
    std::map<std::array<int, 4>, int> owners;
    auto key_for = [&](const int* element, const int* face) {
        std::array<int, 4> key{-1, -1, -1, -1};
        for (int i = 0; i < corners_per_face; ++i) key[i] = element[face[i]];
        std::sort(key.begin(), key.begin() + corners_per_face);
        return key;
    };
    for (size_t e = 0; e < tets.size() / stride; ++e) {
        const int* element = &tets[e * stride];
        for (int face = 0; face < nfaces; ++face) {
            const int* table = hex ? kHexFaces[face] : kTetFaces[face].corner;
            ++owners[key_for(element, table)];
        }
    }
    FacesetSurface analytic_surface;
    const bool has_analytic_surface = faceset_surface(fs, analytic_surface);
    std::unordered_set<int> boundary;
    for (size_t e = 0; e < tets.size() / stride; ++e) {
        const int* element = &tets[e * stride];
        for (int face = 0; face < nfaces; ++face) {
            const int* table = hex ? kHexFaces[face] : kTetFaces[face].corner;
            if (owners[key_for(element, table)] != 1) continue;
            if (has_analytic_surface) {
                double center[3] = {0.0, 0.0, 0.0};
                for (int i = 0; i < corners_per_face; ++i) {
                    const double* p =
                        &points[static_cast<size_t>(element[table[i]]) * 3];
                    for (int c = 0; c < 3; ++c) center[c] += p[c];
                }
                for (double& value : center) value /= corners_per_face;
                const double* p0 =
                    &points[static_cast<size_t>(element[table[0]]) * 3];
                const double* p1 =
                    &points[static_cast<size_t>(element[table[1]]) * 3];
                const double* p2 =
                    &points[static_cast<size_t>(element[table[2]]) * 3];
                const double a[3] = {p1[0] - p0[0], p1[1] - p0[1],
                                     p1[2] - p0[2]};
                const double b[3] = {p2[0] - p0[0], p2[1] - p0[1],
                                     p2[2] - p0[2]};
                const double face_normal[3] = {
                    a[1] * b[2] - a[2] * b[1],
                    a[2] * b[0] - a[0] * b[2],
                    a[0] * b[1] - a[1] * b[0]};
                const double face_norm =
                    std::sqrt(face_normal[0] * face_normal[0] +
                              face_normal[1] * face_normal[1] +
                              face_normal[2] * face_normal[2]);
                double analytic_normal[3];
                if (!(face_norm > 1e-12) ||
                    !faceset_surface_normal(analytic_surface, center,
                                            analytic_normal))
                    continue;
                const double alignment =
                    std::fabs(face_normal[0] * analytic_normal[0] +
                              face_normal[1] * analytic_normal[1] +
                              face_normal[2] * analytic_normal[2]) /
                    face_norm;
                if (alignment < 0.5) continue;
            }
            for (int i = 0; i < corners_per_face; ++i)
                boundary.insert(element[table[i]]);
            if (nodes_per_elem == 10) {
                const FaceTab& ft = kTetFaces[face];
                for (int i = 0; i < 3; ++i) boundary.insert(element[ft.mid[i]]);
            }
        }
    }
    ids.erase(std::remove_if(ids.begin(), ids.end(),
                             [&](int id) { return !boundary.count(id); }),
              ids.end());
    return ids;
}

static nlohmann::json fea_run_static_impl(
    const nlohmann::json& study, const SimCancelToken& inherited_token) {
    SimJobScope job(study, inherited_token);
    if (!study.is_object())
        return json{{"ok", false}, {"error", "fea_static: study must be an object"}};
    if (!has_mesh_source(study))
        return json{{"ok", false},
                    {"error", "fea_static: a non-empty STEP path or inline mesh is required"}};
    if (job.cancelled())
        return json{{"ok", false}, {"error", "fea_static: cancelled"}};

    const json material_input = study.value("material", json::object());
    if (!material_input.is_object() || !material_input.contains("E_mpa") ||
        !material_input["E_mpa"].is_number() || !material_input.contains("nu") ||
        !material_input["nu"].is_number())
        return json{{"ok", false},
                    {"error", "fea_static: material requires numeric E_mpa and nu"}};
    const double E_input = material_input["E_mpa"].get<double>();
    const double nu_input = material_input["nu"].get<double>();
    const double yield_input =
        !material_input.contains("yield_mpa")
            ? 250.0
            : (material_input["yield_mpa"].is_number()
                   ? material_input["yield_mpa"].get<double>()
                   : std::numeric_limits<double>::quiet_NaN());
    if (!std::isfinite(E_input) || !(E_input > 0.0) || !std::isfinite(nu_input) ||
        !(nu_input > -1.0 && nu_input < 0.5) || !std::isfinite(yield_input) ||
        !(yield_input > 0.0))
        return json{{"ok", false},
                    {"error", "fea_static: material E, Poisson ratio, and yield strength "
                              "must be finite and physically valid"}};
    if (!study.contains("fixtures") || !study["fixtures"].is_array() ||
        study["fixtures"].empty())
        return json{{"ok", false},
                    {"error", "fea_static: at least one fixture is required"}};
    for (size_t i = 0; i < study["fixtures"].size(); ++i) {
        const auto& fixture = study["fixtures"][i];
        if (!fixture.is_object())
            return json{{"ok", false},
                        {"error", "fea_static: fixture " + std::to_string(i) +
                                      " must be an object"}};
        std::string faceset_error;
        if (!validate_faceset_input(fixture, faceset_error))
            return json{{"ok", false},
                        {"error", "fea_static: fixture " + std::to_string(i) +
                                      ": " + faceset_error}};
        if (fixture.contains("tol_mm") && !fixture["tol_mm"].is_number())
            return json{{"ok", false},
                        {"error", "fea_static: fixture " + std::to_string(i) +
                                      " has non-numeric tol_mm"}};
        const double tol = fixture.value("tol_mm", 1.0);
        if (!std::isfinite(tol) || !(tol > 0.0))
            return json{{"ok", false},
                        {"error", "fea_static: fixture " + std::to_string(i) +
                                      " has invalid tol_mm"}};
    }
    if (!study.contains("loads") || !study["loads"].is_array() ||
        study["loads"].empty())
        return json{{"ok", false},
                    {"error", "fea_static: at least one non-zero load is required"}};
    bool have_nonzero_load = false;
    for (size_t i = 0; i < study["loads"].size(); ++i) {
        const auto& load = study["loads"][i];
        if (!load.is_object() || !load.contains("faces") ||
            !load["faces"].is_object())
            return json{{"ok", false},
                        {"error", "fea_static: load " + std::to_string(i) +
                                      " requires a faces object"}};
        if (load["faces"].contains("tol_mm") &&
            !load["faces"]["tol_mm"].is_number())
            return json{{"ok", false},
                        {"error", "fea_static: load " + std::to_string(i) +
                                      " has non-numeric tol_mm"}};
        const double tol = load["faces"].value("tol_mm", 1.0);
        std::string faceset_error;
        if (!validate_faceset_input(load["faces"], faceset_error))
            return json{{"ok", false},
                        {"error", "fea_static: load " + std::to_string(i) +
                                      ": " + faceset_error}};
        if (!std::isfinite(tol) || !(tol > 0.0))
            return json{{"ok", false},
                        {"error", "fea_static: load " + std::to_string(i) +
                                      " has invalid tol_mm"}};
        if (!load.contains("type") || !load["type"].is_string())
            return json{{"ok", false},
                        {"error", "fea_static: load " + std::to_string(i) +
                                      " requires a string type"}};
        const std::string type = load.value("type", std::string());
        if (type == "force") {
            if (!load.contains("vector") || !finite3(load["vector"]))
                return json{{"ok", false},
                            {"error", "fea_static: force load " + std::to_string(i) +
                                          " requires a finite 3-vector"}};
            have_nonzero_load = have_nonzero_load || nonzero3(load["vector"]);
        } else if (type == "pressure") {
            if (!load.contains("magnitude") || !load["magnitude"].is_number()) {
                return json{{"ok", false},
                            {"error", "fea_static: pressure load " + std::to_string(i) +
                                          " requires a finite magnitude"}};
            }
            const double magnitude = load["magnitude"].get<double>();
            if (!std::isfinite(magnitude))
                return json{{"ok", false},
                            {"error", "fea_static: pressure load " + std::to_string(i) +
                                          " requires a finite magnitude"}};
            have_nonzero_load = have_nonzero_load || magnitude != 0.0;
        } else {
            return json{{"ok", false},
                        {"error", "fea_static: unsupported load type '" + type + "'"}};
        }
    }
    if (!have_nonzero_load)
        return json{{"ok", false},
                    {"error", "fea_static: at least one non-zero load is required"}};
    // Content-addressed solve-result cache (sim_cache): HIT returns the stored result
    // (O(load)); ANY input change is a MISS. Tag the result with cache_hit so callers
    // / tests can observe the path honestly.
    json cache_study = study;
    cache_study.erase("job_id");
    const std::string input_hash =
        chudware::sim::cache::hash_fea_study(
            chudware::sim::cache::Kind::FeaStatic, cache_study);
    {
        auto hit = chudware::sim::cache::lookup(chudware::sim::cache::Kind::FeaStatic, input_hash);
        // A hit whose promised .vtu evidence file no longer exists re-solves: the
        // file IS the request, so echoing a path the filesystem lost would be a lie.
        if (hit.hit && !detail::vtu_evidence_missing(study)) {
            if (job.cancelled())
                return json{{"ok", false}, {"error", "fea_static: cancelled"}};
            json out = hit.result;
            out["cache_hit"] = true;
            out["cache_reason"] = "hit";
            out["input_hash"] = input_hash;
            if (job.cancelled())
                return json{{"ok", false}, {"error", "fea_static: cancelled"}};
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
    std::optional<MeshAdvisor> mesh_advisor;
    detail::MeshQualityFacts mesh_quality;
    std::string err;
    {
        chudware::prof::phases::PhaseScope _p("mesh");
        if (!resolve_mesh(study, points, tets, nodes_per_elem, mesh_type_used,
                          mesh_note, mesh_advisor, mesh_quality, err))
            return json{{"ok", false}, {"error", err}};
    }
    if (job.cancelled())
        return json{{"ok", false}, {"error", "fea_static: cancelled"}};
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
    // (the GUI/MCP already render {"ok":false,"error"} gracefully). The projected
    // peak comes from governor::fea_peak_bytes — MEASURED peak RSS of this very
    // path, safety-factored — so it is compared to the budget as-is.
    {
        std::string why;
        const size_t n_elems_est = tets.size() / static_cast<size_t>(nodes_per_elem);
        if (!chudware::governor::admit_fea(n_elems_est, nodes_per_elem, 3,
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
            const std::string type = load.value("type", std::string());
            // A load that deposits nothing is a no-op, so it is decided BEFORE the
            // selection is judged: refusing a whole study over a ZERO load's
            // unresolvable faceset reports a fault where there is no load. Upstream
            // validation already guarantees `vector`/`magnitude` exist and are finite
            // here, and `have_nonzero_load` guarantees the study carries a real load.
            // (Witness: a consistent-pressure oracle sums the traction over every
            // element face and relies on interior faces cancelling identically —
            // Zienkiewicz & Taylor §2 — so a coarse bore mesh yields a handful of
            // O(1e-15) forces sitting on INTERIOR mid-side nodes, which
            // boundary_faceset_membership correctly resolves to nothing. Ordering the
            // refusal first killed the whole Lamé study over 32 numerically-zero
            // loads out of 1208.)
            if ((type == "force" && !nonzero3(load["vector"])) ||
                (type == "pressure" && load["magnitude"].get<double>() == 0.0))
                continue;
            std::vector<int> node_ids =
                boundary_faceset_membership(faces, points, tets, nodes_per_elem);
            if (node_ids.empty())
                return json{{"ok", false},
                            {"error", "fea_static: load faces resolved to 0 mesh nodes "
                                      "(tol=" +
                                          std::to_string(tol) + " mm)"}};
            // Consistent boundary-load weights w_i = ∫ N_i dA over the loaded faces.
            // Tet10 uses mapped tri6 quadrature; tet4/hex8 use A/n per corner. Shared by
            // force and pressure deposition.
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
                std::string fold_detail;
                loaded_area = accumulate_face_weights(points, tets, nodes_per_elem,
                                                      node_set, node_w, node_nw,
                                                      fold_detail);
                if (!std::isfinite(loaded_area))
                    return json{{"ok", false},
                                {"error", "fea_static: invalid or folded quadratic "
                                          "boundary face" + fold_detail}};
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
                    // Consistent pressure load: f_i = −p · ∫ N_i n̂ dA, accumulated per node
                    // with the PER-FACE outward normal (node_nw), so a curved loaded surface
                    // gets the right directional load and the resultant spans p·A·(−n̂). The
                    // load lands on the mid-side nodes for tet10 (the prior code lumped p·A
                    // onto corner nodes only, which drove the δ/σ several-fold error).
                    //
                    // SIGN (V&V, 2026-09-05): the minus is the CONTRACT. `pressure` is
                    // documented "MPa, inward" (cad_sim_tools.run_fea_static, cad_guide,
                    // and cad_render_tools, which quotes p·A "N inward"), and a positive
                    // pressure pushing into the surface is the universal engineering
                    // convention. Depositing +p·n̂ made +20 MPa on a Lamé bore read
                    // σ_hoop = −29.6 MPa (external pressurisation) against Lamé's +33.33,
                    // i.e. every bore/vessel study needed a hand-flipped magnitude.
                    for (int nid : node_ids)
                        for (int c = 0; c < 3; ++c)
                            nodal_forces[nid * 3 + c] -=
                                pressure * node_nw[static_cast<size_t>(nid) * 3 + c];
                    notes.push_back("Pressure on loaded area " + std::to_string(loaded_area) +
                                    " mm2 (work-consistent, +p acts inward)");
                } else {
                    return json{{"ok", false},
                                {"error", "fea_static: pressure selection contains no "
                                          "complete exterior boundary face"}};
                }
            }
        }
    }

    // ── Fixtures -> fixed nodes (union, sorted unique) ───────────────────────
    std::vector<int> fixed_nodes;
    // Which fixture a fixed node came from, kept only so a diagnostic can NAME the
    // fixture nearest an excluded stress peak (one entry per fixed node).
    std::unordered_map<int, std::size_t> fixture_of_node;
    std::vector<std::string> fixture_labels;
    if (study.contains("fixtures") && study["fixtures"].is_array()) {
        std::unordered_set<int> seen;
        for (const auto& fx : study["fixtures"]) {
            const double tol = fx.value("tol_mm", 1.0);
            const std::size_t ordinal = fixture_labels.size();
            std::string label = "fixtures[" + std::to_string(ordinal) + "]";
            if (fx.is_object() && fx.contains("sid") && fx["sid"].is_string())
                label = fx["sid"].get<std::string>() + " (" + label + ")";
            fixture_labels.push_back(label);
            std::vector<int> node_ids =
                boundary_faceset_membership(fx, points, tets, nodes_per_elem);
            if (node_ids.empty())
                return json{{"ok", false},
                            {"error", "fea_static: fixture faces resolved to 0 mesh nodes "
                                      "(tol=" +
                                          std::to_string(tol) + " mm)"}};
            for (int nid : node_ids)
                if (seen.insert(nid).second) {
                    fixed_nodes.push_back(nid);
                    fixture_of_node.emplace(nid, ordinal);
                }
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

    // ── Component connectivity vs fixtures: the MECHANISM gate ───────────────
    // An imported assembly meshes as disconnected components, and so does a
    // compound of two solids. Never infer physical contact from proximity alone:
    // only a positive study bond_gap_mm (or the legacy environment override)
    // requests the bonded-contact idealization.
    //
    // Whatever the ties, a component that reaches NO fixture is free to translate
    // and rotate: the stiffness matrix is singular in those six directions and any
    // number the solver prints for it is arbitrary. Live (ME490 front spine tube,
    // O60 x 5.16 mm wall, tet10 at h = 16 mm): max displacement 9.6e6 mm at an
    // equilibrium error of 0.00042% — the residual is tiny precisely BECAUSE the
    // mechanism carries no load, so equilibrium can never catch this. Only
    // connectivity can, and it is checked before the solve rather than explained
    // afterwards.
    // ── study.contact: real unilateral (frictionless) contact ────────────────
    // {"enabled":true, "max_gap_mm":0.5, "max_iterations":25, "penalty_scale":1.0}
    // Contact and bond_gap_mm are two DIFFERENT idealizations of the same interface —
    // one carries tension, one cannot — so asking for both is a contradiction, not a
    // merge, and it is refused rather than silently resolved.
    bool contact_enabled = false;
    ContactCandidateOptions contact_opts;
    FeaContactOptions contact_solver_opts;
    if (study.contains("contact") && !study["contact"].is_null()) {
        const json& c = study["contact"];
        if (!c.is_object())
            return json{{"ok", false},
                        {"error", "fea_static: study.contact must be an object like "
                                  "{\"enabled\":true,\"max_gap_mm\":0.5}"}};
        contact_enabled = c.value("enabled", true);
        if (contact_enabled) {
            if (study.contains("bond_gap_mm"))
                return json{
                    {"ok", false},
                    {"error", "fea_static: study.contact and study.bond_gap_mm are "
                              "mutually exclusive — a bonded tie carries tension across "
                              "the interface and a contact pair cannot. Pick one: "
                              "bond_gap_mm for a welded/glued joint, contact for a "
                              "seated or slip-fit one."}};
            auto number = [&](const char* key, double fallback, double lo,
                              double hi) -> double {
                if (!c.contains(key) || c[key].is_null()) return fallback;
                if (!c[key].is_number()) return std::numeric_limits<double>::quiet_NaN();
                const double v = c[key].get<double>();
                if (!std::isfinite(v) || v < lo || v > hi)
                    return std::numeric_limits<double>::quiet_NaN();
                return v;
            };
            contact_opts.max_gap_mm = number("max_gap_mm", 0.5, 1e-6, 1e4);
            contact_opts.penalty_scale = number("penalty_scale", 1.0, 1e-6, 1e6);
            const double iters = number("max_iterations", 25.0, 1.0, 500.0);
            if (!std::isfinite(contact_opts.max_gap_mm) ||
                !std::isfinite(contact_opts.penalty_scale) || !std::isfinite(iters))
                return json{
                    {"ok", false},
                    {"error", "fea_static: study.contact needs finite max_gap_mm > 0 "
                              "(mm), penalty_scale > 0, and max_iterations in 1..500"}};
            contact_solver_opts.max_iterations = static_cast<int>(iters);
        }
    }
    ContactCandidateSet contact_set;
    std::vector<int> held_by_contact_only;
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
        // ── Nodes no element references are not components of the DOMAIN ────
        // The mesher's hanging-fragment dissolve deliberately leaves such nodes
        // behind: node numbering must stay 1:1 with the polymesh mesh the
        // adaptive route refines through, so only the element list shrinks.
        // fea_static eliminates them exactly (their rows are identically zero,
        // nothing couples them) and still REFUSES a load applied to one. So
        // counting them here would refuse every repaired mesh as a mechanism —
        // one dissolved tet10 orphans up to ten nodes, i.e. up to ten
        // "connected component(s) with no fixture", on a mesh that is one solid
        // body. Bond and contact springs cannot rescue such a node: both are
        // built from the MESH's own boundary faces, so every endpoint is an
        // element node by construction.
        std::vector<char> referenced(static_cast<size_t>(n_nodes_i), 0);
        for (int nid : tets)
            if (nid >= 0 && nid < n_nodes_i)
                referenced[static_cast<size_t>(nid)] = 1;
        int unreferenced_nodes = 0;
        for (int i = 0; i < n_nodes_i; ++i)
            if (!referenced[static_cast<size_t>(i)]) ++unreferenced_nodes;
        std::unordered_set<int> roots;
        for (int i = 0; i < n_nodes_i; ++i)
            if (referenced[static_cast<size_t>(i)]) roots.insert(find(i));
        // The authoritative count for the result: this union-find covers every
        // element stride the solver accepts (hex8 included), where the mesh-level
        // metric from fill_quality is tet-only.
        mesh_quality.connected_components = static_cast<int>(roots.size());
        if (unreferenced_nodes > 0)
            notes.push_back(
                std::to_string(unreferenced_nodes) +
                " node(s) reference no element (left behind by mesh repair, which "
                "preserves node numbering); they are not counted as mesh components "
                "and the solver eliminates their zero rows");
        if (contact_enabled) {
            // Real contact on the meshed bodies: surface nodes of DIFFERENT components
            // that face each other across at most max_gap_mm, normals taken from the
            // MESH's own boundary faces (fea_contact.cpp), each pair remembering its
            // initial signed gap. No pair is a tie: the solver decides per pair whether
            // it is touching.
            if (roots.size() < 2)
                return json{
                    {"ok", false},
                    {"error", "fea_static: study.contact needs at least two disconnected "
                              "mesh components (two bodies). This mesh is one connected "
                              "solid, so there is no interface to open or close — run the "
                              "ordinary static study."},
                    {"notes", notes}};
            contact_set = fea_contact_candidates(points, tets, nodes_per_elem, E_mpa,
                                                 contact_opts);
            if (!contact_set.error.empty())
                return json{{"ok", false},
                            {"error", "fea_static: " + contact_set.error},
                            {"notes", notes}};
            if (contact_set.springs.empty())
                return json{
                    {"ok", false},
                    {"error", "fea_static: no contact pair was found within max_gap_mm = " +
                                  std::to_string(contact_opts.max_gap_mm) +
                                  " mm — the " + std::to_string(roots.size()) +
                                  " bodies in this mesh never come that close, or their "
                                  "facing surfaces are not opposed. Measure the real "
                                  "clearance and raise max_gap_mm to just above it."},
                    {"notes", notes}};
            notes.push_back(
                "unilateral contact: " + std::to_string(contact_set.springs.size()) +
                " candidate pair(s) across " +
                std::to_string(contact_set.group_components.size()) +
                " body pair(s) (gap <= " + std::to_string(contact_opts.max_gap_mm) +
                " mm), frictionless normal penalty, active set solved per iteration");
            if (std::getenv("CHUDWARE_FEA_BOND_GAP_MM"))
                notes.push_back(
                    "CHUDWARE_FEA_BOND_GAP_MM is set but IGNORED: this study requested "
                    "contact, which cannot carry the tension a bonded tie would");
        } else if (roots.size() > 1) {
            double gap_mm = 0.0;
            if (study.contains("bond_gap_mm")) {
                if (!study["bond_gap_mm"].is_number() ||
                    !std::isfinite(study["bond_gap_mm"].get<double>()) ||
                    study["bond_gap_mm"].get<double>() < 0.0)
                    return json{{"ok", false},
                                {"error", "fea_static: bond_gap_mm must be a finite "
                                          "non-negative number"}};
                gap_mm = study["bond_gap_mm"].get<double>();
            } else if (const char* env = std::getenv("CHUDWARE_FEA_BOND_GAP_MM")) {
                char* end = nullptr;
                const double parsed = std::strtod(env, &end);
                if (end == env || *end != '\0' || !std::isfinite(parsed) || parsed < 0.0)
                    return json{{"ok", false},
                                {"error", "fea_static: CHUDWARE_FEA_BOND_GAP_MM must be "
                                          "a finite non-negative number"}};
                gap_mm = parsed;
            }
            if (gap_mm > 0.0) {
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
                const double char_len =
                    (h_n > 0 && h_sum > 0.0) ? (h_sum / h_n) : 1.0;
                // Spatial hash (cell = gap): each node ties to its nearest node in every
                // other component within the requested gap; pairs are deduplicated.
                const double cell = gap_mm;
                auto cell_key = [&](double x, double y, double z) {
                    const long long ix = static_cast<long long>(std::floor(x / cell));
                    const long long iy = static_cast<long long>(std::floor(y / cell));
                    const long long iz = static_cast<long long>(std::floor(z / cell));
                    return (ix * 73856093LL) ^ (iy * 19349663LL) ^
                           (iz * 83492791LL);
                };
                std::unordered_map<long long, std::vector<int>> grid;
                grid.reserve(static_cast<size_t>(n_nodes_i));
                for (int i = 0; i < n_nodes_i; ++i)
                    grid[cell_key(points[i * 3], points[i * 3 + 1],
                                  points[i * 3 + 2])]
                        .push_back(i);
                std::set<std::pair<int, int>> tied;
                for (int i = 0; i < n_nodes_i; ++i) {
                    const int ci = find(i);
                    const double xi = points[i * 3], yi = points[i * 3 + 1],
                                 zi = points[i * 3 + 2];
                    std::unordered_map<int, std::pair<double, int>> best_per_root;
                    for (int ox = -1; ox <= 1; ++ox)
                        for (int oy = -1; oy <= 1; ++oy)
                            for (int oz = -1; oz <= 1; ++oz) {
                                const auto it = grid.find(cell_key(
                                    xi + ox * cell, yi + oy * cell, zi + oz * cell));
                                if (it == grid.end()) continue;
                                for (int j : it->second) {
                                    const int cj = find(j);
                                    if (cj == ci) continue;
                                    const double dx2 = points[j * 3] - xi;
                                    const double dy2 = points[j * 3 + 1] - yi;
                                    const double dz2 = points[j * 3 + 2] - zi;
                                    const double d2 =
                                        dx2 * dx2 + dy2 * dy2 + dz2 * dz2;
                                    if (d2 > gap_mm * gap_mm) continue;
                                    auto bit = best_per_root.find(cj);
                                    if (bit == best_per_root.end() ||
                                        d2 < bit->second.first)
                                        best_per_root[cj] = {d2, j};
                                }
                            }
                    for (const auto& kv : best_per_root) {
                        const int best = kv.second.second;
                        if (tied.emplace(std::min(i, best), std::max(i, best)).second)
                            bond_springs.push_back(
                                BondSpring{i, best, E_mpa * char_len});
                    }
                }
                notes.push_back(
                    "assembly mesh has " + std::to_string(roots.size()) +
                    " disconnected component(s): " +
                    std::to_string(bond_springs.size()) +
                    " explicit bonded gap tie(s) added (gap <= " +
                    std::to_string(gap_mm) + " mm)");
            } else {
                notes.push_back(
                    "assembly mesh has " + std::to_string(roots.size()) +
                    " disconnected component(s); no bonded contact was requested");
            }
        }

        // Every component must have an explicit stiffness path to a fixture. The gap
        // ties above are the only contact idealization this study requested; never
        // invent longer bridges or ground a floating part, because both change the
        // model while presenting the result as a physical solve.
        std::vector<int> super(comp);
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
            if (a != b)
                super[static_cast<size_t>(std::max(a, b))] = std::min(a, b);
        }
        // A component held ONLY by contact candidates is REACHABLE for this gate — the
        // active-set solve is what decides whether those pairs are touching, and it
        // refuses on its own if they all separate. But such a component is held by an
        // idealization the reader must see, so it is named in the packet.
        {
            std::unordered_set<int> bond_anchored;
            for (int nid : fixed_nodes) bond_anchored.insert(sfind(nid));
            std::vector<int> pre_contact_root(static_cast<size_t>(n_nodes_i));
            for (int i = 0; i < n_nodes_i; ++i) pre_contact_root[static_cast<size_t>(i)] = sfind(i);
            for (const ContactSpring& s : contact_set.springs) {
                const int a = sfind(s.a), b = sfind(s.b);
                if (a != b)
                    super[static_cast<size_t>(std::max(a, b))] = std::min(a, b);
            }
            if (!contact_set.springs.empty()) {
                std::unordered_set<int> now_anchored;
                for (int nid : fixed_nodes) now_anchored.insert(sfind(nid));
                std::set<int> only;
                for (int i = 0; i < n_nodes_i; ++i) {
                    const int comp_id = i < static_cast<int>(contact_set.component.size())
                                            ? contact_set.component[static_cast<size_t>(i)]
                                            : -1;
                    if (comp_id < 0) continue;
                    if (bond_anchored.count(pre_contact_root[static_cast<size_t>(i)])) continue;
                    if (now_anchored.count(sfind(i))) only.insert(comp_id);
                }
                held_by_contact_only.assign(only.begin(), only.end());
            }
        }
        std::unordered_set<int> anchored;
        for (int nid : fixed_nodes) anchored.insert(sfind(nid));
        // Same `referenced` mask as the component count above: a node no element
        // references is eliminated by fea_static (zero rows, never a rigid mode),
        // so it is not a floating component either.
        std::unordered_set<int> floating;
        for (int i = 0; i < n_nodes_i; ++i) {
            if (!referenced[static_cast<size_t>(i)]) continue;
            const int root = sfind(i);
            if (!anchored.count(root)) floating.insert(root);
        }
        mesh_quality.unfixed_components = static_cast<int>(floating.size());
        if (!floating.empty()) {
            std::string message =
                "fea_static: mesh has " + std::to_string(floating.size()) +
                " connected component(s) with no fixture: rigid-body motion — "
                "refine the mesh (h <= wall thickness) or fix the island";
            if (!bond_springs.empty())
                message += ". " + std::to_string(bond_springs.size()) +
                           " explicit bonded gap tie(s) were added and still did not "
                           "reach a fixtured component; raise bond_gap_mm only when that "
                           "contact is physically intended";
            if (!contact_set.springs.empty())
                message += ". " + std::to_string(contact_set.springs.size()) +
                           " contact candidate pair(s) were found and still did not "
                           "reach a fixtured component; raise contact.max_gap_mm only "
                           "when that interface really is that close";
            return json{
                {"ok", false},
                {"error", message},
                {"mesh_quality",
                 detail::mesh_quality_json(
                     mesh_quality, points.size() / 3,
                     tets.size() / static_cast<std::size_t>(nodes_per_elem),
                     nodes_per_elem)},
                {"notes", notes}};
        }
    }
    // ── A hanging FRAGMENT is a REFUSAL, in the same class as an unfixtured
    // component. `lonely_nodes_remaining` counts the nodes of element fragments —
    // elements glued to each other face-to-face — that touch the rest of their own
    // body at two corners or fewer: such a fragment rotates about that node or edge
    // at zero strain energy, so the stiffness is singular while every connectivity
    // check passes (the fragment does reach a fixture, through the one node it hangs
    // from). It is deliberately NOT the raw count of nodes used by a single element
    // (`single_element_nodes`) — a tet apex nobody else uses is fully constrained by
    // its own element, and 45 of the 51 such tips on the measured wheel mesh were
    // ordinary. That mesh: condition number 4e33 and a solve that returned a
    // plausible-looking 0.032 mm with a zero equilibrium residual. The mesher
    // dissolves these (mesh_quality.hanging_elems_dropped); anything left here is
    // not solvable, and a warning would only be as good as someone reading it.
    if (mesh_quality.lonely_nodes_remaining > 0) {
        // Say which mesh the caller has to act on from what they SENT, not from
        // whether the mesher happened to repair anything: a geometry mesh that
        // needed no repair also reports zero drops, and calling it "inline" pointed
        // the caller at arrays they never supplied.
        const bool inline_mesh = study.contains("points") &&
                                 study["points"].is_array() &&
                                 study.contains("tets") && study["tets"].is_array();
        std::string message =
            "fea_static: mesh has " +
            std::to_string(mesh_quality.lonely_nodes_remaining) +
            " node(s) on element fragment(s) that touch the rest of the body at two "
            "corners or fewer: such a fragment can rotate about that node or edge at "
            "zero strain energy, so the stiffness matrix is singular and any "
            "displacement or FoS from it is meaningless — including one that looks "
            "plausible and reports a zero equilibrium residual.";
        message += inline_mesh
                       ? " This is an inline mesh, which is never edited here: remove "
                         "the hanging elements from points/tets, or mesh from geometry "
                         "and let the mesher dissolve them."
                       : " The mesher dissolved " +
                             std::to_string(mesh_quality.hanging_elems_dropped) +
                             " hanging element(s) and these survived, which points at "
                             "the geometry rather than the size: repair the sliver "
                             "feature, or re-mesh with a size band that does not have "
                             "to straddle it.";
        return json{
            {"ok", false},
            {"error", message},
            {"mesh_quality",
             detail::mesh_quality_json(
                 mesh_quality, points.size() / 3,
                 tets.size() / static_cast<std::size_t>(nodes_per_elem),
                 nodes_per_elem)},
            {"notes", notes}};
    }

    const auto t_solve0 = std::chrono::steady_clock::now();
    chudware::prof::phases::add("setup", std::chrono::duration<double, std::milli>(
                                          t_solve0 - t_setup0).count());
    if (job.cancelled())
        return json{{"ok", false}, {"error", "fea_static: cancelled"}};
    // ── study.export_system_path: the resolved system, verbatim (V&V hook) ───
    // Everything an independent solver needs to rebuild THIS system node-for-node,
    // written immediately before the solve so it can never describe a different one.
    // Lengths mm, forces N, modulus MPa, spring stiffness N/mm. fixed_nodes is a flat
    // node-index list because that is the solver's only Dirichlet form: each entry pins
    // all three translational DOFs to zero. A write failure fails the RUN — a V&V export
    // that silently did not happen is worse than no export.
    if (study.contains("export_system_path") && !study["export_system_path"].is_null()) {
        if (!study["export_system_path"].is_string() ||
            study["export_system_path"].get<std::string>().empty())
            return json{{"ok", false},
                        {"error", "fea_static: export_system_path must be a non-empty "
                                  "string path"},
                        {"notes", notes}};
        const std::string path = study["export_system_path"].get<std::string>();
        const int n_nodes_out = static_cast<int>(points.size() / 3);
        json dump;
        dump["nodes_per_elem"] = nodes_per_elem;
        dump["E_mpa"] = E_mpa;
        dump["nu"] = nu;
        json pts = json::array();
        json forces = json::array();
        for (int i = 0; i < n_nodes_out; ++i) {
            pts.push_back(json::array({points[static_cast<size_t>(i) * 3],
                                       points[static_cast<size_t>(i) * 3 + 1],
                                       points[static_cast<size_t>(i) * 3 + 2]}));
            forces.push_back(json::array({nodal_forces[static_cast<size_t>(i) * 3],
                                          nodal_forces[static_cast<size_t>(i) * 3 + 1],
                                          nodal_forces[static_cast<size_t>(i) * 3 + 2]}));
        }
        dump["points"] = std::move(pts);
        dump["nodal_forces"] = std::move(forces);
        json elems = json::array();
        for (size_t e = 0; e + nodes_per_elem <= tets.size();
             e += static_cast<size_t>(nodes_per_elem)) {
            json row = json::array();
            for (int j = 0; j < nodes_per_elem; ++j) row.push_back(tets[e + j]);
            elems.push_back(std::move(row));
        }
        dump["elems"] = std::move(elems);
        dump["fixed_nodes"] = fixed_nodes;
        json bonds = json::array();
        for (const BondSpring& s : bond_springs)
            bonds.push_back(json{{"a", s.a}, {"b", s.b}, {"k", s.k}});
        dump["bond_springs"] = std::move(bonds);
        json contacts = json::array();
        for (const ContactSpring& s : contact_set.springs)
            contacts.push_back(json{{"a", s.a},
                                    {"b", s.b},
                                    {"k", s.k},
                                    {"gap0_mm", s.gap0_mm},
                                    {"normal", s.normal},
                                    {"area_mm2", s.area_mm2},
                                    {"group", s.group}});
        dump["contact_springs"] = std::move(contacts);
        std::error_code ec;
        const std::filesystem::path fs_path(path);
        if (fs_path.has_parent_path())
            std::filesystem::create_directories(fs_path.parent_path(), ec);
        std::ofstream out_file(fs_path, std::ios::binary | std::ios::trunc);
        if (!out_file) {
            return json{{"ok", false},
                        {"error", "fea_static: could not open export_system_path for "
                                  "writing: " + path},
                        {"notes", notes}};
        }
        out_file << dump.dump();
        out_file.flush();
        if (!out_file) {
            return json{{"ok", false},
                        {"error", "fea_static: failed while writing "
                                  "export_system_path: " + path},
                        {"notes", notes}};
        }
        notes.push_back("resolved system exported to " + path +
                        " (mm / N / MPa; fixed_nodes pins all 3 DOFs per entry)");
    }
    FeaContactReport contact_report;
    FeaStaticResult r =
        contact_enabled
            ? fea_static_solve_contact(points, tets, E_mpa, nu, yield_mpa, fixed_nodes,
                                       nodal_forces, nodes_per_elem, bond_springs,
                                       contact_set.springs, contact_solver_opts,
                                       contact_report, job.token())
            : fea_static_solve(points, tets, E_mpa, nu, yield_mpa, fixed_nodes,
                               nodal_forces, nodes_per_elem, bond_springs, job.token());
    // The contact block is EVIDENCE, so it rides on the refusal too: a reader has to be
    // able to see how many pairs there were and how far the active set got.
    auto contact_json = [&]() {
        json body = {{"enabled", true},
                     {"max_gap_mm", contact_opts.max_gap_mm},
                     {"penalty_scale", contact_opts.penalty_scale},
                     {"penalty_stiffness_n_per_mm",
                      round_nd(E_mpa * contact_set.char_len_mm * contact_opts.penalty_scale, 4)},
                     {"max_iterations", contact_solver_opts.max_iterations},
                     {"candidate_pairs", contact_report.candidate_pairs},
                     {"active_pairs", contact_report.active_pairs},
                     {"contact_iterations", contact_report.contact_iterations},
                     {"converged", contact_report.converged},
                     {"tangential_stabilization", contact_report.tangential_stabilization},
                     {"tangential_force_fraction",
                      round_nd(contact_report.tangential_force_fraction, 6)},
                     {"held_by_contact_only", held_by_contact_only},
                     {"pairs", json::array()}};
        for (const FeaContactGroup& g : contact_report.groups) {
            const std::array<int, 2> comps =
                g.group >= 0 && g.group < static_cast<int>(contact_set.group_components.size())
                    ? contact_set.group_components[static_cast<size_t>(g.group)]
                    : std::array<int, 2>{-1, -1};
            body["pairs"].push_back(
                json{{"bodies", comps},
                     {"candidate_pairs", g.candidate_pairs},
                     {"active_pairs", g.active_pairs},
                     {"contact_force_n", round_nd(g.contact_force_n, 4)},
                     {"force_vector_n",
                      json::array({round_nd(g.force_n[0], 4), round_nd(g.force_n[1], 4),
                                   round_nd(g.force_n[2], 4)})},
                     {"max_contact_pressure_mpa", round_nd(g.max_contact_pressure_mpa, 4)},
                     {"max_penetration_mm", round_nd(g.max_penetration_mm, 6)}});
        }
        return body;
    };
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
                   " explicit bonded gap ties were added, but a loaded part may still not "
                   "reach any fixtured part. Increase bond_gap_mm only when the larger "
                   "bonded-contact region is physically intended.";
        json fail{{"ok", false}, {"error", msg}, {"notes", notes}};
        if (contact_enabled) fail["contact"] = contact_json();
        return fail;
    }
    // A non-converged active set is not a contact solution: the displacements belong to
    // whichever set the loop happened to stop on, so the stresses are not the ones the
    // touching state implies. Report the evidence, refuse the verdict.
    if (contact_enabled && !contact_report.converged) {
        return json{
            {"ok", false},
            {"error", "fea_static: the contact active set did not settle in " +
                          std::to_string(contact_solver_opts.max_iterations) +
                          " iteration(s) — pairs kept opening and closing, so no state "
                          "is the solution. Raise contact.max_iterations, or reduce "
                          "contact.penalty_scale (a very stiff penalty makes pairs "
                          "chatter across the gap)."},
            {"contact", contact_json()},
            {"notes", notes}};
    }
    if (nodes_per_elem == 10) notes.push_back("second-order (tet10) elements");
    // Which factorization actually ran, and whether this solve queued behind another
    // one — both come from the kernel, so a packet never has to be guessed at from
    // build flags or wall time.
    if (!r.solver.empty()) notes.push_back("solver: " + r.solver);
    if (r.solve_wait_ms > 0.0)
        notes.push_back("resources: this solve waited " +
                        std::to_string(static_cast<long long>(r.solve_wait_ms)) +
                        " ms for the host's solve slot (another heavy solve held it)");
    // A conditioning verdict outranks everything else in the note list: the numbers
    // below it may be meaningless. It goes in FIRST so a reader cannot scroll past it.
    if (!r.conditioning_warning.empty())
        notes.insert(notes.begin(), "CONDITIONING WARNING: " + r.conditioning_warning);
    // The note a reader sees first has to carry the two numbers, not just the
    // policy: a note saying "excludes the fixture rings" did not stop a released
    // packet from quoting the masked peak as the peak AT the fixture holes.
    if (!fixed_nodes.empty())
        notes.push_back(
            "von Mises/FoS summary excludes the " +
            std::to_string(r.summary_excluded_nodes) + " of " +
            std::to_string(r.n_nodes) +
            " nodes inside the two element rings adjacent to ideal fixed "
            "constraints (summary.max_von_mises_reduction = "
            "node_averaged_excluding_fixture_ring2): reported peak " +
            std::to_string(r.max_von_mises_mpa) + " MPa, unmasked nodal peak " +
            std::to_string(r.max_von_mises_raw_mpa) +
            " MPa (summary.max_von_mises_raw_mpa). The full nodal field and the "
            "summary_node mask that selects it ride in the result and in any .vtu");
    // A ratio this large is no longer a boundary-layer detail: the number the packet
    // reports and the number the field shows are different design conclusions, so it
    // gets plain words, a location, and the fixture's own name. The threshold is a
    // disclosure trigger, not a verdict — nothing is refused or clipped by it.
    std::string excluded_peak_warning;
    if (!fixed_nodes.empty() && r.max_von_mises_mpa > 0.0 &&
        r.max_von_mises_raw_node >= 0 &&
        r.max_von_mises_raw_mpa > 1.25 * r.max_von_mises_mpa) {
        const std::size_t peak = static_cast<std::size_t>(r.max_von_mises_raw_node);
        double best_d2 = -1.0;
        int nearest = -1;
        for (int nid : fixed_nodes) {
            const std::size_t f = static_cast<std::size_t>(nid);
            if (3 * f + 2 >= points.size() || 3 * peak + 2 >= points.size()) continue;
            double d2 = 0.0;
            for (int c = 0; c < 3; ++c) {
                const double d = points[3 * peak + static_cast<std::size_t>(c)] -
                                 points[3 * f + static_cast<std::size_t>(c)];
                d2 += d * d;
            }
            if (nearest < 0 || d2 < best_d2) { best_d2 = d2; nearest = nid; }
        }
        const auto owner = nearest >= 0 ? fixture_of_node.find(nearest)
                                        : fixture_of_node.end();
        const std::string fixture_name =
            owner != fixture_of_node.end() && owner->second < fixture_labels.size()
                ? fixture_labels[owner->second]
                : std::string("an ideal fixed constraint");
        char at[96] = {0};
        std::snprintf(at, sizeof(at), "(%.4g, %.4g, %.4g) mm",
                      points[3 * peak], points[3 * peak + 1], points[3 * peak + 2]);
        excluded_peak_warning =
            "peak excluded near fixtures: the highest nodal von Mises in this solve is " +
            std::to_string(r.max_von_mises_raw_mpa) + " MPa at node " +
            std::to_string(r.max_von_mises_raw_node) + " " + at + ", " +
            (nearest >= 0 ? std::to_string(std::sqrt(std::max(best_d2, 0.0))) : std::string("?")) +
            " mm from fixture " + fixture_name +
            " (fixtures[i] is the 0-based position in the study's fixtures list, i.e. "
            "the face query you passed at that position), i.e. " +
            std::to_string(r.max_von_mises_raw_mpa / r.max_von_mises_mpa) +
            "x the reported summary.max_von_mises_mpa of " +
            std::to_string(r.max_von_mises_mpa) +
            " MPa. The reported peak SKIPS that node (two element rings around ideal "
            "fixtures are a clamp singularity, not a material-comparable value). If the "
            "fixture face IS the feature you are designing — a bolt hole, a bearing seat, "
            "a keyway — then the peak you care about is the excluded one: use "
            "summary.max_von_mises_raw_mpa (" +
            std::to_string(r.max_von_mises_raw_mpa) + " MPa) and summary.min_fos_raw (" +
            std::to_string(r.min_fos_raw) +
            "), state that it is mesh-sensitive, and do not quote summary.min_fos as the "
            "margin at the fixture.";
        notes.push_back(excluded_peak_warning);
    }
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
                // The per-node 1/0 selector the summary reduction used, shipped
                // beside the field it selects (and written into the .vtu) so the
                // reported peak is reproducible from the arrays in this reply.
                {"summary_node", r.summary_node},
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
                  {"max_abs_principal_mpa", round_nd(max_abs_principal, 4)},
                  // ── What these scalars are a reduction of (T-EV: the 3.7x gap) ──
                  // max_von_mises_mpa and the FoS/principal/shear extrema beside it
                  // are taken over the nodes OUTSIDE two element rings of an ideal
                  // fixed constraint (a clamp singularity is mesh-dependent, not a
                  // material-comparable design value). That is a decision, and a
                  // reader holding the nodal field — or the .vtu — sees a higher
                  // peak with no way to know why, so the decision is stated, the
                  // unmasked reduction is reported beside it, and the per-key lists
                  // say exactly which scalars in THIS object share the mask (the
                  // Tresca/Rankine pair is unmasked, and sits right next to the
                  // masked von Mises number).
                  {"max_von_mises_reduction",
                   r.summary_excluded_nodes > 0
                       ? "node_averaged_excluding_fixture_ring2"
                       : "node_averaged"},
                  {"max_von_mises_raw_mpa", round_nd(r.max_von_mises_raw_mpa, 4)},
                  {"min_fos_raw", round_nd(r.min_fos_raw, 4)},
                  {"summary_nodes_total", r.n_nodes},
                  {"summary_nodes_excluded", r.summary_excluded_nodes},
                  {"reduction_masked_keys",
                   json::array({"max_von_mises_mpa", "min_fos", "max_principal_mpa",
                                "min_principal_mpa", "max_shear_mpa"})},
                  {"reduction_unmasked_keys",
                   json::array({"max_disp_mm", "max_von_mises_raw_mpa", "min_fos_raw",
                                "max_tresca_mpa", "max_abs_principal_mpa",
                                "min_fos_tresca", "min_fos_rankine"})}}},
                {"solver_notes", notes}};
    if (mesh_advisor) out["advisor"] = detail::mesh_advisor_json(*mesh_advisor);
    // Contact rides BESIDE summary/reactions/mesh_quality, like every other evidence
    // block: the equilibrium check and the contact resultants have to be read together.
    if (contact_enabled) out["contact"] = contact_json();
    out["mesh_quality"] = detail::mesh_quality_json(
        mesh_quality, static_cast<std::size_t>(r.n_nodes),
        static_cast<std::size_t>(r.n_elems), nodes_per_elem);
    out["reactions"] = json{{"applied_force_n", r.applied_force_n},
                            {"reaction_force_n", r.reaction_force_n},
                            {"applied_moment_nmm", r.applied_moment_nmm},
                            {"reaction_moment_nmm", r.reaction_moment_nmm},
                            {"equilibrium_error_pct",
                             round_nd(r.equilibrium_error_pct, 6)}};
    {
        // provenance.solver names the route AND the factorization that produced it.
        const std::string solver_route =
            r.solver.empty() ? std::string("chudware/fea_static")
                             : "chudware/fea_static [" + r.solver + "]";
        out["provenance"] = detail::provenance_json(solver_route.c_str());
    }

    // ── Result plausibility: is this displacement even a small strain? ───────
    // Linear-elastic FEA assumes displacements small against the part. A peak
    // displacement past the whole bounding-box diagonal has left that regime, so
    // the stiffness matrix it was computed with no longer describes the deformed
    // body — the number is not a conservative estimate, it is meaningless. This
    // does NOT refuse (a real 1.2x-diagonal deflection is worth diagnosing, and
    // the fields are wanted for it), but it is stated where a reader cannot miss
    // it, and the convergence ladder refuses to count such a leg.
    {
        double lo[3] = {0.0, 0.0, 0.0}, hi[3] = {0.0, 0.0, 0.0};
        bool seeded = false;
        for (std::size_t i = 0; i + 2 < points.size(); i += 3) {
            if (!std::isfinite(points[i]) || !std::isfinite(points[i + 1]) ||
                !std::isfinite(points[i + 2]))
                continue;
            for (int c = 0; c < 3; ++c) {
                const double v = points[i + static_cast<std::size_t>(c)];
                lo[c] = seeded ? std::min(lo[c], v) : v;
                hi[c] = seeded ? std::max(hi[c], v) : v;
            }
            seeded = true;
        }
        double diag2 = 0.0;
        for (int c = 0; c < 3; ++c) diag2 += (hi[c] - lo[c]) * (hi[c] - lo[c]);
        const double bbox_diag = seeded ? std::sqrt(diag2) : 0.0;
        const bool measurable = bbox_diag > 0.0 && std::isfinite(r.max_disp_mm);
        const double ratio = measurable ? r.max_disp_mm / bbox_diag : 0.0;
        const bool implausible = measurable && ratio > 1.0;
        out["plausibility"] =
            json{{"max_disp_over_bbox_diag",
                  measurable ? json(round_nd(ratio, 6)) : json(nullptr)},
                 {"bbox_diag_mm", measurable ? json(round_nd(bbox_diag, 6))
                                             : json(nullptr)},
                 {"flag", implausible ? "implausible" : "ok"}};
        if (implausible) {
            out["warnings"] = json::array(
                {"displacement_implausible: max displacement " +
                 std::to_string(r.max_disp_mm) + " mm is " +
                 std::to_string(ratio) +
                 "x the model bounding-box diagonal (" +
                 std::to_string(bbox_diag) +
                 " mm), so the linear-elastic small-strain assumption this solve "
                 "is built on is broken and the result is not a deflection. The "
                 "usual cause is a mechanism in the mesh — a thin wall meshed one "
                 "element thick, or a region held by too few fixture nodes. Refine "
                 "(h <= wall thickness), check mesh_quality.thin_wall_ratio, and "
                 "confirm the fixture faces resolved."});
        }
    }
    // Same text as the solver note, but in `warnings` too: an analyst reading a packet
    // scans warnings, and the whole failure mode here was a disclosure nobody read.
    if (!excluded_peak_warning.empty()) {
        if (!out.contains("warnings") || !out["warnings"].is_array())
            out["warnings"] = json::array();
        out["warnings"].push_back(excluded_peak_warning);
    }
    {
        std::string vtu_path, vtu_error;
        if (!detail::write_result_vtu(study, points, tets, nodes_per_elem, out,
                                      vtu_path, vtu_error))
            return json{{"ok", false},
                        {"error", "fea_static: " + vtu_error},
                        {"solver_notes", notes}};
        if (!vtu_path.empty()) out["vtu_path"] = vtu_path;
    }
    if (job.cancelled())
        return json{{"ok", false}, {"error", "fea_static: cancelled"}};
    chudware::sim::cache::store(chudware::sim::cache::Kind::FeaStatic, input_hash, out);
    out["cache_hit"] = false;
    out["cache_reason"] = "miss";
    out["input_hash"] = input_hash;
    return out;
}

nlohmann::json fea_run_static(const nlohmann::json& study) {
    try {
        return fea_run_static_impl(study, {});
    } catch (const nlohmann::json::exception& e) {
        return json{{"ok", false},
                    {"error", std::string("fea_static: invalid study JSON: ") +
                                  e.what()}};
    }
}

namespace detail {
nlohmann::json fea_run_static_with_cancel_token(
    const nlohmann::json& study, const SimCancelToken& token) {
    try {
        return fea_run_static_impl(study, token);
    } catch (const nlohmann::json::exception& e) {
        return json{{"ok", false},
                    {"error", std::string("fea_static: invalid study JSON: ") +
                                  e.what()}};
    }
}
}  // namespace detail

nlohmann::json fea_run_modal(const nlohmann::json& study, int n_modes) {
    try {
        SimJobScope job(study);
    if (!study.is_object())
        return json{{"ok", false}, {"error", "fea_modal: study must be an object"}};
    if (!has_mesh_source(study))
        return json{{"ok", false},
                    {"error", "fea_modal: a non-empty STEP path or inline mesh is required"}};
    if (n_modes <= 0)
        return json{{"ok", false},
                    {"error", "fea_modal: n_modes must be positive"}};
    const json material_input = study.value("material", json::object());
    if (!material_input.is_object() || !material_input.contains("E_mpa") ||
        !material_input["E_mpa"].is_number() || !material_input.contains("nu") ||
        !material_input["nu"].is_number() ||
        !material_input.contains("density_g_cm3") ||
        !material_input["density_g_cm3"].is_number())
        return json{{"ok", false},
                    {"error", "fea_modal: material requires numeric E_mpa, nu, and "
                              "density_g_cm3"}};
    const double E_input = material_input["E_mpa"].get<double>();
    const double nu_input = material_input["nu"].get<double>();
    const double rho_input = material_input["density_g_cm3"].get<double>();
    if (!std::isfinite(E_input) || !(E_input > 0.0) ||
        !std::isfinite(nu_input) || !(nu_input > -1.0 && nu_input < 0.5) ||
        !std::isfinite(rho_input) || !(rho_input > 0.0))
        return json{{"ok", false},
                    {"error", "fea_modal: material values must be finite and physically "
                              "valid"}};
    if (study.contains("fixtures") && !study["fixtures"].is_array())
        return json{{"ok", false},
                    {"error", "fea_modal: fixtures must be an array"}};
    if (study.contains("fixtures")) {
        for (size_t i = 0; i < study["fixtures"].size(); ++i) {
            const auto& fixture = study["fixtures"][i];
            if (!fixture.is_object())
                return json{{"ok", false},
                            {"error", "fea_modal: fixture " + std::to_string(i) +
                                          " must be an object"}};
            std::string faceset_error;
            if (!validate_faceset_input(fixture, faceset_error))
                return json{{"ok", false},
                            {"error", "fea_modal: fixture " +
                                          std::to_string(i) + ": " +
                                          faceset_error}};
            if (fixture.contains("tol_mm") &&
                !fixture["tol_mm"].is_number())
                return json{{"ok", false},
                            {"error", "fea_modal: fixture " + std::to_string(i) +
                                          " has non-numeric tol_mm"}};
            const double tol = fixture.value("tol_mm", 1.0);
            if (!std::isfinite(tol) || !(tol > 0.0))
                return json{{"ok", false},
                            {"error", "fea_modal: fixture " + std::to_string(i) +
                                          " has invalid tol_mm"}};
        }
    }
    if (job.cancelled())
        return json{{"ok", false}, {"error", "fea_modal: cancelled"}};
    // Cache key folds n_modes into the study inputs so a different mode count is a MISS.
    json modal_key = study.is_object() ? study : json::object();
    modal_key["n_modes"] = n_modes;
    modal_key.erase("job_id");
    const std::string input_hash =
        chudware::sim::cache::hash_fea_study(chudware::sim::cache::Kind::FeaModal, modal_key);
    {
        auto hit = chudware::sim::cache::lookup(chudware::sim::cache::Kind::FeaModal, input_hash);
        // See fea_run_static: a promised .vtu that no longer exists is a miss.
        if (hit.hit && !detail::vtu_evidence_missing(study)) {
            if (job.cancelled())
                return json{{"ok", false}, {"error", "fea_modal: cancelled"}};
            json out = hit.result;
            out["cache_hit"] = true;
            out["cache_reason"] = "hit";
            out["input_hash"] = input_hash;
            if (job.cancelled())
                return json{{"ok", false}, {"error", "fea_modal: cancelled"}};
            return out;
        }
    }
    chudware::prof::phases::StudyScope _study("fea_modal");  // per-phase record (prof/phases.h)
    std::vector<double> points;
    std::vector<int> tets;
    int nodes_per_elem = 4;
    std::string mesh_type_used, mesh_note;
    std::optional<MeshAdvisor> mesh_advisor;
    detail::MeshQualityFacts mesh_quality;
    std::string err;
    std::vector<std::string> notes;
    // STEP modal meshes default to tet10 for bending accuracy. Inline meshes keep
    // their declared nodes_per_elem; mesh_order must never reinterpret caller-owned
    // connectivity.
    json modal_study = study;
    const bool has_step = study.contains("step_path") &&
                          study["step_path"].is_string() &&
                          !study["step_path"].get<std::string>().empty();
    if (has_step) modal_study["mesh_order"] = study.value("mesh_order", 2);
    {
        chudware::prof::phases::PhaseScope _p("mesh");
        if (!resolve_mesh(modal_study, points, tets, nodes_per_elem,
                          mesh_type_used, mesh_note, mesh_advisor, mesh_quality, err))
            return json{{"ok", false}, {"error", err}};
    }
    if (job.cancelled())
        return json{{"ok", false}, {"error", "fea_modal: cancelled"}};
    notes.push_back(nodes_per_elem == 10 ? "modal uses 2nd-order (tet10) elements"
                                         : "modal uses 1st-order (tet4) elements");
    if (!mesh_note.empty()) notes.push_back("mesh: " + mesh_note);

    // Governor admission: 2x the calibrated static peak (K + M plus eigensolver
    // workspace), so this keeps admit() with an explicit estimate.
    {
        std::string why;
        const size_t ne = tets.size() / static_cast<size_t>(nodes_per_elem);
        if (!chudware::governor::admit(
                2 * chudware::governor::fea_peak_bytes(ne, nodes_per_elem, 3),
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
        for (size_t i = 0; i < study["fixtures"].size(); ++i) {
            const auto& fx = study["fixtures"][i];
            const std::vector<int> ids =
                boundary_faceset_membership(fx, points, tets, nodes_per_elem);
            if (ids.empty())
                return json{{"ok", false},
                            {"error", "fea_modal: fixture " + std::to_string(i) +
                                          " resolved to 0 mesh nodes"}};
            for (int nid : ids)
                if (seen.insert(nid).second) fixed_nodes.push_back(nid);
        }
    }
    std::sort(fixed_nodes.begin(), fixed_nodes.end());

    if (job.cancelled())
        return json{{"ok", false}, {"error", "fea_modal: cancelled"}};
    FeaModalResult r = [&] {
        chudware::prof::phases::PhaseScope _p("solve");
        return fea_modal_solve(points, tets, E_mpa, nu, density_g_cm3,
                               fixed_nodes, n_modes, nodes_per_elem, job.token());
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
    if (mesh_advisor) out["advisor"] = detail::mesh_advisor_json(*mesh_advisor);
    out["mesh_quality"] = detail::mesh_quality_json(
        mesh_quality, points.size() / 3,
        tets.size() / static_cast<std::size_t>(nodes_per_elem), nodes_per_elem);
    out["provenance"] = detail::provenance_json("chudware/fea_modal");
    {
        std::string vtu_path, vtu_error;
        if (!detail::write_result_vtu(study, points, tets, nodes_per_elem, out,
                                      vtu_path, vtu_error))
            return json{{"ok", false},
                        {"error", "fea_modal: " + vtu_error},
                        {"solver_notes", notes}};
        if (!vtu_path.empty()) out["vtu_path"] = vtu_path;
    }
    if (job.cancelled())
        return json{{"ok", false}, {"error", "fea_modal: cancelled"}};
    chudware::sim::cache::store(chudware::sim::cache::Kind::FeaModal, input_hash, out);
    out["cache_hit"] = false;
    out["cache_reason"] = "miss";
    out["input_hash"] = input_hash;
    return out;
    } catch (const nlohmann::json::exception& e) {
        return json{{"ok", false},
                    {"error", std::string("fea_modal: invalid study JSON: ") +
                                  e.what()}};
    }
}

namespace {

// Resolve a faceset object ({points,tol_mm[,face_normal,perp_tol_mm]}) to mesh node indices,
// completing tet10 mid-side nodes so surface loads/BCs land on the nodes that carry the
// consistent weights (identical treatment to fea_run_static's load loop). Returns sorted
// unique node ids; empty when the faceset resolves to nothing.
std::vector<int> resolve_face_nodes(const json& faces, const std::vector<double>& points,
                                    const std::vector<int>& tets, int nodes_per_elem) {
    std::vector<int> ids =
        boundary_faceset_membership(faces, points, tets, nodes_per_elem);
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
    if (js.is_null()) return true;  // no schedule = constant
    if (!js.is_object()) {
        err = "schedule must be an object";
        return false;
    }
    if (js.contains("duty")) {
        if (!js["duty"].is_number() ||
            (js.contains("period_s") && !js["period_s"].is_number())) {
            err = "schedule duty and period_s must be numeric";
            return false;
        }
        const double d = js["duty"].get<double>();
        const double p = js.value("period_s", 0.0);
        if (!std::isfinite(d) || !(d >= 0.0 && d <= 1.0)) {
            err = "schedule duty must be finite and in [0,1]";
            return false;
        }
        if (!std::isfinite(p) || p < 0.0) {
            err = "schedule period_s must be finite and non-negative";
            return false;
        }
        if (d >= 1.0) return true;
        if (d <= 0.0) {
            out.times = {0.0};
            out.scales = {0.0};
            return true;
        }
        if (!(p > 0.0)) {
            err = "duty schedule needs period_s > 0";
            return false;
        }
        out.times = {0.0, d * p};
        out.scales = {1.0, 0.0};
        out.period = p;
        return true;
    }
    if (js.contains("points") && js["points"].is_array()) {
        if (js["points"].empty()) {
            err = "schedule points must not be empty";
            return false;
        }
        double previous = -1.0;
        for (const auto& pt : js["points"]) {
            if (!pt.is_array() || pt.size() != 2 || !pt[0].is_number() ||
                !pt[1].is_number()) {
                err = "schedule points must be numeric [t_s, scale] pairs";
                return false;
            }
            const double time = pt[0].get<double>();
            const double scale = pt[1].get<double>();
            if (!std::isfinite(time) || time < 0.0 || time <= previous ||
                !std::isfinite(scale)) {
                err = "schedule times must be finite, non-negative, and strictly "
                      "increasing; scales must be finite";
                return false;
            }
            out.times.push_back(time);
            out.scales.push_back(scale);
            previous = time;
        }
        if (out.times.front() != 0.0) {
            err = "schedule points must start at t_s = 0";
            return false;
        }
        if (js.contains("period_s") && !js["period_s"].is_number()) {
            err = "schedule period_s must be numeric";
            return false;
        }
        out.period = js.value("period_s", 0.0);
        if (!std::isfinite(out.period) || out.period < 0.0 ||
            (out.period > 0.0 && out.times.back() >= out.period)) {
            err = "schedule period_s must be finite, non-negative, and greater than "
                  "the last scheduled time";
            return false;
        }
        return true;
    }
    err = "schedule needs {duty, period_s} or {points:[[t,scale],...]}";
    return false;
}

}  // namespace

nlohmann::json fea_run_thermal(const nlohmann::json& study) {
    try {
        SimJobScope job(study);
    if (!study.is_object())
        return json{{"ok", false}, {"error", "fea_thermal: study must be an object"}};
    if (!has_mesh_source(study))
        return json{{"ok", false},
                    {"error", "fea_thermal: a non-empty STEP path or inline mesh is "
                              "required"}};
    if (study.contains("material") && !study["material"].is_object())
        return json{{"ok", false},
                    {"error", "fea_thermal: material must be an object"}};
    const json material_input = study.value("material", json::object());
    const json* conductivity =
        material_input.contains("k_w_mk") ? &material_input["k_w_mk"]
                                          : (study.contains("k_w_mk")
                                                 ? &study["k_w_mk"]
                                                 : nullptr);
    if (!conductivity || !conductivity->is_number())
        return json{{"ok", false},
                    {"error", "fea_thermal: material.k_w_mk must be numeric"}};
    const double k_input = conductivity->get<double>();
    if (!std::isfinite(k_input) || !(k_input > 0.0))
        return json{{"ok", false},
                    {"error", "fea_thermal: conductivity must be finite and positive"}};
    if (study.contains("fixtures") && !study["fixtures"].is_array())
        return json{{"ok", false},
                    {"error", "fea_thermal: fixtures must be an array"}};
    if (study.contains("loads") && !study["loads"].is_array())
        return json{{"ok", false},
                    {"error", "fea_thermal: loads must be an array"}};
    if (study.contains("transient") && !study["transient"].is_object())
        return json{{"ok", false},
                    {"error", "fea_thermal: transient must be an object"}};
    if (job.cancelled())
        return json{{"ok", false}, {"error", "fea_thermal: cancelled"}};
    json cache_study = study;
    cache_study.erase("job_id");
    const std::string input_hash =
        chudware::sim::cache::hash_fea_study(
            chudware::sim::cache::Kind::FeaThermal, cache_study);
    {
        auto hit = chudware::sim::cache::lookup(chudware::sim::cache::Kind::FeaThermal, input_hash);
        // See fea_run_static: a promised .vtu that no longer exists is a miss.
        if (hit.hit && !detail::vtu_evidence_missing(study)) {
            if (job.cancelled())
                return json{{"ok", false}, {"error", "fea_thermal: cancelled"}};
            json out = hit.result;
            out["cache_hit"] = true;
            out["cache_reason"] = "hit";
            out["input_hash"] = input_hash;
            if (job.cancelled())
                return json{{"ok", false}, {"error", "fea_thermal: cancelled"}};
            return out;
        }
    }
    chudware::prof::phases::StudyScope _study("fea_thermal");  // per-phase record (prof/phases.h)
    std::vector<double> points;
    std::vector<int> tets;
    int nodes_per_elem = 4;
    std::string mesh_type_used, mesh_note;
    std::optional<MeshAdvisor> mesh_advisor;
    detail::MeshQualityFacts mesh_quality;
    std::string err;
    {
        chudware::prof::phases::PhaseScope _p("mesh");
        if (!resolve_mesh(study, points, tets, nodes_per_elem, mesh_type_used,
                          mesh_note, mesh_advisor, mesh_quality, err))
            return json{{"ok", false}, {"error", err}};
    }
    if (job.cancelled())
        return json{{"ok", false}, {"error", "fea_thermal: cancelled"}};

    std::vector<std::string> notes;
    if (!mesh_note.empty()) notes.push_back("mesh: " + mesh_note);
    if (nodes_per_elem == 10) notes.push_back("second-order (tet10) elements");

    // Governor admission (scalar field: 1 DOF/node).
    {
        std::string why;
        const size_t ne = tets.size() / static_cast<size_t>(nodes_per_elem);
        if (!chudware::governor::admit_fea(ne, nodes_per_elem, 1, "thermal FEA solve",
                                           why))
            return json{{"ok", false}, {"error", why}};
    }

    // Conductivity enters in W/(m·K) and is converted exactly once to
    // W/(mm·K) for the millimetre mesh.
    const json& material = material_input;
    const double k_w_mk = k_input;
    const double k_cond = k_w_mk / 1000.0;

    // ── Dirichlet fixed-temperature faces ──────────────────────────────────
    std::vector<int> fixed_nodes;
    std::vector<double> fixed_temps;
    std::unordered_map<int, double> fixed_values;
    if (study.contains("fixtures")) {
        for (size_t i = 0; i < study["fixtures"].size(); ++i) {
            const auto& fx = study["fixtures"][i];
            if (!fx.is_object() || !fx.contains("temp_c") ||
                !fx["temp_c"].is_number())
                return json{{"ok", false},
                            {"error", "fea_thermal: fixture " +
                                          std::to_string(i) +
                                          " requires numeric temp_c"}};
            std::string faceset_error;
            if (!validate_faceset_input(fx, faceset_error))
                return json{{"ok", false},
                            {"error", "fea_thermal: fixture " +
                                          std::to_string(i) + ": " +
                                          faceset_error}};
            if (fx.contains("tol_mm") && !fx["tol_mm"].is_number())
                return json{{"ok", false},
                            {"error", "fea_thermal: fixture " +
                                          std::to_string(i) +
                                          " has non-numeric tol_mm"}};
            const double t_c = fx["temp_c"].get<double>();
            const double tol = fx.value("tol_mm", 1.0);
            if (!std::isfinite(t_c) || !std::isfinite(tol) || !(tol > 0.0))
                return json{{"ok", false},
                            {"error", "fea_thermal: fixture " + std::to_string(i) +
                                          " has non-finite temperature or invalid tol_mm"}};
            std::vector<int> ids =
                resolve_face_nodes(fx, points, tets, nodes_per_elem);
            if (ids.empty())
                return json{{"ok", false},
                            {"error", "fea_thermal: fixture " + std::to_string(i) +
                                          " resolved to 0 mesh nodes"}};
            for (int nid : ids) {
                const auto [it, inserted] = fixed_values.emplace(nid, t_c);
                if (!inserted && it->second != t_c)
                    return json{{"ok", false},
                                {"error", "fea_thermal: conflicting fixed temperatures "
                                          "were assigned to mesh node " +
                                              std::to_string(nid)}};
                if (inserted) {
                    fixed_nodes.push_back(nid);
                    fixed_temps.push_back(t_c);
                }
            }
        }
    }

    // ── Loads: heat flux + convection film ─────────────────────────────────
    std::vector<chudware::sim::ThermalConvection> convections;
    std::vector<chudware::sim::ThermalFluxLoad> flux_loads;
    struct FluxFaceLog {
        size_t n_nodes;
        double q_wm2;
        bool scheduled;
    };
    std::vector<FluxFaceLog> flux_log;
    const bool transient =
        study.contains("transient") && study["transient"].is_object();
    bool flux_excitation = false;
    if (study.contains("loads")) {
        for (size_t i = 0; i < study["loads"].size(); ++i) {
            const auto& load = study["loads"][i];
            if (!load.is_object() || !load.contains("faces") ||
                !load["faces"].is_object())
                return json{{"ok", false},
                            {"error", "fea_thermal: load " + std::to_string(i) +
                                          " requires a faces object"}};
            std::string faceset_error;
            if (!validate_faceset_input(load["faces"], faceset_error))
                return json{{"ok", false},
                            {"error", "fea_thermal: load " +
                                          std::to_string(i) + ": " +
                                          faceset_error}};
            if (load["faces"].contains("tol_mm") &&
                !load["faces"]["tol_mm"].is_number())
                return json{{"ok", false},
                            {"error", "fea_thermal: load " + std::to_string(i) +
                                          " has non-numeric tol_mm"}};
            const json& faces = load["faces"];
            const double tol = faces.value("tol_mm", 1.0);
            if (!std::isfinite(tol) || !(tol > 0.0))
                return json{{"ok", false},
                            {"error", "fea_thermal: load " + std::to_string(i) +
                                          " has invalid tol_mm"}};
            std::vector<int> ids =
                resolve_face_nodes(faces, points, tets, nodes_per_elem);
            if (ids.empty())
                return json{{"ok", false},
                            {"error", "fea_thermal: load " + std::to_string(i) +
                                          " resolved to 0 mesh nodes"}};
            if (!load.contains("type") || !load["type"].is_string())
                return json{{"ok", false},
                            {"error", "fea_thermal: load " + std::to_string(i) +
                                          " requires a string type"}};
            const std::string type = load.value("type", std::string());
            if (type == "flux") {
                if (!load.contains("magnitude") ||
                    !load["magnitude"].is_number())
                    return json{{"ok", false},
                                {"error", "fea_thermal: flux load " +
                                              std::to_string(i) +
                                              " requires a finite non-zero magnitude"}};
                const double q_wm2 = load["magnitude"].get<double>();
                if (!std::isfinite(q_wm2) || q_wm2 == 0.0)
                    return json{{"ok", false},
                                {"error", "fea_thermal: flux load " +
                                              std::to_string(i) +
                                              " requires a finite non-zero magnitude"}};
                chudware::sim::ThermalFluxLoad fl;
                fl.nodes = ids;
                fl.q = q_wm2 / 1.0e6;
                std::string serr;
                if (!parse_thermal_schedule(load.value("schedule", json()),
                                            fl.schedule, serr))
                    return json{{"ok", false},
                                {"error", "thermal load schedule: " + serr}};
                bool schedule_active = fl.schedule.times.empty();
                for (double scale : fl.schedule.scales)
                    if (scale != 0.0) schedule_active = true;
                flux_excitation =
                    flux_excitation || !transient || schedule_active;
                flux_log.push_back(
                    {ids.size(), q_wm2, load.contains("schedule")});
                flux_loads.push_back(std::move(fl));
                if (!transient && load.contains("schedule"))
                    notes.push_back(
                        "Flux schedule ignored in a steady-state study");
            } else if (type == "convection") {
                if (!load.contains("h") || !load["h"].is_number() ||
                    !load.contains("t_amb_c") ||
                    !load["t_amb_c"].is_number())
                    return json{{"ok", false},
                                {"error", "fea_thermal: convection load " +
                                              std::to_string(i) +
                                              " requires numeric h and t_amb_c"}};
                const double h = load["h"].get<double>();
                const double ambient = load["t_amb_c"].get<double>();
                if (!std::isfinite(h) || !(h > 0.0) ||
                    !std::isfinite(ambient))
                    return json{{"ok", false},
                                {"error", "fea_thermal: convection h must be finite "
                                          "and positive and ambient temperature must "
                                          "be finite"}};
                convections.push_back(
                    chudware::sim::ThermalConvection{ids, h / 1.0e6, ambient});
            } else {
                return json{{"ok", false},
                            {"error", "fea_thermal: unsupported load type '" + type +
                                          "'"}};
            }
        }
    }
    if (fixed_nodes.empty() && convections.empty() &&
        !(transient && flux_excitation))
        return json{{"ok", false},
                    {"error", "fea_thermal: the steady thermal system would be singular "
                              "without a temperature datum; provide a fixed-temperature "
                              "or convection boundary. A transient study may instead use "
                              "an active non-zero heat flux"}};
    if (job.cancelled())
        return json{{"ok", false}, {"error", "fea_thermal: cancelled"}};
    // ── Transient branch (T-FIN18); the steady branch follows below. ─────────────────────
    if (transient) {
        const json& tr = study["transient"];
        const json* cp_node =
            material.contains("cp_j_kgk") ? &material["cp_j_kgk"]
                                          : (study.contains("cp_j_kgk")
                                                 ? &study["cp_j_kgk"]
                                                 : nullptr);
        if (!cp_node || !cp_node->is_number())
            return json{{"ok", false},
                        {"error", "transient thermal needs numeric "
                                  "material.cp_j_kgk"}};
        if ((material.contains("rho_kg_m3") &&
             !material["rho_kg_m3"].is_number()) ||
            (material.contains("density_g_cm3") &&
             !material["density_g_cm3"].is_number()))
            return json{{"ok", false},
                        {"error", "transient thermal density must be numeric"}};
        const double cp = cp_node->get<double>();
        double rho_kg_m3 = 0.0;
        if (material.contains("rho_kg_m3") &&
            material["rho_kg_m3"].is_number())
            rho_kg_m3 = material["rho_kg_m3"].get<double>();
        else if (material.contains("density_g_cm3") &&
                 material["density_g_cm3"].is_number())
            rho_kg_m3 = material["density_g_cm3"].get<double>() * 1000.0;
        if (!std::isfinite(cp) || !(cp > 0.0) ||
            !std::isfinite(rho_kg_m3) || !(rho_kg_m3 > 0.0))
            return json{{"ok", false},
                        {"error", "transient thermal needs finite positive heat "
                                  "capacity and density"}};
        if (!tr.contains("t_end_s") || !tr["t_end_s"].is_number() ||
            (tr.contains("dt_s") && !tr["dt_s"].is_number()) ||
            (tr.contains("t_init_c") && !tr["t_init_c"].is_number()))
            return json{{"ok", false},
                        {"error", "transient thermal times and initial temperature "
                                  "must be numeric"}};
        if (tr.contains("capacitance") && !tr["capacitance"].is_string())
            return json{{"ok", false},
                        {"error", "transient capacitance must be a string"}};
        const std::string capacitance =
            tr.value("capacitance", std::string("lumped"));
        if (capacitance != "lumped" && capacitance != "consistent")
            return json{{"ok", false},
                        {"error", "transient capacitance must be 'lumped' or "
                                  "'consistent'"}};
        chudware::sim::ThermalTransientOptions opts;
        opts.rho_cp = rho_kg_m3 * cp / 1.0e9;
        opts.t_end = tr["t_end_s"].get<double>();
        opts.dt = tr.value("dt_s", 0.0);
        opts.t_init = tr.value("t_init_c", 20.0);
        opts.lumped = capacitance == "lumped";
        if (!std::isfinite(opts.t_end) || !(opts.t_end > 0.0) ||
            !std::isfinite(opts.dt) || opts.dt < 0.0 ||
            !std::isfinite(opts.t_init))
            return json{{"ok", false},
                        {"error", "transient thermal requires finite t_end_s > 0, "
                                  "dt_s >= 0, and finite t_init_c"}};
        if (tr.contains("frame_times_s")) {
            if (!tr["frame_times_s"].is_array())
                return json{{"ok", false},
                            {"error", "transient frame_times_s must be an array"}};
            for (const auto& t : tr["frame_times_s"]) {
                if (!t.is_number() || !std::isfinite(t.get<double>()))
                    return json{{"ok", false},
                                {"error", "transient frame times must be finite "
                                          "numbers"}};
                opts.frame_times.push_back(t.get<double>());
            }
        }
        if (opts.dt == 0.0) {
            opts.dt = opts.t_end / 200.0;
            notes.push_back(
                "transient dt_s not given — defaulted to t_end_s/200");
        }
        bool excitation = flux_excitation;
        for (double temp : fixed_temps)
            excitation = excitation || temp != opts.t_init;
        for (const auto& cv : convections)
            excitation = excitation || cv.t_amb != opts.t_init;
        if (!excitation)
            return json{{"ok", false},
                        {"error", "fea_thermal: transient study has no non-zero "
                                  "thermal excitation relative to t_init_c"}};
        if (job.cancelled())
            return json{{"ok", false}, {"error", "fea_thermal: cancelled"}};
        chudware::sim::ThermalTransientResult r = [&] {
            chudware::prof::phases::PhaseScope _p("solve.transient");
            return chudware::sim::fea_thermal_transient(
                points, tets, k_cond, fixed_nodes, fixed_temps, flux_loads,
                convections, opts, nodes_per_elem, job.token());
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
        if (mesh_advisor) out["advisor"] = detail::mesh_advisor_json(*mesh_advisor);
        out["mesh_quality"] = detail::mesh_quality_json(
            mesh_quality, static_cast<std::size_t>(r.n_nodes),
            static_cast<std::size_t>(r.n_elems), nodes_per_elem);
        out["provenance"] = detail::provenance_json("chudware/fea_thermal");
        {
            // The .vtu carries the FINAL temperature field (the "temperature" key
            // above); per-frame files would need one path per frame.
            std::string vtu_path, vtu_error;
            if (!detail::write_result_vtu(study, points, tets, nodes_per_elem, out,
                                          vtu_path, vtu_error))
                return json{{"ok", false},
                            {"error", "fea_thermal: " + vtu_error},
                            {"solver_notes", notes}};
            if (!vtu_path.empty()) out["vtu_path"] = vtu_path;
        }
        if (job.cancelled())
            return json{{"ok", false}, {"error", "fea_thermal: cancelled"}};
        chudware::sim::cache::store(chudware::sim::cache::Kind::FeaThermal, input_hash, out);
        out["cache_hit"] = false;
        out["cache_reason"] = "miss";
        out["input_hash"] = input_hash;
        return out;
    }

    bool excitation = flux_excitation;
    double boundary_min = std::numeric_limits<double>::infinity();
    double boundary_max = -std::numeric_limits<double>::infinity();
    for (double temp : fixed_temps) {
        boundary_min = std::min(boundary_min, temp);
        boundary_max = std::max(boundary_max, temp);
    }
    for (const auto& cv : convections) {
        boundary_min = std::min(boundary_min, cv.t_amb);
        boundary_max = std::max(boundary_max, cv.t_amb);
    }
    excitation = excitation || boundary_max > boundary_min;
    if (!excitation)
        return json{{"ok", false},
                    {"error", "fea_thermal: steady study has no non-zero thermal "
                              "excitation"}};
    if (job.cancelled())
        return json{{"ok", false}, {"error", "fea_thermal: cancelled"}};
    chudware::sim::ThermalResult r = [&] {
        chudware::prof::phases::PhaseScope _p("solve");
        return chudware::sim::fea_thermal_solve(
            points, tets, k_cond, fixed_nodes, fixed_temps, flux_loads,
            convections, nodes_per_elem, job.token());
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
    if (mesh_advisor) out["advisor"] = detail::mesh_advisor_json(*mesh_advisor);
    out["mesh_quality"] = detail::mesh_quality_json(
        mesh_quality, static_cast<std::size_t>(r.n_nodes),
        static_cast<std::size_t>(r.n_elems), nodes_per_elem);
    out["provenance"] = detail::provenance_json("chudware/fea_thermal");
    {
        std::string vtu_path, vtu_error;
        if (!detail::write_result_vtu(study, points, tets, nodes_per_elem, out,
                                      vtu_path, vtu_error))
            return json{{"ok", false},
                        {"error", "fea_thermal: " + vtu_error},
                        {"solver_notes", notes}};
        if (!vtu_path.empty()) out["vtu_path"] = vtu_path;
    }
    if (job.cancelled())
        return json{{"ok", false}, {"error", "fea_thermal: cancelled"}};
    chudware::sim::cache::store(chudware::sim::cache::Kind::FeaThermal, input_hash, out);
    out["cache_hit"] = false;
    out["cache_reason"] = "miss";
    out["input_hash"] = input_hash;
    return out;
    } catch (const nlohmann::json::exception& e) {
        return json{{"ok", false},
                    {"error", std::string("fea_thermal: invalid study JSON: ") +
                                  e.what()}};
    }
}

// Memoize mesh_step by STEP contents plus every MeshOptions field. Cache hits
// return an immutable resident mesh without copying its point/connectivity
// arrays. Residency is also bounded by a byte budget derived from the resource
// governor (not only entry count): fine assembly meshes are MBs each and a
// fixed kCap of 8 can still OOM a low-end box.
namespace {
size_t mesh_out_bytes(const MeshOut &mesh) {
  size_t bytes =
      mesh.points.size() * sizeof(double) + mesh.tets.size() * sizeof(int) +
      mesh.aspect_hist.size() * sizeof(int) + mesh.mesh_type_used.size() +
      mesh.note.size() + mesh.error.size() + 256;
  if (mesh.advisor)
    bytes += sizeof(MeshAdvisor) + mesh.advisor->mesher.size() +
             mesh.advisor->model_revision.size() + mesh.advisor->note.size();
  return bytes;
}
uint64_t mesh_cache_budget_bytes() {
  const uint64_t host = chudware::governor::budget_bytes();
  if (host == UINT64_MAX)
    return 512ull * 1024ull * 1024ull; // 512 MiB when governor off
  // Cap mesh-cache residency at 12.5% of the study budget, floored at 64 MiB so
  // a small machine still keeps a couple of warm meshes.
  const uint64_t frac = host / 8;
  return std::max<uint64_t>(64ull * 1024ull * 1024ull, frac);
}
} // namespace

// Process-wide whole-STEP mesh-cache counters.
namespace mesh_cache_detail {

inline uint64_t fnv1a(const void *p, size_t n,
                      uint64_t h = 1469598103934665603ull) {
  const auto *b = static_cast<const unsigned char *>(p);
  for (size_t i = 0; i < n; ++i) {
    h ^= b[i];
    h *= 1099511628211ull;
  }
  return h;
}
inline uint64_t mesh_opts_key(const MeshOptions &options, uint64_t hash) {
  hash = fnv1a(&options.fineness, sizeof options.fineness, hash);
  hash = fnv1a(&options.order, sizeof options.order, hash);
  hash = fnv1a(&options.target_size_mm, sizeof options.target_size_mm, hash);
  hash = fnv1a(&options.min_size_mm, sizeof options.min_size_mm, hash);
  hash = fnv1a(&options.max_size_mm, sizeof options.max_size_mm, hash);
  hash = fnv1a(&options.learned_advisor, sizeof options.learned_advisor, hash);
  hash = fnv1a(&options.advisor_max_dof, sizeof options.advisor_max_dof, hash);
  return hash;
}
struct MeshCacheCounters {
  std::mutex mtx;
  MeshCacheStats s;
};
inline MeshCacheCounters &mesh_cache_counters() {
  static MeshCacheCounters c;
  return c;
}
} // namespace mesh_cache_detail
using mesh_cache_detail::fnv1a;
using mesh_cache_detail::mesh_cache_counters;
using mesh_cache_detail::mesh_opts_key;

MeshCacheStats mesh_cache_stats() {
  auto &c = mesh_cache_counters();
  std::lock_guard<std::mutex> lk(c.mtx);
  return c.s;
}

void mesh_cache_reset_stats() {
  auto &c = mesh_cache_counters();
  std::lock_guard<std::mutex> lk(c.mtx);
  c.s = MeshCacheStats{};
}

void mesh_cache_note_generation() {
  auto &c = mesh_cache_counters();
  std::lock_guard<std::mutex> lk(c.mtx);
  ++c.s.generations;
}

void mesh_cache_set_resident(uint64_t bytes, uint64_t budget) {
  auto &c = mesh_cache_counters();
  std::lock_guard<std::mutex> lk(c.mtx);
  c.s.resident_bytes = bytes;
  c.s.budget_bytes = budget;
}

std::shared_ptr<const MeshOut> mesh_step_cached(const std::string &step_path,
                                                const MeshOptions &options) {
  std::string option_error;
  if (!validate_mesh_options(options, option_error)) {
    MeshOut invalid;
    invalid.nodes_per_elem = options.order == 2 ? 10 : 4;
    invalid.error = option_error;
    return std::make_shared<const MeshOut>(std::move(invalid));
  }
  // Hash the STEP bytes (one block read — not the old istreambuf char-at-a-time
  // slurp). On any read failure, skip the cache and mesh directly.
  std::string bytes;
  {
    std::ifstream f(step_path, std::ios::binary | std::ios::ate);
    if (f) {
      const std::streamsize n = f.tellg();
      if (n > 0) {
        bytes.resize(static_cast<size_t>(n));
        f.seekg(0);
        if (!f.read(&bytes[0], n))
          bytes.clear();
      }
    }
  }
  if (bytes.empty())
    return std::make_shared<const MeshOut>(mesh_step(step_path, options));
  uint64_t key = fnv1a(bytes.data(), bytes.size());
  key = mesh_opts_key(options, key);

  static std::mutex mtx;
  static std::unordered_map<uint64_t, std::shared_ptr<const MeshOut>> cache;
  static std::list<uint64_t> lru; // most-recent at front
  static uint64_t resident = 0;
  constexpr size_t kCap = 16; // soft entry cap; byte budget is the hard bound
  {
    std::lock_guard<std::mutex> lk(mtx);
    auto it = cache.find(key);
    if (it != cache.end()) { // HIT: identical geometry + options — no copy
      lru.remove(key);
      lru.push_front(key);
      {
        auto &c = mesh_cache_counters();
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
    auto &c = mesh_cache_counters();
    std::lock_guard<std::mutex> ck(c.mtx);
    ++c.s.misses;
  }
  auto m = std::make_shared<const MeshOut>(mesh_step(step_path, options));
  if (m->ok) {
    const uint64_t need = static_cast<uint64_t>(mesh_out_bytes(*m));
    const uint64_t budget = mesh_cache_budget_bytes();
    std::lock_guard<std::mutex> lk(mtx);
    // Evict LRU until the new mesh fits (or the cache is empty).
    while ((!lru.empty()) && (lru.size() >= kCap || resident + need > budget)) {
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

}  // namespace chudware::sim
