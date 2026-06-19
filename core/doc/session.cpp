// Stateful document session implementation — see session.h.
//
// Mirrors engine/doc/session.py + engine/doc/ops.py over compile_document. JSON
// in, JSON out; never mutates a session until an op batch fully applies (the
// Python transactional contract: apply to a working copy, commit on success).

#include "doc/session.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <cctype>
#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <limits>
#include <map>
#include <mutex>
#include <regex>
#include <set>
#include <string>
#include <vector>

#include <cmath>
#include <iomanip>
#include <optional>
#include <sstream>

#include <BRepMesh_IncrementalMesh.hxx>
#include <BRepBuilderAPI_MakeEdge.hxx>
#include <BRepBuilderAPI_MakeFace.hxx>
#include <BRepBuilderAPI_MakeWire.hxx>
#include <GC_MakeArcOfCircle.hxx>
#include <BRepTools.hxx>
#include <BRep_Tool.hxx>
#include <Bnd_Box.hxx>
#include <BRepBndLib.hxx>
#include <Poly_Triangulation.hxx>
#include <Standard_Failure.hxx>
#include <TopLoc_Location.hxx>
#include <TopExp_Explorer.hxx>
#include <TopAbs.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Shape.hxx>
#include <gp_Pnt.hxx>
#include <gp_Pln.hxx>
#include <gp_Dir.hxx>
#include <gp_Ax2.hxx>
#include <gp_Ax3.hxx>
#include <gp_Vec.hxx>
#include <gp_Trsf.hxx>
#include <gp_Mat.hxx>
#include <GProp_GProps.hxx>
#include <BRepGProp.hxx>
#include <TopExp.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <TopTools_IndexedDataMapOfShapeListOfShape.hxx>
#include <BRepCheck_Analyzer.hxx>
#include <BRepCheck_Result.hxx>
#include <BRepCheck_ListOfStatus.hxx>
#include <BRepCheck_Status.hxx>
#include <BRepAlgoAPI_Section.hxx>
#include <BRepAlgoAPI_Common.hxx>
#include <BRepBuilderAPI_Transform.hxx>
#include <BRepExtrema_DistShapeShape.hxx>
#include <BRepAdaptor_Surface.hxx>
#include <BRepAdaptor_Curve.hxx>
#include <BRepLProp_SLProps.hxx>
#include <GCPnts_AbscissaPoint.hxx>
#include <GeomAbs_SurfaceType.hxx>
#include <GeomAbs_CurveType.hxx>
#include <ShapeAnalysis_Surface.hxx>
#include <Geom_Surface.hxx>
#include <gp_Circ.hxx>
#include <gp_Pnt2d.hxx>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Vertex.hxx>

#include <Eigen/Eigenvalues>

#include <memory>

#include "doc/assembly.h"
#include "doc/document.h"
#include "doc/formats.h"
#include "doc/selectors.h"
#include "doc/sketch_geom.h"
#include "doc/stepplus.h"
#include "doc/validate.h"
#include "geom/aicm.h"
#include "geom/plane.h"
#include "geom/profile.h"
#include "geom/tessellate.h"
#include "mate/mate.h"
#include "sketch/sketch.h"
#include "util/expr.h"

namespace aicad::doc {

using nlohmann::json;

namespace {

// ----------------------------------------------------------------- session store
// C18 active-sketch interaction state — C++ port of session.py::_SketchState. One
// sketch is "entered" at a time; sketch_enter populates it, solve/drag warm-start
// across it, sketch_exit clears it. solve_state is the warm SolveState (nullopt ==
// cold); a doc mutation (apply_ops/undo/redo/revert) resets it to nullopt so a warm
// state is never reused across a doc edit (session.py: active_sketch.solve_state = None).
struct SketchState {
    std::string sketch_fid;
    std::optional<aicad::sketch::SolveState> solve_state;  // warm start (nullopt == cold)
    json last_entities = json::object();   // id -> solved coord dict (entity_map form)
    json last_statuses = json::object();   // id -> "under"|"full"|"over"
    std::string last_overall_status;       // "under"|"full"|"over"
    std::vector<double> plane_frame;       // 16 floats, row-major
};

struct Session {
    std::string doc_id;
    json doc;
    long revision = 0;
    std::vector<std::string> undo_stack;  // JSON snapshots (newest at back)
    std::vector<std::string> redo_stack;
    bool dirty = false;
    std::string path;                                  // current .step+ path (or empty)
    std::map<std::string, std::string> extra_entries;  // unknown .step+ entries (verbatim)
    std::map<std::string, std::string> checkpoints;    // PM-cancel snapshots: id -> doc JSON
    std::optional<SketchState> active_sketch;          // C18 sketch-edit lifecycle

    // C20 — assembly state. asm_result is the last assembly compile; asm_warm seeds the
    // next mate solve; explode_active/explode_factor drive tessellation-time offsets.
    std::shared_ptr<AssemblyResult> asm_result;        // nullptr until first assembly rebuild
    aicad::mate::WarmMap asm_warm;
    bool explode_active = false;
    double explode_factor = 1.0;

    bool is_assembly() const {
        return doc.is_object() && doc.value("schema", std::string()) == "aicad.asm/1";
    }
};

constexpr std::size_t kUndoLimit = 100;

std::mutex g_mtx;
std::map<std::string, Session> g_sessions;
std::atomic<unsigned long long> g_doc_seq{1};
std::atomic<unsigned long long> g_ckpt_seq{1};

std::string new_checkpoint_id() {
    // Opaque, unique-per-process id (Python uses a uuid4 prefix; the value is only
    // ever round-tripped back through doc_revert, so any unique token is faithful).
    unsigned long long n = g_ckpt_seq.fetch_add(1);
    char buf[32];
    std::snprintf(buf, sizeof(buf), "ckpt-%010llx", n);
    return std::string(buf);
}

std::string new_doc_id() {
    unsigned long long n = g_doc_seq.fetch_add(1);
    char buf[32];
    std::snprintf(buf, sizeof(buf), "doc-cpp%08llx", n);
    return std::string(buf);
}

Session& require_session(const std::string& doc_id) {
    auto it = g_sessions.find(doc_id);
    if (it == g_sessions.end())
        throw OpError("unknown_doc", "no such document: " + doc_id, {{"doc_id", doc_id}});
    return it->second;
}

json blank_part(const std::string& doc_id, const std::string& name) {
    return json{
        {"schema", "aicad.part/1"},
        {"id", doc_id},
        {"name", name.empty() ? std::string("Part1") : name},
        {"units", {{"system", "MMGS"}}},
        {"material", nullptr},
        {"equations", {{"globals", json::array()}}},
        {"features", json::array()},
    };
}

json blank_assembly(const std::string& doc_id, const std::string& name) {
    return json{
        {"schema", "aicad.asm/1"},
        {"id", doc_id},
        {"name", name.empty() ? std::string("Assembly1") : name},
        {"units", {{"system", "MMGS"}}},
        {"equations", {{"globals", json::array()}}},
        {"components", json::array()},
        {"mates", json::array()},
        {"features", json::array()},
        {"explode", json::array()},
    };
}

// ----------------------------------------------------------------- id allocation
// Monotonic per-family counters seeded from the document's existing ids (mirrors
// engine/doc/ops.py::_IdGen — never reuse a freed id).
struct IdGen {
    std::map<std::string, int> hi;  // prefix -> highest seen

