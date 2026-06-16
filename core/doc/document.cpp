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

#include <BRepAdaptor_Surface.hxx>
#include <GeomAbs_SurfaceType.hxx>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Shape.hxx>
#include <gp_Dir.hxx>
#include <gp_Pln.hxx>
#include <gp_Pnt.hxx>

#include "doc/picks.h"
#include "geom/booleans.h"
#include "geom/dress.h"
#include "geom/features.h"
#include "geom/plane.h"
#include "geom/profile.h"
#include "geom/tessellate.h"
#include "geom/transform.h"
#include "geom/aicm.h"
#include "sketch/sketch.h"
#include "util/expr.h"

namespace aicad::doc {

using nlohmann::json;
using EnvMap = std::map<std::string, double>;

namespace {

constexpr double TWO_PI = 6.28318530717958647692;
constexpr double PI = 3.14159265358979323846;

// Degrees -> radians. The circular_pattern angle is in DEGREES (SolidWorks +
// pattern.py convention), so its per-step rotation is converted here before the
// kernel's rotate_copy (which, like revolve_face, takes radians).
inline double radians(double deg) { return deg * PI / 180.0; }

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

                // An "edges" key (a single selector object, or a non-empty json array of
                // selectors) routes to the edge-SELECTOR path: each selector is resolved
                // against the body's live B-rep topology (doc/picks::resolve_edge_picks).
                // A dangling selector throws (-> per-feature error status, the never-raise
                // contract). When "edges" is absent the legacy `dir`-filter path runs
                // unchanged.
                auto edges_it = feat.find("edges");
                const bool has_edge_sel =
                    edges_it != feat.end() &&
                    (edges_it->is_object() ||
                     (edges_it->is_array() && !edges_it->empty()));

                std::optional<std::vector<TopoDS_Edge>> picked;
                if (has_edge_sel) {
                    aicad::doc::EdgePicks picks =
                        aicad::doc::resolve_edge_picks(body, *edges_it);
                    if (!picks.dangling.empty()) {
                        std::string msg = type + ": " +
                            std::to_string(picks.dangling.size()) +
                            " edge selector(s) dangling: ";
                        for (std::size_t k = 0; k < picks.dangling.size(); ++k) {
                            if (k) msg += "; ";
                            msg += picks.dangling[k];
                        }
                        throw std::runtime_error(msg);
                    }
                    if (picks.edges.empty())
                        throw std::runtime_error(type + ": no edges resolved");
                    picked = std::move(picks.edges);
                }

                TopoDS_Shape out;
                if (type == "fillet") {
                    const double radius = num_or_expr(feat.at("radius"), env);
                    if (!(radius > 0.0))
                        throw std::runtime_error("fillet radius must be > 0");
                    out = picked ? ::geom::fillet_edges(body, radius, *picked)
                                 : ::geom::fillet_edges(body, radius, dir);
                } else {
                    const double distance = num_or_expr(feat.at("distance"), env);
                    if (!(distance > 0.0))
                        throw std::runtime_error("chamfer distance must be > 0");
                    out = picked ? ::geom::chamfer_edges(body, distance, *picked)
                                 : ::geom::chamfer_edges(body, distance, dir);
                }
                if (out.IsNull())
                    throw std::runtime_error(type + " produced an empty shape");

                // Dress-up REPLACES the source body (SolidWorks-style): the rounded /
                // beveled solid is the current state of that body in the tree.
                consumed.insert(body_id);

