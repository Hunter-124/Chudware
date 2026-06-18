// ECAD session implementation — see session.h. Mirrors engine/ecad/session.py
// (bounded JSON-snapshot undo/redo, stale-revision guard, revision bumps) and
// engine/ecad/rpc.py response shapes. Pure JSON; no OCCT.

#include "ecad/session.h"

#include <atomic>
#include <cstdio>
#include <map>
#include <mutex>
#include <vector>

#include "ecad/erc.h"
#include "ecad/netlist.h"
#include "ecad/schema.h"

namespace aicad::ecad {

using nlohmann::json;

namespace {

constexpr std::size_t kUndoLimit = 100;

struct Session {
    std::string doc_id;
    json doc;
    long revision = 0;
    std::string path;
    bool dirty = false;
    std::vector<std::string> undo_stack;  // canonical JSON snapshots (doc only)
    std::vector<std::string> redo_stack;
    json last_compile = nullptr;
};

std::mutex g_mtx;
std::map<std::string, Session> g_sessions;
std::atomic<unsigned long long> g_seq{1};

std::string new_doc_id() {
    unsigned long long n = g_seq.fetch_add(1);
    char buf[32];
    std::snprintf(buf, sizeof(buf), "ecadcpp%08llx", n);
    return std::string(buf);
}

Session& require_session(const std::string& doc_id) {
    auto it = g_sessions.find(doc_id);
    if (it == g_sessions.end())
        throw OpError("unknown_doc", "no open ECAD document '" + doc_id + "'", {{"doc_id", doc_id}});
    return it->second;
}

// Canonical snapshot (sorted keys, compact) — same intent as Python's
// json.dumps(sort_keys=True, separators=(",", ":")). nlohmann::json::parse round-
// trips it back to an identical object.
std::string snapshot(const json& doc) { return doc.dump(); }

void push_undo(Session& s) {
    s.undo_stack.push_back(snapshot(s.doc));
    if (s.undo_stack.size() > kUndoLimit) s.undo_stack.erase(s.undo_stack.begin());
    s.redo_stack.clear();
}

// The `compile` field (engine/ecad/session.py::_compile). For a SCHEMATIC the
// netlist + ERC are now computed in-process (build_netlist + run_erc), matching
// the Python oracle. For a BOARD the DRC/ratsnest path is still DEFERRED, so the
// pcb branch keeps the skeleton (drc:[]/ratsnest:[]). The timing `stats.ms` is
// omitted (non-deterministic); the harness compares compile.netlist + compile.erc.
json compile_field(const json& doc) {
    json out = {{"netlist", nullptr}, {"erc", json::array()}, {"drc", json::array()},
                {"ratsnest", json::array()}, {"stats", json::object()}};
    if (schema::is_schematic(doc)) {
        json netlist = build_netlist(doc);
        out["netlist"] = netlist;
        out["erc"] = run_erc(doc, netlist);
        out["stats"]["nets"] = netlist.contains("nets") && netlist["nets"].is_object()
                                   ? netlist["nets"].size()
                                   : 0;
    } else if (schema::is_board(doc)) {
        out["stats"]["nets"] = doc.contains("nets") && doc["nets"].is_array() ? doc["nets"].size() : 0;
        out["stats"]["unrouted"] = 0;
    }
    return out;
}

json ops_response(Session& s, const json& id_map, const json& warnings) {
    json cf = compile_field(s.doc);
    s.last_compile = cf;
    json resp = {{"revision", s.revision}, {"id_map", id_map}, {"kind", schema::doc_kind(s.doc)},
                 {"tree", s.doc}, {"compile", cf}};
    if (warnings.is_array() && !warnings.empty()) resp["warnings"] = warnings;
    return resp;
}

}  // namespace

json ecad_new(const std::string& kind, const std::string& name) {
    std::lock_guard<std::mutex> lk(g_mtx);
    json doc;
    if (kind == "sch")
        doc = schema::new_schematic(name.empty() ? "Schematic1" : name);
    else if (kind == "pcb")
        doc = schema::new_board(name.empty() ? "Board1" : name);
    else
        throw OpError("invalid_op", "doc kind must be sch|pcb, got '" + kind + "'",
                      {{"reason", "bad kind"}});
    Session s;
    s.doc_id = new_doc_id();
    s.doc = std::move(doc);
    s.doc["id"] = s.doc_id;  // Python uses a uuid4; the session id is faithful here
    s.revision = 0;
    Session& stored = g_sessions[s.doc_id] = std::move(s);
    // ecad_new -> rebuild() (no op application, no rev bump) + doc_id.
    json resp = {{"revision", stored.revision}, {"id_map", json::object()},
                 {"kind", schema::doc_kind(stored.doc)}, {"tree", stored.doc},
                 {"compile", compile_field(stored.doc)}};
    stored.last_compile = resp["compile"];
    resp["doc_id"] = stored.doc_id;
    return resp;
}

json ecad_apply_ops(const std::string& doc_id, long base_revision, const json& ops) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    if (base_revision >= 0 && base_revision != s.revision)
        throw OpError("stale_revision",
                      "base_revision " + std::to_string(base_revision) + " != current " +
                          std::to_string(s.revision),
                      {{"current_revision", s.revision}});