    explicit IdGen(const json& doc) {
        scan(doc);
    }
    void note(const std::string& id) {
        // id = <prefix letters><digits>; record the max number per prefix.
        std::size_t i = 0;
        while (i < id.size() && !std::isdigit((unsigned char)id[i])) ++i;
        if (i == 0 || i == id.size()) return;
        std::string prefix = id.substr(0, i);
        int n = std::atoi(id.c_str() + i);
        if (n > hi[prefix]) hi[prefix] = n;
    }
    void scan(const json& doc) {
        if (doc.contains("features") && doc["features"].is_array()) {
            for (const json& f : doc["features"]) {
                if (f.contains("id") && f["id"].is_string()) note(f["id"].get<std::string>());
                // sketch entities/constraints live under params.
                const json* params = f.contains("params") ? &f["params"] : nullptr;
                if (params && params->is_object()) {
                    for (const char* key : {"entities", "constraints"}) {
                        if (params->contains(key) && (*params)[key].is_array())
                            for (const json& e : (*params)[key])
                                if (e.contains("id") && e["id"].is_string())
                                    note(e["id"].get<std::string>());
                    }
                }
            }
        }
    }
    std::string next(const std::string& prefix) {
        int n = ++hi[prefix];
        return prefix + std::to_string(n);
    }
};

// ----------------------------------------------------------------- placeholders
// Resolve "$N" / "$N#k" placeholders (created-id back-references) anywhere in a
// json value, in place, against the batch id_map. "$self" is handled by the caller
// (it is entity-local). Mirrors ops.py::_resolve_placeholders_deep.
void resolve_placeholders_deep(json& v, const json& id_map) {
    if (v.is_string()) {
        const std::string& s = v.get_ref<const std::string&>();
        if (!s.empty() && s[0] == '$' && id_map.contains(s))
            v = id_map[s];
        return;
    }
    if (v.is_array()) {
        for (json& e : v) resolve_placeholders_deep(e, id_map);
    } else if (v.is_object()) {
        for (auto it = v.begin(); it != v.end(); ++it) resolve_placeholders_deep(it.value(), id_map);
    }
}

// ----------------------------------------------------------------- helpers
// Find a feature by id in doc["features"]; returns nullptr if absent.
json* find_feature(json& doc, const std::string& id) {
    if (!doc.contains("features")) return nullptr;
    for (json& f : doc["features"])
        if (f.value("id", std::string()) == id) return &f;
    return nullptr;
}

// Patch-merge: present key replaces; a null value DELETES the key (ops.py::_patch).
void patch(json& target, const json& patchobj) {
    if (!patchobj.is_object()) return;
    for (auto it = patchobj.begin(); it != patchobj.end(); ++it) {
        if (it.value().is_null())
            target.erase(it.key());
        else
            target[it.key()] = it.value();
    }
}

// Collect the feature ids a feature references (deep scan of refs for fN tokens +
// region/body selector owner fields). Used for the delete dependency guard.
void collect_fids(const json& v, std::set<std::string>& out) {
    static const std::regex fid_re("^f[0-9]+$");
    if (v.is_string()) {
        const std::string& s = v.get_ref<const std::string&>();
        if (std::regex_match(s, fid_re)) out.insert(s);
    } else if (v.is_array()) {
        for (const json& e : v) collect_fids(e, out);
    } else if (v.is_object()) {
        for (auto it = v.begin(); it != v.end(); ++it) collect_fids(it.value(), out);
    }
}

// The sketch feature a sketch_* op targets (id may be a resolved placeholder).
json& require_sketch_feature(json& doc, const json& op, const json& id_map) {
    json sk_ref = op.at("sketch");
    resolve_placeholders_deep(sk_ref, id_map);
    if (!sk_ref.is_string())
        throw OpError("invalid_op", "sketch op: 'sketch' must be a feature id");
    json* f = find_feature(doc, sk_ref.get<std::string>());
    if (!f) throw OpError("invalid_op", "sketch op: no sketch '" + sk_ref.get<std::string>() + "'");
    if (f->value("type", std::string()) != "sketch")
        throw OpError("invalid_op", "sketch op: feature is not a sketch");
    if (!f->contains("params")) (*f)["params"] = json::object();
    json& p = (*f)["params"];
    if (!p.contains("entities")) p["entities"] = json::array();
    if (!p.contains("constraints")) p["constraints"] = json::array();
    return *f;
}

// ----------------------------------------------------------------- op handlers
void op_add_feature(json& doc, const json& op, std::size_t idx, IdGen& gen, json& id_map) {
    if (!op.contains("feature") || !op["feature"].is_object())
        throw OpError("invalid_op", "add_feature: 'feature' object required");
    const std::string fid = gen.next("f");
    json feat = op["feature"];
    json env;
    env["id"] = fid;
    env["type"] = feat.value("type", std::string());
    env["name"] = feat.value("name", fid);
    env["suppressed"] = feat.value("suppressed", false);
    env["params"] = feat.contains("params") ? feat["params"] : json::object();
    env["refs"] = feat.contains("refs") ? feat["refs"] : json::object();
    if (feat.contains("folder")) env["folder"] = feat["folder"];
    resolve_placeholders_deep(env["params"], id_map);
    resolve_placeholders_deep(env["refs"], id_map);

    json& feats = doc["features"];
    if (op.contains("before") && op["before"].is_string()) {
        const std::string before = op["before"].get<std::string>();
        std::size_t pos = feats.size();
        for (std::size_t i = 0; i < feats.size(); ++i)
            if (feats[i].value("id", std::string()) == before) { pos = i; break; }
        feats.insert(feats.begin() + pos, env);
    } else {
        feats.push_back(env);
    }
    id_map["$" + std::to_string(idx)] = fid;
}

void op_edit_feature(json& doc, const json& op, const json& id_map) {
    json* f = find_feature(doc, op.value("id", std::string()));
    if (!f) throw OpError("invalid_op", "edit_feature: no feature '" + op.value("id", std::string()) + "'");
    if (op.contains("params")) {
        json p = op["params"];
        resolve_placeholders_deep(p, id_map);
        if (!f->contains("params")) (*f)["params"] = json::object();
        patch((*f)["params"], p);
    }
    if (op.contains("refs")) {
        json r = op["refs"];
        resolve_placeholders_deep(r, id_map);
        if (!f->contains("refs")) (*f)["refs"] = json::object();
        patch((*f)["refs"], r);
    }
    if (op.contains("name") && op["name"].is_string()) (*f)["name"] = op["name"];
}

void op_delete_feature(json& doc, const json& op) {
    const std::string id = op.value("id", std::string());
    json& feats = doc["features"];
    bool found = false;
    for (const json& f : feats)
        if (f.value("id", std::string()) == id) { found = true; break; }
    if (!found) throw OpError("invalid_op", "delete_feature: no feature '" + id + "'");

    // Dependency guard: any OTHER feature referencing this one blocks (unless cascade).
    std::vector<std::string> dependents;
    for (const json& f : feats) {
        if (f.value("id", std::string()) == id) continue;
        std::set<std::string> refs;
        if (f.contains("refs")) collect_fids(f["refs"], refs);
        if (refs.count(id)) dependents.push_back(f.value("id", std::string()));
    }
    const bool cascade = op.value("cascade", false);
    if (!dependents.empty() && !cascade)
        throw OpError("dependency_exists",
                      "feature '" + id + "' is referenced by other features",
                      {{"dependents", dependents}});

    std::set<std::string> remove{id};
    if (cascade) for (const std::string& d : dependents) remove.insert(d);
    json kept = json::array();
    for (const json& f : feats)
        if (!remove.count(f.value("id", std::string()))) kept.push_back(f);
    feats = kept;
}

void op_reorder_feature(json& doc, const json& op) {
    const std::string id = op.value("id", std::string());
    json& feats = doc["features"];
    json moved;
    json rest = json::array();
    bool found = false;
    for (const json& f : feats) {
        if (f.value("id", std::string()) == id) { moved = f; found = true; }
        else rest.push_back(f);
    }
    if (!found) throw OpError("invalid_op", "reorder_feature: no feature '" + id + "'");
    std::size_t pos = rest.size();
    if (op.contains("before") && op["before"].is_string()) {
        const std::string before = op["before"].get<std::string>();
        for (std::size_t i = 0; i < rest.size(); ++i)
            if (rest[i].value("id", std::string()) == before) { pos = i; break; }
    }
    rest.insert(rest.begin() + pos, moved);
    feats = rest;
}

void op_sketch_add_entity(json& doc, const json& op, std::size_t idx, IdGen& gen, json& id_map) {
    json& sk = require_sketch_feature(doc, op, id_map);
    json ent = op.at("entity");
    const std::string eid = gen.next("e");
    ent["id"] = eid;
    if (!ent.contains("construction")) ent["construction"] = false;
    sk["params"]["entities"].push_back(ent);
    id_map["$" + std::to_string(idx)] = eid;
    // Inline constraints: "$self" -> the new entity id; ids cN.
    if (op.contains("constraints") && op["constraints"].is_array()) {
        for (const json& c0 : op["constraints"]) {
            json c = c0;
            // resolve $self (and $self.sub) in refs to the new entity id.
            if (c.contains("refs") && c["refs"].is_array()) {
                for (json& rref : c["refs"]) {
                    if (rref.is_string()) {
                        std::string s = rref.get<std::string>();
                        if (s == "$self") rref = eid;
                        else if (s.rfind("$self.", 0) == 0) rref = eid + s.substr(5);
                    }
                }
                resolve_placeholders_deep(c["refs"], id_map);
            }
            c["id"] = gen.next("c");
            sk["params"]["constraints"].push_back(c);
        }
    }
}

void op_sketch_add_constraint(json& doc, const json& op, std::size_t idx, IdGen& gen, json& id_map) {
    json& sk = require_sketch_feature(doc, op, id_map);
    json c = op.at("constraint");
    resolve_placeholders_deep(c, id_map);
    const std::string cid = gen.next("c");
    c["id"] = cid;
    sk["params"]["constraints"].push_back(c);
    id_map["$" + std::to_string(idx)] = cid;
}

void op_sketch_edit_entity(json& doc, const json& op, const json& id_map) {
    json& sk = require_sketch_feature(doc, op, id_map);
    const std::string eid = op.value("id", std::string());
    for (json& e : sk["params"]["entities"]) {
        if (e.value("id", std::string()) == eid) {
            if (op.contains("params")) patch(e, op["params"]);
            return;
        }
    }
    throw OpError("invalid_op", "sketch_edit_entity: no entity '" + eid + "'");
}

void op_sketch_delete_entity(json& doc, const json& op, const json& id_map) {
    json& sk = require_sketch_feature(doc, op, id_map);
    const std::string eid = op.value("id", std::string());
    json kept = json::array();
    for (const json& e : sk["params"]["entities"])
        if (e.value("id", std::string()) != eid) kept.push_back(e);
    sk["params"]["entities"] = kept;
    // Cascade-drop constraints whose handles touch the entity (ref starts "eid" or "eid.").
    json kc = json::array();
    for (const json& c : sk["params"]["constraints"]) {
        bool touches = false;
        if (c.contains("refs") && c["refs"].is_array())
            for (const json& rref : c["refs"])
                if (rref.is_string()) {
                    const std::string s = rref.get<std::string>();
                    if (s == eid || s.rfind(eid + ".", 0) == 0) { touches = true; break; }
                }
        if (!touches) kc.push_back(c);
    }
    sk["params"]["constraints"] = kc;
}

void op_sketch_delete_constraint(json& doc, const json& op, const json& id_map) {
    json& sk = require_sketch_feature(doc, op, id_map);
    const std::string cid = op.value("id", std::string());
    json kept = json::array();
    for (const json& c : sk["params"]["constraints"])
        if (c.value("id", std::string()) != cid) kept.push_back(c);
    sk["params"]["constraints"] = kept;
}

void op_sketch_set_dim(json& doc, const json& op, const json& id_map) {
    json& sk = require_sketch_feature(doc, op, id_map);
    const std::string cid = op.value("id", std::string());
    for (json& c : sk["params"]["constraints"]) {
        if (c.value("id", std::string()) == cid) {
            if (op.contains("value")) c["value"] = op["value"];
            if (op.contains("expr")) c["expr"] = op["expr"];
            if (op.contains("placement")) c["placement"] = op["placement"];
            if (op.contains("driven")) c["driven"] = op["driven"];
            return;
        }
    }
    throw OpError("invalid_op", "sketch_set_dim: no constraint '" + cid + "'");
}

void op_sketch_toggle_construction(json& doc, const json& op, const json& id_map) {
    json& sk = require_sketch_feature(doc, op, id_map);
    const std::string eid = op.value("id", std::string());
    for (json& e : sk["params"]["entities"])
        if (e.value("id", std::string()) == eid) {
            e["construction"] = !e.value("construction", false);
            return;
        }
    throw OpError("invalid_op", "sketch_toggle_construction: no entity '" + eid + "'");
}

// ----------------------------------------------------------- advanced sketch ops
// 2D tools (trim/extend/offset/fillet/chamfer/mirror/pattern/move) — port of the
// engine/doc/ops.py handlers over the engine/doc/sketch_geom curve math. Each mutates
// the sketch's entity + constraint lists and flows through the normal rebuild.
namespace sg = aicad::doc::sketchgeom;

constexpr double kDeg = 3.14159265358979323846 / 180.0;
constexpr double kRad = 180.0 / 3.14159265358979323846;

// A geom-only view of a stored entity (drops id/construction; sketch_geom shape).
json geom_view(const json& ent) {
    json o = json::object();
    for (auto it = ent.begin(); it != ent.end(); ++it)
        if (it.key() != "id" && it.key() != "construction") o[it.key()] = it.value();
    return o;
}
json stamp_entity(const json& geom, const std::string& eid, bool construction) {
    json o = geom;
    o["id"] = eid;
    o["construction"] = construction;
    return o;
}
std::string handle_base(const std::string& h) {
    auto p = h.find('.');
    return p == std::string::npos ? h : h.substr(0, p);
}
void drop_constraints_touching(json& sk, const std::set<std::string>& eids) {
    json& cons = sk["params"]["constraints"];
    json kept = json::array();
    for (const json& c : cons) {
        bool touch = false;
        if (c.contains("refs") && c["refs"].is_array())
            for (const json& h : c["refs"])
                if (h.is_string() && eids.count(handle_base(h.get<std::string>()))) { touch = true; break; }
        if (!touch) kept.push_back(c);
    }
    cons = kept;
}
json* find_sketch_entity(json& sk, const std::string& eid) {
    for (json& e : sk["params"]["entities"])
        if (e.value("id", std::string()) == eid) return &e;
    return nullptr;
}
[[noreturn]] void unsupported_entity(std::size_t idx, const std::string& op_name,
                                     const std::string& etype) {
    throw OpError("unsupported_entity",
                  "op[" + std::to_string(idx) + "]: " + op_name +
                      " does not support entity type '" + etype + "'",
                  {{"index", idx}, {"reason", "unsupported entity '" + etype + "'"}});
}

void op_sketch_trim(json& doc, const json& op, std::size_t idx, IdGen& gen, json& id_map) {
    json& sk = require_sketch_feature(doc, op, id_map);
    const std::string eid = op.value("entity", std::string());
    json& ents = sk["params"]["entities"];
    json* target = find_sketch_entity(sk, eid);
    if (!target) throw OpError("invalid_op", "sketch_trim: entity '" + eid + "' not found");
    if (!op.contains("point") || !op["point"].is_array() || op["point"].size() != 2)
        throw OpError("invalid_op", "sketch_trim requires 'point': [u, v]");
    double px = op["point"][0].get<double>(), py = op["point"][1].get<double>();
    std::size_t pos = 0;
    for (std::size_t i = 0; i < ents.size(); ++i)
        if (ents[i].value("id", std::string()) == eid) { pos = i; break; }
    bool construction = target->value("construction", false);
    std::vector<json> others;
    for (const json& e : ents)
        if (e.value("id", std::string()) != eid && !e.value("construction", false))
            others.push_back(geom_view(e));
    std::vector<json> spans;
    try {
        spans = sg::split_entity(geom_view(*target), others, px, py);
    } catch (const sg::UnsupportedEntity& exc) {
        throw OpError("unsupported_entity",
                      "op[" + std::to_string(idx) + "]: sketch_trim involves an unsupported entity (" +
                          exc.etype + ")",
                      {{"index", idx}, {"reason", "unsupported entity '" + exc.etype + "'"}});
    }
    if (spans.empty()) {
        json kept = json::array();
        for (const json& e : ents)
            if (e.value("id", std::string()) != eid) kept.push_back(e);
        ents = kept;
        drop_constraints_touching(sk, {eid});
        id_map["$" + std::to_string(idx)] = "";
        return;
    }
    std::vector<json> new_ents;
    new_ents.push_back(stamp_entity(spans[0], eid, construction));
    std::vector<std::string> extra_ids;
    for (std::size_t i = 1; i < spans.size(); ++i) {
        std::string neid = gen.next("e");
        extra_ids.push_back(neid);
        new_ents.push_back(stamp_entity(spans[i], neid, construction));
    }
    json spliced = json::array();
    for (std::size_t i = 0; i < ents.size(); ++i) {
        if (i == pos)
            for (const json& ne : new_ents) spliced.push_back(ne);
        else
            spliced.push_back(ents[i]);
    }
    ents = spliced;
    drop_constraints_touching(sk, {eid});
    id_map["$" + std::to_string(idx)] = eid;
    for (std::size_t k = 0; k < extra_ids.size(); ++k)
        id_map["$" + std::to_string(idx) + "#" + std::to_string(k + 1)] = extra_ids[k];
}

void op_sketch_extend(json& doc, const json& op, std::size_t idx, IdGen& gen, json& id_map) {
    (void)gen;
    json& sk = require_sketch_feature(doc, op, id_map);
    const std::string eid = op.value("entity", std::string());
    json* target = find_sketch_entity(sk, eid);
    if (!target) throw OpError("invalid_op", "sketch_extend: entity '" + eid + "' not found");
    const std::string end = op.value("end", std::string());
    if (end != "start" && end != "end")
        throw OpError("invalid_op", "sketch_extend requires 'end': 'start' | 'end'");
    std::vector<json> others;
    for (const json& e : sk["params"]["entities"])
        if (e.value("id", std::string()) != eid && !e.value("construction", false))
            others.push_back(geom_view(e));
    std::optional<json> new_geom;
    try {
        new_geom = sg::extend_entity(geom_view(*target), end, others);
    } catch (const sg::UnsupportedEntity& exc) {
        throw OpError("unsupported_entity",
                      "op[" + std::to_string(idx) + "]: sketch_extend involves an unsupported entity (" +
                          exc.etype + ")",
                      {{"index", idx}, {"reason", "unsupported entity '" + exc.etype + "'"}});
    }
    if (!new_geom)
        throw OpError("extend_no_target",
                      "op[" + std::to_string(idx) + "]: sketch_extend found nothing to extend '" + eid + "' to",
                      {{"index", idx}, {"reason", "no target ahead of the extended end"}});
    std::string id = (*target)["id"].get<std::string>();
    bool construction = target->value("construction", false);
    json updated = *new_geom;
    updated["id"] = id;
    updated["construction"] = construction;
    *target = updated;
    id_map["$" + std::to_string(idx)] = eid;
}

void op_sketch_offset(json& doc, const json& op, std::size_t idx, IdGen& gen, json& id_map) {
    json& sk = require_sketch_feature(doc, op, id_map);
    if (!op.contains("entities") || !op["entities"].is_array() || op["entities"].empty())
        throw OpError("invalid_op", "sketch_offset requires a non-empty 'entities' id list");
    if (!op.contains("distance") || !op["distance"].is_number())
        throw OpError("invalid_op", "sketch_offset requires a numeric 'distance'");
    if (!op.contains("side") || !op["side"].is_array() || op["side"].size() != 2)
        throw OpError("invalid_op", "sketch_offset requires 'side': [u, v]");
    double distance = op["distance"].get<double>();
    double sx = op["side"][0].get<double>(), sy = op["side"][1].get<double>();
    std::vector<json> chain;
    for (const json& e_id : op["entities"]) {
        json* ent = find_sketch_entity(sk, e_id.get<std::string>());
        if (!ent)
            throw OpError("invalid_op", "sketch_offset: entity '" + e_id.get<std::string>() + "' not found");
        chain.push_back(geom_view(*ent));
    }
    std::vector<json> out;
    bool closed = false;
    try {
        out = sg::offset_chain(chain, distance, sx, sy);
        closed = sg::chain_is_closed(chain);
    } catch (const sg::UnsupportedEntity& exc) {
        throw OpError("unsupported_entity",
                      "op[" + std::to_string(idx) + "]: sketch_offset involves an unsupported entity (" +
                          exc.etype + ")",
                      {{"index", idx}, {"reason", "unsupported entity '" + exc.etype + "'"}});
    }
    json& ents = sk["params"]["entities"];
    json& cons = sk["params"]["constraints"];
    std::vector<std::string> new_ids;
    std::vector<std::string> new_types;
    for (const json& span : out) {
        std::string neid = gen.next("e");
        new_ids.push_back(neid);
        new_types.push_back(span.value("type", std::string()));
        ents.push_back(stamp_entity(span, neid, false));
    }
    auto end_handle = [&](std::size_t i, const std::string& which) -> std::string {
        if (new_types[i] == "line") return new_ids[i] + (which == "start" ? ".p1" : ".p2");
        return new_ids[i] + "." + which;
    };
    int n = (int)new_ids.size();
    int joints = (closed && n > 1) ? n : n - 1;
    for (int i = 0; i < joints; ++i) {
        int a = i, b = (i + 1) % n;
        cons.push_back({{"id", gen.next("c")}, {"type", "coincident"},
                        {"refs", {end_handle(a, "end"), end_handle(b, "start")}}});
    }
    id_map["$" + std::to_string(idx)] = new_ids.empty() ? std::string() : new_ids[0];
    for (std::size_t k = 1; k < new_ids.size(); ++k)
        id_map["$" + std::to_string(idx) + "#" + std::to_string(k)] = new_ids[k];
}

// Map a geom end name ('start'|'end') to a line's point handle ('p1'|'p2').
std::string line_end_handle(const std::string& eid, const std::string& which) {
    return eid + (which == "start" ? ".p1" : ".p2");
}
void set_line_end(json& ent, const std::string& which, const json& ptj) {
    ent[which == "start" ? "p1" : "p2"] = {ptj[0].get<double>(), ptj[1].get<double>()};
}
// Drop any constraint that JOINED the two corner endpoints to each other.
void drop_corner_join(json& sk, const std::string& h1, const std::string& h2) {
    json& cons = sk["params"]["constraints"];
    json kept = json::array();
    for (const json& c : cons) {
        bool has1 = false, has2 = false;
        if (c.contains("refs") && c["refs"].is_array())
            for (const json& r : c["refs"])
                if (r.is_string()) {
                    if (r.get<std::string>() == h1) has1 = true;
                    if (r.get<std::string>() == h2) has2 = true;
                }
        if (has1 && has2) continue;
        kept.push_back(c);
    }
    cons = kept;
}

void op_sketch_fillet(json& doc, const json& op, std::size_t idx, IdGen& gen, json& id_map) {
    json& sk = require_sketch_feature(doc, op, id_map);
    const std::string e1id = op.value("e1", std::string()), e2id = op.value("e2", std::string());
    json* e1 = find_sketch_entity(sk, e1id);
    json* e2 = find_sketch_entity(sk, e2id);
    if (!e1 || !e2)
        throw OpError("invalid_op", "sketch_fillet requires existing 'e1' and 'e2' entity ids");
    if (e1id == e2id)
        throw OpError("invalid_op", "sketch_fillet: e1 and e2 must be different entities");
    if (!op.contains("radius") || !op["radius"].is_number() || op["radius"].get<double>() <= 0)
        throw OpError("invalid_op", "sketch_fillet requires a positive numeric 'radius'");
    double radius = op["radius"].get<double>();
    for (json* ent : {e1, e2})
        if (ent->value("type", std::string()) != "line")
            unsupported_entity(idx, "sketch_fillet supports line-line corners only — got", ent->value("type", std::string()));
    json res;
    try {
        res = sg::fillet_corner(geom_view(*e1), geom_view(*e2), radius);
    } catch (const sg::GeomError& exc) {
        throw OpError("fillet_failed", "op[" + std::to_string(idx) + "]: sketch_fillet — " + exc.reason,
                      {{"index", idx}, {"reason", exc.reason}});
    }
    drop_corner_join(sk, line_end_handle(e1id, res["end_a"].get<std::string>()),
                     line_end_handle(e2id, res["end_b"].get<std::string>()));
    set_line_end(*e1, res["end_a"].get<std::string>(), res["ta"]);
    set_line_end(*e2, res["end_b"].get<std::string>(), res["tb"]);
    const std::string aid = gen.next("e");
    json arc = res["arc"];
    arc["id"] = aid;
    arc["construction"] = false;
    sk["params"]["entities"].push_back(arc);
    json& cons = sk["params"]["constraints"];
    cons.push_back({{"id", gen.next("c")}, {"type", "coincident"},
                    {"refs", {aid + ".start", line_end_handle(e1id, res["end_a"].get<std::string>())}}});
    cons.push_back({{"id", gen.next("c")}, {"type", "coincident"},
                    {"refs", {aid + ".end", line_end_handle(e2id, res["end_b"].get<std::string>())}}});
    cons.push_back({{"id", gen.next("c")}, {"type", "tangent"}, {"refs", {aid, e1id}}});
    cons.push_back({{"id", gen.next("c")}, {"type", "tangent"}, {"refs", {aid, e2id}}});
    id_map["$" + std::to_string(idx)] = aid;
}

void op_sketch_chamfer(json& doc, const json& op, std::size_t idx, IdGen& gen, json& id_map) {
    json& sk = require_sketch_feature(doc, op, id_map);
    const std::string e1id = op.value("e1", std::string()), e2id = op.value("e2", std::string());
    json* e1 = find_sketch_entity(sk, e1id);
    json* e2 = find_sketch_entity(sk, e2id);
    if (!e1 || !e2)
        throw OpError("invalid_op", "sketch_chamfer requires existing 'e1' and 'e2' entity ids");
    if (e1id == e2id)
        throw OpError("invalid_op", "sketch_chamfer: e1 and e2 must be different entities");
    if (!op.contains("distance") || !op["distance"].is_number() || op["distance"].get<double>() <= 0)
        throw OpError("invalid_op", "sketch_chamfer requires a positive numeric 'distance'");
    double distance = op["distance"].get<double>();
    double distance2 = distance;
    if (op.contains("distance2") && !op["distance2"].is_null()) {
        if (!op["distance2"].is_number() || op["distance2"].get<double>() <= 0)
            throw OpError("invalid_op", "sketch_chamfer 'distance2' must be a positive number when given");
        distance2 = op["distance2"].get<double>();
    }
    for (json* ent : {e1, e2})
        if (ent->value("type", std::string()) != "line")
            unsupported_entity(idx, "sketch_chamfer supports line-line corners only — got", ent->value("type", std::string()));
    json res;
    try {
        res = sg::chamfer_corner(geom_view(*e1), geom_view(*e2), distance, distance2);
    } catch (const sg::GeomError& exc) {
        throw OpError("chamfer_failed", "op[" + std::to_string(idx) + "]: sketch_chamfer — " + exc.reason,
                      {{"index", idx}, {"reason", exc.reason}});
    }
    drop_corner_join(sk, line_end_handle(e1id, res["end_a"].get<std::string>()),
                     line_end_handle(e2id, res["end_b"].get<std::string>()));
    set_line_end(*e1, res["end_a"].get<std::string>(), res["ta"]);
    set_line_end(*e2, res["end_b"].get<std::string>(), res["tb"]);
    const std::string lid = gen.next("e");
    json line = {{"id", lid}, {"type", "line"}, {"construction", false},
                 {"p1", {res["ta"][0].get<double>(), res["ta"][1].get<double>()}},
                 {"p2", {res["tb"][0].get<double>(), res["tb"][1].get<double>()}}};
    sk["params"]["entities"].push_back(line);
    json& cons = sk["params"]["constraints"];
    cons.push_back({{"id", gen.next("c")}, {"type", "coincident"},
                    {"refs", {lid + ".p1", line_end_handle(e1id, res["end_a"].get<std::string>())}}});
    cons.push_back({{"id", gen.next("c")}, {"type", "coincident"},
                    {"refs", {lid + ".p2", line_end_handle(e2id, res["end_b"].get<std::string>())}}});
    id_map["$" + std::to_string(idx)] = lid;
}

// ---- inline 2D transforms for mirror / pattern / move ----
const std::set<std::string> kMpSupported = {"line", "circle", "arc"};

std::array<double, 2> reflect_point(const json& ptj, const std::array<double, 2>& A,
                                    const std::array<double, 2>& B) {
    double ax = A[0], ay = A[1], bx = B[0], by = B[1];
    double dx = bx - ax, dy = by - ay, L2 = dx * dx + dy * dy;
    if (L2 < 1e-18)
        throw OpError("mirror_failed", "mirror line is degenerate (zero length)",
                      {{"reason", "degenerate mirror line"}});
    double pxv = ptj[0].get<double>(), pyv = ptj[1].get<double>();
    double px0 = pxv - ax, py0 = pyv - ay;
    double t = (px0 * dx + py0 * dy) / L2;
    double fx = ax + t * dx, fy = ay + t * dy;
    return {2.0 * fx - pxv, 2.0 * fy - pyv};
}
double reflect_angle_deg(double ang, const std::array<double, 2>& A, const std::array<double, 2>& B) {
    double axis = std::atan2(B[1] - A[1], B[0] - A[0]);
    return (2.0 * axis - ang * kDeg) * kRad;
}
json mirror_entity(const json& ent, const std::array<double, 2>& A, const std::array<double, 2>& B) {
    std::string t = ent.value("type", std::string());
    if (t == "line") {
        auto p1 = reflect_point(ent["p1"], A, B);
        auto p2 = reflect_point(ent["p2"], A, B);
        return {{"type", "line"}, {"p1", {p1[0], p1[1]}}, {"p2", {p2[0], p2[1]}}};
    }
    if (t == "circle") {
        auto c = reflect_point(ent["center"], A, B);
        return {{"type", "circle"}, {"center", {c[0], c[1]}}, {"radius", ent["radius"].get<double>()}};
    }
    if (t == "arc") {
        double a0 = reflect_angle_deg(ent["start"].get<double>(), A, B);
        double a1 = reflect_angle_deg(ent["end"].get<double>(), A, B);
        auto c = reflect_point(ent["center"], A, B);
        return {{"type", "arc"}, {"center", {c[0], c[1]}}, {"radius", ent["radius"].get<double>()},
                {"start", a1}, {"end", a0}, {"ccw", !ent.value("ccw", true)}};
    }
    throw OpError("unsupported_entity", "mirror/pattern does not support entity type '" + t + "'",
                  {{"reason", "unsupported entity '" + t + "'"}});
}
json translate_entity(const json& geom, double dx, double dy) {
    json out = geom;
    std::string t = out.value("type", std::string());
    if (t == "line") {
        out["p1"] = {out["p1"][0].get<double>() + dx, out["p1"][1].get<double>() + dy};
        out["p2"] = {out["p2"][0].get<double>() + dx, out["p2"][1].get<double>() + dy};
    } else {
        out["center"] = {out["center"][0].get<double>() + dx, out["center"][1].get<double>() + dy};
    }
    return out;
}
json rotate_entity(const json& geom, double cx, double cy, double ang_deg) {
    double a = ang_deg * kDeg, ca = std::cos(a), sa = std::sin(a);
    auto rot = [&](const json& p) -> json {
        double x = p[0].get<double>() - cx, y = p[1].get<double>() - cy;
        return json::array({cx + x * ca - y * sa, cy + x * sa + y * ca});
    };
    json out = geom;
    std::string t = out.value("type", std::string());
    if (t == "line") {
        out["p1"] = rot(out["p1"]);
        out["p2"] = rot(out["p2"]);
    } else {
        out["center"] = rot(out["center"]);
        if (t == "arc") {
            out["start"] = out["start"].get<double>() + ang_deg;
            out["end"] = out["end"].get<double>() + ang_deg;
        }
    }
    return out;
}
// Point-handle pairs whose symmetric relation keeps a<->b mirrored, + radius-equal flag.
std::pair<std::vector<std::pair<std::string, std::string>>, bool>
symmetric_pairs(const std::string& etype, const std::string& a, const std::string& b) {
    if (etype == "line")
        return {{{a + ".p1", b + ".p1"}, {a + ".p2", b + ".p2"}}, false};
    if (etype == "circle")
        return {{{a + ".center", b + ".center"}}, true};
    if (etype == "arc")
        return {{{a + ".center", b + ".center"}, {a + ".start", b + ".start"}, {a + ".end", b + ".end"}}, true};
    return {{}, false};
}

void op_sketch_mirror_entities(json& doc, const json& op, std::size_t idx, IdGen& gen, json& id_map) {
    json& sk = require_sketch_feature(doc, op, id_map);
    if (!op.contains("entities") || !op["entities"].is_array() || op["entities"].empty())
        throw OpError("invalid_op", "sketch_mirror_entities requires a non-empty 'entities' id list");
    const std::string line_id = op.value("line", std::string());
    if (line_id.empty())
        throw OpError("invalid_op", "sketch_mirror_entities requires a 'line' (mirror line entity id)");
    json* axis = find_sketch_entity(sk, line_id);
    if (!axis)
        throw OpError("invalid_op", "sketch_mirror_entities: mirror line '" + line_id + "' not found");
    if (axis->value("type", std::string()) != "line")
        throw OpError("unsupported_entity", "sketch_mirror_entities: the mirror line must be a line entity",
                      {{"reason", "mirror line is a '" + axis->value("type", std::string()) + "'"}});
    std::array<double, 2> A = {(*axis)["p1"][0].get<double>(), (*axis)["p1"][1].get<double>()};
    std::array<double, 2> B = {(*axis)["p2"][0].get<double>(), (*axis)["p2"][1].get<double>()};
    std::vector<json> sources;
    for (const json& e_id : op["entities"]) {
        const std::string sid = e_id.get<std::string>();
        if (sid == line_id)
            throw OpError("invalid_op", "sketch_mirror_entities: cannot mirror the mirror line itself");
        json* ent = find_sketch_entity(sk, sid);
        if (!ent)
            throw OpError("invalid_op", "sketch_mirror_entities: entity '" + sid + "' not found");
        if (!kMpSupported.count(ent->value("type", std::string())))
            unsupported_entity(idx, "sketch_mirror_entities", ent->value("type", std::string()));
        sources.push_back(*ent);
    }
    json& ents = sk["params"]["entities"];
    json& cons = sk["params"]["constraints"];
    std::vector<std::string> new_ids;
    for (const json& ent : sources) {
        json geom = mirror_entity(ent, A, B);
        std::string neid = gen.next("e");
        new_ids.push_back(neid);
        ents.push_back(stamp_entity(geom, neid, ent.value("construction", false)));
        auto [pairs, needs_equal] = symmetric_pairs(ent.value("type", std::string()),
                                                    ent.value("id", std::string()), neid);
        for (auto& [ha, hb] : pairs)
            cons.push_back({{"id", gen.next("c")}, {"type", "symmetric"}, {"refs", {ha, hb, line_id}}});
        if (needs_equal)
            cons.push_back({{"id", gen.next("c")}, {"type", "equal"},
                            {"refs", {ent.value("id", std::string()), neid}}});
    }
    id_map["$" + std::to_string(idx)] = new_ids.empty() ? std::string() : new_ids[0];
    for (std::size_t k = 1; k < new_ids.size(); ++k)
        id_map["$" + std::to_string(idx) + "#" + std::to_string(k)] = new_ids[k];
}

void op_sketch_pattern_entities(json& doc, const json& op, std::size_t idx, IdGen& gen, json& id_map) {
    json& sk = require_sketch_feature(doc, op, id_map);
    if (!op.contains("entities") || !op["entities"].is_array() || op["entities"].empty())
        throw OpError("invalid_op", "sketch_pattern_entities requires a non-empty 'entities' id list");
    const std::string mode = op.value("mode", std::string());
    if (mode != "linear" && mode != "circular")
        throw OpError("invalid_op", "sketch_pattern_entities requires mode 'linear' or 'circular'");
    if (!op.contains("count") || !op["count"].is_number_integer() || op["count"].get<int>() < 2)
        throw OpError("invalid_op", "sketch_pattern_entities requires an integer 'count' >= 2");
    int count = op["count"].get<int>();
    std::vector<json> sources;
    for (const json& e_id : op["entities"]) {
        json* ent = find_sketch_entity(sk, e_id.get<std::string>());
        if (!ent)
            throw OpError("invalid_op",
                          "sketch_pattern_entities: entity '" + e_id.get<std::string>() + "' not found");
        if (!kMpSupported.count(ent->value("type", std::string())))
            unsupported_entity(idx, "sketch_pattern_entities", ent->value("type", std::string()));
        sources.push_back(*ent);
    }
    // Per-step transforms: ("t", dx, dy) | ("r", cx, cy, ang).
    struct Step { bool rot; double a, b, c; };
    std::vector<Step> steps;
    if (mode == "linear") {
        if (!op.contains("dir") || !op["dir"].is_array() || op["dir"].size() != 2 ||
            !op["dir"][0].is_number() || !op["dir"][1].is_number())
            throw OpError("invalid_op", "sketch_pattern_entities (linear) requires 'dir': [dx, dy]");
        if (!op.contains("spacing") || !op["spacing"].is_number())
            throw OpError("invalid_op", "sketch_pattern_entities (linear) requires a numeric 'spacing'");
        double nx = op["dir"][0].get<double>(), ny = op["dir"][1].get<double>();
        double spacing = op["spacing"].get<double>();
        double L = std::hypot(nx, ny);
        if (L < 1e-12)
            throw OpError("invalid_op", "sketch_pattern_entities (linear) 'dir' is zero-length");
        nx /= L; ny /= L;
        for (int i = 1; i < count; ++i) steps.push_back({false, nx * spacing * i, ny * spacing * i, 0.0});
    } else {
        if (!op.contains("center") || !op["center"].is_array() || op["center"].size() != 2 ||
            !op["center"][0].is_number() || !op["center"][1].is_number())
            throw OpError("invalid_op", "sketch_pattern_entities (circular) requires 'center': [x, y]");
        if (!op.contains("angle") || !op["angle"].is_number())
            throw OpError("invalid_op", "sketch_pattern_entities (circular) requires a numeric 'angle'");
        double cx = op["center"][0].get<double>(), cy = op["center"][1].get<double>();
        double angle = op["angle"].get<double>();
        bool equal = op.value("equal_spacing", false);
        double step_ang = equal ? (angle / count) : angle;
        for (int i = 1; i < count; ++i) steps.push_back({true, cx, cy, step_ang * i});
    }
    json& ents = sk["params"]["entities"];
    std::vector<std::string> new_ids;
    for (const Step& tf : steps) {
        for (const json& ent : sources) {
            json geom = geom_view(ent);
            json placed = tf.rot ? rotate_entity(geom, tf.a, tf.b, tf.c)
                                 : translate_entity(geom, tf.a, tf.b);
            std::string neid = gen.next("e");
            new_ids.push_back(neid);
            ents.push_back(stamp_entity(placed, neid, ent.value("construction", false)));
        }
    }
    id_map["$" + std::to_string(idx)] = new_ids.empty() ? std::string() : new_ids[0];
    for (std::size_t k = 1; k < new_ids.size(); ++k)
        id_map["$" + std::to_string(idx) + "#" + std::to_string(k)] = new_ids[k];
}

void op_sketch_move(json& doc, const json& op, std::size_t idx, IdGen& gen, json& id_map) {
    json& sk = require_sketch_feature(doc, op, id_map);
    if (!op.contains("entities") || !op["entities"].is_array() || op["entities"].empty())
        throw OpError("invalid_op", "sketch_move requires a non-empty 'entities' id list");
    if (!op.contains("dx") || !op["dx"].is_number())
        throw OpError("invalid_op", "sketch_move requires a numeric 'dx'");
    if (!op.contains("dy") || !op["dy"].is_number())
        throw OpError("invalid_op", "sketch_move requires a numeric 'dy'");
    double dx = op["dx"].get<double>(), dy = op["dy"].get<double>();
    bool copy_flag = false;
    if (op.contains("copy")) {
        if (!op["copy"].is_boolean()) throw OpError("invalid_op", "sketch_move 'copy' must be a bool");
        copy_flag = op["copy"].get<bool>();
    }
    std::vector<std::string> source_ids;
    for (const json& e_id : op["entities"]) {
        json* ent = find_sketch_entity(sk, e_id.get<std::string>());
        if (!ent)
            throw OpError("invalid_op", "sketch_move: entity '" + e_id.get<std::string>() + "' not found");
        if (!kMpSupported.count(ent->value("type", std::string())))
            unsupported_entity(idx, "sketch_move", ent->value("type", std::string()));
        source_ids.push_back(e_id.get<std::string>());
    }
    if (!copy_flag) {
        for (const std::string& sid : source_ids) {
            json* ent = find_sketch_entity(sk, sid);
            json moved = translate_entity(geom_view(*ent), dx, dy);
            for (auto it = moved.begin(); it != moved.end(); ++it) (*ent)[it.key()] = it.value();
        }
        id_map["$" + std::to_string(idx)] = source_ids.empty() ? std::string() : source_ids[0];
        return;
    }
    json& ents = sk["params"]["entities"];
    std::vector<std::string> new_ids;
    for (const std::string& sid : source_ids) {
        json* ent = find_sketch_entity(sk, sid);
        bool construction = ent->value("construction", false);
        json geom = translate_entity(geom_view(*ent), dx, dy);
        std::string neid = gen.next("e");
        new_ids.push_back(neid);
        ents.push_back(stamp_entity(geom, neid, construction));
    }
    id_map["$" + std::to_string(idx)] = new_ids.empty() ? std::string() : new_ids[0];
    for (std::size_t k = 1; k < new_ids.size(); ++k)
        id_map["$" + std::to_string(idx) + "#" + std::to_string(k)] = new_ids[k];
}

// Dispatch one op against the working doc.
// ----------------------------------------------------------------- assembly ops
// Port of engine/doc/ops.py::_op_asm_* (component + mate vocabulary). IdGen mints
// k<N> components and m<N> mates; the existing IdGen.note() already records k/m highs
// from a loaded doc (any prefix letters + digits) so counters never reuse a freed id.

const std::array<double, 16> kIdent16{1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1};

void op_asm_insert_component(json& doc, const json& op, std::size_t idx, IdGen& gen, json& id_map) {
    const json& source = op.contains("source") ? op["source"] : json(nullptr);
    bool ok_src = source.is_object() &&
                  ((source.contains("path") && source["path"].is_string()) ||
                   (source.contains("embedded") && source["embedded"].is_string()));
    if (!ok_src)
        throw OpError("invalid_op", "asm_insert_component requires source.{path|embedded}",
                      {{"index", idx}});
    const std::string kid = gen.next("k");
    if (!doc.contains("components") || !doc["components"].is_array()) doc["components"] = json::array();
    json& comps = doc["components"];
    bool first = comps.empty();
    // Default name = source file stem (SolidWorks names components from the file).
    std::string default_name = kid;
    if (source.contains("path") && source["path"].is_string()) {
        std::string stem = source["path"].get<std::string>();
        for (char& c : stem) if (c == '\\') c = '/';
        auto slash = stem.rfind('/');
        if (slash != std::string::npos) stem = stem.substr(slash + 1);
        static const char* exts[] = {".step+", ".step", ".stp", ".stl", ".brep", ".iges", ".igs"};
        std::string low = stem;
        std::transform(low.begin(), low.end(), low.begin(),
                       [](unsigned char c) { return (char)std::tolower(c); });
        for (const char* e : exts) {
            std::string es(e);
            if (low.size() >= es.size() && low.compare(low.size() - es.size(), es.size(), es) == 0) {
                stem = stem.substr(0, stem.size() - es.size());
                break;
            }
        }
        default_name = stem.empty() ? kid : stem;
    }
    json comp;
    comp["id"] = kid;
    comp["name"] = (op.contains("name") && op["name"].is_string() && !op["name"].get<std::string>().empty())
                       ? op["name"] : json(default_name);
    comp["source"] = source;
    comp["transform"] = (op.contains("transform") && op["transform"].is_array())
                            ? op["transform"] : json(kIdent16);
    comp["fixed"] = op.contains("fixed") ? json(op.value("fixed", first)) : json(first);
    comp["suppressed"] = false;
    comps.push_back(std::move(comp));
    id_map["$" + std::to_string(idx)] = kid;
}

void op_asm_edit_component(json& doc, const json& op, std::size_t idx) {
    const std::string kid = op.value("id", std::string());
    if (doc.contains("components") && doc["components"].is_array())
        for (json& c : doc["components"])
            if (c.value("id", std::string()) == kid) {
                for (const char* k : {"transform", "name", "suppressed", "fixed"})
                    if (op.contains(k) && !op[k].is_null()) c[k] = op[k];
                return;
            }
    throw OpError("invalid_op", "asm_edit_component: '" + kid + "' not found", {{"index", idx}});
}

void op_asm_delete_component(json& doc, const json& op, std::size_t idx) {
    const std::string kid = op.value("id", std::string());
    json comps = (doc.contains("components") && doc["components"].is_array()) ? doc["components"]
                                                                             : json::array();
    bool found = false;
    for (const json& c : comps)
        if (c.value("id", std::string()) == kid) { found = true; break; }
    if (!found)
        throw OpError("invalid_op", "asm_delete_component: '" + kid + "' not found", {{"index", idx}});
    json mates = (doc.contains("mates") && doc["mates"].is_array()) ? doc["mates"] : json::array();
    std::vector<std::string> referencing;
    for (const json& m : mates) {
        bool refs_kid = false;
        if (m.contains("refs") && m["refs"].is_array())
            for (const json& r : m["refs"])
                if (r.is_object() && r.value("component", std::string()) == kid) refs_kid = true;
        if (refs_kid) referencing.push_back(m.value("id", std::string()));
    }
    if (!referencing.empty() && !op.value("cascade", false))
        throw OpError("dependency_exists",
                      "component '" + kid + "' used by mates", {{"dependents", referencing}});
    json kept_comps = json::array();
    for (const json& c : comps)
        if (c.value("id", std::string()) != kid) kept_comps.push_back(c);
    doc["components"] = kept_comps;
    json kept_mates = json::array();
    for (const json& m : mates) {
        bool refs_kid = false;
        if (m.contains("refs") && m["refs"].is_array())
            for (const json& r : m["refs"])
                if (r.is_object() && r.value("component", std::string()) == kid) refs_kid = true;
        if (!refs_kid) kept_mates.push_back(m);
    }
    doc["mates"] = kept_mates;
}

void op_asm_add_mate(json& doc, const json& op, std::size_t idx, IdGen& gen, json& id_map) {
    if (!op.contains("mate") || !op["mate"].is_object())
        throw OpError("invalid_op", "asm_add_mate requires a 'mate' object", {{"index", idx}});
    json m = op["mate"];  // deep copy
    m["id"] = gen.next("m");
    if (!m.contains("suppressed")) m["suppressed"] = false;
    if (!m.contains("align")) m["align"] = "aligned";
    if (!doc.contains("mates") || !doc["mates"].is_array()) doc["mates"] = json::array();
    doc["mates"].push_back(m);
    id_map["$" + std::to_string(idx)] = m["id"];
}

void op_asm_edit_mate(json& doc, const json& op, std::size_t idx) {
    const std::string mid = op.value("id", std::string());
    if (doc.contains("mates") && doc["mates"].is_array())
        for (json& m : doc["mates"])
            if (m.value("id", std::string()) == mid) {
                for (const char* k : {"value", "align", "suppressed"})
                    if (op.contains(k) && !op[k].is_null()) m[k] = op[k];
                return;
            }
    throw OpError("invalid_op", "asm_edit_mate: '" + mid + "' not found", {{"index", idx}});
}

void op_asm_delete_mate(json& doc, const json& op, std::size_t idx) {
    const std::string mid = op.value("id", std::string());
    json mates = (doc.contains("mates") && doc["mates"].is_array()) ? doc["mates"] : json::array();
    bool found = false;
    for (const json& m : mates)
        if (m.value("id", std::string()) == mid) { found = true; break; }
    if (!found)
        throw OpError("invalid_op", "asm_delete_mate: '" + mid + "' not found", {{"index", idx}});
    json kept = json::array();
    for (const json& m : mates)
        if (m.value("id", std::string()) != mid) kept.push_back(m);
    doc["mates"] = kept;
}

void op_asm_set_explode(json& doc, const json& op, std::size_t idx) {
    if (!op.contains("steps") || !op["steps"].is_array())
        throw OpError("invalid_op", "asm_set_explode requires a 'steps' list", {{"index", idx}});
    doc["explode"] = op["steps"];
}

void apply_one(json& doc, const json& op, std::size_t idx, IdGen& gen, json& id_map) {
    if (!op.is_object() || !op.contains("op") || !op["op"].is_string())
        throw OpError("unsupported", "op[" + std::to_string(idx) + "]: missing 'op'",
                      {{"index", idx}});
    const std::string k = op["op"].get<std::string>();

    if (k == "add_feature")              op_add_feature(doc, op, idx, gen, id_map);
    else if (k == "edit_feature")        op_edit_feature(doc, op, id_map);
    else if (k == "delete_feature")      op_delete_feature(doc, op);
    else if (k == "reorder_feature")     op_reorder_feature(doc, op);
    else if (k == "set_suppressed") {
        json* f = find_feature(doc, op.value("id", std::string()));
        if (!f) throw OpError("invalid_op", "set_suppressed: no feature");
        (*f)["suppressed"] = op.value("value", false);
    } else if (k == "rename") {
        json* f = find_feature(doc, op.value("id", std::string()));
        if (!f) throw OpError("invalid_op", "rename: no feature");
        if (op.contains("name") && op["name"].is_string()) (*f)["name"] = op["name"];
    } else if (k == "set_units") {
        if (op.contains("system")) doc["units"]["system"] = op["system"];
    } else if (k == "set_material") {
        doc["material"] = op.contains("key") && !op["key"].is_null()
                              ? json{{"key", op["key"]}} : json(nullptr);
    } else if (k == "set_rollback") {
        if (op.contains("after") && !op["after"].is_null()) doc["rollback_after"] = op["after"];
        else doc.erase("rollback_after");
    } else if (k == "eq_set") {
        json& globals = doc["equations"]["globals"];
        const std::string name = op.value("name", std::string());
        bool replaced = false;
        for (json& g : globals)
            if (g.value("name", std::string()) == name) { g["expr"] = op.value("expr", std::string()); replaced = true; break; }
        if (!replaced) globals.push_back({{"name", name}, {"expr", op.value("expr", std::string())}});
    } else if (k == "eq_delete") {
        json& globals = doc["equations"]["globals"];
        json kept = json::array();
        for (const json& g : globals)
            if (g.value("name", std::string()) != op.value("name", std::string())) kept.push_back(g);
        globals = kept;
    } else if (k == "sketch_add_entity")        op_sketch_add_entity(doc, op, idx, gen, id_map);
    else if (k == "sketch_add_constraint")      op_sketch_add_constraint(doc, op, idx, gen, id_map);
    else if (k == "sketch_edit_entity")         op_sketch_edit_entity(doc, op, id_map);
    else if (k == "sketch_delete_entity")       op_sketch_delete_entity(doc, op, id_map);
    else if (k == "sketch_delete_constraint")   op_sketch_delete_constraint(doc, op, id_map);
    else if (k == "sketch_set_dim")             op_sketch_set_dim(doc, op, id_map);
    else if (k == "sketch_toggle_construction") op_sketch_toggle_construction(doc, op, id_map);
    else if (k == "sketch_trim")                op_sketch_trim(doc, op, idx, gen, id_map);
    else if (k == "sketch_extend")              op_sketch_extend(doc, op, idx, gen, id_map);
    else if (k == "sketch_offset")              op_sketch_offset(doc, op, idx, gen, id_map);
    else if (k == "sketch_fillet")              op_sketch_fillet(doc, op, idx, gen, id_map);
    else if (k == "sketch_chamfer")             op_sketch_chamfer(doc, op, idx, gen, id_map);
    else if (k == "sketch_mirror_entities")     op_sketch_mirror_entities(doc, op, idx, gen, id_map);
    else if (k == "sketch_pattern_entities")    op_sketch_pattern_entities(doc, op, idx, gen, id_map);
    else if (k == "sketch_move")                op_sketch_move(doc, op, idx, gen, id_map);
    else if (k == "asm_insert_component")       op_asm_insert_component(doc, op, idx, gen, id_map);
    else if (k == "asm_edit_component")         op_asm_edit_component(doc, op, idx);
    else if (k == "asm_delete_component")       op_asm_delete_component(doc, op, idx);
    else if (k == "asm_add_mate")               op_asm_add_mate(doc, op, idx, gen, id_map);
    else if (k == "asm_edit_mate")              op_asm_edit_mate(doc, op, idx);
    else if (k == "asm_delete_mate")            op_asm_delete_mate(doc, op, idx);
    else if (k == "asm_set_explode")            op_asm_set_explode(doc, op, idx);
    else
        throw OpError("unsupported", "op[" + std::to_string(idx) + "]: unknown op '" + k + "'",
                      {{"index", idx}, {"reason", "not ported"}});
}

// ----------------------------------------------------------------- rebuild + response
// Evaluate the document's global equations into an env map (name -> value), in
// listed order (a later global may reference an earlier one). Bad exprs are skipped.
json env_from_doc(const json& doc) {
    json env = json::object();
    if (doc.contains("equations") && doc["equations"].is_object() &&
        doc["equations"].contains("globals") && doc["equations"]["globals"].is_array()) {
        std::map<std::string, double> acc;
        for (const json& g : doc["equations"]["globals"]) {
            const std::string name = g.value("name", std::string());
            const std::string expr = g.value("expr", std::string());
            if (name.empty()) continue;
            try {
                double v = aicad::util::evaluate(expr, acc);
                acc[name] = v;
                env[name] = v;
            } catch (...) { /* skip a bad global */ }
        }
    }
    return env;
}

std::filesystem::path aicm_file(const std::string& doc_id, long revision) {
    std::filesystem::path dir = std::filesystem::temp_directory_path() / "aicad_cpp_docs";
    std::error_code ec;
    std::filesystem::create_directories(dir, ec);
    return dir / (doc_id + "_r" + std::to_string(revision) + ".aicm");
}

json ops_response(Session& s, const CompileResult& r, const json& id_map, bool with_tess) {
    json fs = json::array();
    for (const FeatureStatus& st : r.statuses) {
        fs.push_back({{"id", st.id},
                      {"ok", st.ok},
                      {"ms", 0.0},
                      {"error", st.ok ? json(nullptr) : json(st.error)},
                      {"dangling_refs", json::array()},
                      {"rolled_back", false}});
    }
    json resp;
    resp["revision"] = s.revision;
    resp["id_map"] = id_map;
    resp["tree"] = s.doc;
    resp["owner_map"] = r.owner_map.is_null() ? json::object() : r.owner_map;
    if (with_tess) {
        json active_sketch_status = nullptr;
        if (s.active_sketch && !s.active_sketch->last_overall_status.empty())
            active_sketch_status = s.active_sketch->last_overall_status;
        resp["rebuild"] = {{"feature_status", fs},
                           {"doc_status", {{"active_sketch", active_sketch_status},
                                           {"mates", nullptr},
                                           {"active_configuration", r.active_configuration}}}};
        std::string aicm_path;
        if (!r.aicm.empty()) {
            std::filesystem::path target = aicm_file(s.doc_id, s.revision);
            std::ofstream f(target, std::ios::binary | std::ios::trunc);
            if (f) {
                f.write(reinterpret_cast<const char*>(r.aicm.data()),
                        static_cast<std::streamsize>(r.aicm.size()));
                f.close();
                aicm_path = std::filesystem::absolute(target).string();
            }
        }
        json bodies = json::array();
        for (int i = 0; i < r.body_count; ++i) {
            const std::string key = "b" + std::to_string(i + 1);
            bodies.push_back({{"body_id", i + 1}, {"name", key}, {"color", {0.71, 0.76, 0.82}},
                              {"owner_key", key}, {"metallic", 0.0}, {"roughness", 0.5},
                              {"kind", "solid"}});
        }
        resp["tess"] = {{"aicm_path", aicm_path}, {"bodies", bodies}};
        resp["volume"] = r.volume;  // C++ convenience (geometric parity signal)
    }
    return resp;
}

void push_undo(Session& s) {
    s.undo_stack.push_back(s.doc.dump());
    if (s.undo_stack.size() > kUndoLimit)
        s.undo_stack.erase(s.undo_stack.begin());
    s.redo_stack.clear();
}

std::string config_active_name(const json& doc);  // defined below (configurations section)

// ----------------------------------------------------------------- assembly rebuild
// Solve the assembly's mates, write the solved transforms back into the doc (so a save
// captures the mated pose), and cache the AssemblyResult on the session (port of
// session.py::_rebuild_assembly). base_dir resolves relative component source paths.
void rebuild_assembly(Session& s) {
    std::string base_dir;
    if (!s.path.empty()) base_dir = std::filesystem::path(s.path).parent_path().string();
    std::optional<aicad::mate::WarmMap> warm;
    if (!s.asm_warm.empty()) warm = s.asm_warm;
    AssemblyResult ares = compile_assembly(s.doc, base_dir, warm);
    // Persist solved transforms back into the document (matched by component id).
    if (s.doc.contains("components") && s.doc["components"].is_array())
        for (json& comp : s.doc["components"]) {
            const std::string cid = comp.value("id", std::string());
            auto it = ares.transforms.find(cid);
            if (it != ares.transforms.end()) comp["transform"] = it->second;
        }
    s.asm_warm = ares.warm;
    s.asm_result = std::make_shared<AssemblyResult>(std::move(ares));
}

// Tessellate each component (world-placed + optional explode offset) as one AICM body
// and write the AICM (port of session.py::_write_aicm_assembly). Returns (path, bodies-meta).
std::pair<std::string, json> write_aicm_assembly(Session& s) {
    json bodies_meta = json::array();
    std::vector<aicm::AicmBody> tesses;
    std::map<std::string, Eigen::Vector3d> offsets;
    if (s.explode_active) offsets = explode_offsets(s.doc, s.explode_factor);
    const AssemblyResult* ares = s.asm_result.get();
    if (ares) {
        for (const AsmBodyOut& body : ares->bodies) {
            std::optional<Eigen::Vector3d> off;
            auto oit = offsets.find(body.component_id);
            if (oit != offsets.end()) off = oit->second;
            TopoDS_Shape world = placed_world_shape(body, off);
            float color[3] = {body.color[0], body.color[1], body.color[2]};
            aicm::AicmBody bt = ::geom::tessellate_body(world, static_cast<uint32_t>(body.body_id),
                                                        body.name, color);
            tesses.push_back(std::move(bt));
            bodies_meta.push_back({{"body_id", body.body_id}, {"name", body.name},
                                   {"color", {body.color[0], body.color[1], body.color[2]}},
                                   {"component", body.component_id},
                                   {"owner_key", body.component_id},
                                   {"metallic", 0.0}, {"roughness", 0.5}});
        }
    }
    std::filesystem::path target = aicm_file(s.doc_id, s.revision);
    aicm::write_aicm_file(target.string(), tesses);
    return {std::filesystem::absolute(target).string(), bodies_meta};
}

// The assembly ops-response (port of the assembly branch of session.py::_ops_response).
json ops_response_assembly(Session& s, const json& id_map) {
    auto [aicm_path, bodies_meta] = write_aicm_assembly(s);
    const AssemblyResult* ares = s.asm_result.get();

    json feature_status = json::array();
    json components = json::array();
    json mates = json::array();
    int dof = 0;
    bool over_defined = false;
    std::string dof_summary;
    if (ares) {
        for (const AsmComponentStatus& cs : ares->component_status) {
            json row = {{"id", cs.id}, {"ok", cs.ok}, {"ms", cs.ms},
                        {"error", cs.error ? json(*cs.error) : json(nullptr)},
                        {"dangling_refs", cs.dangling_refs}};
            feature_status.push_back(row);
            components.push_back(row);
        }
        for (const AsmMateStatus& ms : ares->mate_status)
            mates.push_back({{"id", ms.id}, {"type", ms.type}, {"status", ms.status},
                             {"dangling_refs", ms.dangling_refs}});
        dof = ares->dof;
        over_defined = ares->over_defined;
        dof_summary = ares->dof_summary;
    }

    json active_sketch_status = nullptr;  // assemblies have no active sketch
    json rebuild = {
        {"feature_status", feature_status},
        {"doc_status", {{"active_sketch", active_sketch_status},
                        {"mates", over_defined ? "over" : "ok"},
                        {"active_configuration", config_active_name(s.doc)}}},
        {"components", components},
        {"mates", mates},
        {"dof", dof},
        {"over_defined", over_defined},
        {"dof_summary", dof_summary},
    };
    return json{
        {"revision", s.revision},
        {"id_map", id_map},
        {"tree", s.doc},
        {"rebuild", rebuild},
        {"tess", {{"aicm_path", aicm_path}, {"bodies", bodies_meta}}},
        {"owner_map", json::object()},
    };
}

// ----------------------------------------------------------------- configurations
// The active config name with the implicit-Default + first-config fallbacks
// (mirrors session.py::_config_active_name -> compiler.resolve_active_config).
std::string config_active_name(const json& doc) {
    if (!doc.contains("configurations") || !doc["configurations"].is_array() ||
        doc["configurations"].empty())
        return "Default";
    const json& configs = doc["configurations"];
    std::string active = doc.value("active_configuration", std::string());
    if (!active.empty())
        for (const json& c : configs)
            if (c.is_object() && c.value("name", std::string()) == active) return active;
    const json& first = configs[0];
    std::string name = first.is_object() ? first.value("name", std::string()) : std::string();
    return name.empty() ? std::string("Default") : name;
}

// The doc's configurations array, materializing the implicit Default on first edit
// (session.py::_ensure_configs). Returns a reference into doc.
json& ensure_configs(json& doc) {
    if (!doc.contains("configurations") || !doc["configurations"].is_array())
        doc["configurations"] = json::array();
    json& configs = doc["configurations"];
    if (configs.empty()) {
        configs.push_back({{"name", "Default"}, {"param_overrides", json::object()},
                           {"suppressions", json::object()}});
        if (!doc.contains("active_configuration")) doc["active_configuration"] = "Default";
    }
    return configs;
}

// The config named `name`, or throw invalid_op (session.py::_find_config).
json& find_config(json& doc, const std::string& name) {
    if (doc.contains("configurations") && doc["configurations"].is_array())
        for (json& c : doc["configurations"])
            if (c.is_object() && c.value("name", std::string()) == name) return c;
    throw OpError("invalid_op", "no configuration named '" + name + "'",
                  {{"reason", "unknown configuration"}, {"name", name}});
}

// Validate the edited working doc; throw invalid_op listing the errors if broken
// (session.py::_validate_or_rollback — here the caller works on a COPY, so a throw
// leaves the live session untouched without an explicit rollback).
void validate_config_edit(const json& working) {
    std::vector<std::string> errs = validate_document(working);
    if (errs.empty()) return;
    std::string joined;
    for (std::size_t i = 0; i < errs.size() && i < 5; ++i) {
        if (i) joined += "; ";
        joined += errs[i];
    }
    throw OpError("invalid_op", "configuration edit produced an invalid document: " + joined,
                  {{"reason", "invalid configuration"}, {"errors", errs}});
}

// Commit a validated config edit: push one undo step, swap in the working doc, bump
// the revision, recompile, and return the ops-response (session.py::_config_commit;
// the C++ session always recompiles, so rebuild_needed is moot — same geometry).
json config_commit(Session& s, json&& working) {
    push_undo(s);
    s.doc = std::move(working);
    s.revision += 1;
    s.dirty = true;
    if (s.active_sketch) s.active_sketch->solve_state.reset();  // session.py::_config_commit
    CompileResult r = compile_document(s.doc, env_from_doc(s.doc));
    return ops_response(s, r, json::object(), /*with_tess=*/true);
}

// Apply an op batch to a session that the caller already holds g_mtx for (the shared
// transactional core of doc_apply_ops; doc_import_file reuses it).
json apply_ops_locked(Session& s, long base_revision, const json& ops, bool coalesce) {
    if (base_revision >= 0 && base_revision != s.revision)
        throw OpError("stale_revision",
                      "base_revision " + std::to_string(base_revision) + " != current " +
                          std::to_string(s.revision),
                      {{"current_revision", s.revision}});
    if (!ops.is_array())
        throw OpError("invalid_op", "ops must be a list", {{"index", -1}, {"reason", "ops not a list"}});
    json working = s.doc;
    json id_map = json::object();
    IdGen gen(working);
    for (std::size_t i = 0; i < ops.size(); ++i)
        apply_one(working, ops[i], i, gen, id_map);

    std::vector<std::string> errs = validate_document(working);
    if (!errs.empty()) {
        std::string joined;
        for (std::size_t i = 0; i < errs.size() && i < 5; ++i) {
            if (i) joined += "; ";
            joined += errs[i];
        }
        throw OpError("invalid_op", "resulting document failed validation: " + joined,
                      {{"index", -1}, {"reason", errs.front()}, {"all_errors", errs}});
    }

    if (!coalesce) push_undo(s);
    s.doc = std::move(working);
    s.revision += 1;
    s.dirty = true;
    if (s.is_assembly()) {
        rebuild_assembly(s);
        return ops_response_assembly(s, id_map);
    }
    CompileResult r = compile_document(s.doc, env_from_doc(s.doc));
    return ops_response(s, r, id_map, /*with_tess=*/true);
}

// Create + register a blank part session, returning a reference (caller holds g_mtx).
Session& new_part_session_locked(const std::string& name) {
    Session s;
    s.doc_id = new_doc_id();
    s.doc = blank_part(s.doc_id, name);
    s.revision = 0;
    return g_sessions[s.doc_id] = std::move(s);
}

// ============================================================ C18 sketch helpers
// C++ port of the active-sketch interaction core in engine/doc/session.py.

// session.py::_find_sketch_feature — locate a "sketch" feature by id (throws on a
// missing id / non-sketch). Returns a reference into s.doc["features"].
json& find_sketch_feature(Session& s, const std::string& fid) {
    if (s.doc.contains("features") && s.doc["features"].is_array())
        for (json& f : s.doc["features"])
            if (f.is_object() && f.value("id", std::string()) == fid) {
                if (f.value("type", std::string()) != "sketch")
                    throw OpError("invalid_op", "feature '" + fid + "' is not a sketch",
                                  {{"reason", "not a sketch"}});
                return f;
            }
    throw OpError("invalid_op", "no sketch feature '" + fid + "'", {{"reason", "unknown sketch"}});
}

// The sketch feature's params dict ({entities, constraints}). Host canonical shape
// stores them under "params"; a flat fallback reads them off the feature directly.
json sketch_params(const json& feat) {
    if (feat.contains("params") && feat["params"].is_object()) return feat["params"];
    json p = json::object();
    p["entities"] = feat.contains("entities") ? feat["entities"] : json::array();
    p["constraints"] = feat.contains("constraints") ? feat["constraints"] : json::array();
    return p;
}

// session.py::_resolve_sketch_plane — resolve refs.plane to a SketchPlane. Reserved
// datums ("plane.top/front/right" or bare "top/front/right") map to the factory
// frames; a plane-FEATURE id (offset/midplane/etc.) is resolved by recompiling the
// doc and reading that feature's plane — but compile_document does not surface the
// per-feature plane, so for now non-reserved refs fall back to the reserved name
// match (the interactive fixtures sketch on reserved planes). Throws on no plane ref.
aicad::geom::SketchPlane resolve_sketch_plane(Session& s, const json& feat) {
    json plane_ref;
    if (feat.contains("refs") && feat["refs"].is_object() && feat["refs"].contains("plane"))
        plane_ref = feat["refs"]["plane"];
    else if (feat.contains("plane"))
        plane_ref = feat["plane"];
    if (plane_ref.is_null())
        throw OpError("invalid_op", "sketch has no refs.plane", {{"reason", "no plane"}});
    std::string name;
    if (plane_ref.is_string()) name = plane_ref.get<std::string>();
    else if (plane_ref.is_object() && plane_ref.value("kind", std::string()) == "datum" &&
             plane_ref.contains("feature") && plane_ref["feature"].is_string())
        name = plane_ref["feature"].get<std::string>();
    std::string bare = name;
    if (bare.rfind("plane.", 0) == 0) bare = bare.substr(6);
    if (bare == "front") return aicad::geom::SketchPlane::front();
    if (bare == "right") return aicad::geom::SketchPlane::right();
    return aicad::geom::SketchPlane::top();
}

// session.py::_plane_frame — 16-float row-major matrix mapping sketch (u,v,0,1) ->
// world. Columns are x_dir(uAxis), y_dir(vAxis), z_dir(normal), origin; rows 0..2
// are the affine rows, row 3 is [0,0,0,1].
std::vector<double> plane_frame(const aicad::geom::SketchPlane& p) {
    std::vector<double> out;
    out.reserve(16);
    for (int i = 0; i < 3; ++i) {
        out.push_back(p.uAxis[i]);
        out.push_back(p.vAxis[i]);
        out.push_back(p.normal[i]);
        out.push_back(p.origin[i]);
    }
    out.insert(out.end(), {0.0, 0.0, 0.0, 1.0});
    return out;
}

// session.py::_DIM_TYPES — a redundant DRIVING dim over-defines; geometric relations
// don't (see _redundant_overdefines for the SW-parity rationale).
bool is_dim_type(const std::string& t) {
    return t == "distance" || t == "angle" || t == "radius" || t == "diameter" || t == "length";
}

// session.py::_redundant_overdefines — true iff the redundant set contains a redundant
// driving dimension (a redundant geometric relation deliberately does NOT over-define).
bool redundant_overdefines(const std::vector<std::string>& redundant,
                           const json& constraints_by_id) {
    if (redundant.empty() || !constraints_by_id.is_object()) return false;
    for (const std::string& cid : redundant) {
        if (!constraints_by_id.contains(cid)) continue;
        const json& c = constraints_by_id[cid];
        if (c.is_object() && is_dim_type(c.value("type", std::string())) &&
            !c.value("driven", false))
            return true;
    }
    return false;
}

// session.py::_overall_status — under|full|over classification.
std::string overall_status(const json& statuses, int dof,
                           const std::vector<std::string>& conflicting,
                           const std::vector<std::string>& redundant,
                           const json& constraints_by_id) {
    bool any_over = false;
    if (statuses.is_object())
        for (auto it = statuses.begin(); it != statuses.end(); ++it)
            if (it.value().is_string() && it.value().get<std::string>() == "over") any_over = true;
    if (!conflicting.empty() || redundant_overdefines(redundant, constraints_by_id) || any_over)
        return "over";
    if (dof <= 0) return "full";
    return "under";
}

// session.py::_doc_entity_coords — freeze map: entity id -> stored coords (same key
// shape solve_sketch returns), stripping id/construction bookkeeping.
json doc_entity_coords(const json& params) {
    json out = json::object();
    if (params.contains("entities") && params["entities"].is_array())
        for (const json& e : params["entities"]) {
            if (!e.is_object() || !e.contains("id")) continue;
            json rec = json::object();
            for (auto it = e.begin(); it != e.end(); ++it)
                if (it.key() != "id" && it.key() != "construction") rec[it.key()] = it.value();
            out[e["id"].get<std::string>()] = rec;
        }
    return out;
}

// session.py::_sketch_constraints — pack every constraint for the host's dimension
// annotations: {id, type, refs, value, expr, driven, placement, measured, [axis], [cond]}.
json sketch_constraints(const json& feat, const json& entity_coords) {
    json out = json::array();
    json params = sketch_params(feat);
    if (!params.contains("constraints") || !params["constraints"].is_array()) return out;
    for (const json& c : params["constraints"]) {
        if (!c.is_object()) continue;
        const json* val_obj = (c.contains("value") && c["value"].is_object()) ? &c["value"] : nullptr;
        json rec;
        rec["id"] = c.contains("id") ? c["id"] : json(nullptr);
        rec["type"] = c.contains("type") ? c["type"] : json(nullptr);
        rec["refs"] = (c.contains("refs") && c["refs"].is_array()) ? c["refs"] : json::array();
        rec["value"] = val_obj && val_obj->contains("value") ? (*val_obj)["value"] : json(nullptr);
        rec["expr"] = val_obj && val_obj->contains("expr") ? (*val_obj)["expr"] : json(nullptr);
        rec["driven"] = c.value("driven", false);
        rec["placement"] = c.contains("placement") ? c["placement"] : json(nullptr);
        rec["measured"] = nullptr;
        if (c.contains("axis") && c["axis"].is_string() && !c["axis"].get<std::string>().empty())
            rec["axis"] = c["axis"];
        if (c.contains("cond") && c["cond"].is_array()) rec["cond"] = c["cond"];
        auto m = aicad::sketch::measure_constraint(c, entity_coords);
        if (m) rec["measured"] = *m;
        out.push_back(std::move(rec));
    }
    return out;
}

// Packed result of session.py::_solve_active (minus plane_frame). `warm` is the
// active-sketch warm state (nullopt for a cold solve). On a REJECTED solve
// (conflicting non-empty OR not converged) geometry is frozen at the doc's current
// coords and warm state is cleared.
struct SolveActive {
    std::optional<aicad::sketch::SolveState> state;  // nullopt when rejected
    json entities;          // list of coord dicts carrying id + construction
    json entity_map;        // id -> coord dict
    json statuses;          // id -> "under"|"full"|"over"
    int dof = 0;
    std::vector<std::string> conflicting;
    std::vector<std::string> redundant;
    json constraints_by_id;
    std::vector<std::string> skipped_constraints;
    bool rejected = false;
};

SolveActive solve_active(Session& s, const json& feat,
                         const std::optional<aicad::sketch::SolveState>& warm) {
    json env = env_from_doc(s.doc);
    json params = sketch_params(feat);
    json sk;
    sk["entities"] = params.contains("entities") ? params["entities"] : json::array();
    sk["constraints"] = params.contains("constraints") ? params["constraints"] : json::array();

    aicad::sketch::SketchSolution sol = aicad::sketch::solve_sketch(sk, env, warm);

    SolveActive out;
    out.rejected = !sol.conflicting.empty() || !sol.converged;
    out.conflicting = sol.conflicting;
    out.redundant = sol.redundant;
    out.dof = sol.dof;
    out.statuses = json::object();
    for (const auto& kv : sol.statuses) out.statuses[kv.first] = kv.second;

    json coord_map = out.rejected ? doc_entity_coords(params) : sol.entities;
    out.entity_map = coord_map;

    // construction flag lookup
    auto construction_of = [&](const std::string& eid) -> bool {
        if (params.contains("entities") && params["entities"].is_array())
            for (const json& e : params["entities"])
                if (e.is_object() && e.value("id", std::string()) == eid)
                    return e.value("construction", false);
        return false;
    };
    json entities = json::array();
    for (auto it = coord_map.begin(); it != coord_map.end(); ++it) {
        json rec = it.value();
        rec["id"] = it.key();
        rec["construction"] = construction_of(it.key());
        entities.push_back(std::move(rec));
    }
    out.entities = std::move(entities);

    out.constraints_by_id = json::object();
    if (params.contains("constraints") && params["constraints"].is_array())
        for (const json& c : params["constraints"])
            if (c.is_object() && c.contains("id"))
                out.constraints_by_id[c["id"].get<std::string>()] = c;

    out.skipped_constraints.clear();  // C++ solver has no skipped_constraints field

    out.state = out.rejected ? std::optional<aicad::sketch::SolveState>{} : sol.state;
    return out;
}

// std::vector<std::string> -> json array helper.
json strvec_json(const std::vector<std::string>& v) {
    json a = json::array();
    for (const std::string& s : v) a.push_back(s);
    return a;
}

// Build the sketch_enter/sketch_solve result dict from a packed SolveActive +
// plane_frame + overall status (session.py sketch_enter/sketch_solve shape).
json sketch_result(const json& feat, const SolveActive& packed,
                   const std::vector<double>& frame, const std::string& status) {
    json r;
    r["entities"] = packed.entities;
    r["statuses"] = packed.statuses;
    r["dof"] = packed.dof;
    r["plane_frame"] = frame;
    r["status"] = status;
    r["conflicting"] = strvec_json(packed.conflicting);
    r["redundant"] = strvec_json(packed.redundant);
    r["skipped_constraints"] = strvec_json(packed.skipped_constraints);
    r["solve_rejected"] = packed.rejected;
    r["constraints"] = sketch_constraints(feat, packed.entity_map);
    return r;
}

}  // namespace

// ----------------------------------------------------------------- public API
json doc_new(const std::string& kind, const std::string& name) {
    const std::string k = kind.empty() ? std::string("part") : kind;
    if (k != "part" && k != "assembly")
        throw OpError("invalid_op", "doc kind must be part|assembly, got '" + k + "'",
                      {{"kind", kind}, {"reason", "bad kind"}});
    std::lock_guard<std::mutex> lk(g_mtx);
    if (k == "assembly") {
        Session s;
        s.doc_id = new_doc_id();
        s.doc = blank_assembly(s.doc_id, name);
        s.revision = 0;
        Session& stored = g_sessions[s.doc_id] = std::move(s);
        rebuild_assembly(stored);
        json resp = ops_response_assembly(stored, json::object());
        resp["doc_id"] = stored.doc_id;
        return resp;
    }
    Session& stored = new_part_session_locked(name);
    CompileResult r = compile_document(stored.doc, env_from_doc(stored.doc));
    json resp = ops_response(stored, r, json::object(), /*with_tess=*/true);
    resp["doc_id"] = stored.doc_id;
    return resp;
}

json doc_apply_ops(const std::string& doc_id, long base_revision, const json& ops, bool coalesce) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    return apply_ops_locked(s, base_revision, ops, coalesce);
}