                results[id] = SolidResult{out};
                solid_order.push_back(id);
                st.ok = true;

            } else if (type == "linear_pattern") {
                // Replicate a seed solid on a 1-D row (or 2-D grid) and fuse the copies
                // into one body. Instance (0,0) IS the seed already present, so we only
                // place the additional cells and union each onto the running result —
                // the geometry-pattern strategy of pattern.py, in the simplified id-ref
                // model (no CompileContext / tool_bodies / skip / vary / bidirectional).
                const std::string seed_id = req_str(feat, "seed");
                const TopoDS_Shape& seed = require_solid(results, seed_id, "seed");

                const int count1 =
                    static_cast<int>(std::lround(num_or_expr(feat.at("count1"), env)));
                if (count1 < 1)
                    throw std::runtime_error("linear pattern count1 must be >= 1");
                const double spacing1 = num_or_expr(feat.at("spacing1"), env);
                Eigen::Vector3d dir1 = req_vec3(feat, "dir1");
                if (!(dir1.norm() > 1e-12))
                    throw std::runtime_error("linear pattern dir1 has zero length");
                dir1.normalize();

                // Optional second direction -> a 2-D grid. Absent dir2 keeps count2 = 1.
                const bool has_dir2 = feat.contains("dir2");
                int count2 = 1;
                double spacing2 = 0.0;
                Eigen::Vector3d dir2(0.0, 0.0, 0.0);
                if (has_dir2) {
                    count2 = feat.contains("count2")
                        ? static_cast<int>(std::lround(num_or_expr(feat.at("count2"), env)))
                        : 1;
                    if (count2 < 1)
                        throw std::runtime_error("linear pattern count2 must be >= 1");
                    spacing2 = feat.contains("spacing2")
                        ? num_or_expr(feat.at("spacing2"), env) : 0.0;
                    dir2 = req_vec3(feat, "dir2");
                    if (!(dir2.norm() > 1e-12))
                        throw std::runtime_error("linear pattern dir2 has zero length");
                    dir2.normalize();
                }

                if (count1 * count2 < 2)
                    throw std::runtime_error("linear pattern needs at least 2 instances");

                // Lay copies at i*spacing1*dir1 (+ j*spacing2*dir2). Instance (0,0) is
                // the seed, already in `result`. The result body keyed under THIS pattern
                // feature id is the owner the roadmap's "pattern copies inherit the
                // pattern feature's owner id" refers to in this simplified model.
                TopoDS_Shape res = seed;
                for (int i = 0; i < count1; ++i) {
                    for (int j = 0; j < count2; ++j) {
                        if (i == 0 && j == 0) continue;  // the seed instance
                        Eigen::Vector3d offset = dir1 * (i * spacing1);
                        if (has_dir2) offset += dir2 * (j * spacing2);
                        TopoDS_Shape copy = ::geom::translate_copy(seed, offset);
                        res = ::geom::fuse(res, copy);
                        if (res.IsNull())
                            throw std::runtime_error("linear pattern instance fuse failed");
                    }
                }

                // The seed is absorbed into the pattern result (a SW pattern owns its
                // instances), so it no longer survives as a standalone body.
                consumed.insert(seed_id);

                results[id] = SolidResult{res};
                solid_order.push_back(id);
                st.ok = true;

            } else if (type == "circular_pattern") {
                // Replicate a seed solid around an axis and fuse the copies into one
                // body. k=0 is the seed (already present); copies k=1..count-1 are
                // rotated by step_deg*k about the axis. Mirrors pattern.py's geometry
                // strategy in the simplified id-ref model.
                const std::string seed_id = req_str(feat, "seed");
                const TopoDS_Shape& seed = require_solid(results, seed_id, "seed");

                const int count =
                    static_cast<int>(std::lround(num_or_expr(feat.at("count"), env)));
                if (count < 2)
                    throw std::runtime_error("circular pattern count must be >= 2");

                // Angle is in DEGREES (SolidWorks + pattern.py convention) — note this
                // DIFFERS from revolve's angle, which is radians.
                const double angle_deg = feat.contains("angle")
                    ? num_or_expr(feat.at("angle"), env) : 360.0;
                const bool equal_spacing = feat.value("equal_spacing", true);
                // equal_spacing: instances fill the total angle (a full 360 wraps
                // cleanly); otherwise `angle` is the increment between instances.
                const double step_deg = equal_spacing ? angle_deg / count : angle_deg;

                const Eigen::Vector3d axis_pt = req_vec3(feat, "axis_pt");
                const Eigen::Vector3d axis_dir = req_vec3(feat, "axis_dir");

                TopoDS_Shape res = seed;
                for (int k = 1; k < count; ++k) {
                    TopoDS_Shape copy = ::geom::rotate_copy(
                        seed, axis_pt, axis_dir, radians(step_deg * k));
                    res = ::geom::fuse(res, copy);
                    if (res.IsNull())
                        throw std::runtime_error("circular pattern instance fuse failed");
                }

                // The seed is absorbed into the pattern result (a SW pattern owns its
                // instances), so it no longer survives standalone.
                consumed.insert(seed_id);

                results[id] = SolidResult{res};
                solid_order.push_back(id);
                st.ok = true;

            } else if (type == "mirror") {
                // Reflect a referenced solid body across a plane, then either FUSE the
                // reflection into the body (merge:true, the default — SolidWorks "Mirror
                // bodies" with Merge on) or keep it as a SEPARATE new body (merge:false).
                // The plane comes from a FACE SELECTOR ({"kind":"face",...} resolved
                // against the body's live topology) or an explicit plane_point/normal —
                // the simplified id-ref port of engine/doc/features/pattern.py::compile_mirror.
                const std::string body_id = req_str(feat, "body");
                const TopoDS_Shape& body = require_solid(results, body_id, "body");

                // Resolve the mirror plane to (point, unit normal).
                Eigen::Vector3d point, normal;
                auto plane_it = feat.find("plane");
                const bool face_plane =
                    plane_it != feat.end() && plane_it->is_object() &&
                    plane_it->value("kind", std::string()) == "face";
                if (face_plane) {
                    aicad::doc::FacePicks fp =
                        aicad::doc::resolve_face_picks(body, *plane_it, "plane");
                    if (!fp.dangling.empty()) {
                        std::string msg = "mirror: plane selector dangling: ";
                        for (std::size_t k = 0; k < fp.dangling.size(); ++k) {
                            if (k) msg += "; ";
                            msg += fp.dangling[k];
                        }
                        throw std::runtime_error(msg);
                    }
                    if (fp.faces.empty())
                        throw std::runtime_error("mirror: plane face not resolved");
                    BRepAdaptor_Surface surf(fp.faces[0]);
                    if (surf.GetType() != GeomAbs_Plane)
                        throw std::runtime_error("mirror: selected face is not planar");
                    gp_Pln pln = surf.Plane();
                    gp_Pnt loc = pln.Location();
                    gp_Dir dir = pln.Axis().Direction();
                    point = Eigen::Vector3d(loc.X(), loc.Y(), loc.Z());
                    normal = Eigen::Vector3d(dir.X(), dir.Y(), dir.Z());
                } else {
                    point = req_vec3(feat, "plane_point");
                    normal = req_vec3(feat, "plane_normal");
                    if (!(normal.norm() > 1e-12))
                        throw std::runtime_error("mirror: plane_normal has zero length");
                    normal.normalize();
                }

                TopoDS_Shape reflected = ::geom::mirror_copy(body, point, normal);
                if (reflected.IsNull())
                    throw std::runtime_error("mirror produced an empty reflection");

                const bool merge = feat.value("merge", true);
                if (merge) {
                    // Union the reflection into the body; the body is consumed (the
                    // merged solid is its new current state in the tree).
                    TopoDS_Shape out = ::geom::fuse(body, reflected);
                    if (out.IsNull())
                        throw std::runtime_error("mirror union (fuse) produced an empty shape");
                    consumed.insert(body_id);
                    results[id] = SolidResult{out};
                } else {
                    // The reflection is a NEW standalone body; the original body is NOT
                    // consumed, so BOTH survive.
                    results[id] = SolidResult{reflected};
                }
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
