// Document model implementation — see document.h for the design rationale (the
// simplified id-ref model vs. Python selectors, and the never-raise contract).
//
// The rebuild is a single linear pass over the feature list. Each feature reads
// its inputs from a results map keyed by feature id, compiles via the geom kernel,
// and stores its own result back under its id. Two result KINDS are tracked:
//   - SKETCH : a std::vector<TopoDS_Face> + the SketchPlane it was solved on
//              (so extrude/revolve can default the prism direction to the plane
//              normal). A sketch is NOT a body — it never tessellates on its own.
//   - SOLID  : a TopoDS_Shape body, the unit booleans/dress/extrude consume & make.
// A `consumed` set records solids that were merged/subtracted/replaced by a later
// feature (boolean inputs, the fillet/chamfer source) — those drop out of the
// surviving-body set, exactly as a SolidWorks feature tree shows only the current
// state of the solid.
//
// Every per-feature compile is wrapped in try/catch so an OCCT throw degrades to an
// error status, never a crash — the compile_document never-raises contract.

#include "doc/document.h"

#include <cmath>
#include <map>
#include <optional>
#include <set>
#include <string>
#include <variant>
#include <vector>

#include <Eigen/Dense>

#include <TopoDS_Face.hxx>
#include <TopoDS_Shape.hxx>

#include "geom/booleans.h"
#include "geom/dress.h"
#include "geom/features.h"
#include "geom/plane.h"
#include "geom/profile.h"
#include "geom/tessellate.h"
#include "geom/aicm.h"
#include "sketch/sketch.h"
#include "util/expr.h"