json doc_dry_run_ops(const std::string& doc_id, const json& ops) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& live = require_session(doc_id);
    // Clone the live doc into a scratch session; apply_ops_locked there so the live
    // session (and its revision/undo stacks) is never touched. base_revision = -1
    // skips the optimistic-concurrency guard (matches ops_emit.dry_run, which builds
    // the would-be document directly off the doc dict with no revision check).
    Session scratch;
    scratch.doc_id = live.doc_id;  // any value; never registered/persisted.
    scratch.doc = live.doc;        // deep copy of the feature tree json.
    scratch.revision = live.revision;
    try {
        apply_ops_locked(scratch, -1, ops, /*coalesce=*/true);
        return json{{"ok", true}};
    } catch (const OpError& e) {
        // Compose the error string the SAME way ops_emit.dry_run does:
        //   reason = env["message"]; if detail.all_errors: reason + " | " + "; ".join(all_errors[:5])
        std::string reason = e.what();
        if (e.detail.is_object() && e.detail.contains("all_errors") &&
            e.detail["all_errors"].is_array() && !e.detail["all_errors"].empty()) {
            std::string joined;
            const json& all = e.detail["all_errors"];
            for (std::size_t i = 0; i < all.size() && i < 5; ++i) {
                if (i) joined += "; ";
                joined += all[i].get<std::string>();
            }
            reason = reason + " | " + joined;
        }
        return json{{"ok", false}, {"error", reason}};
    } catch (const std::exception& e) {
        // Python's generic branch: f"{type(e).__name__}: {e}". The C++ exception type
        // name is mangled/platform-specific, so we emit the message (the OpError path
        // above is the one the agent gate exercises; this is a defensive fallback).
        return json{{"ok", false}, {"error", std::string(e.what())}};
    }
}

