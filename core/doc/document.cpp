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

#include <array>
#include <cmath>
#include <limits>
#include <map>
#include <optional>
#include <set>
#include <string>
#include <variant>
#include <vector>

#include <Eigen/Dense>

#include <BRep_Builder.hxx>
#include <BRep_Tool.hxx>
#include <BRepAdaptor_Surface.hxx>
#include <BRepBuilderAPI_MakeEdge.hxx>
#include <BRepBuilderAPI_Transform.hxx>
#include <BRepBndLib.hxx>
#include <BRepGProp.hxx>
#include <BRepPrimAPI_MakeBox.hxx>
#include <BRepTools.hxx>
#include <TopExp.hxx>
#include <TopExp_Explorer.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <TopoDS_Vertex.hxx>
#include <Bnd_Box.hxx>
#include <GProp_GProps.hxx>
#include <GeomAbs_SurfaceType.hxx>
#include <TopAbs.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Compound.hxx>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Shape.hxx>
#include <gp_Ax1.hxx>
#include <gp_Ax2.hxx>
#include <gp_Dir.hxx>
#include <gp_Pln.hxx>
#include <gp_Trsf.hxx>
#include <gp_Pnt.hxx>
#include <gp_Vec.hxx>
#include <Geom_Surface.hxx>

#include "doc/formats.h"  // import_bodies / detect_format (the import feature)
#include "doc/picks.h"
#include "doc/selectors.h"
#include "doc/session.h"  // OpError (plan_push_pull's structured out-of-range errors)
#include "geom/booleans.h"
#include "geom/dress.h"
#include "geom/features.h"
#include "geom/hole.h"
#include "geom/plane.h"
#include "geom/profile.h"
#include "geom/sheetmetal.h"
#include "geom/weldment.h"
#include "geom/surfacing.h"
#include "geom/curves.h"
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
    nlohmann::json entities;  // solved entities (for a sweep path / rib trace wire)
};

// A solid-body result.
struct SolidResult {
    TopoDS_Shape shape;
};

// A datum-plane result: a SketchPlane a later sketch can be built on (e.g. an
// offset plane for a loft section). Never tessellates; not a body. Also carries a
// `csys` flag so a csys datum (which is geometrically a Plane) round-trips its kind.
struct PlaneResult {
    aicad::geom::SketchPlane plane;
    bool is_csys = false;
};

// A datum-axis result: (point on axis, unit direction). Consumed by a downstream
// revolve / circular_pattern. Port of engine/doc/features/datum.py compile_axis.
struct AxisResult {
    Eigen::Vector3d point;
    Eigen::Vector3d dir;  // unit
};

// A datum-point result: a 3-D world point. Consumed by three_point / two_points /
// parallel_point datums. Port of engine/doc/features/datum.py compile_point.
struct PointResult {
    Eigen::Vector3d pt;
};

// A curve result: a reference WIRE (a helix / spiral) in a LOCAL frame plus the base
// plane it is lifted through. Consumed by a downstream sweep as its path (mirrors the
// _sketch_regions path contract). Never a body. Port of curves.py.
struct CurveResult {
    TopoDS_Wire wire;                 // local-frame curve
    aicad::geom::SketchPlane base;    // lift frame
};

