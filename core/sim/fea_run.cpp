// fea_run.cpp — see fea_run.h. The combined mesh+resolve+solve glue.
#include "sim/fea_run.h"

#include "prof/phases.h"

#include <algorithm>
#include <array>
#include <chrono>
#include <cctype>
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

// ── OCCT, for the analytic contact-gap measurement only ───────────────────────
// detail::make_cad_gap_probe re-reads the study's own STEP/BREP so a candidate
// pair's clearance can be measured against the CAD faces instead of the mesh
// facets. Nothing else in this TU touches OCCT; the mesher owns the geometry
// pipeline. NOMINMAX is set project-wide (core/CMakeLists.txt) so the OCCT ->
// windows.h transitive include cannot clobber std::min/std::max below.
#include <BRepBndLib.hxx>
#include <BRepBuilderAPI_MakeVertex.hxx>
#include <BRepClass3d_SolidClassifier.hxx>
#include <BRepExtrema_DistShapeShape.hxx>
#include <BRepTools.hxx>
#include <BRep_Builder.hxx>
#include <Bnd_Box.hxx>
#include <Extrema_ExtFlag.hxx>
#include <IFSelect_ReturnStatus.hxx>
#include <Precision.hxx>
#include <STEPControl_Reader.hxx>
#include <Standard_Failure.hxx>
#include <TopAbs_State.hxx>
#include <TopExp_Explorer.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Shape.hxx>
#include <TopoDS_Solid.hxx>
#include <TopoDS_Vertex.hxx>
#include <gp_Pnt.hxx>

