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
#include <cstring>
#include <filesystem>
#include <fstream>
#include <limits>
#include <map>
#include <mutex>
#include <regex>
#include <thread>
#include <set>
#include <string>
#include <vector>

#include <cmath>
#include <functional>
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
#include <TopoDS_Compound.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Shape.hxx>
#include <BRep_Builder.hxx>
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
#include <TopTools_ListOfShape.hxx>
#include <TopTools_ListIteratorOfListOfShape.hxx>
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
#include <ShapeAnalysis_FreeBounds.hxx>     // section_props: group multi-loop section edges into closed wires
#include <TopTools_HSequenceOfShape.hxx>    // ConnectEdgesToWires in/out sequences
#include <Geom_Surface.hxx>
#include <gp_Circ.hxx>
#include <gp_Pnt2d.hxx>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Wire.hxx>  // section_props stores closed section loops by value
#include <TopoDS_Vertex.hxx>

#include <Eigen/Eigenvalues>

#include <memory>

#include "doc/assembly.h"
#include "doc/document.h"
#include "doc/formats.h"
#include "doc/picks.h"
#include "doc/selectors.h"
#include "doc/sketch_geom.h"
#include "doc/drawing_session.h"  // W12+ drawing embed/restore on save/open
#include "doc/stepplus.h"
#include "doc/validate.h"
#include "geom/aicm.h"
#include "geom/plane.h"
#include "geom/profile.h"
#include "geom/tessellate.h"
#include "geom/airfoil.h"  // "airfoil" sketch-entity expansion (agent aerofoil support)

#include <miniz.h>  // PNG/deflate for the agent vision-review rasterizer (doc_render_png)
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

    // Read-only compile cache (PERF). `compiled` holds the last compile of `doc`, valid only
    // while compiled_rev == revision. Every geometry-changing op bumps revision (or starts a
    // fresh session), and all reads run serialized under the doc lock, so a stale result can
    // never be served. Interactive read paths — viewport picking, "fillet a whole face" edge
    // expansion, owner-map refresh — reuse this instead of recompiling the entire feature tree
    // on every click (measured ~190 ms/click on a modest part before this cache).
    std::shared_ptr<CompileResult> compiled;
    long compiled_rev = -1;

    // C20 — assembly state. asm_result is the last assembly compile; asm_warm seeds the
    // next mate solve; explode_active/explode_factor drive tessellation-time offsets.
    std::shared_ptr<AssemblyResult> asm_result;        // nullptr until first assembly rebuild
    // Per-component topology-description cache for assembly picks (PERF). Keyed "<body_id>:<kind>"
    // -> describe_{faces,edges,vertices}(component). describe_* runs Gauss integration over the
    // whole component, so without this every click in a big assembly re-derived it. Valid only
    // while built against the current asm_result (asm_describe_for); a rebuild swaps the pointer.
    std::map<std::string, std::vector<TopoInfo>> asm_describe_cache;
    const void* asm_describe_for = nullptr;
    aicad::mate::WarmMap asm_warm;
    bool explode_active = false;
    double explode_factor = 1.0;

    bool is_assembly() const {
        return doc.is_object() && doc.value("schema", std::string()) == "aicad.asm/1";
    }
};

constexpr std::size_t kUndoLimit = 100;

std::mutex g_mtx;
// Owner-tracking for crash recovery (see force_release_doc_lock / session.h). We
// record which thread currently holds g_mtx so that, if a Windows structured
// exception (an access violation) unwinds PAST a DocLock without running its C++
// destructor under /EHsc, the dispatch boundary can detect the orphaned lock —
// owned by this very thread — and drop it before serving the next call. A plain
// std::mutex exposes no owner, so we maintain it ourselves. g_mtx_held is true
// only while a DocLock is alive; g_mtx_owner is meaningful only when held.
std::atomic<bool> g_mtx_held{false};
std::thread::id g_mtx_owner;  // guarded by g_mtx (only read/written while held)

// RAII lock for the process-global session store. Behaves exactly like
// std::lock_guard<std::mutex> on g_mtx for the normal (success / C++-exception)
// paths — the destructor releases on stack unwind — while additionally stamping
// the owning thread so an SEH-orphaned lock is recoverable (force_release_doc_lock).
class DocLock {
public:
    DocLock() {
        g_mtx.lock();
        g_mtx_owner = std::this_thread::get_id();
        g_mtx_held.store(true, std::memory_order_release);
    }
    ~DocLock() {
        g_mtx_held.store(false, std::memory_order_release);
        g_mtx.unlock();
    }
    DocLock(const DocLock&) = delete;
    DocLock& operator=(const DocLock&) = delete;
};

std::map<std::string, Session> g_sessions;
std::atomic<unsigned long long> g_doc_seq{1};
std::atomic<unsigned long long> g_ckpt_seq{1};
std::atomic<unsigned long long> g_study_seq{1};

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