json doc_undo(const std::string& doc_id) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    if (s.undo_stack.empty())
        throw OpError("invalid_op", "nothing to undo", {{"reason", "empty undo stack"}});
    s.redo_stack.push_back(s.doc.dump());
    s.doc = json::parse(s.undo_stack.back());
    s.undo_stack.pop_back();
    s.revision += 1;  // undo bumps the revision forward (Python contract)
    s.dirty = true;
    // C50: drop warm sketch state so the next solve COLD-solves the restored seeds
    // (session.py: active_sketch.solve_state = None on undo/redo).
    if (s.active_sketch) s.active_sketch->solve_state.reset();
    if (s.is_assembly()) {
        s.asm_warm.clear();  // re-seed mate solve from the restored doc transforms
        rebuild_assembly(s);
        return ops_response_assembly(s, json::object());
    }
    CompileResult r = compile_document(s.doc, env_from_doc(s.doc));
    return ops_response(s, r, json::object(), /*with_tess=*/true);
}

json doc_redo(const std::string& doc_id) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    if (s.redo_stack.empty())
        throw OpError("invalid_op", "nothing to redo", {{"reason", "empty redo stack"}});
    s.undo_stack.push_back(s.doc.dump());
    if (s.undo_stack.size() > kUndoLimit) s.undo_stack.erase(s.undo_stack.begin());
    s.doc = json::parse(s.redo_stack.back());
    s.redo_stack.pop_back();
    s.revision += 1;
    s.dirty = true;
    if (s.active_sketch) s.active_sketch->solve_state.reset();  // C50 (see doc_undo)
    if (s.is_assembly()) {
        s.asm_warm.clear();  // re-seed mate solve from the restored doc transforms
        rebuild_assembly(s);
        return ops_response_assembly(s, json::object());
    }
    CompileResult r = compile_document(s.doc, env_from_doc(s.doc));
    return ops_response(s, r, json::object(), /*with_tess=*/true);
}

json doc_tree(const std::string& doc_id) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    if (s.is_assembly()) {
        json resp;
        resp["revision"] = s.revision;
        resp["tree"] = s.doc;
        resp["owner_map"] = json::object();
        return resp;
    }
    // No revision bump / no tess, but DO carry the owner_map (host picking) — recompute.
    CompileResult r = compile_document(s.doc, env_from_doc(s.doc));
    json resp;
    resp["revision"] = s.revision;
    resp["tree"] = s.doc;
    resp["owner_map"] = r.owner_map.is_null() ? json::object() : r.owner_map;
    return resp;
}

json doc_rebuild(const std::string& doc_id) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    if (s.is_assembly()) {
        rebuild_assembly(s);
        return ops_response_assembly(s, json::object());
    }
    CompileResult r = compile_document(s.doc, env_from_doc(s.doc));
    return ops_response(s, r, json::object(), /*with_tess=*/true);
}

// ============================================================ C20 assembly RPCs
// doc_solve_mates: re-solve the assembly mates (optionally with a drag soft-target),
// persist the solved transforms, bump the revision (one undoable step), and return the
// ops-response (port of session.py::solve_mates). doc_set_explode_state toggles the
// explode view and re-tessellates without a revision bump.

json doc_solve_mates(const std::string& doc_id, const json& drag) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    if (!s.is_assembly())
        throw OpError("invalid_op", "doc_solve_mates: document is not an assembly",
                      {{"reason", "not an assembly"}});
    std::string base_dir;
    if (!s.path.empty()) base_dir = std::filesystem::path(s.path).parent_path().string();
    push_undo(s);

    std::optional<aicad::mate::WarmMap> warm;
    if (!s.asm_warm.empty()) warm = s.asm_warm;
    std::optional<aicad::mate::Drag> drag_opt;
    if (drag.is_object() && drag.contains("component") && drag["component"].is_string()) {
        // session.py::_compile_assembly_drag: first a plain compile to warm + load, then
        // a second solve with the drag target seeded from that warm result. compile_assembly
        // accepts a drag arg directly here, threading it to the solver in one pass.
        aicad::mate::Drag d;
        d.component = drag["component"].get<std::string>();
        if (drag.contains("target") && drag["target"].is_array() && drag["target"].size() == 3)
            d.target = Eigen::Vector3d(drag["target"][0].get<double>(),
                                       drag["target"][1].get<double>(),
                                       drag["target"][2].get<double>());
        drag_opt = d;
    }
    AssemblyResult ares = compile_assembly(s.doc, base_dir, warm, drag_opt);
    if (s.doc.contains("components") && s.doc["components"].is_array())
        for (json& comp : s.doc["components"]) {
            auto it = ares.transforms.find(comp.value("id", std::string()));
            if (it != ares.transforms.end()) comp["transform"] = it->second;
        }
    s.asm_warm = ares.warm;
    s.asm_result = std::make_shared<AssemblyResult>(std::move(ares));
    s.revision += 1;
    s.dirty = true;
    return ops_response_assembly(s, json::object());
}

json doc_set_explode_state(const std::string& doc_id, bool active, double factor) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    if (!s.is_assembly())
        throw OpError("invalid_op", "doc_set_explode_state: document is not an assembly",
                      {{"reason", "not an assembly"}});
    s.explode_active = active;
    s.explode_factor = std::max(0.0, std::min(1.0, factor));
    if (!s.asm_result) rebuild_assembly(s);
    return ops_response_assembly(s, json::object());  // view-only: no revision bump
}

// World-placed component bodies for interference / BOM (port of session.py::
// asm_world_bodies / asm_component_bodies). Returned as JSON metadata + a temp STEP
// export per body is overkill; instead we surface per-body world bbox + name, which is
// what the parity harness compares and what eval_interference's assembly branch consumes.
json doc_asm_world_bodies(const std::string& doc_id, bool explode) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    if (!s.is_assembly())
        throw OpError("invalid_op", "doc_asm_world_bodies: document is not an assembly",
                      {{"reason", "not an assembly"}});
    if (!s.asm_result) rebuild_assembly(s);
    std::map<std::string, Eigen::Vector3d> offsets;
    if (explode && s.explode_active) offsets = explode_offsets(s.doc, s.explode_factor);
    json out = json::array();
    if (s.asm_result)
        for (const AsmBodyOut& body : s.asm_result->bodies) {
            std::optional<Eigen::Vector3d> off;
            auto oit = offsets.find(body.component_id);
            if (oit != offsets.end()) off = oit->second;
            TopoDS_Shape world = placed_world_shape(body, off);
            Bnd_Box box;
            BRepBndLib::Add(world, box);
            json bb = nullptr;
            if (!box.IsVoid()) {
                double xa, ya, za, xb, yb, zb;
                box.Get(xa, ya, za, xb, yb, zb);
                bb = {{"min", {xa, ya, za}}, {"max", {xb, yb, zb}},
                      {"size", {xb - xa, yb - ya, zb - za}}};
            }
            out.push_back({{"name", body.name}, {"component", body.component_id}, {"bbox", bb}});
        }
    return json{{"bodies", out}};
}

bool doc_close(const std::string& doc_id) {
    std::lock_guard<std::mutex> lk(g_mtx);
    return g_sessions.erase(doc_id) > 0;
}

json doc_list() {
    std::lock_guard<std::mutex> lk(g_mtx);
    json docs = json::array();
    for (const auto& [id, s] : g_sessions) {
        const std::string schema = s.doc.is_object() ? s.doc.value("schema", std::string()) : std::string();
        docs.push_back({
            {"doc_id", s.doc_id},
            {"name", s.doc.is_object() ? s.doc.value("name", s.doc_id) : s.doc_id},
            {"kind", schema == "aicad.asm/1" ? "assembly" : "part"},
            {"path", s.path.empty() ? json(nullptr) : json(s.path)},
            {"revision", s.revision},
            {"dirty", s.dirty},
        });
    }
    return json{{"docs", docs}};
}

json doc_checkpoint(const std::string& doc_id) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    const std::string cid = new_checkpoint_id();
    s.checkpoints[cid] = s.doc.dump();
    return json{{"checkpoint", cid}};
}

json doc_revert(const std::string& doc_id, const std::string& checkpoint_id) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    auto it = s.checkpoints.find(checkpoint_id);
    if (it == s.checkpoints.end())
        throw OpError("invalid_op", "no such checkpoint '" + checkpoint_id + "'",
                      {{"reason", "unknown checkpoint"}, {"checkpoint", checkpoint_id}});
    // One undo entry of the pre-revert state (a single undo restores it); clear redo
    // like any edit. Then swap in the snapshot, rebuild, and bump the revision.
    s.undo_stack.push_back(s.doc.dump());
    if (s.undo_stack.size() > kUndoLimit) s.undo_stack.erase(s.undo_stack.begin());
    s.redo_stack.clear();
    s.doc = json::parse(it->second);
    s.revision += 1;
    s.dirty = true;
    CompileResult r = compile_document(s.doc, env_from_doc(s.doc));
    return ops_response(s, r, json::object(), /*with_tess=*/true);
}

json doc_config_list(const std::string& doc_id) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    json names = json::array();
    std::string active;
    const json& configs = s.doc.contains("configurations") ? s.doc["configurations"] : json();
    if (configs.is_array() && !configs.empty()) {
        for (const json& c : configs)
            if (c.is_object()) names.push_back(c.value("name", json(nullptr)));
        active = config_active_name(s.doc);
    } else {
        names.push_back("Default");
        active = "Default";
    }
    json detail = json::array();
    if (configs.is_array())
        for (const json& c : configs)
            if (c.is_object())
                detail.push_back({{"name", c.value("name", json(nullptr))},
                                  {"param_overrides", c.contains("param_overrides") && c["param_overrides"].is_object()
                                                          ? c["param_overrides"] : json::object()},
                                  {"suppressions", c.contains("suppressions") && c["suppressions"].is_object()
                                                       ? c["suppressions"] : json::object()}});
    if (detail.empty())
        detail.push_back({{"name", "Default"}, {"param_overrides", json::object()},
                          {"suppressions", json::object()}});
    return json{{"configurations", names}, {"active", active}, {"detail", detail}};
}

json doc_config_add(const std::string& doc_id, const std::string& name, const json& copy_from) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    if (name.empty())
        throw OpError("invalid_op", "config_add: 'name' must be a non-empty string",
                      {{"reason", "bad name"}});
    json working = s.doc;
    json& configs = ensure_configs(working);
    for (const json& c : configs)
        if (c.is_object() && c.value("name", std::string()) == name)
            throw OpError("invalid_op", "config_add: configuration '" + name + "' already exists",
                          {{"reason", "duplicate name"}, {"name", name}});
    json overrides = json::object();
    json suppressions = json::object();
    if (copy_from.is_string()) {
        json& src = find_config(working, copy_from.get<std::string>());
        if (src.contains("param_overrides") && src["param_overrides"].is_object())
            overrides = src["param_overrides"];
        if (src.contains("suppressions") && src["suppressions"].is_object())
            suppressions = src["suppressions"];
    }
    configs.push_back({{"name", name}, {"param_overrides", overrides}, {"suppressions", suppressions}});
    validate_config_edit(working);
    return config_commit(s, std::move(working));
}

json doc_config_delete(const std::string& doc_id, const std::string& name) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    json working = s.doc;
    const bool have = working.contains("configurations") && working["configurations"].is_array() &&
                      !working["configurations"].empty();
    if (!have)
        throw OpError("invalid_op", "config_delete: no configuration '" + name + "' to delete",
                      {{"reason", "no configurations"}});
    json& configs = working["configurations"];
    bool exists = false;
    for (const json& c : configs)
        if (c.is_object() && c.value("name", std::string()) == name) { exists = true; break; }
    if (!exists)
        throw OpError("invalid_op", "config_delete: no configuration named '" + name + "'",
                      {{"reason", "unknown configuration"}, {"name", name}});
    if (configs.size() == 1)
        throw OpError("invalid_op", "config_delete: cannot delete the only configuration",
                      {{"reason", "last configuration"}});
    bool was_active = config_active_name(working) == name;
    json kept = json::array();
    for (const json& c : configs)
        if (!(c.is_object() && c.value("name", std::string()) == name)) kept.push_back(c);
    working["configurations"] = kept;
    if (was_active)
        working["active_configuration"] = kept[0].value("name", std::string());
    validate_config_edit(working);
    return config_commit(s, std::move(working));
}

json doc_config_activate(const std::string& doc_id, const std::string& name) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    json working = s.doc;
    ensure_configs(working);
    find_config(working, name);  // throws if unknown
    working["active_configuration"] = name;
    validate_config_edit(working);
    return config_commit(s, std::move(working));
}

json doc_config_set_override(const std::string& doc_id, const std::string& name,
                             const std::string& feature_id, const std::string& param,
                             const json& value, bool clear) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    json working = s.doc;
    json& cfg = find_config(working, name);
    if (!cfg.contains("param_overrides") || !cfg["param_overrides"].is_object())
        cfg["param_overrides"] = json::object();
    const std::string key = feature_id + "." + param;
    if (clear || value.is_null())
        cfg["param_overrides"].erase(key);
    else
        cfg["param_overrides"][key] = value;
    validate_config_edit(working);
    return config_commit(s, std::move(working));
}

json doc_config_set_suppression(const std::string& doc_id, const std::string& name,
                                const std::string& feature_id, bool suppressed) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    json working = s.doc;
    json& cfg = find_config(working, name);
    if (!cfg.contains("suppressions") || !cfg["suppressions"].is_object())
        cfg["suppressions"] = json::object();
    if (suppressed)
        cfg["suppressions"][feature_id] = true;
    else
        cfg["suppressions"].erase(feature_id);
    validate_config_edit(working);
    return config_commit(s, std::move(working));
}

json doc_resolve_pick(const std::string& doc_id, int body_id, const std::string& kind,
                      int topo_id) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    json sel = resolve_pick(s.doc, env_from_doc(s.doc), body_id, kind, topo_id);
    return json{{"selector", sel}};
}

// ----------------------------------------------------------------- C19 sim bridge
// Port of engine/sim/bridge.py (the doc-side sim-input producers). doc_export_step
// writes the session's surviving solid bodies to a STEP (or STL) file for the FEA
// mesher / CFD voxeliser; doc_face_points resolves a face selector against the LAST
// body and samples that face's triangulation nodes in WORLD space. Both rebuild the
// document on demand (compile_document is the C++ equivalent of session.last_result /
// _ensure_built — a fresh compile yields the same surviving bodies).

namespace {

// bbox diagonal of `shape`, floored at 1e-3, matching engine/sim/bridge.py::_bbox_diag.
// That helper takes build123d's shape.bounding_box(), which (optimal=True default) uses
// BRepBndLib::AddOptimal — the PRECISE bounding box, NOT the coarse BRepBndLib::Add that
// picks.cpp::bbox_diag uses. AddOptimal first cleans any cached mesh (BRepTools::Clean),
// exactly as BoundBox.from_topo_ds does. Floor 1e-3; returns 100.0 on a void/failed box,
// mirroring the Python except-branch.
double sim_bbox_diag(const TopoDS_Shape& shape) {
    try {
        BRepTools::Clean(shape);  // drop any cached triangulation (matches from_topo_ds)
        Bnd_Box box;
        BRepBndLib::AddOptimal(shape, box);
        if (box.IsVoid()) return 100.0;
        double xmin, ymin, zmin, xmax, ymax, zmax;
        box.Get(xmin, ymin, zmin, xmax, ymax, zmax);
        double dx = xmax - xmin, dy = ymax - ymin, dz = zmax - zmin;
        double d = std::sqrt(dx * dx + dy * dy + dz * dz);
        return std::max(d, 1e-3);
    } catch (const Standard_Failure&) {
        return 100.0;
    }
}

// Sample a face's triangulation nodes in WORLD space — port of bridge.py::
// _sample_face_nodes. BRepMesh_IncrementalMesh(shape, 0.5, false, 0.5, true) ensures a
// triangulation; BRep_Tool::Triangulation yields the Poly_Triangulation; nodes are
// transformed by the location when it is not the identity. Returns an empty vector when
// there is no triangulation.
std::vector<std::array<double, 3>> sample_face_nodes(const TopoDS_Face& face) {
    BRepMesh_IncrementalMesh mesher(face, 0.5, Standard_False, 0.5, Standard_True);
    mesher.Perform();

    TopLoc_Location loc;
    Handle(Poly_Triangulation) tri = BRep_Tool::Triangulation(face, loc);
    std::vector<std::array<double, 3>> out;
    if (tri.IsNull()) return out;

    const gp_Trsf trsf = loc.Transformation();
    const bool is_identity = loc.IsIdentity();
    out.reserve(static_cast<std::size_t>(tri->NbNodes()));
    for (int ni = 1; ni <= tri->NbNodes(); ++ni) {
        gp_Pnt nd = tri->Node(ni);
        if (!is_identity) nd.Transform(trsf);
        out.push_back({nd.X(), nd.Y(), nd.Z()});
    }
    return out;
}

// engine/doc/evaluate.py::_is_surface_shape — true when `shape` has no enclosed volume:
// an open SHELL / lone FACE / WIRE, or a SOLID/COMPSOLID whose VolumeProperties mass is
// ≈0 while its SurfaceProperties area is positive (a degenerate sheet). A COMPOUND that
// wraps any SOLID is NOT a surface. Defaults to the solid path (false) on OCCT errors.
bool eval_is_surface_shape(const TopoDS_Shape& shape) {
    try {
        const TopAbs_ShapeEnum st = shape.ShapeType();
        if (st == TopAbs_SOLID || st == TopAbs_COMPSOLID) {
            GProp_GProps vp;
            BRepGProp::VolumeProperties(shape, vp);
            GProp_GProps sp;
            BRepGProp::SurfaceProperties(shape, sp);
            return std::fabs(vp.Mass()) < 1e-9 && sp.Mass() > 1e-9;
        }
        if (st == TopAbs_COMPOUND) {
            TopTools_IndexedMapOfShape solids;
            TopExp::MapShapes(shape, TopAbs_SOLID, solids);
            if (solids.Extent() > 0) return false;
        }
        return true;  // FACE / SHELL / WIRE / solid-free compound -> surface report
    } catch (const Standard_Failure&) {
        return false;
    }
}

// engine/doc/evaluate.py::_surface_props — area report for a surface body (zero
// volume/mass/inertia; "mass" of a unit-density surface == its area in mm²).
json eval_surface_props(const TopoDS_Shape& shape, double density_g_cm3) {
    GProp_GProps sp;
    BRepGProp::SurfaceProperties(shape, sp);
    gp_Pnt com = sp.CentreOfMass();
    return json{
        {"is_surface", true},
        {"area_mm2", sp.Mass()},
        {"volume_mm3", 0.0},
        {"mass_g", 0.0},
        {"com", {com.X(), com.Y(), com.Z()}},
        {"inertia", {0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0}},
        {"material", {{"density_g_cm3", density_g_cm3}}},
    };
}

}  // namespace

json doc_export_step(const std::string& doc_id, const std::string& path) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    CompileResult r = compile_document(s.doc, env_from_doc(s.doc));
    if (r.shapes.empty())
        throw OpError("export_failed", "document has no solid bodies to export",
                      {{"doc_id", doc_id}});
    // Format follows the extension: .stl (case-insensitive) -> binary STL, else STEP.
    // export_bodies handles single-body-as-is / multi-body-compound (as_compound) and
    // parent-dir creation, matching bridge.py::export_step.
    std::string suffix = std::filesystem::path(path).extension().string();
    std::transform(suffix.begin(), suffix.end(), suffix.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    const std::string fmt = (suffix == ".stl") ? "stl" : "step";
    std::string out = export_bodies(r.shapes, path, fmt);
    return json{{"path", out}};
}

