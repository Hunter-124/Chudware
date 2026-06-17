// Stateful document session implementation — see session.h.
//
// Mirrors engine/doc/session.py + engine/doc/ops.py over compile_document. JSON
// in, JSON out; never mutates a session until an op batch fully applies (the
// Python transactional contract: apply to a working copy, commit on success).

#include "doc/session.h"

#include <atomic>
#include <cctype>
#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <map>
#include <mutex>
#include <regex>
#include <set>
#include <string>
#include <vector>

#include <cmath>
#include <optional>

#include "doc/document.h"
#include "doc/sketch_geom.h"
#include "doc/stepplus.h"
#include "doc/validate.h"
#include "util/expr.h"

namespace aicad::doc {

using nlohmann::json;

namespace {

// ----------------------------------------------------------------- session store
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
        resp["rebuild"] = {{"feature_status", fs},
                           {"doc_status", {{"active_sketch", nullptr},
                                           {"mates", nullptr},
                                           {"active_configuration", "Default"}}}};
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

}  // namespace

// ----------------------------------------------------------------- public API
json doc_new(const std::string& kind, const std::string& name) {
    if (!kind.empty() && kind != "part")
        throw OpError("unsupported", "doc_new: only 'part' documents are supported in the C++ core",
                      {{"kind", kind}});
    std::lock_guard<std::mutex> lk(g_mtx);
    Session s;
    s.doc_id = new_doc_id();
    s.doc = blank_part(s.doc_id, name);
    s.revision = 0;
    Session& stored = g_sessions[s.doc_id] = std::move(s);
    CompileResult r = compile_document(stored.doc, env_from_doc(stored.doc));
    json resp = ops_response(stored, r, json::object(), /*with_tess=*/true);
    resp["doc_id"] = stored.doc_id;
    return resp;
}

json doc_apply_ops(const std::string& doc_id, long base_revision, const json& ops, bool coalesce) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    if (base_revision >= 0 && base_revision != s.revision)
        throw OpError("stale_revision",
                      "base_revision " + std::to_string(base_revision) + " != current " +
                          std::to_string(s.revision),
                      {{"current_revision", s.revision}});

    // Transactional: apply to a working copy; throw leaves the session untouched.
    if (!ops.is_array())
        throw OpError("invalid_op", "ops must be a list", {{"index", -1}, {"reason", "ops not a list"}});
    json working = s.doc;
    json id_map = json::object();
    IdGen gen(working);
    for (std::size_t i = 0; i < ops.size(); ++i)
        apply_one(working, ops[i], i, gen, id_map);

    // Resulting-document validation (doc_ops.md §2.1): reject batches that produce an
    // invalid doc — same contract as the Python apply_ops (schema.validate_document).
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

    CompileResult r = compile_document(s.doc, env_from_doc(s.doc));
    return ops_response(s, r, id_map, /*with_tess=*/true);
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
    CompileResult r = compile_document(s.doc, env_from_doc(s.doc));
    return ops_response(s, r, json::object(), /*with_tess=*/true);
}

json doc_tree(const std::string& doc_id) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
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
    CompileResult r = compile_document(s.doc, env_from_doc(s.doc));
    return ops_response(s, r, json::object(), /*with_tess=*/true);
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

json doc_resolve_pick(const std::string& doc_id, int body_id, const std::string& kind,
                      int topo_id) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    json sel = resolve_pick(s.doc, env_from_doc(s.doc), body_id, kind, topo_id);
    return json{{"selector", sel}};
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

}  // namespace aicad::doc
