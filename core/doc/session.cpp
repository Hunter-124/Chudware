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

#include "doc/document.h"
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
};

constexpr std::size_t kUndoLimit = 100;

std::mutex g_mtx;
std::map<std::string, Session> g_sessions;
std::atomic<unsigned long long> g_doc_seq{1};

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
    json working = s.doc;
    json id_map = json::object();
    IdGen gen(working);
    const json empty = json::array();
    const json& batch = ops.is_array() ? ops : empty;
    for (std::size_t i = 0; i < batch.size(); ++i)
        apply_one(working, batch[i], i, gen, id_map);

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

json doc_resolve_pick(const std::string& doc_id, int body_id, const std::string& kind,
                      int topo_id) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    json sel = resolve_pick(s.doc, env_from_doc(s.doc), body_id, kind, topo_id);
    return json{{"selector", sel}};
}

}  // namespace aicad::doc