namespace aicad::doc {

using nlohmann::json;
using EnvMap = std::map<std::string, double>;

namespace {

constexpr double TWO_PI = 6.28318530717958647692;

// -------------------------------------------------------------------- result kinds
// A sketch result: the planar faces solved for the sketch, plus the plane they live
// on (downstream extrude/revolve default their direction to the plane normal).
struct SketchResult {
    std::vector<TopoDS_Face> faces;
    aicad::geom::SketchPlane plane;
};

// A solid-body result.
struct SolidResult {
    TopoDS_Shape shape;
};

using FeatureResult = std::variant<SketchResult, SolidResult>;

// -------------------------------------------------------------------- env parsing
// Flatten the env json object into name -> double. Non-numeric entries are skipped
// (forward-tolerant); a non-object env yields an empty map.
EnvMap parse_env(const json& env) {
    EnvMap m;
    if (!env.is_object()) return m;
    for (auto it = env.begin(); it != env.end(); ++it) {
        if (it.value().is_number()) m[it.key()] = it.value().get<double>();
    }
    return m;
}

// A numeric param that may be a raw number OR a string expression evaluated against
// `env` (mirrors the sketch solver's resolve_dim: a string is fed to the arithmetic
// evaluator, a number is taken verbatim). Throws on a bad expression / wrong type —
// the per-feature try/catch turns that into an error status.
double num_or_expr(const json& v, const EnvMap& env) {
    if (v.is_number()) return v.get<double>();
    if (v.is_string()) return aicad::util::evaluate(v.get<std::string>(), env);
    throw std::runtime_error("param is neither a number nor an expression string");
}

// Optional [x,y,z] -> Eigen::Vector3d. Returns nullopt when the key is absent or
// not a 3-array (caller supplies a default direction).
std::optional<Eigen::Vector3d> opt_vec3(const json& feat, const char* key) {
    auto it = feat.find(key);
    if (it == feat.end() || !it->is_array() || it->size() != 3) return std::nullopt;
    return Eigen::Vector3d((*it)[0].get<double>(), (*it)[1].get<double>(),
                           (*it)[2].get<double>());
}

// Required [x,y,z] -> Eigen::Vector3d. Throws (-> error status) when missing/malformed.
Eigen::Vector3d req_vec3(const json& feat, const char* key) {
    auto v = opt_vec3(feat, key);
    if (!v) throw std::runtime_error(std::string("missing/invalid vec3 param '") + key + "'");
    return *v;
}

// Map a plane name to a SketchPlane factory. Defaults to top() for an unknown/absent
// name (forward-tolerant), matching the most natural work plane.
aicad::geom::SketchPlane plane_from_name(const std::string& name) {
    if (name == "front") return aicad::geom::SketchPlane::front();
    if (name == "right") return aicad::geom::SketchPlane::right();
    return aicad::geom::SketchPlane::top();  // "top" or anything else
}

// Look up a referenced feature result by id, requiring it be a SOLID body. Throws a
// descriptive error (consumed by the per-feature try/catch) when the id is missing
// or refers to a sketch rather than a body.
const TopoDS_Shape& require_solid(const std::map<std::string, FeatureResult>& results,
                                  const std::string& ref_id, const char* slot) {
    auto it = results.find(ref_id);
    if (it == results.end())
        throw std::runtime_error(std::string("ref '") + slot + "' -> '" + ref_id +
                                 "' not found");
    if (!std::holds_alternative<SolidResult>(it->second))
        throw std::runtime_error(std::string("ref '") + slot + "' -> '" + ref_id +
                                 "' is a sketch, not a solid body");
    return std::get<SolidResult>(it->second).shape;
}

// Look up a referenced SKETCH result by id (for extrude/revolve profiles). Throws
// when missing or it refers to a solid body rather than a sketch.
const SketchResult& require_sketch(const std::map<std::string, FeatureResult>& results,
                                   const std::string& ref_id, const char* slot) {
    auto it = results.find(ref_id);
    if (it == results.end())
        throw std::runtime_error(std::string("profile '") + slot + "' -> '" + ref_id +
                                 "' not found");
    if (!std::holds_alternative<SketchResult>(it->second))
        throw std::runtime_error(std::string("profile '") + slot + "' -> '" + ref_id +
                                 "' is a solid body, not a sketch");
    return std::get<SketchResult>(it->second);
}

// Read a string field, with a default for an absent key. Throws if present but not a
// string (a malformed feature dict -> error status).
std::string str_field(const json& feat, const char* key, const std::string& dflt) {
    auto it = feat.find(key);
    if (it == feat.end()) return dflt;
    if (!it->is_string())
        throw std::runtime_error(std::string("field '") + key + "' must be a string");
    return it->get<std::string>();
}

// Required string ref id. Throws when absent/non-string.
std::string req_str(const json& feat, const char* key) {
    auto it = feat.find(key);
    if (it == feat.end() || !it->is_string())
        throw std::runtime_error(std::string("missing string ref '") + key + "'");
    return it->get<std::string>();
}

}  // namespace

// ----------------------------------------------------------------- compile_document
CompileResult compile_document(const json& doc, const json& env_json) {
    CompileResult result;
    const EnvMap env = parse_env(env_json);

    // The shared rebuild context: per-feature results keyed by id, plus the set of
    // solids consumed by a later feature (they no longer survive as standalone bodies).
    std::map<std::string, FeatureResult> results;
    std::set<std::string> consumed;
    // Surviving solid ids in first-produced order, so the final body_id assignment
    // (1..N) and AICM body order are deterministic across rebuilds.
    std::vector<std::string> solid_order;

    const json empty = json::array();
    const json& features = doc.contains("features") ? doc.at("features") : empty;

    for (const json& feat : features.is_array() ? features : empty) {
        const std::string id = feat.value("id", std::string("?"));
        const std::string type = feat.value("type", std::string(""));

        FeatureStatus st;
        st.id = id;
        st.type = type;

        // Per-feature compile wrapped so an OCCT/eval throw becomes an error status,
        // never a crash (compile_document never-raises contract).
        try {
            if (type == "sketch") {
                // Solve the 2D sketch, then build planar faces on the named plane.
                json sk;
                sk["entities"] = feat.value("entities", json::array());
                sk["constraints"] = feat.value("constraints", json::array());
                aicad::sketch::SketchSolution sol = aicad::sketch::solve_sketch(sk, env_json);
                if (!sol.converged)
                    throw std::runtime_error("sketch did not converge");

                aicad::geom::SketchPlane plane =
                    plane_from_name(str_field(feat, "plane", "top"));
                std::vector<TopoDS_Face> faces =
                    aicad::geom::faces_from_solved_sketch(sol.entities, plane);
                if (faces.empty())
                    throw std::runtime_error("sketch produced no closed-loop faces");

                results[id] = SketchResult{std::move(faces), plane};
                st.ok = true;

            } else if (type == "extrude") {
                const std::string prof = req_str(feat, "profile");
                const SketchResult& src = require_sketch(results, prof, prof.c_str());
                const double depth = num_or_expr(feat.at("depth"), env);
                if (!(depth > 0.0))
                    throw std::runtime_error("extrude depth must be > 0");
                const bool mid_plane = feat.value("mid_plane", false);
                // Default the prism direction to the source sketch plane's normal.
                Eigen::Vector3d dir =
                    opt_vec3(feat, "dir").value_or(src.plane.normal);

                // Extrude each region face; fuse multiple regions into one solid.
                TopoDS_Shape solid;
                for (const TopoDS_Face& f : src.faces) {
                    TopoDS_Shape prism = ::geom::extrude_face(f, dir, depth, mid_plane);
                    if (prism.IsNull())
                        throw std::runtime_error("extrude produced an empty solid");
                    solid = solid.IsNull() ? prism : ::geom::fuse(solid, prism);
                    if (solid.IsNull())
                        throw std::runtime_error("extrude region fuse failed");
                }
                if (solid.IsNull())
                    throw std::runtime_error("extrude produced no solid");

                results[id] = SolidResult{solid};
                solid_order.push_back(id);
                st.ok = true;

            } else if (type == "revolve") {
                const std::string prof = req_str(feat, "profile");
                const SketchResult& src = require_sketch(results, prof, prof.c_str());
                const Eigen::Vector3d axis_pt = req_vec3(feat, "axis_pt");
                const Eigen::Vector3d axis_dir = req_vec3(feat, "axis_dir");
                const double angle =
                    feat.contains("angle") ? num_or_expr(feat.at("angle"), env) : TWO_PI;
                if (!(std::abs(angle) > 0.0))
                    throw std::runtime_error("revolve angle must be non-zero");

                TopoDS_Shape solid;
                for (const TopoDS_Face& f : src.faces) {
                    TopoDS_Shape rev = ::geom::revolve_face(f, axis_pt, axis_dir, angle);
                    if (rev.IsNull())
                        throw std::runtime_error("revolve produced an empty solid");
                    solid = solid.IsNull() ? rev : ::geom::fuse(solid, rev);
                    if (solid.IsNull())
                        throw std::runtime_error("revolve region fuse failed");
                }
                if (solid.IsNull())
                    throw std::runtime_error("revolve produced no solid");

                results[id] = SolidResult{solid};
                solid_order.push_back(id);
                st.ok = true;

            } else if (type == "boolean") {
                const std::string op = req_str(feat, "op");
                const std::string a_id = req_str(feat, "a");
                const std::string b_id = req_str(feat, "b");
                const TopoDS_Shape& a = require_solid(results, a_id, "a");
                const TopoDS_Shape& b = require_solid(results, b_id, "b");

                TopoDS_Shape out;
                if (op == "fuse")        out = ::geom::fuse(a, b);
                else if (op == "cut")    out = ::geom::cut(a, b);
                else if (op == "common") out = ::geom::common(a, b);
                else throw std::runtime_error("unknown boolean op '" + op + "'");
                if (out.IsNull())
                    throw std::runtime_error("boolean '" + op + "' produced an empty shape");

                // a and b were merged/subtracted — they no longer survive standalone.
                consumed.insert(a_id);
                consumed.insert(b_id);

                results[id] = SolidResult{out};
                solid_order.push_back(id);
                st.ok = true;

            } else if (type == "fillet" || type == "chamfer") {
                const std::string body_id = req_str(feat, "body");
                const TopoDS_Shape& body = require_solid(results, body_id, "body");
                std::optional<Eigen::Vector3d> dir = opt_vec3(feat, "dir");

                TopoDS_Shape out;
                if (type == "fillet") {
                    const double radius = num_or_expr(feat.at("radius"), env);
                    if (!(radius > 0.0))
                        throw std::runtime_error("fillet radius must be > 0");
                    out = ::geom::fillet_edges(body, radius, dir);
                } else {
                    const double distance = num_or_expr(feat.at("distance"), env);
                    if (!(distance > 0.0))
                        throw std::runtime_error("chamfer distance must be > 0");
                    out = ::geom::chamfer_edges(body, distance, dir);
                }
                if (out.IsNull())
                    throw std::runtime_error(type + " produced an empty shape");

                // Dress-up REPLACES the source body (SolidWorks-style): the rounded /
                // beveled solid is the current state of that body in the tree.
                consumed.insert(body_id);

                results[id] = SolidResult{out};
                solid_order.push_back(id);
                st.ok = true;

            } else {
                // Forward-tolerant: an unknown feature type errors but the rebuild
                // continues (mirrors the Python's "no compiler for type" path).
                throw std::runtime_error("no compiler for feature type '" + type + "'");
            }
        } catch (const std::exception& ex) {
            st.ok = false;
            st.error = ex.what();
        } catch (...) {
            st.ok = false;
            st.error = "unknown error during feature compile";
        }

        result.statuses.push_back(std::move(st));
    }

    // -------------------------------------------------- tessellate surviving bodies
    // Surviving = solid results whose id was never consumed by a later feature, in
    // first-produced order. body_id runs 1..N; the feature id is the body name.
    std::vector<aicm::AicmBody> bodies;
    const float color[3] = {0.71f, 0.76f, 0.82f};  // default dielectric grey
    uint32_t body_num = 1;
    for (const std::string& sid : solid_order) {
        if (consumed.count(sid)) continue;
        const TopoDS_Shape& shape = std::get<SolidResult>(results.at(sid)).shape;
        if (shape.IsNull()) continue;
        try {
            bodies.push_back(::geom::tessellate_body(shape, body_num, sid, color));
            ++body_num;
        } catch (...) {
            // A tessellation failure must not crash the rebuild; skip this body.
        }
    }

    result.body_count = static_cast<int>(bodies.size());
    result.aicm = aicm::write_aicm(bodies);

    // ok iff every feature compiled cleanly.
    result.ok = true;
    for (const FeatureStatus& s : result.statuses)
        if (!s.ok) { result.ok = false; break; }

    return result;
}

}  // namespace aicad::doc