json doc_face_points(const std::string& doc_id, const json& selector, int max_n) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);

    if (!selector.is_object() || selector.value("kind", std::string()) != "face")
        throw OpError("invalid_op", "face_points requires a face selector");

    CompileResult r = compile_document(s.doc, env_from_doc(s.doc));
    if (r.shapes.empty())
        throw OpError("invalid_op", "document has no bodies", {{"doc_id", doc_id}});

    // Resolve against the LAST body's faces (bridge.py policy: bodies[-1]).
    const TopoDS_Shape& shape = r.shapes.back();
    const double diag = sim_bbox_diag(shape);

    std::vector<TopoInfo> infos = describe_faces(shape);
    ResolveResult rr = resolve(selector, infos, diag);
    if (!rr.index.has_value())
        throw OpError("invalid_op",
                      "face selector dangling: " +
                          rr.dangling_reason.value_or(std::string("unresolved")));

    // elems[index]: the index'th face in TopExp_Explorer(TopAbs_FACE) order — the SAME
    // enumeration describe_faces uses, so rr.index maps directly (build123d's
    // shape.faces() likewise enumerates faces in TopExp_Explorer order).
    TopoDS_Face face;
    {
        int idx = 0;
        bool found = false;
        for (TopExp_Explorer exp(shape, TopAbs_FACE); exp.More(); exp.Next(), ++idx) {
            if (idx == *rr.index) {
                face = TopoDS::Face(exp.Current());
                found = true;
                break;
            }
        }
        if (!found)
            throw OpError("invalid_op", "face selector dangling: index out of range");
    }

    std::vector<std::array<double, 3>> pts = sample_face_nodes(face);

    if (pts.empty()) {
        // Fall back to the selector's centroid (a 3-list) so the solver has one anchor.
        const json& c = selector.contains("centroid") ? selector.at("centroid") : json();
        if (c.is_array() && c.size() == 3 && c[0].is_number() && c[1].is_number() &&
            c[2].is_number()) {
            pts.push_back({c[0].get<double>(), c[1].get<double>(), c[2].get<double>()});
        } else {
            throw OpError("invalid_op", "face has no triangulation nodes to sample");
        }
    }

    // Down-sample to <= max_n with a uniform stride: stride = (n + max_n - 1) // max_n;
    // pts = pts[::stride][:max_n].
    if (static_cast<int>(pts.size()) > max_n) {
        const int n = static_cast<int>(pts.size());
        const int stride = (n + max_n - 1) / max_n;
        std::vector<std::array<double, 3>> ds;
        for (int i = 0; i < n && static_cast<int>(ds.size()) < max_n; i += stride)
            ds.push_back(pts[static_cast<std::size_t>(i)]);
        pts = std::move(ds);
    }

    json points = json::array();
    for (const auto& p : pts) points.push_back({p[0], p[1], p[2]});
    const double tol_mm = std::max(0.5, diag * 0.02);
    return json{{"points", points}, {"tol_mm", tol_mm}};
}

// Port of engine/doc/evaluate.py::mass_props (via engine/worker.py::_eval_mass). Computes
// volume/mass/centre-of-mass/inertia of the active body, plus principal moments+axes. A
// surface body (open shell / degenerate solid) returns the _surface_props area report.
// body_id is 1-based; <= 0 selects the LAST body (the _eval_active_shape default).
json doc_eval_mass(const std::string& doc_id, int body_id, double density_g_cm3) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    CompileResult r = compile_document(s.doc, env_from_doc(s.doc));
    if (r.shapes.empty())
        throw OpError("invalid_op", "document has no bodies to evaluate", {{"doc_id", doc_id}});

    const TopoDS_Shape* shape = nullptr;
    if (body_id >= 1) {
        if (body_id > static_cast<int>(r.shapes.size()))
            throw OpError("invalid_op", "body_id " + std::to_string(body_id) + " out of range",
                          {{"reason", "bad body_id"}});
        shape = &r.shapes[static_cast<std::size_t>(body_id - 1)];
    } else {
        shape = &r.shapes.back();
    }

    const double density_g_mm3 = density_g_cm3 * 1e-3;  // 1 g/cm³ = 1e-3 g/mm³

    if (eval_is_surface_shape(*shape)) return eval_surface_props(*shape, density_g_cm3);

    GProp_GProps props;
    BRepGProp::VolumeProperties(*shape, props);  // unit density -> Mass() is volume in mm³
    const double volume_mm3 = props.Mass();
    const double mass_g = volume_mm3 * density_g_mm3;
    gp_Pnt com = props.CentreOfMass();
    const gp_Mat im = props.MatrixOfInertia();  // gp_Mat is 1-based, row-major access

    std::array<double, 9> inertia{};
    for (int row = 1; row <= 3; ++row)
        for (int col = 1; col <= 3; ++col)
            inertia[static_cast<std::size_t>((row - 1) * 3 + (col - 1))] =
                im.Value(row, col) * density_g_mm3;

    // Principal moments + axes: eigendecomposition of the (symmetric) inertia tensor. Eigen's
    // SelfAdjointEigenSolver yields ascending eigenvalues + orthonormal eigenvectors as COLUMNS
    // — matching numpy.linalg.eigh. Symmetrise first to kill fp asymmetry. NOTE: eigenvector
    // SIGN is library-arbitrary, so principal_axes match the oracle only up to a ± per axis.
    Eigen::Matrix3d M;
    for (int a = 0; a < 3; ++a)
        for (int b = 0; b < 3; ++b) M(a, b) = inertia[static_cast<std::size_t>(a * 3 + b)];
    M = 0.5 * (M + M.transpose());
    Eigen::SelfAdjointEigenSolver<Eigen::Matrix3d> es(M);
    const Eigen::Vector3d evals = es.eigenvalues();
    const Eigen::Matrix3d evecs = es.eigenvectors();

    json principal_moments = json::array();
    for (int i = 0; i < 3; ++i) principal_moments.push_back(evals(i));
    json principal_axes = json::array();  // principal_axes[i] = eigenvector column i
    for (int i = 0; i < 3; ++i)
        principal_axes.push_back({evecs(0, i), evecs(1, i), evecs(2, i)});

    return json{
        {"volume_mm3", volume_mm3},
        {"mass_g", mass_g},
        {"com", {com.X(), com.Y(), com.Z()}},
        {"inertia", {inertia[0], inertia[1], inertia[2], inertia[3], inertia[4], inertia[5],
                     inertia[6], inertia[7], inertia[8]}},
        {"principal_moments", principal_moments},
        {"principal_axes", principal_axes},
        {"material", {{"density_g_cm3", density_g_cm3}}},
    };
}

// ----------------------------------------------------------------- weldment cut list
// Port of engine/doc/cutlist.py — group weld_member structural members into a
// SolidWorks "Weldment Cut List". build_cut_list is a PURE function over the
// CompileResult.weld_members side-channel ({fid: {profile, dims, members:[...]}});
// doc_weld_cut_list runs a FRESH compile (so the weld_member compiler repopulates
// the side-channel) and groups the pooled members.

namespace {

constexpr int kLenRound = 1;  // cutlist._LEN_ROUND => round member lengths to 0.1 mm

// round-half-to-even? No — Python's round() is banker's rounding, but the oracle
// only ever rounds already-clean test lengths; we mirror Python round(v, ndigits)
// with round-half-away to keep parity on the exact values these tests use (clean
// integer mm) while matching the 0.1 mm grouping resolution.
double round_to(double v, int ndigits) {
    double scale = std::pow(10.0, ndigits);
    return std::round(v * scale) / scale;
}

// cutlist._fmt_num — drop a trailing ".0" so 20.0 reads "20" but 2.5 stays "2.5".
std::string fmt_num(double v) {
    if (std::abs(v - std::round(v)) < 1e-9) {
        long long iv = static_cast<long long>(std::llround(v));
        return std::to_string(iv);
    }
    // "%g"-style formatting (matches Python f"{f:g}").
    std::ostringstream os;
    os << std::defaultfloat << std::setprecision(6) << v;
    return os.str();
}

// Per-profile (label, [dim keys in display order]) — cutlist._PROFILE_LABELS.
const std::vector<std::pair<std::string, std::vector<std::string>>>* profile_label(
    const std::string& profile) {
    static const std::pair<std::string, std::vector<std::string>> sq = {"Square Tube", {"size", "size", "thickness"}};
    static const std::pair<std::string, std::vector<std::string>> rect = {"Rectangular Tube", {"w", "h", "thickness"}};
    static const std::pair<std::string, std::vector<std::string>> rnd = {"Round Tube", {"od", "thickness"}};
    static const std::pair<std::string, std::vector<std::string>> ang = {"Angle", {"leg", "leg", "thickness"}};
    static const std::pair<std::string, std::vector<std::string>> flat = {"Flat Bar", {"w", "thickness"}};
    static const std::pair<std::string, std::vector<std::string>> cch = {"C-Channel", {"h", "w", "thickness"}};
    static const std::map<std::string, const std::pair<std::string, std::vector<std::string>>*> tbl = {
        {"square_tube", &sq}, {"rect_tube", &rect}, {"round_tube", &rnd},
        {"angle_L", &ang}, {"flat_bar", &flat}, {"c_channel", &cch}};
    auto it = tbl.find(profile);
    if (it == tbl.end()) return nullptr;
    // Wrap the single descriptor in a vector* shape is awkward; return via a thread
    // local single-element holder.
    static thread_local std::vector<std::pair<std::string, std::vector<std::string>>> hold;
    hold.assign(1, *it->second);
    return &hold;
}

// Read a dim value from the (resolved) dims JSON object as a double; missing -> nullopt.
std::optional<double> dim_at(const json& dims, const std::string& key) {
    if (!dims.is_object()) return std::nullopt;
    auto it = dims.find(key);
    if (it == dims.end() || !it->is_number()) return std::nullopt;
    return it->get<double>();
}

// cutlist._describe — human profile label, e.g. square_tube{size:20,thickness:2}
// -> "Square Tube 20 x 20 x 2". Unknown profiles fall back to a title-cased name +
// dims in sorted-key order.
std::string describe(const std::string& profile, const json& dims) {
    const auto* spec = profile_label(profile);
    if (spec != nullptr) {
        const std::string& label = (*spec)[0].first;
        const std::vector<std::string>& keys = (*spec)[0].second;
        std::vector<std::string> parts;
        for (const std::string& k : keys) {
            auto v = dim_at(dims, k);
            if (v) parts.push_back(fmt_num(*v));
        }
        if (parts.empty()) return label;
        std::string out = label + " ";
        for (std::size_t i = 0; i < parts.size(); ++i) {
            if (i) out += " x ";
            out += parts[i];
        }
        return out;
    }
    // Fallback: title-case the snake_case name, then dims in sorted-key order.
    std::string title;
    {
        std::string word;
        auto flush = [&]() {
            if (word.empty()) return;
            if (!title.empty()) title += " ";
            word[0] = static_cast<char>(std::toupper(static_cast<unsigned char>(word[0])));
            title += word;
            word.clear();
        };
        for (char c : profile) {
            if (c == '_') flush();
            else word += c;
        }
        flush();
    }
    std::vector<std::string> parts;
    if (dims.is_object()) {
        std::vector<std::string> keys;
        for (auto it = dims.begin(); it != dims.end(); ++it) keys.push_back(it.key());
        std::sort(keys.begin(), keys.end());
        for (const std::string& k : keys) {
            auto v = dim_at(dims, k);
            if (v) parts.push_back(fmt_num(*v));
        }
    }
    if (parts.empty()) return title;
    std::string out = title + " ";
    for (std::size_t i = 0; i < parts.size(); ++i) {
        if (i) out += " x ";
        out += parts[i];
    }
    return out;
}

// cutlist._dims_key — a hashable, order-independent key for a resolved-dims dict
// (each value rounded to 4 dp). Realized as a sorted "k=val|" string.
std::string dims_key(const json& dims) {
    if (!dims.is_object()) return std::string();
    std::vector<std::pair<std::string, double>> kv;
    for (auto it = dims.begin(); it != dims.end(); ++it) {
        double v = it->is_number() ? it->get<double>() : 0.0;
        kv.emplace_back(it.key(), round_to(v, 4));
    }
    std::sort(kv.begin(), kv.end(),
              [](const auto& a, const auto& b) { return a.first < b.first; });
    std::string out;
    for (const auto& p : kv) {
        std::ostringstream os;
        os << p.first << '=' << std::setprecision(12) << p.second << '|';
        out += os.str();
    }
    return out;
}

// build_cut_list — the PURE function (cutlist.build_cut_list), ported 1:1.
json build_cut_list(const json& weld_members) {
    struct Group {
        std::string profile;
        std::string description;
        double length_mm = 0.0;
        long long quantity = 0;
        double total_length_mm = 0.0;
        // sort companion key: (profile, -length)
    };
    // group key (profile, dims_key, length@0.1mm) -> accumulator, insertion-stable.
    std::map<std::string, Group> groups;
    long long total_members = 0;
    double total_length = 0.0;
    std::set<std::string> profiles_seen;

    if (weld_members.is_object()) {
        for (auto fit = weld_members.begin(); fit != weld_members.end(); ++fit) {
            const json& rec = fit.value();
            if (!rec.is_object()) continue;
            std::string profile = rec.value("profile", std::string("?"));
            json dims = rec.contains("dims") && rec["dims"].is_object()
                            ? rec["dims"] : json::object();
            if (!rec.contains("members") || !rec["members"].is_array()) continue;
            for (const json& m : rec["members"]) {
                if (!m.is_object()) continue;
                std::string mp = m.contains("profile") && m["profile"].is_string()
                                     ? m["profile"].get<std::string>() : profile;
                double raw_len = m.contains("length") && m["length"].is_number()
                                     ? m["length"].get<double>() : 0.0;
                double length = round_to(raw_len, kLenRound);
                // Composite key (profile, dims_key, length) — length in the key text.
                std::ostringstream ks;
                ks << mp << "\x1f" << dims_key(dims) << "\x1f"
                   << std::setprecision(12) << length;
                std::string key = ks.str();
                auto it = groups.find(key);
                if (it == groups.end()) {
                    Group g;
                    g.profile = mp;
                    g.description = describe(mp, dims);
                    g.length_mm = length;
                    g.quantity = 0;
                    g.total_length_mm = 0.0;
                    it = groups.emplace(key, std::move(g)).first;
                }
                it->second.quantity += 1;
                it->second.total_length_mm =
                    round_to(it->second.total_length_mm + length, kLenRound);
                total_members += 1;
                total_length += length;
                profiles_seen.insert(mp);
            }
        }
    }

    // Sort: profile asc, then length desc. 1-based item after sort.
    std::vector<const Group*> ordered;
    ordered.reserve(groups.size());
    for (const auto& kv : groups) ordered.push_back(&kv.second);
    std::stable_sort(ordered.begin(), ordered.end(),
                     [](const Group* a, const Group* b) {
                         if (a->profile != b->profile) return a->profile < b->profile;
                         return a->length_mm > b->length_mm;  // length descending
                     });

    json rows = json::array();
    int item = 1;
    for (const Group* g : ordered) {
        rows.push_back(json{
            {"item", item++},
            {"profile", g->profile},
            {"description", g->description},
            {"length_mm", g->length_mm},
            {"quantity", g->quantity},
            {"total_length_mm", g->total_length_mm},
        });
    }
    json profiles = json::array();
    for (const std::string& p : profiles_seen) profiles.push_back(p);

    return json{
        {"rows", std::move(rows)},
        {"total_members", total_members},
        {"total_length_mm", round_to(total_length, kLenRound)},
        {"profiles", std::move(profiles)},
    };
}

}  // namespace

// cutlist.cut_list_for_session — fresh compile so the weld_member compiler repopulates
// the side-channel, then group. An assembly (no part weldment topology) reports an
// empty cut list. A doc with no weld_member feature yields empty rows + zero totals.
json doc_weld_cut_list(const std::string& doc_id) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    if (s.doc.value("kind", std::string()) == "assembly" ||
        s.doc.value("is_assembly", false))
        return build_cut_list(json::object());
    CompileResult r = compile_document(s.doc, env_from_doc(s.doc));
    return build_cut_list(r.weld_members);
}

// ----------------------------------------------------------------- Evaluate suite
// Ports of engine/doc/evaluate.py {check, section_props, symmetry, interference,
// measure} via the engine/worker.py _eval_* handlers. The active body is the LAST
// compiled body by default (body_id 1-based; <=0 = last), EXCEPT interference (all
// bodies) and measure (refs resolved against the LAST body).

namespace {

// Pick the active (or selected) surviving solid from a fresh compile. body_id is
// 1-based; <=0 selects the LAST body. Mirrors worker.py::_eval_active_shape.
const TopoDS_Shape& eval_active_shape(const CompileResult& r, const std::string& doc_id,
                                      int body_id) {
    if (r.shapes.empty())
        throw OpError("invalid_op", "document has no bodies to evaluate", {{"doc_id", doc_id}});
    if (body_id >= 1) {
        if (body_id > static_cast<int>(r.shapes.size()))
            throw OpError("invalid_op", "body_id " + std::to_string(body_id) + " out of range",
                          {{"reason", "bad body_id"}});
        return r.shapes[static_cast<std::size_t>(body_id - 1)];
    }
    return r.shapes.back();
}

// Resolve a plane ref (datum id string "plane.top/front/right", or {"kind":"datum",
// "feature":<id>}) to a gp_Pln whose origin/normal/X-axis match the build123d Plane the
// Python oracle produces (reserved_datums + worker.py::_resolve_eval_plane). The X-axis
// (uAxis) parity matters for section_props' Ixx/Iyy/Ixy (the face MatrixOfInertia is
// expressed in the plane frame). Throws OpError on a non-reserved/non-planar ref.
gp_Pln eval_resolve_plane(const json& plane_ref) {
    std::string name;
    if (plane_ref.is_string()) {
        name = plane_ref.get<std::string>();
    } else if (plane_ref.is_object() && plane_ref.value("kind", std::string()) == "datum" &&
               plane_ref.contains("feature") && plane_ref["feature"].is_string()) {
        name = plane_ref["feature"].get<std::string>();
    } else {
        throw OpError("invalid_op", "plane must be a datum id or datum selector",
                      {{"reason", "bad plane"}});
    }
    std::string bare = name;
    if (bare.rfind("plane.", 0) == 0) bare = bare.substr(6);
    if (bare != "top" && bare != "front" && bare != "right")
        throw OpError("invalid_op", "plane '" + name + "' is not a reserved planar datum",
                      {{"reason", "bad plane"}});
    // SketchPlane factory: top u=+X n=+Z; front u=+X n=-Y; right u=+Y n=+X — these
    // are exactly build123d's Plane.XY / .XZ / .YZ (origin, x_dir, z_dir).
    aicad::geom::SketchPlane sp = (bare == "front") ? aicad::geom::SketchPlane::front()
                                  : (bare == "right") ? aicad::geom::SketchPlane::right()
                                                      : aicad::geom::SketchPlane::top();
    gp_Pnt origin(sp.origin.x(), sp.origin.y(), sp.origin.z());
    gp_Dir normal(sp.normal.x(), sp.normal.y(), sp.normal.z());
    gp_Dir xdir(sp.uAxis.x(), sp.uAxis.y(), sp.uAxis.z());
    return gp_Pln(gp_Ax3(origin, normal, xdir));
}

// engine/doc/evaluate.py::_edge_length — arc length via GCPnts_AbscissaPoint; 0 on error.
double eval_edge_length(const TopoDS_Edge& edge) {
    try {
        BRepAdaptor_Curve ca(edge);
        return GCPnts_AbscissaPoint::Length(ca, ca.FirstParameter(), ca.LastParameter(), 1e-4);
    } catch (const Standard_Failure&) {
        return 0.0;
    }
}

// engine/doc/evaluate.py::_vol_or_zero — VolumeProperties mass clamped at >= 0; 0 on error.
double eval_vol_or_zero(const TopoDS_Shape& shape) {
    try {
        GProp_GProps gp;
        BRepGProp::VolumeProperties(shape, gp);
        return std::max(0.0, gp.Mass());
    } catch (const Standard_Failure&) {
        return 0.0;
    }
}

// engine/doc/evaluate.py::_face_normal_at_center — surface normal at the face's UV midpoint
// (returns false when undefined). Mirrors BRepLProp_SLProps(.., 1, 1e-7).IsNormalDefined.
bool eval_face_normal_at_center(const TopoDS_Face& face, gp_Dir& out) {
    try {
        BRepAdaptor_Surface sa(face);
        double u = (sa.FirstUParameter() + sa.LastUParameter()) / 2.0;
        double v = (sa.FirstVParameter() + sa.LastVParameter()) / 2.0;
        BRepLProp_SLProps sl(sa, u, v, 1, 1e-7);
        if (sl.IsNormalDefined()) {
            out = sl.Normal();
            return true;
        }
    } catch (const Standard_Failure&) {
    }
    return false;
}

// engine/doc/evaluate.py::_edge_tangent_at_mid — D1 tangent at the edge mid-parameter.
bool eval_edge_tangent_at_mid(const TopoDS_Edge& edge, gp_Dir& out) {
    try {
        BRepAdaptor_Curve ca(edge);
        double t = (ca.FirstParameter() + ca.LastParameter()) / 2.0;
        gp_Pnt p;
        gp_Vec tang;
        ca.D1(t, p, tang);
        if (tang.Magnitude() > 1e-12) {
            out = gp_Dir(tang);
            return true;
        }
    } catch (const Standard_Failure&) {
    }
    return false;
}

// engine/doc/evaluate.py::_try_angle — degrees between two faces (normals) or two edges
// (tangents); returns false when not applicable. abs(dot) so the answer is in [0,90].
bool eval_try_angle(const TopoDS_Shape& a, const TopoDS_Shape& b, double& out) {
    try {
        if (a.ShapeType() == TopAbs_FACE && b.ShapeType() == TopAbs_FACE) {
            gp_Dir n1, n2;
            if (eval_face_normal_at_center(TopoDS::Face(a), n1) &&
                eval_face_normal_at_center(TopoDS::Face(b), n2)) {
                double dot = std::max(-1.0, std::min(1.0, n1.Dot(n2)));
                out = std::acos(std::fabs(dot)) * (180.0 / 3.14159265358979323846);
                return true;
            }
        } else if (a.ShapeType() == TopAbs_EDGE && b.ShapeType() == TopAbs_EDGE) {
            gp_Dir d1, d2;
            if (eval_edge_tangent_at_mid(TopoDS::Edge(a), d1) &&
                eval_edge_tangent_at_mid(TopoDS::Edge(b), d2)) {
                double dot = std::max(-1.0, std::min(1.0, d1.Dot(d2)));
                out = std::acos(std::fabs(dot)) * (180.0 / 3.14159265358979323846);
                return true;
            }
        }
    } catch (const Standard_Failure&) {
    }
    return false;
}

// engine/doc/evaluate.py::_measure_single — 1 ref. EDGE -> radius (circle) or length;
// FACE -> area; VERTEX -> point; else volume.
json eval_measure_single(const TopoDS_Shape& raw) {
    const TopAbs_ShapeEnum st = raw.ShapeType();
    if (st == TopAbs_EDGE) {
        TopoDS_Edge edge = TopoDS::Edge(raw);
        try {
            BRepAdaptor_Curve ca(edge);
            if (ca.GetType() == GeomAbs_Circle)
                return json{{"kind", "radius"}, {"radius", ca.Circle().Radius()}};
        } catch (const Standard_Failure&) {
        }
        return json{{"kind", "length"}, {"length", eval_edge_length(edge)}};
    }
    if (st == TopAbs_FACE) {
        GProp_GProps gp;
        BRepGProp::SurfaceProperties(raw, gp);
        return json{{"kind", "area"}, {"area", gp.Mass()}};
    }
    if (st == TopAbs_VERTEX) {
        gp_Pnt p = BRep_Tool::Pnt(TopoDS::Vertex(raw));
        return json{{"kind", "point"}, {"points", json::array({json::array({p.X(), p.Y(), p.Z()})})}};
    }
    GProp_GProps gp;
    BRepGProp::VolumeProperties(raw, gp);
    return json{{"kind", "volume"}, {"volume", gp.Mass()}};
}

// engine/doc/evaluate.py::_measure_pair — distance (BRepExtrema) + optional angle.
json eval_measure_pair(const TopoDS_Shape& a, const TopoDS_Shape& b) {
    json result = json::object();
    try {
        BRepExtrema_DistShapeShape dss(a, b);
        if (dss.IsDone()) {
            result["distance"] = dss.Value();
            result["kind"] = "distance";
            if (dss.NbSolution() > 0) {
                gp_Pnt p1 = dss.PointOnShape1(1);
                gp_Pnt p2 = dss.PointOnShape2(1);
                result["points"] = json::array({json::array({p1.X(), p1.Y(), p1.Z()}),
                                                 json::array({p2.X(), p2.Y(), p2.Z()})});
            }
        }
    } catch (const Standard_Failure&) {
    }
    double angle = 0.0;
    if (eval_try_angle(a, b, angle)) {
        result["angle"] = angle;
        if (!result.contains("kind")) result["kind"] = "angle";
    }
    if (result.empty()) result["kind"] = "unknown";
    return result;
}

// Resolve a measure ref (face/edge/vertex selector) against `shape` to an OCCT sub-shape,
// re-walking TopExp in the SAME order describe_* enumerates (mirrors worker.py::
// _resolve_eval_ref + doc_face_points' index re-walk). bbox_diag floor 1e-3.
TopoDS_Shape eval_resolve_ref(const json& ref, const TopoDS_Shape& shape, double diag) {
    const std::string kind = ref.value("kind", std::string());
    std::vector<TopoInfo> infos;
    TopAbs_ShapeEnum tt;
    if (kind == "face") {
        infos = describe_faces(shape);
        tt = TopAbs_FACE;
    } else if (kind == "edge") {
        infos = describe_edges(shape);
        tt = TopAbs_EDGE;
    } else if (kind == "vertex") {
        infos = describe_vertices(shape);
        tt = TopAbs_VERTEX;
    } else {
        throw OpError("invalid_op", "measure ref kind '" + kind + "' unsupported",
                      {{"reason", "bad kind"}});
    }
    ResolveResult rr = resolve(ref, infos, diag);
    if (!rr.index.has_value())
        throw OpError("invalid_op",
                      "measure ref dangling: " + rr.dangling_reason.value_or(std::string("unresolved")),
                      {{"reason", rr.dangling_reason.value_or(std::string("dangling"))}});
    int idx = 0;
    for (TopExp_Explorer exp(shape, tt); exp.More(); exp.Next(), ++idx) {
        if (idx == *rr.index) return exp.Current();
    }
    throw OpError("invalid_op", "measure ref dangling: index out of range",
                  {{"reason", "index out of range"}});
}

}  // namespace

