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
#include <BRepBndLib.hxx>
#include <BRepGProp.hxx>
#include <Bnd_Box.hxx>
#include <GProp_GProps.hxx>
#include <GeomAbs_SurfaceType.hxx>
#include <TopAbs.hxx>
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
#include "geom/hole.h"
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

// A numeric param that may be (a) a raw number, (b) a string expression, or (c) the
// CANONICAL dim object {"value": <num>, "expr": <string|null>} the host sends for
// every dimensioned param (radius/length/diameter/angle/depth/count/spacing...).
// Mirrors the Python ctx.dim resolve contract: prefer a non-empty expr (evaluated
// against env), else the numeric value. Throws on a bad expression / wrong shape —
// the per-feature try/catch turns that into an error status.
double num_or_expr(const json& v, const EnvMap& env) {
    if (v.is_number()) return v.get<double>();
    if (v.is_string()) return aicad::util::evaluate(v.get<std::string>(), env);
    if (v.is_object()) {
        auto e = v.find("expr");
        if (e != v.end() && e->is_string() && !e->get<std::string>().empty())
            return aicad::util::evaluate(e->get<std::string>(), env);
        auto val = v.find("value");
        if (val != v.end() && val->is_number()) return val->get<double>();
        throw std::runtime_error("dim object has neither a usable 'expr' nor numeric 'value'");
    }
    throw std::runtime_error("param is neither a number, expression, nor a dim object");
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

// A reserved axis datum ("axis.x/y/z", or {"kind":"datum","feature":"axis.z"}) ->
// (point on axis, unit direction). nullopt for anything else (a topology selector
// axis is not yet supported here). All reserved axes pass through the world origin
// (engine/doc/features/datum.py reserved_datums), Z-up.
std::optional<std::pair<Eigen::Vector3d, Eigen::Vector3d>> axis_from_datum(const json& ref) {
    std::string name;
    if (ref.is_string()) {
        name = ref.get<std::string>();
    } else if (ref.is_object() && ref.value("kind", std::string()) == "datum" &&
               ref.contains("feature") && ref["feature"].is_string()) {
        name = ref["feature"].get<std::string>();
    } else {
        return std::nullopt;
    }
    const Eigen::Vector3d o(0, 0, 0);
    if (name == "axis.x") return std::make_pair(o, Eigen::Vector3d(1, 0, 0));
    if (name == "axis.y") return std::make_pair(o, Eigen::Vector3d(0, 1, 0));
    if (name == "axis.z") return std::make_pair(o, Eigen::Vector3d(0, 0, 1));
    return std::nullopt;
}

// A reserved plane datum ("plane.top/front/right" or the bare "top"/"front"/"right",
// or {"kind":"datum","feature":"plane.top"}) -> (point, normal), reusing the
// SketchPlane frames so the signs match the rest of the C++ (top=+Z, front=-Y,
// right=+X). nullopt for anything else (a face-selector plane is handled separately).
std::optional<std::pair<Eigen::Vector3d, Eigen::Vector3d>> plane_pt_normal_from_datum(
    const json& ref) {
    std::string name;
    if (ref.is_string()) {
        name = ref.get<std::string>();
    } else if (ref.is_object() && ref.value("kind", std::string()) == "datum" &&
               ref.contains("feature") && ref["feature"].is_string()) {
        name = ref["feature"].get<std::string>();
    } else {
        return std::nullopt;
    }
    if (name.rfind("plane.", 0) == 0) name = name.substr(6);
    if (name != "top" && name != "front" && name != "right") return std::nullopt;
    aicad::geom::SketchPlane p = plane_from_name(name);
    return std::make_pair(p.origin, p.normal);
}

// Extract the owning feature id from a body/region selector entry or a bare id
// string. A canonical body selector is {"kind":"body","owner_feature":"fN"}; a
// region selector carries "sketch"; the simplified model passes a bare id string.
std::string selector_owner_id(const json& sel) {
    if (sel.is_string()) return sel.get<std::string>();
    if (sel.is_object()) {
        if (sel.contains("owner_feature") && sel["owner_feature"].is_string())
            return sel["owner_feature"].get<std::string>();
        if (sel.contains("sketch") && sel["sketch"].is_string())
            return sel["sketch"].get<std::string>();
    }
    return std::string();
}

// A canonical extrude/revolve `profile` ref is a SEL_LIST of region selectors
// {"kind":"region","sketch":fid,...} (engine/doc/features/extrude.py). In the
// simplified id-ref model a profile is just the sketch's feature id string. Map
// the canonical list to that id (the first region selector's sketch) so the
// existing profile path resolves it. A bare string passes through (simplified
// shape). Other selector kinds (face profiles) are not yet supported here.
json canonical_profile_to_id(const json& profile) {
    if (profile.is_string()) return profile;  // already simplified
    if (profile.is_array() && !profile.empty()) {
        const json& sel = profile.front();
        if (sel.is_string()) return sel;
        if (sel.is_object() && sel.contains("sketch") && sel["sketch"].is_string())
            return sel["sketch"];
    }
    if (profile.is_object() && profile.contains("sketch") && profile["sketch"].is_string())
        return profile["sketch"];
    return profile;  // leave as-is -> the profile resolver will raise a clear error
}

// SCHEMA CONVERGENCE (docs/CPP_MIGRATION.md): accept BOTH the host's canonical
// feature shape — {id, type, params:{...}, refs:{slot:<sel|datum>}} per
// engine/doc/schema.py — and the simplified flat shape the early C++ tests use.
// We normalize to the flat shape the per-feature compilers already read: params
// and refs are merged to top level, and the two structural divergences are
// translated (plane datum "plane.top" -> "top"; region-selector profile list ->
// sketch id). A feature already in the flat shape (no params/refs) passes through
// unchanged, so the existing tests stay byte-identical. This lets the parity
// dashboard + the host feed REAL canonical trees into the ported subset.
json normalize_feature(const json& raw) {
    json f = json::object();
    const std::string type = raw.value("type", std::string());
    // 1. Copy top-level scalar/other keys (the flat shape & metadata like id/type).
    for (auto it = raw.begin(); it != raw.end(); ++it) {
        if (it.key() == "params" || it.key() == "refs") continue;
        f[it.key()] = it.value();
    }
    // 2. Merge canonical params -> top level.
    if (raw.contains("params") && raw["params"].is_object()) {
        for (auto it = raw["params"].begin(); it != raw["params"].end(); ++it)
            f[it.key()] = it.value();
    }
    // 3. Merge / translate canonical refs -> top level.
    if (raw.contains("refs") && raw["refs"].is_object()) {
        const json& r = raw["refs"];
        for (auto it = r.begin(); it != r.end(); ++it) {
            const std::string& slot = it.key();
            const json& val = it.value();
            if (slot == "profile") {
                f["profile"] = canonical_profile_to_id(val);
            } else if (slot == "axis") {
                // revolve / circular_pattern axis: a reserved datum -> axis_pt/axis_dir.
                if (auto ax = axis_from_datum(val)) {
                    f["axis_pt"] = {ax->first.x(), ax->first.y(), ax->first.z()};
                    f["axis_dir"] = {ax->second.x(), ax->second.y(), ax->second.z()};
                } else {
                    f["axis"] = val;  // leave for a clearer error / future selector port
                }
            } else if (slot == "dir1" || slot == "dir2") {
                // linear_pattern direction: a reserved axis datum -> a unit vec3.
                if (auto ax = axis_from_datum(val))
                    f[slot] = {ax->second.x(), ax->second.y(), ax->second.z()};
                else
                    f[slot] = val;
            } else if (slot == "targets") {
                if (val.is_array() && !val.empty()) f["a"] = selector_owner_id(val.front());
            } else if (slot == "tools") {
                if (val.is_array() && !val.empty()) f["b"] = selector_owner_id(val.front());
            } else if (slot == "features") {
                // pattern/mirror replicate a LIST of feature ids; the simplified C++
                // model patterns a single seed -> take the first.
                if (val.is_array() && !val.empty()) f["seed"] = selector_owner_id(val.front());
            } else if (slot == "plane") {
                if (type == "sketch" && val.is_string()) {
                    // sketch plane datum "plane.top" -> the plane NAME "top".
                    std::string p = val.get<std::string>();
                    if (p.rfind("plane.", 0) == 0) p = p.substr(6);
                    f["plane"] = p;
                } else if (auto pn = plane_pt_normal_from_datum(val)) {
                    // mirror/draft about a reserved datum plane -> point + normal.
                    f["plane_point"] = {pn->first.x(), pn->first.y(), pn->first.z()};
                    f["plane_normal"] = {pn->second.x(), pn->second.y(), pn->second.z()};
                } else {
                    f["plane"] = val;  // a face-selector object passes through
                }
            } else if (slot == "face" || slot == "edges" || slot == "remove" ||
                       slot == "faces" || slot == "neutral" || slot == "pull" ||
                       slot == "sections") {
                f[slot] = val;  // selector(-list) refs pass through to their branch
            } else {
                f[slot] = val;  // a/b/body/seed/... pass through
            }
        }
    }
    // 4. Param vocabulary translation: the canonical boolean op names map to the
    // kernel's fuse/cut/common; chamfer's canonical "length" is the C++ "distance".
    if (type == "boolean" && f.contains("op") && f["op"].is_string()) {
        const std::string op = f["op"].get<std::string>();
        if (op == "combine") f["op"] = "fuse";
        else if (op == "subtract") f["op"] = "cut";
        else if (op == "intersect") f["op"] = "common";
    }
    if (type == "chamfer" && f.contains("length") && !f.contains("distance"))
        f["distance"] = f["length"];
    // hole: the canonical explicit [u,v] list is refs.points; the C++ hole branch
    // reads "positions" (refs.positions in the canonical schema is a vertex-selector
    // list, a different thing — not yet ported).
    if (type == "hole" && f.contains("points") && !f.contains("positions"))
        f["positions"] = f["points"];
    return f;
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

    // The "current top body": the most-recently-produced solid not yet consumed.
    // Canonical features that omit an explicit body/seed ref (fillet/chamfer/hole/
    // shell/draft/mirror/pattern, and extrude-cut) operate on it (SolidWorks "the
    // running body"). Returns "" when no solid exists yet.
    auto current_solid_id = [&]() -> std::string {
        for (auto it = solid_order.rbegin(); it != solid_order.rend(); ++it)
            if (!consumed.count(*it)) return *it;
        return std::string();
    };
    // The body a dress/hole/mirror feature targets: an explicit "body" id (the
    // simplified model) or, for canonical features that carry no body ref, the
    // current top body. Empty when neither exists -> the branch raises a clear error.
    auto body_ref = [&](const json& feat) -> std::string {
        if (feat.contains("body") && feat["body"].is_string())
            return feat["body"].get<std::string>();
        return current_solid_id();
    };

    const json empty = json::array();
    const json& features = doc.contains("features") ? doc.at("features") : empty;

    for (const json& raw : features.is_array() ? features : empty) {
        // Accept the host's canonical {params,refs} shape OR the simplified flat
        // shape, normalizing to flat so the compilers below read either (see
        // normalize_feature / docs/CPP_MIGRATION.md schema convergence).
        const json feat = normalize_feature(raw);
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
                // Canonical mode: "cut" subtracts the prism from the current body;
                // anything else ("new"/"boss"/"add") builds a standalone boss.
                const std::string mode = feat.value("mode", std::string("new"));
                const bool is_cut = (mode == "cut");
                // Canonical end condition: "blind" (depth), "through_all" (penetrate
                // the body both ways), "mid_plane" (centered). Simplified shape uses
                // depth + optional mid_plane bool.
                const std::string end = feat.value("end", std::string("blind"));
                bool mid_plane = feat.value("mid_plane", false) || end == "mid_plane";
                Eigen::Vector3d dir =
                    opt_vec3(feat, "dir").value_or(src.plane.normal);

                // For a cut / through_all we size against the current body.
                const std::string tgt_id = is_cut ? current_solid_id() : std::string();
                double depth;
                if (end == "through_all") {
                    if (tgt_id.empty())
                        throw std::runtime_error("extrude through_all requires an existing body");
                    const TopoDS_Shape& tb = require_solid(results, tgt_id, "target");
                    const double diag = aicad::doc::bbox_diag(tb);
                    if (!(diag > 0.0))
                        throw std::runtime_error("extrude through_all: body has no extent");
                    depth = diag * 2.2;   // span the whole body...
                    mid_plane = true;     // ...both directions from the sketch plane
                } else {
                    depth = num_or_expr(feat.at("depth"), env);
                    if (!(depth > 0.0))
                        throw std::runtime_error("extrude depth must be > 0");
                }

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

                if (is_cut) {
                    if (tgt_id.empty())
                        throw std::runtime_error("extrude cut requires an existing body");
                    const TopoDS_Shape& tgt = require_solid(results, tgt_id, "target");
                    TopoDS_Shape out = ::geom::cut(tgt, solid);
                    if (out.IsNull())
                        throw std::runtime_error("extrude cut produced an empty shape");
                    GProp_GProps gb, ga;
                    BRepGProp::VolumeProperties(tgt, gb);
                    BRepGProp::VolumeProperties(out, ga);
                    if (std::abs(ga.Mass() - gb.Mass()) < 1e-9)
                        throw std::runtime_error("extrude cut removed no material");
                    consumed.insert(tgt_id);
                    results[id] = SolidResult{out};
                } else {
                    results[id] = SolidResult{solid};
                }
                solid_order.push_back(id);
                st.ok = true;

            } else if (type == "revolve") {
                const std::string prof = req_str(feat, "profile");
                const SketchResult& src = require_sketch(results, prof, prof.c_str());
                const Eigen::Vector3d axis_pt = req_vec3(feat, "axis_pt");
                const Eigen::Vector3d axis_dir = req_vec3(feat, "axis_dir");
                // Canonical revolve angle is in DEGREES (SolidWorks/Python); the
                // kernel revolve_face takes radians. (No existing test drives the
                // revolve branch, so aligning to the canonical degree contract is safe.)
                const double angle_deg =
                    feat.contains("angle") ? num_or_expr(feat.at("angle"), env) : 360.0;
                const double angle = radians(angle_deg);
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
                const std::string body_id = body_ref(feat);
                if (body_id.empty())
                    throw std::runtime_error(type + ": no body ref and no current body");
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
                const std::string body_id = body_ref(feat);
                if (body_id.empty())
                    throw std::runtime_error(type + ": no body ref and no current body");
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

            } else if (type == "hole") {
                // Drill simple / cbore / csink hole(s) into a referenced solid at
                // position(s) on a FACE-SELECTOR-resolved planar face. One tool per
                // position is built, all unioned, then cut from the body in a single
                // boolean (one feature, N holes — SolidWorks Hole Wizard, simplified:
                // no thread tables / cosmetic threads / grid / sketch positions). Port
                // of engine/doc/features/hole.py's geometry core in the id-ref model.
                const std::string body_id = body_ref(feat);
                if (body_id.empty())
                    throw std::runtime_error(type + ": no body ref and no current body");
                const TopoDS_Shape& body = require_solid(results, body_id, "body");

                // --- resolve the target planar face (a {"kind":"face"} selector) ---
                auto face_it = feat.find("face");
                if (face_it == feat.end() || !face_it->is_object())
                    throw std::runtime_error(
                        "hole: 'face' selector is required (the planar face to drill into)");
                aicad::doc::FacePicks fp =
                    aicad::doc::resolve_face_picks(body, *face_it, "face");
                if (!fp.dangling.empty()) {
                    std::string msg = "hole: face selector dangling: ";
                    for (std::size_t k = 0; k < fp.dangling.size(); ++k) {
                        if (k) msg += "; ";
                        msg += fp.dangling[k];
                    }
                    throw std::runtime_error(msg);
                }
                if (fp.faces.empty())
                    throw std::runtime_error("hole: target face not resolved");
                const TopoDS_Face& face = fp.faces[0];

                BRepAdaptor_Surface surf(face);
                if (surf.GetType() != GeomAbs_Plane)
                    throw std::runtime_error("hole: target face is not planar");
                gp_Pln pln = surf.Plane();

                // --- face frame (origin + x/y in-plane + outward normal) ---
                gp_Pnt o = pln.Location();
                gp_Dir xd = pln.XAxis().Direction();
                gp_Dir yd = pln.YAxis().Direction();
                gp_Dir nd = pln.Axis().Direction();
                Eigen::Vector3d origin(o.X(), o.Y(), o.Z());
                Eigen::Vector3d xdir(xd.X(), xd.Y(), xd.Z());
                Eigen::Vector3d ydir(yd.X(), yd.Y(), yd.Z());
                Eigen::Vector3d normal(nd.X(), nd.Y(), nd.Z());
                // OUTWARD-NORMAL CORRECTION: a face stored REVERSED in the solid has its
                // geometric normal pointing INTO the material, so flip it to point OUT
                // of the solid. drill_dir then points INTO the material (the watch-point:
                // get this wrong and the cut removes nothing).
                if (face.Orientation() == TopAbs_REVERSED) normal = -normal;
                const Eigen::Vector3d drill_dir = -normal;

                // --- positions: [u,v] face-local mm pairs; default = face centre ---
                std::vector<std::pair<double, double>> uv;
                auto pos_it = feat.find("positions");
                if (pos_it != feat.end() && pos_it->is_array() && !pos_it->empty()) {
                    for (std::size_t k = 0; k < pos_it->size(); ++k) {
                        const json& p = (*pos_it)[k];
                        if (!p.is_array() || p.size() < 2)
                            throw std::runtime_error(
                                "hole: positions[" + std::to_string(k) + "] must be [u, v]");
                        uv.emplace_back(p[0].get<double>(), p[1].get<double>());
                    }
                } else {
                    uv.emplace_back(0.0, 0.0);  // back-compat single hole at face centre
                }

                // --- style ---
                const std::string style_s = feat.value("style", std::string("simple"));
                ::geom::HoleStyle style;
                if (style_s == "simple")      style = ::geom::HoleStyle::Simple;
                else if (style_s == "cbore")  style = ::geom::HoleStyle::Cbore;
                else if (style_s == "csink")  style = ::geom::HoleStyle::Csink;
                else throw std::runtime_error("hole: unknown style '" + style_s + "'");

                // --- dims ---
                ::geom::HoleDims dims;
                dims.diameter = num_or_expr(feat.at("diameter"), env);
                if (!(dims.diameter > 0.0))
                    throw std::runtime_error("hole: diameter must be > 0");
                if (style == ::geom::HoleStyle::Cbore) {
                    dims.cbore_d = num_or_expr(feat.at("cbore_d"), env);
                    dims.cbore_depth = num_or_expr(feat.at("cbore_depth"), env);
                    if (!(dims.cbore_d > dims.diameter))
                        throw std::runtime_error("hole: cbore_d must be larger than diameter");
                    if (!(dims.cbore_depth > 0.0))
                        throw std::runtime_error("hole: cbore_depth must be > 0");
                } else if (style == ::geom::HoleStyle::Csink) {
                    dims.csink_d = num_or_expr(feat.at("csink_d"), env);
                    dims.csink_angle_deg = feat.contains("csink_angle")
                        ? num_or_expr(feat.at("csink_angle"), env) : 90.0;
                    if (!(dims.csink_d > dims.diameter))
                        throw std::runtime_error("hole: csink_d must be larger than diameter");
                    if (!(dims.csink_angle_deg > 0.0 && dims.csink_angle_deg < 180.0))
                        throw std::runtime_error("hole: csink_angle must be in (0, 180)");
                }

                // --- end condition -> shaft depth ---
                const std::string end = feat.value("end", std::string("through_all"));
                double depth;
                if (end == "through_all") {
                    // Over-length so the bore fully penetrates; the cut only removes the
                    // in-body part, so over-length is harmless.
                    depth = aicad::doc::bbox_diag(body) * 1.1;
                } else if (end == "blind") {
                    depth = num_or_expr(feat.at("depth"), env);
                    if (!(depth > 0.0))
                        throw std::runtime_error("hole: blind depth must be > 0");
                } else {
                    throw std::runtime_error("hole: unknown end '" + end + "'");
                }

                // --- build every position's tool, union into one, then cut ---
                const double lift = 0.05;  // tool mouth sits just above the face
                ::geom::HoleDims tool_dims = dims;
                tool_dims.depth = depth + lift;

                TopoDS_Shape tool_union;
                for (const auto& p : uv) {
                    Eigen::Vector3d world = origin + p.first * xdir + p.second * ydir;
                    Eigen::Vector3d lifted = world + lift * normal;  // just above the face
                    TopoDS_Shape t =
                        ::geom::make_hole_tool(lifted, drill_dir, style, tool_dims);
                    if (t.IsNull())
                        throw std::runtime_error("hole: failed to build a hole tool");
                    tool_union = tool_union.IsNull() ? t : ::geom::fuse(tool_union, t);
                    if (tool_union.IsNull())
                        throw std::runtime_error("hole: tool union (fuse) failed");
                }

                TopoDS_Shape out = ::geom::cut(body, tool_union);
                if (out.IsNull())
                    throw std::runtime_error("hole: boolean cut produced an empty shape");

                // MATERIAL-REMOVED GUARD (mirror hole.py): a cut that removed nothing
                // means the positions miss the face/body or the drill went the wrong
                // way — surface it as a clear per-feature error, not a silent no-op.
                GProp_GProps gp_before, gp_after;
                BRepGProp::VolumeProperties(body, gp_before);
                BRepGProp::VolumeProperties(out, gp_after);
                if (std::abs(gp_after.Mass() - gp_before.Mass()) < 1e-9)
                    throw std::runtime_error(
                        "hole removed no material (positions may fall outside the face/body)");

                // The drilled solid REPLACES the body (SolidWorks-style): the bored
                // solid is the body's new current state in the tree.
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