using FeatureResult = std::variant<SketchResult, SolidResult, PlaneResult, AxisResult,
                                   PointResult, CurveResult>;

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
    // 2. Merge canonical params -> top level. A param named "type" (the sm_hem
    // sub-type "closed"/"open"/"teardrop") would clobber the feature's own "type"
    // key — remap it to "hem_type" so the feature type survives the flatten.
    if (raw.contains("params") && raw["params"].is_object()) {
        for (auto it = raw["params"].begin(); it != raw["params"].end(); ++it) {
            if (it.key() == "type" || it.key() == "id") {
                f["hem_type"] = it.value();
                continue;
            }
            f[it.key()] = it.value();
        }
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
            } else if (slot == "neutral") {
                // draft neutral plane: a reserved datum -> point+normal; a face
                // selector passes through for the branch to resolve.
                if (auto pn = plane_pt_normal_from_datum(val)) {
                    f["plane_point"] = {pn->first.x(), pn->first.y(), pn->first.z()};
                    f["plane_normal"] = {pn->second.x(), pn->second.y(), pn->second.z()};
                } else {
                    f["neutral"] = val;
                }
            } else if (slot == "pull") {
                if (auto ax = axis_from_datum(val))
                    f["pull_dir"] = {ax->second.x(), ax->second.y(), ax->second.z()};
                else
                    f["pull"] = val;
            } else if (slot == "face" || slot == "edges" || slot == "remove" ||
                       slot == "faces" || slot == "sections" || slot == "edge") {
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
// --------------------------------------------------------------------------- configurations
// Pick the active configuration: {name, config-or-null}. A config-less doc compiles as
// the implicit "Default" (null config). Otherwise active_configuration selects by name;
// a missing/unknown pointer falls back to the FIRST config (SolidWorks always has one
// active). Mirrors compiler.py::resolve_active_config.
std::pair<std::string, const json*> resolve_active_config(const json& doc) {
    static const char* kDefault = "Default";
    if (!doc.contains("configurations") || !doc["configurations"].is_array() ||
        doc["configurations"].empty())
        return {kDefault, nullptr};
    const json& configs = doc["configurations"];
    std::string active = doc.value("active_configuration", std::string());
    if (!active.empty()) {
        for (const json& c : configs)
            if (c.is_object() && c.value("name", std::string()) == active) return {active, &c};
    }
    const json& first = configs[0];
    std::string name = first.is_object() ? first.value("name", std::string()) : std::string();
    return {name.empty() ? kDefault : name, first.is_object() ? &first : nullptr};
}

// Overlay the active config onto `features` and return a NEW effective list: per
// feature, suppressed |= config.suppressions[fid] and params[key] replaced by
// config.param_overrides["fid.key"]. config==null returns features unchanged.
// Mirrors compiler.py::_config_effective_features.
json config_effective_features(const json& features, const json* config) {
    if (!config) return features;
    const json overrides = config->contains("param_overrides") && (*config)["param_overrides"].is_object()
                               ? (*config)["param_overrides"] : json::object();
    const json suppressions = config->contains("suppressions") && (*config)["suppressions"].is_object()
                                  ? (*config)["suppressions"] : json::object();
    if (overrides.empty() && suppressions.empty()) return features;

    // Bucket overrides by feature id: {fid: {param_key: value}}.
    std::map<std::string, json> by_fid;
    for (auto it = overrides.begin(); it != overrides.end(); ++it) {
        const std::string& key = it.key();
        auto dot = key.find('.');
        if (dot == std::string::npos) continue;
        by_fid[key.substr(0, dot)][key.substr(dot + 1)] = it.value();
    }
    json out = json::array();
    for (const json& feat : features) {
        if (!feat.is_object()) { out.push_back(feat); continue; }
        const std::string fid = feat.value("id", std::string());
        auto pit = by_fid.find(fid);
        const bool has_param = pit != by_fid.end();
        const bool has_sup = suppressions.contains(fid);
        if (!has_param && !has_sup) { out.push_back(feat); continue; }
        json ef = feat;
        if (has_sup)
            ef["suppressed"] = feat.value("suppressed", false) || suppressions[fid].get<bool>();
        if (has_param) {
            json params = feat.contains("params") && feat["params"].is_object() ? feat["params"]
                                                                                : json::object();
            for (auto it = pit->second.begin(); it != pit->second.end(); ++it)
                params[it.key()] = it.value();
            ef["params"] = params;
        }
        out.push_back(ef);
    }
    return out;
}

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

    // Body KIND per feature id ("solid" default; "surface" for open sheet bodies the
    // surf_* compilers produce). The surviving-bodies loop reads this to report AREA +
    // face/edge counts (surfaces) vs VOLUME (solids), the surfacing parity-gate signal.
    std::map<std::string, std::string> body_kinds;

    // The body each MODIFYING feature derived from (fillet/shell/boolean/... -> its
    // input solid id). Empty/absent for a CREATING feature (extrude boss/revolve/
    // sweep/loft/primitive build a fresh body). Drives the owner-map topology diff.
    std::map<std::string, std::string> source_of;

    // --- sheet-metal part state (the C++ analogue of the oracle's ctx.datums
    // _sheet_metal / _sheet_metal_bends). A base flange establishes the part as
    // sheet metal (thickness / k-factor / default bend radius); later edge flanges /
    // hems / jogs inherit it and each appends a bend record the flat pattern reads. ---
    struct SheetCtx {
        bool active = false;
        double thickness = 0.0;
        double k_factor = 0.0;
        double default_bend_radius = 0.0;
    } sheet_ctx;
    std::vector<::geom::BendRecord> bend_records;

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

    // --- surfacing helpers (engine/doc/features/surf_*.py) -----------------------
    // Resolve a `profile` ref (a sketch id, after normalize_feature) to its solved
    // planar faces. Mirrors extrude._resolve_profile_faces for the simplified model.
    auto surf_profile_faces = [&](const json& feat) -> std::vector<TopoDS_Face> {
        const std::string prof = req_str(feat, "profile");
        const SketchResult& src = require_sketch(results, prof, prof.c_str());
        if (src.faces.empty())
            throw std::runtime_error("surf: profile sketch has no closed face");
        return src.faces;
    };
    // Resolve a sketch-id curve ref to its raw closed/open wire (for ruled rails /
    // fill / boundary boundary curves). The ref is a region selector or a bare id.
    auto surf_sketch_wire = [&](const json& ref, const char* slot) -> TopoDS_Wire {
        const std::string sk = selector_owner_id(ref);
        if (sk.empty())
            throw std::runtime_error(std::string("surf: malformed ") + slot + " ref");
        const SketchResult& src = require_sketch(results, sk, slot);
        TopoDS_Wire w = aicad::geom::wire_from_solved_sketch(src.entities, src.plane);
        if (w.IsNull())
            throw std::runtime_error(std::string("surf: ") + slot + " sketch produced no wire");
        return w;
    };
    // Planar-face normal (build123d face.normal_at() analogue): the geometric surface
    // normal at the face's UV midpoint, used to set the surf_extrude sweep direction.
    auto src_face_normal = [&](const TopoDS_Face& f) -> Eigen::Vector3d {
        Standard_Real u0, u1, v0, v1;
        BRepTools::UVBounds(f, u0, u1, v0, v1);
        Handle(Geom_Surface) gs = BRep_Tool::Surface(f);
        Eigen::Vector3d n(0, 0, 1);
        if (!gs.IsNull()) {
            gp_Pnt p;
            gp_Vec du, dv;
            gs->D1((u0 + u1) / 2.0, (v0 + v1) / 2.0, p, du, dv);
            gp_Vec nv = du.Crossed(dv);
            if (nv.Magnitude() > 1e-12) {
                nv.Normalize();
                n = Eigen::Vector3d(nv.X(), nv.Y(), nv.Z());
            }
        }
        if (f.Orientation() == TopAbs_REVERSED) n = -n;
        return n;
    };

    // --- datum resolvers (engine/doc/features/datum.py) --------------------------
    // Resolve a ref to a 3-D POINT: a reserved "origin" datum, a point-datum feature
    // id, or a {"kind":"datum","feature":id} selector pointing at a point feature.
    // Throws (-> error status) on anything not resolvable to a point here (geometry
    // vertex selectors are not ported — they raise a clear, contained error).
    auto resolve_point = [&](const json& ref, const char* slot) -> Eigen::Vector3d {
        std::string fid;
        if (ref.is_string()) fid = ref.get<std::string>();
        else if (ref.is_object() && ref.value("kind", std::string()) == "datum" &&
                 ref.contains("feature") && ref["feature"].is_string())
            fid = ref["feature"].get<std::string>();
        if (fid == "origin") return Eigen::Vector3d(0, 0, 0);
        if (!fid.empty()) {
            auto it = results.find(fid);
            if (it != results.end() && std::holds_alternative<PointResult>(it->second))
                return std::get<PointResult>(it->second).pt;
        }
        throw std::runtime_error(std::string(slot) +
            ": expected a point datum (geometry vertex selectors not yet ported here)");
    };
    // Resolve a ref to a datum AXIS (point, unit dir): a reserved axis.x/y/z, an
    // axis-datum feature id, or a {"kind":"datum",...} selector. nullopt otherwise.
    auto resolve_axis_ref =
        [&](const json& ref) -> std::optional<std::pair<Eigen::Vector3d, Eigen::Vector3d>> {
        if (auto ax = axis_from_datum(ref)) return ax;  // reserved axis.x/y/z
        std::string fid;
        if (ref.is_string()) fid = ref.get<std::string>();
        else if (ref.is_object() && ref.value("kind", std::string()) == "datum" &&
                 ref.contains("feature") && ref["feature"].is_string())
            fid = ref["feature"].get<std::string>();
        if (!fid.empty()) {
            auto it = results.find(fid);
            if (it != results.end() && std::holds_alternative<AxisResult>(it->second)) {
                const AxisResult& a = std::get<AxisResult>(it->second);
                return std::make_pair(a.point, a.dir);
            }
        }
        return std::nullopt;
    };
    // Resolve a ref to a datum PLANE (origin, normal): a reserved plane.top/front/
    // right, OR a plane/csys datum feature id (its SketchPlane). nullopt otherwise.
    auto resolve_plane_ref =
        [&](const json& ref) -> std::optional<aicad::geom::SketchPlane> {
        std::string fid;
        if (ref.is_string()) fid = ref.get<std::string>();
        else if (ref.is_object() && ref.value("kind", std::string()) == "datum" &&
                 ref.contains("feature") && ref["feature"].is_string())
            fid = ref["feature"].get<std::string>();
        if (!fid.empty()) {
            auto it = results.find(fid);
            if (it != results.end() && std::holds_alternative<PlaneResult>(it->second))
                return std::get<PlaneResult>(it->second).plane;
        }
        std::string name = fid;
        if (name.rfind("plane.", 0) == 0) name = name.substr(6);
        if (name == "top" || name == "front" || name == "right")
            return plane_from_name(name);
        return std::nullopt;
    };

    const json empty = json::array();
    const json& base_features = doc.contains("features") ? doc.at("features") : empty;

    // Configuration overlay: compile the ACTIVE config's effective features (param
    // overrides + suppression). A config-less doc returns the base list unchanged.
    auto [active_cfg_name, active_cfg] = resolve_active_config(doc);
    result.active_configuration = active_cfg_name;
    const json features = config_effective_features(base_features.is_array() ? base_features : empty,
                                                    active_cfg);

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

        // A SUPPRESSED feature contributes no geometry but is OK (mirrors the Python
        // compiler: status ok, no body, topology unchanged). Skip it cleanly.
        if (raw.value("suppressed", false) || feat.value("suppressed", false)) {
            st.ok = true;
            result.statuses.push_back(std::move(st));
            continue;
        }

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

                // The sketch plane is a reserved datum NAME ("top"/"front"/"right")
                // OR a prior `plane` feature id (e.g. an offset datum plane).
                const std::string pref = str_field(feat, "plane", "top");
                aicad::geom::SketchPlane plane;
                auto pit = results.find(pref);
                if (pit != results.end() &&
                    std::holds_alternative<PlaneResult>(pit->second))
                    plane = std::get<PlaneResult>(pit->second).plane;
                else
                    plane = plane_from_name(pref);
                std::vector<TopoDS_Face> faces =
                    aicad::geom::faces_from_solved_sketch(sol.entities, plane);
                // NB: an open sketch (no closed loop) is NOT an error here — it is a
                // valid path/trace for a sweep/rib. A consuming feature that needs a
                // face (extrude/revolve) raises if faces is empty.
                results[id] = SketchResult{std::move(faces), plane, sol.entities};
                st.ok = true;

            } else if (type == "plane") {
                // Datum plane (engine/doc/features/datum.py compile_plane). Ported
                // methods: offset (from a base plane normal), midplane (between two
                // datum planes), angle (rotate a base plane about a datum-axis hinge),
                // three_point (through three point datums), parallel_point (base
                // orientation through a point datum). Selector-driven sub-cases
                // (face/edge/vertex pick hinges, on_surface, normal_to_curve) are
                // deferred — they need the topology selector matcher. Stores a
                // SketchPlane keyed by id; a downstream sketch+extrude gates it.
                const std::string method = feat.value("method", std::string("offset"));

                if (method == "offset") {
                    const json bref = feat.contains("base") ? feat.at("base") : json("plane.top");
                    auto bp = resolve_plane_ref(bref);
                    if (!bp) throw std::runtime_error("plane offset: base is not a planar datum");
                    const double off =
                        feat.contains("offset") ? num_or_expr(feat.at("offset"), env) : 0.0;
                    aicad::geom::SketchPlane p = *bp;
                    p.origin = bp->origin + bp->normal * off;
                    results[id] = PlaneResult{p};

                } else if (method == "midplane") {
                    const json aref = feat.contains("base") ? feat.at("base") : json("plane.top");
                    const json bref = feat.contains("second") ? feat.at("second") : json("plane.top");
                    auto a = resolve_plane_ref(aref);
                    auto b = resolve_plane_ref(bref);
                    if (!a || !b)
                        throw std::runtime_error("plane midplane: base/second must be planar datums");
                    // mid origin = (a.origin + b.origin)/2 ; orientation = a's frame.
                    aicad::geom::SketchPlane p = *a;
                    p.origin = (a->origin + b->origin) * 0.5;
                    results[id] = PlaneResult{p};

                } else if (method == "angle") {
                    const json bref = feat.contains("base") ? feat.at("base") : json("plane.top");
                    auto base = resolve_plane_ref(bref);
                    if (!base) throw std::runtime_error("plane angle: base is not a planar datum");
                    if (!feat.contains("second"))
                        throw std::runtime_error("plane angle: refs.second (a hinge axis) is required");
                    auto hinge = resolve_axis_ref(feat.at("second"));
                    if (!hinge)
                        throw std::runtime_error("plane angle: hinge must be a datum axis "
                                                 "(edge-selector hinges not yet ported here)");
                    double ang = feat.contains("angle") ? num_or_expr(feat.at("angle"), env) : 45.0;
                    if (feat.value("flip", false)) ang = -ang;
                    // Rotate the base plane's origin + axes about the hinge (point, dir).
                    gp_Trsf trsf;
                    trsf.SetRotation(
                        gp_Ax1(gp_Pnt(hinge->first.x(), hinge->first.y(), hinge->first.z()),
                               gp_Dir(hinge->second.x(), hinge->second.y(), hinge->second.z())),
                        radians(ang));
                    auto xf_pt = [&](const Eigen::Vector3d& v) {
                        gp_Pnt q(v.x(), v.y(), v.z()); q.Transform(trsf);
                        return Eigen::Vector3d(q.X(), q.Y(), q.Z());
                    };
                    auto xf_dir = [&](const Eigen::Vector3d& v) {
                        gp_Vec q(v.x(), v.y(), v.z()); q.Transform(trsf);
                        return Eigen::Vector3d(q.X(), q.Y(), q.Z());
                    };
                    Eigen::Vector3d o = xf_pt(base->origin);
                    Eigen::Vector3d nrm = xf_dir(base->normal);
                    Eigen::Vector3d uref = xf_dir(base->uAxis);
                    results[id] = PlaneResult{aicad::geom::SketchPlane::from(o, nrm, uref)};

                } else if (method == "three_point") {
                    auto pit = feat.find("points");
                    if (pit == feat.end() || !pit->is_array() || pit->size() < 3)
                        throw std::runtime_error(
                            "plane three_point: refs.points must be three point datums");
                    Eigen::Vector3d p0 = resolve_point((*pit)[0], "points[0]");
                    Eigen::Vector3d p1 = resolve_point((*pit)[1], "points[1]");
                    Eigen::Vector3d p2 = resolve_point((*pit)[2], "points[2]");
                    Eigen::Vector3d v1 = p1 - p0, v2 = p2 - p0;
                    Eigen::Vector3d n = v1.cross(v2);
                    if (n.norm() < 1e-9 || v1.norm() < 1e-9 || v2.norm() < 1e-9)
                        throw std::runtime_error(
                            "plane three_point: the three points are colinear (no unique plane)");
                    results[id] = PlaneResult{aicad::geom::SketchPlane::from(p0, n, v1)};

                } else if (method == "parallel_point") {
                    const json bref = feat.contains("base") ? feat.at("base") : json("plane.top");
                    auto base = resolve_plane_ref(bref);
                    if (!base) throw std::runtime_error("plane parallel_point: base is not a planar datum");
                    auto pit = feat.find("points");
                    if (pit == feat.end() || !pit->is_array() || pit->empty())
                        throw std::runtime_error(
                            "plane parallel_point: refs.points[0] (a through-point) is required");
                    Eigen::Vector3d thru = resolve_point((*pit)[0], "points[0]");
                    aicad::geom::SketchPlane p = *base;
                    p.origin = thru;
                    results[id] = PlaneResult{p};

                } else {
                    throw std::runtime_error("plane: method '" + method + "' not supported");
                }
                st.ok = true;

            } else if (type == "axis") {
                // Datum axis (engine/doc/features/datum.py compile_axis). Ported:
                // two_points (line through two point datums) + a reserved/axis-datum
                // pass-through. Selector-driven (cylinder face / edge / plane_intersect
                // / on_surface) are deferred. Stores an AxisResult; a downstream revolve
                // about this axis gates it (the swept solid's volume + position).
                const std::string method = feat.value("method", std::string("two_points"));
                if (method == "two_points") {
                    Eigen::Vector3d a = resolve_point(feat.at("a"), "a");
                    Eigen::Vector3d b = resolve_point(feat.at("b"), "b");
                    Eigen::Vector3d d = b - a;
                    if (d.norm() < 1e-9)
                        throw std::runtime_error("axis two_points: coincident points");
                    results[id] = AxisResult{a, d.normalized()};
                } else {
                    // cylinder / edge: accept a datum-axis pass-through (refs.a a datum).
                    auto ax = feat.contains("a") ? resolve_axis_ref(feat.at("a"))
                                                 : std::nullopt;
                    if (ax) {
                        results[id] = AxisResult{ax->first, ax->second};
                    } else {
                        throw std::runtime_error(
                            "axis: method '" + method + "' needs a geometry selector (not yet ported)");
                    }
                }
                st.ok = true;

            } else if (type == "point") {
                // Datum point (engine/doc/features/datum.py compile_point). Ported:
                // coordinate (explicit XYZ) + a point-datum target pass-through. Other
                // methods (vertex/center/on_face/on_curve/intersection/centroid) need
                // the topology selector matcher and are deferred. Stores a PointResult.
                const std::string method = feat.value("method", std::string("vertex"));
                if (method == "coordinate") {
                    Eigen::Vector3d p(0, 0, 0);
                    auto cit = feat.find("coordinate");
                    if (cit != feat.end() && cit->is_array() && cit->size() >= 3) {
                        p = Eigen::Vector3d((*cit)[0].get<double>(), (*cit)[1].get<double>(),
                                            (*cit)[2].get<double>());
                    } else {
                        p = Eigen::Vector3d(
                            feat.contains("x") ? num_or_expr(feat.at("x"), env) : 0.0,
                            feat.contains("y") ? num_or_expr(feat.at("y"), env) : 0.0,
                            feat.contains("z") ? num_or_expr(feat.at("z"), env) : 0.0);
                    }
                    results[id] = PointResult{p};
                } else if (feat.contains("target")) {
                    results[id] = PointResult{resolve_point(feat.at("target"), "target")};
                } else {
                    throw std::runtime_error(
                        "point: method '" + method + "' needs a geometry selector (not yet ported)");
                }
                st.ok = true;

            } else if (type == "csys") {
                // Datum coordinate system (engine/doc/features/datum.py compile_csys).
                // Geometrically a Plane: origin from a point datum (default "origin"),
                // optional x_dir/y_dir from datum axes (Gram-Schmidt orthonormal frame).
                // Edge-selector directions are deferred. Stored as a csys PlaneResult so
                // a sketch can be built on it (gates the origin + frame via the solid).
                const json oref = feat.contains("origin") ? feat.at("origin") : json("origin");
                Eigen::Vector3d o = resolve_point(oref, "origin");
                auto xax = feat.contains("x_dir") ? resolve_axis_ref(feat.at("x_dir")) : std::nullopt;
                if (feat.contains("x_dir") && !xax)
                    throw std::runtime_error("csys x_dir: must be a datum axis (edge selectors not ported)");
                auto yax = feat.contains("y_dir") ? resolve_axis_ref(feat.at("y_dir")) : std::nullopt;
                if (feat.contains("y_dir") && !yax)
                    throw std::runtime_error("csys y_dir: must be a datum axis (edge selectors not ported)");
                aicad::geom::SketchPlane p;
                if (!xax) {
                    p = aicad::geom::SketchPlane::top();
                    p.origin = o;
                } else {
                    // Gram-Schmidt: x = x_dir; y = y hint ⟂ x; z = x×y (matches datum.py
                    // _orthonormal_frame, then Plane(origin, x_dir=x, z_dir=z)).
                    Eigen::Vector3d x = xax->second.normalized();
                    Eigen::Vector3d y;
                    bool have_y = false;
                    if (yax) {
                        Eigen::Vector3d yh = yax->second;
                        Eigen::Vector3d proj = yh - x * x.dot(yh);
                        if (proj.norm() >= 1e-9) { y = proj.normalized(); have_y = true; }
                    }
                    if (!have_y) {
                        Eigen::Vector3d ref = (std::abs(x.z()) < 0.9) ? Eigen::Vector3d(0, 0, 1)
                                                                      : Eigen::Vector3d(1, 0, 0);
                        y = (ref - x * x.dot(ref)).normalized();
                    }
                    Eigen::Vector3d z = x.cross(y).normalized();
                    p = aicad::geom::SketchPlane::from(o, z, x);
                }
                results[id] = PlaneResult{p, true};
                st.ok = true;

            } else if (type == "helix") {
                // Curve feature (engine/doc/features/curves.py): build a helix / flat
                // spiral / variable-pitch wire in the base local frame. Ported:
                // constant-pitch helix (incl. taper + start_angle + handedness) and the
                // flat Archimedean spiral. Variable-pitch (pitch_table) is deferred.
                // refs.base = a datum plane / csys (origin + axis); standalone helices
                // fall back to the `radius` param. Stores a CurveResult a sweep consumes.
                const json bref = feat.contains("base") ? feat.at("base") : json("plane.top");
                auto bp = resolve_plane_ref(bref);
                if (!bp)
                    throw std::runtime_error(
                        "helix: base must be a datum plane/csys (geometry circle/cylinder "
                        "picks not yet ported here)");
                aicad::geom::SketchPlane base = *bp;

                if (feat.contains("pitch_table"))
                    throw std::runtime_error("helix: variable-pitch (pitch_table) not yet ported");

                const double start_angle =
                    feat.contains("start_angle") ? num_or_expr(feat.at("start_angle"), env) : 0.0;
                const bool ccw = feat.value("ccw", true);
                const bool is_spiral = feat.value("spiral", false) ||
                    (feat.contains("shape") && feat["shape"].is_string() &&
                     feat["shape"].get<std::string>() == "spiral");

                TopoDS_Wire local_wire;
                if (is_spiral) {
                    double pitch = feat.contains("pitch") ? num_or_expr(feat.at("pitch"), env) : 5.0;
                    if (!(pitch > 0.0)) throw std::runtime_error("helix: spiral pitch must be > 0");
                    double turns = feat.contains("turns") ? num_or_expr(feat.at("turns"), env) : 0.0;
                    if (!(turns > 0.0)) throw std::runtime_error("helix: spiral needs turns > 0");
                    double start_radius =
                        feat.contains("start_radius") ? num_or_expr(feat.at("start_radius"), env)
                        : (feat.contains("radius") ? num_or_expr(feat.at("radius"), env) : 10.0);
                    if (start_radius < 0.0)
                        throw std::runtime_error("helix: spiral start_radius must be >= 0");
                    const double sign = ccw ? 1.0 : -1.0;
                    const double a0 = radians(start_angle);
                    const double b = pitch / (2.0 * PI);
                    const double total_theta = 2.0 * PI * turns;
                    int n = std::max(2, (int)std::ceil(turns * 48));
                    std::vector<std::array<double, 3>> pts;
                    pts.reserve(n + 1);
                    for (int i = 0; i <= n; ++i) {
                        double theta = total_theta * i / n;
                        double r = start_radius + b * theta;
                        double ang = a0 + sign * theta;
                        pts.push_back({r * std::cos(ang), r * std::sin(ang), 0.0});
                    }
                    local_wire = ::geom::make_spline_wire(pts);
                } else {
                    double pitch = feat.contains("pitch") ? num_or_expr(feat.at("pitch"), env) : 5.0;
                    if (!(pitch > 0.0)) throw std::runtime_error("helix: pitch must be > 0");
                    double height = feat.contains("height") ? num_or_expr(feat.at("height"), env) : 20.0;
                    double taper = feat.contains("taper") ? num_or_expr(feat.at("taper"), env) : 0.0;
                    if (feat.contains("turns")) {
                        double turns = num_or_expr(feat.at("turns"), env);
                        if (!(turns > 0.0)) throw std::runtime_error("helix: turns must be > 0");
                        height = turns * pitch;
                    } else if (!(height > 0.0)) {
                        throw std::runtime_error("helix: height must be > 0");
                    }
                    double radius = feat.contains("radius") ? num_or_expr(feat.at("radius"), env) : 10.0;
                    if (!(radius > 0.0)) throw std::runtime_error("helix: radius must be > 0");
                    local_wire = ::geom::make_helix(pitch, height, radius, taper, start_angle, !ccw);
                }
                if (local_wire.IsNull())
                    throw std::runtime_error("helix: produced no curve geometry");
                results[id] = CurveResult{local_wire, base};
                st.ok = true;

            } else if (type == "loft") {
                // Loft a solid through >= 2 section profiles (region selectors naming
                // sketches). Each section -> its first face's outer wire -> ThruSections.
                auto sit = feat.find("sections");
                if (sit == feat.end() || !sit->is_array() || sit->size() < 2)
                    throw std::runtime_error("loft needs >= 2 sections");
                std::vector<TopoDS_Wire> wires;
                for (const json& s : *sit) {
                    const std::string sk = selector_owner_id(s);
                    if (sk.empty())
                        throw std::runtime_error("loft: malformed section selector");
                    const SketchResult& src = require_sketch(results, sk, sk.c_str());
                    if (src.faces.empty())
                        throw std::runtime_error("loft: section '" + sk + "' has no face");
                    TopoDS_Wire w = BRepTools::OuterWire(src.faces.front());
                    if (w.IsNull())
                        throw std::runtime_error("loft: section '" + sk + "' has no outer wire");
                    wires.push_back(w);
                }
                const bool ruled = feat.value("ruled", false);
                TopoDS_Shape solid = ::geom::loft_sections(wires, ruled);
                if (solid.IsNull())
                    throw std::runtime_error("loft produced an empty solid");

                results[id] = SolidResult{solid};
                solid_order.push_back(id);
                st.ok = true;

            } else if (type == "box" || type == "cylinder" || type == "sphere" ||
                       type == "cone" || type == "torus") {
                // Base primitives, centered on the origin (the SolidWorks/Python
                // default; the `at` placement datum is deferred). Port of
                // engine/doc/features/primitives.py.
                TopoDS_Shape solid;
                if (type == "box") {
                    solid = ::geom::make_box_centered(num_or_expr(feat.at("length"), env),
                                                      num_or_expr(feat.at("width"), env),
                                                      num_or_expr(feat.at("height"), env));
                } else if (type == "cylinder") {
                    solid = ::geom::make_cylinder_centered(num_or_expr(feat.at("radius"), env),
                                                           num_or_expr(feat.at("height"), env));
                } else if (type == "sphere") {
                    solid = ::geom::make_sphere(num_or_expr(feat.at("radius"), env));
                } else if (type == "cone") {
                    solid = ::geom::make_cone_centered(num_or_expr(feat.at("r1"), env),
                                                       num_or_expr(feat.at("r2"), env),
                                                       num_or_expr(feat.at("height"), env));
                } else {  // torus
                    solid = ::geom::make_torus(num_or_expr(feat.at("major_r"), env),
                                               num_or_expr(feat.at("minor_r"), env));
                }
                if (solid.IsNull())
                    throw std::runtime_error(type + " produced an empty solid");
                results[id] = SolidResult{solid};
                solid_order.push_back(id);
                st.ok = true;

            } else if (type == "import") {
                // Import a neutral B-rep file as a frozen body (engine/doc/features/
                // import_feature.py). Read the file via OCCT, then carry all imported
                // solids forward. Multi-body files become ONE compound body here (the
                // single-body case — the common one — is exact; total volume matches
                // the Python oracle either way; per-solid splitting is deferred).
                const std::string src_path = str_field(feat, "source_path", "");
                if (src_path.empty())
                    throw std::runtime_error("import feature missing 'source_path'");
                std::string ifmt = str_field(feat, "format", "");
                if (ifmt.empty()) ifmt = detect_format(src_path);
                std::vector<std::pair<std::string, TopoDS_Shape>> imported;
                try {
                    imported = import_bodies(src_path, ifmt);
                } catch (const OpError& oe) {
                    throw std::runtime_error("import failed for '" + src_path + "': " + oe.what());
                }
                std::vector<TopoDS_Shape> solids;
                for (auto& [name, shp] : imported)
                    if (!shp.IsNull()) solids.push_back(shp);
                if (solids.empty())
                    throw std::runtime_error("import: no usable bodies in '" + src_path + "'");
                TopoDS_Shape body;
                if (solids.size() == 1) {
                    body = solids.front();
                } else {
                    TopoDS_Compound comp;
                    BRep_Builder bb;
                    bb.MakeCompound(comp);
                    for (const TopoDS_Shape& s : solids) bb.Add(comp, s);
                    body = comp;
                }
                results[id] = SolidResult{body};
                solid_order.push_back(id);
                st.ok = true;

            } else if (type == "split") {
                // Split the running solid(s) by a planar tool into pieces (engine/doc/
                // features/split.py). PLANAR tool only here (a datum plane / reserved
                // plane; non-planar BOP surface split is deferred). keep="both" yields
                // both half-pieces, "above"/"below" keep one side along +normal. Each
                // resulting piece is a separate body — gated on exact piece volume.
                const std::string keep = feat.value("keep", std::string("both"));
                if (keep != "both" && keep != "above" && keep != "below")
                    throw std::runtime_error("split: unknown keep '" + keep + "'");
                if (!feat.contains("tool"))
                    throw std::runtime_error("split: refs.tool is required (a planar datum)");
                auto tp = resolve_plane_ref(feat.at("tool"));
                if (!tp)
                    throw std::runtime_error(
                        "split: tool is not a planar datum (face-selector tools not yet ported)");
                const Eigen::Vector3d point = tp->origin;
                const Eigen::Vector3d normal = tp->normal.normalized();

                // Gather surviving solids (the running bodies the plane acts on).
                std::vector<std::string> targets;
                for (const std::string& sid : solid_order)
                    if (!consumed.count(sid) &&
                        std::holds_alternative<SolidResult>(results.at(sid)))
                        targets.push_back(sid);
                if (targets.empty())
                    throw std::runtime_error("split: no body to split");

                // Reach must cover every body; half-space box near-face on the plane,
                // body extending +normal (mirrors split.py::_half_space exactly).
                double reach = 100.0;
                for (const std::string& sid : targets) {
                    Bnd_Box bb;
                    BRepBndLib::Add(std::get<SolidResult>(results.at(sid)).shape, bb);
                    if (bb.IsVoid()) continue;
                    double xmn, ymn, zmn, xmx, ymx, zmx;
                    bb.Get(xmn, ymn, zmn, xmx, ymx, zmx);
                    Eigen::Vector3d d(xmx - xmn, ymx - ymn, zmx - zmn);
                    reach = std::max(reach, std::max(d.norm(), 1e-3));
                }
                reach = reach * 2.0 + 10.0;

                // Half-space box: a 2*reach cube spanning u,v∈[-reach,reach] and
                // n∈[0,2*reach] in the plane's local frame, so its near face IS the
                // cutting plane and it fills the +normal side (mirrors split.py's
                // make_box(2r,2r,2r).translate(-r,-r,0) placed by the cut plane).
                aicad::geom::SketchPlane cut = aicad::geom::SketchPlane::from(point, normal);
                Eigen::Vector3d corner = cut.origin - cut.uAxis * reach - cut.vAxis * reach;
                gp_Ax2 box_frame(gp_Pnt(corner.x(), corner.y(), corner.z()),
                                 gp_Dir(cut.normal.x(), cut.normal.y(), cut.normal.z()),
                                 gp_Dir(cut.uAxis.x(), cut.uAxis.y(), cut.uAxis.z()));
                TopoDS_Shape half_space =
                    BRepPrimAPI_MakeBox(box_frame, 2 * reach, 2 * reach, 2 * reach).Shape();
                if (half_space.IsNull())
                    throw std::runtime_error("split: failed to build the half-space tool");

                std::vector<std::pair<std::string, TopoDS_Shape>> new_pieces;  // (src_id, shape)
                int split_count = 0;
                for (const std::string& sid : targets) {
                    const TopoDS_Shape& body = std::get<SolidResult>(results.at(sid)).shape;
                    TopoDS_Shape above = ::geom::common(body, half_space);
                    TopoDS_Shape below = ::geom::cut(body, half_space);
                    auto vol = [](const TopoDS_Shape& s) -> double {
                        if (s.IsNull()) return 0.0;
                        GProp_GProps g;
                        BRepGProp::VolumeProperties(s, g);
                        return g.Mass();
                    };
                    bool has_above = !above.IsNull() && vol(above) > 1e-9;
                    bool has_below = !below.IsNull() && vol(below) > 1e-9;
                    if (!(has_above && has_below)) {
                        // Plane misses / is tangent to this body: it survives unchanged.
                        continue;
                    }
                    ++split_count;
                    consumed.insert(sid);
                    if (keep == "above") {
                        new_pieces.emplace_back(sid, above);
                    } else if (keep == "below") {
                        new_pieces.emplace_back(sid, below);
                    } else {  // both
                        new_pieces.emplace_back(sid, above);
                        new_pieces.emplace_back(sid, below);
                    }
                }
                if (split_count == 0)
                    throw std::runtime_error("split: the tool plane does not intersect any body");

                // Register each kept piece as a new solid result (id = "<fid>#k").
                int k = 0;
                for (auto& [src_id, shp] : new_pieces) {
                    std::string pid = (k == 0 && new_pieces.size() == 1) ? id
                                                                         : id + "#" + std::to_string(k);
                    results[pid] = SolidResult{shp};
                    solid_order.push_back(pid);
                    source_of[pid] = src_id;  // owner-map: piece derives from the split body
                    ++k;
                }
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
                    source_of[id] = tgt_id;  // owner-map: cut derives from the target body
                    results[id] = SolidResult{out};
                } else {
                    results[id] = SolidResult{solid};
                }
                solid_order.push_back(id);
                st.ok = true;

            } else if (type == "revolve") {
                const std::string prof = req_str(feat, "profile");
                const SketchResult& src = require_sketch(results, prof, prof.c_str());
                // Axis: either pre-translated reserved-datum vecs (axis_pt/axis_dir)
                // or a datum-axis FEATURE ref left in feat["axis"] (resolve here so a
                // revolve can spin about an authored `axis` datum — the datum's gate).
                Eigen::Vector3d axis_pt, axis_dir;
                if (feat.contains("axis_pt") && feat.contains("axis_dir")) {
                    axis_pt = req_vec3(feat, "axis_pt");
                    axis_dir = req_vec3(feat, "axis_dir");
                } else if (feat.contains("axis")) {
                    auto ax = resolve_axis_ref(feat.at("axis"));
                    if (!ax) throw std::runtime_error("revolve: axis is not a datum axis");
                    axis_pt = ax->first;
                    axis_dir = ax->second;
                } else {
                    throw std::runtime_error("revolve: missing axis");
                }
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

            } else if (type == "sweep") {
                // Sweep profile region face(s) along a path sketch's raw wire
                // (BRepOffsetAPI_MakePipe). Port of engine/doc/features/sweep.py's
                // MakePipe fast path (guides/twist/orientation/thin deferred).
                const std::string prof = req_str(feat, "profile");
                const SketchResult& src = require_sketch(results, prof, prof.c_str());
                if (src.faces.empty())
                    throw std::runtime_error("sweep: profile sketch has no closed face");

                auto pit = feat.find("path");
                if (pit == feat.end())
                    throw std::runtime_error("sweep: a 'path' ref is required");
                const std::string path_id = selector_owner_id(*pit);
                if (path_id.empty())
                    throw std::runtime_error("sweep: malformed path ref");
                // The path may be a SKETCH (raw wire) OR a CURVE feature (a helix /
                // spiral, lifted from its local frame through the curve's base plane).
                TopoDS_Wire spine;
                auto path_it = results.find(path_id);
                if (path_it != results.end() &&
                    std::holds_alternative<CurveResult>(path_it->second)) {
                    const CurveResult& cr = std::get<CurveResult>(path_it->second);
                    // Lift local (x,y,z) -> origin + x*u + y*v + z*n (the base frame).
                    const aicad::geom::SketchPlane& b = cr.base;
                    gp_Trsf lift;
                    lift.SetValues(
                        b.uAxis.x(), b.vAxis.x(), b.normal.x(), b.origin.x(),
                        b.uAxis.y(), b.vAxis.y(), b.normal.y(), b.origin.y(),
                        b.uAxis.z(), b.vAxis.z(), b.normal.z(), b.origin.z());
                    spine = TopoDS::Wire(BRepBuilderAPI_Transform(cr.wire, lift, true).Shape());
                } else {
                    const SketchResult& pathsk = require_sketch(results, path_id, "path");
                    spine = aicad::geom::wire_from_solved_sketch(pathsk.entities, pathsk.plane);
                }
                if (spine.IsNull())
                    throw std::runtime_error("sweep: path sketch produced no wire");

                TopoDS_Shape solid;
                for (const TopoDS_Face& f : src.faces) {
                    TopoDS_Shape pipe = ::geom::sweep_face(f, spine);
                    if (pipe.IsNull())
                        throw std::runtime_error("sweep produced an empty solid");
                    solid = solid.IsNull() ? pipe : ::geom::fuse(solid, pipe);
                    if (solid.IsNull())
                        throw std::runtime_error("sweep region fuse failed");
                }
                if (solid.IsNull())
                    throw std::runtime_error("sweep produced no solid");

                results[id] = SolidResult{solid};
                solid_order.push_back(id);
                st.ok = true;

            } else if (type == "rib") {
                // Thin support wall grown from an OPEN sketch contour: silhouette
                // dropped in-plane toward the body, thickened across the sketch
                // normal, trimmed to the body bbox, fused. Port of the parallel/
                // centered path of engine/doc/features/rib.py.
                const std::string body_id = body_ref(feat);
                if (body_id.empty())
                    throw std::runtime_error("rib: no body ref and no current body");
                const TopoDS_Shape& body = require_solid(results, body_id, "body");
                const double thickness =
                    feat.contains("thickness") ? num_or_expr(feat.at("thickness"), env) : 3.0;
                if (!(thickness > 0.0))
                    throw std::runtime_error("rib thickness must be > 0");

                const std::string prof = req_str(feat, "profile");
                const SketchResult& src = require_sketch(results, prof, prof.c_str());
                TopoDS_Wire contour =
                    aicad::geom::wire_from_solved_sketch(src.entities, src.plane);
                if (contour.IsNull())
                    throw std::runtime_error("rib: profile sketch produced no contour");

                // Body bbox -> center (for the inward drop) + a clip box (+5 margin).
                Bnd_Box bb;
                BRepBndLib::Add(body, bb);
                double xmn, ymn, zmn, xmx, ymx, zmx;
                bb.Get(xmn, ymn, zmn, xmx, ymx, zmx);
                Eigen::Vector3d bc((xmn + xmx) / 2, (ymn + ymx) / 2, (zmn + zmx) / 2);

                TopoDS_Vertex v1, v2;
                TopExp::Vertices(contour, v1, v2);
                if (v1.IsNull() || v2.IsNull())
                    throw std::runtime_error("rib: contour is closed (a rib needs an open contour)");
                gp_Pnt q1 = BRep_Tool::Pnt(v1), q2 = BRep_Tool::Pnt(v2);
                Eigen::Vector3d mid((q1.X() + q2.X()) / 2, (q1.Y() + q2.Y()) / 2,
                                    (q1.Z() + q2.Z()) / 2);
                Eigen::Vector3d toward = bc - mid;
                const double reach = aicad::doc::bbox_diag(body) * 1.5;

                TopoDS_Shape slab = ::geom::make_rib_slab(contour, src.plane.normal,
                                                          toward, thickness, reach);
                if (slab.IsNull())
                    throw std::runtime_error("rib: could not build the rib slab");

                // Trim the slab to the body bbox (+5) so the drop doesn't dangle.
                TopoDS_Shape clip = ::geom::make_box_at(xmn - 5, ymn - 5, zmn - 5,
                                                        (xmx - xmn) + 10, (ymx - ymn) + 10,
                                                        (zmx - zmn) + 10);
                TopoDS_Shape trimmed = ::geom::common(slab, clip);
                if (trimmed.IsNull()) trimmed = slab;  // keep untrimmed; fuse still works

                GProp_GProps gb, ga;
                BRepGProp::VolumeProperties(body, gb);
                TopoDS_Shape out = ::geom::fuse(body, trimmed);
                if (out.IsNull())
                    throw std::runtime_error("rib: fusing the rib to the body failed");
                BRepGProp::VolumeProperties(out, ga);
                if (ga.Mass() - gb.Mass() <= 1e-6)
                    throw std::runtime_error("rib added no material (contour must bridge the body)");

                consumed.insert(body_id);
                source_of[id] = body_id;  // owner-map: derives from the input body
                results[id] = SolidResult{out};
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
                source_of[id] = a_id;  // owner-map: result derives from the first target

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
                // beveled solid is the current state of that body in the tree. Recording
                // `source_of` here (it was previously omitted) is what lets the owner-map
                // diff keep the unchanged faces with the ORIGINAL feature instead of
                // attributing the whole body to this dress feature (P14).
                consumed.insert(body_id);

                source_of[id] = body_id;  // owner-map: derives from the input body
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
                source_of[id] = seed_id;  // owner-map: pattern derives from the seed body

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
                source_of[id] = seed_id;  // owner-map: pattern derives from the seed body

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
                source_of[id] = body_id;  // owner-map: derives from the input body
                results[id] = SolidResult{out};
                solid_order.push_back(id);
                st.ok = true;

            } else if (type == "shell") {
                // Hollow the current body to a wall thickness, opening any `remove`
                // faces (face selectors). Port of engine/doc/features/dress.py shell.
                const std::string body_id = body_ref(feat);
                if (body_id.empty())
                    throw std::runtime_error("shell: no body ref and no current body");
                const TopoDS_Shape& body = require_solid(results, body_id, "body");
                const double t = num_or_expr(feat.at("thickness"), env);
                if (!(t > 0.0))
                    throw std::runtime_error("shell thickness must be > 0");

                std::vector<TopoDS_Face> remove;
                auto rm = feat.find("remove");
                if (rm != feat.end() &&
                    (rm->is_object() || (rm->is_array() && !rm->empty()))) {
                    aicad::doc::FacePicks fp =
                        aicad::doc::resolve_face_picks(body, *rm, "remove");
                    if (!fp.dangling.empty()) {
                        std::string msg = "shell: remove selector dangling: ";
                        for (std::size_t k = 0; k < fp.dangling.size(); ++k) {
                            if (k) msg += "; ";
                            msg += fp.dangling[k];
                        }
                        throw std::runtime_error(msg);
                    }
                    remove = std::move(fp.faces);
                }

                const bool outward = feat.value("outward", false);
                TopoDS_Shape out = ::geom::shell_solid(body, t, outward, remove);
                if (out.IsNull())
                    throw std::runtime_error("shell produced an empty shape");
                consumed.insert(body_id);
                source_of[id] = body_id;  // owner-map: derives from the input body
                results[id] = SolidResult{out};
                solid_order.push_back(id);
                st.ok = true;

            } else if (type == "draft") {
                // Taper the selected faces about a neutral plane (datum or face) by an
                // angle (degrees). Port of engine/doc/features/draft.py neutral_plane mode.
                const std::string body_id = body_ref(feat);
                if (body_id.empty())
                    throw std::runtime_error("draft: no body ref and no current body");
                const TopoDS_Shape& body = require_solid(results, body_id, "body");
                const double angle = radians(num_or_expr(feat.at("angle"), env));
                if (!(std::abs(angle) > 0.0))
                    throw std::runtime_error("draft angle must be non-zero");

                // Neutral plane: a reserved datum (normalize set plane_point/normal) or
                // a face selector under "neutral".
                Eigen::Vector3d npt, nnl;
                if (feat.contains("plane_point") && feat.contains("plane_normal")) {
                    npt = req_vec3(feat, "plane_point");
                    nnl = req_vec3(feat, "plane_normal");
                } else {
                    auto nit = feat.find("neutral");
                    if (nit == feat.end() || !nit->is_object())
                        throw std::runtime_error("draft: a 'neutral' plane (datum or face) is required");
                    aicad::doc::FacePicks fp =
                        aicad::doc::resolve_face_picks(body, *nit, "neutral");
                    if (!fp.dangling.empty() || fp.faces.empty())
                        throw std::runtime_error("draft: neutral face not resolved");
                    BRepAdaptor_Surface surf(fp.faces[0]);
                    if (surf.GetType() != GeomAbs_Plane)
                        throw std::runtime_error("draft: neutral face is not planar");
                    gp_Pln pln = surf.Plane();
                    gp_Pnt loc = pln.Location();
                    gp_Dir nd = pln.Axis().Direction();
                    npt = Eigen::Vector3d(loc.X(), loc.Y(), loc.Z());
                    nnl = Eigen::Vector3d(nd.X(), nd.Y(), nd.Z());
                }
                // Pull defaults to the neutral normal (SolidWorks default).
                Eigen::Vector3d pull = opt_vec3(feat, "pull_dir").value_or(nnl);

                auto fit = feat.find("faces");
                if (fit == feat.end() ||
                    !(fit->is_object() || (fit->is_array() && !fit->empty())))
                    throw std::runtime_error("draft: 'faces' selector(s) required");
                aicad::doc::FacePicks fp =
                    aicad::doc::resolve_face_picks(body, *fit, "faces");
                if (!fp.dangling.empty()) {
                    std::string msg = "draft: faces selector dangling: ";
                    for (std::size_t k = 0; k < fp.dangling.size(); ++k) {
                        if (k) msg += "; ";
                        msg += fp.dangling[k];
                    }
                    throw std::runtime_error(msg);
                }
                if (fp.faces.empty())
                    throw std::runtime_error("draft: no faces resolved");

                TopoDS_Shape out =
                    ::geom::draft_faces(body, fp.faces, npt, nnl, pull, angle);
                if (out.IsNull())
                    throw std::runtime_error("draft produced an empty shape");
                consumed.insert(body_id);
                source_of[id] = body_id;  // owner-map: derives from the input body
                results[id] = SolidResult{out};
                solid_order.push_back(id);
                st.ok = true;

            } else if (type == "sm_base_flange") {
                // Base flange — extrude a sketch region by `thickness` to make the
                // base sheet. Establishes the part as sheet metal (stores thickness /
                // k-factor / default bend radius so later flanges inherit them). Port
                // of engine/doc/features/sheetmetal.py::compile_sm_base_flange.
                const std::string prof = req_str(feat, "profile");
                const SketchResult& src = require_sketch(results, prof, prof.c_str());
                if (src.faces.empty())
                    throw std::runtime_error("sm_base_flange: profile sketch has no closed face");

                const double thickness = num_or_expr(feat.at("thickness"), env);
                const double k_factor = feat.contains("k_factor")
                    ? num_or_expr(feat.at("k_factor"), env) : 0.45;
                const double default_bend_radius = feat.contains("default_bend_radius")
                    ? num_or_expr(feat.at("default_bend_radius"), env) : thickness;
                if (!(thickness > 0.0))
                    throw std::runtime_error("sm_base_flange: thickness must be > 0");
                if (!(k_factor >= 0.0 && k_factor <= 1.0))
                    throw std::runtime_error("sm_base_flange: k_factor must be in 0..1");

                // Extrude each region face along its plane normal by thickness, union.
                TopoDS_Shape sheet;
                for (const TopoDS_Face& fc : src.faces) {
                    TopoDS_Shape piece =
                        ::geom::extrude_face(fc, src.plane.normal, thickness, false);
                    if (piece.IsNull())
                        throw std::runtime_error("sm_base_flange: extrude failed");
                    sheet = sheet.IsNull() ? piece : ::geom::fuse(sheet, piece);
                    if (sheet.IsNull())
                        throw std::runtime_error("sm_base_flange: region fuse failed");
                }
                if (sheet.IsNull())
                    throw std::runtime_error("sm_base_flange: produced no solid");

                sheet_ctx.active = true;
                sheet_ctx.thickness = thickness;
                sheet_ctx.k_factor = k_factor;
                sheet_ctx.default_bend_radius = default_bend_radius;

                results[id] = SolidResult{sheet};
                solid_order.push_back(id);
                st.ok = true;

            } else if (type == "sm_edge_flange" || type == "sm_hem" ||
                       type == "sm_jog") {
                // Bent walls off a straight bend edge of the sheet's thickness face.
                // Each resolves the SAME bend-edge frame (sm_bend_frame) and builds a
                // true bend+wall (edge flange) or constant-thickness strip (hem / jog)
                // solid, fused to the running sheet, then appends a bend record for
                // the flat pattern. Ports of compile_sm_edge_flange / _hem / _jog.
                if (!sheet_ctx.active)
                    throw std::runtime_error(type + ": part is not sheet metal — add a base flange first");
                const std::string body_id = current_solid_id();
                if (body_id.empty())
                    throw std::runtime_error(type + ": no sheet body — add a base flange first");
                const TopoDS_Shape& body = require_solid(results, body_id, "body");
                const double thickness = sheet_ctx.thickness;
                const double k_default = sheet_ctx.k_factor;
                const double radius_default = sheet_ctx.default_bend_radius;

                // Resolve the single straight bend edge (reuse the dress edge picker).
                auto edge_it = feat.find("edge");
                if (edge_it == feat.end())
                    throw std::runtime_error(type + ": refs.edge is required (the bend line)");
                aicad::doc::EdgePicks picks =
                    aicad::doc::resolve_edge_picks(body, *edge_it);
                if (!picks.dangling.empty()) {
                    std::string msg = type + ": edge selector dangling: ";
                    for (std::size_t k = 0; k < picks.dangling.size(); ++k) {
                        if (k) msg += "; ";
                        msg += picks.dangling[k];
                    }
                    throw std::runtime_error(msg);
                }
                if (picks.edges.empty())
                    throw std::runtime_error(type + ": bend edge not resolved");

                ::geom::BendFrame fr =
                    ::geom::sm_bend_frame(body, picks.edges[0], thickness);
                if (!fr.ok) throw std::runtime_error(fr.error);

                const bool flip = feat.value("flip", false);
                Eigen::Vector3d fold_dir = fr.n_broad * (flip ? -1.0 : 1.0);
                fold_dir.normalize();

                TopoDS_Shape tool;
                double rec_angle = 0.0, rec_radius = 0.0, rec_wall = 0.0;
                std::string rec_position = "material_inside";
                // A jog appends TWO bend records (the up bend + the back bend).
                std::vector<::geom::BendRecord> new_recs;

                if (type == "sm_edge_flange") {
                    const double angle_deg = feat.contains("angle")
                        ? num_or_expr(feat.at("angle"), env) : 90.0;
                    const double wall = feat.contains("length")
                        ? num_or_expr(feat.at("length"), env) : 20.0;
                    const double inner_radius = feat.contains("radius")
                        ? num_or_expr(feat.at("radius"), env) : radius_default;
                    const double k_factor = feat.contains("k_factor")
                        ? num_or_expr(feat.at("k_factor"), env) : k_default;
                    const std::string position = feat.value("position", std::string("material_inside"));
                    if (!(inner_radius > 0.0))
                        throw std::runtime_error("sm_edge_flange: bend radius must be > 0");
                    if (!(wall > 0.0))
                        throw std::runtime_error("sm_edge_flange: flange length must be > 0");
                    if (!(std::abs(angle_deg) >= 1e-6 && std::abs(angle_deg) < 180.0))
                        throw std::runtime_error("sm_edge_flange: angle must be in (0, 180) degrees");

                    tool = ::geom::sm_edge_flange_solid(
                        fr.p_start, fr.e_dir, fr.length_dir, fold_dir, fr.edge_length,
                        thickness, inner_radius, angle_deg, wall, position);
                    rec_angle = angle_deg; rec_radius = inner_radius; rec_wall = wall;
                    rec_position = position;
                    ::geom::BendRecord rec;
                    rec.angle = angle_deg; rec.radius = inner_radius;
                    rec.thickness = thickness;
                    rec.bend_allowance = ::geom::sm_bend_allowance(angle_deg, inner_radius,
                                                                   thickness, k_factor);
                    rec.wall_length = wall; rec.position = position;
                    rec.p_start = fr.p_start; rec.p_end = fr.p_end;
                    rec.e_dir = fr.e_dir; rec.length_dir = fr.length_dir;
                    new_recs.push_back(rec);

                } else if (type == "sm_hem") {
                    const std::string htype = feat.value("hem_type", std::string("closed"));
                    if (htype != "closed" && htype != "open" && htype != "teardrop")
                        throw std::runtime_error("sm_hem: type must be closed|open|teardrop");
                    double length = feat.contains("length")
                        ? num_or_expr(feat.at("length"), env) : 5.0 * thickness;
                    const double radius_default_hem =
                        (htype == "closed") ? (thickness / 2.0) : thickness;
                    const double inner_radius = feat.contains("radius")
                        ? num_or_expr(feat.at("radius"), env) : radius_default_hem;
                    const double k_factor = feat.contains("k_factor")
                        ? num_or_expr(feat.at("k_factor"), env) : k_default;
                    if (!(inner_radius > 0.0))
                        throw std::runtime_error("sm_hem: bend radius must be > 0");
                    length = std::max(length, 1.5 * thickness);

                    const double bend_angle = (htype == "teardrop") ? 235.0 : 180.0;
                    const double Rc = inner_radius + thickness / 2.0;
                    const double start_x = -(inner_radius + thickness);
                    const double start_y = -thickness / 2.0;
                    const double pre_run = inner_radius + thickness;
                    std::vector<::geom::StripOp> ops = {
                        {::geom::StripOp::Run, pre_run, 0.0, true},
                        {::geom::StripOp::Bend, bend_angle, Rc, true},
                        {::geom::StripOp::Run, length, 0.0, true}};
                    tool = ::geom::sm_strip_from_centerline(
                        fr.p_start, fr.e_dir, fr.length_dir, fold_dir, fr.edge_length,
                        thickness, ops, start_x, start_y);
                    rec_angle = bend_angle; rec_radius = inner_radius; rec_wall = length;
                    ::geom::BendRecord rec;
                    rec.angle = bend_angle; rec.radius = inner_radius;
                    rec.thickness = thickness;
                    rec.bend_allowance = ::geom::sm_bend_allowance(bend_angle, inner_radius,
                                                                   thickness, k_factor);
                    rec.wall_length = length; rec.position = "material_inside";
                    rec.p_start = fr.p_start; rec.p_end = fr.p_end;
                    rec.e_dir = fr.e_dir; rec.length_dir = fr.length_dir;
                    new_recs.push_back(rec);

                } else {  // sm_jog
                    const double angle_deg = feat.contains("angle")
                        ? num_or_expr(feat.at("angle"), env) : 90.0;
                    const double offset = feat.contains("offset")
                        ? num_or_expr(feat.at("offset"), env) : 10.0;
                    const double wall = feat.contains("length")
                        ? num_or_expr(feat.at("length"), env) : 20.0;
                    const double inner_radius = feat.contains("radius")
                        ? num_or_expr(feat.at("radius"), env) : radius_default;
                    const double k_factor = feat.contains("k_factor")
                        ? num_or_expr(feat.at("k_factor"), env) : k_default;
                    const std::string position = feat.value("position", std::string("material_inside"));
                    if (!(inner_radius > 0.0))
                        throw std::runtime_error("sm_jog: bend radius must be > 0");
                    if (!(wall > 0.0))
                        throw std::runtime_error("sm_jog: flange length must be > 0");
                    if (!(std::abs(angle_deg) >= 1e-6 && std::abs(angle_deg) <= 90.0 + 1e-6))
                        throw std::runtime_error("sm_jog: angle must be in (0, 90] degrees");

                    const double ang = radians(angle_deg);
                    const double Rc = inner_radius + thickness / 2.0;
                    const double bend_rise = 2.0 * Rc * (1.0 - std::cos(ang));
                    const double needed = offset - bend_rise;
                    double connector = (std::sin(ang) > 1e-6) ? needed / std::sin(ang) : 0.0;
                    if (connector < 0.0) connector = 0.05 * thickness;

                    double x0;
                    if (position == "material_inside")       x0 = -(inner_radius + thickness);
                    else if (position == "material_outside") x0 = -inner_radius;
                    else                                     x0 = 0.0;
                    double pre_run = -x0;
                    if (pre_run < 0) pre_run = 0.0;
                    const double start_x = x0;
                    const double start_y = -thickness / 2.0;
                    std::vector<::geom::StripOp> ops = {
                        {::geom::StripOp::Run, pre_run + thickness, 0.0, true},
                        {::geom::StripOp::Bend, angle_deg, Rc, true},
                        {::geom::StripOp::Run, connector, 0.0, true},
                        {::geom::StripOp::Bend, angle_deg, Rc, false},
                        {::geom::StripOp::Run, wall, 0.0, true}};
                    tool = ::geom::sm_strip_from_centerline(
                        fr.p_start, fr.e_dir, fr.length_dir, fold_dir, fr.edge_length,
                        thickness, ops, start_x, start_y);
                    rec_angle = angle_deg; rec_radius = inner_radius; rec_wall = wall;
                    rec_position = position;
                    // up bend (wall = connector) + back bend (wall = length).
                    for (double w : {connector, wall}) {
                        ::geom::BendRecord rec;
                        rec.angle = angle_deg; rec.radius = inner_radius;
                        rec.thickness = thickness;
                        rec.bend_allowance = ::geom::sm_bend_allowance(angle_deg, inner_radius,
                                                                       thickness, k_factor);
                        rec.wall_length = w; rec.position = position;
                        rec.p_start = fr.p_start; rec.p_end = fr.p_end;
                        rec.e_dir = fr.e_dir; rec.length_dir = fr.length_dir;
                        new_recs.push_back(rec);
                    }
                }
                (void)rec_angle; (void)rec_radius; (void)rec_wall; (void)rec_position;

                if (tool.IsNull())
                    throw std::runtime_error(type + ": geometry build failed");
                GProp_GProps gt;
                BRepGProp::VolumeProperties(tool, gt);
                if (gt.Mass() <= 1e-9)
                    throw std::runtime_error(type + ": produced an empty solid");

                TopoDS_Shape fused = ::geom::fuse(body, tool);
                if (fused.IsNull())
                    throw std::runtime_error(type + ": fuse to sheet failed");
                // A valid wall shares a face with the sheet -> ONE connected solid.
                int nsolids = 0;
                for (TopExp_Explorer sx(fused, TopAbs_SOLID); sx.More(); sx.Next()) ++nsolids;
                if (nsolids > 1)
                    throw std::runtime_error(type + ": result is not connected to the sheet");
                GProp_GProps gb, ga;
                BRepGProp::VolumeProperties(body, gb);
                BRepGProp::VolumeProperties(fused, ga);
                if (ga.Mass() <= gb.Mass() + 1e-9)
                    throw std::runtime_error(type + ": added no material (degenerate placement?)");

                for (const ::geom::BendRecord& r : new_recs) bend_records.push_back(r);
                consumed.insert(body_id);
                source_of[id] = body_id;
                results[id] = SolidResult{fused};
                solid_order.push_back(id);
                st.ok = true;

            } else if (type == "sm_flat_pattern") {
                // Unfold the running sheet-metal body into its FLAT PATTERN (developed
                // blank): rebuild the flat base from the largest broad face + add one
                // developed strip (BA + wall) per bend record. Port of
                // engine/doc/features/sheetmetal_flat.py::compile_sm_flat_pattern.
                if (!sheet_ctx.active)
                    throw std::runtime_error("sm_flat_pattern: the active body is not sheet metal");
                const std::string body_id = current_solid_id();
                if (body_id.empty())
                    throw std::runtime_error("sm_flat_pattern: no body to flatten");
                const TopoDS_Shape& body = require_solid(results, body_id, "body");
                if (bend_records.empty())
                    throw std::runtime_error("sm_flat_pattern: the part has no bends to unfold");

                TopoDS_Shape flat = ::geom::sm_flat_pattern_solid(
                    body, sheet_ctx.thickness, bend_records);
                if (flat.IsNull())
                    throw std::runtime_error("sm_flat_pattern: produced an empty flat body");
                GProp_GProps gf;
                BRepGProp::VolumeProperties(flat, gf);
                if (gf.Mass() <= 1e-9)
                    throw std::runtime_error("sm_flat_pattern: produced an empty flat body");

                consumed.insert(body_id);
                source_of[id] = body_id;
                results[id] = SolidResult{flat};
                solid_order.push_back(id);
                st.ok = true;

            } else if (type == "weld_member") {
                // Structural weldment members: sweep a library profile perpendicular
                // to each selected sketch segment (BRepOffsetAPI_MakePipe), one
                // SEPARATE body per segment (an assembly of members). Port of
                // engine/doc/features/weldment.py::compile_weld_member.
                //
                // PARITY NOTE: corner trimming (miter/butt/cope) is NOT ported — the
                // C++ fixtures use members that do not share a vertex (or a single
                // member), so trim_corners is a no-op and each member's volume is the
                // exact swept prism area*length. A trimmed-corner fixture would need
                // the boolean half-space / cope ops ported; flagged out-of-scope here.
                const std::string profile = feat.value("profile", std::string("square_tube"));
                if (!::geom::weld_profile_known(profile))
                    throw std::runtime_error("weld_member: unknown profile '" + profile + "'");
                const std::string justify = feat.value("justify", std::string("center"));
                if (justify != "center" && justify != "corner")
                    throw std::runtime_error("weld_member: unknown justify '" + justify + "'");
                const double angle = feat.contains("angle") && !feat.at("angle").is_null()
                    ? num_or_expr(feat.at("angle"), env) : 0.0;

                // The source sketch (its raw segments become the member paths).
                const std::string sid = req_str(feat, "sketch");
                const SketchResult& src = require_sketch(results, sid, sid.c_str());
                // Optional segment filter: a list of entity ids (default = all).
                std::set<std::string> wanted;
                bool have_filter = false;
                auto seg_it = feat.find("segments");
                if (seg_it != feat.end() && seg_it->is_array()) {
                    have_filter = true;
                    for (const json& s : *seg_it)
                        if (s.is_string()) wanted.insert(s.get<std::string>());
                }

                // Build the profile face once (local frame); reused per segment.
                double area = 0.0;
                std::string perr;
                TopoDS_Face face_local = ::geom::weld_profile_face(
                    profile, feat, justify, angle, area, perr);
                if (face_local.IsNull())
                    throw std::runtime_error("weld_member: " + perr);

                // Resolve (entity_id, world-space line edge) pairs to sweep along.
                // Only straight LINE segments are swept (the ported subset); a closed
                // or construction entity is skipped, exactly like the oracle.
                struct Seg { std::string eid; TopoDS_Edge edge; Eigen::Vector3d start, tangent; };
                std::vector<Seg> segs;
                const json& ents = src.entities;
                if (ents.is_object()) {
                    for (auto eit = ents.begin(); eit != ents.end(); ++eit) {
                        const json& ent = eit.value();
                        if (!ent.is_object()) continue;
                        if (ent.value("construction", false)) continue;
                        const std::string eid = ent.value("id", eit.key());
                        if (have_filter && !wanted.count(eid)) continue;
                        if (ent.value("type", std::string()) != "line") continue;  // lines only
                        if (!ent.contains("p1") || !ent.contains("p2")) continue;
                        Eigen::Vector2d a(ent["p1"][0].get<double>(), ent["p1"][1].get<double>());
                        Eigen::Vector2d b(ent["p2"][0].get<double>(), ent["p2"][1].get<double>());
                        if ((a - b).norm() < 1e-9) continue;
                        Eigen::Vector3d wa = src.plane.lift(a), wb = src.plane.lift(b);
                        BRepBuilderAPI_MakeEdge me(gp_Pnt(wa.x(), wa.y(), wa.z()),
                                                   gp_Pnt(wb.x(), wb.y(), wb.z()));
                        if (!me.IsDone()) continue;
                        Seg sg;
                        sg.eid = eid;
                        sg.edge = me.Edge();
                        sg.start = wa;
                        sg.tangent = (wb - wa).normalized();
                        segs.push_back(std::move(sg));
                    }
                }
                if (segs.empty())
                    throw std::runtime_error("weld_member: sketch has no usable segments to sweep along");

                // One SEPARATE surviving body per member. Store each under a synthetic
                // id (feature_id#k) so the surviving-body set + count + total volume
                // match the oracle (which appends each member to ctx.bodies).
                int k = 0;
                for (const Seg& sg : segs) {
                    TopoDS_Shape member =
                        ::geom::weld_sweep_member(face_local, sg.edge, sg.start, sg.tangent);
                    if (member.IsNull())
                        throw std::runtime_error("weld_member: sweep failed for segment '" + sg.eid + "'");
                    GProp_GProps gm;
                    BRepGProp::VolumeProperties(member, gm);
                    if (gm.Mass() <= 1e-9)
                        throw std::runtime_error("weld_member: produced an empty member solid");
                    std::string mid = id + "#" + std::to_string(k++);
                    results[mid] = SolidResult{member};
                    solid_order.push_back(mid);
                }
                st.ok = true;

            } else if (type == "weld_end_cap" || type == "weld_gusset") {
                // Dress weldment features: a plate resolved from picked planar face(s)
                // on the EXISTING member bodies. Both are SEPARATE new bodies appended
                // to the rolling list (the members are NOT consumed). Ports of
                // compile_weld_end_cap / compile_weld_gusset.
                //
                // Face picks resolve against ALL current surviving bodies (members are
                // kept separate), keeping the candidate with the smallest centroid
                // distance — the C++ analogue of weldment.py::_resolve_face_any_body.
                std::vector<std::string> live;
                for (const std::string& s : solid_order)
                    if (!consumed.count(s)) live.push_back(s);
                if (live.empty())
                    throw std::runtime_error(type + ": no member bodies — add weld_member first");

                // Resolve ONE face selector against every live body, returning the
                // best (smallest selector-centroid distance) face.
                auto resolve_face_any = [&](const json& sel, const char* slot) -> TopoDS_Face {
                    if (!sel.is_object() || sel.value("kind", std::string()) != "face")
                        throw std::runtime_error(std::string(slot) + ": a planar face selector is required");
                    std::array<double, 3> want{0, 0, 0};
                    bool have_want = false;
                    auto cit = sel.find("centroid");
                    if (cit != sel.end() && cit->is_array() && cit->size() == 3) {
                        want = {(*cit)[0].get<double>(), (*cit)[1].get<double>(), (*cit)[2].get<double>()};
                        have_want = true;
                    }
                    TopoDS_Face best;
                    double best_d = std::numeric_limits<double>::max();
                    for (const std::string& bid : live) {
                        const TopoDS_Shape& body = std::get<SolidResult>(results.at(bid)).shape;
                        aicad::doc::FacePicks fp = aicad::doc::resolve_face_picks(body, sel, slot);
                        for (const TopoDS_Face& fc : fp.faces) {
                            double d = 0.0;
                            if (have_want) {
                                GProp_GProps gp;
                                BRepGProp::SurfaceProperties(fc, gp);
                                gp_Pnt c = gp.CentreOfMass();
                                d = std::sqrt((c.X() - want[0]) * (c.X() - want[0]) +
                                              (c.Y() - want[1]) * (c.Y() - want[1]) +
                                              (c.Z() - want[2]) * (c.Z() - want[2]));
                            }
                            if (d < best_d) { best_d = d; best = fc; }
                        }
                    }
                    if (best.IsNull())
                        throw std::runtime_error(std::string(slot) + ": face selector did not resolve on any member body");
                    return best;
                };

                // Planar face normal (build123d normal_at: geometric normal, flipped
                // when the face is REVERSED) — matches the oracle's _require_planar.
                auto face_normal = [&](const TopoDS_Face& fc, const char* what) -> Eigen::Vector3d {
                    BRepAdaptor_Surface surf(fc);
                    if (surf.GetType() != GeomAbs_Plane)
                        throw std::runtime_error(std::string(what) + ": the picked face is not planar");
                    gp_Dir d = surf.Plane().Axis().Direction();
                    Eigen::Vector3d n(d.X(), d.Y(), d.Z());
                    if (fc.Orientation() == TopAbs_REVERSED) n = -n;
                    if (n.norm() < 1e-9)
                        throw std::runtime_error(std::string(what) + ": degenerate face normal");
                    return n.normalized();
                };
                auto face_center = [&](const TopoDS_Face& fc) -> Eigen::Vector3d {
                    GProp_GProps gp;
                    BRepGProp::SurfaceProperties(fc, gp);
                    gp_Pnt c = gp.CentreOfMass();
                    return Eigen::Vector3d(c.X(), c.Y(), c.Z());
                };

                TopoDS_Shape plate;
                if (type == "weld_end_cap") {
                    const double thickness = feat.contains("thickness")
                        ? num_or_expr(feat.at("thickness"), env) : 5.0;
                    const std::string cap_type = feat.value("hem_type", feat.value("type", std::string("outer")));
                    const double offset = feat.contains("offset") && !feat.at("offset").is_null()
                        ? num_or_expr(feat.at("offset"), env) : 0.0;
                    const double overhang = feat.contains("overhang") && !feat.at("overhang").is_null()
                        ? num_or_expr(feat.at("overhang"), env) : 0.0;
                    if (!(thickness > 0.0))
                        throw std::runtime_error("weld_end_cap: thickness must be > 0");
                    if (cap_type != "outer" && cap_type != "inner")
                        throw std::runtime_error("weld_end_cap: type must be 'outer' or 'inner'");
                    if (overhang < 0.0)
                        throw std::runtime_error("weld_end_cap: overhang must be >= 0");

                    auto fsit = feat.find("face");
                    if (fsit == feat.end())
                        throw std::runtime_error("weld_end_cap: refs.face is required (a planar member END face)");
                    json fsel = *fsit;
                    if (fsel.is_array()) {
                        if (fsel.size() != 1)
                            throw std::runtime_error("weld_end_cap: refs.face takes exactly one face selector");
                        fsel = fsel.front();
                    }
                    TopoDS_Face end_face = resolve_face_any(fsel, "face");
                    Eigen::Vector3d n = face_normal(end_face, "weld_end_cap");
                    std::string cerr;
                    plate = ::geom::weld_end_cap_plate(end_face, n, cap_type, thickness,
                                                       offset, overhang, cerr);
                    if (plate.IsNull())
                        throw std::runtime_error("weld_end_cap: " + cerr);

                } else {  // weld_gusset
                    const double thickness = feat.contains("thickness")
                        ? num_or_expr(feat.at("thickness"), env) : 5.0;
                    const double d1 = feat.contains("d1") ? num_or_expr(feat.at("d1"), env) : 30.0;
                    const double d2 = feat.contains("d2") ? num_or_expr(feat.at("d2"), env) : 30.0;
                    const std::string gtype = feat.value("hem_type", feat.value("type", std::string("triangular")));
                    const std::string location = feat.value("location", std::string("centered"));
                    const double offset = feat.contains("offset") && !feat.at("offset").is_null()
                        ? num_or_expr(feat.at("offset"), env) : 0.0;
                    if (!(thickness > 0.0))
                        throw std::runtime_error("weld_gusset: thickness must be > 0");
                    if (!(d1 > 0.0 && d2 > 0.0))
                        throw std::runtime_error("weld_gusset: legs d1 and d2 must be > 0");
                    if (gtype != "triangular" && gtype != "polygonal")
                        throw std::runtime_error("weld_gusset: type must be 'triangular' or 'polygonal'");
                    if (location != "centered" && location != "inner" && location != "outer")
                        throw std::runtime_error("weld_gusset: location must be 'centered', 'inner' or 'outer'");

                    auto fsit = feat.find("faces");
                    if (fsit == feat.end() || !fsit->is_array() || fsit->size() != 2)
                        throw std::runtime_error("weld_gusset: refs.faces must be exactly two planar face selectors");
                    TopoDS_Face f1 = resolve_face_any((*fsit)[0], "faces[0]");
                    TopoDS_Face f2 = resolve_face_any((*fsit)[1], "faces[1]");
                    Eigen::Vector3d n1 = face_normal(f1, "weld_gusset");
                    Eigen::Vector3d n2 = face_normal(f2, "weld_gusset");
                    Eigen::Vector3d c1 = face_center(f1), c2 = face_center(f2);
                    double len1 = 0.0, len2 = 0.0;
                    Eigen::Vector3d axis1 = ::geom::weld_longest_edge_dir(f1, len1);
                    Eigen::Vector3d axis2 = ::geom::weld_longest_edge_dir(f2, len2);
                    if (len1 < 1e-6 || len2 < 1e-6)
                        throw std::runtime_error("weld_gusset: could not recover a member axis from a picked face");
                    if (std::abs(axis1.dot(axis2)) > 0.999)
                        throw std::runtime_error("weld_gusset: the two picked faces are parallel/collinear members");

                    Eigen::Vector3d apex =
                        ::geom::weld_closest_point_between_lines(c1, axis1, c2, axis2);

                    // Gusset plane normal: average of the two face normals when they
                    // share a plane; else axis1 x axis2 (oracle).
                    Eigen::Vector3d gn;
                    if (std::abs(n1.dot(n2)) > 0.5) {
                        gn = n1 + (n1.dot(n2) > 0 ? n2 : -n2);
                        if (gn.norm() < 1e-9) gn = axis1.cross(axis2);
                    } else {
                        gn = axis1.cross(axis2);
                    }
                    if (gn.norm() < 1e-9)
                        throw std::runtime_error("weld_gusset: could not determine the gusset plane normal");
                    gn.normalize();

                    auto toward = [](const Eigen::Vector3d& apex, const Eigen::Vector3d& target,
                                     const Eigen::Vector3d& axis) -> Eigen::Vector3d {
                        Eigen::Vector3d v = target - apex;
                        return axis.dot(v) >= 0 ? axis : -axis;
                    };
                    auto in_plane = [](const Eigen::Vector3d& v, const Eigen::Vector3d& nn) -> Eigen::Vector3d {
                        return v - nn * v.dot(nn);
                    };
                    Eigen::Vector3d leg1 = in_plane(toward(apex, c1, axis1), gn);
                    Eigen::Vector3d leg2 = in_plane(toward(apex, c2, axis2), gn);
                    if (leg1.norm() < 1e-9 || leg2.norm() < 1e-9)
                        throw std::runtime_error("weld_gusset: degenerate leg direction at the corner");
                    leg1.normalize();
                    leg2.normalize();

                    Eigen::Vector3d p_a = apex;
                    Eigen::Vector3d p_b = apex + leg1 * d1;
                    Eigen::Vector3d p_c = apex + leg2 * d2;
                    std::vector<Eigen::Vector3d> corners;
                    if (gtype == "triangular") {
                        corners = {p_a, p_b, p_c};
                    } else {
                        Eigen::Vector3d pb2 = apex + leg1 * (d1 * 0.7);
                        Eigen::Vector3d pc2 = apex + leg2 * (d2 * 0.7);
                        corners = {p_a, p_b, pb2, pc2, p_c};
                    }

                    double loc_off = offset;
                    if (location == "inner") loc_off -= thickness / 2.0;
                    else if (location == "outer") loc_off += thickness / 2.0;

                    plate = ::geom::weld_make_solid_plate(corners, gn, thickness, loc_off);
                    if (plate.IsNull())
                        throw std::runtime_error("weld_gusset: plate build failed");
                }

                GProp_GProps gp;
                BRepGProp::VolumeProperties(plate, gp);
                if (gp.Mass() <= 1e-9)
                    throw std::runtime_error(type + ": produced an empty/degenerate plate");
                // The plate is a SEPARATE new body (members survive untouched).
                results[id] = SolidResult{plate};
                solid_order.push_back(id);
                st.ok = true;

            } else if (type == "surf_planar") {
                // The bounding sketch FACE is the surface body (no thickness).
                std::vector<TopoDS_Face> faces = surf_profile_faces(feat);
                results[id] = SolidResult{faces.front()};
                solid_order.push_back(id);
                body_kinds[id] = "surface";
                st.ok = true;

            } else if (type == "surf_extrude") {
                std::vector<TopoDS_Face> faces = surf_profile_faces(feat);
                const std::string end = feat.value("end", std::string("blind"));
                const bool flip = feat.value("flip", false);
                const double depth = feat.contains("depth")
                    ? num_or_expr(feat.at("depth"), env) : 10.0;
                // Direction = first profile face normal (flip negates). Sketch plane
                // normal is the exact analogue of build123d face.normal_at() here.
                Eigen::Vector3d normal = src_face_normal(faces.front());
                if (flip) normal = -normal;
                double amount = depth;
                bool mid = (end == "mid_plane");
                if (end == "through_all") {
                    Bnd_Box bb;
                    BRepBndLib::Add(faces.front(), bb);
                    double x0, y0, z0, x1, y1, z1;
                    bb.Get(x0, y0, z0, x1, y1, z1);
                    double diag = std::sqrt((x1 - x0) * (x1 - x0) + (y1 - y0) * (y1 - y0) +
                                            (z1 - z0) * (z1 - z0));
                    amount = 1.05 * std::max(diag, 1e-3) * 2.0;
                }
                if (!(amount > 0.0))
                    throw std::runtime_error("surf_extrude: effective depth must be > 0");
                TopoDS_Shape shell = ::geom::surf_extrude_walls(faces, normal, amount, mid);
                if (shell.IsNull())
                    throw std::runtime_error("surf_extrude: side-wall build failed");
                results[id] = SolidResult{shell};
                solid_order.push_back(id);
                body_kinds[id] = "surface";
                st.ok = true;

            } else if (type == "surf_revolve") {
                std::vector<TopoDS_Face> faces = surf_profile_faces(feat);
                const Eigen::Vector3d axis_pt = req_vec3(feat, "axis_pt");
                const Eigen::Vector3d axis_dir = req_vec3(feat, "axis_dir");
                double angle_deg = feat.contains("angle")
                    ? num_or_expr(feat.at("angle"), env) : 360.0;
                if (std::abs(angle_deg) < 1e-6)
                    throw std::runtime_error("surf_revolve: angle must be non-zero");
                if (std::abs(angle_deg) > 360.0 + 1e-6)
                    angle_deg = std::copysign(360.0, angle_deg);
                TopoDS_Shape shell = ::geom::surf_revolve_walls(
                    faces, axis_pt, axis_dir, radians(std::abs(angle_deg)));
                if (shell.IsNull())
                    throw std::runtime_error("surf_revolve: MakeRevol failed");
                results[id] = SolidResult{shell};
                solid_order.push_back(id);
                body_kinds[id] = "surface";
                st.ok = true;

            } else if (type == "surf_sweep") {
                std::vector<TopoDS_Face> faces = surf_profile_faces(feat);
                auto pit = feat.find("path");
                if (pit == feat.end())
                    throw std::runtime_error("surf_sweep: a 'path' ref is required");
                const std::string path_id = selector_owner_id(*pit);
                const SketchResult& pathsk = require_sketch(results, path_id, "path");
                TopoDS_Wire spine =
                    aicad::geom::wire_from_solved_sketch(pathsk.entities, pathsk.plane);
                if (spine.IsNull())
                    throw std::runtime_error("surf_sweep: path produced no wire");
                TopoDS_Shape shell = ::geom::surf_sweep_walls(faces, spine);
                if (shell.IsNull())
                    throw std::runtime_error("surf_sweep: build failed");
                results[id] = SolidResult{shell};
                solid_order.push_back(id);
                body_kinds[id] = "surface";
                st.ok = true;

            } else if (type == "surf_loft") {
                auto sit = feat.find("sections");
                if (sit == feat.end() || !sit->is_array() || sit->size() < 2)
                    throw std::runtime_error("surf_loft: needs >= 2 sections");
                std::vector<TopoDS_Wire> wires;
                for (const json& s : *sit) {
                    const std::string sk = selector_owner_id(s);
                    const SketchResult& src = require_sketch(results, sk, sk.c_str());
                    if (src.faces.empty())
                        throw std::runtime_error("surf_loft: section '" + sk + "' has no face");
                    TopoDS_Wire w = BRepTools::OuterWire(src.faces.front());
                    if (w.IsNull())
                        throw std::runtime_error("surf_loft: section '" + sk + "' has no wire");
                    wires.push_back(w);
                }
                const bool ruled = feat.value("ruled", false);
                TopoDS_Shape shell = ::geom::surf_loft_shell(wires, ruled);
                if (shell.IsNull())
                    throw std::runtime_error("surf_loft: ThruSections failed");
                results[id] = SolidResult{shell};
                solid_order.push_back(id);
                body_kinds[id] = "surface";
                st.ok = true;

            } else if (type == "surf_ruled") {
                if (!feat.contains("edge_a") || !feat.contains("edge_b"))
                    throw std::runtime_error("surf_ruled: edge_a and edge_b are required");
                TopoDS_Wire wa = surf_sketch_wire(feat.at("edge_a"), "edge_a");
                TopoDS_Wire wb = surf_sketch_wire(feat.at("edge_b"), "edge_b");
                TopoDS_Shape surf = ::geom::surf_ruled_surface(wa, wb);
                if (surf.IsNull())
                    throw std::runtime_error("surf_ruled: build failed");
                results[id] = SolidResult{surf};
                solid_order.push_back(id);
                body_kinds[id] = "surface";
                st.ok = true;

            } else if (type == "surf_offset") {
                const std::string body_id = body_ref(feat);
                if (body_id.empty())
                    throw std::runtime_error("surf_offset: requires an existing body");
                const TopoDS_Shape& body = require_solid(results, body_id, "body");
                const double distance = feat.contains("distance")
                    ? num_or_expr(feat.at("distance"), env) : 5.0;
                if (std::abs(distance) < 1e-9)
                    throw std::runtime_error("surf_offset: distance must be non-zero");
                auto fit = feat.find("faces");
                if (fit == feat.end())
                    throw std::runtime_error("surf_offset: refs.faces is required");
                aicad::doc::FacePicks picks =
                    aicad::doc::resolve_face_picks(body, *fit, "faces");
                if (!picks.dangling.empty())
                    throw std::runtime_error("surf_offset: face selector(s) dangling");
                if (picks.faces.empty())
                    throw std::runtime_error("surf_offset: no faces resolved");
                TopoDS_Shape surf = ::geom::surf_offset_surface(picks.faces, distance);
                if (surf.IsNull())
                    throw std::runtime_error("surf_offset: offset build failed");
                results[id] = SolidResult{surf};
                solid_order.push_back(id);
                body_kinds[id] = "surface";
                st.ok = true;

            } else if (type == "surf_fill" || type == "surf_boundary") {
                // Boundary edges come from sketch region curve(s). For surf_fill the
                // ref slot is "edges"; for surf_boundary it is "dir1" (+ optional dir2).
                std::vector<TopoDS_Edge> edges;
                auto add_wire_edges = [&](const json& ref, const char* slot) {
                    TopoDS_Wire w = surf_sketch_wire(ref, slot);
                    for (TopExp_Explorer ex(w, TopAbs_EDGE); ex.More(); ex.Next())
                        edges.push_back(TopoDS::Edge(ex.Current()));
                };
                if (type == "surf_fill") {
                    auto eit = feat.find("edges");
                    if (eit == feat.end() || !eit->is_array() || eit->empty())
                        throw std::runtime_error("surf_fill: refs.edges is required");
                    for (const json& s : *eit) add_wire_edges(s, "edges");
                } else {
                    auto d1 = feat.find("dir1");
                    if (d1 == feat.end())
                        throw std::runtime_error("surf_boundary: refs.dir1 is required");
                    if (d1->is_array()) { for (const json& s : *d1) add_wire_edges(s, "dir1"); }
                    else add_wire_edges(*d1, "dir1");
                    auto d2 = feat.find("dir2");
                    if (d2 != feat.end()) {
                        if (d2->is_array()) { for (const json& s : *d2) add_wire_edges(s, "dir2"); }
                        else add_wire_edges(*d2, "dir2");
                    }
                }
                const int cont = (feat.value("continuity", std::string("g0")) == "g1") ? 1 : 0;
                TopoDS_Shape patch = ::geom::surf_fill_patch(edges, cont);
                if (patch.IsNull())
                    throw std::runtime_error(type + ": BRepFill_Filling failed");
                results[id] = SolidResult{patch};
                solid_order.push_back(id);
                body_kinds[id] = "surface";
                st.ok = true;

            } else if (type == "surf_knit") {
                auto sit = feat.find("surfaces");
                if (sit == feat.end() || !sit->is_array() || sit->size() < 2)
                    throw std::runtime_error("surf_knit: needs >= 2 surface bodies");
                std::vector<TopoDS_Shape> inputs;
                std::vector<std::string> consumed_ids;
                for (const json& s : *sit) {
                    const std::string owner = selector_owner_id(s);
                    if (owner.empty())
                        throw std::runtime_error("surf_knit: malformed surface selector");
                    inputs.push_back(require_solid(results, owner, "surfaces"));
                    consumed_ids.push_back(owner);
                }
                // Sew tolerance: 1e-3 * combined bbox diagonal, clamped [1e-4, 0.1].
                Bnd_Box bb;
                for (const TopoDS_Shape& s : inputs) BRepBndLib::Add(s, bb);
                double tol = 1e-3;
                if (!bb.IsVoid()) {
                    double x0, y0, z0, x1, y1, z1;
                    bb.Get(x0, y0, z0, x1, y1, z1);
                    double diag = std::sqrt((x1 - x0) * (x1 - x0) + (y1 - y0) * (y1 - y0) +
                                            (z1 - z0) * (z1 - z0));
                    tol = std::max(1e-4, std::min(0.1, diag * 1e-3));
                }
                bool is_solid = false;
                TopoDS_Shape knit = ::geom::surf_knit_bodies(inputs, tol, is_solid);
                if (knit.IsNull())
                    throw std::runtime_error("surf_knit: sewing failed");
                for (const std::string& cid : consumed_ids) consumed.insert(cid);
                results[id] = SolidResult{knit};
                solid_order.push_back(id);
                body_kinds[id] = is_solid ? "solid" : "surface";
                st.ok = true;

            } else if (type == "surf_delete_face") {
                const std::string body_id = body_ref(feat);
                if (body_id.empty())
                    throw std::runtime_error("surf_delete_face: requires an existing body");
                const TopoDS_Shape& body = require_solid(results, body_id, "body");
                auto fit = feat.find("faces");
                if (fit == feat.end())
                    throw std::runtime_error("surf_delete_face: refs.faces is required");
                aicad::doc::FacePicks picks =
                    aicad::doc::resolve_face_picks(body, *fit, "faces");
                if (!picks.dangling.empty())
                    throw std::runtime_error("surf_delete_face: face selector(s) dangling");
                if (picks.faces.empty())
                    throw std::runtime_error("surf_delete_face: no faces resolved");
                std::vector<Eigen::Vector3d> cents;
                for (const TopoDS_Face& f : picks.faces)
                    cents.push_back(::geom::face_centroid(f));
                double tol = std::max(aicad::doc::bbox_diag(body), 1.0) * 1e-4;
                TopoDS_Shape shell = ::geom::surf_delete_faces(body, cents, tol);
                if (shell.IsNull())
                    throw std::runtime_error("surf_delete_face: nothing left after delete");
                // REPLACE the solid with the opened shell (same body, now a surface).
                consumed.insert(body_id);
                source_of[id] = body_id;
                results[id] = SolidResult{shell};
                solid_order.push_back(id);
                body_kinds[id] = "surface";
                st.ok = true;

            } else if (type == "surf_trim") {
                auto ssel = feat.find("surface");
                if (ssel == feat.end())
                    throw std::runtime_error("surf_trim: refs.surface is required");
                const std::string surf_owner = selector_owner_id(*ssel);
                const TopoDS_Shape& surface = require_solid(results, surf_owner, "surface");
                auto tit = feat.find("tool");
                if (tit == feat.end())
                    throw std::runtime_error("surf_trim: refs.tool is required");
                // The tool is a FACE selector (split._resolve_tool_face searches every
                // running body), a sketch id whose face is the planar tool, or a body
                // owner whose picked face is the tool. Try, in order: a named sketch
                // face, then a face pick against every surviving solid body.
                TopoDS_Face tool_face;
                bool tool_ok = false;
                const std::string tool_owner = selector_owner_id(*tit);
                if (!tool_owner.empty() && results.count(tool_owner) &&
                    std::holds_alternative<SketchResult>(results.at(tool_owner))) {
                    const SketchResult& tsk = std::get<SketchResult>(results.at(tool_owner));
                    if (!tsk.faces.empty()) { tool_face = tsk.faces.front(); tool_ok = true; }
                }
                if (!tool_ok && tit->is_object() &&
                    tit->value("kind", std::string()) == "face") {
                    // Resolve the face pick against every surviving solid body (the split
                    // tool-face resolver scans all bodies); take the first that resolves.
                    for (const std::string& cand : solid_order) {
                        if (consumed.count(cand)) continue;
                        if (!std::holds_alternative<SolidResult>(results.at(cand))) continue;
                        const TopoDS_Shape& cb = std::get<SolidResult>(results.at(cand)).shape;
                        if (cb.IsNull()) continue;
                        aicad::doc::FacePicks tp =
                            aicad::doc::resolve_face_picks(cb, *tit, "tool");
                        if (!tp.faces.empty()) { tool_face = tp.faces.front(); tool_ok = true; break; }
                    }
                }
                if (!tool_ok)
                    throw std::runtime_error("surf_trim: tool face did not resolve");
                const std::string keep = feat.value("keep", std::string("both"));
                int split_count = 0;
                TopoDS_Shape trimmed =
                    ::geom::surf_trim_surface(surface, tool_face, keep, split_count);
                if (trimmed.IsNull() || split_count == 0)
                    throw std::runtime_error("surf_trim: tool did not divide the surface");
                consumed.insert(surf_owner);
                source_of[id] = surf_owner;
                results[id] = SolidResult{trimmed};
                solid_order.push_back(id);
                body_kinds[id] = "surface";
                st.ok = true;

            } else if (type == "surf_replace_face") {
                // The modification target is the last SOLID body (a surface body in the
                // list is skipped — replace_face heals a solid, not a sheet).
                std::string body_id;
                if (feat.contains("body") && feat["body"].is_string()) {
                    body_id = feat["body"].get<std::string>();
                } else {
                    for (auto it = solid_order.rbegin(); it != solid_order.rend(); ++it) {
                        if (consumed.count(*it)) continue;
                        auto kit = body_kinds.find(*it);
                        if (kit != body_kinds.end() && kit->second == "surface") continue;
                        body_id = *it;
                        break;
                    }
                }
                if (body_id.empty())
                    throw std::runtime_error("surf_replace_face: requires an existing solid");
                const TopoDS_Shape& solid = require_solid(results, body_id, "body");
                auto fit = feat.find("faces");
                if (fit == feat.end())
                    throw std::runtime_error("surf_replace_face: refs.faces is required");
                aicad::doc::FacePicks picks =
                    aicad::doc::resolve_face_picks(solid, *fit, "faces");
                if (picks.faces.empty())
                    throw std::runtime_error("surf_replace_face: no target faces resolved");
                auto ssel = feat.find("surface");
                if (ssel == feat.end())
                    throw std::runtime_error("surf_replace_face: refs.surface is required");
                // The replacement surface is a BODY selector (a whole surface body) or a
                // FACE selector (a single face on any running body). Resolve either.
                TopoDS_Shape replacement;
                const std::string surf_owner = selector_owner_id(*ssel);
                if (!surf_owner.empty() && results.count(surf_owner) &&
                    std::holds_alternative<SolidResult>(results.at(surf_owner)) &&
                    surf_owner != body_id) {
                    replacement = std::get<SolidResult>(results.at(surf_owner)).shape;
                } else if (ssel->is_object() &&
                           ssel->value("kind", std::string()) == "face") {
                    for (const std::string& cand : solid_order) {
                        if (cand == body_id || consumed.count(cand)) continue;
                        if (!std::holds_alternative<SolidResult>(results.at(cand))) continue;
                        const TopoDS_Shape& cb = std::get<SolidResult>(results.at(cand)).shape;
                        if (cb.IsNull()) continue;
                        aicad::doc::FacePicks rp =
                            aicad::doc::resolve_face_picks(cb, *ssel, "surface");
                        if (!rp.faces.empty()) { replacement = rp.faces.front(); break; }
                    }
                }
                if (replacement.IsNull())
                    throw std::runtime_error("surf_replace_face: replacement surface did not resolve");
                std::vector<Eigen::Vector3d> cents;
                for (const TopoDS_Face& f : picks.faces)
                    cents.push_back(::geom::face_centroid(f));
                TopoDS_Shape healed =
                    ::geom::surf_replace_face_solid(solid, cents, replacement,
                                                    aicad::doc::bbox_diag(solid));
                if (healed.IsNull())
                    throw std::runtime_error("surf_replace_face: could not heal a solid");
                consumed.insert(body_id);
                source_of[id] = body_id;
                results[id] = SolidResult{healed};
                solid_order.push_back(id);
                body_kinds[id] = "solid";
                st.ok = true;

            } else if (type == "surf_thicken") {
                auto ssel = feat.find("surface");
                if (ssel == feat.end())
                    throw std::runtime_error("surf_thicken: refs.surface is required");
                const std::string surf_owner = selector_owner_id(*ssel);
                const TopoDS_Shape& surface = require_solid(results, surf_owner, "surface");
                const double thickness = feat.contains("thickness")
                    ? num_or_expr(feat.at("thickness"), env) : 2.0;
                if (!(thickness > 0.0))
                    throw std::runtime_error("surf_thicken: thickness must be > 0");
                const std::string side = feat.value("side", std::string("one"));
                const bool flip = feat.value("flip", false);
                TopoDS_Shape solid =
                    ::geom::surf_thicken_solid(surface, thickness, side, flip);
                if (solid.IsNull())
                    throw std::runtime_error("surf_thicken: thickening failed");
                // The solid REPLACES the consumed surface body.
                consumed.insert(surf_owner);
                source_of[id] = surf_owner;
                results[id] = SolidResult{solid};
                solid_order.push_back(id);
                body_kinds[id] = "solid";
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
            result.shapes.push_back(shape);  // AICM order: shapes[i] <-> body ordinal i+1
            ++body_num;
            // Per-body stats: SURFACE bodies report meaningful AREA + face/edge counts
            // (volume reported as 0, matching the oracle — VolumeProperties on an OPEN
            // shell yields a meaningless signed value); SOLID bodies report VOLUME. The
            // surfacing gate reads these (tools/cpp_surf_parity.py). Additive — existing
            // solid fixtures are unaffected (default kind "solid" -> volume accumulates).
            BodyInfo bi;
            bi.name = sid;
            auto kit = body_kinds.find(sid);
            bi.kind = (kit != body_kinds.end()) ? kit->second : std::string("solid");
            const bool is_surface = (bi.kind == "surface");
            double vol = 0.0;
            if (!is_surface) {
                GProp_GProps gp;
                BRepGProp::VolumeProperties(shape, gp);
                vol = gp.Mass();
            }
            result.volume += vol;  // only solids contribute to the total volume signal
            bi.volume = vol;
            bi.area = ::geom::shape_area(shape);
            bi.n_faces = ::geom::shape_face_count(shape);
            bi.n_edges = ::geom::shape_edge_count(shape);
            result.bodies_info.push_back(std::move(bi));
        } catch (...) {
            // A tessellation failure must not crash the rebuild; skip this body.
        }
    }

    result.body_count = static_cast<int>(bodies.size());
    result.aicm = aicm::write_aicm(bodies);

    // -------------------------------------------------- owner map (host picking)
    // Per surviving body, tag each face/edge with the feature that created it: diff
    // the body's topology against the body it derived from (source_of) by TShape
    // identity (existing topology keeps its owner; NEW topology is this feature's).
    // Then invert into {fid:{bodies,faces,edges}} keyed by the AICM body ordinal +
    // 1-based TopExp topo index (port of engine/doc/session.py::_owner_map).
    {
        std::map<std::string, std::map<int, std::string>> face_owners, edge_owners;
        for (const std::string& sid : solid_order) {  // sources precede dependents
            if (!std::holds_alternative<SolidResult>(results.at(sid))) continue;
            const TopoDS_Shape& shape = std::get<SolidResult>(results.at(sid)).shape;
            if (shape.IsNull()) continue;
            TopTools_IndexedMapOfShape fm, em;
            TopExp::MapShapes(shape, TopAbs_FACE, fm);
            TopExp::MapShapes(shape, TopAbs_EDGE, em);
            const std::string src = source_of.count(sid) ? source_of[sid] : std::string();
            const bool has_src = !src.empty() && results.count(src) &&
                                 std::holds_alternative<SolidResult>(results.at(src)) &&
                                 !std::get<SolidResult>(results.at(src)).shape.IsNull();
            TopTools_IndexedMapOfShape sfm, sem;
            if (has_src) {
                const TopoDS_Shape& ss = std::get<SolidResult>(results.at(src)).shape;
                TopExp::MapShapes(ss, TopAbs_FACE, sfm);
                TopExp::MapShapes(ss, TopAbs_EDGE, sem);
            }
            // Owner diff by TShape IDENTITY: a result face/edge that survived UNCHANGED
            // (same TShape) keeps its source owner; a MODIFIED or NEW one belongs to this
            // feature (matches the Python build123d attribution — a fillet owns every face
            // it regenerated, not just the new round). The load-bearing requirement is that
            // a MODIFYING feature has a `source_of` so this diff runs at all (the fillet/
            // chamfer branch sets it; without it the whole body was attributed to the op).
            std::map<int, std::string> fo, eo;
            for (int i = 1; i <= fm.Extent(); ++i) {
                if (has_src && sfm.Contains(fm.FindKey(i))) {
                    int pi = sfm.FindIndex(fm.FindKey(i));
                    auto& s = face_owners[src];
                    auto it = s.find(pi);
                    fo[i] = (it != s.end()) ? it->second : sid;
                } else fo[i] = sid;
            }
            for (int i = 1; i <= em.Extent(); ++i) {
                if (has_src && sem.Contains(em.FindKey(i))) {
                    int pi = sem.FindIndex(em.FindKey(i));
                    auto& s = edge_owners[src];
                    auto it = s.find(pi);
                    eo[i] = (it != s.end()) ? it->second : sid;
                } else eo[i] = sid;
            }
            face_owners[sid] = std::move(fo);
            edge_owners[sid] = std::move(eo);
        }
        json om = json::object();
        auto ensure = [&](const std::string& fid) -> json& {
            if (!om.contains(fid))
                om[fid] = {{"bodies", json::array()}, {"faces", json::array()}, {"edges", json::array()}};
            return om[fid];
        };
        for (const aicm::AicmBody& b : bodies) {  // ordinal = b.body_id (AICM order)
            const std::string& sid = b.name;
            std::set<std::string> body_owners;
            for (const auto& kv : face_owners[sid]) {
                ensure(kv.second)["faces"].push_back({b.body_id, kv.first});
                body_owners.insert(kv.second);
            }
            for (const auto& kv : edge_owners[sid])
                ensure(kv.second)["edges"].push_back({b.body_id, kv.first});
            for (const std::string& fid : body_owners)
                ensure(fid)["bodies"].push_back(b.body_id);
        }
        result.owner_map = std::move(om);
    }

    // ok iff every feature compiled cleanly.
    result.ok = true;
    for (const FeatureStatus& s : result.statuses)
        if (!s.ok) { result.ok = false; break; }

    return result;
}