std::string new_study_id() {
    // Opaque, unique-per-process id for a simulation study. Only assigned by the core
    // (the host fills every other study field; the core treats them as opaque JSON).
    unsigned long long n = g_study_seq.fetch_add(1);
    char buf[32];
    std::snprintf(buf, sizeof(buf), "study-%08llx", n);
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
        // Assembly id families (mirrors ops.py::_scan_max): components mint k<N>, mates m<N>.
        // Without seeding these, a fresh apply_ops batch restarts the counter at 0 and a
        // second asm_insert_component re-mints 'k1' -> "duplicate 'k1'" validation failure.
        for (const char* fam : {"components", "mates"}) {
            if (doc.contains(fam) && doc[fam].is_array())
                for (const json& it : doc[fam])
                    if (it.is_object() && it.contains("id") && it["id"].is_string())
                        note(it["id"].get<std::string>());
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
    // A model may put primitive/feature OPTIONS at the feature TOP LEVEL instead of in
    // params (their canonical home) — e.g. {"type":"cylinder","mode":"cut",...}. Only
    // type/name/suppressed/params/refs/folder are kept above, so a stray top-level option
    // would be SILENTLY DROPPED -> wrong geometry with no error (a cut cylinder fuses).
    // Relocate the known options into params so both conventions behave identically.
    // `body` is the transform/fillet/chamfer/shell/hole/draft target — documented as a
    // top-level field; without hoisting it here it was dropped on persistence, so
    // transform body:"fN" silently moved the current/last body instead (the assembly
    // mistargeting bug T-1). `copy` is transform's instancing flag, same hazard.
    if (env["params"].is_object()) {
        for (const char* k : {"mode", "merge", "centered", "at", "at_offset", "offset",
                              "body", "copy",
                              // W1: `positions` on hole/bolt_circle is documented as a top-level
                              // field but was silently dropped because op_add_feature copies only
                              // type/name/suppressed/params/refs/folder into the stored feature.
                              // Without hoisting it here the feature built only ONE hole at the
                              // face centre (the default when no positions are given). Hoist it
                              // into params exactly like `body`/`copy` so both placements produce
                              // the same N-hole result — consistent with the no-silent-drop rule.
                              "positions"}) {
            if (feat.contains(k) && !env["params"].contains(k)) env["params"][k] = feat[k];
        }
    }
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

    // Leniency for weak local models: a spline's points sometimes arrive DOUBLE-WRAPPED as
    // [[[x,y],[x,y],...]] (one extra array level). Unwrap that single wrapping so the curve
    // builds instead of failing validation. Only fires on the exact double-wrap shape.
    if (ent.value("type", std::string()) == "spline" && ent.contains("points") &&
        ent["points"].is_array() && ent["points"].size() == 1 && ent["points"][0].is_array() &&
        !ent["points"][0].empty() && ent["points"][0][0].is_array()) {
        ent["points"] = ent["points"][0];
    }

    // AIRFOIL convenience entity: {type:"airfoil", naca:"2412"|dat:"<path>", chord, aoa,
    // points, closedTE} expands to the upper-spline + lower-spline (+ a trailing-edge line for
    // an open TE) — one closed aerofoil loop the agent can extrude/loft. Standard splines from
    // here on, so downstream (solver, region detect, profile.cpp) needs no airfoil awareness.
    if (ent.value("type", std::string()) == "airfoil") {
        const std::string naca = ent.value("naca", std::string());
        const std::string dat = ent.value("dat", std::string());
        const double chord = ent.value("chord", 100.0);
        const double aoa = ent.value("aoa", 0.0);
        const int pts = ent.value("points", 60);
        const bool closedTE = ent.value("closedTE", true);
        aicad::geom::AirfoilProfile prof =
            aicad::geom::airfoil_profile(naca, dat, chord, aoa, pts, closedTE);
        if (!prof.error.empty()) throw OpError("invalid_op", "airfoil: " + prof.error);
        if (prof.upper.size() < 2 || prof.lower.size() < 2)
            throw OpError("invalid_op", "airfoil: profile produced too few points");
        const bool constr = ent.value("construction", false);
        std::vector<std::string> ids;
        auto push_spline = [&](const std::vector<std::array<double, 2>>& pl) {
            json s;
            s["type"] = "spline";
            json arr = json::array();
            for (const auto& q : pl) arr.push_back(json::array({q[0], q[1]}));
            s["points"] = arr;
            s["id"] = gen.next("e");
            s["construction"] = constr;
            ids.push_back(s["id"].get<std::string>());
            sk["params"]["entities"].push_back(std::move(s));
        };
        push_spline(prof.upper);  // LE -> TE (shares the LE point with `lower`)
        push_spline(prof.lower);  // LE -> TE
        if (!prof.sharpTE) {      // close the open trailing edge with a short line TE_u -> TE_l
            json ln;
            ln["type"] = "line";
            ln["p1"] = json::array({prof.upper.back()[0], prof.upper.back()[1]});
            ln["p2"] = json::array({prof.lower.back()[0], prof.lower.back()[1]});
            ln["id"] = gen.next("e");
            ln["construction"] = constr;
            ids.push_back(ln["id"].get<std::string>());
            sk["params"]["entities"].push_back(std::move(ln));
        }
        id_map["$" + std::to_string(idx)] = ids.empty() ? std::string() : ids[0];
        for (std::size_t k = 1; k < ids.size(); ++k)
            id_map["$" + std::to_string(idx) + "#" + std::to_string(k)] = ids[k];
        return;
    }

    // REGULAR POLYGON convenience entity: {type:"polygon"|"regular_polygon"|"hexagon", center:[x,y],
    // sides:N, and ONE size — across_flats (wrench/spanner size), circumradius/radius (centre->vertex),
    // or inradius/apothem} expands to N closed line segments. A small local model can't reliably draw
    // a closed hexagon as a spline (it emits an OPEN spline -> no region -> "extrude produced no
    // solid"); this gives it a clean closed hex/nut/bolt-head profile in one entity. type:"hexagon"
    // implies sides=6. Plain lines downstream, so the solver / region-detect / profile.cpp need no
    // polygon awareness — same trick as the airfoil entity above.
    {
        const std::string et = ent.value("type", std::string());
        if (et == "polygon" || et == "regular_polygon" || et == "ngon" || et == "hexagon") {
            int n = et == "hexagon" ? 6 : 0;
            for (const char* k : {"sides", "n", "num_sides", "count"})
                if (ent.contains(k) && ent[k].is_number_integer()) { n = ent[k].get<int>(); break; }
            if (n == 0 && et == "hexagon") n = 6;
            if (n < 3) throw OpError("invalid_op",
                                     "polygon: needs 'sides' >= 3 (e.g. {\"type\":\"polygon\","
                                     "\"sides\":6,\"across_flats\":12})");
            double cx = 0.0, cy = 0.0;
            if (ent.contains("center") && ent["center"].is_array() && ent["center"].size() >= 2) {
                cx = ent["center"][0].get<double>();
                cy = ent["center"][1].get<double>();
            }
            auto getnum = [&](std::initializer_list<const char*> keys, double* out) -> bool {
                for (const char* k : keys)
                    if (ent.contains(k) && ent[k].is_number()) { *out = ent[k].get<double>(); return true; }
                return false;
            };
            const double apo = std::cos(M_PI / n);  // inradius / circumradius
            double v = 0.0, R = 0.0;
            if (getnum({"circumradius", "radius", "r", "vertex_radius"}, &v)) R = v;
            else if (getnum({"across_flats", "width_across_flats", "af", "wrench", "flats",
                             "flat_to_flat", "width"}, &v)) R = (v * 0.5) / apo;
            else if (getnum({"inradius", "apothem"}, &v)) R = v / apo;
            else if (getnum({"diameter"}, &v)) R = v * 0.5;  // circum-diameter
            if (!(R > 0.0))
                throw OpError("invalid_op",
                              "polygon: needs a size — 'across_flats' (wrench size), 'circumradius', "
                              "or 'inradius' (mm)");
            double rot = 0.0;
            for (const char* k : {"rotation", "angle"})
                if (ent.contains(k) && ent[k].is_number()) { rot = ent[k].get<double>(); break; }
            rot *= M_PI / 180.0;
            const bool constr = ent.value("construction", false);
            std::vector<std::array<double, 2>> verts;
            for (int i = 0; i < n; ++i) {
                double a = rot + 2.0 * M_PI * i / n;
                verts.push_back({cx + R * std::cos(a), cy + R * std::sin(a)});
            }
            std::vector<std::string> ids;
            for (int i = 0; i < n; ++i) {
                const auto& a = verts[i];
                const auto& b = verts[(i + 1) % n];
                json ln;
                ln["type"] = "line";
                ln["p1"] = json::array({a[0], a[1]});
                ln["p2"] = json::array({b[0], b[1]});
                ln["id"] = gen.next("e");
                ln["construction"] = constr;
                ids.push_back(ln["id"].get<std::string>());
                sk["params"]["entities"].push_back(std::move(ln));
            }
            id_map["$" + std::to_string(idx)] = ids.empty() ? std::string() : ids[0];
            for (std::size_t k = 1; k < ids.size(); ++k)
                id_map["$" + std::to_string(idx) + "#" + std::to_string(k)] = ids[k];
            return;
        }
    }

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
            // A constraint's `value` is a dim object {value, expr} (see validate_dim).
            // Any subset of {value|expr, placement, driven} may be supplied; expr wins
            // over value when both are present (mirrors the Python oracle).
            bool touched = false;
            if (op.contains("expr") && !op["expr"].is_null()) {
                double cur = (c.contains("value") && c["value"].is_object())
                                 ? c["value"].value("value", 0.0)
                                 : 0.0;
                c["value"] = json{{"value", cur}, {"expr", op["expr"]}};
                touched = true;
            } else if (op.contains("value") && !op["value"].is_null()) {
                c["value"] = json{{"value", op["value"].get<double>()}, {"expr", nullptr}};
                touched = true;
            }
            if (op.contains("placement")) {
                if (op["placement"].is_null()) {
                    c.erase("placement");
                } else if (op["placement"].is_array() && op["placement"].size() == 2) {
                    c["placement"] = json{op["placement"][0].get<double>(),
                                          op["placement"][1].get<double>()};
                } else {
                    throw OpError("invalid_op", "sketch_set_dim.placement must be [u, v] or null");
                }
                touched = true;
            }
            if (op.contains("driven") && !op["driven"].is_null()) {
                if (!op["driven"].is_boolean())
                    throw OpError("invalid_op", "sketch_set_dim.driven must be a bool");
                c["driven"] = op["driven"];
                touched = true;
            }
            if (!touched)
                throw OpError("invalid_op",
                              "sketch_set_dim requires 'value', 'expr', 'placement', or 'driven'");
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

// =========================================================================== copy/paste + derived
// C++ port of the copy/paste-as-derived subsystem (engine/doc/ops.py Wave 4A/4C +
// engine/doc/session.py refresh_derived). copy_feature packages a feature (+ its full
// reference closure) into a portable "aicad.feature_bundle/1" bundle (read-only, via the
// id_map side-channel). paste_feature re-ids the bundle and appends it (refs re-linked to
// the target doc). paste_feature_as_derived additionally stamps a derived_from link on each
// pasted feature so a later doc_refresh_derived can re-pull the live source definition.
// Everything here mirrors the oracle byte-for-byte so the differential parity harness matches.

std::atomic<unsigned long long> g_group_seq{1};
std::string new_group_id() {
    unsigned long long n = g_group_seq.fetch_add(1);
    char buf[32];
    std::snprintf(buf, sizeof(buf), "dg-%010llx", n);
    return std::string(buf);
}

bool is_reserved_datum(const std::string& s) {
    static const std::set<std::string> R = {
        "origin", "plane.front", "plane.top", "plane.right", "axis.x", "axis.y", "axis.z"};
    return R.count(s) > 0;
}
bool is_feature_id_token(const std::string& s) {
    static const std::regex re("^f[0-9]+$");
    return std::regex_match(s, re);
}

// const feature finder (build_feature_bundle / closure read a const source doc).
const json* find_feature_c(const json& doc, const std::string& id) {
    if (doc.contains("features") && doc["features"].is_array())
        for (const json& f : doc["features"])
            if (f.is_object() && f.value("id", std::string()) == id) return &f;
    return nullptr;
}

// ops.py::_walk_selector_owners — collect every owner_feature / datum.feature / region.sketch.
void walk_selector_owners(const json& obj, std::set<std::string>& out) {
    if (obj.is_object()) {
        const std::string kind = obj.value("kind", std::string());
        if (kind == "face" || kind == "edge" || kind == "vertex" || kind == "body") {
            if (obj.contains("owner_feature") && obj["owner_feature"].is_string())
                out.insert(obj["owner_feature"].get<std::string>());
        } else if (kind == "datum") {
            if (obj.contains("feature") && obj["feature"].is_string())
                out.insert(obj["feature"].get<std::string>());
        } else if (kind == "region") {
            if (obj.contains("sketch") && obj["sketch"].is_string())
                out.insert(obj["sketch"].get<std::string>());
        }
        for (auto it = obj.begin(); it != obj.end(); ++it) walk_selector_owners(it.value(), out);
    } else if (obj.is_array()) {
        for (const json& v : obj) walk_selector_owners(v, out);
    }
}

// ops.py::_feature_refs_closure — dependency-first order of the transitive ref closure.
std::pair<std::vector<std::string>, std::set<std::string>>
feature_refs_closure(const json& doc, const std::string& root_fid) {
    std::set<std::string> in_closure;
    std::function<std::set<std::string>(const std::string&)> direct_refs =
        [&](const std::string& fid) -> std::set<std::string> {
        std::set<std::string> out;
        const json* feat = find_feature_c(doc, fid);
        if (!feat) return out;
        const json refs = feat->contains("refs") ? (*feat)["refs"] : json::object();
        walk_selector_owners(refs, out);
        if (refs.is_object())
            for (auto it = refs.begin(); it != refs.end(); ++it) {
                const json& v = it.value();
                if (v.is_array()) {
                    for (const json& x : v)
                        if (x.is_string()) out.insert(x.get<std::string>());
                } else if (v.is_string()) {
                    out.insert(v.get<std::string>());
                }
            }
        std::set<std::string> filtered;
        for (const std::string& r : out)
            if (!is_reserved_datum(r) && find_feature_c(doc, r) != nullptr) filtered.insert(r);
        return filtered;
    };
    std::function<void(const std::string&)> visit = [&](const std::string& fid) {
        if (in_closure.count(fid)) return;
        in_closure.insert(fid);
        for (const std::string& dep : direct_refs(fid)) visit(dep);
    };
    visit(root_fid);
    std::vector<std::string> order;
    if (doc.contains("features") && doc["features"].is_array())
        for (const json& f : doc["features"]) {
            const std::string id = f.value("id", std::string());
            if (in_closure.count(id)) order.push_back(id);
        }
    return {order, in_closure};
}

// ops.py::_remap_handle — rewrite the base of a sketch handle through eid_map.
json remap_handle(const json& handle, const std::map<std::string, std::string>& eid_map) {
    if (handle.is_string()) {
        const std::string h = handle.get<std::string>();
        const auto dot = h.find('.');
        const std::string base = (dot == std::string::npos) ? h : h.substr(0, dot);
        auto it = eid_map.find(base);
        if (it != eid_map.end())
            return json(it->second + (dot == std::string::npos ? std::string() : h.substr(dot)));
    }
    return handle;
}

// ops.py::_stamp_bundle_refs — in-closure ids -> placeholders; out-of-closure feature ids
// recorded in external_refs (left raw).
json stamp_bundle_refs(const json& refs, const std::map<std::string, std::string>& fid_to_ph,
                       std::set<std::string>& external_refs) {
    auto remap_fid = [&](const std::string& fid) -> std::string {
        auto it = fid_to_ph.find(fid);
        if (it != fid_to_ph.end()) return it->second;
        if (!is_reserved_datum(fid) && is_feature_id_token(fid)) external_refs.insert(fid);
        return fid;
    };
    std::function<json(const json&)> walk = [&](const json& obj) -> json {
        if (obj.is_object()) {
            json out = json::object();
            const std::string kind = obj.value("kind", std::string());
            for (auto it = obj.begin(); it != obj.end(); ++it) {
                const std::string& k = it.key();
                const json& v = it.value();
                if ((kind == "face" || kind == "edge" || kind == "vertex" || kind == "body") &&
                    k == "owner_feature" && v.is_string())
                    out[k] = remap_fid(v.get<std::string>());
                else if (kind == "datum" && k == "feature" && v.is_string())
                    out[k] = remap_fid(v.get<std::string>());
                else if (kind == "region" && k == "sketch" && v.is_string())
                    out[k] = remap_fid(v.get<std::string>());
                else
                    out[k] = walk(v);
            }
            return out;
        }
        if (obj.is_array()) {
            json a = json::array();
            for (const json& v : obj) a.push_back(walk(v));
            return a;
        }
        if (obj.is_string()) {
            const std::string s = obj.get<std::string>();
            if (is_reserved_datum(s)) return obj;
            if (is_feature_id_token(s)) return json(remap_fid(s));
        }
        return obj;
    };
    return walk(refs.is_null() ? json::object() : refs);
}

// ops.py::_stamp_bundle_feature — portable copy: id removed, refs placeholdered, sketch
// entity/constraint ids -> ~e<n>/~c<n> with handles re-pointed.
json stamp_bundle_feature(const json& feat, const std::map<std::string, std::string>& fid_to_ph,
                          std::set<std::string>& external_refs) {
    json out = feat;
    out.erase("id");
    out["refs"] = stamp_bundle_refs(out.contains("refs") ? out["refs"] : json::object(),
                                    fid_to_ph, external_refs);
    if (out.value("type", std::string()) == "sketch") {
        if (!out.contains("params") || !out["params"].is_object()) out["params"] = json::object();
        json& params = out["params"];
        if (!params.contains("entities") || !params["entities"].is_array())
            params["entities"] = json::array();
        if (!params.contains("constraints") || !params["constraints"].is_array())
            params["constraints"] = json::array();
        json& ents = params["entities"];
        json& cons = params["constraints"];
        std::map<std::string, std::string> eid_map;
        for (std::size_t i = 0; i < ents.size(); ++i)
            if (ents[i].is_object() && ents[i].contains("id") && ents[i]["id"].is_string())
                eid_map[ents[i]["id"].get<std::string>()] = "~e" + std::to_string(i);
        for (std::size_t i = 0; i < ents.size(); ++i)
            if (ents[i].is_object()) ents[i]["id"] = "~e" + std::to_string(i);
        for (std::size_t i = 0; i < cons.size(); ++i)
            if (cons[i].is_object()) {
                cons[i]["id"] = "~c" + std::to_string(i);
                json nr = json::array();
                if (cons[i].contains("refs") && cons[i]["refs"].is_array())
                    for (const json& h : cons[i]["refs"]) nr.push_back(remap_handle(h, eid_map));
                cons[i]["refs"] = nr;
            }
    }
    return out;
}

// ops.py::build_feature_bundle — package feature `fid` (+ closure) into a portable bundle.
json build_feature_bundle(const json& doc, const std::string& fid, bool include_dependencies) {
    const json* feat = find_feature_c(doc, fid);
    if (!feat)
        throw OpError("invalid_op", "feature '" + fid + "' not found",
                      {{"reason", "unknown feature " + fid}});
    std::vector<std::string> order;
    if (include_dependencies)
        order = feature_refs_closure(doc, fid).first;
    else
        order = {fid};

    std::map<std::string, std::string> fid_to_ph;
    for (std::size_t i = 0; i < order.size(); ++i) fid_to_ph[order[i]] = "$" + std::to_string(i);

    std::set<std::string> external_refs;
    json features = json::object();
    for (const std::string& src_fid : order) {
        const json* f = find_feature_c(doc, src_fid);
        features[fid_to_ph[src_fid]] = stamp_bundle_feature(*f, fid_to_ph, external_refs);
    }

    json external_datums = json::object();
    for (const std::string& ext : external_refs) {
        const json* ef = find_feature_c(doc, ext);
        if (!ef) continue;
        const std::string t = ef->value("type", std::string());
        if (t == "plane" || t == "axis" || t == "point" || t == "csys") {
            const std::string nm = ef->value("name", std::string());
            external_datums[ext] = nm.empty() ? ext : nm;
        }
    }

    json feature_order = json::array();
    for (const std::string& src : order) feature_order.push_back(fid_to_ph[src]);
    json source_ids = json::object();
    for (const std::string& src : order) source_ids[fid_to_ph[src]] = src;
    json ext_sorted = json::array();
    for (const std::string& e : external_refs) ext_sorted.push_back(e);  // std::set is sorted

    return json{
        {"kind", "aicad.feature_bundle/1"},
        {"source_doc_id", doc.contains("id") ? doc["id"] : json(nullptr)},
        {"source_doc_name", doc.contains("name") ? doc["name"] : json(nullptr)},
        {"root", fid_to_ph[fid]},
        {"feature_order", feature_order},
        {"features", features},
        {"external_refs", ext_sorted},
        {"external_datums", external_datums},
        {"source_ids", source_ids},
    };
}

// ops.py::_new_feature_envelope — normalize a bundled feature into a full envelope.
json new_feature_envelope(const json& feature, const std::string& fid) {
    json out = feature;
    out["id"] = fid;
    if (!out.contains("name")) out["name"] = fid;
    if (!out.contains("suppressed")) out["suppressed"] = false;
    if (!out.contains("params")) out["params"] = json::object();
    if (!out.contains("refs")) out["refs"] = json::object();
    return out;
}

// ops.py::_target_datum_by_name — same-named target datum (plane/axis/point/csys) or reserved id.
std::optional<std::string> target_datum_by_name(const json& doc, const std::string& name) {
    if (is_reserved_datum(name)) return name;
    if (doc.contains("features") && doc["features"].is_array())
        for (const json& f : doc["features"]) {
            const std::string t = f.value("type", std::string());
            if ((t == "plane" || t == "axis" || t == "point" || t == "csys") &&
                f.value("name", std::string()) == name)
                return f.value("id", std::string());
        }
    return std::nullopt;
}

// ops.py::_resolve_datum — out-of-closure datum: same-named reuse, else schema-valid fallback.
json resolve_datum(const std::string& src_id, const json& external_datums, const json& doc,
                   const json& src_doc_id, const json& src_doc_name) {
    if (external_datums.contains(src_id) && external_datums[src_id].is_string()) {
        const std::string name = external_datums[src_id].get<std::string>();
        auto match = target_datum_by_name(doc, name);
        if (match) {
            if (is_reserved_datum(*match)) return json(*match);
            return json{{"kind", "datum"}, {"feature", *match}};
        }
    }
    return json{{"kind", "datum"}, {"feature", "plane.front"},
                {"external", {{"doc_id", src_doc_id}, {"doc_name", src_doc_name},
                              {"owner_feature", src_id}, {"unresolved", true}}}};
}

// ops.py::_resolve_paste_refs — re-link a pasted feature's refs against the TARGET doc.
json resolve_paste_refs(const json& refs, const std::map<std::string, std::string>& ph_to_fid,
                        const json& bundle, const json& doc) {
    const json src_doc_id = bundle.contains("source_doc_id") ? bundle["source_doc_id"] : json(nullptr);
    const json src_doc_name = bundle.contains("source_doc_name") ? bundle["source_doc_name"] : json(nullptr);
    const json external_datums = bundle.contains("external_datums") ? bundle["external_datums"] : json::object();
    auto ext_marker = [&](const json& owner) -> json {
        return json{{"doc_id", src_doc_id}, {"doc_name", src_doc_name}, {"owner_feature", owner}};
    };
    std::function<json(const json&)> walk = [&](const json& obj) -> json {
        if (obj.is_object()) {
            const std::string kind = obj.value("kind", std::string());
            if (kind == "face" || kind == "edge" || kind == "vertex" || kind == "body") {
                json out = obj;
                if (obj.contains("owner_feature") && obj["owner_feature"].is_string()) {
                    const std::string ow = obj["owner_feature"].get<std::string>();
                    auto it = ph_to_fid.find(ow);
                    if (it != ph_to_fid.end())
                        out["owner_feature"] = it->second;
                    else {
                        out["owner_feature"] = nullptr;
                        out["external"] = ext_marker(ow);
                    }
                }
                return out;
            }
            if (kind == "datum") {
                json out = obj;
                if (obj.contains("feature") && obj["feature"].is_string()) {
                    const std::string fe = obj["feature"].get<std::string>();
                    auto it = ph_to_fid.find(fe);
                    if (it != ph_to_fid.end()) {
                        out["feature"] = it->second;
                        return out;
                    }
                    return resolve_datum(fe, external_datums, doc, src_doc_id, src_doc_name);
                }
                return out;
            }
            if (kind == "region") {
                json out = obj;
                if (obj.contains("sketch") && obj["sketch"].is_string()) {
                    const std::string sk = obj["sketch"].get<std::string>();
                    auto it = ph_to_fid.find(sk);
                    if (it != ph_to_fid.end())
                        out["sketch"] = it->second;
                    else
                        out["external"] = ext_marker(sk);
                }
                return out;
            }
            json out = json::object();
            for (auto it = obj.begin(); it != obj.end(); ++it) out[it.key()] = walk(it.value());
            return out;
        }
        if (obj.is_array()) {
            bool all_str = !obj.empty();
            for (const json& v : obj)
                if (!v.is_string()) { all_str = false; break; }
            if (all_str) {
                json kept = json::array();
                for (const json& v : obj) {
                    auto it = ph_to_fid.find(v.get<std::string>());
                    if (it != ph_to_fid.end()) kept.push_back(it->second);
                }
                if (kept != obj) return kept;  // a feature-id list with external members dropped
            }
            json resolved = json::array();
            for (const json& v : obj) resolved.push_back(walk(v));
            return resolved;
        }
        if (obj.is_string()) {
            const std::string s = obj.get<std::string>();
            auto it = ph_to_fid.find(s);
            if (it != ph_to_fid.end()) return json(it->second);
            if (is_reserved_datum(s)) return obj;
            if (is_feature_id_token(s)) return resolve_datum(s, external_datums, doc, src_doc_id, src_doc_name);
        }
        return obj;
    };
    return walk(refs.is_null() ? json::object() : refs);
}

// ops.py::_reid_pasted_sketch — fresh e<N>/c<N> ids for a pasted sketch (handles re-pointed).
void reid_pasted_sketch(json& feat, IdGen& gen) {
    if (!feat.contains("params") || !feat["params"].is_object()) feat["params"] = json::object();
    json& params = feat["params"];
    std::map<std::string, std::string> eid_map;
    if (params.contains("entities") && params["entities"].is_array())
        for (json& ent : params["entities"])
            if (ent.is_object()) {
                const std::string neid = gen.next("e");
                if (ent.contains("id") && ent["id"].is_string())
                    eid_map[ent["id"].get<std::string>()] = neid;
                ent["id"] = neid;
            }
    if (params.contains("constraints") && params["constraints"].is_array())
        for (json& con : params["constraints"])
            if (con.is_object()) {
                con["id"] = gen.next("c");
                json nr = json::array();
                if (con.contains("refs") && con["refs"].is_array())
                    for (const json& h : con["refs"]) nr.push_back(remap_handle(h, eid_map));
                con["refs"] = nr;
            }
}

// ops.py::_append_feature — append honoring the rollback bar.
void append_feature(json& doc, const json& feat) {
    if (!doc.contains("features") || !doc["features"].is_array()) doc["features"] = json::array();
    json& feats = doc["features"];
    const std::string rb = doc.value("rollback_after", std::string());
    if (!rb.empty()) {
        int bar_pos = -1;
        for (std::size_t i = 0; i < feats.size(); ++i)
            if (feats[i].value("id", std::string()) == rb) { bar_pos = static_cast<int>(i); break; }
        if (bar_pos >= 0 && bar_pos < static_cast<int>(feats.size()) - 1) {
            feats.insert(feats.begin() + bar_pos + 1, feat);
            doc["rollback_after"] = feat["id"];
            return;
        }
    }
    feats.push_back(feat);
}

// ops.py::_paste_features — re-id every bundled feature, resolve refs, append. Returns
// (ph_to_fid, new_ids in dependency-first order). When `derived`, stamp derived_from.
std::pair<std::map<std::string, std::string>, std::vector<std::string>>
paste_features(json& doc, const json& bundle, IdGen& gen, bool derived,
               const std::string& group, const json& stamp_extra) {
    const json& order = bundle["feature_order"];
    const json& feats = bundle["features"];
    const json source_ids = bundle.contains("source_ids") ? bundle["source_ids"] : json::object();
    const std::string grp = group.empty() ? new_group_id() : group;

    std::map<std::string, std::string> ph_to_fid;
    for (const json& ph : order) ph_to_fid[ph.get<std::string>()] = gen.next("f");

    std::vector<std::string> new_ids;
    for (const json& phj : order) {
        const std::string ph = phj.get<std::string>();
        const json& src = feats[ph];
        const std::string new_fid = ph_to_fid[ph];
        json feat = new_feature_envelope(src, new_fid);
        feat["refs"] = resolve_paste_refs(src.contains("refs") ? src["refs"] : json::object(),
                                          ph_to_fid, bundle, doc);
        if (feat.value("type", std::string()) == "sketch") reid_pasted_sketch(feat, gen);
        if (derived) {
            json stamp = {
                {"doc_id", bundle.contains("source_doc_id") ? bundle["source_doc_id"] : json(nullptr)},
                {"doc_name", bundle.contains("source_doc_name") ? bundle["source_doc_name"] : json(nullptr)},
                {"feature", ph},
                {"source_feature", source_ids.contains(ph) ? source_ids[ph] : json(nullptr)},
                {"group", grp},
            };
            if (stamp_extra.is_object()) stamp.update(stamp_extra);
            feat["derived_from"] = stamp;
        }
        append_feature(doc, feat);
        new_ids.push_back(new_fid);
    }
    return {ph_to_fid, new_ids};
}

// ops.py::_bundle_or_invalid — validate the inbound bundle shape.
json bundle_or_invalid(const json& op, std::size_t index) {
    if (!op.contains("bundle") || !op["bundle"].is_object())
        throw OpError("invalid_op", "paste requires a 'bundle' object", {{"index", index}});
    const json& b = op["bundle"];
    if (b.value("kind", std::string()) != "aicad.feature_bundle/1")
        throw OpError("invalid_op", "bundle.kind must be 'aicad.feature_bundle/1'", {{"index", index}});
    if (!b.contains("feature_order") || !b["feature_order"].is_array() || b["feature_order"].empty())
        throw OpError("invalid_op", "bundle.feature_order must be a non-empty list", {{"index", index}});
    if (!b.contains("features") || !b["features"].is_object())
        throw OpError("invalid_op", "bundle.features must be an object", {{"index", index}});
    for (const json& ph : b["feature_order"]) {
        const std::string p = ph.get<std::string>();
        if (!b["features"].contains(p) || !b["features"][p].is_object())
            throw OpError("invalid_op", "bundle.features is missing feature for '" + p + "'",
                          {{"index", index}});
    }
    if (!b.contains("root") ||
        std::find(b["feature_order"].begin(), b["feature_order"].end(), b["root"]) ==
            b["feature_order"].end())
        throw OpError("invalid_op", "bundle.root must appear in feature_order", {{"index", index}});
    return b;
}

void op_copy_feature(json& doc, const json& op, std::size_t idx, IdGen& gen, json& id_map) {
    (void)gen;
    const std::string fid = op.value("id", std::string());
    bool inc = true;
    if (op.contains("include_dependencies")) {
        if (!op["include_dependencies"].is_boolean())
            throw OpError("invalid_op", "copy_feature.include_dependencies must be a bool",
                          {{"index", idx}});
        inc = op["include_dependencies"].get<bool>();
    }
    if (find_feature_c(doc, fid) == nullptr)
        throw OpError("invalid_op", "copy_feature: '" + fid + "' not found", {{"index", idx}});
    json bundle = build_feature_bundle(doc, fid, inc);
    id_map["$bundle"] = bundle;
    id_map["$bundle:" + std::to_string(idx)] = bundle;
}

void op_paste_feature(json& doc, const json& op, std::size_t idx, IdGen& gen, json& id_map) {
    const json bundle = bundle_or_invalid(op, idx);
    auto pr = paste_features(doc, bundle, gen, /*derived=*/false, "", json(nullptr));
    id_map["$" + std::to_string(idx)] = pr.first[bundle["root"].get<std::string>()];
    for (std::size_t k = 0; k < pr.second.size(); ++k)
        id_map["$" + std::to_string(idx) + "#" + std::to_string(k)] = pr.second[k];
}

void op_paste_feature_as_derived(json& doc, const json& op, std::size_t idx, IdGen& gen, json& id_map) {
    json bundle = bundle_or_invalid(op, idx);
    if (op.contains("source_doc_id") && !op["source_doc_id"].is_null() &&
        (!bundle.contains("source_doc_id") || bundle["source_doc_id"].is_null()))
        bundle["source_doc_id"] = op["source_doc_id"];
    auto pr = paste_features(doc, bundle, gen, /*derived=*/true, "", json(nullptr));
    id_map["$" + std::to_string(idx)] = pr.first[bundle["root"].get<std::string>()];
    for (std::size_t k = 0; k < pr.second.size(); ++k)
        id_map["$" + std::to_string(idx) + "#" + std::to_string(k)] = pr.second[k];
}

// ops.py::_shift_entity_any — deep-copy a sketch entity of ANY type shifted by (dx, dy)
// (id/construction stripped — the caller restamps them). Translates every point-shaped field.
json shift_entity_any(const json& src, double dx, double dy) {
    auto shift_pt = [&](const json& p) -> json {
        if (p.is_array() && p.size() == 2 && p[0].is_number() && p[1].is_number())
            return json::array({p[0].get<double>() + dx, p[1].get<double>() + dy});
        return p;
    };
    json out = json::object();
    for (auto it = src.begin(); it != src.end(); ++it)
        if (it.key() != "id" && it.key() != "construction") out[it.key()] = it.value();
    for (const char* key : {"p", "p1", "p2", "center", "vertex", "focus", "apex", "p0"})
        if (out.contains(key)) out[key] = shift_pt(out[key]);
    if (out.contains("points") && out["points"].is_array()) {
        json pts = json::array();
        for (const json& p : out["points"]) pts.push_back(shift_pt(p));
        out["points"] = pts;
    }
    return out;
}

// ops.py::_op_sketch_paste_entities (Wave 4B) — paste a bundle of sketch entities (+ their
// constraints) into `sketch`, shifted by (offset_x, offset_y), with FRESH ids. Constraints
// internal to the pasted set are kept (handles re-pointed); constraints reaching OUTSIDE the
// set are DROPPED. Records the first new entity id under $<index> and the rest under $<index>#k.
void op_sketch_paste_entities(json& doc, const json& op, std::size_t idx, IdGen& gen, json& id_map) {
    json& sk = require_sketch_feature(doc, op, id_map);
    if (!op.contains("entities_bundle") || !op["entities_bundle"].is_object())
        throw OpError("invalid_op", "sketch_paste_entities requires an 'entities_bundle' object",
                      {{"index", idx}});
    const json& eb = op["entities_bundle"];
    if (!eb.contains("entities") || !eb["entities"].is_array() || eb["entities"].empty())
        throw OpError("invalid_op", "entities_bundle.entities must be a non-empty list",
                      {{"index", idx}});
    const json src_cons = eb.contains("constraints") ? eb["constraints"] : json::array();
    if (!src_cons.is_array())
        throw OpError("invalid_op", "entities_bundle.constraints must be a list", {{"index", idx}});
    if (!op.contains("offset_x") || !op["offset_x"].is_number())
        throw OpError("invalid_op", "sketch_paste_entities requires a numeric 'offset_x'",
                      {{"index", idx}});
    if (!op.contains("offset_y") || !op["offset_y"].is_number())
        throw OpError("invalid_op", "sketch_paste_entities requires a numeric 'offset_y'",
                      {{"index", idx}});
    const double dx = op["offset_x"].get<double>(), dy = op["offset_y"].get<double>();

    std::map<std::string, std::string> eid_map;
    std::vector<json> new_entities;
    for (const json& src : eb["entities"]) {
        if (!src.is_object() || !src.contains("id") || !src["id"].is_string())
            throw OpError("invalid_op", "entities_bundle.entities: each entity needs an 'id'",
                          {{"index", idx}});
        const std::string neid = gen.next("e");
        eid_map[src["id"].get<std::string>()] = neid;
        json geom = shift_entity_any(src, dx, dy);
        geom["id"] = neid;
        geom["construction"] = src.value("construction", false);
        new_entities.push_back(geom);
    }
    for (const json& ne : new_entities) sk["params"]["entities"].push_back(ne);

    // Keep only constraints whose handles ALL land inside the pasted set (drop external/origin).
    json& cons = sk["params"]["constraints"];
    for (const json& con : src_cons) {
        if (!con.is_object()) continue;
        const json handles = con.contains("refs") ? con["refs"] : json::array();
        bool all_in = handles.is_array() && !handles.empty();
        if (all_in)
            for (const json& h : handles)
                if (!(h.is_string() && eid_map.count(handle_base(h.get<std::string>())))) {
                    all_in = false;
                    break;
                }
        if (!all_in) continue;
        json nc = con;
        nc["id"] = gen.next("c");
        json nr = json::array();
        for (const json& h : handles) nr.push_back(remap_handle(h, eid_map));
        nc["refs"] = nr;
        cons.push_back(nc);
    }

    std::vector<std::string> new_ids;
    for (const json& ne : new_entities) new_ids.push_back(ne["id"].get<std::string>());
    id_map["$" + std::to_string(idx)] = new_ids.empty() ? json("") : json(new_ids[0]);
    for (std::size_t k = 1; k < new_ids.size(); ++k)
        id_map["$" + std::to_string(idx) + "#" + std::to_string(k)] = new_ids[k];
}

// ops.py::refresh_derived_group — splice a FRESH source bundle over an existing derived
// group IN PLACE (ids + tree positions preserved). Returns {refreshed, added, stale}.
json refresh_derived_group(json& doc, const std::string& group, const json& bundle, IdGen& gen) {
    const json& order = bundle["feature_order"];
    const json& feats = bundle["features"];
    const json source_ids = bundle.contains("source_ids") ? bundle["source_ids"] : json::object();
    const json grp_doc_id = bundle.contains("source_doc_id") ? bundle["source_doc_id"] : json(nullptr);
    const json grp_doc_name = bundle.contains("source_doc_name") ? bundle["source_doc_name"] : json(nullptr);

    std::vector<std::string> member_ids;
    std::map<std::string, std::string> by_source;  // source_feature -> existing target id
    if (doc.contains("features") && doc["features"].is_array())
        for (const json& f : doc["features"]) {
            if (!f.is_object()) continue;
            if (!f.contains("derived_from") || !f["derived_from"].is_object()) continue;
            const json& df = f["derived_from"];
            if (df.value("group", std::string()) != group) continue;
            const std::string fid = f.value("id", std::string());
            member_ids.push_back(fid);
            if (df.contains("source_feature") && df["source_feature"].is_string()) {
                const std::string sf = df["source_feature"].get<std::string>();
                if (!by_source.count(sf)) by_source[sf] = fid;
            }
        }

    std::map<std::string, std::string> ph_to_fid;
    std::map<std::string, bool> ph_is_new;
    for (const json& phj : order) {
        const std::string ph = phj.get<std::string>();
        const std::string src_fid =
            (source_ids.contains(ph) && source_ids[ph].is_string()) ? source_ids[ph].get<std::string>() : "";
        auto it = (!src_fid.empty()) ? by_source.find(src_fid) : by_source.end();
        if (it != by_source.end()) {
            ph_to_fid[ph] = it->second;
            ph_is_new[ph] = false;
        } else {
            ph_to_fid[ph] = gen.next("f");
            ph_is_new[ph] = true;
        }
    }

    json refreshed = json::array(), added = json::array();
    for (const json& phj : order) {
        const std::string ph = phj.get<std::string>();
        const json& src = feats[ph];
        const std::string target_fid = ph_to_fid[ph];
        json stamp = {
            {"doc_id", grp_doc_id}, {"doc_name", grp_doc_name}, {"feature", ph},
            {"source_feature", source_ids.contains(ph) ? source_ids[ph] : json(nullptr)},
            {"group", group},
        };
        if (ph_is_new[ph]) {
            json feat = new_feature_envelope(src, target_fid);
            feat["refs"] = resolve_paste_refs(src.contains("refs") ? src["refs"] : json::object(),
                                              ph_to_fid, bundle, doc);
            if (feat.value("type", std::string()) == "sketch") reid_pasted_sketch(feat, gen);
            feat["derived_from"] = stamp;
            append_feature(doc, feat);
            added.push_back(target_fid);
        } else {
            json* existing = find_feature(doc, target_fid);
            json rebuilt = new_feature_envelope(src, target_fid);
            rebuilt["refs"] = resolve_paste_refs(src.contains("refs") ? src["refs"] : json::object(),
                                                 ph_to_fid, bundle, doc);
            if (rebuilt.value("type", std::string()) == "sketch") reid_pasted_sketch(rebuilt, gen);
            rebuilt["name"] = existing->value("name", rebuilt.value("name", std::string()));
            *existing = rebuilt;
            (*existing)["derived_from"] = stamp;
            existing->erase("derived_stale");
            refreshed.push_back(target_fid);
        }
    }

    std::set<std::string> fresh_sources;
    for (const json& phj : order) {
        const std::string ph = phj.get<std::string>();
        if (source_ids.contains(ph) && source_ids[ph].is_string())
            fresh_sources.insert(source_ids[ph].get<std::string>());
    }
    std::set<std::string> refreshed_set;
    for (const json& r : refreshed) refreshed_set.insert(r.get<std::string>());
    json stale = json::array();
    for (const std::string& mid : member_ids) {
        if (refreshed_set.count(mid)) continue;
        json* f = find_feature(doc, mid);
        if (!f) continue;
        std::string sf;
        if (f->contains("derived_from") && (*f)["derived_from"].is_object() &&
            (*f)["derived_from"].contains("source_feature") &&
            (*f)["derived_from"]["source_feature"].is_string())
            sf = (*f)["derived_from"]["source_feature"].get<std::string>();
        if (!fresh_sources.count(sf)) {
            (*f)["derived_stale"] = {{"reason", "source_feature_deleted"},
                                     {"source_feature", sf.empty() ? json(nullptr) : json(sf)},
                                     {"doc_id", grp_doc_id}, {"doc_name", grp_doc_name}};
            stale.push_back(mid);
        }
    }
    return json{{"refreshed", refreshed}, {"added", added}, {"stale", stale}};
}

// session.py::_resolve_source_session — find the live source session for a derived link's
// source_doc_id, matching BOTH the registry key (session.doc_id) and the doc's internal id.
// Caller holds g_mtx. Returns nullptr if the source doc is closed/absent.
Session* resolve_source_session(const json& src_doc_id) {
    if (src_doc_id.is_null()) return nullptr;
    if (src_doc_id.is_string()) {
        auto it = g_sessions.find(src_doc_id.get<std::string>());
        if (it != g_sessions.end()) return &it->second;
    }
    for (auto& kv : g_sessions)
        if (kv.second.doc.is_object() &&
            (kv.second.doc.contains("id") ? kv.second.doc["id"] : json(nullptr)) == src_doc_id)
            return &kv.second;
    return nullptr;
}

// session.py::_group_root_source — the source feature to re-bundle a group from: the member
// whose closure (transitively) covers the most of the group's source features.
std::string group_root_source(const json& rec, const json& source_doc) {
    std::vector<std::string> src_ids;
    for (const json& m : rec["members"])
        if (m.contains("source_feature") && m["source_feature"].is_string())
            src_ids.push_back(m["source_feature"].get<std::string>());
    if (src_ids.empty()) return "";
    std::set<std::string> src_set(src_ids.begin(), src_ids.end());
    std::string best;
    std::pair<int, int> best_key{-1, -1};
    for (const std::string& cand : src_ids) {
        if (find_feature_c(source_doc, cand) == nullptr) continue;
        std::set<std::string> closure;
        try {
            closure = feature_refs_closure(source_doc, cand).second;
        } catch (...) {
            continue;
        }
        int inter = 0;
        for (const std::string& x : src_set)
            if (closure.count(x)) ++inter;
        std::pair<int, int> key{inter, static_cast<int>(closure.size())};
        if (key > best_key) {
            best_key = key;
            best = cand;
        }
    }
    return best;
}

// session.py::_mark_group_stale — stamp derived_stale on every member of `group`.
json mark_group_stale(Session& s, const std::string& group, const std::string& reason,
                      const json& detail) {
    json marked = json::array();
    if (!s.doc.contains("features") || !s.doc["features"].is_array()) return marked;
    for (json& f : s.doc["features"]) {
        if (!f.is_object()) continue;
        if (!f.contains("derived_from") || !f["derived_from"].is_object()) continue;
        if (f["derived_from"].value("group", std::string()) != group) continue;
        json st = {{"reason", reason}};
        if (detail.is_object()) st.update(detail);
        f["derived_stale"] = st;
        marked.push_back(f.contains("id") ? f["id"] : json(nullptr));
    }
    return marked;
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
    else if (k == "copy_feature")               op_copy_feature(doc, op, idx, gen, id_map);
    else if (k == "paste_feature")              op_paste_feature(doc, op, idx, gen, id_map);
    else if (k == "paste_feature_as_derived")   op_paste_feature_as_derived(doc, op, idx, gen, id_map);
    else if (k == "sketch_paste_entities")       op_sketch_paste_entities(doc, op, idx, gen, id_map);
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

// Compile the session's LIVE part doc, reusing the cached result when the doc is unchanged
// since the last compile. The cache is keyed on `revision`: every geometry-changing op bumps
// it (or starts a fresh session), and all callers hold the doc lock, so the cache is never
// stale. This turns repeated read-only queries on a static doc (clicking faces, expanding a
// face into its edges, refreshing the owner map) from "recompile the whole feature tree each
// time" into a single compile amortized across the whole interaction. Part docs only — the
// assembly path compiles via rebuild_assembly / asm_result, not compile_document.
const CompileResult& ensure_compiled(Session& s) {
    if (!s.compiled || s.compiled_rev != s.revision) {
        s.compiled =
            std::make_shared<CompileResult>(compile_document(s.doc, env_from_doc(s.doc)));
        s.compiled_rev = s.revision;
    }
    return *s.compiled;
}

json ops_response(Session& s, const CompileResult& r, const json& id_map, bool with_tess);

// Build the ops-response AND warm the read-only compile cache from the SAME compile, so the
// first pick right after an open/edit/undo/rebuild reuses it instead of recompiling the whole
// tree (the post-open first-click hitch). `r` is taken by value and MOVED into the cache after
// ops_response reads it — no extra copy of the aicm/shapes. Call this ONLY where `r` was just
// compiled from the CURRENT live s.doc at the CURRENT s.revision (never the apply-batch rollback
// path, where r is the rejected tree and the restored doc's cache is already valid).
json warm_and_respond(Session& s, CompileResult r, const json& id_map, bool with_tess) {
    json resp = ops_response(s, r, id_map, with_tess);
    s.compiled = std::make_shared<CompileResult>(std::move(r));
    s.compiled_rev = s.revision;
    return resp;
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
                      {"warning", st.warning.empty() ? json(nullptr) : json(st.warning)},
                      {"dangling_refs", json::array()},
                      {"rolled_back", false}});
    }
    json resp;
    resp["revision"] = s.revision;
    resp["id_map"] = id_map;
    resp["tree"] = s.doc;
    resp["owner_map"] = r.owner_map.is_null() ? json::object() : r.owner_map;
    // Displayable reference geometry (datum axes/planes/points/csys) — empty for docs with no
    // datum features. The host draws these as overlays; without it a created datum is invisible.
    resp["datums"] = r.datums;
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
    return warm_and_respond(s, std::move(r), json::object(), /*with_tess=*/true);
}