    json res = apply_ops(s.doc, ops.is_null() ? json::array() : ops);  // throws on failure

    push_undo(s);
    s.doc = res["doc"];
    s.revision += 1;
    s.dirty = true;
    return ops_response(s, res["id_map"],
                        res.contains("warnings") ? res["warnings"] : json::array());
}

json ecad_undo(const std::string& doc_id) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    if (s.undo_stack.empty())
        throw OpError("invalid_op", "nothing to undo", {{"reason", "empty undo stack"}});
    s.redo_stack.push_back(snapshot(s.doc));
    std::string snap = s.undo_stack.back();
    s.undo_stack.pop_back();
    s.doc = json::parse(snap);
    s.revision += 1;
    s.dirty = true;
    return ops_response(s, json::object(), json::array());
}

json ecad_redo(const std::string& doc_id) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    if (s.redo_stack.empty())
        throw OpError("invalid_op", "nothing to redo", {{"reason", "empty redo stack"}});
    s.undo_stack.push_back(snapshot(s.doc));
    if (s.undo_stack.size() > kUndoLimit) s.undo_stack.erase(s.undo_stack.begin());
    std::string snap = s.redo_stack.back();
    s.redo_stack.pop_back();
    s.doc = json::parse(snap);
    s.revision += 1;
    s.dirty = true;
    return ops_response(s, json::object(), json::array());
}

json ecad_tree(const std::string& doc_id) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    json cf = s.last_compile.is_null() ? compile_field(s.doc) : s.last_compile;
    s.last_compile = cf;
    return json{{"revision", s.revision}, {"kind", schema::doc_kind(s.doc)},
                {"tree", s.doc}, {"compile", cf}};
}

json ecad_rebuild(const std::string& doc_id) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    return ops_response(s, json::object(), json::array());
}

bool ecad_close(const std::string& doc_id) {
    std::lock_guard<std::mutex> lk(g_mtx);
    return g_sessions.erase(doc_id) > 0;
}

json ecad_describe(const std::string& doc_id) {
    std::lock_guard<std::mutex> lk(g_mtx);
    Session& s = require_session(doc_id);
    const json& doc = s.doc;
    json compile_info = s.last_compile.is_null() ? json::object() : s.last_compile;
    int nets = 0;
    if (compile_info.is_object() && compile_info.contains("stats") &&
        compile_info["stats"].contains("nets"))
        nets = compile_info["stats"]["nets"].get<int>();
    int erc_violations = 0;
    if (compile_info.is_object() && compile_info.contains("erc") && compile_info["erc"].is_array())
        erc_violations = (int)compile_info["erc"].size();
    int drc_violations = 0;
    if (compile_info.is_object() && compile_info.contains("drc") && compile_info["drc"].is_array())
        drc_violations = (int)compile_info["drc"].size();
    int unrouted = 0;
    if (compile_info.is_object() && compile_info.contains("stats") &&
        compile_info["stats"].contains("unrouted"))
        unrouted = compile_info["stats"]["unrouted"].get<int>();
    auto len = [&](const char* k) -> int {
        return doc.contains(k) && doc[k].is_array() ? (int)doc[k].size() : 0;
    };
    if (schema::is_schematic(doc)) {
        return json{{"kind", "sch"}, {"name", doc.value("name", std::string())},
                    {"revision", s.revision}, {"symbols", len("symbols")}, {"wires", len("wires")},
                    {"nets", nets}, {"erc_violations", erc_violations}};
    }
    return json{{"kind", "pcb"}, {"name", doc.value("name", std::string())},
                {"revision", s.revision}, {"footprints", len("footprints")},
                {"tracks", len("tracks")}, {"vias", len("vias")}, {"nets", nets},
                {"unrouted", unrouted}, {"drc_violations", drc_violations}};
}

json ecad_schema(const std::string& kind) {
    if (kind == "pcb") return schema::new_board("Board");
    return schema::new_schematic("Schematic");
}

}  // namespace aicad::ecad