// ----------------------------------------------------------------- resolve_pick
nlohmann::json resolve_pick(const json& doc, const json& env, int body_id,
                            const std::string& kind, int topo_id) {
    CompileResult r = compile_document(doc, env);
    if (body_id < 1 || body_id > static_cast<int>(r.shapes.size()))
        return json(nullptr);
    const TopoDS_Shape& shape = r.shapes[body_id - 1];

    // Owner = the feature that owns (body_id, topo_id) of this kind, from the map.
    std::optional<std::string> owner;
    const char* arrkey = (kind == "face") ? "faces" : (kind == "edge") ? "edges" : nullptr;
    if (arrkey && r.owner_map.is_object()) {
        for (auto it = r.owner_map.begin(); it != r.owner_map.end() && !owner; ++it) {
            const json& rec = it.value();
            if (!rec.contains(arrkey) || !rec[arrkey].is_array()) continue;
            for (const json& pr : rec[arrkey])
                if (pr.is_array() && pr.size() == 2 &&
                    pr[0].get<int>() == body_id && pr[1].get<int>() == topo_id) {
                    owner = it.key();
                    break;
                }
        }
    }
    return aicad::doc::make_pick_selector(shape, kind, topo_id, owner);
}

// --------------------------------------------------------------- push-pull (Instant3D)
// Port of engine/doc/instant3d.py plan_edit + helpers. NEVER mutates the doc.
namespace {

constexpr double kPpMinDim = 0.1;        // smallest length/depth/radius a drag may leave (mm)
constexpr double kPpParallelTol = 0.966;  // |cos| >= this (~15deg) counts a normal as axis-aligned

std::optional<double> pp_dim_value(const json& feat, const std::string& key) {
    if (!feat.contains("params") || !feat["params"].is_object()) return std::nullopt;
    const json& p = feat["params"];
    auto it = p.find(key);
    if (it == p.end()) return std::nullopt;
    const json* val = it->is_object() ? (it->contains("value") ? &(*it)["value"] : nullptr) : &*it;
    if (!val || val->is_boolean() || !val->is_number()) return std::nullopt;
    return val->get<double>();
}

bool pp_has_expr(const json& feat, const std::string& key) {
    if (!feat.contains("params") || !feat["params"].is_object()) return false;
    auto it = feat["params"].find(key);
    if (it == feat["params"].end() || !it->is_object()) return false;
    auto e = it->find("expr");
    return e != it->end() && e->is_string() && !e->get<std::string>().empty();
}

std::string pp_axis_label(const std::optional<Eigen::Vector3d>& axis) {
    if (!axis) return "";
    Eigen::Vector3d a = *axis;
    double n = a.norm();
    if (n > 1e-12) a /= n;
    const std::pair<const char*, Eigen::Vector3d> bases[] = {
        {"x", {1, 0, 0}}, {"y", {0, 1, 0}}, {"z", {0, 0, 1}}};
    for (const auto& [lbl, base] : bases)
        if (std::abs(a.dot(base)) >= kPpParallelTol) return lbl;
    return "";
}

const json* pp_feature_by_id(const json& doc, const std::string& fid) {
    if (!doc.contains("features") || !doc["features"].is_array()) return nullptr;
    for (const json& f : doc["features"])
        if (f.is_object() && f.value("id", std::string()) == fid) return &f;
    return nullptr;
}

// The extrude direction (= owning sketch's plane normal) for a blind extrude, or
// nullopt when it can't be resolved cheaply (a datum-plane sketch) — in which case
// the caller skips the cap/side-wall test, exactly as the Python _extrude_axis does.
std::optional<Eigen::Vector3d> pp_extrude_axis(const json& doc, const json& feat) {
    if (!feat.contains("refs") || !feat["refs"].is_object()) return std::nullopt;
    const json& refs = feat["refs"];
    if (!refs.contains("profile") || !refs["profile"].is_array()) return std::nullopt;
    std::string sketch_fid;
    for (const json& p : refs["profile"])
        if (p.is_object() && p.value("kind", std::string()) == "region" &&
            p.contains("sketch") && p["sketch"].is_string()) {
            sketch_fid = p["sketch"].get<std::string>();
            break;
        }
    if (sketch_fid.empty()) return std::nullopt;
    const json* sfeat = pp_feature_by_id(doc, sketch_fid);
    if (!sfeat) return std::nullopt;
    std::string pref = "top";
    if (sfeat->contains("refs") && (*sfeat)["refs"].is_object() &&
        (*sfeat)["refs"].value("plane", json()).is_string())
        pref = (*sfeat)["refs"]["plane"].get<std::string>();
    else if (sfeat->contains("params") && (*sfeat)["params"].is_object() &&
             (*sfeat)["params"].value("plane", json()).is_string())
        pref = (*sfeat)["params"]["plane"].get<std::string>();
    if (pref.rfind("plane.", 0) == 0) pref = pref.substr(6);
    if (pref != "top" && pref != "front" && pref != "right") return std::nullopt;
    return plane_from_name(pref).normal;
}

// Fill new_value/clamped for a length-style param (instant3d.py _length_plan); sets
// draggable=false+reason when the param is expr-driven.
void pp_length_plan(PushPullPlan& plan, const json& feat, const std::string& key, double old,
                    double distance, const std::string& axis_lbl) {
    if (pp_has_expr(feat, key)) {
        plan.draggable = false;
        plan.reason = "param '" + key + "' is equation-driven (expr) — edit the equation instead";
        return;
    }
    double nv = old + distance;
    bool clamped = false;
    if (nv < kPpMinDim) { nv = kPpMinDim; clamped = true; }
    plan.draggable = true;
    plan.param = key;
    plan.old_value = old;
    plan.new_value = nv;
    plan.clamped = clamped;
    plan.axis = axis_lbl;
    plan.has_axis = !axis_lbl.empty();
}

void pp_set_not_draggable(PushPullPlan& plan, const std::string& reason) {
    plan.draggable = false;
    plan.reason = reason;
}

// plan_edit dispatch on the face surface + feature type -> the single drivable param.
void pp_plan_edit(const json& doc, const json& feat, const TopoInfo& info, double distance,
                  PushPullPlan& plan) {
    const std::string& ftype = plan.ftype;
    const std::string surf = info.surf.value_or("");

    if (surf == "plane") {
        // An offset/midplane datum's planar face moves with its offset param.
        if (ftype == "plane" && pp_dim_value(feat, "offset").has_value()) {
            const std::string key = "offset";
            if (pp_has_expr(feat, key)) {
                pp_set_not_draggable(plan, "param 'offset' is equation-driven (expr)");
                return;
            }
            double old = *pp_dim_value(feat, key);
            plan.draggable = true;
            plan.param = key;
            plan.old_value = old;
            plan.new_value = old + distance;  // offset is unclamped (can go negative)
            plan.clamped = false;
            plan.axis = "normal";
            plan.has_axis = true;
            return;
        }
        // _plan_for_planar
        std::string axis_lbl = pp_axis_label(info.axis);
        if (ftype == "box") {
            if (axis_lbl.empty()) {
                pp_set_not_draggable(plan, "box face normal is not aligned to a principal axis");
                return;
            }
            const std::string key = axis_lbl == "x" ? "length" : axis_lbl == "y" ? "width" : "height";
            double old = pp_dim_value(feat, key).value_or(20.0);
            pp_length_plan(plan, feat, key, old, distance, axis_lbl);
            return;
        }
        if (ftype == "cylinder" || ftype == "cone") {
            double old = pp_dim_value(feat, "height").value_or(20.0);
            pp_length_plan(plan, feat, "height", old, distance, axis_lbl.empty() ? "axial" : axis_lbl);
            return;
        }
        if (ftype == "extrude") {
            const json& p = feat.contains("params") && feat["params"].is_object() ? feat["params"]
                                                                                  : json::object();
            std::string end = p.is_object() ? p.value("end", std::string("blind")) : "blind";
            if (end != "blind") {
                pp_set_not_draggable(
                    plan, "extrude end condition '" + end + "' has no single blind-depth param to drag");
                return;
            }
            std::optional<Eigen::Vector3d> ext_axis = pp_extrude_axis(doc, feat);
            if (ext_axis && info.axis) {
                Eigen::Vector3d a = *info.axis;
                double n = a.norm();
                if (n > 1e-12) a /= n;
                if (std::abs(a.dot(*ext_axis)) < kPpParallelTol) {
                    pp_set_not_draggable(plan,
                        "dragged face is a side wall of the extrude (normal perpendicular to the "
                        "extrude axis), not a depth cap");
                    return;
                }
            }
            double old = pp_dim_value(feat, "depth").value_or(10.0);
            pp_length_plan(plan, feat, "depth", old, distance, axis_lbl.empty() ? "n" : axis_lbl);
            return;
        }
        pp_set_not_draggable(plan, "planar face of feature type '" + ftype + "' is not push-pull drivable");
        return;
    }
    if (surf == "cylinder") {
        if (ftype == "cylinder") {
            const std::string key = "radius";
            if (pp_has_expr(feat, key)) {
                pp_set_not_draggable(plan, "param 'radius' is equation-driven (expr)");
                return;
            }
            double old = pp_dim_value(feat, key).value_or(10.0);
            double nv = old + distance;
            bool clamped = false;
            if (nv < kPpMinDim) { nv = kPpMinDim; clamped = true; }
            plan.draggable = true;
            plan.param = key;
            plan.old_value = old;
            plan.new_value = nv;
            plan.clamped = clamped;
            plan.axis = "radial";
            plan.has_axis = true;
            return;
        }
        if (ftype == "hole") {
            const json& p = feat.contains("params") && feat["params"].is_object() ? feat["params"]
                                                                                  : json::object();
            std::string standard = p.is_object() && p.contains("standard") && p["standard"].is_string()
                                       ? p["standard"].get<std::string>() : "custom";
            if (standard != "custom") {
                pp_set_not_draggable(plan,
                    "hole bore is driven by standard '" + standard + "', not a free diameter");
                return;
            }
            const std::string key = "diameter";
            if (pp_has_expr(feat, key)) {
                pp_set_not_draggable(plan, "param 'diameter' is equation-driven (expr)");
                return;
            }
            double old = pp_dim_value(feat, key).value_or(3.4);
            double nv = old + 2.0 * distance;  // radial change doubles the diameter
            bool clamped = false;
            if (nv < kPpMinDim) { nv = kPpMinDim; clamped = true; }
            plan.draggable = true;
            plan.param = key;
            plan.old_value = old;
            plan.new_value = nv;
            plan.clamped = clamped;
            plan.axis = "radial";
            plan.has_axis = true;
            return;
        }
        pp_set_not_draggable(plan, "cylindrical face of feature type '" + ftype + "' is not push-pull drivable");
        return;
    }
    if (ftype == "revolve") {
        pp_set_not_draggable(plan, "revolve is angle-driven; dragging a revolved face is not a push-pull");
        return;
    }
    pp_set_not_draggable(plan, "face surface '" + surf + "' of feature '" + ftype +
                                   "' maps to no single push-pull param");
}

}  // namespace