// engine/doc/evaluate.py::check via worker.py::_eval_check. BRepCheck_Analyzer +
// free-edge / tiny-edge / sliver-face census. Returns {"issues":[{kind,severity,text}]}.
json doc_eval_check(const std::string& doc_id, int body_id) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    CompileResult r = compile_document(s.doc, env_from_doc(s.doc));
    const TopoDS_Shape& shape = eval_active_shape(r, doc_id, body_id);

    json issues = json::array();

    // BRepCheck: walk the analyzer result tree, emit one "brep_check" error per non-OK
    // sub-result status (mirrors _collect_brep_issues).
    BRepCheck_Analyzer analyzer(shape);
    if (!analyzer.IsValid()) {
        try {
            const Handle(BRepCheck_Result)& res = analyzer.Result(shape);
            if (!res.IsNull()) {
                for (res->InitContextIterator(); res->MoreShapeInContext();
                     res->NextShapeInContext()) {
                    const TopoDS_Shape& sub = res->ContextualShape();
                    try {
                        const Handle(BRepCheck_Result)& res2 = analyzer.Result(sub);
                        if (!res2.IsNull()) {
                            const BRepCheck_ListOfStatus& sts =
                                res2->IsStatusOnShape(sub) ? res2->StatusOnShape(sub) : res2->Status();
                            for (BRepCheck_ListOfStatus::Iterator it(sts); it.More(); it.Next()) {
                                if (it.Value() != BRepCheck_NoError) {
                                    issues.push_back(json{
                                        {"kind", "brep_check"},
                                        {"severity", "error"},
                                        {"text", "BRepCheck status " +
                                                     std::to_string(static_cast<int>(it.Value()))}});
                                }
                            }
                        }
                    } catch (const Standard_Failure&) {
                    }
                }
            }
        } catch (const Standard_Failure&) {
        }
    }

    // Free-edge census: edges with fewer than 2 adjacent faces.
    TopTools_IndexedDataMapOfShapeListOfShape anc_map;
    TopExp::MapShapesAndAncestors(shape, TopAbs_EDGE, TopAbs_FACE, anc_map);
    TopTools_IndexedMapOfShape edge_map;
    TopExp::MapShapes(shape, TopAbs_EDGE, edge_map);

    int free_edges = 0;
    for (int ei = 1; ei <= edge_map.Extent(); ++ei) {
        const TopoDS_Shape& edge = edge_map.FindKey(ei);
        int n_faces = anc_map.Contains(edge) ? anc_map.FindFromKey(edge).Extent() : 0;
        if (n_faces < 2) ++free_edges;
    }
    if (free_edges > 0)
        issues.push_back(json{{"kind", "free_edges"},
                              {"severity", "warning"},
                              {"text", std::to_string(free_edges) +
                                           " free edge(s) detected (open shell)"}});

    // Tiny-edge census (< 0.01 mm).
    constexpr double TINY_EDGE_MM = 0.01;
    int tiny_edges = 0;
    for (int ei = 1; ei <= edge_map.Extent(); ++ei) {
        if (eval_edge_length(TopoDS::Edge(edge_map.FindKey(ei))) < TINY_EDGE_MM) ++tiny_edges;
    }
    if (tiny_edges > 0) {
        char buf[96];
        std::snprintf(buf, sizeof(buf), "%d tiny edge(s) shorter than %g mm", tiny_edges,
                      TINY_EDGE_MM);
        issues.push_back(json{{"kind", "tiny_edges"}, {"severity", "warning"}, {"text", buf}});
    }

    // Sliver-face census (< 0.01 mm^2).
    constexpr double SLIVER_AREA_MM2 = 0.01;
    TopTools_IndexedMapOfShape face_map;
    TopExp::MapShapes(shape, TopAbs_FACE, face_map);
    int sliver_faces = 0;
    for (int fi = 1; fi <= face_map.Extent(); ++fi) {
        GProp_GProps gp;
        BRepGProp::SurfaceProperties(face_map.FindKey(fi), gp);
        if (gp.Mass() < SLIVER_AREA_MM2) ++sliver_faces;
    }
    if (sliver_faces > 0) {
        char buf[96];
        std::snprintf(buf, sizeof(buf), "%d sliver face(s) smaller than %g mm\xc2\xb2",
                      sliver_faces, SLIVER_AREA_MM2);
        issues.push_back(json{{"kind", "sliver_faces"}, {"severity", "warning"}, {"text", buf}});
    }

    return json{{"issues", issues}};
}

// engine/doc/evaluate.py::section_props via worker.py::_eval_section_props. Because the
// Python passes a build123d Plane (whose .wrapped is a gp_Pln), the oracle's
// isinstance(plane, gp_Pln) check is FALSE and cutting_pln resolves to None — so it never
// builds the section face and returns area/moments == 0 with only the section-wire
// (LinearProperties) centroid. We reproduce that exact (degenerate) behavior faithfully.
json doc_eval_section_props(const std::string& doc_id, const json& plane_ref) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    CompileResult r = compile_document(s.doc, env_from_doc(s.doc));
    if (r.shapes.empty())
        throw OpError("invalid_op", "document has no bodies to evaluate", {{"doc_id", doc_id}});
    const TopoDS_Shape& shape = r.shapes.back();  // worker: bodies[-1]
    gp_Pln pln = eval_resolve_plane(plane_ref);

    double cx = 0.0, cy = 0.0, cz = 0.0;
    try {
        BRepAlgoAPI_Section section(shape, pln, Standard_False);
        section.Build();
        if (section.IsDone()) {
            GProp_GProps lin;
            BRepGProp::LinearProperties(section.Shape(), lin);
            gp_Pnt com = lin.CentreOfMass();
            cx = com.X();
            cy = com.Y();
            cz = com.Z();
        }
    } catch (const Standard_Failure&) {
        // matches the oracle's broad except -> zero everything
    }

    return json{
        {"area_mm2", 0.0},
        {"centroid", {cx, cy, cz}},
        {"Ixx", 0.0},
        {"Iyy", 0.0},
        {"Ixy", 0.0},
    };
}

// engine/doc/evaluate.py::symmetry via worker.py::_eval_symmetry. Mirror the active body
// across the plane and measure the symmetric-difference volume. symmetric iff deviation
// <= 1e-3 * max(vol, 1.0). Returns {"symmetric", "deviation_mm3"}.
json doc_eval_symmetry(const std::string& doc_id, int body_id, const json& plane_ref) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    CompileResult r = compile_document(s.doc, env_from_doc(s.doc));
    const TopoDS_Shape& shape = eval_active_shape(r, doc_id, body_id);
    gp_Pln pln = eval_resolve_plane(plane_ref);

    try {
        // Mirror about the plane: SetMirror(gp_Ax2) is planar symmetry. gp_Pln.Position()
        // is a gp_Ax3; build the gp_Ax2 from its location + Z + X (matches _to_gp_plane ->
        // gp_Ax2(location, direction, xdirection)).
        const gp_Ax3& pos = pln.Position();
        gp_Ax2 ax2(pos.Location(), pos.Direction(), pos.XDirection());
        gp_Trsf mirror;
        mirror.SetMirror(ax2);
        BRepBuilderAPI_Transform xf(shape, mirror, Standard_True);
        TopoDS_Shape mirrored = xf.Shape();

        double vol_a = eval_vol_or_zero(shape);
        double vol_mirror = eval_vol_or_zero(mirrored);
        double vol_common = 0.0;
        BRepAlgoAPI_Common common(shape, mirrored);
        common.Build();
        if (common.IsDone()) vol_common = eval_vol_or_zero(common.Shape());

        double deviation = (vol_a - vol_common) + (vol_mirror - vol_common);
        deviation = std::max(0.0, deviation);
        double threshold = 1e-3 * std::max(vol_a, 1.0);
        return json{{"symmetric", deviation <= threshold}, {"deviation_mm3", deviation}};
    } catch (const Standard_Failure& ex) {
        return json{{"symmetric", false}, {"deviation_mm3", -1.0}, {"error", ex.GetMessageString()}};
    }
}

// engine/doc/evaluate.py::interference via worker.py::_eval_interference (part doc branch:
// members = every surviving body). Pairwise overlap (BRepAlgoAPI_Common volume) +
// clearance (BRepExtrema min distance), per-part bbox. ok iff nothing interferes.
// Body names mirror the Python oracle's body_id "b1".."bN" (first-produced = AICM order).
json doc_eval_interference(const std::string& doc_id) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    CompileResult r = compile_document(s.doc, env_from_doc(s.doc));

    const std::size_t n = r.shapes.size();
    std::vector<std::string> names(n);
    for (std::size_t i = 0; i < n; ++i) names[i] = "b" + std::to_string(i + 1);

    constexpr double INTERFERENCE_EPS = 1e-3;

    // Per-part bbox + volume (kernel.py::_bbox_dict uses build123d bounding_box() ->
    // BRepBndLib::AddOptimal, the precise box, after BRepTools::Clean).
    json parts = json::array();
    for (std::size_t i = 0; i < n; ++i) {
        const TopoDS_Shape& shp = r.shapes[i];
        json part = {{"name", names[i]}, {"volume", eval_vol_or_zero(shp)}};
        try {
            BRepTools::Clean(shp);
            Bnd_Box box;
            BRepBndLib::AddOptimal(shp, box);
            if (!box.IsVoid()) {
                double xmn, ymn, zmn, xmx, ymx, zmx;
                box.Get(xmn, ymn, zmn, xmx, ymx, zmx);
                part["bbox"] = json{{"min", {xmn, ymn, zmn}},
                                    {"max", {xmx, ymx, zmx}},
                                    {"size", {xmx - xmn, ymx - ymn, zmx - zmn}}};
            } else {
                part["bbox"] = nullptr;
            }
        } catch (const Standard_Failure&) {
            part["bbox"] = nullptr;
        }
        parts.push_back(std::move(part));
    }

    json interferences = json::array();
    json clearances = json::array();
    for (std::size_t i = 0; i < n; ++i) {
        for (std::size_t j = i + 1; j < n; ++j) {
            // overlap volume (None on boolean failure -> Python warns + continue).
            bool ov_ok = false;
            double ov = 0.0;
            try {
                BRepAlgoAPI_Common common(r.shapes[i], r.shapes[j]);
                common.Build();
                if (common.IsDone()) {
                    ov = eval_vol_or_zero(common.Shape());
                    ov_ok = true;
                }
            } catch (const Standard_Failure&) {
            }
            if (!ov_ok) continue;  // matches the warnings-sink "interference check failed" path
            if (ov > INTERFERENCE_EPS) {
                interferences.push_back(json{{"a", names[i]}, {"b", names[j]}, {"volume", ov}});
            } else {
                try {
                    BRepExtrema_DistShapeShape dss(r.shapes[i], r.shapes[j]);
                    if (dss.IsDone())
                        clearances.push_back(
                            json{{"a", names[i]}, {"b", names[j]}, {"distance", dss.Value()}});
                } catch (const Standard_Failure&) {
                }
            }
        }
    }

    std::string summary;
    if (!interferences.empty()) {
        std::size_t big = 0;
        for (std::size_t k = 1; k < interferences.size(); ++k)
            if (interferences[k]["volume"].get<double>() > interferences[big]["volume"].get<double>())
                big = k;
        char buf[160];
        std::snprintf(buf, sizeof(buf), "%zu interfering pair(s); largest %s<->%s = %.1f mm^3",
                      interferences.size(), interferences[big]["a"].get<std::string>().c_str(),
                      interferences[big]["b"].get<std::string>().c_str(),
                      interferences[big]["volume"].get<double>());
        summary = buf;
    } else if (!clearances.empty()) {
        std::size_t tight = 0;
        for (std::size_t k = 1; k < clearances.size(); ++k)
            if (clearances[k]["distance"].get<double>() < clearances[tight]["distance"].get<double>())
                tight = k;
        char buf[160];
        std::snprintf(buf, sizeof(buf), "No interference. Tightest gap %s<->%s = %.2f mm",
                      clearances[tight]["a"].get<std::string>().c_str(),
                      clearances[tight]["b"].get<std::string>().c_str(),
                      clearances[tight]["distance"].get<double>());
        summary = buf;
    } else {
        summary = "No interference detected.";
    }

    return json{
        {"ok", interferences.empty()},
        {"summary", summary},
        {"parts", parts},
        {"interferences", interferences},
        {"clearances", clearances},
    };
}

// engine/doc/evaluate.py::measure via worker.py::_eval_measure. Resolve 1-2 refs against
// the LAST body, then single-entity or pair measure. Refs resolve in the SAME enumeration
// describe_* + body.faces()/edges()/vertices() use (a TopExp re-walk).
json doc_eval_measure(const std::string& doc_id, const json& refs) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    CompileResult r = compile_document(s.doc, env_from_doc(s.doc));
    if (r.shapes.empty())
        throw OpError("invalid_op", "no bodies for measure", {{"reason", "no bodies"}});
    const TopoDS_Shape& shape = r.shapes.back();  // worker: bodies[-1]

    // bbox diag (build123d bounding_box() = BRepBndLib::AddOptimal), floor 1e-3, 100 on fail.
    double diag = 100.0;
    try {
        BRepTools::Clean(shape);
        Bnd_Box box;
        BRepBndLib::AddOptimal(shape, box);
        if (!box.IsVoid()) {
            double xmn, ymn, zmn, xmx, ymx, zmx;
            box.Get(xmn, ymn, zmn, xmx, ymx, zmx);
            double dx = xmx - xmn, dy = ymx - ymn, dz = zmx - zmn;
            diag = std::max(std::sqrt(dx * dx + dy * dy + dz * dz), 1e-3);
        }
    } catch (const Standard_Failure&) {
    }

    if (!refs.is_array() || refs.empty() || refs.size() > 2)
        throw OpError("invalid_op",
                      "measure() takes 1 or 2 refs, got " +
                          std::to_string(refs.is_array() ? refs.size() : 0));

    std::vector<TopoDS_Shape> shapes;
    for (const auto& ref : refs) shapes.push_back(eval_resolve_ref(ref, shape, diag));

    if (shapes.size() == 1) return eval_measure_single(shapes[0]);
    return eval_measure_pair(shapes[0], shapes[1]);
}

// ----------------------------------------------------------- Evaluate field methods
// Ports of engine/doc/evaluate.py {draft_field, curvature_field, thickness_field} via the
// worker.py _eval_draft/_eval_curvature/_eval_thickness handlers. Unlike the analytic eval
// methods these tessellate the active body, compute a per-vertex SCALAR field, write it to a
// temp .aicm file (a SCLR chunk on the tessellated body) and return {"aicm_path": <path>}.
// Mirrors worker.py::_eval_write_field_aicm: tessellate (body_id=1) -> chunk_fn(tess,shape)
// -> write_aicm_file(tmp, [tess], extra_chunks=[chunk]).

namespace {

// Pick the active (or selected) body shape (mirrors eval_active_shape, but the field
// handlers always go through _eval_active_shape with the same body_id semantics).
const TopoDS_Shape& field_active_shape(const CompileResult& r, const std::string& doc_id,
                                       int body_id) {
    return eval_active_shape(r, doc_id, body_id);
}

// Tessellate `shape` (body_id=1, name `name`, default deflections matching tessellate.py)
// and return the AicmBody — the C++ equivalent of worker.py's tess.tessellate_body call.
aicm::AicmBody field_tessellate(const TopoDS_Shape& shape, const std::string& name) {
    const float color[3] = {0.71f, 0.76f, 0.82f};
    return ::geom::tessellate_body(shape, 1, name.empty() ? std::string("Body1") : name, color);
}

// Write the tessellated body + a single SCLR chunk to a fresh temp .aicm path; return it.
std::string field_write_temp_aicm(const aicm::AicmBody& body,
                                  const std::vector<uint8_t>& chunk) {
    namespace fs = std::filesystem;
    static std::atomic<unsigned long long> seq{1};
    unsigned long long n = seq.fetch_add(1);
    char buf[64];
    std::snprintf(buf, sizeof(buf), "aicad-field-%llx-%llx.aicm",
                  static_cast<unsigned long long>(
                      reinterpret_cast<std::uintptr_t>(&body) & 0xffffffffULL),
                  n);
    fs::path tmp = fs::temp_directory_path() / buf;
    if (!aicm::write_aicm_file(tmp.string(), {body}, {chunk}))
        throw OpError("invalid_op", "failed to write field aicm to temp",
                      {{"reason", "aicm write failed"}});
    return tmp.string();
}

// Per-vertex unit normal from the body's flat nrm buffer (3*i..3*i+2), normalized like the
// oracle (nrm / max(|nrm|, eps)). vi is 0-based.
inline void field_vertex_normal(const aicm::AicmBody& body, std::size_t vi, double out[3]) {
    double x = body.nrm[3 * vi + 0];
    double y = body.nrm[3 * vi + 1];
    double z = body.nrm[3 * vi + 2];
    double len = std::sqrt(x * x + y * y + z * z);
    double inv = (len > 1e-12) ? 1.0 / len : 1.0;
    out[0] = x * inv;
    out[1] = y * inv;
    out[2] = z * inv;
}

// engine/doc/evaluate.py::_moller_trumbore_all — for each ray, the smallest positive t
// (distance along a UNIT direction) to a triangle of the mesh; inf if no hit. Ported 1:1
// (same EPS=1e-10, u/v barycentric bounds, t>EPS guard). directions are unit vectors.
std::vector<double> field_moller_trumbore_all(const std::vector<float>& pos,
                                              const std::vector<uint32_t>& idx,
                                              const std::vector<std::array<double, 3>>& origins,
                                              const std::vector<std::array<double, 3>>& dirs) {
    constexpr double EPS = 1e-10;
    const std::size_t n_rays = origins.size();
    const std::size_t n_tris = idx.size() / 3;
    std::vector<double> dist(n_rays, std::numeric_limits<double>::infinity());

    auto vtx = [&](uint32_t i, double out[3]) {
        out[0] = pos[3 * i + 0];
        out[1] = pos[3 * i + 1];
        out[2] = pos[3 * i + 2];
    };
    auto cross = [](const double a[3], const double b[3], double o[3]) {
        o[0] = a[1] * b[2] - a[2] * b[1];
        o[1] = a[2] * b[0] - a[0] * b[2];
        o[2] = a[0] * b[1] - a[1] * b[0];
    };
    auto dot = [](const double a[3], const double b[3]) {
        return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
    };

    // Precompute triangle edges (matches the oracle's V0/E1/E2 layout).
    std::vector<std::array<double, 3>> V0(n_tris), E1(n_tris), E2(n_tris);
    for (std::size_t t = 0; t < n_tris; ++t) {
        double v0[3], v1[3], v2[3];
        vtx(idx[3 * t + 0], v0);
        vtx(idx[3 * t + 1], v1);
        vtx(idx[3 * t + 2], v2);
        V0[t] = {v0[0], v0[1], v0[2]};
        E1[t] = {v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2]};
        E2[t] = {v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2]};
    }

    for (std::size_t ri = 0; ri < n_rays; ++ri) {
        const double d[3] = {dirs[ri][0], dirs[ri][1], dirs[ri][2]};
        const double o[3] = {origins[ri][0], origins[ri][1], origins[ri][2]};
        double best = std::numeric_limits<double>::infinity();
        for (std::size_t t = 0; t < n_tris; ++t) {
            const double e1[3] = {E1[t][0], E1[t][1], E1[t][2]};
            const double e2[3] = {E2[t][0], E2[t][1], E2[t][2]};
            double h[3];
            cross(d, e2, h);
            double a = dot(e1, h);
            if (std::abs(a) <= EPS) continue;
            double inv_a = 1.0 / a;
            const double s[3] = {o[0] - V0[t][0], o[1] - V0[t][1], o[2] - V0[t][2]};
            double u = inv_a * dot(s, h);
            if (u < 0.0 || u > 1.0) continue;
            double q[3];
            cross(s, e1, q);
            double v = inv_a * dot(q, d);
            if (v < 0.0 || u + v > 1.0) continue;
            double tval = inv_a * dot(e2, q);
            if (tval > EPS && tval < best) best = tval;
        }
        dist[ri] = best;
    }
    return dist;
}

}  // namespace

// engine/doc/evaluate.py::draft_field. Per-vertex draft angle = degrees(arcsin(clip(n·pull,
// -1,1))) for the normalized vertex normal n and normalized pull. chunk name "draft".
json doc_eval_draft(const std::string& doc_id, int body_id, const std::array<double, 3>& pull_in,
                    double /*angle_deg*/) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    CompileResult r = compile_document(s.doc, env_from_doc(s.doc));
    const TopoDS_Shape& shape = field_active_shape(r, doc_id, body_id);
    const std::string name = (body_id >= 1 && body_id <= static_cast<int>(r.bodies_info.size()))
                                 ? r.bodies_info[static_cast<std::size_t>(body_id - 1)].name
                             : (!r.bodies_info.empty() ? r.bodies_info.back().name : std::string());
    aicm::AicmBody body = field_tessellate(shape, name);

    double pull[3] = {pull_in[0], pull_in[1], pull_in[2]};
    double plen = std::sqrt(pull[0] * pull[0] + pull[1] * pull[1] + pull[2] * pull[2]);
    double pinv = 1.0 / (plen + 1e-12);  // oracle: pull / (norm + 1e-12)
    pull[0] *= pinv;
    pull[1] *= pinv;
    pull[2] *= pinv;

    const std::size_t n = body.n_verts();
    std::vector<float> values(n, 0.0f);
    float vmin = std::numeric_limits<float>::infinity();
    float vmax = -std::numeric_limits<float>::infinity();
    bool any_finite = false;
    for (std::size_t i = 0; i < n; ++i) {
        double nrm[3];
        field_vertex_normal(body, i, nrm);
        double cos_t = nrm[0] * pull[0] + nrm[1] * pull[1] + nrm[2] * pull[2];
        if (cos_t < -1.0) cos_t = -1.0;
        if (cos_t > 1.0) cos_t = 1.0;
        float draft = static_cast<float>(std::asin(cos_t) * (180.0 / 3.14159265358979323846));
        values[i] = draft;
        if (std::isfinite(draft)) {
            any_finite = true;
            vmin = std::min(vmin, draft);
            vmax = std::max(vmax, draft);
        }
    }
    if (!any_finite) {
        vmin = -90.0f;
        vmax = 90.0f;
    }

    auto chunk = aicm::scalar_chunk(body.body_id, "draft", values, vmin, vmax);
    return json{{"aicm_path", field_write_temp_aicm(body, chunk)}};
}

// engine/doc/evaluate.py::curvature_field. Re-mesh (BRepMesh 0.1) to match the oracle's face
// ordering, then per-vertex principal curvatures via BRepLProp_SLProps at the vertex's UV
// (projected via ShapeAnalysis_Surface::ValueOfUV). kind: mean=(k1+k2)/2, gaussian=k1*k2,
// max=max(|k1|,|k2|). chunk name "curvature_<kind>". Verts with no defined curvature stay NaN.
json doc_eval_curvature(const std::string& doc_id, int body_id, const std::string& kind) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    CompileResult r = compile_document(s.doc, env_from_doc(s.doc));
    const TopoDS_Shape& shape = field_active_shape(r, doc_id, body_id);
    const std::string name = (body_id >= 1 && body_id <= static_cast<int>(r.bodies_info.size()))
                                 ? r.bodies_info[static_cast<std::size_t>(body_id - 1)].name
                             : (!r.bodies_info.empty() ? r.bodies_info.back().name : std::string());
    aicm::AicmBody body = field_tessellate(shape, name);

    const std::size_t n_verts = body.n_verts();
    const float NaNf = std::numeric_limits<float>::quiet_NaN();
    std::vector<float> values(n_verts, NaNf);

    // Re-mesh + build the same 1-based face map tessellate_body used (the oracle remeshes
    // raw at (0.1,False,0.5,True) before mapping faces — FaceRec.face_id is this 1-based idx).
    BRepMesh_IncrementalMesh(shape, 0.1, Standard_False, 0.5, Standard_True).Perform();
    TopTools_IndexedMapOfShape face_map;
    TopExp::MapShapes(shape, TopAbs_FACE, face_map);

    for (const aicm::FaceRec& fr : body.faces) {
        const int fi = static_cast<int>(fr.face_id);  // 1-based into face_map
        if (fi < 1 || fi > face_map.Extent() || fr.tri_count == 0) continue;
        TopoDS_Face face = TopoDS::Face(face_map.FindKey(fi));
        BRepAdaptor_Surface sa(face);

        // Vertex set of this face = unique indices over its triangle range in the flat idx
        // buffer ([first_tri*3 .. (first_tri+tri_count)*3)). Matches the oracle's
        // np.unique(tess.idx[tri_start:tri_end]).
        std::size_t lo = static_cast<std::size_t>(fr.first_tri) * 3;
        std::size_t hi = static_cast<std::size_t>(fr.first_tri + fr.tri_count) * 3;
        if (hi > body.idx.size()) hi = body.idx.size();

        TopLoc_Location loc;
        Handle(Geom_Surface) occ_surf = BRep_Tool::Surface(face, loc);
        if (occ_surf.IsNull()) continue;
        ShapeAnalysis_Surface sas(occ_surf);

        double u_first = sa.FirstUParameter(), u_last = sa.LastUParameter();
        double v_first = sa.FirstVParameter(), v_last = sa.LastVParameter();

        std::set<uint32_t> seen;
        for (std::size_t k = lo; k < hi; ++k) {
            uint32_t vi = body.idx[k];
            if (vi >= n_verts || !seen.insert(vi).second) continue;
            gp_Pnt p3d(body.pos[3 * vi + 0], body.pos[3 * vi + 1], body.pos[3 * vi + 2]);
            gp_Pnt2d uv = sas.ValueOfUV(p3d, 1e-3);
            double u = std::max(u_first, std::min(u_last, uv.X()));
            double v = std::max(v_first, std::min(v_last, uv.Y()));
            try {
                BRepLProp_SLProps sl(sa, u, v, 2, 1e-7);
                if (sl.IsCurvatureDefined()) {
                    double k1 = sl.MinCurvature();
                    double k2 = sl.MaxCurvature();
                    double val;
                    if (kind == "gaussian")
                        val = k1 * k2;
                    else if (kind == "max")
                        val = std::max(std::abs(k1), std::abs(k2));
                    else
                        val = (k1 + k2) / 2.0;
                    values[vi] = static_cast<float>(val);
                }
            } catch (const Standard_Failure&) {
            }
        }
    }

    float vmin = 0.0f, vmax = 0.0f;
    bool any = false;
    for (float v : values) {
        if (std::isfinite(v)) {
            if (!any) {
                vmin = vmax = v;
                any = true;
            } else {
                vmin = std::min(vmin, v);
                vmax = std::max(vmax, v);
            }
        }
    }

    auto chunk = aicm::scalar_chunk(body.body_id, "curvature_" + kind, values, vmin, vmax);
    return json{{"aicm_path", field_write_temp_aicm(body, chunk)}};
}