// ops.py::is_readonly_batch — true iff `ops` is non-empty and EVERY op is read-only (only
// copy_feature today). Such a batch surfaces its result only via the id_map side-channel, so
// the session skips the undo snapshot, the revision bump, and the dirty flag (session.py).
bool is_readonly_batch(const json& ops) {
    if (!ops.is_array() || ops.empty()) return false;
    for (const json& op : ops)
        if (!op.is_object() || op.value("op", std::string()) != "copy_feature") return false;
    return true;
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

    // Read-only fast-path: a batch of only read-only ops (copy_feature) makes no document
    // change — it only returns its result through id_map ($bundle). Skip the undo snapshot,
    // the revision bump, and the dirty flag; the live doc is left untouched (session.py).
    if (is_readonly_batch(ops)) {
        if (s.is_assembly()) {
            rebuild_assembly(s);
            return ops_response_assembly(s, id_map);
        }
        CompileResult r = compile_document(s.doc, env_from_doc(s.doc));
        return ops_response(s, r, id_map, /*with_tess=*/true);
    }

    // Bugs C2/C3/C4 — failed-batch undo corruption. Below we optimistically commit the
    // edit (push the undo snapshot, swap in `working`, bump the revision) BEFORE compiling.
    // If the batch then fails to compile (a feature errored — fillet that can't apply, a
    // boolean that produced nothing, etc., so r.ok==false), we MUST NOT leave that wreckage
    // behind: a ghost revision on the undo stack + an errored feature in the live tree make
    // later undo/redo step through phantom states and block a subsequent edit_feature.
    // So we snapshot the EXACT pre-batch session state (doc, revision, both stacks, dirty)
    // and, on a rejected compile, restore all of it verbatim — no ghost revision, no errored
    // feature persisted, stacks untouched. We then RETURN the normal ops-response (with the
    // per-feature failure surfaced in r.statuses), NOT throw: doc_apply_ops's contract is an
    // in-band {ok:false} rejection at rc 0 (the original pre-C2/C3/C4 return path), so the
    // Python add_features wrapper sees the failing feature_status, reverts, and reports
    // rolled_back:true. The SUCCESSFUL path is byte-for-byte identical to before (push undo
    // => clears redo, swap doc, bump revision, return the compiled response).
    const json pre_doc = s.doc;  // deep copy of the live tree (cheap vs. a corrupted doc)
    const long pre_revision = s.revision;
    const std::vector<std::string> pre_undo = s.undo_stack;
    const std::vector<std::string> pre_redo = s.redo_stack;
    const bool pre_dirty = s.dirty;
    auto restore_pre_batch = [&]() {
        s.doc = pre_doc;
        s.revision = pre_revision;
        s.undo_stack = pre_undo;
        s.redo_stack = pre_redo;
        s.dirty = pre_dirty;
    };

    if (!coalesce) push_undo(s);  // NOTE: push_undo clears the redo stack
    s.doc = std::move(working);
    s.revision += 1;
    s.dirty = true;
    if (s.is_assembly()) {
        rebuild_assembly(s);
        return ops_response_assembly(s, id_map);
    }
    CompileResult r = compile_document(s.doc, env_from_doc(s.doc));
    if (!r.ok) {
        // Rejected batch: restore the document AND the undo/redo stacks to exactly their
        // pre-batch state — no ghost revision, no errored feature persisted, stacks untouched
        // (the load-bearing C2/C3/C4 correctness fix). We do NOT throw: doc_apply_ops returns
        // an in-band rejection at rc 0 (the original contract), so the caller reads the
        // per-feature failure out of feature_status. `r` was compiled against the failing
        // `working` tree, so r.statuses carries the offending feature (ok:false + error);
        // ops_response surfaces it while reporting the restored (pre-batch) revision/tree —
        // nothing from the batch was kept. The Python add_features wrapper then sees ok==false
        // (some feature_status.ok is false), reverts its checkpoint, and reports rolled_back:true.
        restore_pre_batch();
        return ops_response(s, r, id_map, /*with_tess=*/true);
    }
    return warm_and_respond(s, std::move(r), id_map, /*with_tess=*/true);
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
// frames; a plane-FEATURE id (offset/midplane/angle/etc.) is resolved by recompiling
// the doc and reading that feature's resolved plane out of CompileResult.datums (which
// now surfaces per-feature plane geometry — origin + x_dir/y_dir/normal). Only the
// reserved factory frames are tried first; an unknown ref falls back to TOP. Throws on
// no plane ref.
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
    if (bare == "top")   return aicad::geom::SketchPlane::top();
    if (bare == "front") return aicad::geom::SketchPlane::front();
    if (bare == "right") return aicad::geom::SketchPlane::right();

    // Non-reserved ref: a created datum-plane feature (offset/midplane/angle/three_point/
    // …). Recompile and read its resolved frame from the datums side-channel so the sketch
    // lands ON that plane (origin + offset) instead of silently falling back to TOP — which
    // was the long-standing "offset plane sketches on the base plane" bug.
    if (!name.empty()) {
        CompileResult r = compile_document(s.doc, env_from_doc(s.doc));
        if (r.datums.is_array()) {
            auto v3 = [](const json& a, const Eigen::Vector3d& dflt) -> Eigen::Vector3d {
                if (a.is_array() && a.size() == 3 && a[0].is_number())
                    return Eigen::Vector3d(a[0].get<double>(), a[1].get<double>(),
                                           a[2].get<double>());
                return dflt;
            };
            for (const json& d : r.datums) {
                if (!d.is_object() || d.value("id", std::string()) != name) continue;
                const std::string k = d.value("kind", std::string());
                if (k != "plane" && k != "csys") break;  // axis/point/curve aren't sketchable
                aicad::geom::SketchPlane p;
                p.origin = v3(d.value("origin", json()), Eigen::Vector3d(0, 0, 0));
                p.uAxis  = v3(d.value("x_dir",  json()), Eigen::Vector3d(1, 0, 0));
                p.vAxis  = v3(d.value("y_dir",  json()), Eigen::Vector3d(0, 1, 0));
                p.normal = v3(d.value("normal", json()), Eigen::Vector3d(0, 0, 1));
                return p;
            }
        }
    }
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

// ----------------------------------------------------------------- crash recovery
// Drop an orphaned g_mtx lock left held by THIS thread after a Windows structured
// exception (an access violation) unwound past a DocLock without running its C++
// destructor under /EHsc. Called from the libaicad C dispatch boundary's __except
// filter after such a fault, so the worker thread doesn't EDEADLK on its next call.
//
// SAFETY: an SEH unwind returns control to this same thread with the OS mutex STILL
// owned by it (the lock_guard destructor was skipped, so no unlock ran). Calling
// unlock() from the owning thread is therefore valid and exactly undoes the orphaned
// lock(). We gate on g_mtx_held && g_mtx_owner == this-thread so we NEVER unlock a
// mutex this thread doesn't own (which would be UB) and NEVER touch a lock another
// thread legitimately holds. Idempotent: a second call no-ops once the flag is clear.
bool force_release_doc_lock() {
    if (!g_mtx_held.load(std::memory_order_acquire)) return false;
    // g_mtx_owner is only meaningful while held; it was last written under the lock by
    // the DocLock that is now orphaned on THIS thread, so the read is consistent here.
    if (g_mtx_owner != std::this_thread::get_id()) return false;
    g_mtx_held.store(false, std::memory_order_release);
    g_mtx.unlock();
    return true;
}

// ----------------------------------------------------------------- public API
json doc_new(const std::string& kind, const std::string& name) {
    const std::string k = kind.empty() ? std::string("part") : kind;
    if (k != "part" && k != "assembly")
        throw OpError("invalid_op", "doc kind must be part|assembly, got '" + k + "'",
                      {{"kind", kind}, {"reason", "bad kind"}});
    DocLock lk;
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
    DocLock lk;
    Session& s = require_session(doc_id);
    return apply_ops_locked(s, base_revision, ops, coalesce);
}

json doc_dry_run_ops(const std::string& doc_id, const json& ops) {
    DocLock lk;
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
        // apply_ops_locked THROWS on a structural/validation rejection (caught below) but
        // RETURNS an in-band rejection on a compile failure (per the batch-atomicity contract:
        // the doomed batch is restored, then its per-feature failure is surfaced in
        // rebuild.feature_status). The dry-run gate must report {ok:false} for BOTH, so we
        // inspect the returned feature_status for any ok:false and compose the same error
        // string shape (the failing features' messages joined) the throw path produces.
        json resp = apply_ops_locked(scratch, -1, ops, /*coalesce=*/true);
        std::vector<std::string> feat_errs;
        if (resp.contains("rebuild") && resp["rebuild"].is_object() &&
            resp["rebuild"].contains("feature_status") &&
            resp["rebuild"]["feature_status"].is_array())
            for (const json& st : resp["rebuild"]["feature_status"])
                if (st.is_object() && !st.value("ok", true)) {
                    std::string emsg = st.contains("error") && st["error"].is_string()
                                           ? st["error"].get<std::string>()
                                           : std::string("feature compile failed");
                    feat_errs.push_back(st.value("id", std::string()) + ": " + emsg);
                }
        if (!feat_errs.empty()) {
            std::string joined;
            for (std::size_t i = 0; i < feat_errs.size() && i < 5; ++i) {
                if (i) joined += "; ";
                joined += feat_errs[i];
            }
            return json{{"ok", false}, {"error", "op batch rejected: " + joined}};
        }
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
    DocLock lk;
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
    return warm_and_respond(s, std::move(r), json::object(), /*with_tess=*/true);
}

json doc_redo(const std::string& doc_id) {
    DocLock lk;
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
    return warm_and_respond(s, std::move(r), json::object(), /*with_tess=*/true);
}

json doc_tree(const std::string& doc_id) {
    DocLock lk;
    Session& s = require_session(doc_id);
    if (s.is_assembly()) {
        json resp;
        resp["revision"] = s.revision;
        resp["tree"] = s.doc;
        resp["owner_map"] = json::object();
        return resp;
    }
    // No revision bump / no tess, but DO carry the owner_map (host picking) — reuse the cached
    // compile (recomputes only when the doc changed since the last query).
    const CompileResult& r = ensure_compiled(s);
    json resp;
    resp["revision"] = s.revision;
    resp["tree"] = s.doc;
    resp["owner_map"] = r.owner_map.is_null() ? json::object() : r.owner_map;
    return resp;
}

json doc_live_source(const std::string& doc_id) {
    // Non-throwing peek at an open session's current tree + env (the associative source
    // for drawings / derived re-pulls). A missing doc returns null so the caller keeps
    // its last good snapshot rather than crashing when the source was closed.
    DocLock lk;
    auto it = g_sessions.find(doc_id);
    if (it == g_sessions.end()) return json(nullptr);
    Session& s = it->second;
    return json{{"tree", s.doc}, {"env", env_from_doc(s.doc)}, {"revision", s.revision}};
}

json doc_rebuild(const std::string& doc_id) {
    DocLock lk;
    Session& s = require_session(doc_id);
    if (s.is_assembly()) {
        rebuild_assembly(s);
        return ops_response_assembly(s, json::object());
    }
    CompileResult r = compile_document(s.doc, env_from_doc(s.doc));
    return warm_and_respond(s, std::move(r), json::object(), /*with_tess=*/true);
}

// ============================================================ C20 assembly RPCs
// doc_solve_mates: re-solve the assembly mates (optionally with a drag soft-target),
// persist the solved transforms, bump the revision (one undoable step), and return the
// ops-response (port of session.py::solve_mates). doc_set_explode_state toggles the
// explode view and re-tessellates without a revision bump.

json doc_solve_mates(const std::string& doc_id, const json& drag) {
    DocLock lk;
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
    DocLock lk;
    Session& s = require_session(doc_id);
    if (!s.is_assembly())
        throw OpError("invalid_op", "doc_set_explode_state: document is not an assembly",
                      {{"reason", "not an assembly"}});
    s.explode_active = active;
    s.explode_factor = std::max(0.0, std::min(1.0, factor));
    if (!s.asm_result) rebuild_assembly(s);
    return ops_response_assembly(s, json::object());  // view-only: no revision bump
}

// ============================================================ simulation studies
// FEA/CFD study setups live as a top-level `studies` array in the document JSON so
// they persist inside the .step+ container automatically (stepplus_save dumps the
// whole doc). The core treats each study as OPAQUE JSON — only the `id` is core-
// assigned — and NEVER rebuilds geometry, so these RPCs are cheap and work for both
// part and assembly docs. The host fills/interprets every other field.

json doc_sim_study_list(const std::string& doc_id) {
    DocLock lk;
    Session& s = require_session(doc_id);
    json resp;
    resp["studies"] = s.doc.value("studies", json::array());
    resp["revision"] = s.revision;
    return resp;  // read-only: no revision bump
}

json doc_sim_study_add(const std::string& doc_id, const json& study) {
    DocLock lk;
    Session& s = require_session(doc_id);
    json entry = study.is_object() ? study : json::object();
    std::string id = entry.value("id", std::string());
    if (id.empty()) {
        id = new_study_id();
        entry["id"] = id;
    }
    if (!s.doc.contains("studies") || !s.doc["studies"].is_array())
        s.doc["studies"] = json::array();
    s.doc["studies"].push_back(entry);
    push_undo(s);
    s.revision += 1;
    s.dirty = true;
    json resp;
    resp["studies"] = s.doc["studies"];
    resp["revision"] = s.revision;
    resp["study_id"] = id;
    return resp;
}

json doc_sim_study_edit(const std::string& doc_id, const std::string& study_id,
                        const json& study) {
    DocLock lk;
    Session& s = require_session(doc_id);
    bool found = false;
    if (s.doc.contains("studies") && s.doc["studies"].is_array()) {
        for (json& el : s.doc["studies"]) {
            if (el.is_object() && el.value("id", std::string()) == study_id) {
                json replacement = study.is_object() ? study : json::object();
                replacement["id"] = study_id;  // id can't be lost or changed
                el = std::move(replacement);
                found = true;
                break;
            }
        }
    }
    if (!found)
        throw OpError("invalid_op", "no such study: " + study_id,
                      {{"reason", "unknown study"}, {"study_id", study_id}});
    push_undo(s);
    s.revision += 1;
    s.dirty = true;
    json resp;
    resp["studies"] = s.doc["studies"];
    resp["revision"] = s.revision;
    return resp;
}

json doc_sim_study_delete(const std::string& doc_id, const std::string& study_id) {
    DocLock lk;
    Session& s = require_session(doc_id);
    bool found = false;
    if (s.doc.contains("studies") && s.doc["studies"].is_array()) {
        json kept = json::array();
        for (const json& el : s.doc["studies"]) {
            if (el.is_object() && el.value("id", std::string()) == study_id) {
                found = true;
                continue;
            }
            kept.push_back(el);
        }
        if (found) s.doc["studies"] = std::move(kept);
    }
    if (!found)
        throw OpError("invalid_op", "no such study: " + study_id,
                      {{"reason", "unknown study"}, {"study_id", study_id}});
    push_undo(s);
    s.revision += 1;
    s.dirty = true;
    json resp;
    resp["studies"] = s.doc["studies"];
    resp["revision"] = s.revision;
    return resp;
}

// World-placed component bodies for interference / BOM (port of session.py::
// asm_world_bodies / asm_component_bodies). Returned as JSON metadata + a temp STEP
// export per body is overkill; instead we surface per-body world bbox + name, which is
// what the parity harness compares and what eval_interference's assembly branch consumes.
json doc_asm_world_bodies(const std::string& doc_id, bool explode) {
    DocLock lk;
    Session& s = require_session(doc_id);
    if (!s.is_assembly()) {
        // Multi-body PART fallback (Issue 9). A part assembled via import_part (several
        // imported + transform-placed bodies) is not a formal assembly doc, but the caller
        // still wants to see where each body sits in the shared frame. Compile and report each
        // surviving body's bbox — already in world coordinates (a part's transforms place
        // bodies absolutely). explode is an assembly-only concept here, so it is ignored.
        CompileResult r = compile_document(s.doc, env_from_doc(s.doc));
        json out = json::array();
        for (std::size_t i = 0; i < r.shapes.size(); ++i) {
            const TopoDS_Shape& world = r.shapes[i];
            if (world.IsNull()) continue;
            Bnd_Box box;
            BRepBndLib::Add(world, box);
            json bb = nullptr;
            if (!box.IsVoid()) {
                double xa, ya, za, xb, yb, zb;
                box.Get(xa, ya, za, xb, yb, zb);
                bb = {{"min", {xa, ya, za}}, {"max", {xb, yb, zb}},
                      {"size", {xb - xa, yb - ya, zb - za}}};
            }
            const std::string nm =
                i < r.bodies_info.size() ? r.bodies_info[i].name : ("Body" + std::to_string(i + 1));
            out.push_back({{"name", nm}, {"component", nm}, {"bbox", bb}});
        }
        return json{{"bodies", out}, {"multibody_part", true}};
    }
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

json doc_asm_bom_components(const std::string& doc_id) {
    // Non-throwing: a missing / part / closed source returns an empty array so drawing_bom
    // degrades to an empty BOM rather than crashing (mirrors the oracle's try/except).
    DocLock lk;
    auto it = g_sessions.find(doc_id);
    if (it == g_sessions.end()) return json::array();
    Session& s = it->second;
    if (!s.is_assembly()) return json::array();
    if (!s.asm_result) rebuild_assembly(s);
    if (!s.asm_result) return json::array();
    // Count component-pattern instances seeded from each real component ("<seed>#<k>").
    std::map<std::string, int> instance_counts;
    for (const AsmBodyOut& b : s.asm_result->bodies) {
        auto h = b.component_id.find('#');
        if (h != std::string::npos) instance_counts[b.component_id.substr(0, h)] += 1;
    }
    // Source path per real component id from doc["components"].
    std::map<std::string, std::string> src_by_id;
    if (s.doc.contains("components") && s.doc["components"].is_array())
        for (const json& c : s.doc["components"]) {
            std::string path = (c.contains("source") && c["source"].is_object())
                                   ? c["source"].value("path", std::string())
                                   : std::string();
            src_by_id[c.value("id", std::string())] = path;
        }
    json out = json::array();
    for (const AsmBodyOut& b : s.asm_result->bodies) {
        if (b.component_id.find('#') != std::string::npos) continue;  // pattern instance
        auto ic = instance_counts.find(b.component_id);
        auto sp = src_by_id.find(b.component_id);
        out.push_back({{"component", b.component_id},
                       {"name", b.name},
                       {"source", sp == src_by_id.end() ? std::string() : sp->second},
                       {"quantity", 1 + (ic == instance_counts.end() ? 0 : ic->second)}});
    }
    return out;
}

bool doc_close(const std::string& doc_id) {
    DocLock lk;
    return g_sessions.erase(doc_id) > 0;
}

json doc_list() {
    DocLock lk;
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
    DocLock lk;
    Session& s = require_session(doc_id);
    const std::string cid = new_checkpoint_id();
    s.checkpoints[cid] = s.doc.dump();
    return json{{"checkpoint", cid}};
}

json doc_revert(const std::string& doc_id, const std::string& checkpoint_id) {
    DocLock lk;
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
    return warm_and_respond(s, std::move(r), json::object(), /*with_tess=*/true);
}

// Live derived re-pull — C++ port of engine.doc.session.refresh_derived (Wave 4C).
// Re-bundles each derived group's source feature (+ closure) from the LIVE source session
// and splices it back over the existing derived copies in place (ids + positions preserved).
// `id` empty == refresh every derived group; otherwise narrow to the group that feature `id`
// belongs to (a non-derived id is a clean invalid_op). Mutating: one undo snapshot + revision
// bump on any change. A closed source / deleted source feature leaves the group at last-good
// and stamps derived_stale (never crashes). Returns the ops-response + a `derived` summary.
json doc_refresh_derived(const std::string& doc_id, const std::string& id) {
    DocLock lk;
    Session& s = require_session(doc_id);

    // _derived_groups: group -> {source_doc_id, source_doc_name, members:[...]}, first-seen order.
    std::vector<std::string> group_order;
    std::map<std::string, json> groups;
    if (s.doc.contains("features") && s.doc["features"].is_array())
        for (const json& f : s.doc["features"]) {
            if (!f.is_object()) continue;
            if (!f.contains("derived_from") || !f["derived_from"].is_object()) continue;
            const json& df = f["derived_from"];
            if (!df.contains("group") || !df["group"].is_string()) continue;
            const std::string grp = df["group"].get<std::string>();
            if (!groups.count(grp)) {
                group_order.push_back(grp);
                groups[grp] = {{"group", grp},
                               {"source_doc_id", df.contains("doc_id") ? df["doc_id"] : json(nullptr)},
                               {"source_doc_name", df.contains("doc_name") ? df["doc_name"] : json(nullptr)},
                               {"members", json::array()}};
            }
            groups[grp]["members"].push_back(
                {{"id", f.contains("id") ? f["id"] : json(nullptr)},
                 {"source_feature", df.contains("source_feature") ? df["source_feature"] : json(nullptr)},
                 {"is_root", df.value("is_root", false)}});
        }

    if (!id.empty()) {
        const json* target = find_feature_c(s.doc, id);
        const json* df = (target && target->contains("derived_from") && (*target)["derived_from"].is_object())
                             ? &(*target)["derived_from"] : nullptr;
        std::string grp = (df && df->contains("group") && (*df)["group"].is_string())
                              ? (*df)["group"].get<std::string>() : "";
        if (grp.empty() || !groups.count(grp))
            throw OpError("invalid_op", "refresh_derived: feature '" + id + "' is not a derived feature",
                          {{"reason", "not derived"}, {"id", id}});
        std::vector<std::string> only{grp};
        std::map<std::string, json> narrowed{{grp, groups[grp]}};
        group_order = only;
        groups = narrowed;
    }

    if (groups.empty()) {
        CompileResult r = compile_document(s.doc, env_from_doc(s.doc));
        json resp = ops_response(s, r, json::object(), /*with_tess=*/true);
        resp["derived"] = {{"groups", json::array()}, {"note", "no derived features in document"}};
        return resp;
    }

    // Snapshot once up front so the whole multi-group refresh is a SINGLE undo step.
    push_undo(s);
    IdGen gen(s.doc);
    json summaries = json::array();
    bool any_change = false;

    for (const std::string& grp : group_order) {
        const json& rec = groups[grp];
        const json src_doc_id = rec["source_doc_id"];
        Session* src = resolve_source_session(src_doc_id);
        if (src == nullptr) {
            json marked = mark_group_stale(s, grp, "source_doc_closed",
                                           {{"doc_id", src_doc_id}, {"doc_name", rec["source_doc_name"]}});
            any_change = any_change || !marked.empty();
            summaries.push_back({{"group", grp}, {"status", "source_doc_closed"},
                                 {"source_doc_id", src_doc_id}, {"stale", marked}});
            continue;
        }
        const std::string root_src = group_root_source(rec, src->doc);
        if (root_src.empty() || find_feature_c(src->doc, root_src) == nullptr) {
            json marked = mark_group_stale(s, grp, "source_feature_deleted",
                                           {{"doc_id", src_doc_id}, {"doc_name", rec["source_doc_name"]}});
            any_change = any_change || !marked.empty();
            summaries.push_back({{"group", grp}, {"status", "source_feature_deleted"},
                                 {"source_doc_id", src_doc_id}, {"stale", marked}});
            continue;
        }
        try {
            json bundle = build_feature_bundle(src->doc, root_src, /*include_dependencies=*/true);
            json res = refresh_derived_group(s.doc, grp, bundle, gen);
            any_change = true;
            json summ = {{"group", grp}, {"status", "refreshed"}, {"source_doc_id", src_doc_id}};
            summ.update(res);
            summaries.push_back(summ);
        } catch (const OpError& exc) {
            json marked = mark_group_stale(s, grp, "refresh_failed",
                                           {{"doc_id", src_doc_id}, {"error", exc.what()}});
            any_change = any_change || !marked.empty();
            summaries.push_back({{"group", grp}, {"status", "refresh_failed"},
                                 {"source_doc_id", src_doc_id}, {"error", exc.what()}, {"stale", marked}});
        }
    }

    // Validate the spliced doc; on failure ROLL BACK (pop our snapshot, restore) and raise.
    std::vector<std::string> errs = validate_document(s.doc);
    if (!errs.empty()) {
        std::string prior = s.undo_stack.back();
        s.undo_stack.pop_back();
        s.doc = json::parse(prior);
        std::string joined;
        for (std::size_t i = 0; i < errs.size() && i < 5; ++i) {
            if (i) joined += "; ";
            joined += errs[i];
        }
        throw OpError("invalid_op", "refresh_derived produced an invalid document: " + joined,
                      {{"reason", errs.front()}, {"all_errors", errs}});
    }

    if (!any_change) {
        // Pure no-op: drop the snapshot we pushed and do NOT bump the revision.
        s.undo_stack.pop_back();
        CompileResult r = compile_document(s.doc, env_from_doc(s.doc));
        json resp = ops_response(s, r, json::object(), /*with_tess=*/true);
        resp["derived"] = {{"groups", summaries}};
        return resp;
    }

    if (s.active_sketch) s.active_sketch->solve_state.reset();
    s.revision += 1;
    s.dirty = true;
    CompileResult r = compile_document(s.doc, env_from_doc(s.doc));
    json resp = ops_response(s, r, json::object(), /*with_tess=*/true);
    resp["derived"] = {{"groups", summaries}};
    return resp;
}

json doc_config_list(const std::string& doc_id) {
    DocLock lk;
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
    DocLock lk;
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
    DocLock lk;
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
    DocLock lk;
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
    DocLock lk;
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
    DocLock lk;
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

// Resolve a pick in an ASSEMBLY: body_id = component ordinal, topo_id indexes that
// component's LOCAL topology (TopExp order is transform-invariant, so the world AICM ids
// match local ids). Returns {selector, component} so the host can build a mate ref
// {component, selector}. Port of session.py::_resolve_pick_assembly.
json resolve_pick_assembly(Session& s, int body_id, const std::string& kind, int topo_id) {
    if (!s.asm_result) rebuild_assembly(s);
    const AssemblyResult* ares = s.asm_result.get();
    if (!ares || body_id < 1 || body_id > static_cast<int>(ares->bodies.size()))
        throw OpError("invalid_op",
                      "body_id " + std::to_string(body_id) + " out of range for assembly",
                      {{"reason", "bad body_id"}});
    const AsmBodyOut& abody = ares->bodies[body_id - 1];
    const TopoDS_Shape& shape_local = abody.shape_local;

    if (kind == "body") {
        return json{{"selector", {{"kind", "body"}, {"owner_feature", ""}}},
                    {"component", abody.component_id}};
    }

    // describe_* runs Gauss integration over the WHOLE component; cache it per (component, kind)
    // so repeated clicks in a big assembly don't re-derive it. The cache is tied to the current
    // asm_result — a rebuild swaps the pointer and drops the stale entries.
    if (s.asm_describe_for != static_cast<const void*>(ares)) {
        s.asm_describe_cache.clear();
        s.asm_describe_for = static_cast<const void*>(ares);
    }
    const std::string ckey = std::to_string(body_id) + ":" + kind;
    auto cit = s.asm_describe_cache.find(ckey);
    if (cit == s.asm_describe_cache.end()) {
        std::vector<TopoInfo> computed;
        if (kind == "face") computed = describe_faces(shape_local);
        else if (kind == "edge") computed = describe_edges(shape_local);
        else if (kind == "vertex") computed = describe_vertices(shape_local);
        else
            throw OpError("invalid_op", "kind '" + kind + "' must be face|edge|vertex|body",
                          {{"reason", "bad kind"}});
        cit = s.asm_describe_cache.emplace(ckey, std::move(computed)).first;
    }
    const std::vector<TopoInfo>& infos = cit->second;
    if (topo_id < 1 || topo_id > static_cast<int>(infos.size()))
        throw OpError("invalid_op",
                      "topo_id " + std::to_string(topo_id) + " out of range (1.." +
                          std::to_string(infos.size()) + ") for " + kind,
                      {{"reason", "bad topo_id"}});
    TopoInfo info = infos[topo_id - 1];
    info.hint_index = topo_id - 1;
    return json{{"selector", make_selector(info)}, {"component", abody.component_id}};
}

json doc_resolve_pick(const std::string& doc_id, int body_id, const std::string& kind,
                      int topo_id) {
    DocLock lk;
    Session& s = require_session(doc_id);
    if (s.is_assembly())
        return resolve_pick_assembly(s, body_id, kind, topo_id);
    json sel = resolve_pick(ensure_compiled(s), body_id, kind, topo_id);
    return json{{"selector", sel}};
}

// SolidWorks "fillet a whole face": the host expands a face pick into the face's edge ids, then
// resolves each to an edge selector. Returns {"edges": [topo_id, ...]} in the host's edge id space.
json doc_face_edges(const std::string& doc_id, int body_id, int face_topo_id) {
    DocLock lk;
    Session& s = require_session(doc_id);
    if (s.is_assembly()) return json{{"edges", json::array()}};  // not supported for assemblies
    std::vector<int> e = face_edges(ensure_compiled(s), body_id, face_topo_id);
    return json{{"edges", e}};
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
// out_vp (optional): receives the VolumeProperties of a SOLID/COMPSOLID so a caller (eval_mass)
// can REUSE it instead of recomputing the same volume integral. *out_vp_computed reports whether
// it was filled (only the SOLID/COMPSOLID branch computes it).
bool eval_is_surface_shape(const TopoDS_Shape& shape, GProp_GProps* out_vp = nullptr,
                           bool* out_vp_computed = nullptr) {
    if (out_vp_computed) *out_vp_computed = false;
    try {
        const TopAbs_ShapeEnum st = shape.ShapeType();
        if (st == TopAbs_SOLID || st == TopAbs_COMPSOLID) {
            GProp_GProps vp;
            BRepGProp::VolumeProperties(shape, vp);
            if (out_vp) { *out_vp = vp; if (out_vp_computed) *out_vp_computed = true; }
            // PERF: a degenerate sheet is only possible when the ENCLOSED VOLUME is ~0. A normal
            // solid (the overwhelming common case, hit per-body on every rebuild) has real volume,
            // so short-circuit BEFORE the expensive SurfaceProperties Gauss area integration over
            // every face — its result is discarded once vp.Mass() is non-zero anyway.
            if (std::fabs(vp.Mass()) >= 1e-9) return false;
            GProp_GProps sp;
            BRepGProp::SurfaceProperties(shape, sp);
            return sp.Mass() > 1e-9;
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

// True only when `shape` is a watertight CLOSED solid (or compound/compsolid of such) —
// i.e. VolumeProperties yields a TRUSTWORTHY CoM / inertia. Bug J4: an open shell that
// slipped past eval_is_surface_shape (e.g. an unsewn import that OCCT still types SOLID, or
// a solid with genuine free edges) makes BRepGProp::VolumeProperties return garbage (CoM at
// z≈0 on a 20 mm-tall part, negative principal moments). We detect this via a SEAM-EXCLUDED
// free-edge census: a closed solid has every edge shared by two faces (seam/degenerate edges
// excepted — those legitimately bound one face). Any genuinely free edge means the volume is
// not enclosed, so the mass props are unreliable. Pure shells/faces are already routed to the
// surface report before this is called; this catches the SOLID-typed-but-not-closed case.
bool eval_is_closed_solid(const TopoDS_Shape& shape) {
    try {
        TopTools_IndexedDataMapOfShapeListOfShape anc;
        TopExp::MapShapesAndAncestors(shape, TopAbs_EDGE, TopAbs_FACE, anc);
        TopTools_IndexedMapOfShape edges;
        TopExp::MapShapes(shape, TopAbs_EDGE, edges);
        if (edges.Extent() == 0) return false;  // no faces/edges -> not a solid
        for (int ei = 1; ei <= edges.Extent(); ++ei) {
            const TopoDS_Shape& es = edges.FindKey(ei);
            int nf = anc.Contains(es) ? anc.FindFromKey(es).Extent() : 0;
            if (nf >= 2) continue;
            const TopoDS_Edge& e = TopoDS::Edge(es);
            if (BRep_Tool::Degenerated(e)) continue;
            bool seam = false;
            if (anc.Contains(es)) {
                const TopTools_ListOfShape& faces = anc.FindFromKey(es);
                for (TopTools_ListIteratorOfListOfShape it(faces); it.More(); it.Next())
                    if (BRep_Tool::IsClosed(e, TopoDS::Face(it.Value()))) { seam = true; break; }
            }
            if (!seam) return false;  // a real free edge -> open shell
        }
        return true;
    } catch (const Standard_Failure&) {
        return false;  // be conservative: an OCCT error means we can't vouch for closure
    }
}

}  // namespace

// Gather the shapes to hand a file writer. Assembly docs resolve their geometry through
// compile_assembly (loaded components + solved mate poses), NOT compile_document — so an
// assembly export must WORLD-PLACE each component body off the cached AssemblyResult.
// Without this branch an assembly compiles to nothing (compile_document sees no features)
// and every export threw "no bodies to export". No explode offset: an export captures the
// assembled configuration, not the exploded view. Parts fall through to compile_document.
// Mirror of tessellate_session_bodies' assembly branch, but yielding exact B-rep shapes.
static std::vector<TopoDS_Shape> gather_export_shapes(Session& s) {
    if (s.is_assembly()) {
        if (!s.asm_result) rebuild_assembly(s);
        std::vector<TopoDS_Shape> shapes;
        const AssemblyResult* ares = s.asm_result.get();
        if (ares)
            for (const AsmBodyOut& body : ares->bodies) {
                try {
                    TopoDS_Shape world = placed_world_shape(body, std::nullopt);
                    if (!world.IsNull()) shapes.push_back(world);
                } catch (...) {
                }
            }
        return shapes;
    }
    CompileResult r = compile_document(s.doc, env_from_doc(s.doc));
    return r.shapes;
}

json doc_export_step(const std::string& doc_id, const std::string& path) {
    DocLock lk;
    Session& s = require_session(doc_id);
    std::vector<TopoDS_Shape> shapes = gather_export_shapes(s);  // assembly-aware (world-placed)
    if (shapes.empty())
        throw OpError("export_failed", "document has no solid bodies to export",
                      {{"doc_id", doc_id}});
    // Format follows the extension: .stl (case-insensitive) -> binary STL, else STEP.
    // export_bodies handles single-body-as-is / multi-body-compound (as_compound) and
    // parent-dir creation, matching bridge.py::export_step.
    std::string suffix = std::filesystem::path(path).extension().string();
    std::transform(suffix.begin(), suffix.end(), suffix.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    const std::string fmt = (suffix == ".stl") ? "stl" : "step";
    std::string out = export_bodies(shapes, path, fmt);
    return json{{"path", out}};
}

// ----------------------------------------------------------------- preview render (vision)
// A tiny OCCT-free software rasterizer: project the tessellated solids isometrically, z-buffer
// + Lambert shade, and PNG-encode. Good enough for the agent to SEE its own model and catch
// gross mistakes (missing features, wrong proportions). Not a pretty renderer.
namespace {

void png_put_u32_be(std::string& out, uint32_t v) {
    out.push_back((char)((v >> 24) & 0xFF));
    out.push_back((char)((v >> 16) & 0xFF));
    out.push_back((char)((v >> 8) & 0xFF));
    out.push_back((char)(v & 0xFF));
}
void png_chunk_rgb(std::string& out, const char tag[4], const std::string& data) {
    png_put_u32_be(out, (uint32_t)data.size());
    std::string body(tag, 4);
    body += data;
    out.append(body);
    mz_ulong crc = mz_crc32(MZ_CRC32_INIT, (const unsigned char*)body.data(), body.size());
    png_put_u32_be(out, (uint32_t)crc);
}
std::string encode_png_rgb(int w, int h, const std::vector<unsigned char>& rgb) {
    std::string out;
    out.append("\x89PNG\r\n\x1a\n", 8);
    std::string ihdr;
    png_put_u32_be(ihdr, (uint32_t)w);
    png_put_u32_be(ihdr, (uint32_t)h);
    ihdr.push_back(8); ihdr.push_back(2); ihdr.push_back(0); ihdr.push_back(0); ihdr.push_back(0);
    png_chunk_rgb(out, "IHDR", ihdr);
    std::string raw;
    raw.reserve((size_t)h * (1 + (size_t)w * 3));
    for (int y = 0; y < h; ++y) {
        raw.push_back(0);  // filter: none
        raw.append((const char*)&rgb[(size_t)y * w * 3], (size_t)w * 3);
    }
    mz_ulong bound = mz_compressBound((mz_ulong)raw.size());
    std::vector<unsigned char> comp(bound);
    mz_ulong clen = bound;
    if (mz_compress2(comp.data(), &clen, (const unsigned char*)raw.data(),
                     (mz_ulong)raw.size(), 9) != MZ_OK)
        clen = 0;
    png_chunk_rgb(out, "IDAT", std::string((char*)comp.data(), clen));
    png_chunk_rgb(out, "IEND", std::string());
    return out;
}

struct RV3 { double x, y, z; };
inline RV3 rsub(const RV3& a, const RV3& b) { return {a.x - b.x, a.y - b.y, a.z - b.z}; }
inline RV3 rcross(const RV3& a, const RV3& b) {
    return {a.y * b.z - a.z * b.y, a.z * b.x - a.x * b.z, a.x * b.y - a.y * b.x};
}
inline double rdot(const RV3& a, const RV3& b) { return a.x * b.x + a.y * b.y + a.z * b.z; }
inline RV3 rnorm(const RV3& a) {
    double l = std::sqrt(rdot(a, a));
    return l < 1e-12 ? RV3{0, 0, 0} : RV3{a.x / l, a.y / l, a.z / l};
}

// ---- annotation overlay: a tiny bitmap font + 2D draw helpers so the agent vision-review
// render is LABELLED (an orientation gizmo + named-face callouts) and COLOUR-CODED by face
// orientation, instead of a bare grey blob the vision model has to guess at. -------------

// Classic 5x7 glyphs, column-major: glyph[j] is column j, bit i (0=top) lights row i.
// Covers space, + - . : 0-9 A-Z — enough for the gizmo letters, the TOP/FRONT/... callouts,
// dimensions, and feature names (upper-cased before drawing).
const unsigned char* glyph5x7(char ch) {
    static const unsigned char F[][5] = {
        {0x00,0x00,0x00,0x00,0x00},                  // ' '
        {0x08,0x08,0x3E,0x08,0x08},                  // '+'
        {0x08,0x08,0x08,0x08,0x08},                  // '-'
        {0x00,0x60,0x60,0x00,0x00},                  // '.'
        {0x00,0x36,0x36,0x00,0x00},                  // ':'
        {0x02,0x01,0x51,0x09,0x06},                  // '?'
        {0x3E,0x51,0x49,0x45,0x3E},{0x00,0x42,0x7F,0x40,0x00},  // 0 1
        {0x42,0x61,0x51,0x49,0x46},{0x21,0x41,0x45,0x4B,0x31},  // 2 3
        {0x18,0x14,0x12,0x7F,0x10},{0x27,0x45,0x45,0x45,0x39},  // 4 5
        {0x3C,0x4A,0x49,0x49,0x30},{0x01,0x71,0x09,0x05,0x03},  // 6 7
        {0x36,0x49,0x49,0x49,0x36},{0x06,0x49,0x49,0x29,0x1E},  // 8 9
        {0x7E,0x11,0x11,0x11,0x7E},{0x7F,0x49,0x49,0x49,0x36},  // A B
        {0x3E,0x41,0x41,0x41,0x22},{0x7F,0x41,0x41,0x22,0x1C},  // C D
        {0x7F,0x49,0x49,0x49,0x41},{0x7F,0x09,0x09,0x09,0x01},  // E F
        {0x3E,0x41,0x49,0x49,0x7A},{0x7F,0x08,0x08,0x08,0x7F},  // G H
        {0x00,0x41,0x7F,0x41,0x00},{0x20,0x40,0x41,0x3F,0x01},  // I J
        {0x7F,0x08,0x14,0x22,0x41},{0x7F,0x40,0x40,0x40,0x40},  // K L
        {0x7F,0x02,0x0C,0x02,0x7F},{0x7F,0x04,0x08,0x10,0x7F},  // M N
        {0x3E,0x41,0x41,0x41,0x3E},{0x7F,0x09,0x09,0x09,0x06},  // O P
        {0x3E,0x41,0x51,0x21,0x5E},{0x7F,0x09,0x19,0x29,0x46},  // Q R
        {0x46,0x49,0x49,0x49,0x31},{0x01,0x01,0x7F,0x01,0x01},  // S T
        {0x3F,0x40,0x40,0x40,0x3F},{0x1F,0x20,0x40,0x20,0x1F},  // U V
        {0x7F,0x20,0x18,0x20,0x7F},{0x63,0x14,0x08,0x14,0x63},  // W X
        {0x07,0x08,0x70,0x08,0x07},{0x61,0x51,0x49,0x45,0x43},  // Y Z
    };
    if (ch == ' ') return F[0];
    if (ch == '+') return F[1];
    if (ch == '-') return F[2];
    if (ch == '.') return F[3];
    if (ch == ':') return F[4];
    if (ch == '?') return F[5];
    if (ch >= '0' && ch <= '9') return F[6 + (ch - '0')];
    if (ch >= 'a' && ch <= 'z') ch = static_cast<char>(ch - 'a' + 'A');
    if (ch >= 'A' && ch <= 'Z') return F[16 + (ch - 'A')];
    return F[0];
}

inline void put_px(std::vector<unsigned char>& img, int W, int H, int x, int y,
                   unsigned char r, unsigned char g, unsigned char b) {
    if (x < 0 || y < 0 || x >= W || y >= H) return;
    size_t p = (size_t)y * W + x;
    img[p * 3] = r; img[p * 3 + 1] = g; img[p * 3 + 2] = b;
}

// Draw `text` at (x,y) top-left, each pixel scaled `sc`x, with a 1px dark halo so it stays
// readable over any face colour. Returns the x just past the string.
int blit_text(std::vector<unsigned char>& img, int W, int H, int x, int y,
              const std::string& text, int sc, unsigned char r, unsigned char g, unsigned char b) {
    int cx = x;
    for (char ch : text) {
        const unsigned char* gly = glyph5x7(ch);
        for (int col = 0; col < 5; ++col)
            for (int row = 0; row < 7; ++row)
                if (gly[col] & (1u << row))
                    for (int dy = 0; dy < sc; ++dy)
                        for (int dx = 0; dx < sc; ++dx) {
                            int px = cx + col * sc + dx, py = y + row * sc + dy;
                            // halo first (8-neighbour dark), then the glyph colour on top
                            for (int oy = -1; oy <= 1; ++oy)
                                for (int ox = -1; ox <= 1; ++ox)
                                    put_px(img, W, H, px + ox, py + oy, 20, 20, 20);
                        }
        // second pass: the bright glyph over the halo
        for (int col = 0; col < 5; ++col)
            for (int row = 0; row < 7; ++row)
                if (gly[col] & (1u << row))
                    for (int dy = 0; dy < sc; ++dy)
                        for (int dx = 0; dx < sc; ++dx)
                            put_px(img, W, H, cx + col * sc + dx, y + row * sc + dy, r, g, b);
        cx += 6 * sc;  // 5px glyph + 1px space
    }
    return cx;
}

void draw_line(std::vector<unsigned char>& img, int W, int H, int x0, int y0, int x1, int y1,
               unsigned char r, unsigned char g, unsigned char b, int thick = 1) {
    int dx = std::abs(x1 - x0), dy = -std::abs(y1 - y0);
    int sx = x0 < x1 ? 1 : -1, sy = y0 < y1 ? 1 : -1, err = dx + dy;
    while (true) {
        for (int oy = 0; oy < thick; ++oy)
            for (int ox = 0; ox < thick; ++ox) put_px(img, W, H, x0 + ox, y0 + oy, r, g, b);
        if (x0 == x1 && y0 == y1) break;
        int e2 = 2 * err;
        if (e2 >= dy) { err += dy; x0 += sx; }
        if (e2 <= dx) { err += dx; y0 += sy; }
    }
}

void fill_disc(std::vector<unsigned char>& img, int W, int H, int cx, int cy, int rad,
               unsigned char r, unsigned char g, unsigned char b) {
    for (int y = -rad; y <= rad; ++y)
        for (int x = -rad; x <= rad; ++x)
            if (x * x + y * y <= rad * rad) put_px(img, W, H, cx + x, cy + y, r, g, b);
}

// Map an annotation/dimension string onto the 5x7 font's ASCII subset (A-Z 0-9 + - . : space):
// the engine composes callouts with Unicode glyphs (⌀ × ° ± Ø) that the bitmap font can't
// draw, so translate them to ASCII shorthand (⌀->DIA, ×->X, °->DEG, ±->+-) and drop any
// other non-ASCII byte. Used by the drawing renderer's annotation overlay (Bug N-7).
std::string sanitize_label(const std::string& in) {
    std::string out;
    out.reserve(in.size());
    for (std::size_t i = 0; i < in.size();) {
        unsigned char c = static_cast<unsigned char>(in[i]);
        if (c < 0x80) { out.push_back(in[i]); ++i; continue; }
        // Multibyte UTF-8: recognise the symbols we care about, else skip the sequence.
        if (c == 0xC3 && i + 1 < in.size()) {
            unsigned char d = static_cast<unsigned char>(in[i + 1]);
            if (d == 0x97) out += "X";              // × multiplication sign
            else if (d == 0x98 || d == 0xB8) out += "DIA ";  // Ø/ø
            i += 2; continue;
        }
        if (c == 0xC2 && i + 1 < in.size()) {
            unsigned char d = static_cast<unsigned char>(in[i + 1]);
            if (d == 0xB0) out += " DEG";           // ° degree
            else if (d == 0xB1) out += "+-";        // ± plus-minus
            i += 2; continue;
        }
        if (c == 0xE2 && i + 2 < in.size()) {
            unsigned char d = static_cast<unsigned char>(in[i + 1]);
            unsigned char e = static_cast<unsigned char>(in[i + 2]);
            // The drafting symbols the engine emits in stacked hole callouts + dims, mapped to
            // readable ASCII shorthand so they don't silently vanish from the PNG (the bitmap
            // font has no glyphs for them). ⌀ diameter, ⌴ counterbore, ⌵ countersink, ↧ depth.
            if (d == 0x8C && e == 0x80) { out += "DIA "; i += 3; continue; }   // ⌀ U+2300
            if (d == 0x8C && e == 0xB4) { out += "CBORE "; i += 3; continue; } // ⌴ U+2334
            if (d == 0x8C && e == 0xB5) { out += "CSK "; i += 3; continue; }   // ⌵ U+2335
            if (d == 0x86 && e == 0xA7) { out += "DEPTH "; i += 3; continue; } // ↧ U+21A7
            // fall through to the generic skip for any other 3-byte sequence
        }
        // Unknown multibyte lead: skip this byte and any continuation bytes.
        ++i;
        while (i < in.size() && (static_cast<unsigned char>(in[i]) & 0xC0) == 0x80) ++i;
    }
    return out;
}

// A face callout the renderer overlays: a world anchor (face centroid) + outward normal (for
// back-face culling) + the label text + its colour.
struct RenderLabel {
    RV3 anchor;
    RV3 normal;
    std::string text;
    unsigned char col[3];
};

// Tint a lit face colour toward its principal-orientation hue when its normal is strongly
// axis-aligned (a flat principal face); curved / oblique faces are left neutral. Returns true
// and writes the tint when aligned. Hues match the gizmo: X=red, Y=green, Z=blue, with the
// negative side a distinct sibling (front=-Y yellow vs back=+Y green; left=-X orange vs right
// =+X red; top=+Z blue vs bottom=-Z teal).
bool orient_tint(double nx, double ny, double nz, unsigned char out[3]) {
    double ax = std::abs(nx), ay = std::abs(ny), az = std::abs(nz);
    double m = std::max({ax, ay, az});
    if (m < 0.92) return false;  // not flat-on a principal axis -> leave neutral
    // Tie-break matches selectors.cpp side_word (Z, then X, then Y) — not float equality on m.
    if (az >= ax && az >= ay) {
        if (nz > 0) { out[0]=90; out[1]=140; out[2]=225; }   // top +Z blue
        else        { out[0]=70; out[1]=175; out[2]=185; }   // bottom -Z teal
    } else if (ax >= ay && ax >= az) {
        if (nx > 0) { out[0]=220; out[1]=80; out[2]=80; }    // right +X red
        else        { out[0]=225; out[1]=140; out[2]=70; }   // left -X orange
    } else {
        if (ny > 0) { out[0]=95; out[1]=185; out[2]=95; }    // back +Y green
        else        { out[0]=215; out[1]=200; out[2]=70; }  // front -Y yellow
    }
    return true;
}

// ---- view spec resolution (task 03: on-request viewing angles) --------------------------------
// A camera for the rasterizer is a single direction `dir` = scene-center -> camera (the existing
// code's `d`). Named views match the SAME convention the vision-review face labels use
// (front = looking at the -Y face, right = +X, top = +Z), so "front"/"right"/"top" mean exactly
// what the model already queries geometry by. iso keeps the legacy front-right-top-ish corner.
struct ResolvedView { RV3 dir; std::string name; };

inline RV3 dir_from_az_el(double az_deg, double el_deg) {
    // az measured from the -Y (front) axis toward +X (right); el above the XY plane toward +Z.
    // az=0,el=0 -> front (0,-1,0); az=90 -> right (1,0,0); el=90 -> top (0,0,1).
    const double k = 3.14159265358979323846 / 180.0;
    double a = az_deg * k, e = el_deg * k;
    return rnorm({std::cos(e) * std::sin(a), -std::cos(e) * std::cos(a), std::sin(e)});
}

// Resolve a view spec to a camera direction + a short display name. Accepts (in priority order):
//   - a bare string name:           "front"|"back"|"top"|"bottom"|"left"|"right"|"iso"
//   - {"view":"front"}              same set, object form
//   - {"azimuth":a,"elevation":e}   parametric angle in degrees (the on-request angle)
//   - {"dir":[x,y,z]}               explicit camera direction (scene -> camera), any length
// Anything unrecognised falls back to iso. Pure + header-free so it is unit-testable.
ResolvedView resolve_view_spec(const nlohmann::json& v) {
    auto named = [](const std::string& s) -> ResolvedView {
        std::string n;
        for (char c : s) n += (char)std::toupper((unsigned char)c);
        if (n == "FRONT")  return {{0, -1, 0}, "FRONT"};
        if (n == "BACK")   return {{0, 1, 0}, "BACK"};
        if (n == "TOP")    return {{0, 0, 1}, "TOP"};
        if (n == "BOTTOM") return {{0, 0, -1}, "BOTTOM"};
        if (n == "RIGHT")  return {{1, 0, 0}, "RIGHT"};
        if (n == "LEFT")   return {{-1, 0, 0}, "LEFT"};
        return {rnorm({1.0, 1.0, 0.8}), "ISO"};  // iso / unknown
    };
    if (v.is_string()) return named(v.get<std::string>());
    if (v.is_object()) {
        if (v.contains("dir") && v.at("dir").is_array() && v.at("dir").size() >= 3) {
            const auto& a = v.at("dir");
            RV3 dir = rnorm({a[0].get<double>(), a[1].get<double>(), a[2].get<double>()});
            if (rdot(dir, dir) > 1e-9)
                return {dir, v.value("name", std::string("VIEW"))};
        }
        if (v.contains("azimuth") || v.contains("elevation")) {
            double az = v.value("azimuth", 0.0), el = v.value("elevation", 0.0);
            RV3 dir = dir_from_az_el(az, el);
            // Compact "AZ45 EL35" name so the header tells the model exactly what it asked for.
            std::string nm = "AZ" + std::to_string((long)std::lround(az)) +
                             " EL" + std::to_string((long)std::lround(el));
            return {dir, nm};
        }
        if (v.contains("view") && v.at("view").is_string()) return named(v.at("view").get<std::string>());
    }
    return {rnorm({1.0, 1.0, 0.8}), "ISO"};
}

// Render the tessellated solids from an arbitrary camera direction into an RGB buffer (no file I/O),
// labelled + colour-coded like the legacy iso render. `header`, when non-empty, is blitted top-left
// (view name + units + bbox dims) so a multi-view contact sheet is self-describing. Returns false if
// essentially nothing was drawn. This is the shared core behind render_iso_png / doc_render_view /
// the contact sheet — one renderer, so headless and on-screen stay consistent (CLAUDE.md rule 3).
bool render_view_to_rgb(const std::vector<aicm::AicmBody>& bodies,
                        const std::vector<RenderLabel>& labels, int W, int H, RV3 viewDir,
                        const std::string& header, std::vector<unsigned char>& outImg) {
    RV3 lo{1e30, 1e30, 1e30}, hi{-1e30, -1e30, -1e30};
    bool any = false;
    for (const auto& b : bodies)
        for (size_t i = 0; i + 2 < b.pos.size(); i += 3) {
            lo.x = std::min(lo.x, (double)b.pos[i]);     hi.x = std::max(hi.x, (double)b.pos[i]);
            lo.y = std::min(lo.y, (double)b.pos[i + 1]); hi.y = std::max(hi.y, (double)b.pos[i + 1]);
            lo.z = std::min(lo.z, (double)b.pos[i + 2]); hi.z = std::max(hi.z, (double)b.pos[i + 2]);
            any = true;
        }
    if (!any) return false;
    RV3 c{(lo.x + hi.x) / 2, (lo.y + hi.y) / 2, (lo.z + hi.z) / 2};
    RV3 d = rnorm(viewDir);                   // view dir (scene -> camera)
    if (rdot(d, d) < 0.5) d = rnorm({1.0, 1.0, 0.8});  // degenerate spec -> iso
    // Robust screen-up reference: world +Z, except for a top/bottom view (d ~ ±Z), where +Z is
    // parallel to the view dir and rcross would collapse — fall back to world +Y there.
    RV3 upRef = (std::abs(d.z) > 0.985) ? RV3{0, 1, 0} : RV3{0, 0, 1};
    RV3 r = rnorm(rcross(upRef, d));
    RV3 u = rcross(d, r);
    RV3 light = rnorm({0.4, 0.5, 1.0});
    auto proj = [&](double x, double y, double z, double& pu, double& pv, double& pd) {
        RV3 p = rsub({x, y, z}, c);
        pu = rdot(p, r); pv = rdot(p, u); pd = rdot(p, d);
    };
    double minu = 1e30, maxu = -1e30, minv = 1e30, maxv = -1e30;
    for (const auto& b : bodies)
        for (size_t i = 0; i + 2 < b.pos.size(); i += 3) {
            double pu, pv, pd; proj(b.pos[i], b.pos[i + 1], b.pos[i + 2], pu, pv, pd);
            minu = std::min(minu, pu); maxu = std::max(maxu, pu);
            minv = std::min(minv, pv); maxv = std::max(maxv, pv);
        }
    double scale = 0.85 * std::min(W / std::max(maxu - minu, 1e-6), H / std::max(maxv - minv, 1e-6));
    double cu = (minu + maxu) / 2, cv = (minv + maxv) / 2;
    auto toScreen = [&](double pu, double pv, double& sx, double& sy) {
        sx = (pu - cu) * scale + W / 2.0;
        sy = H / 2.0 - (pv - cv) * scale;
    };
    std::vector<unsigned char> img((size_t)W * H * 3, 245);  // light-grey background
    std::vector<double> zbuf((size_t)W * H, -1e30);
    long painted = 0;
    for (const auto& b : bodies) {
        double br = b.color[0], bg = b.color[1], bb = b.color[2];
        // GSL render fix — decide the orientation hue ONCE per B-REP FACE, not per triangle.
        // The old code fed each triangle's own geometric normal to orient_tint, so a single
        // curved face (whose triangles point every which way) came back SPLATTERED with
        // red/green/blue patches wherever a triangle happened to cross the 0.92 axis threshold
        // (the impeller-blade / vase-wall blotch). Now: only a PLANAR FaceRec (surf_type 0) is
        // eligible, and its hue is taken from the face's averaged normal — so a flat principal
        // face is cleanly tinted and every curved face stays neutral grey. Triangles with no
        // FaceRec (raw mesh imports) fall back to the per-triangle path inside the loop.
        const size_t ntri = b.idx.size() / 3;
        std::vector<int> face_of_tri(ntri, -1);
        std::vector<unsigned char> face_tint;   // 3 bytes per FaceRec
        std::vector<char> face_tinted;          // 1 per FaceRec
        face_tint.reserve(b.faces.size() * 3);
        face_tinted.reserve(b.faces.size());
        for (size_t fi = 0; fi < b.faces.size(); ++fi) {
            const aicm::FaceRec& fr = b.faces[fi];
            const size_t last = std::min<size_t>((size_t)fr.first_tri + fr.tri_count, ntri);
            RV3 acc{0, 0, 0};
            size_t used = 0;
            for (size_t tri = fr.first_tri; tri < last; ++tri) {
                face_of_tri[tri] = (int)fi;
                const size_t t = tri * 3;
                const uint32_t a = b.idx[t], c1 = b.idx[t + 1], c2 = b.idx[t + 2];
                if (3u * a + 2 >= b.pos.size() || 3u * c1 + 2 >= b.pos.size() ||
                    3u * c2 + 2 >= b.pos.size())
                    continue;
                RV3 p0{b.pos[3 * a], b.pos[3 * a + 1], b.pos[3 * a + 2]};
                RV3 p1{b.pos[3 * c1], b.pos[3 * c1 + 1], b.pos[3 * c1 + 2]};
                RV3 p2{b.pos[3 * c2], b.pos[3 * c2 + 1], b.pos[3 * c2 + 2]};
                RV3 n = rnorm(rcross(rsub(p1, p0), rsub(p2, p0)));
                acc.x += n.x; acc.y += n.y; acc.z += n.z; ++used;
            }
            unsigned char tint[3] = {0, 0, 0};
            bool tinted = false;
            // Only flat (planar) faces carry an orientation hue; curved faces stay neutral.
            if (fr.surf_type == 0 && used > 0) {
                RV3 mn = rnorm({acc.x, acc.y, acc.z});
                if (orient_tint(mn.x, mn.y, mn.z, tint)) tinted = true;
            }
            face_tint.push_back(tint[0]); face_tint.push_back(tint[1]); face_tint.push_back(tint[2]);
            face_tinted.push_back(tinted ? 1 : 0);
        }
        for (size_t t = 0; t + 2 < b.idx.size(); t += 3) {
            uint32_t i0 = b.idx[t], i1 = b.idx[t + 1], i2 = b.idx[t + 2];
            if (3u * i0 + 2 >= b.pos.size() || 3u * i1 + 2 >= b.pos.size() ||
                3u * i2 + 2 >= b.pos.size())
                continue;
            RV3 w0{b.pos[3 * i0], b.pos[3 * i0 + 1], b.pos[3 * i0 + 2]};
            RV3 w1{b.pos[3 * i1], b.pos[3 * i1 + 1], b.pos[3 * i1 + 2]};
            RV3 w2{b.pos[3 * i2], b.pos[3 * i2 + 1], b.pos[3 * i2 + 2]};
            RV3 fn = rnorm(rcross(rsub(w1, w0), rsub(w2, w0)));
            double lam = 0.35 + 0.65 * std::max(0.0, std::abs(rdot(fn, light)));
            // Orientation colour-coding: blend a flat principal face toward its direction hue
            // (curved/oblique faces stay the neutral body colour) so the vision model can read
            // which way each face points at a glance.
            double cr = br, cg = bg, cb = bb;
            unsigned char tint[3];
            bool use_tint = false;
            const int fidx = (t / 3 < face_of_tri.size()) ? face_of_tri[t / 3] : -1;
            if (fidx >= 0) {
                // Per-FACE decision (kills the curved-face blotch): use the face's precomputed hue.
                if (face_tinted[fidx]) {
                    tint[0] = face_tint[3 * fidx];
                    tint[1] = face_tint[3 * fidx + 1];
                    tint[2] = face_tint[3 * fidx + 2];
                    use_tint = true;
                }
            } else if (orient_tint(fn.x, fn.y, fn.z, tint)) {
                use_tint = true;  // raw mesh body (no FaceRec) -> legacy per-triangle behaviour
            }
            if (use_tint) {
                const double mix = 0.55;
                cr = (1 - mix) * br + mix * (tint[0] / 255.0);
                cg = (1 - mix) * bg + mix * (tint[1] / 255.0);
                cb = (1 - mix) * bb + mix * (tint[2] / 255.0);
            }
            unsigned char R = (unsigned char)std::min(255.0, cr * 255 * lam);
            unsigned char G = (unsigned char)std::min(255.0, cg * 255 * lam);
            unsigned char B = (unsigned char)std::min(255.0, cb * 255 * lam);
            double su0, sv0, su1, sv1, su2, sv2, d0, d1, d2, pu, pv;
            proj(w0.x, w0.y, w0.z, pu, pv, d0); toScreen(pu, pv, su0, sv0);
            proj(w1.x, w1.y, w1.z, pu, pv, d1); toScreen(pu, pv, su1, sv1);
            proj(w2.x, w2.y, w2.z, pu, pv, d2); toScreen(pu, pv, su2, sv2);
            int minx = (int)std::floor(std::min({su0, su1, su2}));
            int maxx = (int)std::ceil(std::max({su0, su1, su2}));
            int miny = (int)std::floor(std::min({sv0, sv1, sv2}));
            int maxy = (int)std::ceil(std::max({sv0, sv1, sv2}));
            minx = std::max(minx, 0); miny = std::max(miny, 0);
            maxx = std::min(maxx, W - 1); maxy = std::min(maxy, H - 1);
            double area = (su1 - su0) * (sv2 - sv0) - (su2 - su0) * (sv1 - sv0);
            if (std::abs(area) < 1e-9) continue;
            for (int py = miny; py <= maxy; ++py)
                for (int px = minx; px <= maxx; ++px) {
                    double fx = px + 0.5, fy = py + 0.5;
                    double a0 = ((su1 - fx) * (sv2 - fy) - (su2 - fx) * (sv1 - fy)) / area;
                    double a1 = ((su2 - fx) * (sv0 - fy) - (su0 - fx) * (sv2 - fy)) / area;
                    double a2 = 1.0 - a0 - a1;
                    if (a0 < 0 || a1 < 0 || a2 < 0) continue;
                    double depth = a0 * d0 + a1 * d1 + a2 * d2;
                    size_t pi = (size_t)py * W + px;
                    if (depth > zbuf[pi]) {
                        zbuf[pi] = depth;
                        img[pi * 3] = R; img[pi * 3 + 1] = G; img[pi * 3 + 2] = B;
                        ++painted;
                    }
                }
        }
    }
    if (painted < (long)(W * H) / 400) return false;  // ~nothing drawn — treat as failed render

    // ---- annotation overlay (drawn ON TOP of the shaded mesh) ----------------------------
    // (a) named-face CALLOUTS: a coloured dot + leader + text at each visible principal face's
    //     screen position, using the SAME words the model addresses geometry with (TOP/FRONT
    //     /RIGHT/...). Back-facing labels (normal pointing away from the camera) are culled.
    for (const RenderLabel& lb : labels) {
        if (rdot(lb.normal, d) < 0.15) continue;  // face points away from camera -> skip
        double pu, pv, pd, sx, sy;
        proj(lb.anchor.x, lb.anchor.y, lb.anchor.z, pu, pv, pd);
        toScreen(pu, pv, sx, sy);
        int ix = (int)std::lround(sx), iy = (int)std::lround(sy);
        fill_disc(img, W, H, ix, iy, 4, 20, 20, 20);
        fill_disc(img, W, H, ix, iy, 3, lb.col[0], lb.col[1], lb.col[2]);
        // place the text just up-right of the dot, nudged inside the frame
        int tx = std::min(ix + 7, W - (int)lb.text.size() * 12 - 2);
        int ty = std::max(2, iy - 8);
        blit_text(img, W, H, tx, ty, lb.text, 2, lb.col[0], lb.col[1], lb.col[2]);
    }
    // (b) ORIENTATION GIZMO bottom-left: the three world axes drawn through the SAME camera so
    //     the model can read which way is +X/+Y/+Z (and thus right/back/top). Fixed screen
    //     anchor; only the projected screen direction of each axis is used.
    {
        int ox = 40, oy = H - 36;
        const int axlen = 26;
        struct Ax { RV3 v; unsigned char c[3]; const char* n; };
        Ax axes[3] = {{{1,0,0},{220,80,80},"X"}, {{0,1,0},{95,185,95},"Y"}, {{0,0,1},{90,140,225},"Z"}};
        for (const Ax& a : axes) {
            double su = rdot(a.v, r), sv = rdot(a.v, u);   // axis dir in screen space
            int ex = ox + (int)std::lround(su * axlen);
            int ey = oy - (int)std::lround(sv * axlen);
            draw_line(img, W, H, ox, oy, ex, ey, a.c[0], a.c[1], a.c[2], 2);
            blit_text(img, W, H, ex - 3, ey - 6, a.n, 2, a.c[0], a.c[1], a.c[2]);
        }
        fill_disc(img, W, H, ox, oy, 3, 30, 30, 30);
    }

    // (c) HEADER top-left (task 03): view name + units + bbox dims, so each preview — especially a
    //     tile in a multi-view contact sheet — says what it is and at what scale. Drawn last so it
    //     sits over everything; blit_text carries its own dark halo, readable over geometry or bg.
    if (!header.empty())
        blit_text(img, W, H, 6, 5, sanitize_label(header), 2, 235, 240, 248);

    outImg.swap(img);
    return true;
}

// Back-compat single-view iso render to a PNG file (the legacy entry the agent vision-review uses).
// Thin wrapper over render_view_to_rgb so there is exactly ONE renderer.
bool render_iso_png(const std::vector<aicm::AicmBody>& bodies, const std::string& out_path,
                    const std::vector<RenderLabel>& labels = {},
                    int W = 512, int H = 400) {
    std::vector<unsigned char> img;
    if (!render_view_to_rgb(bodies, labels, W, H, rnorm({1.0, 1.0, 0.8}), std::string(), img))
        return false;
    std::string png = encode_png_rgb(W, H, img);
    std::ofstream f(out_path, std::ios::binary | std::ios::trunc);
    if (!f) return false;
    f.write(png.data(), (std::streamsize)png.size());
    return (bool)f;
}

// Format "<W> X <D> X <H> MM" from a bbox for the preview header. Integer mm reads cleanly and the
// 5x7 font has no fraction glyphs to spare; sub-mm parts still show non-zero via a 0.x fallback.
inline std::string bbox_dims_label(const std::vector<aicm::AicmBody>& bodies) {
    RV3 lo{1e30, 1e30, 1e30}, hi{-1e30, -1e30, -1e30};
    bool any = false;
    for (const auto& b : bodies)
        for (size_t i = 0; i + 2 < b.pos.size(); i += 3) {
            lo.x = std::min(lo.x, (double)b.pos[i]);     hi.x = std::max(hi.x, (double)b.pos[i]);
            lo.y = std::min(lo.y, (double)b.pos[i + 1]); hi.y = std::max(hi.y, (double)b.pos[i + 1]);
            lo.z = std::min(lo.z, (double)b.pos[i + 2]); hi.z = std::max(hi.z, (double)b.pos[i + 2]);
            any = true;
        }
    if (!any) return std::string();
    auto fmt = [](double v) -> std::string {
        if (v >= 9.95) return std::to_string((long)std::lround(v));
        long tenths = (long)std::lround(v * 10.0);  // sub-cm: keep one decimal so it isn't "0"
        return std::to_string(tenths / 10) + "." + std::to_string(tenths % 10);
    };
    return fmt(hi.x - lo.x) + " X " + fmt(hi.y - lo.y) + " X " + fmt(hi.z - lo.z) + " MM";
}

}  // namespace

// Tessellate the surviving solids of `s` into AICM bodies (no lock — callers hold g_mtx).
static std::vector<aicm::AicmBody> tessellate_session_bodies(Session& s) {
    std::vector<aicm::AicmBody> bodies;
    // Assembly docs resolve their geometry through compile_assembly (loaded components +
    // solved mate poses), NOT compile_document — so tessellate the world-placed component
    // bodies off the cached AssemblyResult. Without this branch every assembly tessellates
    // to nothing (compile_document sees no features), which broke CFD/render on assemblies.
    // No explode offset here: consumers (CFD, iso render) want the assembled configuration.
    if (s.is_assembly()) {
        if (!s.asm_result) rebuild_assembly(s);
        const AssemblyResult* ares = s.asm_result.get();
        if (ares)
            for (const AsmBodyOut& body : ares->bodies) {
                try {
                    TopoDS_Shape world = placed_world_shape(body, std::nullopt);
                    float color[3] = {body.color[0], body.color[1], body.color[2]};
                    bodies.push_back(::geom::tessellate_body(
                        world, static_cast<uint32_t>(body.body_id), body.name, color));
                } catch (...) {
                }
            }
        return bodies;
    }
    CompileResult r = compile_document(s.doc, env_from_doc(s.doc));
    uint32_t id = 1;
    const float color[3] = {0.62f, 0.70f, 0.82f};
    for (const auto& shp : r.shapes) {
        try {
            bodies.push_back(::geom::tessellate_body(shp, id, "b" + std::to_string(id), color));
            ++id;
        } catch (...) {
        }
    }
    return bodies;
}

std::vector<aicm::AicmBody> doc_tessellate_bodies(const std::string& doc_id) {
    try {
        DocLock lk;
        Session& s = require_session(doc_id);
        return tessellate_session_bodies(s);
    } catch (...) {
        return {};
    }
}

// Named-face CALLOUTS (part docs only): label each principal face with the SAME word the model
// queries geometry by, so the vision reviewer can SEE and reference faces concretely (TOP/FRONT/
// RIGHT/...) instead of guessing at a grey blob. Fail-soft (returns whatever it resolved). Shared
// by doc_render_png and the new doc_render_view / contact-sheet entries so every preview is labelled
// identically.
static std::vector<RenderLabel> principal_face_labels(Session& s) {
    std::vector<RenderLabel> labels;
    if (s.is_assembly()) return labels;
    try {
        CompileResult r = compile_document(s.doc, env_from_doc(s.doc));
        if (r.shapes.empty()) return labels;
        const TopoDS_Shape& shp = r.shapes.back();
        struct Dir { const char* side; const char* label; RV3 nrm; unsigned char c[3]; };
        const Dir dirs[] = {
            {"top",    "TOP",    {0, 0, 1},  {90, 140, 225}},
            {"bottom", "BOTTOM", {0, 0, -1}, {70, 175, 185}},
            {"front",  "FRONT",  {0, -1, 0}, {215, 200, 70}},
            {"back",   "BACK",   {0, 1, 0},  {95, 185, 95}},
            {"right",  "RIGHT",  {1, 0, 0},  {220, 80, 80}},
            {"left",   "LEFT",   {-1, 0, 0}, {225, 140, 70}},
        };
        for (const Dir& dd : dirs) {
            aicad::doc::QueryPicks qp = aicad::doc::resolve_geom_query(
                shp, json{{"of", "face"}, {"side", dd.side}});
            if (!qp.error.empty() || !qp.selectors.is_array() || qp.selectors.empty()) continue;
            const json& sel = qp.selectors.front();
            if (!sel.contains("centroid") || !sel.at("centroid").is_array() ||
                sel.at("centroid").size() < 3)
                continue;
            const json& ct = sel.at("centroid");
            RenderLabel L;
            L.anchor = {ct[0].get<double>(), ct[1].get<double>(), ct[2].get<double>()};
            L.normal = dd.nrm;
            L.text = dd.label;
            L.col[0] = dd.c[0]; L.col[1] = dd.c[1]; L.col[2] = dd.c[2];
            labels.push_back(L);
        }
    } catch (...) {
    }
    return labels;
}

json doc_render_png(const std::string& doc_id, const std::string& out_path) {
    try {
        DocLock lk;
        Session& s = require_session(doc_id);
        std::vector<aicm::AicmBody> bodies = tessellate_session_bodies(s);
        if (bodies.empty()) return json{{"ok", false}, {"reason", "no bodies"}};
        std::vector<RenderLabel> labels = principal_face_labels(s);
        bool ok = render_iso_png(bodies, out_path, labels);
        return json{{"ok", ok}, {"path", ok ? out_path : std::string()}};
    } catch (const OpError& e) {
        // Bug K8 (P2): a CLOSED/missing doc made require_session throw unknown_doc, which the
        // catch-all below flattened to an opaque "render exception" — so render_part read very
        // differently from add_features/measure/undo, which all return the structured
        // {ok:false, error, code:"unknown_doc"} envelope on a stale doc id. Surface the same
        // shape here so a closed-doc render is diagnosable (and the orchestrator can route it)
        // instead of looking like an internal renderer crash.
        return json{{"ok", false}, {"error", e.what()}, {"code", e.code}};
    } catch (...) {
        return json{{"ok", false}, {"reason", "render exception"}};
    }
}

// doc_render_view (task 03): render ONE on-request view to a PNG. `view` is any spec accepted by
// resolve_view_spec — a named view ("front"/"top"/"iso"/...), an {"azimuth","elevation"} angle, or
// an explicit {"dir":[x,y,z]}. The image is labelled with the resolved view name + bbox dims so the
// model can reason about orientation/scale. Back-compatible: callers that want the legacy iso shot
// keep using doc_render_png. Returns {ok, path, view, dims}.
json doc_render_view(const std::string& doc_id, const std::string& out_path,
                     const nlohmann::json& view) {
    try {
        DocLock lk;
        Session& s = require_session(doc_id);
        std::vector<aicm::AicmBody> bodies = tessellate_session_bodies(s);
        if (bodies.empty()) return json{{"ok", false}, {"reason", "no bodies"}};
        ResolvedView rv = resolve_view_spec(view);
        std::string dims = bbox_dims_label(bodies);
        std::string header = rv.name + (dims.empty() ? "" : "  " + dims);
        std::vector<RenderLabel> labels = principal_face_labels(s);
        std::vector<unsigned char> img;
        bool ok = render_view_to_rgb(bodies, labels, 512, 400, rv.dir, header, img);
        if (ok) {
            std::string png = encode_png_rgb(512, 400, img);
            std::ofstream f(out_path, std::ios::binary | std::ios::trunc);
            ok = (bool)f && (f.write(png.data(), (std::streamsize)png.size()), (bool)f);
        }
        // Echo the resolved camera direction (scene -> camera) so the model knows the EXACT angle it
        // got — handy for a named/az-el request and the contract the test pins the convention on.
        return json{{"ok", ok}, {"path", ok ? out_path : std::string()},
                    {"view", rv.name}, {"dims", dims},
                    {"dir", json::array({rv.dir.x, rv.dir.y, rv.dir.z})}};
    } catch (const OpError& e) {
        return json{{"ok", false}, {"error", e.what()}, {"code", e.code}};
    } catch (...) {
        return json{{"ok", false}, {"reason", "render exception"}};
    }
}

// doc_render_contact_sheet (task 03): render SEVERAL views and tile them into one labelled PNG — the
// ideal multi-view self-check artifact. `views` is an array of specs (resolve_view_spec each);
// defaults to the six standard ortho views + iso when empty/omitted. Each tile carries its own
// header (view name + dims). Returns {ok, path, views:[names], cols, rows, tile:[w,h]}.
json doc_render_contact_sheet(const std::string& doc_id, const std::string& out_path,
                              const nlohmann::json& views) {
    try {
        DocLock lk;
        Session& s = require_session(doc_id);
        std::vector<aicm::AicmBody> bodies = tessellate_session_bodies(s);
        if (bodies.empty()) return json{{"ok", false}, {"reason", "no bodies"}};

        std::vector<nlohmann::json> specs;
        if (views.is_array() && !views.empty())
            for (const auto& v : views) specs.push_back(v);
        else
            for (const char* n : {"front", "right", "top", "iso"})
                specs.push_back(std::string(n));

        std::vector<RenderLabel> labels = principal_face_labels(s);
        std::string dims = bbox_dims_label(bodies);
        const int TW = 384, TH = 300;  // per-tile size (contact-sheet tiles are a touch smaller)
        std::vector<std::vector<unsigned char>> tiles;
        std::vector<std::string> names;
        for (const auto& sp : specs) {
            ResolvedView rv = resolve_view_spec(sp);
            std::string header = rv.name + (dims.empty() ? "" : "  " + dims);
            std::vector<unsigned char> img;
            if (!render_view_to_rgb(bodies, labels, TW, TH, rv.dir, header, img)) {
                img.assign((size_t)TW * TH * 3, 245);  // blank tile rather than dropping the slot
                blit_text(img, TW, TH, 6, 5, rv.name + " (EMPTY)", 2, 60, 60, 60);
            }
            tiles.push_back(std::move(img));
            names.push_back(rv.name);
        }
        if (tiles.empty()) return json{{"ok", false}, {"reason", "no views"}};

        int n = (int)tiles.size();
        int cols = (int)std::ceil(std::sqrt((double)n));
        int rows = (cols > 0) ? (n + cols - 1) / cols : 1;
        const int GAP = 4;
        int SW = cols * TW + (cols - 1) * GAP;
        int SH = rows * TH + (rows - 1) * GAP;
        std::vector<unsigned char> sheet((size_t)SW * SH * 3, 30);  // dark gutters between tiles
        for (int i = 0; i < n; ++i) {
            int cx = (i % cols) * (TW + GAP);
            int cy = (i / cols) * (TH + GAP);
            const std::vector<unsigned char>& t = tiles[i];
            for (int y = 0; y < TH; ++y) {
                size_t dst = ((size_t)(cy + y) * SW + cx) * 3;
                size_t src = (size_t)y * TW * 3;
                std::memcpy(&sheet[dst], &t[src], (size_t)TW * 3);
            }
        }
        std::string png = encode_png_rgb(SW, SH, sheet);
        std::ofstream f(out_path, std::ios::binary | std::ios::trunc);
        bool ok = (bool)f && (f.write(png.data(), (std::streamsize)png.size()), (bool)f);
        return json{{"ok", ok}, {"path", ok ? out_path : std::string()},
                    {"views", names}, {"cols", cols}, {"rows", rows},
                    {"tile", json::array({TW, TH})}};
    } catch (const OpError& e) {
        return json{{"ok", false}, {"error", e.what()}, {"code", e.code}};
    } catch (...) {
        return json{{"ok", false}, {"reason", "render exception"}};
    }
}

// Rasterize a 2D DRAWING document to a PNG so the AI can SEE and self-review the drawing
// it just made — the same feedback loop it has for 3D parts (Bug D-1; previously
// render_part threw an opaque "render exception" because a drawing has no 3D bodies and
// lives in a separate session registry). Visible edges draw black, hidden edges light
// grey, on white paper, auto-fit to the canvas. Returns {ok, path, visible, hidden}.
json drawing_render_png(const std::string& doc_id, const std::string& out_path) {
    try {
        // drawing_get works on the drawing-session registry (its own locking); it throws
        // unknown_doc if doc_id isn't an open drawing.
        json full = aicad::doc::drawing_get(doc_id);

        std::vector<std::vector<std::array<double, 2>>> visible, hidden;
        auto pool = [](const json& jpls,
                       std::vector<std::vector<std::array<double, 2>>>& out) {
            if (!jpls.is_array()) return;
            for (const auto& jpl : jpls) {
                if (!jpl.is_array() || jpl.size() < 2) continue;
                std::vector<std::array<double, 2>> pl;
                for (const auto& jp : jpl)
                    if (jp.is_array() && jp.size() >= 2)
                        pl.push_back({jp[0].get<double>(), jp[1].get<double>()});
                if (pl.size() >= 2) out.push_back(std::move(pl));
            }
        };
        // Annotation overlay: render the SHARED draw primitives (annot_draw) emitted per
        // annotation by drawing_get — so GD&T frames, surface-finish/weld symbols, section
        // hatch, dims + callouts all rasterize for the AI vision review, identical to the
        // on-screen sheet and the DXF/PDF export.
        std::vector<std::vector<std::array<double, 2>>> annSolid, annDash;  // sheet-mm strokes
        struct Txt { std::array<double, 2> pos{}; std::string text; double h = 3.5; };
        std::vector<Txt> texts;
        // Bug D-8 — title block as label/value cells (not one crammed "key: value" line),
        // so all standard fields fit a two-column grid without truncating or overlapping.
        std::vector<std::pair<std::string, std::string>> title_cells;  // (LABEL, value)
        auto poolDraw = [&](const json& d) {
            if (!d.is_object()) return;
            pool(d.value("segments", json::array()), annSolid);
            pool(d.value("dashed", json::array()), annDash);
            if (d.contains("tris") && d["tris"].is_array())
                for (const auto& t : d["tris"]) {
                    if (!t.is_array() || t.size() != 3) continue;
                    std::vector<std::array<double, 2>> tri;
                    for (const auto& p : t) tri.push_back({p[0].get<double>(), p[1].get<double>()});
                    tri.push_back(tri.front());
                    annSolid.push_back(std::move(tri));
                }
            if (d.contains("arcs") && d["arcs"].is_array())
                for (const auto& a : d["arcs"]) {
                    if (!a.is_object() || !a.contains("center")) continue;
                    double cx = a["center"][0].get<double>(), cy = a["center"][1].get<double>();
                    double r = a.value("radius", 0.0);
                    double a0 = a.value("a0", 0.0), a1 = a.value("a1", 6.28318530718);
                    if (r <= 0) continue;
                    int n = std::max(10, (int)(std::fabs(a1 - a0) / 0.3));
                    std::vector<std::array<double, 2>> pl;
                    for (int i = 0; i <= n; ++i) {
                        double tt = a0 + (a1 - a0) * (double)i / (double)n;
                        pl.push_back({cx + r * std::cos(tt), cy + r * std::sin(tt)});
                    }
                    annSolid.push_back(std::move(pl));
                }
            if (d.contains("texts") && d["texts"].is_array())
                for (const auto& t : d["texts"]) {
                    if (!t.is_object() || !t.contains("pos")) continue;
                    Txt T;
                    T.pos = {t["pos"][0].get<double>(), t["pos"][1].get<double>()};
                    T.text = sanitize_label(t.value("text", std::string()));
                    T.h = t.value("h", 3.5);
                    if (!T.text.empty()) texts.push_back(std::move(T));
                }
        };
        if (full.contains("sheets") && full["sheets"].is_array())
            for (const auto& sheet : full["sheets"]) {
                if (sheet.contains("views") && sheet["views"].is_array())
                    for (const auto& view : sheet["views"]) {
                        pool(view.value("visible", json::array()), visible);
                        pool(view.value("hidden", json::array()), hidden);
                        pool(view.value("hatch", json::array()), annSolid);  // section hatch
                        for (const auto& an : view.value("annotations", json::array()))
                            if (an.is_object() && an.contains("draw")) poolDraw(an["draw"]);
                        // R9 — cutting-plane line + "A-A" label on the PARENT view of a section.
                        // attach_parent_markers puts {label,line} onto the parent view's
                        // section_lines; draw each as a dashed cutting-plane line with the
                        // section label at both endpoints (the SolidWorks "A ──┼── A" marker),
                        // so the section view reads as derived from a real cut on the parent.
                        for (const auto& sl : view.value("section_lines", json::array())) {
                            if (!sl.is_object() || !sl.contains("line") || !sl["line"].is_array())
                                continue;
                            std::vector<std::array<double, 2>> ln;
                            for (const auto& jp : sl["line"])
                                if (jp.is_array() && jp.size() >= 2)
                                    ln.push_back({jp[0].get<double>(), jp[1].get<double>()});
                            if (ln.size() < 2) continue;
                            annDash.push_back(ln);
                            std::string lab = sl.value("label", std::string());
                            if (lab.empty()) lab = "A";
                            Txt t0; t0.pos = ln.front();   t0.text = sanitize_label(lab); t0.h = 4.5;
                            Txt t1; t1.pos = ln.back();    t1.text = sanitize_label(lab); t1.h = 4.5;
                            texts.push_back(std::move(t0));
                            texts.push_back(std::move(t1));
                        }
                    }
                // Title block: gather every non-empty standard field as a (LABEL, value) cell.
                // The full ASME set is 10 fields (set_titleblock): title, drawn_by, date,
                // material, finish, scale, rev, sheet, part_number, company. `rev`/`revision`
                // are accepted interchangeably. Values are rendered in their own column so a
                // long value never eats the label and nothing truncates (Bug D-8).
                const json& tb = sheet.value("title_block", json::object());
                if (tb.is_object()) {
                    // {json key(s), display label}; first present key wins.
                    struct Field { const char* keys[2]; const char* label; };
                    static const Field kFields[] = {
                        {{"title", nullptr}, "TITLE"},
                        {{"part_number", "part_no"}, "PART NO"},
                        {{"drawn_by", "drawn"}, "DRAWN"},
                        {{"date", nullptr}, "DATE"},
                        {{"material", nullptr}, "MATERIAL"},
                        {{"finish", nullptr}, "FINISH"},
                        {{"scale", nullptr}, "SCALE"},
                        {{"rev", "revision"}, "REV"},
                        {{"sheet", nullptr}, "SHEET"},
                        {{"company", nullptr}, "COMPANY"},
                    };
                    for (const Field& fld : kFields) {
                        std::string val;
                        for (const char* k : fld.keys) {
                            if (!k) break;
                            auto it = tb.find(k);
                            if (it != tb.end() && it->is_string() && !it->get<std::string>().empty()) {
                                val = it->get<std::string>();
                                break;
                            }
                        }
                        if (!val.empty())
                            title_cells.emplace_back(std::string(fld.label),
                                                     sanitize_label(val));
                    }
                }
            }
        if (visible.empty() && hidden.empty())
            return json{{"ok", false},
                        {"reason", "drawing has no view geometry to render — add views, then Rebuild"}};

        double minx = 1e30, miny = 1e30, maxx = -1e30, maxy = -1e30;
        auto acc = [&](const std::vector<std::vector<std::array<double, 2>>>& src) {
            for (const auto& pl : src)
                for (const auto& p : pl) {
                    minx = std::min(minx, p[0]); miny = std::min(miny, p[1]);
                    maxx = std::max(maxx, p[0]); maxy = std::max(maxy, p[1]);
                }
        };
        // Bug D-1 — annotation rescale. The fit/content box MUST be computed from the
        // projected GEOMETRY (the view polylines) ONLY. If we grew it to include annotation
        // strokes or text positions, adding a single GD&T frame / datum / dimension placed
        // just outside the part would re-scale and re-shift EVERY view into a corner of the
        // sheet (the layout would chase the annotation). Annotations are drawn afterwards
        // with the SAME sheet->pixel transform (to_pix) at their given sheet coords, so they
        // land in the right place on top of a now-FIXED layout. Adding an annotation no
        // longer changes view placement or scale. (A label far outside the geometry may run
        // toward the paper margin, which is the correct, non-destructive behavior — the views
        // stay put.) Do NOT acc(annSolid)/acc(annDash)/texts here.
        acc(visible); acc(hidden);
        if (!(maxx > minx) || !(maxy > miny))
            return json{{"ok", false}, {"reason", "degenerate drawing bounds"}};

        const int W = 1100, H = 850, margin = 40;
        std::vector<unsigned char> img(static_cast<std::size_t>(W) * H * 3, 255);  // white paper
        const double sc = std::min(double(W - 2 * margin) / (maxx - minx),
                                   double(H - 2 * margin) / (maxy - miny));
        const double offx = margin + (W - 2 * margin - (maxx - minx) * sc) / 2.0;
        const double offy = margin + (H - 2 * margin - (maxy - miny) * sc) / 2.0;
        auto to_pix = [&](const std::array<double, 2>& p, int& ix, int& iy) {
            ix = static_cast<int>(offx + (p[0] - minx) * sc);
            iy = static_cast<int>(H - (offy + (p[1] - miny) * sc));  // flip Y: sheet-up = image-up
        };
        for (const auto& pl : hidden)
            for (std::size_t i = 1; i < pl.size(); ++i) {
                int x0, y0, x1, y1; to_pix(pl[i - 1], x0, y0); to_pix(pl[i], x1, y1);
                draw_line(img, W, H, x0, y0, x1, y1, 150, 150, 150);
            }
        for (const auto& pl : visible)
            for (std::size_t i = 1; i < pl.size(); ++i) {
                int x0, y0, x1, y1; to_pix(pl[i - 1], x0, y0); to_pix(pl[i], x1, y1);
                draw_line(img, W, H, x0, y0, x1, y1, 20, 20, 20);
            }
        // Annotations on top: the compiled draw primitives (dims, callouts, GD&T, hatch) in
        // dim-blue, with text via the bitmap font sized from the paper-space text height.
        const unsigned char DR = 30, DG = 70, DB = 170;  // dimension blue
        for (const auto& pl : annDash)
            for (std::size_t i = 1; i < pl.size(); ++i) {
                int x0, y0, x1, y1; to_pix(pl[i - 1], x0, y0); to_pix(pl[i], x1, y1);
                draw_line(img, W, H, x0, y0, x1, y1, DR, DG, DB);
            }
        for (const auto& pl : annSolid)
            for (std::size_t i = 1; i < pl.size(); ++i) {
                int x0, y0, x1, y1; to_pix(pl[i - 1], x0, y0); to_pix(pl[i], x1, y1);
                draw_line(img, W, H, x0, y0, x1, y1, DR, DG, DB);
            }
        int n_anns_drawn = (int)texts.size();
        for (const auto& T : texts) {
            int tx, ty; to_pix(T.pos, tx, ty);
            int fs = std::max(1, (int)std::lround(T.h * sc / 7.0));  // 5x7 bitmap font scale
            blit_text(img, W, H, tx + 1, ty - fs * 7, T.text, fs, DR, DG, DB);
        }
        // Title block: a fixed two-column grid panel in the bottom-right corner (screen
        // space, never clipped). One row per field; a LABEL column and a VALUE column with a
        // divider between them, so a long value can never overrun a label and nothing
        // truncates against another field (Bug D-8). Text is scale-1 (6px/char advance, 7px
        // tall) so the full 10-field block fits; an over-long value is clipped to its cell
        // with a trailing '.' so it degrades gracefully instead of bleeding past the frame.
        if (!title_cells.empty()) {
            const int ts = 1;            // bitmap-font scale
            const int chW = 6 * ts;      // glyph advance
            const int rowH = 7 * ts + 6; // text height + vertical padding
            const int padX = 4;
            // Label column: sized to the widest label (bounded so it can't dominate).
            int labChars = 0;
            for (const auto& c : title_cells) labChars = std::max(labChars, (int)c.first.size());
            int labW = std::min(labChars * chW + 2 * padX, 90);
            // Value column: sized to the widest value, but the whole panel is capped so it
            // always fits the canvas; the value cell is clipped to whatever width remains.
            int valChars = 0;
            for (const auto& c : title_cells) valChars = std::max(valChars, (int)c.second.size());
            int valW = valChars * chW + 2 * padX;
            const int boxWmax = 360;     // panel width cap
            int boxW = std::min(labW + valW, boxWmax);
            boxW = std::min(boxW, W - 4);
            boxW = std::max(boxW, 150);
            valW = boxW - labW;          // value column gets the remainder
            int boxH = (int)title_cells.size() * rowH;
            // Keep the panel on-paper even if there are many rows.
            boxH = std::min(boxH, H - 4);
            int bx0 = W - boxW - 2, by0 = H - boxH - 2;
            int labX = bx0 + labW;       // label|value divider x
            // Outer frame.
            draw_line(img, W, H, bx0, by0, bx0 + boxW, by0, 20, 20, 20);
            draw_line(img, W, H, bx0, by0 + boxH, bx0 + boxW, by0 + boxH, 20, 20, 20);
            draw_line(img, W, H, bx0, by0, bx0, by0 + boxH, 20, 20, 20);
            draw_line(img, W, H, bx0 + boxW, by0, bx0 + boxW, by0 + boxH, 20, 20, 20);
            // Label|value divider, full height.
            draw_line(img, W, H, labX, by0, labX, by0 + boxH, 20, 20, 20);
            // Clip a string to a pixel width (cells are fixed): keep what fits, mark a cut.
            auto clip_to = [&](const std::string& s, int wpx) -> std::string {
                int maxChars = std::max(0, (wpx - 2 * padX) / chW);
                if ((int)s.size() <= maxChars) return s;
                if (maxChars <= 1) return s.substr(0, std::max(0, maxChars));
                return s.substr(0, maxChars - 1) + ".";
            };
            int ry = by0;
            for (const auto& c : title_cells) {
                if (ry + rowH > by0 + boxH) break;  // ran out of panel height
                if (ry > by0)  // row divider (skip above the first row)
                    draw_line(img, W, H, bx0, ry, bx0 + boxW, ry, 90, 90, 90);
                int ty = ry + 3;
                blit_text(img, W, H, bx0 + padX, ty, clip_to(c.first, labW), ts, 20, 20, 20);
                blit_text(img, W, H, labX + padX, ty, clip_to(c.second, valW), ts, 20, 20, 20);
                ry += rowH;
            }
        }
        std::string png = encode_png_rgb(W, H, img);
        if (png.empty()) return json{{"ok", false}, {"reason", "png encode failed"}};
        std::ofstream f(out_path, std::ios::binary | std::ios::trunc);
        if (!f) return json{{"ok", false}, {"reason", "cannot open output path"}};
        f.write(png.data(), static_cast<std::streamsize>(png.size()));
        if (!f) return json{{"ok", false}, {"reason", "png write failed"}};
        return json{{"ok", true}, {"path", out_path},
                    {"visible", static_cast<int>(visible.size())},
                    {"hidden", static_cast<int>(hidden.size())},
                    {"annotations", n_anns_drawn},
                    {"title_block", !title_cells.empty()}};
    } catch (const OpError& e) {
        // Bug D-3 — review after a detail/section view. drawing_get() re-projects EVERY view
        // on each render; if a derived view (e.g. a detail whose parent is a section with a
        // degenerate cut plane) throws an OpError during re-projection, the whole render
        // aborts. The old catch flattened EVERY OpError to "not a drawing document", which is
        // wrong and undiagnosable (unknown_doc is the only case that actually means that).
        // Surface the real code+message so review_drawing reports WHY (and the orchestrator
        // can route a genuine derived-view-creation bug to drawing_session.cpp). A true
        // missing/closed drawing still reads clearly via the unknown_doc code.
        if (e.code == "unknown_doc")
            return json{{"ok", false}, {"reason", "not a drawing document"},
                        {"code", e.code}};
        return json{{"ok", false},
                    {"reason", std::string("drawing render failed: ") + e.what()},
                    {"code", e.code}};
    } catch (...) {
        return json{{"ok", false}, {"reason", "drawing render exception"}};
    }
}

// A document can compile to ZERO surviving bodies for two very different reasons: the
// feature tree is genuinely empty, OR a feature FAILED to rebuild (e.g. an `import` whose
// source file is gone and that carries no embedded geometry, a degenerate sweep/loft, a
// boolean that consumed its only body). The latter used to surface as the generic
// "document has no bodies …" — indistinguishable from an empty doc, and easy to misread as
// a phantom "eval lost the body right after open" bug. Build a message that NAMES the
// culprit feature(s) + their rebuild error so the failure is self-explaining. Returns ""
// when nothing failed (genuinely empty document).
static std::string rebuild_failure_detail(const CompileResult& r) {
    std::string out;
    int n = 0;
    for (const FeatureStatus& st : r.statuses) {
        if (st.ok) continue;
        out += n++ ? "; " : "";
        out += "feature '" + st.id + "'";
        if (!st.type.empty()) out += " (" + st.type + ")";
        out += " failed to rebuild";
        if (!st.error.empty()) out += ": " + st.error;
    }
    return out;
}

// Throw the right "no bodies to <action>" OpError for an empty compile: a rebuild-failure-
// aware message (naming the broken feature) when a feature errored, else the plain message.
// Centralises the pattern shared by eval_mass / doc_query / doc_bbox / face_points / …
[[noreturn]] static void throw_no_bodies(const std::string& doc_id, const CompileResult& r,
                                         const char* action) {
    const std::string detail = rebuild_failure_detail(r);
    const std::string base = std::string("document has no bodies to ") + action;
    if (!detail.empty())
        throw OpError("invalid_op", base + " — " + detail,
                      {{"doc_id", doc_id}, {"reason", "feature rebuild failed"}});
    throw OpError("invalid_op", base, {{"doc_id", doc_id}});
}

json doc_query(const std::string& doc_id, const json& query) {
    DocLock lk;
    Session& s = require_session(doc_id);
    CompileResult r = compile_document(s.doc, env_from_doc(s.doc));
    if (r.shapes.empty())
        throw_no_bodies(doc_id, r, "query");
    // Query the LAST body (bridge.py policy: bodies[-1]).
    aicad::doc::QueryPicks qp = aicad::doc::resolve_geom_query(r.shapes.back(), query);
    if (!qp.error.empty())
        throw OpError("invalid_op", qp.error);
    int count = qp.selectors.is_array() ? static_cast<int>(qp.selectors.size()) : 0;
    return json{{"count", count}, {"matches", qp.selectors}};
}

// doc_adjacency (GSL W3) — the face-adjacency graph the flat read-back never exposed. For each
// manifold edge (shared by exactly two faces) we record which two faces it joins and whether the
// join is convex (an outside corner) or concave (an inside corner/pocket), plus each face's
// neighbour set. `ord` is the describe_faces enumeration index == the query-match position, so
// the LLM can finally say "the edge between the top and front face" and verify it.
json doc_adjacency(const std::string& doc_id) {
    DocLock lk;
    Session& s = require_session(doc_id);
    CompileResult r = compile_document(s.doc, env_from_doc(s.doc));
    if (r.shapes.empty())
        throw_no_bodies(doc_id, r, "adjacency");
    const TopoDS_Shape& shape = r.shapes.back();
    std::vector<aicad::doc::TopoInfo> faces = aicad::doc::describe_faces(shape);

    // Face ordinals MUST follow describe_faces (TopExp_Explorer), not MapShapes — query
    // matches and doc_resolve_pick face ids use that enumeration.
    TopTools_IndexedMapOfShape face_ord;
    for (TopExp_Explorer fexp(shape, TopAbs_FACE); fexp.More(); fexp.Next())
        face_ord.Add(fexp.Current());
    TopTools_IndexedDataMapOfShapeListOfShape e2f;
    TopExp::MapShapesAndAncestors(shape, TopAbs_EDGE, TopAbs_FACE, e2f);

    std::vector<std::set<int>> nbr(faces.size());
    json edges = json::array();
    auto round6 = [](double x) {
        if (std::abs(x) < 1e-9) return 0.0;
        return std::round(x * 1e6) / 1e6;
    };
    for (int i = 1; i <= e2f.Extent(); ++i) {
        const TopoDS_Edge& e = TopoDS::Edge(e2f.FindKey(i));
        const TopTools_ListOfShape& fl = e2f.FindFromIndex(i);
        if (fl.Extent() != 2) continue;  // only manifold shared edges have a defined dihedral
        TopTools_ListIteratorOfListOfShape lit(fl);
        TopoDS_Face fa = TopoDS::Face(lit.Value());
        lit.Next();
        TopoDS_Face fb = TopoDS::Face(lit.Value());
        int ia = face_ord.FindIndex(fa) - 1, ib = face_ord.FindIndex(fb) - 1;
        if (ia < 0 || ib < 0 || ia >= (int)faces.size() || ib >= (int)faces.size()) continue;
        nbr[ia].insert(ib);
        nbr[ib].insert(ia);
        GProp_GProps gp;
        BRepGProp::LinearProperties(e, gp);
        gp_Pnt cm = gp.CentreOfMass();
        Eigen::Vector3d M(cm.X(), cm.Y(), cm.Z());
        // Convex (outside corner) when each face's outward normal points AWAY from the other
        // face's interior; concave (inside corner/valley) when they point toward each other.
        bool convex = true;
        if (faces[ia].rep_dir && faces[ib].rep_dir) {
            const Eigen::Vector3d& nA = *faces[ia].rep_dir;
            const Eigen::Vector3d& nB = *faces[ib].rep_dir;
            double sgn = nA.dot(faces[ib].centroid - M) + nB.dot(faces[ia].centroid - M);
            convex = (sgn < 0.0);
        }
        std::string edge_surf = "other";
        try {
            BRepAdaptor_Curve ec(e);
            switch (ec.GetType()) {
                case GeomAbs_Line:         edge_surf = "line"; break;
                case GeomAbs_Circle:       edge_surf = "circle"; break;
                case GeomAbs_Ellipse:      edge_surf = "ellipse"; break;
                case GeomAbs_BSplineCurve: edge_surf = "bspline"; break;
                default:                   break;
            }
        } catch (const Standard_Failure&) {}
        edges.push_back({{"between", json::array({ia, ib})},
                         {"convex", convex},
                         {"surf", edge_surf},
                         {"length", round6(gp.Mass())}});
    }

    json jfaces = json::array();
    for (size_t i = 0; i < faces.size(); ++i) {
        json sel = aicad::doc::make_selector(faces[i]);  // reuse Stage-0 side/surf
        json nb = json::array();
        for (int n : nbr[i]) nb.push_back(n);
        jfaces.push_back({{"ord", (int)i},
                          {"surf", sel["surf"]},
                          {"side", sel["side"]},
                          {"concavity", sel["concavity"]},
                          {"neighbors", nb}});
    }
    return json{{"faces", jfaces}, {"edges", edges}};
}

// doc_set_meta / doc_get_meta — generic opaque document metadata, persisted inside the .step+
// exactly like `studies` (the engine never interprets it). The intake subsystem keeps its
// MeasuredPartProfile / ProcessProfile / CriticalDimensionLedger / FitReport / IntakeState here.
json doc_set_meta(const std::string& doc_id, const std::string& key, const json& value) {
    DocLock lk;
    Session& s = require_session(doc_id);
    if (!s.doc.contains("meta") || !s.doc["meta"].is_object())
        s.doc["meta"] = json::object();
    s.doc["meta"][key] = value;
    s.dirty = true;
    return json{{"ok", true}, {"key", key}};
}

json doc_get_meta(const std::string& doc_id, const std::string& key) {
    DocLock lk;
    Session& s = require_session(doc_id);
    json v = nullptr;
    if (s.doc.contains("meta") && s.doc["meta"].is_object() && s.doc["meta"].contains(key))
        v = s.doc["meta"][key];
    return json{{"ok", true}, {"key", key}, {"value", v}};
}

json doc_face_points(const std::string& doc_id, const json& selector, int max_n) {
    DocLock lk;
    Session& s = require_session(doc_id);

    if (!selector.is_object() || selector.value("kind", std::string()) != "face")
        throw OpError("invalid_op", "face_points requires a face selector");

    const CompileResult& r = ensure_compiled(s);
    if (r.shapes.empty())
        throw_no_bodies(doc_id, r, "evaluate");

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

    // Z1 FIX: For flat (planar) faces, OCCT's BRepMesh_IncrementalMesh only produces
    // the 4 boundary corners as triangulation nodes (chord deviation on a plane is 0 for
    // any interior point, so the mesher adds no interior nodes). When these 4 corners are
    // the only sample points, the proximity search in nodes_in_faceset uses a tol based on
    // the body diagonal (~2mm for a 101mm bar) — too small to reach the face center
    // (7.07mm away for a 10×10mm face) — so most gmsh mesh nodes on the end face are
    // EXCLUDED. With only 4 corner sample nodes picked, the work-consistent force
    // distribution concentrates the full 10000N into 4 instead of ~300 nodes → ~21×
    // stress spike (original bug), or with add_midside_face_nodes it still gives
    // corner-biased BCs → wrong stress.
    //
    // FIX: For planar faces, export the face normal vector alongside the sample points.
    // The solver (nodes_in_faceset_with_normal in fea_run.cpp) splits the proximity check
    // into (a) normal-direction distance < perp_tol_mm (tight: 1.5mm — any gmsh node
    // ON this face passes; a node on the OPPOSITE face of a 5mm plate is 5mm away →
    // rejected) and (b) in-plane distance < tol_mm (large: half of face diagonal — covers
    // the entire face surface). This is the ONLY correct fix for the thin-plate case
    // (Z2): a 100×100mm face with 100/2 = 50mm in-plane tol but only 1.5mm normal tol
    // correctly includes the 100×100 fixture face but excludes the top face 5mm away.
    // The face normal vector is added as "face_normal": [nx, ny, nz] in the response and
    // read by fea_run.cpp's resolve_faceset_samples(). Falls back to old Euclidean tol
    // when no normal is present (backward compat — e.g. manually-constructed studies).
    std::optional<std::array<double, 3>> face_normal;
    double tol_mm = std::max(0.5, diag * 0.02);  // default: old formula for curved faces
    {
        BRepAdaptor_Surface ad_face(face);
        if (ad_face.GetType() == GeomAbs_Plane && static_cast<int>(pts.size()) <= 8) {
            // Planar face with few triangulation nodes (just the 4 boundary corners).
            // Use a dense UV-parametric grid so the face is well-sampled in-plane.
            double u0, u1, v0, v1;
            BRepTools::UVBounds(face, u0, u1, v0, v1);
            Handle(Geom_Surface) surf = BRep_Tool::Surface(face);
            const int N = 7;  // 7×7 = 49 interior points for good in-plane coverage
            const double du = (u1 - u0) / (N + 1);
            const double dv = (v1 - v0) / (N + 1);
            for (int ii = 1; ii <= N; ++ii) {
                for (int jj = 1; jj <= N; ++jj) {
                    gp_Pnt p = surf->Value(u0 + ii * du, v0 + jj * dv);
                    pts.push_back({p.X(), p.Y(), p.Z()});
                }
            }
            // The in-plane tol = half the face diagonal: covers the full face plane.
            // For a 10×10 face: sqrt(10²+10²)/2 = 7.07mm — reaches any interior node.
            // For a 100×100 face: sqrt(100²+100²)/2 = 70.7mm — covers full face.
            // This is safe because the PERPENDICULAR check (via face_normal + perp_tol)
            // in nodes_in_faceset rejects any node not on the face plane.
            gp_Pnt p_corner0 = surf->Value(u0, v0);
            gp_Pnt p_corner1 = surf->Value(u1, v1);
            const double face_diag_2d = p_corner0.Distance(p_corner1);
            tol_mm = std::max(0.5, face_diag_2d * 0.5);
            // Outward face normal (signed: REVERSED → flip so normal points away from solid).
            gp_Dir dir = ad_face.Plane().Axis().Direction();
            if (face.Orientation() == TopAbs_REVERSED) dir.Reverse();
            face_normal = {dir.X(), dir.Y(), dir.Z()};
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
    json result = json{{"points", points}, {"tol_mm", tol_mm}};
    if (face_normal) {
        result["face_normal"] = {(*face_normal)[0], (*face_normal)[1], (*face_normal)[2]};
        // perp_tol: tight threshold in the face normal direction to exclude off-face nodes.
        // 1.5mm handles gmsh mesh noise (nodes on a face are at exactly the CAD plane, but
        // floating-point mesh positions may drift ≤ 0.1mm). The nearest off-face node for
        // a 5mm-thick plate is 5mm away → safely excluded. Thinner plates (e.g. 2mm) would
        // need perp_tol < 2mm — 1.5mm is conservative. For very thin (<2mm) plates the user
        // should use a different meshing strategy.
        result["perp_tol_mm"] = 1.5;
    }
    return result;
}

// Precise world axis-aligned bounding box of the whole document (all bodies), via
// BRepBndLib::AddOptimal (matches the sim/eval box). Returns {min,max,size,center} in mm.
// Used to hand callers (e.g. make_standard_part) the extents + a placement datum so they
// can position a part without a measure round-trip (Bug A-1).
json doc_bbox(const std::string& doc_id) {
    DocLock lk;
    Session& s = require_session(doc_id);
    CompileResult r = compile_document(s.doc, env_from_doc(s.doc));
    if (r.shapes.empty())
        throw_no_bodies(doc_id, r, "bound");
    Bnd_Box box;
    for (const TopoDS_Shape& shp : r.shapes)
        if (!shp.IsNull()) BRepBndLib::AddOptimal(shp, box);
    if (box.IsVoid())
        throw OpError("invalid_op", "bounding box is empty", {{"doc_id", doc_id}});
    Standard_Real xmin, ymin, zmin, xmax, ymax, zmax;
    box.Get(xmin, ymin, zmin, xmax, ymax, zmax);
    return json{
        {"min", {xmin, ymin, zmin}},
        {"max", {xmax, ymax, zmax}},
        {"size", {xmax - xmin, ymax - ymin, zmax - zmin}},
        {"center", {(xmin + xmax) / 2.0, (ymin + ymax) / 2.0, (zmin + zmax) / 2.0}},
    };
}

// Port of engine/doc/evaluate.py::mass_props (via engine/worker.py::_eval_mass). Computes
// volume/mass/centre-of-mass/inertia of the active body, plus principal moments+axes. A
// surface body (open shell / degenerate solid) returns the _surface_props area report.
// body_id is 1-based; <= 0 selects the LAST body (the _eval_active_shape default).
json doc_eval_mass(const std::string& doc_id, int body_id, double density_g_cm3) {
    DocLock lk;
    Session& s = require_session(doc_id);
    CompileResult r = compile_document(s.doc, env_from_doc(s.doc));
    if (r.shapes.empty())
        throw_no_bodies(doc_id, r, "evaluate");

    // Flatten the surviving bodies into their individual CONNECTED solids. An in-document
    // boolean fuse / boss-merge of two DISJOINT solids (e.g. two cylinders placed far
    // apart) yields a single TopoDS_Compound holding multiple TopAbs_SOLIDs — OCCT does
    // NOT weld non-touching solids — which the compile pipeline tracks as ONE body. For
    // mass properties we want the PHYSICAL body count, so explode each surviving shape
    // into its sub-solids and report each separately (count + per-body breakdown). A
    // surface / non-solid body (no SOLID inside) stays as a single entry. A shape that IS
    // a single solid maps to exactly one entry, so single-solid docs are unchanged.
    std::vector<TopoDS_Shape> solids;
    for (const TopoDS_Shape& sh : r.shapes) {
        TopTools_IndexedMapOfShape smap;
        TopExp::MapShapes(sh, TopAbs_SOLID, smap);
        if (smap.Extent() == 0) {
            solids.push_back(sh);  // surface / degenerate body — counts as one
        } else {
            for (int i = 1; i <= smap.Extent(); ++i) solids.push_back(smap.FindKey(i));
        }
    }

    const double density_g_mm3 = density_g_cm3 * 1e-3;  // 1 g/cm³ = 1e-3 g/mm³

    // Build the mass-props JSON block from a computed GProp_GProps (volume, mass, COM, the inertia
    // tensor about the global origin, and its principal moments/axes). Shared by the single-body
    // and the per-body assembly paths.
    auto props_json = [&](const GProp_GProps& props) -> json {
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
        };
    };

    // Single explicit body, OR a genuinely single-solid document: report that one body
    // (legacy shape). body_id and the per-body breakdown both index the EXPLODED solids,
    // so a disjoint fuse reports its true N>1 physical bodies instead of one compound.
    if (body_id >= 1 || solids.size() == 1) {
        const TopoDS_Shape* shape = nullptr;
        if (body_id >= 1) {
            if (body_id > static_cast<int>(solids.size()))
                throw OpError("invalid_op", "body_id " + std::to_string(body_id) + " out of range",
                              {{"reason", "bad body_id"}});
            shape = &solids[static_cast<std::size_t>(body_id - 1)];
        } else {
            shape = &solids.front();
        }
        GProp_GProps props;
        bool props_ok = false;
        if (eval_is_surface_shape(*shape, &props, &props_ok))
            return eval_surface_props(*shape, density_g_cm3);
        if (!props_ok)
            BRepGProp::VolumeProperties(*shape, props);  // unit density -> Mass() is volume in mm³
        json out = props_json(props);
        out["body_count"] = 1;
        out["material"] = {{"density_g_cm3", density_g_cm3}};
        // Bug J4: VolumeProperties on a non-closed solid returns a meaningless CoM/inertia
        // (the integral over an unbounded volume). Don't report it as if trustworthy — flag it.
        if (!eval_is_closed_solid(*shape)) {
            out["closed_solid"] = false;
            out["warning"] = "body is not a closed solid (free edges / open shell) — "
                             "volume, mass, centre of mass and inertia may be unreliable";
        }
        return out;
    }

    // MULTI-BODY (body_id <= 0): per-body breakdown PLUS the combined assembly total. Previously
    // this returned only the LAST body, silently under-reporting a multi-body doc. The top-level
    // fields now carry the TOTAL (so the in-app Mass Properties dialog shows the whole assembly);
    // `bodies[]` lists each solid. GProp_GProps::Add combines correctly (same origin reference).
    json bodies = json::array();
    GProp_GProps total;
    bool have_total = false;
    bool any_open = false;  // Bug J4: a non-closed solid taints its own + the assembly total
    for (std::size_t i = 0; i < solids.size(); ++i) {
        const TopoDS_Shape& shape = solids[i];
        json entry;
        entry["body_id"] = static_cast<int>(i + 1);
        GProp_GProps props;
        bool props_ok = false;
        if (eval_is_surface_shape(shape, &props, &props_ok)) {
            entry["surface"] = true;
            json sp = eval_surface_props(shape, density_g_cm3);
            for (auto it = sp.begin(); it != sp.end(); ++it) entry[it.key()] = it.value();
        } else {
            if (!props_ok) BRepGProp::VolumeProperties(shape, props);
            json pj = props_json(props);
            for (auto it = pj.begin(); it != pj.end(); ++it) entry[it.key()] = it.value();
            if (!eval_is_closed_solid(shape)) {
                entry["closed_solid"] = false;
                entry["warning"] = "body is not a closed solid (free edges / open shell) — "
                                   "its volume, mass, centre of mass and inertia may be unreliable";
                any_open = true;
            }
            total.Add(props);  // accumulate the assembly total (volume-weighted COM + inertia)
            have_total = true;
        }
        bodies.push_back(std::move(entry));
    }

    json out = have_total ? props_json(total)
                          : json{{"volume_mm3", 0.0}, {"mass_g", 0.0}};
    out["body_count"] = static_cast<int>(solids.size());
    out["bodies"] = std::move(bodies);
    out["material"] = {{"density_g_cm3", density_g_cm3}};
    if (any_open)
        out["warning"] = "one or more bodies are not closed solids — the combined total "
                         "mass / centre of mass / inertia may be unreliable";
    return out;
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
    DocLock lk;
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
        throw_no_bodies(doc_id, r, "evaluate");
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

// Resolve a measure ref to a single OCCT sub-shape — the SAME predicate-aware path doc_query
// uses (doc/picks::resolve_geom_query), so a measure ref honours pick/near/count/index exactly
// as the rest of the app does.
//
// WHY this matters (the symmetric-ambiguity bug it fixes): the prior code re-resolved the ref
// through the plain resolve() scorer. That scorer ranks a selector against describe_* candidates
// and REFUSES to guess when several equivalent candidates co-rank (margin 0 "symmetric
// ambiguity"). A cylinder's lone seam/round edge, or any one of a box's four identical vertical
// edges, therefore dangled — even though the resolved selector carries a UNIQUE centroid that
// pins down exactly one edge, and even though doc_query (resolve_geom_query) disambiguates the
// very same geometry. We now route through resolve_geom_query:
//   * a raw geom-query ref ({"of":"edge","pick":"longest"}) resolves with its predicate intact;
//   * a RESOLVED selector ref (kind+centroid, what _resolve_one_selector hands us) is turned into
//     {"of":kind,"near":<centroid>,"count":1} so its unique centroid singles out one sub-shape.
// pick:"longest"/"shortest" (and friends) break ties deterministically by taking the first of the
// stable MapShapes-ordered result; a genuinely UNQUALIFIED multi-match (no singling predicate)
// still errors loudly so the caller adds a predicate. Vertices (no resolve_geom_query support)
// keep the original describe_vertices + resolve() path.
TopoDS_Shape eval_resolve_ref(const json& ref, const TopoDS_Shape& shape, double diag) {
    // 1) build the predicate-aware query for an edge/face ref.
    json query;
    bool singling = false;  // does the query explicitly single out ONE sub-shape?
    if (aicad::doc::is_geom_query(ref)) {
        // A raw plain-words query — honour every predicate it carries. It is "singling" when it
        // names a pick/near/index or a count cap (the predicates that intentionally narrow to a
        // specific sub-shape and so license a deterministic tie-break on the survivors).
        query = ref;
        singling = ref.contains("pick") || ref.contains("near") || ref.contains("index") ||
                   ref.contains("count") || ref.contains("side");
    } else {
        const std::string kind = ref.value("kind", std::string());
        if (kind == "edge" || kind == "face") {
            // A resolved make_selector dict: re-express it as a query keyed on its UNIQUE
            // centroid so the symmetric describe_* duplicates can't co-rank it into a dangle.
            const json& c = ref.contains("centroid") ? ref.at("centroid") : json();
            if (c.is_array() && c.size() == 3 && c[0].is_number() && c[1].is_number() &&
                c[2].is_number()) {
                query = json{{"of", kind}, {"near", c}, {"count", 1}};
                singling = true;
            }
        }
        // vertices, or an edge/face selector with no centroid -> handled by the legacy path below.
    }

    if (!query.is_null()) {
        aicad::doc::QueryPicks qp = aicad::doc::resolve_geom_query(shape, query);
        if (!qp.error.empty())
            throw OpError("invalid_op", "measure ref: " + qp.error, {{"reason", "bad query"}});
        const std::size_t n = qp.faces.size() + qp.edges.size();
        if (n == 0)
            throw OpError("invalid_op", "measure ref matched no geometry",
                          {{"reason", "no match"}});
        if (n > 1 && !singling)
            throw OpError("invalid_op",
                          "measure ref matched " + std::to_string(n) +
                              " candidates, not one — narrow it with a predicate "
                              "(pick/side/near/count)",
                          {{"reason", "ambiguous"}});
        // exactly one, OR a singling predicate that deterministically picks the first of the
        // stable-ordered survivors (the longest/shortest tie-break the caller asked for).
        if (!qp.faces.empty()) return qp.faces.front();
        return qp.edges.front();
    }

    // 2) legacy describe_* + resolve() path (vertices, and selectors without a centroid).
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
    DocLock lk;
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

    // Free-edge census: edges with fewer than 2 adjacent faces — BUT a SEAM edge of a
    // periodic face (the 0°/360° closure of a cylinder/sphere/full-revolve) and a
    // DEGENERATE edge (a cone apex / sphere pole) are real, watertight B-rep topology that
    // legitimately bound just one face. MapShapesAndAncestors lists a seam's parent face a
    // single time, so the naive "< 2 faces == free" census flagged every cylindrical bore /
    // fillet / chamfer seam as a "free edge" and reported valid CLOSED solids as open shells
    // (bugs H3/H4/H5/J4). Exclude seams (BRep_Tool::IsClosed(edge,face)) and degenerate edges
    // so only the genuine boundary of an open shell (a lone face, an unsewn import) is
    // counted. This is precise — a real free edge has NO seam/degenerate marker, so an
    // honestly-open shell still reports its real free-edge count.
    TopTools_IndexedDataMapOfShapeListOfShape anc_map;
    TopExp::MapShapesAndAncestors(shape, TopAbs_EDGE, TopAbs_FACE, anc_map);
    TopTools_IndexedMapOfShape edge_map;
    TopExp::MapShapes(shape, TopAbs_EDGE, edge_map);

    int free_edges = 0;
    for (int ei = 1; ei <= edge_map.Extent(); ++ei) {
        const TopoDS_Shape& edge_sh = edge_map.FindKey(ei);
        int n_faces = anc_map.Contains(edge_sh) ? anc_map.FindFromKey(edge_sh).Extent() : 0;
        if (n_faces >= 2) continue;  // shared by two faces -> interior, never free
        const TopoDS_Edge& edge = TopoDS::Edge(edge_sh);
        // A degenerate edge (cone apex / sphere pole) bounds one face but is not free.
        if (BRep_Tool::Degenerated(edge)) continue;
        // A seam edge is closed on its (single) parent face — periodic-surface closure, not
        // a free boundary. The face appears once in the ancestor list yet the edge is glued
        // to itself across the seam, so it is fully shared.
        bool is_seam = false;
        if (anc_map.Contains(edge_sh)) {
            const TopTools_ListOfShape& faces = anc_map.FindFromKey(edge_sh);
            for (TopTools_ListIteratorOfListOfShape it(faces); it.More(); it.Next()) {
                if (BRep_Tool::IsClosed(edge, TopoDS::Face(it.Value()))) { is_seam = true; break; }
            }
        }
        if (is_seam) continue;
        ++free_edges;
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

// engine/doc/evaluate.py::section_props via worker.py::_eval_section_props. Cut the body by
// the cutting plane (BRepAlgoAPI_Section -> a compound of edges), GROUP those edges into the
// closed loop(s) of the cross-section, build the planar face (outer loop bounded by inner
// loops as HOLES), and read the REAL cross-section properties off the face: area =
// |SurfaceProperties.Mass| (so a hollow/annular section = outer - holes), centroid = its CoM,
// and the 2nd area moments Ixx/Iyy/Ixy about the CENTROID expressed in the CUTTING PLANE'S
// local (u,v) frame. Falls back to the section-wire (LinearProperties) centroid with zero
// area/moments only when no face can be built (open/degenerate section).
//
// Two bugs fixed here (vs the prior "force ALL edges into ONE wire / read GLOBAL-axis
// MatrixOfInertia" code):
//   * Multi-loop sections (a tube wall = outer loop + inner bore loop) made the single
//     MakeWire fail -> area 0. Now ShapeAnalysis_FreeBounds::ConnectEdgesToWires groups the
//     edges into separate closed wires and the largest-area wire becomes the face boundary
//     with the rest added as holes, so an annulus reports the wall area.
//   * Ixx/Iyy were read straight off MatrixOfInertia(1,1)/(2,2) which are GLOBAL-axis area
//     moments — correct only for an XY (top) cut. For a front/right cut they mixed in the
//     out-of-plane coordinate. We now express the face in the plane's LOCAL frame located at
//     the section centroid (a gp_Trsf SetTransformation about the centroidal local Ax3) so the
//     in-plane second moments fall straight out of the transformed face's MatrixOfInertia. For
//     an XY cut of a centred body that transform is the identity, so the top-cut result is
//     unchanged.
json doc_eval_section_props(const std::string& doc_id, const json& plane_ref) {
    DocLock lk;
    Session& s = require_session(doc_id);
    CompileResult r = compile_document(s.doc, env_from_doc(s.doc));
    if (r.shapes.empty())
        throw_no_bodies(doc_id, r, "evaluate");
    const TopoDS_Shape& shape = r.shapes.back();  // worker: bodies[-1]
    gp_Pln pln = eval_resolve_plane(plane_ref);

    double area = 0.0, cx = 0.0, cy = 0.0, cz = 0.0, Ixx = 0.0, Iyy = 0.0, Ixy = 0.0;
    try {
        BRepAlgoAPI_Section section(shape, pln, Standard_False);
        section.Build();
        if (section.IsDone()) {
            const TopoDS_Shape& sec_shape = section.Shape();

            try {
                TopTools_IndexedMapOfShape edge_map;
                TopExp::MapShapes(sec_shape, TopAbs_EDGE, edge_map);
                if (edge_map.Extent() > 0) {
                    // Tolerance for joining edge ends into loops — relative to the body size so
                    // section sliver gaps still connect on big and small parts alike.
                    const double join_tol = std::max(sim_bbox_diag(shape) * 1e-4, 1e-6);

                    // Group the section edges into closed loops. A solid box/cylinder yields ONE
                    // loop; a hollow/annular section yields an outer loop + one inner loop per
                    // bore. (The old single MakeWire could only ever hold one loop -> area 0 on
                    // anything hollow.) shared=false joins by end-point proximity (join_tol),
                    // robust to the section's free-edge vertices not being shared.
                    Handle(TopTools_HSequenceOfShape) in_edges = new TopTools_HSequenceOfShape();
                    for (int ei = 1; ei <= edge_map.Extent(); ++ei)
                        in_edges->Append(edge_map.FindKey(ei));
                    Handle(TopTools_HSequenceOfShape) wires;
                    ShapeAnalysis_FreeBounds::ConnectEdgesToWires(in_edges, join_tol,
                                                                  Standard_False, wires);

                    // Build a face per closed wire; keep its area, wire and a 2D polygon (the
                    // wire's vertices projected into the plane's (u,v) basis) so we can classify
                    // loop NESTING. A solid box yields one loop; an annulus yields a big outer
                    // loop + an inner bore loop (the bore is INSIDE the outer); a tube cut along
                    // its length yields TWO DISJOINT solid loops (neither inside the other).
                    const gp_Ax3& pos = pln.Position();
                    const gp_Pnt o3 = pos.Location();
                    const gp_Dir ud = pos.XDirection();
                    const gp_Dir vd = pos.YDirection();
                    auto to_uv = [&](const gp_Pnt& p) -> Eigen::Vector2d {
                        gp_Vec d(o3, p);
                        return Eigen::Vector2d(d.Dot(gp_Vec(ud)), d.Dot(gp_Vec(vd)));
                    };
                    // Sample a wire into a 2D polygon (plane u,v coords). We sample ALONG each
                    // edge's curve — not just the vertices — so a CIRCULAR loop (which carries a
                    // single seam vertex) still yields a real polygon for the point-in-loop test;
                    // a vertex-only polygon would have <3 points and wrongly report "not inside",
                    // breaking the annulus (bore-inside-outer) classification.
                    auto wire_poly = [&](const TopoDS_Wire& w) -> std::vector<Eigen::Vector2d> {
                        std::vector<Eigen::Vector2d> poly;
                        for (TopExp_Explorer ee(w, TopAbs_EDGE); ee.More(); ee.Next()) {
                            try {
                                BRepAdaptor_Curve ca(TopoDS::Edge(ee.Current()));
                                const double t0 = ca.FirstParameter(), t1 = ca.LastParameter();
                                const int N = 16;  // straight edges collapse to their 2 ends
                                for (int k = 0; k < N; ++k)
                                    poly.push_back(to_uv(ca.Value(t0 + (t1 - t0) * k / N)));
                            } catch (const Standard_Failure&) {
                            }
                        }
                        return poly;
                    };
                    struct Loop { TopoDS_Wire wire; double area; std::vector<Eigen::Vector2d> poly;
                                  Eigen::Vector2d cuv{0, 0}; };
                    std::vector<Loop> loops;
                    if (!wires.IsNull()) {
                        for (int wi = 1; wi <= wires->Length(); ++wi) {
                            const TopoDS_Shape& ws = wires->Value(wi);
                            if (ws.ShapeType() != TopAbs_WIRE) continue;
                            TopoDS_Wire w = TopoDS::Wire(ws);
                            BRepBuilderAPI_MakeFace fm(pln, w);
                            if (!fm.IsDone()) continue;
                            GProp_GProps wp;
                            BRepGProp::SurfaceProperties(fm.Face(), wp);
                            Loop lp;
                            lp.wire = w;
                            lp.area = std::abs(wp.Mass());
                            lp.poly = wire_poly(w);
                            gp_Pnt c = wp.CentreOfMass();
                            lp.cuv = to_uv(c);
                            loops.push_back(std::move(lp));
                        }
                    }
                    // Fallback for the single-loop path when grouping returned nothing usable
                    // (e.g. one already-closed wire OCCT didn't re-emit): stitch every edge into
                    // one wire, exactly as before, so the solid-section path stays correct.
                    if (loops.empty()) {
                        BRepBuilderAPI_MakeWire wb;
                        for (int ei = 1; ei <= edge_map.Extent(); ++ei)
                            wb.Add(TopoDS::Edge(edge_map.FindKey(ei)));
                        if (wb.IsDone()) {
                            BRepBuilderAPI_MakeFace fm(pln, wb.Wire());
                            if (fm.IsDone()) {
                                GProp_GProps wp;
                                BRepGProp::SurfaceProperties(fm.Face(), wp);
                                Loop lp;
                                lp.wire = wb.Wire();
                                lp.area = std::abs(wp.Mass());
                                gp_Pnt c = wp.CentreOfMass();
                                lp.cuv = to_uv(c);
                                loops.push_back(std::move(lp));
                            }
                        }
                    }

                    if (!loops.empty()) {
                        // EVEN-ODD nesting: a loop's depth = how many OTHER loops contain it
                        // (its centroid inside their polygon). Depth 0/2/… = SOLID material;
                        // depth 1/3/… = a HOLE in the solid that immediately encloses it. This
                        // is correct for an annulus (outer depth 0, bore depth 1 -> a hole) AND
                        // for two disjoint solids (both depth 0 -> two separate material faces),
                        // unlike a naive "biggest loop is outer, rest are holes" rule which would
                        // wrongly punch one disjoint solid out of the other.
                        const std::size_t n = loops.size();
                        std::vector<int> depth(n, 0);
                        std::vector<int> parent(n, -1);  // immediate (smallest) enclosing loop
                        auto poly_inside = [](const Eigen::Vector2d& pt,
                                              const std::vector<Eigen::Vector2d>& poly) -> bool {
                            const std::size_t m = poly.size();
                            if (m < 3) return false;
                            bool in = false;
                            for (std::size_t i = 0, j = m - 1; i < m; j = i++) {
                                double xi = poly[i].x(), yi = poly[i].y();
                                double xj = poly[j].x(), yj = poly[j].y();
                                if (((yi > pt.y()) != (yj > pt.y())) &&
                                    (pt.x() < (xj - xi) * (pt.y() - yi) / (yj - yi + 1e-30) + xi))
                                    in = !in;
                            }
                            return in;
                        };
                        for (std::size_t i = 0; i < n; ++i) {
                            double parent_area = std::numeric_limits<double>::infinity();
                            for (std::size_t j = 0; j < n; ++j) {
                                if (i == j) continue;
                                // i is inside j only if j is STRICTLY LARGER — without this guard
                                // CONCENTRIC loops (a bore and its outer wall share a centroid)
                                // each test "inside" the other and both collapse to holes (area 0).
                                if (loops[j].area <= loops[i].area) continue;
                                if (poly_inside(loops[i].cuv, loops[j].poly)) {
                                    ++depth[i];
                                    if (loops[j].area < parent_area) {  // smallest enclosing = parent
                                        parent_area = loops[j].area;
                                        parent[i] = static_cast<int>(j);
                                    }
                                }
                            }
                        }

                        // One face per SOLID loop, with its direct HOLE children added. Collect
                        // every solid face into a compound so disjoint material regions SUM.
                        TopoDS_Compound sec_comp;
                        BRep_Builder cb;
                        cb.MakeCompound(sec_comp);
                        bool any_face = false;
                        for (std::size_t i = 0; i < n; ++i) {
                            if (depth[i] % 2 != 0) continue;  // odd depth = a hole, not material
                            BRepBuilderAPI_MakeFace fm(pln, loops[i].wire);
                            for (std::size_t h = 0; h < n; ++h) {
                                if (depth[h] % 2 == 0 || parent[h] != static_cast<int>(i)) continue;
                                // A hole wire must run opposite to its outer wire; MakeFace::Add
                                // treats the added wire as an inner boundary and subtracts it.
                                TopoDS_Wire hole = loops[h].wire;
                                hole.Reverse();
                                fm.Add(hole);
                            }
                            if (fm.IsDone()) { cb.Add(sec_comp, fm.Face()); any_face = true; }
                        }

                        if (any_face) {
                            GProp_GProps fp;
                            BRepGProp::SurfaceProperties(sec_comp, fp);
                            area = std::abs(fp.Mass());
                            gp_Pnt com = fp.CentreOfMass();
                            cx = com.X();
                            cy = com.Y();
                            cz = com.Z();

                            // Ixx/Iyy/Ixy in the CUTTING PLANE's local frame, about the section
                            // centroid. Re-express the section in a frame whose origin is the
                            // centroid and whose X/Y axes are the plane's in-plane u/v: a
                            // gp_Trsf::SetTransformation(localAx3) maps a point's GLOBAL coords
                            // into that frame's LOCAL coords, so after the transform the section
                            // lies in z=0 with its centroid at the origin. Its MatrixOfInertia is
                            // then the centroidal planar tensor: (1,1)=∫v²dA=Ixx (about local u),
                            // (2,2)=∫u²dA=Iyy (about local v), (1,2)=-∫uv dA=Ixy. (Reading the
                            // GLOBAL-axis MatrixOfInertia directly only happens to be right for an
                            // XY/top cut; a front/right cut mixed in the out-of-plane axis.)
                            gp_Ax3 local(com, pos.Direction(), pos.XDirection());
                            gp_Trsf to_local;
                            to_local.SetTransformation(local);
                            TopoDS_Shape lf =
                                BRepBuilderAPI_Transform(sec_comp, to_local, true).Shape();
                            GProp_GProps lp;
                            BRepGProp::SurfaceProperties(lf, lp);
                            const gp_Mat& mat = lp.MatrixOfInertia();  // 1-based, about origin
                            Ixx = mat.Value(1, 1);
                            Iyy = mat.Value(2, 2);
                            Ixy = mat.Value(1, 2);
                        }
                    }
                }
            } catch (const Standard_Failure&) {
                // face build failed -> fall through to the wire-centroid fallback below
            }

            // Fallback: a centroid from the section wire when no face could be built (open
            // or degenerate section); leaves area/moments at 0.
            if (area == 0.0 && cx == 0.0 && cy == 0.0 && cz == 0.0) {
                GProp_GProps lin;
                BRepGProp::LinearProperties(sec_shape, lin);
                gp_Pnt com = lin.CentreOfMass();
                cx = com.X();
                cy = com.Y();
                cz = com.Z();
            }
        }
    } catch (const Standard_Failure&) {
        // matches the oracle's broad except -> zero everything
    }

    return json{
        {"area_mm2", area},
        {"centroid", {cx, cy, cz}},
        {"Ixx", Ixx},
        {"Iyy", Iyy},
        {"Ixy", Ixy},
    };
}

// engine/doc/evaluate.py::symmetry via worker.py::_eval_symmetry. Mirror the active body
// across the plane and measure the symmetric-difference volume. symmetric iff deviation
// <= 1e-3 * max(vol, 1.0). Returns {"symmetric", "deviation_mm3"}.
json doc_eval_symmetry(const std::string& doc_id, int body_id, const json& plane_ref) {
    DocLock lk;
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

// Pairwise interference report over a list of named (name, shape) members — the shared core
// of engine/doc/evaluate.py::interference (kernel.py::_fit_report). Pairwise overlap
// (BRepAlgoAPI_Common volume) + clearance (BRepExtrema min distance), per-part bbox.
// ok iff nothing interferes.
json interference_report(const std::vector<std::pair<std::string, TopoDS_Shape>>& members) {
    const std::size_t n = members.size();
    constexpr double INTERFERENCE_EPS = 1e-3;

    // Per-part bbox + volume (kernel.py::_bbox_dict uses build123d bounding_box() ->
    // BRepBndLib::AddOptimal, the precise box, after BRepTools::Clean).
    json parts = json::array();
    for (std::size_t i = 0; i < n; ++i) {
        const TopoDS_Shape& shp = members[i].second;
        json part = {{"name", members[i].first}, {"volume", eval_vol_or_zero(shp)}};
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
        const std::string& name_i = members[i].first;
        for (std::size_t j = i + 1; j < n; ++j) {
            const std::string& name_j = members[j].first;
            // overlap volume (None on boolean failure -> Python warns + continue).
            bool ov_ok = false;
            double ov = 0.0;
            try {
                BRepAlgoAPI_Common common(members[i].second, members[j].second);
                common.Build();
                if (common.IsDone()) {
                    ov = eval_vol_or_zero(common.Shape());
                    ov_ok = true;
                }
            } catch (const Standard_Failure&) {
            }
            if (!ov_ok) continue;  // matches the warnings-sink "interference check failed" path
            if (ov > INTERFERENCE_EPS) {
                interferences.push_back(json{{"a", name_i}, {"b", name_j}, {"volume", ov}});
            } else {
                try {
                    BRepExtrema_DistShapeShape dss(members[i].second, members[j].second);
                    if (dss.IsDone())
                        clearances.push_back(
                            json{{"a", name_i}, {"b", name_j}, {"distance", dss.Value()}});
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

// engine/doc/evaluate.py::interference via worker.py::_eval_interference.
//   part doc      -> members = every surviving body, names "b1".."bN" (AICM order).
//   assembly doc  -> members = WORLD-placed component bodies, names = component names (port of
//                    session.py::asm_world_bodies, explode=False). This mirrors the oracle's
//                    s.is_assembly branch — without it an assembly always reported "no interference"
//                    because compile_document(asm).shapes is empty.
json doc_eval_interference(const std::string& doc_id) {
    DocLock lk;
    Session& s = require_session(doc_id);

    std::vector<std::pair<std::string, TopoDS_Shape>> members;
    if (s.is_assembly()) {
        if (!s.asm_result) rebuild_assembly(s);
        const AssemblyResult* ares = s.asm_result.get();
        if (ares)
            for (const AsmBodyOut& body : ares->bodies) {
                // interference measures the REAL assembly: no explode offset (explode=False).
                TopoDS_Shape world = placed_world_shape(body, std::nullopt);
                members.emplace_back(body.name, world);
            }
    } else {
        CompileResult r = compile_document(s.doc, env_from_doc(s.doc));
        for (std::size_t i = 0; i < r.shapes.size(); ++i)
            members.emplace_back("b" + std::to_string(i + 1), r.shapes[i]);
    }
    return interference_report(members);
}

// engine/doc/evaluate.py::measure via worker.py::_eval_measure. Resolve 1-2 refs against
// the LAST body, then single-entity or pair measure. Refs resolve in the SAME enumeration
// describe_* + body.faces()/edges()/vertices() use (a TopExp re-walk).
json doc_eval_measure(const std::string& doc_id, const json& refs) {
    DocLock lk;
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
//
// TWO-SIDED (intentional, load-bearing for wall thickness): the determinant test below is
// `std::abs(a) <= EPS` — it rejects ONLY rays parallel to a triangle, accepting BOTH front-
// and back-facing triangles. This is what makes the thickness cast (doc_eval_thickness) find
// the NEAREST opposite wall: the near inner-shell wall is wound toward the cavity (back-facing
// to an inward ray), so a one-sided / back-face-culling cast would SKIP it and cross the whole
// cavity to the far wall (reporting the cavity span, not the wall). Do NOT add a sign-of-`a`
// cull here without giving doc_eval_thickness its own two-sided path.
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
// -1,1))) for the normalized vertex normal n and normalized pull. chunk name "draft". Beyond
// the field, returns the moldability summary: min/max draft over the body, and (using
// angle_deg as the minimum-draft threshold, default 3°) the count + list of FACES whose
// worst-case |draft| is below it — the walls that would drag on the mold during ejection.
json doc_eval_draft(const std::string& doc_id, int body_id, const std::array<double, 3>& pull_in,
                    double angle_deg) {
    DocLock lk;
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

    // Per-FACE moldability classification. For each B-rep face, take its worst-case (minimum
    // over the face's vertices) absolute draft, and its area + area-weighted centroid. A face
    // is "below threshold" iff its worst |draft| < threshold (the shallowest point governs
    // whether it drags on the mold). threshold = angle_deg when given, else the common 3°
    // injection-molding minimum.
    const double threshold = (angle_deg > 0.0) ? angle_deg : 3.0;
    json below = json::array();
    int n_below = 0;
    for (const aicm::FaceRec& fr : body.faces) {
        if (fr.tri_count == 0) continue;
        std::size_t lo = static_cast<std::size_t>(fr.first_tri) * 3;
        std::size_t hi = static_cast<std::size_t>(fr.first_tri + fr.tri_count) * 3;
        if (hi > body.idx.size()) hi = body.idx.size();

        double face_area = 0.0;
        double cw[3] = {0.0, 0.0, 0.0};  // area-weighted centroid accumulator
        double min_abs_draft = std::numeric_limits<double>::infinity();
        bool have_draft = false;
        for (std::size_t k = lo; k + 2 < hi; k += 3) {
            uint32_t a = body.idx[k], b = body.idx[k + 1], c = body.idx[k + 2];
            if (a >= n || b >= n || c >= n) continue;
            const float* pa = &body.pos[3 * a];
            const float* pb = &body.pos[3 * b];
            const float* pc = &body.pos[3 * c];
            double e1[3] = {pb[0] - pa[0], pb[1] - pa[1], pb[2] - pa[2]};
            double e2[3] = {pc[0] - pa[0], pc[1] - pa[1], pc[2] - pa[2]};
            double cx_ = e1[1] * e2[2] - e1[2] * e2[1];
            double cy_ = e1[2] * e2[0] - e1[0] * e2[2];
            double cz_ = e1[0] * e2[1] - e1[1] * e2[0];
            double tri_area = 0.5 * std::sqrt(cx_ * cx_ + cy_ * cy_ + cz_ * cz_);
            face_area += tri_area;
            double tcx = (pa[0] + pb[0] + pc[0]) / 3.0;
            double tcy = (pa[1] + pb[1] + pc[1]) / 3.0;
            double tcz = (pa[2] + pb[2] + pc[2]) / 3.0;
            cw[0] += tcx * tri_area;
            cw[1] += tcy * tri_area;
            cw[2] += tcz * tri_area;
            for (uint32_t vi : {a, b, c}) {
                double dr = std::abs(static_cast<double>(values[vi]));
                if (std::isfinite(dr)) {
                    min_abs_draft = std::min(min_abs_draft, dr);
                    have_draft = true;
                }
            }
        }
        if (!have_draft || face_area <= 0.0) continue;
        if (min_abs_draft < threshold) {
            ++n_below;
            below.push_back(json{
                {"face_id", fr.face_id},
                {"min_draft_deg", min_abs_draft},
                {"area_mm2", face_area},
                {"centroid", {cw[0] / face_area, cw[1] / face_area, cw[2] / face_area}},
            });
        }
    }

    auto chunk = aicm::scalar_chunk(body.body_id, "draft", values, vmin, vmax);
    return json{
        {"aicm_path", field_write_temp_aicm(body, chunk)},
        {"min_draft_deg", any_finite ? json(static_cast<double>(vmin)) : json(nullptr)},
        {"max_draft_deg", any_finite ? json(static_cast<double>(vmax)) : json(nullptr)},
        {"threshold_deg", threshold},
        {"n_faces_below_threshold", n_below},
        {"faces_below_threshold", below},
    };
}

// engine/doc/evaluate.py::curvature_field. Re-mesh (BRepMesh 0.1) to match the oracle's face
// ordering, then per-vertex principal curvatures via BRepLProp_SLProps at the vertex's UV
// (projected via ShapeAnalysis_Surface::ValueOfUV). kind: mean=(k1+k2)/2, gaussian=k1*k2,
// max=max(|k1|,|k2|). chunk name "curvature_<kind>". Verts with no defined curvature stay NaN.
json doc_eval_curvature(const std::string& doc_id, int body_id, const std::string& kind) {
    DocLock lk;
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
    json out{{"aicm_path", field_write_temp_aicm(body, chunk)}};
    out["min_curvature"] = any ? json(static_cast<double>(vmin)) : json(nullptr);
    out["max_curvature"] = any ? json(static_cast<double>(vmax)) : json(nullptr);
    out["kind"] = kind;
    return out;
}

// engine/doc/evaluate.py::thickness_field. Per-vertex inward-raycast wall thickness: from each
// vertex offset 1e-3 along -normal (the inward direction, since field_vertex_normal returns the
// OUTWARD surface normal), cast -normal and take the NEAREST re-intersection distance (minus the
// offset). The cast is TWO-SIDED (field_moller_trumbore_all accepts triangles of either winding)
// so it stops at the FIRST opposite wall — for a hollow shell that is the thin wall, not the far
// side across the cavity. The oracle's primary path is trimesh BVH; we port the deterministic
// Moller-Trumbore FALLBACK (_moller_trumbore_all). chunk name "thickness". NOTE: parity here is
// PHYSICAL (a tolerance band), not bit-exact — the oracle's BVH runs on a divergent mesh.
// CAVEAT: thickness is a property of the SOLID. A solid block reports its full span (a 40 mm
// cube -> ~40 mm), which is correct; only a body with a genuine cavity (e.g. an open shell)
// has a thin wall to report (~2 mm for a 2 mm shell).
json doc_eval_thickness(const std::string& doc_id, int body_id) {
    DocLock lk;
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
    double sum = 0.0;
    std::size_t n_sampled = 0;  // vertices with a valid through-wall hit
    bool any = false;
    for (std::size_t i = 0; i < n; ++i) {
        double d = hits[i];
        if (std::isfinite(d) && d > OFFSET) {
            float t = static_cast<float>(d - OFFSET);
            values[i] = t;
            sum += t;
            ++n_sampled;
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
    json out{{"aicm_path", field_write_temp_aicm(body, chunk)}};
    // Numeric summary over the sampled (valid-hit) vertices. min is the key value — the
    // thinnest wall. null when no vertex got a through-wall hit (e.g. an open/sheet body).
    if (n_sampled > 0) {
        out["min_thickness_mm"] = static_cast<double>(vmin);
        out["max_thickness_mm"] = static_cast<double>(vmax);
        out["mean_thickness_mm"] = sum / static_cast<double>(n_sampled);
    } else {
        out["min_thickness_mm"] = nullptr;
        out["max_thickness_mm"] = nullptr;
        out["mean_thickness_mm"] = nullptr;
    }
    out["n_sampled"] = static_cast<int>(n_sampled);
    return out;
}

json doc_push_pull(const std::string& doc_id, int body_id, int topo_id, double distance,
                   bool commit) {
    DocLock lk;
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

json doc_export_file(const std::string& doc_id, const std::string& path, const std::string& fmt,
                     double lin_deflection, double ang_deflection, bool relative, bool ascii) {
    DocLock lk;
    Session& s = require_session(doc_id);
    if (path.empty())
        throw OpError("invalid_op", "export_file: 'path' is required", {{"reason", "missing path"}});
    if (fmt.empty())
        throw OpError("invalid_op", "export_file: 'format' is required", {{"reason", "missing format"}});
    std::vector<TopoDS_Shape> shapes = gather_export_shapes(s);  // assembly-aware (world-placed)
    if (shapes.empty())
        throw OpError("export_failed", "export_file: document has no bodies to export",
                      {{"doc_id", doc_id}});
    TessOptions tess;
    if (lin_deflection > 0.0) tess.lin_deflection = lin_deflection;
    if (ang_deflection > 0.0) tess.ang_deflection = ang_deflection;
    tess.relative = relative;
    tess.ascii = ascii;
    // Count the ACTUAL solids handed to the writer (Issue 3). A single body can be a COMPOUND
    // of disjoint lumps (a boolean result), so the body count understates how many solids a
    // re-read recovers — which made the round-trip check report a misleading "3/1 solids" for
    // a multi-body export and, on the steering body, reject a perfectly good file. Counting
    // solids here matches what the (per-solid) STEP writer emits and what import_bodies reads
    // back. A body with no solid (an open sheet/shell surface) counts as one unit.
    std::size_t expected_solids = 0;
    for (const TopoDS_Shape& b : shapes) {
        if (b.IsNull()) continue;
        std::size_t n = 0;
        for (TopExp_Explorer ex(b, TopAbs_SOLID); ex.More(); ex.Next()) ++n;
        expected_solids += (n > 0 ? n : 1);
    }
    if (expected_solids == 0) expected_solids = shapes.size();
    std::string out = export_bodies(shapes, path, fmt, tess);  // throws on a writer error

    // Real metadata + a flush check: never report success on a missing/empty (truncated) file.
    std::error_code fec;
    const std::uintmax_t bytes = std::filesystem::exists(out, fec)
                                     ? std::filesystem::file_size(out, fec) : 0;
    if (bytes == 0)
        throw OpError("export_failed", "export_file: wrote 0 bytes (file missing or truncated)",
                      {{"path", out}, {"format", fmt}});

    json meta{{"path", out}, {"format", fmt}, {"bytes", static_cast<double>(bytes)},
              {"expected_solids", static_cast<int>(expected_solids)}};

    // Round-trip B-rep formats: re-read with the SAME kernel and confirm the solids survived and
    // are topologically valid BEFORE returning success. This catches the "STEP/IGES won't open"
    // class (truncated/garbled writes, dropped solids) loudly instead of a false ok:true. Mesh
    // formats have no solids to recover, so we report bytes only.
    const std::string f = fmt;
    const bool is_brep_fmt = (f == "step" || f == "stp" || f == "iges" || f == "igs" ||
                              f == "brep" || f == "step+" || f == "stepplus");
    if (is_brep_fmt) {
        try {
            auto reread = import_bodies(out, (f == "stp") ? "step" : (f == "igs") ? "iges" : f);
            std::size_t valid = 0;
            for (const auto& nb : reread)
                if (!nb.second.IsNull() && BRepCheck_Analyzer(nb.second).IsValid()) ++valid;
            meta["solid_count"] = static_cast<int>(reread.size());
            meta["valid_solids"] = static_cast<int>(valid);
            // IGES is a face-soup format: solids may sew into fewer/zero TopAbs_SOLIDs, so we
            // only HARD-fail step/brep on a solid-count mismatch; IGES just reports what it read.
            const bool count_ok = (f == "iges" || f == "igs") ? (reread.size() > 0)
                                                              : (reread.size() >= expected_solids);
            meta["validated"] = (count_ok && valid == reread.size() && !reread.empty());
            if (!meta["validated"].get<bool>())
                throw OpError("export_failed",
                              "export_file: wrote " + out + " but re-reading it recovered " +
                                  std::to_string(reread.size()) + "/" +
                                  std::to_string(expected_solids) + " solids (" +
                                  std::to_string(valid) + " valid) — the file is incomplete or "
                                  "malformed and would not open elsewhere",
                              {{"path", out}, {"format", fmt}});
        } catch (const OpError&) {
            throw;
        } catch (const std::exception& e) {
            throw OpError("export_failed",
                          std::string("export_file: ") + out +
                              " failed round-trip validation (not re-openable): " + e.what(),
                          {{"path", out}, {"format", fmt}});
        }
    } else {
        meta["validated"] = true;  // mesh formats: bytes>0 is the only integrity signal we have
    }
    return meta;
}

json doc_export_estimate(const std::string& doc_id, double lin_deflection, double ang_deflection,
                         bool relative) {
    DocLock lk;
    Session& s = require_session(doc_id);
    std::vector<TopoDS_Shape> shapes = gather_export_shapes(s);  // assembly-aware (world-placed)
    TessOptions tess;
    if (lin_deflection > 0.0) tess.lin_deflection = lin_deflection;
    if (ang_deflection > 0.0) tess.ang_deflection = ang_deflection;
    tess.relative = relative;
    auto [tris, verts] = estimate_tessellation(shapes, tess);
    return json{{"triangles", tris}, {"vertices", verts}, {"bodies", shapes.size()}};
}

// True iff `path` has the native .step+ container extension (case-insensitive). Used to
// decide whether to read a native document or import a neutral file.
static bool is_stepplus_path(const std::string& path) {
    std::string ext = std::filesystem::path(path).extension().string();
    for (char& c : ext) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return ext == ".step+";
}

// Make `base` unique among the existing feature DISPLAY names in `s` by appending _2, _3, …
// (Issue 10). Importing the same file repeatedly used to add several features all named e.g.
// "HP_Rover_Wheel", which the compiler then flagged with a duplicate-name warning on every
// import. Uniquifying at insert time keeps the tree readable and silences the noise. Caller
// must hold the doc lock (reads s.doc). Empty base is returned as-is.
static std::string unique_feature_name(const Session& s, const std::string& base) {
    if (base.empty()) return base;
    std::set<std::string> used;
    if (s.doc.contains("features") && s.doc["features"].is_array())
        for (const json& f : s.doc["features"])
            if (f.is_object() && f.contains("name") && f["name"].is_string())
                used.insert(f["name"].get<std::string>());
    if (!used.count(base)) return base;
    for (int i = 2;; ++i) {
        std::string cand = base + "_" + std::to_string(i);
        if (!used.count(cand)) return cand;
    }
}

json doc_import_file(const std::string& path, const std::string& into, bool as_solid,
                     const std::string& fmt) {
    // A native .step+ is a DOCUMENT, not a neutral body file: reopen it as a part doc
    // instead of failing on the format sniffer (Bug N-4 — import_part rejected the very
    // extension save_part writes). Route BEFORE taking the lock (doc_open locks g_mtx).
    if (fmt.empty() && is_stepplus_path(path)) {
        if (into.empty())
            return doc_open(path, as_solid);
        // Import a native .step+ INTO an existing doc for assembly (Issue 2). Native is the
        // recommended save format, yet it used to be the one format you couldn't assemble from
        // (the caller had to re-export every part to plain STEP/BREP first). We open the
        // container as a throwaway doc, bake its compiled solids into an embedded-BREP import
        // feature on `into` (the same representation a STEP import uses — survives source
        // deletion), then discard the temp doc. Routed before the lock: doc_open locks g_mtx.
        json opened = doc_open(path, as_solid);
        const std::string tmp_id = opened.value("doc_id", std::string());
        if (tmp_id.empty())
            throw OpError("import_failed", "import_file: could not open .step+ container",
                          {{"path", path}});
        const std::string stem = std::filesystem::path(path).stem().string();
        std::string blob;
        std::size_t n = 0;
        {
            DocLock lk;
            Session& tmp = require_session(tmp_id);
            CompileResult r = compile_document(tmp.doc, env_from_doc(tmp.doc));
            TopoDS_Compound comp;
            BRep_Builder bb;
            bb.MakeCompound(comp);
            for (const TopoDS_Shape& shp : r.shapes)
                if (!shp.IsNull()) { bb.Add(comp, shp); ++n; }
            if (n > 0) blob = shape_to_brep_b64(comp);
        }
        try { doc_close(tmp_id); } catch (...) { /* best-effort cleanup of the temp doc */ }
        if (n == 0 || blob.empty())
            throw OpError("import_failed", "import_file: .step+ contained no solids to import",
                          {{"path", path}});
        DocLock lk;
        Session& s = require_session(into);
        const std::string fname = unique_feature_name(s, stem);  // Issue 10
        json params = {{"source_path", path}, {"format", "step+"}, {"original_name", stem},
                       {"embedded_brep", blob}, {"embedded_bodies", n}};
        json ops = json::array({json{
            {"op", "add_feature"},
            {"feature", {{"type", "import"}, {"name", fname}, {"params", params},
                         {"refs", json::object()}}}}});
        json resp = apply_ops_locked(s, -1, ops, /*coalesce=*/false);
        resp["doc_id"] = s.doc_id;
        return resp;
    }

    DocLock lk;
    if (path.empty())
        throw OpError("invalid_op", "import_file: 'path' is required", {{"reason", "missing path"}});
    // Resolve the format up front so a bad extension errors before we touch a session.
    std::string f = fmt.empty() ? detect_format(path) : fmt;
    const std::string stem = std::filesystem::path(path).stem().string();

    // SolidWorks / Parasolid have no free B-rep reader: drive SolidWorks via COM to export a
    // neutral STEP, then import THAT through the normal B-rep lane (the part keeps the source
    // file's stem as its name). Throws an actionable error if SolidWorks/COM is unavailable.
    std::string import_path_ = path;
    if (f == "sldprt" || f == "sldasm" || f == "x_t" || f == "x_b") {
        import_path_ = sw_export_step(path);
        f = "step";
    }

    json params = {{"source_path", import_path_}, {"format", f}, {"original_name", stem}};
    if (as_solid) params["as_solid"] = true;
    // BAKE the geometry into the feature at import time so a saved .step+ no longer
    // depends on the source file surviving on disk (Bug N-8). We read the source ONCE
    // here, serialize all imported solids to a lossless BREP blob, and carry it in the
    // feature params. The compiler prefers this embedded geometry; source_path stays as
    // provenance and a rebuild fallback. Fail-soft: if the read throws, we leave the
    // feature path-only (the compiler will then read the source as before).
    try {
        auto bodies = import_bodies(import_path_, f);
        TopoDS_Compound comp;
        BRep_Builder bb;
        bb.MakeCompound(comp);
        std::size_t n = 0;
        for (auto& [bname, shp] : bodies)
            if (!shp.IsNull()) { bb.Add(comp, shp); ++n; }
        if (n > 0) {
            std::string blob = shape_to_brep_b64(comp);
            if (!blob.empty()) {
                params["embedded_brep"] = blob;
                params["embedded_bodies"] = n;
            }
        }
    } catch (...) {
        // leave path-only; compiler falls back to source_path
    }
    Session* s = nullptr;
    if (!into.empty())
        s = &require_session(into);  // append to an existing C++ session
    else
        s = &new_part_session_locked(stem.empty() ? std::string("Imported") : stem);

    // Uniquify the feature name against the target doc (Issue 10) so repeated imports of the
    // same file don't all share one name (and trip the duplicate-name warning every time).
    const std::string fname = unique_feature_name(*s, stem.empty() ? std::string("Imported") : stem);
    json ops = json::array({json{
        {"op", "add_feature"},
        {"feature", {{"type", "import"}, {"name", fname}, {"params", params}, {"refs", json::object()}}}}});

    json resp = apply_ops_locked(*s, -1, ops, /*coalesce=*/false);
    resp["doc_id"] = s->doc_id;
    return resp;
}

// Slug a string the SAME way engine/vendor_parts.py::slug does: trim whitespace, collapse every
// run of chars outside [A-Za-z0-9_.-] to a single '-', strip leading/trailing '-', lowercase;
// empty -> "part". Used to rebuild the vendor index key `slug(supplier)::slug(part_number)`.
static std::string vendor_slug(const std::string& v) {
    size_t b = v.find_first_not_of(" \t\r\n");
    size_t e = v.find_last_not_of(" \t\r\n");
    std::string s = (b == std::string::npos) ? std::string() : v.substr(b, e - b + 1);
    std::string t;
    bool prev_dash = false;
    for (char c : s) {
        const bool ok = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                        (c >= '0' && c <= '9') || c == '_' || c == '.' || c == '-';
        if (ok) { t += c; prev_dash = false; }
        else if (!prev_dash) { t += '-'; prev_dash = true; }
    }
    size_t lb = t.find_first_not_of('-');
    size_t le = t.find_last_not_of('-');
    t = (lb == std::string::npos) ? std::string() : t.substr(lb, le - lb + 1);
    for (char& c : t) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return t.empty() ? std::string("part") : t;
}

// Insert a registered supplier/vendor CAD part as an `import` feature (port of
// engine/vendor_parts.py::_vendor_part_insert, which is now removed from the Python worker — the
// doc mutation lives here). The vendor REGISTRY stays in Python (register/list/context write
// %APPDATA%/AI-CAD/vendor_parts/index.json); this reads that SAME index, resolves the cached CAD
// path + metadata, and applies an import op carrying the vendor_* params verbatim. `into` empty ->
// mint a new part doc. Throws OpError on a missing index / unknown part / missing CAD file.
json doc_vendor_part_insert(const std::string& into, const std::string& supplier,
                            const std::string& part_number, const std::string& name) {
    DocLock lk;
    // %APPDATA%/AI-CAD/vendor_parts/index.json (mirror of engine/paths.py::appdata_root()).
    const char* appdata = std::getenv("APPDATA");
    std::filesystem::path root;
    if (appdata && appdata[0]) {
        root = std::filesystem::path(appdata);
    } else {
        const char* home = std::getenv("USERPROFILE");
        root = std::filesystem::path(home && home[0] ? home : ".") / "AppData" / "Roaming";
    }
    const std::filesystem::path index = root / "AI-CAD" / "vendor_parts" / "index.json";
    if (!std::filesystem::exists(index))
        throw OpError("invalid_op", "vendor_part_insert: no vendor parts registered",
                      {{"reason", "no vendor index"}, {"index", index.string()}});

    json idx;
    try {
        std::ifstream in(index);
        in >> idx;
    } catch (const std::exception& ex) {
        throw OpError("invalid_op",
                      std::string("vendor_part_insert: cannot read index: ") + ex.what(),
                      {{"reason", "bad index"}});
    }
    const std::string key = vendor_slug(supplier) + "::" + vendor_slug(part_number);
    if (!idx.is_object() || !idx.contains(key))
        throw OpError("invalid_op", "vendor part not registered: " + supplier + " " + part_number,
                      {{"reason", "unknown part"}, {"supplier", supplier},
                       {"part_number", part_number}});
    const json& rec = idx[key];
    std::string cad_path = rec.value("cad_path", std::string());
    if (cad_path.empty() || !std::filesystem::exists(cad_path))
        throw OpError("invalid_op",
                      "registered CAD file is missing for " + supplier + " " + part_number,
                      {{"reason", "missing cad file"}, {"cad_path", cad_path}});

    std::string feat_name = !name.empty()
                                ? name
                                : (rec.value("supplier", std::string()) + " " +
                                   rec.value("part_number", std::string()));
    std::string fmt = rec.value("cad_format", std::string());
    // SolidWorks / Parasolid have no free B-rep reader: route through COM -> STEP first, exactly
    // like doc_import_file.
    std::string import_path_ = cad_path;
    if (fmt == "sldprt" || fmt == "sldasm" || fmt == "x_t" || fmt == "x_b") {
        import_path_ = sw_export_step(cad_path);
        fmt = "step";
    }
    const std::string original = rec.value("description", std::string()).empty()
                                     ? feat_name
                                     : rec.value("description", std::string());
    json params = {{"source_path", import_path_},
                   {"format", fmt.empty() ? detect_format(cad_path) : fmt},
                   {"original_name", original},
                   {"vendor_supplier", rec.value("supplier", std::string())},
                   {"vendor_part_number", rec.value("part_number", std::string())},
                   {"vendor_product_url", rec.value("product_url", std::string())}};
    json ops = json::array({json{
        {"op", "add_feature"},
        {"feature",
         {{"type", "import"}, {"name", feat_name}, {"params", params}, {"refs", json::object()}}}}});

    Session* s = nullptr;
    if (!into.empty())
        s = &require_session(into);
    else
        s = &new_part_session_locked(feat_name.empty() ? std::string("Vendor Part") : feat_name);

    json resp = apply_ops_locked(*s, -1, ops, /*coalesce=*/false);
    resp["doc_id"] = s->doc_id;
    resp["vendor_part"] = rec;
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

// Bake any import feature that still carries only a source_path reference into a
// self-contained `embedded_brep` blob, so the saved .step+ survives the source file
// being deleted (Bug N-8 insurance — covers parts imported before embedding existed, or
// where the import-time embed fail-softed). Reads the SOURCE file; if it is gone we leave
// the feature as-is (nothing we can do without geometry). Returns the number baked.
static int bake_import_features(json& doc) {
    if (!doc.contains("features") || !doc["features"].is_array()) return 0;
    int baked = 0;
    for (json& feat : doc["features"]) {
        if (!feat.is_object() || feat.value("type", std::string()) != "import") continue;
        // Params may be nested (canonical {params}) or flat — write/read either shape.
        json* dst = (feat.contains("params") && feat["params"].is_object()) ? &feat["params"]
                                                                            : &feat;
        auto has_embed = [&](const json& o) {
            auto it = o.find("embedded_brep");
            return it != o.end() && it->is_string() && !it->get<std::string>().empty();
        };
        if (has_embed(feat) || has_embed(*dst)) continue;  // already self-contained
        auto get_str = [&](const char* k) -> std::string {
            if (dst->contains(k) && (*dst)[k].is_string()) return (*dst)[k].get<std::string>();
            if (feat.contains(k) && feat[k].is_string()) return feat[k].get<std::string>();
            return std::string();
        };
        const std::string src = get_str("source_path");
        if (src.empty() || !std::filesystem::exists(src)) continue;
        std::string ifmt = get_str("format");
        try {
            if (ifmt.empty()) ifmt = detect_format(src);
            auto bodies = import_bodies(src, ifmt);
            TopoDS_Compound comp;
            BRep_Builder bb;
            bb.MakeCompound(comp);
            std::size_t n = 0;
            for (auto& [bname, shp] : bodies)
                if (!shp.IsNull()) { bb.Add(comp, shp); ++n; }
            if (n == 0) continue;
            std::string blob = shape_to_brep_b64(comp);
            if (blob.empty()) continue;
            (*dst)["embedded_brep"] = blob;
            (*dst)["embedded_bodies"] = n;
            ++baked;
        } catch (...) {
            // unreadable source — leave path-only
        }
    }
    return baked;
}

json doc_save(const std::string& doc_id, const std::string& path) {
    // W12+: embed every open drawing whose live source is this doc as a drawings/<id>.json
    // entry. Gather them BEFORE taking the session lock — drawings_for_source takes the
    // (separate) drawing lock, and a drawing projection takes drawing->session, so doing it
    // under the session lock would invert the lock order. drawings_for_source touches no
    // session state, so it is safe to call lock-free here.
    std::map<std::string, std::string> drawing_entries = drawings_for_source(doc_id);

    DocLock lk;
    Session& s = require_session(doc_id);
    const std::string target = !path.empty() ? path : s.path;
    if (target.empty())
        throw OpError("io_error",
                      "doc_save: no path given and document has no current path",
                      {{"reason", "missing path"}});
    // Re-write the live drawing set: drop any stale drawings/*.json carried over from a
    // previous open, then add the current ones (mirrors stepplus.py::save).
    for (auto it = s.extra_entries.begin(); it != s.extra_entries.end();) {
        if (it->first.rfind("drawings/", 0) == 0) it = s.extra_entries.erase(it);
        else ++it;
    }
    for (auto& [name, data] : drawing_entries) s.extra_entries[name] = data;

    // Make every imported body self-contained before writing (Bug N-8).
    bake_import_features(s.doc);

    std::string out = stepplus_save(s.doc, target, s.extra_entries);
    s.path = out;
    s.dirty = false;
    return json{{"path", out}};
}

json doc_open(const std::string& path, bool as_solid) {
    // Manual File>Open / File>Import of a NON-native file (.step/.stp/.iges/.brep/.stl/
    // .obj/.ply/.gltf/.glb/.3mf/.3dm/.sldprt/…) is NOT a .step+ container — importing it
    // as a fresh part doc is what the user expects (and is why "open a non-.step+ part"
    // previously did nothing: the zip reader rejected it and nothing fell back). Route it
    // through the import lane, which mints a new part doc with the file's geometry baked in.
    if (!path.empty() && !is_stepplus_path(path))
        return doc_import_file(path, "", as_solid, "");

    std::string opened_doc_id;
    std::map<std::string, std::string> drawing_entries;  // restored after the session lock
    json resp;
    {
    DocLock lk;
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
    resp = warm_and_respond(stored, std::move(r), json::object(), /*with_tess=*/true);
    resp["doc_id"] = stored.doc_id;
    if (stored.doc.is_object())
        resp["name"] = stored.doc.value("name", std::string());
    opened_doc_id = stored.doc_id;
    // Capture embedded drawings to restore AFTER releasing the session lock (registering
    // a drawing takes the drawing lock; doing it here would invert the lock order).
    for (const auto& [name, data] : stored.extra_entries)
        if (name.rfind("drawings/", 0) == 0) drawing_entries[name] = data;

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
    }  // release the session lock before restoring drawings (lock-order safety)

    // W12+ — restore any embedded drawings as FRESH drawing sessions re-pointed at this
    // reopened part, so they project against the reopened bodies. Fully contained: a
    // malformed / non-drawing entry is skipped, never breaking the part open. The host
    // reads resp["drawings"] to re-create the Drawing tabs (doc_mirror lastOpenedDrawings).
    json drawings = json::array();
    for (const auto& [name, data] : drawing_entries) {
        try {
            json ddoc = json::parse(data);
            drawings.push_back(register_restored_drawing(ddoc, opened_doc_id));
        } catch (const std::exception&) {
            // skip a malformed drawing entry (mirrors stepplus.restore_drawings)
        }
    }
    resp["drawings"] = drawings;
    return resp;
}

// ============================================================ C18 sketch RPCs
// Port of engine/doc/session.py sketch_enter/solve/drag/drag_commit/exit/overview/
// regions/probe/project_ref. doc_id-keyed; operate on the per-session active_sketch.

json doc_sketch_enter(const std::string& doc_id, const std::string& sketch_fid) {
    DocLock lk;
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
    DocLock lk;
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
    DocLock lk;
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
    DocLock lk;
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
    DocLock lk;
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
    DocLock lk;
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
    return warm_and_respond(s, std::move(r), json::object(), /*with_tess=*/true);
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
    DocLock lk;
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
    DocLock lk;
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
    DocLock lk;
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