#include "sim/fea_contact.h"
#include "sim/fea_modal.h"
#include "sim/fea_selection.h"
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
    // Fill cells Chudware had to correct before the mesh could carry a solve
    // (mesher.h MeshOut): `reoriented` were listed left-handed (node order
    // only, no geometry change), `inflated` were shipped FLAT and were given
    // volume by moving one node inside a hard bound. Both are counts, both ride
    // with every mesh: a nonzero value is why an order-2 study on a sharp-edged
    // part runs at all, and it is also the evidence that the delivered geometry
    // is not byte-identical to the fill's.
    int reoriented_elems = 0;
    int degenerate_elems_inflated = 0;
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
    // ── The refined region's own quality (mesher.h MeshOut) ──────────────────
    // A local size contract is delivered by conforming bisection, which is
    // shapely nowhere except at the interface it was asked for: the ME 490
    // hinge-latch packet shipped a study whose interface was exactly the
    // requested size and whose mesh one layer out carried aspect_max 263 and
    // jacobian_min 0.0029. So the mesher runs a bounded quality pass over that
    // region and reports what it found and what it left. `active` is false when
    // the study made no local request, in which case the block is omitted
    // rather than reported as a row of zeros.
    bool local_refine_active = false;
    int local_refine_waves = 0;
    int local_refine_smoothing_passes = 0;
    double local_refine_jacobian_min_before = 0.0;
    double local_refine_jacobian_min_after = 0.0;
    double local_refine_aspect_max_before = 0.0;
    double local_refine_aspect_max_after = 0.0;
    std::uint64_t local_refine_cells_below_floor_before = 0;
    std::uint64_t local_refine_cells_below_floor_after = 0;
    bool local_refine_resplit = false;
    // ── Solids the mesher FUSED, and the sub-element debris it dissolved ────
    // Both are edits to the DOMAIN the study solves, so both ride with the
    // packet: a fused body is a different structure from two touching solids
    // (it carries moment across the weld), and the volume delta is the
    // interpenetration the design model contained. `bodies` carries one entry
    // per meshed body with the 1-based STEP solid ordinals that went into it,
    // which is what lets a rigid-body refusal name an island's body.
    std::vector<chudware::sim::MeshOut::Body> bodies;
    int fused_groups = 0;
    int fused_solids = 0;
    double fused_overlap_mm3 = 0.0;
    int debris_components_dissolved = 0;
    int debris_elems_dropped = 0;
    double debris_volume_mm3 = 0.0;
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
    out["reoriented_elems"] = quality.reoriented_elems;
    out["degenerate_elems_inflated"] = quality.degenerate_elems_inflated;
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
    // The refined region's before/after, only where a local request was made.
    // These describe the REGION the size contract was delivered in, not the
    // whole mesh — the aspect_max / jacobian_min above still do that — because
    // a 300 k-element mesh whose refined shell is debris reads healthy on a
    // whole-mesh statistic.
    if (quality.local_refine_active)
        out["local_refine"] =
            json{{"waves", quality.local_refine_waves},
                 {"smoothing_passes", quality.local_refine_smoothing_passes},
                 {"jacobian_min_before", quality.local_refine_jacobian_min_before},
                 {"jacobian_min_after", quality.local_refine_jacobian_min_after},
                 {"aspect_max_before", quality.local_refine_aspect_max_before},
                 {"aspect_max_after", quality.local_refine_aspect_max_after},
                 {"cells_below_floor_before",
                  quality.local_refine_cells_below_floor_before},
                 {"cells_below_floor_after",
                  quality.local_refine_cells_below_floor_after},
                 {"resplit", quality.local_refine_resplit}};
    // ── What the mesher did to the DOMAIN before solving it ─────────────────
    // Present only when it happened, so a study on one clean solid carries
    // neither block. `fused_bodies` is per fused body: which 1-based STEP
    // solid ordinals were welded, the fused volume, and the overlap the fuse
    // removed — the interpenetration the design model carried, measured.
    if (quality.fused_groups > 0) {
        json fused = json::array();
        for (const auto& body : quality.bodies) {
            if (body.source_ordinals.size() < 2) continue;
            fused.push_back(json{{"body_ordinals", body.source_ordinals},
                                 {"volume_mm3", body.volume_mm3},
                                 {"overlap_removed_mm3", body.fused_overlap_mm3}});
        }
        out["fused_bodies"] = std::move(fused);
        out["fused_solids"] = quality.fused_solids;
        out["fused_overlap_mm3"] = quality.fused_overlap_mm3;
    }
    if (quality.debris_components_dissolved > 0)
        out["debris_islands_dissolved"] =
            json{{"components", quality.debris_components_dissolved},
                 {"elements", quality.debris_elems_dropped},
                 {"volume_mm3", quality.debris_volume_mm3}};
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
    // equivalent_plastic_strain rides beside them on an elastic-plastic run: it is the
    // field that says WHERE the redistribution happened, and a von Mises plot capped
    // at the flow stress is uninterpretable without it.
    for (const char* scalar :
         {"von_mises", "summary_node", "temperature", "equivalent_plastic_strain"}) {
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

// The two numbers the MESH needs out of study.contact, read leniently: the
// contact block's own strict validation runs later (it has to, because a
// malformed block must refuse the STUDY, not the mesh), and this pass only ever
// asks the mesher for MORE resolution. A block that is not an object, disabled,
// or carries a non-positive size simply requests nothing.
double contact_interface_size_mm(const json& study) {
    if (!study.contains("contact") || !study["contact"].is_object()) return 0.0;
    const json& c = study["contact"];
    if (!c.value("enabled", true)) return 0.0;
    const double size = c.value("interface_size_mm", 0.0);
    return std::isfinite(size) && size > 0.0 ? size : 0.0;
}

// How far the DELIVERED interface size may sit above the requested one before
// the study is refused. The number and its reasoning live in sim/mesher.h
// (kLocalContractSlack), because the mesher's own last-resort re-fill decides
// whether to spend a whole second fill on the same question and the two must
// not disagree about whether a contract was met.
constexpr double kInterfaceContractSlack = chudware::sim::kLocalContractSlack;

double contact_max_gap_mm(const json& study) {
    if (!study.contains("contact") || !study["contact"].is_object()) return 0.5;
    const double gap = study["contact"].value("max_gap_mm", 0.5);
    return std::isfinite(gap) && gap > 0.0 ? gap : 0.5;
}

// Has the study DECLARED how the interfaces between its bodies are treated? A
// `contact` block (enabled) or a positive `bond_gap_mm` says the bodies are
// meant to stay separate and to interact through their surfaces, so the mesher
// must not fuse touching solids into one body (mesher.h
// MeshOptions::declared_interfaces). Read leniently for the same reason as the
// two above: a malformed block refuses the STUDY later, not the mesh.
bool declares_body_interfaces(const json& study) {
    if (study.contains("contact") && study["contact"].is_object() &&
        study["contact"].value("enabled", true))
        return true;
    if (study.contains("bond_gap_mm") && study["bond_gap_mm"].is_number() &&
        study["bond_gap_mm"].get<double>() > 0.0)
        return true;
    if (const char* env = std::getenv("CHUDWARE_FEA_BOND_GAP_MM")) {
        char* end = nullptr;
        const double parsed = std::strtod(env, &end);
        if (end != env && *end == '\0' && std::isfinite(parsed) && parsed > 0.0)
            return true;
    }
    return false;
}

// The contact.resolution block: the discretization facts a reader needs to know
// whether "contact_force_n / contact_area_mm2" is a pressure or an artefact.
// Same shape whether the study was refused before the solve (the a-priori
// clause: this mesh cannot describe the interface) or after it (the load
// crossed through too few pairs), so a caller parses one thing.
//
// `criterion` says WHICH a-priori test was applied, and the numbers that follow
// are the ones that test uses: a clearance carries ratio/max_ratio, two
// TOUCHING faces have no clearance and carry the candidate patch area and the
// coarsest element size that patch admits. Printing "clearance 0 mm" beside a
// ratio of 0 read as a resolved interface however coarse the mesh was.
json contact_resolution_json(const ContactResolution& r) {
    json out{{"h_interface_mm", round_nd(r.h_interface_mm, 6)},
             {"gap_mm", round_nd(r.gap_mm, 6)},
             {"criterion", r.criterion ? r.criterion : kCriterionClearance},
             {"candidate_pairs", r.candidate_pairs},
             {"seatable_pairs", r.seatable_pairs},
             // The LENGTH that decided seatable_pairs: a candidate at or below it is
             // a gap this mesh cannot tell from touching, so it starts the solve
             // seated (fea_contact.h kSeatFractionH). initial_penetration_max_mm is
             // how deep the placement/facet error pushed a node through the partner
             // face; it was clamped to a touching gap rather than turned into force.
             {"seat_limit_mm", round_nd(r.seat_limit_mm, 6)},
             {"initial_penetration_max_mm", round_nd(r.initial_penetration_max_mm, 6)},
             {"min_active_pairs", r.min_active_pairs},
             {"candidate_area_mm2", round_nd(r.candidate_area_mm2, 6)},
             {"required_interface_size_mm", round_nd(r.required_interface_size_mm, 4)},
             // Where gap_mm came from, and what the mesh alone would have said.
             // On a curved fit those are different numbers and the difference is
             // the whole reason the guard used to be unsatisfiable: the ME 490
             // hinge's 0.300 mm saddle clearance read 0.03665 mm on the facets
             // and demanded an interface size no mesher could deliver
             // (fea_contact.h ContactGapProbe). `gap_source` is "cad_faces" when
             // the analytic surfaces answered, "mesh_facets" when there was
             // nothing to project onto; `gap_fallback_nodes` counts the nodes in
             // repaired/dissolved regions that kept the facet value anyway.
             {"gap_source", r.gap_analytic ? "cad_faces" : "mesh_facets"},
             {"gap_facet_mm", round_nd(r.gap_facet_mm, 6)},
             {"facet_error_mm", round_nd(r.facet_error_mm, 6)},
             {"gap_fallback_nodes", r.gap_fallback_nodes},
             {"resolved", r.resolved}};
    if (r.criterion == kCriterionPatchArea) {
        out["max_h_interface_mm"] = round_nd(r.max_h_interface_mm, 4);
        out["min_elements_across_patch"] = kContactMinPatchElems;
    } else {
        out["ratio"] = round_nd(r.ratio, 4);
        out["max_ratio"] = r.max_ratio;
    }
    if (r.active_pairs >= 0) {
        out["active_pairs"] = r.active_pairs;
        out["contact_area_mm2"] = round_nd(r.contact_area_mm2, 6);
    }
    if (!r.reason.empty()) out["reason"] = r.reason;
    return out;
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
    options.declared_interfaces = declares_body_interfaces(study);
    const std::string step_path = study["step_path"].get<std::string>();
    auto mesh = mesh_step_cached(step_path, options);
    if (!mesh->ok) {
      err = "mesh failed: " + mesh->error;
      return false;
    }
    // ── contact.interface_size_mm: LOCAL refinement of the contact faces ──────
    // The candidate faces are only knowable from a mesh, so this is a two-pass
    // operation: mesh once, find the pairs the study's own max_gap_mm produces,
    // then re-mesh with those candidate nodes as local refinement seeds. It is
    // the interface that gets refined, not the part — the whole reason the knob
    // exists is that resolving a 0.35 mm clearance with a uniform element size
    // costs a million elements.
    if (const double interface_size = contact_interface_size_mm(study);
        interface_size > 0.0) {
      // No gap probe here, deliberately: this pass wants SEED LOCATIONS, and the
      // facet gap only ever understates a curved clearance, so a facet-measured
      // set is a superset of the analytic one — every face the study will later
      // judge is refined, plus a few that turn out to be further apart than
      // max_gap_mm. Paying for a second CAD read to refine slightly fewer faces
      // would be the wrong trade.
      const ContactCandidateSet probe = fea_contact_candidates(
          mesh->points, mesh->tets, mesh->nodes_per_elem, /*E_mpa=*/1.0,
          ContactCandidateOptions{contact_max_gap_mm(study), 1.0, 0.35});
      if (probe.error.empty() && !probe.springs.empty()) {
        MeshOptions refined = options;
        refined.local_refine.size_mm = interface_size;
        // One coarse element of influence around each seed, and never less than
        // the requested size: the coarse candidates are a node spacing apart,
        // so a narrower band would refine dots instead of the interface.
        refined.local_refine.band_mm =
            std::max(mesh->char_len_mm, 2.0 * interface_size);
        refined.local_refine.label = "contact interface";
        // Seeds at the candidate NODES themselves — both endpoints, so both
        // bodies' faces are refined — deduped onto a grid of the requested size
        // so a dense candidate set cannot blow the seed count up. The midpoint of
        // the pair was tried first and is worse: it lies in the clearance, i.e.
        // outside both solids, and the fill carves those cells away.
        std::set<std::array<long long, 3>> cells;
        const double cell = std::max(interface_size, 1e-6);
        auto seed_node = [&](int node) {
            const double* p = &mesh->points[static_cast<size_t>(node) * 3];
            const std::array<long long, 3> key{
                static_cast<long long>(std::floor(p[0] / cell)),
                static_cast<long long>(std::floor(p[1] / cell)),
                static_cast<long long>(std::floor(p[2] / cell))};
            if (!cells.insert(key).second) return;
            for (int c = 0; c < 3; ++c)
                refined.local_refine.points.push_back(p[c]);
        };
        for (const ContactSpring& s : probe.springs) {
            seed_node(s.a);
            seed_node(s.b);
        }
        const auto refined_mesh = mesh_step_cached(step_path, refined);
        // A refinement contract that cannot be met is a REFUSAL, not a silent
        // fallback to the coarse mesh the study asked to get away from.
        if (!refined_mesh->ok) {
          err = "mesh failed with contact.interface_size_mm = " +
                std::to_string(interface_size) + " mm: " + refined_mesh->error;
          return false;
        }
        // ── The contract is VERIFIED, not assumed ────────────────────────────
        // Every lever that can raise a local size raises it silently: the
        // lattice cannot place below background/4, polymesh floors the
        // background at its own grid-cell budget, and the element ceiling stops
        // the bisection waves. The live keyway asked for 0.35 mm and solved a
        // 1.299 mm interface — converged, balanced, and physically meaningless.
        // So the achieved size is measured on the mesh and a miss refuses,
        // naming what bound and what IS achievable.
        const double achieved = refined_mesh->local_refine_achieved_mm;
        if (achieved > kInterfaceContractSlack * interface_size) {
          char text[768];
          if (refined_mesh->local_refine_ceiling_bound)
            std::snprintf(
                text, sizeof(text),
                "contact.interface_size_mm = %.4g mm was not delivered: the local "
                "refinement stopped at this machine's mesh element ceiling of %llu "
                "elements (%d element(s) added over %d bisection wave(s)), so the "
                "element size AT the interface is %.4g mm. Free memory (the ceiling "
                "is the study memory budget divided by the measured mesh cost per "
                "element) or isolate a smaller assembly — %.4g mm is what this "
                "ceiling affords here",
                interface_size,
                static_cast<unsigned long long>(
                    refined_mesh->local_refine_element_ceiling),
                refined_mesh->local_refine_elems_added,
                refined_mesh->local_refine_waves, achieved, achieved);
          else {
            // ── The remedy has to be a LEGAL value, and it was not ───────────
            // `interface_size_mm` becomes the h_min of the mesher's own graded
            // size field, whose h_max is the background, so a request ABOVE the
            // background is not a coarser request — it is an illegal one
            // (`GradedSizing: need 0 < h_min <= h_max`, reproduced on the O-22
            // hinge saddle at a 1.2 mm background). Advising "pass `achieved` or
            // coarser" when `achieved` exceeded the background therefore closed
            // the box: every request at or below the background was refused as
            // undeliverable and every request above it mesh-FAILED, which is the
            // "no legal value exists" that stopped CONTACT_BLOCKED_2026-09-06b.
            // The field now clamps h_min to the background (mesher.cpp
            // local_size_field), so a coarse request is legal and trivially met;
            // this text must still only ever name a value inside (0, background].
            const double background = refined_mesh->char_len_mm;
            const double legal = std::min(achieved, background);
            if (achieved <= background)
              std::snprintf(
                  text, sizeof(text),
                  "contact.interface_size_mm = %.4g mm was not delivered: after "
                  "%d bisection wave(s) the element size AT the interface is "
                  "%.4g mm (background %.4g mm, the fill's own graded floor "
                  "%.4g mm — bisection is what goes below that). The study is "
                  "refused rather than solved on an interface coarser than it "
                  "asked for. Legal remedies: pass interface_size_mm between "
                  "%.4g mm and the %.4g mm background (it is the size field's "
                  "h_min and must satisfy 0 < interface_size_mm <= "
                  "mesh_size_mm), lower mesh_size_mm to about %.4g mm so the "
                  "fill itself places the request, or re-isolate the bodies so "
                  "the interface carries fewer candidate faces",
                  interface_size, refined_mesh->local_refine_waves, achieved,
                  background, background * 0.25, legal, background,
                  4.0 * interface_size);
            else
              std::snprintf(
                  text, sizeof(text),
                  "contact.interface_size_mm = %.4g mm was not delivered: after "
                  "%d bisection wave(s) the element size AT the interface is "
                  "%.4g mm, which is COARSER than the %.4g mm background itself "
                  "— the faces carrying this interface are stretched, not merely "
                  "unrefined, so no interface_size_mm is deliverable here "
                  "(the request is the size field's h_min and must satisfy "
                  "0 < interface_size_mm <= mesh_size_mm, so nothing above the "
                  "background is legal either). Legal remedies: lower "
                  "mesh_size_mm to about %.4g mm so the fill places the request "
                  "on its own lattice, or re-isolate the bodies so the interface "
                  "carries fewer candidate faces",
                  interface_size, refined_mesh->local_refine_waves, achieved,
                  background, 4.0 * interface_size);
          }
          err = text;
          // What the mesher itself hit, when it hit something nameable (a
          // bisection closure that could not conform, a wave that grew nothing):
          // the refusal is only actionable if it says so.
          if (!refined_mesh->local_refine_limit.empty())
            err += " — " + refined_mesh->local_refine_limit;
          return false;
        }
        mesh = refined_mesh;
      }
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
    quality.reoriented_elems = mesh->reoriented_elems;
    quality.degenerate_elems_inflated = mesh->degenerate_elems_repaired;
    quality.min_tet_volume_mm3 = mesh->min_tet_volume_mm3;
    quality.max_tet_edge_ratio = mesh->max_tet_edge_ratio;
    quality.connected_components = mesh->connected_components;
    quality.thin_wall_ratio = mesh->thin_wall_ratio;
    quality.lonely_nodes_repaired = mesh->lonely_nodes_repaired;
    quality.lonely_nodes_remaining = mesh->lonely_nodes_remaining;
    quality.hanging_elems_dropped = mesh->hanging_elems_dropped;
    quality.bodies = mesh->bodies;
    quality.fused_groups = mesh->fused_groups;
    quality.fused_solids = mesh->fused_solids;
    quality.fused_overlap_mm3 = mesh->fused_overlap_mm3;
    quality.debris_components_dissolved = mesh->debris_components_dissolved;
    quality.debris_elems_dropped = mesh->debris_elems_dropped;
    quality.debris_volume_mm3 = mesh->debris_volume_mm3;
    // Only a study that actually asked for a local size carries the region
    // report; `local_refine_size_mm` is the request, and it is zero otherwise.
    if (mesh->local_refine_size_mm > 0.0) {
      quality.local_refine_active = true;
      quality.local_refine_waves = mesh->local_refine_waves;
      quality.local_refine_smoothing_passes = mesh->local_refine_smoothing_passes;
      quality.local_refine_jacobian_min_before =
          mesh->local_refine_jacobian_min_before;
      quality.local_refine_jacobian_min_after =
          mesh->local_refine_jacobian_min_after;
      quality.local_refine_aspect_max_before = mesh->local_refine_aspect_max_before;
      quality.local_refine_aspect_max_after = mesh->local_refine_aspect_max_after;
      quality.local_refine_cells_below_floor_before =
          mesh->local_refine_cells_below_floor_before;
      quality.local_refine_cells_below_floor_after =
          mesh->local_refine_cells_below_floor_after;
      quality.local_refine_resplit = mesh->local_refine_resplit;
    }
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

namespace detail {

namespace {

// ── The CAD-face gap oracle (fea_run.h make_cad_gap_probe) ────────────────────
// One solid per body, each with its faces and their bounding boxes. A query is
//   1. which solid does this node belong to?  -> the one whose boundary is
//      nearest it (a boundary node lies ON its own body's surface to within the
//      mesher's snap residual, and a clearance fit puts the other body a whole
//      clearance away, so the argmin is not close),
//   2. how far is the node from the OTHER solid's boundary, signed?
// Both are the same primitive, so one memo table serves both: a node takes part
// in one or two pairs and each pair asks about two solids.
class CadGapProbe final : public ContactGapProbe {
public:
    CadGapProbe(std::vector<TopoDS_Solid> solids, double reach_mm)
        : reach_(reach_mm) {
        bodies_.reserve(solids.size());
        for (TopoDS_Solid& solid : solids) {
            Body body;
            for (TopExp_Explorer ex(solid, TopAbs_FACE); ex.More(); ex.Next()) {
                Facet facet;
                facet.face = TopoDS::Face(ex.Current());
                BRepBndLib::Add(facet.face, facet.box);
                facet.box.SetGap(facet.box.GetGap() + reach_);
                body.faces.push_back(std::move(facet));
            }
            if (body.faces.empty()) continue;
            // BRepClass3d_SolidClassifier is neither copyable nor movable (its
            // solid explorer's copy ctor is private), so the body owns one.
            body.classifier =
                std::make_unique<BRepClass3d_SolidClassifier>(solid);
            bodies_.push_back(std::move(body));
        }
    }

    std::size_t bodies() const { return bodies_.size(); }

    bool pair_gap_mm(int node_a, const double* pa, int node_b, const double* pb,
                     double& gap_mm) const override {
        const int own_a = owner(node_a, pa);
        const int own_b = owner(node_b, pb);
        if (own_a < 0 || own_b < 0 || own_a == own_b) return false;
        double ga = 0.0, gb = 0.0;
        const bool ok_a = signed_gap(node_a, pa, own_b, ga);
        const bool ok_b = signed_gap(node_b, pb, own_a, gb);
        if (ok_a && ok_b) {
            // The CLOSEST of the two perpendicular measurements. Each is exact for
            // a concentric fit; where the clearance varies along the interface the
            // smaller one is the approach the contact will close first, which is
            // the number the guard and the seat limit are about.
            gap_mm = std::min(ga, gb);
            return true;
        }
        if (ok_a) { gap_mm = ga; return true; }
        if (ok_b) { gap_mm = gb; return true; }
        return false;
    }

private:
    struct Facet {
        TopoDS_Face face;
        Bnd_Box box;  // grown by reach_, so a hit means "worth measuring"
    };
    struct Body {
        std::vector<Facet> faces;
        // BRepClass3d_SolidClassifier gives the SIGN without needing a surface
        // normal at the projection (which is undefined when the closest point
        // lands on a seam edge or a vertex): inside the material is negative.
        std::unique_ptr<BRepClass3d_SolidClassifier> classifier;
    };

    // Signed distance from `p` to body `b`, memoized per (node, body). false when
    // no face of that body lies within reach_ of the point.
    bool signed_gap(int node, const double* p, int b, double& out) const {
        const long long key = static_cast<long long>(node) * 64LL + b;
        std::lock_guard<std::mutex> lock(mu_);
        auto it = memo_.find(key);
        if (it == memo_.end()) it = memo_.emplace(key, measure(p, b)).first;
        if (!std::isfinite(it->second)) return false;
        out = it->second;
        return true;
    }

    // Called under mu_.
    double measure(const double* p, int b) const {
        const gp_Pnt pnt(p[0], p[1], p[2]);
        const Body& body = bodies_[static_cast<std::size_t>(b)];
        const TopoDS_Vertex vertex = BRepBuilderAPI_MakeVertex(pnt);
        double best = std::numeric_limits<double>::quiet_NaN();
        for (const Facet& facet : body.faces) {
            if (facet.box.IsOut(pnt)) continue;
            BRepExtrema_DistShapeShape dss(vertex, facet.face,
                                           Extrema_ExtFlag_MIN);
            if (!dss.IsDone() || dss.NbSolution() < 1) continue;
            const double d = dss.Value();
            if (!std::isfinite(d) || d > reach_) continue;
            if (!std::isfinite(best) || d < best) best = d;
        }
        if (!std::isfinite(best)) return best;
        body.classifier->Perform(pnt, Precision::Confusion());
        return body.classifier->State() == TopAbs_IN ? -best : best;
    }

    int owner(int node, const double* p) const {
        int best = -1;
        double best_d = std::numeric_limits<double>::infinity();
        for (std::size_t b = 0; b < bodies_.size(); ++b) {
            double d = 0.0;
            if (!signed_gap(node, p, static_cast<int>(b), d)) continue;
            if (std::abs(d) < best_d) {
                best_d = std::abs(d);
                best = static_cast<int>(b);
            }
        }
        return best;
    }

    std::vector<Body> bodies_;
    double reach_ = 0.0;
    mutable std::mutex mu_;
    mutable std::unordered_map<long long, double> memo_;
};

}  // namespace

std::unique_ptr<ContactGapProbe> make_cad_gap_probe(const std::string& geometry_path,
                                                    double reach_mm, std::string& note) {
    note.clear();
    if (geometry_path.empty() || !(reach_mm > 0.0)) {
        note = "no geometry file to measure contact gaps against";
        return nullptr;
    }
    TopoDS_Shape shape;
    try {
        const std::string lower = [&] {
            std::string s = geometry_path;
            for (char& c : s) c = static_cast<char>(std::tolower(c));
            return s;
        }();
        const bool brep = lower.size() > 5 &&
                          lower.compare(lower.size() - 5, 5, ".brep") == 0;
        if (brep) {
            BRep_Builder builder;
            if (!BRepTools::Read(shape, geometry_path.c_str(), builder))
                shape = TopoDS_Shape();
        } else {
            STEPControl_Reader reader;
            if (reader.ReadFile(geometry_path.c_str()) == IFSelect_RetDone) {
                reader.TransferRoots();
                shape = reader.OneShape();
            }
        }
    } catch (const Standard_Failure&) {
        shape = TopoDS_Shape();
    } catch (const std::exception&) {
        shape = TopoDS_Shape();
    }
    if (shape.IsNull()) {
        note = "contact gaps stayed on the mesh facets: the study geometry could not "
               "be re-read for an analytic measurement";
        return nullptr;
    }
    std::vector<TopoDS_Solid> solids;
    for (TopExp_Explorer ex(shape, TopAbs_SOLID); ex.More(); ex.Next())
        solids.push_back(TopoDS::Solid(ex.Current()));
    if (solids.size() < 2) {
        note = "contact gaps stayed on the mesh facets: the study geometry carries " +
               std::to_string(solids.size()) +
               " solid(s), so there is no second body's face to measure against";
        return nullptr;
    }
    auto probe = std::make_unique<CadGapProbe>(std::move(solids), reach_mm);
    if (probe->bodies() < 2) {
        note = "contact gaps stayed on the mesh facets: fewer than two of the study "
               "geometry's solids carry faces";
        return nullptr;
    }
    note = "contact clearances measured on the CAD faces of " +
           std::to_string(probe->bodies()) +
           " solid(s), not on the mesh facets (a linear facet of a curved fit "
           "understates the clearance by up to the whole of it)";
    return probe;
}

}  // namespace detail

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

// The label a refusal can hand back to the caller. A mesh ordinal is useless to
// act on and, worse, MISLEADING: the MCP layer expands one face query into as
// many facesets as it matched, so `fixtures[3]` in the study is not fixtures[3]
// in the request. `selector` is the original query text stamped on the faceset by
// cad_sim_tools, `sid` the face id; the positional form is the last resort for a
// hand-written study.
std::string selector_label(const nlohmann::json& fs, const char* arg,
                           std::size_t ordinal) {
    const std::string positional =
        std::string(arg) + "[" + std::to_string(ordinal) + "]";
    if (!fs.is_object()) return positional;
    for (const char* key : {"selector", "sid"})
        if (fs.contains(key) && fs[key].is_string() &&
            !fs[key].get<std::string>().empty())
            return fs[key].get<std::string>() + " (" + positional + ")";
    return positional;
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
    // ── material.plasticity ──────────────────────────────────────────────────
    // Validated at the study gate (not at the solver) so a malformed elastic-plastic
    // request costs no mesh. yield_mpa defaults to the material's own yield strength,
    // which is the number summary.min_fos is already measured against.
    if (material_input.contains("plasticity") &&
        !material_input["plasticity"].is_null()) {
        const json& pl = material_input["plasticity"];
        if (!pl.is_object())
            return json{{"ok", false},
                        {"error", "fea_static: material.plasticity must be an object "
                                  "{yield_mpa, hardening_mpa, max_increments, tol}"}};
        for (auto it = pl.begin(); it != pl.end(); ++it) {
            static const std::set<std::string> known = {
                "enabled", "yield_mpa", "hardening_mpa", "max_increments", "tol"};
            if (!known.count(it.key()))
                return json{{"ok", false},
                            {"error", "fea_static: material.plasticity has unsupported "
                                      "key '" + it.key() +
                                          "' — refusing to ignore it. Supported: "
                                          "enabled, yield_mpa, hardening_mpa, "
                                          "max_increments, tol"}};
        }
        const double pl_yield = pl.contains("yield_mpa") && pl["yield_mpa"].is_number()
                                    ? pl["yield_mpa"].get<double>()
                                    : yield_input;
        const double pl_hard = pl.contains("hardening_mpa")
                                   ? (pl["hardening_mpa"].is_number()
                                          ? pl["hardening_mpa"].get<double>()
                                          : std::numeric_limits<double>::quiet_NaN())
                                   : 0.0;
        const double pl_tol =
            pl.contains("tol") ? (pl["tol"].is_number()
                                      ? pl["tol"].get<double>()
                                      : std::numeric_limits<double>::quiet_NaN())
                               : 1e-3;
        if (!std::isfinite(pl_yield) || !(pl_yield > 0.0))
            return json{{"ok", false},
                        {"error", "fea_static: material.plasticity.yield_mpa must be "
                                  "finite and > 0 (omit it to use material.yield_mpa)"}};
        if (!std::isfinite(pl_hard) || pl_hard < 0.0)
            return json{{"ok", false},
                        {"error", "fea_static: material.plasticity.hardening_mpa is a "
                                  "TANGENT MODULUS in MPa (not a ratio of E) and must "
                                  "be finite and >= 0; 0 means perfectly plastic"}};
        if (pl_hard >= E_input)
            return json{{"ok", false},
                        {"error", "fea_static: material.plasticity.hardening_mpa (" +
                                      std::to_string(pl_hard) +
                                      " MPa) is at or above Young's modulus (" +
                                      std::to_string(E_input) +
                                      " MPa), so the post-yield branch would be stiffer "
                                      "than the elastic one. Real metals sit near "
                                      "E/100 to E/20."}};
        if (pl.contains("max_increments")) {
            if (!pl["max_increments"].is_number_integer())
                return json{{"ok", false},
                            {"error", "fea_static: material.plasticity.max_increments "
                                      "must be an integer from 1 through 1000"}};
            const int inc = pl["max_increments"].get<int>();
            if (inc < 1 || inc > 1000)
                return json{{"ok", false},
                            {"error", "fea_static: material.plasticity.max_increments "
                                      "must be an integer from 1 through 1000"}};
        }
        if (!std::isfinite(pl_tol) || !(pl_tol > 0.0) || !(pl_tol < 1.0))
            return json{{"ok", false},
                        {"error", "fea_static: material.plasticity.tol is a RELATIVE "
                                  "residual tolerance (||out-of-balance|| / ||applied "
                                  "load||) and must be in (0, 1)"}};
        if (study.contains("adapt") && !study["adapt"].is_null())
            return json{{"ok", false},
                        {"error", "fea_static: material.plasticity cannot be combined "
                                  "with an adaptive (error-estimator) run — the ZZ "
                                  "estimator is derived for linear elasticity and its "
                                  "indicator is meaningless once the material yields. "
                                  "Converge the mesh elastically, then re-solve that "
                                  "mesh with plasticity at an explicit mesh_size_mm."}};
    }
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
    // Elastic-plastic law (validated at the study gate above). An absent or
    // explicitly disabled `plasticity` block leaves this default-constructed, and the
    // solve is the linear-elastic one it has always been.
    chudware::sim::FeaPlasticity plasticity;
    if (material.contains("plasticity") && material["plasticity"].is_object()) {
        const json& pl = material["plasticity"];
        plasticity.enabled = pl.value("enabled", true);
        plasticity.yield_mpa = pl.value("yield_mpa", yield_mpa);
        plasticity.hardening_mpa = pl.value("hardening_mpa", 0.0);
        plasticity.max_increments = pl.value("max_increments", 10);
        plasticity.tol = pl.value("tol", 1e-3);
    }
    const bool plastic_enabled = plasticity.enabled;

    std::vector<std::string> notes;
    if (!mesh_note.empty()) notes.push_back("mesh: " + mesh_note);

    const auto t_setup0 = std::chrono::steady_clock::now();  // BC/load resolution phase
    // ── A selection that spans the contact interface (sim/fea_selection.h) ────
    // The check needs each selector's OWN node set, which the union in
    // `fixed_nodes` and the in-place force deposition below both destroy. Keeping
    // them is only worth it when the study DECLARED contact: an oracle-style study
    // carries hundreds of single-node loads, and none of them can span an
    // interface that was never declared.
    const bool contact_declared = study.contains("contact") &&
                                  study["contact"].is_object() &&
                                  study["contact"].value("enabled", true);
    // Label + resolved nodes per load selector, in study order. Only populated
    // while `contact_declared`.
    std::vector<std::pair<std::string, std::vector<int>>> load_selections;
    // ── Force vector (replicate run_static's load loop EXACTLY) ──────────────
    std::vector<double> nodal_forces(static_cast<size_t>(n_dof), 0.0);
    std::size_t loads_seen = 0;
    if (study.contains("loads") && study["loads"].is_array()) {
        for (const auto& load : study["loads"]) {
            const std::size_t load_ordinal = loads_seen++;
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
            if (contact_declared)
                load_selections.emplace_back(
                    selector_label(faces, "loads", load_ordinal), node_ids);
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
    // Each fixture's OWN resolved nodes, in study order — the union above cannot
    // answer "did THIS selector span the interface" (first fixture wins a shared
    // node). Only populated while `contact_declared`.
    std::vector<std::vector<int>> fixture_selections;
    if (study.contains("fixtures") && study["fixtures"].is_array()) {
        std::unordered_set<int> seen;
        for (const auto& fx : study["fixtures"]) {
            const double tol = fx.value("tol_mm", 1.0);
            const std::size_t ordinal = fixture_labels.size();
            const std::string label = selector_label(fx, "fixtures", ordinal);
            fixture_labels.push_back(label);
            std::vector<int> node_ids =
                boundary_faceset_membership(fx, points, tets, nodes_per_elem);
            if (contact_declared) fixture_selections.push_back(node_ids);
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
    // {"enabled":true, "max_gap_mm":0.5, "max_iterations":25, "penalty_scale":1.0,
    //  "interface_size_mm":0.35}
    // Contact and bond_gap_mm are two DIFFERENT idealizations of the same interface —
    // one carries tension, one cannot — so asking for both is a contradiction, not a
    // merge, and it is refused rather than silently resolved.
    bool contact_enabled = false;
    ContactCandidateOptions contact_opts;
    FeaContactOptions contact_solver_opts;
    double contact_interface_size = 0.0;
    ContactResolution contact_resolution;
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
            // interface_size_mm is a MESH contract, applied back in resolve_mesh
            // (local refinement on the candidate faces). It is validated here so
            // a malformed value refuses the study instead of being ignored by a
            // pass that only ever asks for more resolution.
            contact_interface_size = number("interface_size_mm", 0.0, 1e-4, 1e4);
            if (!std::isfinite(contact_interface_size))
                return json{
                    {"ok", false},
                    {"error", "fea_static: study.contact.interface_size_mm must be a "
                              "finite element size in mm (0.0001..10000) — it refines "
                              "the contact faces locally so the clearance is resolved "
                              "without meshing the whole part that fine"}};
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
            // ── The clearance a fit has is a CAD fact, so measure it on the CAD ──
            // The pairing stays a mesh question; the GAP does not. A candidate is
            // two boundary nodes, so its facet gap is r_bore*cos(dtheta) - r_pin
            // for whatever dtheta the two meshes sampled — on the ME 490 hinge
            // saddle that read 0.03665 mm of a real 0.300 mm clearance and made
            // the resolution guard unsatisfiable at every mesh size
            // (fea_contact.h ContactGapProbe). The probe outlives the candidate
            // build below and nothing else reads it.
            std::unique_ptr<ContactGapProbe> gap_probe;
            if (const std::string geometry = study.value("step_path", std::string());
                !geometry.empty()) {
                // Search reach: the declared clearance twice over, plus one mesh
                // cell, so a boundary node left off its own true surface by the
                // fill's snap residual still finds the faces it sits between.
                double h_sum = 0.0;
                int h_n = 0;
                const int n_elems = static_cast<int>(tets.size()) / nodes_per_elem;
                const int h_step = std::max(1, n_elems / 1000);
                for (int e = 0; e < n_elems; e += h_step) {
                    const int a = tets[static_cast<size_t>(e) * nodes_per_elem];
                    const int b = tets[static_cast<size_t>(e) * nodes_per_elem + 1];
                    const double dx = points[static_cast<size_t>(a) * 3] -
                                      points[static_cast<size_t>(b) * 3];
                    const double dy = points[static_cast<size_t>(a) * 3 + 1] -
                                      points[static_cast<size_t>(b) * 3 + 1];
                    const double dz = points[static_cast<size_t>(a) * 3 + 2] -
                                      points[static_cast<size_t>(b) * 3 + 2];
                    h_sum += std::sqrt(dx * dx + dy * dy + dz * dz);
                    ++h_n;
                }
                const double h_mesh = (h_n > 0 && h_sum > 0.0) ? h_sum / h_n : 1.0;
                std::string probe_note;
                gap_probe = detail::make_cad_gap_probe(
                    geometry, 2.0 * contact_opts.max_gap_mm + h_mesh, probe_note);
                if (!probe_note.empty()) notes.push_back("contact: " + probe_note);
                contact_opts.gap_probe = gap_probe.get();
            }
            contact_set = fea_contact_candidates(points, tets, nodes_per_elem, E_mpa,
                                                 contact_opts);
            contact_opts.gap_probe = nullptr;  // consumed; nothing below reads it
            if (contact_set.gap_beyond_max_pairs > 0)
                notes.push_back(
                    std::to_string(contact_set.gap_beyond_max_pairs) +
                    " candidate pair(s) the mesh facets placed within max_gap_mm are "
                    "further apart than that on the CAD faces and were dropped");
            if (contact_set.gap_fallback_nodes > 0)
                notes.push_back(
                    std::to_string(contact_set.gap_fallback_nodes) +
                    " interface node(s) had no CAD face to measure against (repaired "
                    "or dissolved mesh region); their pairs kept the facet gap");
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
            // ── A selection must not SPAN the interface (sim/fea_selection.h) ──
            // Decided the moment both halves are known — the resolved BC nodes and
            // the candidate set that says which bodies are in contact — and BEFORE
            // the resolution guard, because this is an ill-posed STUDY and not an
            // under-resolved mesh: refining the interface of a study whose fixture
            // holds both sides of it just buys a finer non-answer.
            for (std::size_t i = 0; i < fixture_selections.size(); ++i) {
                // fixture_labels is pushed before fixture_selections in the same
                // iteration, so the two are 1:1 — read once and use one value.
                const std::string& label = fixture_labels[i];
                const SelectionSpan span = selection_spans_contact(
                    "fixture", label, fixture_selections[i], contact_set);
                if (!span.spans) continue;
                return json{
                    {"ok", false},
                    {"error", "fea_static: " + span.message},
                    {"selection_spans_contact",
                     json{{"kind", "fixture"},
                          {"selector", label},
                          {"bodies", json::array({span.body_a, span.body_b})},
                          {"nodes_captured",
                           json::array({span.captured_a, span.captured_b})},
                          {"body_nodes",
                           json::array({span.body_a_nodes, span.body_b_nodes})},
                          {"median_gap_mm", round_nd(contact_set.median_gap0_mm, 6)},
                          {"min_gap_mm", round_nd(contact_set.min_gap0_mm, 6)}}},
                    {"notes", notes}};
            }
            for (const auto& sel : load_selections) {
                const SelectionSpan span = selection_spans_contact(
                    "load", sel.first, sel.second, contact_set);
                if (!span.spans) continue;
                return json{
                    {"ok", false},
                    {"error", "fea_static: " + span.message},
                    {"selection_spans_contact",
                     json{{"kind", "load"},
                          {"selector", sel.first},
                          {"bodies", json::array({span.body_a, span.body_b})},
                          {"nodes_captured",
                           json::array({span.captured_a, span.captured_b})},
                          {"body_nodes",
                           json::array({span.body_a_nodes, span.body_b_nodes})},
                          {"median_gap_mm", round_nd(contact_set.median_gap0_mm, 6)},
                          {"min_gap_mm", round_nd(contact_set.min_gap0_mm, 6)}}},
                    {"notes", notes}};
            }
            // The active set STARTS at the pairs this mesh cannot tell from touching
            // (fea_contact.h kSeatFractionH); the loop seats the rest as the bodies
            // deform. Handing the solver every candidate instead made the seated
            // population run-to-run mesh luck on a nominally-touching fit.
            contact_solver_opts.seat_tolerance_mm = contact_set.seat_limit_mm;
            // Is this mesh even capable of describing the interface? The a-priori
            // clause (fea_contact.h ContactResolution) is decided here, BEFORE the
            // active-set solve: a solve cannot rescue an interface the mesh cannot
            // represent, and it would return a "converged" answer with a 0.0%
            // equilibrium error to dress up the artefact. The refusal carries the
            // discretization facts, which are the whole evidence for it.
            contact_resolution = fea_contact_resolution(contact_set);
            {
                char text[448];
                if (contact_resolution.criterion == kCriterionPatchArea)
                    std::snprintf(
                        text, sizeof(text),
                        "contact interface element size %.4g mm on a %.4g mm^2 "
                        "candidate patch (the faces TOUCH — no clearance to resolve, "
                        "so resolution is judged as %.0f elements across the patch, "
                        "i.e. %.4g mm or finer); %d of %d candidate pair(s) seat "
                        "within the %.4g mm seat limit",
                        contact_resolution.h_interface_mm,
                        contact_resolution.candidate_area_mm2, kContactMinPatchElems,
                        contact_resolution.max_h_interface_mm,
                        contact_resolution.seatable_pairs,
                        contact_resolution.candidate_pairs,
                        contact_resolution.seat_limit_mm);
                else
                    std::snprintf(
                        text, sizeof(text),
                        "contact interface element size %.4g mm against a %.4g mm "
                        "closest approach on the %s (ratio %.1f; the mesh facets read "
                        "%.4g mm); %d of %d candidate pair(s) seat within the %.4g mm "
                        "seat limit",
                        contact_resolution.h_interface_mm, contact_resolution.gap_mm,
                        contact_resolution.gap_analytic ? "CAD faces" : "mesh facets",
                        contact_resolution.ratio, contact_resolution.gap_facet_mm,
                        contact_resolution.seatable_pairs,
                        contact_resolution.candidate_pairs,
                        contact_resolution.seat_limit_mm);
                notes.push_back(text);
            }
            if (contact_set.initial_penetration_max_mm > 0.0) {
                char pen[608];
                std::snprintf(
                    pen, sizeof(pen),
                    "contact: candidate pairs sit up to %.4g mm INSIDE each other, "
                    "measured on the %s — %s. It is CLAMPED to a touching gap, so no "
                    "k*penetration preload enters the solve (unclamped it was 56 kN "
                    "through 11 pairs on the case that found this, reported as "
                    "3.0e6 MPa of contact pressure for a 1500 N load)",
                    contact_set.initial_penetration_max_mm,
                    contact_resolution.gap_analytic ? "CAD faces" : "mesh facets",
                    contact_resolution.gap_analytic
                        ? "so the two solids really do interfere, which is a "
                          "modelling defect no contact idealization can absorb"
                        : "which on a curved or nominally-touching fit is the "
                          "discretization's own surface error, not a press fit");
                notes.push_back(pen);
            }
            if (!contact_resolution.resolved)
                return json{
                    {"ok", false},
                    {"error", "fea_static: " + contact_resolution.reason},
                    {"contact",
                     json{{"enabled", true},
                          {"max_gap_mm", contact_opts.max_gap_mm},
                          {"interface_size_mm", contact_interface_size},
                          {"candidate_pairs",
                           static_cast<int>(contact_set.springs.size())},
                          {"candidate_area_mm2",
                           round_nd(contact_set.candidate_area_mm2, 6)},
                          {"contact_pressure_reliable", false},
                          {"resolution", contact_resolution_json(contact_resolution)}}},
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
            // ── Name the island, don't just count it ───────────────────────
            // "3 connected component(s) with no fixture" told a reader nothing
            // they could act on: not WHICH body floated, and not whether it was
            // 1 mm from the structure (a missing weld / an undeclared contact)
            // or 200 mm away (the wrong bodies were isolated). Both are
            // knowable here — the mesher records which STEP solid ordinals went
            // into each meshed body (mesher.h MeshOut::Body), and the distance
            // to the nearest FIXTURED body is a min over nodes. This runs only
            // on the refusal path, so the O(n) sweep costs nothing that matters.
            struct Island {
                std::vector<int> ordinals;
                int nodes = 0;
                double gap_mm = std::numeric_limits<double>::infinity();
                std::vector<int> nearest_ordinals;
            };
            std::unordered_map<int, Island> islands;
            islands.reserve(floating.size());
            const auto body_ordinals_of = [&](int node) {
                for (const auto& body : mesh_quality.bodies)
                    if (node >= body.node_begin && node < body.node_end)
                        return body.source_ordinals;
                return std::vector<int>{};
            };
            std::vector<int> island_nodes;
            for (int i = 0; i < n_nodes_i; ++i) {
                if (!referenced[static_cast<size_t>(i)]) continue;
                const int root = sfind(i);
                if (!floating.count(root)) continue;
                Island& island = islands[root];
                if (island.ordinals.empty()) island.ordinals = body_ordinals_of(i);
                ++island.nodes;
                island_nodes.push_back(i);
            }
            // One pass over the anchored nodes against every island node: the
            // islands are what is small here (they are why the study is being
            // refused), so this is |anchored| x |island nodes| with no build-up.
            for (int j = 0; j < n_nodes_i; ++j) {
                if (!referenced[static_cast<size_t>(j)]) continue;
                const int root = sfind(j);
                if (floating.count(root)) continue; // another island, not a fixture
                const double jx = points[static_cast<size_t>(j) * 3];
                const double jy = points[static_cast<size_t>(j) * 3 + 1];
                const double jz = points[static_cast<size_t>(j) * 3 + 2];
                for (int i : island_nodes) {
                    const double dx = points[static_cast<size_t>(i) * 3] - jx;
                    const double dy = points[static_cast<size_t>(i) * 3 + 1] - jy;
                    const double dz = points[static_cast<size_t>(i) * 3 + 2] - jz;
                    const double d = std::sqrt(dx * dx + dy * dy + dz * dz);
                    Island& island = islands[sfind(i)];
                    if (d < island.gap_mm) {
                        island.gap_mm = d;
                        island.nearest_ordinals = body_ordinals_of(j);
                    }
                }
            }
            const auto ordinal_text = [](const std::vector<int>& ordinals) {
                if (ordinals.empty()) return std::string("unattributed");
                std::string text;
                for (std::size_t k = 0; k < ordinals.size(); ++k) {
                    if (k) text += "+";
                    text += std::to_string(ordinals[k]);
                }
                return text;
            };
            std::string message =
                "fea_static: mesh has " + std::to_string(floating.size()) +
                " connected component(s) with no fixture: rigid-body motion";
            json island_json = json::array();
            int shown = 0;
            for (const auto& entry : islands) {
                const Island& island = entry.second;
                json record{{"body_ordinals", island.ordinals},
                            {"nodes", island.nodes}};
                if (std::isfinite(island.gap_mm)) {
                    record["gap_to_nearest_fixed_body_mm"] =
                        round_nd(island.gap_mm, 6);
                    record["nearest_fixed_body_ordinals"] =
                        island.nearest_ordinals;
                }
                island_json.push_back(std::move(record));
                if (shown++ >= 4) continue; // the text names the first five
                message += shown == 1 ? ". Island(s): " : "; ";
                message += "body " + ordinal_text(island.ordinals) + " (" +
                           std::to_string(island.nodes) + " node(s))";
                if (std::isfinite(island.gap_mm)) {
                    char gap[160];
                    std::snprintf(gap, sizeof(gap),
                                  " sits %.4g mm from the nearest fixtured body "
                                  "(body %s)",
                                  island.gap_mm,
                                  ordinal_text(island.nearest_ordinals).c_str());
                    message += gap;
                }
            }
            message +=
                ". Remedies, in order: weld/union the bodies in CAD if that gap "
                "is not real (solids closer than 1e-3 mm are fused for meshing "
                "automatically), declare the interface with `contact` "
                "(max_gap_mm above the gap) or `bond_gap_mm`, fixture the island, "
                "or re-isolate the study without it";
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
                {"unfixed_islands", std::move(island_json)},
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
    chudware::sim::FeaPlasticReport plastic_report;
    // Three routes, ONE kernel: the plastic one subsumes contact (its active-set loop
    // runs inside each load increment), so an elastic-plastic contact study is a
    // single call and not a contact solve with a material pass bolted on.
    FeaStaticResult r =
        plastic_enabled
            ? fea_static_solve_plastic(
                  points, tets, E_mpa, nu, yield_mpa, fixed_nodes, nodal_forces,
                  nodes_per_elem, bond_springs,
                  contact_enabled ? contact_set.springs
                                  : std::vector<chudware::sim::ContactSpring>{},
                  contact_solver_opts, plasticity, contact_report, plastic_report,
                  job.token())
            : (contact_enabled
                   ? fea_static_solve_contact(points, tets, E_mpa, nu, yield_mpa,
                                              fixed_nodes, nodal_forces, nodes_per_elem,
                                              bond_springs, contact_set.springs,
                                              contact_solver_opts, contact_report,
                                              job.token())
                   : fea_static_solve(points, tets, E_mpa, nu, yield_mpa, fixed_nodes,
                                      nodal_forces, nodes_per_elem, bond_springs,
                                      job.token()));
    // The contact block is EVIDENCE, so it rides on the refusal too: a reader has to be
    // able to see how many pairs there were and how far the active set got.
    //
    // The active set is also what settles whether a PRESSURE was measured or
    // invented: `contact_area_mm2` is the summed tributary area of the pairs that
    // actually carried load, `mean_contact_pressure_mpa` is the resultant over it,
    // and `contact_pressure_reliable` is false whenever that patch is too small a
    // sample of the interface to be one (fea_contact.h ContactResolution). A single
    // active pair produces a "pressure" of force/one node area — 13,543 MPa on the
    // live keyway that motivated this — and no other field in the packet says so.
    auto contact_json = [&]() {
        double total_force = 0.0;
        double total_area = 0.0;
        for (const FeaContactGroup& g : contact_report.groups) {
            total_force += g.contact_force_n;
            total_area += g.contact_area_mm2;
        }
        json body = {{"enabled", true},
                     {"max_gap_mm", contact_opts.max_gap_mm},
                     {"penalty_scale", contact_opts.penalty_scale},
                     {"interface_size_mm", contact_interface_size},
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
                     {"candidate_area_mm2", round_nd(contact_set.candidate_area_mm2, 6)},
                     {"contact_area_mm2", round_nd(total_area, 6)},
                     {"mean_contact_pressure_mpa",
                      round_nd(total_area > 0.0 ? total_force / total_area : 0.0, 4)},
                     {"contact_pressure_reliable", contact_resolution.pressure_reliable},
                     {"resolution", contact_resolution_json(contact_resolution)},
                     {"held_by_contact_only", held_by_contact_only},
                     {"contact_carries_load", contact_resolution.contact_carries_load},
                     // The load path, as three numbers a reader can check: what was
                     // applied to the bodies this interface is the only support for,
                     // what crossed it, and their quotient. `note` is empty exactly
                     // when the interface IS the load path.
                     {"load_path",
                      json{{"applied_n", round_nd(contact_resolution.applied_load_n, 4)},
                           {"carried_n", round_nd(contact_resolution.carried_load_n, 4)},
                           {"fraction", round_nd(contact_resolution.load_fraction, 6)},
                           {"note", contact_resolution.load_path_note}}},
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
                     {"contact_area_mm2", round_nd(g.contact_area_mm2, 6)},
                     {"mean_contact_pressure_mpa",
                      round_nd(g.contact_area_mm2 > 0.0
                                   ? g.contact_force_n / g.contact_area_mm2
                                   : 0.0,
                               4)},
                     {"max_contact_pressure_mpa", round_nd(g.max_contact_pressure_mpa, 4)},
                     {"max_penetration_mm", round_nd(g.max_penetration_mm, 6)}});
        }
        return body;
    };
    if (contact_enabled) {
        double total_force = 0.0;
        double total_area = 0.0;
        for (const FeaContactGroup& g : contact_report.groups) {
            total_force += g.contact_force_n;
            total_area += g.contact_area_mm2;
        }
        fea_contact_resolution_after_solve(contact_resolution,
                                           contact_report.active_pairs, total_force,
                                           total_area);
        // ── Is this contact the LOAD PATH, or just a parallel spring? ──────────
        // A resolved, well-seated patch can still answer nothing. The live keyway
        // isolate fixtured BOTH sides of a 0.35 mm radial clearance (one face set
        // with a cylinder filter reached across it onto the mating bore), so six
        // active pairs carried 9.9 N of a 1500 N applied load and the packet
        // called that a contact pressure. The resultant to compare against is the
        // one applied to the components held ONLY by contact — the bodies whose
        // entire stiffness path to a fixture is this interface.
        const std::unordered_set<int> held(held_by_contact_only.begin(),
                                           held_by_contact_only.end());
        std::array<double, 3> applied{0.0, 0.0, 0.0};
        const size_t n_comp = contact_set.component.size();
        for (size_t i = 0; i * 3 + 2 < nodal_forces.size(); ++i) {
            const int comp_id = i < n_comp ? contact_set.component[i] : -1;
            if (comp_id < 0 || held.find(comp_id) == held.end()) continue;
            for (size_t d = 0; d < 3; ++d) applied[d] += nodal_forces[i * 3 + d];
        }
        const double applied_n = std::sqrt(applied[0] * applied[0] +
                                           applied[1] * applied[1] +
                                           applied[2] * applied[2]);
        fea_contact_resolution_load_path(contact_resolution, applied_n, total_force,
                                         !held_by_contact_only.empty());
        if (!contact_resolution.load_path_note.empty())
            notes.push_back("contact load path: " + contact_resolution.load_path_note);
    }
    // The plastic block is EVIDENCE on the same terms the contact block is: a
    // collapse refusal is only actionable WITH the load fraction it reached and how
    // far the material got, so it rides on the refusal too.
    auto plasticity_json = [&]() {
        return json{
            {"enabled", true},
            {"yield_mpa", round_nd(plasticity.yield_mpa, 4)},
            {"hardening_mpa", round_nd(plasticity.hardening_mpa, 4)},
            {"tol", plasticity.tol},
            {"requested_increments", plasticity.max_increments},
            {"increments", plastic_report.increments},
            {"newton_iterations_total", plastic_report.newton_iterations_total},
            {"converged", plastic_report.converged},
            {"load_fraction", round_nd(plastic_report.load_fraction, 6)},
            {"max_equivalent_plastic_strain",
             round_nd(plastic_report.max_equivalent_plastic_strain, 8)},
            {"plastic_volume_mm3", round_nd(plastic_report.plastic_volume_mm3, 6)},
            {"plastic_fraction", round_nd(plastic_report.plastic_fraction, 6)},
            {"yielded_nodes", plastic_report.yielded_nodes}};
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
        if (plastic_enabled) fail["plasticity"] = plasticity_json();
        return fail;
    }
    // A non-converged active set is not a contact solution: the displacements belong to
    // whichever set the loop happened to stop on, so the stresses are not the ones the
    // touching state implies. Report the evidence, refuse the verdict.
    if (contact_enabled && !contact_report.converged) {
        json fail{
            {"ok", false},
            {"error", plastic_enabled
                          ? std::string(
                                "fea_static: the contact active set did not settle "
                                "inside a load increment, so no state is the solution. "
                                "Reduce contact.penalty_scale (a very stiff penalty "
                                "makes pairs chatter across the gap), resolve the "
                                "interface (contact.interface_size_mm), or raise "
                                "material.plasticity.max_increments so each increment "
                                "moves the seat less.")
                          : "fea_static: the contact active set did not settle in " +
                                std::to_string(contact_solver_opts.max_iterations) +
                                " iteration(s) — pairs kept opening and closing, so no "
                                "state is the solution. Raise contact.max_iterations, "
                                "or reduce contact.penalty_scale (a very stiff penalty "
                                "makes pairs chatter across the gap)."},
            {"contact", contact_json()},
            {"notes", notes}};
        if (plastic_enabled) fail["plasticity"] = plasticity_json();
        return fail;
    }
    // The load crossed the interface through too few pairs to be a contact patch:
    // a converged, equilibrium-clean, plausible-looking point load. This clause is
    // the one the live keyway needed — 1 active pair of 848 carrying 2902 N — and
    // it can only be checked here, after the active set is known.
    if (contact_enabled && !contact_resolution.resolved) {
        json fail{{"ok", false},
                  {"error", "fea_static: " + contact_resolution.reason},
                  {"contact", contact_json()},
                  {"notes", notes}};
        if (plastic_enabled) fail["plasticity"] = plasticity_json();
        return fail;
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
    // Plasticity rides beside them for the same reason: summary.max_von_mises_mpa is
    // still the stress, but with the law on it is CAPPED at the flow stress
    // (yield + hardening_mpa * max_equivalent_plastic_strain), and a reader comparing
    // it against an elastic run needs the plastic state to know why it moved.
    if (plastic_enabled) {
        out["plasticity"] = plasticity_json();
        if (static_cast<int>(r.equivalent_plastic_strain.size()) == r.n_nodes)
            out["equivalent_plastic_strain"] = r.equivalent_plastic_strain;
    }
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
    // ── Is the plastic strain still a SMALL strain? ──────────────────────────
    // The law is small-strain J2 on an undeformed mesh: strains add, the geometry
    // never updates, and no area reduction is accounted for. Past a few percent of
    // plastic strain that stops being an approximation and starts being the wrong
    // problem — a bearing edge at 20 % plastic strain has visibly changed shape, so
    // the patch the solve spread the load over is not the patch the part has. 5 % is
    // the conventional line, and it is stated where an analyst scanning a packet
    // cannot miss it rather than left implicit in the plasticity block.
    if (plastic_enabled && plastic_report.max_equivalent_plastic_strain > 0.05) {
        if (!out.contains("warnings") || !out["warnings"].is_array())
            out["warnings"] = json::array();
        out["warnings"].push_back(
            "plastic_strain_beyond_small_strain: peak equivalent plastic strain " +
            std::to_string(plastic_report.max_equivalent_plastic_strain) +
            " (" + std::to_string(100.0 *
                                  plastic_report.max_equivalent_plastic_strain) +
            " %) exceeds the 5 % small-strain validity limit of this formulation. The "
            "mesh is never updated and no area reduction is modelled, so the stress "
            "and the contact patch are both optimistic at this strain. Treat the "
            "result as a redistribution study, not a strength number: reduce the load, "
            "or take the geometry to a large-deformation solver. plasticity."
            "plastic_fraction (" +
            std::to_string(plastic_report.plastic_fraction) +
            ") says how much of the part is involved.");
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

// ── Plasticity is a STATIC law and belongs to no other physics ──────────────────
// A modal solve linearizes about an unstressed state and its eigenvalues are the
// elastic ones; a thermal solve has no stress at all; and buckling is an EIGENVALUE
// problem whose critical multiplier is defined only for a linear pre-buckling
// response. Accepting material.plasticity on any of them and silently ignoring it
// would report an elastic answer under a plastic label, which is the exact failure
// mode ME 490 O-28 was: a number that looks like the material's and is not.
static bool plasticity_requested(const nlohmann::json& study) {
    if (!study.is_object() || !study.contains("material") ||
        !study["material"].is_object())
        return false;
    const json& m = study["material"];
    if (!m.contains("plasticity") || !m["plasticity"].is_object()) return false;
    return m["plasticity"].value("enabled", true);
}

nlohmann::json fea_run_modal(const nlohmann::json& study, int n_modes) {
    try {
        SimJobScope job(study);
    if (!study.is_object())
        return json{{"ok", false}, {"error", "fea_modal: study must be an object"}};
    if (plasticity_requested(study))
        return json{{"ok", false},
                    {"error", "fea_modal: material.plasticity is not applicable to a "
                              "modal study. A modal solve linearizes about an "
                              "unstressed state, so its eigenvalues are the ELASTIC "
                              "ones however the material yields under load — the "
                              "plastic law would be silently ignored, and an elastic "
                              "answer under a plastic label is worse than a refusal. "
                              "Run the elastic-plastic study with sim_run_static, and "
                              "the modal study with the elastic material."}};
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
    if (plasticity_requested(study))
        return json{{"ok", false},
                    {"error", "fea_thermal: material.plasticity is not applicable to a "
                              "thermal study — a conduction solve has no stress for a "
                              "yield surface to bound. Take the temperature field to a "
                              "static study if you need thermal stress; plasticity "
                              "belongs there."}};
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
  // Whether the study declared its interfaces changes the MESH itself
  // (overlapping solids fused into one body versus kept separate), so it is
  // part of the key: serving the fused mesh to a contact study would hand it a
  // single component and no interface at all.
  hash =
      fnv1a(&options.declared_interfaces, sizeof options.declared_interfaces, hash);
  // The local refinement contract is part of the mesh, so it is part of the
  // key: the same STEP at the same fineness with a refined contact interface is
  // a DIFFERENT mesh, and serving the coarse one from cache would hand back
  // exactly the unresolved interface the request exists to avoid. The seed
  // POINTS are hashed too — they are where the refinement went.
  hash = fnv1a(&options.local_refine.size_mm, sizeof options.local_refine.size_mm,
               hash);
  hash = fnv1a(&options.local_refine.band_mm, sizeof options.local_refine.band_mm,
               hash);
  if (!options.local_refine.points.empty())
    hash = fnv1a(options.local_refine.points.data(),
                 options.local_refine.points.size() * sizeof(double), hash);
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