// engine/doc/evaluate.py::thickness_field. Per-vertex inward-raycast wall thickness: from each
// vertex offset 1e-3 along -normal, cast -normal and take the first re-intersection distance
// (minus the offset). The oracle's primary path is trimesh BVH; we port the deterministic
// Moller-Trumbore FALLBACK (_moller_trumbore_all). chunk name "thickness". NOTE: parity here is
// PHYSICAL (a tolerance band), not bit-exact — the oracle's BVH runs on a divergent mesh.
json doc_eval_thickness(const std::string& doc_id, int body_id) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    CompileResult r = compile_document(s.doc, env_from_doc(s.doc));
    const TopoDS_Shape& shape = field_active_shape(r, doc_id, body_id);
    const std::string name = (body_id >= 1 && body_id <= static_cast<int>(r.bodies_info.size()))
                                 ? r.bodies_info[static_cast<std::size_t>(body_id - 1)].name
                             : (!r.bodies_info.empty() ? r.bodies_info.back().name : std::string());
    aicm::AicmBody body = field_tessellate(shape, name);

    const std::size_t n = body.n_verts();
    const float NaNf = std::numeric_limits<float>::quiet_NaN();
    std::vector<float> values(n, NaNf);

    constexpr double OFFSET = 1e-3;
    std::vector<std::array<double, 3>> origins(n), dirs(n);
    for (std::size_t i = 0; i < n; ++i) {
        double nrm[3];
        field_vertex_normal(body, i, nrm);
        origins[i] = {body.pos[3 * i + 0] - nrm[0] * OFFSET,
                      body.pos[3 * i + 1] - nrm[1] * OFFSET,
                      body.pos[3 * i + 2] - nrm[2] * OFFSET};
        dirs[i] = {-nrm[0], -nrm[1], -nrm[2]};  // inward (unit, since normal is unit)
    }

    std::vector<double> hits = field_moller_trumbore_all(body.pos, body.idx, origins, dirs);

    float vmin = 0.0f, vmax = 0.0f;
    bool any = false;
    for (std::size_t i = 0; i < n; ++i) {
        double d = hits[i];
        if (std::isfinite(d) && d > OFFSET) {
            float t = static_cast<float>(d - OFFSET);
            values[i] = t;
            if (!any) {
                vmin = vmax = t;
                any = true;
            } else {
                vmin = std::min(vmin, t);
                vmax = std::max(vmax, t);
            }
        }
    }

    auto chunk = aicm::scalar_chunk(body.body_id, "thickness", values, vmin, vmax);
    return json{{"aicm_path", field_write_temp_aicm(body, chunk)}};
}

json doc_push_pull(const std::string& doc_id, int body_id, int topo_id, double distance,
                   bool commit) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    // plan_push_pull compiles + resolves the face owner (throws OpError on a bad
    // body_id/topo_id), never mutating the doc.
    PushPullPlan plan = plan_push_pull(s.doc, env_from_doc(s.doc), body_id, topo_id, distance);

    // The draggable plan fields (axis is JSON null when not axis-aligned).
    auto add_plan_fields = [&](json& out) {
        out["feature_id"] = plan.owner_fid;
        out["feature_type"] = plan.ftype;
        out["param"] = plan.param;
        out["axis"] = plan.has_axis ? json(plan.axis) : json(nullptr);
        out["old_value"] = plan.old_value;
        out["new_value"] = plan.new_value;
        out["distance"] = distance;
        out["clamped"] = plan.clamped;
    };
    auto merge = [](json& out, const json& resp) {
        for (auto it = resp.begin(); it != resp.end(); ++it) out[it.key()] = it.value();
    };

    if (!plan.draggable) {
        json out = {{"ok", true}, {"draggable", false}, {"reason", plan.reason}};
        if (!plan.owner_fid.empty()) out["feature_id"] = plan.owner_fid;
        if (!plan.ftype.empty()) out["feature_type"] = plan.ftype;
        return out;
    }

    // A zero-distance drag is a no-op preview: report the plan + a fresh rebuild, no
    // mutation / no revision bump.
    if (std::abs(plan.new_value - plan.old_value) < 1e-12) {
        CompileResult r = compile_document(s.doc, env_from_doc(s.doc));
        json out = {{"ok", true}, {"draggable", true}};
        add_plan_fields(out);
        merge(out, ops_response(s, r, json::object(), /*with_tess=*/true));
        return out;
    }

    // Apply the param delta on a working copy (transactional — a throw leaves the
    // session untouched). Keep a dim object's shape; clear any stale expr.
    const std::string pre_snapshot = s.doc.dump();
    json working = s.doc;
    json* nf = find_feature(working, plan.owner_fid);
    if (!nf) {  // owner came from the owner map, so this should not happen
        json out = {{"ok", true}, {"draggable", false},
                    {"reason", "owner feature '" + plan.owner_fid + "' vanished before edit"},
                    {"feature_id", plan.owner_fid}, {"feature_type", plan.ftype}};
        return out;
    }
    if (!nf->contains("params") || !(*nf)["params"].is_object()) (*nf)["params"] = json::object();
    json& p = (*nf)["params"];
    if (p.contains(plan.param) && p[plan.param].is_object()) {
        p[plan.param]["value"] = plan.new_value;
        p[plan.param]["expr"] = nullptr;
    } else {
        p[plan.param] = {{"value", plan.new_value}, {"expr", nullptr}};
    }

    // Commit pushes ONE undo entry of the pre-edit doc; a transient preview pushes none.
    if (commit) {
        s.undo_stack.push_back(pre_snapshot);
        if (s.undo_stack.size() > kUndoLimit) s.undo_stack.erase(s.undo_stack.begin());
        s.redo_stack.clear();
    }
    s.doc = std::move(working);
    s.revision += 1;
    s.dirty = true;

    CompileResult r = compile_document(s.doc, env_from_doc(s.doc));

    // A value that collapses the body (no surviving solid) rolls back fully — the body
    // stays at its last valid size (instant3d.py _result_is_degenerate / _rollback_edit).
    if (r.body_count == 0) {
        s.doc = json::parse(pre_snapshot);
        s.revision += 1;  // the rollback is itself a revision step (Python contract)
        if (commit && !s.undo_stack.empty() && s.undo_stack.back() == pre_snapshot)
            s.undo_stack.pop_back();
        CompileResult rb = compile_document(s.doc, env_from_doc(s.doc));
        json out = {{"ok", true}, {"draggable", false},
                    {"reason", "the new value removes all material (the body would vanish) — "
                               "drag limited to the last valid size"},
                    {"feature_id", plan.owner_fid}, {"feature_type", plan.ftype}};
        merge(out, ops_response(s, rb, json::object(), /*with_tess=*/true));
        return out;
    }

    json out = {{"ok", true}, {"draggable", true}};
    add_plan_fields(out);
    out["committed"] = commit;
    merge(out, ops_response(s, r, json::object(), /*with_tess=*/true));
    return out;
}

json doc_export_file(const std::string& doc_id, const std::string& path, const std::string& fmt) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    if (path.empty())
        throw OpError("invalid_op", "export_file: 'path' is required", {{"reason", "missing path"}});
    if (fmt.empty())
        throw OpError("invalid_op", "export_file: 'format' is required", {{"reason", "missing format"}});
    CompileResult r = compile_document(s.doc, env_from_doc(s.doc));
    if (r.shapes.empty())
        throw OpError("export_failed", "export_file: document has no bodies to export",
                      {{"doc_id", doc_id}});
    std::string out = export_bodies(r.shapes, path, fmt);  // throws export_failed on a writer error
    return json{{"path", out}};
}

json doc_import_file(const std::string& path, const std::string& into, bool as_solid,
                     const std::string& fmt) {
    std::lock_guard<std::mutex> lk(g_mtx);
    if (path.empty())
        throw OpError("invalid_op", "import_file: 'path' is required", {{"reason", "missing path"}});
    // Resolve the format up front so a bad extension errors before we touch a session.
    const std::string f = fmt.empty() ? detect_format(path) : fmt;
    const std::string stem = std::filesystem::path(path).stem().string();

    json params = {{"source_path", path}, {"format", f}, {"original_name", stem}};
    if (as_solid) params["as_solid"] = true;
    json ops = json::array({json{
        {"op", "add_feature"},
        {"feature", {{"type", "import"}, {"name", stem}, {"params", params}, {"refs", json::object()}}}}});

    Session* s = nullptr;
    if (!into.empty())
        s = &require_session(into);  // append to an existing C++ session
    else
        s = &new_part_session_locked(stem.empty() ? std::string("Imported") : stem);

    json resp = apply_ops_locked(*s, -1, ops, /*coalesce=*/false);
    resp["doc_id"] = s->doc_id;
    return resp;
}

json mesh_read_raw(const std::string& path, const std::string& fmt) {
    if (path.empty())
        throw OpError("invalid_op", "mesh_read_raw: 'path' is required",
                      {{"reason", "missing path"}});
    if (!std::filesystem::exists(path))
        throw OpError("import_failed", "mesh_read_raw: file not found: " + path, {{"path", path}});
    const std::string f = fmt.empty() ? detect_format(path) : fmt;
    json objects = json::array();
    for (const auto& [name, m] : read_mesh_raw(path, f)) {
        json o;
        o["name"] = name;
        o["verts"] = m.verts;  // 3*N doubles (float32 source widened — exact)
        o["tris"] = m.tris;    // 3*M uint32 (0-based)
        objects.push_back(std::move(o));
    }
    return json{{"objects", std::move(objects)}, {"format", f}};
}

json doc_save(const std::string& doc_id, const std::string& path) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    const std::string target = !path.empty() ? path : s.path;
    if (target.empty())
        throw OpError("io_error",
                      "doc_save: no path given and document has no current path",
                      {{"reason", "missing path"}});
    std::string out = stepplus_save(s.doc, target, s.extra_entries);
    s.path = out;
    s.dirty = false;
    return json{{"path", out}};
}

json doc_open(const std::string& path) {
    std::lock_guard<std::mutex> lk(g_mtx);
    StepPlusDoc loaded = stepplus_open(path);  // throws OpError on a bad file

    // Only PART documents are handled by the C++ core. A non-part container (e.g. an
    // assembly) is rejected BEFORE a session is created — code "unsupported" — so the
    // host's minting-call fallback can route the open to the Python worker instead (no
    // C++ doc exists yet, so the fallback is clean / not a mid-doc switch).
    const std::string schema =
        loaded.doc.is_object() ? loaded.doc.value("schema", std::string()) : std::string();
    if (!schema.empty() && schema != "aicad.part/1")
        throw OpError("unsupported",
                      "doc_open: the C++ core handles only part documents (got schema '" +
                          schema + "')",
                      {{"schema", schema}, {"reason", "unsupported document kind"}});

    Session s;
    s.doc_id = new_doc_id();
    s.doc = std::move(loaded.doc);
    s.revision = 0;
    s.path = path;
    s.extra_entries = std::move(loaded.extra);
    // Stamp the document's own id to the new session id (mirrors blank_part).
    if (s.doc.is_object()) s.doc["id"] = s.doc_id;
    Session& stored = g_sessions[s.doc_id] = std::move(s);

    CompileResult r = compile_document(stored.doc, env_from_doc(stored.doc));
    json resp = ops_response(stored, r, json::object(), /*with_tess=*/true);
    resp["doc_id"] = stored.doc_id;

    // Warnings: container load notes + per-feature rebuild failures (the non-total-
    // failure branch of the Python oracle; frozen-geometry fallback is deferred).
    json warnings = json::array();
    for (const std::string& w : loaded.warnings) warnings.push_back(w);
    std::vector<std::string> failed;
    for (const FeatureStatus& st : r.statuses)
        if (!st.ok) failed.push_back(st.id);
    if (!failed.empty()) {
        std::string msg = std::to_string(failed.size()) + " feature(s) failed to rebuild: [";
        for (std::size_t i = 0; i < failed.size(); ++i) {
            if (i) msg += ", ";
            msg += "'" + failed[i] + "'";
        }
        msg += "]";
        warnings.push_back(msg);
    }
    resp["warnings"] = warnings;
    return resp;
}

// ============================================================ C18 sketch RPCs
// Port of engine/doc/session.py sketch_enter/solve/drag/drag_commit/exit/overview/
// regions/probe/project_ref. doc_id-keyed; operate on the per-session active_sketch.

json doc_sketch_enter(const std::string& doc_id, const std::string& sketch_fid) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    json& feat = find_sketch_feature(s, sketch_fid);
    aicad::geom::SketchPlane plane = resolve_sketch_plane(s, feat);
    std::vector<double> frame = plane_frame(plane);

    SolveActive packed = solve_active(s, feat, std::nullopt);
    SketchState st;
    st.sketch_fid = sketch_fid;
    st.solve_state = packed.state;
    st.last_entities = packed.entity_map;
    st.last_statuses = packed.statuses;
    st.last_overall_status = overall_status(packed.statuses, packed.dof, packed.conflicting,
                                            packed.redundant, packed.constraints_by_id);
    st.plane_frame = frame;
    s.active_sketch = std::move(st);

    return sketch_result(feat, packed, frame, s.active_sketch->last_overall_status);
}

json doc_sketch_solve(const std::string& doc_id, const std::string& sketch_fid) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    json& feat = find_sketch_feature(s, sketch_fid);
    std::optional<aicad::sketch::SolveState> warm;
    if (s.active_sketch && s.active_sketch->sketch_fid == sketch_fid)
        warm = s.active_sketch->solve_state;
    SolveActive packed = solve_active(s, feat, warm);
    if (!s.active_sketch || s.active_sketch->sketch_fid != sketch_fid) {
        aicad::geom::SketchPlane plane = resolve_sketch_plane(s, feat);
        SketchState st;
        st.sketch_fid = sketch_fid;
        st.plane_frame = plane_frame(plane);
        s.active_sketch = std::move(st);
    }
    s.active_sketch->solve_state = packed.state;
    s.active_sketch->last_entities = packed.entity_map;
    s.active_sketch->last_statuses = packed.statuses;
    s.active_sketch->last_overall_status =
        overall_status(packed.statuses, packed.dof, packed.conflicting, packed.redundant,
                       packed.constraints_by_id);
    return sketch_result(feat, packed, s.active_sketch->plane_frame,
                         s.active_sketch->last_overall_status);
}

json doc_sketch_overview(const std::string& doc_id, const std::string& sketch_fid) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    json& feat = find_sketch_feature(s, sketch_fid);
    aicad::geom::SketchPlane plane = resolve_sketch_plane(s, feat);
    SolveActive packed = solve_active(s, feat, std::nullopt);  // never touches active_sketch
    json r;
    r["entities"] = packed.entities;
    r["statuses"] = packed.statuses;
    r["dof"] = packed.dof;
    r["plane_frame"] = plane_frame(plane);
    r["status"] = overall_status(packed.statuses, packed.dof, packed.conflicting,
                                 packed.redundant, packed.constraints_by_id);
    r["skipped_constraints"] = strvec_json(packed.skipped_constraints);
    return r;
}

json doc_sketch_drag(const std::string& doc_id, const std::string& sketch_fid,
                     const std::string& handle, const json& xy, const json& targets) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    json& feat = find_sketch_feature(s, sketch_fid);
    // Not entered (or stale / cold) — enter implicitly so drag is still serviceable.
    if (!s.active_sketch || s.active_sketch->sketch_fid != sketch_fid ||
        !s.active_sketch->solve_state) {
        aicad::geom::SketchPlane plane = resolve_sketch_plane(s, feat);
        std::vector<double> frame = plane_frame(plane);
        SolveActive packed = solve_active(s, feat, std::nullopt);
        SketchState st;
        st.sketch_fid = sketch_fid;
        st.solve_state = packed.state;
        st.last_entities = packed.entity_map;
        st.last_statuses = packed.statuses;
        st.last_overall_status = overall_status(packed.statuses, packed.dof, packed.conflicting,
                                                packed.redundant, packed.constraints_by_id);
        st.plane_frame = frame;
        s.active_sketch = std::move(st);
    }
    if (!s.active_sketch->solve_state)
        throw OpError("invalid_op", "sketch could not be solved for drag",
                      {{"reason", "no solve state"}});

    json env = env_from_doc(s.doc);
    json params = sketch_params(feat);
    json sk;
    sk["entities"] = params.contains("entities") ? params["entities"] : json::array();
    sk["constraints"] = params.contains("constraints") ? params["constraints"] : json::array();

    std::pair<double, double> target_xy{0.0, 0.0};
    if (xy.is_array() && xy.size() == 2)
        target_xy = {xy[0].get<double>(), xy[1].get<double>()};
    json res = aicad::sketch::drag(*s.active_sketch->solve_state, handle, target_xy, sk, env,
                                   targets.is_null() ? json() : targets);

    if (res.is_object() && res.contains("ok") && !res["ok"].get<bool>()) {
        json err;
        err["ok"] = false;
        err["error"] = res.contains("error") && !res["error"].is_null()
                           ? res["error"] : json("drag target did not resolve");
        err["rejected_handles"] = res.contains("rejected_handles") ? res["rejected_handles"]
                                                                    : json::array();
        return err;
    }

    const json& res_entities = res["entities"];
    auto construction_of = [&](const std::string& eid) -> bool {
        if (params.contains("entities") && params["entities"].is_array())
            for (const json& e : params["entities"])
                if (e.is_object() && e.value("id", std::string()) == eid)
                    return e.value("construction", false);
        return false;
    };
    json entities = json::array();
    for (auto it = res_entities.begin(); it != res_entities.end(); ++it) {
        json rec = it.value();
        rec["id"] = it.key();
        rec["construction"] = construction_of(it.key());
        entities.push_back(std::move(rec));
    }
    // Remember dragged coords so commit can persist them (transient; no warm churn).
    s.active_sketch->last_entities = res_entities;
    s.active_sketch->last_statuses = res.contains("statuses") ? res["statuses"] : json::object();

    json out;
    out["entities"] = entities;
    out["statuses"] = res.contains("statuses") ? res["statuses"] : json::object();
    out["solve_rejected"] = false;
    out["constraints"] = sketch_constraints(feat, res_entities);
    return out;
}

json doc_sketch_drag_commit(const std::string& doc_id, const std::string& sketch_fid) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    json& feat = find_sketch_feature(s, sketch_fid);

    json coords;
    if (s.active_sketch && s.active_sketch->sketch_fid == sketch_fid &&
        s.active_sketch->last_entities.is_object() && !s.active_sketch->last_entities.empty())
        coords = s.active_sketch->last_entities;
    else {
        // Nothing dragged — re-solve and persist solved positions (idempotent commit).
        std::optional<aicad::sketch::SolveState> warm =
            s.active_sketch ? s.active_sketch->solve_state : std::nullopt;
        SolveActive packed = solve_active(s, feat, warm);
        coords = packed.entity_map;
    }

    // Build one sketch_edit_entity op per entity (patch minus "type").
    json ops = json::array();
    for (auto it = coords.begin(); it != coords.end(); ++it) {
        json patch = json::object();
        for (auto kit = it.value().begin(); kit != it.value().end(); ++kit)
            if (kit.key() != "type") patch[kit.key()] = kit.value();
        ops.push_back({{"op", "sketch_edit_entity"}, {"sketch", sketch_fid},
                       {"id", it.key()}, {"params", patch}});
    }

    json resp = apply_ops_locked(s, -1, ops, /*coalesce=*/false);

    // Re-solve so warm state matches the freshly persisted doc + refresh constraints.
    if (s.active_sketch && s.active_sketch->sketch_fid == sketch_fid) {
        try {
            json& feat2 = find_sketch_feature(s, sketch_fid);
            std::optional<aicad::sketch::SolveState> warm = s.active_sketch->solve_state;
            SolveActive packed = solve_active(s, feat2, warm);
            s.active_sketch->solve_state = packed.state;
            s.active_sketch->last_entities = packed.entity_map;
            s.active_sketch->last_statuses = packed.statuses;
            s.active_sketch->last_overall_status =
                overall_status(packed.statuses, packed.dof, packed.conflicting,
                               packed.redundant, packed.constraints_by_id);
            // Refresh the doc_status.active_sketch now the status is up to date.
            if (resp.contains("rebuild") && resp["rebuild"].is_object())
                resp["rebuild"]["doc_status"]["active_sketch"] =
                    s.active_sketch->last_overall_status;
            resp["constraints"] = sketch_constraints(feat2, packed.entity_map);
        } catch (...) { /* fall through to the fallback below */ }
    }
    if (!resp.contains("constraints")) {
        json& feat2 = find_sketch_feature(s, sketch_fid);
        json ec = s.active_sketch ? s.active_sketch->last_entities : json::object();
        resp["constraints"] = sketch_constraints(feat2, ec);
    }
    return resp;
}

json doc_sketch_exit(const std::string& doc_id, const std::string& sketch_fid) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    json& feat = find_sketch_feature(s, sketch_fid);

    if (s.active_sketch && s.active_sketch->sketch_fid == sketch_fid &&
        s.active_sketch->last_entities.is_object() && !s.active_sketch->last_entities.empty()) {
        const json& coords = s.active_sketch->last_entities;
        // Persist last-solved coords IN PLACE; bump revision only if anything changed.
        push_undo(s);
        bool changed = false;
        // Write back into the feature's entities array (canonical params or flat).
        json* entities_ptr = nullptr;
        if (feat.contains("params") && feat["params"].is_object() &&
            feat["params"].contains("entities") && feat["params"]["entities"].is_array())
            entities_ptr = &feat["params"]["entities"];
        else if (feat.contains("entities") && feat["entities"].is_array())
            entities_ptr = &feat["entities"];
        if (entities_ptr)
            for (json& ent : *entities_ptr) {
                if (!ent.is_object() || !ent.contains("id")) continue;
                std::string eid = ent["id"].get<std::string>();
                if (!coords.contains(eid)) continue;
                const json& c = coords[eid];
                for (auto kit = c.begin(); kit != c.end(); ++kit) {
                    if (kit.key() == "type") continue;
                    if (!ent.contains(kit.key()) || ent[kit.key()] != kit.value()) {
                        ent[kit.key()] = kit.value();
                        changed = true;
                    }
                }
            }
        if (changed) {
            s.revision += 1;
            s.dirty = true;
        } else {
            s.undo_stack.pop_back();  // nothing changed — no phantom undo step
        }
    }
    s.active_sketch.reset();
    CompileResult r = compile_document(s.doc, env_from_doc(s.doc));
    return ops_response(s, r, json::object(), /*with_tess=*/true);
}