PushPullPlan plan_push_pull(const json& doc, const json& env, int body_id, int topo_id,
                            double distance) {
    PushPullPlan plan;
    if (doc.is_object() && doc.value("schema", std::string()) == "aicad.asm/1")
        throw OpError("invalid_op", "doc_push_pull: not supported on assemblies", {{"reason", "assembly"}});

    CompileResult r = compile_document(doc, env);
    const int nbodies = static_cast<int>(r.shapes.size());
    if (body_id < 1 || body_id > nbodies)
        throw OpError("invalid_op",
                      "body_id " + std::to_string(body_id) + " out of range (1.." +
                          std::to_string(nbodies) + ")",
                      {{"reason", "bad body_id"}});
    const TopoDS_Shape& shape = r.shapes[body_id - 1];
    std::vector<TopoInfo> infos = describe_faces(shape);
    const int nfaces = static_cast<int>(infos.size());
    if (topo_id < 1 || topo_id > nfaces)
        throw OpError("invalid_op",
                      "topo_id " + std::to_string(topo_id) + " out of range (1.." +
                          std::to_string(nfaces) + ") for faces",
                      {{"reason", "bad topo_id"}});
    const TopoInfo& info = infos[topo_id - 1];

    // Owner = the feature owning (body_id, topo_id) per the owner map (same scan as
    // resolve_pick; FACE describe order == MapShapes order, so topo_id aligns).
    std::optional<std::string> owner;
    if (r.owner_map.is_object()) {
        for (auto it = r.owner_map.begin(); it != r.owner_map.end() && !owner; ++it) {
            const json& rec = it.value();
            if (!rec.contains("faces") || !rec["faces"].is_array()) continue;
            for (const json& pr : rec["faces"])
                if (pr.is_array() && pr.size() == 2 && pr[0].get<int>() == body_id &&
                    pr[1].get<int>() == topo_id) {
                    owner = it.key();
                    break;
                }
        }
    }
    if (!owner) {
        pp_set_not_draggable(plan, "picked face has no owning feature in the owner map");
        return plan;
    }
    plan.owner_fid = *owner;
    const json* feat = pp_feature_by_id(doc, *owner);
    if (!feat) {
        pp_set_not_draggable(plan, "owner feature '" + *owner + "' not found in the document");
        return plan;
    }
    plan.ftype = feat->value("type", std::string());
    pp_plan_edit(doc, *feat, info, distance, plan);
    return plan;
}

}  // namespace aicad::doc