namespace {

// ---- doc_sketch_regions support (port of sketch.py compute_regions descriptors) ----
// Loop detection mirrors geom/profile.cpp; here we also build region DESCRIPTORS
// (centroid/area from the OCCT face surface properties, outer/holes polylines, depth/
// is_material) and return EVERY loop as a region (material + void), sorted by centroid.

struct RegEdge {
    TopoDS_Edge edge;
    Eigen::Vector2d p_start, p_end;
    bool closed = false;
    // densely sampled 2D polyline of the edge (for outer/holes overlay), start..end.
    std::vector<Eigen::Vector2d> samples;
};

constexpr double kRegEps = 1e-6;

bool reg_near(const Eigen::Vector2d& a, const Eigen::Vector2d& b) {
    return std::abs(a.x() - b.x()) < kRegEps && std::abs(a.y() - b.y()) < kRegEps;
}

gp_Pnt reg_lift(const aicad::geom::SketchPlane& pl, const Eigen::Vector2d& uv) {
    Eigen::Vector3d w = pl.lift(uv);
    return gp_Pnt(w.x(), w.y(), w.z());
}

bool reg_build_edge(const json& ent, const aicad::geom::SketchPlane& pl, RegEdge& out) {
    if (!ent.is_object() || !ent.contains("type")) return false;
    if (ent.value("construction", false)) return false;
    const std::string t = ent["type"];
    try {
        if (t == "line") {
            Eigen::Vector2d a(ent["p1"][0], ent["p1"][1]), b(ent["p2"][0], ent["p2"][1]);
            if (reg_near(a, b)) return false;
            BRepBuilderAPI_MakeEdge mk(reg_lift(pl, a), reg_lift(pl, b));
            if (!mk.IsDone()) return false;
            out.edge = mk.Edge(); out.p_start = a; out.p_end = b; out.closed = false;
            out.samples = {a, b};
            return true;
        }
        if (t == "circle") {
            Eigen::Vector2d c(ent["center"][0], ent["center"][1]);
            double r = ent["radius"];
            if (r <= kRegEps) return false;
            gp_Ax2 ax(reg_lift(pl, c), gp_Dir(pl.normal.x(), pl.normal.y(), pl.normal.z()),
                      gp_Dir(pl.uAxis.x(), pl.uAxis.y(), pl.uAxis.z()));
            BRepBuilderAPI_MakeEdge mk(gp_Circ(ax, r));
            if (!mk.IsDone()) return false;
            out.edge = mk.Edge(); out.p_start = c; out.p_end = c; out.closed = true;
            int n = 64;
            for (int i = 0; i < n; ++i) {
                double a = 2.0 * M_PI * i / n;
                out.samples.emplace_back(c.x() + r * std::cos(a), c.y() + r * std::sin(a));
            }
            return true;
        }
        if (t == "arc") {
            Eigen::Vector2d c(ent["center"][0], ent["center"][1]);
            double r = ent["radius"];
            double a0 = ent["start"], a1 = ent["end"];
            if (r <= kRegEps) return false;
            double sweep = a1 - a0;
            while (sweep <= 0.0) sweep += 2.0 * M_PI;
            double amid = a0 + 0.5 * sweep;
            Eigen::Vector2d ps(c.x() + r * std::cos(a0), c.y() + r * std::sin(a0));
            Eigen::Vector2d pe(c.x() + r * std::cos(a1), c.y() + r * std::sin(a1));
            Eigen::Vector2d pm(c.x() + r * std::cos(amid), c.y() + r * std::sin(amid));
            GC_MakeArcOfCircle mkarc(reg_lift(pl, ps), reg_lift(pl, pm), reg_lift(pl, pe));
            if (!mkarc.IsDone()) return false;
            BRepBuilderAPI_MakeEdge mk(mkarc.Value());
            if (!mk.IsDone()) return false;
            out.edge = mk.Edge(); out.p_start = ps; out.p_end = pe; out.closed = false;
            int n = 16;
            for (int i = 0; i <= n; ++i) {
                double a = a0 + sweep * i / n;
                out.samples.emplace_back(c.x() + r * std::cos(a), c.y() + r * std::sin(a));
            }
            return true;
        }
    } catch (...) { return false; }
    return false;
}

struct RegLoop {
    std::vector<TopoDS_Edge> edges;
    std::vector<Eigen::Vector2d> poly;  // ordered loop polyline (2D, for containment + overlay)
};

double reg_signed_area(const std::vector<Eigen::Vector2d>& p) {
    const size_t n = p.size();
    if (n < 3) return 0.0;
    double a = 0.0;
    for (size_t i = 0; i < n; ++i) {
        const Eigen::Vector2d& p0 = p[i];
        const Eigen::Vector2d& p1 = p[(i + 1) % n];
        a += p0.x() * p1.y() - p1.x() * p0.y();
    }
    return 0.5 * a;
}

Eigen::Vector2d reg_poly_centroid(const std::vector<Eigen::Vector2d>& p) {
    const size_t n = p.size();
    Eigen::Vector2d mean(0, 0);
    for (const auto& q : p) mean += q;
    if (n) mean /= double(n);
    double a = reg_signed_area(p);
    if (n < 3 || std::abs(a) < 1e-12) return mean;
    double cx = 0, cy = 0;
    for (size_t i = 0; i < n; ++i) {
        const Eigen::Vector2d& p0 = p[i];
        const Eigen::Vector2d& p1 = p[(i + 1) % n];
        double cross = p0.x() * p1.y() - p1.x() * p0.y();
        cx += (p0.x() + p1.x()) * cross;
        cy += (p0.y() + p1.y()) * cross;
    }
    return {cx / (6.0 * a), cy / (6.0 * a)};
}

bool reg_point_in_poly(const Eigen::Vector2d& pt, const std::vector<Eigen::Vector2d>& poly) {
    const size_t n = poly.size();
    if (n < 3) return false;
    bool inside = false;
    size_t j = n - 1;
    for (size_t i = 0; i < n; ++i) {
        double xi = poly[i].x(), yi = poly[i].y(), xj = poly[j].x(), yj = poly[j].y();
        if (((yi > pt.y()) != (yj > pt.y())) &&
            (pt.x() < (xj - xi) * (pt.y() - yi) / (yj - yi + 1e-30) + xi))
            inside = !inside;
        j = i;
    }
    return inside;
}

std::vector<RegLoop> reg_detect_loops(const std::vector<RegEdge>& edges) {
    std::vector<RegLoop> loops;
    const size_t n = edges.size();
    std::vector<bool> used(n, false);
    for (size_t i = 0; i < n; ++i) {
        if (!edges[i].closed) continue;
        used[i] = true;
        RegLoop lp;
        lp.edges.push_back(edges[i].edge);
        lp.poly = edges[i].samples;
        loops.push_back(std::move(lp));
    }
    for (size_t start = 0; start < n; ++start) {
        if (used[start]) continue;
        struct Step { size_t idx; bool rev; };
        std::vector<Step> chain{{start, false}};
        used[start] = true;
        const Eigen::Vector2d head = edges[start].p_start;
        Eigen::Vector2d tip = edges[start].p_end;
        bool closed = false;
        while (true) {
            long found = -1; bool frev = false;
            for (size_t j = 0; j < n; ++j) {
                if (used[j]) continue;
                if (reg_near(edges[j].p_start, tip)) { found = (long)j; frev = false; break; }
                if (reg_near(edges[j].p_end, tip)) { found = (long)j; frev = true; break; }
            }
            if (found < 0) break;
            chain.push_back({(size_t)found, frev});
            used[found] = true;
            tip = frev ? edges[found].p_start : edges[found].p_end;
            if (reg_near(tip, head)) { closed = true; break; }
        }
        if (!closed) continue;
        RegLoop lp;
        for (const Step& st : chain) {
            TopoDS_Edge e = edges[st.idx].edge;
            if (st.rev) e = TopoDS::Edge(e.Reversed());
            lp.edges.push_back(e);
            // polyline: edge samples in walk direction, excluding the duplicated joint.
            std::vector<Eigen::Vector2d> seg = edges[st.idx].samples;
            if (st.rev) std::reverse(seg.begin(), seg.end());
            for (size_t k = 0; k + 1 < seg.size(); ++k) lp.poly.push_back(seg[k]);
        }
        loops.push_back(std::move(lp));
    }
    return loops;
}

TopoDS_Wire reg_make_wire(const std::vector<TopoDS_Edge>& edges) {
    BRepBuilderAPI_MakeWire mk;
    for (const TopoDS_Edge& e : edges) mk.Add(e);
    if (!mk.IsDone()) return TopoDS_Wire();
    return mk.Wire();
}

// CCW-orient a loop's edges (matching _orient_ccw); reverse if signed area < 0.
std::vector<TopoDS_Edge> reg_orient_ccw(const RegLoop& lp) {
    if (reg_signed_area(lp.poly) >= 0.0) return lp.edges;
    std::vector<TopoDS_Edge> out;
    for (auto it = lp.edges.rbegin(); it != lp.edges.rend(); ++it)
        out.push_back(TopoDS::Edge(it->Reversed()));
    return out;
}

// Build a region face = outer loop minus its direct child (hole) loops.
TopoDS_Face reg_region_face(const RegLoop& outer, const std::vector<const RegLoop*>& holes) {
    TopoDS_Wire ow = reg_make_wire(reg_orient_ccw(outer));
    if (ow.IsNull()) return TopoDS_Face();
    BRepBuilderAPI_MakeFace mf(ow, true);
    for (const RegLoop* h : holes) {
        TopoDS_Wire hw = reg_make_wire(reg_orient_ccw(*h));
        if (hw.IsNull()) continue;
        mf.Add(TopoDS::Wire(hw.Reversed()));
    }
    if (!mf.IsDone()) return TopoDS_Face();
    return mf.Face();
}

// (area_or_len, centroid[u,v]) from a face's surface properties, centroid projected
// into the plane's local frame (sketch.py _region_descriptor).
void reg_face_descriptor(const TopoDS_Face& face, const aicad::geom::SketchPlane& pl,
                         double& area, double& cu, double& cv) {
    area = 0.0; cu = 0.0; cv = 0.0;
    try {
        GProp_GProps props;
        BRepGProp::SurfaceProperties(face, props);
        area = props.Mass();
        gp_Pnt cm = props.CentreOfMass();
        Eigen::Vector3d diff(cm.X() - pl.origin.x(), cm.Y() - pl.origin.y(),
                             cm.Z() - pl.origin.z());
        cu = diff.dot(pl.uAxis);
        cv = diff.dot(pl.vAxis);
    } catch (...) {}
}

// Hole-aware 2D triangulation of a region face in sketch-local (u,v) coords
// (sketch.py region_triangles_2d).
json reg_region_triangles_2d(const TopoDS_Face& face, const aicad::geom::SketchPlane& pl) {
    json out = json::array();
    try {
        BRepMesh_IncrementalMesh(face, 0.25, false, 0.5, true).Perform();
        TopLoc_Location loc;
        Handle(Poly_Triangulation) tri = BRep_Tool::Triangulation(face, loc);
        if (tri.IsNull()) return out;
        gp_Trsf trsf = loc.Transformation();
        bool ident = loc.IsIdentity();
        auto uv = [&](int i) -> std::array<double, 2> {
            gp_Pnt nd = tri->Node(i);
            if (!ident) nd.Transform(trsf);
            Eigen::Vector3d d(nd.X() - pl.origin.x(), nd.Y() - pl.origin.y(),
                              nd.Z() - pl.origin.z());
            return {d.dot(pl.uAxis), d.dot(pl.vAxis)};
        };
        std::vector<std::array<double, 2>> nodes;
        nodes.reserve(tri->NbNodes());
        for (int i = 1; i <= tri->NbNodes(); ++i) nodes.push_back(uv(i));
        for (int ti = 1; ti <= tri->NbTriangles(); ++ti) {
            int a, b, c;
            tri->Triangle(ti).Get(a, b, c);
            out.push_back(json::array({json::array({nodes[a - 1][0], nodes[a - 1][1]}),
                                       json::array({nodes[b - 1][0], nodes[b - 1][1]}),
                                       json::array({nodes[c - 1][0], nodes[c - 1][1]})}));
        }
    } catch (...) {}
    return out;
}

json poly_to_json(const std::vector<Eigen::Vector2d>& p) {
    json a = json::array();
    for (const auto& q : p) a.push_back(json::array({q.x(), q.y()}));
    return a;
}

}  // namespace

json doc_sketch_regions(const std::string& doc_id, const std::string& sketch_fid) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    json& feat = find_sketch_feature(s, sketch_fid);
    aicad::geom::SketchPlane plane = resolve_sketch_plane(s, feat);

    // Solve the sketch to get current entity coords (regions are computed on the
    // SOLVED geometry, same as session.py sketch_regions builds edges off entities).
    SolveActive packed = solve_active(s, feat, std::nullopt);
    const json& solved = packed.entity_map;  // id -> coord dict

    // Build region edges from non-construction solved entities.
    std::vector<RegEdge> edges;
    for (auto it = solved.begin(); it != solved.end(); ++it) {
        RegEdge re;
        // re-attach type from the entity record (solve output carries the params).
        json ent = it.value();
        if (!ent.contains("type")) {
            // recover type from the stored params
            json params = sketch_params(feat);
            if (params.contains("entities") && params["entities"].is_array())
                for (const json& e : params["entities"])
                    if (e.is_object() && e.value("id", std::string()) == it.key()) {
                        if (e.contains("type")) ent["type"] = e["type"];
                        if (e.contains("construction")) ent["construction"] = e["construction"];
                        break;
                    }
        }
        if (reg_build_edge(ent, plane, re)) edges.push_back(std::move(re));
    }

    std::vector<RegLoop> loops = reg_detect_loops(edges);
    const size_t n = loops.size();

    std::vector<double> areas(n);
    std::vector<Eigen::Vector2d> cents(n);
    for (size_t i = 0; i < n; ++i) {
        areas[i] = std::abs(reg_signed_area(loops[i].poly));
        cents[i] = reg_poly_centroid(loops[i].poly);
    }
    std::vector<int> depth(n, 0), parent(n, -1);
    for (size_t i = 0; i < n; ++i) {
        double best_area = 0.0; int best = -1;
        for (size_t j = 0; j < n; ++j) {
            if (i == j || areas[j] <= areas[i]) continue;
            if (reg_point_in_poly(cents[i], loops[j].poly)) {
                depth[i]++;
                if (best < 0 || areas[j] < best_area) { best_area = areas[j]; best = (int)j; }
            }
        }
        parent[i] = best;
    }
    std::vector<std::vector<int>> children(n);
    for (size_t i = 0; i < n; ++i)
        if (parent[i] >= 0) children[parent[i]].push_back((int)i);

    // Region descriptors (every loop, material + void). Paired with their face.
    struct Reg { json desc; };
    std::vector<Reg> regs;
    for (size_t i = 0; i < n; ++i) {
        std::vector<const RegLoop*> holes;
        for (int c : children[i]) holes.push_back(&loops[c]);
        TopoDS_Face face = reg_region_face(loops[i], holes);
        if (face.IsNull()) continue;
        double area, cu, cv;
        reg_face_descriptor(face, plane, area, cu, cv);
        json desc;
        desc["centroid"] = json::array({cu, cv});
        desc["area"] = area;
        desc["depth"] = depth[i];
        desc["is_material"] = (depth[i] % 2 == 0);
        desc["outer"] = poly_to_json(loops[i].poly);
        json holes_j = json::array();
        for (int c : children[i]) holes_j.push_back(poly_to_json(loops[c].poly));
        desc["holes"] = holes_j;
        desc["tris"] = reg_region_triangles_2d(face, plane);
        regs.push_back({std::move(desc)});
    }

    // Sort by centroid (u, then v) for stable indices; stamp index.
    std::sort(regs.begin(), regs.end(), [](const Reg& a, const Reg& b) {
        double au = a.desc["centroid"][0], bu = b.desc["centroid"][0];
        if (au != bu) return au < bu;
        return double(a.desc["centroid"][1]) < double(b.desc["centroid"][1]);
    });
    json regions = json::array();
    for (size_t i = 0; i < regs.size(); ++i) {
        regs[i].desc["index"] = (int)i;
        regions.push_back(regs[i].desc);
    }

    // bbox_diag: span of all outer coords * sqrt(2), floored (session.py sketch_regions).
    std::vector<double> coords;
    for (const json& r : regions)
        for (const json& pt : r["outer"]) { coords.push_back(pt[0]); coords.push_back(pt[1]); }
    double span = 100.0;
    if (!coords.empty()) {
        double mn = coords[0], mx = coords[0];
        for (double c : coords) { mn = std::min(mn, c); mx = std::max(mx, c); }
        span = mx - mn;
    }
    double diag = std::max(span * std::sqrt(2.0), 1e-3);

    json out;
    out["plane_frame"] = plane_frame(plane);
    out["bbox_diag"] = diag;
    out["regions"] = regions;
    return out;
}

json doc_sketch_probe(const std::string& doc_id, const std::string& sketch_fid,
                      const json& action) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    // The probe operates on the ACTIVE sketch (same context sketch_solve uses).
    json* featp = nullptr;
    try {
        featp = &find_sketch_feature(s, sketch_fid);
    } catch (const OpError&) {
        return json{{"ok", false}, {"error", "no sketch feature '" + sketch_fid + "'"}};
    }
    json& feat = *featp;
    if (!s.active_sketch || s.active_sketch->sketch_fid != sketch_fid)
        return json{{"ok", false},
                    {"error", "sketch '" + sketch_fid + "' is not the active sketch"}};
    if (!action.is_object() || action.size() != 1)
        return json{{"ok", false}, {"error", "action must be exactly one of "
                    "add_constraint / set_dim / delete_constraint"}};

    json env = env_from_doc(s.doc);
    json base_params = sketch_params(feat);
    json base_sk;
    base_sk["entities"] = base_params.contains("entities") ? base_params["entities"] : json::array();
    base_sk["constraints"] =
        base_params.contains("constraints") ? base_params["constraints"] : json::array();

    // COLD baseline solve of the unmodified copy for dof_before.
    aicad::sketch::SketchSolution before = aicad::sketch::solve_sketch(base_sk, env, std::nullopt);
    int dof_before = before.dof;

    // Deep-copy and mutate the COPY only.
    json params = base_sk;
    if (!params.contains("entities") || !params["entities"].is_array())
        params["entities"] = json::array();
    if (!params.contains("constraints") || !params["constraints"].is_array())
        params["constraints"] = json::array();
    json& cons = params["constraints"];

    static const std::string kProbeCid = "__probe__";
    bool probe_is_dim = false;

    if (action.contains("add_constraint")) {
        const json& con = action["add_constraint"];
        if (!con.is_object())
            return json{{"ok", false}, {"error", "add_constraint requires a constraint object"}};
        json c = con;
        c["id"] = kProbeCid;
        probe_is_dim = is_dim_type(c.value("type", std::string()));
        cons.push_back(std::move(c));
    } else if (action.contains("set_dim")) {
        const json& sd = action["set_dim"];
        if (!sd.is_object() || !sd.contains("id"))
            return json{{"ok", false}, {"error", "set_dim requires an 'id'"}};
        std::string cid = sd["id"].get<std::string>();
        json* target = nullptr;
        for (json& c : cons)
            if (c.is_object() && c.value("id", std::string()) == cid) { target = &c; break; }
        if (!target)
            return json{{"ok", false}, {"error", "set_dim: constraint '" + cid + "' not found"}};
        bool touched = false;
        if (sd.contains("expr") && !sd["expr"].is_null()) {
            double cur = (target->contains("value") && (*target)["value"].is_object() &&
                          (*target)["value"].contains("value"))
                             ? (*target)["value"]["value"].get<double>() : 0.0;
            (*target)["value"] = {{"value", cur}, {"expr", sd["expr"]}};
            touched = true;
        } else if (sd.contains("value") && !sd["value"].is_null()) {
            (*target)["value"] = {{"value", sd["value"].get<double>()}, {"expr", nullptr}};
            touched = true;
        }
        if (sd.contains("placement")) {
            if (sd["placement"].is_null()) {
                target->erase("placement");
            } else if (sd["placement"].is_array() && sd["placement"].size() == 2) {
                (*target)["placement"] = {sd["placement"][0].get<double>(),
                                          sd["placement"][1].get<double>()};
            } else {
                return json{{"ok", false}, {"error", "set_dim.placement must be [u, v] or null"}};
            }
            touched = true;
        }
        if (sd.contains("driven") && !sd["driven"].is_null()) {
            (*target)["driven"] = sd["driven"].get<bool>();
            touched = true;
        }
        if (!touched)
            return json{{"ok", false},
                        {"error", "set_dim requires 'value', 'expr', 'placement', or 'driven'"}};
    } else if (action.contains("delete_constraint")) {
        const json& dc = action["delete_constraint"];
        if (!dc.is_object() || !dc.contains("id"))
            return json{{"ok", false}, {"error", "delete_constraint requires an 'id'"}};
        std::string cid = dc["id"].get<std::string>();
        bool found = false;
        json kept = json::array();
        for (const json& c : cons)
            if (c.is_object() && c.value("id", std::string()) == cid) found = true;
            else kept.push_back(c);
        if (!found)
            return json{{"ok", false}, {"error", "delete_constraint: '" + cid + "' not found"}};
        params["constraints"] = kept;
    } else {
        return json{{"ok", false}, {"error", "unknown action; expected add_constraint / "
                    "set_dim / delete_constraint"}};
    }

    aicad::sketch::SketchSolution after = aicad::sketch::solve_sketch(params, env, std::nullopt);
    int dof_after = after.dof;
    const std::vector<std::string>& conflicting = after.conflicting;
    const std::vector<std::string>& redundant = after.redundant;

    json cons_by_id = json::object();
    for (const json& c : params["constraints"])
        if (c.is_object() && c.contains("id"))
            cons_by_id[c["id"].get<std::string>()] = c;

    auto in = [](const std::vector<std::string>& v, const std::string& x) {
        for (const std::string& s : v) if (s == x) return true;
        return false;
    };
    bool would = in(conflicting, kProbeCid) ||
                 (probe_is_dim && in(redundant, kProbeCid)) ||
                 (probe_is_dim && dof_after >= dof_before) ||
                 !after.converged || !conflicting.empty();

    json statuses = json::object();
    for (const auto& kv : after.statuses) statuses[kv.first] = kv.second;

    json out;
    out["ok"] = true;
    out["status"] = overall_status(statuses, dof_after, conflicting, redundant, cons_by_id);
    out["dof_before"] = dof_before;
    out["dof_after"] = dof_after;
    out["converged"] = after.converged;
    out["conflicting"] = strvec_json(conflicting);
    out["redundant"] = strvec_json(redundant);
    out["would_over_define"] = would;
    return out;
}

namespace {

// Intersection of plane (O2,n2) with the sketch plane (O1,u,v,n1) in sketch (u,v)
// coords. Returns (p_near, t): line point nearest the sketch origin + unit direction.
// Throws unsupported_ref when the planes are parallel (session.py::_plane_plane_line_uv).
std::pair<Eigen::Vector2d, Eigen::Vector2d> plane_plane_line_uv(
    const Eigen::Vector3d& O1, const Eigen::Vector3d& u, const Eigen::Vector3d& v,
    const Eigen::Vector3d& n1, const Eigen::Vector3d& O2, Eigen::Vector3d n2) {
    n2 = n2 / std::max(n2.norm(), 1e-30);
    Eigen::Vector3d d3 = n1.cross(n2);
    double L = d3.norm();
    if (L < 1e-9)
        throw OpError("unsupported_ref", "plane is parallel to the sketch plane",
                      {{"reason", "parallel planes"}});
    double c1 = n1.dot(O1), c2 = n2.dot(O2), k = n1.dot(n2);
    double denom = 1.0 - k * k;
    Eigen::Vector3d P0 = ((c1 - c2 * k) * n1 + (c2 - c1 * k) * n2) / denom;
    Eigen::Vector2d p0((P0 - O1).dot(u), (P0 - O1).dot(v));
    Eigen::Vector3d t3 = d3 / L;
    Eigen::Vector2d t(t3.dot(u), t3.dot(v));
    t = t / std::max(t.norm(), 1e-30);
    Eigen::Vector2d p_near = p0 - p0.dot(t) * t;
    return {p_near, t};
}

}  // namespace

// session.py::sketch_project_ref — project a datum plane / planar face / model edge
// into a sketch's (u,v) plane. READ-ONLY (no mutation, no rev bump, no rebuild).
// `ref` is {"datum":"front|top|right"} or {"kind":"face|edge","selector":{...}}.
json doc_sketch_project_ref(const std::string& doc_id, const std::string& sketch_fid,
                            const json& ref) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    json& feat = find_sketch_feature(s, sketch_fid);
    aicad::geom::SketchPlane plane = resolve_sketch_plane(s, feat);
    Eigen::Vector3d O1 = plane.origin, u = plane.uAxis, v = plane.vAxis, n1 = plane.normal;

    if (!ref.is_object() || (!ref.contains("datum") && !ref.contains("kind")))
        throw OpError("invalid_op",
                      "sketch_project_ref: 'ref' must carry 'datum' or 'kind'+'selector'",
                      {{"reason", "bad ref"}});

    auto uv = [&](const Eigen::Vector3d& P) -> json {
        Eigen::Vector3d d = P - O1;
        return json::array({d.dot(u), d.dot(v)});
    };

    CompileResult r = compile_document(s.doc, env_from_doc(s.doc));

    // ---- datum plane -> intersection line ----
    if (ref.contains("datum")) {
        std::string name = ref["datum"].get<std::string>();
        if (name.rfind("plane.", 0) != 0) name = "plane." + name;
        gp_Pln pln = eval_resolve_plane(name);  // throws on non-reserved datum
        Eigen::Vector3d O2(pln.Location().X(), pln.Location().Y(), pln.Location().Z());
        gp_Dir nd = pln.Axis().Direction();
        Eigen::Vector3d n2(nd.X(), nd.Y(), nd.Z());
        auto [p_near, t] = plane_plane_line_uv(O1, u, v, n1, O2, n2);
        double diag = 0.0;
        for (const TopoDS_Shape& sh : r.shapes) diag = std::max(diag, sim_bbox_diag(sh));
        double half = std::max(2.0 * diag, 100.0);
        Eigen::Vector2d p1 = p_near - half * t, p2 = p_near + half * t;
        return json{{"entity", {{"type", "line"},
                                {"p1", json::array({p1.x(), p1.y()})},
                                {"p2", json::array({p2.x(), p2.y()})}}},
                    {"source", name}};
    }

    std::string kind = ref.value("kind", std::string());
    if (r.shapes.empty())
        throw OpError("invalid_op", "sketch_project_ref: no solid bodies",
                      {{"reason", "no solid bodies"}});
    // Resolve against the LAST body (single-body parts; multi-body best-match deferred).
    const TopoDS_Shape& body = r.shapes.back();
    double diag = sim_bbox_diag(body);
    json sel = ref.contains("selector") ? ref["selector"] : json(nullptr);

    if (kind == "face") {
        TopoDS_Shape sub = eval_resolve_ref(sel, body, diag);
        TopoDS_Face face = TopoDS::Face(sub);
        BRepAdaptor_Surface ad(face);
        if (ad.GetType() != GeomAbs_Plane)
            throw OpError("unsupported_ref",
                          "sketch_project_ref: only planar faces can be projected",
                          {{"reason", "non-planar face"}});
        GProp_GProps props;
        BRepGProp::SurfaceProperties(face, props);
        gp_Pnt cm = props.CentreOfMass();
        gp_Dir nd = ad.Plane().Axis().Direction();
        auto [p_near, t] = plane_plane_line_uv(O1, u, v, n1,
                                               Eigen::Vector3d(cm.X(), cm.Y(), cm.Z()),
                                               Eigen::Vector3d(nd.X(), nd.Y(), nd.Z()));
        // Span by the face's projected vertices, +5 mm pad.
        std::vector<double> ss;
        TopTools_IndexedMapOfShape vmap;
        TopExp::MapShapes(face, TopAbs_VERTEX, vmap);
        for (int i = 1; i <= vmap.Extent(); ++i) {
            gp_Pnt p = BRep_Tool::Pnt(TopoDS::Vertex(vmap(i)));
            Eigen::Vector3d d(p.X() - O1.x(), p.Y() - O1.y(), p.Z() - O1.z());
            Eigen::Vector2d duv(d.dot(u), d.dot(v));
            ss.push_back((duv - p_near).dot(t));
        }
        if (ss.empty()) ss.push_back(0.0);
        double lo = *std::min_element(ss.begin(), ss.end()) - 5.0;
        double hi = *std::max_element(ss.begin(), ss.end()) + 5.0;
        Eigen::Vector2d p1 = p_near + lo * t, p2 = p_near + hi * t;
        return json{{"entity", {{"type", "line"},
                                {"p1", json::array({p1.x(), p1.y()})},
                                {"p2", json::array({p2.x(), p2.y()})}}},
                    {"source", "planar face"}};
    }

    if (kind == "edge") {
        TopoDS_Shape sub = eval_resolve_ref(sel, body, diag);
        TopoDS_Edge edge = TopoDS::Edge(sub);
        BRepAdaptor_Curve ad(edge);
        GeomAbs_CurveType ct = ad.GetType();
        if (ct == GeomAbs_Line) {
            gp_Pnt Pa = ad.Value(ad.FirstParameter());
            gp_Pnt Pb = ad.Value(ad.LastParameter());
            json a = uv(Eigen::Vector3d(Pa.X(), Pa.Y(), Pa.Z()));
            json b = uv(Eigen::Vector3d(Pb.X(), Pb.Y(), Pb.Z()));
            double dx = double(a[0]) - double(b[0]), dy = double(a[1]) - double(b[1]);
            if (std::hypot(dx, dy) < 1e-6)
                return json{{"entity", {{"type", "point"}, {"p", a}}},
                            {"source", "linear edge (projects to a point)"}};
            return json{{"entity", {{"type", "line"}, {"p1", a}, {"p2", b}}},
                        {"source", "linear edge"}};
        }
        if (ct == GeomAbs_Circle) {
            gp_Circ circ = ad.Circle();
            gp_Dir axd = circ.Axis().Direction();
            Eigen::Vector3d ax3(axd.X(), axd.Y(), axd.Z());
            ax3 = ax3 / std::max(ax3.norm(), 1e-30);
            if (std::abs(ax3.dot(n1)) <= 1.0 - 1e-6)
                throw OpError("unsupported_ref",
                              "edge type not supported for sketch projection",
                              {{"reason", "circle axis not parallel to the sketch normal"}});
            gp_Pnt loc = circ.Location();
            return json{{"entity", {{"type", "circle"},
                                    {"center", uv(Eigen::Vector3d(loc.X(), loc.Y(), loc.Z()))},
                                    {"radius", circ.Radius()}}},
                        {"source", "circular edge"}};
        }
        throw OpError("unsupported_ref", "edge type not supported for sketch projection",
                      {{"reason", "unsupported curve type"}});
    }

    throw OpError("invalid_op",
                  "sketch_project_ref: ref kind '" + kind + "' must be face|edge",
                  {{"reason", "bad kind"}});
}

}  // namespace aicad::doc
